"""Shared large-attachment policy and metadata helpers."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, Optional


LARGE_ATTACHMENT_CAPABILITY = "large_attachment_store_v1"
LARGE_ATTACHMENT_THRESHOLD = 10 * 1024 * 1024  # 10 MB fixed protocol threshold
LARGE_ATTACHMENT_CHUNK_SIZE = 512 * 1024  # 512 KB
LARGE_ATTACHMENT_STORE_DIRNAME = "canopy-large-attachments"
LARGE_ATTACHMENT_STORE_ROOT_KEY = "large_attachment_store_root"
LARGE_ATTACHMENT_DOWNLOAD_MODE_KEY = "large_attachment_download_mode"
LARGE_ATTACHMENT_DOWNLOAD_AUTO = "auto"
LARGE_ATTACHMENT_DOWNLOAD_MANUAL = "manual"
LARGE_ATTACHMENT_DOWNLOAD_PAUSED = "paused"
LARGE_ATTACHMENT_ALLOWED_DOWNLOAD_MODES = {
    LARGE_ATTACHMENT_DOWNLOAD_AUTO,
    LARGE_ATTACHMENT_DOWNLOAD_MANUAL,
    LARGE_ATTACHMENT_DOWNLOAD_PAUSED,
}


def normalize_large_attachment_download_mode(value: Any) -> str:
    mode = str(value or "").strip().lower()
    if mode not in LARGE_ATTACHMENT_ALLOWED_DOWNLOAD_MODES:
        return LARGE_ATTACHMENT_DOWNLOAD_AUTO
    return mode


def get_large_attachment_download_mode(db_manager: Any) -> str:
    if not db_manager:
        return LARGE_ATTACHMENT_DOWNLOAD_AUTO
    try:
        return normalize_large_attachment_download_mode(
            db_manager.get_system_state(LARGE_ATTACHMENT_DOWNLOAD_MODE_KEY)
        )
    except Exception:
        return LARGE_ATTACHMENT_DOWNLOAD_AUTO


def get_large_attachment_store_root(db_manager: Any) -> str:
    if not db_manager:
        return ""
    try:
        return str(db_manager.get_system_state(LARGE_ATTACHMENT_STORE_ROOT_KEY) or "").strip()
    except Exception:
        return ""


def set_large_attachment_settings(
    db_manager: Any,
    *,
    store_root: Optional[str],
    download_mode: Optional[str],
) -> bool:
    if not db_manager:
        return False
    root_value = str(store_root or "").strip()
    mode_value = normalize_large_attachment_download_mode(download_mode)
    ok_root = db_manager.set_system_state(
        LARGE_ATTACHMENT_STORE_ROOT_KEY,
        root_value or None,
    )
    ok_mode = db_manager.set_system_state(
        LARGE_ATTACHMENT_DOWNLOAD_MODE_KEY,
        mode_value,
    )
    return bool(ok_root and ok_mode)


def resolve_large_attachment_store_root(configured_root: str) -> Optional[Path]:
    raw = str(configured_root or "").strip()
    if not raw:
        return None
    candidate = Path(os.path.expanduser(raw))
    return candidate / LARGE_ATTACHMENT_STORE_DIRNAME


def build_large_attachment_metadata(
    *,
    file_info: Any,
    source_peer_id: str,
    download_status: str = "pending",
) -> Dict[str, Any]:
    return {
        "name": getattr(file_info, "original_name", "") or "file",
        "type": getattr(file_info, "content_type", "") or "application/octet-stream",
        "size": int(getattr(file_info, "size", 0) or 0),
        "checksum": getattr(file_info, "checksum", "") or "",
        "origin_file_id": getattr(file_info, "id", "") or "",
        "source_peer_id": str(source_peer_id or "").strip(),
        "storage_mode": "remote_large",
        "large_attachment": True,
        "download_status": str(download_status or "pending").strip().lower() or "pending",
        "url": "",
    }


def is_large_attachment_reference(attachment: Any) -> bool:
    if not isinstance(attachment, dict):
        return False
    if attachment.get("large_attachment") is True:
        return True
    if str(attachment.get("storage_mode") or "").strip().lower() == "remote_large":
        return True
    return bool(
        attachment.get("origin_file_id")
        and attachment.get("source_peer_id")
        and not attachment.get("data")
    )


def get_attachment_origin_file_id(attachment: Any) -> str:
    if not isinstance(attachment, dict):
        return ""
    return str(
        attachment.get("origin_file_id")
        or attachment.get("remote_file_id")
        or ""
    ).strip()


def get_attachment_source_peer_id(attachment: Any) -> str:
    if not isinstance(attachment, dict):
        return ""
    return str(
        attachment.get("source_peer_id")
        or attachment.get("origin_peer")
        or ""
    ).strip()
