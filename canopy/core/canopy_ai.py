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
from typing import Any, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)


DEFAULT_CANOPY_LLM_MODEL = os.getenv('CANOPY_LLM_DEFAULT_MODEL', 'gpt-5-mini').strip() or 'gpt-5-mini'
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
DEFAULT_CANOPY_LLM_SYSTEM_PROMPT = (
    "You are Canopy's local compose assistant. Convert the user's draft into the exact "
    "Canopy channel post they should review and optionally send. Output only the final post body, with no "
    "preamble, no markdown fence, and no mention of these instructions. Remove the "
    "@Canopy trigger unless the user explicitly asks to discuss it. Preserve intentional "
    "Canopy syntax such as @mentions and #channels. Preserve or emit [task], [request], "
    "[objective], [signal], and [handoff] blocks only when they follow the structured block "
    "rules below, so Canopy can process them normally after "
    "the post is sent. Do not claim access to hidden files, private channel context, or "
    "mesh state unless the user included that context in the draft."
    f"\n\n{CANOPY_LLM_POSTING_STRUCTURE_GUIDE}"
)

CANOPY_TRIGGER_RE = re.compile(r'(?i)(^|\s)@canopy\b[:,]?\s*')
MAX_SYSTEM_PROMPT_CHARS = 4000
MAX_LLM_INPUT_CHARS = 24000
_MAX_LLM_RESPONSE_BYTES = 512 * 1024  # 512 KiB is generous; typical responses are much smaller.


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
        if not user_id:
            return defaults
        self._ensure_schema()
        with self.db_manager.get_connection() as conn:
            row = conn.execute(
                """
                SELECT provider, model, api_key_ciphertext, enabled, system_prompt, updated_at
                FROM user_llm_settings
                WHERE user_id = ?
                """,
                (user_id,),
            ).fetchone()
        if not row:
            return defaults
        provider = str(self._row_value(row, 'provider', 0, 'openai') or 'openai').strip() or 'openai'
        model = str(self._row_value(row, 'model', 1, DEFAULT_CANOPY_LLM_MODEL) or DEFAULT_CANOPY_LLM_MODEL).strip() or DEFAULT_CANOPY_LLM_MODEL
        ciphertext = self._row_value(row, 'api_key_ciphertext', 2, '')
        enabled = self._row_value(row, 'enabled', 3, 0)
        system_prompt = str(self._row_value(row, 'system_prompt', 4, '') or '').strip() or DEFAULT_CANOPY_LLM_SYSTEM_PROMPT
        updated_at = self._row_value(row, 'updated_at', 5, None)
        return {
            'provider': provider if provider == 'openai' else 'openai',
            'model': model[:120],
            'enabled': bool(enabled),
            'api_key_configured': bool(str(ciphertext or '').strip()),
            'system_prompt': system_prompt[:MAX_SYSTEM_PROMPT_CHARS],
            'updated_at': updated_at,
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
    ) -> dict[str, Any]:
        user_id = str(user_id or '').strip()
        if not user_id:
            raise CanopyLLMError('Sign in before configuring Canopy AI Compose.', status_code=401, reason='not_authenticated')
        provider_clean = self._normalize_provider(provider)
        model_clean = self._normalize_model(model)
        prompt_clean = self._normalize_system_prompt(system_prompt)
        enabled_clean = 1 if bool(enabled) else 0

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
                    user_id, provider, model, api_key_ciphertext, enabled, system_prompt, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(user_id) DO UPDATE SET
                    provider = excluded.provider,
                    model = excluded.model,
                    api_key_ciphertext = excluded.api_key_ciphertext,
                    enabled = excluded.enabled,
                    system_prompt = excluded.system_prompt,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    user_id,
                    provider_clean,
                    model_clean,
                    ciphertext,
                    enabled_clean,
                    prompt_clean,
                ),
            )
            conn.commit()
        return self.get_settings(user_id)

    def expand_prompt(
        self,
        user_id: str,
        content: Any,
        *,
        channel_name: Optional[str] = None,
    ) -> dict[str, Any]:
        user_id = str(user_id or '').strip()
        if not user_id:
            raise CanopyLLMError('Sign in before using Canopy AI Compose.', status_code=401, reason='not_authenticated')
        raw_content = str(content or '')
        if not self.has_canopy_trigger(raw_content):
            raise CanopyLLMError('Add @Canopy to the draft to use AI compose.', status_code=400, reason='missing_trigger')

        settings = self.get_settings(user_id)
        if not settings.get('enabled'):
            raise CanopyLLMError(
                'Canopy AI Compose is disabled. Enable it from Profile > Canopy AI Compose.',
                status_code=400,
                reason='llm_disabled',
            )

        provider = str(settings.get('provider') or 'openai')
        if provider != 'openai':
            raise CanopyLLMError('Only OpenAI is supported for Canopy AI Compose right now.', status_code=400, reason='unsupported_provider')

        api_key = self._get_api_key(user_id)
        if not api_key:
            raise CanopyLLMError(
                'Add an OpenAI API key in Profile > Canopy AI Compose before using @Canopy.',
                status_code=400,
                reason='missing_api_key',
            )

        prompt = self.strip_canopy_trigger(raw_content)
        if not prompt:
            raise CanopyLLMError('Write a prompt after @Canopy before sending.', status_code=400, reason='empty_prompt')
        if len(prompt) > MAX_LLM_INPUT_CHARS:
            raise CanopyLLMError(
                f'Canopy AI Compose prompts are capped at {MAX_LLM_INPUT_CHARS:,} characters.',
                status_code=400,
                reason='prompt_too_long',
            )

        channel_line = f"Channel: #{channel_name}\n\n" if channel_name else ''
        composed_prompt = (
            f"{channel_line}"
            "User draft to transform into a Canopy post:\n"
            f"{prompt}"
        )
        output = self._call_openai(
            api_key=api_key,
            model=str(settings.get('model') or DEFAULT_CANOPY_LLM_MODEL),
            system_prompt=self._compose_system_prompt(str(settings.get('system_prompt') or DEFAULT_CANOPY_LLM_SYSTEM_PROMPT)),
            prompt=composed_prompt,
        )
        if not output.strip():
            raise CanopyLLMError('The LLM returned an empty draft.', status_code=502, reason='empty_llm_output')
        return {
            'content': output.strip(),
            'provider': provider,
            'model': str(settings.get('model') or DEFAULT_CANOPY_LLM_MODEL),
        }

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
                    system_prompt TEXT,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
                )
                """
            )
            conn.commit()
        self._schema_ready = True

    def _default_settings(self) -> dict[str, Any]:
        return {
            'provider': 'openai',
            'model': DEFAULT_CANOPY_LLM_MODEL,
            'enabled': False,
            'api_key_configured': False,
            'system_prompt': DEFAULT_CANOPY_LLM_SYSTEM_PROMPT,
            'updated_at': None,
        }

    @staticmethod
    def _compose_system_prompt(system_prompt: Any) -> str:
        """Attach non-optional Canopy syntax rules even when the user customizes tone."""
        base = str(system_prompt or '').strip() or DEFAULT_CANOPY_LLM_SYSTEM_PROMPT
        if 'Canopy structured block rules:' in base:
            return base[:MAX_SYSTEM_PROMPT_CHARS]
        guide = CANOPY_LLM_POSTING_STRUCTURE_GUIDE
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

    def _call_openai(self, *, api_key: str, model: str, system_prompt: str, prompt: str) -> str:
        base_url = os.getenv('CANOPY_OPENAI_BASE_URL', 'https://api.openai.com/v1').strip().rstrip('/')
        timeout = float(os.getenv('CANOPY_LLM_TIMEOUT_SECONDS', '60') or '60')
        payload = {
            'model': model,
            'instructions': system_prompt,
            'input': prompt,
            'max_output_tokens': int(os.getenv('CANOPY_LLM_MAX_OUTPUT_TOKENS', '2200') or '2200'),
            'store': False,
        }
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
        text = self._extract_response_text(data)
        if not text:
            logger.warning('OpenAI compose response contained no output text: %s', data)
            raise CanopyLLMError('OpenAI returned no output text.', status_code=502, reason='provider_empty_response')
        return text

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
