"""
P2P Network Manager for Canopy.

Coordinates all P2P networking components and provides a unified interface
for the application layer.

Author: Konrad Walus (architecture, design, and direction)
Project: Canopy - Local Mesh Communication
License: Apache 2.0
Development: AI-assisted implementation (Claude, Codex, GitHub Copilot, Cursor IDE, Ollama)
"""

import asyncio
import logging
import os
import threading
import time
import json
from collections import deque
from typing import Optional, Callable, Dict, Any, Union, Tuple, Deque
from pathlib import Path

from .. import __version__ as CANOPY_VERSION
from .. import __protocol_version__ as CANOPY_PROTOCOL_VERSION
from ..core.messaging import (
    DM_E2E_CAPABILITY,
    build_dm_security_summary,
    encrypt_dm_transport_bundle,
    is_local_dm_user,
)
from ..core.large_attachments import (
    LARGE_ATTACHMENT_CAPABILITY,
    LARGE_ATTACHMENT_THRESHOLD,
    LARGE_ATTACHMENT_CHUNK_SIZE,
    build_large_attachment_metadata,
)
from .identity import IdentityManager, PeerIdentity
from .discovery import PeerDiscovery, DiscoveredPeer
from .connection import ConnectionManager, PeerConnection
from .routing import (
    MessageRouter,
    P2PMessage,
    MessageType,
    encrypt_with_channel_key,
    decode_channel_key_material,
)

logger = logging.getLogger('canopy.network.manager')


