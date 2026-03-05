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
        self.p2p_manager._active_relays = {}
        self.p2p_manager.reconnect_known_peers.return_value = True

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
            MagicMock(),               # config
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


if __name__ == '__main__':
    unittest.main()
