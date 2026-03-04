"""Tests for the /ajax/p2p/diagnostics endpoint."""

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


def _make_app(p2p_manager):
    config = MagicMock()
    config.network.mesh_port = 7771

    db_manager = MagicMock()
    db_manager.get_instance_owner_user_id.return_value = 'admin-user'

    components = (
        db_manager,    # db_manager
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


class TestP2PDiagnosticsEndpoint(unittest.TestCase):
    def setUp(self):
        self.p2p_manager = MagicMock()
        self.p2p_manager.get_mesh_diagnostics.return_value = {
            'connected_peers': ['peer-a', 'peer-b'],
            'known_peers_count': 5,
            'sync': {
                'queue_depth': 0,
                'digest': {
                    'enabled': True,
                    'require_capability': True,
                    'max_channels_per_request': 200,
                    'stats': {
                        'channels_checked': 10,
                        'channels_matched': 9,
                        'channels_mismatched': 1,
                        'fallbacks': 0,
                        'requests_with_digest': 4,
                        'last_used_at': 1700000000.0,
                    },
                },
            },
        }
        self.app, self.patcher = _make_app(self.p2p_manager)
        self.addCleanup(self.patcher.stop)
        self.client = self.app.test_client()

    def _authenticate(self, user_id='admin-user'):
        with self.client.session_transaction() as sess:
            sess['authenticated'] = True
            sess['user_id'] = user_id

    def test_requires_login(self):
        response = self.client.get('/ajax/p2p/diagnostics')
        self.assertIn(response.status_code, (302, 401))

    def test_returns_mesh_diagnostics_payload(self):
        self._authenticate()
        response = self.client.get('/ajax/p2p/diagnostics')
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertTrue(data.get('success'))
        diagnostics = data.get('diagnostics') or {}
        self.assertEqual(diagnostics.get('known_peers_count'), 5)
        self.assertTrue((diagnostics.get('sync') or {}).get('digest', {}).get('enabled'))
        self.assertEqual(
            ((diagnostics.get('sync') or {}).get('digest') or {}).get('stats', {}).get('channels_checked'),
            10,
        )
        self.p2p_manager.get_mesh_diagnostics.assert_called_once()

    def test_returns_503_when_p2p_manager_missing(self):
        app, patcher = _make_app(None)
        self.addCleanup(patcher.stop)
        client = app.test_client()
        with client.session_transaction() as sess:
            sess['authenticated'] = True
            sess['user_id'] = 'admin-user'
        response = client.get('/ajax/p2p/diagnostics')
        self.assertEqual(response.status_code, 503)
        payload = response.get_json()
        self.assertFalse(payload.get('success'))
        self.assertIn('unavailable', (payload.get('error') or '').lower())


if __name__ == '__main__':
    unittest.main()
