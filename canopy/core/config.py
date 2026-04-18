"""
Configuration management for Canopy application.

Data isolation: each physical machine (or VM) gets its own data directory
based on a stable device_id so that databases, keys, and files never
collide — even when the source tree is shared via Dropbox / cloud sync.

Project: Canopy - Local Mesh Communication
License: Apache 2.0
"""

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Dict, Any, Optional, cast
from dataclasses import dataclass, field

from .meshspaces import (
    build_legacy_meshspace_id,
    default_meshspace_root,
    meshspace_cookie_name,
    meshspaces_root_dir,
    sanitize_meshspace_id,
)

logger = logging.getLogger('canopy.config')

TRANSPORT_SECURITY_CONFIG_FILENAME = 'transport_security.json'
TRANSPORT_SECURITY_MODES = {
    'disabled',
    'self_signed',
    'provided',
    'external_terminator',
}


def _config_data_dir(config: 'Config') -> Path:
    raw_data_dir = str(getattr(getattr(config, 'storage', None), 'data_dir', '') or '').strip()
    if raw_data_dir:
        return Path(raw_data_dir).expanduser()
    raw_db_path = str(getattr(getattr(config, 'storage', None), 'database_path', '') or '').strip()
    if raw_db_path:
        return Path(raw_db_path).expanduser().parent
    return Path(__file__).resolve().parents[2] / 'data'


def transport_security_config_path(config: 'Config') -> Path:
    """Return the node-owned persistent transport-security settings path."""
    path = _config_data_dir(config) / TRANSPORT_SECURITY_CONFIG_FILENAME
    try:
        config.network.transport_security_config_path = str(path)
    except Exception:
        pass
    return path


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if not text:
        return default
    return text in {'1', 'true', 'yes', 'on'}


