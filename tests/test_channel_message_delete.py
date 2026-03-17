"""Regression tests for deleting channel messages from the UI."""

import os
import sqlite3
import sys
import types
import unittest
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

from canopy.ui.routes import create_ui_blueprint


class _FakeDbManager:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def get_connection(self) -> sqlite3.Connection:
        return self._conn


class TestChannelMessageDelete(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            """
            CREATE TABLE channel_messages (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                channel_id TEXT,
                content TEXT,
                attachments TEXT
            );
            """
        )

        db_manager = _FakeDbManager(self.conn)

        p2p_manager = MagicMock()
        p2p_manager.is_running.return_value = False

        # Order must match get_app_components in canopy.core.utils.
        components = (
            db_manager,               # db_manager
            MagicMock(),             # api_key_manager
            MagicMock(),             # trust_manager
            MagicMock(),             # message_manager
            MagicMock(),             # channel_manager
            MagicMock(),             # file_manager
            MagicMock(),             # feed_manager
            MagicMock(),             # interaction_manager
            MagicMock(),             # profile_manager
            MagicMock(),             # config
            p2p_manager,             # p2p_manager
        )

        self.get_components_patcher = patch(
            'canopy.ui.routes.get_app_components',
            return_value=components,
        )
        self.get_components_patcher.start()
        self.addCleanup(self.get_components_patcher.stop)

        app = Flask(__name__)
        app.config['TESTING'] = True
        app.secret_key = 'test-secret'
        app.register_blueprint(create_ui_blueprint())

        self.client = app.test_client()

    def tearDown(self) -> None:
        self.conn.close()

    def _set_authenticated_session(self, csrf_token: str = 'csrf-test-token') -> None:
        with self.client.session_transaction() as sess:
            sess['authenticated'] = True
            sess['user_id'] = 'test-user'
            sess['_csrf_token'] = csrf_token

    def _insert_message(self, message_id: str, owner_id: str) -> None:
        self.conn.execute(
            'INSERT INTO channel_messages (id, user_id, channel_id, content) VALUES (?, ?, ?, ?)',
            (message_id, owner_id, 'general', 'hello'),
        )
        self.conn.commit()

    def test_delete_channel_message_requires_csrf(self) -> None:
        self._insert_message('m-1', 'test-user')
        self._set_authenticated_session()

        response = self.client.post(
            '/ajax/delete_channel_message',
            json={'message_id': 'm-1'},
        )

        self.assertEqual(response.status_code, 403)

    def test_delete_channel_message_succeeds_for_owner_with_csrf(self) -> None:
        self._insert_message('m-2', 'test-user')
        csrf_token = 'csrf-ok'
        self._set_authenticated_session(csrf_token=csrf_token)

        response = self.client.post(
            '/ajax/delete_channel_message',
            json={'message_id': 'm-2'},
            headers={'X-CSRFToken': csrf_token},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json() or {}
        self.assertTrue(payload.get('success'))

    def test_delete_channel_message_rejects_non_owner(self) -> None:
        self._insert_message('m-3', 'other-user')
        csrf_token = 'csrf-ok'
        self._set_authenticated_session(csrf_token=csrf_token)

        response = self.client.post(
            '/ajax/delete_channel_message',
            json={'message_id': 'm-3'},
            headers={'X-CSRFToken': csrf_token},
        )

        self.assertEqual(response.status_code, 403)
        payload = response.get_json() or {}
        self.assertIn('own messages', payload.get('error', ''))

    def test_delete_channel_message_missing_message_id_returns_400(self) -> None:
        csrf_token = 'csrf-ok'
        self._set_authenticated_session(csrf_token=csrf_token)

        response = self.client.post(
            '/ajax/delete_channel_message',
            data='not-json',
            content_type='application/json',
            headers={'X-CSRFToken': csrf_token},
        )

        self.assertEqual(response.status_code, 400)
        payload = response.get_json() or {}
        self.assertEqual(payload.get('error'), 'Message ID required')


if __name__ == '__main__':
    unittest.main()
