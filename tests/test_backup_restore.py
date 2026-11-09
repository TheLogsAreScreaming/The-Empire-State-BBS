import json
import shutil
import sqlite3
import stat
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import empire_backup
from empire_backup import BackupError, inspect_backup, restore_backup


def make_db(path, value):
    connection = sqlite3.connect(path)
    with connection:
        connection.execute("CREATE TABLE IF NOT EXISTS t (v TEXT)")
        connection.execute("DELETE FROM t")
        connection.execute("INSERT INTO t VALUES (?)", (value,))
    connection.close()


def read_db(path):
    connection = sqlite3.connect(path)
    try:
        return connection.execute("SELECT v FROM t").fetchone()[0]
    finally:
        connection.close()


class RestoreTestCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.temp = Path(temp.name)
        self.data = self.temp / "data"
        self.source = self.temp / "source"
        self.data.mkdir()
        self.source.mkdir()
        self.backup = self.temp / "backup.zip"

    def destination(self, name):
        return self.data / name

    def restore(self):
        return restore_backup(self.backup, self.destination, self.data, clock=lambda: 1000)

    def write_backup(self, members):
        with zipfile.ZipFile(self.backup, "w") as archive:
            for name, content in members.items():
                archive.writestr(name, content)

    def good_backup(self):
        make_db(self.source / "bulletins.db", "restored")
        (self.source / "launcher.json").write_text(json.dumps({"welcome": "restored"}))
        (self.source / "config.ini").write_text("[interface]\ntype = tcp\n")
        with zipfile.ZipFile(self.backup, "w") as archive:
            for path in self.source.iterdir():
                archive.write(path, path.name)

    def current_state(self):
        make_db(self.data / "bulletins.db", "current")
        (self.data / "launcher.json").write_text(json.dumps({"welcome": "current"}))


class RestoreBehaviourTests(RestoreTestCase):
    def test_restores_files_and_keeps_a_safety_copy(self):
        self.good_backup()
        self.current_state()
        result = self.restore()
        self.assertEqual(result.names, ["bulletins.db", "config.ini", "launcher.json"])
        self.assertEqual(read_db(self.data / "bulletins.db"), "restored")
        self.assertEqual(json.loads((self.data / "launcher.json").read_text())["welcome"], "restored")
        with zipfile.ZipFile(result.safety_copy) as archive:
            self.assertEqual(sorted(archive.namelist()), ["bulletins.db", "launcher.json"])
            self.assertIn(b"current", archive.read("launcher.json"))
        self.assertEqual(stat.S_IMODE(result.safety_copy.stat().st_mode), 0o600)

    def test_files_missing_from_the_backup_are_left_alone(self):
        self.good_backup()
        make_db(self.data / "node-directory.db", "keep me")
        self.restore()
        self.assertEqual(read_db(self.data / "node-directory.db"), "keep me")

    def test_restored_files_are_private(self):
        self.good_backup()
        self.restore()
        self.assertEqual(stat.S_IMODE((self.data / "config.ini").stat().st_mode), 0o600)

    def test_stale_sqlite_journal_is_not_replayed(self):
        self.good_backup()
        self.current_state()
        (self.data / "bulletins.db-wal").write_bytes(b"stale")
        (self.data / "bulletins.db-shm").write_bytes(b"stale")
        self.restore()
        self.assertFalse((self.data / "bulletins.db-wal").exists())
        self.assertFalse((self.data / "bulletins.db-shm").exists())
        self.assertEqual(read_db(self.data / "bulletins.db"), "restored")

    def test_leaves_no_working_folder_behind(self):
        self.good_backup()
        self.restore()
        self.assertEqual([p.name for p in self.data.glob(".restore-*")], [])

    def test_failed_swap_puts_everything_back(self):
        self.good_backup()
        self.current_state()
        (self.data / "bulletins.db-wal").write_bytes(b"journal")
        real_move = shutil.move

        def failing_move(source, target):
            if Path(target).name == "launcher.json" and "/new/" in source:
                raise OSError("disk full")
            return real_move(source, target)

        with patch.object(empire_backup.shutil, "move", failing_move):
            with self.assertRaisesRegex(BackupError, "current data was kept"):
                self.restore()
        self.assertEqual((self.data / "bulletins.db-wal").read_bytes(), b"journal")
        self.assertEqual(read_db(self.data / "bulletins.db"), "current")
        self.assertEqual(json.loads((self.data / "launcher.json").read_text())["welcome"], "current")
        self.assertFalse((self.data / "config.ini").exists())


