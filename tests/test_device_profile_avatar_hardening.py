"""Regression tests for device-profile avatar hardening."""

import base64
import io
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

from canopy.api.routes import create_api_blueprint
from canopy.core.device import normalize_device_avatar


class TestDeviceProfileAvatarHardening(unittest.TestCase):
    def test_normalize_device_avatar_converts_to_bounded_jpeg(self):
        from PIL import Image

        image = Image.new('RGBA', (1200, 900), (20, 140, 220, 255))
        raw = io.BytesIO()
        image.save(raw, format='PNG')

        avatar_b64, avatar_mime = normalize_device_avatar(
            base64.b64encode(raw.getvalue()).decode('ascii'),
            'image/png',
        )

        normalized_bytes = base64.b64decode(avatar_b64)
        self.assertEqual(avatar_mime, 'image/jpeg')
        self.assertLessEqual(len(normalized_bytes), 48 * 1024)

        reopened = Image.open(io.BytesIO(normalized_bytes))
        self.assertEqual(reopened.format, 'JPEG')
        self.assertLessEqual(max(reopened.size), 256)

    def test_normalize_device_avatar_rejects_invalid_base64(self):
        with self.assertRaises(ValueError):
            normalize_device_avatar('not-base64!!!', 'image/png')

    def test_device_profile_api_rejects_invalid_avatar_payload(self):
        db_manager = MagicMock()
        db_manager.get_user.return_value = {
            'id': 'test-user',
            'username': 'test-user',
            'display_name': 'Test User',
            'account_type': 'human',
            'status': 'active',
        }
        api_key_manager = MagicMock()
        api_key_manager.validate_key.return_value = None
        p2p_manager = MagicMock()
        p2p_manager.is_running.return_value = False

        components = (
            db_manager,
            api_key_manager,
            MagicMock(),
            MagicMock(),
            MagicMock(),
            MagicMock(),
            MagicMock(),
            MagicMock(),
            MagicMock(),
            MagicMock(),
            p2p_manager,
        )

        with patch('canopy.api.routes.get_app_components', return_value=components):
            app = Flask(__name__)
            app.config['TESTING'] = True
            app.secret_key = 'test-secret'
            app.register_blueprint(create_api_blueprint(), url_prefix='/api/v1')
            client = app.test_client()
            with client.session_transaction() as sess:
                sess['authenticated'] = True
                sess['user_id'] = 'test-user'
                sess['_csrf_token'] = 'csrf-device-avatar'

            response = client.post(
                '/api/v1/device/profile',
                json={
                    'display_name': 'Device',
                    'avatar_b64': 'not-base64!!!',
                    'avatar_mime': 'image/png',
                },
                headers={'X-CSRFToken': 'csrf-device-avatar'},
            )

        self.assertEqual(response.status_code, 400)
        payload = response.get_json() or {}
        self.assertIn('base64', str(payload.get('error') or '').lower())


if __name__ == '__main__':
    unittest.main()
