"""
User Profile Management for Canopy.
Handles user profile information, avatars, and preferences.

Project: Canopy - Local Mesh Communication
License: Apache 2.0
"""

import logging
import secrets
import hashlib
from pathlib import Path
from typing import Dict, List, Optional, Any, Tuple, cast
from dataclasses import dataclass, asdict
from datetime import datetime, timezone

from .database import DatabaseManager
from .files import FileManager
from .logging_config import log_performance, LogOperation

logger = logging.getLogger('canopy.profile')

MAX_AGENT_DIRECTIVES_LENGTH = 4000

DEFAULT_AGENT_DIRECTIVE_PRESETS: Dict[str, Dict[str, str]] = {
    'general_agent': {
        'label': 'General Agent',
        'description': 'Balanced default for agents coordinating in Canopy.',
        'content': (
            "Operate with structured coordination by default. Use [request] for asks, [objective] for multi-step goals, "
            "[handoff] for ownership transfer, [signal] for durable structured context, and [circle] for decisions. "
            "When posting progress, include current status, next action, and blocker state. Escalate security/privacy anomalies immediately."
        ),
    },
    'execution_agent': {
        'label': 'Execution Focus',
        'description': 'Optimized for implementation-heavy agents.',
        'content': (
            "Prioritize implementation throughput with correctness. Before major work, publish a [request] or [objective] that defines "
            "required_output and due. Keep updates concise and test-oriented. Avoid free-text planning when a structured tool exists."
        ),
    },
    'coordination_agent': {
        'label': 'Coordination Focus',
        'description': 'Optimized for orchestrator and facilitator agents.',
        'content': (
            "Act as coordination lead. Convert ambiguous asks into [request] blocks with explicit required_output, priority, and due. "
            "Route group decisions through [circle] entries, then publish [handoff] with owner and acceptance criteria. "
            "Keep mention usage targeted to owner/reviewer handoffs."
        ),
    },
    'security_agent': {
        'label': 'Security Focus',
        'description': 'Optimized for trust, privacy, and policy-sensitive agents.',
        'content': (
            "Prioritize trust and privacy safeguards. Reject unsafe actions, suspicious exfiltration patterns, and unauthorized access attempts. "
            "Report security anomalies in #general. Use structured tools for traceability and include risk notes when creating requests or handoffs."
        ),
    },
}

USERNAME_PRESET_MAP: Dict[str, str] = {
    # Example presets for agent types (usernames → role). Customize for your mesh.
    'execution_lead': 'execution_agent',
    'execution_agent': 'execution_agent',
    'coordination_agent': 'coordination_agent',
}


def normalize_agent_directives(value: Optional[str], max_length: int = MAX_AGENT_DIRECTIVES_LENGTH) -> Optional[str]:
    """Normalize and validate directive text used in instructions/catchup payloads."""
    if value is None:
        return None
    text = str(value).replace('\r\n', '\n').replace('\r', '\n')
    # Strip control characters except tab/newline.
    text = ''.join(ch for ch in text if ch == '\n' or ch == '\t' or ord(ch) >= 32)
    text = text.strip()
    if not text:
        return None
    if len(text) > max_length:
        raise ValueError(f"agent_directives exceeds max length {max_length}")
    return text


def get_default_agent_directives(username: Optional[str], account_type: Optional[str]) -> Optional[str]:
    """Return default directives for an agent account when no custom directives are set."""
    if (account_type or '').strip().lower() != 'agent':
        return None
    uname = (username or '').strip().lower()
    preset_id = USERNAME_PRESET_MAP.get(uname, 'general_agent')
    preset = DEFAULT_AGENT_DIRECTIVE_PRESETS.get(preset_id)
    if not preset:
        return None
    return preset.get('content')


@dataclass
class UserProfile:
    """Represents a user profile with all settings and preferences."""
    user_id: str
    username: str
    display_name: Optional[str] = None
    origin_peer: Optional[str] = None
    bio: Optional[str] = None
    avatar_file_id: Optional[str] = None
    avatar_url: Optional[str] = None
    agent_directives: Optional[str] = None
    theme_preference: str = "dark"  # dark, light, auto, liquid-glass
    notification_settings: Optional[Dict[str, Any]] = None
    privacy_settings: Optional[Dict[str, Any]] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        data = asdict(self)
        if self.created_at:
            data['created_at'] = self.created_at.isoformat()
        if self.updated_at:
            data['updated_at'] = self.updated_at.isoformat()
        return data

