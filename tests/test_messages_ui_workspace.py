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
        active_direct_card = re.search(
            r'<a href="/messages\?with=peer-a" class="dm-conversation-card active">([\s\S]*?)</a>',
            body,
        )
        self.assertIsNotNone(active_direct_card)
        self.assertNotIn('dm-unread-pill', active_direct_card.group(1))

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

    def test_group_thread_view_uses_canonical_identity_for_relayed_group_messages(self) -> None:
        canonical_group_id = compute_group_id(['owner', 'peer-b', 'peer-c'])

        response = self.client.get(f'/messages?group={canonical_group_id}')
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)

        self.assertIn('Group check-in', body)
        self.assertIn('Relay delivered through broker', body)
        self.assertIn(f'/messages?group={canonical_group_id}', body)


if __name__ == '__main__':
    unittest.main()
