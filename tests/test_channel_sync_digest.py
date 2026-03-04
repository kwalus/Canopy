"""Regression tests for channel sync digest helpers."""

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


class TestChannelSyncDigest(unittest.TestCase):
    def setUp(self) -> None:
        self.db = _FakeDbManager()
        self.channel_manager = ChannelManager(self.db, MagicMock())

    def tearDown(self) -> None:
        self.db.conn.close()

    def _create_channel(self) -> str:
        channel = self.channel_manager.create_channel(
            name='digest-test',
            channel_type=ChannelType.PRIVATE,
            created_by='owner-user',
            description='digest checks',
            privacy_mode='private',
        )
        self.assertIsNotNone(channel)
        assert channel is not None
        return channel.id

    def test_schema_contains_channel_sync_digest_table(self) -> None:
        row = self.db.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='channel_sync_digests'"
        ).fetchone()
        self.assertIsNotNone(row)

    def test_digest_ignores_local_attachment_ids_and_urls(self) -> None:
        channel_id = self._create_channel()
        self.db.conn.execute(
            """
            INSERT INTO channel_messages
                (id, channel_id, user_id, content, message_type, attachments, created_at, expires_at)
            VALUES
                (?, ?, ?, ?, 'file', ?, datetime('now'), datetime('now', '+7 days'))
            """,
            (
                'MATT001',
                channel_id,
                'owner-user',
                'attachment message',
                '[{"id":"FLOCAL1","name":"doc.txt","type":"text/plain","size":12,"url":"/files/FLOCAL1"}]',
            ),
        )
        self.db.conn.commit()

        first = self.channel_manager.compute_channel_sync_digest(channel_id)
        self.assertTrue(first['root'])

        self.db.conn.execute(
            """
            UPDATE channel_messages
            SET attachments = ?
            WHERE id = ?
            """,
            ('[{"id":"FLOCAL2","name":"doc.txt","type":"text/plain","size":12,"url":"/files/FLOCAL2"}]', 'MATT001'),
        )
        self.db.conn.commit()

        second = self.channel_manager.compute_channel_sync_digest(channel_id)
        self.assertEqual(first['root'], second['root'])

        self.db.conn.execute(
            "UPDATE channel_messages SET attachments = ? WHERE id = ?",
            ('[{"id":"FLOCAL3","name":"doc.txt","type":"text/plain","size":13}]', 'MATT001'),
        )
        self.db.conn.commit()
        third = self.channel_manager.compute_channel_sync_digest(channel_id)
        self.assertNotEqual(second['root'], third['root'])

    def test_digest_prefers_encrypted_payload_when_present(self) -> None:
        channel_id = self._create_channel()
        self.db.conn.execute(
            """
            INSERT INTO channel_messages
                (id, channel_id, user_id, content, message_type,
                 encrypted_content, crypto_state, key_id, nonce, created_at, expires_at)
            VALUES
                (?, ?, ?, ?, 'text', ?, 'encrypted', ?, ?, datetime('now'), datetime('now', '+7 days'))
            """,
            (
                'MENC001',
                channel_id,
                'owner-user',
                'plaintext-a',
                'cipher-a',
                'key-a',
                'nonce-a',
            ),
        )
        self.db.conn.commit()

        first = self.channel_manager.compute_channel_sync_digest(channel_id)

        # Content-only change should not affect digest when encrypted payload exists.
        self.db.conn.execute(
            "UPDATE channel_messages SET content = ? WHERE id = ?",
            ('plaintext-b', 'MENC001'),
        )
        self.db.conn.commit()
        second = self.channel_manager.compute_channel_sync_digest(channel_id)
        self.assertEqual(first['root'], second['root'])

        self.db.conn.execute(
            "UPDATE channel_messages SET encrypted_content = ? WHERE id = ?",
            ('cipher-b', 'MENC001'),
        )
        self.db.conn.commit()
        third = self.channel_manager.compute_channel_sync_digest(channel_id)
        self.assertNotEqual(second['root'], third['root'])


if __name__ == '__main__':
    unittest.main()
