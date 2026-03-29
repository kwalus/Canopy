"""Regression tests for Trust and Admin page identity context."""

import os
import sys
import types
import unittest
from unittest.mock import MagicMock, patch

from flask import Flask, jsonify

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


def _mock_components():
    """Return a tuple of MagicMock objects matching _get_app_components_any."""
    db = MagicMock()
    db.get_instance_owner_user_id.return_value = 'user-123'
    db.get_all_users_for_admin.return_value = []
    db.get_connection.return_value = MagicMock()

    trust = MagicMock()
    trust.get_all_trust_scores.return_value = {}
    trust.get_trust_statistics.return_value = {}
    trust.get_pending_delete_signals.return_value = []
    trust.get_trusted_peers.return_value = []

    channel = MagicMock()
    channel.get_all_peer_device_profiles.return_value = {}
    channel.list_channels_for_governance.return_value = []

    p2p = MagicMock()
    p2p.get_connected_peers.return_value = []
    p2p.get_introduced_peers.return_value = []
    p2p.get_peer_id.return_value = 'local-peer'

    profile = MagicMock()
    signal = MagicMock()
    skill = MagicMock()
    feed = MagicMock()
    message = MagicMock()
    mention = MagicMock()

    return (db, profile, trust, signal, channel, skill, feed, message, mention, MagicMock(), p2p)


class TestTrustPageIdentityContext(unittest.TestCase):
    def setUp(self) -> None:
        app = Flask(__name__)
        app.config['TESTING'] = True
        app.secret_key = 'test-secret'
        app.register_blueprint(create_ui_blueprint())
        self.client = app.test_client()

    def test_trust_page_passes_user_id(self) -> None:
        with self.client.session_transaction() as sess:
            sess['authenticated'] = True
            sess['user_id'] = 'user-trust-42'
            sess['username'] = 'trustuser'
            sess['display_name'] = 'Trust User'

        with patch('canopy.ui.routes._get_app_components_any', return_value=_mock_components()), \
             patch('canopy.ui.routes.render_template') as rt:
            rt.side_effect = lambda tpl, **ctx: jsonify({'template': tpl, **ctx})
            response = self.client.get('/trust')

        self.assertEqual(response.status_code, 200)
        payload = response.get_json() or {}
        self.assertEqual(payload.get('template'), 'trust.html')
        self.assertEqual(payload.get('user_id'), 'user-trust-42')


class TestAdminPageIdentityContext(unittest.TestCase):
    def setUp(self) -> None:
        app = Flask(__name__)
        app.config['TESTING'] = True
        app.secret_key = 'test-secret'
        app.register_blueprint(create_ui_blueprint())
        self.client = app.test_client()

    def test_admin_page_passes_user_id(self) -> None:
        with self.client.session_transaction() as sess:
            sess['authenticated'] = True
            sess['user_id'] = 'user-admin-99'
            sess['username'] = 'adminuser'
            sess['display_name'] = 'Admin User'

        comps = _mock_components()
        db = comps[0]
        db.get_instance_owner_user_id.return_value = 'user-admin-99'

        with patch('canopy.ui.routes._get_app_components_any', return_value=comps), \
             patch('canopy.ui.routes.get_app_components', return_value=comps), \
             patch('canopy.ui.routes.render_template') as rt:
            rt.side_effect = lambda tpl, **ctx: jsonify({'template': tpl, **ctx})
            response = self.client.get('/admin')

        self.assertEqual(response.status_code, 200)
        payload = response.get_json() or {}
        self.assertEqual(payload.get('template'), 'admin.html')
        self.assertEqual(payload.get('user_id'), 'user-admin-99')
