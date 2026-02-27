"""Regression tests for UI stream setup bundle endpoint."""

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

from canopy.core.streams import StreamManager
from canopy.ui.routes import create_ui_blueprint


class _FakeDbManager:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self.db_path = Path(':memory:')

    @contextmanager
    def get_connection(self):
        yield self._conn

    def get_user(self, user_id: str):
        row = self._conn.execute(
            "SELECT * FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
        return dict(row) if row else None

    def get_instance_owner_user_id(self):
        row = self._conn.execute(
            "SELECT id FROM users ORDER BY rowid ASC LIMIT 1"
        ).fetchone()
        if not row:
            return None
        return row['id'] if isinstance(row, sqlite3.Row) else row[0]


class TestUiStreamSetupEndpoint(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)

        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        self.conn.execute('PRAGMA foreign_keys = ON')
        self.conn.executescript(
            """
            CREATE TABLE users (
                id TEXT PRIMARY KEY,
                username TEXT,
                display_name TEXT
            );
            CREATE TABLE channels (
                id TEXT PRIMARY KEY,
                name TEXT,
                privacy_mode TEXT
            );
            CREATE TABLE channel_members (
                channel_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                role TEXT DEFAULT 'member'
            );
            """
        )
        self.conn.executemany(
            "INSERT INTO users (id, username, display_name) VALUES (?, ?, ?)",
            [
                ('u-owner', 'owner', 'Owner'),
                ('u-viewer', 'viewer', 'Viewer'),
                ('u-outsider', 'outsider', 'Outsider'),
            ],
        )
        self.conn.execute(
            "INSERT INTO channels (id, name, privacy_mode) VALUES (?, ?, ?)",
            ('C1', 'ops-stream', 'open'),
        )
        self.conn.executemany(
            "INSERT INTO channel_members (channel_id, user_id, role) VALUES (?, ?, ?)",
            [
                ('C1', 'u-owner', 'member'),
                ('C1', 'u-viewer', 'member'),
            ],
        )
        self.conn.commit()

        self.db_manager = _FakeDbManager(self.conn)
        self.stream_manager = StreamManager(
            db=self.db_manager,
            channel_manager=MagicMock(),
            data_root=self.tempdir.name,
        )
        self.stream_row, create_err = self.stream_manager.create_stream(
            channel_id='C1',
            created_by='u-owner',
            title='Ops audio',
            stream_kind='media',
            media_kind='audio',
            protocol='hls',
        )
        self.assertIsNone(create_err)
        self.assertIsNotNone(self.stream_row)

        components = (
            self.db_manager,    # db_manager
            MagicMock(),        # api_key_manager
            MagicMock(),        # trust_manager
            MagicMock(),        # message_manager
            MagicMock(),        # channel_manager
            MagicMock(),        # file_manager
            MagicMock(),        # feed_manager
            MagicMock(),        # interaction_manager
            MagicMock(),        # profile_manager
            MagicMock(),        # config
            MagicMock(),        # p2p_manager
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
        app.config['STREAM_MANAGER'] = self.stream_manager
        app.register_blueprint(create_ui_blueprint())
        self.client = app.test_client()

    def tearDown(self) -> None:
        self.conn.close()

    def _set_session(self, user_id: str) -> None:
        with self.client.session_transaction() as sess:
            sess['authenticated'] = True
            sess['user_id'] = user_id
            sess['username'] = user_id
            sess['_csrf_token'] = 'csrf-test-token'

    def test_owner_gets_setup_bundle_for_media_stream(self) -> None:
        self._set_session('u-owner')
        stream_id = str((self.stream_row or {}).get('id') or '')
        response = self.client.post(
            f'/ajax/streams/{stream_id}/setup',
            json={'ingest_ttl_seconds': 3600, 'view_ttl_seconds': 900},
            headers={'X-CSRFToken': 'csrf-test-token'},
        )
        self.assertEqual(response.status_code, 200)
        payload = response.get_json() or {}
        self.assertTrue(payload.get('success'))
        setup = payload.get('setup') or {}
        ingest = setup.get('ingest') or {}
        playback = setup.get('playback') or {}
        commands = setup.get('commands') or {}
        self.assertIn('/ingest/manifest?token=', ingest.get('manifest_url') or '')
        self.assertIn('/ingest/segments/seg%06d.ts?token=', ingest.get('segment_url_template') or '')
        self.assertIn('/manifest.m3u8?token=', playback.get('url') or '')
        self.assertIn('ffmpeg', commands.get('posix') or '')
        self.assertIn('ffmpeg', commands.get('powershell') or '')

    def test_non_manager_gets_not_found_style_response(self) -> None:
        self._set_session('u-viewer')
        stream_id = str((self.stream_row or {}).get('id') or '')
        response = self.client.post(
            f'/ajax/streams/{stream_id}/setup',
            json={'ingest_ttl_seconds': 3600},
            headers={'X-CSRFToken': 'csrf-test-token'},
        )
        self.assertEqual(response.status_code, 404)
        payload = response.get_json() or {}
        self.assertEqual(payload.get('error'), 'Not found')


if __name__ == '__main__':
    unittest.main()
