"""
Mention event handling for Canopy.

Stores per-user mention events and provides helpers to resolve
@mentions in content so agents can react on demand.

Author: Konrad Walus (architecture, design, and direction)
Project: Canopy - Local Mesh Communication
License: Apache 2.0
Development: AI-assisted implementation (Claude, Codex, GitHub Copilot, Cursor IDE, Ollama)
"""

from __future__ import annotations

import json
import logging
import re
import secrets
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple, cast

from .events import EVENT_MENTION_ACKNOWLEDGED, EVENT_MENTION_CREATED

logger = logging.getLogger(__name__)


# Match @handles when the preceding character is not part of a handle.
# This supports markdown wrappers like "**@Agent**", avoids emails, and
# avoids swallowing trailing punctuation at sentence boundaries.
MENTION_REGEX = re.compile(
    r'(^|[^A-Za-z0-9_.\-@])@([A-Za-z0-9](?:[A-Za-z0-9_.\-]{0,47}[A-Za-z0-9]))'
)


def extract_mentions(text: str) -> List[str]:
    """Extract @handles from a string."""
    if not text or '@' not in text:
        return []
    return [m.group(2) for m in MENTION_REGEX.finditer(text)]


def build_preview(text: str, limit: int = 120) -> str:
    """Create a short preview string for notifications."""
    if not text:
        return ""
    preview = str(text).strip()
    if len(preview) > limit:
        preview = preview[: limit - 3] + "..."
    return preview


def _to_datetime_utc(value: Any) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        raw = str(value).strip()
        if not raw:
            return None
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except Exception:
            try:
                dt = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S.%f")
            except Exception:
                dt = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _to_iso_utc(value: Any) -> Optional[str]:
    try:
        dt = _to_datetime_utc(value)
        return dt.isoformat() if dt else None
    except Exception:
        return str(value) if value is not None else None


def _sqlite_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")


def _normalize_handles(handles: Sequence[str]) -> List[str]:
    """Normalize handles while preserving case (case-sensitive dedupe)."""
    cleaned = []
    for h in handles or []:
        if not h:
            continue
        cleaned.append(str(h).strip())
    # preserve order while de-duping (case-sensitive)
    seen = set()
    ordered = []
    for h in cleaned:
        if h in seen:
            continue
        seen.add(h)
        ordered.append(h)
    return ordered


def _normalize_display_handle(display_name: Optional[str]) -> str:
    """Normalize display_name to a mention handle: trim, collapse spaces, replace with underscore.
    Must match SQL normalization used in _query so resolution is consistent."""
    if not display_name:
        return ""
    return "_".join(str(display_name).strip().split())


def _lower_handles(handles: Sequence[str]) -> List[str]:
    """Lowercase handles while preserving order (case-insensitive dedupe)."""
    cleaned = []
    for h in handles or []:
        if not h:
            continue
        cleaned.append(str(h).strip().lower())
    seen = set()
    ordered = []
    for h in cleaned:
        if h in seen:
            continue
        seen.add(h)
        ordered.append(h)
    return ordered


