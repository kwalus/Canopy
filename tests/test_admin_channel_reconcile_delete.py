"""Tests for admin channel delete-signal reconciliation endpoint."""

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
    def __init__(self, conn: sqlite3.Connection, owner_user_id: str) -> None:
        self._conn = conn
        self._owner_user_id = owner_user_id

    def get_connection(self) -> sqlite3.Connection:
        return self._conn

    def get_instance_owner_user_id(self):
        return self._owner_user_id


class TestAdminChannelReconcileDelete(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            """
            CREATE TABLE channels (
                id TEXT PRIMARY KEY,
                origin_peer TEXT
            );
            """
        )
        self.conn.executemany(
            "INSERT INTO channels (id, origin_peer) VALUES (?, ?)",
            [
                ('C_local', 'peer-local'),
                ('C_remote', 'peer-remote'),
            ],
        )
        self.conn.commit()

        self.db_manager = _FakeDbManager(self.conn, owner_user_id='admin-user')
        self.p2p_manager = MagicMock()
        self.p2p_manager.is_running.return_value = True
        self.p2p_manager.get_peer_id.return_value = 'peer-local'
        self.p2p_manager.broadcast_delete_signal.return_value = True

        components = (
            self.db_manager,          # db_manager
            MagicMock(),              # api_key_manager
            MagicMock(),              # trust_manager
            MagicMock(),              # message_manager
            MagicMock(),              # channel_manager
            MagicMock(),              # file_manager
            MagicMock(),              # feed_manager
            MagicMock(),              # interaction_manager
            MagicMock(),              # profile_manager
            MagicMock(),              # config
            self.p2p_manager,         # p2p_manager
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

    def _set_authenticated_session(self, user_id: str = 'admin-user', csrf_token: str = 'csrf-ok') -> None:
        with self.client.session_transaction() as sess:
            sess['authenticated'] = True
            sess['user_id'] = user_id
            sess['_csrf_token'] = csrf_token

    def test_reconcile_delete_requires_admin(self) -> None:
        self._set_authenticated_session(user_id='not-admin')
        response = self.client.post(
            '/ajax/admin/channels/reconcile-delete',
            json={'channel_ids_text': 'C_local'},
            headers={'X-CSRFToken': 'csrf-ok'},
        )
        self.assertEqual(response.status_code, 403)

    def test_reconcile_delete_sends_for_local_and_missing_channels(self) -> None:
        self._set_authenticated_session(csrf_token='csrf-reconcile')
        response = self.client.post(
            '/ajax/admin/channels/reconcile-delete',
            json={
                'channel_ids_text': 'C_local, C_remote\nC_missing\ngeneral',
                'target_peer_id': 'all',
                'reason': 'manual_cleanup',
            },
            headers={'X-CSRFToken': 'csrf-reconcile'},
        )
        self.assertEqual(response.status_code, 200)
        payload = response.get_json() or {}
        self.assertEqual(payload.get('requested'), 4)
        self.assertEqual(payload.get('sent'), 2)
        self.assertEqual(payload.get('skipped'), 2)
        self.assertEqual(payload.get('failed'), 0)

        sent_ids = {
            call.kwargs.get('data_id')
            for call in self.p2p_manager.broadcast_delete_signal.call_args_list
        }
        self.assertEqual(sent_ids, {'C_local', 'C_missing'})
        for call in self.p2p_manager.broadcast_delete_signal.call_args_list:
            self.assertEqual(call.kwargs.get('data_type'), 'channel')
            self.assertEqual(call.kwargs.get('reason'), 'manual_cleanup')
            self.assertIsNone(call.kwargs.get('target_peer'))

    def test_reconcile_delete_supports_target_peer(self) -> None:
        self._set_authenticated_session(csrf_token='csrf-target')
        response = self.client.post(
            '/ajax/admin/channels/reconcile-delete',
            json={
                'channel_ids_text': 'C_local',
                'target_peer_id': 'peer-target',
            },
            headers={'X-CSRFToken': 'csrf-target'},
        )
        self.assertEqual(response.status_code, 200)
        self.p2p_manager.broadcast_delete_signal.assert_called_once()
        kwargs = self.p2p_manager.broadcast_delete_signal.call_args.kwargs
        self.assertEqual(kwargs.get('target_peer'), 'peer-target')
        self.assertEqual(kwargs.get('data_id'), 'C_local')


if __name__ == '__main__':
    unittest.main()
