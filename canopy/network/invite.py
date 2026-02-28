"""
Invite code system for Canopy P2P connections.

Generates and parses compact invite codes that encode peer identity
and connection endpoints. A friend can scan/paste the code to connect.

Invite payload (JSON, then base64url-encoded):
{
    "v": 1,
    "pid": "<peer_id>",
    "epk": "<ed25519_public_key base58>",
    "xpk": "<x25519_public_key base58>",
    "ep": ["ws://<ip>:<port>"]     # list of endpoints to try
}

Author: Konrad Walus (architecture, design, and direction)
Project: Canopy - Local Mesh Communication
License: Apache 2.0
Development: AI-assisted implementation (Claude, Codex, GitHub Copilot, Cursor IDE, Ollama)
"""

import base64
import json
import logging
import socket
from typing import Dict, List, Optional, Any
from dataclasses import dataclass

import base58

logger = logging.getLogger('canopy.network.invite')


@dataclass
class InviteCode:
    """Parsed invite code with peer identity and endpoints."""
    peer_id: str
    ed25519_public_key_b58: str
    x25519_public_key_b58: str
    endpoints: List[str]
    version: int = 1

    def to_dict(self) -> Dict[str, Any]:
        return {
            'v': self.version,
            'pid': self.peer_id,
            'epk': self.ed25519_public_key_b58,
            'xpk': self.x25519_public_key_b58,
            'ep': self.endpoints,
        }

    def encode(self) -> str:
        """Encode invite as a compact base64url string prefixed with 'canopy:'."""
        payload = json.dumps(self.to_dict(), separators=(',', ':'))
        b64 = base64.urlsafe_b64encode(payload.encode('utf-8')).decode('ascii').rstrip('=')
        return f"canopy:{b64}"

    @classmethod
    def decode(cls, code: str) -> 'InviteCode':
        """
        Decode an invite code string back into an InviteCode.

        Accepts:
          - 'canopy:<base64url>'
          - raw base64url
          - raw JSON string
        """
        code = code.strip()

        # Strip prefix
        if code.startswith('canopy:'):
            code = code[len('canopy:'):]

        # Try base64url decode
        try:
            # Restore padding
            padding = 4 - len(code) % 4
            if padding != 4:
                code += '=' * padding
            raw = base64.urlsafe_b64decode(code).decode('utf-8')
        except Exception:
            raw = code  # maybe it's raw JSON

        data = json.loads(raw)
        return cls(
            version=data.get('v', 1),
            peer_id=data['pid'],
            ed25519_public_key_b58=data['epk'],
            x25519_public_key_b58=data['xpk'],
            endpoints=data.get('ep', []),
        )


def get_local_ips() -> List[str]:
    """Get the machine's LAN IP addresses (non-loopback)."""
    ips = []
    try:
        # Connect to a public address to find default interface IP
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.5)
        s.connect(('8.8.8.8', 80))
        default_ip = s.getsockname()[0]
        s.close()
        if default_ip and default_ip != '0.0.0.0':
            ips.append(default_ip)
    except Exception:
        pass

    # Also try hostname resolution
    try:
        hostname = socket.gethostname()
        for addr in socket.getaddrinfo(hostname, None, socket.AF_INET):
            ip = addr[4][0]
            if isinstance(ip, str) and ip not in ips and not ip.startswith('127.'):
                ips.append(ip)
    except Exception:
        pass

    return ips


def generate_invite(identity_manager: Any, mesh_port: int,
                    public_host: Optional[str] = None,
                    public_port: Optional[int] = None) -> InviteCode:
    """
    Generate an invite code from the local peer identity.

    Args:
        identity_manager: IdentityManager with initialised local identity
        mesh_port: The local P2P mesh port (e.g. 7771)
        public_host: Optional public/external IP or hostname
        public_port: Optional public port (if port-forwarded)

    Returns:
        InviteCode ready to .encode()
    """
    local = identity_manager.local_identity
    if not local:
        raise ValueError("Local identity not initialized")

    epk = base58.b58encode(local.ed25519_public_key).decode()
    xpk = base58.b58encode(local.x25519_public_key).decode()

    endpoints: List[str] = []

    # Public / port-forwarded endpoint first
    if public_host:
        port = public_port or mesh_port
        endpoints.append(f"ws://{public_host}:{port}")

    # LAN endpoints
    for ip in get_local_ips():
        ep = f"ws://{ip}:{mesh_port}"
        if ep not in endpoints:
            endpoints.append(ep)

    invite = InviteCode(
        peer_id=local.peer_id,
        ed25519_public_key_b58=epk,
        x25519_public_key_b58=xpk,
        endpoints=endpoints,
    )

    logger.info(f"Generated invite code for peer {local.peer_id} with {len(endpoints)} endpoint(s)")
    return invite


def import_invite(identity_manager: Any, connection_manager: Any, invite: InviteCode) -> Dict[str, Any]:
    """
    Import an invite code: register the remote peer identity and
    return info needed to attempt connection.

    Args:
        identity_manager: IdentityManager to register the peer
        connection_manager: ConnectionManager (unused here, but available)
        invite: Parsed InviteCode

    Returns:
        dict with peer_id, endpoints, and status
    """
    # Decode public keys
    ed25519_pub = base58.b58decode(invite.ed25519_public_key_b58)
    x25519_pub = base58.b58decode(invite.x25519_public_key_b58)

    # Verify peer_id matches public key
    if not identity_manager.verify_peer_id(invite.peer_id, ed25519_pub):
        raise ValueError("Peer ID does not match public key — possible tampering")

    # Register as known peer
    identity_manager.create_remote_peer(invite.peer_id, ed25519_pub, x25519_pub)

    logger.info(f"Imported invite for peer {invite.peer_id} with {len(invite.endpoints)} endpoint(s)")
    return {
        'peer_id': invite.peer_id,
        'endpoints': invite.endpoints,
        'status': 'imported',
    }
