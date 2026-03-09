"""Tests for the /ajax/connection_diagnostics endpoint."""

import os
import sys
import types
import unittest
from unittest.mock import MagicMock, patch

from flask import Flask

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

from canopy.ui.routes import create_ui_blueprint


def _make_mock_p2p_manager(
    connected_peers=None,
    active_relays=None,
    relay_policy='broker_only',
    activity_events=None,
):
    """Build a minimal p2p_manager mock for diagnostics tests."""
    if connected_peers is None:
        connected_peers = []
    if active_relays is None:
        active_relays = {}
    if activity_events is None:
        activity_events = []

    im = MagicMock()
    im.peer_display_names = {}
    im.peer_endpoints = {}
    im.known_peers = {}

    conn_mgr = MagicMock()
    conn_mgr.get_connection.return_value = None

    p2p = MagicMock()
    p2p.identity_manager = im
    p2p.connection_manager = conn_mgr
    p2p._active_relays = active_relays
    p2p.get_connected_peers.return_value = connected_peers
    p2p.get_discovered_peers.return_value = []
    p2p.get_peer_endpoint_diagnostics.return_value = []
    p2p.get_activity_events.return_value = activity_events
    p2p.get_relay_status.return_value = {
        'relay_policy': relay_policy,
        'active_relays': active_relays,
        'routing_table': {},
    }
    p2p._reconnect_tasks = {}
    return p2p


def _make_app(p2p_manager, mesh_port=7771):
    """Build a Flask test app with the UI blueprint registered."""
    config = MagicMock()
    config.network.mesh_port = mesh_port

    components = (
        MagicMock(),   # db_manager
        MagicMock(),   # api_key_manager
        MagicMock(),   # trust_manager
        MagicMock(),   # message_manager
        MagicMock(),   # channel_manager
        MagicMock(),   # file_manager
        MagicMock(),   # feed_manager
        MagicMock(),   # interaction_manager
        MagicMock(),   # profile_manager
        config,        # config
        p2p_manager,   # p2p_manager
    )

    patcher = patch('canopy.ui.routes.get_app_components', return_value=components)
    patcher.start()

    app = Flask(__name__)
    app.config['TESTING'] = True
    app.secret_key = 'test-secret'
    app.register_blueprint(create_ui_blueprint())

    return app, patcher


