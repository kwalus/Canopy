"""
Objective management for Canopy.

Objectives group tasks under a shared goal with multi-member roles,
progress tracking, and deadline metadata. Parsed from [objective] blocks
in posts and channel messages.
"""

from __future__ import annotations

import json
import logging
import re
import secrets
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from .database import DatabaseManager
from .tasks import TaskManager, TASK_STATUSES, derive_task_id

logger = logging.getLogger('canopy.objectives')

OBJECTIVE_STATUSES = ('pending', 'in_progress', 'completed', 'archived')
OBJECTIVE_ROLES = ('lead', 'contributor', 'reviewer')

_OBJECTIVE_BLOCK_PATTERNS = [
    re.compile(r"(?is)\[objective\](.*?)\[/objective\]"),
    re.compile(r"(?is)::objective\s*(.*?)\s*::endobjective"),
]

_MAX_OBJECTIVE_BLOCKS = 10
_MAX_OBJECTIVE_INPUT_SIZE = 1_000_000  # 1MB


@dataclass
class ObjectiveMemberSpec:
    handle: str
    role: str = 'contributor'


@dataclass
class ObjectiveTaskSpec:
    title: str
    status: str = 'open'
    assignee: Optional[str] = None
    raw: Optional[str] = None


@dataclass
class ObjectiveSpec:
    title: str
    description: Optional[str] = None
    deadline: Optional[datetime] = None
    status: Optional[str] = None
    members: List[ObjectiveMemberSpec] = field(default_factory=list)
    tasks: List[ObjectiveTaskSpec] = field(default_factory=list)
    objective_id: Optional[str] = None
    raw: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            'title': self.title,
            'description': self.description,
            'deadline': self.deadline.isoformat() if self.deadline else None,
            'status': self.status,
            'members': [asdict(m) for m in self.members],
            'tasks': [asdict(t) for t in self.tasks],
            'objective_id': self.objective_id,
        }


@dataclass
class Objective:
    id: str
    title: str
    description: Optional[str]
    status: str
    created_by: str
    created_at: datetime
    updated_at: datetime
    deadline: Optional[datetime]
    visibility: str
    origin_peer: Optional[str] = None
    source_type: Optional[str] = None
    source_id: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data['created_at'] = self.created_at.isoformat() if self.created_at else None
        data['updated_at'] = self.updated_at.isoformat() if self.updated_at else None
        data['deadline'] = self.deadline.isoformat() if self.deadline else None
        return data


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


def derive_objective_id(source_type: str, source_id: str,
                         index: int = 0, total: int = 1,
                         override: Optional[str] = None) -> str:
    if override:
        cleaned = override.strip()
        if cleaned:
            return cleaned if cleaned.startswith('objective_') else f"objective_{cleaned}"
    base = f"objective_{source_type}_{source_id}"
    if total > 1:
        return f"{base}_{index + 1}"
    return base


def _mask_code_fences(text: str) -> str:
    return re.sub(r"```.*?```", lambda m: "\x00" * len(m.group(0)), text, flags=re.S)


def _sanitize_text(text: str) -> str:
    if not text:
        return text
    text = text.replace('\x00', '')
    text = re.sub(r"[\x01-\x08\x0b\x0c\x0e-\x1f\x7f]", '', text)
    text = re.sub(r"[\u200b-\u200f\u2028-\u202f\u2060\ufeff]", '', text)
    return text


def _parse_members(value: str) -> List[ObjectiveMemberSpec]:
    members: List[ObjectiveMemberSpec] = []
    if not value:
        return members
    raw_tokens = [t.strip() for t in value.split(',') if t.strip()]
    for token in raw_tokens:
        role = 'contributor'
        role_match = re.search(r"\(([^)]+)\)\s*$", token)
        if role_match:
            role_candidate = role_match.group(1).strip().lower()
            if role_candidate in OBJECTIVE_ROLES:
                role = role_candidate
            token = token[:role_match.start()].strip()
        if token:
            members.append(ObjectiveMemberSpec(handle=token, role=role))
    return members


