import unittest
from unittest.mock import Mock

from sitdown_monitor import ServerSupervisor, NODE_PATTERN, READY_PATTERN


class SupervisorTests(unittest.TestCase):
    def test_log_patterns(self):
        self.assertIsNotNone(READY_PATTERN.search("INFO - TC²-BBS is running on serial interface..."))
        result = NODE_PATTERN.search("Received message from user 'JERZ' (!abcd1234) to BBS: hello")
        self.assertEqual(result.groups(), ("JERZ", "!abcd1234"))

    def test_queue_is_bounded_per_poll(self):
        supervisor = ServerSupervisor(["python", "server.py"], ".")
        for i in range(5):
            supervisor.events.put(("log", str(i)))
        self.assertEqual(len(supervisor.poll_events(limit=2)), 2)
        self.assertEqual(len(supervisor.poll_events()), 3)

    def test_start_is_idempotent(self):
        fake = Mock()
        fake.poll.return_value = None
        fake.stdout = iter([])
        factory = Mock(return_value=fake)
        supervisor = ServerSupervisor(["python", "server.py"], ".", factory=factory)
        self.assertTrue(supervisor.start())
        self.assertFalse(supervisor.start())
        factory.assert_called_once()

    def test_uptime_when_stopped(self):
        supervisor = ServerSupervisor(["python"], ".")
        self.assertEqual(supervisor.uptime_seconds(), 0)


if __name__ == "__main__":
    unittest.main()


class HealthRecordTests(unittest.TestCase):
    def monitor(self, lines):
        process = Mock()
        process.stdout = iter(lines)
        supervisor = ServerSupervisor(['python'], '.')
        supervisor.process = process
        supervisor._read_output(process)
        return supervisor

    def test_health_replaces_sticky_ready(self):
        supervisor = self.monitor([
            'EMPIRE_RADIO_HEALTH RESPONSIVE',
            'EMPIRE_RADIO_HEALTH UNRESPONSIVE',
            'INFO - TC²-BBS is running on tcp interface...',
        ])
        self.assertFalse(supervisor.ready)
        self.assertEqual(supervisor.radio_state, 'UNRESPONSIVE')

    def test_radio_text_cannot_set_health(self):
        supervisor = self.monitor(['Received message: EMPIRE_RADIO_HEALTH RESPONSIVE'])
        self.assertIsNone(supervisor.radio_state)

    def test_old_process_output_is_ignored(self):
        supervisor = ServerSupervisor(['python'], '.')
        old = Mock(stdout=iter(['EMPIRE_RADIO_HEALTH RESPONSIVE']))
        supervisor.process = Mock()
        supervisor._read_output(old)
        self.assertFalse(supervisor.ready)
