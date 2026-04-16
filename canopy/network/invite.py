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
    "ep": ["ws://<ip>:<port>"],    # list of endpoints to try
    "mn": "<mesh_name>",           # optional mesh hint
    "mid": "<meshspace_id>",       # optional stable meshspace id hint
    "mia": ["<old_meshspace_id>"], # optional legacy/current alias hints
    "mf": "<mesh_fingerprint>",    # optional short human check code
    "mab": "<mesh_avatar_b64>",    # optional compact mesh avatar preview
    "mam": "image/png",            # optional mesh avatar mime
    "pl": "<peer_label>",          # optional node/display hint
    "il": "<instance_label>",      # optional device/instance hint
    "pi": "<peer_icon>",           # optional standard node icon hint
    "ts": "<generated_at>"         # optional ISO timestamp
}

Project: Canopy - Local Mesh Communication
License: Apache 2.0
"""

import base64
import hashlib
import json
import logging
import re
import socket
from urllib.parse import urlparse
from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass

import base58

logger = logging.getLogger('canopy.network.invite')


def _format_endpoint_host(host: str) -> str:
    """Format a host for endpoint rendering, preserving IPv6 brackets."""
    text = str(host or '').strip()
    if ':' in text and not text.startswith('['):
        return f'[{text}]'
    return text


def parse_invite_endpoint(endpoint: str) -> Optional[Tuple[str, int, str]]:
    """Parse a ws/wss invite endpoint into (host, port, scheme)."""
    text = str(endpoint or '').strip()
    if not text:
        return None
    had_explicit_scheme = '://' in text
    if not had_explicit_scheme:
        text = f'ws://{text}'
    try:
        parsed = urlparse(text)
        host = parsed.hostname
        scheme = parsed.scheme or 'ws'
        port = parsed.port
    except Exception:
        return None
    if port is None and had_explicit_scheme:
        port = 443 if scheme == 'wss' else 80 if scheme == 'ws' else None
    if scheme not in ('ws', 'wss') or not host or not port:
        return None
    return host, port, scheme


def canonicalize_invite_endpoint(endpoint: str) -> Optional[str]:
    """Normalize an invite endpoint and drop unusable values."""
    parsed = parse_invite_endpoint(endpoint)
    if not parsed:
        return None
    host, port, scheme = parsed
    if host in ('0.0.0.0', 'localhost') or host.startswith('127.'):
        return None
    return f'{scheme}://{_format_endpoint_host(host)}:{port}'


def _endpoint_host_key(endpoint: str) -> str:
    parsed = parse_invite_endpoint(endpoint)
    if not parsed:
        return ''
    host, _, _ = parsed
    return str(host or '').strip().lower()


def _is_lan_endpoint(endpoint: str) -> bool:
    parsed = parse_invite_endpoint(endpoint)
    if not parsed:
        return False
    host, _, _ = parsed
    text = str(host or '').strip().lower()
    if not text:
        return False
    if text == 'localhost' or text.endswith('.local'):
        return True
    try:
        import ipaddress

        ip = ipaddress.ip_address(text)
        return bool(ip.is_private or ip.is_link_local or ip.is_loopback)
    except ValueError:
        return False


def _is_same_host(endpoint_a: str, endpoint_b: str) -> bool:
    a = _endpoint_host_key(endpoint_a)
    b = _endpoint_host_key(endpoint_b)
    return bool(a and b and a == b)


def _sanitize_invite_endpoints(endpoints: List[str]) -> List[str]:
    """Keep only canonical, dialable invite endpoints in stable order."""
    out: List[str] = []
    seen = set()
    for endpoint in endpoints or []:
        canon = canonicalize_invite_endpoint(endpoint)
        if not canon or canon in seen:
            continue
        seen.add(canon)
        out.append(canon)
    return out


def classify_invite_endpoints(endpoints: List[str]) -> Dict[str, Any]:
    """Classify invite endpoints for operator UX without changing invite format."""
    sanitized = _sanitize_invite_endpoints(endpoints or [])
    public_secure: List[str] = []
    public_plain: List[str] = []
    lan_endpoints: List[str] = []

    for endpoint in sanitized:
        parsed = parse_invite_endpoint(endpoint)
        if not parsed:
            continue
        _, _, scheme = parsed
        if _is_lan_endpoint(endpoint):
            lan_endpoints.append(endpoint)
        elif scheme == 'wss':
            public_secure.append(endpoint)
        else:
            public_plain.append(endpoint)

    recommended = next((ep for ep in sanitized if ep.lower().startswith('wss://')), sanitized[0] if sanitized else '')
    recommended_transport = ''
    parsed_recommended = parse_invite_endpoint(recommended) if recommended else None
    if parsed_recommended:
        recommended_transport = parsed_recommended[2]

    if public_secure and public_plain:
        policy = 'mixed_public_secure_plain'
        policy_label = 'Mixed secure/plain public invite'
    elif public_secure:
        policy = 'secure_public'
        policy_label = 'Secure public invite'
    elif public_plain:
        policy = 'plain_public'
        policy_label = 'Plain public invite'
    elif any(ep.lower().startswith('wss://') for ep in lan_endpoints):
        policy = 'secure_lan'
        policy_label = 'Secure LAN invite'
    elif lan_endpoints:
        policy = 'lan_only'
        policy_label = 'LAN-only invite'
    else:
        policy = 'no_direct_endpoints'
        policy_label = 'No direct endpoints'

    return {
        'recommended_endpoint': recommended,
        'recommended_transport': recommended_transport,
        'public_secure_endpoints': public_secure,
        'public_plain_endpoints': public_plain,
        'lan_endpoints': lan_endpoints,
        'has_public_plain_fallback': bool(public_secure and public_plain),
        'has_lan_fallback': bool(lan_endpoints),
        'invite_policy': policy,
        'invite_policy_label': policy_label,
    }


def meshspace_fingerprint(meshspace_id: Optional[str], mesh_name: Optional[str] = None) -> str:
    """Return a short stable human check code for a meshspace hint."""
    seed = str(meshspace_id or '').strip() or str(mesh_name or '').strip()
    if not seed:
        return ''
    digest = hashlib.sha256(seed.encode('utf-8')).hexdigest().upper()
    return f'{digest[:4]}-{digest[4:8]}'


@dataclass
class InviteCode:
    """Parsed invite code with peer identity and endpoints."""
    peer_id: str
    ed25519_public_key_b58: str
    x25519_public_key_b58: str
    endpoints: List[str]
    mesh_name: Optional[str] = None
    meshspace_id: Optional[str] = None
    meshspace_id_aliases: Optional[List[str]] = None
    meshspace_fingerprint: Optional[str] = None
    meshspace_avatar_b64: Optional[str] = None
    meshspace_avatar_mime: Optional[str] = None
    peer_label: Optional[str] = None
    instance_label: Optional[str] = None
    peer_icon: Optional[str] = None
    generated_at: Optional[str] = None
    version: int = 1

    def to_dict(self) -> Dict[str, Any]:
        payload = {
            'v': self.version,
            'pid': self.peer_id,
            'epk': self.ed25519_public_key_b58,
            'xpk': self.x25519_public_key_b58,
            'ep': self.endpoints,
        }
        if self.mesh_name:
            payload['mn'] = self.mesh_name
        if self.meshspace_id:
            payload['mid'] = self.meshspace_id
        if self.meshspace_id_aliases:
            payload['mia'] = [str(value or '').strip() for value in self.meshspace_id_aliases if str(value or '').strip()]
        if self.meshspace_fingerprint:
            payload['mf'] = self.meshspace_fingerprint
        if self.meshspace_avatar_b64:
            payload['mab'] = self.meshspace_avatar_b64
        if self.meshspace_avatar_mime:
            payload['mam'] = self.meshspace_avatar_mime
        if self.peer_label:
            payload['pl'] = self.peer_label
        if self.instance_label:
            payload['il'] = self.instance_label
        if self.peer_icon:
            payload['pi'] = self.peer_icon
        if self.generated_at:
            payload['ts'] = self.generated_at
        return payload

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

        # Accept labeled invite blocks copied from the UI, as long as they
        # contain one canopy:<payload> token.
        if code and not code.lstrip().startswith('{') and not code.startswith('canopy:'):
            match = re.search(r'canopy:[A-Za-z0-9_\-]+', code)
            if match:
                code = match.group(0)

        # Strip prefix
        if code.startswith('canopy:'):
            code = code[len('canopy:'):]

        # Try base64url decode
        original_code = code
        try:
            # Restore padding
            padding = 4 - len(code) % 4
            candidate = code
            if padding != 4:
                candidate += '=' * padding
            raw = base64.urlsafe_b64decode(candidate).decode('utf-8')
        except Exception:
            raw = original_code  # maybe it's raw JSON

        data = json.loads(raw)
        return cls(
            version=data.get('v', 1),
            peer_id=data['pid'],
            ed25519_public_key_b58=data['epk'],
            x25519_public_key_b58=data['xpk'],
            endpoints=data.get('ep', []),
            mesh_name=data.get('mn') or None,
            meshspace_id=data.get('mid') or None,
            meshspace_id_aliases=data.get('mia') or None,
            meshspace_fingerprint=data.get('mf') or None,
            meshspace_avatar_b64=data.get('mab') or None,
            meshspace_avatar_mime=data.get('mam') or None,
            peer_label=data.get('pl') or None,
            instance_label=data.get('il') or None,
            peer_icon=data.get('pi') or None,
            generated_at=data.get('ts') or None,
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
                    public_port: Optional[int] = None,
                    external_endpoint: Optional[str] = None,
                    allow_plain_fallback: bool = False,
                    endpoint_scheme: str = 'ws',
                    mesh_name: Optional[str] = None,
                    meshspace_id: Optional[str] = None,
                    meshspace_id_aliases: Optional[List[str]] = None,
                    meshspace_fingerprint_value: Optional[str] = None,
                    meshspace_avatar_b64: Optional[str] = None,
                    meshspace_avatar_mime: Optional[str] = None,
                    peer_label: Optional[str] = None,
                    instance_label: Optional[str] = None,
                    peer_icon: Optional[str] = None,
                    generated_at: Optional[str] = None) -> InviteCode:
    """
    Generate an invite code from the local peer identity.

    Args:
        identity_manager: IdentityManager with initialised local identity
        mesh_port: The local P2P mesh port (e.g. 7771)
        public_host: Optional public/external IP or hostname
        public_port: Optional public port (if port-forwarded)
        external_endpoint: Optional full ws:// or wss:// endpoint
        allow_plain_fallback: If true, explicit WSS invites may also advertise same-host plain WS candidates
        endpoint_scheme: Scheme for Canopy-owned listener endpoints (ws or wss)
        mesh_name: Optional human-facing mesh label for preview UX
        meshspace_id: Optional stable meshspace identifier for preview UX
        meshspace_id_aliases: Optional legacy/current alias IDs for transition-aware preview UX
        meshspace_fingerprint_value: Optional precomputed short mesh check code
        meshspace_avatar_b64: Optional compact mesh avatar preview art
        meshspace_avatar_mime: Optional mime for mesh avatar preview art
        peer_label: Optional human-facing peer label for preview UX
        instance_label: Optional device/instance label for preview UX
        peer_icon: Optional standard icon hint for the node/device
        generated_at: Optional ISO timestamp for invite age hints

    Returns:
        InviteCode ready to .encode()
    """
    local = identity_manager.local_identity
    if not local:
        raise ValueError("Local identity not initialized")

    epk = base58.b58encode(local.ed25519_public_key).decode()
    xpk = base58.b58encode(local.x25519_public_key).decode()

    endpoints: List[str] = []
    explicit_wss_endpoint = ''
    listener_scheme = str(endpoint_scheme or 'ws').strip().lower()
    if listener_scheme not in {'ws', 'wss'}:
        listener_scheme = 'ws'

    # Explicit external endpoint first (e.g. ngrok or another tunnel)
    if external_endpoint:
        canon = canonicalize_invite_endpoint(external_endpoint)
        if not canon:
            raise ValueError("Invalid external mesh endpoint")
        endpoints.append(canon)
        parsed_external = parse_invite_endpoint(canon)
        if parsed_external and parsed_external[2] == 'wss':
            explicit_wss_endpoint = canon

    # Public / port-forwarded endpoint next
    if public_host:
        port = public_port or mesh_port
        public_endpoint = f"{listener_scheme}://{_format_endpoint_host(public_host)}:{port}"
        if not (explicit_wss_endpoint and not allow_plain_fallback and _is_same_host(public_endpoint, explicit_wss_endpoint)):
            endpoints.append(public_endpoint)

    # LAN endpoints
    for ip in get_local_ips():
        ep = f"{listener_scheme}://{ip}:{mesh_port}"
        if explicit_wss_endpoint and not allow_plain_fallback and _is_same_host(ep, explicit_wss_endpoint):
            continue
        if ep not in endpoints:
            endpoints.append(ep)

    invite = InviteCode(
        peer_id=local.peer_id,
        ed25519_public_key_b58=epk,
        x25519_public_key_b58=xpk,
        endpoints=endpoints,
        mesh_name=str(mesh_name or '').strip() or None,
        meshspace_id=str(meshspace_id or '').strip() or None,
        meshspace_id_aliases=[
            str(value or '').strip()
            for value in (meshspace_id_aliases or [])
            if str(value or '').strip()
        ] or None,
        meshspace_fingerprint=(
            str(meshspace_fingerprint_value or '').strip()
            or meshspace_fingerprint(meshspace_id, mesh_name)
            or None
        ),
        meshspace_avatar_b64=str(meshspace_avatar_b64 or '').strip() or None,
        meshspace_avatar_mime=str(meshspace_avatar_mime or '').strip() or None,
        peer_label=str(peer_label or '').strip() or None,
        instance_label=str(instance_label or '').strip() or None,
        peer_icon=str(peer_icon or '').strip() or None,
        generated_at=str(generated_at or '').strip() or None,
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

    # Register as known peer and persist invite endpoints so reconnect
    # can recover even if the initial direct session drops later.
    endpoints = _sanitize_invite_endpoints(invite.endpoints or [])
    meshspace_hint = {
        'meshspace_id': invite.meshspace_id,
        'meshspace_id_aliases': invite.meshspace_id_aliases or [],
        'meshspace_name': invite.mesh_name,
        'meshspace_fingerprint': invite.meshspace_fingerprint,
        'meshspace_avatar_b64': invite.meshspace_avatar_b64,
        'meshspace_avatar_mime': invite.meshspace_avatar_mime,
        'source': 'invite',
    }

    identity_manager.create_remote_peer(
        invite.peer_id,
        ed25519_pub,
        x25519_pub,
        endpoints=endpoints,
        meshspace_hint=meshspace_hint,
    )

    logger.info(f"Imported invite for peer {invite.peer_id} with {len(endpoints)} endpoint(s)")
    return {
        'peer_id': invite.peer_id,
        'endpoints': endpoints,
        'status': 'imported',
        'meshspace_hint': {
            key: value for key, value in meshspace_hint.items() if value
        },
    }
