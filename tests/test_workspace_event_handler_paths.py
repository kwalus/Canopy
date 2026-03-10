"""End-to-end handler-path regressions for workspace event Patch 1."""

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
        self.delete_acks = []
        self.on_direct_message = None
        self.on_delete_signal = None
        self.on_peer_connected = None

    def set_relay_policy(self, policy):
        self.relay_policy = policy

    def get_peer_id(self):
        return self.local_identity.peer_id

    def peer_supports_capability(self, peer_id, capability):
        return False

    def describe_direct_message_security(self, recipient_ids):
        return {
            'mode': 'legacy_plaintext',
            'state': 'plaintext',
            'label': 'Legacy relay/plaintext',
            'relay_confidential': False,
            'local_only': False,
        }

    def is_running(self):
        return self._running

    def start(self):
        self._running = True

    def send_delete_signal_ack(self, peer_id, signal_id, status):
        self.delete_acks.append({
            'peer_id': peer_id,
            'signal_id': signal_id,
            'status': status,
        })
        return True


class TestWorkspaceEventHandlerPaths(unittest.TestCase):
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
        self.inbox_manager = self.app.config['INBOX_MANAGER']
        self.p2p_manager = self.app.config['P2P_MANAGER']

        self._seed_local_recipient()

    def _seed_local_recipient(self) -> None:
        with self.app.app_context():
            with self.db_manager.get_connection() as conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO users (
                        id, username, public_key, password_hash, display_name,
                        origin_peer, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                    """,
                    (
                        'agent-local',
                        'agent_local',
                        'pk-local',
                        'pw-local',
                        'Agent Local',
                        None,
                    ),
                )
                conn.commit()
        self.inbox_manager.set_config(
            'agent-local',
            {
                'trusted_only': False,
                'allowed_trigger_types': ['mention', 'dm', 'reply', 'channel_added'],
                'cooldown_seconds': 0,
                'sender_cooldown_seconds': 0,
                'agent_sender_cooldown_seconds': 0,
                'channel_burst_limit': 100,
                'channel_hourly_limit': 1000,
                'sender_hourly_limit': 1000,
                'max_pending': 100,
                'expire_days': 14,
            },
        )

    def _seed_remote_shadow(self, user_id: str, origin_peer: str) -> None:
        with self.app.app_context():
            with self.db_manager.get_connection() as conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO users (
                        id, username, public_key, password_hash, display_name,
                        origin_peer, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                    """,
                    (
                        user_id,
                        f'peer-{user_id[:8]}',
                        '',
                        None,
                        'Remote Shadow',
                        origin_peer,
                    ),
                )
                conn.commit()

    def test_real_inbound_dm_create_emits_one_created_event_with_canonical_id(self) -> None:
        with self.app.app_context():
            self.p2p_manager.on_direct_message(
                sender_id='remote-user',
                recipient_id='agent-local',
                content='remote hello',
                message_id='DM-handler-1',
                timestamp='2026-03-09T12:00:00+00:00',
                display_name='Remote User',
                metadata={},
                update_only=False,
                edited_at=None,
                from_peer='peer-remote',
            )
            with self.db_manager.get_connection() as conn:
                messages = conn.execute(
                    "SELECT id FROM messages ORDER BY id ASC",
                ).fetchall()
                event_rows = conn.execute(
                    """
                    SELECT event_type, message_id
                    FROM workspace_events
                    WHERE event_type = 'dm.message.created'
                    """,
                ).fetchall()
            self.assertEqual([row['id'] for row in messages], ['DM-handler-1'])
            self.assertEqual(len(event_rows), 1)
            self.assertEqual(event_rows[0]['message_id'], 'DM-handler-1')

    def test_real_inbound_dm_delete_emits_one_deleted_event_and_cleans_inbox(self) -> None:
        with self.app.app_context():
            self.p2p_manager.on_direct_message(
                sender_id='remote-user',
                recipient_id='agent-local',
                content='remote delete me',
                message_id='DM-handler-delete',
                timestamp='2026-03-09T12:01:00+00:00',
                display_name='Remote User',
                metadata={},
                update_only=False,
                edited_at=None,
                from_peer='peer-remote',
            )
            with self.db_manager.get_connection() as conn:
                inbox_before = conn.execute(
                    """
                    SELECT COUNT(*) AS n
                    FROM agent_inbox
                    WHERE source_type = 'dm' AND source_id = ?
                    """,
                    ('DM-handler-delete',),
                ).fetchone()['n']
            self.assertGreaterEqual(inbox_before, 1)

            self.p2p_manager.on_delete_signal(
                signal_id='DS-handler-1',
                data_type='direct_message',
                data_id='DM-handler-delete',
                reason='user_delete',
                requester_peer='peer-remote',
                is_ack=False,
                ack_status=None,
                from_peer='peer-remote',
            )

            with self.db_manager.get_connection() as conn:
                remaining_message = conn.execute(
                    "SELECT id FROM messages WHERE id = ?",
                    ('DM-handler-delete',),
                ).fetchone()
                inbox_after = conn.execute(
                    """
                    SELECT COUNT(*) AS n
                    FROM agent_inbox
                    WHERE source_type = 'dm' AND source_id = ?
                    """,
                    ('DM-handler-delete',),
                ).fetchone()['n']
                delete_events = conn.execute(
                    """
                    SELECT message_id
                    FROM workspace_events
                    WHERE event_type = 'dm.message.deleted'
                    """,
                ).fetchall()

            self.assertIsNone(remaining_message)
            self.assertEqual(inbox_after, 0)
            self.assertEqual([row['message_id'] for row in delete_events], ['DM-handler-delete'])

    def test_profile_sync_does_not_reassign_existing_remote_origin(self) -> None:
        self._seed_remote_shadow('remote-user', 'peer-windy')

        with self.app.app_context():
            self.p2p_manager.on_profile_sync(
                profile_data={
                    'user_id': 'remote-user',
                    'peer_id': 'peer-relay',
                    'display_name': 'Remote User',
                    'profile_hash': 'hash-profile-1',
                },
                from_peer='peer-relay',
            )
            with self.db_manager.get_connection() as conn:
                row = conn.execute(
                    "SELECT origin_peer FROM users WHERE id = ?",
                    ('remote-user',),
                ).fetchone()

        self.assertIsNotNone(row)
        self.assertEqual(row['origin_peer'], 'peer-windy')

    def test_real_inbound_dm_repairs_remote_origin_from_sender_peer(self) -> None:
        self._seed_remote_shadow('remote-user', 'peer-stale')

        with self.app.app_context():
            self.p2p_manager.on_direct_message(
                sender_id='remote-user',
                recipient_id='agent-local',
                content='repair origin',
                message_id='DM-handler-repair',
                timestamp='2026-03-09T12:03:00+00:00',
                display_name='Remote User',
                metadata={},
                update_only=False,
                edited_at=None,
                from_peer='peer-windy',
            )
            with self.db_manager.get_connection() as conn:
                row = conn.execute(
                    "SELECT origin_peer FROM users WHERE id = ?",
                    ('remote-user',),
                ).fetchone()

        self.assertIsNotNone(row)
        self.assertEqual(row['origin_peer'], 'peer-windy')

    def test_real_inbound_dm_repairs_remote_shadow_misstamped_as_local(self) -> None:
        local_peer = self.p2p_manager.get_peer_id()
        assert local_peer
        self._seed_remote_shadow('remote-user', local_peer)

        with self.app.app_context():
            self.p2p_manager.on_direct_message(
                sender_id='remote-user',
                recipient_id='agent-local',
                content='repair local stamp',
                message_id='DM-handler-repair-local',
                timestamp='2026-03-09T12:03:30+00:00',
                display_name='Remote User',
                metadata={},
                update_only=False,
                edited_at=None,
                from_peer='peer-windy',
            )
            with self.db_manager.get_connection() as conn:
                row = conn.execute(
                    "SELECT origin_peer FROM users WHERE id = ?",
                    ('remote-user',),
                ).fetchone()

        self.assertIsNotNone(row)
        self.assertEqual(row['origin_peer'], 'peer-windy')

    def test_catchup_uses_message_origin_peer_not_relay_peer_for_shadow_updates(self) -> None:
        self._seed_remote_shadow('remote-user', 'peer-stale')

        with self.app.app_context():
            self.p2p_manager.on_catchup_response(
                messages=[
                    {
                        'id': 'M-catchup-origin',
                        'channel_id': 'C-catchup',
                        'user_id': 'remote-user',
                        'content': 'hello from catchup',
                        'created_at': '2026-03-09T12:04:00+00:00',
                        'origin_peer': 'peer-windy',
                        'display_name': 'Remote User',
                    }
                ],
                from_peer='peer-relay',
                feed_posts=[],
                circle_entries=[],
                circle_votes=[],
                circles=[],
                tasks=[],
            )
            with self.db_manager.get_connection() as conn:
                row = conn.execute(
                    "SELECT origin_peer FROM users WHERE id = ?",
                    ('remote-user',),
                ).fetchone()

        self.assertIsNotNone(row)
        self.assertEqual(row['origin_peer'], 'peer-windy')


if __name__ == '__main__':
    unittest.main()
