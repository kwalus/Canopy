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
    profile.get_all_permissions.return_value = []
    profile.get_default_permissions.return_value = []
    signal = MagicMock()
    skill = MagicMock()
    feed = MagicMock()
    message = MagicMock()
    mention = MagicMock()

    db.list_remote_shadow_duplicate_groups.return_value = []
    db.list_cross_peer_same_name_groups.return_value = []

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
        self.assertFalse(bool(payload.get('is_admin')))

    def test_pending_peer_cards_expose_decision_summary_without_duplicate_noise(self) -> None:
        with self.client.session_transaction() as sess:
            sess['authenticated'] = True
            sess['user_id'] = 'user-trust-42'
            sess['username'] = 'trustuser'
            sess['display_name'] = 'Trust User'

        comps = _mock_components()
        p2p = comps[10]
        p2p.identity_manager = types.SimpleNamespace(
            known_peers={},
            peer_display_names={},
            peer_endpoints={},
        )
        p2p.get_connected_peers.return_value = ['peer-pending-1234567890']
        p2p.get_introduced_peers.return_value = []
        p2p.get_peer_public_identity.return_value = {
            'node_name': 'peer-pending',
            'source': 'fallback',
            'unverified': True,
        }
        p2p.get_peer_endpoint_diagnostics.return_value = [{
            'endpoint': 'ws://192.168.1.104:61321',
            'sources': ['handshake'],
            'currently_connected': True,
            'last_failure_reason': '',
        }]
        p2p.get_peer_sync_status.return_value = {
            'preview_only': True,
            'effective_scope': '',
            'remote_meshspace_name': 'canopy-mesh',
        }
        p2p.get_peer_cross_mesh_status.return_value = {
            'requires_manual_confirmation': False,
            'mesh_relationship': 'same_mesh_confirmed',
        }

        with patch('canopy.ui.routes._get_app_components_any', return_value=comps), \
             patch('canopy.ui.routes.render_template') as rt:
            rt.side_effect = lambda tpl, **ctx: jsonify({'template': tpl, **ctx})
            response = self.client.get('/trust')

        self.assertEqual(response.status_code, 200)
        payload = response.get_json() or {}
        peers = payload.get('potential_peers') or []
        self.assertEqual(len(peers), 1)
        peer = peers[0]
        summary = peer.get('review_summary') or []
        self.assertEqual([item.get('label') for item in summary], ['Connection', 'Review gate', 'Identity'])
        self.assertEqual(summary[0].get('value'), 'Connected')
        self.assertEqual(summary[1].get('value'), 'Sync approval required')
        self.assertEqual(summary[2].get('value'), 'Only peer ID available')
        self.assertNotIn('Role unknown', peer.get('attention_flags') or [])
        self.assertNotIn('Awaiting sync approval', peer.get('attention_flags') or [])
        self.assertEqual((payload.get('trust_overview') or {}).get('attention_count'), 1)

    def test_mesh_review_pending_peer_counts_toward_attention_metric(self) -> None:
        with self.client.session_transaction() as sess:
            sess['authenticated'] = True
            sess['user_id'] = 'user-trust-42'
            sess['username'] = 'trustuser'
            sess['display_name'] = 'Trust User'

        comps = _mock_components()
        p2p = comps[10]
        p2p.identity_manager = types.SimpleNamespace(
            known_peers={},
            peer_display_names={},
            peer_endpoints={'peer-mesh-review-123': ['ws://192.168.1.5:7771']},
        )
        p2p.get_connected_peers.return_value = ['peer-mesh-review-123']
        p2p.get_introduced_peers.return_value = []
        p2p.get_peer_public_identity.return_value = {
            'node_name': 'Windy Laptop',
            'source': 'handshake',
            'unverified': True,
        }
        p2p.get_peer_endpoint_diagnostics.return_value = [{
            'endpoint': 'ws://192.168.1.5:7771',
            'sources': ['handshake'],
            'currently_connected': True,
            'last_failure_reason': '',
        }]
        p2p.get_peer_sync_status.return_value = {
            'preview_only': False,
            'effective_scope': '',
            'remote_meshspace_name': 'gold-gang',
        }
        p2p.get_peer_cross_mesh_status.return_value = {
            'requires_manual_confirmation': True,
            'mesh_relationship': 'mesh_mismatch_pending_review',
            'cross_mesh_allowed': False,
        }

        with patch('canopy.ui.routes._get_app_components_any', return_value=comps), \
             patch('canopy.ui.routes.render_template') as rt:
            rt.side_effect = lambda tpl, **ctx: jsonify({'template': tpl, **ctx})
            response = self.client.get('/trust')

        self.assertEqual(response.status_code, 200)
        payload = response.get_json() or {}
        self.assertEqual((payload.get('trust_overview') or {}).get('attention_count'), 1)
        attention = payload.get('attention_peers') or []
        self.assertEqual(len(attention), 1)
        self.assertEqual(attention[0].get('review_gate_label'), 'Mesh review required')

    def test_preview_only_pending_peer_keeps_refresh_profile_available(self) -> None:
        with self.client.session_transaction() as sess:
            sess['authenticated'] = True
            sess['user_id'] = 'user-trust-42'
            sess['username'] = 'trustuser'
            sess['display_name'] = 'Trust User'

        comps = _mock_components()
        p2p = comps[10]
        p2p.identity_manager = types.SimpleNamespace(
            known_peers={},
            peer_display_names={},
            peer_endpoints={'peer-preview-1': ['ws://192.168.1.9:7771']},
        )
        p2p.get_connected_peers.return_value = ['peer-preview-1']
        p2p.get_introduced_peers.return_value = []
        p2p.get_peer_public_identity.return_value = {
            'node_name': 'Preview Peer',
            'source': 'handshake',
            'unverified': True,
        }
        p2p.get_peer_endpoint_diagnostics.return_value = [{
            'endpoint': 'ws://192.168.1.9:7771',
            'sources': ['handshake'],
            'currently_connected': True,
            'last_failure_reason': '',
        }]
        p2p.get_peer_sync_status.return_value = {
            'preview_only': True,
            'effective_scope': '',
            'remote_meshspace_name': 'Preview Mesh',
        }
        p2p.get_peer_cross_mesh_status.return_value = {
            'requires_manual_confirmation': False,
            'mesh_relationship': 'same_mesh_confirmed',
        }
        p2p.clear_peer_profile_cache = MagicMock()
        p2p.recover_peer_profile_state = MagicMock()

        with patch('canopy.ui.routes._get_app_components_any', return_value=comps), \
             patch('canopy.ui.routes.render_template') as rt:
            rt.side_effect = lambda tpl, **ctx: jsonify({'template': tpl, **ctx})
            response = self.client.get('/trust')

        self.assertEqual(response.status_code, 200)
        payload = response.get_json() or {}
        peers = payload.get('potential_peers') or []
        self.assertEqual(len(peers), 1)
        self.assertTrue(bool(peers[0].get('can_refresh_profile')))
        self.assertTrue(bool(peers[0].get('requires_review_attention')))


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
