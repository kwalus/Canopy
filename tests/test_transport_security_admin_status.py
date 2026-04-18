"""Regression tests for admin transport-security status truthfulness."""

import os
import sys
import tempfile
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

from canopy.ui.routes import create_ui_blueprint


def _build_components(tmpdir: str):
    db_manager = MagicMock()
    db_manager.get_instance_owner_user_id.return_value = 'owner-user'

    network = types.SimpleNamespace(
        mesh_port=7771,
        enable_tls=False,
        tls_cert_path='',
        tls_key_path='',
        external_tls_endpoint='',
        transport_security_mode='disabled',
        require_verified_wss=True,
    )
    config = types.SimpleNamespace(
        network=network,
        storage=types.SimpleNamespace(data_dir=tmpdir),
        meshspace=types.SimpleNamespace(meshspace_id='mesh-a'),
    )
    p2p_manager = types.SimpleNamespace(
        connection_manager=types.SimpleNamespace(tls_cert_path='', tls_key_path=''),
        get_transport_security_status=lambda: {
            'tls_enabled': False,
            'client_verification_mode': 'permissive',
            'transport_security_mode': 'disabled',
            'transport_security_mode_label': 'Disabled',
            'listener_endpoint': 'ws://0.0.0.0:7771',
            'warnings': [],
        },
    )

    return (
        db_manager,
        MagicMock(),
        MagicMock(),
        MagicMock(),
        MagicMock(),
        MagicMock(),
        MagicMock(),
        MagicMock(),
        MagicMock(),
        config,
        p2p_manager,
    )


class TestTransportSecurityAdminStatus(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        components = _build_components(self._tmp.name)
        self.patcher = patch('canopy.ui.routes.get_app_components', return_value=components)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

        self.app = Flask(__name__)
        self.app.config['TESTING'] = True
        self.app.secret_key = 'test-secret'
        self.app.register_blueprint(create_ui_blueprint())
        self.client = self.app.test_client()

    def _authenticate_admin(self):
        with self.client.session_transaction() as sess:
            sess['authenticated'] = True
            sess['user_id'] = 'owner-user'

    def test_status_marks_verification_mode_drift_as_restart_required(self):
        self._authenticate_admin()

        with patch('canopy.ui.routes.validate_csrf_request', return_value=None):
            response = self.client.get('/ajax/admin/transport-security/status')

        self.assertEqual(response.status_code, 200)
        payload = response.get_json() or {}
        self.assertTrue(payload.get('restart_required'))
        self.assertEqual(payload.get('configured_client_verification_mode'), 'verified')
        self.assertEqual(payload.get('live_client_verification_mode'), 'permissive')
        self.assertEqual(payload.get('client_verification_mode'), 'permissive')
        self.assertIn('restart pending', str(payload.get('verification_display') or '').lower())
        self.assertTrue(
            any(
                'verification mode has changed' in str(item).lower()
                for item in (payload.get('warnings') or [])
            )
        )


if __name__ == '__main__':
    unittest.main()
