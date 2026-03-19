"""
Data-at-rest encryption for Canopy.

Provides transparent encryption/decryption of content stored in the local
SQLite database using the instance's cryptographic identity. This ensures
that even if someone copies the database file, they cannot read the contents
without the instance's private key.

Uses ChaCha20-Poly1305 (AEAD) with keys derived from the local peer identity.

Author: Konrad Walus (architecture, design, and direction)
Project: Canopy - Local Mesh Communication
License: Apache 2.0
Development: AI-assisted implementation (Claude, Codex, GitHub Copilot, Cursor IDE, Ollama)
"""

import hashlib
import json
import logging
import secrets
import time
from pathlib import Path
from typing import Optional, cast

from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives import hashes

logger = logging.getLogger('canopy.security.encryption')

# Prefix to identify encrypted content in the database
ENCRYPTED_PREFIX = "ENC:1:"


class DataEncryptor:
    """
    Encrypts and decrypts data at rest using the local instance key.
    
    The encryption key is derived from the local peer's Ed25519 private key
    using HKDF, so only this instance can decrypt its own stored data.
    """
    
    def __init__(self, identity_path: Optional[Path] = None):
        """
        Initialize the data encryptor.
        
        Args:
            identity_path: Path to the peer identity file. If None, encryption
                          is disabled (passthrough mode).
        """
        self._cipher_key: Optional[bytes] = None
        self._enabled = False
        self.identity_path = identity_path
        self._decrypt_error_seen: dict[str, float] = {}
        self._decrypt_error_cooldown_s = 60.0
        
        if identity_path and identity_path.exists():
            self._derive_encryption_key(identity_path)
    
    def _derive_encryption_key(self, identity_path: Path) -> None:
        """Derive a 256-bit encryption key from the local peer identity."""
        try:
            with open(identity_path, 'r') as f:
                identity_data = json.load(f)
            
            # Use the Ed25519 private key as the source material
            ed25519_priv = identity_data.get('ed25519_private_key')
            if not ed25519_priv:
                logger.warning("No private key found in identity file - encryption disabled")
                return
            
            # Derive an encryption key using HKDF
            import base58
            private_key_bytes = base58.b58decode(ed25519_priv)
            
            derived_key = HKDF(
                algorithm=hashes.SHA256(),
                length=32,
                salt=b'canopy-data-at-rest-v1',
                info=b'canopy-local-storage-encryption',
            ).derive(private_key_bytes)
            
            self._cipher_key = derived_key
            self._enabled = True
            logger.info("Data-at-rest encryption initialized successfully")
            
        except Exception as e:
            logger.error(f"Failed to initialize data encryption: {e}")
            self._enabled = False
    
    @property
    def is_enabled(self) -> bool:
        """Check if encryption is currently enabled."""
        return self._enabled
    
    _LARGE_PAYLOAD_WARN_BYTES = 1 * 1024 * 1024  # 1 MiB

    def encrypt(self, plaintext: Optional[str]) -> Optional[str]:
        """
        Encrypt a string for storage.
        
        Args:
            plaintext: The string to encrypt. Binary data must be
                       base64-encoded by the caller before passing here.
            
        Returns:
            Encrypted string with prefix, or original string if encryption
            disabled. Returns None when plaintext is None.
        """
        if plaintext is None:
            return None
        if not self._enabled or not plaintext:
            return plaintext
        
        try:
            plaintext_bytes = plaintext.encode('utf-8')
            if len(plaintext_bytes) > self._LARGE_PAYLOAD_WARN_BYTES:
                logger.warning(
                    f"Encrypting large payload ({len(plaintext_bytes)} bytes); "
                    "consider chunking or compressing before encryption"
                )
            cipher = ChaCha20Poly1305(cast(bytes, self._cipher_key))
            nonce = secrets.token_bytes(12)
            ciphertext = cipher.encrypt(nonce, plaintext_bytes, None)
            
            # Format: ENC:1:<nonce_hex>:<ciphertext_hex>
            return f"{ENCRYPTED_PREFIX}{nonce.hex()}:{ciphertext.hex()}"
            
        except Exception as e:
            logger.error(f"Encryption failed: {e}")
            return plaintext  # Fail open - store unencrypted rather than lose data
    
    def decrypt(self, stored_value: Optional[str]) -> Optional[str]:
        """
        Decrypt a stored string.
        
        Args:
            stored_value: The stored string (possibly encrypted)
            
        Returns:
            Decrypted string, or original string if not encrypted.
            Returns None when stored_value is None.
        """
        if stored_value is None:
            return None
        if not stored_value or not stored_value.startswith(ENCRYPTED_PREFIX):
            return stored_value  # Not encrypted, return as-is
        
        if not self._enabled:
            logger.warning("Encountered encrypted data but encryption is not initialized")
            return "[Encrypted - key not available]"
        
        try:
            # Parse: ENC:1:<nonce_hex>:<ciphertext_hex>
            parts = stored_value[len(ENCRYPTED_PREFIX):].split(':', 1)
            if len(parts) != 2:
                logger.error("Malformed encrypted data")
                return stored_value
            
            nonce = bytes.fromhex(parts[0])
            ciphertext = bytes.fromhex(parts[1])
            
            cipher = ChaCha20Poly1305(cast(bytes, self._cipher_key))
            plaintext_bytes = cipher.decrypt(nonce, ciphertext, None)
            
            return plaintext_bytes.decode('utf-8')
            
        except Exception as e:
            # Avoid flooding logs on repeated decrypt failures for the same value,
            # which can happen when a DB contains data encrypted by a different
            # instance key (e.g., imported/migrated database).
            sample = stored_value[:256] if isinstance(stored_value, str) else str(stored_value)
            fingerprint = hashlib.sha256(sample.encode('utf-8', errors='ignore')).hexdigest()[:12]
            now = time.time()
            last = self._decrypt_error_seen.get(fingerprint, 0.0)
            if now - last >= self._decrypt_error_cooldown_s:
                self._decrypt_error_seen[fingerprint] = now
                logger.warning(f"Decryption failed (fingerprint={fingerprint}): {e}")
            else:
                logger.debug(f"Decryption failed (suppressed repeat, fingerprint={fingerprint}): {e}")
            return "[Decryption failed]"
    
    def is_encrypted(self, value: Optional[str]) -> bool:
        """Check if a value is encrypted."""
        return value is not None and value.startswith(ENCRYPTED_PREFIX)


