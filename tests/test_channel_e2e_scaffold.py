"""Regression tests for phase-1 private-channel E2E scaffolding."""

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


class TestChannelE2EScaffold(unittest.TestCase):
    def setUp(self) -> None:
        self.db = _FakeDbManager()
        self.channel_manager = ChannelManager(self.db, MagicMock())

    def tearDown(self) -> None:
        self.db.conn.close()

    def test_schema_contains_e2e_scaffold_columns_and_tables(self) -> None:
        channels_cols = {
            row['name']
            for row in self.db.conn.execute("PRAGMA table_info(channels)").fetchall()
        }
        self.assertIn('crypto_mode', channels_cols)

        msg_cols = {
            row['name']
            for row in self.db.conn.execute("PRAGMA table_info(channel_messages)").fetchall()
        }
        self.assertIn('encrypted_content', msg_cols)
        self.assertIn('crypto_state', msg_cols)
        self.assertIn('key_id', msg_cols)
        self.assertIn('nonce', msg_cols)

        key_table = self.db.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='channel_keys'"
        ).fetchone()
        member_key_table = self.db.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='channel_member_keys'"
        ).fetchone()
        member_sync_table = self.db.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='channel_member_sync_deliveries'"
        ).fetchone()
        self.assertIsNotNone(key_table)
        self.assertIsNotNone(member_key_table)
        self.assertIsNotNone(member_sync_table)

    def test_key_state_helpers_roundtrip(self) -> None:
        channel = self.channel_manager.create_channel(
            name='e2e-scaffold',
            channel_type=ChannelType.PRIVATE,
            created_by='owner-user',
            description='phase-1 scaffold channel',
            privacy_mode='private',
        )
        self.assertIsNotNone(channel)
        assert channel is not None

        self.assertEqual(
            self.channel_manager.get_channel_crypto_mode(channel.id),
            ChannelManager.CRYPTO_MODE_LEGACY,
        )
        self.assertTrue(
            self.channel_manager.set_channel_crypto_mode(
                channel.id,
                ChannelManager.CRYPTO_MODE_E2E_OPTIONAL,
            )
        )
        self.assertEqual(
            self.channel_manager.get_channel_crypto_mode(channel.id),
            ChannelManager.CRYPTO_MODE_E2E_OPTIONAL,
        )

        self.assertTrue(
            self.channel_manager.upsert_channel_key(
                channel_id=channel.id,
                key_id='key-001',
                key_material_enc='wrapped-key-material',
                created_by_peer='peer-alpha',
                metadata={'key_version': 1},
            )
        )
        active = self.channel_manager.get_active_channel_key(channel.id)
        self.assertIsNotNone(active)
        assert active is not None
        self.assertEqual(active['key_id'], 'key-001')
        self.assertEqual(active['key_material_enc'], 'wrapped-key-material')

        self.assertTrue(
            self.channel_manager.upsert_channel_member_key_state(
                channel_id=channel.id,
                key_id='key-001',
                peer_id='peer-alpha',
                delivery_state='acked',
                delivered=True,
                acked=True,
            )
        )
        states = self.channel_manager.get_channel_member_key_states(channel.id, 'key-001')
        self.assertEqual(len(states), 1)
        self.assertEqual(states[0]['peer_id'], 'peer-alpha')
        self.assertEqual(states[0]['delivery_state'], 'acked')
        self.assertIsNotNone(states[0]['acked_at'])

        key_row = self.channel_manager.get_channel_key(channel.id, 'key-001')
        self.assertIsNotNone(key_row)
        key_bytes = self.channel_manager.get_channel_key_bytes(channel.id, 'key-001')
        self.assertIsNone(
            key_bytes,
            "Scaffold test key is wrapped/plaintext marker, so decode should fail cleanly",
        )

        self.assertTrue(self.channel_manager.revoke_channel_key(channel.id, 'key-001'))
        self.assertIsNone(self.channel_manager.get_active_channel_key(channel.id))

    def test_pending_decrypt_helpers_roundtrip(self) -> None:
        channel = self.channel_manager.create_channel(
            name='pending-decrypt',
            channel_type=ChannelType.PRIVATE,
            created_by='owner-user',
            description='pending decrypt helper test',
            privacy_mode='private',
        )
        self.assertIsNotNone(channel)
        assert channel is not None

        self.db.conn.execute(
            """
            INSERT INTO channel_messages
                (id, channel_id, user_id, content, message_type, encrypted_content, crypto_state, key_id, nonce)
            VALUES
                (?, ?, ?, ?, 'text', ?, 'pending_decrypt', ?, ?)
            """,
            (
                'M_PENDING_1',
                channel.id,
                'owner-user',
                '',
                'ciphertext-b64',
                'key-001',
                'nonce-b64',
            ),
        )
        self.db.conn.commit()

        rows = self.channel_manager.get_pending_decrypt_messages(channel.id, 'key-001')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['id'], 'M_PENDING_1')
        self.assertEqual(rows[0]['channel_id'], channel.id)
        self.assertEqual(rows[0]['user_id'], 'owner-user')

        updated = self.channel_manager.update_message_decrypt(
            message_id='M_PENDING_1',
            content='hello world',
            new_state='decrypted',
        )
        self.assertTrue(updated)

        row = self.db.conn.execute(
            "SELECT content, crypto_state FROM channel_messages WHERE id = ?",
            ('M_PENDING_1',),
        ).fetchone()
        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual(row['content'], 'hello world')
        self.assertEqual(row['crypto_state'], 'decrypted')

    def test_retryable_channel_member_key_states_excludes_acked_and_revoked(self) -> None:
        channel = self.channel_manager.create_channel(
            name='retryable-keys',
            channel_type=ChannelType.PRIVATE,
            created_by='owner-user',
            description='retry state selection',
            privacy_mode='private',
        )
        self.assertIsNotNone(channel)
        assert channel is not None

        self.assertTrue(
            self.channel_manager.upsert_channel_key(
                channel_id=channel.id,
                key_id='key-active',
                key_material_enc='raw:YWJjZA==',
                created_by_peer='peer-owner',
                metadata={'key_version': 1},
            )
        )
        self.assertTrue(
            self.channel_manager.upsert_channel_key(
                channel_id=channel.id,
                key_id='key-revoked',
                key_material_enc='raw:ZWZn',
                created_by_peer='peer-owner',
                metadata={'key_version': 1},
            )
        )
        self.assertTrue(
            self.channel_manager.upsert_channel_key(
                channel_id=channel.id,
                key_id='key-acked',
                key_material_enc='raw:aGlq',
                created_by_peer='peer-owner',
                metadata={'key_version': 1},
            )
        )
        self.assertTrue(self.channel_manager.revoke_channel_key(channel.id, 'key-revoked'))

        self.assertTrue(
            self.channel_manager.upsert_channel_member_key_state(
                channel_id=channel.id,
                key_id='key-active',
                peer_id='peer-target',
                delivery_state='failed',
                delivered=False,
                acked=False,
            )
        )
        self.assertTrue(
            self.channel_manager.upsert_channel_member_key_state(
                channel_id=channel.id,
                key_id='key-revoked',
                peer_id='peer-target',
                delivery_state='failed',
                delivered=False,
                acked=False,
            )
        )
        self.assertTrue(
            self.channel_manager.upsert_channel_member_key_state(
                channel_id=channel.id,
                key_id='key-acked',
                peer_id='peer-target',
                delivery_state='acked',
                delivered=True,
                acked=True,
            )
        )

        retryable = self.channel_manager.get_retryable_channel_member_key_states('peer-target', limit=50)
        self.assertEqual(len(retryable), 1)
        self.assertEqual(retryable[0]['key_id'], 'key-active')
        self.assertEqual(retryable[0]['peer_id'], 'peer-target')

    def test_member_sync_delivery_helpers_roundtrip(self) -> None:
        channel = self.channel_manager.create_channel(
            name='member-sync',
            channel_type=ChannelType.PRIVATE,
            created_by='owner-user',
            description='member sync delivery state',
            privacy_mode='private',
        )
        self.assertIsNotNone(channel)
        assert channel is not None

        sync_id = 'MS_test_001'
        queued = self.channel_manager.queue_member_sync_delivery(
            sync_id=sync_id,
            channel_id=channel.id,
            target_user_id='member-user',
            action='add',
            role='member',
            target_peer_id='peer-target',
            payload={
                'channel_name': channel.name,
                'channel_type': channel.channel_type.value,
                'channel_description': channel.description,
                'privacy_mode': channel.privacy_mode,
            },
        )
        self.assertTrue(queued)
        self.assertTrue(
            self.channel_manager.mark_member_sync_delivery_attempt(
                sync_id=sync_id,
                sent=True,
            )
        )

        retryable = self.channel_manager.get_retryable_member_sync_deliveries(
            peer_id='peer-target',
            limit=10,
            min_retry_seconds=0,
        )
        self.assertEqual(len(retryable), 1)
        self.assertEqual(retryable[0]['sync_id'], sync_id)
        self.assertEqual(retryable[0]['action'], 'add')
        self.assertEqual(retryable[0]['target_user_id'], 'member-user')

        self.assertTrue(
            self.channel_manager.mark_member_sync_delivery_acked(
                sync_id=sync_id,
                status='ok',
            )
        )
        retryable_after_ack = self.channel_manager.get_retryable_member_sync_deliveries(
            peer_id='peer-target',
            limit=10,
            min_retry_seconds=0,
        )
        self.assertEqual(len(retryable_after_ack), 0)

    def test_mark_stale_pending_decrypt_marks_only_old_rows(self) -> None:
        channel = self.channel_manager.create_channel(
            name='pending-stale',
            channel_type=ChannelType.PRIVATE,
            created_by='owner-user',
            description='pending decrypt stale sweep',
            privacy_mode='private',
        )
        self.assertIsNotNone(channel)
        assert channel is not None

        self.db.conn.execute(
            """
            INSERT INTO channel_messages
                (id, channel_id, user_id, content, message_type, encrypted_content, crypto_state, key_id, nonce, created_at)
            VALUES
                (?, ?, ?, ?, 'text', ?, 'pending_decrypt', ?, ?, datetime('now', '-48 hours'))
            """,
            ('M_PENDING_OLD', channel.id, 'owner-user', '', 'ciphertext-old', 'key-001', 'nonce-old'),
        )
        self.db.conn.execute(
            """
            INSERT INTO channel_messages
                (id, channel_id, user_id, content, message_type, encrypted_content, crypto_state, key_id, nonce, created_at)
            VALUES
                (?, ?, ?, ?, 'text', ?, 'pending_decrypt', ?, ?, datetime('now'))
            """,
            ('M_PENDING_NEW', channel.id, 'owner-user', '', 'ciphertext-new', 'key-001', 'nonce-new'),
        )
        self.db.conn.commit()

        marked = self.channel_manager.mark_stale_pending_decrypt(max_age_hours=24, limit=100)
        self.assertEqual(marked, 1)

        old_row = self.db.conn.execute(
            "SELECT crypto_state FROM channel_messages WHERE id = ?",
            ('M_PENDING_OLD',),
        ).fetchone()
        new_row = self.db.conn.execute(
            "SELECT crypto_state FROM channel_messages WHERE id = ?",
            ('M_PENDING_NEW',),
        ).fetchone()
        self.assertIsNotNone(old_row)
        self.assertIsNotNone(new_row)
        assert old_row is not None and new_row is not None
        self.assertEqual(old_row['crypto_state'], 'decrypt_failed')
        self.assertEqual(new_row['crypto_state'], 'pending_decrypt')


if __name__ == '__main__':
    unittest.main()
