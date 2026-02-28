"""
Agent heartbeat snapshot helpers.

Builds a lightweight but actionable heartbeat payload that agents can use to
decide whether to fetch catchup and continue work without waiting for mentions.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any, Dict, Optional


def _safe_int(value: Any) -> int:
    try:
        if value is None:
            return 0
        return int(value)
    except Exception:
        return 0


def _to_iso(value: Any) -> Optional[str]:
    if not value:
        return None
    try:
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
        return dt.isoformat()
    except Exception:
        return str(value)


def _to_seq(value: Any) -> Optional[int]:
    """Convert datetime-ish values to a monotonic epoch-millisecond hint."""
    if not value:
        return None
    try:
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
        return int(dt.timestamp() * 1000)
    except Exception:
        return None


def _owner_tokens(user_id: str, username: Optional[str], display_name: Optional[str]) -> list[str]:
    tokens = set()
    for value in (user_id, username, display_name):
        if not value:
            continue
        cleaned = str(value).strip()
        if not cleaned:
            continue
        tokens.add(cleaned.lower())
        tokens.add(f"@{cleaned.lower()}")
        if " " in cleaned:
            underscored = cleaned.replace(" ", "_")
            tokens.add(underscored.lower())
            tokens.add(f"@{underscored.lower()}")
    if username:
        base = str(username).split(".", 1)[0].strip()
        if base:
            tokens.add(base.lower())
            tokens.add(f"@{base.lower()}")
    return sorted(tokens)


def build_agent_heartbeat_snapshot(
    db_manager: Any,
    user_id: str,
    mention_manager: Any = None,
    inbox_manager: Any = None,
) -> Dict[str, Any]:
    """
    Build a heartbeat payload for a user/agent.

    Design goals:
    - Keep response light enough for frequent polling.
    - Include actionable workload counts so agents do not stall when no new mention arrives.
    - Preserve backward-compatible keys used by existing clients.
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    if not user_id:
        return {
            "timestamp": now_iso,
            "agent_user_id": user_id,
            "unacked_mentions": 0,
            "latest_mention_at": None,
            "last_mention_id": None,
            "last_mention_seq": None,
            "pending_inbox": 0,
            "latest_inbox_at": None,
            "last_inbox_id": None,
            "last_inbox_seq": None,
            "last_event_seq": None,
            "active_tasks": 0,
            "assigned_open_tasks": 0,
            "assigned_in_progress_tasks": 0,
            "assigned_blocked_tasks": 0,
            "active_objectives": 0,
            "lead_objectives": 0,
            "active_requests": 0,
            "assignee_requests": 0,
            "reviewer_requests": 0,
            "watcher_requests": 0,
            "owned_handoffs": 0,
            "pending_work_total": 0,
            "directives_hash": None,
            "needs_catchup": False,
            "needs_action": False,
            "poll_hint_seconds": 30,
        }

    unacked_mentions = 0
    latest_mention_at = None
    last_mention_id = None
    last_mention_seq = None
    pending_inbox = 0
    latest_inbox_at = None
    last_inbox_id = None
    last_inbox_seq = None
    assigned_open_tasks = 0
    assigned_in_progress_tasks = 0
    assigned_blocked_tasks = 0
    active_objectives = 0
    lead_objectives = 0
    active_requests = 0
    assignee_requests = 0
    reviewer_requests = 0
    watcher_requests = 0
    owned_handoffs = 0
    directives_hash = None
    username = None
    display_name = None

    if db_manager:
        try:
            user_row = db_manager.get_user(user_id)
            if user_row:
                username = user_row.get("username")
                display_name = user_row.get("display_name")
                if user_row.get("agent_directives"):
                    directives_hash = hashlib.sha256(
                        user_row["agent_directives"].encode()
                    ).hexdigest()[:16]
        except Exception:
            pass

    if db_manager:
        try:
            with db_manager.get_connection() as conn:
                # Mention events
                try:
                    row = conn.execute(
                        """
                        SELECT COUNT(*) AS count, MAX(created_at) AS latest
                        FROM mention_events
                        WHERE user_id = ? AND acknowledged_at IS NULL
                        """,
                        (user_id,),
                    ).fetchone()
                    if row:
                        unacked_mentions = _safe_int(row["count"])
                        latest_mention_at = _to_iso(row["latest"])
                        last_mention_seq = _to_seq(row["latest"])
                    latest_row = conn.execute(
                        """
                        SELECT id, created_at
                        FROM mention_events
                        WHERE user_id = ? AND acknowledged_at IS NULL
                        ORDER BY created_at DESC
                        LIMIT 1
                        """,
                        (user_id,),
                    ).fetchone()
                    if latest_row:
                        last_mention_id = latest_row["id"]
                        if not latest_mention_at:
                            latest_mention_at = _to_iso(latest_row["created_at"])
                        if last_mention_seq is None:
                            last_mention_seq = _to_seq(latest_row["created_at"])
                except Exception:
                    pass

                # Agent inbox
                try:
                    row = conn.execute(
                        """
                        SELECT COUNT(*) AS count, MAX(created_at) AS latest
                        FROM agent_inbox
                        WHERE agent_user_id = ? AND status = 'pending'
                        """,
                        (user_id,),
                    ).fetchone()
                    if row:
                        pending_inbox = _safe_int(row["count"])
                        latest_inbox_at = _to_iso(row["latest"])
                        last_inbox_seq = _to_seq(row["latest"])
                    latest_row = conn.execute(
                        """
                        SELECT id, created_at
                        FROM agent_inbox
                        WHERE agent_user_id = ? AND status = 'pending'
                        ORDER BY created_at DESC
                        LIMIT 1
                        """,
                        (user_id,),
                    ).fetchone()
                    if latest_row:
                        last_inbox_id = latest_row["id"]
                        if not latest_inbox_at:
                            latest_inbox_at = _to_iso(latest_row["created_at"])
                        if last_inbox_seq is None:
                            last_inbox_seq = _to_seq(latest_row["created_at"])
                except Exception:
                    pass

                # Assigned task workload
                try:
                    row = conn.execute(
                        """
                        SELECT
                          COALESCE(SUM(CASE WHEN status = 'open' THEN 1 ELSE 0 END), 0) AS open_count,
                          COALESCE(SUM(CASE WHEN status = 'in_progress' THEN 1 ELSE 0 END), 0) AS in_progress_count,
                          COALESCE(SUM(CASE WHEN status = 'blocked' THEN 1 ELSE 0 END), 0) AS blocked_count
                        FROM tasks
                        WHERE assigned_to = ?
                        """,
                        (user_id,),
                    ).fetchone()
                    if row:
                        assigned_open_tasks = _safe_int(row["open_count"])
                        assigned_in_progress_tasks = _safe_int(row["in_progress_count"])
                        assigned_blocked_tasks = _safe_int(row["blocked_count"])
                except Exception:
                    pass

                # Objectives where user is a member
                try:
                    row = conn.execute(
                        """
                        SELECT
                          COALESCE(SUM(CASE WHEN o.status IN ('pending', 'in_progress') THEN 1 ELSE 0 END), 0) AS active_count,
                          COALESCE(SUM(CASE WHEN om.role = 'lead' AND o.status IN ('pending', 'in_progress') THEN 1 ELSE 0 END), 0) AS lead_count
                        FROM objective_members om
                        JOIN objectives o ON o.id = om.objective_id
                        WHERE om.user_id = ?
                        """,
                        (user_id,),
                    ).fetchone()
                    if row:
                        active_objectives = _safe_int(row["active_count"])
                        lead_objectives = _safe_int(row["lead_count"])
                except Exception:
                    pass

                # Requests where user is a member
                try:
                    row = conn.execute(
                        """
                        SELECT
                          COALESCE(SUM(CASE WHEN r.status IN ('open', 'acknowledged', 'in_progress') THEN 1 ELSE 0 END), 0) AS active_count,
                          COALESCE(SUM(CASE WHEN rm.role = 'assignee' AND r.status IN ('open', 'acknowledged', 'in_progress') THEN 1 ELSE 0 END), 0) AS assignee_count,
                          COALESCE(SUM(CASE WHEN rm.role = 'reviewer' AND r.status IN ('open', 'acknowledged', 'in_progress') THEN 1 ELSE 0 END), 0) AS reviewer_count,
                          COALESCE(SUM(CASE WHEN rm.role = 'watcher' AND r.status IN ('open', 'acknowledged', 'in_progress') THEN 1 ELSE 0 END), 0) AS watcher_count
                        FROM request_members rm
                        JOIN requests r ON r.id = rm.request_id
                        WHERE rm.user_id = ?
                        """,
                        (user_id,),
                    ).fetchone()
                    if row:
                        active_requests = _safe_int(row["active_count"])
                        assignee_requests = _safe_int(row["assignee_count"])
                        reviewer_requests = _safe_int(row["reviewer_count"])
                        watcher_requests = _safe_int(row["watcher_count"])
                except Exception:
                    pass

                # Handoffs owned by this user (owner may be user_id, username, or @handle text)
                try:
                    tokens = _owner_tokens(user_id, username, display_name)
                    if tokens:
                        placeholders = ",".join("?" for _ in tokens)
                        row = conn.execute(
                            f"""
                            SELECT COUNT(*) AS count
                            FROM handoff_notes
                            WHERE owner IS NOT NULL
                              AND TRIM(owner) != ''
                              AND LOWER(owner) IN ({placeholders})
                            """,
                            tokens,
                        ).fetchone()
                        if row:
                            owned_handoffs = _safe_int(row["count"])
                except Exception:
                    pass
        except Exception:
            pass

    # Fallbacks when direct table queries are unavailable on an older node.
    if unacked_mentions == 0 and mention_manager:
        try:
            mention_items = mention_manager.get_mentions(
                user_id=user_id,
                limit=200,
                include_acknowledged=False,
            )
            unacked_mentions = len(mention_items or [])
            if mention_items:
                latest_mention_at = mention_items[0].get("created_at")
                last_mention_id = mention_items[0].get("id")
                last_mention_seq = _to_seq(mention_items[0].get("created_at"))
        except Exception:
            pass

    if pending_inbox == 0 and inbox_manager:
        try:
            count_data = inbox_manager.count_items(user_id=user_id, status="pending")
            pending_inbox = count_data if isinstance(count_data, int) else _safe_int((count_data or {}).get("count", 0))
            preview = inbox_manager.list_items(
                user_id=user_id,
                status="pending",
                limit=1,
                include_handled=False,
            )
            if preview:
                latest_inbox_at = preview[0].get("created_at")
                last_inbox_id = preview[0].get("id")
                last_inbox_seq = _to_seq(preview[0].get("created_at"))
        except Exception:
            pass

    active_tasks = assigned_open_tasks + assigned_in_progress_tasks + assigned_blocked_tasks
    pending_work_total = active_tasks + active_objectives + active_requests + owned_handoffs
    needs_catchup = (unacked_mentions > 0 or pending_inbox > 0)
    needs_action = (needs_catchup or pending_work_total > 0)

    # Poll faster when actionable work exists.
    poll_hint_seconds = 5 if needs_action else 30
    last_event_seq = None
    seq_candidates = [last_mention_seq, last_inbox_seq]
    seq_values = [s for s in seq_candidates if isinstance(s, int)]
    if seq_values:
        last_event_seq = max(seq_values)

    return {
        "timestamp": now_iso,
        "agent_user_id": user_id,
        "unacked_mentions": unacked_mentions,
        "latest_mention_at": latest_mention_at,
        "last_mention_id": last_mention_id,
        "last_mention_seq": last_mention_seq,
        "pending_inbox": pending_inbox,
        "latest_inbox_at": latest_inbox_at,
        "last_inbox_id": last_inbox_id,
        "last_inbox_seq": last_inbox_seq,
        "last_event_seq": last_event_seq,
        # Backward-compatible key retained; now scoped to assigned actionable tasks.
        "active_tasks": active_tasks,
        "assigned_open_tasks": assigned_open_tasks,
        "assigned_in_progress_tasks": assigned_in_progress_tasks,
        "assigned_blocked_tasks": assigned_blocked_tasks,
        "active_objectives": active_objectives,
        "lead_objectives": lead_objectives,
        "active_requests": active_requests,
        "assignee_requests": assignee_requests,
        "reviewer_requests": reviewer_requests,
        "watcher_requests": watcher_requests,
        "owned_handoffs": owned_handoffs,
        "pending_work_total": pending_work_total,
        "directives_hash": directives_hash,
        # Existing field used by current clients.
        "needs_catchup": needs_catchup,
        # New field: true when any actionable work exists even without new mentions.
        "needs_action": needs_action,
        "poll_hint_seconds": poll_hint_seconds,
    }


