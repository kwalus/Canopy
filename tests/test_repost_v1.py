"""Security and regression coverage for feed repost v1."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import types
import unittest
from contextlib import contextmanager
from datetime import datetime, timezone
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
from canopy.core.feed import FeedAlgorithm, FeedManager, PostVisibility, PostType
from canopy.security.api_keys import ApiKeyInfo, Permission


class _FakeDbManager:
    def __init__(self, conn: sqlite3.Connection, db_path: Path) -> None:
        self._conn = conn
        self.db_path = db_path

    @contextmanager
    def get_connection(self):
        yield self._conn

    def get_user(self, user_id: str):
        row = self._conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return dict(row) if row else None

    def get_instance_owner_user_id(self):
        return 'owner'


class _FakeApiKeyManager:
    def __init__(self, key_map: dict[str, ApiKeyInfo]) -> None:
        self.key_map = dict(key_map)

    def validate_key(self, raw_key: str, required_permission=None):
        key_info = self.key_map.get(raw_key)
        if not key_info:
            return None
        if required_permission and not key_info.has_permission(required_permission):
            return None
        return key_info


def _create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE users (
            id TEXT PRIMARY KEY,
            username TEXT,
            display_name TEXT,
            account_type TEXT,
            status TEXT,
            origin_peer TEXT,
            created_at TEXT
        );
        CREATE TABLE feed_posts (
            id TEXT PRIMARY KEY,
            author_id TEXT NOT NULL,
            content TEXT NOT NULL,
            content_type TEXT DEFAULT 'text',
            visibility TEXT DEFAULT 'network',
            metadata TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            expires_at TIMESTAMP,
            likes INTEGER DEFAULT 0,
            comments INTEGER DEFAULT 0,
            shares INTEGER DEFAULT 0,
            source_type TEXT DEFAULT 'human',
            source_agent_id TEXT DEFAULT NULL,
            source_url TEXT DEFAULT NULL,
            tags TEXT DEFAULT NULL,
            last_activity_at TIMESTAMP
        );
        CREATE TABLE post_permissions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            post_id TEXT NOT NULL,
            user_id TEXT NOT NULL
        );
        """
    )
    conn.executemany(
        "INSERT INTO users (id, username, display_name, account_type, status, origin_peer, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            ('author', 'author', 'Author', 'human', 'active', None, '2026-03-23T08:00:00+00:00'),
            ('reposter', 'reposter', 'Reposter', 'human', 'active', None, '2026-03-23T08:01:00+00:00'),
            ('viewer', 'viewer', 'Viewer', 'agent', 'active', None, '2026-03-23T08:02:00+00:00'),
        ],
    )
    conn.commit()


