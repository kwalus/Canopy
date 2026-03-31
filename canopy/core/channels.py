"""
Slack-style channel system for Canopy.

Implements channel-based organization similar to Slack, with real-time
messaging, threading, and work-focused communication.

Project: Canopy - Local Mesh Communication
License: Apache 2.0
"""

import logging
import math
import re
import secrets
import json
import hashlib
import sqlite3
import threading
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Any, Union, Tuple, cast, Set
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
from .source_layout import normalize_source_layout
from ..security.api_keys import ApiKeyManager, Permission
from .logging_config import log_performance, LogOperation
from ..network.routing import (
    decode_channel_key_material as decode_channel_key_material_value,
    encode_channel_key_material as encode_channel_key_material_value,
)

logger = logging.getLogger('canopy.channels')


_CHANNEL_REPOST_POLICY_VALUES = {'same_scope', 'deny'}
_CHANNEL_SOURCE_REFERENCE_KIND_VALUES = {'repost_v1', 'variant_v1'}
_CHANNEL_VARIANT_RELATIONSHIP_VALUES = {
    'curated_recomposition',
    'module_variant',
    'parameterized_variant',
}
_CHANNEL_REMOVAL_VOTE_VALUES = {'remove', 'keep'}
_CHANNEL_REMOVAL_OPEN_STATUS = 'open'
_CHANNEL_REMOVAL_RETIRED_STATUS = 'retired'
_CHANNEL_REMOVAL_REJECTED_STATUS = 'rejected'
_CHANNEL_REMOVAL_RESTORED_STATUS = 'restored'
_CHANNEL_REMOVAL_TERMINAL_STATUSES = {
    _CHANNEL_REMOVAL_RETIRED_STATUS,
    _CHANNEL_REMOVAL_REJECTED_STATUS,
    _CHANNEL_REMOVAL_RESTORED_STATUS,
}
_CHANNEL_REPOST_REFERENCE_BODY_MAX_CHARS = 8000
_CHANNEL_REPOST_REFERENCE_ATTACHMENT_IMAGE_CAP = 6
_CHANNEL_REPOST_REFERENCE_YOUTUBE_CAP = 6

# YouTube video ids in message body (aligns with client rich-text embed patterns).
_CHANNEL_YOUTUBE_ID_IN_CONTENT_RE = re.compile(
    r"(?:https?://)?(?:www\.)?(?:youtube\.com/watch\?v=|youtu\.be/|youtube\.com/(?:shorts|live)/)([\w-]{11})\b",
    re.IGNORECASE,
)


def _channel_youtube_embeds_from_content(content: str) -> Dict[str, Any]:
    """Structured link hints when the original is plain text with YouTube URL(s) (no attachments).

    Matches the common case where the channel row shows inline YouTube embeds from ``content``
    but ``message_type`` is TEXT and ``attachments`` is empty — repost cards must still get
    ``has_source_layout`` (Deck) and preview thumbnails for each distinct video (capped).
    """
    text = str(content or "")
    ids_ordered: List[str] = []
    seen: Set[str] = set()
    for match in _CHANNEL_YOUTUBE_ID_IN_CONTENT_RE.finditer(text):
        video_id = str(match.group(1) or "").strip()
        if len(video_id) != 11 or video_id in seen:
            continue
        seen.add(video_id)
        ids_ordered.append(video_id)
        if len(ids_ordered) >= _CHANNEL_REPOST_REFERENCE_YOUTUBE_CAP:
            break
    if not ids_ordered:
        return {}
    first = ids_ordered[0]
    canonical = f"https://www.youtube.com/watch?v={first}"
    out: Dict[str, Any] = {
        "link_url": canonical,
        "link_title": canonical,
        "youtube_video_id": first,
        "youtube_video_ids": ids_ordered,
    }
    return out


def _normalize_origin_peer_id(value: Any) -> Optional[str]:
    peer_id = str(value or '').strip() if isinstance(value, str) else ''
    return peer_id or None


def _normalize_channel_repost_policy(value: Any) -> Optional[str]:
    policy = str(value or '').strip().lower()
    if policy in _CHANNEL_REPOST_POLICY_VALUES:
        return policy
    return None


def _normalize_channel_variant_relationship(value: Any) -> str:
    relationship = str(value or '').strip().lower()
    if relationship in _CHANNEL_VARIANT_RELATIONSHIP_VALUES:
        return relationship
    return 'curated_recomposition'


def _normalize_channel_source_reference(value: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(value, dict):
        return None
    kind = str(value.get('kind') or '').strip().lower()
    source_type = str(value.get('source_type') or '').strip().lower()
    source_id = str(value.get('source_id') or '').strip()
    channel_id = str(value.get('channel_id') or '').strip()
    if kind not in _CHANNEL_SOURCE_REFERENCE_KIND_VALUES or source_type != 'channel_message' or not source_id or not channel_id:
        return None

    normalized: Dict[str, Any] = {
        'kind': kind,
        'source_type': 'channel_message',
        'source_id': source_id,
        'channel_id': channel_id,
    }
    created_by_user_id = str(value.get('created_by_user_id') or '').strip()
    if created_by_user_id:
        normalized['created_by_user_id'] = created_by_user_id
    if kind == 'variant_v1':
        normalized['relationship_kind'] = _normalize_channel_variant_relationship(value.get('relationship_kind'))
        module_param_delta = str(value.get('module_param_delta') or '').strip()
        if module_param_delta:
            normalized['module_param_delta'] = module_param_delta[:500]
    return normalized


def _extract_channel_source_reference(value: Any) -> Optional[Dict[str, Any]]:
    return _normalize_channel_source_reference(value)


def _is_channel_repost_reference(value: Any) -> bool:
    reference = _extract_channel_source_reference(value)
    if not reference:
        return False
    return str(reference.get('kind') or '').strip().lower() == 'repost_v1'


def _is_channel_variant_reference(value: Any) -> bool:
    reference = _extract_channel_source_reference(value)
    if not reference:
        return False
    return str(reference.get('kind') or '').strip().lower() == 'variant_v1'


def _build_channel_repost_preview_text(content: str, limit: int = 220) -> str:
    preview = ' '.join(str(content or '').split()).strip()
    if len(preview) <= limit:
        return preview
    return preview[: max(0, limit - 3)].rstrip() + '...'


def _truncate_channel_repost_reference_body(
    text: str,
    max_chars: int = _CHANNEL_REPOST_REFERENCE_BODY_MAX_CHARS,
) -> tuple[str, bool]:
    raw = str(text or '')
    if len(raw) <= max_chars:
        return raw, False
    cut = raw[: max(0, max_chars - 1)].rstrip()
    return cut + '…', True


def _safe_channel_message_type_label(message: Any) -> str:
    """JSON/UI-safe message type string for lineage preview payloads."""
    mt = getattr(message, 'message_type', None)
    if mt is None:
        return 'text'
    try:
        val = getattr(mt, 'value', None)
        if val is not None:
            return str(val)
    except Exception:
        pass
    return str(mt)


def _safe_channel_created_at_iso(message: Any) -> str:
    """ISO timestamp for lineage previews; never raises."""
    dt = getattr(message, 'created_at', None)
    if dt is None:
        return ''
    iso_fn = getattr(dt, 'isoformat', None)
    if callable(iso_fn):
        try:
            return str(iso_fn())
        except Exception:
            return ''
    return str(dt)


def _channel_repost_embed_from_original(message: Any) -> Dict[str, Any]:
    embed: Dict[str, Any] = {}
    attachments = getattr(message, 'attachments', None)
    images: List[Dict[str, str]] = []
    if isinstance(attachments, list):
        for item in attachments:
            if not isinstance(item, dict):
                continue
            typ = str(item.get('type') or '')
            file_id = str(item.get('id') or item.get('file_id') or '').strip()
            url = str(item.get('url') or '').strip()
            if not url and file_id:
                url = f"/files/{file_id}"
            if not url or not typ.startswith('image/'):
                continue
            images.append({
                'url': url,
                'name': str(item.get('name') or 'Image'),
                'type': typ,
            })
            if len(images) >= _CHANNEL_REPOST_REFERENCE_ATTACHMENT_IMAGE_CAP:
                break
    if images:
        embed['attachment_images'] = images

    for key, value in _channel_youtube_embeds_from_content(getattr(message, 'content', '')).items():
        embed.setdefault(key, value)

    return embed


def _channel_attachment_list_signals_deck_queue(
    attachments: Any,
    db_manager: Any = None,
) -> bool:
    """True when attachments include media or a Canopy HTML module the deck can use.

    When attachment dicts only carry ``id``/``file_id`` (common after sync), optional
    ``db_manager`` resolves ``files.original_name`` / ``content_type`` for the same
    rules as inline metadata.
    """
    if not isinstance(attachments, list):
        return False
    for item in attachments:
        if not isinstance(item, dict):
            continue
        typ = str(
            item.get('type') or item.get('content_type') or item.get('mime_type') or ''
        ).lower()
        name = str(
            item.get('name')
            or item.get('filename')
            or item.get('original_name')
            or item.get('file_name')
            or ''
        ).lower()
        if typ.startswith('image/') or typ.startswith('video/') or typ.startswith('audio/'):
            return True
        if name.endswith('.canopy-module.html') or name.endswith('.canopy-module.htm'):
            return True
        if typ.startswith('text/html'):
            return True
        if name.endswith('.html') or name.endswith('.htm'):
            return True

        fid = str(item.get('id') or item.get('file_id') or '').strip()
        if not fid or not db_manager:
            continue
        try:
            with db_manager.get_connection() as conn:
                row = conn.execute(
                    "SELECT original_name, content_type FROM files WHERE id = ? LIMIT 1",
                    (fid,),
                ).fetchone()
        except Exception:
            row = None
        if not row:
            continue
        try:
            db_name = str(row['original_name'] or '').lower()
            db_ct = str(row['content_type'] or '').lower()
        except (TypeError, KeyError, IndexError):
            continue
        if db_ct.startswith('image/') or db_ct.startswith('video/') or db_ct.startswith('audio/'):
            return True
        if db_name.endswith('.canopy-module.html') or db_name.endswith('.canopy-module.htm'):
            return True
        if db_ct.startswith('text/html'):
            return True
        if db_name.endswith('.html') or db_name.endswith('.htm'):
            return True
    return False


def _channel_original_signals_deck_ui(
    original: Any,
    embed: Dict[str, Any],
    db_manager: Any = None,
) -> bool:
    """Deck-eligible antecedent without requiring persisted source_layout JSON."""
    if embed.get('attachment_images'):
        return True
    if str(embed.get('link_url') or embed.get('video_url') or '').strip():
        return True
    if str(embed.get('youtube_video_id') or '').strip():
        return True
    yids = embed.get('youtube_video_ids')
    if isinstance(yids, list) and any(str(x or '').strip() for x in yids):
        return True
    atts = getattr(original, 'attachments', None)
    if _channel_attachment_list_signals_deck_queue(atts, db_manager):
        return True
    mt = getattr(original, 'message_type', None)
    if mt in (MessageType.IMAGE, MessageType.LINK):
        return True
    # FILE (or any row with stored attachments) usually has a renderable surface on the source card.
    if isinstance(atts, list) and len(atts) > 0 and mt in (
        MessageType.FILE,
        MessageType.TEXT,
        MessageType.THREAD_REPLY,
    ):
        return True
    return False


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
    post_policy: str = 'open'
    allow_member_replies: bool = True
    can_post_top_level: bool = True
    can_reply: bool = True
    allowed_poster_count: int = 0
    retired_by_vote: bool = False
    removal_status: Optional[str] = None
    active_removal_proposal_id: Optional[str] = None

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
            'post_policy': self.post_policy,
            'allow_member_replies': self.allow_member_replies,
            'can_post_top_level': self.can_post_top_level,
            'can_reply': self.can_reply,
            'allowed_poster_count': self.allowed_poster_count,
            'retired_by_vote': self.retired_by_vote,
            'removal_status': self.removal_status,
            'active_removal_proposal_id': self.active_removal_proposal_id,
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
    source_layout: Optional[Dict[str, Any]] = None
    source_reference: Optional[Dict[str, Any]] = None
    repost_policy: Optional[str] = None
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
            'source_layout': self.source_layout,
            'source_reference': self.source_reference,
            'repost_policy': self.repost_policy,
            'edited_at': self.edited_at.isoformat() if self.edited_at else None,
            'origin_peer': self.origin_peer,
            'crypto_state': self.crypto_state,
        }


