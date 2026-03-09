"""Workspace event journal helpers.

Additive, local-only event spine for incremental UI and agent consumers.
This module is intentionally a thin read/delivery model and not a source of truth.
"""

from __future__ import annotations

import json
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence


logger = logging.getLogger(__name__)


EVENT_DM_MESSAGE_CREATED = "dm.message.created"
EVENT_DM_MESSAGE_EDITED = "dm.message.edited"
EVENT_DM_MESSAGE_DELETED = "dm.message.deleted"
EVENT_MENTION_CREATED = "mention.created"
EVENT_MENTION_ACKNOWLEDGED = "mention.acknowledged"
EVENT_INBOX_ITEM_CREATED = "inbox.item.created"
EVENT_INBOX_ITEM_UPDATED = "inbox.item.updated"
EVENT_ATTACHMENT_AVAILABLE = "attachment.available"

PATCH1_EVENT_TYPES = {
    EVENT_DM_MESSAGE_CREATED,
    EVENT_DM_MESSAGE_EDITED,
    EVENT_DM_MESSAGE_DELETED,
    EVENT_MENTION_CREATED,
    EVENT_MENTION_ACKNOWLEDGED,
    EVENT_INBOX_ITEM_CREATED,
    EVENT_INBOX_ITEM_UPDATED,
    EVENT_ATTACHMENT_AVAILABLE,
}

WORKSPACE_EVENT_RETENTION_DAYS = 30
WORKSPACE_EVENT_MAX_ROWS = 50_000


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


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


