"""Tests for admin private-channel member-sync diagnostics endpoint."""

import os
import sqlite3
import sys
import types
import unittest
from unittest.mock import MagicMock, patch

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
    def __init__(self, conn: sqlite3.Connection, owner_user_id: str) -> None:
        self._conn = conn
        self._owner_user_id = owner_user_id

    def get_connection(self) -> sqlite3.Connection:
        return self._conn

    def get_instance_owner_user_id(self):
        return self._owner_user_id

    def get_user(self, user_id: str):
        row = self._conn.execute(
            "SELECT * FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
        return dict(row) if row else None


class TestAdminChannelMemberSyncDiagnostics(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            """
            CREATE TABLE channels (
                id TEXT PRIMARY KEY,
                name TEXT,
                channel_type TEXT,
                description TEXT,
                privacy_mode TEXT,
                origin_peer TEXT,
                created_by TEXT,
                crypto_mode TEXT,
                created_at TEXT
            );

            CREATE TABLE users (
                id TEXT PRIMARY KEY,
                username TEXT,
                display_name TEXT,
                origin_peer TEXT,
                account_type TEXT,
                status TEXT
            );

            CREATE TABLE channel_members (
                channel_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                role TEXT DEFAULT 'member',
                notifications_enabled INTEGER DEFAULT 1,
                joined_at TEXT
            );

            CREATE TABLE channel_member_sync_deliveries (
                sync_id TEXT PRIMARY KEY,
                channel_id TEXT NOT NULL,
                target_user_id TEXT NOT NULL,
                action TEXT NOT NULL,
                role TEXT DEFAULT 'member',
                target_peer_id TEXT NOT NULL,
                payload_json TEXT,
                delivery_state TEXT NOT NULL DEFAULT 'pending',
                last_error TEXT,
                attempt_count INTEGER DEFAULT 0,
                last_attempt_at TEXT,
                acked_at TEXT,
                created_at TEXT,
                updated_at TEXT
            );
            """
        )

        self.conn.execute(
            """
            INSERT INTO channels (
                id, name, channel_type, description, privacy_mode, origin_peer,
                created_by, crypto_mode, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
            """,
            (
                'C_private',
                'private-test',
                'private',
                'diagnostics test channel',
                'private',
                'peer-local',
                'admin-user',
                'legacy_plaintext',
            ),
        )

        self.conn.executemany(
            """
            INSERT INTO users (id, username, display_name, origin_peer, account_type, status)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                ('admin-user', 'admin', 'Admin User', None, 'human', 'active'),
                ('remote-user', 'remote_user', 'Remote User', 'peer-remote-a', 'human', 'active'),
                ('remote-missing', 'remote_missing', 'Remote Missing Origin', '', 'agent', 'active'),
            ],
        )

        self.conn.executemany(
            """
            INSERT INTO channel_members (channel_id, user_id, role, notifications_enabled, joined_at)
            VALUES (?, ?, ?, ?, datetime('now'))
            """,
            [
                ('C_private', 'admin-user', 'admin', 1),
                ('C_private', 'remote-missing', 'member', 1),
            ],
        )

        self.conn.executemany(
            """
            INSERT INTO channel_member_sync_deliveries (
                sync_id, channel_id, target_user_id, action, role, target_peer_id,
                payload_json, delivery_state, last_error, attempt_count,
                last_attempt_at, acked_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), ?, datetime('now'), datetime('now'))
            """,
            [
                (
                    'MS_diag_1',
                    'C_private',
                    'remote-missing',
                    'add',
                    'member',
                    'peer-remote-a',
                    '{"channel_name":"private-test","privacy_mode":"private"}',
                    'failed',
                    'send_failed',
                    2,
                    None,
                ),
                (
                    'MS_diag_2',
                    'C_private',
                    'remote-missing',
                    'add',
                    'member',
                    'peer-remote-a',
                    '{"channel_name":"private-test","privacy_mode":"private"}',
                    'acked',
                    None,
                    1,
                    "2026-03-02T12:00:00Z",
                ),
            ],
        )
        self.conn.commit()

        self.db_manager = _FakeDbManager(self.conn, owner_user_id='admin-user')
        self.channel_manager = MagicMock()
        self.channel_manager.get_member_peer_ids.return_value = {'peer-local', 'peer-remote-a'}
        self.p2p_manager = MagicMock()
        self.p2p_manager.is_running.return_value = True
        self.p2p_manager.get_peer_id.return_value = 'peer-local'
        self.p2p_manager.get_connected_peers.return_value = ['peer-remote-a']

        components = (
            self.db_manager,          # db_manager
            MagicMock(),              # api_key_manager
            MagicMock(),              # trust_manager
            MagicMock(),              # message_manager
            self.channel_manager,     # channel_manager
            MagicMock(),              # file_manager
            MagicMock(),              # feed_manager
            MagicMock(),              # interaction_manager
            MagicMock(),              # profile_manager
            MagicMock(),              # config
            self.p2p_manager,         # p2p_manager
        )

        self.get_components_patcher = patch(
            'canopy.ui.routes.get_app_components',
            return_value=components,
        )
        self.get_components_patcher.start()
        self.addCleanup(self.get_components_patcher.stop)

        app = Flask(__name__)
        app.config['TESTING'] = True
        app.secret_key = 'test-secret'
        app.register_blueprint(create_ui_blueprint())

        self.client = app.test_client()

    def tearDown(self) -> None:
        self.conn.close()

    def _set_authenticated_session(self, user_id: str = 'admin-user') -> None:
        with self.client.session_transaction() as sess:
            sess['authenticated'] = True
            sess['user_id'] = user_id

    def test_member_sync_diagnostics_requires_admin(self) -> None:
        self._set_authenticated_session(user_id='remote-user')
        response = self.client.get(
            '/ajax/admin/channels/member-sync-diagnostics?channel_id=C_private'
        )
        self.assertEqual(response.status_code, 403)

    def test_member_sync_diagnostics_requires_channel_id(self) -> None:
        self._set_authenticated_session()
        response = self.client.get('/ajax/admin/channels/member-sync-diagnostics')
        self.assertEqual(response.status_code, 400)
        payload = response.get_json() or {}
        self.assertIn('channel_id', payload.get('error', ''))

    def test_member_sync_diagnostics_returns_expected_payload(self) -> None:
        self._set_authenticated_session()
        response = self.client.get(
            '/ajax/admin/channels/member-sync-diagnostics?channel_id=C_private&target_user_id=remote-missing&limit=200'
        )
        self.assertEqual(response.status_code, 200)
        payload = response.get_json() or {}
        self.assertTrue(payload.get('success'))

        diagnostics = payload.get('diagnostics') or {}
        self.assertEqual((diagnostics.get('channel') or {}).get('id'), 'C_private')
        self.assertIn('peer-local', diagnostics.get('member_peer_ids') or [])
        self.assertIn('peer-remote-a', diagnostics.get('connected_peers') or [])

        member_sync = diagnostics.get('member_sync') or {}
        self.assertEqual(member_sync.get('total_records'), 2)
        self.assertEqual((member_sync.get('state_counts') or {}).get('failed'), 1)
        self.assertEqual((member_sync.get('state_counts') or {}).get('acked'), 1)
        self.assertEqual(member_sync.get('failed_count'), 1)
        self.assertEqual(member_sync.get('pending_count'), 1)

        target_user = diagnostics.get('target_user') or {}
        self.assertTrue(target_user.get('is_channel_member'))
        self.assertIsNone(target_user.get('origin_peer'))

        warnings = diagnostics.get('warnings') or []
        self.assertTrue(any('no origin_peer' in str(w) for w in warnings))

    def test_member_sync_diagnostics_unknown_channel(self) -> None:
        self._set_authenticated_session()
        response = self.client.get(
            '/ajax/admin/channels/member-sync-diagnostics?channel_id=C_missing'
        )
        self.assertEqual(response.status_code, 404)


if __name__ == '__main__':
    unittest.main()
