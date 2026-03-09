"""Regression tests for mention suggestions account_type payloads."""

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
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def get_connection(self) -> sqlite3.Connection:
        return self._conn


class _FakeProfileManager:
    def __init__(self) -> None:
        self._all = {
            'human-1': {
                'username': 'human_user',
                'display_name': 'Human User',
                'avatar_url': None,
            },
            'agent-1': {
                'username': 'agent_user',
                'display_name': 'Agent User',
                'avatar_url': None,
            },
        }

    def get_profile(self, user_id: str):
        return None

    def get_all_users_display_info(self):
        return dict(self._all)


class TestMentionSuggestionsAccountType(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            """
            CREATE TABLE users (
                id TEXT PRIMARY KEY,
                username TEXT,
                display_name TEXT,
                account_type TEXT,
                status TEXT,
                agent_directives TEXT,
                origin_peer TEXT
            );
            CREATE TABLE channel_members (
                channel_id TEXT NOT NULL,
                user_id TEXT NOT NULL
            );
            """
        )
        self.conn.executemany(
            "INSERT INTO users (id, username, display_name, account_type, status, agent_directives, origin_peer) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                ('human-1', 'human_user', 'Human User', 'human', 'active', None, ''),
                # Legacy misclassified row: should still resolve to agent.
                ('agent-1', 'agent_user', 'Agent User', 'human', 'pending_approval', None, ''),
                ('system', 'System', 'System', 'human', 'active', None, ''),
                ('local_user', 'Local User', 'Local User', 'human', 'active', None, ''),
            ],
        )
        self.conn.executemany(
            "INSERT INTO channel_members (channel_id, user_id) VALUES (?, ?)",
            [
                ('general', 'human-1'),
                ('general', 'agent-1'),
            ],
        )
        self.conn.commit()

        db_manager = _FakeDbManager(self.conn)
        channel_manager = MagicMock()
        channel_manager.get_channel_members_list.return_value = [
            {
                'user_id': 'human-1',
                'username': 'human_user',
                'display_name': 'Human User',
            },
            {
                'user_id': 'agent-1',
                'username': 'agent_user',
                'display_name': 'Agent User',
            },
        ]

        self.profile_manager = _FakeProfileManager()

        # Order must match get_app_components in canopy.core.utils.
        components = (
            db_manager,              # db_manager
            MagicMock(),            # api_key_manager
            MagicMock(),            # trust_manager
            MagicMock(),            # message_manager
            channel_manager,        # channel_manager
            MagicMock(),            # file_manager
            MagicMock(),            # feed_manager
            MagicMock(),            # interaction_manager
            self.profile_manager,   # profile_manager
            MagicMock(),            # config
            MagicMock(),            # p2p_manager
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

    def _set_authenticated_session(self) -> None:
        with self.client.session_transaction() as sess:
            sess['authenticated'] = True
            sess['user_id'] = 'human-1'
            sess['_csrf_token'] = 'csrf-test-token'

    def test_global_suggestions_include_account_type(self) -> None:
        self._set_authenticated_session()
        response = self.client.get('/ajax/mention_suggestions')
        self.assertEqual(response.status_code, 200)
        payload = response.get_json() or {}
        users = payload.get('users') or []
        by_id = {u.get('user_id'): u for u in users}
        self.assertEqual(by_id.get('human-1', {}).get('account_type'), 'human')
        self.assertEqual(by_id.get('agent-1', {}).get('account_type'), 'agent')
        self.assertIn('presence_state', by_id.get('agent-1', {}))
        self.assertIn('presence_label', by_id.get('agent-1', {}))

    def test_channel_scoped_suggestions_include_account_type(self) -> None:
        self._set_authenticated_session()
        response = self.client.get('/ajax/mention_suggestions?channel_id=general')
        self.assertEqual(response.status_code, 200)
        payload = response.get_json() or {}
        users = payload.get('users') or []
        by_id = {u.get('user_id'): u for u in users}
        self.assertEqual(by_id.get('human-1', {}).get('account_type'), 'human')
        self.assertEqual(by_id.get('agent-1', {}).get('account_type'), 'agent')
        self.assertIn('presence_state', by_id.get('agent-1', {}))
        self.assertIn('presence_label', by_id.get('agent-1', {}))

    def test_global_suggestions_support_query_and_limit_for_large_directories(self) -> None:
        for idx in range(260):
            user_id = f'agent-{idx:03d}'
            username = f'agent_user_{idx:03d}'
            display_name = f'Agent {idx:03d}'
            self.profile_manager._all[user_id] = {
                'username': username,
                'display_name': display_name,
                'avatar_url': None,
            }
            self.conn.execute(
                "INSERT OR REPLACE INTO users (id, username, display_name, account_type, status, agent_directives, origin_peer) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (user_id, username, display_name, 'agent', 'active', None, ''),
            )
        self.conn.commit()

        self._set_authenticated_session()
        response = self.client.get('/ajax/mention_suggestions?q=agent_user_259&limit=20')
        self.assertEqual(response.status_code, 200)
        payload = response.get_json() or {}
        users = payload.get('users') or []
        self.assertLessEqual(len(users), 20)
        user_ids = {u.get('user_id') for u in users}
        self.assertIn('agent-259', user_ids)

    def test_global_suggestions_handle_older_user_schema_without_name_columns(self) -> None:
        legacy_conn = sqlite3.connect(':memory:')
        legacy_conn.row_factory = sqlite3.Row
        self.addCleanup(legacy_conn.close)
        legacy_conn.executescript(
            """
            CREATE TABLE users (
                id TEXT PRIMARY KEY,
                account_type TEXT,
                status TEXT,
                agent_directives TEXT,
                origin_peer TEXT
            );
            """
        )
        legacy_conn.executemany(
            "INSERT INTO users (id, account_type, status, agent_directives, origin_peer) VALUES (?, ?, ?, ?, ?)",
            [
                ('agent-legacy', 'agent', 'active', None, ''),
                ('human-legacy', 'human', 'active', None, ''),
            ],
        )
        legacy_conn.commit()

        legacy_components = (
            _FakeDbManager(legacy_conn),
            MagicMock(),
            MagicMock(),
            MagicMock(),
            MagicMock(),
            MagicMock(),
            MagicMock(),
            MagicMock(),
            _FakeProfileManager(),
            MagicMock(),
            MagicMock(),
        )

        app = Flask(__name__)
        app.config['TESTING'] = True
        app.secret_key = 'legacy-schema-secret'
        with patch('canopy.ui.routes.get_app_components', return_value=legacy_components):
            app.register_blueprint(create_ui_blueprint())
            client = app.test_client()
            with client.session_transaction() as sess:
                sess['authenticated'] = True
                sess['user_id'] = 'agent-legacy'

            response = client.get('/ajax/mention_suggestions')
            self.assertEqual(response.status_code, 200)
            payload = response.get_json() or {}
            user_ids = {u.get('user_id') for u in (payload.get('users') or [])}
            self.assertIn('agent-legacy', user_ids)

            response = client.get('/ajax/mention_suggestions?q=human')
            self.assertEqual(response.status_code, 200)
            payload = response.get_json() or {}
            user_ids = {u.get('user_id') for u in (payload.get('users') or [])}
            self.assertIn('human-legacy', user_ids)


if __name__ == '__main__':
    unittest.main()
