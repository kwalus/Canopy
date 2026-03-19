"""
Core messaging system for Canopy local communication.

Handles message creation, delivery, and management for local network communication
with support for different message types and encryption.

Author: Konrad Walus (architecture, design, and direction)
Project: Canopy - Local Mesh Communication
License: Apache 2.0
Development: AI-assisted implementation (Claude, Codex, GitHub Copilot, Cursor IDE, Ollama)
"""

import hashlib
import logging
import secrets
import json
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any, Sequence, Union, cast
from dataclasses import dataclass, asdict
from enum import Enum

from .database import DatabaseManager
from ..security.api_keys import ApiKeyManager, Permission
from ..security.encryption import RecipientEncryptor
from .events import (
    EVENT_DM_MESSAGE_CREATED,
    EVENT_DM_MESSAGE_DELETED,
    EVENT_DM_MESSAGE_EDITED,
    EVENT_DM_MESSAGE_READ,
)

logger = logging.getLogger(__name__)


DM_E2E_CAPABILITY = "dm_e2e_v1"
DM_E2E_PROTOCOL = "dm_peer_e2e_v1"
DM_CRYPTO_METADATA_KEY = "__dm_crypto"


def _normalize_dm_security_summary(summary: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    data = dict(summary or {})
    mode = str(data.get("mode") or "").strip().lower()
    state = str(data.get("state") or "").strip().lower()
    label = str(data.get("label") or "").strip()
    if not mode:
        mode = "legacy_plaintext"
    if not state:
        state = "plaintext"
    if not label:
        if mode == "peer_e2e_v1":
            label = "E2E over mesh"
        elif mode == "local_only":
            label = "Local only"
        elif mode == "mixed":
            label = "Mixed delivery"
        elif state == "decrypt_failed":
            label = "Decryption failed"
        else:
            label = "Legacy relay/plaintext"
    data["mode"] = mode
    data["state"] = state
    data["label"] = label
    data["e2e"] = bool(data.get("e2e", mode == "peer_e2e_v1"))
    data["relay_confidential"] = bool(
        data.get("relay_confidential", mode == "peer_e2e_v1" or mode == "local_only")
    )
    data["local_only"] = bool(data.get("local_only", mode == "local_only"))
    return data


def build_dm_security_summary(
    db_manager: Any,
    p2p_manager: Any,
    recipient_ids: Sequence[str],
) -> Dict[str, Any]:
    recipients = [
        str(raw_user_id or "").strip()
        for raw_user_id in (recipient_ids or [])
        if str(raw_user_id or "").strip()
    ]
    if p2p_manager and hasattr(p2p_manager, "describe_direct_message_security"):
        try:
            return _normalize_dm_security_summary(
                p2p_manager.describe_direct_message_security(recipients)
            )
        except Exception:
            pass

    local_targets = filter_local_dm_targets(db_manager, p2p_manager, recipients)
    if recipients and len(local_targets) == len(recipients):
        return _normalize_dm_security_summary(
            {
                "mode": "local_only",
                "state": "local_only",
                "label": "Local only",
                "e2e": False,
                "relay_confidential": True,
                "local_only": True,
                "recipient_ids": recipients,
                "local_recipient_ids": local_targets,
                "remote_peer_ids": [],
            }
        )

    return _normalize_dm_security_summary(
        {
            "mode": "legacy_plaintext",
            "state": "plaintext",
            "label": "Legacy relay/plaintext",
            "e2e": False,
            "relay_confidential": False,
            "local_only": False,
            "recipient_ids": recipients,
            "local_recipient_ids": local_targets,
        }
    )


def _strip_dm_internal_metadata(metadata: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    clean = dict(metadata or {})
    clean.pop(DM_CRYPTO_METADATA_KEY, None)
    return clean


def encrypt_dm_transport_bundle(
    content: str,
    metadata: Optional[Dict[str, Any]],
    recipient_peer_id: str,
    recipient_public_key_bytes: bytes,
    *,
    sender_peer_id: Optional[str] = None,
) -> tuple[str, Dict[str, Any], Dict[str, Any]]:
    clean_metadata = _strip_dm_internal_metadata(metadata)
    bundle = {
        "content": content or "",
        "metadata": clean_metadata,
    }
    encrypted = RecipientEncryptor.encrypt_for_recipients(
        json.dumps(bundle, ensure_ascii=True, separators=(",", ":")),
        {recipient_peer_id: recipient_public_key_bytes},
    )
    security = _normalize_dm_security_summary(
        {
            "mode": "peer_e2e_v1",
            "state": "encrypted",
            "label": "E2E over mesh",
            "e2e": True,
            "relay_confidential": True,
            "local_only": False,
            "target_peer_id": recipient_peer_id,
            "encrypted_for_peer_ids": [recipient_peer_id],
        }
    )
    encrypted_metadata = {
        "security": dict(security),
        DM_CRYPTO_METADATA_KEY: {
            "protocol": DM_E2E_PROTOCOL,
            "version": 1,
            "state": "encrypted",
            "target_peer_id": recipient_peer_id,
            "sender_peer_id": str(sender_peer_id or "").strip() or None,
            "encrypted_content": encrypted.get("encrypted_content"),
            "wrapped_keys": encrypted.get("wrapped_keys") or {},
            "payload_format": "dm_bundle_v1",
        },
    }
    return "", encrypted_metadata, security


def unwrap_dm_transport_bundle(
    content: str,
    metadata: Optional[Dict[str, Any]],
    recipient_peer_id: str,
    recipient_private_key_bytes: Optional[bytes],
) -> tuple[str, Dict[str, Any], Dict[str, Any]]:
    clean_metadata = _strip_dm_internal_metadata(metadata)
    crypto = (metadata or {}).get(DM_CRYPTO_METADATA_KEY) if isinstance(metadata, dict) else None
    if not isinstance(crypto, dict):
        security = _normalize_dm_security_summary(clean_metadata.get("security"))
        clean_metadata["security"] = security
        return content or "", clean_metadata, security

    encrypted_content = str(crypto.get("encrypted_content") or "").strip()
    wrapped_keys = crypto.get("wrapped_keys") if isinstance(crypto.get("wrapped_keys"), dict) else {}
    wrapped_key = str(wrapped_keys.get(recipient_peer_id) or "").strip()
    if not encrypted_content or not wrapped_key or not recipient_private_key_bytes:
        security = _normalize_dm_security_summary(
            {
                "mode": "peer_e2e_v1",
                "state": "decrypt_failed",
                "label": "Decryption failed",
                "e2e": True,
                "relay_confidential": True,
                "local_only": False,
                "target_peer_id": recipient_peer_id,
                "warning": "Encrypted DM could not be decrypted on this peer",
            }
        )
        clean_metadata["security"] = security
        return "[Encrypted direct message could not be decrypted]", clean_metadata, security

    plaintext = RecipientEncryptor.decrypt_for_recipient(
        encrypted_content,
        wrapped_key,
        recipient_private_key_bytes,
    )
    if plaintext == "[Access denied - cannot decrypt]":
        security = _normalize_dm_security_summary(
            {
                "mode": "peer_e2e_v1",
                "state": "decrypt_failed",
                "label": "Decryption failed",
                "e2e": True,
                "relay_confidential": True,
                "local_only": False,
                "target_peer_id": recipient_peer_id,
                "warning": "Encrypted DM could not be decrypted on this peer",
            }
        )
        clean_metadata["security"] = security
        return "[Encrypted direct message could not be decrypted]", clean_metadata, security

    try:
        decoded = json.loads(plaintext)
    except Exception:
        decoded = {"content": plaintext, "metadata": {}}

    bundle_content = ""
    bundle_metadata: Dict[str, Any] = {}
    if isinstance(decoded, dict):
        bundle_content = str(decoded.get("content") or "")
        if isinstance(decoded.get("metadata"), dict):
            bundle_metadata = _strip_dm_internal_metadata(decoded.get("metadata"))
    else:
        bundle_content = str(decoded or "")

    security = _normalize_dm_security_summary(
        {
            "mode": "peer_e2e_v1",
            "state": "encrypted",
            "label": "E2E over mesh",
            "e2e": True,
            "relay_confidential": True,
            "local_only": False,
            "target_peer_id": recipient_peer_id,
            "sender_peer_id": str(crypto.get("sender_peer_id") or "").strip() or None,
            "encrypted_for_peer_ids": [recipient_peer_id],
        }
    )
    bundle_metadata["security"] = security
    return bundle_content, bundle_metadata, security


def compute_group_id(member_ids: Sequence[str]) -> str:
    """Create a stable group DM identifier from a member set."""
    cleaned = sorted({str(member_id).strip() for member_id in (member_ids or []) if str(member_id).strip()})
    digest = hashlib.sha256("|".join(cleaned).encode("utf-8")).hexdigest()[:12]
    return f"group:{digest}"


def build_dm_preview(content: str, attachments: Optional[Sequence[Dict[str, Any]]] = None) -> Optional[str]:
    """Build a human-readable preview for DM inbox/catchup payloads."""
    text = str(content or "").strip()
    if text:
        return text[:200]
    attachment_count = len(list(attachments or []))
    if attachment_count:
        return f"Sent {attachment_count} attachment{'s' if attachment_count != 1 else ''}"
    return None


def is_local_dm_user(db_manager: Any, p2p_manager: Any, user_id: Optional[str]) -> bool:
    """Return True only for real local accounts on this node.

    Remote shadow users should not receive locally-created inbox rows; their
    inbox is created on the owning peer via the P2P DM receive path.
    """
    uid = str(user_id or "").strip()
    if not uid or not db_manager:
        return False
    try:
        row = db_manager.get_user(uid)
    except Exception:
        row = None
    if not row:
        return False
    username = str(row.get("username") or "").strip()
    if username.startswith("peer-"):
        return False
    try:
        local_peer_id = p2p_manager.get_peer_id() if p2p_manager else None
    except Exception:
        local_peer_id = None
    origin_peer = str(row.get("origin_peer") or "").strip()
    local_peer_id = str(local_peer_id or "").strip()
    if origin_peer:
        return bool(local_peer_id and origin_peer == local_peer_id)

    # Blank origin metadata is ambiguous on legacy or partially synced peers.
    # Only treat the recipient as local when we have positive local-account
    # evidence, otherwise fall back to mesh delivery instead of silently
    # downgrading the DM to local_only.
    if str(row.get("password_hash") or "").strip():
        return True
    if bool(row.get("is_registered")):
        return True

    try:
        with db_manager.get_connection() as conn:
            row_user_key = None
            row_api_key = None
            try:
                row_user_key = conn.execute(
                    "SELECT 1 FROM user_keys WHERE user_id = ? LIMIT 1",
                    (uid,),
                ).fetchone()
            except Exception:
                row_user_key = None
            try:
                row_api_key = conn.execute(
                    "SELECT 1 FROM api_keys WHERE user_id = ? AND COALESCE(revoked, 0) = 0 LIMIT 1",
                    (uid,),
                ).fetchone()
            except Exception:
                row_api_key = None
        return bool(row_user_key or row_api_key)
    except Exception:
        return False


def filter_local_dm_targets(db_manager: Any, p2p_manager: Any, user_ids: Sequence[str]) -> List[str]:
    """Return deduplicated DM target IDs that are local to this node."""
    seen: set[str] = set()
    filtered: List[str] = []
    for raw_user_id in user_ids or []:
        uid = str(raw_user_id or "").strip()
        if not uid or uid in seen:
            continue
        seen.add(uid)
        if is_local_dm_user(db_manager, p2p_manager, uid):
            filtered.append(uid)
    return filtered


class MessageType(Enum):
    """Types of messages supported by Canopy."""
    TEXT = "text"
    FILE = "file"
    IMAGE = "image"
    VOICE = "voice"
    SYSTEM = "system"
    DELETE_SIGNAL = "delete_signal"


class MessageStatus(Enum):
    """Status of message delivery."""
    PENDING = "pending"
    SENT = "sent"
    DELIVERED = "delivered"
    READ = "read"
    FAILED = "failed"


@dataclass
class Message:
    """Represents a message in the Canopy system."""
    id: str
    sender_id: str
    recipient_id: Optional[str]  # None for broadcast messages
    content: str
    message_type: MessageType
    status: MessageStatus
    created_at: datetime
    metadata: Optional[Dict[str, Any]] = None
    delivered_at: Optional[datetime] = None
    read_at: Optional[datetime] = None
    edited_at: Optional[datetime] = None
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert message to dictionary for JSON serialization."""
        result: Dict[str, Any] = {
            'id': self.id,
            'sender_id': self.sender_id,
            'recipient_id': self.recipient_id,
            'content': self.content,
            'message_type': self.message_type.value,
            'status': self.status.value,
            'created_at': self.created_at.isoformat(),
            'metadata': self.metadata,
            'delivered_at': self.delivered_at.isoformat() if self.delivered_at else None,
            'read_at': self.read_at.isoformat() if self.read_at else None,
            'edited_at': self.edited_at.isoformat() if self.edited_at else None
        }
        
        # Extract attachments from metadata for easier template access  
        if self.metadata and 'attachments' in self.metadata:
            result['attachments'] = self.metadata['attachments']
            logger.debug(f"Message {self.id}: Found {len(self.metadata['attachments'])} attachments")
        else:
            result['attachments'] = []
            logger.debug(f"Message {self.id}: No attachments found")
            
        return result
    
    def is_broadcast(self) -> bool:
        """Check if message is a broadcast message."""
        return self.recipient_id is None
    
    def is_system_message(self) -> bool:
        """Check if message is a system message."""
        return self.message_type == MessageType.SYSTEM


class MessageManager:
    """Manages message operations for Canopy."""
    
    def __init__(self, db_manager: DatabaseManager, api_key_manager: ApiKeyManager,
                 data_encryptor: Any = None):
        """Initialize message manager with database and API key manager."""
        self.db = db_manager
        self.api_key_manager = api_key_manager
        self.data_encryptor = data_encryptor
        self.workspace_events: Any = None
        self.max_message_length = 4096  # 4KB for text messages
        self.max_broadcast_recipients = 100

    def _emit_dm_event(
        self,
        *,
        event_type: str,
        message: Message,
        dedupe_key: str,
        created_at: Optional[Any] = None,
    ) -> None:
        manager = self.workspace_events
        if not manager or not message:
            return
        metadata = message.metadata if isinstance(message.metadata, dict) else {}
        is_group_dm = isinstance(metadata.get('group_members'), list) and bool(metadata.get('group_members'))
        if not message.recipient_id and not is_group_dm:
            return
        attachments = metadata.get('attachments') or []
        preview = build_dm_preview(message.content or '', attachments)
        manager.emit_event(
            event_type=event_type,
            actor_user_id=message.sender_id,
            target_user_id=None,
            message_id=message.id,
            visibility_scope='dm',
            dedupe_key=dedupe_key,
            created_at=created_at or message.created_at,
            payload={
                'preview': preview,
                'sender_id': message.sender_id,
                'recipient_id': message.recipient_id,
                'group_id': metadata.get('group_id'),
                'group_members': metadata.get('group_members') or [],
                'attachments_count': len(attachments) if isinstance(attachments, list) else 0,
                'edited_at': message.edited_at.isoformat() if getattr(message, 'edited_at', None) else None,
            },
        )
    
    def create_message(self, sender_id: str, content: str, 
                      recipient_id: Optional[str] = None,
                      message_type: MessageType = MessageType.TEXT,
                      metadata: Optional[Dict[str, Any]] = None) -> Optional[Message]:
        """Create a new message."""
        try:
            # Validate content length
            if len(content) > self.max_message_length:
                logger.error(f"Message content too long: {len(content)} > {self.max_message_length}")
                return None
            
            # Generate unique message ID
            message_id = secrets.token_hex(16)
            
            # Create message object
            message = Message(
                id=message_id,
                sender_id=sender_id,
                recipient_id=recipient_id,
                content=content,
                message_type=message_type,
                status=MessageStatus.PENDING,
                created_at=datetime.now(timezone.utc),
                metadata=metadata
            )
            
            # Encrypt content before storage
            stored_content = content
            if self.data_encryptor and self.data_encryptor.is_enabled:
                stored_content = self.data_encryptor.encrypt(content)
            
            # Store in database
            success = self.db.store_message(
                message_id, sender_id, recipient_id, stored_content,
                message_type.value, metadata
            )
            
            if success:
                logger.info(f"Created message {message_id} from {sender_id}")
                return message
            else:
                logger.error(f"Failed to store message {message_id}")
                return None
                
        except Exception as e:
            logger.error(f"Failed to create message: {e}")
            return None
    
    def send_message(self, message: Message, api_key: Optional[str] = None) -> bool:
        """Send a message to recipient(s)."""
        try:
            # Validate API key if provided
            if api_key:
                key_info = self.api_key_manager.validate_key(api_key, Permission.WRITE_MESSAGES)
                if not key_info:
                    logger.error("Invalid API key for sending message")
                    return False
            
            # Update message status to sent
            message.status = MessageStatus.SENT
            
            # For local chat, we'll simulate immediate delivery
            # In a real mesh network, this would involve P2P communication
            if not message.is_broadcast():
                message.status = MessageStatus.DELIVERED
                message.delivered_at = datetime.now(timezone.utc)
            
            # Update in database
            with self.db.get_connection() as conn:
                conn.execute("""
                    UPDATE messages 
                    SET delivered_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                """, (message.id,))
                conn.commit()

            self._emit_dm_event(
                event_type=EVENT_DM_MESSAGE_CREATED,
                message=message,
                dedupe_key=f"{EVENT_DM_MESSAGE_CREATED}:{message.id}",
                created_at=message.created_at,
            )
            logger.info(f"Sent message {message.id}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to send message {message.id}: {e}")
            return False
    
    def get_message(self, message_id: str) -> Optional[Message]:
        """Get a single message by ID."""
        try:
            with self.db.get_connection() as conn:
                cursor = conn.execute("""
                    SELECT id, sender_id, recipient_id, content, message_type,
                           created_at, delivered_at, read_at, edited_at, metadata
                    FROM messages WHERE id = ?
                """, (message_id,))
                row = cursor.fetchone()
                if not row:
                    return None

                content = row['content']
                if self.data_encryptor and self.data_encryptor.is_enabled:
                    content = self.data_encryptor.decrypt(content)

                return Message(
                    id=row['id'],
                    sender_id=row['sender_id'],
                    recipient_id=row['recipient_id'],
                    content=content,
                    message_type=MessageType(row['message_type']),
                    status=MessageStatus.READ if row['read_at'] else (
                        MessageStatus.DELIVERED if row['delivered_at'] else MessageStatus.SENT),
                    created_at=datetime.fromisoformat(row['created_at']),
                    metadata=json.loads(row['metadata']) if row['metadata'] else None,
                    delivered_at=datetime.fromisoformat(row['delivered_at']) if row['delivered_at'] else None,
                    read_at=datetime.fromisoformat(row['read_at']) if row['read_at'] else None,
                    edited_at=datetime.fromisoformat(row['edited_at']) if row['edited_at'] else None,
                )
        except Exception as e:
            logger.error(f"Failed to get message {message_id}: {e}")
            return None

    def get_messages(self, user_id: str, limit: int = 50, 
                    since: Optional[datetime] = None) -> List[Message]:
        """Get messages for a user."""
        try:
            with self.db.get_connection() as conn:
                query = """
                    SELECT m.*, u.username as sender_username 
                    FROM messages m
                    LEFT JOIN users u ON m.sender_id = u.id
                    WHERE (
                        m.recipient_id = ?
                        OR m.recipient_id IS NULL
                        OR m.sender_id = ?
                        OR EXISTS (
                            SELECT 1
                            FROM json_each(
                                CASE WHEN json_valid(m.metadata) THEN m.metadata ELSE '{}' END,
                                '$.group_members'
                            ) gm
                            WHERE CAST(gm.value AS TEXT) = ?
                        )
                    )
                """
                params: List[Any] = [user_id, user_id, user_id]
                
                if since:
                    query += " AND m.created_at > ?"
                    params.append(since.isoformat())
                
                query += " ORDER BY m.created_at DESC LIMIT ?"
                params.append(limit)
                
                cursor = conn.execute(query, params)
                
                messages = []
                for row in cursor.fetchall():
                    # Decrypt content if encrypted
                    content = row['content']
                    if self.data_encryptor and self.data_encryptor.is_enabled:
                        content = self.data_encryptor.decrypt(content)
                    
                    message = Message(
                        id=row['id'],
                        sender_id=row['sender_id'],
                        recipient_id=row['recipient_id'],
                        content=content,
                        message_type=MessageType(row['message_type']),
                        status=MessageStatus.DELIVERED,  # Assume delivered for stored messages
                        created_at=datetime.fromisoformat(row['created_at']),
                        metadata=json.loads(row['metadata']) if row['metadata'] else None,
                        delivered_at=datetime.fromisoformat(row['delivered_at']) if row['delivered_at'] else None,
                        read_at=datetime.fromisoformat(row['read_at']) if row['read_at'] else None,
                        edited_at=datetime.fromisoformat(row['edited_at']) if row['edited_at'] else None
                    )
                    messages.append(message)
                
                return messages
                
        except Exception as e:
            logger.error(f"Failed to get messages for user {user_id}: {e}")
            return []
    
    def get_conversation(self, user_id: str, other_user_id: str, 
                        limit: int = 50) -> List[Message]:
        """Get conversation between two users."""
        try:
            with self.db.get_connection() as conn:
                cursor = conn.execute("""
                    SELECT m.*, u.username as sender_username 
                    FROM messages m
                    LEFT JOIN users u ON m.sender_id = u.id
                    WHERE ((m.sender_id = ? AND m.recipient_id = ?) OR 
                           (m.sender_id = ? AND m.recipient_id = ?))
                    ORDER BY m.created_at ASC
                    LIMIT ?
                """, (user_id, other_user_id, other_user_id, user_id, limit))
                
                messages = []
                for row in cursor.fetchall():
                    # Decrypt content if encrypted
                    content = row['content']
                    if self.data_encryptor and self.data_encryptor.is_enabled:
                        content = self.data_encryptor.decrypt(content)
                    
                    message = Message(
                        id=row['id'],
                        sender_id=row['sender_id'],
                        recipient_id=row['recipient_id'],
                        content=content,
                        message_type=MessageType(row['message_type']),
                        status=MessageStatus.DELIVERED,
                        created_at=datetime.fromisoformat(row['created_at']),
                        metadata=json.loads(row['metadata']) if row['metadata'] else None,
                        delivered_at=datetime.fromisoformat(row['delivered_at']) if row['delivered_at'] else None,
                        read_at=datetime.fromisoformat(row['read_at']) if row['read_at'] else None,
                        edited_at=datetime.fromisoformat(row['edited_at']) if row['edited_at'] else None
                    )
                    messages.append(message)
                
                return messages
                
        except Exception as e:
            logger.error(f"Failed to get conversation between {user_id} and {other_user_id}: {e}")
            return []
    
    def mark_message_read(self, message_id: str, user_id: str) -> bool:
        """Mark a message as read."""
        try:
            with self.db.get_connection() as conn:
                cursor = conn.execute("""
                    UPDATE messages 
                    SET read_at = CURRENT_TIMESTAMP
                    WHERE id = ? AND (
                        read_at IS NULL AND (
                        recipient_id = ?
                        OR recipient_id IS NULL
                        OR EXISTS (
                            SELECT 1
                            FROM json_each(
                                CASE WHEN json_valid(metadata) THEN metadata ELSE '{}' END,
                                '$.group_members'
                            ) gm
                            WHERE CAST(gm.value AS TEXT) = ?
                        )
                        )
                    )
                """, (message_id, user_id, user_id))
                
                success = cast(int, cursor.rowcount) > 0
                conn.commit()
                
                if success:
                    logger.info(f"Marked message {message_id} as read by {user_id}")
                    message = self.get_message(message_id)
                    if message:
                        self._emit_dm_event(
                            event_type=EVENT_DM_MESSAGE_READ,
                            message=message,
                            dedupe_key=f"{EVENT_DM_MESSAGE_READ}:{message_id}:{user_id}",
                            created_at=message.read_at or datetime.now(timezone.utc),
                        )
                
                return success
                
        except Exception as e:
            logger.error(f"Failed to mark message as read: {e}")
            return False
    
    def delete_message(self, message_id: str, user_id: str, file_manager: Any = None) -> bool:
        """Delete a message (only sender can delete)."""
        try:
            with self.db.get_connection() as conn:
                # Check if user is the sender
                cursor = conn.execute("""
                    SELECT sender_id, recipient_id, content, message_type, created_at, delivered_at, read_at, edited_at, metadata
                    FROM messages WHERE id = ?
                """, (message_id,))
                
                row = cursor.fetchone()
                if not row or row['sender_id'] != user_id:
                    logger.warning(f"User {user_id} cannot delete message {message_id}")
                    return False
                
                # Load metadata for attachment cleanup before delete
                metadata = None
                try:
                    meta_row = conn.execute(
                        "SELECT metadata FROM messages WHERE id = ?",
                        (message_id,)
                    ).fetchone()
                    if meta_row and meta_row['metadata']:
                        metadata = json.loads(meta_row['metadata'])
                except Exception:
                    metadata = None

                # Delete the message
                cursor = conn.execute("""
                    DELETE FROM messages WHERE id = ?
                """, (message_id,))
                
                success = cast(int, cursor.rowcount) > 0
                conn.commit()
                
                if success:
                    logger.info(f"Deleted message {message_id}")
                    deleted_message = Message(
                        id=message_id,
                        sender_id=row['sender_id'],
                        recipient_id=row['recipient_id'],
                        content=self.data_encryptor.decrypt(row['content']) if self.data_encryptor and self.data_encryptor.is_enabled else row['content'],
                        message_type=MessageType(row['message_type']),
                        status=MessageStatus.READ if row['read_at'] else (
                            MessageStatus.DELIVERED if row['delivered_at'] else MessageStatus.SENT
                        ),
                        created_at=datetime.fromisoformat(row['created_at']),
                        metadata=json.loads(row['metadata']) if row['metadata'] else None,
                        delivered_at=datetime.fromisoformat(row['delivered_at']) if row['delivered_at'] else None,
                        read_at=datetime.fromisoformat(row['read_at']) if row['read_at'] else None,
                        edited_at=datetime.fromisoformat(row['edited_at']) if row['edited_at'] else None,
                    )
                    self._emit_dm_event(
                        event_type=EVENT_DM_MESSAGE_DELETED,
                        message=deleted_message,
                        dedupe_key=f"{EVENT_DM_MESSAGE_DELETED}:{message_id}",
                    )
                    # Best-effort attachment cleanup (only if unreferenced)
                    if file_manager and metadata and metadata.get('attachments'):
                        for att in metadata.get('attachments') or []:
                            if not isinstance(att, dict):
                                continue
                            file_id = att.get('id') or att.get('file_id')
                            if not file_id:
                                continue
                            try:
                                file_info = file_manager.get_file(file_id)
                                if not file_info or file_info.uploaded_by != user_id:
                                    continue
                                if file_manager.is_file_referenced(file_id, exclude_message_id=message_id):
                                    continue
                                file_manager.delete_file(file_id, user_id)
                            except Exception:
                                continue
                
                return success
                
        except Exception as e:
            logger.error(f"Failed to delete message: {e}")
            return False

    def update_message(self, message_id: str, user_id: str, content: str,
                       message_type: Optional[MessageType] = None,
                       metadata: Optional[Dict[str, Any]] = None,
                       allow_admin: bool = False,
                       edited_at: Optional[str] = None) -> bool:
        """Update a message (only sender can edit, unless admin override)."""
        try:
            with self.db.get_connection() as conn:
                row = conn.execute(
                    "SELECT sender_id, message_type, metadata FROM messages WHERE id = ?",
                    (message_id,)
                ).fetchone()
                if not row:
                    return False

                if row['sender_id'] != user_id and not allow_admin:
                    logger.warning(f"User {user_id} cannot edit message {message_id}")
                    return False

                # Preserve existing message_type/metadata if not provided
                final_message_type = message_type.value if message_type else row['message_type']
                existing_meta = None
                if row['metadata']:
                    try:
                        existing_meta = json.loads(row['metadata'])
                    except Exception:
                        existing_meta = None
                final_metadata = existing_meta if metadata is None else metadata

                stored_content = content
                if self.data_encryptor and self.data_encryptor.is_enabled:
                    stored_content = self.data_encryptor.encrypt(content)

                edited_at_db = None
                if edited_at:
                    try:
                        parsed = datetime.fromisoformat(str(edited_at).replace('Z', '+00:00'))
                        edited_at_db = parsed.strftime('%Y-%m-%d %H:%M:%S')
                    except Exception:
                        edited_at_db = None
                if edited_at_db is None:
                    edited_at_db = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')

                conn.execute(
                    "UPDATE messages SET content = ?, message_type = ?, metadata = ?, edited_at = ? WHERE id = ?",
                    (
                        stored_content,
                        final_message_type,
                        json.dumps(final_metadata) if final_metadata else None,
                        edited_at_db,
                        message_id,
                    )
                )
                conn.commit()

                updated_message = Message(
                    id=message_id,
                    sender_id=row['sender_id'],
                    recipient_id=None,
                    content=content,
                    message_type=MessageType(final_message_type),
                    status=MessageStatus.DELIVERED,
                    created_at=datetime.now(timezone.utc),
                    metadata=final_metadata,
                    edited_at=datetime.fromisoformat(edited_at_db.replace(' ', 'T') + '+00:00'),
                )
                recipient_row = conn.execute(
                    "SELECT recipient_id, created_at, delivered_at, read_at FROM messages WHERE id = ?",
                    (message_id,),
                ).fetchone()
                if recipient_row:
                    updated_message.recipient_id = recipient_row['recipient_id']
                    try:
                        updated_message.created_at = datetime.fromisoformat(recipient_row['created_at'])
                    except Exception:
                        pass
                    try:
                        updated_message.delivered_at = datetime.fromisoformat(recipient_row['delivered_at']) if recipient_row['delivered_at'] else None
                    except Exception:
                        updated_message.delivered_at = None
                    try:
                        updated_message.read_at = datetime.fromisoformat(recipient_row['read_at']) if recipient_row['read_at'] else None
                    except Exception:
                        updated_message.read_at = None

                self._emit_dm_event(
                    event_type=EVENT_DM_MESSAGE_EDITED,
                    message=updated_message,
                    dedupe_key=f"{EVENT_DM_MESSAGE_EDITED}:{message_id}:{edited_at_db}",
                    created_at=edited_at_db,
                )
                logger.info(f"Updated message {message_id}")
                return True
        except Exception as e:
            logger.error(f"Failed to update message {message_id}: {e}")
            return False
    
    def broadcast_message(self, sender_id: str, content: str, 
                         message_type: MessageType = MessageType.TEXT,
                         metadata: Optional[Dict[str, Any]] = None) -> Optional[Message]:
        """Create and send a broadcast message to all users."""
        try:
            message = self.create_message(
                sender_id, content, None, message_type, metadata
            )
            
            if message and self.send_message(message):
                logger.info(f"Broadcast message {message.id} sent")
                return message
            
            return None
            
        except Exception as e:
            logger.error(f"Failed to broadcast message: {e}")
            return None
    
    def get_message_statistics(self, user_id: str) -> Dict[str, int]:
        """Get message statistics for a user."""
        try:
            with self.db.get_connection() as conn:
                cursor = conn.execute("""
                    SELECT 
                        COUNT(*) as total_messages,
                        SUM(CASE WHEN sender_id = ? THEN 1 ELSE 0 END) as sent_messages,
                        SUM(CASE WHEN recipient_id = ? THEN 1 ELSE 0 END) as received_messages,
                        SUM(CASE WHEN read_at IS NOT NULL AND recipient_id = ? THEN 1 ELSE 0 END) as read_messages,
                        COUNT(DISTINCT CASE WHEN sender_id = ? THEN recipient_id ELSE sender_id END) as unique_contacts
                    FROM messages 
                    WHERE sender_id = ? OR recipient_id = ?
                """, (user_id, user_id, user_id, user_id, user_id, user_id))
                
                row = cursor.fetchone()
                return {
                    'total_messages': row['total_messages'] or 0,
                    'sent_messages': row['sent_messages'] or 0,
                    'received_messages': row['received_messages'] or 0,
                    'read_messages': row['read_messages'] or 0,
                    'unique_contacts': row['unique_contacts'] or 0
                }
                
        except Exception as e:
            logger.error(f"Failed to get message statistics: {e}")
            return {
                'total_messages': 0, 'sent_messages': 0, 'received_messages': 0,
                'read_messages': 0, 'unique_contacts': 0
            }
    
    def search_messages(self, user_id: str, query: str, limit: int = 20) -> List[Message]:
        """Search messages by content."""
        try:
            clean_query = str(query or '').strip()
            if not clean_query:
                return []
            query_folded = clean_query.casefold()
            target_limit = max(int(limit or 20), 1)
            fetch_limit = max(target_limit * 25, 400)
            offset = 0
            with self.db.get_connection() as conn:
                messages = []
                while len(messages) < target_limit:
                    cursor = conn.execute("""
                        SELECT m.*, u.username as sender_username 
                        FROM messages m
                        LEFT JOIN users u ON m.sender_id = u.id
                        WHERE (
                            m.sender_id = ?
                            OR m.recipient_id = ?
                            OR m.recipient_id IS NULL
                            OR EXISTS (
                                SELECT 1
                                FROM json_each(
                                    CASE WHEN json_valid(m.metadata) THEN m.metadata ELSE '{}' END,
                                    '$.group_members'
                                ) gm
                                WHERE CAST(gm.value AS TEXT) = ?
                            )
                        )
                        ORDER BY m.created_at DESC
                        LIMIT ? OFFSET ?
                    """, (user_id, user_id, user_id, fetch_limit, offset))
                    rows = cursor.fetchall()
                    if not rows:
                        break

                    for row in rows:
                        # Decrypt content if encrypted
                        content = row['content']
                        if self.data_encryptor and self.data_encryptor.is_enabled:
                            content = self.data_encryptor.decrypt(content)

                        metadata = json.loads(row['metadata']) if row['metadata'] else None
                        searchable_chunks = [str(content or '')]
                        if isinstance(metadata, dict):
                            for attachment in metadata.get('attachments') or []:
                                if isinstance(attachment, dict):
                                    searchable_chunks.append(str(attachment.get('name') or ''))
                                    searchable_chunks.append(str(attachment.get('type') or ''))
                        if not any(query_folded in chunk.casefold() for chunk in searchable_chunks if chunk):
                            continue

                        message = Message(
                            id=row['id'],
                            sender_id=row['sender_id'],
                            recipient_id=row['recipient_id'],
                            content=content,
                            message_type=MessageType(row['message_type']),
                            status=MessageStatus.DELIVERED,
                            created_at=datetime.fromisoformat(row['created_at']),
                            metadata=metadata,
                            delivered_at=datetime.fromisoformat(row['delivered_at']) if row['delivered_at'] else None,
                            read_at=datetime.fromisoformat(row['read_at']) if row['read_at'] else None,
                            edited_at=datetime.fromisoformat(row['edited_at']) if row['edited_at'] else None
                        )
                        messages.append(message)
                        if len(messages) >= target_limit:
                            break

                    if len(rows) < fetch_limit:
                        break
                    offset += len(rows)

                return messages
                
        except Exception as e:
            logger.error(f"Failed to search messages: {e}")
            return []

    def get_group_conversation(self, user_id: str, group_id: str,
                               limit: int = 100) -> List[Message]:
        """Get conversation for a group DM by group_id."""
        if not group_id:
            return []
        try:
            with self.db.get_connection() as conn:
                cursor = conn.execute("""
                    SELECT m.*, u.username as sender_username
                    FROM messages m
                    LEFT JOIN users u ON m.sender_id = u.id
                    WHERE (
                        m.sender_id = ?
                        OR m.recipient_id = ?
                        OR m.recipient_id LIKE 'group:%'
                        OR EXISTS (
                            SELECT 1
                            FROM json_each(
                                CASE WHEN json_valid(m.metadata) THEN m.metadata ELSE '{}' END,
                                '$.group_members'
                            ) gm
                            WHERE CAST(gm.value AS TEXT) = ?
                        )
                    )
                    ORDER BY m.created_at ASC
                """, (user_id, user_id, user_id))

                messages = []
                requested_group_id = str(group_id or '').strip()
                target_aliases: set[str] = {requested_group_id}
                target_canonical_keys: set[str] = set()
                decoded_rows: list[tuple[Any, Any, Optional[dict[str, Any]], list[str], set[str], Optional[str]]] = []

                for row in cursor.fetchall():
                    content = row['content']
                    if self.data_encryptor and self.data_encryptor.is_enabled:
                        content = self.data_encryptor.decrypt(content)

                    meta = json.loads(row['metadata']) if row['metadata'] else None
                    metadata = meta if isinstance(meta, dict) else {}
                    row_group_members = [
                        str(member_id).strip()
                        for member_id in (metadata.get('group_members') or [])
                        if str(member_id).strip()
                    ]
                    # Determine whether this row is a group-targeted message so we
                    # can apply the correct membership guard.  We check the recipient
                    # prefix and the group_id metadata field before the aliases set is
                    # built, because the SQL WHERE clause uses an overly broad
                    # `recipient_id LIKE 'group:%'` predicate that would otherwise
                    # allow a non-member to read group messages that have no
                    # group_members list (e.g. legacy or malformed rows).
                    _rcp_early = str(row['recipient_id'] or '').strip()
                    _gid_early = str(metadata.get('group_id') or '').strip()
                    _is_group_msg = _rcp_early.startswith('group:') or bool(_gid_early)
                    if _is_group_msg and (not row_group_members or user_id not in row_group_members):
                        # Group message: only the original sender may see it when the
                        # membership list is absent or does not include this user.
                        if row['sender_id'] != user_id:
                            continue
                    elif row_group_members and user_id not in row_group_members:
                        continue

                    row_aliases: set[str] = set()
                    row_group_id = str(metadata.get('group_id') or '').strip()
                    if row_group_id:
                        row_aliases.add(row_group_id)
                    row_recipient_id = str(row['recipient_id'] or '').strip()
                    if row_recipient_id.startswith('group:'):
                        row_aliases.add(row_recipient_id)
                    row_canonical_key = compute_group_id(row_group_members) if row_group_members else None
                    if row_canonical_key:
                        row_aliases.add(row_canonical_key)

                    decoded_rows.append((row, content, metadata or None, row_group_members, row_aliases, row_canonical_key))

                    if requested_group_id in row_aliases and row_canonical_key:
                        target_canonical_keys.add(row_canonical_key)

                if requested_group_id.startswith('group:'):
                    target_canonical_keys.add(requested_group_id)

                for row, content, meta, row_group_members, row_aliases, row_canonical_key in decoded_rows:
                    if not row_aliases and not row_group_members:
                        continue
                    if not row_aliases.intersection(target_aliases) and (
                        not row_canonical_key or row_canonical_key not in target_canonical_keys
                    ):
                        continue

                    message = Message(
                        id=row['id'],
                        sender_id=row['sender_id'],
                        recipient_id=row['recipient_id'],
                        content=content,
                        message_type=MessageType(row['message_type']),
                        status=MessageStatus.DELIVERED,
                        created_at=datetime.fromisoformat(row['created_at']),
                        metadata=meta,
                        delivered_at=datetime.fromisoformat(row['delivered_at']) if row['delivered_at'] else None,
                        read_at=datetime.fromisoformat(row['read_at']) if row['read_at'] else None,
                        edited_at=datetime.fromisoformat(row['edited_at']) if row['edited_at'] else None
                    )
                    messages.append(message)

                if limit and len(messages) > limit:
                    return messages[-limit:]
                return messages
        except Exception as e:
            logger.error(f"Failed to get group conversation {group_id}: {e}")
            return []
