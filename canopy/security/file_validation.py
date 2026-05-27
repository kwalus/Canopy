"""
File upload validation and security checks for Canopy.

Validates file types, sizes, and content to prevent malicious uploads.

Project: Canopy - Local Mesh Communication
License: Apache 2.0
"""

from __future__ import annotations

import io
import logging
import os
import zipfile
from html.parser import HTMLParser
from typing import Any, Optional, Tuple

logger = logging.getLogger(__name__)


CANOPY_MODULE_SUFFIXES = ('.canopy-module.html', '.canopy-module.htm')
CANOPY_MODULE_MAX_BYTES = 300 * 1024
DEFAULT_UPLOAD_MAX_BYTES = 100 * 1024 * 1024
DEFAULT_VAULT_UPLOAD_MAX_BYTES = 512 * 1024 * 1024


def format_upload_size_limit(size_bytes: Any) -> str:
    """Return a compact human-readable upload limit label."""
    try:
        value = float(size_bytes or 0)
    except (TypeError, ValueError):
        value = 0
    if value <= 0:
        return "0 B"
    units = ("B", "KB", "MB", "GB", "TB")
    unit_index = 0
    while value >= 1024 and unit_index < len(units) - 1:
        value /= 1024
        unit_index += 1
    precision = 0 if value >= 10 or unit_index == 0 else 1
    return f"{value:.{precision}f} {units[unit_index]}"


def resolve_upload_max_bytes(
    config: Any,
    *,
    config_key: str,
    env_key: str,
    default: int,
) -> int:
    """Resolve an upload size cap from Flask config/env with a safe default."""
    candidates: list[Any] = []
    if config is not None:
        try:
            candidates.append(config.get(config_key))
        except AttributeError:
            candidates.append(getattr(config, config_key, None))
    candidates.append(os.getenv(env_key))
    candidates.append(default)
    for candidate in candidates:
        if candidate is None or candidate == '':
            continue
        try:
            value = int(candidate)
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return int(default)


def get_standard_upload_max_bytes(config: Any = None) -> int:
    """Return the post/message attachment upload cap."""
    return resolve_upload_max_bytes(
        config,
        config_key="MAX_FILE_SIZE",
        env_key="CANOPY_MAX_FILE_SIZE",
        default=DEFAULT_UPLOAD_MAX_BYTES,
    )


def get_vault_upload_max_bytes(config: Any = None) -> int:
    """Return the local File Vault upload cap, intentionally larger than post uploads."""
    return resolve_upload_max_bytes(
        config,
        config_key="MAX_VAULT_FILE_SIZE",
        env_key="CANOPY_MAX_VAULT_FILE_SIZE",
        default=DEFAULT_VAULT_UPLOAD_MAX_BYTES,
    )

SOURCE_CODE_EXT_TO_MIME = {
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
}
SOURCE_CODE_MIME_TO_EXTENSIONS: dict[str, list[str]] = {}
for _source_ext, _source_mime in SOURCE_CODE_EXT_TO_MIME.items():
    SOURCE_CODE_MIME_TO_EXTENSIONS.setdefault(_source_mime, []).append(_source_ext)
SOURCE_CODE_MIME_TO_EXTENSIONS.setdefault('text/javascript', ['.js', '.mjs', '.cjs'])
SOURCE_CODE_MIME_TO_EXTENSIONS.setdefault('text/typescript', ['.ts'])
SOURCE_CODE_MIME_TO_EXTENSIONS.setdefault('text/x-php', ['.php'])
SOURCE_CODE_MIME_TYPES = set(SOURCE_CODE_MIME_TO_EXTENSIONS)
SOURCE_CODE_VALIDATION_MIME_TYPES = SOURCE_CODE_MIME_TYPES - {'text/plain'}


# Extension-to-MIME mapping for when browsers send application/octet-stream
_EXT_TO_MIME = {
    # Images
    '.jpg': 'image/jpeg',
    '.jpeg': 'image/jpeg',
    '.png': 'image/png',
    '.gif': 'image/gif',
    '.webp': 'image/webp',
    '.bmp': 'image/bmp',
    '.svg': 'image/svg+xml',
    # Audio — agents uploading binary files typically send application/octet-stream
    '.mp3': 'audio/mpeg',
    '.wav': 'audio/wav',
    '.ogg': 'audio/ogg',
    '.oga': 'audio/ogg',
    '.m4a': 'audio/mp4',
    # Video
    '.mp4': 'video/mp4',
    '.m4v': 'video/mp4',
    '.webm': 'video/webm',
    '.mov': 'video/quicktime',
    # Documents / text
    '.pdf': 'application/pdf',
    '.tex': 'text/x-tex',
    '.sty': 'text/x-tex',
    '.cls': 'text/x-tex',
    '.bib': 'text/x-tex',
    '.bst': 'text/x-tex',
    '.latex': 'application/x-latex',
    '.ltx': 'application/x-latex',
    '.py': 'text/x-python',
    '.pyi': 'text/x-python',
    '.pyw': 'text/x-python',
    '.md': 'text/markdown',
    '.markdown': 'text/markdown',
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
    '.txt': 'text/plain',
    '.log': 'text/plain',
    '.cfg': 'text/plain',
    '.ini': 'text/plain',
    '.yml': 'text/plain',
    '.yaml': 'text/plain',
    '.toml': 'text/plain',
    '.json': 'application/json',
    '.xml': 'application/xml',
    '.html': 'text/html',
    '.htm': 'text/html',
    **SOURCE_CODE_EXT_TO_MIME,
    # Archives
    '.zip': 'application/zip',
    '.tar': 'application/x-tar',
    '.gz': 'application/gzip',
    '.gzip': 'application/gzip',
    '.tgz': 'application/gzip',
    '.bz2': 'application/x-bzip2',
    '.tbz2': 'application/x-bzip2',
    '.xz': 'application/x-xz',
    '.7z': 'application/x-7z-compressed',
    '.rar': 'application/vnd.rar',
}


