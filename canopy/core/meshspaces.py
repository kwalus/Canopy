"""Meshspace registry and runtime metadata helpers.

Meshspaces are machine-local runtime containers that isolate Canopy data,
peer identity, trust, users, channels, files, and invites by storage root
and process boundary. This module intentionally manages only shell-safe
metadata; it does not read child runtime databases directly.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

logger = logging.getLogger("canopy.meshspaces")

try:  # pragma: no cover - platform-specific import
    import fcntl  # type: ignore
except Exception:  # pragma: no cover
    fcntl = None

try:  # pragma: no cover - platform-specific import
    import msvcrt  # type: ignore
except Exception:  # pragma: no cover
    msvcrt = None

_PORT_BLOCK_BASE = 7800
_PORT_BLOCK_SIZE = 3
_MESHSPACE_AVATAR_MAX_BYTES = 5 * 1024 * 1024
_MESHSPACE_AVATAR_MAX_DIMENSION = 768
_MESHSPACE_AVATAR_PREVIEW_MAX_BYTES = 24 * 1024
_MESHSPACE_AVATAR_PREVIEW_DIMENSION = 96
_PROBE_CACHE_TTL = 2.0
_WINDOWS_DETACHED_PROCESS = getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
_WINDOWS_NEW_PROCESS_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
_WINDOWS_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
_WINDOWS_BREAKAWAY_FROM_JOB = getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0x01000000)
_MESHSPACE_DEFAULT_AGENT_PERMISSIONS = [
    "read_messages",
    "write_messages",
    "read_feed",
    "write_feed",
]
_MESHSPACE_ALLOWED_AGENT_PERMISSIONS = {
    "read_messages",
    "write_messages",
    "read_files",
    "write_files",
    "read_feed",
    "write_feed",
    "voice_call",
    "manage_keys",
    "view_trust",
    "delete_data",
}
_MESHSPACE_RUNTIME_ENV_KEYS = {
    "CANOPY_DATA_DIR",
    "CANOPY_DATABASE_PATH",
    "CANOPY_PORT",
    "CANOPY_MESH_PORT",
    "CANOPY_DISCOVERY_PORT",
    "CANOPY_MESHSPACE_ID",
    "CANOPY_MESHSPACE_NAME",
    "CANOPY_MESHSPACE_ROOT",
    "CANOPY_MESHSPACE_SUPERVISED",
    "CANOPY_MESHSPACE_NETWORK_QUARANTINED",
    "CANOPY_ENABLE_TLS",
    "CANOPY_TLS_CERT_PATH",
    "CANOPY_TLS_KEY_PATH",
    "CANOPY_REQUIRE_VERIFIED_WSS",
    "CANOPY_EXTERNAL_TLS_ENDPOINT",
    "CANOPY_TRANSPORT_SECURITY_MODE",
}


def _default_summary() -> Dict[str, Any]:
    """Return the shell-safe mesh summary persisted in the local registry.

    These values are machine-local shell hints for local mesh management.
    They are not authoritative state and may lag briefly behind the runtime,
    but they should preserve local unread/mention/admin-review pressure so the
    mesh shell can reflect active worlds accurately.
    """
    return {
        "active_peer_count": 0,
        "attention_count": 0,
        "unread_count": 0,
        "mention_count": 0,
        "pending_review_count": 0,
        "last_activity_at": "",
        "last_summary_at": "",
        "has_recent_activity": False,
        "attention_level": "quiet",
    }


def _normalize_agent_permission_values(raw: Any) -> List[str]:
    """Normalize mesh-local default agent API permissions."""
    values = raw if isinstance(raw, list) else _MESHSPACE_DEFAULT_AGENT_PERMISSIONS
    normalized: List[str] = []
    for value in values:
        text = str(value or "").strip().lower()
        if text and text in _MESHSPACE_ALLOWED_AGENT_PERMISSIONS and text not in normalized:
            normalized.append(text)
    return normalized or list(_MESHSPACE_DEFAULT_AGENT_PERMISSIONS)


def _summary_attention_level(summary: Dict[str, Any], *, status: str = "") -> str:
    """Derive shell attention level from status plus shell-safe summary counts."""
    status_lower = str(status or "").strip().lower()
    if status_lower == "crashed":
        return "critical"
    if status_lower in {"degraded", "stopping"}:
        return "warning"
    attention_count = max(0, int(summary.get("attention_count") or 0))
    pending_review_count = max(0, int(summary.get("pending_review_count") or 0))
    if attention_count > 0:
        return "warning" if pending_review_count > 0 else "active"
    if bool(summary.get("has_recent_activity")) or max(0, int(summary.get("active_peer_count") or 0)) > 0:
        return "active"
    return "quiet"


def _sanitized_runtime_environment(base_env: Dict[str, str], overrides: Dict[str, str]) -> Dict[str, str]:
    """Remove inherited runtime selectors before applying one meshspace's env."""
    cleaned = {
        str(key): str(value)
        for key, value in (base_env or {}).items()
        if str(key) not in _MESHSPACE_RUNTIME_ENV_KEYS
    }
    for key, value in (overrides or {}).items():
        text = str(value or "").strip()
        if text:
            cleaned[str(key)] = text
    return cleaned


def _windows_creation_flags(*, include_breakaway: bool) -> int:
    """Return the preferred detached process flags for Windows mesh launches."""
    flags = _WINDOWS_DETACHED_PROCESS | _WINDOWS_NEW_PROCESS_GROUP | _WINDOWS_NO_WINDOW
    if include_breakaway:
        flags |= _WINDOWS_BREAKAWAY_FROM_JOB
    return flags


def _is_windows_platform() -> bool:
    """Return True when meshspace process-launch behavior should follow Windows rules."""
    return os.name == "nt"


def _windows_preferred_python_executable(default_python: str) -> str:
    """Prefer ``pythonw.exe`` on Windows for silent background launches."""
    if not _is_windows_platform():
        return default_python
    import ntpath

    candidate = str(default_python or "")
    if ntpath.basename(candidate).lower() != "python.exe":
        return default_python
    pythonw = ntpath.join(ntpath.dirname(candidate), "pythonw.exe")
    return pythonw if os.path.exists(pythonw) else default_python


def _windows_pid_is_alive(pid: int) -> bool:
    """Return True if a Windows PID still exists without relying on ``os.kill(pid, 0)``."""
    if pid <= 0:
        return False
    try:
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        process = kernel32.OpenProcess(0x1000, False, int(pid))
        if not process:
            return False
        try:
            exit_code = ctypes.c_ulong(0)
            if not kernel32.GetExitCodeProcess(process, ctypes.byref(exit_code)):
                return False
            return int(exit_code.value) == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(process)
    except Exception:
        return False


def count_meshspace_message_unread(db_manager: Any, user_id: str) -> int:
    """Count unread direct and group messages for one local user."""
    if not db_manager or not user_id:
        return 0
    try:
        with db_manager.get_connection() as conn:
            row = conn.execute(
                """
                SELECT COUNT(DISTINCT m.id) AS unread_count
                FROM messages m
                WHERE m.read_at IS NULL
                  AND COALESCE(m.sender_id, '') != ?
                  AND (
                      m.recipient_id = ?
                      OR EXISTS (
                          SELECT 1
                          FROM json_each(
                              CASE WHEN json_valid(m.metadata) THEN m.metadata ELSE '{}' END,
                              '$.group_members'
                          ) gm
                          WHERE CAST(gm.value AS TEXT) = ?
                      )
                  )
                """,
                (user_id, user_id, user_id),
            ).fetchone()
            return max(0, int((row["unread_count"] if row else 0) or 0))
    except Exception:
        return 0


def count_meshspace_channel_unread(channel_manager: Any, p2p_manager: Any, user_id: str) -> int:
    """Count unread channel items for one local user."""
    if not channel_manager or not user_id:
        return 0
    try:
        # Keep meshspace shell counts aligned with the sidebar path. The channel
        # manager API takes only ``user_id``; passing extra args silently zeroed
        # mesh header unread totals via this exception path.
        channels = channel_manager.get_user_channels(user_id) or []
    except Exception:
        return 0
    total = 0
    for channel in channels:
        try:
            total += max(0, int(getattr(channel, "unread_count", 0) or 0))
        except Exception:
            continue
    return total


def count_meshspace_feed_unread(feed_manager: Any, user_id: str) -> int:
    """Count unread feed posts for one local user."""
    if not feed_manager or not user_id:
        return 0
    try:
        if hasattr(feed_manager, "count_unread_posts"):
            return max(0, int(feed_manager.count_unread_posts(user_id) or 0))
    except Exception:
        return 0
    return 0


