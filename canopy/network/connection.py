"""
Connection management for Canopy P2P network.

Handles establishing, maintaining, and closing connections to peers.

Author: Konrad Walus (architecture, design, and direction)
Project: Canopy - Local Mesh Communication
License: Apache 2.0
Development: AI-assisted implementation (Claude, Codex, GitHub Copilot, Cursor IDE, Ollama)
"""

import asyncio
import base58
import json
import logging
import ssl
import time
import websockets
from pathlib import Path
from typing import Dict, Optional, Callable, Any, Awaitable, List
from enum import Enum
from dataclasses import dataclass, field

logger = logging.getLogger('canopy.network.connection')


def _generate_self_signed_cert(cert_path: Path, key_path: Path) -> bool:
    """Generate a self-signed TLS certificate for P2P WebSocket connections.

    Uses the `cryptography` library which is already a Canopy dependency
    (used for ChaCha20-Poly1305 and Ed25519).

    Returns True if cert was generated successfully.
    """
    try:
        from cryptography import x509
        from cryptography.x509.oid import NameOID
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        import datetime

        key = ec.generate_private_key(ec.SECP256R1())

        subject = issuer = x509.Name([
            x509.NameAttribute(NameOID.COMMON_NAME, 'Canopy P2P Node'),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, 'Canopy'),
        ])

        cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(issuer)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(datetime.datetime.now(datetime.timezone.utc))
            .not_valid_after(
                datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=3650)
            )
            .add_extension(
                x509.SubjectAlternativeName([
                    x509.DNSName('localhost'),
                    x509.IPAddress(
                        __import__('ipaddress').IPv4Address('127.0.0.1')),
                ]),
                critical=False,
            )
            .sign(key, hashes.SHA256())
        )

        cert_path.parent.mkdir(parents=True, exist_ok=True)
        cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        key_path.write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.TraditionalOpenSSL,
                serialization.NoEncryption(),
            )
        )
        logger.info(f"Generated self-signed TLS cert: {cert_path}")
        return True
    except Exception as e:
        logger.error(f"Failed to generate TLS cert: {e}")
        return False


def create_server_ssl_context(cert_path: Path, key_path: Path) -> Optional[ssl.SSLContext]:
    """Create an SSL context for the WebSocket server."""
    if not cert_path.exists() or not key_path.exists():
        if not _generate_self_signed_cert(cert_path, key_path):
            return None
    try:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(str(cert_path), str(key_path))
        logger.info("TLS server context created")
        return ctx
    except Exception as e:
        logger.error(f"Failed to create TLS server context: {e}")
        return None


def create_client_ssl_context() -> ssl.SSLContext:
    """Create a permissive SSL context for connecting to peers.

    Self-signed certs are expected in a mesh network, so we disable
    hostname and certificate verification.  E2E encryption
    (ChaCha20-Poly1305) protects the content independently.
    """
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


class ConnectionState(Enum):
    """States of a peer connection."""
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    HANDSHAKING = "handshaking"
    CONNECTED = "connected"
    AUTHENTICATED = "authenticated"
    FAILED = "failed"


@dataclass
class PeerConnection:
    """Represents a connection to a peer."""
    peer_id: str
    address: str
    port: int
    state: ConnectionState
    websocket: Optional[Any] = None
    connected_at: Optional[float] = None
    last_activity: Optional[float] = None
    last_inbound_activity: Optional[float] = None
    last_outbound_activity: Optional[float] = None
    is_outbound: bool = True  # True if we initiated connection
    capabilities: Optional[Dict[str, bool]] = None
    last_ping_latency_ms: Optional[float] = None  # RTT from last keepalive ping
    handshake_version: Optional[str] = None
    canopy_version: Optional[str] = None
    protocol_version: Optional[int] = None
    failure_reason: Optional[str] = None
    failure_detail: Optional[str] = None
    _send_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    
    def is_connected(self) -> bool:
        """Check if connection is active."""
        return self.state in [ConnectionState.CONNECTED, ConnectionState.AUTHENTICATED]
    
    def update_activity(self) -> None:
        """Update last activity timestamp."""
        self.last_activity = time.time()

    def update_inbound_activity(self) -> None:
        """Update last inbound activity timestamp."""
        now = time.time()
        self.last_inbound_activity = now
        self.last_activity = now

    def update_outbound_activity(self) -> None:
        """Update last outbound activity timestamp."""
        now = time.time()
        self.last_outbound_activity = now
        self.last_activity = now


