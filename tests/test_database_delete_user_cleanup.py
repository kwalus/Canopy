"""Regression tests for DatabaseManager.delete_user cleanup and FK safety."""

import os
import sqlite3
import sys
import unittest

# Ensure repository root is importable when running tests directly.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from canopy.core.database import DatabaseManager


class _DeleteUserHarness(DatabaseManager):
    """Lightweight harness that reuses DatabaseManager.delete_user logic."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def get_connection(self, busy_timeout_ms: int = 5000):
        return self._conn


class TestDatabaseDeleteUserCleanup(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        self.conn.execute('PRAGMA foreign_keys = ON')
        self.conn.executescript(
            """
            CREATE TABLE users (
                id TEXT PRIMARY KEY,
                username TEXT NOT NULL,
                public_key TEXT NOT NULL
            );
            CREATE TABLE system_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE channels (
                id TEXT PRIMARY KEY,
                created_by TEXT NOT NULL,
                FOREIGN KEY (created_by) REFERENCES users(id)
            );
            CREATE TABLE api_keys (id TEXT PRIMARY KEY, user_id TEXT);
            CREATE TABLE user_keys (user_id TEXT PRIMARY KEY);
            CREATE TABLE channel_members (channel_id TEXT, user_id TEXT);
            CREATE TABLE post_permissions (post_id TEXT, user_id TEXT);
            CREATE TABLE post_content_keys (post_id TEXT, user_id TEXT);
            CREATE TABLE agent_inbox (id TEXT PRIMARY KEY, agent_user_id TEXT, sender_user_id TEXT);
            CREATE TABLE agent_inbox_config (user_id TEXT PRIMARY KEY);
            CREATE TABLE user_feed_preferences (user_id TEXT PRIMARY KEY);
            CREATE TABLE messages (id TEXT PRIMARY KEY, sender_id TEXT);
            CREATE TABLE feed_posts (id TEXT PRIMARY KEY, author_id TEXT);
            CREATE TABLE content_contexts (id TEXT PRIMARY KEY, owner_user_id TEXT);
            CREATE TABLE mention_events (id TEXT PRIMARY KEY, user_id TEXT, author_id TEXT);
            CREATE TABLE channel_messages (id TEXT PRIMARY KEY, user_id TEXT, parent_message_id TEXT);
            CREATE TABLE likes (id TEXT PRIMARY KEY, message_id TEXT, user_id TEXT);
            """
        )
        self.db = _DeleteUserHarness(self.conn)

    def tearDown(self) -> None:
        self.conn.close()

    def test_delete_user_reassigns_owned_channels_to_instance_owner(self) -> None:
        self.conn.executemany(
            "INSERT INTO users (id, username, public_key) VALUES (?, ?, ?)",
            [
                ('system', 'System', 'pk-system'),
                ('owner-user', 'Owner', 'pk-owner'),
                ('victim-user', 'Victim', 'pk-victim'),
            ],
        )
        self.conn.execute(
            "INSERT INTO system_state (key, value) VALUES ('instance_owner_id', ?)",
            ('owner-user',),
        )
        self.conn.execute(
            "INSERT INTO channels (id, created_by) VALUES (?, ?)",
            ('C_owned', 'victim-user'),
        )
        self.conn.execute(
            "INSERT INTO channel_messages (id, user_id, parent_message_id) VALUES (?, ?, NULL)",
            ('M_owned', 'victim-user'),
        )
        self.conn.execute(
            "INSERT INTO likes (id, message_id, user_id) VALUES (?, ?, ?)",
            ('L1', 'M_owned', 'owner-user'),
        )
        self.conn.commit()

        self.assertTrue(self.db.delete_user('victim-user'))

        victim_row = self.conn.execute(
            "SELECT 1 FROM users WHERE id = ?",
            ('victim-user',),
        ).fetchone()
        channel_row = self.conn.execute(
            "SELECT created_by FROM channels WHERE id = ?",
            ('C_owned',),
        ).fetchone()
        self.assertIsNone(victim_row)
        self.assertIsNotNone(channel_row)
        assert channel_row is not None
        self.assertEqual(channel_row['created_by'], 'owner-user')

    def test_delete_user_returns_false_when_missing(self) -> None:
        self.conn.execute(
            "INSERT INTO users (id, username, public_key) VALUES (?, ?, ?)",
            ('system', 'System', 'pk-system'),
        )
        self.conn.commit()

        self.assertFalse(self.db.delete_user('missing-user'))


if __name__ == '__main__':
    unittest.main()