# Prefix for per-recipient encrypted content
RECIPIENT_ENCRYPTED_PREFIX = "RENC:1:"


class RecipientEncryptor:
    """
    Encrypts content with a per-post random key, then wraps that key
    for each authorized recipient using their X25519 public key.
    
    This enables true cryptographic access control: revoking a recipient
    means removing their wrapped key, after which they cannot decrypt
    even if they have the ciphertext.
    
    Storage format in DB:
        Content field: RENC:1:<nonce_hex>:<ciphertext_hex>
        A separate table stores wrapped content keys per recipient.
    """
    
    @staticmethod
    def encrypt_for_recipients(plaintext: str, recipient_public_keys: dict) -> dict:
        """
        Encrypt content for specific recipients.
        
        Args:
            plaintext: The content to encrypt
            recipient_public_keys: Dict of {user_id: x25519_public_key_bytes}
            
        Returns:
            Dict with:
                'encrypted_content': str - the encrypted content
                'wrapped_keys': dict - {user_id: wrapped_key_hex}
        """
        if not plaintext or not recipient_public_keys:
            return {'encrypted_content': plaintext, 'wrapped_keys': {}}
        
        try:
            # Generate a random content encryption key (CEK)
            cek = secrets.token_bytes(32)
            
            # Encrypt the content with the CEK
            cipher = ChaCha20Poly1305(cek)
            nonce = secrets.token_bytes(12)
            ciphertext = cipher.encrypt(nonce, plaintext.encode('utf-8'), None)
            
            encrypted_content = f"{RECIPIENT_ENCRYPTED_PREFIX}{nonce.hex()}:{ciphertext.hex()}"
            
            # Wrap the CEK for each recipient using their X25519 public key
            from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
            
            # Generate an ephemeral X25519 keypair for wrapping
            ephemeral_private = X25519PrivateKey.generate()
            ephemeral_public = ephemeral_private.public_key()
            from cryptography.hazmat.primitives import serialization
            ephemeral_public_bytes = ephemeral_public.public_bytes(
                encoding=serialization.Encoding.Raw,
                format=serialization.PublicFormat.Raw
            )
            
            wrapped_keys = {}
            for user_id, recipient_pub_bytes in recipient_public_keys.items():
                try:
                    recipient_pub = X25519PublicKey.from_public_bytes(recipient_pub_bytes)
                    shared_secret = ephemeral_private.exchange(recipient_pub)
                    
                    # Derive wrapping key from shared secret
                    wrapping_key = HKDF(
                        algorithm=hashes.SHA256(),
                        length=32,
                        salt=None,
                        info=b'canopy-post-key-wrap',
                    ).derive(shared_secret)
                    
                    # Encrypt the CEK with the wrapping key
                    wrap_cipher = ChaCha20Poly1305(wrapping_key)
                    wrap_nonce = secrets.token_bytes(12)
                    wrapped_cek = wrap_cipher.encrypt(wrap_nonce, cek, None)
                    
                    # Store: ephemeral_public + wrap_nonce + wrapped_cek
                    wrapped_keys[user_id] = (
                        ephemeral_public_bytes + wrap_nonce + wrapped_cek
                    ).hex()
                    
                except Exception as e:
                    logger.error(f"Failed to wrap key for {user_id}: {e}")
            
            return {
                'encrypted_content': encrypted_content,
                'wrapped_keys': wrapped_keys
            }
            
        except Exception as e:
            logger.error(f"Recipient encryption failed: {e}")
            return {'encrypted_content': plaintext, 'wrapped_keys': {}}
    
    @staticmethod
    def decrypt_for_recipient(encrypted_content: str, wrapped_key_hex: str, 
                              recipient_private_key_bytes: bytes) -> str:
        """
        Decrypt content using a recipient's wrapped key.
        
        Args:
            encrypted_content: The RENC: prefixed encrypted content
            wrapped_key_hex: The hex-encoded wrapped key for this recipient
            recipient_private_key_bytes: The recipient's X25519 private key
            
        Returns:
            Decrypted plaintext, or error message
        """
        if not encrypted_content or not encrypted_content.startswith(RECIPIENT_ENCRYPTED_PREFIX):
            return encrypted_content
        
        try:
            from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
            
            # Parse the wrapped key: ephemeral_public(32) + wrap_nonce(12) + wrapped_cek(32+16)
            wrapped_data = bytes.fromhex(wrapped_key_hex)
            ephemeral_public_bytes = wrapped_data[:32]
            wrap_nonce = wrapped_data[32:44]
            wrapped_cek = wrapped_data[44:]
            
            # Derive the wrapping key
            recipient_private = X25519PrivateKey.from_private_bytes(recipient_private_key_bytes)
            ephemeral_public = X25519PublicKey.from_public_bytes(ephemeral_public_bytes)
            shared_secret = recipient_private.exchange(ephemeral_public)
            
            wrapping_key = HKDF(
                algorithm=hashes.SHA256(),
                length=32,
                salt=None,
                info=b'canopy-post-key-wrap',
            ).derive(shared_secret)
            
            # Unwrap the CEK
            wrap_cipher = ChaCha20Poly1305(wrapping_key)
            cek = wrap_cipher.decrypt(wrap_nonce, wrapped_cek, None)
            
            # Decrypt the content
            content_parts = encrypted_content[len(RECIPIENT_ENCRYPTED_PREFIX):].split(':', 1)
            nonce = bytes.fromhex(content_parts[0])
            ciphertext = bytes.fromhex(content_parts[1])
            
            cipher = ChaCha20Poly1305(cek)
            plaintext = cipher.decrypt(nonce, ciphertext, None)
            
            return plaintext.decode('utf-8')
            
        except Exception as e:
            logger.error(f"Recipient decryption failed: {e}")
            return "[Access denied - cannot decrypt]"