class RestoreRefusalTests(RestoreTestCase):
    def assert_refused(self, message):
        self.current_state()
        before = {p.name: p.read_bytes() for p in self.data.iterdir()}
        with self.assertRaisesRegex(BackupError, message):
            self.restore()
        self.assertEqual({p.name: p.read_bytes() for p in self.data.iterdir()}, before)

    def test_not_a_zip(self):
        self.backup.write_text("nope")
        self.assert_refused("can't be read")

    def test_empty_archive(self):
        self.write_backup({})
        self.assert_refused("empty")

    def test_path_traversal(self):
        self.write_backup({"../evil.db": b""})
        self.assert_refused("Unexpected file")

    def test_absolute_and_nested_paths(self):
        for name in ("/tmp/evil.db", "sub/evil.db"):
            with self.subTest(name):
                self.write_backup({name: b""})
                self.assert_refused("Unexpected file")

    def test_unknown_file_type(self):
        self.write_backup({"launcher.json": "{}", "payload.command": "x"})
        self.assert_refused("Unexpected file")

    def test_symlink_entry(self):
        info = zipfile.ZipInfo("bulletins.db")
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        with zipfile.ZipFile(self.backup, "w") as archive:
            archive.writestr(info, "/etc/passwd")
        self.assert_refused("link")

    def test_oversized_contents(self):
        self.write_backup({"launcher.json": "{}"})
        with patch.object(empire_backup, "MAX_TOTAL_BYTES", 1):
            self.assert_refused("too large")

    def test_corrupt_database(self):
        self.write_backup({"bulletins.db": b"this is not a database at all" * 100})
        self.assert_refused("bulletins.db in this backup is not valid")

    def test_invalid_settings(self):
        for content in ("{broken", "[1, 2]"):
            with self.subTest(content):
                self.write_backup({"launcher.json": content})
                self.assert_refused("launcher.json in this backup is not valid")

    def test_invalid_config(self):
        self.write_backup({"config.ini": "no section header"})
        self.assert_refused("config.ini in this backup is not valid")

    def test_damaged_archive(self):
        self.write_backup({"launcher.json": "{" + '"a": "' + "x" * 200 + '"}'})
        raw = bytearray(self.backup.read_bytes())
        raw[raw.index(b"xxxx")] = ord("y")
        self.backup.write_bytes(raw)
        self.assert_refused("damaged|can't be read")


class BackupRoundTripTests(RestoreTestCase):
    def test_inspect_lists_files(self):
        self.good_backup()
        self.assertEqual(inspect_backup(self.backup), ["bulletins.db", "config.ini", "launcher.json"])


def bare_app():
    from sitdown_desktop import SitdownApp
    app = SitdownApp.__new__(SitdownApp)
    app.root = Mock()
    app.log = Mock()
    app.toast = Mock()
    app.load = Mock()
    app.refresh_node_count = Mock()
    app.supervisor = None
    return app


class RestoreCommandTests(RestoreTestCase):
    def run_restore(self, app, confirm=True, chosen=None):
        chosen = str(self.backup) if chosen is None else chosen
        with patch("sitdown_desktop.DATA", self.data), patch("sitdown_desktop.ROOT", self.temp / "bundle"), \
             patch("sitdown_desktop.CONFIG", self.data / "launcher.json"), \
             patch("sitdown_desktop.filedialog.askopenfilename", return_value=chosen) as pick, \
             patch("sitdown_desktop.messagebox.askyesno", return_value=confirm) as ask, \
             patch("sitdown_desktop.messagebox.showerror") as error, \
             patch("sitdown_desktop.messagebox.showinfo") as info:
            app.restore()
        return pick, ask, error, info

    def test_refused_while_the_server_is_running(self):
        self.good_backup()
        self.current_state()
        app = bare_app()
        app.supervisor = Mock(running=True)
        pick, _ask, error, _info = self.run_restore(app)
        pick.assert_not_called()
        error.assert_called_once()
        self.assertEqual(read_db(self.data / "bulletins.db"), "current")

    def test_declining_the_confirmation_changes_nothing(self):
        self.good_backup()
        self.current_state()
        _pick, ask, _error, info = self.run_restore(bare_app(), confirm=False)
        ask.assert_called_once()
        self.assertIn("bulletins.db", ask.call_args.args[1])
        info.assert_not_called()
        self.assertEqual(read_db(self.data / "bulletins.db"), "current")

    def test_confirmed_restore_replaces_data_and_reloads_settings(self):
        self.good_backup()
        self.current_state()
        app = bare_app()
        _pick, _ask, error, info = self.run_restore(app)
        error.assert_not_called()
        info.assert_called_once()
        self.assertIn("reopen", info.call_args.args[1])
        app.load.assert_called_once()
        self.assertEqual(read_db(self.data / "bulletins.db"), "restored")

    def test_invalid_backup_shows_an_error_before_asking(self):
        self.backup.write_text("nope")
        _pick, ask, error, _info = self.run_restore(bare_app())
        ask.assert_not_called()
        error.assert_called_once()

    def test_cancelled_picker_does_nothing(self):
        _pick, ask, error, _info = self.run_restore(bare_app(), chosen="")
        ask.assert_not_called()
        error.assert_not_called()


if __name__ == "__main__":
    unittest.main()
