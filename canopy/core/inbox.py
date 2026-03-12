"""
Agent action inbox for Canopy.

Stores per-agent trigger items (e.g., @mentions) so agents can
pull pending actions without scanning every post.

Author: Konrad Walus (architecture, design, and direction)
Project: Canopy - Local Mesh Communication
License: Apache 2.0
Development: AI-assisted implementation (Claude, Codex, GitHub Copilot, Cursor IDE, Ollama)
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import time
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .database import DatabaseManager
from .events import EVENT_INBOX_ITEM_CREATED, EVENT_INBOX_ITEM_UPDATED
from ..security.trust import TrustManager

logger = logging.getLogger(__name__)

# Conservative defaults for human accounts.
DEFAULT_INBOX_CONFIG: Dict[str, Any] = {
    "channels": [],
    "allowed_senders": [],
    "allowed_trigger_types": ["mention", "dm", "reply", "channel_added"],
    "thread_reply_notifications": True,
    "auto_subscribe_own_threads": True,
    "min_trust_score": 50,
    "cooldown_seconds": 10,
    "sender_cooldown_seconds": 30,
    "agent_sender_cooldown_seconds": 60,
    "channel_burst_limit": 3,
    "channel_burst_window_seconds": 60,
    "channel_hourly_limit": 20,
    "channel_hourly_window_seconds": 3600,
    "sender_hourly_limit": 20,
    "sender_hourly_window_seconds": 3600,
    "max_pending": 100,
    "expire_days": 7,
    "audit_rejections": True,
    "audit_retention_days": 7,
}

# Relaxed defaults for agent accounts (account_type='agent').
# Agents need rapid-fire mention delivery for team coordination; the
# conservative human defaults silently drop most bot-to-bot mentions.
DEFAULT_AGENT_INBOX_CONFIG: Dict[str, Any] = {
    "channels": [],
    "allowed_senders": [],
    "allowed_trigger_types": ["mention", "dm", "reply", "channel_added"],
    "thread_reply_notifications": True,
    "auto_subscribe_own_threads": True,
    # Mesh peers are implicitly trusted; TrustManager default_trust_score=100
    # so this is belt-and-suspenders, but disabling it avoids false rejections
    # when a peer hasn't been explicitly added to trust_scores yet.
    "trusted_only": False,
    "min_trust_score": 0,
    # Very short cooldowns for agent use — agents need to react quickly.
    "cooldown_seconds": 0,
    "sender_cooldown_seconds": 5,
    "agent_sender_cooldown_seconds": 10,
    # Much higher burst/hourly caps to handle active multi-agent sessions.
    "channel_burst_limit": 50,
    "channel_burst_window_seconds": 60,
    "channel_hourly_limit": 500,
    "channel_hourly_window_seconds": 3600,
    "sender_hourly_limit": 200,
    "sender_hourly_window_seconds": 3600,
    "max_pending": 500,
    "expire_days": 14,
    "audit_rejections": True,
    "audit_retention_days": 14,
}

ALLOWED_STATUSES = {"pending", "seen", "completed", "handled", "skipped", "expired"}
ACTIONABLE_STATUSES = ("pending", "seen")
TERMINAL_STATUSES = {"completed", "skipped"}
# Statuses an agent may write via PATCH.  "expired" is system-only (set by
# _enforce_capacity / expire_items) and must not be accepted from external callers.
AGENT_SETTABLE_STATUSES = {"pending", "seen", "completed", "handled", "skipped"}
MAX_TRIGGER_DEPTH = 3  # Cascade prevention: reject triggers beyond this depth


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: Optional[datetime]) -> Optional[str]:
    if not dt:
        return None
    try:
        return dt.isoformat()
    except Exception:
        return str(dt)


def _normalize_storage_status(status: Optional[str]) -> str:
    normalized = str(status or "").strip().lower()
    if normalized == "handled":
        return "completed"
    if normalized not in ALLOWED_STATUSES:
        return "pending"
    return normalized


def _normalize_output_status(status: Optional[str]) -> str:
    normalized = str(status or "").strip().lower()
    if normalized == "handled":
        return "completed"
    return normalized or "pending"


def _sanitize_completion_ref(value: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(value, dict):
        return None
    sanitized: Dict[str, Any] = {}
    for key, raw in value.items():
        token = str(key or "").strip()
        if not token:
            continue
        if raw is None:
            continue
        if isinstance(raw, (dict, list)):
            sanitized[token] = raw
            continue
        text = str(raw).strip()
        if text:
            sanitized[token] = text
    return sanitized or None


def _normalize_status_rows(value: Any) -> List[str]:
    """Normalize a status filter to one or more storage statuses."""
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        raw_values = value
    else:
        raw_values = [value]
    out: List[str] = []
    for raw in raw_values:
        normalized = _normalize_storage_status(raw)
        if normalized in ALLOWED_STATUSES and normalized not in out:
            out.append(normalized)
    return out


def _normalize_item_payload(row: Any, payload: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Backfill agent-facing payload fields from inbox columns when needed."""
    normalized: Dict[str, Any] = dict(payload or {})

    sender_user_id = str(row["sender_user_id"] or "").strip()
    channel_id = str(row["channel_id"] or "").strip()
    message_id = str(row["message_id"] or "").strip()
    trigger_type = str(row["trigger_type"] or "").strip().lower()

    if sender_user_id and not normalized.get("sender_user_id"):
        normalized["sender_user_id"] = sender_user_id
    if channel_id and not normalized.get("channel_id"):
        normalized["channel_id"] = channel_id
    if message_id and not normalized.get("message_id"):
        normalized["message_id"] = message_id
    if trigger_type and not normalized.get("trigger_type"):
        normalized["trigger_type"] = trigger_type

    if trigger_type == "dm":
        dm_thread_id = str(normalized.get("dm_thread_id") or "").strip()
        if not dm_thread_id:
            group_id = str(normalized.get("group_id") or "").strip()
            dm_thread_id = group_id or sender_user_id
        if dm_thread_id:
            normalized["dm_thread_id"] = dm_thread_id
        normalized.setdefault("reply_endpoint", "/api/v1/messages/reply")

    return normalized


