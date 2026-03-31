"""Regression tests for peer-connection arbitration visibility."""

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

from canopy.network.connection import ConnectionManager, ConnectionState, PeerConnection


class _FakeIdentityManager:
    local_identity = None


class _ClosingWebSocket:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class TestConnectionArbitrationRegressions(unittest.IsolatedAsyncioTestCase):
    async def test_replacement_keeps_peer_visible_during_disconnect(self):
        manager = ConnectionManager(
            local_peer_id='peer-local',
            identity_manager=_FakeIdentityManager(),
        )

        existing = PeerConnection(
            peer_id='peer-remote',
            address='127.0.0.1',
            port=7771,
            state=ConnectionState.HANDSHAKING,
            websocket=_ClosingWebSocket(),
            is_outbound=False,
        )
        candidate = PeerConnection(
            peer_id='peer-remote',
            address='127.0.0.1',
            port=7771,
            state=ConnectionState.AUTHENTICATED,
            websocket=_ClosingWebSocket(),
            is_outbound=True,
        )
        manager.connections['peer-remote'] = existing

        adopted = await manager._adopt_authenticated_connection(candidate)

        self.assertTrue(adopted)
        self.assertEqual(manager.get_connected_peers(), ['peer-remote'])
        self.assertIs(manager.get_connection('peer-remote'), candidate)
        self.assertEqual(candidate.state, ConnectionState.AUTHENTICATED)
        self.assertEqual(existing.state, ConnectionState.DISCONNECTED)
        self.assertTrue(existing.websocket.closed)

    async def test_replacement_does_not_fire_disconnect_callback_for_loser(self):
        manager = ConnectionManager(
            local_peer_id='peer-local',
            identity_manager=_FakeIdentityManager(),
        )
        disconnected: list[str] = []
        manager.on_peer_disconnected = disconnected.append

        existing = PeerConnection(
            peer_id='peer-remote',
            address='127.0.0.1',
            port=7771,
            state=ConnectionState.HANDSHAKING,
            websocket=_ClosingWebSocket(),
            is_outbound=False,
        )
        candidate = PeerConnection(
            peer_id='peer-remote',
            address='127.0.0.1',
            port=7771,
            state=ConnectionState.AUTHENTICATED,
            websocket=_ClosingWebSocket(),
            is_outbound=True,
        )
        manager.connections['peer-remote'] = existing

        adopted = await manager._adopt_authenticated_connection(candidate)

        self.assertTrue(adopted)
        self.assertEqual(disconnected, [])


if __name__ == '__main__':
    unittest.main()
