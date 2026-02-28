"""
Task management for Canopy.

Provides lightweight collaborative tasks with status, priority, assignee,
and P2P synchronization metadata.

Author: Konrad Walus (architecture, design, and direction)
Project: Canopy - Local Mesh Communication
License: Apache 2.0
Development: AI-assisted implementation (Claude, Codex, GitHub Copilot, Cursor IDE, Ollama)
"""

from __future__ import annotations

import json
import logging
import secrets
import re
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Any

from .database import DatabaseManager

logger = logging.getLogger('canopy.tasks')

TASK_STATUSES = ('open', 'in_progress', 'blocked', 'done')
TASK_PRIORITIES = ('low', 'normal', 'high', 'critical')
TASK_VISIBILITY = ('network', 'local')

_TASK_BLOCK_PATTERNS = [
    re.compile(r"(?is)\[task\](.*?)\[/task\]"),
    re.compile(r"(?is)::task\s*(.*?)\s*::endtask"),
]

_CONFIRM_FALSE = {'false', 'no', 'off', '0'}
_CONFIRM_TRUE = {'true', 'yes', 'on', '1'}
_CLEAR_TOKENS = {'none', 'null', 'clear', 'unset', 'unassigned', '-', 'n/a'}


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


@dataclass
class Task:
    id: str
    title: str
    description: Optional[str]
    status: str
    priority: str
    created_by: str
    assigned_to: Optional[str]
    objective_id: Optional[str]
    created_at: datetime
    updated_at: datetime
    updated_by: Optional[str] = None
    due_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    visibility: str = "network"
    metadata: Optional[Dict[str, Any]] = None
    origin_peer: Optional[str] = None
    source_type: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data['created_at'] = self.created_at.isoformat() if self.created_at else None
        data['updated_at'] = self.updated_at.isoformat() if self.updated_at else None
        data['due_at'] = self.due_at.isoformat() if self.due_at else None
        data['completed_at'] = self.completed_at.isoformat() if self.completed_at else None
        return data


@dataclass
class TaskSpec:
    title: str
    description: Optional[str] = None
    status: Optional[str] = None
    priority: Optional[str] = None
    assignee: Optional[str] = None
    editors: Optional[List[str]] = None
    task_id: Optional[str] = None
    due_at: Optional[datetime] = None
    assignee_clear: bool = False
    editors_clear: bool = False
    due_clear: bool = False
    confirmed: bool = True
    start: Optional[int] = None
    end: Optional[int] = None
    raw: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            'title': self.title,
            'description': self.description,
            'status': self.status,
            'priority': self.priority,
            'assignee': self.assignee,
            'editors': self.editors or [],
            'task_id': self.task_id,
            'due_at': self.due_at.isoformat() if self.due_at else None,
            'confirmed': self.confirmed,
        }


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
        now = _now_utc()
        return now + timedelta(seconds=seconds)
    except Exception:
        return None


_CODE_FENCE_RE = re.compile(r"```[\s\S]*?```")


def _mask_code_fences(text: str) -> tuple[str, str]:
    """Replace code-fenced regions with whitespace placeholders so parsers
    skip content inside triple-backtick blocks.  Returns (masked_text, original_text)."""
    if '```' not in text:
        return text, text
    masked = text
    for m in reversed(list(_CODE_FENCE_RE.finditer(text))):
        masked = masked[:m.start()] + (' ' * (m.end() - m.start())) + masked[m.end():]
    return masked, text


