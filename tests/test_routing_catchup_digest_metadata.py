"""Tests for catch-up digest metadata routing compatibility."""

import os
import sys
import time
import types
import unittest

# Ensure repository root is importable when running tests directly.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

# Provide a lightweight zeroconf stub for environments without optional deps.
if 'zeroconf' not in sys.modules:
    zeroconf_stub = types.ModuleType('zeroconf')

    class _Dummy:
        def __init__(self, *args, **kwargs):
            pass

    zeroconf_stub.ServiceBrowser = _Dummy
    zeroconf_stub.ServiceInfo = _Dummy
    zeroconf_stub.Zeroconf = _Dummy
    zeroconf_stub.ServiceStateChange = _Dummy
    sys.modules['zeroconf'] = zeroconf_stub

from canopy.network.routing import MessageRouter, MessageType, P2PMessage


class _DummyIdentityManager:
    class _LocalIdentity:
        @staticmethod
        def sign(_payload: bytes) -> bytes:
            return b'\x01' * 64

    local_identity = _LocalIdentity()

    def get_peer(self, _peer_id):
        return None


class _DummyConnectionManager:
    def __init__(self):
        self.sent = []
        self._connected = {'peer-remote'}

    def get_connected_peers(self):
        return list(self._connected)

    def is_connected(self, peer_id):
        return peer_id in self._connected

    async def send_to_peer(self, peer_id, payload):
        self.sent.append((peer_id, payload))
        return True


class TestRoutingCatchupDigestMetadata(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.conn = _DummyConnectionManager()
        self.router = MessageRouter(
            local_peer_id='peer-local',
            identity_manager=_DummyIdentityManager(),
            connection_manager=self.conn,
        )

    async def test_send_catchup_request_includes_digest_metadata(self) -> None:
        ok = await self.router.send_catchup_request(
            to_peer='peer-remote',
            channel_timestamps={'general': '2026-03-04 10:00:00'},
            digest={
                'version': 1,
                'channels': {
                    'general': {'root': 'abc', 'live_count': 1, 'max_created_at': '2026-03-04 10:00:00'}
                },
            },
        )
        self.assertTrue(ok)
        self.assertEqual(len(self.conn.sent), 1)
        _peer, payload = self.conn.sent[0]
        metadata = payload['message']['payload']['metadata']
        self.assertIn('digest', metadata)
        self.assertEqual(metadata['digest']['version'], 1)

    async def test_send_catchup_request_includes_channel_ranges_metadata(self) -> None:
        ok = await self.router.send_catchup_request(
            to_peer='peer-remote',
            channel_timestamps={'general': '2026-03-04 10:00:00'},
            channel_ranges={
                'general': {
                    'latest': '2026-03-04 10:00:00',
                    'oldest': '2026-03-01 09:00:00',
                    'message_count': 8,
                }
            },
        )
        self.assertTrue(ok)
        _peer, payload = self.conn.sent[-1]
        metadata = payload['message']['payload']['metadata']
        self.assertIn('channel_ranges', metadata)
        self.assertEqual(metadata['channel_ranges']['general']['oldest'], '2026-03-01 09:00:00')

    async def test_callback_receives_digest_when_supported(self) -> None:
        seen = {}

        def _cb(**kwargs):
            seen.update(kwargs)

        self.router.on_catchup_request = _cb
        msg = P2PMessage(
            id='MCATCH001',
            type=MessageType.CHANNEL_CATCHUP_REQUEST,
            from_peer='peer-remote',
            to_peer='peer-local',
            timestamp=time.time(),
            ttl=5,
            payload={
                'content': '',
                'metadata': {
                    'channel_timestamps': {'general': '2026-03-04 10:00:00'},
                    'digest': {
                        'version': 1,
                        'channels': {'general': {'root': 'abc', 'live_count': 1}},
                    },
                },
            },
            signature='sig',
            encrypted_payload=None,
        )

        ok = await self.router.route_message(msg)
        self.assertTrue(ok)
        self.assertIn('digest', seen)
        self.assertEqual(seen['digest']['version'], 1)

    async def test_callback_receives_channel_ranges_when_supported(self) -> None:
        seen = {}

        def _cb(**kwargs):
            seen.update(kwargs)

        self.router.on_catchup_request = _cb
        msg = P2PMessage(
            id='MCATCH003',
            type=MessageType.CHANNEL_CATCHUP_REQUEST,
            from_peer='peer-remote',
            to_peer='peer-local',
            timestamp=time.time(),
            ttl=5,
            payload={
                'content': '',
                'metadata': {
                    'channel_timestamps': {'general': '2026-03-04 10:00:00'},
                    'channel_ranges': {
                        'general': {
                            'latest': '2026-03-04 10:00:00',
                            'oldest': '2026-03-01 09:00:00',
                            'message_count': 8,
                        }
                    },
                },
            },
            signature='sig',
            encrypted_payload=None,
        )

        ok = await self.router.route_message(msg)
        self.assertTrue(ok)
        self.assertIn('channel_ranges', seen)
        self.assertEqual(seen['channel_ranges']['general']['message_count'], 8)

    async def test_callback_without_digest_arg_still_works(self) -> None:
        called = {'value': False}

        def _legacy_cb(channel_timestamps, from_peer,
                       feed_latest=None, circle_entries_latest=None,
                       circle_votes_latest=None, circles_latest=None,
                       tasks_latest=None):
            called['value'] = True

        self.router.on_catchup_request = _legacy_cb
        msg = P2PMessage(
            id='MCATCH002',
            type=MessageType.CHANNEL_CATCHUP_REQUEST,
            from_peer='peer-remote',
            to_peer='peer-local',
            timestamp=time.time(),
            ttl=5,
            payload={
                'content': '',
                'metadata': {
                    'channel_timestamps': {'general': '2026-03-04 10:00:00'},
                    'digest': {'version': 1, 'channels': {}},
                },
            },
            signature='sig',
            encrypted_payload=None,
        )

        ok = await self.router.route_message(msg)
        self.assertTrue(ok)
        self.assertTrue(called['value'])


if __name__ == '__main__':
    unittest.main()
