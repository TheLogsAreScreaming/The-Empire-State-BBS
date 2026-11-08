import logging
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

with mock.patch.dict(sys.modules, {"command_handlers": mock.Mock()}):
    import js8call_integration


CONFIG = """[js8call]
host = 127.0.0.1
port = 2442
db_file = {db_file}
js8groups = @BBS
js8urgent = @URGENT
store_messages = true
"""


class JS8CallMessageStorageTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        config = os.path.join(folder.name, "config.ini")
        with open(config, "w", encoding="utf-8") as handle:
            handle.write(CONFIG.format(db_file=os.path.join(folder.name, "js8.db")))
        patcher = mock.patch.object(js8call_integration, "config_file", config)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.interface = object()
        self.client = js8call_integration.JS8CallClient(
            self.interface, logger=logging.getLogger("js8call-test")
        )
        self.addCleanup(self.client.db_conn.close)

    def receive(self, value):
        self.client.process({"type": "RX.DIRECTED", "value": value})

    def rows(self, table, column):
        return self.client.db_conn.execute(
            f"SELECT sender, {column}, message FROM {table}"
        ).fetchall()

    def test_direct_message_is_stored(self):
        self.receive("K2ABC W2XYZ hello there")
        self.assertEqual(
            self.rows("messages", "receiver"), [("K2ABC", "W2XYZ", "hello there")]
        )

    def test_group_message_is_stored(self):
        self.receive("K2ABC @BBS net tonight")
        self.assertEqual(
            self.rows("groups", "groupname"), [("K2ABC", "@BBS", "net tonight")]
        )

    def test_urgent_message_is_stored_and_announced(self):
        with mock.patch.object(js8call_integration, "send_message") as send:
            self.receive("K2ABC @URGENT road closed")
        self.assertEqual(
            self.rows("urgent", "groupname"), [("K2ABC", "@URGENT", "road closed")]
        )
        send.assert_called_once()
        text, destination, interface = send.call_args.args
        self.assertIn("K2ABC", text)
        self.assertEqual(destination, js8call_integration.BROADCAST_NUM)
        self.assertIs(interface, self.interface)


if __name__ == "__main__":
    unittest.main()
