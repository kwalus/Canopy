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
    CANOPY_LLM_POSTING_STRUCTURE_GUIDE,
    DEFAULT_CANOPY_LLM_SYSTEM_PROMPT,
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
        self.saved_payloads: list[dict[str, Any]] = []

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
            'system_prompt': 'Compose clean Canopy posts.',
            'updated_at': None,
        }

    def save_settings(self, user_id: str, **kwargs: Any) -> dict[str, Any]:
        self.saved_payloads.append({'user_id': user_id, **kwargs})
        return self.get_settings(user_id)

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
        self.assertIn('use the hosted web search tool', DEFAULT_CANOPY_LLM_SYSTEM_PROMPT)
        self.assertIn('Never invent bracket tags', DEFAULT_CANOPY_LLM_SYSTEM_PROMPT)
        self.assertIn('[signal] requires type:, title:, summary:, and tags:.', DEFAULT_CANOPY_LLM_SYSTEM_PROMPT)
        custom = CanopyLLMManager._compose_system_prompt('Compose clean Canopy posts.')
        self.assertIn('Compose clean Canopy posts.', custom)
        self.assertIn(CANOPY_LLM_CURRENT_INFO_GUIDE, custom)
        self.assertIn(CANOPY_LLM_POSTING_STRUCTURE_GUIDE, custom)
        long_custom = CanopyLLMManager._compose_system_prompt('x' * 8000)
        self.assertLessEqual(len(long_custom), 4000)
        self.assertIn(CANOPY_LLM_POSTING_STRUCTURE_GUIDE, long_custom)

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
