"""
P2P Networking module for Canopy.

Provides peer-to-peer communication infrastructure including:
- Peer identity and cryptographic keys
- Peer discovery (mDNS and DHT)
- Connection management
- Message routing
- End-to-end encryption

Author: Konrad Walus (architecture, design, and direction)
Project: Canopy - Local Mesh Communication
License: Apache 2.0
Development: AI-assisted implementation (Claude, Codex, GitHub Copilot, Cursor IDE, Ollama)
"""

from .identity import PeerIdentity, IdentityManager
from .discovery import PeerDiscovery
from .connection import ConnectionManager, PeerConnection
from .routing import MessageRouter, P2PMessage
from .manager import P2PNetworkManager

__all__ = [
    'PeerIdentity',
    'IdentityManager',
    'PeerDiscovery',
    'ConnectionManager',
    'PeerConnection',
    'MessageRouter',
    'P2PMessage',
    'P2PNetworkManager',
]
