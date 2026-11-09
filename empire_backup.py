"""Validate a data backup and restore it without leaving a half-replaced folder."""
import configparser
import json
import os
import re
import shutil
import sqlite3
import stat
import tempfile
import time
import zipfile
from collections import namedtuple
from pathlib import Path

MAX_MEMBERS = 64
MAX_TOTAL_BYTES = 512 * 1024 * 1024
SIDE_FILES = ("-wal", "-shm")
_DATABASE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*\.db$")

RestoreResult = namedtuple("RestoreResult", "names safety_copy")


class BackupError(ValueError):
    """The backup can't be restored; the message is safe to show the operator."""


def allowed_name(name):
    return name in ("launcher.json", "config.ini") or bool(_DATABASE.match(name))


def inspect_backup(path):
    """Return the file names in a backup, refusing anything Back up data can't produce."""
    try:
        with zipfile.ZipFile(path) as archive:
            members = archive.infolist()
            _check_members(members)
            damaged = archive.testzip()
    except (zipfile.BadZipFile, OSError, EOFError, RuntimeError, NotImplementedError) as exc:
        raise BackupError(f"This file can't be read as a backup: {exc}") from exc
    if damaged:
        raise BackupError(f"The backup is damaged at {damaged}.")
    return sorted(member.filename for member in members)


def _check_members(members):
    if not members:
        raise BackupError("This backup is empty.")
    if len(members) > MAX_MEMBERS:
        raise BackupError("This file has too many entries to be a backup.")
    if sum(member.file_size for member in members) > MAX_TOTAL_BYTES:
        raise BackupError("This file is too large to be a backup.")
    seen = set()
    for member in members:
        if not allowed_name(member.filename):
            raise BackupError(f"Unexpected file in backup: {member.filename}")
        if stat.S_ISLNK(member.external_attr >> 16):
            raise BackupError(f"The backup contains a link: {member.filename}")
        if member.filename in seen:
            raise BackupError(f"The backup lists a file twice: {member.filename}")
        seen.add(member.filename)


def _validate_file(path):
    name = path.name
    try:
        if name == "launcher.json":
            if not isinstance(json.loads(path.read_text(encoding="utf-8")), dict):
                raise ValueError("expected settings")
        elif name == "config.ini":
            configparser.RawConfigParser().read_string(path.read_text(encoding="utf-8"))
        else:
            connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
            try:
                result = connection.execute("PRAGMA integrity_check").fetchone()[0]
            finally:
                connection.close()
            if result != "ok":
                raise ValueError(result)
    except (ValueError, UnicodeError, configparser.Error, sqlite3.Error) as exc:
        raise BackupError(f"{name} in this backup is not valid: {exc}") from exc


def _write_safety_copy(targets, folder, clock):
    existing = [path for path in targets if path.is_file()]
    if not existing:
        return None
    stamp = time.strftime("%Y-%m-%d-%H%M%S", time.localtime(clock()))
    destination = Path(folder) / f"before-restore-{stamp}.zip"
    counter = 1
    while destination.exists():
        destination = Path(folder) / f"before-restore-{stamp}-{counter}.zip"
        counter += 1
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in existing:
            archive.write(path, path.name)
    os.chmod(destination, 0o600)
    return destination


def restore_backup(path, destination_for, folder, clock=time.time):
    """Replace only the files in the backup, or change nothing.

    destination_for maps a backup file name to its live path. folder receives
    the safety copy and the temporary working files.
    """
    names = inspect_backup(path)
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    targets = {name: Path(destination_for(name)) for name in names}
    try:
        with tempfile.TemporaryDirectory(prefix=".restore-", dir=folder) as work:
            incoming, previous = Path(work) / "new", Path(work) / "old"
            incoming.mkdir()
            previous.mkdir()
            with zipfile.ZipFile(path) as archive:
                for name in names:
                    with archive.open(name) as source, open(incoming / name, "wb") as output:
                        shutil.copyfileobj(source, output)
            for name in names:
                _validate_file(incoming / name)
                os.chmod(incoming / name, 0o600)
            safety = _write_safety_copy(list(targets.values()), folder, clock)
            _swap(names, targets, incoming, previous)
    except (OSError, zipfile.BadZipFile, EOFError) as exc:
        raise BackupError(f"Restore failed and your current data was kept: {exc}") from exc
    return RestoreResult(names, safety)


def _swap(names, targets, incoming, previous):
    moved, placed = [], []
    try:
        for name in names:
            target = targets[name]
            target.parent.mkdir(parents=True, exist_ok=True)
            # A leftover SQLite journal would be replayed onto the restored file.
            for live in [target, *[Path(f"{target}{suffix}") for suffix in SIDE_FILES]]:
                if live.exists():
                    aside = previous / live.name
                    shutil.move(str(live), str(aside))
                    moved.append((aside, live))
            shutil.move(str(incoming / name), str(target))
            placed.append(target)
    except OSError:
        for target in placed:
            target.unlink(missing_ok=True)
        for aside, live in reversed(moved):
            shutil.move(str(aside), str(live))
        raise
