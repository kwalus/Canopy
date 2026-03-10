"""Regression tests for channel privacy consistency during P2P sync merges."""

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
        self.conn.executemany(
            "INSERT INTO users (id, username, public_key, password_hash, origin_peer) "
            "VALUES (?, ?, ?, ?, ?)",
            [
                ('owner-user', 'owner', 'pk-owner', 'hash-owner', None),
                ('member-user', 'member', 'pk-member', 'hash-member', None),
            ],
        )
        self.conn.commit()

    def get_connection(self):
        return self.conn

    def get_instance_owner_user_id(self):
        return 'owner-user'

    def get_user(self, user_id: str):
        row = self.conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return dict(row) if row else None


class TestChannelPrivacyConsistency(unittest.TestCase):
    def setUp(self) -> None:
        self.db = _FakeDbManager()
        self.channel_manager = ChannelManager(self.db, MagicMock())

    def tearDown(self) -> None:
        self.db.conn.close()

    def _get_channel_row(self, channel_id: str):
        return self.db.conn.execute(
            "SELECT id, privacy_mode, origin_peer FROM channels WHERE id = ?",
            (channel_id,),
        ).fetchone()

    def test_non_origin_announce_cannot_downgrade_private_channel(self) -> None:
        channel = self.channel_manager.create_channel(
            name='private-sync-test',
            channel_type=ChannelType.PRIVATE,
            created_by='owner-user',
            description='private channel',
            privacy_mode='private',
            origin_peer='peer-local',
        )
        self.assertIsNotNone(channel)
        channel_id = channel.id

        self.channel_manager.merge_or_adopt_channel(
            remote_id=channel_id,
            remote_name='private-sync-test',
            remote_type='private',
            remote_desc='remote attempted update',
            local_user_id='owner-user',
            from_peer='peer-remote',
            privacy_mode='open',
        )

        row = self._get_channel_row(channel_id)
        self.assertIsNotNone(row)
        self.assertEqual(row['privacy_mode'], 'private')
        self.assertEqual(row['origin_peer'], 'peer-local')

    def test_origin_announce_can_apply_privacy_change(self) -> None:
        channel = self.channel_manager.create_channel(
            name='origin-sync-test',
            channel_type=ChannelType.PRIVATE,
            created_by='owner-user',
            description='origin controlled',
            privacy_mode='private',
            origin_peer='peer-local',
        )
        self.assertIsNotNone(channel)
        channel_id = channel.id

        self.channel_manager.merge_or_adopt_channel(
            remote_id=channel_id,
            remote_name='origin-sync-test',
            remote_type='private',
            remote_desc='origin initiated update',
            local_user_id='owner-user',
            from_peer='peer-local',
            privacy_mode='open',
        )

        row = self._get_channel_row(channel_id)
        self.assertIsNotNone(row)
        self.assertEqual(row['privacy_mode'], 'open')
        self.assertEqual(row['origin_peer'], 'peer-local')

    def test_legacy_synced_channel_with_unknown_origin_blocks_downgrade(self) -> None:
        self.db.conn.execute(
            """
            INSERT INTO channels (id, name, channel_type, created_by, description, privacy_mode, origin_peer)
            VALUES (?, ?, ?, ?, ?, ?, NULL)
            """,
            (
                'Clegacy123',
                'peer-channel-legacy',
                'public',
                'p2p-sync',
                'Auto-created from P2P',
                'private',
            ),
        )
        self.db.conn.execute(
            "INSERT INTO channel_members (channel_id, user_id, role) VALUES (?, ?, ?)",
            ('Clegacy123', 'owner-user', 'admin'),
        )
        self.db.conn.commit()

        self.channel_manager.merge_or_adopt_channel(
            remote_id='Clegacy123',
            remote_name='legacy-channel',
            remote_type='public',
            remote_desc='real description',
            local_user_id='owner-user',
            from_peer='peer-remote',
            privacy_mode='open',
        )

        row = self._get_channel_row('Clegacy123')
        self.assertIsNotNone(row)
        self.assertEqual(row['privacy_mode'], 'private')
        self.assertEqual(row['origin_peer'], 'peer-remote')

    def test_origin_sync_updates_last_activity_timestamp(self) -> None:
        channel = self.channel_manager.create_channel(
            name='activity-sync-test',
            channel_type=ChannelType.PUBLIC,
            created_by='owner-user',
            description='activity sync test',
            privacy_mode='open',
            origin_peer='peer-local',
        )
        self.assertIsNotNone(channel)

        older_activity = '2026-03-01T00:00:00+00:00'
        newer_activity = '2026-03-09T15:30:00+00:00'
        self.db.conn.execute(
            "UPDATE channels SET last_activity_at = ? WHERE id = ?",
            (older_activity, channel.id),
        )
        self.db.conn.commit()

        self.channel_manager.merge_or_adopt_channel(
            remote_id=channel.id,
            remote_name='activity-sync-test',
            remote_type='public',
            remote_desc='updated desc',
            local_user_id='owner-user',
            from_peer='peer-local',
            privacy_mode='open',
            last_activity_at=newer_activity,
        )

        row = self.db.conn.execute(
            "SELECT last_activity_at FROM channels WHERE id = ?",
            (channel.id,),
        ).fetchone()
        self.assertIsNotNone(row)
        stored = self.channel_manager._parse_datetime(row['last_activity_at'])
        self.assertIsNotNone(stored)
        self.assertEqual(stored.isoformat(), newer_activity)


if __name__ == '__main__':
    unittest.main()
