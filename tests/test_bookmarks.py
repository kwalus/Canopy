"""Tests for local-first personal bookmarks."""

import json
import os
import sqlite3
import sys
import tempfile
import types
import unittest
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

from canopy.core.bookmarks import BookmarkManager
from canopy.core.feed import Post, PostType, PostVisibility
from canopy.api.routes import create_api_blueprint
from canopy.security.api_keys import ApiKeyInfo, Permission
from canopy.ui.routes import create_ui_blueprint


class _FakeDbManager:
    def __init__(self, conn: sqlite3.Connection, db_path: Path) -> None:
        self._conn = conn
        self.db_path = db_path

    def get_connection(self) -> sqlite3.Connection:
        return self._conn

    def get_user(self, user_id: str):
        row = self._conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return dict(row) if row else None

    def get_instance_owner_user_id(self):
        return 'owner'


class _FakeFeedManager:
    def __init__(self, posts: dict[str, Post]) -> None:
        self._posts = posts

    def get_post(self, post_id: str):
        return self._posts.get(post_id)


class _FakeChannelManager:
    def __init__(self, allowed_channel_ids: set[str]) -> None:
        self.allowed_channel_ids = set(allowed_channel_ids)

    def get_channel_access_decision(self, channel_id: str, user_id: str, require_membership: bool = True):
        allowed = channel_id in self.allowed_channel_ids
        return {
            'allowed': allowed,
            'reason': 'ok' if allowed else 'not_member',
            'role': 'member' if allowed else None,
        }


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


