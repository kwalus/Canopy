"""
Message routing for Canopy P2P network.

Handles routing messages between peers, including direct messages,
broadcasts, and multi-hop routing.

Author: Konrad Walus (architecture, design, and direction)
Project: Canopy - Local Mesh Communication
License: Apache 2.0
Development: AI-assisted implementation (Claude, Codex, GitHub Copilot, Cursor IDE, Ollama)
"""

import logging
import secrets
import time
import json
from collections import OrderedDict
from typing import Dict, List, Optional, Any, Set, cast
from enum import Enum
from dataclasses import dataclass, asdict
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

logger = logging.getLogger('canopy.network.routing')

# Maximum messages queued for a single offline peer.
# Prevents a misbehaving or permanently-offline peer from exhausting RAM.
MAX_PENDING_PER_PEER = 500


class MessageType(Enum):
    """Types of P2P messages."""
    # Communication
    DIRECT_MESSAGE = "direct_message"
    BROADCAST = "broadcast"
    CHANNEL_MESSAGE = "channel_message"
    
    # Channel synchronization
    CHANNEL_ANNOUNCE = "channel_announce"   # "I created/have this channel"
    CHANNEL_JOIN = "channel_join"           # "I want to join this channel"
    CHANNEL_SYNC = "channel_sync"          # Bulk sync on initial connection
    
    # System
    PEER_ANNOUNCEMENT = "peer_announcement"
    DELETE_SIGNAL = "delete_signal"
    TRUST_UPDATE = "trust_update"
    
    # Synchronization
    SYNC_REQUEST = "sync_request"
    SYNC_RESPONSE = "sync_response"
    
    # Message catch-up (missed-message recovery on reconnect)
    CHANNEL_CATCHUP_REQUEST = "channel_catchup_request"
    CHANNEL_CATCHUP_RESPONSE = "channel_catchup_response"
    
    # Profile sharing
    PROFILE_SYNC = "profile_sync"       # Exchange profile cards on connect
    PROFILE_UPDATE = "profile_update"   # Broadcast profile change to peers
    
    # Private channel membership
    MEMBER_SYNC = "member_sync"                  # Add/remove member on remote peer
    PRIVATE_CHANNEL_INVITE = "private_channel_invite"  # Invite peer to private channel

    # Connection brokering and relay
    BROKER_REQUEST = "broker_request"   # Ask intermediary to help connect
    BROKER_INTRO = "broker_intro"       # Intermediary introduces requester
    RELAY_OFFER = "relay_offer"         # Intermediary offers to relay traffic
    
    # Feed and interaction propagation
    FEED_POST = "feed_post"             # Broadcast a new feed post to peers
    INTERACTION = "interaction"          # Like/unlike propagation
    
    # Voice (future)
    VOICE_OFFER = "voice_offer"
    VOICE_ANSWER = "voice_answer"
    VOICE_ICE = "voice_ice"


@dataclass
class P2PMessage:
    """
    Represents a P2P network message.
    
    All messages are signed by the sender and optionally encrypted.
    """
    id: str
    type: MessageType
    from_peer: str
    to_peer: Optional[str]  # None for broadcast
    timestamp: float
    ttl: int  # Time-to-live for routing
    signature: Optional[str] = None
    encrypted_payload: Optional[str] = None
    payload: Optional[Dict[str, Any]] = None  # Decrypted payload
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            'id': self.id,
            'type': self.type.value,
            'from_peer': self.from_peer,
            'to_peer': self.to_peer,
            'timestamp': self.timestamp,
            'ttl': self.ttl,
            'signature': self.signature,
            'encrypted_payload': self.encrypted_payload,
            'payload': self.payload if not self.encrypted_payload else None
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'P2PMessage':
        """Create message from dictionary."""
        return cls(
            id=data['id'],
            type=MessageType(data['type']),
            from_peer=data['from_peer'],
            to_peer=data.get('to_peer'),
            timestamp=data['timestamp'],
            ttl=data['ttl'],
            signature=data.get('signature'),
            encrypted_payload=data.get('encrypted_payload'),
            payload=data.get('payload')
        )
    
    def is_expired(self) -> bool:
        """Check if message has expired."""
        return self.ttl <= 0
    
    def decrement_ttl(self) -> None:
        """Decrement TTL for routing."""
        self.ttl -= 1


