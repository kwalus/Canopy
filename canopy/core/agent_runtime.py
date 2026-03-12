"""Agent runtime telemetry helpers.

Tracks lightweight operational timestamps for agent-facing fetch loops so
admins can inspect whether agents are actually servicing Canopy in time.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from .inbox import ACTIONABLE_STATUSES

logger = logging.getLogger(__name__)

_SQLITE_TS_FORMAT = "%Y-%m-%d %H:%M:%S.%f"


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
                try:
                    dt = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
                except Exception:
                    return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _to_iso_utc(value: Any) -> Optional[str]:
    dt = _to_datetime_utc(value)
    return dt.isoformat() if dt else None


def _format_age_short(age_seconds: Optional[int]) -> Optional[str]:
    if age_seconds is None:
        return None
    age = max(0, int(age_seconds))
    if age < 60:
        return f"{age}s"
    if age < 3600:
        return f"{age // 60}m"
    if age < 86400:
        return f"{age // 3600}h"
    return f"{age // 86400}d"


def ensure_agent_runtime_schema(db_manager: Any) -> None:
    if not db_manager:
        return
    with db_manager.get_connection() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS agent_runtime_state (
                user_id TEXT PRIMARY KEY,
                last_event_fetch_at TIMESTAMP,
                last_event_cursor_seen INTEGER,
                last_inbox_fetch_at TIMESTAMP,
                updated_at TIMESTAMP NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_agent_runtime_event_fetch
                ON agent_runtime_state(last_event_fetch_at);
            CREATE INDEX IF NOT EXISTS idx_agent_runtime_inbox_fetch
                ON agent_runtime_state(last_inbox_fetch_at);
            """
        )
        conn.commit()


def record_agent_runtime_state(
    db_manager: Any,
    user_id: str,
    *,
    event_fetch_at: Optional[datetime] = None,
    event_cursor_seen: Optional[int] = None,
    inbox_fetch_at: Optional[datetime] = None,
) -> None:
    if not db_manager or not user_id:
        return
    now_dt = datetime.now(timezone.utc)
    try:
        ensure_agent_runtime_schema(db_manager)
        with db_manager.get_connection() as conn:
            existing = conn.execute(
                """
                SELECT last_event_fetch_at, last_event_cursor_seen, last_inbox_fetch_at
                FROM agent_runtime_state
                WHERE user_id = ?
                """,
                (user_id,),
            ).fetchone()
            last_event_fetch_sql = (
                event_fetch_at.astimezone(timezone.utc).strftime(_SQLITE_TS_FORMAT)
                if event_fetch_at else
                ((existing["last_event_fetch_at"] if existing and hasattr(existing, "__getitem__") else None) or None)
            )
            last_event_cursor_value = (
                int(event_cursor_seen)
                if event_cursor_seen is not None else
                (int(existing["last_event_cursor_seen"]) if existing and hasattr(existing, "__getitem__") and existing["last_event_cursor_seen"] is not None else None)
            )
            last_inbox_fetch_sql = (
                inbox_fetch_at.astimezone(timezone.utc).strftime(_SQLITE_TS_FORMAT)
                if inbox_fetch_at else
                ((existing["last_inbox_fetch_at"] if existing and hasattr(existing, "__getitem__") else None) or None)
            )
            conn.execute(
                """
                INSERT INTO agent_runtime_state (
                    user_id, last_event_fetch_at, last_event_cursor_seen,
                    last_inbox_fetch_at, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    last_event_fetch_at = excluded.last_event_fetch_at,
                    last_event_cursor_seen = excluded.last_event_cursor_seen,
                    last_inbox_fetch_at = excluded.last_inbox_fetch_at,
                    updated_at = excluded.updated_at
                """,
                (
                    user_id,
                    last_event_fetch_sql,
                    last_event_cursor_value,
                    last_inbox_fetch_sql,
                    now_dt.strftime(_SQLITE_TS_FORMAT),
                ),
            )
            conn.commit()
    except Exception as e:
        logger.debug("Failed to record agent runtime state for %s: %s", user_id, e)


