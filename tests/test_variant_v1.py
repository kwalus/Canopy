"""Security and regression coverage for feed variant v1."""

from __future__ import annotations

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
from canopy.core.feed import FeedManager, PostType, PostVisibility
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
            ('variant-maker', 'variantmaker', 'Variant Maker', 'human', 'active', None, '2026-03-23T08:01:00+00:00'),
            ('viewer', 'viewer', 'Viewer', 'agent', 'active', None, '2026-03-23T08:02:00+00:00'),
        ],
    )
    conn.commit()


class TestFeedVariantManager(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        db_path = Path(self.tempdir.name) / 'variant_v1.db'
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

    def test_create_variant_creates_provenance_wrapper_without_copying_original_payload(self) -> None:
        original = self._create_feed_post(
            visibility=PostVisibility.NETWORK,
            content='Keyboard Hero Studio',
            metadata={
                'attachments': [{'id': 'F1', 'name': 'hero.png', 'type': 'image/png'}],
                'source_layout': {'version': 1, 'deck': {'default_ref': 'attachment:F1'}},
            },
        )

        variant = self.manager.create_variant(
            original.id,
            'variant-maker',
            'Tighter right-hand ladder',
            relationship_kind='module_variant',
            module_param_delta='tempo=132; loop=bars 5-8',
        )
        self.assertIsNotNone(variant)
        assert variant is not None
        self.assertEqual(variant.visibility, PostVisibility.NETWORK)
        self.assertEqual(variant.content, 'Tighter right-hand ladder')
        self.assertIsInstance(variant.metadata, dict)
        source_reference = variant.metadata.get('source_reference')
        self.assertEqual(source_reference.get('kind'), 'variant_v1')
        self.assertEqual(source_reference.get('source_id'), original.id)
        self.assertEqual(source_reference.get('relationship_kind'), 'module_variant')
        self.assertEqual(source_reference.get('module_param_delta'), 'tempo=132; loop=bars 5-8')
        self.assertNotIn('attachments', variant.metadata)

        resolved = self.manager.resolve_variant_reference(variant, 'viewer')
        self.assertIsNotNone(resolved)
        assert resolved is not None
        self.assertTrue(resolved['available'])
        self.assertEqual(resolved['source_id'], original.id)
        self.assertEqual(resolved['relationship_kind'], 'module_variant')
        self.assertEqual(resolved['module_param_delta'], 'tempo=132; loop=bars 5-8')
        self.assertEqual(resolved['deck_default_ref'], 'attachment:F1')

    def test_generic_create_strips_forged_variant_and_update_preserves_existing_provenance(self) -> None:
        created = self.manager.create_post(
            author_id='author',
            content='Attempted forged variant',
            post_type=PostType.TEXT,
            visibility=PostVisibility.NETWORK,
            metadata={
                'source_reference': {
                    'kind': 'variant_v1',
                    'source_type': 'feed_post',
                    'source_id': 'ORIG-1',
                    'relationship_kind': 'module_variant',
                },
                'source_layout': {'version': 1},
            },
        )
        self.assertIsNotNone(created)
        assert created is not None
        self.assertNotIn('source_reference', created.metadata or {})

        original = self._create_feed_post(visibility=PostVisibility.NETWORK, content='Original source')
        variant = self.manager.create_variant(original.id, 'variant-maker', 'First variant')
        self.assertIsNotNone(variant)
        assert variant is not None

        updated = self.manager.update_post(variant.id, 'variant-maker', 'Edited variant note')
        self.assertTrue(updated)
        refreshed = self.manager.get_post(variant.id)
        self.assertIsNotNone(refreshed)
        assert refreshed is not None
        self.assertEqual(refreshed.content, 'Edited variant note')
        self.assertEqual(refreshed.metadata.get('source_reference', {}).get('kind'), 'variant_v1')
        self.assertEqual(refreshed.metadata.get('source_reference', {}).get('source_id'), original.id)

    def test_variant_rejects_private_custom_policy_deny_and_repost_wrappers(self) -> None:
        private_post = self._create_feed_post(visibility=PostVisibility.PRIVATE, content='Private source')
        self.assertIsNone(self.manager.create_variant(private_post.id, 'variant-maker', 'nope'))

        custom_post = self._create_feed_post(
            visibility=PostVisibility.CUSTOM,
            permissions=['variant-maker'],
            content='Custom post',
        )
        self.assertIsNone(self.manager.create_variant(custom_post.id, 'variant-maker', 'still nope'))

        deny_post = self._create_feed_post(
            visibility=PostVisibility.NETWORK,
            content='Do not variant me',
            metadata={'repost_policy': 'deny'},
        )
        self.assertIsNone(self.manager.create_variant(deny_post.id, 'variant-maker', 'blocked'))

        original = self._create_feed_post(visibility=PostVisibility.NETWORK, content='Chain seed')
        repost = self.manager.create_repost(original.id, 'variant-maker', 'first repost')
        self.assertIsNotNone(repost)
        assert repost is not None
        self.assertIsNone(self.manager.create_variant(repost.id, 'viewer', 'chain attempt'))

    def test_variant_reference_degrades_when_original_disappears_or_access_changes(self) -> None:
        original = self._create_feed_post(visibility=PostVisibility.NETWORK, content='Source to lose')
        variant = self.manager.create_variant(original.id, 'variant-maker', 'remember this')
        self.assertIsNotNone(variant)
        assert variant is not None

        self.conn.execute("DELETE FROM feed_posts WHERE id = ?", (original.id,))
        self.conn.commit()
        missing = self.manager.resolve_variant_reference(self.manager.get_post(variant.id), 'viewer')
        self.assertFalse(missing['available'])
        self.assertEqual(missing['unavailable_reason'], 'missing')

        second_original = self._create_feed_post(visibility=PostVisibility.NETWORK, content='Source to tighten')
        second_variant = self.manager.create_variant(second_original.id, 'variant-maker', 'watch access')
        self.assertIsNotNone(second_variant)
        self.conn.execute(
            "UPDATE feed_posts SET visibility = ?, metadata = ? WHERE id = ?",
            ('private', '{"repost_policy":"same_scope"}', second_original.id),
        )
        self.conn.commit()
        access_changed = self.manager.resolve_variant_reference(self.manager.get_post(second_variant.id), 'viewer')
        self.assertFalse(access_changed['available'])
        self.assertEqual(access_changed['unavailable_reason'], 'access_changed')


class TestFeedVariantApi(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        db_path = Path(self.tempdir.name) / 'variant_v1_api.db'
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
                user_id='variant-maker',
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
        app.secret_key = 'variant-api-secret'
        app.register_blueprint(create_api_blueprint(), url_prefix='/api/v1')
        self.client = app.test_client()

    def tearDown(self) -> None:
        self.conn.close()

    def test_variant_endpoint_creates_wrapper_and_feed_get_returns_resolved_reference(self) -> None:
        response = self.client.post(
            f'/api/v1/feed/posts/{self.public_post.id}/variant',
            json={
                'comment': 'Lighter intro loop',
                'relationship_kind': 'parameterized_variant',
                'module_param_delta': 'loop=intro; density=medium',
            },
            headers={'X-API-Key': 'writer-key'},
        )
        self.assertEqual(response.status_code, 201)
        payload = response.get_json() or {}
        post = payload.get('post') or {}
        self.assertTrue(post.get('is_variant'))
        self.assertEqual(post.get('content'), 'Lighter intro loop')
        ref = post.get('variant_reference') or {}
        self.assertEqual(ref.get('source_id'), self.public_post.id)
        self.assertTrue(ref.get('available'))
        self.assertEqual(ref.get('relationship_kind'), 'parameterized_variant')
        self.assertEqual(ref.get('module_param_delta'), 'loop=intro; density=medium')
        self.assertEqual(ref.get('href'), f'/feed?focus_post={self.public_post.id}')

        variant_id = post.get('id')
        fetched = self.client.get(
            f'/api/v1/feed/posts/{variant_id}',
            headers={'X-API-Key': 'writer-key'},
        )
        self.assertEqual(fetched.status_code, 200)
        fetched_payload = fetched.get_json() or {}
        self.assertTrue(fetched_payload.get('post', {}).get('is_variant'))
        self.assertTrue(fetched_payload.get('post', {}).get('variant_reference', {}).get('available'))

    def test_variant_endpoint_rejects_private_source(self) -> None:
        response = self.client.post(
            f'/api/v1/feed/posts/{self.private_post.id}/variant',
            json={'comment': 'Should fail'},
            headers={'X-API-Key': 'writer-key'},
        )
        self.assertEqual(response.status_code, 403)
        payload = response.get_json() or {}
        self.assertIn('Access denied', payload.get('error', ''))


if __name__ == '__main__':
    unittest.main()
