"""Shared large-attachment policy and metadata helpers."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, Optional


LARGE_ATTACHMENT_CAPABILITY = "large_attachment_store_v1"
LARGE_ATTACHMENT_THRESHOLD = 10 * 1024 * 1024  # 10 MB fixed protocol threshold
# Chunks are base64-encoded and wrapped in a P2P envelope, so keep them well
# below the router payload cap to avoid oversized-message drops on receive.
LARGE_ATTACHMENT_CHUNK_SIZE = 256 * 1024  # 256 KB
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


def coerce_remote_attachment_reference(
    attachment: Any,
    *,
    default_source_peer_id: str = "",
) -> Optional[Dict[str, Any]]:
    """Upgrade metadata-only remote attachments into fetchable references.

    Older peers can send attachment metadata with just ``id``/``file_id`` and no
    inline ``data`` payload. Treat those entries as remote attachment references
    rooted at the sender peer so the receiver can request the file instead of
    leaving a dead placeholder in the UI.
    """
    if not isinstance(attachment, dict):
        return None
    if attachment.get("data"):
        return None

    source_peer_id = get_attachment_source_peer_id(attachment) or str(default_source_peer_id or "").strip()
    origin_file_id = str(
        get_attachment_origin_file_id(attachment)
        or attachment.get("id")
        or attachment.get("file_id")
        or ""
    ).strip()
    if not source_peer_id or not origin_file_id:
        return None

    normalized = dict(attachment)
    normalized["origin_file_id"] = origin_file_id
    normalized["source_peer_id"] = source_peer_id
    normalized.setdefault("name", attachment.get("filename") or attachment.get("original_name") or "file")
    normalized.setdefault("type", attachment.get("content_type") or "application/octet-stream")
    normalized.setdefault("size", attachment.get("size") or 0)
    normalized.setdefault("checksum", attachment.get("checksum") or "")
    normalized["large_attachment"] = True
    normalized["storage_mode"] = str(normalized.get("storage_mode") or "remote_large").strip().lower() or "remote_large"
    normalized["download_status"] = str(normalized.get("download_status") or "pending").strip().lower() or "pending"
    normalized.pop("url", None)
    return normalized
