"""Regression tests for channel-delete propagation and origin enforcement in UI route."""

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
        self._owner_user_id = 'owner-user'

    def get_connection(self) -> sqlite3.Connection:
        return self._conn

    def get_instance_owner_user_id(self) -> str:
        return self._owner_user_id


class TestChannelDeletePropagation(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            """
            CREATE TABLE channels (
                id TEXT PRIMARY KEY,
                origin_peer TEXT,
                privacy_mode TEXT
            );
            """
        )

        db_manager = _FakeDbManager(self.conn)
        self.channel_manager = MagicMock()
        self.channel_manager.delete_channel.return_value = True
        self.channel_manager.get_member_peer_ids.return_value = set()

        self.p2p_manager = MagicMock()
        self.p2p_manager.get_peer_id.return_value = 'peer-local'
        self.p2p_manager.is_running.return_value = True

        components = (
            db_manager,              # db_manager
            MagicMock(),             # api_key_manager
            MagicMock(),             # trust_manager
            MagicMock(),             # message_manager
            self.channel_manager,    # channel_manager
            MagicMock(),             # file_manager
            MagicMock(),             # feed_manager
            MagicMock(),             # interaction_manager
            MagicMock(),             # profile_manager
            MagicMock(),             # config
            self.p2p_manager,        # p2p_manager
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

    def _set_authenticated_session(self, csrf_token: str = 'csrf-test-token') -> None:
        with self.client.session_transaction() as sess:
            sess['authenticated'] = True
            sess['user_id'] = 'test-user'
            sess['_csrf_token'] = csrf_token

    def _upsert_channel(self, channel_id: str, origin_peer, privacy_mode: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO channels (id, origin_peer, privacy_mode) VALUES (?, ?, ?)",
            (channel_id, origin_peer, privacy_mode),
        )
        self.conn.commit()

    def test_delete_channel_rejects_remote_origin_when_delete_not_authorized(self) -> None:
        self._upsert_channel('Cremote', 'peer-remote', 'open')
        self.channel_manager.delete_channel.return_value = False
        token = 'csrf-remote'
        self._set_authenticated_session(csrf_token=token)

        response = self.client.post(
            '/ajax/delete_channel',
            json={'channel_id': 'Cremote'},
            headers={'X-CSRFToken': token},
        )

        self.assertEqual(response.status_code, 403)
        payload = response.get_json() or {}
        self.assertIn('delete', payload.get('error', '').lower())
        self.channel_manager.delete_channel.assert_called_once_with('Cremote', 'test-user', force=False)
        self.p2p_manager.broadcast_delete_signal.assert_not_called()

    def test_delete_channel_allows_local_only_cleanup_for_remote_origin(self) -> None:
        self._upsert_channel('Cremote', 'peer-remote', 'open')
        token = 'csrf-remote-ok'
        self._set_authenticated_session(csrf_token=token)

        response = self.client.post(
            '/ajax/delete_channel',
            json={'channel_id': 'Cremote'},
            headers={'X-CSRFToken': token},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json() or {}
        self.assertTrue(payload.get('success'))
        self.assertTrue(payload.get('local_only'))
        self.channel_manager.delete_channel.assert_called_once_with('Cremote', 'test-user', force=False)
        self.p2p_manager.broadcast_delete_signal.assert_not_called()

    def test_delete_channel_broadcasts_open_channel_delete_signal(self) -> None:
        self._upsert_channel('Copen', 'peer-local', 'open')
        token = 'csrf-open'
        self._set_authenticated_session(csrf_token=token)

        response = self.client.post(
            '/ajax/delete_channel',
            json={'channel_id': 'Copen'},
            headers={'X-CSRFToken': token},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json() or {}
        self.assertTrue(payload.get('success'))
        self.channel_manager.delete_channel.assert_called_once_with('Copen', 'test-user', force=False)
        self.p2p_manager.broadcast_delete_signal.assert_called_once()
        kwargs = self.p2p_manager.broadcast_delete_signal.call_args.kwargs
        self.assertEqual(kwargs.get('data_type'), 'channel')
        self.assertEqual(kwargs.get('data_id'), 'Copen')
        self.assertNotIn('target_peer', kwargs)

    def test_delete_channel_treats_null_origin_as_local(self) -> None:
        self._upsert_channel('Cnull', None, 'open')
        token = 'csrf-null-origin'
        self._set_authenticated_session(csrf_token=token)

        response = self.client.post(
            '/ajax/delete_channel',
            json={'channel_id': 'Cnull'},
            headers={'X-CSRFToken': token},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json() or {}
        self.assertTrue(payload.get('success'))
        self.channel_manager.delete_channel.assert_called_once_with('Cnull', 'test-user', force=False)

    def test_delete_channel_targets_private_member_peers(self) -> None:
        self._upsert_channel('Cprivate', 'peer-local', 'private')
        self.channel_manager.get_member_peer_ids.return_value = {'peer-local', 'peer-a', 'peer-b'}
        token = 'csrf-private'
        self._set_authenticated_session(csrf_token=token)

        response = self.client.post(
            '/ajax/delete_channel',
            json={'channel_id': 'Cprivate'},
            headers={'X-CSRFToken': token},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json() or {}
        self.assertTrue(payload.get('success'))
        self.channel_manager.delete_channel.assert_called_once_with('Cprivate', 'test-user', force=False)
        self.assertEqual(self.p2p_manager.broadcast_delete_signal.call_count, 2)
        targeted = {
            call.kwargs.get('target_peer')
            for call in self.p2p_manager.broadcast_delete_signal.call_args_list
        }
        self.assertEqual(targeted, {'peer-a', 'peer-b'})
        for call in self.p2p_manager.broadcast_delete_signal.call_args_list:
            self.assertEqual(call.kwargs.get('data_type'), 'channel')
            self.assertEqual(call.kwargs.get('data_id'), 'Cprivate')

    def test_delete_channel_rejects_general(self) -> None:
        token = 'csrf-general'
        self._set_authenticated_session(csrf_token=token)

        response = self.client.post(
            '/ajax/delete_channel',
            json={'channel_id': 'general'},
            headers={'X-CSRFToken': token},
        )

        self.assertEqual(response.status_code, 403)
        payload = response.get_json() or {}
        self.assertIn('general', payload.get('error', '').lower())
        self.channel_manager.delete_channel.assert_not_called()


if __name__ == '__main__':
    unittest.main()