class MessageRouter:
    """Routes messages between peers in the P2P network."""
    
    def __init__(self, local_peer_id: str, identity_manager: Any, connection_manager: Any):
        """
        Initialize message router.
        
        Args:
            local_peer_id: This peer's ID
            identity_manager: IdentityManager for signing/encryption
            connection_manager: ConnectionManager for sending messages
        """
        self.local_peer_id = local_peer_id
        self.identity_manager = identity_manager
        self.connection_manager = connection_manager
        
        # Message tracking — OrderedDict gives true insertion-order eviction
        # (the previous set-based trim was not LRU because sets are unordered)
        self.seen_messages: OrderedDict = OrderedDict()
        self.max_seen_messages = 10000
        
        # Per-peer rate limiting (stricter sliding window to prevent DoS)
        # for normal messaging traffic.
        self._peer_msg_counts: Dict[str, list] = {}  # peer -> [count, window_start]
        self._peer_rate_limit = 50  # Reduced from 100: max messages per 60s window per peer
        self._peer_burst_limit = 10  # Max messages in 5s burst per peer
        self._peer_burst_window = 5  # 5 second burst window
        self._peer_burst_counts: Dict[str, list] = {}  # peer -> [(timestamp, count)]

        # Catch-up and bulk sync flows can legitimately be much burstier than
        # interactive chat traffic. Keep separate counters so bulk sync doesn't
        # throttle normal message handling or trigger false-positive warnings.
        self._peer_sync_msg_counts: Dict[str, list] = {}  # peer -> [count, window_start]
        self._peer_sync_rate_limit = 500  # Max sync msgs per 60s per peer
        self._peer_sync_burst_limit = 120  # Max sync msgs in 5s burst per peer
        self._peer_sync_burst_window = 5  # 5 second burst window
        self._peer_sync_burst_counts: Dict[str, list] = {}  # peer -> [(timestamp, count)]
        self._high_volume_sync_types: Set[MessageType] = {
            MessageType.CHANNEL_CATCHUP_RESPONSE,
            MessageType.CHANNEL_SYNC,
            MessageType.PEER_ANNOUNCEMENT,
            MessageType.PROFILE_SYNC,
            MessageType.PROFILE_UPDATE,
        }

        # Avoid log flooding when a peer repeatedly exceeds limits.
        self._rate_limit_warn_cooldown_s = 15.0
        self._rate_limit_warned_at: Dict[str, float] = {}
        
        # Routing table (peer_id -> next_hop_peer_id)
        self.routing_table: Dict[str, str] = {}
        
        # Store-and-forward queue for offline peers
        self.pending_messages: Dict[str, List[P2PMessage]] = {}
        
        # Application callbacks
        self.on_channel_message: Optional[Any] = None
        self.on_channel_announce: Optional[Any] = None
        self.on_channel_sync: Optional[Any] = None
        self.on_catchup_request: Optional[Any] = None
        self.on_catchup_response: Optional[Any] = None
        self.on_member_sync: Optional[Any] = None
        self.on_private_channel_invite: Optional[Any] = None
        self.on_profile_sync: Optional[Any] = None
        self.on_peer_announcement: Optional[Any] = None
        self.on_delete_signal: Optional[Any] = None
        self.on_feed_post: Optional[Any] = None
        self.on_interaction: Optional[Any] = None
        self.on_direct_message: Optional[Any] = None
        self.on_broker_request: Optional[Any] = None
        self.on_broker_intro: Optional[Any] = None
        self.on_relay_offer: Optional[Any] = None
        # Fires only for locally-delivered, user-facing activity messages.
        # Expected signature: callback(event_dict)
        self.on_activity_event: Optional[Any] = None
        
        logger.info(f"Initialized MessageRouter for {local_peer_id}")
    
    def create_message(self, message_type: MessageType, to_peer: Optional[str], 
                      payload: Dict[str, Any], ttl: int = 5) -> P2PMessage:
        """
        Create a new P2P message.
        
        Args:
            message_type: Type of message
            to_peer: Recipient peer ID (None for broadcast)
            payload: Message payload
            ttl: Time-to-live for routing
            
        Returns:
            P2PMessage instance
        """
        message = P2PMessage(
            id=secrets.token_hex(16),
            type=message_type,
            from_peer=self.local_peer_id,
            to_peer=to_peer,
            timestamp=time.time(),
            ttl=ttl,
            payload=payload
        )
        
        logger.debug(f"Created message {message.id}: {message_type.value}")
        return message

    def _should_log_rate_limit_warning(self, peer_id: str, scope: str,
                                       now: Optional[float] = None) -> bool:
        ts = now if now is not None else time.time()
        key = f"{scope}:{peer_id}"
        last = self._rate_limit_warned_at.get(key, 0.0)
        if ts - last >= self._rate_limit_warn_cooldown_s:
            self._rate_limit_warned_at[key] = ts
            return True
        return False
    
    def sign_message(self, message: P2PMessage) -> None:
        """
        Sign a message with local peer's Ed25519 key.

        Signs over whichever payload is present: plaintext ``payload``
        for unencrypted messages, or ``encrypted_payload`` for encrypted
        ones.  This allows the call-site to sign either before or after
        encryption and still produce a verifiable signature.
        
        Args:
            message: Message to sign
        """
        # Serialize message data for signing — use the payload that
        # is currently populated.  For encrypted messages payload is
        # None and encrypted_payload holds the ciphertext hex.
        sign_data = {
            'id': message.id,
            'type': message.type.value,
            'from_peer': message.from_peer,
            'to_peer': message.to_peer,
            'timestamp': message.timestamp,
            'payload': message.payload,
            'encrypted_payload': message.encrypted_payload,
        }
        
        sign_bytes = json.dumps(sign_data, sort_keys=True).encode('utf-8')
        
        # Sign with Ed25519
        signature = self.identity_manager.local_identity.sign(sign_bytes)
        message.signature = signature.hex()
        
        logger.debug(f"Signed message {message.id}")
    
    def verify_message(self, message: P2PMessage) -> bool:
        """
        Verify message signature.

        Matches the sign_data layout used by sign_message: includes
        both ``payload`` and ``encrypted_payload`` so the signature
        is valid regardless of whether it was signed before or after
        encryption.
        
        Args:
            message: Message to verify
            
        Returns:
            True if signature is valid
        """
        if not message.signature:
            logger.warning(f"Message {message.id} has no signature")
            return False
        
        try:
            # Get sender's identity
            sender_identity = self.identity_manager.get_peer(message.from_peer)
            if not sender_identity:
                known = list(self.identity_manager.known_peers.keys())
                logger.warning(
                    f"Unknown peer: {message.from_peer} "
                    f"(known peers: {known})")
                return False
            
            # Reconstruct signed data (must mirror sign_message exactly)
            sign_data = {
                'id': message.id,
                'type': message.type.value,
                'from_peer': message.from_peer,
                'to_peer': message.to_peer,
                'timestamp': message.timestamp,
                'payload': message.payload,
                'encrypted_payload': message.encrypted_payload,
            }
            
            sign_bytes = json.dumps(sign_data, sort_keys=True).encode('utf-8')
            signature = bytes.fromhex(message.signature)
            
            # Verify signature
            return bool(sender_identity.verify(sign_bytes, signature))
            
        except Exception as e:
            logger.error(f"Signature verification failed: {e}", exc_info=True)
            return False
    
    def encrypt_message(self, message: P2PMessage, recipient_peer_id: str) -> None:
        """
        Encrypt message payload for recipient.
        
        Args:
            message: Message to encrypt
            recipient_peer_id: Recipient's peer ID
        """
        if not message.payload:
            return
        
        try:
            # Get recipient's identity
            recipient_identity = self.identity_manager.get_peer(recipient_peer_id)
            if not recipient_identity:
                logger.warning(f"Cannot encrypt: unknown peer {recipient_peer_id}")
                return
            
            # Derive shared secret
            shared_secret = self.identity_manager.local_identity.derive_shared_secret(
                recipient_identity.x25519_public_key
            )
            
            # Encrypt payload
            cipher = ChaCha20Poly1305(shared_secret)
            nonce = secrets.token_bytes(12)
            
            payload_bytes = json.dumps(message.payload).encode('utf-8')
            ciphertext = cipher.encrypt(nonce, payload_bytes, None)
            
            # Store encrypted payload
            message.encrypted_payload = (nonce + ciphertext).hex()
            message.payload = None  # Clear plaintext
            
            logger.debug(f"Encrypted message {message.id} for {recipient_peer_id}")
            
        except Exception as e:
            logger.error(f"Encryption failed: {e}", exc_info=True)
    
    def decrypt_message(self, message: P2PMessage) -> bool:
        """
        Decrypt message payload.
        
        Args:
            message: Message to decrypt
            
        Returns:
            True if decryption successful
        """
        if not message.encrypted_payload:
            return True  # Already decrypted or not encrypted
        
        try:
            # Get sender's identity
            sender_identity = self.identity_manager.get_peer(message.from_peer)
            if not sender_identity:
                logger.warning(f"Cannot decrypt: unknown peer {message.from_peer}")
                return False
            
            # Derive shared secret
            shared_secret = self.identity_manager.local_identity.derive_shared_secret(
                sender_identity.x25519_public_key
            )
            
            # Decrypt payload
            encrypted_data = bytes.fromhex(message.encrypted_payload)
            nonce = encrypted_data[:12]
            ciphertext = encrypted_data[12:]
            
            cipher = ChaCha20Poly1305(shared_secret)
            plaintext = cipher.decrypt(nonce, ciphertext, None)
            
            # Restore payload
            message.payload = json.loads(plaintext.decode('utf-8'))
            message.encrypted_payload = None
            
            logger.debug(f"Decrypted message {message.id}")
            return True
            
        except Exception as e:
            logger.error(f"Decryption failed: {e}", exc_info=True)
            return False
    
    async def route_message(self, message: P2PMessage) -> bool:
        """
        Route a message to its destination.
        
        Args:
            message: Message to route
            
        Returns:
            True if routing successful
        """
        # Check if we've seen this message (prevent loops)
        if message.id in self.seen_messages:
            logger.debug(f"Already seen message {message.id}, skipping")
            return False
        
        # Per-peer rate limiting for incoming messages
        if message.from_peer and message.from_peer != self.local_peer_id:
            now = time.time()

            # Prune stale entries from peers not seen in 5 minutes to prevent
            # unbounded growth in regular + sync limiter maps.
            cutoff = now - 300
            stale_regular = [
                p for p, entry in self._peer_msg_counts.items()
                if isinstance(entry, list) and len(entry) >= 2 and entry[1] < cutoff
            ]
            for p in stale_regular:
                self._peer_msg_counts.pop(p, None)
                self._peer_burst_counts.pop(p, None)
                self._rate_limit_warned_at.pop(f"burst:{p}", None)
                self._rate_limit_warned_at.pop(f"sustained:{p}", None)

            stale_sync = [
                p for p, entry in self._peer_sync_msg_counts.items()
                if isinstance(entry, list) and len(entry) >= 2 and entry[1] < cutoff
            ]
            for p in stale_sync:
                self._peer_sync_msg_counts.pop(p, None)
                self._peer_sync_burst_counts.pop(p, None)
                self._rate_limit_warned_at.pop(f"sync-burst:{p}", None)
                self._rate_limit_warned_at.pop(f"sync-sustained:{p}", None)

            # Choose limiter profile by message type.
            is_sync_heavy = message.type in self._high_volume_sync_types
            if is_sync_heavy:
                msg_counts = self._peer_sync_msg_counts
                burst_map = self._peer_sync_burst_counts
                burst_window = self._peer_sync_burst_window
                burst_limit = self._peer_sync_burst_limit
                rate_limit = self._peer_sync_rate_limit
                burst_scope = "sync-burst"
                sustained_scope = "sync-sustained"
            else:
                msg_counts = self._peer_msg_counts
                burst_map = self._peer_burst_counts
                burst_window = self._peer_burst_window
                burst_limit = self._peer_burst_limit
                rate_limit = self._peer_rate_limit
                burst_scope = "burst"
                sustained_scope = "sustained"

            # Check burst rate (short window)
            if message.from_peer not in burst_map:
                burst_map[message.from_peer] = []

            burst_counts = burst_map[message.from_peer]
            burst_counts = [(ts, cnt) for ts, cnt in burst_counts if now - ts < burst_window]
            burst_count = sum(cnt for _, cnt in burst_counts)

            if burst_count >= burst_limit:
                if self._should_log_rate_limit_warning(message.from_peer, burst_scope, now=now):
                    logger.warning(
                        f"P2P {burst_scope} rate limit exceeded for peer "
                        f"{message.from_peer}: {burst_count} msgs in {burst_window}s "
                        f"(type={message.type.value})"
                    )
                return False

            # Add current message to burst count
            burst_counts.append((now, 1))
            burst_map[message.from_peer] = burst_counts

            # Check sustained rate (60s window)
            entry = msg_counts.get(message.from_peer)
            if entry is None:
                msg_counts[message.from_peer] = [1, now]
            else:
                if now - entry[1] > 60:
                    entry[0] = 1
                    entry[1] = now
                else:
                    entry[0] += 1
                    if entry[0] > rate_limit:
                        if self._should_log_rate_limit_warning(message.from_peer, sustained_scope, now=now):
                            logger.warning(
                                f"P2P {sustained_scope} rate limit exceeded for peer "
                                f"{message.from_peer}: {entry[0]} msgs in 60s "
                                f"(type={message.type.value})"
                            )
                        return False
        
        # Add to seen messages (OrderedDict maintains insertion order)
        self.seen_messages[message.id] = True
        
        # Trim seen messages if too large — evict oldest entries first
        if len(self.seen_messages) > self.max_seen_messages:
            trim_count = len(self.seen_messages) - self.max_seen_messages // 2
            for _ in range(trim_count):
                self.seen_messages.popitem(last=False)  # FIFO: remove oldest
        
        # Check TTL
        if message.is_expired():
            logger.debug(f"Message {message.id} expired (TTL=0)")
            return False
        
        # Decrement TTL
        message.decrement_ttl()
        
        # Route based on message type
        if message.to_peer is None:
            # Broadcast message
            return await self._route_broadcast(message)
        elif message.to_peer == self.local_peer_id:
            # Message for us
            return await self._deliver_local(message)
        else:
            # Forward to destination
            return await self._route_to_peer(message)
    
    async def _route_broadcast(self, message: P2PMessage) -> bool:
        """Route broadcast message to all connected peers."""
        connected_peers = self.connection_manager.get_connected_peers()
        
        # Don't send back to sender
        peers_to_send = [p for p in connected_peers if p != message.from_peer]
        
        logger.info(f"Broadcasting {message.type.value} {message.id} to {len(peers_to_send)} peers: {peers_to_send}")
        
        success = False
        for peer_id in peers_to_send:
            sent = await self.connection_manager.send_to_peer(
                peer_id,
                {'type': 'p2p_message', 'message': message.to_dict()}
            )
            if sent:
                logger.info(f"  -> Sent to {peer_id}: OK")
            else:
                logger.warning(f"  -> Sent to {peer_id}: FAILED")
            success = success or sent
        
        # Also deliver to ourselves
        await self._deliver_local(message)
        
        return success
    
    async def _route_to_peer(self, message: P2PMessage) -> bool:
        """Route message to specific peer."""
        target_peer = cast(str, message.to_peer)
        
        # Check if directly connected
        if self.connection_manager.is_connected(target_peer):
            logger.debug(f"Sending message {message.id} directly to {target_peer}")
            return bool(await self.connection_manager.send_to_peer(
                target_peer,
                {'type': 'p2p_message', 'message': message.to_dict()}
            ))
        
        # Check routing table for next hop
        next_hop = self.routing_table.get(target_peer)
        if next_hop and self.connection_manager.is_connected(next_hop):
            logger.debug(f"Forwarding message {message.id} to {target_peer} via {next_hop}")
            return bool(await self.connection_manager.send_to_peer(
                next_hop,
                {'type': 'p2p_message', 'message': message.to_dict()}
            ))
        
        # Store for later delivery (store-and-forward)
        logger.debug(f"Peer {target_peer} not reachable, queueing message {message.id}")
        if target_peer not in self.pending_messages:
            self.pending_messages[target_peer] = []
        queue = self.pending_messages[target_peer]
        if len(queue) >= MAX_PENDING_PER_PEER:
            dropped = queue.pop(0)
            logger.warning(
                f"Pending queue for {target_peer} full ({MAX_PENDING_PER_PEER}); "
                f"dropped oldest message {dropped.id}"
            )
        queue.append(message)
        
        return False
    
    async def _deliver_local(self, message: P2PMessage) -> bool:
        """Deliver message to local application."""
        logger.debug(f"Delivering message {message.id} locally")
        payload = cast(Dict[str, Any], message.payload)
        
        # Decrypt if needed
        if message.encrypted_payload:
            if not self.decrypt_message(message):
                logger.error(f"Failed to decrypt message {message.id}")
                return False
        
        logger.debug(f"Received {message.type.value} message from {message.from_peer}")

        # Emit a lightweight UI-facing activity event for user-level messages.
        # Skip events that originated from this node — the local user doesn't
        # need to be notified about their own posts/messages.
        if (self.on_activity_event
                and message.from_peer != self.local_peer_id
                and message.type in {
                    MessageType.CHANNEL_MESSAGE,
                    MessageType.FEED_POST,
                    MessageType.DIRECT_MESSAGE,
                    MessageType.INTERACTION,
                }):
            try:
                emit_event = True
                preview = ""
                ref: Dict[str, Any] = {}
                if message.type in {MessageType.CHANNEL_MESSAGE, MessageType.FEED_POST, MessageType.DIRECT_MESSAGE}:
                    preview = payload.get('content', '') or ''
                    meta = payload.get('metadata', {}) or {}
                    if message.type == MessageType.CHANNEL_MESSAGE:
                        ref = {
                            'channel_id': meta.get('channel_id'),
                            'message_id': meta.get('message_id'),
                            'user_id': meta.get('user_id'),
                        }
                    elif message.type == MessageType.FEED_POST:
                        ref = {
                            'post_id': meta.get('post_id'),
                            'author_id': meta.get('author_id'),
                        }
                    elif message.type == MessageType.DIRECT_MESSAGE:
                        ref = {
                            'message_id': meta.get('message_id'),
                            'sender_id': meta.get('sender_id'),
                            'recipient_id': meta.get('recipient_id'),
                        }
                elif message.type == MessageType.INTERACTION:
                    meta = payload.get('metadata', {}) or {}
                    action = meta.get('action', 'interaction')
                    item_type = meta.get('item_type', 'item')
                    preview = meta.get('preview') or f"{action} {item_type}"
                    ref = {
                        'item_id': meta.get('item_id'),
                        'item_type': meta.get('item_type'),
                        'action': meta.get('action'),
                        'user_id': meta.get('user_id'),
                    }
                    # Optional poll navigation helpers
                    if meta.get('poll_id'):
                        ref['poll_id'] = meta.get('poll_id')
                    if meta.get('poll_kind'):
                        ref['poll_kind'] = meta.get('poll_kind')
                    if meta.get('channel_id'):
                        ref['channel_id'] = meta.get('channel_id')
                    if action in ('poll_vote', 'mention'):
                        emit_event = False
                preview = preview.strip()
                if len(preview) > 120:
                    preview = preview[:117] + "..."

                if emit_event:
                    self.on_activity_event({
                        'id': message.id,
                        'peer_id': message.from_peer,
                        'kind': message.type.value,
                        'timestamp': message.timestamp,
                        'preview': preview,
                        'ref': ref,
                    })
            except Exception as e:
                logger.debug(f"Activity event callback failed: {e}")
        
        # Dispatch based on message type
        if message.type == MessageType.CHANNEL_MESSAGE and self.on_channel_message:
            try:
                meta = payload.get('metadata', {})
                self.on_channel_message(
                    channel_id=meta.get('channel_id', 'general'),
                    user_id=meta.get('user_id', f'peer_{message.from_peer}'),
                    content=payload.get('content', ''),
                    message_id=meta.get('message_id'),
                    timestamp=meta.get('timestamp'),
                    from_peer=message.from_peer,
                    attachments=meta.get('attachments'),
                    security=meta.get('security'),
                    message_type=meta.get('message_type', 'text'),
                    display_name=meta.get('display_name'),
                    expires_at=meta.get('expires_at'),
                    ttl_seconds=meta.get('ttl_seconds'),
                    ttl_mode=meta.get('ttl_mode'),
                    update_only=bool(meta.get('update_only')),
                    origin_peer=meta.get('origin_peer'),
                    parent_message_id=meta.get('parent_message_id'),
                    edited_at=meta.get('edited_at'),
                )
            except Exception as e:
                logger.error(f"Error delivering channel message locally: {e}", exc_info=True)
        
        elif message.type == MessageType.CHANNEL_ANNOUNCE and self.on_channel_announce:
            try:
                meta = payload.get('metadata', {})
                self.on_channel_announce(
                    channel_id=meta.get('channel_id'),
                    name=meta.get('name', ''),
                    channel_type=meta.get('channel_type', 'public'),
                    description=meta.get('description', ''),
                    created_by_peer=meta.get('created_by_peer', message.from_peer),
                    privacy_mode=meta.get('privacy_mode'),
                    from_peer=message.from_peer,
                    initial_members=meta.get('initial_members'),
                )
            except Exception as e:
                logger.error(f"Error delivering channel announce locally: {e}", exc_info=True)
        
        elif message.type == MessageType.CHANNEL_SYNC and self.on_channel_sync:
            try:
                meta = payload.get('metadata', {})
                self.on_channel_sync(
                    channels=meta.get('channels', []),
                    from_peer=message.from_peer,
                )
            except Exception as e:
                logger.error(f"Error delivering channel sync locally: {e}", exc_info=True)
        
        elif message.type == MessageType.MEMBER_SYNC and self.on_member_sync:
            try:
                meta = payload.get('metadata', {})
                self.on_member_sync(
                    channel_id=meta.get('channel_id'),
                    target_user_id=meta.get('target_user_id'),
                    action=meta.get('action', 'add'),
                    role=meta.get('role', 'member'),
                    channel_name=meta.get('channel_name', ''),
                    channel_type=meta.get('channel_type', 'private'),
                    channel_description=meta.get('channel_description', ''),
                    privacy_mode=meta.get('privacy_mode', 'private'),
                    from_peer=message.from_peer,
                )
            except Exception as e:
                logger.error(f"Error delivering member sync locally: {e}", exc_info=True)

        elif message.type == MessageType.CHANNEL_CATCHUP_REQUEST and self.on_catchup_request:
            try:
                meta = payload.get('metadata', {})
                self.on_catchup_request(
                    channel_timestamps=meta.get('channel_timestamps', {}),
                    from_peer=message.from_peer,
                    feed_latest=meta.get('feed_latest'),
                    circle_entries_latest=meta.get('circle_entries_latest'),
                    circle_votes_latest=meta.get('circle_votes_latest'),
                    circles_latest=meta.get('circles_latest'),
                    tasks_latest=meta.get('tasks_latest'),
                )
            except Exception as e:
                logger.error(f"Error handling catchup request: {e}", exc_info=True)
        
        elif message.type == MessageType.CHANNEL_CATCHUP_RESPONSE and self.on_catchup_response:
            try:
                meta = payload.get('metadata', {})
                self.on_catchup_response(
                    messages=meta.get('messages', []),
                    from_peer=message.from_peer,
                    feed_posts=meta.get('feed_posts', []),
                    circle_entries=meta.get('circle_entries', []),
                    circle_votes=meta.get('circle_votes', []),
                    circles=meta.get('circles', []),
                    tasks=meta.get('tasks', []),
                )
            except Exception as e:
                logger.error(f"Error handling catchup response: {e}", exc_info=True)
        
        elif message.type in (MessageType.PROFILE_SYNC, MessageType.PROFILE_UPDATE) and self.on_profile_sync:
            try:
                meta = payload.get('metadata', {})
                self.on_profile_sync(
                    profile_data=meta.get('profile', {}),
                    from_peer=message.from_peer,
                )
            except Exception as e:
                logger.error(f"Error handling profile sync: {e}", exc_info=True)
        
        elif message.type == MessageType.PEER_ANNOUNCEMENT and self.on_peer_announcement:
            try:
                meta = payload.get('metadata', {})
                self.on_peer_announcement(
                    introduced_peers=meta.get('introduced_peers', []),
                    from_peer=message.from_peer,
                )
            except Exception as e:
                logger.error(f"Error handling peer announcement: {e}", exc_info=True)
        
        elif message.type == MessageType.DELETE_SIGNAL and self.on_delete_signal:
            try:
                meta = payload.get('metadata', {})
                self.on_delete_signal(
                    signal_id=meta.get('signal_id', ''),
                    data_type=meta.get('data_type', ''),
                    data_id=meta.get('data_id', ''),
                    reason=meta.get('reason'),
                    requester_peer=meta.get('requester_peer', message.from_peer),
                    is_ack=meta.get('is_ack', False),
                    ack_status=meta.get('ack_status'),
                    from_peer=message.from_peer,
                )
            except Exception as e:
                logger.error(f"Error handling delete signal: {e}", exc_info=True)
        
        elif message.type == MessageType.FEED_POST and self.on_feed_post:
            try:
                meta = payload.get('metadata', {})
                self.on_feed_post(
                    post_id=meta.get('post_id'),
                    author_id=meta.get('author_id', f'peer_{message.from_peer}'),
                    content=payload.get('content', ''),
                    post_type=meta.get('post_type', 'text'),
                    visibility=meta.get('visibility', 'network'),
                    timestamp=meta.get('timestamp'),
                    metadata=meta.get('metadata'),
                    expires_at=meta.get('expires_at'),
                    ttl_seconds=meta.get('ttl_seconds'),
                    ttl_mode=meta.get('ttl_mode'),
                    display_name=meta.get('display_name'),
                    from_peer=message.from_peer,
                )
            except Exception as e:
                logger.error(f"Error delivering feed post locally: {e}", exc_info=True)
        
        elif message.type == MessageType.INTERACTION and self.on_interaction:
            try:
                meta = payload.get('metadata', {})
                self.on_interaction(
                    item_id=meta.get('item_id'),
                    user_id=meta.get('user_id'),
                    action=meta.get('action', 'like'),
                    item_type=meta.get('item_type', 'post'),
                    display_name=meta.get('display_name'),
                    metadata=meta,
                    from_peer=message.from_peer,
                )
            except Exception as e:
                logger.error(f"Error delivering interaction locally: {e}", exc_info=True)
        
        elif message.type == MessageType.DIRECT_MESSAGE and self.on_direct_message:
            try:
                meta = payload.get('metadata', {})
                self.on_direct_message(
                    sender_id=meta.get('sender_id', f'peer_{message.from_peer}'),
                    recipient_id=meta.get('recipient_id', ''),
                    content=payload.get('content', ''),
                    message_id=meta.get('message_id'),
                    timestamp=meta.get('timestamp'),
                    display_name=meta.get('display_name'),
                    metadata=meta.get('metadata'),
                    update_only=meta.get('update_only'),
                    edited_at=meta.get('edited_at'),
                    from_peer=message.from_peer,
                )
            except Exception as e:
                logger.error(f"Error delivering direct message locally: {e}", exc_info=True)
        
        elif message.type == MessageType.BROKER_REQUEST and self.on_broker_request:
            try:
                meta = payload.get('metadata', {})
                self.on_broker_request(
                    target_peer=meta.get('target_peer', ''),
                    requester_endpoints=meta.get('requester_endpoints', []),
                    requester_keys=meta.get('requester_keys', {}),
                    from_peer=message.from_peer,
                )
            except Exception as e:
                logger.error(f"Error handling broker request: {e}", exc_info=True)
        
        elif message.type == MessageType.BROKER_INTRO and self.on_broker_intro:
            try:
                meta = payload.get('metadata', {})
                self.on_broker_intro(
                    requester_peer_id=meta.get('requester_peer_id', ''),
                    requester_endpoints=meta.get('requester_endpoints', []),
                    requester_keys=meta.get('requester_keys', {}),
                    from_peer=message.from_peer,
                )
            except Exception as e:
                logger.error(f"Error handling broker intro: {e}", exc_info=True)
        
        elif message.type == MessageType.RELAY_OFFER and self.on_relay_offer:
            try:
                meta = payload.get('metadata', {})
                self.on_relay_offer(
                    relay_peer=message.from_peer,
                    target_peer=meta.get('target_peer', ''),
                )
            except Exception as e:
                logger.error(f"Error handling relay offer: {e}", exc_info=True)
        
        return True
    
    async def send_direct_message(self, to_peer: str, content: str, 
                                 metadata: Optional[Dict] = None) -> bool:
        """
        Send a direct message to a peer.
        
        Args:
            to_peer: Recipient peer ID
            content: Message content
            metadata: Optional metadata
            
        Returns:
            True if sent successfully
        """
        payload = {
            'content': content,
            'metadata': metadata or {}
        }
        
        message = self.create_message(MessageType.DIRECT_MESSAGE, to_peer, payload)
        # Encrypt first, then sign — so the signature covers the
        # ciphertext and can be verified before decryption.
        self.encrypt_message(message, to_peer)
        self.sign_message(message)
        
        return await self.route_message(message)
    
    # TTL for user-content messages (feed, channel, DM, interactions).
    # Kept lower than the default (5) because small meshes rarely exceed
    # 2-3 hops and this reduces redundant re-broadcasts.
    _CONTENT_TTL = 3

    async def send_channel_broadcast(self, content: str, metadata: Dict,
                                      to_peer: Optional[str] = None) -> bool:
        """
        Send a channel message. Broadcasts to all peers by default,
        or targeted to a specific peer if to_peer is provided (for private channels).
        
        Uses CHANNEL_MESSAGE type so receivers know to store it locally.
        """
        payload = {
            'content': content,
            'metadata': metadata or {}
        }

        message = self.create_message(MessageType.CHANNEL_MESSAGE, to_peer, payload,
                                      ttl=self._CONTENT_TTL)
        self.sign_message(message)

        if to_peer:
            return await self._route_to_peer(message)
        return await self._route_broadcast(message)

    async def send_feed_post_broadcast(self, content: str, metadata: Dict) -> bool:
        """
        Broadcast a feed post to all peers so they can store and display it.
        Uses FEED_POST type.
        """
        payload = {
            'content': content,
            'metadata': metadata or {}
        }

        message = self.create_message(MessageType.FEED_POST, None, payload,
                                      ttl=self._CONTENT_TTL)
        self.sign_message(message)
        return await self._route_broadcast(message)

    async def send_interaction_broadcast(self, metadata: Dict) -> bool:
        """
        Broadcast an interaction (like/unlike) to all peers.
        Uses INTERACTION type.
        """
        payload = {
            'content': '',
            'metadata': metadata or {}
        }

        message = self.create_message(MessageType.INTERACTION, None, payload,
                                      ttl=self._CONTENT_TTL)
        self.sign_message(message)
        return await self._route_broadcast(message)

    async def send_dm_broadcast(self, content: str, metadata: Dict) -> bool:
        """
        Broadcast a direct message to all peers.
        The receiving peer checks if the recipient is local and stores it.
        Uses DIRECT_MESSAGE type with broadcast routing.
        """
        payload = {
            'content': content,
            'metadata': metadata or {}
        }

        message = self.create_message(MessageType.DIRECT_MESSAGE, None, payload,
                                      ttl=self._CONTENT_TTL)
        self.sign_message(message)
        return await self._route_broadcast(message)

    async def send_channel_announce(self, channel_id: str, name: str,
                                     channel_type: str, description: str,
                                     created_by_peer: str,
                                     privacy_mode: Optional[str] = None,
                                     to_peer: Optional[str] = None,
                                     initial_members: Optional[list[Any]] = None) -> bool:
        """
        Send a channel announcement. Broadcasts to all peers by default,
        or targeted to a specific peer if to_peer is provided (for private channels).
        
        initial_members: list of user_ids that should be added on the receiving peer
        (used for private channel targeted announces).
        """
        metadata: Dict[str, Any] = {
            'type': 'channel_announce',
            'channel_id': channel_id,
            'name': name,
            'channel_type': channel_type,
            'description': description,
            'created_by_peer': created_by_peer,
            'privacy_mode': privacy_mode,
        }
        if initial_members:
            metadata['initial_members'] = initial_members

        payload = {
            'content': '',
            'metadata': metadata,
        }

        message = self.create_message(MessageType.CHANNEL_ANNOUNCE, to_peer, payload)
        self.sign_message(message)
        if to_peer:
            return await self._route_to_peer(message)
        return await self._route_broadcast(message)

    async def send_member_sync(self, to_peer: str, channel_id: str,
                                target_user_id: str, action: str,
                                role: str = 'member',
                                channel_name: str = '',
                                channel_type: str = 'private',
                                channel_description: str = '',
                                privacy_mode: str = 'private') -> bool:
        """
        Send a targeted member sync to a specific peer.
        
        Used when a member is added/removed from a private channel
        to notify the peer where that user lives.
        """
        payload = {
            'content': '',
            'metadata': {
                'type': 'member_sync',
                'channel_id': channel_id,
                'target_user_id': target_user_id,
                'action': action,  # 'add' or 'remove'
                'role': role,
                'channel_name': channel_name,
                'channel_type': channel_type,
                'channel_description': channel_description,
                'privacy_mode': privacy_mode,
            }
        }

        message = self.create_message(MessageType.MEMBER_SYNC, to_peer, payload)
        self.sign_message(message)
        return await self._route_to_peer(message)

    async def send_channel_sync(self, to_peer: str,
                                 channels: List[Dict[str, Any]]) -> bool:
        """
        Send a bulk channel sync to a specific peer.
        
        Sent when a new peer connects so they learn about our channels.
        """
        payload = {
            'content': '',
            'metadata': {
                'type': 'channel_sync',
                'channels': channels,
            }
        }

        message = self.create_message(MessageType.CHANNEL_SYNC, to_peer, payload)
        self.sign_message(message)

        # Send only to the target peer (not a full broadcast)
        if self.connection_manager.is_connected(to_peer):
            return bool(await self.connection_manager.send_to_peer(
                to_peer,
                {'type': 'p2p_message', 'message': message.to_dict()}
            ))
        return False

    async def send_catchup_request(self, to_peer: str,
                                    channel_timestamps: Dict[str, str],
                                    extra_timestamps: Optional[Dict[str, str]] = None) -> bool:
        """
        Send a catch-up request to a specific peer.

        Args:
            to_peer: Peer to request catch-up from
            channel_timestamps: {channel_id: last_message_timestamp} pairs
            extra_timestamps: Optional dict with feed_latest, circle_entries_latest,
                              circle_votes_latest, tasks_latest
        """
        meta: Dict[str, Any] = {
            'type': 'channel_catchup_request',
            'channel_timestamps': channel_timestamps,
        }
        if extra_timestamps:
            meta.update(extra_timestamps)

        payload = {
            'content': '',
            'metadata': meta,
        }

        message = self.create_message(
            MessageType.CHANNEL_CATCHUP_REQUEST, to_peer, payload)
        self.sign_message(message)

        if self.connection_manager.is_connected(to_peer):
            return bool(await self.connection_manager.send_to_peer(
                to_peer,
                {'type': 'p2p_message', 'message': message.to_dict()}
            ))
        return False

    async def send_catchup_response(self, to_peer: str,
                                     messages: List[Dict],
                                     extra_data: Optional[Dict[str, List]] = None) -> bool:
        """
        Send a catch-up response with missed messages to a peer.

        Args:
            to_peer: Peer that requested catch-up
            messages: List of channel message dicts to deliver
            extra_data: Optional dict with feed_posts, circle_entries,
                        circle_votes, tasks lists
        """
        meta: Dict[str, Any] = {
            'type': 'channel_catchup_response',
            'messages': messages,
        }
        if extra_data:
            meta.update(extra_data)

        payload = {
            'content': '',
            'metadata': meta,
        }

        message = self.create_message(
            MessageType.CHANNEL_CATCHUP_RESPONSE, to_peer, payload)
        self.sign_message(message)

        if self.connection_manager.is_connected(to_peer):
            return bool(await self.connection_manager.send_to_peer(
                to_peer,
                {'type': 'p2p_message', 'message': message.to_dict()}
            ))
        return False

    async def send_broadcast(self, content: str, metadata: Optional[Dict] = None) -> bool:
        """
        Send a broadcast message to all peers.
        
        Args:
            content: Message content
            metadata: Optional metadata
            
        Returns:
            True if sent successfully
        """
        payload = {
            'content': content,
            'metadata': metadata or {}
        }
        
        message = self.create_message(MessageType.BROADCAST, None, payload)
        self.sign_message(message)
        
        return await self.route_message(message)
    
    def update_routing_table(self, peer_id: str, next_hop: str) -> None:
        """
        Update routing table entry.
        
        Args:
            peer_id: Destination peer ID
            next_hop: Next hop peer ID
        """
        self.routing_table[peer_id] = next_hop
        logger.debug(f"Updated route: {peer_id} -> {next_hop}")
    
    # ------------------------------------------------------------------ #
    #  Connection brokering and relay                                      #
    # ------------------------------------------------------------------ #

    async def send_broker_request(self, to_peer: str, target_peer: str,
                                   requester_endpoints: List[str],
                                   requester_keys: Dict[str, str]) -> bool:
        """Ask a connected peer to broker a connection to target_peer."""
        payload = {
            'content': '',
            'metadata': {
                'type': 'broker_request',
                'target_peer': target_peer,
                'requester_endpoints': requester_endpoints,
                'requester_keys': requester_keys,
            }
        }
        message = self.create_message(MessageType.BROKER_REQUEST, to_peer, payload, ttl=2)
        self.sign_message(message)
        return await self.route_message(message)

    async def send_broker_intro(self, to_peer: str,
                                 requester_peer_id: str,
                                 requester_endpoints: List[str],
                                 requester_keys: Dict[str, str]) -> bool:
        """Forward a broker introduction to the target peer."""
        payload = {
            'content': '',
            'metadata': {
                'type': 'broker_intro',
                'requester_peer_id': requester_peer_id,
                'requester_endpoints': requester_endpoints,
                'requester_keys': requester_keys,
            }
        }
        message = self.create_message(MessageType.BROKER_INTRO, to_peer, payload, ttl=2)
        self.sign_message(message)
        return await self.route_message(message)

    async def send_relay_offer(self, to_peer: str,
                                target_peer: str) -> bool:
        """Offer to relay traffic between to_peer and target_peer."""
        payload = {
            'content': '',
            'metadata': {
                'type': 'relay_offer',
                'target_peer': target_peer,
            }
        }
        message = self.create_message(MessageType.RELAY_OFFER, to_peer, payload, ttl=2)
        self.sign_message(message)
        return await self.route_message(message)

    def remove_route(self, peer_id: str) -> None:
        """Remove a routing table entry."""
        if peer_id in self.routing_table:
            del self.routing_table[peer_id]
            logger.info(f"Removed route for {peer_id}")

    def cleanup_routes_via(self, next_hop: str) -> int:
        """Remove all routes that use next_hop. Returns count removed."""
        to_remove = [dest for dest, hop in self.routing_table.items()
                     if hop == next_hop]
        for dest in to_remove:
            del self.routing_table[dest]
        if to_remove:
            logger.info(f"Cleaned up {len(to_remove)} routes via {next_hop}")
        return len(to_remove)

    async def send_profile_sync(self, to_peer: str,
                                profile_data: Dict[str, Any]) -> bool:
        """Send a profile card to a specific peer (on connection)."""
        payload = {
            'content': '',
            'metadata': {
                'type': 'profile_sync',
                'profile': profile_data,
            }
        }
        message = self.create_message(MessageType.PROFILE_SYNC, to_peer, payload)
        self.sign_message(message)
        if self.connection_manager.is_connected(to_peer):
            return bool(await self.connection_manager.send_to_peer(
                to_peer,
                {'type': 'p2p_message', 'message': message.to_dict()}
            ))
        return False

    async def send_profile_update(self, profile_data: Dict[str, Any]) -> bool:
        """Broadcast a profile update to all connected peers."""
        payload = {
            'content': '',
            'metadata': {
                'type': 'profile_update',
                'profile': profile_data,
            }
        }
        message = self.create_message(MessageType.PROFILE_UPDATE, None, payload)
        self.sign_message(message)
        return await self._route_broadcast(message)

    async def send_peer_announcement(self, to_peer: str,
                                      introduced_peers: List[Dict[str, Any]]) -> bool:
        """Send a list of known/connected peers to a specific peer."""
        payload = {
            'content': '',
            'metadata': {
                'type': 'peer_announcement',
                'introduced_peers': introduced_peers,
            }
        }
        message = self.create_message(MessageType.PEER_ANNOUNCEMENT, to_peer, payload)
        self.sign_message(message)
        if self.connection_manager.is_connected(to_peer):
            return bool(await self.connection_manager.send_to_peer(
                to_peer,
                {'type': 'p2p_message', 'message': message.to_dict()}
            ))
        return False

    async def send_delete_signal(self, signal_id: str, data_type: str,
                                 data_id: str, reason: Optional[str] = None,
                                 target_peer: Optional[str] = None) -> bool:
        """Broadcast a delete signal to all peers (or a specific peer).

        The signal asks recipients to delete the specified data item.
        """
        payload = {
            'content': '',
            'metadata': {
                'type': 'delete_signal',
                'signal_id': signal_id,
                'data_type': data_type,
                'data_id': data_id,
                'reason': reason,
                'requester_peer': self.local_peer_id,
                'is_ack': False,
            }
        }
        message = self.create_message(MessageType.DELETE_SIGNAL, target_peer, payload)
        self.sign_message(message)

        if target_peer:
            return await self.route_message(message)
        return await self._route_broadcast(message)

    async def send_delete_signal_ack(self, to_peer: str, signal_id: str,
                                     status: str) -> bool:
        """Send acknowledgment / compliance response for a delete signal."""
        payload = {
            'content': '',
            'metadata': {
                'type': 'delete_signal',
                'signal_id': signal_id,
                'is_ack': True,
                'ack_status': status,  # 'acknowledged' | 'complied' | 'rejected'
                'responder_peer': self.local_peer_id,
            }
        }
        message = self.create_message(MessageType.DELETE_SIGNAL, to_peer, payload)
        self.sign_message(message)
        return await self.route_message(message)

    async def flush_pending_messages(self, peer_id: str) -> int:
        """
        Flush pending messages when peer comes online.
        
        Args:
            peer_id: Peer that came online
            
        Returns:
            Number of messages sent
        """
        if peer_id not in self.pending_messages:
            return 0
        
        messages = self.pending_messages.pop(peer_id)
        sent_count = 0
        
        logger.info(f"Flushing {len(messages)} pending messages to {peer_id}")
        
        for message in messages:
            if await self.route_message(message):
                sent_count += 1
        
        return sent_count