class TestBookmarkManager(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        db_path = Path(self.tempdir.name) / 'bookmarks.db'
        self.conn = sqlite3.connect(str(db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(
            """
            CREATE TABLE users (
                id TEXT PRIMARY KEY,
                username TEXT,
                display_name TEXT,
                origin_peer TEXT
            )
            """
        )
        self.conn.execute(
            "INSERT INTO users (id, username, display_name, origin_peer) VALUES (?, ?, ?, ?)",
            ('owner', 'owner', 'Owner', None),
        )
        self.conn.commit()
        self.db_manager = _FakeDbManager(self.conn, db_path)
        self.manager = BookmarkManager(self.db_manager)

    def tearDown(self) -> None:
        self.conn.close()

    def test_upsert_touch_and_remove_bookmark(self) -> None:
        created = self.manager.upsert_bookmark(
            user_id='owner',
            source_type='feed_post',
            source_id='POST-1',
            source_href='/feed?focus_post=POST-1',
            title='Piano Lab',
            preview='A saved lesson',
            snapshot={'source_label': 'Feed post'},
            source_layout={'version': 1, 'deck': {'default_ref': 'attachment:F1'}},
            hero_ref='attachment:F0',
            deck_default_ref='attachment:F1',
            container_type='feed',
            source_author_id='owner',
        )
        self.assertEqual(created['source_type'], 'feed_post')
        self.assertEqual(created['source_id'], 'POST-1')
        self.assertEqual(created['deck_default_ref'], 'attachment:F1')
        self.assertEqual(created['snapshot']['source_label'], 'Feed post')

        mapping = self.manager.get_bookmark_map('owner', [('feed_post', 'POST-1')])
        self.assertIn(('feed_post', 'POST-1'), mapping)

        self.assertTrue(self.manager.touch_bookmark_opened(created['id'], 'owner'))
        touched = self.manager.get_bookmark(created['id'], 'owner')
        self.assertIsNotNone(touched['last_opened_at'])

        self.assertTrue(self.manager.remove_bookmark_for_source('owner', 'feed_post', 'POST-1'))
        self.assertIsNone(self.manager.get_bookmark_for_source('owner', 'feed_post', 'POST-1'))


class TestBookmarkRoutes(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        db_path = Path(self.tempdir.name) / 'bookmarks_routes.db'
        self.conn = sqlite3.connect(str(db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            """
            CREATE TABLE users (
                id TEXT PRIMARY KEY,
                username TEXT,
                display_name TEXT,
                avatar_file_id TEXT,
                account_type TEXT,
                origin_peer TEXT,
                created_at TEXT
            );
            CREATE TABLE channels (
                id TEXT PRIMARY KEY,
                name TEXT,
                channel_type TEXT,
                privacy_mode TEXT
            );
            CREATE TABLE channel_messages (
                id TEXT PRIMARY KEY,
                channel_id TEXT,
                user_id TEXT,
                content TEXT,
                message_type TEXT,
                created_at TEXT,
                attachments TEXT,
                source_layout TEXT
            );
            CREATE TABLE messages (
                id TEXT PRIMARY KEY,
                sender_id TEXT,
                recipient_id TEXT,
                content TEXT,
                message_type TEXT,
                status TEXT,
                created_at TEXT,
                delivered_at TEXT,
                read_at TEXT,
                edited_at TEXT,
                metadata TEXT
            );
            """
        )
        self.conn.executemany(
            "INSERT INTO users (id, username, display_name, avatar_file_id, account_type, origin_peer, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                ('owner', 'owner', 'Owner', None, 'human', None, '2026-03-23T08:00:00+00:00'),
                ('author', 'author', 'Author', None, 'human', None, '2026-03-23T08:01:00+00:00'),
                ('peer-a', 'peer_a', 'Alice', None, 'human', None, '2026-03-23T08:02:00+00:00'),
            ],
        )
        self.conn.execute(
            "INSERT INTO channels (id, name, channel_type, privacy_mode) VALUES (?, ?, ?, ?)",
            ('CHAN-1', 'testing', 'public', 'open'),
        )
        self.conn.execute(
            """
            INSERT INTO channel_messages (id, channel_id, user_id, content, message_type, created_at, attachments, source_layout)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                'MSG-1',
                'CHAN-1',
                'author',
                'Keyboard Hero showcase run',
                'text',
                '2026-03-23T10:00:00+00:00',
                json.dumps([{'id': 'F1', 'name': 'hero.png', 'type': 'image/png'}]),
                json.dumps({'version': 1, 'hero': {'ref': 'attachment:F1', 'label': 'Keyboard Hero'}, 'deck': {'default_ref': 'attachment:F1'}}),
            ),
        )
        self.conn.commit()

        self.db_manager = _FakeDbManager(self.conn, db_path)
        self.bookmark_manager = BookmarkManager(self.db_manager)
        self.feed_post = Post(
            id='POST-1',
            author_id='author',
            content='Canopy Module piano practice session',
            post_type=PostType.TEXT,
            visibility=PostVisibility.NETWORK,
            created_at=__import__('datetime').datetime.fromisoformat('2026-03-23T09:00:00+00:00'),
            metadata={
                'attachments': [{'id': 'F2', 'name': 'score.png', 'type': 'image/png'}],
                'source_layout': {
                    'version': 1,
                    'hero': {'ref': 'attachment:F2', 'label': 'Piano Lab'},
                    'deck': {'default_ref': 'attachment:F2'},
                },
            },
        )
        self.feed_manager = _FakeFeedManager({'POST-1': self.feed_post})
        self.channel_manager = _FakeChannelManager({'CHAN-1'})
        self.profile_manager = MagicMock()
        self.profile_manager.get_profile.return_value = None

        components = (
            self.db_manager,
            MagicMock(),
            MagicMock(),
            MagicMock(),
            self.channel_manager,
            MagicMock(),
            self.feed_manager,
            MagicMock(),
            self.profile_manager,
            MagicMock(),
            MagicMock(),
        )

        self.get_components_patcher = patch('canopy.ui.routes.get_app_components', return_value=components)
        self.get_components_patcher.start()
        self.addCleanup(self.get_components_patcher.stop)

        app = Flask(__name__)
        app.config['TESTING'] = True
        app.secret_key = 'bookmark-secret'
        app.config['BOOKMARK_MANAGER'] = self.bookmark_manager
        app.register_blueprint(create_ui_blueprint())
        self.app = app
        self.client = app.test_client()
        self._set_authenticated_session()

    def tearDown(self) -> None:
        self.conn.close()

    def _set_authenticated_session(self) -> None:
        with self.client.session_transaction() as sess:
            sess['authenticated'] = True
            sess['user_id'] = 'owner'
            sess['username'] = 'owner'
            sess['display_name'] = 'Owner'
            sess['_csrf_token'] = 'csrf-bookmarks'

    def test_toggle_feed_post_bookmark_roundtrip(self) -> None:
        response = self.client.post(
            '/ajax/bookmarks/toggle',
            json={'source_type': 'feed_post', 'source_id': 'POST-1'},
            headers={'X-CSRFToken': 'csrf-bookmarks'},
        )
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload['bookmarked'])
        saved = self.bookmark_manager.get_bookmark_for_source('owner', 'feed_post', 'POST-1')
        self.assertIsNotNone(saved)
        self.assertEqual(saved['title'], 'Piano Lab')
        self.assertEqual(saved['source_href'], '/feed?focus_post=POST-1')

        response = self.client.post(
            '/ajax/bookmarks/toggle',
            json={'source_type': 'feed_post', 'source_id': 'POST-1'},
            headers={'X-CSRFToken': 'csrf-bookmarks'},
        )
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertFalse(payload['bookmarked'])
        self.assertIsNone(self.bookmark_manager.get_bookmark_for_source('owner', 'feed_post', 'POST-1'))

    def test_bookmarks_page_and_open_route(self) -> None:
        response = self.client.post(
            '/ajax/bookmarks/toggle',
            json={'source_type': 'channel_message', 'source_id': 'MSG-1'},
            headers={'X-CSRFToken': 'csrf-bookmarks'},
        )
        self.assertEqual(response.status_code, 200)
        bookmark = self.bookmark_manager.get_bookmark_for_source('owner', 'channel_message', 'MSG-1')
        self.assertIsNotNone(bookmark)

        page = self.client.get('/bookmarks')
        self.assertEqual(page.status_code, 200)
        body = page.get_data(as_text=True)
        self.assertIn('Bookmarks', body)
        self.assertIn('Nothing in this view is mesh-broadcast.', body)
        self.assertIn('Keyboard Hero', body)

        open_response = self.client.get(f"/bookmarks/open/{bookmark['id']}")
        self.assertEqual(open_response.status_code, 302)
        self.assertIn('/channels/locate?message_id=MSG-1', open_response.headers['Location'])
        reopened = self.bookmark_manager.get_bookmark(bookmark['id'], 'owner')
        self.assertIsNotNone(reopened['last_opened_at'])

class TestBookmarkApiRoutes(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        db_path = Path(self.tempdir.name) / 'bookmarks_api.db'
        self.conn = sqlite3.connect(str(db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            """
            CREATE TABLE users (
                id TEXT PRIMARY KEY,
                username TEXT,
                display_name TEXT,
                avatar_file_id TEXT,
                account_type TEXT,
                status TEXT,
                origin_peer TEXT,
                created_at TEXT
            );
            CREATE TABLE channels (
                id TEXT PRIMARY KEY,
                name TEXT,
                channel_type TEXT,
                privacy_mode TEXT
            );
            CREATE TABLE channel_messages (
                id TEXT PRIMARY KEY,
                channel_id TEXT,
                user_id TEXT,
                content TEXT,
                message_type TEXT,
                created_at TEXT,
                attachments TEXT,
                source_layout TEXT
            );
            CREATE TABLE messages (
                id TEXT PRIMARY KEY,
                sender_id TEXT,
                recipient_id TEXT,
                content TEXT,
                message_type TEXT,
                status TEXT,
                created_at TEXT,
                delivered_at TEXT,
                read_at TEXT,
                edited_at TEXT,
                metadata TEXT
            );
            """
        )
        self.conn.executemany(
            "INSERT INTO users (id, username, display_name, avatar_file_id, account_type, status, origin_peer, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                ('owner', 'owner', 'Owner', None, 'agent', 'active', None, '2026-03-23T08:00:00+00:00'),
                ('admin', 'admin', 'Admin', None, 'human', 'active', None, '2026-03-23T08:01:00+00:00'),
                ('author', 'author', 'Author', None, 'human', 'active', None, '2026-03-23T08:02:00+00:00'),
                ('peer-a', 'peer_a', 'Alice', None, 'human', 'active', None, '2026-03-23T08:03:00+00:00'),
            ],
        )
        self.conn.execute(
            "INSERT INTO channels (id, name, channel_type, privacy_mode) VALUES (?, ?, ?, ?)",
            ('CHAN-1', 'testing', 'public', 'open'),
        )
        self.conn.execute(
            """
            INSERT INTO channel_messages (id, channel_id, user_id, content, message_type, created_at, attachments, source_layout)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                'MSG-1',
                'CHAN-1',
                'author',
                'Keyboard Hero showcase run',
                'text',
                '2026-03-23T10:00:00+00:00',
                json.dumps([{'id': 'F1', 'name': 'hero.png', 'type': 'image/png'}]),
                json.dumps({'version': 1, 'hero': {'ref': 'attachment:F1', 'label': 'Keyboard Hero'}, 'deck': {'default_ref': 'attachment:F1'}}),
            ),
        )
        self.conn.execute(
            """
            INSERT INTO messages (id, sender_id, recipient_id, content, message_type, status, created_at, delivered_at, read_at, edited_at, metadata)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                'DM-1',
                'author',
                'owner',
                'Private lesson brief',
                'text',
                'delivered',
                '2026-03-23T10:05:00+00:00',
                None,
                None,
                None,
                json.dumps({
                    'attachments': [{'id': 'F9', 'name': 'private.png', 'type': 'image/png'}],
                    'source_layout': {'version': 1, 'hero': {'ref': 'attachment:F9', 'label': 'Private Brief'}},
                }),
            ),
        )
        self.conn.commit()

        self.db_manager = _FakeDbManager(self.conn, db_path)
        self.bookmark_manager = BookmarkManager(self.db_manager)
        self.feed_post = Post(
            id='POST-1',
            author_id='author',
            content='Canopy Module piano practice session',
            post_type=PostType.TEXT,
            visibility=PostVisibility.NETWORK,
            created_at=datetime.fromisoformat('2026-03-23T09:00:00+00:00'),
            metadata={
                'attachments': [{'id': 'F2', 'name': 'score.png', 'type': 'image/png'}],
                'source_layout': {
                    'version': 1,
                    'hero': {'ref': 'attachment:F2', 'label': 'Piano Lab'},
                    'deck': {'default_ref': 'attachment:F2'},
                },
            },
        )
        self.feed_manager = _FakeFeedManager({'POST-1': self.feed_post})
        self.channel_manager = _FakeChannelManager({'CHAN-1'})
        self.profile_manager = MagicMock()
        self.profile_manager.get_profile.return_value = None

        self.api_key_manager = _FakeApiKeyManager({
            'agent-key': ApiKeyInfo(
                id='key-agent',
                user_id='owner',
                key_hash='hash-agent',
                permissions={Permission.READ_FEED, Permission.READ_MESSAGES},
                created_at=datetime.now(timezone.utc),
            ),
            'feed-only-key': ApiKeyInfo(
                id='key-feed',
                user_id='owner',
                key_hash='hash-feed',
                permissions={Permission.READ_FEED},
                created_at=datetime.now(timezone.utc),
            ),
            'admin-key': ApiKeyInfo(
                id='key-admin',
                user_id='admin',
                key_hash='hash-admin',
                permissions={Permission.READ_FEED, Permission.READ_MESSAGES},
                created_at=datetime.now(timezone.utc),
            ),
        })

        components = (
            self.db_manager,
            self.api_key_manager,
            MagicMock(),
            MagicMock(),
            self.channel_manager,
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
        app.secret_key = 'bookmark-api-secret'
        app.config['BOOKMARK_MANAGER'] = self.bookmark_manager
        app.register_blueprint(create_api_blueprint(), url_prefix='/api/v1')
        self.app = app
        self.client = app.test_client()

    def tearDown(self) -> None:
        self.conn.close()

    def test_agent_can_crud_own_feed_bookmark(self) -> None:
        created = self.client.post(
            '/api/v1/bookmarks',
            json={
                'source_type': 'feed_post',
                'source_id': 'POST-1',
                'note': 'Replay this practice source',
                'tags': ['music', 'module'],
            },
            headers={'X-API-Key': 'agent-key'},
        )
        self.assertEqual(created.status_code, 201)
        payload = created.get_json() or {}
        bookmark = payload.get('bookmark') or {}
        self.assertEqual(bookmark.get('user_id'), 'owner')
        self.assertEqual(bookmark.get('source_type'), 'feed_post')
        self.assertEqual(bookmark.get('note'), 'Replay this practice source')
        self.assertEqual(bookmark.get('tags'), ['music', 'module'])

        bookmark_id = bookmark['id']
        fetched = self.client.get(
            f'/api/v1/bookmarks/{bookmark_id}',
            headers={'X-API-Key': 'agent-key'},
        )
        self.assertEqual(fetched.status_code, 200)

        updated = self.client.patch(
            f'/api/v1/bookmarks/{bookmark_id}',
            json={'note': 'Pinned for future context', 'tags': ['music', 'hero']},
            headers={'X-API-Key': 'agent-key'},
        )
        self.assertEqual(updated.status_code, 200)
        updated_payload = updated.get_json() or {}
        self.assertEqual(updated_payload.get('bookmark', {}).get('note'), 'Pinned for future context')
        self.assertEqual(updated_payload.get('bookmark', {}).get('tags'), ['music', 'hero'])

        listing = self.client.get('/api/v1/bookmarks', headers={'X-API-Key': 'agent-key'})
        self.assertEqual(listing.status_code, 200)
        listing_payload = listing.get_json() or {}
        self.assertEqual(listing_payload.get('count'), 1)

        deleted = self.client.delete(
            f'/api/v1/bookmarks/{bookmark_id}',
            headers={'X-API-Key': 'agent-key'},
        )
        self.assertEqual(deleted.status_code, 200)
        self.assertIsNone(self.bookmark_manager.get_bookmark(bookmark_id, 'owner'))

    def test_feed_only_key_cannot_see_dm_bookmarks(self) -> None:
        feed_bookmark = self.bookmark_manager.upsert_bookmark(
            user_id='owner',
            source_type='feed_post',
            source_id='POST-1',
            source_href='/feed?focus_post=POST-1',
            title='Piano Lab',
            preview='Saved practice source',
            snapshot={'source_label': 'Feed post'},
        )
        self.bookmark_manager.upsert_bookmark(
            user_id='owner',
            source_type='dm_message',
            source_id='DM-1',
            source_href='/messages?with=author#message-DM-1',
            title='Private Brief',
            preview='Sensitive DM',
            snapshot={'source_label': 'Direct message'},
        )

        listing = self.client.get('/api/v1/bookmarks', headers={'X-API-Key': 'feed-only-key'})
        self.assertEqual(listing.status_code, 200)
        payload = listing.get_json() or {}
        self.assertEqual(payload.get('count'), 1)
        self.assertEqual(payload.get('bookmarks', [])[0].get('id'), feed_bookmark.get('id'))

        dm_lookup = self.bookmark_manager.get_bookmark_for_source('owner', 'dm_message', 'DM-1')
        hidden = self.client.get(
            f"/api/v1/bookmarks/{dm_lookup['id']}",
            headers={'X-API-Key': 'feed-only-key'},
        )
        self.assertEqual(hidden.status_code, 404)

    def test_agent_cannot_fetch_admin_bookmark(self) -> None:
        admin_bookmark = self.bookmark_manager.upsert_bookmark(
            user_id='admin',
            source_type='channel_message',
            source_id='MSG-1',
            source_href='/channels/locate?message_id=MSG-1',
            title='Admin Save',
            preview='Should stay private',
            snapshot={'source_label': 'Channel message'},
        )

        response = self.client.get(
            f"/api/v1/bookmarks/{admin_bookmark['id']}",
            headers={'X-API-Key': 'agent-key'},
        )
        self.assertEqual(response.status_code, 404)


if __name__ == '__main__':
    unittest.main()
