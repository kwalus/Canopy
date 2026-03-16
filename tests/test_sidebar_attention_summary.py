"""Regression coverage for sidebar unread summary badges."""

import os
import sqlite3
import sys
import types
import unittest
from types import SimpleNamespace
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
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def get_connection(self) -> sqlite3.Connection:
        return self._conn

    def get_instance_owner_user_id(self):
        return None


class _FakeFeedManager:
    def __init__(self, unread_count: int = 0) -> None:
        self.unread_count = unread_count
        self.marked_users: list[str] = []

    def count_unread_posts(self, user_id: str) -> int:
        return int(self.unread_count)

    def mark_feed_viewed(self, user_id: str) -> bool:
        self.marked_users.append(str(user_id))
        return True

    def purge_expired_posts(self):
        return []

    def get_user_feed(self, user_id: str, limit: int = 50, algorithm: str = 'chronological'):
        return []


class TestSidebarAttentionSummary(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(':memory:')
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
                ('owner', 'owner', 'Owner', None, 'human', None, '2026-03-16T09:00:00+00:00'),
                ('peer-a', 'peer_a', 'Peer A', None, 'agent', None, '2026-03-16T09:01:00+00:00'),
                ('peer-b', 'peer_b', 'Peer B', None, 'human', None, '2026-03-16T09:02:00+00:00'),
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
                    'dm-unread', 'peer-a', 'owner', 'Unread direct', 'text', 'delivered',
                    '2026-03-16T10:00:00+00:00', '2026-03-16T10:00:01+00:00', None, None, None,
                ),
                (
                    'group-unread', 'peer-b', 'group:alpha', 'Unread group', 'text', 'delivered',
                    '2026-03-16T10:05:00+00:00', '2026-03-16T10:05:01+00:00', None, None,
                    '{"group_id":"group:alpha","group_members":["owner","peer-b"]}',
                ),
                (
                    'dm-read', 'peer-a', 'owner', 'Already read', 'text', 'delivered',
                    '2026-03-16T10:10:00+00:00', '2026-03-16T10:10:01+00:00', '2026-03-16T10:11:00+00:00', None, None,
                ),
                (
                    'dm-outbound', 'owner', 'peer-a', 'Sent by owner', 'text', 'delivered',
                    '2026-03-16T10:12:00+00:00', '2026-03-16T10:12:01+00:00', None, None, None,
                ),
            ],
        )
        self.conn.commit()

        self.db_manager = _FakeDbManager(self.conn)
        self.feed_manager = _FakeFeedManager(unread_count=4)
        self.channel_manager = MagicMock()
        self.channel_manager.get_user_channels.return_value = [
            SimpleNamespace(
                id='chan-1',
                name='general',
                description='',
                channel_type='public',
                privacy_mode='open',
                origin_peer='',
                user_role='member',
                member_count=3,
                unread_count=2,
                notifications_enabled=True,
                crypto_mode='',
                lifecycle_status='active',
                lifecycle_ttl_days=180,
                lifecycle_preserved=False,
                archived_at=None,
                archive_reason=None,
                days_until_archive=None,
                owner_peer_state=None,
            ),
            SimpleNamespace(
                id='chan-2',
                name='ops',
                description='',
                channel_type='private',
                privacy_mode='open',
                origin_peer='',
                user_role='member',
                member_count=2,
                unread_count=3,
                notifications_enabled=True,
                crypto_mode='',
                lifecycle_status='active',
                lifecycle_ttl_days=180,
                lifecycle_preserved=False,
                archived_at=None,
                archive_reason=None,
                days_until_archive=None,
                owner_peer_state=None,
            ),
        ]

        self.interaction_manager = MagicMock()
        self.interaction_manager.get_user_liked_ids.return_value = set()

        components = (
            self.db_manager,
            MagicMock(),
            MagicMock(),
            None,
            self.channel_manager,
            MagicMock(),
            self.feed_manager,
            self.interaction_manager,
            MagicMock(),
            MagicMock(),
            None,
        )

        self.get_components_patcher = patch('canopy.ui.routes.get_app_components', return_value=components)
        self.get_components_patcher.start()
        self.addCleanup(self.get_components_patcher.stop)

        self.render_template_patcher = patch('canopy.ui.routes.render_template', return_value='ok')
        self.render_template_patcher.start()
        self.addCleanup(self.render_template_patcher.stop)

        app = Flask(__name__)
        app.config['TESTING'] = True
        app.secret_key = 'test-secret'
        app.register_blueprint(create_ui_blueprint())
        self.client = app.test_client()
        with self.client.session_transaction() as sess:
            sess['authenticated'] = True
            sess['user_id'] = 'owner'
            sess['username'] = 'owner'
            sess['display_name'] = 'Owner'

    def tearDown(self) -> None:
        self.conn.close()

    def test_sidebar_attention_summary_reports_messages_channels_and_feed(self) -> None:
        response = self.client.get('/ajax/sidebar_attention_summary')
        self.assertEqual(response.status_code, 200)
        payload = response.get_json() or {}
        self.assertTrue(payload.get('success'))
        self.assertTrue(payload.get('changed'))
        self.assertEqual(payload.get('summary'), {
            'messages': 2,
            'channels': 5,
            'feed': 4,
            'total': 11,
        })

        rev = payload.get('rev')
        repeat = self.client.get(f'/ajax/sidebar_attention_summary?rev={rev}')
        self.assertEqual(repeat.status_code, 200)
        repeat_payload = repeat.get_json() or {}
        self.assertTrue(repeat_payload.get('success'))
        self.assertFalse(repeat_payload.get('changed'))
        self.assertEqual(repeat_payload.get('summary'), {})

    def test_feed_route_marks_feed_viewed_on_page_open(self) -> None:
        response = self.client.get('/feed')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.feed_manager.marked_users, ['owner'])


if __name__ == '__main__':
    unittest.main()
