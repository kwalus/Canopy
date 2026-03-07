"""Regression tests for routing rate-limit behavior during catch-up sync."""

import os
import sys
import types
import unittest
from unittest.mock import patch

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


class TestRoutingRateLimits(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.router = MessageRouter(
            local_peer_id='peer-local',
            identity_manager=_DummyIdentityManager(),
            connection_manager=_DummyConnectionManager(),
        )

    def _incoming(self, idx: int, msg_type: MessageType, from_peer: str = 'peer-remote') -> P2PMessage:
        return P2PMessage(
            id=f"M{idx:04d}",
            type=msg_type,
            from_peer=from_peer,
            to_peer='peer-local',
            timestamp=1000.0,
            ttl=5,
            payload={'content': '', 'metadata': {}},
            signature='sig',
            encrypted_payload=None,
        )

    async def test_direct_messages_hit_regular_burst_limit(self) -> None:
        with patch('canopy.network.routing.time.time', return_value=1000.0):
            results = []
            for idx in range(1, 13):
                results.append(await self.router.route_message(
                    self._incoming(idx, MessageType.DIRECT_MESSAGE)
                ))

        # First 10 within the same 5s window pass; subsequent ones are limited.
        self.assertEqual(results[:10], [True] * 10)
        self.assertEqual(results[10:], [False, False])

    async def test_catchup_responses_use_separate_sync_limits(self) -> None:
        with patch('canopy.network.routing.time.time', return_value=1000.0):
            results = []
            for idx in range(1, 41):
                results.append(await self.router.route_message(
                    self._incoming(idx, MessageType.CHANNEL_CATCHUP_RESPONSE)
                ))

        # Sync responses should not be clipped by the strict interactive burst limit.
        self.assertTrue(all(results))

    async def test_sync_traffic_does_not_consume_regular_budget(self) -> None:
        with patch('canopy.network.routing.time.time', return_value=1000.0):
            # Heavy sync burst from peer-remote.
            for idx in range(1, 31):
                ok = await self.router.route_message(
                    self._incoming(idx, MessageType.CHANNEL_CATCHUP_RESPONSE)
                )
                self.assertTrue(ok)

            # A direct interactive message immediately after should still pass.
            ok_direct = await self.router.route_message(
                self._incoming(999, MessageType.DIRECT_MESSAGE)
            )

        self.assertTrue(ok_direct)

    async def test_member_sync_uses_sync_budget_during_reconnect_repair(self) -> None:
        with patch('canopy.network.routing.time.time', return_value=1000.0):
            results = []
            for idx in range(1, 41):
                results.append(await self.router.route_message(
                    self._incoming(idx, MessageType.MEMBER_SYNC)
                ))

        self.assertTrue(all(results))


if __name__ == '__main__':
    unittest.main()
