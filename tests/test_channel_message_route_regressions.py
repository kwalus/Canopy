"""Regression tests for channel message UI routes."""

import json
import os
import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path
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


class _FakeDbManager:
    def __init__(self, conn: sqlite3.Connection, db_path: Path) -> None:
        self._conn = conn
        self.db_path = db_path

    def get_connection(self) -> sqlite3.Connection:
        return self._conn

    def get_instance_owner_user_id(self):
        return 'owner'


class _FakeWorkspaceEventManager:
    def __init__(self, latest_seq: int = 0) -> None:
        self.latest_seq = latest_seq

    def get_latest_seq(self) -> int:
        return int(self.latest_seq)


class TestChannelMessageRouteRegressions(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)

        db_path = Path(self.tempdir.name) / 'channel-route.db'
        self.conn = sqlite3.connect(str(db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            """
            CREATE TABLE channel_messages (
                id TEXT PRIMARY KEY,
                channel_id TEXT,
                user_id TEXT,
                attachments TEXT
            );
            """
        )
        self.conn.execute(
            """
            INSERT INTO channel_messages (id, channel_id, user_id, attachments)
            VALUES (?, ?, ?, ?)
            """,
            (
                'M-delete',
                'general',
                'owner',
                json.dumps([{'id': 'F1', 'name': 'proof.txt'}]),
            ),
        )
        self.conn.commit()

        self.db_manager = _FakeDbManager(self.conn, db_path)
        self.channel_manager = MagicMock()
        self.channel_manager.DEFAULT_CHANNEL_LIFECYCLE_DAYS = 180
        self.channel_manager.delete_message.return_value = True
        self.channel_manager.get_channel_access_decision.return_value = {'allowed': True}
        self.channel_manager.purge_expired_channel_messages.return_value = []
        self.channel_manager.get_channel_messages.return_value = []
        self.file_manager = MagicMock()
        self.file_manager.get_file.return_value = types.SimpleNamespace(uploaded_by='owner')
        self.file_manager.is_file_referenced.return_value = False
        self.p2p_manager = MagicMock()
        self.p2p_manager.is_running.return_value = False
        self.workspace_events = _FakeWorkspaceEventManager()

        components = (
            self.db_manager,
            MagicMock(),
            MagicMock(),
            MagicMock(),
            self.channel_manager,
            self.file_manager,
            MagicMock(),
            MagicMock(),
            MagicMock(),
            MagicMock(),
            self.p2p_manager,
        )

        self.get_components_any_patcher = patch(
            'canopy.ui.routes._get_app_components_any',
            return_value=components,
        )
        self.get_components_patcher = patch(
            'canopy.ui.routes.get_app_components',
            return_value=components,
        )
        self.get_components_any_patcher.start()
        self.get_components_patcher.start()
        self.addCleanup(self.get_components_any_patcher.stop)
        self.addCleanup(self.get_components_patcher.stop)

        app = Flask(__name__)
        app.config['TESTING'] = True
        app.secret_key = 'channel-route-secret'
        app.config['WORKSPACE_EVENT_MANAGER'] = self.workspace_events
        app.register_blueprint(create_ui_blueprint())
        self.client = app.test_client()
        with self.client.session_transaction() as sess:
            sess['authenticated'] = True
            sess['user_id'] = 'owner'
            sess['_csrf_token'] = 'csrf-channel-delete'

    def tearDown(self) -> None:
        self.conn.close()

    def test_ajax_delete_channel_message_uses_channel_manager_path(self) -> None:
        response = self.client.post(
            '/ajax/delete_channel_message',
            json={'message_id': 'M-delete'},
            headers={'X-CSRFToken': 'csrf-channel-delete'},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json() or {}
        self.assertTrue(payload.get('success'))
        self.channel_manager.delete_message.assert_called_once_with(
            channel_id='general',
            message_id='M-delete',
            user_id='owner',
            allow_admin=False,
        )
        self.file_manager.delete_file.assert_called_once_with('F1', 'owner')

    def test_channel_messages_snapshot_cursor_does_not_advance_past_snapshot_state(self) -> None:
        self.workspace_events.latest_seq = 5
        original_get_channel_messages = self.channel_manager.get_channel_messages

        def _race_get_channel_messages(*args, **kwargs):
            self.workspace_events.latest_seq = 9
            return original_get_channel_messages(*args, **kwargs)

        with patch.object(self.channel_manager, 'get_channel_messages', side_effect=_race_get_channel_messages):
            response = self.client.get('/ajax/channel_messages/general')

        self.assertEqual(response.status_code, 200)
        payload = response.get_json() or {}
        self.assertEqual(payload.get('workspace_event_cursor'), 5)


if __name__ == '__main__':
    unittest.main()
