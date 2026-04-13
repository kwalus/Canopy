"""
Peer identity management for Canopy P2P network.

Handles cryptographic identities using Ed25519 for signatures
and X25519 for key exchange.

Project: Canopy - Local Mesh Communication
License: Apache 2.0
"""

import os
import base58
import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Dict, Any
from dataclasses import dataclass, asdict
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey
)
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey
)
from cryptography.hazmat.primitives import serialization, hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

logger = logging.getLogger('canopy.network.identity')


@dataclass
class PeerIdentity:
    """
    Represents a peer's cryptographic identity.
    
    Each peer has two keypairs:
    - Ed25519: For signing and identity verification
    - X25519: For key exchange and encryption
    """
    peer_id: str  # Base58 encoded hash of Ed25519 public key
    ed25519_public_key: bytes
    x25519_public_key: bytes
    ed25519_private_key: Optional[bytes] = None  # None for remote peers
    x25519_private_key: Optional[bytes] = None  # None for remote peers
    
    def is_local(self) -> bool:
        """Check if this is the local peer (has private keys)."""
        return self.ed25519_private_key is not None
    
    def to_dict(self, include_private: bool = False) -> Dict[str, Any]:
        """Convert to dictionary for serialization."""
        data = {
            'peer_id': self.peer_id,
            'ed25519_public_key': base58.b58encode(self.ed25519_public_key).decode(),
            'x25519_public_key': base58.b58encode(self.x25519_public_key).decode(),
        }
        
        if include_private and self.is_local():
            ed25519_private_key = self.ed25519_private_key
            x25519_private_key = self.x25519_private_key
            if ed25519_private_key is not None and x25519_private_key is not None:
                data['ed25519_private_key'] = base58.b58encode(ed25519_private_key).decode()
                data['x25519_private_key'] = base58.b58encode(x25519_private_key).decode()
        
        return data
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'PeerIdentity':
        """Create PeerIdentity from dictionary."""
        return cls(
            peer_id=data['peer_id'],
            ed25519_public_key=base58.b58decode(data['ed25519_public_key']),
            ed25519_private_key=base58.b58decode(data['ed25519_private_key']) if 'ed25519_private_key' in data else None,
            x25519_public_key=base58.b58decode(data['x25519_public_key']),
            x25519_private_key=base58.b58decode(data['x25519_private_key']) if 'x25519_private_key' in data else None,
        )
    
    def sign(self, message: bytes) -> bytes:
        """Sign a message with Ed25519 private key."""
        if not self.is_local():
            raise ValueError("Cannot sign without private key")
        
        private_key_bytes = self.ed25519_private_key
        if private_key_bytes is None:
            raise ValueError("Cannot sign without private key")
        private_key = Ed25519PrivateKey.from_private_bytes(private_key_bytes)
        return private_key.sign(message)
    
    def verify(self, message: bytes, signature: bytes) -> bool:
        """Verify a signature with Ed25519 public key."""
        try:
            public_key = Ed25519PublicKey.from_public_bytes(self.ed25519_public_key)
            public_key.verify(signature, message)
            return True
        except Exception as e:
            logger.warning(f"Signature verification failed: {e}")
            return False
    
    def derive_shared_secret(self, remote_public_key: bytes) -> bytes:
        """
        Derive shared secret using X25519 key exchange.
        
        Args:
            remote_public_key: Remote peer's X25519 public key
            
        Returns:
            32-byte shared secret
        """
        if not self.is_local():
            raise ValueError("Cannot derive shared secret without private key")
        
        # Load keys
        private_key_bytes = self.x25519_private_key
        if private_key_bytes is None:
            raise ValueError("Cannot derive shared secret without private key")
        private_key = X25519PrivateKey.from_private_bytes(private_key_bytes)
        public_key = X25519PublicKey.from_public_bytes(remote_public_key)
        
        # Perform key exchange
        shared_secret = private_key.exchange(public_key)
        
        # Derive a 256-bit key using HKDF
        derived_key = HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=None,
            info=b'canopy-p2p-encryption',
        ).derive(shared_secret)
        
        return derived_key