class TestConnectionDiagnosticsEndpoint(unittest.TestCase):
    def setUp(self):
        self.p2p_manager = _make_mock_p2p_manager()
        self.app, self.patcher = _make_app(self.p2p_manager)
        self.addCleanup(self.patcher.stop)
        self.client = self.app.test_client()

    def _authenticate(self, user_id='test-user'):
        with self.client.session_transaction() as sess:
            sess['authenticated'] = True
            sess['user_id'] = user_id

    def test_requires_login(self):
        """Endpoint must redirect unauthenticated requests."""
        response = self.client.get('/ajax/connection_diagnostics')
        self.assertIn(response.status_code, (302, 401))

    def test_returns_success_structure(self):
        """Authenticated request returns success with expected keys."""
        self._authenticate()
        with patch('canopy.network.invite.generate_invite', side_effect=Exception('no-op')):
            response = self.client.get('/ajax/connection_diagnostics')
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertTrue(data.get('success'))
        self.assertIn('peers', data)
        self.assertIn('recent_failures', data)
        self.assertIn('local', data)

    def test_local_section_contains_mesh_port_and_relay_policy(self):
        """local section must include mesh_port and relay_policy."""
        self._authenticate()
        with patch('canopy.network.invite.generate_invite', side_effect=Exception('no-op')):
            response = self.client.get('/ajax/connection_diagnostics')
        data = response.get_json()
        local = data.get('local', {})
        self.assertEqual(local.get('mesh_port'), 7771)
        self.assertEqual(local.get('relay_policy'), 'broker_only')

    def test_direct_peer_is_reported_as_direct(self):
        """A peer not in active_relays is labelled as a direct connection."""
        self.p2p_manager.get_connected_peers.return_value = ['peer-abc']
        self.p2p_manager._active_relays = {}
        self._authenticate()
        with patch('canopy.network.invite.generate_invite', side_effect=Exception('no-op')):
            response = self.client.get('/ajax/connection_diagnostics')
        data = response.get_json()
        peers = data.get('peers', [])
        self.assertEqual(len(peers), 1)
        self.assertEqual(peers[0]['peer_id'], 'peer-abc')
        self.assertEqual(peers[0]['connection_type'], 'direct')
        self.assertIsNone(peers[0]['relay_via'])

    def test_relayed_peer_is_reported_as_relayed(self):
        """A peer in active_relays is labelled as a relayed connection."""
        self.p2p_manager.get_connected_peers.return_value = []
        self.p2p_manager._active_relays = {'peer-abc': 'relay-xyz'}
        self.p2p_manager.identity_manager.peer_display_names = {'relay-xyz': 'Relay Node'}
        self._authenticate()
        with patch('canopy.network.invite.generate_invite', side_effect=Exception('no-op')):
            response = self.client.get('/ajax/connection_diagnostics')
        data = response.get_json()
        peers = data.get('peers', [])
        self.assertEqual(len(peers), 1)
        self.assertEqual(peers[0]['connection_type'], 'relayed')
        self.assertEqual(peers[0]['relay_via'], 'relay-xyz')
        self.assertEqual(peers[0]['relay_via_name'], 'Relay Node')

    def test_connected_peer_wins_over_stale_relay_marker(self):
        """A live connected peer should still be shown as direct."""
        self.p2p_manager.get_connected_peers.return_value = ['peer-abc']
        self.p2p_manager._active_relays = {'peer-abc': 'relay-xyz'}
        self._authenticate()
        with patch('canopy.network.invite.generate_invite', side_effect=Exception('no-op')):
            response = self.client.get('/ajax/connection_diagnostics')
        data = response.get_json()
        peers = data.get('peers', [])
        self.assertEqual(len(peers), 1)
        self.assertEqual(peers[0]['connection_type'], 'direct')

    def test_relay_only_peer_included_in_peers(self):
        """A peer in active_relays but not directly connected appears in the list."""
        self.p2p_manager.get_connected_peers.return_value = []
        self.p2p_manager._active_relays = {'dest-peer': 'relay-xyz'}
        self._authenticate()
        with patch('canopy.network.invite.generate_invite', side_effect=Exception('no-op')):
            response = self.client.get('/ajax/connection_diagnostics')
        data = response.get_json()
        peers = data.get('peers', [])
        self.assertEqual(len(peers), 1)
        self.assertEqual(peers[0]['peer_id'], 'dest-peer')
        self.assertEqual(peers[0]['connection_type'], 'relayed')

    def test_recent_failures_limited_to_five(self):
        """recent_failures contains at most 5 entries."""
        events = [
            {
                'kind': 'connection', 'status': 'failed',
                'peer_id': f'peer-{i}', 'endpoint': f'ws://x:{i}',
                'detail': 'timeout', 'timestamp': 1000.0 + i,
            }
            for i in range(10)
        ]
        self.p2p_manager.get_activity_events.return_value = events
        self._authenticate()
        with patch('canopy.network.invite.generate_invite', side_effect=Exception('no-op')):
            response = self.client.get('/ajax/connection_diagnostics')
        data = response.get_json()
        self.assertLessEqual(len(data.get('recent_failures', [])), 5)

    def test_recent_failures_prefers_most_recent_entries(self):
        """The diagnostics feed should show the newest failures first."""
        events = [
            {
                'kind': 'connection', 'status': 'failed',
                'peer_id': f'peer-{i}', 'endpoint': f'ws://x:{i}',
                'detail': f'failure-{i}', 'timestamp': 1000.0 + i,
            }
            for i in range(10)
        ]
        self.p2p_manager.get_activity_events.return_value = events
        self._authenticate()
        with patch('canopy.network.invite.generate_invite', side_effect=Exception('no-op')):
            response = self.client.get('/ajax/connection_diagnostics')
        data = response.get_json()
        failures = data.get('recent_failures', [])
        self.assertEqual([item['peer_id'] for item in failures[:3]], ['peer-9', 'peer-8', 'peer-7'])

    def test_latency_included_when_available(self):
        """Peer latency is included when the connection object tracks it."""
        self.p2p_manager.get_connected_peers.return_value = ['peer-abc']
        self.p2p_manager._active_relays = {}
        mock_conn = MagicMock()
        mock_conn.last_ping_latency_ms = 42.5
        mock_conn.connected_at = 1000.0
        mock_conn.last_activity = 1001.0
        self.p2p_manager.connection_manager.get_connection.return_value = mock_conn
        self._authenticate()
        with patch('canopy.network.invite.generate_invite', side_effect=Exception('no-op')):
            response = self.client.get('/ajax/connection_diagnostics')
        data = response.get_json()
        peers = data.get('peers', [])
        self.assertEqual(peers[0]['latency_ms'], 42.5)

    def test_no_p2p_manager_returns_503(self):
        """If p2p_manager is None the endpoint returns 503."""
        self.p2p_manager = None
        app, patcher = _make_app(None)
        self.addCleanup(patcher.stop)
        client = app.test_client()
        with client.session_transaction() as sess:
            sess['authenticated'] = True
            sess['user_id'] = 'test-user'
        response = client.get('/ajax/connection_diagnostics')
        self.assertEqual(response.status_code, 503)

    def test_disconnected_known_peer_included_with_endpoint_details(self):
        """Known but disconnected peers should still appear with endpoint diagnostics."""
        self.p2p_manager.identity_manager.known_peers = {'peer-abc': object()}
        self.p2p_manager.identity_manager.peer_endpoints = {
            'peer-abc': ['ws://192.168.1.50:7771']
        }
        self.p2p_manager.get_discovered_peers.return_value = [
            {
                'peer_id': 'peer-abc',
                'address': '192.168.1.50',
                'addresses': ['192.168.1.50'],
                'port': 7771,
                'connected': False,
            }
        ]
        self.p2p_manager.get_peer_endpoint_diagnostics.side_effect = lambda peer_id: (
            [{
                'endpoint': 'ws://192.168.1.50:7771',
                'sources': ['stored', 'discovered'],
                'currently_connected': False,
                'attempt_count': 2,
                'success_count': 0,
                'consecutive_failures': 2,
                'last_attempt_at': 2000.0,
                'last_success_at': None,
                'last_failure_at': 2000.0,
                'last_failure_reason': 'timeout',
                'last_failure_detail': 'Connection timed out',
                'last_status': 'failed',
            }] if peer_id == 'peer-abc' else []
        )
        self._authenticate()
        with patch('canopy.network.invite.generate_invite', side_effect=Exception('no-op')):
            response = self.client.get('/ajax/connection_diagnostics')
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        peers = data.get('peers', [])
        self.assertEqual(len(peers), 1)
        self.assertEqual(peers[0]['peer_id'], 'peer-abc')
        self.assertEqual(peers[0]['connection_type'], 'known')
        self.assertTrue(peers[0]['discovered'])
        self.assertEqual(peers[0]['endpoint_details'][0]['sources'], ['stored', 'discovered'])
        self.assertEqual(peers[0]['last_failure']['reason'], 'timeout')

    def test_completed_reconnect_task_is_not_reported_as_scheduled(self):
        """Finished reconnect tasks should not look active in diagnostics."""
        class _DoneTask:
            def cancelled(self):
                return False

            def done(self):
                return True

        self.p2p_manager.identity_manager.known_peers = {'peer-abc': object()}
        self.p2p_manager._reconnect_tasks = {'peer-abc': _DoneTask()}
        self._authenticate()
        with patch('canopy.network.invite.generate_invite', side_effect=Exception('no-op')):
            response = self.client.get('/ajax/connection_diagnostics')
        data = response.get_json()
        peers = data.get('peers', [])
        self.assertEqual(len(peers), 1)
        self.assertFalse(peers[0]['reconnect_scheduled'])


if __name__ == '__main__':
    unittest.main()
