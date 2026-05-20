"""
Social interaction system for Canopy.
Handles likes, comments, and reactions on messages and posts.

Project: Canopy - Local Mesh Communication
License: Apache 2.0
"""

import logging
import re
import secrets
from pathlib import Path
from typing import Dict, List, Optional, Any, cast
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from enum import Enum

from .database import DatabaseManager
from .events import EVENT_FEED_POST_UPDATED
from .logging_config import log_performance, LogOperation

logger = logging.getLogger('canopy.interactions')

class InteractionType(Enum):
    """Types of interactions."""
    LIKE = "like"
    LOVE = "love"
    LAUGH = "laugh"
    WOW = "wow"
    SAD = "sad"
    ANGRY = "angry"
    CELEBRATE = "celebrate"
    ROCKET = "rocket"
    EYES = "eyes"
    CHECK = "check"
    HUNDRED = "hundred"
    IDK = "idk"
    PRAY = "pray"
    DISLIKE = "dislike"
    BEER = "beer"

CUSTOM_REACTION_PREFIX = "custom:"
_CUSTOM_REACTION_NAME_RE = re.compile(r"[^a-z0-9_-]+")
_MAX_CUSTOM_REACTION_NAME_LEN = 48

REACTION_OPTIONS: List[Dict[str, str]] = [
    {"type": InteractionType.LIKE.value, "emoji": "👍", "label": "Like"},
    {"type": InteractionType.LOVE.value, "emoji": "❤️", "label": "Love"},
    {"type": InteractionType.LAUGH.value, "emoji": "😂", "label": "Laugh"},
    {"type": InteractionType.WOW.value, "emoji": "😮", "label": "Wow"},
    {"type": InteractionType.SAD.value, "emoji": "😢", "label": "Sad"},
    {"type": InteractionType.ANGRY.value, "emoji": "😡", "label": "Angry"},
    {"type": InteractionType.CELEBRATE.value, "emoji": "🎉", "label": "Celebrate"},
    {"type": InteractionType.ROCKET.value, "emoji": "🚀", "label": "Ship it"},
    {"type": InteractionType.EYES.value, "emoji": "👀", "label": "Watching"},
    {"type": InteractionType.CHECK.value, "emoji": "✅", "label": "Done"},
    {"type": InteractionType.HUNDRED.value, "emoji": "💯", "label": "100%"},
    {"type": InteractionType.IDK.value, "emoji": "🤷", "label": "IDK"},
    {"type": InteractionType.PRAY.value, "emoji": "🙏", "label": "Thanks"},
    {"type": InteractionType.DISLIKE.value, "emoji": "👎", "label": "Dislike"},
    {"type": InteractionType.BEER.value, "emoji": "🍺", "label": "Beer"},
]


def _slugify_custom_reaction_name(value: Any) -> str:
    raw = str(value or '').strip().lower()
    raw = raw.strip(':')
    raw = _CUSTOM_REACTION_NAME_RE.sub('-', raw)
    raw = re.sub(r'-{2,}', '-', raw).strip('-_')
    return raw[:_MAX_CUSTOM_REACTION_NAME_LEN].strip('-_')


def normalize_reaction_key(value: Any) -> str:
    """Normalize an untrusted reaction value to a persisted reaction key.

    Standard reactions use the InteractionType values. Team-specific custom
    emojis are persisted as ``custom:<slug>`` so they can round-trip through
    UI, API, and P2P without expanding the enum for every meshspace.
    """
    if isinstance(value, InteractionType):
        return value.value
    raw = str(value or InteractionType.LIKE.value).strip().lower()
    if raw.startswith(':') and raw.endswith(':') and len(raw) > 2:
        raw = f"{CUSTOM_REACTION_PREFIX}{raw[1:-1]}"
    if raw.startswith(CUSTOM_REACTION_PREFIX):
        slug = _slugify_custom_reaction_name(raw[len(CUSTOM_REACTION_PREFIX):])
        return f"{CUSTOM_REACTION_PREFIX}{slug}" if slug else InteractionType.LIKE.value
    try:
        return InteractionType(raw).value
    except ValueError:
        return InteractionType.LIKE.value


def normalize_reaction_type(value: Any) -> InteractionType:
    """Normalize an untrusted reaction value to a supported reaction enum."""
    raw = normalize_reaction_key(value)
    try:
        return InteractionType(raw)
    except ValueError:
        return InteractionType.LIKE

@dataclass
class Like:
    """Represents a like on a message/post."""
    id: str
    message_id: str
    user_id: str
    created_at: datetime
    reaction_type: Any = InteractionType.LIKE

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        data = asdict(self)
        data['created_at'] = self.created_at.isoformat()
        data['reaction_type'] = normalize_reaction_key(self.reaction_type)
        return data

@dataclass
class Comment:
    """Represents a comment on a message/post."""
    id: str
    message_id: str
    user_id: str
    content: str
    created_at: datetime
    expires_at: Optional[datetime] = None
    parent_comment_id: Optional[str] = None  # For reply threads

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        data = asdict(self)
        data['created_at'] = self.created_at.isoformat()
        data['expires_at'] = self.expires_at.isoformat() if self.expires_at else None
        return data

