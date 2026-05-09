"""Regression tests for source advancement without reposting."""

from __future__ import annotations

import sqlite3
import sys
import types
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock

if "zeroconf" not in sys.modules:
    zeroconf_stub = types.ModuleType("zeroconf")

    class _Dummy:
        def __init__(self, *args, **kwargs):
            pass

    zeroconf_stub.ServiceBrowser = _Dummy
    zeroconf_stub.ServiceInfo = _Dummy
    zeroconf_stub.Zeroconf = _Dummy
    zeroconf_stub.ServiceStateChange = _Dummy
    sys.modules["zeroconf"] = zeroconf_stub

from canopy.core.channels import ChannelManager
from canopy.core.feed import FeedManager


class _FakeDb:
    def __init__(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row

    def get_connection(self):
        return self.conn

    def get_user(self, user_id: str):
        row = self.conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return dict(row) if row else None


class TestSourceAdvance(unittest.TestCase):
    def tearDown(self) -> None:
        if hasattr(self, "db"):
            self.db.conn.close()

    def test_feed_advance_updates_activity_without_stale_rewind(self) -> None:
        self.db = _FakeDb()
        self.db.conn.executescript(
            """
            CREATE TABLE users (
                id TEXT PRIMARY KEY,
                username TEXT,
                origin_peer TEXT
            );
            CREATE TABLE feed_posts (
                id TEXT PRIMARY KEY,
                author_id TEXT,
                content TEXT,
                content_type TEXT DEFAULT 'text',
                visibility TEXT DEFAULT 'network',
                metadata TEXT,
                created_at TEXT,
                expires_at TEXT,
                likes INTEGER DEFAULT 0,
                comments INTEGER DEFAULT 0,
                shares INTEGER DEFAULT 0,
                source_type TEXT DEFAULT 'human',
                source_agent_id TEXT,
                source_url TEXT,
                tags TEXT,
                last_activity_at TEXT
            );
            CREATE TABLE post_permissions (post_id TEXT, user_id TEXT);
            """
        )
        self.db.conn.execute("INSERT INTO users (id, username) VALUES ('owner', 'owner')")
        self.db.conn.execute(
            """
            INSERT INTO feed_posts (id, author_id, content, content_type, visibility, metadata, created_at, last_activity_at)
            VALUES ('post-1', 'owner', 'Telemetry source', 'text', 'network', '{}', '2026-05-09T10:00:00+00:00', '2026-05-09 10:00:00')
            """
        )
        self.db.conn.commit()
        manager = FeedManager(self.db, MagicMock())

        advanced = manager.advance_post(
            'post-1',
            'owner',
            reason='telemetry updated',
            advanced_at=datetime(2026, 5, 9, 12, 0, tzinfo=timezone.utc),
        )
        self.assertFalse((advanced or {}).get('unchanged'))
        stored = self.db.conn.execute("SELECT last_activity_at FROM feed_posts WHERE id = 'post-1'").fetchone()
        self.assertEqual(stored['last_activity_at'], '2026-05-09 12:00:00')

        stale = manager.advance_post(
            'post-1',
            'owner',
            reason='old relay event',
            advanced_at=datetime(2026, 5, 9, 11, 0, tzinfo=timezone.utc),
        )
        self.assertTrue((stale or {}).get('unchanged'))
        stored = self.db.conn.execute("SELECT last_activity_at FROM feed_posts WHERE id = 'post-1'").fetchone()
        self.assertEqual(stored['last_activity_at'], '2026-05-09 12:00:00')

    def test_feed_advance_uses_created_at_when_activity_is_missing(self) -> None:
        self.db = _FakeDb()
        self.db.conn.executescript(
            """
            CREATE TABLE users (
                id TEXT PRIMARY KEY,
                username TEXT,
                origin_peer TEXT
            );
            CREATE TABLE feed_posts (
                id TEXT PRIMARY KEY,
                author_id TEXT,
                content TEXT,
                content_type TEXT DEFAULT 'text',
                visibility TEXT DEFAULT 'network',
                metadata TEXT,
                created_at TEXT,
                expires_at TEXT,
                likes INTEGER DEFAULT 0,
                comments INTEGER DEFAULT 0,
                shares INTEGER DEFAULT 0,
                source_type TEXT DEFAULT 'human',
                source_agent_id TEXT,
                source_url TEXT,
                tags TEXT,
                last_activity_at TEXT
            );
            CREATE TABLE post_permissions (post_id TEXT, user_id TEXT);
            """
        )
        self.db.conn.execute("INSERT INTO users (id, username) VALUES ('owner', 'owner')")
        self.db.conn.execute(
            """
            INSERT INTO feed_posts (id, author_id, content, content_type, visibility, metadata, created_at, last_activity_at)
            VALUES ('post-2', 'owner', 'Recent source', 'text', 'network', '{}', '2026-05-09 13:00:00', NULL)
            """
        )
        self.db.conn.commit()
        manager = FeedManager(self.db, MagicMock())

        stale = manager.advance_post(
            'post-2',
            'owner',
            reason='old relay event',
            advanced_at=datetime(2026, 5, 9, 12, 0, tzinfo=timezone.utc),
        )

        self.assertTrue((stale or {}).get('unchanged'))
        self.assertEqual((stale or {}).get('last_activity_at'), '2026-05-09T13:00:00+00:00')
        stored = self.db.conn.execute("SELECT last_activity_at FROM feed_posts WHERE id = 'post-2'").fetchone()
        self.assertIsNone(stored['last_activity_at'])

    def test_channel_advance_resurfaces_root_thread_without_stale_rewind(self) -> None:
        self.db = _FakeDb()
        self.db.conn.executescript(
            """
            CREATE TABLE channels (
                id TEXT PRIMARY KEY,
                name TEXT,
                channel_type TEXT DEFAULT 'public',
                privacy_mode TEXT DEFAULT 'open',
                created_by TEXT,
                created_at TEXT,
                last_activity_at TEXT,
                lifecycle_archived_at TEXT,
                lifecycle_archive_reason TEXT
            );
            CREATE TABLE channel_messages (
                id TEXT PRIMARY KEY,
                channel_id TEXT,
                user_id TEXT,
                content TEXT,
                created_at TEXT,
                expires_at TEXT,
                parent_message_id TEXT,
                last_activity_at TEXT
            );
            """
        )
        self.db.conn.execute(
            "INSERT INTO channels (id, name, created_by, created_at, last_activity_at) VALUES ('general', 'general', 'owner', '2026-05-09 10:00:00', '2026-05-09 10:00:00')"
        )
        self.db.conn.execute(
            "INSERT INTO channel_messages (id, channel_id, user_id, content, created_at, last_activity_at) VALUES ('root-1', 'general', 'owner', 'Telemetry card', '2026-05-09 10:00:00', '2026-05-09 10:00:00')"
        )
        self.db.conn.execute(
            "INSERT INTO channel_messages (id, channel_id, user_id, content, created_at, parent_message_id) VALUES ('reply-1', 'general', 'owner', 'Nested update', '2026-05-09 10:05:00', 'root-1')"
        )
        self.db.conn.commit()

        manager = object.__new__(ChannelManager)
        manager.db = self.db
        manager.workspace_events = None
        manager.get_channel_access_decision = lambda **kwargs: {'allowed': True}
        manager.can_user_post_message = lambda **kwargs: {'allowed': True}

        advanced = manager.advance_message_thread(
            'general',
            'reply-1',
            'owner',
            reason='telemetry updated',
            advanced_at=datetime(2026, 5, 9, 12, 0, tzinfo=timezone.utc),
        )
        self.assertEqual((advanced or {}).get('root_message_id'), 'root-1')
        stored = self.db.conn.execute("SELECT last_activity_at FROM channel_messages WHERE id = 'root-1'").fetchone()
        self.assertEqual(stored['last_activity_at'], '2026-05-09 12:00:00')

        stale = manager.advance_message_thread(
            'general',
            'reply-1',
            'owner',
            reason='old relay event',
            advanced_at=datetime(2026, 5, 9, 11, 0, tzinfo=timezone.utc),
        )
        self.assertTrue((stale or {}).get('unchanged'))
        stored = self.db.conn.execute("SELECT last_activity_at FROM channel_messages WHERE id = 'root-1'").fetchone()
        self.assertEqual(stored['last_activity_at'], '2026-05-09 12:00:00')

    def test_channel_advance_does_not_rewind_channel_activity(self) -> None:
        self.db = _FakeDb()
        self.db.conn.executescript(
            """
            CREATE TABLE channels (
                id TEXT PRIMARY KEY,
                name TEXT,
                channel_type TEXT DEFAULT 'public',
                privacy_mode TEXT DEFAULT 'open',
                created_by TEXT,
                created_at TEXT,
                last_activity_at TEXT,
                lifecycle_archived_at TEXT,
                lifecycle_archive_reason TEXT
            );
            CREATE TABLE channel_messages (
                id TEXT PRIMARY KEY,
                channel_id TEXT,
                user_id TEXT,
                content TEXT,
                created_at TEXT,
                expires_at TEXT,
                parent_message_id TEXT,
                last_activity_at TEXT
            );
            """
        )
        self.db.conn.execute(
            "INSERT INTO channels (id, name, created_by, created_at, last_activity_at) VALUES ('ops', 'ops', 'owner', '2026-05-09 09:00:00', '2026-05-09 13:00:00')"
        )
        self.db.conn.execute(
            "INSERT INTO channel_messages (id, channel_id, user_id, content, created_at, last_activity_at) VALUES ('root-2', 'ops', 'owner', 'Telemetry card', '2026-05-09 10:00:00', NULL)"
        )
        self.db.conn.commit()

        manager = object.__new__(ChannelManager)
        manager.db = self.db
        manager.workspace_events = None
        manager.get_channel_access_decision = lambda **kwargs: {'allowed': True}
        manager.can_user_post_message = lambda **kwargs: {'allowed': True}

        stale = manager.advance_message_thread(
            'ops',
            'root-2',
            'owner',
            reason='old relay event',
            advanced_at=datetime(2026, 5, 9, 12, 0, tzinfo=timezone.utc),
        )

        self.assertTrue((stale or {}).get('unchanged'))
        stored_channel = self.db.conn.execute("SELECT last_activity_at FROM channels WHERE id = 'ops'").fetchone()
        stored_message = self.db.conn.execute("SELECT last_activity_at FROM channel_messages WHERE id = 'root-2'").fetchone()
        self.assertEqual(stored_channel['last_activity_at'], '2026-05-09 13:00:00')
        self.assertIsNone(stored_message['last_activity_at'])


if __name__ == '__main__':
    unittest.main()
