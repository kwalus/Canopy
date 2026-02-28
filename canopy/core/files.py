"""
File management system for Canopy.
Handles file uploads, storage, and serving.

Author: Konrad Walus (architecture, design, and direction)
Project: Canopy - Local Mesh Communication
License: Apache 2.0
Development: AI-assisted implementation (Claude, Codex, GitHub Copilot, Cursor IDE, Ollama)
"""

import logging
import os
import secrets
import hashlib
from pathlib import Path
from typing import Dict, List, Optional, Any, BinaryIO
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
import json
import base64

from .database import DatabaseManager
from .logging_config import log_performance, LogOperation

# Pillow for thumbnail generation (optional — graceful degradation)
try:
    from PIL import Image
    import io as _io
    _PILLOW_AVAILABLE = True
except ImportError:
    _PILLOW_AVAILABLE = False

logger = logging.getLogger('canopy.files')

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

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        data = asdict(self)
        data['uploaded_at'] = self.uploaded_at.isoformat()
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
        '.tex': 'text/x-tex',
        '.latex': 'application/x-latex',
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
    }
    _MIME_TO_EXT = {
        'application/pdf': '.pdf',
        'text/markdown': '.md',
        'text/plain': '.txt',
        'application/json': '.json',
        'text/csv': '.csv',
        'text/x-tex': '.tex',
        'application/x-latex': '.tex',
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

        def _add(path: Path) -> None:
            p = path.expanduser()
            if p not in roots:
                roots.append(p)

        _add(self.storage_path)

        # Legacy shared locations (before strict per-device file roots).
        _add(self._project_root / 'data' / 'files')
        _add(Path.cwd() / 'data' / 'files')

        # If storage path is device-scoped (.../devices/<id>/files), add common alternates.
        parts = list(self.storage_path.parts)
        if 'devices' in parts:
            idx = parts.index('devices')
            if idx + 2 < len(parts):
                device_id = parts[idx + 1]
                _add(self._project_root / 'data' / 'devices' / device_id / 'files')
                _add(Path.cwd() / 'data' / 'devices' / device_id / 'files')
                _add(Path.home() / '.canopy' / 'data' / 'devices' / device_id / 'files')

        return roots

    def _resolve_file_disk_path(self, stored_path: str) -> Path:
        """Resolve a DB file_path to an on-disk file path with compatibility fallbacks."""
        normalized = str(stored_path or '').replace('\\', '/').strip()
        if not normalized:
            return self.storage_path / '__missing__'

        candidates: List[Path] = []

        def _add(path: Path) -> None:
            if path not in candidates:
                candidates.append(path)

        storage_roots = self._candidate_storage_roots()
        storage_prefix = str(self.storage_path).replace('\\', '/') + '/'
        path_obj = Path(normalized)

        if path_obj.is_absolute():
            _add(path_obj)

        # Relative paths that begin with data/... should be rooted at project or current CWD.
        if normalized.startswith('data/'):
            _add(self._project_root / normalized)
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
        return candidates[0] if candidates else (self.storage_path / normalized)
    
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
                        checksum TEXT NOT NULL,
                        FOREIGN KEY (uploaded_by) REFERENCES users (id)
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
                    CREATE INDEX IF NOT EXISTS idx_file_access_log_file_id ON file_access_log(file_id);
                """)
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
                              'text/plain', 'text/csv', 'text/markdown',
                              'text/x-tex', 'application/x-latex',
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

    def _thumb_path_for(self, original_path: Path) -> Path:
        """Return the expected thumbnail path for a given original file path."""
        stem = original_path.stem
        suffix = original_path.suffix
        return original_path.with_name(f"{stem}_thumb{suffix}")

    def _generate_thumbnail(self, file_data: bytes, original_path: Path,
                            file_extension: str) -> None:
        """Generate a resized thumbnail alongside the original image.

        Best-effort: failures are logged but never propagate.
        """
        try:
            img: Any = Image.open(_io.BytesIO(file_data))
            # Skip tiny images that are already smaller than the thumb size
            w, h = img.size
            if max(w, h) <= self.THUMB_MAX_SIZE:
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

        Returns (thumb_bytes, file_info) or falls back to the original
        if no thumbnail exists.  Returns None if file not found at all.
        """
        file_info = self.get_file(file_id)
        if not file_info:
            return None

        original_path = self._resolve_file_disk_path(file_info.file_path)

        thumb_path = self._thumb_path_for(original_path)
        target = thumb_path if thumb_path.exists() else original_path

        if not target.exists():
            logger.error(f"Neither thumb nor original found for {file_id}")
            return None

        try:
            with open(target, 'rb') as f:
                data = f.read()
            return data, file_info
        except Exception as e:
            logger.error(f"Failed to read thumbnail for {file_id}: {e}")
            return None

    @log_performance('files')
    def save_file(self, file_data: bytes, original_name: str, content_type: str, 
                  uploaded_by: str) -> Optional[FileInfo]:
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

            # Validate file size
            if len(file_data) > self.max_file_size:
                logger.error(f"File too large: {len(file_data)} bytes (max: {self.max_file_size})")
                return None
            
            # Generate unique file ID and stored name
            file_id = f"F{secrets.token_hex(12)}"
            file_extension = Path(original_name).suffix.lower()
            stored_name = f"{file_id}{file_extension}"
            
            # Determine storage category and path
            category = self._get_file_category(content_type)
            file_path = self.storage_path / category / stored_name

            # Verify the resolved path is within storage directory (prevent path traversal)
            try:
                file_path = file_path.resolve()
                storage_path_resolved = self.storage_path.resolve()
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
                checksum=checksum
            )
            
            # Save to database
            with LogOperation(f"Saving file metadata to database: {file_id}"):
                with self.db.get_connection() as conn:
                    conn.execute("""
                        INSERT INTO files (id, original_name, stored_name, file_path, 
                                         content_type, size, uploaded_by, checksum)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        file_info.id, file_info.original_name, file_info.stored_name,
                        file_info.file_path, file_info.content_type, file_info.size,
                        file_info.uploaded_by, file_info.checksum
                    ))
                    conn.commit()
            
            # Generate thumbnail for images (best-effort)
            if _PILLOW_AVAILABLE and content_type.startswith('image/'):
                self._generate_thumbnail(file_data, file_path, file_extension)

            logger.info(f"File saved successfully: {file_id} -> {file_path}")
            return file_info
            
        except Exception as e:
            logger.error(f"Failed to save file {original_name}: {e}", exc_info=True)
            return None
    
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
                           size, uploaded_by, uploaded_at, checksum
                    FROM files WHERE id = ?
                """, (file_id,))
                
                row = cursor.fetchone()
                if not row:
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
                    checksum=row['checksum']
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
                conn.execute("DELETE FROM file_access_log WHERE file_id = ?", (file_id,))
                conn.execute("DELETE FROM files WHERE id = ?", (file_id,))
                conn.commit()
            
            # Delete from disk
            try:
                if os.path.exists(file_info.file_path):
                    os.remove(file_info.file_path)
                    logger.info(f"File deleted from disk: {file_info.file_path}")
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
                    params: List[Any] = [f'%\"id\":\"{file_id}\"%']
                    if exclude_channel_message_id:
                        query += " AND id != ?"
                        params.append(exclude_channel_message_id)
                    rows = conn.execute(query, params).fetchall()
                    for row in rows:
                        try:
                            parsed = json.loads(row['attachments'] or '[]')
                            if any(isinstance(att, dict) and att.get('id') == file_id for att in parsed):
                                return True
                        except Exception:
                            continue
                except Exception:
                    pass

                # Feed post attachments (metadata JSON)
                try:
                    query = "SELECT id, metadata FROM feed_posts WHERE metadata LIKE ?"
                    params = [f'%\"id\":\"{file_id}\"%']
                    if exclude_feed_post_id:
                        query += " AND id != ?"
                        params.append(exclude_feed_post_id)
                    rows = conn.execute(query, params).fetchall()
                    for row in rows:
                        try:
                            meta = json.loads(row['metadata'] or '{}')
                            atts = (meta or {}).get('attachments') or []
                            if any(isinstance(att, dict) and att.get('id') == file_id for att in atts):
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
                    params = [f'%\"id\":\"{file_id}\"%']
                    if exclude_message_id:
                        query += " AND id != ?"
                        params.append(exclude_message_id)
                    rows = conn.execute(query, params).fetchall()
                    for row in rows:
                        try:
                            meta = json.loads(row['metadata'] or '{}')
                            atts = (meta or {}).get('attachments') or []
                            if any(isinstance(att, dict) and att.get('id') == file_id for att in atts):
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
    
    def get_user_files(self, user_id: str, limit: int = 50) -> List[FileInfo]:
        """Get files uploaded by a specific user.
        
        Args:
            user_id: User ID
            limit: Maximum number of files to return
            
        Returns:
            List of FileInfo objects
        """
        logger.debug(f"Getting files for user {user_id}")
        
        try:
            with self.db.get_connection() as conn:
                cursor = conn.execute("""
                    SELECT id, original_name, stored_name, file_path, content_type, 
                           size, uploaded_by, uploaded_at, checksum
                    FROM files 
                    WHERE uploaded_by = ?
                    ORDER BY uploaded_at DESC
                    LIMIT ?
                """, (user_id, limit))
                
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
                        checksum=row['checksum']
                    ))
                
                logger.debug(f"Found {len(files)} files for user {user_id}")
                return files
                
        except Exception as e:
            logger.error(f"Failed to get files for user {user_id}: {e}", exc_info=True)
            return []
    
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
                            WHEN content_type IN ('application/pdf', 'text/plain', 'application/msword') THEN 'documents'
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