def _infer_content_type(filename: str) -> Optional[str]:
    """Infer MIME type from filename extension when the browser-supplied type is generic.
    
    Handles compound extensions like .tar.gz by checking the final extension first.
    """
    if not filename:
        return None
    lower = filename.lower()
    # Check compound extensions first
    if lower.endswith('.tar.gz') or lower.endswith('.tar.gzip'):
        return 'application/gzip'
    ext = lower.rsplit('.', 1)[-1] if '.' in lower else ''
    return _EXT_TO_MIME.get(f'.{ext}')


def is_canopy_module_filename(filename: str | None) -> bool:
    lower = str(filename or '').strip().lower()
    return any(lower.endswith(suffix) for suffix in CANOPY_MODULE_SUFFIXES)


def _filename_extension(filename: str | None) -> str:
    lower = str(filename or '').strip().lower()
    return f".{lower.rsplit('.', 1)[-1]}" if '.' in lower else ''


def _has_safe_inline_module_resource_urls(file_str: str) -> bool:
    scanner = _CanopyModuleHTMLScanner()
    scanner.feed(file_str)
    scanner.close()
    return scanner.unsafe_resource_attr is None


class _CanopyModuleHTMLScanner(HTMLParser):
    """Inspect actual HTML tag attributes without treating JS bodies as markup."""

    _BLOCKED_TAGS = {'iframe', 'frame', 'frameset', 'object', 'embed', 'applet', 'base'}
    _RESOURCE_ATTRS = {'src', 'href', 'poster', 'action', 'formaction'}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.blocked_tag: Optional[str] = None
        self.external_script_src = False
        self.inline_event_attr: Optional[str] = None
        self.runtime_csp_override = False
        self.unsafe_resource_attr: Optional[tuple[str, str, str]] = None

    @staticmethod
    def _is_safe_inline_resource_url(value: str | None) -> bool:
        normalized = str(value or '').strip().lower()
        if not normalized:
            return True
        return normalized.startswith('data:') or normalized.startswith('blob:') or normalized.startswith('#')

    def handle_starttag(self, tag: str, attrs) -> None:
        self._inspect_tag(tag, attrs)

    def handle_startendtag(self, tag: str, attrs) -> None:
        self._inspect_tag(tag, attrs)

    def _inspect_tag(self, tag: str, attrs) -> None:
        tag_name = str(tag or '').strip().lower()
        if not tag_name:
            return
        if self.blocked_tag is None and tag_name in self._BLOCKED_TAGS:
            self.blocked_tag = tag_name
        for attr_name, attr_value in attrs:
            name = str(attr_name or '').strip().lower()
            value = str(attr_value or '').strip()
            if not name:
                continue
            if self.inline_event_attr is None and name.startswith('on'):
                self.inline_event_attr = name
            if tag_name == 'script' and name == 'src':
                self.external_script_src = True
            if tag_name == 'meta' and name == 'http-equiv' and value.lower() == 'content-security-policy':
                self.runtime_csp_override = True
            if (
                self.unsafe_resource_attr is None
                and name in self._RESOURCE_ATTRS
                and not self._is_safe_inline_resource_url(value)
            ):
                self.unsafe_resource_attr = (tag_name, name, value)


def _validate_canopy_module_bundle(file_data: bytes) -> tuple[bool, Optional[str]]:
    try:
        file_str = file_data.decode('utf-8', errors='strict')
    except UnicodeDecodeError:
        return False, "Canopy Module bundle must be valid UTF-8 HTML"

    lowered = file_str.lower()
    stripped = lowered.lstrip()

    if not (stripped.startswith('<!doctype') or stripped.startswith('<html')):
        return False, "Canopy Module bundle must be a complete HTML document"

    if len(file_data) > CANOPY_MODULE_MAX_BYTES:
        return False, (
            f"Canopy Module bundle exceeds the v1 size budget of {CANOPY_MODULE_MAX_BYTES} bytes"
        )

    blocked_substrings = [
        'javascript:',
    ]
    for pattern in blocked_substrings:
        if pattern in lowered:
            return False, "Canopy Module bundle contains a blocked HTML feature"

    scanner = _CanopyModuleHTMLScanner()
    scanner.feed(file_str)
    scanner.close()

    if scanner.blocked_tag:
        return False, "Canopy Module bundle contains a blocked HTML feature"
    if scanner.external_script_src:
        return False, "Canopy Module bundle cannot load external scripts"
    if scanner.inline_event_attr:
        return False, "Canopy Module bundle cannot use inline event handler attributes"
    if scanner.runtime_csp_override:
        return False, "Canopy Module bundle cannot override the Canopy runtime CSP"
    if scanner.unsafe_resource_attr is not None:
        return False, "Canopy Module bundle must be self-contained (data/blob/hash URLs only)"

    return True, None