class ProfileManager:
    """Manages user profiles and preferences."""
    
    def __init__(self, db: DatabaseManager, file_manager: FileManager):
        """Initialize the profile manager.
        
        Args:
            db: Database manager instance
            file_manager: File manager for avatar storage
        """
        self.db = db
        self.file_manager = file_manager
        logger.info("Initializing ProfileManager")
        
        self._ensure_tables()
        logger.info("ProfileManager initialized successfully")
    
    def _ensure_tables(self) -> None:
        """Ensure profile-related database tables exist."""
        logger.info("Ensuring profile database tables exist...")
        
        # Add columns one by one to handle existing columns gracefully
        columns_to_add = [
            ("display_name", "TEXT"),
            ("bio", "TEXT"), 
            ("avatar_file_id", "TEXT"),
            ("agent_directives", "TEXT"),
            ("theme_preference", "TEXT DEFAULT 'dark'"),
            ("notification_settings", "TEXT"),
            ("privacy_settings", "TEXT"),
            ("profile_updated_at", "TIMESTAMP")
        ]
        
        try:
            with self.db.get_connection() as conn:
                for column_name, column_def in columns_to_add:
                    try:
                        conn.execute(f"ALTER TABLE users ADD COLUMN {column_name} {column_def}")
                        logger.debug(f"Added column {column_name} to users table")
                    except Exception as e:
                        if "duplicate column name" in str(e).lower():
                            logger.debug(f"Column {column_name} already exists, skipping")
                        else:
                            logger.warning(f"Failed to add column {column_name}: {e}")
                conn.commit()
                logger.info("Profile database tables ensured successfully")
        except Exception as e:
            logger.error(f"Failed to ensure profile tables: {e}", exc_info=True)
            raise
    
    @log_performance('profile')
    def get_profile(self, user_id: str) -> Optional[UserProfile]:
        """Get user profile by user ID.
        
        Args:
            user_id: ID of the user
            
        Returns:
            UserProfile object if found, None otherwise
        """
        logger.debug(f"Getting profile for user {user_id}")
        
        try:
            with self.db.get_connection() as conn:
                cursor = conn.execute("""
                    SELECT 
                        id, username, display_name, origin_peer, bio, avatar_file_id,
                        agent_directives, theme_preference, notification_settings, privacy_settings,
                        created_at, profile_updated_at
                    FROM users 
                    WHERE id = ?
                """, (user_id,))
                
                row = cursor.fetchone()
                if not row:
                    # Compatibility fallback: some call sites may provide username
                    # or display_name. Resolve only when the match is unique.
                    alias_rows = conn.execute("""
                        SELECT
                            id, username, display_name, origin_peer, bio, avatar_file_id,
                            agent_directives, theme_preference, notification_settings, privacy_settings,
                            created_at, profile_updated_at
                        FROM users
                        WHERE username = ? OR display_name = ?
                        LIMIT 2
                    """, (user_id, user_id)).fetchall()
                    if len(alias_rows) == 1:
                        row = alias_rows[0]
                        logger.debug(
                            f"Resolved profile alias '{user_id}' to user_id '{row['id']}'"
                        )
                    else:
                        logger.debug(f"User profile not found: {user_id}")
                        return None
                
                # Generate avatar URL if avatar exists
                avatar_url = None
                if row['avatar_file_id']:
                    avatar_url = f"/files/{row['avatar_file_id']}"
                
                # Parse JSON fields
                notification_settings = None
                privacy_settings = None
                
                if row['notification_settings']:
                    try:
                        import json
                        notification_settings = json.loads(row['notification_settings'])
                    except:
                        pass
                
                if row['privacy_settings']:
                    try:
                        import json
                        privacy_settings = json.loads(row['privacy_settings'])
                    except:
                        pass
                
                profile = UserProfile(
                    user_id=row['id'],
                    username=row['username'],
                    display_name=row['display_name'],
                    origin_peer=row['origin_peer'] if 'origin_peer' in row.keys() else None,
                    bio=row['bio'],
                    avatar_file_id=row['avatar_file_id'],
                    avatar_url=avatar_url,
                    agent_directives=row['agent_directives'] if 'agent_directives' in row.keys() else None,
                    theme_preference=row['theme_preference'] or 'dark',
                    notification_settings=notification_settings,
                    privacy_settings=privacy_settings,
                    created_at=datetime.fromisoformat(row['created_at']) if row['created_at'] else None,
                    updated_at=datetime.fromisoformat(row['profile_updated_at']) if row['profile_updated_at'] else None
                )
                
                logger.debug(f"Found profile for user {user_id}")
                return profile
                
        except Exception as e:
            logger.error(f"Failed to get profile: {e}", exc_info=True)
            return None
    
    @log_performance('profile')
    def update_profile(self, user_id: str, **updates: Any) -> bool:
        """Update user profile fields.
        
        Args:
            user_id: ID of the user
            **updates: Fields to update (display_name, bio, theme_preference, etc.)
            
        Returns:
            True if updated successfully, False otherwise
        """
        logger.info(f"Updating profile for user {user_id}")
        
        try:
            # Build dynamic update query
            valid_fields = {
                'display_name', 'bio', 'theme_preference',
                'notification_settings', 'privacy_settings', 'avatar_file_id',
                'agent_directives', 'account_type',
            }
            
            update_fields = []
            update_values = []
            
            for field, value in updates.items():
                if field in valid_fields:
                    update_fields.append(f"{field} = ?")
                    
                    # Convert dict/list to JSON string for settings fields
                    if field.endswith('_settings') and isinstance(value, (dict, list)):
                        import json
                        value = json.dumps(value)
                    
                    if field == 'agent_directives':
                        value = normalize_agent_directives(value)

                    update_values.append(value)
            
            if not update_fields:
                logger.warning("No valid fields to update")
                return False
            
            # Add updated timestamp
            update_fields.append("profile_updated_at = ?")
            update_values.append(datetime.now(timezone.utc).isoformat())
            update_values.append(user_id)
            
            query = f"""
                UPDATE users 
                SET {', '.join(update_fields)}
                WHERE id = ?
            """
            
            with self.db.get_connection() as conn:
                cursor = conn.execute(query, update_values)
                conn.commit()
                
                if cursor.rowcount > 0:
                    logger.info(f"Profile updated for user {user_id}")
                    return True
                else:
                    logger.warning(f"No rows updated for user {user_id}")
                    return False
                    
        except Exception as e:
            logger.error(f"Failed to update profile: {e}", exc_info=True)
            return False
    
    @log_performance('profile')
    def update_avatar(self, user_id: str, avatar_data: bytes, 
                     filename: str, content_type: str) -> Optional[str]:
        """Update user avatar image.
        
        Args:
            user_id: ID of the user
            avatar_data: Image file data
            filename: Original filename
            content_type: MIME type of the image
            
        Returns:
            File ID of saved avatar, None if failed
        """
        logger.info(f"Updating avatar for user {user_id}")
        
        try:
            # Validate image type
            if not content_type.startswith('image/'):
                logger.error(f"Invalid avatar content type: {content_type}")
                return None
            
            # Save avatar file
            file_info = self.file_manager.save_file(
                avatar_data, filename, content_type, user_id
            )
            
            if not file_info:
                logger.error("Failed to save avatar file")
                return None
            
            # Update user record with avatar file ID directly
            try:
                with self.db.get_connection() as conn:
                    cursor = conn.execute("""
                        UPDATE users 
                        SET avatar_file_id = ?, profile_updated_at = ?
                        WHERE id = ?
                    """, (file_info.id, datetime.now(timezone.utc).isoformat(), user_id))
                    conn.commit()
                    success = cursor.rowcount > 0
            except Exception as e:
                logger.error(f"Failed to update avatar in database: {e}")
                success = False
            
            if success:
                logger.info(f"Avatar updated for user {user_id}: {file_info.id}")
                return cast(str, file_info.id)
            else:
                logger.error("Failed to update user record with avatar")
                return None
                
        except Exception as e:
            logger.error(f"Failed to update avatar: {e}", exc_info=True)
            return None
    
    @log_performance('profile')
    def get_user_display_name(self, user_id: str) -> str:
        """Get the display name for a user, falling back to username.
        
        Args:
            user_id: ID of the user
            
        Returns:
            Display name or username
        """
        try:
            profile = self.get_profile(user_id)
            if profile and profile.display_name:
                return cast(str, profile.display_name)
            elif profile:
                return cast(str, profile.username)
            else:
                return user_id
        except Exception as e:
            logger.error(f"Failed to get display name: {e}")
            return user_id
    
    def get_user_avatar_url(self, user_id: str) -> Optional[str]:
        """Get the avatar URL for a user.
        
        Args:
            user_id: ID of the user
            
        Returns:
            Avatar URL if exists, None otherwise
        """
        try:
            profile = self.get_profile(user_id)
            return profile.avatar_url if profile else None
        except Exception as e:
            logger.error(f"Failed to get avatar URL: {e}")
            return None
    
    @log_performance('profile')
    def ensure_default_profile(self, user_id: str, username: str) -> UserProfile:
        """Ensure a user has a default profile, creating if needed.
        
        Args:
            user_id: ID of the user
            username: Username for the user
            
        Returns:
            UserProfile object
        """
        
        profile = self.get_profile(user_id)
        if profile:
            return cast(UserProfile, profile)
        
        logger.info(f"Creating default profile for user {user_id}")
        
        # Create user if doesn't exist
        try:
            with self.db.get_connection() as conn:
                conn.execute("""
                    INSERT OR IGNORE INTO users (id, username, public_key, theme_preference)
                    VALUES (?, ?, ?, ?)
                """, (user_id, username, 'default_public_key', 'dark'))
                conn.commit()
        except Exception as e:
            logger.error(f"Failed to ensure user exists: {e}")
        
        # Try to get profile again
        profile = self.get_profile(user_id)
        if not profile:
            # Create a minimal profile object
            profile = UserProfile(
                user_id=user_id,
                username=username,
                theme_preference='dark',
                created_at=datetime.now(timezone.utc)
            )
        
        return cast(UserProfile, profile)
    
    def get_profile_card(self, user_id: str) -> Optional[Dict[str, Any]]:
        """Build a shareable profile card for P2P transmission.

        Returns a dict with username, display_name, bio, account_type, and
        an optional base64-encoded avatar thumbnail (max ~64 KB JPEG).
        Returns None if the user does not exist.
        """
        profile = self.get_profile(user_id)
        if not profile:
            return None

        account_type = 'human'
        try:
            with self.db.get_connection() as conn:
                row = conn.execute(
                    "SELECT account_type FROM users WHERE id = ?",
                    (user_id,),
                ).fetchone()
            if row:
                raw_account_type = ''
                if hasattr(row, 'keys'):
                    raw_account_type = str(row['account_type'] or '').strip().lower()
                else:
                    raw_account_type = str(row[0] or '').strip().lower()
                if raw_account_type in ('human', 'agent'):
                    account_type = raw_account_type
        except Exception:
            pass

        card: Dict[str, Any] = {
            'user_id': profile.user_id,
            'username': profile.username,
            'display_name': profile.display_name or profile.username,
            'bio': profile.bio or '',
            'account_type': account_type,
        }

        # Include avatar thumbnail if available (required for P2P propagation to other peers)
        if profile.avatar_file_id and self.file_manager:
            try:
                result = self.file_manager.get_file_data(profile.avatar_file_id)
                if result:
                    import base64
                    file_data, file_info = result
                    thumb_bytes = self._make_thumbnail(file_data,
                                                       getattr(file_info, 'content_type', 'image/jpeg'))
                    if thumb_bytes and len(thumb_bytes) <= 64 * 1024:
                        card['avatar_thumbnail'] = base64.b64encode(thumb_bytes).decode('ascii')
                        card['avatar_content_type'] = 'image/jpeg'
                    else:
                        if not thumb_bytes:
                            logger.warning(
                                "Avatar not included in profile card (thumbnail failed). "
                                "Install Pillow on all peers for avatar P2P sync when image is > 64 KB."
                            )
                        else:
                            logger.warning(
                                "Avatar thumbnail too large for profile card (> 64 KB); "
                                "other peers will not receive avatar until image is smaller."
                            )
            except Exception as e:
                logger.warning(f"Could not read avatar for profile card: {e}")

        return card

    @staticmethod
    def _make_thumbnail(image_bytes: bytes, content_type: str,
                        max_size: int = 128, max_bytes: int = 64 * 1024) -> Optional[bytes]:
        """Downscale an image to fit within max_size x max_size and max_bytes (default 64 KB).

        Uses Pillow if available; otherwise returns the raw bytes only when already small enough.
        Tries lower quality/smaller size if first attempt exceeds max_bytes so avatar can propagate.
        """
        try:
            from PIL import Image
            import io
            img: Any = Image.open(io.BytesIO(image_bytes))
            if img.mode in ('RGBA', 'P'):
                img = img.convert('RGB')
            resample_lanczos = getattr(getattr(Image, "Resampling", Image), "LANCZOS")
            for size, quality in [(max_size, 75), (max_size, 50), (96, 50), (64, 45)]:
                work = img.copy()
                work.thumbnail((size, size), resample_lanczos)
                buf = io.BytesIO()
                work.save(buf, format='JPEG', quality=quality)
                out = buf.getvalue()
                if len(out) <= max_bytes:
                    return out
            return None  # Could not fit under max_bytes
        except ImportError:
            # Pillow not installed — return raw if small enough
            if len(image_bytes) <= max_bytes:
                return image_bytes
            return None
        except Exception as e:
            logger.warning(f"Thumbnail generation failed: {e}")
            if len(image_bytes) <= max_bytes:
                return image_bytes
            return None

    def update_from_remote(self, user_id: str,
                           profile_data: Dict[str, Any],
                           force_display_name: bool = False) -> bool:
        """Apply a remote profile card to a local (shadow) user.

        Updates display_name, bio, and avatar.  Creates the user row
        if it does not exist yet.

        If force_display_name is True, the display_name is always
        updated (used when a real profile sync arrives from the
        actual peer, which should overwrite any provisional name).

        Returns True if any change was applied.
        """
        display_name = profile_data.get('display_name')
        bio = profile_data.get('bio')
        remote_account_type = str(profile_data.get('account_type') or '').strip().lower()
        avatar_b64 = profile_data.get('avatar_thumbnail')
        avatar_ct = profile_data.get('avatar_content_type', 'image/jpeg')
        remote_username = profile_data.get('username', '')

        changed = False

        try:
            # Ensure user row exists
            with self.db.get_connection() as conn:
                row = conn.execute(
                    "SELECT id, display_name, bio, avatar_file_id, account_type FROM users WHERE id = ?",
                    (user_id,)
                ).fetchone()

            if not row:
                logger.debug(f"update_from_remote: user {user_id} not found, skipping")
                return False

            updates: Dict[str, Any] = {}
            if display_name:
                if force_display_name or display_name != row['display_name']:
                    updates['display_name'] = display_name
            if bio is not None and bio != (row['bio'] or ''):
                updates['bio'] = bio
            if remote_account_type in ('human', 'agent'):
                current_account_type = str(row['account_type'] or '').strip().lower()
                if remote_account_type != current_account_type:
                    updates['account_type'] = remote_account_type

            # Save avatar thumbnail — only if content actually changed
            if avatar_b64 and self.file_manager:
                try:
                    import base64, hashlib
                    avatar_bytes = base64.b64decode(avatar_b64)
                    new_hash = hashlib.sha256(avatar_bytes).hexdigest()

                    # Check if existing avatar has same content
                    existing_avatar_id = row['avatar_file_id'] if 'avatar_file_id' in row.keys() else None
                    save_needed = True
                    if existing_avatar_id:
                        try:
                            existing_data = self.file_manager.get_file_data(existing_avatar_id)
                            if existing_data:
                                existing_bytes, _ = existing_data
                                if hashlib.sha256(existing_bytes).hexdigest() == new_hash:
                                    save_needed = False
                                    logger.debug(f"Avatar unchanged for {user_id}, skipping save")
                        except Exception:
                            pass  # If we can't read existing, save new one

                    if save_needed:
                        file_info = self.file_manager.save_file(
                            file_data=avatar_bytes,
                            original_name=f"avatar_{remote_username or user_id}.jpg",
                            content_type=avatar_ct,
                            uploaded_by=user_id,
                        )
                        if file_info:
                            updates['avatar_file_id'] = file_info.id
                            logger.info(f"Saved remote avatar for {user_id}: {file_info.id}")
                except Exception as e:
                    logger.warning(f"Failed to save remote avatar: {e}")

            if updates:
                changed = self.update_profile(user_id, **updates)
                if changed:
                    logger.info(f"Updated remote profile for {user_id}: "
                                f"{list(updates.keys())}")

        except Exception as e:
            logger.error(f"update_from_remote failed for {user_id}: {e}",
                         exc_info=True)

        return changed

    def get_all_users_display_info(self) -> Dict[str, Dict[str, Any]]:
        """Get display information for all users.
        
        Returns:
            Dictionary mapping user_id to display info
        """
        try:
            with self.db.get_connection() as conn:
                cursor = conn.execute("""
                    SELECT id, username, display_name, avatar_file_id, origin_peer
                    FROM users
                """)
                
                users = {}
                for row in cursor.fetchall():
                    avatar_url = None
                    if row['avatar_file_id']:
                        avatar_url = f"/files/{row['avatar_file_id']}"
                    
                    users[row['id']] = {
                        'username': row['username'],
                        'display_name': row['display_name'] or row['username'],
                        'avatar_url': avatar_url,
                        'origin_peer': row['origin_peer'] if 'origin_peer' in row.keys() else None,
                    }
                
                return users
                
        except Exception as e:
            logger.error(f"Failed to get users display info: {e}")
            return {}