def _parse_task_line(line: str) -> Optional[ObjectiveTaskSpec]:
    """Parse a task line in several natural formats:

    Checkbox formats (original):
      - [ ] Task text @assignee
      - [x] Completed task @assignee (done)

    Bracket-assignee format (agents naturally use this):
      - [AgentName] Task description
      - [LeadAgent] Identify competitors

    Plain list items:
      - Task description
      - Task description @assignee
      * Task description
    """
    # Must start with - or *
    m_list = re.match(r"^[\-*]\s+(.+)$", line)
    if not m_list:
        return None
    rest = m_list.group(1).strip()
    if not rest:
        return None

    status = 'open'
    assignee = None
    body = rest

    # Pattern 1: checkbox [ ] or [x] or [X]
    m_check = re.match(r"^\[(?P<check>[ xX])\]\s*(?P<body>.+)$", rest)
    if m_check:
        body = m_check.group('body').strip()
        status = 'done' if m_check.group('check').lower() == 'x' else 'open'
    else:
        # Pattern 2: bracket-assignee [Name] body  (where Name is NOT a space/x/X alone)
        m_bracket = re.match(r"^\[(?P<name>[A-Za-z0-9_.\-]+(?:\s+[A-Za-z0-9_.\-]+)*)\]\s*(?P<body>.+)$", rest)
        if m_bracket:
            bracket_name = m_bracket.group('name').strip()
            body = m_bracket.group('body').strip()
            # Treat the bracket content as the assignee handle
            assignee = bracket_name
        # else: Pattern 3 — plain list item, body is already set

    # Extract @mention assignee (overrides bracket-assignee if both present)
    mention = re.search(r"@([A-Za-z0-9_.\-]+)", body)
    if mention:
        if not assignee:
            assignee = mention.group(1)
        body = re.sub(r"\s*@([A-Za-z0-9_.\-]+)\s*", ' ', body).strip()

    # Remove trailing done/completed markers
    done_match = re.search(r"\((done|completed|complete)\)\s*$", body, flags=re.I)
    if done_match:
        status = 'done'
        body = body[:done_match.start()].strip()

    if not body:
        return None
    return ObjectiveTaskSpec(title=body, status=status, assignee=assignee, raw=line)


def parse_objective_blocks(text: str) -> List[ObjectiveSpec]:
    if not text:
        return []
    if len(text) > _MAX_OBJECTIVE_INPUT_SIZE:
        logger.warning(f"Rejecting oversized input ({len(text)} bytes) for objective parsing")
        return []

    text = _sanitize_text(text)
    masked = _mask_code_fences(text)

    specs: List[ObjectiveSpec] = []
    for pattern in _OBJECTIVE_BLOCK_PATTERNS:
        for match in pattern.finditer(masked):
            if len(specs) >= _MAX_OBJECTIVE_BLOCKS:
                logger.warning("Objective block limit reached; ignoring extras")
                break

            block = text[match.start(1):match.end(1)] if match.group(1) else ''
            raw_block = text[match.start():match.end()]

            title = None
            description_lines: List[str] = []
            deadline = None
            status = None
            members: List[ObjectiveMemberSpec] = []
            tasks: List[ObjectiveTaskSpec] = []
            objective_id = None

            in_tasks = False
            # Also support bare "tasks:" field without [tasks] wrapper
            after_tasks_field = False
            for line in block.splitlines():
                stripped = line.strip()
                if not stripped:
                    continue
                lower = stripped.lower()

                if lower.startswith('[tasks]'):
                    in_tasks = True
                    after_tasks_field = False
                    continue
                if lower.startswith('[/tasks]'):
                    in_tasks = False
                    continue

                if in_tasks or after_tasks_field:
                    task_spec = _parse_task_line(stripped)
                    if task_spec:
                        tasks.append(task_spec)
                        continue
                    # If we're in after_tasks_field mode (no [tasks] wrapper)
                    # and the line doesn't parse as a task, we've left the
                    # task section — fall through to normal field parsing
                    if after_tasks_field:
                        after_tasks_field = False
                    else:
                        continue

                if lower.startswith(('title:', 'objective:', 'name:')):
                    title = stripped.split(':', 1)[1].strip() or title
                    after_tasks_field = False
                    continue
                if lower.startswith(('description:', 'desc:', 'details:')):
                    desc = stripped.split(':', 1)[1].strip()
                    if desc:
                        description_lines.append(desc)
                    after_tasks_field = False
                    continue
                if lower.startswith(('deadline:', 'due:', 'end:')):
                    deadline = _parse_dt(stripped.split(':', 1)[1].strip()) or deadline
                    after_tasks_field = False
                    continue
                if lower.startswith(('members:', 'people:', 'team:')):
                    members = _parse_members(stripped.split(':', 1)[1].strip())
                    after_tasks_field = False
                    continue
                if lower.startswith(('status:',)):
                    status = stripped.split(':', 1)[1].strip().lower() or status
                    after_tasks_field = False
                    continue
                if lower.startswith(('id:', 'objective_id:')):
                    objective_id = stripped.split(':', 1)[1].strip() or objective_id
                    after_tasks_field = False
                    continue
                if lower.startswith(('tasks:',)):
                    # "tasks:" field — what follows on the same line is ignored,
                    # but subsequent list items are treated as tasks
                    after_tasks_field = True
                    continue

            if not title:
                continue
            spec = ObjectiveSpec(
                title=title,
                description='\n'.join(description_lines).strip() if description_lines else None,
                deadline=deadline,
                status=status,
                members=members,
                tasks=tasks,
                objective_id=objective_id,
                raw=raw_block,
            )
            specs.append(spec)

    return specs


