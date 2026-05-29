"""Regression tests for local @Canopy LLM compose support."""

import json
import os
import sqlite3
import sys
import types
import unittest
from typing import Any, Optional
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

from canopy.core.canopy_ai import (
    CANOPY_LLM_CURRENT_INFO_GUIDE,
    CANOPY_LLM_MODEL_OPTIONS,
    CANOPY_LLM_NO_WEB_SEARCH_CURRENT_INFO_GUIDE,
    CANOPY_LLM_POSTING_STRUCTURE_GUIDE,
    CANOPY_LLM_TRANSFORMATION_GUIDE,
    DEFAULT_BEDROCK_LLM_MODEL,
    DEFAULT_CANOPY_LLM_SYSTEM_PROMPT,
    CanopyLLMError,
    CanopyLLMManager,
)
from canopy.ui.routes import create_ui_blueprint


class _FakeDbManager:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def get_connection(self) -> sqlite3.Connection:
        return self._conn

    def get_instance_owner_user_id(self) -> str:
        return 'user-1'


class _FakeChannelManager:
    def __init__(self, allowed: bool = True) -> None:
        self.allowed = allowed

    def get_channel_access_decision(self, *, channel_id: str, user_id: str, require_membership: bool) -> dict[str, Any]:
        return {'allowed': self.allowed, 'reason': None if self.allowed else 'not_member'}

    def can_user_post_message(
        self,
        *,
        channel_id: str,
        user_id: str,
        parent_message_id: Optional[str],
        allow_admin: bool,
    ) -> dict[str, Any]:
        return {'allowed': self.allowed, 'reason': None if self.allowed else 'posting_denied'}


class _FakeLLMManager:
    def __init__(self) -> None:
        self.expand_calls: list[dict[str, Any]] = []
        self.capsule_calls: list[dict[str, Any]] = []
        self.saved_payloads: list[dict[str, Any]] = []
        self.saved_instance_payloads: list[dict[str, Any]] = []
        self.saved_digestion_payloads: list[dict[str, Any]] = []
        self.saved_instance_digestion_payloads: list[dict[str, Any]] = []

    @staticmethod
    def has_canopy_trigger(content: Any) -> bool:
        return '@Canopy' in str(content or '') or '@canopy' in str(content or '')

    def get_settings(self, user_id: str) -> dict[str, Any]:
        return {
            'provider': 'openai',
            'model': 'gpt-5-mini',
            'enabled': True,
            'api_key_configured': True,
            'web_search_enabled': True,
            'memory_enabled': True,
            'compose_memory': '',
            'system_prompt': 'Compose clean Canopy posts.',
            'updated_at': None,
        }

    def save_settings(self, user_id: str, **kwargs: Any) -> dict[str, Any]:
        self.saved_payloads.append({'user_id': user_id, **kwargs})
        return self.get_settings(user_id)

    def get_instance_settings(self) -> dict[str, Any]:
        return {
            'provider': 'openai',
            'model': 'gpt-5-mini',
            'enabled': True,
            'api_key_configured': True,
            'web_search_enabled': True,
            'system_prompt': 'Compose clean Canopy posts.',
            'updated_at': None,
            'updated_by': 'user-1',
        }

    def save_instance_settings(self, admin_user_id: str, **kwargs: Any) -> dict[str, Any]:
        self.saved_instance_payloads.append({'admin_user_id': admin_user_id, **kwargs})
        return self.get_instance_settings()

    def get_digestion_settings(self, user_id: str) -> dict[str, Any]:
        return {
            'provider': 'openai',
            'model': 'gpt-5-mini',
            'enabled': True,
            'api_key_configured': True,
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
            'updated_at': None,
        }

    def save_digestion_settings(self, user_id: str, **kwargs: Any) -> dict[str, Any]:
        self.saved_digestion_payloads.append({'user_id': user_id, **kwargs})
        return self.get_digestion_settings(user_id)

    def get_instance_digestion_settings(self) -> dict[str, Any]:
        return {
            'provider': 'openai',
            'model': 'gpt-5-mini',
            'enabled': True,
            'api_key_configured': True,
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
            'updated_at': None,
            'updated_by': 'user-1',
        }

    def save_instance_digestion_settings(self, admin_user_id: str, **kwargs: Any) -> dict[str, Any]:
        self.saved_instance_digestion_payloads.append({'admin_user_id': admin_user_id, **kwargs})
        return self.get_instance_digestion_settings()

    def expand_prompt(
        self,
        user_id: str,
        content: Any,
        *,
        channel_name: Optional[str] = None,
        context_label: Optional[str] = None,
    ) -> dict[str, Any]:
        self.expand_calls.append({
            'user_id': user_id,
            'content': content,
            'channel_name': channel_name,
            'context_label': context_label,
        })
        return {
            'content': 'Expanded post with @Forge and [signal]\ntitle: Useful finding\n[/signal]',
            'provider': 'openai',
            'model': 'gpt-5-mini',
        }

    def stream_expand_prompt(
        self,
        user_id: str,
        content: Any,
        *,
        channel_name: Optional[str] = None,
        context_label: Optional[str] = None,
    ):
        self.expand_calls.append({
            'user_id': user_id,
            'content': content,
            'channel_name': channel_name,
            'context_label': context_label,
            'stream': True,
        })
        yield {'type': 'status', 'message': 'Streaming test draft...'}
        yield {'type': 'delta', 'delta': 'Streamed '}
        yield {'type': 'delta', 'delta': 'draft'}
        yield {
            'type': 'done',
            'content': 'Streamed draft',
            'provider': 'openai',
            'model': 'gpt-5-mini',
        }

    def summarize_capsule(
        self,
        user_id: str,
        capsule_payload: dict[str, Any],
        *,
        channel_name: Optional[str] = None,
        context_label: Optional[str] = None,
    ) -> dict[str, Any]:
        self.capsule_calls.append({
            'user_id': user_id,
            'capsule_payload': capsule_payload,
            'channel_name': channel_name,
            'context_label': context_label,
        })
        return {
            'summary': {
                'title': 'Access checkpoint ready',
                'overview': 'Forge produced a compact checkpoint with one file output.',
                'key_update': 'One source file was uploaded and tagged for review.',
                'attention': '',
                'source_trail': '1 file source, 2 source posts',
                'next_action': 'Open the file or reply with review instructions.',
                'work_effort': {
                    'tag': 'Complete • 1 file',
                    'lede': 'Forge produced one source-linked output for review.',
                    'phases': [{'key': 'output', 'label': 'Output', 'count': 1, 'message_id': 'M1'}],
                },
            },
            'provider': 'openai',
            'model': 'gpt-5-mini',
            'credential_source': 'instance',
            'cached': False,
            'source_hash': 'server-hash',
        }


