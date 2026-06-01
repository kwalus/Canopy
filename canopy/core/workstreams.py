"""Workstream management for Canopy.

A Workstream is a durable, human-readable coordination object that links a goal,
participants, status events, and produced artifacts. It is intentionally separate
from tasks/requests: tasks capture discrete asks; Workstreams capture sustained
agent-human work effort and its evidence trail.
"""

from __future__ import annotations

import json
import logging
import secrets
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence

from .database import DatabaseManager

logger = logging.getLogger('canopy.workstreams')

WORKSTREAM_STATUSES = (
    'active',
    'blocked',
    'review_ready',
    'complete',
    'closed',
    'archived',
    'cancelled',
)
WORKSTREAM_PRIORITIES = ('low', 'normal', 'high', 'critical')
WORKSTREAM_ROLES = ('owner', 'lead', 'contributor', 'reviewer', 'watcher', 'assignee')
WORKSTREAM_EVENT_TYPES = (
    'created',
    'status',
    'progress',
    'artifact',
    'blocker',
    'decision',
    'evidence',
    'review',
    'comment',
    'handoff',
)
WORKSTREAM_EVENT_STATES = (
    'open',
    'resolved',
    'candidate',
    'confirmed',
    'stale',
    'superseded',
    'waiting',
    'complete',
)
WORKSTREAM_ARTIFACT_TYPES = (
    'file',
    'digestion',
    'message',
    'post',
    'url',
    'report',
    'figure',
    'code',
    'note',
)
_EDIT_ROLES = {'owner', 'lead', 'contributor', 'assignee'}
_CONTRIBUTE_ROLES = {'owner', 'lead', 'contributor', 'assignee', 'reviewer'}


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _coerce_text(value: Any, *, limit: int = 8000) -> Optional[str]:
    if value is None:
        return None
    text = str(value).replace('\x00', '').strip()
    if not text:
        return None
    if len(text) > limit:
        return text[:limit]
    return text


def _json_dumps(value: Optional[Dict[str, Any]]) -> Optional[str]:
    if not value:
        return None
    try:
        return json.dumps(value, separators=(',', ':'), sort_keys=True)
    except Exception:
        logger.debug("Unable to JSON encode workstream metadata", exc_info=True)
        return None


def _json_loads(value: Any) -> Dict[str, Any]:
    if not value:
        return {}
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(str(value))
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _normalize_choice(value: Any, allowed: Sequence[str], default: str) -> str:
    clean = str(value or default).strip().lower().replace(' ', '_')
    return clean if clean in allowed else default


def _normalize_optional_choice(value: Any, allowed: Sequence[str]) -> Optional[str]:
    clean = str(value or '').strip().lower().replace(' ', '_')
    return clean if clean in allowed else None


def _infer_artifact_type(artifact: Dict[str, Any]) -> str:
    explicit = artifact.get('artifact_type') or artifact.get('type')
    if explicit:
        return _normalize_choice(explicit, WORKSTREAM_ARTIFACT_TYPES, 'note')
    if artifact.get('digestion_id'):
        return 'digestion'
    if artifact.get('file_id'):
        return 'file'
    if artifact.get('message_id'):
        return 'message'
    if artifact.get('post_id'):
        return 'post'
    if artifact.get('url'):
        return 'url'
    return 'note'


def _artifact_ref_id(artifact: Dict[str, Any]) -> Optional[str]:
    for key in (
        'ref_id',
        'reference_id',
        'file_id',
        'digestion_id',
        'message_id',
        'post_id',
        'artifact_id',
        'id',
        'url',
    ):
        value = _coerce_text(artifact.get(key), limit=500)
        if value:
            return value
    return None


def _new_id(prefix: str) -> str:
    return f"{prefix}{secrets.token_hex(10)}"


@dataclass
class WorkstreamParticipant:
    user_id: str
    role: str = 'contributor'
    status: str = 'active'
    added_by: Optional[str] = None
    added_at: Optional[str] = None
    updated_at: Optional[str] = None
    user: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class WorkstreamEvent:
    id: str
    workstream_id: str
    event_type: str
    actor_user_id: str
    title: Optional[str]
    body: Optional[str]
    status: Optional[str]
    metadata: Dict[str, Any]
    created_at: str
    dedupe_key: Optional[str] = None
    actor: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class WorkstreamArtifact:
    id: str
    workstream_id: str
    artifact_type: str
    ref_id: str
    title: Optional[str]
    summary: Optional[str]
    metadata: Dict[str, Any]
    created_by: str
    created_at: str
    creator: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class Workstream:
    id: str
    title: str
    objective: Optional[str]
    required_output: Optional[str]
    status: str
    priority: str
    owner_user_id: str
    created_by: str
    created_at: str
    updated_at: str
    updated_by: Optional[str]
    source_type: Optional[str]
    source_id: Optional[str]
    channel_id: Optional[str]
    summary: Optional[str]
    next_action: Optional[str]
    visibility: str
    metadata: Dict[str, Any]
    owner: Dict[str, Any] = field(default_factory=dict)
    creator: Dict[str, Any] = field(default_factory=dict)
    participants: List[WorkstreamParticipant] = field(default_factory=list)
    events: List[WorkstreamEvent] = field(default_factory=list)
    artifacts: List[WorkstreamArtifact] = field(default_factory=list)

    def to_dict(self, *, include_details: bool = True) -> Dict[str, Any]:
        data = asdict(self)
        if not include_details:
            data.pop('events', None)
            data.pop('artifacts', None)
        return data


