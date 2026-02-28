"""
Device identity for Canopy.

Each physical machine (or VM) gets a stable device_id that persists across
reboots.  The identity file lives in ~/.canopy/ — a location that is NOT
synced by Dropbox / iCloud / Git — so even when the Canopy source tree is
shared across machines via cloud sync, each machine keeps its own identity.

The device_id is used to create a per-device data directory inside the
project (e.g. ./data/devices/<device_id>/) so that databases, peer keys,
uploaded files, and session secrets are fully isolated per machine.

Author: Konrad Walus (architecture, design, and direction)
Project: Canopy - Local Mesh Communication
License: Apache 2.0
Development: AI-assisted implementation (Claude, Codex, GitHub Copilot, Cursor IDE, Ollama)
"""

import json
import logging
import os
import platform
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, Optional, cast

logger = logging.getLogger('canopy.device')

# Where device identity is stored (per-machine, NOT cloud-synced)
_DEVICE_DIR = Path.home() / '.canopy'
_DEVICE_FILE = _DEVICE_DIR / 'device_identity.json'


def _resolve_path(path: Path) -> Path:
    """Best-effort absolute path expansion without throwing on odd filesystems."""
    try:
        return path.expanduser().resolve()
    except Exception:
        return path.expanduser()


def _default_project_data_root() -> Path:
    """Stable project data root derived from source location, not process CWD."""
    return Path(__file__).resolve().parents[2] / 'data'


def _persist_device_data_root(data_root: Path) -> None:
    """Persist selected data root into device identity for cross-restart stability."""
    try:
        identity = get_device_identity()
        root_value = str(_resolve_path(data_root))
        if str(identity.get('data_root') or '') == root_value:
            return
        identity['data_root'] = root_value
        _DEVICE_DIR.mkdir(parents=True, exist_ok=True)
        _DEVICE_FILE.write_text(json.dumps(identity, indent=2))
        os.chmod(_DEVICE_FILE, 0o600)
        logger.info(f"Persisted device data root: {root_value}")
    except Exception as e:
        logger.warning(f"Could not persist data_root in {_DEVICE_FILE}: {e}")


def get_device_identity() -> Dict[str, Any]:
    """
    Return the stable device identity for this machine.

    On first call the identity is generated and persisted to
    ~/.canopy/device_identity.json.  Subsequent calls return the
    same identity.

    Returns dict with at least:
        device_id   – short stable UUID (e.g. "a3f7b2c1")
        hostname    – machine hostname at creation time
        label       – human-friendly label (defaults to hostname; editable)
        created_at  – ISO timestamp
    """
    if _DEVICE_FILE.exists():
        try:
            data = json.loads(_DEVICE_FILE.read_text())
            if data.get('device_id'):
                logger.debug(f"Loaded device identity: {data['device_id']} ({data.get('label', '?')})")
                return cast(Dict[str, Any], data)
        except Exception as e:
            logger.warning(f"Could not read {_DEVICE_FILE}: {e}")

    # First run on this machine — generate identity
    device_id = uuid.uuid4().hex[:8]
    hostname = platform.node() or 'unknown'
    identity = {
        'device_id': device_id,
        'hostname': hostname,
        'label': hostname,  # user can rename later
        'platform': platform.system(),
        'created_at': datetime.now(timezone.utc).isoformat(),
    }

    try:
        _DEVICE_DIR.mkdir(parents=True, exist_ok=True)
        _DEVICE_FILE.write_text(json.dumps(identity, indent=2))
        os.chmod(_DEVICE_FILE, 0o600)
        logger.info(f"Created new device identity: {device_id} (hostname={hostname})")
    except Exception as e:
        logger.error(f"Could not persist device identity to {_DEVICE_FILE}: {e}")
        # Still usable for this session; will regenerate next time (not ideal but safe)

    return identity


def get_device_id() -> str:
    """Shorthand: return just the device_id string."""
    return cast(str, get_device_identity()['device_id'])


