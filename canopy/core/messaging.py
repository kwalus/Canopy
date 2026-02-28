"""
Core messaging system for Canopy local communication.

Handles message creation, delivery, and management for local network communication
with support for different message types and encryption.

Author: Konrad Walus (architecture, design, and direction)
Project: Canopy - Local Mesh Communication
License: Apache 2.0
Development: AI-assisted implementation (Claude, Codex, GitHub Copilot, Cursor IDE, Ollama)
"""

import logging
import secrets
import json
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any, Union, cast
from dataclasses import dataclass, asdict
from enum import Enum

from .database import DatabaseManager
from ..security.api_keys import ApiKeyManager, Permission

logger = logging.getLogger(__name__)


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
            logger.debug(f"Message {self.id}: No attachments found (metadata: {self.metadata})")
            
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
        self.max_message_length = 4096  # 4KB for text messages
        self.max_broadcast_recipients = 100
    
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
                    WHERE (m.recipient_id = ? OR m.recipient_id IS NULL OR m.sender_id = ?)
                """
                params: List[Any] = [user_id, user_id]
                
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
                    WHERE id = ? AND (recipient_id = ? OR recipient_id IS NULL)
                """, (message_id, user_id))
                
                success = cast(int, cursor.rowcount) > 0
                conn.commit()
                
                if success:
                    logger.info(f"Marked message {message_id} as read by {user_id}")
                
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
                    SELECT sender_id FROM messages WHERE id = ?
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
            with self.db.get_connection() as conn:
                cursor = conn.execute("""
                    SELECT m.*, u.username as sender_username 
                    FROM messages m
                    LEFT JOIN users u ON m.sender_id = u.id
                    WHERE (m.sender_id = ? OR m.recipient_id = ? OR m.recipient_id IS NULL)
                    AND m.content LIKE ?
                    ORDER BY m.created_at DESC
                    LIMIT ?
                """, (user_id, user_id, f"%{query}%", limit))
                
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
                    WHERE (m.sender_id = ? OR m.recipient_id = ? OR m.recipient_id = ?)
                    AND (m.recipient_id = ? OR json_extract(m.metadata, '$.group_id') = ?)
                    ORDER BY m.created_at ASC
                    LIMIT ?
                """, (user_id, user_id, group_id, group_id, group_id, limit))

                messages = []
                for row in cursor.fetchall():
                    content = row['content']
                    if self.data_encryptor and self.data_encryptor.is_enabled:
                        content = self.data_encryptor.decrypt(content)

                    meta = json.loads(row['metadata']) if row['metadata'] else None
                    # Extra safety: if metadata has group_members, ensure user is a member
                    if meta and isinstance(meta, dict) and meta.get('group_members'):
                        try:
                            if user_id not in meta.get('group_members', []):
                                continue
                        except Exception:
                            pass

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

                return messages
        except Exception as e:
            logger.error(f"Failed to get group conversation {group_id}: {e}")
            return []
