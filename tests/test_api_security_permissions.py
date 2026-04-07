"""Security regression tests for API permission guards.

Covers:
- delete_channel_message must require WRITE_FEED, not READ_FEED.
"""

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


def _make_api_key_manager(permissions):
    """Build a minimal fake ApiKeyManager that grants the given permissions."""

    class _FakeApiKeyManager:
        def validate_key(self, raw_key, required_permission=None):
            if raw_key != 'test-key':
                return None
            if required_permission and required_permission not in permissions:
                return None
            return ApiKeyInfo(
                id='key-test',
                user_id='user-test',
                key_hash='hash',
                permissions=permissions,
                created_at=datetime.now(timezone.utc),
            )

    return _FakeApiKeyManager()


def _make_db_manager():
    db = MagicMock()
    db.get_instance_owner_user_id.return_value = 'owner-user'
    return db


def _build_app(api_key_manager, channel_manager):
    db_manager = _make_db_manager()
    components = (
        db_manager,
        api_key_manager,
        MagicMock(),
        MagicMock(),
        channel_manager,
        MagicMock(),  # file_manager
        MagicMock(),
        MagicMock(),
        MagicMock(),
        MagicMock(),
        None,  # p2p_manager
    )
    get_patcher = patch('canopy.api.routes.get_app_components', return_value=components)
    get_any_patcher = patch('canopy.api.routes._get_app_components_any', return_value=components)
    get_patcher.start()
    get_any_patcher.start()

    app = Flask(__name__)
    app.config['TESTING'] = True
    app.secret_key = 'api-security-test'
    app.register_blueprint(create_api_blueprint(), url_prefix='/api/v1')
    client = app.test_client()
    return client, get_patcher, get_any_patcher


class TestDeleteChannelMessagePermission(unittest.TestCase):
    """delete_channel_message must gate on WRITE_FEED, not READ_FEED."""

    def _make_channel_manager(self, delete_success=True):
        cm = MagicMock()
        cm.delete_message.return_value = delete_success
        return cm

    def test_read_feed_only_key_is_rejected(self) -> None:
        """An API key with only READ_FEED must not be allowed to DELETE a channel message."""
        akm = _make_api_key_manager({Permission.READ_FEED})
        channel_manager = self._make_channel_manager()
        client, p1, p2 = _build_app(akm, channel_manager)
        try:
            response = client.delete(
                '/api/v1/channels/C1/messages/M1',
                headers={'X-API-Key': 'test-key'},
            )
            self.assertEqual(
                response.status_code,
                403,
                "READ_FEED key must be rejected with 403 for DELETE channel message",
            )
            channel_manager.delete_message.assert_not_called()
        finally:
            p1.stop()
            p2.stop()

    def test_write_feed_key_is_accepted(self) -> None:
        """An API key with WRITE_FEED must be allowed to DELETE a channel message."""
        akm = _make_api_key_manager({Permission.READ_FEED, Permission.WRITE_FEED})
        channel_manager = self._make_channel_manager(delete_success=True)
        client, p1, p2 = _build_app(akm, channel_manager)
        try:
            response = client.delete(
                '/api/v1/channels/C1/messages/M1',
                headers={'X-API-Key': 'test-key'},
            )
            # delete_message returns True → 200; the key itself must not be rejected
            self.assertNotEqual(
                response.status_code,
                403,
                "WRITE_FEED key must not be rejected by the permission guard",
            )
        finally:
            p1.stop()
            p2.stop()


if __name__ == '__main__':
    unittest.main()
