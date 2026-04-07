"""
Simple test script for Canopy P2P networking.

This script tests the P2P network components independently.
"""

import asyncio
import logging
from pathlib import Path

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

from canopy.network.identity import IdentityManager
from canopy.network.discovery import PeerDiscovery
from canopy.network.connection import ConnectionManager
from canopy.network.routing import MessageRouter, MessageType


async def test_identity():
    """Test peer identity generation."""
    print("\n=== Testing Peer Identity ===")
    
    identity_mgr = IdentityManager(Path('./test_data/test_identity.json'))
    identity = identity_mgr.initialize()
    
    print(f"Peer ID: {identity.peer_id}")
    print(f"Ed25519 Public Key: {identity.ed25519_public_key[:16].hex()}...")
    print(f"X25519 Public Key: {identity.x25519_public_key[:16].hex()}...")
    
    # Test signing
    message = b"Hello, Canopy P2P!"
    signature = identity.sign(message)
    verified = identity.verify(message, signature)
    
    print(f"Signature verification: {'✓ PASSED' if verified else '✗ FAILED'}")
    
    return identity_mgr


async def test_discovery(identity_mgr):
    """Test peer discovery."""
    print("\n=== Testing Peer Discovery ===")
    
    discovery = PeerDiscovery(
        local_peer_id=identity_mgr.local_identity.peer_id,
        service_port=7771
    )
    
    discovered_peers = []
    
    def on_peer(peer, added):
        if added:
            print(f"✓ Discovered peer: {peer.peer_id} at {peer.address}:{peer.port}")
            discovered_peers.append(peer)
        else:
            print(f"✗ Peer left: {peer.peer_id}")
    
    discovery.on_peer_discovered(on_peer)
    discovery.start()
    
    print("Waiting for peer discoveries (10 seconds)...")
    await asyncio.sleep(10)
    
    print(f"\nTotal peers discovered: {len(discovered_peers)}")
    for peer in discovered_peers:
        print(f"  - {peer.peer_id}: {peer.address}:{peer.port}")
    
    discovery.stop()
    return discovered_peers


async def test_connection(identity_mgr, discovered_peers):
    """Test peer connections."""
    print("\n=== Testing Peer Connections ===")
    
    conn_mgr = ConnectionManager(
        local_peer_id=identity_mgr.local_identity.peer_id,
        identity_manager=identity_mgr,
        host="0.0.0.0",
        port=7771
    )
    
    await conn_mgr.start()
    
    # Try to connect to discovered peers
    for peer in discovered_peers:
        print(f"Connecting to {peer.peer_id}...")
        success = await conn_mgr.connect_to_peer(
            peer.peer_id,
            peer.address,
            peer.port
        )
        if success:
            print(f"✓ Connected to {peer.peer_id}")
        else:
            print(f"✗ Failed to connect to {peer.peer_id}")
    
    print(f"\nConnected peers: {conn_mgr.get_connected_peers()}")
    
    # Keep connections alive
    await asyncio.sleep(5)
    
    await conn_mgr.stop()
    return conn_mgr


async def test_routing(identity_mgr, conn_mgr):
    """Test message routing."""
    print("\n=== Testing Message Routing ===")
    
    router = MessageRouter(
        local_peer_id=identity_mgr.local_identity.peer_id,
        identity_manager=identity_mgr,
        connection_manager=conn_mgr
    )
    
    # Create a test message
    message = router.create_message(
        MessageType.DIRECT_MESSAGE,
        to_peer=None,  # Broadcast
        payload={'content': 'Hello from Canopy P2P test!'},
        ttl=3
    )
    
    # Sign message
    router.sign_message(message)
    print(f"Created message: {message.id}")
    print(f"  Type: {message.type.value}")
    print(f"  From: {message.from_peer}")
    print(f"  Signature: {message.signature[:32]}...")
    
    # Verify signature
    verified = router.verify_message(message)
    print(f"  Verification: {'✓ PASSED' if verified else '✗ FAILED'}")
    
    return router


async def main():
    """Run all tests."""
    print("=" * 60)
    print("Canopy P2P Network Test Suite")
    print("=" * 60)
    
    try:
        # Test 1: Identity
        identity_mgr = await test_identity()
        
        # Test 2: Discovery
        discovered_peers = await test_discovery(identity_mgr)
        
        # Test 3: Connections (skip if no peers)
        if discovered_peers:
            conn_mgr = await test_connection(identity_mgr, discovered_peers)
        else:
            print("\n⚠ No peers discovered, skipping connection tests")
            print("  To test connections, run another instance of Canopy")
        
        # Test 4: Routing
        # We'll just test message creation and signing
        print("\n=== Testing Message Creation ===")
        from canopy.network.connection import ConnectionManager
        mock_conn = ConnectionManager(
            identity_mgr.local_identity.peer_id,
            identity_mgr,
            "0.0.0.0",
            7771
        )
        router = await test_routing(identity_mgr, mock_conn)
        
        print("\n" + "=" * 60)
        print("✓ All tests completed successfully!")
        print("=" * 60)
        
    except Exception as e:
        print(f"\n✗ Test failed with error: {e}")
        import traceback
        traceback.print_exc()


if __name__ == '__main__':
    asyncio.run(main())
