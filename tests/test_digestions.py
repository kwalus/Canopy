"""Regression tests for File Vault Digestions."""

import base64
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
from canopy.core.digestions import DigestionError, DigestionManager, ExtractedSegment
from canopy.core.file_preview import build_file_preview
from canopy.core.files import FileManager
from canopy.security.api_keys import ApiKeyInfo, Permission
from canopy.security.file_access import evaluate_file_access

_TINY_PNG = base64.b64decode(
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII='
)


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
            'manager-key': {Permission.READ_FILES, Permission.WRITE_FILES},
            'other-key': {Permission.READ_FILES},
        }
        key_users = {
            'owner-key': 'owner-user',
            'reader-key': 'reader-user',
            'manager-key': 'reader-user',
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
        self.conn.execute(
            "INSERT INTO users (id, username, origin_peer) VALUES (?, ?, ?)",
            ('remote-user', 'remote-user', 'peer-remote'),
        )
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

    def _save_image(self, name: str = 'figure-001.png', owner: str = 'owner-user'):
        info = self.file_manager.save_file(
            _TINY_PNG,
            name,
            'image/png',
            owner,
        )
        self.assertIsNotNone(info)
        return info

    def _fake_datapoint_llm_context(self) -> dict:
        return {
            'manager': object(),
            'provider': 'openai',
            'model': 'gpt-test',
            'api_key': 'test-key',
            'credential_source': 'user',
            'default_lens': 'technical datapoints',
            'parameters': {
                'max_chunks': 80,
                'max_datapoints': 400,
                'batch_chunks': 6,
                'batch_chars': 18000,
                'chunk_chars': 2800,
                'batch_records': 40,
                'max_output_tokens': 7000,
            },
        }

    def test_digestion_package_file_preview_is_bounded_reader_payload(self) -> None:
        payload = {
            'kind': 'canopy_digestion_package_v1',
            'digestion': {'id': 'dig_123', 'name': 'Materials Digest', 'purpose': 'Human-reader test'},
            'stats': {'source_count': 2, 'chunks': 9, 'token_estimate': 1200, 'output_count': 1},
            'live_query_access': {'recipient_query_requires_acl': True},
            'agent_reference': {
                'digestion_id': 'dig_123',
                'query_endpoint': '/api/v1/digestions/dig_123/query',
                'api': {
                    'query': 'POST /api/v1/digestions/dig_123/query',
                    'append_contributions': 'POST /api/v1/digestions/dig_123/contributions',
                },
                'mcp': {'query': 'canopy_digest_query', 'append_contributions': 'canopy_digest_append_contributions'},
            },
            'outputs': [
                {'kind': 'structured_datapoints', 'title': 'Datapoints', 'metadata': {'datapoint_count': 4}},
            ],
            'sources': [
                {'file_name': 'paper-a.pdf', 'chunk_count': 5},
                {'file_name': 'paper-b.pdf', 'chunk_count': 4},
            ],
            'reuse_guidance': ['Use the package summary for handoff.', 'Ask the owner for live query ACL.'],
            'content': 'full package content should not be echoed by preview',
        }

        preview = build_file_preview(
            json.dumps(payload).encode('utf-8'),
            'materials-canopy-digestion-package.json',
            'application/json',
        )

        self.assertTrue(preview['previewable'])
        self.assertEqual(preview['kind'], 'digestion_package')
        self.assertEqual(preview['digestion']['id'], 'dig_123')
        self.assertEqual(preview['stats']['chunks'], 9)
        self.assertEqual(preview['stats']['source_count'], 2)
        self.assertEqual(preview['stats']['output_count'], 1)
        self.assertEqual(preview['outputs'][0]['datapoint_count'], 4)
        self.assertEqual(preview['sources'][0]['file_name'], 'paper-a.pdf')
        self.assertIn('append_contributions', preview['agent_reference']['api'])
        self.assertIn('append_contributions', preview['agent_reference']['mcp'])
        self.assertNotIn('content', preview)

        metadata_only_payload = dict(payload)
        metadata_only_payload['sources'] = []
        metadata_only_payload['outputs'] = []
        metadata_only_preview = build_file_preview(
            json.dumps(metadata_only_payload).encode('utf-8'),
            'materials-canopy-digestion-package.json',
            'application/json',
        )
        self.assertEqual(metadata_only_preview['stats']['source_count'], 2)
        self.assertEqual(metadata_only_preview['stats']['output_count'], 1)

    def _fake_datapoint_llm_response(self, *, source_ref: str = 'chunk_0001') -> str:
        return json.dumps({
            'datapoints': [
                {
                    'subject': 'hydrogen-passivated silicon drain current',
                    'claim': 'Hydrogen passivation increased silicon device drain current by 42% at 300 K.',
                    'materials': ['silicon devices', 'hydrogen passivation'],
                    'methods': ['fabricated silicon devices using hydrogen passivation'],
                    'measurements': ['drain current at 300 K'],
                    'numerical_results': [
                        'The treated device increased current by 42% compared with the untreated control.'
                    ],
                    'relationships': [
                        'The treated device increased current by 42% compared with the untreated control.'
                    ],
                    'quantitative_results': [
                        {
                            'measurement_label': 'current increase',
                            'value_text': '42',
                            'unit': '%',
                            'evidence_sentence': (
                                'The treated device increased current by 42% compared with the untreated control.'
                            ),
                        }
                    ],
                    'limitations_or_uncertainty': [
                        'The result remains preliminary because only 3 samples were evaluated.'
                    ],
                    'evidence': [
                        {
                            'source_ref': source_ref,
                            'field': 'numerical_results',
                            'quote': (
                                'The treated device increased current by 42% compared with the untreated control.'
                            ),
                        }
                    ],
                    'tags': ['silicon', 'current', 'passivation'],
                    'confidence': 0.89,
                }
            ]
        })

    def _fake_workflow_datapoint_llm_response(self, *, source_ref: str = 'chunk_0001') -> str:
        quote = 'In a pilot, the workflow reduced setup time by 42% across 3 agent handoffs.'
        return json.dumps({
            'datapoints': [
                {
                    'subject': 'workflow setup time',
                    'claim': 'The pilot workflow reduced setup time by 42% across 3 agent handoffs.',
                    'materials': ['agent context packs'],
                    'methods': ['pilot workflow'],
                    'measurements': ['setup time', 'agent handoffs'],
                    'numerical_results': [quote],
                    'relationships': ['The workflow reduced setup time.'],
                    'quantitative_results': [
                        {
                            'measurement_label': 'setup time reduction',
                            'value_text': '42',
                            'unit': '%',
                            'evidence_sentence': quote,
                        },
                        {
                            'measurement_label': 'agent handoffs',
                            'value_text': '3',
                            'unit': '',
                            'evidence_sentence': quote,
                        },
                    ],
                    'limitations_or_uncertainty': ['pilot result'],
                    'evidence': [
                        {
                            'source_ref': source_ref,
                            'field': 'numerical_results',
                            'quote': quote,
                        }
                    ],
                    'tags': ['workflow', 'handoffs'],
                    'confidence': 0.86,
                }
            ]
        })

    def test_local_hash_digestion_builds_and_queries_owned_vault_files(self) -> None:
        silicon = self._save_text(
            'silicon-notes.txt',
            'Quantum silicon devices need careful surface preparation and hyperfine control. '
            'SiDB circuits can be compared with other storage media. '
            'Voltage tuning stabilizes the device response.',
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
        listed = self.digestion_manager.list_digestions('owner-user', include_sources=True)
        listed_item = next(item for item in listed if item['id'] == digestion['id'])
        self.assertGreaterEqual(listed_item['stats']['chunks'], build['chunk_count'])
        self.assertGreaterEqual(listed_item['stats']['token_estimate'], 1)
        self.assertIn('indexed', listed_item['stats']['sources_by_status'])

        result = self.digestion_manager.query(digestion['id'], 'owner-user', 'quantum silicon hyperfine', top_k=3)
        self.assertTrue(result['success'])
        self.assertGreaterEqual(result['result_count'], 1)
        self.assertEqual(result['results'][0]['file_name'], 'silicon-notes.txt')
        self.assertIn('hyperfine', result['results'][0]['snippet'])
        partial = self.digestion_manager.query(digestion['id'], 'owner-user', 'volt', top_k=3)
        self.assertTrue(partial['success'])
        self.assertGreaterEqual(partial['result_count'], 1)
        self.assertEqual(partial['results'][0]['file_name'], 'silicon-notes.txt')
        self.assertIn('Voltage', partial['results'][0]['snippet'])
        unrelated = self.digestion_manager.query(digestion['id'], 'owner-user', 'winter tire recipes', top_k=3)
        self.assertTrue(unrelated['success'])
        self.assertEqual(unrelated['result_count'], 0)
        self.assertTrue(unrelated['retrieval_ready'])

    def test_merge_sources_from_digestion_copies_references_without_destroying_source(self) -> None:
        alpha = self._save_text('alpha.txt', 'Alpha source material for reusable digestion merging.')
        beta = self._save_text('beta.txt', 'Beta source material for reusable digestion merging.')
        source = self.digestion_manager.create_digestion(
            'owner-user',
            name='Source corpus',
            source_file_ids=[alpha.id, beta.id],
            provider='local_hash',
        )
        target = self.digestion_manager.create_digestion(
            'owner-user',
            name='Target corpus',
            source_file_ids=[alpha.id],
            provider='local_hash',
        )

        result = self.digestion_manager.merge_sources_from_digestion(
            target['id'],
            source['id'],
            'owner-user',
        )

        self.assertTrue(result['success'])
        self.assertEqual(result['added'], 1)
        self.assertEqual(result['updated'], 1)
        self.assertFalse(result['skipped'])
        target_sources = self.digestion_manager.list_sources(target['id'], user_id='owner-user')
        source_sources = self.digestion_manager.list_sources(source['id'], user_id='owner-user')
        self.assertEqual({item['file_id'] for item in target_sources}, {alpha.id, beta.id})
        self.assertEqual({item['file_id'] for item in source_sources}, {alpha.id, beta.id})

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
        grant = self.digestion_manager.grant_access(digestion['id'], 'owner-user', 'reader-user', can_query=True)
        self.assertTrue(grant['success'])
        self.assertTrue(grant['can_query'])
        self.assertFalse(grant['can_read_sources'])
        self.assertEqual(grant['grantee']['username'], 'reader-user')
        with self.assertRaisesRegex(Exception, 'local users or agents'):
            self.digestion_manager.grant_access(digestion['id'], 'owner-user', 'missing-user', can_query=True)
        with self.assertRaisesRegex(Exception, 'local users or agents'):
            self.digestion_manager.grant_access(digestion['id'], 'owner-user', 'remote-user', can_query=True)

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

    def test_digestion_acl_keeps_multiple_grantees_and_revokes_one(self) -> None:
        source = self._save_text('multi-acl.txt', 'Multi-grantee access should preserve each recipient independently.')
        digestion = self.digestion_manager.create_digestion(
            'owner-user',
            name='Multi ACL corpus',
            source_file_ids=[source.id],
            provider='local_hash',
        )
        self.digestion_manager.build_digestion(digestion['id'], 'owner-user')

        self.digestion_manager.grant_access(
            digestion['id'],
            'owner-user',
            'reader-user',
            can_query=True,
            can_read_sources=True,
        )
        self.digestion_manager.grant_access(
            digestion['id'],
            'owner-user',
            'other-user',
            can_query=True,
            can_manage=False,
        )
        with self.assertRaises(DigestionError) as owner_grant_context:
            self.digestion_manager.grant_access(digestion['id'], 'owner-user', 'owner-user', can_query=True)
        self.assertEqual(owner_grant_context.exception.reason, 'owner_not_grantable')

        acl = self.digestion_manager.list_access(digestion['id'], 'owner-user')
        entries = {entry['user_id']: entry for entry in acl['entries']}
        self.assertEqual(set(entries), {'reader-user', 'other-user'})
        self.assertTrue(entries['reader-user']['can_read_sources'])
        self.assertFalse(entries['other-user']['can_read_sources'])
        self.assertTrue(self.digestion_manager.query(digestion['id'], 'reader-user', 'access')['success'])
        self.assertTrue(self.digestion_manager.query(digestion['id'], 'other-user', 'access')['success'])

        self.digestion_manager.grant_access(
            digestion['id'],
            'owner-user',
            'reader-user',
            can_query=True,
            can_manage=True,
            can_read_sources=True,
        )
        updated_acl = self.digestion_manager.list_access(digestion['id'], 'owner-user')
        updated_entries = {entry['user_id']: entry for entry in updated_acl['entries']}
        self.assertEqual(set(updated_entries), {'reader-user', 'other-user'})
        self.assertTrue(updated_entries['reader-user']['can_manage'])

        revoke = self.digestion_manager.revoke_access(digestion['id'], 'owner-user', 'reader-user')
        self.assertTrue(revoke['revoked'])
        after_revoke = self.digestion_manager.list_access(digestion['id'], 'owner-user')
        self.assertEqual([entry['user_id'] for entry in after_revoke['entries']], ['other-user'])
        with self.assertRaisesRegex(Exception, 'query access'):
            self.digestion_manager.query(digestion['id'], 'reader-user', 'access')
        self.assertTrue(self.digestion_manager.query(digestion['id'], 'other-user', 'access')['success'])

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
        agent_ref = package['agent_reference']
        self.assertIn('/query', agent_ref['query_endpoint'])
        self.assertIn('/package', agent_ref['package_endpoint'])
        self.assertIn('append_contributions', agent_ref['api'])
        self.assertIn('datapoints_search', agent_ref['api'])
        self.assertIn('acl_grant', agent_ref['api'])
        self.assertEqual(agent_ref['mcp']['append_contributions'], 'canopy_digest_append_contributions')
        self.assertIn('sources_figures_datapoints', agent_ref['permissions'])
        self.assertEqual(package['digestion']['access_subject_user_id'], 'owner-user')
        self.assertEqual(package['digestion']['access_scope'], 'exporting_user')
        self.assertEqual(package['access_subject']['user_id'], 'owner-user')
        self.assertTrue(package['access_subject']['access']['can_query'])
        self.assertFalse(package['access_subject']['recipient_live_query_implied'])
        self.assertEqual(package['live_query_access']['package_access_reflects'], 'exporting_user')
        self.assertFalse(package['live_query_access']['recipient_live_query_implied'])
        self.assertTrue(package['live_query_access']['recipient_query_requires_acl'])
        self.assertTrue({'manifest', 'human_brief', 'agent_context'}.issubset(
            {item['output_kind'] for item in package['outputs']}
        ))
        self.assertTrue(any('content' in item for item in package['outputs']))
        light_package = self.digestion_manager.package_payload(digestion['id'], 'owner-user', include_content=False)
        self.assertFalse(any('content' in item for item in light_package['outputs']))

        exported_package = self.digestion_manager.export_package_to_vault(digestion['id'], 'owner-user')
        self.assertTrue(exported_package['success'])
        self.assertTrue(exported_package['file']['original_name'].endswith('-canopy-digestion-package.json'))

    def test_pdf_caption_candidates_include_tables_charts_and_diagrams(self) -> None:
        candidates = self.digestion_manager._pdf_caption_candidates_by_page([
            ExtractedSegment(
                text=(
                    'Table 2 summarizes the measured gate voltage values.\n\n'
                    'Chart 3 compares current density across samples.\n\n'
                    'Diagram IV shows the device stack.'
                ),
                page_label='p. 4',
            )
        ])

        joined = ' '.join(candidates.get('p. 4') or [])
        self.assertIn('Table 2', joined)
        self.assertIn('Chart 3', joined)
        self.assertIn('Diagram IV', joined)

    def test_pdf_figures_are_source_gated_packaged_and_output_ready(self) -> None:
        source = self._save_text(
            'figure-corpus.txt',
            'Figure 1. Device geometry for a silicon single-electron transistor experiment. '
            'The caption reports the gate voltage and quantum dot geometry.',
        )
        image = self._save_image()
        digestion = self.digestion_manager.create_digestion(
            'owner-user',
            name='Figure digest',
            source_file_ids=[source.id],
            provider='local_hash',
        )
        self.digestion_manager.build_digestion(digestion['id'], 'owner-user')
        self.digestion_manager._insert_pdf_figure({
            'digestion_id': digestion['id'],
            'source_file_id': source.id,
            'source_checksum': source.checksum,
            'figure_index': 1,
            'page_number': 2,
            'page_label': 'p. 2',
            'image_file_id': image.id,
            'image_name': image.original_name,
            'content_type': image.content_type,
            'width': 640,
            'height': 360,
            'byte_size': image.size,
            'caption': 'Figure 1. Device geometry for a silicon single-electron transistor experiment.',
            'context_text': 'Figure 1 on p. 2 shows the device geometry and gate-voltage context.',
            'vision_description': '',
            'extraction_method': 'test.fixture',
            'metadata': {'vision_status': 'not_run', 'source_file_name': source.original_name},
        })

        figures = self.digestion_manager.list_figures(digestion['id'], 'owner-user')

        self.assertEqual(figures['count'], 1)
        self.assertEqual(figures['stats']['figures'], 1)
        figure = figures['figures'][0]
        self.assertEqual(figure['source_file_id'], source.id)
        self.assertEqual(figure['image_file_id'], image.id)
        self.assertEqual(figure['image_url'], f'/files/{image.id}')
        self.assertEqual(figure['thumb_url'], f'/files/{image.id}/thumb')
        self.assertEqual(figure['page_label'], 'p. 2')
        self.assertIn('single-electron transistor', figure['caption'])

        outputs = self.digestion_manager.generate_outputs(digestion['id'], 'owner-user', kinds=['pdf_figures'])
        self.assertEqual([row['output_kind'] for row in outputs['outputs']], ['pdf_figures'])
        output = self.digestion_manager.get_output(digestion['id'], 'owner-user', 'pdf_figures')
        self.assertIn('canopy_pdf_figures_v1', output['content'])
        self.assertIn(image.id, output['content'])

        package = self.digestion_manager.package_payload(digestion['id'], 'owner-user')
        self.assertTrue(package['figures_included'])
        self.assertEqual(package['figures'][0]['image_file_id'], image.id)

        self.digestion_manager.grant_access(digestion['id'], 'owner-user', 'reader-user', can_query=True)
        with self.assertRaises(DigestionError) as denied_context:
            self.digestion_manager.list_figures(digestion['id'], 'reader-user')
        self.assertEqual(denied_context.exception.reason, 'source_metadata_denied')
        denied_image_access = evaluate_file_access(
            db_manager=self.db_manager,
            file_id=image.id,
            viewer_user_id='reader-user',
            file_uploaded_by=image.uploaded_by,
        )
        self.assertFalse(denied_image_access.allowed)
        reader_package = self.digestion_manager.package_payload(digestion['id'], 'reader-user')
        self.assertFalse(reader_package['figures_included'])

        self.digestion_manager.grant_access(
            digestion['id'],
            'owner-user',
            'reader-user',
            can_query=True,
            can_read_sources=True,
        )
        reader_figures = self.digestion_manager.list_figures(digestion['id'], 'reader-user')
        self.assertEqual(reader_figures['count'], 1)
        granted_image_access = evaluate_file_access(
            db_manager=self.db_manager,
            file_id=image.id,
            viewer_user_id='reader-user',
            file_uploaded_by=image.uploaded_by,
        )
        self.assertTrue(granted_image_access.allowed)
        self.assertEqual(granted_image_access.reason, 'digestion-source-metadata')
        reader_output = self.digestion_manager.get_output(digestion['id'], 'reader-user', 'pdf_figures')
        self.assertIn(image.id, reader_output['content'])

    def test_structured_datapoints_output_is_source_grounded_and_source_gated(self) -> None:
        source = self._save_text(
            'datapoint-corpus.txt',
            'We fabricated silicon devices using hydrogen passivation and measured drain current at 300 K. '
            'The treated device increased current by 42% compared with the untreated control. '
            'However, the result remains preliminary because only 3 samples were evaluated.',
        )
        digestion = self.digestion_manager.create_digestion(
            'owner-user',
            name='Datapoint corpus',
            source_file_ids=[source.id],
            provider='local_hash',
            chunk_size=420,
            chunk_overlap=20,
        )
        self.digestion_manager.build_digestion(digestion['id'], 'owner-user')

        with patch.object(
            self.digestion_manager,
            '_resolve_datapoint_llm_context',
            return_value=self._fake_datapoint_llm_context(),
        ), patch.object(
            self.digestion_manager,
            '_call_datapoint_llm',
            return_value=self._fake_datapoint_llm_response(),
        ):
            result = self.digestion_manager.generate_structured_datapoints(
                digestion['id'],
                'owner-user',
                lens='device measurements and quantitative results',
            )

        self.assertTrue(result['success'])
        self.assertEqual(result['output']['output_kind'], 'structured_datapoints')
        self.assertGreaterEqual(result['datapoint_count'], 1)
        self.assertGreaterEqual(result['quantitative_result_count'], 1)
        output = self.digestion_manager.get_output(digestion['id'], 'owner-user', 'structured_datapoints')
        payload = json.loads(output['content'])
        self.assertEqual(payload['kind'], 'canopy_structured_datapoints_v1')
        datapoint = payload['datapoints'][0]
        self.assertEqual(datapoint['source']['file_name'], 'datapoint-corpus.txt')
        self.assertIn('42%', ' '.join(datapoint['numerical_results']))
        self.assertTrue(any(item['field'] == 'numerical_results' for item in datapoint['evidence']))
        self.assertEqual(payload['extractor']['mode'], 'source_grounded_llm')
        self.assertEqual(payload['extractor']['provider'], 'openai')
        self.assertEqual(result['progress']['status'], 'completed')
        self.assertEqual(result['progress']['percent'], 100)
        self.assertGreaterEqual(result['progress']['details']['datapoint_count'], 1)
        progress = self.digestion_manager.get_operation_progress(digestion['id'], 'owner-user')
        self.assertEqual(progress['operations']['datapoints']['status'], 'completed')
        stats = self.digestion_manager.stats(digestion['id'])
        self.assertEqual(stats['source_count'], 1)
        self.assertGreaterEqual(stats['datapoint_count'], 1)
        self.assertGreaterEqual(stats['quantitative_result_count'], 1)

        search = self.digestion_manager.search_structured_datapoints(
            digestion['id'],
            'owner-user',
            'drain current hydrogen passivation',
            limit='not-a-number',
        )
        self.assertTrue(search['success'])
        self.assertTrue(search['datapoints_ready'])
        self.assertGreaterEqual(search['result_count'], 1)
        self.assertEqual(search['results'][0]['source']['file_name'], 'datapoint-corpus.txt')
        self.assertIn('content_type', search['results'][0]['source'])
        self.assertIn('structured_fields', search['results'][0])
        self.assertIn('numerical_results', search['results'][0]['structured_fields'])

        self.digestion_manager.grant_access(digestion['id'], 'owner-user', 'reader-user', can_query=True)
        self.assertEqual(
            {output['output_kind'] for output in self.digestion_manager.list_outputs(digestion['id'], 'reader-user')},
            {'agent_context'},
        )
        with self.assertRaisesRegex(Exception, 'Source metadata access'):
            self.digestion_manager.get_output(digestion['id'], 'reader-user', 'structured_datapoints')
        with self.assertRaisesRegex(Exception, 'Source metadata access'):
            self.digestion_manager.search_structured_datapoints(digestion['id'], 'reader-user', 'drain current')

    def test_structured_datapoints_use_digestion_ai_parameters_when_no_request_override(self) -> None:
        source = self._save_text(
            'parameter-corpus.txt',
            'The treated device increased current by 42% compared with the untreated control. ' * 20
            + '\n\nThe untreated device remained stable at 300 K during the short test. ' * 20,
        )
        digestion = self.digestion_manager.create_digestion(
            'owner-user',
            name='Parameterized corpus',
            source_file_ids=[source.id],
            provider='local_hash',
            chunk_size=260,
            chunk_overlap=0,
        )
        self.digestion_manager.build_digestion(digestion['id'], 'owner-user')
        context = self._fake_datapoint_llm_context()
        context['default_lens'] = 'latency and setup-time metrics'
        context['parameters'] = {
            'max_chunks': 1,
            'max_datapoints': 1,
            'batch_chunks': 1,
            'batch_chars': 5000,
            'chunk_chars': 1200,
            'batch_records': 3,
            'max_output_tokens': 4321,
        }

        with patch.object(
            self.digestion_manager,
            '_resolve_datapoint_llm_context',
            return_value=context,
        ), patch.object(
            self.digestion_manager,
            '_call_datapoint_llm',
            return_value=self._fake_datapoint_llm_response(),
        ) as call_mock:
            result = self.digestion_manager.generate_structured_datapoints(
                digestion['id'],
                'owner-user',
            )

        self.assertTrue(result['success'])
        output = self.digestion_manager.get_output(digestion['id'], 'owner-user', 'structured_datapoints')
        payload = json.loads(output['content'])
        self.assertEqual(payload['limits']['max_chunks'], 1)
        self.assertEqual(payload['limits']['max_datapoints'], 1)
        self.assertEqual(payload['limits']['batch_chunks'], 1)
        self.assertEqual(payload['limits']['batch_records'], 3)
        self.assertEqual(payload['limits']['max_output_tokens'], 4321)
        self.assertEqual(payload['extractor']['lens'], 'latency and setup-time metrics')
        self.assertEqual(call_mock.call_args.args[0]['parameters']['max_output_tokens'], 4321)

    def test_structured_datapoints_drop_unsupported_claims(self) -> None:
        source = self._save_text(
            'unsupported-claim-corpus.txt',
            'The benchmark measured a 42% improvement after the workflow change at 300 K.',
        )
        digestion = self.digestion_manager.create_digestion(
            'owner-user',
            name='Unsupported claim corpus',
            source_file_ids=[source.id],
            provider='local_hash',
            chunk_size=320,
            chunk_overlap=20,
        )
        self.digestion_manager.build_digestion(digestion['id'], 'owner-user')
        response = json.dumps({
            'datapoints': [
                {
                    'subject': 'workflow benchmark',
                    'claim': 'The workflow doubled performance in every downstream system.',
                    'numerical_results': ['The benchmark measured a 42% improvement after the workflow change at 300 K.'],
                    'relationships': ['The workflow completely eliminated all errors in production.'],
                    'evidence': [
                        {
                            'source_ref': 'chunk_0001',
                            'field': 'numerical_results',
                            'quote': 'The benchmark measured a 42% improvement after the workflow change at 300 K.',
                        }
                    ],
                }
            ]
        })

        with patch.object(
            self.digestion_manager,
            '_resolve_datapoint_llm_context',
            return_value=self._fake_datapoint_llm_context(),
        ), patch.object(
            self.digestion_manager,
            '_call_datapoint_llm',
            return_value=response,
        ):
            result = self.digestion_manager.generate_structured_datapoints(digestion['id'], 'owner-user')

        self.assertTrue(result['success'])
        output = self.digestion_manager.get_output(digestion['id'], 'owner-user', 'structured_datapoints')
        payload = json.loads(output['content'])
        datapoint = payload['datapoints'][0]
        self.assertEqual(datapoint['claim'], '')
        self.assertEqual(datapoint['relationships'], [])
        self.assertTrue(datapoint['numerical_results'])

    def test_structured_datapoints_limits_report_provider_bounded_output_tokens(self) -> None:
        source = self._save_text(
            'bedrock-limit-corpus.txt',
            'The treated device increased current by 42% compared with the untreated control.',
        )
        digestion = self.digestion_manager.create_digestion(
            'owner-user',
            name='Bedrock limit corpus',
            source_file_ids=[source.id],
            provider='local_hash',
            chunk_size=320,
            chunk_overlap=20,
        )
        self.digestion_manager.build_digestion(digestion['id'], 'owner-user')
        context = self._fake_datapoint_llm_context()
        context['provider'] = 'bedrock'
        context['parameters']['max_output_tokens'] = 999999

        with patch.object(
            self.digestion_manager,
            '_resolve_datapoint_llm_context',
            return_value=context,
        ), patch.object(
            self.digestion_manager,
            '_call_datapoint_llm',
            return_value=self._fake_datapoint_llm_response(),
        ):
            result = self.digestion_manager.generate_structured_datapoints(digestion['id'], 'owner-user')

        self.assertTrue(result['success'])
        output = self.digestion_manager.get_output(digestion['id'], 'owner-user', 'structured_datapoints')
        payload = json.loads(output['content'])
        self.assertEqual(payload['limits']['max_output_tokens'], 12000)

    def test_call_datapoint_llm_bounds_output_tokens_by_provider(self) -> None:
        manager = MagicMock()
        manager._call_openai.return_value = '{"datapoints":[]}'
        manager._call_bedrock.return_value = '{"datapoints":[]}'

        self.digestion_manager._call_datapoint_llm(
            {
                'manager': manager,
                'provider': 'openai',
                'model': 'gpt-test',
                'api_key': 'openai-key',
                'parameters': {'max_output_tokens': 999999},
            },
            system_prompt='system',
            prompt='prompt',
        )
        self.assertEqual(manager._call_openai.call_args.kwargs['max_output_tokens'], 20000)

        self.digestion_manager._call_datapoint_llm(
            {
                'manager': manager,
                'provider': 'bedrock',
                'model': 'bedrock-model',
                'api_key': 'bedrock-secret',
                'parameters': {'max_output_tokens': 999999},
            },
            system_prompt='system',
            prompt='prompt',
        )
        self.assertEqual(manager._call_bedrock.call_args.kwargs['max_output_tokens'], 12000)

    def test_structured_datapoints_requires_configured_llm_provider(self) -> None:
        source = self._save_text(
            'requires-llm.txt',
            'The benchmark measured a 42% improvement after the workflow change.',
        )
        digestion = self.digestion_manager.create_digestion(
            'owner-user',
            name='Provider required corpus',
            source_file_ids=[source.id],
            provider='local_hash',
            chunk_size=320,
            chunk_overlap=20,
        )
        self.digestion_manager.build_digestion(digestion['id'], 'owner-user')

        with self.assertRaises(DigestionError) as context:
            self.digestion_manager.generate_structured_datapoints(
                digestion['id'],
                'owner-user',
                max_chunks=1,
                max_datapoints=1,
            )
        self.assertTrue(context.exception.reason.startswith('datapoint_'))
        self.assertIn('LLM-backed datapoint extraction is not configured', str(context.exception))

    def test_structured_datapoint_evidence_requires_supported_quote(self) -> None:
        self.assertTrue(DigestionManager._datapoint_quote_supported(
            'The benchmark measured a 42% improvement after the workflow change.',
            'The benchmark measured a 42% improvement after the workflow change.',
        ))
        self.assertFalse(DigestionManager._datapoint_quote_supported(
            'The workflow conclusively doubled performance in every downstream system.',
            'The benchmark measured a 42% improvement after the workflow change.',
        ))

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
            can_manage=True,
            can_read_sources=False,
        )
        managed_outputs = self.digestion_manager.list_outputs(digestion['id'], 'reader-user', include_content=True)
        self.assertEqual({output['output_kind'] for output in managed_outputs}, {'agent_context'})
        managed_package = self.digestion_manager.package_payload(digestion['id'], 'reader-user')
        self.assertFalse(managed_package['sources_included'])
        with self.assertRaisesRegex(Exception, 'source metadata access'):
            self.digestion_manager.list_sources(digestion['id'], user_id='reader-user')
        with patch.object(
            self.digestion_manager,
            '_resolve_datapoint_llm_context',
            return_value=self._fake_datapoint_llm_context(),
        ) as context_mock:
            with self.assertRaises(DigestionError) as extraction_context:
                self.digestion_manager.generate_structured_datapoints(digestion['id'], 'reader-user')
        self.assertEqual(extraction_context.exception.reason, 'datapoint_source_metadata_denied')
        context_mock.assert_not_called()
        gen_result = self.digestion_manager.generate_outputs(digestion['id'], 'reader-user')
        self.assertEqual({output['output_kind'] for output in gen_result['outputs']}, {'agent_context'})
        for output_row in gen_result['outputs']:
            self.assertNotIn('source-revealing-corpus.txt', output_row.get('preview', ''))

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

    def test_manager_added_vault_sources_are_copied_owner_bound_and_buildable(self) -> None:
        manager_file = self._save_text(
            'agent-found-paper.txt',
            'A trusted agent found a paper reporting that voltage stability improved by 17% after calibration.',
            owner='reader-user',
        )
        digestion = self.digestion_manager.create_digestion(
            'owner-user',
            name='Agent contributed papers',
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

        added = self.digestion_manager.add_sources(digestion['id'], 'reader-user', [manager_file.id])

        self.assertTrue(added['success'])
        self.assertEqual(added['added'], 1)
        self.assertEqual(added['sources'][0]['input_file_id'], manager_file.id)
        self.assertTrue(added['sources'][0]['copied_to_owner_vault'])
        source = self.digestion_manager.list_sources(digestion['id'], user_id='reader-user')[0]
        self.assertNotEqual(source['file_id'], manager_file.id)
        saved_file = self.file_manager.get_file(source['file_id'])
        self.assertEqual(saved_file.uploaded_by, 'owner-user')
        metadata = json.loads(source['source_metadata_json'])
        self.assertEqual(metadata['original_file_id'], manager_file.id)
        self.assertEqual(metadata['submitted_by'], 'reader-user')

        duplicate = self.digestion_manager.add_sources(digestion['id'], 'reader-user', [manager_file.id])
        self.assertEqual(duplicate['added'], 1)
        self.assertEqual(len(self.digestion_manager.list_sources(digestion['id'], user_id='reader-user')), 1)
        build = self.digestion_manager.build_digestion(digestion['id'], 'reader-user')

        self.assertTrue(build['success'])
        self.assertGreaterEqual(build['chunk_count'], 1)

    def test_manager_added_vault_sources_use_file_manager_path_resolver(self) -> None:
        manager_file = self._save_text(
            'resolver-owned-paper.txt',
            'A delegated source has bytes available only after FileManager path resolution.',
            owner='reader-user',
        )
        actual_path = Path(manager_file.file_path)
        self.conn.execute(
            "UPDATE files SET file_path = ? WHERE id = ?",
            ('legacy/storage/path.txt', manager_file.id),
        )
        self.conn.commit()
        original_resolver = self.file_manager._resolve_file_disk_path
        self.file_manager._resolve_file_disk_path = lambda raw_path: actual_path if raw_path == 'legacy/storage/path.txt' else original_resolver(raw_path)  # type: ignore[method-assign]
        digestion = self.digestion_manager.create_digestion(
            'owner-user',
            name='Resolver-backed source',
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

        added = self.digestion_manager.add_sources(digestion['id'], 'reader-user', [manager_file.id])

        self.assertTrue(added['success'])
        self.assertEqual(added['added'], 1)
        self.assertEqual(added['skipped'], [])

    def test_manager_added_vault_sources_skip_when_intake_folder_unavailable(self) -> None:
        manager_file = self._save_text(
            'intake-folder-failure.txt',
            'A delegated source should not fall back to the Vault root when intake folders fail.',
            owner='reader-user',
        )
        self.file_manager.create_user_folder = MagicMock(side_effect=RuntimeError('folder db unavailable'))  # type: ignore[method-assign]
        digestion = self.digestion_manager.create_digestion(
            'owner-user',
            name='Unavailable intake folder',
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

        added = self.digestion_manager.add_sources(digestion['id'], 'reader-user', [manager_file.id])

        self.assertTrue(added['success'])
        self.assertEqual(added['added'], 0)
        self.assertEqual(added['skipped'][0]['reason'], 'intake_folder_unavailable')

    def test_manager_can_append_agent_contributions_files_and_structured_datapoints(self) -> None:
        manager_file = self._save_text(
            'agent-derived-table.csv',
            'metric,value,unit\nvoltage stability,17,%\n',
            owner='reader-user',
        )
        digestion = self.digestion_manager.create_digestion(
            'owner-user',
            name='Agent contribution digest',
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

        result = self.digestion_manager.append_contributions(
            digestion['id'],
            'reader-user',
            contributions=[
                {
                    'kind': 'synthesis_note',
                    'title': 'Voltage calibration synthesis',
                    'content': 'The agent compared source snippets and found the calibration result worth retaining.',
                    'claims': ['Voltage stability improved after calibration.'],
                    'references': ['agent-derived-table.csv'],
                    'source_file_ids': [manager_file.id],
                    'datapoints': [
                        {
                            'subject': 'voltage calibration',
                            'claim': 'Voltage stability improved by 17% after calibration.',
                            'measurements': ['voltage stability'],
                            'numerical_results': ['Voltage stability improved by 17%.'],
                            'quantitative_results': [
                                {
                                    'measurement_label': 'voltage stability improvement',
                                    'value_text': '17',
                                    'unit': '%',
                                }
                            ],
                            'evidence': ['Agent-derived table reported voltage stability, 17, %.'],
                            'tags': ['voltage', 'calibration'],
                            'confidence': 0.82,
                        }
                    ],
                }
            ],
            build_after=True,
        )

        self.assertTrue(result['success'])
        self.assertEqual(result['materials_added'], 1)
        self.assertEqual(result['source_files_added'], 1)
        self.assertEqual(result['datapoints_added'], 1)
        sources = self.digestion_manager.list_sources(digestion['id'], user_id='reader-user')
        source_kinds = {source['source_kind'] for source in sources}
        self.assertIn('agent_contribution', source_kinds)
        copied_sources = [
            source for source in sources
            if source['file_name'] == manager_file.original_name
        ]
        self.assertEqual(len(copied_sources), 1)
        self.assertEqual(self.file_manager.get_file(copied_sources[0]['file_id']).uploaded_by, 'owner-user')

        output = self.digestion_manager.get_output(digestion['id'], 'reader-user', 'structured_datapoints')
        payload = json.loads(output['content'])
        self.assertEqual(payload['stats']['agent_contributed_datapoint_count'], 1)
        search = self.digestion_manager.search_structured_datapoints(
            digestion['id'],
            'reader-user',
            'voltage stability calibration',
        )
        self.assertGreaterEqual(search['result_count'], 1)
        self.assertEqual(search['results'][0]['quantitative_results'][0]['value_text'], '17')

    def test_manage_only_contribution_datapoints_are_rejected_without_source_access(self) -> None:
        digestion = self.digestion_manager.create_digestion(
            'owner-user',
            name='Manage only contribution digest',
            provider='local_hash',
        )
        self.digestion_manager.grant_access(
            digestion['id'],
            'owner-user',
            'reader-user',
            can_query=True,
            can_manage=True,
            can_read_sources=False,
        )

        with self.assertRaises(DigestionError) as context:
            self.digestion_manager.append_contributions(
                digestion['id'],
                'reader-user',
                datapoints=[
                    {
                        'subject': 'private metric',
                        'claim': 'A source-revealing datapoint should require source access.',
                        'evidence': ['This citation would reveal source context.'],
                    }
                ],
            )

        self.assertEqual(context.exception.status_code, 403)
        self.assertEqual(context.exception.reason, 'datapoint_source_metadata_denied')
        outputs = self.digestion_manager.list_outputs(digestion['id'], 'owner-user', include_content=True)
        self.assertFalse([item for item in outputs if item['output_kind'] == 'structured_datapoints'])

    def test_structured_datapoints_default_to_incremental_new_sources(self) -> None:
        first = self._save_text(
            'a-current-paper.txt',
            'We fabricated silicon devices using hydrogen passivation and measured drain current at 300 K. '
            'The treated device increased current by 42% compared with the untreated control. '
            'However, the result remains preliminary because only 3 samples were evaluated.',
        )
        second = self._save_text(
            'z-voltage-paper.txt',
            'The second paper reported voltage stability at 5 V across 8 calibration trials.',
        )
        digestion = self.digestion_manager.create_digestion(
            'owner-user',
            name='Incremental datapoints',
            source_file_ids=[first.id],
            provider='local_hash',
            chunk_size=420,
            chunk_overlap=20,
        )
        self.digestion_manager.build_digestion(digestion['id'], 'owner-user')
        second_response = json.dumps({
            'datapoints': [
                {
                    'subject': 'voltage stability',
                    'claim': 'The second paper reported voltage stability at 5 V across 8 calibration trials.',
                    'materials': ['calibration trials'],
                    'methods': ['calibration'],
                    'measurements': ['voltage stability'],
                    'numerical_results': ['The second paper reported voltage stability at 5 V across 8 calibration trials.'],
                    'relationships': [],
                    'quantitative_results': [
                        {
                            'measurement_label': 'voltage stability',
                            'value_text': '5',
                            'unit': 'V',
                            'evidence_sentence': 'The second paper reported voltage stability at 5 V across 8 calibration trials.',
                        }
                    ],
                    'limitations_or_uncertainty': [],
                    'evidence': [
                        {
                            'source_ref': 'chunk_0001',
                            'field': 'numerical_results',
                            'quote': 'The second paper reported voltage stability at 5 V across 8 calibration trials.',
                        }
                    ],
                    'tags': ['voltage', 'stability'],
                    'confidence': 0.91,
                }
            ]
        })

        with patch.object(
            self.digestion_manager,
            '_resolve_datapoint_llm_context',
            return_value=self._fake_datapoint_llm_context(),
        ), patch.object(
            self.digestion_manager,
            '_call_datapoint_llm',
            return_value=self._fake_datapoint_llm_response(),
        ):
            first_result = self.digestion_manager.generate_structured_datapoints(digestion['id'], 'owner-user')
        self.assertEqual(first_result['extraction_scope'], 'new')
        self.assertEqual(first_result['datapoint_count'], 1)

        self.digestion_manager.add_sources(digestion['id'], 'owner-user', [second.id])
        self.digestion_manager.build_digestion(digestion['id'], 'owner-user')
        with patch.object(
            self.digestion_manager,
            '_resolve_datapoint_llm_context',
            return_value=self._fake_datapoint_llm_context(),
        ), patch.object(
            self.digestion_manager,
            '_call_datapoint_llm',
            return_value=second_response,
        ) as call_mock:
            second_result = self.digestion_manager.generate_structured_datapoints(digestion['id'], 'owner-user')

        self.assertEqual(call_mock.call_count, 1)
        self.assertEqual(second_result['extraction_scope'], 'new')
        self.assertEqual(second_result['new_datapoint_count'], 1)
        self.assertEqual(second_result['preserved_datapoint_count'], 1)
        self.assertEqual(second_result['datapoint_count'], 2)
        output = self.digestion_manager.get_output(digestion['id'], 'owner-user', 'structured_datapoints')
        payload = json.loads(output['content'])
        self.assertEqual(payload['stats']['extraction_scope'], 'new')
        self.assertEqual(payload['stats']['new_datapoint_count'], 1)
        self.assertEqual(payload['stats']['preserved_datapoint_count'], 1)
        self.assertEqual(
            {item['source']['file_name'] for item in payload['datapoints']},
            {'a-current-paper.txt', 'z-voltage-paper.txt'},
        )

        with patch.object(
            self.digestion_manager,
            '_resolve_datapoint_llm_context',
            return_value=self._fake_datapoint_llm_context(),
        ), patch.object(
            self.digestion_manager,
            '_call_datapoint_llm',
            side_effect=AssertionError('no LLM call expected when there are no new chunks'),
        ):
            skipped = self.digestion_manager.generate_structured_datapoints(digestion['id'], 'owner-user')
        self.assertTrue(skipped['skipped'])
        self.assertEqual(skipped['reason'], 'no_new_chunks')
        self.assertEqual(skipped['datapoint_count'], 2)

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
            progress_response = client.get(
                f'/api/v1/digestions/{digestion_id}/progress',
                headers={'X-API-Key': 'owner-key'},
            )
            self.assertEqual(progress_response.status_code, 200)
            self.assertEqual(progress_response.get_json()['operations']['build']['status'], 'completed')

            blocked_query = client.post(
                f'/api/v1/digestions/{digestion_id}/query',
                json={'query': 'document corpus'},
                headers={'X-API-Key': 'reader-key'},
            )
            self.assertEqual(blocked_query.status_code, 403)
            blocked_get = client.get(
                f'/api/v1/digestions/{digestion_id}',
                headers={'X-API-Key': 'reader-key'},
            )
            self.assertEqual(blocked_get.status_code, 404)

            grant_response = client.post(
                f'/api/v1/digestions/{digestion_id}/acl',
                json={
                    'grantee_user_id': 'reader-user',
                    'can_query': 'true',
                    'can_read_sources': 'false',
                    'can_manage': 'false',
                },
                headers={'X-API-Key': 'owner-key'},
            )
            self.assertEqual(grant_response.status_code, 200)
            grant_payload = grant_response.get_json() or {}
            self.assertTrue(grant_payload['can_query'])
            self.assertFalse(grant_payload['can_read_sources'])
            self.assertFalse(grant_payload['can_manage'])
            self.assertEqual(grant_payload['grantee']['username'], 'reader-user')

            bad_grant_response = client.post(
                f'/api/v1/digestions/{digestion_id}/acl',
                json={'grantee_user_id': 'not-a-user', 'can_query': True},
                headers={'X-API-Key': 'owner-key'},
            )
            self.assertEqual(bad_grant_response.status_code, 400)
            self.assertEqual((bad_grant_response.get_json() or {}).get('reason'), 'grantee_not_eligible')

            remote_grant_response = client.post(
                f'/api/v1/digestions/{digestion_id}/acl',
                json={'grantee_user_id': 'remote-user', 'can_query': True},
                headers={'X-API-Key': 'owner-key'},
            )
            self.assertEqual(remote_grant_response.status_code, 400)
            self.assertEqual((remote_grant_response.get_json() or {}).get('reason'), 'grantee_not_eligible')

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

            shared_get = client.get(
                f'/api/v1/digestions/{digestion_id}',
                headers={'X-API-Key': 'reader-key'},
            )
            self.assertEqual(shared_get.status_code, 200)
            shared_payload = shared_get.get_json() or {}
            self.assertTrue(shared_payload['digestion']['access']['can_query'])

            sources_response = client.get(
                f'/api/v1/digestions/{digestion_id}/sources',
                headers={'X-API-Key': 'reader-key'},
            )
            self.assertEqual(sources_response.status_code, 403)

            second_grant_response = client.post(
                f'/api/v1/digestions/{digestion_id}/acl',
                json={'grantee_user_id': 'other-user', 'can_query': True, 'can_read_sources': True},
                headers={'X-API-Key': 'owner-key'},
            )
            self.assertEqual(second_grant_response.status_code, 200)
            acl_response = client.get(
                f'/api/v1/digestions/{digestion_id}/acl',
                headers={'X-API-Key': 'owner-key'},
            )
            self.assertEqual(acl_response.status_code, 200)
            acl_payload = acl_response.get_json() or {}
            self.assertEqual(acl_payload['count'], 2)
            self.assertEqual(
                {entry['user_id'] for entry in acl_payload['entries']},
                {'reader-user', 'other-user'},
            )

            revoke_response = client.delete(
                f'/api/v1/digestions/{digestion_id}/acl/reader-user',
                headers={'X-API-Key': 'owner-key'},
            )
            self.assertEqual(revoke_response.status_code, 200)
            self.assertTrue((revoke_response.get_json() or {})['revoked'])
            revoked_query_response = client.post(
                f'/api/v1/digestions/{digestion_id}/query',
                json={'query': 'document corpus', 'top_k': 2},
                headers={'X-API-Key': 'reader-key'},
            )
            self.assertEqual(revoked_query_response.status_code, 403)
            other_query_response = client.post(
                f'/api/v1/digestions/{digestion_id}/query',
                json={'query': 'document corpus', 'top_k': 2},
                headers={'X-API-Key': 'other-key'},
            )
            self.assertEqual(other_query_response.status_code, 200)
            final_acl_response = client.get(
                f'/api/v1/digestions/{digestion_id}/acl',
                headers={'X-API-Key': 'owner-key'},
            )
            self.assertEqual([entry['user_id'] for entry in final_acl_response.get_json()['entries']], ['other-user'])

    def test_digestion_rest_manager_can_add_vault_sources(self) -> None:
        manager_source = self._save_text(
            'api-agent-source.txt',
            'The agent added an external paper about retrieval latency and queue throughput.',
            owner='reader-user',
        )
        digestion = self.digestion_manager.create_digestion(
            'owner-user',
            name='API managed source corpus',
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
            app.secret_key = 'digestion-api-manager'
            app.config['DIGESTION_MANAGER'] = self.digestion_manager
            app.register_blueprint(create_api_blueprint(), url_prefix='/api/v1')
            client = app.test_client()

            response = client.post(
                f'/api/v1/digestions/{digestion["id"]}/sources',
                json={'source_file_ids': [manager_source.id]},
                headers={'X-API-Key': 'manager-key'},
            )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json() or {}
        self.assertTrue(payload['success'])
        self.assertEqual(payload['added'], 1)
        self.assertTrue(payload['sources'][0]['copied_to_owner_vault'])
        source = self.digestion_manager.list_sources(digestion['id'], user_id='reader-user')[0]
        self.assertEqual(self.file_manager.get_file(source['file_id']).uploaded_by, 'owner-user')

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
                            'content': (
                                'Reusable Digestions can normalize source material into agent context packs. '
                                'In a pilot, the workflow reduced setup time by 42% across 3 agent handoffs.'
                            ),
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

            with patch.object(
                self.digestion_manager,
                '_resolve_datapoint_llm_context',
                return_value=self._fake_datapoint_llm_context(),
            ), patch.object(
                self.digestion_manager,
                '_call_datapoint_llm',
                return_value=self._fake_workflow_datapoint_llm_response(),
            ):
                datapoints_response = client.post(
                    f'/api/v1/digestions/{digestion_id}/datapoints/extract',
                    json={'lens': 'workflow metrics'},
                    headers={'X-API-Key': 'owner-key'},
                )
            self.assertEqual(datapoints_response.status_code, 200)
            datapoints_payload = datapoints_response.get_json() or {}
            self.assertTrue(datapoints_payload['success'])
            self.assertEqual(datapoints_payload['output']['output_kind'], 'structured_datapoints')
            self.assertGreaterEqual(datapoints_payload['datapoint_count'], 1)

            datapoints_search_response = client.post(
                f'/api/v1/digestions/{digestion_id}/datapoints/search',
                json={'query': 'reduced setup time workflow', 'limit': 10},
                headers={'X-API-Key': 'owner-key'},
            )
            self.assertEqual(datapoints_search_response.status_code, 200)
            datapoints_search_payload = datapoints_search_response.get_json() or {}
            self.assertEqual(datapoints_search_payload['mode'], 'structured_datapoints')
            self.assertGreaterEqual(datapoints_search_payload['result_count'], 1)

            contribution_response = client.post(
                f'/api/v1/digestions/{digestion_id}/contributions',
                json={
                    'contributions': [
                        {
                            'kind': 'agent_note',
                            'title': 'Follow-up synthesis',
                            'content': 'Agent noted that setup-time reduction should be tracked as a reusable KPI.',
                            'facts': ['Setup time reduction is an operational KPI.'],
                            'datapoints': [
                                {
                                    'subject': 'setup time reduction',
                                    'claim': 'Setup time reduction was preserved as an agent-contributed KPI.',
                                    'measurements': ['setup time reduction'],
                                    'tags': ['workflow', 'kpi'],
                                }
                            ],
                        }
                    ],
                },
                headers={'X-API-Key': 'owner-key'},
            )
            self.assertEqual(contribution_response.status_code, 200)
            contribution_payload = contribution_response.get_json() or {}
            self.assertEqual(contribution_payload['materials_added'], 1)
            self.assertEqual(contribution_payload['datapoints_added'], 1)

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

    def test_grant_access_does_not_enumerate_user_existence(self) -> None:
        """Unknown and remote grantees return identical public errors."""
        digestion = self.digestion_manager.create_digestion(
            'owner-user',
            name='Enum test',
            provider='local_hash',
        )

        with self.assertRaises(DigestionError) as unknown_ctx:
            self.digestion_manager.grant_access(digestion['id'], 'owner-user', 'does-not-exist', can_query=True)
        with self.assertRaises(DigestionError) as remote_ctx:
            self.digestion_manager.grant_access(digestion['id'], 'owner-user', 'remote-user', can_query=True)

        self.assertEqual(unknown_ctx.exception.status_code, 400)
        self.assertEqual(remote_ctx.exception.status_code, 400)
        self.assertEqual(unknown_ctx.exception.reason, 'grantee_not_eligible')
        self.assertEqual(remote_ctx.exception.reason, 'grantee_not_eligible')
        self.assertEqual(str(unknown_ctx.exception), str(remote_ctx.exception))

    def test_output_responses_do_not_expose_created_by(self) -> None:
        """Output API rows should not leak the user ID that generated them."""
        source = self._save_text(
            'privacy-corpus.txt',
            'Output responses should not reveal which user generated them.',
        )
        digestion = self.digestion_manager.create_digestion(
            'owner-user',
            name='Privacy outputs',
            source_file_ids=[source.id],
            provider='local_hash',
        )
        self.digestion_manager.build_digestion(digestion['id'], 'owner-user')
        self.digestion_manager.grant_access(digestion['id'], 'owner-user', 'reader-user', can_query=True)

        for output in self.digestion_manager.list_outputs(digestion['id'], 'owner-user'):
            self.assertNotIn('created_by', output)
        for output in self.digestion_manager.list_outputs(digestion['id'], 'reader-user'):
            self.assertNotIn('created_by', output)
        self.assertNotIn('created_by', self.digestion_manager.get_output(digestion['id'], 'owner-user', 'agent_context'))

    def test_request_access_info_helps_unauthorized_agents_recover(self) -> None:
        """request_access_info returns actionable recovery info for query_denied scenarios."""
        source = self._save_text('sensitive-doc.txt', 'Sensitive research findings for authorized personnel only.')
        digestion = self.digestion_manager.create_digestion(
            'owner-user',
            name='Restricted corpus',
            source_file_ids=[source.id],
            provider='local_hash',
        )

        with self.assertRaisesRegex(Exception, 'query access'):
            self.digestion_manager.query(digestion['id'], 'other-user', 'sensitive findings')

        info = self.digestion_manager.request_access_info(digestion['id'], 'other-user')
        self.assertTrue(info['success'])
        self.assertEqual(info['digestion_id'], digestion['id'])
        self.assertEqual(info['owner_user_id'], 'owner-user')
        self.assertEqual(info['your_user_id'], 'other-user')
        self.assertFalse(info['already_has_query_access'])
        self.assertEqual(info['acl_grant_body']['grantee_user_id'], 'other-user')
        self.assertTrue(info['acl_grant_body']['can_query'])
        self.assertIn('guidance', info)

        self.digestion_manager.grant_access(digestion['id'], 'owner-user', 'other-user', can_query=True)
        self.assertTrue(
            self.digestion_manager.request_access_info(digestion['id'], 'other-user')['already_has_query_access']
        )

        with self.assertRaises(DigestionError) as ctx:
            self.digestion_manager.request_access_info('nonexistent-id', 'other-user')
        self.assertEqual(ctx.exception.reason, 'not_found')

    def test_rebuild_partial_failure_preserves_prior_indexed_chunks(self) -> None:
        """A rebuild failure for one source must not destroy other indexed sources."""
        file1 = self._save_text('stable-content.txt', 'Quantum silicon devices require hyperfine control.')
        file2 = self._save_text('fragile-content.txt', 'Garden tomatoes require soil and water to grow.')
        digestion = self.digestion_manager.create_digestion(
            'owner-user',
            name='Rebuild safety test',
            source_file_ids=[file1.id, file2.id],
            provider='local_hash',
            chunk_size=80,
            chunk_overlap=10,
        )

        initial = self.digestion_manager.build_digestion(digestion['id'], 'owner-user')
        self.assertTrue(initial['success'])
        initial_total_chunks = initial['stats']['chunks']
        original_index = self.digestion_manager._index_source

        def failing_index(digestion_obj, source_row, **kwargs):
            if str(source_row['file_id']) == file2.id:
                raise DigestionError('bytes unavailable', status_code=404, reason='source_bytes_missing')
            return original_index(digestion_obj, source_row, **kwargs)

        with patch.object(self.digestion_manager, '_index_source', side_effect=failing_index):
            partial = self.digestion_manager.build_digestion(digestion['id'], 'owner-user', rebuild=True)

        self.assertTrue(partial['success'])
        self.assertTrue(any(e['file_id'] == file2.id for e in partial['errors']))
        self.assertGreaterEqual(partial['stats']['chunks'], initial_total_chunks)
        result = self.digestion_manager.query(digestion['id'], 'owner-user', 'garden tomatoes', top_k=3)
        self.assertTrue(result['success'])
        self.assertGreaterEqual(result['result_count'], 1)

    def test_rebuild_unchanged_content_reuses_cached_embeddings(self) -> None:
        """Rebuilding unchanged content should use the embedding cache."""
        source = self._save_text('cached-content.txt', 'Hyperfine silicon control for quantum devices.')
        digestion = self.digestion_manager.create_digestion(
            'owner-user',
            name='Cache reuse test',
            source_file_ids=[source.id],
            provider='local_hash',
        )
        self.assertTrue(self.digestion_manager.build_digestion(digestion['id'], 'owner-user')['success'])

        embed_call_count = [0]
        original_embed = self.digestion_manager._embed_texts

        def counting_embed(texts, **kwargs):
            embed_call_count[0] += len(texts)
            return original_embed(texts, **kwargs)

        with patch.object(self.digestion_manager, '_embed_texts', side_effect=counting_embed):
            second = self.digestion_manager.build_digestion(digestion['id'], 'owner-user')

        self.assertTrue(second['success'])
        self.assertEqual(embed_call_count[0], 0)

    def test_digestion_build_progress_is_available_after_build(self) -> None:
        """Build operations should expose progress state for UI/API polling."""
        source = self._save_text('progress-content.txt', 'Silicon telemetry requires cited retrieval and source grounding.')
        digestion = self.digestion_manager.create_digestion(
            'owner-user',
            name='Progress test',
            source_file_ids=[source.id],
            provider='local_hash',
            chunk_size=80,
            chunk_overlap=0,
        )

        initial_progress = self.digestion_manager.get_operation_progress(digestion['id'], 'owner-user')
        self.assertEqual(initial_progress['operations']['build']['status'], 'idle')

        build = self.digestion_manager.build_digestion(digestion['id'], 'owner-user')
        self.assertTrue(build['success'])
        self.assertEqual(build['progress']['status'], 'completed')
        self.assertEqual(build['progress']['percent'], 100)
        self.assertGreaterEqual(build['progress']['details']['chunk_count'], 1)

        progress = self.digestion_manager.get_operation_progress(digestion['id'], 'owner-user')
        self.assertEqual(progress['operations']['build']['status'], 'completed')
        self.assertEqual(progress['operations']['build']['processed'], 1)
        self.assertEqual(progress['operations']['build']['total'], 1)
        listed = self.digestion_manager.list_digestions('owner-user')
        self.assertIn('operation_progress', listed[0])

        self.digestion_manager.grant_access(digestion['id'], 'owner-user', 'reader-user', can_query=True)
        self.digestion_manager._set_operation_progress(
            digestion['id'],
            'build',
            status='running',
            phase='reading_source',
            percent=30,
            processed=0,
            total=1,
            current_label='progress-content.txt',
            message='Reading progress-content.txt.',
            details={'errors': [{'file_id': source.id, 'error': 'private filename leaked'}], 'chunk_count': 2},
        )
        reader_progress = self.digestion_manager.get_operation_progress(digestion['id'], 'reader-user')
        reader_build = reader_progress['operations']['build']
        self.assertEqual(reader_build['status'], 'running')
        self.assertEqual(reader_build['current_label'], '')
        self.assertNotIn('progress-content.txt', reader_build['message'])
        self.assertNotIn('errors', reader_build['details'])
        self.assertEqual(reader_build['details']['chunk_count'], 2)

    def test_chunk_limit_build_reports_truncation_and_remains_queryable(self) -> None:
        """Chunk-limit truncation should surface clearly without making the index unusable."""
        import canopy.core.digestions as dig_mod

        file1 = self._save_text('big1.txt', 'silicon ' * 200)
        file2 = self._save_text('big2.txt', 'tomato ' * 200)
        digestion = self.digestion_manager.create_digestion(
            'owner-user',
            name='Limit test',
            source_file_ids=[file1.id, file2.id],
            provider='local_hash',
            chunk_size=80,
            chunk_overlap=0,
        )

        original_limit = dig_mod.MAX_CHUNKS_PER_BUILD
        dig_mod.MAX_CHUNKS_PER_BUILD = 3
        try:
            build = self.digestion_manager.build_digestion(digestion['id'], 'owner-user')
        finally:
            dig_mod.MAX_CHUNKS_PER_BUILD = original_limit

        self.assertTrue(build['success'])
        self.assertLessEqual(build['chunk_count'], 3)
        self.assertTrue(any('build_chunk_limit_reached' in e['error'] for e in build['errors']))
        result = self.digestion_manager.query(digestion['id'], 'owner-user', 'silicon', top_k=3)
        self.assertTrue(result['success'])
        self.assertGreaterEqual(result['result_count'], 1)

    def test_pdf_extraction_falls_back_when_pypdf_returns_no_text(self) -> None:
        """Some Windows/user PDFs expose sources but pypdf extracts no text; fall back before giving up."""

        class EmptyPdfReader:
            def __init__(self, *_args, **_kwargs):
                self.pages = [types.SimpleNamespace(extract_text=lambda: '')]

        pypdf_stub = types.ModuleType('pypdf')
        pypdf_stub.PdfReader = EmptyPdfReader

        fallback_segments = [ExtractedSegment(text='fallback academic PDF text about silicon teleoperation latency', page_label='p. 1')]
        with patch.dict(sys.modules, {'pypdf': pypdf_stub}):
            with patch.object(self.digestion_manager, '_extract_pdfminer_segments', return_value=fallback_segments) as fallback:
                segments = self.digestion_manager._extract_pdf_segments(b'%PDF-1.7 fake')

        self.assertEqual(segments, fallback_segments)
        fallback.assert_called_once()

        source = self.file_manager.save_file(b'%PDF-1.7 fake', 'academic-paper.pdf', 'application/pdf', 'owner-user')
        self.assertIsNotNone(source)
        digestion = self.digestion_manager.create_digestion(
            'owner-user',
            name='PDF fallback build',
            source_file_ids=[source.id],
            provider='local_hash',
            chunk_size=240,
            chunk_overlap=0,
        )
        with patch.dict(sys.modules, {'pypdf': pypdf_stub}):
            with patch.object(self.digestion_manager, '_extract_pdfminer_segments', return_value=fallback_segments):
                build = self.digestion_manager.build_digestion(digestion['id'], 'owner-user')

        self.assertTrue(build['success'])
        self.assertGreaterEqual(build['stats']['chunks'], 1)
        self.assertGreaterEqual(build['stats']['token_estimate'], 1)


if __name__ == '__main__':
    unittest.main()
