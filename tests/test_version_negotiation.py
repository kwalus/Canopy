"""Tests for handshake version/protocol negotiation behavior."""

import json
import os
import sys
import types
import unittest
from unittest.mock import patch

import base58

# Ensure repository root is importable when running tests directly.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

# Provide a lightweight zeroconf stub for environments without optional deps.
if 'zeroconf' not in sys.modules:
    zeroconf_stub = types.ModuleType('zeroconf')

    class _Dummy:
        def __init__(self, *args, **kwargs):
            pass

    zeroconf_stub.ServiceBrowser = _Dummy
    zeroconf_stub.ServiceInfo = _Dummy
    zeroconf_stub.Zeroconf = _Dummy
    zeroconf_stub.ServiceStateChange = _Dummy
    sys.modules['zeroconf'] = zeroconf_stub

from canopy.network.connection import ConnectionManager, ConnectionState, PeerConnection


class _FakeLocalIdentity:
    def sign(self, _payload: bytes) -> bytes:
        return bytes.fromhex('aa' * 64)


class _FakeIdentityManager:
    def __init__(self) -> None:
        self.local_identity = _FakeLocalIdentity()

    def export_public_identity(self):
        return {
            'ed25519_public_key': base58.b58encode(b'e' * 32).decode(),
            'x25519_public_key': base58.b58encode(b'x' * 32).decode(),
        }

    def verify_peer_id(self, _peer_id, _pubkey):
        return True

    def add_known_peer(self, _peer):
        return None


class _FakePeerIdentity:
    def __init__(self, peer_id, ed25519_public_key, x25519_public_key):
        self.peer_id = peer_id
        self.ed25519_public_key = ed25519_public_key
        self.x25519_public_key = x25519_public_key

    def verify(self, _payload_bytes, _signature_bytes):
        return True


class _FakeWebSocket:
    def __init__(self, response_payload):
        self.sent = []
        self._response = json.dumps(response_payload)

    async def send(self, message: str):
        self.sent.append(json.loads(message))

    async def recv(self):
        return self._response


def _handshake_ack(peer_id='peer-remote', canopy_version='0.4.30', protocol_version=1):
    return {
        'type': 'handshake_ack',
        'peer_id': peer_id,
        'ed25519_public_key': base58.b58encode(b'r' * 32).decode(),
        'x25519_public_key': base58.b58encode(b's' * 32).decode(),
        'version': '0.1.0',
        'canopy_version': canopy_version,
        'protocol_version': protocol_version,
        'capabilities': ['chat', 'files'],
        'timestamp': 1700000000.0,
        'signature': 'bb' * 64,
    }


class TestVersionNegotiation(unittest.IsolatedAsyncioTestCase):
    async def test_matching_protocol_versions_succeed(self):
        identity_manager = _FakeIdentityManager()
        manager = ConnectionManager(
            local_peer_id='peer-local',
            identity_manager=identity_manager,
            canopy_version='0.4.30',
            protocol_version=1,
            reject_protocol_mismatch=True,
        )
        websocket = _FakeWebSocket(_handshake_ack(protocol_version=1))
        connection = PeerConnection(
            peer_id='peer-remote',
            address='127.0.0.1',
            port=7771,
            state=ConnectionState.HANDSHAKING,
            websocket=websocket,
        )

        with patch('canopy.network.identity.PeerIdentity', _FakePeerIdentity):
            ok = await manager._perform_handshake(connection)

        self.assertTrue(ok)
        self.assertEqual(connection.protocol_version, 1)
        self.assertEqual(connection.canopy_version, '0.4.30')
        self.assertEqual(websocket.sent[0].get('protocol_version'), 1)
        self.assertEqual(websocket.sent[0].get('canopy_version'), '0.4.30')

    async def test_canopy_version_diff_with_same_protocol_is_allowed(self):
        identity_manager = _FakeIdentityManager()
        manager = ConnectionManager(
            local_peer_id='peer-local',
            identity_manager=identity_manager,
            canopy_version='0.4.30',
            protocol_version=1,
            reject_protocol_mismatch=True,
        )
        websocket = _FakeWebSocket(_handshake_ack(canopy_version='0.4.3', protocol_version=1))
        connection = PeerConnection(
            peer_id='peer-remote',
            address='127.0.0.1',
            port=7771,
            state=ConnectionState.HANDSHAKING,
            websocket=websocket,
        )

        with patch('canopy.network.identity.PeerIdentity', _FakePeerIdentity):
            ok = await manager._perform_handshake(connection)

        self.assertTrue(ok)
        self.assertEqual(connection.protocol_version, 1)
        self.assertEqual(connection.canopy_version, '0.4.3')

    async def test_protocol_mismatch_rejected_when_configured(self):
        identity_manager = _FakeIdentityManager()
        manager = ConnectionManager(
            local_peer_id='peer-local',
            identity_manager=identity_manager,
            canopy_version='0.4.30',
            protocol_version=1,
            reject_protocol_mismatch=True,
        )
        websocket = _FakeWebSocket(_handshake_ack(protocol_version=2))
        connection = PeerConnection(
            peer_id='peer-remote',
            address='127.0.0.1',
            port=7771,
            state=ConnectionState.HANDSHAKING,
            websocket=websocket,
        )

        with patch('canopy.network.identity.PeerIdentity', _FakePeerIdentity):
            ok = await manager._perform_handshake(connection)

        self.assertFalse(ok)


if __name__ == '__main__':
    unittest.main()
