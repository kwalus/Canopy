"""Regression tests for API key generation defaults and legacy permission aliases."""

import json
import os
import sqlite3
import sys
import types
import unittest
import hashlib
from datetime import datetime, timezone

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

from canopy.security.api_keys import ApiKeyManager, Permission


class _FakeDbManager:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.config = types.SimpleNamespace(
            meshspace=types.SimpleNamespace(meshspace_id='test-meshspace')
        )

    def get_connection(self) -> sqlite3.Connection:
        return self.conn

    def get_user(self, user_id: str):
        row = self.conn.execute(
            "SELECT id, status FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
        return dict(row) if row else None


class TestApiKeyGenerationDefaults(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            """
            CREATE TABLE users (
                id TEXT PRIMARY KEY,
                status TEXT
            );

            CREATE TABLE api_keys (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                key_hash TEXT NOT NULL UNIQUE,
                permissions TEXT NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                expires_at TEXT,
                revoked BOOLEAN DEFAULT 0,
                FOREIGN KEY (user_id) REFERENCES users(id)
            );
            """
        )
        self.conn.execute(
            "INSERT INTO users (id, status) VALUES (?, ?)",
            ('user-test', 'active'),
        )
        self.conn.commit()
        self.manager = ApiKeyManager(_FakeDbManager(self.conn))

    def tearDown(self) -> None:
        self.conn.close()

    def test_generate_key_without_permissions_applies_defaults(self) -> None:
        raw_key = self.manager.generate_key('user-test', [])
        self.assertTrue(raw_key)

        row = self.conn.execute(
            "SELECT permissions FROM api_keys WHERE user_id = ? ORDER BY created_at DESC LIMIT 1",
            ('user-test',),
        ).fetchone()
        self.assertIsNotNone(row)

        granted = set(json.loads(row['permissions']))
        expected = {p.value for p in ApiKeyManager.get_default_permissions()}
        self.assertEqual(granted, expected)

    def test_legacy_message_permissions_satisfy_feed_permissions(self) -> None:
        raw_key = 'legacy-compat-key'
        self.conn.execute(
            """
            INSERT INTO api_keys (id, user_id, key_hash, permissions, created_at, revoked)
            VALUES (?, ?, ?, ?, ?, 0)
            """,
            (
                'legacy-key-id',
                'user-test',
                hashlib.sha256(raw_key.encode()).hexdigest(),
                json.dumps([Permission.READ_MESSAGES.value, Permission.WRITE_MESSAGES.value]),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        self.conn.commit()

        self.assertIsNotNone(self.manager.validate_key(raw_key, Permission.READ_FEED))
        self.assertIsNotNone(self.manager.validate_key(raw_key, Permission.WRITE_FEED))

    def test_generate_key_populates_mesh_id_when_schema_requires_it(self) -> None:
        conn = sqlite3.connect(':memory:')
        conn.row_factory = sqlite3.Row
        try:
            conn.executescript(
                """
                CREATE TABLE users (
                    id TEXT PRIMARY KEY,
                    status TEXT
                );

                CREATE TABLE api_keys (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    key_hash TEXT NOT NULL UNIQUE,
                    permissions TEXT NOT NULL,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    expires_at TEXT,
                    revoked BOOLEAN DEFAULT 0,
                    mesh_id TEXT NOT NULL,
                    FOREIGN KEY (user_id) REFERENCES users(id)
                );
                """
            )
            conn.execute(
                "INSERT INTO users (id, status) VALUES (?, ?)",
                ('user-test', 'active'),
            )
            conn.commit()
            manager = ApiKeyManager(_FakeDbManager(conn))

            raw_key = manager.generate_key('user-test', [Permission.READ_MESSAGES])
            self.assertTrue(raw_key)

            row = conn.execute(
                "SELECT mesh_id FROM api_keys WHERE user_id = ?",
                ('user-test',),
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row['mesh_id'], 'test-meshspace')
        finally:
            conn.close()

    def test_generate_key_can_resolve_mesh_id_from_environment(self) -> None:
        original_mesh_id = os.environ.get('CANOPY_MESHSPACE_ID')
        os.environ['CANOPY_MESHSPACE_ID'] = 'env-meshspace'
        conn = sqlite3.connect(':memory:')
        conn.row_factory = sqlite3.Row
        try:
            conn.executescript(
                """
                CREATE TABLE users (
                    id TEXT PRIMARY KEY,
                    status TEXT
                );

                CREATE TABLE api_keys (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    key_hash TEXT NOT NULL UNIQUE,
                    permissions TEXT NOT NULL,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    expires_at TEXT,
                    revoked BOOLEAN DEFAULT 0,
                    mesh_id TEXT NOT NULL,
                    FOREIGN KEY (user_id) REFERENCES users(id)
                );
                """
            )
            conn.execute(
                "INSERT INTO users (id, status) VALUES (?, ?)",
                ('user-test', 'active'),
            )
            conn.commit()
            db = _FakeDbManager(conn)
            db.config = None
            manager = ApiKeyManager(db)

            raw_key = manager.generate_key('user-test', [Permission.READ_MESSAGES])
            self.assertTrue(raw_key)

            row = conn.execute(
                "SELECT mesh_id FROM api_keys WHERE user_id = ?",
                ('user-test',),
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row['mesh_id'], 'env-meshspace')
        finally:
            conn.close()
            if original_mesh_id is None:
                os.environ.pop('CANOPY_MESHSPACE_ID', None)
            else:
                os.environ['CANOPY_MESHSPACE_ID'] = original_mesh_id


if __name__ == '__main__':
    unittest.main()