class InteractionManager:
    """Manages social interactions like likes and comments."""
    
    def __init__(self, db: DatabaseManager):
        """Initialize the interaction manager.
        
        Args:
            db: Database manager instance
        """
        self.db = db
        self.workspace_events: Any = None
        logger.info("Initializing InteractionManager")
        
        self._ensure_tables()
        logger.info("InteractionManager initialized successfully")

    @staticmethod
    def _build_event_preview(content: str, fallback: str = 'Feed activity') -> str:
        preview = ' '.join(str(content or '').split()).strip()
        if not preview:
            return fallback
        if len(preview) > 160:
            return preview[:157].rstrip() + '...'
        return preview

    def _emit_feed_post_update_event(
        self,
        *,
        post_id: str,
        preview: str,
        update_reason: str,
        actor_user_id: Optional[str] = None,
    ) -> None:
        manager = self.workspace_events
        if not manager or not post_id:
            return
        try:
            with self.db.get_connection() as conn:
                row = conn.execute(
                    """
                    SELECT author_id, content_type, visibility
                    FROM feed_posts
                    WHERE id = ?
                    LIMIT 1
                    """,
                    (post_id,),
                ).fetchone()
                if not row:
                    return
                permissions = []
                if str(row['visibility'] or '').strip().lower() == 'custom':
                    perm_rows = conn.execute(
                        "SELECT user_id FROM post_permissions WHERE post_id = ?",
                        (post_id,),
                    ).fetchall()
                    permissions = [str(perm['user_id']) for perm in (perm_rows or []) if perm and perm['user_id']]
            now_dt = datetime.now(timezone.utc)
            manager.emit_event(
                event_type=EVENT_FEED_POST_UPDATED,
                actor_user_id=actor_user_id or (str(row['author_id'] or '').strip() or None),
                post_id=post_id,
                visibility_scope='feed',
                dedupe_key=f"{EVENT_FEED_POST_UPDATED}:{post_id}:{update_reason}:{now_dt.isoformat()}",
                created_at=now_dt,
                payload={
                    'author_id': str(row['author_id'] or '').strip() or None,
                    'post_type': str(row['content_type'] or 'text'),
                    'preview': self._build_event_preview(preview, 'Feed activity'),
                    'visibility': str(row['visibility'] or 'network').strip().lower(),
                    'permissions': permissions,
                    'update_reason': update_reason,
                },
            )
        except Exception as e:
            logger.warning(f"Failed to emit feed post update event for {post_id}: {e}")
    
    def _ensure_tables(self) -> None:
        """Ensure interaction-related database tables exist."""
        logger.info("Ensuring interaction database tables exist...")
        try:
            with self.db.get_connection() as conn:
                # First, check if tables exist and what columns they have
                cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name IN ('likes', 'comments')")
                existing_tables = [row[0] for row in cursor.fetchall()]
                
                if 'likes' not in existing_tables:
                    # Create new schema WITHOUT foreign key constraints
                    conn.execute("""
                        CREATE TABLE likes (
                            id TEXT PRIMARY KEY,
                            message_id TEXT NOT NULL,
                            user_id TEXT NOT NULL,
                            reaction_type TEXT DEFAULT 'like',
                            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                            UNIQUE(message_id, user_id)
                        )
                    """)
                else:
                    # Drop and recreate table without foreign key constraints
                    try:
                        # Save existing data
                        cursor = conn.execute("SELECT * FROM likes")
                        existing_likes = cursor.fetchall()
                        
                        # Drop old table
                        conn.execute("DROP TABLE likes")
                        
                        # Create new table without foreign key constraints
                        conn.execute("""
                            CREATE TABLE likes (
                                id TEXT PRIMARY KEY,
                                message_id TEXT NOT NULL,
                                user_id TEXT NOT NULL,
                                reaction_type TEXT DEFAULT 'like',
                                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                                UNIQUE(message_id, user_id)
                            )
                        """)
                        
                        # Restore data
                        for like in existing_likes:
                            conn.execute("""
                                INSERT OR IGNORE INTO likes (id, message_id, user_id, reaction_type, created_at)
                                VALUES (?, ?, ?, ?, ?)
                            """, (like['id'], like['message_id'], like['user_id'], 
                                 like['reaction_type'], like['created_at']))
                        
                        logger.info("Recreated likes table without foreign key constraints")
                    except Exception as e:
                        logger.warning(f"Could not recreate likes table: {e}")
                
                if 'comments' not in existing_tables:
                    # Create new schema WITHOUT foreign key constraints
                    conn.execute("""
                        CREATE TABLE comments (
                            id TEXT PRIMARY KEY,
                            message_id TEXT NOT NULL,
                            user_id TEXT NOT NULL,
                            content TEXT NOT NULL,
                            parent_comment_id TEXT,
                            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                            expires_at TIMESTAMP
                        )
                    """)
                else:
                    # Drop and recreate table without foreign key constraints
                    try:
                        # Save existing data
                        cursor = conn.execute("SELECT * FROM comments")
                        existing_comments = cursor.fetchall()
                        
                        # Drop old table
                        conn.execute("DROP TABLE comments")
                        
                        # Create new table without foreign key constraints
                        conn.execute("""
                            CREATE TABLE comments (
                                id TEXT PRIMARY KEY,
                                message_id TEXT NOT NULL,
                                user_id TEXT NOT NULL,
                                content TEXT NOT NULL,
                                parent_comment_id TEXT,
                                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                                expires_at TIMESTAMP
                            )
                        """)
                        
                        # Restore data
                        for comment in existing_comments:
                            try:
                                expires_at = comment['expires_at']
                            except Exception:
                                expires_at = None
                            conn.execute("""
                                INSERT OR IGNORE INTO comments (id, message_id, user_id, content, parent_comment_id, created_at, expires_at)
                                VALUES (?, ?, ?, ?, ?, ?, ?)
                            """, (comment['id'], comment['message_id'], comment['user_id'], 
                                 comment['content'], comment['parent_comment_id'], comment['created_at'], expires_at))
                        
                        logger.info("Recreated comments table without foreign key constraints")
                    except Exception as e:
                        logger.warning(f"Could not recreate comments table: {e}")
                
                # Create indexes for simple schema
                conn.execute("CREATE INDEX IF NOT EXISTS idx_likes_message ON likes(message_id)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_likes_user ON likes(user_id)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_comments_message ON comments(message_id)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_comments_user ON comments(user_id)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_comments_parent ON comments(parent_comment_id)")

                # Add expires_at column if missing (older DBs)
                try:
                    conn.execute("SELECT expires_at FROM comments LIMIT 1")
                except Exception:
                    try:
                        conn.execute("ALTER TABLE comments ADD COLUMN expires_at TIMESTAMP")
                    except Exception:
                        pass

                # Backfill expires_at from parent post if possible
                try:
                    conn.execute("""
                        UPDATE comments
                        SET expires_at = (
                            SELECT expires_at FROM feed_posts WHERE feed_posts.id = comments.message_id
                        )
                        WHERE expires_at IS NULL
                    """)
                except Exception:
                    pass

                # Create expires_at index after column is ensured
                try:
                    conn.execute("CREATE INDEX IF NOT EXISTS idx_comments_expires_at ON comments(expires_at)")
                except Exception:
                    pass

                # Poll votes (for feed/channel polls)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS poll_votes (
                        id TEXT PRIMARY KEY,
                        poll_id TEXT NOT NULL,
                        item_type TEXT NOT NULL,
                        user_id TEXT NOT NULL,
                        option_index INTEGER NOT NULL,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE(poll_id, item_type, user_id)
                    )
                """)
                conn.execute("CREATE INDEX IF NOT EXISTS idx_poll_votes_poll ON poll_votes(poll_id)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_poll_votes_user ON poll_votes(user_id)")

                # Poll closures (to avoid duplicate close notifications per node)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS poll_closures (
                        poll_id TEXT NOT NULL,
                        item_type TEXT NOT NULL,
                        closed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        summary TEXT,
                        PRIMARY KEY (poll_id, item_type)
                    )
                """)
                conn.execute("CREATE INDEX IF NOT EXISTS idx_poll_closures_closed_at ON poll_closures(closed_at)")
                
                conn.commit()
                logger.info("Interaction database tables ensured successfully")
        except Exception as e:
            logger.error(f"Failed to ensure interaction tables: {e}", exc_info=True)
            logger.info("Continuing with existing table structure...")
    
    @log_performance('interactions')
    def toggle_like(self, message_id: str, user_id: str,
                   reaction_type: Any = InteractionType.LIKE) -> bool:
        """Toggle a like/reaction on a message.
        
        Args:
            message_id: ID of the message to like
            user_id: ID of the user doing the liking
            reaction_type: Type of reaction
            
        Returns:
            True if like was added, False if like was removed
        """
        reaction_key = normalize_reaction_key(reaction_type)
        logger.info(f"Toggling {reaction_key} on message {message_id} by user {user_id}")
        
        try:
            with self.db.get_connection() as conn:
                # Check if user already liked this message
                cursor = conn.execute("""
                    SELECT id, reaction_type FROM likes
                    WHERE message_id = ? AND user_id = ?
                """, (message_id, user_id))
                
                existing_like = cursor.fetchone()
                
                if existing_like:
                    if str(existing_like['reaction_type'] or InteractionType.LIKE.value) == reaction_key:
                        conn.execute("DELETE FROM likes WHERE id = ?", (existing_like['id'],))
                        conn.commit()
                        logger.info(f"Removed {reaction_key} from message {message_id}")
                        return False
                    conn.execute(
                        "UPDATE likes SET reaction_type = ?, created_at = CURRENT_TIMESTAMP WHERE id = ?",
                        (reaction_key, existing_like['id']),
                    )
                    conn.commit()
                    logger.info(f"Changed reaction on message {message_id} to {reaction_key}")
                    return True
                else:
                    # Add new like - use INSERT OR IGNORE to handle foreign key constraints gracefully
                    like_id = f"L{secrets.token_hex(8)}"
                    conn.execute("""
                        INSERT OR IGNORE INTO likes (id, message_id, user_id, reaction_type)
                        VALUES (?, ?, ?, ?)
                    """, (like_id, message_id, user_id, reaction_key))
                    
                    # Check if the insert was successful
                    cursor = conn.execute("SELECT changes()")
                    changes = cursor.fetchone()[0]
                    
                    if changes > 0:
                        conn.commit()
                        logger.info(f"Added {reaction_key} to message {message_id}")
                        return True
                    else:
                        logger.warning(f"Failed to like message {message_id} - foreign key constraint or duplicate")
                        return False
                    
        except Exception as e:
            logger.error(f"Failed to toggle like: {e}", exc_info=True)
            return False
    
    @log_performance('interactions')
    def add_comment(self, message_id: str, user_id: str, content: str,
                   parent_comment_id: Optional[str] = None) -> Optional[Comment]:
        """Add a comment to a message.
        
        Args:
            message_id: ID of the message to comment on
            user_id: ID of the user commenting
            content: Comment content
            parent_comment_id: ID of parent comment for replies
            
        Returns:
            Comment object if successful, None otherwise
        """
        logger.info(f"Adding comment to message {message_id} by user {user_id}")
        
        try:
            comment_id = f"C{secrets.token_hex(10)}"
            created_at = datetime.now(timezone.utc)
            
            comment = Comment(
                id=comment_id,
                message_id=message_id,
                user_id=user_id,
                content=content.strip(),
                created_at=created_at,
                parent_comment_id=parent_comment_id
            )
            
            with self.db.get_connection() as conn:
                # Use INSERT OR IGNORE to handle foreign key constraints gracefully
                conn.execute("""
                    INSERT OR IGNORE INTO comments (id, message_id, user_id, content, parent_comment_id)
                    VALUES (?, ?, ?, ?, ?)
                """, (
                    comment.id, comment.message_id, comment.user_id,
                    comment.content, comment.parent_comment_id
                ))
                
                # Check if the insert was successful
                cursor = conn.execute("SELECT changes()")
                changes = cursor.fetchone()[0]
                
                if changes > 0:
                    conn.commit()
                    logger.info(f"Comment added successfully: {comment_id}")
                    return comment
                else:
                    logger.warning(f"Failed to add comment to message {message_id} - foreign key constraint")
                    return None
            
        except Exception as e:
            logger.error(f"Failed to add comment: {e}", exc_info=True)
            return None
    
    @log_performance('interactions')
    def get_message_interactions(self, message_id: str) -> Dict[str, Any]:
        """Get all interactions for a message.
        
        Args:
            message_id: ID of the message
            
        Returns:
            Dictionary with likes and comments data
        """
        logger.debug(f"Getting interactions for message {message_id}")
        
        try:
            with self.db.get_connection() as conn:
                # Get like counts by reaction type
                cursor = conn.execute("""
                    SELECT reaction_type, COUNT(*) as count
                    FROM likes
                    WHERE message_id = ?
                    GROUP BY reaction_type
                """, (message_id,))
                
                like_counts = {}
                total_likes = 0
                for row in cursor.fetchall():
                    like_counts[row['reaction_type']] = row['count']
                    total_likes += row['count']
                
                # Get recent likers
                cursor = conn.execute("""
                    SELECT user_id, reaction_type, created_at
                    FROM likes
                    WHERE message_id = ?
                    ORDER BY created_at DESC
                    LIMIT 10
                """, (message_id,))
                
                recent_likes = []
                for row in cursor.fetchall():
                    recent_likes.append({
                        'user_id': row['user_id'],
                        'reaction_type': row['reaction_type'],
                        'created_at': row['created_at']
                    })
                
                # Get comment count
                cursor = conn.execute("""
                    SELECT COUNT(*) as count
                    FROM comments 
                    WHERE message_id = ?
                      AND (expires_at IS NULL OR expires_at > CURRENT_TIMESTAMP)
                """, (message_id,))
                
                comment_count = cursor.fetchone()['count']
                
                return {
                    'message_id': message_id,
                    'total_likes': total_likes,
                    'like_counts': like_counts,
                    'recent_likes': recent_likes,
                    'comment_count': comment_count
                }
                
        except Exception as e:
            logger.error(f"Failed to get message interactions: {e}", exc_info=True)
            return {
                'message_id': message_id,
                'total_likes': 0,
                'like_counts': {},
                'recent_likes': [],
                'comment_count': 0
            }

    def get_message_interactions_map(self, message_ids: list[str]) -> Dict[str, Dict[str, Any]]:
        """Batch interaction counts for many messages/posts."""
        clean_ids = [str(item_id).strip() for item_id in (message_ids or []) if str(item_id).strip()]
        if not clean_ids:
            return {}
        result = {
            item_id: {
                'message_id': item_id,
                'total_likes': 0,
                'like_counts': {},
                'recent_likes': [],
                'comment_count': 0,
            }
            for item_id in clean_ids
        }
        try:
            with self.db.get_connection() as conn:
                chunk_size = 250
                for start in range(0, len(clean_ids), chunk_size):
                    chunk = clean_ids[start:start + chunk_size]
                    placeholders = ','.join('?' for _ in chunk)
                    like_rows = conn.execute(f"""
                        SELECT message_id, reaction_type, COUNT(*) as count
                        FROM likes
                        WHERE message_id IN ({placeholders})
                        GROUP BY message_id, reaction_type
                    """, chunk).fetchall()
                    for row in like_rows:
                        item_id = str(row['message_id'])
                        reaction_type = str(row['reaction_type'] or InteractionType.LIKE.value)
                        count = int(row['count'] or 0)
                        bucket = result.setdefault(item_id, {
                            'message_id': item_id,
                            'total_likes': 0,
                            'like_counts': {},
                            'recent_likes': [],
                            'comment_count': 0,
                        })
                        bucket['like_counts'][reaction_type] = count
                        bucket['total_likes'] += count

                    comment_rows = conn.execute(f"""
                        SELECT message_id, COUNT(*) as count
                        FROM comments
                        WHERE message_id IN ({placeholders})
                          AND (expires_at IS NULL OR expires_at > CURRENT_TIMESTAMP)
                        GROUP BY message_id
                    """, chunk).fetchall()
                    for row in comment_rows:
                        item_id = str(row['message_id'])
                        if item_id in result:
                            result[item_id]['comment_count'] = int(row['count'] or 0)
            return result
        except Exception as e:
            logger.error(f"Failed to batch fetch message interactions: {e}", exc_info=True)
            return result

    def get_reaction_details(self, item_id: str, limit: int = 80) -> Dict[str, Any]:
        """Return reaction counts plus the users behind each reaction.

        The ``likes`` table is intentionally shared by feed posts, channel
        messages, and DMs. Callers are responsible for source access checks
        before exposing this roster.
        """
        clean_item_id = str(item_id or '').strip()
        if not clean_item_id:
            return {
                'item_id': '',
                'total': 0,
                'counts': {},
                'groups': [],
                'recent_reactions': [],
                'truncated': False,
            }
        try:
            bounded_limit = max(1, min(int(limit or 80), 200))
        except Exception:
            bounded_limit = 80

        try:
            with self.db.get_connection() as conn:
                count_rows = conn.execute("""
                    SELECT reaction_type, COUNT(*) AS count
                    FROM likes
                    WHERE message_id = ?
                    GROUP BY reaction_type
                """, (clean_item_id,)).fetchall()
                counts = {
                    str(row['reaction_type'] or InteractionType.LIKE.value): int(row['count'] or 0)
                    for row in count_rows
                }
                total = sum(counts.values())

                try:
                    user_columns = {
                        str(row['name'])
                        for row in conn.execute("PRAGMA table_info(users)").fetchall()
                    }
                except Exception:
                    user_columns = set()
                username_expr = "u.username" if "username" in user_columns else "''"
                display_name_expr = "u.display_name" if "display_name" in user_columns else "''"
                avatar_expr = "u.avatar_file_id" if "avatar_file_id" in user_columns else "''"
                account_type_expr = "u.account_type" if "account_type" in user_columns else "'human'"
                origin_peer_expr = "u.origin_peer" if "origin_peer" in user_columns else "''"
                rows = conn.execute(f"""
                    SELECT
                        l.user_id,
                        l.reaction_type,
                        l.created_at,
                        {username_expr} AS username,
                        {display_name_expr} AS display_name,
                        {avatar_expr} AS avatar_file_id,
                        {account_type_expr} AS account_type,
                        {origin_peer_expr} AS origin_peer
                    FROM likes l
                    LEFT JOIN users u ON u.id = l.user_id
                    WHERE l.message_id = ?
                    ORDER BY l.created_at DESC
                    LIMIT ?
                """, (clean_item_id, bounded_limit)).fetchall()

            groups_by_type: Dict[str, Dict[str, Any]] = {
                reaction_type: {
                    'reaction_type': reaction_type,
                    'count': count,
                    'reactors': [],
                }
                for reaction_type, count in counts.items()
            }
            recent_reactions: List[Dict[str, Any]] = []
            for row in rows:
                user_id = str(row['user_id'] or '').strip()
                reaction_type = str(row['reaction_type'] or InteractionType.LIKE.value).strip() or InteractionType.LIKE.value
                username = str(row['username'] or '').strip()
                display_name = str(row['display_name'] or '').strip() or username or user_id
                avatar_file_id = str(row['avatar_file_id'] or '').strip()
                actor = {
                    'user_id': user_id,
                    'username': username or user_id,
                    'display_name': display_name,
                    'avatar_url': f"/files/{avatar_file_id}" if avatar_file_id else '',
                    'account_type': str(row['account_type'] or 'human').strip().lower() or 'human',
                    'origin_peer': str(row['origin_peer'] or '').strip(),
                    'reaction_type': reaction_type,
                    'created_at': str(row['created_at'] or '').strip(),
                }
                groups_by_type.setdefault(reaction_type, {
                    'reaction_type': reaction_type,
                    'count': counts.get(reaction_type, 0),
                    'reactors': [],
                })['reactors'].append(actor)
                recent_reactions.append(actor)

            groups = sorted(
                groups_by_type.values(),
                key=lambda group: (-int(group.get('count') or 0), str(group.get('reaction_type') or '')),
            )
            return {
                'item_id': clean_item_id,
                'total': total,
                'counts': counts,
                'groups': groups,
                'recent_reactions': recent_reactions,
                'truncated': total > len(recent_reactions),
                'limit': bounded_limit,
            }
        except Exception as e:
            logger.error(f"Failed to fetch reaction details for {clean_item_id}: {e}", exc_info=True)
            return {
                'item_id': clean_item_id,
                'total': 0,
                'counts': {},
                'groups': [],
                'recent_reactions': [],
                'truncated': False,
                'limit': bounded_limit,
            }
    
    @log_performance('interactions')
    def get_message_comments(self, message_id: str, limit: int = 50) -> List[Comment]:
        """Get comments for a message.
        
        Args:
            message_id: ID of the message
            limit: Maximum number of comments to return
            
        Returns:
            List of Comment objects
        """
        logger.debug(f"Getting comments for message {message_id}")
        
        try:
            with self.db.get_connection() as conn:
                cursor = conn.execute("""
                    SELECT id, message_id, user_id, content, parent_comment_id, created_at, expires_at
                    FROM comments 
                    WHERE message_id = ?
                      AND (expires_at IS NULL OR expires_at > CURRENT_TIMESTAMP)
                    ORDER BY created_at ASC
                    LIMIT ?
                """, (message_id, limit))
                
                comments = []
                for row in cursor.fetchall():
                    comments.append(Comment(
                        id=row['id'],
                        message_id=row['message_id'],
                        user_id=row['user_id'],
                        content=row['content'],
                        created_at=datetime.fromisoformat(row['created_at']),
                        expires_at=datetime.fromisoformat(row['expires_at']) if row['expires_at'] else None,
                        parent_comment_id=row['parent_comment_id']
                    ))
                
                logger.debug(f"Found {len(comments)} comments for message {message_id}")
                return comments
                
        except Exception as e:
            logger.error(f"Failed to get comments: {e}", exc_info=True)
            return []
    
    def delete_comment(self, comment_id: str, user_id: str) -> bool:
        """Delete a comment (only by owner).
        
        Args:
            comment_id: ID of the comment to delete
            user_id: ID of the user requesting deletion
            
        Returns:
            True if deleted successfully, False otherwise
        """
        logger.info(f"Deleting comment {comment_id} by user {user_id}")
        
        try:
            with self.db.get_connection() as conn:
                # Check if user owns the comment
                cursor = conn.execute("""
                    SELECT user_id FROM comments WHERE id = ?
                """, (comment_id,))
                
                comment = cursor.fetchone()
                if not comment:
                    logger.warning(f"Comment not found: {comment_id}")
                    return False
                
                if comment['user_id'] != user_id:
                    logger.warning(f"User {user_id} attempted to delete comment {comment_id} owned by {comment['user_id']}")
                    return False
                
                # Delete the comment
                conn.execute("DELETE FROM comments WHERE id = ?", (comment_id,))
                conn.commit()
                
                logger.info(f"Comment deleted successfully: {comment_id}")
                return True
                
        except Exception as e:
            logger.error(f"Failed to delete comment: {e}", exc_info=True)
            return False
    
    def get_user_has_liked(self, message_id: str, user_id: str) -> Optional[str]:
        """Check if user has liked a message or post.
        
        Args:
            message_id: ID of the message or post
            user_id: ID of the user
            
        Returns:
            Reaction type if user has liked, None otherwise
        """
        try:
            with self.db.get_connection() as conn:
                cursor = conn.execute("""
                    SELECT reaction_type FROM likes
                    WHERE message_id = ? AND user_id = ?
                """, (message_id, user_id))
                
                result = cursor.fetchone()
                return result['reaction_type'] if result else None
                
        except Exception as e:
            logger.error(f"Failed to check user like: {e}", exc_info=True)
            return None

    def get_user_liked_ids(self, item_ids: list, user_id: str) -> set:
        """Check which items (messages or posts) a user has liked.
        
        Args:
            item_ids: List of message/post IDs to check
            user_id: ID of the user
            
        Returns:
            Set of item IDs the user has liked
        """
        if not item_ids:
            return set()
        try:
            with self.db.get_connection() as conn:
                placeholders = ','.join('?' for _ in item_ids)
                cursor = conn.execute(f"""
                    SELECT message_id FROM likes
                    WHERE message_id IN ({placeholders}) AND user_id = ?
                """, (*item_ids, user_id))
                return {row['message_id'] for row in cursor.fetchall()}
        except Exception as e:
            logger.error(f"Failed to batch check user likes: {e}", exc_info=True)
            return set()

    def get_user_reaction_map(self, item_ids: list, user_id: str) -> Dict[str, str]:
        """Return the current user's reaction type for each requested item."""
        if not item_ids:
            return {}
        try:
            with self.db.get_connection() as conn:
                placeholders = ','.join('?' for _ in item_ids)
                cursor = conn.execute(f"""
                    SELECT message_id, reaction_type FROM likes
                    WHERE message_id IN ({placeholders}) AND user_id = ?
                """, (*item_ids, user_id))
                return {
                    str(row['message_id']): str(row['reaction_type'] or InteractionType.LIKE.value)
                    for row in cursor.fetchall()
                }
        except Exception as e:
            logger.error(f"Failed to batch fetch user reactions: {e}", exc_info=True)
            return {}
    
    # POST-specific interaction methods
    @log_performance('interactions')
    def toggle_post_like(self, post_id: str, user_id: str,
                        reaction_type: Any = InteractionType.LIKE) -> bool:
        """Toggle a like/reaction on a post.
        
        Args:
            post_id: ID of the post to like/unlike
            user_id: ID of user performing the action
            reaction_type: Type of reaction (default: LIKE)
            
        Returns:
            True if now liked, False if unliked
        """
        reaction_key = normalize_reaction_key(reaction_type)
        try:
            with self.db.get_connection() as conn:
                # Check if user already liked this post
                cursor = conn.execute("""
                    SELECT id, reaction_type FROM likes
                    WHERE message_id = ? AND user_id = ?
                """, (post_id, user_id))
                
                existing_like = cursor.fetchone()
                
                if existing_like:
                    if str(existing_like['reaction_type'] or InteractionType.LIKE.value) != reaction_key:
                        conn.execute(
                            "UPDATE likes SET reaction_type = ?, created_at = CURRENT_TIMESTAMP WHERE id = ?",
                            (reaction_key, existing_like['id']),
                        )
                        logger.info(f"User {user_id} changed reaction on post {post_id} to {reaction_key}")
                        conn.commit()
                        return True

                    # Unlike the post when the same reaction is selected again.
                    conn.execute("DELETE FROM likes WHERE id = ?", (existing_like['id'],))
                    conn.execute(
                        "UPDATE feed_posts SET likes = MAX(0, likes - 1) WHERE id = ?",
                        (post_id,))
                    logger.info(f"User {user_id} unliked post {post_id}")
                    conn.commit()
                    return False
                else:
                    # Like the post
                    like_id = f"L{secrets.token_hex(8)}"
                    conn.execute("""
                        INSERT OR IGNORE INTO likes (id, message_id, user_id, reaction_type)
                        VALUES (?, ?, ?, ?)
                    """, (like_id, post_id, user_id, reaction_key))
                    conn.execute(
                        "UPDATE feed_posts SET likes = likes + 1 WHERE id = ?",
                        (post_id,))
                    
                    logger.info(f"User {user_id} liked post {post_id} with {reaction_key}")
                    conn.commit()
                    return True
                    
        except Exception as e:
            logger.error(f"Failed to toggle post like: {e}", exc_info=True)
            return False

    @log_performance('interactions')
    def add_post_comment(self, post_id: str, user_id: str, content: str, 
                        parent_comment_id: Optional[str] = None) -> Optional[Comment]:
        """Add a comment to a post.
        
        Args:
            post_id: ID of the post to comment on
            user_id: ID of user adding comment
            content: Comment content
            parent_comment_id: ID of parent comment if this is a reply
            
        Returns:
            Comment object if successful, None otherwise
        """
        try:
            if not content.strip():
                logger.warning("Attempted to add empty comment")
                return None
                
            comment_id = secrets.token_hex(16)
            timestamp = datetime.now(timezone.utc)
                
            with self.db.get_connection() as conn:
                # Inherit expiry from parent post (if any)
                expires_at = None
                try:
                    row = conn.execute(
                        "SELECT expires_at FROM feed_posts WHERE id = ?",
                        (post_id,)
                    ).fetchone()
                    expires_at = row['expires_at'] if row else None
                except Exception:
                    expires_at = None

                conn.execute("""
                    INSERT INTO comments (id, message_id, user_id, content, parent_comment_id, created_at, expires_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (comment_id, post_id, user_id, content, parent_comment_id, timestamp.isoformat(), expires_at))
                # Keep feed_posts.comments counter in sync and resurface post
                # (6-hour cap: only update last_activity_at if it's been >6h since last resurface)
                conn.execute(
                    "UPDATE feed_posts SET comments = comments + 1 WHERE id = ?",
                    (post_id,))
                conn.execute("""
                    UPDATE feed_posts SET last_activity_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                      AND (last_activity_at IS NULL
                           OR last_activity_at < datetime('now', '-6 hours'))
                """, (post_id,))
                
                conn.commit()
                
                comment = Comment(
                    id=comment_id,
                    message_id=post_id,  # For compatibility
                    user_id=user_id,
                    content=content,
                    parent_comment_id=parent_comment_id,
                    expires_at=datetime.fromisoformat(expires_at.replace('Z', '+00:00')) if expires_at else None,
                    created_at=timestamp
                )
                
                logger.info(f"Added comment {comment_id} to post {post_id} by user {user_id}")
                self._emit_feed_post_update_event(
                    post_id=post_id,
                    preview=content,
                    update_reason='comment',
                    actor_user_id=user_id,
                )
                return comment
                
        except Exception as e:
            logger.error(f"Failed to add post comment: {e}", exc_info=True)
            return None

    @log_performance('interactions')
    def get_post_interactions(self, post_id: str) -> Dict[str, Any]:
        """Get interaction statistics for a post.
        
        Args:
            post_id: ID of the post
            
        Returns:
            Dictionary with interaction counts and details
        """
        try:
            with self.db.get_connection() as conn:
                # Get like counts
                cursor = conn.execute("""
                    SELECT reaction_type, COUNT(*) as count 
                    FROM likes
                    WHERE message_id = ?
                    GROUP BY reaction_type
                """, (post_id,))
                
                like_counts = {row['reaction_type']: row['count'] for row in cursor.fetchall()}
                total_likes = sum(like_counts.values())
                
                # Get comment count
                cursor = conn.execute("""
                    SELECT COUNT(*) as count 
                    FROM comments 
                    WHERE message_id = ?
                      AND (expires_at IS NULL OR expires_at > CURRENT_TIMESTAMP)
                """, (post_id,))
                
                comment_count = cursor.fetchone()['count']
                
                return {
                    'total_likes': total_likes,
                    'like_counts': like_counts,
                    'comment_count': comment_count,
                    'post_id': post_id
                }
                
        except Exception as e:
            logger.error(f"Failed to get post interactions: {e}", exc_info=True)
            return {
                'total_likes': 0,
                'like_counts': {},
                'comment_count': 0,
                'post_id': post_id
            }

    @log_performance('interactions')
    def get_post_comments(self, post_id: str) -> List[Comment]:
        """Get all comments for a post.
        
        Args:
            post_id: ID of the post
            
        Returns:
            List of Comment objects
        """
        try:
            with self.db.get_connection() as conn:
                cursor = conn.execute("""
                    SELECT * FROM comments 
                    WHERE message_id = ?
                      AND (expires_at IS NULL OR expires_at > CURRENT_TIMESTAMP)
                    ORDER BY created_at ASC
                """, (post_id,))
                
                comments = []
                for row in cursor.fetchall():
                    expires_at = None
                    try:
                        expires_at = datetime.fromisoformat(row['expires_at']) if row['expires_at'] else None
                    except Exception:
                        expires_at = None
                    comment = Comment(
                        id=row['id'],
                        message_id=row['message_id'],
                        user_id=row['user_id'],
                        content=row['content'],
                        parent_comment_id=row['parent_comment_id'],
                        created_at=datetime.fromisoformat(row['created_at']),
                        expires_at=expires_at,
                    )
                    comments.append(comment)
                
                logger.debug(f"Retrieved {len(comments)} comments for post {post_id}")
                return comments
                
        except Exception as e:
            logger.error(f"Failed to get post comments: {e}", exc_info=True)
            return []

    @log_performance('interactions')
    def record_poll_vote(self, poll_id: str, item_type: str, user_id: str,
                         option_index: int) -> Dict[str, Any]:
        """Record or update a user's poll vote. Returns vote state."""
        try:
            option_index = int(option_index)
        except (TypeError, ValueError):
            return {'changed': False, 'created': False, 'previous': None}

        try:
            with self.db.get_connection() as conn:
                existing = conn.execute(
                    "SELECT option_index FROM poll_votes WHERE poll_id = ? AND item_type = ? AND user_id = ?",
                    (poll_id, item_type, user_id)
                ).fetchone()

                if existing and existing['option_index'] == option_index:
                    return {'changed': False, 'created': False, 'previous': option_index}

                if existing:
                    conn.execute(
                        "UPDATE poll_votes SET option_index = ?, updated_at = CURRENT_TIMESTAMP "
                        "WHERE poll_id = ? AND item_type = ? AND user_id = ?",
                        (option_index, poll_id, item_type, user_id)
                    )
                    conn.commit()
                    return {'changed': True, 'created': False, 'previous': existing['option_index']}

                vote_id = f"PV{secrets.token_hex(10)}"
                conn.execute(
                    "INSERT OR IGNORE INTO poll_votes (id, poll_id, item_type, user_id, option_index) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (vote_id, poll_id, item_type, user_id, option_index)
                )
                conn.commit()
                return {'changed': True, 'created': True, 'previous': None}

        except Exception as e:
            logger.error(f"Failed to record poll vote: {e}", exc_info=True)
            return {'changed': False, 'created': False, 'previous': None}

    @log_performance('interactions')
    def get_poll_results(self, poll_id: str, item_type: str, option_count: int) -> Dict[str, Any]:
        """Return poll vote counts and totals."""
        counts = [0 for _ in range(max(0, int(option_count)))]
        try:
            with self.db.get_connection() as conn:
                cursor = conn.execute(
                    "SELECT option_index, COUNT(*) as count "
                    "FROM poll_votes WHERE poll_id = ? AND item_type = ? GROUP BY option_index",
                    (poll_id, item_type)
                )
                for row in cursor.fetchall():
                    idx = row['option_index']
                    if isinstance(idx, int) and 0 <= idx < len(counts):
                        counts[idx] = row['count']
        except Exception as e:
            logger.error(f"Failed to fetch poll results: {e}", exc_info=True)
        total = sum(counts)
        return {'counts': counts, 'total': total}

    @log_performance('interactions')
    def get_user_poll_vote(self, poll_id: str, item_type: str, user_id: str) -> Optional[int]:
        """Return the option index the user voted for (or None)."""
        try:
            with self.db.get_connection() as conn:
                row = conn.execute(
                    "SELECT option_index FROM poll_votes WHERE poll_id = ? AND item_type = ? AND user_id = ?",
                    (poll_id, item_type, user_id)
                ).fetchone()
                if row:
                    return cast(Optional[int], row['option_index'])
        except Exception:
            return None
        return None

    @log_performance('interactions')
    def mark_poll_closed(self, poll_id: str, item_type: str, summary: Optional[str] = None) -> bool:
        """Mark a poll as closed (per node). Returns True if newly marked."""
        try:
            with self.db.get_connection() as conn:
                cursor = conn.execute(
                    "INSERT OR IGNORE INTO poll_closures (poll_id, item_type, summary) VALUES (?, ?, ?)",
                    (poll_id, item_type, summary)
                )
                conn.commit()
                return cast(int, cursor.rowcount) > 0
        except Exception as e:
            logger.error(f"Failed to mark poll closed: {e}", exc_info=True)
            return False
