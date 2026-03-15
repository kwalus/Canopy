"""Regression tests for stream lifecycle, token scope, and manifest safety."""

import os
import sqlite3
import sys
import tempfile
import types
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock

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


class _FakeDbManager:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    @contextmanager
    def get_connection(self):
        yield self._conn


class TestStreamManager(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.db_path = Path(self.tempdir.name) / 'stream_manager.db'
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute('PRAGMA foreign_keys = ON')
        self.conn.executescript(
            """
            CREATE TABLE users (
                id TEXT PRIMARY KEY,
                username TEXT UNIQUE,
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
                ('u-admin', 'admin', 'Admin User'),
                ('u-member', 'member', 'Member User'),
                ('u-outsider', 'outsider', 'Outsider User'),
            ],
        )
        self.conn.execute(
            "INSERT INTO channels (id, name, privacy_mode) VALUES (?, ?, ?)",
            ('Cmain', 'main', 'open'),
        )
        self.conn.executemany(
            "INSERT INTO channel_members (channel_id, user_id, role) VALUES (?, ?, ?)",
            [
                ('Cmain', 'u-admin', 'admin'),
                ('Cmain', 'u-member', 'member'),
            ],
        )
        self.conn.commit()

        self.db = _FakeDbManager(self.conn)
        self.manager = StreamManager(
            db=self.db,
            channel_manager=MagicMock(),
            data_root=self.tempdir.name,
        )

    def tearDown(self) -> None:
        self.conn.close()

    def test_create_stream_requires_channel_membership(self) -> None:
        stream_row, error = self.manager.create_stream(
            channel_id='Cmain',
            created_by='u-outsider',
            title='No Access',
        )
        self.assertIsNone(stream_row)
        self.assertEqual(error, 'not_channel_member')

    def test_issue_token_rejects_invalid_ttl(self) -> None:
        stream_row, error = self.manager.create_stream(
            channel_id='Cmain',
            created_by='u-member',
            title='TTL test',
        )
        self.assertIsNone(error)
        self.assertIsNotNone(stream_row)

        token_payload, token_error = self.manager.issue_token(
            stream_id=stream_row['id'],
            user_id='u-member',
            scope='view',
            ttl_seconds='not-an-int',
        )
        self.assertIsNone(token_payload)
        self.assertEqual(token_error, 'invalid_ttl')

    def test_stream_health_and_default_latency_metadata(self) -> None:
        stream_row, error = self.manager.create_stream(
            channel_id='Cmain',
            created_by='u-member',
            title='Health test',
        )
        self.assertIsNone(error)
        self.assertEqual((stream_row or {}).get('metadata', {}).get('latency_mode'), 'hls')

        health = self.manager.get_runtime_health()
        self.assertTrue(health.get('stream_manager_ready'))
        self.assertEqual(health.get('latency_mode_supported'), 'hls')
        self.assertIn('storage_root', health)
        self.assertGreaterEqual(int(health.get('streams_total') or 0), 1)

    def test_view_and_ingest_scope_authorization(self) -> None:
        stream_row, error = self.manager.create_stream(
            channel_id='Cmain',
            created_by='u-member',
            title='Scope test',
        )
        self.assertIsNone(error)
        self.assertIsNotNone(stream_row)
        stream_id = stream_row['id']

        view_payload, view_error = self.manager.issue_token(
            stream_id=stream_id,
            user_id='u-member',
            scope='view',
        )
        self.assertIsNone(view_error)
        self.assertIsNotNone(view_payload)

        token_data, token_err = self.manager.validate_token(
            stream_id=stream_id,
            token=view_payload['token'],
            scope='view',
        )
        self.assertIsNone(token_err)
        self.assertEqual(token_data['user_id'], 'u-member')

        _, wrong_scope_err = self.manager.validate_token(
            stream_id=stream_id,
            token=view_payload['token'],
            scope='ingest',
        )
        self.assertEqual(wrong_scope_err, 'invalid_token')

        ingest_payload, ingest_error = self.manager.issue_token(
            stream_id=stream_id,
            user_id='u-admin',
            scope='ingest',
        )
        self.assertIsNone(ingest_error)
        self.assertIsNotNone(ingest_payload)

        denied_ingest, denied_ingest_err = self.manager.issue_token(
            stream_id=stream_id,
            user_id='u-outsider',
            scope='ingest',
        )
        self.assertIsNone(denied_ingest)
        self.assertEqual(denied_ingest_err, 'not_authorized')

    def test_refresh_token_revokes_old_token(self) -> None:
        stream_row, error = self.manager.create_stream(
            channel_id='Cmain',
            created_by='u-member',
            title='Refresh test',
        )
        self.assertIsNone(error)
        stream_id = stream_row['id']

        view_payload, view_error = self.manager.issue_token(
            stream_id=stream_id,
            user_id='u-member',
            scope='view',
            ttl_seconds=300,
        )
        self.assertIsNone(view_error)
        self.assertIsNotNone(view_payload)

        refreshed, refresh_error = self.manager.refresh_token(
            stream_id=stream_id,
            current_token=view_payload['token'],
            scope='view',
            user_id='u-member',
            ttl_seconds=600,
            metadata={'issued_via': 'test'},
        )
        self.assertIsNone(refresh_error)
        self.assertIsNotNone(refreshed)
        self.assertNotEqual(refreshed['token'], view_payload['token'])
        _, old_error = self.manager.validate_token(
            stream_id=stream_id,
            token=view_payload['token'],
            scope='view',
        )
        self.assertEqual(old_error, 'revoked_token')

    def test_status_changes_sync_stream_attachment_status_hook(self) -> None:
        stream_row, error = self.manager.create_stream(
            channel_id='Cmain',
            created_by='u-member',
            title='Lifecycle sync test',
        )
        self.assertIsNone(error)
        stream_id = (stream_row or {}).get('id')
        self.assertTrue(stream_id)

        started, start_error = self.manager.start_stream(stream_id, 'u-member')
        self.assertIsNone(start_error)
        self.assertEqual((started or {}).get('status'), 'live')
        self.manager.channel_manager.update_stream_attachment_status.assert_called_with(stream_id, 'live')

        stopped, stop_error = self.manager.stop_stream(stream_id, 'u-member')
        self.assertIsNone(stop_error)
        self.assertEqual((stopped or {}).get('status'), 'stopped')
        self.manager.channel_manager.update_stream_attachment_status.assert_called_with(stream_id, 'stopped')

    def test_manifest_rewrite_and_segment_guards(self) -> None:
        stream_row, error = self.manager.create_stream(
            channel_id='Cmain',
            created_by='u-member',
            title='Manifest test',
        )
        self.assertIsNone(error)
        self.assertIsNotNone(stream_row)
        stream_id = stream_row['id']

        ingest, ingest_err = self.manager.issue_token(
            stream_id=stream_id,
            user_id='u-member',
            scope='ingest',
        )
        self.assertIsNone(ingest_err)
        self.assertIsNotNone(ingest)
        view, view_err = self.manager.issue_token(
            stream_id=stream_id,
            user_id='u-member',
            scope='view',
        )
        self.assertIsNone(view_err)
        self.assertIsNotNone(view)

        manifest = (
            "#EXTM3U\n"
            "#EXT-X-VERSION:3\n"
            "seg01.ts\n"
            "../escape.ts\n"
            "nested/seg02.ts\n"
            "https://cdn.example/seg03.ts\n"
            "seg04.m4s\n"
        )
        self.assertIsNone(
            self.manager.store_manifest(
                stream_id=stream_id,
                manifest_bytes=manifest.encode('utf-8'),
            )
        )
        self.assertIsNone(
            self.manager.store_segment(
                stream_id=stream_id,
                segment_name='seg01.ts',
                segment_bytes=b'\x00\x01\x02',
            )
        )
        self.assertEqual(
            self.manager.store_segment(
                stream_id=stream_id,
                segment_name='../bad.ts',
                segment_bytes=b'bad',
            ),
            'invalid_segment_name',
        )

        rendered, render_err = self.manager.render_manifest_for_token(
            stream_id=stream_id,
            token=view['token'],
            api_base_path='/api/v1/streams',
        )
        self.assertIsNone(render_err)
        self.assertIsNotNone(rendered)
        rendered_text = rendered.decode('utf-8')
        self.assertIn('/api/v1/streams/', rendered_text)
        self.assertIn('/segments/seg01.ts?token=', rendered_text)
        self.assertIn('/segments/seg04.m4s?token=', rendered_text)
        self.assertIn('https://cdn.example/seg03.ts', rendered_text)
        self.assertNotIn('../escape.ts', rendered_text)
        self.assertNotIn('nested/seg02.ts', rendered_text)

        data, mimetype, data_err = self.manager.get_segment_data(
            stream_id=stream_id,
            segment_name='seg01.ts',
        )
        self.assertIsNone(data_err)
        self.assertEqual(data, b'\x00\x01\x02')
        self.assertEqual(mimetype, 'video/mp2t')

    def test_telemetry_event_store_list_and_retention(self) -> None:
        stream_row, error = self.manager.create_stream(
            channel_id='Cmain',
            created_by='u-admin',
            title='Plant sensor bus',
            stream_kind='telemetry',
            protocol='events-json',
            metadata={'retention_max_events': 3},
        )
        self.assertIsNone(error)
        self.assertIsNotNone(stream_row)
        stream_id = stream_row['id']
        self.assertEqual(stream_row.get('stream_kind'), 'telemetry')
        self.assertEqual(stream_row.get('media_kind'), 'data')

        for idx in range(5):
            event_row, event_err = self.manager.store_event(
                stream_id=stream_id,
                event_payload={'sensor': 'temp', 'value': idx},
                content_type='application/json',
            )
            self.assertIsNone(event_err)
            self.assertIsNotNone(event_row)
            self.assertEqual(event_row.get('seq'), idx + 1)

        events, list_err = self.manager.list_events(stream_id=stream_id, after_seq=0, limit=10)
        self.assertIsNone(list_err)
        self.assertIsNotNone(events)
        seqs = [int(ev.get('seq') or 0) for ev in events]
        self.assertEqual(seqs, [3, 4, 5])
        last_payload = events[-1].get('payload') or {}
        self.assertEqual(last_payload.get('value'), 4)


if __name__ == '__main__':
    unittest.main()
