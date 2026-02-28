"""
Signal management for Canopy.

Signals capture structured, durable memory objects extracted from posts/messages.
They are designed for multi-agent collaboration on structured data (e.g. decisions,
requirements, research findings, claim sets), with independent TTL and ownership.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

from .database import DatabaseManager

logger = logging.getLogger("canopy.signals")

SIGNAL_STATUSES = ("draft", "active", "locked", "archived")
SIGNAL_DEFAULT_TTL_SECONDS = 30 * 24 * 3600  # 30 days

_SIGNAL_BLOCK_PATTERNS = [
    re.compile(r"(?is)\[signal\](.*?)\[/signal\]"),
    re.compile(r"(?is)::signal\s*(.*?)\s*::endsignal"),
    # Bracket-colon format: [signal: ... ] where closing ] is unindented on its own line
    re.compile(r"(?s)\[signal:\s*(.*?)\n\]"),
]

_MAX_SIGNAL_BLOCKS = 20
_MAX_SIGNAL_INPUT_SIZE = 1_000_000  # 1MB


@dataclass
class SignalSpec:
    signal_type: str
    title: str
    summary: Optional[str] = None
    owner: Optional[str] = None
    status: Optional[str] = None
    tags: List[str] = field(default_factory=list)
    confidence: Optional[float] = None
    expires_at: Optional[datetime] = None
    ttl_seconds: Optional[int] = None
    ttl_mode: Optional[str] = None
    data: Optional[Dict[str, Any]] = None
    data_raw: Optional[str] = None
    notes: Optional[str] = None
    signal_id: Optional[str] = None
    raw: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload["expires_at"] = self.expires_at.isoformat() if self.expires_at else None
        return payload


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _parse_dt(value: Any) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        raw = str(value).strip()
        if not raw:
            return None
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _parse_ttl(value: str) -> Optional[int]:
    if not value:
        return None
    raw = str(value).strip().lower()
    if not raw:
        return None
    try:
        if raw.isdigit():
            return int(raw)
        m = re.match(r"^(\d+)\s*([smhdw]|mo|q|y)$", raw)
        if not m:
            return None
        amount = int(m.group(1))
        unit = m.group(2)
        seconds = amount
        if unit == "m":
            seconds *= 60
        elif unit == "h":
            seconds *= 3600
        elif unit == "d":
            seconds *= 86400
        elif unit == "w":
            seconds *= 7 * 86400
        elif unit == "mo":
            seconds *= 30 * 86400
        elif unit == "q":
            seconds *= 90 * 86400
        elif unit == "y":
            seconds *= 365 * 86400
        return seconds
    except Exception:
        return None


def _normalize_confidence(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        v = float(str(value).strip())
        if v < 0:
            return None
        if v > 1.0 and v <= 100.0:
            v = v / 100.0
        return max(0.0, min(1.0, v))
    except Exception:
        return None


def _mask_code_fences(text: str) -> str:
    return re.sub(r"```.*?```", lambda m: "\x00" * len(m.group(0)), text, flags=re.S)


def _sanitize_text(text: str) -> str:
    if not text:
        return text
    text = text.replace("\x00", "")
    text = re.sub(r"[\x01-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)
    text = re.sub(r"[\u200b-\u200f\u2028-\u202f\u2060\ufeff]", "", text)
    return text


def _parse_tags(value: str) -> List[str]:
    tags: List[str] = []
    if not value:
        return tags
    for raw in value.split(","):
        tag = raw.strip()
        if tag:
            tags.append(tag)
    return tags


def _parse_structured_text(raw: str) -> Optional[Dict[str, Any]]:
    """Parse YAML-like key:value text into a proper dict.

    Handles quoted strings, boolean literals, single-line and multi-line
    arrays, and nested indented continuation lines.
    """
    import textwrap
    lines = textwrap.dedent(raw).splitlines()
    result: Dict[str, Any] = {}
    current_key: Optional[str] = None
    current_array: Optional[List[str]] = None

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue

        m = re.match(r"^([A-Za-z0-9_\-]+)\s*:\s*(.*)", stripped)
        if m:
            # Flush any pending multi-line array
            if current_key is not None and current_array is not None:
                result[current_key] = current_array
                current_array = None
                current_key = None

            key = m.group(1)
            val = m.group(2).strip()

            if not val:
                result[key] = ""
                current_key = key
                continue

            # Single-line array: key: [a, b, c]
            if val.startswith("[") and val.endswith("]"):
                try:
                    result[key] = json.loads(val)
                except Exception:
                    result[key] = val
                current_key = key
                continue

            # Multi-line array start: key: [
            if val == "[":
                current_key = key
                current_array = []
                continue

            # Booleans
            if val.lower() in ("true", "yes"):
                result[key] = True
                current_key = key
                continue
            if val.lower() in ("false", "no"):
                result[key] = False
                current_key = key
                continue

            # Strip surrounding quotes
            if len(val) >= 2 and val[0] in ('"', "'") and val[-1] == val[0]:
                val = val[1:-1]

            result[key] = val
            current_key = key

        elif current_array is not None:
            # Inside a multi-line array
            if stripped == "]":
                if current_key is not None:
                    result[current_key] = current_array
                current_array = None
                current_key = None
            else:
                item = stripped.rstrip(",").strip()
                if len(item) >= 2 and item[0] in ('"', "'") and item[-1] == item[0]:
                    item = item[1:-1]
                if item:
                    current_array.append(item)

    # Flush any trailing array
    if current_key is not None and current_array is not None:
        result[current_key] = current_array

    return result if result else None


def _parse_data_block(raw: str) -> Dict[str, Any]:
    if not raw:
        return {}
    # Try JSON first
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return parsed
        return {"_value": parsed}
    except Exception:
        pass
    # Try YAML if available
    try:
        import yaml  # type: ignore[import-untyped]  # optional
        parsed = yaml.safe_load(raw)
        if isinstance(parsed, dict):
            return parsed
        return {"_value": parsed}
    except Exception:
        pass
    # Try structured key:value text parsing
    try:
        parsed = _parse_structured_text(raw)
        if parsed:
            return parsed
    except Exception:
        pass
    return {"_raw": raw.strip()}


def derive_signal_id(source_type: str, source_id: str, index: int = 0,
                     total: int = 1, override: Optional[str] = None) -> str:
    if override:
        cleaned = override.strip()
        if cleaned:
            return cleaned if cleaned.startswith("signal_") else f"signal_{cleaned}"
    base = f"signal_{source_type}_{source_id}"
    if total > 1:
        return f"{base}_{index + 1}"
    return base


def parse_signal_blocks(text: str) -> List[SignalSpec]:
    if not text:
        return []
    if len(text) > _MAX_SIGNAL_INPUT_SIZE:
        logger.warning(f"Rejecting oversized input ({len(text)} bytes) for signal parsing")
        return []

    text = _sanitize_text(text)
    masked = _mask_code_fences(text)

    specs: List[SignalSpec] = []
    for pattern in _SIGNAL_BLOCK_PATTERNS:
        for match in pattern.finditer(masked):
            if len(specs) >= _MAX_SIGNAL_BLOCKS:
                logger.warning("Signal block limit reached; ignoring extras")
                break

            block = text[match.start(1):match.end(1)] if match.group(1) else ""
            raw_block = text[match.start():match.end()]

            signal_type = None
            title = None
            summary = None
            owner = None
            status = None
            tags: List[str] = []
            confidence = None
            expires_at = None
            ttl_seconds = None
            ttl_mode = None
            data_raw_lines: List[str] = []
            notes_lines: List[str] = []
            signal_id = None

            in_data = False
            in_notes = False
            in_unrecognized = False  # collecting continuation lines after an unrecognized field

            # Known top-level field prefixes (lowercase) for exit-detection
            _KNOWN_PREFIXES = (
                "type:", "signal:", "kind:", "title:", "name:",
                "summary:", "desc:", "description:",
                "owner:", "facilitator:", "status:", "tags:",
                "confidence:", "conf:", "expires:", "expires_at:",
                "expiry:", "deadline:", "filing_target:",
                "ttl:", "ttl_seconds:", "ttl_mode:",
                "id:", "signal_id:", "data:", "notes:",
            )

            for line in block.splitlines():
                raw_line = line.rstrip()
                stripped = raw_line.strip()
                if not stripped:
                    if in_data:
                        data_raw_lines.append("")
                    elif in_notes:
                        notes_lines.append("")
                    elif in_unrecognized:
                        data_raw_lines.append("")
                    continue

                lower = stripped.lower()
                is_field_like = re.match(r"^[A-Za-z0-9_\-]+\s*:", stripped)

                # Exit multi-line collection when we hit a new top-level field
                if (in_data or in_notes or in_unrecognized) and is_field_like:
                    # Determine indent level: unindented or same-level = new field
                    # For bracket-colon format everything is indented, so we use
                    # a heuristic: if it matches a known prefix OR is at the same
                    # indent as a typical top-level field, exit collection
                    is_known = lower.startswith(_KNOWN_PREFIXES)
                    is_unindented = not raw_line.startswith((" ", "\t"))
                    if is_known or is_unindented:
                        in_data = False
                        in_notes = False
                        in_unrecognized = False

                if in_data:
                    data_raw_lines.append(raw_line)
                    continue
                if in_notes:
                    notes_lines.append(raw_line)
                    continue
                if in_unrecognized:
                    # Continuation line for an unrecognized multi-line field
                    data_raw_lines.append(raw_line)
                    continue

                # --- Recognized fields ---
                if lower.startswith(("type:", "signal:", "kind:")):
                    signal_type = stripped.split(":", 1)[1].strip() or signal_type
                    continue
                if lower.startswith(("title:", "name:")):
                    title = stripped.split(":", 1)[1].strip() or title
                    continue
                if lower.startswith(("summary:", "desc:", "description:")):
                    summary = stripped.split(":", 1)[1].strip() or summary
                    continue
                if lower.startswith(("owner:", "facilitator:")):
                    owner = stripped.split(":", 1)[1].strip() or owner
                    continue
                if lower.startswith(("status:",)):
                    status = stripped.split(":", 1)[1].strip().lower() or status
                    continue
                if lower.startswith(("tags:",)):
                    tags = _parse_tags(stripped.split(":", 1)[1].strip())
                    continue
                if lower.startswith(("confidence:", "conf:")):
                    confidence = _normalize_confidence(stripped.split(":", 1)[1].strip())
                    continue
                if lower.startswith(("expires:", "expires_at:", "expiry:", "deadline:", "filing_target:")):
                    expires_at = _parse_dt(stripped.split(":", 1)[1].strip()) or expires_at
                    continue
                if lower.startswith(("ttl:", "ttl_seconds:")):
                    ttl_seconds = _parse_ttl(stripped.split(":", 1)[1].strip()) or ttl_seconds
                    continue
                if lower.startswith(("ttl_mode:",)):
                    ttl_mode = stripped.split(":", 1)[1].strip().lower() or ttl_mode
                    continue
                if lower.startswith(("id:", "signal_id:")):
                    signal_id = stripped.split(":", 1)[1].strip() or signal_id
                    continue
                if lower.startswith("data:"):
                    in_data = True
                    remainder = stripped.split(":", 1)[1].strip()
                    if remainder:
                        data_raw_lines.append(remainder)
                    continue
                if lower.startswith("notes:"):
                    in_notes = True
                    remainder = stripped.split(":", 1)[1].strip()
                    if remainder:
                        notes_lines.append(remainder)
                    continue

                # --- Unrecognized field-like lines → structured data ---
                if is_field_like:
                    val = stripped.split(":", 1)[1].strip()
                    # If value starts [ or { it's likely multi-line (array/object)
                    if val and val[0] in ("[", "{") and val[-1] not in ("]", "}"):
                        in_unrecognized = True
                    data_raw_lines.append(raw_line)
                    continue

                # --- Catch-all: any unmatched line goes to data ---
                # (array items, quoted strings, bare values, etc.)
                data_raw_lines.append(raw_line)

            # --- Auto-derive title from natural data fields ---
            if not title:
                _TITLE_FIELDS = (
                    "decision:", "outcome:", "finding:", "result:",
                    "conclusion:", "topic:", "subject:", "name:",
                )
                for dline in data_raw_lines:
                    dl = dline.strip().lower()
                    if dl.startswith(_TITLE_FIELDS):
                        candidate = dline.strip().split(":", 1)[1].strip().strip('"').strip("'")
                        if candidate:
                            title = candidate
                            break

            # --- Auto-derive type from content clues ---
            if not signal_type:
                data_lower = "\n".join(data_raw_lines).lower()
                if "claim" in data_lower:
                    signal_type = "claimset"
                elif "decision:" in data_lower:
                    signal_type = "decision"
                elif "finding:" in data_lower or "result:" in data_lower:
                    signal_type = "finding"
                elif "requirement" in data_lower:
                    signal_type = "requirement"

            # --- Auto-derive summary from outcome or first substantial data line ---
            if not summary:
                for dline in data_raw_lines:
                    dl = dline.strip().lower()
                    if dl.startswith(("outcome:", "summary:", "abstract:")):
                        candidate = dline.strip().split(":", 1)[1].strip().strip('"').strip("'")
                        if candidate:
                            summary = candidate
                            break

            if not title:
                continue

            data_raw = "\n".join(data_raw_lines).strip() if data_raw_lines else None
            data = _parse_data_block(data_raw) if data_raw else None
            notes = "\n".join(notes_lines).strip() if notes_lines else None

            spec = SignalSpec(
                signal_type=signal_type or "signal",
                title=title,
                summary=summary,
                owner=owner,
                status=status,
                tags=tags,
                confidence=confidence,
                expires_at=expires_at,
                ttl_seconds=ttl_seconds,
                ttl_mode=ttl_mode,
                data=data,
                data_raw=data_raw,
                notes=notes,
                signal_id=signal_id,
                raw=raw_block,
            )
            specs.append(spec)

    return specs


def strip_signal_blocks(text: str) -> str:
    if not text:
        return ""
    masked = _mask_code_fences(text)
    out = text
    for pattern in _SIGNAL_BLOCK_PATTERNS:
        for match in pattern.finditer(masked):
            out = out.replace(text[match.start():match.end()], "").strip()
    return out


class SignalManager:
    """Manages structured Signal records."""

    def __init__(self, db: DatabaseManager):
        self.db = db
        logger.info("Initializing SignalManager")
        self._ensure_tables()
        logger.info("SignalManager initialized successfully")

    def _ensure_tables(self) -> None:
        try:
            with self.db.get_connection() as conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS signals (
                        id TEXT PRIMARY KEY,
                        type TEXT,
                        title TEXT NOT NULL,
                        summary TEXT,
                        status TEXT DEFAULT 'active',
                        confidence REAL,
                        tags TEXT,
                        data TEXT,
                        notes TEXT,
                        owner_id TEXT,
                        created_by TEXT NOT NULL,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        visibility TEXT DEFAULT 'network',
                        origin_peer TEXT,
                        source_type TEXT,
                        source_id TEXT,
                        expires_at TIMESTAMP,
                        ttl_seconds INTEGER,
                        ttl_mode TEXT
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS signal_versions (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        signal_id TEXT NOT NULL,
                        version INTEGER NOT NULL,
                        status TEXT DEFAULT 'accepted',
                        payload TEXT,
                        created_by TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        FOREIGN KEY (signal_id) REFERENCES signals(id) ON DELETE CASCADE
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS signal_links (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        from_signal TEXT NOT NULL,
                        to_signal TEXT NOT NULL,
                        relation TEXT DEFAULT 'relates',
                        created_by TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
                conn.execute("CREATE INDEX IF NOT EXISTS idx_signals_status ON signals(status)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_signals_owner ON signals(owner_id)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_signals_expires ON signals(expires_at)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_signal_versions_signal ON signal_versions(signal_id)")
                conn.commit()
        except Exception as e:
            logger.error(f"Failed to ensure signal tables: {e}", exc_info=True)
            raise

    def _normalize_status(self, status: Optional[str]) -> str:
        value = (status or "active").strip().lower()
        return value if value in SIGNAL_STATUSES else "active"

    def _resolve_expiry(self,
                        expires_at: Optional[Any],
                        ttl_seconds: Optional[int],
                        ttl_mode: Optional[str],
                        base_time: Optional[datetime] = None) -> Optional[datetime]:
        if ttl_mode in ("none", "no_expiry", "immortal"):
            return None
        if expires_at:
            return _parse_dt(expires_at)
        base = base_time or _now_utc()
        if ttl_seconds is not None:
            try:
                ttl_val = int(ttl_seconds)
            except (TypeError, ValueError):
                ttl_val = None
            if ttl_val is not None and ttl_val > 0:
                return base + timedelta(seconds=ttl_val)
        return base + timedelta(seconds=SIGNAL_DEFAULT_TTL_SECONDS)

    def _next_version(self, conn: Any, signal_id: str) -> int:
        row = conn.execute(
            "SELECT MAX(version) AS v FROM signal_versions WHERE signal_id = ?",
            (signal_id,)
        ).fetchone()
        max_v = row["v"] if row and row["v"] is not None else 0
        return int(max_v) + 1

    def _serialize_payload(self, payload: Dict[str, Any]) -> str:
        try:
            return json.dumps(payload)
        except Exception:
            return json.dumps({})

    def _row_to_signal(self, row: Any) -> Dict[str, Any]:
        data = dict(row)
        data["tags"] = [t for t in (data.get("tags") or "").split(",") if t]
        try:
            data["data"] = json.loads(data["data"]) if data.get("data") else None
        except Exception:
            data["data"] = None
        data["confidence"] = data.get("confidence")
        return data

    def _is_admin(self, actor_id: Optional[str]) -> bool:
        if not actor_id:
            return False
        admin_id = self.db.get_instance_owner_user_id()
        return bool(admin_id and actor_id == admin_id)

    def _can_manage(self, signal: Dict[str, Any], actor_id: Optional[str]) -> bool:
        if not actor_id:
            return False
        if self._is_admin(actor_id):
            return True
        return actor_id == signal.get("owner_id") or actor_id == signal.get("created_by")

    def upsert_signal(
        self,
        signal_id: str,
        signal_type: str,
        title: str,
        created_by: str,
        summary: Optional[str] = None,
        status: Optional[str] = None,
        confidence: Optional[float] = None,
        tags: Optional[List[str]] = None,
        data: Optional[Dict[str, Any]] = None,
        notes: Optional[str] = None,
        owner_id: Optional[str] = None,
        visibility: Optional[str] = None,
        origin_peer: Optional[str] = None,
        source_type: Optional[str] = None,
        source_id: Optional[str] = None,
        expires_at: Optional[Any] = None,
        ttl_seconds: Optional[int] = None,
        ttl_mode: Optional[str] = None,
        created_at: Optional[Any] = None,
        updated_at: Optional[Any] = None,
        actor_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        if not signal_id or not title:
            return None
        status_val = self._normalize_status(status)
        data_json = json.dumps(data) if data is not None else None
        tags_csv = ",".join(tags or [])
        created_dt = _parse_dt(created_at) or _now_utc()
        updated_dt = _parse_dt(updated_at) or _now_utc()
        expires_dt = self._resolve_expiry(expires_at, ttl_seconds, ttl_mode, base_time=created_dt)

        try:
            with self.db.get_connection() as conn:
                row = conn.execute("SELECT * FROM signals WHERE id = ?", (signal_id,)).fetchone()
                if row:
                    existing = self._row_to_signal(row)
                    if existing.get("status") == "locked" and not self._can_manage(existing, actor_id or created_by):
                        # Locked: ignore updates from non-owner/admin
                        return existing
                    conn.execute(
                        """
                        UPDATE signals
                        SET type = ?, title = ?, summary = ?, status = ?, confidence = ?, tags = ?,
                            data = ?, notes = ?, owner_id = ?, visibility = ?, origin_peer = ?,
                            source_type = ?, source_id = ?, expires_at = ?, ttl_seconds = ?, ttl_mode = ?,
                            updated_at = ?
                        WHERE id = ?
                        """,
                        (
                            signal_type,
                            title,
                            summary,
                            status_val,
                            confidence,
                            tags_csv,
                            data_json,
                            notes,
                            owner_id or existing.get("owner_id"),
                            visibility or existing.get("visibility"),
                            origin_peer or existing.get("origin_peer"),
                            source_type or existing.get("source_type"),
                            source_id or existing.get("source_id"),
                            expires_dt.isoformat() if expires_dt else None,
                            ttl_seconds,
                            ttl_mode,
                            updated_dt.isoformat(),
                            signal_id,
                        )
                    )
                    version = self._next_version(conn, signal_id)
                    payload = self.get_signal(signal_id)
                    conn.execute(
                        """
                        INSERT INTO signal_versions (signal_id, version, status, payload, created_by)
                        VALUES (?, ?, 'accepted', ?, ?)
                        """,
                        (signal_id, version, self._serialize_payload(payload or {}), actor_id or created_by),
                    )
                else:
                    conn.execute(
                        """
                        INSERT INTO signals
                        (id, type, title, summary, status, confidence, tags, data, notes, owner_id,
                         created_by, created_at, updated_at, visibility, origin_peer, source_type, source_id,
                         expires_at, ttl_seconds, ttl_mode)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            signal_id,
                            signal_type,
                            title,
                            summary,
                            status_val,
                            confidence,
                            tags_csv,
                            data_json,
                            notes,
                            owner_id or created_by,
                            created_by,
                            created_dt.isoformat(),
                            updated_dt.isoformat(),
                            visibility or "network",
                            origin_peer,
                            source_type,
                            source_id,
                            expires_dt.isoformat() if expires_dt else None,
                            ttl_seconds,
                            ttl_mode,
                        )
                    )
                    payload = self.get_signal(signal_id)
                    conn.execute(
                        """
                        INSERT INTO signal_versions (signal_id, version, status, payload, created_by)
                        VALUES (?, ?, 'accepted', ?, ?)
                        """,
                        (signal_id, 1, self._serialize_payload(payload or {}), actor_id or created_by),
                    )
                conn.commit()
            return self.get_signal(signal_id)
        except Exception as e:
            logger.error(f"Failed to upsert signal {signal_id}: {e}", exc_info=True)
            return None

    def get_signal(self, signal_id: str) -> Optional[Dict[str, Any]]:
        if not signal_id:
            return None
        try:
            with self.db.get_connection() as conn:
                row = conn.execute("SELECT * FROM signals WHERE id = ?", (signal_id,)).fetchone()
            if not row:
                return None
            return self._row_to_signal(row)
        except Exception as e:
            logger.error(f"Failed to get signal {signal_id}: {e}")
            return None

    def list_signals(self,
                     limit: int = 50,
                     signal_type: Optional[str] = None,
                     status: Optional[str] = None,
                     tag: Optional[str] = None) -> List[Dict[str, Any]]:
        try:
            limit_val = max(1, min(int(limit or 50), 200))
            clauses = []
            params: List[Any] = []
            if signal_type:
                clauses.append("type = ?")
                params.append(signal_type)
            if status:
                clauses.append("status = ?")
                params.append(self._normalize_status(status))
            if tag:
                escaped_tag = tag.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                clauses.append("tags LIKE ? ESCAPE '\\'")
                params.append(f"%{escaped_tag}%")
            query = "SELECT * FROM signals"
            if clauses:
                query += " WHERE " + " AND ".join(clauses)
            query += " ORDER BY updated_at DESC LIMIT ?"
            params.append(limit_val)

            results = []
            with self.db.get_connection() as conn:
                rows = conn.execute(query, params).fetchall()
                for row in rows:
                    results.append(self._row_to_signal(row))
            return results
        except Exception as e:
            logger.error(f"Failed to list signals: {e}")
            return []

    def update_signal(self, signal_id: str, updates: Dict[str, Any],
                      actor_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        if not signal_id or not updates:
            return self.get_signal(signal_id)
        try:
            with self.db.get_connection() as conn:
                row = conn.execute("SELECT * FROM signals WHERE id = ?", (signal_id,)).fetchone()
                if not row:
                    return None
                existing = self._row_to_signal(row)
                can_manage = self._can_manage(existing, actor_id)
                status_val = existing.get("status")
                if not can_manage:
                    return self.propose_update(signal_id, updates, actor_id=actor_id)
                if status_val == "locked" and not can_manage:
                    return self.propose_update(signal_id, updates, actor_id=actor_id)

                fields = []
                values: List[Any] = []
                if "title" in updates:
                    fields.append("title = ?")
                    values.append(updates.get("title"))
                if "summary" in updates:
                    fields.append("summary = ?")
                    values.append(updates.get("summary"))
                if "status" in updates:
                    fields.append("status = ?")
                    values.append(self._normalize_status(updates.get("status")))
                if "confidence" in updates:
                    fields.append("confidence = ?")
                    values.append(_normalize_confidence(updates.get("confidence")))
                if "tags" in updates:
                    tags = updates.get("tags") or []
                    if isinstance(tags, str):
                        tags = _parse_tags(tags)
                    fields.append("tags = ?")
                    values.append(",".join(tags))
                if "data" in updates:
                    data_val = updates.get("data")
                    fields.append("data = ?")
                    values.append(json.dumps(data_val) if data_val is not None else None)
                if "notes" in updates:
                    fields.append("notes = ?")
                    values.append(updates.get("notes"))
                if "owner_id" in updates:
                    fields.append("owner_id = ?")
                    values.append(updates.get("owner_id"))
                if "expires_at" in updates or "ttl_seconds" in updates or "ttl_mode" in updates:
                    expires_dt = self._resolve_expiry(
                        updates.get("expires_at"),
                        updates.get("ttl_seconds"),
                        updates.get("ttl_mode"),
                        base_time=_parse_dt(existing.get("created_at")) or _now_utc()
                    )
                    fields.append("expires_at = ?")
                    values.append(expires_dt.isoformat() if expires_dt else None)
                    fields.append("ttl_seconds = ?")
                    values.append(updates.get("ttl_seconds"))
                    fields.append("ttl_mode = ?")
                    values.append(updates.get("ttl_mode"))

                if not fields:
                    return existing

                fields.append("updated_at = ?")
                values.append(_now_utc().isoformat())
                values.append(signal_id)
                conn.execute(f"UPDATE signals SET {', '.join(fields)} WHERE id = ?", values)
                version = self._next_version(conn, signal_id)
                payload = self.get_signal(signal_id)
                conn.execute(
                    """
                    INSERT INTO signal_versions (signal_id, version, status, payload, created_by)
                    VALUES (?, ?, 'accepted', ?, ?)
                    """,
                    (signal_id, version, self._serialize_payload(payload or {}), actor_id),
                )
                conn.commit()
            return self.get_signal(signal_id)
        except Exception as e:
            logger.error(f"Failed to update signal {signal_id}: {e}")
            return None

    def propose_update(self, signal_id: str, updates: Dict[str, Any],
                       actor_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        if not signal_id or not updates:
            return None
        try:
            with self.db.get_connection() as conn:
                row = conn.execute("SELECT * FROM signals WHERE id = ?", (signal_id,)).fetchone()
                if not row:
                    return None
                existing = self._row_to_signal(row)
                payload = dict(existing)
                payload.update(updates or {})
                version = self._next_version(conn, signal_id)
                conn.execute(
                    """
                    INSERT INTO signal_versions (signal_id, version, status, payload, created_by)
                    VALUES (?, ?, 'pending', ?, ?)
                    """,
                    (signal_id, version, self._serialize_payload(payload), actor_id),
                )
                conn.commit()
            return {"signal_id": signal_id, "proposal_version": version, "status": "pending"}
        except Exception as e:
            logger.error(f"Failed to propose update for {signal_id}: {e}")
            return None

    def apply_proposal(self, signal_id: str, version: int,
                       actor_id: Optional[str] = None, accept: bool = True) -> Optional[Dict[str, Any]]:
        if not signal_id or not version:
            return None
        try:
            with self.db.get_connection() as conn:
                existing_row = conn.execute("SELECT * FROM signals WHERE id = ?", (signal_id,)).fetchone()
                if not existing_row:
                    return None
                existing = self._row_to_signal(existing_row)
                if not self._can_manage(existing, actor_id):
                    return None
                row = conn.execute(
                    "SELECT * FROM signal_versions WHERE signal_id = ? AND version = ?",
                    (signal_id, version)
                ).fetchone()
                if not row:
                    return None
                payload_raw = row["payload"] or "{}"
                try:
                    payload = json.loads(payload_raw)
                except Exception:
                    payload = {}
                status = "accepted" if accept else "rejected"
                conn.execute(
                    "UPDATE signal_versions SET status = ? WHERE id = ?",
                    (status, row["id"])
                )
                if accept:
                    fields: List[str] = []
                    values: List[Any] = []
                    for key in ("title", "summary", "status", "confidence", "tags", "data", "notes", "owner_id", "expires_at", "ttl_seconds", "ttl_mode"):
                        if key not in payload:
                            continue
                        if key == "tags":
                            tags = payload.get("tags") or []
                            if isinstance(tags, list):
                                values.append(",".join(tags))
                            else:
                                values.append(str(tags))
                            fields.append("tags = ?")
                        elif key == "data":
                            values.append(json.dumps(payload.get("data")) if payload.get("data") is not None else None)
                            fields.append("data = ?")
                        else:
                            values.append(payload.get(key))
                            fields.append(f"{key} = ?")
                    fields.append("updated_at = ?")
                    values.append(_now_utc().isoformat())
                    values.append(signal_id)
                    if fields:
                        conn.execute(f"UPDATE signals SET {', '.join(fields)} WHERE id = ?", values)
                conn.commit()
            return self.get_signal(signal_id)
        except Exception as e:
            logger.error(f"Failed to apply proposal for {signal_id}: {e}")
            return None

    def list_proposals(self, signal_id: str, status: str = "pending") -> List[Dict[str, Any]]:
        if not signal_id:
            return []
        try:
            with self.db.get_connection() as conn:
                rows = conn.execute(
                    """
                    SELECT version, status, payload, created_by, created_at
                    FROM signal_versions
                    WHERE signal_id = ? AND status = ?
                    ORDER BY version DESC
                    """,
                    (signal_id, status),
                ).fetchall()
            proposals = []
            for row in rows or []:
                payload: Dict[str, Any] = {}
                try:
                    payload = json.loads(row["payload"]) if row["payload"] else {}
                except Exception:
                    payload = {}
                proposals.append({
                    "version": row["version"],
                    "status": row["status"],
                    "payload": payload,
                    "created_by": row["created_by"],
                    "created_at": row["created_at"],
                })
            return proposals
        except Exception as e:
            logger.error(f"Failed to list proposals for {signal_id}: {e}")
            return []

    def lock_signal(self, signal_id: str, actor_id: Optional[str], locked: bool = True) -> Optional[Dict[str, Any]]:
        if not signal_id:
            return None
        try:
            with self.db.get_connection() as conn:
                row = conn.execute("SELECT * FROM signals WHERE id = ?", (signal_id,)).fetchone()
                if not row:
                    return None
                existing = self._row_to_signal(row)
                if not self._can_manage(existing, actor_id):
                    return None
                status_val = "locked" if locked else "active"
                conn.execute(
                    "UPDATE signals SET status = ?, updated_at = ? WHERE id = ?",
                    (status_val, _now_utc().isoformat(), signal_id)
                )
                version = self._next_version(conn, signal_id)
                payload = self.get_signal(signal_id)
                conn.execute(
                    """
                    INSERT INTO signal_versions (signal_id, version, status, payload, created_by)
                    VALUES (?, ?, 'accepted', ?, ?)
                    """,
                    (signal_id, version, self._serialize_payload(payload or {}), actor_id),
                )
                conn.commit()
            return self.get_signal(signal_id)
        except Exception as e:
            logger.error(f"Failed to lock/unlock signal {signal_id}: {e}")
            return None

    def purge_expired_signals(self) -> int:
        try:
            now = _now_utc().isoformat()
            with self.db.get_connection() as conn:
                rows = conn.execute(
                    "SELECT id FROM signals WHERE expires_at IS NOT NULL AND expires_at <= ?",
                    (now,)
                ).fetchall()
                if not rows:
                    return 0
                conn.execute(
                    "DELETE FROM signals WHERE expires_at IS NOT NULL AND expires_at <= ?",
                    (now,)
                )
                conn.commit()
                return len(rows)
        except Exception as e:
            logger.error(f"Failed to purge expired signals: {e}")
            return 0
