"""Security and regression coverage for channel variant v1."""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import types
import unittest
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
from canopy.core.channels import ChannelManager, ChannelType
from canopy.security.api_keys import ApiKeyInfo, Permission


class _FakeDbManager:
    def __init__(self, conn: sqlite3.Connection, db_path: Path) -> None:
        self._conn = conn
        self.db_path = db_path

    def get_connection(self):
        return self._conn

    def get_user(self, user_id: str):
        row = self._conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return dict(row) if row else None

    def get_instance_owner_user_id(self):
        return 'owner-user'


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


class TestChannelVariantManager(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        db_path = Path(self.tempdir.name) / 'channel_variant_v1.db'
        self.conn = sqlite3.connect(str(db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(
            """
            CREATE TABLE users (
                id TEXT PRIMARY KEY,
                username TEXT,
                display_name TEXT,
                public_key TEXT,
                password_hash TEXT,
                account_type TEXT,
                origin_peer TEXT,
                status TEXT,
                created_at TEXT
            )
            """
        )
        self.conn.executemany(
            """
            INSERT INTO users (id, username, display_name, public_key, password_hash, account_type, origin_peer, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                ('owner-user', 'owner', 'Owner', 'pk-owner', 'hash-owner', 'human', None, 'active', '2026-03-23T08:00:00+00:00'),
                ('author-user', 'author', 'Author', 'pk-author', 'hash-author', 'human', None, 'active', '2026-03-23T08:01:00+00:00'),
                ('variant-user', 'variant', 'Variant Maker', 'pk-variant', 'hash-variant', 'agent', None, 'active', '2026-03-23T08:02:00+00:00'),
            ],
        )
        self.conn.commit()
        self.db_manager = _FakeDbManager(self.conn, db_path)
        self.manager = ChannelManager(self.db_manager, MagicMock())

        channel = self.manager.create_channel(
            name='testing',
            channel_type=ChannelType.PUBLIC,
            created_by='owner-user',
            description='variant test',
            privacy_mode='open',
        )
        assert channel is not None
        self.channel = channel
        self.assertTrue(self.manager.add_member(self.channel.id, 'variant-user', 'owner-user'))
        self.assertTrue(self.manager.add_member(self.channel.id, 'author-user', 'owner-user'))

    def tearDown(self) -> None:
        self.conn.close()

    def test_create_variant_creates_reference_wrapper_without_copying_original_payload(self) -> None:
        original = self.manager.send_message(
            channel_id=self.channel.id,
            user_id='author-user',
            content='Keyboard Hero Studio',
            attachments=[{'id': 'F1', 'name': 'hero.png', 'type': 'image/png'}],
            source_layout={'version': 1, 'deck': {'default_ref': 'attachment:F1'}},
        )
        self.assertIsNotNone(original)
        assert original is not None

        variant = self.manager.create_variant(
            original.id,
            'variant-user',
            self.channel.id,
            'Faster two-hand drill',
            relationship_kind='module_variant',
            module_param_delta='tempo=144; hand_split=wide',
        )
        self.assertIsNotNone(variant)
        assert variant is not None
        self.assertEqual(variant.content, 'Faster two-hand drill')
        self.assertIsNone(variant.attachments)
        self.assertIsInstance(variant.source_reference, dict)
        self.assertEqual(variant.source_reference.get('kind'), 'variant_v1')
        self.assertEqual(variant.source_reference.get('source_id'), original.id)
        self.assertEqual(variant.source_reference.get('channel_id'), self.channel.id)
        self.assertEqual(variant.source_reference.get('relationship_kind'), 'module_variant')
        self.assertEqual(variant.source_reference.get('module_param_delta'), 'tempo=144; hand_split=wide')

        resolved = self.manager.resolve_variant_reference(variant, 'variant-user')
        self.assertIsNotNone(resolved)
        assert resolved is not None
        self.assertTrue(resolved['available'])
        self.assertEqual(resolved['source_id'], original.id)
        self.assertEqual(resolved['relationship_kind'], 'module_variant')
        self.assertEqual(resolved['module_param_delta'], 'tempo=144; hand_split=wide')
        self.assertTrue(resolved['has_source_layout'])
        self.assertEqual(resolved['deck_default_ref'], 'attachment:F1')

    def test_generic_send_and_update_strip_forged_variant_reference(self) -> None:
        forged = self.manager.send_message(
            channel_id=self.channel.id,
            user_id='author-user',
            content='Attempted forged variant',
            source_reference={
                'kind': 'variant_v1',
                'source_type': 'channel_message',
                'source_id': 'ORIG-1',
                'channel_id': self.channel.id,
                'relationship_kind': 'module_variant',
            },
            allow_source_reference=False,
        )
        self.assertIsNotNone(forged)
        assert forged is not None
        self.assertIsNone(forged.source_reference)

        original = self.manager.send_message(self.channel.id, 'author-user', 'Original source')
        assert original is not None
        variant = self.manager.create_variant(original.id, 'variant-user', self.channel.id, 'Note')
        assert variant is not None

        updated = self.manager.update_message(
            variant.id,
            'variant-user',
            'Edited note',
            source_reference={
                'kind': 'variant_v1',
                'source_type': 'channel_message',
                'source_id': 'ORIG-2',
                'channel_id': self.channel.id,
            },
            allow_source_reference=False,
        )
        self.assertTrue(updated)
        refreshed = self.manager.get_channel_message(self.channel.id, variant.id, 'variant-user')
        self.assertIsNotNone(refreshed)
        assert refreshed is not None
        self.assertEqual(refreshed.source_reference.get('source_id'), original.id)
        self.assertEqual(refreshed.content, 'Edited note')

    def test_variant_rejects_cross_channel_policy_deny_and_repost_wrappers(self) -> None:
        second_channel = self.manager.create_channel(
            name='other-room',
            channel_type=ChannelType.PUBLIC,
            created_by='owner-user',
            description='other',
            privacy_mode='open',
        )
        assert second_channel is not None
        self.assertTrue(self.manager.add_member(second_channel.id, 'variant-user', 'owner-user'))
        self.assertTrue(self.manager.add_member(second_channel.id, 'author-user', 'owner-user'))

        original = self.manager.send_message(self.channel.id, 'author-user', 'Do not variant me')
        assert original is not None
        self.assertTrue(
            self.manager.update_message(original.id, 'author-user', original.content, repost_policy='deny')
        )
        self.assertIsNone(self.manager.create_variant(original.id, 'variant-user', self.channel.id, 'blocked'))

        allowed_source = self.manager.send_message(self.channel.id, 'author-user', 'Allowed source')
        assert allowed_source is not None
        self.assertIsNone(self.manager.create_variant(allowed_source.id, 'variant-user', second_channel.id, 'wrong channel'))

        first_repost = self.manager.create_repost(allowed_source.id, 'variant-user', self.channel.id, 'first repost')
        self.assertIsNotNone(first_repost)
        assert first_repost is not None
        self.assertIsNone(self.manager.create_variant(first_repost.id, 'author-user', self.channel.id, 'chain attempt'))

    def test_variant_reference_degrades_when_original_disappears_or_access_changes(self) -> None:
        original = self.manager.send_message(self.channel.id, 'author-user', 'Source to lose')
        assert original is not None
        variant = self.manager.create_variant(original.id, 'variant-user', self.channel.id, 'remember this')
        assert variant is not None

        self.assertTrue(self.manager.delete_message(self.channel.id, original.id, 'author-user'))
        missing = self.manager.resolve_variant_reference(
            self.manager.get_channel_message(self.channel.id, variant.id, 'variant-user'),
            'variant-user',
        )
        self.assertFalse(missing['available'])
        self.assertEqual(missing['unavailable_reason'], 'missing')

        second_original = self.manager.send_message(self.channel.id, 'author-user', 'Source to tighten')
        assert second_original is not None
        second_variant = self.manager.create_variant(second_original.id, 'variant-user', self.channel.id, 'watch access')
        assert second_variant is not None
        self.conn.execute(
            "DELETE FROM channel_members WHERE channel_id = ? AND user_id = ?",
            (self.channel.id, 'variant-user'),
        )
        self.conn.commit()
        access_changed = self.manager.resolve_variant_reference(second_variant, 'variant-user')
        self.assertFalse(access_changed['available'])
        self.assertEqual(access_changed['unavailable_reason'], 'access_changed')


class TestChannelVariantApi(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        db_path = Path(self.tempdir.name) / 'channel_variant_v1_api.db'
        self.conn = sqlite3.connect(str(db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(
            """
            CREATE TABLE users (
                id TEXT PRIMARY KEY,
                username TEXT,
                display_name TEXT,
                public_key TEXT,
                password_hash TEXT,
                account_type TEXT,
                origin_peer TEXT,
                status TEXT,
                created_at TEXT
            )
            """
        )
        self.conn.executemany(
            """
            INSERT INTO users (id, username, display_name, public_key, password_hash, account_type, origin_peer, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                ('owner-user', 'owner', 'Owner', 'pk-owner', 'hash-owner', 'human', None, 'active', '2026-03-23T08:00:00+00:00'),
                ('author-user', 'author', 'Author', 'pk-author', 'hash-author', 'human', None, 'active', '2026-03-23T08:01:00+00:00'),
                ('variant-user', 'variant', 'Variant Maker', 'pk-variant', 'hash-variant', 'agent', None, 'active', '2026-03-23T08:02:00+00:00'),
            ],
        )
        self.conn.commit()
        self.db_manager = _FakeDbManager(self.conn, db_path)
        self.channel_manager = ChannelManager(self.db_manager, MagicMock())
        channel = self.channel_manager.create_channel(
            name='testing',
            channel_type=ChannelType.PUBLIC,
            created_by='owner-user',
            description='api variant test',
            privacy_mode='open',
        )
        assert channel is not None
        self.channel = channel
        self.assertTrue(self.channel_manager.add_member(self.channel.id, 'author-user', 'owner-user'))
        self.assertTrue(self.channel_manager.add_member(self.channel.id, 'variant-user', 'owner-user'))
        self.original = self.channel_manager.send_message(
            channel_id=self.channel.id,
            user_id='author-user',
            content='Replayable source',
            source_layout={'version': 1, 'deck': {'default_ref': 'attachment:F1'}},
        )
        assert self.original is not None

        self.api_key_manager = _FakeApiKeyManager({
            'writer-key': ApiKeyInfo(
                id='key-writer',
                user_id='variant-user',
                key_hash='hash-writer',
                permissions={Permission.READ_MESSAGES, Permission.WRITE_MESSAGES, Permission.READ_FEED},
                created_at=self.original.created_at,
            ),
            'write-only-key': ApiKeyInfo(
                id='key-write-only',
                user_id='variant-user',
                key_hash='hash-write-only',
                permissions={Permission.WRITE_MESSAGES},
                created_at=self.original.created_at,
            ),
            'feed-key': ApiKeyInfo(
                id='key-feed',
                user_id='variant-user',
                key_hash='hash-feed',
                permissions={Permission.READ_FEED, Permission.WRITE_FEED},
                created_at=self.original.created_at,
            ),
        })
        self.profile_manager = MagicMock()
        self.profile_manager.get_profile.return_value = None
        components = (
            self.db_manager,
            self.api_key_manager,
            MagicMock(),
            MagicMock(),
            self.channel_manager,
            MagicMock(),
            MagicMock(),
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
        app.secret_key = 'channel-variant-secret'
        app.register_blueprint(create_api_blueprint(), url_prefix='/api/v1')
        self.client = app.test_client()

    def tearDown(self) -> None:
        self.conn.close()

    def test_channel_variant_endpoint_creates_wrapper_and_channel_get_returns_resolved_reference(self) -> None:
        response = self.client.post(
            f'/api/v1/channels/{self.channel.id}/messages/{self.original.id}/variant',
            json={
                'comment': 'Neon ladder variant',
                'relationship_kind': 'module_variant',
                'module_param_delta': 'lane_map=split; tempo=138',
            },
            headers={'X-API-Key': 'writer-key'},
        )
        self.assertEqual(response.status_code, 201)
        payload = response.get_json() or {}
        message = payload.get('message') or {}
        self.assertTrue(message.get('is_variant'))
        self.assertEqual(message.get('content'), 'Neon ladder variant')
        ref = message.get('variant_reference') or {}
        self.assertEqual(ref.get('source_id'), self.original.id)
        self.assertEqual(ref.get('channel_id'), self.channel.id)
        self.assertTrue(ref.get('available'))
        self.assertEqual(ref.get('relationship_kind'), 'module_variant')
        self.assertEqual(ref.get('module_param_delta'), 'lane_map=split; tempo=138')
        self.assertEqual(ref.get('href'), f'/channels/locate?message_id={self.original.id}')

        variant_id = message.get('id')
        fetched = self.client.get(
            f'/api/v1/channels/{self.channel.id}/messages/{variant_id}',
            headers={'X-API-Key': 'writer-key'},
        )
        self.assertEqual(fetched.status_code, 200)
        fetched_payload = fetched.get_json() or {}
        self.assertTrue(fetched_payload.get('message', {}).get('is_variant'))
        self.assertTrue(fetched_payload.get('message', {}).get('variant_reference', {}).get('available'))

    def test_channel_variant_endpoint_rejects_wrong_channel(self) -> None:
        other = self.channel_manager.create_channel(
            name='other',
            channel_type=ChannelType.PUBLIC,
            created_by='owner-user',
            description='other room',
            privacy_mode='open',
        )
        assert other is not None
        self.assertTrue(self.channel_manager.add_member(other.id, 'variant-user', 'owner-user'))
        response = self.client.post(
            f'/api/v1/channels/{other.id}/messages/{self.original.id}/variant',
            json={'comment': 'Should fail'},
            headers={'X-API-Key': 'writer-key'},
        )
        self.assertEqual(response.status_code, 403)
        payload = response.get_json() or {}
        self.assertIn('same channel', payload.get('error', ''))

    def test_channel_variant_endpoint_requires_message_permissions(self) -> None:
        write_only = self.client.post(
            f'/api/v1/channels/{self.channel.id}/messages/{self.original.id}/variant',
            json={'comment': 'Needs read permission too'},
            headers={'X-API-Key': 'write-only-key'},
        )
        self.assertEqual(write_only.status_code, 403)
        self.assertEqual((write_only.get_json() or {}).get('error'), 'READ_MESSAGES permission required')

        wrong_surface = self.client.post(
            f'/api/v1/channels/{self.channel.id}/messages/{self.original.id}/variant',
            json={'comment': 'Feed permissions should not work here'},
            headers={'X-API-Key': 'feed-key'},
        )
        self.assertEqual(wrong_surface.status_code, 403)
        self.assertEqual((wrong_surface.get_json() or {}).get('error'), 'Invalid or insufficient permissions')


if __name__ == '__main__':
    unittest.main()
