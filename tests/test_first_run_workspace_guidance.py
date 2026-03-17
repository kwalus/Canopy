"""Regression coverage for first-run workspace guidance and landing defaults."""

import os
import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path
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

from canopy.core.messaging import MessageManager
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

    def get_instance_owner_user_id(self):
        return 'owner'

    def get_pending_approval_count(self):
        return 0


class _FakeP2PManager:
    def __init__(self, connected_peers=None) -> None:
        self._connected_peers = list(connected_peers or [])

    def is_running(self) -> bool:
        return True

    def get_connected_peers(self):
        return list(self._connected_peers)

    def get_peer_id(self):
        return 'peer-local'


class _FakeWorkspaceEventManager:
    def __init__(self, latest_seq: int = 0) -> None:
        self.latest_seq = latest_seq

    def get_latest_seq(self) -> int:
        return int(self.latest_seq)


class _FakeFeedManager:
    def __init__(self) -> None:
        self.marked_viewed = []

    def mark_feed_viewed(self, user_id: str) -> None:
        self.marked_viewed.append(user_id)

    def purge_expired_posts(self):
        return []

    def get_user_feed(self, user_id: str, limit: int = 50, algorithm: str = 'chronological'):
        return []

    def search_posts(self, query: str, user_id: str, limit: int = 50):
        return []

    def get_post(self, post_id: str):
        return None


