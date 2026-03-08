import asyncio
import os
import sys
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

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

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from cryptography.hazmat.primitives import serialization

from canopy.core.messaging import (
    DM_CRYPTO_METADATA_KEY,
    encrypt_dm_transport_bundle,
    unwrap_dm_transport_bundle,
)
from canopy.network.manager import P2PNetworkManager


class _FakeRouter:
    def __init__(self):
        self.calls = []

    async def send_dm_broadcast(self, content, metadata):
        self.calls.append({'content': content, 'metadata': metadata})
        return True


class _FakeFuture:
    def __init__(self, value):
        self._value = value

    def result(self, timeout=None):
        return self._value


class _FakeDb:
    def __init__(self, user_row):
        self._user_row = user_row

    def get_user(self, user_id):
        if self._user_row and user_id == self._user_row.get('id'):
            return dict(self._user_row)
        return None


class _FakeIdentityManager:
    def __init__(self, peer_identity):
        self._peer_identity = peer_identity

    def get_peer(self, peer_id):
        if self._peer_identity and peer_id == self._peer_identity.peer_id:
            return self._peer_identity
        return None


class _PeerIdentity:
    def __init__(self, peer_id, x25519_public_key):
        self.peer_id = peer_id
        self.x25519_public_key = x25519_public_key


class _DiscoveryPeer:
    def __init__(self, peer_id, capabilities):
        self.peer_id = peer_id
        self.service_info = {'capabilities': list(capabilities)}


class _Discovery:
    def __init__(self, peers):
        self._peers = peers

    def get_discovered_peers(self):
        return list(self._peers)


class TestDmE2ETransport(unittest.TestCase):
    def test_encrypt_and_unwrap_dm_bundle_round_trip(self):
        recipient_private = X25519PrivateKey.generate()
        recipient_public = recipient_private.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        recipient_private_bytes = recipient_private.private_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PrivateFormat.Raw,
            encryption_algorithm=serialization.NoEncryption(),
        )

        outbound_content, outbound_meta, security = encrypt_dm_transport_bundle(
            'secret hello',
            {
                'reply_to': 'DM-root',
                'attachments': [{'id': 'file-1', 'name': 'report.txt'}],
                'group_members': ['user-a', 'user-b'],
            },
            'peer-remote',
            recipient_public,
            sender_peer_id='peer-local',
        )

        self.assertEqual(outbound_content, '')
        self.assertEqual(security.get('mode'), 'peer_e2e_v1')
        self.assertIn(DM_CRYPTO_METADATA_KEY, outbound_meta)

        plaintext, restored_meta, restored_security = unwrap_dm_transport_bundle(
            outbound_content,
            outbound_meta,
            'peer-remote',
            recipient_private_bytes,
        )

        self.assertEqual(plaintext, 'secret hello')
        self.assertEqual(restored_meta.get('reply_to'), 'DM-root')
        self.assertEqual(restored_meta.get('attachments')[0]['id'], 'file-1')
        self.assertEqual(restored_meta.get('group_members'), ['user-a', 'user-b'])
        self.assertEqual(restored_security.get('mode'), 'peer_e2e_v1')
        self.assertEqual(restored_security.get('state'), 'encrypted')

    def test_broadcast_direct_message_encrypts_for_capable_remote_peer(self):
        recipient_private = X25519PrivateKey.generate()
        recipient_public = recipient_private.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        recipient_peer = _PeerIdentity('peer-remote', recipient_public)

        manager = P2PNetworkManager.__new__(P2PNetworkManager)
        manager._running = True
        manager._event_loop = object()
        manager.message_router = _FakeRouter()
        manager.db = _FakeDb({
            'id': 'user-remote',
            'username': 'peer-remote-user',
            'origin_peer': 'peer-remote',
        })
        manager.local_identity = type('LocalIdentity', (), {'peer_id': 'peer-local'})()
        manager.identity_manager = _FakeIdentityManager(recipient_peer)
        manager.connection_manager = None
        manager.discovery = None
        manager.peer_versions = {'peer-remote': {'capabilities': ['dm_e2e_v1']}}
        manager._introduced_peers = {'peer-remote': {'capabilities': ['dm_e2e_v1']}}

        def _supports(peer_id, capability):
            return peer_id == 'peer-remote' and capability == 'dm_e2e_v1'

        manager.peer_supports_capability = _supports

        def _run_coroutine(coro, _loop):
            loop = asyncio.new_event_loop()
            try:
                return _FakeFuture(loop.run_until_complete(coro))
            finally:
                loop.close()

        with patch('canopy.network.manager.asyncio.run_coroutine_threadsafe', side_effect=_run_coroutine):
            ok = manager.broadcast_direct_message(
                sender_id='user-local',
                recipient_id='user-remote',
                content='relay-safe secret',
                message_id='DM-e2e-1',
                timestamp='2026-03-08T12:00:00+00:00',
                metadata={'reply_to': 'DM-root'},
            )

        self.assertTrue(ok)
        self.assertEqual(len(manager.message_router.calls), 1)
        call = manager.message_router.calls[0]
        self.assertEqual(call['content'], '')
        self.assertIn(DM_CRYPTO_METADATA_KEY, call['metadata'].get('metadata', {}))
        self.assertEqual(call['metadata']['metadata']['security']['mode'], 'peer_e2e_v1')

    def test_peer_supports_capability_uses_introduced_and_discovery_metadata(self):
        manager = P2PNetworkManager.__new__(P2PNetworkManager)
        manager.connection_manager = None
        manager._introduced_peers = {
            'peer-introduced': {'capabilities': ['dm_e2e_v1']},
        }
        manager.peer_versions = {}
        manager.discovery = _Discovery([
            _DiscoveryPeer('peer-discovered', ['chat', 'dm_e2e_v1']),
        ])

        self.assertTrue(manager.peer_supports_capability('peer-introduced', 'dm_e2e_v1'))
        self.assertTrue(manager.peer_supports_capability('peer-discovered', 'dm_e2e_v1'))
        self.assertFalse(manager.peer_supports_capability('peer-missing', 'dm_e2e_v1'))


if __name__ == '__main__':
    unittest.main()
