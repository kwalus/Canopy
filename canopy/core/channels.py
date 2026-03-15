"""
Slack-style channel system for Canopy.

Implements channel-based organization similar to Slack, with real-time
messaging, threading, and work-focused communication.

Author: Konrad Walus (architecture, design, and direction)
Project: Canopy - Local Mesh Communication
License: Apache 2.0
Development: AI-assisted implementation (Claude, Codex, GitHub Copilot, Cursor IDE, Ollama)
"""

import logging
import math
import secrets
import json
import hashlib
import threading
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Any, Union, Tuple, cast
from dataclasses import dataclass
from enum import Enum

from .database import DatabaseManager
from .events import (
    EVENT_CHANNEL_MESSAGE_CREATED,
    EVENT_CHANNEL_MESSAGE_DELETED,
    EVENT_CHANNEL_MESSAGE_EDITED,
    EVENT_CHANNEL_MESSAGE_READ,
    EVENT_CHANNEL_STATE_UPDATED,
)
from ..security.api_keys import ApiKeyManager, Permission
from .logging_config import log_performance, LogOperation
from ..network.routing import (
    decode_channel_key_material as decode_channel_key_material_value,
    encode_channel_key_material as encode_channel_key_material_value,
)

logger = logging.getLogger('canopy.channels')


class ChannelType(Enum):
    """Types of channels supported."""
    PUBLIC = "public"      # Anyone can join and see
    PRIVATE = "private"    # Invite-only
    DM = "dm"             # Direct message (2 people)
    GROUP_DM = "group_dm" # Group direct message (3+ people)
    GENERAL = "general"   # Company-wide general channel


class MessageType(Enum):
    """Types of messages supported."""
    TEXT = "text"
    SYSTEM = "system"     # System notifications
    FILE = "file"         # File attachments
    IMAGE = "image"       # Image attachments
    LINK = "link"         # Link shares
    THREAD_REPLY = "thread_reply"  # Reply to a thread


@dataclass
class Channel:
    """Represents a communication channel."""
    id: str
    name: str
    channel_type: ChannelType
    created_by: str
    created_at: datetime
    description: Optional[str] = None
    topic: Optional[str] = None
    member_count: int = 0
    unread_count: int = 0
    last_message_at: Optional[datetime] = None
    last_activity_at: Optional[datetime] = None
    origin_peer: Optional[str] = None
    privacy_mode: str = 'open'
    user_role: str = 'member'
    notifications_enabled: bool = True
    crypto_mode: str = 'legacy_plaintext'
    lifecycle_ttl_days: Optional[int] = None
    lifecycle_preserved: bool = False
    archived_at: Optional[datetime] = None
    archive_reason: Optional[str] = None
    lifecycle_status: str = 'active'
    days_until_archive: Optional[int] = None
    owner_peer_state: Optional[str] = None
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert channel to dictionary."""
        return {
            'id': self.id,
            'name': self.name,
            'type': self.channel_type.value,
            'created_by': self.created_by,
            'created_at': self.created_at.isoformat(),
            'description': self.description,
            'topic': self.topic,
            'member_count': self.member_count,
            'unread_count': self.unread_count,
            'last_message_at': self.last_message_at.isoformat() if self.last_message_at else None,
            'last_activity_at': self.last_activity_at.isoformat() if self.last_activity_at else None,
            'origin_peer': self.origin_peer,
            'privacy_mode': self.privacy_mode,
            'user_role': self.user_role,
            'notifications_enabled': self.notifications_enabled,
            'crypto_mode': self.crypto_mode,
            'lifecycle_ttl_days': self.lifecycle_ttl_days,
            'lifecycle_preserved': self.lifecycle_preserved,
            'archived_at': self.archived_at.isoformat() if self.archived_at else None,
            'archive_reason': self.archive_reason,
            'lifecycle_status': self.lifecycle_status,
            'days_until_archive': self.days_until_archive,
            'owner_peer_state': self.owner_peer_state,
        }


@dataclass
class Message:
    """Represents a message in a channel."""
    id: str
    channel_id: str
    user_id: str
    content: str
    message_type: MessageType
    created_at: datetime
    thread_id: Optional[str] = None  # If this is a thread reply
    parent_message_id: Optional[str] = None  # For threading
    reactions: Optional[Dict[str, List[str]]] = None  # emoji -> [user_ids]
    attachments: Optional[List[Dict[str, Any]]] = None
    security: Optional[Dict[str, Any]] = None
    edited_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    origin_peer: Optional[str] = None
    crypto_state: Optional[str] = None

    @staticmethod
    def normalize_attachment(attachment: Any) -> Optional[Dict[str, Any]]:
        """Return a canonical attachment dict for UI/API compatibility.

        Canonical keys are:
        - id (or file_id alias)
        - name
        - type
        - size
        - url (optional)

        Agents sometimes send attachment metadata using upload-response keys
        (file_id, filename, content_type). This normalizer preserves backward
        compatibility by mapping aliases into canonical fields.
        """
        if isinstance(attachment, str):
            file_id = attachment.strip()
            if not file_id:
                return None
            return {
                'id': file_id,
                'file_id': file_id,
                'name': file_id,
                'type': 'application/octet-stream',
            }

        if not isinstance(attachment, dict):
            return None

        att = dict(attachment)
        file_id = att.get('id') or att.get('file_id')
        if file_id:
            file_id_str = str(file_id).strip()
            if file_id_str:
                att['id'] = file_id_str
                att.setdefault('file_id', file_id_str)

        name = (
            att.get('name')
            or att.get('filename')
            or att.get('original_name')
            or att.get('file_name')
        )
        if name:
            att['name'] = str(name)

        content_type = (
            att.get('type')
            or att.get('content_type')
            or att.get('mime_type')
            or att.get('mime')
        )
        if content_type:
            att['type'] = str(content_type)

        if 'size' in att and att.get('size') is not None:
            try:
                att['size'] = int(att.get('size'))
            except (TypeError, ValueError):
                pass

        return att

    @classmethod
    def normalize_attachments(cls, attachments: Optional[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
        """Normalize an attachment list while preserving unknown extra keys."""
        normalized: List[Dict[str, Any]] = []
        for attachment in attachments or []:
            att = cls.normalize_attachment(attachment)
            if att:
                normalized.append(att)
        return normalized

    @property
    def is_expired(self) -> bool:
        """Return True if this message has expired."""
        if not self.expires_at:
            return False
        now = datetime.now(timezone.utc)
        exp = self.expires_at
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        return exp <= now
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert message to dictionary."""
        attachments = self.normalize_attachments(self.attachments)
        return {
            'id': self.id,
            'channel_id': self.channel_id,
            'user_id': self.user_id,
            'content': self.content,
            'type': self.message_type.value,
            'created_at': self.created_at.isoformat(),
            'expires_at': self.expires_at.isoformat() if self.expires_at else None,
            'thread_id': self.thread_id,
            'parent_message_id': self.parent_message_id,
            'reactions': self.reactions or {},
            'attachments': attachments,
            'security': self.security or {},
            'edited_at': self.edited_at.isoformat() if self.edited_at else None,
            'origin_peer': self.origin_peer,
            'crypto_state': self.crypto_state,
        }


