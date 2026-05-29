"""
File management system for Canopy.
Handles file uploads, storage, and serving.

Project: Canopy - Local Mesh Communication
License: Apache 2.0
"""

import logging
import os
import secrets
import hashlib
import shutil
from pathlib import Path
from typing import Dict, List, Optional, Any, BinaryIO
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
import json
import base64

from .database import DatabaseManager
from .logging_config import log_performance, LogOperation
from .large_attachments import (
    LARGE_ATTACHMENT_THRESHOLD,
    get_large_attachment_store_root,
    resolve_large_attachment_store_root,
)

# Pillow for thumbnail generation (optional — graceful degradation)
try:
    from PIL import Image
    from PIL import ImageOps
    import io as _io
    _PILLOW_AVAILABLE = True
except ImportError:
    _PILLOW_AVAILABLE = False

logger = logging.getLogger('canopy.files')

_OBVIOUS_PLACEHOLDER_FILE_IDS = {
    'F',
    'FAIL',
    'FILE',
    'FILE_ID',
    'FILEID',
    'FILE_ID_HERE',
    'FILEIDHERE',
    'FILE_ID_PLACEHOLDER',
    'FILE_PLACEHOLDER',
}


def is_obvious_placeholder_file_id(file_id: Any) -> bool:
    """Return True for documentation/example tokens that are not real files."""
    compact = str(file_id or '').strip().replace('-', '_').upper()
    if compact in _OBVIOUS_PLACEHOLDER_FILE_IDS:
        return True
    return compact.startswith('FILE_ID_') and compact.endswith('_HERE')


@dataclass
class FileInfo:
    """Information about an uploaded file."""
    id: str
    original_name: str
    stored_name: str
    file_path: str
    content_type: str
    size: int
    uploaded_by: str
    uploaded_at: datetime
    url: str
    checksum: str
    vault_folder_id: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        data = asdict(self)
        data['uploaded_at'] = self.uploaded_at.isoformat()
        return data


@dataclass
class VaultFolder:
    """User-owned logical folder inside the File Vault."""
    id: str
    user_id: str
    name: str
    parent_id: Optional[str]
    created_at: datetime
    updated_at: datetime

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data['created_at'] = self.created_at.isoformat()
        data['updated_at'] = self.updated_at.isoformat()
        return data

