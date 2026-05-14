"""Regression tests for File Vault Digestions."""

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
from canopy.core.digestions import DigestionManager
from canopy.core.files import FileManager
from canopy.security.api_keys import ApiKeyInfo, Permission


class _FakeDbManager:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    @contextmanager
    def get_connection(self, *args, **kwargs):
        yield self.conn

    def get_instance_owner_user_id(self):
        return 'owner-user'

    def get_user(self, user_id):
        return {'id': user_id, 'origin_peer': None, 'username': user_id}


class _FakeApiKeyManager:
    def validate_key(self, raw_key: str, required_permission=None):
        key_perms = {
            'owner-key': {Permission.READ_FILES, Permission.WRITE_FILES},
            'reader-key': {Permission.READ_FILES},
            'other-key': {Permission.READ_FILES},
        }
        key_users = {
            'owner-key': 'owner-user',
            'reader-key': 'reader-user',
            'other-key': 'other-user',
        }
        perms = key_perms.get(raw_key)
        if not perms:
            return None
        if required_permission and required_permission not in perms:
            return None
        return ApiKeyInfo(
            id=f'key-{raw_key}',
            user_id=key_users[raw_key],
            key_hash='hash',
            permissions=perms,
            created_at=datetime.now(timezone.utc),
        )


class TestDigestions(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("CREATE TABLE users (id TEXT PRIMARY KEY, avatar_file_id TEXT, origin_peer TEXT, username TEXT)")
        for user_id in ('owner-user', 'reader-user', 'other-user'):
            self.conn.execute("INSERT INTO users (id, username) VALUES (?, ?)", (user_id, user_id))
        self.conn.execute("CREATE TABLE channel_messages (id TEXT PRIMARY KEY, attachments TEXT, content TEXT)")
        self.conn.execute("CREATE TABLE feed_posts (id TEXT PRIMARY KEY, metadata TEXT, content TEXT)")
        self.conn.execute("CREATE TABLE messages (id TEXT PRIMARY KEY, metadata TEXT, content TEXT)")
        self.conn.commit()

        self.db_manager = _FakeDbManager(self.conn)
        self.file_manager = FileManager(self.db_manager, str(Path(self.tempdir.name) / 'files'))
        self.digestion_manager = DigestionManager(self.db_manager, self.file_manager)

    def tearDown(self) -> None:
        self.conn.close()

    def _save_text(self, name: str, content: str, owner: str = 'owner-user'):
        info = self.file_manager.save_file(
            content.encode('utf-8'),
            name,
            'text/plain',
            owner,
        )
        self.assertIsNotNone(info)
        return info

    def test_local_hash_digestion_builds_and_queries_owned_vault_files(self) -> None:
        silicon = self._save_text(
            'silicon-notes.txt',
            'Quantum silicon devices need careful surface preparation and hyperfine control. '
            'SiDB circuits can be compared with other storage media.',
        )
        unrelated = self._save_text(
            'gardening.txt',
            'Tomatoes need light, water, and soil nutrients.',
        )

        digestion = self.digestion_manager.create_digestion(
            'owner-user',
            name='Lab references',
            source_file_ids=[silicon.id, unrelated.id],
            provider='local_hash',
            chunk_size=320,
            chunk_overlap=20,
        )
        build = self.digestion_manager.build_digestion(digestion['id'], 'owner-user')
        self.assertTrue(build['success'])
        self.assertGreaterEqual(build['chunk_count'], 2)

        result = self.digestion_manager.query(digestion['id'], 'owner-user', 'quantum silicon hyperfine', top_k=3)
        self.assertTrue(result['success'])
        self.assertGreaterEqual(result['result_count'], 1)
        self.assertEqual(result['results'][0]['file_name'], 'silicon-notes.txt')
        self.assertIn('hyperfine', result['results'][0]['snippet'])

    def test_digestion_acl_allows_reader_without_vault_file_ownership(self) -> None:
        source = self._save_text('private-corpus.txt', 'Agent-safe retrieval can cite snippets without granting raw vault access.')
        digestion = self.digestion_manager.create_digestion(
            'owner-user',
            name='Private corpus',
            source_file_ids=[source.id],
            provider='local_hash',
        )
        self.digestion_manager.build_digestion(digestion['id'], 'owner-user')
        self.digestion_manager.grant_access(digestion['id'], 'owner-user', 'reader-user', can_query=True)

        reader_items = self.digestion_manager.list_digestions('reader-user')
        self.assertEqual(len(reader_items), 1)
        reader_items_with_sources = self.digestion_manager.list_digestions('reader-user', include_sources=True)
        self.assertEqual(reader_items_with_sources[0]['sources'], [])
        self.assertFalse(self.digestion_manager.list_digestions('other-user'))

        result = self.digestion_manager.query(digestion['id'], 'reader-user', 'retrieval snippets', top_k=2)
        self.assertTrue(result['success'])
        self.assertEqual(result['results'][0]['file_name'], 'private-corpus.txt')
        with self.assertRaisesRegex(Exception, 'source metadata access'):
            self.digestion_manager.list_sources(digestion['id'], user_id='reader-user')

        self.digestion_manager.grant_access(
            digestion['id'],
            'owner-user',
            'reader-user',
            can_query=True,
            can_read_sources=True,
        )
        self.assertEqual(
            self.digestion_manager.list_sources(digestion['id'], user_id='reader-user')[0]['file_name'],
            'private-corpus.txt',
        )

    def test_digestion_rest_create_build_query_and_acl(self) -> None:
        source = self._save_text('api-corpus.txt', 'Canopy Digestions help agents query a large document corpus with citations.')
        components = (
            self.db_manager,
            _FakeApiKeyManager(),
            MagicMock(),
            MagicMock(),
            MagicMock(),
            self.file_manager,
            MagicMock(),
            MagicMock(),
            MagicMock(),
            MagicMock(),
            MagicMock(),
        )
        with patch('canopy.api.routes.get_app_components', return_value=components), \
             patch('canopy.api.routes._get_app_components_any', return_value=components):
            app = Flask(__name__)
            app.config['TESTING'] = True
            app.secret_key = 'digestion-api'
            app.config['DIGESTION_MANAGER'] = self.digestion_manager
            app.register_blueprint(create_api_blueprint(), url_prefix='/api/v1')
            client = app.test_client()

            create_response = client.post(
                '/api/v1/digestions',
                json={
                    'name': 'API corpus',
                    'source_file_ids': [source.id],
                    'provider': 'local_hash',
                },
                headers={'X-API-Key': 'owner-key'},
            )
            self.assertEqual(create_response.status_code, 201)
            digestion_id = create_response.get_json()['digestion_id']

            build_response = client.post(
                f'/api/v1/digestions/{digestion_id}/build',
                json={},
                headers={'X-API-Key': 'owner-key'},
            )
            self.assertEqual(build_response.status_code, 200)
            self.assertTrue(build_response.get_json()['success'])

            blocked_query = client.post(
                f'/api/v1/digestions/{digestion_id}/query',
                json={'query': 'document corpus'},
                headers={'X-API-Key': 'reader-key'},
            )
            self.assertEqual(blocked_query.status_code, 403)

            grant_response = client.post(
                f'/api/v1/digestions/{digestion_id}/acl',
                json={'grantee_user_id': 'reader-user', 'can_query': True},
                headers={'X-API-Key': 'owner-key'},
            )
            self.assertEqual(grant_response.status_code, 200)

            query_response = client.post(
                f'/api/v1/digestions/{digestion_id}/query',
                json={'query': 'document corpus', 'top_k': 2},
                headers={'X-API-Key': 'reader-key'},
            )
            self.assertEqual(query_response.status_code, 200)
            payload = query_response.get_json()
            self.assertTrue(payload['success'])
            self.assertGreaterEqual(payload['result_count'], 1)
            self.assertEqual(payload['results'][0]['file_name'], 'api-corpus.txt')

            sources_response = client.get(
                f'/api/v1/digestions/{digestion_id}/sources',
                headers={'X-API-Key': 'reader-key'},
            )
            self.assertEqual(sources_response.status_code, 403)


if __name__ == '__main__':
    unittest.main()
