"""Tests for private channel membership recovery payload generation."""

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

from canopy.core.channels import ChannelManager


class _FakeDbManager:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._system_state = {}

    def get_connection(self) -> sqlite3.Connection:
        return self._conn

    def get_system_state(self, key: str):
        return self._system_state.get(key)

    def set_system_state(self, key: str, value):
        if value is None:
            self._system_state.pop(key, None)
        else:
            self._system_state[key] = value
        return True


class TestChannelMembershipRecovery(unittest.TestCase):
    def setUp(self) -> None:
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
                account_type TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        self.conn.executemany(
            """
            INSERT INTO users (id, username, public_key, password_hash, display_name, origin_peer, account_type)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                ('user-local-a', 'local-a', 'pk-a', 'hash-a', 'Local A', 'peer-a', 'human'),
                ('user-local-b', 'local-b', 'pk-b', 'hash-b', 'Local B', 'peer-a', 'agent'),
                ('user-origin', 'origin', 'pk-o', 'hash-o', 'Origin User', 'peer-origin', 'human'),
            ],
        )
        self.conn.commit()
        self.db = _FakeDbManager(self.conn)
        self.manager = ChannelManager(self.db, MagicMock())

    def tearDown(self) -> None:
        self.conn.close()

    def _seed_private_channel(self, channel_id: str, name: str = 'private-room') -> None:
        self.conn.execute(
            """
            INSERT OR REPLACE INTO channels (
                id, name, channel_type, created_by, description, origin_peer, privacy_mode, crypto_mode
            ) VALUES (?, ?, 'private', 'user-origin', 'desc', 'peer-origin', 'private', 'e2e_optional')
            """,
            (channel_id, name),
        )
        self.conn.executemany(
            """
            INSERT OR REPLACE INTO channel_members (channel_id, user_id, role)
            VALUES (?, ?, ?)
            """,
            [
                (channel_id, 'user-local-a', 'member'),
                (channel_id, 'user-origin', 'admin'),
            ],
        )
        self.conn.commit()

    def test_recovery_returns_private_channel_for_querying_peer_users(self) -> None:
        self._seed_private_channel('Cprivate1')

        payload = self.manager.get_private_channel_recovery_payload(
            query_user_ids=['user-local-a'],
            requester_peer_id='peer-a',
            limit=20,
        )

        channels = payload.get('channels') or []
        self.assertEqual(len(channels), 1)
        self.assertEqual(channels[0].get('channel_id'), 'Cprivate1')
        member_ids = {m.get('user_id') for m in (channels[0].get('members') or [])}
        self.assertIn('user-local-a', member_ids)
        self.assertIn('user-origin', member_ids)

    def test_recovery_excludes_pending_member_removals(self) -> None:
        self._seed_private_channel('Cprivate2')
        self.conn.execute(
            """
            INSERT INTO channel_member_sync_deliveries (
                sync_id, channel_id, target_user_id, action, role,
                target_peer_id, delivery_state, acked_at
            ) VALUES (?, ?, ?, 'remove', 'member', ?, 'sent', NULL)
            """,
            ('MS-remove-1', 'Cprivate2', 'user-local-a', 'peer-a'),
        )
        self.conn.commit()

        payload = self.manager.get_private_channel_recovery_payload(
            query_user_ids=['user-local-a'],
            requester_peer_id='peer-a',
            limit=20,
        )

        channels = payload.get('channels') or []
        self.assertEqual(channels, [])

    def test_recovery_response_is_bounded_and_marks_truncated(self) -> None:
        for idx in range(5):
            self._seed_private_channel(f'Cpriv-{idx}', name=f'private-{idx}')

        payload = self.manager.get_private_channel_recovery_payload(
            query_user_ids=['user-local-a'],
            requester_peer_id='peer-a',
            limit=3,
        )

        channels = payload.get('channels') or []
        self.assertEqual(len(channels), 3)
        self.assertTrue(payload.get('truncated'))

    def test_recovery_excludes_local_agent_quarantine_channel(self) -> None:
        self._seed_private_channel(
            self.manager.AGENT_START_CHANNEL_ID,
            name=self.manager.AGENT_START_CHANNEL_NAME,
        )

        payload = self.manager.get_private_channel_recovery_payload(
            query_user_ids=['user-local-a'],
            requester_peer_id='peer-a',
            limit=20,
        )

        channels = payload.get('channels') or []
        self.assertEqual(channels, [])

    def test_recovery_username_fallback_returns_channel_when_ids_are_replaced(self) -> None:
        self.conn.execute(
            """
            INSERT INTO users (id, username, public_key, password_hash, display_name, origin_peer, account_type)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            ('user-old-a', 'restart-a', 'pk-old-a', 'hash-old-a', 'Restart A', 'peer-a', 'human'),
        )
        self.conn.commit()
        self._seed_private_channel('Crestart1')
        self.conn.execute("DELETE FROM channel_members WHERE channel_id = ? AND user_id = ?", ('Crestart1', 'user-local-a'))
        self.conn.execute(
            "INSERT OR REPLACE INTO channel_members (channel_id, user_id, role) VALUES (?, ?, ?)",
            ('Crestart1', 'user-old-a', 'member'),
        )
        self.conn.commit()

        payload = self.manager.get_private_channel_recovery_payload(
            query_user_ids=['user-new-a'],
            requester_peer_id='peer-a',
            limit=20,
            query_username_hints={'user-new-a': 'restart-a'},
        )

        channels = payload.get('channels') or []
        self.assertEqual(len(channels), 1)
        self.assertEqual(channels[0].get('channel_id'), 'Crestart1')
        self.assertEqual(payload.get('queried_users'), ['user-old-a'])

    def test_recovery_direct_id_match_takes_priority_over_username_hints(self) -> None:
        self.conn.execute(
            """
            INSERT INTO users (id, username, public_key, password_hash, display_name, origin_peer, account_type)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            ('user-old-direct', 'old-direct', 'pk-old-direct', 'hash-old-direct', 'Old Direct', 'peer-a', 'human'),
        )
        self.conn.commit()
        self._seed_private_channel('Cdirect1')

        payload = self.manager.get_private_channel_recovery_payload(
            query_user_ids=['user-local-a'],
            requester_peer_id='peer-a',
            limit=20,
            query_username_hints={'user-local-a': 'old-direct'},
        )

        self.assertEqual(payload.get('queried_users'), ['user-local-a'])
        channels = payload.get('channels') or []
        self.assertEqual(len(channels), 1)
        member_ids = {m.get('user_id') for m in (channels[0].get('members') or [])}
        self.assertIn('user-local-a', member_ids)
        self.assertNotIn('user-old-direct', member_ids)

    def test_recovery_does_not_fallback_without_username_hints(self) -> None:
        self.conn.execute(
            """
            INSERT INTO users (id, username, public_key, password_hash, display_name, origin_peer, account_type)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            ('user-old-nohint', 'restart-nohint', 'pk-old-nohint', 'hash-old-nohint', 'Restart No Hint', 'peer-a', 'human'),
        )
        self.conn.commit()
        self._seed_private_channel('Cnohint1')
        self.conn.execute("DELETE FROM channel_members WHERE channel_id = ? AND user_id = ?", ('Cnohint1', 'user-local-a'))
        self.conn.execute(
            "INSERT OR REPLACE INTO channel_members (channel_id, user_id, role) VALUES (?, ?, ?)",
            ('Cnohint1', 'user-old-nohint', 'member'),
        )
        self.conn.commit()

        payload = self.manager.get_private_channel_recovery_payload(
            query_user_ids=['user-new-nohint'],
            requester_peer_id='peer-a',
            limit=20,
        )

        self.assertEqual(payload.get('channels'), [])
        self.assertEqual(payload.get('queried_users'), [])

    def test_recovery_username_fallback_is_scoped_to_requesting_peer(self) -> None:
        self.conn.execute(
            """
            INSERT INTO users (id, username, public_key, password_hash, display_name, origin_peer, account_type)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            ('user-peer-b', 'restart-b', 'pk-peer-b', 'hash-peer-b', 'Restart B', 'peer-b', 'human'),
        )
        self.conn.commit()
        self._seed_private_channel('Cscope1')
        self.conn.execute("DELETE FROM channel_members WHERE channel_id = ? AND user_id = ?", ('Cscope1', 'user-local-a'))
        self.conn.execute(
            "INSERT OR REPLACE INTO channel_members (channel_id, user_id, role) VALUES (?, ?, ?)",
            ('Cscope1', 'user-peer-b', 'member'),
        )
        self.conn.commit()

        payload = self.manager.get_private_channel_recovery_payload(
            query_user_ids=['user-new-a'],
            requester_peer_id='peer-a',
            limit=20,
            query_username_hints={'user-new-a': 'restart-b'},
        )

        self.assertEqual(payload.get('channels'), [])
        self.assertEqual(payload.get('queried_users'), [])


if __name__ == '__main__':
    unittest.main()
