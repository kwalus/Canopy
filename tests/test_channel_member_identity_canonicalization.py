"""Regression tests for canonicalizing duplicate remote member identities."""

import os
import sqlite3
import sys
import types
import unittest
from unittest.mock import MagicMock, patch

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

from canopy.ui.routes import create_ui_blueprint


class _FakeDbManager:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def get_connection(self) -> sqlite3.Connection:
        return self._conn

    def get_user(self, user_id: str):
        row = self._conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return dict(row) if row else None


class TestChannelMemberIdentityCanonicalization(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            """
            CREATE TABLE users (
                id TEXT PRIMARY KEY,
                username TEXT,
                display_name TEXT,
                avatar_file_id TEXT,
                profile_updated_at TEXT,
                created_at TEXT,
                account_type TEXT,
                status TEXT,
                agent_directives TEXT,
                origin_peer TEXT
            );
            CREATE TABLE channel_members (
                channel_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                role TEXT
            );
            """
        )
        self.conn.executemany(
            "INSERT INTO users (id, username, display_name, avatar_file_id, profile_updated_at, created_at, account_type, status, agent_directives, origin_peer) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                ('local-admin', 'local_admin', 'Local Admin', None, None, '2026-03-29 08:00:00', 'human', 'active', None, ''),
                ('windy-stale', 'Windy_MacCursor.826sN3', 'Windy MacCursor', 'avatar-stale', '2026-03-29T14:28:23+00:00', '2026-03-29 14:04:05', 'human', 'active', None, 'peer-123456'),
                ('windy-current', 'Windy_MacCursor-74506f', 'Windy MacCursor', 'avatar-current', '2026-03-29T15:50:44+00:00', '2026-03-29 14:28:24', 'human', 'active', None, 'peer-123456'),
            ],
        )
        self.conn.execute(
            "INSERT INTO channel_members (channel_id, user_id, role) VALUES (?, ?, ?)",
            ('chan-1', 'windy-stale', 'member'),
        )
        self.conn.commit()

        db_manager = _FakeDbManager(self.conn)
        self.channel_manager = MagicMock()
        self.channel_manager.add_member.return_value = True

        components = (
            db_manager,
            MagicMock(),
            MagicMock(),
            MagicMock(),
            self.channel_manager,
            MagicMock(),
            MagicMock(),
            MagicMock(),
            MagicMock(),
            MagicMock(),
            None,
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

        with self.client.session_transaction() as sess:
            sess['authenticated'] = True
            sess['user_id'] = 'local-admin'
            sess['_csrf_token'] = 'csrf-test-token'

    def tearDown(self) -> None:
        self.conn.close()

    def test_add_channel_member_uses_freshest_duplicate_remote_identity(self) -> None:
        response = self.client.post(
            '/ajax/channel_members/chan-1',
            json={'user_id': 'windy-stale', 'role': 'member'},
            headers={'X-CSRFToken': 'csrf-test-token'},
        )
        self.assertEqual(response.status_code, 200)
        self.channel_manager.add_member.assert_called_once_with(
            'chan-1',
            'windy-current',
            'local-admin',
            'member',
        )
        stale_row = self.conn.execute(
            "SELECT 1 FROM channel_members WHERE channel_id = ? AND user_id = ?",
            ('chan-1', 'windy-stale'),
        ).fetchone()
        self.assertIsNone(stale_row)

    def test_add_channel_member_removes_stale_duplicate_when_canonical_is_selected(self) -> None:
        self.channel_manager.add_member.reset_mock()
        response = self.client.post(
            '/ajax/channel_members/chan-1',
            json={'user_id': 'windy-current', 'role': 'member'},
            headers={'X-CSRFToken': 'csrf-test-token'},
        )
        self.assertEqual(response.status_code, 200)
        self.channel_manager.add_member.assert_called_once_with(
            'chan-1',
            'windy-current',
            'local-admin',
            'member',
        )
        stale_row = self.conn.execute(
            "SELECT 1 FROM channel_members WHERE channel_id = ? AND user_id = ?",
            ('chan-1', 'windy-stale'),
        ).fetchone()
        self.assertIsNone(stale_row)


if __name__ == '__main__':
    unittest.main()