class InboxManager:
    """Stores per-agent trigger inbox items."""

    # Short-lived in-process cache for account_type lookups.  The type almost
    # never changes during a session, so caching for 60 s is safe and avoids a
    # round-trip per rate-limit / cooldown check.
    _ACCOUNT_TYPE_CACHE_TTL = 60  # seconds

    def __init__(self, db_manager: DatabaseManager, trust_manager: Optional[TrustManager] = None):
        self.db = db_manager
        self.trust_manager = trust_manager
        self.workspace_events: Any = None
        # (account_type_str, fetched_at_monotonic)
        self._account_type_cache: Dict[str, Tuple[str, float]] = {}
        self._ensure_tables()

    def _ensure_tables(self) -> None:
        try:
            with self.db.get_connection() as conn:
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS agent_inbox (
                        id TEXT PRIMARY KEY,
                        agent_user_id TEXT NOT NULL,
                        source_type TEXT NOT NULL,
                        source_id TEXT NOT NULL,
                        message_id TEXT,
                        channel_id TEXT,
                        sender_user_id TEXT,
                        origin_peer TEXT,
                        trigger_type TEXT NOT NULL,
                        payload_json TEXT,
                        status TEXT DEFAULT 'pending',
                        priority TEXT DEFAULT 'normal',
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        seen_at TIMESTAMP,
                        handled_at TIMESTAMP,
                        completed_at TIMESTAMP,
                        completion_ref_json TEXT,
                        last_resolution_status TEXT,
                        last_resolution_at TIMESTAMP,
                        last_completion_ref_json TEXT,
                        expires_at TIMESTAMP,
                        triggered_by_inbox_id TEXT,
                        depth INTEGER DEFAULT 0,
                        FOREIGN KEY (agent_user_id) REFERENCES users(id)
                    );
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_inbox_unique
                        ON agent_inbox(agent_user_id, source_type, source_id, trigger_type);
                    CREATE INDEX IF NOT EXISTS idx_agent_inbox_status
                        ON agent_inbox(agent_user_id, status, created_at);
                    CREATE INDEX IF NOT EXISTS idx_agent_inbox_sender
                        ON agent_inbox(sender_user_id);
                    CREATE INDEX IF NOT EXISTS idx_agent_inbox_expires
                        ON agent_inbox(expires_at);
                    CREATE INDEX IF NOT EXISTS idx_agent_inbox_channel_created
                        ON agent_inbox(agent_user_id, channel_id, created_at);
                    CREATE INDEX IF NOT EXISTS idx_agent_inbox_sender_created
                        ON agent_inbox(agent_user_id, sender_user_id, created_at);

                    CREATE TABLE IF NOT EXISTS agent_inbox_config (
                        user_id TEXT PRIMARY KEY,
                        config_json TEXT NOT NULL,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        FOREIGN KEY (user_id) REFERENCES users(id)
                    );

                    CREATE TABLE IF NOT EXISTS agent_inbox_audit (
                        id TEXT PRIMARY KEY,
                        agent_user_id TEXT NOT NULL,
                        reason TEXT NOT NULL,
                        source_type TEXT NOT NULL,
                        source_id TEXT NOT NULL,
                        channel_id TEXT,
                        sender_user_id TEXT,
                        origin_peer TEXT,
                        trigger_type TEXT NOT NULL,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        FOREIGN KEY (agent_user_id) REFERENCES users(id)
                    );
                    CREATE INDEX IF NOT EXISTS idx_agent_inbox_audit_user
                        ON agent_inbox_audit(agent_user_id, created_at);
                    CREATE INDEX IF NOT EXISTS idx_agent_inbox_audit_reason
                        ON agent_inbox_audit(reason);
                    """
                )
                columns = {
                    str(row["name"]).strip()
                    for row in conn.execute("PRAGMA table_info(agent_inbox)").fetchall()
                    if hasattr(row, "__getitem__") and row["name"]
                }
                if "seen_at" not in columns:
                    conn.execute("ALTER TABLE agent_inbox ADD COLUMN seen_at TIMESTAMP")
                if "completed_at" not in columns:
                    conn.execute("ALTER TABLE agent_inbox ADD COLUMN completed_at TIMESTAMP")
                if "completion_ref_json" not in columns:
                    conn.execute("ALTER TABLE agent_inbox ADD COLUMN completion_ref_json TEXT")
                if "last_resolution_status" not in columns:
                    conn.execute("ALTER TABLE agent_inbox ADD COLUMN last_resolution_status TEXT")
                if "last_resolution_at" not in columns:
                    conn.execute("ALTER TABLE agent_inbox ADD COLUMN last_resolution_at TIMESTAMP")
                if "last_completion_ref_json" not in columns:
                    conn.execute("ALTER TABLE agent_inbox ADD COLUMN last_completion_ref_json TEXT")
                conn.execute(
                    """
                    UPDATE agent_inbox
                    SET status = 'completed',
                        completed_at = COALESCE(completed_at, handled_at),
                        seen_at = COALESCE(seen_at, handled_at, created_at)
                    WHERE status = 'handled'
                    """
                )
                conn.execute(
                    """
                    UPDATE agent_inbox
                    SET last_resolution_status = COALESCE(last_resolution_status, CASE WHEN status IN ('completed', 'skipped') THEN status ELSE NULL END),
                        last_resolution_at = COALESCE(last_resolution_at, completed_at, handled_at),
                        last_completion_ref_json = COALESCE(last_completion_ref_json, completion_ref_json)
                    WHERE status IN ('completed', 'skipped')
                       OR completed_at IS NOT NULL
                       OR completion_ref_json IS NOT NULL
                    """
                )
                conn.commit()
        except Exception as e:
            logger.error(f"Failed to ensure agent_inbox tables: {e}")

    def _get_account_type(self, user_id: str) -> str:
        """Return account_type for a user ('agent' or 'human').

        Results are cached in-process for ``_ACCOUNT_TYPE_CACHE_TTL`` seconds to
        avoid a DB round-trip on every rate-limit / cooldown check.
        """
        if not user_id:
            return 'human'
        now = time.monotonic()
        cached = self._account_type_cache.get(user_id)
        if cached is not None:
            account_type, fetched_at = cached
            if now - fetched_at < self._ACCOUNT_TYPE_CACHE_TTL:
                return account_type
        try:
            with self.db.get_connection() as conn:
                row = conn.execute(
                    "SELECT account_type FROM users WHERE id = ?", (user_id,)
                ).fetchone()
            if row and row[0]:
                result = str(row[0]).lower()
            else:
                result = 'human'
        except Exception:
            result = 'human'
        self._account_type_cache[user_id] = (result, now)
        return result

    def get_config(self, user_id: str) -> Dict[str, Any]:
        # Choose base defaults based on account type so agents get relaxed
        # rate limits without needing explicit per-user config rows.
        account_type = self._get_account_type(user_id)
        base = DEFAULT_AGENT_INBOX_CONFIG if account_type == 'agent' else DEFAULT_INBOX_CONFIG
        config = dict(base)
        if not user_id:
            return config
        try:
            with self.db.get_connection() as conn:
                row = conn.execute(
                    "SELECT config_json FROM agent_inbox_config WHERE user_id = ?",
                    (user_id,)
                ).fetchone()
            if row and row[0]:
                try:
                    data = json.loads(row[0])
                    if isinstance(data, dict):
                        for key, value in data.items():
                            if value is None:
                                continue
                            config[key] = value
                except Exception:
                    pass
        except Exception as e:
            logger.warning(f"Failed to fetch inbox config for {user_id}: {e}")

        # Backward-compatible normalization:
        # - keep old configs working,
        # - enable reply notifications by default unless explicitly disabled.
        allowed_types_raw = config.get('allowed_trigger_types') or []
        if isinstance(allowed_types_raw, (list, tuple, set)):
            allowed_types = [str(v).strip().lower() for v in allowed_types_raw if str(v).strip()]
        else:
            allowed_types = []
        allowed_types = list(dict.fromkeys(allowed_types))

        thread_reply_notifications = config.get('thread_reply_notifications')
        if thread_reply_notifications is None:
            thread_reply_notifications = True
            # Migrate legacy allowlists that only had mention+dm.
            if allowed_types and set(allowed_types).issubset({'mention', 'dm'}):
                allowed_types.append('reply')
        else:
            thread_reply_notifications = bool(thread_reply_notifications)
            if thread_reply_notifications and 'reply' not in allowed_types:
                allowed_types.append('reply')
            if not thread_reply_notifications and 'reply' in allowed_types:
                allowed_types = [t for t in allowed_types if t != 'reply']

        if not allowed_types:
            allowed_types = ['mention', 'dm', 'reply']

        config['allowed_trigger_types'] = allowed_types
        config['thread_reply_notifications'] = thread_reply_notifications
        if config.get('auto_subscribe_own_threads') is None:
            config['auto_subscribe_own_threads'] = True
        return config

    def set_config(self, user_id: str, updates: Dict[str, Any]) -> Dict[str, Any]:
        if not user_id:
            return dict(DEFAULT_INBOX_CONFIG)
        current = self.get_config(user_id)
        if not isinstance(updates, dict):
            return current
        merged = dict(current)
        for key, value in updates.items():
            if value is None:
                continue
            merged[key] = value
        try:
            with self.db.get_connection() as conn:
                conn.execute(
                    """
                    INSERT INTO agent_inbox_config (user_id, config_json, updated_at)
                    VALUES (?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(user_id) DO UPDATE SET
                        config_json = excluded.config_json,
                        updated_at = excluded.updated_at
                    """,
                    (user_id, json.dumps(merged))
                )
                conn.commit()
        except Exception as e:
            logger.error(f"Failed to update inbox config for {user_id}: {e}")
        return merged

    def _cooldown_ok(self, agent_user_id: str, sender_user_id: Optional[str], config: Dict[str, Any]) -> bool:
        # Agent inboxes already use much higher rate-limit ceilings; cooldowns
        # are too blunt and can hide legitimate rapid follow-up work.
        if self._get_account_type(agent_user_id) == 'agent':
            return True
        try:
            cooldown = int(config.get("cooldown_seconds") or 0)
            sender_cooldown = int(config.get("sender_cooldown_seconds") or 0)
            agent_sender_cooldown = int(config.get("agent_sender_cooldown_seconds") or 0)
        except Exception:
            cooldown = 0
            sender_cooldown = 0
            agent_sender_cooldown = 0

        if cooldown <= 0 and sender_cooldown <= 0 and agent_sender_cooldown <= 0:
            return True

        try:
            with self.db.get_connection() as conn:
                if cooldown > 0:
                    row = conn.execute(
                        """
                        SELECT CAST(strftime('%s','now') - strftime('%s', created_at) AS INTEGER) AS delta
                        FROM agent_inbox
                        WHERE agent_user_id = ?
                        ORDER BY created_at DESC
                        LIMIT 1
                        """,
                        (agent_user_id,)
                    ).fetchone()
                    if row and row[0] is not None and int(row[0]) < cooldown:
                        return False

                if sender_user_id:
                    sender_row = conn.execute(
                        """
                        SELECT CAST(strftime('%s','now') - strftime('%s', created_at) AS INTEGER) AS delta
                        FROM agent_inbox
                        WHERE agent_user_id = ? AND sender_user_id = ?
                        ORDER BY created_at DESC
                        LIMIT 1
                        """,
                        (agent_user_id, sender_user_id)
                    ).fetchone()
                    if sender_row and sender_row[0] is not None:
                        delta = int(sender_row[0])
                        # If sender is an agent, prefer the longer cooldown.
                        is_agent = False
                        try:
                            row = conn.execute(
                                "SELECT account_type FROM users WHERE id = ?",
                                (sender_user_id,)
                            ).fetchone()
                            if row and row[0] == 'agent':
                                is_agent = True
                        except Exception:
                            is_agent = False
                        threshold = agent_sender_cooldown if (is_agent and agent_sender_cooldown > 0) else sender_cooldown
                        if threshold > 0 and delta < threshold:
                            return False
        except Exception as e:
            # Fail closed: reject trigger when cooldown cannot be verified
            logger.warning(f"Cooldown check failed (rejecting): {e}")
            return False
        return True

    def _rate_limit_ok(
        self,
        agent_user_id: str,
        channel_id: Optional[str],
        sender_user_id: Optional[str],
        config: Dict[str, Any],
    ) -> bool:
        try:
            burst_limit = int(config.get("channel_burst_limit") or 0)
            burst_window = int(config.get("channel_burst_window_seconds") or 0)
            hourly_limit = int(config.get("channel_hourly_limit") or 0)
            hourly_window = int(config.get("channel_hourly_window_seconds") or 0)
            sender_hourly_limit = int(config.get("sender_hourly_limit") or 0)
            sender_hourly_window = int(config.get("sender_hourly_window_seconds") or 0)
        except Exception:
            burst_limit = burst_window = hourly_limit = hourly_window = 0
            sender_hourly_limit = sender_hourly_window = 0

        if not channel_id and not sender_user_id:
            return True

        check_channel_burst = bool(channel_id and burst_limit > 0 and burst_window > 0)
        check_channel_hourly = bool(channel_id and hourly_limit > 0 and hourly_window > 0)
        check_sender_hourly = bool(sender_user_id and sender_hourly_limit > 0 and sender_hourly_window > 0)

        if not check_channel_burst and not check_channel_hourly and not check_sender_hourly:
            return True

        now = _now_utc()
        try:
            with self.db.get_connection() as conn:
                # Consolidate the two channel COUNT queries into one when both
                # burst and hourly limits are active: the longer window always
                # covers the shorter one, so a single scan suffices.
                if check_channel_burst or check_channel_hourly:
                    # Use the longer of the two windows so one query covers both.
                    use_burst = check_channel_burst
                    use_hourly = check_channel_hourly
                    if use_burst and use_hourly:
                        # Longer window is the hourly one; the burst window is a
                        # subset of it, so count rows in the hourly window and
                        # re-count in the burst sub-window via a CASE expression.
                        since_hourly = (now - timedelta(seconds=hourly_window)).isoformat()
                        since_burst = (now - timedelta(seconds=burst_window)).isoformat()
                        row = conn.execute(
                            """
                            SELECT
                                COUNT(*) AS hourly_n,
                                SUM(CASE WHEN created_at >= ? THEN 1 ELSE 0 END) AS burst_n
                            FROM agent_inbox
                            WHERE agent_user_id = ?
                              AND channel_id = ?
                              AND created_at >= ?
                            """,
                            (since_burst, agent_user_id, channel_id, since_hourly),
                        ).fetchone()
                        if row:
                            if row[0] is not None and int(row[0]) >= hourly_limit:
                                return False
                            if row[1] is not None and int(row[1]) >= burst_limit:
                                return False
                    elif use_burst:
                        since = (now - timedelta(seconds=burst_window)).isoformat()
                        row = conn.execute(
                            """
                            SELECT COUNT(*) AS n
                            FROM agent_inbox
                            WHERE agent_user_id = ?
                              AND channel_id = ?
                              AND created_at >= ?
                            """,
                            (agent_user_id, channel_id, since),
                        ).fetchone()
                        if row and row[0] is not None and int(row[0]) >= burst_limit:
                            return False
                    else:  # use_hourly only
                        since = (now - timedelta(seconds=hourly_window)).isoformat()
                        row = conn.execute(
                            """
                            SELECT COUNT(*) AS n
                            FROM agent_inbox
                            WHERE agent_user_id = ?
                              AND channel_id = ?
                              AND created_at >= ?
                            """,
                            (agent_user_id, channel_id, since),
                        ).fetchone()
                        if row and row[0] is not None and int(row[0]) >= hourly_limit:
                            return False

                if check_sender_hourly:
                    since = (now - timedelta(seconds=sender_hourly_window)).isoformat()
                    row = conn.execute(
                        """
                        SELECT COUNT(*) AS n
                        FROM agent_inbox
                        WHERE agent_user_id = ?
                          AND sender_user_id = ?
                          AND created_at >= ?
                        """,
                        (agent_user_id, sender_user_id, since),
                    ).fetchone()
                    if row and row[0] is not None and int(row[0]) >= sender_hourly_limit:
                        return False
        except Exception as e:
            # Fail closed: reject trigger when rate limit cannot be verified
            logger.warning(f"Rate limit check failed (rejecting): {e}")
            return False
        return True

    def _prune_audit(self, retention_days: int) -> None:
        if retention_days <= 0:
            return
        try:
            with self.db.get_connection() as conn:
                cutoff = (_now_utc() - timedelta(days=retention_days)).isoformat()
                conn.execute(
                    "DELETE FROM agent_inbox_audit WHERE created_at < ?",
                    (cutoff,),
                )
                conn.commit()
        except Exception:
            pass

    def _record_rejection(
        self,
        agent_user_id: str,
        reason: str,
        source_type: str,
        source_id: str,
        channel_id: Optional[str],
        sender_user_id: Optional[str],
        origin_peer: Optional[str],
        trigger_type: str,
    ) -> None:
        if not agent_user_id:
            return
        config = self.get_config(agent_user_id)
        if not config.get("audit_rejections", True):
            return
        try:
            retention = int(config.get("audit_retention_days") or 0)
        except Exception:
            retention = 0
        # Prune probabilistically (~1 in 20 calls) to avoid a DELETE on every rejection
        if retention and secrets.randbelow(20) == 0:
            self._prune_audit(retention)
        try:
            with self.db.get_connection() as conn:
                conn.execute(
                    """
                    INSERT INTO agent_inbox_audit
                    (id, agent_user_id, reason, source_type, source_id, channel_id,
                     sender_user_id, origin_peer, trigger_type, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        f"INA{secrets.token_hex(8)}",
                        agent_user_id,
                        reason,
                        source_type,
                        source_id,
                        channel_id,
                        sender_user_id,
                        origin_peer,
                        trigger_type,
                        _now_utc().isoformat(),
                    ),
                )
                conn.commit()
        except Exception as e:
            logger.debug(f"Failed to record inbox audit entry: {e}")

    def _enforce_capacity(self, agent_user_id: str, max_pending: int) -> None:
        if max_pending <= 0:
            return
        try:
            with self.db.get_connection() as conn:
                row = conn.execute(
                    """
                    SELECT COUNT(*) AS n
                    FROM agent_inbox
                    WHERE agent_user_id = ? AND status IN (?, ?)
                    """,
                    (agent_user_id, ACTIONABLE_STATUSES[0], ACTIONABLE_STATUSES[1])
                ).fetchone()
                count = int(row[0]) if row and row[0] is not None else 0
                if count < max_pending:
                    return
                to_expire = (count - max_pending) + 1
                conn.execute(
                    f"""
                    UPDATE agent_inbox
                    SET status = 'expired', handled_at = CURRENT_TIMESTAMP
                    WHERE id IN (
                        SELECT id FROM agent_inbox
                        WHERE agent_user_id = ? AND status IN ('pending', 'seen')
                        ORDER BY created_at ASC
                        LIMIT {to_expire}
                    )
                    """,
                    (agent_user_id,)
                )
                conn.commit()
        except Exception as e:
            logger.warning(f"Failed to enforce inbox capacity: {e}")

    def _expire_items(self, agent_user_id: Optional[str] = None) -> int:
        """Mark expired items as expired."""
        try:
            with self.db.get_connection() as conn:
                if agent_user_id:
                    cur = conn.execute(
                        """
                        UPDATE agent_inbox
                        SET status = 'expired', handled_at = CURRENT_TIMESTAMP
                        WHERE agent_user_id = ?
                          AND status IN ('pending', 'seen')
                          AND expires_at IS NOT NULL
                          AND expires_at <= CURRENT_TIMESTAMP
                        """,
                        (agent_user_id,)
                    )
                else:
                    cur = conn.execute(
                        """
                        UPDATE agent_inbox
                        SET status = 'expired', handled_at = CURRENT_TIMESTAMP
                        WHERE status IN ('pending', 'seen')
                          AND expires_at IS NOT NULL
                          AND expires_at <= CURRENT_TIMESTAMP
                        """
                    )
                conn.commit()
                return cur.rowcount or 0
        except Exception:
            return 0

    def create_trigger(
        self,
        agent_user_id: str,
        source_type: str,
        source_id: str,
        sender_user_id: Optional[str] = None,
        origin_peer: Optional[str] = None,
        channel_id: Optional[str] = None,
        trigger_type: str = 'mention',
        preview: Optional[str] = None,
        payload: Optional[Dict[str, Any]] = None,
        message_id: Optional[str] = None,
        priority: Optional[str] = None,
        triggered_by_inbox_id: Optional[str] = None,
        depth: int = 0,
    ) -> Optional[str]:
        if not agent_user_id or not source_type or not source_id:
            return None

        inbox_enabled = os.getenv("CANOPY_INBOX_ENABLED", "1").strip().lower()
        if inbox_enabled in ("0", "false", "no"):
            self._record_rejection(
                agent_user_id, "disabled", source_type, source_id,
                channel_id, sender_user_id, origin_peer, trigger_type,
            )
            logger.info(
                "Inbox trigger rejected: reason=disabled (CANOPY_INBOX_ENABLED=0) "
                "agent_user_id=%s source_id=%s", agent_user_id, source_id,
            )
            return None

        # Cascade prevention: reject triggers beyond max depth
        if depth >= MAX_TRIGGER_DEPTH:
            self._record_rejection(
                agent_user_id, "depth_exceeded", source_type, source_id,
                channel_id, sender_user_id, origin_peer, trigger_type,
            )
            logger.info(
                "Inbox trigger rejected: reason=depth_exceeded agent_user_id=%s source_id=%s depth=%s",
                agent_user_id, source_id, depth,
            )
            return None

        config = self.get_config(agent_user_id)
        allowed_types = config.get('allowed_trigger_types') or []
        if allowed_types and trigger_type not in allowed_types:
            self._record_rejection(
                agent_user_id, "trigger_type_blocked", source_type, source_id,
                channel_id, sender_user_id, origin_peer, trigger_type,
            )
            logger.info(
                "Inbox trigger rejected: reason=trigger_type_blocked agent_user_id=%s source_id=%s trigger_type=%s",
                agent_user_id, source_id, trigger_type,
            )
            return None

        # Channel allowlist: only filter when channel_id is present.
        # Feed post mentions have no channel_id and should not be blocked.
        allowed_channels = config.get('channels') or []
        if allowed_channels and channel_id and channel_id not in allowed_channels:
            self._record_rejection(
                agent_user_id, "channel_blocked", source_type, source_id,
                channel_id, sender_user_id, origin_peer, trigger_type,
            )
            logger.info(
                "Inbox trigger rejected: reason=channel_blocked agent_user_id=%s source_id=%s channel_id=%s",
                agent_user_id, source_id, channel_id,
            )
            return None

        # Sender allowlist: only filter when sender_user_id is present.
        # P2P or system-generated triggers may lack a sender.
        allowed_senders = config.get('allowed_senders') or []
        if allowed_senders and sender_user_id and sender_user_id not in allowed_senders:
            self._record_rejection(
                agent_user_id, "sender_blocked", source_type, source_id,
                channel_id, sender_user_id, origin_peer, trigger_type,
            )
            logger.info(
                "Inbox trigger rejected: reason=sender_blocked agent_user_id=%s source_id=%s sender_user_id=%s",
                agent_user_id, source_id, sender_user_id,
            )
            return None

        if config.get('trusted_only', True) and origin_peer and self.trust_manager:
            try:
                min_score = int(config.get('min_trust_score') or 50)
            except Exception:
                min_score = 50
            try:
                if self.trust_manager.get_trust_score(origin_peer) < min_score:
                    self._record_rejection(
                        agent_user_id, "trust_rejected", source_type, source_id,
                        channel_id, sender_user_id, origin_peer, trigger_type,
                    )
                    logger.info(
                        "Inbox trigger rejected: reason=trust_rejected agent_user_id=%s source_id=%s origin_peer=%s",
                        agent_user_id, source_id, origin_peer,
                    )
                    return None
            except Exception:
                # Fail closed: reject trigger when trust cannot be verified
                self._record_rejection(
                    agent_user_id, "trust_error", source_type, source_id,
                    channel_id, sender_user_id, origin_peer, trigger_type,
                )
                logger.info(
                    "Inbox trigger rejected: reason=trust_error agent_user_id=%s source_id=%s origin_peer=%s",
                    agent_user_id, source_id, origin_peer,
                )
                return None

        if not self._cooldown_ok(agent_user_id, sender_user_id, config):
            self._record_rejection(
                agent_user_id, "cooldown", source_type, source_id,
                channel_id, sender_user_id, origin_peer, trigger_type,
            )
            logger.info(
                "Inbox trigger rejected: reason=cooldown agent_user_id=%s source_id=%s sender_user_id=%s",
                agent_user_id, source_id, sender_user_id,
            )
            return None

        if not self._rate_limit_ok(agent_user_id, channel_id, sender_user_id, config):
            self._record_rejection(
                agent_user_id, "rate_limited", source_type, source_id,
                channel_id, sender_user_id, origin_peer, trigger_type,
            )
            logger.info(
                "Inbox trigger rejected: reason=rate_limited agent_user_id=%s source_id=%s channel_id=%s",
                agent_user_id, source_id, channel_id,
            )
            return None

        try:
            max_pending = int(config.get('max_pending') or 0)
        except Exception:
            max_pending = 0
        if max_pending:
            self._enforce_capacity(agent_user_id, max_pending)

        try:
            expire_days = int(config.get('expire_days') or 0)
        except Exception:
            expire_days = 0
        expires_at = None
        if expire_days > 0:
            expires_at = _now_utc() + timedelta(days=expire_days)

        payload_data = dict(payload or {})
        if preview and not payload_data.get('preview'):
            payload_data['preview'] = preview
        if channel_id and not payload_data.get('channel_id'):
            payload_data['channel_id'] = channel_id
        if sender_user_id and not payload_data.get('sender_user_id'):
            payload_data['sender_user_id'] = sender_user_id
        if origin_peer and not payload_data.get('origin_peer'):
            payload_data['origin_peer'] = origin_peer
        payload_data.setdefault('source_type', source_type)
        payload_data.setdefault('source_id', source_id)
        payload_data.setdefault('trigger_type', trigger_type)
        if message_id:
            payload_data.setdefault('message_id', message_id)

        inbox_id = f"INB{secrets.token_hex(8)}"
        created_at_iso = _now_utc().isoformat()
        try:
            with self.db.get_connection() as conn:
                cur = conn.execute(
                    """
                    INSERT OR IGNORE INTO agent_inbox
                    (id, agent_user_id, source_type, source_id, message_id, channel_id,
                     sender_user_id, origin_peer, trigger_type, payload_json, status,
                     priority, created_at, expires_at, triggered_by_inbox_id, depth)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?)
                    """,
                    (
                        inbox_id,
                        agent_user_id,
                        source_type,
                        source_id,
                        message_id,
                        channel_id,
                        sender_user_id,
                        origin_peer,
                        trigger_type,
                        json.dumps(payload_data) if payload_data else None,
                        priority or 'normal',
                        created_at_iso,
                        _iso(expires_at),
                        triggered_by_inbox_id,
                        int(depth) if depth is not None else 0,
                    )
                )
                conn.commit()
            if not (cur.rowcount or 0):
                return None
            if self.workspace_events:
                self.workspace_events.emit_event(
                    event_type=EVENT_INBOX_ITEM_CREATED,
                    actor_user_id=sender_user_id,
                    target_user_id=agent_user_id,
                    channel_id=channel_id,
                    message_id=message_id,
                    visibility_scope='user',
                    dedupe_key=f"{EVENT_INBOX_ITEM_CREATED}:{inbox_id}",
                    created_at=created_at_iso,
                    payload={
                        'inbox_id': inbox_id,
                        'source_type': source_type,
                        'source_id': source_id,
                        'trigger_type': trigger_type,
                        'status': 'pending',
                        'priority': priority or 'normal',
                        'preview': preview or payload_data.get('preview') or '',
                    },
                )
            return inbox_id
        except Exception as e:
            logger.warning(f"Failed to insert inbox item: {e}")
            return None

    def record_mention_triggers(
        self,
        target_ids: Sequence[str],
        source_type: str,
        source_id: str,
        author_id: Optional[str],
        origin_peer: Optional[str] = None,
        channel_id: Optional[str] = None,
        preview: Optional[str] = None,
        extra_ref: Optional[Dict[str, Any]] = None,
        source_content: Optional[str] = None,
        trigger_type: str = 'mention',
    ) -> int:
        if not target_ids:
            return 0
        payload_base: Dict[str, Any] = dict(extra_ref or {})
        if preview:
            payload_base.setdefault('preview', preview)
        if source_content:
            payload_base.setdefault('content', source_content)
        if author_id:
            payload_base.setdefault('author_id', author_id)
        if channel_id:
            payload_base.setdefault('channel_id', channel_id)
        if origin_peer:
            payload_base.setdefault('origin_peer', origin_peer)

        message_id = payload_base.get('message_id')
        if not message_id and source_type == 'channel_message':
            message_id = source_id
        if source_type == 'feed_post':
            payload_base.setdefault('post_id', source_id)

        inserted = 0
        for tid in target_ids:
            if not tid:
                continue
            payload = dict(payload_base)
            inbox_id = self.create_trigger(
                agent_user_id=tid,
                source_type=source_type,
                source_id=source_id,
                sender_user_id=author_id,
                origin_peer=origin_peer,
                channel_id=channel_id,
                trigger_type=trigger_type,
                preview=preview,
                payload=payload,
                message_id=message_id,
            )
            if inbox_id:
                inserted += 1
        return inserted

    def sync_source_triggers(
        self,
        *,
        source_type: str,
        source_id: str,
        trigger_type: str,
        target_ids: Optional[Sequence[str]] = None,
        sender_user_id: Optional[str] = None,
        origin_peer: Optional[str] = None,
        channel_id: Optional[str] = None,
        preview: Optional[str] = None,
        payload: Optional[Dict[str, Any]] = None,
        message_id: Optional[str] = None,
        priority: Optional[str] = None,
        source_content: Optional[str] = None,
        mark_missing_as_stale: bool = False,
    ) -> Dict[str, int]:
        """Refresh stored trigger payloads for a source and create missing rows.

        This is used when a source object is edited after the original inbox
        item was created. Existing rows keep the same IDs/status but receive the
        latest payload so agents read current text rather than a stale snapshot.
        """
        if not source_type or not source_id or not trigger_type:
            return {'updated': 0, 'created': 0, 'stale_marked': 0}

        desired_ids = list(dict.fromkeys([str(uid).strip() for uid in (target_ids or []) if str(uid).strip()]))
        desired_set = set(desired_ids)
        now_iso = _now_utc().isoformat()

        payload_base: Dict[str, Any] = dict(payload or {})
        if preview is not None:
            payload_base['preview'] = preview
        if source_content is not None:
            payload_base['content'] = source_content
        if channel_id:
            payload_base['channel_id'] = channel_id
        if sender_user_id:
            payload_base['sender_user_id'] = sender_user_id
        if origin_peer:
            payload_base['origin_peer'] = origin_peer
        payload_base['source_type'] = source_type
        payload_base['source_id'] = source_id
        payload_base['trigger_type'] = trigger_type

        sync_message_id = message_id
        if not sync_message_id and source_type in {'channel_message', 'dm'}:
            sync_message_id = source_id
        if sync_message_id:
            payload_base['message_id'] = sync_message_id

        updated = 0
        created = 0
        stale_marked = 0
        existing_user_ids: set[str] = set()

        try:
            with self.db.get_connection() as conn:
                rows = conn.execute(
                    """
                    SELECT id, agent_user_id, payload_json
                    FROM agent_inbox
                    WHERE source_type = ? AND source_id = ? AND trigger_type = ?
                    """,
                    (source_type, source_id, trigger_type),
                ).fetchall()

                for row in rows or []:
                    agent_user_id = str(row['agent_user_id'] or '').strip()
                    if not agent_user_id:
                        continue
                    existing_user_ids.add(agent_user_id)

                    merged_payload: Dict[str, Any] = {}
                    payload_raw = row['payload_json']
                    if payload_raw:
                        try:
                            loaded = json.loads(payload_raw)
                            if isinstance(loaded, dict):
                                merged_payload = loaded
                        except Exception:
                            merged_payload = {}
                    merged_payload.update(payload_base)

                    if mark_missing_as_stale:
                        still_mentioned = agent_user_id in desired_set
                        merged_payload['still_mentioned'] = still_mentioned
                        if still_mentioned:
                            merged_payload.pop('mention_removed_at', None)
                        else:
                            merged_payload['mention_removed_at'] = now_iso
                            stale_marked += 1
                    elif agent_user_id in desired_set:
                        merged_payload.pop('mention_removed_at', None)

                    conn.execute(
                        """
                        UPDATE agent_inbox
                        SET payload_json = ?, message_id = COALESCE(?, message_id),
                            channel_id = COALESCE(?, channel_id),
                            sender_user_id = COALESCE(?, sender_user_id),
                            origin_peer = COALESCE(?, origin_peer),
                            priority = COALESCE(?, priority)
                        WHERE id = ?
                        """,
                        (
                            json.dumps(merged_payload) if merged_payload else None,
                            sync_message_id,
                            channel_id,
                            sender_user_id,
                            origin_peer,
                            priority,
                            row['id'],
                        ),
                    )
                    updated += 1

                conn.commit()
        except Exception as e:
            logger.warning(f"Failed to refresh inbox source payloads: {e}")
            return {'updated': updated, 'created': created, 'stale_marked': stale_marked}

        for agent_user_id in desired_ids:
            if agent_user_id in existing_user_ids:
                continue
            create_payload = dict(payload_base)
            if mark_missing_as_stale:
                create_payload['still_mentioned'] = True
            inbox_id = self.create_trigger(
                agent_user_id=agent_user_id,
                source_type=source_type,
                source_id=source_id,
                sender_user_id=sender_user_id,
                origin_peer=origin_peer,
                channel_id=channel_id,
                trigger_type=trigger_type,
                preview=preview,
                payload=create_payload,
                message_id=sync_message_id,
                priority=priority,
            )
            if inbox_id:
                created += 1

        return {'updated': updated, 'created': created, 'stale_marked': stale_marked}

    def rebuild_from_channel_messages(
        self,
        user_id: str,
        username: str,
        display_name: Optional[str] = None,
        window_hours: int = 168,
        limit: int = 2000,
        channel_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Scan recent channel messages for @mentions of this user and
        create any missing inbox items, bypassing rate limits.

        This is a catch-up / recovery operation — it fills gaps caused by
        P2P downtime, rate-limit drops, or cooldown suppressions.  It is
        idempotent: already-existing items are left untouched (IGNORE on
        the unique index prevents duplicates). Only channels the user is
        currently a member of are considered.
        """
        if not user_id or not username:
            return {"scanned": 0, "created": 0, "skipped": 0}

        # Build a set of @handles this user might appear as.
        handles: set = set()
        handles.add(f"@{username}")
        if display_name:
            # Some agents write "@Display Name" with spaces — add both.
            handles.add(f"@{display_name}")
            # Also try first word of display name (e.g. "Alice" from "Alice Smith")
            first_word = display_name.split()[0]
            if first_word != display_name:
                handles.add(f"@{first_word}")
        # Strip peer suffix (e.g. "AgentName" from "AgentName.3XjWVz")
        base = username.split('.')[0]
        if base != username:
            handles.add(f"@{base}")

        since_dt = _now_utc() - timedelta(hours=window_hours)
        since_iso = since_dt.isoformat()

        try:
            with self.db.get_connection() as conn:
                like_clauses = " OR ".join(["LOWER(content) LIKE ?" for _ in handles])
                like_args = [f"%{h.lower()}%" for h in handles]
                params = [user_id] + like_args
                channel_clause = ""
                if channel_id:
                    channel_clause = "AND cm.channel_id = ?"
                    params.append(channel_id)
                params.extend([since_iso, limit])
                rows = conn.execute(
                    f"""
                    SELECT cm.id, cm.channel_id, cm.user_id AS sender_id, cm.content,
                           created_at, origin_peer
                    FROM channel_messages cm
                    INNER JOIN channel_members cmm
                      ON cmm.channel_id = cm.channel_id
                     AND cmm.user_id = ?
                    WHERE ({like_clauses})
                      {channel_clause}
                      AND (cm.expires_at IS NULL OR cm.expires_at > CURRENT_TIMESTAMP)
                      AND cm.created_at >= ?
                    ORDER BY cm.created_at DESC
                    LIMIT ?
                    """,
                    params,
                ).fetchall()
        except Exception as e:
            logger.error(f"inbox rebuild query failed for {user_id}: {e}")
            return {"scanned": 0, "created": 0, "skipped": 0, "error": str(e)}

        scanned = len(rows)
        created = 0
        skipped = 0
        expire_at = (_now_utc() + timedelta(days=14)).isoformat()

        for row in rows:
            msg_id = row["id"]
            ch_id = row["channel_id"]
            sender_id = row["sender_id"]
            content = row["content"] or ""
            origin_peer = row["origin_peer"]

            # Build a short preview (first 200 chars, no newlines); never leave empty (clients show "N/A")
            raw = (content or "").replace("\n", " ").strip()[:200]
            preview = raw if raw else "(no content available)"

            payload = json.dumps({
                "preview": preview,
                "content": content,
                "channel_id": ch_id,
                "sender_user_id": sender_id,
                "source_id": msg_id,
                "trigger_type": "mention",
                "source_type": "channel_message",
                "rebuilt": True,
            })

            inbox_id = f"INB{secrets.token_hex(8)}"
            try:
                with self.db.get_connection() as conn:
                    cur = conn.execute(
                        """
                        INSERT OR IGNORE INTO agent_inbox
                        (id, agent_user_id, source_type, source_id, message_id,
                         channel_id, sender_user_id, origin_peer, trigger_type,
                         payload_json, status, priority, created_at, expires_at,
                         depth)
                        VALUES (?, ?, 'channel_message', ?, ?, ?, ?, ?, 'mention',
                                ?, 'pending', 'normal', ?, ?, 0)
                        """,
                        (
                            inbox_id, user_id, msg_id, msg_id,
                            ch_id, sender_id, origin_peer,
                            payload,
                            _now_utc().isoformat(),
                            expire_at,
                        ),
                    )
                    conn.commit()
                if cur.rowcount:
                    created += 1
                else:
                    skipped += 1
            except Exception as e:
                logger.warning(f"inbox rebuild: failed to insert item for {msg_id}: {e}")
                skipped += 1

        logger.info(
            f"inbox rebuild for {user_id}: scanned={scanned} "
            f"created={created} skipped={skipped}"
        )
        return {"scanned": scanned, "created": created, "skipped": skipped}

    def list_items(
        self,
        user_id: str,
        status: Optional[str] = None,
        limit: int = 50,
        since: Optional[str] = None,
        include_handled: bool = False,
    ) -> List[Dict[str, Any]]:
        if not user_id:
            return []
        self._expire_items(user_id)

        status_filters = _normalize_status_rows(status)
        try:
            limit_val = int(limit)
        except Exception:
            limit_val = 50
        if limit_val <= 0:
            limit_val = 50

        params: List[Any] = [user_id]
        where = "WHERE agent_user_id = ?"
        if status_filters:
            placeholders = ",".join("?" for _ in status_filters)
            where += f" AND status IN ({placeholders})"
            params.extend(status_filters)
        elif not include_handled:
            where += " AND status IN ('pending', 'seen')"
        if since:
            where += " AND created_at > ?"
            params.append(since)

        try:
            with self.db.get_connection() as conn:
                rows = conn.execute(
                    f"""
                    SELECT * FROM agent_inbox
                    {where}
                    ORDER BY created_at DESC
                    LIMIT ?
                    """,
                    params + [limit_val]
                ).fetchall()
            items: List[Dict[str, Any]] = []
            for row in rows:
                payload = None
                try:
                    if row['payload_json']:
                        payload = json.loads(row['payload_json'])
                except Exception:
                    payload = None
                payload = _normalize_item_payload(row, payload if isinstance(payload, dict) else None)
                items.append({
                    'id': row['id'],
                    'agent_user_id': row['agent_user_id'],
                    'source_type': row['source_type'],
                    'source_id': row['source_id'],
                    'message_id': row['message_id'],
                    'channel_id': row['channel_id'],
                    'sender_user_id': row['sender_user_id'],
                    'origin_peer': row['origin_peer'],
                    'trigger_type': row['trigger_type'],
                    'status': _normalize_output_status(row['status']),
                    'priority': row['priority'],
                    'created_at': row['created_at'],
                    'seen_at': row['seen_at'],
                    'handled_at': row['handled_at'],
                    'completed_at': row['completed_at'],
                    'completion_ref': (
                        json.loads(row['completion_ref_json'])
                        if row['completion_ref_json']
                        else None
                    ),
                    'last_resolution_status': _normalize_output_status(row['last_resolution_status']),
                    'last_resolution_at': row['last_resolution_at'],
                    'last_completion_ref': (
                        json.loads(row['last_completion_ref_json'])
                        if row['last_completion_ref_json']
                        else None
                    ),
                    'expires_at': row['expires_at'],
                    'triggered_by_inbox_id': row['triggered_by_inbox_id'],
                    'depth': row['depth'],
                    'preview': payload.get('preview'),
                    'dm_thread_id': payload.get('dm_thread_id'),
                    'reply_endpoint': payload.get('reply_endpoint'),
                    'payload': payload,
                })
            return items
        except Exception as e:
            logger.error(f"Failed to list inbox items: {e}")
            return []

    def count_items(self, user_id: str, status: Optional[str] = None, include_handled: bool = False) -> int:
        if not user_id:
            return 0
        self._expire_items(user_id)
        status_filters = _normalize_status_rows(status)
        params: List[Any] = [user_id]
        where = "WHERE agent_user_id = ?"
        if status_filters:
            placeholders = ",".join("?" for _ in status_filters)
            where += f" AND status IN ({placeholders})"
            params.extend(status_filters)
        elif not include_handled:
            where += " AND status IN ('pending', 'seen')"
        try:
            with self.db.get_connection() as conn:
                row = conn.execute(
                    f"SELECT COUNT(*) AS n FROM agent_inbox {where}",
                    params,
                ).fetchone()
            return int(row[0]) if row and row[0] is not None else 0
        except Exception:
            return 0

    def update_items(
        self,
        user_id: str,
        ids: Sequence[str],
        status: str,
        completion_ref: Optional[Dict[str, Any]] = None,
    ) -> int:
        if not user_id or not ids:
            return 0
        requested_status = str(status or '').strip().lower()
        # Reject unknown statuses before normalization so an unrecognised value
        # cannot silently reset items to 'pending' (normalisation fallback).
        if requested_status not in ALLOWED_STATUSES:
            return 0
        status = _normalize_storage_status(requested_status)
        if status not in ALLOWED_STATUSES:
            return 0
        ids_clean = [i for i in ids if i]
        if not ids_clean:
            return 0
        sanitized_completion_ref = _sanitize_completion_ref(completion_ref)
        try:
            with self.db.get_connection() as conn:
                placeholders = ",".join("?" for _ in ids_clean)
                affected_rows = conn.execute(
                    f"""
                    SELECT id, source_type, source_id, message_id, channel_id, sender_user_id,
                           priority, payload_json, status, seen_at, handled_at, completed_at,
                           completion_ref_json, last_resolution_status, last_resolution_at,
                           last_completion_ref_json
                    FROM agent_inbox
                    WHERE agent_user_id = ? AND id IN ({placeholders})
                    """,
                    [user_id] + ids_clean,
                ).fetchall()
                updated = 0
                event_rows: List[Dict[str, Any]] = []
                now_iso = _now_utc().isoformat()
                for row in affected_rows or []:
                    current_seen_at = row['seen_at']
                    current_completed_at = row['completed_at']
                    current_completion_ref = row['completion_ref_json']
                    current_status = _normalize_storage_status(row['status'])
                    current_last_resolution_status = row['last_resolution_status']
                    current_last_resolution_at = row['last_resolution_at']
                    current_last_completion_ref = row['last_completion_ref_json']
                    next_last_resolution_status = current_last_resolution_status
                    next_last_resolution_at = current_last_resolution_at
                    next_last_completion_ref_json = current_last_completion_ref
                    if status == 'pending':
                        next_seen_at = current_seen_at
                        next_handled_at = None
                        next_completed_at = None
                        next_completion_ref_json = None
                    else:
                        next_seen_at = current_seen_at or now_iso
                        next_handled_at = now_iso
                        if status in {'completed', 'skipped'}:
                            next_completed_at = current_completed_at or now_iso
                            next_completion_ref_json = (
                                json.dumps(sanitized_completion_ref)
                                if sanitized_completion_ref is not None
                                else current_completion_ref
                            )
                            next_last_resolution_status = status
                            next_last_resolution_at = next_completed_at
                            next_last_completion_ref_json = next_completion_ref_json
                        else:
                            # Transitioning to 'seen', 'expired', or similar
                            # intermediate state: clear any stale finalization
                            # metadata left over from a prior completed/skipped
                            # state so the item does not show misleading
                            # completion timestamps or evidence links.
                            next_completed_at = None
                            next_completion_ref_json = None
                    if current_status in TERMINAL_STATUSES and status not in TERMINAL_STATUSES:
                        next_last_resolution_status = current_status
                        next_last_resolution_at = current_completed_at or row['handled_at'] or current_last_resolution_at
                        next_last_completion_ref_json = current_completion_ref or current_last_completion_ref
                    cur = conn.execute(
                        """
                        UPDATE agent_inbox
                        SET status = ?, seen_at = ?, handled_at = ?, completed_at = ?, completion_ref_json = ?,
                            last_resolution_status = ?, last_resolution_at = ?, last_completion_ref_json = ?
                        WHERE agent_user_id = ? AND id = ?
                        """,
                        (
                            status,
                            next_seen_at,
                            next_handled_at,
                            next_completed_at,
                            next_completion_ref_json,
                            next_last_resolution_status,
                            next_last_resolution_at,
                            next_last_completion_ref_json,
                            user_id,
                            row['id'],
                        ),
                    )
                    if cur.rowcount:
                        updated += cur.rowcount or 0
                        event_rows.append({
                            'id': row['id'],
                            'source_type': row['source_type'],
                            'source_id': row['source_id'],
                            'message_id': row['message_id'],
                            'channel_id': row['channel_id'],
                            'priority': row['priority'],
                            'payload_json': row['payload_json'],
                            'status': status,
                            'handled_at': next_handled_at,
                            'completion_ref': (
                                json.loads(next_completion_ref_json)
                                if next_completion_ref_json else None
                            ),
                            'last_resolution_status': _normalize_output_status(next_last_resolution_status),
                            'last_resolution_at': next_last_resolution_at,
                            'last_completion_ref': (
                                json.loads(next_last_completion_ref_json)
                                if next_last_completion_ref_json else None
                            ),
                        })
                conn.commit()
            if updated and self.workspace_events:
                for row in event_rows:
                    preview = ''
                    try:
                        loaded = json.loads(row['payload_json']) if row['payload_json'] else {}
                        if isinstance(loaded, dict):
                            preview = str(loaded.get('preview') or '').strip()
                    except Exception:
                        preview = ''
                    handled_suffix = row['handled_at'] or 'pending'
                    self.workspace_events.emit_event(
                        event_type=EVENT_INBOX_ITEM_UPDATED,
                        actor_user_id=user_id,
                        target_user_id=user_id,
                        channel_id=row['channel_id'],
                        message_id=row['message_id'],
                        visibility_scope='user',
                        dedupe_key=f"{EVENT_INBOX_ITEM_UPDATED}:{row['id']}:{requested_status}:{handled_suffix}",
                        created_at=row['handled_at'] or _now_utc().isoformat(),
                        payload={
                            'inbox_id': row['id'],
                            'source_type': row['source_type'],
                            'source_id': row['source_id'],
                            'status': _normalize_output_status(row['status']),
                            'handled_at': row['handled_at'],
                            'priority': row['priority'] or 'normal',
                            'preview': preview,
                            'completion_ref': row['completion_ref'],
                            'last_resolution_status': row['last_resolution_status'],
                            'last_resolution_at': row['last_resolution_at'],
                            'last_completion_ref': row['last_completion_ref'],
                        },
                    )
            return updated
        except Exception as e:
            logger.error(f"Failed to update inbox items: {e}")
            return 0

    def remove_source_triggers(
        self,
        *,
        source_type: str,
        source_id: str,
        trigger_type: Optional[str] = None,
        agent_user_id: Optional[str] = None,
    ) -> int:
        """Delete inbox rows for a specific source object.

        Used when the source object itself is deleted and any pending action
        rows should disappear with it.
        """
        if not source_type or not source_id:
            return 0
        params: List[Any] = [source_type, source_id]
        where = "WHERE source_type = ? AND source_id = ?"
        if trigger_type:
            where += " AND trigger_type = ?"
            params.append(trigger_type)
        if agent_user_id:
            where += " AND agent_user_id = ?"
            params.append(agent_user_id)
        try:
            with self.db.get_connection() as conn:
                cur = conn.execute(
                    f"DELETE FROM agent_inbox {where}",
                    params,
                )
                conn.commit()
                return cur.rowcount or 0
        except Exception as e:
            logger.warning(
                "Failed to remove inbox source triggers for %s:%s: %s",
                source_type,
                source_id,
                e,
            )
            return 0

    def get_stats(self, user_id: str, window_hours: int = 24) -> Dict[str, Any]:
        if not user_id:
            return {"status_counts": {}, "rejection_counts": {}}
        self._expire_items(user_id)
        try:
            window_hours = int(window_hours)
        except Exception:
            window_hours = 24
        if window_hours <= 0:
            window_hours = 24
        since = (_now_utc() - timedelta(hours=window_hours)).isoformat()
        status_counts: Dict[str, int] = {}
        rejection_counts: Dict[str, int] = {}
        discrepancy_counts: Dict[str, int] = {}
        try:
            with self.db.get_connection() as conn:
                rows = conn.execute(
                    """
                    SELECT status, COUNT(*) AS n
                    FROM agent_inbox
                    WHERE agent_user_id = ?
                    GROUP BY status
                    """,
                    (user_id,),
                ).fetchall()
                for row in rows:
                    normalized_status = _normalize_output_status(row["status"])
                    status_counts[normalized_status] = status_counts.get(normalized_status, 0) + int(row["n"])

                discrepancy_rows = conn.execute(
                    """
                    SELECT
                        SUM(CASE
                            WHEN status IN ('completed', 'handled')
                             AND (completion_ref_json IS NULL OR TRIM(completion_ref_json) = '')
                            THEN 1 ELSE 0 END
                        ) AS completed_without_completion_ref,
                        SUM(CASE
                            WHEN status = 'skipped'
                             AND (completion_ref_json IS NULL OR TRIM(completion_ref_json) = '')
                            THEN 1 ELSE 0 END
                        ) AS skipped_without_completion_ref
                    FROM agent_inbox
                    WHERE agent_user_id = ?
                    """,
                    (user_id,),
                ).fetchone()
                if discrepancy_rows:
                    discrepancy_counts = {
                        "completed_without_completion_ref": int(discrepancy_rows["completed_without_completion_ref"] or 0),
                        "skipped_without_completion_ref": int(discrepancy_rows["skipped_without_completion_ref"] or 0),
                    }

                rows = conn.execute(
                    """
                    SELECT reason, COUNT(*) AS n
                    FROM agent_inbox_audit
                    WHERE agent_user_id = ?
                      AND created_at >= ?
                    GROUP BY reason
                    """,
                    (user_id, since),
                ).fetchall()
                for row in rows:
                    rejection_counts[str(row["reason"])] = int(row["n"])
        except Exception as e:
            logger.warning(f"Failed to fetch inbox stats: {e}")
        return {
            "window_hours": window_hours,
            "status_counts": status_counts,
            "rejection_counts": rejection_counts,
            "discrepancy_counts": discrepancy_counts,
        }

    def list_audit(self, user_id: str, limit: int = 50, since: Optional[str] = None) -> List[Dict[str, Any]]:
        if not user_id:
            return []
        try:
            limit_val = int(limit)
        except Exception:
            limit_val = 50
        if limit_val <= 0:
            limit_val = 50
        params: List[Any] = [user_id]
        where = "WHERE agent_user_id = ?"
        if since:
            where += " AND created_at > ?"
            params.append(since)
        try:
            with self.db.get_connection() as conn:
                rows = conn.execute(
                    f"""
                    SELECT *
                    FROM agent_inbox_audit
                    {where}
                    ORDER BY created_at DESC
                    LIMIT ?
                    """,
                    params + [limit_val],
                ).fetchall()
            items: List[Dict[str, Any]] = []
            for row in rows:
                items.append({
                    "id": row["id"],
                    "agent_user_id": row["agent_user_id"],
                    "reason": row["reason"],
                    "source_type": row["source_type"],
                    "source_id": row["source_id"],
                    "channel_id": row["channel_id"],
                    "sender_user_id": row["sender_user_id"],
                    "origin_peer": row["origin_peer"],
                    "trigger_type": row["trigger_type"],
                    "created_at": row["created_at"],
                })
            return items
        except Exception as e:
            logger.warning(f"Failed to list inbox audit: {e}")
            return []