class WorkspaceEventManager:
    """Durable local event journal for UI and agent consumers."""

    def __init__(self, db_manager: Any):
        self.db = db_manager
        self._emit_counter = 0
        self._ensure_tables()
        self.prune_old_events()

    def _ensure_tables(self) -> None:
        if not self.db:
            return
        try:
            with self.db.get_connection() as conn:
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS workspace_events (
                        seq INTEGER PRIMARY KEY AUTOINCREMENT,
                        event_id TEXT UNIQUE NOT NULL,
                        event_type TEXT NOT NULL,
                        actor_user_id TEXT,
                        target_user_id TEXT,
                        channel_id TEXT,
                        post_id TEXT,
                        message_id TEXT,
                        visibility_scope TEXT NOT NULL,
                        dedupe_key TEXT,
                        payload_json TEXT,
                        created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                    );
                    CREATE INDEX IF NOT EXISTS idx_workspace_events_created_at
                        ON workspace_events(created_at);
                    CREATE INDEX IF NOT EXISTS idx_workspace_events_event_type_created
                        ON workspace_events(event_type, created_at);
                    CREATE INDEX IF NOT EXISTS idx_workspace_events_target_created
                        ON workspace_events(target_user_id, created_at);
                    CREATE INDEX IF NOT EXISTS idx_workspace_events_message_created
                        ON workspace_events(message_id, created_at);
                    CREATE INDEX IF NOT EXISTS idx_workspace_events_channel_created
                        ON workspace_events(channel_id, created_at);
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_workspace_events_dedupe
                        ON workspace_events(dedupe_key)
                        WHERE dedupe_key IS NOT NULL;
                    """
                )
                conn.commit()
        except Exception as e:
            logger.error("Failed to ensure workspace_events table: %s", e)

    def emit_event(
        self,
        *,
        event_type: str,
        visibility_scope: str,
        actor_user_id: Optional[str] = None,
        target_user_id: Optional[str] = None,
        channel_id: Optional[str] = None,
        post_id: Optional[str] = None,
        message_id: Optional[str] = None,
        payload: Optional[Dict[str, Any]] = None,
        dedupe_key: Optional[str] = None,
        created_at: Optional[Any] = None,
        event_id: Optional[str] = None,
    ) -> Optional[int]:
        if not self.db or not event_type or not visibility_scope:
            return None
        if event_type not in PATCH1_EVENT_TYPES:
            logger.warning("Ignoring unsupported workspace event type: %s", event_type)
            return None
        event_id = str(event_id or f"EVT{secrets.token_hex(8)}").strip()
        created_iso = _to_iso(created_at) or _now_utc().isoformat()
        payload_json = json.dumps(payload) if payload else None
        dedupe_text = str(dedupe_key or "").strip() or None
        try:
            with self.db.get_connection() as conn:
                cur = conn.execute(
                    """
                    INSERT OR IGNORE INTO workspace_events
                    (event_id, event_type, actor_user_id, target_user_id, channel_id,
                     post_id, message_id, visibility_scope, dedupe_key, payload_json, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event_id,
                        event_type,
                        actor_user_id,
                        target_user_id,
                        channel_id,
                        post_id,
                        message_id,
                        visibility_scope,
                        dedupe_text,
                        payload_json,
                        created_iso,
                    ),
                )
                if not cur.rowcount:
                    existing = None
                    if dedupe_text:
                        existing = conn.execute(
                            "SELECT seq FROM workspace_events WHERE dedupe_key = ? LIMIT 1",
                            (dedupe_text,),
                        ).fetchone()
                    conn.commit()
                    return int(existing["seq"]) if existing else None
                row = conn.execute("SELECT last_insert_rowid() AS seq").fetchone()
                conn.commit()
                seq = int((row["seq"] if row and hasattr(row, "__getitem__") else 0) or 0)
            self._emit_counter += 1
            if self._emit_counter % 50 == 0:
                self.prune_old_events()
            return seq or None
        except Exception as e:
            logger.warning("Failed to emit workspace event %s: %s", event_type, e)
            return None

    def prune_old_events(
        self,
        *,
        retention_days: int = WORKSPACE_EVENT_RETENTION_DAYS,
        max_rows: int = WORKSPACE_EVENT_MAX_ROWS,
    ) -> int:
        if not self.db:
            return 0
        removed = 0
        try:
            cutoff = (_now_utc() - timedelta(days=max(1, int(retention_days)))).isoformat()
        except Exception:
            cutoff = (_now_utc() - timedelta(days=WORKSPACE_EVENT_RETENTION_DAYS)).isoformat()
        try:
            with self.db.get_connection() as conn:
                cur = conn.execute(
                    "DELETE FROM workspace_events WHERE created_at < ?",
                    (cutoff,),
                )
                removed += cur.rowcount or 0
                row = conn.execute("SELECT COUNT(*) AS n FROM workspace_events").fetchone()
                total = int((row["n"] if row and hasattr(row, "__getitem__") else 0) or 0)
                overflow = max(0, total - max_rows)
                if overflow > 0:
                    cur = conn.execute(
                        """
                        DELETE FROM workspace_events
                        WHERE seq IN (
                            SELECT seq FROM workspace_events
                            ORDER BY seq ASC
                            LIMIT ?
                        )
                        """,
                        (overflow,),
                    )
                    removed += cur.rowcount or 0
                conn.commit()
        except Exception as e:
            logger.warning("Failed to prune workspace events: %s", e)
        return removed

    def get_latest_seq(self) -> Optional[int]:
        if not self.db:
            return None
        try:
            with self.db.get_connection() as conn:
                row = conn.execute("SELECT MAX(seq) AS seq FROM workspace_events").fetchone()
            if not row:
                return None
            value = row["seq"] if hasattr(row, "__getitem__") else None
            return int(value) if value is not None else None
        except Exception:
            return None

    def get_diagnostics(self, *, limit: int = 50) -> Dict[str, Any]:
        try:
            limit_val = max(1, min(int(limit), 200))
        except Exception:
            limit_val = 50
        result = {
            "count": 0,
            "oldest_created_at": None,
            "latest_created_at": None,
            "latest_seq": None,
            "type_counts": {},
            "items": [],
        }
        if not self.db:
            return result
        try:
            with self.db.get_connection() as conn:
                stats = conn.execute(
                    """
                    SELECT COUNT(*) AS n,
                           MIN(created_at) AS oldest_created_at,
                           MAX(created_at) AS latest_created_at,
                           MAX(seq) AS latest_seq
                    FROM workspace_events
                    """
                ).fetchone()
                type_rows = conn.execute(
                    """
                    SELECT event_type, COUNT(*) AS n
                    FROM workspace_events
                    GROUP BY event_type
                    ORDER BY event_type ASC
                    """
                ).fetchall()
                rows = conn.execute(
                    """
                    SELECT seq, event_id, event_type, actor_user_id, target_user_id,
                           channel_id, post_id, message_id, visibility_scope,
                           dedupe_key, payload_json, created_at
                    FROM workspace_events
                    ORDER BY seq DESC
                    LIMIT ?
                    """,
                    (limit_val,),
                ).fetchall()
            result["count"] = int((stats["n"] if stats and hasattr(stats, "__getitem__") else 0) or 0)
            result["oldest_created_at"] = _to_iso(stats["oldest_created_at"]) if stats else None
            result["latest_created_at"] = _to_iso(stats["latest_created_at"]) if stats else None
            latest_seq = stats["latest_seq"] if stats and hasattr(stats, "__getitem__") else None
            result["latest_seq"] = int(latest_seq) if latest_seq is not None else None
            result["type_counts"] = {
                str(row["event_type"]): int((row["n"] if hasattr(row, "__getitem__") else 0) or 0)
                for row in (type_rows or [])
                if row and row["event_type"]
            }
            result["items"] = [
                self._serialize_diagnostic_row(row)
                for row in rows or []
            ]
            return result
        except Exception as e:
            logger.warning("Failed to load workspace event diagnostics: %s", e)
            return result

    def list_events_for_user(
        self,
        *,
        user_id: str,
        after_seq: Optional[Any] = None,
        limit: Optional[Any] = None,
        types: Optional[Sequence[str]] = None,
        can_read_messages: bool = True,
    ) -> Dict[str, Any]:
        if not self.db or not user_id:
            return {"items": [], "next_after_seq": 0, "has_more": False}
        try:
            after_seq_val = max(0, int(after_seq or 0))
        except Exception:
            after_seq_val = 0
        try:
            limit_val = max(1, min(int(limit or 50), 200))
        except Exception:
            limit_val = 50

        allowed_types = [t for t in (types or []) if t in PATCH1_EVENT_TYPES]
        items: List[Dict[str, Any]] = []
        scan_cursor = after_seq_val
        chunk_size = max(100, limit_val * 3)
        has_more = False

        while len(items) < limit_val:
            params: List[Any] = [scan_cursor]
            where = "WHERE seq > ?"
            if allowed_types:
                placeholders = ",".join("?" for _ in allowed_types)
                where += f" AND event_type IN ({placeholders})"
                params.extend(allowed_types)
            try:
                with self.db.get_connection() as conn:
                    rows = conn.execute(
                        f"""
                        SELECT seq, event_id, event_type, actor_user_id, target_user_id,
                               channel_id, post_id, message_id, visibility_scope,
                               dedupe_key, payload_json, created_at
                        FROM workspace_events
                        {where}
                        ORDER BY seq ASC
                        LIMIT ?
                        """,
                        params + [chunk_size],
                    ).fetchall()
            except Exception as e:
                logger.warning("Failed to list workspace events: %s", e)
                break
            if not rows:
                has_more = False
                break

            batch_has_more = len(rows) == chunk_size
            max_scanned_seq = int(rows[-1]["seq"])
            for idx, row in enumerate(rows):
                row_seq = int(row["seq"])
                if self._row_visible_to_user(row, user_id=user_id, can_read_messages=can_read_messages):
                    items.append(self._serialize_row(row))
                    scan_cursor = row_seq
                    if len(items) >= limit_val:
                        has_more = bool(idx < len(rows) - 1 or batch_has_more)
                        break
            else:
                scan_cursor = max_scanned_seq

            if len(items) >= limit_val:
                break
            if not batch_has_more:
                has_more = False
                break
            has_more = True

        return {
            "items": items,
            "next_after_seq": scan_cursor,
            "has_more": has_more,
        }

    def _serialize_row(self, row: Any) -> Dict[str, Any]:
        payload = None
        try:
            if row["payload_json"]:
                payload = json.loads(row["payload_json"])
        except Exception:
            payload = None
        return {
            "seq": int(row["seq"]),
            "event_id": row["event_id"],
            "event_type": row["event_type"],
            "actor_user_id": row["actor_user_id"],
            "target_user_id": row["target_user_id"],
            "channel_id": row["channel_id"],
            "post_id": row["post_id"],
            "message_id": row["message_id"],
            "visibility_scope": row["visibility_scope"],
            "created_at": _to_iso(row["created_at"]),
            "payload": payload,
        }

    def _serialize_diagnostic_row(self, row: Any) -> Dict[str, Any]:
        payload = None
        payload_keys: List[str] = []
        payload_preview = None
        try:
            if row["payload_json"]:
                loaded = json.loads(row["payload_json"])
                if isinstance(loaded, dict):
                    payload = loaded
        except Exception:
            payload = None
        if isinstance(payload, dict):
            payload_keys = sorted(str(key) for key in payload.keys())
            preview_value = payload.get("preview")
            if preview_value is None and payload.get("content"):
                preview_value = payload.get("content")
            if preview_value is not None:
                payload_preview = str(preview_value).strip()[:160]
        return {
            "seq": int(row["seq"]),
            "event_id": row["event_id"],
            "event_type": row["event_type"],
            "actor_user_id": row["actor_user_id"],
            "target_user_id": row["target_user_id"],
            "channel_id": row["channel_id"],
            "post_id": row["post_id"],
            "message_id": row["message_id"],
            "visibility_scope": row["visibility_scope"],
            "dedupe_key": row["dedupe_key"],
            "created_at": _to_iso(row["created_at"]),
            "payload_keys": payload_keys,
            "payload_preview": payload_preview,
        }

    def _row_visible_to_user(self, row: Any, *, user_id: str, can_read_messages: bool) -> bool:
        event_type = str(row["event_type"] or "").strip().lower()
        visibility_scope = str(row["visibility_scope"] or "").strip().lower()
        target_user_id = str(row["target_user_id"] or "").strip()
        message_id = str(row["message_id"] or "").strip()

        if event_type.startswith("mention.") or event_type.startswith("inbox.item."):
            return bool(target_user_id and target_user_id == user_id)

        if visibility_scope == "dm" or event_type.startswith("dm.message."):
            if not can_read_messages or not message_id:
                return False
            if self._can_user_view_dm_message(user_id, message_id):
                return True
            return self._user_visible_via_payload(row, user_id=user_id)

        if event_type == EVENT_ATTACHMENT_AVAILABLE:
            if message_id:
                if not can_read_messages:
                    return False
                if self._can_user_view_dm_message(user_id, message_id):
                    return True
                return self._user_visible_via_payload(row, user_id=user_id)
            if target_user_id:
                return target_user_id == user_id
            return False

        return False

    def _user_visible_via_payload(self, row: Any, *, user_id: str) -> bool:
        try:
            payload_raw = row["payload_json"]
        except Exception:
            payload_raw = None
        if not payload_raw:
            return False
        try:
            payload = json.loads(payload_raw)
        except Exception:
            return False
        if not isinstance(payload, dict):
            return False

        sender_id = str(payload.get("sender_id") or "").strip()
        recipient_id = str(payload.get("recipient_id") or "").strip()
        if user_id and user_id in {sender_id, recipient_id}:
            return True

        members = payload.get("group_members") or []
        if isinstance(members, list):
            normalized = {str(member or "").strip() for member in members}
            return user_id in normalized
        return False

    def _can_user_view_dm_message(self, user_id: str, message_id: str) -> bool:
        try:
            with self.db.get_connection() as conn:
                row = conn.execute(
                    """
                    SELECT sender_id, recipient_id, metadata
                    FROM messages
                    WHERE id = ?
                    """,
                    (message_id,),
                ).fetchone()
            if not row:
                return False
            if row["sender_id"] == user_id or row["recipient_id"] == user_id:
                return True
            metadata = {}
            try:
                if row["metadata"]:
                    loaded = json.loads(row["metadata"])
                    if isinstance(loaded, dict):
                        metadata = loaded
            except Exception:
                metadata = {}
            members = metadata.get("group_members") or []
            return isinstance(members, list) and user_id in [str(m or "").strip() for m in members]
        except Exception:
            return False
