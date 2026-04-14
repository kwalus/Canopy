"""Regression tests for the redesigned DM workspace UI and composer."""

import json
import os
import re
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

from canopy.core.messaging import MessageManager
from canopy.core.messaging import compute_group_id
from canopy.core.mentions import MentionManager
from canopy.ui.routes import create_ui_blueprint


class _FakeDbManager:
    def __init__(self, conn: sqlite3.Connection, db_path: Path) -> None:
        self._conn = conn
        self.db_path = db_path

    def get_connection(self) -> sqlite3.Connection:
        return self._conn

    def get_user(self, user_id: str):
        row = self._conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return dict(row) if row else None

    def store_message(self, message_id, sender_id, recipient_id, content, message_type, metadata):
        self._conn.execute(
            """
            INSERT INTO messages (
                id, sender_id, recipient_id, content, message_type, status,
                created_at, delivered_at, read_at, edited_at, metadata
            ) VALUES (?, ?, ?, ?, ?, ?, datetime('now'), NULL, NULL, NULL, ?)
            """,
            (
                message_id,
                sender_id,
                recipient_id,
                content,
                message_type,
                'pending',
                json.dumps(metadata) if metadata else None,
            ),
        )
        self._conn.commit()
        return True


class _FakeP2PManager:
    def __init__(self) -> None:
        self.direct_messages = []

    def is_running(self) -> bool:
        return True

    def broadcast_direct_message(self, **kwargs):
        self.direct_messages.append(dict(kwargs))

    def get_peer_id(self) -> str:
        return 'peer-local'

    def describe_direct_message_security(self, recipient_ids):
        return {
            'mode': 'peer_e2e_v1',
            'state': 'encrypted',
            'label': 'E2E over mesh',
            'e2e': True,
            'relay_confidential': True,
            'local_only': False,
            'recipient_ids': list(recipient_ids or []),
        }


class _FakeWorkspaceEventManager:
    def __init__(self, latest_seq: int = 0) -> None:
        self.latest_seq = latest_seq

    def get_latest_seq(self) -> int:
        return int(self.latest_seq)


