"""Unit tests for Phase-2 channel E2E crypto helper functions."""

import os
import sys
import unittest

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

# Ensure repository root is importable when running tests directly.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from canopy.network.identity import PeerIdentity
from canopy.network.routing import (
    decrypt_key_from_peer,
    decrypt_with_channel_key,
    encode_channel_key_material,
    decode_channel_key_material,
    encrypt_key_for_peer,
    encrypt_with_channel_key,
)


def _make_identity(peer_id: str) -> PeerIdentity:
    priv = X25519PrivateKey.generate()
    pub = priv.public_key()
    priv_bytes = priv.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    pub_bytes = pub.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    # Ed25519 keys are not used by these helpers; any 32-byte values are fine.
    return PeerIdentity(
        peer_id=peer_id,
        ed25519_public_key=b'\x01' * 32,
        x25519_public_key=pub_bytes,
        ed25519_private_key=b'\x02' * 32,
        x25519_private_key=priv_bytes,
    )


class TestChannelE2ECryptoHelpers(unittest.TestCase):
    def test_channel_message_encrypt_decrypt_roundtrip(self) -> None:
        key = os.urandom(32)
        plaintext = "hello secure canopy"
        encrypted_content, nonce = encrypt_with_channel_key(plaintext, key)
        decrypted = decrypt_with_channel_key(encrypted_content, key, nonce)
        self.assertEqual(decrypted, plaintext)

    def test_channel_key_wrap_roundtrip(self) -> None:
        sender = _make_identity('peer-sender')
        recipient = _make_identity('peer-recipient')
        raw_key = os.urandom(32)

        wrapped = encrypt_key_for_peer(
            key_material=raw_key,
            local_identity=sender,
            recipient_identity=recipient,
        )
        unwrapped = decrypt_key_from_peer(
            wrapped_key_hex=wrapped,
            local_identity=recipient,
            sender_identity=sender,
        )
        self.assertEqual(unwrapped, raw_key)

    def test_channel_key_encoding_roundtrip(self) -> None:
        raw_key = os.urandom(32)
        encoded = encode_channel_key_material(raw_key)
        decoded = decode_channel_key_material(encoded)
        self.assertEqual(decoded, raw_key)


if __name__ == '__main__':
    unittest.main()