class TestCanopyLLMManager(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        self.db = _FakeDbManager(self.conn)

    def tearDown(self) -> None:
        self.conn.close()

    def test_local_api_key_is_encrypted_and_not_returned_in_settings(self) -> None:
        manager = CanopyLLMManager(self.db, 'test-secret')

        settings = manager.save_settings(
            'user-1',
            provider='openai',
            model='gpt-5-mini',
            enabled=True,
            api_key='sk-test-secret',
            system_prompt='Write concise posts.',
        )

        self.assertTrue(settings['enabled'])
        self.assertTrue(settings['api_key_configured'])
        self.assertTrue(settings['web_search_enabled'])
        self.assertIn(CANOPY_LLM_MODEL_OPTIONS[0], settings['model_options'])
        self.assertNotIn('api_key', settings)
        row = self.conn.execute(
            "SELECT api_key_ciphertext FROM user_llm_settings WHERE user_id = ?",
            ('user-1',),
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertNotIn('sk-test-secret', row['api_key_ciphertext'])
        self.assertEqual(manager._get_api_key('user-1'), 'sk-test-secret')

    def test_instance_fallback_key_is_encrypted_and_used_without_personal_key(self) -> None:
        manager = CanopyLLMManager(self.db, 'test-secret')

        settings = manager.save_instance_settings(
            'admin-1',
            provider='openai',
            model='gpt-5.4-mini',
            enabled=True,
            api_key='sk-instance-secret',
            web_search_enabled=False,
            system_prompt='Use the shared lab compose policy.',
        )

        self.assertTrue(settings['enabled'])
        self.assertTrue(settings['api_key_configured'])
        self.assertNotIn('api_key', settings)
        row = self.conn.execute(
            "SELECT api_key_ciphertext FROM instance_llm_settings WHERE id = ?",
            ('default',),
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertNotIn('sk-instance-secret', row['api_key_ciphertext'])

        user_settings = manager.get_settings('user-without-key')
        self.assertTrue(user_settings['instance_fallback_available'])
        self.assertTrue(user_settings['effective_enabled'])
        self.assertTrue(user_settings['using_instance_fallback'])

        context = manager._prepare_expand_context('user-without-key', '@Canopy draft a useful post')
        self.assertEqual(context['api_key'], 'sk-instance-secret')
        self.assertEqual(context['model'], 'gpt-5.4-mini')
        self.assertEqual(context['credential_source'], 'instance')
        self.assertIn('Use the shared lab compose policy.', context['system_prompt'])

    def test_instance_fallback_decrypt_failure_points_to_admin_settings(self) -> None:
        manager = CanopyLLMManager(self.db, 'test-secret')
        manager.save_instance_settings(
            'admin-1',
            provider='openai',
            model='gpt-5.4-mini',
            enabled=True,
            api_key='sk-instance-secret',
            system_prompt='Shared policy.',
        )

        manager_with_rotated_secret = CanopyLLMManager(self.db, 'different-secret')
        with self.assertRaises(Exception) as ctx:
            manager_with_rotated_secret._prepare_expand_context('user-without-key', '@Canopy draft a useful post')

        self.assertEqual(getattr(ctx.exception, 'reason', ''), 'api_key_decrypt_failed')
        self.assertIn('Admin > Instance AI Compose Fallback', str(ctx.exception))

    def test_personal_key_overrides_instance_fallback(self) -> None:
        manager = CanopyLLMManager(self.db, 'test-secret')
        manager.save_instance_settings(
            'admin-1',
            provider='openai',
            model='gpt-5.4-mini',
            enabled=True,
            api_key='sk-instance-secret',
            system_prompt='Shared policy.',
        )
        manager.save_settings(
            'user-1',
            provider='openai',
            model='gpt-5.5',
            enabled=True,
            api_key='sk-personal-secret',
            system_prompt='Personal policy.',
        )

        context = manager._prepare_expand_context('user-1', '@Canopy draft a useful post')
        self.assertEqual(context['api_key'], 'sk-personal-secret')
        self.assertEqual(context['model'], 'gpt-5.5')
        self.assertEqual(context['credential_source'], 'user')
        self.assertIn('Personal policy.', context['system_prompt'])

    def test_personal_model_can_use_instance_fallback_key(self) -> None:
        manager = CanopyLLMManager(self.db, 'test-secret')
        manager.save_instance_settings(
            'admin-1',
            provider='openai',
            model='gpt-5.4-mini',
            enabled=True,
            api_key='sk-instance-secret',
            system_prompt='Shared policy.',
        )
        manager.save_settings(
            'user-1',
            provider='openai',
            model='gpt-5.5',
            enabled=True,
            system_prompt='Personal policy.',
        )

        context = manager._prepare_expand_context('user-1', '@Canopy draft a useful post')
        self.assertEqual(context['api_key'], 'sk-instance-secret')
        self.assertEqual(context['model'], 'gpt-5.5')
        self.assertEqual(context['credential_source'], 'instance')
        self.assertIn('Personal policy.', context['system_prompt'])

    def test_digestion_settings_use_encrypted_personal_key_and_parameters(self) -> None:
        manager = CanopyLLMManager(self.db, 'test-secret')

        settings = manager.save_digestion_settings(
            'user-1',
            provider='openai',
            model='gpt-5.4-mini',
            enabled=True,
            api_key='sk-digest-secret',
            default_lens='extract device performance metrics',
            parameters={
                'max_chunks': 17,
                'max_datapoints': 33,
                'batch_chunks': 3,
                'batch_chars': 9000,
                'chunk_chars': 1600,
                'batch_records': 9,
                'max_output_tokens': 5000,
            },
        )

        self.assertTrue(settings['enabled'])
        self.assertTrue(settings['api_key_configured'])
        self.assertEqual(settings['parameters']['max_chunks'], 17)
        self.assertEqual(settings['parameters']['batch_records'], 9)
        self.assertNotIn('api_key', settings)
        row = self.conn.execute(
            "SELECT api_key_ciphertext FROM user_digestion_llm_settings WHERE user_id = ?",
            ('user-1',),
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertNotIn('sk-digest-secret', row['api_key_ciphertext'])

        resolved = manager._resolve_effective_digestion_settings('user-1')
        self.assertEqual(resolved['api_key'], 'sk-digest-secret')
        self.assertEqual(resolved['model'], 'gpt-5.4-mini')
        self.assertEqual(resolved['credential_source'], 'user')
        self.assertEqual(resolved['default_lens'], 'extract device performance metrics')

    def test_digestion_instance_fallback_can_supply_key_for_user_preferences(self) -> None:
        manager = CanopyLLMManager(self.db, 'test-secret')
        manager.save_instance_digestion_settings(
            'admin-1',
            provider='openai',
            model='gpt-5-mini',
            enabled=True,
            api_key='sk-instance-digest',
            default_lens='shared extraction lens',
            parameters={'max_chunks': 80, 'batch_records': 40},
        )
        manager.save_digestion_settings(
            'user-1',
            provider='openai',
            model='gpt-5.5',
            enabled=True,
            default_lens='personal extraction lens',
            parameters={'max_chunks': 12, 'batch_records': 6},
        )

        resolved = manager._resolve_effective_digestion_settings('user-1')
        self.assertEqual(resolved['api_key'], 'sk-instance-digest')
        self.assertEqual(resolved['model'], 'gpt-5.5')
        self.assertEqual(resolved['credential_source'], 'instance')
        self.assertEqual(resolved['default_lens'], 'personal extraction lens')
        self.assertEqual(resolved['parameters']['max_chunks'], 12)
        self.assertEqual(resolved['parameters']['batch_records'], 6)

    def test_compose_memory_adds_user_controlled_context_to_prompt(self) -> None:
        manager = CanopyLLMManager(self.db, 'test-secret')
        self.conn.executescript(
            """
            CREATE TABLE users (
                id TEXT PRIMARY KEY,
                username TEXT,
                display_name TEXT,
                account_type TEXT,
                password_hash TEXT
            );
            CREATE TABLE channels (id TEXT PRIMARY KEY, name TEXT);
            CREATE TABLE channel_members (channel_id TEXT, user_id TEXT, role TEXT);
            CREATE TABLE channel_messages (
                id TEXT PRIMARY KEY,
                channel_id TEXT,
                user_id TEXT,
                content TEXT,
                created_at TEXT
            );
            CREATE TABLE feed_posts (
                id TEXT PRIMARY KEY,
                author_id TEXT,
                content TEXT,
                visibility TEXT,
                created_at TEXT
            );
            """
        )
        self.conn.executemany(
            "INSERT INTO users (id, username, display_name, account_type, password_hash) VALUES (?, ?, ?, ?, ?)",
            [
                ('user-1', 'konrad', 'Konrad', 'human', 'hash'),
                ('agent-1', 'Forge_McClaw', 'Forge McClaw', 'agent', 'hash'),
            ],
        )
        self.conn.executemany("INSERT INTO channels (id, name) VALUES (?, ?)", [('chan-1', 'general'), ('chan-2', 'private-lab')])
        self.conn.executemany(
            "INSERT INTO channel_members (channel_id, user_id, role) VALUES (?, ?, ?)",
            [('chan-1', 'user-1', 'owner'), ('chan-1', 'agent-1', 'member')],
        )
        self.conn.executemany(
            "INSERT INTO channel_messages (id, channel_id, user_id, content, created_at) VALUES (?, ?, ?, ?, ?)",
            [
                ('msg-1', 'chan-1', 'user-1', 'Please keep the team moving with concise work product.', '2026-05-15T10:00:00'),
                ('msg-2', 'chan-2', 'user-1', 'Private lab phrase should not leave this channel.', '2026-05-15T10:05:00'),
            ],
        )
        self.conn.executemany(
            "INSERT INTO feed_posts (id, author_id, content, visibility, created_at) VALUES (?, ?, ?, ?, ?)",
            [
                ('post-1', 'user-1', 'Public feed style can be sampled.', 'network', '2026-05-15T10:10:00'),
                ('post-2', 'user-1', 'Private feed phrase should not be sampled.', 'private', '2026-05-15T10:11:00'),
            ],
        )
        self.conn.commit()
        manager.save_settings(
            'user-1',
            provider='openai',
            model='gpt-5-mini',
            enabled=True,
            api_key='sk-personal-secret',
            memory_enabled=True,
            compose_memory='Prefer concise lab-update style. Tag Forge for module packaging.',
            system_prompt='Personal policy.',
        )

        context = manager._prepare_expand_context('user-1', '@Canopy draft the next update', channel_name='general')

        self.assertIn('Node-local compose memory and team context:', context['prompt'])
        self.assertIn('Prefer concise lab-update style.', context['prompt'])
        self.assertIn('Forge McClaw / @Forge_McClaw', context['prompt'])
        self.assertIn('Recent writing examples from this user', context['prompt'])
        self.assertIn('Please keep the team moving', context['prompt'])
        self.assertIn('Public feed style can be sampled.', context['prompt'])
        self.assertNotIn('Private lab phrase should not leave this channel.', context['prompt'])
        self.assertNotIn('Private feed phrase should not be sampled.', context['prompt'])

    def test_compose_memory_can_be_disabled(self) -> None:
        manager = CanopyLLMManager(self.db, 'test-secret')
        manager.save_settings(
            'user-1',
            provider='openai',
            model='gpt-5-mini',
            enabled=True,
            api_key='sk-personal-secret',
            memory_enabled=False,
            compose_memory='Do not include this private preference.',
            system_prompt='Personal policy.',
        )

        context = manager._prepare_expand_context('user-1', '@Canopy draft the next update')

        self.assertNotIn('Node-local compose memory and team context:', context['prompt'])
        self.assertNotIn('Do not include this private preference.', context['prompt'])

    def test_bedrock_instance_fallback_can_use_environment_bearer_token(self) -> None:
        with patch.dict(os.environ, {
            'AWS_BEARER_TOKEN_BEDROCK': 'bedrock-env-token',
            'AWS_REGION': 'us-east-1',
        }, clear=False):
            manager = CanopyLLMManager(self.db, 'test-secret')
            settings = manager.save_instance_settings(
                'admin-1',
                provider='bedrock',
                model=DEFAULT_BEDROCK_LLM_MODEL,
                enabled=True,
                web_search_enabled=True,
                system_prompt='Use the shared Bedrock compose policy.',
            )

            self.assertTrue(settings['enabled'])
            self.assertTrue(settings['api_key_configured'])
            self.assertFalse(settings['key_saved'])
            self.assertTrue(settings['environment_credentials_available'])

            user_settings = manager.get_settings('user-without-key')
            self.assertTrue(user_settings['instance_fallback_available'])
            self.assertEqual(user_settings['instance_fallback_provider'], 'bedrock')

            context = manager._prepare_expand_context('user-without-key', '@Canopy draft a useful current weather post')

        self.assertEqual(context['provider'], 'bedrock')
        self.assertEqual(context['api_key'], '')
        self.assertEqual(context['model'], DEFAULT_BEDROCK_LLM_MODEL)
        self.assertEqual(context['credential_source'], 'instance')
        self.assertFalse(context['web_search_enabled'])
        self.assertIn('Use the shared Bedrock compose policy.', context['system_prompt'])
        self.assertIn(CANOPY_LLM_NO_WEB_SEARCH_CURRENT_INFO_GUIDE, context['system_prompt'])
        self.assertNotIn('use the hosted web search tool', context['system_prompt'])

    def test_bedrock_sigv4_environment_requires_region_even_with_endpoint(self) -> None:
        with patch.dict(os.environ, {
            'AWS_ACCESS_KEY_ID': 'AKIATEST',
            'AWS_SECRET_ACCESS_KEY': 'secret-test',
            'CANOPY_BEDROCK_RUNTIME_ENDPOINT': 'https://bedrock-runtime.us-west-2.amazonaws.com',
        }, clear=True):
            self.assertFalse(CanopyLLMManager._bedrock_environment_credentials_available())
            with self.assertRaises(Exception) as ctx:
                CanopyLLMManager._parse_bedrock_credentials('')
        self.assertEqual(getattr(ctx.exception, 'reason', ''), 'missing_bedrock_region')

    def test_bedrock_bare_api_key_secret_is_treated_as_bearer_token(self) -> None:
        with patch.dict(os.environ, {'AWS_REGION': 'us-east-1'}, clear=True):
            parsed = CanopyLLMManager._parse_bedrock_credentials('bedrock-raw-api-key')
        self.assertEqual(parsed['bearer_token'], 'bedrock-raw-api-key')
        self.assertEqual(parsed['region'], 'us-east-1')

    def test_bedrock_web_search_is_stored_as_disabled_regardless_of_input(self) -> None:
        manager = CanopyLLMManager(self.db, 'test-secret')

        user_settings = manager.save_settings(
            'user-2',
            provider='bedrock',
            model='amazon.nova-pro-v1:0',
            enabled=True,
            api_key='aws_bearer_token_bedrock=token;region=us-east-1',
            web_search_enabled=True,
        )
        self.assertFalse(user_settings['web_search_enabled'])
        self.assertFalse(manager.get_settings('user-2')['web_search_enabled'])

        instance_settings = manager.save_instance_settings(
            'admin-1',
            provider='bedrock',
            model='amazon.nova-pro-v1:0',
            enabled=True,
            api_key='aws_bearer_token_bedrock=token;region=us-east-1',
            web_search_enabled=True,
        )
        self.assertFalse(instance_settings['web_search_enabled'])

    def test_bedrock_error_fallback_does_not_return_raw_body(self) -> None:
        manager = CanopyLLMManager(self.db, 'test-secret')

        class _RawError:
            code = 403

            def read(self) -> bytes:
                return b'SENSITIVE AWS DETAIL account 123456789'

        message = manager._extract_bedrock_error(_RawError())  # type: ignore[arg-type]
        self.assertIn('HTTP 403', message)
        self.assertNotIn('SENSITIVE', message)
        self.assertNotIn('123456789', message)

    def test_bedrock_personal_credentials_are_saved_and_used(self) -> None:
        manager = CanopyLLMManager(self.db, 'test-secret')

        settings = manager.save_settings(
            'user-1',
            provider='bedrock',
            model='amazon.nova-pro-v1:0',
            enabled=True,
            api_key='aws_bearer_token_bedrock=personal-bedrock-token;region=us-west-2',
            web_search_enabled=True,
            system_prompt='Personal Bedrock policy.',
        )

        self.assertTrue(settings['api_key_configured'])
        self.assertEqual(settings['provider'], 'bedrock')
        self.assertNotIn('api_key', settings)
        context = manager._prepare_expand_context('user-1', '@Canopy post the latest lab update')
        self.assertEqual(context['provider'], 'bedrock')
        self.assertEqual(context['model'], 'amazon.nova-pro-v1:0')
        self.assertIn('personal-bedrock-token', context['api_key'])
        self.assertEqual(context['credential_source'], 'user')
        self.assertFalse(context['web_search_enabled'])

    def test_canopy_trigger_detection_and_strip(self) -> None:
        self.assertTrue(CanopyLLMManager.has_canopy_trigger('@Canopy draft this'))
        self.assertTrue(CanopyLLMManager.has_canopy_trigger('please @canopy: draft this'))
        self.assertFalse(CanopyLLMManager.has_canopy_trigger('email a@canopy.local'))
        self.assertEqual(
            CanopyLLMManager.strip_canopy_trigger('please @Canopy: draft this'),
            'please draft this',
        )

    def test_system_prompt_includes_canopy_structured_block_contract(self) -> None:
        self.assertIn('Canopy structured block rules:', DEFAULT_CANOPY_LLM_SYSTEM_PROMPT)
        self.assertIn('Default to plain text.', DEFAULT_CANOPY_LLM_SYSTEM_PROMPT)
        self.assertIn('Current-information and web-search rules:', DEFAULT_CANOPY_LLM_SYSTEM_PROMPT)
        self.assertIn('Draft-transformation rules:', DEFAULT_CANOPY_LLM_SYSTEM_PROMPT)
        self.assertIn('use the hosted web search tool', DEFAULT_CANOPY_LLM_SYSTEM_PROMPT)
        self.assertIn('Treat the user\'s text as an instruction to satisfy', DEFAULT_CANOPY_LLM_SYSTEM_PROMPT)
        self.assertIn('Never invent bracket tags', DEFAULT_CANOPY_LLM_SYSTEM_PROMPT)
        self.assertIn('[signal] requires type:, title:, summary:, and tags:.', DEFAULT_CANOPY_LLM_SYSTEM_PROMPT)
        custom = CanopyLLMManager._compose_system_prompt('Compose clean Canopy posts.')
        self.assertIn('Compose clean Canopy posts.', custom)
        self.assertIn(CANOPY_LLM_TRANSFORMATION_GUIDE, custom)
        self.assertIn(CANOPY_LLM_CURRENT_INFO_GUIDE, custom)
        self.assertIn(CANOPY_LLM_POSTING_STRUCTURE_GUIDE, custom)
        long_custom = CanopyLLMManager._compose_system_prompt('x' * 8000)
        self.assertLessEqual(len(long_custom), 4000)
        self.assertIn(CANOPY_LLM_POSTING_STRUCTURE_GUIDE, long_custom)

    def test_expand_context_frames_user_text_as_instruction_not_echo_material(self) -> None:
        manager = CanopyLLMManager(self.db, 'test-secret')
        manager.save_settings(
            'user-1',
            provider='openai',
            model='gpt-5-mini',
            api_key='sk-test',
            enabled=True,
            system_prompt='Compose clean Canopy posts.',
        )

        context = manager._prepare_expand_context('user-1', '@Canopy help me announce the meeting notes are ready', channel_name='general')

        self.assertIn('The following text is the user\'s instruction or rough draft.', context['prompt'])
        self.assertIn('Satisfy the instruction and write the final Canopy message body; do not merely repeat the instruction.', context['prompt'])
        self.assertIn('<<<\nhelp me announce the meeting notes are ready\n>>>', context['prompt'])
        self.assertIn('Return only the polished Canopy message body for the human to review.', context['prompt'])

    def test_schema_ready_flag_prevents_repeated_create_table(self) -> None:
        manager = CanopyLLMManager(self.db, 'test-secret')
        self.assertTrue(manager._schema_ready)

        with patch.object(self.db, 'get_connection', side_effect=AssertionError('schema touched')):
            manager._ensure_schema()

    def test_openai_response_read_is_bounded(self) -> None:
        manager = CanopyLLMManager(self.db, 'test-secret')

        class _LargeResponse:
            def __enter__(self) -> '_LargeResponse':
                return self

            def __exit__(self, *args: object) -> None:
                return None

            def read(self, size: int = -1) -> bytes:
                return b'{' + (b' ' * (size + 1))

        with patch('canopy.core.canopy_ai.urlopen', return_value=_LargeResponse()):
            with self.assertRaises(Exception) as ctx:
                manager._call_openai(
                    api_key='sk-test',
                    model='gpt-5-mini',
                    system_prompt='Compose.',
                    prompt='Draft.',
                )
        self.assertEqual(getattr(ctx.exception, 'reason', ''), 'provider_response_too_large')

    def test_openai_payload_can_enable_responses_web_search_tool(self) -> None:
        manager = CanopyLLMManager(self.db, 'test-secret')
        captured: dict[str, Any] = {}

        class _Response:
            def __enter__(self) -> '_Response':
                return self

            def __exit__(self, *args: object) -> None:
                return None

            def read(self, size: int = -1) -> bytes:
                return b'{"output_text":"Current weather summary with source link."}'

        def _fake_urlopen(request: Any, timeout: float = 0) -> _Response:
            captured['url'] = request.full_url
            captured['payload'] = json.loads(request.data.decode('utf-8'))
            captured['timeout'] = timeout
            return _Response()

        with patch('canopy.core.canopy_ai.urlopen', side_effect=_fake_urlopen):
            output = manager._call_openai(
                api_key='sk-test',
                model='gpt-5.4-mini',
                system_prompt='Compose.',
                prompt='Current node timestamp: 2026-05-02T12:00:00-07:00\n\nUser draft to transform into a Canopy post:\npost today weather in Vancouver',
                web_search_enabled=True,
            )

        self.assertIn('/responses', captured['url'])
        self.assertEqual(output, 'Current weather summary with source link.')
        self.assertEqual(captured['payload']['model'], 'gpt-5.4-mini')
        self.assertEqual(captured['payload']['tool_choice'], 'auto')
        self.assertEqual(captured['payload']['tools'][0]['type'], 'web_search')
        self.assertEqual(captured['payload']['tools'][0]['search_context_size'], 'low')
        self.assertEqual(captured['payload']['max_output_tokens'], 6000)
        self.assertEqual(captured['payload']['max_tool_calls'], 2)
        self.assertEqual(captured['timeout'], 90)

    def test_plain_prompts_skip_web_search_tool_to_reduce_latency(self) -> None:
        manager = CanopyLLMManager(self.db, 'test-secret')
        captured: dict[str, Any] = {}

        class _Response:
            def __enter__(self) -> '_Response':
                return self

            def __exit__(self, *args: object) -> None:
                return None

            def read(self, size: int = -1) -> bytes:
                return b'{"output_text":"Plain draft."}'

        def _fake_urlopen(request: Any, timeout: float = 0) -> _Response:
            captured['payload'] = json.loads(request.data.decode('utf-8'))
            return _Response()

        with patch('canopy.core.canopy_ai.urlopen', side_effect=_fake_urlopen):
            output = manager._call_openai(
                api_key='sk-test',
                model='gpt-5.4-mini',
                system_prompt='Compose.',
                prompt='User draft to transform into a Canopy message:\nwrite a friendly follow-up',
                web_search_enabled=manager._should_enable_web_search_for_prompt('write a friendly follow-up'),
            )

        self.assertEqual(output, 'Plain draft.')
        self.assertNotIn('tools', captured['payload'])
        self.assertNotIn('tool_choice', captured['payload'])
        self.assertEqual(captured['payload']['max_output_tokens'], 2600)

    def test_openai_repeated_timeouts_trigger_short_cooldown(self) -> None:
        manager = CanopyLLMManager(self.db, 'test-secret')
        calls = {'count': 0}

        def _timeout(*args: Any, **kwargs: Any) -> None:
            calls['count'] += 1
            raise TimeoutError('The read operation timed out')

        env = {
            'CANOPY_LLM_PROVIDER_FAILURE_THRESHOLD': '2',
            'CANOPY_LLM_PROVIDER_FAILURE_WINDOW_SECONDS': '60',
            'CANOPY_LLM_PROVIDER_COOLDOWN_SECONDS': '30',
        }
        with patch.dict(os.environ, env, clear=False):
            with patch('canopy.core.canopy_ai.urlopen', side_effect=_timeout):
                for _ in range(2):
                    with self.assertRaises(Exception) as ctx:
                        manager._call_openai(
                            api_key='sk-test',
                            model='gpt-5-mini',
                            system_prompt='Compose.',
                            prompt='Draft.',
                        )
                    self.assertEqual(getattr(ctx.exception, 'reason', ''), 'provider_unreachable')

                with self.assertRaises(Exception) as ctx:
                    manager._call_openai(
                        api_key='sk-test',
                        model='gpt-5-mini',
                        system_prompt='Compose.',
                        prompt='Draft.',
                    )

        self.assertEqual(getattr(ctx.exception, 'reason', ''), 'provider_cooldown')
        self.assertEqual(calls['count'], 2)

    def test_bedrock_bearer_token_payload_uses_converse_api_without_sigv4(self) -> None:
        manager = CanopyLLMManager(self.db, 'test-secret')
        captured: dict[str, Any] = {}

        class _Response:
            def __enter__(self) -> '_Response':
                return self

            def __exit__(self, *args: object) -> None:
                return None

            def read(self, size: int = -1) -> bytes:
                return b'{"output":{"message":{"content":[{"text":"Bedrock bearer draft."}]}}}'

        def _fake_urlopen(request: Any, timeout: float = 0) -> _Response:
            captured['url'] = request.full_url
            captured['payload'] = json.loads(request.data.decode('utf-8'))
            captured['timeout'] = timeout
            captured['authorization'] = request.get_header('Authorization')
            return _Response()

        with patch('canopy.core.canopy_ai.urlopen', side_effect=_fake_urlopen):
            output = manager._call_bedrock(
                credential_secret=json.dumps({
                    'bedrock_api_key': 'bedrock-api-key',
                    'region': 'us-west-2',
                }),
                model='amazon.nova-pro-v1:0',
                system_prompt='Compose.',
                prompt='Draft.',
            )

        self.assertEqual(output, 'Bedrock bearer draft.')
        self.assertIn('/model/amazon.nova-pro-v1%3A0/converse', captured['url'])
        self.assertEqual(captured['authorization'], 'Bearer bedrock-api-key')
        self.assertEqual(captured['payload']['messages'][0]['role'], 'user')
        self.assertEqual(captured['payload']['system'][0]['text'], 'Compose.')
        self.assertEqual(captured['payload']['inferenceConfig']['maxTokens'], 2600)
        self.assertEqual(captured['timeout'], 90)

    def test_bedrock_sigv4_payload_is_signed_and_extracts_text(self) -> None:
        manager = CanopyLLMManager(self.db, 'test-secret')
        captured: dict[str, Any] = {}

        class _Response:
            def __enter__(self) -> '_Response':
                return self

            def __exit__(self, *args: object) -> None:
                return None

            def read(self, size: int = -1) -> bytes:
                return b'{"output":{"message":{"content":[{"text":"Bedrock signed draft."}]}}}'

        def _fake_urlopen(request: Any, timeout: float = 0) -> _Response:
            captured['url'] = request.full_url
            captured['payload'] = json.loads(request.data.decode('utf-8'))
            captured['authorization'] = request.get_header('Authorization')
            captured['security_token'] = request.get_header('X-amz-security-token')
            return _Response()

        with patch('canopy.core.canopy_ai.urlopen', side_effect=_fake_urlopen):
            output = manager._call_bedrock(
                credential_secret='aws_access_key_id=AKIATEST;aws_secret_access_key=test-secret;aws_session_token=session-token;region=us-east-1',
                model='anthropic.claude-3-5-sonnet-20240620-v1:0',
                system_prompt='Compose.',
                prompt='Draft.',
            )

        self.assertEqual(output, 'Bedrock signed draft.')
        self.assertIn('/model/anthropic.claude-3-5-sonnet-20240620-v1%3A0/converse', captured['url'])
        self.assertTrue(str(captured['authorization']).startswith('AWS4-HMAC-SHA256 Credential=AKIATEST/'))
        self.assertIn('SignedHeaders=', captured['authorization'])
        self.assertEqual(captured['security_token'], 'session-token')
        self.assertEqual(captured['payload']['messages'][0]['content'][0]['text'], 'Draft.')

    def test_bedrock_stream_falls_back_to_single_delta_for_review_flow(self) -> None:
        with patch.dict(os.environ, {
            'AWS_BEARER_TOKEN_BEDROCK': 'bedrock-env-token',
            'AWS_REGION': 'us-east-1',
        }, clear=False):
            manager = CanopyLLMManager(self.db, 'test-secret')
            manager.save_instance_settings(
                'admin-1',
                provider='bedrock',
                model='amazon.nova-pro-v1:0',
                enabled=True,
                system_prompt='Shared Bedrock policy.',
            )
            with patch.object(manager, '_call_bedrock', return_value='Stream-compatible Bedrock draft.'):
                events = list(manager.stream_expand_prompt('user-without-key', '@Canopy draft this'))

        self.assertEqual(events[0], {'type': 'status', 'message': 'Generating draft with AWS Bedrock...'})
        self.assertEqual(events[1], {'type': 'delta', 'delta': 'Stream-compatible Bedrock draft.'})
        self.assertEqual(events[2]['type'], 'done')
        self.assertEqual(events[2]['provider'], 'bedrock')
        self.assertEqual(events[2]['credential_source'], 'instance')

    def test_openai_empty_tool_only_response_retries_with_final_instruction(self) -> None:
        manager = CanopyLLMManager(self.db, 'test-secret')
        captured_payloads: list[dict[str, Any]] = []

        class _Response:
            def __init__(self, payload: bytes) -> None:
                self.payload = payload

            def __enter__(self) -> '_Response':
                return self

            def __exit__(self, *args: object) -> None:
                return None

            def read(self, size: int = -1) -> bytes:
                return self.payload

        responses = [
            b'{"id":"resp_1","status":"completed","output":[{"type":"web_search_call","status":"completed"}]}',
            b'{"id":"resp_2","status":"completed","output_text":"Weather draft with source link."}',
        ]

        def _fake_urlopen(request: Any, timeout: float = 0) -> _Response:
            captured_payloads.append(json.loads(request.data.decode('utf-8')))
            return _Response(responses.pop(0))

        with patch('canopy.core.canopy_ai.urlopen', side_effect=_fake_urlopen):
            output = manager._call_openai(
                api_key='sk-test',
                model='gpt-5.4-mini',
                system_prompt='Compose.',
                prompt='User draft to transform into a Canopy post:\npost current weather in Vancouver',
                web_search_enabled=True,
            )

        self.assertEqual(output, 'Weather draft with source link.')
        self.assertEqual(len(captured_payloads), 2)
        self.assertIn('Generate the final Canopy post body now', captured_payloads[1]['input'])
        self.assertGreater(captured_payloads[1]['max_output_tokens'], captured_payloads[0]['max_output_tokens'])

    def test_openai_web_search_token_exhaustion_final_retry_disables_tools(self) -> None:
        manager = CanopyLLMManager(self.db, 'test-secret')
        captured_payloads: list[dict[str, Any]] = []

        class _Response:
            def __init__(self, payload: bytes) -> None:
                self.payload = payload

            def __enter__(self) -> '_Response':
                return self

            def __exit__(self, *args: object) -> None:
                return None

            def read(self, size: int = -1) -> bytes:
                return self.payload

        responses = [
            b'{"id":"resp_1","status":"incomplete","incomplete_details":{"reason":"max_output_tokens"},"output":[{"type":"web_search_call","status":"completed"}]}',
            b'{"id":"resp_2","status":"incomplete","incomplete_details":{"reason":"max_output_tokens"},"output":[{"type":"web_search_call","status":"completed"}]}',
            b'{"id":"resp_3","status":"completed","output_text":"Draft noting current facts still need verification."}',
        ]

        def _fake_urlopen(request: Any, timeout: float = 0) -> _Response:
            captured_payloads.append(json.loads(request.data.decode('utf-8')))
            return _Response(responses.pop(0))

        with patch('canopy.core.canopy_ai.urlopen', side_effect=_fake_urlopen):
            output = manager._call_openai(
                api_key='sk-test',
                model='gpt-5.4-mini',
                system_prompt='Compose.',
                prompt='User draft to transform into a Canopy post:\npost current weather in Vancouver',
                web_search_enabled=True,
            )

        self.assertEqual(output, 'Draft noting current facts still need verification.')
        self.assertIn('tools', captured_payloads[0])
        self.assertEqual(captured_payloads[1]['max_tool_calls'], 1)
        self.assertNotIn('tools', captured_payloads[2])
        self.assertNotIn('tool_choice', captured_payloads[2])
        self.assertIn('Do not use web search on this final retry', captured_payloads[2]['input'])

    def test_openai_pending_response_is_polled_before_retrying(self) -> None:
        manager = CanopyLLMManager(self.db, 'test-secret')
        request_methods: list[str] = []

        class _Response:
            def __init__(self, payload: bytes) -> None:
                self.payload = payload

            def __enter__(self) -> '_Response':
                return self

            def __exit__(self, *args: object) -> None:
                return None

            def read(self, size: int = -1) -> bytes:
                return self.payload

        responses = [
            b'{"id":"resp_pending","status":"in_progress","output":[]}',
            b'{"id":"resp_pending","status":"completed","output_text":"Final draft after poll."}',
        ]

        def _fake_urlopen(request: Any, timeout: float = 0) -> _Response:
            request_methods.append(request.get_method())
            return _Response(responses.pop(0))

        with patch.dict(os.environ, {'CANOPY_LLM_PENDING_POLL_DELAY_SECONDS': '0'}):
            with patch('canopy.core.canopy_ai.urlopen', side_effect=_fake_urlopen):
                output = manager._call_openai(
                    api_key='sk-test',
                    model='gpt-5.4-mini',
                    system_prompt='Compose.',
                    prompt='Draft.',
                    web_search_enabled=True,
                )

        self.assertEqual(output, 'Final draft after poll.')
        self.assertEqual(request_methods, ['POST', 'GET'])

    def test_openai_stream_yields_text_deltas_and_done(self) -> None:
        manager = CanopyLLMManager(self.db, 'test-secret')
        captured_payload: dict[str, Any] = {}

        class _StreamResponse:
            def __init__(self) -> None:
                self.lines = iter([
                    b'data: {"type":"response.web_search_call.completed"}\n',
                    b'\n',
                    b'data: {"type":"response.output_text.delta","delta":"Hello "}\n',
                    b'\n',
                    b'data: {"type":"response.output_text.delta","delta":"Canopy"}\n',
                    b'\n',
                    b'data: {"type":"response.completed","response":{"status":"completed"}}\n',
                    b'\n',
                ])

            def __enter__(self) -> '_StreamResponse':
                return self

            def __exit__(self, *args: object) -> None:
                return None

            def readline(self, size: int = -1) -> bytes:
                return next(self.lines, b'')

        def _fake_urlopen(request: Any, timeout: float = 0) -> _StreamResponse:
            captured_payload.update(json.loads(request.data.decode('utf-8')))
            return _StreamResponse()

        with patch('canopy.core.canopy_ai.urlopen', side_effect=_fake_urlopen):
            events = list(manager._stream_openai(
                api_key='sk-test',
                model='gpt-5.4-mini',
                system_prompt='Compose.',
                prompt='Draft.',
                web_search_enabled=True,
            ))

        self.assertTrue(captured_payload['stream'])
        self.assertEqual(captured_payload['max_output_tokens'], 6000)
        self.assertIn({'type': 'status', 'message': 'Checking current web sources...'}, events)
        self.assertEqual(
            [event for event in events if event.get('type') in ('delta', 'done')],
            [
                {'type': 'delta', 'delta': 'Hello '},
                {'type': 'delta', 'delta': 'Canopy'},
                {'type': 'done', 'content': 'Hello Canopy'},
            ],
        )

    def test_capsule_summary_uses_bounded_prompt_without_web_search(self) -> None:
        manager = CanopyLLMManager(self.db, 'test-secret')
        manager.save_settings(
            'user-1',
            provider='openai',
            model='gpt-5-mini',
            enabled=True,
            api_key='sk-test-secret',
            web_search_enabled=True,
            memory_enabled=True,
            compose_memory='Prefer short operational checkpoints.',
        )
        captured: dict[str, Any] = {}

        def _fake_openai(**kwargs: Any) -> str:
            captured.update(kwargs)
            return json.dumps({
                'title': 'Files ready for review',
                'overview': 'Gene uploaded new PDF sources and needs owner review.',
                'key_update': 'Two source files were added to the run.',
                'attention': 'Owner access is needed before indexing.',
                'source_trail': '2 file sources, 3 source posts',
                'next_action': 'Grant access or open the trace to inspect the files.',
                'work_effort': {
                    'tag': 'Needs review • 2 files',
                    'lede': 'New files were found, but owner access is still required.',
                    'phases': [
                        {'key': 'output', 'label': 'Files gathered', 'count': 2, 'message_id': 'M19'},
                        {'key': 'review', 'label': 'Owner review', 'count': 1, 'message_id': 'M19'},
                    ],
                },
                'workproducts': [
                    {'type': 'Finding', 'label': 'Open-access source list verified', 'message_id': 'M19'},
                ],
            })

        manager._call_openai = _fake_openai  # type: ignore[method-assign]
        result = manager.summarize_capsule(
            'user-1',
            {
                'capsule_id': 'M1-agent-run-0',
                'channel_id': 'general',
                'capsule_kind': 'reply',
                'level': 'Max',
                'work_effort': {
                    'tag': 'Fallback • 2 files',
                    'lede': 'Fallback work effort lede.',
                    'phases': [{'key': 'output', 'label': 'Output', 'count': 2, 'message_id': 'M19'}],
                },
                'deterministic': {
                    'title': 'Source and file work run',
                    'overview': 'Gene reached a checkpoint.',
                    'key_update': 'File work completed.',
                    'source_trail': '2 file sources',
                    'next_action': 'Open trace.',
                },
                'participants': [{'display_name': 'Gene McClaw', 'account_type': 'agent'}],
                'artifacts': [{'kind': 'file-ref', 'label': 'teleop.pdf', 'file_id': 'Fabc123456'}],
                'messages': [
                    {'id': f'M{i}', 'author': 'Gene McClaw', 'text': 'Uploaded and verified open access PDF source for teleoperation.'}
                    for i in range(20)
                ],
            },
            channel_name='fleetops-teleops',
        )

        self.assertFalse(captured['web_search_enabled'])
        self.assertLessEqual(captured['max_output_tokens'], 1600)
        self.assertLessEqual(captured['timeout_seconds'], 20)
        self.assertLessEqual(len(captured['prompt']), 9000)
        self.assertIn('Prefer short operational checkpoints.', captured['prompt'])
        self.assertIn('fleetops-teleops', captured['prompt'])
        self.assertIn('workproducts', captured['system_prompt'])
        self.assertIn('work_effort', captured['system_prompt'])
        self.assertIn('Fallback work effort lede.', captured['prompt'])
        self.assertEqual(result['summary']['title'], 'Files ready for review')
        self.assertEqual(result['summary']['work_effort']['tag'], 'Needs review • 2 files')
        self.assertEqual(result['summary']['work_effort']['phases'][0]['message_id'], 'M19')
        self.assertEqual(result['summary']['workproducts'][0]['label'], 'Open-access source list verified')
        self.assertEqual(result['summary']['workproducts'][0]['message_id'], 'M19')
        self.assertEqual(result['credential_source'], 'user')

    def test_capsule_summary_reuses_cache_for_same_source(self) -> None:
        manager = CanopyLLMManager(self.db, 'test-secret')
        manager.save_settings(
            'user-1',
            provider='openai',
            model='gpt-5-mini',
            enabled=True,
            api_key='sk-test-secret',
        )
        calls = {'count': 0}

        def _fake_openai(**kwargs: Any) -> str:
            calls['count'] += 1
            return json.dumps({
                'title': 'Cached title',
                'overview': 'Cached overview.',
                'key_update': 'Cached update.',
                'attention': '',
                'source_trail': '1 post',
                'next_action': 'Open the trace.',
            })

        manager._call_openai = _fake_openai  # type: ignore[method-assign]
        payload = {
            'capsule_id': 'M1-agent-run-0',
            'channel_id': 'general',
            'deterministic': {'title': 'Fallback', 'overview': 'Fallback overview.'},
            'messages': [{'id': 'M1', 'author': 'Forge', 'text': 'Done. Uploaded the requested output.'}],
        }

        first = manager.summarize_capsule('user-1', payload, channel_name='general')
        second = manager.summarize_capsule('user-1', payload, channel_name='general')

        self.assertFalse(first['cached'])
        self.assertTrue(second['cached'])
        self.assertEqual(calls['count'], 1)


class TestCanopyLLMComposeRoutes(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("CREATE TABLE channels (id TEXT PRIMARY KEY, name TEXT)")
        self.conn.execute("INSERT INTO channels (id, name) VALUES ('general', 'general')")
        self.conn.commit()
        self.db = _FakeDbManager(self.conn)
        self.channel_manager = _FakeChannelManager()
        self.llm_manager = _FakeLLMManager()
        self.components = (
            self.db,
            MagicMock(),
            MagicMock(),
            MagicMock(),
            self.channel_manager,
            MagicMock(),
            MagicMock(),
            MagicMock(),
            MagicMock(),
            MagicMock(),
            MagicMock(),
        )
        self.patcher = patch('canopy.ui.routes.get_app_components', return_value=self.components)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

        app = Flask(__name__)
        app.config['TESTING'] = True
        app.config['SECRET_KEY'] = 'test-secret'
        app.config['CANOPY_LLM_MANAGER'] = self.llm_manager
        app.register_blueprint(create_ui_blueprint())
        self.app = app
        self.client = app.test_client()

    def tearDown(self) -> None:
        self.conn.close()

    def _login(self, csrf_token: str = 'csrf-ok') -> str:
        with self.client.session_transaction() as sess:
            sess['authenticated'] = True
            sess['user_id'] = 'user-1'
            sess['_csrf_token'] = csrf_token
        return csrf_token

    def test_expand_endpoint_validates_channel_then_returns_expanded_draft(self) -> None:
        csrf = self._login()

        response = self.client.post(
            '/ajax/canopy_llm/expand',
            json={'channel_id': 'general', 'content': '@Canopy write an update'},
            headers={'X-CSRFToken': csrf},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json() or {}
        self.assertTrue(payload.get('success'))
        self.assertIn('@Forge', payload.get('content') or '')
        self.assertEqual(payload.get('model'), 'gpt-5-mini')
        self.assertEqual(self.llm_manager.expand_calls[0]['channel_name'], 'general')

    def test_expand_endpoint_allows_dm_compose_without_channel_id(self) -> None:
        csrf = self._login()

        response = self.client.post(
            '/ajax/canopy_llm/expand',
            json={'surface': 'dm', 'recipient_id': 'user-1', 'content': '@Canopy draft a private note'},
            headers={'X-CSRFToken': csrf},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json() or {}
        self.assertTrue(payload.get('success'))
        self.assertIsNone(self.llm_manager.expand_calls[-1]['channel_name'])
        self.assertEqual(self.llm_manager.expand_calls[-1]['context_label'], 'Personal scratchpad')

    def test_capsule_summary_endpoint_requires_channel_access_and_returns_summary(self) -> None:
        csrf = self._login()

        response = self.client.post(
            '/ajax/canopy_llm/capsule_summary',
            json={
                'channel_id': 'general',
                'capsule': {
                    'capsule_id': 'M1-agent-run-0',
                    'source_hash': 'client-hash',
                    'deterministic': {
                        'title': 'Source work run',
                        'overview': 'Fallback overview',
                    },
                    'messages': [{'id': 'M1', 'author': 'Gene', 'text': 'Uploaded a file and needs review.'}],
                },
            },
            headers={'X-CSRFToken': csrf},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json() or {}
        self.assertTrue(payload.get('success'))
        self.assertEqual(payload.get('summary', {}).get('title'), 'Access checkpoint ready')
        self.assertEqual(payload.get('credential_source'), 'instance')
        self.assertEqual(self.llm_manager.capsule_calls[0]['channel_name'], 'general')
        self.assertEqual(self.llm_manager.capsule_calls[0]['capsule_payload']['channel_id'], 'general')

    def test_capsule_summary_provider_failure_sets_user_scoped_fast_fallback(self) -> None:
        csrf = self._login()
        calls = {'count': 0}

        def _fail_capsule_summary(*args: Any, **kwargs: Any) -> dict[str, Any]:
            calls['count'] += 1
            raise CanopyLLMError('Could not reach OpenAI: timed out', status_code=502, reason='provider_unreachable')

        self.llm_manager.summarize_capsule = _fail_capsule_summary  # type: ignore[method-assign]
        payload = {
            'channel_id': 'general',
            'capsule': {
                'capsule_id': 'M1-agent-run-0',
                'source_hash': 'client-hash',
                'deterministic': {'title': 'Source work run', 'overview': 'Fallback overview'},
                'messages': [{'id': 'M1', 'author': 'Gene', 'text': 'Uploaded a file.'}],
            },
        }

        first = self.client.post('/ajax/canopy_llm/capsule_summary', json=payload, headers={'X-CSRFToken': csrf})
        self.assertEqual(first.status_code, 200)
        first_payload = first.get_json() or {}
        self.assertFalse(first_payload.get('success'))
        self.assertTrue(first_payload.get('fallback'))
        self.assertEqual(first_payload.get('reason'), 'provider_unreachable')

        second = self.client.post('/ajax/canopy_llm/capsule_summary', json=payload, headers={'X-CSRFToken': csrf})
        self.assertEqual(second.status_code, 200)
        second_payload = second.get_json() or {}
        self.assertFalse(second_payload.get('success'))
        self.assertTrue(second_payload.get('fallback'))
        self.assertEqual(second_payload.get('reason'), 'provider_cooldown')
        self.assertEqual(calls['count'], 1)

    def test_expand_stream_endpoint_returns_sse_draft_events(self) -> None:
        csrf = self._login()

        response = self.client.post(
            '/ajax/canopy_llm/expand_stream',
            json={'channel_id': 'general', 'content': '@Canopy write an update'},
            headers={'X-CSRFToken': csrf},
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn('text/event-stream', response.content_type)
        body = response.get_data(as_text=True)
        self.assertIn('"type":"status"', body)
        self.assertIn('"delta":"Streamed "', body)
        self.assertIn('"content":"Streamed draft"', body)
        self.assertTrue(self.llm_manager.expand_calls[0]['stream'])
        self.assertEqual(self.llm_manager.expand_calls[0]['channel_name'], 'general')

    def test_expand_endpoint_requires_canopy_trigger(self) -> None:
        csrf = self._login()

        response = self.client.post(
            '/ajax/canopy_llm/expand',
            json={'channel_id': 'general', 'content': 'write an update'},
            headers={'X-CSRFToken': csrf},
        )

        self.assertEqual(response.status_code, 400)
        payload = response.get_json() or {}
        self.assertEqual(payload.get('reason'), 'missing_trigger')

    def test_settings_endpoint_saves_local_settings(self) -> None:
        csrf = self._login()

        response = self.client.post(
            '/ajax/canopy_llm/settings',
            json={
                'provider': 'openai',
                'model': 'gpt-5-mini',
                'enabled': True,
                'web_search_enabled': False,
                'memory_enabled': False,
                'compose_memory': 'Prefer short operational updates.',
                'api_key': 'sk-test',
                'system_prompt': 'Be useful.',
            },
            headers={'X-CSRFToken': csrf},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json() or {}
        self.assertTrue(payload.get('success'))
        self.assertEqual(self.llm_manager.saved_payloads[0]['api_key'], 'sk-test')
        self.assertFalse(self.llm_manager.saved_payloads[0]['web_search_enabled'])
        self.assertFalse(self.llm_manager.saved_payloads[0]['memory_enabled'])
        self.assertEqual(self.llm_manager.saved_payloads[0]['compose_memory'], 'Prefer short operational updates.')

    def test_admin_instance_settings_endpoint_saves_fallback_settings(self) -> None:
        csrf = self._login()

        response = self.client.post(
            '/ajax/admin/canopy_llm/settings',
            json={
                'provider': 'openai',
                'model': 'gpt-5.4-mini',
                'enabled': True,
                'web_search_enabled': False,
                'api_key': 'sk-instance',
                'system_prompt': 'Shared fallback.',
            },
            headers={'X-CSRFToken': csrf},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json() or {}
        self.assertTrue(payload.get('success'))
        self.assertEqual(self.llm_manager.saved_instance_payloads[0]['admin_user_id'], 'user-1')
        self.assertEqual(self.llm_manager.saved_instance_payloads[0]['api_key'], 'sk-instance')
        self.assertFalse(self.llm_manager.saved_instance_payloads[0]['web_search_enabled'])

    def test_digestion_settings_endpoint_saves_local_settings(self) -> None:
        csrf = self._login()

        response = self.client.post(
            '/ajax/canopy_llm/digestion_settings',
            json={
                'provider': 'openai',
                'model': 'gpt-5.4-mini',
                'enabled': True,
                'api_key': 'sk-digest',
                'default_lens': 'extract lab measurements',
                'parameters': {
                    'max_chunks': 24,
                    'max_datapoints': 88,
                    'batch_chunks': 4,
                    'batch_chars': 12000,
                    'chunk_chars': 2000,
                    'batch_records': 12,
                    'max_output_tokens': 6000,
                },
            },
            headers={'X-CSRFToken': csrf},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json() or {}
        self.assertTrue(payload.get('success'))
        saved = self.llm_manager.saved_digestion_payloads[0]
        self.assertEqual(saved['user_id'], 'user-1')
        self.assertEqual(saved['api_key'], 'sk-digest')
        self.assertTrue(saved['enabled'])
        self.assertEqual(saved['default_lens'], 'extract lab measurements')
        self.assertEqual(saved['parameters']['max_chunks'], 24)
        self.assertEqual(saved['parameters']['batch_records'], 12)

    def test_digestion_settings_endpoint_parses_boolean_strings_strictly(self) -> None:
        csrf = self._login()

        response = self.client.post(
            '/ajax/canopy_llm/digestion_settings',
            json={
                'provider': 'openai',
                'model': 'gpt-5.4-mini',
                'enabled': 'false',
                'clear_api_key': 'false',
                'parameters': {'max_chunks': 24},
            },
            headers={'X-CSRFToken': csrf},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json() or {}
        self.assertTrue(payload.get('success'))
        saved = self.llm_manager.saved_digestion_payloads[0]
        self.assertFalse(saved['enabled'])
        self.assertFalse(saved['clear_api_key'])

    def test_admin_digestion_settings_endpoint_saves_fallback_settings(self) -> None:
        csrf = self._login()

        response = self.client.post(
            '/ajax/admin/canopy_llm/digestion_settings',
            json={
                'provider': 'openai',
                'model': 'gpt-5.4-mini',
                'enabled': True,
                'api_key': 'sk-instance-digest',
                'default_lens': 'extract reusable technical datapoints',
                'parameters': {
                    'max_chunks': 120,
                    'max_datapoints': 600,
                    'batch_chunks': 5,
                    'batch_chars': 16000,
                    'chunk_chars': 2400,
                    'batch_records': 20,
                    'max_output_tokens': 7000,
                },
            },
            headers={'X-CSRFToken': csrf},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json() or {}
        self.assertTrue(payload.get('success'))
        saved = self.llm_manager.saved_instance_digestion_payloads[0]
        self.assertEqual(saved['admin_user_id'], 'user-1')
        self.assertEqual(saved['api_key'], 'sk-instance-digest')
        self.assertTrue(saved['enabled'])
        self.assertEqual(saved['default_lens'], 'extract reusable technical datapoints')
        self.assertEqual(saved['parameters']['max_chunks'], 120)

    def test_admin_digestion_settings_endpoint_parses_boolean_strings_strictly(self) -> None:
        csrf = self._login()

        response = self.client.post(
            '/ajax/admin/canopy_llm/digestion_settings',
            json={
                'provider': 'openai',
                'model': 'gpt-5.4-mini',
                'enabled': 'false',
                'clear_api_key': 'false',
                'parameters': {'max_chunks': 80},
            },
            headers={'X-CSRFToken': csrf},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json() or {}
        self.assertTrue(payload.get('success'))
        saved = self.llm_manager.saved_instance_digestion_payloads[0]
        self.assertFalse(saved['enabled'])
        self.assertFalse(saved['clear_api_key'])