# Allowed MIME types and their magic bytes signatures
ALLOWED_TYPES = {
    # Images
    'image/jpeg': [
        b'\xFF\xD8\xFF',  # JPEG
    ],
    'image/png': [
        b'\x89PNG\r\n\x1a\n',  # PNG
    ],
    'image/gif': [
        b'GIF87a',  # GIF87a
        b'GIF89a',  # GIF89a
    ],
    'image/webp': [
        b'RIFF',  # WebP (RIFF container, need to check WEBP later in file)
    ],
    'image/bmp': [
        b'BM',  # BMP
    ],
    'image/svg+xml': [
        b'<?xml',  # SVG
        b'<svg',   # SVG without XML declaration
    ],
    
    # Audio
    'audio/mpeg': [
        b'ID3',       # MP3 with ID3v2 tag (most common)
        b'\xFF\xFB',  # MPEG1 Layer3, 128kbps CBR (typical ElevenLabs output)
        b'\xFF\xFA',  # MPEG1 Layer3, no padding
        b'\xFF\xF3',  # MPEG2 Layer3
        b'\xFF\xF2',  # MPEG2 Layer3
        b'\xFF\xE3',  # MPEG2.5 Layer3
        b'\xFF\xE2',  # MPEG2.5 Layer3
    ],
    'audio/x-mpeg': [  # alias sent by some HTTP clients / ElevenLabs SDK
        b'ID3',
        b'\xFF\xFB',
        b'\xFF\xFA',
        b'\xFF\xF3',
        b'\xFF\xF2',
    ],
    'audio/mp3': [     # another common alias
        b'ID3',
        b'\xFF\xFB',
        b'\xFF\xFA',
        b'\xFF\xF3',
        b'\xFF\xF2',
    ],
    'audio/wav': [
        b'RIFF',  # WAV (RIFF container)
    ],
    'audio/ogg': [
        b'OggS',  # OGG
    ],
    'audio/webm': [
        b'\x1A\x45\xDF\xA3',  # WebM
    ],
    'audio/mp4': [
        b'\x00\x00\x00\x18ftypmp4',  # MP4
        b'\x00\x00\x00\x1Cftypisom',  # MP4
    ],
    
    # Video
    'video/mp4': [
        b'\x00\x00\x00\x18ftypmp4',  # MP4
        b'\x00\x00\x00\x1Cftypisom',  # MP4
        b'\x00\x00\x00\x1Cftypmp42',  # MP4
    ],
    'video/webm': [
        b'\x1A\x45\xDF\xA3',  # WebM
    ],
    'video/quicktime': [
        b'\x00\x00\x00\x14ftypqt',  # QuickTime
    ],
    
    # Documents
    'application/pdf': [
        b'%PDF-',  # PDF
    ],
    'application/msword': [
        b'\xD0\xCF\x11\xE0\xA1\xB1\x1A\xE1',  # OLE compound document
    ],
    'application/vnd.ms-excel': [
        b'\xD0\xCF\x11\xE0\xA1\xB1\x1A\xE1',
    ],
    'application/vnd.ms-powerpoint': [
        b'\xD0\xCF\x11\xE0\xA1\xB1\x1A\xE1',
    ],
    'application/vnd.ms-outlook': [
        b'\xD0\xCF\x11\xE0\xA1\xB1\x1A\xE1',
    ],
    'text/plain': [
        # Text files don't have magic bytes, validated by content
    ],
    'text/markdown': [
        # Markdown files don't have magic bytes
    ],
    'text/x-tex': [
        # TeX/LaTeX source files — no reliable magic bytes
    ],
    'application/x-latex': [
        # LaTeX files (alternate MIME)
    ],
    'text/x-python': [
        # Python source files — validated as UTF-8 text below
    ],
    **{
        source_mime: []
        for source_mime in SOURCE_CODE_VALIDATION_MIME_TYPES
    },
    'text/csv': [
        # CSV files — no magic bytes
    ],
    'application/rtf': [
        b'{\\rtf',
    ],
    'text/rtf': [
        b'{\\rtf',
    ],
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document': [
        b'PK\x03\x04',
        b'PK\x05\x06',
        b'PK\x07\x08',
    ],
    'application/vnd.ms-word.document.macroenabled.12': [
        b'PK\x03\x04',
        b'PK\x05\x06',
        b'PK\x07\x08',
    ],
    'application/vnd.openxmlformats-officedocument.wordprocessingml.template': [
        b'PK\x03\x04',
        b'PK\x05\x06',
        b'PK\x07\x08',
    ],
    'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet': [
        b'PK\x03\x04',
        b'PK\x05\x06',
        b'PK\x07\x08',
    ],
    'application/vnd.ms-excel.sheet.macroenabled.12': [
        b'PK\x03\x04',
        b'PK\x05\x06',
        b'PK\x07\x08',
    ],
    'application/vnd.ms-excel.sheet.binary.macroenabled.12': [
        b'PK\x03\x04',
        b'PK\x05\x06',
        b'PK\x07\x08',
    ],
    'application/vnd.openxmlformats-officedocument.presentationml.presentation': [
        b'PK\x03\x04',
        b'PK\x05\x06',
        b'PK\x07\x08',
    ],
    'application/vnd.ms-powerpoint.presentation.macroenabled.12': [
        b'PK\x03\x04',
        b'PK\x05\x06',
        b'PK\x07\x08',
    ],
    'application/vnd.openxmlformats-officedocument.presentationml.slideshow': [
        b'PK\x03\x04',
        b'PK\x05\x06',
        b'PK\x07\x08',
    ],
    'application/vnd.openxmlformats-officedocument.presentationml.template': [
        b'PK\x03\x04',
        b'PK\x05\x06',
        b'PK\x07\x08',
    ],
    'application/vnd.oasis.opendocument.text': [
        b'PK\x03\x04',
        b'PK\x05\x06',
        b'PK\x07\x08',
    ],
    'application/vnd.oasis.opendocument.spreadsheet': [
        b'PK\x03\x04',
        b'PK\x05\x06',
        b'PK\x07\x08',
    ],
    'application/vnd.oasis.opendocument.presentation': [
        b'PK\x03\x04',
        b'PK\x05\x06',
        b'PK\x07\x08',
    ],
    'application/vnd.apple.pages': [
        b'PK\x03\x04',
        b'PK\x05\x06',
        b'PK\x07\x08',
    ],
    'application/vnd.apple.numbers': [
        b'PK\x03\x04',
        b'PK\x05\x06',
        b'PK\x07\x08',
    ],
    'application/vnd.apple.keynote': [
        b'PK\x03\x04',
        b'PK\x05\x06',
        b'PK\x07\x08',
    ],
    'text/html': [
        b'<!DOCTYPE',
        b'<html',
        b'<HTML',
    ],
    'application/xml': [
        b'<?xml',
    ],
    'text/xml': [
        b'<?xml',
    ],
    'application/json': [
        b'{',  # JSON object
        b'[',  # JSON array
    ],
    'message/rfc822': [
        # Email messages are text containers with varied encodings.
    ],
    
    # Archives (be careful with these - can contain malicious content)
    'application/zip': [
        b'PK\x03\x04',  # ZIP
        b'PK\x05\x06',  # Empty ZIP
        b'PK\x07\x08',  # Spanned ZIP
    ],
    'application/x-tar': [
        b'ustar',  # TAR (at offset 257)
    ],
    'application/gzip': [
        b'\x1f\x8b',  # GZIP
    ],
    'application/x-bzip2': [
        b'BZh',  # BZIP2
    ],
    'application/x-xz': [
        b'\xfd7zXZ\x00',  # XZ
    ],
    'application/x-7z-compressed': [
        b"7z\xbc\xaf'\x1c",  # 7-Zip
    ],
    'application/vnd.rar': [
        b'Rar!\x1a\x07\x00',
        b'Rar!\x1a\x07\x01\x00',
    ],
    'application/x-rar-compressed': [
        b'Rar!\x1a\x07\x00',
        b'Rar!\x1a\x07\x01\x00',
    ],
}


