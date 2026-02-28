"""
API Key management system for Canopy.

Provides granular access control with permissions for different data types
and operations, supporting the core security model of the application.

Author: Konrad Walus (architecture, design, and direction)
Project: Canopy - Local Mesh Communication
License: Apache 2.0
Development: AI-assisted implementation (Claude, Codex, GitHub Copilot, Cursor IDE, Ollama)
"""

import hashlib
import secrets
import json
import logging
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Set, Any, cast
from dataclasses import dataclass, asdict
from enum import Enum

from ..core.database import DatabaseManager

logger = logging.getLogger(__name__)


class Permission(Enum):
    """Available permissions for API keys."""
    READ_MESSAGES = "read_messages"
    WRITE_MESSAGES = "write_messages"
    READ_FILES = "read_files"
    WRITE_FILES = "write_files"
    READ_FEED = "read_feed"
    WRITE_FEED = "write_feed"
    VOICE_CALL = "voice_call"
    MANAGE_KEYS = "manage_keys"
    VIEW_TRUST = "view_trust"
    DELETE_DATA = "delete_data"


@dataclass
class ApiKeyInfo:
    """Information about an API key."""
    id: str
    user_id: str
    key_hash: str
    permissions: Set[Permission]
    created_at: datetime
    expires_at: Optional[datetime] = None
    revoked: bool = False
    account_pending: bool = False  # True if user status is pending_approval (agent not yet approved)
    
    def has_permission(self, permission: Permission) -> bool:
        """Check if key has a specific permission."""
        return not self.revoked and permission in self.permissions
    
    def is_valid(self) -> bool:
        """Check if key is currently valid."""
        if self.revoked:
            return False
        if self.expires_at and datetime.now() > self.expires_at.replace(tzinfo=None):
            return False
        return True
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            'id': self.id,
            'user_id': self.user_id,
            'permissions': [p.value for p in self.permissions],
            'created_at': self.created_at.isoformat(),
            'expires_at': self.expires_at.isoformat() if self.expires_at else None,
            'revoked': self.revoked
        }


