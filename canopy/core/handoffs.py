"""
Handoff notes for Canopy.

Handoffs are lightweight, structured notes created inline in posts or channel
messages so humans and agents can capture state and next steps without new UI.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .database import DatabaseManager

logger = logging.getLogger('canopy.handoffs')

_HANDOFF_BLOCK_PATTERNS = [
    re.compile(r"(?is)\[handoff\](.*?)\[/handoff\]"),
    re.compile(r"(?is)::handoff\s*(.*?)\s*::endhandoff"),
]

_CONFIRM_FALSE = {'false', 'no', 'off', '0'}
_CONFIRM_TRUE = {'true', 'yes', 'on', '1'}

_CODE_FENCE_RE = re.compile(r"```[\s\S]*?```")


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _parse_datetime(value: Any) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        raw = str(value).strip()
        if not raw:
            return None
        try:
            dt = datetime.fromisoformat(raw.replace('Z', '+00:00'))
        except Exception:
            try:
                dt = datetime.strptime(raw, '%Y-%m-%d %H:%M:%S')
            except Exception:
                return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _format_db_timestamp(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')


def _mask_code_fences(text: str) -> str:
    return _CODE_FENCE_RE.sub(lambda m: "\x00" * len(m.group(0)), text)


def _split_tags(raw: str) -> List[str]:
    if not raw:
        return []
    parts = re.split(r"[,;]", raw)
    cleaned: List[str] = []
    for part in parts:
        tag = part.strip()
        if not tag:
            continue
        if tag.startswith('#'):
            tag = tag[1:]
        if tag:
            cleaned.append(tag)
    # preserve order, remove duplicates
    seen = set()
    ordered: List[str] = []
    for tag in cleaned:
        if tag in seen:
            continue
        seen.add(tag)
        ordered.append(tag)
    return ordered


@dataclass
class HandoffSpec:
    title: str
    summary: Optional[str] = None
    next_steps: Optional[List[str]] = None
    owner: Optional[str] = None
    tags: Optional[List[str]] = None
    handoff_id: Optional[str] = None
    confirmed: bool = True
    raw: Optional[str] = None
    start: Optional[int] = None
    end: Optional[int] = None
    # Capability routing fields
    required_capabilities: Optional[List[str]] = None
    escalation_level: Optional[str] = None  # 'normal' | 'elevated' | 'admin'
    return_to: Optional[str] = None  # user_id for return routing after completion
    context_payload: Optional[Dict[str, Any]] = None  # structured context for receiving agent

    def to_dict(self) -> Dict[str, Any]:
        return {
            'title': self.title,
            'summary': self.summary,
            'next_steps': self.next_steps or [],
            'owner': self.owner,
            'tags': self.tags or [],
            'handoff_id': self.handoff_id,
            'confirmed': self.confirmed,
            'required_capabilities': self.required_capabilities or [],
            'escalation_level': self.escalation_level,
            'return_to': self.return_to,
            'context_payload': self.context_payload,
        }


@dataclass
class Handoff:
    id: str
    source_type: str
    source_id: str
    channel_id: Optional[str]
    author_id: str
    title: str
    summary: Optional[str]
    next_steps: Optional[List[str]]
    owner: Optional[str]
    tags: Optional[List[str]]
    raw: Optional[str]
    created_at: datetime
    updated_at: datetime
    visibility: str
    origin_peer: Optional[str]
    permissions: Optional[List[str]]
    required_capabilities: Optional[List[str]] = None
    escalation_level: Optional[str] = None
    return_to: Optional[str] = None
    context_payload: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data['created_at'] = self.created_at.isoformat() if self.created_at else None
        data['updated_at'] = self.updated_at.isoformat() if self.updated_at else None
        return data


def derive_handoff_id(source_type: str, source_id: str, index: int = 0, total: int = 1,
                      override: Optional[str] = None) -> str:
    if override:
        cleaned = override.strip()
        if cleaned:
            return cleaned if cleaned.startswith('handoff_') else f"handoff_{cleaned}"
    base = f"handoff_{source_type}_{source_id}"
    if total > 1:
        return f"{base}_{index + 1}"
    return base


def parse_handoff_blocks(text: str) -> List[HandoffSpec]:
    if not text:
        return []

    masked = _mask_code_fences(text)
    specs: List[HandoffSpec] = []

    for pattern in _HANDOFF_BLOCK_PATTERNS:
        for match in pattern.finditer(masked):
            block_full = text[match.start():match.end()]
            inner = pattern.search(block_full)
            block = inner.group(1) if inner else ''
            title = None
            summary_lines: List[str] = []
            next_steps: List[str] = []
            owner = None
            tags: List[str] = []
            handoff_id = None
            confirmed = True
            current_section = None
            required_capabilities: List[str] = []
            escalation_level = None
            return_to = None

            for line in block.splitlines():
                stripped = line.strip()
                if not stripped:
                    continue
                lower = stripped.lower()

                if lower.startswith(('title:', 'handoff:', 'topic:', 'name:')):
                    title = stripped.split(':', 1)[1].strip() or title
                    current_section = None
                    continue
                if lower.startswith(('summary:', 'context:', 'notes:', 'description:', 'details:', 'body:')):
                    val = stripped.split(':', 1)[1].strip()
                    if val:
                        summary_lines.append(val)
                    current_section = 'summary'
                    continue
                if lower.startswith(('next:', 'next_steps:', 'actions:', 'followup:', 'todo:', 'tasks:')):
                    val = stripped.split(':', 1)[1].strip()
                    if val:
                        next_steps.append(val)
                    current_section = 'next'
                    continue
                if lower.startswith(('owner:', 'assignee:', 'responsible:', 'lead:')):
                    owner = stripped.split(':', 1)[1].strip() or owner
                    current_section = None
                    continue
                if lower.startswith(('tags:', 'tag:')):
                    raw_tags = stripped.split(':', 1)[1].strip()
                    tags.extend(_split_tags(raw_tags))
                    current_section = None
                    continue
                if lower.startswith(('id:', 'handoff_id:')):
                    handoff_id = stripped.split(':', 1)[1].strip() or handoff_id
                    current_section = None
                    continue
                if lower.startswith(('confirm:', 'enabled:')):
                    raw = stripped.split(':', 1)[1].strip().lower()
                    if raw in _CONFIRM_FALSE:
                        confirmed = False
                    elif raw in _CONFIRM_TRUE:
                        confirmed = True
                    current_section = None
                    continue
                if lower.startswith(('capabilities:', 'required_capabilities:', 'requires:', 'needs:')):
                    raw_caps = stripped.split(':', 1)[1].strip()
                    required_capabilities = _split_tags(raw_caps)
                    current_section = None
                    continue
                if lower.startswith(('escalation:', 'escalation_level:', 'priority_level:')):
                    escalation_level = stripped.split(':', 1)[1].strip().lower()
                    if escalation_level not in ('normal', 'elevated', 'admin'):
                        escalation_level = 'normal'
                    current_section = None
                    continue
                if lower.startswith(('return_to:', 'return:', 'callback:')):
                    return_to = stripped.split(':', 1)[1].strip()
                    current_section = None
                    continue

                if current_section == 'next' and stripped.startswith(('-', '*')):
                    step = stripped.lstrip('-*').strip()
                    if step:
                        next_steps.append(step)
                    continue
                if current_section == 'summary':
                    summary_lines.append(stripped)
                    continue

                if not title:
                    title = stripped
                else:
                    summary_lines.append(stripped)

            if not title:
                if summary_lines:
                    title = summary_lines[0][:120]
                else:
                    title = 'Handoff'

            summary = "\n".join(summary_lines).strip() if summary_lines else None
            tags_final = tags or None
            next_final = next_steps or None

            specs.append(HandoffSpec(
                title=title,
                summary=summary,
                next_steps=next_final,
                owner=owner,
                tags=tags_final,
                handoff_id=handoff_id,
                confirmed=confirmed,
                raw=block.strip() if block else None,
                start=match.start(),
                end=match.end(),
                required_capabilities=required_capabilities or None,
                escalation_level=escalation_level,
                return_to=return_to,
            ))

    return specs


def strip_handoff_blocks(text: str, remove_unconfirmed: bool = False) -> str:
    """Remove confirmed handoff blocks from text, optionally removing unconfirmed too.

    Blocks inside triple-backtick code fences are preserved as-is.
    If remove_unconfirmed=False, unconfirmed blocks are replaced with their body
    (confirm line removed) so the content still reads well.
    """
    if not text:
        return text

    code_ranges: list = []
    for m in _CODE_FENCE_RE.finditer(text):
        code_ranges.append((m.start(), m.end()))

    def _in_code_fence(start: int, end: int) -> bool:
        for cs, ce in code_ranges:
            if start >= cs and end <= ce:
                return True
        return False

    pattern = re.compile(r"(?is)\[handoff\](.*?)\[/handoff\]")

    def _replace(match):
        if _in_code_fence(match.start(), match.end()):
            return match.group(0)
        body = match.group(1) or ''
        confirm_match = re.search(r"(?im)^\s*confirm\s*:\s*(.+)$", body)
        confirmed = True
        if confirm_match:
            val = confirm_match.group(1).strip().lower()
            if val in _CONFIRM_FALSE:
                confirmed = False
        if confirmed or remove_unconfirmed:
            return ''
        cleaned_body = re.sub(r"(?im)^\s*confirm\s*:.*$", "", body).strip()
        return cleaned_body

    cleaned_text = pattern.sub(_replace, text)
    return cleaned_text.strip()


class HandoffManager:
    """Stores and retrieves handoff notes."""

    def __init__(self, db: DatabaseManager):
        self.db = db
        logger.info("Initializing HandoffManager")
        self._ensure_tables()
        logger.info("HandoffManager initialized successfully")

    def _ensure_tables(self) -> None:
        try:
            with self.db.get_connection() as conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS handoff_notes (
                        id TEXT PRIMARY KEY,
                        source_type TEXT NOT NULL,
                        source_id TEXT NOT NULL,
                        channel_id TEXT,
                        author_id TEXT NOT NULL,
                        title TEXT NOT NULL,
                        summary TEXT,
                        next_steps TEXT,
                        owner TEXT,
                        tags TEXT,
                        raw TEXT,
                        permissions TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        visibility TEXT DEFAULT 'network',
                        origin_peer TEXT
                    )
                """)
                # Non-destructive migrations
                columns = [
                    ('summary', 'TEXT'),
                    ('next_steps', 'TEXT'),
                    ('owner', 'TEXT'),
                    ('tags', 'TEXT'),
                    ('raw', 'TEXT'),
                    ('permissions', 'TEXT'),
                    ('updated_at', 'TIMESTAMP DEFAULT CURRENT_TIMESTAMP'),
                    ('visibility', "TEXT DEFAULT 'network'"),
                    ('origin_peer', 'TEXT'),
                    ('required_capabilities', 'TEXT'),
                    ('escalation_level', 'TEXT'),
                    ('return_to', 'TEXT'),
                    ('context_payload', 'TEXT'),
                ]
                for col, col_def in columns:
                    try:
                        conn.execute(f"ALTER TABLE handoff_notes ADD COLUMN {col} {col_def}")
                    except Exception:
                        pass
                conn.execute("CREATE INDEX IF NOT EXISTS idx_handoff_notes_updated_at ON handoff_notes(updated_at)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_handoff_notes_channel ON handoff_notes(channel_id)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_handoff_notes_author ON handoff_notes(author_id)")
                conn.commit()
        except Exception as e:
            logger.error(f"Failed to ensure handoff tables: {e}", exc_info=True)
            raise

    def _row_to_handoff(self, row: Any) -> Handoff:
        tags = None
        next_steps = None
        permissions = None
        try:
            if row['tags']:
                tags = json.loads(row['tags'])
        except Exception:
            tags = None
        try:
            if row['next_steps']:
                next_steps = json.loads(row['next_steps'])
        except Exception:
            next_steps = None
        try:
            if row['permissions']:
                permissions = json.loads(row['permissions'])
        except Exception:
            permissions = None

        required_capabilities = None
        context_payload = None
        try:
            raw_caps = row['required_capabilities']
            if raw_caps:
                required_capabilities = json.loads(raw_caps)
        except Exception:
            required_capabilities = None
        try:
            raw_ctx = row['context_payload']
            if raw_ctx:
                context_payload = json.loads(raw_ctx)
        except Exception:
            context_payload = None

        escalation_level = None
        return_to = None
        try:
            escalation_level = row['escalation_level']
        except Exception:
            pass
        try:
            return_to = row['return_to']
        except Exception:
            pass

        return Handoff(
            id=row['id'],
            source_type=row['source_type'],
            source_id=row['source_id'],
            channel_id=row['channel_id'],
            author_id=row['author_id'],
            title=row['title'],
            summary=row['summary'],
            next_steps=next_steps,
            owner=row['owner'],
            tags=tags,
            raw=row['raw'],
            created_at=_parse_datetime(row['created_at']) or _now_utc(),
            updated_at=_parse_datetime(row['updated_at']) or _now_utc(),
            visibility=row['visibility'] or 'network',
            origin_peer=row['origin_peer'],
            permissions=permissions,
            required_capabilities=required_capabilities,
            escalation_level=escalation_level,
            return_to=return_to,
            context_payload=context_payload,
        )

    def get_handoff(self, handoff_id: str) -> Optional[Handoff]:
        if not handoff_id:
            return None
        try:
            with self.db.get_connection() as conn:
                row = conn.execute(
                    "SELECT * FROM handoff_notes WHERE id = ?",
                    (handoff_id,)
                ).fetchone()
            if not row:
                return None
            return self._row_to_handoff(row)
        except Exception as e:
            logger.error(f"Failed to get handoff {handoff_id}: {e}")
            return None

    def upsert_handoff(
        self,
        handoff_id: str,
        source_type: str,
        source_id: str,
        author_id: str,
        title: str,
        summary: Optional[str] = None,
        next_steps: Optional[List[str]] = None,
        owner: Optional[str] = None,
        tags: Optional[List[str]] = None,
        raw: Optional[str] = None,
        channel_id: Optional[str] = None,
        visibility: Optional[str] = None,
        origin_peer: Optional[str] = None,
        permissions: Optional[List[str]] = None,
        created_at: Optional[Any] = None,
        updated_at: Optional[Any] = None,
        required_capabilities: Optional[List[str]] = None,
        escalation_level: Optional[str] = None,
        return_to: Optional[str] = None,
        context_payload: Optional[Dict[str, Any]] = None,
    ) -> Optional[Handoff]:
        if not handoff_id or not title or not author_id:
            return None

        now_dt = _parse_datetime(updated_at) or _now_utc()
        created_dt = _parse_datetime(created_at) or now_dt
        created_db = _format_db_timestamp(created_dt)
        updated_db = _format_db_timestamp(now_dt)

        tags_json = json.dumps(tags) if tags is not None else None
        next_json = json.dumps(next_steps) if next_steps is not None else None
        permissions_json = json.dumps(permissions) if permissions is not None else None
        caps_json = json.dumps(required_capabilities) if required_capabilities is not None else None
        ctx_json = json.dumps(context_payload) if context_payload is not None else None

        try:
            with self.db.get_connection() as conn:
                row = conn.execute("SELECT id, permissions FROM handoff_notes WHERE id = ?", (handoff_id,)).fetchone()
                if row:
                    if permissions is None and row['permissions']:
                        permissions_json = row['permissions']
                    conn.execute(
                        """
                        UPDATE handoff_notes
                        SET title = ?, summary = ?, next_steps = ?, owner = ?, tags = ?, raw = ?,
                            channel_id = ?, visibility = ?, origin_peer = ?, permissions = ?,
                            required_capabilities = ?, escalation_level = ?, return_to = ?,
                            context_payload = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (
                            title,
                            summary,
                            next_json,
                            owner,
                            tags_json,
                            raw,
                            channel_id,
                            visibility or 'network',
                            origin_peer,
                            permissions_json,
                            caps_json,
                            escalation_level,
                            return_to,
                            ctx_json,
                            updated_db,
                            handoff_id,
                        ),
                    )
                else:
                    conn.execute(
                        """
                        INSERT INTO handoff_notes
                        (id, source_type, source_id, channel_id, author_id, title, summary, next_steps,
                         owner, tags, raw, permissions, created_at, updated_at, visibility, origin_peer,
                         required_capabilities, escalation_level, return_to, context_payload)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            handoff_id,
                            source_type,
                            source_id,
                            channel_id,
                            author_id,
                            title,
                            summary,
                            next_json,
                            owner,
                            tags_json,
                            raw,
                            permissions_json,
                            created_db,
                            updated_db,
                            visibility or 'network',
                            origin_peer,
                            caps_json,
                            escalation_level,
                            return_to,
                            ctx_json,
                        ),
                    )
                conn.commit()
            return self.get_handoff(handoff_id)
        except Exception as e:
            logger.error(f"Failed to upsert handoff {handoff_id}: {e}", exc_info=True)
            return None

    def list_handoffs(
        self,
        limit: int = 50,
        since: Optional[Any] = None,
        channel_id: Optional[str] = None,
        author_id: Optional[str] = None,
        source_type: Optional[str] = None,
        viewer_id: Optional[str] = None,
    ) -> List[Handoff]:
        limit_val = max(1, min(int(limit or 50), 200))
        since_dt = _parse_datetime(since) if since else None
        since_db = _format_db_timestamp(since_dt) if since_dt else None

        query = "SELECT * FROM handoff_notes"
        clauses: List[str] = []
        params: List[Any] = []
        if channel_id:
            clauses.append("channel_id = ?")
            params.append(channel_id)
        if author_id:
            clauses.append("author_id = ?")
            params.append(author_id)
        if source_type:
            clauses.append("source_type = ?")
            params.append(source_type)
        if since_db:
            clauses.append("updated_at > ?")
            params.append(since_db)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit_val)

        try:
            with self.db.get_connection() as conn:
                rows = conn.execute(query, params).fetchall()

                channel_memberships = set()
                if viewer_id:
                    try:
                        member_rows = conn.execute(
                            "SELECT channel_id FROM channel_members WHERE user_id = ?",
                            (viewer_id,)
                        ).fetchall()
                        channel_memberships = {r['channel_id'] for r in member_rows}
                    except Exception:
                        channel_memberships = set()

            results: List[Handoff] = []
            for row in rows:
                handoff = self._row_to_handoff(row)
                if viewer_id:
                    # Channel visibility gating
                    if handoff.channel_id and handoff.channel_id not in channel_memberships:
                        continue
                    # Feed visibility gating
                    if handoff.visibility == 'private' and handoff.author_id != viewer_id:
                        continue
                    if handoff.visibility == 'custom':
                        allowed = False
                        if handoff.permissions and viewer_id in handoff.permissions:
                            allowed = True
                        if handoff.author_id == viewer_id:
                            allowed = True
                        if not allowed:
                            continue
                results.append(handoff)
            return results
        except Exception as e:
            logger.error(f"Failed to list handoffs: {e}", exc_info=True)
            return []

    def list_handoffs_since(self, since: Any, limit: int = 50, viewer_id: Optional[str] = None) -> List[Handoff]:
        return self.list_handoffs(limit=limit, since=since, viewer_id=viewer_id)

    def get_latest_timestamp(self) -> Optional[str]:
        try:
            with self.db.get_connection() as conn:
                row = conn.execute(
                    "SELECT MAX(updated_at) AS latest FROM handoff_notes"
                ).fetchone()
            return row['latest'] if row and row['latest'] else None
        except Exception:
            return None
