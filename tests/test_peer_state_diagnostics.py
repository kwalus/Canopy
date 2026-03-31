"""Regression tests for peer-state diagnostics helpers."""

import os
import sys
import types
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

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
from canopy.network.manager import P2PNetworkManager


class _FakeIdentityManager:
    local_identity = None

    def __init__(self) -> None:
        self.known_peers = {}


class TestPeerStateDiagnostics(unittest.IsolatedAsyncioTestCase):
    async def test_connection_manager_reports_state_counts_and_pending_handshakes(self):
        manager = ConnectionManager(
            local_peer_id='peer-local',
            identity_manager=_FakeIdentityManager(),
        )
        manager.connections = {
            'peer-auth': PeerConnection(
                peer_id='peer-auth',
                address='127.0.0.1',
                port=7771,
                state=ConnectionState.AUTHENTICATED,
            ),
            'peer-pending': PeerConnection(
                peer_id='peer-pending',
                address='127.0.0.1',
                port=7771,
                state=ConnectionState.HANDSHAKING,
            ),
            'peer-connecting': PeerConnection(
                peer_id='peer-connecting',
                address='127.0.0.1',
                port=7771,
                state=ConnectionState.CONNECTING,
            ),
        }

        counts = manager.get_connection_state_counts()

        self.assertEqual(counts.get('authenticated'), 1)
        self.assertEqual(counts.get('handshaking'), 1)
        self.assertEqual(counts.get('connecting'), 1)
        self.assertEqual(manager.get_pending_handshake_peer_ids(), ['peer-pending'])

    async def test_mesh_diagnostics_include_state_counts_and_recent_transitions(self):
        manager = object.__new__(P2PNetworkManager)
        manager.connection_manager = MagicMock()
        manager.connection_manager.get_connection_state_counts.return_value = {
            'authenticated': 2,
            'handshaking': 1,
            'connecting': 3,
        }
        manager.connection_manager.get_pending_handshake_peer_ids.return_value = ['peer-h1']
        manager.identity_manager = _FakeIdentityManager()
        manager.identity_manager.known_peers = {'a': {}, 'b': {}}
        manager._sync_queue = MagicMock()
        manager._sync_queue.qsize.return_value = 4
        manager._active_catchups = {'peer-a'}
        manager.sync_digest_enabled = True
        manager.sync_digest_require_capability = False
        manager.sync_digest_max_channels_per_request = 200
        manager._sync_digest_stats = {'channels_checked': 3}
        manager.allow_unverified_relay_messages = False
        manager.message_router = None
        manager.get_connected_peers = MagicMock(return_value=['peer-a', 'peer-b'])
        manager.get_activity_events = MagicMock(return_value=[
            {'kind': 'connection', 'peer_id': 'peer-a', 'status': 'attempt', 'timestamp': 1.0, 'endpoint': 'ws://a'},
            {'kind': 'message', 'peer_id': 'peer-a', 'status': 'delivered', 'timestamp': 2.0, 'endpoint': 'ws://a'},
            {'kind': 'connection', 'peer_id': 'peer-b', 'status': 'connected', 'timestamp': 3.0, 'endpoint': 'ws://b', 'detail': 'ignored'},
        ])
        manager._pending_acks = {}
        manager._reconnect_tasks = {}
        manager._in_startup_grace_period = MagicMock(return_value=False)

        diagnostics = P2PNetworkManager.get_mesh_diagnostics(manager)

        self.assertEqual(diagnostics.get('authenticated_count'), 2)
        self.assertEqual(diagnostics.get('pending_connection_count'), 4)
        self.assertEqual(diagnostics.get('pending_handshake_candidates'), ['peer-h1'])
        self.assertEqual((diagnostics.get('connection_state_counts') or {}).get('connecting'), 3)
        transitions = diagnostics.get('recent_peer_state_transitions') or []
        self.assertEqual(len(transitions), 2)
        self.assertEqual(transitions[0], {
            'peer_id': 'peer-a',
            'status': 'attempt',
            'timestamp': 1.0,
            'endpoint': 'ws://a',
        })
        self.assertNotIn('detail', transitions[0])


if __name__ == '__main__':
    unittest.main()
