"""Regression coverage for sidebar recent-DM contact rail."""

import json
import os
import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path
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

from canopy.core.messaging import MessageManager
from canopy.ui.routes import create_ui_blueprint


class _FakeDbManager:
    def __init__(self, conn: sqlite3.Connection, db_path: Path) -> None:
        self._conn = conn
        self.db_path = db_path

    def get_connection(self) -> sqlite3.Connection:
        return self._conn

    def get_user(self, user_id: str):
        row = self._conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return dict(row) if row else None

    def get_instance_owner_user_id(self):
        return 'owner'

    def get_pending_approval_count(self):
        return 0


class _FakeConnection:
    def __init__(self) -> None:
        self.connected_at = 1700000000
        self.last_activity = 1700000010
        self.last_inbound_activity = 1700000012
        self.last_outbound_activity = 1700000014


class _FakeConnectionManager:
    def __init__(self, connected_peer_ids):
        self._connected_peer_ids = list(connected_peer_ids or [])
        self._connections = {peer_id: _FakeConnection() for peer_id in self._connected_peer_ids}

    def get_connected_peers(self):
        return list(self._connected_peer_ids)

    def get_connection(self, peer_id):
        return self._connections.get(peer_id)


class _FakeP2PManager:
    def __init__(self) -> None:
        self.connection_manager = _FakeConnectionManager(['peer-alpha'])

    def is_running(self) -> bool:
        return True

    def get_connected_peers(self):
        return ['peer-alpha']

    def get_peer_id(self):
        return 'peer-local'

    def get_activity_events(self, since=None, limit=50):
        return []


