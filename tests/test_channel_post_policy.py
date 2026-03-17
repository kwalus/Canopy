"""Core regression tests for curated channel posting policy."""

import os
import sqlite3
import sys
import types
import unittest
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
            "INSERT INTO users (id, username, public_key, password_hash, origin_peer) "
            "VALUES (?, ?, ?, ?, ?)",
            [
                ('owner-user', 'owner', 'pk-owner', 'hash-owner', None),
                ('member-user', 'member', 'pk-member', 'hash-member', None),
                ('reader-user', 'reader', 'pk-reader', 'hash-reader', None),
                ('remote-user', 'remote', 'pk-remote', None, 'peer-remote'),
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


class TestChannelPostPolicy(unittest.TestCase):
    def setUp(self) -> None:
        self.db = _FakeDbManager()
        self.channel_manager = ChannelManager(self.db, MagicMock())

    def tearDown(self) -> None:
        self.db.conn.close()

    def test_curated_channel_blocks_top_level_posts_but_keeps_replies_open(self) -> None:
        channel = self.channel_manager.create_channel(
            name='ops-curated',
            channel_type=ChannelType.PUBLIC,
            created_by='owner-user',
            description='curated channel',
            privacy_mode='open',
            post_policy='curated',
            allow_member_replies=True,
        )
        self.assertIsNotNone(channel)
        self.assertTrue(self.channel_manager.add_member(channel.id, 'member-user', 'owner-user', 'member'))

        top_level = self.channel_manager.can_user_post_message(channel.id, 'member-user')
        reply = self.channel_manager.can_user_post_message(channel.id, 'member-user', parent_message_id='M1')
        owner = self.channel_manager.can_user_post_message(channel.id, 'owner-user')

        self.assertFalse(top_level['allowed'])
        self.assertEqual(top_level['reason'], 'top_level_post_restricted')
        self.assertEqual(top_level['post_policy'], 'curated')
        self.assertTrue(reply['allowed'])
        self.assertEqual(reply['reason'], 'ok')
        self.assertTrue(owner['allowed'])

    def test_grant_and_revoke_channel_poster_updates_effective_access(self) -> None:
        channel = self.channel_manager.create_channel(
            name='ops-grants',
            channel_type=ChannelType.PUBLIC,
            created_by='owner-user',
            description='grant test',
            privacy_mode='open',
            post_policy='curated',
            allow_member_replies=True,
        )
        self.assertIsNotNone(channel)
        self.assertTrue(self.channel_manager.add_member(channel.id, 'member-user', 'owner-user', 'member'))

        initial = self.channel_manager.can_user_post_message(channel.id, 'member-user')
        self.assertFalse(initial['allowed'])

        granted = self.channel_manager.grant_channel_post_permission(
            channel.id,
            'member-user',
            'owner-user',
            allow_admin=False,
            local_peer_id=None,
        )
        self.assertIsNotNone(granted)
        self.assertEqual(granted['allowed_poster_count'], 1)

        after_grant = self.channel_manager.can_user_post_message(channel.id, 'member-user')
        self.assertTrue(after_grant['allowed'])

        revoked = self.channel_manager.revoke_channel_post_permission(
            channel.id,
            'member-user',
            'owner-user',
            allow_admin=False,
            local_peer_id=None,
        )
        self.assertIsNotNone(revoked)
        self.assertEqual(revoked['allowed_poster_count'], 0)

        after_revoke = self.channel_manager.can_user_post_message(channel.id, 'member-user')
        self.assertFalse(after_revoke['allowed'])
        self.assertEqual(after_revoke['reason'], 'top_level_post_restricted')

    def test_origin_sync_updates_curated_policy_and_allowlist(self) -> None:
        channel = self.channel_manager.create_channel(
            name='sync-policy',
            channel_type=ChannelType.PUBLIC,
            created_by='owner-user',
            description='sync target',
            privacy_mode='open',
            origin_peer='peer-local',
        )
        self.assertIsNotNone(channel)
        self.assertTrue(self.channel_manager.add_member(channel.id, 'member-user', 'owner-user', 'member'))
        self.assertTrue(self.channel_manager.add_member(channel.id, 'reader-user', 'owner-user', 'member'))

        merged = self.channel_manager.merge_or_adopt_channel(
            remote_id=channel.id,
            remote_name='sync-policy',
            remote_type='public',
            remote_desc='sync target',
            local_user_id='owner-user',
            from_peer='peer-local',
            privacy_mode='open',
            post_policy='curated',
            allow_member_replies=True,
            allowed_poster_user_ids=['member-user'],
        )
        self.assertEqual(merged, channel.id)

        poster_state = self.channel_manager.get_channel_posting_state(channel.id, 'member-user')
        reader_state = self.channel_manager.get_channel_posting_state(channel.id, 'reader-user')

        self.assertEqual(poster_state['post_policy'], 'curated')
        self.assertTrue(poster_state['can_post_top_level'])
        self.assertTrue(reader_state['can_reply'])
        self.assertFalse(reader_state['can_post_top_level'])
        self.assertEqual(self.channel_manager.get_channel_allowed_poster_ids(channel.id), ['member-user'])

    def test_non_origin_announce_cannot_overwrite_local_origin_curated_policy(self) -> None:
        channel = self.channel_manager.create_channel(
            name='local-curated',
            channel_type=ChannelType.PUBLIC,
            created_by='owner-user',
            description='local origin channel',
            privacy_mode='open',
            post_policy='curated',
            allow_member_replies=True,
        )
        self.assertIsNotNone(channel)
        self.assertTrue(self.channel_manager.add_member(channel.id, 'member-user', 'owner-user', 'member'))
        granted = self.channel_manager.grant_channel_post_permission(
            channel.id,
            'member-user',
            'owner-user',
            allow_admin=False,
            local_peer_id=None,
        )
        self.assertIsNotNone(granted)

        merged = self.channel_manager.merge_or_adopt_channel(
            remote_id=channel.id,
            remote_name='local-curated',
            remote_type='public',
            remote_desc='local origin channel',
            local_user_id='owner-user',
            from_peer='peer-remote',
            privacy_mode='open',
            post_policy='open',
            allow_member_replies=True,
            allowed_poster_user_ids=[],
        )
        self.assertIsNone(merged)

        state = self.channel_manager.get_channel_posting_state(channel.id, 'member-user')
        self.assertEqual(state['post_policy'], 'curated')
        self.assertTrue(state['can_post_top_level'])
        self.assertEqual(self.channel_manager.get_channel_allowed_poster_ids(channel.id), ['member-user'])

    def test_remote_posting_snapshot_requires_origin_authority(self) -> None:
        channel = self.channel_manager.create_channel_from_sync(
            channel_id='sync-authority',
            name='sync-authority',
            channel_type='public',
            description='synced authority channel',
            local_user_id='owner-user',
            origin_peer='peer-origin',
            privacy_mode='open',
            post_policy='curated',
            allow_member_replies=True,
            allowed_poster_user_ids=['member-user'],
        )
        self.assertIsNotNone(channel)
        self.assertTrue(self.channel_manager.add_member(channel.id, 'member-user', 'owner-user', 'member'))

        rejected = self.channel_manager.apply_remote_channel_posting_snapshot(
            channel.id,
            'peer-other',
            post_policy='open',
            allow_member_replies=True,
            allowed_poster_user_ids=[],
            log_context='test_non_origin',
        )
        self.assertFalse(rejected)
        state_after_reject = self.channel_manager.get_channel_posting_state(channel.id, 'member-user')
        self.assertEqual(state_after_reject['post_policy'], 'curated')
        self.assertTrue(state_after_reject['can_post_top_level'])

        accepted = self.channel_manager.apply_remote_channel_posting_snapshot(
            channel.id,
            'peer-origin',
            post_policy='open',
            allow_member_replies=True,
            allowed_poster_user_ids=[],
            log_context='test_origin',
        )
        self.assertTrue(accepted)
        state_after_accept = self.channel_manager.get_channel_posting_state(channel.id, 'member-user')
        self.assertEqual(state_after_accept['post_policy'], 'open')
        self.assertTrue(state_after_accept['can_post_top_level'])
        self.assertEqual(self.channel_manager.get_channel_allowed_poster_ids(channel.id), [])

    def test_sync_allowlist_persists_known_remote_users_before_membership_exists(self) -> None:
        channel = self.channel_manager.create_channel(
            name='sync-allowlist-remote',
            channel_type=ChannelType.PUBLIC,
            created_by='owner-user',
            description='remote poster sync',
            privacy_mode='open',
            origin_peer='peer-local',
        )
        self.assertIsNotNone(channel)

        synced = self.channel_manager.sync_channel_post_permissions(
            channel.id,
            post_policy='curated',
            allow_member_replies=True,
            allowed_poster_user_ids=['remote-user'],
        )
        self.assertTrue(synced)
        self.assertEqual(
            self.channel_manager.get_channel_allowed_poster_ids(channel.id),
            ['remote-user'],
        )

        self.assertTrue(
            self.channel_manager.add_member(channel.id, 'remote-user', 'owner-user', 'member')
        )
        remote_state = self.channel_manager.can_user_post_message(channel.id, 'remote-user')
        self.assertTrue(remote_state['allowed'])

    def test_incoming_curated_decision_blocks_top_level_but_keeps_reply_open(self) -> None:
        channel = self.channel_manager.create_channel(
            name='incoming-curated',
            channel_type=ChannelType.PUBLIC,
            created_by='owner-user',
            description='incoming enforcement',
            privacy_mode='open',
            post_policy='curated',
            allow_member_replies=True,
        )
        self.assertIsNotNone(channel)
        self.assertTrue(self.channel_manager.add_member(channel.id, 'member-user', 'owner-user', 'member'))

        top_level = self.channel_manager.can_accept_incoming_message(channel.id, 'member-user')
        reply = self.channel_manager.can_accept_incoming_message(
            channel.id,
            'member-user',
            parent_message_id='M1',
        )
        general = self.channel_manager.can_accept_incoming_message('general', 'member-user')

        self.assertFalse(top_level['allowed'])
        self.assertEqual(top_level['reason'], 'top_level_post_restricted')
        self.assertTrue(reply['allowed'])
        self.assertEqual(reply['reason'], 'ok')
        self.assertTrue(general['allowed'])
        self.assertEqual(general['reason'], 'general_channel_exempt')

    def test_channel_posting_snapshot_returns_curated_metadata(self) -> None:
        channel = self.channel_manager.create_channel(
            name='snapshot-curated',
            channel_type=ChannelType.PUBLIC,
            created_by='owner-user',
            description='snapshot metadata',
            privacy_mode='open',
            post_policy='curated',
            allow_member_replies=False,
        )
        self.assertIsNotNone(channel)
        self.assertTrue(self.channel_manager.add_member(channel.id, 'member-user', 'owner-user', 'member'))
        self.channel_manager.grant_channel_post_permission(
            channel.id,
            'member-user',
            'owner-user',
            allow_admin=False,
            local_peer_id=None,
        )

        snapshot = self.channel_manager.get_channel_posting_snapshot(channel.id)

        self.assertEqual(snapshot['post_policy'], 'curated')
        self.assertFalse(snapshot['allow_member_replies'])
        self.assertEqual(snapshot['allowed_poster_user_ids'], ['member-user'])

    def test_get_all_public_channels_preserves_curated_sync_metadata(self) -> None:
        channel = self.channel_manager.create_channel(
            name='public-curated-sync',
            channel_type=ChannelType.PUBLIC,
            created_by='owner-user',
            description='public curated channel for sync',
            privacy_mode='open',
            post_policy='curated',
            allow_member_replies=False,
        )
        self.assertIsNotNone(channel)
        self.assertTrue(self.channel_manager.add_member(channel.id, 'member-user', 'owner-user', 'member'))
        granted = self.channel_manager.grant_channel_post_permission(
            channel.id,
            'member-user',
            'owner-user',
            allow_admin=False,
            local_peer_id=None,
        )
        self.assertIsNotNone(granted)

        public_channels = self.channel_manager.get_all_public_channels()
        public_entry = next((row for row in public_channels if row['id'] == channel.id), None)

        self.assertIsNotNone(public_entry)
        self.assertEqual(public_entry['post_policy'], 'curated')
        self.assertFalse(public_entry['allow_member_replies'])
        self.assertEqual(public_entry['allowed_poster_user_ids'], ['member-user'])


if __name__ == '__main__':
    unittest.main()