# Maximum file sizes per type (in bytes)
MAX_SIZES = {
    'image/jpeg': 10 * 1024 * 1024,  # 10MB
    'image/png': 10 * 1024 * 1024,
    'image/gif': 10 * 1024 * 1024,
    'image/webp': 10 * 1024 * 1024,
    'image/bmp': 10 * 1024 * 1024,
    'image/svg+xml': 1 * 1024 * 1024,  # 1MB for SVG (can be dangerous)
    'audio/mpeg': 50 * 1024 * 1024,  # 50MB
    'audio/x-mpeg': 50 * 1024 * 1024,
    'audio/mp3': 50 * 1024 * 1024,
    'audio/wav': 50 * 1024 * 1024,
    'audio/ogg': 50 * 1024 * 1024,
    'audio/webm': 50 * 1024 * 1024,
    'audio/mp4': 50 * 1024 * 1024,
    'video/mp4': 100 * 1024 * 1024,  # 100MB
    'video/webm': 100 * 1024 * 1024,
    'video/quicktime': 100 * 1024 * 1024,
    'application/pdf': 10 * 1024 * 1024,
    'application/msword': 25 * 1024 * 1024,
    'application/vnd.ms-excel': 25 * 1024 * 1024,
    'application/vnd.ms-powerpoint': 25 * 1024 * 1024,
    'application/vnd.ms-outlook': 25 * 1024 * 1024,
    'text/plain': 1 * 1024 * 1024,
    'text/markdown': 1 * 1024 * 1024,
    'text/x-tex': 2 * 1024 * 1024,       # 2MB for TeX/LaTeX
    'application/x-latex': 2 * 1024 * 1024,
    'text/x-python': 2 * 1024 * 1024,    # 2MB for Python source shared as text
    **{
        source_mime: 2 * 1024 * 1024
        for source_mime in SOURCE_CODE_VALIDATION_MIME_TYPES
    },
    'text/csv': 5 * 1024 * 1024,          # 5MB for CSV
    'application/rtf': 5 * 1024 * 1024,
    'text/rtf': 5 * 1024 * 1024,
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document': 25 * 1024 * 1024,
    'application/vnd.ms-word.document.macroenabled.12': 25 * 1024 * 1024,
    'application/vnd.openxmlformats-officedocument.wordprocessingml.template': 25 * 1024 * 1024,
    'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet': 25 * 1024 * 1024,
    'application/vnd.ms-excel.sheet.macroenabled.12': 25 * 1024 * 1024,
    'application/vnd.ms-excel.sheet.binary.macroenabled.12': 25 * 1024 * 1024,
    'application/vnd.openxmlformats-officedocument.presentationml.presentation': 25 * 1024 * 1024,
    'application/vnd.ms-powerpoint.presentation.macroenabled.12': 25 * 1024 * 1024,
    'application/vnd.openxmlformats-officedocument.presentationml.slideshow': 25 * 1024 * 1024,
    'application/vnd.openxmlformats-officedocument.presentationml.template': 25 * 1024 * 1024,
    'application/vnd.oasis.opendocument.text': 25 * 1024 * 1024,
    'application/vnd.oasis.opendocument.spreadsheet': 25 * 1024 * 1024,
    'application/vnd.oasis.opendocument.presentation': 25 * 1024 * 1024,
    'application/vnd.apple.pages': 25 * 1024 * 1024,
    'application/vnd.apple.numbers': 25 * 1024 * 1024,
    'application/vnd.apple.keynote': 25 * 1024 * 1024,
    'text/html': 2 * 1024 * 1024,
    'application/xml': 2 * 1024 * 1024,
    'text/xml': 2 * 1024 * 1024,
    'application/json': 1 * 1024 * 1024,
    'message/rfc822': 5 * 1024 * 1024,
    'application/zip': 100 * 1024 * 1024,
    'application/x-tar': 100 * 1024 * 1024,
    'application/gzip': 100 * 1024 * 1024,
    'application/x-bzip2': 100 * 1024 * 1024,
    'application/x-xz': 100 * 1024 * 1024,
    'application/x-7z-compressed': 100 * 1024 * 1024,
    'application/vnd.rar': 100 * 1024 * 1024,
    'application/x-rar-compressed': 100 * 1024 * 1024,
}


