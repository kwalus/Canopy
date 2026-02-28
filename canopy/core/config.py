"""
Configuration management for Canopy application.

Data isolation: each physical machine (or VM) gets its own data directory
based on a stable device_id so that databases, keys, and files never
collide — even when the source tree is shared via Dropbox / cloud sync.

Author: Konrad Walus (architecture, design, and direction)
Project: Canopy - Local Mesh Communication
License: Apache 2.0
Development: AI-assisted implementation (Claude, Codex, GitHub Copilot, Cursor IDE, Ollama)
"""

import json
import logging
import os
from pathlib import Path
from typing import Dict, Any, Optional, cast
from dataclasses import dataclass, field

logger = logging.getLogger('canopy.config')


@dataclass
class NetworkConfig:
    """Network-related configuration settings."""
    host: str = "0.0.0.0"  # Bind to all interfaces for LAN access
    port: int = 7770
    mesh_port: int = 7771
    discovery_port: int = 7772
    max_peers: int = 50
    connection_timeout: int = 30
    relay_policy: str = "broker_only"  # off, broker_only, full_relay
    enable_tls: bool = False  # Use wss:// for P2P connections
    tls_cert_path: str = ""  # Path to TLS cert (auto-generated if empty)
    tls_key_path: str = ""   # Path to TLS key (auto-generated if empty)


@dataclass
class SecurityConfig:
    """Security-related configuration settings."""
    encryption_algorithm: str = "ChaCha20Poly1305"
    key_derivation_rounds: int = 100000
    session_timeout: int = 3600  # 1 hour
    max_key_age: int = 86400 * 30  # 30 days
    trust_threshold: int = 50
    require_login: bool = True  # Require PIN/password to access web UI


@dataclass
class StorageConfig:
    """Storage-related configuration settings."""
    database_path: str = ""  # Set at runtime by Config.from_env() / _apply_device_paths()
    data_dir: str = ""       # Per-device data directory (set at runtime)
    backup_interval: int = 3600  # 1 hour
    max_message_size: int = 1024 * 1024  # 1MB
    max_file_size: int = 1024 * 1024 * 100  # 100MB


@dataclass
class UIConfig:
    """User interface configuration settings."""
    theme: str = "light"
    language: str = "en"
    auto_refresh: int = 5  # seconds
    max_feed_items: int = 50


def _load_or_create_secret_key(data_dir: Optional[Path] = None) -> str:
    """Load persistent secret key from data dir, or create one if it doesn't exist.
    
    If data_dir is provided (device-specific), the key lives there.
    Falls back to ./data/secret_key.json for legacy compat.
    """
    if data_dir:
        key_file = data_dir / 'secret_key.json'
    else:
        key_file = Path('./data/secret_key.json')

    try:
        if key_file.exists():
            with open(key_file, 'r') as f:
                data = json.load(f)
                return cast(str, data['secret_key'])
    except Exception:
        pass
    
    # Generate and persist a new key
    secret = os.urandom(32).hex()
    try:
        key_file.parent.mkdir(parents=True, exist_ok=True)
        with open(key_file, 'w') as f:
            json.dump({'secret_key': secret}, f)
        os.chmod(key_file, 0o600)
    except Exception:
        pass  # If we can't write, use ephemeral key (passwords won't survive restart)
    return secret


def _apply_device_paths(config: 'Config') -> None:
    """
    Set storage paths based on the device_id for this machine.

    Layout:  ./data/devices/<device_id>/canopy.db
             ./data/devices/<device_id>/files/
             ./data/devices/<device_id>/secret_key.json
             ./data/devices/<device_id>/peer_identity.json

    If a legacy ./data/canopy.db exists and the device dir is empty,
    migrate it automatically so users keep their data on first upgrade.
    """
    from .device import get_device_data_dir, get_device_id, get_device_label

    # Resolve from module location so path selection is independent of process CWD.
    project_data_root = Path(__file__).resolve().parents[2] / 'data'
    device_dir = get_device_data_dir(project_data_root)
    device_dir.mkdir(parents=True, exist_ok=True)

    db_path = device_dir / 'canopy.db'

    # Auto-migrate legacy data (<data_root>/canopy.db → device dir) on first run.
    legacy_candidates = [
        project_data_root / 'canopy.db',
        Path('./data/canopy.db'),
    ]
    legacy_db = next((p for p in legacy_candidates if p.exists()), None)
    if legacy_db and not db_path.exists():
        import shutil
        logger.info(
            f"Migrating legacy database to device directory: "
            f"{legacy_db} → {db_path}"
        )
        shutil.copy2(legacy_db, db_path)
        # Also migrate secret_key and peer_identity if present
        legacy_root = legacy_db.parent
        for fname in ('secret_key.json', 'peer_identity.json'):
            src = legacy_root / fname
            dst = device_dir / fname
            if src.exists() and not dst.exists():
                shutil.copy2(src, dst)
        # Migrate files directory
        legacy_files = legacy_root / 'files'
        device_files = device_dir / 'files'
        if legacy_files.is_dir() and not device_files.exists():
            shutil.copytree(legacy_files, device_files)
        # Rename legacy db so it won't be re-migrated (keep as backup)
        try:
            legacy_db.rename(legacy_db.with_suffix('.db.pre_device_migration'))
            logger.info("Renamed legacy database to canopy.db.pre_device_migration")
        except Exception as e:
            logger.warning(f"Could not rename legacy database: {e}")

    config.storage.database_path = str(db_path)
    config.storage.data_dir = str(device_dir)

    # Regenerate secret_key from device-specific location
    config.secret_key = _load_or_create_secret_key(device_dir)

    logger.info(
        f"Device: {get_device_id()} ({get_device_label()}) — "
        f"data dir: {device_dir}"
    )


