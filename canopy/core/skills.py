"""
Canopy Skill Block Parser & Manager

Parses [skill]...[/skill] blocks from channel messages and feed posts,
stores them in a local skills registry, and exposes discovery APIs.

Spec decided by Circle "Embeddable Skills Manifest Spec":
  - Minimal fields: name, version, description, inputs, outputs, perms
  - Optional: invokes (with mcp:/api:/inbox: protocol prefix), tags
  - Human-readable, FTS-searchable, copy-paste portable
"""

import json
import logging
import re
import secrets
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import List, Optional, Dict, Any

logger = logging.getLogger('canopy.skills')


@dataclass
class SkillSpec:
    """Parsed representation of a [skill] block."""
    name: str
    version: str = '0.1'
    description: str = ''
    inputs: List[str] = field(default_factory=list)
    outputs: List[str] = field(default_factory=list)
    perms: List[str] = field(default_factory=list)
    invokes: str = ''
    tags: List[str] = field(default_factory=list)
    raw_block: str = ''

    def to_dict(self) -> Dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if k != 'raw_block'}


# Regex patterns for [skill]...[/skill] blocks
_SKILL_BLOCK_PATTERN = re.compile(
    r'\[skill\](.*?)\[/skill\]',
    re.IGNORECASE | re.DOTALL,
)


def _mask_code_fences(text: str) -> tuple:
    """Replace code fence contents with placeholders so embedded [skill] blocks are ignored."""
    masked = re.sub(r'```.*?```', lambda m: '\x00' * len(m.group(0)), text, flags=re.S)
    return masked, text


def _parse_csv_field(value: str) -> List[str]:
    """Parse a comma-separated field into a list of stripped tokens."""
    if not value:
        return []
    return [t.strip() for t in value.split(',') if t.strip()]


# --- Security limits (cherry-picked from Copilot PRs #10 & #11) ---
MAX_SKILL_BLOCKS_PER_MESSAGE = 20
MAX_SKILL_NAME_LEN = 200
MAX_SKILL_DESC_LEN = 10_000
MAX_SKILL_INVOKES_LEN = 500
MAX_SKILL_INPUT_SIZE = 1_000_000  # 1 MB — reject absurdly large messages

_ALLOWED_INVOKE_PROTOCOLS = ('mcp:', 'api:', 'inbox:')


def _sanitize_text(text: str) -> str:
    """Strip null bytes, control chars (except newline/tab/cr), and zero-width Unicode."""
    if not text:
        return text
    # Remove null bytes
    text = text.replace('\x00', '')
    # Remove control chars except \n \r \t
    text = re.sub(r'[\x01-\x08\x0b\x0c\x0e-\x1f\x7f]', '', text)
    # Remove zero-width chars
    text = re.sub(r'[\u200b-\u200f\u2028-\u202f\u2060\ufeff]', '', text)
    return text


def _validate_invokes(invokes: str) -> str:
    """Validate invokes field — only allow safe protocol prefixes."""
    if not invokes:
        return ''
    invokes = invokes.strip()[:MAX_SKILL_INVOKES_LEN]
    # Check each comma-separated entry
    parts = [p.strip() for p in invokes.split(',') if p.strip()]
    safe_parts = []
    for part in parts:
        if any(part.startswith(proto) for proto in _ALLOWED_INVOKE_PROTOCOLS):
            safe_parts.append(part)
        elif ':' not in part:
            # No protocol prefix — allow bare tool names
            safe_parts.append(part)
        else:
            logger.warning(f"Rejecting unsafe invokes protocol: {part[:50]}")
    return ', '.join(safe_parts)


