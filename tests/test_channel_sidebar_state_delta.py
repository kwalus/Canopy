"""Regression tests for /ajax/channel_sidebar_state delta payloads."""

import os
import sys
import types
import unittest
from types import SimpleNamespace
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


class TestChannelSidebarStateDelta(unittest.TestCase):
    def setUp(self) -> None:
        self.channel_manager = MagicMock()
        self.channel_manager.get_user_channels.return_value = [
            SimpleNamespace(
                id='general',
                name='general',
                description='General discussion',
                channel_type='public',
                privacy_mode='open',
                origin_peer='',
                user_role='owner',
                member_count=4,
                unread_count=2,
                notifications_enabled=True,
                crypto_mode='',
            ),
            SimpleNamespace(
                id='ops',
                name='ops',
                description='Operations',
                channel_type='private',
                privacy_mode='guarded',
                origin_peer='peer-1',
                user_role='member',
                member_count=3,
                unread_count=0,
                notifications_enabled=False,
                crypto_mode='channel_e2e_v1',
            ),
        ]

        components = (
            MagicMock(),
            MagicMock(),
            MagicMock(),
            MagicMock(),
            self.channel_manager,
            MagicMock(),
            MagicMock(),
            MagicMock(),
            MagicMock(),
            MagicMock(),
            MagicMock(),
        )

        self.get_components_patcher = patch('canopy.ui.routes.get_app_components', return_value=components)
        self.get_components_any_patcher = patch('canopy.ui.routes._get_app_components_any', return_value=components)
        self.get_components_patcher.start()
        self.get_components_any_patcher.start()
        self.addCleanup(self.get_components_patcher.stop)
        self.addCleanup(self.get_components_any_patcher.stop)

        app = Flask(__name__)
        app.config['TESTING'] = True
        app.secret_key = 'channel-sidebar-secret'
        app.register_blueprint(create_ui_blueprint())

        self.client = app.test_client()
        with self.client.session_transaction() as sess:
            sess['authenticated'] = True
            sess['user_id'] = 'owner'

    def test_channel_sidebar_state_returns_revision(self) -> None:
        response = self.client.get('/ajax/channel_sidebar_state')
        self.assertEqual(response.status_code, 200)
        payload = response.get_json() or {}

        self.assertTrue(payload.get('success'))
        self.assertTrue(payload.get('changed'))
        self.assertEqual(payload.get('count'), 2)
        self.assertTrue(payload.get('rev'))
        channels = payload.get('channels') or []
        self.assertEqual(channels[0].get('id'), 'general')
        self.assertEqual(channels[1].get('crypto_mode'), 'channel_e2e_v1')

    def test_channel_sidebar_state_returns_empty_channels_when_revision_matches(self) -> None:
        first = self.client.get('/ajax/channel_sidebar_state')
        self.assertEqual(first.status_code, 200)
        first_payload = first.get_json() or {}

        response = self.client.get(f"/ajax/channel_sidebar_state?rev={first_payload.get('rev')}")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json() or {}

        self.assertTrue(payload.get('success'))
        self.assertFalse(payload.get('changed'))
        self.assertEqual(payload.get('count'), 2)
        self.assertEqual(payload.get('channels'), [])
        self.assertEqual(payload.get('rev'), first_payload.get('rev'))


if __name__ == '__main__':
    unittest.main()
