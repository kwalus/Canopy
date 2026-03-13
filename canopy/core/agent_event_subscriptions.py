"""Agent event subscription preferences.

Stores per-agent preferred workspace event families for low-noise wake loops.
Subscriptions may only narrow the event stream; permission filtering still
applies at read time and can remove event families from the effective feed.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set

from .events import (
    PATCH1_EVENT_TYPES,
    EVENT_ATTACHMENT_AVAILABLE,
    EVENT_CHANNEL_MESSAGE_CREATED,
    EVENT_CHANNEL_MESSAGE_DELETED,
    EVENT_CHANNEL_MESSAGE_EDITED,
    EVENT_DM_MESSAGE_CREATED,
    EVENT_DM_MESSAGE_DELETED,
    EVENT_DM_MESSAGE_EDITED,
    EVENT_INBOX_ITEM_CREATED,
    EVENT_INBOX_ITEM_UPDATED,
    EVENT_MENTION_ACKNOWLEDGED,
    EVENT_MENTION_CREATED,
)


_SQLITE_TS_FORMAT = "%Y-%m-%d %H:%M:%S.%f"

AGENT_DEFAULT_EVENT_TYPES = {
    EVENT_ATTACHMENT_AVAILABLE,
    EVENT_DM_MESSAGE_CREATED,
    EVENT_DM_MESSAGE_DELETED,
    EVENT_DM_MESSAGE_EDITED,
    EVENT_MENTION_CREATED,
    EVENT_MENTION_ACKNOWLEDGED,
    EVENT_INBOX_ITEM_CREATED,
    EVENT_INBOX_ITEM_UPDATED,
}

AGENT_MESSAGE_EVENT_TYPES = {
    EVENT_ATTACHMENT_AVAILABLE,
    EVENT_DM_MESSAGE_CREATED,
    EVENT_DM_MESSAGE_DELETED,
    EVENT_DM_MESSAGE_EDITED,
    EVENT_CHANNEL_MESSAGE_CREATED,
    EVENT_CHANNEL_MESSAGE_EDITED,
    EVENT_CHANNEL_MESSAGE_DELETED,
}

# Keep the full agent-visible subscription surface centralized so routes,
# heartbeat, and diagnostics do not drift as new workspace event families land.
AGENT_SUPPORTED_EVENT_TYPES = set(PATCH1_EVENT_TYPES)


def ensure_agent_event_subscription_schema(db_manager: Any) -> None:
    if not db_manager:
        return
    with db_manager.get_connection() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS agent_event_subscription_state (
                user_id TEXT PRIMARY KEY,
                custom_enabled INTEGER NOT NULL DEFAULT 0,
                updated_at TIMESTAMP NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_agent_event_subscription_state_enabled
                ON agent_event_subscription_state(custom_enabled, updated_at);
            CREATE TABLE IF NOT EXISTS agent_event_subscriptions (
                user_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                updated_at TIMESTAMP NOT NULL,
                PRIMARY KEY (user_id, event_type)
            );
            CREATE INDEX IF NOT EXISTS idx_agent_event_subscriptions_user
                ON agent_event_subscriptions(user_id, updated_at);
            """
        )
        conn.commit()


def get_agent_event_subscriptions(db_manager: Any, user_id: str) -> Optional[List[str]]:
    if not db_manager or not user_id:
        return None
    try:
        ensure_agent_event_subscription_schema(db_manager)
        with db_manager.get_connection() as conn:
            state_row = conn.execute(
                """
                SELECT custom_enabled
                FROM agent_event_subscription_state
                WHERE user_id = ?
                """,
                (user_id,),
            ).fetchone()
            if not state_row or not int(state_row["custom_enabled"] or 0):
                return None
            rows = conn.execute(
                """
                SELECT event_type
                FROM agent_event_subscriptions
                WHERE user_id = ?
                ORDER BY event_type ASC
                """,
                (user_id,),
            ).fetchall()
        if rows is None:
            return None
        return [str(row["event_type"] or "").strip() for row in rows if str(row["event_type"] or "").strip()]
    except Exception:
        return None