def strip_objective_blocks(text: str) -> str:
    if not text:
        return ''
    masked = _mask_code_fences(text)
    out = text
    for pattern in _OBJECTIVE_BLOCK_PATTERNS:
        for match in pattern.finditer(masked):
            out = out.replace(text[match.start():match.end()], '').strip()
    return out


class ObjectiveManager:
    """Manages objectives and their members."""

    def __init__(self, db: DatabaseManager, task_manager: Optional[TaskManager] = None):
        self.db = db
        self.task_manager = task_manager
        logger.info("Initializing ObjectiveManager")
        self._ensure_tables()
        logger.info("ObjectiveManager initialized successfully")

    def _ensure_tables(self) -> None:
        try:
            with self.db.get_connection() as conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS objectives (
                        id TEXT PRIMARY KEY,
                        title TEXT NOT NULL,
                        description TEXT,
                        status TEXT DEFAULT 'pending',
                        created_by TEXT NOT NULL,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        deadline TIMESTAMP,
                        visibility TEXT DEFAULT 'network',
                        origin_peer TEXT,
                        source_type TEXT,
                        source_id TEXT
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS objective_members (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        objective_id TEXT NOT NULL,
                        user_id TEXT NOT NULL,
                        role TEXT DEFAULT 'contributor',
                        added_by TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE(objective_id, user_id),
                        FOREIGN KEY (objective_id) REFERENCES objectives(id) ON DELETE CASCADE
                    )
                    """
                )
                conn.execute("CREATE INDEX IF NOT EXISTS idx_objectives_status ON objectives(status)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_objectives_updated_at ON objectives(updated_at)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_objective_members_obj ON objective_members(objective_id)")
                conn.commit()
        except Exception as e:
            logger.error(f"Failed to ensure objective tables: {e}", exc_info=True)
            raise

    def _normalize_status(self, status: Optional[str]) -> str:
        value = (status or 'pending').strip().lower()
        return value if value in OBJECTIVE_STATUSES else 'pending'

    def _normalize_role(self, role: Optional[str]) -> str:
        value = (role or 'contributor').strip().lower()
        return value if value in OBJECTIVE_ROLES else 'contributor'

    def _row_to_objective(self, row: Any) -> Objective:
        return Objective(
            id=row['id'],
            title=row['title'],
            description=row['description'],
            status=row['status'] or 'pending',
            created_by=row['created_by'],
            created_at=_parse_dt(row['created_at']) or _now_utc(),
            updated_at=_parse_dt(row['updated_at']) or _now_utc(),
            deadline=_parse_dt(row['deadline']),
            visibility=row['visibility'] or 'network',
            origin_peer=row['origin_peer'],
            source_type=row['source_type'],
            source_id=row['source_id'],
        )

    def _count_tasks(self, conn: Any, objective_id: str) -> Tuple[int, int]:
        row = conn.execute(
            "SELECT COUNT(*) as total, SUM(CASE WHEN status = 'done' THEN 1 ELSE 0 END) as done "
            "FROM tasks WHERE objective_id = ?",
            (objective_id,)
        ).fetchone()
        total = row['total'] or 0
        done = row['done'] or 0
        return total, done

    def _maybe_autostatus(self, conn: Any, objective_id: str, current_status: str,
                           total: int, done: int) -> str:
        status = current_status
        if status not in OBJECTIVE_STATUSES:
            status = 'pending'
        if status == 'archived':
            return status
        if total > 0 and done >= total and status != 'completed':
            status = 'completed'
        elif status == 'pending' and done > 0:
            status = 'in_progress'
        return status

    def _set_members(self, conn: Any, objective_id: str, members: List[Dict[str, Any]],
                     added_by: Optional[str] = None) -> None:
        conn.execute("DELETE FROM objective_members WHERE objective_id = ?", (objective_id,))
        for member in members or []:
            user_id = member.get('user_id')
            if not user_id:
                continue
            role = self._normalize_role(member.get('role'))
            conn.execute(
                """
                INSERT OR REPLACE INTO objective_members
                (objective_id, user_id, role, added_by, created_at)
                VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                """,
                (objective_id, user_id, role, added_by)
            )

    def upsert_objective(
        self,
        objective_id: str,
        title: str,
        created_by: str,
        description: Optional[str] = None,
        status: Optional[str] = None,
        deadline: Optional[Any] = None,
        visibility: Optional[str] = None,
        origin_peer: Optional[str] = None,
        source_type: Optional[str] = None,
        source_id: Optional[str] = None,
        created_at: Optional[Any] = None,
        updated_at: Optional[Any] = None,
        members: Optional[List[Dict[str, Any]]] = None,
        tasks: Optional[List[Dict[str, Any]]] = None,
        updated_by: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        if not objective_id or not title:
            return None
        now_dt = _parse_dt(updated_at) or _now_utc()
        created_dt = _parse_dt(created_at) or _now_utc()
        status_val = self._normalize_status(status)
        visibility_val = (visibility or 'network').strip().lower()
        deadline_dt = _parse_dt(deadline)

        try:
            with self.db.get_connection() as conn:
                row = conn.execute(
                    "SELECT * FROM objectives WHERE id = ?",
                    (objective_id,)
                ).fetchone()
                if row:
                    existing = self._row_to_objective(row)
                    if status is None:
                        status_val = existing.status
                    conn.execute(
                        """
                        UPDATE objectives
                        SET title = ?, description = ?, status = ?, deadline = ?,
                            visibility = ?, origin_peer = ?, source_type = ?, source_id = ?,
                            updated_at = ?
                        WHERE id = ?
                        """,
                        (
                            title,
                            description,
                            status_val,
                            deadline_dt.isoformat() if deadline_dt else None,
                            visibility_val,
                            origin_peer,
                            source_type,
                            source_id,
                            now_dt.isoformat(),
                            objective_id,
                        )
                    )
                else:
                    conn.execute(
                        """
                        INSERT INTO objectives
                        (id, title, description, status, created_by, created_at, updated_at, deadline,
                         visibility, origin_peer, source_type, source_id)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            objective_id,
                            title,
                            description,
                            status_val,
                            created_by,
                            created_dt.isoformat(),
                            now_dt.isoformat(),
                            deadline_dt.isoformat() if deadline_dt else None,
                            visibility_val,
                            origin_peer,
                            source_type,
                            source_id,
                        )
                    )

                if members is not None:
                    self._set_members(conn, objective_id, members, added_by=updated_by or created_by)

                conn.commit()

            if tasks and self.task_manager:
                total_tasks = len(tasks)
                for idx, task in enumerate(tasks):
                    title_val = task.get('title')
                    if not title_val:
                        continue
                    task_id = task.get('task_id') or derive_task_id(
                        'objective', objective_id, idx, total_tasks
                    )
                    status_val = task.get('status') or 'open'
                    if status_val not in TASK_STATUSES:
                        status_val = 'open'
                    self.task_manager.create_task(
                        task_id=task_id,
                        title=title_val,
                        description=task.get('description'),
                        status=status_val,
                        priority=task.get('priority'),
                        created_by=created_by,
                        assigned_to=task.get('assigned_to'),
                        due_at=task.get('due_at'),
                        visibility=visibility_val,
                        metadata=task.get('metadata'),
                        origin_peer=origin_peer,
                        source_type=task.get('source_type') or 'human',
                        updated_by=updated_by or created_by,
                        objective_id=objective_id,
                    )

            return self.get_objective(objective_id, include_members=True, include_tasks=True)
        except Exception as e:
            logger.error(f"Failed to upsert objective {objective_id}: {e}", exc_info=True)
            return None

    def get_objective(self, objective_id: str,
                      include_members: bool = True,
                      include_tasks: bool = False) -> Optional[Dict[str, Any]]:
        if not objective_id:
            return None
        try:
            with self.db.get_connection() as conn:
                row = conn.execute(
                    "SELECT * FROM objectives WHERE id = ?",
                    (objective_id,)
                ).fetchone()
                if not row:
                    return None
                obj = self._row_to_objective(row)
                total, done = self._count_tasks(conn, objective_id)
                status = self._maybe_autostatus(conn, objective_id, obj.status, total, done)
                if status != obj.status:
                    conn.execute(
                        "UPDATE objectives SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                        (status, objective_id)
                    )
                    obj.status = status
                    obj.updated_at = _now_utc()
                    conn.commit()

                members = []
                if include_members:
                    rows = conn.execute(
                        "SELECT user_id, role, added_by, created_at FROM objective_members "
                        "WHERE objective_id = ? ORDER BY created_at ASC",
                        (objective_id,)
                    ).fetchall()
                    for mrow in rows:
                        members.append({
                            'user_id': mrow['user_id'],
                            'role': mrow['role'] or 'contributor',
                            'added_by': mrow['added_by'],
                            'created_at': mrow['created_at'],
                        })

                tasks = []
                if include_tasks:
                    rows = conn.execute(
                        "SELECT * FROM tasks WHERE objective_id = ? ORDER BY created_at ASC",
                        (objective_id,)
                    ).fetchall()
                    for trow in rows:
                        try:
                            task = self.task_manager._row_to_task(trow) if self.task_manager else None
                        except Exception:
                            task = None
                        if task:
                            tasks.append(task.to_dict())
                        else:
                            tasks.append(dict(trow))

                data = obj.to_dict()
                data['tasks_total'] = total
                data['tasks_done'] = done
                data['progress_percent'] = int(round((done / total) * 100)) if total else 0
                if include_members:
                    data['members'] = members
                if include_tasks:
                    data['tasks'] = tasks
                return data
        except Exception as e:
            logger.error(f"Failed to get objective {objective_id}: {e}")
            return None

    def list_objectives(self, limit: int = 50, status: Optional[str] = None) -> List[Dict[str, Any]]:
        try:
            limit_val = max(1, min(int(limit or 50), 200))
            clauses = []
            params: List[Any] = []
            if status:
                clauses.append("status = ?")
                params.append(self._normalize_status(status))
            query = "SELECT * FROM objectives"
            if clauses:
                query += " WHERE " + " AND ".join(clauses)
            query += " ORDER BY updated_at DESC LIMIT ?"
            params.append(limit_val)

            results: List[Dict[str, Any]] = []
            with self.db.get_connection() as conn:
                rows = conn.execute(query, params).fetchall()
                for row in rows:
                    obj = self._row_to_objective(row)
                    total, done = self._count_tasks(conn, obj.id)
                    status_val = self._maybe_autostatus(conn, obj.id, obj.status, total, done)
                    if status_val != obj.status:
                        conn.execute(
                            "UPDATE objectives SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                            (status_val, obj.id)
                        )
                        obj.status = status_val
                        obj.updated_at = _now_utc()
                    data = obj.to_dict()
                    data['tasks_total'] = total
                    data['tasks_done'] = done
                    data['progress_percent'] = int(round((done / total) * 100)) if total else 0
                    results.append(data)
                conn.commit()
            return results
        except Exception as e:
            logger.error(f"Failed to list objectives: {e}")
            return []

    def update_objective(self, objective_id: str, updates: Dict[str, Any],
                         actor_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        if not objective_id or not updates:
            return self.get_objective(objective_id, include_members=True, include_tasks=True)

        allowed = {
            'title', 'description', 'status', 'deadline', 'visibility',
            'origin_peer', 'source_type', 'source_id'
        }
        fields = []
        values: List[Any] = []

        for key, value in updates.items():
            if key not in allowed:
                continue
            if key == 'status':
                fields.append('status = ?')
                values.append(self._normalize_status(value))
                continue
            if key == 'deadline':
                dt = _parse_dt(value)
                fields.append('deadline = ?')
                values.append(dt.isoformat() if dt else None)
                continue
            # Safety: key is already verified to be in `allowed` (a hardcoded
            # frozenset of literal column names). This guard makes the invariant
            # explicit so future changes cannot accidentally open an injection path.
            if key not in allowed:
                raise ValueError(f"Column name not in allowed set: {key!r}")
            fields.append(f"{key} = ?")
            values.append(value)

        if not fields:
            return self.get_objective(objective_id, include_members=True, include_tasks=True)

        fields.append('updated_at = ?')
        values.append(_now_utc().isoformat())
        values.append(objective_id)

        try:
            with self.db.get_connection() as conn:
                conn.execute(f"UPDATE objectives SET {', '.join(fields)} WHERE id = ?", values)
                conn.commit()
            return self.get_objective(objective_id, include_members=True, include_tasks=True)
        except Exception as e:
            logger.error(f"Failed to update objective {objective_id}: {e}")
            return None

    def set_members(self, objective_id: str, members: List[Dict[str, Any]],
                    added_by: Optional[str] = None) -> Optional[Dict[str, Any]]:
        if not objective_id:
            return None
        try:
            with self.db.get_connection() as conn:
                self._set_members(conn, objective_id, members, added_by=added_by)
                conn.commit()
            return self.get_objective(objective_id, include_members=True, include_tasks=True)
        except Exception as e:
            logger.error(f"Failed to set members for objective {objective_id}: {e}")
            return None
