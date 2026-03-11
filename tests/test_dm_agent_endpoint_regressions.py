"""Regression tests for DM agent-facing REST contracts and inbox integration."""

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

from canopy.api.routes import create_api_blueprint
from canopy.core.inbox import InboxManager
from canopy.core.messaging import MessageManager
from canopy.security.api_keys import ApiKeyInfo, Permission


class _FakeDbManager:
    def __init__(self, conn: sqlite3.Connection, db_path: Path) -> None:
        self._conn = conn
        self.db_path = db_path

    def get_connection(self) -> sqlite3.Connection:
        return self._conn

    def get_user(self, user_id: str):
        row = self._conn.execute(
            "SELECT * FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
        return dict(row) if row else None

    def store_message(self, message_id, sender_id, recipient_id, content, message_type, metadata):
        created_at = datetime.now(timezone.utc).isoformat()
        self._conn.execute(
            """
            INSERT INTO messages (
                id, sender_id, recipient_id, content, message_type, status,
                created_at, delivered_at, read_at, edited_at, metadata
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
    def __init__(self, key_to_user: dict[str, str]) -> None:
        self._key_to_user = key_to_user

    def validate_key(self, raw_key: str, required_permission=None):
        user_id = self._key_to_user.get(raw_key)
        if not user_id:
            return None
        perms = {
            Permission.READ_FEED,
            Permission.WRITE_FEED,
            Permission.READ_MESSAGES,
            Permission.WRITE_MESSAGES,
        }
        if required_permission and required_permission not in perms:
            return None
        return ApiKeyInfo(
            id=f"key-{user_id}",
            user_id=user_id,
            key_hash="hash",
            permissions=perms,
            created_at=datetime.now(timezone.utc),
        )


class _FakeP2PManager:
    def __init__(self) -> None:
        self.direct_messages: list[dict] = []
        self.delete_signals: list[dict] = []

    def get_peer_id(self) -> str:
        return 'peer-local'

    def is_running(self) -> bool:
        return True

    def broadcast_direct_message(self, **kwargs) -> None:
        self.direct_messages.append(dict(kwargs))

    def broadcast_delete_signal(self, **kwargs) -> None:
        self.delete_signals.append(dict(kwargs))

    def describe_direct_message_security(self, recipient_ids):
        recipients = list(recipient_ids or [])
        if recipients == ['agent-local']:
            return {
                'mode': 'local_only',
                'state': 'local_only',
                'label': 'Local only',
                'relay_confidential': True,
                'local_only': True,
            }
        if 'remote-shadow' in recipients and 'agent-local' in recipients:
            return {
                'mode': 'mixed',
                'state': 'mixed',
                'label': 'Mixed delivery',
                'relay_confidential': False,
                'warning': 'Some recipients require legacy/plaintext mesh delivery',
            }
        return {
            'mode': 'legacy_plaintext',
            'state': 'plaintext',
            'label': 'Legacy relay/plaintext',
            'relay_confidential': False,
        }


class TestDmAgentEndpointRegressions(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)

        self.db_file = Path(self.tempdir.name) / 'dm_agent_contracts.db'
        self.conn = sqlite3.connect(str(self.db_file))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            """
            CREATE TABLE users (
                id TEXT PRIMARY KEY,
                username TEXT,
                display_name TEXT,
                public_key TEXT,
                password_hash TEXT,
                account_type TEXT,
                status TEXT,
                origin_peer TEXT,
                bio TEXT,
                created_at TEXT
            );
            CREATE TABLE messages (
                id TEXT PRIMARY KEY,
                sender_id TEXT NOT NULL,
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
            """
        )
        self.conn.executemany(
            """
            INSERT INTO users (
                id, username, display_name, public_key, password_hash,
                account_type, status, origin_peer, bio, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    'author',
                    'author',
                    'Author',
                    'pk-author',
                    'pw-author',
                    'human',
                    'active',
                    None,
                    'local author',
                    '2026-03-07T08:00:00+00:00',
                ),
                (
                    'agent-local',
                    'agent_local',
                    'Agent Local',
                    'pk-agent',
                    'pw-agent',
                    'agent',
                    'active',
                    None,
                    'local agent recipient',
                    '2026-03-07T08:01:00+00:00',
                ),
                (
                    'remote-shadow',
                    'peer-remote-shadow',
                    'Remote Shadow',
                    'pk-shadow',
                    'pw-shadow',
                    'agent',
                    'active',
                    'peer-remote',
                    'remote placeholder',
                    '2026-03-07T08:02:00+00:00',
                ),
            ],
        )
        self.conn.commit()

        self.db_manager = _FakeDbManager(self.conn, self.db_file)
        self.api_key_manager = _FakeApiKeyManager(
            {
                'key-author': 'author',
                'key-agent-local': 'agent-local',
            }
        )
        self.message_manager = MessageManager(self.db_manager, self.api_key_manager)
        self.inbox_manager = InboxManager(self.db_manager)
        for user_id in ('author', 'agent-local'):
            self.inbox_manager.set_config(
                user_id,
                {
                    'cooldown_seconds': 0,
                    'sender_cooldown_seconds': 0,
                    'agent_sender_cooldown_seconds': 0,
                    'channel_burst_limit': 100,
                    'channel_hourly_limit': 1000,
                    'sender_hourly_limit': 1000,
                },
            )

        self.p2p_manager = _FakeP2PManager()
        self.profile_manager = MagicMock()
        self.profile_manager.get_profile.return_value = None
        self.channel_manager = MagicMock()
        self.channel_manager.get_channel_activity_since.return_value = []
        self.feed_manager = MagicMock()
        self.feed_manager.get_posts_since.return_value = []

        components = (
            self.db_manager,
            self.api_key_manager,
            MagicMock(),
            self.message_manager,
            self.channel_manager,
            MagicMock(),
            self.feed_manager,
            MagicMock(),
            self.profile_manager,
            MagicMock(),
            self.p2p_manager,
        )

        self.get_components_patcher = patch(
            'canopy.api.routes.get_app_components',
            return_value=components,
        )
        self.get_components_patcher.start()
        self.addCleanup(self.get_components_patcher.stop)

        self.heartbeat_patcher = patch(
            'canopy.api.routes.build_agent_heartbeat_snapshot',
            return_value={'needs_action': False, 'pending_inbox': 0},
        )
        self.heartbeat_patcher.start()
        self.addCleanup(self.heartbeat_patcher.stop)

        self.actionable_patcher = patch(
            'canopy.api.routes.build_actionable_work_preview',
            return_value=[],
        )
        self.actionable_patcher.start()
        self.addCleanup(self.actionable_patcher.stop)

        app = Flask(__name__)
        app.config['TESTING'] = True
        app.secret_key = 'test-secret'
        app.config['INBOX_MANAGER'] = self.inbox_manager
        api_bp = create_api_blueprint()
        app.register_blueprint(api_bp, url_prefix='/api/v1')
        self.client = app.test_client()

    def tearDown(self) -> None:
        self.conn.close()

    def _headers(self, key: str) -> dict[str, str]:
        return {
            'X-API-Key': key,
            'Content-Type': 'application/json',
        }

    def test_group_dm_send_update_read_search_and_catchup_contract(self) -> None:
        send_resp = self.client.post(
            '/api/v1/messages',
            json={
                'content': 'Initial group DM',
                'recipient_ids': ['agent-local', 'remote-shadow'],
                'reply_to': 'DM-root',
                'attachments': [{'id': 'Fdemo', 'name': 'demo.txt', 'type': 'text/plain'}],
            },
            headers=self._headers('key-author'),
        )
        self.assertEqual(send_resp.status_code, 201)
        send_payload = send_resp.get_json() or {}
        message = send_payload.get('message') or {}
        message_id = message.get('id')
        group_id = send_payload.get('group_id')
        self.assertTrue(message_id)
        self.assertTrue(group_id)
        self.assertEqual(len(self.p2p_manager.direct_messages), 2)
        self.assertEqual(
            sorted(item['recipient_id'] for item in self.p2p_manager.direct_messages),
            ['agent-local', 'remote-shadow'],
        )

        inbox_rows = self.conn.execute(
            "SELECT agent_user_id, payload_json FROM agent_inbox WHERE source_id = ? ORDER BY agent_user_id",
            (message_id,),
        ).fetchall()
        self.assertEqual([row['agent_user_id'] for row in inbox_rows], ['agent-local'])
        payload = json.loads(inbox_rows[0]['payload_json'])
        self.assertEqual(payload.get('content'), 'Initial group DM')
        self.assertEqual(payload.get('reply_to'), 'DM-root')
        self.assertEqual(payload.get('group_id'), group_id)
        self.assertEqual(
            payload.get('group_members'),
            ['agent-local', 'author', 'remote-shadow'],
        )
        self.assertEqual(len(payload.get('attachments') or []), 1)
        self.assertEqual((payload.get('security') or {}).get('mode'), 'mixed')

        self.p2p_manager.direct_messages.clear()
        update_resp = self.client.patch(
            f'/api/v1/messages/{message_id}',
            json={'content': 'Edited group DM'},
            headers=self._headers('key-author'),
        )
        self.assertEqual(update_resp.status_code, 200)
        self.assertEqual(len(self.p2p_manager.direct_messages), 2)
        self.assertEqual(
            sorted(item['recipient_id'] for item in self.p2p_manager.direct_messages),
            ['agent-local', 'remote-shadow'],
        )

        inbox_row = self.conn.execute(
            "SELECT payload_json FROM agent_inbox WHERE source_id = ? AND agent_user_id = ?",
            (message_id, 'agent-local'),
        ).fetchone()
        self.assertIsNotNone(inbox_row)
        updated_payload = json.loads(inbox_row['payload_json'])
        self.assertEqual(updated_payload.get('content'), 'Edited group DM')
        self.assertEqual(updated_payload.get('group_id'), group_id)
        self.assertEqual(updated_payload.get('reply_to'), 'DM-root')
        self.assertIsNotNone(updated_payload.get('edited_at'))
        self.assertEqual(len(updated_payload.get('attachments') or []), 1)
        self.assertEqual((updated_payload.get('security') or {}).get('mode'), 'mixed')

        list_resp = self.client.get(
            '/api/v1/messages',
            headers=self._headers('key-agent-local'),
        )
        self.assertEqual(list_resp.status_code, 200)
        listed_ids = [item['id'] for item in (list_resp.get_json() or {}).get('messages') or []]
        self.assertIn(message_id, listed_ids)

        conv_resp = self.client.get(
            f'/api/v1/messages/conversation/group/{group_id}',
            headers=self._headers('key-agent-local'),
        )
        self.assertEqual(conv_resp.status_code, 200)
        self.assertEqual((conv_resp.get_json() or {}).get('count'), 1)

        read_resp = self.client.post(
            f'/api/v1/messages/{message_id}/read',
            headers=self._headers('key-agent-local'),
        )
        self.assertEqual(read_resp.status_code, 200)
        read_row = self.conn.execute(
            "SELECT read_at FROM messages WHERE id = ?",
            (message_id,),
        ).fetchone()
        self.assertIsNotNone(read_row['read_at'])

        search_resp = self.client.get(
            '/api/v1/messages/search?q=Edited',
            headers=self._headers('key-agent-local'),
        )
        self.assertEqual(search_resp.status_code, 200)
        search_ids = [item['id'] for item in (search_resp.get_json() or {}).get('messages') or []]
        self.assertIn(message_id, search_ids)

        catchup_resp = self.client.get(
            '/api/v1/agents/me/catchup?limit=10',
            headers=self._headers('key-agent-local'),
        )
        self.assertEqual(catchup_resp.status_code, 200)
        catchup_payload = catchup_resp.get_json() or {}
        dm_items = ((catchup_payload.get('messages') or {}).get('items')) or []
        dm_item = next((item for item in dm_items if item.get('message_id') == message_id), None)
        self.assertIsNotNone(dm_item)
        self.assertEqual(dm_item.get('group_id'), group_id)
        self.assertEqual(dm_item.get('reply_to'), 'DM-root')
        self.assertEqual(dm_item.get('attachments_count'), 1)
        self.assertEqual(
            dm_item.get('group_members'),
            ['agent-local', 'author', 'remote-shadow'],
        )
        self.assertIsNotNone(dm_item.get('edited_at'))

    def test_dm_inbox_exposes_reply_target_and_reply_endpoint_keeps_response_in_dm(self) -> None:
        send_resp = self.client.post(
            '/api/v1/messages',
            json={
                'content': 'Need help with this DM',
                'recipient_id': 'agent-local',
            },
            headers=self._headers('key-author'),
        )
        self.assertEqual(send_resp.status_code, 201)
        original_message_id = (send_resp.get_json() or {}).get('message', {}).get('id')
        self.assertTrue(original_message_id)

        inbox_resp = self.client.get(
            '/api/v1/agents/me/inbox?limit=5',
            headers=self._headers('key-agent-local'),
        )
        self.assertEqual(inbox_resp.status_code, 200)
        inbox_items = (inbox_resp.get_json() or {}).get('items') or []
        dm_item = next((item for item in inbox_items if item.get('message_id') == original_message_id), None)
        self.assertIsNotNone(dm_item)
        self.assertEqual(dm_item.get('trigger_type'), 'dm')
        self.assertEqual(dm_item.get('sender_user_id'), 'author')
        self.assertEqual(dm_item.get('dm_thread_id'), 'author')
        self.assertEqual(dm_item.get('reply_endpoint'), '/api/v1/messages/reply')
        self.assertEqual((dm_item.get('payload') or {}).get('sender_user_id'), 'author')
        self.assertEqual((dm_item.get('payload') or {}).get('dm_thread_id'), 'author')

        self.p2p_manager.direct_messages.clear()
        reply_resp = self.client.post(
            '/api/v1/messages/reply',
            json={
                'message_id': original_message_id,
                'content': 'Replying in the DM thread',
            },
            headers=self._headers('key-agent-local'),
        )
        self.assertEqual(reply_resp.status_code, 201)
        reply_payload = reply_resp.get_json() or {}
        reply_message = reply_payload.get('message') or {}
        self.assertEqual(reply_payload.get('reply_to'), original_message_id)
        self.assertEqual(reply_message.get('recipient_id'), 'author')
        self.assertEqual((reply_message.get('metadata') or {}).get('reply_to'), original_message_id)
        self.assertEqual(len(self.p2p_manager.direct_messages), 1)
        self.assertEqual(self.p2p_manager.direct_messages[0]['recipient_id'], 'author')
        self.channel_manager.send_message.assert_not_called()

    def test_inbox_skip_can_persist_completion_ref_evidence(self) -> None:
        send_resp = self.client.post(
            '/api/v1/messages',
            json={
                'content': 'This item will be skipped with evidence',
                'recipient_id': 'agent-local',
            },
            headers=self._headers('key-author'),
        )
        self.assertEqual(send_resp.status_code, 201)
        message_id = (send_resp.get_json() or {}).get('message', {}).get('id')
        self.assertTrue(message_id)

        inbox_row = self.conn.execute(
            """
            SELECT id
            FROM agent_inbox
            WHERE agent_user_id = ? AND source_id = ?
            """,
            ('agent-local', message_id),
        ).fetchone()
        self.assertIsNotNone(inbox_row)

        patch_resp = self.client.patch(
            '/api/v1/agents/me/inbox',
            json={
                'ids': [inbox_row['id']],
                'status': 'skipped',
                'completion_ref': {
                    'source_type': 'feed_post',
                    'source_id': 'post-skip-1',
                    'note': 'Duplicate request already addressed elsewhere.',
                },
            },
            headers=self._headers('key-agent-local'),
        )
        self.assertEqual(patch_resp.status_code, 200)
        self.assertEqual((patch_resp.get_json() or {}).get('updated'), 1)

        refreshed_resp = self.client.get(
            '/api/v1/agents/me/inbox?status=skipped&limit=5',
            headers=self._headers('key-agent-local'),
        )
        self.assertEqual(refreshed_resp.status_code, 200)
        refreshed_items = (refreshed_resp.get_json() or {}).get('items') or []
        skipped_item = next((item for item in refreshed_items if item.get('id') == inbox_row['id']), None)
        self.assertIsNotNone(skipped_item)
        self.assertEqual(skipped_item.get('status'), 'skipped')
        self.assertEqual(
            skipped_item.get('completion_ref'),
            {
                'source_type': 'feed_post',
                'source_id': 'post-skip-1',
                'note': 'Duplicate request already addressed elsewhere.',
            },
        )

    def test_default_inbox_endpoints_keep_seen_items_actionable(self) -> None:
        send_resp = self.client.post(
            '/api/v1/messages',
            json={
                'content': 'This item will be seen but remain actionable',
                'recipient_id': 'agent-local',
            },
            headers=self._headers('key-author'),
        )
        self.assertEqual(send_resp.status_code, 201)
        message_id = (send_resp.get_json() or {}).get('message', {}).get('id')
        self.assertTrue(message_id)

        inbox_row = self.conn.execute(
            """
            SELECT id
            FROM agent_inbox
            WHERE agent_user_id = ? AND source_id = ?
            """,
            ('agent-local', message_id),
        ).fetchone()
        self.assertIsNotNone(inbox_row)

        patch_resp = self.client.patch(
            '/api/v1/agents/me/inbox',
            json={
                'ids': [inbox_row['id']],
                'status': 'seen',
            },
            headers=self._headers('key-agent-local'),
        )
        self.assertEqual(patch_resp.status_code, 200)
        self.assertEqual((patch_resp.get_json() or {}).get('updated'), 1)

        list_resp = self.client.get(
            '/api/v1/agents/me/inbox?limit=10',
            headers=self._headers('key-agent-local'),
        )
        self.assertEqual(list_resp.status_code, 200)
        items = (list_resp.get_json() or {}).get('items') or []
        seen_item = next((item for item in items if item.get('id') == inbox_row['id']), None)
        self.assertIsNotNone(seen_item)
        self.assertEqual(seen_item.get('status'), 'seen')

        count_resp = self.client.get(
            '/api/v1/agents/me/inbox/count',
            headers=self._headers('key-agent-local'),
        )
        self.assertEqual(count_resp.status_code, 200)
        self.assertGreaterEqual(int((count_resp.get_json() or {}).get('count') or 0), 1)

    def test_agent_dm_followups_are_not_dropped_by_persisted_cooldown_config(self) -> None:
        self.inbox_manager.set_config(
            'agent-local',
            {
                'cooldown_seconds': 10,
                'sender_cooldown_seconds': 30,
                'agent_sender_cooldown_seconds': 60,
            },
        )

        first_resp = self.client.post(
            '/api/v1/messages',
            json={
                'content': 'First DM',
                'recipient_id': 'agent-local',
            },
            headers=self._headers('key-author'),
        )
        self.assertEqual(first_resp.status_code, 201)
        first_id = (first_resp.get_json() or {}).get('message', {}).get('id')
        self.assertTrue(first_id)

        second_resp = self.client.post(
            '/api/v1/messages',
            json={
                'content': 'Second DM right away',
                'recipient_id': 'agent-local',
            },
            headers=self._headers('key-author'),
        )
        self.assertEqual(second_resp.status_code, 201)
        second_id = (second_resp.get_json() or {}).get('message', {}).get('id')
        self.assertTrue(second_id)

        inbox_rows = self.conn.execute(
            """
            SELECT source_id, sender_user_id, trigger_type
            FROM agent_inbox
            WHERE agent_user_id = ?
            ORDER BY created_at ASC
            """,
            ('agent-local',),
        ).fetchall()
        self.assertEqual([row['source_id'] for row in inbox_rows], [first_id, second_id])
        self.assertTrue(all(row['sender_user_id'] == 'author' for row in inbox_rows))
        self.assertTrue(all(row['trigger_type'] == 'dm' for row in inbox_rows))

    def test_recipient_ids_ignores_sender_when_only_one_real_recipient(self) -> None:
        send_resp = self.client.post(
            '/api/v1/messages',
            json={
                'content': 'Single recipient via recipient_ids',
                'recipient_ids': ['author', 'agent-local'],
            },
            headers=self._headers('key-author'),
        )
        self.assertEqual(send_resp.status_code, 201)
        payload = send_resp.get_json() or {}
        self.assertIsNone(payload.get('group_id'))

        message = payload.get('message') or {}
        self.assertEqual(message.get('recipient_id'), 'agent-local')
        self.assertEqual(len(self.p2p_manager.direct_messages), 1)
        self.assertEqual(self.p2p_manager.direct_messages[0]['recipient_id'], 'agent-local')
        self.assertEqual(
            (self.p2p_manager.direct_messages[0].get('metadata', {}).get('security') or {}).get('mode'),
            'local_only',
        )

    def test_delete_message_clears_local_dm_inbox_and_uses_direct_message_signal(self) -> None:
        send_resp = self.client.post(
            '/api/v1/messages',
            json={
                'content': 'Delete me',
                'recipient_id': 'agent-local',
            },
            headers=self._headers('key-author'),
        )
        self.assertEqual(send_resp.status_code, 201)
        message_id = (send_resp.get_json() or {}).get('message', {}).get('id')
        self.assertTrue(message_id)

        inbox_before = self.conn.execute(
            "SELECT COUNT(*) AS n FROM agent_inbox WHERE source_id = ? AND trigger_type = 'dm'",
            (message_id,),
        ).fetchone()
        self.assertEqual(inbox_before['n'], 1)

        delete_resp = self.client.delete(
            f'/api/v1/messages/{message_id}',
            headers=self._headers('key-author'),
        )
        self.assertEqual(delete_resp.status_code, 200)
        self.assertEqual(len(self.p2p_manager.delete_signals), 1)
        self.assertEqual(self.p2p_manager.delete_signals[0]['data_type'], 'direct_message')
        self.assertEqual(self.p2p_manager.delete_signals[0]['data_id'], message_id)

        row = self.conn.execute(
            "SELECT 1 FROM messages WHERE id = ?",
            (message_id,),
        ).fetchone()
        self.assertIsNone(row)

        inbox_after = self.conn.execute(
            "SELECT COUNT(*) AS n FROM agent_inbox WHERE source_id = ? AND trigger_type = 'dm'",
            (message_id,),
        ).fetchone()
        self.assertEqual(inbox_after['n'], 0)


class TestInboxStateMachineEdgeCases(unittest.TestCase):
    """Regression tests for inbox state-machine edge cases and correctness fixes."""

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)

        self.db_file = Path(self.tempdir.name) / 'inbox_state_machine.db'
        self.conn = sqlite3.connect(str(self.db_file))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            """
            CREATE TABLE users (
                id TEXT PRIMARY KEY,
                username TEXT,
                display_name TEXT,
                public_key TEXT,
                password_hash TEXT,
                account_type TEXT,
                status TEXT,
                origin_peer TEXT,
                bio TEXT,
                created_at TEXT
            );
            """
        )
        self.conn.executemany(
            """
            INSERT INTO users (
                id, username, display_name, public_key, password_hash,
                account_type, status, origin_peer, bio, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    'agent-test',
                    'agent_test',
                    'Agent Test',
                    'pk-agent',
                    'pw-agent',
                    'agent',
                    'active',
                    None,
                    'test agent',
                    '2026-03-07T08:00:00+00:00',
                ),
            ],
        )
        self.conn.commit()

        self.db_manager = _FakeDbManager(self.conn, self.db_file)
        self.inbox = InboxManager(self.db_manager)
        # Disable rate-limiting for tests
        self.inbox.set_config(
            'agent-test',
            {
                'cooldown_seconds': 0,
                'sender_cooldown_seconds': 0,
                'agent_sender_cooldown_seconds': 0,
                'channel_burst_limit': 1000,
                'channel_hourly_limit': 10000,
                'sender_hourly_limit': 10000,
            },
        )

    def tearDown(self) -> None:
        self.conn.close()

    def _create_item(self, source_id: str = 'msg-1') -> str:
        inbox_id = self.inbox.create_trigger(
            agent_user_id='agent-test',
            source_type='dm',
            source_id=source_id,
            sender_user_id='sender-1',
            trigger_type='dm',
        )
        self.assertIsNotNone(inbox_id)
        return inbox_id

    def _row(self, inbox_id: str) -> sqlite3.Row:
        return self.conn.execute(
            "SELECT * FROM agent_inbox WHERE id = ?",
            (inbox_id,),
        ).fetchone()

    def test_seen_after_complete_clears_completion_metadata(self) -> None:
        """Transitioning completed -> seen must clear completed_at and completion_ref_json.

        Before the fix, completed_at and completion_ref survived the seen
        transition, producing misleading timestamps and phantom evidence links
        on in-progress items.
        """
        inbox_id = self._create_item('msg-seen-after-complete')

        # Complete with evidence
        updated = self.inbox.update_items(
            user_id='agent-test',
            ids=[inbox_id],
            status='completed',
            completion_ref={'source_id': 'post-1', 'note': 'done'},
        )
        self.assertEqual(updated, 1)
        row = self._row(inbox_id)
        self.assertEqual(row['status'], 'completed')
        self.assertIsNotNone(row['completed_at'])
        self.assertIsNotNone(row['completion_ref_json'])

        # Re-open for review (seen)
        updated = self.inbox.update_items(
            user_id='agent-test',
            ids=[inbox_id],
            status='seen',
        )
        self.assertEqual(updated, 1)
        row = self._row(inbox_id)
        self.assertEqual(row['status'], 'seen')
        # Stale finalization data must be cleared
        self.assertIsNone(row['completed_at'], "completed_at must be cleared when transitioning to 'seen'")
        self.assertIsNone(row['completion_ref_json'], "completion_ref_json must be cleared when transitioning to 'seen'")
        # seen_at should be set
        self.assertIsNotNone(row['seen_at'])

    def test_seen_after_skipped_clears_completion_metadata(self) -> None:
        """Transitioning skipped -> seen must also clear completion metadata."""
        inbox_id = self._create_item('msg-seen-after-skipped')

        self.inbox.update_items(
            user_id='agent-test',
            ids=[inbox_id],
            status='skipped',
            completion_ref={'note': 'duplicate'},
        )
        row = self._row(inbox_id)
        self.assertEqual(row['status'], 'skipped')
        self.assertIsNotNone(row['completed_at'])
        self.assertIsNotNone(row['completion_ref_json'])

        self.inbox.update_items(user_id='agent-test', ids=[inbox_id], status='seen')
        row = self._row(inbox_id)
        self.assertEqual(row['status'], 'seen')
        self.assertIsNone(row['completed_at'])
        self.assertIsNone(row['completion_ref_json'])

    def test_pending_reset_clears_completion_state_preserves_seen_at(self) -> None:
        """pending reset clears handled_at, completed_at, completion_ref but preserves seen_at."""
        inbox_id = self._create_item('msg-pending-reset')

        # Mark seen first
        self.inbox.update_items(user_id='agent-test', ids=[inbox_id], status='seen')
        seen_row = self._row(inbox_id)
        seen_at_value = seen_row['seen_at']
        self.assertIsNotNone(seen_at_value)

        # Complete with evidence
        self.inbox.update_items(
            user_id='agent-test',
            ids=[inbox_id],
            status='completed',
            completion_ref={'note': 'initial resolution'},
        )
        self.assertEqual(self._row(inbox_id)['status'], 'completed')

        # Reset to pending
        updated = self.inbox.update_items(user_id='agent-test', ids=[inbox_id], status='pending')
        self.assertEqual(updated, 1)
        row = self._row(inbox_id)
        self.assertEqual(row['status'], 'pending')
        self.assertIsNone(row['handled_at'], "handled_at must be cleared on pending reset")
        self.assertIsNone(row['completed_at'], "completed_at must be cleared on pending reset")
        self.assertIsNone(row['completion_ref_json'], "completion_ref_json must be cleared on pending reset")
        # seen_at is preserved (item was acknowledged before)
        self.assertEqual(row['seen_at'], seen_at_value, "seen_at should survive a pending reset")
        self.assertEqual(row['last_resolution_status'], 'completed')
        self.assertIsNotNone(row['last_resolution_at'])
        self.assertIsNotNone(row['last_completion_ref_json'])

    def test_reopen_preserves_last_resolution_evidence(self) -> None:
        """Reopening an item must retain prior terminal-state evidence in the audit trail."""
        inbox_id = self._create_item('msg-reopen-audit')

        self.inbox.update_items(
            user_id='agent-test',
            ids=[inbox_id],
            status='skipped',
            completion_ref={'reason': 'duplicate', 'message_id': 'msg-dup-1'},
        )
        self.inbox.update_items(user_id='agent-test', ids=[inbox_id], status='seen')

        row = self._row(inbox_id)
        self.assertEqual(row['status'], 'seen')
        self.assertEqual(row['last_resolution_status'], 'skipped')
        self.assertIsNotNone(row['last_resolution_at'])
        self.assertIsNone(row['completion_ref_json'])
        self.assertIsNotNone(row['last_completion_ref_json'])
        last_ref = json.loads(row['last_completion_ref_json'])
        self.assertEqual(last_ref['reason'], 'duplicate')
        self.assertEqual(last_ref['message_id'], 'msg-dup-1')

    def test_default_actionable_list_and_count_include_seen(self) -> None:
        """Default inbox list/count should include seen items because they remain actionable."""
        pending_id = self._create_item('msg-actionable-pending')
        seen_id = self._create_item('msg-actionable-seen')

        self.inbox.update_items(user_id='agent-test', ids=[seen_id], status='seen')
        self.inbox.update_items(user_id='agent-test', ids=[pending_id], status='completed')

        count = self.inbox.count_items(user_id='agent-test')
        self.assertEqual(count, 1)

        items = self.inbox.list_items(user_id='agent-test', include_handled=False)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['id'], seen_id)
        self.assertEqual(items[0]['status'], 'seen')

    def test_repeated_completion_with_new_ref_updates_evidence(self) -> None:
        """Re-completing an already-completed item with a new ref must overwrite evidence."""
        inbox_id = self._create_item('msg-repeated-complete')

        self.inbox.update_items(
            user_id='agent-test',
            ids=[inbox_id],
            status='completed',
            completion_ref={'source_id': 'post-old', 'note': 'first attempt'},
        )
        row = self._row(inbox_id)
        first_completed_at = row['completed_at']
        self.assertIsNotNone(first_completed_at)
        self.assertIn('post-old', row['completion_ref_json'])

        # Re-complete with updated evidence
        self.inbox.update_items(
            user_id='agent-test',
            ids=[inbox_id],
            status='completed',
            completion_ref={'source_id': 'post-new', 'note': 'revised'},
        )
        row = self._row(inbox_id)
        self.assertEqual(row['status'], 'completed')
        # completed_at should be preserved (original completion time)
        self.assertEqual(row['completed_at'], first_completed_at)
        # completion_ref must be updated to the new evidence
        ref = json.loads(row['completion_ref_json'])
        self.assertEqual(ref['source_id'], 'post-new')
        self.assertEqual(ref['note'], 'revised')

    def test_repeated_completion_without_new_ref_preserves_existing_evidence(self) -> None:
        """Re-completing without a new ref must not erase existing evidence."""
        inbox_id = self._create_item('msg-preserve-ref')

        self.inbox.update_items(
            user_id='agent-test',
            ids=[inbox_id],
            status='completed',
            completion_ref={'note': 'keeper'},
        )
        original_ref = self._row(inbox_id)['completion_ref_json']

        # Re-complete with no ref
        self.inbox.update_items(user_id='agent-test', ids=[inbox_id], status='completed')
        row = self._row(inbox_id)
        self.assertEqual(row['completion_ref_json'], original_ref, "existing evidence must survive re-completion without a new ref")

    def test_batch_update_partial_ids_returns_actual_updated_count(self) -> None:
        """Batch update with some nonexistent IDs returns only the count of rows actually changed."""
        id1 = self._create_item('msg-batch-1')
        id2 = self._create_item('msg-batch-2')

        updated = self.inbox.update_items(
            user_id='agent-test',
            ids=[id1, id2, 'nonexistent-inbox-id'],
            status='seen',
        )
        self.assertEqual(updated, 2, "should update exactly the 2 existing rows, not the phantom ID")
        self.assertEqual(self._row(id1)['status'], 'seen')
        self.assertEqual(self._row(id2)['status'], 'seen')

    def test_invalid_status_is_rejected_no_update_applied(self) -> None:
        """An unrecognised status string must return 0 and leave the item unchanged.

        Before the fix, _normalize_storage_status fell back to 'pending',
        which would silently reset items instead of rejecting the request.
        """
        inbox_id = self._create_item('msg-invalid-status')

        # Complete first so we can detect an accidental reset
        self.inbox.update_items(user_id='agent-test', ids=[inbox_id], status='completed')
        self.assertEqual(self._row(inbox_id)['status'], 'completed')

        updated = self.inbox.update_items(
            user_id='agent-test',
            ids=[inbox_id],
            status='bogus_status',
        )
        self.assertEqual(updated, 0, "unrecognised status must be rejected")
        # Item must remain completed, not silently reset to pending
        self.assertEqual(self._row(inbox_id)['status'], 'completed')

    def test_migration_backfill_converts_handled_to_completed(self) -> None:
        """_ensure_tables migration must convert legacy 'handled' rows to 'completed'."""
        import secrets as _secrets
        # Insert two legacy 'handled' rows directly so they pre-date the migration
        handled_id = f"INB{_secrets.token_hex(8)}"
        completed_id = f"INB{_secrets.token_hex(8)}"
        handled_at = '2026-01-01T12:00:00+00:00'
        self.conn.executemany(
            """
            INSERT INTO agent_inbox
            (id, agent_user_id, source_type, source_id, trigger_type,
             status, priority, created_at, handled_at, depth)
            VALUES (?, 'agent-test', 'dm', ?, 'dm', ?, 'normal', ?, ?, 0)
            """,
            [
                (handled_id, f'src-handled-{handled_id}', 'handled', '2026-01-01T11:00:00+00:00', handled_at),
                (completed_id, f'src-completed-{completed_id}', 'completed', '2026-01-01T11:00:00+00:00', None),
            ],
        )
        self.conn.commit()

        # Re-run the migration by calling _ensure_tables explicitly
        self.inbox._ensure_tables()

        handled_row = self.conn.execute(
            "SELECT status, completed_at, seen_at FROM agent_inbox WHERE id = ?",
            (handled_id,),
        ).fetchone()
        self.assertIsNotNone(handled_row)
        self.assertEqual(handled_row['status'], 'completed', "legacy 'handled' status must be migrated to 'completed'")
        self.assertIsNotNone(handled_row['completed_at'], "completed_at must be backfilled from handled_at")
        self.assertIsNotNone(handled_row['seen_at'], "seen_at must be backfilled from handled_at")

        # Pre-existing completed row must be left untouched
        completed_row = self.conn.execute(
            "SELECT status FROM agent_inbox WHERE id = ?",
            (completed_id,),
        ).fetchone()
        self.assertEqual(completed_row['status'], 'completed')


if __name__ == '__main__':
    unittest.main()
