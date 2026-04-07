"""Regression coverage for quarantined self-registered agent accounts."""

import os
import sqlite3
import sys
import types
import unittest
from unittest.mock import patch

from flask import Flask

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

from canopy.api.routes import create_api_blueprint
from canopy.security.api_keys import Permission


class _FakeDbManager:
    def __init__(self) -> None:
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            """
            CREATE TABLE users (
                id TEXT PRIMARY KEY,
                username TEXT UNIQUE NOT NULL,
                public_key TEXT,
                password_hash TEXT,
                display_name TEXT,
                account_type TEXT DEFAULT 'human',
                status TEXT DEFAULT 'active',
                origin_peer TEXT
            );
            CREATE TABLE user_keys (
                user_id TEXT PRIMARY KEY,
                ed25519_public_key TEXT,
                ed25519_private_key TEXT,
                x25519_public_key TEXT,
                x25519_private_key TEXT
            );
            CREATE TABLE channels (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL
            );
            CREATE TABLE channel_members (
                channel_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                role TEXT DEFAULT 'member',
                UNIQUE(channel_id, user_id)
            );
            CREATE TABLE user_channel_governance (
                user_id TEXT PRIMARY KEY,
                enabled INTEGER DEFAULT 0,
                block_public_channels INTEGER DEFAULT 0,
                restrict_to_allowed_channels INTEGER DEFAULT 0,
                allowed_channel_ids TEXT,
                updated_at TEXT,
                updated_by TEXT
            );
            """
        )
        self.conn.executemany(
            "INSERT INTO channels (id, name) VALUES (?, ?)",
            [
                ('general', 'general'),
                ('agent-start-here', 'agent-start-here'),
            ],
        )
        self.conn.commit()

    def get_connection(self):
        return self.conn

    def get_user_by_username(self, username: str):
        row = self.conn.execute(
            "SELECT * FROM users WHERE username = ?",
            (username,),
        ).fetchone()
        return dict(row) if row else None

    def create_user(
        self,
        user_id: str,
        username: str,
        public_key: str,
        password_hash: str = None,
        display_name: str = None,
        account_type: str = 'human',
        status: str = 'active',
        origin_peer: str = None,
    ):
        try:
            self.conn.execute(
                """
                INSERT INTO users (id, username, public_key, password_hash, display_name, account_type, status, origin_peer)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (user_id, username, public_key, password_hash, display_name, account_type, status, origin_peer),
            )
            self.conn.commit()
            return True
        except Exception:
            return False

    def store_user_keys(
        self,
        user_id: str,
        ed25519_pub: str,
        ed25519_priv: str,
        x25519_pub: str,
        x25519_priv: str,
    ):
        self.conn.execute(
            """
            INSERT OR REPLACE INTO user_keys
            (user_id, ed25519_public_key, ed25519_private_key, x25519_public_key, x25519_private_key)
            VALUES (?, ?, ?, ?, ?)
            """,
            (user_id, ed25519_pub, ed25519_priv, x25519_pub, x25519_priv),
        )
        self.conn.commit()

    def set_user_status(self, user_id: str, status: str) -> bool:
        cursor = self.conn.execute(
            "UPDATE users SET status = ? WHERE id = ?",
            (status, user_id),
        )
        self.conn.commit()
        return bool(cursor.rowcount)


class _FakeApiKeyManager:
    def __init__(self) -> None:
        self.last_permissions: list = []

    def generate_key(self, user_id, permissions):
        self.last_permissions = list(permissions)
        return f"key-for-{user_id}"


class _FakeChannelManager:
    AGENT_START_CHANNEL_ID = 'agent-start-here'

    def __init__(self, db_manager: _FakeDbManager, *, should_quarantine_succeed: bool = True) -> None:
        self.db_manager = db_manager
        self.calls = []
        self.should_quarantine_succeed = should_quarantine_succeed

    def ensure_agent_quarantine_assignment(self, user_id: str, *, updated_by: str = None, role: str = 'member'):
        self.calls.append({'user_id': user_id, 'updated_by': updated_by, 'role': role})
        if not self.should_quarantine_succeed:
            return False
        with self.db_manager.get_connection() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO channel_members (channel_id, user_id, role) VALUES (?, ?, ?)",
                (self.AGENT_START_CHANNEL_ID, user_id, role),
            )
            conn.execute(
                """
                INSERT OR REPLACE INTO user_channel_governance
                (user_id, enabled, block_public_channels, restrict_to_allowed_channels, allowed_channel_ids, updated_at, updated_by)
                VALUES (?, 1, 1, 1, ?, CURRENT_TIMESTAMP, ?)
                """,
                (user_id, '["agent-start-here"]', updated_by or 'system'),
            )
            conn.commit()
        return True


class TestAgentRegistrationQuarantine(unittest.TestCase):
    def setUp(self) -> None:
        self.db_manager = _FakeDbManager()
        self.api_key_manager = _FakeApiKeyManager()
        self.channel_manager = _FakeChannelManager(self.db_manager)

        components = (
            self.db_manager,
            self.api_key_manager,
            None,
            None,
            self.channel_manager,
            None,
            None,
            None,
            None,
            None,
            None,
        )
        self.components_patcher = patch('canopy.api.routes._get_app_components_any', return_value=components)
        self.components_patcher.start()
        self.addCleanup(self.components_patcher.stop)

        app = Flask(__name__)
        app.config['TESTING'] = True
        app.register_blueprint(create_api_blueprint(), url_prefix='/api/v1')
        self.client = app.test_client()

    def tearDown(self) -> None:
        self.db_manager.conn.close()

    def test_agent_register_starts_pending_and_quarantined(self) -> None:
        response = self.client.post(
            '/api/v1/register',
            json={
                'username': 'quarantined-agent',
                'password': 'StrongPass123!',
                'display_name': 'Quarantined Agent',
                'account_type': 'agent',
            },
        )
        self.assertEqual(response.status_code, 201)
        payload = response.get_json() or {}
        self.assertEqual(payload.get('account_type'), 'agent')
        self.assertEqual(payload.get('status'), 'pending_approval')
        self.assertEqual(payload.get('quarantine_channel_id'), 'agent-start-here')
        self.assertIn('agent-start-here', payload.get('message') or '')

        row = self.db_manager.conn.execute(
            "SELECT id FROM users WHERE username = ?",
            ('quarantined-agent',),
        ).fetchone()
        self.assertIsNotNone(row)
        user_id = row['id']

        memberships = self.db_manager.conn.execute(
            "SELECT channel_id FROM channel_members WHERE user_id = ? ORDER BY channel_id",
            (user_id,),
        ).fetchall()
        channel_ids = [m['channel_id'] for m in memberships]
        self.assertEqual(channel_ids, ['agent-start-here'])

        governance = self.db_manager.conn.execute(
            """
            SELECT enabled, block_public_channels, restrict_to_allowed_channels, allowed_channel_ids
            FROM user_channel_governance
            WHERE user_id = ?
            """,
            (user_id,),
        ).fetchone()
        self.assertIsNotNone(governance)
        self.assertEqual(int(governance['enabled'] or 0), 1)
        self.assertEqual(int(governance['block_public_channels'] or 0), 1)
        self.assertEqual(int(governance['restrict_to_allowed_channels'] or 0), 1)
        self.assertEqual(governance['allowed_channel_ids'], '["agent-start-here"]')
        self.assertEqual(
            self.api_key_manager.last_permissions,
            [
                Permission.READ_MESSAGES,
                Permission.WRITE_MESSAGES,
                Permission.READ_FEED,
                Permission.WRITE_FEED,
            ],
        )

    def test_human_api_register_starts_pending(self) -> None:
        """All API-registered accounts start pending_approval regardless of type."""
        response = self.client.post(
            '/api/v1/register',
            json={
                'username': 'human-user',
                'password': 'StrongPass123!',
                'display_name': 'Human User',
                'account_type': 'human',
            },
        )
        self.assertEqual(response.status_code, 201)
        payload = response.get_json() or {}
        self.assertEqual(payload.get('status'), 'pending_approval')
        self.assertIn('account_type="agent"', payload.get('registration_hint') or '')

        row = self.db_manager.conn.execute(
            "SELECT id FROM users WHERE username = ?",
            ('human-user',),
        ).fetchone()
        self.assertIsNotNone(row)
        memberships = self.db_manager.conn.execute(
            "SELECT channel_id FROM channel_members WHERE user_id = ? ORDER BY channel_id",
            (row['id'],),
        ).fetchall()
        channel_ids = [m['channel_id'] for m in memberships]
        self.assertEqual(channel_ids, ['general'])

    def test_agent_api_register_uses_current_meshspace_permission_template(self) -> None:
        self.client.application.config['MESHSPACE_RECORD'] = {
            'meshspace_id': 'private-mesh',
            'default_agent_permissions': [
                Permission.READ_MESSAGES.value,
                Permission.WRITE_MESSAGES.value,
                Permission.READ_FILES.value,
                Permission.WRITE_FILES.value,
            ],
        }
        response = self.client.post(
            '/api/v1/register',
            json={
                'username': 'file-agent',
                'password': 'StrongPass123!',
                'display_name': 'File Agent',
                'account_type': 'agent',
            },
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(
            self.api_key_manager.last_permissions,
            [
                Permission.READ_MESSAGES,
                Permission.WRITE_MESSAGES,
                Permission.READ_FILES,
                Permission.WRITE_FILES,
            ],
        )

    def test_auto_approved_agent_falls_back_to_pending_if_quarantine_bootstrap_fails(self) -> None:
        failing_channel_manager = _FakeChannelManager(self.db_manager, should_quarantine_succeed=False)
        components = (
            self.db_manager,
            self.api_key_manager,
            None,
            None,
            failing_channel_manager,
            None,
            None,
            None,
            None,
            None,
            None,
        )
        with patch('canopy.api.routes._get_app_components_any', return_value=components), patch.dict(os.environ, {'CANOPY_AUTO_APPROVE_AGENTS': '1'}, clear=False):
            response = self.client.post(
                '/api/v1/register',
                json={
                    'username': 'auto-agent',
                    'password': 'StrongPass123!',
                    'display_name': 'Auto Agent',
                    'account_type': 'agent',
                },
            )

        self.assertEqual(response.status_code, 201)
        payload = response.get_json() or {}
        self.assertEqual(payload.get('status'), 'pending_approval')
        self.assertIsNone(payload.get('quarantine_channel_id'))
        row = self.db_manager.conn.execute(
            "SELECT status FROM users WHERE username = ?",
            ('auto-agent',),
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row['status'], 'pending_approval')


if __name__ == '__main__':
    unittest.main()
