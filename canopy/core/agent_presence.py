"""
Agent presence helpers for status badges.

Tracks lightweight last check-in timestamps and derives a badge-friendly
presence state (online/recent/idle/offline) from recency.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Sequence

logger = logging.getLogger(__name__)

_SQLITE_TS_FORMAT = "%Y-%m-%d %H:%M:%S.%f"

# Presence thresholds (seconds)
ONLINE_MAX_AGE = 120
RECENT_MAX_AGE = 15 * 60
IDLE_MAX_AGE = 60 * 60


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


def ensure_agent_presence_schema(db_manager: Any) -> None:
    """Ensure the agent_presence table exists."""
    if not db_manager:
        return
    with db_manager.get_connection() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS agent_presence (
                user_id TEXT PRIMARY KEY,
                last_checkin_at TIMESTAMP NOT NULL,
                last_source TEXT,
                updated_at TIMESTAMP NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_agent_presence_checkin
                ON agent_presence(last_checkin_at);
            """
        )
        conn.commit()


def record_agent_checkin(
    db_manager: Any,
    user_id: str,
    source: str = "heartbeat",
    checkin_at: Optional[datetime] = None,
) -> Optional[str]:
    """Upsert last check-in for a user and return ISO timestamp."""
    if not db_manager or not user_id:
        return None
    now_dt = checkin_at or datetime.now(timezone.utc)
    if now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=timezone.utc)
    now_dt = now_dt.astimezone(timezone.utc)
    now_sql = now_dt.strftime(_SQLITE_TS_FORMAT)
    try:
        ensure_agent_presence_schema(db_manager)
        with db_manager.get_connection() as conn:
            conn.execute(
                """
                INSERT INTO agent_presence (user_id, last_checkin_at, last_source, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    last_checkin_at = excluded.last_checkin_at,
                    last_source = excluded.last_source,
                    updated_at = excluded.updated_at
                """,
                (user_id, now_sql, source or "heartbeat", now_sql),
            )
            conn.commit()
        return now_dt.isoformat()
    except Exception as e:
        logger.debug(f"Failed to record agent check-in for {user_id}: {e}")
        return None


def get_agent_presence_records(
    db_manager: Any,
    user_ids: Sequence[str],
) -> Dict[str, Dict[str, Optional[str]]]:
    """Return last-check-in metadata keyed by user_id."""
    ids = [str(uid).strip() for uid in (user_ids or []) if uid and str(uid).strip()]
    if not db_manager or not ids:
        return {}
    try:
        placeholders = ",".join("?" for _ in ids)
        with db_manager.get_connection() as conn:
            rows = conn.execute(
                f"""
                SELECT user_id, last_checkin_at, last_source, updated_at
                FROM agent_presence
                WHERE user_id IN ({placeholders})
                """,
                ids,
            ).fetchall()
    except Exception as e:
        if "no such table" in str(e).lower():
            return {}
        logger.debug(f"Failed to load agent presence records: {e}")
        return {}

    out: Dict[str, Dict[str, Optional[str]]] = {}
    for row in rows or []:
        uid = str(row["user_id"] if hasattr(row, "__getitem__") else row[0])
        out[uid] = {
            "last_check_in_at": _to_iso_utc(row["last_checkin_at"] if hasattr(row, "__getitem__") else row[1]),
            "last_check_in_source": (row["last_source"] if hasattr(row, "__getitem__") else row[2]) or None,
            "updated_at": _to_iso_utc(row["updated_at"] if hasattr(row, "__getitem__") else row[3]),
        }
    return out


def build_agent_presence_payload(
    *,
    last_check_in_at: Any = None,
    is_remote: bool = False,
    account_type: str = "agent",
    now_dt: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Derive badge metadata from last check-in recency."""
    acct = str(account_type or "").strip().lower() or "human"
    if acct != "agent":
        return {
            "state": "human",
            "label": "Human",
            "color": "secondary",
            "age_seconds": None,
            "age_text": None,
            "last_check_in_at": None,
        }

    now = now_dt or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    now = now.astimezone(timezone.utc)

    checkin_dt = _to_datetime_utc(last_check_in_at)
    if not checkin_dt:
        return {
            "state": "remote_unknown" if is_remote else "no_checkin",
            "label": "Remote" if is_remote else "No check-in",
            "color": "dark" if is_remote else "secondary",
            "age_seconds": None,
            "age_text": None,
            "last_check_in_at": None,
        }

    age_seconds = max(0, int((now - checkin_dt).total_seconds()))
    if age_seconds <= ONLINE_MAX_AGE:
        state = "online"
        label = "Online"
        color = "success"
    elif age_seconds <= RECENT_MAX_AGE:
        state = "recent"
        label = "Recent"
        color = "info"
    elif age_seconds <= IDLE_MAX_AGE:
        state = "idle"
        label = "Idle"
        color = "warning"
    else:
        state = "offline"
        label = "Offline"
        color = "secondary"

    return {
        "state": state,
        "label": label,
        "color": color,
        "age_seconds": age_seconds,
        "age_text": _format_age_short(age_seconds),
        "last_check_in_at": checkin_dt.isoformat(),
    }
