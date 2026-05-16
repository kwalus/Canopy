"""Regression tests for recursive parent hydration in channel message fetches."""

import os
import sqlite3
import sys
import types
import unittest
from unittest.mock import MagicMock

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

from canopy.core.channels import ChannelManager, ChannelType


class _FakeDbManager:
    def __init__(self) -> None:
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(
            """
            CREATE TABLE users (
                id TEXT PRIMARY KEY,
                username TEXT,
                public_key TEXT,
                password_hash TEXT,
                origin_peer TEXT
            )
            """
        )
        self.conn.execute(
            """
            INSERT INTO users (id, username, public_key, password_hash, origin_peer)
            VALUES (?, ?, ?, ?, ?)
            """,
            ('owner-user', 'owner', 'pk-owner', 'hash-owner', None),
        )
        self.conn.commit()

    def get_connection(self):
        return self.conn

    def get_instance_owner_user_id(self):
        return 'owner-user'

    def get_user(self, user_id: str):
        row = self.conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return dict(row) if row else None


class TestChannelThreadParentHydration(unittest.TestCase):
    def setUp(self) -> None:
        self.db = _FakeDbManager()
        self.channel_manager = ChannelManager(self.db, MagicMock())

    def tearDown(self) -> None:
        self.db.conn.close()

    def test_get_channel_messages_hydrates_full_ancestor_chain(self) -> None:
        channel = self.channel_manager.create_channel(
            name='thread-hydration',
            channel_type=ChannelType.PUBLIC,
            created_by='owner-user',
            description='thread chain test',
            privacy_mode='open',
        )
        self.assertIsNotNone(channel)
        assert channel is not None

        root = self.channel_manager.send_message(
            channel_id=channel.id,
            user_id='owner-user',
            content='root',
        )
        self.assertIsNotNone(root)
        assert root is not None

        reply_one = self.channel_manager.send_message(
            channel_id=channel.id,
            user_id='owner-user',
            content='reply one',
            parent_message_id=root.id,
        )
        self.assertIsNotNone(reply_one)
        assert reply_one is not None

        reply_two = self.channel_manager.send_message(
            channel_id=channel.id,
            user_id='owner-user',
            content='reply two',
            parent_message_id=reply_one.id,
        )
        self.assertIsNotNone(reply_two)
        assert reply_two is not None

        # Force deterministic ordering so the newest page contains only the
        # deepest reply and requires recursive parent hydration.
        self.db.conn.execute(
            "UPDATE channel_messages SET created_at = ?, last_activity_at = NULL WHERE id = ?",
            ('2026-01-01 00:00:00', root.id),
        )
        self.db.conn.execute(
            "UPDATE channel_messages SET created_at = ?, last_activity_at = NULL WHERE id = ?",
            ('2026-01-01 00:00:01', reply_one.id),
        )
        self.db.conn.execute(
            "UPDATE channel_messages SET created_at = ?, last_activity_at = NULL WHERE id = ?",
            ('2026-01-01 00:00:02', reply_two.id),
        )
        self.db.conn.commit()

        # Fetch with a very small page so only the newest reply is in the primary query.
        # The manager should recursively include missing ancestors.
        messages = self.channel_manager.get_channel_messages(
            channel_id=channel.id,
            user_id='owner-user',
            limit=1,
        )

        by_id = {m.id: m for m in messages}
        self.assertIn(reply_two.id, by_id)
        self.assertIn(reply_one.id, by_id)
        self.assertIn(root.id, by_id)
        self.assertEqual(by_id[reply_two.id].parent_message_id, reply_one.id)
        self.assertEqual(by_id[reply_one.id].parent_message_id, root.id)

    def test_get_channel_messages_by_ids_hydrates_target_and_parents_only(self) -> None:
        channel = self.channel_manager.create_channel(
            name='targeted-thread-hydration',
            channel_type=ChannelType.PUBLIC,
            created_by='owner-user',
            description='targeted thread chain test',
            privacy_mode='open',
        )
        self.assertIsNotNone(channel)
        assert channel is not None

        root = self.channel_manager.send_message(
            channel_id=channel.id,
            user_id='owner-user',
            content='root',
        )
        reply_one = self.channel_manager.send_message(
            channel_id=channel.id,
            user_id='owner-user',
            content='reply one',
            parent_message_id=root.id if root else None,
        )
        reply_two = self.channel_manager.send_message(
            channel_id=channel.id,
            user_id='owner-user',
            content='reply two',
            parent_message_id=reply_one.id if reply_one else None,
        )
        unrelated = self.channel_manager.send_message(
            channel_id=channel.id,
            user_id='owner-user',
            content='unrelated',
        )
        self.assertIsNotNone(root)
        self.assertIsNotNone(reply_one)
        self.assertIsNotNone(reply_two)
        self.assertIsNotNone(unrelated)
        assert root is not None
        assert reply_one is not None
        assert reply_two is not None
        assert unrelated is not None

        messages = self.channel_manager.get_channel_messages_by_ids(
            channel_id=channel.id,
            user_id='owner-user',
            message_ids=[reply_two.id],
        )

        by_id = {message.id: message for message in messages}
        self.assertIn(reply_two.id, by_id)
        self.assertIn(reply_one.id, by_id)
        self.assertIn(root.id, by_id)
        self.assertNotIn(unrelated.id, by_id)
        self.assertEqual(by_id[reply_two.id].parent_message_id, reply_one.id)
        self.assertEqual(by_id[reply_one.id].parent_message_id, root.id)


if __name__ == '__main__':
    unittest.main()