class ChannelManager:
    """Manages Slack-style channels and messaging."""

    DEFAULT_TTL_DAYS = 90  # Quarterly default
    DEFAULT_TTL_SECONDS = DEFAULT_TTL_DAYS * 24 * 3600
    DEFAULT_CHANNEL_LIFECYCLE_DAYS = 180
    MAX_CHANNEL_LIFECYCLE_DAYS = 730
    CHANNEL_LIFECYCLE_WARNING_DAYS = 14
    CHANNEL_LIFECYCLE_SCAN_INTERVAL_SECONDS = 60
    # Upper bound on message retention to prevent unbounded growth.
    MAX_TTL_DAYS = 730  # 2 years
    MAX_TTL_SECONDS = MAX_TTL_DAYS * 24 * 3600
    # Backward-compatibility window for legacy no-expiry semantics.
    LEGACY_NO_EXPIRY_TTL_DAYS = 365  # 1 year
    LEGACY_NO_EXPIRY_TTL_SECONDS = LEGACY_NO_EXPIRY_TTL_DAYS * 24 * 3600
    TARGETED_PRIVACY_MODES = {'private', 'confidential'}
    PRIVACY_ORDER = {
        'open': 0,
        'guarded': 1,
        'private': 2,
        'confidential': 3,
    }
    SECURITY_ALLOWED_KEYS = {
        'algorithm',
        'ciphertext',
        'nonce',
        'sender_key',
        'recipient_key',
        'version',
        'payload_format',
        'key_id',
        'tag',
        'aad',
        'iv',
        'salt',
        'kdf',
    }
    SECURITY_MAX_JSON_BYTES = 16384
    SECURITY_MAX_VALUE_BYTES = 4096
    SECURITY_MAX_LIST_ITEMS = 64
    GOVERNANCE_MAX_ALLOWED_CHANNELS = 512
    GOVERNANCE_MAX_CHANNEL_ID_LENGTH = 128
    CRYPTO_MODE_LEGACY = 'legacy_plaintext'
    CRYPTO_MODE_E2E_OPTIONAL = 'e2e_optional'
    CRYPTO_MODE_E2E_ENFORCED = 'e2e_enforced'
    ALLOWED_CRYPTO_MODES = {
        CRYPTO_MODE_LEGACY,
        CRYPTO_MODE_E2E_OPTIONAL,
        CRYPTO_MODE_E2E_ENFORCED,
    }
    SYNC_DIGEST_VERSION = 1
    SYNC_DIGEST_EMPTY_ROOT = hashlib.sha256(
        b"canopy:sync_digest:v1:empty"
    ).hexdigest()
    
    def __init__(self, db: DatabaseManager, api_key_manager: ApiKeyManager):
        """Initialize channel manager."""
        logger.info("Initializing ChannelManager")
        self.db = db
        self.api_key_manager = api_key_manager
        self.workspace_events: Any = None
        self._channel_key_lock = threading.RLock()
        self._channel_key_cache: Dict[Tuple[str, str], bytes] = {}
        self._lifecycle_scan_lock = threading.RLock()
        self._last_lifecycle_scan_at: Optional[datetime] = None
        
        # Ensure database tables exist
        with LogOperation("Channel tables initialization"):
            self._ensure_tables()
        
        # Create default general channel if it doesn't exist
        with LogOperation("Default channel creation"):
            self._ensure_default_channels()
        
        logger.info("ChannelManager initialized successfully")

    def _channel_member_user_ids(self, channel_id: str) -> List[str]:
        """Return all member ids for a channel for local UI event fanout."""
        if not channel_id:
            return []
        try:
            with self.db.get_connection() as conn:
                rows = conn.execute(
                    "SELECT user_id FROM channel_members WHERE channel_id = ? ORDER BY user_id ASC",
                    (channel_id,),
                ).fetchall()
            seen: set[str] = set()
            members: List[str] = []
            for row in rows or []:
                user_id = str(row["user_id"] or "").strip()
                if not user_id or user_id in seen:
                    continue
                seen.add(user_id)
                members.append(user_id)
            return members
        except Exception:
            return []

    def _emit_channel_user_event(
        self,
        *,
        channel_id: str,
        event_type: str,
        actor_user_id: Optional[str] = None,
        payload: Optional[Dict[str, Any]] = None,
        target_user_ids: Optional[List[str]] = None,
        exclude_user_ids: Optional[List[str]] = None,
        dedupe_suffix: Optional[str] = None,
    ) -> None:
        if not self.workspace_events or not channel_id or not event_type:
            return

        exclude = {
            str(user_id or "").strip()
            for user_id in (exclude_user_ids or [])
            if str(user_id or "").strip()
        }
        targets = target_user_ids or self._channel_member_user_ids(channel_id)
        for target_user_id in targets:
            clean_target = str(target_user_id or "").strip()
            if not clean_target or clean_target in exclude:
                continue
            dedupe_key = f"{event_type}:{channel_id}:{clean_target}"
            if dedupe_suffix:
                dedupe_key = f"{dedupe_key}:{dedupe_suffix}"
            self.workspace_events.emit_event(
                event_type=event_type,
                actor_user_id=actor_user_id,
                target_user_id=clean_target,
                channel_id=channel_id,
                visibility_scope="user",
                dedupe_key=dedupe_key,
                payload=payload or {},
            )

    @staticmethod
    def _parse_datetime(value: Any) -> Optional[datetime]:
        """Parse a timestamp string into a timezone-aware datetime (UTC)."""
        if not value:
            return None
        if isinstance(value, datetime):
            dt = value
        else:
            try:
                dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
            except Exception:
                try:
                    dt = datetime.strptime(str(value), '%Y-%m-%d %H:%M:%S')
                except Exception:
                    return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt

    @staticmethod
    def _format_db_timestamp(dt: datetime) -> str:
        """Format a datetime for SQLite comparisons (UTC, no timezone suffix)."""
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')

    @classmethod
    def _normalize_channel_lifecycle_ttl_days(
        cls,
        ttl_days: Optional[Any],
        default: Optional[int] = None,
    ) -> Optional[int]:
        """Clamp lifecycle TTL days to a safe supported range."""
        fallback = cls.DEFAULT_CHANNEL_LIFECYCLE_DAYS if default is None else default
        if ttl_days is None:
            return fallback
        try:
            ttl_val = int(ttl_days)
        except (TypeError, ValueError):
            return fallback
        if ttl_val <= 0:
            return fallback
        return max(1, min(ttl_val, cls.MAX_CHANNEL_LIFECYCLE_DAYS))

    def _run_channel_lifecycle_scan(
        self,
        conn: Any,
        now: Optional[datetime] = None,
    ) -> int:
        """Soft-archive inactive channels based on lifecycle metadata."""
        now_dt = now or datetime.now(timezone.utc)
        if now_dt.tzinfo is None:
            now_dt = now_dt.replace(tzinfo=timezone.utc)
        now_db = self._format_db_timestamp(now_dt)
        cursor = conn.execute(
            """
            UPDATE channels
               SET lifecycle_archived_at = COALESCE(lifecycle_archived_at, ?),
                   lifecycle_archive_reason = COALESCE(lifecycle_archive_reason, 'inactive_ttl')
             WHERE COALESCE(lifecycle_preserved, 0) = 0
               AND COALESCE(channel_type, 'public') NOT IN ('dm', 'group_dm', 'general')
               AND lifecycle_archived_at IS NULL
               AND julianday(COALESCE(last_activity_at, created_at)) +
                   COALESCE(lifecycle_ttl_days, ?) <= julianday(?)
            """,
            (now_db, self.DEFAULT_CHANNEL_LIFECYCLE_DAYS, now_db),
        )
        return int(getattr(cursor, 'rowcount', 0) or 0)

    def _maybe_run_channel_lifecycle_scan(self, force: bool = False) -> None:
        """Throttle lifecycle scans so sidebar polling does not churn the DB."""
        now_dt = datetime.now(timezone.utc)
        if not force and self._last_lifecycle_scan_at:
            delta = (now_dt - self._last_lifecycle_scan_at).total_seconds()
            if delta < self.CHANNEL_LIFECYCLE_SCAN_INTERVAL_SECONDS:
                return
        with self._lifecycle_scan_lock:
            if not force and self._last_lifecycle_scan_at:
                delta = (now_dt - self._last_lifecycle_scan_at).total_seconds()
                if delta < self.CHANNEL_LIFECYCLE_SCAN_INTERVAL_SECONDS:
                    return
            try:
                with self.db.get_connection() as conn:
                    archived = self._run_channel_lifecycle_scan(conn, now=now_dt)
                    conn.commit()
                if archived:
                    logger.info("Soft-archived %d inactive channel(s)", archived)
            except Exception as e:
                logger.debug(f"Channel lifecycle scan skipped: {e}")
            finally:
                self._last_lifecycle_scan_at = now_dt

    def touch_channel_activity(
        self,
        channel_id: str,
        activity_at: Optional[Any] = None,
        revive_archived: bool = True,
    ) -> bool:
        """Refresh a channel's activity timestamp and optionally unarchive it."""
        if not channel_id:
            return False
        activity_dt = self._parse_datetime(activity_at) or datetime.now(timezone.utc)
        activity_db = self._format_db_timestamp(activity_dt)
        try:
            with self.db.get_connection() as conn:
                if revive_archived:
                    conn.execute(
                        """
                        UPDATE channels
                           SET last_activity_at = ?,
                               lifecycle_archived_at = NULL,
                               lifecycle_archive_reason = NULL
                         WHERE id = ?
                        """,
                        (activity_db, channel_id),
                    )
                else:
                    conn.execute(
                        "UPDATE channels SET last_activity_at = ? WHERE id = ?",
                        (activity_db, channel_id),
                    )
                conn.commit()
            return True
        except Exception as e:
            logger.debug(f"Failed to touch channel activity for {channel_id}: {e}")
            return False

    def describe_channel_lifecycle(
        self,
        channel: Channel,
        now: Optional[datetime] = None,
        local_peer_id: Optional[str] = None,
        connected_peer_ids: Optional[set[str]] = None,
        known_peer_ids: Optional[set[str]] = None,
    ) -> Dict[str, Any]:
        """Return derived lifecycle state for one channel."""
        now_dt = now or datetime.now(timezone.utc)
        if now_dt.tzinfo is None:
            now_dt = now_dt.replace(tzinfo=timezone.utc)

        last_activity = channel.last_activity_at or channel.last_message_at or channel.created_at
        ttl_days = self._normalize_channel_lifecycle_ttl_days(
            channel.lifecycle_ttl_days,
            default=self.DEFAULT_CHANNEL_LIFECYCLE_DAYS,
        )
        archived_at = channel.archived_at
        preserved = bool(channel.lifecycle_preserved)
        warning_window = self.CHANNEL_LIFECYCLE_WARNING_DAYS

        owner_state = 'unknown'
        origin_peer = str(channel.origin_peer or '').strip()
        if not origin_peer or (local_peer_id and origin_peer == local_peer_id):
            owner_state = 'local'
        elif connected_peer_ids is not None and origin_peer in connected_peer_ids:
            owner_state = 'connected'
        elif known_peer_ids is not None and origin_peer in known_peer_ids:
            owner_state = 'known'

        status = 'active'
        days_until_archive: Optional[int] = None
        if archived_at:
            status = 'archived'
        elif preserved:
            status = 'preserved'
        elif last_activity and ttl_days:
            due_at = last_activity + timedelta(days=ttl_days)
            remaining_seconds = (due_at - now_dt).total_seconds()
            days_until_archive = max(0, int((remaining_seconds + 86399) // 86400))
            if remaining_seconds <= 0:
                status = 'inactive'
            elif days_until_archive <= warning_window:
                status = 'cooling'

        return {
            'status': status,
            'ttl_days': ttl_days,
            'preserved': preserved,
            'last_activity_at': last_activity,
            'archived_at': archived_at,
            'archive_reason': channel.archive_reason,
            'days_until_archive': days_until_archive,
            'owner_peer_state': owner_state,
        }

    def _resolve_expiry(self,
                        expires_at: Optional[Any] = None,
                        ttl_seconds: Optional[int] = None,
                        ttl_mode: Optional[str] = None,
                        apply_default: bool = True,
                        base_time: Optional[datetime] = None) -> Optional[datetime]:
        """Resolve expiry for a channel message based on explicit expiry, TTL, or defaults."""
        base = base_time or datetime.now(timezone.utc)
        if base.tzinfo is None:
            base = base.replace(tzinfo=timezone.utc)

        max_expiry = base + timedelta(seconds=self.MAX_TTL_SECONDS)
        compatibility_no_expiry = base + timedelta(seconds=self.LEGACY_NO_EXPIRY_TTL_SECONDS)

        ttl_mode_norm = str(ttl_mode or '').strip().lower()
        if ttl_mode_norm in ('none', 'no_expiry', 'immortal'):
            resolved = compatibility_no_expiry
            return min(resolved, max_expiry)

        if expires_at:
            parsed = self._parse_datetime(expires_at)
            if parsed:
                return min(parsed, max_expiry)

        if ttl_seconds is not None:
            try:
                ttl_val = int(ttl_seconds)
            except (TypeError, ValueError):
                ttl_val = None
            if ttl_val is not None:
                if ttl_val > 0:
                    return min(base + timedelta(seconds=ttl_val), max_expiry)
                if apply_default:
                    return min(base + timedelta(seconds=self.DEFAULT_TTL_SECONDS), max_expiry)
                if ttl_mode_norm in ('none', 'no_expiry', 'immortal'):
                    # Legacy clients can send ttl_seconds=0 with ttl_mode to
                    # request no-expiry semantics.
                    return min(compatibility_no_expiry, max_expiry)
                return None

        if apply_default:
            return min(base + timedelta(seconds=self.DEFAULT_TTL_SECONDS), max_expiry)

        return None

    def validate_security_metadata(
        self,
        security: Optional[Dict[str, Any]],
        strict: bool = True
    ) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        """Validate and sanitize security metadata for channel messages."""
        if security is None:
            return None, None
        if not isinstance(security, dict):
            return None, "Security metadata must be an object"

        sanitized: Dict[str, Any] = {}
        for key, value in security.items():
            if key not in self.SECURITY_ALLOWED_KEYS:
                if strict:
                    return None, f"Unsupported security field: {key}"
                continue

            if value is None or isinstance(value, (str, int, float, bool)):
                if isinstance(value, str) and len(value) > self.SECURITY_MAX_VALUE_BYTES:
                    return None, f"Security field too large: {key}"
                sanitized[key] = value
                continue

            if isinstance(value, list):
                if len(value) > self.SECURITY_MAX_LIST_ITEMS:
                    return None, f"Security field list too long: {key}"
                for item in value:
                    if not isinstance(item, (str, int, float, bool)) and item is not None:
                        return None, f"Unsupported security list item: {key}"
                    if isinstance(item, str) and len(item) > self.SECURITY_MAX_VALUE_BYTES:
                        return None, f"Security field too large: {key}"
                sanitized[key] = value
                continue

            if strict:
                return None, f"Unsupported security value for {key}"
            return None, "Invalid security metadata"

        if not sanitized:
            return None, None

        try:
            encoded = json.dumps(sanitized)
        except Exception:
            return None, "Security metadata not serializable"

        if len(encoded.encode('utf-8')) > self.SECURITY_MAX_JSON_BYTES:
            return None, "Security metadata too large"

        return sanitized, None
    
    def _ensure_tables(self) -> None:
        """Ensure channel-related tables exist."""
        logger.info("Ensuring channel database tables exist...")
        
        try:
            with self.db.get_connection() as conn:
                conn.executescript("""
                    -- Channels table
                    CREATE TABLE IF NOT EXISTS channels (
                        id TEXT PRIMARY KEY,
                        name TEXT NOT NULL,
                        channel_type TEXT NOT NULL,
                        created_by TEXT NOT NULL,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        last_activity_at TIMESTAMP,
                        description TEXT,
                        topic TEXT,
                        crypto_mode TEXT DEFAULT 'legacy_plaintext',
                        lifecycle_ttl_days INTEGER DEFAULT 180,
                        lifecycle_preserved BOOLEAN DEFAULT 0,
                        lifecycle_archived_at TIMESTAMP,
                        lifecycle_archive_reason TEXT,
                        FOREIGN KEY (created_by) REFERENCES users (id)
                    );
                    
                    -- Channel members table
                    CREATE TABLE IF NOT EXISTS channel_members (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        channel_id TEXT NOT NULL,
                        user_id TEXT NOT NULL,
                        joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        role TEXT DEFAULT 'member',  -- admin, member
                        notifications_enabled BOOLEAN DEFAULT TRUE,
                        last_read_at TIMESTAMP,
                        FOREIGN KEY (channel_id) REFERENCES channels (id) ON DELETE CASCADE,
                        FOREIGN KEY (user_id) REFERENCES users (id),
                        UNIQUE(channel_id, user_id)
                    );
                    
                    -- Channel messages table  
                    CREATE TABLE IF NOT EXISTS channel_messages (
                        id TEXT PRIMARY KEY,
                        channel_id TEXT NOT NULL,
                        user_id TEXT NOT NULL,
                        content TEXT NOT NULL,
                        message_type TEXT DEFAULT 'text',
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        origin_peer TEXT,
                        expires_at TIMESTAMP,
                        thread_id TEXT,
                        parent_message_id TEXT,
                        reactions TEXT,  -- JSON blob
                        attachments TEXT,  -- JSON blob
                        security TEXT,  -- JSON blob for future encryption metadata
                        encrypted_content TEXT,  -- future E2E payload storage
                        crypto_state TEXT DEFAULT 'plaintext',  -- plaintext|encrypted|pending_key|decrypt_failed
                        key_id TEXT,
                        nonce TEXT,
                        edited_at TIMESTAMP,
                        FOREIGN KEY (channel_id) REFERENCES channels (id) ON DELETE CASCADE,
                        FOREIGN KEY (user_id) REFERENCES users (id),
                        FOREIGN KEY (parent_message_id) REFERENCES channel_messages (id)
                    );
                    
                    -- Indexes for performance
                    CREATE INDEX IF NOT EXISTS idx_channel_members_channel ON channel_members(channel_id);
                    CREATE INDEX IF NOT EXISTS idx_channel_members_user ON channel_members(user_id);
                    CREATE INDEX IF NOT EXISTS idx_channel_messages_channel ON channel_messages(channel_id);
                    CREATE INDEX IF NOT EXISTS idx_channel_messages_created_at ON channel_messages(created_at);
                    CREATE INDEX IF NOT EXISTS idx_channel_messages_thread ON channel_messages(thread_id);
                    -- Unique constraint for public channel names
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_channels_public_name ON channels(name) WHERE channel_type = 'public';

                    -- Private-channel E2E key state (phase-1 scaffolding).
                    CREATE TABLE IF NOT EXISTS channel_keys (
                        channel_id TEXT NOT NULL,
                        key_id TEXT NOT NULL,
                        key_material_enc TEXT NOT NULL,
                        created_by_peer TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        revoked_at TIMESTAMP,
                        metadata TEXT,
                        PRIMARY KEY (channel_id, key_id),
                        FOREIGN KEY (channel_id) REFERENCES channels(id) ON DELETE CASCADE
                    );
                    CREATE INDEX IF NOT EXISTS idx_channel_keys_channel_active
                        ON channel_keys(channel_id, revoked_at, created_at DESC);

                    CREATE TABLE IF NOT EXISTS channel_member_keys (
                        channel_id TEXT NOT NULL,
                        key_id TEXT NOT NULL,
                        peer_id TEXT NOT NULL,
                        delivery_state TEXT NOT NULL DEFAULT 'pending',
                        last_error TEXT,
                        delivered_at TIMESTAMP,
                        acked_at TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        PRIMARY KEY (channel_id, key_id, peer_id),
                        FOREIGN KEY (channel_id, key_id)
                            REFERENCES channel_keys(channel_id, key_id)
                            ON DELETE CASCADE
                    );
                    CREATE INDEX IF NOT EXISTS idx_channel_member_keys_peer
                        ON channel_member_keys(peer_id, delivery_state, updated_at DESC);

                    CREATE TABLE IF NOT EXISTS channel_member_sync_deliveries (
                        sync_id TEXT PRIMARY KEY,
                        channel_id TEXT NOT NULL,
                        target_user_id TEXT NOT NULL,
                        action TEXT NOT NULL,
                        role TEXT DEFAULT 'member',
                        target_peer_id TEXT NOT NULL,
                        payload_json TEXT,
                        delivery_state TEXT NOT NULL DEFAULT 'pending',
                        last_error TEXT,
                        attempt_count INTEGER DEFAULT 0,
                        last_attempt_at TIMESTAMP,
                        acked_at TIMESTAMP,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        FOREIGN KEY (channel_id) REFERENCES channels(id) ON DELETE CASCADE
                    );
                    CREATE INDEX IF NOT EXISTS idx_channel_member_sync_peer
                        ON channel_member_sync_deliveries(target_peer_id, delivery_state, updated_at DESC);
                    CREATE INDEX IF NOT EXISTS idx_channel_member_sync_channel
                        ON channel_member_sync_deliveries(channel_id, target_user_id, action, updated_at DESC);

                    -- Optional catch-up digest cache for Merkle-assisted sync.
                    CREATE TABLE IF NOT EXISTS channel_sync_digests (
                        channel_id TEXT PRIMARY KEY,
                        digest_version INTEGER NOT NULL,
                        root_hash TEXT NOT NULL,
                        live_count INTEGER NOT NULL,
                        max_created_at TIMESTAMP,
                        computed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        FOREIGN KEY (channel_id) REFERENCES channels(id) ON DELETE CASCADE
                    );
                """)

                # Add origin_peer column if it doesn't exist (migration)
                try:
                    conn.execute("SELECT origin_peer FROM channels LIMIT 1")
                except Exception:
                    conn.execute("ALTER TABLE channels ADD COLUMN origin_peer TEXT DEFAULT NULL")
                    logger.info("Added origin_peer column to channels table")

                # Add privacy_mode column if missing
                try:
                    conn.execute("SELECT privacy_mode FROM channels LIMIT 1")
                except Exception:
                    conn.execute("ALTER TABLE channels ADD COLUMN privacy_mode TEXT DEFAULT 'open'")
                    logger.info("Added privacy_mode column to channels table")

                # Add crypto_mode column if missing (phase-1 E2E scaffolding)
                try:
                    conn.execute("SELECT crypto_mode FROM channels LIMIT 1")
                except Exception:
                    conn.execute(
                        "ALTER TABLE channels ADD COLUMN crypto_mode TEXT DEFAULT 'legacy_plaintext'"
                    )
                    logger.info("Added crypto_mode column to channels table")

                for col, sql in [
                    ('last_activity_at', "ALTER TABLE channels ADD COLUMN last_activity_at TIMESTAMP"),
                    (
                        'lifecycle_ttl_days',
                        f"ALTER TABLE channels ADD COLUMN lifecycle_ttl_days INTEGER DEFAULT {self.DEFAULT_CHANNEL_LIFECYCLE_DAYS}",
                    ),
                    ('lifecycle_preserved', "ALTER TABLE channels ADD COLUMN lifecycle_preserved BOOLEAN DEFAULT 0"),
                    ('lifecycle_archived_at', "ALTER TABLE channels ADD COLUMN lifecycle_archived_at TIMESTAMP"),
                    ('lifecycle_archive_reason', "ALTER TABLE channels ADD COLUMN lifecycle_archive_reason TEXT"),
                ]:
                    try:
                        conn.execute(f"SELECT {col} FROM channels LIMIT 1")
                    except Exception:
                        conn.execute(sql)
                        logger.info(f"Added {col} column to channels table")

                try:
                    conn.execute(
                        "CREATE INDEX IF NOT EXISTS idx_channels_last_activity ON channels(last_activity_at)"
                    )
                    conn.execute(
                        "CREATE INDEX IF NOT EXISTS idx_channels_lifecycle_archived ON channels(lifecycle_archived_at, lifecycle_preserved)"
                    )
                except Exception as idx_err:
                    logger.debug(f"Could not create channel lifecycle indexes: {idx_err}")

                # Add expires_at column to channel_messages if missing
                try:
                    conn.execute("SELECT expires_at FROM channel_messages LIMIT 1")
                except Exception:
                    conn.execute("ALTER TABLE channel_messages ADD COLUMN expires_at TIMESTAMP")
                    # Backfill existing messages with default TTL so the network shrinks over time
                    try:
                        conn.execute("""
                            UPDATE channel_messages
                            SET expires_at = datetime(created_at, ?)
                            WHERE expires_at IS NULL
                        """, (f'+{self.DEFAULT_TTL_DAYS} days',))
                    except Exception as backfill_err:
                        logger.debug(f"Channel message TTL backfill skipped: {backfill_err}")
                    logger.info("Added expires_at column to channel_messages table")

                # Add origin_peer column to channel_messages if missing
                try:
                    conn.execute("SELECT origin_peer FROM channel_messages LIMIT 1")
                except Exception:
                    conn.execute("ALTER TABLE channel_messages ADD COLUMN origin_peer TEXT")
                    logger.info("Added origin_peer column to channel_messages table")

                # Ensure expires_at index exists now that the column is guaranteed
                try:
                    conn.execute("""
                        CREATE INDEX IF NOT EXISTS idx_channel_messages_expires_at
                        ON channel_messages(expires_at)
                    """)
                except Exception as idx_err:
                    logger.debug(f"Could not create expires_at index: {idx_err}")

                # Add ttl_seconds, ttl_mode, edited_at, security so catchup/edit can send them
                for col, typ in [('ttl_seconds', 'INTEGER'), ('ttl_mode', 'TEXT'), ('edited_at', 'TIMESTAMP'), ('security', 'TEXT')]:
                    try:
                        conn.execute(f"SELECT {col} FROM channel_messages LIMIT 1")
                    except Exception:
                        conn.execute(f"ALTER TABLE channel_messages ADD COLUMN {col} {typ}")
                        logger.info(f"Added {col} column to channel_messages table")

                # Phase-1 E2E scaffolding columns on channel_messages
                for col, typ, default_sql in [
                    ('encrypted_content', 'TEXT', None),
                    ('crypto_state', 'TEXT', "DEFAULT 'plaintext'"),
                    ('key_id', 'TEXT', None),
                    ('nonce', 'TEXT', None),
                ]:
                    try:
                        conn.execute(f"SELECT {col} FROM channel_messages LIMIT 1")
                    except Exception:
                        if default_sql:
                            conn.execute(
                                f"ALTER TABLE channel_messages ADD COLUMN {col} {typ} {default_sql}"
                            )
                        else:
                            conn.execute(
                                f"ALTER TABLE channel_messages ADD COLUMN {col} {typ}"
                            )
                        logger.info(f"Added {col} column to channel_messages table")

                try:
                    conn.execute("""
                        CREATE INDEX IF NOT EXISTS idx_channel_messages_crypto_state
                        ON channel_messages(crypto_state)
                    """)
                    conn.execute("""
                        CREATE INDEX IF NOT EXISTS idx_channel_messages_key_id
                        ON channel_messages(key_id)
                    """)
                except Exception as idx_err:
                    logger.debug(f"Could not create crypto indexes: {idx_err}")

                # Add last_activity_at for reply resurfacing (Circle decision)
                try:
                    conn.execute("SELECT last_activity_at FROM channel_messages LIMIT 1")
                except Exception:
                    conn.execute("ALTER TABLE channel_messages ADD COLUMN last_activity_at TIMESTAMP")
                    conn.execute("CREATE INDEX IF NOT EXISTS idx_channel_messages_last_activity ON channel_messages(last_activity_at)")
                    logger.info("Added last_activity_at column to channel_messages table")

                # Add notifications_enabled to channel_members for per-user mute
                try:
                    conn.execute("SELECT notifications_enabled FROM channel_members LIMIT 1")
                except Exception:
                    conn.execute("ALTER TABLE channel_members ADD COLUMN notifications_enabled BOOLEAN DEFAULT 1")
                    logger.info("Added notifications_enabled column to channel_members table")

                # Migration: strip leading '#' from channel names
                # (the '#' is a display prefix added by the UI)
                try:
                    fixed = conn.execute("""
                        UPDATE channels SET name = LTRIM(name, '#')
                        WHERE name LIKE '#%'
                    """).rowcount
                    if fixed:
                        conn.commit()
                        logger.info(f"Stripped '#' prefix from {fixed} channel name(s)")
                except Exception as e:
                    logger.debug(f"Channel name '#' migration skipped: {e}")

                try:
                    conn.execute(
                        """
                        UPDATE channels
                           SET lifecycle_ttl_days = COALESCE(lifecycle_ttl_days, ?)
                        """,
                        (self.DEFAULT_CHANNEL_LIFECYCLE_DAYS,),
                    )
                    conn.execute(
                        """
                        UPDATE channels
                           SET lifecycle_preserved = 1
                         WHERE id = 'general'
                            OR channel_type = 'general'
                        """
                    )
                    conn.execute(
                        """
                        UPDATE channels
                           SET last_activity_at = COALESCE(
                               last_activity_at,
                               (
                                   SELECT MAX(COALESCE(cm.last_activity_at, cm.created_at))
                                     FROM channel_messages cm
                                    WHERE cm.channel_id = channels.id
                               ),
                               created_at
                           )
                        """
                    )
                except Exception as lifecycle_backfill_err:
                    logger.debug(f"Channel lifecycle backfill skipped: {lifecycle_backfill_err}")

                # Peer device profiles — store name, description, avatar
                # received from remote peers so the UI can show them.
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS peer_device_profiles (
                        peer_id TEXT PRIMARY KEY,
                        display_name TEXT,
                        description TEXT,
                        avatar_b64 TEXT,
                        avatar_mime TEXT,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)

                # Processed-messages table — persistent deduplication
                # Prevents duplicate messages after restart + catch-up.
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS processed_messages (
                        message_id TEXT PRIMARY KEY,
                        processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_processed_messages_at
                    ON processed_messages(processed_at)
                """)

                # Per-user channel governance policy.
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS user_channel_governance (
                        user_id TEXT PRIMARY KEY,
                        enabled BOOLEAN NOT NULL DEFAULT 0,
                        block_public_channels BOOLEAN NOT NULL DEFAULT 0,
                        restrict_to_allowed_channels BOOLEAN NOT NULL DEFAULT 0,
                        allowed_channel_ids TEXT NOT NULL DEFAULT '[]',
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_by TEXT,
                        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
                    )
                """)
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_user_channel_governance_enabled
                    ON user_channel_governance(enabled)
                """)

                # Per-user subscriptions for thread reply inbox notifications.
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS channel_thread_subscriptions (
                        channel_id TEXT NOT NULL,
                        thread_root_message_id TEXT NOT NULL,
                        user_id TEXT NOT NULL,
                        subscribed BOOLEAN NOT NULL DEFAULT 1,
                        source TEXT NOT NULL DEFAULT 'manual',
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        PRIMARY KEY (thread_root_message_id, user_id),
                        FOREIGN KEY (channel_id) REFERENCES channels(id) ON DELETE CASCADE,
                        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
                    )
                """)
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_channel_thread_subscriptions_channel
                    ON channel_thread_subscriptions(channel_id, thread_root_message_id, subscribed)
                """)
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_channel_thread_subscriptions_user
                    ON channel_thread_subscriptions(user_id, subscribed)
                """)
                
                conn.commit()
                logger.info("Channel database tables ensured successfully")
                
        except Exception as e:
            logger.error(f"Failed to ensure channel tables: {e}", exc_info=True)
            raise
    
    def _ensure_default_channels(self) -> None:
        """Ensure default channels exist."""
        logger.info("Ensuring default channels exist...")
        
        try:
            with self.db.get_connection() as conn:
                # Ensure system user exists
                conn.execute("""
                    INSERT OR IGNORE INTO users (id, username, public_key)
                    VALUES ('system', 'System', 'system_public_key')
                """)
                
                # Ensure local user exists
                conn.execute("""
                    INSERT OR IGNORE INTO users (id, username, public_key)
                    VALUES ('local_user', 'Local User', 'default_public_key')
                """)
                
                # Check if general channel exists
                cursor = conn.execute("SELECT id FROM channels WHERE name = 'general'")
                if not cursor.fetchone():
                    logger.info("Creating default general channel")
                    
                    # Create the general channel
                    conn.execute("""
                        INSERT INTO channels (
                            id, name, channel_type, created_by, description, privacy_mode,
                            last_activity_at, lifecycle_ttl_days, lifecycle_preserved
                        )
                        VALUES (
                            'general', 'general', 'public', 'system', 'General discussion channel', 'open',
                            CURRENT_TIMESTAMP, ?, 1
                        )
                    """, (self.DEFAULT_CHANNEL_LIFECYCLE_DAYS,))
                    conn.execute(
                        "UPDATE channels SET lifecycle_preserved = 1, last_activity_at = COALESCE(last_activity_at, created_at) WHERE id = 'general'",
                    )
                    
                    # Add local user to the general channel
                    conn.execute("""
                        INSERT OR IGNORE INTO channel_members (channel_id, user_id, role)
                        VALUES ('general', 'local_user', 'admin')
                    """)
                    
                    conn.commit()
                    logger.info("Default general channel created successfully")
                else:
                    logger.debug("General channel already exists")
                    # Ensure general channel is always open
                    conn.execute("""
                        UPDATE channels SET privacy_mode = 'open' WHERE id = 'general'
                    """)
                    
                    # Ensure local user is in general channel even if channel exists
                    conn.execute("""
                        INSERT OR IGNORE INTO channel_members (channel_id, user_id, role)
                        VALUES ('general', 'local_user', 'admin')
                    """)
                    conn.commit()
                    
        except Exception as e:
            logger.error(f"Failed to ensure default channels: {e}", exc_info=True)
            raise

    # ------------------------------------------------------------------
    #  Channel governance helpers
    # ------------------------------------------------------------------

    def _default_channel_governance_policy(self, user_id: str) -> Dict[str, Any]:
        return {
            'user_id': user_id,
            'enabled': False,
            'block_public_channels': False,
            'restrict_to_allowed_channels': False,
            'allowed_channel_ids': [],
            'updated_at': None,
            'updated_by': None,
        }

    def _normalize_allowed_channel_ids(self, raw: Any) -> List[str]:
        values: list[Any]
        if raw is None:
            values = []
        elif isinstance(raw, str):
            parsed: Any = None
            txt = raw.strip()
            if txt:
                try:
                    parsed = json.loads(txt)
                except Exception:
                    parsed = [part.strip() for part in txt.split(',')]
            values = parsed if isinstance(parsed, list) else []
        elif isinstance(raw, (list, tuple, set)):
            values = list(raw)
        else:
            values = []

        out: List[str] = []
        seen = set()
        for item in values:
            cid = str(item or '').strip()
            if not cid:
                continue
            if len(cid) > self.GOVERNANCE_MAX_CHANNEL_ID_LENGTH:
                continue
            if cid in seen:
                continue
            seen.add(cid)
            out.append(cid)
            if len(out) >= self.GOVERNANCE_MAX_ALLOWED_CHANNELS:
                break
        return out

    def _is_public_channel(self, channel_type: Any, privacy_mode: Any) -> bool:
        ctype = str(channel_type or '').strip().lower()
        mode = self._normalize_privacy_mode(privacy_mode, default='open')
        return mode == 'open' and ctype in {'public', 'general'}

    def _is_channel_allowed_by_policy(
        self,
        policy: Dict[str, Any],
        channel_id: str,
        channel_type: Any,
        privacy_mode: Any,
    ) -> Tuple[bool, str]:
        if not bool(policy.get('enabled')):
            return True, 'policy_disabled'
        if bool(policy.get('block_public_channels')) and self._is_public_channel(channel_type, privacy_mode):
            return False, 'governance_public_channels_blocked'
        if bool(policy.get('restrict_to_allowed_channels')):
            allowed = set(self._normalize_allowed_channel_ids(policy.get('allowed_channel_ids')))
            if channel_id not in allowed:
                return False, 'governance_channel_not_allowlisted'
        return True, 'ok'

    def _load_user_channel_governance(self, conn: Any, user_id: str) -> Dict[str, Any]:
        policy = self._default_channel_governance_policy(user_id)
        if not user_id:
            return policy
        row = conn.execute(
            """
            SELECT enabled, block_public_channels, restrict_to_allowed_channels,
                   allowed_channel_ids, updated_at, updated_by
            FROM user_channel_governance
            WHERE user_id = ?
            """,
            (user_id,),
        ).fetchone()
        if not row:
            return policy

        def _get(name: str, default: Any = None) -> Any:
            try:
                return row[name]
            except Exception:
                return default

        def _as_bool(value: Any) -> bool:
            if isinstance(value, bool):
                return value
            if value is None:
                return False
            if isinstance(value, (int, float)):
                return value != 0
            return str(value).strip().lower() in {'1', 'true', 'yes', 'on'}

        policy['enabled'] = _as_bool(_get('enabled', 0))
        policy['block_public_channels'] = _as_bool(_get('block_public_channels', 0))
        policy['restrict_to_allowed_channels'] = _as_bool(_get('restrict_to_allowed_channels', 0))
        policy['allowed_channel_ids'] = self._normalize_allowed_channel_ids(_get('allowed_channel_ids', '[]'))
        policy['updated_at'] = _get('updated_at')
        policy['updated_by'] = _get('updated_by')
        return policy

    def get_user_channel_governance(self, user_id: str) -> Dict[str, Any]:
        """Return persisted channel governance policy for a user."""
        if not user_id:
            return self._default_channel_governance_policy('')
        try:
            with self.db.get_connection() as conn:
                return self._load_user_channel_governance(conn, user_id)
        except Exception as e:
            logger.error(f"Failed to load channel governance for {user_id}: {e}", exc_info=True)
            return self._default_channel_governance_policy(user_id)

    def set_user_channel_governance(
        self,
        user_id: str,
        *,
        enabled: bool,
        block_public_channels: bool,
        restrict_to_allowed_channels: bool,
        allowed_channel_ids: Optional[List[str]] = None,
        updated_by: Optional[str] = None,
    ) -> bool:
        """Create or update a user's channel governance policy."""
        if not user_id:
            return False
        normalized_ids = self._normalize_allowed_channel_ids(allowed_channel_ids)
        try:
            with self.db.get_connection() as conn:
                exists = conn.execute(
                    "SELECT 1 FROM users WHERE id = ?",
                    (user_id,),
                ).fetchone()
                if not exists:
                    return False

                conn.execute(
                    """
                    INSERT INTO user_channel_governance (
                        user_id, enabled, block_public_channels,
                        restrict_to_allowed_channels, allowed_channel_ids,
                        updated_at, updated_by
                    ) VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP, ?)
                    ON CONFLICT(user_id) DO UPDATE SET
                        enabled = excluded.enabled,
                        block_public_channels = excluded.block_public_channels,
                        restrict_to_allowed_channels = excluded.restrict_to_allowed_channels,
                        allowed_channel_ids = excluded.allowed_channel_ids,
                        updated_at = CURRENT_TIMESTAMP,
                        updated_by = excluded.updated_by
                    """,
                    (
                        user_id,
                        1 if enabled else 0,
                        1 if block_public_channels else 0,
                        1 if restrict_to_allowed_channels else 0,
                        json.dumps(normalized_ids),
                        updated_by,
                    ),
                )
                conn.commit()
                return True
        except Exception as e:
            logger.error(f"Failed to update channel governance for {user_id}: {e}", exc_info=True)
            return False

    def get_channel_access_decision(
        self,
        channel_id: str,
        user_id: str,
        *,
        require_membership: bool = True,
    ) -> Dict[str, Any]:
        """Resolve whether a user can access a channel under membership + governance."""
        decision = {
            'allowed': False,
            'reason': 'invalid_request',
            'role': None,
            'channel_exists': False,
            'policy': self._default_channel_governance_policy(user_id),
        }
        if not channel_id or not user_id:
            return decision
        try:
            with self.db.get_connection() as conn:
                row = conn.execute(
                    """
                    SELECT c.id, c.channel_type, COALESCE(c.privacy_mode, 'open') AS privacy_mode,
                           cm.role AS member_role
                    FROM channels c
                    LEFT JOIN channel_members cm
                      ON cm.channel_id = c.id AND cm.user_id = ?
                    WHERE c.id = ?
                    """,
                    (user_id, channel_id),
                ).fetchone()
                if not row:
                    decision['reason'] = 'channel_not_found'
                    return decision

                decision['channel_exists'] = True
                role = row['member_role'] if 'member_role' in row.keys() else None
                decision['role'] = role
                if require_membership and not role:
                    decision['reason'] = 'not_member'
                    return decision

                policy = self._load_user_channel_governance(conn, user_id)
                decision['policy'] = policy
                allowed, reason = self._is_channel_allowed_by_policy(
                    policy=policy,
                    channel_id=channel_id,
                    channel_type=row['channel_type'],
                    privacy_mode=row['privacy_mode'],
                )
                decision['allowed'] = allowed
                decision['reason'] = reason
                return decision
        except Exception as e:
            logger.error(
                f"Failed to evaluate channel access for user={user_id} channel={channel_id}: {e}",
                exc_info=True,
            )
            decision['reason'] = 'internal_error'
            return decision

    def enforce_user_channel_governance(self, user_id: str) -> Dict[str, Any]:
        """Prune disallowed memberships based on current governance policy."""
        result = {
            'user_id': user_id,
            'enabled': False,
            'checked_count': 0,
            'removed_count': 0,
            'removed_channel_ids': [],
        }
        if not user_id:
            return result
        try:
            with self.db.get_connection() as conn:
                policy = self._load_user_channel_governance(conn, user_id)
                result['enabled'] = bool(policy.get('enabled'))
                rows = conn.execute(
                    """
                    SELECT cm.channel_id, c.channel_type, COALESCE(c.privacy_mode, 'open') AS privacy_mode
                    FROM channel_members cm
                    JOIN channels c ON c.id = cm.channel_id
                    WHERE cm.user_id = ?
                    """,
                    (user_id,),
                ).fetchall()
                result['checked_count'] = len(rows)
                if not policy.get('enabled'):
                    return result

                removed_ids: List[str] = []
                for row in rows:
                    channel_id = row['channel_id']
                    allowed, _ = self._is_channel_allowed_by_policy(
                        policy=policy,
                        channel_id=channel_id,
                        channel_type=row['channel_type'],
                        privacy_mode=row['privacy_mode'],
                    )
                    if allowed:
                        continue
                    conn.execute(
                        "DELETE FROM channel_members WHERE channel_id = ? AND user_id = ?",
                        (channel_id, user_id),
                    )
                    removed_ids.append(channel_id)
                conn.commit()

                result['removed_count'] = len(removed_ids)
                result['removed_channel_ids'] = removed_ids
                return result
        except Exception as e:
            logger.error(f"Failed to enforce channel governance for {user_id}: {e}", exc_info=True)
            return result

    def list_channels_for_governance(self, user_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """List channels for admin governance UI with optional user-specific flags."""
        try:
            with self.db.get_connection() as conn:
                policy = self._load_user_channel_governance(conn, user_id) if user_id else None
                if user_id:
                    rows = conn.execute(
                        """
                        SELECT c.id, c.name, c.channel_type, COALESCE(c.privacy_mode, 'open') AS privacy_mode,
                               COUNT(DISTINCT cm.user_id) AS member_count,
                               MAX(CASE WHEN cmu.user_id IS NOT NULL THEN 1 ELSE 0 END) AS is_member
                        FROM channels c
                        LEFT JOIN channel_members cm ON cm.channel_id = c.id
                        LEFT JOIN channel_members cmu
                          ON cmu.channel_id = c.id AND cmu.user_id = ?
                        GROUP BY c.id, c.name, c.channel_type, COALESCE(c.privacy_mode, 'open')
                        ORDER BY CASE WHEN c.id = 'general' THEN 0 ELSE 1 END, LOWER(c.name) ASC
                        """,
                        (user_id,),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        """
                        SELECT c.id, c.name, c.channel_type, COALESCE(c.privacy_mode, 'open') AS privacy_mode,
                               COUNT(DISTINCT cm.user_id) AS member_count
                        FROM channels c
                        LEFT JOIN channel_members cm ON cm.channel_id = c.id
                        GROUP BY c.id, c.name, c.channel_type, COALESCE(c.privacy_mode, 'open')
                        ORDER BY CASE WHEN c.id = 'general' THEN 0 ELSE 1 END, LOWER(c.name) ASC
                        """
                    ).fetchall()

                payload: List[Dict[str, Any]] = []
                for row in rows:
                    channel_id = row['id']
                    channel_type = row['channel_type']
                    privacy_mode = row['privacy_mode']
                    is_public = self._is_public_channel(channel_type, privacy_mode)
                    entry: Dict[str, Any] = {
                        'id': channel_id,
                        'name': row['name'],
                        'channel_type': channel_type,
                        'privacy_mode': privacy_mode,
                        'member_count': int(row['member_count'] or 0),
                        'is_public_open': bool(is_public),
                    }
                    if user_id:
                        entry['is_member'] = bool(row['is_member'])
                        if policy:
                            allowed, reason = self._is_channel_allowed_by_policy(
                                policy=policy,
                                channel_id=channel_id,
                                channel_type=channel_type,
                                privacy_mode=privacy_mode,
                            )
                            entry['governance_allowed'] = bool(allowed)
                            entry['governance_reason'] = reason
                    payload.append(entry)
                return payload
        except Exception as e:
            logger.error(f"Failed to list channels for governance: {e}", exc_info=True)
            return []

    # ------------------------------------------------------------------
    #  Private-channel E2E scaffolding (phase 1)
    # ------------------------------------------------------------------

    def get_channel_crypto_mode(self, channel_id: str) -> str:
        """Return a channel crypto mode (legacy_plaintext by default)."""
        if not channel_id:
            return self.CRYPTO_MODE_LEGACY
        try:
            with self.db.get_connection() as conn:
                row = conn.execute(
                    "SELECT COALESCE(crypto_mode, ?) AS crypto_mode FROM channels WHERE id = ?",
                    (self.CRYPTO_MODE_LEGACY, channel_id),
                ).fetchone()
                if not row:
                    return self.CRYPTO_MODE_LEGACY
                mode = str(row['crypto_mode'] or self.CRYPTO_MODE_LEGACY).strip().lower()
                return mode if mode in self.ALLOWED_CRYPTO_MODES else self.CRYPTO_MODE_LEGACY
        except Exception as e:
            logger.debug(f"Failed to get crypto mode for channel {channel_id}: {e}")
            return self.CRYPTO_MODE_LEGACY

    def set_channel_crypto_mode(self, channel_id: str, crypto_mode: str) -> bool:
        """Set channel crypto mode for staged rollout."""
        if not channel_id:
            return False
        mode = str(crypto_mode or '').strip().lower() or self.CRYPTO_MODE_LEGACY
        if mode not in self.ALLOWED_CRYPTO_MODES:
            return False
        try:
            with self.db.get_connection() as conn:
                cur = conn.execute(
                    "UPDATE channels SET crypto_mode = ? WHERE id = ?",
                    (mode, channel_id),
                )
                conn.commit()
                return cast(int, cur.rowcount) > 0
        except Exception as e:
            logger.error(f"Failed to set crypto mode for {channel_id}: {e}", exc_info=True)
            return False

    def encode_channel_key_material(self, key_material: bytes) -> str:
        """Encode local channel key bytes for DB storage."""
        return encode_channel_key_material_value(key_material)

    def decode_channel_key_material(self, key_material_enc: Optional[str]) -> Optional[bytes]:
        """Decode locally stored channel key bytes when available."""
        return decode_channel_key_material_value(key_material_enc or '')

    def _cache_channel_key(self, channel_id: str, key_id: str, key_material: Optional[bytes]) -> None:
        """Update in-memory key cache for fast decrypt lookups."""
        if not channel_id or not key_id:
            return
        cache_key = (channel_id, key_id)
        with self._channel_key_lock:
            if key_material:
                self._channel_key_cache[cache_key] = key_material
            else:
                self._channel_key_cache.pop(cache_key, None)

    def _cached_channel_key(self, channel_id: str, key_id: str) -> Optional[bytes]:
        """Return cached channel key material if present."""
        with self._channel_key_lock:
            return self._channel_key_cache.get((channel_id, key_id))

    def upsert_channel_key(
        self,
        channel_id: str,
        key_id: str,
        key_material_enc: str,
        created_by_peer: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Store/update wrapped key material for a private channel."""
        if not channel_id or not key_id or not key_material_enc:
            return False
        metadata_json = None
        if metadata is not None:
            try:
                metadata_json = json.dumps(metadata)
            except Exception:
                metadata_json = None
        try:
            with self.db.get_connection() as conn:
                conn.execute(
                    """
                    INSERT INTO channel_keys (
                        channel_id, key_id, key_material_enc, created_by_peer, metadata
                    ) VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(channel_id, key_id) DO UPDATE SET
                        key_material_enc = excluded.key_material_enc,
                        created_by_peer = COALESCE(excluded.created_by_peer, channel_keys.created_by_peer),
                        metadata = COALESCE(excluded.metadata, channel_keys.metadata),
                        revoked_at = NULL
                    """,
                    (channel_id, key_id, key_material_enc, created_by_peer, metadata_json),
                )
                conn.commit()
                key_bytes = self.decode_channel_key_material(key_material_enc)
                self._cache_channel_key(channel_id, key_id, key_bytes)
                return True
        except Exception as e:
            logger.error(f"Failed to upsert channel key for {channel_id}/{key_id}: {e}", exc_info=True)
            return False

    def list_channel_keys(self, channel_id: str, include_revoked: bool = False) -> List[Dict[str, Any]]:
        """Return channel key rows for diagnostics/rotation workflows."""
        if not channel_id:
            return []
        try:
            with self.db.get_connection() as conn:
                if include_revoked:
                    rows = conn.execute(
                        """
                        SELECT channel_id, key_id, key_material_enc, created_by_peer, metadata,
                               created_at, revoked_at
                        FROM channel_keys
                        WHERE channel_id = ?
                        ORDER BY created_at DESC
                        """,
                        (channel_id,),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        """
                        SELECT channel_id, key_id, key_material_enc, created_by_peer, metadata,
                               created_at, revoked_at
                        FROM channel_keys
                        WHERE channel_id = ? AND revoked_at IS NULL
                        ORDER BY created_at DESC
                        """,
                        (channel_id,),
                    ).fetchall()
                out: List[Dict[str, Any]] = []
                for row in rows:
                    metadata_value = None
                    if row['metadata']:
                        try:
                            metadata_value = json.loads(row['metadata'])
                        except Exception:
                            metadata_value = None
                    out.append({
                        'channel_id': row['channel_id'],
                        'key_id': row['key_id'],
                        'key_material_enc': row['key_material_enc'],
                        'created_by_peer': row['created_by_peer'],
                        'metadata': metadata_value,
                        'created_at': row['created_at'],
                        'revoked_at': row['revoked_at'],
                    })
                return out
        except Exception as e:
            logger.error(f"Failed to list channel keys for {channel_id}: {e}", exc_info=True)
            return []

    def get_active_channel_key(self, channel_id: str) -> Optional[Dict[str, Any]]:
        """Return the latest non-revoked key for a channel."""
        if not channel_id:
            return None
        try:
            with self.db.get_connection() as conn:
                row = conn.execute(
                    """
                    SELECT channel_id, key_id, key_material_enc, created_by_peer, metadata,
                           created_at, revoked_at
                    FROM channel_keys
                    WHERE channel_id = ? AND revoked_at IS NULL
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    (channel_id,),
                ).fetchone()
                if not row:
                    return None
                metadata_value = None
                if row['metadata']:
                    try:
                        metadata_value = json.loads(row['metadata'])
                    except Exception:
                        metadata_value = None
                return {
                    'channel_id': row['channel_id'],
                    'key_id': row['key_id'],
                    'key_material_enc': row['key_material_enc'],
                    'created_by_peer': row['created_by_peer'],
                    'metadata': metadata_value,
                    'created_at': row['created_at'],
                    'revoked_at': row['revoked_at'],
                }
        except Exception as e:
            logger.error(f"Failed to get active channel key for {channel_id}: {e}", exc_info=True)
            return None

    def get_channel_key(
        self,
        channel_id: str,
        key_id: str,
        include_revoked: bool = True,
    ) -> Optional[Dict[str, Any]]:
        """Return one channel key row by channel/key ID."""
        if not channel_id or not key_id:
            return None
        try:
            with self.db.get_connection() as conn:
                if include_revoked:
                    row = conn.execute(
                        """
                        SELECT channel_id, key_id, key_material_enc, created_by_peer, metadata,
                               created_at, revoked_at
                        FROM channel_keys
                        WHERE channel_id = ? AND key_id = ?
                        LIMIT 1
                        """,
                        (channel_id, key_id),
                    ).fetchone()
                else:
                    row = conn.execute(
                        """
                        SELECT channel_id, key_id, key_material_enc, created_by_peer, metadata,
                               created_at, revoked_at
                        FROM channel_keys
                        WHERE channel_id = ? AND key_id = ? AND revoked_at IS NULL
                        LIMIT 1
                        """,
                        (channel_id, key_id),
                    ).fetchone()
            if not row:
                return None
            metadata_value = None
            if row['metadata']:
                try:
                    metadata_value = json.loads(row['metadata'])
                except Exception:
                    metadata_value = None
            return {
                'channel_id': row['channel_id'],
                'key_id': row['key_id'],
                'key_material_enc': row['key_material_enc'],
                'created_by_peer': row['created_by_peer'],
                'metadata': metadata_value,
                'created_at': row['created_at'],
                'revoked_at': row['revoked_at'],
            }
        except Exception as e:
            logger.error(f"Failed to get channel key for {channel_id}/{key_id}: {e}", exc_info=True)
            return None

    def get_channel_key_bytes(self, channel_id: str, key_id: str) -> Optional[bytes]:
        """Return decoded key material for one key when locally available."""
        cached = self._cached_channel_key(channel_id, key_id)
        if cached:
            return cached
        key_row = self.get_channel_key(channel_id, key_id, include_revoked=True)
        if not key_row:
            return None
        key_bytes = self.decode_channel_key_material(key_row.get('key_material_enc'))
        self._cache_channel_key(channel_id, key_id, key_bytes)
        return key_bytes

    def get_active_channel_key_bytes(self, channel_id: str) -> Optional[Tuple[str, bytes]]:
        """Return (key_id, key_bytes) for the active key when locally available."""
        key_row = self.get_active_channel_key(channel_id)
        if not key_row:
            return None
        key_id = key_row.get('key_id')
        if not key_id:
            return None
        key_bytes = self.get_channel_key_bytes(channel_id, key_id)
        if not key_bytes:
            return None
        return (key_id, key_bytes)

    def revoke_channel_key(self, channel_id: str, key_id: str) -> bool:
        """Mark a channel key as revoked (kept for historical decrypt)."""
        if not channel_id or not key_id:
            return False
        try:
            with self.db.get_connection() as conn:
                cur = conn.execute(
                    """
                    UPDATE channel_keys
                    SET revoked_at = CURRENT_TIMESTAMP
                    WHERE channel_id = ? AND key_id = ? AND revoked_at IS NULL
                    """,
                    (channel_id, key_id),
                )
                conn.commit()
                if cast(int, cur.rowcount) > 0:
                    self._cache_channel_key(channel_id, key_id, None)
                return cast(int, cur.rowcount) > 0
        except Exception as e:
            logger.error(f"Failed to revoke channel key {channel_id}/{key_id}: {e}", exc_info=True)
            return False

    def upsert_channel_member_key_state(
        self,
        channel_id: str,
        key_id: str,
        peer_id: str,
        delivery_state: str = 'pending',
        last_error: Optional[str] = None,
        delivered: bool = False,
        acked: bool = False,
    ) -> bool:
        """Persist per-peer key-delivery state for operational visibility."""
        if not channel_id or not key_id or not peer_id:
            return False
        state = str(delivery_state or 'pending').strip().lower() or 'pending'
        delivered_at = "CURRENT_TIMESTAMP" if delivered else "NULL"
        acked_at = "CURRENT_TIMESTAMP" if acked else "NULL"
        try:
            with self.db.get_connection() as conn:
                conn.execute(
                    f"""
                    INSERT INTO channel_member_keys (
                        channel_id, key_id, peer_id, delivery_state, last_error,
                        delivered_at, acked_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, {delivered_at}, {acked_at}, CURRENT_TIMESTAMP)
                    ON CONFLICT(channel_id, key_id, peer_id) DO UPDATE SET
                        delivery_state = excluded.delivery_state,
                        last_error = excluded.last_error,
                        delivered_at = CASE
                            WHEN excluded.delivered_at IS NOT NULL THEN excluded.delivered_at
                            ELSE channel_member_keys.delivered_at
                        END,
                        acked_at = CASE
                            WHEN excluded.acked_at IS NOT NULL THEN excluded.acked_at
                            ELSE channel_member_keys.acked_at
                        END,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    (channel_id, key_id, peer_id, state, last_error),
                )
                conn.commit()
                return True
        except Exception as e:
            logger.error(
                f"Failed to upsert channel member key state {channel_id}/{key_id}/{peer_id}: {e}",
                exc_info=True,
            )
            return False

    def get_channel_member_key_states(self, channel_id: str, key_id: str) -> List[Dict[str, Any]]:
        """Return per-peer key-delivery state for one channel key."""
        if not channel_id or not key_id:
            return []
        try:
            with self.db.get_connection() as conn:
                rows = conn.execute(
                    """
                    SELECT channel_id, key_id, peer_id, delivery_state, last_error,
                           delivered_at, acked_at, updated_at
                    FROM channel_member_keys
                    WHERE channel_id = ? AND key_id = ?
                    ORDER BY updated_at DESC
                    """,
                    (channel_id, key_id),
                ).fetchall()
                return [
                    {
                        'channel_id': row['channel_id'],
                        'key_id': row['key_id'],
                        'peer_id': row['peer_id'],
                        'delivery_state': row['delivery_state'],
                        'last_error': row['last_error'],
                        'delivered_at': row['delivered_at'],
                        'acked_at': row['acked_at'],
                        'updated_at': row['updated_at'],
                    }
                    for row in rows
                ]
        except Exception as e:
            logger.error(
                f"Failed to get channel member key states for {channel_id}/{key_id}: {e}",
                exc_info=True,
            )
            return []

    def get_pending_decrypt_messages(
        self,
        channel_id: str,
        key_id: Optional[str] = None,
        limit: int = 500,
    ) -> List[Dict[str, Any]]:
        """Return encrypted messages waiting for key-driven decrypt backfill."""
        if not channel_id:
            return []
        try:
            with self.db.get_connection() as conn:
                if key_id:
                    rows = conn.execute(
                        """
                        SELECT id, channel_id, user_id, origin_peer, parent_message_id,
                               encrypted_content, nonce, key_id, created_at
                        FROM channel_messages
                        WHERE channel_id = ?
                          AND crypto_state = 'pending_decrypt'
                          AND key_id = ?
                          AND encrypted_content IS NOT NULL
                          AND nonce IS NOT NULL
                        ORDER BY created_at ASC
                        LIMIT ?
                        """,
                        (channel_id, key_id, limit),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        """
                        SELECT id, channel_id, user_id, origin_peer, parent_message_id,
                               encrypted_content, nonce, key_id, created_at
                        FROM channel_messages
                        WHERE channel_id = ?
                          AND crypto_state = 'pending_decrypt'
                          AND encrypted_content IS NOT NULL
                          AND nonce IS NOT NULL
                        ORDER BY created_at ASC
                        LIMIT ?
                        """,
                        (channel_id, limit),
                    ).fetchall()
            return [
                {
                    'id': row['id'],
                    'channel_id': row['channel_id'],
                    'user_id': row['user_id'],
                    'origin_peer': row['origin_peer'],
                    'parent_message_id': row['parent_message_id'],
                    'encrypted_content': row['encrypted_content'],
                    'nonce': row['nonce'],
                    'key_id': row['key_id'],
                    'created_at': row['created_at'],
                }
                for row in rows
            ]
        except Exception as e:
            logger.error(
                f"Failed to load pending decrypt messages for channel {channel_id}: {e}",
                exc_info=True,
            )
            return []

    def get_retryable_channel_member_key_states(
        self,
        peer_id: str,
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        """Return key-delivery rows that should be retried for a connected peer."""
        if not peer_id:
            return []
        try:
            with self.db.get_connection() as conn:
                rows = conn.execute(
                    """
                    SELECT cmk.channel_id, cmk.key_id, cmk.peer_id, cmk.delivery_state,
                           cmk.last_error, cmk.updated_at,
                           ck.key_material_enc, ck.metadata
                    FROM channel_member_keys cmk
                    JOIN channel_keys ck
                      ON ck.channel_id = cmk.channel_id
                     AND ck.key_id = cmk.key_id
                    WHERE cmk.peer_id = ?
                      AND cmk.acked_at IS NULL
                      AND cmk.delivery_state IN ('pending', 'failed', 'delivered')
                      AND ck.revoked_at IS NULL
                    ORDER BY cmk.updated_at ASC
                    LIMIT ?
                    """,
                    (peer_id, int(limit)),
                ).fetchall()
            out: List[Dict[str, Any]] = []
            for row in rows:
                metadata: Dict[str, Any] = {}
                if row['metadata']:
                    try:
                        metadata = json.loads(row['metadata'])
                    except Exception:
                        metadata = {}
                out.append({
                    'channel_id': row['channel_id'],
                    'key_id': row['key_id'],
                    'peer_id': row['peer_id'],
                    'delivery_state': row['delivery_state'],
                    'last_error': row['last_error'],
                    'updated_at': row['updated_at'],
                    'key_material_enc': row['key_material_enc'],
                    'metadata': metadata,
                })
            return out
        except Exception as e:
            logger.error(
                f"Failed to get retryable key states for peer {peer_id}: {e}",
                exc_info=True,
            )
            return []

    def queue_member_sync_delivery(
        self,
        sync_id: str,
        channel_id: str,
        target_user_id: str,
        action: str,
        role: str,
        target_peer_id: str,
        payload: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Persist a member_sync delivery record for ack/retry handling."""
        if not sync_id or not channel_id or not target_user_id or not action or not target_peer_id:
            return False
        payload_json = None
        if payload is not None:
            try:
                payload_json = json.dumps(payload)
            except Exception:
                payload_json = None
        try:
            with self.db.get_connection() as conn:
                conn.execute(
                    """
                    INSERT INTO channel_member_sync_deliveries (
                        sync_id, channel_id, target_user_id, action, role, target_peer_id,
                        payload_json, delivery_state, last_error, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', NULL, CURRENT_TIMESTAMP)
                    ON CONFLICT(sync_id) DO UPDATE SET
                        channel_id = excluded.channel_id,
                        target_user_id = excluded.target_user_id,
                        action = excluded.action,
                        role = excluded.role,
                        target_peer_id = excluded.target_peer_id,
                        payload_json = excluded.payload_json,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    (
                        sync_id,
                        channel_id,
                        target_user_id,
                        action,
                        role or 'member',
                        target_peer_id,
                        payload_json,
                    ),
                )
                conn.commit()
            return True
        except Exception as e:
            logger.error(
                f"Failed to queue member sync delivery {sync_id} ({channel_id}->{target_peer_id}): {e}",
                exc_info=True,
            )
            return False

    def mark_member_sync_delivery_attempt(
        self,
        sync_id: str,
        sent: bool,
        error: Optional[str] = None,
    ) -> bool:
        """Record one member_sync send attempt."""
        if not sync_id:
            return False
        state = 'sent' if sent else 'failed'
        try:
            with self.db.get_connection() as conn:
                cur = conn.execute(
                    """
                    UPDATE channel_member_sync_deliveries
                    SET attempt_count = COALESCE(attempt_count, 0) + 1,
                        last_attempt_at = CURRENT_TIMESTAMP,
                        delivery_state = ?,
                        last_error = ?,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE sync_id = ?
                    """,
                    (state, None if sent else error, sync_id),
                )
                conn.commit()
                return cast(int, cur.rowcount) > 0
        except Exception as e:
            logger.error(f"Failed to mark member sync attempt {sync_id}: {e}", exc_info=True)
            return False

    def mark_member_sync_delivery_acked(
        self,
        sync_id: str,
        status: str = 'ok',
        error: Optional[str] = None,
    ) -> bool:
        """Mark member_sync delivery as acknowledged (or failed with explicit reason)."""
        if not sync_id:
            return False
        ok = str(status or '').strip().lower() == 'ok'
        new_state = 'acked' if ok else 'failed'
        try:
            with self.db.get_connection() as conn:
                cur = conn.execute(
                    """
                    UPDATE channel_member_sync_deliveries
                    SET delivery_state = ?,
                        acked_at = CASE WHEN ? = 'acked' THEN CURRENT_TIMESTAMP ELSE acked_at END,
                        last_error = CASE WHEN ? = 'acked' THEN NULL ELSE ? END,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE sync_id = ?
                    """,
                    (new_state, new_state, new_state, error, sync_id),
                )
                conn.commit()
                return cast(int, cur.rowcount) > 0
        except Exception as e:
            logger.error(f"Failed to mark member sync ack {sync_id}: {e}", exc_info=True)
            return False

    def get_retryable_member_sync_deliveries(
        self,
        peer_id: str,
        limit: int = 200,
        min_retry_seconds: int = 10,
        max_attempts: int = 8,
    ) -> List[Dict[str, Any]]:
        """Return pending/unacked member_sync items that are eligible for resend."""
        if not peer_id:
            return []
        retry_window = f"-{max(0, int(min_retry_seconds))} seconds"
        try:
            with self.db.get_connection() as conn:
                rows = conn.execute(
                    """
                    SELECT sync_id, channel_id, target_user_id, action, role, target_peer_id,
                           payload_json, delivery_state, last_error, attempt_count,
                           last_attempt_at, acked_at, created_at, updated_at
                    FROM channel_member_sync_deliveries
                    WHERE target_peer_id = ?
                      AND acked_at IS NULL
                      AND COALESCE(attempt_count, 0) < ?
                      AND delivery_state IN ('pending', 'sent', 'failed')
                      AND (
                           last_attempt_at IS NULL
                           OR last_attempt_at <= datetime('now', ?)
                      )
                    ORDER BY created_at ASC
                    LIMIT ?
                    """,
                    (peer_id, max(1, int(max_attempts)), retry_window, int(limit)),
                ).fetchall()
            out: List[Dict[str, Any]] = []
            for row in rows:
                payload: Dict[str, Any] = {}
                payload_json = row['payload_json']
                if payload_json:
                    try:
                        payload = json.loads(payload_json)
                    except Exception:
                        payload = {}
                out.append({
                    'sync_id': row['sync_id'],
                    'channel_id': row['channel_id'],
                    'target_user_id': row['target_user_id'],
                    'action': row['action'],
                    'role': row['role'],
                    'target_peer_id': row['target_peer_id'],
                    'payload': payload,
                    'delivery_state': row['delivery_state'],
                    'last_error': row['last_error'],
                    'attempt_count': row['attempt_count'],
                    'last_attempt_at': row['last_attempt_at'],
                    'created_at': row['created_at'],
                    'updated_at': row['updated_at'],
                })
            return out
        except Exception as e:
            logger.error(f"Failed to get retryable member sync deliveries for {peer_id}: {e}", exc_info=True)
            return []

    def mark_stale_pending_decrypt(
        self,
        max_age_hours: int = 24,
        limit: int = 1000,
    ) -> int:
        """Mark old pending_decrypt messages as decrypt_failed to avoid indefinite limbo."""
        max_age = max(1, int(max_age_hours))
        max_rows = max(1, int(limit))
        cutoff_expr = f"-{max_age} hours"
        try:
            with self.db.get_connection() as conn:
                ids = conn.execute(
                    """
                    SELECT id
                    FROM channel_messages
                    WHERE crypto_state = 'pending_decrypt'
                      AND created_at <= datetime('now', ?)
                    ORDER BY created_at ASC
                    LIMIT ?
                    """,
                    (cutoff_expr, max_rows),
                ).fetchall()
                if not ids:
                    return 0
                placeholders = ",".join("?" for _ in ids)
                cur = conn.execute(
                    f"""
                    UPDATE channel_messages
                    SET crypto_state = 'decrypt_failed',
                        edited_at = CURRENT_TIMESTAMP
                    WHERE id IN ({placeholders})
                    """,
                    [row['id'] if hasattr(row, 'keys') and 'id' in row.keys() else row[0] for row in ids],
                )
                conn.commit()
                return cast(int, cur.rowcount)
        except Exception as e:
            logger.error(f"Failed to mark stale pending_decrypt messages: {e}", exc_info=True)
            return 0

    def update_message_decrypt(self, message_id: str, content: str, new_state: str) -> bool:
        """Update decrypted content/state for one channel message."""
        if not message_id:
            return False
        state = str(new_state or '').strip().lower() or 'decrypt_failed'
        if state not in {'decrypted', 'decrypt_failed', 'pending_decrypt', 'encrypted', 'plaintext'}:
            state = 'decrypt_failed'
        try:
            with self.db.get_connection() as conn:
                cur = conn.execute(
                    """
                    UPDATE channel_messages
                    SET content = ?, crypto_state = ?, edited_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (content or '', state, message_id),
                )
                conn.commit()
                return cast(int, cur.rowcount) > 0
        except Exception as e:
            logger.error(f"Failed to update decrypt state for message {message_id}: {e}", exc_info=True)
            return False

    # ------------------------------------------------------------------
    #  Peer device profile helpers
    # ------------------------------------------------------------------

    def store_peer_device_profile(self, peer_id: str,
                                  display_name: Optional[str] = None,
                                  description: Optional[str] = None,
                                  avatar_b64: Optional[str] = None,
                                  avatar_mime: Optional[str] = None) -> bool:
        """Upsert a device profile received from a remote peer."""
        try:
            with self.db.get_connection() as conn:
                conn.execute("""
                    INSERT INTO peer_device_profiles
                        (peer_id, display_name, description,
                         avatar_b64, avatar_mime, updated_at)
                    VALUES (?, ?, ?, ?, ?, datetime('now'))
                    ON CONFLICT(peer_id) DO UPDATE SET
                        display_name = COALESCE(excluded.display_name, display_name),
                        description  = COALESCE(excluded.description, description),
                        avatar_b64   = COALESCE(excluded.avatar_b64, avatar_b64),
                        avatar_mime  = COALESCE(excluded.avatar_mime, avatar_mime),
                        updated_at   = datetime('now')
                """, (peer_id, display_name, description,
                      avatar_b64, avatar_mime))
                conn.commit()
            logger.debug(f"Stored device profile for peer {peer_id}: "
                         f"name={display_name}")
            return True
        except Exception as e:
            logger.error(f"Failed to store device profile for {peer_id}: {e}")
            return False

    def get_peer_device_profile(self, peer_id: str) -> Optional[Dict[str, Any]]:
        """Return the stored device profile for a peer, or None."""
        try:
            with self.db.get_connection() as conn:
                row = conn.execute(
                    "SELECT display_name, description, avatar_b64, avatar_mime "
                    "FROM peer_device_profiles WHERE peer_id = ?",
                    (peer_id,)
                ).fetchone()
                if row:
                    return {
                        'peer_id': peer_id,
                        'display_name': row[0] or peer_id[:12],
                        'description': row[1] or '',
                        'avatar_b64': row[2] or '',
                        'avatar_mime': row[3] or '',
                    }
        except Exception as e:
            logger.error(f"Failed to get device profile for {peer_id}: {e}")
        return None

    def get_all_peer_device_profiles(self) -> Dict[str, Dict[str, Any]]:
        """Return all stored peer device profiles keyed by peer_id."""
        profiles = {}
        try:
            with self.db.get_connection() as conn:
                rows = conn.execute(
                    "SELECT peer_id, display_name, description, "
                    "avatar_b64, avatar_mime FROM peer_device_profiles"
                ).fetchall()
                for r in rows:
                    profiles[r[0]] = {
                        'peer_id': r[0],
                        'display_name': r[1] or r[0][:12],
                        'description': r[2] or '',
                        'avatar_b64': r[3] or '',
                        'avatar_mime': r[4] or '',
                    }
        except Exception as e:
            logger.error(f"Failed to get all device profiles: {e}")
        return profiles

    def get_peer_device_profiles(self, peer_ids: List[str]) -> Dict[str, Dict[str, Any]]:
        """Return stored peer device profiles for the requested peer IDs only."""
        cleaned_ids = [
            str(peer_id or '').strip()
            for peer_id in (peer_ids or [])
            if str(peer_id or '').strip()
        ]
        if not cleaned_ids:
            return {}
        try:
            with self.db.get_connection() as conn:
                placeholders = ",".join("?" for _ in cleaned_ids)
                rows = conn.execute(
                    f"""
                    SELECT peer_id, display_name, description, avatar_b64, avatar_mime
                    FROM peer_device_profiles
                    WHERE peer_id IN ({placeholders})
                    """,
                    cleaned_ids,
                ).fetchall()
            profiles: Dict[str, Dict[str, Any]] = {}
            for row in rows or []:
                peer_id = str(row[0] or '').strip()
                if not peer_id:
                    continue
                profiles[peer_id] = {
                    'peer_id': peer_id,
                    'display_name': row[1] or peer_id[:12],
                    'description': row[2] or '',
                    'avatar_b64': row[3] or '',
                    'avatar_mime': row[4] or '',
                }
            return profiles
        except Exception as e:
            logger.error(f"Failed to get peer device profiles: {e}")
            return {}

    @staticmethod
    def _normalize_channel_name(name: str) -> str:
        """Strip leading '#' characters from a channel name.

        The '#' is a display prefix added by the UI — it should never
        be stored in the database.  This prevents double-hash rendering
        (e.g. '##general') when users or agents include '#' in the name.
        """
        return name.lstrip('#').strip() if name else name

    @classmethod
    def _normalize_privacy_mode(cls, mode: Any, default: str = 'open') -> str:
        """Normalize channel privacy mode to a known value."""
        candidate = str(mode or '').strip().lower()
        if candidate in cls.PRIVACY_ORDER:
            return candidate
        return default

    @classmethod
    def _is_privacy_downgrade(cls, old_mode: str, new_mode: str) -> bool:
        """Return True if new_mode is less restrictive than old_mode."""
        old_rank = cls.PRIVACY_ORDER.get(cls._normalize_privacy_mode(old_mode), 0)
        new_rank = cls.PRIVACY_ORDER.get(cls._normalize_privacy_mode(new_mode), 0)
        return new_rank < old_rank

    def _resolve_sync_channel_creator(
        self,
        conn: Any,
        local_user_id: Optional[str],
        origin_peer: Optional[str] = None,
    ) -> str:
        """Resolve a valid user ID for FK-safe synced channel creation.

        Preference order:
        1) A shadow/local user that matches the channel origin peer (if provided)
        2) explicit local_user_id hint
        3) system/local bootstrap users
        4) first available user row
        """
        if origin_peer and isinstance(origin_peer, str):
            origin_row = conn.execute(
                "SELECT id FROM users WHERE origin_peer = ? ORDER BY id ASC LIMIT 1",
                (origin_peer,),
            ).fetchone()
            if origin_row:
                return cast(
                    str,
                    origin_row[0] if not hasattr(origin_row, 'keys') else origin_row['id'],
                )

        candidates: list[str] = []
        if local_user_id and isinstance(local_user_id, str):
            candidates.append(local_user_id)
        candidates.extend(['system', 'local_user'])

        for candidate in candidates:
            row = conn.execute(
                "SELECT id FROM users WHERE id = ?",
                (candidate,),
            ).fetchone()
            if row:
                return candidate

        # Last-resort bootstrap for legacy installs where defaults were not seeded.
        try:
            conn.execute(
                """
                INSERT OR IGNORE INTO users (id, username, public_key)
                VALUES ('system', 'System', 'system_public_key')
                """
            )
            row = conn.execute(
                "SELECT id FROM users WHERE id = 'system'"
            ).fetchone()
            if row:
                return 'system'
        except Exception as e:
            logger.warning(f"Could not bootstrap system user for synced channel creator: {e}")

        row = conn.execute("SELECT id FROM users LIMIT 1").fetchone()
        if row:
            return cast(str, row[0] if not hasattr(row, 'keys') else row['id'])

        raise RuntimeError("No local users available to satisfy channels.created_by foreign key")

    @log_performance('channels')
    def create_channel(self, name: str, channel_type: ChannelType,
                      created_by: str, description: Optional[str] = None,
                      initial_members: Optional[List[str]] = None,
                      privacy_mode: str = 'open',
                      origin_peer: Optional[str] = None) -> Optional[Channel]:
        """Create a new channel."""
        # Normalize — strip leading '#' so the UI doesn't double-prefix
        name = self._normalize_channel_name(name)
        logger.info(f"Creating channel: name={name}, type={channel_type.value}, created_by={created_by}")

        try:
            # Validate channel name
            if not name or len(name) < 1:
                logger.error("Channel name cannot be empty")
                return None
            
            if len(name) > 80:
                logger.error(f"Channel name too long: {len(name)} > 80")
                return None

            creator_policy = self.get_user_channel_governance(created_by)
            if (
                creator_policy.get('enabled')
                and creator_policy.get('block_public_channels')
                and self._is_public_channel(channel_type.value, privacy_mode)
            ):
                logger.warning(
                    f"Governance denied public channel creation for {created_by}: "
                    f"name={name}, channel_type={channel_type.value}, privacy_mode={privacy_mode}"
                )
                return None
            if creator_policy.get('enabled') and creator_policy.get('restrict_to_allowed_channels'):
                logger.warning(
                    f"Governance denied channel creation for {created_by}: "
                    f"allowlist mode active for user"
                )
                return None
            
            # Generate unique channel ID
            channel_id = f"C{secrets.token_hex(8)}"
            logger.debug(f"Generated channel ID: {channel_id}")
            
            # Create channel object
            created_at = datetime.now(timezone.utc)
            lifecycle_preserved = channel_type == ChannelType.GENERAL or name == 'general'
            lifecycle_ttl_days = self.DEFAULT_CHANNEL_LIFECYCLE_DAYS
            channel = Channel(
                id=channel_id,
                name=name,
                channel_type=channel_type,
                created_by=created_by,
                created_at=created_at,
                last_activity_at=created_at,
                description=description,
                origin_peer=origin_peer,
                privacy_mode=privacy_mode or 'open',
                user_role='admin',
                crypto_mode=self.CRYPTO_MODE_LEGACY,
                lifecycle_ttl_days=lifecycle_ttl_days,
                lifecycle_preserved=lifecycle_preserved,
                lifecycle_status='preserved' if lifecycle_preserved else 'active',
            )
            
            with LogOperation(f"Database insert for channel {channel_id}"):
                with self.db.get_connection() as conn:
                    # Insert channel
                    conn.execute("""
                        INSERT INTO channels (
                            id, name, channel_type, created_by, created_at,
                            last_activity_at, description, privacy_mode, origin_peer,
                            lifecycle_ttl_days, lifecycle_preserved
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        channel_id,
                        name,
                        channel_type.value,
                        created_by,
                        self._format_db_timestamp(created_at),
                        self._format_db_timestamp(created_at),
                        description,
                        privacy_mode or 'open',
                        origin_peer,
                        lifecycle_ttl_days,
                        1 if lifecycle_preserved else 0,
                    ))
                    
                    # Add creator as admin member
                    conn.execute("""
                        INSERT INTO channel_members (channel_id, user_id, role)
                        VALUES (?, ?, 'admin')
                    """, (channel_id, created_by))

                    # Ensure instance owner (admin) always has access to targeted channels.
                    mode = (privacy_mode or 'open').lower()
                    if channel_type == ChannelType.PRIVATE or mode in self.TARGETED_PRIVACY_MODES:
                        try:
                            owner_id = self.db.get_instance_owner_user_id()
                        except Exception:
                            owner_id = None
                        if owner_id and owner_id != created_by:
                            conn.execute("""
                                INSERT OR IGNORE INTO channel_members (channel_id, user_id, role)
                                VALUES (?, ?, 'admin')
                            """, (channel_id, owner_id))
                    
                    # Add initial members if provided
                    if initial_members:
                        for user_id in initial_members:
                            if user_id != created_by:  # Don't add creator twice
                                target_policy = self._load_user_channel_governance(conn, user_id)
                                allowed, reason = self._is_channel_allowed_by_policy(
                                    policy=target_policy,
                                    channel_id=channel_id,
                                    channel_type=channel_type.value,
                                    privacy_mode=privacy_mode,
                                )
                                if not allowed:
                                    logger.warning(
                                        f"Skipping member {user_id} for channel {channel_id} "
                                        f"due to governance policy ({reason})"
                                    )
                                    continue
                                conn.execute("""
                                    INSERT OR IGNORE INTO channel_members (channel_id, user_id)
                                    VALUES (?, ?)
                                """, (channel_id, user_id))
                    
                    conn.commit()
            
            logger.info(f"Successfully created channel {channel_id}: {name}")
            self._emit_channel_user_event(
                channel_id=channel_id,
                event_type=EVENT_CHANNEL_STATE_UPDATED,
                actor_user_id=created_by,
                target_user_ids=[created_by] + list(initial_members or []),
                payload={
                    "reason": "channel_created",
                    "channel_name": name,
                },
                dedupe_suffix="channel_created",
            )
            return channel
            
        except Exception as e:
            logger.error(f"Failed to create channel: {e}", exc_info=True)
            return None
    
    def get_all_public_channels(self) -> List[Dict[str, Any]]:
        """
        Return all public channels as lightweight dicts for P2P sync.
        
        Returns:
            List of dicts with id, name, type, description, origin_peer.
        """
        try:
            self._maybe_run_channel_lifecycle_scan()
            with self.db.get_connection() as conn:
                rows = conn.execute("""
                    SELECT id, name, channel_type, description,
                           created_at, origin_peer, privacy_mode,
                           COALESCE(last_activity_at, created_at) AS last_activity_at,
                           COALESCE(lifecycle_ttl_days, ?) AS lifecycle_ttl_days,
                           COALESCE(lifecycle_preserved, 0) AS lifecycle_preserved,
                           lifecycle_archived_at,
                           lifecycle_archive_reason
                    FROM channels
                    WHERE (channel_type = 'public' OR channel_type = 'general')
                      AND COALESCE(privacy_mode, 'open') NOT IN ('private', 'confidential')
                """, (self.DEFAULT_CHANNEL_LIFECYCLE_DAYS,)).fetchall()
                return [
                    {
                        'id': r[0],
                        'name': r[1],
                        'type': r[2],
                        'desc': r[3] or '',
                        'origin_peer': r[5] or '',
                        'privacy_mode': r[6] or 'open',
                        'last_activity_at': r[7],
                        'lifecycle_ttl_days': r[8],
                        'lifecycle_preserved': bool(r[9]),
                        'lifecycle_archived_at': r[10],
                        'lifecycle_archive_reason': r[11],
                    }
                    for r in rows
                ]
        except Exception as e:
            logger.error(f"Failed to get public channels: {e}", exc_info=True)
            return []

    def create_channel_from_sync(self, channel_id: str, name: str,
                                  channel_type: str, description: str,
                                  local_user_id: Optional[str],
                                  origin_peer: Optional[str] = None,
                                  privacy_mode: str = 'open',
                                  last_activity_at: Optional[Any] = None,
                                  initial_members: Optional[list[Any]] = None,
                                  lifecycle_ttl_days: Optional[int] = None,
                                  lifecycle_preserved: bool = False,
                                  lifecycle_archived_at: Optional[Any] = None,
                                  lifecycle_archive_reason: Optional[str] = None) -> Optional[Channel]:
        """
        Create a channel with a specific ID received from P2P sync.
        
        Unlike create_channel(), this accepts an explicit ID so the
        channel matches the one on the remote peer. Open channels
        add all local human users as members so the channel is visible
        to everyone on this instance. Targeted channels only add the
        explicitly specified initial_members (targeted propagation).
        
        Args:
            channel_id: The exact channel ID from the remote peer
            name: Channel name
            channel_type: Channel type string (e.g. 'public')
            description: Channel description
            local_user_id: Preferred local user ID for FK-safe creator fallback
            origin_peer: Peer ID that originally owns this channel
            privacy_mode: Channel privacy mode ('open', 'guarded', 'private', 'confidential')
            initial_members: For targeted channels, explicit list of user IDs to add
            
        Returns:
            Channel object if created, None if it already exists or failed
        """
        # Normalize — strip leading '#' to prevent double-hash display
        name = self._normalize_channel_name(name)
        try:
            sync_creator_id = 'system'
            with self.db.get_connection() as conn:
                # Check if channel already exists
                existing = conn.execute(
                    "SELECT 1 FROM channels WHERE id = ?", (channel_id,)
                ).fetchone()
                if existing:
                    logger.debug(f"Channel {channel_id} already exists, skipping sync-create")
                    return None

                sync_creator_id = self._resolve_sync_channel_creator(
                    conn,
                    local_user_id,
                    origin_peer=origin_peer,
                )

                created_at = datetime.now(timezone.utc)
                last_activity_dt = self._parse_datetime(last_activity_at) or created_at
                ttl_days = self._normalize_channel_lifecycle_ttl_days(
                    lifecycle_ttl_days,
                    default=self.DEFAULT_CHANNEL_LIFECYCLE_DAYS,
                )
                archived_dt = self._parse_datetime(lifecycle_archived_at)
                preserved = bool(lifecycle_preserved or channel_id == 'general' or channel_type == 'general')

                conn.execute("""
                    INSERT INTO channels (
                        id, name, channel_type, created_by, created_at,
                        last_activity_at, description, origin_peer, privacy_mode,
                        lifecycle_ttl_days, lifecycle_preserved,
                        lifecycle_archived_at, lifecycle_archive_reason
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    channel_id,
                    name,
                    channel_type,
                    sync_creator_id,
                    self._format_db_timestamp(created_at),
                    self._format_db_timestamp(last_activity_dt),
                    description,
                    origin_peer,
                    privacy_mode or 'open',
                    ttl_days,
                    1 if preserved else 0,
                    self._format_db_timestamp(archived_dt) if archived_dt else None,
                    lifecycle_archive_reason,
                ))

                added = 0
                mode = (privacy_mode or '').lower()
                is_targeted = mode in self.TARGETED_PRIVACY_MODES or channel_type == 'private'
                if is_targeted:
                    # Targeted channels: only add explicitly specified members
                    if initial_members:
                        # SECURITY: Validate that initial_members are legitimate local users
                        for uid in initial_members:
                            if uid and isinstance(uid, str):
                                user_check = conn.execute(
                                    "SELECT id FROM users WHERE id = ?", (uid,)
                                ).fetchone()
                                if not user_check:
                                    logger.warning(
                                        f"SECURITY: Rejected invalid user_id '{uid}' in initial_members "
                                        f"from peer {origin_peer} for channel {channel_id}"
                                    )
                                    continue
                                target_policy = self._load_user_channel_governance(conn, uid)
                                allowed, reason = self._is_channel_allowed_by_policy(
                                    policy=target_policy,
                                    channel_id=channel_id,
                                    channel_type=channel_type,
                                    privacy_mode=privacy_mode,
                                )
                                if not allowed:
                                    logger.warning(
                                        f"SECURITY: Skipping targeted member {uid} for channel {channel_id} "
                                        f"due to governance policy ({reason})"
                                    )
                                    continue
                                conn.execute("""
                                    INSERT OR IGNORE INTO channel_members (channel_id, user_id, role)
                                    VALUES (?, ?, 'member')
                                """, (channel_id, uid))
                                added += 1
                                logger.debug(f"Added targeted member {uid} to channel {channel_id}")
                else:
                    # Public channels: add ALL registered human users
                    human_users = conn.execute("""
                        SELECT id FROM users
                        WHERE id != 'system' AND id != 'local_user'
                          AND password_hash IS NOT NULL AND password_hash != ''
                    """).fetchall()

                    for (uid,) in human_users:
                        target_policy = self._load_user_channel_governance(conn, uid)
                        allowed, reason = self._is_channel_allowed_by_policy(
                            policy=target_policy,
                            channel_id=channel_id,
                            channel_type=channel_type,
                            privacy_mode=privacy_mode,
                        )
                        if not allowed:
                            logger.info(
                                f"Skipping auto-membership for {uid} in synced channel {channel_id} "
                                f"due to governance policy ({reason})"
                            )
                            continue
                        conn.execute("""
                            INSERT OR IGNORE INTO channel_members (channel_id, user_id, role)
                            VALUES (?, ?, 'member')
                        """, (channel_id, uid))
                        added += 1

                # Fallback: if no human users found, add the provided user (open/guarded only)
                if added == 0 and local_user_id and not is_targeted:
                    fallback_policy = self._load_user_channel_governance(conn, local_user_id)
                    fallback_allowed, fallback_reason = self._is_channel_allowed_by_policy(
                        policy=fallback_policy,
                        channel_id=channel_id,
                        channel_type=channel_type,
                        privacy_mode=privacy_mode,
                    )
                    if not fallback_allowed:
                        logger.info(
                            f"Skipped fallback member {local_user_id} for synced channel {channel_id} "
                            f"due to governance policy ({fallback_reason})"
                        )
                    else:
                        conn.execute("""
                            INSERT OR IGNORE INTO channel_members (channel_id, user_id, role)
                            VALUES (?, ?, 'member')
                        """, (channel_id, local_user_id))

                conn.commit()

            channel = Channel(
                id=channel_id,
                name=name,
                channel_type=ChannelType(channel_type) if channel_type in [e.value for e in ChannelType] else ChannelType.PUBLIC,
                created_by=sync_creator_id,
                created_at=created_at,
                last_activity_at=last_activity_dt,
                description=description,
                privacy_mode=privacy_mode or 'open',
                crypto_mode=self.CRYPTO_MODE_LEGACY,
                lifecycle_ttl_days=ttl_days,
                lifecycle_preserved=preserved,
                archived_at=archived_dt,
                archive_reason=lifecycle_archive_reason,
                lifecycle_status='archived' if archived_dt else ('preserved' if preserved else 'active'),
            )
            logger.info(f"Created synced channel {channel_id}: {name}")
            return channel

        except Exception as e:
            logger.error(f"Failed to create synced channel {channel_id}: {e}", exc_info=True)
            return None

    def merge_or_adopt_channel(self, remote_id: str, remote_name: str,
                                remote_type: str, remote_desc: str,
                                local_user_id: str,
                                from_peer: str,
                                privacy_mode: str = 'open',
                                last_activity_at: Optional[Any] = None,
                                lifecycle_ttl_days: Optional[int] = None,
                                lifecycle_preserved: bool = False,
                                lifecycle_archived_at: Optional[Any] = None,
                                lifecycle_archive_reason: Optional[str] = None) -> Optional[str]:
        """
        Handle a remote channel that may conflict with a local one.
        
        Rules:
        - If remote ID already exists locally: skip (already synced).
        - If a local channel has the same name but different ID and is
          empty (no messages): adopt the remote ID (delete local, create
          with remote ID).
        - If same name, different ID, and local has messages: create the
          remote channel with a disambiguated name.
        - Otherwise: create the remote channel as-is.
        
        Returns:
            The channel ID that was created/adopted, or None if skipped.
        """
        # Normalize — strip leading '#' to prevent double-hash display
        remote_name = self._normalize_channel_name(remote_name)
        privacy_mode = self._normalize_privacy_mode(privacy_mode, default='open')
        # General channel is always open, never downgraded by remote metadata
        if remote_id == 'general':
            privacy_mode = 'open'
            lifecycle_preserved = True
        try:
            with self.db.get_connection() as conn:
                # Already have this exact channel?
                existing = conn.execute(
                    "SELECT name, description, privacy_mode, origin_peer, created_by FROM channels WHERE id = ?",
                    (remote_id,)
                ).fetchone()
                if existing:
                    old_name = existing[0] or ''
                    old_desc = existing[1] or ''
                    old_privacy = self._normalize_privacy_mode(existing[2], default='open')
                    old_origin_peer = existing[3] or ''
                    old_created_by = existing[4] or ''

                    can_apply_remote_metadata = False
                    if old_origin_peer:
                        can_apply_remote_metadata = bool(from_peer and str(old_origin_peer) == str(from_peer))
                    elif old_created_by == 'p2p-sync':
                        # Legacy synced rows may have NULL origin_peer; allow updates cautiously.
                        can_apply_remote_metadata = True

                    # Update placeholder names / empty descriptions
                    # with real metadata from the remote peer
                    needs_update = False
                    new_name = old_name
                    new_desc = old_desc
                    new_privacy = old_privacy
                    if (can_apply_remote_metadata and old_name.startswith('peer-channel-') and remote_name
                            and not remote_name.startswith('peer-channel-')):
                        new_name = remote_name
                        needs_update = True
                    if (can_apply_remote_metadata and (not old_desc or old_desc.startswith('Auto-created from P2P'))
                            and remote_desc
                            and not remote_desc.startswith('Auto-created from P2P')):
                        new_desc = remote_desc
                        needs_update = True
                    if privacy_mode and privacy_mode != old_privacy:
                        privacy_downgrade = self._is_privacy_downgrade(old_privacy, privacy_mode)
                        if can_apply_remote_metadata:
                            # For legacy rows with unknown origin, fail closed on downgrades.
                            if old_origin_peer or not privacy_downgrade:
                                new_privacy = privacy_mode
                                needs_update = True
                            else:
                                logger.warning(
                                    "SECURITY: Ignoring privacy downgrade for channel %s "
                                    "(origin unknown, old=%s, new=%s, from=%s)",
                                    remote_id, old_privacy, privacy_mode, from_peer,
                                )
                        else:
                            logger.warning(
                                "SECURITY: Ignoring privacy update for channel %s from non-origin peer %s "
                                "(origin=%s, old=%s, incoming=%s)",
                                remote_id, from_peer, old_origin_peer or 'local/unknown',
                                old_privacy, privacy_mode,
                            )
                    ttl_days = self._normalize_channel_lifecycle_ttl_days(
                        lifecycle_ttl_days,
                        default=self.DEFAULT_CHANNEL_LIFECYCLE_DAYS,
                    )
                    incoming_last_activity = self._parse_datetime(last_activity_at)
                    archived_dt = self._parse_datetime(lifecycle_archived_at)
                    old_ttl_days = None
                    old_preserved = False
                    old_archived_at = None
                    old_archive_reason = None
                    old_last_activity = None
                    try:
                        extra_row = conn.execute(
                            """
                            SELECT lifecycle_ttl_days, COALESCE(lifecycle_preserved, 0) AS lifecycle_preserved,
                                   last_activity_at,
                                   lifecycle_archived_at, lifecycle_archive_reason
                            FROM channels WHERE id = ?
                            """,
                            (remote_id,),
                        ).fetchone()
                        if extra_row:
                            old_ttl_days = extra_row['lifecycle_ttl_days']
                            old_preserved = bool(extra_row['lifecycle_preserved'])
                            old_last_activity = self._parse_datetime(extra_row['last_activity_at'])
                            old_archived_at = extra_row['lifecycle_archived_at']
                            old_archive_reason = extra_row['lifecycle_archive_reason']
                    except Exception:
                        pass
                    if can_apply_remote_metadata:
                        if ttl_days and ttl_days != self._normalize_channel_lifecycle_ttl_days(old_ttl_days, default=ttl_days):
                            needs_update = True
                        if bool(lifecycle_preserved) != bool(old_preserved):
                            needs_update = True
                        if incoming_last_activity and (old_last_activity is None or incoming_last_activity > old_last_activity):
                            needs_update = True
                        if bool(archived_dt) != bool(self._parse_datetime(old_archived_at)):
                            needs_update = True
                        if lifecycle_archive_reason != old_archive_reason:
                            needs_update = True
                    if needs_update:
                        try:
                            conn.execute(
                                """
                                UPDATE channels
                                   SET name = ?,
                                       description = ?,
                                       privacy_mode = ?,
                                       origin_peer = COALESCE(origin_peer, ?),
                                       last_activity_at = COALESCE(?, last_activity_at),
                                       lifecycle_ttl_days = ?,
                                       lifecycle_preserved = ?,
                                       lifecycle_archived_at = ?,
                                       lifecycle_archive_reason = ?
                                 WHERE id = ?
                                """,
                                (
                                    new_name,
                                    new_desc,
                                    new_privacy,
                                    from_peer,
                                    self._format_db_timestamp(incoming_last_activity) if incoming_last_activity else None,
                                    ttl_days,
                                    1 if lifecycle_preserved else 0,
                                    self._format_db_timestamp(archived_dt) if archived_dt else None,
                                    lifecycle_archive_reason,
                                    remote_id,
                                )
                            )
                            conn.commit()
                            logger.info(f"Updated channel {remote_id}: "
                                        f"name='{old_name}'->'{new_name}', "
                                        f"desc updated={old_desc != new_desc}")
                            return remote_id
                        except Exception as ue:
                            logger.debug(f"Channel update for {remote_id} skipped: {ue}")
                    # Still set origin_peer if not yet set (only for synced rows).
                    try:
                        if old_created_by == 'p2p-sync':
                            conn.execute(
                                "UPDATE channels SET origin_peer = ? "
                                "WHERE id = ? AND origin_peer IS NULL",
                                (from_peer, remote_id)
                            )
                            conn.commit()
                    except Exception:
                        pass
                    return None  # Already synced, no updates needed

                # Check for same-name conflict
                conflict = conn.execute(
                    "SELECT id FROM channels WHERE name = ? AND id != ?",
                    (remote_name, remote_id)
                ).fetchone()

                if conflict:
                    local_id = conflict[0]
                    # Count messages in the local channel
                    msg_count = conn.execute(
                        "SELECT COUNT(*) FROM channel_messages WHERE channel_id = ?",
                        (local_id,)
                    ).fetchone()[0]

                    if msg_count == 0:
                        # Local channel is empty — adopt remote ID
                        # Move members to the new channel, delete old one
                        sync_creator_id = self._resolve_sync_channel_creator(
                            conn,
                            local_user_id,
                            origin_peer=from_peer,
                        )
                        members = conn.execute(
                            "SELECT user_id, role FROM channel_members WHERE channel_id = ?",
                            (local_id,)
                        ).fetchall()

                        conn.execute("DELETE FROM channel_members WHERE channel_id = ?", (local_id,))
                        conn.execute("DELETE FROM channels WHERE id = ?", (local_id,))

                        conn.execute("""
                            INSERT INTO channels (
                                id, name, channel_type, created_by, created_at,
                                last_activity_at, description, origin_peer, privacy_mode,
                                lifecycle_ttl_days, lifecycle_preserved,
                                lifecycle_archived_at, lifecycle_archive_reason
                            )
                            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP, ?, ?, ?, ?, ?, ?, ?, ?)
                        """, (
                            remote_id,
                            remote_name,
                            remote_type,
                            sync_creator_id,
                            self._format_db_timestamp(self._parse_datetime(last_activity_at) or datetime.now(timezone.utc)),
                            remote_desc,
                            from_peer,
                            privacy_mode or 'open',
                            self._normalize_channel_lifecycle_ttl_days(
                                lifecycle_ttl_days,
                                default=self.DEFAULT_CHANNEL_LIFECYCLE_DAYS,
                            ),
                            1 if lifecycle_preserved else 0,
                            self._format_db_timestamp(self._parse_datetime(lifecycle_archived_at))
                            if self._parse_datetime(lifecycle_archived_at) else None,
                            lifecycle_archive_reason,
                        ))

                        for user_id, role in members:
                            conn.execute("""
                                INSERT OR IGNORE INTO channel_members (channel_id, user_id, role)
                                VALUES (?, ?, ?)
                            """, (remote_id, user_id, role))

                        conn.commit()
                        logger.info(f"Adopted remote channel {remote_id} for '{remote_name}' "
                                    f"(replaced empty local {local_id})")
                        return remote_id
                    else:
                        # Local channel has messages — keep both, disambiguate remote
                        peer_tag = from_peer[:8] if from_peer else 'remote'
                        disambig_name = f"{remote_name} (peer-{peer_tag})"
                        logger.info(f"Name conflict for '{remote_name}': keeping local {local_id}, "
                                    f"creating remote {remote_id} as '{disambig_name}'")
                        self.create_channel_from_sync(
                            remote_id, disambig_name, remote_type, remote_desc,
                            local_user_id, origin_peer=from_peer,
                            privacy_mode=privacy_mode,
                            last_activity_at=last_activity_at,
                            lifecycle_ttl_days=lifecycle_ttl_days,
                            lifecycle_preserved=lifecycle_preserved,
                            lifecycle_archived_at=lifecycle_archived_at,
                            lifecycle_archive_reason=lifecycle_archive_reason,
                        )
                        return remote_id

            # No conflict — create normally
            self.create_channel_from_sync(
                remote_id, remote_name, remote_type, remote_desc, local_user_id,
                origin_peer=from_peer,
                privacy_mode=privacy_mode,
                last_activity_at=last_activity_at,
                lifecycle_ttl_days=lifecycle_ttl_days,
                lifecycle_preserved=lifecycle_preserved,
                lifecycle_archived_at=lifecycle_archived_at,
                lifecycle_archive_reason=lifecycle_archive_reason,
            )
            return remote_id

        except Exception as e:
            logger.error(f"Error in merge_or_adopt_channel: {e}", exc_info=True)
            return None

    def update_channel_privacy(self, channel_id: str, user_id: str,
                               privacy_mode: str,
                               allow_admin: bool = False,
                               local_peer_id: Optional[str] = None) -> bool:
        """Update a channel privacy mode. Only channel admins can edit."""
        allowed_modes = {'open', 'guarded', 'private', 'confidential'}
        if privacy_mode not in allowed_modes:
            return False
        try:
            with self.db.get_connection() as conn:
                # Never allow privacy changes on the default general channel
                if channel_id == 'general':
                    return False

                role_row = conn.execute(
                    "SELECT role FROM channel_members WHERE channel_id = ? AND user_id = ?",
                    (channel_id, user_id)
                ).fetchone()
                if not role_row:
                    return False
                role = role_row['role'] if isinstance(role_row, dict) or hasattr(role_row, '__getitem__') else role_row[0]

                chan_row = conn.execute(
                    "SELECT origin_peer FROM channels WHERE id = ?",
                    (channel_id,)
                ).fetchone()
                origin_peer = None
                if chan_row:
                    try:
                        origin_peer = chan_row['origin_peer']
                    except Exception:
                        origin_peer = chan_row[0]

                is_origin_local = not origin_peer
                if local_peer_id and origin_peer:
                    is_origin_local = origin_peer == local_peer_id

                if not is_origin_local:
                    return False
                if role != 'admin' and not allow_admin:
                    return False
                conn.execute(
                    "UPDATE channels SET privacy_mode = ? WHERE id = ?",
                    (privacy_mode, channel_id)
                )
                if privacy_mode in self.TARGETED_PRIVACY_MODES:
                    # Prune members so targeted channels don't keep broad-era membership.
                    keep_ids = set()
                    try:
                        rows = conn.execute(
                            "SELECT user_id, role FROM channel_members WHERE channel_id = ?",
                            (channel_id,)
                        ).fetchall()
                        for r in rows:
                            try:
                                uid = r['user_id']
                                role_val = r['role']
                            except Exception:
                                uid = r[0]
                                role_val = r[1] if len(r) > 1 else None
                            if role_val == 'admin':
                                keep_ids.add(uid)
                    except Exception:
                        pass
                    try:
                        owner_id = self.db.get_instance_owner_user_id()
                    except Exception:
                        owner_id = None
                    if owner_id:
                        keep_ids.add(owner_id)
                        conn.execute(
                            "INSERT OR IGNORE INTO channel_members (channel_id, user_id, role) VALUES (?, ?, 'admin')",
                            (channel_id, owner_id)
                        )
                    try:
                        creator_row = conn.execute(
                            "SELECT created_by FROM channels WHERE id = ?",
                            (channel_id,)
                        ).fetchone()
                        if creator_row:
                            try:
                                keep_ids.add(creator_row['created_by'])
                            except Exception:
                                keep_ids.add(creator_row[0])
                    except Exception:
                        pass
                    if user_id:
                        keep_ids.add(user_id)
                    if keep_ids:
                        placeholders = ",".join("?" for _ in keep_ids)
                        params = [channel_id] + list(keep_ids)
                        conn.execute(
                            f"DELETE FROM channel_members WHERE channel_id = ? AND user_id NOT IN ({placeholders})",
                            params
                        )
                conn.commit()
                current_member_count = 0
                try:
                    member_row = conn.execute(
                        "SELECT COUNT(*) AS count FROM channel_members WHERE channel_id = ?",
                        (channel_id,),
                    ).fetchone()
                    if member_row:
                        current_member_count = int(
                            member_row['count'] if hasattr(member_row, 'keys') else member_row[0]
                        )
                except Exception:
                    current_member_count = 0
                self._emit_channel_user_event(
                    channel_id=channel_id,
                    event_type=EVENT_CHANNEL_STATE_UPDATED,
                    actor_user_id=user_id,
                    payload={
                        "reason": "privacy_updated",
                        "privacy_mode": privacy_mode,
                        "channel_type": 'private' if privacy_mode in self.TARGETED_PRIVACY_MODES else 'public',
                        "member_count": current_member_count,
                    },
                    dedupe_suffix=f"privacy:{privacy_mode}:{current_member_count}",
                )
                return True
        except Exception as e:
            logger.error(f"Failed to update channel privacy: {e}", exc_info=True)
            return False

    def update_channel_lifecycle_settings(
        self,
        channel_id: str,
        user_id: str,
        ttl_days: Optional[Any] = None,
        preserved: Optional[bool] = None,
        archived: Optional[bool] = None,
        archive_reason: Optional[str] = None,
        allow_admin: bool = False,
        local_peer_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Update non-destructive lifecycle settings for a channel."""
        try:
            normalized_ttl = None
            if ttl_days is not None:
                normalized_ttl = self._normalize_channel_lifecycle_ttl_days(
                    ttl_days,
                    default=self.DEFAULT_CHANNEL_LIFECYCLE_DAYS,
                )

            with self.db.get_connection() as conn:
                row = conn.execute(
                    """
                    SELECT created_by, origin_peer, channel_type,
                           COALESCE(lifecycle_ttl_days, ?) AS lifecycle_ttl_days,
                           COALESCE(lifecycle_preserved, 0) AS lifecycle_preserved,
                           lifecycle_archived_at, lifecycle_archive_reason,
                           COALESCE(last_activity_at, created_at) AS last_activity_at,
                           created_at
                    FROM channels
                    WHERE id = ?
                    """,
                    (self.DEFAULT_CHANNEL_LIFECYCLE_DAYS, channel_id),
                ).fetchone()
                if not row:
                    return None

                channel_type = str(row['channel_type'] or 'public').strip().lower()
                if channel_id == 'general' or channel_type == 'general':
                    archived = False
                    preserved = True if preserved is None else bool(preserved)

                origin_peer = str(row['origin_peer'] or '').strip()
                is_origin_local = not origin_peer or (local_peer_id and origin_peer == local_peer_id)
                if not is_origin_local:
                    return None
                role_row = conn.execute(
                    "SELECT role FROM channel_members WHERE channel_id = ? AND user_id = ?",
                    (channel_id, user_id),
                ).fetchone()
                role = role_row['role'] if role_row and hasattr(role_row, 'keys') else (role_row[0] if role_row else None)
                if role != 'admin' and not allow_admin:
                    return None

                next_ttl = normalized_ttl if normalized_ttl is not None else int(row['lifecycle_ttl_days'] or self.DEFAULT_CHANNEL_LIFECYCLE_DAYS)
                next_preserved = bool(row['lifecycle_preserved']) if preserved is None else bool(preserved)
                next_archived_at = self._parse_datetime(row['lifecycle_archived_at'])
                next_archive_reason = row['lifecycle_archive_reason']

                if archived is True:
                    next_archived_at = datetime.now(timezone.utc)
                    next_archive_reason = archive_reason or 'manual_archive'
                elif archived is False:
                    next_archived_at = None
                    next_archive_reason = None

                conn.execute(
                    """
                    UPDATE channels
                       SET lifecycle_ttl_days = ?,
                           lifecycle_preserved = ?,
                           lifecycle_archived_at = ?,
                           lifecycle_archive_reason = ?
                     WHERE id = ?
                    """,
                    (
                        next_ttl,
                        1 if next_preserved else 0,
                        self._format_db_timestamp(next_archived_at) if next_archived_at else None,
                        next_archive_reason,
                        channel_id,
                    ),
                )
                conn.commit()

                last_activity = self._parse_datetime(row['last_activity_at']) or self._parse_datetime(row['created_at']) or datetime.now(timezone.utc)
                status = 'archived' if next_archived_at else ('preserved' if next_preserved else 'active')
                if not next_preserved and not next_archived_at:
                    due_at = last_activity + timedelta(days=next_ttl)
                    remaining_seconds = (due_at - datetime.now(timezone.utc)).total_seconds()
                    if remaining_seconds <= 0:
                        status = 'inactive'
                    elif remaining_seconds <= self.CHANNEL_LIFECYCLE_WARNING_DAYS * 86400:
                        status = 'cooling'

                result = {
                    'ttl_days': next_ttl,
                    'preserved': next_preserved,
                    'archived_at': self._format_db_timestamp(next_archived_at) if next_archived_at else None,
                    'archive_reason': next_archive_reason,
                    'status': status,
                    'days_until_archive': None,
                    'last_activity_at': self._format_db_timestamp(last_activity) if last_activity else None,
                }
                if not next_preserved and not next_archived_at:
                    due_at = last_activity + timedelta(days=next_ttl)
                    remaining_seconds = (due_at - datetime.now(timezone.utc)).total_seconds()
                    if remaining_seconds > 0:
                        result['days_until_archive'] = max(
                            0,
                            int(math.ceil(remaining_seconds / 86400.0)),
                        )
            self._emit_channel_user_event(
                channel_id=channel_id,
                event_type=EVENT_CHANNEL_STATE_UPDATED,
                actor_user_id=user_id,
                payload={
                    'reason': 'lifecycle_updated',
                    'status': result['status'],
                    'ttl_days': result['ttl_days'],
                    'preserved': result['preserved'],
                    'archived_at': result['archived_at'],
                    'archive_reason': result['archive_reason'],
                    'days_until_archive': result['days_until_archive'],
                    'last_activity_at': result['last_activity_at'],
                },
                dedupe_suffix=f"lifecycle:{result['status']}:{result['ttl_days']}:{1 if result['preserved'] else 0}:{result['archived_at'] or ''}",
            )
            return result
        except Exception as e:
            logger.error(f"Failed to update channel lifecycle settings: {e}", exc_info=True)
            return None

    def update_channel_notifications(self, channel_id: str, user_id: str,
                                     enabled: bool) -> bool:
        """Enable/disable notifications for a channel membership."""
        try:
            with self.db.get_connection() as conn:
                row = conn.execute(
                    "SELECT 1 FROM channel_members WHERE channel_id = ? AND user_id = ?",
                    (channel_id, user_id)
                ).fetchone()
                if not row:
                    return False
                conn.execute(
                    "UPDATE channel_members SET notifications_enabled = ? "
                    "WHERE channel_id = ? AND user_id = ?",
                    (1 if enabled else 0, channel_id, user_id)
                )
                conn.commit()
                self._emit_channel_user_event(
                    channel_id=channel_id,
                    event_type=EVENT_CHANNEL_STATE_UPDATED,
                    actor_user_id=user_id,
                    target_user_ids=[user_id],
                    payload={
                        "reason": "notifications_updated",
                        "notifications_enabled": bool(enabled),
                    },
                    dedupe_suffix=f"notifications:{1 if enabled else 0}",
                )
                return True
        except Exception as e:
            logger.error(f"Failed to update channel notifications: {e}", exc_info=True)
            return False

    def _resolve_thread_root_id_conn(
        self,
        conn: Any,
        channel_id: str,
        message_id: str,
    ) -> Optional[str]:
        """Return the root message id for a message inside a channel thread."""
        if not channel_id or not message_id:
            return None

        current_id = str(message_id).strip()
        if not current_id:
            return None

        visited: set[str] = set()
        max_hops = 64
        hops = 0

        while current_id and current_id not in visited and hops < max_hops:
            visited.add(current_id)
            row = conn.execute(
                """
                SELECT parent_message_id
                FROM channel_messages
                WHERE id = ? AND channel_id = ?
                """,
                (current_id, channel_id),
            ).fetchone()
            if not row:
                return None
            parent_id = str(row['parent_message_id']).strip() if row['parent_message_id'] else ''
            if not parent_id:
                return current_id
            current_id = parent_id
            hops += 1

        return current_id if current_id else None

    def resolve_thread_root_message_id(self, channel_id: str, message_id: str) -> Optional[str]:
        """Resolve a message id to its thread root id."""
        if not channel_id or not message_id:
            return None
        try:
            with self.db.get_connection() as conn:
                return self._resolve_thread_root_id_conn(conn, channel_id, message_id)
        except Exception as e:
            logger.debug(f"Failed to resolve thread root for {message_id}: {e}")
            return None

    def get_thread_root_author_id(self, channel_id: str, message_id: str) -> Optional[str]:
        """Return the root-author user id for a thread message."""
        if not channel_id or not message_id:
            return None
        try:
            with self.db.get_connection() as conn:
                root_id = self._resolve_thread_root_id_conn(conn, channel_id, message_id)
                if not root_id:
                    return None
                row = conn.execute(
                    """
                    SELECT user_id
                    FROM channel_messages
                    WHERE id = ? AND channel_id = ?
                    """,
                    (root_id, channel_id),
                ).fetchone()
                if not row:
                    return None
                return str(row['user_id']).strip() if row['user_id'] else None
        except Exception as e:
            logger.debug(f"Failed to fetch thread root author for {message_id}: {e}")
            return None

    def get_thread_subscription_state(
        self,
        user_id: str,
        channel_id: str,
        message_id: str,
    ) -> Dict[str, Any]:
        """Return explicit thread subscription state for a user/message."""
        result = {
            'thread_root_message_id': None,
            'root_author_id': None,
            'is_root_author': False,
            'explicit_subscribed': None,
            'subscribed': None,
        }
        if not user_id or not channel_id or not message_id:
            return result

        try:
            with self.db.get_connection() as conn:
                root_id = self._resolve_thread_root_id_conn(conn, channel_id, message_id)
                if not root_id:
                    return result
                result['thread_root_message_id'] = root_id

                root_row = conn.execute(
                    """
                    SELECT user_id
                    FROM channel_messages
                    WHERE id = ? AND channel_id = ?
                    """,
                    (root_id, channel_id),
                ).fetchone()
                if root_row and root_row['user_id']:
                    root_author_id = str(root_row['user_id']).strip()
                    result['root_author_id'] = root_author_id
                    result['is_root_author'] = bool(root_author_id and root_author_id == user_id)

                sub_row = conn.execute(
                    """
                    SELECT subscribed
                    FROM channel_thread_subscriptions
                    WHERE thread_root_message_id = ? AND user_id = ?
                    LIMIT 1
                    """,
                    (root_id, user_id),
                ).fetchone()
                if sub_row is not None:
                    explicit = bool(sub_row['subscribed'])
                    result['explicit_subscribed'] = explicit
                    result['subscribed'] = explicit
                return result
        except Exception as e:
            logger.debug(
                f"Failed to fetch thread subscription state user={user_id} channel={channel_id} message={message_id}: {e}"
            )
            return result

    def set_thread_subscription(
        self,
        user_id: str,
        channel_id: str,
        message_id: str,
        subscribed: bool,
        *,
        source: str = 'manual',
        require_membership: bool = True,
    ) -> Dict[str, Any]:
        """Persist a per-thread subscription preference for a user."""
        result = {
            'success': False,
            'thread_root_message_id': None,
            'subscribed': bool(subscribed),
        }
        if not user_id or not channel_id or not message_id:
            return result

        try:
            with self.db.get_connection() as conn:
                if require_membership:
                    member_row = conn.execute(
                        """
                        SELECT 1
                        FROM channel_members
                        WHERE channel_id = ? AND user_id = ?
                        LIMIT 1
                        """,
                        (channel_id, user_id),
                    ).fetchone()
                    if not member_row:
                        return result

                root_id = self._resolve_thread_root_id_conn(conn, channel_id, message_id)
                if not root_id:
                    return result

                conn.execute(
                    """
                    INSERT INTO channel_thread_subscriptions
                    (channel_id, thread_root_message_id, user_id, subscribed, source, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                    ON CONFLICT(thread_root_message_id, user_id) DO UPDATE SET
                        subscribed = excluded.subscribed,
                        source = excluded.source,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    (
                        channel_id,
                        root_id,
                        user_id,
                        1 if subscribed else 0,
                        source or 'manual',
                    ),
                )
                conn.commit()
                result['success'] = True
                result['thread_root_message_id'] = root_id
                return result
        except Exception as e:
            logger.error(
                f"Failed to set thread subscription user={user_id} channel={channel_id} message={message_id}: {e}",
                exc_info=True,
            )
            return result

    def get_thread_subscriber_ids(self, channel_id: str, thread_root_message_id: str) -> List[str]:
        """Return subscribed user ids for a channel thread root."""
        if not channel_id or not thread_root_message_id:
            return []
        try:
            with self.db.get_connection() as conn:
                rows = conn.execute(
                    """
                    SELECT ts.user_id
                    FROM channel_thread_subscriptions ts
                    INNER JOIN channel_members cm
                      ON cm.channel_id = ts.channel_id
                     AND cm.user_id = ts.user_id
                    WHERE ts.channel_id = ?
                      AND ts.thread_root_message_id = ?
                      AND ts.subscribed = 1
                    """,
                    (channel_id, thread_root_message_id),
                ).fetchall()
                return [str(row['user_id']) for row in rows if row and row['user_id']]
        except Exception as e:
            logger.debug(
                f"Failed to list thread subscribers channel={channel_id} root={thread_root_message_id}: {e}"
            )
            return []

    @log_performance('channels')
    def send_message(self, channel_id: str, user_id: str, content: str,
                    message_type: MessageType = MessageType.TEXT,
                    thread_id: Optional[str] = None, parent_message_id: Optional[str] = None,
                    attachments: Optional[List[Dict[str, Any]]] = None,
                    security: Optional[Dict[str, Any]] = None,
                    expires_at: Optional[Any] = None,
                    ttl_seconds: Optional[int] = None,
                    ttl_mode: Optional[str] = None,
                    origin_peer: Optional[str] = None) -> Optional[Message]:
        """Send a message to a channel."""
        logger.info(f"Sending message to channel {channel_id} by user {user_id}")
        logger.debug(f"Content length: {len(content)}, type: {message_type.value}")
        
        try:
            access = self.get_channel_access_decision(
                channel_id=channel_id,
                user_id=user_id,
                require_membership=True,
            )
            if not access.get('allowed'):
                logger.warning(
                    f"Channel send denied for user={user_id}, channel={channel_id}, "
                    f"reason={access.get('reason')}"
                )
                return None

            security_clean, sec_error = self.validate_security_metadata(security, strict=False)
            if sec_error:
                logger.warning(f"Dropping invalid security metadata for channel {channel_id}: {sec_error}")
            security = security_clean

            normalized_attachments = Message.normalize_attachments(attachments)
            if normalized_attachments:
                message_type = MessageType.FILE
            else:
                normalized_attachments = None
            
            # Generate unique message ID
            message_id = f"M{secrets.token_hex(12)}"
            logger.debug(f"Generated message ID: {message_id}")

            created_at = datetime.now(timezone.utc)
            expires_dt = self._resolve_expiry(
                expires_at=expires_at,
                ttl_seconds=ttl_seconds,
                ttl_mode=ttl_mode,
                apply_default=True,
                base_time=created_at,
            )
            created_db = self._format_db_timestamp(created_at)
            expires_db = self._format_db_timestamp(expires_dt) if expires_dt else None
            
            # Create message object
            message = Message(
                id=message_id,
                channel_id=channel_id,
                user_id=user_id,
                content=content,
                message_type=message_type,
                created_at=created_at,
                thread_id=thread_id,
                parent_message_id=parent_message_id,
                attachments=normalized_attachments,
                security=security,
                expires_at=expires_dt,
                origin_peer=origin_peer,
            )
            
            # Persist ttl_seconds/ttl_mode so catchup can send them to other peers
            ttl_sec_db = int(ttl_seconds) if ttl_seconds is not None else None
            ttl_mode_db = (ttl_mode or '').strip() or None

            with LogOperation(f"Database insert for message {message_id}"):
                with self.db.get_connection() as conn:
                    conn.execute("""
                        INSERT INTO channel_messages 
                        (id, channel_id, user_id, content, message_type, thread_id, parent_message_id, attachments, security, created_at, origin_peer, expires_at, ttl_seconds, ttl_mode)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        message_id, channel_id, user_id, content, message_type.value,
                        thread_id, parent_message_id,
                        json.dumps(normalized_attachments) if normalized_attachments else None,
                        json.dumps(security) if security else None,
                        created_db,
                        origin_peer,
                        expires_db,
                        ttl_sec_db,
                        ttl_mode_db,
                    ))

                    # Resurface parent message when this is a threaded reply
                    # (6-hour cap per parent to prevent spam)
                    if parent_message_id:
                        try:
                            conn.execute("""
                                UPDATE channel_messages SET last_activity_at = CURRENT_TIMESTAMP
                                WHERE id = ?
                                  AND (last_activity_at IS NULL
                                       OR last_activity_at < datetime('now', '-6 hours'))
                            """, (parent_message_id,))
                        except Exception as resurface_err:
                            logger.debug(f"Reply resurfacing skipped: {resurface_err}")

                    conn.execute(
                        """
                        UPDATE channels
                           SET last_activity_at = ?,
                               lifecycle_archived_at = NULL,
                               lifecycle_archive_reason = NULL
                         WHERE id = ?
                        """,
                        (created_db, channel_id),
                    )

                    conn.commit()
            
            logger.info(f"Successfully sent message {message_id} to channel {channel_id}")
            self._emit_channel_user_event(
                channel_id=channel_id,
                event_type=EVENT_CHANNEL_MESSAGE_CREATED,
                actor_user_id=user_id,
                payload={
                    "message_id": message_id,
                    "preview": (content or "").strip()[:160] or ("Attachment" if normalized_attachments else ""),
                },
                dedupe_suffix=message_id,
            )
            return message
            
        except Exception as e:
            logger.error(f"Failed to send message: {e}", exc_info=True)
            return None

    def update_message(self, message_id: str, user_id: str, content: str,
                       attachments: Optional[List[Dict[str, Any]]] = None,
                       allow_admin: bool = False) -> bool:
        """Update a channel message (author or admin)."""
        try:
            with self.db.get_connection() as conn:
                row = conn.execute(
                    "SELECT channel_id, user_id, attachments, message_type FROM channel_messages WHERE id = ?",
                    (message_id,)
                ).fetchone()
                if not row or (row['user_id'] != user_id and not allow_admin):
                    logger.warning(f"User {user_id} cannot update channel message {message_id}")
                    return False
                channel_id = str(row['channel_id'] or '').strip()

                final_attachments = attachments
                if final_attachments is None:
                    if row['attachments']:
                        try:
                            final_attachments = json.loads(row['attachments'])
                        except Exception:
                            final_attachments = None
                final_attachments = Message.normalize_attachments(final_attachments) if final_attachments else None

                if final_attachments:
                    final_message_type = MessageType.FILE.value
                else:
                    final_message_type = MessageType.TEXT.value

                edited_at = datetime.now(timezone.utc)
                edited_db = self._format_db_timestamp(edited_at)

                conn.execute(
                    "UPDATE channel_messages SET content = ?, message_type = ?, attachments = ?, edited_at = ? WHERE id = ?",
                    (
                        content,
                        final_message_type,
                        json.dumps(final_attachments) if final_attachments else None,
                        edited_db,
                        message_id,
                    )
                )
                if channel_id:
                    conn.execute(
                        """
                        UPDATE channels
                           SET last_activity_at = ?,
                               lifecycle_archived_at = NULL,
                               lifecycle_archive_reason = NULL
                         WHERE id = ?
                        """,
                        (edited_db, channel_id),
                    )
                conn.commit()
                logger.info(f"Updated channel message {message_id}")
                if channel_id:
                    self._emit_channel_user_event(
                        channel_id=channel_id,
                        event_type=EVENT_CHANNEL_MESSAGE_EDITED,
                        actor_user_id=user_id,
                        payload={
                            "message_id": message_id,
                            "preview": (content or "").strip()[:160]
                            or ("Attachment" if final_attachments else ""),
                        },
                        dedupe_suffix=message_id,
                    )
                return True
        except Exception as e:
            logger.error(f"Failed to update channel message: {e}", exc_info=True)
            return False

    def update_stream_attachment_status(self, stream_id: str, status: str) -> int:
        """Update posted stream-card attachment statuses for a stream lifecycle change."""
        sid = str(stream_id or '').strip()
        next_status = str(status or '').strip().lower()
        if not sid or next_status not in {'created', 'live', 'stopped'}:
            return 0
        changed_rows: List[Dict[str, str]] = []
        try:
            with self.db.get_connection() as conn:
                rows = conn.execute(
                    """
                    SELECT id, channel_id, user_id, content, attachments
                    FROM channel_messages
                    WHERE attachments IS NOT NULL AND attachments != '[]'
                    """
                ).fetchall()
                for row in rows:
                    try:
                        attachments = json.loads(row['attachments'] or '[]')
                    except Exception:
                        continue
                    if not isinstance(attachments, list):
                        continue
                    touched = False
                    updated_attachments: List[Dict[str, Any]] = []
                    for attachment in attachments:
                        att = Message.normalize_attachment(attachment)
                        if not att:
                            continue
                        if str(att.get('stream_id') or '').strip() == sid:
                            if str(att.get('status') or '').strip().lower() != next_status:
                                att['status'] = next_status
                                touched = True
                        updated_attachments.append(att)
                    if not touched:
                        continue
                    conn.execute(
                        "UPDATE channel_messages SET attachments = ? WHERE id = ?",
                        (json.dumps(updated_attachments), row['id']),
                    )
                    changed_rows.append({
                        'message_id': str(row['id'] or ''),
                        'channel_id': str(row['channel_id'] or ''),
                        'user_id': str(row['user_id'] or ''),
                        'preview': (str(row['content'] or '').strip()[:160] or 'Attachment'),
                    })
                if changed_rows:
                    conn.commit()
            for row in changed_rows:
                if row['channel_id']:
                    self._emit_channel_user_event(
                        channel_id=row['channel_id'],
                        event_type=EVENT_CHANNEL_MESSAGE_EDITED,
                        actor_user_id=row['user_id'],
                        payload={
                            'message_id': row['message_id'],
                            'preview': row['preview'],
                            'reason': 'stream_status_updated',
                            'stream_status': next_status,
                        },
                        dedupe_suffix=f"stream_status_updated:{row['message_id']}:{next_status}",
                    )
            return len(changed_rows)
        except Exception as e:
            logger.error(f"Failed to update stream attachment status for {sid}: {e}", exc_info=True)
            return 0

    def update_message_expiry(self, message_id: str, user_id: str,
                              expires_at: Optional[Any] = None,
                              ttl_seconds: Optional[int] = None,
                              ttl_mode: Optional[str] = None,
                              allow_admin: bool = False) -> Optional[datetime]:
        """Update a channel message expiry (author or admin). Returns new expiry."""
        try:
            with self.db.get_connection() as conn:
                row = conn.execute(
                    "SELECT user_id FROM channel_messages WHERE id = ?",
                    (message_id,)
                ).fetchone()
                if not row or (row['user_id'] != user_id and not allow_admin):
                    logger.warning(f"User {user_id} cannot update expiry for message {message_id}")
                    return None

                base_time = datetime.now(timezone.utc)
                expires_dt = self._resolve_expiry(
                    expires_at=expires_at,
                    ttl_seconds=ttl_seconds,
                    ttl_mode=ttl_mode,
                    apply_default=False,
                    base_time=base_time,
                )
                expires_db = self._format_db_timestamp(expires_dt) if expires_dt else None
                ttl_sec_db = int(ttl_seconds) if ttl_seconds is not None else None
                ttl_mode_db = (ttl_mode or '').strip() or None

                conn.execute(
                    "UPDATE channel_messages SET expires_at = ?, ttl_seconds = ?, ttl_mode = ? WHERE id = ?",
                    (expires_db, ttl_sec_db, ttl_mode_db, message_id)
                )
                conn.commit()
                return expires_dt
        except Exception as e:
            logger.error(f"Failed to update channel message expiry: {e}")
            return None
    
    # ------------------------------------------------------------------ #
    # Channel role & membership management                                 #
    # ------------------------------------------------------------------ #

    def get_member_role(self, channel_id: str, user_id: str) -> Optional[str]:
        """Return the role of a user in a channel ('admin' | 'member'), or None."""
        decision = self.get_channel_access_decision(
            channel_id=channel_id,
            user_id=user_id,
            require_membership=True,
        )
        if not decision.get('allowed'):
            return None
        role = decision.get('role')
        return str(role) if role else None

    def mark_channel_read(self, channel_id: str, user_id: str) -> None:
        """Update last_read_at for a user in a channel to now, clearing its unread count."""
        try:
            with self.db.get_connection() as conn:
                unread_exists = conn.execute(
                    """
                    SELECT 1
                    FROM channel_members cm
                    WHERE cm.channel_id = ? AND cm.user_id = ?
                      AND EXISTS (
                            SELECT 1
                            FROM channel_messages unread
                            WHERE unread.channel_id = cm.channel_id
                              AND (unread.expires_at IS NULL OR unread.expires_at > CURRENT_TIMESTAMP)
                              AND (cm.last_read_at IS NULL OR unread.created_at > cm.last_read_at)
                            LIMIT 1
                      )
                    LIMIT 1
                    """,
                    (channel_id, user_id),
                ).fetchone()
                if not unread_exists:
                    return
                conn.execute(
                    """UPDATE channel_members SET last_read_at = CURRENT_TIMESTAMP
                       WHERE channel_id = ? AND user_id = ?""",
                    (channel_id, user_id)
                )
                conn.commit()
            self._emit_channel_user_event(
                channel_id=channel_id,
                event_type=EVENT_CHANNEL_MESSAGE_READ,
                actor_user_id=user_id,
                target_user_ids=[user_id],
                payload={"reason": "channel_read"},
                dedupe_suffix=f"channel_read:{user_id}",
            )
        except Exception as e:
            logger.warning(f"Failed to mark channel {channel_id} as read for {user_id}: {e}")

    def is_channel_admin(self, channel_id: str, user_id: str) -> bool:
        """Check if a user is an admin (or creator) of a channel."""
        role = self.get_member_role(channel_id, user_id)
        if role == 'admin':
            return True
        # Channel creator is always treated as admin
        try:
            with self.db.get_connection() as conn:
                row = conn.execute(
                    "SELECT created_by FROM channels WHERE id = ?",
                    (channel_id,)).fetchone()
                if not row:
                    return False
                return cast(str, row['created_by']) == user_id
        except Exception:
            return False

    def set_member_role(self, channel_id: str, target_user_id: str,
                        new_role: str, requester_id: str) -> bool:
        """Change a member's role. Only admins can do this."""
        if new_role not in ('admin', 'member'):
            return False
        if not self.is_channel_admin(channel_id, requester_id):
            logger.warning(f"Role change denied: {requester_id} is not admin of {channel_id}")
            return False
        try:
            with self.db.get_connection() as conn:
                cur = conn.execute(
                    "UPDATE channel_members SET role = ? WHERE channel_id = ? AND user_id = ?",
                    (new_role, channel_id, target_user_id))
                conn.commit()
                return cast(int, cur.rowcount) > 0
        except Exception as e:
            logger.error(f"Failed to set role: {e}")
            return False

    def add_member(self, channel_id: str, target_user_id: str,
                   requester_id: str, role: str = 'member') -> bool:
        """Add a user to a channel. Requester must be admin for guarded/targeted channels."""
        try:
            with self.db.get_connection() as conn:
                ch = conn.execute(
                    "SELECT channel_type, privacy_mode FROM channels WHERE id = ?",
                    (channel_id,)).fetchone()
                if not ch:
                    return False

                channel_type = ch['channel_type']
                privacy_mode = ch['privacy_mode'] if 'privacy_mode' in ch.keys() else 'open'

                # SECURITY: Guarded/private/confidential channels require admin to add members
                requires_admin = (
                    channel_type == 'private' or
                    (privacy_mode and privacy_mode.lower() in {'guarded', 'private', 'confidential'})
                )
                if requires_admin:
                    if not self.is_channel_admin(channel_id, requester_id):
                        logger.warning(
                            f"SECURITY: Add member denied for {channel_type}/{privacy_mode} channel: "
                            f"{requester_id} not admin of {channel_id}"
                        )
                        return False

                # SECURITY: Validate that target user exists
                user_check = conn.execute(
                    "SELECT id FROM users WHERE id = ?", (target_user_id,)
                ).fetchone()
                if not user_check:
                    logger.warning(
                        f"SECURITY: Add member denied: user {target_user_id} does not exist"
                    )
                    return False

                target_policy = self._load_user_channel_governance(conn, target_user_id)
                allowed, reason = self._is_channel_allowed_by_policy(
                    policy=target_policy,
                    channel_id=channel_id,
                    channel_type=channel_type,
                    privacy_mode=privacy_mode,
                )
                if not allowed:
                    logger.warning(
                        f"SECURITY: Add member denied by governance policy for user={target_user_id}, "
                        f"channel={channel_id}, reason={reason}"
                    )
                    return False

                conn.execute(
                    "INSERT OR IGNORE INTO channel_members (channel_id, user_id, role) VALUES (?, ?, ?)",
                    (channel_id, target_user_id, role))
                conn.commit()
                logger.info(f"Added member {target_user_id} to channel {channel_id} by {requester_id}")
                current_member_count = 0
                try:
                    member_row = conn.execute(
                        "SELECT COUNT(*) AS count FROM channel_members WHERE channel_id = ?",
                        (channel_id,),
                    ).fetchone()
                    if member_row:
                        current_member_count = int(
                            member_row['count'] if hasattr(member_row, 'keys') else member_row[0]
                        )
                except Exception:
                    current_member_count = 0
                self._emit_channel_user_event(
                    channel_id=channel_id,
                    event_type=EVENT_CHANNEL_STATE_UPDATED,
                    actor_user_id=requester_id,
                    target_user_ids=[target_user_id, requester_id],
                    payload={
                        "reason": "member_added",
                        "member_user_id": target_user_id,
                        "role": role,
                        "member_count": current_member_count,
                    },
                    dedupe_suffix=f"member_added:{target_user_id}:{current_member_count}",
                )
                return True
        except Exception as e:
            logger.error(f"Failed to add member: {e}")
            return False

    def remove_member(self, channel_id: str, target_user_id: str,
                      requester_id: str) -> bool:
        """Remove a user from a channel. Admin or self-removal allowed."""
        is_self = target_user_id == requester_id
        if not is_self and not self.is_channel_admin(channel_id, requester_id):
            logger.warning(f"Remove member denied: {requester_id} not admin")
            return False
        try:
            with self.db.get_connection() as conn:
                cur = conn.execute(
                    "DELETE FROM channel_members WHERE channel_id = ? AND user_id = ?",
                    (channel_id, target_user_id))
                current_member_count = 0
                try:
                    member_row = conn.execute(
                        "SELECT COUNT(*) AS count FROM channel_members WHERE channel_id = ?",
                        (channel_id,),
                    ).fetchone()
                    if member_row:
                        current_member_count = int(
                            member_row['count'] if hasattr(member_row, 'keys') else member_row[0]
                        )
                except Exception:
                    current_member_count = 0
                conn.commit()
                removed = cast(int, cur.rowcount) > 0
            if removed:
                self._emit_channel_user_event(
                    channel_id=channel_id,
                    event_type=EVENT_CHANNEL_STATE_UPDATED,
                    actor_user_id=requester_id,
                    target_user_ids=[target_user_id, requester_id],
                    payload={
                        "reason": "member_removed",
                        "member_user_id": target_user_id,
                        "member_count": current_member_count,
                    },
                    dedupe_suffix=f"member_removed:{target_user_id}:{current_member_count}",
                )
            return removed
        except Exception as e:
            logger.error(f"Failed to remove member: {e}")
            return False

    def get_member_peer_ids(self, channel_id: str, local_peer_id: Optional[str] = None) -> set:
        """Return the set of peer IDs that have at least one member in this channel.

        Queries channel_members JOIN users to get distinct origin_peer values.
        Always includes the local peer (users with NULL origin_peer are local).
        """
        peers = set()
        try:
            with self.db.get_connection() as conn:
                rows = conn.execute("""
                    SELECT DISTINCT u.origin_peer
                    FROM channel_members cm
                    JOIN users u ON cm.user_id = u.id
                    WHERE cm.channel_id = ?
                """, (channel_id,)).fetchall()
                for r in rows:
                    op = r['origin_peer'] if isinstance(r, dict) or hasattr(r, 'keys') else r[0]
                    if op:
                        peers.add(str(op))
        except Exception as e:
            logger.error(f"Failed to get member peer IDs for {channel_id}: {e}")
        # Always include local peer
        if local_peer_id:
            peers.add(local_peer_id)
        return peers

    def get_channel_members_list(self, channel_id: str) -> List[Dict[str, Any]]:
        """Get all members of a channel with their roles."""
        try:
            with self.db.get_connection() as conn:
                rows = conn.execute("""
                    SELECT cm.user_id, cm.role, cm.joined_at,
                           u.username, u.display_name
                    FROM channel_members cm
                    LEFT JOIN users u ON cm.user_id = u.id
                    WHERE cm.channel_id = ?
                    ORDER BY cm.role DESC, cm.joined_at ASC
                """, (channel_id,)).fetchall()
                return [{
                    'user_id': r['user_id'],
                    'role': r['role'],
                    'joined_at': r['joined_at'],
                    'username': r['username'],
                    'display_name': r['display_name'] or r['username'],
                } for r in rows]
        except Exception as e:
            logger.error(f"Failed to get channel members: {e}")
            return []

    def get_private_channel_recovery_payload(
        self,
        query_user_ids: List[str],
        requester_peer_id: str,
        limit: int = 200,
        max_members_per_channel: int = 200,
    ) -> Dict[str, Any]:
        """Return private/confidential channels relevant to querying peer users."""
        requester = str(requester_peer_id or '').strip()
        if not requester:
            return {'channels': [], 'truncated': False, 'queried_users': []}

        user_ids: List[str] = []
        seen_users = set()
        for uid in query_user_ids or []:
            u = str(uid or '').strip()
            if not u or u in seen_users:
                continue
            seen_users.add(u)
            user_ids.append(u)
        if not user_ids:
            return {'channels': [], 'truncated': False, 'queried_users': []}

        try:
            with self.db.get_connection() as conn:
                user_placeholders = ','.join('?' for _ in user_ids)
                valid_rows = conn.execute(
                    f"""
                    SELECT id
                    FROM users
                    WHERE id IN ({user_placeholders})
                      AND origin_peer = ?
                    """,
                    tuple(user_ids) + (requester,),
                ).fetchall()
                valid_user_ids = [
                    str(row['id'] if hasattr(row, 'keys') and 'id' in row.keys() else row[0])
                    for row in (valid_rows or [])
                ]
                if not valid_user_ids:
                    return {'channels': [], 'truncated': False, 'queried_users': []}

                valid_placeholders = ','.join('?' for _ in valid_user_ids)
                channel_rows = conn.execute(
                    f"""
                    SELECT c.id, c.name, c.channel_type, c.description, c.origin_peer,
                           c.created_by, c.created_at,
                           COALESCE(c.privacy_mode, 'open') AS privacy_mode,
                           COALESCE(c.crypto_mode, '{self.CRYPTO_MODE_LEGACY}') AS crypto_mode,
                           MAX(cm.joined_at) AS membership_joined_at
                    FROM channels c
                    JOIN channel_members cm ON cm.channel_id = c.id
                    WHERE cm.user_id IN ({valid_placeholders})
                      AND (
                        COALESCE(c.privacy_mode, 'open') IN ('private', 'confidential')
                        OR c.channel_type = 'private'
                      )
                      AND NOT EXISTS (
                        SELECT 1
                        FROM channel_member_sync_deliveries d
                        WHERE d.channel_id = c.id
                          AND d.target_user_id = cm.user_id
                          AND d.target_peer_id = ?
                          AND d.action = 'remove'
                          AND d.acked_at IS NULL
                          AND d.delivery_state IN ('pending', 'sent', 'failed')
                      )
                    GROUP BY c.id, c.name, c.channel_type, c.description,
                             c.origin_peer, c.created_by, c.created_at,
                             COALESCE(c.privacy_mode, 'open'),
                             COALESCE(c.crypto_mode, '{self.CRYPTO_MODE_LEGACY}')
                    ORDER BY COALESCE(MAX(cm.joined_at), c.created_at) DESC
                    LIMIT ?
                    """,
                    tuple(valid_user_ids) + (requester, int(limit) + 1),
                ).fetchall()

                truncated = len(channel_rows) > int(limit)
                channel_rows = channel_rows[: int(limit)]
                channels: List[Dict[str, Any]] = []
                member_limit = max(1, int(max_members_per_channel))
                for row in channel_rows:
                    channel_id = str(row['id'])
                    member_rows = conn.execute(
                        """
                        SELECT cm.user_id, cm.role, cm.joined_at,
                               u.origin_peer, u.username, u.display_name
                        FROM channel_members cm
                        LEFT JOIN users u ON cm.user_id = u.id
                        WHERE cm.channel_id = ?
                        ORDER BY cm.role DESC, cm.joined_at ASC
                        LIMIT ?
                        """,
                        (channel_id, member_limit),
                    ).fetchall()
                    members = []
                    for mrow in member_rows or []:
                        members.append({
                            'user_id': mrow['user_id'],
                            'role': mrow['role'] or 'member',
                            'origin_peer': mrow['origin_peer'],
                            'username': mrow['username'],
                            'display_name': mrow['display_name'] or mrow['username'],
                            'joined_at': mrow['joined_at'],
                        })

                    channels.append({
                        'channel_id': channel_id,
                        'name': row['name'],
                        'channel_type': row['channel_type'],
                        'description': row['description'] or '',
                        'origin_peer': row['origin_peer'],
                        'created_by_user_id': row['created_by'],
                        'privacy_mode': row['privacy_mode'] or 'private',
                        'crypto_mode': row['crypto_mode'] or self.CRYPTO_MODE_LEGACY,
                        'members': members,
                    })

                return {
                    'channels': channels,
                    'truncated': truncated,
                    'queried_users': valid_user_ids,
                }
        except Exception as e:
            logger.error(
                f"Failed to build private channel recovery payload for peer {requester}: {e}",
                exc_info=True,
            )
            return {'channels': [], 'truncated': False, 'queried_users': []}

    def delete_message(self, channel_id: str, message_id: str, user_id: str,
                       allow_admin: bool = False) -> bool:
        """Delete a channel message. Only the author can delete (or channel admin if allow_admin)."""
        try:
            with self.db.get_connection() as conn:
                cursor = conn.execute(
                    "SELECT user_id FROM channel_messages WHERE id = ? AND channel_id = ?",
                    (message_id, channel_id),
                )
                row = cursor.fetchone()
                if not row:
                    return False
                if row['user_id'] != user_id and not allow_admin:
                    logger.warning(f"User {user_id} cannot delete message {message_id}")
                    return False
                # Remove FK references: likes and parent_message_id on other messages
                conn.execute("DELETE FROM likes WHERE message_id = ?", (message_id,))
                conn.execute(
                    "UPDATE channel_messages SET parent_message_id = NULL WHERE parent_message_id = ?",
                    (message_id,),
                )
                conn.execute("DELETE FROM channel_messages WHERE id = ?", (message_id,))
                conn.commit()
                logger.info(f"Deleted channel message {message_id} from {channel_id}")
                self._emit_channel_user_event(
                    channel_id=channel_id,
                    event_type=EVENT_CHANNEL_MESSAGE_DELETED,
                    actor_user_id=user_id,
                    payload={
                        "message_id": message_id,
                    },
                    dedupe_suffix=message_id,
                )
                return True
        except Exception as e:
            logger.error(f"Failed to delete channel message: {e}")
            return False

    def delete_channel(self, channel_id: str, requester_id: str, *, force: bool = False) -> bool:
        """Delete a channel. Only channel admins can do this unless *force* is True (node-level admin)."""
        if not force and not self.is_channel_admin(channel_id, requester_id):
            logger.warning(f"Delete denied: {requester_id} not admin of {channel_id}")
            return False
        try:
            target_user_ids = self._channel_member_user_ids(channel_id)
            with self.db.get_connection() as conn:
                conn.execute(
                    "UPDATE channel_messages SET parent_message_id = NULL WHERE channel_id = ?",
                    (channel_id,),
                )
                conn.execute("DELETE FROM likes WHERE message_id IN (SELECT id FROM channel_messages WHERE channel_id = ?)", (channel_id,))
                conn.execute("DELETE FROM channel_messages WHERE channel_id = ?", (channel_id,))
                conn.execute("DELETE FROM channel_members WHERE channel_id = ?", (channel_id,))
                conn.execute("DELETE FROM channels WHERE id = ?", (channel_id,))
                conn.commit()
                logger.info(f"Channel {channel_id} deleted by {requester_id}")
            self._emit_channel_user_event(
                channel_id=channel_id,
                event_type=EVENT_CHANNEL_STATE_UPDATED,
                actor_user_id=requester_id,
                target_user_ids=target_user_ids,
                payload={"reason": "channel_deleted"},
                dedupe_suffix="channel_deleted",
            )
            return True
        except Exception as e:
                logger.error(f"Failed to delete channel: {e}")
                return False

    def purge_expired_channel_messages(self) -> List[Dict[str, Any]]:
        """Remove expired channel messages and related likes."""
        purged: List[Dict[str, Any]] = []
        try:
            with self.db.get_connection() as conn:
                rows = conn.execute("""
                    SELECT id, user_id, channel_id, expires_at, attachments
                    FROM channel_messages
                    WHERE expires_at IS NOT NULL
                      AND expires_at <= CURRENT_TIMESTAMP
                """).fetchall()

                if not rows:
                    return purged

                message_ids = [row['id'] for row in rows]
                purged = []
                for row in rows:
                    attachment_ids: List[str] = []
                    if row['attachments']:
                        try:
                            parsed = json.loads(row['attachments'])
                            if isinstance(parsed, list):
                                for att in parsed:
                                    file_id = att.get('id') if isinstance(att, dict) else None
                                    if file_id:
                                        attachment_ids.append(file_id)
                        except Exception:
                            pass
                    purged.append({
                        'id': row['id'],
                        'user_id': row['user_id'],
                        'channel_id': row['channel_id'],
                        'expires_at': row['expires_at'],
                        'attachment_ids': attachment_ids,
                    })

                # Cleanup likes table if present
                likes_table = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='likes'"
                ).fetchone()
                if likes_table:
                    placeholders = ",".join("?" for _ in message_ids)
                    conn.execute(
                        f"DELETE FROM likes WHERE message_id IN ({placeholders})",
                        message_ids,
                    )

                # Null out parent_message_id in any message that references an expired one
                # (avoids FOREIGN KEY constraint on parent_message_id -> channel_messages.id)
                placeholders = ",".join("?" for _ in message_ids)
                conn.execute(
                    f"UPDATE channel_messages SET parent_message_id = NULL "
                    f"WHERE parent_message_id IN ({placeholders})",
                    message_ids,
                )
                conn.execute(
                    f"DELETE FROM channel_messages WHERE id IN ({placeholders})",
                    message_ids,
                )
                conn.commit()

                logger.info(f"Purged {len(message_ids)} expired channel messages")
        except Exception as e:
            logger.error(f"Failed to purge expired channel messages: {e}", exc_info=True)

        return purged

    def get_channel_messages(self, channel_id: str, user_id: str, 
                           limit: int = 50, before_message_id: Optional[str] = None) -> List[Message]:
        """Get messages from a channel."""
        logger.debug(f"Getting messages for channel {channel_id}, user {user_id}, limit {limit}")
        
        try:
            access = self.get_channel_access_decision(
                channel_id=channel_id,
                user_id=user_id,
                require_membership=True,
            )
            if not access.get('allowed'):
                logger.warning(
                    f"Channel read denied for user={user_id}, channel={channel_id}, "
                    f"reason={access.get('reason')}"
                )
                return []

            with self.db.get_connection() as conn:
                # Build query — sort root messages by activity time
                # (resurfaced parents appear at the bottom/newest position).
                # Replies sort with their parent via COALESCE on the parent row.
                sort_expr = """
                    CASE
                        WHEN m.parent_message_id IS NOT NULL THEN
                            COALESCE(
                                (SELECT p.last_activity_at FROM channel_messages p WHERE p.id = m.parent_message_id),
                                (SELECT p.created_at FROM channel_messages p WHERE p.id = m.parent_message_id)
                            )
                        ELSE COALESCE(m.last_activity_at, m.created_at)
                    END
                """

                before_sort_expr = """
                    CASE
                        WHEN b.parent_message_id IS NOT NULL THEN
                            COALESCE(
                                (SELECT p.last_activity_at FROM channel_messages p WHERE p.id = b.parent_message_id),
                                (SELECT p.created_at FROM channel_messages p WHERE p.id = b.parent_message_id)
                            )
                        ELSE COALESCE(b.last_activity_at, b.created_at)
                    END
                """

                query = f"""
                    SELECT m.*, u.username as author_username,
                           {sort_expr} AS sort_time
                    FROM channel_messages m
                    LEFT JOIN users u ON m.user_id = u.id
                    WHERE m.channel_id = ?
                      AND (m.expires_at IS NULL OR m.expires_at > CURRENT_TIMESTAMP)
                """
                params: List[Any] = [channel_id]
                
                if before_message_id:
                    # Pagination must use the same sort tuple as ORDER BY to
                    # avoid gaps/duplicates when old threads are resurfaced by
                    # new replies.
                    query += f"""
                        AND (
                            {sort_expr} < (
                                SELECT {before_sort_expr}
                                FROM channel_messages b
                                WHERE b.id = ?
                            )
                            OR (
                                {sort_expr} = (
                                    SELECT {before_sort_expr}
                                    FROM channel_messages b
                                    WHERE b.id = ?
                                )
                                AND m.created_at < (
                                    SELECT b.created_at
                                    FROM channel_messages b
                                    WHERE b.id = ?
                                )
                            )
                        )
                    """
                    params.extend([
                        before_message_id,
                        before_message_id,
                        before_message_id,
                    ])
                
                query += " ORDER BY sort_time DESC, m.created_at DESC LIMIT ?"
                params.append(limit)
                
                cursor = conn.execute(query, params)
                rows = cursor.fetchall()
                
                def _row_to_message(row: Any, row_type: str = "message") -> Optional[Message]:
                    """Best-effort row parser used for primary query rows and recursively fetched parents."""
                    try:
                        try:
                            msg_type = MessageType(row['message_type'])
                        except (ValueError, KeyError):
                            msg_type = MessageType.TEXT

                        created_at_raw = row['created_at'] or ''
                        try:
                            created_at = datetime.fromisoformat(created_at_raw.replace('Z', '+00:00'))
                        except (ValueError, AttributeError):
                            try:
                                created_at = datetime.strptime(created_at_raw, '%Y-%m-%d %H:%M:%S')
                            except Exception:
                                created_at = datetime.now()

                        edited_at = None
                        if row['edited_at']:
                            try:
                                edited_at = datetime.fromisoformat(row['edited_at'].replace('Z', '+00:00'))
                            except (ValueError, AttributeError):
                                pass

                        expires_at = self._parse_datetime(row['expires_at']) if 'expires_at' in row.keys() else None
                        content_text = row['content'] or ''
                        try:
                            crypto_state = (row['crypto_state'] or '').strip().lower()
                        except Exception:
                            crypto_state = ''
                        if not content_text and crypto_state == 'pending_decrypt':
                            content_text = '[Encrypted message pending key]'
                        elif not content_text and crypto_state == 'decrypt_failed':
                            content_text = '[Encrypted message could not be decrypted]'

                        return Message(
                            id=row['id'],
                            channel_id=row['channel_id'],
                            user_id=row['user_id'],
                            content=content_text,
                            message_type=msg_type,
                            created_at=created_at,
                            thread_id=row['thread_id'],
                            parent_message_id=row['parent_message_id'],
                            reactions=json.loads(row['reactions']) if row['reactions'] else None,
                            attachments=json.loads(row['attachments']) if row['attachments'] else None,
                            security=json.loads(row['security']) if row['security'] else None,
                            edited_at=edited_at,
                            expires_at=expires_at,
                            origin_peer=row['origin_peer'] if 'origin_peer' in row.keys() else None,
                            crypto_state=row['crypto_state'] if 'crypto_state' in row.keys() else None,
                        )
                    except Exception as row_err:
                        row_id = '?'
                        try:
                            row_id = row['id'] if 'id' in row.keys() else '?'
                        except Exception:
                            row_id = '?'
                        logger.warning(f"Skipping corrupt {row_type} row {row_id}: {row_err}")
                        return None

                # Convert to Message objects (skip corrupt rows gracefully)
                messages: List[Message] = []
                for row in rows:
                    message = _row_to_message(row, "message")
                    if message:
                        messages.append(message)
                
                # Reverse to get chronological order
                messages.reverse()

                # Include any missing parent/ancestor messages so replies render under the correct post.
                # This walks parent chains recursively; one-level hydration can orphan deep reply chains.
                msg_ids = {m.id for m in messages}
                missing_parent_ids = {
                    m.parent_message_id
                    for m in messages
                    if m.parent_message_id and m.parent_message_id not in msg_ids
                }
                while missing_parent_ids:
                    placeholders = ",".join("?" * len(missing_parent_ids))
                    parent_rows = conn.execute(
                        f"""
                        SELECT m.*, u.username as author_username
                        FROM channel_messages m
                        LEFT JOIN users u ON m.user_id = u.id
                        WHERE m.channel_id = ? AND m.id IN ({placeholders})
                          AND (m.expires_at IS NULL OR m.expires_at > CURRENT_TIMESTAMP)
                        """,
                        [channel_id] + list(missing_parent_ids),
                    ).fetchall()
                    if not parent_rows:
                        break

                    next_missing_parent_ids = set()
                    for row in parent_rows:
                        message = _row_to_message(row, "parent")
                        if not message or message.id in msg_ids:
                            continue
                        messages.append(message)
                        msg_ids.add(message.id)
                        if message.parent_message_id and message.parent_message_id not in msg_ids:
                            next_missing_parent_ids.add(message.parent_message_id)
                    # Keep original page ordering stable for pagination cursors and only append ancestors.
                    missing_parent_ids = next_missing_parent_ids

                logger.debug(f"Retrieved {len(messages)} messages from channel {channel_id}")
                return messages
                
        except Exception as e:
            logger.error(f"Failed to get channel messages: {e}", exc_info=True)
            return []

    def get_channel_message(
        self, channel_id: str, message_id: str, user_id: str
    ) -> Optional[Message]:
        """Get a single channel message by id. Returns None if not found or user not a member."""
        if not channel_id or not message_id or not user_id:
            return None
        try:
            access = self.get_channel_access_decision(
                channel_id=channel_id,
                user_id=user_id,
                require_membership=True,
            )
            if not access.get('allowed'):
                return None
            with self.db.get_connection() as conn:
                row = conn.execute(
                    """
                    SELECT * FROM channel_messages
                    WHERE channel_id = ? AND id = ?
                      AND (expires_at IS NULL OR expires_at > CURRENT_TIMESTAMP)
                    """,
                    (channel_id, message_id),
                ).fetchone()
                if not row:
                    return None
                try:
                    msg_type = MessageType(row['message_type'])
                except (ValueError, KeyError):
                    msg_type = MessageType.TEXT
                created_at_raw = row['created_at'] or ''
                try:
                    created_at = datetime.fromisoformat(created_at_raw.replace('Z', '+00:00'))
                except (ValueError, AttributeError):
                    try:
                        created_at = datetime.strptime(created_at_raw, '%Y-%m-%d %H:%M:%S')
                    except Exception:
                        created_at = datetime.now()
                edited_at = None
                if row.get('edited_at'):
                    try:
                        edited_at = datetime.fromisoformat(str(row['edited_at']).replace('Z', '+00:00'))
                    except (ValueError, AttributeError):
                        pass
                expires_at = self._parse_datetime(row['expires_at']) if row.get('expires_at') else None
                return Message(
                    id=row['id'],
                    channel_id=row['channel_id'],
                    user_id=row['user_id'],
                    content=row['content'] or '',
                    message_type=msg_type,
                    created_at=created_at,
                    thread_id=row.get('thread_id'),
                    parent_message_id=row.get('parent_message_id'),
                    reactions=json.loads(row['reactions']) if row.get('reactions') else None,
                    attachments=json.loads(row['attachments']) if row.get('attachments') else None,
                    security=json.loads(row['security']) if row.get('security') else None,
                    edited_at=edited_at,
                    expires_at=expires_at,
                    origin_peer=row.get('origin_peer'),
                    crypto_state=row.get('crypto_state'),
                )
        except Exception as e:
            logger.error(f"Failed to get channel message {message_id}: {e}", exc_info=True)
            return None

    def get_channel_activity_since(self, user_id: str, since: datetime, limit: int = 50) -> List[Dict[str, Any]]:
        """Return per-channel activity since a timestamp (counts + latest preview)."""
        if not user_id or not since:
            return []

        try:
            limit_val = max(1, min(int(limit or 50), 200))
        except Exception:
            limit_val = 50

        since_db = self._format_db_timestamp(since)
        results: List[Dict[str, Any]] = []

        try:
            with self.db.get_connection() as conn:
                policy = self._load_user_channel_governance(conn, user_id)
                rows = conn.execute(
                    """
                    SELECT m.channel_id, c.name as channel_name,
                           c.channel_type,
                           COALESCE(c.privacy_mode, 'open') AS privacy_mode,
                           COUNT(*) as new_messages,
                           MAX(m.created_at) as latest_at
                    FROM channel_messages m
                    JOIN channel_members cm ON m.channel_id = cm.channel_id
                    JOIN channels c ON c.id = m.channel_id
                    WHERE cm.user_id = ?
                      AND m.created_at > ?
                      AND (m.expires_at IS NULL OR m.expires_at > CURRENT_TIMESTAMP)
                    GROUP BY m.channel_id
                    ORDER BY latest_at DESC
                    LIMIT ?
                    """,
                    (user_id, since_db, limit_val),
                ).fetchall()

                for row in rows:
                    channel_id = row['channel_id']
                    allowed, _ = self._is_channel_allowed_by_policy(
                        policy=policy,
                        channel_id=channel_id,
                        channel_type=row['channel_type'],
                        privacy_mode=row['privacy_mode'],
                    )
                    if not allowed:
                        continue
                    latest = conn.execute(
                        """
                        SELECT m.id, m.user_id, m.content, m.created_at, u.username as author_username
                        FROM channel_messages m
                        LEFT JOIN users u ON m.user_id = u.id
                        WHERE m.channel_id = ?
                          AND m.created_at > ?
                          AND (m.expires_at IS NULL OR m.expires_at > CURRENT_TIMESTAMP)
                        ORDER BY m.created_at DESC
                        LIMIT 1
                        """,
                        (channel_id, since_db),
                    ).fetchone()
                    latest_preview = ''
                    latest_author_id = None
                    latest_author_username = None
                    latest_message_id = None
                    latest_at = row['latest_at']
                    if latest:
                        latest_message_id = latest['id']
                        latest_author_id = latest['user_id']
                        latest_author_username = latest['author_username']
                        latest_preview = latest['content'] or ''
                        latest_at = latest['created_at'] or latest_at

                    try:
                        from .mentions import build_preview
                        latest_preview = build_preview(latest_preview or '')
                    except Exception:
                        pass

                    results.append({
                        'channel_id': channel_id,
                        'channel_name': row['channel_name'],
                        'new_messages': row['new_messages'],
                        'latest_at': latest_at,
                        'latest_message_id': latest_message_id,
                        'latest_author_id': latest_author_id,
                        'latest_author_username': latest_author_username,
                        'latest_preview': latest_preview,
                    })
            return results
        except Exception as e:
            logger.error(f"Failed to get channel activity: {e}", exc_info=True)
            return []
    
    def get_user_channels(self, user_id: str) -> List[Channel]:
        """Get all channels for a user."""
        logger.debug(f"Getting channels for user {user_id}")
        
        try:
            self._maybe_run_channel_lifecycle_scan()
            with self.db.get_connection() as conn:
                policy = self._load_user_channel_governance(conn, user_id)
                cursor = conn.execute("""
                    SELECT c.*, cm.last_read_at, cm.notifications_enabled, cm.role as user_role,
                           COUNT(DISTINCT cm2.user_id) as member_count,
                           MAX(msg.created_at) as last_message_at,
                           COALESCE(c.last_activity_at, c.created_at) AS channel_last_activity_at,
                           COALESCE(c.lifecycle_ttl_days, ?) AS lifecycle_ttl_days,
                           COALESCE(c.lifecycle_preserved, 0) AS lifecycle_preserved,
                           c.lifecycle_archived_at,
                           c.lifecycle_archive_reason,
                           (SELECT COUNT(*)
                            FROM channel_messages unread
                            WHERE unread.channel_id = c.id
                              AND (unread.expires_at IS NULL OR unread.expires_at > CURRENT_TIMESTAMP)
                              AND (cm.last_read_at IS NULL OR unread.created_at > cm.last_read_at)
                           ) as unread_count
                    FROM channels c
                    INNER JOIN channel_members cm ON c.id = cm.channel_id AND cm.user_id = ?
                    LEFT JOIN channel_members cm2 ON c.id = cm2.channel_id
                    LEFT JOIN channel_messages msg
                        ON c.id = msg.channel_id
                       AND (msg.expires_at IS NULL OR msg.expires_at > CURRENT_TIMESTAMP)
                    GROUP BY c.id, cm.last_read_at, cm.notifications_enabled, cm.role
                    ORDER BY CASE WHEN c.lifecycle_archived_at IS NULL THEN 0 ELSE 1 END,
                             COALESCE(c.last_activity_at, last_message_at, c.created_at) DESC
                """, (self.DEFAULT_CHANNEL_LIFECYCLE_DAYS, user_id))
                
                channels = []
                for row in cursor.fetchall():
                    allowed, reason = self._is_channel_allowed_by_policy(
                        policy=policy,
                        channel_id=row['id'],
                        channel_type=row['channel_type'],
                        privacy_mode=(row['privacy_mode'] if 'privacy_mode' in row.keys() else 'open'),
                    )
                    if not allowed:
                        logger.debug(
                            f"Skipping channel {row['id']} for user {user_id} due to governance ({reason})"
                        )
                        continue
                    # origin_peer may not exist in older DBs
                    try:
                        origin_peer = row['origin_peer']
                    except (IndexError, KeyError):
                        origin_peer = None
                    # privacy_mode may not exist in older DBs
                    try:
                        privacy_mode = row['privacy_mode'] or 'open'
                    except (IndexError, KeyError):
                        privacy_mode = 'open'
                    try:
                        user_role = row['user_role'] or 'member'
                    except (IndexError, KeyError):
                        user_role = 'member'
                    try:
                        notifications_enabled = bool(row['notifications_enabled'])
                    except (IndexError, KeyError, TypeError):
                        notifications_enabled = True

                    try:
                        unread_count = int(row['unread_count'] or 0)
                    except (IndexError, KeyError, TypeError):
                        unread_count = 0

                    channel = Channel(
                        id=row['id'],
                        name=row['name'],
                        channel_type=ChannelType(row['channel_type']),
                        created_by=row['created_by'],
                        created_at=datetime.fromisoformat(row['created_at'].replace('Z', '+00:00')),
                        description=row['description'],
                        topic=row['topic'],
                        member_count=row['member_count'],
                        last_message_at=datetime.fromisoformat(row['last_message_at'].replace('Z', '+00:00')) if row['last_message_at'] else None,
                        last_activity_at=self._parse_datetime(row['channel_last_activity_at']),
                        origin_peer=origin_peer,
                        privacy_mode=privacy_mode,
                        user_role=user_role,
                        notifications_enabled=notifications_enabled,
                        unread_count=unread_count,
                        crypto_mode=(
                            row['crypto_mode']
                            if 'crypto_mode' in row.keys() and row['crypto_mode']
                            else self.CRYPTO_MODE_LEGACY
                        ),
                        lifecycle_ttl_days=int(row['lifecycle_ttl_days'] or self.DEFAULT_CHANNEL_LIFECYCLE_DAYS),
                        lifecycle_preserved=bool(row['lifecycle_preserved']),
                        archived_at=self._parse_datetime(row['lifecycle_archived_at']),
                        archive_reason=row['lifecycle_archive_reason'],
                    )
                    lifecycle = self.describe_channel_lifecycle(channel)
                    channel.lifecycle_status = str(lifecycle['status'])
                    channel.days_until_archive = lifecycle['days_until_archive']
                    channels.append(channel)
                
                logger.debug(f"Retrieved {len(channels)} channels for user {user_id}")
                return channels
                
        except Exception as e:
            logger.error(f"Failed to get user channels: {e}", exc_info=True)
            return []

    def get_channel_latest_timestamps(self) -> Dict[str, str]:
        """Get the latest message timestamp for every channel.

        Returns:
            Dict mapping channel_id -> latest created_at string,
            using only channels that have at least one message.
        """
        try:
            with self.db.get_connection() as conn:
                rows = conn.execute("""
                    SELECT channel_id, MAX(created_at) as latest
                    FROM channel_messages
                    WHERE expires_at IS NULL OR expires_at > CURRENT_TIMESTAMP
                    GROUP BY channel_id
                """).fetchall()
                result = {row['channel_id']: row['latest'] for row in rows}
                logger.debug(f"Latest timestamps for {len(result)} channels")
                return result
        except Exception as e:
            logger.error(f"Failed to get channel latest timestamps: {e}",
                         exc_info=True)
            return {}

    @staticmethod
    def _stable_json(value: Any) -> str:
        """Serialize JSON deterministically for hashing."""
        return json.dumps(
            value,
            sort_keys=True,
            separators=(',', ':'),
            ensure_ascii=True,
        )

    def _build_merkle_root(self, leaves: List[str]) -> str:
        """Compute a deterministic binary Merkle root from leaf hashes."""
        if not leaves:
            return self.SYNC_DIGEST_EMPTY_ROOT

        level = [str(item).strip().lower() for item in leaves if str(item).strip()]
        if not level:
            return self.SYNC_DIGEST_EMPTY_ROOT

        while len(level) > 1:
            next_level: List[str] = []
            idx = 0
            while idx < len(level):
                left = level[idx]
                right = level[idx + 1] if idx + 1 < len(level) else left
                next_level.append(
                    hashlib.sha256(f"{left}{right}".encode('utf-8')).hexdigest()
                )
                idx += 2
            level = next_level
        return level[0]

    def _canonical_attachment_hash(self, attachments_raw: Any) -> str:
        """Hash attachment metadata using peer-stable fields only."""
        if not attachments_raw:
            return hashlib.sha256(b'[]').hexdigest()
        try:
            parsed = attachments_raw
            if isinstance(attachments_raw, str):
                parsed = json.loads(attachments_raw)
            if not isinstance(parsed, list):
                parsed = []

            canon: List[Dict[str, Any]] = []
            for att in parsed:
                if not isinstance(att, dict):
                    continue
                size_val = att.get('size')
                try:
                    size_val = int(size_val) if size_val is not None else 0
                except Exception:
                    size_val = 0
                canon.append({
                    'name': str(att.get('name') or ''),
                    'type': str(att.get('type') or att.get('content_type') or ''),
                    'size': size_val,
                    'sha256': str(att.get('sha256') or att.get('checksum') or att.get('hash') or ''),
                })

            canon.sort(
                key=lambda item: (
                    item.get('name') or '',
                    item.get('type') or '',
                    item.get('size') or 0,
                    item.get('sha256') or '',
                )
            )
            blob = self._stable_json(canon).encode('utf-8')
            return hashlib.sha256(blob).hexdigest()
        except Exception:
            return hashlib.sha256(b'[]').hexdigest()

    def _channel_message_fingerprint(self, row: Any) -> str:
        """Compute a message-level canonical hash for sync digesting."""
        payload_obj: Dict[str, Any]
        encrypted_content = row['encrypted_content'] if 'encrypted_content' in row.keys() else None
        if encrypted_content:
            payload_obj = {
                'encrypted_content': str(encrypted_content),
                'nonce': str((row['nonce'] if 'nonce' in row.keys() else '') or ''),
                'key_id': str((row['key_id'] if 'key_id' in row.keys() else '') or ''),
            }
        else:
            payload_obj = {'content': str(row['content'] or '')}

        payload_hash = hashlib.sha256(
            self._stable_json(payload_obj).encode('utf-8')
        ).hexdigest()
        attachments_hash = self._canonical_attachment_hash(
            row['attachments'] if 'attachments' in row.keys() else None
        )

        envelope = {
            'id': str(row['id'] or ''),
            'created_at': str(row['created_at'] or ''),
            'edited_at': str((row['edited_at'] if 'edited_at' in row.keys() else '') or ''),
            'message_type': str((row['message_type'] if 'message_type' in row.keys() else '') or ''),
            'parent_message_id': str((row['parent_message_id'] if 'parent_message_id' in row.keys() else '') or ''),
            'expires_at': str((row['expires_at'] if 'expires_at' in row.keys() else '') or ''),
            'crypto_state': str((row['crypto_state'] if 'crypto_state' in row.keys() else '') or ''),
            'key_id': str((row['key_id'] if 'key_id' in row.keys() else '') or ''),
            'payload_hash': payload_hash,
            'attachments_hash': attachments_hash,
        }
        return hashlib.sha256(self._stable_json(envelope).encode('utf-8')).hexdigest()

    def compute_channel_sync_digest(self, channel_id: str, conn: Optional[Any] = None) -> Dict[str, Any]:
        """Compute and cache sync digest metadata for one channel."""
        if not channel_id:
            return {
                'root': self.SYNC_DIGEST_EMPTY_ROOT,
                'live_count': 0,
                'max_created_at': None,
            }
        if conn is None:
            with self.db.get_connection() as conn_ctx:
                return self.compute_channel_sync_digest(channel_id, conn=conn_ctx)

        try:
            rows = conn.execute(
                """
                SELECT id, content, created_at, edited_at, message_type, parent_message_id,
                       expires_at, attachments, encrypted_content, crypto_state, key_id, nonce
                FROM channel_messages
                WHERE channel_id = ?
                  AND (expires_at IS NULL OR expires_at > CURRENT_TIMESTAMP)
                ORDER BY id ASC
                """,
                (channel_id,),
            ).fetchall()
            leaves = [self._channel_message_fingerprint(row) for row in rows]
            root_hash = self._build_merkle_root(leaves)
            live_count = len(rows)
            max_created_at = None
            if rows:
                max_created_at = max((row['created_at'] for row in rows if row['created_at']), default=None)

            conn.execute(
                """
                INSERT INTO channel_sync_digests
                    (channel_id, digest_version, root_hash, live_count, max_created_at, computed_at)
                VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(channel_id) DO UPDATE SET
                    digest_version = excluded.digest_version,
                    root_hash = excluded.root_hash,
                    live_count = excluded.live_count,
                    max_created_at = excluded.max_created_at,
                    computed_at = CURRENT_TIMESTAMP
                """,
                (
                    channel_id,
                    self.SYNC_DIGEST_VERSION,
                    root_hash,
                    live_count,
                    max_created_at,
                ),
            )
            conn.commit()

            return {
                'root': root_hash,
                'live_count': int(live_count),
                'max_created_at': max_created_at,
            }
        except Exception as e:
            logger.debug(f"Failed to compute sync digest for channel {channel_id}: {e}")
            return {
                'root': self.SYNC_DIGEST_EMPTY_ROOT,
                'live_count': 0,
                'max_created_at': None,
            }

    def get_channel_sync_digests(
        self,
        channel_ids: Optional[List[str]] = None,
        max_channels: int = 200,
    ) -> Dict[str, Dict[str, Any]]:
        """Return per-channel digest metadata for catch-up optimization."""
        try:
            cap = max(1, int(max_channels or 200))
        except Exception:
            cap = 200

        result: Dict[str, Dict[str, Any]] = {}
        try:
            with self.db.get_connection() as conn:
                ids: List[str] = []
                if channel_ids:
                    seen = set()
                    for raw in channel_ids:
                        cid = str(raw or '').strip()
                        if not cid or cid in seen:
                            continue
                        seen.add(cid)
                        ids.append(cid)
                        if len(ids) >= cap:
                            break
                else:
                    rows = conn.execute(
                        """
                        SELECT channel_id
                        FROM channel_messages
                        WHERE expires_at IS NULL OR expires_at > CURRENT_TIMESTAMP
                        GROUP BY channel_id
                        ORDER BY MAX(created_at) DESC
                        LIMIT ?
                        """,
                        (cap,),
                    ).fetchall()
                    ids = [str(row['channel_id']) for row in rows if row['channel_id']]

                for cid in ids:
                    digest = self.compute_channel_sync_digest(cid, conn=conn)
                    result[cid] = {
                        'root': str(digest.get('root') or self.SYNC_DIGEST_EMPTY_ROOT),
                        'live_count': int(digest.get('live_count') or 0),
                        'max_created_at': digest.get('max_created_at'),
                    }

            return result
        except Exception as e:
            logger.debug(f"Failed to collect channel sync digests: {e}")
            return {}

    def get_messages_since(self, channel_id: str, since_timestamp: str,
                           limit: int = 200) -> List[Dict[str, Any]]:
        """Get messages in a channel created after *since_timestamp*.

        Returns plain dicts (not Message objects) suitable for P2P
        serialisation.  Attachments metadata is included but the
        binary ``data`` field (used for inline file transfer) is
        stripped to keep the payload small.

        Args:
            channel_id: Channel to query
            since_timestamp: Only return messages after this timestamp
            limit: Maximum messages to return (default 200)
        """
        try:
            with self.db.get_connection() as conn:
                rows = conn.execute("""
                    SELECT id, channel_id, user_id, content,
                           message_type, created_at, attachments, expires_at,
                           origin_peer,
                           ttl_seconds, ttl_mode, parent_message_id,
                           encrypted_content, crypto_state, key_id, nonce
                    FROM channel_messages
                    WHERE channel_id = ?
                      AND created_at > ?
                      AND (expires_at IS NULL OR expires_at > CURRENT_TIMESTAMP)
                    ORDER BY created_at ASC
                    LIMIT ?
                """, (channel_id, since_timestamp, limit)).fetchall()

                messages = []
                for row in rows:
                    msg = {
                        'id': row['id'],
                        'channel_id': row['channel_id'],
                        'user_id': row['user_id'],
                        'content': row['content'],
                        'message_type': row['message_type'],
                        'created_at': row['created_at'],
                        'expires_at': row['expires_at'],
                        'origin_peer': row['origin_peer'] if 'origin_peer' in row.keys() else None,
                        'ttl_seconds': row['ttl_seconds'] if 'ttl_seconds' in row.keys() else None,
                        'ttl_mode': row['ttl_mode'] if 'ttl_mode' in row.keys() else None,
                        'parent_message_id': row['parent_message_id'] if 'parent_message_id' in row.keys() else None,
                        'encrypted_content': (
                            row['encrypted_content'] if 'encrypted_content' in row.keys() else None
                        ),
                        'crypto_state': (
                            row['crypto_state'] if 'crypto_state' in row.keys() else None
                        ),
                        'key_id': row['key_id'] if 'key_id' in row.keys() else None,
                        'nonce': row['nonce'] if 'nonce' in row.keys() else None,
                    }
                    # Include attachment metadata but strip heavy data
                    if row['attachments']:
                        try:
                            atts = json.loads(row['attachments'])
                            for att in atts:
                                att.pop('data', None)
                            msg['attachments'] = atts
                        except Exception:
                            pass
                    messages.append(msg)

                logger.debug(f"Catchup: {len(messages)} messages in "
                             f"#{channel_id} since {since_timestamp}")
                return messages
        except Exception as e:
            logger.error(f"Failed to get messages since {since_timestamp} "
                         f"for #{channel_id}: {e}", exc_info=True)
            return []

    # ------------------------------------------------------------------ #
    # Persistent message deduplication                                     #
    # ------------------------------------------------------------------ #

    def is_message_processed(self, message_id: str) -> bool:
        """Check if a message has already been processed (dedup)."""
        if not message_id:
            return False
        try:
            with self.db.get_connection() as conn:
                row = conn.execute(
                    "SELECT 1 FROM processed_messages WHERE message_id = ?",
                    (message_id,)
                ).fetchone()
                return row is not None
        except Exception:
            return False

    def mark_message_processed(self, message_id: str) -> None:
        """Record a message as processed so it won't be stored twice."""
        if not message_id:
            return
        try:
            with self.db.get_connection() as conn:
                conn.execute(
                    "INSERT OR IGNORE INTO processed_messages (message_id) VALUES (?)",
                    (message_id,))
                conn.commit()
        except Exception as e:
            logger.debug(f"Failed to mark message {message_id} as processed: {e}")

    def prune_processed_messages(self, keep_days: int = 7) -> int:
        """Remove old entries from the dedup table to save space."""
        try:
            with self.db.get_connection() as conn:
                cur = conn.execute("""
                    DELETE FROM processed_messages
                    WHERE processed_at < datetime('now', ?)
                """, (f'-{keep_days} days',))
                conn.commit()
                pruned = cast(int, cur.rowcount)
                if pruned:
                    logger.info(f"Pruned {pruned} old dedup records "
                                f"(older than {keep_days} days)")
                return pruned
        except Exception as e:
            logger.error(f"Failed to prune processed_messages: {e}")
            return 0

    # ------------------------------------------------------------------ #
    # Channel message search                                               #
    # ------------------------------------------------------------------ #

    def search_channel_messages(self, channel_id: str, query: str,
                                user_id: str, limit: int = 50) -> List[Message]:
        """Search messages in a channel by content.

        The caller must be a member of the channel.  Uses SQLite LIKE
        for simple substring matching (case-insensitive).
        """
        if not query or not query.strip():
            return []
        try:
            access = self.get_channel_access_decision(
                channel_id=channel_id,
                user_id=user_id,
                require_membership=True,
            )
            if not access.get('allowed'):
                return []
            with self.db.get_connection() as conn:
                search_term = f"%{query.strip()}%"
                rows = conn.execute("""
                    SELECT m.*, u.username as author_username
                    FROM channel_messages m
                    LEFT JOIN users u ON m.user_id = u.id
                    WHERE m.channel_id = ?
                      AND m.content LIKE ?
                      AND (m.expires_at IS NULL OR m.expires_at > CURRENT_TIMESTAMP)
                    ORDER BY m.created_at DESC
                    LIMIT ?
                """, (channel_id, search_term, limit)).fetchall()

                messages = []
                for row in rows:
                    msg = Message(
                        id=row['id'],
                        channel_id=row['channel_id'],
                        user_id=row['user_id'],
                        content=row['content'],
                        message_type=MessageType(row['message_type']),
                        created_at=datetime.fromisoformat(
                            row['created_at'].replace('Z', '+00:00')),
                        thread_id=row['thread_id'],
                        parent_message_id=row['parent_message_id'],
                        reactions=json.loads(row['reactions']) if row['reactions'] else None,
                        attachments=json.loads(row['attachments']) if row['attachments'] else None,
                        edited_at=datetime.fromisoformat(
                            row['edited_at'].replace('Z', '+00:00')) if row['edited_at'] else None,
                        expires_at=self._parse_datetime(row['expires_at']) if 'expires_at' in row.keys() else None,
                    )
                    messages.append(msg)
                return messages
        except Exception as e:
            logger.error(f"Failed to search messages in channel {channel_id}: {e}",
                         exc_info=True)
            return []