def parse_task_blocks(text: str) -> List[TaskSpec]:
    """Extract task specs from a text blob.  Blocks inside triple-backtick
    code fences are ignored."""
    if not text:
        return []

    # Mask code fences so [task] blocks inside them are not matched
    masked, original = _mask_code_fences(text)

    specs: List[TaskSpec] = []
    for pattern in _TASK_BLOCK_PATTERNS:
        for match in pattern.finditer(masked):
            # Re-extract the actual block content from the original text
            block = original[match.start():match.end()]
            # Parse the inner content (between tags)
            inner_match = pattern.search(block)
            block = inner_match.group(1) if inner_match else ''
            start = match.start()
            end = match.end()
            title = None
            description_lines: List[str] = []
            status = None
            priority = None
            assignee = None
            editors: List[str] = []
            task_id = None
            due_at = None
            assignee_clear = False
            editors_clear = False
            due_clear = False
            confirmed = True

            for line in block.splitlines():
                stripped = line.strip()
                if not stripped:
                    continue
                lower = stripped.lower()

                if lower.startswith(('title:', 'task:')):
                    title = stripped.split(':', 1)[1].strip() or title
                    continue
                if lower.startswith(('description:', 'details:', 'notes:')):
                    desc = stripped.split(':', 1)[1].strip()
                    if desc:
                        description_lines.append(desc)
                    continue
                if lower.startswith(('status:',)):
                    status = stripped.split(':', 1)[1].strip().lower()
                    continue
                if lower.startswith(('priority:',)):
                    priority = stripped.split(':', 1)[1].strip().lower()
                    continue
                if lower.startswith(('id:', 'task_id:')):
                    task_id = stripped.split(':', 1)[1].strip()
                    continue
                if lower.startswith(('assignee:', 'assigned:', 'owner:')):
                    raw_val = stripped.split(':', 1)[1].strip()
                    if not raw_val or raw_val.lower() in _CLEAR_TOKENS:
                        assignee = ''
                        assignee_clear = True
                    else:
                        assignee = raw_val
                    continue
                if lower.startswith(('editors:', 'collaborators:')):
                    raw_editors = stripped.split(':', 1)[1].strip()
                    if not raw_editors or raw_editors.lower() in _CLEAR_TOKENS:
                        editors_clear = True
                        editors = []
                    else:
                        parts = re.split(r"[,\s]+", raw_editors)
                        for part in parts:
                            token = part.strip()
                            if not token:
                                continue
                            if token.startswith('@'):
                                token = token[1:]
                            if token:
                                editors.append(token)
                    continue
                if lower.startswith(('due:', 'due_at:', 'deadline:')):
                    due_raw = stripped.split(':', 1)[1].strip()
                    if not due_raw or due_raw.lower() in _CLEAR_TOKENS:
                        due_at = None
                        due_clear = True
                    else:
                        due_at = _parse_dt(due_raw) or _parse_relative_due(due_raw)
                    continue
                if lower.startswith(('confirm:', 'confirmed:')):
                    val = stripped.split(':', 1)[1].strip().lower()
                    if val in _CONFIRM_FALSE:
                        confirmed = False
                    elif val in _CONFIRM_TRUE:
                        confirmed = True
                    continue

                if title is None:
                    title = stripped
                else:
                    description_lines.append(stripped)

            if not title:
                continue

            spec = TaskSpec(
                title=title,
                description="\n".join(description_lines).strip() or None,
                status=status,
                priority=priority,
                assignee=assignee,
                editors=editors or None,
                task_id=task_id or None,
                due_at=due_at,
                assignee_clear=assignee_clear,
                editors_clear=editors_clear,
                due_clear=due_clear,
                confirmed=confirmed,
                start=start,
                end=end,
                raw=match.group(0),
            )
            specs.append(spec)

    return specs


def strip_task_blocks(text: str, remove_unconfirmed: bool = False) -> str:
    """Remove confirmed task blocks from text, optionally removing unconfirmed too.

    Blocks inside triple-backtick code fences are preserved as-is.
    If remove_unconfirmed=False, unconfirmed blocks are replaced with their body
    (confirm line removed) so the content still reads well.
    """
    if not text:
        return text

    # Identify code-fenced regions to protect them
    code_ranges: list = []
    for m in _CODE_FENCE_RE.finditer(text):
        code_ranges.append((m.start(), m.end()))

    def _in_code_fence(start: int, end: int) -> bool:
        for cs, ce in code_ranges:
            if start >= cs and end <= ce:
                return True
        return False

    pattern = re.compile(r"(?is)\[task\](.*?)\[/task\]")

    def _replace(match):
        if _in_code_fence(match.start(), match.end()):
            return match.group(0)  # preserve inside code fences
        body = match.group(1) or ''
        confirm_match = re.search(r"(?im)^\s*confirm\s*:\s*(.+)$", body)
        confirmed = True
        if confirm_match:
            val = confirm_match.group(1).strip().lower()
            if val in _CONFIRM_FALSE:
                confirmed = False
        if confirmed or remove_unconfirmed:
            return ''
        # Remove confirm line for clean display
        cleaned_body = re.sub(r"(?im)^\s*confirm\s*:.*$", "", body).strip()
        return cleaned_body

    cleaned_text = pattern.sub(_replace, text)
    return cleaned_text.strip()


def derive_task_id(source_type: str, source_id: str, index: int = 0, total: int = 1,
                   override: Optional[str] = None) -> str:
    if override:
        cleaned = override.strip()
        if cleaned:
            return cleaned if cleaned.startswith('task_') else f"task_{cleaned}"
    base = f"task_{source_type}_{source_id}"
    if total > 1:
        return f"{base}_{index + 1}"
    return base


