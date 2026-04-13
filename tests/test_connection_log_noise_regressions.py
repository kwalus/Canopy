"""Regression tests for expected reconnect failure logging."""

import os
import sys
import types
import unittest
from unittest.mock import AsyncMock, patch

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

from canopy.network.connection import ConnectionManager
from canopy.network.connection import ConnectionState, PeerConnection


class _FakeIdentityManager:
    local_identity = None

    def __init__(self) -> None:
        self.removed: list[tuple[str, str]] = []
        self.recorded: list[tuple[str, str, bool]] = []

    def remove_endpoint(self, peer_id: str, endpoint: str) -> None:
        self.removed.append((peer_id, endpoint))

    def record_endpoint(self, peer_id: str, endpoint: str, *, claim: bool = True) -> None:
        self.recorded.append((peer_id, endpoint, claim))


class TestConnectionLogNoiseRegressions(unittest.IsolatedAsyncioTestCase):
    async def test_expected_connect_refusal_logs_warning_without_traceback(self):
        manager = ConnectionManager(
            local_peer_id='peer-local',
            identity_manager=_FakeIdentityManager(),
        )
        manager._disconnect_connection = AsyncMock()

        error = ConnectionRefusedError(61, "Connect call failed ('127.0.0.1', 7771)")

        with patch('canopy.network.connection.websockets.connect', side_effect=error), patch(
            'canopy.network.connection.logger'
        ) as logger:
            ok = await manager.connect_to_peer('peer-remote', '127.0.0.1', 7771)

        self.assertFalse(ok)
        logger.warning.assert_called_once()
        logger.error.assert_not_called()

    async def test_connection_arbitration_prefers_stable_outbound_winner_during_dual_handshake(self):
        manager = ConnectionManager(
            local_peer_id='peer-a',
            identity_manager=_FakeIdentityManager(),
        )
        existing = PeerConnection(
            peer_id='peer-z',
            address='127.0.0.1',
            port=7771,
            state=ConnectionState.HANDSHAKING,
            is_outbound=True,
        )
        candidate = PeerConnection(
            peer_id='peer-z',
            address='127.0.0.1',
            port=7771,
            state=ConnectionState.AUTHENTICATED,
            is_outbound=False,
        )

        self.assertFalse(manager._should_replace_existing_connection(existing, candidate))

    async def test_connection_arbitration_prefers_stable_inbound_winner_during_dual_handshake(self):
        manager = ConnectionManager(
            local_peer_id='peer-z',
            identity_manager=_FakeIdentityManager(),
        )
        existing = PeerConnection(
            peer_id='peer-a',
            address='127.0.0.1',
            port=7771,
            state=ConnectionState.HANDSHAKING,
            is_outbound=True,
        )
        candidate = PeerConnection(
            peer_id='peer-a',
            address='127.0.0.1',
            port=7771,
            state=ConnectionState.AUTHENTICATED,
            is_outbound=False,
        )

        self.assertTrue(manager._should_replace_existing_connection(existing, candidate))

    async def test_handshake_peerid_mismatch_quarantines_stale_endpoint_without_adopting_actual_peer(self):
        identity_manager = _FakeIdentityManager()
        manager = ConnectionManager(
            local_peer_id='peer-local',
            identity_manager=identity_manager,
        )

        manager._record_handshake_peerid_mismatch(
            expected_peer_id='peer-expected',
            actual_peer_id='peer-actual',
            endpoint_uri='ws://198.51.100.50:7771/p2p',
        )

        self.assertEqual(
            identity_manager.removed,
            [('peer-expected', 'ws://198.51.100.50:7771')],
        )
        self.assertEqual(identity_manager.recorded, [])


if __name__ == '__main__':
    unittest.main()