def normalize_transport_security_settings(settings: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Normalize persisted/admin transport settings without validating reachability."""
    raw = dict(settings or {})
    external_endpoint = str(raw.get('external_tls_endpoint') or '').strip()
    cert_path = str(raw.get('tls_cert_path') or '').strip()
    key_path = str(raw.get('tls_key_path') or '').strip()
    mode = str(raw.get('mode') or raw.get('transport_security_mode') or '').strip().lower()
    if mode not in TRANSPORT_SECURITY_MODES:
        if external_endpoint:
            mode = 'external_terminator'
        elif _as_bool(raw.get('enable_tls'), False):
            mode = 'provided' if cert_path and key_path else 'self_signed'
        else:
            mode = 'disabled'

    enable_tls = _as_bool(raw.get('enable_tls'), mode in {'self_signed', 'provided'})
    if mode == 'external_terminator':
        enable_tls = False
    elif mode == 'disabled':
        enable_tls = False

    return {
        'mode': mode,
        'enable_tls': enable_tls,
        'tls_cert_path': cert_path,
        'tls_key_path': key_path,
        'require_verified_wss': _as_bool(raw.get('require_verified_wss'), False),
        'external_tls_endpoint': external_endpoint if mode == 'external_terminator' else '',
        'updated_at': str(raw.get('updated_at') or '').strip(),
    }


def load_transport_security_settings(config: 'Config') -> Dict[str, Any]:
    """Load persisted transport-security settings for this device/meshspace."""
    path = transport_security_config_path(config)
    try:
        if not path.exists():
            return {}
        data = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(data, dict):
            return {}
        return normalize_transport_security_settings(data)
    except Exception as exc:
        logger.warning("Could not load transport security settings from %s: %s", path, exc)
        return {}


def save_transport_security_settings(config: 'Config', settings: Dict[str, Any]) -> Path:
    """Persist transport-security settings into the active node/meshspace data dir."""
    normalized = normalize_transport_security_settings(settings)
    path = transport_security_config_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(normalized, indent=2, sort_keys=True), encoding='utf-8')
    try:
        os.chmod(path, 0o600)
    except Exception:
        pass
    return path


def apply_transport_security_settings(
    config: 'Config',
    settings: Dict[str, Any],
    *,
    env_overrides: Optional[Dict[str, bool]] = None,
) -> Dict[str, Any]:
    """Apply normalized transport settings to a Config object, respecting env overrides."""
    normalized = normalize_transport_security_settings(settings)
    overrides = env_overrides or {}
    network = config.network

    if not overrides.get('transport_security_mode'):
        network.transport_security_mode = normalized['mode']
    if not overrides.get('enable_tls'):
        network.enable_tls = bool(normalized['enable_tls'])
    if not overrides.get('require_verified_wss'):
        network.require_verified_wss = bool(normalized['require_verified_wss'])

    mode = normalized['mode']
    if mode in {'self_signed', 'provided'}:
        if not overrides.get('tls_cert_path') and normalized['tls_cert_path']:
            network.tls_cert_path = normalized['tls_cert_path']
        if not overrides.get('tls_key_path') and normalized['tls_key_path']:
            network.tls_key_path = normalized['tls_key_path']
        if not overrides.get('external_tls_endpoint'):
            network.external_tls_endpoint = ''
    elif mode == 'external_terminator':
        if not overrides.get('external_tls_endpoint'):
            network.external_tls_endpoint = normalized['external_tls_endpoint']
        if not overrides.get('tls_cert_path'):
            network.tls_cert_path = ''
        if not overrides.get('tls_key_path'):
            network.tls_key_path = ''
    elif mode == 'disabled':
        if not overrides.get('external_tls_endpoint'):
            network.external_tls_endpoint = ''

    network.transport_security_config_path = str(transport_security_config_path(config))
    return normalized


def apply_persisted_transport_security_settings(
    config: 'Config',
    *,
    env_overrides: Optional[Dict[str, bool]] = None,
) -> Dict[str, Any]:
    """Apply node-owned persisted transport settings after storage paths are known."""
    settings = load_transport_security_settings(config)
    if settings:
        return apply_transport_security_settings(config, settings, env_overrides=env_overrides)
    config.network.transport_security_config_path = str(transport_security_config_path(config))
    return {}


def _resolve_meshspace_registry_root(config: 'Config', data_root: str) -> str:
    """Choose the registry root for this runtime."""
    override = os.getenv('CANOPY_MESHSPACE_REGISTRY_ROOT', '').strip()
    if override:
        return str(Path(override).expanduser())
    if config.testing:
        base_root = Path(data_root).expanduser() if str(data_root or '').strip() else Path(tempfile.gettempdir()) / 'canopy-testing'
        return str(base_root / '.meshspaces-registry')
    return str(meshspaces_root_dir())


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
    require_verified_wss: bool = False  # Verify outbound wss:// certs/hostnames
    external_tls_endpoint: str = ""  # Public wss:// endpoint when TLS terminates at a proxy/tunnel
    transport_security_mode: str = ""  # disabled, self_signed, provided, external_terminator
    transport_security_config_path: str = ""  # Node-owned persisted settings file


@dataclass
class SecurityConfig:
    """Security-related configuration settings."""
    encryption_algorithm: str = "ChaCha20Poly1305"
    key_derivation_rounds: int = 100000
    session_timeout: int = 3600  # 1 hour
    max_key_age: int = 86400 * 30  # 30 days
    trust_threshold: int = 50
    require_login: bool = True  # Require PIN/password to access web UI
    allow_unverified_relay_messages: bool = False  # Mixed-version compatibility switch
    e2e_private_channels: bool = False  # Phase 1 scaffold; disabled by default
    e2e_private_channels_enforce: bool = False  # Enforce only when all peers support it
    sync_digest_enabled: bool = False  # Optional Merkle-assisted catch-up optimization
    sync_digest_require_capability: bool = True  # Only use when peer advertises support
    sync_digest_max_channels_per_request: int = 200
    identity_portability_enabled: bool = False  # Distributed-auth Phase 1 (metadata + grants)


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


@dataclass
class MeshspaceConfig:
    """Meshspace runtime configuration."""
    enabled: bool = False
    meshspace_id: str = ""
    meshspace_id_aliases: list[str] = field(default_factory=list)
    name: str = ""
    description: str = ""
    data_root: str = ""
    registry_root: str = ""
    runtime_mode: str = "legacy-default"
    is_default: bool = True
    supervised: bool = False
    network_quarantined: bool = False
    session_cookie_name: str = ""
    avatar_path: str = ""
    avatar_mime: str = ""
    avatar_preview_b64: str = ""
    avatar_preview_mime: str = ""


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


def _apply_meshspace_paths(
    config: 'Config',
    *,
    meshspace_id: str,
    meshspace_name: str,
    meshspace_root: Optional[str] = None,
    supervised: bool = False,
    network_quarantined: bool = False,
) -> None:
    """Configure storage and identity paths for an explicit meshspace runtime."""
    normalized_id = sanitize_meshspace_id(meshspace_id, fallback="meshspace")
    root = Path(meshspace_root).expanduser() if meshspace_root else default_meshspace_root(normalized_id)
    root.mkdir(parents=True, exist_ok=True)

    config.storage.data_dir = str(root)
    config.storage.database_path = str(root / 'canopy.db')
    config.secret_key = _load_or_create_secret_key(root)
    config.meshspace.enabled = True
    config.meshspace.meshspace_id = normalized_id
    config.meshspace.name = str(meshspace_name or normalized_id).strip() or normalized_id
    config.meshspace.data_root = str(root)
    config.meshspace.registry_root = _resolve_meshspace_registry_root(config, str(root))
    config.meshspace.runtime_mode = "meshspace"
    config.meshspace.is_default = False
    config.meshspace.supervised = bool(supervised)
    config.meshspace.network_quarantined = bool(network_quarantined)
    config.meshspace.session_cookie_name = meshspace_cookie_name(normalized_id)

    logger.info(
        "Meshspace runtime configured: id=%s name=%s data_dir=%s",
        normalized_id,
        config.meshspace.name,
        root,
    )


def _finalize_legacy_meshspace(config: 'Config') -> None:
    """Populate meshspace metadata for the current legacy/single-mesh runtime."""
    device_id = str(config.device_id or "device").strip() or "device"
    data_dir = str(config.storage.data_dir or "")
    meshspace_id = build_legacy_meshspace_id(device_id, data_dir)
    config.meshspace.enabled = False
    config.meshspace.meshspace_id = meshspace_id
    config.meshspace.name = "Default Mesh"
    config.meshspace.data_root = data_dir
    config.meshspace.registry_root = _resolve_meshspace_registry_root(config, data_dir)
    config.meshspace.runtime_mode = "legacy-default"
    config.meshspace.is_default = True
    config.meshspace.supervised = False
    config.meshspace.network_quarantined = False
    config.meshspace.session_cookie_name = meshspace_cookie_name(meshspace_id)


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
    meshspace: MeshspaceConfig = field(default_factory=MeshspaceConfig)
    
    @classmethod
    def from_env(cls) -> 'Config':
        """Create configuration from environment variables."""
        config = cls()

        def _env_bool(name: str, default: bool) -> bool:
            raw = os.getenv(name)
            if raw is None:
                return default
            return raw.strip().lower() in {'1', 'true', 'yes', 'on'}
        
        # Override with environment variables if present
        config.debug = os.getenv('CANOPY_DEBUG', 'false').lower() == 'true'
        config.testing = os.getenv('CANOPY_TESTING', 'false').lower() == 'true'
        config.security.allow_unverified_relay_messages = _env_bool(
            'CANOPY_ALLOW_UNVERIFIED_RELAY_MESSAGES',
            config.security.allow_unverified_relay_messages,
        )
        config.security.e2e_private_channels = _env_bool(
            'CANOPY_E2E_PRIVATE_CHANNELS',
            config.security.e2e_private_channels,
        )
        config.security.e2e_private_channels_enforce = _env_bool(
            'CANOPY_E2E_PRIVATE_CHANNELS_ENFORCE',
            config.security.e2e_private_channels_enforce,
        )
        config.security.sync_digest_enabled = _env_bool(
            'CANOPY_SYNC_DIGEST_ENABLED',
            config.security.sync_digest_enabled,
        )
        config.security.sync_digest_require_capability = _env_bool(
            'CANOPY_SYNC_DIGEST_REQUIRE_CAPABILITY',
            config.security.sync_digest_require_capability,
        )
        config.security.identity_portability_enabled = _env_bool(
            'CANOPY_IDENTITY_PORTABILITY_ENABLED',
            config.security.identity_portability_enabled,
        )
        if digest_max := os.getenv('CANOPY_SYNC_DIGEST_MAX_CHANNELS'):
            try:
                config.security.sync_digest_max_channels_per_request = max(1, int(digest_max))
            except Exception:
                logger.warning(
                    "Invalid CANOPY_SYNC_DIGEST_MAX_CHANNELS value '%s'; using default %s",
                    digest_max,
                    config.security.sync_digest_max_channels_per_request,
                )
            
        # Network configuration
        if host := os.getenv('CANOPY_HOST'):
            config.network.host = host
        if port := os.getenv('CANOPY_PORT'):
            config.network.port = int(port)
        if mesh_port := os.getenv('CANOPY_MESH_PORT'):
            config.network.mesh_port = int(mesh_port)
        if discovery_port := os.getenv('CANOPY_DISCOVERY_PORT'):
            config.network.discovery_port = int(discovery_port)
        if relay := os.getenv('CANOPY_RELAY_POLICY'):
            if relay in ('off', 'broker_only', 'full_relay'):
                config.network.relay_policy = relay
        network_env_overrides = {
            'enable_tls': os.getenv('CANOPY_ENABLE_TLS') is not None,
            'tls_cert_path': os.getenv('CANOPY_TLS_CERT_PATH') is not None,
            'tls_key_path': os.getenv('CANOPY_TLS_KEY_PATH') is not None,
            'require_verified_wss': os.getenv('CANOPY_REQUIRE_VERIFIED_WSS') is not None,
            'external_tls_endpoint': os.getenv('CANOPY_EXTERNAL_TLS_ENDPOINT') is not None,
            'transport_security_mode': os.getenv('CANOPY_TRANSPORT_SECURITY_MODE') is not None,
        }
        config.network.enable_tls = _env_bool('CANOPY_ENABLE_TLS', config.network.enable_tls)
        if tls_cert_path := os.getenv('CANOPY_TLS_CERT_PATH'):
            config.network.tls_cert_path = tls_cert_path
        if tls_key_path := os.getenv('CANOPY_TLS_KEY_PATH'):
            config.network.tls_key_path = tls_key_path
        config.network.require_verified_wss = _env_bool(
            'CANOPY_REQUIRE_VERIFIED_WSS',
            config.network.require_verified_wss,
        )
        if external_tls_endpoint := os.getenv('CANOPY_EXTERNAL_TLS_ENDPOINT'):
            config.network.external_tls_endpoint = external_tls_endpoint.strip()
        if transport_mode := os.getenv('CANOPY_TRANSPORT_SECURITY_MODE'):
            clean_mode = transport_mode.strip().lower()
            if clean_mode in TRANSPORT_SECURITY_MODES:
                config.network.transport_security_mode = clean_mode

        # Store device info on config for display. The peer identity remains mesh-local
        # because it lives under config.storage.data_dir, but the machine device label is
        # still useful in the shell and diagnostics.
        from .device import get_device_id, get_device_label
        config.device_id = get_device_id()
        config.device_label = get_device_label()

        meshspace_id = os.getenv('CANOPY_MESHSPACE_ID', '').strip()
        meshspace_name = os.getenv('CANOPY_MESHSPACE_NAME', '').strip()
        meshspace_root = os.getenv('CANOPY_MESHSPACE_ROOT', '').strip()
        meshspace_supervised = _env_bool('CANOPY_MESHSPACE_SUPERVISED', False)
        meshspace_network_quarantined = _env_bool('CANOPY_MESHSPACE_NETWORK_QUARANTINED', False)

        if meshspace_id:
            _apply_meshspace_paths(
                config,
                meshspace_id=meshspace_id,
                meshspace_name=meshspace_name or meshspace_id,
                meshspace_root=meshspace_root or None,
                supervised=meshspace_supervised,
                network_quarantined=meshspace_network_quarantined,
            )
            if os.getenv('CANOPY_DATA_DIR') or os.getenv('CANOPY_DATABASE_PATH'):
                logger.warning(
                    "Ignoring CANOPY_DATA_DIR/CANOPY_DATABASE_PATH because CANOPY_MESHSPACE_ID is set; "
                    "use CANOPY_MESHSPACE_ROOT for meshspace runtimes"
                )
        else:
            # Apply device-specific data paths (database, secret key, files)
            # This must happen before legacy overrides so the user can still force
            # a specific path via env var in single-mesh mode.
            _apply_device_paths(config)

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
            _finalize_legacy_meshspace(config)

        if secret_key := os.getenv('CANOPY_SECRET_KEY'):
            config.secret_key = secret_key

        apply_persisted_transport_security_settings(
            config,
            env_overrides=network_env_overrides,
        )

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
                'relay_policy': self.network.relay_policy,
                'enable_tls': self.network.enable_tls,
                'tls_cert_path': self.network.tls_cert_path,
                'tls_key_path': self.network.tls_key_path,
                'require_verified_wss': self.network.require_verified_wss,
                'external_tls_endpoint': self.network.external_tls_endpoint,
                'transport_security_mode': self.network.transport_security_mode,
                'transport_security_config_path': self.network.transport_security_config_path,
            },
            'security': {
                'encryption_algorithm': self.security.encryption_algorithm,
                'session_timeout': self.security.session_timeout,
                'trust_threshold': self.security.trust_threshold,
                'allow_unverified_relay_messages': self.security.allow_unverified_relay_messages,
                'e2e_private_channels': self.security.e2e_private_channels,
                'e2e_private_channels_enforce': self.security.e2e_private_channels_enforce,
                'sync_digest_enabled': self.security.sync_digest_enabled,
                'sync_digest_require_capability': self.security.sync_digest_require_capability,
                'sync_digest_max_channels_per_request': self.security.sync_digest_max_channels_per_request,
                'identity_portability_enabled': self.security.identity_portability_enabled,
            },
            'storage': {
                'database_path': self.storage.database_path,
                'data_dir': self.storage.data_dir,
                'max_message_size': self.storage.max_message_size,
                'max_file_size': self.storage.max_file_size,
            },
            'meshspace': {
                'enabled': self.meshspace.enabled,
                'meshspace_id': self.meshspace.meshspace_id,
                'name': self.meshspace.name,
                'data_root': self.meshspace.data_root,
                'registry_root': self.meshspace.registry_root,
                'runtime_mode': self.meshspace.runtime_mode,
                'is_default': self.meshspace.is_default,
                'supervised': self.meshspace.supervised,
                'network_quarantined': self.meshspace.network_quarantined,
                'session_cookie_name': self.meshspace.session_cookie_name,
            },
            'ui': {
                'theme': self.ui.theme,
                'language': self.ui.language,
                'auto_refresh': self.ui.auto_refresh,
                'max_feed_items': self.ui.max_feed_items,
            }
        }