class TaskManager:
    """Manages collaborative tasks."""

    def __init__(self, db: DatabaseManager):
        self.db = db
        logger.info("Initializing TaskManager")
        self._ensure_tables()
        logger.info("TaskManager initialized successfully")

    def _ensure_tables(self) -> None:
        """Ensure task tables exist and evolve schema safely."""
        try:
            with self.db.get_connection() as conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS tasks (
                        id TEXT PRIMARY KEY,
                        title TEXT NOT NULL,
                        description TEXT,
                        status TEXT DEFAULT 'open',
                        priority TEXT DEFAULT 'normal',
                        created_by TEXT NOT NULL,
                        assigned_to TEXT,
                        objective_id TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_by TEXT,
                        due_at TIMESTAMP,
                        completed_at TIMESTAMP,
                        visibility TEXT DEFAULT 'network',
                        metadata TEXT,
                        origin_peer TEXT,
                        source_type TEXT
                    )
                """)
                # Non-destructive migrations for older DBs
                columns = [
                    ("description", "TEXT"),
                    ("status", "TEXT DEFAULT 'open'"),
                    ("priority", "TEXT DEFAULT 'normal'"),
                    ("assigned_to", "TEXT"),
                    ("objective_id", "TEXT"),
                    ("updated_at", "TIMESTAMP DEFAULT CURRENT_TIMESTAMP"),
                    ("updated_by", "TEXT"),
                    ("due_at", "TIMESTAMP"),
                    ("completed_at", "TIMESTAMP"),
                    ("visibility", "TEXT DEFAULT 'network'"),
                    ("metadata", "TEXT"),
                    ("origin_peer", "TEXT"),
                    ("source_type", "TEXT"),
                ]
                for col, col_def in columns:
                    try:
                        conn.execute(f"ALTER TABLE tasks ADD COLUMN {col} {col_def}")
                    except Exception:
                        pass
                conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_assigned_to ON tasks(assigned_to)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_updated_at ON tasks(updated_at)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_objective ON tasks(objective_id)")
                conn.commit()
        except Exception as e:
            logger.error(f"Failed to ensure task tables: {e}", exc_info=True)
            raise

    def _normalize_status(self, status: Optional[str]) -> str:
        value = (status or "open").strip().lower()
        return value if value in TASK_STATUSES else "open"

    def _normalize_priority(self, priority: Optional[str]) -> str:
        value = (priority or "normal").strip().lower()
        return value if value in TASK_PRIORITIES else "normal"

    def _normalize_visibility(self, visibility: Optional[str]) -> str:
        value = (visibility or "network").strip().lower()
        return value if value in TASK_VISIBILITY else "network"

    def _editor_set(self, task: Task) -> set:
        editors = set()
        meta = task.metadata or {}
        raw = meta.get('editors')
        if isinstance(raw, list):
            for item in raw:
                if item:
                    editors.add(str(item))
        return editors

    def _is_authorized(self, task: Task, actor_id: Optional[str],
                       admin_user_id: Optional[str] = None) -> bool:
        if not actor_id:
            return True
        if admin_user_id and actor_id == admin_user_id:
            return True
        if actor_id == task.created_by:
            return True
        if task.assigned_to and actor_id == task.assigned_to:
            return True
        if actor_id in self._editor_set(task):
            return True
        return False

    def _row_to_task(self, row: Any) -> Task:
        metadata = None
        if row['metadata']:
            try:
                metadata = json.loads(row['metadata'])
            except Exception:
                metadata = None
        objective_id = None
        try:
            objective_id = row['objective_id']
        except Exception:
            objective_id = None
        return Task(
            id=row['id'],
            title=row['title'],
            description=row['description'],
            status=row['status'],
            priority=row['priority'],
            created_by=row['created_by'],
            assigned_to=row['assigned_to'],
            objective_id=objective_id,
            created_at=_parse_dt(row['created_at']) or _now_utc(),
            updated_at=_parse_dt(row['updated_at']) or _now_utc(),
            updated_by=row['updated_by'],
            due_at=_parse_dt(row['due_at']),
            completed_at=_parse_dt(row['completed_at']),
            visibility=row['visibility'] or 'network',
            metadata=metadata,
            origin_peer=row['origin_peer'],
            source_type=row['source_type'],
        )

    def create_task(
        self,
        title: str,
        created_by: str,
        description: Optional[str] = None,
        status: Optional[str] = None,
        priority: Optional[str] = None,
        assigned_to: Optional[str] = None,
        objective_id: Optional[str] = None,
        due_at: Optional[Any] = None,
        visibility: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        origin_peer: Optional[str] = None,
        source_type: Optional[str] = None,
        task_id: Optional[str] = None,
        created_at: Optional[Any] = None,
        updated_at: Optional[Any] = None,
        updated_by: Optional[str] = None,
        completed_at: Optional[Any] = None,
    ) -> Optional[Task]:
        if not title:
            return None
        task_id = task_id or f"task_{secrets.token_hex(8)}"
        created_at_dt = _parse_dt(created_at) or _now_utc()
        updated_at_dt = _parse_dt(updated_at) or created_at_dt
        status_val = self._normalize_status(status)
        priority_val = self._normalize_priority(priority)
        visibility_val = self._normalize_visibility(visibility)
        due_at_dt = _parse_dt(due_at)
        completed_at_dt = _parse_dt(completed_at)
        if status_val == 'done' and not completed_at_dt:
            completed_at_dt = updated_at_dt
        meta_json = json.dumps(metadata) if metadata is not None else None

        try:
            with self.db.get_connection() as conn:
                conn.execute("""
                    INSERT OR IGNORE INTO tasks
                    (id, title, description, status, priority, created_by, assigned_to, objective_id,
                     created_at, updated_at, updated_by, due_at, completed_at, visibility,
                     metadata, origin_peer, source_type)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    task_id, title, description, status_val, priority_val,
                    created_by, assigned_to, objective_id,
                    created_at_dt.isoformat(), updated_at_dt.isoformat(), updated_by,
                    due_at_dt.isoformat() if due_at_dt else None,
                    completed_at_dt.isoformat() if completed_at_dt else None,
                    visibility_val, meta_json, origin_peer, source_type
                ))
                conn.commit()
            return self.get_task(task_id)
        except Exception as e:
            logger.error(f"Failed to create task: {e}", exc_info=True)
            return None

    def get_task(self, task_id: str) -> Optional[Task]:
        if not task_id:
            return None
        try:
            with self.db.get_connection() as conn:
                row = conn.execute(
                    "SELECT * FROM tasks WHERE id = ?",
                    (task_id,)
                ).fetchone()
            if not row:
                return None
            return self._row_to_task(row)
        except Exception as e:
            logger.error(f"Failed to get task {task_id}: {e}")
            return None

    def get_tasks_since(self, since_timestamp: str, limit: int = 500) -> List[Dict[str, Any]]:
        """Return task snapshots updated after *since_timestamp*.

        Used by the P2P catch-up mechanism to send missed task updates
        to a reconnecting peer.
        """
        try:
            with self.db.get_connection() as conn:
                rows = conn.execute(
                    "SELECT * FROM tasks WHERE updated_at > ? "
                    "ORDER BY updated_at ASC LIMIT ?",
                    (since_timestamp, limit)
                ).fetchall()
            results = []
            for row in rows:
                task = self._row_to_task(row)
                results.append(task.to_dict())
            return results
        except Exception as e:
            logger.error(f"Failed to get tasks since {since_timestamp}: {e}")
            return []

    def get_tasks_latest_timestamp(self) -> Optional[str]:
        """Return the updated_at of the most recently modified task, or None."""
        try:
            with self.db.get_connection() as conn:
                row = conn.execute(
                    "SELECT MAX(updated_at) AS latest FROM tasks"
                ).fetchone()
            return row['latest'] if row and row['latest'] else None
        except Exception:
            return None

    def list_tasks(self, limit: int = 200, status: Optional[str] = None) -> List[Task]:
        try:
            query = "SELECT * FROM tasks"
            params: List[Any] = []
            if status:
                query += " WHERE status = ?"
                params.append(self._normalize_status(status))
            query += " ORDER BY updated_at DESC LIMIT ?"
            params.append(limit)
            with self.db.get_connection() as conn:
                rows = conn.execute(query, params).fetchall()
            return [self._row_to_task(r) for r in rows]
        except Exception as e:
            logger.error(f"Failed to list tasks: {e}")
            return []

    def list_tasks_for_objective(self, objective_id: str, limit: int = 200) -> List[Task]:
        if not objective_id:
            return []
        try:
            limit_val = max(1, min(int(limit or 200), 500))
            with self.db.get_connection() as conn:
                rows = conn.execute(
                    "SELECT * FROM tasks WHERE objective_id = ? ORDER BY created_at ASC LIMIT ?",
                    (objective_id, limit_val)
                ).fetchall()
            return [self._row_to_task(r) for r in rows]
        except Exception as e:
            logger.error(f"Failed to list tasks for objective {objective_id}: {e}")
            return []

    def update_task(self, task_id: str, updates: Dict[str, Any], actor_id: Optional[str] = None,
                    updated_at: Optional[Any] = None,
                    admin_user_id: Optional[str] = None) -> Optional[Task]:
        if not task_id:
            return None
        if not updates:
            return self.get_task(task_id)

        existing = self.get_task(task_id)
        if existing and actor_id and not self._is_authorized(existing, actor_id, admin_user_id=admin_user_id):
            raise PermissionError("Not authorized to update task")

        allowed = {
            'title', 'description', 'status', 'priority', 'assigned_to', 'due_at',
            'visibility', 'metadata', 'origin_peer', 'source_type', 'objective_id'
        }

        fields = []
        values: List[Any] = []
        status_val = None
        now_dt = _parse_dt(updated_at) or _now_utc()

        for key, value in updates.items():
            if key not in allowed:
                continue
            if key == 'status':
                status_val = self._normalize_status(value)
                fields.append("status = ?")
                values.append(status_val)
                continue
            if key == 'priority':
                fields.append("priority = ?")
                values.append(self._normalize_priority(value))
                continue
            if key == 'visibility':
                fields.append("visibility = ?")
                values.append(self._normalize_visibility(value))
                continue
            if key == 'metadata' and value is not None:
                fields.append("metadata = ?")
                values.append(json.dumps(value))
                continue
            if key == 'due_at':
                due_dt = _parse_dt(value)
                fields.append("due_at = ?")
                values.append(due_dt.isoformat() if due_dt else None)
                continue
            fields.append(f"{key} = ?")
            values.append(value)

        if not fields:
            return self.get_task(task_id)

        # Completion timestamp adjustments
        if status_val == 'done':
            fields.append("completed_at = ?")
            values.append(now_dt.isoformat())
        elif status_val:
            fields.append("completed_at = ?")
            values.append(None)

        fields.append("updated_at = ?")
        values.append(now_dt.isoformat())
        fields.append("updated_by = ?")
        values.append(actor_id)

        values.append(task_id)

        try:
            with self.db.get_connection() as conn:
                conn.execute(f"""
                    UPDATE tasks
                    SET {', '.join(fields)}
                    WHERE id = ?
                """, values)
                conn.commit()
            return self.get_task(task_id)
        except Exception as e:
            logger.error(f"Failed to update task {task_id}: {e}", exc_info=True)
            return None

    def apply_task_snapshot(self, data: Dict[str, Any]) -> Optional[Task]:
        """Upsert a task from a full snapshot (typically from P2P)."""
        if not data:
            return None
        task_id = data.get('id')
        title = data.get('title')
        if not task_id or not title:
            return None

        incoming_updated_at = _parse_dt(data.get('updated_at'))
        existing = self.get_task(task_id)
        if existing and incoming_updated_at and existing.updated_at and incoming_updated_at <= existing.updated_at:
            return existing

        if not existing:
            return self.create_task(
                task_id=task_id,
                title=title,
                description=data.get('description'),
                status=data.get('status'),
                priority=data.get('priority'),
                created_by=data.get('created_by') or data.get('author_id') or 'system',
                assigned_to=data.get('assigned_to'),
                objective_id=data.get('objective_id'),
                created_at=data.get('created_at'),
                updated_at=data.get('updated_at'),
                updated_by=data.get('updated_by'),
                due_at=data.get('due_at'),
                completed_at=data.get('completed_at'),
                visibility=data.get('visibility'),
                metadata=data.get('metadata') if isinstance(data.get('metadata'), dict) else None,
                origin_peer=data.get('origin_peer'),
                source_type=data.get('source_type'),
            )

        # Merge updates onto existing task
        merged = {
            'title': data.get('title', existing.title),
            'description': data.get('description', existing.description),
            'status': data.get('status', existing.status),
            'priority': data.get('priority', existing.priority),
            'assigned_to': data.get('assigned_to', existing.assigned_to),
            'objective_id': data.get('objective_id', existing.objective_id),
            'due_at': data.get('due_at', existing.due_at.isoformat() if existing.due_at else None),
            'visibility': data.get('visibility', existing.visibility),
            'metadata': data.get('metadata', existing.metadata),
            'origin_peer': data.get('origin_peer', existing.origin_peer),
            'source_type': data.get('source_type', existing.source_type),
        }

        try:
            return self.update_task(
                task_id,
                merged,
                actor_id=data.get('updated_by') or data.get('created_by'),
                updated_at=data.get('updated_at'),
            )
        except PermissionError:
            return existing
