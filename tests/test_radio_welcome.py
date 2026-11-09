import configparser
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import empire_welcome
from empire_welcome import ENV_NAME, MAX_BYTES, WelcomeGate, byte_length, clean, fits, launch_environment, trim

_read = configparser.ConfigParser.read
with patch.object(configparser.ConfigParser, "read",
                  lambda self, *_args, **_kwargs: _read(self, ROOT / "example_config.ini")):
    import command_handlers


class WelcomeTextTests(unittest.TestCase):
    def test_clean_makes_one_line(self):
        self.assertEqual(clean("  Hello\n\tnet  night\r\n"), "Hello net night")
        self.assertEqual(clean(None), "")

    def test_limit_counts_bytes_not_characters(self):
        self.assertTrue(fits("a" * MAX_BYTES))
        self.assertFalse(fits("a" * (MAX_BYTES + 1)))
        self.assertEqual(byte_length("☕"), 3)
        self.assertFalse(fits("☕" * 67))

    def test_trim_never_splits_a_character(self):
        trimmed = trim("☕" * 100)
        self.assertEqual(trimmed, "☕" * 66)
        self.assertLessEqual(len(trimmed.encode("utf-8")), MAX_BYTES)

    def test_launch_environment_is_blank_unless_turned_on(self):
        self.assertEqual(launch_environment(False, "Hello"), {ENV_NAME: ""})
        self.assertEqual(launch_environment(True, " Hello\nthere "), {ENV_NAME: "Hello there"})


class WelcomeGateTests(unittest.TestCase):
    def test_each_node_gets_it_once(self):
        gate = WelcomeGate({ENV_NAME: "Hello"})
        self.assertEqual(gate.take(1), "Hello")
        self.assertIsNone(gate.take(1))
        self.assertEqual(gate.take(2), "Hello")

    def test_nothing_when_unset_or_blank(self):
        for environ in ({}, {ENV_NAME: ""}, {ENV_NAME: "  \n "}):
            with self.subTest(environ):
                gate = WelcomeGate(environ)
                self.assertIsNone(gate.take(1))
                self.assertEqual(gate.greeted, set())

    def test_oversized_environment_value_is_trimmed(self):
        self.assertEqual(len(WelcomeGate({ENV_NAME: "a" * 500}).take(1)), MAX_BYTES)

    def test_memory_is_bounded(self):
        gate = WelcomeGate({ENV_NAME: "Hello"})
        with patch.object(empire_welcome, "MAX_REMEMBERED", 2):
            gate.take(1)
            gate.take(2)
            gate.take(3)
        self.assertEqual(gate.greeted, {3})


class MainMenuTests(unittest.TestCase):
    def open_menu(self, gate, sender=7):
        with patch.object(command_handlers, "WELCOMES", gate), \
             patch.object(command_handlers, "send_message") as send, \
             patch.object(command_handlers, "get_mail", return_value=[]), \
             patch.object(command_handlers, "get_node_id_from_num", return_value="!1"), \
             patch.object(command_handlers, "update_user_state"):
            command_handlers.handle_help_command(sender, Mock())
        return [call.args[0] for call in send.call_args_list]

    def test_welcome_comes_first_and_only_once(self):
        gate = WelcomeGate({ENV_NAME: "Hello mesh"})
        first = self.open_menu(gate)
        self.assertEqual(first[0], "Hello mesh")
        self.assertIn("EMPIRE STATE BBS", first[1])
        self.assertEqual(len(first), 2)
        self.assertEqual(len(self.open_menu(gate)), 1)

    def test_turned_off_sends_only_the_menu(self):
        self.assertEqual(len(self.open_menu(WelcomeGate({}))), 1)

    def test_sub_menus_do_not_send_it(self):
        gate = WelcomeGate({ENV_NAME: "Hello mesh"})
        with patch.object(command_handlers, "WELCOMES", gate), \
             patch.object(command_handlers, "send_message") as send, \
             patch.object(command_handlers, "update_user_state"):
            command_handlers.handle_help_command(7, Mock(), "bbs")
        self.assertEqual(send.call_count, 1)
        self.assertEqual(gate.greeted, set())


if __name__ == "__main__":
    unittest.main()
