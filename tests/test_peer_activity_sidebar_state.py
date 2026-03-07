"""Tests for sidebar peer state data returned by /ajax/peer_activity."""

import os
import sys
import types
import unittest
from unittest.mock import MagicMock, patch

from flask import Flask

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

from canopy.ui.routes import create_ui_blueprint


class TestPeerActivitySidebarState(unittest.TestCase):
    def setUp(self):
        self.conn_a = MagicMock()
        self.conn_a.connected_at = 101.0
        self.conn_a.last_activity = 201.0
        self.conn_a.last_inbound_activity = 202.0
        self.conn_a.last_outbound_activity = 203.0

        self.conn_b = MagicMock()
        self.conn_b.connected_at = 111.0
        self.conn_b.last_activity = 211.0
        self.conn_b.last_inbound_activity = 212.0
        self.conn_b.last_outbound_activity = 213.0

        conn_mgr = MagicMock()
        conn_mgr.get_connected_peers.return_value = ['peer-a', 'peer-b']
        conn_mgr.get_connection.side_effect = lambda peer_id: {
            'peer-a': self.conn_a,
            'peer-b': self.conn_b,
        }.get(peer_id)

        self.trust_manager = MagicMock()
        self.trust_manager.get_trust_score.side_effect = lambda peer_id: {
            'peer-a': 84,
            'peer-b': 61,
        }.get(peer_id)

        self.p2p_manager = MagicMock()
        self.p2p_manager.connection_manager = conn_mgr
        self.p2p_manager.get_activity_events.return_value = [
            {'id': 'evt-1', 'peer_id': 'peer-a', 'kind': 'channel_message', 'timestamp': 300.0}
        ]

        components = (
            MagicMock(),         # db_manager
            MagicMock(),         # api_key_manager
            self.trust_manager,  # trust_manager
            MagicMock(),         # message_manager
            MagicMock(),         # channel_manager
            MagicMock(),         # file_manager
            MagicMock(),         # feed_manager
            MagicMock(),         # interaction_manager
            MagicMock(),         # profile_manager
            MagicMock(),         # config
            self.p2p_manager,    # p2p_manager
        )

        self.patcher = patch('canopy.ui.routes.get_app_components', return_value=components)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

        app = Flask(__name__)
        app.config['TESTING'] = True
        app.secret_key = 'test-secret'
        app.register_blueprint(create_ui_blueprint())

        self.client = app.test_client()
        with self.client.session_transaction() as sess:
            sess['authenticated'] = True
            sess['user_id'] = 'human-owner'

    def test_peer_activity_returns_connected_peer_ids_and_trust_map(self):
        response = self.client.get('/ajax/peer_activity')
        self.assertEqual(response.status_code, 200)

        payload = response.get_json() or {}
        self.assertTrue(payload.get('success'))
        self.assertEqual(payload.get('connected_peer_ids'), ['peer-a', 'peer-b'])
        self.assertEqual((payload.get('peer_trust') or {}).get('peer-a'), 84)
        self.assertEqual((payload.get('peer_trust') or {}).get('peer-b'), 61)
        self.assertIn('peer-a', payload.get('peers') or {})
        self.assertIn('peer-b', payload.get('peers') or {})

    def test_peer_activity_returns_empty_sidebar_state_when_p2p_missing(self):
        components = (
            MagicMock(), MagicMock(), self.trust_manager, MagicMock(), MagicMock(),
            MagicMock(), MagicMock(), MagicMock(), MagicMock(), MagicMock(), None
        )
        with patch('canopy.ui.routes.get_app_components', return_value=components):
            response = self.client.get('/ajax/peer_activity')

        self.assertEqual(response.status_code, 200)
        payload = response.get_json() or {}
        self.assertTrue(payload.get('success'))
        self.assertEqual(payload.get('connected_peer_ids'), [])
        self.assertEqual(payload.get('peer_trust'), {})
        self.assertEqual(payload.get('peers'), {})


if __name__ == '__main__':
    unittest.main()
