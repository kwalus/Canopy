"""Regression tests for channel message UI routes."""

import json
import os
import sqlite3
import sys
import tempfile
import types
import unittest
from datetime import datetime, timezone
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
                content TEXT,
                attachments TEXT
            );
            CREATE TABLE community_notes (
                id TEXT PRIMARY KEY,
                target_type TEXT,
                target_id TEXT,
                author_id TEXT
            );
            """
        )
        self.conn.execute(
            """
            INSERT INTO channel_messages (id, channel_id, user_id, content, attachments)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                'M-delete',
                'general',
                'owner',
                'Original channel message content',
                json.dumps([{'id': 'F1', 'name': 'proof.txt'}]),
            ),
        )
        self.conn.execute(
            """
            INSERT INTO community_notes (id, target_type, target_id, author_id)
            VALUES (?, ?, ?, ?)
            """,
            ('CN-rate', 'channel_message', 'M-delete', 'owner'),
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
        self.skill_manager = MagicMock()
        self.skill_manager.create_community_note.return_value = 'CN-created'
        self.skill_manager.rate_community_note.return_value = True
        self.skill_manager.get_community_notes.side_effect = self._fake_get_community_notes

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
        app.config['CHANNEL_MANAGER'] = self.channel_manager
        app.config['SKILL_MANAGER'] = self.skill_manager
        app.register_blueprint(create_ui_blueprint())
        self.client = app.test_client()
        with self.client.session_transaction() as sess:
            sess['authenticated'] = True
            sess['user_id'] = 'owner'
            sess['_csrf_token'] = 'csrf-channel-delete'

    def tearDown(self) -> None:
        self.conn.close()

    def _fake_get_community_notes(self, *args, **kwargs):
        target_type = kwargs.get('target_type')
        target_id = kwargs.get('target_id')
        if target_type == 'channel_message' and target_id == 'M-delete':
            return [
                {
                    'id': 'CN-created',
                    'target_type': 'channel_message',
                    'target_id': 'M-delete',
                    'author_id': 'owner',
                    'content': 'Fresh context note',
                    'status': 'proposed',
                    'note_type': 'context',
                    'ratings': {'total': 0, 'helpful': 0},
                },
                {
                    'id': 'CN-rate',
                    'target_type': 'channel_message',
                    'target_id': 'M-delete',
                    'author_id': 'owner',
                    'content': 'Existing note',
                    'status': 'proposed',
                    'note_type': 'correction',
                    'ratings': {'total': 1, 'helpful': 1},
                },
            ]
        return []

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

    def test_channel_messages_snapshot_refreshes_remote_stream_attachment_status(self) -> None:
        message = MagicMock()
        message.id = 'M-stream'
        message.channel_id = 'general'
        message.user_id = 'owner'
        message.content = 'Remote stream card'
        message.created_at = datetime.now(timezone.utc)
        message.expires_at = None
        message.to_dict.return_value = {
            'id': 'M-stream',
            'channel_id': 'general',
            'user_id': 'owner',
            'content': 'Remote stream card',
            'type': 'file',
            'created_at': message.created_at.isoformat(),
            'attachments': [
                {
                    'kind': 'stream',
                    'type': 'application/vnd.canopy.stream+json',
                    'stream_id': 'ST-remote',
                    'status': 'created',
                    'title': 'Remote watch',
                }
            ],
            'security': {},
            'reactions': {},
        }
        self.channel_manager.get_channel_messages.return_value = [message]
        self.client.application.config['STREAM_MANAGER'] = MagicMock()
        self.client.application.config['STREAM_MANAGER'].get_stream_for_user.return_value = None
        route_components = (
            self.db_manager,
            MagicMock(),
            MagicMock(),
            MagicMock(),
            self.channel_manager,
            self.file_manager,
            MagicMock(),
            None,
            None,
            MagicMock(),
            self.p2p_manager,
        )

        with patch('canopy.ui.routes._get_app_components_any', return_value=route_components), \
             patch('canopy.ui.routes._resolve_p2p_stream', return_value={'remote_base': 'http://peer.test'}), \
             patch('canopy.ui.routes._probe_remote_stream_manifest_live', return_value=True):
            response = self.client.get('/ajax/channel_messages/general')

        self.assertEqual(response.status_code, 200)
        payload = response.get_json() or {}
        messages = payload.get('messages') or []
        self.assertEqual(len(messages), 1)
        attachments = messages[0].get('attachments') or []
        self.assertEqual(len(attachments), 1)
        self.assertEqual(attachments[0].get('status'), 'live')

    def test_create_community_note_on_channel_message_emits_metadata_event(self) -> None:
        response = self.client.post(
            '/ajax/community_notes',
            json={
                'target_type': 'channel_message',
                'target_id': 'M-delete',
                'note_type': 'context',
                'content': 'Fresh context note for this channel message.',
            },
            headers={'X-CSRFToken': 'csrf-channel-delete'},
        )

        self.assertEqual(response.status_code, 201)
        payload = response.get_json() or {}
        self.assertTrue(payload.get('success'))
        self.channel_manager._emit_channel_user_event.assert_any_call(
            channel_id='general',
            event_type='channel.message.edited',
            actor_user_id='owner',
            payload={
                'message_id': 'M-delete',
                'preview': 'Original channel message content',
                'reason': 'community_note_created',
            },
            dedupe_suffix='community_note_created:CN-created',
        )

    def test_rate_community_note_on_channel_message_emits_metadata_event(self) -> None:
        response = self.client.post(
            '/ajax/community_notes/CN-rate/rate',
            json={'helpful': True},
            headers={'X-CSRFToken': 'csrf-channel-delete'},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json() or {}
        self.assertTrue(payload.get('success'))
        self.channel_manager._emit_channel_user_event.assert_any_call(
            channel_id='general',
            event_type='channel.message.edited',
            actor_user_id='owner',
            payload={
                'message_id': 'M-delete',
                'preview': 'Original channel message content',
                'reason': 'community_note_rated',
            },
            dedupe_suffix='community_note_rated:CN-rate:1',
        )


if __name__ == '__main__':
    unittest.main()
