"""Regression tests for task page identity context."""

import os
import sys
import types
import unittest
from unittest.mock import patch

from flask import Flask, jsonify

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

from canopy.ui.routes import create_ui_blueprint


class TestTasksPageIdentityContext(unittest.TestCase):
    def setUp(self) -> None:
        app = Flask(__name__)
        app.config['TESTING'] = True
        app.secret_key = 'test-secret'
        app.register_blueprint(create_ui_blueprint())
        self.client = app.test_client()

    def test_tasks_page_passes_global_user_identity_to_base_template(self) -> None:
        with self.client.session_transaction() as sess:
            sess['authenticated'] = True
            sess['user_id'] = 'user-123'
            sess['username'] = 'konrad'
            sess['display_name'] = 'Konrad'

        with patch('canopy.ui.routes.render_template') as render_template_mock:
            render_template_mock.side_effect = lambda template_name, **context: jsonify(
                {'template': template_name, **context}
            )
            response = self.client.get('/tasks')

        self.assertEqual(response.status_code, 200)
        payload = response.get_json() or {}
        self.assertEqual(payload.get('template'), 'tasks.html')
        self.assertEqual(payload.get('current_user_id'), 'user-123')
        self.assertEqual(payload.get('current_user_name'), 'Konrad')
        self.assertEqual(payload.get('user_id'), 'user-123')