def count_meshspace_unacked_mentions(db_manager: Any, user_id: str) -> int:
    """Count unacknowledged mentions for one local user."""
    if not db_manager or not user_id:
        return 0
    try:
        with db_manager.get_connection() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS n
                FROM mention_events
                WHERE user_id = ? AND acknowledged_at IS NULL
                """,
                (user_id,),
            ).fetchone()
            return max(0, int((row["n"] if row else 0) or 0))
    except Exception:
        return 0


def build_meshspace_notification_summary(
    db_manager: Any,
    channel_manager: Any,
    feed_manager: Any,
    p2p_manager: Any,
    user_id: str,
) -> Dict[str, int]:
    """Return shell-safe local notification counts for one mesh-local user."""
    if not user_id:
        return {
            "unread_count": 0,
            "mention_count": 0,
            "pending_review_count": 0,
            "attention_count": 0,
        }

    unread_count = (
        count_meshspace_message_unread(db_manager, user_id)
        + count_meshspace_channel_unread(channel_manager, p2p_manager, user_id)
        + count_meshspace_feed_unread(feed_manager, user_id)
    )
    mention_count = count_meshspace_unacked_mentions(db_manager, user_id)
    owner_id = db_manager.get_instance_owner_user_id() if db_manager else None
    pending_review_count = (
        max(0, int(db_manager.get_pending_approval_count() or 0))
        if owner_id and owner_id == user_id
        else 0
    )
    return {
        "unread_count": unread_count,
        "mention_count": mention_count,
        "pending_review_count": pending_review_count,
        "attention_count": unread_count + mention_count + pending_review_count,
    }


def build_meshspace_shell_summary(
    db_manager: Any,
    channel_manager: Any,
    feed_manager: Any,
    p2p_manager: Any,
    *,
    user_id: Optional[str],
    status: str = "",
    last_activity_at: str = "",
    fallback_last_seen_at: str = "",
) -> Dict[str, Any]:
    """Build the machine-local shell summary for one runtime."""
    try:
        active_peer_count = len(p2p_manager.get_connected_peers() or []) if p2p_manager else 0
    except Exception:
        active_peer_count = 0

    last_activity = str(last_activity_at or fallback_last_seen_at or "").strip()
    has_recent_activity = False
    if last_activity:
        try:
            parsed = datetime.fromisoformat(last_activity.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            has_recent_activity = (datetime.now(timezone.utc) - parsed).total_seconds() <= 30 * 60
        except Exception:
            has_recent_activity = False

    counts = build_meshspace_notification_summary(
        db_manager,
        channel_manager,
        feed_manager,
        p2p_manager,
        str(user_id or "").strip(),
    ) if user_id else {
        "unread_count": 0,
        "mention_count": 0,
        "pending_review_count": 0,
        "attention_count": 0,
    }

    status_lower = str(status or "").strip().lower()
    attention_level = "quiet"
    if status_lower == "crashed":
        attention_level = "critical"
    elif status_lower in {"degraded", "stopping"}:
        attention_level = "warning"
    elif counts["attention_count"] > 0:
        attention_level = "warning" if counts["pending_review_count"] > 0 else "active"
    elif has_recent_activity or active_peer_count > 0:
        attention_level = "active"

    return {
        "active_peer_count": active_peer_count,
        "attention_count": max(0, int(counts["attention_count"] or 0)),
        "unread_count": max(0, int(counts["unread_count"] or 0)),
        "mention_count": max(0, int(counts["mention_count"] or 0)),
        "pending_review_count": max(0, int(counts["pending_review_count"] or 0)),
        "last_activity_at": last_activity,
        "last_summary_at": datetime.now(timezone.utc).isoformat(),
        "has_recent_activity": has_recent_activity,
        "attention_level": attention_level,
    }


def meshspaces_root_dir() -> Path:
    """Return the machine-local meshspace root directory."""
    override = os.getenv("CANOPY_MESHSPACE_REGISTRY_ROOT", "").strip()
    root = Path(override).expanduser() if override else (Path.home() / ".canopy" / "meshspaces")
    root.mkdir(parents=True, exist_ok=True)
    return root


def _registry_file_path(registry_root: Optional[Path] = None) -> Path:
    root = Path(registry_root).expanduser() if registry_root is not None else meshspaces_root_dir()
    return root / "registry.json"


def _registry_lock_path(registry_root: Optional[Path] = None) -> Path:
    root = Path(registry_root).expanduser() if registry_root is not None else meshspaces_root_dir()
    return root / "registry.lock"


def default_meshspace_root(meshspace_id: str) -> Path:
    """Return the default data root for a meshspace."""
    return meshspaces_root_dir() / meshspace_id


def build_runtime_environment_from_config(config: Any) -> Dict[str, str]:
    """Build an explicit environment contract for the currently running runtime."""
    mesh_cfg = getattr(config, "meshspace", None)
    env_map: Dict[str, str] = {
        "CANOPY_PORT": str(int(getattr(config.network, "port", 0) or 0)),
        "CANOPY_MESH_PORT": str(int(getattr(config.network, "mesh_port", 0) or 0)),
        "CANOPY_DISCOVERY_PORT": str(int(getattr(config.network, "discovery_port", 0) or 0)),
        "CANOPY_ENABLE_TLS": "true" if bool(getattr(config.network, "enable_tls", False)) else "false",
        "CANOPY_REQUIRE_VERIFIED_WSS": "true"
        if bool(getattr(config.network, "require_verified_wss", False))
        else "false",
    }
    tls_cert_path = str(getattr(config.network, "tls_cert_path", "") or "").strip()
    tls_key_path = str(getattr(config.network, "tls_key_path", "") or "").strip()
    external_tls_endpoint = str(getattr(config.network, "external_tls_endpoint", "") or "").strip()
    transport_mode = str(getattr(config.network, "transport_security_mode", "") or "").strip()
    if tls_cert_path:
        env_map["CANOPY_TLS_CERT_PATH"] = tls_cert_path
    if tls_key_path:
        env_map["CANOPY_TLS_KEY_PATH"] = tls_key_path
    if external_tls_endpoint:
        env_map["CANOPY_EXTERNAL_TLS_ENDPOINT"] = external_tls_endpoint
    if transport_mode:
        env_map["CANOPY_TRANSPORT_SECURITY_MODE"] = transport_mode
    if mesh_cfg and str(getattr(mesh_cfg, "meshspace_id", "") or "").strip():
        env_map.update(
            {
                "CANOPY_MESHSPACE_ID": str(getattr(mesh_cfg, "meshspace_id", "") or "").strip(),
                "CANOPY_MESHSPACE_NAME": str(getattr(mesh_cfg, "name", "") or "").strip(),
                "CANOPY_MESHSPACE_ROOT": str(getattr(mesh_cfg, "data_root", "") or "").strip(),
                "CANOPY_MESHSPACE_SUPERVISED": "true" if bool(getattr(mesh_cfg, "supervised", False)) else "false",
                "CANOPY_MESHSPACE_NETWORK_QUARANTINED": "true"
                if bool(getattr(mesh_cfg, "network_quarantined", False))
                else "false",
            }
        )
    else:
        env_map["CANOPY_DATA_DIR"] = str(getattr(config.storage, "data_dir", "") or "").strip()
    return {key: value for key, value in env_map.items() if value}


def schedule_self_restart(
    env_map: Dict[str, str],
    *,
    cwd: Optional[str] = None,
    parent_pid: Optional[int] = None,
    delay_seconds: float = 1.0,
) -> int:
    """Schedule a replacement Canopy process after the current process exits."""
    wait_pid = int(parent_pid or os.getpid())
    launcher_env = dict(os.environ)
    launcher_env.update(env_map)
    launcher_env["CANOPY_RESTART_PARENT_PID"] = str(wait_pid)
    launcher_env["CANOPY_RESTART_DELAY"] = str(max(0.2, float(delay_seconds or 1.0)))
    launcher_env["CANOPY_RESTART_CWD"] = str(cwd or Path.cwd())
    helper = (
        "import os, sys, time, subprocess\n"
        "def _parent_alive(pid):\n"
        "    if pid <= 0:\n"
        "        return False\n"
        "    if os.name == 'nt':\n"
        "        try:\n"
        "            import ctypes\n"
        "            kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)\n"
        "            process = kernel32.OpenProcess(0x1000, False, int(pid))\n"
        "            if not process:\n"
        "                return False\n"
        "            try:\n"
        "                exit_code = ctypes.c_ulong(0)\n"
        "                if not kernel32.GetExitCodeProcess(process, ctypes.byref(exit_code)):\n"
        "                    return False\n"
        "                return int(exit_code.value) == 259\n"
        "            finally:\n"
        "                kernel32.CloseHandle(process)\n"
        "        except Exception:\n"
        "            return False\n"
        "    try:\n"
        "        os.kill(pid, 0)\n"
        "        return True\n"
        "    except PermissionError:\n"
        "        return True\n"
        "    except Exception:\n"
        "        return False\n"
        "parent = int(os.environ.get('CANOPY_RESTART_PARENT_PID', '0') or '0')\n"
        "delay = float(os.environ.get('CANOPY_RESTART_DELAY', '1.0') or '1.0')\n"
        "cwd = os.environ.get('CANOPY_RESTART_CWD') or os.getcwd()\n"
        "child_env = dict(os.environ)\n"
        "for key in ('CANOPY_RESTART_PARENT_PID', 'CANOPY_RESTART_DELAY', 'CANOPY_RESTART_CWD'):\n"
        "    child_env.pop(key, None)\n"
        "for _ in range(300):\n"
        "    if parent <= 0 or not _parent_alive(parent):\n"
        "        break\n"
        "    time.sleep(0.1)\n"
        "time.sleep(max(0.2, delay))\n"
        "popen_kwargs = {'env': child_env, 'cwd': cwd, 'stdin': subprocess.DEVNULL, 'stdout': subprocess.DEVNULL, 'stderr': subprocess.DEVNULL}\n"
        "if os.name == 'nt':\n"
        "    flags = getattr(subprocess, 'DETACHED_PROCESS', 0x00000008)\n"
        "    flags |= getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0x00000200)\n"
        "    flags |= getattr(subprocess, 'CREATE_NO_WINDOW', 0x08000000)\n"
        "    flags_with_breakaway = flags | getattr(subprocess, 'CREATE_BREAKAWAY_FROM_JOB', 0x01000000)\n"
        "    try:\n"
        "        popen_kwargs['creationflags'] = flags_with_breakaway\n"
        "        subprocess.Popen([sys.executable, '-m', 'canopy.main'], **popen_kwargs)\n"
        "    except OSError:\n"
        "        popen_kwargs['creationflags'] = flags\n"
        "        subprocess.Popen([sys.executable, '-m', 'canopy.main'], **popen_kwargs)\n"
        "else:\n"
        "    popen_kwargs['start_new_session'] = True\n"
        "    subprocess.Popen([sys.executable, '-m', 'canopy.main'], **popen_kwargs)\n"
    )
    launcher_python = _windows_preferred_python_executable(os.sys.executable or "python3")
    proc = subprocess.Popen(
        [launcher_python, "-c", helper],
        env=launcher_env,
        cwd=str(cwd or Path.cwd()),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=_windows_creation_flags(include_breakaway=True) if _is_windows_platform() else 0,
        start_new_session=False if _is_windows_platform() else True,
    )
    return int(proc.pid)


def sanitize_meshspace_id(value: str, fallback: str = "meshspace") -> str:
    raw = str(value or "").strip().lower()
    if not raw:
        return fallback
    cleaned = []
    for char in raw:
        if char.isalnum():
            cleaned.append(char)
        elif char in ("-", "_", " "):
            cleaned.append("-")
    slug = "".join(cleaned).strip("-")
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug or fallback


def build_legacy_meshspace_id(device_id: str, data_dir: str) -> str:
    digest = hashlib.sha1(str(data_dir or "").encode("utf-8")).hexdigest()[:6]
    base = sanitize_meshspace_id(f"legacy-{device_id}-{digest}", fallback="legacy-default")
    return base


def meshspace_cookie_name(meshspace_id: str) -> str:
    normalized = sanitize_meshspace_id(meshspace_id, fallback="default")
    if len(normalized) <= 32:
        suffix = normalized
    else:
        suffix = hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:12]
    return f"canopy_session_{suffix}"


def _normalize_meshspace_id_aliases(raw: Any, *, current_id: str = "") -> List[str]:
    aliases: List[str] = []
    values = raw if isinstance(raw, (list, tuple, set)) else ([raw] if raw else [])
    current = sanitize_meshspace_id(current_id, fallback="") if current_id else ""
    for value in values:
        alias = sanitize_meshspace_id(str(value or "").strip(), fallback="")
        if not alias or alias == current or alias in aliases:
            continue
        aliases.append(alias)
    return aliases


def apply_meshspace_record_to_config(
    config: Any,
    record: Dict[str, Any],
    *,
    avatar_preview: Optional[Dict[str, str]] = None,
) -> None:
    """Copy registry-backed mesh identity fields onto the active config."""
    if not config or not isinstance(record, dict):
        return
    mesh_cfg = getattr(config, "meshspace", None)
    if not mesh_cfg:
        return
    mesh_cfg.meshspace_id = str(record.get("meshspace_id") or mesh_cfg.meshspace_id or "").strip()
    mesh_cfg.meshspace_id_aliases = _normalize_meshspace_id_aliases(
        record.get("meshspace_id_aliases"),
        current_id=mesh_cfg.meshspace_id,
    )
    mesh_cfg.name = str(record.get("name") or mesh_cfg.name or "").strip()
    mesh_cfg.description = str(record.get("description") or "").strip()
    mesh_cfg.runtime_mode = str(record.get("runtime_mode") or mesh_cfg.runtime_mode or "").strip() or "meshspace"
    mesh_cfg.is_default = bool(record.get("is_default"))
    mesh_cfg.supervised = bool(record.get("supervised"))
    mesh_cfg.network_quarantined = bool(record.get("network_quarantined"))
    mesh_cfg.session_cookie_name = str(
        record.get("session_cookie_name") or mesh_cfg.session_cookie_name or meshspace_cookie_name(mesh_cfg.meshspace_id)
    ).strip()
    mesh_cfg.avatar_path = str(record.get("avatar_path") or "").strip()
    mesh_cfg.avatar_mime = str(record.get("avatar_mime") or "image/png").strip() or "image/png"
    preview = avatar_preview if isinstance(avatar_preview, dict) else {}
    mesh_cfg.avatar_preview_b64 = str(preview.get("avatar_b64") or "").strip()
    mesh_cfg.avatar_preview_mime = str(preview.get("avatar_mime") or "image/png").strip() or "image/png"


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _resolved_path(value: str | Path) -> Path:
    """Normalize a filesystem path for safe overlap comparisons."""
    return Path(value).expanduser().resolve(strict=False)


def _paths_overlap(left: Path, right: Path) -> bool:
    """Return True when two paths are equal or nested within each other."""
    return left == right or left in right.parents or right in left.parents


def _pid_is_alive(pid: int) -> bool:
    """Return True if *pid* is still running (signal-0 probe)."""
    if pid <= 0:
        return False
    if _is_windows_platform():
        return _windows_pid_is_alive(pid)
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except ProcessLookupError:
        return False
    except Exception:
        return False


def _port_accepts_connections(host: str, port: int, timeout: float = 0.5) -> bool:
    """Return True when *host:port* accepts TCP connections."""
    if port <= 0:
        return False
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _normalize_listener_host(host: str) -> str:
    """Normalize a listener host string for host-aware PID matching."""
    value = str(host or "").strip().lower()
    if not value:
        return ""
    if value.startswith("[") and value.endswith("]"):
        value = value[1:-1]
    if value == "localhost":
        return "127.0.0.1"
    return value


def _listener_host_matches(local_host: str, expected_host: Optional[str]) -> bool:
    """Return True when a listener host is compatible with the expected host."""
    normalized_local = _normalize_listener_host(local_host)
    normalized_expected = _normalize_listener_host(expected_host or "")
    if not normalized_expected:
        return True
    if normalized_local == normalized_expected:
        return True
    if normalized_local in {"0.0.0.0", "::", "*"}:
        return True
    if normalized_expected == "127.0.0.1" and normalized_local == "::1":
        return True
    return False


def _windows_listener_pid_for_port(port: int, host: Optional[str] = None) -> int:
    """Resolve the PID listening on TCP *port* using ``netstat -ano`` (Windows).

    Without this, supervised mesh stop/restart falls back to registry PIDs, which
    can be stale or wrong and accidentally terminate another mesh's process.
    """
    if port <= 0:
        return 0
    port_s = str(int(port))
    try:
        run_kwargs: Dict[str, Any] = {
            "capture_output": True,
            "text": True,
            "timeout": 5,
            "check": False,
        }
        if _is_windows_platform():
            startupinfo_cls = getattr(subprocess, "STARTUPINFO", None)
            if startupinfo_cls is not None:
                startupinfo = startupinfo_cls()
                startupinfo.dwFlags |= getattr(subprocess, "STARTF_USESHOWWINDOW", 0x00000001)
                startupinfo.wShowWindow = 0
                run_kwargs["startupinfo"] = startupinfo
            run_kwargs["creationflags"] = _WINDOWS_NO_WINDOW
        result = subprocess.run(["netstat", "-ano"], **run_kwargs)
    except Exception:
        return 0
    for line in (result.stdout or "").splitlines():
        line = line.strip()
        if "LISTENING" not in line:
            continue
        parts = line.split()
        if len(parts) < 5:
            continue
        local_addr = parts[1]
        if not local_addr.endswith(":" + port_s):
            continue
        host_part, _, port_part = local_addr.rpartition(":")
        if not host_part or port_part != port_s:
            continue
        if not _listener_host_matches(host_part, host):
            continue
        pid_str = parts[-1]
        if pid_str.isdigit():
            return int(pid_str)
    return 0


def _ss_listener_pid_for_port(port: int, host: Optional[str] = None) -> int:
    """Resolve the PID listening on TCP *port* using ``ss -tlnp`` on Linux."""
    if port <= 0:
        return 0
    port_s = str(int(port))
    try:
        ss = shutil.which("ss")
        if not ss:
            return 0
        result = subprocess.run(
            [ss, "-tlnp", f"sport = :{port_s}"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except Exception:
        return 0
    for line in (result.stdout or "").splitlines():
        if f":{port_s}" not in line:
            continue
        parts = line.split()
        if len(parts) >= 4:
            local_addr = parts[3]
            local_host, _, local_port = local_addr.rpartition(":")
            if local_port != port_s or not _listener_host_matches(local_host, host):
                continue
        for part in line.split(","):
            part = part.strip()
            if part.startswith("pid="):
                pid_str = part[4:].rstrip(")")
                if pid_str.isdigit():
                    return int(pid_str)
    return 0


def _listener_pid_for_port(port: int, host: Optional[str] = None) -> int:
    """Best-effort lookup for the PID listening on a TCP port and host."""
    if port <= 0:
        return 0
    try:
        lsof = shutil.which("lsof")
        if lsof:
            result = subprocess.run(
                [lsof, "-nP", "-iTCP:%d" % port, "-sTCP:LISTEN", "-Fp", "-Fn"],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
            current_pid = 0
            for line in (result.stdout or "").splitlines():
                line = line.strip()
                if not line:
                    continue
                if line.startswith("p") and line[1:].isdigit():
                    current_pid = int(line[1:])
                    continue
                if not line.startswith("n") or current_pid <= 0:
                    continue
                name = line[1:].strip()
                if name.lower().startswith("tcp "):
                    name = name[4:].strip()
                name = name.replace(" (LISTEN)", "").strip()
                local_host, _, local_port = name.rpartition(":")
                if local_port != str(int(port)):
                    continue
                if _listener_host_matches(local_host, host):
                    return current_pid
    except Exception:
        pass
    if not _is_windows_platform():
        ss_pid = _ss_listener_pid_for_port(port, host=host)
        if ss_pid > 0:
            return ss_pid
    if _is_windows_platform():
        win_pid = _windows_listener_pid_for_port(port, host=host)
        if win_pid > 0:
            return win_pid
    return 0


def _http_json(url: str, timeout: float = 1.0) -> Dict[str, Any]:
    """Fetch a JSON object from *url* and return {} on failure."""
    try:
        with urlopen(url, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
            return payload if isinstance(payload, dict) else {}
    except (URLError, HTTPError, TimeoutError, ValueError):
        return {}
    except Exception:
        return {}


@contextmanager
def _locked_registry_file(registry_root: Optional[Path] = None) -> Iterator[None]:
    lock_path = _registry_lock_path(registry_root)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a+b") as fh:
        try:
            if fcntl is not None:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            elif msvcrt is not None:  # pragma: no cover - windows only
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
        except Exception as exc:  # pragma: no cover - best effort lock
            logger.warning("Meshspace registry lock degraded: %s", exc)
        try:
            yield
        finally:
            try:
                if fcntl is not None:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
                elif msvcrt is not None:  # pragma: no cover - windows only
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            except Exception:
                pass


def _read_registry_file(registry_root: Optional[Path] = None) -> Dict[str, Any]:
    path = _registry_file_path(registry_root)
    if not path.exists():
        return {"version": 1, "meshspaces": []}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("Failed to parse meshspace registry %s: %s", path, exc)
        return {"version": 1, "meshspaces": []}
    if not isinstance(payload, dict):
        return {"version": 1, "meshspaces": []}
    meshspaces = payload.get("meshspaces")
    if not isinstance(meshspaces, list):
        meshspaces = []
    return {
        "version": int(payload.get("version") or 1),
        "meshspaces": meshspaces,
    }


def _write_registry_file(payload: Dict[str, Any], registry_root: Optional[Path] = None) -> None:
    path = _registry_file_path(registry_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix="registry-", suffix=".json.tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
        try:
            os.chmod(path, 0o600)
        except Exception:
            pass
    finally:
        try:
            if os.path.exists(tmp_name):
                os.remove(tmp_name)
        except Exception:
            pass


class MeshspaceRegistryManager:
    """Machine-local meshspace registry manager."""

    def __init__(self, registry_root: Optional[Path] = None) -> None:
        self.registry_root = Path(registry_root or meshspaces_root_dir())
        self.registry_root.mkdir(parents=True, exist_ok=True)
        self._probe_cache: Dict[str, Dict[str, Any]] = {}

    def _path_under_system_temp(self, value: str | Path) -> bool:
        raw = str(value or "").strip()
        if not raw:
            return False
        candidate = _resolved_path(raw)
        temp_root = _resolved_path(Path(tempfile.gettempdir()))
        return candidate == temp_root or temp_root in candidate.parents

    def _record_port_signature(self, record: Dict[str, Any]) -> tuple[int, int, int]:
        return (
            int(record.get("http_port") or 0),
            int(record.get("mesh_port") or 0),
            int(record.get("discovery_port") or 0),
        )

    def _should_prune_registry_record(
        self,
        record: Dict[str, Any],
        *,
        stable_legacy_signatures: Optional[set[tuple[int, int, int]]] = None,
    ) -> bool:
        runtime_mode = str(record.get("runtime_mode") or "").strip().lower()
        if runtime_mode != "legacy-default":
            return False
        data_dir = str(record.get("data_dir") or "").strip()
        if not data_dir or not self._path_under_system_temp(data_dir):
            return False
        signature = self._record_port_signature(record)
        if stable_legacy_signatures and signature in stable_legacy_signatures:
            return True
        status = str(record.get("status") or "").strip().lower()
        return status not in {"running", "starting", "degraded", "stopping"}

    def _prune_registry_payload(self, payload: Dict[str, Any]) -> tuple[Dict[str, Any], bool]:
        meshspaces = [item for item in payload.get("meshspaces", []) if isinstance(item, dict)]
        stable_legacy_signatures = {
            self._record_port_signature(item)
            for item in meshspaces
            if str(item.get("runtime_mode") or "").strip().lower() == "legacy-default"
            and not self._path_under_system_temp(str(item.get("data_dir") or "").strip())
        }
        kept = []
        removed = False
        for item in meshspaces:
            if self._should_prune_registry_record(item, stable_legacy_signatures=stable_legacy_signatures):
                removed = True
                continue
            kept.append(item)
        if not removed:
            return payload, False
        pruned = dict(payload)
        pruned["meshspaces"] = kept
        return pruned, True

    def _avatar_root(self) -> Path:
        path = self.registry_root / "avatars"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _avatar_path_for(self, meshspace_id: str) -> Path:
        return self._avatar_root() / f"{sanitize_meshspace_id(meshspace_id, fallback='meshspace')}.png"

    def _runlog_root(self) -> Path:
        path = self.registry_root.parent / "runlogs"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _runlog_path_for(self, meshspace_id: str) -> Path:
        return self._runlog_root() / f"{sanitize_meshspace_id(meshspace_id, fallback='meshspace')}.out"

    def _schedule_post_spawn_diagnostics(
        self,
        meshspace_id: str,
        *,
        pid: int,
        http_port: int,
        log_path: Path,
        breakaway_used: bool,
    ) -> None:
        """Collect a short delayed health check after mesh child launch."""
        def _check() -> None:
            try:
                time.sleep(2.0)
                alive = _pid_is_alive(pid)
                http_up = _port_accepts_connections("127.0.0.1", http_port, timeout=0.6) if http_port > 0 else False
                if _is_windows_platform():
                    logger.info(
                        "Meshspace spawn check mesh=%s pid=%s breakaway=%s alive=%s http_up=%s log=%s",
                        meshspace_id,
                        pid,
                        breakaway_used,
                        alive,
                        http_up,
                        log_path,
                    )
                else:
                    logger.info(
                        "Meshspace spawn check mesh=%s pid=%s alive=%s http_up=%s log=%s",
                        meshspace_id,
                        pid,
                        alive,
                        http_up,
                        log_path,
                    )
                if not alive and not http_up:
                    record = self.get_meshspace(meshspace_id) or {}
                    if record:
                        record["status"] = "crashed"
                        record["pid"] = 0
                        record["updated_at"] = _utcnow_iso()
                        self._upsert_record(record)
                        logger.warning(
                            "Meshspace %s died shortly after launch (pid=%s, log=%s)",
                            meshspace_id,
                            pid,
                            log_path,
                        )
            except Exception:
                logger.warning("Post-spawn diagnostics failed for meshspace %s", meshspace_id, exc_info=True)

        threading.Thread(target=_check, daemon=True).start()

    def _normalize_record(self, record: Dict[str, Any]) -> Dict[str, Any]:
        meshspace_id = sanitize_meshspace_id(str(record.get("meshspace_id") or ""))
        now = _utcnow_iso()
        data_dir = Path(str(record.get("data_dir") or default_meshspace_root(meshspace_id))).expanduser()
        database_path = Path(str(record.get("database_path") or (data_dir / "canopy.db"))).expanduser()
        status = str(record.get("status") or "defined").strip().lower() or "defined"
        raw_summary = record.get("summary") if isinstance(record.get("summary"), dict) else {}
        base_summary = _default_summary()
        base_summary.update(
            {
                "active_peer_count": max(0, int(raw_summary.get("active_peer_count") or 0)),
                "attention_count": max(0, int(raw_summary.get("attention_count") or 0)),
                "unread_count": max(0, int(raw_summary.get("unread_count") or 0)),
                "mention_count": max(0, int(raw_summary.get("mention_count") or 0)),
                "pending_review_count": max(0, int(raw_summary.get("pending_review_count") or 0)),
                "last_activity_at": str(raw_summary.get("last_activity_at") or "").strip(),
                "last_summary_at": str(raw_summary.get("last_summary_at") or "").strip(),
                "has_recent_activity": bool(raw_summary.get("has_recent_activity")),
                "attention_level": str(raw_summary.get("attention_level") or "quiet").strip() or "quiet",
            }
        )
        normalized = {
            "meshspace_id": meshspace_id,
            "meshspace_id_aliases": _normalize_meshspace_id_aliases(
                record.get("meshspace_id_aliases"),
                current_id=meshspace_id,
            ),
            "name": str(record.get("name") or meshspace_id).strip() or meshspace_id,
            "description": str(record.get("description") or "").strip(),
            "icon": str(record.get("icon") or "bi-grid-1x2").strip() or "bi-grid-1x2",
            "accent": str(record.get("accent") or "emerald").strip() or "emerald",
            "default_agent_permissions": _normalize_agent_permission_values(record.get("default_agent_permissions")),
            "runtime_mode": str(record.get("runtime_mode") or "meshspace").strip() or "meshspace",
            "is_default": bool(record.get("is_default")),
            "supervised": bool(record.get("supervised")),
            "network_quarantined": bool(record.get("network_quarantined")),
            "status": status,
            "http_host": str(record.get("http_host") or "127.0.0.1").strip() or "127.0.0.1",
            "http_port": int(record.get("http_port") or 0),
            "mesh_port": int(record.get("mesh_port") or 0),
            "discovery_port": int(record.get("discovery_port") or 0),
            "data_dir": str(data_dir),
            "database_path": str(database_path),
            "session_cookie_name": str(record.get("session_cookie_name") or meshspace_cookie_name(meshspace_id)),
            "pid": max(0, int(record.get("pid") or 0)),
            "peer_id": str(record.get("peer_id") or "").strip(),
            "last_seen_at": str(record.get("last_seen_at") or now),
            "last_started_at": str(record.get("last_started_at") or record.get("created_at") or now),
            "last_stopped_at": str(record.get("last_stopped_at") or ""),
            "created_at": str(record.get("created_at") or now),
            "updated_at": str(record.get("updated_at") or now),
            "launch_url": str(record.get("launch_url") or ""),
            "launch_log_path": str(record.get("launch_log_path") or "").strip(),
            "avatar_path": str(record.get("avatar_path") or "").strip(),
            "avatar_mime": str(record.get("avatar_mime") or "").strip() or "image/png",
            "avatar_updated_at": str(record.get("avatar_updated_at") or ""),
            "summary": base_summary,
        }
        return normalized

    def list_meshspaces(self) -> List[Dict[str, Any]]:
        with _locked_registry_file(self.registry_root):
            payload = _read_registry_file(self.registry_root)
            payload, changed = self._prune_registry_payload(payload)
            if changed:
                _write_registry_file(payload, self.registry_root)
        records = [self._normalize_record(cast) for cast in payload.get("meshspaces", []) if isinstance(cast, dict)]
        records.sort(key=lambda rec: (not rec.get("is_default"), str(rec.get("name") or "").lower()))
        return records

    def get_meshspace(self, meshspace_id: str) -> Optional[Dict[str, Any]]:
        normalized_id = sanitize_meshspace_id(meshspace_id)
        for record in self.list_meshspaces():
            if record.get("meshspace_id") == normalized_id:
                return record
        return None

    def _upsert_record(self, record: Dict[str, Any]) -> Dict[str, Any]:
        normalized = self._normalize_record(record)
        with _locked_registry_file(self.registry_root):
            payload = _read_registry_file(self.registry_root)
            payload, _ = self._prune_registry_payload(payload)
            meshspaces = [item for item in payload.get("meshspaces", []) if isinstance(item, dict)]
            updated = []
            found = False
            for item in meshspaces:
                if sanitize_meshspace_id(str(item.get("meshspace_id") or "")) == normalized["meshspace_id"]:
                    merged = dict(item)
                    merged.update(normalized)
                    merged["updated_at"] = _utcnow_iso()
                    updated.append(merged)
                    found = True
                else:
                    updated.append(item)
            if not found:
                normalized["created_at"] = normalized.get("created_at") or _utcnow_iso()
                normalized["updated_at"] = _utcnow_iso()
                updated.append(normalized)
            payload["meshspaces"] = updated
            _write_registry_file(payload, self.registry_root)
        self._probe_cache.pop(normalized["meshspace_id"], None)
        return self.get_meshspace(normalized["meshspace_id"]) or normalized

    def _replace_record(self, old_meshspace_id: str, new_record: Dict[str, Any]) -> Dict[str, Any]:
        normalized_old = sanitize_meshspace_id(old_meshspace_id, fallback="")
        normalized = self._normalize_record(new_record)
        with _locked_registry_file(self.registry_root):
            payload = _read_registry_file(self.registry_root)
            payload, _ = self._prune_registry_payload(payload)
            updated: List[Dict[str, Any]] = []
            for item in payload.get("meshspaces", []):
                if not isinstance(item, dict):
                    continue
                item_id = sanitize_meshspace_id(str(item.get("meshspace_id") or ""), fallback="")
                if item_id in {normalized_old, normalized["meshspace_id"]}:
                    continue
                updated.append(item)
            normalized["updated_at"] = _utcnow_iso()
            normalized["created_at"] = str(normalized.get("created_at") or _utcnow_iso())
            updated.append(normalized)
            payload["meshspaces"] = updated
            _write_registry_file(payload, self.registry_root)
        self._probe_cache.pop(normalized_old, None)
        self._probe_cache.pop(normalized["meshspace_id"], None)
        return self.get_meshspace(normalized["meshspace_id"]) or normalized

    def _validate_mesh_root(
        self,
        mesh_root: str | Path,
        *,
        exclude_meshspace_id: Optional[str] = None,
    ) -> None:
        """Reject roots that would overlap another meshspace's storage."""
        target_root = _resolved_path(mesh_root)
        normalized_exclude = sanitize_meshspace_id(exclude_meshspace_id or "") if exclude_meshspace_id else None
        for record in self.list_meshspaces():
            existing_id = sanitize_meshspace_id(str(record.get("meshspace_id") or ""))
            if normalized_exclude and existing_id == normalized_exclude:
                continue
            existing_root_raw = str(record.get("data_dir") or "").strip()
            if not existing_root_raw:
                continue
            existing_root = _resolved_path(existing_root_raw)
            if _paths_overlap(target_root, existing_root):
                raise ValueError(
                    f"Meshspace root '{target_root}' overlaps existing meshspace '{existing_id}'"
                )

    def _port_block_from_http_port(self, http_port: Any) -> Dict[str, int]:
        """Return the derived 3-port runtime block for a base HTTP port."""
        try:
            base = int(str(http_port).strip())
        except Exception as exc:
            raise ValueError("Base HTTP port must be a number") from exc
        if base < 1024 or base > 65533:
            raise ValueError("Base HTTP port must be between 1024 and 65533")
        return {
            "http_port": base,
            "mesh_port": base + 1,
            "discovery_port": base + 2,
        }

    def _probe_host_for_record(self, record: Dict[str, Any]) -> str:
        host = str(record.get("http_host") or "127.0.0.1").strip() or "127.0.0.1"
        return "127.0.0.1" if host in {"0.0.0.0", "::"} else host

    def _listener_filter_host_for_record(self, record: Dict[str, Any]) -> Optional[str]:
        host = str(record.get("http_host") or "127.0.0.1").strip() or "127.0.0.1"
        return None if host in {"0.0.0.0", "::"} else host

    def _launch_url_for_record(self, record: Dict[str, Any], *, http_port: Optional[int] = None) -> str:
        host = self._probe_host_for_record(record)
        port = int(http_port if http_port is not None else (record.get("http_port") or 0))
        return f"http://{host}:{port}" if port > 0 else ""

    def _validate_port_block_registry_ownership(
        self,
        meshspace_id: str,
        block: Dict[str, int],
    ) -> None:
        normalized_id = sanitize_meshspace_id(meshspace_id, fallback="")
        requested_ports = {
            int(block["http_port"]),
            int(block["mesh_port"]),
            int(block["discovery_port"]),
        }
        for record in self.list_meshspaces():
            other_id = sanitize_meshspace_id(str(record.get("meshspace_id") or ""), fallback="")
            if not other_id or other_id == normalized_id:
                continue
            other_ports = {
                int(record.get("http_port") or 0),
                int(record.get("mesh_port") or 0),
                int(record.get("discovery_port") or 0),
            }
            overlaps = sorted(port for port in (requested_ports & other_ports) if port > 0)
            if overlaps:
                other_name = str(record.get("name") or other_id).strip() or other_id
                raise ValueError(
                    f"Port block conflicts with meshspace '{other_name}' "
                    f"({', '.join(str(port) for port in overlaps)})"
                )

    def _validate_port_block_listener_availability(
        self,
        record: Dict[str, Any],
        block: Dict[str, int],
    ) -> None:
        listener_host = self._listener_filter_host_for_record(record)
        display_host = listener_host or str(record.get("http_host") or "all interfaces").strip() or "all interfaces"
        occupied: List[str] = []
        for label, port in (
            ("HTTP", int(block["http_port"])),
            ("mesh", int(block["mesh_port"])),
            ("discovery", int(block["discovery_port"])),
        ):
            pid = _listener_pid_for_port(port, host=listener_host)
            if pid > 0:
                occupied.append(f"{label}:{port} pid:{pid}")
        if occupied:
            raise ValueError(
                "Cannot assign that port block because a listener is already active on "
                f"{display_host} ({', '.join(occupied)})"
            )

    def update_meshspace_ports(self, meshspace_id: str, *, http_port: Any) -> Optional[Dict[str, Any]]:
        """Change a stopped meshspace to a new derived HTTP/mesh/discovery port block."""
        record = self.get_meshspace(meshspace_id)
        if not record:
            return None

        current = self.reconcile_meshspace_runtime(meshspace_id) or record
        probe = self.probe_meshspace_runtime(meshspace_id)
        if probe.get("live"):
            raise ValueError("Stop this meshspace before changing its ports")

        status = str(probe.get("effective_status") or current.get("status") or "").strip().lower()
        if status not in {"defined", "stopped", "crashed", "degraded"}:
            raise ValueError(f"Ports can only be changed while the meshspace is stopped (current state: {status or 'unknown'})")

        block = self._port_block_from_http_port(http_port)
        existing_block = self._port_block_from_http_port(int(current.get("http_port") or 0)) if int(current.get("http_port") or 0) else {}
        if existing_block and all(int(current.get(key) or 0) == int(block[key]) for key in ("http_port", "mesh_port", "discovery_port")):
            current["launch_url"] = self._launch_url_for_record(current, http_port=block["http_port"])
            current["updated_at"] = _utcnow_iso()
            return self._upsert_record(current)

        self._validate_port_block_registry_ownership(meshspace_id, block)
        self._validate_port_block_listener_availability(current, block)

        updated = dict(current)
        updated.update(block)
        updated["launch_url"] = self._launch_url_for_record(updated, http_port=block["http_port"])
        updated["pid"] = 0
        updated["status"] = status if status in {"defined", "crashed", "degraded"} else "stopped"
        updated["updated_at"] = _utcnow_iso()
        return self._upsert_record(updated)

    def ensure_runtime_registered(self, config: Any, peer_id: Optional[str] = None) -> Dict[str, Any]:
        mesh_cfg = getattr(config, "meshspace", None)
        meshspace_id = sanitize_meshspace_id(str(getattr(mesh_cfg, "meshspace_id", "") or "default"))
        data_dir = str(getattr(config.storage, "data_dir", "") or default_meshspace_root(meshspace_id))
        self._validate_mesh_root(data_dir, exclude_meshspace_id=meshspace_id)
        existing = self.get_meshspace(meshspace_id) or {}
        runtime_mode = str(getattr(mesh_cfg, "runtime_mode", "") or existing.get("runtime_mode") or "meshspace")
        configured_name = str(getattr(mesh_cfg, "name", "") or "").strip()
        if existing and runtime_mode == "legacy-default" and configured_name.lower() in {"", "default mesh"}:
            name = str(existing.get("name") or configured_name or meshspace_id).strip() or meshspace_id
        else:
            name = configured_name or str(existing.get("name") or meshspace_id).strip() or meshspace_id
        now = _utcnow_iso()
        record = dict(existing)
        record.update(
            {
                "meshspace_id": meshspace_id,
                "name": name,
                "description": str(getattr(mesh_cfg, "description", "") or existing.get("description") or ""),
                "icon": str(existing.get("icon") or "bi-grid-1x2"),
                "accent": str(existing.get("accent") or "emerald"),
                "runtime_mode": runtime_mode,
                "is_default": bool(getattr(mesh_cfg, "is_default", False)),
                "supervised": bool(getattr(mesh_cfg, "supervised", False)),
                "network_quarantined": bool(
                    getattr(mesh_cfg, "network_quarantined", False)
                    or existing.get("network_quarantined")
                ),
                "http_host": str(getattr(config.network, "host", "") or "127.0.0.1"),
                "http_port": int(getattr(config.network, "port", 0) or 0),
                "mesh_port": int(getattr(config.network, "mesh_port", 0) or 0),
                "discovery_port": int(getattr(config.network, "discovery_port", 0) or 0),
                "data_dir": data_dir,
                "database_path": str(getattr(config.storage, "database_path", "") or (Path(data_dir) / "canopy.db")),
                "session_cookie_name": meshspace_cookie_name(meshspace_id),
                "peer_id": str(peer_id or "").strip(),
                "status": "running",
                "last_seen_at": now,
                "last_started_at": str(existing.get("last_started_at") or now),
                "launch_url": self._launch_url_for(config),
                "pid": int(existing.get("pid") or 0),
                "launch_log_path": str(existing.get("launch_log_path") or "").strip(),
                "created_at": str(existing.get("created_at") or now),
            }
        )
        return self._upsert_record(record)

    def update_runtime_state(
        self,
        meshspace_id: str,
        state: str,
        *,
        peer_id: Optional[str] = None,
        launch_url: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        record = self.get_meshspace(meshspace_id)
        if not record:
            return None
        record["status"] = str(state or record.get("status") or "defined")
        record["last_seen_at"] = _utcnow_iso()
        if peer_id is not None:
            record["peer_id"] = str(peer_id or "").strip()
        if launch_url:
            record["launch_url"] = str(launch_url)
        if record["status"] == "running":
            record["last_started_at"] = _utcnow_iso()
        if record["status"] in {"stopped", "crashed"}:
            record["last_stopped_at"] = _utcnow_iso()
            record["pid"] = 0
            summary = record.get("summary") if isinstance(record.get("summary"), dict) else _default_summary()
            summary = dict(summary)
            summary["active_peer_count"] = 0
            summary["has_recent_activity"] = False
            summary["attention_level"] = _summary_attention_level(summary, status=record["status"])
            record["summary"] = summary
        return self._upsert_record(record)

    def update_shell_summary(
        self,
        meshspace_id: str,
        summary: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        record = self.get_meshspace(meshspace_id)
        if not record:
            return None
        merged = _default_summary()
        if isinstance(record.get("summary"), dict):
            merged.update(record.get("summary") or {})
        if isinstance(summary, dict):
            merged.update(summary)
        merged["active_peer_count"] = max(0, int(merged.get("active_peer_count") or 0))
        merged["attention_count"] = max(0, int(merged.get("attention_count") or 0))
        merged["unread_count"] = max(0, int(merged.get("unread_count") or 0))
        merged["mention_count"] = max(0, int(merged.get("mention_count") or 0))
        merged["pending_review_count"] = max(0, int(merged.get("pending_review_count") or 0))
        merged["last_activity_at"] = str(merged.get("last_activity_at") or "").strip()
        merged["last_summary_at"] = _utcnow_iso()
        merged["has_recent_activity"] = bool(merged.get("has_recent_activity"))
        merged["attention_level"] = _summary_attention_level(merged, status=str(record.get("status") or ""))
        record["summary"] = merged
        record["updated_at"] = _utcnow_iso()
        return self._upsert_record(record)

    def set_meshspace_avatar(
        self,
        meshspace_id: str,
        avatar_bytes: bytes,
        *,
        filename: str = "",
        content_type: str = "",
    ) -> Optional[Dict[str, Any]]:
        record = self.get_meshspace(meshspace_id)
        if not record:
            return None
        payload = bytes(avatar_bytes or b"")
        if not payload:
            raise ValueError("No avatar file provided")
        if len(payload) > _MESHSPACE_AVATAR_MAX_BYTES:
            raise ValueError("Avatar image must be 5 MB or smaller")
        if content_type and not str(content_type).startswith("image/"):
            raise ValueError("Mesh avatar must be an image")
        try:
            from PIL import Image
        except Exception as exc:  # pragma: no cover - dependency exists in app env
            raise ValueError("Pillow is required for mesh avatar processing") from exc
        resampling = getattr(getattr(Image, "Resampling", Image), "LANCZOS", getattr(Image, "LANCZOS", 1))
        avatar_path = self._avatar_path_for(meshspace_id)
        tmp_path = avatar_path.with_suffix(".tmp")
        try:
            from io import BytesIO

            image = Image.open(BytesIO(payload))
            image.load()
            if image.mode not in ("RGB", "RGBA"):
                image = image.convert("RGBA" if "A" in image.getbands() else "RGB")
            image.thumbnail((_MESHSPACE_AVATAR_MAX_DIMENSION, _MESHSPACE_AVATAR_MAX_DIMENSION), resampling)
            if image.mode == "RGBA":
                image.save(tmp_path, format="PNG", optimize=True)
            else:
                image.save(tmp_path, format="PNG", optimize=True)
        except Exception as exc:
            raise ValueError("Failed to process mesh avatar image") from exc
        try:
            os.replace(tmp_path, avatar_path)
            os.chmod(avatar_path, 0o600)
        finally:
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
            except Exception:
                pass
        record["avatar_path"] = str(avatar_path)
        record["avatar_mime"] = "image/png"
        record["avatar_updated_at"] = _utcnow_iso()
        return self._upsert_record(record)

    def clear_meshspace_avatar(self, meshspace_id: str) -> Optional[Dict[str, Any]]:
        record = self.get_meshspace(meshspace_id)
        if not record:
            return None
        avatar_path = Path(str(record.get("avatar_path") or "").strip()) if record.get("avatar_path") else self._avatar_path_for(meshspace_id)
        try:
            if avatar_path.exists():
                avatar_path.unlink()
        except Exception:
            logger.warning("Failed to remove meshspace avatar %s", avatar_path, exc_info=True)
        record["avatar_path"] = ""
        record["avatar_updated_at"] = ""
        return self._upsert_record(record)

    def get_meshspace_avatar_path(self, meshspace_id: str) -> Optional[Path]:
        record = self.get_meshspace(meshspace_id)
        if not record:
            return None
        raw = str(record.get("avatar_path") or "").strip()
        if not raw:
            candidate = self._avatar_path_for(meshspace_id)
            return candidate if candidate.exists() else None
        candidate = Path(raw).expanduser()
        return candidate if candidate.exists() else None

    def get_meshspace_avatar_preview(
        self,
        meshspace_id: str,
        *,
        max_dimension: int = _MESHSPACE_AVATAR_PREVIEW_DIMENSION,
        max_bytes: int = _MESHSPACE_AVATAR_PREVIEW_MAX_BYTES,
    ) -> Dict[str, str]:
        """Return a compact base64 PNG preview for invite/handshake hints."""
        avatar_path = self.get_meshspace_avatar_path(meshspace_id)
        if not avatar_path or not avatar_path.exists():
            return {}
        try:
            from PIL import Image
        except Exception:
            return {}
        resampling = getattr(getattr(Image, "Resampling", Image), "LANCZOS", getattr(Image, "LANCZOS", 1))
        try:
            with Image.open(avatar_path) as opened_image:
                opened_image.load()
                if opened_image.mode not in ("RGB", "RGBA"):
                    image = opened_image.convert("RGBA" if "A" in opened_image.getbands() else "RGB")
                else:
                    image = opened_image.copy()
            image.thumbnail((max_dimension, max_dimension), resampling)
            buffer = BytesIO()
            image.save(buffer, format="PNG", optimize=True)
            payload = buffer.getvalue()
        except Exception:
            return {}
        if not payload or len(payload) > max_bytes:
            return {}
        return {
            "avatar_b64": base64.b64encode(payload).decode("ascii"),
            "avatar_mime": "image/png",
        }

    def create_meshspace(
        self,
        *,
        name: str,
        description: str = "",
        icon: str = "bi-grid-1x2",
        accent: str = "emerald",
        meshspace_id: Optional[str] = None,
        data_dir: Optional[str] = None,
        http_port: Optional[int] = None,
        mesh_port: Optional[int] = None,
        discovery_port: Optional[int] = None,
    ) -> Dict[str, Any]:
        normalized_id = sanitize_meshspace_id(meshspace_id or name, fallback="meshspace")
        if self.get_meshspace(normalized_id):
            raise ValueError(f"Meshspace '{normalized_id}' already exists")
        allocated = self.allocate_port_block()
        mesh_root = Path(data_dir).expanduser() if data_dir else default_meshspace_root(normalized_id)
        self._validate_mesh_root(mesh_root)
        mesh_root.mkdir(parents=True, exist_ok=True)
        record = {
            "meshspace_id": normalized_id,
            "name": str(name or normalized_id).strip() or normalized_id,
            "description": str(description or "").strip(),
            "icon": icon,
            "accent": accent,
            "runtime_mode": "meshspace",
            "is_default": False,
            "supervised": True,
            "network_quarantined": True,
            "status": "defined",
            "http_host": "127.0.0.1",
            "http_port": int(http_port or allocated["http_port"]),
            "mesh_port": int(mesh_port or allocated["mesh_port"]),
            "discovery_port": int(discovery_port or allocated["discovery_port"]),
            "data_dir": str(mesh_root),
            "database_path": str(mesh_root / "canopy.db"),
            "session_cookie_name": meshspace_cookie_name(normalized_id),
            "created_at": _utcnow_iso(),
            "updated_at": _utcnow_iso(),
        }
        return self._upsert_record(record)

    def update_meshspace_metadata(
        self,
        meshspace_id: str,
        *,
        name: Optional[str] = None,
        description: Optional[str] = None,
        icon: Optional[str] = None,
        accent: Optional[str] = None,
        default_agent_permissions: Optional[List[str]] = None,
    ) -> Optional[Dict[str, Any]]:
        record = self.get_meshspace(meshspace_id)
        if not record:
            return None
        if name is not None:
            cleaned_name = str(name or '').strip()
            if len(cleaned_name) < 2:
                raise ValueError("Mesh name must be at least 2 characters")
            record["name"] = cleaned_name
        if description is not None:
            record["description"] = str(description or '').strip()
        if icon is not None:
            record["icon"] = str(icon or 'bi-grid-1x2').strip() or 'bi-grid-1x2'
        if accent is not None:
            record["accent"] = str(accent or 'emerald').strip() or 'emerald'
        if default_agent_permissions is not None:
            record["default_agent_permissions"] = _normalize_agent_permission_values(default_agent_permissions)
        return self._upsert_record(record)

    def add_meshspace_aliases(self, meshspace_id: str, aliases: List[str]) -> Optional[Dict[str, Any]]:
        """Persist additional admin-approved compatibility aliases for a meshspace."""
        record = self.get_meshspace(meshspace_id)
        if not record:
            return None
        current_id = str(record.get("meshspace_id") or meshspace_id or "").strip()
        merged_aliases = _normalize_meshspace_id_aliases(
            list(record.get("meshspace_id_aliases") or []) + list(aliases or []),
            current_id=current_id,
        )
        if merged_aliases == list(record.get("meshspace_id_aliases") or []):
            return record
        updated = dict(record)
        updated["meshspace_id_aliases"] = merged_aliases
        return self._upsert_record(updated)

    def promote_legacy_meshspace(
        self,
        meshspace_id: str,
        *,
        new_meshspace_id: str,
        new_name: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Convert a legacy-default runtime record into a stable explicit mesh identity."""
        record = self.get_meshspace(meshspace_id)
        if not record:
            return None
        runtime_mode = str(record.get("runtime_mode") or "").strip().lower()
        if runtime_mode != "legacy-default":
            raise ValueError("Only legacy default meshes can be promoted to a stable mesh ID")
        old_meshspace_id = str(record.get("meshspace_id") or "").strip()
        normalized_new_id = sanitize_meshspace_id(new_meshspace_id, fallback="")
        if len(normalized_new_id) < 2:
            raise ValueError("Mesh ID must contain at least 2 letters or numbers")
        existing = self.get_meshspace(normalized_new_id)
        if existing and str(existing.get("meshspace_id") or "").strip() != old_meshspace_id:
            raise ValueError(f"Meshspace '{normalized_new_id}' already exists")

        avatar_path_raw = str(record.get("avatar_path") or "").strip()
        old_avatar_path = Path(avatar_path_raw).expanduser() if avatar_path_raw else self._avatar_path_for(old_meshspace_id)
        avatar_path = str(old_avatar_path)
        if old_avatar_path.exists():
            new_avatar_path = self._avatar_path_for(normalized_new_id)
            try:
                if old_avatar_path != new_avatar_path:
                    new_avatar_path.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(old_avatar_path, new_avatar_path)
                avatar_path = str(new_avatar_path)
            except Exception:
                avatar_path = str(old_avatar_path)

        promoted = dict(record)
        promoted["meshspace_id"] = normalized_new_id
        promoted["meshspace_id_aliases"] = _normalize_meshspace_id_aliases(
            list(record.get("meshspace_id_aliases") or []) + [old_meshspace_id],
            current_id=normalized_new_id,
        )
        if new_name is not None:
            cleaned_name = str(new_name or "").strip()
            if len(cleaned_name) < 2:
                raise ValueError("Mesh name must be at least 2 characters")
            promoted["name"] = cleaned_name
        promoted["runtime_mode"] = "meshspace"
        promoted["is_default"] = False
        promoted["session_cookie_name"] = meshspace_cookie_name(normalized_new_id)
        promoted["avatar_path"] = avatar_path if avatar_path and Path(avatar_path).expanduser().exists() else str(record.get("avatar_path") or "")
        promoted["updated_at"] = _utcnow_iso()
        return self._replace_record(old_meshspace_id, promoted)

    def allocate_port_block(self) -> Dict[str, int]:
        records = self.list_meshspaces()
        used_ports = set()
        for record in records:
            for key in ("http_port", "mesh_port", "discovery_port"):
                try:
                    used_ports.add(int(record.get(key) or 0))
                except Exception:
                    continue
        offset = 0
        while True:
            http_port = _PORT_BLOCK_BASE + offset
            mesh_port = http_port + 1
            discovery_port = http_port + 2
            block = {http_port, mesh_port, discovery_port}
            if not (block & used_ports):
                return {
                    "http_port": http_port,
                    "mesh_port": mesh_port,
                    "discovery_port": discovery_port,
                }
            offset += _PORT_BLOCK_SIZE

    def launch_environment(self, meshspace_id: str) -> Optional[Dict[str, str]]:
        record = self.get_meshspace(meshspace_id)
        if not record:
            return None
        env = {
            "CANOPY_MESHSPACE_ID": str(record["meshspace_id"]),
            "CANOPY_MESHSPACE_NAME": str(record["name"]),
            "CANOPY_MESHSPACE_ROOT": str(record["data_dir"]),
            "CANOPY_MESHSPACE_SUPERVISED": "true" if record.get("supervised") else "false",
            "CANOPY_MESHSPACE_NETWORK_QUARANTINED": "true" if record.get("network_quarantined") else "false",
            "CANOPY_PORT": str(record["http_port"]),
            "CANOPY_MESH_PORT": str(record["mesh_port"]),
            "CANOPY_DISCOVERY_PORT": str(record["discovery_port"]),
        }
        for key in (
            "CANOPY_ENABLE_TLS",
            "CANOPY_TLS_CERT_PATH",
            "CANOPY_TLS_KEY_PATH",
            "CANOPY_REQUIRE_VERIFIED_WSS",
            "CANOPY_EXTERNAL_TLS_ENDPOINT",
            "CANOPY_TRANSPORT_SECURITY_MODE",
        ):
            value = str(os.environ.get(key) or "").strip()
            if value:
                env[key] = value
        return env

    def _spawn_meshspace_process(self, meshspace_id: str) -> Dict[str, Any]:
        record = self.get_meshspace(meshspace_id)
        if not record:
            raise ValueError(f"Meshspace '{meshspace_id}' not found")
        env = self.launch_environment(meshspace_id)
        if not env:
            raise ValueError(f"Cannot build launch environment for '{meshspace_id}'")

        child_env = _sanitized_runtime_environment(dict(os.environ), env)

        python = _windows_preferred_python_executable(os.sys.executable or "python3")
        log_path = self._runlog_path_for(meshspace_id)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        breakaway_used = False
        with open(log_path, "ab") as log_fh:
            popen_kwargs: Dict[str, Any] = {
                "env": child_env,
                "stdin": subprocess.DEVNULL,
                "stdout": log_fh,
                "stderr": subprocess.STDOUT,
            }
            if _is_windows_platform():
                popen_kwargs["creationflags"] = _windows_creation_flags(include_breakaway=True)
                try:
                    proc = subprocess.Popen([python, "-m", "canopy.main"], **popen_kwargs)
                    breakaway_used = True
                except OSError:
                    popen_kwargs["creationflags"] = _windows_creation_flags(include_breakaway=False)
                    proc = subprocess.Popen([python, "-m", "canopy.main"], **popen_kwargs)
            else:
                popen_kwargs["start_new_session"] = True
                proc = subprocess.Popen([python, "-m", "canopy.main"], **popen_kwargs)
        record["status"] = "starting"
        record["pid"] = proc.pid
        record["launch_log_path"] = str(log_path)
        record["updated_at"] = _utcnow_iso()
        self._upsert_record(record)
        logger.info("Started meshspace %s as PID %d (log: %s)", meshspace_id, proc.pid, log_path)
        self._schedule_post_spawn_diagnostics(
            meshspace_id,
            pid=int(proc.pid),
            http_port=int(record.get("http_port") or 0),
            log_path=log_path,
            breakaway_used=breakaway_used,
        )
        return record

    def probe_meshspace_runtime(self, meshspace_id: str) -> Dict[str, Any]:
        """Inspect whether a meshspace runtime appears live right now."""
        cached = self._probe_cache.get(meshspace_id)
        if cached and (time.time() - float(cached.get("at") or 0)) < _PROBE_CACHE_TTL:
            return dict(cached.get("value") or {})
        record = self.get_meshspace(meshspace_id)
        if not record:
            raise ValueError(f"Meshspace '{meshspace_id}' not found")

        host = str(record.get("http_host") or "127.0.0.1").strip() or "127.0.0.1"
        probe_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
        http_port = int(record.get("http_port") or 0)
        mesh_port = int(record.get("mesh_port") or 0)
        registry_pid = int(record.get("pid") or 0)
        registry_pid_alive = _pid_is_alive(registry_pid) if registry_pid > 0 else False
        detected_pid = _listener_pid_for_port(http_port, host=probe_host) or _listener_pid_for_port(mesh_port, host=probe_host)
        http_listening = _port_accepts_connections(probe_host, http_port) if http_port > 0 else False
        health = _http_json(f"http://{probe_host}:{http_port}/api/v1/health", timeout=0.9) if http_port > 0 else {}
        health_meshspace = (health.get("meshspace") or {}) if isinstance(health.get("meshspace"), dict) else {}
        health_meshspace_id = str(health_meshspace.get("meshspace_id") or "").strip()
        health_meshspace_match = not health_meshspace_id or health_meshspace_id == meshspace_id
        wrong_meshspace_detected = bool(health_meshspace_id) and health_meshspace_id != meshspace_id
        health_ok = health_meshspace_match and bool(health) and (
            bool(health.get("ready"))
            or str(health.get("status") or "").strip().lower() in {"healthy", "ready"}
        )
        registry_status = str(record.get("status") or "defined").strip().lower() or "defined"
        registry_pid_counts_as_live = bool(registry_pid_alive and registry_status == "starting")
        live = bool((registry_pid_counts_as_live or detected_pid > 0 or http_listening or health_ok) and not wrong_meshspace_detected)
        effective_status = registry_status
        if live:
            if health_ok or http_listening:
                effective_status = "running" if registry_status != "degraded" else "degraded"
            elif registry_status in {"defined", "stopped", "crashed"}:
                effective_status = "starting"
        elif registry_status in {"running", "starting", "degraded", "stopping"}:
            effective_status = "stopped"
        shell_summary = {}
        if live and http_port > 0:
            shell_summary = _http_json(
                f"http://{probe_host}:{http_port}/api/v1/meshspace/shell_summary",
                timeout=0.9,
            )
        result = {
            "live": live,
            "registry_status": registry_status,
            "effective_status": effective_status,
            "status_mismatch": effective_status != registry_status,
            "registry_pid": registry_pid,
            "registry_pid_alive": registry_pid_alive,
            "detected_pid": int(detected_pid or 0),
            "http_listening": http_listening,
            "health_ok": health_ok,
            "health_meshspace_id": health_meshspace_id,
            "health_meshspace_match": health_meshspace_match,
            "wrong_meshspace_detected": wrong_meshspace_detected,
            "version": str(health.get("version") or "").strip(),
            "peer_id": str(((health.get("peer") or {}) if isinstance(health.get("peer"), dict) else {}).get("peer_id") or "").strip(),
            "launch_url": f"http://{probe_host}:{http_port}" if http_port > 0 else str(record.get("launch_url") or ""),
            "shell_summary": shell_summary if isinstance(shell_summary, dict) else {},
        }
        self._probe_cache[meshspace_id] = {"at": time.time(), "value": dict(result)}
        return result

    def reconcile_meshspace_runtime(self, meshspace_id: str) -> Optional[Dict[str, Any]]:
        """Update registry state to reflect the currently observed runtime."""
        record = self.get_meshspace(meshspace_id)
        if not record:
            return None
        probe = self.probe_meshspace_runtime(meshspace_id)
        changed = False
        detected_pid = int(probe.get("detected_pid") or 0)
        effective_status = str(probe.get("effective_status") or record.get("status") or "defined")
        launch_url = str(probe.get("launch_url") or record.get("launch_url") or "").strip()

        if str(record.get("status") or "") != effective_status:
            record["status"] = effective_status
            changed = True
        if launch_url and str(record.get("launch_url") or "").strip() != launch_url:
            record["launch_url"] = launch_url
            changed = True
        if probe.get("live"):
            if detected_pid and int(record.get("pid") or 0) != detected_pid:
                record["pid"] = detected_pid
                changed = True
            if probe.get("peer_id") and str(record.get("peer_id") or "").strip() != str(probe.get("peer_id") or "").strip():
                record["peer_id"] = str(probe.get("peer_id") or "").strip()
                changed = True
            record["last_seen_at"] = _utcnow_iso()
        else:
            if int(record.get("pid") or 0) != 0:
                record["pid"] = 0
                changed = True
            if str(record.get("status") or "").strip().lower() != "stopped":
                record["status"] = "stopped"
                changed = True
            summary = record.get("summary") if isinstance(record.get("summary"), dict) else _default_summary()
            summary = dict(summary)
            summary["active_peer_count"] = 0
            summary["has_recent_activity"] = False
            summary["attention_level"] = _summary_attention_level(summary, status="stopped")
            record["summary"] = summary
            record["last_stopped_at"] = _utcnow_iso()

        if not changed:
            return record
        record["updated_at"] = _utcnow_iso()
        return self._upsert_record(record)

    def set_network_quarantine(self, meshspace_id: str, quarantined: bool) -> Optional[Dict[str, Any]]:
        """Enable or disable network quarantine for a meshspace."""
        record = self.get_meshspace(meshspace_id)
        if not record:
            return None
        record["network_quarantined"] = bool(quarantined)
        record["updated_at"] = _utcnow_iso()
        return self._upsert_record(record)

    def start_meshspace(self, meshspace_id: str) -> Dict[str, Any]:
        """Spawn a child Canopy process for the given meshspace.

        Returns the updated registry record with status='starting' and the
        child PID.  Raises ValueError for meshspaces that are already running
        or are the current (parent) runtime.
        """
        record = self.get_meshspace(meshspace_id)
        if not record:
            raise ValueError(f"Meshspace '{meshspace_id}' not found")
        record = self.reconcile_meshspace_runtime(meshspace_id) or record
        probe = self.probe_meshspace_runtime(meshspace_id)
        if probe.get("live"):
            live_pid = int(probe.get("detected_pid") or record.get("pid") or 0)
            if live_pid > 0:
                raise ValueError(f"Meshspace '{meshspace_id}' is already running (PID {live_pid})")
            raise ValueError(f"Meshspace '{meshspace_id}' is already responding on port {record.get('http_port')}")
        # Extra guard: even when live probe is transiently false, never spawn if
        # either assigned runtime port is already occupied.
        http_port = int(record.get("http_port") or 0)
        mesh_port = int(record.get("mesh_port") or 0)
        probe_host = "127.0.0.1" if str(record.get("http_host") or "").strip() in {"0.0.0.0", "::"} else str(record.get("http_host") or "127.0.0.1").strip() or "127.0.0.1"
        http_listener_pid = _listener_pid_for_port(http_port, host=probe_host) if http_port > 0 else 0
        mesh_listener_pid = _listener_pid_for_port(mesh_port, host=probe_host) if mesh_port > 0 else 0
        if http_listener_pid > 0 or mesh_listener_pid > 0:
            in_use = []
            if http_listener_pid > 0:
                in_use.append(f"http:{http_port} pid:{http_listener_pid}")
            if mesh_listener_pid > 0:
                in_use.append(f"mesh:{mesh_port} pid:{mesh_listener_pid}")
            raise ValueError(
                f"Meshspace '{meshspace_id}' cannot start because assigned ports are already in use "
                f"({', '.join(in_use)}). Refresh state or stop the existing runtime first."
            )
        return self._spawn_meshspace_process(meshspace_id)

    def stop_meshspace(self, meshspace_id: str) -> Dict[str, Any]:
        """Send SIGTERM to a running meshspace child process.

        Returns the updated registry record.  Raises ValueError if the
        meshspace is not running or the PID is stale.
        """
        import signal

        record = self.get_meshspace(meshspace_id)
        if not record:
            raise ValueError(f"Meshspace '{meshspace_id}' not found")
        record = self.reconcile_meshspace_runtime(meshspace_id) or record
        probe = self.probe_meshspace_runtime(meshspace_id)
        pid = int(probe.get("detected_pid") or record.get("pid") or 0)
        if not probe.get("live"):
            record["status"] = "stopped"
            record["pid"] = 0
            record["updated_at"] = _utcnow_iso()
            self._upsert_record(record)
            raise ValueError("Meshspace is not running (no live PID)")
        if pid <= 0:
            raise ValueError(
                f"Meshspace '{meshspace_id}' is live on port {record.get('http_port')} but no process ID was detected"
            )

        def _converge_stopped_after_signal_failure() -> Optional[Dict[str, Any]]:
            refreshed = self.reconcile_meshspace_runtime(meshspace_id) or self.get_meshspace(meshspace_id) or record
            probe_after = self.probe_meshspace_runtime(meshspace_id)
            if not probe_after.get("live"):
                refreshed["status"] = "stopped"
                refreshed["pid"] = 0
                refreshed["updated_at"] = _utcnow_iso()
                return self._upsert_record(refreshed)
            return None

        signal_error: Optional[Exception] = None
        try:
            if _is_windows_platform():
                ctrl_break = getattr(signal, "CTRL_BREAK_EVENT", None)
                if ctrl_break is not None:
                    try:
                        os.kill(pid, ctrl_break)
                    except Exception:
                        os.kill(pid, signal.SIGTERM)
                else:
                    os.kill(pid, signal.SIGTERM)
            else:
                os.kill(pid, signal.SIGTERM)
        except Exception as exc:
            signal_error = exc
            logger.warning("Failed to send stop signal to PID %d: %s", pid, exc)
            stopped = _converge_stopped_after_signal_failure()
            if stopped is not None:
                logger.info(
                    "Meshspace %s already converged to stopped state after signal failure for PID %d",
                    meshspace_id,
                    pid,
                )
                return stopped

        record["status"] = "stopping"
        summary = record.get("summary") if isinstance(record.get("summary"), dict) else _default_summary()
        summary = dict(summary)
        summary["active_peer_count"] = 0
        summary["has_recent_activity"] = False
        summary["attention_level"] = _summary_attention_level(summary, status="stopping")
        record["summary"] = summary
        record["updated_at"] = _utcnow_iso()
        self._upsert_record(record)
        if signal_error is not None and _is_windows_platform():
            raise ValueError(
                f"Meshspace '{meshspace_id}' could not be stopped cleanly; please refresh state and try again"
            ) from signal_error
        logger.info("Sent stop signal to meshspace %s (PID %d)", meshspace_id, pid)
        return record

    def restart_meshspace(self, meshspace_id: str, *, wait_timeout: float = 10.0) -> Dict[str, Any]:
        """Restart a supervised child runtime by stopping and spawning it again."""
        record = self.get_meshspace(meshspace_id)
        if not record:
            raise ValueError(f"Meshspace '{meshspace_id}' not found")
        record = self.reconcile_meshspace_runtime(meshspace_id) or record
        probe = self.probe_meshspace_runtime(meshspace_id)
        pid = int(probe.get("detected_pid") or record.get("pid") or 0)
        status = str((probe.get("effective_status") or record.get("status") or "")).strip().lower()
        if probe.get("live"):
            if pid <= 0:
                raise ValueError(
                    f"Meshspace '{meshspace_id}' is live on port {record.get('http_port')} but no process ID was detected"
                )
            self.stop_meshspace(meshspace_id)
            deadline = time.time() + max(1.0, float(wait_timeout or 10.0))
            while time.time() < deadline:
                if not _pid_is_alive(pid):
                    break
                time.sleep(0.2)
            if _pid_is_alive(pid):
                raise ValueError(f"Meshspace '{meshspace_id}' did not stop cleanly for restart")
        elif status not in {"defined", "stopped", "crashed", "degraded", "starting", "stopping", "running"}:
            raise ValueError(f"Meshspace '{meshspace_id}' cannot be restarted from state '{status or 'unknown'}'")
        refreshed = self.get_meshspace(meshspace_id)
        if refreshed:
            refreshed["pid"] = 0
            refreshed["status"] = "stopped"
            self._upsert_record(refreshed)
        return self._spawn_meshspace_process(meshspace_id)

    def _launch_url_for(self, config: Any) -> str:
        host = str(getattr(config.network, "host", "") or "127.0.0.1")
        if host == "0.0.0.0":
            host = "127.0.0.1"
        port = int(getattr(config.network, "port", 0) or 0)
        return f"http://{host}:{port}" if port else ""
