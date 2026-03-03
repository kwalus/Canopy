"""Regression tests for private-channel mention membership enforcement."""

import os
import sqlite3
import sys
import types
import unittest
from datetime import datetime, timezone

# Ensure repository root is importable when running tests directly.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

# Provide a lightweight zeroconf stub for environments without optional deps.
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

from canopy.core.inbox import InboxManager
from canopy.core.mentions import MentionManager, record_mention_activity


class _FakeDbManager:
    def __init__(self) -> None:
        self.conn = sqlite3.connect(':memory:')
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
                origin_peer TEXT
            );
            CREATE TABLE channels (
                id TEXT PRIMARY KEY,
                name TEXT,
                privacy_mode TEXT
            );
            CREATE TABLE channel_members (
                channel_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                role TEXT DEFAULT 'member',
                notifications_enabled INTEGER DEFAULT 1
            );
            CREATE TABLE channel_messages (
                id TEXT PRIMARY KEY,
                channel_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                content TEXT,
                created_at TEXT,
                origin_peer TEXT,
                expires_at TEXT
            );
            """
        )
        self.conn.executemany(
            """
            INSERT INTO users (
                id, username, display_name, public_key, password_hash, account_type, origin_peer
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                ('author', 'Author', 'Author User', 'pk-author', 'pw', 'human', None),
                ('member', 'Member', 'Member User', 'pk-member', 'pw', 'agent', 'peer-a'),
                ('nonmember', 'NonMember', 'Non Member', 'pk-nonmember', 'pw', 'agent', 'peer-b'),
            ],
        )
        self.conn.execute(
            "INSERT INTO channels (id, name, privacy_mode) VALUES (?, ?, ?)",
            ('Cprivate', 'private-test', 'private'),
        )
        self.conn.executemany(
            "INSERT INTO channel_members (channel_id, user_id, role, notifications_enabled) VALUES (?, ?, ?, ?)",
            [
                ('Cprivate', 'author', 'admin', 1),
                ('Cprivate', 'member', 'member', 1),
            ],
        )
        self.conn.commit()

    def get_connection(self):
        return self.conn


class TestPrivateChannelMentionMembership(unittest.TestCase):
    def setUp(self) -> None:
        self.db = _FakeDbManager()
        self.mention_manager = MentionManager(self.db)
        self.inbox_manager = InboxManager(self.db)

    def tearDown(self) -> None:
        self.db.conn.close()

    def test_record_mention_activity_drops_targets_without_channel_membership(self) -> None:
        record_mention_activity(
            mention_manager=self.mention_manager,
            p2p_manager=None,
            target_ids=['member', 'nonmember'],
            source_type='channel_message',
            source_id='Mprivate1',
            author_id='author',
            origin_peer='peer-origin',
            channel_id='Cprivate',
            preview='hello',
            inbox_manager=self.inbox_manager,
            source_content='@Member @NonMember hello',
        )

        mention_rows = self.db.conn.execute(
            "SELECT user_id FROM mention_events WHERE source_id = ? ORDER BY user_id",
            ('Mprivate1',),
        ).fetchall()
        inbox_rows = self.db.conn.execute(
            "SELECT agent_user_id FROM agent_inbox WHERE source_id = ? ORDER BY agent_user_id",
            ('Mprivate1',),
        ).fetchall()

        self.assertEqual([row['user_id'] for row in mention_rows], ['member'])
        self.assertEqual([row['agent_user_id'] for row in inbox_rows], ['member'])

    def test_rebuild_from_channel_messages_scans_only_member_channels(self) -> None:
        now_iso = datetime.now(timezone.utc).isoformat()
        self.db.conn.executemany(
            """
            INSERT INTO channel_messages
            (id, channel_id, user_id, content, created_at, origin_peer, expires_at)
            VALUES (?, ?, ?, ?, ?, ?, NULL)
            """,
            [
                ('Mallowed', 'Cprivate', 'author', '@Member please review', now_iso, 'peer-origin'),
                ('Mhidden', 'Cother', 'author', '@Member should stay hidden', now_iso, 'peer-origin'),
            ],
        )
        self.db.conn.commit()

        result = self.inbox_manager.rebuild_from_channel_messages(
            user_id='member',
            username='Member',
            display_name='Member User',
            window_hours=24,
            limit=50,
        )
        self.assertGreaterEqual(int(result.get('scanned') or 0), 1)
        self.assertGreaterEqual(int(result.get('created') or 0), 1)

        source_ids = [
            row['source_id']
            for row in self.db.conn.execute(
                "SELECT source_id FROM agent_inbox WHERE agent_user_id = ? ORDER BY source_id",
                ('member',),
            ).fetchall()
        ]
        self.assertIn('Mallowed', source_ids)
        self.assertNotIn('Mhidden', source_ids)

    def test_record_mention_activity_respects_channel_mute(self) -> None:
        self.db.conn.execute(
            "UPDATE channel_members SET notifications_enabled = 0 WHERE channel_id = ? AND user_id = ?",
            ('Cprivate', 'member'),
        )
        self.db.conn.commit()

        record_mention_activity(
            mention_manager=self.mention_manager,
            p2p_manager=None,
            target_ids=['member'],
            source_type='channel_message',
            source_id='Mprivate-muted',
            author_id='author',
            origin_peer='peer-origin',
            channel_id='Cprivate',
            preview='hello muted',
            inbox_manager=self.inbox_manager,
            source_content='@Member muted mention',
        )

        mention_rows = self.db.conn.execute(
            "SELECT user_id FROM mention_events WHERE source_id = ?",
            ('Mprivate-muted',),
        ).fetchall()
        inbox_rows = self.db.conn.execute(
            "SELECT agent_user_id FROM agent_inbox WHERE source_id = ?",
            ('Mprivate-muted',),
        ).fetchall()
        self.assertEqual(len(mention_rows), 0)
        self.assertEqual(len(inbox_rows), 0)


if __name__ == '__main__':
    unittest.main()
