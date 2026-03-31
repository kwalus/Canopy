"""Regression tests for per-user channel governance controls."""

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
        self._system_state = {}
        self.conn.execute(
            """
            CREATE TABLE users (
                id TEXT PRIMARY KEY,
                username TEXT,
                public_key TEXT,
                password_hash TEXT,
                origin_peer TEXT,
                account_type TEXT DEFAULT 'human',
                created_at TIMESTAMP
            )
            """
        )
        self.conn.executemany(
            """
            INSERT INTO users (id, username, public_key, password_hash, origin_peer, account_type, created_at)
            VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            """,
            [
                ('owner-user', 'owner', 'pk-owner', 'hash-owner', None, 'human'),
                ('agent-user', 'agent', 'pk-agent', 'hash-agent', None, 'agent'),
                ('member-user', 'member', 'pk-member', 'hash-member', None, 'human'),
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

    def get_system_state(self, key: str):
        return self._system_state.get(key)

    def set_system_state(self, key: str, value):
        if value is None:
            self._system_state.pop(key, None)
        else:
            self._system_state[key] = value
        return True


class TestChannelGovernance(unittest.TestCase):
    def setUp(self) -> None:
        self.db = _FakeDbManager()
        self.channel_manager = ChannelManager(self.db, MagicMock())

        # Ensure test user has membership in general so policy can prune it.
        self.db.conn.execute(
            "INSERT OR IGNORE INTO channel_members (channel_id, user_id, role) VALUES (?, ?, ?)",
            ('general', 'agent-user', 'member'),
        )
        self.db.conn.commit()

    def tearDown(self) -> None:
        self.db.conn.close()

    def test_default_agent_start_here_channel_exists(self) -> None:
        self.assertNotEqual(
            self.channel_manager.AGENT_START_CHANNEL_ID,
            self.channel_manager.LEGACY_AGENT_START_CHANNEL_ID,
        )
        row = self.db.conn.execute(
            """
            SELECT id, name, channel_type, privacy_mode, lifecycle_preserved
            FROM channels
            WHERE id = ?
            """,
            (self.channel_manager.AGENT_START_CHANNEL_ID,),
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row['name'], self.channel_manager.AGENT_START_CHANNEL_NAME)
        self.assertEqual(row['channel_type'], 'private')
        self.assertEqual(row['privacy_mode'], 'private')
        self.assertEqual(int(row['lifecycle_preserved'] or 0), 1)

        members = self.db.conn.execute(
            "SELECT user_id FROM channel_members WHERE channel_id = ? ORDER BY user_id",
            (self.channel_manager.AGENT_START_CHANNEL_ID,),
        ).fetchall()
        member_ids = [row['user_id'] for row in members]
        self.assertIn('local_user', member_ids)
        self.assertIn('owner-user', member_ids)

    def test_ensure_agent_quarantine_assignment_sets_allowlist_policy(self) -> None:
        saved = self.channel_manager.ensure_agent_quarantine_assignment(
            'agent-user',
            updated_by='owner-user',
        )
        self.assertTrue(saved)

        policy = self.channel_manager.get_user_channel_governance('agent-user')
        self.assertTrue(policy.get('enabled'))
        self.assertTrue(policy.get('block_public_channels'))
        self.assertTrue(policy.get('restrict_to_allowed_channels'))
        self.assertEqual(
            policy.get('allowed_channel_ids'),
            [self.channel_manager.AGENT_START_CHANNEL_ID],
        )

        visible = self.channel_manager.get_user_channels('agent-user')
        visible_ids = {channel.id for channel in visible}
        self.assertIn(self.channel_manager.AGENT_START_CHANNEL_ID, visible_ids)
        self.assertNotIn('general', visible_ids)

        denied = self.channel_manager.send_message(
            channel_id='general',
            user_id='agent-user',
            content='should stay quarantined',
        )
        self.assertIsNone(denied)

    def test_governance_filters_and_prunes_public_channels(self) -> None:
        open_channel = self.channel_manager.create_channel(
            name='public-work',
            channel_type=ChannelType.PUBLIC,
            created_by='owner-user',
            description='open channel',
            privacy_mode='open',
        )
        self.assertIsNotNone(open_channel)
        private_channel = self.channel_manager.create_channel(
            name='private-work',
            channel_type=ChannelType.PRIVATE,
            created_by='owner-user',
            description='restricted',
            privacy_mode='private',
        )
        self.assertIsNotNone(private_channel)
        assert open_channel is not None
        assert private_channel is not None

        self.assertTrue(
            self.channel_manager.add_member(
                channel_id=open_channel.id,
                target_user_id='agent-user',
                requester_id='owner-user',
            )
        )
        self.assertTrue(
            self.channel_manager.add_member(
                channel_id=private_channel.id,
                target_user_id='agent-user',
                requester_id='owner-user',
            )
        )

        saved = self.channel_manager.set_user_channel_governance(
            user_id='agent-user',
            enabled=True,
            block_public_channels=True,
            restrict_to_allowed_channels=True,
            allowed_channel_ids=[private_channel.id],
            updated_by='owner-user',
        )
        self.assertTrue(saved)

        visible = self.channel_manager.get_user_channels('agent-user')
        visible_ids = {channel.id for channel in visible}
        self.assertIn(private_channel.id, visible_ids)
        self.assertNotIn('general', visible_ids)
        self.assertNotIn(open_channel.id, visible_ids)

        denied_msg = self.channel_manager.send_message(
            channel_id=open_channel.id,
            user_id='agent-user',
            content='should not post to public channel',
        )
        self.assertIsNone(denied_msg)
        denied_read = self.channel_manager.get_channel_messages(
            channel_id=open_channel.id,
            user_id='agent-user',
            limit=10,
        )
        self.assertEqual(denied_read, [])

        enforcement = self.channel_manager.enforce_user_channel_governance('agent-user')
        self.assertGreaterEqual(int(enforcement.get('removed_count') or 0), 1)

        member_rows = self.db.conn.execute(
            """
            SELECT channel_id
            FROM channel_members
            WHERE user_id = ?
            ORDER BY channel_id
            """,
            ('agent-user',),
        ).fetchall()
        channel_ids = [row['channel_id'] for row in member_rows]
        self.assertEqual(channel_ids, [private_channel.id])

    def test_sync_public_channel_auto_membership_skips_restricted_user(self) -> None:
        self.assertTrue(
            self.channel_manager.set_user_channel_governance(
                user_id='agent-user',
                enabled=True,
                block_public_channels=True,
                restrict_to_allowed_channels=False,
                allowed_channel_ids=[],
                updated_by='owner-user',
            )
        )

        synced = self.channel_manager.create_channel_from_sync(
            channel_id='CsyncOpen001',
            name='sync-open',
            channel_type='public',
            description='synced open channel',
            local_user_id='owner-user',
            origin_peer='peer-alpha',
            privacy_mode='open',
            initial_members=None,
        )
        self.assertIsNotNone(synced)

        members = self.db.conn.execute(
            "SELECT user_id FROM channel_members WHERE channel_id = ? ORDER BY user_id",
            ('CsyncOpen001',),
        ).fetchall()
        member_ids = [row['user_id'] for row in members]
        self.assertIn('member-user', member_ids)
        self.assertNotIn('agent-user', member_ids)

    def test_add_member_respects_governance_policy(self) -> None:
        open_channel = self.channel_manager.create_channel(
            name='governance-open',
            channel_type=ChannelType.PUBLIC,
            created_by='owner-user',
            description='open channel',
            privacy_mode='open',
        )
        self.assertIsNotNone(open_channel)
        assert open_channel is not None

        self.assertTrue(
            self.channel_manager.set_user_channel_governance(
                user_id='agent-user',
                enabled=True,
                block_public_channels=True,
                restrict_to_allowed_channels=False,
                allowed_channel_ids=[],
                updated_by='owner-user',
            )
        )

        added = self.channel_manager.add_member(
            channel_id=open_channel.id,
            target_user_id='agent-user',
            requester_id='owner-user',
        )
        self.assertFalse(added)

        role = self.channel_manager.get_member_role(open_channel.id, 'agent-user')
        self.assertIsNone(role)


if __name__ == '__main__':
    unittest.main()
