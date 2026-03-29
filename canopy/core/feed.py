"""
Social feed system for Canopy.

Implements Facebook-like timeline posts with permissions, media support,
and customizable feed algorithms.

Project: Canopy - Local Mesh Communication
License: Apache 2.0
"""

from __future__ import annotations

import logging
import math
import secrets
import json
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Any, Union, cast
from dataclasses import dataclass, field, asdict
from enum import Enum

from .database import DatabaseManager
from .events import (
    EVENT_FEED_POST_CREATED,
    EVENT_FEED_POST_DELETED,
    EVENT_FEED_POST_UPDATED,
)
from ..security.api_keys import ApiKeyManager, Permission
from ..security.encryption import RecipientEncryptor, RECIPIENT_ENCRYPTED_PREFIX
from .logging_config import log_performance, LogOperation
from .source_layout import normalize_source_layout
from .polls import parse_poll

logger = logging.getLogger('canopy.feed')


class PostType(Enum):
    """Types of posts supported in the feed."""
    TEXT = "text"
    LINK = "link"
    IMAGE = "image"
    VIDEO = "video"
    AUDIO = "audio"
    POLL = "poll"


class PostVisibility(Enum):
    """Visibility levels for posts."""
    PUBLIC = "public"      # Everyone can see
    NETWORK = "network"    # Only network peers can see
    TRUSTED = "trusted"    # Only trusted peers can see
    PRIVATE = "private"    # Only specific users can see
    CUSTOM = "custom"      # Custom permission list


class SourceType(Enum):
    """Classification of post origin."""
    HUMAN = "human"                # Written by a human user
    AGENT = "agent"                # Written by an AI agent on this node
    AGENT_CURATED = "agent_curated"  # Agent-fetched content from the internet
    SYSTEM = "system"              # System-generated (milestones, alerts)


_REPOST_POLICY_VALUES = {'same_scope', 'deny'}
_SOURCE_REFERENCE_KIND_VALUES = {'repost_v1', 'variant_v1'}
_VARIANT_RELATIONSHIP_VALUES = {
    'curated_recomposition',
    'module_variant',
    'parameterized_variant',
}
_REPOST_ELIGIBLE_VISIBILITIES = {
    PostVisibility.PUBLIC,
    PostVisibility.NETWORK,
    PostVisibility.TRUSTED,
}
_LEGACY_REPOST_METADATA_KEYS = {
    'shared_post_id',
    'original_author_id',
    'original_content',
    'original_type',
    'original_metadata',
}


def _normalize_repost_policy(value: Any) -> Optional[str]:
    policy = str(value or '').strip().lower()
    if policy in _REPOST_POLICY_VALUES:
        return policy
    return None


def _normalize_variant_relationship(value: Any) -> str:
    relationship = str(value or '').strip().lower()
    if relationship in _VARIANT_RELATIONSHIP_VALUES:
        return relationship
    return 'curated_recomposition'


