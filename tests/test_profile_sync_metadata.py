"""Regression tests for profile sync metadata propagation."""

import os
import sqlite3
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

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

from canopy.core.app import create_app
from canopy.core.profile import ProfileManager


class _FakeP2PNetworkManager:
    def __init__(self, config, db_manager):
        self.config = config
        self.db_manager = db_manager
        self.local_identity = types.SimpleNamespace(peer_id='peer-local', x25519_private_key=None)
        self.identity_manager = types.SimpleNamespace(
            local_identity=self.local_identity,
            peer_display_names={},
            known_peers={},
        )
        self.connection_manager = types.SimpleNamespace(get_connected_peers=lambda: [], get_connection=lambda peer_id: None)
        self.discovery = None
        self.peer_versions = {}
        self._running = False
        self.on_profile_sync = None

    def get_peer_id(self):
        return self.local_identity.peer_id

    def is_running(self):
        return self._running

    def start(self):
        self._running = True


class _FakeDb:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def get_connection(self) -> sqlite3.Connection:
        return self._conn


class _FakeFileManager:
    def get_file_data(self, file_id: str):
        return None


class TestProfileSyncMetadata(unittest.TestCase):
    def test_get_profile_card_includes_account_type(self) -> None:
        conn = sqlite3.connect(':memory:')
        conn.row_factory = sqlite3.Row
        self.addCleanup(conn.close)
        conn.executescript(
            """
            CREATE TABLE users (
                id TEXT PRIMARY KEY,
                username TEXT,
                display_name TEXT,
                origin_peer TEXT,
                bio TEXT,
                avatar_file_id TEXT,
                agent_directives TEXT,
                theme_preference TEXT,
                notification_settings TEXT,
                privacy_settings TEXT,
                created_at TEXT,
                profile_updated_at TEXT,
                account_type TEXT DEFAULT 'human',
                updated_at TEXT
            );
            """
        )
        conn.execute(
            """
            INSERT INTO users (
                id, username, display_name, origin_peer, bio, avatar_file_id,
                agent_directives, theme_preference, notification_settings,
                privacy_settings, created_at, profile_updated_at, account_type, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'), ?, datetime('now'))
            """,
            (
                'agent-local',
                'agent_local',
                'Agent Local',
                None,
                'Profile',
                None,
                None,
                'dark',
                None,
                None,
                'agent',
            ),
        )
        conn.commit()

        profile_manager = ProfileManager(_FakeDb(conn), _FakeFileManager())
        card = profile_manager.get_profile_card('agent-local')
        self.assertIsNotNone(card)
        self.assertEqual(card.get('account_type'), 'agent')

    def test_update_from_remote_applies_remote_account_type(self) -> None:
        conn = sqlite3.connect(':memory:')
        conn.row_factory = sqlite3.Row
        self.addCleanup(conn.close)
        conn.executescript(
            """
            CREATE TABLE users (
                id TEXT PRIMARY KEY,
                username TEXT,
                display_name TEXT,
                origin_peer TEXT,
                bio TEXT,
                avatar_file_id TEXT,
                agent_directives TEXT,
                theme_preference TEXT,
                notification_settings TEXT,
                privacy_settings TEXT,
                created_at TEXT,
                profile_updated_at TEXT,
                account_type TEXT DEFAULT 'human',
                updated_at TEXT
            );
            """
        )
        conn.execute(
            """
            INSERT INTO users (
                id, username, display_name, origin_peer, bio, avatar_file_id,
                agent_directives, theme_preference, notification_settings,
                privacy_settings, created_at, profile_updated_at, account_type, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'), ?, datetime('now'))
            """,
            (
                'remote-agent',
                'peer-remote-agent',
                'Remote Agent',
                'peer-remote',
                '',
                None,
                None,
                'dark',
                None,
                None,
                'human',
            ),
        )
        conn.commit()

        profile_manager = ProfileManager(_FakeDb(conn), _FakeFileManager())
        changed = profile_manager.update_from_remote(
            'remote-agent',
            {
                'display_name': 'Remote Agent',
                'bio': '',
                'username': 'remote_agent',
                'account_type': 'agent',
            },
            force_display_name=True,
        )
        self.assertTrue(changed)
        row = conn.execute("SELECT account_type FROM users WHERE id = 'remote-agent'").fetchone()
        self.assertEqual(row['account_type'], 'agent')

    def test_profile_sync_includes_local_key_only_users(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)

        env_patcher = patch.dict(
            os.environ,
            {
                'CANOPY_TESTING': 'true',
                'CANOPY_DISABLE_MESH': 'true',
                'CANOPY_DATA_DIR': tempdir.name,
                'CANOPY_DATABASE_PATH': os.path.join(tempdir.name, 'canopy.db'),
                'CANOPY_SECRET_KEY': 'test-secret',
            },
            clear=False,
        )
        env_patcher.start()
        self.addCleanup(env_patcher.stop)

        checkpoint_patcher = patch(
            'canopy.core.database.DatabaseManager._start_checkpoint_thread',
            lambda self: None,
        )
        checkpoint_patcher.start()
        self.addCleanup(checkpoint_patcher.stop)

        logging_patcher = patch(
            'canopy.core.app.setup_logging',
            lambda debug=False: None,
        )
        logging_patcher.start()
        self.addCleanup(logging_patcher.stop)

        p2p_patcher = patch(
            'canopy.core.app.P2PNetworkManager',
            _FakeP2PNetworkManager,
        )
        p2p_patcher.start()
        self.addCleanup(p2p_patcher.stop)

        app = create_app()
        db_manager = app.config['DB_MANAGER']
        p2p_manager = app.config['P2P_MANAGER']
        stream_manager = app.config.get('STREAM_MANAGER')

        self.assertIsNotNone(stream_manager)
        self.assertEqual(stream_manager.__class__.__name__, 'StreamManager')

        with app.app_context():
            with db_manager.get_connection() as conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO users (
                        id, username, public_key, password_hash, display_name,
                        origin_peer, account_type, status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                    """,
                    (
                        'local-key-only',
                        'local_key_only',
                        'public-key-material',
                        None,
                        'Local Key Only',
                        None,
                        'human',
                        'active',
                    ),
                )
                conn.commit()

        cards = p2p_manager.get_all_local_profile_cards()
        by_user_id = {card.get('user_id'): card for card in cards or []}
        self.assertIn('local-key-only', by_user_id)
        self.assertEqual(by_user_id['local-key-only'].get('account_type'), 'human')


if __name__ == '__main__':
    unittest.main()