def get_device_data_dir(project_data_root: Path = Path('./data')) -> Path:
    """
    Return the per-device data directory.

    Default layout:  <project_data_root>/devices/<device_id>/

    Override: set CANOPY_DATA_ROOT env var to store device data outside the
    project tree (useful when the source folder is synced via Dropbox/iCloud
    and you don't want databases to collide across machines).

    Creates the directory if it doesn't exist.
    """
    device_id = get_device_id()

    env_root = os.environ.get('CANOPY_DATA_ROOT', '').strip()
    data_root: Optional[Path] = None

    # 1) Explicit override always wins.
    if env_root:
        data_root = _resolve_path(Path(env_root))
    else:
        # 2) Reuse previously chosen root from device identity when present.
        identity = get_device_identity()
        persisted_root = str(identity.get('data_root') or '').strip()
        if persisted_root:
            data_root = _resolve_path(Path(persisted_root))
        else:
            # 3) First-run selection (and persist it):
            #    prefer any root that already contains this device DB.
            cwd_root = (
                _resolve_path(project_data_root)
                if project_data_root.is_absolute()
                else _resolve_path(Path.cwd() / project_data_root)
            )
            module_root = _resolve_path(_default_project_data_root())
            home_root = _resolve_path(_DEVICE_DIR / 'data')

            candidates = []
            for root in (module_root, cwd_root, home_root):
                if root not in candidates:
                    candidates.append(root)

            existing_roots = [
                root for root in candidates
                if (root / 'devices' / device_id / 'canopy.db').exists()
            ]
            if existing_roots:
                data_root = existing_roots[0]
            else:
                # Default new installs to the module-derived project root when writable;
                # otherwise fall back to ~/.canopy/data.
                writable_base = module_root if os.access(module_root.parent, os.W_OK) else home_root
                data_root = writable_base

            _persist_device_data_root(data_root)

    device_dir = data_root / 'devices' / device_id
    try:
        device_dir.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        # Last-resort fallback to ~/.canopy/data when selected root is unavailable.
        if env_root:
            raise
        fallback_root = _resolve_path(_DEVICE_DIR / 'data')
        logger.warning(
            f"Could not create device data dir under {data_root} ({e}); "
            f"falling back to {fallback_root}"
        )
        _persist_device_data_root(fallback_root)
        device_dir = fallback_root / 'devices' / device_id
        device_dir.mkdir(parents=True, exist_ok=True)

    return device_dir


def get_device_label() -> str:
    """Return the human-friendly label for this device."""
    return cast(str, get_device_identity().get('label', get_device_id()))


def set_device_label(label: str) -> bool:
    """Update the human-friendly label for this device."""
    try:
        identity = get_device_identity()
        identity['label'] = label
        _DEVICE_FILE.write_text(json.dumps(identity, indent=2))
        logger.info(f"Device label updated to: {label}")
        return True
    except Exception as e:
        logger.error(f"Failed to update device label: {e}")
        return False


# ---------------------------------------------------------------------------
# Device Profile  (display name, description, avatar)
# ---------------------------------------------------------------------------
_DEVICE_PROFILE_FILE = _DEVICE_DIR / 'device_profile.json'


def get_device_profile() -> Dict[str, Any]:
    """Return the full device profile.

    Fields:
        display_name – user-friendly name shown to peers (defaults to label)
        description  – free-text description of this device
        avatar_b64   – base64-encoded avatar image (small, thumbnail)
        avatar_mime  – MIME type of the avatar (e.g. image/png)
    """
    default = {
        'display_name': get_device_label(),
        'description': '',
        'avatar_b64': '',
        'avatar_mime': '',
    }
    if _DEVICE_PROFILE_FILE.exists():
        try:
            data = json.loads(_DEVICE_PROFILE_FILE.read_text())
            for k in default:
                if k not in data:
                    data[k] = default[k]
            return cast(Dict[str, Any], data)
        except Exception as e:
            logger.warning(f"Could not read device profile: {e}")
    return default


def set_device_profile(display_name: Optional[str] = None,
                       description: Optional[str] = None,
                       avatar_b64: Optional[str] = None,
                       avatar_mime: Optional[str] = None) -> bool:
    """Update one or more device profile fields.  None = keep existing."""
    try:
        profile = get_device_profile()
        if display_name is not None:
            profile['display_name'] = display_name
        if description is not None:
            profile['description'] = description
        if avatar_b64 is not None:
            profile['avatar_b64'] = avatar_b64
        if avatar_mime is not None:
            profile['avatar_mime'] = avatar_mime
        _DEVICE_DIR.mkdir(parents=True, exist_ok=True)
        _DEVICE_PROFILE_FILE.write_text(json.dumps(profile, indent=2))
        os.chmod(_DEVICE_PROFILE_FILE, 0o600)
        logger.info(f"Device profile updated: display_name={profile['display_name']}")
        return True
    except Exception as e:
        logger.error(f"Failed to update device profile: {e}")
        return False
