"""
Request management for Canopy.

Requests are structured asks embedded inline in posts or channel messages so
agents and humans can coordinate work with explicit ownership, status, and due dates.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Set, cast

from .database import DatabaseManager

logger = logging.getLogger('canopy.requests')

REQUEST_STATUSES = ('open', 'acknowledged', 'in_progress', 'completed', 'closed', 'cancelled')
REQUEST_PRIORITIES = ('low', 'normal', 'high', 'critical')
REQUEST_ROLES = ('assignee', 'reviewer', 'watcher')

_REQUEST_BLOCK_PATTERNS = [
    re.compile(r"(?is)\[request\](.*?)\[/request\]"),
    re.compile(r"(?is)::request\s*(.*?)\s*::endrequest"),
    re.compile(r"(?s)\[request:\s*(.*?)\n\]"),
]

_MAX_REQUEST_BLOCKS = 20
_MAX_REQUEST_INPUT_SIZE = 1_000_000  # 1MB

_CONFIRM_FALSE = {'false', 'no', 'off', '0'}
_CONFIRM_TRUE = {'true', 'yes', 'on', '1'}


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
        dt = datetime.fromisoformat(raw.replace('Z', '+00:00'))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _parse_relative_due(raw: str) -> Optional[datetime]:
    try:
        value = raw.strip().lower()
        if not value:
            return None
        m = re.match(r"^(\d+)\s*([smhdw])$", value)
        if not m:
            return None
        amount = int(m.group(1))
        unit = m.group(2)
        seconds = amount
        if unit == 'm':
            seconds *= 60
        elif unit == 'h':
            seconds *= 3600
        elif unit == 'd':
            seconds *= 24 * 3600
        elif unit == 'w':
            seconds *= 7 * 24 * 3600
        return _now_utc() + timedelta(seconds=seconds)
    except Exception:
        return None


def _mask_code_fences(text: str) -> str:
    return re.sub(r"```.*?```", lambda m: "\x00" * len(m.group(0)), text, flags=re.S)


def _sanitize_text(text: str) -> str:
    if not text:
        return text
    text = text.replace('\x00', '')
    text = re.sub(r"[\x01-\x08\x0b\x0c\x0e-\x1f\x7f]", '', text)
    text = re.sub(r"[\u200b-\u200f\u2028-\u202f\u2060\ufeff]", '', text)
    return text


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
    seen = set()
    ordered: List[str] = []
    for tag in cleaned:
        if tag in seen:
            continue
        seen.add(tag)
        ordered.append(tag)
    return ordered


@dataclass
class RequestMemberSpec:
    handle: str
    role: str = 'assignee'


@dataclass
class RequestSpec:
    title: str
    request: Optional[str] = None
    required_output: Optional[str] = None
    status: Optional[str] = None
    priority: Optional[str] = None
    due_at: Optional[datetime] = None
    tags: Optional[List[str]] = None
    members: List[RequestMemberSpec] = field(default_factory=list)
    request_id: Optional[str] = None
    confirmed: bool = True
    raw: Optional[str] = None
    fields: Set[str] = field(default_factory=set)

    def to_dict(self) -> Dict[str, Any]:
        return {
            'title': self.title,
            'request': self.request,
            'required_output': self.required_output,
            'status': self.status,
            'priority': self.priority,
            'due_at': self.due_at.isoformat() if self.due_at else None,
            'tags': self.tags or [],
            'members': [asdict(m) for m in self.members],
            'request_id': self.request_id,
            'confirmed': self.confirmed,
        }


@dataclass
class Request:
    id: str
    title: str
    request: Optional[str]
    required_output: Optional[str]
    status: str
    priority: str
    tags: List[str]
    created_by: str
    created_at: datetime
    updated_at: datetime
    due_at: Optional[datetime]
    completed_at: Optional[datetime]
    visibility: str
    origin_peer: Optional[str] = None
    source_type: Optional[str] = None
    source_id: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data['created_at'] = self.created_at.isoformat() if self.created_at else None
        data['updated_at'] = self.updated_at.isoformat() if self.updated_at else None
        data['due_at'] = self.due_at.isoformat() if self.due_at else None
        data['completed_at'] = self.completed_at.isoformat() if self.completed_at else None
        return data


def derive_request_id(source_type: str, source_id: str, index: int = 0,
                      total: int = 1, override: Optional[str] = None) -> str:
    if override:
        cleaned = override.strip()
        if cleaned:
            return cleaned if cleaned.startswith('request_') else f"request_{cleaned}"
    base = f"request_{source_type}_{source_id}"
    if total > 1:
        return f"{base}_{index + 1}"
    return base


def _parse_members(raw: str, role_default: str = 'assignee') -> List[RequestMemberSpec]:
    members: List[RequestMemberSpec] = []
    if not raw:
        return members
    tokens = [t.strip() for t in raw.split(',') if t.strip()]
    for token in tokens:
        role = role_default
        role_match = re.search(r"\(([^)]+)\)\s*$", token)
        if role_match:
            role_candidate = role_match.group(1).strip().lower()
            if role_candidate in REQUEST_ROLES:
                role = role_candidate
            token = token[:role_match.start()].strip()
        if token:
            members.append(RequestMemberSpec(handle=token, role=role))
    return members


def parse_request_blocks(text: str) -> List[RequestSpec]:
    if not text:
        return []
    if len(text) > _MAX_REQUEST_INPUT_SIZE:
        logger.warning(f"Rejecting oversized input ({len(text)} bytes) for request parsing")
        return []

    text = _sanitize_text(text)
    masked = _mask_code_fences(text)
    specs: List[RequestSpec] = []

    for pattern in _REQUEST_BLOCK_PATTERNS:
        for match in pattern.finditer(masked):
            if len(specs) >= _MAX_REQUEST_BLOCKS:
                logger.warning("Request block limit reached; ignoring extras")
                break

            block = text[match.start(1):match.end(1)] if match.group(1) else ''
            raw_block = text[match.start():match.end()]
            title = None
            request_lines: List[str] = []
            output_lines: List[str] = []
            status = None
            priority = None
            due_at = None
            tags: List[str] = []
            members: List[RequestMemberSpec] = []
            request_id = None
            confirmed = True
            fields: Set[str] = set()
            current_section = None

            for line in block.splitlines():
                stripped = line.strip()
                if not stripped:
                    if current_section == 'request':
                        request_lines.append('')
                    if current_section == 'output':
                        output_lines.append('')
                    continue

                # Detect new key
                m = re.match(r"^([A-Za-z0-9_\-]+)\s*:\s*(.*)", stripped)
                if m:
                    key = m.group(1).lower()
                    val = (m.group(2) or '').strip()

                    if key in ('title', 'subject', 'name'):
                        title = val or title
                        fields.add('title')
                        current_section = None
                        continue
                    if key in ('request', 'ask', 'description', 'details', 'body'):
                        if val:
                            request_lines.append(val)
                        fields.add('request')
                        current_section = 'request'
                        continue
                    if key in ('required_output', 'deliverable', 'deliverables', 'output', 'success'):
                        if val:
                            output_lines.append(val)
                        fields.add('required_output')
                        current_section = 'output'
                        continue
                    if key in ('status',):
                        status = val.lower() or status
                        fields.add('status')
                        current_section = None
                        continue
                    if key in ('priority', 'urgency'):
                        priority = val.lower() or priority
                        fields.add('priority')
                        current_section = None
                        continue
                    if key in ('due', 'deadline', 'by'):
                        due_at = _parse_dt(val) or _parse_relative_due(val) or due_at
                        fields.add('due_at')
                        current_section = None
                        continue
                    if key in ('assignees', 'assignee', 'members'):
                        members.extend(_parse_members(val, role_default='assignee'))
                        fields.add('members')
                        current_section = None
                        continue
                    if key in ('reviewers', 'reviewer'):
                        members.extend(_parse_members(val, role_default='reviewer'))
                        fields.add('members')
                        current_section = None
                        continue
                    if key in ('watchers', 'watcher', 'observers'):
                        members.extend(_parse_members(val, role_default='watcher'))
                        fields.add('members')
                        current_section = None
                        continue
                    if key in ('tags', 'tag'):
                        tags.extend(_split_tags(val))
                        fields.add('tags')
                        current_section = None
                        continue
                    if key in ('id', 'request_id'):
                        request_id = val or request_id
                        fields.add('request_id')
                        current_section = None
                        continue
                    if key in ('confirm', 'enabled'):
                        raw = val.lower()
                        if raw in _CONFIRM_FALSE:
                            confirmed = False
                        elif raw in _CONFIRM_TRUE:
                            confirmed = True
                        fields.add('confirm')
                        current_section = None
                        continue

                if current_section == 'request':
                    if stripped.startswith(('-', '*')):
                        request_lines.append(stripped.lstrip('-*').strip())
                    else:
                        request_lines.append(stripped)
                elif current_section == 'output':
                    if stripped.startswith(('-', '*')):
                        output_lines.append(stripped.lstrip('-*').strip())
                    else:
                        output_lines.append(stripped)

            request_text = "\n".join([line for line in request_lines if line is not None]).strip() or None
            output_text = "\n".join([line for line in output_lines if line is not None]).strip() or None

            if not title:
                if request_text:
                    title = request_text.splitlines()[0][:120]
                elif output_text:
                    title = output_text.splitlines()[0][:120]

            if not title:
                continue

            spec = RequestSpec(
                title=title,
                request=request_text,
                required_output=output_text,
                status=status,
                priority=priority,
                due_at=due_at,
                tags=tags if tags else None,
                members=members,
                request_id=request_id,
                confirmed=confirmed,
                raw=raw_block,
                fields=fields,
            )
            specs.append(spec)

    return specs


def strip_request_blocks(text: str, remove_unconfirmed: bool = False) -> str:
    """Remove confirmed request blocks from text, optionally removing unconfirmed too.

    Blocks inside triple-backtick code fences are preserved as-is.
    If remove_unconfirmed=False, unconfirmed blocks are replaced with their body
    (confirm line removed) so the content still reads well.
    """
    if not text:
        return text

    code_ranges: list = []
    for m in re.finditer(r"```.*?```", text, flags=re.S):
        code_ranges.append((m.start(), m.end()))

    def _in_code_fence(start: int, end: int) -> bool:
        for cs, ce in code_ranges:
            if start >= cs and end <= ce:
                return True
        return False

    out = text
    for pattern in _REQUEST_BLOCK_PATTERNS:
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

        out = pattern.sub(_replace, out)
    return out.strip()


class RequestManager:
    """Manages structured Requests."""

    def __init__(self, db: DatabaseManager):
        self.db = db
        logger.info("Initializing RequestManager")
        self._ensure_tables()
        logger.info("RequestManager initialized successfully")

    def _ensure_tables(self) -> None:
        try:
            with self.db.get_connection() as conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS requests (
                        id TEXT PRIMARY KEY,
                        title TEXT NOT NULL,
                        request TEXT,
                        required_output TEXT,
                        status TEXT DEFAULT 'open',
                        priority TEXT DEFAULT 'normal',
                        tags TEXT,
                        created_by TEXT NOT NULL,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        due_at TIMESTAMP,
                        completed_at TIMESTAMP,
                        visibility TEXT DEFAULT 'network',
                        origin_peer TEXT,
                        source_type TEXT,
                        source_id TEXT,
                        metadata TEXT
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS request_members (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        request_id TEXT NOT NULL,
                        user_id TEXT NOT NULL,
                        role TEXT DEFAULT 'assignee',
                        added_by TEXT,
                        added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE(request_id, user_id, role),
                        FOREIGN KEY (request_id) REFERENCES requests(id) ON DELETE CASCADE
                    )
                    """
                )
                conn.execute("CREATE INDEX IF NOT EXISTS idx_requests_status ON requests(status)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_requests_priority ON requests(priority)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_requests_due ON requests(due_at)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_request_members_req ON request_members(request_id)")
                conn.commit()
        except Exception as e:
            logger.error(f"Failed to ensure request tables: {e}", exc_info=True)
            raise

    def _normalize_status(self, status: Optional[str]) -> str:
        value = (status or 'open').strip().lower()
        return value if value in REQUEST_STATUSES else 'open'

    def _normalize_priority(self, priority: Optional[str]) -> str:
        value = (priority or 'normal').strip().lower()
        return value if value in REQUEST_PRIORITIES else 'normal'

    def _row_to_request(self, row: Any) -> Dict[str, Any]:
        data = dict(row)
        data['tags'] = [t for t in (data.get('tags') or '').split(',') if t]
        try:
            data['metadata'] = json.loads(data['metadata']) if data.get('metadata') else None
        except Exception:
            data['metadata'] = None
        return data

    def _can_update(self, request: Dict[str, Any], actor_id: Optional[str],
                    admin_user_id: Optional[str] = None, assignees: Optional[List[str]] = None) -> bool:
        if not actor_id:
            return False
        if admin_user_id and actor_id == admin_user_id:
            return True
        if actor_id == request.get('created_by'):
            return True
        if assignees and actor_id in assignees:
            return True
        return False

    def list_members(self, request_id: str) -> List[Dict[str, Any]]:
        if not request_id:
            return []
        try:
            with self.db.get_connection() as conn:
                rows = conn.execute(
                    "SELECT user_id, role FROM request_members WHERE request_id = ?",
                    (request_id,)
                ).fetchall()
            return [{'user_id': r['user_id'], 'role': r['role']} for r in rows]
        except Exception as e:
            logger.error(f"Failed to list request members: {e}")
            return []

    def set_members(self, request_id: str, members: List[Dict[str, Any]],
                    added_by: Optional[str] = None) -> None:
        if not request_id:
            return
        try:
            with self.db.get_connection() as conn:
                conn.execute("DELETE FROM request_members WHERE request_id = ?", (request_id,))
                for member in members or []:
                    uid = member.get('user_id')
                    if not uid:
                        continue
                    role = (member.get('role') or 'assignee').strip().lower()
                    if role not in REQUEST_ROLES:
                        role = 'assignee'
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO request_members (request_id, user_id, role, added_by)
                        VALUES (?, ?, ?, ?)
                        """,
                        (request_id, uid, role, added_by)
                    )
                conn.commit()
        except Exception as e:
            logger.error(f"Failed to set request members: {e}")

    def upsert_request(self,
                       request_id: str,
                       title: str,
                       created_by: str,
                       request_text: Optional[str] = None,
                       required_output: Optional[str] = None,
                       status: Optional[str] = None,
                       priority: Optional[str] = None,
                       tags: Optional[List[str]] = None,
                       due_at: Optional[Any] = None,
                       visibility: Optional[str] = None,
                       origin_peer: Optional[str] = None,
                       source_type: Optional[str] = None,
                       source_id: Optional[str] = None,
                       metadata: Optional[Dict[str, Any]] = None,
                       created_at: Optional[Any] = None,
                       updated_at: Optional[Any] = None,
                       members: Optional[List[Dict[str, Any]]] = None,
                       members_defined: bool = False,
                       actor_id: Optional[str] = None,
                       fields: Optional[Set[str]] = None) -> Optional[Dict[str, Any]]:
        if not request_id or not title:
            return None
        status_val = self._normalize_status(status) if status is not None else None
        priority_val = self._normalize_priority(priority) if priority is not None else None
        tags_csv = ",".join(tags or []) if tags is not None else None
        created_dt = _parse_dt(created_at) or _now_utc()
        updated_dt = _parse_dt(updated_at) or _now_utc()
        due_dt = None
        if isinstance(due_at, datetime):
            due_dt = due_at if due_at.tzinfo else due_at.replace(tzinfo=timezone.utc)
        else:
            due_dt = _parse_dt(due_at) or _parse_relative_due(str(due_at)) if due_at else None
        meta_json = json.dumps(metadata) if metadata is not None else None
        fields = fields or set()

        try:
            with self.db.get_connection() as conn:
                row = conn.execute("SELECT * FROM requests WHERE id = ?", (request_id,)).fetchone()
                if row:
                    existing = self._row_to_request(row)
                    updates = []
                    values: List[Any] = []

                    if title and ('title' in fields or not fields):
                        updates.append("title = ?")
                        values.append(title)
                    if request_text is not None and ('request' in fields or not fields):
                        updates.append("request = ?")
                        values.append(request_text)
                    if required_output is not None and ('required_output' in fields or not fields):
                        updates.append("required_output = ?")
                        values.append(required_output)
                    if status_val is not None and ('status' in fields or not fields):
                        updates.append("status = ?")
                        values.append(status_val)
                    if priority_val is not None and ('priority' in fields or not fields):
                        updates.append("priority = ?")
                        values.append(priority_val)
                    if tags_csv is not None and ('tags' in fields or not fields):
                        updates.append("tags = ?")
                        values.append(tags_csv)
                    if due_dt is not None and ('due_at' in fields or not fields):
                        updates.append("due_at = ?")
                        values.append(due_dt.isoformat() if due_dt else None)
                    if metadata is not None and ('metadata' in fields or not fields):
                        updates.append("metadata = ?")
                        values.append(meta_json)

                    updates.append("updated_at = ?")
                    values.append(updated_dt.isoformat())
                    values.append(request_id)

                    if updates:
                        conn.execute(f"UPDATE requests SET {', '.join(updates)} WHERE id = ?", values)
                    if members_defined and members is not None:
                        self.set_members(request_id, members, added_by=actor_id or created_by)
                else:
                    conn.execute(
                        """
                        INSERT INTO requests
                        (id, title, request, required_output, status, priority, tags,
                         created_by, created_at, updated_at, due_at, completed_at,
                         visibility, origin_peer, source_type, source_id, metadata)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            request_id,
                            title,
                            request_text,
                            required_output,
                            status_val or 'open',
                            priority_val or 'normal',
                            tags_csv or '',
                            created_by,
                            created_dt.isoformat(),
                            updated_dt.isoformat(),
                            due_dt.isoformat() if due_dt else None,
                            None,
                            visibility or 'network',
                            origin_peer,
                            source_type,
                            source_id,
                            meta_json,
                        )
                    )
                    if members:
                        self.set_members(request_id, members, added_by=actor_id or created_by)
                conn.commit()
            return self.get_request(request_id, include_members=True)
        except Exception as e:
            logger.error(f"Failed to upsert request {request_id}: {e}", exc_info=True)
            return None

    def get_request(self, request_id: str, include_members: bool = False) -> Optional[Dict[str, Any]]:
        if not request_id:
            return None
        try:
            with self.db.get_connection() as conn:
                row = conn.execute("SELECT * FROM requests WHERE id = ?", (request_id,)).fetchone()
            if not row:
                return None
            data = self._row_to_request(row)
            if include_members:
                data['members'] = self.list_members(request_id)
            return data
        except Exception as e:
            logger.error(f"Failed to get request {request_id}: {e}")
            return None

    def list_requests(self, limit: int = 50, status: Optional[str] = None,
                      priority: Optional[str] = None, tag: Optional[str] = None,
                      include_members: bool = False) -> List[Dict[str, Any]]:
        try:
            limit_val = max(1, min(int(limit or 50), 200))
            clauses = []
            params: List[Any] = []
            if status:
                clauses.append("status = ?")
                params.append(self._normalize_status(status))
            if priority:
                clauses.append("priority = ?")
                params.append(self._normalize_priority(priority))
            if tag:
                escaped_tag = tag.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                clauses.append("tags LIKE ? ESCAPE '\\'")
                params.append(f"%{escaped_tag}%")
            query = "SELECT * FROM requests"
            if clauses:
                query += " WHERE " + " AND ".join(clauses)
            query += " ORDER BY updated_at DESC LIMIT ?"
            params.append(limit_val)

            results: List[Dict[str, Any]] = []
            with self.db.get_connection() as conn:
                rows = conn.execute(query, params).fetchall()
                for row in rows:
                    item = self._row_to_request(row)
                    if include_members:
                        item['members'] = self.list_members(cast(str, item.get('id')))
                    results.append(item)
            return results
        except Exception as e:
            logger.error(f"Failed to list requests: {e}")
            return []

    def update_request(self, request_id: str, updates: Dict[str, Any],
                       actor_id: Optional[str] = None,
                       admin_user_id: Optional[str] = None,
                       members: Optional[List[Dict[str, Any]]] = None,
                       replace_members: bool = False) -> Optional[Dict[str, Any]]:
        if not request_id:
            return None
        try:
            with self.db.get_connection() as conn:
                row = conn.execute("SELECT * FROM requests WHERE id = ?", (request_id,)).fetchone()
                if not row:
                    return None
                existing = self._row_to_request(row)
                member_ids = [m['user_id'] for m in self.list_members(request_id)]
                if not self._can_update(existing, actor_id, admin_user_id=admin_user_id, assignees=member_ids):
                    raise PermissionError("Not authorized to update request")

                fields = []
                values: List[Any] = []
                if 'title' in updates:
                    fields.append("title = ?")
                    values.append(updates.get('title'))
                if 'request' in updates:
                    fields.append("request = ?")
                    values.append(updates.get('request'))
                if 'required_output' in updates:
                    fields.append("required_output = ?")
                    values.append(updates.get('required_output'))
                if 'status' in updates:
                    fields.append("status = ?")
                    values.append(self._normalize_status(updates.get('status')))
                    if updates.get('status') in ('completed', 'closed'):
                        fields.append("completed_at = ?")
                        values.append(_now_utc().isoformat())
                if 'priority' in updates:
                    fields.append("priority = ?")
                    values.append(self._normalize_priority(updates.get('priority')))
                if 'tags' in updates:
                    tags_val = updates.get('tags') or []
                    if isinstance(tags_val, str):
                        tags_val = _split_tags(tags_val)
                    fields.append("tags = ?")
                    values.append(",".join(tags_val))
                if 'due_at' in updates:
                    due_dt = _parse_dt(updates.get('due_at')) or _parse_relative_due(str(updates.get('due_at')))
                    fields.append("due_at = ?")
                    values.append(due_dt.isoformat() if due_dt else None)
                if 'metadata' in updates:
                    fields.append("metadata = ?")
                    values.append(json.dumps(updates.get('metadata')) if updates.get('metadata') is not None else None)

                if fields:
                    fields.append("updated_at = ?")
                    values.append(_now_utc().isoformat())
                    values.append(request_id)
                    conn.execute(f"UPDATE requests SET {', '.join(fields)} WHERE id = ?", values)
                conn.commit()

            if replace_members and members is not None:
                self.set_members(request_id, members, added_by=actor_id)

            return self.get_request(request_id, include_members=True)
        except PermissionError:
            raise
        except Exception as e:
            logger.error(f"Failed to update request {request_id}: {e}")
            return None
