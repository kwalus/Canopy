"""Regression tests for channel removal voting governance state."""

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
            """
            INSERT INTO users (id, username, public_key, password_hash, origin_peer)
            VALUES (?, ?, ?, ?, ?)
            """,
            [
                ('owner-user', 'owner', 'pk-owner', 'hash-owner', None),
                ('member-user', 'member', 'pk-member', 'hash-member', None),
                ('remote-a', 'remote-a', 'pk-a', 'hash-a', 'peer-a'),
                ('remote-b', 'remote-b', 'pk-b', 'hash-b', 'peer-b'),
                ('remote-c', 'remote-c', 'pk-c', 'hash-c', 'peer-c'),
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


class TestChannelRemovalVoting(unittest.TestCase):
    def setUp(self) -> None:
        self.db = _FakeDbManager()
        self.channel_manager = ChannelManager(self.db, MagicMock())

    def tearDown(self) -> None:
        self.db.conn.close()

    def _create_private_channel(self, channel_id_hint: str = 'governance-room'):
        channel = self.channel_manager.create_channel(
            name=channel_id_hint,
            channel_type=ChannelType.PRIVATE,
            created_by='owner-user',
            description='private governance test channel',
            privacy_mode='private',
            initial_members=['member-user', 'remote-a', 'remote-b'],
        )
        self.assertIsNotNone(channel)
        return channel

    def test_system_channels_are_ineligible(self) -> None:
        result_general = self.channel_manager.start_channel_removal_vote(
            channel_id='general',
            user_id='owner-user',
            local_peer_id='peer-local',
            connected_peer_ids=['peer-a'],
            trusted_peer_ids=['peer-a'],
            allow_admin=True,
        )
        self.assertFalse(result_general['ok'])
        self.assertEqual(result_general['error'], 'system_channel')

        result_quarantine = self.channel_manager.start_channel_removal_vote(
            channel_id=self.channel_manager.AGENT_START_CHANNEL_ID,
            user_id='owner-user',
            local_peer_id='peer-local',
            connected_peer_ids=['peer-a'],
            trusted_peer_ids=['peer-a'],
            allow_admin=True,
        )
        self.assertFalse(result_quarantine['ok'])
        self.assertEqual(result_quarantine['error'], 'system_channel')

    def test_private_channel_electorate_is_member_scoped_and_frozen(self) -> None:
        channel = self._create_private_channel()

        result = self.channel_manager.start_channel_removal_vote(
            channel_id=channel.id,
            user_id='owner-user',
            local_peer_id='peer-local',
            connected_peer_ids=['peer-a', 'peer-b', 'peer-c'],
            trusted_peer_ids=['peer-a', 'peer-b', 'peer-c'],
        )

        self.assertTrue(result['ok'])
        self.assertEqual(
            set(result['electorate_peer_ids']),
            {'peer-local', 'peer-a', 'peer-b'},
        )

        status = self.channel_manager.get_channel_removal_status(
            channel.id,
            local_peer_id='peer-local',
            viewer_user_id='owner-user',
            connected_peer_ids=['peer-a', 'peer-b', 'peer-c', 'peer-z'],
            trusted_peer_ids=['peer-a', 'peer-b', 'peer-c', 'peer-z'],
        )
        electorate = {
            entry['peer_id']
            for entry in (status.get('active_proposal') or {}).get('electorate', [])
        }
        self.assertEqual(electorate, {'peer-local', 'peer-a', 'peer-b'})
        self.assertNotIn('peer-c', electorate)
        self.assertNotIn('peer-z', electorate)

        duplicate = self.channel_manager.cast_channel_removal_vote(
            channel_id=channel.id,
            proposal_id=result['proposal_id'],
            user_id='owner-user',
            local_peer_id='peer-local',
            vote='remove',
        )
        self.assertTrue(duplicate['ok'])
        self.assertFalse(duplicate['changed'])
        self.assertEqual((duplicate.get('status') or {}).get('active_proposal', {}).get('local_vote'), 'remove')

    def test_local_peer_can_change_open_vote_before_finalization(self) -> None:
        channel = self._create_private_channel('toggle-room')

        result = self.channel_manager.start_channel_removal_vote(
            channel_id=channel.id,
            user_id='owner-user',
            local_peer_id='peer-local',
            connected_peer_ids=['peer-a', 'peer-b'],
            trusted_peer_ids=['peer-a', 'peer-b'],
        )
        self.assertTrue(result['ok'])

        status_before = self.channel_manager.get_channel_removal_status(
            channel.id,
            local_peer_id='peer-local',
            viewer_user_id='owner-user',
            connected_peer_ids=['peer-a', 'peer-b'],
            trusted_peer_ids=['peer-a', 'peer-b'],
        )
        self.assertFalse(status_before['can_vote'])
        self.assertTrue(status_before['can_change_vote'])
        self.assertEqual((status_before.get('active_proposal') or {}).get('local_vote'), 'remove')

        changed = self.channel_manager.cast_channel_removal_vote(
            channel_id=channel.id,
            proposal_id=result['proposal_id'],
            user_id='owner-user',
            local_peer_id='peer-local',
            vote='keep',
            connected_peer_ids=['peer-a', 'peer-b'],
            trusted_peer_ids=['peer-a', 'peer-b'],
        )
        self.assertTrue(changed['ok'])
        self.assertTrue(changed['changed'])
        self.assertEqual(changed['previous_vote'], 'remove')
        self.assertEqual((changed.get('finalization') or {}).get('result'), 'rejected')

    def test_member_can_cast_local_peer_vote_on_open_proposal(self) -> None:
        channel = self.channel_manager.create_channel(
            name='member-vote-room',
            channel_type=ChannelType.PUBLIC,
            created_by='owner-user',
            description='open governance test channel',
            privacy_mode='open',
        )
        self.assertIsNotNone(channel)
        assert channel is not None
        self.assertTrue(
            self.channel_manager.add_member(
                channel_id=channel.id,
                target_user_id='member-user',
                requester_id='owner-user',
            )
        )

        received = self.channel_manager.receive_channel_removal_proposal(
            proposal_id='CRP-member-cast',
            channel_id=channel.id,
            channel_name=channel.name,
            channel_origin_peer=channel.origin_peer,
            channel_privacy_mode=channel.privacy_mode,
            initiator_peer_id='peer-a',
            initiator_user_id='remote-a',
            electorate_peer_ids=['peer-local', 'peer-a'],
            threshold_count=2,
        )
        self.assertTrue(received)

        status_before = self.channel_manager.get_channel_removal_status(
            channel.id,
            local_peer_id='peer-local',
            viewer_user_id='member-user',
            connected_peer_ids=['peer-a'],
            trusted_peer_ids=['peer-a'],
        )
        self.assertTrue(status_before['can_vote'])
        self.assertFalse(status_before['can_start'])

        cast = self.channel_manager.cast_channel_removal_vote(
            channel_id=channel.id,
            proposal_id='CRP-member-cast',
            user_id='member-user',
            local_peer_id='peer-local',
            vote='keep',
        )
        self.assertTrue(cast['ok'])
        self.assertEqual((cast.get('finalization') or {}).get('result'), 'rejected')

    def test_retired_channel_is_hidden_and_not_recreated_from_sync(self) -> None:
        channel = self._create_private_channel('retire-room')

        result = self.channel_manager.start_channel_removal_vote(
            channel_id=channel.id,
            user_id='owner-user',
            local_peer_id='peer-local',
            connected_peer_ids=['peer-a'],
            trusted_peer_ids=['peer-a'],
        )
        self.assertTrue(result['ok'])
        self.assertIsNone(result.get('finalization'))

        remote_vote = self.channel_manager.receive_channel_removal_vote(
            proposal_id=result['proposal_id'],
            channel_id=channel.id,
            voter_peer_id='peer-a',
            voter_user_id='remote-a',
            vote='remove',
        )
        self.assertTrue(remote_vote['ok'])
        self.assertEqual((remote_vote.get('finalization') or {}).get('result'), 'retired')

        decision = self.channel_manager.get_channel_access_decision(channel.id, 'owner-user')
        self.assertFalse(decision['allowed'])
        self.assertEqual(decision['reason'], 'retired_by_vote')

        visible = {entry.id for entry in self.channel_manager.get_user_channels('owner-user')}
        self.assertNotIn(channel.id, visible)
        self.assertTrue(self.channel_manager.is_channel_retired_by_vote(channel.id))

        recreated = self.channel_manager.create_channel_from_sync(
            channel_id=channel.id,
            name=channel.name,
            channel_type='private',
            description='stale remote reannounce',
            local_user_id='owner-user',
            origin_peer='peer-origin',
            privacy_mode='private',
            initial_members=['owner-user', 'remote-a'],
        )
        self.assertIsNone(recreated)

    def test_member_cannot_start_vote_without_admin_privileges(self) -> None:
        channel = self.channel_manager.create_channel(
            name='member-start-room',
            channel_type=ChannelType.PUBLIC,
            created_by='owner-user',
            description='member start authorization test',
            privacy_mode='open',
        )
        self.assertIsNotNone(channel)
        assert channel is not None
        self.assertTrue(
            self.channel_manager.add_member(
                channel_id=channel.id,
                target_user_id='member-user',
                requester_id='owner-user',
            )
        )

        result = self.channel_manager.start_channel_removal_vote(
            channel_id=channel.id,
            user_id='member-user',
            local_peer_id='peer-local',
            connected_peer_ids=['peer-a'],
            trusted_peer_ids=['peer-a'],
        )

        self.assertFalse(result['ok'])
        self.assertEqual(result['error'], 'not_authorized')

    def test_untrusted_remote_proposal_is_rejected(self) -> None:
        channel = self._create_private_channel('proposal-trust-room')

        received = self.channel_manager.receive_channel_removal_proposal(
            proposal_id='CRP-untrusted-proposal',
            channel_id=channel.id,
            channel_name=channel.name,
            channel_origin_peer=channel.origin_peer,
            channel_privacy_mode=channel.privacy_mode,
            initiator_peer_id='peer-a',
            initiator_user_id='remote-a',
            electorate_peer_ids=['peer-local', 'peer-a'],
            threshold_count=2,
            trusted_peer_ids=['peer-b'],
        )

        self.assertFalse(received)
        status = self.channel_manager.get_channel_removal_status(
            channel.id,
            local_peer_id='peer-local',
            viewer_user_id='owner-user',
            connected_peer_ids=['peer-a'],
            trusted_peer_ids=['peer-b'],
        )
        self.assertIsNone(status.get('active_proposal'))

    def test_result_cannot_rewrite_frozen_electorate(self) -> None:
        channel = self._create_private_channel('result-mismatch-room')
        start = self.channel_manager.start_channel_removal_vote(
            channel_id=channel.id,
            user_id='owner-user',
            local_peer_id='peer-local',
            connected_peer_ids=['peer-a'],
            trusted_peer_ids=['peer-a'],
        )
        self.assertTrue(start['ok'])

        applied = self.channel_manager.apply_channel_removal_result(
            proposal_id=start['proposal_id'],
            channel_id=channel.id,
            result='retired',
            electorate_peer_ids=['peer-local', 'peer-a', 'peer-b'],
            threshold_count=3,
            finalizing_peer_id='peer-a',
            finalizing_user_id='remote-a',
            trusted_peer_ids=['peer-a'],
        )

        self.assertFalse(applied)
        self.assertFalse(self.channel_manager.is_channel_retired_by_vote(channel.id))
        status = self.channel_manager.get_channel_removal_status(
            channel.id,
            local_peer_id='peer-local',
            viewer_user_id='owner-user',
            connected_peer_ids=['peer-a'],
            trusted_peer_ids=['peer-a'],
        )
        proposal = status.get('active_proposal') or {}
        self.assertEqual(
            [entry['peer_id'] for entry in (proposal.get('electorate') or [])],
            ['peer-a', 'peer-local'],
        )

    def test_result_with_empty_finalizer_peer_is_rejected(self) -> None:
        channel = self._create_private_channel('empty-finalizer-room')
        applied = self.channel_manager.apply_channel_removal_result(
            proposal_id='CRP-empty-finalizer',
            channel_id=channel.id,
            result='retired',
            electorate_peer_ids=['peer-local', 'peer-a'],
            threshold_count=2,
            finalizing_peer_id='',
            finalizing_user_id='remote-a',
            trusted_peer_ids=['peer-a'],
        )
        self.assertFalse(applied)
        self.assertFalse(self.channel_manager.is_channel_retired_by_vote(channel.id))

    def test_retired_result_rejected_when_local_keep_vote_exists(self) -> None:
        channel = self.channel_manager.create_channel(
            name='keep-contradiction-room',
            channel_type=ChannelType.PUBLIC,
            created_by='owner-user',
            description='keep vote contradiction test',
            privacy_mode='open',
        )
        self.assertIsNotNone(channel)
        assert channel is not None
        self.assertTrue(
            self.channel_manager.add_member(
                channel_id=channel.id,
                target_user_id='member-user',
                requester_id='owner-user',
            )
        )
        self.assertTrue(
            self.channel_manager.receive_channel_removal_proposal(
                proposal_id='CRP-keep-contradiction',
                channel_id=channel.id,
                channel_name=channel.name,
                channel_origin_peer=channel.origin_peer,
                channel_privacy_mode=channel.privacy_mode,
                initiator_peer_id='peer-a',
                initiator_user_id='remote-a',
                electorate_peer_ids=['peer-local', 'peer-a'],
                threshold_count=2,
                trusted_peer_ids=['peer-a'],
            )
        )
        keep_vote = self.channel_manager.cast_channel_removal_vote(
            channel_id=channel.id,
            proposal_id='CRP-keep-contradiction',
            user_id='member-user',
            local_peer_id='peer-local',
            vote='keep',
        )
        self.assertTrue(keep_vote['ok'])
        self.assertEqual((keep_vote.get('finalization') or {}).get('result'), 'rejected')

        applied = self.channel_manager.apply_channel_removal_result(
            proposal_id='CRP-keep-contradiction',
            channel_id=channel.id,
            result='retired',
            electorate_peer_ids=['peer-local', 'peer-a'],
            threshold_count=2,
            finalizing_peer_id='peer-a',
            finalizing_user_id='remote-a',
            trusted_peer_ids=['peer-a'],
        )
        self.assertFalse(applied)
        self.assertFalse(self.channel_manager.is_channel_retired_by_vote(channel.id))

    def test_rejected_result_rejected_when_local_remove_votes_meet_threshold(self) -> None:
        channel = self.channel_manager.create_channel(
            name='remove-threshold-room',
            channel_type=ChannelType.PUBLIC,
            created_by='owner-user',
            description='remove threshold contradiction test',
            privacy_mode='open',
        )
        self.assertIsNotNone(channel)
        assert channel is not None
        start = self.channel_manager.start_channel_removal_vote(
            channel_id=channel.id,
            user_id='owner-user',
            local_peer_id='peer-local',
            connected_peer_ids=[],
            trusted_peer_ids=[],
        )
        self.assertTrue(start['ok'])
        self.assertEqual((start.get('finalization') or {}).get('result'), 'retired')

        applied = self.channel_manager.apply_channel_removal_result(
            proposal_id=start['proposal_id'],
            channel_id=channel.id,
            result='rejected',
            electorate_peer_ids=['peer-local'],
            threshold_count=1,
            finalizing_peer_id='peer-local',
            finalizing_user_id='owner-user',
            trusted_peer_ids=['peer-local'],
        )
        self.assertFalse(applied)
        self.assertTrue(self.channel_manager.is_channel_retired_by_vote(channel.id))

    def test_bootstrap_result_applied_when_no_local_proposal(self) -> None:
        channel = self._create_private_channel('bootstrap-result-room')
        applied = self.channel_manager.apply_channel_removal_result(
            proposal_id='CRP-bootstrap-result',
            channel_id=channel.id,
            result='retired',
            electorate_peer_ids=['peer-local', 'peer-a'],
            threshold_count=2,
            finalizing_peer_id='peer-a',
            finalizing_user_id='remote-a',
            trusted_peer_ids=['peer-a'],
        )
        self.assertTrue(applied)
        self.assertTrue(self.channel_manager.is_channel_retired_by_vote(channel.id))

    def test_bootstrap_result_rejected_from_non_electorate_peer(self) -> None:
        channel = self._create_private_channel('bootstrap-non-electorate-room')
        applied = self.channel_manager.apply_channel_removal_result(
            proposal_id='CRP-bootstrap-non-electorate',
            channel_id=channel.id,
            result='retired',
            electorate_peer_ids=['peer-local', 'peer-a'],
            threshold_count=2,
            finalizing_peer_id='peer-b',
            finalizing_user_id='remote-b',
            trusted_peer_ids=['peer-b'],
        )
        self.assertFalse(applied)
        self.assertFalse(self.channel_manager.is_channel_retired_by_vote(channel.id))

