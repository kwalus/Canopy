"""Regression tests for FK-safe synced channel creator assignment."""

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
        self.conn.execute('PRAGMA foreign_keys = ON')
        self.conn.execute(
            """
            CREATE TABLE users (
                id TEXT PRIMARY KEY,
                username TEXT UNIQUE NOT NULL,
                public_key TEXT NOT NULL,
                password_hash TEXT,
                display_name TEXT,
                origin_peer TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        self.conn.execute(
            """
            INSERT INTO users (id, username, public_key, password_hash, display_name)
            VALUES (?, ?, ?, ?, ?)
            """,
            ('owner-user', 'owner', 'pk-owner', 'hash-owner', 'Owner User'),
        )
        self.conn.commit()

    def get_connection(self):
        return self.conn

    def get_instance_owner_user_id(self):
        return 'owner-user'

    def get_user(self, user_id: str):
        row = self.conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return dict(row) if row else None


class TestChannelSyncCreatorFK(unittest.TestCase):
    def setUp(self) -> None:
        self.db = _FakeDbManager()
        self.channel_manager = ChannelManager(self.db, MagicMock())

    def tearDown(self) -> None:
        self.db.conn.close()

    def test_create_channel_from_sync_uses_existing_user_for_created_by_fk(self) -> None:
        channel = self.channel_manager.create_channel_from_sync(
            channel_id='Csyncfk001',
            name='sync-fk-private',
            channel_type='private',
            description='synced private channel',
            local_user_id=None,
            origin_peer='peer-remote',
            privacy_mode='private',
            initial_members=['owner-user'],
        )
        self.assertIsNotNone(channel)

        row = self.db.conn.execute(
            "SELECT created_by FROM channels WHERE id = ?",
            ('Csyncfk001',),
        ).fetchone()
        self.assertIsNotNone(row)
        creator_id = row['created_by']
        self.assertNotEqual(creator_id, 'p2p-sync')

        user_row = self.db.conn.execute(
            "SELECT id FROM users WHERE id = ?",
            (creator_id,),
        ).fetchone()
        self.assertIsNotNone(user_row)

    def test_create_channel_from_sync_prefers_origin_peer_shadow_user_when_available(self) -> None:
        self.db.conn.execute(
            """
            INSERT INTO users (id, username, public_key, password_hash, display_name, origin_peer)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            ('remote-owner', 'remote_owner', 'pk-remote-owner', 'hash-remote-owner', 'Remote Owner', 'peer-origin'),
        )
        self.db.conn.commit()

        channel = self.channel_manager.create_channel_from_sync(
            channel_id='Csyncfk-origin',
            name='sync-origin-owned',
            channel_type='private',
            description='synced private channel with explicit remote owner peer',
            local_user_id='owner-user',
            origin_peer='peer-origin',
            privacy_mode='private',
            initial_members=['owner-user'],
        )
        self.assertIsNotNone(channel)

        row = self.db.conn.execute(
            "SELECT created_by, origin_peer FROM channels WHERE id = ?",
            ('Csyncfk-origin',),
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row['origin_peer'], 'peer-origin')
        self.assertEqual(row['created_by'], 'remote-owner')

    def test_merge_or_adopt_empty_conflict_uses_fk_safe_creator(self) -> None:
        local_channel = self.channel_manager.create_channel(
            name='sync-fk-conflict',
            channel_type=ChannelType.PUBLIC,
            created_by='owner-user',
            description='local placeholder',
            privacy_mode='open',
        )
        self.assertIsNotNone(local_channel)

        adopted_id = self.channel_manager.merge_or_adopt_channel(
            remote_id='Csyncfk002',
            remote_name='sync-fk-conflict',
            remote_type='public',
            remote_desc='remote authoritative',
            local_user_id='owner-user',
            from_peer='peer-origin',
            privacy_mode='open',
        )
        self.assertEqual(adopted_id, 'Csyncfk002')

        row = self.db.conn.execute(
            "SELECT created_by, origin_peer FROM channels WHERE id = ?",
            ('Csyncfk002',),
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertNotEqual(row['created_by'], 'p2p-sync')
        self.assertEqual(row['origin_peer'], 'peer-origin')

        user_row = self.db.conn.execute(
            "SELECT id FROM users WHERE id = ?",
            (row['created_by'],),
        ).fetchone()
        self.assertIsNotNone(user_row)


if __name__ == '__main__':
    unittest.main()