def resolve_mention_targets(
    db_manager: Any,
    handles: Sequence[str],
    channel_id: Optional[str] = None,
    visibility: Optional[str] = None,
    permissions: Optional[Sequence[str]] = None,
    author_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Resolve mention handles to user records.

    For channel mentions, restrict to channel members.
    For feed mentions, apply visibility/permission filtering.
    """
    handles_raw = _normalize_handles(handles)
    if not handles_raw:
        return []

    # Normalize display_name for matching: TRIM + collapse multiple spaces + space to underscore.
    # Must match _normalize_display_handle() so DB and Python comparison agree.
    _norm_display_u = (
        "REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(TRIM(COALESCE(u.display_name,'')), '  ', ' '), '  ', ' '), '  ', ' '), '  ', ' '), ' ', '_')"
    )
    _norm_display = (
        "REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(TRIM(COALESCE(display_name,'')), '  ', ' '), '  ', ' '), '  ', ' '), '  ', ' '), ' ', '_')"
    )
    try:
        with db_manager.get_connection() as conn:
            def _query(handles_list: Sequence[str], case_sensitive: bool) -> List[Any]:
                if not handles_list:
                    return []
                placeholders = ",".join("?" for _ in handles_list)
                if channel_id:
                    params = [channel_id] + list(handles_list) + list(handles_list) + list(handles_list)
                    if case_sensitive:
                        sql = f"""
                        SELECT u.id, u.username, u.display_name, u.public_key, u.origin_peer,
                               cm.notifications_enabled
                        FROM users u
                        JOIN channel_members cm ON u.id = cm.user_id
                        WHERE cm.channel_id = ?
                          AND u.id NOT IN ('system', 'local_user')
                          AND (u.id IN ({placeholders})
                               OR TRIM(COALESCE(u.username,'')) IN ({placeholders})
                               OR {_norm_display_u} IN ({placeholders}))
                        """
                    else:
                        sql = f"""
                        SELECT u.id, u.username, u.display_name, u.public_key, u.origin_peer,
                               cm.notifications_enabled
                        FROM users u
                        JOIN channel_members cm ON u.id = cm.user_id
                        WHERE cm.channel_id = ?
                          AND u.id NOT IN ('system', 'local_user')
                          AND (LOWER(u.id) IN ({placeholders})
                               OR LOWER(TRIM(COALESCE(u.username,''))) IN ({placeholders})
                               OR LOWER({_norm_display_u}) IN ({placeholders}))
                        """
                    return cast(List[Any], conn.execute(sql, params).fetchall())
                else:
                    params = list(handles_list) + list(handles_list) + list(handles_list)
                    if case_sensitive:
                        sql = f"""
                        SELECT id, username, display_name, public_key, origin_peer
                        FROM users
                        WHERE id NOT IN ('system', 'local_user')
                          AND (id IN ({placeholders})
                               OR TRIM(COALESCE(username,'')) IN ({placeholders})
                               OR {_norm_display} IN ({placeholders}))
                        """
                    else:
                        sql = f"""
                        SELECT id, username, display_name, public_key, origin_peer
                        FROM users
                        WHERE id NOT IN ('system', 'local_user')
                          AND (LOWER(id) IN ({placeholders})
                               OR LOWER(TRIM(COALESCE(username,''))) IN ({placeholders})
                               OR LOWER({_norm_display}) IN ({placeholders}))
                        """
                    return cast(List[Any], conn.execute(sql, params).fetchall())

            exact_rows = _query(handles_raw, case_sensitive=True)
            exact_handles = set()
            rows = []
            for row in exact_rows:
                try:
                    row_id = row['id']
                    row_username = (row['username'] or '').strip()
                    row_display = row['display_name'] or ''
                except Exception:
                    row_id = row[0]
                    row_username = (row[1] or '').strip() if len(row) > 1 else ''
                    row_display = row[2] if len(row) > 2 else ''
                display_handle = _normalize_display_handle(row_display)
                for handle in handles_raw:
                    if handle == row_id or (row_username and handle == row_username) or (display_handle and handle == display_handle):
                        exact_handles.add(handle)
                rows.append(row)

            remaining = [h for h in handles_raw if h not in exact_handles]
            if remaining:
                remaining_lower = _lower_handles(remaining)
                rows.extend(_query(remaining_lower, case_sensitive=False))
    except Exception as e:
        logger.error(f"Failed to resolve mention targets: {e}")
        return []

    # Apply visibility filters for feed posts
    allowed: Optional[set] = None
    if visibility:
        vis = str(visibility).lower()
        if vis == 'private' and author_id:
            allowed = {author_id}
        elif vis == 'custom':
            allowed = set(permissions or [])

    targets: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        try:
            user_id = row['id']
            username = row['username']
            public_key = row['public_key']
            origin_peer = row['origin_peer']
            notifications_enabled = None
            if 'notifications_enabled' in row.keys():
                notifications_enabled = row['notifications_enabled']
        except Exception:
            user_id = row[0]
            username = row[1]
            # row[2] is display_name when selected; shift indices accordingly
            public_key = row[3] if len(row) > 3 else row[2]
            origin_peer = row[4] if len(row) > 4 else None
            notifications_enabled = row[5] if len(row) > 5 else None

        if author_id and user_id == author_id:
            continue
        if allowed is not None and user_id not in allowed:
            continue

        targets[user_id] = {
            'user_id': user_id,
            'username': username,
            'public_key': public_key,
            'origin_peer': origin_peer,
            'notifications_enabled': notifications_enabled,
        }

    return list(targets.values())


def split_mention_targets(
    targets: Sequence[Dict[str, Any]],
    local_peer_id: Optional[str] = None,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Split mention targets into local vs remote candidates."""
    local_targets: List[Dict[str, Any]] = []
    remote_targets: List[Dict[str, Any]] = []
    for t in targets or []:
        public_key = t.get('public_key')
        origin_peer = t.get('origin_peer')
        is_local = bool(public_key) and (not origin_peer or (local_peer_id and origin_peer == local_peer_id))
        if is_local:
            local_targets.append(t)
        else:
            remote_targets.append(t)
    return local_targets, remote_targets


def _filter_channel_target_ids_for_notifications(
    mention_manager: Optional["MentionManager"],
    *,
    channel_id: str,
    source_id: str,
    target_ids: Sequence[str],
) -> List[str]:
    """Filter channel mention targets to members with notifications enabled."""
    resolved_target_ids = list(dict.fromkeys([str(tid).strip() for tid in (target_ids or []) if str(tid).strip()]))
    if not resolved_target_ids:
        return []
    if not mention_manager:
        logger.warning(
            "Skipping channel mention activity without mention manager "
            "(source_id=%s channel_id=%s)",
            source_id,
            channel_id,
        )
        return []

    try:
        with mention_manager.db.get_connection() as conn:
            placeholders = ",".join("?" for _ in resolved_target_ids)
            if placeholders:
                try:
                    rows = conn.execute(
                        f"""
                        SELECT user_id, notifications_enabled
                        FROM channel_members
                        WHERE channel_id = ? AND user_id IN ({placeholders})
                        """,
                        [channel_id] + resolved_target_ids,
                    ).fetchall()
                except Exception:
                    # Backward compatibility with legacy schemas that may
                    # not yet have channel_members.notifications_enabled.
                    rows = conn.execute(
                        f"""
                        SELECT user_id, 1 AS notifications_enabled
                        FROM channel_members
                        WHERE channel_id = ? AND user_id IN ({placeholders})
                        """,
                        [channel_id] + resolved_target_ids,
                    ).fetchall()
            else:
                rows = []

        member_user_ids: set[str] = set()
        muted_user_ids: set[str] = set()
        for row in rows:
            if hasattr(row, 'keys'):
                uid = row['user_id']
                enabled_raw = row['notifications_enabled'] if 'notifications_enabled' in row.keys() else 1
            else:
                uid = row[0]
                enabled_raw = row[1] if len(row) > 1 else 1
            uid_s = str(uid or '').strip()
            if not uid_s:
                continue
            member_user_ids.add(uid_s)
            if enabled_raw is None:
                enabled = True
            elif isinstance(enabled_raw, str):
                enabled = enabled_raw.strip().lower() not in {'0', 'false', 'off', 'no'}
            else:
                enabled = bool(enabled_raw)
            if not enabled:
                muted_user_ids.add(uid_s)

        allowed_user_ids = member_user_ids - muted_user_ids
        filtered_ids = [uid for uid in resolved_target_ids if uid in allowed_user_ids]
        dropped_ids = [uid for uid in resolved_target_ids if uid not in allowed_user_ids]
        if dropped_ids:
            dropped_nonmembers = [uid for uid in dropped_ids if uid not in member_user_ids]
            dropped_muted = [uid for uid in dropped_ids if uid in muted_user_ids]
            if dropped_nonmembers:
                logger.info(
                    "Dropped %d mention target(s) without channel membership "
                    "(source_id=%s channel_id=%s users=%s)",
                    len(dropped_nonmembers),
                    source_id,
                    channel_id,
                    dropped_nonmembers,
                )
            if dropped_muted:
                logger.info(
                    "Dropped %d mention target(s) due to channel mute "
                    "(source_id=%s channel_id=%s users=%s)",
                    len(dropped_muted),
                    source_id,
                    channel_id,
                    dropped_muted,
                )
        return filtered_ids
    except Exception as e:
        logger.warning(
            "Mention membership verification failed; dropping channel mention "
            "(source_id=%s channel_id=%s error=%s)",
            source_id,
            channel_id,
            e,
        )
        return []


class MentionManager:
    """Stores per-user mention events."""
    DEFAULT_CLAIM_TTL_SECONDS = 120
    MIN_CLAIM_TTL_SECONDS = 15
    MAX_CLAIM_TTL_SECONDS = 1800

    def __init__(self, db_manager):
        self.db = db_manager
        self.workspace_events: Any = None
        self._ensure_tables()

    def _ensure_tables(self) -> None:
        try:
            with self.db.get_connection() as conn:
                conn.executescript("""
                    CREATE TABLE IF NOT EXISTS mention_events (
                        id TEXT PRIMARY KEY,
                        user_id TEXT NOT NULL,
                        source_type TEXT NOT NULL,
                        source_id TEXT NOT NULL,
                        author_id TEXT,
                        origin_peer TEXT,
                        channel_id TEXT,
                        preview TEXT,
                        metadata TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        acknowledged_at TIMESTAMP,
                        status TEXT DEFAULT 'new'
                    );
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_mention_events_unique
                        ON mention_events(user_id, source_type, source_id);
                    CREATE INDEX IF NOT EXISTS idx_mention_events_user
                        ON mention_events(user_id, created_at);

                    CREATE TABLE IF NOT EXISTS mention_claims (
                        id TEXT PRIMARY KEY,
                        source_type TEXT NOT NULL,
                        source_id TEXT NOT NULL,
                        channel_id TEXT,
                        claimed_by_user_id TEXT NOT NULL,
                        claimed_by_username TEXT,
                        claimed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        expires_at TIMESTAMP NOT NULL,
                        released_at TIMESTAMP,
                        release_reason TEXT,
                        metadata TEXT,
                        UNIQUE(source_type, source_id)
                    );
                    CREATE INDEX IF NOT EXISTS idx_mention_claims_source
                        ON mention_claims(source_type, source_id);
                    CREATE INDEX IF NOT EXISTS idx_mention_claims_owner
                        ON mention_claims(claimed_by_user_id, claimed_at);
                """)
                conn.commit()
        except Exception as e:
            logger.error(f"Failed to ensure mention_events table: {e}")

    def _normalize_claim_ttl(self, ttl_seconds: Any) -> int:
        try:
            ttl = int(ttl_seconds)
        except Exception:
            ttl = self.DEFAULT_CLAIM_TTL_SECONDS
        ttl = max(self.MIN_CLAIM_TTL_SECONDS, ttl)
        ttl = min(self.MAX_CLAIM_TTL_SECONDS, ttl)
        return ttl

    def _serialize_claim_row(self, row: Any, *, now_dt: Optional[datetime] = None) -> Optional[Dict[str, Any]]:
        if not row:
            return None
        now_val = now_dt or datetime.now(timezone.utc)
        expires_dt = _to_datetime_utc(row['expires_at'] if hasattr(row, '__getitem__') else row[7])
        claimed_dt = _to_datetime_utc(row['claimed_at'] if hasattr(row, '__getitem__') else row[6])
        released_dt = _to_datetime_utc(row['released_at'] if hasattr(row, '__getitem__') else row[8])
        active = bool(expires_dt and expires_dt > now_val and not released_dt)
        metadata_raw = row['metadata'] if hasattr(row, '__getitem__') else row[10]
        metadata = None
        if metadata_raw:
            try:
                metadata = json.loads(metadata_raw)
            except Exception:
                metadata = None
        return {
            'id': row['id'] if hasattr(row, '__getitem__') else row[0],
            'source_type': row['source_type'] if hasattr(row, '__getitem__') else row[1],
            'source_id': row['source_id'] if hasattr(row, '__getitem__') else row[2],
            'channel_id': row['channel_id'] if hasattr(row, '__getitem__') else row[3],
            'claimed_by_user_id': row['claimed_by_user_id'] if hasattr(row, '__getitem__') else row[4],
            'claimed_by_username': row['claimed_by_username'] if hasattr(row, '__getitem__') else row[5],
            'claimed_at': _to_iso_utc(claimed_dt),
            'expires_at': _to_iso_utc(expires_dt),
            'released_at': _to_iso_utc(released_dt),
            'release_reason': row['release_reason'] if hasattr(row, '__getitem__') else row[9],
            'metadata': metadata,
            'active': active,
        }

    def _get_active_claim_row(self, conn: Any, source_type: str, source_id: str) -> Optional[Any]:
        row = conn.execute(
            """
            SELECT id, source_type, source_id, channel_id, claimed_by_user_id,
                   claimed_by_username, claimed_at, expires_at, released_at,
                   release_reason, metadata
            FROM mention_claims
            WHERE source_type = ? AND source_id = ? AND released_at IS NULL
            LIMIT 1
            """,
            (source_type, source_id),
        ).fetchone()
        if not row:
            return None

        now_dt = datetime.now(timezone.utc)
        expires_dt = _to_datetime_utc(row['expires_at'] if hasattr(row, '__getitem__') else row[7])
        if expires_dt and expires_dt > now_dt:
            return row

        # Expired claim rows are retained for audit visibility but considered inactive.
        conn.execute(
            """
            UPDATE mention_claims
            SET released_at = ?, release_reason = COALESCE(release_reason, 'expired')
            WHERE id = ? AND released_at IS NULL
            """,
            (_sqlite_timestamp(now_dt), row['id'] if hasattr(row, '__getitem__') else row[0]),
        )
        return None

    def get_active_claim(
        self,
        source_type: str,
        source_id: str,
    ) -> Optional[Dict[str, Any]]:
        """Return an active claim for a mention source, if any."""
        if not source_type or not source_id:
            return None
        try:
            with self.db.get_connection() as conn:
                row = self._get_active_claim_row(conn, source_type, source_id)
                conn.commit()
            return self._serialize_claim_row(row) if row else None
        except Exception as e:
            logger.warning(f"Failed to load active mention claim: {e}")
            return None

    def claim_source(
        self,
        source_type: str,
        source_id: str,
        claimer_user_id: str,
        claimer_username: Optional[str] = None,
        channel_id: Optional[str] = None,
        ttl_seconds: Any = None,
        allow_takeover: bool = False,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Claim a mention source so one agent can handle it without duplicate replies."""
        if not source_type or not source_id or not claimer_user_id:
            return {'claimed': False, 'reason': 'invalid_request', 'claim': None}

        ttl = self._normalize_claim_ttl(ttl_seconds)
        now_dt = datetime.now(timezone.utc)
        expires_dt = now_dt + timedelta(seconds=ttl)
        claim_meta_json = json.dumps(metadata or {}) if metadata else None
        now_sql = _sqlite_timestamp(now_dt)
        expires_sql = _sqlite_timestamp(expires_dt)

        try:
            with self.db.get_connection() as conn:
                existing = self._get_active_claim_row(conn, source_type, source_id)
                if existing:
                    existing_owner = existing['claimed_by_user_id']
                    existing_id = existing['id']
                    if existing_owner == claimer_user_id:
                        conn.execute(
                            """
                            UPDATE mention_claims
                            SET claimed_by_username = ?, channel_id = COALESCE(?, channel_id),
                                claimed_at = ?, expires_at = ?, metadata = COALESCE(?, metadata)
                            WHERE id = ?
                            """,
                            (
                                claimer_username,
                                channel_id,
                                now_sql,
                                expires_sql,
                                claim_meta_json,
                                existing_id,
                            ),
                        )
                        row = conn.execute(
                            """
                            SELECT id, source_type, source_id, channel_id, claimed_by_user_id,
                                   claimed_by_username, claimed_at, expires_at, released_at,
                                   release_reason, metadata
                            FROM mention_claims WHERE id = ?
                            """,
                            (existing_id,),
                        ).fetchone()
                        conn.commit()
                        return {'claimed': True, 'reason': 'renewed', 'claim': self._serialize_claim_row(row, now_dt=now_dt)}
                    if not allow_takeover:
                        conn.commit()
                        return {'claimed': False, 'reason': 'already_claimed', 'claim': self._serialize_claim_row(existing, now_dt=now_dt)}

                    conn.execute(
                        """
                        UPDATE mention_claims
                        SET claimed_by_user_id = ?, claimed_by_username = ?, channel_id = COALESCE(?, channel_id),
                            claimed_at = ?, expires_at = ?, released_at = NULL, release_reason = NULL,
                            metadata = COALESCE(?, metadata)
                        WHERE id = ?
                        """,
                        (
                            claimer_user_id,
                            claimer_username,
                            channel_id,
                            now_sql,
                            expires_sql,
                            claim_meta_json,
                            existing_id,
                        ),
                    )
                    row = conn.execute(
                        """
                        SELECT id, source_type, source_id, channel_id, claimed_by_user_id,
                               claimed_by_username, claimed_at, expires_at, released_at,
                               release_reason, metadata
                        FROM mention_claims WHERE id = ?
                        """,
                        (existing_id,),
                    ).fetchone()
                    conn.commit()
                    return {'claimed': True, 'reason': 'taken_over', 'claim': self._serialize_claim_row(row, now_dt=now_dt)}

                existing_any = conn.execute(
                    """
                    SELECT id
                    FROM mention_claims
                    WHERE source_type = ? AND source_id = ?
                    LIMIT 1
                    """,
                    (source_type, source_id),
                ).fetchone()
                if existing_any:
                    existing_id = existing_any['id']
                    conn.execute(
                        """
                        UPDATE mention_claims
                        SET claimed_by_user_id = ?, claimed_by_username = ?, channel_id = COALESCE(?, channel_id),
                            claimed_at = ?, expires_at = ?, released_at = NULL, release_reason = NULL,
                            metadata = COALESCE(?, metadata)
                        WHERE id = ?
                        """,
                        (
                            claimer_user_id,
                            claimer_username,
                            channel_id,
                            now_sql,
                            expires_sql,
                            claim_meta_json,
                            existing_id,
                        ),
                    )
                    row = conn.execute(
                        """
                        SELECT id, source_type, source_id, channel_id, claimed_by_user_id,
                               claimed_by_username, claimed_at, expires_at, released_at,
                               release_reason, metadata
                        FROM mention_claims WHERE id = ?
                        """,
                        (existing_id,),
                    ).fetchone()
                    conn.commit()
                    return {'claimed': True, 'reason': 'reclaimed', 'claim': self._serialize_claim_row(row, now_dt=now_dt)}

                claim_id = f"MCL{secrets.token_hex(8)}"
                conn.execute(
                    """
                    INSERT INTO mention_claims
                    (id, source_type, source_id, channel_id, claimed_by_user_id,
                     claimed_by_username, claimed_at, expires_at, metadata)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        claim_id,
                        source_type,
                        source_id,
                        channel_id,
                        claimer_user_id,
                        claimer_username,
                        now_sql,
                        expires_sql,
                        claim_meta_json,
                    ),
                )
                row = conn.execute(
                    """
                    SELECT id, source_type, source_id, channel_id, claimed_by_user_id,
                           claimed_by_username, claimed_at, expires_at, released_at,
                           release_reason, metadata
                    FROM mention_claims WHERE id = ?
                    """,
                    (claim_id,),
                ).fetchone()
                conn.commit()
                return {'claimed': True, 'reason': 'claimed', 'claim': self._serialize_claim_row(row, now_dt=now_dt)}
        except Exception as e:
            # Backward-compatible race handling: if another actor won the unique
            # claim insert between our read and write, surface the semantic
            # loser-path payload instead of a generic 400-style error.
            err_lower = str(e).lower()
            unique_conflict = (
                "unique constraint failed" in err_lower
                and "mention_claims" in err_lower
            )
            if unique_conflict:
                try:
                    with self.db.get_connection() as conn:
                        conflict_row = self._get_active_claim_row(conn, source_type, source_id)
                        conn.commit()
                    if conflict_row:
                        return {
                            'claimed': False,
                            'reason': 'already_claimed',
                            'claim': self._serialize_claim_row(conflict_row, now_dt=now_dt),
                        }
                except Exception as lookup_err:
                    logger.warning(
                        "Mention claim conflict recovery lookup failed for %s:%s: %s",
                        source_type,
                        source_id,
                        lookup_err,
                    )
            logger.error(f"Failed to claim mention source {source_type}:{source_id}: {e}")
            return {'claimed': False, 'reason': 'error', 'error': str(e), 'claim': None}

    def release_claim(
        self,
        source_type: str,
        source_id: str,
        claimer_user_id: Optional[str] = None,
        reason: str = 'released',
        force: bool = False,
    ) -> Dict[str, Any]:
        """Release an active claim."""
        if not source_type or not source_id:
            return {'released': False, 'reason': 'invalid_request', 'claim': None}
        now_dt = datetime.now(timezone.utc)
        now_sql = _sqlite_timestamp(now_dt)
        try:
            with self.db.get_connection() as conn:
                row = self._get_active_claim_row(conn, source_type, source_id)
                if not row:
                    conn.commit()
                    return {'released': False, 'reason': 'not_claimed', 'claim': None}
                owner = row['claimed_by_user_id']
                if not force and claimer_user_id and owner != claimer_user_id:
                    conn.commit()
                    return {'released': False, 'reason': 'not_owner', 'claim': self._serialize_claim_row(row, now_dt=now_dt)}
                conn.execute(
                    """
                    UPDATE mention_claims
                    SET released_at = ?, release_reason = ?
                    WHERE id = ? AND released_at IS NULL
                    """,
                    (now_sql, reason, row['id']),
                )
                updated_row = conn.execute(
                    """
                    SELECT id, source_type, source_id, channel_id, claimed_by_user_id,
                           claimed_by_username, claimed_at, expires_at, released_at,
                           release_reason, metadata
                    FROM mention_claims WHERE id = ?
                    """,
                    (row['id'],),
                ).fetchone()
                conn.commit()
            return {'released': True, 'reason': reason, 'claim': self._serialize_claim_row(updated_row, now_dt=now_dt)}
        except Exception as e:
            logger.error(f"Failed to release mention claim {source_type}:{source_id}: {e}")
            return {'released': False, 'reason': 'error', 'error': str(e), 'claim': None}

    def release_claims_for_mentions(self, user_id: str, mention_ids: Sequence[str], reason: str = 'acknowledged') -> int:
        """Release active claims owned by user for the given mention IDs."""
        ids = [str(mid).strip() for mid in (mention_ids or []) if mid and str(mid).strip()]
        if not user_id or not ids:
            return 0
        released = 0
        try:
            with self.db.get_connection() as conn:
                placeholders = ",".join("?" for _ in ids)
                refs = conn.execute(
                    f"""
                    SELECT DISTINCT source_type, source_id
                    FROM mention_events
                    WHERE user_id = ? AND id IN ({placeholders})
                    """,
                    [user_id] + ids,
                ).fetchall()
                if not refs:
                    conn.commit()
                    return 0
                now_sql = _sqlite_timestamp(datetime.now(timezone.utc))
                for ref in refs:
                    cur = conn.execute(
                        """
                        UPDATE mention_claims
                        SET released_at = ?, release_reason = ?
                        WHERE source_type = ? AND source_id = ?
                          AND released_at IS NULL
                          AND claimed_by_user_id = ?
                        """,
                        (
                            now_sql,
                            reason,
                            ref['source_type'],
                            ref['source_id'],
                            user_id,
                        ),
                    )
                    released += cur.rowcount or 0
                conn.commit()
            return released
        except Exception as e:
            logger.warning(f"Failed to release mention claims after ack: {e}")
            return released

    def record_mentions(
        self,
        user_ids: Sequence[str],
        source_type: str,
        source_id: str,
        author_id: Optional[str] = None,
        origin_peer: Optional[str] = None,
        channel_id: Optional[str] = None,
        preview: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> List[str]:
        """Insert mention events for the given user IDs."""
        if not user_ids or not source_id:
            return []

        inserted: List[str] = []
        inserted_pairs: List[Tuple[str, str]] = []
        unique_users = list(dict.fromkeys([uid for uid in user_ids if uid]))
        meta_json = json.dumps(metadata) if metadata else None
        base_time = datetime.now(timezone.utc)
        try:
            with self.db.get_connection() as conn:
                for idx, uid in enumerate(unique_users):
                    mention_id = f"MN{secrets.token_hex(8)}"
                    created_at = (base_time + timedelta(microseconds=idx)).strftime('%Y-%m-%d %H:%M:%S.%f')
                    cur = conn.execute(
                        """
                        INSERT OR IGNORE INTO mention_events
                        (id, user_id, source_type, source_id, author_id, origin_peer,
                         channel_id, preview, metadata, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            mention_id, uid, source_type, source_id,
                            author_id, origin_peer, channel_id,
                            preview or '', meta_json, created_at
                        ),
                    )
                    if cur.rowcount:
                        inserted.append(mention_id)
                        inserted_pairs.append((mention_id, uid))
                conn.commit()
        except Exception as e:
            logger.error(f"Failed to record mention events: {e}")
        if inserted and self.workspace_events:
            meta_base: Dict[str, Any] = dict(metadata or {})
            if channel_id:
                meta_base.setdefault('channel_id', channel_id)
            if author_id:
                meta_base.setdefault('author_id', author_id)
            if origin_peer:
                meta_base.setdefault('origin_peer', origin_peer)
            if source_type == 'channel_message':
                meta_base.setdefault('message_id', source_id)
            elif source_type == 'feed_post':
                meta_base.setdefault('post_id', source_id)
            for mention_id, uid in inserted_pairs:
                self.workspace_events.emit_event(
                    event_type=EVENT_MENTION_CREATED,
                    actor_user_id=author_id,
                    target_user_id=uid,
                    channel_id=channel_id,
                    message_id=source_id if source_type == 'channel_message' else None,
                    post_id=source_id if source_type == 'feed_post' else None,
                    visibility_scope='user',
                    dedupe_key=f"{EVENT_MENTION_CREATED}:{mention_id}",
                    payload={
                        'mention_id': mention_id,
                        'source_type': source_type,
                        'source_id': source_id,
                        'preview': preview or '',
                        'metadata': meta_base,
                    },
                    created_at=base_time,
                )
        return inserted

    def sync_source_mentions(
        self,
        *,
        source_type: str,
        source_id: str,
        target_ids: Optional[Sequence[str]] = None,
        author_id: Optional[str] = None,
        origin_peer: Optional[str] = None,
        channel_id: Optional[str] = None,
        preview: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        source_content: Optional[str] = None,
        mark_missing_as_stale: bool = False,
    ) -> Dict[str, int]:
        """Refresh stored mention-event payloads for an edited source."""
        if not source_type or not source_id:
            return {'updated': 0, 'created': 0, 'stale_marked': 0}

        desired_ids = list(dict.fromkeys([str(uid).strip() for uid in (target_ids or []) if str(uid).strip()]))
        desired_set = set(desired_ids)
        meta_base: Dict[str, Any] = dict(metadata or {})
        if source_content is not None:
            meta_base['content'] = source_content
        if channel_id:
            meta_base['channel_id'] = channel_id
        if author_id:
            meta_base['author_id'] = author_id
        if origin_peer:
            meta_base['origin_peer'] = origin_peer
        if source_type == 'channel_message':
            meta_base.setdefault('message_id', source_id)
        elif source_type == 'feed_post':
            meta_base.setdefault('post_id', source_id)

        updated = 0
        created = 0
        stale_marked = 0
        existing_user_ids: set[str] = set()
        now_iso = datetime.now(timezone.utc).isoformat()

        try:
            with self.db.get_connection() as conn:
                rows = conn.execute(
                    """
                    SELECT id, user_id, metadata
                    FROM mention_events
                    WHERE source_type = ? AND source_id = ?
                    """,
                    (source_type, source_id),
                ).fetchall()

                for row in rows or []:
                    user_id = str(row['user_id'] or '').strip()
                    if not user_id:
                        continue
                    existing_user_ids.add(user_id)

                    merged_meta: Dict[str, Any] = {}
                    meta_raw = row['metadata']
                    if meta_raw:
                        try:
                            loaded = json.loads(meta_raw)
                            if isinstance(loaded, dict):
                                merged_meta = loaded
                        except Exception:
                            merged_meta = {}
                    merged_meta.update(meta_base)

                    if mark_missing_as_stale:
                        still_mentioned = user_id in desired_set
                        merged_meta['still_mentioned'] = still_mentioned
                        if still_mentioned:
                            merged_meta.pop('mention_removed_at', None)
                        else:
                            merged_meta['mention_removed_at'] = now_iso
                            stale_marked += 1
                    elif user_id in desired_set:
                        merged_meta.pop('mention_removed_at', None)

                    conn.execute(
                        """
                        UPDATE mention_events
                        SET preview = COALESCE(?, preview),
                            metadata = ?
                        WHERE id = ?
                        """,
                        (
                            preview,
                            json.dumps(merged_meta) if merged_meta else None,
                            row['id'],
                        ),
                    )
                    updated += 1
                conn.commit()
        except Exception as e:
            logger.warning(f"Failed to refresh mention-event payloads: {e}")
            return {'updated': updated, 'created': created, 'stale_marked': stale_marked}

        missing_ids = [uid for uid in desired_ids if uid not in existing_user_ids]
        if missing_ids:
            create_meta = dict(meta_base)
            if mark_missing_as_stale:
                create_meta['still_mentioned'] = True
            created_ids = self.record_mentions(
                user_ids=missing_ids,
                source_type=source_type,
                source_id=source_id,
                author_id=author_id,
                origin_peer=origin_peer,
                channel_id=channel_id,
                preview=preview,
                metadata=create_meta,
            )
            created = len(created_ids)

        return {'updated': updated, 'created': created, 'stale_marked': stale_marked}

    def get_mentions(
        self,
        user_id: str,
        since: Optional[Any] = None,
        limit: int = 50,
        include_acknowledged: bool = False,
    ) -> List[Dict[str, Any]]:
        """Fetch mention events for a user."""
        if not user_id:
            return []

        since_db = None
        if since is not None and since != "":
            try:
                if isinstance(since, (int, float)):
                    dt = datetime.fromtimestamp(float(since), tz=timezone.utc)
                else:
                    s = str(since)
                    if s.isdigit():
                        dt = datetime.fromtimestamp(float(s), tz=timezone.utc)
                    else:
                        dt = datetime.fromisoformat(s.replace('Z', '+00:00'))
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=timezone.utc)
                since_db = dt.strftime('%Y-%m-%d %H:%M:%S')
            except Exception:
                since_db = None

        limit_val = max(1, min(int(limit or 50), 200))

        try:
            with self.db.get_connection() as conn:
                query = """
                    SELECT me.id, me.user_id, me.source_type, me.source_id, me.author_id,
                           me.origin_peer, me.channel_id, me.preview, me.metadata,
                           me.created_at, me.acknowledged_at, me.status,
                           mc.id AS claim_id, mc.claimed_by_user_id, mc.claimed_by_username,
                           mc.claimed_at, mc.expires_at, mc.released_at, mc.release_reason,
                           mc.metadata AS claim_metadata
                    FROM mention_events me
                    LEFT JOIN mention_claims mc
                      ON mc.source_type = me.source_type
                     AND mc.source_id = me.source_id
                     AND mc.released_at IS NULL
                    WHERE user_id = ?
                """
                params: List[Any] = [user_id]
                if not include_acknowledged:
                    query += " AND acknowledged_at IS NULL"
                if since_db:
                    query += " AND created_at > ?"
                    params.append(since_db)
                query += " ORDER BY created_at DESC LIMIT ?"
                params.append(limit_val)
                rows = conn.execute(query, params).fetchall()
        except Exception as e:
            logger.error(f"Failed to fetch mention events: {e}")
            return []

        results: List[Dict[str, Any]] = []
        for row in rows:
            try:
                metadata = json.loads(row['metadata']) if row['metadata'] else None
            except Exception:
                metadata = None

            claim = None
            claim_id = row['claim_id'] if hasattr(row, '__getitem__') else row[12]
            if claim_id:
                claim_metadata_raw = row['claim_metadata'] if hasattr(row, '__getitem__') else row[19]
                claim_metadata = None
                if claim_metadata_raw:
                    try:
                        claim_metadata = json.loads(claim_metadata_raw)
                    except Exception:
                        claim_metadata = None
                claim_expires_at = _to_datetime_utc(row['expires_at'] if hasattr(row, '__getitem__') else row[16])
                claim_released_at = _to_datetime_utc(row['released_at'] if hasattr(row, '__getitem__') else row[17])
                claim_is_active = bool(
                    claim_expires_at and
                    claim_expires_at > datetime.now(timezone.utc) and
                    not claim_released_at
                )
                claim_user_id = row['claimed_by_user_id'] if hasattr(row, '__getitem__') else row[13]
                claim = {
                    'id': claim_id,
                    'claimed_by_user_id': claim_user_id,
                    'claimed_by_username': row['claimed_by_username'] if hasattr(row, '__getitem__') else row[14],
                    'claimed_at': _to_iso_utc(row['claimed_at'] if hasattr(row, '__getitem__') else row[15]),
                    'expires_at': _to_iso_utc(row['expires_at'] if hasattr(row, '__getitem__') else row[16]),
                    'released_at': _to_iso_utc(row['released_at'] if hasattr(row, '__getitem__') else row[17]),
                    'release_reason': row['release_reason'] if hasattr(row, '__getitem__') else row[18],
                    'metadata': claim_metadata,
                    'active': claim_is_active,
                    'claimed_by_me': bool(claim_user_id and claim_user_id == user_id),
                }

            results.append({
                'id': row['id'] if hasattr(row, '__getitem__') else row[0],
                'user_id': row['user_id'] if hasattr(row, '__getitem__') else row[1],
                'source_type': row['source_type'] if hasattr(row, '__getitem__') else row[2],
                'source_id': row['source_id'] if hasattr(row, '__getitem__') else row[3],
                'author_id': row['author_id'] if hasattr(row, '__getitem__') else row[4],
                'origin_peer': row['origin_peer'] if hasattr(row, '__getitem__') else row[5],
                'channel_id': row['channel_id'] if hasattr(row, '__getitem__') else row[6],
                'preview': row['preview'] if hasattr(row, '__getitem__') else row[7],
                'metadata': metadata,
                'created_at': _to_iso_utc(row['created_at'] if hasattr(row, '__getitem__') else row[9]),
                'acknowledged_at': _to_iso_utc(row['acknowledged_at'] if hasattr(row, '__getitem__') else row[10]),
                'status': row['status'] if hasattr(row, '__getitem__') else row[11],
                'claim': claim,
            })
        return results

    def get_mention_by_id(self, mention_id: str) -> Optional[Dict[str, Any]]:
        """Fetch a single mention event by ID."""
        if not mention_id:
            return None
        try:
            with self.db.get_connection() as conn:
                row = conn.execute(
                    """
                    SELECT me.id, me.user_id, me.source_type, me.source_id, me.author_id,
                           me.origin_peer, me.channel_id, me.preview, me.metadata,
                           me.created_at, me.acknowledged_at, me.status,
                           mc.id AS claim_id, mc.claimed_by_user_id, mc.claimed_by_username,
                           mc.claimed_at, mc.expires_at, mc.released_at, mc.release_reason,
                           mc.metadata AS claim_metadata
                    FROM mention_events me
                    LEFT JOIN mention_claims mc
                      ON mc.source_type = me.source_type
                     AND mc.source_id = me.source_id
                     AND mc.released_at IS NULL
                    WHERE me.id = ?
                    """,
                    (mention_id,),
                ).fetchone()
            if not row:
                return None
        except Exception as e:
            logger.error(f"Failed to fetch mention event {mention_id}: {e}")
            return None

        try:
            metadata = json.loads(row['metadata']) if row['metadata'] else None
        except Exception:
            metadata = None

        claim = None
        claim_id = row['claim_id'] if hasattr(row, '__getitem__') else row[12]
        if claim_id:
            claim_metadata_raw = row['claim_metadata'] if hasattr(row, '__getitem__') else row[19]
            claim_metadata = None
            if claim_metadata_raw:
                try:
                    claim_metadata = json.loads(claim_metadata_raw)
                except Exception:
                    claim_metadata = None
            claim_expires_at = _to_datetime_utc(row['expires_at'] if hasattr(row, '__getitem__') else row[16])
            claim_released_at = _to_datetime_utc(row['released_at'] if hasattr(row, '__getitem__') else row[17])
            claim = {
                'id': claim_id,
                'claimed_by_user_id': row['claimed_by_user_id'] if hasattr(row, '__getitem__') else row[13],
                'claimed_by_username': row['claimed_by_username'] if hasattr(row, '__getitem__') else row[14],
                'claimed_at': _to_iso_utc(row['claimed_at'] if hasattr(row, '__getitem__') else row[15]),
                'expires_at': _to_iso_utc(row['expires_at'] if hasattr(row, '__getitem__') else row[16]),
                'released_at': _to_iso_utc(row['released_at'] if hasattr(row, '__getitem__') else row[17]),
                'release_reason': row['release_reason'] if hasattr(row, '__getitem__') else row[18],
                'metadata': claim_metadata,
                'active': bool(
                    claim_expires_at and
                    claim_expires_at > datetime.now(timezone.utc) and
                    not claim_released_at
                ),
            }

        return {
            'id': row['id'] if hasattr(row, '__getitem__') else row[0],
            'user_id': row['user_id'] if hasattr(row, '__getitem__') else row[1],
            'source_type': row['source_type'] if hasattr(row, '__getitem__') else row[2],
            'source_id': row['source_id'] if hasattr(row, '__getitem__') else row[3],
            'author_id': row['author_id'] if hasattr(row, '__getitem__') else row[4],
            'origin_peer': row['origin_peer'] if hasattr(row, '__getitem__') else row[5],
            'channel_id': row['channel_id'] if hasattr(row, '__getitem__') else row[6],
            'preview': row['preview'] if hasattr(row, '__getitem__') else row[7],
            'metadata': metadata,
            'created_at': _to_iso_utc(row['created_at'] if hasattr(row, '__getitem__') else row[9]),
            'acknowledged_at': _to_iso_utc(row['acknowledged_at'] if hasattr(row, '__getitem__') else row[10]),
            'status': row['status'] if hasattr(row, '__getitem__') else row[11],
            'claim': claim,
        }

    def acknowledge_mentions(self, user_id: str, mention_ids: Sequence[str]) -> int:
        """Mark mention events as acknowledged."""
        ids = [str(mid).strip() for mid in (mention_ids or []) if mid and str(mid).strip()]
        if not user_id or not ids:
            return 0
        released_claims = 0
        try:
            with self.db.get_connection() as conn:
                placeholders = ",".join("?" for _ in ids)
                ack_rows = conn.execute(
                    f"""
                    SELECT id, source_type, source_id, channel_id, preview
                    FROM mention_events
                    WHERE user_id = ? AND id IN ({placeholders})
                    """,
                    [user_id] + ids,
                ).fetchall()
                now_iso = datetime.now(timezone.utc).isoformat()
                params = [user_id] + ids
                cur = conn.execute(
                    f"""
                    UPDATE mention_events
                    SET acknowledged_at = ?, status = 'acknowledged'
                    WHERE user_id = ? AND id IN ({placeholders})
                    """,
                    [now_iso] + params,
                )
                updated = cur.rowcount or 0
                if updated == 0 and ids:
                    # Diagnose: do these ids exist and for which user_id?
                    check = conn.execute(
                        f"""
                        SELECT id, user_id FROM mention_events
                        WHERE id IN ({placeholders})
                        """,
                        ids,
                    ).fetchall()
                    if not check:
                        logger.warning(
                            "Mention ack: no rows found for ids=%s (ids may be invalid or from another instance)",
                            ids[:5],
                        )
                    else:
                        sample_user = check[0]["user_id"] if hasattr(check[0], "__getitem__") else check[0][1]
                        logger.warning(
                            "Mention ack: 0 updated for request user_id=%r; sample row user_id=%r (possible user_id mismatch)",
                            user_id,
                            sample_user,
                        )
                if updated > 0:
                    try:
                        now_sql = _sqlite_timestamp(datetime.now(timezone.utc))
                        ref_rows = conn.execute(
                            f"""
                            SELECT DISTINCT source_type, source_id
                            FROM mention_events
                            WHERE user_id = ? AND id IN ({placeholders})
                            """,
                            [user_id] + ids,
                        ).fetchall()
                        for ref in ref_rows or []:
                            rel_cur = conn.execute(
                                """
                                UPDATE mention_claims
                                SET released_at = ?, release_reason = 'acknowledged'
                                WHERE source_type = ? AND source_id = ?
                                  AND released_at IS NULL
                                  AND claimed_by_user_id = ?
                                """,
                                (
                                    now_sql,
                                    ref['source_type'],
                                    ref['source_id'],
                                    user_id,
                                ),
                            )
                            released_claims += rel_cur.rowcount or 0
                    except Exception as release_err:
                        logger.debug(f"Mention claim release on ack failed: {release_err}")
                conn.commit()
                if released_claims:
                    logger.debug(
                        "Mention ack released %d claim(s) for user_id=%s",
                        released_claims,
                        user_id,
                    )
                if updated and self.workspace_events:
                    for row in ack_rows or []:
                        self.workspace_events.emit_event(
                            event_type=EVENT_MENTION_ACKNOWLEDGED,
                            actor_user_id=user_id,
                            target_user_id=user_id,
                            channel_id=row['channel_id'],
                            message_id=row['source_id'] if row['source_type'] == 'channel_message' else None,
                            post_id=row['source_id'] if row['source_type'] == 'feed_post' else None,
                            visibility_scope='user',
                            dedupe_key=f"{EVENT_MENTION_ACKNOWLEDGED}:{row['id']}:{now_iso}",
                            created_at=now_iso,
                            payload={
                                'mention_id': row['id'],
                                'source_type': row['source_type'],
                                'source_id': row['source_id'],
                                'preview': row['preview'] or '',
                                'acknowledged_at': now_iso,
                            },
                        )
                return updated
        except Exception as e:
            logger.error(f"Failed to acknowledge mention events: {e}")
            return 0


def record_mention_activity(
    mention_manager: Optional[MentionManager],
    p2p_manager: Any,
    target_ids: Sequence[str],
    source_type: str,
    source_id: str,
    author_id: Optional[str],
    origin_peer: Optional[str],
    channel_id: Optional[str],
    preview: str,
    extra_ref: Optional[Dict[str, Any]] = None,
    inbox_manager: Any = None,
    source_content: Optional[str] = None,
) -> None:
    """Persist mention events and surface a UI activity notification."""
    resolved_target_ids = list(dict.fromkeys([tid for tid in target_ids if tid]))
    if source_type == 'channel_message' and channel_id:
        resolved_target_ids = _filter_channel_target_ids_for_notifications(
            mention_manager,
            channel_id=channel_id,
            source_id=source_id,
            target_ids=resolved_target_ids,
        )
        if not resolved_target_ids:
            return

    if mention_manager and resolved_target_ids:
        mention_manager.record_mentions(
            user_ids=resolved_target_ids,
            source_type=source_type,
            source_id=source_id,
            author_id=author_id,
            origin_peer=origin_peer,
            channel_id=channel_id,
            preview=preview,
            metadata=extra_ref,
        )

    if resolved_target_ids and not inbox_manager:
        logger.warning(
            "Inbox skipped: INBOX_MANAGER not configured (mention targets=%s, source_type=%s, source_id=%s)",
            list(resolved_target_ids), source_type, source_id,
        )
    if inbox_manager and resolved_target_ids:
        try:
            inserted = inbox_manager.record_mention_triggers(
                target_ids=resolved_target_ids,
                source_type=source_type,
                source_id=source_id,
                author_id=author_id,
                origin_peer=origin_peer,
                channel_id=channel_id,
                preview=preview,
                extra_ref=extra_ref,
                source_content=source_content,
            )
            if inserted == 0:
                logger.info(
                    "Inbox: 0 triggers created for %d target(s) (source_type=%s, source_id=%s, targets=%s) "
                    "- check agent_inbox_audit for rejection reasons",
                    len(resolved_target_ids), source_type, source_id, list(resolved_target_ids),
                )
        except Exception as e:
            logger.warning(
                "Inbox trigger creation failed: %s (source_type=%s, source_id=%s, targets=%s)",
                e, source_type, source_id, list(resolved_target_ids),
            )

    if p2p_manager and resolved_target_ids:
        try:
            ref = dict(extra_ref or {})
            ref.setdefault('mention_targets', list(resolved_target_ids))
            if source_type == 'channel_message':
                ref.setdefault('message_id', source_id)
                ref.setdefault('channel_id', channel_id)
                ref.setdefault('user_id', author_id)
            elif source_type == 'feed_post':
                ref.setdefault('post_id', source_id)
                ref.setdefault('author_id', author_id)

            event_id = f"MN:{source_id or secrets.token_hex(6)}"
            p2p_manager.record_activity_event({
                'id': event_id,
                'peer_id': origin_peer or '',
                'kind': 'mention',
                'timestamp': datetime.now(timezone.utc).timestamp(),
                'preview': preview,
                'ref': ref,
            })
        except Exception:
            pass


def record_thread_reply_activity(
    *,
    channel_manager: Any,
    inbox_manager: Any,
    channel_id: str,
    reply_message_id: str,
    parent_message_id: str,
    author_id: Optional[str],
    origin_peer: Optional[str],
    source_content: Optional[str] = None,
    preview: Optional[str] = None,
    mentioned_user_ids: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """Create inbox reply triggers for thread subscribers.

    Behavior:
    - If no explicit subscription exists for the thread root author and that
      author has `auto_subscribe_own_threads=true`, create an auto subscription.
    - Notify subscribed users on reply (`trigger_type='reply'`), excluding
      the reply author and users already explicitly @mentioned in the reply.
    """
    result: Dict[str, Any] = {
        'thread_root_message_id': None,
        'root_author_id': None,
        'target_user_ids': [],
        'notified_count': 0,
    }
    if not inbox_manager or not channel_manager:
        return result
    if not channel_id or not reply_message_id or not parent_message_id:
        return result

    mentioned = {
        str(uid).strip() for uid in (mentioned_user_ids or []) if str(uid).strip()
    }
    author = str(author_id).strip() if author_id else ''

    try:
        thread_root_id = (
            channel_manager.resolve_thread_root_message_id(channel_id, parent_message_id)
            or parent_message_id
        )
        if not thread_root_id:
            return result
        result['thread_root_message_id'] = thread_root_id

        root_author_id = channel_manager.get_thread_root_author_id(channel_id, thread_root_id)
        if root_author_id:
            root_author_id = str(root_author_id).strip()
        result['root_author_id'] = root_author_id

        target_ids: set[str] = set(
            channel_manager.get_thread_subscriber_ids(channel_id, thread_root_id) or []
        )

        # Root authors are subscribed by default unless they explicitly mute.
        if root_author_id and root_author_id != author:
            root_state = channel_manager.get_thread_subscription_state(
                root_author_id,
                channel_id,
                thread_root_id,
            )
            explicit = root_state.get('explicit_subscribed')
            if explicit is True:
                target_ids.add(root_author_id)
            elif explicit is None:
                root_config = inbox_manager.get_config(root_author_id) if inbox_manager else {}
                auto_subscribe = bool(root_config.get('auto_subscribe_own_threads', True))
                if auto_subscribe:
                    upsert = channel_manager.set_thread_subscription(
                        root_author_id,
                        channel_id,
                        thread_root_id,
                        True,
                        source='auto',
                        require_membership=False,
                    )
                    if upsert.get('success'):
                        target_ids.add(root_author_id)

        channel_mute_map: Dict[str, bool] = {}
        try:
            candidate_ids = sorted({str(uid).strip() for uid in target_ids if str(uid).strip()})
            if candidate_ids:
                placeholders = ",".join("?" for _ in candidate_ids)
                with channel_manager.db.get_connection() as conn:
                    try:
                        pref_rows = conn.execute(
                            f"""
                            SELECT user_id, notifications_enabled
                            FROM channel_members
                            WHERE channel_id = ? AND user_id IN ({placeholders})
                            """,
                            [channel_id] + candidate_ids,
                        ).fetchall()
                    except Exception:
                        pref_rows = conn.execute(
                            f"""
                            SELECT user_id, 1 AS notifications_enabled
                            FROM channel_members
                            WHERE channel_id = ? AND user_id IN ({placeholders})
                            """,
                            [channel_id] + candidate_ids,
                        ).fetchall()
                for row in pref_rows:
                    uid = row['user_id'] if hasattr(row, 'keys') and 'user_id' in row.keys() else row[0]
                    enabled_raw = (
                        row['notifications_enabled']
                        if hasattr(row, 'keys') and 'notifications_enabled' in row.keys()
                        else (row[1] if len(row) > 1 else 1)
                    )
                    if enabled_raw is None:
                        enabled = True
                    elif isinstance(enabled_raw, str):
                        enabled = enabled_raw.strip().lower() not in {'0', 'false', 'off', 'no'}
                    else:
                        enabled = bool(enabled_raw)
                    channel_mute_map[str(uid).strip()] = enabled
        except Exception:
            channel_mute_map = {}

        final_targets: List[str] = []
        for uid in sorted(target_ids):
            clean_uid = str(uid).strip()
            if not clean_uid or clean_uid == author or clean_uid in mentioned:
                continue
            if clean_uid in channel_mute_map and not channel_mute_map.get(clean_uid, True):
                continue
            cfg = inbox_manager.get_config(clean_uid)
            if not bool(cfg.get('thread_reply_notifications', True)):
                continue
            final_targets.append(clean_uid)

        if not final_targets:
            return result

        preview_text = build_preview(source_content or preview or '')
        inserted = inbox_manager.record_mention_triggers(
            target_ids=final_targets,
            source_type='channel_message',
            source_id=reply_message_id,
            author_id=author or None,
            origin_peer=origin_peer,
            channel_id=channel_id,
            preview=preview_text,
            extra_ref={
                'channel_id': channel_id,
                'message_id': reply_message_id,
                'parent_message_id': parent_message_id,
                'thread_root_message_id': thread_root_id,
            },
            source_content=source_content,
            trigger_type='reply',
        )
        result['target_user_ids'] = final_targets
        result['notified_count'] = int(inserted or 0)
        return result
    except Exception as e:
        logger.debug(
            "Thread reply notification skipped (channel=%s reply=%s parent=%s): %s",
            channel_id,
            reply_message_id,
            parent_message_id,
            e,
        )
        return result


def broadcast_mention_interaction(
    p2p_manager: Any,
    source_type: str,
    source_id: str,
    author_id: str,
    target_user_ids: Sequence[str],
    preview: str,
    channel_id: Optional[str] = None,
    origin_peer: Optional[str] = None,
) -> None:
    """Send mention events to peers via the interaction channel."""
    if not p2p_manager or not target_user_ids:
        return

    extra = {
        'action': 'mention',
        'source_type': source_type,
        'source_id': source_id,
        'author_id': author_id,
        'target_user_ids': list(dict.fromkeys([tid for tid in target_user_ids if tid])),
        'preview': preview,
        'origin_peer': origin_peer,
    }
    if channel_id:
        extra['channel_id'] = channel_id

    try:
        p2p_manager.broadcast_interaction(
            item_id=source_id,
            user_id=author_id,
            action='mention',
            item_type=source_type,
            extra=extra,
        )
    except Exception:
        pass


def sync_edited_mention_activity(
    *,
    db_manager: Any,
    mention_manager: Optional[MentionManager],
    inbox_manager: Any,
    p2p_manager: Any,
    content: Optional[str],
    source_type: str,
    source_id: str,
    author_id: Optional[str],
    origin_peer: Optional[str],
    channel_id: Optional[str] = None,
    visibility: Optional[str] = None,
    permissions: Optional[Sequence[str]] = None,
    edited_at: Optional[str] = None,
) -> Dict[str, Any]:
    """Refresh local mention/inbox payloads after an edited source changes."""
    text = str(content or '')
    mentions = extract_mentions(text)
    preview = build_preview(text or '') or None
    extra_ref: Dict[str, Any] = {}
    if channel_id:
        extra_ref['channel_id'] = channel_id
    if source_type == 'channel_message':
        extra_ref['message_id'] = source_id
    elif source_type == 'feed_post':
        extra_ref['post_id'] = source_id
    if edited_at:
        extra_ref['edited_at'] = edited_at

    targets = resolve_mention_targets(
        db_manager,
        mentions,
        channel_id=channel_id,
        visibility=visibility,
        permissions=permissions,
        author_id=author_id,
    ) if mentions else []

    local_peer_id = None
    try:
        if p2p_manager:
            local_peer_id = p2p_manager.get_peer_id()
    except Exception:
        local_peer_id = None
    local_targets, remote_targets = split_mention_targets(targets, local_peer_id=local_peer_id)
    local_target_ids = [
        cast(str, t.get('user_id'))
        for t in local_targets
        if t.get('user_id')
    ]
    remote_target_ids = [
        cast(str, t.get('user_id'))
        for t in remote_targets
        if t.get('user_id')
    ]

    if source_type == 'channel_message' and channel_id:
        local_target_ids = _filter_channel_target_ids_for_notifications(
            mention_manager,
            channel_id=channel_id,
            source_id=source_id,
            target_ids=local_target_ids,
        )

    if mention_manager:
        try:
            mention_manager.sync_source_mentions(
                source_type=source_type,
                source_id=source_id,
                target_ids=local_target_ids,
                author_id=author_id,
                origin_peer=origin_peer,
                channel_id=channel_id,
                preview=preview,
                metadata=extra_ref,
                source_content=text,
                mark_missing_as_stale=True,
            )
        except Exception as e:
            logger.warning(
                "Mention-event refresh failed: %s (source_type=%s source_id=%s)",
                e,
                source_type,
                source_id,
            )

    if inbox_manager:
        try:
            inbox_manager.sync_source_triggers(
                source_type=source_type,
                source_id=source_id,
                trigger_type='mention',
                target_ids=local_target_ids,
                sender_user_id=author_id,
                origin_peer=origin_peer,
                channel_id=channel_id,
                preview=preview,
                payload=extra_ref,
                source_content=text,
                mark_missing_as_stale=True,
            )
        except Exception as e:
            logger.warning(
                "Inbox mention refresh failed: %s (source_type=%s source_id=%s)",
                e,
                source_type,
                source_id,
            )

    return {
        'local_target_ids': local_target_ids,
        'remote_target_ids': remote_target_ids,
        'preview': preview,
    }
