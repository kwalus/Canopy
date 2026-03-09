"""Regression tests for managed large-attachment storage and UI controls."""

import json
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

from canopy.core.files import FileManager
from canopy.core.large_attachments import (
    LARGE_ATTACHMENT_STORE_ROOT_KEY,
    LARGE_ATTACHMENT_THRESHOLD,
)
from canopy.security.file_access import evaluate_file_access_for_peer
from canopy.ui.routes import create_ui_blueprint


class _FakeDbManager:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    @contextmanager
    def get_connection(self, *args, **kwargs):
        yield self.conn

    def get_system_state(self, key: str):
        row = self.conn.execute(
            "SELECT value FROM system_state WHERE key = ?",
            (key,),
        ).fetchone()
        return row['value'] if row else None

    def set_system_state(self, key: str, value):
        if value is None:
            self.conn.execute("DELETE FROM system_state WHERE key = ?", (key,))
        else:
            self.conn.execute(
                "INSERT OR REPLACE INTO system_state (key, value) VALUES (?, ?)",
                (key, value),
            )
        self.conn.commit()
        return True

    def get_instance_owner_user_id(self):
        row = self.conn.execute(
            "SELECT value FROM system_state WHERE key = 'instance_owner_id'"
        ).fetchone()
        return row['value'] if row else None


class _RouteFileManager:
    def __init__(self) -> None:
        self.upserts = []

    def upsert_remote_attachment_transfer(self, **kwargs):
        self.upserts.append(kwargs)
        return True


