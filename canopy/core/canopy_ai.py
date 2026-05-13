"""
Local-only LLM compose support for Canopy.

This module intentionally stores provider credentials in a node-local table
instead of user profiles or any mesh-synced identity surface.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import time
from datetime import datetime
from typing import Any, Iterator, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)


DEFAULT_CANOPY_LLM_MODEL = os.getenv('CANOPY_LLM_DEFAULT_MODEL', 'gpt-5-mini').strip() or 'gpt-5-mini'
INSTANCE_LLM_SETTINGS_ID = 'default'
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
]
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
    f"\n\n{CANOPY_LLM_CURRENT_INFO_GUIDE}\n\n{CANOPY_LLM_POSTING_STRUCTURE_GUIDE}"
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
                SELECT provider, model, api_key_ciphertext, enabled, system_prompt, updated_at, web_search_enabled
                FROM user_llm_settings
                WHERE user_id = ?
                """,
                (user_id,),
            ).fetchone()
        if not row:
            return self._with_instance_summary(defaults, instance_settings)
        provider = str(self._row_value(row, 'provider', 0, 'openai') or 'openai').strip() or 'openai'
        model = str(self._row_value(row, 'model', 1, DEFAULT_CANOPY_LLM_MODEL) or DEFAULT_CANOPY_LLM_MODEL).strip() or DEFAULT_CANOPY_LLM_MODEL
        ciphertext = self._row_value(row, 'api_key_ciphertext', 2, '')
        enabled = self._row_value(row, 'enabled', 3, 0)
        system_prompt = str(self._row_value(row, 'system_prompt', 4, '') or '').strip() or DEFAULT_CANOPY_LLM_SYSTEM_PROMPT
        updated_at = self._row_value(row, 'updated_at', 5, None)
        web_search_enabled = self._row_value(row, 'web_search_enabled', 6, 1)
        return self._with_instance_summary({
            'provider': provider if provider == 'openai' else 'openai',
            'model': model[:120],
            'enabled': bool(enabled),
            'api_key_configured': bool(str(ciphertext or '').strip()),
            'web_search_enabled': self._normalize_bool(web_search_enabled, default=True),
            'system_prompt': system_prompt[:MAX_SYSTEM_PROMPT_CHARS],
            'updated_at': updated_at,
            'model_options': CANOPY_LLM_MODEL_OPTIONS,
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
        provider = str(self._row_value(row, 'provider', 0, 'openai') or 'openai').strip() or 'openai'
        model = str(self._row_value(row, 'model', 1, DEFAULT_CANOPY_LLM_MODEL) or DEFAULT_CANOPY_LLM_MODEL).strip() or DEFAULT_CANOPY_LLM_MODEL
        ciphertext = self._row_value(row, 'api_key_ciphertext', 2, '')
        enabled = self._row_value(row, 'enabled', 3, 0)
        system_prompt = str(self._row_value(row, 'system_prompt', 4, '') or '').strip() or DEFAULT_CANOPY_LLM_SYSTEM_PROMPT
        updated_at = self._row_value(row, 'updated_at', 5, None)
        web_search_enabled = self._row_value(row, 'web_search_enabled', 6, 1)
        updated_by = self._row_value(row, 'updated_by', 7, None)
        return {
            'provider': provider if provider == 'openai' else 'openai',
            'model': model[:120],
            'enabled': bool(enabled),
            'api_key_configured': bool(str(ciphertext or '').strip()),
            'web_search_enabled': self._normalize_bool(web_search_enabled, default=True),
            'system_prompt': system_prompt[:MAX_SYSTEM_PROMPT_CHARS],
            'updated_at': updated_at,
            'updated_by': updated_by,
            'model_options': CANOPY_LLM_MODEL_OPTIONS,
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
    ) -> dict[str, Any]:
        user_id = str(user_id or '').strip()
        if not user_id:
            raise CanopyLLMError('Sign in before configuring Canopy AI Compose.', status_code=401, reason='not_authenticated')
        provider_clean = self._normalize_provider(provider)
        model_clean = self._normalize_model(model)
        prompt_clean = self._normalize_system_prompt(system_prompt)
        enabled_clean = 1 if bool(enabled) else 0
        web_search_enabled_clean = 1 if self._normalize_bool(web_search_enabled, default=True) else 0

        self._ensure_schema()
        with self.db_manager.get_connection() as conn:
            existing = conn.execute(
                "SELECT api_key_ciphertext FROM user_llm_settings WHERE user_id = ?",
                (user_id,),
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
                INSERT INTO user_llm_settings (
                    user_id, provider, model, api_key_ciphertext, enabled, system_prompt, web_search_enabled, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(user_id) DO UPDATE SET
                    provider = excluded.provider,
                    model = excluded.model,
                    api_key_ciphertext = excluded.api_key_ciphertext,
                    enabled = excluded.enabled,
                    system_prompt = excluded.system_prompt,
                    web_search_enabled = excluded.web_search_enabled,
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
        model_clean = self._normalize_model(model)
        prompt_clean = self._normalize_system_prompt(system_prompt)
        enabled_clean = 1 if bool(enabled) else 0
        web_search_enabled_clean = 1 if self._normalize_bool(web_search_enabled, default=True) else 0

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
        if provider != 'openai':
            raise CanopyLLMError('Only OpenAI is supported for Canopy AI Compose right now.', status_code=400, reason='unsupported_provider')
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
        current_timestamp = datetime.now().astimezone().isoformat(timespec='seconds')
        effective_web_search = bool(settings.get('web_search_enabled', True)) and self._should_enable_web_search_for_prompt(prompt)
        composed_prompt = (
            f"{context_block}"
            f"Current node timestamp: {current_timestamp}\n\n"
            "User draft to transform into a Canopy message:\n"
            f"{prompt}"
        )
        return {
            'provider': provider,
            'api_key': api_key,
            'model': str(settings.get('model') or DEFAULT_CANOPY_LLM_MODEL),
            'system_prompt': self._compose_system_prompt(str(settings.get('system_prompt') or DEFAULT_CANOPY_LLM_SYSTEM_PROMPT)),
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
        instance_key = (
            self._get_instance_api_key()
            if instance_settings.get('enabled') and instance_settings.get('api_key_configured')
            else ''
        )
        if instance_settings.get('enabled') and instance_key:
            use_personal_preferences = personal_enabled
            return {
                'provider': instance_settings.get('provider') or 'openai',
                'model': (
                    personal.get('model')
                    if use_personal_preferences and personal.get('model')
                    else instance_settings.get('model')
                ) or DEFAULT_CANOPY_LLM_MODEL,
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
                'credential_source': 'instance',
            }

        if personal_enabled:
            raise CanopyLLMError(
                'Add an OpenAI API key in Profile > Canopy AI Compose, or ask an admin to configure the instance fallback key.',
                status_code=400,
                reason='missing_api_key',
            )
        raise CanopyLLMError(
            'Canopy AI Compose is not configured. Add your own key in Profile, or ask an admin to enable the instance fallback key.',
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
            columns = {
                str(row['name'] if hasattr(row, 'keys') else row[1])
                for row in conn.execute("PRAGMA table_info(user_llm_settings)").fetchall()
            }
            if 'web_search_enabled' not in columns:
                conn.execute(
                    "ALTER TABLE user_llm_settings "
                    "ADD COLUMN web_search_enabled INTEGER NOT NULL DEFAULT 1"
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
            conn.commit()
        self._schema_ready = True

    def _default_settings(self) -> dict[str, Any]:
        return {
            'provider': 'openai',
            'model': DEFAULT_CANOPY_LLM_MODEL,
            'enabled': False,
            'api_key_configured': False,
            'web_search_enabled': True,
            'system_prompt': DEFAULT_CANOPY_LLM_SYSTEM_PROMPT,
            'updated_at': None,
            'model_options': CANOPY_LLM_MODEL_OPTIONS,
        }

    def _default_instance_settings(self) -> dict[str, Any]:
        return {
            'provider': 'openai',
            'model': DEFAULT_CANOPY_LLM_MODEL,
            'enabled': False,
            'api_key_configured': False,
            'web_search_enabled': True,
            'system_prompt': DEFAULT_CANOPY_LLM_SYSTEM_PROMPT,
            'updated_at': None,
            'updated_by': None,
            'model_options': CANOPY_LLM_MODEL_OPTIONS,
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
            'instance_fallback_model': instance_settings.get('model') or DEFAULT_CANOPY_LLM_MODEL,
            'effective_enabled': bool(personal_available or instance_available),
            'using_instance_fallback': bool(instance_available and not personal_available),
        })
        return merged

    @staticmethod
    def _compose_system_prompt(system_prompt: Any) -> str:
        """Attach non-optional Canopy syntax rules even when the user customizes tone."""
        base = str(system_prompt or '').strip() or DEFAULT_CANOPY_LLM_SYSTEM_PROMPT
        if 'Canopy structured block rules:' in base:
            if 'Current-information and web-search rules:' in base:
                return base[:MAX_SYSTEM_PROMPT_CHARS]
            guide = CANOPY_LLM_CURRENT_INFO_GUIDE
            base_limit = max(0, MAX_SYSTEM_PROMPT_CHARS - len(guide) - 2)
            return f"{base[:base_limit].rstrip()}\n\n{guide}"[:MAX_SYSTEM_PROMPT_CHARS]
        guide = f"{CANOPY_LLM_CURRENT_INFO_GUIDE}\n\n{CANOPY_LLM_POSTING_STRUCTURE_GUIDE}"
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
    def _normalize_provider(provider: Any) -> str:
        provider_clean = str(provider or 'openai').strip().lower()
        if provider_clean != 'openai':
            raise CanopyLLMError('Only OpenAI is supported right now.', status_code=400, reason='unsupported_provider')
        return provider_clean

    @staticmethod
    def _normalize_model(model: Any) -> str:
        model_clean = str(model or DEFAULT_CANOPY_LLM_MODEL).strip()
        if not model_clean:
            return DEFAULT_CANOPY_LLM_MODEL
        if len(model_clean) > 120:
            raise CanopyLLMError('Model name is too long.', status_code=400, reason='model_too_long')
        if not re.match(r'^[A-Za-z0-9._:/+-]+$', model_clean):
            raise CanopyLLMError('Model name contains unsupported characters.', status_code=400, reason='invalid_model')
        return model_clean

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

    def _call_openai(
        self,
        *,
        api_key: str,
        model: str,
        system_prompt: str,
        prompt: str,
        web_search_enabled: bool = False,
    ) -> str:
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
