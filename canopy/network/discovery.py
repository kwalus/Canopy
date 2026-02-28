"""
Peer discovery mechanisms for Canopy P2P network.

Supports:
- mDNS (Multicast DNS) for local network discovery
- DHT (Distributed Hash Table) for remote peer discovery (future)

Author: Konrad Walus (architecture, design, and direction)
Project: Canopy - Local Mesh Communication
License: Apache 2.0
Development: AI-assisted implementation (Claude, Codex, GitHub Copilot, Cursor IDE, Ollama)
"""

import logging
import socket
import threading
import time
from typing import Callable, Dict, List, Optional, Set
from dataclasses import dataclass
from zeroconf import ServiceBrowser, ServiceInfo, Zeroconf, ServiceStateChange

logger = logging.getLogger('canopy.network.discovery')


@dataclass
class DiscoveredPeer:
    """Information about a discovered peer."""
    peer_id: str
    address: str
    port: int
    discovered_at: float
    service_info: Optional[Dict] = None


class PeerDiscovery:
    """Manages peer discovery on local and remote networks."""
    
    def __init__(self, local_peer_id: str, service_port: int = 7771, 
                 service_name: Optional[str] = None):
        """
        Initialize peer discovery.
        
        Args:
            local_peer_id: This peer's ID
            service_port: Port for P2P service
            service_name: Custom service name (default: peer_id)
        """
        self.local_peer_id = local_peer_id
        self.service_port = service_port
        self.service_name = service_name or f"canopy-{local_peer_id}"
        self.service_type = "_canopy._tcp.local."
        
        # State
        self.discovered_peers: Dict[str, DiscoveredPeer] = {}
        self.peer_callbacks: List[Callable[[DiscoveredPeer, bool], None]] = []
        self._running = False
        self._lock = threading.Lock()
        
        # Zeroconf components
        self.zeroconf: Optional[Zeroconf] = None
        self.service_info: Optional[ServiceInfo] = None
        self.browser: Optional[ServiceBrowser] = None
        
        logger.info(f"Initialized PeerDiscovery for {local_peer_id}")
    
    def start(self) -> None:
        """Start peer discovery service."""
        if self._running:
            logger.warning("Discovery already running")
            return
        
        try:
            logger.info("Starting peer discovery service...")
            
            # Initialize Zeroconf
            self.zeroconf = Zeroconf()
            
            # Register our service
            self._register_service()
            
            # Start browsing for peers
            self.browser = ServiceBrowser(
                self.zeroconf, 
                self.service_type, 
                handlers=[self._on_service_state_change]
            )
            
            self._running = True
            logger.info("Peer discovery service started")
            
        except Exception as e:
            logger.error(f"Failed to start discovery service: {e}", exc_info=True)
            raise
    
    def stop(self) -> None:
        """Stop peer discovery service."""
        if not self._running:
            return
        
        logger.info("Stopping peer discovery service...")
        self._running = False
        
        try:
            # Unregister service
            if self.zeroconf and self.service_info:
                logger.info("Unregistering service...")
                self.zeroconf.unregister_service(self.service_info)
            
            # Close browser
            if self.browser:
                self.browser.cancel()
            
            # Close Zeroconf
            if self.zeroconf:
                self.zeroconf.close()
            
            logger.info("Peer discovery service stopped")
            
        except Exception as e:
            logger.error(f"Error stopping discovery service: {e}", exc_info=True)
    
    def _register_service(self) -> None:
        """Register this peer's service on the network."""
        try:
            # Get local IP addresses
            addresses = self._get_local_addresses()
            
            if not addresses:
                logger.warning("No network interfaces found, using localhost")
                addresses = [socket.inet_aton("127.0.0.1")]
            
            # Create service info
            self.service_info = ServiceInfo(
                self.service_type,
                f"{self.service_name}.{self.service_type}",
                addresses=addresses,
                port=self.service_port,
                properties={
                    'peer_id': self.local_peer_id,
                    'version': '0.1.0',
                    'capabilities': 'chat,files,voice'
                },
                server=f"{self.service_name}.local."
            )
            
            # Register with Zeroconf
            zeroconf = self.zeroconf
            if zeroconf is None:
                raise RuntimeError("Zeroconf is not initialized")
            zeroconf.register_service(self.service_info)
            
            logger.info(f"Registered service: {self.service_name} on port {self.service_port}")
            logger.info(f"Addresses: {[socket.inet_ntoa(addr) for addr in addresses]}")
            
        except Exception as e:
            logger.error(f"Failed to register service: {e}", exc_info=True)
            raise
    
    def _get_local_addresses(self) -> List[bytes]:
        """Get local IP addresses for service registration."""
        addresses = []
        
        try:
            # Prefer the invite-code helper which tends to be more reliable across OSes.
            try:
                from .invite import get_local_ips
                for ip in get_local_ips():
                    if ip and not ip.startswith('127.'):
                        addresses.append(socket.inet_aton(ip))
            except Exception:
                pass

            # Try to get all network interfaces
            hostname = socket.gethostname()
            for addr_info in socket.getaddrinfo(hostname, None):
                family, socktype, proto, canonname, sockaddr = addr_info
                
                # Only IPv4 for now
                if family == socket.AF_INET:
                    ip_address = sockaddr[0]
                    # Skip loopback
                    if isinstance(ip_address, str) and not ip_address.startswith('127.'):
                        addresses.append(socket.inet_aton(ip_address))
            
        except Exception as e:
            logger.warning(f"Error getting local addresses: {e}")
        
        # De-dupe while preserving order (Zeroconf will happily take multiple)
        out: List[bytes] = []
        seen: Set[bytes] = set()
        for a in addresses:
            if a in seen:
                continue
            seen.add(a)
            out.append(a)
        return out
    
    def _on_service_state_change(self, zeroconf: Zeroconf, service_type: str, 
                                 name: str, state_change: ServiceStateChange) -> None:
        """
        Handle service state changes from Zeroconf browser.
        
        Called when peers are discovered, updated, or removed.
        """
        try:
            if state_change is ServiceStateChange.Added:
                self._on_service_added(zeroconf, service_type, name)
            elif state_change is ServiceStateChange.Updated:
                self._on_service_updated(zeroconf, service_type, name)
            elif state_change is ServiceStateChange.Removed:
                self._on_service_removed(zeroconf, service_type, name)
        except Exception as e:
            logger.error(f"Error handling service state change: {e}", exc_info=True)
    
    def _on_service_added(self, zeroconf: Zeroconf, service_type: str, name: str) -> None:
        """Handle newly discovered service."""
        logger.info(f"Service added: {name}")
        
        info = zeroconf.get_service_info(service_type, name)
        if not info:
            logger.warning(f"Could not get info for service: {name}")
            return
        
        # Extract peer information
        peer_id_raw = info.properties.get(b'peer_id')
        if not peer_id_raw:
            logger.warning(f"Service {name} has no peer_id")
            return

        if isinstance(peer_id_raw, bytes):
            peer_id = peer_id_raw.decode('utf-8')
        else:
            peer_id = str(peer_id_raw)
        
        # Ignore our own service
        if peer_id == self.local_peer_id:
            logger.debug("Ignoring own service")
            return
        
        # Get address and port
        if not info.addresses:
            logger.warning(f"Service {name} has no addresses")
            return
        
        address = socket.inet_ntoa(info.addresses[0])
        port = info.port
        if port is None:
            logger.warning(f"Service {name} has no port")
            return
        
        # Create discovered peer
        version_raw = info.properties.get(b'version', b'unknown')
        capabilities_raw = info.properties.get(b'capabilities', b'')
        version = version_raw.decode('utf-8') if isinstance(version_raw, bytes) else str(version_raw)
        capabilities_text = capabilities_raw.decode('utf-8') if isinstance(capabilities_raw, bytes) else str(capabilities_raw)
        peer = DiscoveredPeer(
            peer_id=peer_id,
            address=address,
            port=port,
            discovered_at=time.time(),
            service_info={
                'name': name,
                'version': version,
                'capabilities': capabilities_text.split(',')
            }
        )
        
        # Store discovered peer
        with self._lock:
            self.discovered_peers[peer_id] = peer
        
        logger.info(f"Discovered peer: {peer_id} at {address}:{port}")
        
        # Notify callbacks
        self._notify_callbacks(peer, added=True)
    
    def _on_service_updated(self, zeroconf: Zeroconf, service_type: str, name: str) -> None:
        """Handle service update."""
        logger.debug(f"Service updated: {name}")
        # Re-process as if added
        self._on_service_added(zeroconf, service_type, name)
    
    def _on_service_removed(self, zeroconf: Zeroconf, service_type: str, name: str) -> None:
        """Handle service removal."""
        logger.info(f"Service removed: {name}")
        
        # Find and remove peer
        with self._lock:
            peer_to_remove = None
            for peer_id, peer in self.discovered_peers.items():
                if peer.service_info and peer.service_info.get('name') == name:
                    peer_to_remove = peer
                    break
            
            if peer_to_remove:
                del self.discovered_peers[peer_to_remove.peer_id]
                logger.info(f"Removed peer: {peer_to_remove.peer_id}")
                
                # Notify callbacks
                self._notify_callbacks(peer_to_remove, added=False)
    
    def _notify_callbacks(self, peer: DiscoveredPeer, added: bool) -> None:
        """Notify registered callbacks about peer changes."""
        for callback in self.peer_callbacks:
            try:
                callback(peer, added)
            except Exception as e:
                logger.error(f"Error in peer callback: {e}", exc_info=True)
    
    def on_peer_discovered(self, callback: Callable[[DiscoveredPeer, bool], None]) -> None:
        """
        Register callback for peer discovery events.
        
        Args:
            callback: Function called with (peer, added) where added is True for new peers, False for removed
        """
        self.peer_callbacks.append(callback)
        logger.debug(f"Registered peer discovery callback: {callback.__name__}")
    
    def get_discovered_peers(self) -> List[DiscoveredPeer]:
        """Get list of currently discovered peers."""
        with self._lock:
            return list(self.discovered_peers.values())
    
    def get_peer(self, peer_id: str) -> Optional[DiscoveredPeer]:
        """Get specific discovered peer by ID."""
        with self._lock:
            return self.discovered_peers.get(peer_id)
    
    def is_peer_available(self, peer_id: str) -> bool:
        """Check if a peer is currently available."""
        with self._lock:
            return peer_id in self.discovered_peers


# Future: DHT-based discovery for remote peers
class DHTDiscovery:
    """
    DHT-based peer discovery for remote networks.
    
    Uses Kademlia DHT for decentralized peer discovery beyond local network.
    Implementation pending.
    """
    
    def __init__(self, local_peer_id: str, bootstrap_nodes: List[str]):
        """
        Initialize DHT discovery.
        
        Args:
            local_peer_id: This peer's ID
            bootstrap_nodes: List of bootstrap node addresses
        """
        self.local_peer_id = local_peer_id
        self.bootstrap_nodes = bootstrap_nodes
        logger.info("DHT discovery not yet implemented")
    
    def start(self) -> None:
        """Start DHT node."""
        raise NotImplementedError("DHT discovery not yet implemented")
    
    def stop(self) -> None:
        """Stop DHT node."""
        raise NotImplementedError("DHT discovery not yet implemented")