class TestLargeAttachmentStoreSupport(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)

        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            """
            CREATE TABLE users (
                id TEXT PRIMARY KEY,
                origin_peer TEXT
            );
            CREATE TABLE system_state (
                key TEXT PRIMARY KEY,
                value TEXT
            );
            CREATE TABLE channels (
                id TEXT PRIMARY KEY,
                privacy_mode TEXT
            );
            CREATE TABLE channel_members (
                channel_id TEXT,
                user_id TEXT
            );
            CREATE TABLE channel_messages (
                id TEXT PRIMARY KEY,
                channel_id TEXT,
                attachments TEXT,
                content TEXT
            );
            CREATE TABLE messages (
                id TEXT PRIMARY KEY,
                sender_id TEXT,
                recipient_id TEXT,
                metadata TEXT,
                content TEXT
            );
            CREATE TABLE feed_posts (
                id TEXT PRIMARY KEY,
                author_id TEXT,
                metadata TEXT,
                content TEXT,
                visibility TEXT
            );
            """
        )
        self.conn.execute("INSERT INTO users (id, origin_peer) VALUES (?, ?)", ('user-local', None))
        self.conn.execute("INSERT INTO users (id, origin_peer) VALUES (?, ?)", ('user-remote', 'peer-remote'))
        self.conn.execute(
            "INSERT INTO system_state (key, value) VALUES ('instance_owner_id', 'user-local')"
        )
        self.conn.commit()
        self.db = _FakeDbManager(self.conn)

    def tearDown(self) -> None:
        self.conn.close()

    def test_large_files_use_managed_external_store_root(self) -> None:
        default_root = self.root / 'files'
        external_root = self.root / 'external-store'
        self.db.set_system_state(LARGE_ATTACHMENT_STORE_ROOT_KEY, str(external_root))

        manager = FileManager(self.db, str(default_root))

        small = manager.save_file(b'abc', 'small.txt', 'text/plain', 'user-local')
        self.assertIsNotNone(small)
        assert small is not None
        self.assertTrue(str(default_root) in small.file_path)

        large_payload = b'a' * (LARGE_ATTACHMENT_THRESHOLD + 1024)
        large = manager.save_file(
            large_payload,
            'large.bin',
            'application/octet-stream',
            'user-local',
        )
        self.assertIsNotNone(large)
        assert large is not None
        self.assertIn('canopy-large-attachments', large.file_path)
        self.assertTrue(str(external_root) in large.file_path)

    def test_remote_transfer_tracking_round_trip(self) -> None:
        manager = FileManager(self.db, str(self.root / 'files'))
        ok = manager.upsert_remote_attachment_transfer(
            origin_peer_id='peer-remote',
            origin_file_id='Forigin123',
            file_name='report.zip',
            content_type='application/zip',
            size=42,
            checksum='abc123',
            status='pending',
        )
        self.assertTrue(ok)

        row = manager.get_remote_attachment_transfer('peer-remote', 'Forigin123')
        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual(row['status'], 'pending')
        self.assertEqual(row['file_name'], 'report.zip')

        listed = manager.list_pending_remote_attachment_transfers(origin_peer_id='peer-remote')
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]['origin_file_id'], 'Forigin123')

    def test_peer_access_allows_open_channel_and_dm_participant(self) -> None:
        open_file_id = 'Flarge-open'
        dm_file_id = 'Flarge-dm'
        attachments = json.dumps([{'origin_file_id': open_file_id, 'source_peer_id': 'peer-origin'}])
        self.conn.execute(
            "INSERT INTO channels (id, privacy_mode) VALUES (?, ?)",
            ('Copen', 'open'),
        )
        self.conn.execute(
            "INSERT INTO channel_messages (id, channel_id, attachments, content) VALUES (?, ?, ?, ?)",
            ('Mopen', 'Copen', attachments, ''),
        )
        self.conn.execute(
            "INSERT INTO messages (id, sender_id, recipient_id, metadata, content) VALUES (?, ?, ?, ?, ?)",
            (
                'DM1',
                'user-local',
                'group:abc',
                json.dumps({'group_members': ['user-local', 'user-remote'], 'attachments': [{'origin_file_id': dm_file_id, 'source_peer_id': 'peer-origin'}]}),
                '',
            ),
        )
        self.conn.commit()

        open_result = evaluate_file_access_for_peer(
            db_manager=self.db,
            file_id=open_file_id,
            requester_peer_id='peer-remote',
        )
        self.assertTrue(open_result.allowed)
        self.assertEqual(open_result.reason, 'channel-peer-membership')

        dm_result = evaluate_file_access_for_peer(
            db_manager=self.db,
            file_id=dm_file_id,
            requester_peer_id='peer-remote',
        )
        self.assertTrue(dm_result.allowed)
        self.assertEqual(dm_result.reason, 'direct-message-peer-visibility')

    def test_peer_access_allows_public_feed(self) -> None:
        file_id = 'Ffeed1'
        self.conn.execute(
            "INSERT INTO feed_posts (id, author_id, metadata, content, visibility) VALUES (?, ?, ?, ?, ?)",
            (
                'P1',
                'user-local',
                json.dumps({'attachments': [{'origin_file_id': file_id, 'source_peer_id': 'peer-origin'}]}),
                '',
                'network',
            ),
        )
        self.conn.commit()

        result = evaluate_file_access_for_peer(
            db_manager=self.db,
            file_id=file_id,
            requester_peer_id='peer-remote',
        )
        self.assertTrue(result.allowed)
        self.assertEqual(result.reason, 'feed-network-visibility')

    def test_ui_routes_persist_settings_and_request_manual_download(self) -> None:
        file_manager = _RouteFileManager()
        p2p_manager = MagicMock()
        p2p_manager.connection_manager.is_connected.return_value = True
        p2p_manager.peer_supports_capability.return_value = True
        p2p_manager.send_large_attachment_request.return_value = True

        config = MagicMock()
        config.to_dict.return_value = {
            'network': {
                'host': '127.0.0.1',
                'port': 7770,
                'mesh_port': 7771,
                'discovery_port': 7772,
                'max_peers': 32,
                'connection_timeout': 5,
            },
            'debug': False,
            'storage': {
                'database_path': str(self.root / 'canopy.db'),
                'backup_interval': 300,
                'max_message_size': 65536,
                'max_file_size': 104857600,
            },
            'security': {
                'trust_threshold': 50,
                'encryption_algorithm': 'ChaCha20-Poly1305',
                'key_derivation_rounds': 100000,
                'session_timeout': 3600,
                'max_key_age': 86400,
            },
            'ui': {
                'theme': 'default',
                'language': 'en',
                'auto_refresh': 5,
                'max_feed_items': 50,
            },
        }
        config.network.host = '127.0.0.1'
        config.network.port = 7770
        config.network.mesh_port = 7771
        config.network.discovery_port = 7772
        config.network.max_peers = 32
        config.network.connection_timeout = 5
        config.debug = False
        config.storage.database_path = str(self.root / 'canopy.db')
        config.storage.backup_interval = 300
        config.storage.max_message_size = 65536
        config.storage.max_file_size = 104857600
        config.security.trust_threshold = 50
        config.security.encryption_algorithm = 'ChaCha20-Poly1305'
        config.security.key_derivation_rounds = 100000
        config.security.session_timeout = 3600
        config.security.max_key_age = 86400
        config.ui.theme = 'default'
        config.ui.language = 'en'
        config.ui.auto_refresh = 5
        config.ui.max_feed_items = 50

        components = (
            self.db,
            MagicMock(),
            MagicMock(),
            MagicMock(),
            MagicMock(),
            file_manager,
            MagicMock(),
            MagicMock(),
            MagicMock(),
            config,
            p2p_manager,
        )

        app = Flask(__name__)
        app.config['TESTING'] = True
        app.secret_key = 'test-secret'
        with patch('canopy.ui.routes.get_app_components', return_value=components):
            app.register_blueprint(create_ui_blueprint())
            client = app.test_client()
            csrf_token = 'test-csrf-token'
            with client.session_transaction() as sess:
                sess['authenticated'] = True
                sess['user_id'] = 'user-local'
                sess['_csrf_token'] = csrf_token

            response = client.post(
                '/ajax/settings/large-attachments',
                json={
                    'store_root': str(self.root / 'managed'),
                    'download_mode': 'manual',
                },
                headers={'X-CSRFToken': csrf_token},
            )
            self.assertEqual(response.status_code, 200)
            payload = response.get_json()
            self.assertTrue(payload['success'])
            self.assertEqual(self.db.get_system_state(LARGE_ATTACHMENT_STORE_ROOT_KEY), str(self.root / 'managed'))

            response = client.post(
                '/ajax/files/request-remote-download',
                json={
                    'attachment': {
                        'origin_file_id': 'Forigin55',
                        'source_peer_id': 'peer-remote',
                        'name': 'big.zip',
                        'type': 'application/zip',
                        'size': 123,
                        'large_attachment': True,
                        'storage_mode': 'remote_large',
                    }
                },
                headers={'X-CSRFToken': csrf_token},
            )
            self.assertEqual(response.status_code, 200)
            payload = response.get_json()
            self.assertTrue(payload['success'])
            self.assertTrue(file_manager.upserts)
            p2p_manager.send_large_attachment_request.assert_called_once()

    def test_manual_download_route_respects_paused_policy(self) -> None:
        self.db.set_system_state('large_attachment_download_mode', 'paused')
        file_manager = _RouteFileManager()
        p2p_manager = MagicMock()
        p2p_manager.connection_manager.is_connected.return_value = True
        p2p_manager.peer_supports_capability.return_value = True

        config = MagicMock()
        config.to_dict.return_value = {'network': {}, 'storage': {}, 'security': {}, 'ui': {}, 'debug': False}
        components = (
            self.db,
            MagicMock(),
            MagicMock(),
            MagicMock(),
            MagicMock(),
            file_manager,
            MagicMock(),
            MagicMock(),
            MagicMock(),
            config,
            p2p_manager,
        )

        app = Flask(__name__)
        app.config['TESTING'] = True
        app.secret_key = 'paused-policy-secret'
        with patch('canopy.ui.routes.get_app_components', return_value=components):
            app.register_blueprint(create_ui_blueprint())
            client = app.test_client()
            csrf_token = 'paused-csrf-token'
            with client.session_transaction() as sess:
                sess['authenticated'] = True
                sess['user_id'] = 'user-local'
                sess['_csrf_token'] = csrf_token

            response = client.post(
                '/ajax/files/request-remote-download',
                json={
                    'attachment': {
                        'origin_file_id': 'Forigin-paused',
                        'source_peer_id': 'peer-remote',
                        'name': 'big.zip',
                        'type': 'application/zip',
                        'size': 123,
                        'large_attachment': True,
                        'storage_mode': 'remote_large',
                    }
                },
                headers={'X-CSRFToken': csrf_token},
            )
            self.assertEqual(response.status_code, 409)
            payload = response.get_json() or {}
            self.assertFalse(payload.get('success'))
            self.assertFalse(file_manager.upserts)
            p2p_manager.send_large_attachment_request.assert_not_called()


if __name__ == '__main__':
    unittest.main()
