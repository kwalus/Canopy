"""Regression tests for API session fallback used by the web UI.

These checks ensure Connect/Settings routes that opt into session auth:
- reject anonymous requests without API keys
- accept authenticated UI sessions on allowed GET routes
- enforce CSRF on allowed state-changing requests
"""

import os
import sys
import unittest
import types
from datetime import datetime, timezone
from urllib.error import HTTPError
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

from canopy.api.routes import create_api_blueprint
from canopy.security.api_keys import ApiKeyInfo, Permission


class _ConnState:
    def __init__(self) -> None:
        self.connected_at = 1000.0
        self.last_activity = 1005.0
        self.last_inbound_activity = 1004.5
        self.last_outbound_activity = 1004.0


class TestApiSessionFallback(unittest.TestCase):
    def setUp(self) -> None:
        self.db_manager = MagicMock()
        self.db_manager.get_user.return_value = {
            'id': 'test-user',
            'username': 'test-user',
            'display_name': 'Test User',
            'account_type': 'agent',
            'status': 'active',
        }

        self.api_key_manager = MagicMock()
        self.api_key_manager.validate_key.return_value = None

        self.p2p_manager = MagicMock()
        self.p2p_manager.get_relay_status.return_value = {
            'relay_policy': 'broker_only',
            'active_relays': {},
            'routing_table': {},
        }
        self.p2p_manager.get_activity_events.return_value = [
            {
                'id': 'evt-force',
                'peer_id': 'peer-alpha',
                'kind': 'connection',
                'timestamp': 2000.0,
                'status': 'forced_failover',
                'detail': 'Direct connect skipped by caller; testing broker/relay path',
            },
            {
                'id': 'evt-broker',
                'peer_id': 'peer-alpha',
                'kind': 'connection',
                'timestamp': 2001.0,
                'status': 'broker',
                'detail': 'Broker request sent',
            },
        ]
        self.p2p_manager.connection_manager = MagicMock()
        self.p2p_manager.connection_manager.get_connected_peers.return_value = ['peer-alpha']
        self.p2p_manager.connection_manager.get_connection.return_value = _ConnState()
        self.p2p_manager.connection_manager.is_connected.return_value = False
        self.p2p_manager.identity_manager = MagicMock()
        self.p2p_manager.identity_manager.peer_display_names = {}
        self.p2p_manager.identity_manager.peer_endpoints = {}
        self.p2p_manager.identity_manager.local_identity = object()
        self.p2p_manager._active_relays = {}
        self.p2p_manager.reconnect_known_peers.return_value = True
        self.config = types.SimpleNamespace(
            meshspace=types.SimpleNamespace(
                enabled=True,
                meshspace_id='family-mesh',
                name='Family Mesh',
            )
        )

        # Order must match get_app_components in canopy.core.utils
        components = (
            self.db_manager,           # db_manager
            self.api_key_manager,     # api_key_manager
            MagicMock(),               # trust_manager
            MagicMock(),               # message_manager
            MagicMock(),               # channel_manager
            MagicMock(),               # file_manager
            MagicMock(),               # feed_manager
            MagicMock(),               # interaction_manager
            MagicMock(),               # profile_manager
            self.config,               # config
            self.p2p_manager,          # p2p_manager
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

        self.app = app
        self.client = app.test_client()

    def _set_authenticated_session(self, csrf_token: str = 'csrf-test-token') -> None:
        with self.client.session_transaction() as sess:
            sess['authenticated'] = True
            sess['user_id'] = 'test-user'
            sess['_csrf_token'] = csrf_token

    def test_anonymous_request_requires_auth(self) -> None:
        response = self.client.get('/api/v1/p2p/relay_status')
        self.assertEqual(response.status_code, 401)
        payload = response.get_json() or {}
        self.assertEqual(payload.get('error'), 'Authentication required')
        self.assertIn('X-API-Key', payload.get('message', ''))

    def test_authenticated_session_can_access_allowed_get_endpoint(self) -> None:
        self._set_authenticated_session()
        response = self.client.get('/api/v1/p2p/relay_status')
        self.assertEqual(response.status_code, 200)
        payload = response.get_json() or {}
        self.assertEqual(payload.get('relay_policy'), 'broker_only')
        self.assertIn('active_relays', payload)

    def test_youtube_title_lookup_returns_404_without_error_log_on_upstream_404(self) -> None:
        self._set_authenticated_session()
        with patch(
            'canopy.api.routes.urlopen',
            side_effect=HTTPError(
                url='https://www.youtube.com/oembed',
                code=404,
                msg='Not Found',
                hdrs=None,
                fp=None,
            ),
        ), patch('canopy.api.routes.logger') as logger:
            response = self.client.get('/api/v1/deck/youtube-title?video_id=fa6R4NUl8BQ')

        self.assertEqual(response.status_code, 404)
        payload = response.get_json() or {}
        self.assertEqual(payload.get('error'), 'YouTube title unavailable')
        logger.error.assert_not_called()
        logger.info.assert_called_once()

    def test_youtube_title_lookup_caches_success_by_video_id(self) -> None:
        self._set_authenticated_session()
        app = self.app
        key = 'fa6R4NUl8BQ'
        with app.app_context():
            routes = app.view_functions['api.deck_youtube_title_api'].__globals__
            routes['_youtube_oembed_cache'].clear()
        response = MagicMock()
        response.read.return_value = b'{"title":"Cached Title","author_name":"Tester","provider_name":"YouTube","thumbnail_url":"https://img.youtube.com/x.jpg"}'
        response.headers = {'Content-Type': 'application/json; charset=utf-8'}
        response.__enter__.return_value = response
        response.__exit__.return_value = None
        with patch('canopy.api.routes.urlopen', return_value=response) as http_get:
            first = self.client.get(f'/api/v1/deck/youtube-title?video_id={key}')
            second = self.client.get(f'/api/v1/deck/youtube-title?video_id={key}')
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual((first.get_json() or {}).get('title'), 'Cached Title')
        self.assertEqual((second.get_json() or {}).get('title'), 'Cached Title')
        self.assertEqual(http_get.call_count, 1)

    def test_youtube_title_lookup_caches_upstream_404_briefly(self) -> None:
        self._set_authenticated_session()
        app = self.app
        key = 'fa6R4NUl8BQ'
        with app.app_context():
            routes = app.view_functions['api.deck_youtube_title_api'].__globals__
            routes['_youtube_oembed_cache'].clear()
        err = HTTPError(
            url='https://www.youtube.com/oembed',
            code=404,
            msg='Not Found',
            hdrs=None,
            fp=None,
        )
        with patch('canopy.api.routes.urlopen', side_effect=err) as http_get:
            first = self.client.get(f'/api/v1/deck/youtube-title?video_id={key}')
            second = self.client.get(f'/api/v1/deck/youtube-title?video_id={key}')
        self.assertEqual(first.status_code, 404)
        self.assertEqual(second.status_code, 404)
        self.assertEqual(http_get.call_count, 1)

    def test_authenticated_session_can_access_p2p_activity_endpoint(self) -> None:
        self._set_authenticated_session()
        response = self.client.get('/api/v1/p2p/activity?kind=connection&limit=20')
        self.assertEqual(response.status_code, 200)
        payload = response.get_json() or {}
        self.assertIn('relay_status', payload)
        self.assertIn('events', payload)
        self.assertIn('peers', payload)
        self.assertEqual(payload.get('relay_status', {}).get('relay_policy'), 'broker_only')
        validation = payload.get('validation') or {}
        self.assertEqual(validation.get('forced_failover_events'), 1)
        self.assertEqual(validation.get('broker_events'), 1)
        self.assertEqual(validation.get('failed_connection_events'), 0)
        self.assertIn('peer-alpha', payload.get('peers') or {})

    def test_authenticated_session_post_requires_csrf(self) -> None:
        self._set_authenticated_session()
        response = self.client.post('/api/v1/p2p/reconnect_all')
        self.assertEqual(response.status_code, 403)

    def test_authenticated_session_post_succeeds_with_csrf(self) -> None:
        csrf_token = 'csrf-pass'
        self._set_authenticated_session(csrf_token=csrf_token)
        response = self.client.post(
            '/api/v1/p2p/reconnect_all',
            headers={'X-CSRFToken': csrf_token},
        )
        self.assertEqual(response.status_code, 202)
        payload = response.get_json() or {}
        self.assertEqual(payload.get('status'), 'scheduled')

    def test_reconnect_reports_relayed_when_peer_is_reachable_via_relay(self) -> None:
        csrf_token = 'csrf-relayed'
        self._set_authenticated_session(csrf_token=csrf_token)
        self.p2p_manager._active_relays = {'peer-beta': 'relay-1'}
        self.p2p_manager.identity_manager.peer_display_names = {'relay-1': 'Relay Node'}

        response = self.client.post(
            '/api/v1/p2p/reconnect',
            json={'peer_id': 'peer-beta'},
            headers={'X-CSRFToken': csrf_token},
        )
        self.assertEqual(response.status_code, 200)
        payload = response.get_json() or {}
        self.assertEqual(payload.get('status'), 'relayed')
        self.assertEqual(payload.get('relay_via'), 'relay-1')
        self.assertEqual(payload.get('relay_via_name'), 'Relay Node')

    def test_reconnect_prefers_public_endpoint_over_stale_lan_history(self) -> None:
        csrf_token = 'csrf-reconnect-order'
        self._set_authenticated_session(csrf_token=csrf_token)
        self.p2p_manager._event_loop = MagicMock()
        self.p2p_manager._event_loop.is_closed.return_value = False
        self.p2p_manager.identity_manager.peer_endpoints = {
            'peer-beta': ['ws://198.51.100.159:7771', 'wss://vps.example.com:443']
        }

        class _ImmediateFuture:
            def __init__(self, result):
                self._result = result

            def result(self, timeout=None):
                return self._result

        with patch(
            'asyncio.run_coroutine_threadsafe',
            side_effect=[_ImmediateFuture(False), _ImmediateFuture(True)],
        ):
            response = self.client.post(
                '/api/v1/p2p/reconnect',
                json={'peer_id': 'peer-beta'},
                headers={'X-CSRFToken': csrf_token},
            )

        self.assertEqual(response.status_code, 200)
        call_args = self.p2p_manager.connection_manager.connect_to_peer.call_args_list
        self.assertEqual(call_args[0].args[1:], ('vps.example.com', 443))
        self.assertEqual(call_args[0].kwargs.get('scheme'), 'wss')
        self.assertEqual(call_args[1].args[1:], ('198.51.100.159', 7771))
        self.assertEqual(call_args[1].kwargs.get('scheme'), 'ws')

    def test_non_admin_session_cannot_confirm_cross_mesh_reconnect(self) -> None:
        csrf_token = 'csrf-cross-mesh-reconnect'
        self._set_authenticated_session(csrf_token=csrf_token)
        self.db_manager.get_instance_owner_user_id.return_value = 'owner-user'
        self.p2p_manager.get_peer_cross_mesh_status.return_value = {
            'requires_manual_confirmation': True,
            'remote_meshspace_name': 'Other Mesh',
        }

        response = self.client.post(
            '/api/v1/p2p/reconnect',
            json={'peer_id': 'peer-beta', 'allow_cross_mesh': True},
            headers={'X-CSRFToken': csrf_token},
        )

        self.assertEqual(response.status_code, 403)
        payload = response.get_json() or {}
        self.assertEqual(payload.get('diagnostic_code'), 'cross_mesh_admin_required')
        self.p2p_manager.mark_peer_cross_mesh_allowed.assert_not_called()

    def test_non_admin_session_cannot_confirm_cross_mesh_invite_import(self) -> None:
        csrf_token = 'csrf-cross-mesh-invite'
        self._set_authenticated_session(csrf_token=csrf_token)
        self.db_manager.get_instance_owner_user_id.return_value = 'owner-user'

        invite = types.SimpleNamespace(
            peer_id='peer-foreign',
            meshspace_id='other-mesh',
            mesh_name='Other Mesh',
            meshspace_fingerprint='ABCD-1234',
        )

        with patch('canopy.network.invite.InviteCode.decode', return_value=invite):
            response = self.client.post(
                '/api/v1/p2p/invite/import',
                json={'invite_code': 'canopy:test', 'allow_cross_mesh': True},
                headers={'X-CSRFToken': csrf_token},
            )

        self.assertEqual(response.status_code, 403)
        payload = response.get_json() or {}
        self.assertEqual(payload.get('diagnostic_code'), 'cross_mesh_admin_required')
        self.p2p_manager.mark_peer_cross_mesh_allowed.assert_not_called()

    def test_admin_can_treat_peer_mesh_as_same_mesh_alias(self) -> None:
        csrf_token = 'csrf-mesh-alias'
        self._set_authenticated_session(csrf_token=csrf_token)
        self.db_manager.get_instance_owner_user_id.return_value = 'test-user'
        self.p2p_manager.mark_peer_meshspace_equivalent.return_value = {
            'peer_id': 'peer-beta',
            'mesh_relationship': 'same_mesh_alias_confirmed',
        }

        response = self.client.post(
            '/api/v1/p2p/mesh_relationship',
            json={'peer_id': 'peer-beta', 'action': 'same_mesh_alias'},
            headers={'X-CSRFToken': csrf_token},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json() or {}
        self.assertEqual(payload.get('status'), 'same_mesh_alias_confirmed')
        self.p2p_manager.mark_peer_meshspace_equivalent.assert_called_once_with('peer-beta')
        self.p2p_manager.mark_peer_cross_mesh_allowed.assert_not_called()

    def test_admin_can_keep_cross_mesh_bridge_without_aliasing(self) -> None:
        csrf_token = 'csrf-mesh-bridge'
        self._set_authenticated_session(csrf_token=csrf_token)
        self.db_manager.get_instance_owner_user_id.return_value = 'test-user'
        self.p2p_manager.get_peer_cross_mesh_status.return_value = {
            'peer_id': 'peer-beta',
            'mesh_relationship': 'cross_mesh_bridge_allowed',
        }

        response = self.client.post(
            '/api/v1/p2p/mesh_relationship',
            json={'peer_id': 'peer-beta', 'action': 'cross_mesh_bridge'},
            headers={'X-CSRFToken': csrf_token},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json() or {}
        self.assertEqual(payload.get('status'), 'cross_mesh_bridge_allowed')
        self.p2p_manager.mark_peer_cross_mesh_allowed.assert_called_once_with('peer-beta', True)
        self.p2p_manager.mark_peer_meshspace_equivalent.assert_not_called()

    def test_non_admin_cannot_resolve_mesh_relationship(self) -> None:
        csrf_token = 'csrf-mesh-review-denied'
        self._set_authenticated_session(csrf_token=csrf_token)
        self.db_manager.get_instance_owner_user_id.return_value = 'owner-user'

        response = self.client.post(
            '/api/v1/p2p/mesh_relationship',
            json={'peer_id': 'peer-beta', 'action': 'same_mesh_alias'},
            headers={'X-CSRFToken': csrf_token},
        )

        self.assertEqual(response.status_code, 403)
        payload = response.get_json() or {}
        self.assertEqual(payload.get('diagnostic_code'), 'mesh_identity_admin_required')
        self.p2p_manager.mark_peer_meshspace_equivalent.assert_not_called()

    def test_authorization_header_parses_lowercase_bearer_scheme(self) -> None:
        key_info = ApiKeyInfo(
            id='key-test',
            user_id='test-user',
            key_hash='hash',
            permissions={
                Permission.READ_MESSAGES,
                Permission.WRITE_MESSAGES,
                Permission.READ_FEED,
                Permission.WRITE_FEED,
                Permission.MANAGE_KEYS,
            },
            created_at=datetime.now(timezone.utc),
        )
        self.api_key_manager.validate_key.return_value = key_info

        response = self.client.get(
            '/api/v1/auth/status',
            headers={'Authorization': 'bearer test-key'},
        )
        self.assertEqual(response.status_code, 200)
        payload = response.get_json() or {}
        self.assertEqual(payload.get('user_id'), 'test-user')

    def test_auth_status_returns_pending_next_step_for_pending_accounts(self) -> None:
        key_info = ApiKeyInfo(
            id='key-test',
            user_id='test-user',
            key_hash='hash',
            permissions={
                Permission.READ_MESSAGES,
                Permission.WRITE_MESSAGES,
            },
            created_at=datetime.now(timezone.utc),
        )
        self.api_key_manager.validate_key.return_value = key_info
        for account_type in ('agent', 'human'):
            self.db_manager.get_user.return_value = {
                'id': 'test-user',
                'username': 'test-user',
                'display_name': 'Test User',
                'account_type': account_type,
                'status': 'pending_approval',
            }
            response = self.client.get(
                '/api/v1/auth/status',
                headers={'Authorization': 'bearer test-key'},
            )
            self.assertEqual(response.status_code, 200)
            payload = response.get_json() or {}
            self.assertEqual(payload.get('status'), 'pending_approval')
            self.assertIn('Poll GET /api/v1/auth/status until status is "active".', payload.get('next_step', ''))
            self.assertNotIn('approved_hint', payload)

    def test_auth_status_returns_type_specific_approved_hint_for_active_accounts(self) -> None:
        key_info = ApiKeyInfo(
            id='key-test',
            user_id='test-user',
            key_hash='hash',
            permissions={
                Permission.READ_MESSAGES,
                Permission.WRITE_MESSAGES,
            },
            created_at=datetime.now(timezone.utc),
        )
        self.api_key_manager.validate_key.return_value = key_info

        self.db_manager.get_user.return_value = {
            'id': 'test-user',
            'username': 'test-user',
            'display_name': 'Test User',
            'account_type': 'agent',
            'status': 'active',
        }
        response = self.client.get(
            '/api/v1/auth/status',
            headers={'Authorization': 'bearer test-key'},
        )
        self.assertEqual(response.status_code, 200)
        payload = response.get_json() or {}
        self.assertIn('#agent-start-here', payload.get('approved_hint', ''))
        self.assertNotIn('next_step', payload)

        self.db_manager.get_user.return_value = {
            'id': 'test-user',
            'username': 'test-user',
            'display_name': 'Test User',
            'account_type': 'human',
            'status': 'active',
        }
        response = self.client.get(
            '/api/v1/auth/status',
            headers={'Authorization': 'bearer test-key'},
        )
        self.assertEqual(response.status_code, 200)
        payload = response.get_json() or {}
        self.assertEqual(payload.get('approved_hint'), 'Your account is active. You can now access the full API.')
        self.assertNotIn('next_step', payload)


if __name__ == '__main__':
    unittest.main()