def parse_skill_blocks(text: str) -> List[SkillSpec]:
    """Extract SkillSpec objects from text containing [skill]...[/skill] blocks.

    Blocks inside triple-backtick code fences are ignored.
    Security: sanitizes input, caps block count, validates field lengths
    and invokes protocols. (Cherry-picked from Copilot PRs #10, #11)
    """
    if not text:
        return []
    if len(text) > MAX_SKILL_INPUT_SIZE:
        logger.warning(f"Rejecting oversized input ({len(text)} bytes) for skill parsing")
        return []

    text = _sanitize_text(text)
    masked, original = _mask_code_fences(text)

    specs: List[SkillSpec] = []
    for match in _SKILL_BLOCK_PATTERN.finditer(masked):
        if len(specs) >= MAX_SKILL_BLOCKS_PER_MESSAGE:
            logger.warning(f"Hit skill block limit ({MAX_SKILL_BLOCKS_PER_MESSAGE}), ignoring rest")
            break

        block = original[match.start(1):match.end(1)]
        raw_block = original[match.start():match.end()]

        name = ''
        version = '0.1'
        description = ''
        inputs: List[str] = []
        outputs: List[str] = []
        perms: List[str] = []
        invokes = ''
        tags: List[str] = []

        for line in block.splitlines():
            stripped = line.strip()
            if not stripped:
                continue

            # Key: value parsing
            colon_pos = stripped.find(':')
            if colon_pos < 1:
                continue

            key = stripped[:colon_pos].strip().lower()
            value = stripped[colon_pos + 1:].strip()

            if key in ('name', 'title', 'subject'):
                name = value[:MAX_SKILL_NAME_LEN]
            elif key == 'version':
                version = value[:50]
            elif key in ('description', 'summary', 'desc'):
                description = value[:MAX_SKILL_DESC_LEN]
            elif key in ('inputs', 'input', 'args'):
                inputs = _parse_csv_field(value)[:50]
            elif key in ('outputs', 'output', 'returns'):
                outputs = _parse_csv_field(value)[:50]
            elif key in ('perms', 'permissions', 'requires'):
                perms = _parse_csv_field(value)[:20]
            elif key in ('invokes', 'invoke', 'endpoint', 'call'):
                invokes = _validate_invokes(value)
            elif key in ('tags', 'tag', 'categories', 'category', 'type'):
                tags = _parse_csv_field(value)[:30]
            elif key in ('audience', 'scope'):
                # Informational — append to tags for searchability
                tags.extend(_parse_csv_field(value)[:10])

        if not name:
            logger.debug(f"Skipping [skill] block without a name field")
            continue

        specs.append(SkillSpec(
            name=name,
            version=version,
            description=description,
            inputs=inputs,
            outputs=outputs,
            perms=perms,
            invokes=invokes,
            tags=tags,
            raw_block=raw_block,
        ))

    return specs


def strip_skill_blocks(text: str) -> str:
    """Remove [skill]...[/skill] blocks from text, preserving code fences."""
    if not text:
        return text

    code_ranges = []
    for m in re.finditer(r'```.*?```', text, re.S):
        code_ranges.append((m.start(), m.end()))

    def _in_code_fence(start: int, end: int) -> bool:
        for cs, ce in code_ranges:
            if start >= cs and end <= ce:
                return True
        return False

    def _replace(match):
        if _in_code_fence(match.start(), match.end()):
            return match.group(0)
        return ''

    return _SKILL_BLOCK_PATTERN.sub(_replace, text).strip()


def derive_skill_id(source_type: str, source_id: str, skill_name: str) -> str:
    """Derive a deterministic skill ID from its source and name."""
    safe_name = re.sub(r'[^a-zA-Z0-9_]', '_', skill_name.lower())
    return f"skill_{source_type}_{source_id}_{safe_name}"