class FileManager:
    """Manages file uploads, storage, and retrieval."""
    _GENERIC_CONTENT_TYPES = {
        '',
        'application/octet-stream',
        'binary/octet-stream',
        'application/x-binary',
        'application/unknown',
    }
    _GENERIC_FILENAMES = {
        '',
        'file',
        'upload',
        'attachment',
        'unnamed_file',
    }
    _EXT_TO_MIME = {
        '.pdf': 'application/pdf',
        '.md': 'text/markdown',
        '.markdown': 'text/markdown',
        '.txt': 'text/plain',
        '.log': 'text/plain',
        '.json': 'application/json',
        '.csv': 'text/csv',
        '.tsv': 'text/csv',
        '.doc': 'application/msword',
        '.dot': 'application/msword',
        '.docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        '.docm': 'application/vnd.ms-word.document.macroenabled.12',
        '.dotx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.template',
        '.rtf': 'application/rtf',
        '.odt': 'application/vnd.oasis.opendocument.text',
        '.xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        '.xlsm': 'application/vnd.ms-excel.sheet.macroenabled.12',
        '.xls': 'application/vnd.ms-excel',
        '.xlsb': 'application/vnd.ms-excel.sheet.binary.macroenabled.12',
        '.ods': 'application/vnd.oasis.opendocument.spreadsheet',
        '.ppt': 'application/vnd.ms-powerpoint',
        '.pot': 'application/vnd.ms-powerpoint',
        '.pps': 'application/vnd.ms-powerpoint',
        '.pptx': 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
        '.pptm': 'application/vnd.ms-powerpoint.presentation.macroenabled.12',
        '.ppsx': 'application/vnd.openxmlformats-officedocument.presentationml.slideshow',
        '.potx': 'application/vnd.openxmlformats-officedocument.presentationml.template',
        '.odp': 'application/vnd.oasis.opendocument.presentation',
        '.eml': 'message/rfc822',
        '.msg': 'application/vnd.ms-outlook',
        '.pages': 'application/vnd.apple.pages',
        '.numbers': 'application/vnd.apple.numbers',
        '.key': 'application/vnd.apple.keynote',
        '.tex': 'text/x-tex',
        '.latex': 'application/x-latex',
        '.py': 'text/x-python',
        '.pyi': 'text/x-python',
        '.pyw': 'text/x-python',
        '.bash': 'text/x-shellscript',
        '.bat': 'text/plain',
        '.c': 'text/x-c',
        '.cjs': 'application/javascript',
        '.cpp': 'text/x-c++src',
        '.cs': 'text/plain',
        '.css': 'text/css',
        '.dockerfile': 'text/plain',
        '.go': 'text/x-go',
        '.gradle': 'text/plain',
        '.h': 'text/x-c',
        '.hpp': 'text/x-c++src',
        '.java': 'text/x-java-source',
        '.js': 'application/javascript',
        '.jsx': 'text/jsx',
        '.kt': 'text/plain',
        '.kts': 'text/plain',
        '.makefile': 'text/plain',
        '.mjs': 'application/javascript',
        '.php': 'text/plain',
        '.ps1': 'text/plain',
        '.rb': 'text/x-ruby',
        '.rs': 'text/x-rust',
        '.sh': 'text/x-shellscript',
        '.sql': 'application/sql',
        '.svelte': 'text/plain',
        '.swift': 'text/plain',
        '.ts': 'application/typescript',
        '.tsx': 'text/tsx',
        '.vue': 'text/plain',
        '.zsh': 'text/x-shellscript',
        '.jpg': 'image/jpeg',
        '.jpeg': 'image/jpeg',
        '.png': 'image/png',
        '.gif': 'image/gif',
        '.webp': 'image/webp',
        '.bmp': 'image/bmp',
        '.svg': 'image/svg+xml',
        '.mp3': 'audio/mpeg',
        '.wav': 'audio/wav',
        '.ogg': 'audio/ogg',
        '.oga': 'audio/ogg',
        '.m4a': 'audio/mp4',
        '.mp4': 'video/mp4',
        '.m4v': 'video/mp4',
        '.webm': 'video/webm',
        '.mov': 'video/quicktime',
        '.xml': 'application/xml',
        '.html': 'text/html',
        '.htm': 'text/html',
        '.zip': 'application/zip',
        '.gz': 'application/gzip',
        '.gzip': 'application/gzip',
        '.tgz': 'application/gzip',
        '.bz2': 'application/x-bzip2',
        '.tbz2': 'application/x-bzip2',
        '.xz': 'application/x-xz',
        '.7z': 'application/x-7z-compressed',
        '.rar': 'application/vnd.rar',
    }
    _MIME_TO_EXT = {
        'application/pdf': '.pdf',
        'text/markdown': '.md',
        'text/plain': '.txt',
        'application/json': '.json',
        'text/csv': '.csv',
        'application/msword': '.doc',
        'application/vnd.openxmlformats-officedocument.wordprocessingml.document': '.docx',
        'application/vnd.ms-word.document.macroenabled.12': '.docm',
        'application/vnd.openxmlformats-officedocument.wordprocessingml.template': '.dotx',
        'application/rtf': '.rtf',
        'text/rtf': '.rtf',
        'application/vnd.oasis.opendocument.text': '.odt',
        'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet': '.xlsx',
        'application/vnd.ms-excel.sheet.macroenabled.12': '.xlsm',
        'application/vnd.ms-excel': '.xls',
        'application/vnd.ms-excel.sheet.binary.macroenabled.12': '.xlsb',
        'application/vnd.oasis.opendocument.spreadsheet': '.ods',
        'application/vnd.ms-powerpoint': '.ppt',
        'application/vnd.openxmlformats-officedocument.presentationml.presentation': '.pptx',
        'application/vnd.ms-powerpoint.presentation.macroenabled.12': '.pptm',
        'application/vnd.openxmlformats-officedocument.presentationml.slideshow': '.ppsx',
        'application/vnd.openxmlformats-officedocument.presentationml.template': '.potx',
        'application/vnd.oasis.opendocument.presentation': '.odp',
        'message/rfc822': '.eml',
        'application/vnd.ms-outlook': '.msg',
        'application/vnd.apple.pages': '.pages',
        'application/vnd.apple.numbers': '.numbers',
        'application/vnd.apple.keynote': '.key',
        'text/x-tex': '.tex',
        'application/x-latex': '.tex',
        'text/x-python': '.py',
        'application/javascript': '.js',
        'application/typescript': '.ts',
        'application/sql': '.sql',
        'text/css': '.css',
        'text/javascript': '.js',
        'text/jsx': '.jsx',
        'text/tsx': '.tsx',
        'text/x-c': '.c',
        'text/x-c++src': '.cpp',
        'text/x-go': '.go',
        'text/x-java-source': '.java',
        'text/x-ruby': '.rb',
        'text/x-rust': '.rs',
        'text/x-shellscript': '.sh',
        'text/x-sql': '.sql',
        'image/jpeg': '.jpg',
        'image/png': '.png',
        'image/gif': '.gif',
        'image/webp': '.webp',
        'image/bmp': '.bmp',
        'image/svg+xml': '.svg',
        'audio/mpeg': '.mp3',
        'audio/wav': '.wav',
        'audio/ogg': '.ogg',
        'audio/mp4': '.m4a',
        'video/mp4': '.mp4',
        'video/webm': '.webm',
        'video/quicktime': '.mov',
        'application/xml': '.xml',
        'text/html': '.html',
        'application/zip': '.zip',
        'application/gzip': '.gz',
        'application/x-bzip2': '.bz2',
        'application/x-xz': '.xz',
        'application/x-7z-compressed': '.7z',
        'application/vnd.rar': '.rar',
        'application/x-rar-compressed': '.rar',
    }
    
    def __init__(self, db: DatabaseManager, storage_path: str = "./data/files"):
        """Initialize the file manager.
        
        Args:
            db: Database manager instance
            storage_path: Directory to store uploaded files
        """
        self.db = db
        self.storage_path = Path(storage_path)
        self.max_file_size = 100 * 1024 * 1024  # 100MB default
        self._project_root = Path(__file__).resolve().parents[2]
        
        logger.info(f"Initializing FileManager with storage path: {self.storage_path}")
        
        # Create storage directory if it doesn't exist
        self.storage_path.mkdir(parents=True, exist_ok=True)
        
        # Create subdirectories for organization
        (self.storage_path / "images").mkdir(exist_ok=True)
        (self.storage_path / "videos").mkdir(exist_ok=True)
        (self.storage_path / "documents").mkdir(exist_ok=True)
        (self.storage_path / "audio").mkdir(exist_ok=True)
        (self.storage_path / "other").mkdir(exist_ok=True)
        
        self._ensure_tables()
        logger.info("FileManager initialized successfully")

    def _candidate_storage_roots(self) -> List[Path]:
        """Return plausible storage roots for backward-compatible file lookup."""
        roots: List[Path] = []
        project_root = Path(getattr(self, '_project_root', Path.cwd()))
        storage_path = Path(self.storage_path)

        def _add(path: Path) -> None:
            p = Path(path).expanduser()
            if p not in roots:
                roots.append(p)

        _add(storage_path)

        # Legacy shared locations (before strict per-device file roots).
        _add(project_root / 'data' / 'files')
        _add(Path.cwd() / 'data' / 'files')

        configured_large_root = resolve_large_attachment_store_root(
            get_large_attachment_store_root(self.db)
        )
        if configured_large_root:
            _add(configured_large_root)

        # If storage path is device-scoped (.../devices/<id>/files), add common alternates.
        parts = list(storage_path.parts)
        if 'devices' in parts:
            idx = parts.index('devices')
            if idx + 2 < len(parts):
                device_id = parts[idx + 1]
                _add(project_root / 'data' / 'devices' / device_id / 'files')
                _add(Path.cwd() / 'data' / 'devices' / device_id / 'files')
                _add(Path.home() / '.canopy' / 'data' / 'devices' / device_id / 'files')

        return roots

    def _select_storage_root(self, file_size: int) -> Path:
        """Choose the on-disk storage root for a new file."""
        configured_large_root = resolve_large_attachment_store_root(
            get_large_attachment_store_root(self.db)
        )
        if configured_large_root and int(file_size or 0) > LARGE_ATTACHMENT_THRESHOLD:
            return configured_large_root
        return self.storage_path

    def _resolve_file_disk_path(self, stored_path: str) -> Path:
        """Resolve a DB file_path to an on-disk file path with compatibility fallbacks."""
        normalized = str(stored_path or '').replace('\\', '/').strip()
        storage_path = Path(self.storage_path)
        if not normalized:
            return storage_path / '__missing__'

        candidates: List[Path] = []

        def _add(path: Path) -> None:
            if path not in candidates:
                candidates.append(path)

        storage_roots = self._candidate_storage_roots()
        storage_prefix = str(storage_path).replace('\\', '/') + '/'
        path_obj = Path(normalized)

        if path_obj.is_absolute():
            _add(path_obj)

        # Relative paths that begin with data/... should be rooted at project or current CWD.
        if normalized.startswith('data/'):
            _add(project_root / normalized)
            _add(Path.cwd() / normalized)

        if normalized.startswith('data/files/'):
            rel = normalized.replace('data/files/', '', 1)
            for root in storage_roots:
                _add(root / rel)
        elif normalized.startswith('data/devices/'):
            # Legacy per-device relative path.
            # Example: data/devices/<id>/files/images/Fabc.jpg -> images/Fabc.jpg
            tail = normalized.split('/files/', 1)[1] if '/files/' in normalized else ''
            if tail:
                for root in storage_roots:
                    _add(root / tail)

        if normalized.startswith(storage_prefix):
            _add(Path(normalized))
        elif not path_obj.is_absolute():
            # Generic relative fallback.
            for root in storage_roots:
                _add(root / normalized)

            # Basename fallback by category for mismatched historical roots.
            basename = Path(normalized).name
            if basename:
                for root in storage_roots:
                    for category in ('images', 'videos', 'documents', 'audio', 'other'):
                        _add(root / category / basename)

        for candidate in candidates:
            if candidate.exists():
                return candidate

        # Return best-effort primary candidate for diagnostic logging.
        return candidates[0] if candidates else (storage_path / normalized)
    
    def _ensure_tables(self) -> None:
        """Ensure file-related database tables exist."""
        logger.info("Ensuring file database tables exist...")
        try:
            with self.db.get_connection() as conn:
                conn.executescript("""
                    -- Files table
                    CREATE TABLE IF NOT EXISTS files (
                        id TEXT PRIMARY KEY,
                        original_name TEXT NOT NULL,
                        stored_name TEXT NOT NULL,
                        file_path TEXT NOT NULL,
                        content_type TEXT NOT NULL,
                        size INTEGER NOT NULL,
                        uploaded_by TEXT NOT NULL,
                        uploaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        vault_folder_id TEXT,
                        checksum TEXT NOT NULL,
                        FOREIGN KEY (uploaded_by) REFERENCES users (id),
                        FOREIGN KEY (vault_folder_id) REFERENCES vault_folders (id) ON DELETE SET NULL
                    );

                    -- User-owned logical folders for File Vault organization.
                    CREATE TABLE IF NOT EXISTS vault_folders (
                        id TEXT PRIMARY KEY,
                        user_id TEXT NOT NULL,
                        name TEXT NOT NULL,
                        parent_id TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        FOREIGN KEY (user_id) REFERENCES users (id),
                        FOREIGN KEY (parent_id) REFERENCES vault_folders (id) ON DELETE CASCADE,
                        UNIQUE(user_id, parent_id, name)
                    );

                    -- Explicit per-file Vault sharing.  Vault files remain
                    -- owner-private unless a row here, or content-scoped
                    -- evidence elsewhere, grants a recipient read access.
                    CREATE TABLE IF NOT EXISTS vault_file_acl (
                        file_id TEXT NOT NULL,
                        grantee_user_id TEXT NOT NULL,
                        granted_by TEXT NOT NULL,
                        can_read INTEGER NOT NULL DEFAULT 1,
                        can_manage INTEGER NOT NULL DEFAULT 0,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        PRIMARY KEY (file_id, grantee_user_id),
                        FOREIGN KEY (file_id) REFERENCES files (id) ON DELETE CASCADE,
                        FOREIGN KEY (grantee_user_id) REFERENCES users (id),
                        FOREIGN KEY (granted_by) REFERENCES users (id)
                    );
                    
                    -- File access log (optional, for tracking downloads)
                    CREATE TABLE IF NOT EXISTS file_access_log (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        file_id TEXT NOT NULL,
                        accessed_by TEXT NOT NULL,
                        accessed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        ip_address TEXT,
                        user_agent TEXT,
                        FOREIGN KEY (file_id) REFERENCES files (id),
                        FOREIGN KEY (accessed_by) REFERENCES users (id)
                    );
                    
                    -- Indexes for performance
                    CREATE INDEX IF NOT EXISTS idx_files_uploaded_by ON files(uploaded_by);
                    CREATE INDEX IF NOT EXISTS idx_files_content_type ON files(content_type);
                    CREATE INDEX IF NOT EXISTS idx_files_uploaded_at ON files(uploaded_at);
                    CREATE INDEX IF NOT EXISTS idx_vault_folders_user_parent ON vault_folders(user_id, parent_id);
                    CREATE INDEX IF NOT EXISTS idx_vault_file_acl_grantee ON vault_file_acl(grantee_user_id, file_id);
                    CREATE INDEX IF NOT EXISTS idx_vault_file_acl_file ON vault_file_acl(file_id);
                    CREATE INDEX IF NOT EXISTS idx_file_access_log_file_id ON file_access_log(file_id);

                    -- Remote transfer tracking for large attachments fetched over P2P.
                    CREATE TABLE IF NOT EXISTS remote_attachment_transfers (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        origin_peer_id TEXT NOT NULL,
                        origin_file_id TEXT NOT NULL,
                        local_file_id TEXT,
                        file_name TEXT,
                        content_type TEXT,
                        size INTEGER,
                        checksum TEXT,
                        status TEXT NOT NULL DEFAULT 'pending',
                        last_request_id TEXT,
                        error TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE(origin_peer_id, origin_file_id)
                    );

                    CREATE INDEX IF NOT EXISTS idx_remote_attachment_transfers_peer
                        ON remote_attachment_transfers(origin_peer_id, status);
                    CREATE INDEX IF NOT EXISTS idx_remote_attachment_transfers_origin_file
                        ON remote_attachment_transfers(origin_file_id);
                """)
                columns = {
                    str(row['name'] if hasattr(row, 'keys') else row[1])
                    for row in conn.execute("PRAGMA table_info(files)").fetchall()
                }
                if 'vault_folder_id' not in columns:
                    conn.execute("ALTER TABLE files ADD COLUMN vault_folder_id TEXT")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_files_vault_folder ON files(uploaded_by, vault_folder_id)")
                conn.commit()
                logger.info("File database tables ensured successfully")
        except Exception as e:
            logger.error(f"Failed to ensure file tables: {e}", exc_info=True)
            raise
    
    def _sanitize_filename(self, filename: str) -> str:
        """Sanitize filename to prevent path traversal and other attacks.

        Args:
            filename: Original filename from user

        Returns:
            Sanitized filename safe for storage
        """
        # Remove any path components (/ or \)
        filename = os.path.basename(filename)

        # Remove dangerous characters
        dangerous_chars = ['..', '~', '|', '<', '>', ':', '"', '\\', '*', '?']
        for char in dangerous_chars:
            filename = filename.replace(char, '_')

        # Limit filename length
        if len(filename) > 255:
            name_part = Path(filename).stem[:200]
            ext_part = Path(filename).suffix
            filename = name_part + ext_part

        # Ensure filename is not empty
        if not filename or filename.strip() == '':
            filename = 'unnamed_file'

        return filename

    def _is_generic_filename(self, filename: str) -> bool:
        stem = Path(filename or '').stem.lower().strip()
        return stem in self._GENERIC_FILENAMES or not Path(filename or '').suffix

    def _is_generic_content_type(self, content_type: str) -> bool:
        return str(content_type or '').strip().lower() in self._GENERIC_CONTENT_TYPES

    def _looks_like_text(self, sample: bytes) -> bool:
        if not sample or b'\x00' in sample:
            return False
        try:
            text = sample.decode('utf-8')
        except UnicodeDecodeError:
            return False
        if not text:
            return False
        printable = sum(1 for ch in text if ch.isprintable() or ch in '\r\n\t')
        return (printable / max(len(text), 1)) >= 0.9

    def _detect_markdown_like(self, text: str) -> bool:
        lines = [ln.strip() for ln in text.splitlines()[:40] if ln.strip()]
        if not lines:
            return False
        md_markers = ('#', '##', '###', '- ', '* ', '> ', '```', '|', '1. ', '2. ')
        if any(any(line.startswith(marker) for marker in md_markers) for line in lines):
            return True
        return ('[](' in text or '](' in text or '**' in text or '__' in text)

    def _detect_content_type(self, file_data: bytes, filename: str, claimed_content_type: str) -> Optional[str]:
        ext = Path(filename or '').suffix.lower()
        if ext and ext in self._EXT_TO_MIME:
            return self._EXT_TO_MIME[ext]

        data = file_data or b''
        if data.startswith(b'%PDF-'):
            return 'application/pdf'
        if data.startswith(b'\x89PNG\r\n\x1a\n'):
            return 'image/png'
        if data.startswith(b'\xff\xd8\xff'):
            return 'image/jpeg'
        if data.startswith((b'GIF87a', b'GIF89a')):
            return 'image/gif'
        if data.startswith(b'RIFF') and len(data) > 12 and data[8:12] == b'WEBP':
            return 'image/webp'
        if data.startswith(b'ID3') or data.startswith((b'\xff\xfb', b'\xff\xfa', b'\xff\xf3', b'\xff\xf2')):
            return 'audio/mpeg'
        if data.startswith(b'RIFF') and len(data) > 12 and data[8:12] == b'WAVE':
            return 'audio/wav'
        if data.startswith(b'OggS'):
            return 'audio/ogg'
        if data.startswith((b'\x00\x00\x00\x18ftypmp4', b'\x00\x00\x00\x1Cftypisom', b'\x00\x00\x00\x1Cftypmp42')):
            if str(claimed_content_type or '').startswith('audio/'):
                return 'audio/mp4'
            return 'video/mp4'
        if data.startswith(b'\x1A\x45\xDF\xA3'):
            if str(claimed_content_type or '').startswith('audio/'):
                return 'audio/webm'
            return 'video/webm'
        if data.startswith((b'<!DOCTYPE', b'<html', b'<HTML')):
            return 'text/html'
        if data.startswith(b'<?xml'):
            return 'application/xml'
        if data.startswith((b'PK\x03\x04', b'PK\x05\x06', b'PK\x07\x08')):
            return 'application/zip'
        if data.startswith(b'\x1f\x8b'):
            return 'application/gzip'
        if data.startswith(b'BZh'):
            return 'application/x-bzip2'
        if data.startswith(b'\xfd7zXZ\x00'):
            return 'application/x-xz'
        if data.startswith(b"7z\xbc\xaf'\x1c"):
            return 'application/x-7z-compressed'
        if data.startswith((b'Rar!\x1a\x07\x00', b'Rar!\x1a\x07\x01\x00')):
            return 'application/vnd.rar'

        sample = data[:8192]
        if self._looks_like_text(sample):
            try:
                text = sample.decode('utf-8', errors='ignore')
            except Exception:
                text = ''
            stripped = text.lstrip()
            if stripped.startswith(('{', '[')):
                return 'application/json'
            if '\\documentclass' in text or '\\begin{' in text:
                return 'text/x-tex'
            if self._detect_markdown_like(text):
                return 'text/markdown'
            if ',' in text and '\n' in text:
                return 'text/csv'
            return 'text/plain'

        return None

    def _normalize_incoming_metadata(self, file_data: bytes, original_name: str,
                                     content_type: str) -> tuple[str, str]:
        name = self._sanitize_filename(original_name or 'file')
        ctype = str(content_type or '').strip().lower()
        if ';' in ctype:
            ctype = ctype.split(';', 1)[0].strip()
        if not ctype:
            ctype = 'application/octet-stream'

        generic_name = self._is_generic_filename(name)
        generic_type = self._is_generic_content_type(ctype)
        detected = self._detect_content_type(file_data, name, ctype)

        if generic_type and detected:
            ctype = detected

        if generic_name:
            ext = Path(name).suffix.lower()
            if not ext:
                ext = self._MIME_TO_EXT.get(ctype, '')
            base = Path(name).stem.lower()
            if not base or base in self._GENERIC_FILENAMES:
                base = 'file'
            if ext:
                name = f"{base}{ext}"
            else:
                name = base

        return name, ctype

    def normalize_upload_metadata(self, file_data: bytes, original_name: str,
                                  content_type: str) -> tuple[str, str]:
        """Public helper for routes to normalize generic upload metadata."""
        return self._normalize_incoming_metadata(file_data, original_name, content_type)

    def _get_file_category(self, content_type: str) -> str:
        """Determine file category based on content type."""
        if content_type.startswith('image/'):
            return 'images'
        elif content_type.startswith('video/'):
            return 'videos'
        elif content_type.startswith('audio/'):
            return 'audio'
        elif content_type in ['application/pdf', 'application/msword', 
                              'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
                              'application/vnd.ms-word.document.macroenabled.12',
                              'application/vnd.openxmlformats-officedocument.wordprocessingml.template',
                              'application/rtf', 'text/rtf',
                              'application/vnd.oasis.opendocument.text',
                              'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                              'application/vnd.ms-excel.sheet.macroenabled.12',
                              'application/vnd.ms-excel',
                              'application/vnd.ms-excel.sheet.binary.macroenabled.12',
                              'application/vnd.oasis.opendocument.spreadsheet',
                              'application/vnd.ms-powerpoint',
                              'application/vnd.openxmlformats-officedocument.presentationml.presentation',
                              'application/vnd.ms-powerpoint.presentation.macroenabled.12',
                              'application/vnd.openxmlformats-officedocument.presentationml.slideshow',
                              'application/vnd.openxmlformats-officedocument.presentationml.template',
                              'application/vnd.oasis.opendocument.presentation',
                              'message/rfc822', 'application/vnd.ms-outlook',
                              'application/vnd.apple.pages', 'application/vnd.apple.numbers',
                              'application/vnd.apple.keynote',
                              'text/plain', 'text/csv', 'text/markdown',
                              'text/x-tex', 'application/x-latex', 'text/x-python',
                              'text/html', 'application/xml', 'text/xml',
                              'application/json']:
            return 'documents'
        else:
            return 'other'
    
    def _calculate_checksum(self, file_data: bytes) -> str:
        """Calculate SHA-256 checksum of file data."""
        return hashlib.sha256(file_data).hexdigest()

    def _backfill_generic_file_metadata(self, file_info: FileInfo) -> FileInfo:
        """Best-effort metadata backfill for legacy generic uploads."""
        needs_name = self._is_generic_filename(file_info.original_name)
        needs_type = self._is_generic_content_type(file_info.content_type)
        if not needs_name and not needs_type:
            return file_info

        disk_path = self._resolve_file_disk_path(file_info.file_path)
        if not disk_path.exists():
            return file_info

        try:
            with open(disk_path, 'rb') as f:
                sample = f.read(8192)
        except Exception:
            return file_info

        new_name, new_type = self._normalize_incoming_metadata(
            file_data=sample,
            original_name=file_info.original_name,
            content_type=file_info.content_type,
        )
        if new_name == file_info.original_name and new_type == file_info.content_type:
            return file_info

        try:
            with self.db.get_connection() as conn:
                conn.execute(
                    "UPDATE files SET original_name = ?, content_type = ? WHERE id = ?",
                    (new_name, new_type, file_info.id),
                )
                conn.commit()
            file_info.original_name = new_name
            file_info.content_type = new_type
            logger.info(
                f"Backfilled file metadata for {file_info.id}: "
                f"name={new_name}, type={new_type}"
            )
        except Exception as e:
            logger.debug(f"File metadata backfill skipped for {file_info.id}: {e}")

        return file_info

    # ------------------------------------------------------------------
    # Thumbnail helpers
    # ------------------------------------------------------------------

    THUMB_MAX_SIZE = 800  # longest side in px
    THUMB_ORIGINAL_FALLBACK_MAX_BYTES = 1024 * 1024
    _EXIF_ORIENTATION_TAG = 274
    _THUMB_NATIVE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.gif', '.webp'}
    _THUMB_INLINE_FALLBACK_TYPES = {
        'image/jpeg',
        'image/png',
        'image/gif',
        'image/webp',
        'image/bmp',
        'image/svg+xml',
    }
    _THUMB_MIME_BY_EXTENSION = {
        '.jpg': 'image/jpeg',
        '.jpeg': 'image/jpeg',
        '.png': 'image/png',
        '.gif': 'image/gif',
        '.webp': 'image/webp',
        '.bmp': 'image/bmp',
        '.svg': 'image/svg+xml',
    }

    def _thumb_path_for(self, original_path: Path) -> Path:
        """Return the expected thumbnail path for a given original file path."""
        stem = original_path.stem
        suffix = original_path.suffix.lower()
        if suffix not in self._THUMB_NATIVE_EXTENSIONS:
            suffix = '.jpg'
        return original_path.with_name(f"{stem}_thumb{suffix}")

    def _thumbnail_content_type_for(self, target_path: Path, file_info: FileInfo, *, is_thumbnail: bool) -> str:
        if is_thumbnail:
            return self._THUMB_MIME_BY_EXTENSION.get(target_path.suffix.lower(), 'image/jpeg')
        return str(file_info.content_type or 'application/octet-stream')

    def _image_exif_orientation(self, image: Any) -> Optional[int]:
        """Return the EXIF orientation tag if present."""
        try:
            exif = image.getexif()
            orientation = int(exif.get(self._EXIF_ORIENTATION_TAG, 1)) if exif else 1
            return orientation
        except Exception:
            return None

    def _thumbnail_orientation_mismatch(self, original_path: Path, thumb_path: Path) -> bool:
        """Detect old thumbnails that ignored side-rotation EXIF orientation."""
        if not _PILLOW_AVAILABLE or not original_path.exists() or not thumb_path.exists():
            return False
        try:
            with Image.open(str(original_path)) as original:
                orientation = self._image_exif_orientation(original)
                if orientation not in {5, 6, 7, 8}:
                    return False
                normalized = ImageOps.exif_transpose(original)
                expected_portrait = normalized.size[1] > normalized.size[0]
            with Image.open(str(thumb_path)) as thumb:
                actual_portrait = thumb.size[1] > thumb.size[0]
            return expected_portrait != actual_portrait
        except Exception:
            return False

    def _generate_thumbnail(self, file_data: bytes, original_path: Path,
                            file_extension: str) -> None:
        """Generate a resized thumbnail alongside the original image.

        Best-effort: failures are logged but never propagate.
        """
        if str(file_extension or '').lower() == '.svg':
            logger.debug("SVG %s uses original preview fallback; raster thumbnail skipped", original_path.name)
            return
        try:
            img: Any = Image.open(_io.BytesIO(file_data))
            orientation = self._image_exif_orientation(img)
            needs_orientation_fix = bool(orientation and orientation != 1)
            if needs_orientation_fix:
                img = ImageOps.exif_transpose(img)
            # Skip tiny images that are already smaller than the thumb size
            w, h = img.size
            if max(w, h) <= self.THUMB_MAX_SIZE and not needs_orientation_fix:
                logger.debug(f"Image {original_path.name} already ≤{self.THUMB_MAX_SIZE}px, skipping thumbnail")
                return

            resample_lanczos = getattr(getattr(Image, "Resampling", Image), "LANCZOS")
            img.thumbnail((self.THUMB_MAX_SIZE, self.THUMB_MAX_SIZE), resample_lanczos)
            thumb_path = self._thumb_path_for(original_path)

            # Determine save format
            fmt = img.format or 'JPEG'
            if file_extension.lower() in ('.jpg', '.jpeg'):
                fmt = 'JPEG'
            elif file_extension.lower() == '.png':
                fmt = 'PNG'
            elif file_extension.lower() == '.webp':
                fmt = 'WEBP'
            elif file_extension.lower() == '.gif':
                fmt = 'GIF'
            else:
                fmt = 'JPEG'

            # Convert RGBA to RGB for JPEG
            if fmt == 'JPEG' and img.mode in ('RGBA', 'P'):
                img = img.convert('RGB')

            img.save(str(thumb_path), fmt, quality=82, optimize=True)
            logger.info(f"Thumbnail generated: {thumb_path} ({img.size[0]}x{img.size[1]})")
        except Exception as e:
            logger.warning(f"Thumbnail generation failed for {original_path.name}: {e}")

    @log_performance('files')
    def get_thumbnail_data(self, file_id: str) -> Any:
        """Get thumbnail data for an image file.

        Returns (thumb_bytes, file_info, mimetype).  For small, browser-safe
        originals, this may fall back to the original when no thumbnail exists.
        Large or unsupported originals return None so the UI can show a download
        fallback instead of trying to inline a heavy phone image as a thumbnail.
        """
        file_info = self.get_file(file_id)
        if not file_info:
            return None

        original_path = self._resolve_file_disk_path(file_info.file_path)

        thumb_path = self._thumb_path_for(original_path)
        is_svg = original_path.suffix.lower() == '.svg' or str(file_info.content_type or '').lower() == 'image/svg+xml'
        if _PILLOW_AVAILABLE and not is_svg and str(file_info.content_type or '').startswith('image/') and original_path.exists():
            if not thumb_path.exists() or self._thumbnail_orientation_mismatch(original_path, thumb_path):
                try:
                    self._generate_thumbnail(original_path.read_bytes(), original_path, original_path.suffix)
                except Exception as e:
                    logger.debug(f"Lazy thumbnail normalization skipped for {file_id}: {e}")
        has_thumbnail = thumb_path.exists()
        target = thumb_path if has_thumbnail else original_path

        if not target.exists():
            logger.error(f"Neither thumb nor original found for {file_id}")
            return None

        if not has_thumbnail:
            content_type = str(file_info.content_type or '').lower()
            try:
                target_size = target.stat().st_size
            except OSError:
                target_size = int(file_info.size or 0)
            if content_type not in self._THUMB_INLINE_FALLBACK_TYPES:
                logger.debug("Thumbnail fallback skipped for unsupported image type %s (%s)", file_id, content_type)
                return None
            if target_size > self.THUMB_ORIGINAL_FALLBACK_MAX_BYTES:
                logger.debug(
                    "Thumbnail fallback skipped for large original %s (%d bytes)",
                    file_id,
                    target_size,
                )
                return None

        try:
            with open(target, 'rb') as f:
                data = f.read()
            return data, file_info, self._thumbnail_content_type_for(target, file_info, is_thumbnail=has_thumbnail)
        except Exception as e:
            logger.error(f"Failed to read thumbnail for {file_id}: {e}")
            return None

    @log_performance('files')
    def save_file(
        self,
        file_data: bytes,
        original_name: str,
        content_type: str,
        uploaded_by: str,
        *,
        vault_folder_id: Optional[str] = None,
        max_size_override: Optional[int] = None,
    ) -> Optional[FileInfo]:
        """Save an uploaded file to disk and database.
        
        Args:
            file_data: Raw file data as bytes
            original_name: Original filename
            content_type: MIME content type
            uploaded_by: User ID who uploaded the file
            
        Returns:
            FileInfo object with file details, or None if save failed
        """
        logger.info(f"Saving file: {original_name} ({len(file_data)} bytes) by user {uploaded_by}")
        
        try:
            # Normalize incoming metadata so generic agent uploads don't degrade
            # into name=file/type=application/octet-stream attachments.
            original_name, content_type = self._normalize_incoming_metadata(
                file_data=file_data,
                original_name=original_name,
                content_type=content_type,
            )

            # Enforce the shared upload policy here too: agent/MCP paths often
            # enter at FileManager.save_file() instead of the browser upload route.
            from ..security.file_validation import detect_zip_bomb, validate_file_upload

            is_valid, error_msg, validated_type = validate_file_upload(
                file_data,
                content_type,
                original_name,
                max_size_override=max_size_override or self.max_file_size,
            )
            if not is_valid:
                logger.error("File upload rejected for %s: %s", original_name, error_msg)
                return None
            content_type = validated_type or content_type

            is_safe_archive, archive_error = detect_zip_bomb(file_data, content_type)
            if not is_safe_archive:
                logger.error("Archive upload rejected for %s: %s", original_name, archive_error)
                return None
            
            folder_clean = str(vault_folder_id or '').strip() or None
            if folder_clean and not self.get_user_folder(uploaded_by, folder_clean):
                logger.error("File upload rejected for %s: Vault folder %s was not found for user %s", original_name, folder_clean, uploaded_by)
                return None

            # Generate unique file ID and stored name
            file_id = f"F{secrets.token_hex(12)}"
            file_extension = Path(original_name).suffix.lower()
            stored_name = f"{file_id}{file_extension}"
            
            # Determine storage category and path
            category = self._get_file_category(content_type)
            storage_root = self._select_storage_root(len(file_data))
            (storage_root / "images").mkdir(parents=True, exist_ok=True)
            (storage_root / "videos").mkdir(parents=True, exist_ok=True)
            (storage_root / "documents").mkdir(parents=True, exist_ok=True)
            (storage_root / "audio").mkdir(parents=True, exist_ok=True)
            (storage_root / "other").mkdir(parents=True, exist_ok=True)
            file_path = storage_root / category / stored_name

            # Verify the resolved path is within the selected storage root (prevent path traversal)
            try:
                file_path = file_path.resolve()
                storage_path_resolved = storage_root.resolve()
                if not str(file_path).startswith(str(storage_path_resolved)):
                    logger.error(f"Path traversal attempt detected: {file_path}")
                    return None
            except Exception as e:
                logger.error(f"Path resolution failed: {e}")
                return None
            
            # Calculate checksum
            checksum = self._calculate_checksum(file_data)
            
            # Save file to disk
            with LogOperation(f"Writing file to disk: {file_path}"):
                with open(file_path, 'wb') as f:
                    f.write(file_data)
            
            # Create FileInfo object
            file_info = FileInfo(
                id=file_id,
                original_name=original_name,
                stored_name=stored_name,
                file_path=str(file_path),
                content_type=content_type,
                size=len(file_data),
                uploaded_by=uploaded_by,
                uploaded_at=datetime.now(timezone.utc),
                url=f"/files/{file_id}",
                checksum=checksum,
                vault_folder_id=folder_clean,
            )
            
            # Save to database
            with LogOperation(f"Saving file metadata to database: {file_id}"):
                with self.db.get_connection() as conn:
                    conn.execute("""
                        INSERT INTO files (id, original_name, stored_name, file_path, 
                                         content_type, size, uploaded_by, checksum, vault_folder_id)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        file_info.id, file_info.original_name, file_info.stored_name,
                        file_info.file_path, file_info.content_type, file_info.size,
                        file_info.uploaded_by, file_info.checksum, file_info.vault_folder_id
                    ))
                    conn.commit()
            
            # Generate thumbnail for images (best-effort)
            if _PILLOW_AVAILABLE and content_type.startswith('image/') and file_extension != '.svg':
                self._generate_thumbnail(file_data, file_path, file_extension)

            logger.info(f"File saved successfully: {file_id} -> {file_path}")
            return file_info
            
        except Exception as e:
            logger.error(f"Failed to save file {original_name}: {e}", exc_info=True)
            return None

    @log_performance('files')
    def replace_user_file_content(
        self,
        user_id: str,
        file_id: str,
        file_data: bytes,
        *,
        original_name: Optional[str] = None,
        content_type: Optional[str] = None,
        max_size_override: Optional[int] = None,
    ) -> Optional[FileInfo]:
        """Replace the bytes for an existing user-owned Vault file.

        This keeps the stable file ID so existing drafts/tooling can continue
        referencing the same Vault object while applying the same upload safety
        checks used for new files.
        """
        user_id = str(user_id or '').strip()
        file_id = str(file_id or '').strip()
        if not user_id or not file_id:
            return None

        current = self.get_file(file_id)
        if not current or str(current.uploaded_by or '') != user_id:
            return None

        try:
            raw = bytes(file_data or b'')
            name = original_name if original_name is not None else current.original_name
            ctype = content_type if content_type is not None else current.content_type
            name, ctype = self._normalize_incoming_metadata(
                file_data=raw,
                original_name=name or current.original_name or 'file',
                content_type=ctype or current.content_type or 'application/octet-stream',
            )

            from ..security.file_validation import detect_zip_bomb, validate_file_upload

            is_valid, error_msg, validated_type = validate_file_upload(
                raw,
                ctype,
                name,
                max_size_override=max_size_override or self.max_file_size,
            )
            if not is_valid:
                logger.error("Vault file replacement rejected for %s: %s", file_id, error_msg)
                return None
            ctype = validated_type or ctype

            is_safe_archive, archive_error = detect_zip_bomb(raw, ctype)
            if not is_safe_archive:
                logger.error("Vault archive replacement rejected for %s: %s", file_id, archive_error)
                return None

            file_extension = Path(name).suffix.lower()
            stored_name = f"{file_id}{file_extension}"
            category = self._get_file_category(ctype)
            storage_root = self._select_storage_root(len(raw))
            for folder in ("images", "videos", "documents", "audio", "other"):
                (storage_root / folder).mkdir(parents=True, exist_ok=True)

            target_path = (storage_root / category / stored_name).resolve()
            storage_root_resolved = storage_root.resolve()
            if not str(target_path).startswith(str(storage_root_resolved)):
                logger.error("Path traversal guard blocked Vault replacement target: %s", target_path)
                return None

            checksum = self._calculate_checksum(raw)
            tmp_path = target_path.with_name(f".{target_path.name}.tmp-{secrets.token_hex(4)}")
            try:
                tmp_path.write_bytes(raw)
                os.replace(tmp_path, target_path)
            finally:
                try:
                    if tmp_path.exists():
                        tmp_path.unlink()
                except Exception:
                    pass

            old_path = self._resolve_file_disk_path(current.file_path)
            if old_path != target_path and old_path.exists():
                try:
                    old_thumb = self._thumb_path_for(old_path)
                    old_path.unlink()
                    if old_thumb.exists():
                        old_thumb.unlink()
                except Exception:
                    logger.debug("Could not remove superseded Vault file path for %s", file_id, exc_info=True)

            now = datetime.now(timezone.utc)
            with self.db.get_connection() as conn:
                conn.execute(
                    """
                    UPDATE files
                    SET original_name = ?, stored_name = ?, file_path = ?,
                        content_type = ?, size = ?, checksum = ?, uploaded_at = ?
                    WHERE id = ? AND uploaded_by = ?
                    """,
                    (
                        name,
                        stored_name,
                        str(target_path),
                        ctype,
                        len(raw),
                        checksum,
                        now.isoformat(),
                        file_id,
                        user_id,
                    ),
                )
                conn.commit()

            if _PILLOW_AVAILABLE and ctype.startswith('image/') and file_extension != '.svg':
                self._generate_thumbnail(raw, target_path, file_extension)

            return self.get_file(file_id)
        except Exception as e:
            logger.error("Failed to replace Vault file %s for user %s: %s", file_id, user_id, e, exc_info=True)
            return None

    @log_performance('files')
    def copy_file_to_user_vault(
        self,
        source_file_id: str,
        uploaded_by: str,
        *,
        vault_folder_id: Optional[str] = None,
        duplicate_if_owned: bool = False,
    ) -> Optional[FileInfo]:
        """Copy an existing local file into a user's Vault without a browser download."""
        source_file_id = str(source_file_id or '').strip()
        uploaded_by = str(uploaded_by or '').strip()
        if not source_file_id or not uploaded_by:
            return None

        source = self.get_file(source_file_id)
        if not source:
            return None
        if str(source.uploaded_by or '') == uploaded_by and not duplicate_if_owned:
            if vault_folder_id is not None:
                try:
                    return self.move_user_file_to_folder(uploaded_by, source.id, vault_folder_id)
                except Exception:
                    logger.debug("Could not move owned Vault file %s during save-to-vault", source.id, exc_info=True)
            return source

        try:
            source_path = self._resolve_file_disk_path(source.file_path)
            if not source_path.exists():
                logger.warning("Cannot copy file %s to Vault because local data is unavailable", source_file_id)
                return None

            original_name, content_type = self._normalize_incoming_metadata(
                file_data=b'',
                original_name=source.original_name,
                content_type=source.content_type,
            )
            file_id = f"F{secrets.token_hex(12)}"
            file_extension = Path(original_name).suffix.lower()
            stored_name = f"{file_id}{file_extension}"
            category = self._get_file_category(content_type)
            storage_root = self._select_storage_root(int(source.size or source_path.stat().st_size or 0))
            for folder in ("images", "videos", "documents", "audio", "other"):
                (storage_root / folder).mkdir(parents=True, exist_ok=True)
            target_path = (storage_root / category / stored_name).resolve()
            storage_root_resolved = storage_root.resolve()
            if not str(target_path).startswith(str(storage_root_resolved)):
                logger.error("Path traversal guard blocked Vault copy target: %s", target_path)
                return None

            hasher = hashlib.sha256()
            size = 0
            with source_path.open('rb') as src, target_path.open('wb') as dst:
                while True:
                    chunk = src.read(1024 * 1024)
                    if not chunk:
                        break
                    dst.write(chunk)
                    hasher.update(chunk)
                    size += len(chunk)
            checksum = hasher.hexdigest()
            now = datetime.now(timezone.utc)
            file_info = FileInfo(
                id=file_id,
                original_name=original_name,
                stored_name=stored_name,
                file_path=str(target_path),
                content_type=content_type,
                size=size,
                uploaded_by=uploaded_by,
                uploaded_at=now,
                url=f"/files/{file_id}",
                checksum=checksum,
                vault_folder_id=str(vault_folder_id or '').strip() or None,
            )

            with self.db.get_connection() as conn:
                conn.execute("""
                    INSERT INTO files (id, original_name, stored_name, file_path,
                                     content_type, size, uploaded_by, checksum, vault_folder_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    file_info.id,
                    file_info.original_name,
                    file_info.stored_name,
                    file_info.file_path,
                    file_info.content_type,
                    file_info.size,
                    file_info.uploaded_by,
                    file_info.checksum,
                    file_info.vault_folder_id,
                ))
                conn.commit()

            source_thumb = self._thumb_path_for(source_path)
            if source_thumb.exists():
                try:
                    shutil.copy2(source_thumb, self._thumb_path_for(target_path))
                except Exception:
                    logger.debug("Could not copy thumbnail for Vault copy %s", file_id, exc_info=True)
            elif _PILLOW_AVAILABLE and content_type.startswith('image/') and file_extension != '.svg':
                try:
                    self._generate_thumbnail(target_path.read_bytes(), target_path, file_extension)
                except Exception:
                    logger.debug("Could not generate thumbnail for Vault copy %s", file_id, exc_info=True)

            logger.info("Copied file %s into user %s Vault as %s", source_file_id, uploaded_by, file_id)
            return file_info
        except Exception as e:
            logger.error("Failed to copy file %s into Vault for user %s: %s", source_file_id, uploaded_by, e, exc_info=True)
            return None

    def get_remote_attachment_transfer(self, origin_peer_id: str,
                                       origin_file_id: str) -> Optional[Dict[str, Any]]:
        """Return tracked transfer state for a remote large attachment."""
        if not origin_peer_id or not origin_file_id:
            return None
        try:
            with self.db.get_connection() as conn:
                row = conn.execute(
                    """
                    SELECT origin_peer_id, origin_file_id, local_file_id, file_name,
                           content_type, size, checksum, status, last_request_id,
                           error, created_at, updated_at
                    FROM remote_attachment_transfers
                    WHERE origin_peer_id = ? AND origin_file_id = ?
                    """,
                    (origin_peer_id, origin_file_id),
                ).fetchone()
                return dict(row) if row else None
        except Exception as e:
            logger.debug(
                "Failed to load remote attachment transfer %s/%s: %s",
                origin_peer_id,
                origin_file_id,
                e,
            )
            return None

    def upsert_remote_attachment_transfer(
        self,
        *,
        origin_peer_id: str,
        origin_file_id: str,
        file_name: Optional[str] = None,
        content_type: Optional[str] = None,
        size: Optional[int] = None,
        checksum: Optional[str] = None,
        status: str = 'pending',
        last_request_id: Optional[str] = None,
        error: Optional[str] = None,
        local_file_id: Optional[str] = None,
    ) -> bool:
        """Create or update tracked transfer state for a remote large attachment."""
        if not origin_peer_id or not origin_file_id:
            return False
        try:
            with self.db.get_connection() as conn:
                conn.execute(
                    """
                    INSERT INTO remote_attachment_transfers (
                        origin_peer_id, origin_file_id, local_file_id, file_name,
                        content_type, size, checksum, status, last_request_id,
                        error, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(origin_peer_id, origin_file_id) DO UPDATE SET
                        local_file_id = COALESCE(excluded.local_file_id, remote_attachment_transfers.local_file_id),
                        file_name = COALESCE(excluded.file_name, remote_attachment_transfers.file_name),
                        content_type = COALESCE(excluded.content_type, remote_attachment_transfers.content_type),
                        size = COALESCE(excluded.size, remote_attachment_transfers.size),
                        checksum = COALESCE(excluded.checksum, remote_attachment_transfers.checksum),
                        status = COALESCE(excluded.status, remote_attachment_transfers.status),
                        last_request_id = COALESCE(excluded.last_request_id, remote_attachment_transfers.last_request_id),
                        error = excluded.error,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    (
                        origin_peer_id,
                        origin_file_id,
                        local_file_id,
                        file_name,
                        content_type,
                        size,
                        checksum,
                        status,
                        last_request_id,
                        error,
                    ),
                )
                conn.commit()
            return True
        except Exception as e:
            logger.error(
                "Failed to upsert remote attachment transfer %s/%s: %s",
                origin_peer_id,
                origin_file_id,
                e,
                exc_info=True,
            )
            return False

    def list_pending_remote_attachment_transfers(
        self,
        *,
        origin_peer_id: Optional[str] = None,
        statuses: Optional[List[str]] = None,
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        """List remote large-attachment transfers that still need action."""
        try:
            clauses = []
            params: List[Any] = []
            if origin_peer_id:
                clauses.append("origin_peer_id = ?")
                params.append(origin_peer_id)
            wanted = [str(s).strip().lower() for s in (statuses or ['pending', 'requested', 'error']) if str(s).strip()]
            if wanted:
                placeholders = ",".join("?" for _ in wanted)
                clauses.append(f"LOWER(status) IN ({placeholders})")
                params.extend(wanted)
            where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
            params.append(max(1, int(limit or 200)))
            with self.db.get_connection() as conn:
                rows = conn.execute(
                    f"""
                    SELECT origin_peer_id, origin_file_id, local_file_id, file_name,
                           content_type, size, checksum, status, last_request_id,
                           error, created_at, updated_at
                    FROM remote_attachment_transfers
                    {where_sql}
                    ORDER BY updated_at DESC
                    LIMIT ?
                    """,
                    params,
                ).fetchall()
            return [dict(row) for row in rows]
        except Exception as e:
            logger.debug("Failed to list pending remote attachment transfers: %s", e)
            return []
    
    @log_performance('files')
    def get_file(self, file_id: str) -> Optional[FileInfo]:
        """Retrieve file information by ID.
        
        Args:
            file_id: Unique file identifier
            
        Returns:
            FileInfo object or None if not found
        """
        logger.debug(f"Retrieving file info: {file_id}")
        
        try:
            with self.db.get_connection() as conn:
                cursor = conn.execute("""
                    SELECT id, original_name, stored_name, file_path, content_type,
                           size, uploaded_by, uploaded_at, checksum, vault_folder_id
                    FROM files WHERE id = ?
                """, (file_id,))
                
                row = cursor.fetchone()
                if not row:
                    if is_obvious_placeholder_file_id(file_id):
                        logger.debug("Ignoring placeholder file lookup: %s", file_id)
                    else:
                        logger.warning(f"File not found: {file_id}")
                    return None
                
                file_info = FileInfo(
                    id=row['id'],
                    original_name=row['original_name'],
                    stored_name=row['stored_name'],
                    file_path=row['file_path'],
                    content_type=row['content_type'],
                    size=row['size'],
                    uploaded_by=row['uploaded_by'],
                    uploaded_at=datetime.fromisoformat(row['uploaded_at']),
                    url=f"/files/{row['id']}",
                    checksum=row['checksum'],
                    vault_folder_id=row['vault_folder_id'] if 'vault_folder_id' in row.keys() else None,
                )
                return self._backfill_generic_file_metadata(file_info)

        except Exception as e:
            logger.error(f"Failed to retrieve file {file_id}: {e}", exc_info=True)
            return None
    
    @log_performance('files')
    def get_file_data(self, file_id: str) -> Optional[tuple[bytes, FileInfo]]:
        """Get file data and info for serving.
        
        Args:
            file_id: Unique file identifier
            
        Returns:
            Tuple of (file_data, file_info) or None if not found
        """
        logger.debug(f"Getting file data: {file_id}")
        
        try:
            file_info = self.get_file(file_id)
            if not file_info:
                return None
            
            file_path = self._resolve_file_disk_path(file_info.file_path)
            
            # Check if file exists on disk
            if not file_path.exists():
                logger.error(f"File not found on disk: {file_path}")
                return None
            
            # Read file data
            with open(file_path, 'rb') as f:
                file_data = f.read()
            
            # Verify checksum
            actual_checksum = self._calculate_checksum(file_data)
            if actual_checksum != file_info.checksum:
                logger.error(f"File checksum mismatch for {file_id}: expected {file_info.checksum}, got {actual_checksum}")
                return None
            
            return file_data, file_info
            
        except Exception as e:
            logger.error(f"Failed to get file data for {file_id}: {e}", exc_info=True)
            return None

    def _vault_file_acl_entry_from_row(self, row: Any) -> Dict[str, Any]:
        """Return a JSON-ready Vault file ACL entry from a DB row."""
        user_id = str(row['grantee_user_id'] or '').strip()
        username = str((row['username'] if 'username' in row.keys() else '') or '').strip()
        display_name = str((row['display_name'] if 'display_name' in row.keys() else '') or '').strip()
        avatar_file_id = str((row['avatar_file_id'] if 'avatar_file_id' in row.keys() else '') or '').strip()
        account_type = str((row['account_type'] if 'account_type' in row.keys() else '') or '').strip()
        origin_peer = str((row['origin_peer'] if 'origin_peer' in row.keys() else '') or '').strip()
        return {
            'file_id': str(row['file_id'] or '').strip(),
            'user_id': user_id,
            'grantee_user_id': user_id,
            'username': username or user_id,
            'display_name': display_name or username or user_id,
            'avatar_url': f"/files/{avatar_file_id}" if avatar_file_id else '',
            'account_type': account_type,
            'origin_peer': origin_peer,
            'is_remote': bool(origin_peer),
            'can_read': bool(row['can_read']),
            'can_manage': bool(row['can_manage']),
            'granted_by': str(row['granted_by'] or '').strip(),
            'created_at': str(row['created_at'] or ''),
            'updated_at': str(row['updated_at'] or ''),
            'missing_local_user': not bool(username or display_name),
        }

    def _ensure_user_owns_file(self, file_id: str, owner_user_id: str) -> FileInfo:
        file_info = self.get_file(str(file_id or '').strip())
        if not file_info or str(file_info.uploaded_by or '') != str(owner_user_id or ''):
            raise ValueError('File was not found in your Vault.')
        return file_info

    def list_vault_file_access(self, file_id: str, owner_user_id: str) -> List[Dict[str, Any]]:
        """List explicit recipients for an owner-managed Vault file."""
        file_info = self._ensure_user_owns_file(file_id, owner_user_id)
        try:
            with self.db.get_connection() as conn:
                try:
                    user_columns = {
                        str(row['name'])
                        for row in conn.execute("PRAGMA table_info(users)").fetchall()
                    }
                except Exception:
                    user_columns = set()
                username_expr = "u.username" if "username" in user_columns else "''"
                display_name_expr = "u.display_name" if "display_name" in user_columns else "''"
                avatar_expr = "u.avatar_file_id" if "avatar_file_id" in user_columns else "''"
                account_type_expr = "u.account_type" if "account_type" in user_columns else "'human'"
                origin_peer_expr = "u.origin_peer" if "origin_peer" in user_columns else "''"
                display_sort_expr = "u.display_name" if "display_name" in user_columns else "''"
                username_sort_expr = "u.username" if "username" in user_columns else "''"
                rows = conn.execute(
                    f"""
                    SELECT
                        a.file_id,
                        a.grantee_user_id,
                        a.granted_by,
                        a.can_read,
                        a.can_manage,
                        a.created_at,
                        a.updated_at,
                        {username_expr} AS username,
                        {display_name_expr} AS display_name,
                        {avatar_expr} AS avatar_file_id,
                        {account_type_expr} AS account_type,
                        {origin_peer_expr} AS origin_peer
                    FROM vault_file_acl a
                    LEFT JOIN users u ON u.id = a.grantee_user_id
                    WHERE a.file_id = ?
                    ORDER BY LOWER(COALESCE(NULLIF({display_sort_expr}, ''), NULLIF({username_sort_expr}, ''), a.grantee_user_id))
                    """,
                    (file_info.id,),
                ).fetchall()
            return [self._vault_file_acl_entry_from_row(row) for row in rows]
        except Exception as e:
            logger.error("Failed to list Vault file access for %s: %s", file_id, e, exc_info=True)
            raise ValueError('Could not load file access.') from e

    def count_vault_file_access(self, file_id: str, owner_user_id: str) -> int:
        """Return explicit recipient count for an owner-managed Vault file."""
        file_info = self._ensure_user_owns_file(file_id, owner_user_id)
        try:
            with self.db.get_connection() as conn:
                row = conn.execute(
                    "SELECT COUNT(*) AS count FROM vault_file_acl WHERE file_id = ? AND can_read = 1",
                    (file_info.id,),
                ).fetchone()
            return int((row['count'] if row and 'count' in row.keys() else 0) or 0)
        except Exception:
            return 0

    def count_vault_file_access_map(self, owner_user_id: str, file_ids: List[str]) -> Dict[str, int]:
        """Return explicit recipient counts keyed by file ID for owned Vault files."""
        clean_ids = []
        for raw in file_ids or []:
            file_id = str(raw or '').strip()
            if file_id and file_id not in clean_ids:
                clean_ids.append(file_id)
        if not owner_user_id or not clean_ids:
            return {}
        try:
            placeholders = ",".join("?" for _ in clean_ids)
            with self.db.get_connection() as conn:
                rows = conn.execute(
                    f"""
                    SELECT f.id AS file_id, COUNT(a.grantee_user_id) AS count
                    FROM files f
                    LEFT JOIN vault_file_acl a
                      ON a.file_id = f.id
                     AND a.can_read = 1
                    WHERE f.uploaded_by = ?
                      AND f.id IN ({placeholders})
                    GROUP BY f.id
                    """,
                    [owner_user_id] + clean_ids,
                ).fetchall()
            return {str(row['file_id']): int(row['count'] or 0) for row in rows}
        except Exception as e:
            logger.debug("Failed to count Vault file ACLs for %s: %s", owner_user_id, e)
            return {}

    def grant_vault_file_access(
        self,
        file_id: str,
        owner_user_id: str,
        grantee_user_id: str,
        *,
        can_read: bool = True,
        can_manage: bool = False,
    ) -> Dict[str, Any]:
        """Grant or update explicit access to an owner-managed Vault file."""
        file_info = self._ensure_user_owns_file(file_id, owner_user_id)
        grantee = str(grantee_user_id or '').strip()
        if not grantee:
            raise ValueError('Choose a user or agent to share with.')
        if grantee == str(owner_user_id or '').strip():
            raise ValueError('The owner already has full access to this file.')
        try:
            now = datetime.now(timezone.utc).isoformat()
            with self.db.get_connection() as conn:
                user_row = conn.execute(
                    "SELECT id FROM users WHERE id = ? LIMIT 1",
                    (grantee,),
                ).fetchone()
                if not user_row:
                    raise ValueError('That local user or agent was not found.')
                conn.execute(
                    """
                    INSERT INTO vault_file_acl (
                        file_id, grantee_user_id, granted_by, can_read, can_manage, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(file_id, grantee_user_id) DO UPDATE SET
                        granted_by = excluded.granted_by,
                        can_read = excluded.can_read,
                        can_manage = excluded.can_manage,
                        updated_at = excluded.updated_at
                    """,
                    (
                        file_info.id,
                        grantee,
                        str(owner_user_id or '').strip(),
                        1 if can_read else 0,
                        1 if can_manage else 0,
                        now,
                        now,
                    ),
                )
                conn.commit()
            entry = next(
                (item for item in self.list_vault_file_access(file_info.id, owner_user_id) if item.get('user_id') == grantee),
                None,
            )
            if not entry:
                raise ValueError('File access was saved but could not be reloaded.')
            return entry
        except ValueError:
            raise
        except Exception as e:
            logger.error("Failed to grant Vault file access for %s: %s", file_id, e, exc_info=True)
            raise ValueError('Could not grant file access.') from e

    def revoke_vault_file_access(self, file_id: str, owner_user_id: str, grantee_user_id: str) -> bool:
        """Revoke explicit access from one recipient without changing others."""
        file_info = self._ensure_user_owns_file(file_id, owner_user_id)
        grantee = str(grantee_user_id or '').strip()
        if not grantee:
            raise ValueError('Recipient user id is required.')
        try:
            with self.db.get_connection() as conn:
                conn.execute(
                    "DELETE FROM vault_file_acl WHERE file_id = ? AND grantee_user_id = ?",
                    (file_info.id, grantee),
                )
                conn.commit()
            return True
        except Exception as e:
            logger.error("Failed to revoke Vault file access for %s: %s", file_id, e, exc_info=True)
            raise ValueError('Could not revoke file access.') from e
    
    def log_file_access(self, file_id: str, accessed_by: str, ip_address: Optional[str] = None,
                       user_agent: Optional[str] = None) -> None:
        """Log file access for analytics/security.
        
        Args:
            file_id: File that was accessed
            accessed_by: User who accessed the file
            ip_address: Client IP address
            user_agent: Client user agent string
        """
        try:
            with self.db.get_connection() as conn:
                conn.execute("""
                    INSERT INTO file_access_log (file_id, accessed_by, ip_address, user_agent)
                    VALUES (?, ?, ?, ?)
                """, (file_id, accessed_by, ip_address, user_agent))
                conn.commit()
        except Exception as e:
            logger.error(f"Failed to log file access: {e}", exc_info=True)
    
    def delete_file(self, file_id: str, user_id: str, is_admin: bool = False) -> bool:
        """Delete a file.

        Only the file owner or the local instance admin may delete a file.
        The ``is_admin`` flag must be determined by the caller by comparing
        ``user_id`` against the local instance-owner user ID — it must never
        be sourced from a remote peer or a client-supplied claim.

        Args:
            file_id: File to delete
            user_id: User requesting deletion
            is_admin: True only when the caller has verified the user is the
                      local instance admin (owner of this Canopy instance).

        Returns:
            True if deleted successfully, False otherwise
        """
        logger.info(f"Deleting file {file_id} requested by user {user_id} (is_admin={is_admin})")
        
        try:
            file_info = self.get_file(file_id)
            if not file_info:
                logger.warning(f"File not found for deletion: {file_id}")
                return False
            
            # Only the file owner or the local instance admin may delete.
            if file_info.uploaded_by != user_id and not is_admin:
                logger.warning(f"User {user_id} attempted to delete file {file_id} owned by {file_info.uploaded_by}")
                return False
            
            # Delete from database (file_access_log references files, so delete it first)
            with self.db.get_connection() as conn:
                conn.execute("DELETE FROM vault_file_acl WHERE file_id = ?", (file_id,))
                conn.execute("DELETE FROM file_access_log WHERE file_id = ?", (file_id,))
                conn.execute("DELETE FROM files WHERE id = ?", (file_id,))
                conn.commit()
            
            # Delete from disk using the same resolver used for reads so legacy
            # relative paths and migrated storage roots are cleaned up too.
            try:
                disk_path = self._resolve_file_disk_path(file_info.file_path)
                if disk_path.exists():
                    disk_path.unlink()
                    logger.info(f"File deleted from disk: {disk_path}")
            except Exception as e:
                logger.error(f"Failed to delete file from disk: {e}")
                # Don't fail the whole operation if disk deletion fails
            
            logger.info(f"File deleted successfully: {file_id}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to delete file {file_id}: {e}", exc_info=True)
            return False

    def is_file_referenced(self,
                           file_id: str,
                           exclude_channel_message_id: Optional[str] = None,
                           exclude_feed_post_id: Optional[str] = None,
                           exclude_message_id: Optional[str] = None) -> bool:
        """Check if a file is still referenced by any content.

        Returns True if referenced, False if safe to delete.
        """
        if not file_id:
            return False
        try:
            with self.db.get_connection() as conn:
                # Channel message attachments (JSON list)
                try:
                    query = "SELECT id, attachments FROM channel_messages WHERE attachments LIKE ?"
                    params: List[Any] = [f'%{file_id}%']
                    if exclude_channel_message_id:
                        query += " AND id != ?"
                        params.append(exclude_channel_message_id)
                    rows = conn.execute(query, params).fetchall()
                    for row in rows:
                        try:
                            parsed = json.loads(row['attachments'] or '[]')
                            if any(self._attachment_references_file(att, file_id) for att in parsed):
                                return True
                        except Exception:
                            continue
                except Exception:
                    pass

                # Feed post attachments (metadata JSON)
                try:
                    query = "SELECT id, metadata FROM feed_posts WHERE metadata LIKE ?"
                    params = [f'%{file_id}%']
                    if exclude_feed_post_id:
                        query += " AND id != ?"
                        params.append(exclude_feed_post_id)
                    rows = conn.execute(query, params).fetchall()
                    for row in rows:
                        try:
                            meta = json.loads(row['metadata'] or '{}')
                            atts = (meta or {}).get('attachments') or []
                            if any(self._attachment_references_file(att, file_id) for att in atts):
                                return True
                        except Exception:
                            continue
                except Exception:
                    pass

                # Feed post legacy references in content
                try:
                    query = "SELECT 1 FROM feed_posts WHERE content LIKE ?"
                    params = [f'%/files/{file_id}%']
                    if exclude_feed_post_id:
                        query += " AND id != ?"
                        params.append(exclude_feed_post_id)
                    row = conn.execute(query, params).fetchone()
                    if row:
                        return True
                except Exception:
                    pass

                # Direct message attachments (metadata JSON)
                try:
                    query = "SELECT id, metadata FROM messages WHERE metadata LIKE ?"
                    params = [f'%{file_id}%']
                    if exclude_message_id:
                        query += " AND id != ?"
                        params.append(exclude_message_id)
                    rows = conn.execute(query, params).fetchall()
                    for row in rows:
                        try:
                            meta = json.loads(row['metadata'] or '{}')
                            atts = (meta or {}).get('attachments') or []
                            if any(self._attachment_references_file(att, file_id) for att in atts):
                                return True
                        except Exception:
                            continue
                except Exception:
                    pass

                # Direct message legacy references in content
                try:
                    query = "SELECT 1 FROM messages WHERE content LIKE ?"
                    params = [f'%/files/{file_id}%']
                    if exclude_message_id:
                        query += " AND id != ?"
                        params.append(exclude_message_id)
                    row = conn.execute(query, params).fetchone()
                    if row:
                        return True
                except Exception:
                    pass

                # Channel message legacy references in content
                try:
                    query = "SELECT 1 FROM channel_messages WHERE content LIKE ?"
                    params = [f'%/files/{file_id}%']
                    if exclude_channel_message_id:
                        query += " AND id != ?"
                        params.append(exclude_channel_message_id)
                    row = conn.execute(query, params).fetchone()
                    if row:
                        return True
                except Exception:
                    pass

                # Comments may embed file URLs in content (best-effort)
                try:
                    has_comments = conn.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='comments'"
                    ).fetchone()
                    if has_comments:
                        row = conn.execute(
                            "SELECT 1 FROM comments WHERE content LIKE ? LIMIT 1",
                            (f'%{file_id}%',)
                        ).fetchone()
                        if row:
                            return True
                except Exception:
                    pass

            return False
        except Exception as e:
            logger.debug(f"File reference check failed for {file_id}: {e}")
            # Fail-safe: if in doubt, consider referenced
            return True

    @staticmethod
    def _attachment_references_file(attachment: Any, file_id: str) -> bool:
        if not isinstance(attachment, dict):
            return False
        for key in ('id', 'file_id', 'vault_file_id', 'origin_file_id', 'remote_file_id'):
            if str(attachment.get(key) or '').strip() == file_id:
                return True
        return False
    
    def list_user_files(
        self,
        user_id: str,
        *,
        limit: int = 50,
        offset: int = 0,
        query: Optional[str] = None,
        category: Optional[str] = None,
        folder_id: Optional[str] = None,
    ) -> List[FileInfo]:
        """Get files uploaded by a specific user.
        
        Args:
            user_id: User ID
            limit: Maximum number of files to return
            offset: Number of matching files to skip
            query: Optional filename/content-type search term
            category: Optional category filter (image, video, audio, document, other)
            
        Returns:
            List of FileInfo objects
        """
        logger.debug(f"Getting files for user {user_id}")
        
        try:
            limit = max(1, min(int(limit or 50), 500))
            offset = max(0, int(offset or 0))
            clauses = ["uploaded_by = ?"]
            params: List[Any] = [user_id]

            if folder_id is not None:
                folder_clean = str(folder_id or '').strip()
                if folder_clean:
                    clauses.append("vault_folder_id = ?")
                    params.append(folder_clean)
                else:
                    clauses.append("(vault_folder_id IS NULL OR vault_folder_id = '')")

            search = str(query or '').strip()
            if search:
                clauses.append("(LOWER(original_name) LIKE ? OR LOWER(content_type) LIKE ?)")
                needle = f"%{search.lower()}%"
                params.extend([needle, needle])

            category_key = str(category or '').strip().lower()
            if category_key in {'image', 'images'}:
                clauses.append("content_type LIKE 'image/%'")
            elif category_key in {'video', 'videos'}:
                clauses.append("content_type LIKE 'video/%'")
            elif category_key in {'audio'}:
                clauses.append("content_type LIKE 'audio/%'")
            elif category_key in {'document', 'documents', 'doc', 'docs'}:
                clauses.append(
                    "(content_type LIKE 'text/%' OR content_type IN ("
                    "'application/pdf',"
                    "'application/msword',"
                    "'application/vnd.openxmlformats-officedocument.wordprocessingml.document',"
                    "'application/vnd.ms-word.document.macroenabled.12',"
                    "'application/vnd.openxmlformats-officedocument.wordprocessingml.template',"
                    "'application/rtf',"
                    "'text/rtf',"
                    "'application/vnd.oasis.opendocument.text',"
                    "'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',"
                    "'application/vnd.ms-excel.sheet.macroenabled.12',"
                    "'application/vnd.ms-excel',"
                    "'application/vnd.ms-excel.sheet.binary.macroenabled.12',"
                    "'application/vnd.oasis.opendocument.spreadsheet',"
                    "'application/vnd.ms-powerpoint',"
                    "'application/vnd.openxmlformats-officedocument.presentationml.presentation',"
                    "'application/vnd.ms-powerpoint.presentation.macroenabled.12',"
                    "'application/vnd.openxmlformats-officedocument.presentationml.slideshow',"
                    "'application/vnd.openxmlformats-officedocument.presentationml.template',"
                    "'application/vnd.oasis.opendocument.presentation',"
                    "'message/rfc822',"
                    "'application/vnd.ms-outlook',"
                    "'application/vnd.apple.pages',"
                    "'application/vnd.apple.numbers',"
                    "'application/vnd.apple.keynote',"
                    "'application/json',"
                    "'application/xml'"
                    "))"
                )
            elif category_key in {'other', 'archive', 'archives'}:
                clauses.append(
                    "content_type NOT LIKE 'image/%' "
                    "AND content_type NOT LIKE 'video/%' "
                    "AND content_type NOT LIKE 'audio/%' "
                    "AND content_type NOT LIKE 'text/%' "
                    "AND content_type NOT IN ("
                    "'application/pdf',"
                    "'application/msword',"
                    "'application/vnd.openxmlformats-officedocument.wordprocessingml.document',"
                    "'application/vnd.ms-word.document.macroenabled.12',"
                    "'application/vnd.openxmlformats-officedocument.wordprocessingml.template',"
                    "'application/rtf',"
                    "'text/rtf',"
                    "'application/vnd.oasis.opendocument.text',"
                    "'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',"
                    "'application/vnd.ms-excel.sheet.macroenabled.12',"
                    "'application/vnd.ms-excel',"
                    "'application/vnd.ms-excel.sheet.binary.macroenabled.12',"
                    "'application/vnd.oasis.opendocument.spreadsheet',"
                    "'application/vnd.ms-powerpoint',"
                    "'application/vnd.openxmlformats-officedocument.presentationml.presentation',"
                    "'application/vnd.ms-powerpoint.presentation.macroenabled.12',"
                    "'application/vnd.openxmlformats-officedocument.presentationml.slideshow',"
                    "'application/vnd.openxmlformats-officedocument.presentationml.template',"
                    "'application/vnd.oasis.opendocument.presentation',"
                    "'message/rfc822',"
                    "'application/vnd.ms-outlook',"
                    "'application/vnd.apple.pages',"
                    "'application/vnd.apple.numbers',"
                    "'application/vnd.apple.keynote',"
                    "'application/json',"
                    "'application/xml'"
                    ")"
                )

            where_sql = " AND ".join(clauses)
            params.extend([limit, offset])
            with self.db.get_connection() as conn:
                cursor = conn.execute(f"""
                    SELECT id, original_name, stored_name, file_path, content_type,
                           size, uploaded_by, uploaded_at, checksum, vault_folder_id
                    FROM files
                    WHERE {where_sql}
                    ORDER BY uploaded_at DESC
                    LIMIT ? OFFSET ?
                """, params)
                
                files = []
                for row in cursor.fetchall():
                    files.append(FileInfo(
                        id=row['id'],
                        original_name=row['original_name'],
                        stored_name=row['stored_name'],
                        file_path=row['file_path'],
                        content_type=row['content_type'],
                        size=row['size'],
                        uploaded_by=row['uploaded_by'],
                        uploaded_at=datetime.fromisoformat(row['uploaded_at']),
                        url=f"/files/{row['id']}",
                        checksum=row['checksum'],
                        vault_folder_id=row['vault_folder_id'] if 'vault_folder_id' in row.keys() else None,
                    ))
                
                logger.debug(f"Found {len(files)} files for user {user_id}")
                return files
                
        except Exception as e:
            logger.error(f"Failed to get files for user {user_id}: {e}", exc_info=True)
            return []

    @staticmethod
    def _vault_folder_from_row(row: Any) -> VaultFolder:
        return VaultFolder(
            id=row['id'],
            user_id=row['user_id'],
            name=row['name'],
            parent_id=row['parent_id'],
            created_at=datetime.fromisoformat(row['created_at']),
            updated_at=datetime.fromisoformat(row['updated_at']),
        )

    @staticmethod
    def _normalize_vault_folder_name(name: Any) -> str:
        clean = ' '.join(str(name or '').strip().split())
        clean = clean.strip('/\\')
        if not clean:
            raise ValueError('Folder name is required.')
        if len(clean) > 80:
            raise ValueError('Folder name must be 80 characters or fewer.')
        if clean in {'.', '..'}:
            raise ValueError('Choose a different folder name.')
        return clean

    def get_user_folder(self, user_id: str, folder_id: Optional[str]) -> Optional[VaultFolder]:
        folder_clean = str(folder_id or '').strip()
        if not folder_clean:
            return None
        try:
            with self.db.get_connection() as conn:
                row = conn.execute(
                    """
                    SELECT id, user_id, name, parent_id, created_at, updated_at
                    FROM vault_folders
                    WHERE id = ? AND user_id = ?
                    """,
                    (folder_clean, user_id),
                ).fetchone()
            return self._vault_folder_from_row(row) if row else None
        except Exception as e:
            logger.error("Failed to load vault folder %s for user %s: %s", folder_id, user_id, e, exc_info=True)
            return None

    def get_user_folder_by_name(self, user_id: str, name: Any, parent_id: Optional[str] = None) -> Optional[VaultFolder]:
        """Return an existing user folder with the given normalized name and parent."""
        try:
            name_clean = self._normalize_vault_folder_name(name)
        except ValueError:
            return None
        parent_clean = str(parent_id or '').strip() or None
        try:
            with self.db.get_connection() as conn:
                row = conn.execute(
                    """
                    SELECT id, user_id, name, parent_id, created_at, updated_at
                    FROM vault_folders
                    WHERE user_id = ? AND name = ? AND (
                        (? IS NULL AND (parent_id IS NULL OR parent_id = '')) OR parent_id = ?
                    )
                    LIMIT 1
                    """,
                    (user_id, name_clean, parent_clean, parent_clean or ''),
                ).fetchone()
            return self._vault_folder_from_row(row) if row else None
        except Exception as e:
            logger.error("Failed to resolve vault folder %s for user %s: %s", name, user_id, e, exc_info=True)
            return None

    def ensure_user_folder_path(self, user_id: str, parts: list[Any], parent_id: Optional[str] = None) -> Optional[VaultFolder]:
        """Create or reuse a nested folder path for a user's Vault."""
        current_parent = str(parent_id or '').strip() or None
        current: Optional[VaultFolder] = None
        for raw_part in parts:
            name = self._normalize_vault_folder_name(raw_part)
            current = self.get_user_folder_by_name(user_id, name, current_parent)
            if current is None:
                current = self.create_user_folder(user_id, name, current_parent)
            current_parent = current.id
        return current

    def list_user_folders(self, user_id: str, parent_id: Optional[str] = None) -> List[VaultFolder]:
        parent_clean = str(parent_id or '').strip()
        try:
            with self.db.get_connection() as conn:
                if parent_clean:
                    rows = conn.execute(
                        """
                        SELECT id, user_id, name, parent_id, created_at, updated_at
                        FROM vault_folders
                        WHERE user_id = ? AND parent_id = ?
                        ORDER BY LOWER(name), name
                        """,
                        (user_id, parent_clean),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        """
                        SELECT id, user_id, name, parent_id, created_at, updated_at
                        FROM vault_folders
                        WHERE user_id = ? AND (parent_id IS NULL OR parent_id = '')
                        ORDER BY LOWER(name), name
                        """,
                        (user_id,),
                    ).fetchall()
            return [self._vault_folder_from_row(row) for row in rows]
        except Exception as e:
            logger.error("Failed to list vault folders for user %s: %s", user_id, e, exc_info=True)
            return []

    def create_user_folder(self, user_id: str, name: Any, parent_id: Optional[str] = None) -> VaultFolder:
        name_clean = self._normalize_vault_folder_name(name)
        parent_clean = str(parent_id or '').strip() or None
        if parent_clean and not self.get_user_folder(user_id, parent_clean):
            raise ValueError('Parent folder was not found.')
        folder_id = f"V{secrets.token_hex(8)}"
        now = datetime.now(timezone.utc)
        try:
            with self.db.get_connection() as conn:
                duplicate = conn.execute(
                    """
                    SELECT id FROM vault_folders
                    WHERE user_id = ? AND name = ? AND (
                        (? IS NULL AND (parent_id IS NULL OR parent_id = '')) OR parent_id = ?
                    )
                    LIMIT 1
                    """,
                    (user_id, name_clean, parent_clean, parent_clean or ''),
                ).fetchone()
                if duplicate:
                    raise ValueError('A folder with that name already exists here.')
                conn.execute(
                    """
                    INSERT INTO vault_folders (id, user_id, name, parent_id, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (folder_id, user_id, name_clean, parent_clean, now.isoformat(), now.isoformat()),
                )
                conn.commit()
            folder = self.get_user_folder(user_id, folder_id)
            if not folder:
                raise ValueError('Folder could not be created.')
            return folder
        except ValueError:
            raise
        except Exception as e:
            logger.error("Failed to create vault folder for user %s: %s", user_id, e, exc_info=True)
            raise ValueError('Folder could not be created.') from e

    def rename_user_folder(self, user_id: str, folder_id: str, name: Any) -> VaultFolder:
        folder = self.get_user_folder(user_id, folder_id)
        if not folder:
            raise ValueError('Folder was not found.')
        name_clean = self._normalize_vault_folder_name(name)
        try:
            with self.db.get_connection() as conn:
                duplicate = conn.execute(
                    """
                    SELECT id FROM vault_folders
                    WHERE user_id = ? AND id != ? AND name = ? AND (
                        (? IS NULL AND (parent_id IS NULL OR parent_id = '')) OR parent_id = ?
                    )
                    LIMIT 1
                    """,
                    (user_id, folder.id, name_clean, folder.parent_id, folder.parent_id or ''),
                ).fetchone()
                if duplicate:
                    raise ValueError('A folder with that name already exists here.')
                conn.execute(
                    "UPDATE vault_folders SET name = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ? AND user_id = ?",
                    (name_clean, folder.id, user_id),
                )
                conn.commit()
            updated = self.get_user_folder(user_id, folder.id)
            if not updated:
                raise ValueError('Folder was not found.')
            return updated
        except ValueError:
            raise
        except Exception as e:
            logger.error("Failed to rename vault folder %s: %s", folder_id, e, exc_info=True)
            raise ValueError('Folder could not be renamed.') from e

    def delete_user_folder(self, user_id: str, folder_id: str) -> bool:
        folder = self.get_user_folder(user_id, folder_id)
        if not folder:
            raise ValueError('Folder was not found.')
        try:
            with self.db.get_connection() as conn:
                child = conn.execute(
                    "SELECT 1 FROM vault_folders WHERE user_id = ? AND parent_id = ? LIMIT 1",
                    (user_id, folder.id),
                ).fetchone()
                if child:
                    raise ValueError('Move or delete nested folders first.')
                file_row = conn.execute(
                    "SELECT 1 FROM files WHERE uploaded_by = ? AND vault_folder_id = ? LIMIT 1",
                    (user_id, folder.id),
                ).fetchone()
                if file_row:
                    raise ValueError('Move files out of this folder before deleting it.')
                conn.execute("DELETE FROM vault_folders WHERE id = ? AND user_id = ?", (folder.id, user_id))
                conn.commit()
            return True
        except ValueError:
            raise
        except Exception as e:
            logger.error("Failed to delete vault folder %s: %s", folder_id, e, exc_info=True)
            raise ValueError('Folder could not be deleted.') from e

    def move_user_file_to_folder(self, user_id: str, file_id: str, folder_id: Optional[str]) -> FileInfo:
        file_info = self.get_file(file_id)
        if not file_info or str(file_info.uploaded_by) != str(user_id):
            raise ValueError('File was not found in your Vault.')
        folder_clean = str(folder_id or '').strip() or None
        if folder_clean and not self.get_user_folder(user_id, folder_clean):
            raise ValueError('Destination folder was not found.')
        try:
            with self.db.get_connection() as conn:
                conn.execute(
                    "UPDATE files SET vault_folder_id = ? WHERE id = ? AND uploaded_by = ?",
                    (folder_clean, file_id, user_id),
                )
                conn.commit()
            updated = self.get_file(file_id)
            if not updated:
                raise ValueError('File was not found in your Vault.')
            return updated
        except ValueError:
            raise
        except Exception as e:
            logger.error("Failed to move vault file %s: %s", file_id, e, exc_info=True)
            raise ValueError('File could not be moved.') from e

    def get_user_folder_path(self, user_id: str, folder_id: Optional[str]) -> List[VaultFolder]:
        path: List[VaultFolder] = []
        seen: set[str] = set()
        current = self.get_user_folder(user_id, folder_id)
        while current and current.id not in seen:
            path.append(current)
            seen.add(current.id)
            current = self.get_user_folder(user_id, current.parent_id)
        path.reverse()
        return path

    def count_user_files(self, user_id: str, *, query: Optional[str] = None) -> Dict[str, Any]:
        """Return lightweight vault statistics for a user's local files."""
        try:
            clauses = ["uploaded_by = ?"]
            params: List[Any] = [user_id]
            search = str(query or '').strip()
            if search:
                clauses.append("(LOWER(original_name) LIKE ? OR LOWER(content_type) LIKE ?)")
                needle = f"%{search.lower()}%"
                params.extend([needle, needle])
            where_sql = " AND ".join(clauses)
            with self.db.get_connection() as conn:
                row = conn.execute(
                    f"""
                    SELECT COUNT(*) AS count, COALESCE(SUM(size), 0) AS bytes
                    FROM files
                    WHERE {where_sql}
                    """,
                    params,
                ).fetchone()
                by_kind_rows = conn.execute(
                    f"""
                    SELECT
                        CASE
                            WHEN content_type LIKE 'image/%' THEN 'images'
                            WHEN content_type LIKE 'video/%' THEN 'videos'
                            WHEN content_type LIKE 'audio/%' THEN 'audio'
                            WHEN content_type LIKE 'text/%' OR content_type IN (
                                'application/pdf',
                                'application/msword',
                                'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
                                'application/vnd.ms-word.document.macroenabled.12',
                                'application/vnd.openxmlformats-officedocument.wordprocessingml.template',
                                'application/rtf',
                                'text/rtf',
                                'application/vnd.oasis.opendocument.text',
                                'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                                'application/vnd.ms-excel.sheet.macroenabled.12',
                                'application/vnd.ms-excel',
                                'application/vnd.ms-excel.sheet.binary.macroenabled.12',
                                'application/vnd.oasis.opendocument.spreadsheet',
                                'application/vnd.ms-powerpoint',
                                'application/vnd.openxmlformats-officedocument.presentationml.presentation',
                                'application/vnd.ms-powerpoint.presentation.macroenabled.12',
                                'application/vnd.openxmlformats-officedocument.presentationml.slideshow',
                                'application/vnd.openxmlformats-officedocument.presentationml.template',
                                'application/vnd.oasis.opendocument.presentation',
                                'message/rfc822',
                                'application/vnd.ms-outlook',
                                'application/vnd.apple.pages',
                                'application/vnd.apple.numbers',
                                'application/vnd.apple.keynote',
                                'application/json',
                                'application/xml'
                            ) THEN 'documents'
                            ELSE 'other'
                        END AS category,
                        COUNT(*) AS count,
                        COALESCE(SUM(size), 0) AS bytes
                    FROM files
                    WHERE {where_sql}
                    GROUP BY category
                    """,
                    params,
                ).fetchall()
            return {
                'count': int(row['count'] if row else 0),
                'bytes': int(row['bytes'] if row else 0),
                'by_category': {
                    str(r['category']): {
                        'count': int(r['count'] or 0),
                        'bytes': int(r['bytes'] or 0),
                    }
                    for r in by_kind_rows
                },
            }
        except Exception as e:
            logger.error(f"Failed to count files for user {user_id}: {e}", exc_info=True)
            return {'count': 0, 'bytes': 0, 'by_category': {}}

    def get_user_files(self, user_id: str, limit: int = 50) -> List[FileInfo]:
        """Backward-compatible wrapper for older callers."""
        return self.list_user_files(user_id, limit=limit)
    
    def get_file_stats(self) -> Dict[str, Any]:
        """Get file storage statistics.
        
        Returns:
            Dictionary with storage statistics
        """
        try:
            with self.db.get_connection() as conn:
                cursor = conn.execute("""
                    SELECT 
                        COUNT(*) as total_files,
                        SUM(size) as total_size,
                        AVG(size) as avg_size,
                        MAX(size) as max_size,
                        COUNT(DISTINCT uploaded_by) as unique_uploaders
                    FROM files
                """)
                
                stats = dict(cursor.fetchone())
                
                # Get file counts by type
                cursor = conn.execute("""
                    SELECT 
                        CASE 
                            WHEN content_type LIKE 'image/%' THEN 'images'
                            WHEN content_type LIKE 'video/%' THEN 'videos'
                            WHEN content_type LIKE 'audio/%' THEN 'audio'
                            WHEN content_type IN (
                                'application/pdf',
                                'text/plain',
                                'application/msword',
                                'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
                                'application/vnd.ms-word.document.macroenabled.12',
                                'application/vnd.openxmlformats-officedocument.wordprocessingml.template',
                                'application/rtf',
                                'text/rtf',
                                'application/vnd.oasis.opendocument.text',
                                'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                                'application/vnd.ms-excel.sheet.macroenabled.12',
                                'application/vnd.ms-excel',
                                'application/vnd.ms-excel.sheet.binary.macroenabled.12',
                                'application/vnd.oasis.opendocument.spreadsheet',
                                'application/vnd.ms-powerpoint',
                                'application/vnd.openxmlformats-officedocument.presentationml.presentation',
                                'application/vnd.ms-powerpoint.presentation.macroenabled.12',
                                'application/vnd.openxmlformats-officedocument.presentationml.slideshow',
                                'application/vnd.openxmlformats-officedocument.presentationml.template',
                                'application/vnd.oasis.opendocument.presentation',
                                'message/rfc822',
                                'application/vnd.ms-outlook',
                                'application/vnd.apple.pages',
                                'application/vnd.apple.numbers',
                                'application/vnd.apple.keynote'
                            ) THEN 'documents'
                            ELSE 'other'
                        END as category,
                        COUNT(*) as count,
                        SUM(size) as size
                    FROM files
                    GROUP BY category
                """)
                
                stats['by_category'] = {}
                for row in cursor.fetchall():
                    stats['by_category'][row['category']] = {
                        'count': row['count'],
                        'size': row['size']
                    }
                
                return stats
                
        except Exception as e:
            logger.error(f"Failed to get file stats: {e}", exc_info=True)
            return {}
