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
        unrelated = self.digestion_manager.query(digestion['id'], 'owner-user', 'winter tire recipes', top_k=3)
        self.assertTrue(unrelated['success'])
        self.assertEqual(unrelated['result_count'], 0)
        self.assertTrue(unrelated['retrieval_ready'])

    def test_query_unbuilt_digestion_returns_explicit_no_chunk_warning_without_embedding(self) -> None:
        source = self._save_text('unbuilt-corpus.txt', 'This text is present but has not been indexed yet.')
        digestion = self.digestion_manager.create_digestion(
            'owner-user',
            name='Unbuilt corpus',
            source_file_ids=[source.id],
            provider='local_hash',
        )

        with patch.object(self.digestion_manager, '_embed_one', side_effect=AssertionError('should not embed empty index')):
            result = self.digestion_manager.query(digestion['id'], 'owner-user', 'indexed yet', top_k=3)

        self.assertTrue(result['success'])
        self.assertEqual(result['result_count'], 0)
        self.assertFalse(result['retrieval_ready'])
        self.assertEqual(result['indexed_chunks'], 0)
        self.assertIn('no indexed chunks', result['warning'])
        context = self.digestion_manager.context_pack(digestion['id'], 'owner-user', 'indexed yet', top_k=3)
        self.assertFalse(context['retrieval_ready'])
        self.assertEqual(context['indexed_chunks'], 0)
        self.assertIn('no indexed chunks', context['warning'])
        self.assertIn('Retrieval warning:', context['prompt_context'])

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

    def test_material_sources_outputs_context_and_export(self) -> None:
        digestion = self.digestion_manager.create_digestion(
            'owner-user',
            name='Reusable lab digest',
            purpose='Normalize notes and make them reusable for agents.',
            provider='local_hash',
            source_materials=[
                {
                    'kind': 'meeting_note',
                    'title': 'Surface prep note',
                    'source_uri': 'canopy://channel/lab/1',
                    'content': 'Hydrogen passivation and silicon surface preparation matter for reliable SiDB experiments.',
                    'metadata': {'channel': 'lab'},
                }
            ],
        )
        sources = self.digestion_manager.list_sources(digestion['id'], user_id='owner-user')
        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0]['source_kind'], 'meeting_note')

        build = self.digestion_manager.build_digestion(digestion['id'], 'owner-user')

        self.assertTrue(build['success'])
        self.assertGreaterEqual(len(build['outputs']), 3)
        outputs = self.digestion_manager.list_outputs(digestion['id'], 'owner-user')
        output_kinds = {item['output_kind'] for item in outputs}
        self.assertTrue({'manifest', 'human_brief', 'agent_context'}.issubset(output_kinds))
        manifest = self.digestion_manager.get_output(digestion['id'], 'owner-user', 'manifest')
        self.assertEqual(manifest['content_type'], 'application/json')
        self.assertIn('canopy_digestion_manifest_v2', manifest['content'])

        context = self.digestion_manager.context_pack(digestion['id'], 'owner-user', 'silicon surface preparation', top_k=2)

        self.assertTrue(context['success'])
        self.assertIn('Use only the cited snippets', context['prompt_context'])
        self.assertGreaterEqual(len(context['citations']), 1)

        exported = self.digestion_manager.export_output_to_vault(digestion['id'], 'owner-user', 'human_brief')

        self.assertTrue(exported['success'])
        self.assertTrue(exported['file']['original_name'].endswith('-human_brief.md'))
        self.assertEqual(exported['agent_reference']['digestion_id'], digestion['id'])

        package = self.digestion_manager.package_payload(digestion['id'], 'owner-user')
        self.assertEqual(package['kind'], 'canopy_digestion_package_v1')
        self.assertTrue(package['sources_included'])
        self.assertIn('agent_reference', package)
        self.assertTrue({'manifest', 'human_brief', 'agent_context'}.issubset(
            {item['output_kind'] for item in package['outputs']}
        ))
        self.assertTrue(any('content' in item for item in package['outputs']))
        light_package = self.digestion_manager.package_payload(digestion['id'], 'owner-user', include_content=False)
        self.assertFalse(any('content' in item for item in light_package['outputs']))

        exported_package = self.digestion_manager.export_package_to_vault(digestion['id'], 'owner-user')
        self.assertTrue(exported_package['success'])
        self.assertTrue(exported_package['file']['original_name'].endswith('-canopy-digestion-package.json'))

    def test_query_only_users_cannot_read_source_revealing_outputs(self) -> None:
        source = self._save_text(
            'source-revealing-corpus.txt',
            'Reusable outputs should preserve the same source metadata boundary as source listings.',
        )
        digestion = self.digestion_manager.create_digestion(
            'owner-user',
            name='Source-gated outputs',
            source_file_ids=[source.id],
            provider='local_hash',
        )
        self.digestion_manager.build_digestion(digestion['id'], 'owner-user')
        self.digestion_manager.grant_access(digestion['id'], 'owner-user', 'reader-user', can_query=True)

        reader_outputs = self.digestion_manager.list_outputs(digestion['id'], 'reader-user', include_content=True)
        self.assertEqual({output['output_kind'] for output in reader_outputs}, {'agent_context'})
        reader_package = self.digestion_manager.package_payload(digestion['id'], 'reader-user')
        self.assertFalse(reader_package['sources_included'])
        self.assertEqual({output['output_kind'] for output in reader_package['outputs']}, {'agent_context'})
        with self.assertRaisesRegex(Exception, 'Source metadata access'):
            self.digestion_manager.get_output(digestion['id'], 'reader-user', 'manifest')
        with self.assertRaisesRegex(Exception, 'Source metadata access'):
            self.digestion_manager.export_output_to_vault(digestion['id'], 'reader-user', 'human_brief')

        self.digestion_manager.grant_access(
            digestion['id'],
            'owner-user',
            'reader-user',
            can_query=True,
            can_read_sources=True,
        )
        reader_manifest = self.digestion_manager.get_output(digestion['id'], 'reader-user', 'manifest')
        self.assertIn('source-revealing-corpus.txt', reader_manifest['content'])

    def test_manager_added_materials_remain_owner_bound_and_buildable(self) -> None:
        digestion = self.digestion_manager.create_digestion(
            'owner-user',
            name='Delegated material digest',
            provider='local_hash',
        )
        self.digestion_manager.grant_access(
            digestion['id'],
            'owner-user',
            'reader-user',
            can_query=True,
            can_manage=True,
            can_read_sources=True,
        )

        added = self.digestion_manager.add_materials(
            digestion['id'],
            'reader-user',
            [
                {
                    'kind': 'agent_note',
                    'title': 'Delegated source',
                    'content': 'A trusted agent can normalize source material into the owner-bound Digestion corpus.',
                }
            ],
        )

        self.assertTrue(added['success'])
        self.assertEqual(added['added'], 1)
        source = self.digestion_manager.list_sources(digestion['id'], user_id='reader-user')[0]
        saved_file = self.file_manager.get_file(source['file_id'])
        self.assertEqual(saved_file.uploaded_by, 'owner-user')

        build = self.digestion_manager.build_digestion(digestion['id'], 'reader-user')

        self.assertTrue(build['success'])
        self.assertGreaterEqual(build['chunk_count'], 1)

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

    def test_digestion_rest_materials_outputs_and_context(self) -> None:
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
            app.secret_key = 'digestion-api-v2'
            app.config['DIGESTION_MANAGER'] = self.digestion_manager
            app.register_blueprint(create_api_blueprint(), url_prefix='/api/v1')
            client = app.test_client()

            create_response = client.post(
                '/api/v1/digestions',
                json={
                    'name': 'API materials',
                    'provider': 'local_hash',
                    'materials': [
                        {
                            'kind': 'post',
                            'title': 'Agent memo',
                            'content': 'Reusable Digestions can normalize source material into agent context packs.',
                        }
                    ],
                    'auto_build': True,
                },
                headers={'X-API-Key': 'owner-key'},
            )
            self.assertEqual(create_response.status_code, 201)
            digestion_id = create_response.get_json()['digestion_id']

            outputs_response = client.get(
                f'/api/v1/digestions/{digestion_id}/outputs',
                headers={'X-API-Key': 'owner-key'},
            )
            self.assertEqual(outputs_response.status_code, 200)
            self.assertGreaterEqual(outputs_response.get_json()['count'], 3)

            context_response = client.post(
                f'/api/v1/digestions/{digestion_id}/context',
                json={'query': 'agent context packs'},
                headers={'X-API-Key': 'owner-key'},
            )
            self.assertEqual(context_response.status_code, 200)
            self.assertIn('prompt_context', context_response.get_json())

            export_response = client.post(
                f'/api/v1/digestions/{digestion_id}/outputs/human_brief/export',
                json={},
                headers={'X-API-Key': 'owner-key'},
            )
            self.assertEqual(export_response.status_code, 200)
            self.assertTrue(export_response.get_json()['success'])

            package_response = client.get(
                f'/api/v1/digestions/{digestion_id}/package',
                headers={'X-API-Key': 'owner-key'},
            )
            self.assertEqual(package_response.status_code, 200)
            self.assertEqual(package_response.get_json()['package']['kind'], 'canopy_digestion_package_v1')

            package_export_response = client.post(
                f'/api/v1/digestions/{digestion_id}/package/export',
                json={},
                headers={'X-API-Key': 'owner-key'},
            )
            self.assertEqual(package_export_response.status_code, 200)
            self.assertTrue(package_export_response.get_json()['success'])


if __name__ == '__main__':
    unittest.main()
