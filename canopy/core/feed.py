"""
Social feed system for Canopy.

Implements Facebook-like timeline posts with permissions, media support,
and customizable feed algorithms.

Author: Konrad Walus (architecture, design, and direction)
Project: Canopy - Local Mesh Communication
License: Apache 2.0
Development: AI-assisted implementation (Claude, Codex, GitHub Copilot, Cursor IDE, Ollama)
"""

import logging
import math
import secrets
import json
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Any, Union, cast
from dataclasses import dataclass, field, asdict
from enum import Enum

from .database import DatabaseManager
from ..security.api_keys import ApiKeyManager, Permission
from ..security.encryption import RecipientEncryptor, RECIPIENT_ENCRYPTED_PREFIX
from .logging_config import log_performance, LogOperation

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
        if not self.show_reposts and post.metadata and post.metadata.get('shared_post_id'):
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
    
    def can_view(self, viewer_id: str, trust_score: int = 50) -> bool:
        """Check if a user can view this post based on visibility settings."""
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
    
    def __init__(self, db_manager: DatabaseManager, api_key_manager: ApiKeyManager,
                 data_encryptor: Any = None):
        """Initialize feed manager with database and API key manager."""
        self.db = db_manager
        self.api_key_manager = api_key_manager
        self.data_encryptor = data_encryptor
        self.max_content_length = 4096  # 4KB for text posts
        self.supported_media_types = [
            'image/jpeg', 'image/png', 'image/gif',
            'video/mp4', 'video/webm',
            'audio/mp3', 'audio/wav', 'audio/ogg'
        ]

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
        if ttl_mode in ('none', 'no_expiry', 'immortal'):
            return None

        if expires_at:
            return self._parse_datetime(expires_at)

        base = base_time or datetime.now(timezone.utc)

        if ttl_seconds is not None:
            try:
                ttl_val = int(ttl_seconds)
            except (TypeError, ValueError):
                ttl_val = None
            if ttl_val is not None:
                if ttl_val <= 0:
                    return None
                return base + timedelta(seconds=ttl_val)

        if apply_default:
            return base + timedelta(seconds=self.DEFAULT_TTL_SECONDS)

        return None
    
    @log_performance('feed')
    def create_post(self, author_id: str, content: str,
                   post_type: PostType = PostType.TEXT,
                   visibility: PostVisibility = PostVisibility.NETWORK,
                   metadata: Optional[Dict[str, Any]] = None,
                   permissions: Optional[List[str]] = None,
                   source_type: str = 'human',
                   source_agent_id: Optional[str] = None,
                   source_url: Optional[str] = None,
                   tags: Optional[List[str]] = None,
                   expires_at: Optional[Any] = None,
                   ttl_seconds: Optional[int] = None,
                   ttl_mode: Optional[str] = None) -> Optional[Post]:
        """Create a new social media post."""
        logger.info(f"Creating post by author_id={author_id}, type={post_type.value}, visibility={visibility.value}, source={source_type}")
        logger.debug(f"Content length: {len(content)}, permissions: {permissions}")
        
        try:
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

        return Post(
            id=row['id'],
            author_id=row['author_id'],
            content=self._decrypt_content(row['content']),
            post_type=PostType(row['content_type']),
            visibility=PostVisibility(row['visibility']),
            created_at=datetime.fromisoformat(row['created_at']),
            expires_at=expires_dt,
            metadata=json.loads(row['metadata']) if row['metadata'] else None,
            permissions=permissions,
            likes=row['likes'] if 'likes' in row.keys() else 0,
            comments=row['comments'] if 'comments' in row.keys() else 0,
            shares=row['shares'] if 'shares' in row.keys() else 0,
            source_type=row['source_type'] if 'source_type' in row.keys() else 'human',
            source_agent_id=row['source_agent_id'] if 'source_agent_id' in row.keys() else None,
            source_url=row['source_url'] if 'source_url' in row.keys() else None,
            tags=row['tags'] if 'tags' in row.keys() else None,
        )
    
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
                return [self._row_to_post(row, conn) for row in cursor.fetchall()]
                
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
                        (p.visibility = 'custom' AND pp.user_id = ?) OR
                        p.author_id = ?
                    )
                      AND p.created_at > ?
                      AND (p.expires_at IS NULL OR p.expires_at > CURRENT_TIMESTAMP)
                    ORDER BY p.created_at DESC
                    LIMIT ?
                """
                rows = conn.execute(query, (user_id, user_id, since_db, limit_val)).fetchall()
                return [self._row_to_post(row, conn) for row in rows]
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
                score = algo.score_post(post, user_id)
                if score >= 0:
                    scored.append((score, post))
            except Exception:
                continue

        # Sort by score descending, return top N
        scored.sort(key=lambda x: x[0], reverse=True)
        return [post for _, post in scored[:limit]]
    
    def get_user_posts(self, author_id: str, limit: int = 50) -> List[Post]:
        """Get all posts by a specific user."""
        try:
            with self.db.get_connection() as conn:
                cursor = conn.execute("""
                    SELECT p.*, u.username as author_username
                    FROM feed_posts p
                    LEFT JOIN users u ON p.author_id = u.id
                    WHERE p.author_id = ?
                      AND (p.expires_at IS NULL OR p.expires_at > CURRENT_TIMESTAMP)
                    ORDER BY p.created_at DESC
                    LIMIT ?
                """, (author_id, limit))
                
                return [self._row_to_post(row, conn) for row in cursor.fetchall()]
                
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
                final_metadata = metadata if metadata is not None else (json.loads(row['metadata']) if row['metadata'] else None)
                
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
    
    def share_post(self, post_id: str, user_id: str, comment: str = '') -> Optional[Post]:
        """Share (repost) an existing post. Creates a new post that references
        the original and increments the original's share count."""
        try:
            original = self.get_post(post_id)
            if not original:
                logger.warning(f"Cannot share: post {post_id} not found")
                return None
            
            # Build share metadata
            share_meta: Dict[str, Any] = {
                'shared_post_id': post_id,
                'original_author_id': original.author_id,
                'original_content': original.content[:200],
                'original_type': original.post_type.value,
            }
            if original.metadata:
                share_meta['original_metadata'] = original.metadata
            
            content = comment if comment else f"Shared a post"
            
            shared = self.create_post(
                author_id=user_id,
                content=content,
                post_type=original.post_type,
                visibility=PostVisibility.NETWORK,
                metadata=share_meta,
            )
            
            if shared:
                # Increment original's share count
                with self.db.get_connection() as conn:
                    conn.execute(
                        "UPDATE feed_posts SET shares = shares + 1 WHERE id = ?",
                        (post_id,))
                    conn.commit()
                logger.info(f"User {user_id} shared post {post_id} as {shared.id}")
            
            return cast(Optional[Post], shared)
            
        except Exception as e:
            logger.error(f"Failed to share post: {e}")
            return None

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
        """Get feed statistics for a user."""
        try:
            with self.db.get_connection() as conn:
                cursor = conn.execute("""
                    SELECT 
                        COUNT(*) as total_posts,
                        SUM(CASE WHEN author_id = ? THEN 1 ELSE 0 END) as user_posts,
                        COUNT(DISTINCT author_id) as unique_authors
                    FROM feed_posts
                    WHERE (visibility = 'public' OR visibility = 'network' OR author_id = ?)
                      AND (expires_at IS NULL OR expires_at > CURRENT_TIMESTAMP)
                """, (user_id, user_id))
                
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

    def get_available_tags(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Get popular tags across all posts for the tag picker UI."""
        try:
            with self.db.get_connection() as conn:
                cursor = conn.execute(
                    "SELECT tags FROM feed_posts WHERE tags IS NOT NULL AND tags != ''"
                    " AND (expires_at IS NULL OR expires_at > CURRENT_TIMESTAMP)"
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
