"""
Local-only LLM compose support for Canopy.

This module intentionally stores provider credentials in a node-local table
instead of user profiles or any mesh-synced identity surface.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import time
from datetime import datetime
from typing import Any, Iterator, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)


DEFAULT_CANOPY_LLM_MODEL = os.getenv('CANOPY_LLM_DEFAULT_MODEL', 'gpt-5-mini').strip() or 'gpt-5-mini'
DEFAULT_BEDROCK_LLM_MODEL = (
    os.getenv('CANOPY_BEDROCK_DEFAULT_MODEL', 'anthropic.claude-3-5-sonnet-20240620-v1:0').strip()
    or 'anthropic.claude-3-5-sonnet-20240620-v1:0'
)
INSTANCE_LLM_SETTINGS_ID = 'default'
INSTANCE_DIGESTION_LLM_SETTINGS_ID = 'default'
CANOPY_LLM_PROVIDER_OPTIONS = [
    {
        'id': 'openai',
        'label': 'OpenAI Responses',
        'credential_label': 'OpenAI API key',
        'credential_help': 'Paste an OpenAI API key. This enables hosted web search when the selected model supports it.',
        'supports_web_search': True,
    },
    {
        'id': 'bedrock',
        'label': 'AWS Bedrock',
        'credential_label': 'AWS Bedrock API key or credentials',
        'credential_help': (
            'Paste a Bedrock API key/bearer token, or JSON/KEY=VALUE credentials with '
            'aws_access_key_id, aws_secret_access_key, optional aws_session_token, and region. '
            'Admin instance fallback can also use server AWS environment credentials.'
        ),
        'supports_web_search': False,
    },
]
CANOPY_LLM_MODEL_OPTIONS = [
    {
        'id': 'gpt-5.4-mini',
        'label': 'GPT-5.4 mini - recommended lower-latency Responses model with web search support',
    },
    {
        'id': 'gpt-5.4',
        'label': 'GPT-5.4 - stronger Responses model with web search support',
    },
    {
        'id': 'gpt-5.5',
        'label': 'GPT-5.5 - flagship Responses model with web search support',
    },
    {
        'id': 'gpt-5-mini',
        'label': 'GPT-5 mini - existing economical default with Responses web search support',
    },
    {
        'id': 'gpt-5',
        'label': 'GPT-5 - previous-generation Responses model with web search support',
    },
    {
        'id': DEFAULT_BEDROCK_LLM_MODEL,
        'label': 'AWS Bedrock default - Converse API model or inference profile ID',
    },
    {
        'id': 'anthropic.claude-3-5-sonnet-20240620-v1:0',
        'label': 'AWS Bedrock Claude 3.5 Sonnet - requires Bedrock model access',
    },
    {
        'id': 'amazon.nova-pro-v1:0',
        'label': 'AWS Bedrock Nova Pro - requires Bedrock model access',
    },
]
CANOPY_DIGESTION_LLM_PARAMETER_LIMITS = {
    'max_chunks': {'default': 80, 'min': 1, 'max': 240},
    'max_datapoints': {'default': 400, 'min': 1, 'max': 1200},
    'batch_chunks': {'default': 6, 'min': 1, 'max': 24},
    'batch_chars': {'default': 18000, 'min': 4000, 'max': 60000},
    'chunk_chars': {'default': 2800, 'min': 800, 'max': 8000},
    'batch_records': {'default': 40, 'min': 1, 'max': 120},
    'max_output_tokens': {'default': 7000, 'min': 1200, 'max': 20000},
}
MAX_DIGESTION_LLM_LENS_CHARS = 800
DEFAULT_DIGESTION_LLM_LENS = (
    os.getenv('CANOPY_DIGESTION_DEFAULT_LENS', '').strip()
    or 'general reusable scientific, technical, operational, and decision-support datapoints'
)
CANOPY_LLM_POSTING_STRUCTURE_GUIDE = """
Canopy structured block rules:
- Default to plain text. Only emit a structured block when the user clearly asks to create a task, request, objective, signal, or handoff.
- Structured tags must appear alone on their own lines with no Markdown decoration: [task] ... [/task], [request] ... [/request], [objective] ... [/objective], [signal] ... [/signal], [handoff] ... [/handoff].
- Never invent bracket tags such as [status], [update], [artifact], [finding], [decision], or [request_accepted]. Use plain text, or use [signal] only when the content is truly a durable finding/report.
- Every structured block must have both an opening and closing tag.
- [task] requires title: and description:. Prefer priority: normal unless the user specifies urgency.
- [request] requires title: plus request: or required_output:.
- [objective] requires title:, description:, and a tasks: list using - [ ] items.
- [signal] requires type:, title:, summary:, and tags:.
- [handoff] requires title:, summary:, and next: lines.
- Valid examples:
  [task]\ntitle: Short action\ndescription: What needs doing\npriority: normal\n[/task]
  [request]\ntitle: Clear ask\nrequest: What you need from whom\nrequired_output: Expected reply/evidence\npriority: normal\n[/request]
  [signal]\ntype: finding\ntitle: Durable finding\nsummary: What was learned\ntags: update, evidence\n[/signal]