def _has_openxml_workbook_structure(file_data: bytes) -> bool:
    """Return True when a ZIP container looks like an OOXML spreadsheet workbook."""
    try:
        with zipfile.ZipFile(io.BytesIO(file_data)) as archive:
            names = set(archive.namelist())
            return (
                '[Content_Types].xml' in names
                and 'xl/workbook.xml' in names
                and any(name.startswith('xl/worksheets/') for name in names)
            )
    except Exception:
        return False


def _has_openxml_document_structure(file_data: bytes) -> bool:
    """Return True when a ZIP container looks like an OOXML Word document."""
    try:
        with zipfile.ZipFile(io.BytesIO(file_data)) as archive:
            names = set(archive.namelist())
            return '[Content_Types].xml' in names and 'word/document.xml' in names
    except Exception:
        return False


def _has_openxml_presentation_structure(file_data: bytes) -> bool:
    """Return True when a ZIP container looks like an OOXML PowerPoint deck."""
    try:
        with zipfile.ZipFile(io.BytesIO(file_data)) as archive:
            names = set(archive.namelist())
            return (
                '[Content_Types].xml' in names
                and 'ppt/presentation.xml' in names
                and any(name.startswith('ppt/slides/slide') for name in names)
            )
    except Exception:
        return False


def _has_openxml_binary_workbook_structure(file_data: bytes) -> bool:
    """Return True when a ZIP container looks like a binary OOXML workbook."""
    try:
        with zipfile.ZipFile(io.BytesIO(file_data)) as archive:
            names = set(archive.namelist())
            return (
                '[Content_Types].xml' in names
                and ('xl/workbook.bin' in names or 'xl/workbook.xml' in names)
            )
    except Exception:
        return False


def _has_opendocument_structure(file_data: bytes, expected_mimetype: str) -> bool:
    """Return True when a ZIP container looks like the expected OpenDocument file."""
    try:
        with zipfile.ZipFile(io.BytesIO(file_data)) as archive:
            names = set(archive.namelist())
            if 'content.xml' not in names:
                return False
            try:
                mimetype = archive.read('mimetype').decode('utf-8', errors='ignore').strip()
            except Exception:
                mimetype = ''
            return not mimetype or mimetype == expected_mimetype
    except Exception:
        return False


def _has_iwork_zip_structure(file_data: bytes) -> bool:
    """Best-effort guard for modern Apple iWork ZIP bundles."""
    try:
        with zipfile.ZipFile(io.BytesIO(file_data)) as archive:
            names = set(archive.namelist())
            return bool(
                any(name.startswith('Index/') for name in names)
                or any(name.startswith('Metadata/') for name in names)
                or 'preview.jpg' in names
                or 'preview.png' in names
            )
    except Exception:
        return False


def _validate_utf8_source_payload(file_data: bytes, label: str) -> Tuple[bool, Optional[str]]:
    """Ensure source-code attachments remain text, not binary files in disguise."""
    if b'\x00' in file_data:
        return False, f"{label} source file contains binary data"
    try:
        file_data.decode('utf-8', errors='strict')
    except UnicodeDecodeError:
        return False, f"{label} source file must be valid UTF-8 text"
    return True, None


def _validate_mostly_text_payload(file_data: bytes, label: str) -> Tuple[bool, Optional[str]]:
    """Reject obvious binary blobs for text-container business formats."""
    if b'\x00' in file_data:
        return False, f"{label} file contains binary data"
    sample = file_data[:8192]
    try:
        text = sample.decode('utf-8', errors='ignore')
    except Exception:
        text = ''
    if not text:
        return False, f"{label} file is not readable text"
    printable = sum(1 for ch in text if ch.isprintable() or ch in '\r\n\t')
    if (printable / max(len(text), 1)) < 0.85:
        return False, f"{label} file contains too much non-text data"
    return True, None