@dataclass
class Config:
    """Main application configuration."""
    debug: bool = False
    testing: bool = False
    secret_key: str = ""  # Set by _apply_device_paths or from_env
    device_id: str = ""   # Set at runtime
    device_label: str = ""
    
    network: NetworkConfig = field(default_factory=NetworkConfig)
    security: SecurityConfig = field(default_factory=SecurityConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    ui: UIConfig = field(default_factory=UIConfig)
    
    @classmethod
    def from_env(cls) -> 'Config':
        """Create configuration from environment variables."""
        config = cls()
        
        # Override with environment variables if present
        config.debug = os.getenv('CANOPY_DEBUG', 'false').lower() == 'true'
        config.testing = os.getenv('CANOPY_TESTING', 'false').lower() == 'true'
            
        # Network configuration
        if host := os.getenv('CANOPY_HOST'):
            config.network.host = host
        if port := os.getenv('CANOPY_PORT'):
            config.network.port = int(port)
        if mesh_port := os.getenv('CANOPY_MESH_PORT'):
            config.network.mesh_port = int(mesh_port)
        if relay := os.getenv('CANOPY_RELAY_POLICY'):
            if relay in ('off', 'broker_only', 'full_relay'):
                config.network.relay_policy = relay

        # Apply device-specific data paths (database, secret key, files)
        # This must happen before CANOPY_DATABASE_PATH override so the user
        # can still force a specific path via env var.
        _apply_device_paths(config)

        # Store device info on config for display
        from .device import get_device_id, get_device_label
        config.device_id = get_device_id()
        config.device_label = get_device_label()

        # Allow explicit env-var override (e.g. for tests or isolated testnet instances)
        if secret_key := os.getenv('CANOPY_SECRET_KEY'):
            config.secret_key = secret_key
        if db_path := os.getenv('CANOPY_DATABASE_PATH'):
            config.storage.database_path = db_path
        # Override the whole data directory (e.g. for a testnet that uses its own files/ folder)
        if data_dir_override := os.getenv('CANOPY_DATA_DIR'):
            data_dir_path = Path(data_dir_override)
            data_dir_path.mkdir(parents=True, exist_ok=True)
            config.storage.data_dir = str(data_dir_path)
            # Re-apply DB path from this directory only if no explicit DB path was given
            if not os.getenv('CANOPY_DATABASE_PATH'):
                config.storage.database_path = str(data_dir_path / 'canopy.db')
            # Re-generate secret key from this directory so sessions are isolated
            if not os.getenv('CANOPY_SECRET_KEY'):
                config.secret_key = _load_or_create_secret_key(data_dir_path)

        return config
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert configuration to dictionary."""
        return {
            'debug': self.debug,
            'testing': self.testing,
            'device': {
                'device_id': self.device_id,
                'label': self.device_label,
            },
            'network': {
                'host': self.network.host,
                'port': self.network.port,
                'mesh_port': self.network.mesh_port,
                'discovery_port': self.network.discovery_port,
                'max_peers': self.network.max_peers,
                'connection_timeout': self.network.connection_timeout,
            },
            'security': {
                'encryption_algorithm': self.security.encryption_algorithm,
                'session_timeout': self.security.session_timeout,
                'trust_threshold': self.security.trust_threshold,
            },
            'storage': {
                'database_path': self.storage.database_path,
                'data_dir': self.storage.data_dir,
                'max_message_size': self.storage.max_message_size,
                'max_file_size': self.storage.max_file_size,
            },
            'ui': {
                'theme': self.ui.theme,
                'language': self.ui.language,
                'auto_refresh': self.ui.auto_refresh,
                'max_feed_items': self.ui.max_feed_items,
            }
        }
