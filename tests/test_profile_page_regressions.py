"""Regression tests for profile page CSRF handling and activity stats."""

import os
import sqlite3
import sys
import types
import unittest
from io import BytesIO
from typing import Optional
from unittest.mock import MagicMock, patch

from flask import Flask, jsonify

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
    def __init__(self, conn: sqlite3.Connection, users: Optional[dict] = None) -> None:
        self._conn = conn
        self._users = users or {}

    def get_connection(self) -> sqlite3.Connection:
        return self._conn

    def get_user(self, user_id: str):
        return self._users.get(user_id)


class TestProfilePageRegressions(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row

        self.fake_users = {}
        db_manager = _FakeDbManager(self.conn, self.fake_users)
        self.db_manager = db_manager
        self.profile_manager = MagicMock()
        self.profile_manager.ensure_default_profile.return_value = {
            'username': 'test-user',
            'display_name': 'Test User',
            'bio': '',
            'theme_preference': 'dark',
            'avatar_url': None,
        }
        self.profile_manager.update_profile.return_value = True
        self.profile_manager.update_avatar.return_value = 'file-test-avatar'
        self.profile_manager.get_profile_card.return_value = None

        p2p_manager = MagicMock()
        p2p_manager.is_running.return_value = False
        p2p_manager.get_peer_id.return_value = 'peer-local'

        # Order must match get_app_components in canopy.core.utils.
        components = (
            db_manager,               # db_manager
            MagicMock(),             # api_key_manager
            MagicMock(),             # trust_manager
            MagicMock(),             # message_manager
            MagicMock(),             # channel_manager
            MagicMock(),             # file_manager
            MagicMock(),             # feed_manager
            MagicMock(),             # interaction_manager
            self.profile_manager,    # profile_manager
            MagicMock(),             # config
            p2p_manager,             # p2p_manager
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

        self.app = app
        self.client = app.test_client()

    def tearDown(self) -> None:
        self.conn.close()

    def _set_authenticated_session(self, csrf_token: str = 'csrf-test-token') -> None:
        with self.client.session_transaction() as sess:
            sess['authenticated'] = True
            sess['user_id'] = 'test-user'
            sess['_csrf_token'] = csrf_token

    def _seed_stats_tables(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE channel_messages (id INTEGER PRIMARY KEY, user_id TEXT);
            CREATE TABLE messages (id INTEGER PRIMARY KEY, sender_id TEXT);
            CREATE TABLE channel_members (id INTEGER PRIMARY KEY, user_id TEXT, channel_id TEXT);
            CREATE TABLE feed_posts (id INTEGER PRIMARY KEY, author_id TEXT);
            CREATE TABLE api_keys (id INTEGER PRIMARY KEY, user_id TEXT, revoked INTEGER);
            """
        )

        self.conn.executemany(
            'INSERT INTO channel_messages (user_id) VALUES (?)',
            [('test-user',), ('test-user',), ('other-user',)],
        )
        self.conn.executemany(
            'INSERT INTO messages (sender_id) VALUES (?)',
            [('test-user',), ('test-user',), ('test-user',), ('other-user',)],
        )
        self.conn.executemany(
            'INSERT INTO channel_members (user_id, channel_id) VALUES (?, ?)',
            [
                ('test-user', 'general'),
                ('test-user', 'general'),
                ('test-user', 'private'),
                ('other-user', 'general'),
            ],
        )
        self.conn.executemany(
            'INSERT INTO feed_posts (author_id) VALUES (?)',
            [('test-user',), ('other-user',)],
        )
        self.conn.executemany(
            'INSERT INTO api_keys (user_id, revoked) VALUES (?, ?)',
            [('test-user', 0), ('test-user', 1), ('other-user', 0)],
        )
        self.conn.commit()

    def test_update_profile_requires_csrf(self) -> None:
        self._set_authenticated_session()

        response = self.client.post(
            '/ajax/update_profile',
            json={'display_name': 'A', 'bio': 'B', 'theme_preference': 'dark'},
        )

        self.assertEqual(response.status_code, 403)

    def test_update_profile_succeeds_with_csrf(self) -> None:
        csrf_token = 'csrf-ok'
        self._set_authenticated_session(csrf_token=csrf_token)

        response = self.client.post(
            '/ajax/update_profile',
            json={'display_name': 'A', 'bio': 'B', 'theme_preference': 'dark'},
            headers={'X-CSRFToken': csrf_token},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json() or {}
        self.assertTrue(payload.get('success'))

    def test_upload_avatar_succeeds_with_csrf(self) -> None:
        csrf_token = 'csrf-ok'
        self._set_authenticated_session(csrf_token=csrf_token)

        response = self.client.post(
            '/ajax/upload_avatar',
            data={
                'avatar': (BytesIO(b'\x89PNG\r\n\x1a\nprofile-avatar'), 'avatar.png', 'image/png'),
            },
            content_type='multipart/form-data',
            headers={'X-CSRFToken': csrf_token},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json() or {}
        self.assertTrue(payload.get('success'))
        self.assertEqual(payload.get('avatar_url'), '/files/file-test-avatar')

    def test_profile_stats_use_real_data(self) -> None:
        self._seed_stats_tables()
        self._set_authenticated_session()

        with patch('canopy.ui.routes.render_template') as render_template_mock:
            render_template_mock.side_effect = (
                lambda template_name, **context: jsonify(
                    {'template': template_name, 'profile_stats': context.get('profile_stats', {})}
                )
            )
            response = self.client.get('/profile')

        self.assertEqual(response.status_code, 200)
        payload = response.get_json() or {}
        self.assertEqual(payload.get('template'), 'profile.html')
        self.assertEqual(
            payload.get('profile_stats'),
            {
                'messages': 5,
                'channels': 2,
                'posts': 1,
                'api_keys': 1,
            },
        )

    def test_profile_stats_default_to_zero_without_tables(self) -> None:
        self._set_authenticated_session()

        with patch('canopy.ui.routes.render_template') as render_template_mock:
            render_template_mock.side_effect = (
                lambda template_name, **context: jsonify(
                    {'template': template_name, 'profile_stats': context.get('profile_stats', {})}
                )
            )
            response = self.client.get('/profile')

        self.assertEqual(response.status_code, 200)
        payload = response.get_json() or {}
        self.assertEqual(payload.get('template'), 'profile.html')
        self.assertEqual(
            payload.get('profile_stats'),
            {
                'messages': 0,
                'channels': 0,
                'posts': 0,
                'api_keys': 0,
            },
        )

    def test_get_user_display_info_includes_identity_fields(self) -> None:
        self._set_authenticated_session()
        self.fake_users['agent-1'] = {
            'id': 'agent-1',
            'username': 'agent_one',
            'display_name': 'Agent One',
            'account_type': 'agent',
            'status': 'active',
            'origin_peer': 'peer-alpha',
        }
        self.profile_manager.get_profile.return_value = types.SimpleNamespace(
            display_name='Agent One',
            username='agent_one',
            avatar_url='/files/avatar-agent-1',
            avatar_file_id='avatar-agent-1',
            origin_peer='peer-alpha',
            account_type='agent',
        )

        response = self.client.get('/ajax/get_user_display_info?user_ids=agent-1')

        self.assertEqual(response.status_code, 200)
        payload = response.get_json() or {}
        self.assertTrue(payload.get('success'))
        user = (payload.get('users') or {}).get('agent-1') or {}
        self.assertEqual(user.get('display_name'), 'Agent One')
        self.assertEqual(user.get('username'), 'agent_one')
        self.assertEqual(user.get('avatar_url'), '/files/avatar-agent-1')
        self.assertEqual(user.get('account_type'), 'agent')
        self.assertEqual(user.get('status'), 'active')
        self.assertEqual(user.get('origin_peer'), 'peer-alpha')
        self.assertTrue(user.get('is_remote'))

    def test_get_user_display_info_falls_back_to_db_user(self) -> None:
        self._set_authenticated_session()
        self.db_manager.get_user = MagicMock(
            return_value={
                'id': 'human-1',
                'username': 'human_one',
                'display_name': 'Human One',
                'account_type': 'human',
                'status': 'active',
                'agent_directives': None,
                'origin_peer': '',
            }
        )
        self.profile_manager.get_profile.return_value = None

        response = self.client.get('/ajax/get_user_display_info?user_ids=human-1')

        self.assertEqual(response.status_code, 200)
        payload = response.get_json() or {}
        self.assertTrue(payload.get('success'))
        user = (payload.get('users') or {}).get('human-1') or {}
        self.assertEqual(user.get('display_name'), 'Human One')
        self.assertEqual(user.get('username'), 'human_one')
        self.assertEqual(user.get('avatar_url'), None)
        self.assertEqual(user.get('account_type'), 'human')
        self.assertEqual(user.get('status'), 'active')
        self.assertEqual(user.get('origin_peer'), None)
        self.assertFalse(user.get('is_remote'))
        self.db_manager.get_user.assert_called_once_with('human-1')

    def test_get_user_display_info_infers_agent_from_status(self) -> None:
        self._set_authenticated_session()
        self.db_manager.get_user = MagicMock(
            return_value={
                'id': 'agent-pending',
                'username': 'agent_pending',
                'display_name': 'Pending Agent',
                'account_type': 'human',
                'status': 'pending_approval',
                'agent_directives': None,
                'origin_peer': '',
            }
        )
        self.profile_manager.get_profile.return_value = None

        response = self.client.get('/ajax/get_user_display_info?user_ids=agent-pending')

        self.assertEqual(response.status_code, 200)
        payload = response.get_json() or {}
        self.assertTrue(payload.get('success'))
        user = (payload.get('users') or {}).get('agent-pending') or {}
        self.assertEqual(user.get('account_type'), 'agent')
        self.assertEqual(user.get('status'), 'pending_approval')

    def test_get_user_display_info_infers_agent_from_directives(self) -> None:
        self._set_authenticated_session()
        self.db_manager.get_user = MagicMock(
            return_value={
                'id': 'agent-directive',
                'username': 'agent_directive',
                'display_name': 'Directive Agent',
                'account_type': 'human',
                'status': 'active',
                'agent_directives': 'Always act as an agent runtime.',
                'origin_peer': '',
            }
        )
        self.profile_manager.get_profile.return_value = None

        response = self.client.get('/ajax/get_user_display_info?user_ids=agent-directive')

        self.assertEqual(response.status_code, 200)
        payload = response.get_json() or {}
        self.assertTrue(payload.get('success'))
        user = (payload.get('users') or {}).get('agent-directive') or {}
        self.assertEqual(user.get('account_type'), 'agent')
        self.assertEqual(user.get('status'), 'active')

    def test_get_user_display_info_treats_local_peer_origin_as_local(self) -> None:
        self._set_authenticated_session()
        self.fake_users['local-admin'] = {
            'id': 'local-admin',
            'username': 'maddog',
            'display_name': 'Maddog',
            'account_type': 'human',
            'status': 'active',
            'origin_peer': 'peer-local',
        }
        self.profile_manager.get_profile.return_value = types.SimpleNamespace(
            display_name='Maddog',
            username='maddog',
            avatar_url=None,
            avatar_file_id=None,
            origin_peer='peer-local',
            account_type='human',
        )

        response = self.client.get('/ajax/get_user_display_info?user_ids=local-admin')

        self.assertEqual(response.status_code, 200)
        payload = response.get_json() or {}
        self.assertTrue(payload.get('success'))
        user = (payload.get('users') or {}).get('local-admin') or {}
        self.assertEqual(user.get('origin_peer'), None)
        self.assertFalse(user.get('is_remote'))


if __name__ == '__main__':
    unittest.main()
