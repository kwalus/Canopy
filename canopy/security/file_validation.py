"""
File upload validation and security checks for Canopy.

Validates file types, sizes, and content to prevent malicious uploads.

Author: Konrad Walus (architecture, design, and direction)
Project: Canopy - Local Mesh Communication
License: Apache 2.0
Development: AI-assisted implementation (Claude, Codex, GitHub Copilot, Cursor IDE, Ollama)
"""

import logging
from typing import Tuple, Optional

logger = logging.getLogger(__name__)


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
    '.md': 'text/markdown',
    '.markdown': 'text/markdown',
    '.csv': 'text/csv',
    '.tsv': 'text/csv',
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
    # Archives
    '.zip': 'application/zip',
    '.tar': 'application/x-tar',
    '.gz': 'application/gzip',
    '.gzip': 'application/gzip',
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
    'text/csv': [
        # CSV files — no magic bytes
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
    'text/plain': 1 * 1024 * 1024,
    'text/markdown': 1 * 1024 * 1024,
    'text/x-tex': 2 * 1024 * 1024,       # 2MB for TeX/LaTeX
    'application/x-latex': 2 * 1024 * 1024,
    'text/csv': 5 * 1024 * 1024,          # 5MB for CSV
    'text/html': 2 * 1024 * 1024,
    'application/xml': 2 * 1024 * 1024,
    'text/xml': 2 * 1024 * 1024,
    'application/json': 1 * 1024 * 1024,
    'application/zip': 100 * 1024 * 1024,
    'application/x-tar': 100 * 1024 * 1024,
    'application/gzip': 100 * 1024 * 1024,
}


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
    }
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
    max_size = max_size_override or MAX_SIZES.get(claimed_content_type, 10 * 1024 * 1024)
    if len(file_data) > max_size:
        return False, f"File size {len(file_data)} bytes exceeds maximum {max_size} bytes", None
    
    if len(file_data) == 0:
        return False, "File is empty", None
    
    # 3. Verify magic bytes match claimed type
    magic_bytes = ALLOWED_TYPES[claimed_content_type]
    if magic_bytes:  # Some types like text/plain don't have magic bytes
        magic_match = False
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
        try:
            file_str = file_data.decode('utf-8', errors='strict').lower()
        except UnicodeDecodeError:
            return False, "HTML file contains invalid UTF-8 encoding", None
        
        dangerous_patterns = ['<script', 'javascript:', 'onerror=', 'onload=', '<iframe',
                              '<object', '<embed', '<applet']
        for pattern in dangerous_patterns:
            if pattern in file_str:
                return False, "HTML file contains potentially dangerous content", None
    
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
        'text/plain': ['.txt', '.log', '.cfg', '.ini', '.yml', '.yaml', '.toml'],
        'text/markdown': ['.md', '.markdown'],
        'text/x-tex': ['.tex', '.sty', '.cls', '.bib', '.bst'],
        'application/x-latex': ['.tex', '.latex', '.ltx'],
        'text/csv': ['.csv', '.tsv'],
        'text/html': ['.html', '.htm'],
        'application/xml': ['.xml', '.xsl', '.xslt'],
        'text/xml': ['.xml'],
        'application/json': ['.json'],
        'application/zip': ['.zip'],
        'application/x-tar': ['.tar'],
        'application/gzip': ['.gz', '.gzip'],
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
    if content_type not in ['application/zip', 'application/gzip']:
        return True, None
    
    # Check compression ratio - if suspiciously high, might be a zip bomb
    # This is a simple heuristic; true zip bomb detection requires decompression
    
    if content_type == 'application/zip':
        try:
            import zipfile
            import io
            
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
