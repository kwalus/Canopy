"""Tests for feed unread-count persistence."""

import sqlite3
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from canopy.core.feed import FeedManager


class _FakeDb:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    @contextmanager
    def get_connection(self, *args, **kwargs):
        yield self._conn


class TestFeedUnreadState(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            """
            CREATE TABLE users (
                id TEXT PRIMARY KEY,
                username TEXT
            );
            CREATE TABLE feed_posts (
                id TEXT PRIMARY KEY,
                author_id TEXT NOT NULL,
                content TEXT NOT NULL,
                content_type TEXT DEFAULT 'text',
                metadata TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                expires_at TIMESTAMP,
                visibility TEXT DEFAULT 'network',
                likes INTEGER DEFAULT 0,
                comments INTEGER DEFAULT 0,
                shares INTEGER DEFAULT 0,
                last_activity_at TIMESTAMP
            );
            CREATE TABLE post_permissions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                post_id TEXT NOT NULL,
                user_id TEXT NOT NULL
            );
            CREATE TABLE user_feed_preferences (
                user_id TEXT PRIMARY KEY,
                algorithm_json TEXT NOT NULL DEFAULT '{}',
                last_viewed_at TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        self.conn.executemany(
            "INSERT INTO users (id, username) VALUES (?, ?)",
            [('viewer', 'viewer'), ('author-a', 'author-a'), ('author-b', 'author-b')],
        )

        now = datetime.now(timezone.utc).replace(microsecond=0)
        last_viewed = now - timedelta(hours=12)
        old_activity = now - timedelta(days=2)
        recent_one = now - timedelta(hours=2)
        recent_two = now - timedelta(minutes=30)

        self.conn.execute(
            "INSERT INTO user_feed_preferences (user_id, algorithm_json, last_viewed_at, updated_at) VALUES (?, ?, ?, ?)",
            ('viewer', '{}', last_viewed.strftime('%Y-%m-%d %H:%M:%S'), last_viewed.strftime('%Y-%m-%d %H:%M:%S')),
        )
        self.conn.executemany(
            """
            INSERT INTO feed_posts (
                id, author_id, content, created_at, last_activity_at, expires_at, visibility
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    'post-public-new',
                    'author-a',
                    'Public new',
                    recent_one.strftime('%Y-%m-%d %H:%M:%S'),
                    recent_one.strftime('%Y-%m-%d %H:%M:%S'),
                    (now + timedelta(days=7)).strftime('%Y-%m-%d %H:%M:%S'),
                    'public',
                ),
                (
                    'post-custom-new',
                    'author-b',
                    'Custom new',
                    recent_two.strftime('%Y-%m-%d %H:%M:%S'),
                    recent_two.strftime('%Y-%m-%d %H:%M:%S'),
                    (now + timedelta(days=7)).strftime('%Y-%m-%d %H:%M:%S'),
                    'custom',
                ),
                (
                    'post-old',
                    'author-a',
                    'Old activity',
                    old_activity.strftime('%Y-%m-%d %H:%M:%S'),
                    old_activity.strftime('%Y-%m-%d %H:%M:%S'),
                    (now + timedelta(days=7)).strftime('%Y-%m-%d %H:%M:%S'),
                    'public',
                ),
                (
                    'post-own',
                    'viewer',
                    'Own post',
                    recent_two.strftime('%Y-%m-%d %H:%M:%S'),
                    recent_two.strftime('%Y-%m-%d %H:%M:%S'),
                    (now + timedelta(days=7)).strftime('%Y-%m-%d %H:%M:%S'),
                    'public',
                ),
            ],
        )
        self.conn.execute(
            "INSERT INTO post_permissions (post_id, user_id) VALUES (?, ?)",
            ('post-custom-new', 'viewer'),
        )
        self.conn.commit()

        self.feed_manager = FeedManager(_FakeDb(self.conn), MagicMock())

    def tearDown(self) -> None:
        self.conn.close()

    def test_count_unread_posts_respects_last_viewed_time_and_permissions(self) -> None:
        self.assertEqual(self.feed_manager.count_unread_posts('viewer'), 2)

    def test_mark_feed_viewed_clears_unread_count(self) -> None:
        now = datetime.now(timezone.utc).replace(microsecond=0)
        self.assertTrue(self.feed_manager.mark_feed_viewed('viewer', now))
        self.assertEqual(self.feed_manager.count_unread_posts('viewer'), 0)


if __name__ == '__main__':
    unittest.main()
