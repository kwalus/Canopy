"""Focused regressions for safe pre-trust peer identity previews."""

import os
import sys
import types
import unittest
from types import SimpleNamespace
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

from canopy.network.manager import P2PNetworkManager
from canopy.ui.routes import create_ui_blueprint


def _make_manager(*, introduced=None, peer_display_names=None):
    manager = P2PNetworkManager.__new__(P2PNetworkManager)
    manager._introduced_peers = introduced or {}
    manager.identity_manager = SimpleNamespace(
        peer_display_names=peer_display_names or {},
    )
    return manager


class TestPeerPublicIdentityHelper(unittest.TestCase):
    def test_announced_display_name_is_preferred_without_avatar_bytes(self) -> None:
        manager = _make_manager(
            introduced={
                'peer-bob': {
                    'peer_id': 'peer-bob',
                    'display_name': 'Bob Relay',
                    'device_profile': {
                        'avatar_b64': 'SHOULD_NOT_LEAK',
                        'avatar_mime': 'image/png',
                    },
                }
            }
        )

        result = manager.get_peer_public_identity('peer-bob')

        self.assertEqual(result['node_name'], 'Bob Relay')
        self.assertEqual(result['source'], 'announced')
        self.assertEqual(result['avatar_initials'], 'BR')
        self.assertIsNone(result['avatar_b64'])
        self.assertIsNone(result['avatar_mime'])
        self.assertTrue(result['unverified'])

    def test_identity_cache_and_fallback_stay_public_safe(self) -> None:
        manager = _make_manager(peer_display_names={'peer-charlie': 'Charlie Hub'})

        identified = manager.get_peer_public_identity('peer-charlie')
        fallback = manager.get_peer_public_identity('abcdef1234567890')

        self.assertEqual(identified['node_name'], 'Charlie Hub')
        self.assertEqual(identified['source'], 'identity')
        self.assertTrue(identified['avatar_color'].startswith('hsl('))
        self.assertEqual(fallback['node_name'], 'abcdef123456')
        self.assertEqual(fallback['source'], 'fallback')
        self.assertNotIn('bio', identified)
        self.assertNotIn('email', identified)


def _mock_trust_components():
    db = MagicMock()
    db.get_instance_owner_user_id.return_value = 'owner'
    db.get_all_users_for_admin.return_value = []
    db.get_connection.return_value = MagicMock()
    db.list_remote_shadow_duplicate_groups.return_value = []
    db.list_cross_peer_same_name_groups.return_value = []

    trust = MagicMock()
    trust.get_all_trust_scores.return_value = {}
    trust.get_trust_statistics.return_value = {}
    trust.get_pending_delete_signals.return_value = []
    trust.get_trusted_peers.return_value = []

    channel = MagicMock()
    channel.get_all_peer_device_profiles.return_value = {}
    channel.list_channels_for_governance.return_value = []

    p2p = MagicMock()
    p2p.get_connected_peers.return_value = ['peer-unknown-1']
    p2p.get_introduced_peers.return_value = []
    p2p.get_peer_public_identity.return_value = {
        'peer_id': 'peer-unknown-1',
        'node_name': 'Node Unknown',
        'avatar_initials': 'NU',
        'avatar_color': 'hsl(120, 55%, 48%)',
        'avatar_b64': None,
        'avatar_mime': None,
        'source': 'fallback',
        'unverified': True,
    }

    profile = MagicMock()
    profile.get_all_permissions.return_value = []
    profile.get_default_permissions.return_value = []

    return (db, profile, trust, MagicMock(), channel, MagicMock(), MagicMock(), MagicMock(), MagicMock(), MagicMock(), p2p)


def _mock_connect_components():
    db = MagicMock()
    db.get_instance_owner_user_id.return_value = 'owner'

    trust = MagicMock()
    trust.get_trust_score.return_value = 0

    channel = MagicMock()
    channel.get_all_peer_device_profiles.return_value = {}

    identity_manager = SimpleNamespace(
        local_identity=None,
        known_peers={},
        peer_display_names={},
        peer_endpoints={},
    )

    p2p = MagicMock()
    p2p.identity_manager = identity_manager
    p2p.get_connected_peers.return_value = []
    p2p.get_discovered_peers.return_value = []
    p2p.get_introduced_peers.return_value = [
        {'peer_id': 'peer-intro-1', 'endpoints': [], 'introduced_by': 'peer-a'}
    ]
    p2p.get_relay_status.return_value = {}
    p2p.get_peer_public_identity.return_value = {
        'peer_id': 'peer-intro-1',
        'node_name': 'Introduced Node',
        'avatar_initials': 'IN',
        'avatar_color': 'hsl(80, 55%, 48%)',
        'avatar_b64': None,
        'avatar_mime': None,
        'source': 'announced',
        'unverified': True,
    }

    config = SimpleNamespace(network=SimpleNamespace(mesh_port=7771))
    profile = MagicMock()
    profile.get_all_permissions.return_value = []
    profile.get_default_permissions.return_value = []

    return (db, profile, trust, MagicMock(), channel, MagicMock(), MagicMock(), MagicMock(), MagicMock(), config, p2p)


class TestPeerPublicIdentityRouteIntegration(unittest.TestCase):
    def setUp(self) -> None:
        app = Flask(__name__)
        app.config['TESTING'] = True
        app.secret_key = 'test-secret'
        app.register_blueprint(create_ui_blueprint())
        self.client = app.test_client()

        with self.client.session_transaction() as sess:
            sess['authenticated'] = True
            sess['user_id'] = 'owner'
            sess['username'] = 'owner'
            sess['display_name'] = 'Owner'

    def test_trust_page_attaches_public_identity_to_potential_peers(self) -> None:
        with patch('canopy.ui.routes._get_app_components_any', return_value=_mock_trust_components()), \
             patch('canopy.ui.routes.render_template') as render:
            render.side_effect = lambda tpl, **ctx: jsonify({'template': tpl, **ctx})
            response = self.client.get('/trust')

        self.assertEqual(response.status_code, 200)
        payload = response.get_json() or {}
        potential = payload.get('potential_peers') or []
        self.assertEqual(potential[0]['public_identity']['node_name'], 'Node Unknown')
        self.assertIsNone(potential[0]['public_identity']['avatar_b64'])

    def test_connect_page_attaches_public_identity_to_introduced_peers(self) -> None:
        with patch('canopy.ui.routes._get_app_components_any', return_value=_mock_connect_components()), \
             patch('canopy.ui.routes.render_template') as render:
            render.side_effect = lambda tpl, **ctx: jsonify({'template': tpl, **ctx})
            response = self.client.get('/connect')

        self.assertEqual(response.status_code, 200)
        payload = response.get_json() or {}
        introduced = payload.get('introduced_peers') or []
        self.assertEqual(introduced[0]['public_identity']['node_name'], 'Introduced Node')
        self.assertEqual(introduced[0]['public_identity']['source'], 'announced')


if __name__ == '__main__':
    unittest.main()
