"""Regression tests for non-destructive channel lifecycle behavior."""

import os
import sqlite3
import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

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
            "INSERT INTO users (id, username, public_key, password_hash, origin_peer) VALUES (?, ?, ?, ?, ?)",
            [
                ('owner-user', 'owner', 'pk-owner', 'pw-owner', None),
                ('member-user', 'member', 'pk-member', 'pw-member', None),
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


class _LegacyChannelSchemaDbManager(_FakeDbManager):
    def __init__(self) -> None:
        super().__init__()
        self.conn.execute("DROP TABLE IF EXISTS channels")
        self.conn.execute(
            """
            CREATE TABLE channels (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                channel_type TEXT NOT NULL,
                created_by TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                description TEXT,
                topic TEXT,
                crypto_mode TEXT DEFAULT 'legacy_plaintext'
            )
            """
        )
        self.conn.commit()


class TestChannelLifecycle(unittest.TestCase):
    def setUp(self) -> None:
        self.db = _FakeDbManager()
        self.channel_manager = ChannelManager(self.db, MagicMock())

    def tearDown(self) -> None:
        self.db.conn.close()

    def test_touch_channel_activity_revives_archived_channel(self) -> None:
        channel = self.channel_manager.create_channel(
            name='ops-lifecycle',
            channel_type=ChannelType.PUBLIC,
            created_by='owner-user',
            description='lifecycle test',
            origin_peer='peer-local',
        )
        self.assertIsNotNone(channel)

        archived = self.channel_manager.update_channel_lifecycle_settings(
            channel_id=channel.id,
            user_id='owner-user',
            archived=True,
            allow_admin=True,
            local_peer_id='peer-local',
        )
        self.assertIsNotNone(archived)
        self.assertIsNotNone(archived['archived_at'])

        touched = self.channel_manager.touch_channel_activity(channel.id)
        self.assertTrue(touched)

        row = self.db.conn.execute(
            "SELECT lifecycle_archived_at, lifecycle_archive_reason, last_activity_at FROM channels WHERE id = ?",
            (channel.id,),
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertIsNone(row['lifecycle_archived_at'])
        self.assertIsNone(row['lifecycle_archive_reason'])
        self.assertIsNotNone(row['last_activity_at'])

    def test_describe_channel_lifecycle_marks_cooling_window(self) -> None:
        channel = self.channel_manager.create_channel(
            name='cooling-window',
            channel_type=ChannelType.PUBLIC,
            created_by='owner-user',
            description='cooling test',
            origin_peer='peer-local',
        )
        self.assertIsNotNone(channel)

        now = datetime.now(timezone.utc)
        last_activity = now - timedelta(days=174)
        self.db.conn.execute(
            "UPDATE channels SET last_activity_at = ?, lifecycle_ttl_days = ? WHERE id = ?",
            (last_activity.isoformat(), 180, channel.id),
        )
        self.db.conn.commit()

        refreshed = next(ch for ch in self.channel_manager.get_user_channels('owner-user') if ch.id == channel.id)
        lifecycle = self.channel_manager.describe_channel_lifecycle(refreshed, now=now)

        self.assertEqual(lifecycle['status'], 'cooling')
        self.assertEqual(lifecycle['ttl_days'], 180)
        self.assertIsNotNone(lifecycle['days_until_archive'])
        self.assertLessEqual(lifecycle['days_until_archive'], self.channel_manager.CHANNEL_LIFECYCLE_WARNING_DAYS)

    def test_update_channel_lifecycle_requires_local_origin(self) -> None:
        channel = self.channel_manager.create_channel(
            name='remote-origin',
            channel_type=ChannelType.PUBLIC,
            created_by='owner-user',
            description='remote origin test',
            origin_peer='peer-remote',
        )
        self.assertIsNotNone(channel)

        result = self.channel_manager.update_channel_lifecycle_settings(
            channel_id=channel.id,
            user_id='owner-user',
            ttl_days=30,
            allow_admin=True,
            local_peer_id='peer-local',
        )

        self.assertIsNone(result)

    def test_instance_admin_can_manage_local_origin_channel_without_membership_row(self) -> None:
        channel = self.channel_manager.create_channel(
            name='admin-override',
            channel_type=ChannelType.PUBLIC,
            created_by='owner-user',
            description='admin override test',
            origin_peer='peer-local',
        )
        self.assertIsNotNone(channel)

        self.db.conn.execute(
            "DELETE FROM channel_members WHERE channel_id = ? AND user_id = ?",
            (channel.id, 'owner-user'),
        )
        self.db.conn.commit()

        result = self.channel_manager.update_channel_lifecycle_settings(
            channel_id=channel.id,
            user_id='owner-user',
            ttl_days=365,
            allow_admin=True,
            local_peer_id='peer-local',
        )

        self.assertIsNotNone(result)
        self.assertEqual(result['ttl_days'], 365)

    def test_ensure_tables_migrates_legacy_channel_schema_before_indexes(self) -> None:
        legacy_db = _LegacyChannelSchemaDbManager()
        try:
            manager = ChannelManager(legacy_db, MagicMock())
            row = legacy_db.conn.execute(
                "SELECT last_activity_at, lifecycle_ttl_days, lifecycle_preserved, lifecycle_archived_at, lifecycle_archive_reason FROM channels LIMIT 1"
            ).fetchone()
            self.assertIsNotNone(row)
            idx_names = {
                r[0]
                for r in legacy_db.conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'").fetchall()
            }
            self.assertIn('idx_channels_last_activity', idx_names)
            self.assertIn('idx_channels_lifecycle_archived', idx_names)
            self.assertIsNotNone(manager)
        finally:
            legacy_db.conn.close()


if __name__ == '__main__':
    unittest.main()