class ApiKeyManager:
    """Manages API keys for access control."""
    
    def __init__(self, db_manager: DatabaseManager):
        """Initialize API key manager with database connection."""
        self.db = db_manager
    
    def generate_key(self, user_id: str, permissions: List[Permission], 
                    expires_days: Optional[int] = None) -> Optional[str]:
        """Generate a new API key with specified permissions."""
        try:
            # Backward-compatible safety: prevent unusable zero-permission keys.
            if not permissions:
                permissions = self.get_default_permissions()
                logger.info(
                    "No permissions provided for API key generation; "
                    f"applying defaults for user {user_id}"
                )

            # Generate secure random key
            raw_key = secrets.token_urlsafe(32)
            key_hash = self._hash_key(raw_key)
            key_id = secrets.token_hex(16)
            
            # Calculate expiry
            expires_at = None
            if expires_days:
                expires_at = datetime.now() + timedelta(days=expires_days)
            
            # Store in database
            with self.db.get_connection() as conn:
                conn.execute("""
                    INSERT INTO api_keys (id, user_id, key_hash, permissions, expires_at)
                    VALUES (?, ?, ?, ?, ?)
                """, (
                    key_id, user_id, key_hash, 
                    json.dumps([p.value for p in permissions]),
                    expires_at.isoformat() if expires_at else None
                ))
                conn.commit()
            
            logger.info(f"Generated API key for user {user_id} with {len(permissions)} permissions")
            return raw_key
            
        except Exception as e:
            logger.error(f"Failed to generate API key: {e}")
            return None
    
    def validate_key(self, raw_key: str, required_permission: Optional[Permission] = None) -> Optional[ApiKeyInfo]:
        """Validate an API key and return key information if valid."""
        try:
            key_hash = self._hash_key(raw_key)
            
            with self.db.get_connection() as conn:
                cursor = conn.execute("""
                    SELECT id, user_id, key_hash, permissions, created_at, expires_at, revoked
                    FROM api_keys WHERE key_hash = ?
                """, (key_hash,))
                
                row = cursor.fetchone()
                if not row:
                    return None
                
                # Parse the key info
                key_info = ApiKeyInfo(
                    id=row['id'],
                    user_id=row['user_id'],
                    key_hash=row['key_hash'],
                    permissions={Permission(p) for p in json.loads(row['permissions'])},
                    created_at=datetime.fromisoformat(row['created_at']).replace(tzinfo=None),
                    expires_at=datetime.fromisoformat(row['expires_at']).replace(tzinfo=None) if row['expires_at'] else None,
                    revoked=bool(row['revoked'])
                )
                
                # Check if key is valid
                if not key_info.is_valid():
                    logger.warning(f"Invalid API key used: {key_info.id}")
                    return None
                
                # Check required permission if specified
                if required_permission and not self._has_effective_permission(
                    key_info.permissions,
                    required_permission,
                ):
                    logger.warning(
                        "API key %s lacks required permission: %s (granted=%s)",
                        key_info.id,
                        required_permission.value,
                        sorted(p.value for p in key_info.permissions),
                    )
                    return None

                # Check user account status: suspended => invalid; pending_approval => limited (auth/status only)
                user = self.db.get_user(key_info.user_id)
                if user:
                    status = (user.get('status') or 'active').lower()
                    if status == 'suspended':
                        logger.warning(f"API key {key_info.id} belongs to suspended account")
                        return None
                    if status == 'pending_approval':
                        key_info = ApiKeyInfo(
                            id=key_info.id, user_id=key_info.user_id, key_hash=key_info.key_hash,
                            permissions=key_info.permissions, created_at=key_info.created_at,
                            expires_at=key_info.expires_at, revoked=key_info.revoked,
                            account_pending=True
                        )
                
                return key_info
                
        except Exception as e:
            logger.error(f"API key validation error: {e}")
            return None
    
    def revoke_key(self, key_id: str, user_id: str) -> bool:
        """Revoke an API key."""
        try:
            with self.db.get_connection() as conn:
                cursor = conn.execute("""
                    UPDATE api_keys SET revoked = TRUE 
                    WHERE id = ? AND user_id = ?
                """, (key_id, user_id))
                
                if cursor.rowcount == 0:
                    logger.warning(f"No API key found to revoke: {key_id}")
                    return False
                
                conn.commit()
                logger.info(f"Revoked API key: {key_id}")
                return True
                
        except Exception as e:
            logger.error(f"Failed to revoke API key: {e}")
            return False
    
    def list_keys(self, user_id: str) -> List[ApiKeyInfo]:
        """List all API keys for a user."""
        try:
            with self.db.get_connection() as conn:
                cursor = conn.execute("""
                    SELECT id, user_id, key_hash, permissions, created_at, expires_at, revoked
                    FROM api_keys WHERE user_id = ?
                    ORDER BY created_at DESC
                """, (user_id,))
                
                keys = []
                for row in cursor.fetchall():
                    key_info = ApiKeyInfo(
                        id=row['id'],
                        user_id=row['user_id'],
                        key_hash=row['key_hash'],
                        permissions={Permission(p) for p in json.loads(row['permissions'])},
                        created_at=datetime.fromisoformat(row['created_at']).replace(tzinfo=None),
                        expires_at=datetime.fromisoformat(row['expires_at']).replace(tzinfo=None) if row['expires_at'] else None,
                        revoked=bool(row['revoked'])
                    )
                    keys.append(key_info)
                
                return keys
                
        except Exception as e:
            logger.error(f"Failed to list API keys: {e}")
            return []
    
    def update_key_permissions(self, key_id: str, user_id: str, 
                             new_permissions: List[Permission]) -> bool:
        """Update permissions for an existing API key."""
        try:
            with self.db.get_connection() as conn:
                cursor = conn.execute("""
                    UPDATE api_keys SET permissions = ?
                    WHERE id = ? AND user_id = ? AND revoked = FALSE
                """, (
                    json.dumps([p.value for p in new_permissions]),
                    key_id, user_id
                ))
                
                if cursor.rowcount == 0:
                    logger.warning(f"No valid API key found to update: {key_id}")
                    return False
                
                conn.commit()
                logger.info(f"Updated permissions for API key: {key_id}")
                return True
                
        except Exception as e:
            logger.error(f"Failed to update API key permissions: {e}")
            return False
    
    def cleanup_expired_keys(self) -> int:
        """Remove expired API keys from the database."""
        try:
            current_time = datetime.now().isoformat()
            
            with self.db.get_connection() as conn:
                cursor = conn.execute("""
                    DELETE FROM api_keys 
                    WHERE expires_at IS NOT NULL AND expires_at < ?
                """, (current_time,))
                
                deleted_count = cast(int, cursor.rowcount)
                conn.commit()
                
                if deleted_count > 0:
                    logger.info(f"Cleaned up {deleted_count} expired API keys")
                
                return deleted_count
                
        except Exception as e:
            logger.error(f"Failed to cleanup expired keys: {e}")
            return 0
    
    def get_key_usage_stats(self, user_id: str) -> Dict[str, int]:
        """Get usage statistics for a user's API keys."""
        try:
            with self.db.get_connection() as conn:
                cursor = conn.execute("""
                    SELECT 
                        COUNT(*) as total_keys,
                        SUM(CASE WHEN revoked THEN 1 ELSE 0 END) as revoked_keys,
                        SUM(CASE WHEN expires_at IS NULL THEN 1 ELSE 0 END) as permanent_keys,
                        SUM(CASE WHEN expires_at > ? THEN 1 ELSE 0 END) as active_keys
                    FROM api_keys WHERE user_id = ?
                """, (datetime.now(timezone.utc).isoformat(), user_id))
                
                row = cursor.fetchone()
                return {
                    'total_keys': row['total_keys'] or 0,
                    'revoked_keys': row['revoked_keys'] or 0,
                    'permanent_keys': row['permanent_keys'] or 0,
                    'active_keys': row['active_keys'] or 0
                }
                
        except Exception as e:
            logger.error(f"Failed to get key usage stats: {e}")
            return {'total_keys': 0, 'revoked_keys': 0, 'permanent_keys': 0, 'active_keys': 0}
    
    def _hash_key(self, raw_key: str) -> str:
        """Hash an API key for secure storage."""
        return hashlib.sha256(raw_key.encode()).hexdigest()

    @staticmethod
    def _has_effective_permission(
        granted_permissions: Set[Permission],
        required_permission: Permission,
    ) -> bool:
        """Resolve permission with legacy compatibility aliases."""
        if required_permission in granted_permissions:
            return True
        legacy_aliases = {
            # Legacy agent keys were often scoped to message perms only.
            Permission.READ_FEED: Permission.READ_MESSAGES,
            Permission.WRITE_FEED: Permission.WRITE_MESSAGES,
        }
        alias = legacy_aliases.get(required_permission)
        return bool(alias and alias in granted_permissions)
    
    @staticmethod
    def get_default_permissions() -> List[Permission]:
        """Default permissions for new keys (read + write feed/channels so agents can post)."""
        return [
            Permission.READ_MESSAGES,
            Permission.WRITE_MESSAGES,
            Permission.READ_FEED,
            Permission.WRITE_FEED,
        ]
    
    @staticmethod
    def get_all_permissions() -> List[Permission]:
        """Get all available permissions."""
        return list(Permission)
