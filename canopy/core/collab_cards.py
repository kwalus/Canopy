"""
Universal collaboration cards for Canopy.

Cards are durable structured objects declared inline in posts or channel
messages.  They give agents and humans a shared, permission-gated surface for
soliciting input and publishing live task/process telemetry.
"""

from __future__ import annotations

import json
import logging
import re
import secrets
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

from .database import DatabaseManager

logger = logging.getLogger("canopy.collab_cards")

CARD_TYPES = ("input", "telemetry")
INPUT_CARD_KINDS = ("text", "decision", "approval", "choice", "multi_choice")
INPUT_CARD_STATUSES = ("open", "waiting", "resolved", "closed", "cancelled")
TELEMETRY_CARD_STATUSES = ("idle", "running", "paused", "blocked", "complete", "warning", "error")

_CARD_BLOCK_PATTERNS = [
    ("input", re.compile(r"(?is)\[(?:input-card|input_card|input)\](.*?)\[/(?:input-card|input_card|input)\]")),
    ("input", re.compile(r"(?is)::(?:input-card|input_card|input)\s*(.*?)\s*::end(?:input-card|input_card|input)")),
    ("telemetry", re.compile(r"(?is)\[(?:telemetry-card|telemetry_card|telemetry)\](.*?)\[/(?:telemetry-card|telemetry_card|telemetry)\]")),
    ("telemetry", re.compile(r"(?is)::(?:telemetry-card|telemetry_card|telemetry)\s*(.*?)\s*::end(?:telemetry-card|telemetry_card|telemetry)")),
]

_MAX_CARD_BLOCKS = 30
_MAX_CARD_INPUT_SIZE = 1_000_000
_CONFIRM_FALSE = {"false", "no", "off", "0"}
_CONFIRM_TRUE = {"true", "yes", "on", "1"}


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now_utc().isoformat()


def _mask_code_fences(text: str) -> str:
    return re.sub(r"```.*?```", lambda m: "\x00" * len(m.group(0)), text, flags=re.S)