class ChannelManager:
    """Manages Slack-style channels and messaging."""

    GENERAL_CHANNEL_ID = "general"
    LEGACY_AGENT_START_CHANNEL_ID = "agent-start-here"
    AGENT_START_CHANNEL_ID = LEGACY_AGENT_START_CHANNEL_ID
    AGENT_START_CHANNEL_NAME = "agent-start-here"
    AGENT_START_CHANNEL_DESCRIPTION = (
        "Private local-only quarantine and onboarding channel for newly approved agents."
    )
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
    POST_POLICY_OPEN = 'open'
    POST_POLICY_CURATED = 'curated'
    ALLOWED_POST_POLICIES = {
        POST_POLICY_OPEN,
        POST_POLICY_CURATED,
    }
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
        self.AGENT_START_CHANNEL_ID = self._load_or_create_agent_start_channel_id()
        self.workspace_events: Any = None
        self.public_placeholder_reconcile_callback: Optional[Callable[..., None]] = None
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

        # Repair membership drift so locally-hosted users see any existing
        # public/open channels even if those channels arrived after account creation.
        with LogOperation("Public channel membership repair"):
            self._repair_public_channel_memberships()
        
        logger.info("ChannelManager initialized successfully")

    def _build_agent_start_channel_id(self) -> str:
        return f"Cagentstart{secrets.token_hex(8)}"

    def _load_or_create_agent_start_channel_id(self) -> str:
        getter = getattr(self.db, 'get_system_state', None)
        setter = getattr(self.db, 'set_system_state', None)
        if callable(getter):
            try:
                existing = str(getter('agent_quarantine_channel_id') or '').strip()
            except Exception:
                existing = ''
            if existing:
                return existing
        fallback = "Cagentstartlocal"
        channel_id = self._build_agent_start_channel_id() if callable(setter) else fallback
        if callable(setter):
            try:
                setter('agent_quarantine_channel_id', channel_id)
            except Exception:
                pass
        return channel_id

    def is_agent_quarantine_channel(self, channel_id: Any, channel_name: Optional[str] = None) -> bool:
        clean_id = str(channel_id or '').strip()
        clean_name = self._normalize_channel_name(channel_name or '')
        if clean_id and clean_id in {self.AGENT_START_CHANNEL_ID, self.LEGACY_AGENT_START_CHANNEL_ID}:
            return True
        return clean_name == self.AGENT_START_CHANNEL_NAME

    def get_agent_quarantine_channel_id(self) -> str:
        return self.AGENT_START_CHANNEL_ID

    def ensure_default_channels_exist(self) -> None:
        """Public wrapper for default channel bootstrap/self-heal."""
        self._ensure_default_channels()

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

    def _get_local_public_channel_candidate_user_ids(
        self,
        conn: sqlite3.Connection,
        fallback_user_id: Optional[str] = None,
    ) -> List[str]:
        """Return locally-hosted users that should see public/open channels."""
        candidate_ids: List[str] = []
        seen: set[str] = set()
        try:
            rows = conn.execute(
                """
                SELECT id
                FROM users
                WHERE id != 'system'
                  AND id != 'local_user'
                  AND (origin_peer IS NULL OR trim(origin_peer) = '')
                  AND (
                        (password_hash IS NOT NULL AND trim(password_hash) != '')
                        OR lower(COALESCE(account_type, 'human')) = 'agent'
                  )
                ORDER BY created_at ASC, id ASC
                """
            ).fetchall()
        except Exception:
            rows = []

        for row in rows or []:
            user_id = str(row['id'] if hasattr(row, 'keys') else row[0] or '').strip()
            if not user_id or user_id in seen:
                continue
            seen.add(user_id)
            candidate_ids.append(user_id)

        fallback = str(fallback_user_id or '').strip()
        if fallback and fallback not in seen:
            row = conn.execute(
                "SELECT id FROM users WHERE id = ?",
                (fallback,),
            ).fetchone()
            if row:
                candidate_ids.append(fallback)

        return candidate_ids

    def _ensure_public_channel_membership_conn(
        self,
        conn: sqlite3.Connection,
        channel_id: str,
        channel_type: Any,
        privacy_mode: Any,
        *,
        fallback_user_id: Optional[str] = None,
    ) -> int:
        """Ensure local users are members of a public/open channel."""
        if not channel_id or not self._is_public_channel(channel_type, privacy_mode):
            return 0

        added = 0
        for user_id in self._get_local_public_channel_candidate_user_ids(
            conn,
            fallback_user_id=fallback_user_id,
        ):
            target_policy = self._load_user_channel_governance(conn, user_id)
            allowed, reason = self._is_channel_allowed_by_policy(
                policy=target_policy,
                channel_id=channel_id,
                channel_type=channel_type,
                privacy_mode=privacy_mode,
            )
            if not allowed:
                logger.info(
                    "Skipping public auto-membership for %s in %s due to governance policy (%s)",
                    user_id,
                    channel_id,
                    reason,
                )
                continue
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO channel_members (channel_id, user_id, role)
                VALUES (?, ?, 'member')
                """,
                (channel_id, user_id),
            )
            if int(getattr(cur, 'rowcount', 0) or 0) > 0:
                added += 1
        return added

    def _repair_public_channel_memberships(self) -> int:
        """Backfill local memberships for any existing public/open channels."""
        repaired = 0
        try:
            with self.db.get_connection() as conn:
                rows = conn.execute(
                    """
                    SELECT id, channel_type, COALESCE(privacy_mode, 'open') AS privacy_mode
                    FROM channels
                    WHERE (channel_type = 'public' OR channel_type = 'general')
                      AND COALESCE(privacy_mode, 'open') NOT IN ('private', 'confidential')
                    """
                ).fetchall()
                for row in rows or []:
                    repaired += self._ensure_public_channel_membership_conn(
                        conn,
                        str(row['id'] or '').strip(),
                        row['channel_type'],
                        row['privacy_mode'],
                    )
                conn.commit()
        except Exception as e:
            logger.warning(f"Failed to repair public channel memberships: {e}")
            return 0

        if repaired:
            logger.info("Repaired %s public channel membership row(s)", repaired)
        return repaired

    @staticmethod
    def _has_placeholder_channel_marker(
        name: Any,
        description: Any,
    ) -> bool:
        clean_name = str(name or '').strip()
        clean_desc = str(description or '').strip()
        return (
            clean_name.startswith('peer-channel-')
            or clean_desc.startswith('Auto-created from P2P catchup')
        )

    @classmethod
    def _is_placeholder_channel_signature(
        cls,
        name: Any,
        description: Any,
        channel_type: Any,
        privacy_mode: Any,
    ) -> bool:
        clean_type = str(channel_type or '').strip().lower()
        clean_privacy = str(privacy_mode or '').strip().lower()
        return (
            cls._has_placeholder_channel_marker(name, description)
            and clean_type == 'private'
            and clean_privacy == 'private'
        )

    def list_public_placeholder_reconcile_candidates(self, limit: int = 200) -> List[Dict[str, Any]]:
        """Return placeholder/private channels that should be rechecked against origin metadata."""
        max_rows = max(1, int(limit or 200))
        try:
            with self.db.get_connection() as conn:
                rows = conn.execute(
                    """
                    SELECT c.id,
                           c.name,
                           c.description,
                           c.origin_peer,
                           c.channel_type,
                           COALESCE(c.privacy_mode, 'private') AS privacy_mode,
                           COUNT(m.id) AS message_count
                    FROM channels c
                    LEFT JOIN channel_messages m ON m.channel_id = c.id
                    WHERE c.origin_peer IS NOT NULL
                      AND TRIM(c.origin_peer) != ''
                      AND (
                            c.name LIKE 'peer-channel-%'
                            OR c.description LIKE 'Auto-created from P2P catchup%'
                      )
                    GROUP BY c.id, c.name, c.description, c.origin_peer, c.channel_type, COALESCE(c.privacy_mode, 'private')
                    HAVING COUNT(m.id) > 0
                    ORDER BY COUNT(m.id) DESC, c.id ASC
                    LIMIT ?
                    """,
                    (max_rows,),
                ).fetchall()
            return [dict(row) for row in rows or []]
        except Exception as e:
            logger.warning(f"Failed to list public placeholder reconcile candidates: {e}")
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
                        post_policy TEXT DEFAULT 'open',
                        allow_member_replies BOOLEAN DEFAULT 1,
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
                        source_reference TEXT,  -- JSON blob for repost wrappers
                        repost_policy TEXT DEFAULT 'same_scope',
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

                    CREATE TABLE IF NOT EXISTS channel_post_permissions (
                        channel_id TEXT NOT NULL,
                        user_id TEXT NOT NULL,
                        granted_by TEXT,
                        granted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        PRIMARY KEY (channel_id, user_id),
                        FOREIGN KEY (channel_id) REFERENCES channels(id) ON DELETE CASCADE,
                        FOREIGN KEY (user_id) REFERENCES users(id),
                        FOREIGN KEY (granted_by) REFERENCES users(id)
                    );
                    CREATE INDEX IF NOT EXISTS idx_channel_post_permissions_user
                        ON channel_post_permissions(user_id, granted_at DESC);

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

                    CREATE TABLE IF NOT EXISTS channel_removal_proposals (
                        proposal_id TEXT PRIMARY KEY,
                        channel_id TEXT NOT NULL,
                        channel_name TEXT,
                        channel_origin_peer TEXT,
                        channel_privacy_mode TEXT,
                        initiator_peer_id TEXT NOT NULL,
                        initiator_user_id TEXT,
                        electorate_json TEXT NOT NULL,
                        threshold_count INTEGER NOT NULL,
                        status TEXT NOT NULL DEFAULT 'open',
                        final_result TEXT,
                        tombstone_id TEXT,
                        opened_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        finalized_at TIMESTAMP,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        FOREIGN KEY (channel_id) REFERENCES channels(id) ON DELETE CASCADE
                    );
                    CREATE INDEX IF NOT EXISTS idx_channel_removal_proposals_channel
                        ON channel_removal_proposals(channel_id, status, opened_at DESC);

                    CREATE TABLE IF NOT EXISTS channel_removal_votes (
                        proposal_id TEXT NOT NULL,
                        channel_id TEXT NOT NULL,
                        voter_peer_id TEXT NOT NULL,
                        voter_user_id TEXT,
                        vote TEXT NOT NULL,
                        reason TEXT,
                        cast_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        PRIMARY KEY (proposal_id, voter_peer_id),
                        FOREIGN KEY (proposal_id) REFERENCES channel_removal_proposals(proposal_id) ON DELETE CASCADE,
                        FOREIGN KEY (channel_id) REFERENCES channels(id) ON DELETE CASCADE
                    );
                    CREATE INDEX IF NOT EXISTS idx_channel_removal_votes_channel
                        ON channel_removal_votes(channel_id, cast_at DESC);

                    CREATE TABLE IF NOT EXISTS channel_removal_tombstones (
                        tombstone_id TEXT PRIMARY KEY,
                        channel_id TEXT NOT NULL,
                        proposal_id TEXT,
                        channel_name TEXT,
                        channel_origin_peer TEXT,
                        electorate_json TEXT NOT NULL,
                        threshold_count INTEGER NOT NULL,
                        retired_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        retired_by_peer_id TEXT NOT NULL,
                        retired_by_user_id TEXT,
                        restored_at TIMESTAMP,
                        restored_by_peer_id TEXT,
                        restored_by_user_id TEXT,
                        restoration_reason TEXT,
                        FOREIGN KEY (proposal_id) REFERENCES channel_removal_proposals(proposal_id)
                    );
                    CREATE INDEX IF NOT EXISTS idx_channel_removal_tombstones_channel
                        ON channel_removal_tombstones(channel_id, restored_at, retired_at DESC);
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
                    ('post_policy', "ALTER TABLE channels ADD COLUMN post_policy TEXT DEFAULT 'open'"),
                    ('allow_member_replies', "ALTER TABLE channels ADD COLUMN allow_member_replies BOOLEAN DEFAULT 1"),
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
                for col, typ in [('ttl_seconds', 'INTEGER'), ('ttl_mode', 'TEXT'), ('edited_at', 'TIMESTAMP'), ('security', 'TEXT'), ('source_layout', 'TEXT'), ('source_reference', 'TEXT'), ('repost_policy', 'TEXT')]:
                    try:
                        conn.execute(f"SELECT {col} FROM channel_messages LIMIT 1")
                    except Exception:
                        if col == 'repost_policy':
                            conn.execute(
                                "ALTER TABLE channel_messages ADD COLUMN repost_policy TEXT DEFAULT 'same_scope'"
                            )
                        else:
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
                           SET post_policy = COALESCE(post_policy, ?),
                               allow_member_replies = COALESCE(allow_member_replies, 1)
                        """,
                        (self.POST_POLICY_OPEN,),
                    )
                except Exception as post_policy_backfill_err:
                    logger.debug(f"Channel post policy backfill skipped: {post_policy_backfill_err}")

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

                owner_id = None
                try:
                    owner_id = self.db.get_instance_owner_user_id()
                except Exception:
                    owner_id = None

                cursor = conn.execute(
                    "SELECT id FROM channels WHERE id = ? OR name = ?",
                    (self.GENERAL_CHANNEL_ID, self.GENERAL_CHANNEL_ID),
                )
                if not cursor.fetchone():
                    logger.info("Creating default general channel")
                    conn.execute(
                        """
                        INSERT INTO channels (
                            id, name, channel_type, created_by, description, privacy_mode,
                            last_activity_at, lifecycle_ttl_days, lifecycle_preserved
                        )
                        VALUES (?, ?, 'public', 'system', ?, 'open', CURRENT_TIMESTAMP, ?, 1)
                        """,
                        (
                            self.GENERAL_CHANNEL_ID,
                            self.GENERAL_CHANNEL_ID,
                            'General discussion channel',
                            self.DEFAULT_CHANNEL_LIFECYCLE_DAYS,
                        ),
                    )
                    logger.info("Default general channel created successfully")
                else:
                    logger.debug("General channel already exists")
                    conn.execute(
                        "UPDATE channels SET privacy_mode = 'open' WHERE id = ?",
                        (self.GENERAL_CHANNEL_ID,),
                    )

                conn.execute(
                    "UPDATE channels SET lifecycle_preserved = 1, last_activity_at = COALESCE(last_activity_at, created_at) WHERE id = ?",
                    (self.GENERAL_CHANNEL_ID,),
                )
                conn.execute(
                    """
                    INSERT OR IGNORE INTO channel_members (channel_id, user_id, role)
                    VALUES (?, 'local_user', 'admin')
                    """,
                    (self.GENERAL_CHANNEL_ID,),
                )

                self._ensure_agent_quarantine_channel(conn)
                conn.execute(
                    "UPDATE channels SET lifecycle_preserved = 1, last_activity_at = COALESCE(last_activity_at, created_at) WHERE id = ?",
                    (self.AGENT_START_CHANNEL_ID,),
                )
                conn.execute(
                    """
                    INSERT OR IGNORE INTO channel_members (channel_id, user_id, role)
                    VALUES (?, 'local_user', 'admin')
                    """,
                    (self.AGENT_START_CHANNEL_ID,),
                )
                if owner_id and owner_id not in {'system', 'local_user'}:
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO channel_members (channel_id, user_id, role)
                        VALUES (?, ?, 'admin')
                        """,
                        (self.AGENT_START_CHANNEL_ID, owner_id),
                    )
                conn.commit()
                    
        except Exception as e:
            logger.error(f"Failed to ensure default channels: {e}", exc_info=True)
            raise

    def _ensure_agent_quarantine_channel(self, conn: sqlite3.Connection) -> None:
        current_id = self.AGENT_START_CHANNEL_ID
        existing_current = conn.execute(
            "SELECT id FROM channels WHERE id = ?",
            (current_id,),
        ).fetchone()
        if not existing_current:
            logger.info("Creating per-instance agent quarantine channel %s", current_id)
            conn.execute(
                """
                INSERT INTO channels (
                    id, name, channel_type, created_by, description, privacy_mode,
                    last_activity_at, lifecycle_ttl_days, lifecycle_preserved,
                    post_policy, allow_member_replies
                )
                VALUES (?, ?, 'private', 'system', ?, 'private', CURRENT_TIMESTAMP, ?, 1, ?, 1)
                """,
                (
                    current_id,
                    self.AGENT_START_CHANNEL_NAME,
                    self.AGENT_START_CHANNEL_DESCRIPTION,
                    self.DEFAULT_CHANNEL_LIFECYCLE_DAYS,
                    self.POST_POLICY_OPEN,
                ),
            )

        stale_rows = conn.execute(
            """
            SELECT id, COALESCE(origin_peer, '') AS origin_peer
            FROM channels
            WHERE name = ?
              AND id != ?
            ORDER BY CASE WHEN id = ? THEN 0 ELSE 1 END, id ASC
            """,
            (
                self.AGENT_START_CHANNEL_NAME,
                current_id,
                self.LEGACY_AGENT_START_CHANNEL_ID,
            ),
        ).fetchall()
        if not stale_rows:
            return

        governance_updates: List[str] = []
        for row in stale_rows:
            stale_id = str(row['id'] if hasattr(row, 'keys') and 'id' in row.keys() else row[0])
            origin_peer = str(row['origin_peer'] if hasattr(row, 'keys') and 'origin_peer' in row.keys() else row[1]).strip()
            if not stale_id or stale_id == current_id:
                continue
            if not origin_peer:
                self._migrate_agent_quarantine_references(conn, stale_id, current_id)
                governance_updates.append(stale_id)
            logger.info("Removing stale agent quarantine channel %s", stale_id)
            conn.execute("DELETE FROM channels WHERE id = ?", (stale_id,))

        for stale_id in governance_updates:
            self._replace_quarantine_channel_in_governance(conn, stale_id, current_id)

    def _migrate_agent_quarantine_references(
        self,
        conn: sqlite3.Connection,
        old_channel_id: str,
        new_channel_id: str,
    ) -> None:
        if not old_channel_id or not new_channel_id or old_channel_id == new_channel_id:
            return
        conn.execute(
            """
            INSERT OR IGNORE INTO channel_members (
                channel_id, user_id, joined_at, role, notifications_enabled, last_read_at
            )
            SELECT ?, user_id, joined_at, role, notifications_enabled, last_read_at
            FROM channel_members
            WHERE channel_id = ?
            """,
            (new_channel_id, old_channel_id),
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO channel_post_permissions (
                channel_id, user_id, granted_by, granted_at
            )
            SELECT ?, user_id, granted_by, granted_at
            FROM channel_post_permissions
            WHERE channel_id = ?
            """,
            (new_channel_id, old_channel_id),
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO channel_keys (
                channel_id, key_id, key_material_enc, created_by_peer, created_at, revoked_at, metadata
            )
            SELECT ?, key_id, key_material_enc, created_by_peer, created_at, revoked_at, metadata
            FROM channel_keys
            WHERE channel_id = ?
            """,
            (new_channel_id, old_channel_id),
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO channel_member_keys (
                channel_id, key_id, peer_id, delivery_state, last_error, delivered_at, acked_at, updated_at
            )
            SELECT ?, key_id, peer_id, delivery_state, last_error, delivered_at, acked_at, updated_at
            FROM channel_member_keys
            WHERE channel_id = ?
            """,
            (new_channel_id, old_channel_id),
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO channel_thread_subscriptions (
                channel_id, thread_root_message_id, user_id, subscribed, source, created_at, updated_at
            )
            SELECT ?, thread_root_message_id, user_id, subscribed, source, created_at, updated_at
            FROM channel_thread_subscriptions
            WHERE channel_id = ?
            """,
            (new_channel_id, old_channel_id),
        )
        conn.execute(
            "UPDATE channel_messages SET channel_id = ? WHERE channel_id = ?",
            (new_channel_id, old_channel_id),
        )
        for table_name in (
            'channel_member_sync_deliveries',
            'channel_sync_digests',
            'channel_removal_proposals',
            'channel_removal_votes',
            'channel_removal_tombstones',
        ):
            conn.execute(
                f"UPDATE {table_name} SET channel_id = ? WHERE channel_id = ?",
                (new_channel_id, old_channel_id),
            )
        for table_name in (
            'channel_members',
            'channel_post_permissions',
            'channel_member_keys',
            'channel_keys',
            'channel_thread_subscriptions',
        ):
            conn.execute(f"DELETE FROM {table_name} WHERE channel_id = ?", (old_channel_id,))

    def _replace_quarantine_channel_in_governance(
        self,
        conn: sqlite3.Connection,
        old_channel_id: str,
        new_channel_id: str,
    ) -> None:
        rows = conn.execute(
            """
            SELECT user_id, allowed_channel_ids
            FROM user_channel_governance
            WHERE allowed_channel_ids IS NOT NULL
              AND allowed_channel_ids != ''
            """
        ).fetchall()
        for row in rows or []:
            user_id = str(row['user_id'])
            raw = row['allowed_channel_ids']
            try:
                parsed = json.loads(raw or '[]')
            except Exception:
                continue
            if not isinstance(parsed, list) or old_channel_id not in parsed:
                continue
            updated = []
            seen: set[str] = set()
            for value in parsed:
                channel_id = new_channel_id if str(value or '').strip() == old_channel_id else str(value or '').strip()
                if not channel_id or channel_id in seen:
                    continue
                seen.add(channel_id)
                updated.append(channel_id)
            conn.execute(
                """
                UPDATE user_channel_governance
                SET allowed_channel_ids = ?, updated_at = CURRENT_TIMESTAMP
                WHERE user_id = ?
                """,
                (json.dumps(updated), user_id),
            )

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

    def ensure_agent_quarantine_assignment(
        self,
        user_id: str,
        *,
        updated_by: Optional[str] = None,
        role: str = 'member',
    ) -> bool:
        """Place an agent into the built-in quarantine channel and restrict channel access."""
        if not user_id:
            return False
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
                    INSERT OR IGNORE INTO channel_members (channel_id, user_id, role)
                    VALUES (?, ?, ?)
                    """,
                    (self.AGENT_START_CHANNEL_ID, user_id, role or 'member'),
                )
                conn.commit()
            return self.set_user_channel_governance(
                user_id=user_id,
                enabled=True,
                block_public_channels=True,
                restrict_to_allowed_channels=True,
                allowed_channel_ids=[self.AGENT_START_CHANNEL_ID],
                updated_by=updated_by or 'system',
            )
        except Exception as e:
            logger.error(f"Failed to quarantine agent {user_id}: {e}", exc_info=True)
            return False

    @staticmethod
    def _normalize_peer_id_list(peer_ids: Optional[List[Any]]) -> List[str]:
        seen: set[str] = set()
        normalized: List[str] = []
        for peer_id in peer_ids or []:
            clean = str(peer_id or '').strip()
            if not clean or clean in seen:
                continue
            seen.add(clean)
            normalized.append(clean)
        return sorted(normalized)

    @staticmethod
    def _load_channel_removal_electorate(raw_json: Any) -> List[str]:
        try:
            parsed = json.loads(raw_json or '[]')
        except Exception:
            parsed = []
        if not isinstance(parsed, list):
            parsed = []
        return ChannelManager._normalize_peer_id_list(parsed)

    def _channel_removal_ineligible_reason(
        self,
        *,
        channel_id: str,
        channel_type: str,
        lifecycle_preserved: bool,
    ) -> Optional[str]:
        clean_id = str(channel_id or '').strip()
        ctype = str(channel_type or '').strip().lower()
        if not clean_id:
            return 'invalid_channel'
        if clean_id == self.GENERAL_CHANNEL_ID or self.is_agent_quarantine_channel(clean_id):
            return 'system_channel'
        if ctype in {'general', 'dm', 'group_dm'}:
            return 'channel_type_ineligible'
        if bool(lifecycle_preserved):
            return 'preserved_channel'
        return None

    def _get_active_channel_removal_tombstone_row(
        self,
        conn: sqlite3.Connection,
        channel_id: str,
    ) -> Optional[sqlite3.Row]:
        return conn.execute(
            """
            SELECT *
            FROM channel_removal_tombstones
            WHERE channel_id = ?
              AND restored_at IS NULL
            ORDER BY retired_at DESC
            LIMIT 1
            """,
            (channel_id,),
        ).fetchone()

    def _get_open_channel_removal_proposal_row(
        self,
        conn: sqlite3.Connection,
        channel_id: str,
    ) -> Optional[sqlite3.Row]:
        return conn.execute(
            """
            SELECT *
            FROM channel_removal_proposals
            WHERE channel_id = ?
              AND status = ?
            ORDER BY opened_at DESC
            LIMIT 1
            """,
            (channel_id, _CHANNEL_REMOVAL_OPEN_STATUS),
        ).fetchone()

    def is_channel_retired_by_vote(self, channel_id: str) -> bool:
        if not channel_id:
            return False
        try:
            with self.db.get_connection() as conn:
                return bool(self._get_active_channel_removal_tombstone_row(conn, channel_id))
        except Exception as e:
            logger.error(f"Failed to load removal tombstone for {channel_id}: {e}", exc_info=True)
            return False

    def resolve_channel_removal_electorate(
        self,
        channel_id: str,
        *,
        local_peer_id: Optional[str],
        connected_peer_ids: Optional[List[Any]],
        trusted_peer_ids: Optional[List[Any]],
    ) -> List[str]:
        clean_channel_id = str(channel_id or '').strip()
        if not clean_channel_id:
            return []
        try:
            with self.db.get_connection() as conn:
                row = conn.execute(
                    """
                    SELECT channel_type, COALESCE(privacy_mode, 'open') AS privacy_mode
                    FROM channels
                    WHERE id = ?
                    """,
                    (clean_channel_id,),
                ).fetchone()
            if not row:
                return []

            connected = set(self._normalize_peer_id_list(connected_peer_ids))
            trusted = set(self._normalize_peer_id_list(trusted_peer_ids))
            electorate: set[str] = set()
            local_peer = str(local_peer_id or '').strip()
            if local_peer:
                electorate.add(local_peer)

            privacy_mode = str(row['privacy_mode'] or 'open').strip().lower()
            channel_type = str(row['channel_type'] or 'public').strip().lower()
            targeted = privacy_mode in self.TARGETED_PRIVACY_MODES or channel_type == 'private'
            if targeted:
                member_peers = self.get_member_peer_ids(clean_channel_id, local_peer or None)
                electorate.update(member_peers & connected & trusted)
            else:
                electorate.update(connected & trusted)

            return self._normalize_peer_id_list(list(electorate))
        except Exception as e:
            logger.error(f"Failed to resolve removal electorate for {channel_id}: {e}", exc_info=True)
            return []

    def _build_channel_removal_status_locked(
        self,
        conn: sqlite3.Connection,
        channel_row: Optional[sqlite3.Row],
        *,
        channel_id: str,
        local_peer_id: Optional[str],
        viewer_user_id: Optional[str],
        allow_admin: bool,
        connected_peer_ids: Optional[List[Any]],
        trusted_peer_ids: Optional[List[Any]],
    ) -> Dict[str, Any]:
        status: Dict[str, Any] = {
            'channel_id': channel_id,
            'channel_exists': bool(channel_row),
            'retired': False,
            'retired_reason': None,
            'can_start': False,
            'can_vote': False,
            'can_change_vote': False,
            'ineligible_reason': None,
            'active_proposal': None,
            'tombstone': None,
        }
        if not channel_row:
            status['ineligible_reason'] = 'channel_not_found'
            return status

        channel_type = str(channel_row['channel_type'] or 'public').strip().lower()
        lifecycle_preserved = bool(channel_row['lifecycle_preserved'])
        status['ineligible_reason'] = self._channel_removal_ineligible_reason(
            channel_id=channel_id,
            channel_type=channel_type,
            lifecycle_preserved=lifecycle_preserved,
        )

        active_tombstone = self._get_active_channel_removal_tombstone_row(conn, channel_id)
        if active_tombstone:
            electorate = self._load_channel_removal_electorate(active_tombstone['electorate_json'])
            status['retired'] = True
            status['retired_reason'] = 'retired_by_vote'
            status['tombstone'] = {
                'tombstone_id': active_tombstone['tombstone_id'],
                'proposal_id': active_tombstone['proposal_id'],
                'channel_name': active_tombstone['channel_name'],
                'channel_origin_peer': active_tombstone['channel_origin_peer'],
                'electorate_peer_ids': electorate,
                'threshold_count': int(active_tombstone['threshold_count'] or len(electorate) or 0),
                'retired_at': active_tombstone['retired_at'],
                'retired_by_peer_id': active_tombstone['retired_by_peer_id'],
                'retired_by_user_id': active_tombstone['retired_by_user_id'],
            }
            return status

        if viewer_user_id:
            status['can_start'] = bool(allow_admin or self.is_channel_admin(channel_id, viewer_user_id))

        proposal_row = self._get_open_channel_removal_proposal_row(conn, channel_id)
        if proposal_row:
            electorate = self._load_channel_removal_electorate(proposal_row['electorate_json'])
            vote_rows = conn.execute(
                """
                SELECT voter_peer_id, voter_user_id, vote, reason, cast_at
                FROM channel_removal_votes
                WHERE proposal_id = ?
                ORDER BY cast_at ASC
                """,
                (proposal_row['proposal_id'],),
            ).fetchall()
            votes_by_peer = {
                str(row['voter_peer_id'] or '').strip(): {
                    'peer_id': str(row['voter_peer_id'] or '').strip(),
                    'user_id': str(row['voter_user_id'] or '').strip() or None,
                    'vote': str(row['vote'] or '').strip().lower(),
                    'reason': row['reason'],
                    'cast_at': row['cast_at'],
                }
                for row in vote_rows or []
                if str(row['voter_peer_id'] or '').strip()
            }
            local_peer = str(local_peer_id or '').strip()
            connected = set(self._normalize_peer_id_list(connected_peer_ids))
            trusted = set(self._normalize_peer_id_list(trusted_peer_ids))
            electorate_entries: List[Dict[str, Any]] = []
            remove_votes = 0
            keep_votes = 0
            for peer_id in electorate:
                vote_entry = votes_by_peer.get(peer_id)
                vote_value = str(vote_entry.get('vote') or '').strip().lower() if vote_entry else ''
                if vote_value == 'remove':
                    remove_votes += 1
                elif vote_value == 'keep':
                    keep_votes += 1
                electorate_entries.append({
                    'peer_id': peer_id,
                    'connected': peer_id in connected or (local_peer and peer_id == local_peer),
                    'trusted': peer_id in trusted or (local_peer and peer_id == local_peer),
                    'is_local_peer': bool(local_peer and peer_id == local_peer),
                    'vote': vote_value or None,
                    'voter_user_id': vote_entry.get('user_id') if vote_entry else None,
                    'cast_at': vote_entry.get('cast_at') if vote_entry else None,
                })

            local_vote = votes_by_peer.get(local_peer, {}).get('vote') if local_peer else None
            status['active_proposal'] = {
                'proposal_id': proposal_row['proposal_id'],
                'status': proposal_row['status'],
                'channel_name': proposal_row['channel_name'],
                'channel_origin_peer': proposal_row['channel_origin_peer'],
                'channel_privacy_mode': proposal_row['channel_privacy_mode'],
                'initiator_peer_id': proposal_row['initiator_peer_id'],
                'initiator_user_id': proposal_row['initiator_user_id'],
                'opened_at': proposal_row['opened_at'],
                'electorate_count': len(electorate),
                'threshold_count': int(proposal_row['threshold_count'] or len(electorate) or 0),
                'remove_votes': remove_votes,
                'keep_votes': keep_votes,
                'pending_votes': max(0, len(electorate) - remove_votes - keep_votes),
                'electorate': electorate_entries,
                'local_peer_id': local_peer or None,
                'local_vote': local_vote,
            }
            status['can_vote'] = bool(
                viewer_user_id
                and (allow_admin or self.get_member_role(channel_id, viewer_user_id))
                and local_peer
                and local_peer in electorate
                and not local_vote
            )
            status['can_change_vote'] = bool(
                viewer_user_id
                and (allow_admin or self.get_member_role(channel_id, viewer_user_id))
                and local_peer
                and local_peer in electorate
                and local_vote
            )
            return status

        return status

    def get_channel_removal_status(
        self,
        channel_id: str,
        *,
        local_peer_id: Optional[str] = None,
        viewer_user_id: Optional[str] = None,
        allow_admin: bool = False,
        connected_peer_ids: Optional[List[Any]] = None,
        trusted_peer_ids: Optional[List[Any]] = None,
    ) -> Dict[str, Any]:
        clean_channel_id = str(channel_id or '').strip()
        if not clean_channel_id:
            return {
                'channel_id': '',
                'channel_exists': False,
                'retired': False,
                'retired_reason': None,
                'can_start': False,
                'can_vote': False,
                'can_change_vote': False,
                'ineligible_reason': 'invalid_channel',
                'active_proposal': None,
                'tombstone': None,
            }
        try:
            with self.db.get_connection() as conn:
                channel_row = conn.execute(
                    """
                    SELECT id, name, channel_type, origin_peer,
                           COALESCE(privacy_mode, 'open') AS privacy_mode,
                           COALESCE(lifecycle_preserved, 0) AS lifecycle_preserved
                    FROM channels
                    WHERE id = ?
                    """,
                    (clean_channel_id,),
                ).fetchone()
                return self._build_channel_removal_status_locked(
                    conn,
                    channel_row,
                    channel_id=clean_channel_id,
                    local_peer_id=local_peer_id,
                    viewer_user_id=viewer_user_id,
                    allow_admin=allow_admin,
                    connected_peer_ids=connected_peer_ids,
                    trusted_peer_ids=trusted_peer_ids,
                )
        except Exception as e:
            logger.error(f"Failed to get channel removal status for {channel_id}: {e}", exc_info=True)
            return {
                'channel_id': clean_channel_id,
                'channel_exists': False,
                'retired': False,
                'retired_reason': None,
                'can_start': False,
                'can_vote': False,
                'can_change_vote': False,
                'ineligible_reason': 'internal_error',
                'active_proposal': None,
                'tombstone': None,
            }

    def _finalize_channel_removal_locked(
        self,
        conn: sqlite3.Connection,
        *,
        proposal_row: sqlite3.Row,
        finalizing_peer_id: str,
        finalizing_user_id: Optional[str],
    ) -> Optional[Dict[str, Any]]:
        proposal_id = str(proposal_row['proposal_id'] or '').strip()
        channel_id = str(proposal_row['channel_id'] or '').strip()
        if not proposal_id or not channel_id:
            return None

        electorate = self._load_channel_removal_electorate(proposal_row['electorate_json'])
        threshold_count = int(proposal_row['threshold_count'] or len(electorate) or 0)
        vote_rows = conn.execute(
            """
            SELECT voter_peer_id, vote
            FROM channel_removal_votes
            WHERE proposal_id = ?
            """,
            (proposal_id,),
        ).fetchall()
        electorate_set = set(electorate)
        vote_map = {
            str(row['voter_peer_id'] or '').strip(): str(row['vote'] or '').strip().lower()
            for row in vote_rows or []
            if str(row['voter_peer_id'] or '').strip() in electorate_set
        }
        remove_votes = sum(1 for vote in vote_map.values() if vote == 'remove')
        keep_votes = sum(1 for vote in vote_map.values() if vote == 'keep')
        now_ts = self._format_db_timestamp(datetime.now(timezone.utc))
        if keep_votes > 0:
            conn.execute(
                """
                UPDATE channel_removal_proposals
                SET status = ?, final_result = ?, finalized_at = ?, updated_at = ?
                WHERE proposal_id = ?
                """,
                (
                    _CHANNEL_REMOVAL_REJECTED_STATUS,
                    _CHANNEL_REMOVAL_REJECTED_STATUS,
                    now_ts,
                    now_ts,
                    proposal_id,
                ),
            )
            return {
                'proposal_id': proposal_id,
                'channel_id': channel_id,
                'status': _CHANNEL_REMOVAL_REJECTED_STATUS,
                'result': _CHANNEL_REMOVAL_REJECTED_STATUS,
                'electorate_peer_ids': electorate,
                'threshold_count': threshold_count,
                'remove_votes': remove_votes,
                'keep_votes': keep_votes,
                'finalized_at': now_ts,
            }

        if threshold_count > 0 and remove_votes >= threshold_count:
            tombstone_row = self._get_active_channel_removal_tombstone_row(conn, channel_id)
            tombstone_id = None
            if tombstone_row:
                tombstone_id = str(tombstone_row['tombstone_id'] or '').strip() or None
            if not tombstone_id:
                tombstone_id = f"CRT{secrets.token_hex(16)}"
                conn.execute(
                    """
                    INSERT INTO channel_removal_tombstones (
                        tombstone_id, channel_id, proposal_id, channel_name,
                        channel_origin_peer, electorate_json, threshold_count,
                        retired_at, retired_by_peer_id, retired_by_user_id
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        tombstone_id,
                        channel_id,
                        proposal_id,
                        proposal_row['channel_name'],
                        proposal_row['channel_origin_peer'],
                        json.dumps(electorate),
                        threshold_count,
                        now_ts,
                        finalizing_peer_id,
                        finalizing_user_id,
                    ),
                )
            conn.execute(
                """
                UPDATE channel_removal_proposals
                SET status = ?, final_result = ?, tombstone_id = ?, finalized_at = ?, updated_at = ?
                WHERE proposal_id = ?
                """,
                (
                    _CHANNEL_REMOVAL_RETIRED_STATUS,
                    _CHANNEL_REMOVAL_RETIRED_STATUS,
                    tombstone_id,
                    now_ts,
                    now_ts,
                    proposal_id,
                ),
            )
            return {
                'proposal_id': proposal_id,
                'channel_id': channel_id,
                'status': _CHANNEL_REMOVAL_RETIRED_STATUS,
                'result': _CHANNEL_REMOVAL_RETIRED_STATUS,
                'tombstone_id': tombstone_id,
                'electorate_peer_ids': electorate,
                'threshold_count': threshold_count,
                'remove_votes': remove_votes,
                'keep_votes': keep_votes,
                'finalized_at': now_ts,
            }
        return None

    def start_channel_removal_vote(
        self,
        *,
        channel_id: str,
        user_id: str,
        local_peer_id: str,
        connected_peer_ids: Optional[List[Any]],
        trusted_peer_ids: Optional[List[Any]],
        allow_admin: bool = False,
        reason: Optional[str] = None,
        proposal_id: Optional[str] = None,
        opened_at: Optional[str] = None,
    ) -> Dict[str, Any]:
        clean_channel_id = str(channel_id or '').strip()
        local_peer = str(local_peer_id or '').strip()
        actor_user_id = str(user_id or '').strip()
        if not clean_channel_id or not local_peer or not actor_user_id:
            return {'ok': False, 'error': 'invalid_request'}
        try:
            with self.db.get_connection() as conn:
                channel_row = conn.execute(
                    """
                    SELECT id, name, channel_type, origin_peer,
                           COALESCE(privacy_mode, 'open') AS privacy_mode,
                           COALESCE(lifecycle_preserved, 0) AS lifecycle_preserved
                    FROM channels
                    WHERE id = ?
                    """,
                    (clean_channel_id,),
                ).fetchone()
                if not channel_row:
                    return {'ok': False, 'error': 'channel_not_found'}
                if not (allow_admin or self.is_channel_admin(clean_channel_id, actor_user_id)):
                    return {'ok': False, 'error': 'not_authorized'}

                ineligible = self._channel_removal_ineligible_reason(
                    channel_id=clean_channel_id,
                    channel_type=channel_row['channel_type'],
                    lifecycle_preserved=bool(channel_row['lifecycle_preserved']),
                )
                if ineligible:
                    return {'ok': False, 'error': ineligible}
                if self._get_active_channel_removal_tombstone_row(conn, clean_channel_id):
                    return {'ok': False, 'error': 'already_retired'}

                existing = self._get_open_channel_removal_proposal_row(conn, clean_channel_id)
                if existing:
                    status = self._build_channel_removal_status_locked(
                        conn,
                        channel_row,
                        channel_id=clean_channel_id,
                        local_peer_id=local_peer,
                        viewer_user_id=actor_user_id,
                        allow_admin=allow_admin,
                        connected_peer_ids=connected_peer_ids,
                        trusted_peer_ids=trusted_peer_ids,
                    )
                    return {'ok': False, 'error': 'proposal_exists', 'status': status}

                electorate = self.resolve_channel_removal_electorate(
                    clean_channel_id,
                    local_peer_id=local_peer,
                    connected_peer_ids=connected_peer_ids,
                    trusted_peer_ids=trusted_peer_ids,
                )
                if not electorate:
                    return {'ok': False, 'error': 'no_eligible_electorate'}
                if local_peer not in electorate:
                    electorate.append(local_peer)
                    electorate = self._normalize_peer_id_list(electorate)

                clean_reason = str(reason or '').strip() or None
                clean_proposal_id = str(proposal_id or '').strip() or f"CRP{secrets.token_hex(16)}"
                opened_ts = str(opened_at or '').strip() or self._format_db_timestamp(datetime.now(timezone.utc))
                conn.execute(
                    """
                    INSERT INTO channel_removal_proposals (
                        proposal_id, channel_id, channel_name, channel_origin_peer,
                        channel_privacy_mode, initiator_peer_id, initiator_user_id,
                        electorate_json, threshold_count, status, opened_at,
                        created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        clean_proposal_id,
                        clean_channel_id,
                        channel_row['name'],
                        channel_row['origin_peer'],
                        channel_row['privacy_mode'],
                        local_peer,
                        actor_user_id,
                        json.dumps(electorate),
                        len(electorate),
                        _CHANNEL_REMOVAL_OPEN_STATUS,
                        opened_ts,
                        opened_ts,
                        opened_ts,
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO channel_removal_votes (
                        proposal_id, channel_id, voter_peer_id, voter_user_id, vote, reason, cast_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        clean_proposal_id,
                        clean_channel_id,
                        local_peer,
                        actor_user_id,
                        'remove',
                        clean_reason,
                        opened_ts,
                    ),
                )
                proposal_row = self._get_open_channel_removal_proposal_row(conn, clean_channel_id)
                finalization = None
                if proposal_row:
                    finalization = self._finalize_channel_removal_locked(
                        conn,
                        proposal_row=proposal_row,
                        finalizing_peer_id=local_peer,
                        finalizing_user_id=actor_user_id,
                    )
                conn.commit()

            member_targets = self._channel_member_user_ids(clean_channel_id)
            event_reason = 'channel_removal_vote_opened'
            dedupe_suffix = f"removal_vote_opened:{clean_proposal_id}"
            if finalization and finalization.get('result') == _CHANNEL_REMOVAL_RETIRED_STATUS:
                event_reason = 'channel_retired_by_vote'
                dedupe_suffix = f"channel_retired_by_vote:{clean_proposal_id}"
            elif finalization and finalization.get('result') == _CHANNEL_REMOVAL_REJECTED_STATUS:
                event_reason = 'channel_removal_vote_rejected'
                dedupe_suffix = f"channel_removal_vote_rejected:{clean_proposal_id}"
            self._emit_channel_user_event(
                channel_id=clean_channel_id,
                event_type=EVENT_CHANNEL_STATE_UPDATED,
                actor_user_id=actor_user_id,
                target_user_ids=member_targets,
                payload={
                    'reason': event_reason,
                    'proposal_id': clean_proposal_id,
                },
                dedupe_suffix=dedupe_suffix,
            )
            status = self.get_channel_removal_status(
                clean_channel_id,
                local_peer_id=local_peer,
                viewer_user_id=actor_user_id,
                allow_admin=allow_admin,
                connected_peer_ids=connected_peer_ids,
                trusted_peer_ids=trusted_peer_ids,
            )
            return {
                'ok': True,
                'proposal_id': clean_proposal_id,
                'status': status,
                'finalization': finalization,
                'electorate_peer_ids': electorate,
            }
        except Exception as e:
            logger.error(f"Failed to start channel removal vote for {channel_id}: {e}", exc_info=True)
            return {'ok': False, 'error': 'internal_error'}

    def cast_channel_removal_vote(
        self,
        *,
        channel_id: str,
        proposal_id: str,
        user_id: str,
        local_peer_id: str,
        vote: str,
        allow_admin: bool = False,
        reason: Optional[str] = None,
        connected_peer_ids: Optional[List[Any]] = None,
        trusted_peer_ids: Optional[List[Any]] = None,
        cast_at: Optional[str] = None,
    ) -> Dict[str, Any]:
        clean_channel_id = str(channel_id or '').strip()
        clean_proposal_id = str(proposal_id or '').strip()
        actor_user_id = str(user_id or '').strip()
        local_peer = str(local_peer_id or '').strip()
        vote_value = str(vote or '').strip().lower()
        if vote_value not in _CHANNEL_REMOVAL_VOTE_VALUES or not clean_channel_id or not clean_proposal_id or not actor_user_id or not local_peer:
            return {'ok': False, 'error': 'invalid_request'}
        try:
            with self.db.get_connection() as conn:
                channel_row = conn.execute(
                    """
                    SELECT id, name, channel_type, origin_peer,
                           COALESCE(privacy_mode, 'open') AS privacy_mode,
                           COALESCE(lifecycle_preserved, 0) AS lifecycle_preserved
                    FROM channels
                    WHERE id = ?
                    """,
                    (clean_channel_id,),
                ).fetchone()
                if not channel_row:
                    return {'ok': False, 'error': 'channel_not_found'}
                if not (allow_admin or self.get_member_role(clean_channel_id, actor_user_id)):
                    return {'ok': False, 'error': 'not_authorized'}

                proposal_row = conn.execute(
                    """
                    SELECT *
                    FROM channel_removal_proposals
                    WHERE proposal_id = ?
                      AND channel_id = ?
                      AND status = ?
                    """,
                    (clean_proposal_id, clean_channel_id, _CHANNEL_REMOVAL_OPEN_STATUS),
                ).fetchone()
                if not proposal_row:
                    return {'ok': False, 'error': 'proposal_not_found'}

                electorate = self._load_channel_removal_electorate(proposal_row['electorate_json'])
                if local_peer not in electorate:
                    return {'ok': False, 'error': 'peer_not_in_electorate'}
                existing_vote = conn.execute(
                    """
                    SELECT vote
                    FROM channel_removal_votes
                    WHERE proposal_id = ? AND voter_peer_id = ?
                    """,
                    (clean_proposal_id, local_peer),
                ).fetchone()
                previous_vote = str(existing_vote['vote'] or '').strip().lower() if existing_vote else ''
                changed = True
                if existing_vote and previous_vote == vote_value:
                    changed = False
                cast_ts = str(cast_at or '').strip() or self._format_db_timestamp(datetime.now(timezone.utc))
                if existing_vote and changed:
                    conn.execute(
                        """
                        UPDATE channel_removal_votes
                        SET voter_user_id = ?, vote = ?, reason = ?, cast_at = ?
                        WHERE proposal_id = ? AND voter_peer_id = ?
                        """,
                        (
                            actor_user_id,
                            vote_value,
                            str(reason or '').strip() or None,
                            cast_ts,
                            clean_proposal_id,
                            local_peer,
                        ),
                    )
                elif not existing_vote:
                    conn.execute(
                        """
                        INSERT INTO channel_removal_votes (
                            proposal_id, channel_id, voter_peer_id, voter_user_id, vote, reason, cast_at
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            clean_proposal_id,
                            clean_channel_id,
                            local_peer,
                            actor_user_id,
                            vote_value,
                            str(reason or '').strip() or None,
                            cast_ts,
                        ),
                    )
                finalization = None
                if changed:
                    finalization = self._finalize_channel_removal_locked(
                        conn,
                        proposal_row=proposal_row,
                        finalizing_peer_id=local_peer,
                        finalizing_user_id=actor_user_id,
                    )
                conn.commit()
            if changed:
                event_reason = 'channel_removal_vote_updated'
                dedupe_suffix = f"channel_removal_vote_updated:{clean_proposal_id}:{local_peer}"
                if finalization and finalization.get('result') == _CHANNEL_REMOVAL_RETIRED_STATUS:
                    event_reason = 'channel_retired_by_vote'
                    dedupe_suffix = f"channel_retired_by_vote:{clean_proposal_id}"
                elif finalization and finalization.get('result') == _CHANNEL_REMOVAL_REJECTED_STATUS:
                    event_reason = 'channel_removal_vote_rejected'
                    dedupe_suffix = f"channel_removal_vote_rejected:{clean_proposal_id}"
                self._emit_channel_user_event(
                    channel_id=clean_channel_id,
                    event_type=EVENT_CHANNEL_STATE_UPDATED,
                    actor_user_id=actor_user_id,
                    payload={
                        'reason': event_reason,
                        'proposal_id': clean_proposal_id,
                        'vote': vote_value,
                        'previous_vote': previous_vote or None,
                        'voter_peer_id': local_peer,
                    },
                    dedupe_suffix=dedupe_suffix,
                )
            status = self.get_channel_removal_status(
                clean_channel_id,
                local_peer_id=local_peer,
                viewer_user_id=actor_user_id,
                allow_admin=allow_admin,
                connected_peer_ids=connected_peer_ids,
                trusted_peer_ids=trusted_peer_ids,
            )
            return {
                'ok': True,
                'status': status,
                'finalization': finalization,
                'changed': changed,
                'previous_vote': previous_vote or None,
            }
        except Exception as e:
            logger.error(f"Failed to cast channel removal vote for {channel_id}: {e}", exc_info=True)
            return {'ok': False, 'error': 'internal_error'}

    def receive_channel_removal_proposal(
        self,
        *,
        proposal_id: str,
        channel_id: str,
        channel_name: Optional[str],
        channel_origin_peer: Optional[str],
        channel_privacy_mode: Optional[str],
        initiator_peer_id: str,
        initiator_user_id: Optional[str],
        electorate_peer_ids: Optional[List[Any]],
        threshold_count: int,
        opened_at: Optional[str] = None,
        trusted_peer_ids: Optional[List[Any]] = None,
    ) -> bool:
        clean_proposal_id = str(proposal_id or '').strip()
        clean_channel_id = str(channel_id or '').strip()
        initiator_peer = str(initiator_peer_id or '').strip()
        electorate = self._normalize_peer_id_list(electorate_peer_ids)
        if not clean_proposal_id or not clean_channel_id or not initiator_peer or not electorate:
            return False
        trusted = set(self._normalize_peer_id_list(trusted_peer_ids))
        if trusted and initiator_peer not in trusted:
            logger.warning(
                "Ignoring channel removal proposal %s for %s from untrusted peer %s",
                clean_proposal_id,
                clean_channel_id,
                initiator_peer,
            )
            return False
        if initiator_peer not in electorate:
            logger.warning(
                "Ignoring channel removal proposal %s for %s because initiator %s is outside the electorate",
                clean_proposal_id,
                clean_channel_id,
                initiator_peer,
            )
            return False
        try:
            with self.db.get_connection() as conn:
                channel_row = conn.execute(
                    """
                    SELECT channel_type, COALESCE(lifecycle_preserved, 0) AS lifecycle_preserved
                    FROM channels
                    WHERE id = ?
                    """,
                    (clean_channel_id,),
                ).fetchone()
                if not channel_row:
                    logger.debug("Ignoring channel removal proposal for unknown channel %s", clean_channel_id)
                    return False
                if self._channel_removal_ineligible_reason(
                    channel_id=clean_channel_id,
                    channel_type=channel_row['channel_type'],
                    lifecycle_preserved=bool(channel_row['lifecycle_preserved']),
                ):
                    return False
                if self._get_active_channel_removal_tombstone_row(conn, clean_channel_id):
                    return False
                if self._get_open_channel_removal_proposal_row(conn, clean_channel_id):
                    return True
                opened_ts = str(opened_at or '').strip() or self._format_db_timestamp(datetime.now(timezone.utc))
                conn.execute(
                    """
                    INSERT OR IGNORE INTO channel_removal_proposals (
                        proposal_id, channel_id, channel_name, channel_origin_peer,
                        channel_privacy_mode, initiator_peer_id, initiator_user_id,
                        electorate_json, threshold_count, status, opened_at,
                        created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        clean_proposal_id,
                        clean_channel_id,
                        channel_name,
                        channel_origin_peer,
                        channel_privacy_mode,
                        initiator_peer,
                        initiator_user_id,
                        json.dumps(electorate),
                        max(1, int(threshold_count or len(electorate))),
                        _CHANNEL_REMOVAL_OPEN_STATUS,
                        opened_ts,
                        opened_ts,
                        opened_ts,
                    ),
                )
                conn.commit()
            self._emit_channel_user_event(
                channel_id=clean_channel_id,
                event_type=EVENT_CHANNEL_STATE_UPDATED,
                payload={
                    'reason': 'channel_removal_vote_opened',
                    'proposal_id': clean_proposal_id,
                },
                dedupe_suffix=f"channel_removal_vote_opened:{clean_proposal_id}",
            )
            return True
        except Exception as e:
            logger.error(f"Failed to receive channel removal proposal {proposal_id}: {e}", exc_info=True)
            return False

    def receive_channel_removal_vote(
        self,
        *,
        proposal_id: str,
        channel_id: str,
        voter_peer_id: str,
        voter_user_id: Optional[str],
        vote: str,
        reason: Optional[str] = None,
        cast_at: Optional[str] = None,
        trusted_peer_ids: Optional[List[Any]] = None,
    ) -> Dict[str, Any]:
        clean_proposal_id = str(proposal_id or '').strip()
        clean_channel_id = str(channel_id or '').strip()
        voter_peer = str(voter_peer_id or '').strip()
        vote_value = str(vote or '').strip().lower()
        if not clean_proposal_id or not clean_channel_id or not voter_peer or vote_value not in _CHANNEL_REMOVAL_VOTE_VALUES:
            return {'ok': False, 'error': 'invalid_request'}
        trusted = set(self._normalize_peer_id_list(trusted_peer_ids))
        if trusted and voter_peer not in trusted:
            return {'ok': False, 'error': 'untrusted_peer'}
        try:
            with self.db.get_connection() as conn:
                proposal_row = conn.execute(
                    """
                    SELECT *
                    FROM channel_removal_proposals
                    WHERE proposal_id = ?
                      AND channel_id = ?
                      AND status = ?
                    """,
                    (clean_proposal_id, clean_channel_id, _CHANNEL_REMOVAL_OPEN_STATUS),
                ).fetchone()
                if not proposal_row:
                    return {'ok': False, 'error': 'proposal_not_found'}
                electorate = self._load_channel_removal_electorate(proposal_row['electorate_json'])
                if voter_peer not in electorate:
                    return {'ok': False, 'error': 'peer_not_in_electorate'}
                existing_vote = conn.execute(
                    """
                    SELECT 1
                           , vote
                    FROM channel_removal_votes
                    WHERE proposal_id = ? AND voter_peer_id = ?
                    """,
                    (clean_proposal_id, voter_peer),
                ).fetchone()
                previous_vote = str(existing_vote['vote'] or '').strip().lower() if existing_vote else ''
                changed = True
                if existing_vote and previous_vote == vote_value:
                    changed = False
                cast_ts = str(cast_at or '').strip() or self._format_db_timestamp(datetime.now(timezone.utc))
                if existing_vote and changed:
                    conn.execute(
                        """
                        UPDATE channel_removal_votes
                        SET voter_user_id = ?, vote = ?, reason = ?, cast_at = ?
                        WHERE proposal_id = ? AND voter_peer_id = ?
                        """,
                        (
                            str(voter_user_id or '').strip() or None,
                            vote_value,
                            str(reason or '').strip() or None,
                            cast_ts,
                            clean_proposal_id,
                            voter_peer,
                        ),
                    )
                elif not existing_vote:
                    conn.execute(
                        """
                        INSERT INTO channel_removal_votes (
                            proposal_id, channel_id, voter_peer_id, voter_user_id, vote, reason, cast_at
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            clean_proposal_id,
                            clean_channel_id,
                            voter_peer,
                            str(voter_user_id or '').strip() or None,
                            vote_value,
                            str(reason or '').strip() or None,
                            cast_ts,
                        ),
                    )
                finalization = None
                if changed:
                    finalization = self._finalize_channel_removal_locked(
                        conn,
                        proposal_row=proposal_row,
                        finalizing_peer_id=voter_peer,
                        finalizing_user_id=str(voter_user_id or '').strip() or None,
                    )
                conn.commit()
            if changed:
                event_reason = 'channel_removal_vote_updated'
                dedupe_suffix = f"channel_removal_vote_updated:{clean_proposal_id}:{voter_peer}"
                if finalization and finalization.get('result') == _CHANNEL_REMOVAL_RETIRED_STATUS:
                    event_reason = 'channel_retired_by_vote'
                    dedupe_suffix = f"channel_retired_by_vote:{clean_proposal_id}"
                elif finalization and finalization.get('result') == _CHANNEL_REMOVAL_REJECTED_STATUS:
                    event_reason = 'channel_removal_vote_rejected'
                    dedupe_suffix = f"channel_removal_vote_rejected:{clean_proposal_id}"
                self._emit_channel_user_event(
                    channel_id=clean_channel_id,
                    event_type=EVENT_CHANNEL_STATE_UPDATED,
                    actor_user_id=str(voter_user_id or '').strip() or None,
                    payload={
                        'reason': event_reason,
                        'proposal_id': clean_proposal_id,
                        'vote': vote_value,
                        'previous_vote': previous_vote or None,
                        'voter_peer_id': voter_peer,
                    },
                    dedupe_suffix=dedupe_suffix,
                )
            return {'ok': True, 'finalization': finalization, 'changed': changed, 'previous_vote': previous_vote or None}
        except Exception as e:
            logger.error(f"Failed to receive channel removal vote {proposal_id}: {e}", exc_info=True)
            return {'ok': False, 'error': 'internal_error'}

    def apply_channel_removal_result(
        self,
        *,
        proposal_id: str,
        channel_id: str,
        result: str,
        electorate_peer_ids: Optional[List[Any]],
        threshold_count: int,
        finalizing_peer_id: str,
        finalizing_user_id: Optional[str] = None,
        tombstone_id: Optional[str] = None,
        finalized_at: Optional[str] = None,
        trusted_peer_ids: Optional[List[Any]] = None,
    ) -> bool:
        clean_proposal_id = str(proposal_id or '').strip()
        clean_channel_id = str(channel_id or '').strip()
        result_value = str(result or '').strip().lower()
        if result_value not in {_CHANNEL_REMOVAL_RETIRED_STATUS, _CHANNEL_REMOVAL_REJECTED_STATUS}:
            return False
        electorate = self._normalize_peer_id_list(electorate_peer_ids)
        if not clean_proposal_id or not clean_channel_id or not electorate:
            return False
        finalizer_peer = str(finalizing_peer_id or '').strip()
        if not finalizer_peer:
            logger.warning(
                "Ignoring channel removal result %s for %s because finalizing peer is missing",
                clean_proposal_id,
                clean_channel_id,
            )
            return False
        trusted = set(self._normalize_peer_id_list(trusted_peer_ids))
        if trusted and finalizer_peer not in trusted:
            logger.warning(
                "Ignoring channel removal result %s for %s from untrusted peer %s",
                clean_proposal_id,
                clean_channel_id,
                finalizer_peer,
            )
            return False
        if finalizer_peer and finalizer_peer not in electorate:
            logger.warning(
                "Ignoring channel removal result %s for %s because finalizer %s is outside the electorate",
                clean_proposal_id,
                clean_channel_id,
                finalizer_peer,
            )
            return False
        try:
            with self.db.get_connection() as conn:
                channel_row = conn.execute(
                    """
                    SELECT name, origin_peer, COALESCE(privacy_mode, 'open') AS privacy_mode
                    FROM channels
                    WHERE id = ?
                    """,
                    (clean_channel_id,),
                ).fetchone()
                if not channel_row:
                    return False
                proposal_row = conn.execute(
                    """
                    SELECT *
                    FROM channel_removal_proposals
                    WHERE proposal_id = ? AND channel_id = ?
                    """,
                    (clean_proposal_id, clean_channel_id),
                ).fetchone()
                now_ts = str(finalized_at or '').strip() or self._format_db_timestamp(datetime.now(timezone.utc))
                if not proposal_row:
                    conn.execute(
                        """
                        INSERT INTO channel_removal_proposals (
                            proposal_id, channel_id, channel_name, channel_origin_peer,
                            channel_privacy_mode, initiator_peer_id, initiator_user_id,
                            electorate_json, threshold_count, status, final_result,
                            opened_at, finalized_at, created_at, updated_at
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            clean_proposal_id,
                            clean_channel_id,
                            channel_row['name'],
                            channel_row['origin_peer'],
                            channel_row['privacy_mode'],
                            str(finalizing_peer_id or '').strip(),
                            str(finalizing_user_id or '').strip() or None,
                            json.dumps(electorate),
                            max(1, int(threshold_count or len(electorate))),
                            result_value,
                            result_value,
                            now_ts,
                            now_ts,
                            now_ts,
                            now_ts,
                        ),
                    )
                else:
                    stored_electorate = self._load_channel_removal_electorate(proposal_row['electorate_json'])
                    stored_threshold = max(1, int(proposal_row['threshold_count'] or len(stored_electorate) or 0))
                    if stored_electorate != electorate or stored_threshold != max(1, int(threshold_count or len(electorate) or 0)):
                        logger.warning(
                            "Ignoring channel removal result %s for %s due to electorate/threshold mismatch",
                            clean_proposal_id,
                            clean_channel_id,
                        )
                        return False
                    electorate = stored_electorate
                    threshold_count = stored_threshold
                    # Reject a remote terminal result when our locally stored ballots
                    # already prove the opposite outcome.
                    vote_rows = conn.execute(
                        """
                        SELECT voter_peer_id, vote
                        FROM channel_removal_votes
                        WHERE proposal_id = ?
                        """,
                        (clean_proposal_id,),
                    ).fetchall()
                    electorate_set = set(stored_electorate)
                    stored_votes = {
                        str(row['voter_peer_id'] or '').strip(): str(row['vote'] or '').strip().lower()
                        for row in vote_rows or []
                        if str(row['voter_peer_id'] or '').strip() in electorate_set
                    }
                    stored_remove_votes = sum(1 for vote in stored_votes.values() if vote == 'remove')
                    stored_keep_votes = sum(1 for vote in stored_votes.values() if vote == 'keep')
                    if result_value == _CHANNEL_REMOVAL_RETIRED_STATUS and stored_keep_votes > 0:
                        logger.warning(
                            "Ignoring channel removal result %s for %s because locally stored keep votes contradict retirement",
                            clean_proposal_id,
                            clean_channel_id,
                        )
                        return False
                    if result_value == _CHANNEL_REMOVAL_REJECTED_STATUS and threshold_count > 0 and stored_remove_votes >= threshold_count:
                        logger.warning(
                            "Ignoring channel removal result %s for %s because locally stored remove votes already meet the retirement threshold",
                            clean_proposal_id,
                            clean_channel_id,
                        )
                        return False
                    conn.execute(
                        """
                        UPDATE channel_removal_proposals
                        SET status = ?, final_result = ?,
                            tombstone_id = COALESCE(?, tombstone_id), finalized_at = ?, updated_at = ?
                        WHERE proposal_id = ?
                        """,
                        (
                            result_value,
                            result_value,
                            str(tombstone_id or '').strip() or None,
                            now_ts,
                            now_ts,
                            clean_proposal_id,
                        ),
                    )

                if result_value == _CHANNEL_REMOVAL_RETIRED_STATUS:
                    active_tombstone = self._get_active_channel_removal_tombstone_row(conn, clean_channel_id)
                    if not active_tombstone:
                        clean_tombstone_id = str(tombstone_id or '').strip() or f"CRT{secrets.token_hex(16)}"
                        conn.execute(
                            """
                            INSERT INTO channel_removal_tombstones (
                                tombstone_id, channel_id, proposal_id, channel_name,
                                channel_origin_peer, electorate_json, threshold_count,
                                retired_at, retired_by_peer_id, retired_by_user_id
                            )
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                clean_tombstone_id,
                                clean_channel_id,
                                clean_proposal_id,
                                channel_row['name'],
                                channel_row['origin_peer'],
                                json.dumps(electorate),
                                max(1, int(threshold_count or len(electorate))),
                                now_ts,
                                str(finalizing_peer_id or '').strip(),
                                str(finalizing_user_id or '').strip() or None,
                            ),
                        )
                conn.commit()
            self._emit_channel_user_event(
                channel_id=clean_channel_id,
                event_type=EVENT_CHANNEL_STATE_UPDATED,
                actor_user_id=str(finalizing_user_id or '').strip() or None,
                payload={
                    'reason': 'channel_retired_by_vote' if result_value == _CHANNEL_REMOVAL_RETIRED_STATUS else 'channel_removal_vote_rejected',
                    'proposal_id': clean_proposal_id,
                },
                dedupe_suffix=f"channel_removal_result:{clean_proposal_id}:{result_value}",
            )
            return True
        except Exception as e:
            logger.error(f"Failed to apply channel removal result for {proposal_id}: {e}", exc_info=True)
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
                if self._get_active_channel_removal_tombstone_row(conn, channel_id):
                    decision['reason'] = 'retired_by_vote'
                    return decision
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
                               c.origin_peer,
                               COUNT(DISTINCT cm.user_id) AS member_count,
                               MAX(CASE WHEN cmu.user_id IS NOT NULL THEN 1 ELSE 0 END) AS is_member
                        FROM channels c
                        LEFT JOIN channel_members cm ON cm.channel_id = c.id
                        LEFT JOIN channel_members cmu
                          ON cmu.channel_id = c.id AND cmu.user_id = ?
                        GROUP BY c.id, c.name, c.channel_type, COALESCE(c.privacy_mode, 'open'), c.origin_peer
                        ORDER BY CASE WHEN c.id = 'general' THEN 0 ELSE 1 END, LOWER(c.name) ASC
                        """,
                        (user_id,),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        """
                        SELECT c.id, c.name, c.channel_type, COALESCE(c.privacy_mode, 'open') AS privacy_mode,
                               c.origin_peer,
                               COUNT(DISTINCT cm.user_id) AS member_count
                        FROM channels c
                        LEFT JOIN channel_members cm ON cm.channel_id = c.id
                        GROUP BY c.id, c.name, c.channel_type, COALESCE(c.privacy_mode, 'open'), c.origin_peer
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
                        'origin_peer': row['origin_peer'],
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
    def _normalize_post_policy(cls, policy: Any, default: str = POST_POLICY_OPEN) -> str:
        """Normalize channel posting policy to a known value."""
        candidate = str(policy or '').strip().lower()
        if candidate in cls.ALLOWED_POST_POLICIES:
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
                      post_policy: str = POST_POLICY_OPEN,
                      allow_member_replies: bool = True,
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
                post_policy=self._normalize_post_policy(post_policy, default=self.POST_POLICY_OPEN),
                allow_member_replies=bool(allow_member_replies),
                user_role='admin',
                can_post_top_level=True,
                can_reply=True,
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
                            post_policy, allow_member_replies,
                            lifecycle_ttl_days, lifecycle_preserved
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                        channel.post_policy,
                        1 if channel.allow_member_replies else 0,
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

                    self._ensure_public_channel_membership_conn(
                        conn,
                        channel_id,
                        channel_type.value,
                        privacy_mode,
                        fallback_user_id=created_by,
                    )
                    
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
                           COALESCE(post_policy, ?) AS post_policy,
                           COALESCE(allow_member_replies, 1) AS allow_member_replies,
                           COALESCE(last_activity_at, created_at) AS last_activity_at,
                           COALESCE(lifecycle_ttl_days, ?) AS lifecycle_ttl_days,
                           COALESCE(lifecycle_preserved, 0) AS lifecycle_preserved,
                           lifecycle_archived_at,
                           lifecycle_archive_reason
                    FROM channels
                    WHERE (channel_type = 'public' OR channel_type = 'general')
                      AND COALESCE(privacy_mode, 'open') NOT IN ('private', 'confidential')
                      AND NOT EXISTS (
                          SELECT 1
                          FROM channel_removal_tombstones crt
                          WHERE crt.channel_id = channels.id
                            AND crt.restored_at IS NULL
                      )
                """, (self.POST_POLICY_OPEN, self.DEFAULT_CHANNEL_LIFECYCLE_DAYS)).fetchall()
                channel_ids = [str(r[0]) for r in rows if r and r[0]]
                allowed_by_channel: Dict[str, List[str]] = {}
                if channel_ids:
                    placeholders = ",".join("?" for _ in channel_ids)
                    allowed_rows = conn.execute(
                        f"""
                        SELECT channel_id, user_id
                        FROM channel_post_permissions
                        WHERE channel_id IN ({placeholders})
                        ORDER BY channel_id ASC, granted_at ASC, user_id ASC
                        """,
                        channel_ids,
                    ).fetchall()
                    for allowed_row in allowed_rows:
                        channel_key = str(allowed_row[0] or "").strip()
                        user_key = str(allowed_row[1] or "").strip()
                        if not channel_key or not user_key:
                            continue
                        allowed_by_channel.setdefault(channel_key, []).append(user_key)
                return [
                    {
                        'id': r[0],
                        'name': r[1],
                        'type': r[2],
                        'desc': r[3] or '',
                        'origin_peer': r[5] or '',
                        'privacy_mode': r[6] or 'open',
                        'post_policy': self._normalize_post_policy(r[7], default=self.POST_POLICY_OPEN),
                        'allow_member_replies': bool(r[8]),
                        'allowed_poster_user_ids': list(allowed_by_channel.get(str(r[0]), [])),
                        'last_activity_at': r[9],
                        'lifecycle_ttl_days': r[10],
                        'lifecycle_preserved': bool(r[11]),
                        'lifecycle_archived_at': r[12],
                        'lifecycle_archive_reason': r[13],
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
                                  post_policy: str = POST_POLICY_OPEN,
                                  allow_member_replies: bool = True,
                                  allowed_poster_user_ids: Optional[List[str]] = None,
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
                if self._get_active_channel_removal_tombstone_row(conn, channel_id):
                    logger.info(
                        "Ignoring synced channel %s because the channel is retired by vote",
                        channel_id,
                    )
                    return None
                # Check if channel already exists
                existing = conn.execute(
                    "SELECT 1 FROM channels WHERE id = ?", (channel_id,)
                ).fetchone()
                if existing:
                    self.sync_channel_post_permissions(
                        channel_id,
                        post_policy=post_policy,
                        allow_member_replies=allow_member_replies,
                        allowed_poster_user_ids=allowed_poster_user_ids,
                    )
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
                        post_policy, allow_member_replies,
                        lifecycle_ttl_days, lifecycle_preserved,
                        lifecycle_archived_at, lifecycle_archive_reason
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    self._normalize_post_policy(post_policy, default=self.POST_POLICY_OPEN),
                    1 if allow_member_replies else 0,
                    ttl_days,
                    1 if preserved else 0,
                    self._format_db_timestamp(archived_dt) if archived_dt else None,
                    lifecycle_archive_reason,
                ))

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
                                logger.debug(f"Added targeted member {uid} to channel {channel_id}")
                else:
                    self._ensure_public_channel_membership_conn(
                        conn,
                        channel_id,
                        channel_type,
                        privacy_mode,
                        fallback_user_id=local_user_id,
                    )

                conn.commit()

            self.sync_channel_post_permissions(
                channel_id,
                post_policy=post_policy,
                allow_member_replies=allow_member_replies,
                allowed_poster_user_ids=allowed_poster_user_ids,
            )

            channel = Channel(
                id=channel_id,
                name=name,
                channel_type=ChannelType(channel_type) if channel_type in [e.value for e in ChannelType] else ChannelType.PUBLIC,
                created_by=sync_creator_id,
                created_at=created_at,
                last_activity_at=last_activity_dt,
                description=description,
                privacy_mode=privacy_mode or 'open',
                post_policy=self._normalize_post_policy(post_policy, default=self.POST_POLICY_OPEN),
                allow_member_replies=bool(allow_member_replies),
                can_post_top_level=True,
                can_reply=True,
                allowed_poster_count=len(list(allowed_poster_user_ids or [])),
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
                                post_policy: str = POST_POLICY_OPEN,
                                allow_member_replies: bool = True,
                                allowed_poster_user_ids: Optional[List[str]] = None,
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
        post_policy = self._normalize_post_policy(post_policy, default=self.POST_POLICY_OPEN)
        # General channel is always open, never downgraded by remote metadata
        if remote_id == 'general':
            privacy_mode = 'open'
            lifecycle_preserved = True
        try:
            with self.db.get_connection() as conn:
                if self._get_active_channel_removal_tombstone_row(conn, remote_id):
                    logger.info(
                        "Ignoring channel announce for %s because the channel is retired by vote",
                        remote_id,
                    )
                    return None
                # Already have this exact channel?
                existing = conn.execute(
                    "SELECT name, description, channel_type, privacy_mode, origin_peer, created_by FROM channels WHERE id = ?",
                    (remote_id,)
                ).fetchone()
                if existing:
                    old_name = existing[0] or ''
                    old_desc = existing[1] or ''
                    old_type = str(existing[2] or 'public').strip().lower()
                    old_privacy = self._normalize_privacy_mode(existing[3], default='open')
                    old_origin_peer = existing[4] or ''
                    old_created_by = existing[5] or ''

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
                    new_type = old_type
                    new_privacy = old_privacy
                    new_post_policy = post_policy
                    new_allow_member_replies = bool(allow_member_replies)
                    if (can_apply_remote_metadata and old_name.startswith('peer-channel-') and remote_name
                            and not remote_name.startswith('peer-channel-')):
                        new_name = remote_name
                        needs_update = True
                    if (can_apply_remote_metadata and (not old_desc or old_desc.startswith('Auto-created from P2P'))
                            and remote_desc
                            and not remote_desc.startswith('Auto-created from P2P')):
                        new_desc = remote_desc
                        needs_update = True
                    normalized_remote_type = str(remote_type or '').strip().lower() or old_type
                    incoming_looks_public = self._is_public_channel(
                        normalized_remote_type,
                        privacy_mode or old_privacy,
                    )
                    old_has_placeholder_marker = self._has_placeholder_channel_marker(
                        old_name,
                        old_desc,
                    )
                    old_is_placeholder = self._is_placeholder_channel_signature(
                        old_name,
                        old_desc,
                        old_type,
                        old_privacy,
                    )
                    remote_has_canonical_name = bool(
                        remote_name
                        and not str(remote_name or '').strip().startswith('peer-channel-')
                    )
                    remote_has_canonical_desc = bool(
                        remote_desc
                        and not str(remote_desc or '').strip().startswith('Auto-created from P2P')
                    )
                    trusted_placeholder_hint = bool(
                        old_has_placeholder_marker
                        and not can_apply_remote_metadata
                        and incoming_looks_public
                        and (
                            remote_has_canonical_name
                            or remote_has_canonical_desc
                            or normalized_remote_type != old_type
                            or privacy_mode != old_privacy
                        )
                    )
                    if trusted_placeholder_hint:
                        callback = getattr(self, 'public_placeholder_reconcile_callback', None)
                        if callable(callback):
                            try:
                                callback(
                                    channel_id=remote_id,
                                    origin_peer=old_origin_peer,
                                    observed_from_peer=str(from_peer or '').strip(),
                                    remote_name=remote_name,
                                    remote_type=normalized_remote_type,
                                    privacy_mode=privacy_mode,
                                )
                            except Exception as callback_err:
                                logger.debug(
                                    "Placeholder reconcile callback failed for %s: %s",
                                    remote_id,
                                    callback_err,
                                )
                    if can_apply_remote_metadata and normalized_remote_type in {'public', 'private', 'general'} and normalized_remote_type != old_type:
                        new_type = normalized_remote_type
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
                                   COALESCE(post_policy, ?) AS post_policy,
                                   COALESCE(allow_member_replies, 1) AS allow_member_replies,
                                   last_activity_at,
                                   lifecycle_archived_at, lifecycle_archive_reason
                            FROM channels WHERE id = ?
                            """,
                            (self.POST_POLICY_OPEN, remote_id),
                        ).fetchone()
                        if extra_row:
                            old_ttl_days = extra_row['lifecycle_ttl_days']
                            old_preserved = bool(extra_row['lifecycle_preserved'])
                            old_post_policy = self._normalize_post_policy(extra_row['post_policy'], default=self.POST_POLICY_OPEN)
                            old_allow_member_replies = bool(extra_row['allow_member_replies'])
                            old_last_activity = self._parse_datetime(extra_row['last_activity_at'])
                            old_archived_at = extra_row['lifecycle_archived_at']
                            old_archive_reason = extra_row['lifecycle_archive_reason']
                        else:
                            old_post_policy = self.POST_POLICY_OPEN
                            old_allow_member_replies = True
                    except Exception:
                        old_post_policy = self.POST_POLICY_OPEN
                        old_allow_member_replies = True
                    if can_apply_remote_metadata:
                        if ttl_days and ttl_days != self._normalize_channel_lifecycle_ttl_days(old_ttl_days, default=ttl_days):
                            needs_update = True
                        if bool(lifecycle_preserved) != bool(old_preserved):
                            needs_update = True
                        if post_policy != old_post_policy:
                            needs_update = True
                        if bool(allow_member_replies) != bool(old_allow_member_replies):
                            needs_update = True
                        if incoming_last_activity and (old_last_activity is None or incoming_last_activity > old_last_activity):
                            needs_update = True
                        if bool(archived_dt) != bool(self._parse_datetime(old_archived_at)):
                            needs_update = True
                        if lifecycle_archive_reason != old_archive_reason:
                            needs_update = True
                    if needs_update:
                        try:
                            if old_has_placeholder_marker and remote_has_canonical_name:
                                logger.info(
                                    "Placeholder reconcile DB update attempt for %s "
                                    "(from_peer=%s can_apply=%s old_name=%s new_name=%s)",
                                    remote_id,
                                    from_peer,
                                    can_apply_remote_metadata,
                                    old_name,
                                    new_name,
                                )
                            conn.execute(
                                """
                                UPDATE channels
                                   SET name = ?,
                                       description = ?,
                                       channel_type = ?,
                                       privacy_mode = ?,
                                       post_policy = ?,
                                       allow_member_replies = ?,
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
                                    new_type,
                                    new_privacy,
                                    new_post_policy,
                                    1 if new_allow_member_replies else 0,
                                    from_peer,
                                    self._format_db_timestamp(incoming_last_activity) if incoming_last_activity else None,
                                    ttl_days,
                                    1 if lifecycle_preserved else 0,
                                    self._format_db_timestamp(archived_dt) if archived_dt else None,
                                    lifecycle_archive_reason,
                                    remote_id,
                                )
                            )
                            self._ensure_public_channel_membership_conn(
                                conn,
                                remote_id,
                                new_type,
                                new_privacy,
                                fallback_user_id=local_user_id,
                            )
                            conn.commit()
                            if old_has_placeholder_marker and remote_has_canonical_name:
                                readback = conn.execute(
                                    "SELECT name, channel_type, privacy_mode FROM channels WHERE id = ?",
                                    (remote_id,),
                                ).fetchone()
                                logger.info(
                                    "Placeholder reconcile DB update committed for %s "
                                    "(name=%s type=%s privacy=%s)",
                                    remote_id,
                                    str((readback['name'] if readback and hasattr(readback, 'keys') else (readback[0] if readback else '')) or '').strip() or 'missing',
                                    str((readback['channel_type'] if readback and hasattr(readback, 'keys') else (readback[1] if readback else '')) or '').strip() or 'unknown',
                                    str((readback['privacy_mode'] if readback and hasattr(readback, 'keys') else (readback[2] if readback else '')) or '').strip() or 'unknown',
                                )
                            self.apply_remote_channel_posting_snapshot(
                                remote_id,
                                from_peer,
                                post_policy=post_policy,
                                allow_member_replies=allow_member_replies,
                                allowed_poster_user_ids=allowed_poster_user_ids,
                                log_context='channel_announce_update',
                            )
                            logger.info(f"Updated channel {remote_id}: "
                                        f"name='{old_name}'->'{new_name}', "
                                        f"desc updated={old_desc != new_desc}")
                            return remote_id
                        except Exception as ue:
                            logger.warning(f"Channel update for {remote_id} failed: {ue}")
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
                    self.apply_remote_channel_posting_snapshot(
                        remote_id,
                        from_peer,
                        post_policy=post_policy,
                        allow_member_replies=allow_member_replies,
                        allowed_poster_user_ids=allowed_poster_user_ids,
                        log_context='channel_announce_existing',
                    )
                    with self.db.get_connection() as membership_conn:
                        self._ensure_public_channel_membership_conn(
                            membership_conn,
                            remote_id,
                            old_type,
                            privacy_mode,
                            fallback_user_id=local_user_id,
                        )
                        membership_conn.commit()
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
                                post_policy, allow_member_replies,
                                lifecycle_ttl_days, lifecycle_preserved,
                                lifecycle_archived_at, lifecycle_archive_reason
                            )
                            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """, (
                            remote_id,
                            remote_name,
                            remote_type,
                            sync_creator_id,
                            self._format_db_timestamp(self._parse_datetime(last_activity_at) or datetime.now(timezone.utc)),
                            remote_desc,
                            from_peer,
                            privacy_mode or 'open',
                            post_policy,
                            1 if allow_member_replies else 0,
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

                        self._ensure_public_channel_membership_conn(
                            conn,
                            remote_id,
                            remote_type,
                            privacy_mode,
                            fallback_user_id=local_user_id,
                        )

                        conn.commit()
                        self.sync_channel_post_permissions(
                            remote_id,
                            post_policy=post_policy,
                            allow_member_replies=allow_member_replies,
                            allowed_poster_user_ids=allowed_poster_user_ids,
                        )
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
                            post_policy=post_policy,
                            allow_member_replies=allow_member_replies,
                            allowed_poster_user_ids=allowed_poster_user_ids,
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
                post_policy=post_policy,
                allow_member_replies=allow_member_replies,
                allowed_poster_user_ids=allowed_poster_user_ids,
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

    def _load_channel_posting_state_conn(
        self,
        conn: Any,
        channel_id: str,
        user_id: Optional[str],
        *,
        allow_admin: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """Load the effective posting state for a channel/user pair."""
        channel_row = conn.execute(
            """
            SELECT c.id,
                   c.name,
                   c.created_by,
                   c.channel_type,
                   c.origin_peer,
                   COALESCE(c.post_policy, ?) AS post_policy,
                   COALESCE(c.allow_member_replies, 1) AS allow_member_replies,
                   COALESCE(cm.role, 'member') AS user_role,
                   EXISTS(
                       SELECT 1
                       FROM channel_post_permissions cpp
                       WHERE cpp.channel_id = c.id
                         AND cpp.user_id = ?
                   ) AS explicit_post_permission,
                   (
                       SELECT COUNT(*)
                       FROM channel_post_permissions cpp
                       WHERE cpp.channel_id = c.id
                   ) AS allowed_poster_count
            FROM channels c
            LEFT JOIN channel_members cm
              ON cm.channel_id = c.id
             AND cm.user_id = ?
            WHERE c.id = ?
            """,
            (self.POST_POLICY_OPEN, user_id, user_id, channel_id),
        ).fetchone()
        if not channel_row:
            return None

        role = str(channel_row['user_role'] or 'member').strip().lower()
        creator_id = str(channel_row['created_by'] or '').strip()
        clean_user_id = str(user_id or '').strip()
        explicit_post_permission = bool(channel_row['explicit_post_permission'])
        is_admin_like = bool(
            allow_admin
            or role == 'admin'
            or (creator_id and creator_id == clean_user_id)
        )
        post_policy = self._normalize_post_policy(channel_row['post_policy'], default=self.POST_POLICY_OPEN)
        allow_member_replies = bool(channel_row['allow_member_replies'])
        can_post_top_level = bool(
            post_policy == self.POST_POLICY_OPEN
            or is_admin_like
            or explicit_post_permission
        )
        can_reply = bool(allow_member_replies or can_post_top_level)

        return {
            'channel_id': str(channel_row['id'] or '').strip(),
            'channel_name': str(channel_row['name'] or '').strip(),
            'channel_type': str(channel_row['channel_type'] or 'public').strip().lower(),
            'created_by': creator_id,
            'origin_peer': str(channel_row['origin_peer'] or '').strip(),
            'post_policy': post_policy,
            'allow_member_replies': allow_member_replies,
            'user_role': role or 'member',
            'explicit_post_permission': explicit_post_permission,
            'allowed_poster_count': int(channel_row['allowed_poster_count'] or 0),
            'is_admin_like': is_admin_like,
            'can_post_top_level': can_post_top_level,
            'can_reply': can_reply,
        }

    def get_channel_posting_state(
        self,
        channel_id: str,
        user_id: Optional[str],
        *,
        allow_admin: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """Return the effective posting state for a channel/user pair."""
        if not channel_id:
            return None
        try:
            with self.db.get_connection() as conn:
                return self._load_channel_posting_state_conn(
                    conn,
                    channel_id,
                    user_id,
                    allow_admin=allow_admin,
                )
        except Exception as e:
            logger.error(
                "Failed to load channel posting state channel=%s user=%s: %s",
                channel_id,
                user_id,
                e,
                exc_info=True,
            )
            return None

    def get_channel_allowed_poster_ids(self, channel_id: str) -> List[str]:
        """Return the explicit curated-poster allowlist for a channel."""
        if not channel_id:
            return []
        try:
            with self.db.get_connection() as conn:
                rows = conn.execute(
                    """
                    SELECT user_id
                    FROM channel_post_permissions
                    WHERE channel_id = ?
                    ORDER BY granted_at ASC, user_id ASC
                    """,
                    (channel_id,),
                ).fetchall()
            return [str(row['user_id']) for row in rows if row and row['user_id']]
        except Exception as e:
            logger.error(f"Failed to load curated posters for {channel_id}: {e}", exc_info=True)
            return []

    def get_channel_posting_snapshot(self, channel_id: str) -> Dict[str, Any]:
        """Return the channel posting-policy snapshot for sync/broadcast use."""
        if not channel_id:
            return {
                'post_policy': self.POST_POLICY_OPEN,
                'allow_member_replies': True,
                'allowed_poster_user_ids': [],
            }
        try:
            with self.db.get_connection() as conn:
                row = conn.execute(
                    """
                    SELECT COALESCE(post_policy, ?) AS post_policy,
                           COALESCE(allow_member_replies, 1) AS allow_member_replies
                    FROM channels
                    WHERE id = ?
                    """,
                    (self.POST_POLICY_OPEN, channel_id),
                ).fetchone()
                allowed_rows = conn.execute(
                    """
                    SELECT user_id
                    FROM channel_post_permissions
                    WHERE channel_id = ?
                    ORDER BY granted_at ASC, user_id ASC
                    """,
                    (channel_id,),
                ).fetchall()
            return {
                'post_policy': self._normalize_post_policy(
                    row['post_policy'] if row else self.POST_POLICY_OPEN,
                    default=self.POST_POLICY_OPEN,
                ),
                'allow_member_replies': bool(
                    row['allow_member_replies'] if row else True
                ),
                'allowed_poster_user_ids': [
                    str(allowed_row['user_id'])
                    for allowed_row in allowed_rows
                    if allowed_row and allowed_row['user_id']
                ],
            }
        except Exception as e:
            logger.error(
                "Failed to load channel posting snapshot for %s: %s",
                channel_id,
                e,
                exc_info=True,
            )
            return {
                'post_policy': self.POST_POLICY_OPEN,
                'allow_member_replies': True,
                'allowed_poster_user_ids': [],
            }

    @staticmethod
    def _normalize_allowed_poster_ids(allowed_poster_user_ids: Optional[List[str]]) -> List[str]:
        """Normalize and deduplicate curated-poster user ids."""
        allowed_ids: List[str] = []
        seen_ids: set[str] = set()
        for raw_user_id in allowed_poster_user_ids or []:
            clean_user_id = str(raw_user_id or '').strip()
            if not clean_user_id or clean_user_id in seen_ids:
                continue
            seen_ids.add(clean_user_id)
            allowed_ids.append(clean_user_id)
        return allowed_ids

    def _sync_channel_post_permissions_conn(
        self,
        conn: Any,
        channel_id: str,
        *,
        post_policy: Optional[str] = None,
        allow_member_replies: Optional[bool] = None,
        allowed_poster_user_ids: Optional[List[str]] = None,
    ) -> bool:
        """Low-level posting-policy sync inside an existing DB transaction."""
        normalized_policy = self._normalize_post_policy(post_policy, default=self.POST_POLICY_OPEN)
        allowed_ids = self._normalize_allowed_poster_ids(allowed_poster_user_ids)
        conn.execute(
            """
            UPDATE channels
               SET post_policy = ?,
                   allow_member_replies = ?
             WHERE id = ?
            """,
            (
                normalized_policy,
                1 if (True if allow_member_replies is None else bool(allow_member_replies)) else 0,
                channel_id,
            ),
        )
        conn.execute(
            "DELETE FROM channel_post_permissions WHERE channel_id = ?",
            (channel_id,),
        )
        for allowed_user_id in allowed_ids:
            user_row = conn.execute(
                "SELECT 1 FROM users WHERE id = ?",
                (allowed_user_id,),
            ).fetchone()
            if not user_row:
                continue
            conn.execute(
                """
                INSERT OR IGNORE INTO channel_post_permissions
                (channel_id, user_id, granted_by)
                VALUES (?, ?, NULL)
                """,
                (channel_id, allowed_user_id),
            )
        return True

    def apply_remote_channel_posting_snapshot(
        self,
        channel_id: str,
        from_peer: Optional[str],
        *,
        post_policy: Optional[str] = None,
        allow_member_replies: Optional[bool] = None,
        allowed_poster_user_ids: Optional[List[str]] = None,
        log_context: str = 'remote_channel_sync',
    ) -> bool:
        """Apply synced posting metadata only when the sender is authoritative."""
        clean_channel_id = str(channel_id or '').strip()
        clean_from_peer = str(from_peer or '').strip()
        if not clean_channel_id:
            return False
        if clean_channel_id == 'general':
            logger.debug(
                "Ignoring remote posting snapshot for general via %s from %s",
                log_context,
                clean_from_peer or 'unknown',
            )
            return False
        try:
            with self.db.get_connection() as conn:
                row = conn.execute(
                    """
                    SELECT origin_peer,
                           created_by,
                           COALESCE(post_policy, ?) AS post_policy,
                           COALESCE(allow_member_replies, 1) AS allow_member_replies
                    FROM channels
                    WHERE id = ?
                    """,
                    (self.POST_POLICY_OPEN, clean_channel_id),
                ).fetchone()
                if not row:
                    logger.warning(
                        "Ignoring remote posting snapshot for unknown channel %s via %s from %s",
                        clean_channel_id,
                        log_context,
                        clean_from_peer or 'unknown',
                    )
                    return False

                origin_peer = str(row['origin_peer'] or '').strip()
                created_by = str(row['created_by'] or '').strip()
                current_policy = self._normalize_post_policy(
                    row['post_policy'],
                    default=self.POST_POLICY_OPEN,
                )
                current_allow_member_replies = bool(row['allow_member_replies'])

                can_apply = False
                authority_reason = 'origin_mismatch'
                if origin_peer:
                    can_apply = bool(clean_from_peer and clean_from_peer == origin_peer)
                    if can_apply:
                        authority_reason = 'origin_match'
                elif created_by == 'p2p-sync':
                    can_apply = True
                    authority_reason = 'legacy_synced_channel'
                else:
                    authority_reason = 'local_origin_channel'

                incoming_allowed_ids = self._normalize_allowed_poster_ids(allowed_poster_user_ids)
                if not can_apply:
                    logger.warning(
                        "Ignoring remote posting snapshot for channel=%s via %s from=%s "
                        "(reason=%s, origin_peer=%s, current_policy=%s, current_allow_member_replies=%s, "
                        "incoming_policy=%s, incoming_allow_member_replies=%s, incoming_allowed_count=%d)",
                        clean_channel_id,
                        log_context,
                        clean_from_peer or 'unknown',
                        authority_reason,
                        origin_peer or 'local',
                        current_policy,
                        current_allow_member_replies,
                        self._normalize_post_policy(post_policy, default=self.POST_POLICY_OPEN),
                        True if allow_member_replies is None else bool(allow_member_replies),
                        len(incoming_allowed_ids),
                    )
                    return False

                self._sync_channel_post_permissions_conn(
                    conn,
                    clean_channel_id,
                    post_policy=post_policy,
                    allow_member_replies=allow_member_replies,
                    allowed_poster_user_ids=incoming_allowed_ids,
                )
                conn.commit()
                logger.info(
                    "Applied remote posting snapshot for channel=%s via %s from=%s "
                    "(policy=%s, allow_member_replies=%s, allowed_count=%d, authority=%s)",
                    clean_channel_id,
                    log_context,
                    clean_from_peer or 'unknown',
                    self._normalize_post_policy(post_policy, default=self.POST_POLICY_OPEN),
                    True if allow_member_replies is None else bool(allow_member_replies),
                    len(incoming_allowed_ids),
                    authority_reason,
                )
                return True
        except Exception as e:
            logger.error(
                "Failed to apply remote posting snapshot for %s via %s: %s",
                clean_channel_id,
                log_context,
                e,
                exc_info=True,
            )
            return False
        try:
            with self.db.get_connection() as conn:
                row = conn.execute(
                    """
                    SELECT COALESCE(post_policy, ?) AS post_policy,
                           COALESCE(allow_member_replies, 1) AS allow_member_replies
                    FROM channels
                    WHERE id = ?
                    """,
                    (self.POST_POLICY_OPEN, channel_id),
                ).fetchone()
                allowed_rows = conn.execute(
                    """
                    SELECT user_id
                    FROM channel_post_permissions
                    WHERE channel_id = ?
                    ORDER BY granted_at ASC, user_id ASC
                    """,
                    (channel_id,),
                ).fetchall()
            return {
                'post_policy': self._normalize_post_policy(
                    row['post_policy'] if row else self.POST_POLICY_OPEN,
                    default=self.POST_POLICY_OPEN,
                ),
                'allow_member_replies': bool(
                    row['allow_member_replies'] if row else True
                ),
                'allowed_poster_user_ids': [
                    str(allowed_row['user_id'])
                    for allowed_row in allowed_rows
                    if allowed_row and allowed_row['user_id']
                ],
            }
        except Exception as e:
            logger.error(
                "Failed to load channel posting snapshot for %s: %s",
                channel_id,
                e,
                exc_info=True,
            )
            return {
                'post_policy': self.POST_POLICY_OPEN,
                'allow_member_replies': True,
                'allowed_poster_user_ids': [],
            }

    def can_accept_incoming_message(
        self,
        channel_id: str,
        user_id: str,
        *,
        parent_message_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Return the inbound curated-policy decision for a synced channel message."""
        clean_channel_id = str(channel_id or '').strip()
        if clean_channel_id == 'general':
            return {
                'allowed': True,
                'reason': 'general_channel_exempt',
                'post_scope': 'reply' if parent_message_id else 'top_level',
                'post_policy': self.POST_POLICY_OPEN,
                'allow_member_replies': True,
            }
        return self.can_user_post_message(
            clean_channel_id,
            user_id,
            parent_message_id=parent_message_id,
        )

    def can_user_post_message(
        self,
        channel_id: str,
        user_id: str,
        *,
        parent_message_id: Optional[str] = None,
        allow_admin: bool = False,
    ) -> Dict[str, Any]:
        """Return a structured channel-posting decision for the user."""
        access = self.get_channel_access_decision(
            channel_id=channel_id,
            user_id=user_id,
            require_membership=True,
        )
        if not access.get('allowed'):
            return {
                'allowed': False,
                'reason': str(access.get('reason') or 'membership_required'),
                'post_scope': 'top_level' if not parent_message_id else 'reply',
            }

        state = self.get_channel_posting_state(
            channel_id,
            user_id,
            allow_admin=allow_admin,
        )
        if not state:
            return {
                'allowed': False,
                'reason': 'channel_not_found',
                'post_scope': 'top_level' if not parent_message_id else 'reply',
            }

        if parent_message_id:
            return {
                **state,
                'allowed': bool(state['can_reply']),
                'reason': 'ok' if state['can_reply'] else 'reply_restricted',
                'post_scope': 'reply',
            }

        return {
            **state,
            'allowed': bool(state['can_post_top_level']),
            'reason': 'ok' if state['can_post_top_level'] else 'top_level_post_restricted',
            'post_scope': 'top_level',
        }

    def update_channel_post_policy(
        self,
        channel_id: str,
        user_id: str,
        *,
        post_policy: str,
        allow_member_replies: Optional[bool] = None,
        allow_admin: bool = False,
        local_peer_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Update a channel's top-level posting policy."""
        try:
            normalized_policy = self._normalize_post_policy(post_policy, default=self.POST_POLICY_OPEN)
            next_allow_member_replies = True if allow_member_replies is None else bool(allow_member_replies)
            with self.db.get_connection() as conn:
                state = self._load_channel_posting_state_conn(
                    conn,
                    channel_id,
                    user_id,
                    allow_admin=allow_admin,
                )
                if not state:
                    return None
                previous_policy = str(state.get('post_policy') or self.POST_POLICY_OPEN)
                previous_allow_member_replies = bool(state.get('allow_member_replies', True))
                previous_allowed_poster_count = int(state.get('allowed_poster_count') or 0)
                origin_peer = state.get('origin_peer') or ''
                is_origin_local = not origin_peer or (local_peer_id and origin_peer == local_peer_id)
                if not is_origin_local or not state.get('is_admin_like'):
                    return None

                conn.execute(
                    """
                    UPDATE channels
                       SET post_policy = ?,
                           allow_member_replies = ?
                     WHERE id = ?
                    """,
                    (
                        normalized_policy,
                        1 if next_allow_member_replies else 0,
                        channel_id,
                    ),
                )
                conn.commit()

                updated_state = self._load_channel_posting_state_conn(
                    conn,
                    channel_id,
                    user_id,
                    allow_admin=allow_admin,
                ) or state

            logger.info(
                "Updated channel post policy channel=%s requester=%s origin_peer=%s "
                "(policy=%s->%s, allow_member_replies=%s->%s, allowed_poster_count=%s)",
                channel_id,
                user_id,
                origin_peer or 'local',
                previous_policy,
                updated_state.get('post_policy'),
                previous_allow_member_replies,
                updated_state.get('allow_member_replies'),
                previous_allowed_poster_count,
            )

            self._emit_channel_user_event(
                channel_id=channel_id,
                event_type=EVENT_CHANNEL_STATE_UPDATED,
                actor_user_id=user_id,
                payload={
                    'reason': 'post_policy_updated',
                    'post_policy': updated_state.get('post_policy'),
                    'allow_member_replies': updated_state.get('allow_member_replies'),
                    'allowed_poster_count': updated_state.get('allowed_poster_count'),
                },
                dedupe_suffix=f"post_policy:{updated_state.get('post_policy')}:{updated_state.get('allowed_poster_count')}",
            )
            return updated_state
        except Exception as e:
            logger.error(f"Failed to update channel post policy: {e}", exc_info=True)
            return None

    def grant_channel_post_permission(
        self,
        channel_id: str,
        target_user_id: str,
        requester_id: str,
        *,
        allow_admin: bool = False,
        local_peer_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Allow a member to start top-level posts in a curated channel."""
        try:
            with self.db.get_connection() as conn:
                state = self._load_channel_posting_state_conn(
                    conn,
                    channel_id,
                    requester_id,
                    allow_admin=allow_admin,
                )
                if not state:
                    return None
                previous_allowed_poster_count = int(state.get('allowed_poster_count') or 0)
                origin_peer = state.get('origin_peer') or ''
                is_origin_local = not origin_peer or (local_peer_id and origin_peer == local_peer_id)
                if not is_origin_local or not state.get('is_admin_like'):
                    return None

                target_row = conn.execute(
                    """
                    SELECT cm.user_id, cm.role
                    FROM channel_members cm
                    WHERE cm.channel_id = ? AND cm.user_id = ?
                    """,
                    (channel_id, target_user_id),
                ).fetchone()
                if not target_row:
                    return None

                target_role = str(target_row['role'] or 'member').strip().lower()
                if target_role != 'admin':
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO channel_post_permissions
                        (channel_id, user_id, granted_by)
                        VALUES (?, ?, ?)
                        """,
                        (channel_id, target_user_id, requester_id),
                    )
                conn.commit()

                updated_state = self._load_channel_posting_state_conn(
                    conn,
                    channel_id,
                    requester_id,
                    allow_admin=allow_admin,
                ) or state

            logger.info(
                "Granted channel poster access channel=%s requester=%s target=%s origin_peer=%s "
                "(policy=%s, allowed_poster_count=%s->%s)",
                channel_id,
                requester_id,
                target_user_id,
                origin_peer or 'local',
                updated_state.get('post_policy'),
                previous_allowed_poster_count,
                updated_state.get('allowed_poster_count'),
            )

            self._emit_channel_user_event(
                channel_id=channel_id,
                event_type=EVENT_CHANNEL_STATE_UPDATED,
                actor_user_id=requester_id,
                payload={
                    'reason': 'posting_access_updated',
                    'action': 'grant',
                    'target_user_id': target_user_id,
                    'post_policy': updated_state.get('post_policy'),
                    'allow_member_replies': updated_state.get('allow_member_replies'),
                    'allowed_poster_count': updated_state.get('allowed_poster_count'),
                },
                dedupe_suffix=f"post_grant:{target_user_id}:{updated_state.get('allowed_poster_count')}",
            )
            return updated_state
        except Exception as e:
            logger.error(f"Failed to grant channel post permission: {e}", exc_info=True)
            return None

    def revoke_channel_post_permission(
        self,
        channel_id: str,
        target_user_id: str,
        requester_id: str,
        *,
        allow_admin: bool = False,
        local_peer_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Remove explicit top-level posting permission from a member."""
        try:
            with self.db.get_connection() as conn:
                state = self._load_channel_posting_state_conn(
                    conn,
                    channel_id,
                    requester_id,
                    allow_admin=allow_admin,
                )
                if not state:
                    return None
                previous_allowed_poster_count = int(state.get('allowed_poster_count') or 0)
                origin_peer = state.get('origin_peer') or ''
                is_origin_local = not origin_peer or (local_peer_id and origin_peer == local_peer_id)
                if not is_origin_local or not state.get('is_admin_like'):
                    return None

                conn.execute(
                    """
                    DELETE FROM channel_post_permissions
                    WHERE channel_id = ? AND user_id = ?
                    """,
                    (channel_id, target_user_id),
                )
                conn.commit()

                updated_state = self._load_channel_posting_state_conn(
                    conn,
                    channel_id,
                    requester_id,
                    allow_admin=allow_admin,
                ) or state

            logger.info(
                "Revoked channel poster access channel=%s requester=%s target=%s origin_peer=%s "
                "(policy=%s, allowed_poster_count=%s->%s)",
                channel_id,
                requester_id,
                target_user_id,
                origin_peer or 'local',
                updated_state.get('post_policy'),
                previous_allowed_poster_count,
                updated_state.get('allowed_poster_count'),
            )

            self._emit_channel_user_event(
                channel_id=channel_id,
                event_type=EVENT_CHANNEL_STATE_UPDATED,
                actor_user_id=requester_id,
                payload={
                    'reason': 'posting_access_updated',
                    'action': 'revoke',
                    'target_user_id': target_user_id,
                    'post_policy': updated_state.get('post_policy'),
                    'allow_member_replies': updated_state.get('allow_member_replies'),
                    'allowed_poster_count': updated_state.get('allowed_poster_count'),
                },
                dedupe_suffix=f"post_revoke:{target_user_id}:{updated_state.get('allowed_poster_count')}",
            )
            return updated_state
        except Exception as e:
            logger.error(f"Failed to revoke channel post permission: {e}", exc_info=True)
            return None

    def sync_channel_post_permissions(
        self,
        channel_id: str,
        *,
        post_policy: Optional[str] = None,
        allow_member_replies: Optional[bool] = None,
        allowed_poster_user_ids: Optional[List[str]] = None,
    ) -> bool:
        """Apply synced posting metadata from a channel announce."""
        if not channel_id:
            return False
        try:
            with self.db.get_connection() as conn:
                self._sync_channel_post_permissions_conn(
                    conn,
                    channel_id,
                    post_policy=post_policy,
                    allow_member_replies=allow_member_replies,
                    allowed_poster_user_ids=allowed_poster_user_ids,
                )
                conn.commit()
            return True
        except Exception as e:
            logger.error(f"Failed to sync channel post permissions for {channel_id}: {e}", exc_info=True)
            return False

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
                    source_layout: Optional[Dict[str, Any]] = None,
                    source_reference: Optional[Dict[str, Any]] = None,
                    repost_policy: Optional[str] = None,
                    allow_source_reference: bool = False,
                    expires_at: Optional[Any] = None,
                    ttl_seconds: Optional[int] = None,
                    ttl_mode: Optional[str] = None,
                    origin_peer: Optional[str] = None) -> Optional[Message]:
        """Send a message to a channel."""
        logger.info(f"Sending message to channel {channel_id} by user {user_id}")
        logger.debug(f"Content length: {len(content)}, type: {message_type.value}")
        
        try:
            post_decision = self.can_user_post_message(
                channel_id=channel_id,
                user_id=user_id,
                parent_message_id=parent_message_id,
                allow_admin=False,
            )
            if not post_decision.get('allowed'):
                logger.warning(
                    f"Channel send denied for user={user_id}, channel={channel_id}, "
                    f"reason={post_decision.get('reason')}"
                )
                return None

            security_clean, sec_error = self.validate_security_metadata(security, strict=False)
            if sec_error:
                logger.warning(f"Dropping invalid security metadata for channel {channel_id}: {sec_error}")
            security = security_clean
            source_layout = normalize_source_layout(source_layout)
            source_reference = (
                _normalize_channel_source_reference(source_reference)
                if allow_source_reference else None
            )
            repost_policy = _normalize_channel_repost_policy(repost_policy) or 'same_scope'
            origin_peer = _normalize_origin_peer_id(origin_peer)

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
                source_layout=source_layout,
                source_reference=source_reference,
                repost_policy=repost_policy,
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
                        (id, channel_id, user_id, content, message_type, thread_id, parent_message_id, attachments, security, source_layout, source_reference, repost_policy, created_at, origin_peer, expires_at, ttl_seconds, ttl_mode)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        message_id, channel_id, user_id, content, message_type.value,
                        thread_id, parent_message_id,
                        json.dumps(normalized_attachments) if normalized_attachments else None,
                        json.dumps(security) if security else None,
                        json.dumps(source_layout) if source_layout else None,
                        json.dumps(source_reference) if source_reference else None,
                        repost_policy,
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
                       source_layout: Optional[Dict[str, Any]] = None,
                       source_reference: Optional[Dict[str, Any]] = None,
                       repost_policy: Optional[str] = None,
                       allow_source_reference: bool = False,
                       allow_admin: bool = False) -> bool:
        """Update a channel message (author or admin)."""
        try:
            with self.db.get_connection() as conn:
                row = conn.execute(
                    "SELECT channel_id, user_id, attachments, message_type, source_layout, source_reference, repost_policy FROM channel_messages WHERE id = ?",
                    (message_id,)
                ).fetchone()
                if not row or (row['user_id'] != user_id and not allow_admin):
                    logger.warning(f"User {user_id} cannot update channel message {message_id}")
                    return False
                channel_id = str(row['channel_id'] or '').strip()

                final_attachments = attachments
                final_source_layout = source_layout
                final_source_reference = None
                if final_attachments is None:
                    if row['attachments']:
                        try:
                            final_attachments = json.loads(row['attachments'])
                        except Exception:
                            final_attachments = None
                final_attachments = Message.normalize_attachments(final_attachments) if final_attachments else None
                if final_source_layout is None and row['source_layout']:
                    try:
                        final_source_layout = json.loads(row['source_layout'])
                    except Exception:
                        final_source_layout = None
                final_source_layout = normalize_source_layout(final_source_layout)
                if row['source_reference']:
                    try:
                        final_source_reference = _normalize_channel_source_reference(
                            json.loads(row['source_reference'])
                        )
                    except Exception:
                        final_source_reference = None
                if allow_source_reference and source_reference is not None:
                    final_source_reference = _normalize_channel_source_reference(source_reference)
                final_repost_policy = (
                    _normalize_channel_repost_policy(repost_policy)
                    if repost_policy is not None else
                    _normalize_channel_repost_policy(row['repost_policy'])
                ) or 'same_scope'

                if final_attachments:
                    final_message_type = MessageType.FILE.value
                else:
                    final_message_type = MessageType.TEXT.value

                edited_at = datetime.now(timezone.utc)
                edited_db = self._format_db_timestamp(edited_at)

                conn.execute(
                    "UPDATE channel_messages SET content = ?, message_type = ?, attachments = ?, source_layout = ?, source_reference = ?, repost_policy = ?, edited_at = ? WHERE id = ?",
                    (
                        content,
                        final_message_type,
                        json.dumps(final_attachments) if final_attachments else None,
                        json.dumps(final_source_layout) if final_source_layout else None,
                        json.dumps(final_source_reference) if final_source_reference else None,
                        final_repost_policy,
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

    def mark_channel_read(self, channel_id: str, user_id: str) -> bool:
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
                    return False
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
            return True
        except Exception as e:
            logger.warning(f"Failed to mark channel {channel_id} as read for {user_id}: {e}")
            return False

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
                conn.execute(
                    "DELETE FROM channel_post_permissions WHERE channel_id = ? AND user_id = ?",
                    (channel_id, target_user_id),
                )
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
                channel_row = conn.execute(
                    """
                    SELECT created_by,
                           COALESCE(post_policy, ?) AS post_policy
                    FROM channels
                    WHERE id = ?
                    """,
                    (self.POST_POLICY_OPEN, channel_id),
                ).fetchone()
                creator_id = str(channel_row['created_by'] or '').strip() if channel_row else ''
                post_policy = self._normalize_post_policy(
                    channel_row['post_policy'] if channel_row else self.POST_POLICY_OPEN,
                    default=self.POST_POLICY_OPEN,
                )
                rows = conn.execute("""
                    SELECT cm.user_id, cm.role, cm.joined_at,
                           u.username, u.display_name,
                           EXISTS(
                               SELECT 1
                               FROM channel_post_permissions cpp
                               WHERE cpp.channel_id = cm.channel_id
                                 AND cpp.user_id = cm.user_id
                           ) AS explicit_post_permission
                    FROM channel_members cm
                    LEFT JOIN users u ON cm.user_id = u.id
                    WHERE cm.channel_id = ?
                    ORDER BY cm.role DESC, cm.joined_at ASC
                """, (channel_id,)).fetchall()
                members = []
                for row in rows:
                    user_id = str(row['user_id'] or '').strip()
                    role = str(row['role'] or 'member').strip().lower()
                    explicit_allowed = bool(row['explicit_post_permission'])
                    can_start_threads = bool(
                        post_policy == self.POST_POLICY_OPEN
                        or role == 'admin'
                        or (creator_id and creator_id == user_id)
                        or explicit_allowed
                    )
                    posting_access = 'member'
                    if role == 'admin' or (creator_id and creator_id == user_id):
                        posting_access = 'admin'
                    elif explicit_allowed:
                        posting_access = 'allowed'
                    members.append({
                        'user_id': user_id,
                        'role': row['role'],
                        'joined_at': row['joined_at'],
                        'username': row['username'],
                        'display_name': row['display_name'] or row['username'],
                        'explicit_post_permission': explicit_allowed,
                        'can_start_threads': can_start_threads,
                        'posting_access': posting_access,
                    })
                return members
        except Exception as e:
            logger.error(f"Failed to get channel members: {e}")
            return []

    def get_private_channel_recovery_payload(
        self,
        query_user_ids: List[str],
        requester_peer_id: str,
        limit: int = 200,
        max_members_per_channel: int = 200,
        query_username_hints: Optional[Dict[str, str]] = None,
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
                if not valid_user_ids and query_username_hints:
                    hinted_usernames = []
                    seen_usernames = set()
                    for query_user_id in user_ids:
                        username = str((query_username_hints or {}).get(query_user_id) or '').strip()
                        if not username or username in seen_usernames:
                            continue
                        seen_usernames.add(username)
                        hinted_usernames.append(username)
                    if hinted_usernames:
                        username_placeholders = ','.join('?' for _ in hinted_usernames)
                        fallback_rows = conn.execute(
                            f"""
                            SELECT id
                            FROM users
                            WHERE origin_peer = ?
                              AND username IN ({username_placeholders})
                            """,
                            (requester, *hinted_usernames),
                        ).fetchall()
                        valid_user_ids = [
                            str(row['id'] if hasattr(row, 'keys') and 'id' in row.keys() else row[0])
                            for row in (fallback_rows or [])
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
                      AND NOT (
                        c.name = ?
                        OR c.id = ?
                        OR c.id = ?
                      )
                      AND NOT EXISTS (
                        SELECT 1
                        FROM channel_removal_tombstones crt
                        WHERE crt.channel_id = c.id
                          AND crt.restored_at IS NULL
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
                    tuple(valid_user_ids)
                    + (
                        self.AGENT_START_CHANNEL_NAME,
                        self.AGENT_START_CHANNEL_ID,
                        self.LEGACY_AGENT_START_CHANNEL_ID,
                        requester,
                        int(limit) + 1,
                    ),
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
                try:
                    conn.execute("DELETE FROM likes WHERE message_id = ?", (message_id,))
                except sqlite3.OperationalError as like_err:
                    logger.debug(f"Skipping like cleanup for channel message delete: {like_err}")
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

    def _row_to_channel_message(self, row: Any, row_type: str = "message") -> Optional[Message]:
        """Best-effort row parser used across channel message query paths."""
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
                source_layout=json.loads(row['source_layout']) if ('source_layout' in row.keys() and row['source_layout']) else None,
                source_reference=json.loads(row['source_reference']) if ('source_reference' in row.keys() and row['source_reference']) else None,
                repost_policy=_normalize_channel_repost_policy(row['repost_policy']) if ('repost_policy' in row.keys()) else None,
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

    def _hydrate_missing_parent_messages(
        self,
        conn: Any,
        channel_id: str,
        messages: List[Message],
    ) -> List[Message]:
        """Append missing parent/ancestor messages so replies render with context."""
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
                message = self._row_to_channel_message(row, "parent")
                if not message or message.id in msg_ids:
                    continue
                messages.append(message)
                msg_ids.add(message.id)
                if message.parent_message_id and message.parent_message_id not in msg_ids:
                    next_missing_parent_ids.add(message.parent_message_id)
            missing_parent_ids = next_missing_parent_ids
        return messages

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

                rows = conn.execute(query, params).fetchall()
                messages: List[Message] = []
                for row in rows:
                    message = self._row_to_channel_message(row, "message")
                    if message:
                        messages.append(message)

                messages.reverse()
                messages = self._hydrate_missing_parent_messages(conn, channel_id, messages)

                logger.debug(f"Retrieved {len(messages)} messages from channel {channel_id}")
                return messages

        except Exception as e:
            logger.error(f"Failed to get channel messages: {e}", exc_info=True)
            return []

    def get_channel_message_context(
        self,
        channel_id: str,
        message_id: str,
        user_id: str,
        radius: int = 12,
    ) -> List[Message]:
        """Return a focused message window around a specific message id."""
        if not channel_id or not message_id or not user_id:
            return []
        try:
            access = self.get_channel_access_decision(
                channel_id=channel_id,
                user_id=user_id,
                require_membership=True,
            )
            if not access.get('allowed'):
                return []

            radius = max(1, min(int(radius or 12), 50))
            with self.db.get_connection() as conn:
                target_row = conn.execute(
                    """
                    SELECT m.*, u.username as author_username
                    FROM channel_messages m
                    LEFT JOIN users u ON m.user_id = u.id
                    WHERE m.channel_id = ? AND m.id = ?
                      AND (m.expires_at IS NULL OR m.expires_at > CURRENT_TIMESTAMP)
                    LIMIT 1
                    """,
                    (channel_id, message_id),
                ).fetchone()
                if not target_row:
                    return []

                pivot_created_at = target_row['created_at']
                before_rows = conn.execute(
                    """
                    SELECT m.*, u.username as author_username
                    FROM channel_messages m
                    LEFT JOIN users u ON m.user_id = u.id
                    WHERE m.channel_id = ?
                      AND (m.expires_at IS NULL OR m.expires_at > CURRENT_TIMESTAMP)
                      AND m.created_at < ?
                    ORDER BY m.created_at DESC
                    LIMIT ?
                    """,
                    (channel_id, pivot_created_at, radius),
                ).fetchall()
                after_rows = conn.execute(
                    """
                    SELECT m.*, u.username as author_username
                    FROM channel_messages m
                    LEFT JOIN users u ON m.user_id = u.id
                    WHERE m.channel_id = ?
                      AND (m.expires_at IS NULL OR m.expires_at > CURRENT_TIMESTAMP)
                      AND m.created_at > ?
                    ORDER BY m.created_at ASC
                    LIMIT ?
                    """,
                    (channel_id, pivot_created_at, radius),
                ).fetchall()

                rows: List[Any] = list(reversed(before_rows)) + [target_row] + list(after_rows)
                messages: List[Message] = []
                seen_ids: set[str] = set()
                for row in rows:
                    message = self._row_to_channel_message(row, "context")
                    if not message or message.id in seen_ids:
                        continue
                    seen_ids.add(message.id)
                    messages.append(message)

                messages = self._hydrate_missing_parent_messages(conn, channel_id, messages)
                messages.sort(
                    key=lambda m: (
                        (m.created_at.isoformat() if hasattr(m.created_at, 'isoformat') else str(m.created_at)),
                        m.id,
                    )
                )
                return messages
        except Exception as e:
            logger.error(
                "Failed to get channel message context channel=%s message=%s: %s",
                channel_id,
                message_id,
                e,
                exc_info=True,
            )
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
                return self._row_to_channel_message(row, "single")
        except Exception as e:
            logger.error(f"Failed to get channel message {message_id}: {e}", exc_info=True)
            return None

    def get_repost_policy(self, message: Optional[Message]) -> str:
        """Resolve repost policy for a channel message; default to same-scope reposts."""
        if not message:
            return 'same_scope'
        policy = _normalize_channel_repost_policy(getattr(message, 'repost_policy', None))
        return policy or 'same_scope'

    def is_repost_message(self, message: Optional[Message]) -> bool:
        """Return True when a channel message is a repost wrapper."""
        if not message:
            return False
        return _is_channel_repost_reference(getattr(message, 'source_reference', None))

    def is_variant_message(self, message: Optional[Message]) -> bool:
        """Return True when a channel message is a lineage variant wrapper."""
        if not message:
            return False
        return _is_channel_variant_reference(getattr(message, 'source_reference', None))

    def _get_message_for_reference(
        self,
        channel_id: str,
        message_id: str,
        viewer_id: str,
    ) -> tuple[str, Optional[Message]]:
        """Resolve a channel message for repost rendering without widening access."""
        try:
            with self.db.get_connection() as conn:
                row = conn.execute(
                    """
                    SELECT *
                    FROM channel_messages
                    WHERE id = ?
                    """,
                    (message_id,),
                ).fetchone()
            if not row:
                return ('missing', None)

            message_channel_id = str(row['channel_id'] or '').strip()
            if not message_channel_id or message_channel_id != str(channel_id or '').strip():
                return ('access_changed', None)

            access = self.get_channel_access_decision(
                channel_id=message_channel_id,
                user_id=viewer_id,
                require_membership=True,
            )
            if not access.get('allowed'):
                return ('access_changed', None)

            message = self._row_to_channel_message(row, "repost_reference")
            if not message:
                return ('missing', None)
            if message.is_expired:
                return ('expired', None)
            if self.get_repost_policy(message) == 'deny':
                return ('policy_denied', None)
            return ('available', message)
        except Exception as e:
            logger.error(
                "Failed to resolve channel repost source %s in %s: %s",
                message_id,
                channel_id,
                e,
                exc_info=True,
            )
            return ('missing', None)

    def get_repost_eligibility(
        self,
        message_id: str,
        user_id: str,
        channel_id: str,
    ) -> Dict[str, Any]:
        """Evaluate whether a user can create a repost wrapper for a channel message."""
        source_channel_id = ''
        try:
            with self.db.get_connection() as conn:
                row = conn.execute(
                    "SELECT channel_id FROM channel_messages WHERE id = ?",
                    (message_id,),
                ).fetchone()
            if row:
                source_channel_id = str(row['channel_id'] or '').strip()
        except Exception as lookup_err:
            logger.debug(f"Channel repost source lookup failed for {message_id}: {lookup_err}")

        if not source_channel_id:
            return {
                'allowed': False,
                'status_code': 404,
                'reason': 'Message not found',
            }

        if source_channel_id != str(channel_id or '').strip():
            source_access = self.get_channel_access_decision(
                channel_id=source_channel_id,
                user_id=user_id,
                require_membership=True,
            )
            if source_access.get('allowed'):
                return {
                    'allowed': False,
                    'status_code': 403,
                    'reason': 'Channel reposts are limited to the same channel in v1',
                }
            return {
                'allowed': False,
                'status_code': 403,
                'reason': 'Access denied',
            }

        state, original = self._get_message_for_reference(source_channel_id, message_id, user_id)
        if state != 'available' or not original:
            if state == 'policy_denied':
                return {
                    'allowed': False,
                    'status_code': 403,
                    'reason': 'This message cannot be reposted',
                }
            return {
                'allowed': False,
                'status_code': 404 if state in {'missing', 'expired'} else 403,
                'reason': 'Message not found' if state in {'missing', 'expired'} else 'Access denied',
            }

        if self.is_repost_message(original):
            return {
                'allowed': False,
                'status_code': 400,
                'reason': 'Repost chains are not supported',
            }

        post_decision = self.can_user_post_message(
            channel_id=channel_id,
            user_id=user_id,
            parent_message_id=None,
            allow_admin=False,
        )
        if not post_decision.get('allowed'):
            return {
                'allowed': False,
                'status_code': 403,
                'reason': str(post_decision.get('reason') or 'posting_denied'),
                'post_policy': post_decision.get('post_policy'),
                'can_reply': post_decision.get('can_reply'),
            }

        return {
            'allowed': True,
            'status_code': 200,
            'reason': 'ok',
            'message': original,
        }

    def resolve_repost_reference(self, message: Message, viewer_id: str) -> Optional[Dict[str, Any]]:
        """Resolve a repost wrapper into a live original-source preview contract."""
        source_reference = _extract_channel_source_reference(message.source_reference)
        if not source_reference:
            return None
        if str(source_reference.get('kind') or '').strip().lower() != 'repost_v1':
            return None

        source_id = str(source_reference.get('source_id') or '').strip()
        source_channel_id = str(source_reference.get('channel_id') or message.channel_id or '').strip()
        result: Dict[str, Any] = {
            'kind': str(source_reference.get('kind') or '').strip() or 'repost_v1',
            'source_type': 'channel_message',
            'source_id': source_id,
            'channel_id': source_channel_id,
            'available': False,
            'unavailable_reason': 'missing',
        }
        if not source_id or not source_channel_id:
            return result

        state, original = self._get_message_for_reference(source_channel_id, source_id, viewer_id)
        if state != 'available' or not original:
            result['unavailable_reason'] = state
            return result

        source_layout = original.source_layout if isinstance(original.source_layout, dict) else None
        deck_default_ref = (
            str(source_layout.get('deck', {}).get('default_ref') or '').strip()
            if isinstance(source_layout, dict) and isinstance(source_layout.get('deck'), dict)
            else ''
        )
        body_text, body_truncated = _truncate_channel_repost_reference_body(original.content)
        embed = _channel_repost_embed_from_original(original)
        has_deck_ui = bool(source_layout) or _channel_original_signals_deck_ui(original, embed, self.db)
        try:
            result.update({
                'available': True,
                'unavailable_reason': None,
                'author_id': original.user_id,
                'created_at': _safe_channel_created_at_iso(original),
                'message_type': _safe_channel_message_type_label(original),
                'preview_text': _build_channel_repost_preview_text(original.content),
                'body_text': body_text,
                'body_truncated': body_truncated,
                'embed': embed,
                'has_source_layout': has_deck_ui,
                'deck_default_ref': deck_default_ref or None,
            })
        except Exception as preview_err:
            logger.warning(
                "Repost reference preview build failed for source %s: %s",
                source_id,
                preview_err,
            )
            result['available'] = False
            result['unavailable_reason'] = 'missing'
        return result

    def get_variant_eligibility(
        self,
        message_id: str,
        user_id: str,
        channel_id: str,
    ) -> Dict[str, Any]:
        """Evaluate whether a user can create a lineage variant for a channel message."""
        source_channel_id = ''
        try:
            with self.db.get_connection() as conn:
                row = conn.execute(
                    "SELECT channel_id FROM channel_messages WHERE id = ?",
                    (message_id,),
                ).fetchone()
            if row:
                source_channel_id = str(row['channel_id'] or '').strip()
        except Exception as lookup_err:
            logger.debug(f"Channel variant source lookup failed for {message_id}: {lookup_err}")

        if not source_channel_id:
            return {
                'allowed': False,
                'status_code': 404,
                'reason': 'Message not found',
            }

        if source_channel_id != str(channel_id or '').strip():
            source_access = self.get_channel_access_decision(
                channel_id=source_channel_id,
                user_id=user_id,
                require_membership=True,
            )
            if source_access.get('allowed'):
                return {
                    'allowed': False,
                    'status_code': 403,
                    'reason': 'Variants are limited to the same channel in v1',
                }
            return {
                'allowed': False,
                'status_code': 403,
                'reason': 'Access denied',
            }

        state, original = self._get_message_for_reference(source_channel_id, message_id, user_id)
        if state != 'available' or not original:
            if state == 'policy_denied':
                return {
                    'allowed': False,
                    'status_code': 403,
                    'reason': 'This message cannot be used as a variant source',
                }
            return {
                'allowed': False,
                'status_code': 404 if state in {'missing', 'expired'} else 403,
                'reason': 'Message not found' if state in {'missing', 'expired'} else 'Access denied',
            }

        if self.is_repost_message(original):
            return {
                'allowed': False,
                'status_code': 400,
                'reason': 'Repost wrappers cannot be used as variant sources',
            }

        if self.get_repost_policy(original) == 'deny':
            return {
                'allowed': False,
                'status_code': 403,
                'reason': 'This message does not allow variants',
            }

        post_decision = self.can_user_post_message(
            channel_id=channel_id,
            user_id=user_id,
            parent_message_id=None,
            allow_admin=False,
        )
        if not post_decision.get('allowed'):
            return {
                'allowed': False,
                'status_code': 403,
                'reason': str(post_decision.get('reason') or 'posting_denied'),
                'post_policy': post_decision.get('post_policy'),
                'can_reply': post_decision.get('can_reply'),
            }

        return {
            'allowed': True,
            'status_code': 200,
            'reason': 'ok',
            'message': original,
        }

    def resolve_variant_reference(self, message: Message, viewer_id: str) -> Optional[Dict[str, Any]]:
        """Resolve a lineage variant into a live antecedent-source preview contract."""
        source_reference = _extract_channel_source_reference(message.source_reference)
        if not source_reference:
            return None
        if str(source_reference.get('kind') or '').strip().lower() != 'variant_v1':
            return None

        source_id = str(source_reference.get('source_id') or '').strip()
        source_channel_id = str(source_reference.get('channel_id') or message.channel_id or '').strip()
        relationship_kind = _normalize_channel_variant_relationship(source_reference.get('relationship_kind'))
        result: Dict[str, Any] = {
            'kind': 'variant_v1',
            'source_type': 'channel_message',
            'source_id': source_id,
            'channel_id': source_channel_id,
            'available': False,
            'unavailable_reason': 'missing',
            'relationship_kind': relationship_kind,
            'module_param_delta': str(source_reference.get('module_param_delta') or '').strip() or None,
        }
        if not source_id or not source_channel_id:
            return result

        state, original = self._get_message_for_reference(source_channel_id, source_id, viewer_id)
        if state != 'available' or not original:
            result['unavailable_reason'] = state
            return result

        source_layout = original.source_layout if isinstance(original.source_layout, dict) else None
        deck_default_ref = (
            str(source_layout.get('deck', {}).get('default_ref') or '').strip()
            if isinstance(source_layout, dict) and isinstance(source_layout.get('deck'), dict)
            else ''
        )
        body_text, body_truncated = _truncate_channel_repost_reference_body(original.content)
        embed = _channel_repost_embed_from_original(original)
        has_deck_ui = bool(source_layout) or _channel_original_signals_deck_ui(original, embed, self.db)
        try:
            result.update({
                'available': True,
                'unavailable_reason': None,
                'author_id': original.user_id,
                'created_at': _safe_channel_created_at_iso(original),
                'message_type': _safe_channel_message_type_label(original),
                'preview_text': _build_channel_repost_preview_text(original.content),
                'body_text': body_text,
                'body_truncated': body_truncated,
                'embed': embed,
                'has_source_layout': has_deck_ui,
                'deck_default_ref': deck_default_ref or None,
            })
        except Exception as preview_err:
            logger.warning(
                "Variant reference preview build failed for source %s: %s",
                source_id,
                preview_err,
            )
            result['available'] = False
            result['unavailable_reason'] = 'missing'
        return result

    def create_repost(
        self,
        source_message_id: str,
        user_id: str,
        channel_id: str,
        comment: str = '',
        origin_peer: Optional[str] = None,
    ) -> Optional[Message]:
        """Create a secure same-channel repost wrapper for an eligible channel message."""
        try:
            eligibility = self.get_repost_eligibility(source_message_id, user_id, channel_id)
            if not eligibility.get('allowed'):
                logger.warning(
                    "Cannot repost channel message %s for user %s in %s: %s",
                    source_message_id,
                    user_id,
                    channel_id,
                    eligibility.get('reason'),
                )
                return None

            original = cast(Message, eligibility['message'])
            # Persist the antecedent's channel (same as posting channel in v1; avoids stale param drift).
            antecedent_channel_id = str(getattr(original, 'channel_id', None) or channel_id or '').strip()
            return self.send_message(
                channel_id=channel_id,
                user_id=user_id,
                content=str(comment or '').strip(),
                message_type=MessageType.TEXT,
                source_reference={
                    'kind': 'repost_v1',
                    'source_type': 'channel_message',
                    'source_id': original.id,
                    'channel_id': antecedent_channel_id,
                    'created_by_user_id': user_id,
                },
                allow_source_reference=True,
                origin_peer=origin_peer,
            )
        except Exception as e:
            logger.error(f"Failed to create channel repost: {e}", exc_info=True)
            return None

    def create_variant(
        self,
        source_message_id: str,
        user_id: str,
        channel_id: str,
        comment: str = '',
        *,
        relationship_kind: str = 'curated_recomposition',
        module_param_delta: str = '',
        origin_peer: Optional[str] = None,
    ) -> Optional[Message]:
        """Create a secure same-channel lineage variant wrapper."""
        try:
            eligibility = self.get_variant_eligibility(source_message_id, user_id, channel_id)
            if not eligibility.get('allowed'):
                logger.warning(
                    "Cannot create variant from channel message %s for user %s in %s: %s",
                    source_message_id,
                    user_id,
                    channel_id,
                    eligibility.get('reason'),
                )
                return None

            original = cast(Message, eligibility['message'])
            return self.send_message(
                channel_id=channel_id,
                user_id=user_id,
                content=str(comment or '').strip(),
                message_type=MessageType.TEXT,
                source_reference={
                    'kind': 'variant_v1',
                    'source_type': 'channel_message',
                    'source_id': original.id,
                    'channel_id': channel_id,
                    'created_by_user_id': user_id,
                    'relationship_kind': relationship_kind,
                    'module_param_delta': module_param_delta,
                },
                allow_source_reference=True,
                origin_peer=origin_peer,
            )
        except Exception as e:
            logger.error(f"Failed to create channel variant: {e}", exc_info=True)
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
                           EXISTS(
                               SELECT 1
                               FROM channel_removal_tombstones crt
                               WHERE crt.channel_id = c.id
                                 AND crt.restored_at IS NULL
                           ) AS retired_by_vote,
                           (
                               SELECT proposal_id
                               FROM channel_removal_proposals crp
                               WHERE crp.channel_id = c.id
                                 AND crp.status = 'open'
                               ORDER BY crp.opened_at DESC
                               LIMIT 1
                           ) AS active_removal_proposal_id,
                           COUNT(DISTINCT cm2.user_id) as member_count,
                           MAX(msg.created_at) as last_message_at,
                           COALESCE(c.post_policy, ?) AS post_policy,
                           COALESCE(c.allow_member_replies, 1) AS allow_member_replies,
                           EXISTS(
                               SELECT 1
                               FROM channel_post_permissions cpp
                               WHERE cpp.channel_id = c.id
                                 AND cpp.user_id = cm.user_id
                           ) AS explicit_post_permission,
                           (
                               SELECT COUNT(*)
                               FROM channel_post_permissions cpp
                               WHERE cpp.channel_id = c.id
                           ) AS allowed_poster_count,
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
                """, (self.POST_POLICY_OPEN, self.DEFAULT_CHANNEL_LIFECYCLE_DAYS, user_id))
                
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
                    if bool(row['retired_by_vote']):
                        logger.debug(
                            "Skipping channel %s for user %s because it is retired by vote",
                            row['id'],
                            user_id,
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
                    try:
                        post_policy = self._normalize_post_policy(row['post_policy'], default=self.POST_POLICY_OPEN)
                    except (IndexError, KeyError):
                        post_policy = self.POST_POLICY_OPEN
                    try:
                        allow_member_replies = bool(row['allow_member_replies'])
                    except (IndexError, KeyError, TypeError):
                        allow_member_replies = True
                    try:
                        explicit_post_permission = bool(row['explicit_post_permission'])
                    except (IndexError, KeyError, TypeError):
                        explicit_post_permission = False
                    try:
                        allowed_poster_count = int(row['allowed_poster_count'] or 0)
                    except (IndexError, KeyError, TypeError):
                        allowed_poster_count = 0
                    can_post_top_level = bool(
                        post_policy == self.POST_POLICY_OPEN
                        or user_role == 'admin'
                        or str(row['created_by'] or '').strip() == user_id
                        or explicit_post_permission
                    )
                    can_reply = bool(allow_member_replies or can_post_top_level)

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
                        post_policy=post_policy,
                        allow_member_replies=allow_member_replies,
                        can_post_top_level=can_post_top_level,
                        can_reply=can_reply,
                        allowed_poster_count=allowed_poster_count,
                        retired_by_vote=bool(row['retired_by_vote']),
                        removal_status='open' if row['active_removal_proposal_id'] else None,
                        active_removal_proposal_id=row['active_removal_proposal_id'],
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

    def get_channel_history_bounds(self) -> Dict[str, Dict[str, Any]]:
        """Return oldest/latest timestamp and live message count per channel."""
        try:
            with self.db.get_connection() as conn:
                rows = conn.execute("""
                    SELECT channel_id,
                           MIN(created_at) AS oldest,
                           MAX(created_at) AS latest,
                           COUNT(id) AS message_count
                    FROM channel_messages
                    WHERE expires_at IS NULL OR expires_at > CURRENT_TIMESTAMP
                    GROUP BY channel_id
                """).fetchall()
                result: Dict[str, Dict[str, Any]] = {}
                for row in rows:
                    channel_id = str(row['channel_id'] or '').strip()
                    if not channel_id:
                        continue
                    result[channel_id] = {
                        'oldest': row['oldest'],
                        'latest': row['latest'],
                        'message_count': int(row['message_count'] or 0),
                    }
                return result
        except Exception as e:
            logger.error(f"Failed to get channel history bounds: {e}", exc_info=True)
            return {}

    def get_channel_visibility_map(self, channel_ids: List[str]) -> Dict[str, bool]:
        """Return whether each channel ID is public/open enough for public mesh bootstrap."""
        result: Dict[str, bool] = {}
        ids: List[str] = []
        seen = set()
        for raw in channel_ids or []:
            cid = str(raw or '').strip()
            if not cid or cid in seen:
                continue
            seen.add(cid)
            ids.append(cid)
        if not ids:
            return result
        try:
            with self.db.get_connection() as conn:
                placeholders = ",".join("?" for _ in ids)
                rows = conn.execute(
                    f"""
                    SELECT id, channel_type, COALESCE(privacy_mode, 'open') AS privacy_mode
                    FROM channels
                    WHERE id IN ({placeholders})
                    """,
                    tuple(ids),
                ).fetchall()
            for row in rows or []:
                channel_id = str(row['id'] or '').strip()
                if not channel_id:
                    continue
                result[channel_id] = self._is_public_channel(
                    row['channel_type'],
                    row['privacy_mode'],
                )
        except Exception as e:
            logger.debug(f"Failed to build channel visibility map: {e}")
        return result

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
                    SELECT m.id, m.channel_id, m.user_id, m.content,
                           m.message_type, m.created_at, m.attachments, m.expires_at,
                           m.origin_peer,
                           m.ttl_seconds, m.ttl_mode, m.parent_message_id, m.source_layout,
                           m.source_reference, m.repost_policy,
                           m.encrypted_content, m.crypto_state, m.key_id, m.nonce,
                           c.name AS channel_name,
                           c.channel_type AS channel_type,
                           COALESCE(c.privacy_mode, 'open') AS channel_privacy_mode,
                           c.origin_peer AS channel_origin_peer
                    FROM channel_messages m
                    LEFT JOIN channels c ON c.id = m.channel_id
                    WHERE m.channel_id = ?
                      AND m.created_at > ?
                      AND (m.expires_at IS NULL OR m.expires_at > CURRENT_TIMESTAMP)
                    ORDER BY m.created_at ASC
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
                        'channel_name': row['channel_name'] if 'channel_name' in row.keys() else None,
                        'channel_type': row['channel_type'] if 'channel_type' in row.keys() else None,
                        'channel_privacy_mode': row['channel_privacy_mode'] if 'channel_privacy_mode' in row.keys() else None,
                        'channel_origin_peer': row['channel_origin_peer'] if 'channel_origin_peer' in row.keys() else None,
                        'ttl_seconds': row['ttl_seconds'] if 'ttl_seconds' in row.keys() else None,
                        'ttl_mode': row['ttl_mode'] if 'ttl_mode' in row.keys() else None,
                        'parent_message_id': row['parent_message_id'] if 'parent_message_id' in row.keys() else None,
                        'source_layout': json.loads(row['source_layout']) if ('source_layout' in row.keys() and row['source_layout']) else None,
                        'source_reference': json.loads(row['source_reference']) if ('source_reference' in row.keys() and row['source_reference']) else None,
                        'repost_policy': _normalize_channel_repost_policy(row['repost_policy']) if 'repost_policy' in row.keys() else None,
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

    def get_messages_before(self, channel_id: str, before_timestamp: str,
                            limit: int = 50) -> List[Dict[str, Any]]:
        """Get older messages in a channel created before *before_timestamp*.

        Results are returned in ascending timestamp order so callers can append
        them directly to replay/catchup payloads without additional sorting.
        """
        try:
            with self.db.get_connection() as conn:
                rows = conn.execute("""
                    SELECT m.id, m.channel_id, m.user_id, m.content,
                           m.message_type, m.created_at, m.attachments, m.expires_at,
                           m.origin_peer,
                           m.ttl_seconds, m.ttl_mode, m.parent_message_id, m.source_layout,
                           m.source_reference, m.repost_policy,
                           m.encrypted_content, m.crypto_state, m.key_id, m.nonce,
                           c.name AS channel_name,
                           c.channel_type AS channel_type,
                           COALESCE(c.privacy_mode, 'open') AS channel_privacy_mode,
                           c.origin_peer AS channel_origin_peer
                    FROM channel_messages m
                    LEFT JOIN channels c ON c.id = m.channel_id
                    WHERE m.channel_id = ?
                      AND m.created_at < ?
                      AND (m.expires_at IS NULL OR m.expires_at > CURRENT_TIMESTAMP)
                    ORDER BY m.created_at DESC
                    LIMIT ?
                """, (channel_id, before_timestamp, limit)).fetchall()

                messages: List[Dict[str, Any]] = []
                for row in reversed(rows):
                    msg = {
                        'id': row['id'],
                        'channel_id': row['channel_id'],
                        'user_id': row['user_id'],
                        'content': row['content'],
                        'message_type': row['message_type'],
                        'created_at': row['created_at'],
                        'expires_at': row['expires_at'],
                        'origin_peer': row['origin_peer'] if 'origin_peer' in row.keys() else None,
                        'channel_name': row['channel_name'] if 'channel_name' in row.keys() else None,
                        'channel_type': row['channel_type'] if 'channel_type' in row.keys() else None,
                        'channel_privacy_mode': row['channel_privacy_mode'] if 'channel_privacy_mode' in row.keys() else None,
                        'channel_origin_peer': row['channel_origin_peer'] if 'channel_origin_peer' in row.keys() else None,
                        'ttl_seconds': row['ttl_seconds'] if 'ttl_seconds' in row.keys() else None,
                        'ttl_mode': row['ttl_mode'] if 'ttl_mode' in row.keys() else None,
                        'parent_message_id': row['parent_message_id'] if 'parent_message_id' in row.keys() else None,
                        'source_layout': json.loads(row['source_layout']) if ('source_layout' in row.keys() and row['source_layout']) else None,
                        'source_reference': json.loads(row['source_reference']) if ('source_reference' in row.keys() and row['source_reference']) else None,
                        'repost_policy': _normalize_channel_repost_policy(row['repost_policy']) if 'repost_policy' in row.keys() else None,
                        'encrypted_content': (
                            row['encrypted_content'] if 'encrypted_content' in row.keys() else None
                        ),
                        'crypto_state': (
                            row['crypto_state'] if 'crypto_state' in row.keys() else None
                        ),
                        'key_id': row['key_id'] if 'key_id' in row.keys() else None,
                        'nonce': row['nonce'] if 'nonce' in row.keys() else None,
                    }
                    if row['attachments']:
                        try:
                            atts = json.loads(row['attachments'])
                            for att in atts:
                                att.pop('data', None)
                            msg['attachments'] = atts
                        except Exception:
                            pass
                    messages.append(msg)

                logger.debug(f"Catchup backfill: {len(messages)} messages in "
                             f"#{channel_id} before {before_timestamp}")
                return messages
        except Exception as e:
            logger.error(f"Failed to get messages before {before_timestamp} "
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