- If you are unsure whether a block will be valid, do not use a structured block; write a normal readable Canopy post instead.
""".strip()
CANOPY_LLM_CURRENT_INFO_GUIDE = """
Current-information and web-search rules:
- If the user asks for current, today, latest, recent, live, weather, market, schedule, price, availability, news, or other time-sensitive information, use the hosted web search tool when it is available instead of relying on model memory.
- If web search is unavailable or disabled, say in the draft that current facts were not checked; do not fabricate live information.
- When web search is used, incorporate the useful facts into the post and include concise source attribution or source links in the post body so readers can verify them.
- For local requests such as weather, traffic, restaurants, or events, use the location supplied by the user. If no location is supplied, ask for the location or write a short draft that explicitly says the location is needed.
- Keep the result suitable for a Canopy post: short, useful, source-aware, and ready for human review.
""".strip()
CANOPY_LLM_NO_WEB_SEARCH_CURRENT_INFO_GUIDE = """
Current-information and web-search rules:
- Hosted web search is not available in this compose path.
- If the user asks for current, today, latest, recent, live, weather, market, schedule, price, availability, news, or other time-sensitive information, say in the draft that current facts were not checked.
- Do not fabricate live information, source links, prices, schedules, or recent events.
- For local requests such as weather, traffic, restaurants, or events, use only the location supplied by the user and make clear that live details still need verification.
- Keep the result suitable for a Canopy post: short, useful, and ready for human review.
""".strip()
CANOPY_LLM_TRANSFORMATION_GUIDE = """
Draft-transformation rules:
- Treat the user's text as an instruction to satisfy, not as text to echo. Do the requested drafting, synthesis, research framing, or decision support work.
- Do not simply restate, summarize, or lightly reword the user's request unless the user explicitly asks for that.
- Add useful structure, specifics, and next-step clarity appropriate to a Canopy post while staying concise enough for a human to edit.
- If required facts or context are missing, write a useful draft that clearly names what is missing and asks for the smallest next input.
- Preserve @mentions, #channels, file names, URLs, and user-provided facts exactly unless fixing obvious punctuation around them.
""".strip()
DEFAULT_CANOPY_LLM_SYSTEM_PROMPT = (
    "You are Canopy's local compose assistant. Convert the user's draft into the exact "
    "Canopy message or post they should review and optionally send. Output only the final post body, with no "
    "preamble, no markdown fence, and no mention of these instructions. Remove the "
    "@Canopy trigger unless the user explicitly asks to discuss it. Preserve intentional "
    "Canopy syntax such as @mentions and #channels. Preserve or emit [task], [request], "
    "[objective], [signal], and [handoff] blocks only when they follow the structured block "
    "rules below, so Canopy can process them normally after "
    "the post is sent. Do not claim access to hidden files, private channel context, or "
    "mesh state unless the user included that context in the draft."
    f"\n\n{CANOPY_LLM_TRANSFORMATION_GUIDE}\n\n{CANOPY_LLM_CURRENT_INFO_GUIDE}\n\n{CANOPY_LLM_POSTING_STRUCTURE_GUIDE}"
)

CANOPY_TRIGGER_RE = re.compile(r'(?i)(^|\s)@canopy\b[:,]?\s*')
CURRENT_INFO_TRIGGER_RE = re.compile(
    r'(?i)\b('
    r'current|today|tonight|tomorrow|latest|recent|live|now|this\s+(?:morning|afternoon|evening|week|month|year)|'
    r'weather|traffic|news|price|prices|market|markets|stock|stocks|schedule|availability|available|'
    r'event|events|restaurant|restaurants|flight|flights|score|scores|release|released'
    r')\b'
)
MAX_SYSTEM_PROMPT_CHARS = 4000
MAX_LLM_INPUT_CHARS = 24000
MAX_COMPOSE_MEMORY_CHARS = 2000
MAX_COMPOSE_CONTEXT_CHARS = 6500
MAX_COMPOSE_TEAM_MEMBERS = 24
MAX_COMPOSE_STYLE_EXAMPLES = 5
_MAX_LLM_RESPONSE_BYTES = 512 * 1024  # 512 KiB is generous; typical responses are much smaller.
_OPENAI_PENDING_STATUSES = {'queued', 'in_progress'}
_OPENAI_WEB_SEARCH_TOOL_STATUSES = {
    'response.web_search_call.in_progress',
    'response.web_search_call.searching',
    'response.web_search_call.completed',
}


class CanopyLLMError(RuntimeError):
    """User-facing LLM compose failure with an HTTP-appropriate status."""

    def __init__(self, message: str, *, status_code: int = 400, reason: str = 'llm_error') -> None:
        super().__init__(message)
        self.status_code = int(status_code or 400)
        self.reason = reason


class CanopyLLMManager:
    """Manage local LLM compose settings and provider calls."""

    def __init__(self, db_manager: Any, secret_key: str | bytes | None) -> None:
        self.db_manager = db_manager
        self.secret_key = self._normalize_secret(secret_key)
        self._fernet: Optional[Fernet] = None
        self._schema_ready = False
        self._ensure_schema()

    @staticmethod
    def has_canopy_trigger(text: Any) -> bool:
        return bool(CANOPY_TRIGGER_RE.search(str(text or '')))

    @staticmethod
    def strip_canopy_trigger(text: Any) -> str:
        return CANOPY_TRIGGER_RE.sub(lambda match: match.group(1) or '', str(text or ''), count=1).strip()

    def get_settings(self, user_id: str) -> dict[str, Any]:
        user_id = str(user_id or '').strip()
        defaults = self._default_settings()
        instance_settings = self.get_instance_settings()
        if not user_id:
            return self._with_instance_summary(defaults, instance_settings)
        self._ensure_schema()
        with self.db_manager.get_connection() as conn:
            row = conn.execute(
                """
                SELECT provider, model, api_key_ciphertext, enabled, system_prompt, updated_at,
                       web_search_enabled, memory_enabled, compose_memory
                FROM user_llm_settings
                WHERE user_id = ?
                """,
                (user_id,),
            ).fetchone()
        if not row:
            return self._with_instance_summary(defaults, instance_settings)
        provider = self._normalize_provider_for_display(self._row_value(row, 'provider', 0, 'openai'))
        model = str(self._row_value(row, 'model', 1, DEFAULT_CANOPY_LLM_MODEL) or DEFAULT_CANOPY_LLM_MODEL).strip() or DEFAULT_CANOPY_LLM_MODEL
        ciphertext = self._row_value(row, 'api_key_ciphertext', 2, '')
        enabled = self._row_value(row, 'enabled', 3, 0)
        system_prompt = str(self._row_value(row, 'system_prompt', 4, '') or '').strip() or DEFAULT_CANOPY_LLM_SYSTEM_PROMPT
        updated_at = self._row_value(row, 'updated_at', 5, None)
        web_search_enabled = self._row_value(row, 'web_search_enabled', 6, 1)
        memory_enabled = self._row_value(row, 'memory_enabled', 7, 1)
        compose_memory = str(self._row_value(row, 'compose_memory', 8, '') or '').strip()
        return self._with_instance_summary({
            'provider': provider,
            'model': model[:120],
            'enabled': bool(enabled),
            'api_key_configured': self._provider_secret_configured(provider, ciphertext, allow_environment=False),
            'web_search_enabled': self._normalize_bool(web_search_enabled, default=True),
            'memory_enabled': self._normalize_bool(memory_enabled, default=True),
            'compose_memory': compose_memory[:MAX_COMPOSE_MEMORY_CHARS],
            'system_prompt': system_prompt[:MAX_SYSTEM_PROMPT_CHARS],
            'updated_at': updated_at,
            'model_options': CANOPY_LLM_MODEL_OPTIONS,
            'provider_options': CANOPY_LLM_PROVIDER_OPTIONS,
        }, instance_settings)

    def get_instance_settings(self) -> dict[str, Any]:
        """Return admin-managed node-local fallback settings without exposing the key."""
        defaults = self._default_instance_settings()
        self._ensure_schema()
        with self.db_manager.get_connection() as conn:
            row = conn.execute(
                """
                SELECT provider, model, api_key_ciphertext, enabled, system_prompt,
                       updated_at, web_search_enabled, updated_by
                FROM instance_llm_settings
                WHERE id = ?
                """,
                (INSTANCE_LLM_SETTINGS_ID,),
            ).fetchone()
        if not row:
            return defaults
        provider = self._normalize_provider_for_display(self._row_value(row, 'provider', 0, 'openai'))
        model = str(self._row_value(row, 'model', 1, DEFAULT_CANOPY_LLM_MODEL) or DEFAULT_CANOPY_LLM_MODEL).strip() or DEFAULT_CANOPY_LLM_MODEL
        ciphertext = self._row_value(row, 'api_key_ciphertext', 2, '')
        enabled = self._row_value(row, 'enabled', 3, 0)
        system_prompt = str(self._row_value(row, 'system_prompt', 4, '') or '').strip() or DEFAULT_CANOPY_LLM_SYSTEM_PROMPT
        updated_at = self._row_value(row, 'updated_at', 5, None)
        web_search_enabled = self._row_value(row, 'web_search_enabled', 6, 1)
        updated_by = self._row_value(row, 'updated_by', 7, None)
        return {
            'provider': provider,
            'model': model[:120],
            'enabled': bool(enabled),
            'api_key_configured': self._provider_secret_configured(provider, ciphertext, allow_environment=True),
            'key_saved': bool(str(ciphertext or '').strip()),
            'environment_credentials_available': bool(provider == 'bedrock' and self._bedrock_environment_credentials_available()),
            'web_search_enabled': self._normalize_bool(web_search_enabled, default=True),
            'system_prompt': system_prompt[:MAX_SYSTEM_PROMPT_CHARS],
            'updated_at': updated_at,
            'updated_by': updated_by,
            'model_options': CANOPY_LLM_MODEL_OPTIONS,
            'provider_options': CANOPY_LLM_PROVIDER_OPTIONS,
        }

    def get_digestion_settings(self, user_id: str) -> dict[str, Any]:
        """Return node-local structured Digestion extraction settings for a user."""
        user_id = str(user_id or '').strip()
        defaults = self._default_digestion_settings()
        instance_settings = self.get_instance_digestion_settings()
        if not user_id:
            return self._with_digestion_instance_summary(defaults, instance_settings)
        self._ensure_schema()
        with self.db_manager.get_connection() as conn:
            row = conn.execute(
                """
                SELECT provider, model, api_key_ciphertext, enabled, default_lens,
                       max_chunks, max_datapoints, batch_chunks, batch_chars,
                       chunk_chars, batch_records, max_output_tokens, updated_at
                FROM user_digestion_llm_settings
                WHERE user_id = ?
                """,
                (user_id,),
            ).fetchone()
        if not row:
            return self._with_digestion_instance_summary(defaults, instance_settings)
        provider = self._normalize_provider_for_display(self._row_value(row, 'provider', 0, 'openai'))
        model = self._normalize_model_for_display(self._row_value(row, 'model', 1, None), provider=provider)
        ciphertext = self._row_value(row, 'api_key_ciphertext', 2, '')
        parameters = self._normalize_digestion_parameters({
            'max_chunks': self._row_value(row, 'max_chunks', 5, None),
            'max_datapoints': self._row_value(row, 'max_datapoints', 6, None),
            'batch_chunks': self._row_value(row, 'batch_chunks', 7, None),
            'batch_chars': self._row_value(row, 'batch_chars', 8, None),
            'chunk_chars': self._row_value(row, 'chunk_chars', 9, None),
            'batch_records': self._row_value(row, 'batch_records', 10, None),
            'max_output_tokens': self._row_value(row, 'max_output_tokens', 11, None),
        })
        return self._with_digestion_instance_summary({
            'provider': provider,
            'model': model,
            'enabled': bool(self._row_value(row, 'enabled', 3, 0)),
            'api_key_configured': self._provider_secret_configured(provider, ciphertext, allow_environment=False),
            'default_lens': self._normalize_digestion_lens(self._row_value(row, 'default_lens', 4, '')),
            'parameters': parameters,
            'updated_at': self._row_value(row, 'updated_at', 12, None),
            'model_options': CANOPY_LLM_MODEL_OPTIONS,
            'provider_options': CANOPY_LLM_PROVIDER_OPTIONS,
            'parameter_limits': CANOPY_DIGESTION_LLM_PARAMETER_LIMITS,
        }, instance_settings)

    def get_instance_digestion_settings(self) -> dict[str, Any]:
        """Return admin-managed node-local fallback settings for Digestion extraction."""
        defaults = self._default_instance_digestion_settings()
        self._ensure_schema()
        with self.db_manager.get_connection() as conn:
            row = conn.execute(
                """
                SELECT provider, model, api_key_ciphertext, enabled, default_lens,
                       max_chunks, max_datapoints, batch_chunks, batch_chars,
                       chunk_chars, batch_records, max_output_tokens, updated_by, updated_at
                FROM instance_digestion_llm_settings
                WHERE id = ?
                """,
                (INSTANCE_DIGESTION_LLM_SETTINGS_ID,),
            ).fetchone()
        if not row:
            return defaults
        provider = self._normalize_provider_for_display(self._row_value(row, 'provider', 0, 'openai'))
        model = self._normalize_model_for_display(self._row_value(row, 'model', 1, None), provider=provider)
        ciphertext = self._row_value(row, 'api_key_ciphertext', 2, '')
        parameters = self._normalize_digestion_parameters({
            'max_chunks': self._row_value(row, 'max_chunks', 5, None),
            'max_datapoints': self._row_value(row, 'max_datapoints', 6, None),
            'batch_chunks': self._row_value(row, 'batch_chunks', 7, None),
            'batch_chars': self._row_value(row, 'batch_chars', 8, None),
            'chunk_chars': self._row_value(row, 'chunk_chars', 9, None),
            'batch_records': self._row_value(row, 'batch_records', 10, None),
            'max_output_tokens': self._row_value(row, 'max_output_tokens', 11, None),
        })
        return {
            'provider': provider,
            'model': model,
            'enabled': bool(self._row_value(row, 'enabled', 3, 0)),
            'api_key_configured': self._provider_secret_configured(provider, ciphertext, allow_environment=True),
            'key_saved': bool(str(ciphertext or '').strip()),
            'environment_credentials_available': bool(provider == 'bedrock' and self._bedrock_environment_credentials_available()),
            'default_lens': self._normalize_digestion_lens(self._row_value(row, 'default_lens', 4, '')),
            'parameters': parameters,
            'updated_by': self._row_value(row, 'updated_by', 12, None),
            'updated_at': self._row_value(row, 'updated_at', 13, None),
            'model_options': CANOPY_LLM_MODEL_OPTIONS,
            'provider_options': CANOPY_LLM_PROVIDER_OPTIONS,
            'parameter_limits': CANOPY_DIGESTION_LLM_PARAMETER_LIMITS,
        }

    def save_settings(
        self,
        user_id: str,
        *,
        provider: Any = 'openai',
        model: Any = None,
        enabled: Any = False,
        api_key: Optional[str] = None,
        clear_api_key: bool = False,
        system_prompt: Any = None,
        web_search_enabled: Any = True,
        memory_enabled: Any = None,
        compose_memory: Any = None,
    ) -> dict[str, Any]:
        user_id = str(user_id or '').strip()
        if not user_id:
            raise CanopyLLMError('Sign in before configuring Canopy AI Compose.', status_code=401, reason='not_authenticated')
        provider_clean = self._normalize_provider(provider)
        model_clean = self._normalize_model(model, provider=provider_clean)
        prompt_clean = self._normalize_system_prompt(system_prompt)
        enabled_clean = 1 if bool(enabled) else 0
        web_search_enabled_clean = 1 if self._normalize_bool(web_search_enabled, default=True) else 0
        if provider_clean == 'bedrock':
            web_search_enabled_clean = 0

        self._ensure_schema()
        with self.db_manager.get_connection() as conn:
            existing = conn.execute(
                "SELECT api_key_ciphertext, compose_memory, memory_enabled FROM user_llm_settings WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            existing_ciphertext = ''
            existing_compose_memory = ''
            existing_memory_enabled = 1
            if existing:
                existing_ciphertext = str(self._row_value(existing, 'api_key_ciphertext', 0, '') or '').strip()
                existing_compose_memory = str(self._row_value(existing, 'compose_memory', 1, '') or '')
                existing_memory_enabled = self._row_value(existing, 'memory_enabled', 2, 1)
            memory_enabled_clean = 1 if self._normalize_bool(
                existing_memory_enabled if memory_enabled is None else memory_enabled,
                default=True,
            ) else 0
            compose_memory_clean = self._normalize_compose_memory(
                existing_compose_memory if compose_memory is None else compose_memory
            )

            api_key_clean = str(api_key or '').strip() if api_key is not None else ''
            if clear_api_key:
                ciphertext = None
            elif api_key_clean:
                ciphertext = self._encrypt(api_key_clean)
            else:
                ciphertext = existing_ciphertext or None

            conn.execute(
                """
                INSERT INTO user_llm_settings (
                    user_id, provider, model, api_key_ciphertext, enabled, system_prompt,
                    web_search_enabled, memory_enabled, compose_memory, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(user_id) DO UPDATE SET
                    provider = excluded.provider,
                    model = excluded.model,
                    api_key_ciphertext = excluded.api_key_ciphertext,
                    enabled = excluded.enabled,
                    system_prompt = excluded.system_prompt,
                    web_search_enabled = excluded.web_search_enabled,
                    memory_enabled = excluded.memory_enabled,
                    compose_memory = excluded.compose_memory,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    user_id,
                    provider_clean,
                    model_clean,
                    ciphertext,
                    enabled_clean,
                    prompt_clean,
                    web_search_enabled_clean,
                    memory_enabled_clean,
                    compose_memory_clean,
                ),
            )
            conn.commit()
        return self.get_settings(user_id)

    def save_instance_settings(
        self,
        admin_user_id: str,
        *,
        provider: Any = 'openai',
        model: Any = None,
        enabled: Any = False,
        api_key: Optional[str] = None,
        clear_api_key: bool = False,
        system_prompt: Any = None,
        web_search_enabled: Any = True,
    ) -> dict[str, Any]:
        """Save admin-managed node-local fallback settings for users without personal keys."""
        admin_user_id = str(admin_user_id or '').strip()
        provider_clean = self._normalize_provider(provider)
        model_clean = self._normalize_model(model, provider=provider_clean)
        prompt_clean = self._normalize_system_prompt(system_prompt)
        enabled_clean = 1 if bool(enabled) else 0
        web_search_enabled_clean = 1 if self._normalize_bool(web_search_enabled, default=True) else 0
        if provider_clean == 'bedrock':
            web_search_enabled_clean = 0

        self._ensure_schema()
        with self.db_manager.get_connection() as conn:
            existing = conn.execute(
                "SELECT api_key_ciphertext FROM instance_llm_settings WHERE id = ?",
                (INSTANCE_LLM_SETTINGS_ID,),
            ).fetchone()
            existing_ciphertext = ''
            if existing:
                existing_ciphertext = str(self._row_value(existing, 'api_key_ciphertext', 0, '') or '').strip()

            api_key_clean = str(api_key or '').strip() if api_key is not None else ''
            if clear_api_key:
                ciphertext = None
            elif api_key_clean:
                ciphertext = self._encrypt(api_key_clean)
            else:
                ciphertext = existing_ciphertext or None

            conn.execute(
                """
                INSERT INTO instance_llm_settings (
                    id, provider, model, api_key_ciphertext, enabled, system_prompt,
                    web_search_enabled, updated_by, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(id) DO UPDATE SET
                    provider = excluded.provider,
                    model = excluded.model,
                    api_key_ciphertext = excluded.api_key_ciphertext,
                    enabled = excluded.enabled,
                    system_prompt = excluded.system_prompt,
                    web_search_enabled = excluded.web_search_enabled,
                    updated_by = excluded.updated_by,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    INSTANCE_LLM_SETTINGS_ID,
                    provider_clean,
                    model_clean,
                    ciphertext,
                    enabled_clean,
                    prompt_clean,
                    web_search_enabled_clean,
                    admin_user_id or None,
                ),
            )
            conn.commit()
        return self.get_instance_settings()

    def save_digestion_settings(
        self,
        user_id: str,
        *,
        provider: Any = 'openai',
        model: Any = None,
        enabled: Any = False,
        api_key: Optional[str] = None,
        clear_api_key: bool = False,
        default_lens: Any = None,
        parameters: Optional[dict[str, Any]] = None,
        max_chunks: Any = None,
        max_datapoints: Any = None,
        batch_chunks: Any = None,
        batch_chars: Any = None,
        chunk_chars: Any = None,
        batch_records: Any = None,
        max_output_tokens: Any = None,
    ) -> dict[str, Any]:
        """Save a user's node-local Digestion extraction provider and cost controls."""
        user_id = str(user_id or '').strip()
        if not user_id:
            raise CanopyLLMError('Sign in before configuring Digestion AI extraction.', status_code=401, reason='not_authenticated')
        provider_clean = self._normalize_provider(provider)
        model_clean = self._normalize_model(model, provider=provider_clean)
        lens_clean = self._normalize_digestion_lens(default_lens)
        params_clean = self._normalize_digestion_parameters(parameters, **{
            'max_chunks': max_chunks,
            'max_datapoints': max_datapoints,
            'batch_chunks': batch_chunks,
            'batch_chars': batch_chars,
            'chunk_chars': chunk_chars,
            'batch_records': batch_records,
            'max_output_tokens': max_output_tokens,
        })
        enabled_clean = 1 if bool(enabled) else 0

        self._ensure_schema()
        with self.db_manager.get_connection() as conn:
            existing = conn.execute(
                "SELECT api_key_ciphertext FROM user_digestion_llm_settings WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            existing_ciphertext = str(self._row_value(existing, 'api_key_ciphertext', 0, '') or '').strip() if existing else ''
            api_key_clean = str(api_key or '').strip() if api_key is not None else ''
            if clear_api_key:
                ciphertext = None
            elif api_key_clean:
                ciphertext = self._encrypt(api_key_clean)
            else:
                ciphertext = existing_ciphertext or None

            conn.execute(
                """
                INSERT INTO user_digestion_llm_settings (
                    user_id, provider, model, api_key_ciphertext, enabled, default_lens,
                    max_chunks, max_datapoints, batch_chunks, batch_chars, chunk_chars,
                    batch_records, max_output_tokens, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(user_id) DO UPDATE SET
                    provider = excluded.provider,
                    model = excluded.model,
                    api_key_ciphertext = excluded.api_key_ciphertext,
                    enabled = excluded.enabled,
                    default_lens = excluded.default_lens,
                    max_chunks = excluded.max_chunks,
                    max_datapoints = excluded.max_datapoints,
                    batch_chunks = excluded.batch_chunks,
                    batch_chars = excluded.batch_chars,
                    chunk_chars = excluded.chunk_chars,
                    batch_records = excluded.batch_records,
                    max_output_tokens = excluded.max_output_tokens,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    user_id,
                    provider_clean,
                    model_clean,
                    ciphertext,
                    enabled_clean,
                    lens_clean,
                    params_clean['max_chunks'],
                    params_clean['max_datapoints'],
                    params_clean['batch_chunks'],
                    params_clean['batch_chars'],
                    params_clean['chunk_chars'],
                    params_clean['batch_records'],
                    params_clean['max_output_tokens'],
                ),
            )
            conn.commit()
        return self.get_digestion_settings(user_id)

    def save_instance_digestion_settings(
        self,
        admin_user_id: str,
        *,
        provider: Any = 'openai',
        model: Any = None,
        enabled: Any = False,
        api_key: Optional[str] = None,
        clear_api_key: bool = False,
        default_lens: Any = None,
        parameters: Optional[dict[str, Any]] = None,
        max_chunks: Any = None,
        max_datapoints: Any = None,
        batch_chunks: Any = None,
        batch_chars: Any = None,
        chunk_chars: Any = None,
        batch_records: Any = None,
        max_output_tokens: Any = None,
    ) -> dict[str, Any]:
        """Save admin-managed node-local Digestion extraction fallback settings."""
        admin_user_id = str(admin_user_id or '').strip()
        provider_clean = self._normalize_provider(provider)
        model_clean = self._normalize_model(model, provider=provider_clean)
        lens_clean = self._normalize_digestion_lens(default_lens)
        params_clean = self._normalize_digestion_parameters(parameters, **{
            'max_chunks': max_chunks,
            'max_datapoints': max_datapoints,
            'batch_chunks': batch_chunks,
            'batch_chars': batch_chars,
            'chunk_chars': chunk_chars,
            'batch_records': batch_records,
            'max_output_tokens': max_output_tokens,
        })
        enabled_clean = 1 if bool(enabled) else 0

        self._ensure_schema()
        with self.db_manager.get_connection() as conn:
            existing = conn.execute(
                "SELECT api_key_ciphertext FROM instance_digestion_llm_settings WHERE id = ?",
                (INSTANCE_DIGESTION_LLM_SETTINGS_ID,),
            ).fetchone()
            existing_ciphertext = str(self._row_value(existing, 'api_key_ciphertext', 0, '') or '').strip() if existing else ''
            api_key_clean = str(api_key or '').strip() if api_key is not None else ''
            if clear_api_key:
                ciphertext = None
            elif api_key_clean:
                ciphertext = self._encrypt(api_key_clean)
            else:
                ciphertext = existing_ciphertext or None

            conn.execute(
                """
                INSERT INTO instance_digestion_llm_settings (
                    id, provider, model, api_key_ciphertext, enabled, default_lens,
                    max_chunks, max_datapoints, batch_chunks, batch_chars, chunk_chars,
                    batch_records, max_output_tokens, updated_by, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(id) DO UPDATE SET
                    provider = excluded.provider,
                    model = excluded.model,
                    api_key_ciphertext = excluded.api_key_ciphertext,
                    enabled = excluded.enabled,
                    default_lens = excluded.default_lens,
                    max_chunks = excluded.max_chunks,
                    max_datapoints = excluded.max_datapoints,
                    batch_chunks = excluded.batch_chunks,
                    batch_chars = excluded.batch_chars,
                    chunk_chars = excluded.chunk_chars,
                    batch_records = excluded.batch_records,
                    max_output_tokens = excluded.max_output_tokens,
                    updated_by = excluded.updated_by,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    INSTANCE_DIGESTION_LLM_SETTINGS_ID,
                    provider_clean,
                    model_clean,
                    ciphertext,
                    enabled_clean,
                    lens_clean,
                    params_clean['max_chunks'],
                    params_clean['max_datapoints'],
                    params_clean['batch_chunks'],
                    params_clean['batch_chars'],
                    params_clean['chunk_chars'],
                    params_clean['batch_records'],
                    params_clean['max_output_tokens'],
                    admin_user_id or None,
                ),
            )
            conn.commit()
        return self.get_instance_digestion_settings()

    def expand_prompt(
        self,
        user_id: str,
        content: Any,
        *,
        channel_name: Optional[str] = None,
        context_label: Optional[str] = None,
    ) -> dict[str, Any]:
        context = self._prepare_expand_context(user_id, content, channel_name=channel_name, context_label=context_label)
        output = self._call_openai(
            api_key=context['api_key'],
            model=context['model'],
            system_prompt=context['system_prompt'],
            prompt=context['prompt'],
            web_search_enabled=context['web_search_enabled'],
        ) if context['provider'] == 'openai' else self._call_bedrock(
            credential_secret=context.get('api_key') or '',
            model=context['model'],
            system_prompt=context['system_prompt'],
            prompt=context['prompt'],
        )
        if not output.strip():
            raise CanopyLLMError('The LLM returned an empty draft.', status_code=502, reason='empty_llm_output')
        return {
            'content': output.strip(),
            'provider': context['provider'],
            'model': context['model'],
            'credential_source': context.get('credential_source') or 'user',
        }

    def stream_expand_prompt(
        self,
        user_id: str,
        content: Any,
        *,
        channel_name: Optional[str] = None,
        context_label: Optional[str] = None,
    ) -> Iterator[dict[str, Any]]:
        """Stream an expanded draft as small events for the browser composer."""
        context = self._prepare_expand_context(user_id, content, channel_name=channel_name, context_label=context_label)
        if context['provider'] == 'bedrock':
            yield {
                'type': 'status',
                'message': 'Generating draft with AWS Bedrock...',
            }
            output = self._call_bedrock(
                credential_secret=context.get('api_key') or '',
                model=context['model'],
                system_prompt=context['system_prompt'],
                prompt=context['prompt'],
            ).strip()
            if output:
                yield {'type': 'delta', 'delta': output}
                yield {
                    'type': 'done',
                    'content': output,
                    'provider': context['provider'],
                    'model': context['model'],
                    'credential_source': context.get('credential_source') or 'user',
                }
                return
            raise CanopyLLMError('The LLM returned an empty draft.', status_code=502, reason='empty_llm_output')

        final_content = ''
        for event in self._stream_openai(
            api_key=context['api_key'],
            model=context['model'],
            system_prompt=context['system_prompt'],
            prompt=context['prompt'],
            web_search_enabled=context['web_search_enabled'],
        ):
            if event.get('type') == 'done':
                final_content = str(event.get('content') or '').strip()
                yield {
                    'type': 'done',
                    'content': final_content,
                    'provider': context['provider'],
                    'model': context['model'],
                    'credential_source': context.get('credential_source') or 'user',
                }
            else:
                yield event
        if not final_content:
            raise CanopyLLMError('The LLM returned an empty draft.', status_code=502, reason='empty_llm_output')

    def _prepare_expand_context(
        self,
        user_id: str,
        content: Any,
        *,
        channel_name: Optional[str] = None,
        context_label: Optional[str] = None,
    ) -> dict[str, Any]:
        user_id = str(user_id or '').strip()
        if not user_id:
            raise CanopyLLMError('Sign in before using Canopy AI Compose.', status_code=401, reason='not_authenticated')
        raw_content = str(content or '')
        if not self.has_canopy_trigger(raw_content):
            raise CanopyLLMError('Add @Canopy to the draft to use AI compose.', status_code=400, reason='missing_trigger')

        settings = self._resolve_effective_settings(user_id)
        provider = str(settings.get('provider') or 'openai')
        api_key = str(settings.get('api_key') or '').strip()

        prompt = self.strip_canopy_trigger(raw_content)
        if not prompt:
            raise CanopyLLMError('Write a prompt after @Canopy before sending.', status_code=400, reason='empty_prompt')
        if len(prompt) > MAX_LLM_INPUT_CHARS:
            raise CanopyLLMError(
                f'Canopy AI Compose prompts are capped at {MAX_LLM_INPUT_CHARS:,} characters.',
                status_code=400,
                reason='prompt_too_long',
            )

        context_lines = []
        if channel_name:
            context_lines.extend(['Surface: Channel', f'Channel: #{channel_name}'])
        elif context_label:
            context_lines.append(f'Surface: {str(context_label).strip()}')
        context_block = '\n'.join(context_lines)
        context_block = f"{context_block}\n\n" if context_block else ''
        compose_memory_context = self._build_compose_memory_context(
            user_id,
            settings,
            channel_name=channel_name,
            context_label=context_label,
        )
        memory_block = (
            "Node-local compose memory and team context:\n"
            "Use this only to make the draft feel natural, consistent, and well-routed. "
            "Do not quote private memory verbatim or imply access to hidden content.\n"
            f"{compose_memory_context}\n\n"
            if compose_memory_context
            else ''
        )
        current_timestamp = datetime.now().astimezone().isoformat(timespec='seconds')
        effective_web_search = (
            provider == 'openai'
            and bool(settings.get('web_search_enabled', True))
            and self._should_enable_web_search_for_prompt(prompt)
        )
        composed_prompt = (
            f"{context_block}"
            f"Current node timestamp: {current_timestamp}\n\n"
            f"{memory_block}"
            "The following text is the user's instruction or rough draft. Satisfy the instruction and write the final Canopy message body; do not merely repeat the instruction.\n"
            "User instruction/draft:\n"
            f"<<<\n{prompt}\n>>>\n\n"
            "Return only the polished Canopy message body for the human to review."
        )
        return {
            'provider': provider,
            'api_key': api_key,
            'model': str(settings.get('model') or DEFAULT_CANOPY_LLM_MODEL),
            'system_prompt': self._compose_system_prompt(
                str(settings.get('system_prompt') or DEFAULT_CANOPY_LLM_SYSTEM_PROMPT),
                web_search_available=provider == 'openai',
            ),
            'prompt': composed_prompt,
            'web_search_enabled': effective_web_search,
            'credential_source': settings.get('credential_source') or 'user',
        }

    def _resolve_effective_settings(self, user_id: str) -> dict[str, Any]:
        """Resolve personal settings first, then admin fallback credentials."""
        personal = self.get_settings(user_id)
        personal_enabled = bool(personal.get('enabled'))
        personal_key = self._get_api_key(user_id) if personal_enabled and personal.get('api_key_configured') else ''
        if personal_enabled and personal_key:
            resolved = dict(personal)
            resolved.update({
                'api_key': personal_key,
                'credential_source': 'user',
            })
            return resolved

        instance_settings = self.get_instance_settings()
        instance_provider = str(instance_settings.get('provider') or 'openai')
        instance_key = ''
        if instance_settings.get('enabled') and instance_settings.get('api_key_configured'):
            instance_key = self._get_instance_api_key() if instance_settings.get('key_saved') else ''
            if instance_provider == 'bedrock' and not instance_key and self._bedrock_environment_credentials_available():
                instance_key = ''
        instance_auth_available = (
            bool(instance_key)
            if instance_provider == 'openai'
            else bool(instance_settings.get('api_key_configured'))
        )
        if instance_settings.get('enabled') and instance_auth_available:
            use_personal_preferences = (
                personal_enabled
                and str(personal.get('provider') or 'openai') == instance_provider
            )
            return {
                'provider': instance_provider,
                'model': (
                    personal.get('model')
                    if use_personal_preferences and personal.get('model')
                    else instance_settings.get('model')
                ) or (DEFAULT_BEDROCK_LLM_MODEL if instance_provider == 'bedrock' else DEFAULT_CANOPY_LLM_MODEL),
                'enabled': True,
                'api_key': instance_key,
                'web_search_enabled': (
                    personal.get('web_search_enabled')
                    if use_personal_preferences
                    else instance_settings.get('web_search_enabled')
                ),
                'system_prompt': (
                    personal.get('system_prompt')
                    if use_personal_preferences and personal.get('system_prompt')
                    else instance_settings.get('system_prompt')
                ) or DEFAULT_CANOPY_LLM_SYSTEM_PROMPT,
                'memory_enabled': personal.get('memory_enabled', True),
                'compose_memory': personal.get('compose_memory') or '',
                'credential_source': 'instance',
            }

        if personal_enabled:
            raise CanopyLLMError(
                'Add provider credentials in Profile > Canopy AI Compose, or ask an admin to configure the instance fallback.',
                status_code=400,
                reason='missing_api_key',
            )
        raise CanopyLLMError(
            'Canopy AI Compose is not configured. Add your own key in Profile, or ask an admin to enable the instance fallback key.',
            status_code=400,
            reason='llm_disabled',
        )

    def _resolve_effective_digestion_settings(self, user_id: str) -> dict[str, Any]:
        """Resolve personal Digestion extraction settings, then admin fallback credentials."""
        personal = self.get_digestion_settings(user_id)
        personal_enabled = bool(personal.get('enabled'))
        personal_key = (
            self._get_digestion_api_key(user_id)
            if personal_enabled and personal.get('api_key_configured')
            else ''
        )
        if personal_enabled and personal_key:
            resolved = dict(personal)
            resolved.update({
                'api_key': personal_key,
                'credential_source': 'user',
                'parameters': self._normalize_digestion_parameters(personal.get('parameters') or {}),
            })
            return resolved

        instance_settings = self.get_instance_digestion_settings()
        instance_provider = str(instance_settings.get('provider') or 'openai')
        instance_key = ''
        if instance_settings.get('enabled') and instance_settings.get('api_key_configured'):
            instance_key = self._get_instance_digestion_api_key() if instance_settings.get('key_saved') else ''
            if instance_provider == 'bedrock' and not instance_key and self._bedrock_environment_credentials_available():
                instance_key = ''
        instance_auth_available = (
            bool(instance_key)
            if instance_provider == 'openai'
            else bool(instance_settings.get('api_key_configured'))
        )
        if instance_settings.get('enabled') and instance_auth_available:
            use_personal_preferences = (
                personal_enabled
                and str(personal.get('provider') or 'openai') == instance_provider
            )
            return {
                'provider': instance_provider,
                'model': (
                    personal.get('model')
                    if use_personal_preferences and personal.get('model')
                    else instance_settings.get('model')
                ) or (DEFAULT_BEDROCK_LLM_MODEL if instance_provider == 'bedrock' else DEFAULT_CANOPY_LLM_MODEL),
                'enabled': True,
                'api_key': instance_key,
                'default_lens': (
                    personal.get('default_lens')
                    if use_personal_preferences and personal.get('default_lens')
                    else instance_settings.get('default_lens')
                ) or DEFAULT_DIGESTION_LLM_LENS,
                'parameters': self._normalize_digestion_parameters(
                    personal.get('parameters') if use_personal_preferences else instance_settings.get('parameters')
                ),
                'credential_source': 'instance',
            }

        if personal_enabled:
            raise CanopyLLMError(
                'Add provider credentials in Profile > Digestion AI Extraction, or ask an admin to configure the instance Digestion fallback.',
                status_code=400,
                reason='missing_api_key',
            )
        raise CanopyLLMError(
            'Digestion AI extraction is not configured. Add your own key in Profile, or ask an admin to enable the instance Digestion fallback key.',
            status_code=400,
            reason='llm_disabled',
        )

    @staticmethod
    def _should_enable_web_search_for_prompt(prompt: Any) -> bool:
        """Avoid hosted web-search latency unless the draft asks for live/current facts."""
        if str(os.getenv('CANOPY_LLM_ALWAYS_ENABLE_WEB_SEARCH') or '').strip().lower() in {'1', 'true', 'yes', 'on'}:
            return True
        return bool(CURRENT_INFO_TRIGGER_RE.search(str(prompt or '')))

    def _ensure_schema(self) -> None:
        if self._schema_ready:
            return
        with self.db_manager.get_connection() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS user_llm_settings (
                    user_id TEXT PRIMARY KEY,
                    provider TEXT NOT NULL DEFAULT 'openai',
                    model TEXT NOT NULL DEFAULT 'gpt-5-mini',
                    api_key_ciphertext TEXT,
                    enabled INTEGER NOT NULL DEFAULT 0,
                    web_search_enabled INTEGER NOT NULL DEFAULT 1,
                    memory_enabled INTEGER NOT NULL DEFAULT 1,
                    compose_memory TEXT,
                    system_prompt TEXT,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS instance_llm_settings (
                    id TEXT PRIMARY KEY,
                    provider TEXT NOT NULL DEFAULT 'openai',
                    model TEXT NOT NULL DEFAULT 'gpt-5-mini',
                    api_key_ciphertext TEXT,
                    enabled INTEGER NOT NULL DEFAULT 0,
                    web_search_enabled INTEGER NOT NULL DEFAULT 1,
                    system_prompt TEXT,
                    updated_by TEXT,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS user_digestion_llm_settings (
                    user_id TEXT PRIMARY KEY,
                    provider TEXT NOT NULL DEFAULT 'openai',
                    model TEXT NOT NULL DEFAULT 'gpt-5-mini',
                    api_key_ciphertext TEXT,
                    enabled INTEGER NOT NULL DEFAULT 0,
                    default_lens TEXT,
                    max_chunks INTEGER NOT NULL DEFAULT 80,
                    max_datapoints INTEGER NOT NULL DEFAULT 400,
                    batch_chunks INTEGER NOT NULL DEFAULT 6,
                    batch_chars INTEGER NOT NULL DEFAULT 18000,
                    chunk_chars INTEGER NOT NULL DEFAULT 2800,
                    batch_records INTEGER NOT NULL DEFAULT 40,
                    max_output_tokens INTEGER NOT NULL DEFAULT 7000,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS instance_digestion_llm_settings (
                    id TEXT PRIMARY KEY,
                    provider TEXT NOT NULL DEFAULT 'openai',
                    model TEXT NOT NULL DEFAULT 'gpt-5-mini',
                    api_key_ciphertext TEXT,
                    enabled INTEGER NOT NULL DEFAULT 0,
                    default_lens TEXT,
                    max_chunks INTEGER NOT NULL DEFAULT 80,
                    max_datapoints INTEGER NOT NULL DEFAULT 400,
                    batch_chunks INTEGER NOT NULL DEFAULT 6,
                    batch_chars INTEGER NOT NULL DEFAULT 18000,
                    chunk_chars INTEGER NOT NULL DEFAULT 2800,
                    batch_records INTEGER NOT NULL DEFAULT 40,
                    max_output_tokens INTEGER NOT NULL DEFAULT 7000,
                    updated_by TEXT,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            columns = {
                str(row['name'] if hasattr(row, 'keys') else row[1])
                for row in conn.execute("PRAGMA table_info(user_llm_settings)").fetchall()
            }
            if 'web_search_enabled' not in columns:
                conn.execute(
                    "ALTER TABLE user_llm_settings "
                    "ADD COLUMN web_search_enabled INTEGER NOT NULL DEFAULT 1"
                )
            if 'memory_enabled' not in columns:
                conn.execute(
                    "ALTER TABLE user_llm_settings "
                    "ADD COLUMN memory_enabled INTEGER NOT NULL DEFAULT 1"
                )
            if 'compose_memory' not in columns:
                conn.execute(
                    "ALTER TABLE user_llm_settings "
                    "ADD COLUMN compose_memory TEXT"
                )
            instance_columns = {
                str(row['name'] if hasattr(row, 'keys') else row[1])
                for row in conn.execute("PRAGMA table_info(instance_llm_settings)").fetchall()
            }
            if 'web_search_enabled' not in instance_columns:
                conn.execute(
                    "ALTER TABLE instance_llm_settings "
                    "ADD COLUMN web_search_enabled INTEGER NOT NULL DEFAULT 1"
                )
            if 'updated_by' not in instance_columns:
                conn.execute(
                    "ALTER TABLE instance_llm_settings "
                    "ADD COLUMN updated_by TEXT"
                )
            self._ensure_digestion_settings_columns(conn, 'user_digestion_llm_settings', include_updated_by=False)
            self._ensure_digestion_settings_columns(conn, 'instance_digestion_llm_settings', include_updated_by=True)
            conn.commit()
        self._schema_ready = True

    def _ensure_digestion_settings_columns(self, conn: Any, table_name: str, *, include_updated_by: bool) -> None:
        columns = {
            str(row['name'] if hasattr(row, 'keys') else row[1])
            for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()
        }
        specs = {
            'provider': "TEXT NOT NULL DEFAULT 'openai'",
            'model': "TEXT NOT NULL DEFAULT 'gpt-5-mini'",
            'api_key_ciphertext': "TEXT",
            'enabled': "INTEGER NOT NULL DEFAULT 0",
            'default_lens': "TEXT",
            'max_chunks': "INTEGER NOT NULL DEFAULT 80",
            'max_datapoints': "INTEGER NOT NULL DEFAULT 400",
            'batch_chunks': "INTEGER NOT NULL DEFAULT 6",
            'batch_chars': "INTEGER NOT NULL DEFAULT 18000",
            'chunk_chars': "INTEGER NOT NULL DEFAULT 2800",
            'batch_records': "INTEGER NOT NULL DEFAULT 40",
            'max_output_tokens': "INTEGER NOT NULL DEFAULT 7000",
            'updated_at': "TIMESTAMP DEFAULT CURRENT_TIMESTAMP",
        }
        if include_updated_by:
            specs['updated_by'] = "TEXT"
        for column, spec in specs.items():
            if column not in columns:
                conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column} {spec}")

    def _default_settings(self) -> dict[str, Any]:
        return {
            'provider': 'openai',
            'model': DEFAULT_CANOPY_LLM_MODEL,
            'enabled': False,
            'api_key_configured': False,
            'web_search_enabled': True,
            'memory_enabled': True,
            'compose_memory': '',
            'system_prompt': DEFAULT_CANOPY_LLM_SYSTEM_PROMPT,
            'updated_at': None,
            'model_options': CANOPY_LLM_MODEL_OPTIONS,
            'provider_options': CANOPY_LLM_PROVIDER_OPTIONS,
        }

    def _default_instance_settings(self) -> dict[str, Any]:
        return {
            'provider': 'openai',
            'model': DEFAULT_CANOPY_LLM_MODEL,
            'enabled': False,
            'api_key_configured': False,
            'key_saved': False,
            'environment_credentials_available': False,
            'web_search_enabled': True,
            'system_prompt': DEFAULT_CANOPY_LLM_SYSTEM_PROMPT,
            'updated_at': None,
            'updated_by': None,
            'model_options': CANOPY_LLM_MODEL_OPTIONS,
            'provider_options': CANOPY_LLM_PROVIDER_OPTIONS,
        }

    def _default_digestion_settings(self) -> dict[str, Any]:
        return {
            'provider': 'openai',
            'model': DEFAULT_CANOPY_LLM_MODEL,
            'enabled': False,
            'api_key_configured': False,
            'default_lens': DEFAULT_DIGESTION_LLM_LENS,
            'parameters': self._normalize_digestion_parameters({}),
            'updated_at': None,
            'model_options': CANOPY_LLM_MODEL_OPTIONS,
            'provider_options': CANOPY_LLM_PROVIDER_OPTIONS,
            'parameter_limits': CANOPY_DIGESTION_LLM_PARAMETER_LIMITS,
        }

    def _default_instance_digestion_settings(self) -> dict[str, Any]:
        return {
            'provider': 'openai',
            'model': DEFAULT_CANOPY_LLM_MODEL,
            'enabled': False,
            'api_key_configured': False,
            'key_saved': False,
            'environment_credentials_available': False,
            'default_lens': DEFAULT_DIGESTION_LLM_LENS,
            'parameters': self._normalize_digestion_parameters({}),
            'updated_at': None,
            'updated_by': None,
            'model_options': CANOPY_LLM_MODEL_OPTIONS,
            'provider_options': CANOPY_LLM_PROVIDER_OPTIONS,
            'parameter_limits': CANOPY_DIGESTION_LLM_PARAMETER_LIMITS,
        }

    @staticmethod
    def _with_instance_summary(settings: dict[str, Any], instance_settings: dict[str, Any]) -> dict[str, Any]:
        instance_available = bool(instance_settings.get('enabled') and instance_settings.get('api_key_configured'))
        personal_available = bool(settings.get('enabled') and settings.get('api_key_configured'))
        merged = dict(settings)
        merged.update({
            'instance_fallback_enabled': bool(instance_settings.get('enabled')),
            'instance_fallback_key_configured': bool(instance_settings.get('api_key_configured')),
            'instance_fallback_available': instance_available,
            'instance_fallback_provider': instance_settings.get('provider') or 'openai',
            'instance_fallback_model': instance_settings.get('model') or DEFAULT_CANOPY_LLM_MODEL,
            'effective_enabled': bool(personal_available or instance_available),
            'using_instance_fallback': bool(instance_available and not personal_available),
        })
        return merged

    @staticmethod
    def _with_digestion_instance_summary(settings: dict[str, Any], instance_settings: dict[str, Any]) -> dict[str, Any]:
        instance_available = bool(instance_settings.get('enabled') and instance_settings.get('api_key_configured'))
        personal_available = bool(settings.get('enabled') and settings.get('api_key_configured'))
        merged = dict(settings)
        merged.update({
            'instance_fallback_enabled': bool(instance_settings.get('enabled')),
            'instance_fallback_key_configured': bool(instance_settings.get('api_key_configured')),
            'instance_fallback_available': instance_available,
            'instance_fallback_provider': instance_settings.get('provider') or 'openai',
            'instance_fallback_model': instance_settings.get('model') or DEFAULT_CANOPY_LLM_MODEL,
            'effective_enabled': bool(personal_available or instance_available),
            'using_instance_fallback': bool(instance_available and not personal_available),
        })
        return merged

    @staticmethod
    def _compose_system_prompt(system_prompt: Any, *, web_search_available: bool = True) -> str:
        """Attach non-optional Canopy syntax rules even when the user customizes tone."""
        base = str(system_prompt or '').strip() or DEFAULT_CANOPY_LLM_SYSTEM_PROMPT
        current_info_guide = (
            CANOPY_LLM_CURRENT_INFO_GUIDE
            if web_search_available
            else CANOPY_LLM_NO_WEB_SEARCH_CURRENT_INFO_GUIDE
        )
        if not web_search_available and CANOPY_LLM_CURRENT_INFO_GUIDE in base:
            base = base.replace(CANOPY_LLM_CURRENT_INFO_GUIDE, current_info_guide)
        if 'Canopy structured block rules:' in base:
            if 'Current-information and web-search rules:' in base and 'Draft-transformation rules:' in base:
                return base[:MAX_SYSTEM_PROMPT_CHARS]
            guide_parts = []
            if 'Draft-transformation rules:' not in base:
                guide_parts.append(CANOPY_LLM_TRANSFORMATION_GUIDE)
            if 'Current-information and web-search rules:' not in base:
                guide_parts.append(current_info_guide)
            guide = "\n\n".join(guide_parts)
            base_limit = max(0, MAX_SYSTEM_PROMPT_CHARS - len(guide) - 2)
            return f"{base[:base_limit].rstrip()}\n\n{guide}"[:MAX_SYSTEM_PROMPT_CHARS]
        guide = f"{CANOPY_LLM_TRANSFORMATION_GUIDE}\n\n{current_info_guide}\n\n{CANOPY_LLM_POSTING_STRUCTURE_GUIDE}"
        base_limit = max(0, MAX_SYSTEM_PROMPT_CHARS - len(guide) - 2)
        return f"{base[:base_limit].rstrip()}\n\n{guide}"[:MAX_SYSTEM_PROMPT_CHARS]

    @staticmethod
    def _normalize_secret(secret_key: str | bytes | None) -> str:
        if isinstance(secret_key, bytes):
            try:
                return secret_key.decode('utf-8')
            except Exception:
                return base64.urlsafe_b64encode(secret_key).decode('ascii')
        return str(secret_key or 'canopy-local-secret-fallback')

    def _get_fernet(self) -> Fernet:
        if self._fernet is None:
            material = hashlib.sha256(f'canopy-llm-settings-v1:{self.secret_key}'.encode('utf-8')).digest()
            self._fernet = Fernet(base64.urlsafe_b64encode(material))
        return self._fernet

    def _encrypt(self, value: str) -> str:
        return self._get_fernet().encrypt(value.encode('utf-8')).decode('ascii')

    def _decrypt(self, value: str) -> str:
        try:
            return self._get_fernet().decrypt(str(value or '').encode('ascii')).decode('utf-8')
        except InvalidToken as exc:
            raise CanopyLLMError(
                'Saved LLM API key could not be decrypted on this node. Re-enter the key in Profile.',
                status_code=400,
                reason='api_key_decrypt_failed',
            ) from exc

    def _get_api_key(self, user_id: str) -> str:
        self._ensure_schema()
        with self.db_manager.get_connection() as conn:
            row = conn.execute(
                "SELECT api_key_ciphertext FROM user_llm_settings WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        ciphertext = ''
        if row:
            ciphertext = str(self._row_value(row, 'api_key_ciphertext', 0, '') or '').strip()
        return self._decrypt(ciphertext) if ciphertext else ''

    def _get_instance_api_key(self) -> str:
        self._ensure_schema()
        with self.db_manager.get_connection() as conn:
            row = conn.execute(
                "SELECT api_key_ciphertext FROM instance_llm_settings WHERE id = ?",
                (INSTANCE_LLM_SETTINGS_ID,),
            ).fetchone()
        ciphertext = ''
        if row:
            ciphertext = str(self._row_value(row, 'api_key_ciphertext', 0, '') or '').strip()
        try:
            return self._decrypt(ciphertext) if ciphertext else ''
        except CanopyLLMError as exc:
            if exc.reason == 'api_key_decrypt_failed':
                raise CanopyLLMError(
                    'Saved instance fallback API key could not be decrypted on this node. '
                    'Re-enter the key in Admin > Instance AI Compose Fallback.',
                    status_code=400,
                    reason='api_key_decrypt_failed',
                ) from exc
            raise

    def _get_digestion_api_key(self, user_id: str) -> str:
        self._ensure_schema()
        with self.db_manager.get_connection() as conn:
            row = conn.execute(
                "SELECT api_key_ciphertext FROM user_digestion_llm_settings WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        ciphertext = str(self._row_value(row, 'api_key_ciphertext', 0, '') or '').strip() if row else ''
        return self._decrypt(ciphertext) if ciphertext else ''

    def _get_instance_digestion_api_key(self) -> str:
        self._ensure_schema()
        with self.db_manager.get_connection() as conn:
            row = conn.execute(
                "SELECT api_key_ciphertext FROM instance_digestion_llm_settings WHERE id = ?",
                (INSTANCE_DIGESTION_LLM_SETTINGS_ID,),
            ).fetchone()
        ciphertext = str(self._row_value(row, 'api_key_ciphertext', 0, '') or '').strip() if row else ''
        try:
            return self._decrypt(ciphertext) if ciphertext else ''
        except CanopyLLMError as exc:
            if exc.reason == 'api_key_decrypt_failed':
                raise CanopyLLMError(
                    'Saved instance Digestion fallback API key could not be decrypted on this node. '
                    'Re-enter the key in Admin > Instance Digestion AI Fallback.',
                    status_code=400,
                    reason='api_key_decrypt_failed',
                ) from exc
            raise

    def _provider_secret_configured(
        self,
        provider: Any,
        ciphertext: Any,
        *,
        allow_environment: bool = False,
    ) -> bool:
        provider_clean = self._normalize_provider_for_display(provider)
        if str(ciphertext or '').strip():
            return True
        if provider_clean == 'bedrock' and allow_environment:
            return self._bedrock_environment_credentials_available()
        return False

    @staticmethod
    def _bedrock_environment_credentials_available() -> bool:
        region_available = bool(
            os.getenv('AWS_REGION')
            or os.getenv('AWS_DEFAULT_REGION')
            or os.getenv('CANOPY_BEDROCK_REGION')
        )
        endpoint_available = bool(
            os.getenv('CANOPY_BEDROCK_RUNTIME_ENDPOINT')
        )
        bearer_available = bool(
            os.getenv('AWS_BEARER_TOKEN_BEDROCK')
            and (region_available or endpoint_available)
        )
        sigv4_available = bool(
            os.getenv('AWS_ACCESS_KEY_ID')
            and os.getenv('AWS_SECRET_ACCESS_KEY')
            and region_available
        )
        return bool(bearer_available or sigv4_available)

    @staticmethod
    def _parse_bedrock_credentials(secret: str) -> dict[str, str]:
        """Parse encrypted Bedrock credential text or fall back to server environment."""
        values: dict[str, str] = {}
        raw = str(secret or '').strip()
        if raw:
            parsed: Any = None
            if raw.startswith('{'):
                try:
                    parsed = json.loads(raw)
                except json.JSONDecodeError as exc:
                    raise CanopyLLMError(
                        'AWS Bedrock credentials must be valid JSON or KEY=VALUE lines.',
                        status_code=400,
                        reason='invalid_bedrock_credentials',
                    ) from exc
                if not isinstance(parsed, dict):
                    raise CanopyLLMError(
                        'AWS Bedrock credential JSON must be an object.',
                        status_code=400,
                        reason='invalid_bedrock_credentials',
                    )
                for key, value in parsed.items():
                    values[str(key).strip().lower()] = str(value or '').strip()
            else:
                saw_key_value = False
                for line in raw.replace(';', '\n').splitlines():
                    if '=' not in line:
                        continue
                    key, value = line.split('=', 1)
                    values[key.strip().lower()] = value.strip().strip('"').strip("'")
                    saw_key_value = True
                if not saw_key_value and '\n' not in raw and ';' not in raw:
                    values['bearer_token'] = raw

        def pick(*keys: str, env: Optional[str] = None) -> str:
            for key in keys:
                if values.get(key):
                    return values[key]
            return str(os.getenv(env or '') or '').strip() if env else ''

        credentials = {
            'bearer_token': pick(
                'aws_bearer_token_bedrock',
                'bedrock_api_key',
                'api_key',
                'bearer_token',
                env='AWS_BEARER_TOKEN_BEDROCK',
            ),
            'access_key_id': pick('aws_access_key_id', 'access_key_id', env='AWS_ACCESS_KEY_ID'),
            'secret_access_key': pick('aws_secret_access_key', 'secret_access_key', env='AWS_SECRET_ACCESS_KEY'),
            'session_token': pick('aws_session_token', 'session_token', 'token', env='AWS_SESSION_TOKEN'),
            'region': pick('region', 'aws_region', env='CANOPY_BEDROCK_REGION')
                or str(os.getenv('AWS_REGION') or os.getenv('AWS_DEFAULT_REGION') or '').strip(),
            'endpoint_url': pick('endpoint_url', 'bedrock_endpoint_url', env='CANOPY_BEDROCK_RUNTIME_ENDPOINT'),
        }
        if not credentials['bearer_token'] and (not credentials['access_key_id'] or not credentials['secret_access_key']):
            raise CanopyLLMError(
                'AWS Bedrock credentials are missing a Bedrock API key/bearer token, or access key ID and secret access key.',
                status_code=400,
                reason='missing_bedrock_credentials',
            )
        if not credentials['region'] and not credentials['endpoint_url']:
            raise CanopyLLMError(
                'AWS Bedrock region is required unless a runtime endpoint URL is provided.',
                status_code=400,
                reason='missing_bedrock_region',
            )
        if not credentials['bearer_token'] and not credentials['region']:
            raise CanopyLLMError(
                'AWS Bedrock region is required when using access key / secret key credentials.',
                status_code=400,
                reason='missing_bedrock_region',
            )
        return credentials

    @staticmethod
    def _row_value(row: Any, key: str, index: int, default: Any = None) -> Any:
        try:
            if hasattr(row, 'keys') and key in row.keys():
                return row[key]
        except Exception:
            pass
        try:
            return row[index]
        except Exception:
            return default

    @staticmethod
    def _table_exists(conn: Any, table_name: str) -> bool:
        try:
            row = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ? LIMIT 1",
                (str(table_name or '').strip(),),
            ).fetchone()
            return bool(row)
        except Exception:
            return False

    @staticmethod
    def _table_columns(conn: Any, table_name: str) -> set[str]:
        try:
            rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
        except Exception:
            return set()
        columns: set[str] = set()
        for row in rows:
            try:
                columns.add(str(row['name'] if hasattr(row, 'keys') else row[1]))
            except Exception:
                continue
        return columns

    @staticmethod
    def _compact_compose_text(text: Any, *, limit: int = 220) -> str:
        compact = re.sub(r'\s+', ' ', str(text or '').replace('\u00a0', ' ')).strip()
        if not compact:
            return ''
        if len(compact) <= limit:
            return compact
        return compact[: max(0, limit - 3)].rstrip() + '...'

    @staticmethod
    def _normalize_compose_memory(compose_memory: Any) -> str:
        memory = str(compose_memory or '').replace('\r\n', '\n').replace('\r', '\n').strip()
        memory = re.sub(r'\n{4,}', '\n\n\n', memory)
        if len(memory) > MAX_COMPOSE_MEMORY_CHARS:
            raise CanopyLLMError(
                f'Compose memory is capped at {MAX_COMPOSE_MEMORY_CHARS:,} characters.',
                status_code=400,
                reason='compose_memory_too_long',
            )
        return memory

    def _resolve_channel_id_for_memory(self, conn: Any, channel_name: Optional[str]) -> str:
        channel_name_clean = str(channel_name or '').strip().lstrip('#')
        if not channel_name_clean or not self._table_exists(conn, 'channels'):
            return ''
        try:
            row = conn.execute(
                "SELECT id FROM channels WHERE name = ? COLLATE NOCASE LIMIT 1",
                (channel_name_clean,),
            ).fetchone()
            return str(self._row_value(row, 'id', 0, '') or '').strip() if row else ''
        except Exception:
            return ''

    def _load_compose_team_members(self, conn: Any, user_id: str, *, channel_id: str = '') -> list[str]:
        if not self._table_exists(conn, 'users'):
            return []
        user_columns = self._table_columns(conn, 'users')
        if not user_columns:
            return []
        select_parts = ['u.id']
        select_parts.append('u.username' if 'username' in user_columns else "u.id AS username")
        select_parts.append('u.display_name' if 'display_name' in user_columns else "'' AS display_name")
        select_parts.append('u.account_type' if 'account_type' in user_columns else "'' AS account_type")
        display_expr = "u.display_name" if 'display_name' in user_columns else "''"
        username_expr = "u.username" if 'username' in user_columns else "u.id"
        order_expr = f"COALESCE({display_expr}, {username_expr}, u.id)"
        if channel_id and self._table_exists(conn, 'channel_members'):
            channel_member_columns = self._table_columns(conn, 'channel_members')
            role_select = 'cm.role AS channel_role' if 'role' in channel_member_columns else "'' AS channel_role"
            try:
                rows = conn.execute(
                    f"""
                    SELECT {', '.join(select_parts)}, {role_select}
                    FROM channel_members cm
                    JOIN users u ON u.id = cm.user_id
                    WHERE cm.channel_id = ?
                    ORDER BY
                        CASE WHEN u.id = ? THEN 0 ELSE 1 END,
                        {order_expr} COLLATE NOCASE
                    LIMIT ?
                    """,
                    (channel_id, user_id, MAX_COMPOSE_TEAM_MEMBERS),
                ).fetchall()
            except Exception:
                rows = []
        else:
            identity_predicates = [
                f"COALESCE(u.{column}, '') != ''"
                for column in ('password_hash', 'display_name', 'username')
                if column in user_columns
            ]
            where_sql = f"WHERE {' OR '.join(identity_predicates)}" if identity_predicates else ''
            try:
                rows = conn.execute(
                    f"""
                    SELECT {', '.join(select_parts)}, '' AS channel_role
                    FROM users u
                    {where_sql}
                    ORDER BY
                        CASE WHEN u.id = ? THEN 0 ELSE 1 END,
                        {order_expr} COLLATE NOCASE
                    LIMIT ?
                    """,
                    (user_id, MAX_COMPOSE_TEAM_MEMBERS),
                ).fetchall()
            except Exception:
                rows = []

        members: list[str] = []
        seen: set[str] = set()
        for row in rows or []:
            uid = str(self._row_value(row, 'id', 0, '') or '').strip()
            username = str(self._row_value(row, 'username', 1, '') or '').strip()
            display_name = str(self._row_value(row, 'display_name', 2, '') or '').strip()
            account_type = str(self._row_value(row, 'account_type', 3, '') or '').strip().lower()
            role = str(self._row_value(row, 'channel_role', 4, '') or '').strip().lower()
            key = uid or username or display_name
            if not key or key in seen:
                continue
            seen.add(key)
            handle = username or uid
            handle_label = f"@{handle.lstrip('@')}" if handle else uid
            label = display_name or username or uid
            detail_parts = []
            if account_type:
                detail_parts.append(account_type)
            if role:
                detail_parts.append(role)
            detail = f" ({', '.join(detail_parts)})" if detail_parts else ''
            if uid == user_id:
                detail = f"{detail} [current user]".strip()
            members.append(f"{label} / {handle_label}{detail}")
        return members[:MAX_COMPOSE_TEAM_MEMBERS]

    def _load_compose_style_examples(self, conn: Any, user_id: str, *, channel_id: str = '') -> list[str]:
        examples: list[tuple[str, str]] = []

        def add_rows(
            table: str,
            user_col: str,
            content_col: str,
            created_col: str,
            label: str,
            *,
            extra_clauses: Optional[list[str]] = None,
            extra_params: Optional[list[Any]] = None,
        ) -> None:
            if not self._table_exists(conn, table):
                return
            columns = self._table_columns(conn, table)
            required = {user_col, content_col}
            if not required.issubset(columns):
                return
            order_col = created_col if created_col in columns else ''
            order_sql = f"ORDER BY {order_col} DESC" if order_col else ''
            select_created = order_col if order_col else "''"
            clauses = [
                f"{user_col} = ?",
                f"TRIM(COALESCE({content_col}, '')) != ''",
            ]
            params: list[Any] = [user_id]
            for clause in extra_clauses or []:
                if clause:
                    clauses.append(clause)
            params.extend(extra_params or [])
            where_sql = " AND ".join(clauses)
            try:
                rows = conn.execute(
                    f"""
                    SELECT {content_col} AS content, {select_created} AS created_at
                    FROM {table}
                    WHERE {where_sql}
                    {order_sql}
                    LIMIT 8
                    """,
                    tuple(params),
                ).fetchall()
            except Exception:
                return
            for row in rows or []:
                content = self._compact_compose_text(self._row_value(row, 'content', 0, ''), limit=240)
                if not content or '@Canopy' in content or '@canopy' in content:
                    continue
                created = str(self._row_value(row, 'created_at', 1, '') or '').strip()
                examples.append((created, f"{label}: {content}"))

        if channel_id:
            add_rows(
                'channel_messages',
                'user_id',
                'content',
                'created_at',
                'channel',
                extra_clauses=['channel_id = ?'],
                extra_params=[channel_id],
            )
        feed_clauses = []
        if 'visibility' in self._table_columns(conn, 'feed_posts'):
            feed_clauses.append("visibility IN ('public', 'network', 'trusted')")
        add_rows('feed_posts', 'author_id', 'content', 'created_at', 'feed', extra_clauses=feed_clauses)
        examples.sort(key=lambda item: item[0] or '', reverse=True)
        deduped: list[str] = []
        seen: set[str] = set()
        for _, example in examples:
            key = example.lower()
            if key in seen:
                continue
            seen.add(key)
            deduped.append(example)
            if len(deduped) >= MAX_COMPOSE_STYLE_EXAMPLES:
                break
        return deduped

    def _build_compose_memory_context(
        self,
        user_id: str,
        settings: dict[str, Any],
        *,
        channel_name: Optional[str] = None,
        context_label: Optional[str] = None,
    ) -> str:
        if not self._normalize_bool(settings.get('memory_enabled'), default=True):
            return ''

        sections: list[str] = []
        explicit_memory = str(settings.get('compose_memory') or '').strip()
        if explicit_memory:
            sections.append(f"User-stated compose memory:\n{explicit_memory[:MAX_COMPOSE_MEMORY_CHARS]}")

        try:
            with self.db_manager.get_connection() as conn:
                channel_id = self._resolve_channel_id_for_memory(conn, channel_name)
                members = self._load_compose_team_members(conn, user_id, channel_id=channel_id)
                examples = self._load_compose_style_examples(conn, user_id, channel_id=channel_id)
        except Exception as exc:
            logger.debug("Skipping Canopy compose memory context: %s", exc)
            members = []
            examples = []

        if members:
            label = f"Known team members for #{channel_name}" if channel_name else "Known Canopy teammates"
            sections.append(label + ":\n" + "\n".join(f"- {member}" for member in members))
        if examples:
            sections.append(
                "Recent writing examples from this user for tone and habits; do not quote unless the user asks:\n"
                + "\n".join(f"- {example}" for example in examples)
            )
        context = "\n\n".join(section.strip() for section in sections if section.strip())
        return context[:MAX_COMPOSE_CONTEXT_CHARS].rstrip()

    @staticmethod
    def _normalize_provider(provider: Any) -> str:
        provider_clean = str(provider or 'openai').strip().lower()
        aliases = {
            'openai': 'openai',
            'responses': 'openai',
            'aws': 'bedrock',
            'aws-bedrock': 'bedrock',
            'amazon-bedrock': 'bedrock',
            'bedrock': 'bedrock',
        }
        normalized = aliases.get(provider_clean)
        if not normalized:
            raise CanopyLLMError('Unsupported AI compose provider.', status_code=400, reason='unsupported_provider')
        return normalized

    @staticmethod
    def _normalize_provider_for_display(provider: Any) -> str:
        try:
            return CanopyLLMManager._normalize_provider(provider)
        except CanopyLLMError:
            return 'openai'

    @staticmethod
    def _normalize_model(model: Any, *, provider: str = 'openai') -> str:
        default_model = DEFAULT_BEDROCK_LLM_MODEL if provider == 'bedrock' else DEFAULT_CANOPY_LLM_MODEL
        model_clean = str(model or default_model).strip()
        if not model_clean:
            return default_model
        if len(model_clean) > 120:
            raise CanopyLLMError('Model name is too long.', status_code=400, reason='model_too_long')
        if not re.match(r'^[A-Za-z0-9._:/+-]+$', model_clean):
            raise CanopyLLMError('Model name contains unsupported characters.', status_code=400, reason='invalid_model')
        return model_clean

    @staticmethod
    def _normalize_model_for_display(model: Any, *, provider: str = 'openai') -> str:
        try:
            return CanopyLLMManager._normalize_model(model, provider=provider)
        except CanopyLLMError:
            return DEFAULT_BEDROCK_LLM_MODEL if provider == 'bedrock' else DEFAULT_CANOPY_LLM_MODEL

    @staticmethod
    def _bounded_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
        try:
            parsed = int(value if value is not None and str(value).strip() != '' else default)
        except Exception:
            parsed = int(default)
        return max(int(minimum), min(parsed, int(maximum)))

    @classmethod
    def _normalize_digestion_parameters(
        cls,
        parameters: Optional[dict[str, Any]] = None,
        **overrides: Any,
    ) -> dict[str, int]:
        raw: dict[str, Any] = dict(parameters or {})
        for key, value in overrides.items():
            if value is not None:
                raw[key] = value
        normalized: dict[str, int] = {}
        for key, spec in CANOPY_DIGESTION_LLM_PARAMETER_LIMITS.items():
            normalized[key] = cls._bounded_int(
                raw.get(key),
                default=int(spec['default']),
                minimum=int(spec['min']),
                maximum=int(spec['max']),
            )
        return normalized

    @staticmethod
    def _normalize_digestion_lens(default_lens: Any) -> str:
        lens = str(default_lens or '').replace('\r\n', '\n').replace('\r', '\n').strip()
        lens = re.sub(r'\n{4,}', '\n\n\n', lens)
        if not lens:
            lens = DEFAULT_DIGESTION_LLM_LENS
        if len(lens) > MAX_DIGESTION_LLM_LENS_CHARS:
            raise CanopyLLMError(
                f'Digestion extraction lens is capped at {MAX_DIGESTION_LLM_LENS_CHARS:,} characters.',
                status_code=400,
                reason='digestion_lens_too_long',
            )
        return lens

    @staticmethod
    def _normalize_system_prompt(system_prompt: Any) -> str:
        prompt = str(system_prompt or '').strip() or DEFAULT_CANOPY_LLM_SYSTEM_PROMPT
        if len(prompt) > MAX_SYSTEM_PROMPT_CHARS:
            raise CanopyLLMError(
                f'System prompt is capped at {MAX_SYSTEM_PROMPT_CHARS:,} characters.',
                status_code=400,
                reason='system_prompt_too_long',
            )
        return prompt

    @staticmethod
    def _normalize_bool(value: Any, *, default: bool = False) -> bool:
        if value is None:
            return bool(default)
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        text = str(value).strip().lower()
        if text in {'1', 'true', 'yes', 'on', 'enabled'}:
            return True
        if text in {'0', 'false', 'no', 'off', 'disabled'}:
            return False
        return bool(default)

    @staticmethod
    def _web_search_tool_payload() -> dict[str, Any]:
        tool: dict[str, Any] = {'type': 'web_search'}
        context_size = str(os.getenv('CANOPY_LLM_WEB_SEARCH_CONTEXT_SIZE', 'low') or 'low').strip().lower()
        if context_size in {'low', 'medium', 'high'}:
            tool['search_context_size'] = context_size
        external_access = os.getenv('CANOPY_LLM_WEB_SEARCH_EXTERNAL_ACCESS')
        if external_access is not None:
            tool['external_web_access'] = CanopyLLMManager._normalize_bool(external_access, default=True)
        return tool

    def _call_bedrock(
        self,
        *,
        credential_secret: str,
        model: str,
        system_prompt: str,
        prompt: str,
        max_output_tokens: Optional[int] = None,
    ) -> str:
        credentials = self._parse_bedrock_credentials(credential_secret)
        region = credentials['region']
        endpoint = (credentials.get('endpoint_url') or f'https://bedrock-runtime.{region}.amazonaws.com').rstrip('/')
        timeout = float(os.getenv('CANOPY_LLM_TIMEOUT_SECONDS', '90') or '90')
        if max_output_tokens is None:
            max_output_tokens = self._bounded_int_env(
                'CANOPY_BEDROCK_MAX_TOKENS',
                default=2600,
                minimum=256,
                maximum=12000,
            )
        else:
            max_output_tokens = max(256, min(int(max_output_tokens or 2600), 12000))
        payload = {
            'messages': [
                {
                    'role': 'user',
                    'content': [{'text': prompt}],
                }
            ],
            'system': [{'text': system_prompt}],
            'inferenceConfig': {
                'maxTokens': max_output_tokens,
                'temperature': self._bounded_float_env(
                    'CANOPY_BEDROCK_TEMPERATURE',
                    default=0.4,
                    minimum=0.0,
                    maximum=1.0,
                ),
                'topP': self._bounded_float_env(
                    'CANOPY_BEDROCK_TOP_P',
                    default=0.9,
                    minimum=0.0,
                    maximum=1.0,
                ),
            },
        }
        data = self._invoke_bedrock_converse(
            endpoint=endpoint,
            region=region,
            model=model,
            payload=payload,
            credentials=credentials,
            timeout=timeout,
        )
        text = self._extract_bedrock_converse_text(data)
        if not text:
            raise CanopyLLMError(
                'AWS Bedrock returned no output text. Check the selected model or inference profile.',
                status_code=502,
                reason='provider_empty_response',
            )
        return text

    def _invoke_bedrock_converse(
        self,
        *,
        endpoint: str,
        region: str,
        model: str,
        payload: dict[str, Any],
        credentials: dict[str, str],
        timeout: float,
    ) -> dict[str, Any]:
        model_path = quote(str(model or DEFAULT_BEDROCK_LLM_MODEL), safe='')
        url = f'{endpoint}/model/{model_path}/converse'
        body = json.dumps(payload, separators=(',', ':')).encode('utf-8')
        bearer_token = str(credentials.get('bearer_token') or '').strip()
        if bearer_token:
            headers = {
                'Accept': 'application/json',
                'Content-Type': 'application/json',
                'Authorization': f'Bearer {bearer_token}',
                'User-Agent': 'Canopy-LLM-Compose/1',
            }
        else:
            headers = self._bedrock_sigv4_headers(
                url=url,
                body=body,
                region=region,
                credentials=credentials,
            )
        request = Request(url, data=body, method='POST', headers=headers)
        try:
            with urlopen(request, timeout=timeout) as response:
                raw_bytes = response.read(_MAX_LLM_RESPONSE_BYTES + 1)
                if len(raw_bytes) > _MAX_LLM_RESPONSE_BYTES:
                    raise CanopyLLMError(
                        'AWS Bedrock returned a response that exceeded Canopy AI Compose limits.',
                        status_code=502,
                        reason='provider_response_too_large',
                    )
                raw = raw_bytes.decode('utf-8')
        except HTTPError as exc:
            message = self._extract_bedrock_error(exc)
            logger.warning('AWS Bedrock compose request failed with HTTP %s: %s', exc.code, message)
            raise CanopyLLMError(message, status_code=502, reason='provider_http_error') from exc
        except (URLError, TimeoutError) as exc:
            logger.warning('AWS Bedrock compose request failed: %s', exc)
            raise CanopyLLMError(f'Could not reach AWS Bedrock: {exc}', status_code=502, reason='provider_unreachable') from exc

        try:
            data = json.loads(raw or '{}')
        except json.JSONDecodeError as exc:
            raise CanopyLLMError('AWS Bedrock returned a non-JSON response.', status_code=502, reason='provider_bad_response') from exc
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _bedrock_sigv4_headers(
        *,
        url: str,
        body: bytes,
        region: str,
        credentials: dict[str, str],
    ) -> dict[str, str]:
        parsed = urlparse(url)
        host = parsed.netloc
        canonical_uri = parsed.path or '/'
        canonical_query = parsed.query or ''
        now = datetime.utcnow()
        amz_date = now.strftime('%Y%m%dT%H%M%SZ')
        date_stamp = now.strftime('%Y%m%d')
        payload_hash = hashlib.sha256(body).hexdigest()
        headers = {
            'accept': 'application/json',
            'content-type': 'application/json',
            'host': host,
            'x-amz-date': amz_date,
        }
        token = str(credentials.get('session_token') or '').strip()
        if token:
            headers['x-amz-security-token'] = token
        signed_headers = ';'.join(sorted(headers.keys()))
        canonical_headers = ''.join(f'{key}:{headers[key]}\n' for key in sorted(headers.keys()))
        canonical_request = '\n'.join([
            'POST',
            canonical_uri,
            canonical_query,
            canonical_headers,
            signed_headers,
            payload_hash,
        ])
        service = 'bedrock'
        credential_scope = f'{date_stamp}/{region}/{service}/aws4_request'
        string_to_sign = '\n'.join([
            'AWS4-HMAC-SHA256',
            amz_date,
            credential_scope,
            hashlib.sha256(canonical_request.encode('utf-8')).hexdigest(),
        ])

        def sign(key: bytes, msg: str) -> bytes:
            return hmac.new(key, msg.encode('utf-8'), hashlib.sha256).digest()

        signing_key = sign(
            sign(
                sign(
                    sign(('AWS4' + credentials['secret_access_key']).encode('utf-8'), date_stamp),
                    region,
                ),
                service,
            ),
            'aws4_request',
        )
        signature = hmac.new(signing_key, string_to_sign.encode('utf-8'), hashlib.sha256).hexdigest()
        authorization = (
            f"AWS4-HMAC-SHA256 Credential={credentials['access_key_id']}/{credential_scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        )
        return {
            'Accept': headers['accept'],
            'Content-Type': headers['content-type'],
            'Host': headers['host'],
            'X-Amz-Date': headers['x-amz-date'],
            **({'X-Amz-Security-Token': token} if token else {}),
            'Authorization': authorization,
            'User-Agent': 'Canopy-LLM-Compose/1',
        }

    @staticmethod
    def _extract_bedrock_converse_text(data: dict[str, Any]) -> str:
        chunks: list[str] = []
        output = data.get('output')
        message = output.get('message') if isinstance(output, dict) else None
        content = message.get('content') if isinstance(message, dict) else None
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and isinstance(part.get('text'), str):
                    chunks.append(part['text'])
        return ''.join(chunks).strip()

    @staticmethod
    def _extract_bedrock_error(exc: HTTPError) -> str:
        try:
            raw = exc.read().decode('utf-8', errors='replace')
            data = json.loads(raw or '{}')
            for key in ('message', 'Message', 'errorMessage'):
                if isinstance(data, dict) and data.get(key):
                    return f"AWS Bedrock request failed: {data[key]}"
            if isinstance(data, dict) and isinstance(data.get('__type'), str):
                return f"AWS Bedrock request failed ({data.get('__type')})."
        except Exception:
            pass
        return f'AWS Bedrock request failed with HTTP {getattr(exc, "code", "error")}.'

    def _call_openai(
        self,
        *,
        api_key: str,
        model: str,
        system_prompt: str,
        prompt: str,
        web_search_enabled: bool = False,
        max_output_tokens: Optional[int] = None,
    ) -> str:
        base_url = os.getenv('CANOPY_OPENAI_BASE_URL', 'https://api.openai.com/v1').strip().rstrip('/')
        timeout = float(os.getenv('CANOPY_LLM_TIMEOUT_SECONDS', '90') or '90')
        if max_output_tokens is None:
            max_output_tokens = self._default_max_output_tokens(web_search_enabled=web_search_enabled)
        else:
            max_output_tokens = max(800, min(int(max_output_tokens or 2600), 20000))
        payload = {
            'model': model,
            'instructions': system_prompt,
            'input': prompt,
            'max_output_tokens': max_output_tokens,
            'store': False,
        }
        if web_search_enabled:
            payload['tools'] = [self._web_search_tool_payload()]
            payload['tool_choice'] = 'auto'
            payload['max_tool_calls'] = self._bounded_int_env(
                'CANOPY_LLM_WEB_SEARCH_MAX_TOOL_CALLS',
                default=2,
                minimum=1,
                maximum=6,
            )

        attempts = 1 + self._bounded_int_env('CANOPY_LLM_EMPTY_RETRY_ATTEMPTS', default=2, minimum=0, maximum=3)
        last_summary = ''
        last_incomplete_reason = ''
        for attempt in range(attempts):
            attempt_payload = self._openai_attempt_payload(
                payload,
                prompt=prompt,
                attempt=attempt,
                total_attempts=attempts,
                web_search_enabled=web_search_enabled,
                last_incomplete_reason=last_incomplete_reason,
            )
            data = self._create_openai_response(
                base_url=base_url,
                api_key=api_key,
                payload=attempt_payload,
                timeout=timeout,
            )
            data = self._resolve_openai_response_if_pending(
                base_url=base_url,
                api_key=api_key,
                data=data,
                timeout=timeout,
            )
            provider_error = self._extract_response_error_message(data)
            if provider_error:
                raise CanopyLLMError(provider_error, status_code=502, reason='provider_response_error')
            text = self._extract_response_text(data)
            if text:
                return text
            last_summary = self._summarize_response_shape(data)
            last_incomplete_reason = self._response_incomplete_reason(data)
            logger.warning(
                'OpenAI compose response had no output text on attempt %s/%s: %s',
                attempt + 1,
                attempts,
                last_summary,
            )

        detail = f' Response shape: {last_summary}' if last_summary else ''
        raise CanopyLLMError(
            'OpenAI returned no output text after retry. Please try Generate again or turn off web search for this draft.'
            + detail,
            status_code=502,
            reason='provider_empty_response',
        )

    def _default_max_output_tokens(self, *, web_search_enabled: bool) -> int:
        """Choose a token budget that leaves room for Responses reasoning/tool work."""
        if os.getenv('CANOPY_LLM_MAX_OUTPUT_TOKENS'):
            raw = os.getenv('CANOPY_LLM_MAX_OUTPUT_TOKENS', '')
        elif web_search_enabled:
            raw = os.getenv('CANOPY_LLM_WEB_SEARCH_MAX_OUTPUT_TOKENS', '6000')
        else:
            raw = os.getenv('CANOPY_LLM_PLAIN_MAX_OUTPUT_TOKENS', '2600')
        try:
            value = int(str(raw or '').strip())
        except Exception:
            value = 6000 if web_search_enabled else 2600
        return max(800, min(value, 20000))

    def _openai_attempt_payload(
        self,
        payload: dict[str, Any],
        *,
        prompt: str,
        attempt: int,
        total_attempts: int,
        web_search_enabled: bool,
        last_incomplete_reason: str = '',
    ) -> dict[str, Any]:
        attempt_payload = dict(payload)
        base_tokens = int(attempt_payload.get('max_output_tokens') or 2600)
        if attempt <= 0:
            return attempt_payload

        final_retry = attempt >= total_attempts - 1
        had_token_exhaustion = str(last_incomplete_reason or '').strip().lower() == 'max_output_tokens'
        retry_tokens_default = 9000 if web_search_enabled else 4000
        retry_token_cap = self._bounded_int_env(
            'CANOPY_LLM_RETRY_MAX_OUTPUT_TOKENS',
            default=retry_tokens_default,
            minimum=1200,
            maximum=20000,
        )
        attempt_payload['max_output_tokens'] = min(max(int(base_tokens * 1.5), retry_tokens_default), retry_token_cap)
        attempt_payload['input'] = (
            f"{prompt}\n\n"
            "Generate the final Canopy post body now. Do not return only tool calls or reasoning. "
            "Keep the draft concise and ready for human review. "
            "If current facts could not be verified, say that plainly in the draft."
        )

        if web_search_enabled and not final_retry:
            attempt_payload['max_tool_calls'] = 1
            attempt_payload['input'] += (
                "\nUse at most one web search call on this retry, then write the final post body."
            )
        elif web_search_enabled and (final_retry or had_token_exhaustion):
            # If web search repeatedly consumes the whole output budget, still give the
            # human an editable draft rather than a dead composer. The prompt requires
            # the model to disclose that current facts were not freshly verified.
            attempt_payload.pop('tools', None)
            attempt_payload.pop('tool_choice', None)
            attempt_payload.pop('max_tool_calls', None)
            attempt_payload['input'] += (
                "\nDo not use web search on this final retry. If the post needs live facts, "
                "write a concise draft that says current facts still need verification."
            )
        return attempt_payload

    def _stream_openai(
        self,
        *,
        api_key: str,
        model: str,
        system_prompt: str,
        prompt: str,
        web_search_enabled: bool = False,
    ) -> Iterator[dict[str, Any]]:
        base_url = os.getenv('CANOPY_OPENAI_BASE_URL', 'https://api.openai.com/v1').strip().rstrip('/')
        timeout = float(os.getenv('CANOPY_LLM_TIMEOUT_SECONDS', '90') or '90')
        max_output_tokens = self._default_max_output_tokens(web_search_enabled=web_search_enabled)
        payload = {
            'model': model,
            'instructions': system_prompt,
            'input': prompt,
            'max_output_tokens': max_output_tokens,
            'store': False,
        }
        if web_search_enabled:
            payload['tools'] = [self._web_search_tool_payload()]
            payload['tool_choice'] = 'auto'
            payload['max_tool_calls'] = self._bounded_int_env(
                'CANOPY_LLM_WEB_SEARCH_MAX_TOOL_CALLS',
                default=2,
                minimum=1,
                maximum=6,
            )

        attempts = 1 + self._bounded_int_env('CANOPY_LLM_EMPTY_RETRY_ATTEMPTS', default=2, minimum=0, maximum=3)
        last_summary = ''
        last_incomplete_reason = ''
        for attempt in range(attempts):
            if attempt > 0:
                yield {
                    'type': 'status',
                    'message': 'Retrying with a tighter final-draft instruction...',
                }
            attempt_payload = self._openai_attempt_payload(
                payload,
                prompt=prompt,
                attempt=attempt,
                total_attempts=attempts,
                web_search_enabled=web_search_enabled,
                last_incomplete_reason=last_incomplete_reason,
            )
            text_parts: list[str] = []
            final_response: dict[str, Any] = {}
            for event in self._create_openai_response_stream(
                base_url=base_url,
                api_key=api_key,
                payload=attempt_payload,
                timeout=timeout,
            ):
                event_type = str(event.get('type') or '').strip()
                if event_type == 'response.output_text.delta':
                    delta = str(event.get('delta') or '')
                    if delta:
                        text_parts.append(delta)
                        yield {'type': 'delta', 'delta': delta}
                elif event_type == 'response.output_text.done':
                    text = str(event.get('text') or '')
                    if text and not text_parts:
                        text_parts.append(text)
                        yield {'type': 'delta', 'delta': text}
                elif event_type in _OPENAI_WEB_SEARCH_TOOL_STATUSES:
                    yield {'type': 'status', 'message': 'Checking current web sources...'}
                elif event_type in {'response.completed', 'response.done', 'response.incomplete', 'response.failed'}:
                    response_payload = event.get('response')
                    if isinstance(response_payload, dict):
                        final_response = response_payload
                elif event_type == 'error':
                    message = str(event.get('message') or event.get('error') or '').strip()
                    if message:
                        raise CanopyLLMError(message, status_code=502, reason='provider_response_error')

            final_text = ''.join(text_parts).strip()
            if not final_text and final_response:
                final_text = self._extract_response_text(final_response)
                if final_text:
                    yield {'type': 'delta', 'delta': final_text}
            if final_text:
                yield {'type': 'done', 'content': final_text}
                return
            last_summary = self._summarize_response_shape(final_response) if final_response else 'status=unknown; output=none'
            last_incomplete_reason = self._response_incomplete_reason(final_response)
            logger.warning(
                'Streaming OpenAI compose response had no output text on attempt %s/%s: %s',
                attempt + 1,
                attempts,
                last_summary,
            )

        detail = f' Response shape: {last_summary}' if last_summary else ''
        raise CanopyLLMError(
            'OpenAI returned no output text after retry. Please try Generate again or turn off web search for this draft.'
            + detail,
            status_code=502,
            reason='provider_empty_response',
        )

    def _create_openai_response(
        self,
        *,
        base_url: str,
        api_key: str,
        payload: dict[str, Any],
        timeout: float,
    ) -> dict[str, Any]:
        body = json.dumps(payload).encode('utf-8')
        request = Request(
            f'{base_url}/responses',
            data=body,
            method='POST',
            headers={
                'Authorization': f'Bearer {api_key}',
                'Content-Type': 'application/json',
                'Accept': 'application/json',
                'User-Agent': 'Canopy-LLM-Compose/1',
            },
        )
        return self._read_openai_json(request, timeout=timeout)

    def _create_openai_response_stream(
        self,
        *,
        base_url: str,
        api_key: str,
        payload: dict[str, Any],
        timeout: float,
    ) -> Iterator[dict[str, Any]]:
        stream_payload = dict(payload)
        stream_payload['stream'] = True
        body = json.dumps(stream_payload).encode('utf-8')
        request = Request(
            f'{base_url}/responses',
            data=body,
            method='POST',
            headers={
                'Authorization': f'Bearer {api_key}',
                'Content-Type': 'application/json',
                'Accept': 'text/event-stream',
                'User-Agent': 'Canopy-LLM-Compose/1',
            },
        )
        yield from self._read_openai_sse(request, timeout=timeout)

    def _retrieve_openai_response(
        self,
        *,
        base_url: str,
        api_key: str,
        response_id: str,
        timeout: float,
    ) -> dict[str, Any]:
        request = Request(
            f'{base_url}/responses/{response_id}',
            method='GET',
            headers={
                'Authorization': f'Bearer {api_key}',
                'Accept': 'application/json',
                'User-Agent': 'Canopy-LLM-Compose/1',
            },
        )
        return self._read_openai_json(request, timeout=timeout)

    def _read_openai_json(self, request: Request, *, timeout: float) -> dict[str, Any]:
        try:
            with urlopen(request, timeout=timeout) as response:
                raw_bytes = response.read(_MAX_LLM_RESPONSE_BYTES + 1)
                if len(raw_bytes) > _MAX_LLM_RESPONSE_BYTES:
                    raise CanopyLLMError(
                        'OpenAI returned a response that exceeded Canopy AI Compose limits.',
                        status_code=502,
                        reason='provider_response_too_large',
                    )
                raw = raw_bytes.decode('utf-8')
        except HTTPError as exc:
            message = self._extract_openai_error(exc)
            logger.warning('OpenAI compose request failed with HTTP %s: %s', exc.code, message)
            raise CanopyLLMError(message, status_code=502, reason='provider_http_error') from exc
        except (URLError, TimeoutError) as exc:
            logger.warning('OpenAI compose request failed: %s', exc)
            raise CanopyLLMError(f'Could not reach OpenAI: {exc}', status_code=502, reason='provider_unreachable') from exc

        try:
            data = json.loads(raw or '{}')
        except json.JSONDecodeError as exc:
            raise CanopyLLMError('OpenAI returned a non-JSON response.', status_code=502, reason='provider_bad_response') from exc
        return data if isinstance(data, dict) else {}

    def _read_openai_sse(self, request: Request, *, timeout: float) -> Iterator[dict[str, Any]]:
        try:
            with urlopen(request, timeout=timeout) as response:
                event_name = ''
                data_lines: list[str] = []
                total_bytes = 0

                def _flush_event() -> Iterator[dict[str, Any]]:
                    nonlocal event_name, data_lines
                    if not data_lines:
                        event_name = ''
                        return
                    raw_data = '\n'.join(data_lines).strip()
                    name = event_name
                    event_name = ''
                    data_lines = []
                    if not raw_data or raw_data == '[DONE]':
                        return
                    try:
                        parsed = json.loads(raw_data)
                    except json.JSONDecodeError:
                        logger.debug("Skipping non-JSON OpenAI SSE event %s: %s", name, raw_data[:120])
                        return
                    if isinstance(parsed, dict):
                        if name and not parsed.get('type'):
                            parsed['type'] = name
                        yield parsed

                while True:
                    raw_line = response.readline(64 * 1024)
                    if not raw_line:
                        yield from _flush_event()
                        break
                    total_bytes += len(raw_line)
                    if total_bytes > _MAX_LLM_RESPONSE_BYTES:
                        raise CanopyLLMError(
                            'OpenAI streamed a response that exceeded Canopy AI Compose limits.',
                            status_code=502,
                            reason='provider_response_too_large',
                        )
                    line = raw_line.decode('utf-8', errors='replace').rstrip('\r\n')
                    if line == '':
                        yield from _flush_event()
                    elif line.startswith('event:'):
                        event_name = line.split(':', 1)[1].strip()
                    elif line.startswith('data:'):
                        data_lines.append(line.split(':', 1)[1].lstrip())
        except HTTPError as exc:
            message = self._extract_openai_error(exc)
            logger.warning('OpenAI compose stream failed with HTTP %s: %s', exc.code, message)
            raise CanopyLLMError(message, status_code=502, reason='provider_http_error') from exc
        except (URLError, TimeoutError) as exc:
            logger.warning('OpenAI compose stream failed: %s', exc)
            raise CanopyLLMError(f'Could not reach OpenAI: {exc}', status_code=502, reason='provider_unreachable') from exc

    def _resolve_openai_response_if_pending(
        self,
        *,
        base_url: str,
        api_key: str,
        data: dict[str, Any],
        timeout: float,
    ) -> dict[str, Any]:
        status = str(data.get('status') or '').strip().lower()
        response_id = str(data.get('id') or '').strip()
        if status not in _OPENAI_PENDING_STATUSES or not response_id:
            return data

        polls = self._bounded_int_env('CANOPY_LLM_PENDING_POLL_ATTEMPTS', default=3, minimum=0, maximum=8)
        delay = float(os.getenv('CANOPY_LLM_PENDING_POLL_DELAY_SECONDS', '0.35') or '0.35')
        current = data
        for _ in range(polls):
            if delay > 0:
                time.sleep(min(delay, 2.0))
            try:
                current = self._retrieve_openai_response(
                    base_url=base_url,
                    api_key=api_key,
                    response_id=response_id,
                    timeout=timeout,
                )
            except CanopyLLMError as exc:
                logger.warning('OpenAI compose pending response poll failed: %s', exc)
                return data
            status = str(current.get('status') or '').strip().lower()
            if status not in _OPENAI_PENDING_STATUSES:
                return current
        return current

    @staticmethod
    def _extract_openai_error(exc: HTTPError) -> str:
        try:
            raw = exc.read().decode('utf-8', errors='replace')
            data = json.loads(raw or '{}')
            err = data.get('error') if isinstance(data, dict) else None
            if isinstance(err, dict) and err.get('message'):
                return str(err.get('message'))
            if isinstance(data, dict) and data.get('message'):
                return str(data.get('message'))
            if raw.strip():
                return raw.strip()[:500]
        except Exception:
            pass
        return f'OpenAI request failed with HTTP {getattr(exc, "code", "error")}.'

    @staticmethod
    def _extract_response_text(data: dict[str, Any]) -> str:
        direct = data.get('output_text')
        if isinstance(direct, str) and direct.strip():
            return direct.strip()

        chunks: list[str] = []
        output = data.get('output')
        if isinstance(output, list):
            for item in output:
                if not isinstance(item, dict):
                    continue
                content = item.get('content')
                if not isinstance(content, list):
                    continue
                for part in content:
                    if not isinstance(part, dict):
                        continue
                    part_type = str(part.get('type') or '')
                    if part_type in {'output_text', 'text'} and isinstance(part.get('text'), str):
                        chunks.append(part['text'])
        return ''.join(chunks).strip()

    @staticmethod
    def _extract_response_error_message(data: dict[str, Any]) -> str:
        error = data.get('error')
        if isinstance(error, dict):
            message = str(error.get('message') or '').strip()
            code = str(error.get('code') or '').strip()
            if message and code:
                return f'OpenAI response failed ({code}): {message}'
            if message:
                return f'OpenAI response failed: {message}'
        status = str(data.get('status') or '').strip().lower()
        if status == 'failed':
            return 'OpenAI response failed before producing a draft.'
        return ''

    @staticmethod
    def _response_incomplete_reason(data: dict[str, Any]) -> str:
        if not isinstance(data, dict):
            return ''
        incomplete = data.get('incomplete_details')
        if isinstance(incomplete, dict):
            return str(incomplete.get('reason') or '').strip()
        return ''

    @staticmethod
    def _summarize_response_shape(data: dict[str, Any]) -> str:
        status = str(data.get('status') or 'unknown').strip() or 'unknown'
        incomplete_reason = CanopyLLMManager._response_incomplete_reason(data)
        output = data.get('output')
        item_summaries: list[str] = []
        if isinstance(output, list):
            for item in output[:8]:
                if not isinstance(item, dict):
                    item_summaries.append(type(item).__name__)
                    continue
                item_type = str(item.get('type') or 'unknown').strip() or 'unknown'
                item_status = str(item.get('status') or '').strip()
                content = item.get('content')
                content_types: list[str] = []
                if isinstance(content, list):
                    for part in content[:4]:
                        if isinstance(part, dict):
                            content_types.append(str(part.get('type') or 'unknown').strip() or 'unknown')
                suffix = f":{item_status}" if item_status else ''
                if content_types:
                    suffix += f"[{','.join(content_types)}]"
                item_summaries.append(f"{item_type}{suffix}")
        summary = f"status={status}; output={','.join(item_summaries) or 'none'}"
        if incomplete_reason:
            summary += f"; incomplete={incomplete_reason}"
        if data.get('id'):
            summary += '; id=present'
        return summary[:500]

    @staticmethod
    def _bounded_int_env(name: str, *, default: int, minimum: int, maximum: int) -> int:
        try:
            value = int(os.getenv(name, str(default)) or default)
        except Exception:
            value = default
        return max(minimum, min(maximum, value))

    @staticmethod
    def _bounded_float_env(name: str, *, default: float, minimum: float, maximum: float) -> float:
        try:
            value = float(os.getenv(name, str(default)) or default)
        except Exception:
            value = default
        return max(minimum, min(maximum, value))