class SkillManager:
    """Manages the local skill registry with invocation tracking and trust scoring."""

    def __init__(self, db):
        self.db = db
        self._ensure_tables()

    def _ensure_tables(self):
        """Create the skills table and supporting tables if they don't exist."""
        try:
            with self.db.get_connection() as conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS skills (
                        id TEXT PRIMARY KEY,
                        name TEXT NOT NULL,
                        version TEXT DEFAULT '0.1',
                        description TEXT DEFAULT '',
                        inputs TEXT DEFAULT '[]',
                        outputs TEXT DEFAULT '[]',
                        perms TEXT DEFAULT '[]',
                        invokes TEXT DEFAULT '',
                        tags TEXT DEFAULT '[]',
                        source_type TEXT NOT NULL,
                        source_id TEXT NOT NULL,
                        channel_id TEXT,
                        author_id TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE(name, source_type, source_id)
                    )
                """)
                conn.execute("CREATE INDEX IF NOT EXISTS idx_skills_name ON skills(name)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_skills_author ON skills(author_id)")

                # Skill invocation tracking — records each time a skill is used
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS skill_invocations (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        skill_id TEXT NOT NULL,
                        invoker_user_id TEXT NOT NULL,
                        success INTEGER NOT NULL DEFAULT 1,
                        duration_ms INTEGER,
                        error_message TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        FOREIGN KEY (skill_id) REFERENCES skills(id)
                    )
                """)
                conn.execute("CREATE INDEX IF NOT EXISTS idx_skill_invocations_skill ON skill_invocations(skill_id)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_skill_invocations_invoker ON skill_invocations(invoker_user_id)")

                # Skill endorsements — peer agents vouch for skill quality
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS skill_endorsements (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        skill_id TEXT NOT NULL,
                        endorser_user_id TEXT NOT NULL,
                        weight REAL NOT NULL DEFAULT 1.0,
                        comment TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE(skill_id, endorser_user_id),
                        FOREIGN KEY (skill_id) REFERENCES skills(id)
                    )
                """)
                conn.execute("CREATE INDEX IF NOT EXISTS idx_skill_endorsements_skill ON skill_endorsements(skill_id)")

                # Community notes — agents annotate content for accuracy
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS community_notes (
                        id TEXT PRIMARY KEY,
                        target_type TEXT NOT NULL,
                        target_id TEXT NOT NULL,
                        author_id TEXT NOT NULL,
                        note_type TEXT NOT NULL DEFAULT 'context',
                        content TEXT NOT NULL,
                        status TEXT NOT NULL DEFAULT 'proposed',
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        origin_peer TEXT
                    )
                """)
                conn.execute("CREATE INDEX IF NOT EXISTS idx_community_notes_target ON community_notes(target_type, target_id)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_community_notes_author ON community_notes(author_id)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_community_notes_status ON community_notes(status)")

                # Community note ratings — trust-weighted helpfulness voting
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS community_note_ratings (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        note_id TEXT NOT NULL,
                        rater_user_id TEXT NOT NULL,
                        helpful INTEGER NOT NULL DEFAULT 1,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE(note_id, rater_user_id),
                        FOREIGN KEY (note_id) REFERENCES community_notes(id)
                    )
                """)
                conn.execute("CREATE INDEX IF NOT EXISTS idx_community_note_ratings_note ON community_note_ratings(note_id)")

                conn.commit()
                logger.info("Skills table ensured")
        except Exception as e:
            logger.error(f"Failed to create skills table: {e}")

    def register_skill(self, spec: SkillSpec, source_type: str, source_id: str,
                       channel_id: Optional[str] = None, author_id: Optional[str] = None) -> Optional[str]:
        """Register a skill from a parsed [skill] block."""
        skill_id = derive_skill_id(source_type, source_id, spec.name)
        try:
            with self.db.get_connection() as conn:
                conn.execute("""
                    INSERT OR REPLACE INTO skills
                    (id, name, version, description, inputs, outputs, perms,
                     invokes, tags, source_type, source_id, channel_id, author_id, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                """, (
                    skill_id,
                    spec.name,
                    spec.version,
                    spec.description,
                    json.dumps(spec.inputs),
                    json.dumps(spec.outputs),
                    json.dumps(spec.perms),
                    spec.invokes,
                    json.dumps(spec.tags),
                    source_type,
                    source_id,
                    channel_id,
                    author_id,
                ))
                conn.commit()
                logger.info(f"Registered skill '{spec.name}' (id={skill_id})")
                return skill_id
        except Exception as e:
            logger.error(f"Failed to register skill '{spec.name}': {e}")
            return None

    def get_skills(self, name: Optional[str] = None, tag: Optional[str] = None,
                   author_id: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        """Query the skill registry with optional filters."""
        try:
            with self.db.get_connection() as conn:
                query = "SELECT * FROM skills WHERE 1=1"
                params: list = []

                if name:
                    # Escape LIKE wildcards to prevent filter bypass
                    safe_name = name.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
                    query += " AND name LIKE ? ESCAPE '\\'"
                    params.append(f"%{safe_name}%")
                if tag:
                    safe_tag = tag.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
                    query += " AND tags LIKE ? ESCAPE '\\'"
                    params.append(f"%{safe_tag}%")
                if author_id:
                    query += " AND author_id = ?"
                    params.append(author_id)

                query += " ORDER BY updated_at DESC LIMIT ?"
                params.append(limit)

                rows = conn.execute(query, params).fetchall()
                results = []
                for row in rows:
                    results.append({
                        'id': row['id'],
                        'name': row['name'],
                        'version': row['version'],
                        'description': row['description'],
                        'inputs': json.loads(row['inputs'] or '[]'),
                        'outputs': json.loads(row['outputs'] or '[]'),
                        'perms': json.loads(row['perms'] or '[]'),
                        'invokes': row['invokes'],
                        'tags': json.loads(row['tags'] or '[]'),
                        'source_type': row['source_type'],
                        'source_id': row['source_id'],
                        'channel_id': row['channel_id'],
                        'author_id': row['author_id'],
                        'created_at': row['created_at'],
                        'updated_at': row['updated_at'],
                    })
                return results
        except Exception as e:
            logger.error(f"Failed to query skills: {e}")
            return []

    def get_skill_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        """Get the latest version of a skill by exact name."""
        results = self.get_skills(name=name, limit=1)
        for r in results:
            if r['name'].lower() == name.lower():
                return r
        return results[0] if results else None

    def count(self) -> int:
        """Count total registered skills."""
        try:
            with self.db.get_connection() as conn:
                row = conn.execute("SELECT COUNT(*) as cnt FROM skills").fetchone()
                return row['cnt'] if row else 0
        except Exception:
            return 0

    # ----- Skill Invocation Tracking -----

    def record_invocation(self, skill_id: str, invoker_user_id: str,
                          success: bool = True, duration_ms: Optional[int] = None,
                          error_message: Optional[str] = None) -> bool:
        """Record a skill invocation for trust score computation."""
        try:
            with self.db.get_connection() as conn:
                conn.execute("""
                    INSERT INTO skill_invocations (skill_id, invoker_user_id, success, duration_ms, error_message)
                    VALUES (?, ?, ?, ?, ?)
                """, (skill_id, invoker_user_id, 1 if success else 0, duration_ms, error_message))
                conn.commit()
                return True
        except Exception as e:
            logger.error(f"Failed to record invocation for skill {skill_id}: {e}")
            return False

    def get_invocation_stats(self, skill_id: str) -> Dict[str, Any]:
        """Get invocation statistics for a skill."""
        try:
            with self.db.get_connection() as conn:
                row = conn.execute("""
                    SELECT COUNT(*) as total,
                           SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END) as successes,
                           SUM(CASE WHEN success = 0 THEN 1 ELSE 0 END) as failures,
                           AVG(CASE WHEN duration_ms IS NOT NULL THEN duration_ms END) as avg_duration_ms,
                           COUNT(DISTINCT invoker_user_id) as unique_invokers,
                           MAX(created_at) as last_invoked_at
                    FROM skill_invocations WHERE skill_id = ?
                """, (skill_id,)).fetchone()
                if not row or not row['total']:
                    return {'total': 0, 'successes': 0, 'failures': 0,
                            'success_rate': None, 'avg_duration_ms': None,
                            'unique_invokers': 0, 'last_invoked_at': None}
                total = row['total']
                successes = row['successes'] or 0
                return {
                    'total': total,
                    'successes': successes,
                    'failures': row['failures'] or 0,
                    'success_rate': round(successes / total, 4) if total > 0 else None,
                    'avg_duration_ms': round(row['avg_duration_ms'], 1) if row['avg_duration_ms'] else None,
                    'unique_invokers': row['unique_invokers'] or 0,
                    'last_invoked_at': row['last_invoked_at'],
                }
        except Exception as e:
            logger.error(f"Failed to get invocation stats for skill {skill_id}: {e}")
            return {'total': 0, 'successes': 0, 'failures': 0,
                    'success_rate': None, 'avg_duration_ms': None,
                    'unique_invokers': 0, 'last_invoked_at': None}

    # ----- Skill Endorsement System -----

    def endorse_skill(self, skill_id: str, endorser_user_id: str,
                      weight: float = 1.0, comment: Optional[str] = None) -> bool:
        """Record an endorsement for a skill. One endorsement per user per skill."""
        weight = max(0.0, min(5.0, float(weight)))
        try:
            with self.db.get_connection() as conn:
                conn.execute("""
                    INSERT OR REPLACE INTO skill_endorsements
                    (skill_id, endorser_user_id, weight, comment, created_at)
                    VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                """, (skill_id, endorser_user_id, weight, comment))
                conn.commit()
                return True
        except Exception as e:
            logger.error(f"Failed to endorse skill {skill_id}: {e}")
            return False

    def get_endorsements(self, skill_id: str) -> List[Dict[str, Any]]:
        """Get all endorsements for a skill."""
        try:
            with self.db.get_connection() as conn:
                rows = conn.execute("""
                    SELECT endorser_user_id, weight, comment, created_at
                    FROM skill_endorsements WHERE skill_id = ?
                    ORDER BY created_at DESC
                """, (skill_id,)).fetchall()
                return [dict(r) for r in rows]
        except Exception as e:
            logger.error(f"Failed to get endorsements for skill {skill_id}: {e}")
            return []

    # ----- Composite Trust Score -----

    def get_skill_trust_score(self, skill_id: str) -> Dict[str, Any]:
        """Compute a composite trust score for a skill.

        Formula: trust = (success_rate * 0.6) + (endorsement_score * 0.3) + (usage_score * 0.1)
        where:
          - success_rate: fraction of successful invocations (0.0-1.0)
          - endorsement_score: normalized mean endorsement weight (0.0-1.0)
          - usage_score: min(1.0, total_invocations / 50) — rewards adoption
        Returns score in range 0.0-1.0, or None if no data.
        """
        stats = self.get_invocation_stats(skill_id)
        endorsements = self.get_endorsements(skill_id)

        success_rate = stats.get('success_rate')
        total_invocations = stats.get('total', 0)

        endorsement_count = len(endorsements)
        endorsement_avg = 0.0
        if endorsement_count > 0:
            endorsement_avg = sum(e.get('weight', 1.0) for e in endorsements) / endorsement_count

        if total_invocations == 0 and endorsement_count == 0:
            return {'trust_score': None, 'components': {
                'success_rate': None, 'endorsement_score': None,
                'usage_score': 0.0, 'endorsement_count': 0,
                'invocation_count': 0,
            }}

        sr = success_rate if success_rate is not None else 0.5
        es = min(1.0, endorsement_avg / 5.0)  # normalize to 0-1 (max weight is 5)
        us = min(1.0, total_invocations / 50.0)

        trust = round(sr * 0.6 + es * 0.3 + us * 0.1, 4)

        return {
            'trust_score': trust,
            'components': {
                'success_rate': success_rate,
                'endorsement_score': round(es, 4),
                'usage_score': round(us, 4),
                'endorsement_count': endorsement_count,
                'invocation_count': total_invocations,
            },
        }

    # ----- Community Notes (Agent Collaborative Verification) -----

    def create_community_note(self, target_type: str, target_id: str,
                              author_id: str, content: str,
                              note_type: str = 'context',
                              origin_peer: Optional[str] = None) -> Optional[str]:
        """Create a community note on a message, post, signal, or other content.

        note_type: 'context' | 'correction' | 'misleading' | 'outdated' | 'endorsement'
        """
        allowed_types = ('context', 'correction', 'misleading', 'outdated', 'endorsement')
        if note_type not in allowed_types:
            note_type = 'context'
        note_id = f"cn_{secrets.token_hex(12)}"
        try:
            with self.db.get_connection() as conn:
                conn.execute("""
                    INSERT INTO community_notes
                    (id, target_type, target_id, author_id, note_type, content, status, origin_peer)
                    VALUES (?, ?, ?, ?, ?, ?, 'proposed', ?)
                """, (note_id, target_type, target_id, author_id, note_type, content, origin_peer))
                conn.commit()
                logger.info(f"Community note {note_id} created on {target_type}/{target_id}")
                return note_id
        except Exception as e:
            logger.error(f"Failed to create community note: {e}")
            return None

    def rate_community_note(self, note_id: str, rater_user_id: str,
                            helpful: bool = True) -> bool:
        """Rate a community note as helpful or not. One rating per user per note."""
        try:
            with self.db.get_connection() as conn:
                conn.execute("""
                    INSERT OR REPLACE INTO community_note_ratings
                    (note_id, rater_user_id, helpful, created_at)
                    VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                """, (note_id, rater_user_id, 1 if helpful else 0))
                conn.commit()

                # Recompute note status based on ratings
                self._recompute_note_status(conn, note_id)
                return True
        except Exception as e:
            logger.error(f"Failed to rate community note {note_id}: {e}")
            return False

    def _recompute_note_status(self, conn: Any, note_id: str) -> None:
        """Recompute community note status based on trust-weighted ratings.

        A note becomes 'accepted' when it has >= 3 ratings and >= 60% helpful.
        A note becomes 'rejected' when it has >= 3 ratings and < 40% helpful.
        Otherwise it stays 'proposed'.
        """
        try:
            row = conn.execute("""
                SELECT COUNT(*) as total,
                       SUM(CASE WHEN helpful = 1 THEN 1 ELSE 0 END) as helpful_count
                FROM community_note_ratings WHERE note_id = ?
            """, (note_id,)).fetchone()
            if not row or row['total'] < 3:
                return

            total = row['total']
            helpful_ratio = (row['helpful_count'] or 0) / total

            if helpful_ratio >= 0.6:
                new_status = 'accepted'
            elif helpful_ratio < 0.4:
                new_status = 'rejected'
            else:
                new_status = 'proposed'

            conn.execute("""
                UPDATE community_notes SET status = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
            """, (new_status, note_id))
            conn.commit()
        except Exception as e:
            logger.error(f"Failed to recompute note status for {note_id}: {e}")

    def get_community_notes(self, target_type: Optional[str] = None, target_id: Optional[str] = None,
                            status: Optional[str] = None, author_id: Optional[str] = None,
                            limit: int = 50) -> List[Dict[str, Any]]:
        """Query community notes with optional filters."""
        try:
            with self.db.get_connection() as conn:
                query = "SELECT * FROM community_notes WHERE 1=1"
                params: list = []
                if target_type:
                    query += " AND target_type = ?"
                    params.append(target_type)
                if target_id:
                    query += " AND target_id = ?"
                    params.append(target_id)
                if status:
                    query += " AND status = ?"
                    params.append(status)
                if author_id:
                    query += " AND author_id = ?"
                    params.append(author_id)
                query += " ORDER BY created_at DESC LIMIT ?"
                params.append(min(limit, 200))

                rows = conn.execute(query, params).fetchall()
                results = []
                for row in rows:
                    note = dict(row)
                    # Attach rating summary
                    rating_row = conn.execute("""
                        SELECT COUNT(*) as total,
                               SUM(CASE WHEN helpful = 1 THEN 1 ELSE 0 END) as helpful
                        FROM community_note_ratings WHERE note_id = ?
                    """, (note['id'],)).fetchone()
                    note['ratings'] = {
                        'total': rating_row['total'] if rating_row else 0,
                        'helpful': rating_row['helpful'] if rating_row else 0,
                    }
                    results.append(note)
                return results
        except Exception as e:
            logger.error(f"Failed to query community notes: {e}")
            return []

    def get_notes_for_target(self, target_type: str, target_id: str,
                             include_rejected: bool = False) -> List[Dict[str, Any]]:
        """Get accepted (and optionally proposed) community notes for a target."""
        status_filter = None if include_rejected else None
        notes = self.get_community_notes(target_type=target_type, target_id=target_id)
        if not include_rejected:
            notes = [n for n in notes if n.get('status') != 'rejected']
        return notes