def validate_file_upload(
    file_data: bytes,
    claimed_content_type: str,
    filename: str,
    max_size_override: Optional[int] = None
) -> Tuple[bool, Optional[str], Optional[str]]:
    """
    Validate a file upload for security.
    
    Checks:
    1. File size is within limits
    2. Content type is allowed
    3. Magic bytes match the claimed content type
    4. Filename extension matches content type
    5. No dangerous content patterns
    
    Args:
        file_data: Raw file bytes
        claimed_content_type: MIME type from upload
        filename: Original filename
        max_size_override: Override default max size for this type
        
    Returns:
        (is_valid, error_message, validated_content_type)
    """
    # 0. Infer MIME type from extension when the claimed type is generic or an alias.
    #    Browsers and Python HTTP clients (requests, urllib) often send
    #    application/octet-stream for binary files; some send non-canonical
    #    variants like audio/x-mpeg or audio/mp3.
    _GENERIC_TYPES = ('application/octet-stream', '', None, 'text/plain')
    _CANONICAL_ALIASES = {
        'audio/x-mpeg': 'audio/mpeg',
        'audio/mp3':    'audio/mpeg',
        'audio/x-wav':  'audio/wav',
        'audio/x-ogg':  'audio/ogg',
        'application/x-python': 'text/x-python',
        'application/x-python-code': 'text/x-python',
        'text/x-python-script': 'text/x-python',
        'application/vnd.ms-excel.sheet.macroenabled.12': 'application/vnd.ms-excel.sheet.macroenabled.12',
        'application/vnd.ms-excel.sheet.macroenabled.12; charset=binary': 'application/vnd.ms-excel.sheet.macroenabled.12',
        'application/x-rtf': 'application/rtf',
        'text/richtext': 'application/rtf',
        'application/vnd.msword': 'application/msword',
        'application/mspowerpoint': 'application/vnd.ms-powerpoint',
        'application/powerpoint': 'application/vnd.ms-powerpoint',
        'application/x-mspowerpoint': 'application/vnd.ms-powerpoint',
        'application/x-msoutlook': 'application/vnd.ms-outlook',
        'application/octet-stream; charset=binary': 'application/octet-stream',
        'application/x-zip-compressed': 'application/zip',
        'application/x-gzip': 'application/gzip',
        'application/gzip-compressed': 'application/gzip',
        'application/x-rar': 'application/vnd.rar',
    }
    claimed_content_type = (claimed_content_type or '').strip().lower()
    if ';' in claimed_content_type:
        claimed_content_type = claimed_content_type.split(';', 1)[0].strip()
    if claimed_content_type in _GENERIC_TYPES:
        inferred = _infer_content_type(filename)
        if inferred and inferred in ALLOWED_TYPES:
            claimed_content_type = inferred
    elif claimed_content_type in _CANONICAL_ALIASES:
        claimed_content_type = _CANONICAL_ALIASES[claimed_content_type]
    
    # 1. Check if content type is allowed
    if claimed_content_type not in ALLOWED_TYPES:
        return False, f"File type '{claimed_content_type}' is not allowed", None
    
    # 2. Check file size
    default_max_size = MAX_SIZES.get(claimed_content_type, 10 * 1024 * 1024)
    max_size = max_size_override or default_max_size
    if claimed_content_type == 'text/x-python' or claimed_content_type in SOURCE_CODE_VALIDATION_MIME_TYPES:
        max_size = min(max_size, default_max_size)
    if len(file_data) > max_size:
        return False, f"File size {len(file_data)} bytes exceeds maximum {max_size} bytes", None
    
    if len(file_data) == 0:
        return False, "File is empty", None

    is_canopy_module = claimed_content_type == 'text/html' and is_canopy_module_filename(filename)

    # 3. Verify magic bytes match claimed type
    magic_bytes = ALLOWED_TYPES[claimed_content_type]
    if magic_bytes:  # Some types like text/plain don't have magic bytes
        magic_match = False
        if is_canopy_module:
            stripped = file_data.lstrip().lower()
            magic_match = stripped.startswith(b'<!doctype') or stripped.startswith(b'<html')
        else:
            for signature in magic_bytes:
                if claimed_content_type == 'application/x-tar':
                    # TAR signature is at offset 257
                    if len(file_data) > 262 and file_data[257:262] == signature:
                        magic_match = True
                        break
                elif claimed_content_type in ('image/webp', 'audio/wav'):
                    # RIFF containers need extra validation
                    if file_data.startswith(b'RIFF'):
                        if claimed_content_type == 'image/webp' and len(file_data) > 12 and file_data[8:12] == b'WEBP':
                            magic_match = True
                            break
                        elif claimed_content_type == 'audio/wav' and len(file_data) > 12 and file_data[8:12] == b'WAVE':
                            magic_match = True
                            break
                else:
                    if file_data.startswith(signature):
                        magic_match = True
                        break

        if not magic_match:
            return False, f"File content does not match claimed type '{claimed_content_type}'", None
    
    # 4. Check for dangerous patterns in SVG files
    if claimed_content_type == 'image/svg+xml':
        try:
            file_str = file_data.decode('utf-8', errors='strict').lower()
        except UnicodeDecodeError:
            return False, "SVG file contains invalid UTF-8 encoding", None
        
        dangerous_patterns = ['<script', 'javascript:', 'onerror=', 'onload=', '<iframe']
        for pattern in dangerous_patterns:
            if pattern in file_str:
                return False, "SVG file contains potentially dangerous content", None
    
    # 4b. Check for dangerous patterns in HTML files
    if claimed_content_type in ('text/html',):
        if is_canopy_module:
            module_ok, module_error = _validate_canopy_module_bundle(file_data)
            if not module_ok:
                return False, module_error, None
        else:
            try:
                file_str = file_data.decode('utf-8', errors='strict').lower()
            except UnicodeDecodeError:
                return False, "HTML file contains invalid UTF-8 encoding", None

            dangerous_patterns = ['<script', 'javascript:', 'onerror=', 'onload=', '<iframe',
                                  '<object', '<embed', '<applet']
            for pattern in dangerous_patterns:
                if pattern in file_str:
                    return False, "HTML file contains potentially dangerous content", None

    # 4c. Validate that OOXML/OpenDocument business uploads are actually the
    # expected container shape. These formats are stored/downloaded only; no
    # macros, embedded scripts, or active content are executed by Canopy.
    if claimed_content_type in (
        'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        'application/vnd.ms-excel.sheet.macroenabled.12',
    ):
        if not _has_openxml_workbook_structure(file_data):
            return False, "Spreadsheet file is invalid or malformed", None

    if claimed_content_type == 'application/vnd.ms-excel.sheet.binary.macroenabled.12':
        if not _has_openxml_binary_workbook_structure(file_data):
            return False, "Spreadsheet file is invalid or malformed", None

    if claimed_content_type in (
        'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        'application/vnd.ms-word.document.macroenabled.12',
        'application/vnd.openxmlformats-officedocument.wordprocessingml.template',
    ):
        if not _has_openxml_document_structure(file_data):
            return False, "Word document is invalid or malformed", None

    if claimed_content_type in (
        'application/vnd.openxmlformats-officedocument.presentationml.presentation',
        'application/vnd.ms-powerpoint.presentation.macroenabled.12',
        'application/vnd.openxmlformats-officedocument.presentationml.slideshow',
        'application/vnd.openxmlformats-officedocument.presentationml.template',
    ):
        if not _has_openxml_presentation_structure(file_data):
            return False, "Presentation file is invalid or malformed", None

    opendocument_mimetypes = {
        'application/vnd.oasis.opendocument.text': 'application/vnd.oasis.opendocument.text',
        'application/vnd.oasis.opendocument.spreadsheet': 'application/vnd.oasis.opendocument.spreadsheet',
        'application/vnd.oasis.opendocument.presentation': 'application/vnd.oasis.opendocument.presentation',
    }
    if claimed_content_type in opendocument_mimetypes:
        if not _has_opendocument_structure(file_data, opendocument_mimetypes[claimed_content_type]):
            return False, "OpenDocument file is invalid or malformed", None

    if claimed_content_type in (
        'application/vnd.apple.pages',
        'application/vnd.apple.numbers',
        'application/vnd.apple.keynote',
    ):
        if not _has_iwork_zip_structure(file_data):
            return False, "Apple iWork file is invalid or malformed", None

    if claimed_content_type == 'message/rfc822':
        is_text, text_error = _validate_mostly_text_payload(file_data, 'Email')
        if not is_text:
            return False, text_error, None

    # 4d. Source attachments are allowed for agent collaboration, but only as UTF-8 text.
    source_ext = _filename_extension(filename)
    source_like_plain_text = claimed_content_type == 'text/plain' and source_ext in SOURCE_CODE_EXT_TO_MIME
    if claimed_content_type == 'text/x-python' or claimed_content_type in SOURCE_CODE_VALIDATION_MIME_TYPES or source_like_plain_text:
        is_text, text_error = _validate_utf8_source_payload(file_data, 'Source code')
        if not is_text:
            return False, text_error, None
    
    # 5. Validate filename extension matches content type
    extension_map = {
        'image/jpeg': ['.jpg', '.jpeg'],
        'image/png': ['.png'],
        'image/gif': ['.gif'],
        'image/webp': ['.webp'],
        'image/bmp': ['.bmp'],
        'image/svg+xml': ['.svg'],
        'audio/mpeg': ['.mp3'],
        'audio/x-mpeg': ['.mp3'],
        'audio/mp3': ['.mp3'],
        'audio/wav': ['.wav'],
        'audio/ogg': ['.ogg', '.oga'],
        'audio/webm': ['.webm'],
        'audio/mp4': ['.m4a', '.mp4'],
        'video/mp4': ['.mp4', '.m4v'],
        'video/webm': ['.webm'],
        'video/quicktime': ['.mov', '.qt'],
        'application/pdf': ['.pdf'],
        'application/msword': ['.doc', '.dot'],
        'application/vnd.ms-excel': ['.xls'],
        'application/vnd.ms-powerpoint': ['.ppt', '.pot', '.pps'],
        'application/vnd.ms-outlook': ['.msg'],
        'text/plain': [
            '.txt', '.log', '.cfg', '.ini', '.yml', '.yaml', '.toml',
            '.bat', '.cs', '.dockerfile', '.gradle', '.kt', '.kts', '.makefile', '.php', '.ps1', '.svelte', '.swift', '.vue',
        ],
        'text/markdown': ['.md', '.markdown'],
        'text/x-tex': ['.tex', '.sty', '.cls', '.bib', '.bst'],
        'application/x-latex': ['.tex', '.latex', '.ltx'],
        'text/x-python': ['.py', '.pyi', '.pyw'],
        **{
            source_mime: source_exts
            for source_mime, source_exts in SOURCE_CODE_MIME_TO_EXTENSIONS.items()
            if source_mime != 'text/plain'
        },
        'text/csv': ['.csv', '.tsv'],
        'application/rtf': ['.rtf'],
        'text/rtf': ['.rtf'],
        'application/vnd.openxmlformats-officedocument.wordprocessingml.document': ['.docx'],
        'application/vnd.ms-word.document.macroenabled.12': ['.docm'],
        'application/vnd.openxmlformats-officedocument.wordprocessingml.template': ['.dotx'],
        'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet': ['.xlsx'],
        'application/vnd.ms-excel.sheet.macroenabled.12': ['.xlsm'],
        'application/vnd.ms-excel.sheet.binary.macroenabled.12': ['.xlsb'],
        'application/vnd.openxmlformats-officedocument.presentationml.presentation': ['.pptx'],
        'application/vnd.ms-powerpoint.presentation.macroenabled.12': ['.pptm'],
        'application/vnd.openxmlformats-officedocument.presentationml.slideshow': ['.ppsx'],
        'application/vnd.openxmlformats-officedocument.presentationml.template': ['.potx'],
        'application/vnd.oasis.opendocument.text': ['.odt'],
        'application/vnd.oasis.opendocument.spreadsheet': ['.ods'],
        'application/vnd.oasis.opendocument.presentation': ['.odp'],
        'application/vnd.apple.pages': ['.pages'],
        'application/vnd.apple.numbers': ['.numbers'],
        'application/vnd.apple.keynote': ['.key'],
        'text/html': ['.html', '.htm'],
        'application/xml': ['.xml', '.xsl', '.xslt'],
        'text/xml': ['.xml'],
        'application/json': ['.json'],
        'message/rfc822': ['.eml'],
        'application/zip': ['.zip'],
        'application/x-tar': ['.tar'],
        'application/gzip': ['.gz', '.gzip', '.tgz'],
        'application/x-bzip2': ['.bz2', '.tbz2'],
        'application/x-xz': ['.xz'],
        'application/x-7z-compressed': ['.7z'],
        'application/vnd.rar': ['.rar'],
        'application/x-rar-compressed': ['.rar'],
    }
    
    expected_extensions = extension_map.get(claimed_content_type, [])
    if expected_extensions:
        filename_lower = filename.lower()
        if not any(filename_lower.endswith(ext) for ext in expected_extensions):
            return False, f"Filename extension does not match content type '{claimed_content_type}'", None
    
    # All checks passed
    return True, None, claimed_content_type


