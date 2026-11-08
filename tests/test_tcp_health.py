import socket
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from meshtastic.protobuf import admin_pb2
from tcp_health import ManagedTCPInterface, ProtocolWatchdog


class WatchdogTests(unittest.TestCase):
    def setUp(self):
        self.now = 0
        self.states = []
        self.interface = Mock()
        self.interface.transport_failed = threading.Event()
        self.interface.isConnected = threading.Event()
        self.interface.isConnected.set()
        self.interface.localNode.nodeNum = 123
        self.interface.responseHandlers = {}
        self.interface.sendData.return_value = SimpleNamespace(id=42)
        self.health = ProtocolWatchdog(self.interface, self.states.append, lambda: self.now)

    def reply(self, **overrides):
        message = admin_pb2.AdminMessage()
        message.get_device_metadata_response.firmware_version = 'test'
        packet = {'from': 123, 'decoded': {'portnum': 'ADMIN_APP', 'payload': message.SerializeToString()}}
        packet.update(overrides)
        self.interface.sendData.call_args.kwargs['onResponse'](packet)

    def test_valid_local_protocol_reply_and_idle_mesh(self):
        self.assertTrue(self.health.tick())
        self.assertEqual(self.interface.sendData.call_args.kwargs['destinationId'], 123)
        self.assertEqual(self.health.state, 'CHECKING')
        self.reply()
        self.health.tick()
        self.assertEqual(self.health.state, 'RESPONSIVE')
        self.now = 29
        self.assertTrue(self.health.tick())
        self.assertEqual(self.interface.sendData.call_count, 1)

    def test_blackhole_warns_then_exits_after_two_misses(self):
        self.health.tick()
        self.now = 15
        self.assertTrue(self.health.tick())
        self.assertEqual(self.health.state, 'UNRESPONSIVE')
        self.now = 45
        self.health.tick()
        self.now = 60
        self.assertFalse(self.health.tick())
        self.assertEqual(self.interface.sendData.call_count, 2)

    def test_ack_remote_and_malformed_reply_do_not_prove_health(self):
        self.health.tick()
        self.reply(**{'from': 456})
        self.reply(decoded={'portnum': 'ROUTING_APP', 'routing': {}})
        self.reply(decoded={'portnum': 'ADMIN_APP', 'payload': b'\xff'})
        self.now = 15
        self.health.tick()
        self.assertEqual(self.health.state, 'UNRESPONSIVE')

    def test_late_reply_ignored_and_next_valid_reply_recovers(self):
        self.health.tick()
        self.now = 15
        self.health.tick()
        self.reply()
        self.assertFalse(self.health.reply.is_set())
        self.now = 45
        self.health.tick()
        self.reply()
        self.health.tick()
        self.assertEqual(self.health.state, 'RESPONSIVE')
        self.assertEqual(self.health.misses, 0)

    def test_disconnect_exits_without_another_request(self):
        self.interface.transport_failed.set()
        self.assertFalse(self.health.tick())
        self.assertEqual(self.health.state, 'DISCONNECTED')
        self.interface.sendData.assert_not_called()

    def test_send_failure_and_cleanup(self):
        self.interface.sendData.side_effect = OSError('gone')
        self.assertFalse(self.health.tick())
        self.assertEqual(self.health.state, 'UNRESPONSIVE')
        self.health.close()
        self.assertIsNone(self.health.deadline)

    def test_pending_handler_removed_on_timeout(self):
        self.health.tick()
        self.interface.responseHandlers[42] = object()
        self.now = 15
        self.health.tick()
        self.assertEqual(self.interface.responseHandlers, {})


class AdapterTests(unittest.TestCase):
    def adapter(self):
        interface = ManagedTCPInterface.__new__(ManagedTCPInterface)
        interface.transport_failed = threading.Event()
        interface._wantExit = False
        interface._send_deadline = threading.local()
        interface._write_lock = threading.Lock()
        interface.socket = Mock()
        return interface

    def test_eof_disables_library_reconnect(self):
        interface = self.adapter()
        interface.socket.recv.return_value = b''
        with patch('tcp_health.socket.create_connection') as dial:
            interface._readBytes(1)
        dial.assert_not_called()
        self.assertTrue(interface.transport_failed.is_set())
        self.assertTrue(interface._wantExit)

    def test_idle_socket_is_not_disconnect(self):
        interface = self.adapter()
        interface.socket.recv.side_effect = socket.timeout()
        self.assertIsNone(interface._readBytes(1))
        self.assertFalse(interface.transport_failed.is_set())

    def test_send_error_does_not_reconnect(self):
        interface = self.adapter()
        interface.socket.sendall.side_effect = OSError('gone')
        with patch('tcp_health.socket.create_connection') as dial:
            with self.assertRaises(OSError):
                interface._writeBytes(b'frame')
        dial.assert_not_called()
        self.assertTrue(interface.transport_failed.is_set())

    def test_queue_wait_is_bounded(self):
        interface = self.adapter()
        interface._send_deadline.at = 1
        with patch('tcp_health.time.monotonic', return_value=2):
            with self.assertRaises(TimeoutError):
                interface._queueHasFreeSpace()
