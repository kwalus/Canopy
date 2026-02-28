"""
Social interaction system for Canopy.
Handles likes, comments, and reactions on messages and posts.

Author: Konrad Walus (architecture, design, and direction)
Project: Canopy - Local Mesh Communication
License: Apache 2.0
Development: AI-assisted implementation (Claude, Codex, GitHub Copilot, Cursor IDE, Ollama)
"""

import logging
import secrets
from pathlib import Path
from typing import Dict, List, Optional, Any, cast
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from enum import Enum

from .database import DatabaseManager
from .logging_config import log_performance, LogOperation

logger = logging.getLogger('canopy.interactions')

class InteractionType(Enum):
    """Types of interactions."""
    LIKE = "like"
    DISLIKE = "dislike"
    LOVE = "love"
    LAUGH = "laugh"
    ANGRY = "angry"

@dataclass
class Like:
    """Represents a like on a message/post."""
    id: str
    message_id: str
    user_id: str
    created_at: datetime
    reaction_type: InteractionType = InteractionType.LIKE

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        data = asdict(self)
        data['created_at'] = self.created_at.isoformat()
        data['reaction_type'] = self.reaction_type.value
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
        logger.info("Initializing InteractionManager")
        
        self._ensure_tables()
        logger.info("InteractionManager initialized successfully")
    
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
                   reaction_type: InteractionType = InteractionType.LIKE) -> bool:
        """Toggle a like/reaction on a message.
        
        Args:
            message_id: ID of the message to like
            user_id: ID of the user doing the liking
            reaction_type: Type of reaction
            
        Returns:
            True if like was added, False if like was removed
        """
        logger.info(f"Toggling {reaction_type.value} on message {message_id} by user {user_id}")
        
        try:
            with self.db.get_connection() as conn:
                # Check if user already liked this message
                cursor = conn.execute("""
                    SELECT id FROM likes 
                    WHERE message_id = ? AND user_id = ?
                """, (message_id, user_id))
                
                existing_like = cursor.fetchone()
                
                if existing_like:
                    # Remove existing like
                    conn.execute("DELETE FROM likes WHERE id = ?", (existing_like['id'],))
                    conn.commit()
                    logger.info(f"Removed {reaction_type.value} from message {message_id}")
                    return False
                else:
                    # Add new like - use INSERT OR IGNORE to handle foreign key constraints gracefully
                    like_id = f"L{secrets.token_hex(8)}"
                    conn.execute("""
                        INSERT OR IGNORE INTO likes (id, message_id, user_id, reaction_type)
                        VALUES (?, ?, ?, ?)
                    """, (like_id, message_id, user_id, reaction_type.value))
                    
                    # Check if the insert was successful
                    cursor = conn.execute("SELECT changes()")
                    changes = cursor.fetchone()[0]
                    
                    if changes > 0:
                        conn.commit()
                        logger.info(f"Added {reaction_type.value} to message {message_id}")
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
    
    # POST-specific interaction methods
    @log_performance('interactions')
    def toggle_post_like(self, post_id: str, user_id: str, 
                        reaction_type: InteractionType = InteractionType.LIKE) -> bool:
        """Toggle a like/reaction on a post.
        
        Args:
            post_id: ID of the post to like/unlike
            user_id: ID of user performing the action
            reaction_type: Type of reaction (default: LIKE)
            
        Returns:
            True if now liked, False if unliked
        """
        try:
            with self.db.get_connection() as conn:
                # Check if user already liked this post
                cursor = conn.execute("""
                    SELECT id FROM likes 
                    WHERE message_id = ? AND user_id = ?
                """, (post_id, user_id))
                
                existing_like = cursor.fetchone()
                
                if existing_like:
                    # Unlike the post
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
                    """, (like_id, post_id, user_id, reaction_type.value))
                    conn.execute(
                        "UPDATE feed_posts SET likes = likes + 1 WHERE id = ?",
                        (post_id,))
                    
                    logger.info(f"User {user_id} liked post {post_id} with {reaction_type.value}")
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
