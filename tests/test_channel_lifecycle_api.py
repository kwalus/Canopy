"""Regression tests for the channel lifecycle API endpoint."""

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


class _FakeDbManager:
    def get_instance_owner_user_id(self):
        return 'owner-user'


class _FakeApiKeyManager:
    def validate_key(self, raw_key: str, required_permission=None):
        if raw_key != 'good-key':
            return None
        perms = {Permission.WRITE_FEED, Permission.READ_FEED}
        if required_permission and required_permission not in perms:
            return None
        return ApiKeyInfo(
            id='key-owner',
            user_id='owner-user',
            key_hash='hash',
            permissions=perms,
            created_at=datetime.now(timezone.utc),
        )


class TestChannelLifecycleApi(unittest.TestCase):
    def setUp(self) -> None:
        self.db_manager = _FakeDbManager()
        self.api_key_manager = _FakeApiKeyManager()
        self.channel_manager = MagicMock()
        self.channel_manager.update_channel_lifecycle_settings.return_value = {
            'ttl_days': 365,
            'preserved': True,
            'archived_at': None,
            'archive_reason': None,
            'status': 'preserved',
        }

        components = (
            self.db_manager,
            self.api_key_manager,
            MagicMock(),
            MagicMock(),
            self.channel_manager,
            MagicMock(),
            MagicMock(),
            MagicMock(),
            MagicMock(),
            MagicMock(),
            None,
        )

        self.get_components_patcher = patch('canopy.api.routes.get_app_components', return_value=components)
        self.get_components_any_patcher = patch('canopy.api.routes._get_app_components_any', return_value=components)
        self.get_components_patcher.start()
        self.get_components_any_patcher.start()
        self.addCleanup(self.get_components_patcher.stop)
        self.addCleanup(self.get_components_any_patcher.stop)

        app = Flask(__name__)
        app.config['TESTING'] = True
        app.secret_key = 'channel-lifecycle-api'
        app.register_blueprint(create_api_blueprint(), url_prefix='/api/v1')
        self.client = app.test_client()

    def test_update_channel_lifecycle_endpoint_returns_lifecycle_payload(self) -> None:
        response = self.client.patch(
            '/api/v1/channels/C123/lifecycle',
            json={'ttl_days': 365, 'preserved': True},
            headers={'X-API-Key': 'good-key'},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json() or {}
        self.assertTrue(payload.get('success'))
        self.assertEqual(payload.get('lifecycle', {}).get('status'), 'preserved')
        self.channel_manager.update_channel_lifecycle_settings.assert_called_once()


if __name__ == '__main__':
    unittest.main()