class IdentityManager:
    """Manages peer identities and cryptographic operations."""
    
    def __init__(self, identity_path: Optional[Path] = None):
        """
        Initialize identity manager.
        
        Args:
            identity_path: Path to store/load peer identity. If None, generates ephemeral identity.
        """
        self.identity_path = identity_path or Path('./data/peer_identity.json')
        self.local_identity: Optional[PeerIdentity] = None
        self.known_peers: Dict[str, PeerIdentity] = {}
        
        # Endpoint info for known peers (peer_id -> list of endpoint strings)
        self.peer_endpoints: Dict[str, list] = {}
        # Display names for known peers (peer_id -> display_name)
        self.peer_display_names: Dict[str, str] = {}
        # Meshspace hints learned from invites/handshakes (peer_id -> metadata).
        # These hints are not cryptographic authorization; they are safety rails
        # for preventing silent reconnects across visibly different meshspaces.
        self.peer_meshspace_hints: Dict[str, Dict[str, Any]] = {}
        
        # Persistence path for known peers
        self._known_peers_path = self.identity_path.parent / 'known_peers.json'
        
        logger.info(f"Initializing IdentityManager with path: {self.identity_path}")
    
    def initialize(self) -> PeerIdentity:
        """
        Initialize or load local peer identity.
        
        Returns:
            Local peer identity
        """
        if self.identity_path.exists():
            logger.info("Loading existing peer identity")
            self.local_identity = self._load_identity()
        else:
            logger.info("Generating new peer identity")
            self.local_identity = self._generate_identity()
            self._save_identity(self.local_identity)
        
        # Load persisted known peers
        self._load_known_peers()
        
        logger.info(f"Local peer ID: {self.local_identity.peer_id}")
        return self.local_identity
    
    def _generate_identity(self) -> PeerIdentity:
        """Generate a new peer identity with fresh keypairs."""
        logger.info("Generating Ed25519 signing keypair...")
        ed25519_private_key = Ed25519PrivateKey.generate()
        ed25519_public_key = ed25519_private_key.public_key()
        
        logger.info("Generating X25519 key exchange keypair...")
        x25519_private_key = X25519PrivateKey.generate()
        x25519_public_key = x25519_private_key.public_key()
        
        # Serialize keys to bytes
        ed25519_private_bytes = ed25519_private_key.private_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PrivateFormat.Raw,
            encryption_algorithm=serialization.NoEncryption()
        )
        ed25519_public_bytes = ed25519_public_key.public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw
        )
        
        x25519_private_bytes = x25519_private_key.private_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PrivateFormat.Raw,
            encryption_algorithm=serialization.NoEncryption()
        )
        x25519_public_bytes = x25519_public_key.public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw
        )
        
        # Generate peer ID from Ed25519 public key
        peer_id = self._generate_peer_id(ed25519_public_bytes)
        
        logger.info(f"Generated peer identity with ID: {peer_id}")
        
        return PeerIdentity(
            peer_id=peer_id,
            ed25519_public_key=ed25519_public_bytes,
            ed25519_private_key=ed25519_private_bytes,
            x25519_public_key=x25519_public_bytes,
            x25519_private_key=x25519_private_bytes
        )
    
    def _generate_peer_id(self, public_key: bytes) -> str:
        """
        Generate peer ID from public key.
        
        Peer ID is base58-encoded SHA-256 hash of Ed25519 public key.
        """
        hash_digest = hashlib.sha256(public_key).digest()
        return base58.b58encode(hash_digest).decode()[:16]  # Truncate for readability
    
    def _save_identity(self, identity: PeerIdentity) -> None:
        """Save identity to disk."""
        try:
            # Ensure directory exists
            self.identity_path.parent.mkdir(parents=True, exist_ok=True)
            
            # Save with private keys
            data = identity.to_dict(include_private=True)
            
            with open(self.identity_path, 'w') as f:
                json.dump(data, f, indent=2)
            
            # Set restrictive permissions (owner read/write only)
            os.chmod(self.identity_path, 0o600)
            
            logger.info(f"Saved peer identity to {self.identity_path}")
        except Exception as e:
            logger.error(f"Failed to save identity: {e}", exc_info=True)
            raise
    
    def _load_identity(self) -> PeerIdentity:
        """Load identity from disk."""
        try:
            with open(self.identity_path, 'r') as f:
                data = json.load(f)
            
            identity = PeerIdentity.from_dict(data)
            logger.info(f"Loaded peer identity: {identity.peer_id}")
            return identity
        except Exception as e:
            logger.error(f"Failed to load identity: {e}", exc_info=True)
            raise
    
    def _save_known_peers(self) -> None:
        """Persist known peers (public keys + endpoints) to disk."""
        try:
            peers_data = []
            for pid, identity in self.known_peers.items():
                if self.local_identity and pid == self.local_identity.peer_id:
                    continue  # never persist our own peer_id here
                if identity.is_local():
                    continue  # don't persist our own identity here
                entry = identity.to_dict(include_private=False)
                entry['endpoints'] = self.peer_endpoints.get(pid, [])
                entry['display_name'] = self.peer_display_names.get(pid, '')
                hint = self.peer_meshspace_hints.get(pid)
                if isinstance(hint, dict) and hint:
                    entry['meshspace_hint'] = hint
                peers_data.append(entry)

            self._known_peers_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._known_peers_path, 'w') as f:
                json.dump(peers_data, f, indent=2)
            logger.debug(f"Saved {len(peers_data)} known peers to {self._known_peers_path}")
        except Exception as e:
            logger.warning(f"Failed to save known peers: {e}")

    def _load_known_peers(self) -> None:
        """Load known peers from disk."""
        if not self._known_peers_path.exists():
            return
        try:
            with open(self._known_peers_path, 'r') as f:
                peers_data = json.load(f)
            loaded = 0
            for entry in peers_data:
                try:
                    # Never load our own peer_id as a "known peer". If it got persisted
                    # (e.g., from an accidental self-connect), it can cause reconnect churn.
                    if (
                        self.local_identity
                        and entry.get('peer_id') == self.local_identity.peer_id
                    ):
                        continue
                    identity = PeerIdentity.from_dict(entry)
                    self.known_peers[identity.peer_id] = identity
                    if entry.get('endpoints'):
                        self.peer_endpoints[identity.peer_id] = entry['endpoints']
                    if entry.get('display_name'):
                        self.peer_display_names[identity.peer_id] = entry['display_name']
                    hint = entry.get('meshspace_hint')
                    if isinstance(hint, dict):
                        cleaned = self._normalize_meshspace_hint(hint)
                        if cleaned:
                            self.peer_meshspace_hints[identity.peer_id] = cleaned
                    loaded += 1
                except Exception as e:
                    logger.warning(f"Skipping invalid known peer entry: {e}")
            logger.info(f"Loaded {loaded} known peers from {self._known_peers_path}")
        except Exception as e:
            logger.warning(f"Failed to load known peers: {e}")

    def add_known_peer(self, identity: PeerIdentity,
                       endpoints: Optional[list] = None,
                       display_name: Optional[str] = None) -> None:
        """Add a known peer's identity and optionally persist."""
        if self.local_identity and identity.peer_id == self.local_identity.peer_id:
            return
        self.known_peers[identity.peer_id] = identity
        if endpoints:
            self.peer_endpoints[identity.peer_id] = endpoints
        if display_name:
            self.peer_display_names[identity.peer_id] = display_name
        self._save_known_peers()
        logger.debug(f"Added known peer: {identity.peer_id}")

    @staticmethod
    def _normalize_meshspace_hint(raw: Dict[str, Any]) -> Dict[str, Any]:
        """Return a compact, persistence-safe meshspace hint dict."""
        out: Dict[str, Any] = {}
        field_map = {
            'meshspace_id': 160,
            'meshspace_name': 160,
            'meshspace_fingerprint': 64,
            'source': 64,
            'last_source': 64,
            'sync_approval_scope': 16,
            'sync_approved_at': 64,
            'mesh_relationship': 48,
            'meshspace_avatar_mime': 80,
        }
        for key, limit in field_map.items():
            text = str(raw.get(key) or '').strip()
            if text:
                out[key] = text[:limit]
        aliases = raw.get('meshspace_id_aliases')
        if isinstance(aliases, (list, tuple, set)):
            cleaned_aliases = []
            current_id = str(out.get('meshspace_id') or '').strip()
            for value in aliases:
                alias = str(value or '').strip()
                if not alias or alias == current_id or alias in cleaned_aliases:
                    continue
                cleaned_aliases.append(alias[:160])
            if cleaned_aliases:
                out['meshspace_id_aliases'] = cleaned_aliases[:8]
        avatar_b64 = str(raw.get('meshspace_avatar_b64') or '').strip()
        if avatar_b64:
            out['meshspace_avatar_b64'] = avatar_b64[:65536]
        if 'cross_mesh_allowed' in raw:
            out['cross_mesh_allowed'] = bool(raw.get('cross_mesh_allowed'))
        if 'last_seen_at' in raw:
            text = str(raw.get('last_seen_at') or '').strip()
            if text:
                out['last_seen_at'] = text[:64]
        return out

    def record_peer_meshspace_hint(
        self,
        peer_id: str,
        *,
        meshspace_id: Optional[str] = None,
        meshspace_id_aliases: Optional[list[str]] = None,
        meshspace_name: Optional[str] = None,
        meshspace_fingerprint: Optional[str] = None,
        meshspace_avatar_b64: Optional[str] = None,
        meshspace_avatar_mime: Optional[str] = None,
        source: str = '',
        cross_mesh_allowed: Optional[bool] = None,
        mesh_relationship: Optional[str] = None,
    ) -> None:
        """Persist the meshspace identity a peer advertised.

        The hint is intentionally separate from cryptographic peer identity.
        It supports UI review and avoids automatic reconnects silently crossing
        meshspace boundaries.
        """
        if not peer_id:
            return
        if self.local_identity and peer_id == self.local_identity.peer_id:
            return

        incoming: Dict[str, Any] = {
            'meshspace_id': meshspace_id,
            'meshspace_id_aliases': meshspace_id_aliases or [],
            'meshspace_name': meshspace_name,
            'meshspace_fingerprint': meshspace_fingerprint,
            'meshspace_avatar_b64': meshspace_avatar_b64,
            'meshspace_avatar_mime': meshspace_avatar_mime,
            'last_source': source,
        }
        if cross_mesh_allowed is not None:
            incoming['cross_mesh_allowed'] = bool(cross_mesh_allowed)
        if mesh_relationship is not None:
            incoming['mesh_relationship'] = str(mesh_relationship or '').strip()

        normalized = self._normalize_meshspace_hint(incoming)
        existing = dict(self.peer_meshspace_hints.get(peer_id) or {})
        changed = False
        for key, value in normalized.items():
            if value is None or value == '':
                continue
            if existing.get(key) != value:
                existing[key] = value
                changed = True
        if source and existing.get('last_source') != source:
            existing['last_source'] = source
            changed = True
        if normalized.get('meshspace_id_aliases') and existing.get('meshspace_id_aliases') != normalized.get('meshspace_id_aliases'):
            existing['meshspace_id_aliases'] = list(normalized.get('meshspace_id_aliases') or [])
            changed = True
        if normalized.get('meshspace_avatar_b64') and existing.get('meshspace_avatar_b64') != normalized.get('meshspace_avatar_b64'):
            existing['meshspace_avatar_b64'] = normalized.get('meshspace_avatar_b64')
            changed = True
        if normalized.get('meshspace_avatar_mime') and existing.get('meshspace_avatar_mime') != normalized.get('meshspace_avatar_mime'):
            existing['meshspace_avatar_mime'] = normalized.get('meshspace_avatar_mime')
            changed = True
        if changed:
            self.peer_meshspace_hints[peer_id] = self._normalize_meshspace_hint(existing)
            self._save_known_peers()

    def get_peer_meshspace_hint(self, peer_id: str) -> Dict[str, Any]:
        """Return the persisted meshspace hint for a peer, if any."""
        return dict(self.peer_meshspace_hints.get(peer_id, {}) or {})

    @staticmethod
    def _meshspace_hints_match(left: Dict[str, Any], right: Dict[str, Any]) -> bool:
        """Return True when two peer hints appear to refer to the same meshspace."""
        left_fingerprint = str(left.get('meshspace_fingerprint') or '').strip()
        right_fingerprint = str(right.get('meshspace_fingerprint') or '').strip()
        if left_fingerprint and right_fingerprint:
            return left_fingerprint == right_fingerprint
        left_ids = {str(left.get('meshspace_id') or '').strip()}
        right_ids = {str(right.get('meshspace_id') or '').strip()}
        left_ids.update(str(item or '').strip() for item in (left.get('meshspace_id_aliases') or []))
        right_ids.update(str(item or '').strip() for item in (right.get('meshspace_id_aliases') or []))
        left_ids.discard('')
        right_ids.discard('')
        if left_ids and right_ids:
            return bool(left_ids.intersection(right_ids))
        return False

    def set_peer_sync_approval(self, peer_id: str, scope: Optional[str]) -> None:
        """Persist whether a peer is approved for sync preview exit."""
        clean_peer = str(peer_id or '').strip()
        if not clean_peer:
            return
        if self.local_identity and clean_peer == self.local_identity.peer_id:
            return
        clean_scope = str(scope or '').strip().lower()
        if clean_scope not in {'', 'peer', 'mesh'}:
            raise ValueError(f"Unsupported sync approval scope: {scope}")
        existing = dict(self.peer_meshspace_hints.get(clean_peer) or {})
        changed = False
        if clean_scope:
            if existing.get('sync_approval_scope') != clean_scope:
                existing['sync_approval_scope'] = clean_scope
                changed = True
            approved_at = datetime.now(timezone.utc).isoformat()
            if existing.get('sync_approved_at') != approved_at:
                existing['sync_approved_at'] = approved_at
                changed = True
        else:
            for key in ('sync_approval_scope', 'sync_approved_at'):
                if key in existing:
                    del existing[key]
                    changed = True
        if changed:
            normalized = self._normalize_meshspace_hint(existing)
            if normalized:
                self.peer_meshspace_hints[clean_peer] = normalized
            else:
                self.peer_meshspace_hints.pop(clean_peer, None)
            self._save_known_peers()

    def get_peer_sync_approval_status(self, peer_id: str) -> Dict[str, Any]:
        """Return explicit/effective sync approval state for a peer."""
        clean_peer = str(peer_id or '').strip()
        hint = dict(self.peer_meshspace_hints.get(clean_peer, {}) or {})
        explicit_scope = str(hint.get('sync_approval_scope') or '').strip().lower()
        effective_scope = explicit_scope if explicit_scope in {'peer', 'mesh'} else ''
        inherited_from_peer_id = ''
        if not effective_scope and hint:
            for other_peer_id, other_hint in self.peer_meshspace_hints.items():
                if other_peer_id == clean_peer or not isinstance(other_hint, dict):
                    continue
                other_scope = str(other_hint.get('sync_approval_scope') or '').strip().lower()
                if other_scope != 'mesh':
                    continue
                if self._meshspace_hints_match(hint, other_hint):
                    effective_scope = 'mesh'
                    inherited_from_peer_id = other_peer_id
                    break
        approved_at = ''
        if effective_scope == explicit_scope:
            approved_at = str(hint.get('sync_approved_at') or '').strip()
        elif inherited_from_peer_id:
            inherited_hint = dict(self.peer_meshspace_hints.get(inherited_from_peer_id, {}) or {})
            approved_at = str(inherited_hint.get('sync_approved_at') or '').strip()
        return {
            'peer_id': clean_peer,
            'explicit_scope': explicit_scope,
            'effective_scope': effective_scope,
            'preview_only': not bool(effective_scope),
            'approved_at': approved_at,
            'inherited_from_peer_id': inherited_from_peer_id,
        }

    def record_endpoint(self, peer_id: str, endpoint: str, *, claim: bool = True) -> None:
        """Record an endpoint for a peer and optionally "claim" it.

        Claiming removes this exact endpoint string from all other peers.
        This helps prevent stale endpoint reuse (e.g., DHCP IP reuse) from
        causing repeated reconnect attempts against the wrong peer identity.
        """
        if not peer_id or not endpoint:
            return
        if self.local_identity and peer_id == self.local_identity.peer_id:
            return

        endpoints = self.peer_endpoints.setdefault(peer_id, [])
        if endpoint not in endpoints:
            endpoints.append(endpoint)

        if claim:
            for other_peer_id, other_endpoints in list(self.peer_endpoints.items()):
                if other_peer_id == peer_id:
                    continue
                if endpoint in other_endpoints:
                    self.peer_endpoints[other_peer_id] = [e for e in other_endpoints if e != endpoint]

        self._save_known_peers()

    def remove_endpoint(self, peer_id: str, endpoint: str) -> None:
        """Remove an endpoint from a peer's endpoint list and persist."""
        if not peer_id or not endpoint:
            return
        if self.local_identity and peer_id == self.local_identity.peer_id:
            return
        endpoints = self.peer_endpoints.get(peer_id)
        if not endpoints:
            return
        if endpoint not in endpoints:
            return
        self.peer_endpoints[peer_id] = [e for e in endpoints if e != endpoint]
        self._save_known_peers()

    def remove_known_peer(self, peer_id: str) -> bool:
        """Remove a peer from known peers and persisted endpoints."""
        if not peer_id:
            return False
        if self.local_identity and peer_id == self.local_identity.peer_id:
            return False
        changed = False
        if peer_id in self.known_peers:
            del self.known_peers[peer_id]
            changed = True
        if peer_id in self.peer_endpoints:
            del self.peer_endpoints[peer_id]
            changed = True
        if peer_id in self.peer_display_names:
            del self.peer_display_names[peer_id]
            changed = True
        if peer_id in self.peer_meshspace_hints:
            del self.peer_meshspace_hints[peer_id]
            changed = True
        if changed:
            self._save_known_peers()
        return changed
    
    def get_peer(self, peer_id: str) -> Optional[PeerIdentity]:
        """Get known peer identity by ID."""
        return self.known_peers.get(peer_id)
    
    def create_remote_peer(self, peer_id: str, ed25519_public_key: bytes,
                          x25519_public_key: bytes,
                          endpoints: Optional[list] = None,
                          display_name: Optional[str] = None,
                          meshspace_hint: Optional[Dict[str, Any]] = None,
                          cross_mesh_allowed: Optional[bool] = None) -> PeerIdentity:
        """
        Create identity for a remote peer (without private keys).
        
        Args:
            peer_id: Peer's ID
            ed25519_public_key: Peer's Ed25519 public key
            x25519_public_key: Peer's X25519 public key
            endpoints: Optional list of endpoint strings
            display_name: Optional display name for the peer
            
        Returns:
            PeerIdentity for the remote peer
        """
        identity = PeerIdentity(
            peer_id=peer_id,
            ed25519_public_key=ed25519_public_key,
            x25519_public_key=x25519_public_key
        )
        
        self.add_known_peer(identity, endpoints=endpoints,
                           display_name=display_name)
        if isinstance(meshspace_hint, dict) and meshspace_hint:
            self.record_peer_meshspace_hint(
                peer_id,
                meshspace_id=meshspace_hint.get('meshspace_id'),
                meshspace_id_aliases=meshspace_hint.get('meshspace_id_aliases'),
                meshspace_name=meshspace_hint.get('meshspace_name'),
                meshspace_fingerprint=meshspace_hint.get('meshspace_fingerprint'),
                meshspace_avatar_b64=meshspace_hint.get('meshspace_avatar_b64'),
                meshspace_avatar_mime=meshspace_hint.get('meshspace_avatar_mime'),
                source=str(meshspace_hint.get('source') or 'invite'),
                cross_mesh_allowed=cross_mesh_allowed,
            )
        return identity
    
    def verify_peer_id(self, peer_id: str, ed25519_public_key: bytes) -> bool:
        """
        Verify that a peer ID matches the public key.
        
        Args:
            peer_id: Claimed peer ID
            ed25519_public_key: Ed25519 public key
            
        Returns:
            True if peer ID is valid for the public key
        """
        expected_peer_id = self._generate_peer_id(ed25519_public_key)
        return peer_id == expected_peer_id
    
    def export_public_identity(self) -> Dict[str, str]:
        """
        Export local peer's public identity for sharing.
        
        Returns:
            Dictionary with peer_id and public keys
        """
        if not self.local_identity:
            raise ValueError("Local identity not initialized")
        
        return {
            'peer_id': self.local_identity.peer_id,
            'ed25519_public_key': base58.b58encode(self.local_identity.ed25519_public_key).decode(),
            'x25519_public_key': base58.b58encode(self.local_identity.x25519_public_key).decode(),
        }