def get_agent_runtime_record(db_manager: Any, user_id: str) -> Dict[str, Any]:
    if not db_manager or not user_id:
        return {
            "last_event_fetch_at": None,
            "last_event_cursor_seen": None,
            "last_inbox_fetch_at": None,
        }
    try:
        ensure_agent_runtime_schema(db_manager)
        with db_manager.get_connection() as conn:
            row = conn.execute(
                """
                SELECT last_event_fetch_at, last_event_cursor_seen, last_inbox_fetch_at
                FROM agent_runtime_state
                WHERE user_id = ?
                """,
                (user_id,),
            ).fetchone()
    except Exception as e:
        logger.debug("Failed to load agent runtime state for %s: %s", user_id, e)
        row = None

    if not row:
        return {
            "last_event_fetch_at": None,
            "last_event_cursor_seen": None,
            "last_inbox_fetch_at": None,
        }
    return {
        "last_event_fetch_at": _to_iso_utc(row["last_event_fetch_at"] if hasattr(row, "__getitem__") else None),
        "last_event_cursor_seen": (
            int(row["last_event_cursor_seen"])
            if hasattr(row, "__getitem__") and row["last_event_cursor_seen"] is not None
            else None
        ),
        "last_inbox_fetch_at": _to_iso_utc(row["last_inbox_fetch_at"] if hasattr(row, "__getitem__") else None),
    }


def build_agent_runtime_payload(db_manager: Any, user_id: str) -> Dict[str, Any]:
    payload = get_agent_runtime_record(db_manager, user_id)
    now_dt = datetime.now(timezone.utc)

    oldest_pending_created_at = None
    oldest_unacked_created_at = None
    if db_manager and user_id:
        try:
            with db_manager.get_connection() as conn:
                inbox_row = conn.execute(
                    """
                    SELECT MIN(created_at) AS oldest_created_at
                    FROM agent_inbox
                    WHERE agent_user_id = ? AND status IN (?, ?)
                    """,
                    (user_id, ACTIONABLE_STATUSES[0], ACTIONABLE_STATUSES[1]),
                ).fetchone()
                mention_row = conn.execute(
                    """
                    SELECT MIN(created_at) AS oldest_created_at
                    FROM mention_events
                    WHERE user_id = ? AND acknowledged_at IS NULL
                    """,
                    (user_id,),
                ).fetchone()
            oldest_pending_created_at = (
                inbox_row["oldest_created_at"] if inbox_row and hasattr(inbox_row, "__getitem__") else None
            )
            oldest_unacked_created_at = (
                mention_row["oldest_created_at"] if mention_row and hasattr(mention_row, "__getitem__") else None
            )
        except Exception as e:
            logger.debug("Failed to compute agent runtime ages for %s: %s", user_id, e)

    oldest_pending_dt = _to_datetime_utc(oldest_pending_created_at)
    oldest_unacked_dt = _to_datetime_utc(oldest_unacked_created_at)

    oldest_pending_age_seconds = (
        max(0, int((now_dt - oldest_pending_dt).total_seconds()))
        if oldest_pending_dt else None
    )
    oldest_unacked_age_seconds = (
        max(0, int((now_dt - oldest_unacked_dt).total_seconds()))
        if oldest_unacked_dt else None
    )

    payload.update({
        "oldest_pending_inbox_at": oldest_pending_dt.isoformat() if oldest_pending_dt else None,
        "oldest_pending_inbox_age_seconds": oldest_pending_age_seconds,
        "oldest_pending_inbox_age_text": _format_age_short(oldest_pending_age_seconds),
        "oldest_unacked_mention_at": oldest_unacked_dt.isoformat() if oldest_unacked_dt else None,
        "oldest_unacked_mention_age_seconds": oldest_unacked_age_seconds,
        "oldest_unacked_mention_age_text": _format_age_short(oldest_unacked_age_seconds),
    })
    return payload