class P2PNetworkManager:
    """
    Main P2P network manager that coordinates all networking components.
    
    This is the primary interface between the application and the P2P network.
    """
    
    def __init__(self, config, database_manager):
        """
        Initialize P2P network manager.
        
        Args:
            config: Application configuration
            database_manager: Database manager for persistence
        """
        self.config = config
        self.db = database_manager
        
        # Determine identity path
        identity_path = Path(config.storage.database_path).parent / 'peer_identity.json'
        
        # Initialize components
        logger.info("Initializing P2P network components...")
        
        self.identity_manager = IdentityManager(identity_path)
        self.local_identity: Optional[PeerIdentity] = None
        
        self.discovery: Optional[PeerDiscovery] = None
        self.connection_manager: Optional[ConnectionManager] = None
        self.message_router: Optional[MessageRouter] = None
        
        # State
        self._running = False
        self._event_loop: Optional[asyncio.AbstractEventLoop] = None
        self._network_thread: Optional[threading.Thread] = None
        
        # Application callbacks
        self.on_peer_connected: Optional[Callable[[str], None]] = None
        self.on_peer_disconnected: Optional[Callable[[str], None]] = None
        self.on_message_received: Optional[Callable[[P2PMessage], None]] = None
        self.on_channel_message: Optional[Callable] = None
        self.on_channel_announce: Optional[Callable] = None
        self.on_channel_sync: Optional[Callable] = None
        self.on_member_sync: Optional[Callable] = None
        self.on_member_sync_ack: Optional[Callable] = None
        self.on_channel_membership_query: Optional[Callable] = None
        self.on_channel_membership_response: Optional[Callable] = None
        self.on_channel_key_distribution: Optional[Callable] = None
        self.on_channel_key_request: Optional[Callable] = None
        self.on_channel_key_ack: Optional[Callable] = None
        self.on_large_attachment_request: Optional[Callable] = None
        self.on_large_attachment_chunk: Optional[Callable] = None
        self.on_large_attachment_error: Optional[Callable] = None
        self.on_principal_announce: Optional[Callable] = None
        self.on_principal_key_update: Optional[Callable] = None
        self.on_bootstrap_grant_sync: Optional[Callable] = None
        self.on_bootstrap_grant_revoke: Optional[Callable] = None
        self.peer_versions: Dict[str, Dict[str, Any]] = {}
        self.local_canopy_version = str(CANOPY_VERSION or '0.1.0')
        self.local_protocol_version = int(CANOPY_PROTOCOL_VERSION or 1)

        # Recent peer activity events for UI (thread-safe; event-loop thread writes, Flask reads).
        self._activity_lock = threading.Lock()
        self._activity_events: Deque[Dict[str, Any]] = deque(maxlen=200)
        self._endpoint_health_lock = threading.Lock()
        self._endpoint_health: Dict[str, Dict[str, Dict[str, Any]]] = {}
        
        # Callback that returns list of public channels for sync
        self.get_public_channels_for_sync: Optional[Callable] = None
        
        # Catch-up callbacks
        self.on_catchup_request: Optional[Callable] = None
        self.on_catchup_response: Optional[Callable] = None
        self.get_channel_latest_timestamps: Optional[Callable] = None
        self.get_channel_sync_digests: Optional[Callable] = None
        self.get_feed_latest_timestamp: Optional[Callable[[], Optional[str]]] = None
        self.get_circle_entries_latest_timestamp: Optional[Callable[[], Optional[str]]] = None
        self.get_circle_votes_latest_timestamp: Optional[Callable[[], Optional[str]]] = None
        self.get_circles_latest_timestamp: Optional[Callable[[], Optional[str]]] = None
        self.get_tasks_latest_timestamp: Optional[Callable[[], Optional[str]]] = None
        
        # Profile sync callbacks
        self.on_profile_sync: Optional[Callable] = None
        self.get_local_profile_card: Optional[Callable] = None
        self.get_all_local_profile_cards: Optional[Callable] = None
        # Optional callback to fetch a device profile for a peer_id
        self.get_peer_device_profile: Optional[Callable[[str], Optional[Dict[str, Any]]]] = None

        # Peer announcement callbacks
        self.on_peer_announcement: Optional[Callable] = None
        
        # Delete signal callbacks
        self.on_delete_signal: Optional[Callable] = None
        
        # Feed post, interaction, and direct message callbacks
        self.on_feed_post: Optional[Callable] = None
        self.on_interaction: Optional[Callable] = None
        self.on_direct_message: Optional[Callable] = None
        
        # Trust score lookup (injected from app layer)
        # Signature: get_trust_score(peer_id: str) -> int (0-100)
        self.get_trust_score: Optional[Callable] = None
        
        # Introduced peers learned from connected peers
        self._introduced_peers: Dict[str, Dict[str, Any]] = {}
        
        # Relay policy: 'off', 'broker_only' (default), 'full_relay'
        self.relay_policy: str = 'broker_only'
        self._local_capabilities = self._build_local_capabilities()
        
        # Track active relay routes (destination_peer -> relay_peer)
        self._active_relays: Dict[str, str] = {}
        
        # File manager reference for reading attachment bytes during broadcast
        self.file_manager = None
        
        # Auto-reconnect state. The backoff stage is capped, but retries
        # continue until connectivity recovers or the peer is forgotten.
        self._reconnect_tasks: Dict[str, Any] = {}  # peer_id -> Future/Task
        self._RECONNECT_MAX_BACKOFF_STAGE = 20
        self._RECONNECT_INITIAL_DELAY = 5   # seconds
        self._RECONNECT_MAX_DELAY = 60      # seconds

        # Security compatibility switch:
        # disabled by default so unverifiable relayed messages are rejected.
        # Can be enabled temporarily for mixed-version meshes.
        cfg_security = getattr(config, 'security', None)
        cfg_compat = bool(getattr(cfg_security, 'allow_unverified_relay_messages', False))
        env_compat = os.getenv('CANOPY_ALLOW_UNVERIFIED_RELAY_MESSAGES')
        if env_compat is None:
            self.allow_unverified_relay_messages = cfg_compat
        else:
            self.allow_unverified_relay_messages = env_compat.strip().lower() in (
                '1', 'true', 'yes', 'on'
            )

        # Startup grace period — serializes syncs and limits concurrency
        self._startup_time: Optional[float] = None
        self._STARTUP_GRACE_PERIOD = 10.0  # seconds
        self._sync_queue: asyncio.Queue[str] = asyncio.Queue()
        self._sync_queue_task: Optional[asyncio.Task] = None
        self._active_catchups: set = set()
        self._MAX_CONCURRENT_CATCHUPS_STARTUP = 2
        self._MAX_CONCURRENT_CATCHUPS_NORMAL = 5
        self.sync_digest_enabled = bool(
            getattr(cfg_security, 'sync_digest_enabled', False)
        )
        self.sync_digest_require_capability = bool(
            getattr(cfg_security, 'sync_digest_require_capability', True)
        )
        self.sync_digest_max_channels_per_request = max(
            1,
            int(getattr(cfg_security, 'sync_digest_max_channels_per_request', 200) or 200),
        )
        self._sync_digest_stats: Dict[str, Any] = {
            'channels_checked': 0,
            'channels_matched': 0,
            'channels_mismatched': 0,
            'fallbacks': 0,
            'requests_with_digest': 0,
            'last_used_at': None,
        }

        # Load persisted relay policy (overrides default)
        self._load_persisted_relay_policy()
        
        logger.info("P2PNetworkManager initialized")

    def _build_local_capabilities(self) -> list[str]:
        """Compute P2P capability advertisement for this node."""
        caps = ['chat', 'files', 'voice']
        caps.append(DM_E2E_CAPABILITY)
        sec_cfg = getattr(self.config, 'security', None)
        if bool(getattr(sec_cfg, 'e2e_private_channels', False)):
            caps.append('e2e_channel_v1')
        if bool(getattr(sec_cfg, 'e2e_private_channels_enforce', False)):
            caps.append('e2e_channel_enforce')
        if bool(getattr(sec_cfg, 'sync_digest_enabled', False)):
            caps.append('sync_digest_v1')
        if bool(getattr(sec_cfg, 'identity_portability_enabled', False)):
            caps.append('identity_portability_v1')
        caps.append(LARGE_ATTACHMENT_CAPABILITY)

        out: list[str] = []
        seen = set()
        for cap in caps:
            c = str(cap).strip()
            if not c or c in seen:
                continue
            seen.add(c)
            out.append(c)
        return out

    def get_local_capabilities(self) -> list[str]:
        """Return local advertised P2P capabilities."""
        return list(self._local_capabilities)

    def peer_supports_capability(self, peer_id: str, capability: str) -> bool:
        """Return True when a connected peer advertises the given capability."""
        if not peer_id or not capability:
            return False
        if self.connection_manager:
            conn = self.connection_manager.connections.get(peer_id)
            if conn and conn.capabilities and conn.capabilities.get(capability):
                return True

        introduced = self._introduced_peers.get(peer_id, {}) if hasattr(self, '_introduced_peers') else {}
        introduced_caps = introduced.get('capabilities') if isinstance(introduced, dict) else None
        if capability in {
            str(item).strip()
            for item in (introduced_caps or [])
            if str(item).strip()
        }:
            return True

        peer_version_caps = self.peer_versions.get(peer_id, {}).get('capabilities')
        if capability in {
            str(item).strip()
            for item in (peer_version_caps or [])
            if str(item).strip()
        }:
            return True

        try:
            if self.discovery:
                discovered = self.discovery.get_discovered_peers()
                for peer in discovered:
                    if str(getattr(peer, 'peer_id', '') or '').strip() != peer_id:
                        continue
                    service_info = getattr(peer, 'service_info', {}) or {}
                    discovered_caps = service_info.get('capabilities') if isinstance(service_info, dict) else []
                    if capability in {
                        str(item).strip()
                        for item in (discovered_caps or [])
                        if str(item).strip()
                    }:
                        return True
        except Exception:
            pass
        return False

    def _build_p2p_attachment_entry(self, attachment: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Prepare one attachment payload for P2P propagation."""
        if not isinstance(attachment, dict):
            return None

        file_id = attachment.get('id', attachment.get('file_id'))
        entry: Dict[str, Any] = {
            'name': attachment.get('name', attachment.get('original_name', 'file')),
            'type': attachment.get('type', attachment.get('content_type', 'application/octet-stream')),
            'size': attachment.get('size', 0),
        }
        if file_id:
            entry['id'] = file_id

        if not file_id or not self.file_manager:
            return entry

        try:
            result = self.file_manager.get_file_data(file_id)
            if not result:
                return entry
            file_data, file_info = result
            if len(file_data) <= LARGE_ATTACHMENT_THRESHOLD:
                import base64
                entry['data'] = base64.b64encode(file_data).decode('ascii')
                logger.info(
                    "Attached file %s (%d bytes) to P2P payload",
                    file_id,
                    len(file_data),
                )
                return entry

            local_peer_id = ''
            try:
                local_peer_id = str(self.get_peer_id() or '').strip()
            except Exception:
                local_peer_id = ''
            entry.update(build_large_attachment_metadata(
                file_info=file_info,
                source_peer_id=local_peer_id,
                download_status='pending',
            ))
            entry.pop('url', None)
            logger.info(
                "Prepared large attachment metadata for %s (%d bytes)",
                file_id,
                len(file_data),
            )
        except Exception as e:
            logger.error("Failed to read file %s for P2P transfer: %s", file_id, e)
        return entry

    def _normalize_capability_items(self, raw_caps: Any) -> list[str]:
        values = raw_caps if isinstance(raw_caps, (list, tuple, set)) else []
        out: list[str] = []
        seen: set[str] = set()
        for raw in values:
            cap = str(raw or '').strip()
            if not cap or cap in seen:
                continue
            seen.add(cap)
            out.append(cap)
        return out

    def _get_dm_recipient_row(self, recipient_id: str) -> Optional[Dict[str, Any]]:
        if not self.db or not recipient_id:
            return None
        try:
            row = self.db.get_user(recipient_id)
        except Exception:
            row = None
        return row if isinstance(row, dict) else None

    def describe_direct_message_security(self, recipient_ids: list[str]) -> Dict[str, Any]:
        clean_recipient_ids = []
        seen_ids: set[str] = set()
        for raw in recipient_ids or []:
            uid = str(raw or '').strip()
            if not uid or uid in seen_ids:
                continue
            seen_ids.add(uid)
            clean_recipient_ids.append(uid)

        local_peer_id = self.local_identity.peer_id if self.local_identity else ''
        local_recipient_ids: list[str] = []
        remote_peer_ids: list[str] = []
        encrypted_peer_ids: list[str] = []
        legacy_peer_ids: list[str] = []
        unknown_peer_ids: list[str] = []

        for recipient_id in clean_recipient_ids:
            row = self._get_dm_recipient_row(recipient_id)
            if not row:
                unknown_peer_ids.append(recipient_id)
                continue
            if is_local_dm_user(self.db, self, recipient_id):
                local_recipient_ids.append(recipient_id)
                continue
            origin_peer = str(row.get('origin_peer') or '').strip()
            if not origin_peer or origin_peer == local_peer_id:
                unknown_peer_ids.append(recipient_id)
                continue
            target_peer_id = origin_peer

            if target_peer_id not in remote_peer_ids:
                remote_peer_ids.append(target_peer_id)
            peer_identity = self.identity_manager.get_peer(target_peer_id)
            if peer_identity and self.peer_supports_capability(target_peer_id, DM_E2E_CAPABILITY):
                if target_peer_id not in encrypted_peer_ids:
                    encrypted_peer_ids.append(target_peer_id)
            else:
                if target_peer_id not in legacy_peer_ids:
                    legacy_peer_ids.append(target_peer_id)

        if clean_recipient_ids and len(local_recipient_ids) == len(clean_recipient_ids):
            return {
                'mode': 'local_only',
                'state': 'local_only',
                'label': 'Local only',
                'e2e': False,
                'relay_confidential': True,
                'local_only': True,
                'recipient_ids': clean_recipient_ids,
                'local_recipient_ids': local_recipient_ids,
                'remote_peer_ids': [],
                'encrypted_peer_ids': [],
                'legacy_peer_ids': [],
                'unknown_peer_ids': [],
            }

        if remote_peer_ids and not legacy_peer_ids and not unknown_peer_ids:
            return {
                'mode': 'peer_e2e_v1',
                'state': 'encrypted',
                'label': 'E2E over mesh',
                'e2e': True,
                'relay_confidential': True,
                'local_only': False,
                'recipient_ids': clean_recipient_ids,
                'local_recipient_ids': local_recipient_ids,
                'remote_peer_ids': remote_peer_ids,
                'encrypted_peer_ids': encrypted_peer_ids,
                'legacy_peer_ids': [],
                'unknown_peer_ids': [],
            }

        if remote_peer_ids and (encrypted_peer_ids or local_recipient_ids):
            return {
                'mode': 'mixed',
                'state': 'mixed',
                'label': 'Mixed delivery',
                'e2e': False,
                'relay_confidential': False,
                'local_only': False,
                'recipient_ids': clean_recipient_ids,
                'local_recipient_ids': local_recipient_ids,
                'remote_peer_ids': remote_peer_ids,
                'encrypted_peer_ids': encrypted_peer_ids,
                'legacy_peer_ids': legacy_peer_ids,
                'unknown_peer_ids': unknown_peer_ids,
                'warning': 'Some recipients require legacy/plaintext mesh delivery',
            }

        return {
            'mode': 'legacy_plaintext',
            'state': 'plaintext',
            'label': 'Legacy relay/plaintext',
            'e2e': False,
            'relay_confidential': False,
            'local_only': False,
            'recipient_ids': clean_recipient_ids,
            'local_recipient_ids': local_recipient_ids,
            'remote_peer_ids': remote_peer_ids,
            'encrypted_peer_ids': encrypted_peer_ids,
            'legacy_peer_ids': legacy_peer_ids,
            'unknown_peer_ids': unknown_peer_ids,
            'warning': 'Recipient peer does not advertise DM E2E support',
        }

    def _record_activity_event(self, event: Dict[str, Any]) -> None:
        """Record a user-facing activity event from the message router."""
        try:
            with self._activity_lock:
                self._activity_events.append(event)
        except Exception as e:
            logger.debug(f"Failed to record activity event: {e}")

    def get_activity_events(self, since: Optional[float] = None, limit: int = 50) -> list[Dict[str, Any]]:
        """Return recent activity events, optionally filtered by timestamp."""
        with self._activity_lock:
            events = list(self._activity_events)
        if since is not None:
            try:
                since_val = float(since)
            except (TypeError, ValueError):
                since_val = None
            if since_val is not None:
                events = [e for e in events if (e.get('timestamp') or 0) > since_val]
        if limit and limit > 0:
            events = events[-limit:]
        return events

    def record_activity_event(self, event: Dict[str, Any]) -> None:
        """Public hook to record a UI activity event (e.g., mentions)."""
        self._record_activity_event(event)

    def _record_connection_event(self, peer_id: str, status: str,
                                 detail: Optional[str] = None,
                                 endpoint: Optional[str] = None,
                                 via_peer: Optional[str] = None) -> None:
        """Record a connection event for UI history."""
        try:
            event = {
                'id': f"conn_{peer_id}_{int(time.time() * 1000)}",
                'peer_id': peer_id,
                'kind': 'connection',
                'timestamp': time.time(),
                'status': status,
                'detail': detail or '',
                'endpoint': endpoint,
                'via_peer': via_peer,
            }
            self._record_activity_event(event)
        except Exception as e:
            logger.debug(f"Failed to record connection event: {e}")
    
    def _in_startup_grace_period(self) -> bool:
        """Check if we're still in the startup grace period."""
        if self._startup_time is None:
            return False
        return (time.time() - self._startup_time) < self._STARTUP_GRACE_PERIOD

    def _get_max_concurrent_catchups(self) -> int:
        """Get max concurrent catchups based on startup state."""
        if self._in_startup_grace_period():
            return self._MAX_CONCURRENT_CATCHUPS_STARTUP
        return self._MAX_CONCURRENT_CATCHUPS_NORMAL

    async def _acquire_catchup_slot(self, peer_id: str) -> bool:
        """Try to acquire a catchup slot. Returns True if acquired."""
        max_catchups = self._get_max_concurrent_catchups()
        if len(self._active_catchups) >= max_catchups:
            logger.debug(f"Catchup slot denied for {peer_id}: {len(self._active_catchups)}/{max_catchups} active")
            return False
        self._active_catchups.add(peer_id)
        return True

    def _release_catchup_slot(self, peer_id: str) -> None:
        """Release a catchup slot."""
        self._active_catchups.discard(peer_id)

    async def _process_sync_queue(self) -> None:
        """Process post-connect sync requests serially to prevent DB contention."""
        logger.info("Sync queue processor started")
        while self._running:
            try:
                peer_id = await self._sync_queue.get()
                try:
                    if not await self._acquire_catchup_slot(peer_id):
                        # Wait a bit and retry if at capacity
                        await asyncio.sleep(2.0)
                        if not await self._acquire_catchup_slot(peer_id):
                            logger.warning(f"Skipping sync for {peer_id}: at capacity")
                            self._sync_queue.task_done()
                            continue

                    await self._run_post_connect_sync_impl(peer_id)

                    # Delay between syncs to reduce contention
                    if self._in_startup_grace_period():
                        await asyncio.sleep(1.0)
                    else:
                        await asyncio.sleep(0.3)
                except Exception as e:
                    logger.error(f"Error processing sync for {peer_id}: {e}", exc_info=True)
                finally:
                    self._release_catchup_slot(peer_id)
                    self._sync_queue.task_done()
            except asyncio.CancelledError:
                logger.info("Sync queue processor cancelled")
                break
            except Exception as e:
                logger.error(f"Error in sync queue processor: {e}", exc_info=True)

    def start(self) -> None:
        """Start P2P networking in a background thread."""
        if self._running:
            logger.warning("P2P network already running")
            return
        
        logger.info("Starting P2P network...")
        
        # Initialize identity
        self.local_identity = self.identity_manager.initialize()
        
        logger.info(f"Local Peer ID: {self.local_identity.peer_id}")
        logger.info(f"Ed25519 Public Key: {self.local_identity.ed25519_public_key[:16].hex()}...")
        
        # Start network thread
        self._network_thread = threading.Thread(target=self._run_network_loop, daemon=True)
        self._network_thread.start()
        
        logger.info("P2P network started")
    
    def stop(self) -> None:
        """Stop P2P networking."""
        if not self._running:
            return
        
        logger.info("Stopping P2P network...")
        self._running = False
        
        # Stop event loop
        if self._event_loop:
            self._event_loop.call_soon_threadsafe(self._event_loop.stop)
        
        # Wait for thread to finish
        if self._network_thread:
            self._network_thread.join(timeout=5.0)
        
        logger.info("P2P network stopped")
    
    def _run_network_loop(self) -> None:
        """Run the asyncio event loop in a separate thread."""
        try:
            # Create new event loop for this thread
            self._event_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._event_loop)
            
            # Run startup coroutine
            self._event_loop.run_until_complete(self._startup())
            
            # Keep loop running
            self._event_loop.run_forever()
            
        except Exception as e:
            logger.error(f"Error in network loop: {e}", exc_info=True)
        finally:
            # Cleanup
            if self._event_loop:
                self._event_loop.run_until_complete(self._shutdown())
                self._event_loop.close()
    
    async def _startup(self) -> None:
        """Initialize and start all networking components."""
        try:
            logger.info("Starting P2P networking components...")
            
            # Get network config
            network_config = self.config.network
            if self.local_identity is None:
                raise RuntimeError("Local identity is not initialized")
            
            # Initialize connection manager FIRST (core functionality)
            # Always bind P2P listener to all interfaces for peer connectivity
            self.connection_manager = ConnectionManager(
                local_peer_id=self.local_identity.peer_id,
                identity_manager=self.identity_manager,
                host="0.0.0.0",
                port=network_config.mesh_port,
                enable_tls=getattr(network_config, 'enable_tls', False),
                tls_cert_path=getattr(network_config, 'tls_cert_path', None),
                tls_key_path=getattr(network_config, 'tls_key_path', None),
                handshake_capabilities=self._local_capabilities,
                canopy_version=self.local_canopy_version,
                protocol_version=self.local_protocol_version,
                reject_protocol_mismatch=bool(
                    os.getenv('CANOPY_REJECT_PROTOCOL_MISMATCH', '').strip().lower() in ('1', 'true', 'yes', 'on')
                ),
            )
            
            # Initialize message router
            self.message_router = MessageRouter(
                local_peer_id=self.local_identity.peer_id,
                identity_manager=self.identity_manager,
                connection_manager=self.connection_manager
            )
            
            # Pass application callbacks
            if self.on_channel_message:
                self.message_router.on_channel_message = self.on_channel_message
            if self.on_channel_announce:
                self.message_router.on_channel_announce = self.on_channel_announce
            if self.on_channel_sync:
                self.message_router.on_channel_sync = self.on_channel_sync
            if self.on_member_sync:
                self.message_router.on_member_sync = self.on_member_sync
            if self.on_member_sync_ack:
                self.message_router.on_member_sync_ack = self.on_member_sync_ack
            if self.on_channel_membership_query:
                self.message_router.on_channel_membership_query = self.on_channel_membership_query
            if self.on_channel_membership_response:
                self.message_router.on_channel_membership_response = self.on_channel_membership_response
            if self.on_channel_key_distribution:
                self.message_router.on_channel_key_distribution = self.on_channel_key_distribution
            if self.on_channel_key_request:
                self.message_router.on_channel_key_request = self.on_channel_key_request
            if self.on_channel_key_ack:
                self.message_router.on_channel_key_ack = self.on_channel_key_ack
            if self.on_large_attachment_request:
                self.message_router.on_large_attachment_request = self.on_large_attachment_request
            if self.on_large_attachment_chunk:
                self.message_router.on_large_attachment_chunk = self.on_large_attachment_chunk
            if self.on_large_attachment_error:
                self.message_router.on_large_attachment_error = self.on_large_attachment_error
            if self.on_principal_announce:
                self.message_router.on_principal_announce = self.on_principal_announce
            if self.on_principal_key_update:
                self.message_router.on_principal_key_update = self.on_principal_key_update
            if self.on_bootstrap_grant_sync:
                self.message_router.on_bootstrap_grant_sync = self.on_bootstrap_grant_sync
            if self.on_bootstrap_grant_revoke:
                self.message_router.on_bootstrap_grant_revoke = self.on_bootstrap_grant_revoke
            if self.on_catchup_request:
                self.message_router.on_catchup_request = self.on_catchup_request
            if self.on_catchup_response:
                self.message_router.on_catchup_response = self.on_catchup_response
            if self.on_profile_sync:
                self.message_router.on_profile_sync = self.on_profile_sync
            if self.on_peer_announcement:
                self.message_router.on_peer_announcement = self.on_peer_announcement
            if self.on_delete_signal:
                self.message_router.on_delete_signal = self.on_delete_signal
            if self.on_feed_post:
                self.message_router.on_feed_post = self.on_feed_post
            if self.on_interaction:
                self.message_router.on_interaction = self.on_interaction
            if self.on_direct_message:
                self.message_router.on_direct_message = self.on_direct_message

            # Internal UI activity tracking (user-facing message types only)
            self.message_router.on_activity_event = self._record_activity_event
            
            # Wire broker/relay callbacks (handled internally by manager)
            self.message_router.on_broker_request = self._on_broker_request
            self.message_router.on_broker_intro = self._on_broker_intro
            self.message_router.on_relay_offer = self._on_relay_offer
            
            # Register message handler
            self.connection_manager.register_message_handler(
                'p2p_message',
                self._handle_p2p_message
            )
            
            # When a remote peer connects to us (incoming), run channel sync + catch-up
            self.connection_manager.on_peer_authenticated = self._on_incoming_peer_authenticated
            
            # Clean up relay routes when a peer disconnects
            self.connection_manager.on_peer_disconnected = self.on_peer_disconnected_cleanup
            
            # Start the WebSocket server (core P2P functionality)
            await self.connection_manager.start()
            
            self._running = True
            logger.info("P2P WebSocket mesh started successfully")
            
            # Start mDNS discovery in a SEPARATE thread to avoid zeroconf
            # corrupting the P2P asyncio event loop (known issue on Windows).
            def _start_discovery_isolated():
                try:
                    local_identity = self.local_identity
                    if local_identity is None:
                        raise RuntimeError("Local identity not initialized")
                    self.discovery = PeerDiscovery(
                        local_peer_id=local_identity.peer_id,
                        service_port=network_config.mesh_port,
                        service_name=f"canopy-{local_identity.peer_id}",
                        capabilities=self._local_capabilities,
                    )
                    self.discovery.on_peer_discovered(self._on_peer_discovered)
                    self.discovery.start()
                    logger.info("mDNS discovery started")
                except Exception as disc_err:
                    logger.warning(f"mDNS discovery failed (non-fatal, invite codes still work): {disc_err}")
                    self.discovery = None
            
            discovery_thread = threading.Thread(target=_start_discovery_isolated, daemon=True)
            discovery_thread.start()
            discovery_thread.join(timeout=15)  # Wait up to 15s, then move on
            
            # Initialize sync queue and start processor
            self._sync_queue = asyncio.Queue()
            self._startup_time = time.time()
            self._sync_queue_task = asyncio.ensure_future(self._process_sync_queue())

            # Auto-reconnect to all known peers from previous sessions
            asyncio.ensure_future(self._reconnect_known_peers())

            # Periodic catch-up: every few minutes, compare timestamps
            # with all connected peers and request any missed messages.
            # This covers messages lost due to flaky connections where
            # the websocket send "succeeds" but the remote never processes it.
            asyncio.ensure_future(self._periodic_catchup_loop())

        except Exception as e:
            logger.error(f"Failed to start P2P components: {e}", exc_info=True)
            raise
    
    async def _reconnect_known_peers(self) -> None:
        """Try to connect to all known peers on startup using persisted endpoints.
        
        Runs after a short delay to let the mesh listener settle.
        """
        await asyncio.sleep(3)  # Let WebSocket server and mDNS settle first
        
        known_peer_ids = set(self.identity_manager.peer_endpoints.keys())
        known_peer_ids.update((getattr(self.identity_manager, 'known_peers', {}) or {}).keys())
        local_id = self.local_identity.peer_id if self.local_identity else ''
        attempted = 0
        connected = 0

        for peer_id in list(known_peer_ids):
            if peer_id == local_id:
                continue
            if self.connection_manager and self.connection_manager.is_connected(peer_id):
                continue
            endpoints = self._get_connectable_peer_endpoints(peer_id, prefer_discovered=True)
            if not endpoints:
                continue

            attempted += 1
            for ep in endpoints:
                try:
                    await self._connect_to_endpoint(peer_id, ep)
                    if self.connection_manager and self.connection_manager.is_connected(peer_id):
                        logger.info(f"Startup reconnect: connected to {peer_id} via {ep}")
                        connected += 1
                        # Queue post-connect sync instead of fire-and-forget
                        await self._enqueue_sync(peer_id)
                        break
                except Exception as e:
                    logger.debug(f"Startup reconnect: {peer_id} via {ep} failed: {e}")
                    continue

        if attempted:
            logger.info(f"Startup reconnect: {connected}/{attempted} known peers connected")

    def reconnect_known_peers(self) -> bool:
        """Public method: schedule reconnect to all known peers now."""
        if not self._event_loop or self._event_loop.is_closed():
            logger.warning("Reconnect: event loop not available")
            return False
        if not self._running:
            logger.warning("Reconnect: P2P network not running")
            return False
        asyncio.run_coroutine_threadsafe(self._reconnect_known_peers(), self._event_loop)
        logger.info("Reconnect: scheduled reconnect to known peers")
        return True

    # ------------------------------------------------------------------ #
    #  Auto-reconnect on disconnect                                        #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _parse_endpoint(endpoint: str) -> Optional[Tuple[str, int, str]]:
        """Parse ws:// or wss:// endpoint into (host, port, scheme)."""
        if not endpoint:
            return None
        try:
            from urllib.parse import urlparse
            ep = endpoint.strip()
            if '://' not in ep:
                ep = f"ws://{ep}"
            parsed = urlparse(ep)
            host = parsed.hostname
            port = parsed.port
            scheme = parsed.scheme or 'ws'
            if not host or not port:
                return None
            return host, port, scheme
        except Exception:
            return None

    @staticmethod
    def _format_endpoint_host(host: str) -> str:
        """Format a host for endpoint rendering, preserving IPv6 brackets."""
        text = str(host or '').strip()
        if ':' in text and not text.startswith('['):
            return f"[{text}]"
        return text

    def _canonicalize_endpoint(self, endpoint: str) -> Optional[str]:
        parsed = self._parse_endpoint(endpoint)
        if not parsed:
            return None
        host, port, scheme = parsed
        return f"{scheme}://{self._format_endpoint_host(host)}:{port}"

    def _sanitize_endpoints(self, peer_id: str, endpoints: list) -> list:
        """Drop unusable endpoints (loopback/0.0.0.0) and de-dupe while preserving order."""
        local_id = self.local_identity.peer_id if self.local_identity else ''
        out: list = []
        seen: set = set()
        for ep in endpoints or []:
            canon = self._canonicalize_endpoint(ep)
            if not canon:
                continue
            parsed = self._parse_endpoint(canon)
            if not parsed:
                continue
            host, _, _ = parsed
            if peer_id != local_id:
                if host in ('0.0.0.0', 'localhost') or host.startswith('127.'):
                    continue
            if canon in seen:
                continue
            seen.add(canon)
            out.append(canon)
        return out

    def _discovered_peer_endpoints(self, peer: Optional[DiscoveredPeer]) -> list[str]:
        """Return sanitized endpoints derived from a discovered peer record."""
        if not peer:
            return []
        addresses = list(getattr(peer, 'addresses', []) or [])
        primary = str(getattr(peer, 'address', '') or '').strip()
        if primary and primary not in addresses:
            addresses.insert(0, primary)
        port = int(getattr(peer, 'port', 0) or 0)
        if port <= 0:
            return []
        endpoints = [
            f"{self.ws_scheme}://{self._format_endpoint_host(addr)}:{port}"
            for addr in addresses
            if addr
        ]
        return self._sanitize_endpoints(getattr(peer, 'peer_id', ''), endpoints)

    def _get_discovered_peer_endpoints(self, peer_id: str) -> list[str]:
        """Return currently discovered endpoints for one peer."""
        if not self.discovery or not peer_id:
            return []
        try:
            peer = self.discovery.get_peer(peer_id)
        except Exception:
            peer = None
        return self._discovered_peer_endpoints(peer)

    def _get_advertisable_peer_endpoints(self, peer_id: str) -> list[str]:
        """Return endpoints safe to persist/re-announce for a peer.

        Stored endpoints come first. If none exist, fall back to addresses from
        live discovery rather than inventing a socket-origin endpoint.
        """
        stored = self._sanitize_endpoints(
            peer_id,
            self.identity_manager.peer_endpoints.get(peer_id, []),
        )
        if stored != self.identity_manager.peer_endpoints.get(peer_id, []):
            self.identity_manager.peer_endpoints[peer_id] = stored
            self.identity_manager._save_known_peers()
        if stored:
            return stored
        return self._get_discovered_peer_endpoints(peer_id)

    @staticmethod
    def _merge_endpoint_lists(*endpoint_groups: list[str]) -> list[str]:
        """Merge endpoint lists while preserving order and removing duplicates."""
        merged: list[str] = []
        seen: set[str] = set()
        for group in endpoint_groups:
            for endpoint in group or []:
                if endpoint in seen:
                    continue
                seen.add(endpoint)
                merged.append(endpoint)
        return merged

    def _get_connectable_peer_endpoints(self, peer_id: str, prefer_discovered: bool = True) -> list[str]:
        """Return the best available endpoints for dialing a peer.

        Live discovery is preferred for connection attempts because it is more
        likely to reflect current LAN reality than persisted endpoint state.
        """
        stored = self._sanitize_endpoints(
            peer_id,
            self.identity_manager.peer_endpoints.get(peer_id, []),
        )
        if stored != self.identity_manager.peer_endpoints.get(peer_id, []):
            self.identity_manager.peer_endpoints[peer_id] = stored
            self.identity_manager._save_known_peers()
        discovered = self._get_discovered_peer_endpoints(peer_id)
        if discovered:
            self._remember_discovered_peer_endpoints(peer_id)
        if prefer_discovered:
            return self._merge_endpoint_lists(discovered, stored)
        return self._merge_endpoint_lists(stored, discovered)

    def _remember_discovered_peer_endpoints(self, peer_id: str) -> list[str]:
        """Persist endpoints learned from mDNS discovery for later reconnect."""
        endpoints = self._get_discovered_peer_endpoints(peer_id)
        if not endpoints:
            return []
        existing = set(self.identity_manager.peer_endpoints.get(peer_id, []) or [])
        changed = False
        for endpoint in endpoints:
            if endpoint in existing:
                continue
            # Do not "claim" a discovered endpoint until a real connection
            # succeeds. Discovery can be stale or wrong; successful connect
            # paths already claim the endpoint authoritatively.
            self.identity_manager.record_endpoint(peer_id, endpoint, claim=False)
            existing.add(endpoint)
            changed = True
        if changed:
            logger.debug(
                "Recorded %d discovered endpoint(s) for %s",
                len(endpoints),
                peer_id,
            )
        return endpoints

    def _record_endpoint_result(
        self,
        peer_id: str,
        endpoint: Optional[str],
        *,
        success: bool,
        reason: Optional[str] = None,
        detail: Optional[str] = None,
        sources: Optional[list[str]] = None,
    ) -> None:
        """Track endpoint-level health so diagnostics can explain failures."""
        canon = self._canonicalize_endpoint(endpoint or '')
        if not peer_id or not canon:
            return
        now = time.time()
        with self._endpoint_health_lock:
            peer_entries = self._endpoint_health.setdefault(peer_id, {})
            entry = peer_entries.setdefault(
                canon,
                {
                    'endpoint': canon,
                    'attempt_count': 0,
                    'success_count': 0,
                    'consecutive_failures': 0,
                    'last_attempt_at': None,
                    'last_success_at': None,
                    'last_failure_at': None,
                    'last_failure_reason': None,
                    'last_failure_detail': None,
                    'last_status': None,
                    'sources': [],
                },
            )
            entry['attempt_count'] = int(entry.get('attempt_count') or 0) + 1
            entry['last_attempt_at'] = now
            if sources:
                merged_sources = list(entry.get('sources') or [])
                for source in sources:
                    text = str(source or '').strip()
                    if text and text not in merged_sources:
                        merged_sources.append(text)
                entry['sources'] = merged_sources
            if success:
                entry['success_count'] = int(entry.get('success_count') or 0) + 1
                entry['consecutive_failures'] = 0
                entry['last_success_at'] = now
                entry['last_status'] = 'connected'
            else:
                entry['consecutive_failures'] = int(entry.get('consecutive_failures') or 0) + 1
                entry['last_failure_at'] = now
                entry['last_failure_reason'] = str(reason or 'connection_failed')
                entry['last_failure_detail'] = str(detail or reason or 'Connection failed')
                entry['last_status'] = 'failed'

    def _current_connection_endpoint(self, peer_id: str) -> Optional[str]:
        """Return the currently active endpoint for a peer, if known."""
        if not self.connection_manager or not peer_id:
            return None
        conn = self.connection_manager.get_connection(peer_id)
        if not conn:
            return None
        address = str(getattr(conn, 'address', '') or '').strip()
        port = int(getattr(conn, 'port', 0) or 0)
        if not address or port <= 0:
            return None
        return f"{self.ws_scheme}://{self._format_endpoint_host(address)}:{port}"

    def get_peer_endpoint_diagnostics(self, peer_id: str) -> list[Dict[str, Any]]:
        """Return endpoint-source and health information for one peer."""
        stored = self._sanitize_endpoints(
            peer_id,
            self.identity_manager.peer_endpoints.get(peer_id, []),
        )
        discovered = self._get_discovered_peer_endpoints(peer_id)
        active = self._current_connection_endpoint(peer_id)
        endpoint_order = self._merge_endpoint_lists(
            [active] if active else [],
            discovered,
            stored,
        )

        with self._endpoint_health_lock:
            peer_health = dict(self._endpoint_health.get(peer_id, {}) or {})

        rows: list[Dict[str, Any]] = []
        endpoint_order = self._merge_endpoint_lists(endpoint_order, list(peer_health.keys()))
        for endpoint in endpoint_order:
            canon = self._canonicalize_endpoint(endpoint) or endpoint
            health = dict(peer_health.get(canon, {}) or {})
            sources: list[str] = []
            if endpoint in discovered:
                sources.append('discovered')
            if endpoint in stored:
                sources.append('stored')
            if active and canon == active:
                sources.append('active')
            for source in health.get('sources') or []:
                text = str(source or '').strip()
                if text and text not in sources:
                    sources.append(text)
            rows.append({
                'endpoint': canon,
                'sources': sources,
                'currently_connected': bool(active and canon == active),
                'attempt_count': int(health.get('attempt_count') or 0),
                'success_count': int(health.get('success_count') or 0),
                'consecutive_failures': int(health.get('consecutive_failures') or 0),
                'last_attempt_at': health.get('last_attempt_at'),
                'last_success_at': health.get('last_success_at'),
                'last_failure_at': health.get('last_failure_at'),
                'last_failure_reason': health.get('last_failure_reason'),
                'last_failure_detail': health.get('last_failure_detail'),
                'last_status': health.get('last_status'),
            })
        return rows

    async def _connect_to_endpoint(self, peer_id: str, endpoint: str) -> bool:
        """Connect to a peer using a ws:// or wss:// endpoint string."""
        if not self.connection_manager:
            return False
        local_id = self.local_identity.peer_id if self.local_identity else ''
        if peer_id == local_id:
            logger.debug("Reconnect: refusing to connect to self (%s)", peer_id)
            return False
        canon = self._canonicalize_endpoint(endpoint)
        parsed = self._parse_endpoint(canon or endpoint)
        if not parsed:
            logger.debug(f"Reconnect: invalid endpoint '{endpoint}' for {peer_id}")
            return False
        host, port, scheme = parsed
        if peer_id != local_id:
            if host in ('0.0.0.0', 'localhost') or host.startswith('127.'):
                logger.debug(f"Reconnect: skipping unusable endpoint {endpoint} for {peer_id}")
                return False
        if scheme == 'wss' and not self.connection_manager.enable_tls:
            logger.warning(
                f"Reconnect: endpoint {endpoint} requires TLS, but TLS is disabled; "
                f"connection may fail."
            )
        endpoint_sources: list[str] = []
        stored = set(
            self._sanitize_endpoints(
                peer_id,
                self.identity_manager.peer_endpoints.get(peer_id, []),
            )
        )
        discovered = set(self._get_discovered_peer_endpoints(peer_id))
        if canon in discovered:
            endpoint_sources.append('discovered')
        if canon in stored:
            endpoint_sources.append('stored')
        self._record_connection_event(
            peer_id,
            status='attempt',
            detail='Attempting connection',
            endpoint=canon or endpoint,
        )
        ok = await self.connection_manager.connect_to_peer(peer_id, host, port)
        if ok and canon:
            # Claim the endpoint so stale mappings don't keep retrying the wrong peer_id.
            self.identity_manager.record_endpoint(peer_id, canon, claim=True)
            self._record_endpoint_result(
                peer_id,
                canon,
                success=True,
                detail='Connected successfully',
                sources=endpoint_sources,
            )
            self._record_connection_event(
                peer_id,
                status='connected',
                detail='Connected successfully',
                endpoint=canon,
            )
        elif not ok:
            failure_reason = 'connection_failed'
            failure_detail = 'Connection failed'
            if hasattr(self.connection_manager, 'get_last_connect_failure'):
                try:
                    failure = self.connection_manager.get_last_connect_failure(peer_id, host, port)
                except Exception:
                    failure = None
                if isinstance(failure, dict):
                    failure_reason = str(failure.get('reason') or failure_reason)
                    failure_detail = str(failure.get('detail') or failure_detail)
            self._record_endpoint_result(
                peer_id,
                canon or endpoint,
                success=False,
                reason=failure_reason,
                detail=failure_detail,
                sources=endpoint_sources,
            )
            self._record_connection_event(
                peer_id,
                status='failed',
                detail=failure_detail,
                endpoint=canon or endpoint,
            )
        return ok

    def _schedule_reconnect(self, peer_id: str, attempt: int = 1) -> None:
        """Schedule a reconnection attempt for a disconnected peer.

        Uses exponential backoff with a capped delay stage, but keeps retrying
        until the peer reconnects or is explicitly forgotten.
        """
        if not self._event_loop or self._event_loop.is_closed():
            return
        if not self._running:
            return
        local_id = self.local_identity.peer_id if self.local_identity else ''
        if peer_id == local_id:
            return
        if self.connection_manager and self.connection_manager.is_connected(peer_id):
            self._reconnect_tasks.pop(peer_id, None)
            return

        # Keep trying indefinitely, but cap the backoff stage.
        stage = min(attempt, self._RECONNECT_MAX_BACKOFF_STAGE)
        delay = min(
            self._RECONNECT_INITIAL_DELAY * (2 ** (stage - 1)),
            self._RECONNECT_MAX_DELAY,
        )

        async def _attempt() -> None:
            sleep_for = float(delay)
            try:
                # Add a small jitter so multiple peers don't reconnect in lockstep.
                import random

                jitter = random.uniform(0, min(5.0, delay * 0.2))
                sleep_for = delay + jitter
                await asyncio.sleep(sleep_for)
            except asyncio.CancelledError:
                logger.debug(f"Reconnect: task for {peer_id} cancelled during sleep")
                return

            # Check if still needed
            if not self._running:
                return
            if self.connection_manager and self.connection_manager.is_connected(peer_id):
                logger.debug(f"Reconnect: {peer_id} already connected, skipping")
                self._reconnect_tasks.pop(peer_id, None)
                return

            endpoints = self._get_connectable_peer_endpoints(peer_id, prefer_discovered=True)
            if not endpoints:
                logger.debug(f"Reconnect: no endpoints for {peer_id}, giving up")
                self._reconnect_tasks.pop(peer_id, None)
                return

            logger.info(
                f"Reconnect attempt {attempt} (stage {stage}/{self._RECONNECT_MAX_BACKOFF_STAGE}) "
                f"for {peer_id} (delay={sleep_for:.1f}s)"
            )
            self._record_connection_event(
                peer_id,
                status='reconnect',
                detail=f"Reconnect attempt {attempt}",
                endpoint=(endpoints[0] if endpoints else None),
            )

            for ep in endpoints:
                try:
                    await self._connect_to_endpoint(peer_id, ep)
                    if self.connection_manager and self.connection_manager.is_connected(peer_id):
                        logger.info(f"Reconnect: successfully reconnected to {peer_id} via {ep}")
                        self._reconnect_tasks.pop(peer_id, None)
                        asyncio.ensure_future(self._run_post_connect_sync(peer_id))
                        return
                except Exception as e:
                    logger.debug(f"Reconnect: {peer_id} via {ep} failed: {e}")
                    continue

            # All endpoints failed — keep retrying with capped backoff.
            self._reconnect_tasks.pop(peer_id, None)
            self._schedule_reconnect(peer_id, attempt + 1)

        # Cancel any existing task for this peer
        self._cancel_reconnect(peer_id)

        # Schedule in the event loop (may be called from event loop thread or other)
        try:
            loop = self._event_loop
            try:
                running_loop = asyncio.get_running_loop()
            except RuntimeError:
                running_loop = None

            task: Any
            if running_loop is loop:
                task = running_loop.create_task(_attempt())
            elif loop.is_running():
                task = asyncio.run_coroutine_threadsafe(_attempt(), loop)
            else:
                task = loop.create_task(_attempt())
            self._reconnect_tasks[peer_id] = task
        except Exception as e:
            logger.debug(f"Reconnect: failed to schedule task for {peer_id}: {e}")

    def _cancel_reconnect(self, peer_id: str) -> None:
        """Cancel any pending reconnect task for a peer."""
        task = self._reconnect_tasks.pop(peer_id, None)
        if task is None:
            return
        try:
            if hasattr(task, 'cancel'):
                task.cancel()
        except Exception:
            pass

    async def _shutdown(self) -> None:
        """Shutdown all networking components."""
        logger.info("Shutting down P2P components...")

        # Cancel sync queue processor
        if self._sync_queue_task:
            self._sync_queue_task.cancel()
            self._sync_queue_task = None

        # Cancel all pending reconnect tasks
        for peer_id in list(self._reconnect_tasks.keys()):
            self._cancel_reconnect(peer_id)
        
        try:
            if self.connection_manager:
                await self.connection_manager.stop()
            
            if self.discovery:
                self.discovery.stop()
            
            logger.info("All P2P components shutdown")
            
        except Exception as e:
            logger.error(f"Error during shutdown: {e}", exc_info=True)
    
    def _on_peer_discovered(self, peer: DiscoveredPeer, added: bool) -> None:
        """
        Handle peer discovery events.
        
        Args:
            peer: Discovered peer
            added: True if peer was added, False if removed
        """
        if added:
            logger.info(f"Peer discovered: {peer.peer_id} at {peer.address}:{peer.port}")

            discovered_endpoints = self._discovered_peer_endpoints(peer)
            if not discovered_endpoints:
                logger.debug(
                    f"Ignoring discovered peer {peer.peer_id} with no usable discovery endpoints"
                )
                return

            # If we're already connected, just record/claim the endpoint and stop.
            if self.connection_manager and self.connection_manager.is_connected(peer.peer_id):
                for endpoint in discovered_endpoints:
                    self.identity_manager.record_endpoint(peer.peer_id, endpoint, claim=True)
                return

            # Attempt to connect
            if self._event_loop and not self._event_loop.is_closed():
                asyncio.run_coroutine_threadsafe(
                    self._connect_to_discovered_peer(peer),
                    self._event_loop,
                )
        else:
            logger.info(f"Peer removed: {peer.peer_id}")
    
    async def _connect_to_discovered_peer(self, peer: DiscoveredPeer) -> None:
        """
        Connect to a discovered peer.
        
        Args:
            peer: Discovered peer info
        """
        try:
            if not self.connection_manager:
                return
            discovered_endpoints = self._discovered_peer_endpoints(peer)
            if not discovered_endpoints:
                logger.warning(f"Failed to connect to {peer.peer_id}: no usable discovery endpoints")
                return

            for endpoint in discovered_endpoints:
                success = await self._connect_to_endpoint(peer.peer_id, endpoint)
                if not success:
                    continue

                logger.info(f"Successfully connected to {peer.peer_id}")

                # Persist all discovered endpoints for later reconnects.
                for extra_endpoint in discovered_endpoints:
                    if extra_endpoint == endpoint:
                        continue
                    self.identity_manager.record_endpoint(peer.peer_id, extra_endpoint, claim=False)

                # Run the full post-connect sync (channels, profiles, peer announcements, catch-up)
                await self._run_post_connect_sync(peer.peer_id)
                return

            logger.warning(f"Failed to connect to {peer.peer_id}")
                
        except Exception as e:
            logger.error(f"Error connecting to {peer.peer_id}: {e}", exc_info=True)
    
    # ------------------------------------------------------------------ #
    #  Post-connect sync (channel sync + catch-up) for ANY new connection #
    # ------------------------------------------------------------------ #

    def _refresh_peer_version_info(self, peer_id: str) -> None:
        """Refresh cached peer version/protocol metadata from live connection."""
        if not peer_id or not self.connection_manager:
            return
        conn = self.connection_manager.get_connection(peer_id)
        if not conn:
            return
        canopy_version = str(getattr(conn, 'canopy_version', '') or getattr(conn, 'handshake_version', '') or 'unknown')
        protocol_version = int(getattr(conn, 'protocol_version', 1) or 1)
        entry = {
            'canopy_version': canopy_version,
            'protocol_version': protocol_version,
            'version': str(getattr(conn, 'handshake_version', '') or '0.1.0'),
            'compatible_protocol': protocol_version == self.local_protocol_version,
            'capabilities': self._normalize_capability_items(
                list((getattr(conn, 'capabilities', None) or {}).keys())
            ),
        }
        self.peer_versions[peer_id] = entry

        if protocol_version != self.local_protocol_version:
            logger.warning(
                "Connected peer %s protocol mismatch: local=%s remote=%s (canopy=%s)",
                peer_id,
                self.local_protocol_version,
                protocol_version,
                canopy_version,
            )
        elif canopy_version not in {'', 'unknown'} and canopy_version != self.local_canopy_version:
            logger.info(
                "Connected peer %s version differs: local=%s remote=%s",
                peer_id,
                self.local_canopy_version,
                canopy_version,
            )

    def _on_incoming_peer_authenticated(self, peer_id: str, peer_meta: Optional[Dict[str, Any]] = None) -> None:
        """
        Called (from ConnectionManager) when a remote peer successfully
        authenticates on an *incoming* WebSocket.  Schedules channel
        sync + catch-up on the P2P event loop.
        """
        logger.info(f"Incoming peer authenticated: {peer_id} — scheduling post-connect sync")
        self._record_connection_event(
            peer_id,
            status='connected',
            detail='Incoming connection authenticated',
        )
        # Do not invent reconnect endpoints from socket origin addresses. Only
        # persist discovery-derived endpoints, which are authoritative enough
        # to survive reconnects and peer announcements.
        self._remember_discovered_peer_endpoints(peer_id)
        try:
            self._refresh_peer_version_info(peer_id)
            if peer_meta and peer_id in self.peer_versions:
                self.peer_versions[peer_id].update({
                    'canopy_version': str(peer_meta.get('canopy_version') or self.peer_versions[peer_id].get('canopy_version') or 'unknown'),
                    'protocol_version': int(peer_meta.get('protocol_version') or self.peer_versions[peer_id].get('protocol_version') or 1),
                    'version': str(peer_meta.get('version') or self.peer_versions[peer_id].get('version') or '0.1.0'),
                })
                self.peer_versions[peer_id]['compatible_protocol'] = (
                    int(self.peer_versions[peer_id].get('protocol_version') or 1) == self.local_protocol_version
                )
        except Exception:
            pass

        if self._event_loop and not self._event_loop.is_closed():
            asyncio.run_coroutine_threadsafe(
                self._run_post_connect_sync(peer_id),
                self._event_loop
            )

    async def _enqueue_sync(self, peer_id: str) -> None:
        """Add a peer to the sync queue for serialized processing."""
        if self._sync_queue is not None:
            await self._sync_queue.put(peer_id)
            logger.debug(f"Enqueued post-connect sync for {peer_id}")
        else:
            # Fallback if queue not initialized yet
            await self._run_post_connect_sync_impl(peer_id)

    async def _run_post_connect_sync(self, peer_id: str) -> None:
        """
        Schedule post-connect sync for a peer via the sync queue.
        Falls back to direct execution if the queue isn't available.
        """
        await self._enqueue_sync(peer_id)

    async def _run_post_connect_sync_impl(self, peer_id: str) -> None:
        """
        Run channel-sync, profile exchange, peer announcements, flush
        pending messages, and catch-up request for a newly-connected peer
        (works for both incoming and outgoing invite-code connections).
        """
        try:
            # Cancel any pending auto-reconnect task for this peer
            self._cancel_reconnect(peer_id)
            self._refresh_peer_version_info(peer_id)

            # Notify application layer
            if self.on_peer_connected:
                self.on_peer_connected(peer_id)

            # Channel metadata sync
            await self._send_channel_sync_to_peer(peer_id)

            # Ask connected peer for private-channel memberships relevant
            # to local users on this instance (missed announce/member-sync recovery).
            await self._send_membership_recovery_query(peer_id)

            # Retry key requests for E2E private channels where this instance
            # still lacks active key material and the connected peer may provide it.
            await self._retry_missing_channel_key_requests_for_peer(peer_id)

            # Exchange profile cards
            await self._send_profile_to_peer(peer_id)

            # Announce other connected peers to the new peer
            await self._send_peer_announcement_to(peer_id)

            # Announce the new peer to all OTHER existing connected peers
            await self._announce_new_peer_to_others(peer_id)

            # Flush pending messages
            if self.message_router:
                sent_count = await self.message_router.flush_pending_messages(peer_id)
                if sent_count > 0:
                    logger.info(f"Sent {sent_count} pending messages to {peer_id}")

            # Catch-up request for any missed messages
            await self._send_catchup_request(peer_id)
            logger.info(f"Post-connect sync completed for {peer_id}")
        except Exception as e:
            logger.error(f"Error in post-connect sync for {peer_id}: {e}", exc_info=True)

    def trigger_peer_sync(self, peer_id: str) -> bool:
        """
        Public method to trigger channel-sync + catch-up for a peer.
        Called from the API route after a successful invite-code connection.

        Returns True if the sync was successfully scheduled.
        """
        if not self._event_loop or self._event_loop.is_closed():
            logger.warning("Cannot trigger peer sync — event loop unavailable")
            return False
        asyncio.run_coroutine_threadsafe(
            self._run_post_connect_sync(peer_id),
            self._event_loop
        )
        logger.info(f"Peer sync triggered for {peer_id}")
        return True

    # ------------------------------------------------------------------ #
    #  Profile sync helpers                                                #
    # ------------------------------------------------------------------ #

    async def _send_profile_to_peer(self, peer_id: str) -> None:
        """Send local profile cards (all registered users + device) to a peer.

        Sends the primary user profile first (for backwards compat),
        then any additional registered user profiles so remote peers
        can display correct display names for all users on this device.
        """
        if not self.message_router or not self.get_local_profile_card:
            return
        try:
            # Build device info once (shared across all cards)
            device_info = None
            try:
                from canopy.core.device import get_device_profile, get_device_id
                dev_profile = get_device_profile()
                device_info = {
                    'device_id': get_device_id(),
                    'display_name': dev_profile.get('display_name', ''),
                    'description': dev_profile.get('description', ''),
                    'avatar_b64': dev_profile.get('avatar_b64', ''),
                    'avatar_mime': dev_profile.get('avatar_mime', ''),
                }
            except Exception:
                pass

            local_peer_id = self.local_identity.peer_id if self.local_identity else ''

            # Collect all profile cards to send
            cards_to_send: list[Dict[str, Any]] = []
            if self.get_all_local_profile_cards:
                cards_to_send = self.get_all_local_profile_cards() or []
            if not cards_to_send:
                # Fallback to primary profile only
                card = self.get_local_profile_card()
                if card:
                    cards_to_send = [card]

            import hashlib, json as _json

            for card in cards_to_send:
                card['peer_id'] = local_peer_id
                if device_info:
                    card['device'] = device_info
                # Attach a content hash so the receiver can skip
                # re-processing unchanged profiles on reconnect.
                hash_src = _json.dumps(
                    {k: v for k, v in sorted(card.items())
                     if k != 'profile_hash'},
                    sort_keys=True, default=str,
                )
                card['profile_hash'] = hashlib.sha256(
                    hash_src.encode()).hexdigest()[:16]

            if cards_to_send:
                logger.debug(f"Sending {len(cards_to_send)} profile card(s) to {peer_id}")
                for card in cards_to_send:
                    await self.message_router.send_profile_sync(peer_id, card)
        except Exception as e:
            logger.error(f"Error sending profile to {peer_id}: {e}", exc_info=True)

    def broadcast_profile_update(self, profile_data: Dict[str, Any]) -> bool:
        """Broadcast a profile update to all connected peers.

        Called from the application layer when a user changes their
        display name, bio, or avatar.
        """
        if not self._running or not self._event_loop:
            return False
        if not self.message_router:
            return False
        if self.local_identity:
            profile_data['peer_id'] = self.local_identity.peer_id
        # Attach content hash for dedup on the receiving side
        import hashlib, json as _json
        hash_src = _json.dumps(
            {k: v for k, v in sorted(profile_data.items())
             if k != 'profile_hash'},
            sort_keys=True, default=str,
        )
        profile_data['profile_hash'] = hashlib.sha256(
            hash_src.encode()).hexdigest()[:16]
        future = asyncio.run_coroutine_threadsafe(
            self.message_router.send_profile_update(profile_data),
            self._event_loop
        )
        try:
            return future.result(timeout=10.0)
        except Exception as e:
            logger.error(f"Error broadcasting profile update: {e}", exc_info=True)
            return False

    # ------------------------------------------------------------------ #
    #  Peer announcement helpers                                           #
    # ------------------------------------------------------------------ #

    async def _send_peer_announcement_to(self, peer_id: str) -> None:
        """Announce our other connected peers to a specific peer."""
        if not self.message_router or not self.connection_manager:
            return
        try:
            import base58
            connected = self.connection_manager.get_connected_peers()
            introduced: list = []
            for pid in connected:
                if pid == peer_id or pid == (self.local_identity.peer_id if self.local_identity else ''):
                    continue  # skip the target and ourselves
                identity = self.identity_manager.get_peer(pid)
                if not identity:
                    continue
                endpoints = self._get_advertisable_peer_endpoints(pid)
                device_profile = None
                if self.get_peer_device_profile:
                    try:
                        device_profile = self.get_peer_device_profile(pid)
                    except Exception:
                        device_profile = None
                display_name = (
                    device_profile.get('display_name')
                    if device_profile and device_profile.get('display_name')
                    else self.identity_manager.peer_display_names.get(pid, pid[:8])
                )
                introduced.append({
                    'peer_id': pid,
                    'display_name': display_name,
                    'endpoints': endpoints,
                    'ed25519_public_key': base58.b58encode(identity.ed25519_public_key).decode(),
                    'x25519_public_key': base58.b58encode(identity.x25519_public_key).decode(),
                    'capabilities': self._normalize_capability_items(
                        list((getattr(self.connection_manager.get_connection(pid), 'capabilities', None) or {}).keys())
                    ),
                    'device_profile': device_profile,
                })
            if introduced:
                logger.debug(f"Announcing {len(introduced)} peers to {peer_id}")
                await self.message_router.send_peer_announcement(peer_id, introduced)
        except Exception as e:
            logger.error(f"Error sending peer announcement to {peer_id}: {e}", exc_info=True)

    async def _announce_new_peer_to_others(self, new_peer_id: str) -> None:
        """Announce a newly connected peer to all OTHER connected peers.

        This ensures that when Machine B joins, the VM learns about B
        (and vice versa) even if B connected after the VM.
        """
        if not self.message_router or not self.connection_manager:
            return
        try:
            import base58
            identity = self.identity_manager.get_peer(new_peer_id)
            if not identity:
                return
            endpoints = self._get_advertisable_peer_endpoints(new_peer_id)
            device_profile = None
            if self.get_peer_device_profile:
                try:
                    device_profile = self.get_peer_device_profile(new_peer_id)
                except Exception:
                    device_profile = None
            display_name = (
                device_profile.get('display_name')
                if device_profile and device_profile.get('display_name')
                else self.identity_manager.peer_display_names.get(new_peer_id, new_peer_id[:8])
            )
            new_peer_info = [{
                'peer_id': new_peer_id,
                'display_name': display_name,
                'endpoints': endpoints,
                'ed25519_public_key': base58.b58encode(identity.ed25519_public_key).decode(),
                'x25519_public_key': base58.b58encode(identity.x25519_public_key).decode(),
                'capabilities': self._normalize_capability_items(
                    list((getattr(self.connection_manager.get_connection(new_peer_id), 'capabilities', None) or {}).keys())
                ),
                'device_profile': device_profile,
            }]

            connected = self.connection_manager.get_connected_peers()
            local_id = self.local_identity.peer_id if self.local_identity else ''
            announced = 0
            for pid in connected:
                if pid == new_peer_id or pid == local_id:
                    continue
                await self.message_router.send_peer_announcement(pid, new_peer_info)
                announced += 1
            if announced:
                logger.info(f"Announced new peer {new_peer_id} to {announced} existing peer(s)")
        except Exception as e:
            logger.error(f"Error announcing new peer to others: {e}", exc_info=True)

    def store_introduced_peers(self, introduced_peers: list,
                                from_peer: str) -> None:
        """Store peers introduced by a connected peer.

        IMPORTANT: This also registers the peer's public keys in the
        identity manager so that messages relayed from those peers can
        be verified.  Without this, relayed messages from indirectly-
        connected peers would fail signature verification and be
        silently dropped.
        """
        import base58

        for p in introduced_peers:
            pid = p.get('peer_id')
            if not pid:
                continue
            # Skip ourselves
            if self.local_identity and pid == self.local_identity.peer_id:
                continue
            # Skip already-connected peers
            if self.connection_manager and self.connection_manager.is_connected(pid):
                continue
            # Sanitize endpoints so we don't persist unusable (127.*, 0.0.0.0, etc)
            # and keep historical endpoints announced by other introducers.
            existing = self._introduced_peers.get(pid, {})
            existing_eps = existing.get('endpoints', []) if isinstance(existing, dict) else []
            combined_eps = []
            if isinstance(existing_eps, list):
                combined_eps.extend(existing_eps)
            if isinstance(p.get('endpoints', []), list):
                combined_eps.extend(p.get('endpoints', []))
            endpoints = self._sanitize_endpoints(pid, combined_eps)

            # Preserve prior metadata when possible, then overlay latest data.
            cleaned = dict(existing) if isinstance(existing, dict) else {}
            cleaned.update(dict(p))
            cleaned['endpoints'] = endpoints
            cleaned['introduced_by'] = from_peer
            cleaned['capabilities'] = self._normalize_capability_items(cleaned.get('capabilities'))

            # Track all known introducers for more reliable broker fallback.
            introduced_via: list[str] = []
            prior_via = existing.get('introduced_via', []) if isinstance(existing, dict) else []
            if isinstance(prior_via, list):
                for via in prior_via:
                    if isinstance(via, str) and via and via not in introduced_via:
                        introduced_via.append(via)
            prior_single = existing.get('introduced_by') if isinstance(existing, dict) else None
            if isinstance(prior_single, str) and prior_single and prior_single not in introduced_via:
                introduced_via.append(prior_single)
            if from_peer not in introduced_via:
                introduced_via.append(from_peer)
            cleaned['introduced_via'] = introduced_via

            self._introduced_peers[pid] = cleaned
            peer_version_entry = dict(self.peer_versions.get(pid, {}))
            if cleaned.get('capabilities'):
                peer_version_entry['capabilities'] = list(cleaned['capabilities'])
            self.peer_versions[pid] = peer_version_entry

            # Register the peer's public keys in the identity manager
            # so we can verify relayed messages from this peer.
            ed25519_key_b58 = p.get('ed25519_public_key')
            x25519_key_b58 = p.get('x25519_public_key')
            if ed25519_key_b58 and x25519_key_b58:
                try:
                    ed25519_bytes = base58.b58decode(ed25519_key_b58)
                    x25519_bytes = base58.b58decode(x25519_key_b58)
                    display_name = p.get('display_name', '')
                    self.identity_manager.create_remote_peer(
                        peer_id=pid,
                        ed25519_public_key=ed25519_bytes,
                        x25519_public_key=x25519_bytes,
                        endpoints=endpoints,
                        display_name=display_name,
                    )
                    logger.info(f"Registered public keys for introduced "
                                f"peer {pid} ({display_name})")
                except Exception as e:
                    logger.warning(f"Could not register keys for "
                                   f"introduced peer {pid}: {e}")
            else:
                # Persist endpoint info even without keys
                display_name = p.get('display_name', '')
                if endpoints:
                    self.identity_manager.peer_endpoints[pid] = endpoints
                if display_name:
                    self.identity_manager.peer_display_names[pid] = display_name

        if introduced_peers:
            self.identity_manager._save_known_peers()
            logger.info(f"Stored {len(introduced_peers)} introduced peer(s) from {from_peer}")

    def get_introduced_peers(self) -> list:
        """Return the list of peers introduced by our contacts."""
        if not hasattr(self, '_introduced_peers'):
            self._introduced_peers = {}
        return list(self._introduced_peers.values())

    # ------------------------------------------------------------------ #
    #  Connection brokering and relay                                      #
    # ------------------------------------------------------------------ #

    def _on_broker_request(self, target_peer: str,
                           requester_endpoints: list,
                           requester_keys: dict,
                           from_peer: str) -> None:
        """Handle a BROKER_REQUEST: peer asks us to help it connect to target_peer.

        If we are connected to target_peer, forward a BROKER_INTRO with
        the requester's endpoints so target_peer can connect back.
        """
        if self.relay_policy == 'off':
            logger.info(f"Broker request from {from_peer} declined (relay_policy=off)")
            return

        # Privacy-first relay posture: unknown peers do not get broker help.
        if self.get_trust_score:
            try:
                score = self.get_trust_score(from_peer)
                threshold = max(
                    1,
                    int(getattr(getattr(self.config, 'security', None), 'trust_threshold', 50) or 50),
                )
                if score < threshold:
                    logger.warning(f"Broker request from {from_peer} declined "
                                   f"(trust score {score} < {threshold})")
                    return
            except Exception:
                pass

        logger.info(f"Broker request from {from_peer}: wants to reach {target_peer}")

        if not self.connection_manager or not self.connection_manager.is_connected(target_peer):
            logger.warning(f"Cannot broker: not connected to {target_peer}")
            # If full relay, offer to relay instead
            if self.relay_policy == 'full_relay':
                self._schedule_relay_offers(from_peer, target_peer)
            return

        # Forward a BROKER_INTRO to the target
        if self._event_loop and not self._event_loop.is_closed() and self.message_router:
            asyncio.run_coroutine_threadsafe(
                self.message_router.send_broker_intro(
                    to_peer=target_peer,
                    requester_peer_id=from_peer,
                    requester_endpoints=requester_endpoints,
                    requester_keys=requester_keys,
                ),
                self._event_loop
            )
            logger.info(f"Forwarded broker intro for {from_peer} to {target_peer}")

            # If full_relay, also proactively offer relay as a fallback
            # in case the reverse connection also fails
            if self.relay_policy == 'full_relay':
                self._schedule_relay_offers(from_peer, target_peer)

    def _on_broker_intro(self, requester_peer_id: str,
                         requester_endpoints: list,
                         requester_keys: dict,
                         from_peer: str) -> None:
        """Handle a BROKER_INTRO: an intermediary tells us a peer wants to connect.

        Attempt to connect directly to the requester using the provided endpoints.
        """
        logger.info(f"Broker intro from {from_peer}: {requester_peer_id} wants to connect")

        # Register the requester's identity if we have their keys
        if requester_keys and requester_peer_id:
            try:
                import base58
                epk = requester_keys.get('ed25519_public_key', '')
                xpk = requester_keys.get('x25519_public_key', '')
                if epk and xpk:
                    ed_bytes = base58.b58decode(epk)
                    x_bytes = base58.b58decode(xpk)
                    cleaned_eps = self._sanitize_endpoints(requester_peer_id, requester_endpoints)
                    self.identity_manager.create_remote_peer(
                        requester_peer_id, ed_bytes, x_bytes,
                        endpoints=cleaned_eps)
            except Exception as e:
                logger.warning(f"Could not register broker-introduced peer: {e}")

        # Attempt to connect to each endpoint
        if self._event_loop and not self._event_loop.is_closed():
            asyncio.run_coroutine_threadsafe(
                self._attempt_brokered_connection(
                    requester_peer_id, requester_endpoints, broker_peer=from_peer),
                self._event_loop
            )

    async def _attempt_brokered_connection(self, peer_id: str,
                                            endpoints: list,
                                            broker_peer: Optional[str] = None) -> None:
        """Try each endpoint to establish a direct connection.
        
        Args:
            peer_id: Peer we are trying to reach
            endpoints: List of ws:// endpoints to try
            broker_peer: The intermediary that forwarded the intro
        """
        if not self.connection_manager:
            return
        if self.connection_manager.is_connected(peer_id):
            logger.info(f"Already connected to {peer_id}, skipping brokered connect")
            return

        endpoints = self._sanitize_endpoints(peer_id, endpoints)
        for ep in endpoints:
            try:
                logger.info(f"Brokered connect attempt to {peer_id} via {ep}")
                await self._connect_to_endpoint(peer_id, ep)
                if self.connection_manager.is_connected(peer_id):
                    logger.info(f"Brokered connection to {peer_id} succeeded!")
                    # Remove any relay route since we now have a direct connection
                    if self.message_router:
                        self.message_router.remove_route(peer_id)
                        self._active_relays.pop(peer_id, None)
                    await self._run_post_connect_sync(peer_id)
                    return
            except Exception as e:
                logger.warning(f"Brokered connect to {ep} failed: {e}")
                continue

        logger.warning(f"All brokered connection attempts to {peer_id} failed")
        # If broker peer is known and set to full_relay, the broker would have
        # already decided to send relay offers. Nothing more for us to do here.

    def _on_relay_offer(self, relay_peer: str, target_peer: str) -> None:
        """Handle a RELAY_OFFER: a peer offers to relay our traffic to target_peer.

        Populate the routing table so messages for target_peer go through relay_peer.
        """
        if not self.message_router:
            return

        # Don't accept relay to ourselves
        if self.local_identity and target_peer == self.local_identity.peer_id:
            return

        # Don't need relay if already directly connected
        if self.connection_manager and self.connection_manager.is_connected(target_peer):
            logger.debug(f"Declining relay offer for {target_peer}: already connected")
            return

        logger.info(f"Accepted relay offer from {relay_peer} for {target_peer}")
        self.message_router.update_routing_table(target_peer, relay_peer)
        self._active_relays[target_peer] = relay_peer

        # Immediately attempt direct connection in the background.
        # If it succeeds the relay route is replaced by a real link.
        self._try_promote_direct(target_peer)

    def _try_promote_direct(self, peer_id: str) -> None:
        """Background task: try to upgrade a relay route to a direct connection."""
        if not self._event_loop or self._event_loop.is_closed():
            return
        endpoints = self._get_connectable_peer_endpoints(peer_id, prefer_discovered=True)
        if not endpoints:
            return
        connection_manager = self.connection_manager
        if not connection_manager:
            return

        async def _promote():
            await asyncio.sleep(2)  # brief delay to let relay settle
            if connection_manager.is_connected(peer_id):
                logger.debug(f"Promote: {peer_id} already directly connected")
                return
            for ep in endpoints:
                try:
                    ok = await self._connect_to_endpoint(peer_id, ep)
                    if ok:
                        self._active_relays.pop(peer_id, None)
                        logger.info(
                            f"Promoted {peer_id} from relay to direct via {ep}"
                        )
                        await self._run_post_connect_sync(peer_id)
                        return
                except Exception as e:
                    logger.debug(f"Promote direct {ep} for {peer_id}: {e}")
            logger.debug(f"Promote: could not establish direct to {peer_id}")

        asyncio.run_coroutine_threadsafe(_promote(), self._event_loop)

    def _schedule_relay_offers(self, peer_a: str, peer_b: str) -> None:
        """Send RELAY_OFFER to both peers so they can route through us."""
        if not self._event_loop or self._event_loop.is_closed():
            return
        if not self.message_router:
            return
        if not self.connection_manager:
            return
        connection_manager = self.connection_manager
        message_router = self.message_router

        async def _send_offers():
            try:
                if connection_manager.is_connected(peer_a):
                    await message_router.send_relay_offer(peer_a, peer_b)
                    logger.info(f"Sent relay offer to {peer_a} for {peer_b}")
                if connection_manager.is_connected(peer_b):
                    await message_router.send_relay_offer(peer_b, peer_a)
                    logger.info(f"Sent relay offer to {peer_b} for {peer_a}")
            except Exception as e:
                logger.error(f"Error sending relay offers: {e}", exc_info=True)

        asyncio.run_coroutine_threadsafe(_send_offers(), self._event_loop)

    def send_broker_request(self, target_peer_id: str,
                            via_peer_id: str) -> bool:
        """Public method: ask via_peer to broker a connection to target_peer.

        Called from the API layer when a direct connection attempt fails.
        Returns True if the request was successfully scheduled.
        """
        if not self._running or not self._event_loop:
            return False
        if not self.message_router:
            return False
        if not self.local_identity:
            return False

        import base58
        local_id = self.local_identity
        requester_keys = {
            'ed25519_public_key': base58.b58encode(local_id.ed25519_public_key).decode(),
            'x25519_public_key': base58.b58encode(local_id.x25519_public_key).decode(),
        }

        # Gather our own endpoints
        mesh_port = self.config.network.mesh_port
        requester_endpoints = []
        try:
            from .invite import get_local_ips
            for ip in get_local_ips():
                requester_endpoints.append(f"{self.ws_scheme}://{ip}:{mesh_port}")
        except Exception:
            requester_endpoints.append(f"{self.ws_scheme}://0.0.0.0:{mesh_port}")

        future = asyncio.run_coroutine_threadsafe(
            self.message_router.send_broker_request(
                to_peer=via_peer_id,
                target_peer=target_peer_id,
                requester_endpoints=requester_endpoints,
                requester_keys=requester_keys,
            ),
            self._event_loop
        )
        try:
            result = bool(future.result(timeout=5.0))
            if result:
                logger.info(f"Broker request sent to {via_peer_id} for {target_peer_id}")
                self._record_connection_event(
                    target_peer_id,
                    status='broker',
                    detail=f"Broker request sent via {via_peer_id[:8]}",
                    via_peer=via_peer_id,
                )
            else:
                logger.warning(
                    f"Broker request via {via_peer_id} for {target_peer_id} "
                    f"was not routed immediately"
                )
                self._record_connection_event(
                    target_peer_id,
                    status='pending',
                    detail=f"Broker request not routed immediately via {via_peer_id[:8]}",
                    via_peer=via_peer_id,
                )
            return result
        except Exception as e:
            logger.error(f"Error sending broker request: {e}", exc_info=True)
            return False

    def on_peer_disconnected_cleanup(self, peer_id: str) -> None:
        """Clean up routing table entries when a peer disconnects, then schedule auto-reconnect."""
        self.peer_versions.pop(peer_id, None)
        self._record_connection_event(
            peer_id,
            status='disconnected',
            detail='Peer disconnected',
        )
        removed = 0
        if self.message_router:
            removed = self.message_router.cleanup_routes_via(peer_id) or 0
        if removed:
            # Also clean up active relay tracking
            to_remove = [dest for dest, relay in self._active_relays.items() if relay == peer_id]
            for dest in to_remove:
                del self._active_relays[dest]

        # If a reconnect loop is already scheduled/running for this peer,
        # don't restart it (that resets backoff and can cause thrash).
        existing = self._reconnect_tasks.get(peer_id)
        if existing is not None:
            try:
                if hasattr(existing, 'done') and not existing.done():
                    logger.debug(f"Peer {peer_id} disconnected — reconnect already scheduled")
                    return
            except Exception:
                pass
            # Stale/done entry — drop it so we can reschedule.
            self._reconnect_tasks.pop(peer_id, None)

        endpoints = self._get_connectable_peer_endpoints(peer_id, prefer_discovered=True)

        if endpoints and self._running:
            logger.info(
                f"Peer {peer_id} disconnected — scheduling auto-reconnect "
                f"({len(endpoints)} endpoint(s))"
            )
            self._schedule_reconnect(peer_id, attempt=1)

    def set_relay_policy(self, policy: str) -> bool:
        """Update the relay policy. Valid values: 'off', 'broker_only', 'full_relay'.
        
        Persists the setting to a file so it survives restarts.
        """
        if policy not in ('off', 'broker_only', 'full_relay'):
            return False
        old_policy = self.relay_policy
        self.relay_policy = policy
        logger.info(f"Relay policy changed: {old_policy} -> {policy}")
        # Persist to settings file
        try:
            import json
            settings_path = Path(self.config.storage.database_path).parent / 'relay_settings.json'
            settings_path.write_text(json.dumps({'relay_policy': policy}))
            logger.info(f"Relay policy persisted to {settings_path}")
        except Exception as e:
            logger.warning(f"Could not persist relay policy: {e}")
        return True

    def _load_persisted_relay_policy(self) -> None:
        """Load relay policy from persisted settings file."""
        try:
            import json
            settings_path = Path(self.config.storage.database_path).parent / 'relay_settings.json'
            if settings_path.exists():
                data = json.loads(settings_path.read_text())
                policy = data.get('relay_policy', '')
                if policy in ('off', 'broker_only', 'full_relay'):
                    self.relay_policy = policy
                    logger.info(f"Loaded persisted relay policy: {policy}")
        except Exception as e:
            logger.warning(f"Could not load persisted relay policy: {e}")

    def get_relay_status(self) -> Dict[str, Any]:
        """Return current relay status for API/UI."""
        routing = {}
        if self.message_router:
            routing = dict(self.message_router.routing_table)
        return {
            'relay_policy': self.relay_policy,
            'active_relays': dict(self._active_relays),
            'routing_table': routing,
        }

    async def _handle_p2p_message(self, connection: PeerConnection, data: Dict[str, Any]) -> None:
        """
        Handle incoming P2P message.
        
        Args:
            connection: Source connection
            data: Message data
        """
        try:
            if not self.message_router:
                return
            message_dict = data.get('message')
            if not message_dict:
                logger.warning("P2P message missing 'message' field")
                return
            
            # Parse message
            message = P2PMessage.from_dict(message_dict)
            # Keep the immediate upstream peer on the in-memory message object
            # so routing can avoid noisy bounce-backs while relaying.
            setattr(message, '_via_peer', connection.peer_id)
            
            logger.debug(f"Received {message.type.value} message from "
                         f"{message.from_peer} via {connection.peer_id}")
            
            # Verify signature.
            # Default: reject unverifiable messages, including relayed traffic.
            # Optional compatibility mode can temporarily allow trusted relays.
            verified = self.message_router.verify_message(message)
            if not verified:
                relay_peer = connection.peer_id
                is_relayed = (message.from_peer != relay_peer)
                is_trusted_relay = (
                    is_relayed
                    and self.connection_manager
                    and self.connection_manager.is_connected(relay_peer)
                )
                if self.allow_unverified_relay_messages and is_trusted_relay:
                    logger.warning(
                        f"Compatibility mode enabled: accepting unverifiable "
                        f"relayed {message.type.value} from {message.from_peer} "
                        f"via {relay_peer}")
                    self._record_activity_event({
                        'id': f"security_unverified_allow_{int(time.time() * 1000)}",
                        'kind': 'security',
                        'timestamp': time.time(),
                        'status': 'warning',
                        'peer_id': message.from_peer,
                        'detail': (
                            f"Accepted unverifiable relayed {message.type.value} "
                            f"via {relay_peer} (compat mode)"
                        ),
                    })
                else:
                    logger.warning(
                        f"Invalid signature on message {message.id} "
                        f"from {message.from_peer}; dropped")
                    self._record_activity_event({
                        'id': f"security_unverified_drop_{int(time.time() * 1000)}",
                        'kind': 'security',
                        'timestamp': time.time(),
                        'status': 'blocked',
                        'peer_id': message.from_peer,
                        'detail': (
                            f"Dropped {message.type.value} with invalid signature "
                            f"(via {relay_peer})"
                        ),
                    })
                    return
            
            # Route message
            try:
                relay_peer = connection.peer_id
                source_peer = message.from_peer
                if (
                    relay_peer
                    and source_peer
                    and relay_peer != source_peer
                    and self.message_router
                    and self.connection_manager
                    and not self.connection_manager.is_connected(source_peer)
                ):
                    self.message_router.update_routing_table(source_peer, relay_peer)
                    previous = self._active_relays.get(source_peer)
                    self._active_relays[source_peer] = relay_peer
                    if previous != relay_peer:
                        logger.info(f"Learned relay route to {source_peer} via {relay_peer}")
            except Exception:
                pass

            await self.message_router.route_message(message)
            
            # Notify application
            if self.on_message_received:
                self.on_message_received(message)
                
        except Exception as e:
            logger.error(f"Error handling P2P message: {e}", exc_info=True)
    
    def send_message_to_peer(self, peer_id: str, content: str, 
                            metadata: Optional[Dict] = None) -> bool:
        """
        Send a message to a specific peer.
        
        Args:
            peer_id: Target peer ID
            content: Message content
            metadata: Optional metadata
            
        Returns:
            True if sent successfully
        """
        if not self._running or not self._event_loop:
            logger.warning("P2P network not running")
            return False
        if not self.message_router:
            return False
        
        # Schedule coroutine
        future = asyncio.run_coroutine_threadsafe(
            self.message_router.send_direct_message(peer_id, content, metadata),
            self._event_loop
        )
        
        try:
            return future.result(timeout=5.0)
        except Exception as e:
            logger.error(f"Error sending message: {e}", exc_info=True)
            return False
    
    def broadcast_message(self, content: str, metadata: Optional[Dict] = None) -> bool:
        """
        Broadcast a message to all connected peers.
        
        Args:
            content: Message content
            metadata: Optional metadata
            
        Returns:
            True if sent successfully
        """
        if not self._running or not self._event_loop:
            logger.warning("P2P network not running")
            return False
        if not self.message_router:
            return False
        
        # Schedule coroutine
        future = asyncio.run_coroutine_threadsafe(
            self.message_router.send_broadcast(content, metadata),
            self._event_loop
        )
        
        try:
            return future.result(timeout=5.0)
        except Exception as e:
            logger.error(f"Error broadcasting message: {e}", exc_info=True)
            return False

    def _get_channel_send_context(self, channel_id: str) -> Dict[str, Any]:
        """Load privacy + crypto settings for one channel."""
        context: Dict[str, Any] = {
            'privacy_mode': 'open',
            'origin_peer': None,
            'crypto_mode': 'legacy_plaintext',
            'post_policy': 'open',
            'allow_member_replies': True,
            'allowed_poster_user_ids': [],
        }
        if not channel_id:
            return context
        try:
            with self.db.get_connection() as conn:
                row = conn.execute(
                    """
                    SELECT privacy_mode, origin_peer, crypto_mode,
                           COALESCE(post_policy, 'open') AS post_policy,
                           COALESCE(allow_member_replies, 1) AS allow_member_replies
                    FROM channels
                    WHERE id = ?
                    """,
                    (channel_id,),
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
            if row:
                context['privacy_mode'] = (row['privacy_mode'] or 'open').strip().lower()
                context['origin_peer'] = row['origin_peer']
                context['crypto_mode'] = (row['crypto_mode'] or 'legacy_plaintext').strip().lower()
                context['post_policy'] = (row['post_policy'] or 'open').strip().lower()
                context['allow_member_replies'] = bool(row['allow_member_replies'])
            context['allowed_poster_user_ids'] = [
                str(allowed_row['user_id'])
                for allowed_row in allowed_rows
                if allowed_row and allowed_row['user_id']
            ]
        except Exception as e:
            logger.debug(f"Could not load channel context for {channel_id}: {e}")
        return context

    def _get_active_channel_key_bytes(self, channel_id: str) -> Optional[Dict[str, Any]]:
        """Return active channel key as raw bytes (if locally available)."""
        if not channel_id:
            return None
        try:
            with self.db.get_connection() as conn:
                row = conn.execute(
                    """
                    SELECT key_id, key_material_enc, metadata
                    FROM channel_keys
                    WHERE channel_id = ? AND revoked_at IS NULL
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    (channel_id,),
                ).fetchone()
            if not row:
                return None
            key_bytes = decode_channel_key_material(row['key_material_enc'])
            if not key_bytes:
                return None
            meta = {}
            if row['metadata']:
                try:
                    meta = json.loads(row['metadata'])
                except Exception:
                    meta = {}
            return {
                'key_id': row['key_id'],
                'key_material': key_bytes,
                'metadata': meta,
            }
        except Exception as e:
            logger.debug(f"Could not load active key for {channel_id}: {e}")
            return None

    def _persist_local_encrypted_message(
        self,
        message_id: str,
        encrypted_content: str,
        nonce: str,
        key_id: str,
    ) -> None:
        """Store encrypted payload metadata for local catch-up replay."""
        if not message_id:
            return
        try:
            with self.db.get_connection() as conn:
                conn.execute(
                    """
                    UPDATE channel_messages
                    SET encrypted_content = ?,
                        crypto_state = 'decrypted',
                        key_id = ?,
                        nonce = ?
                    WHERE id = ?
                    """,
                    (encrypted_content, key_id, nonce, message_id),
                )
                conn.commit()
        except Exception as e:
            logger.debug(
                f"Could not persist encrypted metadata for message {message_id}: {e}"
            )
    
    def broadcast_channel_message(self, channel_id: str, user_id: str,
                                   content: str, message_id: str,
                                   timestamp: str,
                                   attachments: Optional[list[Any]] = None,
                                   display_name: Optional[str] = None,
                                   expires_at: Optional[str] = None,
                                   ttl_seconds: Optional[int] = None,
                                   ttl_mode: Optional[str] = None,
                                   update_only: bool = False,
                                   parent_message_id: Optional[str] = None,
                                   edited_at: Optional[str] = None,
                                   security: Optional[dict[Any, Any]] = None,
                                   target_peer_ids: Optional[set[Any]] = None) -> bool:
        """
        Broadcast a channel message to all connected peers so they
        can store it locally and display it in their UI.
        
        If attachments are provided and file_manager is set, the actual
        file bytes are base64-encoded and included in the payload so
        receiving peers can save files locally.
        """
        if not self._running or not self._event_loop:
            logger.warning("P2P network not running, cannot broadcast channel message")
            return False

        if not self.message_router:
            return False

        sec_cfg = getattr(self.config, 'security', None)
        e2e_enabled = bool(getattr(sec_cfg, 'e2e_private_channels', False))
        e2e_enforce = bool(getattr(sec_cfg, 'e2e_private_channels_enforce', False))

        channel_ctx = self._get_channel_send_context(channel_id)
        privacy_mode = str(channel_ctx.get('privacy_mode') or 'open').lower()
        channel_crypto_mode = str(channel_ctx.get('crypto_mode') or 'legacy_plaintext').lower()
        targeted_channel = privacy_mode in {'private', 'confidential'}
        e2e_channel_mode = channel_crypto_mode in {'e2e_optional', 'e2e_enforced'}
        should_encrypt = bool(e2e_enabled and targeted_channel and e2e_channel_mode)

        outbound_content = content
        encrypted_content: Optional[str] = None
        nonce_b64: Optional[str] = None
        key_id: Optional[str] = None
        if should_encrypt:
            active_key = self._get_active_channel_key_bytes(channel_id)
            if not active_key:
                if e2e_enforce:
                    logger.warning(
                        "Rejecting private channel message %s in %s: missing E2E channel key",
                        message_id,
                        channel_id,
                    )
                    return False
                logger.warning(
                    "Sending plaintext fallback for private channel message %s in %s (missing E2E key)",
                    message_id,
                    channel_id,
                )
            else:
                try:
                    encrypted_content, nonce_b64 = encrypt_with_channel_key(
                        content or '',
                        active_key['key_material'],
                    )
                    key_id = active_key['key_id']
                    outbound_content = ''
                    if message_id and encrypted_content and nonce_b64 and key_id:
                        self._persist_local_encrypted_message(
                            message_id=message_id,
                            encrypted_content=encrypted_content,
                            nonce=nonce_b64,
                            key_id=key_id,
                        )
                except Exception as enc_err:
                    if e2e_enforce:
                        logger.warning(
                            "Rejecting private channel message %s in %s: encryption failed (%s)",
                            message_id,
                            channel_id,
                            enc_err,
                        )
                        return False
                    logger.warning(
                        "Falling back to plaintext for private channel message %s in %s after encryption error: %s",
                        message_id,
                        channel_id,
                        enc_err,
                    )

        metadata: Dict[str, Any] = {
            'type': 'channel_message',
            'channel_id': channel_id,
            'user_id': user_id,
            'message_id': message_id,
            'timestamp': timestamp,
            'expires_at': expires_at,
            'ttl_seconds': ttl_seconds,
            'ttl_mode': ttl_mode,
            'privacy_mode': privacy_mode,
            'crypto_mode': channel_crypto_mode,
            'post_policy': str(channel_ctx.get('post_policy') or 'open').strip().lower(),
            'allow_member_replies': bool(channel_ctx.get('allow_member_replies', True)),
        }
        allowed_poster_user_ids = channel_ctx.get('allowed_poster_user_ids') or []
        if allowed_poster_user_ids:
            metadata['allowed_poster_user_ids'] = [
                str(allowed_user_id)
                for allowed_user_id in allowed_poster_user_ids
                if str(allowed_user_id or '').strip()
            ]
        if should_encrypt and encrypted_content and nonce_b64 and key_id:
            metadata['encrypted_content'] = encrypted_content
            metadata['nonce'] = nonce_b64
            metadata['key_id'] = key_id
            metadata['crypto_state'] = 'encrypted'
        if parent_message_id:
            metadata['parent_message_id'] = parent_message_id
        if 'origin_peer' not in metadata or not metadata.get('origin_peer'):
            try:
                metadata['origin_peer'] = self.get_peer_id()
            except Exception:
                metadata['origin_peer'] = None
        if update_only:
            metadata['update_only'] = True
        if edited_at:
            metadata['edited_at'] = edited_at
        if security:
            metadata['security'] = security

        # Include sender display_name so remote peers can show the
        # correct name even if they haven't received a profile sync
        # for this specific user yet.
        if display_name:
            metadata['display_name'] = display_name

        # Embed file data for each attachment so peers can store locally.
        # Include original file_id so receivers can rewrite /files/ORIGINAL in content to /files/LOCAL.
        if attachments:
            p2p_attachments = []
            for att in attachments:
                att_entry = self._build_p2p_attachment_entry(att)
                if att_entry:
                    p2p_attachments.append(att_entry)

            metadata['attachments'] = p2p_attachments
            metadata['message_type'] = 'file'

        # Broadcast all channel messages (including restricted) to the
        # full mesh so intermediary peers can relay.  Content
        # confidentiality for private channels will be enforced via
        # E2E encryption; targeted-only sending was removed to fix
        # relay gaps when member peers are not directly connected.
        timeout = 60.0 if attachments else 5.0
        future = asyncio.run_coroutine_threadsafe(
            self.message_router.send_channel_broadcast(outbound_content, metadata),
            self._event_loop
        )
        try:
            return future.result(timeout=timeout)
        except Exception as e:
            logger.error(f"Error broadcasting channel message: {e}", exc_info=True)
            return False

    def broadcast_feed_post(self, post_id: str, author_id: str,
                             content: str, post_type: str = 'text',
                             visibility: str = 'network',
                             previous_visibility: Optional[str] = None,
                             timestamp: Optional[str] = None,
                             metadata: Optional[dict[Any, Any]] = None,
                             expires_at: Optional[str] = None,
                             ttl_seconds: Optional[int] = None,
                             ttl_mode: Optional[str] = None,
                             display_name: Optional[str] = None) -> bool:
        """
        Broadcast a feed post to all connected peers so they
        can store it locally and display it in their feed.
        """
        if not self._running or not self._event_loop:
            logger.warning("P2P network not running, cannot broadcast feed post")
            return False

        if not self.message_router:
            return False
        visibility_mode = str(visibility or 'private').strip().lower() or 'private'
        previous_visibility_mode = str(previous_visibility or visibility_mode).strip().lower() or visibility_mode
        meta: Dict[str, Any] = {
            'type': 'feed_post',
            'post_id': post_id,
            'author_id': author_id,
            'post_type': post_type,
            'visibility': visibility_mode,
            'timestamp': timestamp,
            'metadata': metadata or {},
            'expires_at': expires_at,
            'ttl_seconds': ttl_seconds,
            'ttl_mode': ttl_mode,
        }

        try:
            if meta['metadata'] is None:
                meta['metadata'] = {}
            if isinstance(meta['metadata'], dict) and not meta['metadata'].get('origin_peer'):
                meta['metadata']['origin_peer'] = self.get_peer_id()
        except Exception:
            pass

        if display_name:
            meta['display_name'] = display_name

        # Embed file data for feed attachments so peers can render locally
        try:
            meta_metadata = meta.get('metadata')
            attachments = meta_metadata.get('attachments') if isinstance(meta_metadata, dict) else []
            attachments = attachments or []
            if attachments:
                enriched = []
                for att in attachments:
                    entry = self._build_p2p_attachment_entry(att)
                    if entry:
                        enriched.append(entry)
                if isinstance(meta_metadata, dict):
                    meta_metadata['attachments'] = enriched
        except Exception as e:
            logger.debug(f"Feed attachment embedding failed: {e}")

        target_peers, revoke_peers = self._get_feed_post_target_delta(
            previous_visibility_mode,
            visibility_mode,
        )
        if visibility_mode == 'trusted' and not target_peers:
            logger.info(
                "Feed post %s visibility=trusted has no trusted connected peers; keeping local only",
                post_id,
            )

        async def _send_feed_post() -> bool:
            sent_any = False
            if visibility_mode in {'public', 'network'}:
                sent_any = await self.message_router.send_feed_post_broadcast(content, meta)
            else:
                for peer_id in target_peers:
                    payload = {
                        'content': content,
                        'metadata': dict(meta),
                    }
                    message = self.message_router.create_message(
                        MessageType.FEED_POST,
                        peer_id,
                        payload,
                        ttl=getattr(self.message_router, '_CONTENT_TTL', 5),
                    )
                    self.message_router.sign_message(message)
                    if await self.message_router._route_to_peer(message):
                        sent_any = True

            revoked_any = False
            if revoke_peers:
                for peer_id in revoke_peers:
                    signal_id = secrets.token_hex(12)
                    if await self.message_router.send_delete_signal(
                        signal_id=signal_id,
                        data_type='feed_post',
                        data_id=post_id,
                        reason=f"visibility_narrowed:{previous_visibility_mode}->{visibility_mode}",
                        target_peer=peer_id,
                    ):
                        revoked_any = True
                logger.info(
                    "Feed post %s revoked from %d peer(s) due to visibility change %s -> %s",
                    post_id,
                    len(revoke_peers),
                    previous_visibility_mode,
                    visibility_mode,
                )
            if visibility_mode in {'private', 'custom'}:
                return revoked_any or not revoke_peers
            return sent_any or revoked_any

        future = asyncio.run_coroutine_threadsafe(_send_feed_post(), self._event_loop)

        try:
            return future.result(timeout=5.0)
        except Exception as e:
            logger.error(f"Error broadcasting feed post: {e}", exc_info=True)
            return False

    def _get_feed_post_target_peers(self, visibility: str) -> list[str]:
        """Return connected peer targets for a feed post visibility mode."""
        visibility_mode = str(visibility or 'private').strip().lower() or 'private'
        if visibility_mode in {'private', 'custom'}:
            return []
        peers = list(self.get_connected_peers())
        if visibility_mode in {'public', 'network'}:
            return peers
        if visibility_mode != 'trusted' or not self.get_trust_score:
            return []
        threshold = max(
            1,
            int(getattr(getattr(self.config, 'security', None), 'trust_threshold', 50) or 50),
        )
        trusted_peers: list[str] = []
        for peer_id in peers:
            if not peer_id:
                continue
            try:
                if int(self.get_trust_score(peer_id)) >= threshold:
                    trusted_peers.append(peer_id)
            except Exception:
                continue
        return trusted_peers

    def _get_feed_post_target_delta(
        self,
        previous_visibility: str,
        visibility: str,
    ) -> tuple[list[str], list[str]]:
        """Return (target_peers, revoke_peers) for a feed visibility change."""
        target_peers = self._get_feed_post_target_peers(visibility)
        previous_targets = self._get_feed_post_target_peers(previous_visibility)
        if not previous_targets:
            return target_peers, []
        target_set = set(target_peers)
        revoke_peers = [peer_id for peer_id in previous_targets if peer_id not in target_set]
        return target_peers, revoke_peers

    def broadcast_interaction(self, item_id: str, user_id: str,
                               action: str, item_type: str = 'post',
                               display_name: Optional[str] = None,
                               extra: Optional[dict[Any, Any]] = None) -> bool:
        """
        Broadcast a like/unlike interaction to all connected peers
        so they can apply it locally (idempotent).
        """
        if not self._running or not self._event_loop:
            logger.warning("P2P network not running, cannot broadcast interaction")
            return False

        if not self.message_router:
            return False
        meta: Dict[str, Any] = {
            'type': 'interaction',
            'item_id': item_id,
            'user_id': user_id,
            'action': action,
            'item_type': item_type,
        }
        if extra:
            try:
                meta.update(extra)
            except Exception:
                pass
        if 'origin_peer' not in meta or not meta.get('origin_peer'):
            try:
                meta['origin_peer'] = self.get_peer_id()
            except Exception:
                meta['origin_peer'] = None

        if display_name:
            meta['display_name'] = display_name

        future = asyncio.run_coroutine_threadsafe(
            self.message_router.send_interaction_broadcast(meta),
            self._event_loop
        )

        try:
            return future.result(timeout=5.0)
        except Exception as e:
            logger.error(f"Error broadcasting interaction: {e}", exc_info=True)
            return False

    def broadcast_direct_message(self, sender_id: str, recipient_id: str,
                                  content: str, message_id: str,
                                  timestamp: str,
                                  display_name: Optional[str] = None,
                                  metadata: Optional[dict[Any, Any]] = None,
                                  update_only: bool = False,
                                  edited_at: Optional[str] = None) -> bool:
        """
        Broadcast a direct message to all connected peers.
        The recipient peer stores it; others ignore it.
        """
        if not self._running or not self._event_loop:
            logger.warning("P2P network not running, cannot broadcast DM")
            return False

        if not self.message_router:
            return False
        local_peer_id = self.local_identity.peer_id if self.local_identity else ''
        recipient_row = self._get_dm_recipient_row(recipient_id)
        recipient_origin_peer = str((recipient_row or {}).get('origin_peer') or '').strip()
        recipient_is_local = bool(recipient_row and is_local_dm_user(self.db, self, recipient_id))
        if recipient_is_local:
            logger.debug(
                "Skipping P2P DM broadcast for local recipient %s (message=%s)",
                recipient_id,
                message_id,
            )
            return True

        user_metadata: Dict[str, Any] = dict(metadata or {})
        target_peer_id = recipient_origin_peer if recipient_origin_peer and recipient_origin_peer != local_peer_id else ''
        security_summary = build_dm_security_summary(self.db, self, [recipient_id])
        meta: Dict[str, Any] = {
            'type': 'direct_message',
            'sender_id': sender_id,
            'recipient_id': recipient_id,
            'message_id': message_id,
            'timestamp': timestamp,
        }

        if display_name:
            meta['display_name'] = display_name
        if update_only:
            meta['update_only'] = True
        if edited_at:
            meta['edited_at'] = edited_at

        # Embed file data for DM attachments so recipient can render locally
        try:
            dm_metadata = dict(user_metadata)
            attachments = dm_metadata.get('attachments') if isinstance(dm_metadata, dict) else []
            attachments = attachments or []
            if attachments:
                enriched = []
                for att in attachments:
                    entry = self._build_p2p_attachment_entry(att)
                    if entry:
                        enriched.append(entry)
                if isinstance(dm_metadata, dict):
                    dm_metadata['attachments'] = enriched
            user_metadata = dm_metadata
        except Exception as e:
            logger.debug(f"DM attachment embedding failed: {e}")

        should_encrypt = False
        peer_identity = None
        if target_peer_id:
            peer_identity = self.identity_manager.get_peer(target_peer_id)
            should_encrypt = bool(
                peer_identity
                and self.peer_supports_capability(target_peer_id, DM_E2E_CAPABILITY)
            )

        outbound_content = content
        outbound_metadata = dict(user_metadata)
        if should_encrypt and peer_identity:
            try:
                outbound_content, outbound_metadata, security_summary = encrypt_dm_transport_bundle(
                    content,
                    user_metadata,
                    target_peer_id,
                    peer_identity.x25519_public_key,
                    sender_peer_id=local_peer_id,
                )
            except Exception as enc_err:
                logger.warning(
                    "Falling back to plaintext DM broadcast for %s after E2E preparation failure: %s",
                    message_id,
                    enc_err,
                )
                should_encrypt = False

        if not should_encrypt:
            outbound_metadata = dict(user_metadata)
            outbound_metadata['security'] = dict(security_summary)
            if target_peer_id:
                outbound_metadata['security']['target_peer_id'] = target_peer_id

        meta['metadata'] = outbound_metadata
        future = asyncio.run_coroutine_threadsafe(
            self.message_router.send_dm_broadcast(outbound_content, meta),
            self._event_loop
        )

        # DM sends should not block the request thread on slow or dead peers.
        # Completion still gets logged once the event-loop task finishes.
        def _on_done(f: Any) -> None:
            try:
                result = bool(f.result())
                if result:
                    logger.info(f"Broadcast DM {message_id} from {sender_id} to {recipient_id}")
                else:
                    logger.warning(
                        "DM broadcast %s from %s to %s completed without a live peer delivery",
                        message_id,
                        sender_id,
                        recipient_id,
                    )
            except Exception as exc:
                logger.error(f"Error broadcasting DM: {exc}", exc_info=True)

        if hasattr(future, 'add_done_callback'):
            future.add_done_callback(_on_done)
            return True

        try:
            attachment_count = len((outbound_metadata or {}).get('attachments') or [])
            timeout = 60.0 if attachment_count else 5.0
            result = bool(future.result(timeout=timeout))
            if result:
                logger.info(f"Broadcast DM {message_id} from {sender_id} to {recipient_id}")
            return result
        except Exception as e:
            logger.error(f"Error broadcasting DM: {e}", exc_info=True)
            return False

    def broadcast_delete_signal(self, signal_id: str, data_type: str,
                                data_id: str, reason: Optional[str] = None,
                                target_peer: Optional[str] = None) -> bool:
        """Broadcast (or direct-send) a delete signal via P2P."""
        if not self._running or not self._event_loop:
            logger.warning("P2P network not running, cannot broadcast delete signal")
            return False
        if not self.message_router:
            return False

        future = asyncio.run_coroutine_threadsafe(
            self.message_router.send_delete_signal(
                signal_id=signal_id,
                data_type=data_type,
                data_id=data_id,
                reason=reason,
                target_peer=target_peer,
            ),
            self._event_loop
        )

        try:
            return future.result(timeout=5.0)
        except Exception as e:
            logger.error(f"Error broadcasting delete signal: {e}", exc_info=True)
            return False

    def send_delete_signal_ack(self, to_peer: str, signal_id: str,
                               status: str) -> bool:
        """Send a delete-signal acknowledgment to a specific peer (fire-and-forget)."""
        if not self._running or not self._event_loop:
            return False
        if not self.message_router:
            return False

        future = asyncio.run_coroutine_threadsafe(
            self.message_router.send_delete_signal_ack(to_peer, signal_id, status),
            self._event_loop
        )

        def _on_done(f: Any) -> None:
            try:
                f.result()
            except Exception as exc:
                logger.error("Error sending delete signal ack: %s", exc)

        future.add_done_callback(_on_done)
        return True

    def broadcast_channel_announce(self, channel_id: str, name: str,
                                     channel_type: str, description: str,
                                     privacy_mode: Optional[str] = None,
                                     post_policy: Optional[str] = None,
                                     allow_member_replies: Optional[bool] = None,
                                     allowed_poster_user_ids: Optional[list[Any]] = None,
                                     last_activity_at: Optional[str] = None,
                                     lifecycle_ttl_days: Optional[int] = None,
                                     lifecycle_preserved: Optional[bool] = None,
                                     lifecycle_archived_at: Optional[str] = None,
                                     lifecycle_archive_reason: Optional[str] = None,
                                     created_by_user_id: Optional[str] = None,
                                     member_peer_ids: Optional[set[Any]] = None,
                                     initial_members_by_peer: Optional[dict[Any, Any]] = None) -> bool:
        """
        Broadcast a channel announcement to connected peers.
        
        For open/guarded channels: broadcasts to all peers.
        For private/confidential channels: sends targeted announces only to peers
        that have members, with initial_members specifying which users
        to add on each receiving peer.
        
        Args:
            member_peer_ids: Set of peer IDs to target (private/confidential only).
            initial_members_by_peer: Dict mapping peer_id -> list of user_ids
                to include as initial_members in each targeted announce.
        """
        if not self._running or not self._event_loop:
            logger.warning("P2P network not running, cannot broadcast channel announce")
            return False
        if not self.message_router:
            return False

        peer_id = self.local_identity.peer_id if self.local_identity else 'unknown'
        mode = str(privacy_mode or '').lower()
        is_private = mode in {'private', 'confidential'}
        if is_private:
            target_peers: set[str] = set(member_peer_ids or set())
            target_peers.discard(peer_id)
            if not target_peers:
                try:
                    with self.db.get_connection() as conn:
                        rows = conn.execute(
                            """
                            SELECT DISTINCT u.origin_peer
                            FROM channel_members cm
                            JOIN users u ON cm.user_id = u.id
                            WHERE cm.channel_id = ?
                              AND u.origin_peer IS NOT NULL
                              AND u.origin_peer != ''
                              AND u.origin_peer != ?
                            """,
                            (channel_id, peer_id),
                        ).fetchall()
                    for row in rows or []:
                        op = row['origin_peer'] if hasattr(row, 'keys') and 'origin_peer' in row.keys() else row[0]
                        if op:
                            target_peers.add(str(op))
                except Exception as e:
                    logger.debug(f"Could not derive member peers for targeted announce {channel_id}: {e}")

            if not target_peers:
                logger.debug(
                    "Skipping targeted channel announce for %s (no remote member peers)",
                    channel_id,
                )
                return True

            ok = True
            sent_count = 0
            for target_peer in sorted(target_peers):
                initial_members = None
                if initial_members_by_peer:
                    initial_members = initial_members_by_peer.get(target_peer)
                future = asyncio.run_coroutine_threadsafe(
                    self.message_router.send_channel_announce(
                        channel_id=channel_id,
                        name=name,
                        channel_type=channel_type,
                        description=description or '',
                        privacy_mode=privacy_mode,
                        post_policy=post_policy,
                        allow_member_replies=allow_member_replies,
                        allowed_poster_user_ids=allowed_poster_user_ids,
                        last_activity_at=last_activity_at,
                        lifecycle_ttl_days=lifecycle_ttl_days,
                        lifecycle_preserved=lifecycle_preserved,
                        lifecycle_archived_at=lifecycle_archived_at,
                        lifecycle_archive_reason=lifecycle_archive_reason,
                        created_by_peer=peer_id,
                        created_by_user_id=created_by_user_id,
                        to_peer=target_peer,
                        initial_members=initial_members,
                    ),
                    self._event_loop
                )
                try:
                    sent = future.result(timeout=5.0)
                    ok = ok and bool(sent)
                    if sent:
                        sent_count += 1
                except Exception as e:
                    ok = False
                    logger.error(
                        "Error sending targeted channel announce %s to %s: %s",
                        channel_id,
                        target_peer,
                        e,
                        exc_info=True,
                    )
            logger.info(
                "Targeted channel announce for %s sent to %d peer(s) privacy=%s",
                channel_id,
                sent_count,
                mode,
            )
            return ok

        future = asyncio.run_coroutine_threadsafe(
            self.message_router.send_channel_announce(
                channel_id=channel_id,
                name=name,
                channel_type=channel_type,
                description=description or '',
                privacy_mode=privacy_mode,
                post_policy=post_policy,
                allow_member_replies=allow_member_replies,
                allowed_poster_user_ids=allowed_poster_user_ids,
                last_activity_at=last_activity_at,
                lifecycle_ttl_days=lifecycle_ttl_days,
                lifecycle_preserved=lifecycle_preserved,
                lifecycle_archived_at=lifecycle_archived_at,
                lifecycle_archive_reason=lifecycle_archive_reason,
                created_by_peer=peer_id,
                created_by_user_id=created_by_user_id,
            ),
            self._event_loop
        )
        try:
            return future.result(timeout=5.0)
        except Exception as e:
            logger.error(f"Error broadcasting channel announce: {e}", exc_info=True)
            return False

    def broadcast_member_sync(self, channel_id: str, target_user_id: str,
                               action: str, target_peer_id: str,
                               role: str = 'member',
                               channel_name: str = '',
                               channel_type: str = 'private',
                               channel_description: str = '',
                               privacy_mode: str = 'private',
                               sync_id: Optional[str] = None) -> bool:
        """
        Send a targeted MEMBER_SYNC to a specific peer when a member
        is added/removed from a private channel.
        """
        if not self._running or not self._event_loop:
            logger.warning("P2P network not running, cannot send member sync")
            return False
        if not self.message_router:
            return False

        logger.info(f"Sending member_sync ({action}) for user {target_user_id} "
                     f"in channel {channel_id} to peer {target_peer_id}")
        future = asyncio.run_coroutine_threadsafe(
            self.message_router.send_member_sync(
                to_peer=target_peer_id,
                channel_id=channel_id,
                target_user_id=target_user_id,
                action=action,
                role=role,
                channel_name=channel_name,
                channel_type=channel_type,
                channel_description=channel_description,
                privacy_mode=privacy_mode,
                sync_id=sync_id,
            ),
            self._event_loop
        )
        try:
            return future.result(timeout=5.0)
        except Exception as e:
            logger.error(f"Error sending member sync: {e}", exc_info=True)
            return False

    def send_channel_key_distribution(self, to_peer: str, channel_id: str,
                                       key_id: str, encrypted_key: str,
                                       key_version: int = 1,
                                       rotated_from: Optional[str] = None) -> bool:
        """Send wrapped channel-key material to one peer (fire-and-forget)."""
        if not self._running or not self._event_loop:
            logger.warning("P2P network not running, cannot send channel key distribution")
            return False
        if not self.message_router:
            return False

        future = asyncio.run_coroutine_threadsafe(
            self.message_router.send_channel_key_distribution(
                to_peer=to_peer,
                channel_id=channel_id,
                key_id=key_id,
                encrypted_key=encrypted_key,
                key_version=key_version,
                rotated_from=rotated_from,
            ),
            self._event_loop,
        )

        def _on_done(f: Any) -> None:
            try:
                f.result()
            except Exception as exc:
                logger.error("Error sending channel key distribution: %s", exc)

        future.add_done_callback(_on_done)
        return True

    def send_channel_key_request(self, to_peer: str, channel_id: str,
                                  reason: Optional[str] = None,
                                  key_id: Optional[str] = None) -> bool:
        """Request channel-key distribution/re-send from a peer (fire-and-forget)."""
        if not self._running or not self._event_loop:
            logger.warning("P2P network not running, cannot send channel key request")
            return False
        if not self.message_router:
            return False

        future = asyncio.run_coroutine_threadsafe(
            self.message_router.send_channel_key_request(
                to_peer=to_peer,
                channel_id=channel_id,
                requesting_peer=self.get_peer_id(),
                reason=reason,
                key_id=key_id,
            ),
            self._event_loop,
        )

        def _on_done(f: Any) -> None:
            try:
                f.result()
            except Exception as exc:
                logger.error("Error sending channel key request: %s", exc)

        future.add_done_callback(_on_done)
        return True

    def send_channel_key_ack(self, to_peer: str, channel_id: str,
                              key_id: str, status: str = 'ok',
                              error: Optional[str] = None) -> bool:
        """Acknowledge channel-key import/delivery status (fire-and-forget)."""
        if not self._running or not self._event_loop:
            logger.warning("P2P network not running, cannot send channel key ack")
            return False
        if not self.message_router:
            return False

        future = asyncio.run_coroutine_threadsafe(
            self.message_router.send_channel_key_ack(
                to_peer=to_peer,
                channel_id=channel_id,
                key_id=key_id,
                status=status,
                error=error,
            ),
            self._event_loop,
        )

        def _on_done(f: Any) -> None:
            try:
                f.result()
            except Exception as exc:
                logger.error("Error sending channel key ack: %s", exc)

        future.add_done_callback(_on_done)
        return True

    def send_large_attachment_request(
        self,
        *,
        to_peer: str,
        request_id: str,
        origin_file_id: str,
        source_context: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Request a remote large attachment from its source peer."""
        if not self._running or not self._event_loop or not self.message_router:
            return False
        future = asyncio.run_coroutine_threadsafe(
            self.message_router.send_large_attachment_request(
                to_peer=to_peer,
                request_id=request_id,
                origin_file_id=origin_file_id,
                requester_peer=self.get_peer_id(),
                source_context=source_context or {},
            ),
            self._event_loop,
        )

        def _on_done(f: Any) -> None:
            try:
                f.result()
            except Exception as exc:
                logger.error("Error sending large attachment request: %s", exc)

        future.add_done_callback(_on_done)
        return True

    def send_large_attachment_chunk(
        self,
        *,
        to_peer: str,
        request_id: str,
        origin_file_id: str,
        file_name: str,
        content_type: str,
        checksum: str,
        size: int,
        uploaded_by: Optional[str],
        chunk_index: int,
        total_chunks: int,
        data_b64: str,
    ) -> bool:
        """Send one chunk of a large attachment to a peer."""
        if not self._running or not self._event_loop or not self.message_router:
            return False
        future = asyncio.run_coroutine_threadsafe(
            self.message_router.send_large_attachment_chunk(
                to_peer=to_peer,
                request_id=request_id,
                origin_file_id=origin_file_id,
                file_name=file_name,
                content_type=content_type,
                checksum=checksum,
                size=size,
                uploaded_by=uploaded_by,
                chunk_index=chunk_index,
                total_chunks=total_chunks,
                data_b64=data_b64,
                source_peer_id=self.get_peer_id(),
            ),
            self._event_loop,
        )

        def _on_done(f: Any) -> None:
            try:
                f.result()
            except Exception as exc:
                logger.error("Error sending large attachment chunk: %s", exc)

        future.add_done_callback(_on_done)
        return True

    def send_large_attachment_error(
        self,
        *,
        to_peer: str,
        request_id: str,
        origin_file_id: str,
        error: str,
    ) -> bool:
        """Send a failure marker for a large attachment transfer."""
        if not self._running or not self._event_loop or not self.message_router:
            return False
        future = asyncio.run_coroutine_threadsafe(
            self.message_router.send_large_attachment_error(
                to_peer=to_peer,
                request_id=request_id,
                origin_file_id=origin_file_id,
                error=error,
                source_peer_id=self.get_peer_id(),
            ),
            self._event_loop,
        )

        def _on_done(f: Any) -> None:
            try:
                f.result()
            except Exception as exc:
                logger.error("Error sending large attachment error: %s", exc)

        future.add_done_callback(_on_done)
        return True

    def send_principal_announce(
        self,
        to_peer: str,
        principal: Dict[str, Any],
        keys: Optional[list[Dict[str, Any]]] = None,
    ) -> bool:
        """Send identity portability principal metadata to a peer."""
        if not self._running or not self._event_loop or not self.message_router:
            return False
        future = asyncio.run_coroutine_threadsafe(
            self.message_router.send_principal_announce(
                to_peer=to_peer,
                principal=principal or {},
                keys=keys or [],
            ),
            self._event_loop,
        )

        def _on_done(f: Any) -> None:
            try:
                f.result()
            except Exception as exc:
                logger.error("Error sending principal announce: %s", exc)

        future.add_done_callback(_on_done)
        return True

    def send_principal_key_update(
        self,
        to_peer: str,
        principal_id: str,
        key: Dict[str, Any],
    ) -> bool:
        """Send identity portability principal key update to a peer."""
        if not self._running or not self._event_loop or not self.message_router:
            return False
        future = asyncio.run_coroutine_threadsafe(
            self.message_router.send_principal_key_update(
                to_peer=to_peer,
                principal_id=principal_id,
                key=key or {},
            ),
            self._event_loop,
        )

        def _on_done(f: Any) -> None:
            try:
                f.result()
            except Exception as exc:
                logger.error("Error sending principal key update: %s", exc)

        future.add_done_callback(_on_done)
        return True

    def send_bootstrap_grant_sync(self, to_peer: str, grant: Dict[str, Any]) -> bool:
        """Send bootstrap grant artifact to a peer."""
        if not self._running or not self._event_loop or not self.message_router:
            return False
        future = asyncio.run_coroutine_threadsafe(
            self.message_router.send_bootstrap_grant_sync(
                to_peer=to_peer,
                grant=grant or {},
            ),
            self._event_loop,
        )

        def _on_done(f: Any) -> None:
            try:
                f.result()
            except Exception as exc:
                logger.error("Error sending bootstrap grant sync: %s", exc)

        future.add_done_callback(_on_done)
        return True

    def send_bootstrap_grant_revoke(
        self,
        to_peer: str,
        grant_id: str,
        revoked_at: str,
        reason: Optional[str] = None,
        issuer_peer_id: Optional[str] = None,
    ) -> bool:
        """Send bootstrap grant revocation marker to a peer."""
        if not self._running or not self._event_loop or not self.message_router:
            return False
        future = asyncio.run_coroutine_threadsafe(
            self.message_router.send_bootstrap_grant_revoke(
                to_peer=to_peer,
                grant_id=grant_id,
                revoked_at=revoked_at,
                reason=reason,
                issuer_peer_id=issuer_peer_id or self.get_peer_id(),
            ),
            self._event_loop,
        )

        def _on_done(f: Any) -> None:
            try:
                f.result()
            except Exception as exc:
                logger.error("Error sending bootstrap grant revoke: %s", exc)

        future.add_done_callback(_on_done)
        return True

    def send_member_sync_ack(self, to_peer: str, sync_id: str,
                              status: str = 'ok',
                              error: Optional[str] = None,
                              channel_id: Optional[str] = None,
                              target_user_id: Optional[str] = None,
                              action: Optional[str] = None) -> bool:
        """Acknowledge member-sync processing status (fire-and-forget)."""
        if not self._running or not self._event_loop:
            logger.warning("P2P network not running, cannot send member sync ack")
            return False
        if not self.message_router:
            return False

        future = asyncio.run_coroutine_threadsafe(
            self.message_router.send_member_sync_ack(
                to_peer=to_peer,
                sync_id=sync_id,
                status=status,
                error=error,
                channel_id=channel_id,
                target_user_id=target_user_id,
                action=action,
            ),
            self._event_loop,
        )

        def _on_done(f: Any) -> None:
            try:
                f.result()
            except Exception as exc:
                logger.error("Error sending member sync ack: %s", exc)

        future.add_done_callback(_on_done)
        return True

    def send_channel_membership_query(
        self,
        to_peer: str,
        local_user_ids: list[str],
        limit: int = 200,
        query_id: Optional[str] = None,
    ) -> bool:
        """Request private-channel membership recovery data from a peer."""
        if not self._running or not self._event_loop:
            return False
        if not self.message_router:
            return False
        future = asyncio.run_coroutine_threadsafe(
            self.message_router.send_channel_membership_query(
                to_peer=to_peer,
                local_user_ids=local_user_ids,
                limit=limit,
                query_id=query_id,
            ),
            self._event_loop,
        )

        def _on_done(f: Any) -> None:
            try:
                f.result()
            except Exception as exc:
                logger.error("Error sending channel membership query: %s", exc)

        future.add_done_callback(_on_done)
        return True

    def send_channel_membership_response(
        self,
        to_peer: str,
        query_id: Optional[str],
        channels: list[Dict[str, Any]],
        truncated: bool = False,
    ) -> bool:
        """Respond to private membership recovery query with channel metadata.

        Fire-and-forget: this may be called from the event loop thread (via
        routing callback), so we must NOT block with future.result() which
        would deadlock the loop.
        """
        if not self._running or not self._event_loop:
            return False
        if not self.message_router:
            return False
        future = asyncio.run_coroutine_threadsafe(
            self.message_router.send_channel_membership_response(
                to_peer=to_peer,
                query_id=query_id,
                channels=channels,
                truncated=truncated,
            ),
            self._event_loop,
        )

        def _on_done(f: Any) -> None:
            try:
                f.result()
            except Exception as exc:
                logger.error("Error sending channel membership response: %s", exc)

        future.add_done_callback(_on_done)
        return True

    async def _send_channel_sync_to_peer(self, peer_id: str) -> None:
        """
        Send all local public channels to a newly connected peer.
        
        Called automatically when a peer connects.
        """
        if not self.message_router:
            return

        try:
            channels = []
            if self.get_public_channels_for_sync:
                channels = self.get_public_channels_for_sync()

            if not channels:
                logger.debug(f"No public channels to sync with {peer_id}")
                return

            logger.debug(f"Sending channel sync ({len(channels)} channels) to {peer_id}")
            await self.message_router.send_channel_sync(peer_id, channels)
        except Exception as e:
            logger.error(f"Error sending channel sync to {peer_id}: {e}", exc_info=True)

    def _get_local_user_ids_for_membership_recovery(self, limit: int = 256) -> list[str]:
        """Return local user IDs hosted on this peer for membership recovery."""
        local_peer = str(self.get_peer_id() or '').strip()
        if not local_peer:
            return []
        try:
            with self.db.get_connection() as conn:
                rows = conn.execute(
                    """
                    SELECT id
                    FROM users
                    WHERE id != 'system'
                      AND (
                        origin_peer IS NULL
                        OR origin_peer = ''
                        OR origin_peer = ?
                      )
                    ORDER BY id ASC
                    LIMIT ?
                    """,
                    (local_peer, int(limit)),
                ).fetchall()
            return [
                str(row['id'] if hasattr(row, 'keys') and 'id' in row.keys() else row[0])
                for row in (rows or [])
            ]
        except Exception as e:
            logger.debug(f"Failed to load local users for membership recovery: {e}")
            return []

    async def _send_membership_recovery_query(self, peer_id: str) -> None:
        """Send targeted membership-recovery query to a connected peer."""
        if not self.message_router:
            return
        local_user_ids = self._get_local_user_ids_for_membership_recovery(limit=256)
        if not local_user_ids:
            return
        try:
            await self.message_router.send_channel_membership_query(
                to_peer=peer_id,
                local_user_ids=local_user_ids,
                limit=200,
            )
        except Exception as e:
            logger.debug(f"Membership recovery query to {peer_id} failed: {e}")

    async def _retry_missing_channel_key_requests_for_peer(self, peer_id: str) -> None:
        """Request missing E2E channel keys from a newly connected peer."""
        if not self.message_router or not peer_id:
            return
        local_peer = str(self.get_peer_id() or '').strip()
        if not local_peer or peer_id == local_peer:
            return
        try:
            with self.db.get_connection() as conn:
                rows = conn.execute(
                    """
                    SELECT DISTINCT c.id AS channel_id
                    FROM channels c
                    JOIN channel_members cm_local ON cm_local.channel_id = c.id
                    JOIN users u_local ON u_local.id = cm_local.user_id
                    LEFT JOIN channel_keys ck
                      ON ck.channel_id = c.id
                     AND ck.revoked_at IS NULL
                    WHERE COALESCE(c.privacy_mode, 'open') IN ('private', 'confidential')
                      AND COALESCE(c.crypto_mode, 'legacy_plaintext') IN ('e2e_optional', 'e2e_enforced')
                      AND (
                        u_local.origin_peer IS NULL
                        OR u_local.origin_peer = ''
                        OR u_local.origin_peer = ?
                      )
                      AND ck.key_id IS NULL
                      AND (
                        c.origin_peer = ?
                        OR EXISTS (
                            SELECT 1
                            FROM channel_members cm_remote
                            JOIN users u_remote ON u_remote.id = cm_remote.user_id
                            WHERE cm_remote.channel_id = c.id
                              AND u_remote.origin_peer = ?
                        )
                      )
                    ORDER BY c.created_at DESC
                    LIMIT 128
                    """,
                    (local_peer, peer_id, peer_id),
                ).fetchall()
            requested = 0
            for row in rows or []:
                channel_id = str(row['channel_id'] if hasattr(row, 'keys') and 'channel_id' in row.keys() else row[0])
                ok = await self.message_router.send_channel_key_request(
                    to_peer=peer_id,
                    channel_id=channel_id,
                    requesting_peer=local_peer,
                    reason='post_connect_missing_key',
                    key_id=None,
                )
                if ok:
                    requested += 1
            if requested:
                logger.info(
                    "Post-connect key retry requested %d missing key(s) from %s",
                    requested,
                    peer_id,
                )
        except Exception as e:
            logger.debug(f"Post-connect key retry for {peer_id} failed: {e}")

    PERIODIC_CATCHUP_INTERVAL = 180  # seconds (3 minutes)

    async def _periodic_catchup_loop(self) -> None:
        """Periodically send catch-up requests to all connected peers.

        This covers messages that were "sent OK" at the TCP level but
        never actually processed by the remote peer (e.g. because the
        connection dropped right after the send).  The on-reconnect
        catch-up only fires once; this loop provides ongoing repair.
        """
        # Initial delay — let startup settle before first periodic run
        await asyncio.sleep(60)

        while self._running:
            try:
                if not self.connection_manager:
                    await asyncio.sleep(self.PERIODIC_CATCHUP_INTERVAL)
                    continue

                peers = self.connection_manager.get_connected_peers()
                if peers:
                    logger.info(f"Periodic catch-up: syncing with "
                                f"{len(peers)} connected peer(s)")
                    for peer_id in peers:
                        try:
                            await self._send_catchup_request(peer_id)
                        except Exception as per_err:
                            logger.debug(f"Periodic catchup to {peer_id} "
                                         f"failed: {per_err}")
                        # Small stagger between peers
                        await asyncio.sleep(2)
            except Exception as e:
                logger.error(f"Error in periodic catchup loop: {e}",
                             exc_info=True)

            await asyncio.sleep(self.PERIODIC_CATCHUP_INTERVAL)

    async def _send_catchup_request(self, peer_id: str) -> None:
        """Send a catch-up request to a peer after connecting.

        Builds a map of {channel_id: latest_timestamp} from local data
        and sends it so the peer can reply with any newer messages.
        Also includes latest timestamps for feed posts, circle entries,
        circle votes, and tasks so the peer can send missed items.
        """
        if not self.message_router:
            return

        try:
            channel_timestamps = {}
            if self.get_channel_latest_timestamps:
                channel_timestamps = self.get_channel_latest_timestamps()

            # Gather extra timestamps for non-channel data
            extra_timestamps = {}
            for attr, key in [
                ('get_feed_latest_timestamp', 'feed_latest'),
                ('get_circle_entries_latest_timestamp', 'circle_entries_latest'),
                ('get_circle_votes_latest_timestamp', 'circle_votes_latest'),
                ('get_circles_latest_timestamp', 'circles_latest'),
                ('get_tasks_latest_timestamp', 'tasks_latest'),
            ]:
                cb = getattr(self, attr, None)
                if cb:
                    try:
                        ts = cb()
                        if ts:
                            extra_timestamps[key] = ts
                    except Exception:
                        pass

            digest_payload = None
            if self.sync_digest_enabled and self.get_channel_sync_digests:
                try:
                    can_use_digest = True
                    if self.sync_digest_require_capability:
                        can_use_digest = self.peer_supports_capability(peer_id, 'sync_digest_v1')
                    if can_use_digest:
                        digest_channels = self.get_channel_sync_digests(
                            channel_ids=list(channel_timestamps.keys()),
                            max_channels=self.sync_digest_max_channels_per_request,
                        ) or {}
                        if isinstance(digest_channels, dict) and digest_channels:
                            digest_payload = {
                                'version': 1,
                                'channels': digest_channels,
                            }
                            self._sync_digest_stats['requests_with_digest'] = int(
                                self._sync_digest_stats.get('requests_with_digest') or 0
                            ) + 1
                            self._sync_digest_stats['last_used_at'] = time.time()
                except Exception as digest_err:
                    self._sync_digest_stats['fallbacks'] = int(
                        self._sync_digest_stats.get('fallbacks') or 0
                    ) + 1
                    logger.debug(f"Catchup digest payload generation failed for {peer_id}: {digest_err}")

            # Even if we have no messages yet, send an empty map so the
            # peer can send us everything.
            logger.debug(f"Sending catchup request ({len(channel_timestamps)} "
                         f"channels, extras={list(extra_timestamps.keys())}) "
                         f"to {peer_id}")
            await self.message_router.send_catchup_request(
                peer_id, channel_timestamps,
                extra_timestamps=extra_timestamps if extra_timestamps else None,
                digest=digest_payload,
            )
        except Exception as e:
            logger.error(f"Error sending catchup request to {peer_id}: {e}",
                         exc_info=True)

    def record_sync_digest_stats(self, checked: int = 0, matched: int = 0,
                                 mismatched: int = 0, fallbacks: int = 0) -> None:
        """Record Merkle-assisted catch-up accounting for diagnostics."""
        try:
            self._sync_digest_stats['channels_checked'] = int(
                self._sync_digest_stats.get('channels_checked') or 0
            ) + max(0, int(checked or 0))
            self._sync_digest_stats['channels_matched'] = int(
                self._sync_digest_stats.get('channels_matched') or 0
            ) + max(0, int(matched or 0))
            self._sync_digest_stats['channels_mismatched'] = int(
                self._sync_digest_stats.get('channels_mismatched') or 0
            ) + max(0, int(mismatched or 0))
            self._sync_digest_stats['fallbacks'] = int(
                self._sync_digest_stats.get('fallbacks') or 0
            ) + max(0, int(fallbacks or 0))
            if (checked or matched or mismatched or fallbacks):
                self._sync_digest_stats['last_used_at'] = time.time()
        except Exception:
            pass

    def send_catchup_response(self, peer_id: str,
                               messages: list) -> bool:
        """Send a catch-up response with missed messages to a peer.

        Called from the application layer when a catchup request is
        received and messages have been gathered.  This SYNCHRONOUS
        version blocks until the send completes; prefer
        send_catchup_response_async when called from the event loop
        (the catchup handler in app.py uses the async path to avoid
        blocking and timeouts).
        """
        if not self._running or not self._event_loop:
            return False
        if not self.message_router:
            return False

        future = asyncio.run_coroutine_threadsafe(
            self.message_router.send_catchup_response(peer_id, messages),
            self._event_loop
        )
        # Generous timeout: payload can be large and peer may be slow.
        # Prefer send_catchup_response_async so the event loop is not blocked.
        try:
            return future.result(timeout=60.0)
        except Exception as e:
            logger.error(f"Error sending catchup response to {peer_id}: {e}",
                         exc_info=True)
            return False

    async def send_catchup_response_async(self, peer_id: str,
                                           messages: list,
                                           extra_data: Optional[dict[Any, Any]] = None) -> None:
        """Send catchup response messages asynchronously.

        Sends each channel message individually so a single failure
        doesn't block everything.  Then sends any extra data (feed
        posts, circle entries, circle votes, tasks) as a single batch.

        This is safe to call from the event loop (unlike the sync
        version which would deadlock).
        """
        if not self._running or not self.message_router:
            return

        sent = 0
        for msg in messages:
            try:
                # Per-message timeout so a slow/dead peer doesn't block forever
                ok = await asyncio.wait_for(
                    self.message_router.send_catchup_response(
                        peer_id, [msg]),
                    timeout=25.0)
                if ok:
                    sent += 1
                else:
                    logger.warning(
                        f"Catchup to {peer_id}: send failed at msg "
                        f"{sent + 1}/{len(messages)}, aborting")
                    break
            except asyncio.TimeoutError:
                logger.warning(
                    f"Catchup to {peer_id}: timeout at msg "
                    f"{sent + 1}/{len(messages)}, aborting")
                break
            except Exception as e:
                logger.warning(
                    f"Catchup to {peer_id}: error at msg "
                    f"{sent + 1}/{len(messages)}: {e}")
                break
            # Small stagger between messages
            await asyncio.sleep(0.1)

        if sent > 0:
            logger.info(f"Catchup to {peer_id}: sent {sent}/{len(messages)} "
                        f"messages successfully")

        # Send extra data (circle entries, tasks, feed posts, votes) as
        # a single batch response.  These are typically small so one
        # message is fine.
        if extra_data:
            total_extra = sum(len(v) for v in extra_data.values() if isinstance(v, list))
            has_non_list_payload = any(
                (not isinstance(v, list)) and v not in (None, '', {}, [])
                for v in extra_data.values()
            )
            if total_extra > 0 or has_non_list_payload:
                try:
                    ok = await asyncio.wait_for(
                        self.message_router.send_catchup_response(
                            peer_id, [], extra_data=extra_data),
                        timeout=30.0)
                    if ok:
                        parts = [
                            f"{k}={len(v)}"
                            for k, v in extra_data.items()
                            if isinstance(v, list) and v
                        ]
                        for k, v in extra_data.items():
                            if isinstance(v, dict) and v:
                                parts.append(f"{k}=1")
                        logger.info(f"Catchup to {peer_id}: sent extra data "
                                    f"({', '.join(parts)})")
                    else:
                        logger.warning(f"Catchup to {peer_id}: failed to send extra data")
                except asyncio.TimeoutError:
                    logger.warning(f"Catchup to {peer_id}: timeout sending extra data")
                except Exception as e:
                    logger.warning(f"Catchup to {peer_id}: error sending extra data: {e}")

    def get_connected_peers(self) -> list[str]:
        """Get list of currently connected peer IDs."""
        if not self.connection_manager:
            return []
        return self.connection_manager.get_connected_peers()

    def get_peer_versions(self) -> Dict[str, Dict[str, Any]]:
        """Return cached per-peer version/protocol metadata."""
        # Refresh cache opportunistically from current live connections.
        if self.connection_manager:
            for peer_id in self.connection_manager.get_connected_peers():
                self._refresh_peer_version_info(peer_id)
        return dict(self.peer_versions)
    
    def get_discovered_peers(self) -> list[Dict[str, Any]]:
        """Get list of discovered peers."""
        if not self.discovery:
            return []
        
        peers = self.discovery.get_discovered_peers()
        return [
            {
                'peer_id': p.peer_id,
                'address': p.address,
                'addresses': list(getattr(p, 'addresses', []) or ([p.address] if p.address else [])),
                'port': p.port,
                'discovered_at': p.discovered_at,
                'connected': self.connection_manager.is_connected(p.peer_id) if self.connection_manager else False
            }
            for p in peers
        ]
    
    @property
    def ws_scheme(self) -> str:
        """Return 'wss' if TLS is active, else 'ws'."""
        if self.connection_manager and getattr(self.connection_manager, 'enable_tls', False):
            return 'wss'
        return 'ws'

    def get_peer_id(self) -> Optional[str]:
        """Get local peer ID."""
        return self.local_identity.peer_id if self.local_identity else None

    def get_mesh_diagnostics(self) -> Dict[str, Any]:
        """Return runtime diagnostics for troubleshooting mesh stability."""
        pending_by_peer: Dict[str, int] = {}
        total_pending = 0
        if self.message_router:
            for peer_id, queue in (self.message_router.pending_messages or {}).items():
                count = len(queue or [])
                if count <= 0:
                    continue
                pending_by_peer[peer_id] = count
                total_pending += count

        reconnect_tasks: Dict[str, str] = {}
        for peer_id, task in list(self._reconnect_tasks.items()):
            state = 'scheduled'
            try:
                if getattr(task, 'cancelled', lambda: False)():
                    state = 'cancelled'
                    self._reconnect_tasks.pop(peer_id, None)
                elif getattr(task, 'done', lambda: False)():
                    state = 'done'
                    self._reconnect_tasks.pop(peer_id, None)
                else:
                    state = 'running'
            except Exception:
                state = 'unknown'
            reconnect_tasks[peer_id] = state

        recent_failures = []
        try:
            for event in self.get_activity_events(limit=200):
                kind = event.get('kind')
                status = (event.get('status') or '').lower()
                if kind == 'connection' and status in {'failed', 'disconnected'}:
                    recent_failures.append(event)
                elif kind == 'security' and status in {'blocked', 'warning'}:
                    recent_failures.append(event)
        except Exception:
            recent_failures = []

        sync_queue_depth = 0
        if self._sync_queue:
            try:
                sync_queue_depth = int(self._sync_queue.qsize())
            except Exception:
                sync_queue_depth = 0

        return {
            'timestamp': time.time(),
            'connected_peers': self.get_connected_peers(),
            'known_peers_count': len(getattr(self.identity_manager, 'known_peers', {}) or {}),
            'pending_messages': {
                'total': total_pending,
                'by_peer': pending_by_peer,
            },
            'reconnect_tasks': {
                'count': len(reconnect_tasks),
                'by_peer': reconnect_tasks,
            },
            'sync': {
                'queue_depth': sync_queue_depth,
                'active_catchups': list(self._active_catchups),
                'startup_grace': self._in_startup_grace_period(),
                'digest': {
                    'enabled': bool(self.sync_digest_enabled),
                    'require_capability': bool(self.sync_digest_require_capability),
                    'max_channels_per_request': int(self.sync_digest_max_channels_per_request),
                    'stats': dict(self._sync_digest_stats),
                },
            },
            'security': {
                'allow_unverified_relay_messages': bool(
                    getattr(self, 'allow_unverified_relay_messages', False)
                ),
            },
            'recent_failures': recent_failures[-20:],
        }

    def resync_mesh(self, include_reconnect: bool = True) -> Dict[str, Any]:
        """
        Schedule post-connect sync for connected peers and optionally reconnect known peers.
        """
        if not self._event_loop or self._event_loop.is_closed():
            return {
                'scheduled_syncs': 0,
                'connected_targets': [],
                'reconnect_scheduled': False,
                'error': 'event-loop-unavailable',
            }

        connected = self.get_connected_peers()
        scheduled = 0
        for peer_id in connected:
            try:
                asyncio.run_coroutine_threadsafe(
                    self._run_post_connect_sync(peer_id),
                    self._event_loop
                )
                scheduled += 1
            except Exception:
                logger.warning(f"Failed to schedule manual sync for {peer_id}", exc_info=True)

        reconnect_ok = False
        if include_reconnect:
            reconnect_ok = self.reconnect_known_peers()

        return {
            'scheduled_syncs': scheduled,
            'connected_targets': connected,
            'reconnect_scheduled': reconnect_ok,
        }
    
    def get_network_status(self) -> Dict[str, Any]:
        """
        Get current network status.
        
        Returns:
            Dictionary with network status information
        """
        return {
            'running': self._running,
            'peer_id': self.get_peer_id(),
            'canopy_version': self.local_canopy_version,
            'protocol_version': self.local_protocol_version,
            'connected_peers': len(self.get_connected_peers()),
            'connected_peers_list': self.get_connected_peers(),
            'discovered_peers': len(self.get_discovered_peers()),
            'peers': self.get_discovered_peers(),
            'peer_versions': self.get_peer_versions(),
            'relay_policy': getattr(self, 'relay_policy', 'broker_only'),
            'sync_digest': {
                'enabled': bool(self.sync_digest_enabled),
                'require_capability': bool(self.sync_digest_require_capability),
                'max_channels_per_request': int(self.sync_digest_max_channels_per_request),
                'stats': dict(self._sync_digest_stats),
            },
            'security': {
                'allow_unverified_relay_messages': bool(
                    getattr(self, 'allow_unverified_relay_messages', False)
                ),
            },
        }
    
    def is_running(self) -> bool:
        """Check if P2P network is running."""
        return self._running