def build_actionable_work_preview(
    db_manager: Any,
    user_id: str,
    limit: int = 10,
) -> Dict[str, Any]:
    """Return a compact preview of actionable work assigned to a user/agent."""
    limit_val = max(1, min(int(limit or 10), 50))
    payload: Dict[str, Any] = {
        "assigned_tasks": [],
        "member_objectives": [],
        "member_requests": [],
        "owned_handoffs": [],
        "total": 0,
    }
    if not db_manager or not user_id:
        return payload

    username = None
    display_name = None
    try:
        user_row = db_manager.get_user(user_id)
        if user_row:
            username = user_row.get("username")
            display_name = user_row.get("display_name")
    except Exception:
        pass

    try:
        with db_manager.get_connection() as conn:
            try:
                rows = conn.execute(
                    """
                    SELECT id, title, status, priority, objective_id, due_at, updated_at
                    FROM tasks
                    WHERE assigned_to = ?
                      AND status IN ('open', 'in_progress', 'blocked')
                    ORDER BY updated_at DESC
                    LIMIT ?
                    """,
                    (user_id, limit_val),
                ).fetchall()
                payload["assigned_tasks"] = [
                    {
                        "task_id": row["id"],
                        "title": row["title"],
                        "status": row["status"],
                        "priority": row["priority"],
                        "objective_id": row["objective_id"],
                        "due_at": _to_iso(row["due_at"]),
                        "updated_at": _to_iso(row["updated_at"]),
                    }
                    for row in (rows or [])
                ]
            except Exception:
                pass

            try:
                rows = conn.execute(
                    """
                    SELECT o.id, o.title, o.status, o.updated_at, om.role
                    FROM objective_members om
                    JOIN objectives o ON o.id = om.objective_id
                    WHERE om.user_id = ?
                      AND o.status IN ('pending', 'in_progress')
                    ORDER BY o.updated_at DESC
                    LIMIT ?
                    """,
                    (user_id, limit_val),
                ).fetchall()
                payload["member_objectives"] = [
                    {
                        "objective_id": row["id"],
                        "title": row["title"],
                        "status": row["status"],
                        "role": row["role"],
                        "updated_at": _to_iso(row["updated_at"]),
                    }
                    for row in (rows or [])
                ]
            except Exception:
                pass

            try:
                rows = conn.execute(
                    """
                    SELECT r.id, r.title, r.status, r.priority, r.due_at, r.updated_at, rm.role
                    FROM request_members rm
                    JOIN requests r ON r.id = rm.request_id
                    WHERE rm.user_id = ?
                      AND r.status IN ('open', 'acknowledged', 'in_progress')
                    ORDER BY r.updated_at DESC
                    LIMIT ?
                    """,
                    (user_id, limit_val),
                ).fetchall()
                payload["member_requests"] = [
                    {
                        "request_id": row["id"],
                        "title": row["title"],
                        "status": row["status"],
                        "priority": row["priority"],
                        "role": row["role"],
                        "due_at": _to_iso(row["due_at"]),
                        "updated_at": _to_iso(row["updated_at"]),
                    }
                    for row in (rows or [])
                ]
            except Exception:
                pass

            try:
                tokens = _owner_tokens(user_id, username, display_name)
                if tokens:
                    placeholders = ",".join("?" for _ in tokens)
                    query = f"""
                        SELECT id, title, source_type, source_id, channel_id, owner, updated_at
                        FROM handoff_notes
                        WHERE owner IS NOT NULL
                          AND TRIM(owner) != ''
                          AND LOWER(owner) IN ({placeholders})
                        ORDER BY updated_at DESC
                        LIMIT ?
                    """
                    rows = conn.execute(query, [*tokens, limit_val]).fetchall()
                    payload["owned_handoffs"] = [
                        {
                            "handoff_id": row["id"],
                            "title": row["title"],
                            "source_type": row["source_type"],
                            "source_id": row["source_id"],
                            "channel_id": row["channel_id"],
                            "owner": row["owner"],
                            "updated_at": _to_iso(row["updated_at"]),
                        }
                        for row in (rows or [])
                    ]
            except Exception:
                pass
    except Exception:
        return payload

    payload["total"] = (
        len(payload["assigned_tasks"])
        + len(payload["member_objectives"])
        + len(payload["member_requests"])
        + len(payload["owned_handoffs"])
    )
    return payload
