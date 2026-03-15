"""Regression tests for stream API endpoints and tokenized playback flow."""

import os
import sqlite3
import sys
import tempfile
import types
import unittest
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
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

from canopy.api.routes import create_api_blueprint
from canopy.core.app import _api_limiter, _install_rate_limiting, _stream_playback_limiter
from canopy.core.streams import StreamManager
from canopy.security.api_keys import ApiKeyInfo, Permission


class _FakeDbManager:
    def __init__(self, conn: sqlite3.Connection, db_path: Path) -> None:
        self._conn = conn
        self.db_path = db_path

    @contextmanager
    def get_connection(self):
        yield self._conn

    def get_user(self, user_id: str):
        row = self._conn.execute(
            "SELECT * FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
        return dict(row) if row else None


class _FakeApiKeyManager:
    def __init__(self, key_map: dict[str, str]) -> None:
        self._key_map = key_map

    def validate_key(self, raw_key: str, required_permission=None):
        user_id = self._key_map.get(raw_key)
        if not user_id:
            return None
        perms = {
            Permission.READ_FEED,
            Permission.WRITE_FEED,
            Permission.READ_MESSAGES,
            Permission.WRITE_MESSAGES,
            Permission.MANAGE_KEYS,
        }
        if required_permission and required_permission not in perms:
            return None
        return ApiKeyInfo(
            id=f"key-{user_id}",
            user_id=user_id,
            key_hash="hash",
            permissions=perms,
            created_at=datetime.now(timezone.utc),
        )


class _FakeP2PManager:
    def __init__(self) -> None:
        self.broadcasts = []

    def get_peer_id(self) -> str:
        return 'peer-local'

    def broadcast_channel_message(self, **kwargs) -> None:
        self.broadcasts.append(kwargs)


class TestApiStreamEndpoints(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)

        self.db_file = Path(self.tempdir.name) / 'stream_api.db'
        self.conn = sqlite3.connect(str(self.db_file))
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
                ('u-member', 'member', 'Member'),
                ('u-admin', 'admin', 'Admin'),
                ('u-outsider', 'outsider', 'Outsider'),
            ],
        )
        self.conn.execute(
            "INSERT INTO channels (id, name, privacy_mode) VALUES (?, ?, ?)",
            ('C1', 'general', 'open'),
        )
        self.conn.executemany(
            "INSERT INTO channel_members (channel_id, user_id, role) VALUES (?, ?, ?)",
            [
                ('C1', 'u-member', 'member'),
                ('C1', 'u-admin', 'admin'),
            ],
        )
        self.conn.commit()

        self.db_manager = _FakeDbManager(self.conn, self.db_file)
        self.api_key_manager = _FakeApiKeyManager(
            {
                'key-member': 'u-member',
                'key-admin': 'u-admin',
                'key-outsider': 'u-outsider',
            }
        )
        self.channel_manager = MagicMock()
        self.channel_manager.send_message.side_effect = lambda *args, **kwargs: SimpleNamespace(
            id='Mstream1',
            created_at=datetime.now(timezone.utc),
        )
        self.channel_manager.get_target_peer_ids_for_channel.return_value = []
        self.profile_manager = MagicMock()
        self.profile_manager.get_profile.return_value = None
        self.p2p_manager = _FakeP2PManager()
        self.stream_manager = StreamManager(
            db=self.db_manager,
            channel_manager=self.channel_manager,
            data_root=self.tempdir.name,
        )

        components = (
            self.db_manager,           # db_manager
            self.api_key_manager,     # api_key_manager
            MagicMock(),              # trust_manager
            MagicMock(),              # message_manager
            self.channel_manager,     # channel_manager
            MagicMock(),              # file_manager
            MagicMock(),              # feed_manager
            MagicMock(),              # interaction_manager
            self.profile_manager,     # profile_manager
            MagicMock(),              # config
            self.p2p_manager,         # p2p_manager
        )
        self.get_components_patcher = patch(
            'canopy.api.routes.get_app_components',
            return_value=components,
        )
        self.get_components_patcher.start()
        self.addCleanup(self.get_components_patcher.stop)

        app = Flask(__name__)
        app.config['TESTING'] = True
        app.secret_key = 'test-secret'
        app.config['STREAM_MANAGER'] = self.stream_manager
        app.register_blueprint(create_api_blueprint(), url_prefix='/api/v1')
        self.client = app.test_client()

    def tearDown(self) -> None:
        self.conn.close()

    def _headers(self, key: str) -> dict[str, str]:
        return {
            'X-API-Key': key,
            'Content-Type': 'application/json',
        }

    def test_create_stream_requires_membership(self) -> None:
        allowed = self.client.post(
            '/api/v1/streams',
            json={
                'channel_id': 'C1',
                'title': 'Ops audio',
                'media_kind': 'audio',
                'auto_post': True,
                'start_now': True,
            },
            headers=self._headers('key-member'),
        )
        self.assertEqual(allowed.status_code, 201)
        payload = allowed.get_json() or {}
        self.assertTrue(payload.get('success'))
        self.assertIn('stream', payload)
        stream = payload.get('stream') or {}
        self.assertEqual(stream.get('channel_id'), 'C1')
        self.assertEqual(stream.get('status'), 'live')
        self.assertEqual(self.channel_manager.send_message.call_count, 1)
        sent_attachments = self.channel_manager.send_message.call_args.kwargs.get('attachments') or []
        self.assertTrue(sent_attachments)
        self.assertEqual(sent_attachments[0].get('kind'), 'stream')
        self.assertEqual(sent_attachments[0].get('stream_id'), stream.get('id'))
        self.assertEqual(sent_attachments[0].get('status'), 'live')

        denied = self.client.post(
            '/api/v1/streams',
            json={
                'channel_id': 'C1',
                'title': 'No access',
            },
            headers=self._headers('key-outsider'),
        )
        self.assertEqual(denied.status_code, 404)

    def test_owner_can_stop_stream_via_api_endpoint(self) -> None:
        created = self.client.post(
            '/api/v1/streams',
            json={
                'channel_id': 'C1',
                'title': 'Ops stop test',
                'media_kind': 'audio',
                'auto_post': False,
                'start_now': True,
            },
            headers=self._headers('key-member'),
        )
        self.assertEqual(created.status_code, 201)
        stream_id = (created.get_json() or {}).get('stream', {}).get('id')
        self.assertTrue(stream_id)
        stopped = self.client.post(
            f'/api/v1/streams/{stream_id}/stop',
            headers=self._headers('key-member'),
        )
        self.assertEqual(stopped.status_code, 200)
        payload = stopped.get_json() or {}
        self.assertTrue(payload.get('success'))
        self.assertEqual((payload.get('stream') or {}).get('status'), 'stopped')

    def test_stream_health_reports_runtime_readiness(self) -> None:
        response = self.client.get(
            '/api/v1/streams/health',
            headers=self._headers('key-member'),
        )
        self.assertEqual(response.status_code, 200)
        payload = response.get_json() or {}
        self.assertTrue(payload.get('success'))
        health = payload.get('health') or {}
        self.assertTrue(health.get('stream_manager_ready'))
        self.assertIn('storage_root', health)
        self.assertEqual(health.get('latency_mode_supported'), 'hls')

    def test_tokenized_ingest_and_playback_flow(self) -> None:
        create_resp = self.client.post(
            '/api/v1/streams',
            json={
                'channel_id': 'C1',
                'title': 'Mesh stream',
                'media_kind': 'video',
                'auto_post': False,
            },
            headers=self._headers('key-member'),
        )
        self.assertEqual(create_resp.status_code, 201)
        stream_id = (create_resp.get_json() or {}).get('stream', {}).get('id')
        self.assertTrue(stream_id)

        ingest_token_resp = self.client.post(
            f'/api/v1/streams/{stream_id}/tokens',
            json={'scope': 'ingest', 'ttl_seconds': 600},
            headers=self._headers('key-member'),
        )
        self.assertEqual(ingest_token_resp.status_code, 200)
        ingest_token = (ingest_token_resp.get_json() or {}).get('token')
        self.assertTrue(ingest_token)

        manifest_bytes = b"#EXTM3U\n#EXT-X-VERSION:3\nseg01.ts\n"
        put_manifest = self.client.put(
            f'/api/v1/streams/{stream_id}/ingest/manifest?token={ingest_token}',
            data=manifest_bytes,
            headers={'Content-Type': 'application/vnd.apple.mpegurl'},
        )
        self.assertEqual(put_manifest.status_code, 200)

        put_segment = self.client.put(
            f'/api/v1/streams/{stream_id}/ingest/segments/seg01.ts?token={ingest_token}',
            data=b'\x01\x02\x03',
            headers={'Content-Type': 'video/mp2t'},
        )
        self.assertEqual(put_segment.status_code, 200)

        join_resp = self.client.post(
            f'/api/v1/streams/{stream_id}/join',
            json={},
            headers=self._headers('key-member'),
        )
        self.assertEqual(join_resp.status_code, 200)
        join_payload = join_resp.get_json() or {}
        self.assertTrue(join_payload.get('success'))
        playback_url = join_payload.get('playback_url') or ''
        token = join_payload.get('token') or ''
        self.assertIn('/manifest.m3u8', playback_url)
        self.assertTrue(token)

        manifest_resp = self.client.get(
            f'/api/v1/streams/{stream_id}/manifest.m3u8?token={token}',
        )
        self.assertEqual(manifest_resp.status_code, 200)
        manifest_text = manifest_resp.data.decode('utf-8')
        self.assertIn(f'/api/v1/streams/{stream_id}/segments/seg01.ts?token=', manifest_text)

        segment_resp = self.client.get(
            f'/api/v1/streams/{stream_id}/segments/seg01.ts?token={token}',
        )
        self.assertEqual(segment_resp.status_code, 200)
        self.assertEqual(segment_resp.data, b'\x01\x02\x03')

        bad_token_resp = self.client.get(
            f'/api/v1/streams/{stream_id}/manifest.m3u8?token=bogus',
        )
        self.assertEqual(bad_token_resp.status_code, 404)

        bad_segment_token_resp = self.client.get(
            f'/api/v1/streams/{stream_id}/segments/seg01.ts?token=bogus',
        )
        self.assertEqual(bad_segment_token_resp.status_code, 404)

    def test_refresh_view_token_revokes_old_token(self) -> None:
        create_resp = self.client.post(
            '/api/v1/streams',
            json={
                'channel_id': 'C1',
                'title': 'Refreshable stream',
                'media_kind': 'audio',
                'auto_post': False,
            },
            headers=self._headers('key-member'),
        )
        self.assertEqual(create_resp.status_code, 201)
        stream_id = (create_resp.get_json() or {}).get('stream', {}).get('id')
        self.assertTrue(stream_id)

        join_resp = self.client.post(
            f'/api/v1/streams/{stream_id}/join',
            json={'ttl_seconds': 600},
            headers=self._headers('key-member'),
        )
        self.assertEqual(join_resp.status_code, 200)
        old_token = (join_resp.get_json() or {}).get('token')
        self.assertTrue(old_token)

        refresh_resp = self.client.post(
            f'/api/v1/streams/{stream_id}/tokens/refresh',
            json={'scope': 'view', 'token': old_token, 'ttl_seconds': 900},
            headers=self._headers('key-member'),
        )
        self.assertEqual(refresh_resp.status_code, 200)
        refreshed = refresh_resp.get_json() or {}
        self.assertTrue(refreshed.get('success'))
        self.assertNotEqual(refreshed.get('token'), old_token)
        self.assertIn('/manifest.m3u8?token=', refreshed.get('playback_url') or '')

        old_manifest = self.client.get(
            f'/api/v1/streams/{stream_id}/manifest.m3u8?token={old_token}',
        )
        self.assertEqual(old_manifest.status_code, 404)

    def test_empty_manifest_ingest_returns_actionable_hint(self) -> None:
        create_resp = self.client.post(
            '/api/v1/streams',
            json={
                'channel_id': 'C1',
                'title': 'Hint stream',
                'media_kind': 'video',
                'auto_post': False,
            },
            headers=self._headers('key-member'),
        )
        self.assertEqual(create_resp.status_code, 201)
        stream_id = (create_resp.get_json() or {}).get('stream', {}).get('id')
        self.assertTrue(stream_id)

        ingest_token_resp = self.client.post(
            f'/api/v1/streams/{stream_id}/tokens',
            json={'scope': 'ingest', 'ttl_seconds': 600},
            headers=self._headers('key-member'),
        )
        self.assertEqual(ingest_token_resp.status_code, 200)
        ingest_token = (ingest_token_resp.get_json() or {}).get('token')
        self.assertTrue(ingest_token)

        put_manifest = self.client.put(
            f'/api/v1/streams/{stream_id}/ingest/manifest?token={ingest_token}',
            data=b'',
            headers={'Content-Type': 'application/vnd.apple.mpegurl'},
        )
        self.assertEqual(put_manifest.status_code, 400)
        payload = put_manifest.get_json() or {}
        self.assertEqual(payload.get('error'), 'empty_ingest_payload')
        self.assertEqual(payload.get('hint'), 'possible_empty_upload_or_proxy_buffering_issue')

    def test_manifest_playback_uses_stream_read_rate_limit_not_generic_api_limit(self) -> None:
        _api_limiter._buckets.clear()
        _stream_playback_limiter._buckets.clear()
        _install_rate_limiting(self.client.application)

        create_resp = self.client.post(
            '/api/v1/streams',
            json={
                'channel_id': 'C1',
                'title': 'Playback limiter stream',
                'media_kind': 'video',
                'auto_post': False,
            },
            headers=self._headers('key-member'),
        )
        self.assertEqual(create_resp.status_code, 201)
        stream_id = (create_resp.get_json() or {}).get('stream', {}).get('id')
        self.assertTrue(stream_id)

        ingest_token_resp = self.client.post(
            f'/api/v1/streams/{stream_id}/tokens',
            json={'scope': 'ingest', 'ttl_seconds': 600},
            headers=self._headers('key-member'),
        )
        self.assertEqual(ingest_token_resp.status_code, 200)
        ingest_token = (ingest_token_resp.get_json() or {}).get('token')
        self.assertTrue(ingest_token)

        put_manifest = self.client.put(
            f'/api/v1/streams/{stream_id}/ingest/manifest?token={ingest_token}',
            data=b'#EXTM3U\n#EXT-X-VERSION:3\n#EXTINF:2.0,\nseg000001.ts\n',
            headers={'Content-Type': 'application/vnd.apple.mpegurl'},
        )
        self.assertEqual(put_manifest.status_code, 200)

        put_segment = self.client.put(
            f'/api/v1/streams/{stream_id}/ingest/segments/seg000001.ts?token={ingest_token}',
            data=b'\x01\x02\x03',
            headers={'Content-Type': 'video/mp2t'},
        )
        self.assertEqual(put_segment.status_code, 200)

        join_resp = self.client.post(
            f'/api/v1/streams/{stream_id}/join',
            json={'ttl_seconds': 600},
            headers=self._headers('key-member'),
        )
        self.assertEqual(join_resp.status_code, 200)
        playback_token = (join_resp.get_json() or {}).get('token')
        self.assertTrue(playback_token)

        statuses = []
        for _ in range(25):
            manifest_resp = self.client.get(
                f'/api/v1/streams/{stream_id}/manifest.m3u8?token={playback_token}',
            )
            statuses.append(manifest_resp.status_code)
        self.assertNotIn(429, statuses)
        self.assertTrue(all(code == 200 for code in statuses))

    def test_telemetry_stream_event_ingest_and_read(self) -> None:
        create_resp = self.client.post(
            '/api/v1/streams',
            json={
                'channel_id': 'C1',
                'title': 'Sensor bus',
                'stream_kind': 'telemetry',
                'protocol': 'events-json',
                'auto_post': False,
            },
            headers=self._headers('key-member'),
        )
        self.assertEqual(create_resp.status_code, 201)
        stream = (create_resp.get_json() or {}).get('stream') or {}
        stream_id = stream.get('id')
        self.assertTrue(stream_id)
        self.assertEqual(stream.get('stream_kind'), 'telemetry')

        ingest_token_resp = self.client.post(
            f'/api/v1/streams/{stream_id}/tokens',
            json={'scope': 'ingest', 'ttl_seconds': 600},
            headers=self._headers('key-member'),
        )
        self.assertEqual(ingest_token_resp.status_code, 200)
        ingest_token = (ingest_token_resp.get_json() or {}).get('token')
        self.assertTrue(ingest_token)

        event_resp = self.client.post(
            f'/api/v1/streams/{stream_id}/ingest/events?token={ingest_token}',
            json={'payload': {'sensor': 'pressure', 'value': 42}, 'event_ts': '2026-02-27T12:00:00Z'},
            headers={'Content-Type': 'application/json'},
        )
        self.assertEqual(event_resp.status_code, 201)
        event_payload = event_resp.get_json() or {}
        self.assertTrue(event_payload.get('success'))
        self.assertEqual((event_payload.get('event') or {}).get('seq'), 1)

        join_resp = self.client.post(
            f'/api/v1/streams/{stream_id}/join',
            json={},
            headers=self._headers('key-member'),
        )
        self.assertEqual(join_resp.status_code, 200)
        join_payload = join_resp.get_json() or {}
        playback_url = join_payload.get('playback_url') or ''
        self.assertIn('/events?token=', playback_url)

        events_resp = self.client.get(playback_url)
        self.assertEqual(events_resp.status_code, 200)
        events_payload = events_resp.get_json() or {}
        self.assertTrue(events_payload.get('success'))
        self.assertEqual(events_payload.get('stream_kind'), 'telemetry')
        events = events_payload.get('events') or []
        self.assertEqual(len(events), 1)
        self.assertEqual((events[0].get('payload') or {}).get('sensor'), 'pressure')


if __name__ == '__main__':
    unittest.main()