def _normalize_source_reference(value: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(value, dict):
        return None
    kind = str(value.get('kind') or '').strip().lower()
    source_type = str(value.get('source_type') or '').strip().lower()
    source_id = str(value.get('source_id') or '').strip()
    if kind not in _SOURCE_REFERENCE_KIND_VALUES or source_type != 'feed_post' or not source_id:
        return None

    normalized: Dict[str, Any] = {
        'kind': kind,
        'source_type': 'feed_post',
        'source_id': source_id,
    }
    source_visibility = str(value.get('source_visibility') or '').strip().lower()
    if source_visibility in {member.value for member in PostVisibility}:
        normalized['source_visibility'] = source_visibility
    created_by = str(value.get('created_by_user_id') or '').strip()
    if created_by:
        normalized['created_by_user_id'] = created_by
    if kind == 'variant_v1':
        normalized['relationship_kind'] = _normalize_variant_relationship(value.get('relationship_kind'))
        module_param_delta = str(value.get('module_param_delta') or '').strip()
        if module_param_delta:
            normalized['module_param_delta'] = module_param_delta[:500]
    return normalized


def _extract_source_reference(metadata: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(metadata, dict):
        return None

    source_reference = _normalize_source_reference(metadata.get('source_reference'))
    if source_reference:
        return source_reference

    legacy_source_id = str(metadata.get('shared_post_id') or '').strip()
    if legacy_source_id:
        return {
            'kind': 'legacy_share',
            'source_type': 'feed_post',
            'source_id': legacy_source_id,
            'legacy': True,
        }
    return None


def _is_repost_metadata(metadata: Any) -> bool:
    source_reference = _extract_source_reference(metadata)
    if not source_reference:
        return False
    return str(source_reference.get('kind') or '').strip().lower() in {'repost_v1', 'legacy_share'}


def _is_variant_metadata(metadata: Any) -> bool:
    source_reference = _extract_source_reference(metadata)
    if not source_reference:
        return False
    return str(source_reference.get('kind') or '').strip().lower() == 'variant_v1'


def _normalize_post_metadata(
    metadata: Any,
    *,
    allow_source_reference: bool = False,
    preserve_legacy_repost_keys: bool = False,
) -> Optional[Dict[str, Any]]:
    if not isinstance(metadata, dict):
        return None

    normalized = dict(metadata)
    normalized["source_layout"] = normalize_source_layout(normalized.get("source_layout"))
    if normalized.get("source_layout") is None:
        normalized.pop("source_layout", None)

    repost_policy = _normalize_repost_policy(normalized.get('repost_policy'))
    if repost_policy:
        normalized['repost_policy'] = repost_policy
    else:
        normalized.pop('repost_policy', None)

    if allow_source_reference:
        source_reference = _normalize_source_reference(normalized.get('source_reference'))
        if source_reference:
            normalized['source_reference'] = source_reference
        else:
            normalized.pop('source_reference', None)
    else:
        normalized.pop('source_reference', None)

    if not preserve_legacy_repost_keys:
        for key in _LEGACY_REPOST_METADATA_KEYS:
            normalized.pop(key, None)

    return normalized or None


def _build_preview_text(content: str, limit: int = 220) -> str:
    preview = ' '.join(str(content or '').split()).strip()
    if len(preview) <= limit:
        return preview
    return preview[: max(0, limit - 3)].rstrip() + '...'


# Rich repost card: show substantial original text without copying into the repost row (live resolve).
_REPOST_REFERENCE_BODY_MAX_CHARS = 8000
_REPOST_REFERENCE_ATTACHMENT_IMAGE_CAP = 6


def _truncate_repost_reference_body(text: str, max_chars: int = _REPOST_REFERENCE_BODY_MAX_CHARS) -> tuple[str, bool]:
    """Return (body, truncated) preserving newlines for UI pre-wrap."""
    raw = str(text or '')
    if len(raw) <= max_chars:
        return raw, False
    cut = raw[: max(0, max_chars - 1)].rstrip()
    return cut + '…', True


def _repost_embed_from_original(original: Post) -> Dict[str, Any]:
    """Structured preview for repost cards (same visibility as viewing the source post)."""
    meta = original.metadata if isinstance(original.metadata, dict) else {}
    embed: Dict[str, Any] = {}
    pt = original.post_type

    if pt == PostType.LINK:
        url = str(meta.get('url') or '').strip()
        if url:
            embed['link_url'] = url
            embed['link_title'] = str(meta.get('title') or url).strip() or url
    elif pt == PostType.IMAGE:
        image_url = str(meta.get('image_url') or '').strip()
        if image_url:
            embed['image_url'] = image_url
    elif pt == PostType.VIDEO:
        video_url = str(meta.get('video_url') or '').strip()
        if video_url:
            embed['video_url'] = video_url
    elif pt == PostType.AUDIO:
        audio_url = str(meta.get('audio_url') or '').strip()
        if audio_url:
            embed['audio_url'] = audio_url
            embed['audio_type'] = str(meta.get('audio_type') or 'audio/mpeg').strip() or 'audio/mpeg'
    attachments = meta.get('attachments')
    if isinstance(attachments, list):
        images: List[Dict[str, str]] = []
        for item in attachments:
            if not isinstance(item, dict):
                continue
            url = str(item.get('url') or '').strip()
            typ = str(item.get('type') or '')
            if typ.startswith('image/') and url:
                images.append({
                    'url': url,
                    'name': str(item.get('name') or 'Image'),
                    'type': typ,
                })
            if len(images) >= _REPOST_REFERENCE_ATTACHMENT_IMAGE_CAP:
                break
        if images:
            embed['attachment_images'] = images

    try:
        poll_spec = parse_poll(original.content or '')
        if poll_spec and str(poll_spec.question or '').strip():
            embed['poll_question'] = str(poll_spec.question).strip()
            if poll_spec.options:
                embed['poll_option_previews'] = [str(label) for label in poll_spec.options[:6]]
    except Exception:
        pass

    return embed


_REPOST_EMBED_KEYS_IMPLYING_DECK_QUEUE = frozenset({
    'link_url',
    'image_url',
    'video_url',
    'audio_url',
    'attachment_images',
})


def _metadata_has_canopy_module_attachment(metadata: Any, db_manager: Any = None) -> bool:
    """True when attachments include HTML the feed deck can run (modules or demos).

    Feed UI treats ``.html`` / ``.htm`` attachments as module-capable; MIME is often
    wrong (e.g. ``application/octet-stream``). Align repost ``has_source_layout`` with
    that so Deck appears on repost cards.

    When dicts only have ``id``/``file_id``, optional ``db_manager`` reads ``files``
    for ``original_name`` / ``content_type``.
    """
    if not isinstance(metadata, dict):
        return False
    attachments = metadata.get('attachments')
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


def _metadata_has_any_file_attachment(metadata: Any) -> bool:
    """True when the post metadata lists at least one stored file attachment by id.

    Covers replication/sync where filenames or MIME are missing on the message row but
    the source post still has a deck-capable surface when opened.
    """
    if not isinstance(metadata, dict):
        return False
    attachments = metadata.get('attachments')
    if not isinstance(attachments, list):
        return False
    for item in attachments:
        if not isinstance(item, dict):
            continue
        if str(item.get('id') or item.get('file_id') or '').strip():
            return True
    return False


def _repost_embed_implies_deck_queue(embed: Dict[str, Any]) -> bool:
    """True when structured repost embed carries media the deck can queue."""
    if not embed:
        return False
    return any(key in embed for key in _REPOST_EMBED_KEYS_IMPLYING_DECK_QUEUE)


@dataclass
class FeedAlgorithm:
    """User-controlled feed ranking algorithm.
    
    Stored per-user, runs locally. Every field is a tunable preference
    that the user can adjust through the Algorithm Settings UI.
    """

    # Source weights (0.0 = hide, 1.0 = normal, 2.0 = boost)
    human_weight: float = 1.0
    agent_weight: float = 0.8
    curated_weight: float = 0.6
    system_weight: float = 0.3

    # Engagement weights
    like_weight: float = 1.0
    comment_weight: float = 2.0
    share_weight: float = 3.0

    # Recency curve (higher = more recent posts favored)
    recency_halflife_hours: float = 24.0

    # Topic filters
    boosted_topics: List[str] = field(default_factory=list)
    muted_topics: List[str] = field(default_factory=list)
    topic_boost_factor: float = 2.0

    # Author filters
    boosted_authors: List[str] = field(default_factory=list)
    muted_authors: List[str] = field(default_factory=list)
    own_post_boost: float = 1.2

    # Agent trust overrides (agent_id -> weight multiplier)
    agent_trust: Dict[str, float] = field(default_factory=dict)

    # Content filters
    max_age_days: int = 30
    show_reposts: bool = True

    def score_post(self, post: 'Post', user_id: str) -> float:
        """Score a post for this user's feed. Returns -1 to filter out."""
        if post.is_expired:
            return -1.0
        # Source weight
        source_w = {
            'human': self.human_weight,
            'agent': self.agent_weight,
            'agent_curated': self.curated_weight,
            'system': self.system_weight,
        }.get(post.source_type, 1.0)

        if source_w <= 0:
            return -1.0  # filtered out

        # Engagement score
        engagement = (1.0
            + post.likes * self.like_weight
            + post.comments * self.comment_weight
            + post.shares * self.share_weight)

        # Recency decay (exponential half-life)
        now = datetime.now(timezone.utc)
        try:
            created = post.created_at if post.created_at.tzinfo else post.created_at.replace(tzinfo=timezone.utc)
            age_hours = max(0, (now - created).total_seconds() / 3600)
        except Exception:
            age_hours = 0
        halflife = max(1.0, self.recency_halflife_hours)
        recency = math.pow(2, -age_hours / halflife)

        # Topic boost/mute
        topic_factor = 1.0
        post_tags = post.tags_list
        if self.muted_topics and any(t in self.muted_topics for t in post_tags):
            return -1.0  # muted
        if self.boosted_topics and any(t in self.boosted_topics for t in post_tags):
            topic_factor = self.topic_boost_factor

        # Author boost/mute
        author_factor = 1.0
        if post.author_id in self.muted_authors:
            return -1.0  # muted
        if post.author_id == user_id:
            author_factor = self.own_post_boost
        elif post.author_id in self.boosted_authors:
            author_factor = 1.5

        # Agent trust override
        if post.source_agent_id and post.source_agent_id in self.agent_trust:
            source_w *= max(0, self.agent_trust[post.source_agent_id])

        # Repost filter
        if not self.show_reposts and _is_repost_metadata(post.metadata):
            return -1.0

        return source_w * engagement * recency * topic_factor * author_factor

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to JSON-safe dict."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'FeedAlgorithm':
        """Deserialize from dict, ignoring unknown keys."""
        known_fields = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in data.items() if k in known_fields}
        return cls(**filtered)


@dataclass
class Post:
    """Represents a social media post."""
    id: str
    author_id: str
    content: str
    post_type: PostType
    visibility: PostVisibility
    created_at: datetime
    expires_at: Optional[datetime] = None
    metadata: Optional[Dict[str, Any]] = None
    permissions: Optional[List[str]] = None  # User IDs for custom visibility
    likes: int = 0
    comments: int = 0
    shares: int = 0
    source_type: str = 'human'
    source_agent_id: Optional[str] = None
    source_url: Optional[str] = None
    tags: Optional[str] = None  # JSON array string
    author_origin_peer: Optional[str] = None
    author_peer_trusted: Optional[bool] = None

    @property
    def is_expired(self) -> bool:
        """Return True if the post has expired."""
        if not self.expires_at:
            return False
        now = datetime.now(timezone.utc)
        exp = self.expires_at
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        return exp <= now

    @property
    def tags_list(self) -> List[str]:
        """Parse tags JSON string into a list."""
        if not self.tags:
            return []
        try:
            parsed = json.loads(self.tags)
            return parsed if isinstance(parsed, list) else []
        except (json.JSONDecodeError, TypeError):
            return []

    def to_dict(self) -> Dict[str, Any]:
        """Convert post to dictionary for JSON serialization."""
        return {
            'id': self.id,
            'author_id': self.author_id,
            'content': self.content,
            'post_type': self.post_type.value,
            'visibility': self.visibility.value,
            'created_at': self.created_at.isoformat(),
            'expires_at': self.expires_at.isoformat() if self.expires_at else None,
            'metadata': self.metadata,
            'permissions': self.permissions,
            'likes': self.likes,
            'comments': self.comments,
            'shares': self.shares,
            'source_type': self.source_type,
            'source_agent_id': self.source_agent_id,
            'source_url': self.source_url,
            'tags': self.tags_list,
        }
    
    def can_view(self, viewer_id: str, trust_score: int = 0) -> bool:
        """Check if a user can view this post based on visibility settings.
        
        Default trust_score is 0 (untrusted) so callers must explicitly
        pass the viewer's actual trust level to allow trusted-visibility access.
        """
        if self.author_origin_peer and not bool(self.author_peer_trusted):
            return False
        if self.visibility == PostVisibility.PUBLIC:
            return True
        elif self.visibility == PostVisibility.NETWORK:
            return True  # Assume all users in local network
        elif self.visibility == PostVisibility.TRUSTED:
            return trust_score >= 50  # Trust threshold
        elif self.visibility == PostVisibility.PRIVATE:
            return viewer_id == self.author_id
        elif self.visibility == PostVisibility.CUSTOM:
            return viewer_id in (self.permissions or [])
        return False


class FeedManager:
    """Manages social feed posts and algorithms."""

    DEFAULT_TTL_DAYS = 90  # Quarterly default
    DEFAULT_TTL_SECONDS = DEFAULT_TTL_DAYS * 24 * 3600
    # Upper bound on post retention to prevent unbounded growth.
    MAX_TTL_DAYS = 730  # 2 years
    MAX_TTL_SECONDS = MAX_TTL_DAYS * 24 * 3600
    # Backward-compatibility window for legacy no-expiry semantics.
    LEGACY_NO_EXPIRY_TTL_DAYS = 365  # 1 year
    LEGACY_NO_EXPIRY_TTL_SECONDS = LEGACY_NO_EXPIRY_TTL_DAYS * 24 * 3600
    
    def __init__(self, db_manager: DatabaseManager, api_key_manager: ApiKeyManager,
                 data_encryptor: Any = None, trust_manager: Any = None):
        """Initialize feed manager with database and API key manager."""
        self.db = db_manager
        self.api_key_manager = api_key_manager
        self.data_encryptor = data_encryptor
        self.trust_manager = trust_manager
        self.workspace_events: Any = None
        self.max_content_length = 4096  # 4KB for text posts
        self.supported_media_types = [
            'image/jpeg', 'image/png', 'image/gif',
            'video/mp4', 'video/webm',
            'audio/mp3', 'audio/wav', 'audio/ogg'
        ]

    @staticmethod
    def _build_event_preview(content: str, fallback: str = 'Feed activity') -> str:
        preview = ' '.join(str(content or '').split()).strip()
        if not preview:
            return fallback
        if len(preview) > 160:
            return preview[:157].rstrip() + '...'
        return preview

    def _emit_post_event(
        self,
        *,
        event_type: str,
        post: Optional['Post'],
        created_at: Optional[datetime] = None,
        preview: Optional[str] = None,
        update_reason: Optional[str] = None,
    ) -> None:
        manager = self.workspace_events
        if not manager or not post:
            return
        visibility_value = post.visibility.value if hasattr(post.visibility, 'value') else str(post.visibility or 'network')
        created_dt = created_at or datetime.now(timezone.utc)
        manager.emit_event(
            event_type=event_type,
            actor_user_id=post.author_id,
            post_id=post.id,
            visibility_scope='feed',
            dedupe_key=f"{event_type}:{post.id}:{created_dt.isoformat()}",
            created_at=created_dt,
            payload={
                'author_id': post.author_id,
                'post_type': post.post_type.value if hasattr(post.post_type, 'value') else str(post.post_type or 'text'),
                'preview': preview or self._build_event_preview(post.content or ''),
                'visibility': visibility_value,
                'permissions': list(post.permissions or []),
                'source_type': post.source_type or 'human',
                'update_reason': update_reason or '',
            },
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

    def _resolve_expiry(self,
                        expires_at: Optional[Any] = None,
                        ttl_seconds: Optional[int] = None,
                        ttl_mode: Optional[str] = None,
                        apply_default: bool = True,
                        base_time: Optional[datetime] = None) -> Optional[datetime]:
        """Resolve expiry for a post based on explicit expiry, TTL, or defaults."""
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
    
    @log_performance('feed')
    def create_post(self, author_id: str, content: str,
                   post_type: PostType = PostType.TEXT,
                   visibility: PostVisibility = PostVisibility.PRIVATE,
                   metadata: Optional[Dict[str, Any]] = None,
                   permissions: Optional[List[str]] = None,
                   source_type: str = 'human',
                   source_agent_id: Optional[str] = None,
                   source_url: Optional[str] = None,
                   tags: Optional[List[str]] = None,
                   expires_at: Optional[Any] = None,
                   ttl_seconds: Optional[int] = None,
                   ttl_mode: Optional[str] = None,
                   allow_source_reference: bool = False) -> Optional[Post]:
        """Create a new social media post."""
        logger.info(f"Creating post by author_id={author_id}, type={post_type.value}, visibility={visibility.value}, source={source_type}")
        logger.debug(f"Content length: {len(content)}, permissions: {permissions}")
        
        try:
            metadata = _normalize_post_metadata(metadata, allow_source_reference=allow_source_reference)
            # Validate content length
            if len(content) > self.max_content_length:
                logger.error(f"Post content too long: {len(content)} > {self.max_content_length}")
                return None
            
            # Validate author exists
            with self.db.get_connection() as conn:
                cursor = conn.execute("SELECT id FROM users WHERE id = ?", (author_id,))
                if not cursor.fetchone():
                    logger.error(f"Author {author_id} not found in database")
                    return None
            
            # Generate unique post ID
            post_id = secrets.token_hex(16)
            logger.debug(f"Generated post ID: {post_id}")

            # Normalize tags to JSON string
            tags_json = json.dumps(tags) if tags else None
            
            # Resolve expiry (default to quarterly unless explicitly set to no-expiry)
            created_at = datetime.now(timezone.utc)
            expires_dt = self._resolve_expiry(
                expires_at,
                ttl_seconds,
                ttl_mode,
                apply_default=True,
                base_time=created_at,
            )
            expires_db = self._format_db_timestamp(expires_dt) if expires_dt else None

            # Create post object
            post = Post(
                id=post_id,
                author_id=author_id,
                content=content,
                post_type=post_type,
                visibility=visibility,
                created_at=created_at,
                expires_at=expires_dt,
                metadata=metadata,
                permissions=permissions,
                source_type=source_type,
                source_agent_id=source_agent_id,
                source_url=source_url,
                tags=tags_json,
            )
            
            logger.debug(f"Created post object: {post}")
            
            # Encrypt content before storage
            stored_content = content
            if self.data_encryptor and self.data_encryptor.is_enabled:
                stored_content = self.data_encryptor.encrypt(content)
            
            # Store in database
            with LogOperation(f"Database insert for post {post_id}"):
                with self.db.get_connection() as conn:
                    logger.debug("Inserting post into feed_posts table")
                    conn.execute("""
                        INSERT INTO feed_posts (id, author_id, content, content_type, visibility, metadata,
                                                source_type, source_agent_id, source_url, tags, expires_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        post_id, author_id, stored_content, post_type.value, 
                        visibility.value, json.dumps(metadata) if metadata else None,
                        source_type, source_agent_id, source_url, tags_json, expires_db
                    ))
                    
                    # Store custom permissions if needed
                    if visibility == PostVisibility.CUSTOM and permissions:
                        logger.debug(f"Inserting {len(permissions)} custom permissions")
                        for user_id in permissions:
                            conn.execute("""
                                INSERT INTO post_permissions (post_id, user_id)
                                VALUES (?, ?)
                            """, (post_id, user_id))
                    
                    conn.commit()
                    logger.debug("Database transaction committed successfully")
            
            logger.info(f"Successfully created post {post_id} by {author_id}")
            self._emit_post_event(
                event_type=EVENT_FEED_POST_CREATED,
                post=post,
                created_at=created_at,
            )
            return post
            
        except Exception as e:
            logger.error(f"Failed to create post: {e}", exc_info=True)
            return None
    
    def _decrypt_content(self, content: str) -> str:
        """Decrypt content if encryption is enabled."""
        if self.data_encryptor and self.data_encryptor.is_enabled:
            return cast(str, self.data_encryptor.decrypt(content))
        return content

    def _row_to_post(self, row: Any, conn: Any = None) -> Post:
        """Convert a database row to a Post object."""
        permissions = None
        if row['visibility'] == 'custom' and conn:
            perm_cursor = conn.execute(
                "SELECT user_id FROM post_permissions WHERE post_id = ?",
                (row['id'],))
            permissions = [r['user_id'] for r in perm_cursor.fetchall()]

        expires_dt = None
        if 'expires_at' in row.keys() and row['expires_at']:
            expires_dt = self._parse_datetime(row['expires_at'])

        author_origin_peer = None
        if 'author_origin_peer' in row.keys():
            author_origin_peer = row['author_origin_peer']
        elif conn is not None:
            try:
                origin_row = conn.execute(
                    "SELECT origin_peer FROM users WHERE id = ?",
                    (row['author_id'],),
                ).fetchone()
                if origin_row:
                    author_origin_peer = origin_row['origin_peer'] if 'origin_peer' in origin_row.keys() else origin_row[0]
            except Exception:
                author_origin_peer = None

        return Post(
            id=row['id'],
            author_id=row['author_id'],
            content=self._decrypt_content(row['content']),
            post_type=PostType(row['content_type']),
            visibility=PostVisibility(row['visibility']),
            created_at=datetime.fromisoformat(row['created_at']),
            expires_at=expires_dt,
            metadata=_normalize_post_metadata(
                json.loads(row['metadata']) if row['metadata'] else None,
                allow_source_reference=True,
                preserve_legacy_repost_keys=True,
            ),
            permissions=permissions,
            likes=row['likes'] if 'likes' in row.keys() else 0,
            comments=row['comments'] if 'comments' in row.keys() else 0,
            shares=row['shares'] if 'shares' in row.keys() else 0,
            source_type=row['source_type'] if 'source_type' in row.keys() else 'human',
            source_agent_id=row['source_agent_id'] if 'source_agent_id' in row.keys() else None,
            source_url=row['source_url'] if 'source_url' in row.keys() else None,
            tags=row['tags'] if 'tags' in row.keys() else None,
            author_origin_peer=str(author_origin_peer or '').strip() or None,
            author_peer_trusted=self._is_author_origin_peer_trusted(author_origin_peer),
        )

    def _is_author_origin_peer_trusted(self, origin_peer: Any) -> bool:
        peer_id = str(origin_peer or '').strip()
        if not peer_id:
            return True
        if not self.trust_manager:
            return False
        try:
            if hasattr(self.trust_manager, 'has_explicit_trust_score') and not self.trust_manager.has_explicit_trust_score(peer_id):
                return False
        except Exception:
            return False
        try:
            return bool(self.trust_manager.is_peer_trusted(peer_id))
        except Exception:
            return False

    def _post_visible_to_user(self, post: Post, user_id: str) -> bool:
        if not user_id:
            return False
        return bool(post.can_view(user_id, 50))
    
    def get_post(self, post_id: str) -> Optional[Post]:
        """Get a specific post by ID."""
        try:
            with self.db.get_connection() as conn:
                cursor = conn.execute("""
                    SELECT p.*, u.username as author_username
                    FROM feed_posts p
                    LEFT JOIN users u ON p.author_id = u.id
                    WHERE p.id = ?
                """, (post_id,))
                
                row = cursor.fetchone()
                if not row:
                    return None

                post = self._row_to_post(row, conn)
                if post.is_expired:
                    return None
                return post
                
        except Exception as e:
            logger.error(f"Failed to get post {post_id}: {e}")
            return None
    
    def get_user_feed(self, user_id: str, limit: int = 50, 
                     algorithm: str = 'chronological') -> List[Post]:
        """Get personalized feed for a user.
        
        If algorithm is 'smart', uses the user's FeedAlgorithm preferences
        to score and rank posts locally. Otherwise falls back to simple
        SQL-based ordering (chronological, popularity, relevance).
        """
        try:
            with self.db.get_connection() as conn:
                # For 'smart' algorithm, load user preferences and score locally
                if algorithm == 'smart':
                    return self._get_smart_feed(user_id, limit, conn)

                # Legacy SQL-based algorithms
                query = """
                    SELECT DISTINCT p.*, u.username as author_username
                    FROM feed_posts p
                    LEFT JOIN users u ON p.author_id = u.id
                    LEFT JOIN post_permissions pp ON p.id = pp.post_id
                    WHERE (
                        p.visibility = 'public' OR
                        p.visibility = 'network' OR
                        p.visibility = 'trusted' OR
                        (p.visibility = 'custom' AND pp.user_id = ?) OR
                        p.author_id = ?
                    ) AND (p.expires_at IS NULL OR p.expires_at > CURRENT_TIMESTAMP)
                """
                
                if algorithm == 'chronological':
                    query += " ORDER BY COALESCE(p.last_activity_at, p.created_at) DESC"
                elif algorithm == 'popularity':
                    query += " ORDER BY (p.likes + p.comments + p.shares) DESC, COALESCE(p.last_activity_at, p.created_at) DESC"
                elif algorithm == 'relevance':
                    query += (
                        " ORDER BY"
                        "   (CASE WHEN p.author_id = ? THEN 1.5 ELSE 1.0 END)"
                        "   * (1 + p.likes + p.comments * 2 + p.shares * 3)"
                        "   / (1 + (julianday('now') - julianday(COALESCE(p.last_activity_at, p.created_at)))) DESC"
                    )
                
                query += " LIMIT ?"
                
                params: List[Any] = [user_id, user_id]
                if algorithm == 'relevance':
                    params.append(user_id)
                params.append(limit)
                
                cursor = conn.execute(query, params)
                posts = [self._row_to_post(row, conn) for row in cursor.fetchall()]
                return [post for post in posts if self._post_visible_to_user(post, user_id)]
                
        except Exception as e:
            logger.error(f"Failed to get feed for user {user_id}: {e}")
            return []

    def get_posts_since(self, user_id: str, since: datetime, limit: int = 50) -> List[Post]:
        """Get feed posts visible to a user after a timestamp."""
        if not user_id or not since:
            return []

        try:
            limit_val = max(1, min(int(limit or 50), 200))
        except Exception:
            limit_val = 50

        since_db = self._format_db_timestamp(since)

        try:
            with self.db.get_connection() as conn:
                query = """
                    SELECT DISTINCT p.*, u.username as author_username
                    FROM feed_posts p
                    LEFT JOIN users u ON p.author_id = u.id
                    LEFT JOIN post_permissions pp ON p.id = pp.post_id
                    WHERE (
                        p.visibility = 'public' OR
                        p.visibility = 'network' OR
                        p.visibility = 'trusted' OR
                        (p.visibility = 'custom' AND pp.user_id = ?) OR
                        p.author_id = ?
                    )
                      AND p.created_at > ?
                      AND (p.expires_at IS NULL OR p.expires_at > CURRENT_TIMESTAMP)
                    ORDER BY p.created_at DESC
                    LIMIT ?
                """
                rows = conn.execute(query, (user_id, user_id, since_db, limit_val)).fetchall()
                posts = [self._row_to_post(row, conn) for row in rows]
                return [post for post in posts if self._post_visible_to_user(post, user_id)]
        except Exception as e:
            logger.error(f"Failed to get posts since {since}: {e}")
            return []

    def _get_smart_feed(self, user_id: str, limit: int, conn: Any) -> List[Post]:
        """Fetch posts, score with user's FeedAlgorithm, return top results."""
        algo = self.get_feed_algorithm(user_id)

        # Fetch candidate posts (broader pool, filter client-side)
        max_age_clause = ""
        if algo.max_age_days and algo.max_age_days > 0:
            max_age_clause = f"AND p.created_at >= datetime('now', '-{int(algo.max_age_days)} days')"

        query = f"""
            SELECT DISTINCT p.*, u.username as author_username
            FROM feed_posts p
            LEFT JOIN users u ON p.author_id = u.id
            LEFT JOIN post_permissions pp ON p.id = pp.post_id
            WHERE (
                p.visibility = 'public' OR
                p.visibility = 'network' OR
                p.visibility = 'trusted' OR
                (p.visibility = 'custom' AND pp.user_id = ?) OR
                p.author_id = ?
            ) AND (p.expires_at IS NULL OR p.expires_at > CURRENT_TIMESTAMP) {max_age_clause}
            ORDER BY COALESCE(p.last_activity_at, p.created_at) DESC
            LIMIT ?
        """
        # Fetch more than needed so scoring + filtering has room
        pool_size = min(limit * 5, 500)
        cursor = conn.execute(query, (user_id, user_id, pool_size))
        
        scored = []
        for row in cursor.fetchall():
            try:
                post = self._row_to_post(row, conn)
                if not self._post_visible_to_user(post, user_id):
                    continue
                score = algo.score_post(post, user_id)
                if score >= 0:
                    scored.append((score, post))
            except Exception:
                continue

        # Sort by score descending, return top N
        scored.sort(key=lambda x: x[0], reverse=True)
        return [post for _, post in scored[:limit]]
    
    def get_user_posts(self, author_id: str, limit: int = 50,
                       viewer_id: Optional[str] = None) -> List[Post]:
        """Get posts by a specific user, filtered by visibility.
        
        When viewer_id is provided, applies the standard visibility filter
        so private/custom posts are only returned to authorised viewers.
        When viewer_id is None, only public/network/trusted posts are returned.
        """
        try:
            with self.db.get_connection() as conn:
                if viewer_id:
                    cursor = conn.execute("""
                        SELECT p.*, u.username as author_username
                        FROM feed_posts p
                        LEFT JOIN users u ON p.author_id = u.id
                        LEFT JOIN post_permissions pp ON p.id = pp.post_id
                        WHERE p.author_id = ?
                          AND (p.expires_at IS NULL OR p.expires_at > CURRENT_TIMESTAMP)
                          AND (
                              p.visibility = 'public'
                              OR p.visibility = 'network'
                              OR p.visibility = 'trusted'
                              OR p.author_id = ?
                              OR (p.visibility = 'custom' AND pp.user_id = ?)
                          )
                        ORDER BY p.created_at DESC
                        LIMIT ?
                    """, (author_id, viewer_id, viewer_id, limit))
                else:
                    cursor = conn.execute("""
                        SELECT p.*, u.username as author_username
                        FROM feed_posts p
                        LEFT JOIN users u ON p.author_id = u.id
                        WHERE p.author_id = ?
                          AND (p.expires_at IS NULL OR p.expires_at > CURRENT_TIMESTAMP)
                          AND p.visibility IN ('public', 'network', 'trusted')
                        ORDER BY p.created_at DESC
                        LIMIT ?
                    """, (author_id, limit))

                posts = [self._row_to_post(row, conn) for row in cursor.fetchall()]
                if viewer_id:
                    return [post for post in posts if self._post_visible_to_user(post, viewer_id)]
                return [post for post in posts if post.author_peer_trusted]

        except Exception as e:
            logger.error(f"Failed to get posts for user {author_id}: {e}")
            return []
    
    def update_post(self, post_id: str, user_id: str, content: str,
                   post_type: Optional[PostType] = None,
                   visibility: Optional[PostVisibility] = None,
                   metadata: Optional[Dict[str, Any]] = None,
                   permissions: Optional[List[str]] = None,
                   allow_admin: bool = False) -> bool:
        """Update a post (only author can update)."""
        try:
            with self.db.get_connection() as conn:
                # Check if user is the author and get current post
                cursor = conn.execute("""
                    SELECT author_id, content_type, visibility, metadata FROM feed_posts WHERE id = ?
                """, (post_id,))
                
                row = cursor.fetchone()
                if not row or (row['author_id'] != user_id and not allow_admin):
                    logger.warning(f"User {user_id} cannot update post {post_id}")
                    return False
                
                # Validate content length
                if len(content) > self.max_content_length:
                    logger.error(f"Post content too long: {len(content)} > {self.max_content_length}")
                    return False
                
                # Use existing values if not provided
                final_post_type = post_type or PostType(row['content_type'])
                final_visibility = visibility or PostVisibility(row['visibility'])
                existing_metadata = json.loads(row['metadata']) if row['metadata'] else None
                existing_source_reference = _extract_source_reference(existing_metadata)
                final_metadata = metadata if metadata is not None else existing_metadata
                final_metadata = _normalize_post_metadata(final_metadata, allow_source_reference=False)
                if existing_source_reference:
                    if not final_metadata:
                        final_metadata = {}
                    final_metadata['source_reference'] = existing_source_reference
                
                # Update the post
                cursor = conn.execute("""
                    UPDATE feed_posts 
                    SET content = ?, content_type = ?, visibility = ?, metadata = ?
                    WHERE id = ?
                """, (
                    content, 
                    final_post_type.value,
                    final_visibility.value,
                    json.dumps(final_metadata) if final_metadata else None,
                    post_id
                ))
                
                success = cast(int, cursor.rowcount) > 0
                
                # Update permissions if visibility changed to/from custom
                if final_visibility == PostVisibility.CUSTOM:
                    # Delete existing permissions
                    conn.execute("DELETE FROM post_permissions WHERE post_id = ?", (post_id,))
                    
                    # Add new permissions if provided
                    if permissions:
                        for perm_user_id in permissions:
                            conn.execute("""
                                INSERT INTO post_permissions (post_id, user_id)
                                VALUES (?, ?)
                            """, (post_id, perm_user_id))
                elif row['visibility'] == 'custom':
                    # Changed from custom to something else, remove permissions
                    conn.execute("DELETE FROM post_permissions WHERE post_id = ?", (post_id,))
                
                conn.commit()
                
                if success:
                    logger.info(f"Updated post {post_id}")
                    updated_post = self.get_post(post_id)
                    if updated_post:
                        self._emit_post_event(
                            event_type=EVENT_FEED_POST_UPDATED,
                            post=updated_post,
                            created_at=datetime.now(timezone.utc),
                            update_reason='edit',
                        )
                
                return success
                
        except Exception as e:
            logger.error(f"Failed to update post: {e}")
            return False

    def update_post_expiry(self, post_id: str, user_id: str,
                          expires_at: Optional[Any] = None,
                          ttl_seconds: Optional[int] = None,
                          ttl_mode: Optional[str] = None,
                          allow_admin: bool = False) -> Optional[datetime]:
        """Update a post's expiry (author or admin). Returns new expiry datetime."""
        try:
            with self.db.get_connection() as conn:
                row = conn.execute(
                    "SELECT author_id FROM feed_posts WHERE id = ?",
                    (post_id,)
                ).fetchone()
                if not row or (row['author_id'] != user_id and not allow_admin):
                    logger.warning(f"User {user_id} cannot update expiry for post {post_id}")
                    return None

                # Resolve expiry relative to now
                base_time = datetime.now(timezone.utc)
                expires_dt = self._resolve_expiry(
                    expires_at=expires_at,
                    ttl_seconds=ttl_seconds,
                    ttl_mode=ttl_mode,
                    apply_default=False,
                    base_time=base_time,
                )
                expires_db = self._format_db_timestamp(expires_dt) if expires_dt else None

                conn.execute(
                    "UPDATE feed_posts SET expires_at = ? WHERE id = ?",
                    (expires_db, post_id)
                )
                # Keep comments in sync with parent post expiry
                try:
                    conn.execute(
                        "UPDATE comments SET expires_at = ? WHERE message_id = ?",
                        (expires_db, post_id)
                    )
                except Exception:
                    pass
                conn.commit()
                return expires_dt
        except Exception as e:
            logger.error(f"Failed to update post expiry: {e}")
            return None

    def delete_post(self, post_id: str, user_id: str, allow_admin: bool = False) -> bool:
        """Delete a post (only author can delete unless admin)."""
        try:
            deleted_post = self.get_post(post_id)
            with self.db.get_connection() as conn:
                # Check if user is the author
                cursor = conn.execute("""
                    SELECT author_id FROM feed_posts WHERE id = ?
                """, (post_id,))
                
                row = cursor.fetchone()
                if not row or (row['author_id'] != user_id and not allow_admin):
                    logger.warning(f"User {user_id} cannot delete post {post_id}")
                    return False
                
                # Delete the post and permissions
                conn.execute("DELETE FROM post_permissions WHERE post_id = ?", (post_id,))
                cursor = conn.execute("DELETE FROM feed_posts WHERE id = ?", (post_id,))
                
                success = cast(int, cursor.rowcount) > 0
                conn.commit()
                
                if success:
                    logger.info(f"Deleted post {post_id}")
                    self._emit_post_event(
                        event_type=EVENT_FEED_POST_DELETED,
                        post=deleted_post,
                        created_at=datetime.now(timezone.utc),
                        update_reason='delete',
                    )
                
                return success
                
        except Exception as e:
            logger.error(f"Failed to delete post: {e}")
            return False

    def purge_expired_posts(self) -> List[Dict[str, Any]]:
        """Remove expired posts and related records. Returns list of purged posts."""
        purged: List[Dict[str, Any]] = []
        try:
            with self.db.get_connection() as conn:
                rows = conn.execute("""
                    SELECT id, author_id, expires_at, metadata
                    FROM feed_posts
                    WHERE expires_at IS NOT NULL
                      AND expires_at <= CURRENT_TIMESTAMP
                """).fetchall()

                if not rows:
                    return purged

                post_ids = [row['id'] for row in rows]
                purged = []
                for row in rows:
                    attachment_ids: List[str] = []
                    if row['metadata']:
                        try:
                            meta = json.loads(row['metadata'])
                            atts = (meta or {}).get('attachments') or []
                            for att in atts:
                                if isinstance(att, dict):
                                    file_id = att.get('id')
                                    if file_id:
                                        attachment_ids.append(file_id)
                        except Exception:
                            pass
                    purged.append({
                        'id': row['id'],
                        'author_id': row['author_id'],
                        'expires_at': row['expires_at'],
                        'attachment_ids': attachment_ids,
                    })

                # Helper: delete from a table if it exists.
                def delete_if_exists(table: str, column: str = 'post_id') -> None:
                    exists = conn.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                        (table,)
                    ).fetchone()
                    if not exists:
                        return
                    placeholders = ",".join("?" for _ in post_ids)
                    conn.execute(
                        f"DELETE FROM {table} WHERE {column} IN ({placeholders})",
                        post_ids,
                    )

                # Cleanup dependent records
                delete_if_exists('post_permissions', 'post_id')
                delete_if_exists('post_content_keys', 'post_id')
                delete_if_exists('likes', 'message_id')
                delete_if_exists('comments', 'message_id')

                placeholders = ",".join("?" for _ in post_ids)
                conn.execute(f"DELETE FROM feed_posts WHERE id IN ({placeholders})", post_ids)
                conn.commit()

                logger.info(f"Purged {len(post_ids)} expired posts")

        except Exception as e:
            logger.error(f"Failed to purge expired posts: {e}", exc_info=True)

        return purged

    def get_repost_policy(self, metadata: Any) -> str:
        """Resolve repost policy from metadata; default to same-scope reposts."""
        return _normalize_repost_policy(
            metadata.get('repost_policy') if isinstance(metadata, dict) else None
        ) or 'same_scope'

    def is_repost_post(self, post: Optional[Post]) -> bool:
        """Return True when a post is a repost wrapper (new or legacy)."""
        if not post:
            return False
        return _is_repost_metadata(post.metadata)

    def is_variant_post(self, post: Optional[Post]) -> bool:
        """Return True when a post is a lineage variant wrapper."""
        if not post:
            return False
        return _is_variant_metadata(post.metadata)

    def _get_post_for_reference(
        self,
        post_id: str,
        viewer_id: Optional[str] = None,
    ) -> tuple[str, Optional[Post]]:
        """Resolve a feed post for repost rendering without leaking unavailable content."""
        try:
            with self.db.get_connection() as conn:
                cursor = conn.execute(
                    """
                    SELECT p.*, u.username as author_username
                    FROM feed_posts p
                    LEFT JOIN users u ON p.author_id = u.id
                    WHERE p.id = ?
                    """,
                    (post_id,),
                )
                row = cursor.fetchone()
                if not row:
                    return ('missing', None)

                post = self._row_to_post(row, conn)
                if post.is_expired:
                    return ('expired', None)
                if self.get_repost_policy(post.metadata) == 'deny':
                    return ('policy_denied', None)
                if viewer_id and not post.can_view(viewer_id, 50):
                    return ('access_changed', None)
                return ('available', post)
        except Exception as e:
            logger.error(f"Failed to resolve repost source {post_id}: {e}", exc_info=True)
            return ('missing', None)

    def get_repost_eligibility(self, post_id: str, viewer_id: str) -> Dict[str, Any]:
        """Evaluate whether a user can create a repost wrapper for a feed post."""
        state, original = self._get_post_for_reference(post_id, viewer_id)
        if state != 'available' or not original:
            if state == 'policy_denied':
                return {
                    'allowed': False,
                    'status_code': 403,
                    'reason': 'This post cannot be reposted',
                }
            return {
                'allowed': False,
                'status_code': 404 if state in {'missing', 'expired'} else 403,
                'reason': 'Post not found' if state in {'missing', 'expired'} else 'Access denied',
            }

        if self.is_repost_post(original):
            return {
                'allowed': False,
                'status_code': 400,
                'reason': 'Repost chains are not supported',
            }

        if original.visibility not in _REPOST_ELIGIBLE_VISIBILITIES:
            return {
                'allowed': False,
                'status_code': 403,
                'reason': 'Only public, network, or trusted posts can be reposted in v1',
            }

        if self.get_repost_policy(original.metadata) == 'deny':
            return {
                'allowed': False,
                'status_code': 403,
                'reason': 'This post cannot be reposted',
            }

        return {
            'allowed': True,
            'status_code': 200,
            'reason': 'ok',
            'post': original,
        }

    def resolve_repost_reference(self, post: Post, viewer_id: str) -> Optional[Dict[str, Any]]:
        """Resolve a repost wrapper into a live original-source preview contract."""
        source_reference = _extract_source_reference(post.metadata)
        if not source_reference:
            return None
        if str(source_reference.get('kind') or '').strip().lower() not in {'repost_v1', 'legacy_share'}:
            return None

        source_id = str(source_reference.get('source_id') or '').strip()
        result: Dict[str, Any] = {
            'kind': str(source_reference.get('kind') or '').strip() or 'repost_v1',
            'source_type': 'feed_post',
            'source_id': source_id,
            'available': False,
            'unavailable_reason': 'missing',
            'legacy': bool(source_reference.get('legacy')),
        }
        if not source_id:
            return result

        state, original = self._get_post_for_reference(source_id, viewer_id)
        if state != 'available' or not original:
            result['unavailable_reason'] = state
            return result

        source_layout = (
            original.metadata.get('source_layout')
            if isinstance(original.metadata, dict) and isinstance(original.metadata.get('source_layout'), dict)
            else None
        )
        deck_default_ref = (
            str(source_layout.get('deck', {}).get('default_ref') or '').strip()
            if isinstance(source_layout, dict) and isinstance(source_layout.get('deck'), dict)
            else ''
        )
        body_text, body_truncated = _truncate_repost_reference_body(original.content)
        embed = _repost_embed_from_original(original)
        meta = original.metadata if isinstance(original.metadata, dict) else {}
        # UI uses has_source_layout to show the Deck control on repost cards; originals may
        # be deck-capable via media/embed/module attachments without persisted source_layout.
        has_deck_ui = (
            bool(source_layout)
            or _repost_embed_implies_deck_queue(embed)
            or _metadata_has_canopy_module_attachment(meta, self.db)
            or _metadata_has_any_file_attachment(meta)
        )
        result.update({
            'available': True,
            'unavailable_reason': None,
            'author_id': original.author_id,
            'created_at': original.created_at.isoformat(),
            'visibility': original.visibility.value,
            'post_type': original.post_type.value,
            'preview_text': _build_preview_text(original.content),
            'body_text': body_text,
            'body_truncated': body_truncated,
            'embed': embed,
            'has_source_layout': has_deck_ui,
            'deck_default_ref': deck_default_ref or None,
        })
        return result

    def get_variant_eligibility(self, post_id: str, viewer_id: str) -> Dict[str, Any]:
        """Evaluate whether a user can create a lineage variant for a feed post."""
        state, original = self._get_post_for_reference(post_id, viewer_id)
        if state != 'available' or not original:
            if state == 'policy_denied':
                return {
                    'allowed': False,
                    'status_code': 403,
                    'reason': 'This post cannot be used as a variant source',
                }
            return {
                'allowed': False,
                'status_code': 404 if state in {'missing', 'expired'} else 403,
                'reason': 'Post not found' if state in {'missing', 'expired'} else 'Access denied',
            }

        if self.is_repost_post(original):
            return {
                'allowed': False,
                'status_code': 400,
                'reason': 'Repost wrappers cannot be used as variant sources',
            }

        if original.visibility not in _REPOST_ELIGIBLE_VISIBILITIES:
            return {
                'allowed': False,
                'status_code': 403,
                'reason': 'Only public, network, or trusted posts can be variant sources in v1',
            }

        if self.get_repost_policy(original.metadata) == 'deny':
            return {
                'allowed': False,
                'status_code': 403,
                'reason': 'This post does not allow variants',
            }

        return {
            'allowed': True,
            'status_code': 200,
            'reason': 'ok',
            'post': original,
        }

    def resolve_variant_reference(self, post: Post, viewer_id: str) -> Optional[Dict[str, Any]]:
        """Resolve a lineage variant into a live antecedent-source preview contract."""
        source_reference = _extract_source_reference(post.metadata)
        if not source_reference:
            return None
        if str(source_reference.get('kind') or '').strip().lower() != 'variant_v1':
            return None

        source_id = str(source_reference.get('source_id') or '').strip()
        relationship_kind = _normalize_variant_relationship(source_reference.get('relationship_kind'))
        result: Dict[str, Any] = {
            'kind': 'variant_v1',
            'source_type': 'feed_post',
            'source_id': source_id,
            'available': False,
            'unavailable_reason': 'missing',
            'relationship_kind': relationship_kind,
            'module_param_delta': str(source_reference.get('module_param_delta') or '').strip() or None,
        }
        if not source_id:
            return result

        state, original = self._get_post_for_reference(source_id, viewer_id)
        if state != 'available' or not original:
            result['unavailable_reason'] = state
            return result

        source_layout = (
            original.metadata.get('source_layout')
            if isinstance(original.metadata, dict) and isinstance(original.metadata.get('source_layout'), dict)
            else None
        )
        deck_default_ref = (
            str(source_layout.get('deck', {}).get('default_ref') or '').strip()
            if isinstance(source_layout, dict) and isinstance(source_layout.get('deck'), dict)
            else ''
        )
        body_text, body_truncated = _truncate_repost_reference_body(original.content)
        embed = _repost_embed_from_original(original)
        meta = original.metadata if isinstance(original.metadata, dict) else {}
        has_deck_ui = (
            bool(source_layout)
            or _repost_embed_implies_deck_queue(embed)
            or _metadata_has_canopy_module_attachment(meta, self.db)
            or _metadata_has_any_file_attachment(meta)
        )
        result.update({
            'available': True,
            'unavailable_reason': None,
            'author_id': original.author_id,
            'created_at': original.created_at.isoformat(),
            'visibility': original.visibility.value,
            'post_type': original.post_type.value,
            'preview_text': _build_preview_text(original.content),
            'body_text': body_text,
            'body_truncated': body_truncated,
            'embed': embed,
            'has_source_layout': has_deck_ui,
            'deck_default_ref': deck_default_ref or None,
        })
        return result

    def create_repost(self, post_id: str, user_id: str, comment: str = '') -> Optional[Post]:
        """Create a secure repost wrapper for an eligible feed post."""
        try:
            eligibility = self.get_repost_eligibility(post_id, user_id)
            if not eligibility.get('allowed'):
                logger.warning(
                    "Cannot repost post %s for user %s: %s",
                    post_id,
                    user_id,
                    eligibility.get('reason'),
                )
                return None

            original = cast(Post, eligibility['post'])
            repost_meta = _normalize_post_metadata(
                {
                    'source_reference': {
                        'kind': 'repost_v1',
                        'source_type': 'feed_post',
                        'source_id': original.id,
                        'source_visibility': original.visibility.value,
                        'created_by_user_id': user_id,
                    }
                },
                allow_source_reference=True,
            )
            shared = self.create_post(
                author_id=user_id,
                content=str(comment or '').strip(),
                post_type=PostType.TEXT,
                visibility=original.visibility,
                metadata=repost_meta,
                allow_source_reference=True,
            )

            if shared:
                with self.db.get_connection() as conn:
                    conn.execute(
                        "UPDATE feed_posts SET shares = shares + 1 WHERE id = ?",
                        (post_id,),
                    )
                    conn.commit()
                logger.info("User %s reposted post %s as %s", user_id, post_id, shared.id)

            return cast(Optional[Post], shared)
        except Exception as e:
            logger.error(f"Failed to create repost: {e}", exc_info=True)
            return None

    def create_variant(
        self,
        post_id: str,
        user_id: str,
        comment: str = '',
        *,
        relationship_kind: str = 'curated_recomposition',
        module_param_delta: str = '',
    ) -> Optional[Post]:
        """Create a lineage-preserving variant wrapper for an eligible feed post."""
        try:
            eligibility = self.get_variant_eligibility(post_id, user_id)
            if not eligibility.get('allowed'):
                logger.warning(
                    "Cannot create variant from post %s for user %s: %s",
                    post_id,
                    user_id,
                    eligibility.get('reason'),
                )
                return None

            original = cast(Post, eligibility['post'])
            variant_meta = _normalize_post_metadata(
                {
                    'source_reference': {
                        'kind': 'variant_v1',
                        'source_type': 'feed_post',
                        'source_id': original.id,
                        'source_visibility': original.visibility.value,
                        'created_by_user_id': user_id,
                        'relationship_kind': relationship_kind,
                        'module_param_delta': module_param_delta,
                    }
                },
                allow_source_reference=True,
            )
            variant = self.create_post(
                author_id=user_id,
                content=str(comment or '').strip(),
                post_type=PostType.TEXT,
                visibility=original.visibility,
                metadata=variant_meta,
                allow_source_reference=True,
            )
            if variant:
                logger.info(
                    "User %s created variant %s from post %s",
                    user_id,
                    variant.id,
                    post_id,
                )
            return cast(Optional[Post], variant)
        except Exception as e:
            logger.error(f"Failed to create variant: {e}", exc_info=True)
            return None

    def share_post(self, post_id: str, user_id: str, comment: str = '') -> Optional[Post]:
        """Backward-compatible alias for secure repost creation."""
        return self.create_repost(post_id, user_id, comment)

    def search_posts(self, query: str, user_id: str, limit: int = 20) -> List[Post]:
        """Search posts by content."""
        try:
            with self.db.get_connection() as conn:
                cursor = conn.execute("""
                    SELECT DISTINCT p.*, u.username as author_username
                    FROM feed_posts p
                    LEFT JOIN users u ON p.author_id = u.id
                    LEFT JOIN post_permissions pp ON p.id = pp.post_id
                    WHERE (
                        p.visibility = 'public' OR
                        p.visibility = 'network' OR
                        p.visibility = 'trusted' OR
                        (p.visibility = 'custom' AND pp.user_id = ?) OR
                        p.author_id = ?
                    ) AND (p.expires_at IS NULL OR p.expires_at > CURRENT_TIMESTAMP)
                      AND p.content LIKE ?
                    ORDER BY p.created_at DESC
                    LIMIT ?
                """, (user_id, user_id, f"%{query}%", limit))
                
                return [self._row_to_post(row, conn) for row in cursor.fetchall()]
                
        except Exception as e:
            logger.error(f"Failed to search posts: {e}")
            return []
    
    def get_feed_statistics(self, user_id: str) -> Dict[str, int]:
        """Get feed statistics for a user (includes custom-visibility posts)."""
        try:
            with self.db.get_connection() as conn:
                cursor = conn.execute("""
                    SELECT
                        COUNT(DISTINCT p.id) as total_posts,
                        SUM(CASE WHEN p.author_id = ? THEN 1 ELSE 0 END) as user_posts,
                        COUNT(DISTINCT p.author_id) as unique_authors
                    FROM feed_posts p
                    LEFT JOIN post_permissions pp ON p.id = pp.post_id
                    WHERE (
                        p.visibility = 'public'
                        OR p.visibility = 'network'
                        OR p.visibility = 'trusted'
                        OR p.author_id = ?
                        OR (p.visibility = 'custom' AND pp.user_id = ?)
                    )
                      AND (p.expires_at IS NULL OR p.expires_at > CURRENT_TIMESTAMP)
                """, (user_id, user_id, user_id))
                
                row = cursor.fetchone()
                return {
                    'total_posts': row['total_posts'] or 0,
                    'user_posts': row['user_posts'] or 0,
                    'unique_authors': row['unique_authors'] or 0
                }
                
        except Exception as e:
            logger.error(f"Failed to get feed statistics: {e}")
            return {'total_posts': 0, 'user_posts': 0, 'unique_authors': 0}

    # ------------------------------------------------------------------
    #  Feed algorithm preferences (per-user)
    # ------------------------------------------------------------------

    def get_feed_algorithm(self, user_id: str) -> FeedAlgorithm:
        """Load user's feed algorithm preferences, or return defaults."""
        try:
            with self.db.get_connection() as conn:
                cursor = conn.execute(
                    "SELECT algorithm_json FROM user_feed_preferences WHERE user_id = ?",
                    (user_id,))
                row = cursor.fetchone()
                if row and row['algorithm_json']:
                    data = json.loads(row['algorithm_json'])
                    return FeedAlgorithm.from_dict(data)
        except Exception as e:
            logger.warning(f"Failed to load feed algorithm for {user_id}: {e}")
        return FeedAlgorithm()

    def save_feed_algorithm(self, user_id: str, algo: FeedAlgorithm) -> bool:
        """Persist user's feed algorithm preferences."""
        try:
            with self.db.get_connection() as conn:
                conn.execute("""
                    INSERT INTO user_feed_preferences (user_id, algorithm_json, updated_at)
                    VALUES (?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(user_id) DO UPDATE SET
                        algorithm_json = excluded.algorithm_json,
                        updated_at = CURRENT_TIMESTAMP
                """, (user_id, json.dumps(algo.to_dict())))
                conn.commit()
            logger.info(f"Saved feed algorithm for {user_id}")
            return True
        except Exception as e:
            logger.error(f"Failed to save feed algorithm for {user_id}: {e}")
            return False

    def get_feed_last_viewed_at(self, user_id: str) -> Optional[datetime]:
        """Return the last time the user acknowledged the feed view."""
        try:
            with self.db.get_connection() as conn:
                row = conn.execute(
                    "SELECT last_viewed_at FROM user_feed_preferences WHERE user_id = ?",
                    (user_id,),
                ).fetchone()
                if not row:
                    return None
                return self._parse_datetime(row['last_viewed_at'])
        except Exception as e:
            logger.warning(f"Failed to load feed last_viewed_at for {user_id}: {e}")
            return None

    def mark_feed_viewed(self, user_id: str, viewed_at: Optional[datetime] = None) -> bool:
        """Record that the user has intentionally viewed the feed."""
        if not user_id:
            return False
        viewed_dt = viewed_at or datetime.now(timezone.utc)
        viewed_db = self._format_db_timestamp(viewed_dt)
        try:
            with self.db.get_connection() as conn:
                conn.execute("""
                    INSERT INTO user_feed_preferences (user_id, algorithm_json, last_viewed_at, updated_at)
                    VALUES (?, '{}', ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(user_id) DO UPDATE SET
                        last_viewed_at = excluded.last_viewed_at,
                        updated_at = CURRENT_TIMESTAMP
                """, (user_id, viewed_db))
                conn.commit()
            return True
        except Exception as e:
            logger.error(f"Failed to mark feed viewed for {user_id}: {e}")
            return False

    def count_unread_posts(self, user_id: str, *, exclude_own_posts: bool = True) -> int:
        """Count feed posts with new activity since the user's last acknowledged feed view."""
        if not user_id:
            return 0

        try:
            with self.db.get_connection() as conn:
                lv_row = conn.execute(
                    "SELECT last_viewed_at FROM user_feed_preferences WHERE user_id = ?",
                    (user_id,),
                ).fetchone()
                last_viewed_at = self._parse_datetime(lv_row['last_viewed_at']) if lv_row else None

                params: List[Any] = [user_id, user_id]
                own_clause = ""
                if exclude_own_posts:
                    own_clause = " AND p.author_id != ?"
                    params.append(user_id)
                since_clause = ""
                if last_viewed_at:
                    since_clause = " AND COALESCE(p.last_activity_at, p.created_at) > ?"
                    params.append(self._format_db_timestamp(last_viewed_at))

                row = conn.execute(f"""
                    SELECT COUNT(DISTINCT p.id) AS unread_count
                    FROM feed_posts p
                    LEFT JOIN post_permissions pp ON p.id = pp.post_id
                    WHERE (
                        p.visibility = 'public' OR
                        p.visibility = 'network' OR
                        p.visibility = 'trusted' OR
                        (p.visibility = 'custom' AND pp.user_id = ?) OR
                        p.author_id = ?
                    )
                      AND (p.expires_at IS NULL OR p.expires_at > CURRENT_TIMESTAMP)
                      {own_clause}
                      {since_clause}
                """, params).fetchone()
                return max(0, int((row['unread_count'] if row else 0) or 0))
        except Exception as e:
            logger.error(f"Failed to count unread feed posts for {user_id}: {e}")
            return 0

    def get_available_tags(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Get popular tags across all posts for the tag picker UI."""
        try:
            with self.db.get_connection() as conn:
                cursor = conn.execute(
                    "SELECT tags FROM feed_posts WHERE tags IS NOT NULL AND tags != ''"
                    " AND (expires_at IS NULL OR expires_at > CURRENT_TIMESTAMP)"
                    " ORDER BY created_at DESC LIMIT 2000"
                )
                tag_counts: Dict[str, int] = {}
                for row in cursor.fetchall():
                    try:
                        tags = json.loads(row['tags'])
                        if isinstance(tags, list):
                            for tag in tags:
                                tag = str(tag).strip().lower()
                                if tag:
                                    tag_counts[tag] = tag_counts.get(tag, 0) + 1
                    except (json.JSONDecodeError, TypeError):
                        continue
                
                sorted_tags = sorted(tag_counts.items(), key=lambda x: x[1], reverse=True)
                return [{'tag': t, 'count': c} for t, c in sorted_tags[:limit]]
        except Exception as e:
            logger.error(f"Failed to get available tags: {e}")
            return []