class TestSidebarRecentDmContacts(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)

        db_path = Path(self.tempdir.name) / 'sidebar_recent_dms.db'
        self.conn = sqlite3.connect(str(db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            """
            CREATE TABLE users (
                id TEXT PRIMARY KEY,
                username TEXT,
                display_name TEXT,
                avatar_file_id TEXT,
                account_type TEXT,
                status TEXT,
                origin_peer TEXT,
                agent_directives TEXT,
                created_at TEXT
            );
            CREATE TABLE messages (
                id TEXT PRIMARY KEY,
                sender_id TEXT NOT NULL,
                recipient_id TEXT,
                content TEXT,
                message_type TEXT,
                status TEXT,
                created_at TEXT,
                delivered_at TEXT,
                read_at TEXT,
                edited_at TEXT,
                metadata TEXT
            );
            """
        )
        self.conn.executemany(
            "INSERT INTO users (id, username, display_name, avatar_file_id, account_type, status, origin_peer, agent_directives, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                ('owner', 'owner', 'Owner', None, 'human', 'active', None, None, '2026-03-08T08:00:00+00:00'),
                ('peer-a', 'peer_a', 'Alice', None, 'human', 'active', 'peer-alpha', None, '2026-03-08T08:01:00+00:00'),
                ('peer-b', 'peer_b', 'Bob', None, 'agent', 'active', 'peer-beta', None, '2026-03-08T08:02:00+00:00'),
            ],
        )
        self.conn.executemany(
            """
            INSERT INTO messages (
                id, sender_id, recipient_id, content, message_type, status,
                created_at, delivered_at, read_at, edited_at, metadata
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    'DM-a-unread', 'peer-a', 'owner', 'Latest from Alice', 'text', 'delivered',
                    '2026-03-08T10:10:00+00:00', '2026-03-08T10:10:02+00:00', None, None, None,
                ),
                (
                    'DM-b-read', 'owner', 'peer-b', 'Update for Bob', 'text', 'delivered',
                    '2026-03-08T10:05:00+00:00', '2026-03-08T10:05:01+00:00', '2026-03-08T10:05:02+00:00', None, None,
                ),
                (
                    'DM-group-ignore', 'peer-a', 'group:abc123', 'Ignore group', 'text', 'delivered',
                    '2026-03-08T10:12:00+00:00', '2026-03-08T10:12:01+00:00', None, None,
                    json.dumps({'group_id': 'group:abc123', 'group_members': ['owner', 'peer-a', 'peer-b']}),
                ),
            ],
        )
        self.conn.commit()

        self.db_manager = _FakeDbManager(self.conn, db_path)
        self.message_manager = MessageManager(self.db_manager, MagicMock())
        self.profile_manager = MagicMock()
        self.profile_manager.get_profile.return_value = None
        self.channel_manager = MagicMock()
        self.channel_manager.get_peer_device_profiles.return_value = {}
        self.channel_manager.get_all_peer_device_profiles.return_value = {}
        self.p2p_manager = _FakeP2PManager()
        self.trust_manager = MagicMock()
        self.trust_manager.get_trust_score.return_value = 90

        components = (
            self.db_manager,
            MagicMock(),
            self.trust_manager,
            self.message_manager,
            self.channel_manager,
            MagicMock(),
            MagicMock(),
            MagicMock(),
            self.profile_manager,
            MagicMock(),
            self.p2p_manager,
        )

        self.get_components_patcher = patch('canopy.ui.routes.get_app_components', return_value=components)
        self.get_components_any_patcher = patch('canopy.ui.routes._get_app_components_any', return_value=components)
        self.get_components_patcher.start()
        self.get_components_any_patcher.start()
        self.addCleanup(self.get_components_patcher.stop)
        self.addCleanup(self.get_components_any_patcher.stop)

        app = Flask(__name__)
        app.config['TESTING'] = True
        app.secret_key = 'sidebar-dm-secret'
        app.register_blueprint(create_ui_blueprint())
        self.app = app
        self.client = app.test_client()
        self._set_authenticated_session()

    def tearDown(self) -> None:
        self.conn.close()

    def _set_authenticated_session(self) -> None:
        with self.client.session_transaction() as sess:
            sess['authenticated'] = True
            sess['user_id'] = 'owner'
            sess['username'] = 'owner'
            sess['display_name'] = 'Owner'
            sess['_csrf_token'] = 'csrf-sidebar'

    def test_messages_page_renders_recent_dm_sidebar_contacts(self) -> None:
        response = self.client.get('/messages')
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)

        self.assertIn('Recent DMs', body)
        self.assertIn('Alice', body)
        self.assertIn('Bob', body)
        self.assertIn('data-dm-user-id="peer-a"', body)
        self.assertIn('data-dm-user-id="peer-b"', body)
        self.assertIn('/messages?with=peer-a#message-DM-a-unread', body)
        self.assertNotIn('data-dm-user-id="group:', body)

    def test_peer_activity_includes_recent_dm_contacts(self) -> None:
        response = self.client.get('/ajax/peer_activity')
        self.assertEqual(response.status_code, 200)
        payload = response.get_json() or {}

        self.assertTrue(payload.get('success'))
        contacts = payload.get('recent_dm_contacts') or []
        self.assertEqual([contact.get('user_id') for contact in contacts[:2]], ['peer-a', 'peer-b'])
        self.assertEqual(contacts[0].get('unread_count'), 1)
        self.assertEqual(contacts[0].get('target_message_id'), 'DM-a-unread')
        self.assertEqual(contacts[0].get('status_state'), 'online')

    def test_peer_activity_delta_request_omits_recent_dm_contacts_when_unchanged(self) -> None:
        first = self.client.get('/ajax/peer_activity')
        self.assertEqual(first.status_code, 200)
        first_payload = first.get_json() or {}

        response = self.client.get(
            f"/ajax/peer_activity?peer_rev={first_payload.get('peer_rev')}&dm_rev={first_payload.get('dm_rev')}"
        )
        self.assertEqual(response.status_code, 200)
        payload = response.get_json() or {}

        self.assertTrue(payload.get('success'))
        self.assertFalse(payload.get('dm_changed'))
        self.assertEqual(payload.get('recent_dm_contacts'), [])


if __name__ == '__main__':
    unittest.main()
