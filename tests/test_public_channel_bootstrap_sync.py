"""Regression tests for public-channel bootstrap sync on untrusted peers."""

import asyncio
import os
import sqlite3
import sys
import tempfile
import types
import unittest
from types import SimpleNamespace
from unittest.mock import patch

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

from canopy.core.app import create_app
from canopy.core.channels import ChannelType


class _FakeP2PNetworkManager:
    def __init__(self, config, db_manager):
        self.config = config
        self.db_manager = db_manager
        self.relay_policy = 'broker_only'
        self.local_identity = SimpleNamespace(peer_id='peer-local', x25519_private_key=None)
        self.identity_manager = SimpleNamespace(
            local_identity=self.local_identity,
            peer_display_names={},
            known_peers={},
        )
        self.connection_manager = SimpleNamespace(is_connected=lambda peer_id: True)
        self.discovery = None
        self.peer_versions = {}
        self._introduced_peers = {}
        self._running = False
        self.sent_catchup = []
        self.sync_requests = []
        self.metadata_requests = []
        self.metadata_replies = []
        self.on_channel_sync = None
        self.on_catchup_request = None
        self.on_catchup_response = None
        self.on_channel_metadata_request = None

    def set_relay_policy(self, policy):
        self.relay_policy = policy

    def get_peer_id(self):
        return self.local_identity.peer_id

    def get_connected_peers(self):
        return []

    def peer_supports_capability(self, peer_id, capability):
        return False

    def is_running(self):
        return self._running

    def start(self):
        self._running = True

    def trigger_peer_sync(self, peer_id):
        self.sync_requests.append(peer_id)
        return True

    def send_channel_metadata_request(self, to_peer, channel_ids, request_id=None, reason=None):
        self.metadata_requests.append(
            {
                'to_peer': to_peer,
                'channel_ids': list(channel_ids or []),
                'request_id': request_id,
                'reason': reason,
            }
        )
        return True

    def replay_public_channel_metadata_to_peer(self, to_peer, channel_ids=None, reason='targeted_request', request_id=None):
        self.metadata_replies.append(
            {
                'to_peer': to_peer,
                'channel_ids': list(channel_ids or []),
                'reason': reason,
                'request_id': request_id,
            }
        )
        return True

    async def send_catchup_response_async(self, to_peer, messages, extra_data=None):
        self.sent_catchup.append({
            'to_peer': to_peer,
            'messages': messages,
            'extra_data': extra_data,
        })

    def _build_p2p_attachment_entry(self, att):
        return att