class WorkstreamManager:
    """Durable Workstream storage and access helpers."""

    def __init__(self, db: DatabaseManager):
        self.db = db
        logger.info("Initializing WorkstreamManager")
        self._ensure_tables()
        logger.info("WorkstreamManager initialized successfully")

    def _ensure_tables(self) -> None:
        try:
            with self.db.get_connection() as conn:
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS workstreams (
                        id TEXT PRIMARY KEY,
                        title TEXT NOT NULL,
                        objective TEXT,
                        required_output TEXT,
                        status TEXT NOT NULL DEFAULT 'active',
                        priority TEXT NOT NULL DEFAULT 'normal',
                        owner_user_id TEXT NOT NULL,
                        created_by TEXT NOT NULL,
                        created_at TIMESTAMP NOT NULL,
                        updated_at TIMESTAMP NOT NULL,
                        updated_by TEXT,
                        source_type TEXT,
                        source_id TEXT,
                        channel_id TEXT,
                        summary TEXT,
                        next_action TEXT,
                        visibility TEXT DEFAULT 'channel',
                        metadata_json TEXT
                    );
                    CREATE INDEX IF NOT EXISTS idx_workstreams_owner
                        ON workstreams(owner_user_id, updated_at DESC);
                    CREATE INDEX IF NOT EXISTS idx_workstreams_created_by
                        ON workstreams(created_by, updated_at DESC);
                    CREATE INDEX IF NOT EXISTS idx_workstreams_channel
                        ON workstreams(channel_id, updated_at DESC);
                    CREATE INDEX IF NOT EXISTS idx_workstreams_status
                        ON workstreams(status, updated_at DESC);

                    CREATE TABLE IF NOT EXISTS workstream_participants (
                        workstream_id TEXT NOT NULL,
                        user_id TEXT NOT NULL,
                        role TEXT NOT NULL DEFAULT 'contributor',
                        status TEXT NOT NULL DEFAULT 'active',
                        added_by TEXT,
                        added_at TIMESTAMP NOT NULL,
                        updated_at TIMESTAMP NOT NULL,
                        PRIMARY KEY (workstream_id, user_id),
                        FOREIGN KEY (workstream_id) REFERENCES workstreams(id) ON DELETE CASCADE
                    );
                    CREATE INDEX IF NOT EXISTS idx_workstream_participants_user
                        ON workstream_participants(user_id, status, updated_at DESC);

                    CREATE TABLE IF NOT EXISTS workstream_events (
                        id TEXT PRIMARY KEY,
                        workstream_id TEXT NOT NULL,
                        event_type TEXT NOT NULL,
                        actor_user_id TEXT NOT NULL,
                        title TEXT,
                        body TEXT,
                        status TEXT,
                        metadata_json TEXT,
                        dedupe_key TEXT,
                        created_at TIMESTAMP NOT NULL,
                        FOREIGN KEY (workstream_id) REFERENCES workstreams(id) ON DELETE CASCADE,
                        UNIQUE (workstream_id, dedupe_key)
                    );
                    CREATE INDEX IF NOT EXISTS idx_workstream_events_ws
                        ON workstream_events(workstream_id, created_at DESC);
                    CREATE INDEX IF NOT EXISTS idx_workstream_events_actor
                        ON workstream_events(actor_user_id, created_at DESC);

                    CREATE TABLE IF NOT EXISTS workstream_artifacts (
                        id TEXT PRIMARY KEY,
                        workstream_id TEXT NOT NULL,
                        artifact_type TEXT NOT NULL,
                        ref_id TEXT NOT NULL,
                        title TEXT,
                        summary TEXT,
                        metadata_json TEXT,
                        created_by TEXT NOT NULL,
                        created_at TIMESTAMP NOT NULL,
                        FOREIGN KEY (workstream_id) REFERENCES workstreams(id) ON DELETE CASCADE,
                        UNIQUE (workstream_id, artifact_type, ref_id)
                    );
                    CREATE INDEX IF NOT EXISTS idx_workstream_artifacts_ws
                        ON workstream_artifacts(workstream_id, created_at DESC);
                    CREATE INDEX IF NOT EXISTS idx_workstream_artifacts_ref
                        ON workstream_artifacts(artifact_type, ref_id);
                    """
                )
                conn.commit()
        except Exception as exc:
            logger.error("Failed to ensure Workstream tables: %s", exc, exc_info=True)
            raise

    def _user_payloads(self, conn: Any, user_ids: Iterable[str]) -> Dict[str, Dict[str, Any]]:
        ids = [str(uid) for uid in dict.fromkeys(str(uid or '').strip() for uid in user_ids) if uid]
        if not ids:
            return {}
        placeholders = ','.join('?' for _ in ids)
        try:
            user_cols = {str(row['name']) for row in conn.execute("PRAGMA table_info(users)").fetchall()}
        except Exception:
            user_cols = set()
        avatar_expr = "avatar_file_id" if "avatar_file_id" in user_cols else "NULL AS avatar_file_id"
        account_expr = "account_type" if "account_type" in user_cols else "NULL AS account_type"
        display_expr = "display_name" if "display_name" in user_cols else "NULL AS display_name"
        rows = conn.execute(
            f"""
            SELECT id, username, {display_expr}, {account_expr}, {avatar_expr}
            FROM users
            WHERE id IN ({placeholders})
            """,
            tuple(ids),
        ).fetchall()
        payloads: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            display = row['display_name'] or row['username'] or row['id']
            payloads[row['id']] = {
                'id': row['id'],
                'username': row['username'],
                'display_name': display,
                'account_type': row['account_type'] if 'account_type' in row.keys() else None,
                'avatar_file_id': row['avatar_file_id'] if 'avatar_file_id' in row.keys() else None,
            }
            if payloads[row['id']].get('avatar_file_id'):
                payloads[row['id']]['avatar_url'] = f"/files/{payloads[row['id']]['avatar_file_id']}"
            else:
                payloads[row['id']]['avatar_url'] = None
        for uid in ids:
            payloads.setdefault(uid, {'id': uid, 'username': uid, 'display_name': uid})
        return payloads

    def _row_to_workstream(self, row: Any) -> Workstream:
        return Workstream(
            id=row['id'],
            title=row['title'],
            objective=row['objective'],
            required_output=row['required_output'],
            status=row['status'] or 'active',
            priority=row['priority'] or 'normal',
            owner_user_id=row['owner_user_id'],
            created_by=row['created_by'],
            created_at=str(row['created_at'] or ''),
            updated_at=str(row['updated_at'] or ''),
            updated_by=row['updated_by'],
            source_type=row['source_type'],
            source_id=row['source_id'],
            channel_id=row['channel_id'],
            summary=row['summary'],
            next_action=row['next_action'],
            visibility=row['visibility'] or 'channel',
            metadata=_json_loads(row['metadata_json']),
        )

    def _hydrate(self, conn: Any, ws: Workstream, *, event_limit: int = 50) -> Workstream:
        participants = conn.execute(
            """
            SELECT * FROM workstream_participants
            WHERE workstream_id = ? AND status != 'removed'
            ORDER BY CASE role
                WHEN 'owner' THEN 0 WHEN 'lead' THEN 1 WHEN 'assignee' THEN 2
                WHEN 'contributor' THEN 3 WHEN 'reviewer' THEN 4 ELSE 5 END,
                updated_at DESC
            """,
            (ws.id,),
        ).fetchall()
        artifacts = conn.execute(
            "SELECT * FROM workstream_artifacts WHERE workstream_id = ? ORDER BY created_at DESC LIMIT 80",
            (ws.id,),
        ).fetchall()
        events = conn.execute(
            "SELECT * FROM workstream_events WHERE workstream_id = ? ORDER BY created_at DESC LIMIT ?",
            (ws.id, int(event_limit)),
        ).fetchall()
        user_ids: List[str] = [ws.owner_user_id, ws.created_by]
        user_ids.extend([p['user_id'] for p in participants])
        user_ids.extend([a['created_by'] for a in artifacts])
        user_ids.extend([e['actor_user_id'] for e in events])
        users = self._user_payloads(conn, user_ids)
        ws.owner = users.get(ws.owner_user_id, {})
        ws.creator = users.get(ws.created_by, {})
        ws.participants = [
            WorkstreamParticipant(
                user_id=row['user_id'],
                role=row['role'] or 'contributor',
                status=row['status'] or 'active',
                added_by=row['added_by'],
                added_at=str(row['added_at'] or ''),
                updated_at=str(row['updated_at'] or ''),
                user=users.get(row['user_id'], {}),
            )
            for row in participants
        ]
        ws.artifacts = [
            WorkstreamArtifact(
                id=row['id'],
                workstream_id=row['workstream_id'],
                artifact_type=row['artifact_type'],
                ref_id=row['ref_id'],
                title=row['title'],
                summary=row['summary'],
                metadata=_json_loads(row['metadata_json']),
                created_by=row['created_by'],
                created_at=str(row['created_at'] or ''),
                creator=users.get(row['created_by'], {}),
            )
            for row in artifacts
        ]
        ws.events = [
            WorkstreamEvent(
                id=row['id'],
                workstream_id=row['workstream_id'],
                event_type=row['event_type'],
                actor_user_id=row['actor_user_id'],
                title=row['title'],
                body=row['body'],
                status=row['status'],
                metadata=_json_loads(row['metadata_json']),
                dedupe_key=row['dedupe_key'],
                created_at=str(row['created_at'] or ''),
                actor=users.get(row['actor_user_id'], {}),
            )
            for row in events
        ]
        return ws

    def create_workstream(
        self,
        *,
        title: str,
        owner_user_id: str,
        created_by: str,
        objective: Optional[str] = None,
        required_output: Optional[str] = None,
        status: str = 'active',
        priority: str = 'normal',
        channel_id: Optional[str] = None,
        source_type: Optional[str] = None,
        source_id: Optional[str] = None,
        participants: Optional[List[Dict[str, Any]]] = None,
        artifacts: Optional[List[Dict[str, Any]]] = None,
        summary: Optional[str] = None,
        next_action: Optional[str] = None,
        visibility: str = 'channel',
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Workstream:
        title_clean = _coerce_text(title, limit=300)
        if not title_clean:
            raise ValueError('title required')
        owner = _coerce_text(owner_user_id, limit=120)
        actor = _coerce_text(created_by, limit=120)
        if not owner or not actor:
            raise ValueError('owner_user_id and created_by required')
        now = _now_iso()
        ws_id = _new_id('Ws')
        status_clean = _normalize_choice(status, WORKSTREAM_STATUSES, 'active')
        priority_clean = _normalize_choice(priority, WORKSTREAM_PRIORITIES, 'normal')
        with self.db.get_connection() as conn:
            conn.execute(
                """
                INSERT INTO workstreams (
                    id, title, objective, required_output, status, priority,
                    owner_user_id, created_by, created_at, updated_at, updated_by,
                    source_type, source_id, channel_id, summary, next_action,
                    visibility, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ws_id,
                    title_clean,
                    _coerce_text(objective),
                    _coerce_text(required_output),
                    status_clean,
                    priority_clean,
                    owner,
                    actor,
                    now,
                    now,
                    actor,
                    _coerce_text(source_type, limit=80),
                    _coerce_text(source_id, limit=160),
                    _coerce_text(channel_id, limit=160),
                    _coerce_text(summary),
                    _coerce_text(next_action),
                    _coerce_text(visibility, limit=40) or 'channel',
                    _json_dumps(metadata),
                ),
            )
            self._upsert_participant_conn(conn, ws_id, owner, 'owner', actor, now)
            for item in participants or []:
                user_id = _coerce_text(item.get('user_id') or item.get('id'), limit=120)
                if not user_id:
                    continue
                role = _normalize_choice(item.get('role'), WORKSTREAM_ROLES, 'contributor')
                self._upsert_participant_conn(conn, ws_id, user_id, role, actor, now)
            event_id = _new_id('Wse')
            conn.execute(
                """
                INSERT INTO workstream_events (
                    id, workstream_id, event_type, actor_user_id, title, body,
                    status, metadata_json, dedupe_key, created_at
                ) VALUES (?, ?, 'created', ?, ?, ?, ?, ?, NULL, ?)
                """,
                (event_id, ws_id, actor, 'Workstream created', title_clean, status_clean, _json_dumps({'priority': priority_clean}), now),
            )
            for artifact in artifacts or []:
                self._add_artifact_conn(conn, ws_id, artifact, actor, now)
            conn.commit()
            row = conn.execute("SELECT * FROM workstreams WHERE id = ?", (ws_id,)).fetchone()
            return self._hydrate(conn, self._row_to_workstream(row))

    def _upsert_participant_conn(self, conn: Any, workstream_id: str, user_id: str, role: str, actor: str, now: str) -> None:
        role_clean = _normalize_choice(role, WORKSTREAM_ROLES, 'contributor')
        conn.execute(
            """
            INSERT INTO workstream_participants (workstream_id, user_id, role, status, added_by, added_at, updated_at)
            VALUES (?, ?, ?, 'active', ?, ?, ?)
            ON CONFLICT(workstream_id, user_id) DO UPDATE SET
                role = excluded.role,
                status = 'active',
                updated_at = excluded.updated_at
            """,
            (workstream_id, user_id, role_clean, actor, now, now),
        )

    def _add_artifact_conn(self, conn: Any, workstream_id: str, artifact: Dict[str, Any], actor: str, now: str) -> Optional[str]:
        artifact_type = _infer_artifact_type(artifact)
        ref_id = _artifact_ref_id(artifact)
        if not ref_id:
            return None
        artifact_id = _new_id('Wsa')
        conn.execute(
            """
            INSERT INTO workstream_artifacts (
                id, workstream_id, artifact_type, ref_id, title, summary,
                metadata_json, created_by, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(workstream_id, artifact_type, ref_id) DO UPDATE SET
                title = COALESCE(excluded.title, workstream_artifacts.title),
                summary = COALESCE(excluded.summary, workstream_artifacts.summary),
                metadata_json = COALESCE(excluded.metadata_json, workstream_artifacts.metadata_json)
            """,
            (
                artifact_id,
                workstream_id,
                artifact_type,
                ref_id,
                _coerce_text(artifact.get('title'), limit=300),
                _coerce_text(artifact.get('summary'), limit=1200),
                _json_dumps(artifact.get('metadata') if isinstance(artifact.get('metadata'), dict) else None),
                actor,
                now,
            ),
        )
        return artifact_id

    def get_workstream(self, workstream_id: str, *, event_limit: int = 50) -> Optional[Workstream]:
        clean = _coerce_text(workstream_id, limit=120)
        if not clean:
            return None
        with self.db.get_connection() as conn:
            row = conn.execute("SELECT * FROM workstreams WHERE id = ?", (clean,)).fetchone()
            if not row:
                return None
            return self._hydrate(conn, self._row_to_workstream(row), event_limit=event_limit)

    def list_workstreams(
        self,
        *,
        user_id: Optional[str] = None,
        channel_id: Optional[str] = None,
        status: Optional[str] = None,
        include_closed: bool = False,
        limit: int = 50,
    ) -> List[Workstream]:
        clauses: List[str] = []
        params: List[Any] = []
        clean_user = _coerce_text(user_id, limit=120)
        clean_channel = _coerce_text(channel_id, limit=160)
        if clean_user and clean_channel:
            clauses.append(
                """
                (w.owner_user_id = ? OR w.created_by = ? OR EXISTS (
                    SELECT 1 FROM workstream_participants wp
                    WHERE wp.workstream_id = w.id AND wp.user_id = ? AND wp.status != 'removed'
                ) OR w.channel_id = ?)
                """
            )
            params.extend([clean_user, clean_user, clean_user, clean_channel])
        elif clean_user:
            clauses.append(
                """
                (w.owner_user_id = ? OR w.created_by = ? OR EXISTS (
                    SELECT 1 FROM workstream_participants wp
                    WHERE wp.workstream_id = w.id AND wp.user_id = ? AND wp.status != 'removed'
                ))
                """
            )
            params.extend([clean_user, clean_user, clean_user])
        elif clean_channel:
            clauses.append("w.channel_id = ?")
            params.append(clean_channel)
        if status:
            clauses.append("w.status = ?")
            params.append(_normalize_choice(status, WORKSTREAM_STATUSES, 'active'))
        elif not include_closed:
            clauses.append("w.status NOT IN ('closed', 'archived', 'cancelled')")
        where = 'WHERE ' + ' AND '.join(f"({clause})" for clause in clauses) if clauses else ''
        sql = f"SELECT w.* FROM workstreams w {where} ORDER BY w.updated_at DESC LIMIT ?"
        params.append(max(1, min(int(limit or 50), 200)))
        with self.db.get_connection() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
            users = self._user_payloads(conn, [r['owner_user_id'] for r in rows] + [r['created_by'] for r in rows])
            result: List[Workstream] = []
            for row in rows:
                ws = self._row_to_workstream(row)
                ws.owner = users.get(ws.owner_user_id, {})
                ws.creator = users.get(ws.created_by, {})
                result.append(ws)
            return result

    def user_can_view(self, workstream_id: str, user_id: Optional[str]) -> bool:
        if not workstream_id or not user_id:
            return False
        with self.db.get_connection() as conn:
            row = conn.execute(
                """
                SELECT w.owner_user_id, w.created_by, w.channel_id,
                       wp.user_id AS participant_user,
                       cm.user_id AS channel_member
                FROM workstreams w
                LEFT JOIN workstream_participants wp
                  ON wp.workstream_id = w.id AND wp.user_id = ? AND wp.status != 'removed'
                LEFT JOIN channel_members cm
                  ON cm.channel_id = w.channel_id AND cm.user_id = ?
                WHERE w.id = ?
                """,
                (user_id, user_id, workstream_id),
            ).fetchone()
            if not row:
                return False
            return bool(
                row['owner_user_id'] == user_id
                or row['created_by'] == user_id
                or row['participant_user']
                or row['channel_member']
            )

    def user_can_edit(self, workstream_id: str, user_id: Optional[str]) -> bool:
        if not workstream_id or not user_id:
            return False
        with self.db.get_connection() as conn:
            row = conn.execute(
                """
                SELECT w.owner_user_id, w.created_by, wp.role
                FROM workstreams w
                LEFT JOIN workstream_participants wp
                  ON wp.workstream_id = w.id AND wp.user_id = ? AND wp.status != 'removed'
                WHERE w.id = ?
                """,
                (user_id, workstream_id),
            ).fetchone()
            if not row:
                return False
            return bool(row['owner_user_id'] == user_id or row['created_by'] == user_id or (row['role'] in _EDIT_ROLES))

    def user_can_contribute(self, workstream_id: str, user_id: Optional[str]) -> bool:
        if not workstream_id or not user_id:
            return False
        with self.db.get_connection() as conn:
            row = conn.execute(
                """
                SELECT w.owner_user_id, w.created_by, wp.role
                FROM workstreams w
                LEFT JOIN workstream_participants wp
                  ON wp.workstream_id = w.id AND wp.user_id = ? AND wp.status != 'removed'
                WHERE w.id = ?
                """,
                (user_id, workstream_id),
            ).fetchone()
            if not row:
                return False
            return bool(
                row['owner_user_id'] == user_id
                or row['created_by'] == user_id
                or (row['role'] in _CONTRIBUTE_ROLES)
            )

    def update_workstream(self, workstream_id: str, *, actor_user_id: str, updates: Dict[str, Any]) -> Optional[Workstream]:
        allowed_fields = {
            'title', 'objective', 'required_output', 'status', 'priority',
            'summary', 'next_action', 'visibility', 'metadata'
        }
        clean_updates = {k: v for k, v in (updates or {}).items() if k in allowed_fields}
        if not clean_updates:
            return self.get_workstream(workstream_id)
        assignments: List[str] = []
        params: List[Any] = []
        for key, value in clean_updates.items():
            column = 'metadata_json' if key == 'metadata' else key
            if key == 'status':
                value = _normalize_choice(value, WORKSTREAM_STATUSES, 'active')
            elif key == 'priority':
                value = _normalize_choice(value, WORKSTREAM_PRIORITIES, 'normal')
            elif key == 'metadata':
                value = _json_dumps(value if isinstance(value, dict) else None)
            else:
                value = _coerce_text(value, limit=8000 if key not in {'title'} else 300)
            assignments.append(f"{column} = ?")
            params.append(value)
        now = _now_iso()
        assignments.extend(["updated_at = ?", "updated_by = ?"])
        params.extend([now, actor_user_id, workstream_id])
        with self.db.get_connection() as conn:
            cur = conn.execute(
                f"UPDATE workstreams SET {', '.join(assignments)} WHERE id = ?",
                tuple(params),
            )
            if cur.rowcount <= 0:
                return None
            if 'status' in clean_updates:
                conn.execute(
                    """
                    INSERT INTO workstream_events (id, workstream_id, event_type, actor_user_id, title, body, status, metadata_json, dedupe_key, created_at)
                    VALUES (?, ?, 'status', ?, ?, NULL, ?, NULL, NULL, ?)
                    """,
                    (_new_id('Wse'), workstream_id, actor_user_id, f"Status changed to {clean_updates['status']}", _normalize_choice(clean_updates['status'], WORKSTREAM_STATUSES, 'active'), now),
                )
            conn.commit()
            row = conn.execute("SELECT * FROM workstreams WHERE id = ?", (workstream_id,)).fetchone()
            return self._hydrate(conn, self._row_to_workstream(row)) if row else None

    def set_participants(self, workstream_id: str, *, actor_user_id: str, participants: List[Dict[str, Any]], replace: bool = False) -> Optional[Workstream]:
        now = _now_iso()
        with self.db.get_connection() as conn:
            if replace:
                conn.execute(
                    "UPDATE workstream_participants SET status = 'removed', updated_at = ? WHERE workstream_id = ? AND role != 'owner'",
                    (now, workstream_id),
                )
            changed: List[str] = []
            for item in participants or []:
                user_id = _coerce_text(item.get('user_id') or item.get('id'), limit=120)
                if not user_id:
                    continue
                role = _normalize_choice(item.get('role'), WORKSTREAM_ROLES, 'contributor')
                self._upsert_participant_conn(conn, workstream_id, user_id, role, actor_user_id, now)
                changed.append(user_id)
            conn.execute("UPDATE workstreams SET updated_at = ?, updated_by = ? WHERE id = ?", (now, actor_user_id, workstream_id))
            if changed:
                conn.execute(
                    """
                    INSERT INTO workstream_events (id, workstream_id, event_type, actor_user_id, title, body, status, metadata_json, dedupe_key, created_at)
                    VALUES (?, ?, 'progress', ?, 'Participants updated', ?, NULL, ?, NULL, ?)
                    """,
                    (_new_id('Wse'), workstream_id, actor_user_id, ', '.join(changed[:20]), _json_dumps({'replace': replace, 'count': len(changed)}), now),
                )
            conn.commit()
            row = conn.execute("SELECT * FROM workstreams WHERE id = ?", (workstream_id,)).fetchone()
            return self._hydrate(conn, self._row_to_workstream(row)) if row else None

    def claim_workstream(self, workstream_id: str, *, actor_user_id: str) -> Optional[Workstream]:
        """Add the actor to a Workstream without downgrading an existing role."""
        now = _now_iso()
        with self.db.get_connection() as conn:
            row = conn.execute("SELECT * FROM workstreams WHERE id = ?", (workstream_id,)).fetchone()
            if not row:
                return None
            existing = conn.execute(
                """
                SELECT role, status FROM workstream_participants
                WHERE workstream_id = ? AND user_id = ?
                """,
                (workstream_id, actor_user_id),
            ).fetchone()
            role = 'contributor'
            if existing and existing['status'] != 'removed':
                existing_role = _normalize_choice(existing['role'], WORKSTREAM_ROLES, 'contributor')
                role = 'contributor' if existing_role == 'watcher' else existing_role
            self._upsert_participant_conn(conn, workstream_id, actor_user_id, role, actor_user_id, now)
            conn.execute("UPDATE workstreams SET updated_at = ?, updated_by = ? WHERE id = ?", (now, actor_user_id, workstream_id))
            conn.execute(
                """
                INSERT INTO workstream_events (
                    id, workstream_id, event_type, actor_user_id, title, body,
                    status, metadata_json, dedupe_key, created_at
                ) VALUES (?, ?, 'progress', ?, 'Workstream claimed', ?, NULL, ?, ?, ?)
                ON CONFLICT(workstream_id, dedupe_key) DO NOTHING
                """,
                (
                    _new_id('Wse'),
                    workstream_id,
                    actor_user_id,
                    f'Actor joined or opened this workstream as {role}.',
                    _json_dumps({'role': role}),
                    f'claim:{actor_user_id}',
                    now,
                ),
            )
            conn.commit()
            row = conn.execute("SELECT * FROM workstreams WHERE id = ?", (workstream_id,)).fetchone()
            return self._hydrate(conn, self._row_to_workstream(row)) if row else None

    def add_event(
        self,
        workstream_id: str,
        *,
        actor_user_id: str,
        event_type: str = 'progress',
        title: Optional[str] = None,
        body: Optional[str] = None,
        status: Optional[str] = None,
        summary: Optional[str] = None,
        next_action: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        dedupe_key: Optional[str] = None,
        event_state: Optional[str] = None,
    ) -> WorkstreamEvent:
        event_type_clean = _normalize_choice(event_type, WORKSTREAM_EVENT_TYPES, 'progress')
        status_clean = _normalize_choice(status, WORKSTREAM_STATUSES, '') if status else None
        event_state_clean = _normalize_optional_choice(event_state, WORKSTREAM_EVENT_STATES)
        if not event_state_clean and event_type_clean == 'blocker':
            event_state_clean = 'open'
        metadata_clean = dict(metadata or {})
        if event_state_clean:
            metadata_clean['event_state'] = event_state_clean
        now = _now_iso()
        event_id = _new_id('Wse')
        with self.db.get_connection() as conn:
            if dedupe_key:
                existing = conn.execute(
                    "SELECT * FROM workstream_events WHERE workstream_id = ? AND dedupe_key = ?",
                    (workstream_id, dedupe_key),
                ).fetchone()
                if existing:
                    users = self._user_payloads(conn, [existing['actor_user_id']])
                    return WorkstreamEvent(
                        id=existing['id'],
                        workstream_id=existing['workstream_id'],
                        event_type=existing['event_type'],
                        actor_user_id=existing['actor_user_id'],
                        title=existing['title'],
                        body=existing['body'],
                        status=existing['status'],
                        metadata=_json_loads(existing['metadata_json']),
                        dedupe_key=existing['dedupe_key'],
                        created_at=str(existing['created_at'] or ''),
                        actor=users.get(existing['actor_user_id'], {}),
                    )
            conn.execute(
                """
                INSERT INTO workstream_events (id, workstream_id, event_type, actor_user_id, title, body, status, metadata_json, dedupe_key, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    workstream_id,
                    event_type_clean,
                    actor_user_id,
                    _coerce_text(title, limit=300),
                    _coerce_text(body, limit=16000),
                    status_clean,
                    _json_dumps(metadata_clean),
                    _coerce_text(dedupe_key, limit=200),
                    now,
                ),
            )
            update_bits = ["updated_at = ?", "updated_by = ?"]
            params: List[Any] = [now, actor_user_id]
            if status_clean:
                update_bits.append("status = ?")
                params.append(status_clean)
            if summary is not None:
                update_bits.append("summary = ?")
                params.append(_coerce_text(summary, limit=1600))
            if next_action is not None:
                update_bits.append("next_action = ?")
                params.append(_coerce_text(next_action, limit=1600))
            params.append(workstream_id)
            conn.execute(f"UPDATE workstreams SET {', '.join(update_bits)} WHERE id = ?", tuple(params))
            conn.commit()
            row = conn.execute("SELECT * FROM workstream_events WHERE id = ?", (event_id,)).fetchone()
            users = self._user_payloads(conn, [actor_user_id])
            return WorkstreamEvent(
                id=row['id'],
                workstream_id=row['workstream_id'],
                event_type=row['event_type'],
                actor_user_id=row['actor_user_id'],
                title=row['title'],
                body=row['body'],
                status=row['status'],
                metadata=_json_loads(row['metadata_json']),
                dedupe_key=row['dedupe_key'],
                created_at=str(row['created_at'] or ''),
                actor=users.get(actor_user_id, {}),
            )

    def add_artifact(self, workstream_id: str, *, actor_user_id: str, artifact: Dict[str, Any]) -> Optional[WorkstreamArtifact]:
        now = _now_iso()
        with self.db.get_connection() as conn:
            artifact_id = self._add_artifact_conn(conn, workstream_id, artifact, actor_user_id, now)
            if not artifact_id:
                return None
            conn.execute("UPDATE workstreams SET updated_at = ?, updated_by = ? WHERE id = ?", (now, actor_user_id, workstream_id))
            conn.execute(
                """
                INSERT INTO workstream_events (id, workstream_id, event_type, actor_user_id, title, body, status, metadata_json, dedupe_key, created_at)
                VALUES (?, ?, 'artifact', ?, ?, ?, NULL, ?, NULL, ?)
                """,
                (
                    _new_id('Wse'),
                    workstream_id,
                    actor_user_id,
                    _coerce_text(artifact.get('title'), limit=300) or 'Artifact added',
                    _coerce_text(artifact.get('summary'), limit=1200),
                    _json_dumps({'artifact_type': _infer_artifact_type(artifact), 'ref_id': _artifact_ref_id(artifact)}),
                    now,
                ),
            )
            conn.commit()
            row = conn.execute("SELECT * FROM workstream_artifacts WHERE id = ?", (artifact_id,)).fetchone()
            if not row:
                row = conn.execute(
                    """
                    SELECT * FROM workstream_artifacts
                    WHERE workstream_id = ? AND artifact_type = ? AND ref_id = ?
                    """,
                    (workstream_id, _infer_artifact_type(artifact), _artifact_ref_id(artifact)),
                ).fetchone()
            if not row:
                return None
            users = self._user_payloads(conn, [row['created_by']])
            return WorkstreamArtifact(
                id=row['id'],
                workstream_id=row['workstream_id'],
                artifact_type=row['artifact_type'],
                ref_id=row['ref_id'],
                title=row['title'],
                summary=row['summary'],
                metadata=_json_loads(row['metadata_json']),
                created_by=row['created_by'],
                created_at=str(row['created_at'] or ''),
                creator=users.get(row['created_by'], {}),
            )

    def to_agent_reference(self, workstream_id: str) -> Optional[Dict[str, Any]]:
        ws = self.get_workstream(workstream_id, event_limit=8)
        if not ws:
            return None
        return {
            'type': 'canopy_workstream_reference',
            'workstream_id': ws.id,
            'title': ws.title,
            'status': ws.status,
            'priority': ws.priority,
            'objective': ws.objective,
            'required_output': ws.required_output,
            'summary': ws.summary,
            'next_action': ws.next_action,
            'endpoints': {
                'get': f"/api/v1/workstreams/{ws.id}",
                'update': f"/api/v1/workstreams/{ws.id}",
                'events': f"/api/v1/workstreams/{ws.id}/events",
                'artifacts': f"/api/v1/workstreams/{ws.id}/artifacts",
                'participants': f"/api/v1/workstreams/{ws.id}/participants",
            },
            'participants': [p.to_dict() for p in ws.participants],
            'recent_events': [e.to_dict() for e in ws.events[:5]],
            'artifacts': [a.to_dict() for a in ws.artifacts[:20]],
        }
