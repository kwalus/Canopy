"""
Secure password hashing and validation for Canopy.

Uses bcrypt for password hashing with proper salting and work factor.

Author: Konrad Walus (architecture, design, and direction)
Project: Canopy - Local Mesh Communication
License: Apache 2.0
Development: AI-assisted implementation (Claude, Codex, GitHub Copilot, Cursor IDE, Ollama)
"""

import re
import bcrypt
import logging
from typing import Optional, Tuple

logger = logging.getLogger(__name__)


# Password validation constants
SPECIAL_CHARS_PATTERN = r'[!@#$%^&*()_+\-=\[\]{};:\'",.<>?/\\|`~]'

WEAK_PASSWORDS = frozenset([
    'password', 'password1', 'password123', '12345678', 'qwerty', 
    'abc123', 'monkey', '1234567890', 'letmein', 'trustno1',
    'dragon', 'baseball', 'iloveyou', 'master', 'sunshine'
])


def hash_password(password: str) -> str:
    """
    Hash a password using bcrypt with secure salt.
    
    Args:
        password: Plain text password to hash
        
    Returns:
        Bcrypt hash string (includes salt and work factor)
    """
    # Bcrypt has built-in salt generation and proper work factor (default 12 rounds)
    # This is significantly more secure than SHA256 with a global salt
    password_bytes = password.encode('utf-8')
    salt = bcrypt.gensalt(rounds=12)  # 12 rounds = 2^12 = 4096 iterations (good balance)
    hashed = bcrypt.hashpw(password_bytes, salt)
    return hashed.decode('utf-8')


def verify_password(password: str, password_hash: str) -> bool:
    """
    Verify a password against its bcrypt hash.
    
    Args:
        password: Plain text password to verify
        password_hash: Bcrypt hash to check against
        
    Returns:
        True if password matches, False otherwise
    """
    try:
        password_bytes = password.encode('utf-8')
        hash_bytes = password_hash.encode('utf-8')
        return bcrypt.checkpw(password_bytes, hash_bytes)
    except Exception as e:
        logger.error(f"Password verification failed: {e}")
        return False


def validate_password_strength(password: str) -> Tuple[bool, Optional[str]]:
    """
    Validate password meets minimum security requirements.
    
    Requirements:
    - At least 8 characters long
    - Contains at least one uppercase letter
    - Contains at least one lowercase letter
    - Contains at least one digit
    - Contains at least one special character
    
    Args:
        password: Password to validate
        
    Returns:
        (is_valid, error_message) tuple
    """
    if len(password) < 8:
        return False, "Password must be at least 8 characters long"
    
    if not re.search(r'[A-Z]', password):
        return False, "Password must contain at least one uppercase letter"
    
    if not re.search(r'[a-z]', password):
        return False, "Password must contain at least one lowercase letter"
    
    if not re.search(r'\d', password):
        return False, "Password must contain at least one digit"
    
    if not re.search(SPECIAL_CHARS_PATTERN, password):
        return False, "Password must contain at least one special character"
    
    # Check for common weak passwords
    if password.lower() in WEAK_PASSWORDS:
        return False, "Password is too common. Please choose a more unique password."
    
    return True, None


def is_legacy_hash(password_hash: str) -> bool:
    """
    Check if a password hash is using the legacy SHA256 format.
    
    Args:
        password_hash: Hash to check
        
    Returns:
        True if legacy SHA256 format, False if bcrypt
    """
    # Bcrypt hashes start with $2a$, $2b$, or $2y$
    # Legacy SHA256 hashes are 64 hex characters
    return not password_hash.startswith('$2') and len(password_hash) == 64 and all(c in '0123456789abcdef' for c in password_hash)


def verify_legacy_password(password: str, password_hash: str, secret_key: str) -> bool:
    """
    Verify password against legacy SHA256 hash format.
    
    This is for backward compatibility with existing user accounts.
    After verification, the password should be re-hashed with bcrypt.
    
    Args:
        password: Plain text password
        password_hash: Legacy SHA256 hash
        secret_key: Application secret key
        
    Returns:
        True if password matches legacy hash
    """
    import hashlib
    legacy_hash = hashlib.sha256(f"{secret_key}:{password}".encode('utf-8')).hexdigest()
    return legacy_hash == password_hash
