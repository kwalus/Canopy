"""Regression tests for channel-key routing callbacks."""

import os
import sys
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
    local_identity = None

    def get_peer(self, _peer_id):
        return None


class _DummyConnectionManager:
    def get_connected_peers(self):
        return []

    def is_connected(self, _peer_id):
        return False

    async def send_to_peer(self, _peer_id, _payload):
        return False


class TestChannelKeyRouting(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.router = MessageRouter(
            local_peer_id='peer-local',
            identity_manager=_DummyIdentityManager(),
            connection_manager=_DummyConnectionManager(),
        )

    async def test_channel_key_distribution_callback(self) -> None:
        seen = {}

        def _cb(**kwargs):
            seen.update(kwargs)

        self.router.on_channel_key_distribution = _cb
        msg = P2PMessage(
            id='MKEYDIST',
            type=MessageType.CHANNEL_KEY_DISTRIBUTION,
            from_peer='peer-remote',
            to_peer='peer-local',
            timestamp=1000.0,
            ttl=5,
            payload={
                'content': '',
                'metadata': {
                    'channel_id': 'C123',
                    'key_id': 'K1',
                    'encrypted_key': 'wrapped',
                    'key_version': 1,
                    'rotated_from': None,
                },
            },
        )

        ok = await self.router._deliver_local(msg)
        self.assertTrue(ok)
        self.assertEqual(seen.get('channel_id'), 'C123')
        self.assertEqual(seen.get('key_id'), 'K1')
        self.assertEqual(seen.get('encrypted_key'), 'wrapped')
        self.assertEqual(seen.get('from_peer'), 'peer-remote')

    async def test_channel_key_request_callback(self) -> None:
        seen = {}

        def _cb(**kwargs):
            seen.update(kwargs)

        self.router.on_channel_key_request = _cb
        msg = P2PMessage(
            id='MKEYREQ',
            type=MessageType.CHANNEL_KEY_REQUEST,
            from_peer='peer-remote',
            to_peer='peer-local',
            timestamp=1000.0,
            ttl=5,
            payload={
                'content': '',
                'metadata': {
                    'channel_id': 'C456',
                    'requesting_peer': 'peer-remote',
                    'reason': 'missing_key',
                    'key_id': 'K42',
                },
            },
        )

        ok = await self.router._deliver_local(msg)
        self.assertTrue(ok)
        self.assertEqual(seen.get('channel_id'), 'C456')
        self.assertEqual(seen.get('requesting_peer'), 'peer-remote')
        self.assertEqual(seen.get('reason'), 'missing_key')
        self.assertEqual(seen.get('key_id'), 'K42')

    async def test_channel_key_ack_callback(self) -> None:
        seen = {}

        def _cb(**kwargs):
            seen.update(kwargs)

        self.router.on_channel_key_ack = _cb
        msg = P2PMessage(
            id='MKEYACK',
            type=MessageType.CHANNEL_KEY_ACK,
            from_peer='peer-remote',
            to_peer='peer-local',
            timestamp=1000.0,
            ttl=5,
            payload={
                'content': '',
                'metadata': {
                    'channel_id': 'C789',
                    'key_id': 'K2',
                    'status': 'ok',
                    'error': None,
                },
            },
        )

        ok = await self.router._deliver_local(msg)
        self.assertTrue(ok)
        self.assertEqual(seen.get('channel_id'), 'C789')
        self.assertEqual(seen.get('key_id'), 'K2')
        self.assertEqual(seen.get('status'), 'ok')
        self.assertEqual(seen.get('from_peer'), 'peer-remote')

    async def test_member_sync_ack_callback(self) -> None:
        seen = {}

        def _cb(**kwargs):
            seen.update(kwargs)

        self.router.on_member_sync_ack = _cb
        msg = P2PMessage(
            id='MMEMACK',
            type=MessageType.MEMBER_SYNC_ACK,
            from_peer='peer-remote',
            to_peer='peer-local',
            timestamp=1000.0,
            ttl=5,
            payload={
                'content': '',
                'metadata': {
                    'sync_id': 'MSabc',
                    'status': 'error',
                    'error': 'unknown_target_user',
                    'channel_id': 'Cprivate',
                    'target_user_id': 'user123',
                    'action': 'add',
                },
            },
        )

        ok = await self.router._deliver_local(msg)
        self.assertTrue(ok)
        self.assertEqual(seen.get('sync_id'), 'MSabc')
        self.assertEqual(seen.get('status'), 'error')
        self.assertEqual(seen.get('error'), 'unknown_target_user')
        self.assertEqual(seen.get('channel_id'), 'Cprivate')
        self.assertEqual(seen.get('target_user_id'), 'user123')
        self.assertEqual(seen.get('action'), 'add')

    async def test_channel_membership_query_callback(self) -> None:
        seen = {}

        def _cb(**kwargs):
            seen.update(kwargs)

        self.router.on_channel_membership_query = _cb
        msg = P2PMessage(
            id='MMEMQ1',
            type=MessageType.CHANNEL_MEMBERSHIP_QUERY,
            from_peer='peer-remote',
            to_peer='peer-local',
            timestamp=1000.0,
            ttl=5,
            payload={
                'content': '',
                'metadata': {
                    'query_id': 'Q123',
                    'local_user_ids': ['user-a', 'user-b'],
                    'limit': 80,
                },
            },
        )

        ok = await self.router._deliver_local(msg)
        self.assertTrue(ok)
        self.assertEqual(seen.get('query_id'), 'Q123')
        self.assertEqual(seen.get('local_user_ids'), ['user-a', 'user-b'])
        self.assertEqual(seen.get('limit'), 80)
        self.assertEqual(seen.get('from_peer'), 'peer-remote')

    async def test_channel_membership_response_callback(self) -> None:
        seen = {}

        def _cb(**kwargs):
            seen.update(kwargs)

        self.router.on_channel_membership_response = _cb
        msg = P2PMessage(
            id='MMEMR1',
            type=MessageType.CHANNEL_MEMBERSHIP_RESPONSE,
            from_peer='peer-remote',
            to_peer='peer-local',
            timestamp=1000.0,
            ttl=5,
            payload={
                'content': '',
                'metadata': {
                    'query_id': 'Q123',
                    'channels': [{'channel_id': 'Cprivate'}],
                    'truncated': True,
                },
            },
        )

        ok = await self.router._deliver_local(msg)
        self.assertTrue(ok)
        self.assertEqual(seen.get('query_id'), 'Q123')
        self.assertEqual(seen.get('channels'), [{'channel_id': 'Cprivate'}])
        self.assertTrue(seen.get('truncated'))
        self.assertEqual(seen.get('from_peer'), 'peer-remote')


if __name__ == '__main__':
    unittest.main()