def detect_zip_bomb(file_data: bytes, content_type: str) -> Tuple[bool, Optional[str]]:
    """
    Detect potential zip bomb attacks.
    
    Args:
        file_data: Raw file bytes
        content_type: MIME type
        
    Returns:
        (is_safe, error_message)
    """
    zip_container_types = [
        'application/zip',
        'application/gzip',
        'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        'application/vnd.ms-word.document.macroenabled.12',
        'application/vnd.openxmlformats-officedocument.wordprocessingml.template',
        'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        'application/vnd.ms-excel.sheet.macroenabled.12',
        'application/vnd.ms-excel.sheet.binary.macroenabled.12',
        'application/vnd.openxmlformats-officedocument.presentationml.presentation',
        'application/vnd.ms-powerpoint.presentation.macroenabled.12',
        'application/vnd.openxmlformats-officedocument.presentationml.slideshow',
        'application/vnd.openxmlformats-officedocument.presentationml.template',
        'application/vnd.oasis.opendocument.text',
        'application/vnd.oasis.opendocument.spreadsheet',
        'application/vnd.oasis.opendocument.presentation',
        'application/vnd.apple.pages',
        'application/vnd.apple.numbers',
        'application/vnd.apple.keynote',
    ]
    if content_type not in zip_container_types:
        return True, None
    
    # Check compression ratio - if suspiciously high, might be a zip bomb
    # This is a simple heuristic; true zip bomb detection requires decompression
    
    if content_type in [
        'application/zip',
        'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        'application/vnd.ms-word.document.macroenabled.12',
        'application/vnd.openxmlformats-officedocument.wordprocessingml.template',
        'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        'application/vnd.ms-excel.sheet.macroenabled.12',
        'application/vnd.ms-excel.sheet.binary.macroenabled.12',
        'application/vnd.openxmlformats-officedocument.presentationml.presentation',
        'application/vnd.ms-powerpoint.presentation.macroenabled.12',
        'application/vnd.openxmlformats-officedocument.presentationml.slideshow',
        'application/vnd.openxmlformats-officedocument.presentationml.template',
        'application/vnd.oasis.opendocument.text',
        'application/vnd.oasis.opendocument.spreadsheet',
        'application/vnd.oasis.opendocument.presentation',
        'application/vnd.apple.pages',
        'application/vnd.apple.numbers',
        'application/vnd.apple.keynote',
    ]:
        try:
            zip_file = zipfile.ZipFile(io.BytesIO(file_data))
            total_uncompressed = sum(info.file_size for info in zip_file.filelist)
            
            # If uncompressed size is more than 100x compressed size, suspicious
            compression_ratio = total_uncompressed / len(file_data)
            if compression_ratio > 100:
                return False, "Suspicious compression ratio detected (potential zip bomb)"
            
            # Also check absolute uncompressed size
            if total_uncompressed > 1024 * 1024 * 1024:  # 1GB
                return False, "Archive would expand to more than 1GB"
                
        except Exception as e:
            logger.error(f"Error checking zip file: {e}")
            return False, "Invalid or corrupted zip file"
    
    return True, None