def get_agent_event_subscription_state(db_manager: Any, user_id: str) -> Dict[str, Any]:
    if not db_manager or not user_id:
        return {
            "custom_enabled": False,
            "stored_types": None,
            "updated_at": None,
        }
    try:
        ensure_agent_event_subscription_schema(db_manager)
        with db_manager.get_connection() as conn:
            state_row = conn.execute(
                """
                SELECT custom_enabled, updated_at
                FROM agent_event_subscription_state
                WHERE user_id = ?
                """,
                (user_id,),
            ).fetchone()
            rows = conn.execute(
                """
                SELECT event_type
                FROM agent_event_subscriptions
                WHERE user_id = ?
                ORDER BY event_type ASC
                """,
                (user_id,),
            ).fetchall()
        stored_types = [
            str(row["event_type"] or "").strip()
            for row in (rows or [])
            if str(row["event_type"] or "").strip()
        ]
        custom_enabled = bool(state_row and int(state_row["custom_enabled"] or 0))
        updated_at = str(state_row["updated_at"] or "").strip() if state_row and state_row["updated_at"] else None
        return {
            "custom_enabled": custom_enabled,
            "stored_types": stored_types if custom_enabled else None,
            "updated_at": updated_at,
        }
    except Exception:
        return {
            "custom_enabled": False,
            "stored_types": None,
            "updated_at": None,
        }


def set_agent_event_subscriptions(
    db_manager: Any,
    user_id: str,
    event_types: Sequence[str],
) -> List[str]:
    if not db_manager or not user_id:
        return []
    clean_types = sorted(
        {
            str(event_type or "").strip()
            for event_type in (event_types or [])
            if str(event_type or "").strip()
        }
    )
    ensure_agent_event_subscription_schema(db_manager)
    now_sql = datetime.now(timezone.utc).strftime(_SQLITE_TS_FORMAT)
    with db_manager.get_connection() as conn:
        conn.execute(
            """
            INSERT INTO agent_event_subscription_state (user_id, custom_enabled, updated_at)
            VALUES (?, 1, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                custom_enabled = 1,
                updated_at = excluded.updated_at
            """,
            (user_id, now_sql),
        )
        conn.execute(
            "DELETE FROM agent_event_subscriptions WHERE user_id = ?",
            (user_id,),
        )
        if clean_types:
            conn.executemany(
                """
                INSERT INTO agent_event_subscriptions (user_id, event_type, updated_at)
                VALUES (?, ?, ?)
                """,
                [(user_id, event_type, now_sql) for event_type in clean_types],
            )
        conn.commit()
    return clean_types


def reset_agent_event_subscriptions(db_manager: Any, user_id: str) -> None:
    if not db_manager or not user_id:
        return
    ensure_agent_event_subscription_schema(db_manager)
    now_sql = datetime.now(timezone.utc).strftime(_SQLITE_TS_FORMAT)
    with db_manager.get_connection() as conn:
        conn.execute(
            """
            INSERT INTO agent_event_subscription_state (user_id, custom_enabled, updated_at)
            VALUES (?, 0, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                custom_enabled = 0,
                updated_at = excluded.updated_at
            """,
            (user_id, now_sql),
        )
        conn.execute(
            "DELETE FROM agent_event_subscriptions WHERE user_id = ?",
            (user_id,),
        )
        conn.commit()


def resolve_agent_event_subscription(
    *,
    requested_types: Sequence[str],
    stored_types: Optional[Sequence[str]],
    default_types: Iterable[str],
    message_required_types: Iterable[str],
    supported_types: Iterable[str],
    can_read_messages: bool,
) -> Dict[str, Any]:
    supported: Set[str] = {
        str(item or "").strip()
        for item in supported_types
        if str(item or "").strip()
    }
    defaults: Set[str] = {
        str(item or "").strip()
        for item in default_types
        if str(item or "").strip()
    } & supported
    message_required: Set[str] = {
        str(item or "").strip()
        for item in message_required_types
        if str(item or "").strip()
    } & supported
    requested_clean: List[str] = sorted(
        {
            str(item or "").strip()
            for item in (requested_types or [])
            if str(item or "").strip() in supported
        }
    )
    stored_clean: Optional[List[str]] = None
    if stored_types is not None:
        stored_clean = sorted(
            {
                str(item or "").strip()
                for item in (stored_types or [])
                if str(item or "").strip() in supported
            }
        )

    if requested_clean:
        source = "request"
        selected = requested_clean
    elif stored_clean is not None:
        source = "stored"
        selected = stored_clean
    else:
        source = "default"
        selected = sorted(defaults)

    unavailable = []
    effective = list(selected)
    if not can_read_messages:
        unavailable = [item for item in effective if item in message_required]
        effective = [item for item in effective if item not in message_required]

    return {
        "subscription_source": source,
        "stored_types": stored_clean,
        "selected_types": selected,
        "effective_types": effective,
        "unavailable_types": unavailable,
        "supported_types": sorted(supported),
        "default_types": sorted(defaults),
    }