class TestFirstRunWorkspaceGuidance(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)

        db_path = Path(self.tempdir.name) / 'first_run_guidance.db'
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
            CREATE TABLE channel_members (
                channel_id TEXT,
                user_id TEXT
            );
            CREATE TABLE channel_messages (
                id TEXT PRIMARY KEY,
                channel_id TEXT,
                user_id TEXT,
                content TEXT,
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
            CREATE TABLE feed_posts (
                id TEXT PRIMARY KEY,
                author_id TEXT,
                content TEXT,
                content_type TEXT,
                visibility TEXT,
                metadata TEXT,
                created_at TEXT
            );
            CREATE TABLE api_keys (
                id TEXT PRIMARY KEY,
                user_id TEXT,
                revoked INTEGER DEFAULT 0
            );
            """
        )
        self.conn.execute(
            """
            INSERT INTO users (id, username, display_name, avatar_file_id, account_type, origin_peer, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            ('owner', 'owner', 'Owner', None, 'human', None, '2026-03-17T08:00:00+00:00'),
        )
        self.conn.execute(
            "INSERT INTO channel_members (channel_id, user_id) VALUES (?, ?)",
            ('general', 'owner'),
        )
        self.conn.commit()

        self.db_manager = _FakeDbManager(self.conn, db_path)
        self.message_manager = MessageManager(self.db_manager, MagicMock())
        self.feed_manager = _FakeFeedManager()
        self.channel_manager = MagicMock()
        self.channel_manager.DEFAULT_CHANNEL_LIFECYCLE_DAYS = 180
        self.channel_manager.get_user_channels.return_value = [
            SimpleNamespace(
                id='general',
                name='general',
                description='General discussion',
                channel_type='public',
                privacy_mode='open',
                post_policy='open',
                allow_member_replies=True,
                can_post_top_level=True,
                can_reply=True,
                allowed_poster_count=0,
                origin_peer='',
                user_role='owner',
                member_count=1,
                unread_count=0,
                notifications_enabled=True,
                crypto_mode='',
                lifecycle_status='preserved',
                lifecycle_ttl_days=180,
                lifecycle_preserved=True,
                archived_at=None,
                archive_reason=None,
                days_until_archive=None,
                owner_peer_state='local',
                last_activity_at=None,
            ),
        ]
        self.channel_manager.describe_channel_lifecycle.side_effect = lambda channel, **kwargs: {
            'status': getattr(channel, 'lifecycle_status', 'active'),
            'ttl_days': getattr(channel, 'lifecycle_ttl_days', 180),
            'preserved': bool(getattr(channel, 'lifecycle_preserved', False)),
            'archived_at': getattr(channel, 'archived_at', None),
            'archive_reason': getattr(channel, 'archive_reason', None),
            'days_until_archive': getattr(channel, 'days_until_archive', None),
            'owner_peer_state': getattr(channel, 'owner_peer_state', 'local'),
            'last_activity_at': getattr(channel, 'last_activity_at', None),
        }
        self.channel_manager.get_all_peer_device_profiles.return_value = {}

        self.profile_manager = MagicMock()
        self.profile_manager.get_profile.return_value = None
        self.trust_manager = MagicMock()
        self.trust_manager.get_trust_score.return_value = 65
        self.p2p_manager = _FakeP2PManager()
        self.workspace_event_manager = _FakeWorkspaceEventManager()

        components = (
            self.db_manager,
            MagicMock(),
            self.trust_manager,
            self.message_manager,
            self.channel_manager,
            MagicMock(),
            self.feed_manager,
            MagicMock(),
            self.profile_manager,
            MagicMock(),
            self.p2p_manager,
        )

        self.get_components_patcher = patch('canopy.ui.routes.get_app_components', return_value=components)
        self.get_components_any_patcher = patch('canopy.ui.routes._get_app_components_any', return_value=components)
        self.get_components_patcher.start()
        self.get_components_any_patcher.start()
        self.addCleanup(self.get_components_patcher.stop)
        self.addCleanup(self.get_components_any_patcher.stop)

        app = Flask(__name__)
        app.config['TESTING'] = True
        app.secret_key = 'first-run-guidance'
        app.config['WORKSPACE_EVENT_MANAGER'] = self.workspace_event_manager
        app.register_blueprint(create_ui_blueprint())
        self.app = app
        self.client = app.test_client()
        self._set_authenticated_session()

    def tearDown(self) -> None:
        self.conn.close()

    def _set_authenticated_session(self) -> None:
        with self.client.session_transaction() as sess:
            sess['authenticated'] = True
            sess['user_id'] = 'owner'
            sess['username'] = 'owner'
            sess['display_name'] = 'Owner'
            sess['_csrf_token'] = 'csrf-first-run'

    def test_mobile_dashboard_redirects_first_run_users_to_channels(self) -> None:
        response = self.client.get('/', headers={'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X)'})
        self.assertEqual(response.status_code, 302)
        self.assertIn('/channels?channel=general', response.headers.get('Location', ''))

    def test_mobile_dashboard_uses_feed_after_core_first_run_actions_complete(self) -> None:
        self.conn.execute(
            "INSERT INTO messages (id, sender_id, recipient_id, content, message_type, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ('DM-1', 'owner', 'peer-a', 'hello', 'text', 'delivered', '2026-03-17T09:00:00+00:00'),
        )
        self.conn.execute(
            "INSERT INTO feed_posts (id, author_id, content, content_type, visibility, metadata, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ('POST-1', 'owner', 'first post', 'text', 'network', '{}', '2026-03-17T09:05:00+00:00'),
        )
        self.conn.commit()
        self.p2p_manager._connected_peers = ['peer-alpha']

        response = self.client.get('/', headers={'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X)'})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers.get('Location', '').endswith('/feed'))

    def test_channels_page_renders_first_day_guide(self) -> None:
        response = self.client.get('/channels')
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn('First-day guide', body)
        self.assertIn('Open #general', body)
        self.assertIn('Create first post', body)
        self.assertIn('Open direct messages', body)
        self.assertIn('messages sent', body)
        self.assertIn('peers online', body)

    def test_channels_page_hides_guide_after_core_first_run_actions_complete(self) -> None:
        self.conn.execute(
            "INSERT INTO channel_messages (id, channel_id, user_id, content, created_at) VALUES (?, ?, ?, ?, ?)",
            ('CH-1', 'general', 'owner', 'hello channel', '2026-03-17T09:10:00+00:00'),
        )
        self.conn.execute(
            "INSERT INTO feed_posts (id, author_id, content, content_type, visibility, metadata, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ('POST-2', 'owner', 'first post', 'text', 'network', '{}', '2026-03-17T09:15:00+00:00'),
        )
        self.conn.commit()
        self.p2p_manager._connected_peers = ['peer-alpha']

        response = self.client.get('/channels')
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertNotIn('First-day guide', body)

    def test_feed_page_renders_first_day_guide(self) -> None:
        response = self.client.get('/feed')
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn('First-day guide', body)
        self.assertIn('Create first post', body)
        self.assertIn('Open channels', body)
        self.assertIn('Open direct messages', body)

    def test_messages_page_renders_first_day_guide(self) -> None:
        response = self.client.get('/messages')
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn('First-day guide', body)
        self.assertIn('New conversation', body)
        self.assertIn('Open channels', body)
        self.assertIn('Open feed', body)


if __name__ == '__main__':
    unittest.main()