def _sanitize_text(text: str) -> str:
    if not text:
        return text
    text = text.replace("\x00", "")
    text = re.sub(r"[\x01-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)
    text = re.sub(r"[\u200b-\u200f\u2028-\u202f\u2060\ufeff]", "", text)
    return text


def _split_tokens(raw: Any) -> List[str]:
    if raw is None:
        return []
    if isinstance(raw, (list, tuple, set)):
        parts = [str(part).strip() for part in raw]
    else:
        value = str(raw).strip()
        if not value:
            return []
        if value.startswith("[") and value.endswith("]"):
            try:
                parsed = json.loads(value)
                if isinstance(parsed, list):
                    parts = [str(part).strip() for part in parsed]
                else:
                    parts = [value]
            except Exception:
                parts = re.split(r"[,;]", value.strip("[]"))
        else:
            parts = re.split(r"[,;]", value)

    ordered: List[str] = []
    seen = set()
    for part in parts:
        cleaned = part.strip().strip('"').strip("'")
        if not cleaned:
            continue
        if cleaned.lower() in seen:
            continue
        seen.add(cleaned.lower())
        ordered.append(cleaned)
    return ordered


def _parse_bool(raw: Any, default: bool = True) -> bool:
    value = str(raw or "").strip().lower()
    if value in _CONFIRM_FALSE:
        return False
    if value in _CONFIRM_TRUE:
        return True
    return default


def _parse_progress(raw: Any) -> Optional[int]:
    if raw is None:
        return None
    try:
        value = str(raw).strip().rstrip("%")
        if not value:
            return None
        return max(0, min(100, int(round(float(value)))))
    except Exception:
        return None


def _parse_jsonish(raw: str) -> Any:
    value = (raw or "").strip()
    if not value:
        return None
    try:
        return json.loads(value)
    except Exception:
        return None


def _parse_metric_line(line: str) -> Optional[Dict[str, Any]]:
    raw = line.strip().lstrip("-*").strip()
    if not raw:
        return None
    label = raw
    value = ""
    unit = ""
    status = None
    if ":" in raw:
        label, value = [part.strip() for part in raw.split(":", 1)]
    elif "=" in raw:
        label, value = [part.strip() for part in raw.split("=", 1)]
    if not label:
        return None
    status_match = re.search(r"\(([^)]+)\)\s*$", value)
    if status_match:
        status = status_match.group(1).strip().lower()
        value = value[:status_match.start()].strip()
    unit_match = re.match(r"^([-+]?\d+(?:\.\d+)?)\s*([A-Za-z%/]+)?$", value)
    if unit_match:
        value = unit_match.group(1)
        unit = unit_match.group(2) or ""
    return {"label": label, "value": value, "unit": unit, "status": status}


@dataclass
class InputCardSpec:
    title: str
    prompt: Optional[str] = None
    summary: Optional[str] = None
    kind: str = "text"
    options: List[str] = field(default_factory=list)
    permissions: List[str] = field(default_factory=list)
    editors: List[str] = field(default_factory=list)
    status: str = "open"
    required: bool = False
    card_id: Optional[str] = None
    confirmed: bool = True
    raw: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class TelemetryCardSpec:
    title: str
    summary: Optional[str] = None
    status: str = "running"
    progress: Optional[int] = None
    stage: Optional[str] = None
    metrics: List[Dict[str, Any]] = field(default_factory=list)
    permissions: List[str] = field(default_factory=list)
    editors: List[str] = field(default_factory=list)
    severity: Optional[str] = None
    card_id: Optional[str] = None
    confirmed: bool = True
    raw: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def derive_collab_card_id(source_type: str, source_id: str, card_type: str,
                          index: int = 0, total: int = 1,
                          override: Optional[str] = None) -> str:
    prefix = "input" if card_type == "input" else "telemetry"
    if override:
        cleaned = re.sub(r"[^A-Za-z0-9_\-:.]", "_", override.strip())
        if cleaned:
            return cleaned if cleaned.startswith(f"{prefix}_card_") else f"{prefix}_card_{cleaned}"
    base = f"{prefix}_card_{source_type}_{source_id}"
    if total > 1:
        return f"{base}_{index + 1}"
    return base


def parse_collab_card_blocks(text: str) -> List[InputCardSpec | TelemetryCardSpec]:
    if not text:
        return []
    if len(text) > _MAX_CARD_INPUT_SIZE:
        logger.warning("Rejecting oversized input (%s bytes) for collab card parsing", len(text))
        return []

    text = _sanitize_text(text)
    masked = _mask_code_fences(text)
    found: List[tuple[int, str, str, str]] = []
    for card_type, pattern in _CARD_BLOCK_PATTERNS:
        for match in pattern.finditer(masked):
            body = text[match.start(1):match.end(1)] if match.group(1) else ""
            raw_block = text[match.start():match.end()]
            found.append((match.start(), card_type, body, raw_block))
    found.sort(key=lambda item: item[0])

    specs: List[InputCardSpec | TelemetryCardSpec] = []
    for _, card_type, block, raw_block in found[:_MAX_CARD_BLOCKS]:
        spec = _parse_input_card_block(block, raw_block) if card_type == "input" else _parse_telemetry_card_block(block, raw_block)
        if spec:
            specs.append(spec)
    return specs


def parse_input_card_blocks(text: str) -> List[InputCardSpec]:
    return [spec for spec in parse_collab_card_blocks(text) if isinstance(spec, InputCardSpec)]


def parse_telemetry_card_blocks(text: str) -> List[TelemetryCardSpec]:
    return [spec for spec in parse_collab_card_blocks(text) if isinstance(spec, TelemetryCardSpec)]


def strip_collab_card_blocks(text: str, remove_unconfirmed: bool = False) -> str:
    if not text:
        return ""

    code_ranges = [(m.start(), m.end()) for m in re.finditer(r"```.*?```", text, flags=re.S)]

    def _in_code(start: int, end: int) -> bool:
        return any(start >= cs and end <= ce for cs, ce in code_ranges)

    out = text
    for _, pattern in _CARD_BLOCK_PATTERNS:
        def _replace(match: re.Match[str]) -> str:
            if _in_code(match.start(), match.end()):
                return match.group(0)
            body = match.group(1) or ""
            confirm_match = re.search(r"(?im)^\s*(?:confirm|enabled)\s*:\s*(.+)$", body)
            confirmed = True
            if confirm_match:
                confirmed = _parse_bool(confirm_match.group(1), default=True)
            if confirmed or remove_unconfirmed:
                return ""
            return re.sub(r"(?im)^\s*(?:confirm|enabled)\s*:.*$", "", body).strip()

        out = pattern.sub(_replace, out)
    return out.strip()


def _parse_input_card_block(block: str, raw_block: str) -> Optional[InputCardSpec]:
    title = None
    prompt_lines: List[str] = []
    summary_lines: List[str] = []
    options: List[str] = []
    permissions: List[str] = []
    editors: List[str] = []
    kind = "text"
    status = "open"
    required = False
    card_id = None
    confirmed = True
    section: Optional[str] = None

    for line in block.splitlines():
        raw_line = line.rstrip()
        stripped = raw_line.strip()
        if not stripped:
            if section == "prompt":
                prompt_lines.append("")
            elif section == "summary":
                summary_lines.append("")
            continue
        m = re.match(r"^([A-Za-z0-9_\-]+)\s*:\s*(.*)", stripped)
        if m:
            key = m.group(1).lower()
            val = (m.group(2) or "").strip()
            if key in ("title", "name", "subject"):
                title = val or title
                section = None
                continue
            if key in ("prompt", "question", "ask", "request", "body", "details"):
                if val:
                    prompt_lines.append(val)
                section = "prompt"
                continue
            if key in ("summary", "context", "description"):
                if val:
                    summary_lines.append(val)
                section = "summary"
                continue
            if key in ("kind", "type", "response_type", "input_type"):
                candidate = val.lower().replace("-", "_")
                kind = candidate if candidate in INPUT_CARD_KINDS else "text"
                section = None
                continue
            if key in ("options", "choices"):
                options.extend(_split_tokens(val))
                section = "options"
                continue
            if key in ("target", "targets", "allowed", "permissions", "responders", "recipients", "audience"):
                permissions.extend(_split_tokens(val))
                section = None
                continue
            if key in ("editors", "owners", "maintainers"):
                editors.extend(_split_tokens(val))
                section = None
                continue
            if key == "status":
                candidate = val.lower().replace("-", "_")
                status = candidate if candidate in INPUT_CARD_STATUSES else "open"
                section = None
                continue
            if key in ("required", "must_answer"):
                required = _parse_bool(val, default=False)
                section = None
                continue
            if key in ("id", "card_id", "input_card_id"):
                card_id = val or card_id
                section = None
                continue
            if key in ("confirm", "enabled"):
                confirmed = _parse_bool(val, default=True)
                section = None
                continue

        if section == "options":
            option = stripped.lstrip("-*").strip()
            if option:
                options.append(option)
        elif section == "summary":
            summary_lines.append(stripped.lstrip("-*").strip())
        else:
            prompt_lines.append(stripped.lstrip("-*").strip())

    prompt = "\n".join(prompt_lines).strip() or None
    summary = "\n".join(summary_lines).strip() or None
    if not title:
        title = (summary or prompt or "Input requested").splitlines()[0][:120]
    if not title:
        return None
    if kind in ("choice", "multi_choice", "approval", "decision") and not options and kind != "approval":
        kind = "text"

    return InputCardSpec(
        title=title,
        prompt=prompt,
        summary=summary,
        kind=kind,
        options=list(dict.fromkeys(options)),
        permissions=list(dict.fromkeys(permissions)),
        editors=list(dict.fromkeys(editors)),
        status=status,
        required=required,
        card_id=card_id,
        confirmed=confirmed,
        raw=raw_block,
    )


def _parse_telemetry_card_block(block: str, raw_block: str) -> Optional[TelemetryCardSpec]:
    title = None
    summary_lines: List[str] = []
    metrics: List[Dict[str, Any]] = []
    status = "running"
    progress: Optional[int] = None
    stage = None
    permissions: List[str] = []
    editors: List[str] = []
    severity = None
    card_id = None
    confirmed = True
    section: Optional[str] = None

    for line in block.splitlines():
        raw_line = line.rstrip()
        stripped = raw_line.strip()
        if not stripped:
            if section == "summary":
                summary_lines.append("")
            continue
        m = re.match(r"^([A-Za-z0-9_\-]+)\s*:\s*(.*)", stripped)
        if m:
            key = m.group(1).lower()
            val = (m.group(2) or "").strip()
            if key in ("title", "name", "subject"):
                title = val or title
                section = None
                continue
            if key in ("summary", "context", "description", "details"):
                if val:
                    summary_lines.append(val)
                section = "summary"
                continue
            if key == "status":
                candidate = val.lower().replace("-", "_")
                status = candidate if candidate in TELEMETRY_CARD_STATUSES else "running"
                section = None
                continue
            if key in ("progress", "percent", "completion"):
                progress = _parse_progress(val)
                section = None
                continue
            if key in ("stage", "phase", "step"):
                stage = val or stage
                section = None
                continue
            if key in ("severity", "level"):
                severity = val.lower().replace(" ", "_") if val else severity
                section = None
                continue
            if key in ("metrics", "telemetry", "data"):
                parsed = _parse_jsonish(val)
                if isinstance(parsed, list):
                    metrics.extend([m for m in parsed if isinstance(m, dict)])
                    section = None
                elif val:
                    metric = _parse_metric_line(val)
                    if metric:
                        metrics.append(metric)
                    section = "metrics"
                else:
                    section = "metrics"
                continue
            if key in ("allowed", "permissions", "viewers", "audience"):
                permissions.extend(_split_tokens(val))
                section = None
                continue
            if key in ("editors", "owners", "maintainers", "updaters"):
                editors.extend(_split_tokens(val))
                section = None
                continue
            if key in ("id", "card_id", "telemetry_card_id"):
                card_id = val or card_id
                section = None
                continue
            if key in ("confirm", "enabled"):
                confirmed = _parse_bool(val, default=True)
                section = None
                continue

        if section == "metrics":
            metric = _parse_metric_line(stripped)
            if metric:
                metrics.append(metric)
        elif section == "summary":
            summary_lines.append(stripped.lstrip("-*").strip())

    summary = "\n".join(summary_lines).strip() or None
    if not title:
        title = (summary or stage or "Telemetry").splitlines()[0][:120]
    if not title:
        return None

    return TelemetryCardSpec(
        title=title,
        summary=summary,
        status=status,
        progress=progress,
        stage=stage,
        metrics=metrics,
        permissions=list(dict.fromkeys(permissions)),
        editors=list(dict.fromkeys(editors)),
        severity=severity,
        card_id=card_id,
        confirmed=confirmed,
        raw=raw_block,
    )


class CollabCardManager:
    """Durable storage and permission checks for input and telemetry cards."""

    def __init__(self, db: DatabaseManager):
        self.db = db
        logger.info("Initializing CollabCardManager")
        self._ensure_tables()
        logger.info("CollabCardManager initialized successfully")

    def _ensure_tables(self) -> None:
        with self.db.get_connection() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS collab_cards (
                    id TEXT PRIMARY KEY,
                    card_type TEXT NOT NULL,
                    title TEXT NOT NULL,
                    summary TEXT,
                    prompt TEXT,
                    status TEXT DEFAULT 'open',
                    owner_id TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    source_type TEXT,
                    source_id TEXT,
                    channel_id TEXT,
                    visibility TEXT DEFAULT 'network',
                    origin_peer TEXT,
                    permissions TEXT,
                    editors TEXT,
                    config TEXT,
                    telemetry TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    closed_at TIMESTAMP,
                    expires_at TIMESTAMP
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS collab_card_responses (
                    id TEXT PRIMARY KEY,
                    card_id TEXT NOT NULL,
                    responder_id TEXT NOT NULL,
                    response_type TEXT DEFAULT 'text',
                    value TEXT,
                    comment TEXT,
                    metadata TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(card_id, responder_id),
                    FOREIGN KEY (card_id) REFERENCES collab_cards(id) ON DELETE CASCADE
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_collab_cards_source ON collab_cards(source_type, source_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_collab_cards_type_status ON collab_cards(card_type, status)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_collab_card_responses_card ON collab_card_responses(card_id)")
            conn.commit()

    def _json_loads(self, raw: Any, fallback: Any) -> Any:
        if raw is None or raw == "":
            return fallback
        try:
            return json.loads(raw)
        except Exception:
            return fallback

    def _row_to_card(self, row: Any) -> Dict[str, Any]:
        data = dict(row)
        data["permissions"] = self._json_loads(data.get("permissions"), [])
        data["editors"] = self._json_loads(data.get("editors"), [])
        data["config"] = self._json_loads(data.get("config"), {})
        data["telemetry"] = self._json_loads(data.get("telemetry"), {})
        return data

    def _row_to_response(self, row: Any) -> Dict[str, Any]:
        data = dict(row)
        data["metadata"] = self._json_loads(data.get("metadata"), {})
        raw_value = data.get("value")
        parsed = self._json_loads(raw_value, None)
        data["value"] = parsed if parsed is not None else raw_value
        return data

    def _actor_tokens(self, actor_id: Optional[str]) -> set[str]:
        if not actor_id:
            return set()
        tokens = {actor_id.strip(), actor_id.strip().lower()}
        try:
            user = self.db.get_user(actor_id)
        except Exception:
            user = None
        if user:
            for key in ("id", "username", "display_name"):
                value = str(user.get(key) or "").strip()
                if value:
                    tokens.add(value)
                    tokens.add(value.lower())
                    tokens.add(f"@{value}")
                    tokens.add(f"@{value.lower()}")
        return {token for token in tokens if token}

    def _matches_actor(self, values: Iterable[Any], actor_id: Optional[str]) -> bool:
        tokens = self._actor_tokens(actor_id)
        normalized_values = set()
        for value in values or []:
            raw = str(value or "").strip()
            if raw:
                normalized_values.add(raw)
                normalized_values.add(raw.lower())
                if raw.startswith("@"):
                    normalized_values.add(raw[1:])
                    normalized_values.add(raw[1:].lower())
                else:
                    normalized_values.add(f"@{raw}")
                    normalized_values.add(f"@{raw.lower()}")
        return bool(tokens & normalized_values)

    def can_update(self, card: Dict[str, Any], actor_id: Optional[str], admin_user_id: Optional[str] = None) -> bool:
        if not actor_id:
            return False
        if admin_user_id and actor_id == admin_user_id:
            return True
        if actor_id in (card.get("owner_id"), card.get("created_by")):
            return True
        return self._matches_actor(card.get("editors") or [], actor_id)

    def can_respond(self, card: Dict[str, Any], actor_id: Optional[str], admin_user_id: Optional[str] = None) -> bool:
        if not actor_id:
            return False
        if card.get("card_type") != "input":
            return False
        if (card.get("status") or "open") not in ("open", "waiting"):
            return False
        if self.can_update(card, actor_id, admin_user_id=admin_user_id):
            return True
        allowed = card.get("permissions") or []
        if not allowed:
            return True
        if any(str(v).strip().lower() in ("*", "all", "everyone", "channel") for v in allowed):
            return True
        return self._matches_actor(allowed, actor_id)

    def can_view_source(self, card: Dict[str, Any], actor_id: Optional[str], admin_user_id: Optional[str] = None) -> bool:
        """Check source visibility before exposing or mutating a card by direct ID."""
        if not actor_id:
            return False
        if admin_user_id and actor_id == admin_user_id:
            return True
        if actor_id in (card.get("owner_id"), card.get("created_by")):
            return True

        source_type = str(card.get("source_type") or "").strip()
        source_id = str(card.get("source_id") or "").strip()
        if source_type == "feed_post" and source_id:
            return self._can_view_feed_source(source_id, actor_id)
        if source_type == "channel_message":
            channel_id = str(card.get("channel_id") or "").strip()
            return self._can_view_channel_source(channel_id, source_id, actor_id)

        visibility = str(card.get("visibility") or "network").strip().lower()
        if visibility in {"public", "network", "local"}:
            return True
        return self._matches_actor(card.get("permissions") or [], actor_id) or self._matches_actor(card.get("editors") or [], actor_id)

    def _can_view_feed_source(self, post_id: str, actor_id: str) -> bool:
        try:
            with self.db.get_connection() as conn:
                row = conn.execute(
                    "SELECT author_id, visibility FROM feed_posts WHERE id = ?",
                    (post_id,),
                ).fetchone()
                if not row:
                    return False
                author_id = str(row["author_id"] if hasattr(row, "keys") else row[0])
                visibility = str(row["visibility"] if hasattr(row, "keys") else row[1]).strip().lower()
                if actor_id == author_id:
                    return True
                if visibility in {"public", "network", "trusted"}:
                    return True
                if visibility == "custom":
                    perm = conn.execute(
                        "SELECT 1 FROM post_permissions WHERE post_id = ? AND user_id = ? LIMIT 1",
                        (post_id, actor_id),
                    ).fetchone()
                    return bool(perm)
                return False
        except Exception as exc:
            logger.warning("Failed to evaluate feed source visibility for card source %s: %s", post_id, exc)
            return False

    def _can_view_channel_source(self, channel_id: str, message_id: str, actor_id: str) -> bool:
        try:
            with self.db.get_connection() as conn:
                resolved_channel_id = channel_id
                if not resolved_channel_id and message_id:
                    row = conn.execute(
                        "SELECT channel_id FROM channel_messages WHERE id = ?",
                        (message_id,),
                    ).fetchone()
                    if row:
                        resolved_channel_id = str(row["channel_id"] if hasattr(row, "keys") else row[0])
                if not resolved_channel_id:
                    return False
                membership = conn.execute(
                    "SELECT 1 FROM channel_members WHERE channel_id = ? AND user_id = ? LIMIT 1",
                    (resolved_channel_id, actor_id),
                ).fetchone()
                return bool(membership)
        except Exception as exc:
            logger.warning("Failed to evaluate channel source visibility for card source %s: %s", message_id or channel_id, exc)
            return False

    def upsert_input_card(
        self,
        *,
        card_id: str,
        spec: InputCardSpec,
        created_by: str,
        owner_id: str,
        source_type: str,
        source_id: str,
        visibility: str = "network",
        origin_peer: Optional[str] = None,
        channel_id: Optional[str] = None,
        permissions: Optional[List[str]] = None,
        editors: Optional[List[str]] = None,
        created_at: Optional[Any] = None,
        actor_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        telemetry: Dict[str, Any] = {}
        config = {
            "kind": spec.kind,
            "options": spec.options,
            "required": spec.required,
        }
        return self.upsert_card(
            card_id=card_id,
            card_type="input",
            title=spec.title,
            summary=spec.summary,
            prompt=spec.prompt,
            status=spec.status,
            created_by=created_by,
            owner_id=owner_id,
            source_type=source_type,
            source_id=source_id,
            visibility=visibility,
            origin_peer=origin_peer,
            channel_id=channel_id,
            permissions=permissions if permissions is not None else spec.permissions,
            editors=editors if editors is not None else spec.editors,
            config=config,
            telemetry=telemetry,
            created_at=created_at,
            actor_id=actor_id,
        )

    def upsert_telemetry_card(
        self,
        *,
        card_id: str,
        spec: TelemetryCardSpec,
        created_by: str,
        owner_id: str,
        source_type: str,
        source_id: str,
        visibility: str = "network",
        origin_peer: Optional[str] = None,
        channel_id: Optional[str] = None,
        permissions: Optional[List[str]] = None,
        editors: Optional[List[str]] = None,
        created_at: Optional[Any] = None,
        actor_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        telemetry = {
            "progress": spec.progress,
            "stage": spec.stage,
            "metrics": spec.metrics,
            "severity": spec.severity,
        }
        config = {"kind": "telemetry"}
        return self.upsert_card(
            card_id=card_id,
            card_type="telemetry",
            title=spec.title,
            summary=spec.summary,
            prompt=None,
            status=spec.status,
            created_by=created_by,
            owner_id=owner_id,
            source_type=source_type,
            source_id=source_id,
            visibility=visibility,
            origin_peer=origin_peer,
            channel_id=channel_id,
            permissions=permissions if permissions is not None else spec.permissions,
            editors=editors if editors is not None else spec.editors,
            config=config,
            telemetry=telemetry,
            created_at=created_at,
            actor_id=actor_id,
        )

    def upsert_card(
        self,
        *,
        card_id: str,
        card_type: str,
        title: str,
        created_by: str,
        owner_id: str,
        source_type: str,
        source_id: str,
        summary: Optional[str] = None,
        prompt: Optional[str] = None,
        status: Optional[str] = None,
        visibility: str = "network",
        origin_peer: Optional[str] = None,
        channel_id: Optional[str] = None,
        permissions: Optional[List[str]] = None,
        editors: Optional[List[str]] = None,
        config: Optional[Dict[str, Any]] = None,
        telemetry: Optional[Dict[str, Any]] = None,
        created_at: Optional[Any] = None,
        actor_id: Optional[str] = None,
        preserve_runtime: bool = True,
    ) -> Optional[Dict[str, Any]]:
        if not card_id or card_type not in CARD_TYPES or not title or not created_by:
            return None
        now = _now_iso()
        created_value = str(created_at or now)
        permissions_json = json.dumps(permissions or [])
        editors_json = json.dumps(editors or [])
        config_json = json.dumps(config or {})
        telemetry_json = json.dumps(telemetry or {})

        try:
            with self.db.get_connection() as conn:
                row = conn.execute("SELECT * FROM collab_cards WHERE id = ?", (card_id,)).fetchone()
                if row:
                    existing = self._row_to_card(row)
                    if actor_id and not self.can_update(existing, actor_id):
                        logger.debug("Actor %s not authorized to update collab card %s", actor_id, card_id)
                        return existing
                    # Inline blocks define the card shell. Runtime fields such as
                    # status, responses, and telemetry are updated through card
                    # endpoints and must not be reset merely because a post is
                    # rendered or re-synced. Snapshot ingestion opts out so mesh
                    # peers can receive live state changes.
                    if preserve_runtime:
                        status_to_store = existing.get("status") or status or ("open" if card_type == "input" else "running")
                        telemetry_to_store = existing.get("telemetry") or telemetry or {}
                    else:
                        status_to_store = status or existing.get("status") or ("open" if card_type == "input" else "running")
                        telemetry_to_store = telemetry if telemetry is not None else (existing.get("telemetry") or {})
                    conn.execute(
                        """
                        UPDATE collab_cards
                           SET card_type = ?, title = ?, summary = ?, prompt = ?, status = ?,
                               owner_id = ?, source_type = ?, source_id = ?, channel_id = ?,
                               visibility = ?, origin_peer = ?, permissions = ?, editors = ?,
                               config = ?, telemetry = ?, updated_at = ?
                         WHERE id = ?
                        """,
                        (
                            card_type,
                            title,
                            summary,
                            prompt,
                            status_to_store,
                            owner_id,
                            source_type,
                            source_id,
                            channel_id,
                            visibility,
                            origin_peer,
                            permissions_json,
                            editors_json,
                            config_json,
                            json.dumps(telemetry_to_store),
                            now,
                            card_id,
                        ),
                    )
                else:
                    conn.execute(
                        """
                        INSERT INTO collab_cards
                        (id, card_type, title, summary, prompt, status, owner_id, created_by,
                         source_type, source_id, channel_id, visibility, origin_peer, permissions,
                         editors, config, telemetry, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            card_id,
                            card_type,
                            title,
                            summary,
                            prompt,
                            status or ("open" if card_type == "input" else "running"),
                            owner_id,
                            created_by,
                            source_type,
                            source_id,
                            channel_id,
                            visibility,
                            origin_peer,
                            permissions_json,
                            editors_json,
                            config_json,
                            telemetry_json,
                            created_value,
                            now,
                        ),
                    )
                conn.commit()
            return self.get_card(card_id)
        except Exception as exc:
            logger.error("Failed to upsert collab card %s: %s", card_id, exc, exc_info=True)
            return None

    def get_card(
        self,
        card_id: str,
        *,
        viewer_id: Optional[str] = None,
        admin_user_id: Optional[str] = None,
        include_responses: bool = True,
    ) -> Optional[Dict[str, Any]]:
        if not card_id:
            return None
        try:
            with self.db.get_connection() as conn:
                row = conn.execute("SELECT * FROM collab_cards WHERE id = ?", (card_id,)).fetchone()
            if not row:
                return None
            card = self._row_to_card(row)
            if viewer_id is not None and not self.can_view_source(card, viewer_id, admin_user_id=admin_user_id):
                return None
            return self._decorate_card(card, viewer_id=viewer_id, admin_user_id=admin_user_id, include_responses=include_responses)
        except Exception as exc:
            logger.error("Failed to get collab card %s: %s", card_id, exc)
            return None

    def list_cards_for_source(
        self,
        source_type: str,
        source_id: str,
        *,
        viewer_id: Optional[str] = None,
        admin_user_id: Optional[str] = None,
        include_responses: bool = True,
    ) -> List[Dict[str, Any]]:
        if not source_type or not source_id:
            return []
        try:
            with self.db.get_connection() as conn:
                rows = conn.execute(
                    """
                    SELECT * FROM collab_cards
                    WHERE source_type = ? AND source_id = ?
                    ORDER BY created_at ASC, id ASC
                    """,
                    (source_type, source_id),
                ).fetchall()
            cards = []
            for row in rows:
                card = self._row_to_card(row)
                if viewer_id is not None and not self.can_view_source(card, viewer_id, admin_user_id=admin_user_id):
                    continue
                cards.append(self._decorate_card(card, viewer_id=viewer_id, admin_user_id=admin_user_id, include_responses=include_responses))
            return cards
        except Exception as exc:
            logger.error("Failed to list collab cards for %s:%s: %s", source_type, source_id, exc)
            return []

    def list_cards(
        self,
        *,
        card_type: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 50,
        viewer_id: Optional[str] = None,
        admin_user_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        limit_val = max(1, min(int(limit or 50), 200))
        clauses: List[str] = []
        params: List[Any] = []
        if card_type in CARD_TYPES:
            clauses.append("card_type = ?")
            params.append(card_type)
        if status:
            clauses.append("status = ?")
            params.append(status)
        query = "SELECT * FROM collab_cards"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit_val)
        try:
            with self.db.get_connection() as conn:
                rows = conn.execute(query, params).fetchall()
            cards = []
            for row in rows:
                card = self._row_to_card(row)
                if viewer_id is not None and not self.can_view_source(card, viewer_id, admin_user_id=admin_user_id):
                    continue
                cards.append(self._decorate_card(card, viewer_id=viewer_id, admin_user_id=admin_user_id, include_responses=True))
            return cards
        except Exception as exc:
            logger.error("Failed to list collab cards: %s", exc)
            return []

    def list_responses(self, card_id: str) -> List[Dict[str, Any]]:
        if not card_id:
            return []
        try:
            with self.db.get_connection() as conn:
                rows = conn.execute(
                    """
                    SELECT * FROM collab_card_responses
                    WHERE card_id = ?
                    ORDER BY updated_at DESC, created_at DESC
                    """,
                    (card_id,),
                ).fetchall()
            return [self._row_to_response(row) for row in rows]
        except Exception as exc:
            logger.error("Failed to list card responses for %s: %s", card_id, exc)
            return []

    def submit_response(
        self,
        card_id: str,
        *,
        responder_id: str,
        value: Any,
        response_type: str = "text",
        comment: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        admin_user_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        card = self.get_card(card_id, include_responses=False)
        if not card:
            raise KeyError("Card not found")
        if not self.can_view_source(card, responder_id, admin_user_id=admin_user_id):
            raise PermissionError("Not authorized to view this card")
        if not self.can_respond(card, responder_id, admin_user_id=admin_user_id):
            raise PermissionError("Not authorized to respond to this card")
        if card.get("card_type") != "input":
            raise ValueError("Only input cards accept responses")

        response_id = f"card_response_{card_id}_{responder_id}"
        now = _now_iso()
        value_json = json.dumps(value) if isinstance(value, (dict, list, bool, int, float)) else str(value or "")
        metadata_json = json.dumps(metadata or {})
        with self.db.get_connection() as conn:
            conn.execute(
                """
                INSERT INTO collab_card_responses
                (id, card_id, responder_id, response_type, value, comment, metadata, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(card_id, responder_id) DO UPDATE SET
                    response_type = excluded.response_type,
                    value = excluded.value,
                    comment = excluded.comment,
                    metadata = excluded.metadata,
                    updated_at = excluded.updated_at
                """,
                (response_id, card_id, responder_id, response_type or "text", value_json, comment, metadata_json, now, now),
            )
            conn.execute("UPDATE collab_cards SET updated_at = ? WHERE id = ?", (now, card_id))
            conn.commit()
        updated = self.get_card(card_id, viewer_id=responder_id, admin_user_id=admin_user_id)
        if not updated:
            raise KeyError("Card not found after response")
        return updated

    def update_telemetry(
        self,
        card_id: str,
        *,
        actor_id: str,
        status: Optional[str] = None,
        progress: Optional[Any] = None,
        stage: Optional[str] = None,
        summary: Optional[str] = None,
        metrics: Optional[List[Dict[str, Any]]] = None,
        telemetry_patch: Optional[Dict[str, Any]] = None,
        admin_user_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        card = self.get_card(card_id, include_responses=False)
        if not card:
            raise KeyError("Card not found")
        if card.get("card_type") != "telemetry":
            raise ValueError("Only telemetry cards accept telemetry updates")
        if not self.can_view_source(card, actor_id, admin_user_id=admin_user_id):
            raise PermissionError("Not authorized to view this telemetry card")
        if not self.can_update(card, actor_id, admin_user_id=admin_user_id):
            raise PermissionError("Not authorized to update this telemetry card")

        telemetry = dict(card.get("telemetry") or {})
        if telemetry_patch:
            telemetry.update({k: v for k, v in telemetry_patch.items() if k not in ("id", "card_id")})
        parsed_progress = _parse_progress(progress)
        if parsed_progress is not None:
            telemetry["progress"] = parsed_progress
        if stage is not None:
            telemetry["stage"] = str(stage).strip()
        if metrics is not None:
            telemetry["metrics"] = [m for m in metrics if isinstance(m, dict)]
        if status is not None:
            status_clean = str(status).strip().lower().replace("-", "_")
            if status_clean in TELEMETRY_CARD_STATUSES:
                status = status_clean
            else:
                status = card.get("status") or "running"

        now = _now_iso()
        with self.db.get_connection() as conn:
            updates = ["telemetry = ?", "updated_at = ?"]
            values: List[Any] = [json.dumps(telemetry), now]
            if status is not None:
                updates.append("status = ?")
                values.append(status)
            if summary is not None:
                updates.append("summary = ?")
                values.append(summary)
            values.append(card_id)
            conn.execute(f"UPDATE collab_cards SET {', '.join(updates)} WHERE id = ?", values)
            conn.commit()
        updated = self.get_card(card_id, viewer_id=actor_id, admin_user_id=admin_user_id)
        if not updated:
            raise KeyError("Card not found after telemetry update")
        return updated

    def update_input_status(
        self,
        card_id: str,
        *,
        actor_id: str,
        status: str,
        admin_user_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        card = self.get_card(card_id, include_responses=False)
        if not card:
            raise KeyError("Card not found")
        if card.get("card_type") != "input":
            raise ValueError("Only input cards have input status")
        if not self.can_view_source(card, actor_id, admin_user_id=admin_user_id):
            raise PermissionError("Not authorized to view this input card")
        if not self.can_update(card, actor_id, admin_user_id=admin_user_id):
            raise PermissionError("Not authorized to update this input card")
        status_clean = str(status or "").strip().lower().replace("-", "_")
        if status_clean not in INPUT_CARD_STATUSES:
            raise ValueError("Invalid input card status")
        now = _now_iso()
        closed_at = now if status_clean in ("resolved", "closed", "cancelled") else None
        with self.db.get_connection() as conn:
            conn.execute(
                "UPDATE collab_cards SET status = ?, updated_at = ?, closed_at = ? WHERE id = ?",
                (status_clean, now, closed_at, card_id),
            )
            conn.commit()
        updated = self.get_card(card_id, viewer_id=actor_id, admin_user_id=admin_user_id)
        if not updated:
            raise KeyError("Card not found after status update")
        return updated

    def ingest_card_snapshot(self, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if not isinstance(payload, dict):
            return None
        card_id = str(payload.get("id") or "").strip()
        card_type = str(payload.get("card_type") or "").strip()
        title = str(payload.get("title") or "").strip()
        created_by = str(payload.get("created_by") or payload.get("owner_id") or "").strip()
        owner_id = str(payload.get("owner_id") or created_by).strip()
        if not card_id or card_type not in CARD_TYPES or not title or not created_by:
            return None
        return self.upsert_card(
            card_id=card_id,
            card_type=card_type,
            title=title,
            summary=payload.get("summary"),
            prompt=payload.get("prompt"),
            status=payload.get("status"),
            created_by=created_by,
            owner_id=owner_id,
            source_type=payload.get("source_type") or "api",
            source_id=payload.get("source_id") or card_id,
            visibility=payload.get("visibility") or "network",
            origin_peer=payload.get("origin_peer"),
            channel_id=payload.get("channel_id"),
            permissions=payload.get("permissions") if isinstance(payload.get("permissions"), list) else [],
            editors=payload.get("editors") if isinstance(payload.get("editors"), list) else [],
            config=payload.get("config") if isinstance(payload.get("config"), dict) else {},
            telemetry=payload.get("telemetry") if isinstance(payload.get("telemetry"), dict) else {},
            created_at=payload.get("created_at"),
            actor_id=None,
            preserve_runtime=False,
        )

    def ingest_response_snapshot(self, card_id: str, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if not card_id or not isinstance(payload, dict):
            return None
        responder_id = str(payload.get("responder_id") or "").strip()
        if not responder_id:
            return None
        response_id = str(payload.get("id") or f"card_response_{card_id}_{responder_id}").strip()
        now = str(payload.get("updated_at") or _now_iso())
        value = payload.get("value")
        value_json = json.dumps(value) if isinstance(value, (dict, list, bool, int, float)) else str(value or "")
        with self.db.get_connection() as conn:
            conn.execute(
                """
                INSERT INTO collab_card_responses
                (id, card_id, responder_id, response_type, value, comment, metadata, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(card_id, responder_id) DO UPDATE SET
                    response_type = excluded.response_type,
                    value = excluded.value,
                    comment = excluded.comment,
                    metadata = excluded.metadata,
                    updated_at = excluded.updated_at
                """,
                (
                    response_id,
                    card_id,
                    responder_id,
                    payload.get("response_type") or "text",
                    value_json,
                    payload.get("comment"),
                    json.dumps(payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}),
                    payload.get("created_at") or now,
                    now,
                ),
            )
            conn.execute("UPDATE collab_cards SET updated_at = ? WHERE id = ?", (now, card_id))
            conn.commit()
        return self.get_card(card_id)

    def _decorate_card(
        self,
        card: Dict[str, Any],
        *,
        viewer_id: Optional[str] = None,
        admin_user_id: Optional[str] = None,
        include_responses: bool = True,
    ) -> Dict[str, Any]:
        decorated = dict(card)
        responses = self.list_responses(card.get("id")) if include_responses else []
        my_response = None
        if viewer_id:
            for response in responses:
                if response.get("responder_id") == viewer_id:
                    my_response = response
                    break
        decorated["response_count"] = len(responses)
        decorated["my_response"] = my_response
        decorated["can_update"] = self.can_update(decorated, viewer_id, admin_user_id=admin_user_id)
        decorated["can_respond"] = self.can_respond(decorated, viewer_id, admin_user_id=admin_user_id)
        decorated["status_label"] = str(decorated.get("status") or "").replace("_", " ").title()
        decorated["type_label"] = "Input" if decorated.get("card_type") == "input" else "Telemetry"
        if include_responses and (decorated["can_update"] or str((decorated.get("config") or {}).get("responses_visible") or "").lower() in ("1", "true", "yes", "all")):
            decorated["responses"] = responses
        elif my_response:
            decorated["responses"] = [my_response]
        else:
            decorated["responses"] = []
        return decorated

    def new_ad_hoc_card_id(self, card_type: str) -> str:
        prefix = "input" if card_type == "input" else "telemetry"
        return f"{prefix}_card_{secrets.token_hex(8)}"
