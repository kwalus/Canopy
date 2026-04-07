"""Regression test: registration should auto-join only open public channels."""

import os
import sqlite3
import sys
import types
import unittest
from unittest.mock import patch

from flask import Flask

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

from canopy.ui.routes import create_ui_blueprint


class _FakeDbManager:
    def __init__(self) -> None:
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        self.owner_user_id = None
        self.conn.executescript(
            """
            CREATE TABLE users (
                id TEXT PRIMARY KEY,
                username TEXT UNIQUE NOT NULL,
                public_key TEXT,
                password_hash TEXT,
                display_name TEXT,
                account_type TEXT DEFAULT 'human',
                status TEXT DEFAULT 'active'
            );

            CREATE TABLE user_keys (
                user_id TEXT PRIMARY KEY,
                ed25519_pub TEXT,
                ed25519_priv TEXT,
                x25519_pub TEXT,
                x25519_priv TEXT
            );

            CREATE TABLE channels (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                channel_type TEXT NOT NULL,
                created_by TEXT NOT NULL,
                description TEXT,
                privacy_mode TEXT
            );

            CREATE TABLE channel_members (
                channel_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                role TEXT DEFAULT 'member',
                UNIQUE(channel_id, user_id)
            );
            """
        )
        self.conn.executemany(
            """
            INSERT INTO channels (id, name, channel_type, created_by, description, privacy_mode)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                ('general', 'general', 'public', 'system', 'General', 'open'),
                ('Copen', 'open-room', 'public', 'owner', 'Open room', 'open'),
                ('Crestricted', 'restricted-room', 'public', 'owner', 'Restricted room', 'private'),
            ],
        )
        self.conn.commit()

    def get_connection(self):
        return self.conn

    def has_any_registered_users(self):
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM users WHERE COALESCE(password_hash, '') != ''"
        ).fetchone()
        return bool((row['n'] if row else 0) > 0)

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
        password_hash: str,
        display_name: str,
        account_type: str = 'human',
        status: str = 'active',
    ):
        try:
            self.conn.execute(
                """
                INSERT INTO users (id, username, public_key, password_hash, display_name, account_type, status)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (user_id, username, public_key, password_hash, display_name, account_type, status),
            )
            self.conn.commit()
            return True
        except Exception:
            return False

    def store_user_keys(self, user_id: str, ed25519_pub: str, ed25519_priv: str, x25519_pub: str, x25519_priv: str):
        self.conn.execute(
            """
            INSERT OR REPLACE INTO user_keys
            (user_id, ed25519_pub, ed25519_priv, x25519_pub, x25519_priv)
            VALUES (?, ?, ?, ?, ?)
            """,
            (user_id, ed25519_pub, ed25519_priv, x25519_pub, x25519_priv),
        )
        self.conn.commit()

    def get_instance_owner_user_id(self):
        return self.owner_user_id

    def set_instance_owner_user_id(self, user_id: str) -> None:
        self.owner_user_id = user_id


class _FakeChannelManager:
    def ensure_default_channels_exist(self) -> None:
        return None

    def get_agent_quarantine_channel_id(self) -> str:
        return 'agent-start-here'


class TestRegisterChannelMembershipPrivacy(unittest.TestCase):
    def setUp(self) -> None:
        self.db_manager = _FakeDbManager()
        self.channel_manager = _FakeChannelManager()
        components = (
            self.db_manager,
            None,
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
        self.get_components_any_patcher = patch(
            'canopy.ui.routes._get_app_components_any',
            return_value=components,
        )
        self.get_components_patcher = patch(
            'canopy.ui.routes.get_app_components',
            return_value=components,
        )
        self.get_components_any_patcher.start()
        self.get_components_patcher.start()

        app = Flask(__name__)
        app.config['TESTING'] = True
        app.secret_key = 'test-secret'
        app.config['DB_MANAGER'] = self.db_manager
        app.register_blueprint(create_ui_blueprint())
        self.client = app.test_client()

    def tearDown(self) -> None:
        self.get_components_any_patcher.stop()
        self.get_components_patcher.stop()
        self.db_manager.conn.close()

    def test_register_joins_only_open_public_channels(self) -> None:
        response = self.client.post(
            '/register',
            data={
                'username': 'new_user',
                'display_name': 'New User',
                'password': 'StrongPass123!',
                'password_confirm': 'StrongPass123!',
            },
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)

        row = self.db_manager.conn.execute(
            "SELECT id FROM users WHERE username = ?",
            ('new_user',),
        ).fetchone()
        self.assertIsNotNone(row)
        user_id = row['id']

        memberships = self.db_manager.conn.execute(
            "SELECT channel_id FROM channel_members WHERE user_id = ? ORDER BY channel_id",
            (user_id,),
        ).fetchall()
        channel_ids = [m['channel_id'] for m in memberships]

        self.assertIn('general', channel_ids)
        self.assertIn('Copen', channel_ids)
        self.assertNotIn('Crestricted', channel_ids)

    def test_setup_creates_first_user_and_redirects_without_500(self) -> None:
        response = self.client.post(
            '/setup',
            data={
                'username': 'owner',
                'display_name': 'Owner',
                'password': 'StrongPass123!',
                'password_confirm': 'StrongPass123!',
            },
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)

        row = self.db_manager.conn.execute(
            "SELECT id FROM users WHERE username = ?",
            ('owner',),
        ).fetchone()
        self.assertIsNotNone(row)
        user_id = row['id']

        self.assertEqual(self.db_manager.get_instance_owner_user_id(), user_id)

        with self.client.session_transaction() as sess:
            self.assertTrue(sess.get('authenticated'))
            self.assertEqual(sess.get('user_id'), user_id)
            self.assertEqual(sess.get('username'), 'owner')

    def test_register_second_human_starts_pending_without_login(self) -> None:
        self.db_manager.create_user(
            user_id='owner',
            username='owner',
            public_key='pub-owner',
            password_hash='hash-owner',
            display_name='Owner',
            status='active',
        )
        self.db_manager.set_instance_owner_user_id('owner')

        response = self.client.post(
            '/register',
            data={
                'username': 'new_user',
                'display_name': 'New User',
                'password': 'StrongPass123!',
                'password_confirm': 'StrongPass123!',
            },
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn('awaiting admin approval', response.get_data(as_text=True))

        row = self.db_manager.conn.execute(
            "SELECT id, status FROM users WHERE username = ?",
            ('new_user',),
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row['status'], 'pending_approval')

        with self.client.session_transaction() as sess:
            self.assertFalse(bool(sess.get('authenticated')))

        login_response = self.client.post(
            '/login',
            data={
                'username': 'new_user',
                'password': 'StrongPass123!',
            },
            follow_redirects=False,
        )
        self.assertEqual(login_response.status_code, 200)
        login_html = login_response.get_data(as_text=True)
        self.assertIn('Account awaiting approval', login_html)
        self.assertIn('No action is required from you right now.', login_html)
        self.assertIn('alert-pending', login_html)
        self.assertNotIn('alert-danger mb-3">Your account is pending admin approval.', login_html)


if __name__ == '__main__':
    unittest.main()
