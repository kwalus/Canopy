"""Regression tests for API peer forgetting cleanup."""

import os
import sys
import types
import unittest
from datetime import datetime, timezone
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

from canopy.api.routes import create_api_blueprint
from canopy.security.api_keys import ApiKeyInfo, Permission


class TestPeerForgetCleanupApi(unittest.TestCase):
    def setUp(self) -> None:
        self.db_manager = MagicMock()
        self.db_manager.get_user.return_value = {
            'id': 'test-user',
            'username': 'test-user',
            'display_name': 'Test User',
            'account_type': 'human',
            'status': 'active',
        }
        self.db_manager.forget_peer_residue.return_value = {
            'success': True,
            'peer_id': 'peer-gone',
            'deleted_shadow_user_ids': ['user-peer-gone'],
            'failed_shadow_user_ids': [],
            'cleanup': {
                'deleted_shadow_users': 1,
                'failed_shadow_users': 0,
                'trust_scores_deleted': 1,
                'delete_signals_deleted': 2,
                'peer_profiles_deleted': 1,
                'mesh_principals_deleted': 0,
                'mesh_grants_deleted': 0,
                'mesh_grant_applications_deleted': 0,
                'mesh_grant_revocations_deleted': 0,
                'remote_attachment_transfers_deleted': 0,
            },
        }

        self.api_key_manager = MagicMock()
        self.api_key_manager.validate_key.return_value = None

        self.p2p_manager = MagicMock()
        self.p2p_manager.connection_manager = MagicMock()
        self.p2p_manager.connection_manager.is_connected.return_value = False
        self.p2p_manager.identity_manager = MagicMock()
        self.p2p_manager.identity_manager.remove_known_peer.return_value = True
        self.p2p_manager.identity_manager.peer_display_names = {}
        self.p2p_manager.identity_manager.peer_endpoints = {}
        self.p2p_manager._introduced_peers = {'peer-gone': {'peer_id': 'peer-gone'}}
        self.p2p_manager._active_relays = {'peer-gone': 'relay-hop', 'dest-peer': 'peer-gone'}
        self.reconnect_task = MagicMock()
        self.p2p_manager._reconnect_tasks = {'peer-gone': self.reconnect_task}
        self.p2p_manager.peer_versions = {'peer-gone': '1.0'}
        self.p2p_manager.message_router = MagicMock()
        self.p2p_manager.message_router.remove_route.return_value = True
        self.p2p_manager.discovery = MagicMock()
        self.p2p_manager.discovery.discovered_peers = {'peer-gone': {'peer_id': 'peer-gone'}}

        components = (
            self.db_manager,
            self.api_key_manager,
            MagicMock(),
            MagicMock(),
            MagicMock(),
            MagicMock(),
            MagicMock(),
            MagicMock(),
            MagicMock(),
            MagicMock(),
            self.p2p_manager,
        )

        self.get_components_patcher = patch(
            'canopy.api.routes.get_app_components',
            return_value=components,
        )
        self.get_components_patcher.start()
        self.addCleanup(self.get_components_patcher.stop)

        app = Flask(__name__)
        app.config['TESTING'] = True
        app.secret_key = 'test-secret'
        app.register_blueprint(create_api_blueprint(), url_prefix='/api/v1')
        self.client = app.test_client()

    def _set_authenticated_session(self, csrf_token: str = 'csrf-peer-forget') -> None:
        with self.client.session_transaction() as sess:
            sess['authenticated'] = True
            sess['user_id'] = 'test-user'
            sess['_csrf_token'] = csrf_token

    def test_forget_peer_cleans_runtime_state_and_reports_residue_cleanup(self) -> None:
        self._set_authenticated_session()

        response = self.client.post(
            '/api/v1/p2p/forget',
            json={
                'peer_id': 'peer-gone',
                'remove_introduced': True,
                'purge_residue': True,
                'remove_shadow_users': True,
            },
            headers={'X-CSRFToken': 'csrf-peer-forget'},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json() or {}
        self.assertEqual(payload.get('status'), 'forgotten')
        self.assertTrue(payload.get('removed_known_peer'))
        self.assertEqual(payload.get('runtime_cleanup', {}).get('introduced_removed'), 1)
        self.assertEqual(payload.get('runtime_cleanup', {}).get('routes_removed'), 1)
        self.assertEqual(payload.get('runtime_cleanup', {}).get('relay_entries_removed'), 2)
        self.assertEqual(payload.get('runtime_cleanup', {}).get('reconnect_tasks_cancelled'), 1)
        self.assertEqual(payload.get('runtime_cleanup', {}).get('discovered_removed'), 1)
        self.assertEqual(payload.get('residue_cleanup', {}).get('cleanup', {}).get('trust_scores_deleted'), 1)
        self.db_manager.forget_peer_residue.assert_called_once_with('peer-gone', remove_shadow_users=True)
        self.p2p_manager.clear_peer_profile_cache.assert_called_once_with('peer-gone')
        self.p2p_manager.identity_manager.remove_known_peer.assert_called_once_with('peer-gone')
        self.assertIsNone(self.p2p_manager._reconnect_tasks.get('peer-gone'))
        self.reconnect_task.cancel.assert_called_once()

    def test_forget_peer_clears_profile_cache_even_without_residue_purge(self) -> None:
        self._set_authenticated_session(csrf_token='csrf-no-purge')

        response = self.client.post(
            '/api/v1/p2p/forget',
            json={
                'peer_id': 'peer-gone',
                'purge_residue': False,
            },
            headers={'X-CSRFToken': 'csrf-no-purge'},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json() or {}
        self.assertEqual(payload.get('status'), 'forgotten')
        self.db_manager.forget_peer_residue.assert_not_called()
        self.p2p_manager.clear_peer_profile_cache.assert_called_once_with('peer-gone')

    def test_forget_peer_requires_delete_data_for_api_keys(self) -> None:
        self.api_key_manager.validate_key.side_effect = lambda raw_key, required_permission=None: (
            ApiKeyInfo(
                id='key-read-only',
                user_id='test-user',
                key_hash='hash',
                permissions={Permission.READ_MESSAGES},
                created_at=datetime.now(timezone.utc),
            )
            if raw_key == 'read-only-key' else None
        )

        response = self.client.post(
            '/api/v1/p2p/forget',
            json={
                'peer_id': 'peer-gone',
                'remove_introduced': True,
                'purge_residue': True,
                'remove_shadow_users': True,
            },
            headers={'X-API-Key': 'read-only-key'},
        )

        self.assertEqual(response.status_code, 403)
        payload = response.get_json() or {}
        self.assertEqual(payload.get('error'), 'Invalid or insufficient permissions')
        self.db_manager.forget_peer_residue.assert_not_called()


if __name__ == '__main__':
    unittest.main()