class TestMessagesUiWorkspace(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)

        db_path = Path(self.tempdir.name) / 'messages_ui.db'
        self.conn = sqlite3.connect(str(db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            """
            CREATE TABLE users (
                id TEXT PRIMARY KEY,
                username TEXT,
                display_name TEXT,
                avatar_file_id TEXT,
                account_type TEXT,
                origin_peer TEXT,
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
            "INSERT INTO users (id, username, display_name, avatar_file_id, account_type, origin_peer, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                ('owner', 'owner', 'Owner', None, 'human', None, '2026-03-07T08:00:00+00:00'),
                ('peer-a', 'peer_a', 'Alice', None, 'agent', None, '2026-03-07T08:01:00+00:00'),
                ('peer-b', 'peer_b', 'Bob', None, 'agent', None, '2026-03-07T08:02:00+00:00'),
                ('peer-c', 'peer_c', 'Cara', None, 'human', None, '2026-03-07T08:03:00+00:00'),
            ],
        )
        self.conn.executemany(
            """
            INSERT INTO messages (
                id, sender_id, recipient_id, content, message_type, status,
                created_at, delivered_at, read_at, edited_at, metadata
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    'DM-root', 'peer-a', 'owner', 'Hello owner', 'text', 'delivered',
                    '2026-03-07T10:00:00+00:00', '2026-03-07T10:00:01+00:00', None, None, None,
                ),
                (
                    'DM-reply', 'owner', 'peer-a', 'Need update', 'text', 'delivered',
                    '2026-03-07T10:05:00+00:00', '2026-03-07T10:05:01+00:00', None, None,
                    json.dumps({'reply_to': 'DM-root'}),
                ),
                (
                    'DM-group', 'peer-b', 'group:abc123', 'Group check-in', 'text', 'delivered',
                    '2026-03-07T10:06:00+00:00', '2026-03-07T10:06:01+00:00', None, None,
                    json.dumps({'group_id': 'group:abc123', 'group_members': ['owner', 'peer-b', 'peer-c']}),
                ),
                (
                    'DM-group-relayed', 'peer-c', 'owner', 'Relay delivered through broker', 'text', 'delivered',
                    '2026-03-07T10:07:00+00:00', '2026-03-07T10:07:02+00:00', None, None,
                    json.dumps({'group_id': 'group:relay-alias', 'group_members': ['owner', 'peer-b', 'peer-c']}),
                ),
            ],
        )
        self.conn.commit()

        self.db_manager = _FakeDbManager(self.conn, db_path)
        self.message_manager = MessageManager(self.db_manager, MagicMock())
        self.profile_manager = MagicMock()
        self.profile_manager.get_profile.return_value = None
        self.p2p_manager = _FakeP2PManager()
        self.workspace_event_manager = _FakeWorkspaceEventManager()

        components = (
            self.db_manager,
            MagicMock(),
            MagicMock(),
            self.message_manager,
            MagicMock(),
            MagicMock(),
            MagicMock(),
            MagicMock(),
            self.profile_manager,
            MagicMock(),
            self.p2p_manager,
        )

        self.get_components_patcher = patch('canopy.ui.routes.get_app_components', return_value=components)
        self.get_components_patcher.start()
        self.addCleanup(self.get_components_patcher.stop)

        app = Flask(__name__)
        app.config['TESTING'] = True
        app.secret_key = 'test-secret'
        app.config['WORKSPACE_EVENT_MANAGER'] = self.workspace_event_manager
        app.config['MENTION_MANAGER'] = MentionManager(self.db_manager)
        app.register_blueprint(create_ui_blueprint())
        self.app = app
        self.client = app.test_client()
        self._set_authenticated_session()

    def tearDown(self) -> None:
        self.conn.close()

    def _set_authenticated_session(self, csrf_token: str = 'csrf-ui-messages') -> None:
        with self.client.session_transaction() as sess:
            sess['authenticated'] = True
            sess['user_id'] = 'owner'
            sess['username'] = 'owner'
            sess['display_name'] = 'Owner'
            sess['_csrf_token'] = csrf_token

    def test_messages_page_renders_conversation_workspace(self) -> None:
        response = self.client.get('/messages?with=peer-a')
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn('Direct Messages', body)
        self.assertIn('Alice', body)
        self.assertIn('Bob, Cara', body)
        self.assertIn('Need update', body)
        self.assertIn('Hello owner', body)
        self.assertIn('New conversation', body)
        self.assertIn('E2E over mesh', body)
        active_direct_card = re.search(
            r'<a href="/messages\?with=peer-a" class="dm-conversation-card active">([\s\S]*?)</a>',
            body,
        )
        self.assertIsNotNone(active_direct_card)
        self.assertNotIn('dm-unread-pill', active_direct_card.group(1))
        self.assertIn('.7z,.rar', body)
        self.assertIn('.html,.css,.sh,.bat,.cfg,.ini,.toml', body)
        self.assertIn('/ajax/messages/thread_snapshot', body)
        self.assertIn('/api/v1/events?', body)
        self.assertIn('let dmEventCursor = ', body)
        self.assertIn('function pollDmEvents() {', body)
        self.assertIn('function queueDmSnapshot(options) {', body)
        self.assertIn('dmQueuedSnapshotOptions', body)
        self.assertIn("function refreshMessages() {", body)
        self.assertIn("if (isDmSearchActive()) {", body)
        self.assertIn("loadDmSnapshot({ forceBottom: false, allowDeferred: false, hardFallback: true }).catch(() => {});", body)
        self.assertIn('/ajax/mention_suggestions?', body)
        self.assertIn('let recipientDirectory = [', body)
        self.assertIn('"user_id": "peer-a"', body)
        self.assertIn('"user_id": "peer-b"', body)
        self.assertIn('setupMessageDropzone();', body)
        self.assertIn("composer.addEventListener('drop'", body)
        self.assertNotIn("threadPane.addEventListener('paste'", body)
        self.assertIn('grid-template-rows: auto minmax(0, 1fr);', body)
        self.assertIn('position: sticky;', body)
        self.assertIn('class="messages-page has-active-thread"', body)
        self.assertIn('dm-sidebar-mobile-header', body)
        self.assertIn('dm-mobile-sidebar-backdrop', body)
        self.assertIn('toggleDmMobileSidebar(true)', body)
        self.assertIn('function syncDmMobileLayoutState(options)', body)
        self.assertIn("window.addEventListener('resize', scheduleDmMobileLayoutSync);", body)

    def test_dm_message_text_preserves_multiline_content(self) -> None:
        root = Path(__file__).resolve().parents[1]
        template = (root / 'canopy' / 'ui' / 'templates' / 'messages.html').read_text(encoding='utf-8')
        self.assertIn('.dm-bubble-copy {', template)
        self.assertIn('white-space: pre-wrap;', template)
        self.assertIn('line-height: 1.45;', template)

    def test_inline_edit_js_reapplies_rich_content_after_text_update(self) -> None:
        root = Path(__file__).resolve().parents[1]
        template = (root / 'canopy' / 'ui' / 'templates' / 'messages.html').read_text(encoding='utf-8')
        pattern = re.compile(
            r"function applyInlineDmMessageEdit\(messageId, content, editedAt\)\s*\{.*?"
            r"textEl\.textContent = content \|\| '';\s*"
            r"applyDmRichContentInScope\(msgEl\);",
            re.S,
        )
        self.assertRegex(template, pattern)

    def test_inline_edit_success_path_avoids_full_thread_snapshot_reload(self) -> None:
        root = Path(__file__).resolve().parents[1]
        template = (root / 'canopy' / 'ui' / 'templates' / 'messages.html').read_text(encoding='utf-8')
        pattern = re.compile(
            r"showAlert\('Message updated successfully', 'success'\);\s*"
            r"(?:(?!loadDmSnapshot).)*?\.catch\(err => showAlert\('Failed to update message:",
            re.S,
        )
        self.assertRegex(template, pattern)

    def test_visibility_change_relies_on_event_poll_without_forcing_full_snapshot(self) -> None:
        root = Path(__file__).resolve().parents[1]
        template = (root / 'canopy' / 'ui' / 'templates' / 'messages.html').read_text(encoding='utf-8')
        pattern = re.compile(
            r"document\.addEventListener\('visibilitychange', \(\) => \{\s*"
            r"if \(!document\.hidden && !isDmSearchActive\(\)\) \{\s*"
            r"syncDmMobileLayoutState\(\{ keepSidebarOpen: true \}\);\s*"
            r"pollDmEvents\(\);\s*"
            r"\}\s*\}\);",
            re.S,
        )
        self.assertRegex(template, pattern)

    def test_messages_page_acknowledges_dm_mentions_for_open_thread(self) -> None:
        self.conn.execute(
            """
            INSERT INTO mention_events (id, user_id, source_type, source_id, author_id, preview, status)
            VALUES (?, ?, ?, ?, ?, ?, 'new')
            """,
            ('MN-dm-open-1', 'owner', 'dm', 'DM-root', 'peer-a', 'Reply needed'),
        )
        self.conn.commit()

        before = self.conn.execute(
            "SELECT acknowledged_at FROM mention_events WHERE id = ?",
            ('MN-dm-open-1',),
        ).fetchone()
        self.assertIsNotNone(before)
        self.assertIsNone(before['acknowledged_at'])

        response = self.client.get('/messages?with=peer-a')
        self.assertEqual(response.status_code, 200)

        after = self.conn.execute(
            "SELECT acknowledged_at, status FROM mention_events WHERE id = ?",
            ('MN-dm-open-1',),
        ).fetchone()
        self.assertIsNotNone(after)
        self.assertTrue(after['acknowledged_at'])
        self.assertEqual(after['status'], 'acknowledged')

    def test_messages_page_refreshes_shell_summary_when_open_thread_marks_dm_read(self) -> None:
        with patch('canopy.ui.routes._refresh_current_meshspace_shell_summary') as refresh_mock:
            response = self.client.get('/messages?with=peer-a')
        self.assertEqual(response.status_code, 200)
        refresh_mock.assert_called_once_with('owner')

    def test_messages_page_renders_source_layout_metadata_for_structured_dm_sources(self) -> None:
        self.conn.execute(
            """
            INSERT INTO messages (
                id, sender_id, recipient_id, content, message_type, status,
                created_at, delivered_at, read_at, edited_at, metadata
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                'DM-layout',
                'peer-a',
                'owner',
                'Structured lesson brief',
                'text',
                'delivered',
                '2026-03-07T10:08:00+00:00',
                '2026-03-07T10:08:01+00:00',
                None,
                None,
                json.dumps(
                    {
                        'source_layout': {
                            'version': 1,
                            'hero': {'ref': 'attachment:F-module'},
                            'lede': {'kind': 'rich_text', 'ref': 'content:lede'},
                            'deck': {'default_ref': 'attachment:F-module'},
                        },
                        'attachments': [
                            {
                                'id': 'F-module',
                                'name': 'lesson.canopy-module.html',
                                'type': 'text/html',
                            }
                        ],
                    }
                ),
            ),
        )
        self.conn.commit()

        response = self.client.get('/messages?with=peer-a')
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn('data-canopy-source-layout=', body)
        self.assertIn('data-canopy-source-ref="content:lede"', body)

    def test_messages_search_renders_matching_results(self) -> None:
        response = self.client.get('/messages?search=relay')
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn('Search DMs and group chats', body)
        self.assertIn('Search results', body)
        self.assertIn('Relay delivered through broker', body)
        self.assertIn('Clear search', body)
        self.assertNotIn('id="dm-composer"', body)

    def test_message_search_decrypts_before_matching(self) -> None:
        self.conn.execute(
            """
            INSERT INTO messages (
                id, sender_id, recipient_id, content, message_type, status,
                created_at, delivered_at, read_at, edited_at, metadata
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                'DM-encrypted', 'peer-a', 'owner', 'cipher:relay', 'text', 'delivered',
                '2026-03-07T10:09:00+00:00', '2026-03-07T10:09:01+00:00', None, None, None,
            ),
        )
        self.conn.commit()
        self.message_manager.data_encryptor = types.SimpleNamespace(
            is_enabled=True,
            decrypt=lambda value: 'relay decrypted note' if value == 'cipher:relay' else value,
        )

        results = self.message_manager.search_messages('owner', 'decrypted', limit=20)

        self.assertTrue(any(message.id == 'DM-encrypted' for message in results))

    def test_message_search_pages_past_recent_non_matches_for_older_encrypted_hit(self) -> None:
        newer_rows = []
        for index in range(405):
            newer_rows.append(
                (
                    f'DM-filler-{index}',
                    'peer-a',
                    'owner',
                    f'noise message {index}',
                    'text',
                    'delivered',
                    f'2026-03-08T11:{index // 60:02d}:{index % 60:02d}+00:00',
                    f'2026-03-08T11:{index // 60:02d}:{index % 60:02d}+00:00',
                    None,
                    None,
                    None,
                )
            )
        self.conn.executemany(
            """
            INSERT INTO messages (
                id, sender_id, recipient_id, content, message_type, status,
                created_at, delivered_at, read_at, edited_at, metadata
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            newer_rows,
        )
        self.conn.execute(
            """
            INSERT INTO messages (
                id, sender_id, recipient_id, content, message_type, status,
                created_at, delivered_at, read_at, edited_at, metadata
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                'DM-older-encrypted',
                'peer-a',
                'owner',
                'cipher:older-hit',
                'text',
                'delivered',
                '2026-03-07T09:59:00+00:00',
                '2026-03-07T09:59:01+00:00',
                None,
                None,
                None,
            ),
        )
        self.conn.commit()
        self.message_manager.data_encryptor = types.SimpleNamespace(
            is_enabled=True,
            decrypt=lambda value: 'very old relay search hit' if value == 'cipher:older-hit' else value,
        )

        results = self.message_manager.search_messages('owner', 'relay search hit', limit=20)

        self.assertTrue(any(message.id == 'DM-older-encrypted' for message in results))

    def test_ajax_messages_thread_snapshot_returns_partial_fragments(self) -> None:
        response = self.client.get('/ajax/messages/thread_snapshot?with=peer-a')
        self.assertEqual(response.status_code, 200)
        payload = response.get_json() or {}
        self.assertTrue(payload.get('success'))
        self.assertEqual((payload.get('active_thread') or {}).get('user_id'), 'peer-a')
        self.assertIn('Alice', payload.get('sidebar_html') or '')
        self.assertIn('Hello owner', payload.get('thread_body_html') or '')
        self.assertIn('Need update', payload.get('thread_body_html') or '')
        self.assertIn('E2E over mesh', payload.get('thread_header_html') or '')
        self.assertEqual(payload.get('workspace_event_cursor'), 0)
        self.assertTrue(payload.get('thread_state_token'))
        self.assertTrue(payload.get('sidebar_state_token'))

    def test_thread_snapshot_cursor_does_not_advance_past_snapshot_state(self) -> None:
        self.workspace_event_manager.latest_seq = 5
        original_get_messages = self.message_manager.get_messages

        def _race_get_messages(*args, **kwargs):
            self.workspace_event_manager.latest_seq = 9
            return original_get_messages(*args, **kwargs)

        with patch.object(self.message_manager, 'get_messages', side_effect=_race_get_messages):
            response = self.client.get('/ajax/messages/thread_snapshot?with=peer-a')

        self.assertEqual(response.status_code, 200)
        payload = response.get_json() or {}
        self.assertTrue(payload.get('success'))
        self.assertEqual(payload.get('workspace_event_cursor'), 5)

    def test_thread_snapshot_token_changes_when_existing_message_is_edited(self) -> None:
        before = self.client.get('/ajax/messages/thread_snapshot?with=peer-a').get_json() or {}
        before_token = before.get('thread_state_token')

        self.conn.execute(
            """
            UPDATE messages
            SET content = ?, edited_at = ?
            WHERE id = ?
            """,
            (
                'Need update (edited)',
                '2026-03-07T10:08:00+00:00',
                'DM-reply',
            ),
        )
        self.conn.commit()

        after = self.client.get('/ajax/messages/thread_snapshot?with=peer-a').get_json() or {}
        self.assertTrue(after.get('success'))
        self.assertNotEqual(before_token, after.get('thread_state_token'))
        self.assertIn('Need update (edited)', after.get('thread_body_html') or '')

    def test_ajax_send_message_preserves_reply_to_metadata(self) -> None:
        response = self.client.post(
            '/ajax/send_message',
            json={
                'recipient_id': 'peer-a',
                'content': 'Follow-up with context',
                'reply_to': 'DM-root',
            },
            headers={'X-CSRFToken': 'csrf-ui-messages'},
        )
        self.assertEqual(response.status_code, 200)
        payload = response.get_json() or {}
        self.assertTrue(payload.get('success'))

        row = self.conn.execute(
            'SELECT recipient_id, metadata FROM messages WHERE id = ?',
            (payload['message']['id'],),
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row['recipient_id'], 'peer-a')
        metadata = json.loads(row['metadata']) if row['metadata'] else {}
        self.assertEqual(metadata.get('reply_to'), 'DM-root')
        self.assertEqual(self.p2p_manager.direct_messages[-1]['metadata'].get('reply_to'), 'DM-root')

    def test_ajax_send_message_can_start_fresh_same_member_group_thread(self) -> None:
        base_group_id = compute_group_id(['owner', 'peer-b', 'peer-c'])
        response = self.client.post(
            '/ajax/send_message',
            json={
                'recipient_ids': ['peer-b', 'peer-c'],
                'content': 'Fresh same people thread',
                'force_new_group': True,
            },
            headers={'X-CSRFToken': 'csrf-ui-messages'},
        )
        self.assertEqual(response.status_code, 200)
        payload = response.get_json() or {}
        self.assertTrue(payload.get('success'))
        self.assertTrue(payload.get('group_thread_id'))
        self.assertTrue(payload.get('group_id'))
        self.assertNotEqual(payload.get('group_id'), base_group_id)
        self.assertEqual(payload.get('base_group_id'), base_group_id)

        row = self.conn.execute(
            'SELECT recipient_id, metadata FROM messages WHERE id = ?',
            (payload['message']['id'],),
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row['recipient_id'], payload.get('group_id'))
        metadata = json.loads(row['metadata']) if row['metadata'] else {}
        self.assertEqual(metadata.get('group_id'), payload.get('group_id'))
        self.assertEqual(metadata.get('base_group_id'), base_group_id)
        self.assertEqual(metadata.get('group_thread_id'), payload.get('group_thread_id'))
        self.assertEqual(metadata.get('group_members'), ['owner', 'peer-b', 'peer-c'])
        self.assertEqual(self.p2p_manager.direct_messages[-1]['metadata'].get('group_thread_id'), payload.get('group_thread_id'))

    def test_group_thread_view_uses_canonical_identity_for_relayed_group_messages(self) -> None:
        canonical_group_id = compute_group_id(['owner', 'peer-b', 'peer-c'])

        response = self.client.get(f'/messages?group={canonical_group_id}')
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)

        self.assertIn('Group check-in', body)
        self.assertIn('Relay delivered through broker', body)
        self.assertIn(f'/messages?group={canonical_group_id}', body)

    def test_group_thread_merges_partial_relay_member_sets_by_group_alias(self) -> None:
        shared_group_id = 'group:split-relay'
        partial_group_id = compute_group_id(['owner', 'peer-b'])
        self.conn.executemany(
            """
            INSERT INTO messages (
                id, sender_id, recipient_id, content, message_type, status,
                created_at, delivered_at, read_at, edited_at, metadata
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    'DM-split-b', 'peer-b', shared_group_id, 'Partial relay from Bob',
                    'text', 'delivered', '2026-03-07T10:08:00+00:00',
                    '2026-03-07T10:08:01+00:00', None, None,
                    json.dumps({'group_id': shared_group_id, 'group_members': ['owner', 'peer-b']}),
                ),
                (
                    'DM-split-c', 'peer-c', shared_group_id, 'Partial relay from Cara',
                    'text', 'delivered', '2026-03-07T10:09:00+00:00',
                    '2026-03-07T10:09:01+00:00', None, None,
                    json.dumps({'group_id': shared_group_id, 'group_members': ['owner', 'peer-c']}),
                ),
            ],
        )
        self.conn.commit()

        messages = self.message_manager.get_group_conversation('owner', partial_group_id, limit=20)
        self.assertEqual([message.id for message in messages[-2:]], ['DM-split-b', 'DM-split-c'])

        response = self.client.get(f'/ajax/messages/thread_snapshot?group={partial_group_id}')
        self.assertEqual(response.status_code, 200)
        payload = response.get_json() or {}
        self.assertTrue(payload.get('success'))
        self.assertIn('Partial relay from Bob', payload.get('thread_body_html') or '')
        self.assertIn('Partial relay from Cara', payload.get('thread_body_html') or '')
        composer_ids = sorted(
            item.get('user_id')
            for item in payload.get('composer_recipients') or []
            if item.get('user_id')
        )
        self.assertEqual(composer_ids, ['peer-b', 'peer-c'])


if __name__ == '__main__':
    unittest.main()