class ConnectionManager:
    """Manages connections to multiple peers."""
    
    def __init__(self, local_peer_id: str, identity_manager: Any,
                 host: str = "0.0.0.0", port: int = 7771,
                 tls_cert_path: Optional[str] = None,
                 tls_key_path: Optional[str] = None,
                 enable_tls: bool = False,
                 handshake_capabilities: Optional[List[str]] = None,
                 canopy_version: str = "0.1.0",
                 protocol_version: int = 1,
                 reject_protocol_mismatch: bool = False):
        """
        Initialize connection manager.
        
        Args:
            local_peer_id: This peer's ID
            identity_manager: IdentityManager instance for crypto operations
            host: Host address to bind server
            port: Port for incoming connections
            tls_cert_path: Path to TLS certificate (PEM)
            tls_key_path: Path to TLS private key (PEM)
            enable_tls: If True, use wss:// instead of ws://
        """
        self.local_peer_id = local_peer_id
        self.identity_manager = identity_manager
        self.host = host
        self.port = port
        base_capabilities = handshake_capabilities or ['chat', 'files', 'voice']
        self.handshake_capabilities = [
            cap for cap in (str(item).strip() for item in base_capabilities)
            if cap
        ] or ['chat', 'files', 'voice']
        self.local_canopy_version = str(canopy_version or '0.1.0').strip() or '0.1.0'
        self.local_protocol_version = self._coerce_protocol_version(protocol_version, default=1)
        self.reject_protocol_mismatch = bool(reject_protocol_mismatch)
        
        # TLS configuration
        self.enable_tls = enable_tls
        self._server_ssl: Optional[ssl.SSLContext] = None
        self._client_ssl: Optional[ssl.SSLContext] = None
        if enable_tls:
            cert_p = Path(tls_cert_path) if tls_cert_path else Path('data/tls/canopy.crt')
            key_p = Path(tls_key_path) if tls_key_path else Path('data/tls/canopy.key')
            self._server_ssl = create_server_ssl_context(cert_p, key_p)
            self._client_ssl = create_client_ssl_context()
            if self._server_ssl:
                logger.info("TLS enabled for P2P connections (wss://)")
            else:
                logger.warning("TLS requested but cert setup failed — falling back to ws://")
                self.enable_tls = False
        
        # Connection tracking
        self.connections: Dict[str, PeerConnection] = {}
        self._last_connect_failures: Dict[str, Dict[str, Any]] = {}
        self.max_connections = 50
        
        # Server
        self.server: Optional[Any] = None
        self._server_task: Optional[Any] = None
        
        # Message handlers
        self.message_handlers: Dict[str, Callable[[PeerConnection, Dict[str, Any]], Awaitable[None]]] = {}
        
        # Callback fired when an incoming peer completes authentication
        self.on_peer_authenticated: Optional[Callable[..., None]] = None
        
        # Callback fired when a peer disconnects
        self.on_peer_disconnected: Optional[Callable[[str], None]] = None
        
        # Event loop
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self._running = False
        
        tls_tag = ' (TLS)' if self.enable_tls else ''
        logger.info(f"Initialized ConnectionManager for {local_peer_id} "
                     f"on {host}:{port}{tls_tag}")

    @staticmethod
    def _format_endpoint_host(host: str) -> str:
        """Format a host for endpoint rendering, preserving IPv6 brackets."""
        text = str(host or '').strip()
        if ':' in text and not text.startswith('['):
            return f"[{text}]"
        return text

    def _failure_key(self, peer_id: str, address: str, port: int) -> str:
        return f"{peer_id}|{self._format_endpoint_host(address)}|{int(port)}"

    def _record_connect_failure(self, peer_id: str, address: str, port: int,
                                reason: str, detail: str) -> None:
        self._last_connect_failures[self._failure_key(peer_id, address, port)] = {
            'reason': str(reason or 'connection_failed'),
            'detail': str(detail or reason or 'Connection failed'),
            'timestamp': time.time(),
        }

    def _clear_connect_failure(self, peer_id: str, address: str, port: int) -> None:
        self._last_connect_failures.pop(self._failure_key(peer_id, address, port), None)

    def get_last_connect_failure(self, peer_id: str, address: str, port: int) -> Optional[Dict[str, Any]]:
        return dict(self._last_connect_failures.get(self._failure_key(peer_id, address, port), {}) or {}) or None

    def _get_handshake_capabilities(self) -> List[str]:
        """Return deduplicated capability list advertised in handshakes."""
        return self._normalize_capabilities(self.handshake_capabilities)

    @staticmethod
    def _normalize_capabilities(raw: Any) -> List[str]:
        """Normalize capability payloads to a list of non-empty strings."""
        if raw is None:
            return []
        values: List[Any]
        if isinstance(raw, str):
            values = [part.strip() for part in raw.split(',')]
        elif isinstance(raw, (list, tuple, set)):
            values = list(raw)
        else:
            values = []
        out: List[str] = []
        seen = set()
        for cap in values:
            cap = str(cap).strip()
            if cap in seen:
                continue
            seen.add(cap)
            out.append(cap)
        return out

    @staticmethod
    def _coerce_protocol_version(raw: Any, default: int = 1) -> int:
        """Parse protocol version from mixed payloads safely."""
        try:
            value = int(raw)
            return value if value > 0 else int(default)
        except Exception:
            return int(default)

    @staticmethod
    def _signed_optional_handshake_fields(payload: Dict[str, Any]) -> Dict[str, Any]:
        """Return optional handshake fields exactly as signed on wire."""
        extra: Dict[str, Any] = {}
        if 'canopy_version' in payload:
            extra['canopy_version'] = payload.get('canopy_version')
        if 'protocol_version' in payload:
            extra['protocol_version'] = payload.get('protocol_version')
        return extra
    
    async def start(self) -> None:
        """Start connection manager and WebSocket server."""
        if self._running:
            logger.warning("ConnectionManager already running")
            return

        try:
            logger.info(f"Starting WebSocket server on {self.host}:{self.port}...")

            # Start WebSocket server (with optional TLS)
            # Disable library auto-pings to avoid concurrent write assertions
            # in the legacy websockets protocol. We run our own keepalive pings
            # in _monitor_connections() using the per-connection send lock.
            serve_kwargs: Dict[str, Any] = dict(
                host=self.host,
                port=self.port,
                ping_interval=None,
                ping_timeout=None,
                max_size=20 * 1024 * 1024,  # 20MB — allow P2P image transfer
                compression="deflate",  # permessage-deflate — ~40-70% savings on JSON frames
            )
            if self.enable_tls and self._server_ssl:
                serve_kwargs['ssl'] = self._server_ssl

            # Pass handler positionally for compatibility with newer websockets
            # versions where the handler argument is required positional.
            self.server = await websockets.serve(
                self._handle_incoming_connection,
                **serve_kwargs,
            )

            self._running = True
            scheme = 'wss' if (self.enable_tls and self._server_ssl) else 'ws'
            logger.info(f"WebSocket server started on {scheme}://{self.host}:{self.port}")

            # Start connection monitor
            asyncio.create_task(self._monitor_connections())

        except OSError as e:
            if e.errno == 48:  # Address already in use (macOS/Linux)
                logger.error(
                    "Port %s is already in use. Stop the other process using it "
                    "(e.g. another Canopy instance) or set CANOPY_MESH_PORT to a different port (e.g. 7775).",
                    self.port,
                )
            logger.error(f"Failed to start ConnectionManager: {e}", exc_info=True)
            raise
        except Exception as e:
            logger.error(f"Failed to start ConnectionManager: {e}", exc_info=True)
            raise
    
    async def stop(self) -> None:
        """Stop connection manager and close all connections."""
        if not self._running:
            return
        
        logger.info("Stopping ConnectionManager...")
        self._running = False
        
        # Close all connections
        for peer_id in list(self.connections.keys()):
            await self.disconnect_peer(peer_id)
        
        # Close server
        if self.server:
            self.server.close()
            await self.server.wait_closed()
        
        logger.info("ConnectionManager stopped")
    
    async def connect_to_peer(self, peer_id: str, address: str, port: int) -> bool:
        """
        Establish outbound connection to a peer.
        
        Args:
            peer_id: Target peer ID
            address: Peer's IP address
            port: Peer's port
            
        Returns:
            True if connection successful
        """
        # Never connect to ourselves. This can happen if stale endpoint data
        # (e.g., ws://127.0.0.1:7771) gets associated with our own peer_id.
        if peer_id == self.local_peer_id:
            logger.warning("Refusing to connect to self (peer_id=%s) at %s:%s", peer_id, address, port)
            self._record_connect_failure(peer_id, address, port, 'self_connect_blocked', 'Refusing to connect to self')
            return False

        # Check if already connected
        if peer_id in self.connections:
            conn = self.connections[peer_id]
            if conn.is_connected():
                logger.debug(f"Already connected to {peer_id}")
                return True
        
        # Check connection limit
        if len(self.connections) >= self.max_connections:
            logger.warning(f"Connection limit reached ({self.max_connections})")
            self._record_connect_failure(
                peer_id,
                address,
                port,
                'connection_limit_reached',
                f'Connection limit reached ({self.max_connections})',
            )
            return False
        
        logger.info(f"Connecting to peer {peer_id} at {address}:{port}...")
        
        # Create connection object
        connection = PeerConnection(
            peer_id=peer_id,
            address=address,
            port=port,
            state=ConnectionState.CONNECTING,
            is_outbound=True
        )
        
        self.connections[peer_id] = connection

        try:
            # Connect via WebSocket — try wss:// first if TLS is enabled,
            # then fall back to ws:// for backward compatibility.
            scheme = 'wss' if self.enable_tls else 'ws'
            uri = f"{scheme}://{address}:{port}/p2p"
            connect_kwargs: Dict[str, Any] = dict(
                ping_interval=None,
                ping_timeout=None,
                open_timeout=5,
                max_size=20 * 1024 * 1024,  # 20MB — allow P2P image transfer
                compression="deflate",  # permessage-deflate — matches server setting
            )
            if self.enable_tls and self._client_ssl:
                connect_kwargs['ssl'] = self._client_ssl

            try:
                websocket = await websockets.connect(uri, **connect_kwargs)
            except Exception as tls_err:
                if self.enable_tls:
                    # Fall back to plain ws:// if wss failed
                    logger.debug(
                        f"wss:// failed to {address}:{port}, falling back to ws:// ({tls_err})"
                    )
                    websocket = await websockets.connect(
                        f"ws://{address}:{port}/p2p",
                        ping_interval=None,
                        ping_timeout=None,
                        open_timeout=5,
                        max_size=20 * 1024 * 1024,
                        compression="deflate",
                    )
                else:
                    raise

            connection.websocket = websocket
            connection.state = ConnectionState.HANDSHAKING

            # Perform handshake
            success = await self._perform_handshake(connection)

            if success:
                connection.state = ConnectionState.AUTHENTICATED
                connection.connected_at = time.time()
                connection.update_activity()
                self._clear_connect_failure(peer_id, address, port)

                logger.info(f"Successfully connected to {peer_id}")

                # Start message handler
                asyncio.create_task(self._handle_peer_messages(connection))

                return True

            reason = str(getattr(connection, 'failure_reason', None) or 'handshake_failed')
            detail = str(getattr(connection, 'failure_detail', None) or f'Handshake failed with {peer_id}')
            logger.warning(f"Handshake failed with {peer_id}: {detail}")
            self._record_connect_failure(peer_id, address, port, reason, detail)
            await self._disconnect_connection(connection, notify=False)
            return False

        except (TimeoutError, asyncio.TimeoutError):
            # Expected when trying stale/unreachable addresses; we try other addresses or retry
            logger.info(
                f"Connection to {peer_id} at {address}:{port} timed out "
                "(will try other addresses or retry)"
            )
            connection.state = ConnectionState.FAILED
            connection.failure_reason = 'timeout'
            connection.failure_detail = 'Connection timed out'
            self._record_connect_failure(peer_id, address, port, 'timeout', 'Connection timed out')
            await self._disconnect_connection(connection, notify=False)
            return False
        except Exception as e:
            logger.error(
                f"Failed to connect to {peer_id} at {address}:{port}: "
                f"{type(e).__name__}: {e}",
                exc_info=True,
            )
            connection.state = ConnectionState.FAILED
            connection.failure_reason = type(e).__name__
            connection.failure_detail = str(e)
            self._record_connect_failure(peer_id, address, port, type(e).__name__, str(e))
            await self._disconnect_connection(connection, notify=False)
            return False
    
    async def _handle_incoming_connection(self, websocket: Any, path: Optional[str] = None) -> None:
        """
        Handle incoming WebSocket connection.
        
        Validates the peer's cryptographic identity before accepting.
        
        Args:
            websocket: WebSocket connection
            path: Request path (provided by some websockets versions)
        """
        logger.info(f"Incoming connection from {websocket.remote_address}")
        
        try:
            # First message should be handshake with peer ID and signature
            handshake_msg = await asyncio.wait_for(websocket.recv(), timeout=10.0)
            handshake_data = json.loads(handshake_msg)
            
            peer_id = handshake_data.get('peer_id')
            if not peer_id:
                logger.warning("Handshake missing peer_id")
                await websocket.close()
                return

            # Reject self-connect (e.g., connecting to ws://127.0.0.1:7771).
            if peer_id == self.local_peer_id:
                logger.warning("Rejecting incoming self-connection for peer_id=%s", peer_id)
                await websocket.close()
                return
            
            # Verify the incoming handshake signature
            signature_hex = handshake_data.get('signature')
            ed25519_pub_b58 = handshake_data.get('ed25519_public_key')
            x25519_pub_b58 = handshake_data.get('x25519_public_key')
            
            if not all([signature_hex, ed25519_pub_b58, x25519_pub_b58]):
                logger.warning(f"Handshake from {peer_id} missing signature or keys")
                await websocket.close()
                return
            
            # Reconstruct the signed payload using base fields only.
            # canopy_version/protocol_version are unsigned metadata, NOT part of the signature.
            payload = {
                'peer_id': peer_id,
                'ed25519_public_key': ed25519_pub_b58,
                'x25519_public_key': x25519_pub_b58,
                'version': handshake_data.get('version', '0.1.0'),
                'capabilities': self._normalize_capabilities(
                    handshake_data.get('capabilities', [])
                ),
                'timestamp': handshake_data.get('timestamp', 0)
            }
            payload_bytes = json.dumps(payload, sort_keys=True).encode('utf-8')
            
            # Decode public key and verify peer_id matches
            ed25519_pub_bytes = base58.b58decode(ed25519_pub_b58)
            if not self.identity_manager.verify_peer_id(peer_id, ed25519_pub_bytes):
                logger.warning(f"Peer ID {peer_id} does not match public key - rejecting")
                await websocket.close()
                return
            
            # Verify signature
            from .identity import PeerIdentity
            remote_identity = PeerIdentity(
                peer_id=peer_id,
                ed25519_public_key=ed25519_pub_bytes,
                x25519_public_key=base58.b58decode(x25519_pub_b58)
            )
            
            signature = bytes.fromhex(signature_hex)
            verified = remote_identity.verify(payload_bytes, signature)
            if not verified:
                # Fallback: peer may be running unpatched 0.4.30 that signed optional fields
                fallback_payload = dict(payload)
                fallback_payload.update(self._signed_optional_handshake_fields(handshake_data))
                fallback_bytes = json.dumps(fallback_payload, sort_keys=True).encode('utf-8')
                verified = remote_identity.verify(fallback_bytes, signature)
            if not verified:
                logger.warning(f"Handshake signature verification FAILED for {peer_id} - rejecting")
                await websocket.close()
                return
            
            logger.debug(f"Verified identity of incoming peer: {peer_id}")
            
            # Store verified peer identity
            self.identity_manager.add_known_peer(remote_identity)
            
            # Create connection
            connection = PeerConnection(
                peer_id=peer_id,
                address=websocket.remote_address[0],
                port=websocket.remote_address[1],
                state=ConnectionState.HANDSHAKING,
                websocket=websocket,
                is_outbound=False,
                handshake_version=str(handshake_data.get('version', '0.1.0') or '0.1.0'),
                canopy_version=str(handshake_data.get('canopy_version') or handshake_data.get('version', '0.1.0') or '0.1.0'),
                protocol_version=self._coerce_protocol_version(
                    handshake_data.get('protocol_version', 1),
                    default=1,
                ),
            )
            connection.capabilities = {
                cap: True for cap in self._normalize_capabilities(
                    handshake_data.get('capabilities', [])
                )
            }

            remote_protocol = connection.protocol_version or 1
            remote_canopy = str(connection.canopy_version or '0.1.0')
            if remote_protocol != self.local_protocol_version:
                logger.warning(
                    "Protocol version mismatch with %s: local=%s remote=%s (canopy=%s)",
                    peer_id,
                    self.local_protocol_version,
                    remote_protocol,
                    remote_canopy,
                )
                if self.reject_protocol_mismatch:
                    logger.warning(
                        "Rejecting %s due to protocol mismatch (reject_protocol_mismatch enabled)",
                        peer_id,
                    )
                    await websocket.close()
                    return
            elif remote_canopy != self.local_canopy_version:
                logger.info(
                    "Canopy version difference with %s: local=%s remote=%s",
                    peer_id,
                    self.local_canopy_version,
                    remote_canopy,
                )
            
            # Complete handshake (send our signed ack)
            success = await self._complete_handshake(connection, handshake_data)
            
            if success:
                # If we already had a connection object for this peer_id, close it.
                # (Otherwise its message-handler task may keep running and can cause churn.)
                existing = self.connections.get(peer_id)
                if existing and existing.websocket is not websocket:
                    await self._disconnect_connection(existing, notify=False)

                self.connections[peer_id] = connection
                connection.state = ConnectionState.AUTHENTICATED
                connection.connected_at = time.time()
                connection.update_activity()
                
                logger.debug(f"Accepted authenticated connection from {peer_id}")
                
                # Notify manager so it can run channel-sync + catch-up
                if self.on_peer_authenticated:
                    try:
                        peer_meta = {
                            'version': connection.handshake_version,
                            'canopy_version': connection.canopy_version,
                            'protocol_version': connection.protocol_version,
                            'capabilities': list(connection.capabilities or {}),
                        }
                        self.on_peer_authenticated(peer_id, peer_meta)
                    except TypeError:
                        self.on_peer_authenticated(peer_id)
                    except Exception as cb_err:
                        logger.error(f"on_peer_authenticated callback error for {peer_id}: {cb_err}", exc_info=True)
                
                # Handle messages
                await self._handle_peer_messages(connection)
            else:
                logger.warning(f"Failed to complete handshake with {peer_id}")
                await websocket.close()
                
        except asyncio.TimeoutError:
            logger.warning("Handshake timeout")
            try:
                await websocket.close()
            except Exception:
                pass
        except (websockets.exceptions.ConnectionClosed, asyncio.IncompleteReadError):
            # Client disconnected before sending handshake (e.g. scanner, browser, or dropped link)
            logger.debug(
                "Incoming connection closed before handshake (client disconnected or non-Canopy client)"
            )
            try:
                await websocket.close()
            except Exception:
                pass
        except Exception as e:
            logger.error(f"Error handling incoming connection: {e}", exc_info=True)
            try:
                await websocket.close()
            except Exception:
                pass
    
    async def _perform_handshake(self, connection: PeerConnection) -> bool:
        """
        Perform handshake with peer (outbound connection).
        
        Signs the handshake with our Ed25519 key and verifies the peer's response.
        
        Args:
            connection: PeerConnection instance
            
        Returns:
            True if handshake successful
        """
        try:
            # Prepare handshake payload (the data to be signed)
            identity = self.identity_manager.export_public_identity()
            timestamp = time.time()
            
            # Base fields are signed (backward compatible with all versions).
            # canopy_version and protocol_version are sent as unsigned metadata.
            signed_payload = {
                'peer_id': self.local_peer_id,
                'ed25519_public_key': identity['ed25519_public_key'],
                'x25519_public_key': identity['x25519_public_key'],
                'version': '0.1.0',
                'capabilities': self._get_handshake_capabilities(),
                'timestamp': timestamp
            }
            
            payload_bytes = json.dumps(signed_payload, sort_keys=True).encode('utf-8')
            signature = self.identity_manager.local_identity.sign(payload_bytes)
            
            handshake = {
                'type': 'handshake',
                **signed_payload,
                'canopy_version': self.local_canopy_version,
                'protocol_version': self.local_protocol_version,
                'signature': signature.hex()
            }
            
            # Send handshake
            websocket = connection.websocket
            if websocket is None:
                return False
            await websocket.send(json.dumps(handshake))
            
            # Wait for response
            response_msg = await asyncio.wait_for(websocket.recv(), timeout=10.0)
            response = json.loads(response_msg)
            
            if response.get('type') != 'handshake_ack':
                logger.warning(f"Invalid handshake response: {response.get('type')}")
                connection.failure_reason = 'invalid_handshake_response'
                connection.failure_detail = f"Invalid handshake response: {response.get('type')}"
                return False
            
            # Verify the peer's response signature
            resp_signature_hex = response.get('signature')
            if not resp_signature_hex:
                logger.warning(f"Handshake response from {connection.peer_id} has no signature")
                connection.failure_reason = 'missing_handshake_signature'
                connection.failure_detail = 'Handshake response has no signature'
                return False
            
            # Reconstruct the signed payload from the response
            resp_peer_id = response.get('peer_id')
            resp_ed25519_pub = response.get('ed25519_public_key')
            resp_x25519_pub = response.get('x25519_public_key')
            
            if not all([resp_peer_id, resp_ed25519_pub, resp_x25519_pub]):
                logger.warning("Handshake response missing required identity fields")
                connection.failure_reason = 'missing_identity_fields'
                connection.failure_detail = 'Handshake response missing required identity fields'
                return False
            
            # Base fields only for signature verification (backward compatible).
            resp_payload = {
                'peer_id': resp_peer_id,
                'ed25519_public_key': resp_ed25519_pub,
                'x25519_public_key': resp_x25519_pub,
                'version': response.get('version', '0.1.0'),
                'capabilities': self._normalize_capabilities(
                    response.get('capabilities', [])
                ),
                'timestamp': response.get('timestamp', 0)
            }
            resp_payload_bytes = json.dumps(resp_payload, sort_keys=True).encode('utf-8')
            
            # Decode the peer's public key and verify their signature
            resp_ed25519_pub_bytes = base58.b58decode(resp_ed25519_pub)
            
            # Verify peer_id matches the public key
            if not self.identity_manager.verify_peer_id(resp_peer_id, resp_ed25519_pub_bytes):
                logger.warning(f"Peer ID {resp_peer_id} does not match public key!")
                connection.failure_reason = 'peer_id_public_key_mismatch'
                connection.failure_detail = f"Peer ID {resp_peer_id} does not match public key"
                return False

            # Verify the responding peer is who we expected to connect to.
            if resp_peer_id != connection.peer_id:
                logger.warning(
                    f"Handshake peer-id mismatch! Expected {connection.peer_id}, "
                    f"got {resp_peer_id}. Rejecting connection.")
                connection.failure_reason = 'handshake_peer_id_mismatch'
                connection.failure_detail = (
                    f"Handshake peer-id mismatch: expected {connection.peer_id}, got {resp_peer_id}"
                )
                try:
                    host = connection.address
                    port = connection.port
                    self.identity_manager.remove_endpoint(connection.peer_id, f"ws://{host}:{port}")
                    self.identity_manager.remove_endpoint(connection.peer_id, f"wss://{host}:{port}")
                except Exception:
                    pass
                return False

            # Create a remote peer identity and verify signature
            from .identity import PeerIdentity
            remote_identity = PeerIdentity(
                peer_id=resp_peer_id,
                ed25519_public_key=resp_ed25519_pub_bytes,
                x25519_public_key=base58.b58decode(resp_x25519_pub)
            )
            
            resp_signature = bytes.fromhex(resp_signature_hex)
            verified = remote_identity.verify(resp_payload_bytes, resp_signature)
            if not verified:
                # Fallback: peer may be running unpatched 0.4.30 that signed optional fields
                fallback_payload = dict(resp_payload)
                fallback_payload.update(self._signed_optional_handshake_fields(response))
                fallback_bytes = json.dumps(fallback_payload, sort_keys=True).encode('utf-8')
                verified = remote_identity.verify(fallback_bytes, resp_signature)
            if not verified:
                logger.warning(f"Handshake signature verification FAILED for {resp_peer_id}")
                connection.failure_reason = 'handshake_signature_verification_failed'
                connection.failure_detail = f'Handshake signature verification failed for {resp_peer_id}'
                return False
            
            # Store the verified peer identity
            self.identity_manager.add_known_peer(remote_identity)

            connection.handshake_version = str(response.get('version', '0.1.0') or '0.1.0')
            connection.canopy_version = str(response.get('canopy_version') or response.get('version', '0.1.0') or '0.1.0')
            connection.protocol_version = self._coerce_protocol_version(
                response.get('protocol_version', 1),
                default=1,
            )

            connection.capabilities = {
                cap: True for cap in self._normalize_capabilities(
                    response.get('capabilities', [])
                )
            }

            remote_protocol = connection.protocol_version or 1
            remote_canopy = str(connection.canopy_version or '0.1.0')
            if remote_protocol != self.local_protocol_version:
                logger.warning(
                    "Protocol version mismatch with %s: local=%s remote=%s (canopy=%s)",
                    connection.peer_id,
                    self.local_protocol_version,
                    remote_protocol,
                    remote_canopy,
                )
                if self.reject_protocol_mismatch:
                    logger.warning(
                        "Rejecting %s due to protocol mismatch (reject_protocol_mismatch enabled)",
                        connection.peer_id,
                    )
                    connection.failure_reason = 'protocol_mismatch'
                    connection.failure_detail = (
                        f"Protocol mismatch: local={self.local_protocol_version} remote={remote_protocol}"
                    )
                    return False
            elif remote_canopy != self.local_canopy_version:
                logger.info(
                    "Canopy version difference with %s: local=%s remote=%s",
                    connection.peer_id,
                    self.local_canopy_version,
                    remote_canopy,
                )
            
            logger.info(f"Handshake completed and verified with {connection.peer_id}")
            return True
            
        except Exception as e:
            logger.error(f"Handshake failed: {e}", exc_info=True)
            connection.failure_reason = type(e).__name__
            connection.failure_detail = str(e)
            return False
    
    async def _complete_handshake(self, connection: PeerConnection, 
                                  handshake_data: Dict[str, Any]) -> bool:
        """
        Complete handshake for incoming connection by sending a signed ack.
        
        The incoming peer's identity has already been verified in 
        _handle_incoming_connection before this method is called.
        
        Args:
            connection: PeerConnection instance
            handshake_data: Received handshake data
            
        Returns:
            True if handshake successful
        """
        try:
            # Prepare our signed acknowledgment
            identity = self.identity_manager.export_public_identity()
            timestamp = time.time()
            
            # Sign base fields only (backward compatible with all peer versions).
            signed_payload = {
                'peer_id': self.local_peer_id,
                'ed25519_public_key': identity['ed25519_public_key'],
                'x25519_public_key': identity['x25519_public_key'],
                'version': '0.1.0',
                'capabilities': self._get_handshake_capabilities(),
                'timestamp': timestamp
            }
            
            payload_bytes = json.dumps(signed_payload, sort_keys=True).encode('utf-8')
            signature = self.identity_manager.local_identity.sign(payload_bytes)
            
            ack = {
                'type': 'handshake_ack',
                **signed_payload,
                'canopy_version': self.local_canopy_version,
                'protocol_version': self.local_protocol_version,
                'signature': signature.hex()
            }
            
            websocket = connection.websocket
            if websocket is None:
                return False
            await websocket.send(json.dumps(ack))
            
            logger.debug(f"Signed handshake ack sent to {connection.peer_id}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to complete handshake: {e}", exc_info=True)
            return False
    
    async def _handle_peer_messages(self, connection: PeerConnection) -> None:
        """
        Handle incoming messages from a peer.
        
        Args:
            connection: PeerConnection instance
        """
        logger.debug(f"Started message handler for {connection.peer_id}")
        
        try:
            websocket = connection.websocket
            if websocket is None:
                return
            async for message in websocket:
                connection.update_inbound_activity()
                
                # Dispatch to registered handlers
                await self._dispatch_message(connection, message)
                
        except websockets.exceptions.ConnectionClosed:
            logger.info(f"Connection closed by {connection.peer_id}")
        except Exception as e:
            logger.error(f"Error handling messages from {connection.peer_id}: {e}", exc_info=True)
        finally:
            # Clean up connection
            await self._disconnect_connection(connection)
    
    async def _dispatch_message(self, connection: PeerConnection, message: str) -> None:
        """
        Dispatch message to appropriate handler.
        
        Args:
            connection: Source connection
            message: Raw message string
        """
        try:
            data = json.loads(message)
            
            message_type = data.get('type')
            if not message_type:
                logger.warning("Message missing type field")
                return
            
            # Call registered handler
            handler = self.message_handlers.get(message_type)
            if handler:
                await handler(connection, data)
            else:
                logger.warning(f"No handler for message type: {message_type}")
                
        except json.JSONDecodeError:
            logger.error("Invalid JSON message")
        except Exception as e:
            logger.error(f"Error dispatching message: {e}", exc_info=True)
    
    async def send_to_peer(self, peer_id: str, message: Dict[str, Any]) -> bool:
        """
        Send message to a peer.
        
        Args:
            peer_id: Target peer ID
            message: Message dictionary
            
        Returns:
            True if sent successfully
        """
        connection = self.connections.get(peer_id)
        if not connection or not connection.is_connected():
            logger.warning(f"Not connected to {peer_id}")
            return False
        
        try:
            data = json.dumps(message)
            async with connection._send_lock:
                current = self.connections.get(peer_id)
                if current is not connection or not connection.is_connected():
                    return False
                # 15-second timeout prevents hanging on dead connections
                # where TCP hasn't detected the failure yet.
                websocket = connection.websocket
                if websocket is None or getattr(websocket, 'closed', False):
                    return False
                await asyncio.wait_for(
                    websocket.send(data),
                    timeout=15.0
                )
            connection.update_outbound_activity()
            return True

        except asyncio.TimeoutError:
            connection.state = ConnectionState.DISCONNECTED
            connection.failure_reason = 'send_timeout'
            connection.failure_detail = 'Send timed out'
            logger.error(f"Failed to send to {peer_id}: "
                         f"send timed out (15s), connection likely dead")
            # Force-close the dead connection so it can be re-established
            asyncio.ensure_future(
                self._disconnect_connection(connection, notify=False))
            return False

        except websockets.exceptions.ConnectionClosed as e:
            connection.state = ConnectionState.DISCONNECTED
            connection.failure_reason = type(e).__name__
            connection.failure_detail = str(e)
            if e.rcvd and e.rcvd.code == 1000:
                logger.debug(f"Send to {peer_id} interrupted by normal close: {e}")
            else:
                logger.warning(f"Failed to send to {peer_id}: {e}")
            asyncio.ensure_future(
                self._disconnect_connection(connection, notify=False))
            return False

        except Exception as e:
            connection.state = ConnectionState.DISCONNECTED
            connection.failure_reason = type(e).__name__
            connection.failure_detail = str(e)
            logger.error(f"Failed to send to {peer_id}: {e}")
            asyncio.ensure_future(
                self._disconnect_connection(connection, notify=False))
            return False
    
    async def _disconnect_connection(self, connection: PeerConnection, *, notify: bool = True) -> None:
        """Disconnect a specific connection instance safely.

        Important: multiple WebSockets can exist transiently for the same peer_id
        (simultaneous outbound + inbound connect). We must not let an older
        message-handler task disconnect a newer, currently-active connection.
        """
        if not connection:
            return
        peer_id = connection.peer_id
        current = self.connections.get(peer_id)
        is_current = current is connection

        if is_current:
            logger.info(f"Disconnecting from {peer_id}")

        try:
            if connection.websocket:
                await connection.websocket.close()
        except Exception as e:
            logger.error(f"Error closing connection to {peer_id}: {e}")
        finally:
            connection.state = ConnectionState.DISCONNECTED
            if is_current:
                try:
                    del self.connections[peer_id]
                except KeyError:
                    pass
                # Notify about disconnect
                if notify and self.on_peer_disconnected:
                    try:
                        self.on_peer_disconnected(peer_id)
                    except Exception as cb_err:
                        logger.error(
                            f"on_peer_disconnected callback error for {peer_id}: {cb_err}",
                            exc_info=True,
                        )

    async def disconnect_peer(self, peer_id: str) -> None:
        """
        Disconnect from a peer.
        
        Args:
            peer_id: Peer to disconnect
        """
        connection = self.connections.get(peer_id)
        if not connection:
            return
        await self._disconnect_connection(connection, notify=True)
    
    async def _monitor_connections(self) -> None:
        """Monitor connections, send keepalive pings, and handle timeouts.

        Library auto-pings are disabled (ping_interval=None) because they
        can collide with application sends in the legacy websockets protocol
        and trigger AssertionError in _drain_helper.  Instead we send pings
        ourselves, serialized through the per-connection _send_lock.
        """
        while self._running:
            try:
                await asyncio.sleep(30)  # Check every 30 seconds
                
                current_time = time.time()
                timeout = 3600  # 1 hour — P2P peers should stay connected
                
                for peer_id in list(self.connections.keys()):
                    connection = self.connections.get(peer_id)
                    if not connection or not connection.is_connected():
                        continue

                    # Check for idle timeout
                    if connection.last_activity:
                        idle_time = current_time - connection.last_activity
                        if idle_time > timeout:
                            logger.warning(f"Connection to {peer_id} timed out (idle {idle_time:.0f}s)")
                            await self.disconnect_peer(peer_id)
                            continue

                    # Send keepalive ping through the send lock to avoid
                    # concurrent write assertions in the websockets library.
                    try:
                        t_ping = time.time()
                        async with connection._send_lock:
                            websocket = connection.websocket
                            if websocket is None:
                                await self.disconnect_peer(peer_id)
                                continue
                            pong = await websocket.ping()
                        await asyncio.wait_for(pong, timeout=30)
                        connection.last_ping_latency_ms = round((time.time() - t_ping) * 1000, 1)
                    except asyncio.TimeoutError:
                        logger.warning(f"Keepalive ping to {peer_id} timed out — disconnecting")
                        await self.disconnect_peer(peer_id)
                    except Exception as ping_err:
                        logger.warning(f"Keepalive ping to {peer_id} failed: {ping_err}")
                        await self.disconnect_peer(peer_id)
                            
            except Exception as e:
                logger.error(f"Error in connection monitor: {e}", exc_info=True)
    
    def register_message_handler(self, message_type: str, 
                                 handler: Callable[[PeerConnection, Dict[str, Any]], Awaitable[None]]) -> None:
        """
        Register handler for a message type.
        
        Args:
            message_type: Type of message to handle
            handler: Async function to handle message
        """
        self.message_handlers[message_type] = handler
        logger.debug(f"Registered handler for message type: {message_type}")
    
    def get_connected_peers(self) -> list[str]:
        """Get list of connected peer IDs."""
        return [
            peer_id for peer_id, conn in self.connections.items()
            if conn.is_connected()
        ]
    
    def get_connection(self, peer_id: str) -> Optional[PeerConnection]:
        """Get connection object for a peer."""
        return self.connections.get(peer_id)
    
    def is_connected(self, peer_id: str) -> bool:
        """Check if connected to a peer."""
        conn = self.connections.get(peer_id)
        return conn is not None and conn.is_connected()
