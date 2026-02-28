"""
Circle (structured deliberation) helpers for Canopy.

Circles are created via a compact text format so humans and agents can
spin up structured discussions without new UI controls.
"""

from __future__ import annotations

import json
import logging
import re
import secrets
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict, Any, Tuple

from .database import DatabaseManager
from ..security.trust import TrustManager

logger = logging.getLogger('canopy.circles')

DEFAULT_OPINION_LIMIT = 1
DEFAULT_CLARIFY_LIMIT = 1
DEFAULT_EDIT_WINDOW_SECONDS = 10 * 60

_CIRCLE_BLOCK_PATTERNS = [
    re.compile(r"(?is)\[circle\](.*?)\[/circle\]"),
    re.compile(r"(?is)::circle\s*(.*?)\s*::endcircle"),
]

_CIRCLE_RESPONSE_PATTERNS = [
    re.compile(r"(?is)\[circle-response\](.*?)\[/circle-response\]"),
]

_BOOL_TRUE = {'true', 'yes', 'on', '1'}
_BOOL_FALSE = {'false', 'no', 'off', '0'}


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _parse_datetime(value: Any) -> Optional[datetime]:
    if not value:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except Exception:
        try:
            dt = datetime.strptime(raw, "%Y-%m-%d")
        except Exception:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _parse_duration_seconds(value: str) -> Optional[int]:
    if not value:
        return None
    raw = str(value).strip().lower()
    if not raw:
        return None
    if raw in {"none", "no", "never"}:
        return None
    m = re.match(r"^(\d+)\s*([a-z]+)?$", raw)
    if not m:
        return None
    value_num = int(m.group(1))
    unit = (m.group(2) or "s").strip()
    if unit in {"s", "sec", "secs", "second", "seconds"}:
        return value_num
    if unit in {"m", "min", "mins", "minute", "minutes"}:
        return value_num * 60
    if unit in {"h", "hr", "hrs", "hour", "hours"}:
        return value_num * 3600
    if unit in {"d", "day", "days"}:
        return value_num * 24 * 3600
    if unit in {"w", "wk", "wks", "week", "weeks"}:
        return value_num * 7 * 24 * 3600
    if unit in {"mo", "mon", "month", "months"}:
        return value_num * 30 * 24 * 3600
    return None


def _parse_int(value: str, default: Optional[int] = None) -> Optional[int]:
    if value is None:
        return default
    try:
        return int(str(value).strip())
    except Exception:
        return default


def _parse_bool(value: str, default: bool = False) -> bool:
    if value is None:
        return default
    raw = str(value).strip().lower()
    if raw in _BOOL_TRUE:
        return True
    if raw in _BOOL_FALSE:
        return False
    return default


@dataclass
class CircleSpec:
    topic: str
    description: Optional[str] = None
    facilitator: Optional[str] = None
    mode: str = 'open'  # open|fixed
    opinion_limit: int = DEFAULT_OPINION_LIMIT
    clarify_limit: int = DEFAULT_CLARIFY_LIMIT
    edit_window_seconds: int = DEFAULT_EDIT_WINDOW_SECONDS
    agents_policy: str = 'trusted'  # trusted|allow|deny
    decision_mode: str = 'facilitator'  # facilitator|vote
    duration_seconds: Optional[int] = None
    ends_at: Optional[datetime] = None
    options: Optional[List[str]] = None
    participants: Optional[List[str]] = None
    auto_tasks: bool = False
    circle_id: Optional[str] = None
    raw: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            'topic': self.topic,
            'description': self.description,
            'facilitator': self.facilitator,
            'mode': self.mode,
            'opinion_limit': self.opinion_limit,
            'clarify_limit': self.clarify_limit,
            'edit_window_seconds': self.edit_window_seconds,
            'agents_policy': self.agents_policy,
            'decision_mode': self.decision_mode,
            'duration_seconds': self.duration_seconds,
            'ends_at': self.ends_at.isoformat() if self.ends_at else None,
            'options': self.options or [],
            'participants': self.participants or [],
            'auto_tasks': self.auto_tasks,
            'circle_id': self.circle_id,
        }


@dataclass
class Circle:
    id: str
    source_type: str
    source_id: str
    channel_id: Optional[str]
    topic: str
    description: Optional[str]
    facilitator_id: Optional[str]
    mode: str
    opinion_limit: int
    clarify_limit: int
    edit_window_seconds: int
    agents_policy: str
    decision_mode: str
    auto_tasks: bool
    options: Optional[List[str]]
    participants: Optional[List[str]]
    phase: str
    summary: Optional[str]
    decision: Optional[str]
    created_by: str
    created_at: datetime
    updated_at: datetime
    ends_at: Optional[datetime]
    visibility: str
    origin_peer: Optional[str]
    round_number: int

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data['created_at'] = self.created_at.isoformat() if self.created_at else None
        data['updated_at'] = self.updated_at.isoformat() if self.updated_at else None
        data['ends_at'] = self.ends_at.isoformat() if self.ends_at else None
        return data


def derive_circle_id(source_type: str, source_id: str, index: int = 0, total: int = 1,
                     override: Optional[str] = None) -> str:
    if override:
        cleaned = override.strip()
        if cleaned:
            return cleaned if cleaned.startswith('circle_') else f"circle_{cleaned}"
    base = f"circle_{source_type}_{source_id}"
    if total > 1:
        return f"{base}_{index + 1}"
    return base


def _mask_code_fences(text: str) -> str:
    """Replace code fence contents with placeholders so embedded [circle] blocks are ignored."""
    return re.sub(r"```.*?```", lambda m: "\x00" * len(m.group(0)), text, flags=re.S)