class TestPublicChannelBootstrapSync(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.env_patcher = patch.dict(
            os.environ,
            {
                'CANOPY_TESTING': 'true',
                'CANOPY_DISABLE_MESH': 'true',
                'CANOPY_DATA_DIR': self.tempdir.name,
                'CANOPY_DATABASE_PATH': os.path.join(self.tempdir.name, 'canopy.db'),
                'CANOPY_SECRET_KEY': 'test-secret',
            },
            clear=False,
        )
        self.env_patcher.start()
        self.addCleanup(self.env_patcher.stop)

        self.checkpoint_patcher = patch(
            'canopy.core.database.DatabaseManager._start_checkpoint_thread',
            lambda self: None,
        )
        self.checkpoint_patcher.start()
        self.addCleanup(self.checkpoint_patcher.stop)

        self.logging_patcher = patch(
            'canopy.core.app.setup_logging',
            lambda debug=False: None,
        )
        self.logging_patcher.start()
        self.addCleanup(self.logging_patcher.stop)

        self.p2p_patcher = patch(
            'canopy.core.app.P2PNetworkManager',
            _FakeP2PNetworkManager,
        )
        self.p2p_patcher.start()
        self.addCleanup(self.p2p_patcher.stop)

        self.app = create_app()
        self.db_manager = self.app.config['DB_MANAGER']
        self.channel_manager = self.app.config['CHANNEL_MANAGER']
        self.trust_manager = self.app.config['TRUST_MANAGER']
        self.p2p_manager = self.app.config['P2P_MANAGER']

        with self.db_manager.get_connection() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO users (
                    id, username, public_key, password_hash, display_name,
                    origin_peer, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """,
                ('owner-user', 'owner', 'pk-owner', 'pw-owner', 'Owner', None),
            )
            conn.commit()

    def _mark_peer_untrusted(self, peer_id: str) -> None:
        self.trust_manager.set_trust_score(peer_id, 0, reason='test-untrusted')

    def test_untrusted_channel_sync_imports_public_channels_only(self) -> None:
        self._mark_peer_untrusted('peer-guest')

        self.p2p_manager.on_channel_sync(
            [
                {
                    'id': 'Cpubsync001',
                    'name': 'public-news',
                    'type': 'public',
                    'desc': 'public',
                    'privacy_mode': 'open',
                    'origin_peer': 'peer-guest',
                },
                {
                    'id': 'Cprivsync001',
                    'name': 'private-room',
                    'type': 'private',
                    'desc': 'private',
                    'privacy_mode': 'private',
                    'origin_peer': 'peer-guest',
                },
            ],
            'peer-guest',
        )

        with self.db_manager.get_connection() as conn:
            public_row = conn.execute("SELECT id, privacy_mode FROM channels WHERE id = 'Cpubsync001'").fetchone()
            private_row = conn.execute("SELECT id FROM channels WHERE id = 'Cprivsync001'").fetchone()
            local_membership = conn.execute(
                "SELECT 1 FROM channel_members WHERE channel_id = 'Cpubsync001' AND user_id = 'owner-user'"
            ).fetchone()

        self.assertIsNotNone(public_row)
        self.assertEqual(public_row['privacy_mode'], 'open')
        self.assertIsNone(private_row)
        self.assertIsNotNone(local_membership)

    def test_untrusted_catchup_request_serves_only_public_messages(self) -> None:
        self._mark_peer_untrusted('peer-guest')

        public_channel = self.channel_manager.create_channel(
            name='public-boot',
            channel_type=ChannelType.PUBLIC,
            created_by='owner-user',
            description='public room',
            privacy_mode='open',
        )
        private_channel = self.channel_manager.create_channel(
            name='private-boot',
            channel_type=ChannelType.PRIVATE,
            created_by='owner-user',
            description='private room',
            privacy_mode='private',
            initial_members=['owner-user'],
        )
        assert public_channel is not None
        assert private_channel is not None
        self.channel_manager.send_message(channel_id=public_channel.id, user_id='owner-user', content='public hello')
        self.channel_manager.send_message(channel_id=private_channel.id, user_id='owner-user', content='private hello')

        with patch('asyncio.ensure_future', lambda coro: asyncio.run(coro)):
            self.p2p_manager.on_catchup_request({}, 'peer-guest')

        self.assertEqual(len(self.p2p_manager.sent_catchup), 1)
        payload = self.p2p_manager.sent_catchup[0]
        channel_ids = {msg.get('channel_id') for msg in payload['messages']}
        self.assertEqual(channel_ids, {public_channel.id})
        self.assertIsNone((payload.get('extra_data') or {}).get('circles'))
        self.assertIsNone((payload.get('extra_data') or {}).get('tasks'))

    def test_trusted_catchup_request_backfills_older_public_history_when_peer_is_sparse(self) -> None:
        self.trust_manager.set_trust_score('peer-origin', 100, reason='test-trusted')

        public_channel = self.channel_manager.create_channel(
            name='aviation',
            channel_type=ChannelType.PUBLIC,
            created_by='owner-user',
            description='public room',
            privacy_mode='open',
        )
        assert public_channel is not None
        self.channel_manager.send_message(channel_id=public_channel.id, user_id='owner-user', content='older aviation post')
        self.channel_manager.send_message(channel_id=public_channel.id, user_id='owner-user', content='newer aviation post')
        with self.db_manager.get_connection() as conn:
            inserted_rows = conn.execute(
                "SELECT id, content FROM channel_messages WHERE channel_id = ? ORDER BY rowid ASC",
                (public_channel.id,),
            ).fetchall()
            older_id = next(row['id'] for row in inserted_rows if row['content'] == 'older aviation post')
            newer_id = next(row['id'] for row in inserted_rows if row['content'] == 'newer aviation post')
            conn.execute(
                "UPDATE channel_messages SET created_at = ? WHERE id = ?",
                ('2026-03-28 10:00:00', older_id),
            )
            conn.execute(
                "UPDATE channel_messages SET created_at = ? WHERE id = ?",
                ('2026-03-31 10:00:00', newer_id),
            )
            conn.commit()

        with self.db_manager.get_connection() as conn:
            rows = conn.execute(
                "SELECT id, created_at FROM channel_messages WHERE channel_id = ? ORDER BY created_at ASC",
                (public_channel.id,),
            ).fetchall()
        oldest_ts = rows[0]['created_at']
        newest_ts = rows[-1]['created_at']

        with patch('asyncio.ensure_future', lambda coro: asyncio.run(coro)):
            self.p2p_manager.on_catchup_request(
                {public_channel.id: newest_ts},
                'peer-origin',
                channel_ranges={
                    public_channel.id: {
                        'latest': newest_ts,
                        'oldest': newest_ts,
                        'message_count': 1,
                    }
                },
            )

        self.assertEqual(len(self.p2p_manager.sent_catchup), 1)
        payload = self.p2p_manager.sent_catchup[0]
        message_ids = [msg.get('id') for msg in payload['messages']]
        self.assertIn(rows[0]['id'], message_ids)
        delivered_oldest = next(msg for msg in payload['messages'] if msg.get('id') == rows[0]['id'])
        self.assertEqual(delivered_oldest.get('created_at'), oldest_ts)

    def test_trusted_catchup_backfill_does_not_require_higher_local_message_count(self) -> None:
        self.trust_manager.set_trust_score('peer-origin', 100, reason='test-trusted')

        public_channel = self.channel_manager.create_channel(
            name='aviation-equal-count',
            channel_type=ChannelType.PUBLIC,
            created_by='owner-user',
            description='public room',
            privacy_mode='open',
        )
        assert public_channel is not None
        self.channel_manager.send_message(channel_id=public_channel.id, user_id='owner-user', content='older local message')
        self.channel_manager.send_message(channel_id=public_channel.id, user_id='owner-user', content='newer local message')
        with self.db_manager.get_connection() as conn:
            inserted_rows = conn.execute(
                "SELECT id, content FROM channel_messages WHERE channel_id = ? ORDER BY rowid ASC",
                (public_channel.id,),
            ).fetchall()
            older_id = next(row['id'] for row in inserted_rows if row['content'] == 'older local message')
            newer_id = next(row['id'] for row in inserted_rows if row['content'] == 'newer local message')
            conn.execute(
                "UPDATE channel_messages SET created_at = ? WHERE id = ?",
                ('2026-03-28 09:00:00', older_id),
            )
            conn.execute(
                "UPDATE channel_messages SET created_at = ? WHERE id = ?",
                ('2026-03-31 09:00:00', newer_id),
            )
            conn.commit()

        with self.db_manager.get_connection() as conn:
            rows = conn.execute(
                "SELECT id, created_at FROM channel_messages WHERE channel_id = ? ORDER BY created_at ASC",
                (public_channel.id,),
            ).fetchall()
        oldest_ts = rows[0]['created_at']
        newest_ts = rows[-1]['created_at']

        with patch('asyncio.ensure_future', lambda coro: asyncio.run(coro)):
            self.p2p_manager.on_catchup_request(
                {public_channel.id: newest_ts},
                'peer-origin',
                channel_ranges={
                    public_channel.id: {
                        'latest': newest_ts,
                        'oldest': newest_ts,
                        'message_count': 2,
                    }
                },
            )

        self.assertEqual(len(self.p2p_manager.sent_catchup), 1)
        payload = self.p2p_manager.sent_catchup[0]
        message_ids = [msg.get('id') for msg in payload['messages']]
        self.assertIn(rows[0]['id'], message_ids)
        delivered_oldest = next(msg for msg in payload['messages'] if msg.get('id') == rows[0]['id'])
        self.assertEqual(delivered_oldest.get('created_at'), oldest_ts)


    def test_untrusted_catchup_response_materializes_public_channel_only(self) -> None:
        self._mark_peer_untrusted('peer-guest')

        self.p2p_manager.on_catchup_response(
            [
                {
                    'id': 'Mpub001',
                    'channel_id': 'Cpubcatchup001',
                    'channel_name': 'public-deck',
                    'channel_type': 'public',
                    'channel_privacy_mode': 'open',
                    'channel_origin_peer': 'peer-guest',
                    'user_id': 'remote-user',
                    'content': 'public payload',
                    'message_type': 'text',
                    'created_at': '2026-03-30T12:00:00+00:00',
                },
                {
                    'id': 'Mpriv001',
                    'channel_id': 'Cprivcatchup001',
                    'channel_name': 'private-deck',
                    'channel_type': 'private',
                    'channel_privacy_mode': 'private',
                    'channel_origin_peer': 'peer-guest',
                    'user_id': 'remote-user',
                    'content': 'private payload',
                    'message_type': 'text',
                    'created_at': '2026-03-30T12:00:01+00:00',
                },
            ],
            'peer-guest',
        )

        with self.db_manager.get_connection() as conn:
            public_channel = conn.execute(
                "SELECT id, name, channel_type, privacy_mode FROM channels WHERE id = 'Cpubcatchup001'"
            ).fetchone()
            private_channel = conn.execute(
                "SELECT id FROM channels WHERE id = 'Cprivcatchup001'"
            ).fetchone()
            stored_messages = conn.execute(
                "SELECT id, channel_id, content FROM channel_messages ORDER BY id ASC"
            ).fetchall()
            local_membership = conn.execute(
                "SELECT 1 FROM channel_members WHERE channel_id = 'Cpubcatchup001' AND user_id = 'owner-user'"
            ).fetchone()

        self.assertIsNotNone(public_channel)
        self.assertEqual(public_channel['name'], 'public-deck')
        self.assertEqual(public_channel['channel_type'], 'public')
        self.assertEqual(public_channel['privacy_mode'], 'open')
        self.assertIsNone(private_channel)
        self.assertEqual([(row['id'], row['channel_id']) for row in stored_messages], [('Mpub001', 'Cpubcatchup001')])
        self.assertIsNotNone(local_membership)

    def test_channel_sync_upgrades_placeholder_and_backfills_local_membership(self) -> None:
        self._mark_peer_untrusted('peer-guest')

        with self.db_manager.get_connection() as conn:
            conn.execute(
                """
                INSERT INTO channels (
                    id, name, channel_type, created_by, description, origin_peer, privacy_mode, created_at
                ) VALUES (?, ?, 'private', ?, ?, ?, 'private', CURRENT_TIMESTAMP)
                """,
                ('Cpubupgrade001', 'peer-channel-Cpubupgr', 'owner-user', 'Auto-created from P2P catchup', 'peer-guest'),
            )
            conn.commit()

        self.p2p_manager.on_channel_sync(
            [
                {
                    'id': 'Cpubupgrade001',
                    'name': 'synced-public',
                    'type': 'public',
                    'desc': 'public upgrade',
                    'privacy_mode': 'open',
                    'origin_peer': 'peer-guest',
                },
            ],
            'peer-guest',
        )

        with self.db_manager.get_connection() as conn:
            upgraded = conn.execute(
                "SELECT name, channel_type, privacy_mode FROM channels WHERE id = 'Cpubupgrade001'"
            ).fetchone()
            local_membership = conn.execute(
                "SELECT 1 FROM channel_members WHERE channel_id = 'Cpubupgrade001' AND user_id = 'owner-user'"
            ).fetchone()

        self.assertIsNotNone(upgraded)
        self.assertEqual(upgraded['name'], 'synced-public')
        self.assertEqual(upgraded['channel_type'], 'public')
        self.assertEqual(upgraded['privacy_mode'], 'open')
        self.assertIsNotNone(local_membership)

    def test_relayed_public_channel_announce_uses_created_by_peer_authority(self) -> None:
        self.trust_manager.set_trust_score('peer-relay', 100, reason='test-relay-trusted')

        with self.db_manager.get_connection() as conn:
            conn.execute(
                """
                INSERT INTO channels (
                    id, name, channel_type, created_by, description, origin_peer, privacy_mode, created_at
                ) VALUES (?, ?, 'private', ?, ?, ?, 'private', CURRENT_TIMESTAMP)
                """,
                ('Crelaypub001', 'peer-channel-Crelaypu', 'owner-user', 'Auto-created from P2P catchup', 'peer-origin'),
            )
            conn.commit()

        self.p2p_manager.on_channel_announce(
            channel_id='Crelaypub001',
            name='breaking-news',
            channel_type='public',
            description='relayed public channel',
            created_by_peer='peer-origin',
            created_by_user_id='owner-user',
            privacy_mode='open',
            from_peer='peer-relay',
            initial_members=None,
        )

        with self.db_manager.get_connection() as conn:
            upgraded = conn.execute(
                "SELECT name, channel_type, privacy_mode, origin_peer FROM channels WHERE id = 'Crelaypub001'"
            ).fetchone()
            local_membership = conn.execute(
                "SELECT 1 FROM channel_members WHERE channel_id = 'Crelaypub001' AND user_id = 'owner-user'"
            ).fetchone()

        self.assertIsNotNone(upgraded)
        self.assertEqual(upgraded['name'], 'breaking-news')
        self.assertEqual(upgraded['channel_type'], 'public')
        self.assertEqual(upgraded['privacy_mode'], 'open')
        self.assertEqual(upgraded['origin_peer'], 'peer-origin')
        self.assertIsNotNone(local_membership)

    def test_non_origin_public_sync_requests_authoritative_reconcile(self) -> None:
        self.trust_manager.set_trust_score('peer-relay', 100, reason='test-relay-trusted')

        with self.db_manager.get_connection() as conn:
            conn.execute(
                """
                INSERT INTO channels (
                    id, name, channel_type, created_by, description, origin_peer, privacy_mode, created_at
                ) VALUES (?, ?, 'private', ?, ?, ?, 'private', CURRENT_TIMESTAMP)
                """,
                ('Creconcile001', 'peer-channel-Creconci', 'owner-user', 'Auto-created from P2P catchup', 'peer-origin'),
            )
            conn.commit()

        self.p2p_manager.on_channel_sync(
            [
                {
                    'id': 'Creconcile001',
                    'name': 'breaking-news',
                    'type': 'public',
                    'desc': 'relayed without origin metadata',
                    'privacy_mode': 'open',
                },
            ],
            'peer-relay',
        )

        with self.db_manager.get_connection() as conn:
            placeholder = conn.execute(
                "SELECT name, channel_type, privacy_mode FROM channels WHERE id = 'Creconcile001'"
            ).fetchone()

        self.assertIsNotNone(placeholder)
        self.assertEqual(placeholder['name'], 'peer-channel-Creconci')
        self.assertEqual(placeholder['channel_type'], 'private')
        self.assertEqual(placeholder['privacy_mode'], 'private')
        self.assertEqual(len(self.p2p_manager.metadata_requests), 1)
        self.assertEqual(self.p2p_manager.metadata_requests[0]['to_peer'], 'peer-origin')
        self.assertEqual(self.p2p_manager.metadata_requests[0]['channel_ids'], ['Creconcile001'])

    def test_non_origin_public_name_hint_requests_reconcile_for_half_upgraded_placeholder(self) -> None:
        self.trust_manager.set_trust_score('peer-relay', 100, reason='test-relay-trusted')

        with self.db_manager.get_connection() as conn:
            conn.execute(
                """
                INSERT INTO channels (
                    id, name, channel_type, created_by, description, origin_peer, privacy_mode, created_at
                ) VALUES (?, ?, 'public', ?, ?, ?, 'open', CURRENT_TIMESTAMP)
                """,
                ('Chalfpub001', 'peer-channel-Chalfpub', 'owner-user', 'Auto-created from P2P catchup', 'peer-origin'),
            )
            conn.commit()

        self.p2p_manager.on_channel_sync(
            [
                {
                    'id': 'Chalfpub001',
                    'name': 'breaking-news',
                    'type': 'public',
                    'desc': 'relayed canonical public name',
                    'privacy_mode': 'open',
                },
            ],
            'peer-relay',
        )

        with self.db_manager.get_connection() as conn:
            placeholder = conn.execute(
                "SELECT name, channel_type, privacy_mode FROM channels WHERE id = 'Chalfpub001'"
            ).fetchone()

        self.assertIsNotNone(placeholder)
        self.assertEqual(placeholder['name'], 'peer-channel-Chalfpub')
        self.assertEqual(placeholder['channel_type'], 'public')
        self.assertEqual(placeholder['privacy_mode'], 'open')
        self.assertEqual(len(self.p2p_manager.metadata_requests), 1)
        self.assertEqual(self.p2p_manager.metadata_requests[0]['to_peer'], 'peer-origin')
        self.assertEqual(self.p2p_manager.metadata_requests[0]['channel_ids'], ['Chalfpub001'])

    def test_authoritative_metadata_response_finalizes_placeholder_rename(self) -> None:
        self.trust_manager.set_trust_score('peer-relay', 100, reason='test-relay-trusted')
        self.trust_manager.set_trust_score('peer-origin', 100, reason='test-origin-trusted')

        with self.db_manager.get_connection() as conn:
            conn.execute(
                """
                INSERT INTO channels (
                    id, name, channel_type, created_by, description, origin_peer, privacy_mode, created_at
                ) VALUES (?, ?, 'public', ?, ?, ?, 'open', CURRENT_TIMESTAMP)
                """,
                ('Cfinalize001', 'peer-channel-Cfinaliz', 'owner-user', 'Auto-created from P2P catchup', 'peer-origin'),
            )
            conn.commit()

        self.p2p_manager.on_channel_sync(
            [
                {
                    'id': 'Cfinalize001',
                    'name': 'canopy-radio',
                    'type': 'public',
                    'desc': 'relayed canonical public name',
                    'privacy_mode': 'open',
                },
            ],
            'peer-relay',
        )

        self.assertEqual(len(self.p2p_manager.metadata_requests), 1)
        self.assertEqual(self.p2p_manager.metadata_requests[0]['channel_ids'], ['Cfinalize001'])

        self.p2p_manager.on_channel_announce(
            channel_id='Cfinalize001',
            name='canopy-radio',
            channel_type='public',
            description='authoritative public name',
            created_by_peer='peer-origin',
            created_by_user_id='owner-user',
            privacy_mode='open',
            from_peer='peer-origin',
            initial_members=None,
        )

        with self.db_manager.get_connection() as conn:
            finalized = conn.execute(
                "SELECT name, channel_type, privacy_mode FROM channels WHERE id = 'Cfinalize001'"
            ).fetchone()

        self.assertIsNotNone(finalized)
        self.assertEqual(finalized['name'], 'canopy-radio')
        self.assertEqual(finalized['channel_type'], 'public')
        self.assertEqual(finalized['privacy_mode'], 'open')

    def test_membership_recovery_rebinds_private_visibility_to_instance_owner(self) -> None:
        self.trust_manager.set_trust_score('peer-origin', 100, reason='test-origin-trusted')

        with self.db_manager.get_connection() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO users (
                    id, username, public_key, password_hash, display_name,
                    origin_peer, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """,
                ('old-owner', 'owner-old', 'pk-old-owner', 'pw-old-owner', 'Owner', None),
            )
            conn.commit()
        self.db_manager.set_instance_owner_user_id('owner-user')

        self.p2p_manager.on_channel_membership_response(
            'qry-1',
            [
                {
                    'channel_id': 'Cprivrebind001',
                    'name': 'hidden-private',
                    'channel_type': 'private',
                    'description': 'private continuity repair',
                    'origin_peer': 'peer-origin',
                    'privacy_mode': 'private',
                    'members': [
                        {
                            'user_id': 'old-owner',
                            'origin_peer': '',
                            'display_name': 'Owner',
                            'role': 'member',
                        },
                        {
                            'user_id': 'remote-user',
                            'origin_peer': 'peer-origin',
                            'display_name': 'Remote User',
                            'role': 'member',
                        },
                    ],
                },
            ],
            False,
            'peer-origin',
        )

        with self.db_manager.get_connection() as conn:
            owner_membership = conn.execute(
                "SELECT 1 FROM channel_members WHERE channel_id = 'Cprivrebind001' AND user_id = 'owner-user'"
            ).fetchone()
            stale_membership = conn.execute(
                "SELECT 1 FROM channel_members WHERE channel_id = 'Cprivrebind001' AND user_id = 'old-owner'"
            ).fetchone()

        self.assertIsNotNone(owner_membership)
        self.assertIsNotNone(stale_membership)

    def test_untrusted_catchup_upgrades_existing_placeholder_via_channel_origin(self) -> None:
        self._mark_peer_untrusted('peer-relay')

        with self.db_manager.get_connection() as conn:
            conn.execute(
                """
                INSERT INTO channels (
                    id, name, channel_type, created_by, description, origin_peer, privacy_mode, created_at
                ) VALUES (?, ?, 'private', ?, ?, ?, 'private', CURRENT_TIMESTAMP)
                """,
                ('Ccatchuprel001', 'peer-channel-Ccatchup', 'owner-user', 'Auto-created from P2P catchup', 'peer-origin'),
            )
            conn.commit()

        self.p2p_manager.on_catchup_response(
            [
                {
                    'id': 'Mrelay001',
                    'channel_id': 'Ccatchuprel001',
                    'channel_name': 'breaking-news',
                    'channel_type': 'public',
                    'channel_privacy_mode': 'open',
                    'channel_origin_peer': 'peer-origin',
                    'user_id': 'remote-user',
                    'content': 'relayed public payload',
                    'message_type': 'text',
                    'created_at': '2026-03-30T12:05:00+00:00',
                },
            ],
            'peer-relay',
        )

        with self.db_manager.get_connection() as conn:
            upgraded = conn.execute(
                "SELECT name, channel_type, privacy_mode, origin_peer FROM channels WHERE id = 'Ccatchuprel001'"
            ).fetchone()
            local_membership = conn.execute(
                "SELECT 1 FROM channel_members WHERE channel_id = 'Ccatchuprel001' AND user_id = 'owner-user'"
            ).fetchone()
            stored_message = conn.execute(
                "SELECT id, channel_id FROM channel_messages WHERE id = 'Mrelay001'"
            ).fetchone()

        self.assertIsNotNone(upgraded)
        self.assertEqual(upgraded['name'], 'breaking-news')
        self.assertEqual(upgraded['channel_type'], 'public')
        self.assertEqual(upgraded['privacy_mode'], 'open')
        self.assertEqual(upgraded['origin_peer'], 'peer-origin')
        self.assertIsNotNone(local_membership)
        self.assertIsNotNone(stored_message)

    def test_public_channel_membership_repair_backfills_existing_rows(self) -> None:
        with self.db_manager.get_connection() as conn:
            conn.execute(
                """
                INSERT INTO channels (
                    id, name, channel_type, created_by, description, privacy_mode, created_at
                ) VALUES (?, ?, 'public', ?, ?, 'open', CURRENT_TIMESTAMP)
                """,
                ('Crepair001', 'repair-public', 'owner-user', 'repair target'),
            )
            conn.commit()

        repaired = self.channel_manager._repair_public_channel_memberships()

        with self.db_manager.get_connection() as conn:
            local_membership = conn.execute(
                "SELECT 1 FROM channel_members WHERE channel_id = 'Crepair001' AND user_id = 'owner-user'"
            ).fetchone()

        self.assertGreaterEqual(repaired, 1)
        self.assertIsNotNone(local_membership)

    def test_startup_repairs_private_channel_visibility_for_instance_owner(self) -> None:
        with self.db_manager.get_connection() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO users (
                    id, username, public_key, password_hash, display_name,
                    origin_peer, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """,
                ('old-owner', 'owner-old', 'pk-old-owner', 'pw-old-owner', 'Owner', None),
            )
            conn.commit()
        self.db_manager.set_instance_owner_user_id('owner-user')

        private_channel = self.channel_manager.create_channel(
            name='stale-private',
            channel_type=ChannelType.PRIVATE,
            created_by='old-owner',
            description='stale private continuity',
            privacy_mode='private',
            initial_members=['old-owner'],
        )
        assert private_channel is not None
        self.channel_manager.send_message(
            channel_id=private_channel.id,
            user_id='old-owner',
            content='stale private history',
        )

        repaired_app = create_app()
        repaired_db = repaired_app.config['DB_MANAGER']

        with repaired_db.get_connection() as conn:
            owner_membership = conn.execute(
                "SELECT 1 FROM channel_members WHERE channel_id = ? AND user_id = 'owner-user'",
                (private_channel.id,),
            ).fetchone()

        self.assertIsNotNone(owner_membership)

    def test_resync_user_avatar_reads_sqlite_rows_without_row_get(self) -> None:
        with self.db_manager.get_connection() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO users (
                    id, username, public_key, password_hash, display_name,
                    origin_peer, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """,
                ('avatar-user', 'avatar-user', 'pk-avatar', None, 'Avatar User', 'peer-origin'),
            )
            conn.commit()

        result = self.p2p_manager.resync_user_avatar('avatar-user')

        self.assertTrue(result['ok'])
        self.assertEqual(result['origin_peer'], 'peer-origin')
        self.assertEqual(self.p2p_manager.sync_requests, ['peer-origin'])
