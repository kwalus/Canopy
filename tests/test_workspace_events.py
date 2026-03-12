"""Regression coverage for the unified workspace event journal Patch 1."""

import json
import os
import sqlite3
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
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

from canopy.api.routes import create_api_blueprint
from canopy.core.agent_heartbeat import build_agent_heartbeat_snapshot
from canopy.core.app import _apply_inbound_dm_delete, _finalize_inbound_dm_message
from canopy.core.events import (
    EVENT_ATTACHMENT_AVAILABLE,
    EVENT_CHANNEL_MESSAGE_CREATED,
    EVENT_CHANNEL_MESSAGE_DELETED,
    EVENT_CHANNEL_MESSAGE_EDITED,
    EVENT_CHANNEL_MESSAGE_READ,
    EVENT_CHANNEL_STATE_UPDATED,
    EVENT_DM_MESSAGE_CREATED,
    EVENT_DM_MESSAGE_DELETED,
    EVENT_DM_MESSAGE_READ,
    EVENT_MENTION_CREATED,
    WorkspaceEventManager,
)
from canopy.core.messaging import MessageManager, MessageType
from canopy.security.api_keys import ApiKeyInfo, Permission


class _FakeDbManager:
    def __init__(self, conn: sqlite3.Connection, db_path: Path, owner_user_id: str = 'owner-user') -> None:
        self._conn = conn
        self.db_path = db_path
        self._owner_user_id = owner_user_id

    def get_connection(self) -> sqlite3.Connection:
        return self._conn

    def get_user(self, user_id: str):
        row = self._conn.execute(
            "SELECT * FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
        return dict(row) if row else None

    def get_instance_owner_user_id(self):
        return self._owner_user_id

    def store_message(self, message_id, sender_id, recipient_id, content, message_type, metadata):
        created_at = datetime.now(timezone.utc).isoformat()
        self._conn.execute(
            """
            INSERT INTO messages (
                id, sender_id, recipient_id, content, message_type,
                status, created_at, delivered_at, read_at, edited_at, metadata
            ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, ?)
            """,
            (
                message_id,
                sender_id,
                recipient_id,
                content,
                message_type,
                'pending',
                created_at,
                json.dumps(metadata) if metadata else None,
            ),
        )
        self._conn.commit()
        return True


class _FakeApiKeyManager:
    def __init__(self, key_map: dict[str, tuple[str, set[Permission]]]) -> None:
        self._key_map = key_map

    def validate_key(self, raw_key: str, required_permission=None):
        info = self._key_map.get(raw_key)
        if not info:
            return None
        user_id, permissions = info
        if required_permission and required_permission not in permissions:
            return None
        return ApiKeyInfo(
            id=f"key-{user_id}",
            user_id=user_id,
            key_hash='hash',
            permissions=permissions,
            created_at=datetime.now(timezone.utc),
        )


class TestWorkspaceEvents(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)

        self.db_file = Path(self.tempdir.name) / 'workspace_events.db'
        self.conn = sqlite3.connect(str(self.db_file))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            """
            CREATE TABLE users (
                id TEXT PRIMARY KEY,
                username TEXT,
                display_name TEXT,
                account_type TEXT,
                agent_directives TEXT
            );
            CREATE TABLE messages (
                id TEXT PRIMARY KEY,
                sender_id TEXT,
                recipient_id TEXT,
                content TEXT,
                message_type TEXT,
                status TEXT,
                created_at TEXT,
                delivered_at TEXT,
                read_at TEXT,
                edited_at TEXT,
                metadata TEXT
            );
            CREATE TABLE mention_events (
                id TEXT PRIMARY KEY,
                user_id TEXT,
                created_at TEXT,
                acknowledged_at TEXT
            );
            CREATE TABLE agent_inbox (
                id TEXT PRIMARY KEY,
                agent_user_id TEXT,
                status TEXT,
                created_at TEXT
            );
            """
        )
        self.conn.executemany(
            """
            INSERT INTO users (id, username, display_name, account_type, agent_directives)
            VALUES (?, ?, ?, ?, ?)
            """,
            [
                ('owner-user', 'maddog', 'Maddog', 'human', None),
                ('agent-a', 'Agent_A', 'Agent A', 'agent', None),
                ('agent-b', 'Agent_B', 'Agent B', 'agent', None),
                ('observer', 'Observer', 'Observer', 'human', None),
            ],
        )
        self.conn.commit()

        self.db_manager = _FakeDbManager(self.conn, self.db_file)
        self.workspace_events = WorkspaceEventManager(self.db_manager)

        key_map = {
            'owner-key': (
                'owner-user',
                {Permission.READ_FEED, Permission.READ_MESSAGES, Permission.MANAGE_KEYS},
            ),
            'agent-key': (
                'agent-a',
                {Permission.READ_FEED, Permission.READ_MESSAGES},
            ),
            'agent-feed-only': (
                'agent-a',
                {Permission.READ_FEED},
            ),
            'observer-key': (
                'observer',
                {Permission.READ_FEED, Permission.READ_MESSAGES},
            ),
        }
        self.api_key_manager = _FakeApiKeyManager(key_map)
        self.message_manager = MessageManager(self.db_manager, self.api_key_manager)
        self.message_manager.workspace_events = self.workspace_events

        components = (
            self.db_manager,
            self.api_key_manager,
            MagicMock(),
            self.message_manager,
            MagicMock(),
            MagicMock(),
            MagicMock(),
            MagicMock(),
            MagicMock(),
            MagicMock(),
            MagicMock(),
        )
        self.get_components_patcher = patch(
            'canopy.api.routes.get_app_components',
            return_value=components,
        )
        self.get_components_patcher.start()
        self.addCleanup(self.get_components_patcher.stop)

        app = Flask(__name__)
        app.config['TESTING'] = True
        app.secret_key = 'workspace-events'
        app.config['DB_MANAGER'] = self.db_manager
        app.config['WORKSPACE_EVENT_MANAGER'] = self.workspace_events
        app.config['INBOX_MANAGER'] = MagicMock()
        app.config['INBOX_MANAGER'].list_items.return_value = []
        api_bp = create_api_blueprint()
        app.register_blueprint(api_bp, url_prefix='/api/v1')
        self.client = app.test_client()

    def test_manager_dedupes_and_prunes(self) -> None:
        old_iso = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
        first_seq = self.workspace_events.emit_event(
            event_type=EVENT_MENTION_CREATED,
            actor_user_id='owner-user',
            target_user_id='agent-a',
            visibility_scope='user',
            dedupe_key='mention:dedupe',
            created_at=old_iso,
            payload={'mention_id': 'MN-old', 'source_type': 'channel_message', 'source_id': 'msg-1'},
        )
        duplicate_seq = self.workspace_events.emit_event(
            event_type=EVENT_MENTION_CREATED,
            actor_user_id='owner-user',
            target_user_id='agent-a',
            visibility_scope='user',
            dedupe_key='mention:dedupe',
            created_at=old_iso,
            payload={'mention_id': 'MN-old', 'source_type': 'channel_message', 'source_id': 'msg-1'},
        )
        self.assertEqual(first_seq, duplicate_seq)

        for idx in range(3):
            self.workspace_events.emit_event(
                event_type=EVENT_MENTION_CREATED,
                actor_user_id='owner-user',
                target_user_id='agent-a',
                visibility_scope='user',
                dedupe_key=f'mention:new:{idx}',
                created_at=(datetime.now(timezone.utc) + timedelta(seconds=idx)).isoformat(),
                payload={'mention_id': f'MN-{idx}', 'source_type': 'channel_message', 'source_id': f'msg-{idx + 2}'},
            )

        removed = self.workspace_events.prune_old_events(retention_days=1, max_rows=2)
        count = self.conn.execute("SELECT COUNT(*) AS n FROM workspace_events").fetchone()['n']
        self.assertGreaterEqual(removed, 2)
        self.assertEqual(count, 2)

    def test_channel_edit_and_delete_events_are_supported(self) -> None:
        edit_seq = self.workspace_events.emit_event(
            event_type=EVENT_CHANNEL_MESSAGE_EDITED,
            actor_user_id='owner-user',
            target_user_id='agent-a',
            channel_id='general',
            message_id='M-edit',
            visibility_scope='user',
            dedupe_key='channel-edit:M-edit:agent-a',
            payload={'message_id': 'M-edit', 'preview': 'Edited content'},
        )
        delete_seq = self.workspace_events.emit_event(
            event_type=EVENT_CHANNEL_MESSAGE_DELETED,
            actor_user_id='owner-user',
            target_user_id='agent-a',
            channel_id='general',
            message_id='M-delete',
            visibility_scope='user',
            dedupe_key='channel-delete:M-delete:agent-a',
            payload={'message_id': 'M-delete'},
        )

        self.assertIsInstance(edit_seq, int)
        self.assertIsInstance(delete_seq, int)

        rows = self.conn.execute(
            """
            SELECT event_type, message_id
            FROM workspace_events
            WHERE event_type IN (?, ?)
            ORDER BY seq ASC
            """,
            (EVENT_CHANNEL_MESSAGE_EDITED, EVENT_CHANNEL_MESSAGE_DELETED),
        ).fetchall()
        self.assertEqual(
            [(row['event_type'], row['message_id']) for row in rows],
            [
                (EVENT_CHANNEL_MESSAGE_EDITED, 'M-edit'),
                (EVENT_CHANNEL_MESSAGE_DELETED, 'M-delete'),
            ],
        )

    def test_deleted_dm_event_visibility_falls_back_to_payload(self) -> None:
        self.workspace_events.emit_event(
            event_type=EVENT_DM_MESSAGE_DELETED,
            actor_user_id='agent-b',
            message_id='dm-deleted',
            visibility_scope='dm',
            dedupe_key='dm-delete:1',
            payload={
                'preview': 'secret removed',
                'sender_id': 'agent-b',
                'recipient_id': 'agent-a',
                'group_id': None,
                'group_members': [],
            },
        )

        visible = self.workspace_events.list_events_for_user(
            user_id='agent-a',
            after_seq=0,
            limit=20,
            can_read_messages=True,
        )
        hidden = self.workspace_events.list_events_for_user(
            user_id='observer',
            after_seq=0,
            limit=20,
            can_read_messages=True,
        )
        no_dm_permission = self.workspace_events.list_events_for_user(
            user_id='agent-a',
            after_seq=0,
            limit=20,
            can_read_messages=False,
        )

        self.assertEqual([item['event_type'] for item in visible['items']], [EVENT_DM_MESSAGE_DELETED])
        self.assertEqual(hidden['items'], [])
        self.assertEqual(no_dm_permission['items'], [])

    def test_heartbeat_adds_workspace_event_seq_without_repurposing_last_event_seq(self) -> None:
        self.conn.execute(
            """
            INSERT INTO mention_events (id, user_id, created_at, acknowledged_at)
            VALUES (?, ?, ?, ?)
            """,
            ('MN-latest', 'agent-a', '2026-03-09 10:00:00.000000', None),
        )
        self.conn.execute(
            """
            INSERT INTO agent_inbox (id, agent_user_id, status, created_at)
            VALUES (?, ?, ?, ?)
            """,
            ('INB-latest', 'agent-a', 'pending', '2026-03-09 10:05:00.000000'),
        )
        self.conn.commit()

        emitted_seq = self.workspace_events.emit_event(
            event_type=EVENT_MENTION_CREATED,
            actor_user_id='owner-user',
            target_user_id='agent-a',
            visibility_scope='user',
            dedupe_key='heartbeat:mention',
            payload={'mention_id': 'MN-evt', 'source_type': 'channel_message', 'source_id': 'msg-heartbeat'},
        )

        snapshot = build_agent_heartbeat_snapshot(
            db_manager=self.db_manager,
            user_id='agent-a',
            mention_manager=None,
            inbox_manager=None,
            workspace_event_manager=self.workspace_events,
        )

        self.assertEqual(snapshot['workspace_event_seq'], emitted_seq)
        self.assertEqual(
            snapshot['last_event_seq'],
            max(snapshot['last_mention_seq'], snapshot['last_inbox_seq']),
        )
        self.assertNotEqual(snapshot['last_event_seq'], snapshot['workspace_event_seq'])

    def test_events_endpoint_and_owner_only_diagnostics(self) -> None:
        self.workspace_events.emit_event(
            event_type=EVENT_MENTION_CREATED,
            actor_user_id='owner-user',
            target_user_id='agent-a',
            visibility_scope='user',
            dedupe_key='api:mention',
            payload={'mention_id': 'MN-api', 'source_type': 'channel_message', 'source_id': 'msg-api'},
        )
        self.workspace_events.emit_event(
            event_type=EVENT_DM_MESSAGE_DELETED,
            actor_user_id='agent-b',
            message_id='dm-deleted-api',
            visibility_scope='dm',
            dedupe_key='api:dm-delete',
            payload={
                'preview': 'removed',
                'sender_id': 'agent-b',
                'recipient_id': 'agent-a',
                'group_id': None,
                'group_members': [],
            },
        )

        response = self.client.get(
            '/api/v1/events',
            headers={'X-API-Key': 'agent-key'},
        )
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(
            [item['event_type'] for item in body['items']],
            [EVENT_MENTION_CREATED, EVENT_DM_MESSAGE_DELETED],
        )
        self.assertEqual(body['next_after_seq'], body['latest_seq'])

        mention_only = self.client.get(
            '/api/v1/events?types=mention.created',
            headers={'X-API-Key': 'agent-key'},
        )
        self.assertEqual(mention_only.status_code, 200)
        self.assertEqual(
            [item['event_type'] for item in mention_only.get_json()['items']],
            [EVENT_MENTION_CREATED],
        )

        feed_only = self.client.get(
            '/api/v1/events',
            headers={'X-API-Key': 'agent-feed-only'},
        )
        self.assertEqual(feed_only.status_code, 200)
        self.assertEqual(
            [item['event_type'] for item in feed_only.get_json()['items']],
            [EVENT_MENTION_CREATED],
        )

        forbidden = self.client.get(
            '/api/v1/events/diagnostics',
            headers={'X-API-Key': 'agent-key'},
        )
        self.assertEqual(forbidden.status_code, 403)

        diagnostics = self.client.get(
            '/api/v1/events/diagnostics',
            headers={'X-API-Key': 'owner-key'},
        )
        self.assertEqual(diagnostics.status_code, 200)
        diag_body = diagnostics.get_json()
        self.assertEqual(diag_body['count'], 2)
        self.assertIn('oldest_age_seconds', diag_body)
        self.assertIn('latest_created_at', diag_body)
        self.assertIn('type_counts', diag_body)
        self.assertEqual(diag_body['type_counts'][EVENT_MENTION_CREATED], 1)
        self.assertEqual(diag_body['type_counts'][EVENT_DM_MESSAGE_DELETED], 1)
        self.assertEqual(len(diag_body['items']), 2)
        self.assertIn('payload_keys', diag_body['items'][0])
        self.assertTrue(diag_body['items'][0]['payload_keys'])

        runtime_row = self.conn.execute(
            """
            SELECT last_event_cursor_seen, last_event_fetch_at
            FROM agent_runtime_state
            WHERE user_id = ?
            """,
            ('agent-a',),
        ).fetchone()
        self.assertIsNotNone(runtime_row)
        self.assertEqual(runtime_row['last_event_cursor_seen'], body['next_after_seq'])
        self.assertIsNotNone(runtime_row['last_event_fetch_at'])

    def test_agent_inbox_fetch_updates_runtime_state(self) -> None:
        response = self.client.get(
            '/api/v1/agents/me/inbox',
            headers={'X-API-Key': 'agent-key'},
        )
        self.assertEqual(response.status_code, 200)
        runtime_row = self.conn.execute(
            """
            SELECT last_inbox_fetch_at
            FROM agent_runtime_state
            WHERE user_id = ?
            """,
            ('agent-a',),
        ).fetchone()
        self.assertIsNotNone(runtime_row)
        self.assertIsNotNone(runtime_row['last_inbox_fetch_at'])

    def test_human_api_key_event_fetch_does_not_create_agent_runtime_state(self) -> None:
        response = self.client.get(
            '/api/v1/events',
            headers={'X-API-Key': 'owner-key'},
        )
        self.assertEqual(response.status_code, 200)
        table_row = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='agent_runtime_state'"
        ).fetchone()
        if table_row is None:
            return
        runtime_row = self.conn.execute(
            """
            SELECT last_event_cursor_seen, last_event_fetch_at
            FROM agent_runtime_state
            WHERE user_id = ?
            """,
            ('owner-user',),
        ).fetchone()
        self.assertIsNone(runtime_row)

    def test_agent_events_defaults_to_low_noise_actionable_types(self) -> None:
        self.workspace_events.emit_event(
            event_type=EVENT_MENTION_CREATED,
            actor_user_id='owner-user',
            target_user_id='agent-a',
            visibility_scope='user',
            dedupe_key='agent-events:mention',
            payload={'mention_id': 'MN-agent', 'source_type': 'channel_message', 'source_id': 'msg-agent'},
        )
        self.workspace_events.emit_event(
            event_type=EVENT_DM_MESSAGE_DELETED,
            actor_user_id='agent-b',
            message_id='DM-agent-deleted',
            visibility_scope='dm',
            dedupe_key='agent-events:dm-delete',
            payload={
                'preview': 'removed',
                'sender_id': 'agent-b',
                'recipient_id': 'agent-a',
                'group_id': None,
                'group_members': [],
            },
        )
        self.workspace_events.emit_event(
            event_type=EVENT_CHANNEL_MESSAGE_CREATED,
            actor_user_id='agent-b',
            target_user_id='agent-a',
            channel_id='general',
            visibility_scope='user',
            dedupe_key='agent-events:channel-created',
            payload={'message_id': 'CH-agent-created', 'preview': 'hello channel'},
        )

        response = self.client.get(
            '/api/v1/agents/me/events',
            headers={'X-API-Key': 'agent-key'},
        )
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body['mode'], 'agent')
        self.assertEqual(
            [item['event_type'] for item in body['items']],
            [EVENT_MENTION_CREATED, EVENT_DM_MESSAGE_DELETED],
        )
        self.assertNotIn(EVENT_CHANNEL_MESSAGE_CREATED, body['applied_types'])

        runtime_row = self.conn.execute(
            """
            SELECT last_event_cursor_seen, last_event_fetch_at
            FROM agent_runtime_state
            WHERE user_id = ?
            """,
            ('agent-a',),
        ).fetchone()
        self.assertIsNotNone(runtime_row)
        self.assertEqual(runtime_row['last_event_cursor_seen'], body['next_after_seq'])
        self.assertIsNotNone(runtime_row['last_event_fetch_at'])

    def test_agent_events_accepts_explicit_type_override(self) -> None:
        self.workspace_events.emit_event(
            event_type=EVENT_CHANNEL_MESSAGE_CREATED,
            actor_user_id='agent-b',
            target_user_id='agent-a',
            channel_id='general',
            visibility_scope='user',
            dedupe_key='agent-events:override-channel-created',
            payload={'message_id': 'CH-override-created', 'preview': 'override'},
        )

        response = self.client.get(
            '/api/v1/agents/me/events?types=channel.message.created',
            headers={'X-API-Key': 'agent-key'},
        )
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body['applied_types'], [EVENT_CHANNEL_MESSAGE_CREATED])
        self.assertEqual(
            [item['event_type'] for item in body['items']],
            [EVENT_CHANNEL_MESSAGE_CREATED],
        )

    def test_agent_events_respects_feed_only_permissions(self) -> None:
        self.workspace_events.emit_event(
            event_type=EVENT_MENTION_CREATED,
            actor_user_id='owner-user',
            target_user_id='agent-a',
            visibility_scope='user',
            dedupe_key='agent-events:feed-only:mention',
            payload={'mention_id': 'MN-feed-only', 'source_type': 'channel_message', 'source_id': 'msg-feed-only'},
        )
        self.workspace_events.emit_event(
            event_type=EVENT_DM_MESSAGE_DELETED,
            actor_user_id='agent-b',
            message_id='DM-feed-only',
            visibility_scope='dm',
            dedupe_key='agent-events:feed-only:dm-delete',
            payload={
                'preview': 'removed',
                'sender_id': 'agent-b',
                'recipient_id': 'agent-a',
                'group_id': None,
                'group_members': [],
            },
        )

        response = self.client.get(
            '/api/v1/agents/me/events',
            headers={'X-API-Key': 'agent-feed-only'},
        )
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(
            [item['event_type'] for item in body['items']],
            [EVENT_MENTION_CREATED],
        )

    def test_human_key_agent_events_does_not_create_agent_presence_or_runtime(self) -> None:
        self.workspace_events.emit_event(
            event_type=EVENT_MENTION_CREATED,
            actor_user_id='owner-user',
            target_user_id='observer',
            visibility_scope='user',
            dedupe_key='agent-events:human:mention',
            payload={'mention_id': 'MN-human', 'source_type': 'channel_message', 'source_id': 'msg-human'},
        )

        response = self.client.get(
            '/api/v1/agents/me/events',
            headers={'X-API-Key': 'observer-key'},
        )
        self.assertEqual(response.status_code, 200)

        presence_table = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='agent_presence'"
        ).fetchone()
        if presence_table is not None:
            presence_row = self.conn.execute(
                "SELECT user_id, last_source FROM agent_presence WHERE user_id = ?",
                ('observer',),
            ).fetchone()
            self.assertIsNone(presence_row)

        runtime_table = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='agent_runtime_state'"
        ).fetchone()
        if runtime_table is not None:
            runtime_row = self.conn.execute(
                "SELECT user_id FROM agent_runtime_state WHERE user_id = ?",
                ('observer',),
            ).fetchone()
            self.assertIsNone(runtime_row)

    def test_inbound_dm_finalize_uses_canonical_message_id_for_created_event(self) -> None:
        msg = self.message_manager.create_message(
            sender_id='agent-b',
            content='remote hello',
            recipient_id='agent-a',
            message_type=MessageType.TEXT,
            metadata={'origin_peer': 'peer-remote'},
        )
        self.assertIsNotNone(msg)
        assert msg is not None
        original_id = msg.id

        ok = _finalize_inbound_dm_message(
            self.db_manager,
            self.message_manager,
            msg,
            'DM-canonical-1',
        )
        self.assertTrue(ok)
        self.assertEqual(msg.id, 'DM-canonical-1')

        row = self.conn.execute(
            "SELECT id, delivered_at FROM messages WHERE id = ?",
            ('DM-canonical-1',),
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertIsNotNone(row['delivered_at'])
        old_row = self.conn.execute(
            "SELECT id FROM messages WHERE id = ?",
            (original_id,),
        ).fetchone()
        self.assertIsNone(old_row)

        body = self.client.get(
            '/api/v1/events',
            headers={'X-API-Key': 'agent-key'},
        ).get_json()
        created_items = [item for item in body['items'] if item['event_type'] == EVENT_DM_MESSAGE_CREATED]
        self.assertEqual(len(created_items), 1)
        self.assertEqual(created_items[0]['message_id'], 'DM-canonical-1')

    def test_inbound_dm_delete_emits_one_deleted_event(self) -> None:
        self.conn.execute(
            """
            INSERT INTO messages (
                id, sender_id, recipient_id, content, message_type,
                status, created_at, delivered_at, read_at, edited_at, metadata
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                'DM-delete-1',
                'agent-b',
                'agent-a',
                'remote body',
                'text',
                'delivered',
                '2026-03-09T10:00:00+00:00',
                '2026-03-09T10:00:01+00:00',
                None,
                None,
                json.dumps({'origin_peer': 'peer-remote'}),
            ),
        )
        self.conn.commit()

        removed = _apply_inbound_dm_delete(
            self.db_manager,
            self.message_manager,
            MagicMock(),
            'DM-delete-1',
        )
        self.assertTrue(removed)
        row = self.conn.execute(
            "SELECT id FROM messages WHERE id = ?",
            ('DM-delete-1',),
        ).fetchone()
        self.assertIsNone(row)

        body = self.client.get(
            '/api/v1/events',
            headers={'X-API-Key': 'agent-key'},
        ).get_json()
        deleted_items = [item for item in body['items'] if item['event_type'] == EVENT_DM_MESSAGE_DELETED]
        self.assertEqual(len(deleted_items), 1)
        self.assertEqual(deleted_items[0]['message_id'], 'DM-delete-1')

    def test_mark_message_read_emits_one_read_event(self) -> None:
        self.conn.execute(
            """
            INSERT INTO messages (
                id, sender_id, recipient_id, content, message_type,
                status, created_at, delivered_at, read_at, edited_at, metadata
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                'DM-read-1',
                'agent-b',
                'agent-a',
                'needs read',
                'text',
                'delivered',
                '2026-03-09T10:00:00+00:00',
                '2026-03-09T10:00:01+00:00',
                None,
                None,
                json.dumps({'origin_peer': 'peer-remote'}),
            ),
        )
        self.conn.commit()

        first = self.message_manager.mark_message_read('DM-read-1', 'agent-a')
        second = self.message_manager.mark_message_read('DM-read-1', 'agent-a')

        self.assertTrue(first)
        self.assertFalse(second)

        body = self.client.get(
            '/api/v1/events?types=dm.message.read',
            headers={'X-API-Key': 'agent-key'},
        ).get_json()
        read_items = [item for item in body['items'] if item['event_type'] == EVENT_DM_MESSAGE_READ]
        self.assertEqual(len(read_items), 1)
        self.assertEqual(read_items[0]['message_id'], 'DM-read-1')

    def test_user_scoped_channel_events_are_visible_to_target_user(self) -> None:
        self.workspace_events.emit_event(
            event_type=EVENT_CHANNEL_MESSAGE_CREATED,
            actor_user_id='agent-b',
            target_user_id='agent-a',
            channel_id='general',
            visibility_scope='user',
            dedupe_key='channel:user:created:1',
            payload={'message_id': 'CH-1', 'preview': 'hello'},
        )
        self.workspace_events.emit_event(
            event_type=EVENT_CHANNEL_MESSAGE_READ,
            actor_user_id='agent-a',
            target_user_id='agent-a',
            channel_id='general',
            visibility_scope='user',
            dedupe_key='channel:user:read:1',
            payload={'reason': 'channel_read'},
        )
        self.workspace_events.emit_event(
            event_type=EVENT_CHANNEL_STATE_UPDATED,
            actor_user_id='owner-user',
            target_user_id='agent-a',
            channel_id='general',
            visibility_scope='user',
            dedupe_key='channel:user:state:1',
            payload={'reason': 'member_added'},
        )

        body = self.client.get(
            '/api/v1/events?types=channel.message.created&types=channel.message.read&types=channel.state.updated',
            headers={'X-API-Key': 'agent-key'},
        ).get_json()

        items = body['items']
        self.assertEqual(len(items), 3)
        event_types = [item['event_type'] for item in items]
        self.assertIn(EVENT_CHANNEL_MESSAGE_CREATED, event_types)
        self.assertIn(EVENT_CHANNEL_MESSAGE_READ, event_types)
        self.assertIn(EVENT_CHANNEL_STATE_UPDATED, event_types)
        previews = [item.get('payload', {}).get('preview') for item in items]
        self.assertIn('hello', previews)

    def test_attachment_available_is_dm_only_in_patch1(self) -> None:
        self.conn.execute(
            """
            INSERT INTO messages (
                id, sender_id, recipient_id, content, message_type,
                status, created_at, delivered_at, read_at, edited_at, metadata
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                'DM-attachment-1',
                'agent-b',
                'agent-a',
                '',
                'file',
                'delivered',
                '2026-03-09T10:00:00+00:00',
                '2026-03-09T10:00:01+00:00',
                None,
                None,
                json.dumps({'attachments': [{'id': 'F1'}]}),
            ),
        )
        self.conn.commit()

        self.workspace_events.emit_event(
            event_type=EVENT_ATTACHMENT_AVAILABLE,
            actor_user_id=None,
            target_user_id=None,
            message_id='DM-attachment-1',
            visibility_scope='dm',
            dedupe_key='attachment:dm:1',
            payload={'local_file_id': 'F1'},
        )
        self.workspace_events.emit_event(
            event_type=EVENT_ATTACHMENT_AVAILABLE,
            actor_user_id=None,
            target_user_id=None,
            channel_id='C-channel',
            visibility_scope='channel',
            dedupe_key='attachment:channel:1',
            payload={'local_file_id': 'F2'},
        )

        body = self.client.get(
            '/api/v1/events?types=attachment.available',
            headers={'X-API-Key': 'agent-key'},
        ).get_json()
        self.assertEqual(len(body['items']), 1)
        self.assertEqual(body['items'][0]['event_type'], EVENT_ATTACHMENT_AVAILABLE)
        self.assertEqual(body['items'][0]['message_id'], 'DM-attachment-1')

    def test_cursor_advances_without_skipping_visible_rows_behind_hidden_rows(self) -> None:
        self.workspace_events.emit_event(
            event_type=EVENT_MENTION_CREATED,
            actor_user_id='owner-user',
            target_user_id='agent-a',
            visibility_scope='user',
            dedupe_key='cursor:visible:1',
            payload={'mention_id': 'MN-1', 'source_type': 'channel_message', 'source_id': 'msg-1'},
        )
        self.workspace_events.emit_event(
            event_type=EVENT_DM_MESSAGE_DELETED,
            actor_user_id='agent-b',
            message_id='DM-hidden',
            visibility_scope='dm',
            dedupe_key='cursor:hidden',
            payload={
                'preview': 'not for agent-a',
                'sender_id': 'agent-b',
                'recipient_id': 'observer',
                'group_members': [],
            },
        )
        self.workspace_events.emit_event(
            event_type=EVENT_MENTION_CREATED,
            actor_user_id='owner-user',
            target_user_id='agent-a',
            visibility_scope='user',
            dedupe_key='cursor:visible:2',
            payload={'mention_id': 'MN-2', 'source_type': 'channel_message', 'source_id': 'msg-2'},
        )

        first_page = self.workspace_events.list_events_for_user(
            user_id='agent-a',
            after_seq=0,
            limit=1,
            can_read_messages=True,
        )
        self.assertEqual(len(first_page['items']), 1)
        self.assertEqual(first_page['items'][0]['payload']['mention_id'], 'MN-1')
        first_cursor = first_page['next_after_seq']
        self.assertEqual(first_cursor, first_page['items'][0]['seq'])

        second_page = self.workspace_events.list_events_for_user(
            user_id='agent-a',
            after_seq=first_cursor,
            limit=1,
            can_read_messages=True,
        )
        self.assertEqual(len(second_page['items']), 1)
        self.assertEqual(second_page['items'][0]['payload']['mention_id'], 'MN-2')
        self.assertGreater(second_page['next_after_seq'], first_cursor)


if __name__ == '__main__':
    unittest.main()