def parse_circle_blocks(text: str) -> List[CircleSpec]:
    if not text:
        return []

    masked = _mask_code_fences(text)

    specs: List[CircleSpec] = []
    for pattern in _CIRCLE_BLOCK_PATTERNS:
        for match in pattern.finditer(masked):
            # Use original text for the matched region (masked text was used for matching only)
            block = text[match.start(1):match.end(1)] if match.group(1) else (text[match.start(2):match.end(2)] if match.lastindex and match.lastindex >= 2 and match.group(2) else '')
            raw_match = text[match.start():match.end()]
            topic = None
            description_lines: List[str] = []
            facilitator = None
            mode = 'open'
            opinion_limit = DEFAULT_OPINION_LIMIT
            clarify_limit = DEFAULT_CLARIFY_LIMIT
            edit_window_seconds = DEFAULT_EDIT_WINDOW_SECONDS
            agents_policy = 'trusted'
            decision_mode = 'facilitator'
            duration_seconds = None
            ends_at = None
            options: List[str] = []
            participants: List[str] = []
            auto_tasks = False
            circle_id = None
            reading_options = False

            for line in block.splitlines():
                stripped = line.strip()
                if not stripped:
                    continue
                lower = stripped.lower()

                if lower.startswith(('topic:', 'title:')):
                    topic = stripped.split(':', 1)[1].strip() or topic
                    reading_options = False
                    continue
                if lower.startswith(('description:', 'desc:', 'details:')):
                    desc = stripped.split(':', 1)[1].strip()
                    if desc:
                        description_lines.append(desc)
                    reading_options = False
                    continue
                if lower.startswith(('facilitator:', 'leader:')):
                    facilitator = stripped.split(':', 1)[1].strip()
                    reading_options = False
                    continue
                if lower.startswith(('mode:', 'access:')):
                    mode_val = stripped.split(':', 1)[1].strip().lower()
                    mode = 'fixed' if mode_val in ('fixed', 'invite', 'invited', 'closed') else 'open'
                    reading_options = False
                    continue
                if lower.startswith(('participants:', 'invited:')):
                    raw = stripped.split(':', 1)[1].strip()
                    participants = []
                    for token in re.split(r"[,\s]+", raw):
                        t = token.strip()
                        if not t:
                            continue
                        if t.startswith('@'):
                            t = t[1:]
                        if t:
                            participants.append(t)
                    reading_options = False
                    continue
                if lower.startswith(('opinion_limit:', 'opinions:')):
                    opinion_limit = _parse_int(stripped.split(':', 1)[1], DEFAULT_OPINION_LIMIT) or DEFAULT_OPINION_LIMIT
                    reading_options = False
                    continue
                if lower.startswith(('clarify_limit:', 'clarifications:', 'questions:')):
                    clarify_limit = _parse_int(stripped.split(':', 1)[1], DEFAULT_CLARIFY_LIMIT) or DEFAULT_CLARIFY_LIMIT
                    reading_options = False
                    continue
                if lower.startswith(('edit_window:', 'edit_window_seconds:', 'edit:')):
                    raw = stripped.split(':', 1)[1].strip()
                    edit_window_seconds = _parse_duration_seconds(raw) or _parse_int(raw, DEFAULT_EDIT_WINDOW_SECONDS) or DEFAULT_EDIT_WINDOW_SECONDS
                    reading_options = False
                    continue
                if lower.startswith(('agents:', 'agent_policy:')):
                    policy = stripped.split(':', 1)[1].strip().lower()
                    if policy in ('allow', 'trusted', 'deny'):
                        agents_policy = policy
                    reading_options = False
                    continue
                if lower.startswith(('decision:', 'decision_mode:')):
                    decision_mode_raw = stripped.split(':', 1)[1].strip().lower()
                    decision_mode = 'vote' if decision_mode_raw in ('vote', 'poll') else 'facilitator'
                    reading_options = False
                    continue
                if lower.startswith(('duration:', 'ttl:', 'expires:', 'ends:')):
                    key, val = (stripped.split(':', 1) + [""])[:2]
                    key = key.strip().lower()
                    val = val.strip()
                    if key in {"expires", "ends"}:
                        ends_at = _parse_datetime(val)
                    else:
                        duration_seconds = _parse_duration_seconds(val)
                    reading_options = False
                    continue
                if lower.startswith(('options:', 'choices:')):
                    reading_options = True
                    continue
                if lower.startswith(('auto_tasks:', 'tasks:')):
                    auto_tasks = _parse_bool(stripped.split(':', 1)[1], False)
                    reading_options = False
                    continue
                if lower.startswith(('id:', 'circle_id:')):
                    circle_id = stripped.split(':', 1)[1].strip()
                    reading_options = False
                    continue

                if stripped[0] in {"-", "*", "\u2022"}:
                    option = stripped.lstrip("-* \u2022").strip()
                    if option:
                        options.append(option)
                    continue

                if reading_options:
                    continue

                if topic is None:
                    topic = stripped
                else:
                    description_lines.append(stripped)

            if not topic:
                continue

            spec = CircleSpec(
                topic=topic,
                description="\n".join(description_lines).strip() or None,
                facilitator=facilitator,
                mode=mode,
                opinion_limit=opinion_limit,
                clarify_limit=clarify_limit,
                edit_window_seconds=edit_window_seconds,
                agents_policy=agents_policy,
                decision_mode=decision_mode,
                duration_seconds=duration_seconds,
                ends_at=ends_at,
                options=options or None,
                participants=participants or None,
                auto_tasks=auto_tasks,
                circle_id=circle_id or None,
                raw=raw_match,
            )
            specs.append(spec)

    return specs


def parse_circle_response_blocks(text: str) -> List[Dict[str, str]]:
    """Extract [circle-response] blocks with topic + content payloads."""
    if not text:
        return []

    masked = _mask_code_fences(text)
    responses: List[Dict[str, str]] = []

    for pattern in _CIRCLE_RESPONSE_PATTERNS:
        for match in pattern.finditer(masked):
            block = text[match.start(1):match.end(1)] if match.group(1) else ''
            lines = [line.rstrip() for line in (block or '').strip().splitlines()]
            topic = None
            content_lines: List[str] = []
            in_content = False
            for line in lines:
                stripped = line.strip()
                lower = stripped.lower()
                if not in_content and lower.startswith('topic:'):
                    topic = stripped.split(':', 1)[1].strip()
                    continue
                if not in_content and lower.startswith('content:'):
                    in_content = True
                    content_lines.append(line.split(':', 1)[1].lstrip())
                    continue
                if in_content:
                    content_lines.append(line)
                else:
                    if stripped:
                        content_lines.append(line)
            content = "\n".join([line for line in content_lines if line is not None]).strip()
            if topic and content:
                responses.append({'topic': topic, 'content': content})

    return responses