class TestFeedRepostManager(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        db_path = Path(self.tempdir.name) / 'repost_v1.db'
        self.conn = sqlite3.connect(str(db_path))
        self.conn.row_factory = sqlite3.Row
        _create_schema(self.conn)
        self.db_manager = _FakeDbManager(self.conn, db_path)
        self.manager = FeedManager(self.db_manager, MagicMock())

    def tearDown(self) -> None:
        self.conn.close()

    def _create_feed_post(self, *, visibility: PostVisibility, metadata=None, permissions=None, content='Source post'):
        post = self.manager.create_post(
            author_id='author',
            content=content,
            post_type=PostType.TEXT,
            visibility=visibility,
            metadata=metadata,
            permissions=permissions,
        )
        self.assertIsNotNone(post)
        return post

    def test_create_repost_creates_reference_wrapper_without_copying_original_payload(self) -> None:
        original = self._create_feed_post(
            visibility=PostVisibility.NETWORK,
            content='Keyboard Hero run with module surface',
            metadata={
                'attachments': [{'id': 'F1', 'name': 'hero.png', 'type': 'image/png'}],
                'source_layout': {'version': 1, 'deck': {'default_ref': 'attachment:F1'}},
            },
        )

        repost = self.manager.create_repost(original.id, 'reposter', 'Keep this in circulation')
        self.assertIsNotNone(repost)
        self.assertEqual(repost.visibility, PostVisibility.NETWORK)
        self.assertEqual(repost.post_type, PostType.TEXT)
        self.assertEqual(repost.content, 'Keep this in circulation')
        self.assertIsInstance(repost.metadata, dict)
        source_reference = repost.metadata.get('source_reference')
        self.assertEqual(source_reference.get('kind'), 'repost_v1')
        self.assertEqual(source_reference.get('source_id'), original.id)
        self.assertNotIn('shared_post_id', repost.metadata)
        self.assertNotIn('original_content', repost.metadata)
        self.assertNotIn('original_metadata', repost.metadata)
        self.assertNotIn('attachments', repost.metadata)

        original_row = self.manager.get_post(original.id)
        self.assertEqual(original_row.shares, 1)

    def test_resolve_repost_reference_sets_deck_flag_without_source_layout(self) -> None:
        """Repost cards should offer Deck when the antecedent is module/media-backed."""
        module_original = self._create_feed_post(
            visibility=PostVisibility.NETWORK,
            content='Module surface',
            metadata={
                'attachments': [
                    {'id': 'M1', 'name': 'App.canopy-module.html', 'type': 'text/html'},
                ],
            },
        )
        module_repost = self.manager.create_repost(module_original.id, 'reposter', 'Share module')
        self.assertIsNotNone(module_repost)
        resolved_mod = self.manager.resolve_repost_reference(module_repost, 'viewer')
        self.assertTrue(resolved_mod['available'])
        self.assertTrue(resolved_mod['has_source_layout'])

        video_original = self.manager.create_post(
            author_id='author',
            content='',
            post_type=PostType.VIDEO,
            visibility=PostVisibility.NETWORK,
            metadata={'video_url': 'https://example.com/clip.mp4'},
        )
        self.assertIsNotNone(video_original)
        video_repost = self.manager.create_repost(video_original.id, 'reposter', 'Share video')
        self.assertIsNotNone(video_repost)
        resolved_vid = self.manager.resolve_repost_reference(video_repost, 'viewer')
        self.assertTrue(resolved_vid['available'])
        self.assertTrue(resolved_vid['has_source_layout'])

        html_demo = self._create_feed_post(
            visibility=PostVisibility.NETWORK,
            content='Open the attachment below to try it.',
            metadata={
                'attachments': [
                    {
                        'id': 'H1',
                        'filename': 'stego-demo.html',
                        'type': 'application/octet-stream',
                    },
                ],
            },
        )
        html_repost = self.manager.create_repost(html_demo.id, 'reposter', 'Share demo')
        self.assertIsNotNone(html_repost)
        resolved_html = self.manager.resolve_repost_reference(html_repost, 'viewer')
        self.assertTrue(resolved_html['available'])
        self.assertTrue(
            resolved_html['has_source_layout'],
            'Plain .html attachments should flag deck UI (matches feed module card rules)',
        )

        id_only = self._create_feed_post(
            visibility=PostVisibility.NETWORK,
            content='See attachment',
            metadata={'attachments': [{'id': 'Fz9'}]},
        )
        id_repost = self.manager.create_repost(id_only.id, 'reposter', 'Share file')
        self.assertIsNotNone(id_repost)
        resolved_id = self.manager.resolve_repost_reference(id_repost, 'viewer')
        self.assertTrue(resolved_id['available'])
        self.assertTrue(
            resolved_id['has_source_layout'],
            'Any file attachment id should flag deck UI for repost parity with source card',
        )

    def test_generic_create_and_update_strip_forged_repost_metadata(self) -> None:
        created = self.manager.create_post(
            author_id='author',
            content='Attempted forged wrapper',
            post_type=PostType.TEXT,
            visibility=PostVisibility.NETWORK,
            metadata={
                'source_reference': {'kind': 'repost_v1', 'source_type': 'feed_post', 'source_id': 'ORIG-1'},
                'shared_post_id': 'ORIG-1',
                'original_content': 'copied',
                'repost_policy': 'deny',
                'source_layout': {'version': 1, 'deck': {'default_ref': 'attachment:F7'}},
            },
        )
        self.assertIsNotNone(created)
        self.assertEqual(created.metadata.get('repost_policy'), 'deny')
        self.assertIn('source_layout', created.metadata)
        self.assertNotIn('source_reference', created.metadata)
        self.assertNotIn('shared_post_id', created.metadata)
        self.assertNotIn('original_content', created.metadata)

        updated = self.manager.update_post(
            created.id,
            'author',
            'Edited forged wrapper attempt',
            metadata={
                'source_reference': {'kind': 'repost_v1', 'source_type': 'feed_post', 'source_id': 'ORIG-2'},
                'shared_post_id': 'ORIG-2',
                'repost_policy': 'same_scope',
            },
        )
        self.assertTrue(updated)
        refreshed = self.manager.get_post(created.id)
        self.assertEqual(refreshed.metadata.get('repost_policy'), 'same_scope')
        self.assertNotIn('source_reference', refreshed.metadata)
        self.assertNotIn('shared_post_id', refreshed.metadata)

    def test_create_repost_rejects_private_custom_policy_deny_and_repost_chains(self) -> None:
        private_post = self._create_feed_post(visibility=PostVisibility.PRIVATE, content='Private post')
        self.assertIsNone(self.manager.create_repost(private_post.id, 'reposter', 'nope'))

        custom_post = self._create_feed_post(
            visibility=PostVisibility.CUSTOM,
            permissions=['reposter'],
            content='Custom post',
        )
        self.assertIsNone(self.manager.create_repost(custom_post.id, 'reposter', 'still nope'))

        deny_post = self._create_feed_post(
            visibility=PostVisibility.NETWORK,
            content='Do not repost',
            metadata={'repost_policy': 'deny'},
        )
        self.assertIsNone(self.manager.create_repost(deny_post.id, 'reposter', 'blocked'))

        original = self._create_feed_post(visibility=PostVisibility.NETWORK, content='Chain seed')
        repost = self.manager.create_repost(original.id, 'reposter', 'first repost')
        self.assertIsNotNone(repost)
        self.assertIsNone(self.manager.create_repost(repost.id, 'viewer', 'chain attempt'))

    def test_resolve_repost_reference_degrades_when_original_disappears_or_access_changes(self) -> None:
        original = self._create_feed_post(visibility=PostVisibility.NETWORK, content='Source to lose')
        repost = self.manager.create_repost(original.id, 'reposter', 'remember this')
        self.assertIsNotNone(repost)

        resolved = self.manager.resolve_repost_reference(repost, 'viewer')
        self.assertTrue(resolved['available'])
        self.assertEqual(resolved['source_id'], original.id)

        self.conn.execute("DELETE FROM feed_posts WHERE id = ?", (original.id,))
        self.conn.commit()
        missing = self.manager.resolve_repost_reference(self.manager.get_post(repost.id), 'viewer')
        self.assertFalse(missing['available'])
        self.assertEqual(missing['unavailable_reason'], 'missing')

        second_original = self._create_feed_post(visibility=PostVisibility.NETWORK, content='Source to tighten')
        second_repost = self.manager.create_repost(second_original.id, 'reposter', 'watch access')
        self.assertIsNotNone(second_repost)
        self.conn.execute(
            "UPDATE feed_posts SET visibility = ?, metadata = ? WHERE id = ?",
            ('private', json.dumps({'repost_policy': 'same_scope'}), second_original.id),
        )
        self.conn.commit()
        access_changed = self.manager.resolve_repost_reference(self.manager.get_post(second_repost.id), 'viewer')
        self.assertFalse(access_changed['available'])
        self.assertEqual(access_changed['unavailable_reason'], 'access_changed')

    def test_feed_algorithm_filters_new_and_legacy_reposts_when_disabled(self) -> None:
        original = self._create_feed_post(visibility=PostVisibility.NETWORK, content='Original')
        repost = self.manager.create_repost(original.id, 'reposter', 'commentary')
        algo = FeedAlgorithm(show_reposts=False)
        self.assertEqual(algo.score_post(repost, 'viewer'), -1.0)

        self.conn.execute(
            """
            INSERT INTO feed_posts (id, author_id, content, content_type, visibility, metadata, created_at, expires_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                'legacy-share',
                'reposter',
                'legacy share',
                'text',
                'network',
                json.dumps({'shared_post_id': original.id, 'original_content': 'copied'}),
                datetime.now(timezone.utc).isoformat(),
                None,
            ),
        )
        self.conn.commit()
        legacy_post = self.manager.get_post('legacy-share')
        self.assertEqual(algo.score_post(legacy_post, 'viewer'), -1.0)


class TestFeedRepostApi(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        db_path = Path(self.tempdir.name) / 'repost_v1_api.db'
        self.conn = sqlite3.connect(str(db_path))
        self.conn.row_factory = sqlite3.Row
        _create_schema(self.conn)
        self.db_manager = _FakeDbManager(self.conn, db_path)
        self.feed_manager = FeedManager(self.db_manager, MagicMock())
        self.profile_manager = MagicMock()
        self.profile_manager.get_profile.return_value = None

        self.public_post = self.feed_manager.create_post(
            author_id='author',
            content='Replayable source',
            post_type=PostType.TEXT,
            visibility=PostVisibility.NETWORK,
            metadata={'source_layout': {'version': 1, 'deck': {'default_ref': 'attachment:F1'}}},
        )
        self.private_post = self.feed_manager.create_post(
            author_id='author',
            content='Private source',
            post_type=PostType.TEXT,
            visibility=PostVisibility.PRIVATE,
        )

        self.api_key_manager = _FakeApiKeyManager({
            'writer-key': ApiKeyInfo(
                id='key-writer',
                user_id='reposter',
                key_hash='hash-writer',
                permissions={Permission.READ_FEED, Permission.WRITE_FEED},
                created_at=datetime.now(timezone.utc),
            ),
        })
        components = (
            self.db_manager,
            self.api_key_manager,
            MagicMock(),
            MagicMock(),
            MagicMock(),
            MagicMock(),
            self.feed_manager,
            MagicMock(),
            self.profile_manager,
            MagicMock(),
            MagicMock(),
        )

        self.get_components_patcher = patch('canopy.api.routes.get_app_components', return_value=components)
        self.get_components_patcher.start()
        self.addCleanup(self.get_components_patcher.stop)

        app = Flask(__name__)
        app.config['TESTING'] = True
        app.secret_key = 'repost-api-secret'
        app.register_blueprint(create_api_blueprint(), url_prefix='/api/v1')
        self.client = app.test_client()

    def tearDown(self) -> None:
        self.conn.close()

    def test_repost_endpoint_creates_wrapper_and_feed_get_returns_resolved_reference(self) -> None:
        response = self.client.post(
            f'/api/v1/feed/posts/{self.public_post.id}/repost',
            json={'comment': 'Bring this back'},
            headers={'X-API-Key': 'writer-key'},
        )
        self.assertEqual(response.status_code, 201)
        payload = response.get_json() or {}
        post = payload.get('post') or {}
        self.assertTrue(post.get('is_repost'))
        self.assertEqual(post.get('visibility'), 'network')
        self.assertEqual(post.get('content'), 'Bring this back')
        ref = post.get('repost_reference') or {}
        self.assertEqual(ref.get('source_id'), self.public_post.id)
        self.assertTrue(ref.get('available'))
        self.assertEqual(ref.get('href'), f'/feed?focus_post={self.public_post.id}')
        self.assertEqual(ref.get('body_text'), 'Replayable source')
        self.assertFalse(ref.get('body_truncated'))
        self.assertIsInstance(ref.get('embed'), dict)

        repost_id = post.get('id')
        fetched = self.client.get(
            f'/api/v1/feed/posts/{repost_id}',
            headers={'X-API-Key': 'writer-key'},
        )
        self.assertEqual(fetched.status_code, 200)
        fetched_payload = fetched.get_json() or {}
        self.assertTrue(fetched_payload.get('post', {}).get('is_repost'))
        self.assertTrue(fetched_payload.get('post', {}).get('repost_reference', {}).get('available'))

    def test_repost_endpoint_rejects_private_source(self) -> None:
        response = self.client.post(
            f'/api/v1/feed/posts/{self.private_post.id}/repost',
            json={'comment': 'Should fail'},
            headers={'X-API-Key': 'writer-key'},
        )
        self.assertEqual(response.status_code, 403)
        payload = response.get_json() or {}
        self.assertIn('Access denied', payload.get('error', ''))


if __name__ == '__main__':
    unittest.main()
