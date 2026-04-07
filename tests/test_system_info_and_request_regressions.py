"""Regression tests for system info component wiring and request member writes."""

import os
import sqlite3
import sys
import tempfile
import types
import unittest
from contextlib import contextmanager
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

from canopy.api.routes import create_api_blueprint
from canopy.core.requests import RequestManager


class _SqliteDbManager:
    def __init__(self, db_path: Path, default_busy_timeout_ms: int = 3000) -> None:
        self.db_path = db_path
        self.default_busy_timeout_ms = default_busy_timeout_ms

    @contextmanager
    def get_connection(self, busy_timeout_ms=None, use_pool: bool = False):
        del use_pool
        timeout_ms = (
            self.default_busy_timeout_ms if busy_timeout_ms is None else int(busy_timeout_ms)
        )
        conn = sqlite3.connect(
            str(self.db_path),
            timeout=timeout_ms / 1000.0,
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute(f"PRAGMA busy_timeout = {timeout_ms}")
        try:
            yield conn
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


class TestSystemInfoRegression(unittest.TestCase):
    def test_info_uses_trust_manager_slot_from_app_components(self) -> None:
        db_manager = MagicMock()
        db_manager.get_database_stats.return_value = {'messages': 12}

        api_key_manager = MagicMock()
        api_key_manager.validate_key.return_value = MagicMock(user_id='owner-user')

        trust_manager = MagicMock()
        trust_manager.get_trust_statistics.return_value = {'trusted': 4, 'blocked': 1}

        config = MagicMock()
        config.network.max_peers = 50
        config.security.trust_threshold = 60
        config.storage.max_message_size = 1024

        p2p_manager = MagicMock()
        p2p_manager.get_network_status.return_value = {'running': True, 'connected_peers': 3}

        components = (
            db_manager,
            api_key_manager,
            trust_manager,
            object(),  # MessageManager slot should not be used for trust stats.
            MagicMock(),
            MagicMock(),
            MagicMock(),
            MagicMock(),
            MagicMock(),
            config,
            p2p_manager,
        )

        app = Flask(__name__)
        app.config['TESTING'] = True

        with patch('canopy.api.routes.get_app_components', return_value=components):
            app.register_blueprint(create_api_blueprint(), url_prefix='/api/v1')
            client = app.test_client()
            response = client.get('/api/v1/info', headers={'X-API-Key': 'test-key'})

        self.assertEqual(response.status_code, 200)
        payload = response.get_json() or {}
        self.assertEqual(payload.get('database_stats'), {'messages': 12})
        self.assertEqual(payload.get('trust_stats'), {'trusted': 4, 'blocked': 1})
        self.assertEqual((payload.get('p2p_network') or {}).get('connected_peers'), 3)
        trust_manager.get_trust_statistics.assert_called_once()


class TestRequestMemberLockRegression(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.db_path = Path(self.tempdir.name) / 'requests.db'
        self.db_manager = _SqliteDbManager(self.db_path, default_busy_timeout_ms=50)
        self.manager = RequestManager(self.db_manager)

    def test_upsert_request_persists_members_without_self_lock(self) -> None:
        result = self.manager.upsert_request(
            request_id='request-upsert-1',
            title='Investigate request sync',
            created_by='user-owner',
            members=[{'user_id': 'user-agent', 'role': 'assignee'}],
            members_defined=True,
            actor_id='user-owner',
        )

        self.assertIsNotNone(result)
        self.assertEqual(
            result.get('members'),
            [{'user_id': 'user-agent', 'role': 'assignee'}],
        )

    def test_set_members_retries_transient_database_lock(self) -> None:
        created = self.manager.upsert_request(
            request_id='request-retry-1',
            title='Retry member writes',
            created_by='user-owner',
        )
        self.assertIsNotNone(created)

        original = self.manager._set_members
        calls = {'count': 0}

        def flaky_set_members(conn, request_id, members, added_by=None):
            calls['count'] += 1
            if calls['count'] == 1:
                raise sqlite3.OperationalError('database is locked')
            return original(conn, request_id, members, added_by=added_by)

        with patch.object(self.manager, '_set_members', side_effect=flaky_set_members):
            with patch('canopy.core.requests.time.sleep', return_value=None):
                self.manager.set_members(
                    'request-retry-1',
                    [{'user_id': 'user-reviewer', 'role': 'reviewer'}],
                    added_by='user-owner',
                )

        self.assertEqual(calls['count'], 2)
        self.assertEqual(
            self.manager.list_members('request-retry-1'),
            [{'user_id': 'user-reviewer', 'role': 'reviewer'}],
        )


if __name__ == '__main__':
    unittest.main()