def strip_circle_blocks(text: str) -> str:
    if not text:
        return text

    code_ranges = []
    for m in re.finditer(r"```.*?```", text, re.S):
        code_ranges.append((m.start(), m.end()))

    def _in_code_fence(start: int, end: int) -> bool:
        for cs, ce in code_ranges:
            if start >= cs and end <= ce:
                return True
        return False

    pattern = re.compile(r"(?is)\[circle\](.*?)\[/circle\]|::circle\s*(.*?)\s*::endcircle")

    def _replace(match):
        if _in_code_fence(match.start(), match.end()):
            return match.group(0)
        return ''

    cleaned_text = pattern.sub(_replace, text)
    return cleaned_text.strip()


class CircleManager:
    def __init__(self, db: DatabaseManager,
                 trust_manager: Optional[TrustManager] = None,
                 task_manager: Optional[Any] = None):
        self.db = db
        self.trust_manager = trust_manager
        self.task_manager = task_manager
        self._ensure_tables()

    def _ensure_tables(self) -> None:
        try:
            with self.db.get_connection() as conn:
                conn.executescript("""
                CREATE TABLE IF NOT EXISTS circles (
                    id TEXT PRIMARY KEY,
                    source_type TEXT,
                    source_id TEXT,
                    channel_id TEXT,
                    topic TEXT NOT NULL,
                    description TEXT,
                    facilitator_id TEXT,
                    mode TEXT DEFAULT 'open',
                    opinion_limit INTEGER DEFAULT 1,
                    clarify_limit INTEGER DEFAULT 1,
                    edit_window_seconds INTEGER DEFAULT 600,
                    agents_policy TEXT DEFAULT 'trusted',
                    decision_mode TEXT DEFAULT 'facilitator',
                    auto_tasks BOOLEAN DEFAULT 0,
                    options TEXT,
                    participants TEXT,
                    phase TEXT DEFAULT 'opinion',
                    summary TEXT,
                    decision TEXT,
                    created_by TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    ends_at TIMESTAMP,
                    visibility TEXT DEFAULT 'network',
                    origin_peer TEXT,
                    round_number INTEGER DEFAULT 1
                );

                CREATE TABLE IF NOT EXISTS circle_entries (
                    id TEXT PRIMARY KEY,
                    circle_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    entry_type TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    edited_at TIMESTAMP,
                    metadata TEXT,
                    round_number INTEGER DEFAULT 1,
                    FOREIGN KEY (circle_id) REFERENCES circles (id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS circle_votes (
                    circle_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    option_index INTEGER NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (circle_id, user_id),
                    FOREIGN KEY (circle_id) REFERENCES circles (id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_circle_entries_circle ON circle_entries(circle_id);
                CREATE INDEX IF NOT EXISTS idx_circle_entries_user ON circle_entries(user_id);
                """)

                # Migration: add round_number columns if missing
                try:
                    conn.execute("SELECT round_number FROM circles LIMIT 1")
                except Exception:
                    conn.execute("ALTER TABLE circles ADD COLUMN round_number INTEGER DEFAULT 1")
                    conn.execute("UPDATE circles SET round_number = 1 WHERE round_number IS NULL")
                    logger.info("Added round_number column to circles table")

                try:
                    conn.execute("SELECT round_number FROM circle_entries LIMIT 1")
                except Exception:
                    conn.execute("ALTER TABLE circle_entries ADD COLUMN round_number INTEGER DEFAULT 1")
                    conn.execute("UPDATE circle_entries SET round_number = 1 WHERE round_number IS NULL")
                    logger.info("Added round_number column to circle_entries table")
                conn.commit()
        except Exception as e:
            logger.error(f"Failed to ensure circle tables: {e}", exc_info=True)
            raise

    def _row_to_circle(self, row: Any) -> Circle:
        options = None
        participants = None
        if row['options']:
            try:
                options = json.loads(row['options'])
            except Exception:
                options = None
        if row['participants']:
            try:
                participants = json.loads(row['participants'])
            except Exception:
                participants = None
        return Circle(
            id=row['id'],
            source_type=row['source_type'],
            source_id=row['source_id'],
            channel_id=row['channel_id'],
            topic=row['topic'],
            description=row['description'],
            facilitator_id=row['facilitator_id'],
            mode=row['mode'] or 'open',
            opinion_limit=row['opinion_limit'] or DEFAULT_OPINION_LIMIT,
            clarify_limit=row['clarify_limit'] or DEFAULT_CLARIFY_LIMIT,
            edit_window_seconds=row['edit_window_seconds'] or DEFAULT_EDIT_WINDOW_SECONDS,
            agents_policy=row['agents_policy'] or 'trusted',
            decision_mode=row['decision_mode'] or 'facilitator',
            auto_tasks=bool(row['auto_tasks']),
            options=options,
            participants=participants,
            phase=row['phase'] or 'opinion',
            summary=row['summary'],
            decision=row['decision'],
            created_by=row['created_by'],
            created_at=_parse_datetime(row['created_at']) or _now_utc(),
            updated_at=_parse_datetime(row['updated_at']) or _now_utc(),
            ends_at=_parse_datetime(row['ends_at']),
            visibility=row['visibility'] or 'network',
            origin_peer=row['origin_peer'],
            round_number=int(row['round_number']) if 'round_number' in row.keys() and row['round_number'] is not None else 1,
        )

    def get_circle(self, circle_id: str) -> Optional[Circle]:
        if not circle_id:
            return None
        try:
            with self.db.get_connection() as conn:
                row = conn.execute("SELECT * FROM circles WHERE id = ?", (circle_id,)).fetchone()
            if not row:
                return None
            return self._row_to_circle(row)
        except Exception as e:
            logger.error(f"Failed to get circle {circle_id}: {e}")
            return None

    def list_circles(self, limit: int = 50, source_type: Optional[str] = None,
                     channel_id: Optional[str] = None) -> List[Circle]:
        try:
            query = "SELECT * FROM circles"
            params: List[Any] = []
            clauses = []
            if source_type:
                clauses.append("source_type = ?")
                params.append(source_type)
            if channel_id:
                clauses.append("channel_id = ?")
                params.append(channel_id)
            if clauses:
                query += " WHERE " + " AND ".join(clauses)
            query += " ORDER BY updated_at DESC LIMIT ?"
            params.append(limit)
            with self.db.get_connection() as conn:
                rows = conn.execute(query, params).fetchall()
            return [self._row_to_circle(r) for r in rows]
        except Exception as e:
            logger.error(f"Failed to list circles: {e}")
            return []

    def list_circles_since(self, since: Any, limit: int = 50) -> List[Circle]:
        """List circles updated after a timestamp."""
        since_dt = _parse_datetime(since) if since else None
        if not since_dt:
            return []
        since_iso = since_dt.isoformat()
        try:
            limit_val = max(1, min(int(limit or 50), 200))
        except Exception:
            limit_val = 50

        try:
            with self.db.get_connection() as conn:
                rows = conn.execute(
                    "SELECT * FROM circles WHERE updated_at > ? ORDER BY updated_at DESC LIMIT ?",
                    (since_iso, limit_val),
                ).fetchall()
            return [self._row_to_circle(r) for r in rows]
        except Exception as e:
            logger.error(f"Failed to list circles since {since_iso}: {e}")
            return []

    def find_circle_by_topic(self, topic: str, channel_id: Optional[str] = None) -> Optional[Circle]:
        """Find the most recently updated circle matching a topic (case-insensitive)."""
        if not topic:
            return None
        topic_key = topic.strip().lower()
        if not topic_key:
            return None
        try:
            with self.db.get_connection() as conn:
                if channel_id:
                    row = conn.execute(
                        "SELECT * FROM circles WHERE LOWER(topic) = ? AND channel_id = ? "
                        "ORDER BY updated_at DESC LIMIT 1",
                        (topic_key, channel_id),
                    ).fetchone()
                else:
                    row = conn.execute(
                        "SELECT * FROM circles WHERE LOWER(topic) = ? "
                        "ORDER BY updated_at DESC LIMIT 1",
                        (topic_key,),
                    ).fetchone()
            return self._row_to_circle(row) if row else None
        except Exception as e:
            logger.error(f"Failed to find circle by topic: {e}")
            return None

    def get_entry_counts_since(self, since_timestamp: str,
                               circle_ids: Optional[List[str]] = None) -> Dict[str, int]:
        """Return entry counts per circle since a timestamp."""
        if not since_timestamp:
            return {}
        try:
            params: List[Any] = [since_timestamp]
            query = "SELECT circle_id, COUNT(*) AS n FROM circle_entries WHERE created_at > ?"
            if circle_ids:
                placeholders = ",".join("?" for _ in circle_ids)
                query += f" AND circle_id IN ({placeholders})"
                params.extend(circle_ids)
            query += " GROUP BY circle_id"
            with self.db.get_connection() as conn:
                rows = conn.execute(query, params).fetchall()
            counts: Dict[str, int] = {}
            for row in rows:
                counts[str(row['circle_id'])] = int(row['n'])
            return counts
        except Exception as e:
            logger.error(f"Failed to get circle entry counts since {since_timestamp}: {e}")
            return {}

    def upsert_circle(self,
                      circle_id: str,
                      source_type: str,
                      source_id: str,
                      created_by: str,
                      spec: CircleSpec,
                      channel_id: Optional[str] = None,
                      facilitator_id: Optional[str] = None,
                      visibility: str = 'network',
                      origin_peer: Optional[str] = None,
                      created_at: Optional[Any] = None) -> Optional[Circle]:
        if not circle_id or not spec or not spec.topic:
            return None
        now_dt = _now_utc()
        created_dt = _parse_datetime(created_at) or now_dt
        ends_at = spec.ends_at
        if not ends_at and spec.duration_seconds:
            ends_at = created_dt + timedelta(seconds=spec.duration_seconds)
        try:
            with self.db.get_connection() as conn:
                row = conn.execute("SELECT * FROM circles WHERE id = ?", (circle_id,)).fetchone()
                if row:
                    # Update spec fields without resetting phase/outcome.
                    conn.execute("""
                        UPDATE circles
                        SET topic = ?, description = ?, facilitator_id = ?,
                            mode = ?, opinion_limit = ?, clarify_limit = ?, edit_window_seconds = ?,
                            agents_policy = ?, decision_mode = ?, auto_tasks = ?,
                            options = ?, participants = ?, ends_at = ?, visibility = ?,
                            updated_at = ?
                        WHERE id = ?
                    """, (
                        spec.topic,
                        spec.description,
                        facilitator_id or row['facilitator_id'],
                        spec.mode,
                        spec.opinion_limit,
                        spec.clarify_limit,
                        spec.edit_window_seconds,
                        spec.agents_policy,
                        spec.decision_mode,
                        1 if spec.auto_tasks else 0,
                        json.dumps(spec.options or []),
                        json.dumps(spec.participants or []),
                        ends_at.isoformat() if ends_at else None,
                        # Never downgrade visibility from 'network' to 'local'
                        'network' if (row['visibility'] == 'network' or visibility == 'network') else (visibility or row['visibility']),
                        now_dt.isoformat(),
                        circle_id
                    ))
                else:
                    conn.execute("""
                        INSERT INTO circles
                        (id, source_type, source_id, channel_id, topic, description,
                         facilitator_id, mode, opinion_limit, clarify_limit, edit_window_seconds,
                         agents_policy, decision_mode, auto_tasks, options, participants, phase,
                         summary, decision, created_by, created_at, updated_at, ends_at, visibility, origin_peer, round_number)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'opinion', NULL, NULL, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        circle_id, source_type, source_id, channel_id,
                        spec.topic, spec.description,
                        facilitator_id, spec.mode, spec.opinion_limit, spec.clarify_limit,
                        spec.edit_window_seconds, spec.agents_policy, spec.decision_mode,
                        1 if spec.auto_tasks else 0,
                        json.dumps(spec.options or []),
                        json.dumps(spec.participants or []),
                        created_by,
                        created_dt.isoformat(),
                        now_dt.isoformat(),
                        ends_at.isoformat() if ends_at else None,
                        visibility,
                        origin_peer,
                        1,
                    ))
                conn.commit()
            return self.get_circle(circle_id)
        except Exception as e:
            logger.error(f"Failed to upsert circle {circle_id}: {e}", exc_info=True)
            return None

    def _user_account_type(self, user_id: str) -> str:
        try:
            row = self.db.get_user(user_id)
            if row:
                return (row.get('account_type') or 'human').lower()
        except Exception:
            pass
        return 'human'

    def _user_origin_peer(self, user_id: str) -> Optional[str]:
        try:
            row = self.db.get_user(user_id)
            if row:
                return row.get('origin_peer')
        except Exception:
            pass
        return None

    def _agent_allowed(self, circle: Circle, user_id: str) -> bool:
        account_type = self._user_account_type(user_id)
        if account_type != 'agent':
            return True
        policy = (circle.agents_policy or 'trusted').lower()
        if policy == 'allow':
            return True
        if policy == 'deny':
            return False
        # trusted: require trusted origin peer if available
        origin_peer = self._user_origin_peer(user_id)
        if not origin_peer:
            return True
        if not self.trust_manager:
            return True
        score = self.trust_manager.get_trust_score(origin_peer)
        return score >= 50

    def _is_participant(self, circle: Circle, user_id: str) -> bool:
        if not circle:
            return False
        if user_id == circle.facilitator_id or user_id == circle.created_by:
            return True
        if circle.channel_id:
            try:
                with self.db.get_connection() as conn:
                    row = conn.execute(
                        "SELECT 1 FROM channel_members WHERE channel_id = ? AND user_id = ?",
                        (circle.channel_id, user_id)
                    ).fetchone()
                if not row:
                    return False
            except Exception:
                return False
        if circle.mode == 'open':
            return True
        participants = circle.participants or []
        return user_id in participants

    def list_entries(self, circle_id: str) -> List[Dict[str, Any]]:
        if not circle_id:
            return []
        try:
            with self.db.get_connection() as conn:
                rows = conn.execute(
                    "SELECT * FROM circle_entries WHERE circle_id = ? ORDER BY created_at ASC",
                    (circle_id,)
                ).fetchall()
            results = []
            for row in rows:
                meta = None
                if row['metadata']:
                    try:
                        meta = json.loads(row['metadata'])
                    except Exception:
                        meta = None
                results.append({
                    'id': row['id'],
                    'circle_id': row['circle_id'],
                    'user_id': row['user_id'],
                    'entry_type': row['entry_type'],
                    'content': row['content'],
                    'created_at': row['created_at'],
                    'edited_at': row['edited_at'],
                    'round_number': int(row['round_number']) if 'round_number' in row.keys() and row['round_number'] is not None else 1,
                    'metadata': meta,
                })
            return results
        except Exception as e:
            logger.error(f"Failed to list circle entries: {e}")
            return []

    def get_entries_since(self, since_timestamp: str, limit: int = 500) -> List[Dict[str, Any]]:
        """Return all circle entries created after *since_timestamp*.

        Used by the P2P catch-up mechanism to send missed entries to a
        reconnecting peer.
        """
        try:
            with self.db.get_connection() as conn:
                rows = conn.execute(
                    "SELECT * FROM circle_entries WHERE created_at > ? "
                    "ORDER BY created_at ASC LIMIT ?",
                    (since_timestamp, limit)
                ).fetchall()
            results = []
            for row in rows:
                results.append({
                    'id': row['id'],
                    'circle_id': row['circle_id'],
                    'user_id': row['user_id'],
                    'entry_type': row['entry_type'],
                    'content': row['content'],
                    'created_at': row['created_at'],
                    'edited_at': row['edited_at'],
                    'round_number': int(row['round_number']) if 'round_number' in row.keys() and row['round_number'] is not None else 1,
                })
            return results
        except Exception as e:
            logger.error(f"Failed to get entries since {since_timestamp}: {e}")
            return []

    def get_entries_latest_timestamp(self) -> Optional[str]:
        """Return the created_at of the most recent circle entry, or None."""
        try:
            with self.db.get_connection() as conn:
                row = conn.execute(
                    "SELECT MAX(created_at) AS latest FROM circle_entries"
                ).fetchone()
            return row['latest'] if row and row['latest'] else None
        except Exception:
            return None

    def get_votes_since(self, since_timestamp: str, limit: int = 500) -> List[Dict[str, Any]]:
        """Return all circle votes created after *since_timestamp*."""
        try:
            with self.db.get_connection() as conn:
                rows = conn.execute(
                    "SELECT * FROM circle_votes WHERE created_at > ? "
                    "ORDER BY created_at ASC LIMIT ?",
                    (since_timestamp, limit)
                ).fetchall()
            return [dict(row) for row in rows]
        except Exception as e:
            logger.error(f"Failed to get votes since {since_timestamp}: {e}")
            return []

    def get_votes_latest_timestamp(self) -> Optional[str]:
        """Return the created_at of the most recent circle vote, or None."""
        try:
            with self.db.get_connection() as conn:
                row = conn.execute(
                    "SELECT MAX(created_at) AS latest FROM circle_votes"
                ).fetchone()
            return row['latest'] if row and row['latest'] else None
        except Exception:
            return None

    # --- Circle object sync for catchup (v0.3.55) ---

    def get_circles_latest_timestamp(self) -> Optional[str]:
        """Return the updated_at of the most recently modified circle, or None."""
        try:
            with self.db.get_connection() as conn:
                row = conn.execute(
                    "SELECT MAX(updated_at) AS latest FROM circles"
                ).fetchone()
            return row['latest'] if row and row['latest'] else None
        except Exception:
            return None

    def get_circles_since(self, since_timestamp: str, limit: int = 200) -> List[Dict[str, Any]]:
        """Return circle dicts updated after *since_timestamp* for catchup sync."""
        try:
            with self.db.get_connection() as conn:
                rows = conn.execute(
                    "SELECT * FROM circles WHERE updated_at > ? "
                    "ORDER BY updated_at ASC LIMIT ?",
                    (since_timestamp, limit)
                ).fetchall()
            results = []
            for row in rows:
                c = self._row_to_circle(row)
                if c:
                    results.append(c.to_dict())
            return results
        except Exception as e:
            logger.error(f"Failed to get circles since {since_timestamp}: {e}")
            return []

    def ingest_circle_snapshot(self, data: Dict[str, Any]) -> bool:
        """Ingest a circle object from catchup data (INSERT or update)."""
        circle_id = data.get('id')
        if not circle_id or not data.get('topic'):
            return False
        try:
            with self.db.get_connection() as conn:
                existing = conn.execute(
                    "SELECT updated_at FROM circles WHERE id = ?", (circle_id,)
                ).fetchone()

                if existing:
                    # Only update if incoming is newer
                    existing_ts = existing['updated_at'] or ''
                    incoming_ts = data.get('updated_at') or ''
                    if incoming_ts <= existing_ts:
                        return False
                    conn.execute("""
                        UPDATE circles
                        SET topic = ?, description = ?, facilitator_id = ?,
                            mode = ?, opinion_limit = ?, clarify_limit = ?,
                            edit_window_seconds = ?, agents_policy = ?,
                            decision_mode = ?, auto_tasks = ?, options = ?,
                            participants = ?, phase = ?, summary = ?,
                            decision = ?, ends_at = ?, visibility = ?,
                            origin_peer = ?, round_number = ?, updated_at = ?
                        WHERE id = ?
                    """, (
                        data.get('topic'),
                        data.get('description'),
                        data.get('facilitator_id'),
                        data.get('mode', 'open'),
                        data.get('opinion_limit', 1),
                        data.get('clarify_limit', 1),
                        data.get('edit_window_seconds', 600),
                        data.get('agents_policy', 'trusted'),
                        data.get('decision_mode', 'facilitator'),
                        1 if data.get('auto_tasks') else 0,
                        json.dumps(data.get('options') or []),
                        json.dumps(data.get('participants') or []),
                        data.get('phase', 'opinion'),
                        data.get('summary'),
                        data.get('decision'),
                        data.get('ends_at'),
                        data.get('visibility', 'network'),
                        data.get('origin_peer'),
                        data.get('round_number', 1),
                        data.get('updated_at'),
                        circle_id,
                    ))
                else:
                    conn.execute("""
                        INSERT INTO circles
                        (id, source_type, source_id, channel_id, topic,
                         description, facilitator_id, mode, opinion_limit,
                         clarify_limit, edit_window_seconds, agents_policy,
                         decision_mode, auto_tasks, options, participants,
                         phase, summary, decision, created_by,
                         created_at, updated_at, ends_at, visibility,
                         origin_peer, round_number)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        circle_id,
                        data.get('source_type', 'channel'),
                        data.get('source_id', ''),
                        data.get('channel_id'),
                        data.get('topic'),
                        data.get('description'),
                        data.get('facilitator_id'),
                        data.get('mode', 'open'),
                        data.get('opinion_limit', 1),
                        data.get('clarify_limit', 1),
                        data.get('edit_window_seconds', 600),
                        data.get('agents_policy', 'trusted'),
                        data.get('decision_mode', 'facilitator'),
                        1 if data.get('auto_tasks') else 0,
                        json.dumps(data.get('options') or []),
                        json.dumps(data.get('participants') or []),
                        data.get('phase', 'opinion'),
                        data.get('summary'),
                        data.get('decision'),
                        data.get('created_by', ''),
                        data.get('created_at'),
                        data.get('updated_at'),
                        data.get('ends_at'),
                        data.get('visibility', 'network'),
                        data.get('origin_peer'),
                        data.get('round_number', 1),
                    ))
                conn.commit()
            logger.info(f"Ingested circle snapshot: {circle_id}")
            return True
        except Exception as e:
            logger.error(f"Failed to ingest circle snapshot {circle_id}: {e}")
            return False

    def count_entries(self, circle_id: str, user_id: Optional[str] = None,
                      entry_type: Optional[str] = None,
                      round_number: Optional[int] = None) -> int:
        if not circle_id:
            return 0
        try:
            query = "SELECT COUNT(*) AS n FROM circle_entries WHERE circle_id = ?"
            params: List[Any] = [circle_id]
            if user_id:
                query += " AND user_id = ?"
                params.append(user_id)
            if entry_type:
                query += " AND entry_type = ?"
                params.append(entry_type)
            if round_number is not None:
                query += " AND round_number = ?"
                params.append(int(round_number))
            with self.db.get_connection() as conn:
                row = conn.execute(query, params).fetchone()
            return int(row['n']) if row else 0
        except Exception:
            return 0

    def add_entry(self, circle_id: str, user_id: str, entry_type: str,
                  content: str, admin_user_id: Optional[str] = None,
                  return_error: bool = False) -> Optional[Dict[str, Any]] | Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
        circle = self.get_circle(circle_id)
        if not circle or not user_id:
            if return_error:
                return None, {'code': 'not_found', 'message': 'Circle not found', 'status': 404}
            return None
        entry_type = (entry_type or '').strip().lower()
        if entry_type not in ('opinion', 'clarify', 'summary', 'decision'):
            if return_error:
                return None, {'code': 'invalid_entry_type', 'message': 'Invalid entry type', 'status': 400}
            return None

        def _fail(
            code: str,
            message: str,
            status: int,
            **extra: Any,
        ) -> Optional[Dict[str, Any]] | Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
            if return_error:
                payload = {'code': code, 'message': message, 'status': status}
                payload.update(extra)
                return None, payload
            return None

        if not self._is_participant(circle, user_id):
            return _fail('not_participant', 'User is not a circle participant', 403)
        if not self._agent_allowed(circle, user_id):
            return _fail('agent_not_allowed', 'Agent policy does not allow participation', 403)

        now_dt = _now_utc()
        is_facilitator = (user_id == circle.facilitator_id) or (admin_user_id and user_id == admin_user_id)

        if entry_type in ('summary', 'decision') and not is_facilitator:
            return _fail('not_facilitator', 'Only the facilitator can post this entry type', 403)

        round_number = circle.round_number or 1
        if entry_type == 'opinion':
            if circle.phase != 'opinion':
                return _fail(
                    'phase_mismatch',
                    'Circle is not in the opinion phase',
                    409,
                    expected_phase='opinion',
                    phase=circle.phase,
                    round_number=round_number,
                )
            used = self.count_entries(circle_id, user_id, 'opinion', round_number=round_number)
            if used >= circle.opinion_limit:
                return _fail(
                    'opinion_limit',
                    'Opinion limit reached for this round',
                    429,
                    limit=circle.opinion_limit,
                    count=used,
                    round_number=round_number,
                    phase=circle.phase,
                    suggestions=[
                        'Wait for the facilitator to open another opinion round',
                        'Post during clarify/synthesis if appropriate',
                    ],
                )
        if entry_type == 'clarify':
            if circle.phase != 'clarify':
                return _fail(
                    'phase_mismatch',
                    'Circle is not in the clarify phase',
                    409,
                    expected_phase='clarify',
                    phase=circle.phase,
                    round_number=round_number,
                )
            used = self.count_entries(circle_id, user_id, 'clarify')
            if used >= circle.clarify_limit:
                return _fail(
                    'clarify_limit',
                    'Clarify limit reached',
                    429,
                    limit=circle.clarify_limit,
                    count=used,
                    round_number=round_number,
                    phase=circle.phase,
                )
        if entry_type == 'summary':
            if circle.phase not in ('synthesis', 'decision'):
                return _fail(
                    'phase_mismatch',
                    'Circle is not in synthesis or decision phase',
                    409,
                    expected_phase='synthesis',
                    phase=circle.phase,
                    round_number=round_number,
                )
        if entry_type == 'decision':
            if circle.phase not in ('decision',):
                return _fail(
                    'phase_mismatch',
                    'Circle is not in decision phase',
                    409,
                    expected_phase='decision',
                    phase=circle.phase,
                    round_number=round_number,
                )

        entry_id = f"ce_{secrets.token_hex(8)}"
        try:
            with self.db.get_connection() as conn:
                conn.execute("""
                    INSERT INTO circle_entries (id, circle_id, user_id, entry_type, content, created_at, round_number)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (entry_id, circle_id, user_id, entry_type, content, now_dt.isoformat(), round_number))
                if entry_type == 'summary':
                    conn.execute("UPDATE circles SET summary = ?, updated_at = ? WHERE id = ?",
                                 (content, now_dt.isoformat(), circle_id))
                if entry_type == 'decision':
                    conn.execute("UPDATE circles SET decision = ?, updated_at = ? WHERE id = ?",
                                 (content, now_dt.isoformat(), circle_id))
                conn.commit()
            if entry_type == 'decision' and circle.auto_tasks and self.task_manager:
                try:
                    task_id = f"task_circle_{circle_id}"
                    existing = self.task_manager.get_task(task_id)
                    if not existing:
                        self.task_manager.create_task(
                            task_id=task_id,
                            title=f"Circle: {circle.topic}",
                            description=content,
                            created_by=user_id,
                            visibility=circle.visibility,
                            metadata={
                                'circle_id': circle_id,
                                'auto_task': True,
                                'source_type': 'circle_decision',
                                'source_id': entry_id,
                            },
                            origin_peer=circle.origin_peer,
                            source_type='human',
                            updated_by=user_id,
                        )
                except Exception as task_err:
                    logger.warning(f"Circle auto-task creation failed: {task_err}")
            entry_payload = {
                'id': entry_id,
                'circle_id': circle_id,
                'user_id': user_id,
                'entry_type': entry_type,
                'content': content,
                'created_at': now_dt.isoformat(),
                'edited_at': None,
                'round_number': round_number,
            }
            if return_error:
                return entry_payload, None
            return entry_payload
        except Exception as e:
            logger.error(f"Failed to add circle entry: {e}", exc_info=True)
            if return_error:
                return None, {'code': 'internal_error', 'message': 'Failed to add entry', 'status': 500}
            return None

    def update_entry(self, circle_id: str, entry_id: str, user_id: str,
                     content: str, admin_user_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        circle = self.get_circle(circle_id)
        if not circle:
            return None
        now_dt = _now_utc()
        try:
            with self.db.get_connection() as conn:
                row = conn.execute(
                    "SELECT * FROM circle_entries WHERE id = ? AND circle_id = ?",
                    (entry_id, circle_id)
                ).fetchone()
                if not row:
                    return None
                if row['user_id'] != user_id and not (admin_user_id and user_id == admin_user_id):
                    return None
                created_dt = _parse_datetime(row['created_at']) or now_dt
                if circle.edit_window_seconds > 0:
                    if now_dt > created_dt + timedelta(seconds=circle.edit_window_seconds):
                        return None
                conn.execute("""
                    UPDATE circle_entries
                    SET content = ?, edited_at = ?
                    WHERE id = ? AND circle_id = ?
                """, (content, now_dt.isoformat(), entry_id, circle_id))
                if row['entry_type'] == 'summary':
                    conn.execute("UPDATE circles SET summary = ?, updated_at = ? WHERE id = ?",
                                 (content, now_dt.isoformat(), circle_id))
                if row['entry_type'] == 'decision':
                    conn.execute("UPDATE circles SET decision = ?, updated_at = ? WHERE id = ?",
                                 (content, now_dt.isoformat(), circle_id))
                conn.commit()
            return {
                'id': entry_id,
                'circle_id': circle_id,
                'user_id': row['user_id'],
                'entry_type': row['entry_type'],
                'content': content,
                'created_at': row['created_at'],
                'edited_at': now_dt.isoformat(),
                'round_number': int(row['round_number']) if 'round_number' in row.keys() and row['round_number'] is not None else 1,
            }
        except Exception as e:
            logger.error(f"Failed to update circle entry: {e}", exc_info=True)
            return None

    def update_phase(self, circle_id: str, new_phase: str, actor_id: str,
                     admin_user_id: Optional[str] = None) -> Optional[Circle]:
        circle = self.get_circle(circle_id)
        if not circle:
            return None
        if actor_id != circle.facilitator_id and not (admin_user_id and actor_id == admin_user_id):
            return None
        phase = (new_phase or '').strip().lower()
        if phase not in ('opinion', 'clarify', 'synthesis', 'decision', 'closed'):
            return None
        try:
            with self.db.get_connection() as conn:
                now_iso = _now_utc().isoformat()
                if phase == 'opinion' and (circle.phase or '').lower() == 'synthesis':
                    next_round = (circle.round_number or 1) + 1
                    conn.execute(
                        "UPDATE circles SET phase = ?, updated_at = ?, round_number = ? WHERE id = ?",
                        (phase, now_iso, next_round, circle_id),
                    )
                else:
                    conn.execute(
                        "UPDATE circles SET phase = ?, updated_at = ? WHERE id = ?",
                        (phase, now_iso, circle_id),
                    )
                conn.commit()
            return self.get_circle(circle_id)
        except Exception as e:
            logger.error(f"Failed to update circle phase: {e}", exc_info=True)
            return None

    def record_vote(self, circle_id: str, user_id: str, option_index: int) -> Optional[Dict[str, Any]]:
        circle = self.get_circle(circle_id)
        if not circle:
            return None
        if circle.decision_mode != 'vote':
            return None
        if circle.options and (option_index < 0 or option_index >= len(circle.options)):
            return None
        if not self._is_participant(circle, user_id):
            return None
        if not self._agent_allowed(circle, user_id):
            return None
        if circle.phase != 'decision':
            return None
        try:
            with self.db.get_connection() as conn:
                conn.execute("""
                    INSERT OR REPLACE INTO circle_votes (circle_id, user_id, option_index, created_at)
                    VALUES (?, ?, ?, ?)
                """, (circle_id, user_id, option_index, _now_utc().isoformat()))
                conn.commit()
            return {'circle_id': circle_id, 'user_id': user_id, 'option_index': option_index}
        except Exception as e:
            logger.error(f"Failed to record circle vote: {e}")
            return None

    def get_vote_counts(self, circle_id: str) -> Dict[str, Any]:
        counts: Dict[int, int] = {}
        total = 0
        try:
            with self.db.get_connection() as conn:
                rows = conn.execute("""
                    SELECT option_index, COUNT(*) AS n
                    FROM circle_votes
                    WHERE circle_id = ?
                    GROUP BY option_index
                """, (circle_id,)).fetchall()
            for row in rows:
                idx = int(row['option_index'])
                counts[idx] = int(row['n'])
                total += int(row['n'])
        except Exception:
            pass
        return {'counts': counts, 'total': total}

    def get_user_vote(self, circle_id: str, user_id: str) -> Optional[int]:
        if not circle_id or not user_id:
            return None
        try:
            with self.db.get_connection() as conn:
                row = conn.execute(
                    "SELECT option_index FROM circle_votes WHERE circle_id = ? AND user_id = ?",
                    (circle_id, user_id)
                ).fetchone()
            if not row:
                return None
            return int(row['option_index'])
        except Exception:
            return None

    def ingest_entry_snapshot(self, entry: Dict[str, Any]) -> bool:
        if not entry:
            return False
        entry_id = entry.get('id')
        circle_id = entry.get('circle_id')
        user_id = entry.get('user_id')
        entry_type = entry.get('entry_type')
        content = entry.get('content')
        created_at = entry.get('created_at') or _now_utc().isoformat()
        round_number = entry.get('round_number') or 1
        if not entry_id or not circle_id or not user_id or not entry_type or content is None:
            return False
        try:
            with self.db.get_connection() as conn:
                conn.execute("""
                    INSERT OR REPLACE INTO circle_entries
                    (id, circle_id, user_id, entry_type, content, created_at, edited_at, round_number)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (entry_id, circle_id, user_id, entry_type, content, created_at, entry.get('edited_at'), round_number))
                if entry_type == 'summary':
                    conn.execute("UPDATE circles SET summary = ?, updated_at = ? WHERE id = ?",
                                 (content, _now_utc().isoformat(), circle_id))
                if entry_type == 'decision':
                    conn.execute("UPDATE circles SET decision = ?, updated_at = ? WHERE id = ?",
                                 (content, _now_utc().isoformat(), circle_id))
                conn.commit()
            return True
        except Exception as e:
            logger.error(f"Failed to ingest circle entry snapshot: {e}")
            return False

    def ingest_phase_snapshot(self, circle_id: str, phase: str,
                              updated_at: Optional[str] = None,
                              round_number: Optional[int] = None) -> bool:
        if not circle_id or not phase:
            return False
        phase_val = (phase or '').strip().lower()
        if phase_val not in ('opinion', 'clarify', 'synthesis', 'decision', 'closed'):
            return False
        try:
            with self.db.get_connection() as conn:
                if round_number is not None:
                    conn.execute(
                        "UPDATE circles SET phase = ?, updated_at = ?, round_number = ? WHERE id = ?",
                        (phase_val, updated_at or _now_utc().isoformat(), int(round_number), circle_id)
                    )
                else:
                    conn.execute(
                        "UPDATE circles SET phase = ?, updated_at = ? WHERE id = ?",
                        (phase_val, updated_at or _now_utc().isoformat(), circle_id)
                    )
                conn.commit()
            return True
        except Exception as e:
            logger.error(f"Failed to ingest circle phase snapshot: {e}")
            return False

    def ingest_vote_snapshot(self, circle_id: str, user_id: str,
                             option_index: int, created_at: Optional[str] = None) -> bool:
        if not circle_id or not user_id:
            return False
        try:
            with self.db.get_connection() as conn:
                conn.execute("""
                    INSERT OR REPLACE INTO circle_votes (circle_id, user_id, option_index, created_at)
                    VALUES (?, ?, ?, ?)
                """, (circle_id, user_id, int(option_index), created_at or _now_utc().isoformat()))
                conn.commit()
            return True
        except Exception as e:
            logger.error(f"Failed to ingest circle vote snapshot: {e}")
            return False
