"""
REST API routes for Canopy.

Provides HTTP endpoints for all Canopy functionality including
messaging, key management, trust scoring, and system operations.

Author: Konrad Walus (architecture, design, and direction)
Project: Canopy - Local Mesh Communication
License: Apache 2.0
Development: AI-assisted implementation (Claude, Codex, GitHub Copilot, Cursor IDE, Ollama)
"""

import logging
import io
import os
import base64
import json
import re
import time
import secrets
import ipaddress
import socket
import html as html_lib
from urllib.parse import urlparse, parse_qs, urlencode, quote_plus
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError
import xml.etree.ElementTree as ET
from flask import Blueprint, request, jsonify, current_app, g, send_file, Response, session
from functools import wraps
from datetime import datetime, timezone, timedelta
from typing import Any, Callable, Optional, cast

from ..core.utils import get_app_components
from ..core.mentions import (
    extract_mentions,
    resolve_mention_targets,
    split_mention_targets,
    build_preview,
    record_mention_activity,
    record_thread_reply_activity,
    broadcast_mention_interaction,
    sync_edited_mention_activity,
)
from ..core.profile import (
    get_default_agent_directives,
    normalize_agent_directives,
)
from ..core.agent_heartbeat import (
    build_agent_heartbeat_snapshot,
    build_actionable_work_preview,
)
from ..core.events import PATCH1_EVENT_TYPES
from ..core.agent_presence import (
    record_agent_checkin,
    get_agent_presence_records,
    build_agent_presence_payload,
)
from ..core.file_preview import build_file_preview
from ..security.api_keys import Permission
from ..security.csrf import validate_csrf_request
from ..core.messaging import (
    MessageType,
    build_dm_security_summary,
    build_dm_preview,
    compute_group_id,
    filter_local_dm_targets,
)
from ..security.trust import TrustEvent
from ..security.file_access import evaluate_file_access
from ..network.routing import (
    encrypt_key_for_peer,
    encode_channel_key_material,
)
from .agent_instructions_data import build_agent_instructions_payload

logger = logging.getLogger(__name__)
API_BOOT_TIME = datetime.now(timezone.utc)


def _get_app_components_any(app: Any) -> tuple[Any, ...]:
    return cast(tuple[Any, ...], get_app_components(app))


def _record_connection_event(p2p_manager: Any, peer_id: str, status: str,
                             detail: str = '', endpoint: Optional[str] = None,
                             via_peer: Optional[str] = None) -> None:
    if not p2p_manager:
        return
    try:
        p2p_manager.record_activity_event({
            'id': f"conn_{peer_id}_{int(time.time() * 1000)}",
            'peer_id': peer_id,
            'kind': 'connection',
            'timestamp': time.time(),
            'status': status,
            'detail': detail,
            'endpoint': endpoint,
            'via_peer': via_peer,
        })
    except Exception:
        pass


def _extract_api_key_from_headers(req: Any) -> str:
    """Extract API key from X-API-Key or Authorization headers."""
    direct = str(req.headers.get('X-API-Key') or '').strip()
    if direct:
        return direct

    auth = str(req.headers.get('Authorization') or '').strip()
    if not auth:
        return ''

    parts = auth.split(None, 1)
    if len(parts) == 2 and parts[0].strip().lower() in {'bearer', 'token', 'apikey', 'api-key'}:
        return parts[1].strip()

    # Backward compatibility: allow raw key in Authorization header.
    return auth


def _default_agent_api_permissions() -> list[Permission]:
    """Default key scope for agent usage."""
    return [
        Permission.READ_MESSAGES,
        Permission.WRITE_MESSAGES,
        Permission.READ_FEED,
        Permission.WRITE_FEED,
    ]


_GENERIC_UPLOAD_CONTENT_TYPES = {
    '',
    'application/octet-stream',
    'binary/octet-stream',
    'application/x-binary',
    'application/unknown',
}
_GENERIC_UPLOAD_FILENAMES = {
    '',
    'file',
    'upload',
    'attachment',
    'unnamed_file',
}


def _is_generic_upload_metadata(filename: Any, content_type: Any) -> bool:
    name = str(filename or '').strip()
    ctype = str(content_type or '').strip().lower()
    stem = os.path.splitext(name)[0].strip().lower()
    return (
        ctype in _GENERIC_UPLOAD_CONTENT_TYPES
        or stem in _GENERIC_UPLOAD_FILENAMES
        or not os.path.splitext(name)[1]
    )


def _normalize_channel_attachments(raw_attachments: Any, file_manager: Any) -> list[dict[str, Any]]:
    """Canonicalize attachment payloads and hydrate metadata from file_id when possible."""
    if not isinstance(raw_attachments, list):
        return []

    normalized: list[dict[str, Any]] = []
    for item in raw_attachments:
        if isinstance(item, str):
            att: dict[str, Any] = {'id': item}
        elif isinstance(item, dict):
            att = dict(item)
        else:
            continue

        alias_name = (
            att.get('name')
            or att.get('filename')
            or att.get('original_name')
            or att.get('file_name')
        )
        if alias_name:
            att['name'] = str(alias_name)
        alias_type = (
            att.get('type')
            or att.get('content_type')
            or att.get('mime_type')
            or att.get('mime')
        )
        if alias_type:
            att['type'] = str(alias_type)

        file_id = str(att.get('id') or att.get('file_id') or '').strip()
        if file_id:
            att['id'] = file_id
            att.setdefault('file_id', file_id)
            try:
                if file_manager:
                    file_info = file_manager.get_file(file_id)
                else:
                    file_info = None
            except Exception:
                file_info = None
            if file_info:
                if not att.get('name') or str(att.get('name')).strip().lower() in _GENERIC_UPLOAD_FILENAMES:
                    att['name'] = file_info.original_name
                if not att.get('type') or str(att.get('type')).strip().lower() in _GENERIC_UPLOAD_CONTENT_TYPES:
                    att['type'] = file_info.content_type
                if att.get('size') in (None, '', 0):
                    att['size'] = file_info.size

        if not att.get('name'):
            att['name'] = 'file'
        if not att.get('type'):
            att['type'] = 'application/octet-stream'
        if att.get('size') is not None:
            try:
                att['size'] = int(att.get('size'))
            except (TypeError, ValueError):
                pass

        normalized.append(att)

    return normalized


def create_api_blueprint() -> Blueprint:
    """Create and configure the API blueprint."""
    api = Blueprint('api', __name__)
    
    # Authentication decorator
    def require_auth(required_permission: Optional[Permission] = None,
                     *, allow_session: bool = False) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """Decorator to require API key auth, with optional UI session fallback.

        Session fallback is intentionally limited to routes that opt in via
        allow_session=True and only when no explicit API permission is needed.
        """
        def decorator(f):
            @wraps(f)
            def decorated_function(*args, **kwargs):
                # Get components
                _, api_key_manager, _, _, _, _, _, _, _, _, _ = _get_app_components_any(current_app)

                # Get API key from header
                api_key = _extract_api_key_from_headers(request)

                if api_key:
                    # Validate key
                    key_info = api_key_manager.validate_key(api_key, required_permission)
                    if not key_info:
                        return jsonify({'error': 'Invalid or insufficient permissions'}), 403

                    # Store key info in request context
                    g.api_key_info = key_info
                    # Pending-approval accounts may only call GET /api/auth/status
                    if getattr(key_info, 'account_pending', False):
                        is_auth_status = request.path.rstrip('/').endswith('/auth/status') and request.method == 'GET'
                        if not is_auth_status:
                            return jsonify({
                                'error': 'Account pending approval',
                                'status': 'pending_approval'
                            }), 403
                    return f(*args, **kwargs)

                # Optional browser-session fallback for selected local UI routes.
                is_session_auth = bool(session.get('authenticated', False) and session.get('user_id'))
                if allow_session and required_permission is None and is_session_auth:
                    if request.method not in ('GET', 'HEAD', 'OPTIONS'):
                        validate_csrf_request()
                    g.api_key_info = None
                    return f(*args, **kwargs)

                if allow_session and required_permission is None:
                    return jsonify({
                        'error': 'Authentication required',
                        'message': 'Sign in to the web UI or provide X-API-Key',
                    }), 401

                return jsonify({'error': 'API key required'}), 401
            
            return decorated_function
        return decorator

    def _resolve_handle_to_user_id(db_manager: Any, handle: str,
                                   visibility: Optional[str] = None,
                                   permissions: Optional[list[str]] = None,
                                   channel_id: Optional[str] = None,
                                   author_id: Optional[str] = None) -> Optional[str]:
        if not handle:
            return None
        token = str(handle).strip()
        if token.startswith('@'):
            token = token[1:]
        if not token:
            return None
        try:
            row = db_manager.get_user(token)
            if row:
                return row.get('id') or token
        except Exception:
            pass
        try:
            targets = resolve_mention_targets(
                db_manager,
                [token],
                visibility=visibility,
                permissions=permissions,
                channel_id=channel_id,
                author_id=author_id,
            )
            if targets:
                return targets[0].get('user_id')
        except Exception:
            return None
        return None

    def _resolve_handle_list(db_manager: Any, handles: list[Any],
                              visibility: Optional[str] = None,
                              permissions: Optional[list[str]] = None,
                              channel_id: Optional[str] = None,
                              author_id: Optional[str] = None) -> list[str]:
        resolved: list[str] = []
        for h in handles or []:
            uid = _resolve_handle_to_user_id(
                db_manager,
                h,
                visibility=visibility,
                permissions=permissions,
                channel_id=channel_id,
                author_id=author_id,
            )
            if uid:
                resolved.append(uid)
        return resolved

    _TASK_STATUS_SET = {'open', 'in_progress', 'blocked', 'done'}
    _TASK_PRIORITY_SET = {'low', 'normal', 'high', 'critical'}

    def _normalize_task_status(value: Optional[str]) -> str:
        val = (value or '').strip().lower()
        return val if val in _TASK_STATUS_SET else 'open'

    def _normalize_task_priority(value: Optional[str]) -> str:
        val = (value or '').strip().lower()
        return val if val in _TASK_PRIORITY_SET else 'normal'

    def _merge_task_metadata(existing: Optional[dict[str, Any]], base_meta: dict[str, Any],
                             editor_ids: Optional[list[str]] = None) -> dict[str, Any]:
        merged = dict(existing or {})
        merged.update(base_meta or {})
        if editor_ids is not None:
            merged['editors'] = editor_ids
        return merged

    def _as_bool(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        if value is None:
            return False
        return str(value).strip().lower() in ('1', 'true', 'yes', 'on')

    def _channel_not_found_response() -> tuple[Any, int]:
        """Generic channel-scope miss to reduce enumeration leakage."""
        return jsonify({'error': 'Not found', 'message': 'Resource not found'}), 404

    def _get_stream_manager() -> Any:
        return current_app.config.get('STREAM_MANAGER')

    def _get_db_manager() -> Any:
        return current_app.config.get('DB_MANAGER')

    def _build_stream_attachment(stream_row: dict[str, Any]) -> dict[str, Any]:
        media_kind = str(stream_row.get('media_kind') or 'audio')
        stream_kind = str(stream_row.get('stream_kind') or ('telemetry' if media_kind == 'data' else 'media'))
        title = str(stream_row.get('title') or 'Live stream')
        description = str(stream_row.get('description') or '')
        status = str(stream_row.get('status') or 'created')
        stream_id = str(stream_row.get('id') or '')
        return {
            'name': title,
            'type': 'application/vnd.canopy.stream+json',
            'kind': 'stream',
            'stream_id': stream_id,
            'title': title,
            'description': description,
            'media_kind': media_kind,
            'stream_kind': stream_kind,
            'protocol': str(stream_row.get('protocol') or 'hls'),
            'status': status,
            'channel_id': str(stream_row.get('channel_id') or ''),
            'created_by': str(stream_row.get('created_by') or ''),
            'relay_allowed': bool(stream_row.get('relay_allowed')),
        }

    def _touch_agent_presence(user_id: Optional[str], source: str) -> Optional[str]:
        """Record a lightweight check-in timestamp for agent presence badges."""
        uid = (user_id or '').strip()
        if not uid:
            return None
        try:
            db_manager = _get_app_components_any(current_app)[0]
            return record_agent_checkin(db_manager=db_manager, user_id=uid, source=source)
        except Exception:
            return None

    def _stable_handle_candidates(user_row: dict[str, Any]) -> list[str]:
        """Return mention handles ordered from most-stable to least-stable."""
        out: list[str] = []

        def _add(value: Optional[str]) -> None:
            token = (value or '').strip()
            if not token:
                return
            token = token[1:] if token.startswith('@') else token
            if not token:
                return
            if token not in out:
                out.append(token)

        username = (user_row.get('username') or '').strip()
        display_name = (user_row.get('display_name') or '').strip()
        user_id = (user_row.get('id') or '').strip()

        if username:
            _add(username.split('.', 1)[0])
            _add(username)
        if display_name:
            _add("_".join(display_name.split()))
        _add(user_id)
        return out

    _URL_PATTERN = re.compile(r'https?://[^\s<>"\']+')
    _YT_ID_PATTERN = re.compile(r'^[A-Za-z0-9_-]{11}$')

    def _ensure_content_context_schema(db_manager: Any) -> None:
        """Ensure content_contexts table exists for older live instances."""
        with db_manager.get_connection() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS content_contexts (
                    id TEXT PRIMARY KEY,
                    source_type TEXT NOT NULL,
                    source_id TEXT,
                    source_url TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    owner_user_id TEXT NOT NULL,
                    title TEXT,
                    author TEXT,
                    transcript_lang TEXT,
                    transcript_text TEXT,
                    extracted_text TEXT,
                    summary_text TEXT,
                    owner_note TEXT,
                    status TEXT DEFAULT 'ready',
                    error TEXT,
                    metadata TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(source_type, source_id, source_url, owner_user_id)
                );
                CREATE INDEX IF NOT EXISTS idx_content_contexts_source
                    ON content_contexts(source_type, source_id, updated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_content_contexts_owner
                    ON content_contexts(owner_user_id, updated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_content_contexts_url
                    ON content_contexts(source_url);
            """)
            conn.commit()

    def _extract_urls(text: str, limit: int = 8) -> list:
        if not text:
            return []
        urls = []
        for match in _URL_PATTERN.finditer(text):
            candidate = (match.group(0) or '').strip()
            # Trim punctuation commonly attached at sentence boundaries.
            candidate = candidate.rstrip('.,;:!?')
            candidate = candidate.rstrip(')>]}')
            if candidate and candidate not in urls:
                urls.append(candidate)
                if len(urls) >= max(1, int(limit)):
                    break
        return urls

    def _parse_youtube_video_id(url: str) -> Optional[str]:
        try:
            parsed = urlparse(url)
            host = (parsed.netloc or '').lower()
            if host.startswith('www.'):
                host = host[4:]
            path = (parsed.path or '').strip('/')
            vid = ''
            if host == 'youtu.be':
                vid = path.split('/')[0]
            elif host.endswith('youtube.com') or host.endswith('youtube-nocookie.com'):
                if path == 'watch':
                    vid = parse_qs(parsed.query).get('v', [''])[0]
                elif path.startswith('shorts/'):
                    vid = path.split('/')[1] if len(path.split('/')) > 1 else ''
                elif path.startswith('embed/'):
                    vid = path.split('/')[1] if len(path.split('/')) > 1 else ''
            if vid and _YT_ID_PATTERN.match(vid):
                return vid
        except Exception:
            return None
        return None

    def _is_private_ip(ip_obj: Any) -> bool:
        return bool(
            ip_obj.is_private or
            ip_obj.is_loopback or
            ip_obj.is_link_local or
            ip_obj.is_multicast or
            ip_obj.is_reserved or
            ip_obj.is_unspecified
        )

    def _is_safe_external_url(url: str) -> tuple:
        """
        Prevent SSRF to localhost/private ranges by default.
        Override for trusted environments with CANOPY_ALLOW_PRIVATE_CONTEXT_FETCH=1.
        """
        candidate = (url or '').strip()
        parsed = urlparse(candidate)
        scheme = (parsed.scheme or '').lower()
        if scheme not in ('http', 'https'):
            return False, 'Only http/https URLs are supported'

        host = (parsed.hostname or '').strip().lower()
        if not host:
            return False, 'URL host is required'

        allow_private = str(os.getenv('CANOPY_ALLOW_PRIVATE_CONTEXT_FETCH', '')).strip().lower() in ('1', 'true', 'yes')
        if allow_private:
            return True, ''

        if host in ('localhost',) or host.endswith('.local'):
            return False, 'Local/private hosts are blocked for context extraction'

        # Literal IP host
        try:
            literal_ip = ipaddress.ip_address(host)
            if _is_private_ip(literal_ip):
                return False, 'Private/loopback addresses are blocked for context extraction'
            return True, ''
        except ValueError:
            pass

        # DNS host -> check resolved addresses
        try:
            infos = socket.getaddrinfo(host, None)
            for info in infos:
                sockaddr = info[4]
                if not sockaddr:
                    continue
                ip_txt = str(sockaddr[0] or '').split('%')[0]
                if not ip_txt:
                    continue
                try:
                    resolved_ip = ipaddress.ip_address(ip_txt)
                except ValueError:
                    continue
                if _is_private_ip(resolved_ip):
                    return False, 'Host resolves to private/loopback address and is blocked'
        except socket.gaierror:
            # If DNS resolution fails now, let the HTTP fetch return a concrete error later.
            return True, ''

        return True, ''

    def _http_get_text(url: str, timeout: int = 8, max_bytes: int = 900_000) -> str:
        safe, reason = _is_safe_external_url(url)
        if not safe:
            raise ValueError(reason)
        req = Request(url, headers={
            'User-Agent': 'Canopy/1.0 (+https://canopy.local)',
            'Accept-Language': 'en-US,en;q=0.9',
        })
        with urlopen(req, timeout=max(1, int(timeout))) as resp:
            data = resp.read(max_bytes + 1)
            if len(data) > max_bytes:
                data = data[:max_bytes]
            content_type = resp.headers.get('Content-Type', '')
            charset = 'utf-8'
            if 'charset=' in content_type:
                charset = content_type.split('charset=')[-1].split(';')[0].strip() or 'utf-8'
            try:
                return cast(str, data.decode(charset, errors='replace'))
            except Exception:
                return cast(str, data.decode('utf-8', errors='replace'))

    def _strip_html(text: str) -> str:
        if not text:
            return ''
        cleaned = re.sub(r'(?is)<script[^>]*>.*?</script>', ' ', text)
        cleaned = re.sub(r'(?is)<style[^>]*>.*?</style>', ' ', cleaned)
        cleaned = re.sub(r'(?is)<!--.*?-->', ' ', cleaned)
        cleaned = re.sub(r'(?is)<[^>]+>', ' ', cleaned)
        cleaned = html_lib.unescape(cleaned)
        cleaned = re.sub(r'\s+', ' ', cleaned).strip()
        return cleaned

    def _extract_generic_page_context(url: str) -> dict:
        context: dict[str, Any] = {
            'provider': 'web',
            'canonical_url': url,
            'title': '',
            'author': '',
            'transcript_lang': '',
            'transcript_text': '',
            'extracted_text': '',
            'summary_text': '',
            'status': 'ready',
            'error': '',
            'metadata': {},
        }
        try:
            html = _http_get_text(url, timeout=8)
            title_match = re.search(r'(?is)<title[^>]*>(.*?)</title>', html)
            title = _strip_html(title_match.group(1)) if title_match else ''
            desc = ''
            desc_match = re.search(
                r'(?is)<meta[^>]+(?:name|property)\s*=\s*["\'](?:description|og:description)["\'][^>]*content\s*=\s*["\'](.*?)["\']',
                html
            )
            if desc_match:
                desc = _strip_html(desc_match.group(1))

            plain_text = _strip_html(html)
            if len(plain_text) > 12000:
                plain_text = plain_text[:12000].rstrip() + ' ...'

            parts = []
            if title:
                parts.append(f"Title: {title}")
            if desc:
                parts.append(f"Description: {desc}")
            if plain_text:
                parts.append("Extracted Text:")
                parts.append(plain_text)

            context.update({
                'title': title,
                'extracted_text': plain_text,
                'summary_text': '\n\n'.join(parts).strip(),
            })
        except (HTTPError, URLError, TimeoutError, ValueError) as e:
            context['status'] = 'error'
            context['error'] = str(e)
            context['summary_text'] = f"Failed to extract page context: {e}"
        except Exception as e:
            context['status'] = 'error'
            context['error'] = str(e)
            context['summary_text'] = f"Failed to extract page context: {e}"
        return context

    def _vtt_to_text(vtt_text: str) -> str:
        if not vtt_text:
            return ''
        lines = []
        for raw in vtt_text.splitlines():
            line = (raw or '').strip()
            if not line:
                continue
            if line.upper().startswith('WEBVTT'):
                continue
            if '-->' in line:
                continue
            if re.fullmatch(r'\d+', line):
                continue
            line = re.sub(r'<[^>]+>', '', line)
            line = html_lib.unescape(line).strip()
            if line:
                lines.append(line)
        return re.sub(r'\s+', ' ', ' '.join(lines)).strip()

    def _xml_caption_to_text(xml_text: str) -> str:
        if not xml_text:
            return ''
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError:
            return ''
        lines = []
        for node in root.findall('.//text'):
            text = html_lib.unescape(''.join(node.itertext() or [])).strip()
            if text:
                lines.append(text)
        return re.sub(r'\s+', ' ', ' '.join(lines)).strip()

    def _extract_youtube_context(url: str, video_id: str) -> dict:
        canonical_url = f"https://www.youtube.com/watch?v={video_id}"
        context: dict[str, Any] = {
            'provider': 'youtube',
            'canonical_url': canonical_url,
            'title': '',
            'author': '',
            'transcript_lang': '',
            'transcript_text': '',
            'extracted_text': '',
            'summary_text': '',
            'status': 'partial',
            'error': '',
            'metadata': {'video_id': video_id},
        }

        # OEmbed for title/author
        try:
            oembed_url = f"https://www.youtube.com/oembed?url={quote_plus(canonical_url)}&format=json"
            raw = _http_get_text(oembed_url, timeout=6, max_bytes=128_000)
            obj = json.loads(raw)
            context['title'] = (obj.get('title') or '').strip()
            context['author'] = (obj.get('author_name') or '').strip()
            context['metadata']['oembed'] = {
                'provider_name': obj.get('provider_name'),
                'thumbnail_url': obj.get('thumbnail_url'),
            }
        except Exception as e:
            context['metadata']['oembed_error'] = str(e)

        # Transcript best-effort
        transcript_text = ''
        transcript_lang = ''
        try:
            track_xml = _http_get_text(
                f"https://video.google.com/timedtext?type=list&v={video_id}",
                timeout=6,
                max_bytes=200_000,
            )
            tracks: list[dict[str, Any]] = []
            try:
                root = ET.fromstring(track_xml)
                for tr in root.findall('.//track'):
                    lang = (tr.attrib.get('lang_code') or '').strip()
                    kind = (tr.attrib.get('kind') or '').strip()
                    name = (tr.attrib.get('name') or '').strip()
                    if lang:
                        score = 0
                        if lang == 'en':
                            score += 100
                        elif lang.startswith('en'):
                            score += 80
                        if kind != 'asr':
                            score += 20
                        tracks.append({'lang': lang, 'kind': kind, 'name': name, 'score': score})
            except ET.ParseError:
                tracks = []

            if tracks:
                tracks.sort(key=lambda t: int(t.get('score', 0)), reverse=True)
                selected = tracks[0]
                transcript_lang = cast(str, selected.get('lang') or '')
                params: dict[str, str] = {'v': video_id, 'lang': transcript_lang, 'fmt': 'vtt'}
                kind_val = selected.get('kind')
                if kind_val:
                    params['kind'] = str(kind_val)
                name_val = selected.get('name')
                if name_val:
                    params['name'] = str(name_val)
                captions_url = "https://www.youtube.com/api/timedtext?" + urlencode(params)
                cap_raw = _http_get_text(captions_url, timeout=8, max_bytes=1_200_000)
                transcript_text = _vtt_to_text(cap_raw)
                if not transcript_text:
                    transcript_text = _xml_caption_to_text(cap_raw)
        except Exception as e:
            context['metadata']['transcript_error'] = str(e)

        if transcript_text:
            if len(transcript_text) > 16000:
                transcript_text = transcript_text[:16000].rstrip() + ' ...'
            context['transcript_lang'] = transcript_lang
            context['transcript_text'] = transcript_text
            context['status'] = 'ready'
        else:
            context['status'] = 'partial'

        summary_parts = []
        if context['title']:
            summary_parts.append(f"Title: {context['title']}")
        if context['author']:
            summary_parts.append(f"Author: {context['author']}")
        summary_parts.append(f"Video URL: {canonical_url}")
        if transcript_text:
            summary_parts.append(f"Transcript ({transcript_lang or 'unknown'}):")
            summary_parts.append(transcript_text)
        else:
            summary_parts.append("Transcript: unavailable (captions not available or inaccessible)")
        context['summary_text'] = '\n\n'.join(summary_parts).strip()
        return context

    def _extract_external_context(url: str) -> dict:
        """Best-effort extraction of text context for agents/humans."""
        candidate = (url or '').strip()
        if not candidate:
            return {
                'provider': 'unknown',
                'canonical_url': '',
                'title': '',
                'author': '',
                'transcript_lang': '',
                'transcript_text': '',
                'extracted_text': '',
                'summary_text': 'No URL provided.',
                'status': 'error',
                'error': 'No URL provided',
                'metadata': {},
            }
        vid = _parse_youtube_video_id(candidate)
        if vid:
            return _extract_youtube_context(candidate, vid)
        return _extract_generic_page_context(candidate)

    def _build_context_text_blob(row: dict) -> str:
        parts: list[str] = []
        if row.get('provider'):
            parts.append(f"Provider: {row.get('provider')}")
        if row.get('status'):
            parts.append(f"Status: {row.get('status')}")
        if row.get('title'):
            parts.append(f"Title: {row.get('title')}")
        if row.get('author'):
            parts.append(f"Author: {row.get('author')}")
        if row.get('source_url'):
            parts.append(f"Source URL: {row.get('source_url')}")
        if row.get('error'):
            parts.append(f"Extraction Error: {row.get('error')}")
        if row.get('summary_text'):
            parts.append("Summary:")
            parts.append(cast(str, row.get('summary_text')))
        if row.get('transcript_text'):
            parts.append("Transcript:")
            parts.append(cast(str, row.get('transcript_text')))
        if row.get('extracted_text'):
            parts.append("Extracted Text:")
            parts.append(cast(str, row.get('extracted_text')))
        if row.get('owner_note'):
            parts.append("Owner Note:")
            parts.append(cast(str, row.get('owner_note')))
        return '\n\n'.join([p for p in parts if p]).strip()

    def _serialize_context_row(row: Any, current_user_id: str, admin_user_id: Optional[str]) -> dict[str, Any]:
        if not row:
            return {}
        payload = {
            'id': row['id'],
            'source_type': row['source_type'],
            'source_id': row['source_id'],
            'source_url': row['source_url'],
            'provider': row['provider'],
            'owner_user_id': row['owner_user_id'],
            'title': row['title'],
            'author': row['author'],
            'transcript_lang': row['transcript_lang'],
            'transcript_text': row['transcript_text'] or '',
            'extracted_text': row['extracted_text'] or '',
            'summary_text': row['summary_text'] or '',
            'owner_note': row['owner_note'] or '',
            'status': row['status'] or 'ready',
            'error': row['error'] or '',
            'metadata': {},
            'created_at': row['created_at'],
            'updated_at': row['updated_at'],
            'can_edit_note': bool(
                current_user_id and (
                    row['owner_user_id'] == current_user_id or
                    (admin_user_id and current_user_id == admin_user_id)
                )
            ),
        }
        try:
            payload['metadata'] = json.loads(row['metadata']) if row['metadata'] else {}
        except Exception:
            payload['metadata'] = {}
        payload['text_blob'] = _build_context_text_blob(payload)
        return payload

    def _uploader_is_peer(db_manager: Any, uploaded_by: Optional[str]) -> bool:
        """Return True if the file was uploaded by a peer/shadow user.

        Shadow users have an ``origin_peer`` field set, indicating that they
        represent a user from a different device.  The local admin bypass must
        not apply to files uploaded by such users.
        """
        if not uploaded_by:
            return False
        try:
            row = db_manager.get_user(uploaded_by)
            return bool(row and row.get('origin_peer'))
        except Exception:
            return False

    def _can_user_access_source(db_manager: Any, feed_manager: Any, user_id: str, source_type: str, source_id: str) -> bool:
        if source_type == 'url':
            return True
        if source_type == 'feed_post':
            post = feed_manager.get_post(source_id) if feed_manager else None
            return bool(post and post.can_view(user_id))
        if source_type == 'channel_message':
            with db_manager.get_connection() as conn:
                row = conn.execute(
                    "SELECT channel_id FROM channel_messages WHERE id = ?",
                    (source_id,)
                ).fetchone()
                if not row:
                    return False
                member = conn.execute(
                    "SELECT 1 FROM channel_members WHERE channel_id = ? AND user_id = ?",
                    (row['channel_id'], user_id)
                ).fetchone()
                if member:
                    return True
                # Backward compatibility: allow open general channel reads.
                if row['channel_id'] == 'general':
                    return True
                return False
        if source_type == 'direct_message':
            with db_manager.get_connection() as conn:
                row = conn.execute(
                    "SELECT sender_id, recipient_id FROM messages WHERE id = ?",
                    (source_id,)
                ).fetchone()
                if not row:
                    return False
                sender_id = row['sender_id']
                recipient_id = row['recipient_id']
                if sender_id == user_id:
                    return True
                if recipient_id is None:
                    # Broadcast-style records are visible in the message feed.
                    return True
                return bool(recipient_id == user_id)
        return False

    def _resolve_source_payload(
        db_manager: Any,
        feed_manager: Any,
        user_id: str,
        source_type: str,
        source_id: str,
    ) -> tuple[bool, Any, str]:
        """
        Returns (ok, payload_or_error_code, payload_or_error_msg).
        payload contains: content, owner_user_id, source_url_candidates
        """
        if source_type == 'feed_post':
            post = feed_manager.get_post(source_id) if feed_manager else None
            if not post:
                return False, 404, 'Source feed post not found'
            if not post.can_view(user_id):
                return False, 403, 'Access denied'
            content = post.content or ''
            return True, {
                'content': content,
                'owner_user_id': post.author_id,
                'source_url_candidates': _extract_urls(content),
            }, ''

        if source_type == 'channel_message':
            with db_manager.get_connection() as conn:
                row = conn.execute(
                    "SELECT id, channel_id, user_id, content FROM channel_messages WHERE id = ?",
                    (source_id,)
                ).fetchone()
                if not row:
                    return False, 404, 'Source channel message not found'
                member = conn.execute(
                    "SELECT 1 FROM channel_members WHERE channel_id = ? AND user_id = ?",
                    (row['channel_id'], user_id)
                ).fetchone()
                if not member and row['channel_id'] != 'general':
                    return False, 403, 'Access denied'
                content = row['content'] or ''
                return True, {
                    'content': content,
                    'owner_user_id': row['user_id'],
                    'source_url_candidates': _extract_urls(content),
                }, ''

        if source_type == 'direct_message':
            with db_manager.get_connection() as conn:
                row = conn.execute(
                    "SELECT id, sender_id, recipient_id, content FROM messages WHERE id = ?",
                    (source_id,)
                ).fetchone()
                if not row:
                    return False, 404, 'Source direct message not found'
                sender_id = row['sender_id']
                recipient_id = row['recipient_id']
                if sender_id != user_id and recipient_id not in (None, user_id):
                    return False, 403, 'Access denied'
                content = row['content'] or ''
                return True, {
                    'content': content,
                    'owner_user_id': sender_id or user_id,
                    'source_url_candidates': _extract_urls(content),
                }, ''

        if source_type == 'url':
            return True, {
                'content': '',
                'owner_user_id': user_id,
                'source_url_candidates': [],
            }, ''

        return False, 400, 'Unsupported source_type'

    def _sync_inline_tasks_from_content(
        *,
        task_manager: Any,
        db_manager: Any,
        content: str,
        scope: str,
        source_id: str,
        actor_id: str,
        task_visibility: str,
        base_metadata: dict[str, Any],
        visibility: Optional[str] = None,
        permissions: Optional[list[str]] = None,
        channel_id: Optional[str] = None,
        p2p_manager: Any = None,
        profile_manager: Any = None,
    ) -> None:
        from ..core.tasks import parse_task_blocks, derive_task_id

        if not task_manager:
            return
        task_specs = parse_task_blocks(content or '')
        if not task_specs:
            return

        sender_display = None
        if profile_manager:
            try:
                profile = profile_manager.get_profile(actor_id)
                if profile:
                    sender_display = profile.display_name or profile.username
            except Exception:
                sender_display = None

        for idx, spec in enumerate(cast(Any, task_specs)):
            spec = cast(Any, spec)
            if not spec.confirmed:
                continue

            task_id = derive_task_id(scope, source_id, idx, len(task_specs), override=spec.task_id)
            existing = task_manager.get_task(task_id)

            assignee_specified = spec.assignee is not None or spec.assignee_clear
            resolved_assignee = None
            if assignee_specified:
                if spec.assignee_clear:
                    resolved_assignee = None
                else:
                    raw_assignee = (spec.assignee or '').strip()
                    if raw_assignee:
                        resolved_assignee = _resolve_handle_to_user_id(
                            db_manager,
                            raw_assignee,
                            visibility=visibility,
                            permissions=permissions,
                            channel_id=channel_id,
                            author_id=actor_id,
                        )
                        if not resolved_assignee:
                            logger.warning(f"Inline task assignee '{raw_assignee}' could not be resolved for {scope}:{source_id}")
                            assignee_specified = False
                    else:
                        resolved_assignee = None

            editor_ids = None
            if spec.editors_clear:
                editor_ids = []
            elif spec.editors is not None:
                resolved = _resolve_handle_list(
                    db_manager,
                    spec.editors,
                    visibility=visibility,
                    permissions=permissions,
                    channel_id=channel_id,
                    author_id=actor_id,
                )
                if resolved:
                    editor_ids = list(dict.fromkeys(resolved))
                else:
                    logger.warning(f"Inline task editors could not be resolved for {scope}:{source_id}")

            if existing:
                updates: dict[str, Any] = {}
                if spec.title and spec.title != existing.title:
                    updates['title'] = spec.title
                if spec.description is not None and spec.description != existing.description:
                    updates['description'] = spec.description
                if spec.status is not None:
                    new_status = _normalize_task_status(spec.status)
                    if new_status != existing.status:
                        updates['status'] = new_status
                if spec.priority is not None:
                    new_priority = _normalize_task_priority(spec.priority)
                    if new_priority != existing.priority:
                        updates['priority'] = new_priority
                if assignee_specified and resolved_assignee != existing.assigned_to:
                    updates['assigned_to'] = resolved_assignee
                if spec.due_clear:
                    if existing.due_at is not None:
                        updates['due_at'] = None
                elif spec.due_at is not None:
                    existing_due = existing.due_at.isoformat() if existing.due_at else None
                    new_due = spec.due_at.isoformat()
                    if new_due != existing_due:
                        updates['due_at'] = new_due
                if task_visibility and existing.visibility != task_visibility:
                    updates['visibility'] = task_visibility

                merged_meta = _merge_task_metadata(existing.metadata, base_metadata, editor_ids)
                if merged_meta != (existing.metadata or {}):
                    updates['metadata'] = merged_meta

                if not updates:
                    continue
                try:
                    task = task_manager.update_task(task_id, updates, actor_id=actor_id)
                except PermissionError:
                    logger.warning(f"Inline task update not authorized for {task_id}")
                    continue

                if task and task.visibility == 'network' and p2p_manager and p2p_manager.is_running():
                    try:
                        p2p_manager.broadcast_interaction(
                            item_id=task.id,
                            user_id=actor_id,
                            action='task_update',
                            item_type='task',
                            display_name=sender_display,
                            extra={'task': task.to_dict()},
                        )
                    except Exception as task_err:
                        logger.warning(f"Failed to broadcast inline task update: {task_err}")
            else:
                meta_payload = _merge_task_metadata({}, base_metadata, editor_ids)
                task = task_manager.create_task(
                    task_id=task_id,
                    title=spec.title,
                    description=spec.description,
                    status=spec.status,
                    priority=spec.priority,
                    created_by=actor_id,
                    assigned_to=resolved_assignee,
                    due_at=spec.due_at.isoformat() if spec.due_at else None,
                    visibility=task_visibility,
                    metadata=meta_payload,
                    origin_peer=p2p_manager.get_peer_id() if p2p_manager else None,
                    source_type='human',
                    updated_by=actor_id,
                )

                if task and task.visibility == 'network' and p2p_manager and p2p_manager.is_running():
                    try:
                        p2p_manager.broadcast_interaction(
                            item_id=task.id,
                            user_id=actor_id,
                            action='task_create',
                            item_type='task',
                            display_name=sender_display,
                            extra={'task': task.to_dict()},
                        )
                    except Exception as task_err:
                        logger.warning(f"Failed to broadcast inline task create: {task_err}")

    def _sync_inline_objectives_from_content(
        *,
        objective_manager: Any,
        db_manager: Any,
        content: str,
        scope: str,
        source_id: str,
        actor_id: str,
        objective_visibility: str,
        source_type: str,
        origin_peer: Optional[str] = None,
        created_at: Optional[str] = None,
        visibility: Optional[str] = None,
        permissions: Optional[list[str]] = None,
        channel_id: Optional[str] = None,
    ) -> None:
        from ..core.objectives import parse_objective_blocks, derive_objective_id
        from ..core.tasks import derive_task_id

        if not objective_manager:
            return
        specs = parse_objective_blocks(content or '')
        if not specs:
            return

        for idx, spec in enumerate(cast(Any, specs)):
            spec = cast(Any, spec)
            objective_id = derive_objective_id(scope, source_id, idx, len(specs), override=spec.objective_id)
            members_payload = []
            for member in spec.members or []:
                uid = _resolve_handle_to_user_id(
                    db_manager,
                    member.handle,
                    visibility=visibility,
                    permissions=permissions,
                    channel_id=channel_id,
                    author_id=actor_id,
                )
                if uid:
                    members_payload.append({'user_id': uid, 'role': member.role})

            tasks_payload = []
            task_total = len(spec.tasks or [])
            for t_idx, task in enumerate(spec.tasks or []):
                assignee_id = None
                if task.assignee:
                    assignee_id = _resolve_handle_to_user_id(
                        db_manager,
                        task.assignee,
                        visibility=visibility,
                        permissions=permissions,
                        channel_id=channel_id,
                        author_id=actor_id,
                    )
                task_id = derive_task_id('objective', objective_id, t_idx, task_total)
                tasks_payload.append({
                    'task_id': task_id,
                    'title': task.title,
                    'status': task.status,
                    'assigned_to': assignee_id,
                    'metadata': {
                        'inline_objective_task': True,
                        'source_type': source_type,
                        'source_id': source_id,
                        'channel_id': channel_id,
                    }
                })

            objective_manager.upsert_objective(
                objective_id=objective_id,
                title=spec.title,
                description=spec.description,
                status=spec.status,
                deadline=spec.deadline.isoformat() if spec.deadline else None,
                created_by=actor_id,
                visibility=objective_visibility,
                origin_peer=origin_peer,
                source_type=source_type,
                source_id=source_id,
                created_at=created_at,
                members=members_payload,
                tasks=tasks_payload,
                updated_by=actor_id,
            )

    def _sync_inline_signals_from_content(
        *,
        signal_manager: Any,
        db_manager: Any,
        content: str,
        scope: str,
        source_id: str,
        actor_id: str,
        signal_visibility: str,
        source_type: str,
        origin_peer: Optional[str] = None,
        created_at: Optional[str] = None,
        visibility: Optional[str] = None,
        permissions: Optional[list[str]] = None,
        channel_id: Optional[str] = None,
    ) -> None:
        from ..core.signals import parse_signal_blocks, derive_signal_id

        if not signal_manager:
            return
        specs = parse_signal_blocks(content or '')
        if not specs:
            return

        for idx, spec in enumerate(cast(Any, specs)):
            spec = cast(Any, spec)
            signal_id = derive_signal_id(scope, source_id, idx, len(specs), override=spec.signal_id)
            owner_id = None
            if spec.owner:
                owner_id = _resolve_handle_to_user_id(
                    db_manager,
                    spec.owner,
                    visibility=visibility,
                    permissions=permissions,
                    channel_id=channel_id,
                    author_id=actor_id,
                )
            if not owner_id:
                owner_id = actor_id

            signal_manager.upsert_signal(
                signal_id=signal_id,
                signal_type=spec.signal_type,
                title=spec.title,
                summary=spec.summary,
                status=spec.status,
                confidence=spec.confidence,
                tags=spec.tags,
                data=spec.data,
                notes=spec.notes,
                owner_id=owner_id,
                created_by=actor_id,
                visibility=signal_visibility,
                origin_peer=origin_peer,
                source_type=source_type,
                source_id=source_id,
                expires_at=spec.expires_at.isoformat() if spec.expires_at else None,
                ttl_seconds=spec.ttl_seconds,
                ttl_mode=spec.ttl_mode,
                created_at=created_at,
                actor_id=actor_id,
            )

    def _sync_inline_contracts_from_content(
        *,
        contract_manager: Any,
        db_manager: Any,
        content: str,
        scope: str,
        source_id: str,
        actor_id: str,
        contract_visibility: str,
        source_type: str,
        origin_peer: Optional[str] = None,
        created_at: Optional[str] = None,
        visibility: Optional[str] = None,
        permissions: Optional[list[Any]] = None,
        channel_id: Optional[str] = None,
    ) -> None:
        from ..core.contracts import parse_contract_blocks, derive_contract_id

        if not contract_manager:
            return
        specs = parse_contract_blocks(content or '')
        if not specs:
            return

        for idx, spec in enumerate(specs):
            if not spec.confirmed:
                continue
            contract_id = derive_contract_id(scope, source_id, idx, len(specs), override=spec.contract_id)
            owner_id = None
            if spec.owner:
                owner_id = _resolve_handle_to_user_id(
                    db_manager,
                    spec.owner,
                    visibility=visibility,
                    permissions=permissions,
                    channel_id=channel_id,
                    author_id=actor_id,
                )
            if not owner_id:
                owner_id = actor_id

            counterparties = []
            for cp in spec.counterparties or []:
                cp_id = _resolve_handle_to_user_id(
                    db_manager,
                    cp,
                    visibility=visibility,
                    permissions=permissions,
                    channel_id=channel_id,
                    author_id=actor_id,
                )
                if cp_id:
                    counterparties.append(cp_id)

            contract_manager.upsert_contract(
                contract_id=contract_id,
                title=spec.title,
                summary=spec.summary,
                terms=spec.terms,
                status=spec.status,
                owner_id=owner_id,
                counterparties=counterparties,
                created_by=actor_id,
                visibility=contract_visibility,
                origin_peer=origin_peer,
                source_type=source_type,
                source_id=source_id,
                expires_at=spec.expires_at.isoformat() if spec.expires_at else None,
                ttl_seconds=spec.ttl_seconds,
                ttl_mode=spec.ttl_mode,
                metadata=spec.metadata,
                created_at=created_at,
                actor_id=actor_id,
            )

    def _sync_inline_requests_from_content(
        *,
        request_manager: Any,
        db_manager: Any,
        content: str,
        scope: str,
        source_id: str,
        actor_id: str,
        visibility: str,
        source_type: str,
        origin_peer: Optional[str] = None,
        created_at: Optional[str] = None,
        permissions: Optional[list[str]] = None,
        channel_id: Optional[str] = None,
    ) -> None:
        from ..core.requests import parse_request_blocks, derive_request_id

        if not request_manager:
            return
        specs = parse_request_blocks(content or '')
        if not specs:
            return

        for idx, spec in enumerate(cast(Any, specs)):
            spec = cast(Any, spec)
            if not spec.confirmed:
                continue
            request_id = derive_request_id(scope, source_id, idx, len(specs), override=spec.request_id)
            members_payload = []
            if spec.members:
                for member in spec.members:
                    uid = _resolve_handle_to_user_id(
                        db_manager,
                        member.handle,
                        visibility=visibility,
                        permissions=permissions,
                        channel_id=channel_id,
                        author_id=actor_id,
                    )
                    if uid:
                        members_payload.append({'user_id': uid, 'role': member.role})

            request_manager.upsert_request(
                request_id=request_id,
                title=spec.title,
                created_by=actor_id,
                request_text=spec.request,
                required_output=spec.required_output,
                status=spec.status,
                priority=spec.priority,
                tags=spec.tags,
                due_at=spec.due_at.isoformat() if spec.due_at else None,
                visibility='network' if visibility in ('public', 'network') else 'local',
                origin_peer=origin_peer,
                source_type=source_type,
                source_id=source_id,
                created_at=created_at,
                actor_id=actor_id,
                members=members_payload,
                members_defined=('members' in spec.fields),
                fields=spec.fields,
            )

    def _sync_inline_handoffs_from_content(
        *,
        handoff_manager: Any,
        content: str,
        scope: str,
        source_id: str,
        actor_id: str,
        visibility: str,
        permissions: Optional[list] = None,
        channel_id: Optional[str] = None,
        origin_peer: Optional[str] = None,
        created_at: Optional[str] = None,
    ) -> None:
        from ..core.handoffs import parse_handoff_blocks, derive_handoff_id

        if not handoff_manager:
            return
        handoff_specs = parse_handoff_blocks(content or '')
        if not handoff_specs:
            return

        for idx, spec in enumerate(cast(Any, handoff_specs)):
            spec = cast(Any, spec)
            if not spec.confirmed:
                continue
            handoff_id = derive_handoff_id(scope, source_id, idx, len(handoff_specs), override=spec.handoff_id)
            handoff_manager.upsert_handoff(
                handoff_id=handoff_id,
                source_type=scope,
                source_id=source_id,
                author_id=actor_id,
                title=spec.title,
                summary=spec.summary,
                next_steps=spec.next_steps,
                owner=spec.owner,
                tags=spec.tags,
                raw=spec.raw,
                channel_id=channel_id,
                visibility=visibility,
                origin_peer=origin_peer,
                permissions=permissions,
                created_at=created_at,
                required_capabilities=spec.required_capabilities,
                escalation_level=spec.escalation_level,
                return_to=spec.return_to,
                context_payload=spec.context_payload,
            )

    def _parse_since_window(since_str: Optional[str], window_hours: int = 24) -> datetime:
        now = datetime.now(timezone.utc)
        if since_str:
            raw = str(since_str).strip()
            if raw:
                try:
                    if raw.isdigit():
                        return datetime.fromtimestamp(float(raw), tz=timezone.utc)
                    dt_val = datetime.fromisoformat(raw.replace('Z', '+00:00'))
                    if dt_val.tzinfo is None:
                        dt_val = dt_val.replace(tzinfo=timezone.utc)
                    return dt_val
                except Exception:
                    pass
        try:
            hours = int(window_hours)
        except Exception:
            hours = 24
        return now - timedelta(hours=max(1, hours))
    
    # Health check endpoint
    @api.route('/health', methods=['GET'])
    def health_check():
        """Health check endpoint."""
        from canopy import __version__ as _ver
        return jsonify({
            'status': 'healthy',
            'timestamp': datetime.utcnow().isoformat(),
            'version': _ver
        })

    # ------------------------------------------------------------------ #
    #  Agent instructions (public, no auth) — agents call this first      #
    # ------------------------------------------------------------------ #

    @api.route('/agent-instructions', methods=['GET'])
    def agent_instructions():
        """
        Return instructions for AI agents on how to use Canopy.
        No API key required. Call this first to get auth, endpoints, and examples.
        """
        from canopy import __version__ as _ver
        base = request.url_root.rstrip('/')
        if not base:
            cfg = current_app.config.get('CANOPY_CONFIG')
            port = getattr(getattr(cfg, 'network', None), 'port', None) or 7770
            base = f"http://localhost:{port}"
        instructions_payload = build_agent_instructions_payload(base, _ver)

        # Optional user-scoped directives when caller provides an API key.
        # Keeps endpoint public while enabling lightweight per-agent guidance.
        try:
            db_manager, api_key_manager, _, _, _, _, _, _, _, _, _ = _get_app_components_any(current_app)
            raw_key = _extract_api_key_from_headers(request)
            if raw_key and api_key_manager and db_manager:
                key_info = api_key_manager.validate_key(raw_key)
                if key_info:
                    user = db_manager.get_user(key_info.user_id) or {}
                    directives = None
                    directives_source = 'none'
                    try:
                        directives = normalize_agent_directives(user.get('agent_directives'))
                    except Exception:
                        directives = None
                    if directives:
                        directives_source = 'custom'
                    else:
                        directives = get_default_agent_directives(
                            username=user.get('username'),
                            account_type=user.get('account_type'),
                        )
                        if directives:
                            directives_source = 'default'
                    instructions_payload['agent_directives'] = directives
                    instructions_payload['agent_directives_source'] = {
                        'user_id': key_info.user_id,
                        'username': user.get('username'),
                        'display_name': user.get('display_name') or user.get('username'),
                        'source': directives_source,
                    }
        except Exception as dir_err:
            logger.debug(f"Unable to attach agent directives to /agent-instructions: {dir_err}")

        return jsonify(instructions_payload)

    # Auth status (allowed for pending-approval accounts so they can poll)
    @api.route('/auth/status', methods=['GET'])
    @require_auth()
    def auth_status():
        """Return account status for the authenticated API key. Agents poll this until status is 'active'."""
        db_manager, _, _, _, _, _, _, _, _, _, _ = _get_app_components_any(current_app)
        user = db_manager.get_user(g.api_key_info.user_id)
        if not user:
            return jsonify({'error': 'User not found'}), 404
        key_permissions = [p.value for p in g.api_key_info.permissions] if getattr(g, 'api_key_info', None) else []
        return jsonify({
            'user_id': user['id'],
            'username': user['username'],
            'display_name': user.get('display_name') or user['username'],
            'account_type': (user.get('account_type') or 'human'),
            'status': (user.get('status') or 'active'),
            'permissions': key_permissions,
        })

    # User profile (API key auth)
    @api.route('/profile', methods=['GET'])
    @require_auth()
    def get_profile_api():
        """Get own profile (display_name, bio, avatar_url)."""
        db_manager, _, _, _, _, _, _, _, profile_manager, _, _ = _get_app_components_any(current_app)
        profile = profile_manager.get_profile(g.api_key_info.user_id)
        if not profile:
            return jsonify({'error': 'Profile not found'}), 404
        payload = profile.to_dict()
        effective = None
        source = 'none'
        try:
            effective = normalize_agent_directives(payload.get('agent_directives'))
        except Exception:
            effective = None
        if effective:
            source = 'custom'
        else:
            user_row = db_manager.get_user(g.api_key_info.user_id) if db_manager else None
            if user_row:
                effective = get_default_agent_directives(
                    username=user_row.get('username'),
                    account_type=user_row.get('account_type'),
                )
                if effective:
                    source = 'default'
        payload['agent_directives_effective'] = effective
        payload['agent_directives_source'] = source
        return jsonify(payload)

    @api.route('/profile', methods=['POST'])
    @require_auth()
    def update_profile_api():
        """Update own profile (display_name, bio, avatar_file_id). Broadcasts to P2P peers."""
        db_manager, _, _, _, _, _, _, _, profile_manager, _, p2p_manager = _get_app_components_any(current_app)
        data = request.get_json() or {}
        updates = {}
        if 'display_name' in data:
            updates['display_name'] = (data.get('display_name') or '').strip() or None
        if 'bio' in data:
            updates['bio'] = (data.get('bio') or '').strip() or None
        if 'avatar_file_id' in data:
            updates['avatar_file_id'] = (data.get('avatar_file_id') or '').strip() or None
        if 'account_type' in data:
            # Any authenticated user may declare themselves an agent — this
            # relaxes inbox rate-limits and disables the trusted_only filter so
            # P2P mentions are delivered reliably.  Only 'human' and 'agent' are
            # valid values; anything else is silently clamped to 'human'.
            raw_at = (data.get('account_type') or 'human').strip().lower()
            updates['account_type'] = raw_at if raw_at in ('human', 'agent') else 'human'
        if 'agent_directives' in data:
            owner_id = db_manager.get_instance_owner_user_id() if db_manager else None
            if not owner_id or g.api_key_info.user_id != owner_id:
                return jsonify({'error': 'Only instance admin can edit agent directives'}), 403
            raw_directives = data.get('agent_directives')
            try:
                updates['agent_directives'] = normalize_agent_directives(raw_directives)
            except ValueError as ve:
                return jsonify({'error': str(ve)}), 400
        if not updates:
            return jsonify({'error': 'No profile fields to update'}), 400
        if not profile_manager.update_profile(g.api_key_info.user_id, **updates):
            return jsonify({'error': 'Failed to update profile'}), 500
        try:
            if p2p_manager and p2p_manager.is_running():
                card = profile_manager.get_profile_card(g.api_key_info.user_id)
                if card:
                    p2p_manager.broadcast_profile_update(card)
        except Exception as bcast_err:
            logger.warning(f"Profile broadcast failed: {bcast_err}")
        return jsonify({'success': True})

    # Agent/user registration
    @api.route('/register', methods=['POST'])
    def api_register():
        """
        Register a new user account via API.
        
        Designed for AI agents and remote installations to create their own 
        accounts programmatically. Each agent on each machine should use a 
        unique username (e.g., 'cursor-agent@macmini', 'claude@laptop').
        
        Returns the user_id and a full-permission API key for the new account.
        """
        import secrets as _secrets
        db_manager, api_key_manager, _, _, _, _, _, _, _, _, _ = _get_app_components_any(current_app)
        
        try:
            data = request.get_json()
            if not data:
                return jsonify({'error': 'JSON data required'}), 400
            
            username = data.get('username', '').strip()
            display_name = data.get('display_name', '').strip() or username
            password = data.get('password', '')
            account_type = (data.get('account_type') or 'human').strip().lower()
            if account_type not in ('human', 'agent'):
                account_type = 'human'
            # Optional: auto-approve agent accounts (set CANOPY_AUTO_APPROVE_AGENTS=1)
            auto_approve = (os.getenv('CANOPY_AUTO_APPROVE_AGENTS') or '').strip().lower() in ('1', 'true', 'yes')
            status = 'active' if (account_type == 'agent' and auto_approve) else ('pending_approval' if account_type == 'agent' else 'active')

            if not username or len(username) < 2:
                return jsonify({'error': 'username required (min 2 chars)'}), 400
            if not password:
                return jsonify({'error': 'password required'}), 400
            
            # Validate password strength
            from ..security.password import validate_password_strength, hash_password
            is_valid, error_msg = validate_password_strength(password)
            if not is_valid:
                return jsonify({'error': error_msg}), 400
            
            # Check if username is taken
            existing = db_manager.get_user_by_username(username)
            if existing:
                return jsonify({'error': f'Username "{username}" is already taken'}), 409
            
            # Hash password using bcrypt
            pw_hash = hash_password(password)
            
            user_id = f"user_{_secrets.token_hex(8)}"
            
            # Generate crypto keypair
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
            from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
            from cryptography.hazmat.primitives import serialization
            import base58
            
            ed25519_priv = Ed25519PrivateKey.generate()
            ed25519_pub = ed25519_priv.public_key()
            x25519_priv = X25519PrivateKey.generate()
            x25519_pub = x25519_priv.public_key()
            
            ed25519_pub_b58 = base58.b58encode(ed25519_pub.public_bytes(
                encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
            )).decode()
            ed25519_priv_b58 = base58.b58encode(ed25519_priv.private_bytes(
                encoding=serialization.Encoding.Raw, format=serialization.PrivateFormat.Raw,
                encryption_algorithm=serialization.NoEncryption()
            )).decode()
            x25519_pub_b58 = base58.b58encode(x25519_pub.public_bytes(
                encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
            )).decode()
            x25519_priv_b58 = base58.b58encode(x25519_priv.private_bytes(
                encoding=serialization.Encoding.Raw, format=serialization.PrivateFormat.Raw,
                encryption_algorithm=serialization.NoEncryption()
            )).decode()
            
            success = db_manager.create_user(
                user_id=user_id, username=username,
                public_key=ed25519_pub_b58, password_hash=pw_hash,
                display_name=display_name,
                account_type=account_type, status=status
            )
            
            if not success:
                return jsonify({'error': 'Failed to create account'}), 500
            
            # Store keypair
            db_manager.store_user_keys(user_id, ed25519_pub_b58, ed25519_priv_b58,
                                       x25519_pub_b58, x25519_priv_b58)
            
            # Add to general channel
            try:
                with db_manager.get_connection() as conn:
                    conn.execute("""
                        INSERT OR IGNORE INTO channel_members (channel_id, user_id, role)
                        VALUES ('general', ?, 'member')
                    """, (user_id,))
                    conn.commit()
            except Exception:
                pass
            
            # Generate a full-permission API key for the agent
            all_permissions = [p for p in Permission]
            api_key = api_key_manager.generate_key(user_id, all_permissions)
            
            logger.info(f"Registered new agent/user: '{username}' ({user_id})")
            
            return jsonify({
                'user_id': user_id,
                'username': username,
                'display_name': display_name,
                'account_type': account_type,
                'status': status,
                'api_key': api_key,
                'public_key': ed25519_pub_b58,
                'message': f'Account created. Use the api_key for MCP/API authentication.'
                    + (' Poll GET /api/auth/status until status is "active".' if status == 'pending_approval' else '')
            }), 201
            
        except Exception as e:
            logger.error(f"Registration failed: {e}")
            return jsonify({'error': 'Internal server error'}), 500
    
    # System information (sensitive fields only when authenticated)
    @api.route('/info', methods=['GET'])
    def system_info():
        """Get system information. Full stats/config only with valid API key."""
        db_manager, api_key_manager, _, trust_manager, _, _, _, _, _, config, p2p_manager = _get_app_components_any(current_app)
        try:
            from canopy import __version__ as canopy_version
        except Exception:
            canopy_version = '0.1.0'
        api_key = _extract_api_key_from_headers(request)
        key_info = api_key_manager.validate_key(api_key) if api_key and api_key_manager else None

        if not key_info:
            return jsonify({'version': canopy_version})
        try:
            db_stats = db_manager.get_database_stats()
            trust_stats = trust_manager.get_trust_statistics()
            p2p_status = p2p_manager.get_network_status() if p2p_manager else {'running': False}
            return jsonify({
                'version': canopy_version,
                'database_stats': db_stats,
                'trust_stats': trust_stats,
                'p2p_network': p2p_status,
                'config': {
                    'max_peers': config.network.max_peers,
                    'trust_threshold': config.security.trust_threshold,
                    'max_message_size': config.storage.max_message_size
                }
            })
        except Exception as e:
            logger.error(f"Failed to get system info: {e}")
            return jsonify({'error': 'Failed to get system information'}), 500
    
    # P2P Network endpoints
    @api.route('/p2p/status', methods=['GET'])
    def get_p2p_status():
        """Get P2P network status."""
        *_, p2p_manager = _get_app_components_any(current_app)
        
        if not p2p_manager:
            return jsonify({'error': 'P2P manager not initialized'}), 500
        
        try:
            status = p2p_manager.get_network_status()
            return jsonify(status)
        except Exception as e:
            logger.error(f"Failed to get P2P status: {e}")
            return jsonify({'error': 'Failed to get P2P status'}), 500
    
    @api.route('/p2p/peers', methods=['GET'])
    @require_auth(allow_session=True)
    def get_p2p_peers():
        """Get list of discovered and connected peers."""
        *_, p2p_manager = _get_app_components_any(current_app)
        
        if not p2p_manager:
            return jsonify({'error': 'P2P manager not initialized'}), 500
        
        try:
            discovered_peers = p2p_manager.get_discovered_peers()
            connected_peers = p2p_manager.get_connected_peers()
            peer_versions = (
                p2p_manager.get_peer_versions()
                if hasattr(p2p_manager, 'get_peer_versions')
                else {}
            )
            connected_peer_details = []
            for peer_id in connected_peers:
                ver = peer_versions.get(peer_id, {}) if isinstance(peer_versions, dict) else {}
                connected_peer_details.append({
                    'peer_id': peer_id,
                    'canopy_version': ver.get('canopy_version'),
                    'protocol_version': ver.get('protocol_version'),
                    'compatible_protocol': ver.get('compatible_protocol'),
                })
            
            return jsonify({
                'discovered_peers': discovered_peers,
                'connected_peers': connected_peers,
                'connected_peer_details': connected_peer_details,
                'peer_versions': peer_versions,
                'total_discovered': len(discovered_peers),
                'total_connected': len(connected_peers)
            })
        except Exception as e:
            logger.error(f"Failed to get peers: {e}")
            return jsonify({'error': 'Failed to get peers'}), 500
    
    @api.route('/p2p/invite', methods=['GET'])
    @require_auth(allow_session=True)
    def generate_p2p_invite():
        """Generate an invite code for remote peers to connect."""
        from ..network.invite import generate_invite
        *_, config, p2p_manager = _get_app_components_any(current_app)

        if not p2p_manager or not p2p_manager.identity_manager.local_identity:
            return jsonify({'error': 'P2P identity not initialized'}), 500

        try:
            public_host = request.args.get('public_host')
            public_port = request.args.get('public_port', type=int)
            mesh_port = config.network.mesh_port if config else 7771

            invite = generate_invite(
                p2p_manager.identity_manager,
                mesh_port,
                public_host=public_host,
                public_port=public_port,
            )
            return jsonify({
                'invite_code': invite.encode(),
                'peer_id': invite.peer_id,
                'endpoints': invite.endpoints,
                'raw': invite.to_dict(),
            })
        except Exception as e:
            logger.error(f"Failed to generate invite: {e}", exc_info=True)
            return jsonify({'error': str(e)}), 500

    @api.route('/p2p/invite/import', methods=['POST'])
    @require_auth(allow_session=True)
    def import_p2p_invite():
        """Import an invite code and attempt to connect to the peer."""
        import asyncio
        from ..network.invite import InviteCode, import_invite
        *_, p2p_manager = _get_app_components_any(current_app)

        if not p2p_manager or not p2p_manager.identity_manager.local_identity:
            return jsonify({'error': 'P2P identity not initialized'}), 500

        try:
            data = request.get_json()
            if not data or not data.get('invite_code'):
                return jsonify({'error': 'invite_code required'}), 400

            invite = InviteCode.decode(data['invite_code'])
            result = import_invite(
                p2p_manager.identity_manager,
                p2p_manager.connection_manager,
                invite,
            )

            # Attempt to connect to each endpoint
            connected = False
            if p2p_manager.connection_manager:
                for ep in invite.endpoints:
                    try:
                        _record_connection_event(
                            p2p_manager,
                            invite.peer_id,
                            status='attempt',
                            detail='Invite connection attempt',
                            endpoint=ep,
                        )
                        # Parse ws://host:port
                        addr = ep.replace('ws://', '').replace('wss://', '')
                        host, port_str = addr.rsplit(':', 1)
                        port = int(port_str)

                        # Schedule connection on the P2P manager's event loop
                        # so the WebSocket stays on the persistent loop
                        ev_loop = p2p_manager._event_loop
                        if ev_loop and not ev_loop.is_closed():
                            future = asyncio.run_coroutine_threadsafe(
                                p2p_manager.connection_manager.connect_to_peer(
                                    invite.peer_id, host, port
                                ),
                                ev_loop
                            )
                            connected = future.result(timeout=10.0)
                        else:
                            logger.warning(f"P2P event loop not available for {ep}")
                            connected = False

                        if connected:
                            result['status'] = 'connected'
                            result['connected_endpoint'] = ep
                            _record_connection_event(
                                p2p_manager,
                                invite.peer_id,
                                status='connected',
                                detail='Invite connected',
                                endpoint=ep,
                            )
                            # Trigger channel-sync + catch-up for the new peer
                            try:
                                p2p_manager.trigger_peer_sync(invite.peer_id)
                            except Exception as sync_err:
                                logger.warning(f"Post-connect sync failed for {invite.peer_id}: {sync_err}")
                            break
                    except Exception as ce:
                        logger.warning(f"Connection to {ep} failed: {ce}")
                        continue

            if not connected:
                result['status'] = 'imported_not_connected'
                result['message'] = 'Peer registered but could not connect to any endpoint. Make sure the peer is online and reachable.'
                _record_connection_event(
                    p2p_manager,
                    invite.peer_id,
                    status='failed',
                    detail='Invite connection failed',
                )

            return jsonify(result)

        except ValueError as ve:
            return jsonify({'error': str(ve)}), 400
        except Exception as e:
            logger.error(f"Failed to import invite: {e}", exc_info=True)
            return jsonify({'error': str(e)}), 500

    @api.route('/p2p/introduced', methods=['GET'])
    @require_auth(allow_session=True)
    def get_introduced_peers():
        """Return peers introduced by connected contacts."""
        *_, p2p_manager = _get_app_components_any(current_app)
        if not p2p_manager:
            return jsonify({'introduced_peers': []})
        return jsonify({'introduced_peers': p2p_manager.get_introduced_peers()})

    @api.route('/p2p/known_peers', methods=['GET'])
    @require_auth(allow_session=True)
    def get_known_peers():
        """Return previously known peers (with connection status)."""
        *_, p2p_manager = _get_app_components_any(current_app)
        if not p2p_manager:
            return jsonify({'known_peers': []})
        im = p2p_manager.identity_manager
        connected = set(p2p_manager.get_connected_peers())
        peers = []
        for pid, identity in im.known_peers.items():
            if identity.is_local():
                continue
            peers.append({
                'peer_id': pid,
                'display_name': im.peer_display_names.get(pid, ''),
                'endpoints': im.peer_endpoints.get(pid, []),
                'connected': pid in connected,
            })
        return jsonify({'known_peers': peers})

    @api.route('/p2p/connect_introduced', methods=['POST'])
    @require_auth(allow_session=True)
    def connect_introduced_peer():
        """Connect to a peer that was introduced by a contact."""
        import asyncio
        *_, p2p_manager = _get_app_components_any(current_app)
        if not p2p_manager or not p2p_manager.connection_manager:
            return jsonify({'error': 'P2P not running'}), 500

        data = request.get_json(silent=True) or {}
        peer_id = data.get('peer_id')
        if not peer_id:
            return jsonify({'error': 'peer_id required'}), 400
        force_broker = _as_bool(
            data.get('force_broker')
            or data.get('force_failover')
            or data.get('skip_direct')
        )

        # Look up introduced peer info
        intro = p2p_manager._introduced_peers.get(peer_id)
        if not intro:
            return jsonify({'error': 'Peer not found in introduced list'}), 404

        endpoints = intro.get('endpoints', [])
        if not endpoints:
            return jsonify({'error': 'No endpoints available for this peer'}), 400

        ev_loop = p2p_manager._event_loop
        if not ev_loop or ev_loop.is_closed():
            return jsonify({'error': 'P2P event loop unavailable'}), 500

        direct_attempt_count = 0
        if force_broker:
            _record_connection_event(
                p2p_manager,
                peer_id,
                status='forced_failover',
                detail='Direct connect skipped by caller; testing broker/relay path',
            )
        else:
            for ep in endpoints:
                direct_attempt_count += 1
                try:
                    _record_connection_event(
                        p2p_manager,
                        peer_id,
                        status='attempt',
                        detail='Introduced peer connect attempt',
                        endpoint=ep,
                    )
                    addr = ep.replace('ws://', '').replace('wss://', '')
                    host, port_str = addr.rsplit(':', 1)
                    port = int(port_str)
                    future = asyncio.run_coroutine_threadsafe(
                        p2p_manager.connection_manager.connect_to_peer(
                            peer_id, host, port),
                        ev_loop
                    )
                    connected = future.result(timeout=10.0)
                    if connected:
                        try:
                            p2p_manager.trigger_peer_sync(peer_id)
                        except Exception:
                            pass
                        _record_connection_event(
                            p2p_manager,
                            peer_id,
                            status='connected',
                            detail='Introduced peer connected',
                            endpoint=ep,
                        )
                        return jsonify({
                            'status': 'connected',
                            'peer_id': peer_id,
                            'endpoint': ep,
                            'forced_failover': False,
                            'direct_attempted': True,
                            'direct_attempt_count': direct_attempt_count,
                        })
                except Exception as ce:
                    logger.warning(f"Connect to introduced {ep} failed: {ce}")
                    continue

        # Direct connection failed — try connection brokering.
        # Prefer connected introducers, then other connected peers as fallback.
        attempted_brokers: list[str] = []
        if p2p_manager.relay_policy != 'off':
            broker_candidates: list[str] = []
            seen_brokers: set[str] = set()

            connected_peers: list[str] = []
            try:
                connected_peers = list(p2p_manager.get_connected_peers() or [])
            except Exception:
                connected_peers = []
            connected_set = set(connected_peers)

            local_peer_id = ''
            try:
                local_peer_id = p2p_manager.get_peer_id() or ''
            except Exception:
                local_peer_id = ''

            introducers: list[str] = []
            introduced_via = intro.get('introduced_via', [])
            if isinstance(introduced_via, list):
                for pid in introduced_via:
                    if isinstance(pid, str) and pid:
                        introducers.append(pid)
            introduced_by = intro.get('introduced_by')
            if isinstance(introduced_by, str) and introduced_by:
                introducers.append(introduced_by)

            connected_introducers = [pid for pid in introducers if pid in connected_set]
            disconnected_introducers = [pid for pid in introducers if pid not in connected_set]

            for pid in connected_introducers:
                if pid not in seen_brokers:
                    seen_brokers.add(pid)
                    broker_candidates.append(pid)

            for pid in connected_peers:
                if not pid or pid == peer_id or pid == local_peer_id or pid in seen_brokers:
                    continue
                seen_brokers.add(pid)
                broker_candidates.append(pid)

            for pid in disconnected_introducers:
                if pid not in seen_brokers:
                    seen_brokers.add(pid)
                    broker_candidates.append(pid)

            for broker_peer in broker_candidates:
                attempted_brokers.append(broker_peer)
                try:
                    broker_sent = p2p_manager.send_broker_request(
                        target_peer_id=peer_id,
                        via_peer_id=broker_peer,
                    )
                except Exception as be:
                    logger.warning(f"Broker request via {broker_peer} failed: {be}")
                    broker_sent = False

                if broker_sent:
                    _record_connection_event(
                        p2p_manager,
                        peer_id,
                        status='broker',
                        detail='Broker request sent',
                        via_peer=broker_peer,
                    )
                    return jsonify({
                        'status': 'brokering',
                        'peer_id': peer_id,
                        'via_peer': broker_peer,
                        'attempted_brokers': attempted_brokers,
                        'forced_failover': force_broker,
                        'direct_attempted': not force_broker,
                        'direct_attempt_count': direct_attempt_count,
                        'message': (
                            'Direct connection failed; broker request sent. '
                            'The target peer will attempt to connect back. '
                            'If both peers remain unreachable, use a broker with Full Relay enabled.'
                        ),
                    }), 202

        _record_connection_event(
            p2p_manager,
            peer_id,
            status='failed',
            detail='Introduced peer connection failed',
        )
        guidance = 'Could not connect to any endpoint'
        if attempted_brokers:
            guidance += f" and no broker succeeded ({len(attempted_brokers)} attempted)"
        if p2p_manager.relay_policy != 'full_relay':
            guidance += (
                '. Relay policy is broker_only. '
                'For NAT-restricted peers, enable Full Relay on at least one intermediary.'
            )
        return jsonify({
            'status': 'failed',
            'message': guidance,
            'attempted_brokers': attempted_brokers,
            'relay_policy': getattr(p2p_manager, 'relay_policy', 'broker_only'),
            'forced_failover': force_broker,
            'direct_attempted': not force_broker,
            'direct_attempt_count': direct_attempt_count,
        }), 502

    @api.route('/p2p/reconnect', methods=['POST'])
    @require_auth(allow_session=True)
    def reconnect_known_peer():
        """Reconnect to a previously known peer."""
        import asyncio
        *_, p2p_manager = _get_app_components_any(current_app)
        if not p2p_manager or not p2p_manager.connection_manager:
            return jsonify({'error': 'P2P not running'}), 500

        data = request.get_json()
        peer_id = data.get('peer_id')
        if not peer_id:
            return jsonify({'error': 'peer_id required'}), 400

        if p2p_manager.connection_manager.is_connected(peer_id):
            return jsonify({'status': 'connected', 'peer_id': peer_id,
                            'message': 'Already connected'})

        active_relays = dict(getattr(p2p_manager, '_active_relays', {}) or {})
        relay_via = active_relays.get(peer_id)
        if relay_via:
            relay_name = ''
            try:
                relay_name = (
                    p2p_manager.identity_manager.peer_display_names.get(relay_via)
                    if getattr(p2p_manager, 'identity_manager', None) else ''
                ) or relay_via[:12]
            except Exception:
                relay_name = relay_via[:12]
            return jsonify({
                'status': 'relayed',
                'peer_id': peer_id,
                'relay_via': relay_via,
                'relay_via_name': relay_name,
                'message': f'Connected via relay {relay_name}',
            })

        im = p2p_manager.identity_manager
        endpoints = im.peer_endpoints.get(peer_id, [])
        if not endpoints:
            return jsonify({'error': 'No known endpoints for this peer'}), 400

        ev_loop = p2p_manager._event_loop
        if not ev_loop or ev_loop.is_closed():
            return jsonify({'error': 'P2P event loop unavailable'}), 500

        for ep in endpoints:
            try:
                _record_connection_event(
                    p2p_manager,
                    peer_id,
                    status='attempt',
                    detail='Reconnect attempt',
                    endpoint=ep,
                )
                addr = ep.replace('ws://', '').replace('wss://', '')
                host, port_str = addr.rsplit(':', 1)
                port = int(port_str)
                future = asyncio.run_coroutine_threadsafe(
                    p2p_manager.connection_manager.connect_to_peer(
                        peer_id, host, port),
                    ev_loop
                )
                connected = future.result(timeout=10.0)
                if connected:
                    try:
                        p2p_manager.trigger_peer_sync(peer_id)
                    except Exception:
                        pass
                    _record_connection_event(
                        p2p_manager,
                        peer_id,
                        status='connected',
                        detail='Reconnect succeeded',
                        endpoint=ep,
                    )
                    return jsonify({'status': 'connected', 'peer_id': peer_id,
                                    'endpoint': ep})
            except Exception as ce:
                logger.warning(f"Reconnect to {ep} failed: {ce}")
                continue

        _record_connection_event(
            p2p_manager,
            peer_id,
            status='failed',
            detail='Reconnect failed',
        )
        return jsonify({'status': 'failed',
                        'message': 'Could not reconnect to any endpoint'}), 502

    @api.route('/p2p/reconnect_all', methods=['POST'])
    @require_auth(allow_session=True)
    def reconnect_all_known_peers():
        """Reconnect to all previously known peers (best-effort)."""
        *_, p2p_manager = _get_app_components_any(current_app)
        if not p2p_manager:
            return jsonify({'error': 'P2P not running'}), 500
        ok = p2p_manager.reconnect_known_peers()
        if ok:
            return jsonify({'status': 'scheduled'}), 202
        return jsonify({'status': 'failed', 'message': 'P2P event loop unavailable'}), 500

    @api.route('/p2p/disconnect', methods=['POST'])
    @require_auth(allow_session=True)
    def disconnect_peer():
        """Disconnect from a connected peer."""
        import asyncio
        *_, p2p_manager = _get_app_components_any(current_app)
        if not p2p_manager or not p2p_manager.connection_manager:
            return jsonify({'error': 'P2P not running'}), 500

        data = request.get_json() or {}
        peer_id = data.get('peer_id')
        if not peer_id:
            return jsonify({'error': 'peer_id required'}), 400

        if not p2p_manager.connection_manager.is_connected(peer_id):
            return jsonify({'status': 'not_connected', 'peer_id': peer_id})

        ev_loop = p2p_manager._event_loop
        if not ev_loop or ev_loop.is_closed():
            return jsonify({'error': 'P2P event loop unavailable'}), 500

        try:
            future = asyncio.run_coroutine_threadsafe(
                p2p_manager.connection_manager.disconnect_peer(peer_id),
                ev_loop
            )
            future.result(timeout=10.0)
        except Exception as e:
            logger.warning(f"Disconnect failed for {peer_id}: {e}")
            return jsonify({'status': 'failed', 'message': 'Disconnect failed'}), 500

        return jsonify({'status': 'disconnected', 'peer_id': peer_id})

    @api.route('/p2p/forget', methods=['POST'])
    @require_auth(allow_session=True)
    def forget_peer():
        """Forget a known peer (remove from stored endpoints)."""
        import asyncio
        *_, p2p_manager = _get_app_components_any(current_app)
        if not p2p_manager or not p2p_manager.identity_manager:
            return jsonify({'error': 'P2P not running'}), 500

        data = request.get_json() or {}
        peer_id = data.get('peer_id')
        remove_introduced = data.get('remove_introduced', True)
        if not peer_id:
            return jsonify({'error': 'peer_id required'}), 400

        # Disconnect if currently connected
        try:
            if p2p_manager.connection_manager and p2p_manager.connection_manager.is_connected(peer_id):
                ev_loop = p2p_manager._event_loop
                if ev_loop and not ev_loop.is_closed():
                    future = asyncio.run_coroutine_threadsafe(
                        p2p_manager.connection_manager.disconnect_peer(peer_id),
                        ev_loop
                    )
                    future.result(timeout=10.0)
        except Exception as e:
            logger.warning(f"Disconnect during forget failed for {peer_id}: {e}")

        removed = p2p_manager.identity_manager.remove_known_peer(peer_id)

        if remove_introduced and hasattr(p2p_manager, '_introduced_peers'):
            try:
                p2p_manager._introduced_peers.pop(peer_id, None)
            except Exception:
                pass

        try:
            p2p_manager.record_activity_event({
                'id': f"conn_forget_{peer_id}_{int(time.time() * 1000)}",
                'peer_id': peer_id,
                'kind': 'connection',
                'timestamp': time.time(),
                'status': 'forgotten',
                'detail': 'Peer removed from known list',
            })
        except Exception:
            pass

        return jsonify({'status': 'forgotten' if removed else 'not_found', 'peer_id': peer_id})

    # ------------------------------------------------------------------ #
    #  Relay / brokering status and policy                                 #
    # ------------------------------------------------------------------ #

    @api.route('/p2p/relay_status', methods=['GET'])
    @require_auth(allow_session=True)
    def relay_status():
        """Return relay policy, active relays, and routing table."""
        *_, p2p_manager = _get_app_components_any(current_app)
        if not p2p_manager:
            return jsonify({'error': 'P2P not running'}), 500
        return jsonify(p2p_manager.get_relay_status())

    @api.route('/p2p/promote_direct', methods=['POST'])
    @require_auth(allow_session=True)
    def promote_direct():
        """Drop relay route for a peer and attempt a direct connection."""
        import asyncio as _asyncio
        *_, p2p_manager = _get_app_components_any(current_app)
        if not p2p_manager or not p2p_manager.connection_manager:
            return jsonify({'error': 'P2P not running'}), 500

        data = request.get_json(silent=True) or {}
        peer_id = data.get('peer_id')
        if not peer_id:
            return jsonify({'error': 'peer_id required'}), 400

        ev_loop = p2p_manager._event_loop
        if not ev_loop or ev_loop.is_closed():
            return jsonify({'error': 'P2P event loop unavailable'}), 500

        was_relayed = peer_id in p2p_manager._active_relays

        # Remove relay route so the mesh treats this peer as disconnected
        p2p_manager._active_relays.pop(peer_id, None)
        if p2p_manager.message_router:
            p2p_manager.message_router.remove_route(peer_id)

        # Gather known endpoints for this peer
        endpoints = list(
            p2p_manager.identity_manager.peer_endpoints.get(peer_id, [])
        )
        # Also check introduced peers
        intro = getattr(p2p_manager, '_introduced_peers', {}).get(peer_id)
        if intro:
            for ep in intro.get('endpoints', []):
                if ep not in endpoints:
                    endpoints.append(ep)

        if not endpoints:
            return jsonify({
                'status': 'no_endpoints',
                'peer_id': peer_id,
                'was_relayed': was_relayed,
                'message': 'No known endpoints for direct connection attempt',
            }), 400

        # Try each endpoint
        for ep in endpoints:
            try:
                addr = ep.replace('ws://', '').replace('wss://', '')
                host, port_str = addr.rsplit(':', 1)
                port = int(port_str)
                future = _asyncio.run_coroutine_threadsafe(
                    p2p_manager.connection_manager.connect_to_peer(
                        peer_id, host, port),
                    ev_loop,
                )
                connected = future.result(timeout=10.0)
                if connected:
                    try:
                        p2p_manager.trigger_peer_sync(peer_id)
                    except Exception:
                        pass
                    return jsonify({
                        'status': 'direct',
                        'peer_id': peer_id,
                        'endpoint': ep,
                        'was_relayed': was_relayed,
                    })
            except Exception as ce:
                logger.warning(f"Promote direct {ep} failed: {ce}")
                continue

        return jsonify({
            'status': 'failed',
            'peer_id': peer_id,
            'was_relayed': was_relayed,
            'endpoints_tried': len(endpoints),
            'message': 'Could not establish direct connection on any endpoint',
        }), 502

    @api.route('/p2p/activity', methods=['GET'])
    @require_auth(allow_session=True)
    def p2p_activity():
        """Return connection activity events and per-peer activity timestamps."""
        *_, p2p_manager = _get_app_components_any(current_app)
        if not p2p_manager:
            return jsonify({
                'server_time': time.time(),
                'peers': {},
                'events': [],
                'relay_status': {
                    'relay_policy': 'off',
                    'active_relays': {},
                    'routing_table': {},
                },
                'validation': {
                    'forced_failover_events': 0,
                    'broker_events': 0,
                    'failed_connection_events': 0,
                },
            })

        since = request.args.get('since')
        limit = request.args.get('limit', 100)
        kind_filter = (request.args.get('kind') or '').strip().lower()
        try:
            since_val = float(since) if since is not None and str(since).strip() else None
        except Exception:
            since_val = None
        try:
            limit_val = int(limit)
        except Exception:
            limit_val = 100
        limit_val = max(1, min(limit_val, 500))

        peers: dict[str, dict[str, Any]] = {}
        conn_mgr = getattr(p2p_manager, 'connection_manager', None)
        if conn_mgr:
            try:
                connected_peer_ids = list(conn_mgr.get_connected_peers() or [])
            except Exception:
                connected_peer_ids = []
            for connected_peer_id in connected_peer_ids:
                try:
                    conn = conn_mgr.get_connection(connected_peer_id)
                except Exception:
                    conn = None
                if not conn:
                    continue
                peers[connected_peer_id] = {
                    'connected_at': getattr(conn, 'connected_at', None),
                    'last_activity': getattr(conn, 'last_activity', None),
                    'last_inbound_activity': getattr(conn, 'last_inbound_activity', None),
                    'last_outbound_activity': getattr(conn, 'last_outbound_activity', None),
                }

        events: list[dict[str, Any]] = []
        if hasattr(p2p_manager, 'get_activity_events'):
            try:
                events = p2p_manager.get_activity_events(since=since_val, limit=limit_val)
            except Exception:
                events = []
        if kind_filter:
            events = [evt for evt in events if str(evt.get('kind') or '').strip().lower() == kind_filter]

        forced_failover_events = 0
        broker_events = 0
        failed_connection_events = 0
        for evt in events:
            if str(evt.get('kind') or '').strip().lower() != 'connection':
                continue
            status = str(evt.get('status') or '').strip().lower()
            if status == 'forced_failover':
                forced_failover_events += 1
            elif status == 'broker':
                broker_events += 1
            elif status in {'failed', 'disconnected'}:
                failed_connection_events += 1

        relay_status_payload = {
            'relay_policy': getattr(p2p_manager, 'relay_policy', 'off'),
            'active_relays': {},
            'routing_table': {},
        }
        if hasattr(p2p_manager, 'get_relay_status'):
            try:
                live_status = p2p_manager.get_relay_status()
                if isinstance(live_status, dict):
                    relay_status_payload = live_status
            except Exception:
                pass

        return jsonify({
            'server_time': time.time(),
            'peers': peers,
            'events': events,
            'relay_status': relay_status_payload,
            'validation': {
                'forced_failover_events': forced_failover_events,
                'broker_events': broker_events,
                'failed_connection_events': failed_connection_events,
            },
        })

    @api.route('/p2p/relay_policy', methods=['POST'])
    @require_auth(allow_session=True)
    def set_relay_policy():
        """Update the relay policy.

        Body: {"policy": "off" | "broker_only" | "full_relay"}
        """
        *_, p2p_manager = _get_app_components_any(current_app)
        if not p2p_manager:
            return jsonify({'error': 'P2P not running'}), 500

        data = request.get_json()
        policy = data.get('policy', '').strip().lower()
        if p2p_manager.set_relay_policy(policy):
            return jsonify({'status': 'ok', 'relay_policy': policy})
        return jsonify({'error': 'Invalid policy. Use: off, broker_only, full_relay'}), 400

    # ------------------------------------------------------------------ #
    # Device Profile
    # ------------------------------------------------------------------ #

    @api.route('/device/profile', methods=['GET'])
    @require_auth(allow_session=True)
    def get_device_profile_api():
        """Return this device's profile (name, description, avatar)."""
        from canopy.core.device import get_device_profile, get_device_id
        profile = get_device_profile()
        profile['device_id'] = get_device_id()
        return jsonify(profile)

    @api.route('/device/profile', methods=['POST'])
    @require_auth(allow_session=True)
    def set_device_profile_api():
        """Update this device's profile.

        Body: {"display_name": "...", "description": "...",
               "avatar_b64": "...", "avatar_mime": "..."}
        """
        from canopy.core.device import set_device_profile
        data = request.get_json()
        if not data:
            return jsonify({'error': 'JSON body required'}), 400
        ok = set_device_profile(
            display_name=data.get('display_name'),
            description=data.get('description'),
            avatar_b64=data.get('avatar_b64'),
            avatar_mime=data.get('avatar_mime'),
        )
        if ok:
            # Broadcast updated device profile to connected peers
            *_, p2p_manager = _get_app_components_any(current_app)
            if p2p_manager and p2p_manager.is_running():
                try:
                    p2p_manager.broadcast_profile_update(
                        p2p_manager.get_local_profile_card() or {}
                    )
                except Exception:
                    pass
            return jsonify({'ok': True})
        return jsonify({'error': 'Failed to save'}), 500

    @api.route('/p2p/send', methods=['POST'])
    @require_auth(Permission.WRITE_MESSAGES)
    def send_p2p_message():
        """Send a message via P2P network."""
        *_, p2p_manager = _get_app_components_any(current_app)
        
        if not p2p_manager:
            return jsonify({'error': 'P2P manager not initialized'}), 500
        
        try:
            data = request.get_json()
            if not data:
                return jsonify({'error': 'JSON data required'}), 400
            
            peer_id = data.get('peer_id')
            content = data.get('content')
            metadata = data.get('metadata')
            broadcast = data.get('broadcast', False)
            
            if not content:
                return jsonify({'error': 'Message content required'}), 400
            
            if broadcast:
                # Broadcast message
                success = p2p_manager.broadcast_message(content, metadata)
                return jsonify({
                    'success': success,
                    'broadcast': True,
                    'message': 'Message broadcast to all peers' if success else 'Failed to broadcast'
                })
            else:
                # Direct message
                if not peer_id:
                    return jsonify({'error': 'peer_id required for direct message'}), 400
                
                success = p2p_manager.send_message_to_peer(peer_id, content, metadata)
                return jsonify({
                    'success': success,
                    'peer_id': peer_id,
                    'message': f'Message sent to {peer_id}' if success else 'Failed to send message'
                })
                
        except Exception as e:
            logger.error(f"Failed to send P2P message: {e}")
            return jsonify({'error': 'Internal server error'}), 500
    
    # API Key Management
    @api.route('/keys', methods=['POST'])
    def generate_api_key():
        """
        Generate a new API key.
        
        Security: Requires either an existing MANAGE_KEYS API key in the header,
        or if no API keys exist at all (first-time bootstrap), allows creation
        of the initial admin key without authentication.
        """
        _, api_key_manager, _, _, _, _, _, _, _, _, _ = _get_app_components_any(current_app)
        
        try:
            data = request.get_json()
            if not data:
                return jsonify({'error': 'JSON data required'}), 400
            
            permissions_raw = data.get('permissions', [])
            expires_days = data.get('expires_days')
            
            api_key_header = _extract_api_key_from_headers(request)
            
            # Authentication options:
            # 1) API key with MANAGE_KEYS permission
            # 2) Logged-in web session (for local admin/user UI flows)
            # For agents/bots without a session, /register remains the
            # bootstrap path for first key issuance.
            user_id = None
            if api_key_header:
                key_info = api_key_manager.validate_key(api_key_header, Permission.MANAGE_KEYS)
                if not key_info:
                    return jsonify({'error': 'Invalid API key or insufficient permissions'}), 403
                user_id = key_info.user_id
            else:
                session_user = session.get('user_id')
                if session_user:
                    user_id = session_user
                else:
                    return jsonify({
                        'error': 'Authentication required. Use an API key with MANAGE_KEYS, a logged-in session, or /register for first-time agent bootstrap.'
                    }), 401

            # Keys are always created for the authenticated user — you cannot
            # create keys for other users (prevents impersonation).
            
            if permissions_raw is None:
                permissions_raw = []
            if isinstance(permissions_raw, str):
                permissions_raw = [permissions_raw]
            if not isinstance(permissions_raw, list):
                return jsonify({'error': 'permissions must be a list'}), 400

            # Omitted/empty permissions default to the common agent scope.
            if not permissions_raw:
                permissions = _default_agent_api_permissions()
                permissions_list = [p.value for p in permissions]
            else:
                # Convert permission strings to Permission enums
                try:
                    permissions = [Permission(p) for p in permissions_raw]
                except ValueError as e:
                    return jsonify({'error': f'Invalid permission: {e}'}), 400
                permissions_list = [p.value for p in permissions]
            
            # Generate key
            api_key = api_key_manager.generate_key(user_id, permissions, expires_days)
            
            if api_key:
                return jsonify({
                    'api_key': api_key,
                    'user_id': user_id,
                    'permissions': permissions_list,
                    'expires_days': expires_days
                }), 201
            else:
                return jsonify({'error': 'Failed to generate API key'}), 500
                
        except Exception as e:
            logger.error(f"Failed to generate API key: {e}")
            return jsonify({'error': 'Internal server error'}), 500
    
    @api.route('/keys', methods=['GET'])
    @require_auth(Permission.MANAGE_KEYS)
    def list_api_keys():
        """List API keys for the authenticated user."""
        _, api_key_manager, _, _, _, _, _, _, _, _, _ = _get_app_components_any(current_app)
        
        try:
            keys = api_key_manager.list_keys(g.api_key_info.user_id)
            return jsonify({
                'keys': [key.to_dict() for key in keys]
            })
        except Exception as e:
            logger.error(f"Failed to list API keys: {e}")
            return jsonify({'error': 'Failed to list API keys'}), 500
    
    @api.route('/keys/<key_id>', methods=['DELETE'])
    @require_auth(Permission.MANAGE_KEYS)
    def revoke_api_key(key_id):
        """Revoke an API key."""
        _, api_key_manager, _, _, _, _, _, _, _, _, _ = _get_app_components_any(current_app)
        
        try:
            success = api_key_manager.revoke_key(key_id, g.api_key_info.user_id)
            if success:
                return jsonify({'message': 'API key revoked successfully'})
            else:
                return jsonify({'error': 'API key not found or not owned by user'}), 404
        except Exception as e:
            logger.error(f"Failed to revoke API key: {e}")
            return jsonify({'error': 'Failed to revoke API key'}), 500
    
    # Messaging endpoints
    @api.route('/messages', methods=['POST'])
    @require_auth(Permission.WRITE_MESSAGES)
    def send_message():
        """Send a new direct message."""
        db_manager, _, _, message_manager, _, _, _, _, profile_manager, _, p2p_manager = get_app_components(current_app)
        if not message_manager:
            return jsonify({'error': 'Messaging not available'}), 503
        try:
            data = request.get_json()
            if not data:
                return jsonify({'error': 'JSON data required'}), 400
            
            content = data.get('content') or ''
            recipient_id = str(data.get('recipient_id') or '').strip() or None
            recipient_ids_raw = data.get('recipient_ids') or []
            if isinstance(recipient_ids_raw, str):
                recipient_ids_raw = [value.strip() for value in recipient_ids_raw.split(',') if value.strip()]
            elif not isinstance(recipient_ids_raw, list):
                recipient_ids_raw = []
            message_type_str = data.get('message_type', 'text')
            metadata = data.get('metadata')
            if not isinstance(metadata, dict):
                metadata = {}
            else:
                metadata = dict(metadata)
            top_level_attachments = data.get('attachments')
            if isinstance(top_level_attachments, list):
                metadata['attachments'] = top_level_attachments
            reply_to = str(data.get('reply_to') or '').strip()
            if reply_to:
                metadata['reply_to'] = reply_to
            
            # Warn if caller seems to want a channel message
            if data.get('channel_id'):
                return jsonify({
                    'error': 'Wrong endpoint: POST /api/v1/messages is for DMs only. '
                             'Use POST /api/v1/channels/messages with {channel_id, content} '
                             'to post to a channel (with P2P propagation).'
                }), 400
            
            # Allow attachment-only DMs (content may be empty when attachments are present)
            has_attachments = isinstance(metadata, dict) and bool(metadata.get('attachments'))
            if not content and not has_attachments:
                return jsonify({'error': 'Message content or attachments required'}), 400
            
            try:
                message_type = MessageType(message_type_str)
            except ValueError:
                return jsonify({'error': f'Invalid message type: {message_type_str}'}), 400

            recipients_unique: list[str] = []
            seen_recipient_ids: set[str] = set()
            for raw_recipient in recipient_ids_raw:
                rid = str(raw_recipient or '').strip()
                if not rid or rid in seen_recipient_ids:
                    continue
                seen_recipient_ids.add(rid)
                recipients_unique.append(rid)
            if recipient_id and not recipients_unique:
                recipients_unique = [recipient_id]

            recipients_unique = [rid for rid in recipients_unique if rid != g.api_key_info.user_id]

            effective_recipient_id = recipient_id
            broadcast_targets: list[str] = []
            if len(recipients_unique) > 1:
                group_members = sorted({g.api_key_info.user_id, *recipients_unique})
                effective_recipient_id = compute_group_id(group_members)
                metadata.update({
                    'group_id': effective_recipient_id,
                    'group_members': group_members,
                    'is_group': True,
                })
                broadcast_targets = list(recipients_unique)
            elif recipients_unique:
                effective_recipient_id = recipients_unique[0]
                broadcast_targets = [effective_recipient_id]

            if broadcast_targets:
                metadata['security'] = build_dm_security_summary(
                    db_manager,
                    p2p_manager,
                    broadcast_targets,
                )
            
            # Create and send message
            message = message_manager.create_message(
                g.api_key_info.user_id, content, effective_recipient_id, message_type, metadata if metadata else None
            )
            
            if message and message_manager.send_message(message):
                # Broadcast DM to recipient peer via P2P
                if broadcast_targets and p2p_manager and p2p_manager.is_running():
                    try:
                        sender_display = None
                        if profile_manager:
                            profile = profile_manager.get_profile(g.api_key_info.user_id)
                            if profile:
                                sender_display = profile.display_name or profile.username
                        for target_recipient_id in broadcast_targets:
                            p2p_manager.broadcast_direct_message(
                                sender_id=g.api_key_info.user_id,
                                recipient_id=target_recipient_id,
                                content=content,
                                message_id=message.id,
                                timestamp=message.created_at.isoformat(),
                                display_name=sender_display,
                                metadata=metadata if metadata else None,
                            )
                    except Exception as p2p_err:
                        logger.warning(f"Failed to broadcast DM via P2P: {p2p_err}")

                # Notify recipient inbox for direct messages
                local_target_ids = filter_local_dm_targets(
                    db_manager,
                    p2p_manager,
                    [rid for rid in recipients_unique if rid != g.api_key_info.user_id],
                )
                if local_target_ids:
                    try:
                        inbox_manager = current_app.config.get('INBOX_MANAGER')
                        if inbox_manager:
                            dm_preview = build_dm_preview(content, metadata.get('attachments') or [])
                            payload = {
                                'content': content,
                                'message_id': message.id,
                                'attachments': metadata.get('attachments') or [],
                                'security': metadata.get('security'),
                            }
                            if reply_to:
                                payload['reply_to'] = reply_to
                            if metadata.get('group_id'):
                                payload['group_id'] = metadata.get('group_id')
                            if metadata.get('group_members'):
                                payload['group_members'] = metadata.get('group_members')
                            inbox_manager.sync_source_triggers(
                                source_type='dm',
                                source_id=message.id,
                                trigger_type='dm',
                                target_ids=local_target_ids,
                                sender_user_id=g.api_key_info.user_id,
                                preview=dm_preview,
                                payload=payload,
                                message_id=message.id,
                                source_content=content,
                            )
                    except Exception as inbox_err:
                        logger.warning(f"Failed to create inbox trigger for DM: {inbox_err}")

                response_payload = {
                    'message': message.to_dict(),
                    'status': 'sent',
                }
                if metadata.get('group_id'):
                    response_payload['group_id'] = metadata.get('group_id')
                return jsonify(response_payload), 201
            else:
                return jsonify({'error': 'Failed to send message'}), 500
                
        except Exception as e:
            logger.error(f"Failed to send message: {e}")
            return jsonify({'error': 'Internal server error'}), 500

    @api.route('/messages/reply', methods=['POST'])
    @require_auth(Permission.WRITE_MESSAGES)
    def reply_to_message():
        """Reply to an existing direct message by original message ID."""
        db_manager, _, _, message_manager, _, _, _, _, profile_manager, _, p2p_manager = _get_app_components_any(current_app)
        if not message_manager:
            return jsonify({'error': 'Messaging not available'}), 503
        try:
            data = request.get_json() or {}
            original_message_id = str(
                data.get('message_id') or data.get('original_message_id') or ''
            ).strip()
            content = str(data.get('content') or '')
            metadata = data.get('metadata')
            if not isinstance(metadata, dict):
                metadata = {}
            else:
                metadata = dict(metadata)
            attachments = data.get('attachments')
            if isinstance(attachments, list):
                metadata['attachments'] = attachments

            if data.get('channel_id'):
                return jsonify({
                    'error': 'Wrong endpoint: POST /api/v1/messages/reply is for DM replies only. '
                             'Use POST /api/v1/channels/messages for channel posts.'
                }), 400

            if not original_message_id:
                return jsonify({'error': 'message_id required'}), 400

            original = message_manager.get_message(original_message_id)
            if not original:
                return jsonify({'error': 'Original message not found'}), 404

            original_meta = original.metadata if isinstance(original.metadata, dict) else {}
            group_members = [
                str(member_id).strip()
                for member_id in (original_meta.get('group_members') or [])
                if str(member_id).strip()
            ]
            participants = set(group_members)
            if original.sender_id:
                participants.add(str(original.sender_id).strip())
            if original.recipient_id:
                participants.add(str(original.recipient_id).strip())
            if g.api_key_info.user_id not in participants:
                return jsonify({'error': 'You are not allowed to reply to this direct message'}), 403

            has_attachments = bool(metadata.get('attachments')) if isinstance(metadata, dict) else False
            if not content and not has_attachments:
                return jsonify({'error': 'Message content or attachments required'}), 400

            if group_members:
                group_id = str(original_meta.get('group_id') or original.recipient_id or '').strip()
                recipients = [member_id for member_id in group_members if member_id != g.api_key_info.user_id]
                if not recipients:
                    return jsonify({'error': 'No other group members to reply to'}), 400
                reply_meta = dict(metadata)
                reply_meta.update({
                    'reply_to': original_message_id,
                    'group_id': group_id or compute_group_id(sorted(set(group_members))),
                    'group_members': sorted(set(group_members)),
                    'is_group': True,
                })
                reply_meta['security'] = build_dm_security_summary(
                    db_manager,
                    p2p_manager,
                    recipients,
                )
                effective_recipient_id = reply_meta['group_id']
                broadcast_targets = recipients
            else:
                recipient_id = (
                    str(original.sender_id).strip()
                    if str(original.sender_id or '').strip() != g.api_key_info.user_id
                    else str(original.recipient_id or '').strip()
                )
                if not recipient_id:
                    return jsonify({'error': 'Unable to determine DM recipient'}), 400
                reply_meta = dict(metadata)
                reply_meta['reply_to'] = original_message_id
                reply_meta['security'] = build_dm_security_summary(
                    db_manager,
                    p2p_manager,
                    [recipient_id],
                )
                effective_recipient_id = recipient_id
                broadcast_targets = [recipient_id]

            msg_type = MessageType.FILE if (reply_meta.get('attachments') or []) else MessageType.TEXT
            message = message_manager.create_message(
                sender_id=g.api_key_info.user_id,
                recipient_id=effective_recipient_id,
                content=content,
                message_type=msg_type,
                metadata=reply_meta if reply_meta else None,
            )
            if not message or not message_manager.send_message(message):
                return jsonify({'error': 'Failed to send reply'}), 500

            try:
                inbox_manager = current_app.config.get('INBOX_MANAGER')
                if inbox_manager:
                    local_target_ids = filter_local_dm_targets(db_manager, p2p_manager, broadcast_targets)
                    if local_target_ids:
                        inbox_payload = {
                            'content': content,
                            'message_id': message.id,
                            'reply_to': original_message_id,
                            'attachments': reply_meta.get('attachments') or [],
                            'security': reply_meta.get('security'),
                        }
                        if reply_meta.get('group_id'):
                            inbox_payload['group_id'] = reply_meta.get('group_id')
                        if reply_meta.get('group_members'):
                            inbox_payload['group_members'] = reply_meta.get('group_members')
                        if reply_meta.get('is_group') is not None:
                            inbox_payload['is_group'] = bool(reply_meta.get('is_group'))
                        inbox_manager.sync_source_triggers(
                            source_type='dm',
                            source_id=message.id,
                            trigger_type='dm',
                            target_ids=local_target_ids,
                            sender_user_id=g.api_key_info.user_id,
                            preview=build_dm_preview(content, reply_meta.get('attachments') or []),
                            payload=inbox_payload,
                            message_id=message.id,
                            source_content=content,
                        )
            except Exception as inbox_err:
                logger.warning(f"Failed to create DM reply inbox trigger: {inbox_err}")

            if broadcast_targets and p2p_manager and p2p_manager.is_running():
                try:
                    sender_display = None
                    if profile_manager:
                        profile = profile_manager.get_profile(g.api_key_info.user_id)
                        if profile:
                            sender_display = profile.display_name or profile.username
                    for target_recipient_id in broadcast_targets:
                        p2p_manager.broadcast_direct_message(
                            sender_id=g.api_key_info.user_id,
                            recipient_id=target_recipient_id,
                            content=content,
                            message_id=message.id,
                            timestamp=message.created_at.isoformat(),
                            display_name=sender_display,
                            metadata=reply_meta if reply_meta else None,
                        )
                except Exception as p2p_err:
                    logger.warning(f"Failed to broadcast DM reply via P2P: {p2p_err}")

            response_payload = {
                'success': True,
                'message': message.to_dict(),
                'reply_to': original_message_id,
            }
            if reply_meta.get('group_id'):
                response_payload['group_id'] = reply_meta.get('group_id')
            return jsonify(response_payload), 201
        except Exception as e:
            logger.error(f"Failed to reply to message: {e}")
            return jsonify({'error': 'Internal server error'}), 500

    @api.route('/messages/<message_id>', methods=['PATCH', 'PUT'])
    @require_auth(Permission.WRITE_MESSAGES)
    def update_message(message_id):
        """Update a direct message. Only the author can edit their own message."""
        try:
            db_manager, _, _, message_manager, _, _, _, _, profile_manager, _, p2p_manager = _get_app_components_any(current_app)

            data = request.get_json() or {}
            content = data.get('content')
            attachments = data.get('attachments')
            metadata = data.get('metadata')

            msg = message_manager.get_message(message_id)
            if not msg:
                return jsonify({'error': 'Message not found'}), 404
            if msg.sender_id != g.api_key_info.user_id:
                return jsonify({'error': 'You can only edit your own messages'}), 403

            final_content = msg.content if content is None else str(content).strip()
            existing_meta = msg.metadata or {}
            final_metadata = dict(existing_meta)
            if isinstance(metadata, dict):
                final_metadata.update(metadata)

            if attachments is None:
                final_attachments = final_metadata.get('attachments') or []
            else:
                final_attachments = attachments if isinstance(attachments, list) else []

            if not final_content and not final_attachments:
                return jsonify({'error': 'Message content or attachments required'}), 400

            if final_attachments:
                final_metadata['attachments'] = final_attachments
            else:
                final_metadata.pop('attachments', None)

            group_members_for_security = []
            if isinstance(final_metadata, dict):
                group_members_for_security = [
                    str(member_id).strip()
                    for member_id in (final_metadata.get('group_members') or [])
                    if str(member_id).strip() and str(member_id).strip() != g.api_key_info.user_id
                ]
            target_ids_for_security = group_members_for_security or ([str(msg.recipient_id).strip()] if msg.recipient_id else [])
            if target_ids_for_security:
                final_metadata['security'] = build_dm_security_summary(
                    db_manager,
                    p2p_manager,
                    target_ids_for_security,
                )

            try:
                final_metadata['edited_at'] = datetime.now(timezone.utc).isoformat()
            except Exception:
                pass

            msg_type = MessageType.FILE if final_attachments else MessageType.TEXT
            success = message_manager.update_message(
                message_id=message_id,
                user_id=g.api_key_info.user_id,
                content=final_content,
                message_type=msg_type,
                metadata=final_metadata if final_metadata else None,
                allow_admin=False,
            )
            if not success:
                return jsonify({'error': 'Failed to update message'}), 500

            if msg.recipient_id and p2p_manager and p2p_manager.is_running():
                try:
                    sender_display = None
                    if profile_manager:
                        profile = profile_manager.get_profile(g.api_key_info.user_id)
                        if profile:
                            sender_display = profile.display_name or profile.username
                    group_members = []
                    if isinstance(final_metadata, dict):
                        group_members = [
                            str(member_id).strip()
                            for member_id in (final_metadata.get('group_members') or [])
                            if str(member_id).strip() and str(member_id).strip() != g.api_key_info.user_id
                        ]
                    broadcast_targets = group_members or ([str(msg.recipient_id).strip()] if msg.recipient_id else [])
                    for target_recipient_id in broadcast_targets:
                        p2p_manager.broadcast_direct_message(
                            sender_id=g.api_key_info.user_id,
                            recipient_id=target_recipient_id,
                            content=final_content,
                            message_id=msg.id,
                            timestamp=msg.created_at.isoformat(),
                            display_name=sender_display,
                            metadata=final_metadata if final_metadata else None,
                            update_only=True,
                            edited_at=final_metadata.get('edited_at') if final_metadata else None,
                        )
                except Exception as p2p_err:
                    logger.warning(f"Failed to broadcast DM update via P2P: {p2p_err}")

            try:
                inbox_manager = current_app.config.get('INBOX_MANAGER')
                if inbox_manager:
                    group_members = []
                    if isinstance(final_metadata, dict):
                        group_members = [
                            str(member_id).strip()
                            for member_id in (final_metadata.get('group_members') or [])
                            if str(member_id).strip() and str(member_id).strip() != g.api_key_info.user_id
                        ]
                    target_ids = group_members or ([str(msg.recipient_id).strip()] if msg.recipient_id else [])
                    local_target_ids = filter_local_dm_targets(db_manager, p2p_manager, target_ids)
                    if local_target_ids:
                        dm_preview = build_dm_preview(final_content, final_attachments or [])
                        payload = {
                            'content': final_content,
                            'message_id': message_id,
                            'edited_at': final_metadata.get('edited_at') if isinstance(final_metadata, dict) else None,
                            'attachments': final_attachments or [],
                            'security': final_metadata.get('security') if isinstance(final_metadata, dict) else None,
                        }
                        if isinstance(final_metadata, dict) and final_metadata.get('reply_to'):
                            payload['reply_to'] = final_metadata.get('reply_to')
                        if isinstance(final_metadata, dict) and final_metadata.get('group_id'):
                            payload['group_id'] = final_metadata.get('group_id')
                        if isinstance(final_metadata, dict) and final_metadata.get('group_members'):
                            payload['group_members'] = final_metadata.get('group_members')
                        inbox_manager.sync_source_triggers(
                            source_type='dm',
                            source_id=message_id,
                            trigger_type='dm',
                            target_ids=local_target_ids,
                            sender_user_id=g.api_key_info.user_id,
                            preview=dm_preview,
                            payload=payload,
                            message_id=message_id,
                            source_content=final_content,
                        )
            except Exception as inbox_err:
                logger.warning(f"Failed to refresh DM inbox trigger on edit: {inbox_err}")

            return jsonify({'success': True})
        except Exception as e:
            logger.error(f"Failed to update message: {e}")
            return jsonify({'error': 'Internal server error'}), 500
    
    @api.route('/messages', methods=['GET'])
    @require_auth(Permission.READ_MESSAGES)
    def get_messages():
        """Get messages for the authenticated user."""
        _, _, _, message_manager, _, _, _, _, _, _, _ = _get_app_components_any(current_app)
        if not message_manager:
            return jsonify({'error': 'Messaging not available'}), 503
        try:
            limit = int(request.args.get('limit', 50))
            since_str = request.args.get('since')
            
            since = None
            if since_str:
                since = datetime.fromisoformat(since_str)
            
            messages = message_manager.get_messages(g.api_key_info.user_id, limit, since)
            
            return jsonify({
                'messages': [message.to_dict() for message in messages],
                'count': len(messages)
            })
            
        except Exception as e:
            logger.error(f"Failed to get messages: {e}")
            return jsonify({'error': 'Failed to get messages'}), 500
    
    @api.route('/messages/conversation/<other_user_id>', methods=['GET'])
    @require_auth(Permission.READ_MESSAGES)
    def get_conversation(other_user_id):
        """Get conversation with another user."""
        db_manager, _, _, message_manager, _, _, _, _, _, _, _ = get_app_components(current_app)
        if not message_manager:
            return jsonify({'error': 'Messaging not available'}), 503
        try:
            # 404 guard: reject unknown peer IDs early rather than returning empty list
            if db_manager and not db_manager.get_user(other_user_id):
                return jsonify({'error': 'User not found'}), 404

            limit = int(request.args.get('limit', 50))
            
            messages = message_manager.get_conversation(
                g.api_key_info.user_id, other_user_id, limit
            )
            
            return jsonify({
                'messages': [message.to_dict() for message in messages],
                'other_user_id': other_user_id,
                'count': len(messages)
            })
            
        except Exception as e:
            logger.error(f"Failed to get conversation: {e}")
            return jsonify({'error': 'Failed to get conversation'}), 500

    @api.route('/messages/conversation/group/<group_id>', methods=['GET'])
    @require_auth(Permission.READ_MESSAGES)
    def get_group_conversation(group_id):
        """Get messages for a group DM thread identified by group_id."""
        _, _, _, message_manager, _, _, _, _, _, _, _ = get_app_components(current_app)
        if not message_manager:
            return jsonify({'error': 'Messaging not available'}), 503
        try:
            if not group_id:
                return jsonify({'error': 'group_id required'}), 400

            limit = int(request.args.get('limit', 100))
            messages = message_manager.get_group_conversation(
                g.api_key_info.user_id, group_id, limit
            )

            return jsonify({
                'messages': [m.to_dict() for m in messages],
                'group_id': group_id,
                'count': len(messages),
            })

        except Exception as e:
            logger.error(f"Failed to get group conversation {group_id}: {e}")
            return jsonify({'error': 'Failed to get group conversation'}), 500
    
    @api.route('/messages/<message_id>/read', methods=['POST'])
    @require_auth(Permission.READ_MESSAGES)
    def mark_message_read(message_id):
        """Mark a message as read."""
        _, _, _, message_manager, _, _, _, _, _, _, _ = _get_app_components_any(current_app)
        
        try:
            success = message_manager.mark_message_read(message_id, g.api_key_info.user_id)
            if success:
                return jsonify({'message': 'Message marked as read'})
            else:
                return jsonify({'error': 'Message not found or not accessible'}), 404
        except Exception as e:
            logger.error(f"Failed to mark message as read: {e}")
            return jsonify({'error': 'Failed to mark message as read'}), 500
    
    @api.route('/messages/search', methods=['GET'])
    @require_auth(Permission.READ_MESSAGES)
    def search_messages():
        """Search messages by content."""
        _, _, _, message_manager, _, _, _, _, _, _, _ = _get_app_components_any(current_app)
        
        try:
            query = request.args.get('q', '').strip()
            limit = int(request.args.get('limit', 20))
            
            if not query:
                return jsonify({'error': 'Search query required'}), 400
            
            messages = message_manager.search_messages(g.api_key_info.user_id, query, limit)
            
            return jsonify({
                'messages': [message.to_dict() for message in messages],
                'query': query,
                'count': len(messages)
            })
            
        except Exception as e:
            logger.error(f"Failed to search messages: {e}")
            return jsonify({'error': 'Failed to search messages'}), 500
    
    @api.route('/messages/<message_id>', methods=['DELETE'])
    @require_auth(Permission.WRITE_MESSAGES)
    def delete_message_api(message_id):
        """Delete a direct message (sender only)."""
        _, _, _, message_manager, _, file_manager, _, _, _, _, p2p_manager = _get_app_components_any(current_app)
        try:
            success = message_manager.delete_message(message_id, g.api_key_info.user_id, file_manager=file_manager)
            if success:
                inbox_manager = current_app.config.get('INBOX_MANAGER')
                if inbox_manager:
                    try:
                        inbox_manager.remove_source_triggers(
                            source_type='dm',
                            source_id=message_id,
                            trigger_type='dm',
                        )
                    except Exception as inbox_err:
                        logger.warning(f"Failed to remove DM inbox trigger for delete {message_id}: {inbox_err}")
                if p2p_manager and p2p_manager.is_running():
                    try:
                        p2p_manager.broadcast_delete_signal(
                            signal_id=f"DS{secrets.token_hex(8)}",
                            data_type='direct_message',
                            data_id=message_id,
                            reason='user_deleted',
                        )
                    except Exception as p2p_err:
                        logger.warning(f"Failed to broadcast DM delete via P2P: {p2p_err}")
                return jsonify({'success': True, 'message': 'Message deleted'})
            return jsonify({'error': 'Message not found or not owned by you'}), 404
        except Exception as e:
            logger.error(f"Failed to delete message: {e}")
            return jsonify({'error': 'Internal server error'}), 500

    # Trust management endpoints
    @api.route('/trust', methods=['GET'])
    @require_auth(Permission.VIEW_TRUST)
    def get_trust_scores():
        """Get trust scores for all peers."""
        _, _, trust_manager, _, _, _, _, _, _, _, _ = _get_app_components_any(current_app)
        
        try:
            scores = trust_manager.get_all_trust_scores()
            stats = trust_manager.get_trust_statistics()
            
            return jsonify({
                'trust_scores': scores,
                'statistics': stats
            })
            
        except Exception as e:
            logger.error(f"Failed to get trust scores: {e}")
            return jsonify({'error': 'Failed to get trust scores'}), 500
    
    @api.route('/trust/<peer_id>', methods=['GET'])
    @require_auth(Permission.VIEW_TRUST)
    def get_peer_trust(peer_id):
        """Get trust score for a specific peer."""
        _, _, trust_manager, _, _, _, _, _, _, _, _ = _get_app_components_any(current_app)
        
        try:
            score = trust_manager.get_trust_score(peer_id)
            is_trusted = trust_manager.is_peer_trusted(peer_id)
            
            return jsonify({
                'peer_id': peer_id,
                'trust_score': score,
                'is_trusted': is_trusted
            })
            
        except Exception as e:
            logger.error(f"Failed to get peer trust: {e}")
            return jsonify({'error': 'Failed to get peer trust'}), 500
    
    @api.route('/delete-signals', methods=['POST'])
    @require_auth(Permission.DELETE_DATA)
    def create_delete_signal():
        """Create a delete signal for data removal and broadcast via P2P."""
        _, _, trust_manager, _, _, _, _, _, _, _, p2p_manager = _get_app_components_any(current_app)
        
        try:
            data = request.get_json()
            if not data:
                return jsonify({'error': 'JSON data required'}), 400
            
            target_peer_id = data.get('target_peer_id')
            data_type = data.get('data_type')
            data_id = data.get('data_id')
            reason = data.get('reason')
            
            if not all([target_peer_id, data_type, data_id]):
                return jsonify({'error': 'target_peer_id, data_type, and data_id required'}), 400
            
            signal = trust_manager.create_delete_signal(target_peer_id, data_type, data_id, reason)
            
            if signal:
                # Broadcast the delete signal to P2P peers
                if p2p_manager and p2p_manager.is_running():
                    try:
                        # target_peer_id='*' or 'all' means broadcast to everyone
                        target = None if target_peer_id in ('*', 'all') else target_peer_id
                        p2p_manager.broadcast_delete_signal(
                            signal_id=signal.id,
                            data_type=data_type,
                            data_id=data_id,
                            reason=reason,
                            target_peer=target,
                        )
                        logger.info(f"Delete signal {signal.id} broadcast via P2P")
                    except Exception as bcast_err:
                        logger.warning(f"P2P broadcast of delete signal failed: {bcast_err}")
                
                return jsonify({
                    'delete_signal': signal.to_dict(),
                    'status': 'created_and_broadcast'
                }), 201
            else:
                return jsonify({'error': 'Failed to create delete signal'}), 500
                
        except Exception as e:
            logger.error(f"Failed to create delete signal: {e}")
            return jsonify({'error': 'Internal server error'}), 500
    
    @api.route('/delete-signals', methods=['GET'])
    @require_auth(Permission.VIEW_TRUST)
    def get_delete_signals():
        """Get pending delete signals."""
        _, _, trust_manager, _, _, _, _, _, _, _, _ = _get_app_components_any(current_app)
        
        try:
            target_peer = request.args.get('target_peer_id')
            signals = trust_manager.get_pending_delete_signals(target_peer)
            
            return jsonify({
                'delete_signals': [signal.to_dict() for signal in signals],
                'count': len(signals)
            })
            
        except Exception as e:
            logger.error(f"Failed to get delete signals: {e}")
            return jsonify({'error': 'Failed to get delete signals'}), 500
    
    # Database management endpoints
    @api.route('/database/backup', methods=['POST'])
    @require_auth(Permission.DELETE_DATA)
    def create_database_backup():
        """Create a backup of the database."""
        db_manager, _, _, _, _, _, _, _, _, _, _ = _get_app_components_any(current_app)

        try:
            backup_path = db_manager.backup_database(suffix='manual')
            if backup_path:
                return jsonify({
                    'success': True,
                    'backup_path': str(backup_path),
                    'backup_name': backup_path.name,
                })
            return jsonify({'error': 'Backup failed'}), 500
        except Exception as e:
            logger.error(f"Database backup failed: {e}")
            return jsonify({'error': 'Internal server error'}), 500

    @api.route('/database/cleanup', methods=['POST'])
    @require_auth(Permission.DELETE_DATA)
    def cleanup_database():
        """Clean up old data from the database."""
        db_manager, _, _, _, channel_manager, _, _, _, _, _, _ = _get_app_components_any(current_app)

        try:
            days = int(request.json.get('days', 30)) if request.json else 30
            db_manager.cleanup_old_data(days)
            # Also prune dedup table
            pruned = channel_manager.prune_processed_messages(keep_days=7)
            return jsonify({
                'success': True,
                'message': f'Cleaned up data older than {days} days, pruned {pruned} dedup records',
            })
        except Exception as e:
            logger.error(f"Database cleanup failed: {e}")
            return jsonify({'error': 'Internal server error'}), 500

    @api.route('/database/export', methods=['GET'])
    @require_auth(Permission.DELETE_DATA)
    def export_database():
        """Export database as a downloadable SQLite file."""
        db_manager, _, _, _, _, _, _, _, _, _, _ = _get_app_components_any(current_app)

        try:
            from flask import send_file
            import io
            # Create a fresh backup and send it
            backup_path = db_manager.backup_database(suffix='export')
            if backup_path and backup_path.exists():
                return send_file(
                    str(backup_path),
                    mimetype='application/x-sqlite3',
                    as_attachment=True,
                    download_name=f'canopy_export_{backup_path.stem.split("_")[-1]}.db',
                )
            return jsonify({'error': 'Export failed'}), 500
        except Exception as e:
            logger.error(f"Database export failed: {e}")
            return jsonify({'error': 'Internal server error'}), 500

    # Feed endpoints — all use FeedManager for the feed_posts table
    @api.route('/feed', methods=['POST'])
    @require_auth(Permission.WRITE_FEED)
    def create_feed_post():
        """Create a new feed post."""
        db_manager, _, _, _, _, _, feed_manager, _, profile_manager, _, p2p_manager = _get_app_components_any(current_app)
        
        try:
            data = request.get_json()
            if not data:
                return jsonify({'error': 'JSON data required'}), 400
            
            content = data.get('content')
            post_type = data.get('post_type', 'text')
            visibility = data.get('visibility', 'network')
            permissions = data.get('permissions', [])
            metadata = data.get('metadata')
            expires_at = data.get('expires_at')
            ttl_seconds = data.get('ttl_seconds')
            ttl_mode = data.get('ttl_mode')
            
            if not content:
                return jsonify({'error': 'Post content required'}), 400

            # --- Input validation (cherry-picked from Copilot PR #9) ---
            if len(content) > 50_000:
                return jsonify({'error': 'Content exceeds maximum length (50000 chars)'}), 400
            
            from ..core.feed import PostType, PostVisibility
            from ..core.polls import parse_poll, poll_edit_lock_reason
            from ..core.tasks import parse_task_blocks, derive_task_id
            from ..core.circles import parse_circle_blocks, derive_circle_id
            try:
                pt = PostType(post_type)
            except ValueError:
                pt = PostType.TEXT
            try:
                vis = PostVisibility(visibility)
            except ValueError:
                vis = PostVisibility.NETWORK
            
            # Auto-detect poll posts when content matches poll format
            if pt == PostType.TEXT and parse_poll(content or ''):
                pt = PostType.POLL

            post = feed_manager.create_post(
                author_id=g.api_key_info.user_id,
                content=content,
                post_type=pt,
                visibility=vis,
                metadata=metadata,
                permissions=permissions or None,
                expires_at=expires_at,
                ttl_seconds=ttl_seconds,
                ttl_mode=ttl_mode,
            )
            
            if post:
                # Inline circle creation from [circle] blocks
                try:
                    circle_manager = current_app.config.get('CIRCLE_MANAGER')
                    if circle_manager:
                        circle_specs = parse_circle_blocks(content or '')
                        if circle_specs:
                            for idx, spec in enumerate(cast(Any, circle_specs)):
                                spec = cast(Any, spec)
                                circle_id = derive_circle_id('feed', post.id, idx, len(circle_specs), override=spec.circle_id)
                                facilitator_id = None
                                if spec.facilitator:
                                    facilitator_id = _resolve_handle_to_user_id(
                                        db_manager,
                                        spec.facilitator,
                                        visibility=visibility,
                                        permissions=permissions,
                                        author_id=g.api_key_info.user_id,
                                    )
                                if not facilitator_id:
                                    facilitator_id = g.api_key_info.user_id
                                if spec.participants is not None:
                                    resolved_participants = _resolve_handle_list(
                                        db_manager,
                                        spec.participants,
                                        visibility=visibility,
                                        permissions=permissions,
                                        author_id=g.api_key_info.user_id,
                                    )
                                    spec.participants = resolved_participants

                                circle_manager.upsert_circle(
                                    circle_id=circle_id,
                                    source_type='feed',
                                    source_id=post.id,
                                    created_by=g.api_key_info.user_id,
                                    spec=spec,
                                    facilitator_id=facilitator_id,
                                    visibility=vis.value,
                                    origin_peer=p2p_manager.get_peer_id() if p2p_manager else None,
                                    created_at=post.created_at.isoformat() if getattr(post, 'created_at', None) else None,
                                )
                except Exception as circle_err:
                    logger.warning(f"Inline circle creation failed: {circle_err}")

                # Inline circle responses from [circle-response] blocks
                try:
                    circle_manager = current_app.config.get('CIRCLE_MANAGER')
                    if circle_manager:
                        from ..core.circles import parse_circle_response_blocks
                        responses = parse_circle_response_blocks(content or '')
                        if responses:
                            admin_id = None
                            try:
                                admin_id = db_manager.get_instance_owner_user_id()
                            except Exception:
                                admin_id = None
                            for resp in responses:
                                topic = (resp.get('topic') or '').strip()
                                body = (resp.get('content') or '').strip()
                                if not topic or not body:
                                    continue
                                circle = circle_manager.find_circle_by_topic(topic, channel_id=None)
                                if not circle:
                                    continue
                                entry, err = circle_manager.add_entry(
                                    circle_id=circle.id,
                                    user_id=g.api_key_info.user_id,
                                    entry_type='opinion',
                                    content=body,
                                    admin_user_id=admin_id,
                                    return_error=True,
                                )
                                if not entry:
                                    logger.debug(f"Circle response ignored: {err}")
                                    continue
                                if circle.visibility == 'network' and p2p_manager and p2p_manager.is_running():
                                    try:
                                        p2p_manager.broadcast_interaction(
                                            item_id=entry['id'],
                                            user_id=g.api_key_info.user_id,
                                            action='circle_entry',
                                            item_type='circle_entry',
                                            extra={'circle_id': circle.id, 'entry': entry},
                                        )
                                    except Exception as bcast_err:
                                        logger.warning(f"Failed to broadcast circle entry: {bcast_err}")
                except Exception as resp_err:
                    logger.warning(f"Inline circle response failed: {resp_err}")

                # Inline task creation from [task] blocks
                try:
                    task_manager = current_app.config.get('TASK_MANAGER')
                    if task_manager:
                        task_specs = parse_task_blocks(content or '')
                        if task_specs:
                            task_visibility = 'network' if vis.value in ('public', 'network') else 'local'
                            for idx, spec in enumerate(cast(Any, task_specs)):
                                spec = cast(Any, spec)
                                if not spec.confirmed:
                                    continue
                                task_id = derive_task_id('feed', post.id, idx, len(task_specs), override=spec.task_id)
                                assignee_id = _resolve_handle_to_user_id(
                                    db_manager,
                                    spec.assignee,
                                    visibility=visibility,
                                    permissions=permissions,
                                    author_id=g.api_key_info.user_id,
                                )
                                editor_ids = _resolve_handle_list(
                                    db_manager,
                                    spec.editors or [],
                                    visibility=visibility,
                                    permissions=permissions,
                                    author_id=g.api_key_info.user_id,
                                )
                                meta_payload = {
                                    'inline_task': True,
                                    'source_type': 'feed_post',
                                    'source_id': post.id,
                                    'post_visibility': vis.value,
                                }
                                if editor_ids:
                                    meta_payload['editors'] = editor_ids

                                task = task_manager.create_task(
                                    task_id=task_id,
                                    title=spec.title,
                                    description=spec.description,
                                    status=spec.status,
                                    priority=spec.priority,
                                    created_by=g.api_key_info.user_id,
                                    assigned_to=assignee_id,
                                    due_at=spec.due_at.isoformat() if spec.due_at else None,
                                    visibility=task_visibility,
                                    metadata=meta_payload,
                                    origin_peer=p2p_manager.get_peer_id() if p2p_manager else None,
                                    source_type='agent',
                                    updated_by=g.api_key_info.user_id,
                                )

                                if task and task.visibility == 'network' and p2p_manager and p2p_manager.is_running():
                                    try:
                                        display_name = None
                                        if profile_manager:
                                            profile = profile_manager.get_profile(g.api_key_info.user_id)
                                            if profile:
                                                display_name = profile.display_name or profile.username
                                        p2p_manager.broadcast_interaction(
                                            item_id=task.id,
                                            user_id=g.api_key_info.user_id,
                                            action='task_create',
                                            item_type='task',
                                            display_name=display_name,
                                            extra={'task': task.to_dict()},
                                        )
                                    except Exception as task_err:
                                        logger.warning(f"Failed to broadcast task create: {task_err}")
                except Exception as task_err:
                    logger.warning(f"Inline task creation failed: {task_err}")

                # Inline objective creation from [objective] blocks
                try:
                    objective_manager = current_app.config.get('OBJECTIVE_MANAGER')
                    if objective_manager:
                        obj_visibility = 'network' if vis.value in ('public', 'network') else 'local'
                        _sync_inline_objectives_from_content(
                            objective_manager=objective_manager,
                            db_manager=db_manager,
                            content=content,
                            scope='feed',
                            source_id=post.id,
                            actor_id=g.api_key_info.user_id,
                            objective_visibility=obj_visibility,
                            source_type='feed_post',
                            origin_peer=p2p_manager.get_peer_id() if p2p_manager else None,
                            created_at=post.created_at.isoformat() if getattr(post, 'created_at', None) else None,
                            visibility=visibility,
                            permissions=permissions,
                            channel_id=None,
                        )
                except Exception as obj_err:
                    logger.warning(f"Inline objective creation failed: {obj_err}")

                # Inline request creation from [request] blocks
                try:
                    request_manager = current_app.config.get('REQUEST_MANAGER')
                    if request_manager:
                        _sync_inline_requests_from_content(
                            request_manager=request_manager,
                            db_manager=db_manager,
                            content=content,
                            scope='feed',
                            source_id=post.id,
                            actor_id=g.api_key_info.user_id,
                            visibility=visibility,
                            source_type='feed_post',
                            origin_peer=p2p_manager.get_peer_id() if p2p_manager else None,
                            created_at=post.created_at.isoformat() if getattr(post, 'created_at', None) else None,
                            permissions=permissions,
                            channel_id=None,
                        )
                except Exception as req_err:
                    logger.warning(f"Inline request creation failed: {req_err}")

                # Inline signal creation from [signal] blocks
                try:
                    signal_manager = current_app.config.get('SIGNAL_MANAGER')
                    if signal_manager:
                        sig_visibility = 'network' if vis.value in ('public', 'network') else 'local'
                        _sync_inline_signals_from_content(
                            signal_manager=signal_manager,
                            db_manager=db_manager,
                            content=content,
                            scope='feed',
                            source_id=post.id,
                            actor_id=g.api_key_info.user_id,
                            signal_visibility=sig_visibility,
                            source_type='feed_post',
                            origin_peer=p2p_manager.get_peer_id() if p2p_manager else None,
                            created_at=post.created_at.isoformat() if getattr(post, 'created_at', None) else None,
                            visibility=visibility,
                            permissions=permissions,
                            channel_id=None,
                        )
                except Exception as sig_err:
                    logger.warning(f"Inline signal creation failed: {sig_err}")

                # Inline contract creation from [contract] blocks
                try:
                    contract_manager = current_app.config.get('CONTRACT_MANAGER')
                    if contract_manager:
                        contract_visibility = 'network' if vis.value in ('public', 'network') else 'local'
                        _sync_inline_contracts_from_content(
                            contract_manager=contract_manager,
                            db_manager=db_manager,
                            content=content,
                            scope='feed',
                            source_id=post.id,
                            actor_id=g.api_key_info.user_id,
                            contract_visibility=contract_visibility,
                            source_type='feed_post',
                            origin_peer=p2p_manager.get_peer_id() if p2p_manager else None,
                            created_at=post.created_at.isoformat() if getattr(post, 'created_at', None) else None,
                            visibility=visibility,
                            permissions=permissions,
                            channel_id=None,
                        )
                except Exception as contract_err:
                    logger.warning(f"Inline contract creation failed: {contract_err}")

                # Inline handoff creation from [handoff] blocks
                try:
                    handoff_manager = current_app.config.get('HANDOFF_MANAGER')
                    if handoff_manager:
                        handoff_visibility = 'network' if vis.value in ('public', 'network') else 'local'

                        _sync_inline_handoffs_from_content(
                            handoff_manager=handoff_manager,
                            content=content,
                            scope='feed',
                            source_id=post.id,
                            actor_id=g.api_key_info.user_id,
                            visibility=handoff_visibility,
                            permissions=permissions,
                            channel_id=None,
                            origin_peer=p2p_manager.get_peer_id() if p2p_manager else None,
                            created_at=post.created_at.isoformat() if getattr(post, 'created_at', None) else None,
                        )
                except Exception as handoff_err:
                    logger.warning(f"Inline handoff creation failed: {handoff_err}")

                # Inline skill registration from [skill] blocks
                try:
                    skill_manager = current_app.config.get('SKILL_MANAGER')
                    if skill_manager:
                        from ..core.skills import parse_skill_blocks
                        skill_specs = parse_skill_blocks(content or '')
                        for spec in cast(Any, skill_specs):
                            spec = cast(Any, spec)
                            skill_manager.register_skill(
                                spec,
                                source_type='feed_post',
                                source_id=post.id,
                                channel_id=None,
                                author_id=g.api_key_info.user_id,
                            )
                except Exception as skill_err:
                    logger.warning(f"Inline skill registration failed: {skill_err}")

                # Broadcast to P2P peers
                if p2p_manager and p2p_manager.is_running():
                    try:
                        sender_display = None
                        if profile_manager:
                            profile = profile_manager.get_profile(g.api_key_info.user_id)
                            if profile:
                                sender_display = profile.display_name or profile.username
                        p2p_manager.broadcast_feed_post(
                            post_id=post.id,
                            author_id=post.author_id,
                            content=post.content,
                            post_type=post.post_type.value if hasattr(post.post_type, 'value') else str(post.post_type),
                            visibility=post.visibility.value if hasattr(post.visibility, 'value') else str(post.visibility),
                            timestamp=post.created_at.isoformat() if hasattr(post.created_at, 'isoformat') else str(post.created_at),
                            metadata=post.metadata,
                            expires_at=post.expires_at.isoformat() if getattr(post, 'expires_at', None) else None,
                            ttl_seconds=ttl_seconds,
                            ttl_mode=ttl_mode,
                            display_name=sender_display,
                        )
                    except Exception as p2p_err:
                        logger.warning(f"Failed to broadcast feed post via P2P: {p2p_err}")

                local_mentioned_user_ids: list[str] = []

                # Emit mention events for @handles
                try:
                    mention_manager = current_app.config.get('MENTION_MANAGER')
                    mentions = extract_mentions(content or '')
                    if mention_manager and mentions:
                        targets = resolve_mention_targets(
                            db_manager,
                            mentions,
                            visibility=visibility,
                            permissions=permissions,
                            author_id=g.api_key_info.user_id,
                        )
                        local_peer_id = None
                        try:
                            if p2p_manager:
                                local_peer_id = p2p_manager.get_peer_id()
                        except Exception:
                            local_peer_id = None
                        local_targets, remote_targets = split_mention_targets(targets, local_peer_id=local_peer_id)
                        preview = build_preview(content or '')
                        origin_peer = None
                        if isinstance(metadata, dict):
                            origin_peer = metadata.get('origin_peer')
                        if not origin_peer and p2p_manager:
                            origin_peer = p2p_manager.get_peer_id()

                        if local_targets:
                            record_mention_activity(
                                mention_manager,
                                p2p_manager,
                                target_ids=[cast(str, t.get('user_id')) for t in local_targets if t.get('user_id')],
                                source_type='feed_post',
                                source_id=post.id,
                                author_id=g.api_key_info.user_id,
                                origin_peer=origin_peer or '',
                                channel_id=None,
                                preview=preview,
                                extra_ref={'post_id': post.id},
                                inbox_manager=current_app.config.get('INBOX_MANAGER'),
                                source_content=content,
                            )
                        if remote_targets and p2p_manager:
                            broadcast_mention_interaction(
                                p2p_manager,
                                source_type='feed_post',
                                source_id=post.id,
                                author_id=g.api_key_info.user_id,
                                target_user_ids=[cast(str, t.get('user_id')) for t in remote_targets if t.get('user_id')],
                                preview=preview,
                                channel_id=None,
                                origin_peer=origin_peer,
                            )
                except Exception as mention_err:
                    logger.warning(f"Feed mention processing failed: {mention_err}")

                return jsonify({
                    'post': post.to_dict(),
                    'status': 'created'
                }), 201
            else:
                return jsonify({'error': 'Failed to create post'}), 500
                
        except Exception as e:
            logger.error(f"Failed to create feed post: {e}")
            return jsonify({'error': 'Internal server error'}), 500
    
    @api.route('/feed', methods=['GET'])
    @require_auth(Permission.READ_FEED)
    def get_feed():
        """Get user's personalized feed."""
        _, _, _, _, _, _, feed_manager, _, _, _, _ = _get_app_components_any(current_app)
        
        try:
            feed_manager.purge_expired_posts()
            limit = int(request.args.get('limit', 50))
            algorithm = request.args.get('algorithm', 'chronological')
            
            posts = feed_manager.get_user_feed(
                g.api_key_info.user_id, limit=limit, algorithm=algorithm)
            
            return jsonify({
                'posts': [post.to_dict() for post in posts],
                'count': len(posts)
            })
            
        except Exception as e:
            logger.error(f"Failed to get feed: {e}")
            return jsonify({'error': 'Failed to get feed'}), 500
    
    @api.route('/feed/posts/<post_id>', methods=['GET'])
    @require_auth(Permission.READ_FEED)
    def get_feed_post(post_id):
        """Get a specific feed post."""
        _, _, _, _, _, _, feed_manager, _, _, _, _ = _get_app_components_any(current_app)

        try:
            post = feed_manager.get_post(post_id)
            if not post:
                return jsonify({'error': 'Post not found'}), 404
            
            # Check visibility
            if not post.can_view(g.api_key_info.user_id):
                return jsonify({'error': 'Access denied'}), 403
            
            return jsonify({'post': post.to_dict()})

        except Exception as e:
            logger.error(f"Failed to get post: {e}")
            return jsonify({'error': 'Failed to get post'}), 500

    @api.route('/content-contexts/extract', methods=['POST'])
    @require_auth(Permission.READ_FEED)
    def extract_content_context_api():
        """
        Extract best-effort text context from a URL associated with a source item.
        Source types: url, feed_post, channel_message.
        Context rows are owner-scoped to the caller so each agent/user can maintain notes.
        """
        try:
            db_manager, _, _, _, _, _, feed_manager, _, _, _, _ = _get_app_components_any(current_app)
            _ensure_content_context_schema(db_manager)

            data = request.get_json(silent=True) or {}
            source_type = (data.get('source_type') or 'url').strip().lower()
            source_id = (data.get('source_id') or '').strip()
            url_override = (data.get('url') or '').strip()
            force_refresh = str(data.get('force_refresh', '')).strip().lower() in ('1', 'true', 'yes')

            if source_type not in ('url', 'feed_post', 'channel_message', 'direct_message'):
                return jsonify({'error': 'source_type must be one of: url, feed_post, channel_message, direct_message'}), 400
            if source_type != 'url' and not source_id:
                return jsonify({'error': 'source_id is required for feed_post, channel_message, and direct_message'}), 400

            ok, payload_or_code, payload_or_error = _resolve_source_payload(
                db_manager,
                feed_manager,
                g.api_key_info.user_id,
                source_type,
                source_id,
            )
            if not ok:
                return jsonify({'error': payload_or_error}), int(payload_or_code)

            source_payload = payload_or_code or {}
            source_url = url_override
            if not source_url:
                candidates = source_payload.get('source_url_candidates') or []
                source_url = candidates[0].strip() if candidates else ''
            if not source_url:
                return jsonify({'error': 'No URL found. Provide url or include a URL in the source content.'}), 400

            # Canonicalize YouTube URLs for dedup and stable retrieval.
            video_id = _parse_youtube_video_id(source_url)
            canonical_url = f"https://www.youtube.com/watch?v={video_id}" if video_id else source_url

            safe, reason = _is_safe_external_url(canonical_url)
            if not safe:
                return jsonify({'error': reason}), 400

            owner_user_id = g.api_key_info.user_id
            source_id_key = source_id or ''
            admin_user_id = db_manager.get_instance_owner_user_id() if db_manager else None

            with db_manager.get_connection() as conn:
                existing = conn.execute(
                    """
                    SELECT *
                    FROM content_contexts
                    WHERE source_type = ? AND source_id = ? AND source_url = ? AND owner_user_id = ?
                    LIMIT 1
                    """,
                    (source_type, source_id_key, canonical_url, owner_user_id)
                ).fetchone()

            if existing and not force_refresh:
                return jsonify({
                    'context': _serialize_context_row(existing, g.api_key_info.user_id, admin_user_id),
                    'cached': True,
                    'extracted': False,
                })

            extracted = _extract_external_context(canonical_url)
            stored_url = (extracted.get('canonical_url') or canonical_url).strip() or canonical_url
            safe_stored, reason_stored = _is_safe_external_url(stored_url)
            if not safe_stored:
                stored_url = canonical_url

            metadata = extracted.get('metadata') or {}
            metadata.update({
                'requested_url': source_url,
                'source_url_candidates': source_payload.get('source_url_candidates') or [],
                'source_content_len': len(source_payload.get('content') or ''),
                'source_owner_user_id': source_payload.get('owner_user_id'),
                'extracted_by': g.api_key_info.user_id,
                'extracted_at': datetime.now(timezone.utc).isoformat(),
            })
            if reason_stored and not safe_stored:
                metadata['canonical_url_warning'] = reason_stored

            context_id = existing['id'] if existing else f"ctx_{secrets.token_hex(10)}"
            owner_note = (existing['owner_note'] or '') if existing else ''

            with db_manager.get_connection() as conn:
                conn.execute(
                    """
                    INSERT INTO content_contexts (
                        id, source_type, source_id, source_url, provider, owner_user_id,
                        title, author, transcript_lang, transcript_text, extracted_text,
                        summary_text, owner_note, status, error, metadata
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(source_type, source_id, source_url, owner_user_id) DO UPDATE SET
                        provider = excluded.provider,
                        title = excluded.title,
                        author = excluded.author,
                        transcript_lang = excluded.transcript_lang,
                        transcript_text = excluded.transcript_text,
                        extracted_text = excluded.extracted_text,
                        summary_text = excluded.summary_text,
                        status = excluded.status,
                        error = excluded.error,
                        metadata = excluded.metadata,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    (
                        context_id,
                        source_type,
                        source_id_key,
                        stored_url,
                        (extracted.get('provider') or 'unknown').strip() or 'unknown',
                        owner_user_id,
                        (extracted.get('title') or '').strip(),
                        (extracted.get('author') or '').strip(),
                        (extracted.get('transcript_lang') or '').strip(),
                        extracted.get('transcript_text') or '',
                        extracted.get('extracted_text') or '',
                        extracted.get('summary_text') or '',
                        owner_note,
                        (extracted.get('status') or 'partial').strip() or 'partial',
                        (extracted.get('error') or '').strip(),
                        json.dumps(metadata),
                    ),
                )
                conn.commit()
                row = conn.execute(
                    """
                    SELECT *
                    FROM content_contexts
                    WHERE source_type = ? AND source_id = ? AND source_url = ? AND owner_user_id = ?
                    LIMIT 1
                    """,
                    (source_type, source_id_key, stored_url, owner_user_id)
                ).fetchone()

            if not row:
                return jsonify({'error': 'Context extraction stored no result'}), 500

            return jsonify({
                'context': _serialize_context_row(row, g.api_key_info.user_id, admin_user_id),
                'cached': False,
                'extracted': True,
            })
        except Exception as e:
            logger.error(f"Extract content context failed: {e}")
            return jsonify({'error': 'Failed to extract content context'}), 500

    @api.route('/content-contexts', methods=['GET'])
    @require_auth(Permission.READ_FEED)
    def list_content_contexts_api():
        """List content context rows (owner-scoped by default)."""
        try:
            db_manager, _, _, _, _, _, feed_manager, _, _, _, _ = _get_app_components_any(current_app)
            _ensure_content_context_schema(db_manager)

            source_type = (request.args.get('source_type') or '').strip().lower()
            source_id = (request.args.get('source_id') or '').strip()
            source_url = (request.args.get('source_url') or '').strip()
            owner_param = (request.args.get('owner_user_id') or '').strip()
            limit = request.args.get('limit', 50)
            try:
                limit_i = max(1, min(int(limit), 200))
            except Exception:
                limit_i = 50

            user_id = g.api_key_info.user_id
            admin_user_id = db_manager.get_instance_owner_user_id() if db_manager else None

            owner_user_id = user_id
            if owner_param:
                if owner_param != user_id and (not admin_user_id or user_id != admin_user_id):
                    return jsonify({'error': 'Only admin can read other owners\' context rows'}), 403
                owner_user_id = owner_param

            clauses = ["owner_user_id = ?"]
            params = [owner_user_id]

            if source_type:
                if source_type not in ('url', 'feed_post', 'channel_message', 'direct_message'):
                    return jsonify({'error': 'Invalid source_type filter'}), 400
                clauses.append("source_type = ?")
                params.append(source_type)
            if source_id:
                clauses.append("source_id = ?")
                params.append(source_id)
            if source_url:
                clauses.append("source_url = ?")
                params.append(source_url)

            where_sql = " AND ".join(clauses)
            sql = f"""
                SELECT *
                FROM content_contexts
                WHERE {where_sql}
                ORDER BY updated_at DESC
                LIMIT ?
            """
            params.append(limit_i)

            with db_manager.get_connection() as conn:
                rows = conn.execute(sql, tuple(params)).fetchall()

            contexts = []
            for row in rows:
                row_source_type = (row['source_type'] or '').strip()
                row_source_id = (row['source_id'] or '').strip()
                if row_source_type in ('feed_post', 'channel_message', 'direct_message') and row_source_id:
                    if not _can_user_access_source(db_manager, feed_manager, user_id, row_source_type, row_source_id):
                        continue
                contexts.append(_serialize_context_row(row, user_id, admin_user_id))

            return jsonify({'contexts': contexts, 'count': len(contexts)})
        except Exception as e:
            logger.error(f"List content contexts failed: {e}")
            return jsonify({'error': 'Failed to list content contexts'}), 500

    @api.route('/content-contexts/<context_id>', methods=['GET'])
    @require_auth(Permission.READ_FEED)
    def get_content_context_api(context_id):
        """Get one content context row."""
        try:
            db_manager, _, _, _, _, _, feed_manager, _, _, _, _ = _get_app_components_any(current_app)
            _ensure_content_context_schema(db_manager)

            user_id = g.api_key_info.user_id
            admin_user_id = db_manager.get_instance_owner_user_id() if db_manager else None

            with db_manager.get_connection() as conn:
                row = conn.execute(
                    "SELECT * FROM content_contexts WHERE id = ?",
                    (context_id,)
                ).fetchone()
            if not row:
                return jsonify({'error': 'Context not found'}), 404

            if row['owner_user_id'] != user_id and (not admin_user_id or user_id != admin_user_id):
                return jsonify({'error': 'Access denied'}), 403

            source_type = (row['source_type'] or '').strip()
            source_id = (row['source_id'] or '').strip()
            if source_type in ('feed_post', 'channel_message', 'direct_message') and source_id:
                if not _can_user_access_source(db_manager, feed_manager, user_id, source_type, source_id):
                    return jsonify({'error': 'Access denied'}), 403

            return jsonify({'context': _serialize_context_row(row, user_id, admin_user_id)})
        except Exception as e:
            logger.error(f"Get content context failed: {e}")
            return jsonify({'error': 'Failed to get content context'}), 500

    @api.route('/content-contexts/<context_id>/text', methods=['GET'])
    @require_auth(Permission.READ_FEED)
    def get_content_context_text_api(context_id):
        """Return extracted context as text/plain for easy human/agent ingestion."""
        try:
            db_manager, _, _, _, _, _, feed_manager, _, _, _, _ = _get_app_components_any(current_app)
            _ensure_content_context_schema(db_manager)

            user_id = g.api_key_info.user_id
            admin_user_id = db_manager.get_instance_owner_user_id() if db_manager else None
            with db_manager.get_connection() as conn:
                row = conn.execute(
                    "SELECT * FROM content_contexts WHERE id = ?",
                    (context_id,)
                ).fetchone()
            if not row:
                return jsonify({'error': 'Context not found'}), 404

            if row['owner_user_id'] != user_id and (not admin_user_id or user_id != admin_user_id):
                return jsonify({'error': 'Access denied'}), 403

            source_type = (row['source_type'] or '').strip()
            source_id = (row['source_id'] or '').strip()
            if source_type in ('feed_post', 'channel_message', 'direct_message') and source_id:
                if not _can_user_access_source(db_manager, feed_manager, user_id, source_type, source_id):
                    return jsonify({'error': 'Access denied'}), 403

            payload = _serialize_context_row(row, user_id, admin_user_id)
            return Response(
                payload.get('text_blob') or '',
                mimetype='text/plain; charset=utf-8'
            )
        except Exception as e:
            logger.error(f"Get content context text failed: {e}")
            return jsonify({'error': 'Failed to get content context text'}), 500

    @api.route('/content-contexts/<context_id>/note', methods=['PATCH'])
    @require_auth(Permission.WRITE_FEED)
    def update_content_context_note_api(context_id):
        """Update owner note on a context row (owner or admin)."""
        try:
            db_manager, _, _, _, _, _, feed_manager, _, _, _, _ = _get_app_components_any(current_app)
            _ensure_content_context_schema(db_manager)
            data = request.get_json(silent=True) or {}
            note = data.get('owner_note')
            if note is None and 'note' in data:
                note = data.get('note')
            note_text = (note or '').strip()
            if len(note_text) > 24000:
                return jsonify({'error': 'owner_note is too long (max 24000 chars)'}), 400

            user_id = g.api_key_info.user_id
            admin_user_id = db_manager.get_instance_owner_user_id() if db_manager else None
            with db_manager.get_connection() as conn:
                row = conn.execute(
                    "SELECT * FROM content_contexts WHERE id = ?",
                    (context_id,)
                ).fetchone()
                if not row:
                    return jsonify({'error': 'Context not found'}), 404

                if row['owner_user_id'] != user_id and (not admin_user_id or user_id != admin_user_id):
                    return jsonify({'error': 'Only owner or admin can edit owner_note'}), 403

                source_type = (row['source_type'] or '').strip()
                source_id = (row['source_id'] or '').strip()
                if source_type in ('feed_post', 'channel_message', 'direct_message') and source_id:
                    if not _can_user_access_source(db_manager, feed_manager, user_id, source_type, source_id):
                        return jsonify({'error': 'Access denied'}), 403

                conn.execute(
                    """
                    UPDATE content_contexts
                    SET owner_note = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (note_text, context_id)
                )
                conn.commit()
                updated = conn.execute(
                    "SELECT * FROM content_contexts WHERE id = ?",
                    (context_id,)
                ).fetchone()

            return jsonify({'context': _serialize_context_row(updated, user_id, admin_user_id)})
        except Exception as e:
            logger.error(f"Update content context note failed: {e}")
            return jsonify({'error': 'Failed to update content context note'}), 500

    # Mention events for agents and integrations
    @api.route('/mentions', methods=['GET'])
    @require_auth(Permission.READ_FEED)
    def get_mentions_api():
        """Return mention events for the authenticated user."""
        try:
            mention_manager = current_app.config.get('MENTION_MANAGER')
            if not mention_manager:
                return jsonify({'mentions': [], 'count': 0})

            since = request.args.get('since')
            limit = request.args.get('limit', 50)
            include_ack = request.args.get('include_acknowledged', '').strip().lower() in ('1', 'true', 'yes')

            events = mention_manager.get_mentions(
                user_id=g.api_key_info.user_id,
                since=since,
                limit=limit,
                include_acknowledged=include_ack,
            )
            return jsonify({'mentions': events, 'count': len(events)})
        except Exception as e:
            logger.error(f"Get mentions failed: {e}")
            return jsonify({'error': 'Internal server error'}), 500

    @api.route('/mentions/ack', methods=['POST'])
    @api.route('/mentions/acknowledge', methods=['POST'])
    @api.route('/mentions/acknoledge', methods=['POST'])
    @api.route('/ack', methods=['POST'])
    @api.route('/acknowledge', methods=['POST'])
    @api.route('/acknoledge', methods=['POST'])
    @require_auth(Permission.READ_FEED)
    def acknowledge_mentions_api():
        """Acknowledge mention events for the authenticated user."""
        try:
            mention_manager = current_app.config.get('MENTION_MANAGER')
            if not mention_manager:
                return jsonify({'acknowledged': 0})
            data = request.get_json() or {}
            mention_ids = data.get('mention_ids') or []
            if not isinstance(mention_ids, list):
                return jsonify({'error': 'mention_ids must be a list'}), 400

            count = mention_manager.acknowledge_mentions(
                user_id=g.api_key_info.user_id,
                mention_ids=mention_ids,
            )
            if count == 0 and mention_ids:
                logger.warning(
                    "Mention ack returned 0 for user_id=%r and %d ids; check server log for user_id mismatch or invalid ids",
                    g.api_key_info.user_id,
                    len(mention_ids),
                )
            return jsonify({'acknowledged': count})
        except Exception as e:
            logger.error(f"Acknowledge mentions failed: {e}")
            return jsonify({'error': 'Internal server error'}), 500

    @api.route('/mentions/claim', methods=['GET', 'POST', 'DELETE'])
    @api.route('/claim', methods=['GET', 'POST', 'DELETE'])
    @require_auth(Permission.READ_FEED)
    def mention_claim_api():
        """Claim/release mention sources to prevent duplicate multi-agent replies."""
        try:
            db_manager, _, _, _, _, _, _, _, _, _, _ = _get_app_components_any(current_app)
            mention_manager = current_app.config.get('MENTION_MANAGER')
            if not mention_manager:
                return jsonify({'error': 'Mention manager unavailable'}), 503

            method = request.method
            data = request.get_json(silent=True) or {}
            user_id = g.api_key_info.user_id
            has_manage_keys = bool(
                g.api_key_info and g.api_key_info.has_permission(Permission.MANAGE_KEYS)
            )
            user_row = db_manager.get_user(user_id) if db_manager else None
            username = None
            if user_row:
                username = user_row.get('username') or user_row.get('display_name') or user_id

            mention_id = ''
            inbox_id = ''
            source_type = ''
            source_id = ''
            channel_id = None
            if method == 'GET':
                mention_id = (request.args.get('mention_id') or '').strip()
                inbox_id = (request.args.get('inbox_id') or '').strip()
                source_type = (request.args.get('source_type') or '').strip()
                source_id = (request.args.get('source_id') or '').strip()
            else:
                mention_id = (data.get('mention_id') or '').strip()
                inbox_id = (data.get('inbox_id') or '').strip()
                source_type = (data.get('source_type') or '').strip()
                source_id = (data.get('source_id') or '').strip()
                channel_id = data.get('channel_id')

            if mention_id:
                mention = mention_manager.get_mention_by_id(mention_id)
                if not mention:
                    return jsonify({'error': 'Mention not found'}), 404
                if mention.get('user_id') != user_id:
                    return jsonify({'error': 'Mention does not belong to this user'}), 403
                source_type = mention.get('source_type') or source_type
                source_id = mention.get('source_id') or source_id
                channel_id = mention.get('channel_id') or channel_id

            if inbox_id:
                if not db_manager:
                    return jsonify({'error': 'DB manager unavailable'}), 503
                inbox_channel_id = None
                try:
                    with db_manager.get_connection() as conn:
                        try:
                            inbox_row = conn.execute(
                                """
                                SELECT id, agent_user_id, source_type, source_id, channel_id
                                FROM agent_inbox
                                WHERE id = ?
                                LIMIT 1
                                """,
                                (inbox_id,),
                            ).fetchone()
                            if inbox_row:
                                inbox_channel_id = inbox_row['channel_id']
                        except Exception as inbox_primary_err:
                            # Backward-compatibility for older schemas that predate channel_id.
                            if 'no such column: channel_id' not in str(inbox_primary_err).lower():
                                raise
                            inbox_row = conn.execute(
                                """
                                SELECT id, agent_user_id, source_type, source_id
                                FROM agent_inbox
                                WHERE id = ?
                                LIMIT 1
                                """,
                                (inbox_id,),
                            ).fetchone()
                except Exception as inbox_err:
                    logger.warning(f"Mention claim lookup failed for inbox_id={inbox_id}: {inbox_err}")
                    inbox_row = None

                if not inbox_row:
                    return jsonify({'error': 'Inbox item not found'}), 404

                inbox_owner = str(inbox_row['agent_user_id'] or '').strip()
                if inbox_owner and inbox_owner != user_id and not has_manage_keys:
                    return jsonify({'error': 'Inbox item does not belong to this user'}), 403

                inbox_source_type = str(inbox_row['source_type'] or '').strip()
                inbox_source_id = str(inbox_row['source_id'] or '').strip()

                if source_type and inbox_source_type and source_type != inbox_source_type:
                    return jsonify({'error': 'source_type does not match inbox item'}), 400
                if source_id and inbox_source_id and source_id != inbox_source_id:
                    return jsonify({'error': 'source_id does not match inbox item'}), 400

                source_type = inbox_source_type or source_type
                source_id = inbox_source_id or source_id
                channel_id = inbox_channel_id or channel_id

            if not source_type or not source_id:
                return jsonify({'error': 'source_type and source_id are required (or mention_id/inbox_id)'}), 400

            if method == 'GET':
                claim = mention_manager.get_active_claim(source_type=source_type, source_id=source_id)
                return jsonify({
                    'inbox_id': inbox_id or None,
                    'source_type': source_type,
                    'source_id': source_id,
                    'claim': claim,
                    'claimed': bool(claim and claim.get('active')),
                })

            if method == 'DELETE':
                force = bool(data.get('force')) and has_manage_keys
                result = mention_manager.release_claim(
                    source_type=source_type,
                    source_id=source_id,
                    claimer_user_id=user_id,
                    reason='released_by_api',
                    force=force,
                )
                status = 200 if result.get('released') else (409 if result.get('reason') == 'not_owner' else 404)
                return jsonify({
                    'inbox_id': inbox_id or None,
                    'source_type': source_type,
                    'source_id': source_id,
                    **result,
                }), status

            # POST (claim)
            takeover_requested = bool(data.get('takeover')) and has_manage_keys
            claim_metadata = {}
            if mention_id:
                claim_metadata['mention_id'] = mention_id
            if inbox_id:
                claim_metadata['inbox_id'] = inbox_id
            result = mention_manager.claim_source(
                source_type=source_type,
                source_id=source_id,
                claimer_user_id=user_id,
                claimer_username=username,
                channel_id=channel_id,
                ttl_seconds=data.get('ttl_seconds'),
                allow_takeover=takeover_requested,
                metadata=claim_metadata or None,
            )
            status = 200 if result.get('claimed') else (409 if result.get('reason') == 'already_claimed' else 400)
            payload = {
                'inbox_id': inbox_id or None,
                'source_type': source_type,
                'source_id': source_id,
                **result,
            }

            retry_after_seconds: Optional[int] = None
            if status == 409:
                claim = payload.get('claim') or {}
                expires_at = claim.get('expires_at')
                if expires_at:
                    try:
                        expires_dt = datetime.fromisoformat(str(expires_at).replace('Z', '+00:00'))
                        if expires_dt.tzinfo is None:
                            expires_dt = expires_dt.replace(tzinfo=timezone.utc)
                        remaining = int((expires_dt.astimezone(timezone.utc) - datetime.now(timezone.utc)).total_seconds())
                        retry_after_seconds = max(0, remaining)
                    except Exception:
                        retry_after_seconds = None

                payload.setdefault('action_hint', 'retry_after_ttl')
                if retry_after_seconds is not None:
                    payload['retry_after_seconds'] = retry_after_seconds

            response = jsonify(payload)
            if retry_after_seconds is not None:
                response.headers['Retry-After'] = str(retry_after_seconds)
            return response, status
        except Exception as e:
            logger.error(f"Mention claim API failed: {e}")
            return jsonify({'error': 'Internal server error'}), 500

    @api.route('/mentions/stream', methods=['GET'])
    @require_auth(Permission.READ_FEED)
    def stream_mentions_api():
        """Stream mention events as Server-Sent Events (SSE)."""
        mention_manager = current_app.config.get('MENTION_MANAGER')
        if not mention_manager:
            return jsonify({'error': 'Mention manager unavailable'}), 503

        since = request.args.get('since')
        last_event_id = request.headers.get('Last-Event-ID')
        limit = request.args.get('limit', 50)
        heartbeat = request.args.get('heartbeat', 15)

        try:
            heartbeat_sec = max(5, min(int(heartbeat), 60))
        except Exception:
            heartbeat_sec = 15

        def event_stream():
            last_ts = since
            if not last_ts and last_event_id:
                try:
                    last_evt = mention_manager.get_mention_by_id(last_event_id)
                    if last_evt and last_evt.get('created_at'):
                        last_ts = last_evt.get('created_at')
                except Exception:
                    last_ts = last_ts
            last_heartbeat = time.time()
            yield "retry: 3000\n\n"
            while True:
                try:
                    events = mention_manager.get_mentions(
                        user_id=g.api_key_info.user_id,
                        since=last_ts,
                        limit=limit,
                        include_acknowledged=False,
                    )
                    if events:
                        # Emit oldest -> newest for stable ordering
                        events = list(reversed(events))
                        for evt in events:
                            evt_id = evt.get('id') or ''
                            payload = json.dumps(evt)
                            if evt_id:
                                yield f"id: {evt_id}\n"
                            yield f"event: mention\ndata: {payload}\n\n"
                            last_ts = evt.get('created_at') or last_ts
                    now = time.time()
                    if now - last_heartbeat >= heartbeat_sec:
                        yield f"event: heartbeat\ndata: {int(now)}\n\n"
                        last_heartbeat = now
                    time.sleep(1.5)
                except GeneratorExit:
                    break
                except Exception:
                    time.sleep(2)

        headers = {
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no',
        }
        return Response(event_stream(), mimetype='text/event-stream', headers=headers)

    @api.route('/agents', methods=['GET'])
    @require_auth(Permission.READ_FEED)
    def list_agents_api():
        """List discoverable users/agents with stable mention handles and optional skill summaries."""
        try:
            db_manager, _, _, _, _, _, _, _, _, _, _ = _get_app_components_any(current_app)
            skill_manager = current_app.config.get('SKILL_MANAGER')
            if not db_manager:
                return jsonify({'agents': [], 'count': 0})

            include_humans = _as_bool(request.args.get('include_humans'))
            include_remote = _as_bool(request.args.get('include_remote', '1'))
            active_only = _as_bool(request.args.get('active_only', '1'))
            include_skills = _as_bool(request.args.get('include_skills', '1'))
            query = (request.args.get('q') or '').strip().lower()
            try:
                limit = int(request.args.get('limit', 100))
            except Exception:
                limit = 100
            limit = max(1, min(limit, 500))

            account_type_expr = "LOWER(COALESCE(NULLIF(TRIM(account_type), ''), 'human'))"
            status_expr = "LOWER(COALESCE(NULLIF(TRIM(status), ''), 'active'))"
            origin_peer_expr = "COALESCE(NULLIF(TRIM(origin_peer), ''), '')"

            where = ["id NOT IN ('system', 'local_user')"]
            params: list[Any] = []
            if not include_humans:
                where.append(f"{account_type_expr} = 'agent'")
            if active_only:
                where.append(f"{status_expr} = 'active'")
            if not include_remote:
                where.append(f"{origin_peer_expr} = ''")
            if query:
                where.append(
                    "("
                    "LOWER(COALESCE(username, '')) LIKE ? OR "
                    "LOWER(COALESCE(display_name, '')) LIKE ? OR "
                    "LOWER(COALESCE(bio, '')) LIKE ?"
                    ")"
                )
                like = f"%{query}%"
                params.extend([like, like, like])

            with db_manager.get_connection() as conn:
                rows = conn.execute(
                    f"""
                    SELECT id, username, display_name, account_type, status,
                           origin_peer, bio, created_at
                    FROM users
                    WHERE {' AND '.join(where)}
                    ORDER BY
                        CASE WHEN {account_type_expr} = 'agent' THEN 0 ELSE 1 END,
                        CASE WHEN {status_expr} = 'active' THEN 0 ELSE 1 END,
                        LOWER(COALESCE(display_name, username, id))
                    LIMIT ?
                    """,
                    params + [limit],
                ).fetchall()

                mention_counts: dict[str, int] = {}
                inbox_counts: dict[str, int] = {}
                try:
                    mention_rows = conn.execute(
                        """
                        SELECT user_id, COUNT(*) AS n
                        FROM mention_events
                        WHERE acknowledged_at IS NULL
                        GROUP BY user_id
                        """
                    ).fetchall()
                    for row in mention_rows or []:
                        mention_counts[str(row['user_id'])] = int(row['n'] or 0)
                except Exception:
                    mention_counts = {}
                try:
                    inbox_rows = conn.execute(
                        """
                        SELECT agent_user_id, COUNT(*) AS n
                        FROM agent_inbox
                        WHERE status = 'pending'
                        GROUP BY agent_user_id
                        """
                    ).fetchall()
                    for row in inbox_rows or []:
                        inbox_counts[str(row['agent_user_id'])] = int(row['n'] or 0)
                except Exception:
                    inbox_counts = {}

            skill_map: dict[str, dict[str, Any]] = {}
            if include_skills and skill_manager:
                try:
                    skills = skill_manager.get_skills(limit=2000)
                    for skill in skills or []:
                        author_id = str(skill.get('author_id') or '').strip()
                        if not author_id:
                            continue
                        bucket = skill_map.setdefault(author_id, {'count': 0, 'tags': set(), 'names': set()})
                        bucket['count'] += 1
                        for tag in (skill.get('tags') or []):
                            text = str(tag).strip()
                            if text:
                                bucket['tags'].add(text)
                        name = str(skill.get('name') or '').strip()
                        if name:
                            bucket['names'].add(name)
                except Exception as skill_err:
                    logger.debug(f"Agent discovery skill summary failed: {skill_err}")
                    skill_map = {}

            row_ids = [str(row['id']) for row in (rows or []) if row and row['id']]
            presence_records = get_agent_presence_records(db_manager=db_manager, user_ids=row_ids)

            agents: list[dict[str, Any]] = []
            for row in rows or []:
                account_type_raw = str(row['account_type'] or '').strip().lower()
                account_type = account_type_raw or 'human'
                status_raw = str(row['status'] or '').strip().lower()
                status = status_raw or 'active'
                origin_peer = str(row['origin_peer'] or '').strip()
                row_dict = {
                    'id': row['id'],
                    'username': row['username'],
                    'display_name': row['display_name'],
                    'account_type': account_type,
                    'status': status,
                    'origin_peer': origin_peer,
                    'bio': row['bio'] or '',
                    'created_at': row['created_at'],
                }
                handles = _stable_handle_candidates(row_dict)
                skill_info = skill_map.get(row_dict['id']) or {}
                skill_tags = sorted(list(skill_info.get('tags') or []))[:10]
                skill_names = sorted(list(skill_info.get('names') or []))[:10]
                presence_info = presence_records.get(str(row_dict['id'])) or {}
                presence = build_agent_presence_payload(
                    last_check_in_at=presence_info.get('last_check_in_at'),
                    is_remote=bool((row_dict['origin_peer'] or '').strip()),
                    account_type=row_dict['account_type'],
                )

                agents.append({
                    'user_id': row_dict['id'],
                    'username': row_dict['username'] or '',
                    'display_name': row_dict['display_name'] or row_dict['username'] or row_dict['id'],
                    'account_type': row_dict['account_type'],
                    'status': row_dict['status'],
                    'origin_peer': row_dict['origin_peer'],
                    'is_remote': bool((row_dict['origin_peer'] or '').strip()),
                    'stable_handle': handles[0] if handles else (row_dict['username'] or row_dict['id']),
                    'mention_handles': handles,
                    'bio': row_dict['bio'],
                    'unacked_mentions': mention_counts.get(row_dict['id'], 0),
                    'pending_inbox': inbox_counts.get(row_dict['id'], 0),
                    'capabilities': skill_tags,
                    'skill_count': int(skill_info.get('count') or 0),
                    'skills': skill_names,
                    'last_check_in_at': presence.get('last_check_in_at'),
                    'last_check_in_source': presence_info.get('last_check_in_source'),
                    'presence': presence,
                    'presence_state': presence.get('state'),
                    'presence_label': presence.get('label'),
                    'presence_color': presence.get('color'),
                    'presence_age_seconds': presence.get('age_seconds'),
                    'presence_age_text': presence.get('age_text'),
                    'created_at': row_dict['created_at'],
                })

            return jsonify({
                'agents': agents,
                'count': len(agents),
                'filters': {
                    'include_humans': include_humans,
                    'include_remote': include_remote,
                    'active_only': active_only,
                    'include_skills': include_skills,
                    'query': query,
                    'limit': limit,
                },
            })
        except Exception as e:
            logger.error(f"List agents failed: {e}")
            return jsonify({'error': 'Internal server error'}), 500

    @api.route('/agents/system-health', methods=['GET'])
    @require_auth(Permission.READ_FEED)
    def agent_system_health_api():
        """Operational system health snapshot for agent and admin diagnostics."""
        try:
            db_manager, _, _, _, _, _, _, _, _, _, p2p_manager = _get_app_components_any(current_app)
            now = datetime.now(timezone.utc)
            uptime_seconds = int(max(0, (now - API_BOOT_TIME).total_seconds()))

            db_size_bytes = None
            if db_manager and getattr(db_manager, 'db_path', None):
                try:
                    db_size_bytes = os.path.getsize(str(db_manager.db_path))
                except Exception:
                    db_size_bytes = None

            pending_inbox_total = 0
            unacked_mentions_total = 0
            total_users = 0
            active_agents = 0
            if db_manager:
                try:
                    with db_manager.get_connection() as conn:
                        row = conn.execute(
                            "SELECT COUNT(*) AS n FROM agent_inbox WHERE status = 'pending'"
                        ).fetchone()
                        pending_inbox_total = int((row['n'] if row else 0) or 0)
                        row = conn.execute(
                            "SELECT COUNT(*) AS n FROM mention_events WHERE acknowledged_at IS NULL"
                        ).fetchone()
                        unacked_mentions_total = int((row['n'] if row else 0) or 0)
                        row = conn.execute(
                            "SELECT COUNT(*) AS n FROM users WHERE id NOT IN ('system', 'local_user')"
                        ).fetchone()
                        total_users = int((row['n'] if row else 0) or 0)
                        row = conn.execute(
                            """
                            SELECT COUNT(*) AS n
                            FROM users
                            WHERE id NOT IN ('system', 'local_user')
                              AND COALESCE(account_type, 'human') = 'agent'
                              AND COALESCE(status, 'active') = 'active'
                            """
                        ).fetchone()
                        active_agents = int((row['n'] if row else 0) or 0)
                except Exception as db_err:
                    logger.debug(f"System-health DB metrics failed: {db_err}")

            diagnostics = {}
            connected_peers = []
            known_peers_count = 0
            pending_messages_total = 0
            sync_queue_depth = 0
            if p2p_manager:
                try:
                    diagnostics = p2p_manager.get_mesh_diagnostics() or {}
                    connected_peers = diagnostics.get('connected_peers') or p2p_manager.get_connected_peers() or []
                    known_peers_count = int(diagnostics.get('known_peers_count') or 0)
                    pending_messages_total = int(((diagnostics.get('pending_messages') or {}).get('total') or 0))
                    sync_queue_depth = int(((diagnostics.get('sync') or {}).get('queue_depth') or 0))
                except Exception as mesh_err:
                    logger.debug(f"System-health mesh diagnostics failed: {mesh_err}")
                    connected_peers = p2p_manager.get_connected_peers() or []
                    known_peers_count = len(getattr(getattr(p2p_manager, 'identity_manager', None), 'known_peers', {}) or {})

            needs_attention = any([
                pending_inbox_total > 1000,
                unacked_mentions_total > 1000,
                pending_messages_total > 500,
                sync_queue_depth > 100,
            ])

            return jsonify({
                'timestamp': now.isoformat(),
                'started_at': API_BOOT_TIME.isoformat(),
                'uptime_seconds': uptime_seconds,
                'db': {
                    'size_bytes': db_size_bytes,
                },
                'users': {
                    'total': total_users,
                    'active_agents': active_agents,
                },
                'queues': {
                    'unacked_mentions': unacked_mentions_total,
                    'pending_inbox': pending_inbox_total,
                    'pending_p2p_messages': pending_messages_total,
                    'sync_queue_depth': sync_queue_depth,
                },
                'peers': {
                    'connected_count': len(connected_peers or []),
                    'connected_peers': connected_peers or [],
                    'known_peers_count': known_peers_count,
                },
                'needs_attention': needs_attention,
                'poll_hint_seconds': 5 if needs_attention else 30,
            })
        except Exception as e:
            logger.error(f"System health failed: {e}")
            return jsonify({'error': 'Internal server error'}), 500

    # Agent action inbox (pull-first triggers)
    @api.route('/agents/me', methods=['GET'])
    @require_auth(Permission.READ_FEED)
    def get_agent_me():
        """Return profile and account info for the authenticated user/agent.

        Useful for agents to confirm their own user_id, account_type, username,
        and avatar without a separate profile lookup.
        """
        try:
            db_manager = get_app_components(current_app)[0]
            user_id = g.api_key_info.user_id
            _touch_agent_presence(user_id, 'agents_me')
            user_row = db_manager.get_user(user_id) if db_manager else None
            if not user_row:
                return jsonify({'error': 'User not found'}), 404
            return jsonify({
                'user_id': user_id,
                'username': user_row.get('username') or '',
                'display_name': user_row.get('display_name') or user_row.get('username') or '',
                'account_type': user_row.get('account_type') or 'human',
                'bio': user_row.get('bio') or '',
                'avatar_file_id': user_row.get('avatar_file_id') or None,
                'created_at': str(user_row.get('created_at') or ''),
            })
        except Exception as e:
            logger.error(f"GET /agents/me failed: {e}")
            return jsonify({'error': 'Internal server error'}), 500

    @api.route('/agents/me/inbox', methods=['GET'])
    @require_auth(Permission.READ_FEED)
    def get_agent_inbox_api():
        """Return agent inbox items for the authenticated user."""
        try:
            inbox_manager = current_app.config.get('INBOX_MANAGER')
            if not inbox_manager:
                return jsonify({'items': [], 'count': 0})
            _touch_agent_presence(g.api_key_info.user_id, 'inbox')
            status = request.args.get('status')
            limit = request.args.get('limit', 50)
            since = request.args.get('since')
            include_handled = request.args.get('include_handled', '').strip().lower() in ('1', 'true', 'yes')
            items = inbox_manager.list_items(
                user_id=g.api_key_info.user_id,
                status=status,
                limit=limit,
                since=since,
                include_handled=include_handled,
            )
            return jsonify({'items': items, 'count': len(items)})
        except Exception as e:
            logger.error(f"Get agent inbox failed: {e}")
            return jsonify({'error': 'Internal server error'}), 500

    @api.route('/agents/me/inbox/count', methods=['GET'])
    @require_auth(Permission.READ_FEED)
    def get_agent_inbox_count_api():
        """Return count of agent inbox items for the authenticated user."""
        try:
            inbox_manager = current_app.config.get('INBOX_MANAGER')
            if not inbox_manager:
                return jsonify({'count': 0})
            _touch_agent_presence(g.api_key_info.user_id, 'inbox_count')
            status = request.args.get('status')
            count = inbox_manager.count_items(
                user_id=g.api_key_info.user_id,
                status=status,
            )
            return jsonify({'count': count})
        except Exception as e:
            logger.error(f"Get agent inbox count failed: {e}")
            return jsonify({'error': 'Internal server error'}), 500

    @api.route('/agents/me/inbox', methods=['PATCH'])
    @require_auth(Permission.READ_FEED)
    def update_agent_inbox_api():
        """Batch update inbox items for the authenticated user."""
        try:
            inbox_manager = current_app.config.get('INBOX_MANAGER')
            if not inbox_manager:
                return jsonify({'updated': 0})
            data = request.get_json() or {}
            ids = data.get('ids') or []
            if not isinstance(ids, list):
                return jsonify({'error': 'ids must be a list'}), 400
            status = (data.get('status') or 'handled').strip().lower()
            updated = inbox_manager.update_items(
                user_id=g.api_key_info.user_id,
                ids=ids,
                status=status,
            )
            return jsonify({'updated': updated})
        except Exception as e:
            logger.error(f"Update agent inbox failed: {e}")
            return jsonify({'error': 'Internal server error'}), 500

    @api.route('/agents/me/inbox/<item_id>', methods=['PATCH'])
    @require_auth(Permission.READ_FEED)
    def update_agent_inbox_item_api(item_id):
        """Update a single inbox item for the authenticated user."""
        try:
            inbox_manager = current_app.config.get('INBOX_MANAGER')
            if not inbox_manager:
                return jsonify({'updated': 0})
            data = request.get_json() or {}
            status = (data.get('status') or 'handled').strip().lower()
            updated = inbox_manager.update_items(
                user_id=g.api_key_info.user_id,
                ids=[item_id],
                status=status,
            )
            return jsonify({'updated': updated})
        except Exception as e:
            logger.error(f"Update agent inbox item failed: {e}")
            return jsonify({'error': 'Internal server error'}), 500

    @api.route('/agents/me/inbox/config', methods=['GET', 'PATCH'])
    @require_auth(Permission.READ_FEED)
    def agent_inbox_config_api():
        """Get or update inbox config for the authenticated user."""
        try:
            inbox_manager = current_app.config.get('INBOX_MANAGER')
            if not inbox_manager:
                return jsonify({'config': {}})
            if request.method == 'GET':
                config = inbox_manager.get_config(g.api_key_info.user_id)
                return jsonify({'config': config})
            data = request.get_json() or {}
            if not isinstance(data, dict):
                return jsonify({'error': 'config body must be an object'}), 400
            config = inbox_manager.set_config(g.api_key_info.user_id, data)
            return jsonify({'config': config})
        except Exception as e:
            logger.error(f"Agent inbox config failed: {e}")
            return jsonify({'error': 'Internal server error'}), 500

    @api.route('/agents/me/inbox/stats', methods=['GET'])
    @require_auth(Permission.READ_FEED)
    def agent_inbox_stats_api():
        """Return inbox stats and recent rejection counts for the authenticated user."""
        try:
            inbox_manager = current_app.config.get('INBOX_MANAGER')
            if not inbox_manager:
                return jsonify({'stats': {}})
            window_hours = request.args.get('window_hours', 24)
            stats = inbox_manager.get_stats(
                user_id=g.api_key_info.user_id,
                window_hours=window_hours,
            )
            return jsonify({'stats': stats})
        except Exception as e:
            logger.error(f"Agent inbox stats failed: {e}")
            return jsonify({'error': 'Internal server error'}), 500

    @api.route('/agents/me/inbox/audit', methods=['GET'])
    @require_auth(Permission.READ_FEED)
    def agent_inbox_audit_api():
        """Return recent inbox rejection audit entries."""
        try:
            inbox_manager = current_app.config.get('INBOX_MANAGER')
            if not inbox_manager:
                return jsonify({'items': [], 'count': 0})
            limit = request.args.get('limit', 50)
            since = request.args.get('since')
            items = inbox_manager.list_audit(
                user_id=g.api_key_info.user_id,
                limit=limit,
                since=since,
            )
            return jsonify({'items': items, 'count': len(items)})
        except Exception as e:
            logger.error(f"Agent inbox audit failed: {e}")
            return jsonify({'error': 'Internal server error'}), 500

    @api.route('/handoffs', methods=['GET'])
    @require_auth(Permission.READ_FEED)
    def list_handoffs_api():
        """List handoff notes."""
        try:
            handoff_manager = current_app.config.get('HANDOFF_MANAGER')
            profile_manager = current_app.config.get('PROFILE_MANAGER')
            if not handoff_manager:
                return jsonify({'handoffs': [], 'count': 0})
            limit = request.args.get('limit', 50)
            since = request.args.get('since')
            channel_id = request.args.get('channel_id')
            author_id = request.args.get('author_id')
            source_type = request.args.get('source_type')
            handoffs = handoff_manager.list_handoffs(
                limit=limit,
                since=since,
                channel_id=channel_id,
                author_id=author_id,
                source_type=source_type,
                viewer_id=g.api_key_info.user_id,
            )
            return jsonify({'handoffs': [h.to_dict() for h in handoffs], 'count': len(handoffs)})
        except Exception as e:
            logger.error(f"Failed to list handoffs: {e}")
            return jsonify({'error': 'Internal server error'}), 500

    @api.route('/handoffs/<handoff_id>', methods=['GET'])
    @require_auth(Permission.READ_FEED)
    def get_handoff_api(handoff_id):
        """Get a specific handoff note."""
        try:
            handoff_manager = current_app.config.get('HANDOFF_MANAGER')
            if not handoff_manager:
                return jsonify({'error': 'Handoff manager unavailable'}), 404
            handoff = handoff_manager.get_handoff(handoff_id)
            if not handoff:
                return jsonify({'error': 'Handoff not found'}), 404

            # Visibility guard (mirror list_handoffs rules)
            if handoff.channel_id:
                try:
                    db_manager = current_app.config.get('DB_MANAGER')
                    if not db_manager:
                        return jsonify({'error': 'Access denied'}), 403
                    with db_manager.get_connection() as conn:
                        member = conn.execute(
                            "SELECT 1 FROM channel_members WHERE channel_id = ? AND user_id = ?",
                            (handoff.channel_id, g.api_key_info.user_id)
                        ).fetchone()
                    if not member:
                        return jsonify({'error': 'Access denied'}), 403
                except Exception:
                    return jsonify({'error': 'Access denied'}), 403

            if handoff.visibility == 'private' and handoff.author_id != g.api_key_info.user_id:
                return jsonify({'error': 'Access denied'}), 403
            if handoff.visibility == 'custom':
                allowed = False
                if handoff.permissions and g.api_key_info.user_id in handoff.permissions:
                    allowed = True
                if handoff.author_id == g.api_key_info.user_id:
                    allowed = True
                if not allowed:
                    return jsonify({'error': 'Access denied'}), 403

            return jsonify({'handoff': handoff.to_dict()})
        except Exception as e:
            logger.error(f"Failed to get handoff: {e}")
            return jsonify({'error': 'Internal server error'}), 500

    @api.route('/agents/me/catchup', methods=['GET'])
    @require_auth(Permission.READ_FEED)
    def agent_catchup_api():
        """Return a catch-up digest for agents (feed, channels, mentions, inbox, tasks, circles, handoffs)."""
        try:
            db_manager, _, _, message_manager, channel_manager, _, feed_manager, _, _, _, p2p_manager = _get_app_components_any(current_app)
            mention_manager = current_app.config.get('MENTION_MANAGER')
            inbox_manager = current_app.config.get('INBOX_MANAGER')
            task_manager = current_app.config.get('TASK_MANAGER')
            circle_manager = current_app.config.get('CIRCLE_MANAGER')
            handoff_manager = current_app.config.get('HANDOFF_MANAGER')
            profile_manager = current_app.config.get('PROFILE_MANAGER')
            user_id = g.api_key_info.user_id
            _touch_agent_presence(user_id, 'catchup')

            heartbeat_snapshot = build_agent_heartbeat_snapshot(
                db_manager=db_manager,
                user_id=user_id,
                mention_manager=mention_manager,
                inbox_manager=inbox_manager,
                workspace_event_manager=current_app.config.get('WORKSPACE_EVENT_MANAGER'),
            )
            actionable_work = build_actionable_work_preview(
                db_manager=db_manager,
                user_id=user_id,
                limit=10,
            )

            limit_raw = request.args.get('limit', 25)
            try:
                limit = int(limit_raw)
            except Exception:
                limit = 25
            limit = max(1, min(limit, 200))

            window_hours_raw = request.args.get('window_hours', 24)
            try:
                window_hours = int(window_hours_raw)
            except Exception:
                window_hours = 24
            since_str = request.args.get('since')
            since_dt = _parse_since_window(since_str, window_hours=window_hours)
            since_iso = since_dt.isoformat()
            generated_at = datetime.now(timezone.utc).isoformat()

            channels_activity = []
            if channel_manager:
                try:
                    channels_activity = channel_manager.get_channel_activity_since(
                        user_id=user_id,
                        since=since_dt,
                        limit=limit,
                    )
                except Exception as ch_err:
                    logger.warning(f"Channel catchup failed: {ch_err}")
                    channels_activity = []

            feed_items = []
            if feed_manager:
                try:
                    posts = feed_manager.get_posts_since(
                        user_id=user_id,
                        since=since_dt,
                        limit=limit,
                    )
                    for post in posts:
                        feed_items.append({
                            'post_id': post.id,
                            'author_id': post.author_id,
                            'created_at': post.created_at.isoformat() if post.created_at else None,
                            'visibility': post.visibility.value if hasattr(post.visibility, 'value') else str(post.visibility),
                            'expires_at': post.expires_at.isoformat() if getattr(post, 'expires_at', None) else None,
                            'preview': build_preview(post.content or ''),
                        })
                except Exception as feed_err:
                    logger.warning(f"Feed catchup failed: {feed_err}")

            dm_items = []
            if message_manager and g.api_key_info.has_permission(Permission.READ_MESSAGES):
                try:
                    messages = message_manager.get_messages(
                        user_id,
                        limit=limit,
                        since=since_dt,
                    )
                    for msg in messages:
                        meta = msg.metadata if isinstance(msg.metadata, dict) else {}
                        dm_items.append({
                            'message_id': msg.id,
                            'sender_id': msg.sender_id,
                            'recipient_id': msg.recipient_id,
                            'created_at': msg.created_at.isoformat() if msg.created_at else None,
                            'preview': build_preview(msg.content or ''),
                            'message_type': msg.message_type.value if hasattr(msg.message_type, 'value') else str(msg.message_type),
                            'edited_at': msg.edited_at.isoformat() if getattr(msg, 'edited_at', None) else None,
                            'reply_to': meta.get('reply_to'),
                            'group_id': meta.get('group_id'),
                            'group_members': meta.get('group_members') or [],
                            'attachments_count': len(meta.get('attachments') or []),
                        })
                except Exception as msg_err:
                    logger.warning(f"Message catchup failed: {msg_err}")

            mention_items = []
            if mention_manager:
                try:
                    mention_items = mention_manager.get_mentions(
                        user_id=user_id,
                        since=since_iso,
                        limit=limit,
                        include_acknowledged=False,
                    )
                except Exception as men_err:
                    logger.warning(f"Mention catchup failed: {men_err}")

            inbox_items = []
            if inbox_manager:
                try:
                    inbox_items = inbox_manager.list_items(
                        user_id=user_id,
                        status='pending',
                        limit=limit,
                        since=since_iso,
                        include_handled=False,
                    )
                except Exception as inbox_err:
                    logger.warning(f"Inbox catchup failed: {inbox_err}")

            task_items = []
            if task_manager:
                try:
                    task_items = task_manager.get_tasks_since(since_iso, limit=limit)
                except Exception as task_err:
                    logger.warning(f"Task catchup failed: {task_err}")

            circle_items = []
            if circle_manager:
                try:
                    circles = circle_manager.list_circles_since(since_iso, limit=limit)
                    circle_items = [c.to_dict() for c in circles]
                except Exception as circle_err:
                    logger.warning(f"Circle catchup failed: {circle_err}")

            handoff_items = []
            if handoff_manager:
                try:
                    handoffs = handoff_manager.list_handoffs_since(
                        since=since_dt,
                        limit=limit,
                        viewer_id=user_id,
                    )
                    handoff_items = [h.to_dict() for h in handoffs]
                except Exception as handoff_err:
                    logger.warning(f"Handoff catchup failed: {handoff_err}")

            agent_directives = None
            agent_directives_source = 'none'
            if profile_manager:
                try:
                    profile = profile_manager.get_profile(user_id)
                    if profile and getattr(profile, 'agent_directives', None):
                        agent_directives = normalize_agent_directives(profile.agent_directives)
                        if agent_directives:
                            agent_directives_source = 'custom'
                except Exception as profile_err:
                    logger.warning(f"Agent directives catchup failed: {profile_err}")
            if not agent_directives:
                try:
                    user_row = db_manager.get_user(user_id) if db_manager else None
                    if user_row:
                        agent_directives = get_default_agent_directives(
                            username=user_row.get('username'),
                            account_type=user_row.get('account_type'),
                        )
                        if agent_directives:
                            agent_directives_source = 'default'
                except Exception as dir_err:
                    logger.warning(f"Agent default directives catchup failed: {dir_err}")

            channel_total = 0
            for ch in channels_activity:
                try:
                    channel_total += int(ch.get('new_messages') or 0)
                except Exception:
                    continue

            # Session digest (agent-friendly summary)
            session_channels = []
            for ch in channels_activity:
                session_channels.append({
                    'channel_id': ch.get('channel_id'),
                    'channel_name': ch.get('channel_name'),
                    'new_message_count': ch.get('new_messages'),
                    'latest_message_preview': ch.get('latest_preview') or '',
                })

            session_mentions = mention_items or []

            session_inbox_items = []
            session_inbox_count = 0
            if inbox_manager:
                try:
                    stats = inbox_manager.get_stats(user_id, window_hours=window_hours)
                    session_inbox_count = int((stats.get('status_counts') or {}).get('pending', 0))
                except Exception:
                    session_inbox_count = 0
                try:
                    preview_items = inbox_manager.list_items(
                        user_id=user_id,
                        status='pending',
                        limit=5,
                        since=since_iso,
                        include_handled=False,
                    )
                    for item in preview_items:
                        session_inbox_items.append({
                            'id': item.get('id'),
                            'source_type': item.get('source_type'),
                            'source_id': item.get('source_id'),
                            'message_id': item.get('message_id'),
                            'channel_id': item.get('channel_id'),
                            'sender_user_id': item.get('sender_user_id'),
                            'trigger_type': item.get('trigger_type'),
                            'dm_thread_id': item.get('dm_thread_id'),
                            'reply_endpoint': item.get('reply_endpoint'),
                            'preview': item.get('preview'),
                            'created_at': item.get('created_at'),
                            'status': item.get('status'),
                            'payload': item.get('payload'),
                        })
                except Exception:
                    session_inbox_items = []
            if session_inbox_count == 0 and inbox_items:
                session_inbox_count = len(inbox_items)

            session_circles = []
            if circle_manager:
                try:
                    circles = circle_manager.list_circles_since(since_iso, limit=limit)
                    circle_ids = [c.id for c in circles]
                    entry_counts = circle_manager.get_entry_counts_since(since_iso, circle_ids) if circles else {}
                    for c in circles:
                        session_circles.append({
                            'circle_id': c.id,
                            'topic': c.topic,
                            'phase': c.phase,
                            'new_entries_count': int(entry_counts.get(c.id, 0)),
                        })
                except Exception:
                    session_circles = []

            session_tasks = []
            for task in task_items or []:
                session_tasks.append({
                    'task_id': task.get('id') or task.get('task_id'),
                    'title': task.get('title'),
                    'status': task.get('status'),
                    'assigned_to': task.get('assigned_to'),
                })

            session_peers = []
            if p2p_manager:
                try:
                    connected_peers = set(p2p_manager.get_connected_peers() or [])
                except Exception:
                    connected_peers = set()
                try:
                    local_peer = p2p_manager.get_peer_id()
                except Exception:
                    local_peer = None
                known_peers = set()
                try:
                    known_peers = set((p2p_manager.identity_manager.known_peers or {}).keys())
                except Exception:
                    known_peers = set()
                peer_ids = (known_peers | connected_peers)
                if local_peer and local_peer in peer_ids:
                    peer_ids.remove(local_peer)
                peer_profiles = {}
                try:
                    if channel_manager:
                        peer_profiles = channel_manager.get_all_peer_device_profiles()
                except Exception:
                    peer_profiles = {}
                for pid in sorted(peer_ids):
                    device_name = None
                    if peer_profiles and pid in peer_profiles:
                        device_name = peer_profiles[pid].get('display_name')
                    if not device_name:
                        try:
                            device_name = p2p_manager.identity_manager.peer_display_names.get(pid)
                        except Exception:
                            device_name = None
                    session_peers.append({
                        'peer_id': pid,
                        'device_name': device_name or pid,
                        'connected': pid in connected_peers,
                    })
                session_peers.sort(key=lambda p: (not p.get('connected', False), (p.get('device_name') or p.get('peer_id') or '').lower()))

            return jsonify({
                'since': since_dt.isoformat(),
                'generated_at': generated_at,
                'window_hours': window_hours,
                'channels': {
                    'count': len(channels_activity),
                    'messages_total': channel_total,
                    'items': channels_activity,
                },
                'feed': {'count': len(feed_items), 'items': feed_items},
                'messages': {'count': len(dm_items), 'items': dm_items},
                'mentions': {'count': len(mention_items), 'items': mention_items},
                'inbox': {'count': len(inbox_items), 'items': inbox_items},
                'tasks': {'count': len(task_items), 'items': task_items},
                'circles': {'count': len(circle_items), 'items': circle_items},
                'handoffs': {'count': len(handoff_items), 'items': handoff_items},
                'heartbeat': heartbeat_snapshot,
                'actionable_work': actionable_work,
                'session': {
                    'since': since_dt.isoformat(),
                    'generated_at': generated_at,
                    'agent_directives': agent_directives,
                    'agent_directives_source': agent_directives_source,
                    'channels': session_channels,
                    'mentions': session_mentions,
                    'inbox': {
                        'pending_count': session_inbox_count,
                        'items': session_inbox_items,
                    },
                    'circles': session_circles,
                    'tasks': session_tasks,
                    'peers': session_peers,
                    'heartbeat': heartbeat_snapshot,
                    'actionable_work': actionable_work,
                },
            })
        except Exception as e:
            logger.error(f"Agent catchup failed: {e}")
            return jsonify({'error': 'Internal server error'}), 500

    @api.route('/events', methods=['GET'])
    @require_auth(allow_session=True)
    def list_workspace_events():
        """List locally materialized workspace events visible to the caller."""
        try:
            workspace_event_manager = current_app.config.get('WORKSPACE_EVENT_MANAGER')
            if not workspace_event_manager:
                return jsonify({
                    'items': [],
                    'after_seq': 0,
                    'next_after_seq': 0,
                    'latest_seq': 0,
                    'has_more': False,
                    'supported_types': sorted(PATCH1_EVENT_TYPES),
                })

            key_info = getattr(g, 'api_key_info', None)
            user_id = getattr(key_info, 'user_id', None) or session.get('user_id')
            if not user_id:
                return jsonify({'error': 'Authentication required'}), 401

            after_seq_raw = request.args.get('after_seq', '0')
            try:
                after_seq_value = max(0, int(after_seq_raw or 0))
            except Exception:
                after_seq_value = 0
            limit_raw = request.args.get('limit', '50')
            types_raw = request.args.getlist('types')
            if len(types_raw) == 1 and ',' in str(types_raw[0]):
                types_raw = [part.strip() for part in str(types_raw[0]).split(',')]
            types = [str(item).strip() for item in types_raw if str(item).strip()]
            requested_types = [item for item in types if item in PATCH1_EVENT_TYPES]

            can_read_messages = True
            if key_info is not None:
                can_read_messages = bool(key_info.has_permission(Permission.READ_MESSAGES))

            result = workspace_event_manager.list_events_for_user(
                user_id=user_id,
                after_seq=after_seq_value,
                limit=limit_raw,
                types=requested_types or None,
                can_read_messages=can_read_messages,
            )
            return jsonify({
                'items': result.get('items', []),
                'after_seq': after_seq_value,
                'next_after_seq': int(result.get('next_after_seq') or 0),
                'latest_seq': int(workspace_event_manager.get_latest_seq() or 0),
                'has_more': bool(result.get('has_more', False)),
                'supported_types': sorted(PATCH1_EVENT_TYPES),
                'applied_types': requested_types,
            })
        except Exception as e:
            logger.error(f"Workspace events failed: {e}")
            return jsonify({'error': 'Internal server error'}), 500

    @api.route('/events/diagnostics', methods=['GET'])
    @require_auth(allow_session=True)
    def workspace_event_diagnostics():
        """Return operator diagnostics for the local workspace event journal."""
        try:
            workspace_event_manager = current_app.config.get('WORKSPACE_EVENT_MANAGER')
            if not workspace_event_manager:
                return jsonify({'error': 'Workspace events unavailable'}), 503

            key_info = getattr(g, 'api_key_info', None)
            caller_user_id = getattr(key_info, 'user_id', None) or session.get('user_id')
            db_manager = current_app.config.get('DB_MANAGER')
            owner_user_id = db_manager.get_instance_owner_user_id() if db_manager else None
            if not caller_user_id or not owner_user_id or caller_user_id != owner_user_id:
                return jsonify({'error': 'Only instance owner can view event diagnostics'}), 403

            try:
                limit = max(1, min(int(request.args.get('limit', 50)), 200))
            except Exception:
                limit = 50
            diagnostics = workspace_event_manager.get_diagnostics(limit=limit)
            oldest_created_at = diagnostics.get('oldest_created_at')
            oldest_age_seconds = None
            if oldest_created_at:
                try:
                    oldest_dt = datetime.fromisoformat(str(oldest_created_at).replace('Z', '+00:00'))
                    if oldest_dt.tzinfo is None:
                        oldest_dt = oldest_dt.replace(tzinfo=timezone.utc)
                    oldest_age_seconds = max(
                        0,
                        int((datetime.now(timezone.utc) - oldest_dt).total_seconds()),
                    )
                except Exception:
                    oldest_age_seconds = None
            diagnostics['oldest_age_seconds'] = oldest_age_seconds
            diagnostics['supported_types'] = sorted(PATCH1_EVENT_TYPES)
            return jsonify(diagnostics)
        except Exception as e:
            logger.error(f"Workspace event diagnostics failed: {e}")
            return jsonify({'error': 'Internal server error'}), 500

    @api.route('/agents/me/inbox/rebuild', methods=['POST'])
    @require_auth(Permission.READ_FEED)
    def agent_inbox_rebuild():
        """Rebuild inbox from channel message history.

        Scans recent channel messages for @mentions of the calling agent and
        creates any missing inbox items, bypassing rate limits.  This is a
        catch-up / recovery operation designed to surface mentions that were
        dropped due to P2P downtime, cooldown suppression, or bot restarts.

        Body (JSON, all optional):
          window_hours  int   How far back to scan (default 168 = 7 days)
          limit         int   Max messages to scan (default 2000, max 5000)

        Returns: { scanned, created, skipped, pending_after }
        """
        try:
            db_manager = get_app_components(current_app)[0]
            inbox_manager = current_app.config.get('INBOX_MANAGER')
            user_id = g.api_key_info.user_id

            if not inbox_manager:
                return jsonify({'error': 'Inbox not available'}), 503

            body = request.get_json(silent=True) or {}
            try:
                window_hours = int(body.get('window_hours', 168))
            except Exception:
                window_hours = 168
            window_hours = max(1, min(window_hours, 8760))  # 1h … 1yr
            try:
                limit = int(body.get('limit', 2000))
            except Exception:
                limit = 2000
            limit = max(1, min(limit, 5000))

            user_row = db_manager.get_user(user_id) if db_manager else None
            if not user_row:
                return jsonify({'error': 'User not found'}), 404

            username = user_row.get('username') or ''
            display_name = user_row.get('display_name') or ''

            result = inbox_manager.rebuild_from_channel_messages(
                user_id=user_id,
                username=username,
                display_name=display_name,
                window_hours=window_hours,
                limit=limit,
            )

            pending_after = inbox_manager.count_items(user_id=user_id, status='pending')
            result['pending_after'] = pending_after
            result['user_id'] = user_id
            result['window_hours'] = window_hours
            return jsonify(result)
        except Exception as e:
            logger.error(f"Agent inbox rebuild failed: {e}")
            return jsonify({'error': 'Internal server error'}), 500

    @api.route('/agents/me/heartbeat', methods=['GET'])
    @require_auth(Permission.READ_FEED)
    def agent_heartbeat():
        """Lightweight state snapshot for adaptive agent polling.

        Returns counts of pending items without full payloads, suitable for
        high-frequency polling to decide whether a full catchup is needed.
        """
        try:
            db_manager, _, _, _, _, _, _, _, _, _, _ = _get_app_components_any(current_app)
            mention_manager = current_app.config.get('MENTION_MANAGER')
            inbox_manager = current_app.config.get('INBOX_MANAGER')
            user_id = g.api_key_info.user_id
            _touch_agent_presence(user_id, 'heartbeat')
            snapshot = build_agent_heartbeat_snapshot(
                db_manager=db_manager,
                user_id=user_id,
                mention_manager=mention_manager,
                inbox_manager=inbox_manager,
                workspace_event_manager=current_app.config.get('WORKSPACE_EVENT_MANAGER'),
            )
            return jsonify(snapshot)
        except Exception as e:
            logger.error(f"Agent heartbeat failed: {e}")
            return jsonify({'error': 'Internal server error'}), 500

    @api.route('/feed/posts/<post_id>', methods=['PATCH', 'PUT'])
    @require_auth(Permission.WRITE_FEED)
    def update_feed_post(post_id):
        """Update a feed post (author only)."""
        try:
            db_manager, _, _, _, _, _, feed_manager, interaction_manager, profile_manager, _, p2p_manager = _get_app_components_any(current_app)
            from ..core.feed import PostType, PostVisibility
            from ..core.polls import parse_poll, poll_edit_lock_reason

            data = request.get_json() or {}
            post = feed_manager.get_post(post_id)
            if not post:
                return jsonify({'error': 'Post not found'}), 404
            if post.author_id != g.api_key_info.user_id:
                return jsonify({'error': 'Not authorized to edit this post'}), 403

            existing_poll = parse_poll(post.content or '')
            new_poll = parse_poll(data.get('content') or '') if data.get('content') is not None else None
            poll_spec = existing_poll or new_poll
            if poll_spec:
                votes_total = 0
                if interaction_manager:
                    results = interaction_manager.get_poll_results(post_id, 'feed', len(poll_spec.options))
                    votes_total = results.get('total', 0)
                lock_reason = poll_edit_lock_reason(post.created_at, votes_total, now=datetime.now(timezone.utc))
                if lock_reason:
                    return jsonify({'error': lock_reason}), 400

            content_raw = data.get('content')
            content = post.content if content_raw is None else str(content_raw).strip()
            post_type = data.get('post_type')
            visibility = data.get('visibility')
            permissions = data.get('permissions')
            metadata = data.get('metadata')

            if not content:
                return jsonify({'error': 'Post content required'}), 400

            post_type_enum = None
            visibility_enum = None
            if post_type:
                try:
                    post_type_enum = PostType(post_type)
                except ValueError:
                    return jsonify({'error': f'Invalid post type: {post_type}'}), 400

            if parse_poll(content):
                post_type_enum = PostType.POLL

            if visibility:
                try:
                    visibility_enum = PostVisibility(visibility)
                except ValueError:
                    return jsonify({'error': f'Invalid visibility: {visibility}'}), 400

            base_metadata = post.metadata or {}
            final_metadata = dict(base_metadata)
            if metadata is not None:
                try:
                    final_metadata.update(metadata)
                except Exception:
                    pass
            try:
                final_metadata['edited_at'] = datetime.now(timezone.utc).isoformat()
            except Exception:
                pass

            success = feed_manager.update_post(
                post_id,
                g.api_key_info.user_id,
                content,
                post_type=post_type_enum,
                visibility=visibility_enum,
                metadata=final_metadata,
                permissions=permissions,
            )

            if success:
                try:
                    sync_edited_mention_activity(
                        db_manager=db_manager,
                        mention_manager=current_app.config.get('MENTION_MANAGER'),
                        inbox_manager=current_app.config.get('INBOX_MANAGER'),
                        p2p_manager=p2p_manager,
                        content=content,
                        source_type='feed_post',
                        source_id=post_id,
                        author_id=g.api_key_info.user_id,
                        origin_peer=p2p_manager.get_peer_id() if p2p_manager else None,
                        channel_id=None,
                        visibility=visibility_enum.value if visibility_enum else post.visibility.value,
                        permissions=permissions if permissions is not None else post.permissions,
                        edited_at=final_metadata.get('edited_at') if isinstance(final_metadata, dict) else None,
                    )
                except Exception as mention_sync_err:
                    logger.warning(f"Feed mention refresh failed on edit: {mention_sync_err}")

                # Sync inline circles from edited content (create/update circles)
                try:
                    circle_manager = current_app.config.get('CIRCLE_MANAGER')
                    if circle_manager:
                        from ..core.circles import parse_circle_blocks, derive_circle_id
                        effective_visibility = visibility_enum.value if visibility_enum else post.visibility.value
                        circle_specs = parse_circle_blocks(content or '')
                        if circle_specs:
                            for idx, spec in enumerate(cast(Any, circle_specs)):
                                spec = cast(Any, spec)
                                circle_id = derive_circle_id('feed', post_id, idx, len(circle_specs), override=spec.circle_id)
                                facilitator_id = None
                                if spec.facilitator:
                                    facilitator_id = _resolve_handle_to_user_id(
                                        db_manager,
                                        spec.facilitator,
                                        visibility=effective_visibility,
                                        permissions=permissions if permissions is not None else post.permissions,
                                        author_id=g.api_key_info.user_id,
                                    )
                                if not facilitator_id:
                                    facilitator_id = g.api_key_info.user_id
                                if spec.participants is not None:
                                    resolved_participants = _resolve_handle_list(
                                        db_manager,
                                        spec.participants,
                                        visibility=effective_visibility,
                                        permissions=permissions if permissions is not None else post.permissions,
                                        author_id=g.api_key_info.user_id,
                                    )
                                    spec.participants = resolved_participants

                                circle_manager.upsert_circle(
                                    circle_id=circle_id,
                                    source_type='feed',
                                    source_id=post_id,
                                    created_by=g.api_key_info.user_id,
                                    spec=spec,
                                    facilitator_id=facilitator_id,
                                    visibility=effective_visibility,
                                    origin_peer=p2p_manager.get_peer_id() if p2p_manager else None,
                                    created_at=post.created_at.isoformat() if getattr(post, 'created_at', None) else None,
                                )
                except Exception as circle_err:
                    logger.warning(f"Inline circle sync failed on feed edit: {circle_err}")

                # Sync inline tasks from edited content (create/update tasks)
                try:
                    task_manager = current_app.config.get('TASK_MANAGER')
                    if task_manager:
                        effective_visibility = visibility_enum.value if visibility_enum else post.visibility.value
                        effective_permissions = permissions if permissions is not None else post.permissions
                        task_visibility = 'network' if effective_visibility in ('public', 'network') else 'local'
                        base_meta = {
                            'inline_task': True,
                            'source_type': 'feed_post',
                            'source_id': post_id,
                            'post_visibility': effective_visibility,
                        }
                        _sync_inline_tasks_from_content(
                            task_manager=task_manager,
                            db_manager=db_manager,
                            content=content,
                            scope='feed',
                            source_id=post_id,
                            actor_id=g.api_key_info.user_id,
                            task_visibility=task_visibility,
                            base_metadata=base_meta,
                            visibility=effective_visibility,
                            permissions=effective_permissions,
                            p2p_manager=p2p_manager,
                            profile_manager=profile_manager,
                        )
                except Exception as task_err:
                    logger.warning(f"Inline task sync failed on feed edit: {task_err}")

                # Sync inline objectives from edited content
                try:
                    objective_manager = current_app.config.get('OBJECTIVE_MANAGER')
                    if objective_manager:
                        effective_visibility = visibility_enum.value if visibility_enum else post.visibility.value
                        obj_visibility = 'network' if effective_visibility in ('public', 'network') else 'local'
                        effective_permissions = permissions if permissions is not None else post.permissions
                        _sync_inline_objectives_from_content(
                            objective_manager=objective_manager,
                            db_manager=db_manager,
                            content=content,
                            scope='feed',
                            source_id=post_id,
                            actor_id=g.api_key_info.user_id,
                            objective_visibility=obj_visibility,
                            source_type='feed_post',
                            origin_peer=p2p_manager.get_peer_id() if p2p_manager else None,
                            created_at=post.created_at.isoformat() if getattr(post, 'created_at', None) else None,
                            visibility=effective_visibility,
                            permissions=effective_permissions,
                        )
                except Exception as obj_err:
                    logger.warning(f"Inline objective sync failed on feed edit: {obj_err}")

                # Sync inline requests from edited content
                try:
                    request_manager = current_app.config.get('REQUEST_MANAGER')
                    if request_manager:
                        effective_visibility = visibility_enum.value if visibility_enum else post.visibility.value
                        effective_permissions = permissions if permissions is not None else post.permissions
                        _sync_inline_requests_from_content(
                            request_manager=request_manager,
                            db_manager=db_manager,
                            content=content,
                            scope='feed',
                            source_id=post_id,
                            actor_id=g.api_key_info.user_id,
                            visibility=effective_visibility,
                            source_type='feed_post',
                            origin_peer=p2p_manager.get_peer_id() if p2p_manager else None,
                            created_at=post.created_at.isoformat() if getattr(post, 'created_at', None) else None,
                            permissions=effective_permissions,
                            channel_id=None,
                        )
                except Exception as req_err:
                    logger.warning(f"Inline request sync failed on feed edit: {req_err}")

                # Sync inline signals from edited content
                try:
                    signal_manager = current_app.config.get('SIGNAL_MANAGER')
                    if signal_manager:
                        effective_visibility = visibility_enum.value if visibility_enum else post.visibility.value
                        sig_visibility = 'network' if effective_visibility in ('public', 'network') else 'local'
                        effective_permissions = permissions if permissions is not None else post.permissions
                        _sync_inline_signals_from_content(
                            signal_manager=signal_manager,
                            db_manager=db_manager,
                            content=content,
                            scope='feed',
                            source_id=post_id,
                            actor_id=g.api_key_info.user_id,
                            signal_visibility=sig_visibility,
                            source_type='feed_post',
                            origin_peer=p2p_manager.get_peer_id() if p2p_manager else None,
                            created_at=post.created_at.isoformat() if getattr(post, 'created_at', None) else None,
                            visibility=effective_visibility,
                            permissions=effective_permissions,
                        )
                except Exception as sig_err:
                    logger.warning(f"Inline signal sync failed on feed edit: {sig_err}")

                # Sync inline contracts from edited content
                try:
                    contract_manager = current_app.config.get('CONTRACT_MANAGER')
                    if contract_manager:
                        effective_visibility = visibility_enum.value if visibility_enum else post.visibility.value
                        effective_permissions = permissions if permissions is not None else post.permissions
                        contract_visibility = 'network' if effective_visibility in ('public', 'network') else 'local'
                        _sync_inline_contracts_from_content(
                            contract_manager=contract_manager,
                            db_manager=db_manager,
                            content=content,
                            scope='feed',
                            source_id=post_id,
                            actor_id=g.api_key_info.user_id,
                            contract_visibility=contract_visibility,
                            source_type='feed_post',
                            origin_peer=p2p_manager.get_peer_id() if p2p_manager else None,
                            created_at=post.created_at.isoformat() if getattr(post, 'created_at', None) else None,
                            visibility=effective_visibility,
                            permissions=effective_permissions,
                            channel_id=None,
                        )
                except Exception as contract_err:
                    logger.warning(f"Inline contract sync failed on feed edit: {contract_err}")

                # Sync inline handoffs from edited content
                try:
                    handoff_manager = current_app.config.get('HANDOFF_MANAGER')
                    if handoff_manager:
                        effective_visibility = visibility_enum.value if visibility_enum else post.visibility.value
                        effective_permissions = permissions if permissions is not None else post.permissions
                        _sync_inline_handoffs_from_content(
                            handoff_manager=handoff_manager,
                            content=content,
                            scope='feed',
                            source_id=post_id,
                            actor_id=g.api_key_info.user_id,
                            visibility=effective_visibility,
                            permissions=effective_permissions,
                            channel_id=None,
                            origin_peer=p2p_manager.get_peer_id() if p2p_manager else None,
                            created_at=post.created_at.isoformat() if getattr(post, 'created_at', None) else None,
                        )
                except Exception as handoff_err:
                    logger.warning(f"Inline handoff sync failed on feed edit: {handoff_err}")

                if p2p_manager and p2p_manager.is_running():
                    try:
                        updated = feed_manager.get_post(post_id)
                        sender_display = None
                        if profile_manager:
                            profile = profile_manager.get_profile(g.api_key_info.user_id)
                            if profile:
                                sender_display = profile.display_name or profile.username
                        if updated:
                            p2p_manager.broadcast_feed_post(
                                post_id=updated.id,
                                author_id=updated.author_id,
                                content=updated.content,
                                post_type=updated.post_type.value,
                                visibility=updated.visibility.value,
                                timestamp=updated.created_at.isoformat() if hasattr(updated.created_at, 'isoformat') else str(updated.created_at),
                                metadata=updated.metadata,
                                expires_at=updated.expires_at.isoformat() if getattr(updated, 'expires_at', None) else None,
                                display_name=sender_display,
                            )
                    except Exception as p2p_err:
                        logger.warning(f"Failed to broadcast post update via P2P: {p2p_err}")

                return jsonify({'success': True})
            return jsonify({'error': 'Failed to update post'}), 500
        except Exception as e:
            logger.error(f"Failed to update post: {e}", exc_info=True)
            return jsonify({'error': 'Internal server error'}), 500

    @api.route('/feed/posts/<post_id>', methods=['DELETE'])
    @require_auth(Permission.WRITE_FEED)
    def delete_feed_post(post_id):
        """Delete a feed post."""
        db_manager, _, _, _, _, _, feed_manager, _, _, _, p2p_manager = get_app_components(current_app)
        if not db_manager or not feed_manager:
            return jsonify({'error': 'Service unavailable'}), 503
        try:
            owner_id = db_manager.get_instance_owner_user_id()
            allow_admin = owner_id is not None and owner_id == g.api_key_info.user_id

            # Admin elevation must not cross device boundaries.  If the post
            # carries an origin_peer marker from a different device, restrict
            # deletion to the post author only — the local admin has no
            # authority over content that originated on another device.
            if allow_admin:
                post = feed_manager.get_post(post_id)
                if post:
                    post_meta = post.metadata or {}
                    post_origin_peer = post_meta.get('origin_peer')
                    if post_origin_peer:
                        local_peer_id = None
                        try:
                            if p2p_manager:
                                local_peer_id = p2p_manager.get_peer_id()
                        except Exception:
                            pass
                        if local_peer_id and post_origin_peer != local_peer_id:
                            allow_admin = False

            success = feed_manager.delete_post(post_id, g.api_key_info.user_id, allow_admin=allow_admin)
            if success:
                return jsonify({'message': 'Post deleted successfully'})
            return jsonify({'error': 'Post not found or not owned by user'}), 404

        except Exception as e:
            logger.error(f"Failed to delete post: {e}")
            return jsonify({'error': 'Failed to delete post'}), 500

    @api.route('/feed/posts/<post_id>/like', methods=['POST'])
    @require_auth(Permission.WRITE_FEED)
    def toggle_feed_post_like(post_id):
        """Toggle a like/reaction on a feed post.

        Request body (JSON, all fields optional):
          reaction_type: "like" | "love" | "laugh" | "dislike" | "angry"  (default: "like")

        Returns:
          liked (bool): true if the post is now liked, false if the reaction was removed.
          like_counts (dict): map of reaction_type -> count for this post.
        """
        try:
            _, _, _, _, _, _, feed_manager, interaction_manager, profile_manager, _, p2p_manager = _get_app_components_any(current_app)
            user_id = g.api_key_info.user_id
            data = request.get_json(silent=True) or {}
            from ..core.interactions import InteractionType
            raw = (data.get('reaction_type') or 'like').strip().lower()
            try:
                reaction_enum = InteractionType(raw)
            except ValueError:
                reaction_enum = InteractionType.LIKE
            liked = interaction_manager.toggle_post_like(post_id, user_id, reaction_enum)
            interactions = interaction_manager.get_post_interactions(post_id)
            if p2p_manager and p2p_manager.is_running():
                try:
                    display = None
                    if profile_manager:
                        prof = profile_manager.get_profile(user_id)
                        if prof:
                            display = prof.display_name or prof.username
                    p2p_manager.broadcast_interaction(
                        item_id=post_id, user_id=user_id,
                        action='like' if liked else 'unlike',
                        item_type='post', display_name=display,
                    )
                except Exception as _p2p_err:
                    logger.warning(f"P2P broadcast for post like failed: {_p2p_err}")
            return jsonify({
                'liked': liked,
                'reaction_type': reaction_enum.value,
                'like_counts': (interactions or {}).get('like_counts', {}),
            })
        except Exception as e:
            logger.error(f"toggle_feed_post_like failed: {e}")
            return jsonify({'error': 'Internal server error'}), 500

    @api.route('/channels/<channel_id>/messages/<message_id>/like', methods=['POST'])
    @require_auth(Permission.WRITE_MESSAGES)
    def toggle_channel_message_like(channel_id, message_id):
        """Toggle a like/reaction on a channel message.

        Request body (JSON, all fields optional):
          reaction_type: "like" | "love" | "laugh" | "dislike" | "angry"  (default: "like")

        Returns:
          liked (bool): true if the message is now liked, false if removed.
          like_counts (dict): map of reaction_type -> count for this message.
        """
        try:
            _, _, _, _, _, _, _, interaction_manager, profile_manager, _, p2p_manager = _get_app_components_any(current_app)
            user_id = g.api_key_info.user_id
            data = request.get_json(silent=True) or {}
            from ..core.interactions import InteractionType
            raw = (data.get('reaction_type') or 'like').strip().lower()
            try:
                reaction_enum = InteractionType(raw)
            except ValueError:
                reaction_enum = InteractionType.LIKE
            liked = interaction_manager.toggle_like(message_id, user_id, reaction_enum)
            interactions = interaction_manager.get_message_interactions(message_id)
            if p2p_manager and p2p_manager.is_running():
                try:
                    display = None
                    if profile_manager:
                        prof = profile_manager.get_profile(user_id)
                        if prof:
                            display = prof.display_name or prof.username
                    p2p_manager.broadcast_interaction(
                        item_id=message_id, user_id=user_id,
                        action='like' if liked else 'unlike',
                        item_type='message', display_name=display,
                    )
                except Exception as _p2p_err:
                    logger.warning(f"P2P broadcast for message like failed: {_p2p_err}")
            return jsonify({
                'liked': liked,
                'reaction_type': reaction_enum.value,
                'like_counts': (interactions or {}).get('like_counts', {}),
            })
        except Exception as e:
            logger.error(f"toggle_channel_message_like failed: {e}")
            return jsonify({'error': 'Internal server error'}), 500

    @api.route('/polls/<poll_id>', methods=['GET'])
    @require_auth(Permission.READ_FEED)
    def get_poll_api(poll_id):
        """Get a poll by id (feed post or channel message). Agents use this to read question, options, and results before voting."""
        try:
            db_manager, _, _, _, channel_manager, _, feed_manager, interaction_manager, _, _, _ = _get_app_components_any(current_app)
            from ..core.polls import parse_poll, resolve_poll_end, describe_poll_status

            item_type = (request.args.get('item_type') or '').strip().lower()
            if item_type not in {'feed', 'channel'}:
                return jsonify({'error': 'item_type required (feed or channel)'}), 400

            now_dt = datetime.now(timezone.utc)
            poll_spec = None
            poll_end = None
            channel_id = None

            if item_type == 'feed':
                post = feed_manager.get_post(poll_id) if feed_manager else None
                if not post:
                    return jsonify({'error': 'Poll post not found'}), 404
                if not post.can_view(g.api_key_info.user_id):
                    return jsonify({'error': 'Access denied'}), 403
                poll_spec = parse_poll(post.content or '')
                poll_end = resolve_poll_end(post.created_at, post.expires_at, poll_spec) if poll_spec else None
            else:
                with db_manager.get_connection() as conn:
                    row = conn.execute(
                        "SELECT id, channel_id, user_id, content, created_at, expires_at FROM channel_messages WHERE id = ?",
                        (poll_id,)
                    ).fetchone()
                    if not row:
                        return jsonify({'error': 'Poll message not found'}), 404
                    channel_id = row['channel_id']
                    member = conn.execute(
                        "SELECT 1 FROM channel_members WHERE channel_id = ? AND user_id = ?",
                        (channel_id, g.api_key_info.user_id)
                    ).fetchone()
                    if not member:
                        return jsonify({'error': 'Access denied'}), 403
                    poll_spec = parse_poll(row['content'] or '')
                    item_expires_at = None
                    try:
                        item_expires_at = datetime.fromisoformat(row['expires_at']) if row['expires_at'] else None
                    except Exception:
                        item_expires_at = None
                    created_at = None
                    try:
                        created_at = datetime.fromisoformat(row['created_at']) if row['created_at'] else None
                    except Exception:
                        created_at = None
                    poll_end = resolve_poll_end(created_at or now_dt, item_expires_at, poll_spec) if poll_spec else None

            if not poll_spec:
                return jsonify({'error': 'Poll definition not found'}), 400

            results = interaction_manager.get_poll_results(poll_id, item_type, len(poll_spec.options))
            user_vote = interaction_manager.get_user_poll_vote(poll_id, item_type, g.api_key_info.user_id)
            total_votes = results.get('total', 0)
            option_payload = []
            for idx, label in enumerate(poll_spec.options):
                count = results['counts'][idx] if idx < len(results['counts']) else 0
                percent = (count / total_votes * 100.0) if total_votes else 0.0
                option_payload.append({
                    'label': label,
                    'count': count,
                    'percent': round(percent, 1),
                    'index': idx
                })
            status_label = describe_poll_status(poll_end, now=now_dt)
            is_closed = bool(poll_end and poll_end <= now_dt)

            return jsonify({
                'poll_id': poll_id,
                'item_type': item_type,
                'channel_id': channel_id,
                'question': poll_spec.question,
                'options': option_payload,
                'ends_at': poll_end.isoformat() if poll_end else None,
                'status_label': status_label,
                'is_closed': is_closed,
                'total_votes': total_votes,
                'user_vote': user_vote,
            })
        except Exception as e:
            logger.error(f"Failed to get poll: {e}", exc_info=True)
            return jsonify({'error': 'Internal server error'}), 500

    @api.route('/polls/vote', methods=['POST'])
    @require_auth(Permission.WRITE_FEED)
    def vote_poll_api():
        """Vote in a poll (feed or channel)."""
        try:
            db_manager, _, _, _, channel_manager, _, feed_manager, interaction_manager, profile_manager, _, p2p_manager = _get_app_components_any(current_app)
            from ..core.polls import parse_poll, resolve_poll_end, describe_poll_status

            data = request.get_json() or {}
            poll_id = data.get('poll_id')
            item_type = (data.get('item_type') or '').strip().lower()
            option_index = data.get('option_index')

            if not poll_id or item_type not in {'feed', 'channel'}:
                return jsonify({'error': 'poll_id and item_type required'}), 400
            if option_index is None:
                return jsonify({'error': 'option_index required'}), 400

            now_dt = datetime.now(timezone.utc)
            poll_spec = None
            poll_end = None
            channel_id = None

            if item_type == 'feed':
                post = feed_manager.get_post(poll_id) if feed_manager else None
                if not post:
                    return jsonify({'error': 'Poll post not found'}), 404
                if post.visibility.value == 'private' and post.author_id != g.api_key_info.user_id:
                    return jsonify({'error': 'Access denied'}), 403
                if post.visibility.value == 'custom' and g.api_key_info.user_id not in (post.permissions or []):
                    return jsonify({'error': 'Access denied'}), 403
                poll_spec = parse_poll(post.content or '')
                poll_end = resolve_poll_end(post.created_at, post.expires_at, poll_spec) if poll_spec else None
            else:
                if not db_manager:
                    return jsonify({'error': 'Poll lookup failed'}), 500
                with db_manager.get_connection() as conn:
                    row = conn.execute(
                        "SELECT id, channel_id, user_id, content, created_at, expires_at FROM channel_messages WHERE id = ?",
                        (poll_id,)
                    ).fetchone()
                    if not row:
                        return jsonify({'error': 'Poll message not found'}), 404
                    channel_id = row['channel_id']
                    member = conn.execute(
                        "SELECT 1 FROM channel_members WHERE channel_id = ? AND user_id = ?",
                        (channel_id, g.api_key_info.user_id)
                    ).fetchone()
                    if not member:
                        return jsonify({'error': 'Access denied'}), 403
                    poll_spec = parse_poll(row['content'] or '')
                    item_expires_at = None
                    try:
                        item_expires_at = datetime.fromisoformat(row['expires_at']) if row['expires_at'] else None
                    except Exception:
                        item_expires_at = None
                    created_at = None
                    try:
                        created_at = datetime.fromisoformat(row['created_at']) if row['created_at'] else None
                    except Exception:
                        created_at = None
                    poll_end = resolve_poll_end(created_at or now_dt, item_expires_at, poll_spec) if poll_spec else None

            if not poll_spec:
                return jsonify({'error': 'Poll definition not found'}), 400
            if int(option_index) < 0 or int(option_index) >= len(poll_spec.options):
                return jsonify({'error': 'Invalid poll option'}), 400
            if poll_end and poll_end <= now_dt:
                return jsonify({'error': 'Poll is closed'}), 400

            interaction_manager.record_poll_vote(poll_id, item_type, g.api_key_info.user_id, int(option_index))
            results = interaction_manager.get_poll_results(poll_id, item_type, len(poll_spec.options))
            user_vote = interaction_manager.get_user_poll_vote(poll_id, item_type, g.api_key_info.user_id)
            total_votes = results.get('total', 0)
            option_payload = []
            for idx, label in enumerate(poll_spec.options):
                count = results['counts'][idx] if idx < len(results['counts']) else 0
                percent = (count / total_votes * 100.0) if total_votes else 0.0
                option_payload.append({
                    'label': label,
                    'count': count,
                    'percent': round(percent, 1),
                    'index': idx
                })
            status_label = describe_poll_status(poll_end, now=now_dt)

            if p2p_manager and p2p_manager.is_running():
                try:
                    sender_display = None
                    if profile_manager:
                        profile = profile_manager.get_profile(g.api_key_info.user_id)
                        if profile:
                            sender_display = profile.display_name or profile.username
                    p2p_manager.broadcast_interaction(
                        item_id=poll_id,
                        user_id=g.api_key_info.user_id,
                        action='poll_vote',
                        item_type='poll',
                        display_name=sender_display,
                        extra={
                            'poll_id': poll_id,
                            'poll_kind': item_type,
                            'option_index': int(option_index),
                            'channel_id': channel_id,
                        }
                    )
                except Exception as p2p_err:
                    logger.warning(f"Failed to broadcast poll vote: {p2p_err}")

            return jsonify({
                'success': True,
                'poll': {
                    'question': poll_spec.question,
                    'options': option_payload,
                    'ends_at': poll_end.isoformat() if poll_end else None,
                    'status_label': status_label,
                    'is_closed': False,
                    'user_vote': user_vote,
                    'total_votes': total_votes,
                }
            })
        except Exception as e:
            logger.error(f"Failed to vote in poll: {e}", exc_info=True)
            return jsonify({'error': 'Internal server error'}), 500

    @api.route('/feed/search', methods=['GET'])
    @require_auth(Permission.READ_FEED)
    def search_feed():
        """Search feed posts."""
        _, _, _, _, _, _, feed_manager, _, _, _, _ = _get_app_components_any(current_app)
        
        try:
            feed_manager.purge_expired_posts()
            query = request.args.get('q', '').strip()
            limit = int(request.args.get('limit', 20))

            if not query:
                return jsonify({'error': 'Search query required'}), 400

            posts = feed_manager.search_posts(
                query, g.api_key_info.user_id, limit=limit)
            
            return jsonify({
                'posts': [post.to_dict() for post in posts],
                'query': query,
                'count': len(posts),
            })

        except Exception as e:
            logger.error(f"Failed to search feed: {e}")
            return jsonify({'error': 'Failed to search feed'}), 500

    @api.route('/search', methods=['GET'])
    @require_auth(Permission.READ_FEED)
    def search_local():
        """Search local content across posts, channels, tasks, circles, and handoffs."""
        search_manager = current_app.config.get('SEARCH_MANAGER')
        if not search_manager or not getattr(search_manager, 'enabled', False):
            return jsonify({'error': 'Local search not available'}), 503

        try:
            query = request.args.get('q', '').strip()
            limit = int(request.args.get('limit', 50))
            types_raw = request.args.get('types', '').strip()

            if not query:
                return jsonify({'error': 'Search query required'}), 400

            types = None
            if types_raw:
                types = [t.strip() for t in types_raw.split(',') if t.strip()]

            results = search_manager.search(
                query=query,
                user_id=g.api_key_info.user_id,
                limit=limit,
                types=types,
            )

            return jsonify({
                'query': query,
                'count': len(results),
                'results': results,
            })

        except Exception as e:
            logger.error(f"Local search failed: {e}", exc_info=True)
            return jsonify({'error': 'Local search failed'}), 500
    
    # Skills registry endpoint
    @api.route('/skills', methods=['GET'])
    @require_auth(Permission.READ_FEED)
    def list_skills():
        """List registered skills with optional filters."""
        skill_manager = current_app.config.get('SKILL_MANAGER')
        if not skill_manager:
            return jsonify({'error': 'Skill manager not available'}), 503

        name = request.args.get('name')
        tag = request.args.get('tag')
        author_id = request.args.get('author_id')
        limit = min(int(request.args.get('limit', 100)), 500)

        skills = skill_manager.get_skills(name=name, tag=tag,
                                          author_id=author_id, limit=limit)
        # Attach trust scores when requested
        include_trust = request.args.get('include_trust', '').lower() in ('1', 'true', 'yes')
        if include_trust:
            for s in skills:
                trust_data = skill_manager.get_skill_trust_score(s['id'])
                s['trust_score'] = trust_data.get('trust_score')
                s['trust_components'] = trust_data.get('components')

        return jsonify({
            'skills': skills,
            'count': len(skills),
            'filters': {k: v for k, v in
                        {'name': name, 'tag': tag, 'author_id': author_id}.items()
                        if v},
        })

    @api.route('/skills/<skill_id>/invoke', methods=['POST'])
    @require_auth(Permission.WRITE_MESSAGES)
    def record_skill_invocation(skill_id):
        """Record a skill invocation for trust scoring."""
        skill_manager = current_app.config.get('SKILL_MANAGER')
        if not skill_manager:
            return jsonify({'error': 'Skill manager not available'}), 503

        data = request.get_json() or {}
        success = data.get('success', True)
        duration_ms = data.get('duration_ms')
        error_message = data.get('error_message')

        ok = skill_manager.record_invocation(
            skill_id=skill_id,
            invoker_user_id=g.api_key_info.user_id,
            success=bool(success),
            duration_ms=int(duration_ms) if duration_ms is not None else None,
            error_message=str(error_message)[:500] if error_message else None,
        )
        if ok:
            return jsonify({'success': True})
        return jsonify({'error': 'Failed to record invocation'}), 500

    @api.route('/skills/<skill_id>/trust', methods=['GET'])
    @require_auth(Permission.READ_FEED)
    def get_skill_trust(skill_id):
        """Get composite trust score for a skill."""
        skill_manager = current_app.config.get('SKILL_MANAGER')
        if not skill_manager:
            return jsonify({'error': 'Skill manager not available'}), 503

        trust_data = skill_manager.get_skill_trust_score(skill_id)
        stats = skill_manager.get_invocation_stats(skill_id)
        endorsements = skill_manager.get_endorsements(skill_id)
        return jsonify({
            'skill_id': skill_id,
            'trust': trust_data,
            'invocation_stats': stats,
            'endorsements': endorsements,
        })

    @api.route('/skills/<skill_id>/endorse', methods=['POST'])
    @require_auth(Permission.WRITE_MESSAGES)
    def endorse_skill(skill_id):
        """Endorse a skill (one per user)."""
        skill_manager = current_app.config.get('SKILL_MANAGER')
        if not skill_manager:
            return jsonify({'error': 'Skill manager not available'}), 503

        data = request.get_json() or {}
        weight = float(data.get('weight', 1.0))
        comment = data.get('comment')

        ok = skill_manager.endorse_skill(
            skill_id=skill_id,
            endorser_user_id=g.api_key_info.user_id,
            weight=weight,
            comment=str(comment)[:500] if comment else None,
        )
        if ok:
            return jsonify({'success': True})
        return jsonify({'error': 'Failed to endorse skill'}), 500

    # --- Community Notes (Agent Collaborative Verification) ---

    @api.route('/community-notes', methods=['GET'])
    @require_auth(Permission.READ_FEED)
    def list_community_notes():
        """List community notes with optional filters."""
        skill_manager = current_app.config.get('SKILL_MANAGER')
        if not skill_manager:
            return jsonify({'error': 'Not available'}), 503

        target_type = request.args.get('target_type')
        target_id = request.args.get('target_id')
        status = request.args.get('status')
        author_id = request.args.get('author_id')
        limit = min(int(request.args.get('limit', 50)), 200)

        notes = skill_manager.get_community_notes(
            target_type=target_type, target_id=target_id,
            status=status, author_id=author_id, limit=limit,
        )
        return jsonify({'notes': notes, 'count': len(notes)})

    @api.route('/community-notes', methods=['POST'])
    @require_auth(Permission.WRITE_MESSAGES)
    def create_community_note():
        """Create a community note on a message, post, signal, or other content."""
        skill_manager = current_app.config.get('SKILL_MANAGER')
        if not skill_manager:
            return jsonify({'error': 'Not available'}), 503

        data = request.get_json() or {}
        target_type = data.get('target_type')
        target_id = data.get('target_id')
        content = data.get('content', '').strip()
        note_type = data.get('note_type', 'context')

        if not target_type or not target_id:
            return jsonify({'error': 'target_type and target_id are required'}), 400
        if not content or len(content) < 10:
            return jsonify({'error': 'content must be at least 10 characters'}), 400
        if len(content) > 2000:
            return jsonify({'error': 'content exceeds 2000 character limit'}), 400

        note_id = skill_manager.create_community_note(
            target_type=target_type,
            target_id=target_id,
            author_id=g.api_key_info.user_id,
            content=content,
            note_type=note_type,
        )
        if note_id:
            return jsonify({'success': True, 'note_id': note_id}), 201
        return jsonify({'error': 'Failed to create note'}), 500

    @api.route('/community-notes/<note_id>/rate', methods=['POST'])
    @require_auth(Permission.WRITE_MESSAGES)
    def rate_community_note(note_id):
        """Rate a community note as helpful or not helpful."""
        skill_manager = current_app.config.get('SKILL_MANAGER')
        if not skill_manager:
            return jsonify({'error': 'Not available'}), 503

        data = request.get_json() or {}
        helpful = data.get('helpful', True)

        ok = skill_manager.rate_community_note(
            note_id=note_id,
            rater_user_id=g.api_key_info.user_id,
            helpful=bool(helpful),
        )
        if ok:
            return jsonify({'success': True})
        return jsonify({'error': 'Failed to rate note'}), 500

    # Additional Channel API endpoints
    @api.route('/channels/<channel_id>/messages', methods=['GET'])
    @require_auth(Permission.READ_FEED)
    def get_channel_messages(channel_id):
        """Get messages from a specific channel."""
        db_manager, _, _, _, channel_manager, file_manager, _, _, _, _, p2p_manager = _get_app_components_any(current_app)
        
        try:
            expired = channel_manager.purge_expired_channel_messages()
            if expired and file_manager:
                for msg in expired:
                    owner_id = msg.get('user_id')
                    msg_id = msg.get('id')
                    for file_id in msg.get('attachment_ids') or []:
                        try:
                            file_info = file_manager.get_file(file_id)
                            if not file_info or file_info.uploaded_by != owner_id:
                                continue
                            if file_manager.is_file_referenced(file_id, exclude_channel_message_id=msg_id):
                                continue
                            file_manager.delete_file(file_id, owner_id)
                        except Exception:
                            continue
            if expired and p2p_manager and p2p_manager.is_running():
                import secrets as _sec
                for msg in expired:
                    if msg.get('user_id') != g.api_key_info.user_id:
                        continue
                    try:
                        signal_id = f"DS{_sec.token_hex(8)}"
                        p2p_manager.broadcast_delete_signal(
                            signal_id=signal_id,
                            data_type='channel_message',
                            data_id=msg.get('id'),
                            reason='expired_ttl',
                        )
                    except Exception as p2p_err:
                        logger.warning(f"Failed to broadcast TTL delete for channel message {msg.get('id')}: {p2p_err}")
            limit = int(request.args.get('limit', 50))
            before_message_id = request.args.get('before')

            access = channel_manager.get_channel_access_decision(
                channel_id=channel_id,
                user_id=g.api_key_info.user_id,
                require_membership=True,
            )
            if not access.get('allowed'):
                if str(access.get('reason') or '').startswith('governance_'):
                    return jsonify({
                        'error': 'Channel access blocked by admin governance policy',
                        'reason': access.get('reason'),
                    }), 403
                return _channel_not_found_response()

            messages = channel_manager.get_channel_messages(
                channel_id, g.api_key_info.user_id, limit, before_message_id
            )
            # Enrich with author display_name/username so agents don't mis-resolve user_id -> handle
            db_manager, _, _, _, _, _, _, _, profile_manager, _, _ = _get_app_components_any(current_app)
            out = []
            for message in messages:
                d = message.to_dict()
                uid = d.get('user_id')
                dname = uid
                uname = uid
                if uid:
                    if profile_manager:
                        try:
                            prof = profile_manager.get_profile(uid)
                            if prof:
                                dname = (prof.display_name or prof.username or uid)
                                uname = (prof.username or uid)
                        except Exception:
                            pass
                    if (dname == uid or uname == uid) and db_manager:
                        try:
                            row = db_manager.get_user(uid)
                            if row:
                                dname = (row.get('display_name') or row.get('username') or uid)
                                uname = (row.get('username') or uid)
                        except Exception:
                            pass
                d['display_name'] = dname
                d['username'] = uname
                out.append(d)
            return jsonify({
                'messages': out,
                'channel_id': channel_id,
                'count': len(messages)
            })
            
        except Exception as e:
            logger.error(f"Failed to get channel messages: {e}")
            return jsonify({'error': 'Internal server error'}), 500

    @api.route('/channels/<channel_id>/messages/<message_id>', methods=['GET'])
    @require_auth(Permission.READ_FEED)
    def get_channel_message_api(channel_id, message_id):
        """Get a single channel message by id (for inbox source_id lookup, etc.)."""
        _, _, _, _, channel_manager, _, _, _, profile_manager, _, _ = _get_app_components_any(current_app)
        if not channel_manager:
            return jsonify({'error': 'Channels not available'}), 503
        try:
            message = channel_manager.get_channel_message(
                channel_id, message_id, g.api_key_info.user_id
            )
            if not message:
                return jsonify({'error': 'Message not found or you are not a member'}), 404
            d = message.to_dict()
            uid = d.get('user_id')
            if uid and profile_manager:
                try:
                    prof = profile_manager.get_profile(uid)
                    if prof:
                        d['display_name'] = prof.display_name or prof.username or uid
                        d['username'] = prof.username or uid
                except Exception:
                    pass
            if not d.get('display_name'):
                d['display_name'] = d.get('username') or uid
            if not d.get('username'):
                d['username'] = uid
            return jsonify({'message': d})
        except Exception as e:
            logger.error(f"Get channel message failed: {e}")
            return jsonify({'error': 'Internal server error'}), 500

    @api.route('/channels/<channel_id>/messages/<message_id>', methods=['DELETE'])
    @require_auth(Permission.READ_FEED)
    def delete_channel_message(channel_id, message_id):
        """Delete a channel message. Only the author can delete their own message."""
        db_manager, _, _, _, channel_manager, file_manager, _, _, _, _, p2p_manager = _get_app_components_any(current_app)
        try:
            attachment_ids = []
            if file_manager:
                try:
                    with db_manager.get_connection() as conn:
                        row = conn.execute(
                            "SELECT user_id, attachments FROM channel_messages WHERE id = ? AND channel_id = ?",
                            (message_id, channel_id),
                        ).fetchone()
                    if row and row['user_id'] == g.api_key_info.user_id and row['attachments']:
                        try:
                            parsed = json.loads(row['attachments'] or '[]')
                            if isinstance(parsed, list):
                                for att in parsed:
                                    fid = att.get('id') if isinstance(att, dict) else None
                                    if fid:
                                        attachment_ids.append(fid)
                        except Exception:
                            pass
                except Exception:
                    pass
            success = channel_manager.delete_message(
                channel_id=channel_id,
                message_id=message_id,
                user_id=g.api_key_info.user_id,
                allow_admin=False,
            )
            if not success:
                return jsonify({'error': 'Message not found or you can only delete your own messages'}), 403
            for fid in attachment_ids:
                try:
                    fi = file_manager.get_file(fid)
                    if fi and fi.uploaded_by == g.api_key_info.user_id:
                        if not file_manager.is_file_referenced(fid, exclude_channel_message_id=message_id):
                            file_manager.delete_file(fid, g.api_key_info.user_id)
                except Exception:
                    pass
            if p2p_manager and p2p_manager.is_running():
                try:
                    import secrets as _sec
                    signal_id = f"DS{_sec.token_hex(8)}"
                    p2p_manager.broadcast_delete_signal(
                        signal_id=signal_id,
                        data_type='channel_message',
                        data_id=message_id,
                        reason='user_deleted',
                    )
                except Exception as p2p_err:
                    logger.warning(f"Failed to broadcast channel message delete via P2P: {p2p_err}")
            return jsonify({'success': True})
        except Exception as e:
            logger.error(f"Failed to delete channel message: {e}")
            return jsonify({'error': 'Internal server error'}), 500

    @api.route('/channels/<channel_id>/messages/<message_id>', methods=['PATCH', 'PUT'])
    @require_auth(Permission.WRITE_FEED)
    def update_channel_message(channel_id, message_id):
        """Update a channel message. Only the author can edit their own message."""
        try:
            db_manager, _, _, _, channel_manager, file_manager, _, interaction_manager, profile_manager, _, p2p_manager = _get_app_components_any(current_app)
            from ..core.polls import parse_poll, poll_edit_lock_reason

            data = request.get_json() or {}
            content = data.get('content')
            attachments = data.get('attachments')  # optional list of attachment metadata

            with db_manager.get_connection() as conn:
                row = conn.execute(
                    "SELECT user_id, content, created_at, attachments, expires_at, ttl_seconds, ttl_mode, parent_message_id "
                    "FROM channel_messages WHERE id = ? AND channel_id = ?",
                    (message_id, channel_id)
                ).fetchone()
            if not row:
                return jsonify({'error': 'Message not found'}), 404
            if row['user_id'] != g.api_key_info.user_id:
                return jsonify({'error': 'You can only edit your own messages'}), 403

            existing_poll = parse_poll(row['content'] or '')
            new_poll = parse_poll(data.get('content') or '') if data.get('content') is not None else None
            poll_spec = existing_poll or new_poll
            if poll_spec:
                votes_total = 0
                if interaction_manager:
                    results = interaction_manager.get_poll_results(message_id, 'channel', len(poll_spec.options))
                    votes_total = results.get('total', 0)
                created_dt = channel_manager._parse_datetime(row['created_at'])
                lock_reason = poll_edit_lock_reason(created_dt, votes_total, now=datetime.now(timezone.utc))
                if lock_reason:
                    return jsonify({'error': lock_reason}), 400

            final_content = (row['content'] if content is None else str(content).strip())
            if not final_content and not attachments and not row['attachments']:
                return jsonify({'error': 'Message content required'}), 400

            if attachments is None:
                final_attachments = []
                if row['attachments']:
                    try:
                        final_attachments = json.loads(row['attachments'])
                    except Exception:
                        final_attachments = []
                final_attachments = _normalize_channel_attachments(final_attachments, file_manager)
            else:
                final_attachments = _normalize_channel_attachments(attachments, file_manager)

            success = channel_manager.update_message(
                message_id=message_id,
                user_id=g.api_key_info.user_id,
                content=final_content,
                attachments=final_attachments if final_attachments else None,
                allow_admin=False,
            )
            if not success:
                return jsonify({'error': 'Failed to update message'}), 500

            # Sync inline circles from edited channel message
            try:
                circle_manager = current_app.config.get('CIRCLE_MANAGER')
                if circle_manager:
                    from ..core.circles import parse_circle_blocks, derive_circle_id
                    circle_specs = parse_circle_blocks(final_content or '')
                    if circle_specs:
                        visibility = 'network'
                        try:
                            with db_manager.get_connection() as conn:
                                prow = conn.execute(
                                    "SELECT privacy_mode FROM channels WHERE id = ?",
                                    (channel_id,)
                                ).fetchone()
                            if prow and prow['privacy_mode'] and prow['privacy_mode'] != 'open':
                                visibility = 'local'
                        except Exception:
                            visibility = 'local'

                        for idx, spec in enumerate(cast(Any, circle_specs)):
                            spec = cast(Any, spec)
                            circle_id = derive_circle_id('channel', message_id, idx, len(circle_specs), override=spec.circle_id)
                            facilitator_id = None
                            if spec.facilitator:
                                facilitator_id = _resolve_handle_to_user_id(
                                    db_manager,
                                    spec.facilitator,
                                    channel_id=channel_id,
                                    author_id=g.api_key_info.user_id,
                                )
                            if not facilitator_id:
                                facilitator_id = g.api_key_info.user_id
                            if spec.participants is not None:
                                resolved_participants = _resolve_handle_list(
                                    db_manager,
                                    spec.participants,
                                    channel_id=channel_id,
                                    author_id=g.api_key_info.user_id,
                                )
                                spec.participants = resolved_participants

                            circle_manager.upsert_circle(
                                circle_id=circle_id,
                                source_type='channel',
                                source_id=message_id,
                                created_by=g.api_key_info.user_id,
                                spec=spec,
                                channel_id=channel_id,
                                facilitator_id=facilitator_id,
                                visibility=visibility,
                                origin_peer=p2p_manager.get_peer_id() if p2p_manager else None,
                                created_at=row['created_at'],
                            )
            except Exception as circle_err:
                logger.warning(f"Inline circle sync failed on channel edit: {circle_err}")

            # Sync inline tasks from edited channel message
            try:
                task_manager = current_app.config.get('TASK_MANAGER')
                if task_manager:
                    privacy_mode = None
                    try:
                        with db_manager.get_connection() as conn:
                            prow = conn.execute(
                                "SELECT privacy_mode FROM channels WHERE id = ?",
                                (channel_id,)
                            ).fetchone()
                        if prow:
                            privacy_mode = prow['privacy_mode']
                    except Exception:
                        privacy_mode = None
                    task_visibility = 'local' if privacy_mode and privacy_mode != 'open' else 'network'
                    base_meta = {
                        'inline_task': True,
                        'source_type': 'channel_message',
                        'source_id': message_id,
                        'channel_id': channel_id,
                    }
                    _sync_inline_tasks_from_content(
                        task_manager=task_manager,
                        db_manager=db_manager,
                        content=final_content,
                        scope='channel',
                        source_id=message_id,
                        actor_id=g.api_key_info.user_id,
                        task_visibility=task_visibility,
                        base_metadata=base_meta,
                        channel_id=channel_id,
                        p2p_manager=p2p_manager,
                        profile_manager=profile_manager,
                    )
            except Exception as task_err:
                logger.warning(f"Inline task sync failed on channel edit: {task_err}")

            # Sync inline objectives from edited channel message
            try:
                objective_manager = current_app.config.get('OBJECTIVE_MANAGER')
                if objective_manager:
                    privacy_mode = None
                    try:
                        with db_manager.get_connection() as conn:
                            prow = conn.execute(
                                "SELECT privacy_mode FROM channels WHERE id = ?",
                                (channel_id,)
                            ).fetchone()
                        if prow:
                            privacy_mode = prow['privacy_mode']
                    except Exception:
                        privacy_mode = None
                    obj_visibility = 'local' if privacy_mode and privacy_mode != 'open' else 'network'
                    _sync_inline_objectives_from_content(
                        objective_manager=objective_manager,
                        db_manager=db_manager,
                        content=final_content,
                        scope='channel',
                        source_id=message_id,
                        actor_id=g.api_key_info.user_id,
                        objective_visibility=obj_visibility,
                        source_type='channel_message',
                        origin_peer=p2p_manager.get_peer_id() if p2p_manager else None,
                        created_at=row['created_at'],
                        channel_id=channel_id,
                    )
            except Exception as obj_err:
                logger.warning(f"Inline objective sync failed on channel edit: {obj_err}")

            # Sync inline requests from edited channel message
            try:
                request_manager = current_app.config.get('REQUEST_MANAGER')
                if request_manager:
                    privacy_mode = None
                    try:
                        with db_manager.get_connection() as conn:
                            prow = conn.execute(
                                "SELECT privacy_mode FROM channels WHERE id = ?",
                                (channel_id,)
                            ).fetchone()
                        if prow:
                            privacy_mode = prow['privacy_mode']
                    except Exception:
                        privacy_mode = None
                    req_visibility = 'local' if privacy_mode and privacy_mode != 'open' else 'network'
                    _sync_inline_requests_from_content(
                        request_manager=request_manager,
                        db_manager=db_manager,
                        content=final_content,
                        scope='channel',
                        source_id=message_id,
                        actor_id=g.api_key_info.user_id,
                        visibility=req_visibility,
                        source_type='channel_message',
                        origin_peer=p2p_manager.get_peer_id() if p2p_manager else None,
                        created_at=row['created_at'],
                        permissions=None,
                        channel_id=channel_id,
                    )
            except Exception as req_err:
                logger.warning(f"Inline request sync failed on channel edit: {req_err}")

            # Sync inline signals from edited channel message
            try:
                signal_manager = current_app.config.get('SIGNAL_MANAGER')
                if signal_manager:
                    privacy_mode = None
                    try:
                        with db_manager.get_connection() as conn:
                            prow = conn.execute(
                                "SELECT privacy_mode FROM channels WHERE id = ?",
                                (channel_id,)
                            ).fetchone()
                        if prow:
                            privacy_mode = prow['privacy_mode']
                    except Exception:
                        privacy_mode = None
                    sig_visibility = 'local' if privacy_mode and privacy_mode != 'open' else 'network'
                    _sync_inline_signals_from_content(
                        signal_manager=signal_manager,
                        db_manager=db_manager,
                        content=final_content,
                        scope='channel',
                        source_id=message_id,
                        actor_id=g.api_key_info.user_id,
                        signal_visibility=sig_visibility,
                        source_type='channel_message',
                        origin_peer=p2p_manager.get_peer_id() if p2p_manager else None,
                        created_at=row['created_at'],
                        channel_id=channel_id,
                    )
            except Exception as sig_err:
                logger.warning(f"Inline signal sync failed on channel edit: {sig_err}")

            # Sync inline contracts from edited channel message
            try:
                contract_manager = current_app.config.get('CONTRACT_MANAGER')
                if contract_manager:
                    privacy_mode = None
                    try:
                        with db_manager.get_connection() as conn:
                            prow = conn.execute(
                                "SELECT privacy_mode FROM channels WHERE id = ?",
                                (channel_id,)
                            ).fetchone()
                        if prow:
                            privacy_mode = prow['privacy_mode']
                    except Exception:
                        privacy_mode = None
                    contract_visibility = 'local' if privacy_mode and privacy_mode != 'open' else 'network'
                    _sync_inline_contracts_from_content(
                        contract_manager=contract_manager,
                        db_manager=db_manager,
                        content=final_content,
                        scope='channel',
                        source_id=message_id,
                        actor_id=g.api_key_info.user_id,
                        contract_visibility=contract_visibility,
                        source_type='channel_message',
                        origin_peer=p2p_manager.get_peer_id() if p2p_manager else None,
                        created_at=row['created_at'],
                        channel_id=channel_id,
                    )
            except Exception as contract_err:
                logger.warning(f"Inline contract sync failed on channel edit: {contract_err}")

            # Sync inline handoffs from edited channel message
            try:
                handoff_manager = current_app.config.get('HANDOFF_MANAGER')
                if handoff_manager:
                    privacy_mode = None
                    try:
                        with db_manager.get_connection() as conn:
                            prow = conn.execute(
                                "SELECT privacy_mode FROM channels WHERE id = ?",
                                (channel_id,)
                            ).fetchone()
                        if prow:
                            privacy_mode = prow['privacy_mode']
                    except Exception:
                        privacy_mode = None
                    handoff_visibility = 'local' if privacy_mode and privacy_mode != 'open' else 'network'
                    _sync_inline_handoffs_from_content(
                        handoff_manager=handoff_manager,
                        content=final_content,
                        scope='channel',
                        source_id=message_id,
                        actor_id=g.api_key_info.user_id,
                        visibility=handoff_visibility,
                        permissions=None,
                        channel_id=channel_id,
                        origin_peer=p2p_manager.get_peer_id() if p2p_manager else None,
                        created_at=row['created_at'],
                    )
            except Exception as handoff_err:
                logger.warning(f"Inline handoff sync failed on channel edit: {handoff_err}")

            try:
                sync_edited_mention_activity(
                    db_manager=db_manager,
                    mention_manager=current_app.config.get('MENTION_MANAGER'),
                    inbox_manager=current_app.config.get('INBOX_MANAGER'),
                    p2p_manager=p2p_manager,
                    content=final_content,
                    source_type='channel_message',
                    source_id=message_id,
                    author_id=g.api_key_info.user_id,
                    origin_peer=p2p_manager.get_peer_id() if p2p_manager else None,
                    channel_id=channel_id,
                    edited_at=final_metadata.get('edited_at') if isinstance(final_metadata, dict) else None,
                )
                inbox_manager = current_app.config.get('INBOX_MANAGER')
                if inbox_manager:
                    inbox_manager.sync_source_triggers(
                        source_type='channel_message',
                        source_id=message_id,
                        trigger_type='reply',
                        sender_user_id=g.api_key_info.user_id,
                        origin_peer=p2p_manager.get_peer_id() if p2p_manager else None,
                        channel_id=channel_id,
                        preview=build_preview(final_content or '') or None,
                        payload={
                            'channel_id': channel_id,
                            'message_id': message_id,
                            'parent_message_id': row['parent_message_id'],
                            'edited_at': final_metadata.get('edited_at') if isinstance(final_metadata, dict) else None,
                        },
                        message_id=message_id,
                        source_content=final_content,
                    )
            except Exception as mention_sync_err:
                logger.warning(f"Channel mention/reply refresh failed on edit: {mention_sync_err}")

            if p2p_manager and p2p_manager.is_running():
                try:
                    sender_display = None
                    channel_mode = 'open'
                    target_peer_ids = None
                    try:
                        with db_manager.get_connection() as conn:
                            mode_row = conn.execute(
                                "SELECT privacy_mode FROM channels WHERE id = ?",
                                (channel_id,)
                            ).fetchone()
                        if mode_row:
                            channel_mode = (mode_row['privacy_mode'] or 'open').lower()
                        if channel_mode in {'private', 'confidential'}:
                            local_peer = p2p_manager.get_peer_id() if p2p_manager else None
                            target_peer_ids = channel_manager.get_member_peer_ids(channel_id, local_peer)
                    except Exception:
                        target_peer_ids = None
                    if profile_manager:
                        profile = profile_manager.get_profile(g.api_key_info.user_id)
                        if profile:
                            sender_display = profile.display_name or profile.username
                    p2p_manager.broadcast_channel_message(
                        channel_id=channel_id,
                        user_id=row['user_id'],
                        content=final_content,
                        message_id=message_id,
                        timestamp=str(row['created_at']),
                        attachments=final_attachments if final_attachments else None,
                        display_name=sender_display,
                        expires_at=row['expires_at'],
                        ttl_seconds=row['ttl_seconds'],
                        ttl_mode=row['ttl_mode'],
                        update_only=True,
                        parent_message_id=row['parent_message_id'],
                        edited_at=datetime.now(timezone.utc).isoformat(),
                        target_peer_ids=target_peer_ids,
                        security={'privacy_mode': channel_mode},
                    )
                except Exception as p2p_err:
                    logger.warning(f"Failed to broadcast channel message update via P2P: {p2p_err}")

            return jsonify({'success': True})
        except Exception as e:
            logger.error(f"Failed to update channel message: {e}", exc_info=True)
            return jsonify({'error': 'Internal server error'}), 500

    # ------------------------------------------------------------------ #
    #  File upload endpoint                                                #
    # ------------------------------------------------------------------ #

    @api.route('/files/upload', methods=['POST'])
    @require_auth(Permission.WRITE_FILES)
    def upload_file():
        """Upload a file via API key.

        Accepts either:
          - multipart/form-data with a 'file' field
          - JSON with {"filename": "...", "content_type": "...", "data": "<base64>"}

        Returns the file_id and metadata on success.
        """
        _, _, _, _, _, file_manager, _, _, _, _, _ = _get_app_components_any(current_app)

        try:
            generic_metadata_requested = False
            # --- Multipart upload ---
            if 'file' in request.files:
                f = request.files['file']
                if not f or not f.filename:
                    return jsonify({'error': 'Empty file'}), 400
                file_data = f.read()
                original_name = f.filename
                content_type = f.content_type or 'application/octet-stream'
                generic_metadata_requested = _is_generic_upload_metadata(original_name, content_type)
            else:
                # --- JSON / base64 upload ---
                data = request.get_json(silent=True)
                if not data or 'data' not in data:
                    return jsonify({
                        'error': 'Provide a file via multipart form or JSON '
                                 '{"filename": "...", "content_type": "...", "data": "<base64>"}'
                    }), 400
                import base64
                try:
                    file_data = base64.b64decode(data['data'], validate=True)
                except Exception:
                    return jsonify({'error': 'Invalid base64 in data field'}), 400
                original_name = data.get('filename', 'upload')
                content_type = data.get('content_type', 'application/octet-stream')
                generic_metadata_requested = _is_generic_upload_metadata(original_name, content_type)

            # Normalize generic upload metadata before validation so files
            # with missing/weak metadata can still be classified safely.
            try:
                original_name, content_type = file_manager.normalize_upload_metadata(
                    file_data=file_data,
                    original_name=original_name,
                    content_type=content_type,
                )
            except Exception:
                pass

            # Validate file upload (MIME, magic bytes, extension, size, dangerous content)
            from ..security.file_validation import validate_file_upload, detect_zip_bomb

            max_size = current_app.config.get('MAX_FILE_SIZE', 104857600)  # 100 MB default
            is_valid, error_msg, validated_type = validate_file_upload(
                file_data, content_type, original_name, max_size_override=max_size
            )

            if not is_valid:
                return jsonify({'error': error_msg}), 400

            # Check for zip bombs
            validated_content_type = validated_type or content_type or 'application/octet-stream'
            is_safe, bomb_msg = detect_zip_bomb(file_data, validated_content_type)
            if not is_safe:
                return jsonify({'error': bomb_msg}), 400

            # Use validated content type
            content_type = validated_content_type

            file_info = file_manager.save_file(
                file_data, original_name, content_type, g.api_key_info.user_id)

            if not file_info:
                return jsonify({'error': 'Failed to save file'}), 500

            response_payload = {
                'success': True,
                'file_id': file_info.id,
                'filename': file_info.original_name,
                'content_type': file_info.content_type,
                'size': file_info.size,
            }
            if generic_metadata_requested:
                response_payload['nudge'] = (
                    'Upload metadata was generic and has been normalized. '
                    'Include filename (with extension) and accurate content_type for best cross-platform rendering.'
                )
                response_payload['metadata_normalized'] = True

            return jsonify(response_payload), 201

        except Exception as e:
            logger.error(f"File upload failed: {e}", exc_info=True)
            return jsonify({'error': 'Internal server error'}), 500

    @api.route('/files/<file_id>', methods=['GET'])
    @require_auth(Permission.READ_FILES)
    def get_file_api(file_id):
        """Download a file by ID via API key."""
        db_manager, _, trust_manager, _, _, file_manager, feed_manager, _, _, _, _ = _get_app_components_any(current_app)

        try:
            result = file_manager.get_file_data(file_id)
            if not result:
                return jsonify({'error': 'File not found'}), 404

            file_data, file_info = result

            # Content-scoped access control: a file is readable only if the
            # caller can read at least one parent content item that references it.
            owner_id = db_manager.get_instance_owner_user_id()
            # Admin bypass must not cross device boundaries: if the file was
            # uploaded by a peer/shadow user (origin_peer set), the local admin
            # has no special authority over it — use content-scoped checks.
            is_local_admin = (
                bool(owner_id and owner_id == g.api_key_info.user_id)
                and not _uploader_is_peer(db_manager, file_info.uploaded_by)
            )
            access = evaluate_file_access(
                db_manager=db_manager,
                file_id=file_id,
                viewer_user_id=g.api_key_info.user_id,
                file_uploaded_by=file_info.uploaded_by,
                is_admin=is_local_admin,
                trust_manager=trust_manager,
                feed_manager=feed_manager,
            )
            if not access.allowed:
                return jsonify({
                    'error': 'Access denied',
                    'reason': access.reason,
                }), 403

            from flask import send_file
            import io
            return send_file(
                io.BytesIO(file_data),
                mimetype=file_info.content_type or 'application/octet-stream',
                as_attachment=True,
                download_name=file_info.original_name or file_id,
            )
        except Exception as e:
            logger.error(f"File download failed: {e}", exc_info=True)
            return jsonify({'error': 'Internal server error'}), 500

    @api.route('/files/<file_id>/preview', methods=['GET'])
    @require_auth(Permission.READ_FILES)
    def get_file_preview_api(file_id):
        """Return a bounded, read-only JSON preview for supported files."""
        db_manager, _, trust_manager, _, _, file_manager, feed_manager, _, _, _, _ = _get_app_components_any(current_app)

        try:
            result = file_manager.get_file_data(file_id)
            if not result:
                return jsonify({'error': 'File not found'}), 404

            file_data, file_info = result
            owner_id = db_manager.get_instance_owner_user_id()
            is_local_admin = (
                bool(owner_id and owner_id == g.api_key_info.user_id)
                and not _uploader_is_peer(db_manager, file_info.uploaded_by)
            )
            access = evaluate_file_access(
                db_manager=db_manager,
                file_id=file_id,
                viewer_user_id=g.api_key_info.user_id,
                file_uploaded_by=file_info.uploaded_by,
                is_admin=is_local_admin,
                trust_manager=trust_manager,
                feed_manager=feed_manager,
            )
            if not access.allowed:
                return jsonify({
                    'error': 'Access denied',
                    'reason': access.reason,
                }), 403

            preview = build_file_preview(
                file_data=file_data,
                filename=file_info.original_name,
                content_type=file_info.content_type,
            )
            return jsonify({
                'success': True,
                'file_id': file_id,
                'filename': file_info.original_name,
                'content_type': file_info.content_type,
                **preview,
            })
        except Exception as e:
            logger.error(f"File preview failed: {e}", exc_info=True)
            return jsonify({'error': 'Internal server error'}), 500

    @api.route('/files/<file_id>/access', methods=['GET'])
    @require_auth(Permission.READ_FILES)
    def get_file_access_api(file_id):
        """Inspect whether caller can access a file and why."""
        db_manager, _, trust_manager, _, _, file_manager, feed_manager, _, _, _, _ = _get_app_components_any(current_app)
        try:
            file_info = file_manager.get_file(file_id)
            if not file_info:
                return jsonify({'error': 'File not found'}), 404

            owner_id = db_manager.get_instance_owner_user_id()
            # Admin bypass must not cross device boundaries: if the file was
            # uploaded by a peer/shadow user (origin_peer set), the local admin
            # has no special authority over it — use content-scoped checks.
            is_local_admin = (
                bool(owner_id and owner_id == g.api_key_info.user_id)
                and not _uploader_is_peer(db_manager, file_info.uploaded_by)
            )
            access = evaluate_file_access(
                db_manager=db_manager,
                file_id=file_id,
                viewer_user_id=g.api_key_info.user_id,
                file_uploaded_by=file_info.uploaded_by,
                is_admin=is_local_admin,
                trust_manager=trust_manager,
                feed_manager=feed_manager,
            )
            return jsonify({
                'file_id': file_id,
                'filename': file_info.original_name,
                'access': access.to_dict(),
            })
        except Exception as e:
            logger.error(f"File access inspect failed: {e}", exc_info=True)
            return jsonify({'error': 'Internal server error'}), 500

    @api.route('/files/<file_id>', methods=['DELETE'])
    @require_auth(Permission.DELETE_DATA)
    def delete_file_api(file_id):
        """Delete a file by ID.

        Only the file owner or the local instance admin may delete a file.
        This check is enforced server-side; the ``is_admin`` flag is derived
        from the local instance-owner record and never from client input.
        """
        db_manager, _, _, _, _, file_manager, _, _, _, _, _ = get_app_components(current_app)
        if not db_manager or not file_manager:
            return jsonify({'error': 'Service unavailable'}), 503
        try:
            file_info = file_manager.get_file(file_id)
            if not file_info:
                return jsonify({'error': 'File not found'}), 404

            caller_id = g.api_key_info.user_id
            owner_id = db_manager.get_instance_owner_user_id()
            is_admin = bool(owner_id and owner_id == caller_id)

            # Only owner or local instance admin may delete.
            if file_info.uploaded_by != caller_id and not is_admin:
                return jsonify({'error': 'Access denied — you can only delete your own files'}), 403

            success = file_manager.delete_file(file_id, caller_id, is_admin=is_admin)
            if not success:
                return jsonify({'error': 'Failed to delete file'}), 500

            return jsonify({'success': True, 'file_id': file_id})
        except Exception as e:
            logger.error(f"File deletion failed: {e}", exc_info=True)
            return jsonify({'error': 'Internal server error'}), 500

    @api.route('/streams', methods=['GET'])
    @require_auth(Permission.READ_FEED)
    def list_streams_api():
        stream_manager = _get_stream_manager()
        if not stream_manager:
            return jsonify({'error': 'Streaming unavailable'}), 503
        try:
            channel_id = str(request.args.get('channel_id') or '').strip() or None
            status = str(request.args.get('status') or '').strip().lower() or None
            limit_raw = request.args.get('limit', 100)
            try:
                limit = int(limit_raw)
            except Exception:
                limit = 100
            streams = stream_manager.list_streams_for_user(
                g.api_key_info.user_id,
                channel_id=channel_id,
                status=status,
                limit=limit,
            )
            return jsonify({'streams': streams, 'count': len(streams)})
        except Exception as e:
            logger.error(f"List streams failed: {e}", exc_info=True)
            return jsonify({'error': 'Internal server error'}), 500

    @api.route('/streams', methods=['POST'])
    @require_auth(Permission.WRITE_FEED)
    def create_stream_api():
        db_manager, _, _, _, channel_manager, _, _, _, _, _, p2p_manager = _get_app_components_any(current_app)
        stream_manager = _get_stream_manager()
        if not stream_manager:
            return jsonify({'error': 'Streaming unavailable'}), 503
        try:
            data = request.get_json(silent=True) or {}
            channel_id = str(data.get('channel_id') or '').strip()
            title = str(data.get('title') or '').strip()
            description = str(data.get('description') or '').strip()
            stream_kind = str(data.get('stream_kind') or '').strip().lower() or None
            media_kind = str(data.get('media_kind') or 'audio').strip().lower()
            protocol_default = 'events-json' if stream_kind == 'telemetry' else 'hls'
            protocol = str(data.get('protocol') or protocol_default).strip().lower()
            relay_allowed = _as_bool(data.get('relay_allowed'))
            auto_post = True if data.get('auto_post') is None else _as_bool(data.get('auto_post'))
            start_now = _as_bool(data.get('start_now'))

            stream_row, error = stream_manager.create_stream(
                channel_id=channel_id,
                created_by=g.api_key_info.user_id,
                title=title,
                description=description,
                stream_kind=stream_kind,
                media_kind=media_kind,
                protocol=protocol,
                relay_allowed=relay_allowed,
                origin_peer=(p2p_manager.get_peer_id() if p2p_manager else None),
                metadata={
                    'created_via': 'api',
                    'created_at': datetime.now(timezone.utc).isoformat(),
                },
            )
            if error:
                if error in {'channel_not_found', 'not_channel_member'}:
                    return _channel_not_found_response()
                if error in {
                    'invalid_media_kind',
                    'invalid_stream_kind',
                    'invalid_protocol',
                    'invalid_protocol_for_stream_kind',
                    'title_required',
                    'title_too_long',
                    'description_too_long',
                }:
                    return jsonify({'error': error}), 400
                return jsonify({'error': error}), 403

            posted_message_id = None
            if auto_post and stream_row:
                from ..core.channels import MessageType as ChannelMessageType
                attachment = _build_stream_attachment(stream_row)
                post_content = str(data.get('post_content') or '').strip()
                if not post_content:
                    stream_kind_value = str(stream_row.get('stream_kind') or 'media').lower()
                    if stream_kind_value == 'telemetry':
                        label = "Telemetry stream"
                    else:
                        label = "Live video stream" if media_kind == "video" else "Live audio stream"
                    post_content = f"{label}: {title or stream_row.get('title')}"
                msg = channel_manager.send_message(
                    channel_id,
                    g.api_key_info.user_id,
                    post_content,
                    ChannelMessageType.FILE,
                    attachments=[attachment],
                    origin_peer=(p2p_manager.get_peer_id() if p2p_manager else None),
                )
                if msg:
                    posted_message_id = msg.id
                    # Broadcast stream announcement as standard channel message attachment
                    # to keep backward compatibility with existing peers.
                    try:
                        if p2p_manager:
                            mode_row = None
                            with db_manager.get_connection() as conn:
                                mode_row = conn.execute(
                                    "SELECT privacy_mode FROM channels WHERE id = ?",
                                    (channel_id,),
                                ).fetchone()
                            channel_mode = 'open'
                            if mode_row:
                                channel_mode = str(mode_row['privacy_mode'] or 'open').lower()
                            target_peers = None
                            if channel_mode in {'private', 'confidential'}:
                                target_peers = channel_manager.get_target_peer_ids_for_channel(channel_id)
                            p2p_manager.broadcast_channel_message(
                                channel_id=channel_id,
                                user_id=g.api_key_info.user_id,
                                content=post_content,
                                message_id=msg.id,
                                timestamp=msg.created_at.isoformat() if getattr(msg, 'created_at', None) else datetime.now(timezone.utc).isoformat(),
                                attachments=[attachment],
                                display_name=(db_manager.get_user(g.api_key_info.user_id) or {}).get('display_name'),
                                parent_message_id=None,
                                security={'privacy_mode': channel_mode},
                                target_peer_ids=target_peers,
                            )
                    except Exception as bcast_err:
                        logger.warning(f"Stream post broadcast failed (non-fatal): {bcast_err}")

            if start_now and stream_row:
                started, start_err = stream_manager.start_stream(stream_row['id'], g.api_key_info.user_id)
                if not start_err and started:
                    stream_row = started

            payload = {
                'success': True,
                'stream': stream_row,
            }
            if posted_message_id:
                payload['posted_message_id'] = posted_message_id
            return jsonify(payload), 201
        except Exception as e:
            logger.error(f"Create stream failed: {e}", exc_info=True)
            return jsonify({'error': 'Internal server error'}), 500

    @api.route('/streams/<stream_id>', methods=['GET'])
    @require_auth(Permission.READ_FEED)
    def get_stream_api(stream_id):
        stream_manager = _get_stream_manager()
        if not stream_manager:
            return jsonify({'error': 'Streaming unavailable'}), 503
        try:
            stream_row = stream_manager.get_stream_for_user(stream_id, g.api_key_info.user_id)
            if not stream_row:
                return jsonify({'error': 'Not found'}), 404
            return jsonify({'stream': stream_row})
        except Exception as e:
            logger.error(f"Get stream failed: {e}", exc_info=True)
            return jsonify({'error': 'Internal server error'}), 500

    @api.route('/streams/<stream_id>/start', methods=['POST'])
    @require_auth(Permission.WRITE_FEED)
    def start_stream_api(stream_id):
        stream_manager = _get_stream_manager()
        if not stream_manager:
            return jsonify({'error': 'Streaming unavailable'}), 503
        try:
            stream_row, error = stream_manager.start_stream(stream_id, g.api_key_info.user_id)
            if error in {'not_found', 'not_authorized'}:
                return jsonify({'error': 'Not found'}), 404
            if error:
                return jsonify({'error': error}), 400
            return jsonify({'success': True, 'stream': stream_row})
        except Exception as e:
            logger.error(f"Start stream failed: {e}", exc_info=True)
            return jsonify({'error': 'Internal server error'}), 500

    @api.route('/streams/<stream_id>/stop', methods=['POST'])
    @require_auth(Permission.WRITE_FEED)
    def stop_stream_api(stream_id):
        stream_manager = _get_stream_manager()
        if not stream_manager:
            return jsonify({'error': 'Streaming unavailable'}), 503
        try:
            stream_row, error = stream_manager.stop_stream(stream_id, g.api_key_info.user_id)
            if error in {'not_found', 'not_authorized'}:
                return jsonify({'error': 'Not found'}), 404
            if error:
                return jsonify({'error': error}), 400
            return jsonify({'success': True, 'stream': stream_row})
        except Exception as e:
            logger.error(f"Stop stream failed: {e}", exc_info=True)
            return jsonify({'error': 'Internal server error'}), 500

    @api.route('/streams/<stream_id>/tokens', methods=['POST'])
    @require_auth(Permission.WRITE_FEED)
    def issue_stream_token_api(stream_id):
        stream_manager = _get_stream_manager()
        if not stream_manager:
            return jsonify({'error': 'Streaming unavailable'}), 503
        try:
            data = request.get_json(silent=True) or {}
            scope = str(data.get('scope') or 'view').strip().lower()
            ttl_seconds = data.get('ttl_seconds')
            token_payload, error = stream_manager.issue_token(
                stream_id=stream_id,
                user_id=g.api_key_info.user_id,
                scope=scope,
                ttl_seconds=ttl_seconds,
                metadata={'issued_via': 'api'},
            )
            if error in {'not_found', 'not_authorized'}:
                return jsonify({'error': 'Not found'}), 404
            if error:
                return jsonify({'error': error}), 400
            if not token_payload:
                return jsonify({'error': 'token_issue_failed'}), 500

            if scope == 'view':
                token_q = quote_plus(str(token_payload.get('token') or ''))
                stream_row = stream_manager.get_stream(stream_id) or {}
                stream_kind = str(stream_row.get('stream_kind') or 'media').lower()
                protocol = str(stream_row.get('protocol') or 'hls').lower()
                if stream_kind == 'telemetry' or protocol == 'events-json':
                    token_payload['playback_url'] = f"/api/v1/streams/{stream_id}/events?token={token_q}"
                else:
                    token_payload['playback_url'] = f"/api/v1/streams/{stream_id}/manifest.m3u8?token={token_q}"
            return jsonify({'success': True, **token_payload})
        except Exception as e:
            logger.error(f"Issue stream token failed: {e}", exc_info=True)
            return jsonify({'error': 'Internal server error'}), 500

    @api.route('/streams/<stream_id>/join', methods=['POST'])
    @require_auth(Permission.READ_FEED)
    def join_stream_api(stream_id):
        stream_manager = _get_stream_manager()
        if not stream_manager:
            return jsonify({'error': 'Streaming unavailable'}), 503
        try:
            data = request.get_json(silent=True) or {}
            ttl_seconds = data.get('ttl_seconds')
            token_payload, error = stream_manager.issue_token(
                stream_id=stream_id,
                user_id=g.api_key_info.user_id,
                scope='view',
                ttl_seconds=ttl_seconds,
                metadata={'issued_via': 'join'},
            )
            if error in {'not_found', 'not_authorized'}:
                return jsonify({'error': 'Not found'}), 404
            if error:
                return jsonify({'error': error}), 400
            if not token_payload:
                return jsonify({'error': 'token_issue_failed'}), 500

            token_q = quote_plus(str(token_payload.get('token') or ''))
            stream_row = stream_manager.get_stream_for_user(stream_id, g.api_key_info.user_id)
            stream_kind = str((stream_row or {}).get('stream_kind') or 'media').lower()
            protocol = str((stream_row or {}).get('protocol') or 'hls').lower()
            if stream_kind == 'telemetry' or protocol == 'events-json':
                playback_url = f"/api/v1/streams/{stream_id}/events?token={token_q}"
            else:
                playback_url = f"/api/v1/streams/{stream_id}/manifest.m3u8?token={token_q}"
            return jsonify({
                'success': True,
                'stream': stream_row,
                'stream_kind': stream_kind,
                'token': token_payload.get('token'),
                'expires_at': token_payload.get('expires_at'),
                'playback_url': playback_url,
                'transport_url': playback_url,
            })
        except Exception as e:
            logger.error(f"Join stream failed: {e}", exc_info=True)
            return jsonify({'error': 'Internal server error'}), 500

    @api.route('/streams/<stream_id>/ingest/manifest', methods=['PUT'])
    def ingest_stream_manifest_api(stream_id):
        stream_manager = _get_stream_manager()
        if not stream_manager:
            return jsonify({'error': 'Streaming unavailable'}), 503
        try:
            token = str(request.args.get('token') or request.headers.get('X-Stream-Token') or '').strip()
            token_data, token_err = stream_manager.validate_token(
                stream_id=stream_id,
                token=token,
                scope='ingest',
            )
            if token_err or not token_data:
                return jsonify({'error': 'Not found'}), 404
            payload = request.get_data(cache=False, as_text=False)
            err = stream_manager.store_manifest(stream_id=stream_id, manifest_bytes=payload or b'')
            if err:
                if err in {'not_found', 'manifest_not_found'}:
                    return jsonify({'error': 'Not found'}), 404
                return jsonify({'error': err}), 400
            # Transition to live on first successful ingest update.
            try:
                stream_manager.start_stream(stream_id, str(token_data.get('user_id') or ''))
            except Exception:
                pass
            return jsonify({'success': True})
        except Exception as e:
            logger.error(f"Ingest manifest failed: {e}", exc_info=True)
            return jsonify({'error': 'Internal server error'}), 500

    @api.route('/streams/<stream_id>/ingest/segments/<segment_name>', methods=['PUT'])
    def ingest_stream_segment_api(stream_id, segment_name):
        stream_manager = _get_stream_manager()
        if not stream_manager:
            return jsonify({'error': 'Streaming unavailable'}), 503
        try:
            token = str(request.args.get('token') or request.headers.get('X-Stream-Token') or '').strip()
            token_data, token_err = stream_manager.validate_token(
                stream_id=stream_id,
                token=token,
                scope='ingest',
            )
            if token_err or not token_data:
                return jsonify({'error': 'Not found'}), 404
            payload = request.get_data(cache=False, as_text=False)
            err = stream_manager.store_segment(
                stream_id=stream_id,
                segment_name=segment_name,
                segment_bytes=payload or b'',
            )
            if err:
                if err == 'not_found':
                    return jsonify({'error': 'Not found'}), 404
                return jsonify({'error': err}), 400
            return jsonify({'success': True})
        except Exception as e:
            logger.error(f"Ingest segment failed: {e}", exc_info=True)
            return jsonify({'error': 'Internal server error'}), 500

    @api.route('/streams/<stream_id>/ingest/events', methods=['POST'])
    def ingest_stream_event_api(stream_id):
        stream_manager = _get_stream_manager()
        if not stream_manager:
            return jsonify({'error': 'Streaming unavailable'}), 503
        try:
            token = str(request.args.get('token') or request.headers.get('X-Stream-Token') or '').strip()
            token_data, token_err = stream_manager.validate_token(
                stream_id=stream_id,
                token=token,
                scope='ingest',
            )
            if token_err or not token_data:
                return jsonify({'error': 'Not found'}), 404

            content_type = str(request.headers.get('Content-Type') or 'application/json').split(';', 1)[0].strip().lower()
            body = request.get_json(silent=True)
            if isinstance(body, dict):
                event_payload = body.get('payload', body)
                event_ts = body.get('event_ts')
                event_metadata = body.get('metadata') if isinstance(body.get('metadata'), dict) else None
                if body.get('content_type'):
                    content_type = str(body.get('content_type')).strip().lower()
            else:
                event_payload = request.get_data(cache=False, as_text=True)
                event_ts = None
                event_metadata = None

            event_row, err = stream_manager.store_event(
                stream_id=stream_id,
                event_payload=event_payload,
                content_type=content_type or 'application/json',
                event_ts=event_ts,
                metadata=event_metadata,
            )
            if err in {'not_found'}:
                return jsonify({'error': 'Not found'}), 404
            if err:
                return jsonify({'error': err}), 400
            return jsonify({'success': True, 'event': event_row}), 201
        except Exception as e:
            logger.error(f"Ingest stream event failed: {e}", exc_info=True)
            return jsonify({'error': 'Internal server error'}), 500

    def _find_stream_remote_base(self_stream_id: str) -> Optional[str]:
        """Find a reachable remote base URL for a stream by trying all host_addrs."""
        from urllib.request import urlopen as _urlopen
        from urllib.error import URLError as _URLError
        import json as _json
        db_manager = _get_db_manager()
        if not db_manager:
            return None
        candidates: list[str] = []
        with db_manager.get_connection() as conn:
            rows = conn.execute(
                "SELECT attachments FROM channel_messages "
                "WHERE attachments IS NOT NULL AND attachments != '[]' "
                "ORDER BY created_at DESC LIMIT 300"
            ).fetchall()
        for row in rows:
            try:
                atts = _json.loads(row[0] if not hasattr(row, 'keys') else row['attachments'])
            except Exception:
                continue
            for att in atts:
                if not isinstance(att, dict):
                    continue
                if str(att.get('stream_id') or '') == self_stream_id:
                    candidates = [str(a).rstrip('/') for a in (att.get('host_addrs') or [])]
                    break
            if candidates:
                break
        for base in candidates:
            try:
                test_url = f"{base}/api/v1/streams/{self_stream_id}/manifest.m3u8"
                with _urlopen(test_url, timeout=4) as resp:
                    resp.read(1)
                return base
            except Exception:
                continue
        return candidates[0] if candidates else None

    @api.route('/stream-proxy/<stream_id>/manifest.m3u8', methods=['GET'])
    def stream_proxy_manifest_api(stream_id):
        """Server-side proxy for remote peer streams — fetches manifest from origin peer and rewrites segment URLs."""
        from urllib.request import urlopen as _urlopen
        from urllib.error import URLError as _URLError
        try:
            remote_base = _find_stream_remote_base(stream_id)
            if not remote_base:
                return jsonify({'error': 'Remote peer not found'}), 404
            remote_url = f"{remote_base}/api/v1/streams/{stream_id}/manifest.m3u8"
            try:
                with _urlopen(remote_url, timeout=8) as resp:
                    raw = resp.read().decode('utf-8')
            except _URLError as e:
                logger.warning(f"stream-proxy: failed to fetch {remote_url}: {e}")
                return jsonify({'error': 'Remote stream unreachable'}), 502
            # Rewrite segment URLs to go through the local proxy too
            out_lines = []
            for line in raw.splitlines():
                stripped = line.strip()
                if not stripped or stripped.startswith('#'):
                    # Rewrite EXT-X-MAP URI to local proxy
                    if stripped.startswith('#EXT-X-MAP:URI="'):
                        import re as _re
                        def _rewrite_map(m: Any) -> str:
                            seg = m.group(2).split('/')[-1].split('?')[0]
                            return f'{m.group(1)}/api/v1/stream-proxy/{stream_id}/segments/{seg}{m.group(3)}'
                        line = _re.sub(r'(#EXT-X-MAP:URI=")([^"]+)(".*)', _rewrite_map, stripped)
                    out_lines.append(line)
                elif stripped.startswith('/api/v1/streams/'):
                    # Already absolute path — rewrite to proxy path
                    seg_name = stripped.split('/segments/')[-1].split('?')[0]
                    out_lines.append(f'/api/v1/stream-proxy/{stream_id}/segments/{seg_name}')
                elif stripped.startswith('http'):
                    seg_name = stripped.split('/segments/')[-1].split('?')[0]
                    out_lines.append(f'/api/v1/stream-proxy/{stream_id}/segments/{seg_name}')
                else:
                    # Bare segment name
                    seg_name = stripped.split('?')[0]
                    out_lines.append(f'/api/v1/stream-proxy/{stream_id}/segments/{seg_name}')
            return Response(
                '\n'.join(out_lines),
                mimetype='application/vnd.apple.mpegurl',
                headers={'Cache-Control': 'no-store', 'Access-Control-Allow-Origin': '*'},
            )
        except Exception as e:
            logger.error(f"stream-proxy manifest failed: {e}", exc_info=True)
            return jsonify({'error': 'Internal server error'}), 500

    @api.route('/stream-proxy/<stream_id>/segments/<segment_name>', methods=['GET'])
    def stream_proxy_segment_api(stream_id, segment_name):
        """Server-side proxy for remote peer stream segments."""
        from urllib.request import urlopen as _urlopen
        from urllib.error import URLError as _URLError
        try:
            remote_base = _find_stream_remote_base(stream_id)
            if not remote_base:
                return jsonify({'error': 'Not found'}), 404
            remote_url = f"{remote_base}/api/v1/streams/{stream_id}/segments/{segment_name}"
            try:
                with _urlopen(remote_url, timeout=8) as resp:
                    data = resp.read()
                    content_type = resp.headers.get('Content-Type', 'application/octet-stream')
            except _URLError as e:
                logger.warning(f"stream-proxy segment: failed to fetch {remote_url}: {e}")
                return jsonify({'error': 'Not found'}), 404
            return Response(
                data,
                mimetype=content_type,
                headers={'Cache-Control': 'no-store', 'Access-Control-Allow-Origin': '*'},
            )
        except Exception as e:
            logger.error(f"stream-proxy segment failed: {e}", exc_info=True)
            return jsonify({'error': 'Internal server error'}), 500

    @api.route('/streams/<stream_id>/manifest.m3u8', methods=['GET', 'OPTIONS'])
    def stream_manifest_api(stream_id):
        if request.method == 'OPTIONS':
            return Response('', status=204, headers={
                'Access-Control-Allow-Origin': '*',
                'Access-Control-Allow-Methods': 'GET, OPTIONS',
                'Access-Control-Allow-Headers': 'Content-Type',
            })
        stream_manager = _get_stream_manager()
        if not stream_manager:
            return jsonify({'error': 'Streaming unavailable'}), 503
        try:
            token = str(request.args.get('token') or '').strip()
            token_data, token_err = stream_manager.validate_token(
                stream_id=stream_id,
                token=token,
                scope='view',
            )
            if token_err or not token_data:
                # Only a missing token may fall back to open-stream playback.
                if token:
                    return jsonify({'error': 'Not found'}), 404
                stream_row = stream_manager.get_stream(stream_id)
                if not stream_row or str(stream_row.get('visibility_mode') or 'open').lower() != 'open':
                    return jsonify({'error': 'Not found'}), 404
                token = ''  # serve without token; render_manifest_for_token handles empty token
            rendered, err = stream_manager.render_manifest_for_token(
                stream_id=stream_id,
                token=token,
                api_base_path='/api/v1/streams',
            )
            if err or rendered is None:
                return jsonify({'error': 'Not found'}), 404
            return Response(
                rendered,
                mimetype='application/vnd.apple.mpegurl',
                headers={
                    'Cache-Control': 'no-store',
                    'Content-Disposition': 'inline; filename="master.m3u8"',
                    'Access-Control-Allow-Origin': '*',
                },
            )
        except Exception as e:
            logger.error(f"Serve stream manifest failed: {e}", exc_info=True)
            return jsonify({'error': 'Internal server error'}), 500

    @api.route('/streams/<stream_id>/segments/<segment_name>', methods=['GET', 'OPTIONS'])
    def stream_segment_api(stream_id, segment_name):
        if request.method == 'OPTIONS':
            return Response('', status=204, headers={
                'Access-Control-Allow-Origin': '*',
                'Access-Control-Allow-Methods': 'GET, OPTIONS',
                'Access-Control-Allow-Headers': 'Content-Type',
            })
        stream_manager = _get_stream_manager()
        if not stream_manager:
            return jsonify({'error': 'Streaming unavailable'}), 503
        try:
            token = str(request.args.get('token') or '').strip()
            token_data, token_err = stream_manager.validate_token(
                stream_id=stream_id,
                token=token,
                scope='view',
            )
            if token_err or not token_data:
                # Only a missing token may fall back to open-stream playback.
                if token:
                    return jsonify({'error': 'Not found'}), 404
                stream_row = stream_manager.get_stream(stream_id)
                if not stream_row or str(stream_row.get('visibility_mode') or 'open').lower() != 'open':
                    return jsonify({'error': 'Not found'}), 404
            data, mimetype, err = stream_manager.get_segment_data(
                stream_id=stream_id,
                segment_name=segment_name,
            )
            if err or data is None:
                return jsonify({'error': 'Not found'}), 404
            return Response(
                data,
                mimetype=mimetype or 'application/octet-stream',
                headers={
                    'Cache-Control': 'no-store',
                    'Access-Control-Allow-Origin': '*',
                },
            )
        except Exception as e:
            logger.error(f"Serve stream segment failed: {e}", exc_info=True)
            return jsonify({'error': 'Internal server error'}), 500

    @api.route('/streams/<stream_id>/events', methods=['GET'])
    def stream_events_api(stream_id):
        stream_manager = _get_stream_manager()
        if not stream_manager:
            return jsonify({'error': 'Streaming unavailable'}), 503
        try:
            token = str(request.args.get('token') or '').strip()
            token_data, token_err = stream_manager.validate_token(
                stream_id=stream_id,
                token=token,
                scope='view',
            )
            if token_err or not token_data:
                return jsonify({'error': 'Not found'}), 404

            after_seq_raw = request.args.get('after_seq', 0)
            limit_raw = request.args.get('limit', 100)
            try:
                after_seq = int(after_seq_raw)
            except Exception:
                after_seq = 0
            try:
                limit = int(limit_raw)
            except Exception:
                limit = 100

            events, err = stream_manager.list_events(
                stream_id=stream_id,
                after_seq=after_seq,
                limit=limit,
            )
            if err in {'not_found'}:
                return jsonify({'error': 'Not found'}), 404
            if err:
                return jsonify({'error': err}), 400
            events_list = events or []
            last_seq = after_seq
            if events_list:
                last_seq = max(int(ev.get('seq') or 0) for ev in events_list)
            stream_row = stream_manager.get_stream(stream_id) or {}
            return jsonify({
                'success': True,
                'stream_id': stream_id,
                'stream_kind': stream_row.get('stream_kind') or 'telemetry',
                'events': events_list,
                'count': len(events_list),
                'last_seq': last_seq,
            })
        except Exception as e:
            logger.error(f"Serve stream events failed: {e}", exc_info=True)
            return jsonify({'error': 'Internal server error'}), 500

    @api.route('/channels/messages', methods=['POST'])
    @require_auth(Permission.WRITE_FEED)
    def send_channel_message():
        """Send a message to a channel and broadcast to P2P peers."""
        db_manager, _, _, _, channel_manager, file_manager, _, _, _, _, p2p_manager = _get_app_components_any(current_app)
        
        try:
            data = request.get_json()
            if not data:
                return jsonify({'error': 'JSON data required'}), 400
            
            content = data.get('content', '').strip()
            channel_id = data.get('channel_id')
            attachments = _normalize_channel_attachments(data.get('attachments', []), file_manager)
            parent_message_id = data.get('parent_message_id')
            security = data.get('security')
            ttl_mode = data.get('ttl_mode')
            ttl_seconds = data.get('ttl_seconds')
            expires_at = data.get('expires_at')
            
            if not content and not attachments:
                return jsonify({'error': 'Message content or attachments required'}), 400
            
            if not channel_id:
                return jsonify({'error': 'Channel ID required'}), 400

            # --- Input validation (cherry-picked from Copilot PR #9) ---
            _MAX_CONTENT_LEN = 50_000
            _MAX_ATTACHMENTS = 20
            _ID_PATTERN = re.compile(r'^[a-zA-Z0-9_\-]{1,100}$')

            if content and len(content) > _MAX_CONTENT_LEN:
                return jsonify({'error': f'Content exceeds maximum length ({_MAX_CONTENT_LEN} chars)'}), 400
            if attachments and len(attachments) > _MAX_ATTACHMENTS:
                return jsonify({'error': f'Too many attachments (max {_MAX_ATTACHMENTS})'}), 400
            if not _ID_PATTERN.match(str(channel_id)):
                return jsonify({'error': 'Invalid channel_id format'}), 400
            if parent_message_id and not _ID_PATTERN.match(str(parent_message_id)):
                return jsonify({'error': 'Invalid parent_message_id format'}), 400

            access = channel_manager.get_channel_access_decision(
                channel_id=channel_id,
                user_id=g.api_key_info.user_id,
                require_membership=True,
            )
            if not access.get('allowed'):
                if str(access.get('reason') or '').startswith('governance_'):
                    return jsonify({
                        'error': 'Channel access blocked by admin governance policy',
                        'reason': access.get('reason'),
                    }), 403
                return _channel_not_found_response()

            security_clean = None
            if security is not None:
                security_clean, sec_error = channel_manager.validate_security_metadata(security, strict=True)
                if sec_error:
                    return jsonify({'error': sec_error}), 400
            
            from ..core.channels import MessageType
            from ..core.tasks import parse_task_blocks, derive_task_id
            from ..core.circles import parse_circle_blocks, derive_circle_id
            message_type = MessageType.FILE if attachments else MessageType.TEXT
            
            message = channel_manager.send_message(
                channel_id, g.api_key_info.user_id, content, message_type,
                parent_message_id=parent_message_id,
                attachments=attachments,
                security=security_clean,
                expires_at=expires_at,
                ttl_seconds=ttl_seconds,
                ttl_mode=ttl_mode,
                origin_peer=p2p_manager.get_peer_id() if p2p_manager else None,
            )
            
            if message:
                # Inline circle creation from [circle] blocks
                try:
                    circle_manager = current_app.config.get('CIRCLE_MANAGER')
                    if circle_manager:
                        circle_specs = parse_circle_blocks(content or '')
                        if circle_specs:
                            circle_visibility = 'network'
                            try:
                                with db_manager.get_connection() as conn:
                                    row = conn.execute(
                                        "SELECT privacy_mode FROM channels WHERE id = ?",
                                        (channel_id,)
                                    ).fetchone()
                                    if row and row['privacy_mode'] and row['privacy_mode'] != 'open':
                                        circle_visibility = 'local'
                            except Exception as vis_err:
                                # Default to 'network' — if the message was sent
                                # successfully the channel exists and is open.
                                logger.debug(f"Circle visibility lookup failed: {vis_err}")
                                circle_visibility = 'network'

                            for idx, spec in enumerate(cast(Any, circle_specs)):
                                spec = cast(Any, spec)
                                circle_id = derive_circle_id('channel', message.id, idx, len(circle_specs), override=spec.circle_id)
                                facilitator_id = None
                                if spec.facilitator:
                                    facilitator_id = _resolve_handle_to_user_id(
                                        db_manager,
                                        spec.facilitator,
                                        channel_id=channel_id,
                                        author_id=g.api_key_info.user_id,
                                    )
                                if not facilitator_id:
                                    facilitator_id = g.api_key_info.user_id
                                if spec.participants is not None:
                                    resolved_participants = _resolve_handle_list(
                                        db_manager,
                                        spec.participants,
                                        channel_id=channel_id,
                                        author_id=g.api_key_info.user_id,
                                    )
                                    spec.participants = resolved_participants

                                circle_manager.upsert_circle(
                                    circle_id=circle_id,
                                    source_type='channel',
                                    source_id=message.id,
                                    created_by=g.api_key_info.user_id,
                                    spec=spec,
                                    channel_id=channel_id,
                                    facilitator_id=facilitator_id,
                                    visibility=circle_visibility,
                                    origin_peer=p2p_manager.get_peer_id() if p2p_manager else None,
                                    created_at=message.created_at.isoformat() if getattr(message, 'created_at', None) else None,
                                )
                except Exception as circle_err:
                    logger.warning(f"Inline circle creation failed: {circle_err}")

                # Inline task creation from [task] blocks
                try:
                    task_manager = current_app.config.get('TASK_MANAGER')
                    if task_manager:
                        task_specs = parse_task_blocks(content or '')
                        if task_specs:
                            task_visibility = 'network'
                            try:
                                with db_manager.get_connection() as conn:
                                    row = conn.execute(
                                        "SELECT privacy_mode FROM channels WHERE id = ?",
                                        (channel_id,)
                                    ).fetchone()
                                    if row and row['privacy_mode'] and row['privacy_mode'] != 'open':
                                        task_visibility = 'local'
                            except Exception:
                                task_visibility = 'local'

                            for idx, spec in enumerate(cast(Any, task_specs)):
                                spec = cast(Any, spec)
                                if not spec.confirmed:
                                    continue
                                task_id = derive_task_id('channel', message.id, idx, len(task_specs), override=spec.task_id)
                                assignee_id = _resolve_handle_to_user_id(
                                    db_manager,
                                    spec.assignee,
                                    channel_id=channel_id,
                                    author_id=g.api_key_info.user_id,
                                )
                                editor_ids = _resolve_handle_list(
                                    db_manager,
                                    spec.editors or [],
                                    channel_id=channel_id,
                                    author_id=g.api_key_info.user_id,
                                )
                                meta_payload = {
                                    'inline_task': True,
                                    'source_type': 'channel_message',
                                    'source_id': message.id,
                                    'channel_id': channel_id,
                                }
                                if editor_ids:
                                    meta_payload['editors'] = editor_ids

                                task = task_manager.create_task(
                                    task_id=task_id,
                                    title=spec.title,
                                    description=spec.description,
                                    status=spec.status,
                                    priority=spec.priority,
                                    created_by=g.api_key_info.user_id,
                                    assigned_to=assignee_id,
                                    due_at=spec.due_at.isoformat() if spec.due_at else None,
                                    visibility=task_visibility,
                                    metadata=meta_payload,
                                    origin_peer=p2p_manager.get_peer_id() if p2p_manager else None,
                                    source_type='agent',
                                    updated_by=g.api_key_info.user_id,
                                )

                                if task and task.visibility == 'network' and p2p_manager and p2p_manager.is_running():
                                    try:
                                        p2p_manager.broadcast_interaction(
                                            item_id=task.id,
                                            user_id=g.api_key_info.user_id,
                                            action='task_create',
                                            item_type='task',
                                            extra={'task': task.to_dict()},
                                        )
                                    except Exception as task_err:
                                        logger.warning(f"Failed to broadcast task create: {task_err}")
                except Exception as task_err:
                    logger.warning(f"Inline task creation failed: {task_err}")

                # Inline objective creation from [objective] blocks
                try:
                    objective_manager = current_app.config.get('OBJECTIVE_MANAGER')
                    if objective_manager:
                        obj_visibility = 'network'
                        try:
                            with db_manager.get_connection() as conn:
                                row = conn.execute(
                                    "SELECT privacy_mode FROM channels WHERE id = ?",
                                    (channel_id,)
                                ).fetchone()
                                if row and row['privacy_mode'] and row['privacy_mode'] != 'open':
                                    obj_visibility = 'local'
                        except Exception:
                            obj_visibility = 'local'

                        _sync_inline_objectives_from_content(
                            objective_manager=objective_manager,
                            db_manager=db_manager,
                            content=content,
                            scope='channel',
                            source_id=message.id,
                            actor_id=g.api_key_info.user_id,
                            objective_visibility=obj_visibility,
                            source_type='channel_message',
                            origin_peer=p2p_manager.get_peer_id() if p2p_manager else None,
                            created_at=message.created_at.isoformat() if getattr(message, 'created_at', None) else None,
                            channel_id=channel_id,
                        )
                except Exception as obj_err:
                    logger.warning(f"Inline objective creation failed: {obj_err}")

                # Inline request creation from [request] blocks
                try:
                    request_manager = current_app.config.get('REQUEST_MANAGER')
                    if request_manager:
                        req_visibility = 'network'
                        try:
                            with db_manager.get_connection() as conn:
                                row = conn.execute(
                                    "SELECT privacy_mode FROM channels WHERE id = ?",
                                    (channel_id,)
                                ).fetchone()
                                if row and row['privacy_mode'] and row['privacy_mode'] != 'open':
                                    req_visibility = 'local'
                        except Exception:
                            req_visibility = 'local'

                        _sync_inline_requests_from_content(
                            request_manager=request_manager,
                            db_manager=db_manager,
                            content=content,
                            scope='channel',
                            source_id=message.id,
                            actor_id=g.api_key_info.user_id,
                            visibility=req_visibility,
                            source_type='channel_message',
                            origin_peer=p2p_manager.get_peer_id() if p2p_manager else None,
                            created_at=message.created_at.isoformat() if getattr(message, 'created_at', None) else None,
                            permissions=None,
                            channel_id=channel_id,
                        )
                except Exception as req_err:
                    logger.warning(f"Inline request creation failed: {req_err}")

                # Inline signal creation from [signal] blocks
                try:
                    signal_manager = current_app.config.get('SIGNAL_MANAGER')
                    if signal_manager:
                        sig_visibility = 'network'
                        try:
                            with db_manager.get_connection() as conn:
                                row = conn.execute(
                                    "SELECT privacy_mode FROM channels WHERE id = ?",
                                    (channel_id,)
                                ).fetchone()
                                if row and row['privacy_mode'] and row['privacy_mode'] != 'open':
                                    sig_visibility = 'local'
                        except Exception:
                            sig_visibility = 'local'

                        _sync_inline_signals_from_content(
                            signal_manager=signal_manager,
                            db_manager=db_manager,
                            content=content,
                            scope='channel',
                            source_id=message.id,
                            actor_id=g.api_key_info.user_id,
                            signal_visibility=sig_visibility,
                            source_type='channel_message',
                            origin_peer=p2p_manager.get_peer_id() if p2p_manager else None,
                            created_at=message.created_at.isoformat() if getattr(message, 'created_at', None) else None,
                            channel_id=channel_id,
                        )
                except Exception as sig_err:
                    logger.warning(f"Inline signal creation failed: {sig_err}")

                # Inline contract creation from [contract] blocks
                try:
                    contract_manager = current_app.config.get('CONTRACT_MANAGER')
                    if contract_manager:
                        contract_visibility = 'network'
                        try:
                            with db_manager.get_connection() as conn:
                                row = conn.execute(
                                    "SELECT privacy_mode FROM channels WHERE id = ?",
                                    (channel_id,)
                                ).fetchone()
                                if row and row['privacy_mode'] and row['privacy_mode'] != 'open':
                                    contract_visibility = 'local'
                        except Exception:
                            contract_visibility = 'local'

                        _sync_inline_contracts_from_content(
                            contract_manager=contract_manager,
                            db_manager=db_manager,
                            content=content,
                            scope='channel',
                            source_id=message.id,
                            actor_id=g.api_key_info.user_id,
                            contract_visibility=contract_visibility,
                            source_type='channel_message',
                            origin_peer=p2p_manager.get_peer_id() if p2p_manager else None,
                            created_at=message.created_at.isoformat() if getattr(message, 'created_at', None) else None,
                            channel_id=channel_id,
                        )
                except Exception as contract_err:
                    logger.warning(f"Inline contract creation failed: {contract_err}")

                # Inline handoff creation from [handoff] blocks
                try:
                    handoff_manager = current_app.config.get('HANDOFF_MANAGER')
                    if handoff_manager:
                        handoff_visibility = 'network'
                        try:
                            with db_manager.get_connection() as conn:
                                row = conn.execute(
                                    "SELECT privacy_mode FROM channels WHERE id = ?",
                                    (channel_id,)
                                ).fetchone()
                                if row and row['privacy_mode'] and row['privacy_mode'] != 'open':
                                    handoff_visibility = 'local'
                        except Exception:
                            handoff_visibility = 'local'

                        _sync_inline_handoffs_from_content(
                            handoff_manager=handoff_manager,
                            content=content,
                            scope='channel',
                            source_id=message.id,
                            actor_id=g.api_key_info.user_id,
                            visibility=handoff_visibility,
                            permissions=None,
                            channel_id=channel_id,
                            origin_peer=p2p_manager.get_peer_id() if p2p_manager else None,
                            created_at=message.created_at.isoformat() if getattr(message, 'created_at', None) else None,
                        )
                except Exception as handoff_err:
                    logger.warning(f"Inline handoff creation failed: {handoff_err}")

                # Inline skill registration from [skill] blocks
                try:
                    skill_manager = current_app.config.get('SKILL_MANAGER')
                    if skill_manager:
                        from ..core.skills import parse_skill_blocks
                        skill_specs = parse_skill_blocks(content or '')
                        for spec in cast(Any, skill_specs):
                            spec = cast(Any, spec)
                            skill_manager.register_skill(
                                spec,
                                source_type='channel_message',
                                source_id=message.id,
                                channel_id=channel_id,
                                author_id=g.api_key_info.user_id,
                            )
                except Exception as skill_err:
                    logger.warning(f"Inline skill registration failed: {skill_err}")

                # Broadcast to connected P2P peers so they store it too
                if p2p_manager and p2p_manager.is_running():
                    try:
                        # For private channels, use targeted peer sends
                        _api_tgt_peers = None
                        _api_mode = 'open'
                        try:
                            with db_manager.get_connection() as _aconn:
                                _apm = _aconn.execute(
                                    "SELECT privacy_mode FROM channels WHERE id = ?",
                                    (channel_id,)).fetchone()
                            if _apm:
                                _api_mode = (_apm['privacy_mode'] or 'open').lower()
                            if _api_mode in {'private', 'confidential'}:
                                _alp = p2p_manager.get_peer_id() if p2p_manager else None
                                _api_tgt_peers = channel_manager.get_member_peer_ids(
                                    channel_id, _alp)
                        except Exception:
                            pass
                        _api_security = dict(security_clean or {})
                        _api_security['privacy_mode'] = _api_mode
                        p2p_manager.broadcast_channel_message(
                            channel_id=channel_id,
                            user_id=g.api_key_info.user_id,
                            content=content,
                            message_id=message.id,
                            timestamp=message.created_at.isoformat() if hasattr(message.created_at, 'isoformat') else str(message.created_at),
                            attachments=message.attachments if hasattr(message, 'attachments') and message.attachments else None,
                            expires_at=message.expires_at.isoformat() if getattr(message, 'expires_at', None) else None,
                            ttl_seconds=ttl_seconds,
                            ttl_mode=ttl_mode,
                            parent_message_id=getattr(message, 'parent_message_id', None),
                            security=_api_security,
                            target_peer_ids=_api_tgt_peers,
                        )
                    except Exception as bcast_err:
                        logger.warning(f"P2P broadcast of channel message failed (non-fatal): {bcast_err}")

                # Emit mention events for @handles
                try:
                    mention_manager = current_app.config.get('MENTION_MANAGER')
                    mentions = extract_mentions(content or '')
                    if mention_manager and mentions:
                        targets = resolve_mention_targets(
                            db_manager,
                            mentions,
                            channel_id=channel_id,
                            author_id=g.api_key_info.user_id,
                        )
                        local_peer_id = None
                        try:
                            if p2p_manager:
                                local_peer_id = p2p_manager.get_peer_id()
                        except Exception:
                            local_peer_id = None
                        local_targets, remote_targets = split_mention_targets(targets, local_peer_id=local_peer_id)
                        preview = build_preview(content or '')
                        origin_peer = p2p_manager.get_peer_id() if p2p_manager else None

                        if local_targets:
                            local_mentioned_user_ids = [
                                cast(str, t.get('user_id'))
                                for t in local_targets
                                if t.get('user_id')
                            ]
                            record_mention_activity(
                                mention_manager,
                                p2p_manager,
                                target_ids=local_mentioned_user_ids,
                                source_type='channel_message',
                                source_id=message.id,
                                author_id=g.api_key_info.user_id,
                                origin_peer=origin_peer or '',
                                channel_id=channel_id,
                                preview=preview,
                                extra_ref={'channel_id': channel_id, 'message_id': message.id},
                                inbox_manager=current_app.config.get('INBOX_MANAGER'),
                                source_content=content,
                            )
                        if remote_targets and p2p_manager:
                            broadcast_mention_interaction(
                                p2p_manager,
                                source_type='channel_message',
                                source_id=message.id,
                                author_id=g.api_key_info.user_id,
                                target_user_ids=[cast(str, t.get('user_id')) for t in remote_targets if t.get('user_id')],
                                preview=preview,
                                channel_id=channel_id,
                                origin_peer=origin_peer,
                            )
                except Exception as mention_err:
                    logger.warning(f"Channel mention processing failed: {mention_err}")

                # Reply notifications for thread subscribers/root author.
                if parent_message_id:
                    try:
                        record_thread_reply_activity(
                            channel_manager=channel_manager,
                            inbox_manager=current_app.config.get('INBOX_MANAGER'),
                            channel_id=channel_id,
                            reply_message_id=message.id,
                            parent_message_id=parent_message_id,
                            author_id=g.api_key_info.user_id,
                            origin_peer=p2p_manager.get_peer_id() if p2p_manager else None,
                            source_content=content,
                            preview=build_preview(content or ''),
                            mentioned_user_ids=local_mentioned_user_ids,
                        )
                    except Exception as reply_err:
                        logger.debug(f"Thread reply inbox trigger skipped: {reply_err}")
                
                return jsonify({
                    'success': True,
                    'message': message.to_dict()
                }), 201
            else:
                return jsonify({'error': 'Failed to send message'}), 500
                
        except Exception as e:
            logger.error(f"Failed to send channel message: {e}")
            return jsonify({'error': 'Internal server error'}), 500

    @api.route('/channels/threads/subscription', methods=['GET'])
    @require_auth(Permission.READ_FEED)
    def get_channel_thread_subscription_api():
        """Get per-thread inbox subscription state for the authenticated user."""
        db_manager, _, _, _, channel_manager, _, _, _, _, _, _ = _get_app_components_any(current_app)
        try:
            channel_id = str(request.args.get('channel_id') or '').strip()
            message_id = str(request.args.get('message_id') or '').strip()
            if not channel_id or not message_id:
                return jsonify({'error': 'channel_id and message_id are required'}), 400

            access = channel_manager.get_channel_access_decision(
                channel_id=channel_id,
                user_id=g.api_key_info.user_id,
                require_membership=True,
            )
            if not access.get('allowed'):
                if str(access.get('reason') or '').startswith('governance_'):
                    return jsonify({
                        'error': 'Channel access blocked by admin governance policy',
                        'reason': access.get('reason'),
                    }), 403
                return _channel_not_found_response()

            state = channel_manager.get_thread_subscription_state(
                g.api_key_info.user_id,
                channel_id,
                message_id,
            )
            root_id = state.get('thread_root_message_id')
            if not root_id:
                return jsonify({'error': 'Thread not found'}), 404

            inbox_manager = current_app.config.get('INBOX_MANAGER')
            auto_subscribe = True
            if inbox_manager:
                try:
                    cfg = inbox_manager.get_config(g.api_key_info.user_id)
                    auto_subscribe = bool(cfg.get('auto_subscribe_own_threads', True))
                except Exception:
                    auto_subscribe = True

            explicit = state.get('explicit_subscribed')
            effective = bool(explicit) if explicit is not None else bool(state.get('is_root_author') and auto_subscribe)
            return jsonify({
                'success': True,
                'channel_id': channel_id,
                'message_id': message_id,
                'thread_root_message_id': root_id,
                'root_author_id': state.get('root_author_id'),
                'is_root_author': bool(state.get('is_root_author')),
                'explicit_subscribed': explicit,
                'auto_subscribe_own_threads': auto_subscribe,
                'subscribed': effective,
            })
        except Exception as e:
            logger.error(f"Get channel thread subscription failed: {e}", exc_info=True)
            return jsonify({'error': 'Internal server error'}), 500

    @api.route('/channels/threads/subscription', methods=['POST'])
    @require_auth(Permission.WRITE_FEED)
    def set_channel_thread_subscription_api():
        """Update per-thread inbox subscription state for the authenticated user."""
        db_manager, _, _, _, channel_manager, _, _, _, _, _, _ = _get_app_components_any(current_app)
        try:
            data = request.get_json(silent=True) or {}
            channel_id = str(data.get('channel_id') or '').strip()
            message_id = str(data.get('message_id') or '').strip()
            if not channel_id or not message_id:
                return jsonify({'error': 'channel_id and message_id are required'}), 400

            access = channel_manager.get_channel_access_decision(
                channel_id=channel_id,
                user_id=g.api_key_info.user_id,
                require_membership=True,
            )
            if not access.get('allowed'):
                if str(access.get('reason') or '').startswith('governance_'):
                    return jsonify({
                        'error': 'Channel access blocked by admin governance policy',
                        'reason': access.get('reason'),
                    }), 403
                return _channel_not_found_response()

            inbox_manager = current_app.config.get('INBOX_MANAGER')
            state = channel_manager.get_thread_subscription_state(
                g.api_key_info.user_id,
                channel_id,
                message_id,
            )
            root_id = state.get('thread_root_message_id')
            if not root_id:
                return jsonify({'error': 'Thread not found'}), 404

            explicit = state.get('explicit_subscribed')
            auto_subscribe = True
            if inbox_manager:
                try:
                    cfg = inbox_manager.get_config(g.api_key_info.user_id)
                    auto_subscribe = bool(cfg.get('auto_subscribe_own_threads', True))
                except Exception:
                    auto_subscribe = True
            current_effective = bool(explicit) if explicit is not None else bool(state.get('is_root_author') and auto_subscribe)

            subscribed_raw = data.get('subscribed')
            if subscribed_raw is None:
                subscribed = not current_effective
            elif isinstance(subscribed_raw, bool):
                subscribed = subscribed_raw
            else:
                subscribed = str(subscribed_raw).strip().lower() in {'1', 'true', 'yes', 'on'}

            update = channel_manager.set_thread_subscription(
                user_id=g.api_key_info.user_id,
                channel_id=channel_id,
                message_id=message_id,
                subscribed=subscribed,
                source='manual',
            )
            if not update.get('success'):
                return jsonify({'error': 'Failed to update thread subscription'}), 500

            new_state = channel_manager.get_thread_subscription_state(
                g.api_key_info.user_id,
                channel_id,
                message_id,
            )
            explicit_after = new_state.get('explicit_subscribed')
            effective_after = bool(explicit_after) if explicit_after is not None else bool(new_state.get('is_root_author') and auto_subscribe)
            return jsonify({
                'success': True,
                'channel_id': channel_id,
                'message_id': message_id,
                'thread_root_message_id': new_state.get('thread_root_message_id'),
                'root_author_id': new_state.get('root_author_id'),
                'is_root_author': bool(new_state.get('is_root_author')),
                'explicit_subscribed': explicit_after,
                'auto_subscribe_own_threads': auto_subscribe,
                'subscribed': effective_after,
            })
        except Exception as e:
            logger.error(f"Set channel thread subscription failed: {e}", exc_info=True)
            return jsonify({'error': 'Internal server error'}), 500

    @api.route('/channels/<channel_id>/search', methods=['GET'])
    @require_auth(Permission.READ_FEED)
    def search_channel_messages_api(channel_id):
        """Search messages in a channel by content."""
        db_manager, _, _, _, channel_manager, file_manager, _, _, _, _, p2p_manager = _get_app_components_any(current_app)

        try:
            expired = channel_manager.purge_expired_channel_messages()
            if expired and file_manager:
                for msg in expired:
                    owner_id = msg.get('user_id')
                    msg_id = msg.get('id')
                    for file_id in msg.get('attachment_ids') or []:
                        try:
                            file_info = file_manager.get_file(file_id)
                            if not file_info or file_info.uploaded_by != owner_id:
                                continue
                            if file_manager.is_file_referenced(file_id, exclude_channel_message_id=msg_id):
                                continue
                            file_manager.delete_file(file_id, owner_id)
                        except Exception:
                            continue
            if expired and p2p_manager and p2p_manager.is_running():
                import secrets as _sec
                for msg in expired:
                    if msg.get('user_id') != g.api_key_info.user_id:
                        continue
                    try:
                        signal_id = f"DS{_sec.token_hex(8)}"
                        p2p_manager.broadcast_delete_signal(
                            signal_id=signal_id,
                            data_type='channel_message',
                            data_id=msg.get('id'),
                            reason='expired_ttl',
                        )
                    except Exception as p2p_err:
                        logger.warning(f"Failed to broadcast TTL delete for channel message {msg.get('id')}: {p2p_err}")
            query = request.args.get('q', '').strip()
            limit = int(request.args.get('limit', 50))
            if not query:
                return jsonify({'error': 'Search query (q) is required'}), 400

            access = channel_manager.get_channel_access_decision(
                channel_id=channel_id,
                user_id=g.api_key_info.user_id,
                require_membership=True,
            )
            if not access.get('allowed'):
                if str(access.get('reason') or '').startswith('governance_'):
                    return jsonify({
                        'error': 'Channel access blocked by admin governance policy',
                        'reason': access.get('reason'),
                    }), 403
                return _channel_not_found_response()

            results = channel_manager.search_channel_messages(
                channel_id, query, g.api_key_info.user_id, limit)

            return jsonify({
                'messages': [m.to_dict() for m in results],
                'query': query,
                'count': len(results),
            })
        except Exception as e:
            logger.error(f"Channel search failed: {e}")
            return jsonify({'error': 'Internal server error'}), 500

    def _api_e2e_private_enabled() -> bool:
        cfg = current_app.config.get('CANOPY_CONFIG')
        sec = getattr(cfg, 'security', None)
        return bool(getattr(sec, 'e2e_private_channels', False))

    def _api_normalize_crypto_mode(raw_mode: Any) -> str:
        mode = str(raw_mode or '').strip().lower()
        if mode in {'legacy_plaintext', 'e2e_optional', 'e2e_enforced'}:
            return mode
        return 'legacy_plaintext'

    def _api_channel_targets_e2e(privacy_mode: str, crypto_mode: str) -> bool:
        return (
            str(privacy_mode or '').strip().lower() in {'private', 'confidential'}
            and str(crypto_mode or '').strip().lower() in {'e2e_optional', 'e2e_enforced'}
        )

    def _api_send_channel_key_to_peer(channel_manager: Any, p2p_manager: Any,
                                      channel_id: str, key_payload: dict[str, Any],
                                      peer_id: str, rotated_from: Optional[str] = None) -> bool:
        if not p2p_manager or not peer_id:
            return False
        local_peer = p2p_manager.get_peer_id() if p2p_manager else None
        if not local_peer or peer_id == local_peer:
            return False
        recipient_identity = p2p_manager.identity_manager.get_peer(peer_id)
        local_identity = p2p_manager.identity_manager.local_identity
        if not recipient_identity or not local_identity:
            channel_manager.upsert_channel_member_key_state(
                channel_id=channel_id,
                key_id=key_payload['key_id'],
                peer_id=peer_id,
                delivery_state='failed',
                last_error='unknown_peer_identity',
            )
            return False
        try:
            wrapped = encrypt_key_for_peer(
                key_material=key_payload['key_material'],
                local_identity=local_identity,
                recipient_identity=recipient_identity,
            )
        except Exception as err:
            channel_manager.upsert_channel_member_key_state(
                channel_id=channel_id,
                key_id=key_payload['key_id'],
                peer_id=peer_id,
                delivery_state='failed',
                last_error=f'wrap_failed:{err}',
            )
            return False

        channel_manager.upsert_channel_member_key_state(
            channel_id=channel_id,
            key_id=key_payload['key_id'],
            peer_id=peer_id,
            delivery_state='pending',
            last_error=None,
        )
        sent = p2p_manager.send_channel_key_distribution(
            to_peer=peer_id,
            channel_id=channel_id,
            key_id=key_payload['key_id'],
            encrypted_key=wrapped,
            key_version=int((key_payload.get('metadata') or {}).get('key_version') or 1),
            rotated_from=rotated_from or (key_payload.get('metadata') or {}).get('rotated_from'),
        )
        channel_manager.upsert_channel_member_key_state(
            channel_id=channel_id,
            key_id=key_payload['key_id'],
            peer_id=peer_id,
            delivery_state='delivered' if sent else 'failed',
            delivered=sent,
            last_error=None if sent else 'send_failed',
        )
        return bool(sent)

    def _api_ensure_channel_key(channel_manager: Any, channel_id: str,
                                origin_peer: Optional[str], rotated_from: Optional[str] = None) -> Optional[dict[str, Any]]:
        active = channel_manager.get_active_channel_key(channel_id)
        if active:
            key_bytes = channel_manager.decode_channel_key_material(active.get('key_material_enc'))
            if key_bytes:
                return {'key_id': active.get('key_id'), 'key_material': key_bytes, 'metadata': active.get('metadata') or {}}
        key_bytes = secrets.token_bytes(32)
        key_id = f"K{secrets.token_hex(8)}"
        metadata = {
            'algorithm': 'chacha20poly1305',
            'key_version': 1,
            'rotated_from': rotated_from,
            'generated_at': datetime.now(timezone.utc).isoformat(),
        }
        ok = channel_manager.upsert_channel_key(
            channel_id=channel_id,
            key_id=key_id,
            key_material_enc=encode_channel_key_material(key_bytes),
            created_by_peer=origin_peer,
            metadata=metadata,
        )
        if not ok:
            return None
        return {'key_id': key_id, 'key_material': key_bytes, 'metadata': metadata}

    def _api_rotate_channel_key(channel_manager: Any, channel_id: str,
                                origin_peer: Optional[str], rotated_from: Optional[str]) -> Optional[dict[str, Any]]:
        key_bytes = secrets.token_bytes(32)
        key_id = f"K{secrets.token_hex(8)}"
        metadata = {
            'algorithm': 'chacha20poly1305',
            'key_version': 1,
            'rotated_from': rotated_from,
            'generated_at': datetime.now(timezone.utc).isoformat(),
        }
        ok = channel_manager.upsert_channel_key(
            channel_id=channel_id,
            key_id=key_id,
            key_material_enc=encode_channel_key_material(key_bytes),
            created_by_peer=origin_peer,
            metadata=metadata,
        )
        if not ok:
            return None
        if rotated_from:
            channel_manager.revoke_channel_key(channel_id, rotated_from)
        return {'key_id': key_id, 'key_material': key_bytes, 'metadata': metadata}

    def _api_distribute_channel_key_to_members(channel_manager: Any, p2p_manager: Any,
                                               channel_id: str, key_payload: dict[str, Any],
                                               rotated_from: Optional[str] = None) -> int:
        if not p2p_manager:
            return 0
        local_peer = p2p_manager.get_peer_id() if p2p_manager else None
        peers = channel_manager.get_member_peer_ids(channel_id, local_peer)
        sent = 0
        for peer_id in sorted(peers):
            if _api_send_channel_key_to_peer(
                channel_manager=channel_manager,
                p2p_manager=p2p_manager,
                channel_id=channel_id,
                key_payload=key_payload,
                peer_id=peer_id,
                rotated_from=rotated_from,
            ):
                sent += 1
        return sent

    def _api_trigger_member_sync(db_manager: Any, channel_manager: Any, p2p_manager: Any,
                                 channel_id: str, target_user_id: str, action: str, role: str = 'member') -> None:
        if not p2p_manager or not p2p_manager.is_running():
            return
        try:
            with db_manager.get_connection() as conn:
                row = conn.execute(
                    "SELECT privacy_mode, name, channel_type, description, crypto_mode, created_by FROM channels WHERE id = ?",
                    (channel_id,),
                ).fetchone()
            if not row:
                return
            mode = (row['privacy_mode'] or 'open').strip().lower()
            if mode not in {'private', 'confidential'}:
                return
            local_peer = p2p_manager.get_peer_id()
            target_peer = ''
            try:
                user = db_manager.get_user(target_user_id)
                target_peer = (user.get('origin_peer') or '') if user else ''
            except Exception:
                target_peer = ''

            sync_payload_base = {
                'channel_name': row['name'] or '',
                'channel_type': row['channel_type'] or 'private',
                'channel_description': row['description'] or '',
                'privacy_mode': mode,
            }

            def _queue_and_send(target_peer_id: Optional[str]) -> bool:
                peer_id = str(target_peer_id or '').strip()
                if not peer_id or peer_id == local_peer:
                    return False
                sync_id = f"MS{secrets.token_hex(10)}"
                try:
                    channel_manager.queue_member_sync_delivery(
                        sync_id=sync_id,
                        channel_id=channel_id,
                        target_user_id=target_user_id,
                        action=action,
                        role=role,
                        target_peer_id=peer_id,
                        payload=sync_payload_base,
                    )
                except Exception:
                    pass
                sent = p2p_manager.broadcast_member_sync(
                    channel_id=channel_id,
                    target_user_id=target_user_id,
                    action=action,
                    target_peer_id=peer_id,
                    role=role,
                    channel_name=sync_payload_base['channel_name'],
                    channel_type=sync_payload_base['channel_type'],
                    channel_description=sync_payload_base['channel_description'],
                    privacy_mode=sync_payload_base['privacy_mode'],
                    sync_id=sync_id,
                )
                try:
                    channel_manager.mark_member_sync_delivery_attempt(
                        sync_id=sync_id,
                        sent=bool(sent),
                        error=None if sent else 'send_failed',
                    )
                except Exception:
                    pass
                return bool(sent)

            candidates: list[str] = []
            if target_peer and target_peer != local_peer:
                candidates.append(target_peer)
            try:
                member_peers = channel_manager.get_member_peer_ids(channel_id, local_peer)
                for member_peer in sorted(member_peers):
                    member_peer_s = str(member_peer or '').strip()
                    if member_peer_s and member_peer_s != local_peer and member_peer_s not in candidates:
                        candidates.append(member_peer_s)
            except Exception:
                pass
            try:
                connected = p2p_manager.get_connected_peers() or []
                for cp in connected:
                    pid = cp if isinstance(cp, str) else getattr(cp, 'peer_id', None)
                    pid_s = str(pid or '').strip()
                    if pid_s and pid_s != local_peer and pid_s not in candidates:
                        candidates.append(pid_s)
            except Exception:
                pass

            max_attempts = 3
            attempts = 0
            for candidate_peer in candidates:
                if attempts >= max_attempts:
                    break
                attempts += 1
                if _queue_and_send(candidate_peer):
                    break

            if action == 'add':
                p2p_manager.broadcast_channel_announce(
                    channel_id=channel_id,
                    name=row['name'] or '',
                    channel_type=row['channel_type'] or 'private',
                    description=row['description'] or '',
                    privacy_mode=mode,
                    created_by_user_id=row['created_by'] if row and 'created_by' in row.keys() else None,
                )
                crypto_mode = _api_normalize_crypto_mode(row['crypto_mode'])
                if _api_e2e_private_enabled() and _api_channel_targets_e2e(mode, crypto_mode) and target_peer and target_peer != local_peer:
                    active_key = channel_manager.get_active_channel_key(channel_id)
                    if active_key:
                        key_bytes = channel_manager.decode_channel_key_material(active_key.get('key_material_enc'))
                        if key_bytes:
                            _api_send_channel_key_to_peer(
                                channel_manager=channel_manager,
                                p2p_manager=p2p_manager,
                                channel_id=channel_id,
                                key_payload={
                                    'key_id': active_key['key_id'],
                                    'key_material': key_bytes,
                                    'metadata': active_key.get('metadata') or {},
                                },
                                peer_id=target_peer,
                            )
        except Exception as e:
            logger.warning(f"API member sync trigger failed (non-fatal): {e}")

    @api.route('/channels', methods=['GET'])
    @require_auth(Permission.READ_FEED)
    def get_user_channels_api():
        """Get all channels for the current user."""
        _, _, _, _, channel_manager, _, _, _, _, _, _ = _get_app_components_any(current_app)
        
        try:
            channels = channel_manager.get_user_channels(g.api_key_info.user_id)
            
            return jsonify({
                'channels': [channel.to_dict() for channel in channels],
                'count': len(channels)
            })
            
        except Exception as e:
            logger.error(f"Failed to get user channels: {e}")
            return jsonify({'error': 'Internal server error'}), 500

    @api.route('/channels', methods=['POST'])
    @require_auth(Permission.WRITE_FEED)
    def create_channel():
        """Create a new channel."""
        db_manager, _, _, _, channel_manager, _, _, _, _, _, p2p_manager = _get_app_components_any(current_app)
        
        try:
            data = request.get_json()
            if not data:
                return jsonify({'error': 'JSON data required'}), 400
            
            name = data.get('name', '').strip().lstrip('#').strip()
            description = data.get('description', '').strip()
            privacy_mode = (data.get('privacy_mode') or 'open').strip().lower()
            channel_type_str = data.get('type', 'public')
            requested_crypto_mode = _api_normalize_crypto_mode(data.get('crypto_mode'))
            if privacy_mode not in {'open', 'guarded', 'private', 'confidential'}:
                return jsonify({'error': 'Invalid privacy mode'}), 400
            
            if not name:
                return jsonify({'error': 'Channel name required'}), 400
            
            from ..core.channels import ChannelType
            try:
                channel_type = ChannelType(channel_type_str)
            except ValueError:
                return jsonify({'error': f'Invalid channel type: {channel_type_str}'}), 400

            governance = channel_manager.get_user_channel_governance(g.api_key_info.user_id)
            if governance.get('enabled'):
                is_public_open = (
                    privacy_mode == 'open'
                    and channel_type in {ChannelType.PUBLIC, ChannelType.GENERAL}
                )
                if governance.get('block_public_channels') and is_public_open:
                    return jsonify({
                        'error': 'Channel creation blocked by admin governance policy',
                        'reason': 'governance_public_channels_blocked',
                    }), 403
                if governance.get('restrict_to_allowed_channels'):
                    return jsonify({
                        'error': 'Channel creation blocked by admin governance policy',
                        'reason': 'governance_channel_creation_not_allowlisted',
                    }), 403
            
            channel = channel_manager.create_channel(
                name, channel_type, g.api_key_info.user_id, description,
                privacy_mode=privacy_mode,
                origin_peer=p2p_manager.get_peer_id() if p2p_manager else None,
            )
            
            if channel:
                # Broadcast CHANNEL_ANNOUNCE to connected peers
                if p2p_manager and p2p_manager.is_running():
                    try:
                        _api_priv = (channel.privacy_mode or '').lower() in {'private', 'confidential'}
                        _api_lp = p2p_manager.get_peer_id() if p2p_manager else None
                        _api_mpids = None
                        _api_mbp: Optional[dict[str, list[str]]] = None
                        if _api_priv:
                            _api_mpids = channel_manager.get_member_peer_ids(
                                channel.id, _api_lp)
                            _api_mbp = {}
                            if _api_mpids:
                                _api_mems = channel_manager.get_channel_members_list(channel.id)
                                for _am in _api_mems:
                                    _auid = _am.get('user_id')
                                    if _auid:
                                        try:
                                            _au = db_manager.get_user(_auid)
                                            _aop = (_au.get('origin_peer') or '') if _au else ''
                                        except Exception:
                                            _aop = ''
                                        _apk = _aop if _aop else _api_lp
                                        if _apk and _apk in _api_mpids:
                                            _api_mbp.setdefault(_apk, []).append(_auid)
                        p2p_manager.broadcast_channel_announce(
                            channel_id=channel.id,
                            name=channel.name,
                            channel_type=channel.channel_type.value,
                            description=channel.description or '',
                            privacy_mode=channel.privacy_mode,
                            created_by_user_id=channel.created_by,
                            member_peer_ids=_api_mpids,
                            initial_members_by_peer=_api_mbp,
                        )
                    except Exception as ann_err:
                        logger.warning(f"P2P channel announce failed (non-fatal): {ann_err}")

                # Phase-2 E2E bootstrap for targeted channels.
                try:
                    if _api_e2e_private_enabled() and (channel.privacy_mode or '').lower() in {'private', 'confidential'}:
                        crypto_mode = requested_crypto_mode
                        if crypto_mode == 'legacy_plaintext':
                            crypto_mode = 'e2e_optional'
                        channel_manager.set_channel_crypto_mode(channel.id, crypto_mode)
                        channel.crypto_mode = crypto_mode
                        if _api_channel_targets_e2e(channel.privacy_mode, crypto_mode):
                            key_payload = _api_ensure_channel_key(
                                channel_manager=channel_manager,
                                channel_id=channel.id,
                                origin_peer=(p2p_manager.get_peer_id() if p2p_manager else None),
                                rotated_from=None,
                            )
                            if key_payload and p2p_manager and p2p_manager.is_running():
                                _api_distribute_channel_key_to_members(
                                    channel_manager=channel_manager,
                                    p2p_manager=p2p_manager,
                                    channel_id=channel.id,
                                    key_payload=key_payload,
                                )
                except Exception as key_err:
                    logger.warning(f"API E2E key bootstrap failed for {channel.id}: {key_err}")

                return jsonify({
                    'success': True,
                    'channel': channel.to_dict()
                }), 201
            else:
                return jsonify({'error': 'Failed to create channel'}), 500
                
        except Exception as e:
            logger.error(f"Failed to create channel: {e}")
            return jsonify({'error': 'Internal server error'}), 500

    @api.route('/channels/<channel_id>', methods=['PATCH', 'PUT'])
    @require_auth(Permission.WRITE_FEED)
    def update_channel(channel_id):
        """Update channel settings (privacy_mode)."""
        db_manager, _, _, _, channel_manager, _, _, _, _, _, p2p_manager = _get_app_components_any(current_app)
        try:
            data = request.get_json() or {}
            privacy_mode = (data.get('privacy_mode') or '').strip().lower()
            if privacy_mode not in {'open', 'guarded', 'private', 'confidential'}:
                return jsonify({'error': 'Invalid privacy mode'}), 400
            if channel_id == 'general':
                return jsonify({'error': 'General is always open. Use mute instead.'}), 403

            local_peer_id = None
            try:
                if p2p_manager:
                    local_peer_id = p2p_manager.get_peer_id()
            except Exception:
                local_peer_id = None

            owner_id = db_manager.get_instance_owner_user_id()
            allow_admin = owner_id is not None and owner_id == g.api_key_info.user_id

            success = channel_manager.update_channel_privacy(
                channel_id=channel_id,
                user_id=g.api_key_info.user_id,
                privacy_mode=privacy_mode,
                allow_admin=allow_admin,
                local_peer_id=local_peer_id,
            )
            if not success:
                return jsonify({'error': 'Not authorized to update privacy'}), 403

            if p2p_manager and p2p_manager.is_running():
                try:
                    with db_manager.get_connection() as conn:
                        row = conn.execute(
                            "SELECT name, channel_type, description, created_by FROM channels WHERE id = ?",
                            (channel_id,)
                        ).fetchone()
                    if row:
                        member_peer_ids: Optional[list[str]] = None
                        members_by_peer: Optional[dict[str, list[str]]] = None
                        if privacy_mode in {'private', 'confidential'}:
                            local_peer = p2p_manager.get_peer_id() if p2p_manager else None
                            member_peer_ids = channel_manager.get_member_peer_ids(channel_id, local_peer)
                            members_by_peer = {}
                            try:
                                members = channel_manager.get_channel_members_list(channel_id)
                                for member in members:
                                    uid = member.get('user_id')
                                    if not uid:
                                        continue
                                    user_row = db_manager.get_user(uid)
                                    peer_key = (user_row.get('origin_peer') if user_row else '') or local_peer
                                    if peer_key and peer_key in member_peer_ids:
                                        members_by_peer.setdefault(peer_key, []).append(uid)
                            except Exception:
                                members_by_peer = None
                        p2p_manager.broadcast_channel_announce(
                            channel_id=channel_id,
                            name=row['name'],
                            channel_type=row['channel_type'],
                            description=row['description'] or '',
                            privacy_mode=privacy_mode,
                            created_by_user_id=row['created_by'] if row and 'created_by' in row.keys() else None,
                            member_peer_ids=member_peer_ids,
                            initial_members_by_peer=members_by_peer,
                        )
                except Exception as ann_err:
                    logger.warning(f"Channel privacy announce failed: {ann_err}")

            return jsonify({'success': True, 'privacy_mode': privacy_mode})
        except Exception as e:
            logger.error(f"Failed to update channel privacy: {e}")
            return jsonify({'error': 'Internal server error'}), 500

    # Channel member management endpoints
    @api.route('/channels/<channel_id>/members', methods=['GET'])
    @require_auth(Permission.READ_FEED)
    def get_channel_members_api(channel_id):
        """List members of a channel. Caller must be a member of the channel."""
        _, _, _, _, channel_manager, _, _, _, _, _, _ = get_app_components(current_app)
        if not channel_manager:
            return jsonify({'error': 'Channels not available'}), 503
        try:
            access = channel_manager.get_channel_access_decision(
                channel_id=channel_id,
                user_id=g.api_key_info.user_id,
                require_membership=True,
            )
            if not access.get('allowed'):
                if str(access.get('reason') or '').startswith('governance_'):
                    return jsonify({
                        'error': 'Channel access blocked by admin governance policy',
                        'reason': access.get('reason'),
                    }), 403
                return _channel_not_found_response()
            members = channel_manager.get_channel_members_list(channel_id)
            return jsonify({'members': members, 'count': len(members)})
        except Exception as e:
            logger.error(f"Get channel members failed: {e}")
            return jsonify({'error': 'Internal server error'}), 500

    @api.route('/channels/<channel_id>/members', methods=['POST'])
    @require_auth(Permission.WRITE_FEED)
    def add_channel_member_api(channel_id):
        """Add a user to a channel."""
        db_manager, _, _, _, channel_manager, _, _, _, _, _, p2p_manager = _get_app_components_any(current_app)
        try:
            data = request.get_json() or {}
            target_user_id = data.get('user_id')
            role = data.get('role', 'member')
            if not target_user_id:
                return jsonify({'error': 'user_id required'}), 400
            ok = channel_manager.add_member(channel_id, target_user_id,
                                            g.api_key_info.user_id, role)
            if ok:
                _api_trigger_member_sync(
                    db_manager=db_manager,
                    channel_manager=channel_manager,
                    p2p_manager=p2p_manager,
                    channel_id=channel_id,
                    target_user_id=target_user_id,
                    action='add',
                    role=role,
                )
                return jsonify({'success': True})
            return jsonify({'error': 'Permission denied or user not found'}), 403
        except Exception as e:
            logger.error(f"Add channel member failed: {e}")
            return jsonify({'error': 'Internal server error'}), 500

    @api.route('/channels/<channel_id>/members/<user_id>', methods=['DELETE'])
    @require_auth(Permission.WRITE_FEED)
    def remove_channel_member_api(channel_id, user_id):
        """Remove a user from a channel."""
        db_manager, _, _, _, channel_manager, _, _, _, _, _, p2p_manager = _get_app_components_any(current_app)
        try:
            ok = channel_manager.remove_member(channel_id, user_id,
                                               g.api_key_info.user_id)
            if ok:
                _api_trigger_member_sync(
                    db_manager=db_manager,
                    channel_manager=channel_manager,
                    p2p_manager=p2p_manager,
                    channel_id=channel_id,
                    target_user_id=user_id,
                    action='remove',
                    role='member',
                )
                try:
                    if _api_e2e_private_enabled() and p2p_manager and p2p_manager.is_running():
                        with db_manager.get_connection() as conn:
                            row = conn.execute(
                                "SELECT privacy_mode, crypto_mode FROM channels WHERE id = ?",
                                (channel_id,),
                            ).fetchone()
                        privacy_mode = (row['privacy_mode'] if row and 'privacy_mode' in row.keys() else 'open') or 'open'
                        crypto_mode = _api_normalize_crypto_mode(
                            row['crypto_mode'] if row and 'crypto_mode' in row.keys() else 'legacy_plaintext'
                        )
                        if _api_channel_targets_e2e(privacy_mode, crypto_mode):
                            prev_key = channel_manager.get_active_channel_key(channel_id)
                            prev_key_id = prev_key.get('key_id') if prev_key else None
                            new_key_payload = _api_rotate_channel_key(
                                channel_manager=channel_manager,
                                channel_id=channel_id,
                                origin_peer=p2p_manager.get_peer_id(),
                                rotated_from=prev_key_id,
                            )
                            if new_key_payload:
                                _api_distribute_channel_key_to_members(
                                    channel_manager=channel_manager,
                                    p2p_manager=p2p_manager,
                                    channel_id=channel_id,
                                    key_payload=new_key_payload,
                                    rotated_from=prev_key_id,
                                )
                except Exception as rotate_err:
                    logger.warning(f"API E2E key rotation failed for channel {channel_id}: {rotate_err}")
                return jsonify({'success': True})
            return jsonify({'error': 'Permission denied or user not found'}), 403
        except Exception as e:
            logger.error(f"Remove channel member failed: {e}")
            return jsonify({'error': 'Internal server error'}), 500

    @api.route('/channels/<channel_id>/members/<user_id>/role', methods=['PUT'])
    @require_auth(Permission.WRITE_FEED)
    def set_member_role_api(channel_id, user_id):
        """Change a channel member's role (admin only)."""
        _, _, _, _, channel_manager, _, _, _, _, _, _ = _get_app_components_any(current_app)
        try:
            data = request.get_json() or {}
            new_role = data.get('role')
            if new_role not in ('admin', 'member'):
                return jsonify({'error': 'role must be "admin" or "member"'}), 400
            ok = channel_manager.set_member_role(channel_id, user_id,
                                                 new_role, g.api_key_info.user_id)
            if ok:
                return jsonify({'success': True, 'role': new_role})
            return jsonify({'error': 'Permission denied or user not found'}), 403
        except Exception as e:
            logger.error(f"Set member role failed: {e}")
            return jsonify({'error': 'Internal server error'}), 500

    @api.route('/channels/<channel_id>', methods=['DELETE'])
    @require_auth(Permission.DELETE_DATA)
    def delete_channel_api(channel_id):
        """Delete a channel. API keys with DELETE_DATA can force-remove any local replica."""
        db_manager, _, _, _, channel_manager, _, _, _, _, _, p2p_manager = _get_app_components_any(current_app)
        try:
            if channel_id == 'general':
                return jsonify({'error': 'General cannot be deleted'}), 403

            local_peer_id = None
            if p2p_manager:
                try:
                    local_peer_id = p2p_manager.get_peer_id()
                except Exception:
                    local_peer_id = None

            with db_manager.get_connection() as conn:
                channel_row = conn.execute(
                    "SELECT origin_peer, privacy_mode FROM channels WHERE id = ?",
                    (channel_id,),
                ).fetchone()
            if not channel_row:
                return jsonify({'error': 'Channel not found'}), 404

            origin_peer = str(
                channel_row['origin_peer']
                if hasattr(channel_row, 'keys') and 'origin_peer' in channel_row.keys()
                else channel_row[0]
            ).strip()
            if origin_peer.lower() == 'none':
                origin_peer = ''
            privacy_mode = str(
                channel_row['privacy_mode']
                if hasattr(channel_row, 'keys') and 'privacy_mode' in channel_row.keys()
                else channel_row[1]
            ).strip().lower() or 'open'
            is_origin_local = (not origin_peer) or (
                local_peer_id is not None and origin_peer == local_peer_id
            )

            target_peers: set[str] = set()
            if (
                is_origin_local
                and p2p_manager
                and p2p_manager.is_running()
                and privacy_mode in {'private', 'confidential'}
            ):
                try:
                    target_peers = set(channel_manager.get_member_peer_ids(channel_id, local_peer_id))
                    if local_peer_id:
                        target_peers.discard(local_peer_id)
                except Exception:
                    target_peers = set()

            ok = channel_manager.delete_channel(channel_id, g.api_key_info.user_id, force=True)
            if ok:
                if is_origin_local and p2p_manager and p2p_manager.is_running():
                    reason = 'channel_deleted_by_origin'
                    if privacy_mode in {'private', 'confidential'}:
                        for peer_id in sorted(target_peers):
                            try:
                                p2p_manager.broadcast_delete_signal(
                                    signal_id=f"DS{secrets.token_hex(8)}",
                                    data_type='channel',
                                    data_id=channel_id,
                                    reason=reason,
                                    target_peer=peer_id,
                                )
                            except Exception as p2p_err:
                                logger.warning(
                                    f"Failed to send targeted channel delete signal for {channel_id} "
                                    f"to {peer_id}: {p2p_err}"
                                )
                    else:
                        try:
                            p2p_manager.broadcast_delete_signal(
                                signal_id=f"DS{secrets.token_hex(8)}",
                                data_type='channel',
                                data_id=channel_id,
                                reason=reason,
                            )
                        except Exception as p2p_err:
                            logger.warning(
                                f"Failed to broadcast channel delete signal for {channel_id}: {p2p_err}"
                            )
                return jsonify({
                    'success': True,
                    'local_only': not is_origin_local,
                })
            return jsonify({'error': 'Permission denied — admin role required'}), 403
        except Exception as e:
            logger.error(f"Delete channel failed: {e}")
            return jsonify({'error': 'Internal server error'}), 500

    # ------------------------------------------------------------------ #
    #  Tasks (collaborative work items)                                   #
    # ------------------------------------------------------------------ #

    @api.route('/tasks', methods=['GET'])
    @require_auth(Permission.READ_FEED)
    def list_tasks_api():
        """List tasks visible to the local node."""
        try:
            task_manager = current_app.config.get('TASK_MANAGER')
            if not task_manager:
                return jsonify({'error': 'Task manager unavailable'}), 500
            status = request.args.get('status')
            tasks = task_manager.list_tasks(status=status)
            return jsonify({'tasks': [t.to_dict() for t in tasks], 'count': len(tasks)})
        except Exception as e:
            logger.error(f"List tasks failed: {e}")
            return jsonify({'error': 'Internal server error'}), 500

    @api.route('/tasks/<task_id>', methods=['GET'])
    @require_auth(Permission.READ_FEED)
    def get_task_api(task_id):
        """Get a single task by ID."""
        try:
            task_manager = current_app.config.get('TASK_MANAGER')
            if not task_manager:
                return jsonify({'error': 'Task manager unavailable'}), 500
            task = task_manager.get_task(task_id)
            if not task:
                return jsonify({'error': 'Not found'}), 404
            return jsonify({'task': task.to_dict()})
        except Exception as e:
            logger.error(f"Get task failed: {e}")
            return jsonify({'error': 'Internal server error'}), 500

    @api.route('/tasks', methods=['POST'])
    @require_auth(Permission.WRITE_FEED)
    def create_task_api():
        """Create a collaborative task."""
        try:
            db_manager, _, _, _, _, _, _, _, profile_manager, _, p2p_manager = _get_app_components_any(current_app)
            task_manager = current_app.config.get('TASK_MANAGER')
            if not task_manager:
                return jsonify({'error': 'Task manager unavailable'}), 500

            data = request.get_json() or {}
            title = (data.get('title') or '').strip()
            if not title:
                return jsonify({'error': 'title required', 'code': 'missing_title'}), 400

            description = (data.get('description') or '').strip() or None
            status = data.get('status')
            priority = data.get('priority')
            assigned_to = data.get('assigned_to') or None
            objective_id = data.get('objective_id') or None
            due_at = data.get('due_at') or None
            visibility = data.get('visibility') or 'network'
            metadata = data.get('metadata') if isinstance(data.get('metadata'), dict) else None

            from ..core.tasks import TASK_STATUSES, TASK_PRIORITIES, TASK_VISIBILITY
            if status is not None and str(status).strip() != "":
                status_clean = str(status).strip().lower()
                if status_clean not in TASK_STATUSES:
                    return jsonify({
                        'error': 'Invalid task status',
                        'code': 'invalid_status',
                        'allowed': list(TASK_STATUSES),
                    }), 400
                status = status_clean
            if priority is not None and str(priority).strip() != "":
                priority_clean = str(priority).strip().lower()
                if priority_clean not in TASK_PRIORITIES:
                    return jsonify({
                        'error': 'Invalid task priority',
                        'code': 'invalid_priority',
                        'allowed': list(TASK_PRIORITIES),
                    }), 400
                priority = priority_clean
            if visibility is not None and str(visibility).strip() != "":
                visibility_clean = str(visibility).strip().lower()
                if visibility_clean not in TASK_VISIBILITY:
                    return jsonify({
                        'error': 'Invalid task visibility',
                        'code': 'invalid_visibility',
                        'allowed': list(TASK_VISIBILITY),
                    }), 400
                visibility = visibility_clean

            origin_peer = None
            try:
                if p2p_manager:
                    origin_peer = p2p_manager.get_peer_id()
            except Exception:
                origin_peer = None

            task = task_manager.create_task(
                title=title,
                description=description,
                status=status,
                priority=priority,
                created_by=g.api_key_info.user_id,
                assigned_to=assigned_to,
                objective_id=objective_id,
                due_at=due_at,
                visibility=visibility,
                metadata=metadata,
                origin_peer=origin_peer,
                source_type='agent' if (g.api_key_info and g.api_key_info.user_id) else 'human',
                updated_by=g.api_key_info.user_id,
            )

            if not task:
                return jsonify({'error': 'Failed to create task', 'code': 'create_failed'}), 500

            if visibility == 'network' and p2p_manager and p2p_manager.is_running():
                try:
                    display_name = None
                    if profile_manager:
                        profile = profile_manager.get_profile(g.api_key_info.user_id)
                        if profile:
                            display_name = profile.display_name or profile.username
                    p2p_manager.broadcast_interaction(
                        item_id=task.id,
                        user_id=g.api_key_info.user_id,
                        action='task_create',
                        item_type='task',
                        display_name=display_name,
                        extra={'task': task.to_dict()},
                    )
                except Exception as p2p_err:
                    logger.warning(f"Failed to broadcast task create: {p2p_err}")

            return jsonify({'task': task.to_dict()}), 201
        except Exception as e:
            logger.error(f"Create task failed: {e}")
            return jsonify({'error': 'Internal server error'}), 500

    @api.route('/tasks/<task_id>', methods=['PATCH'])
    @require_auth(Permission.WRITE_FEED)
    def update_task_api(task_id):
        """Update a task."""
        try:
            db_manager, _, _, _, _, _, _, _, profile_manager, _, p2p_manager = _get_app_components_any(current_app)
            task_manager = current_app.config.get('TASK_MANAGER')
            if not task_manager:
                return jsonify({'error': 'Task manager unavailable'}), 500

            data = request.get_json() or {}
            updates = {}
            for key in ('title', 'description', 'status', 'priority', 'assigned_to', 'due_at', 'visibility', 'metadata', 'objective_id'):
                if key in data:
                    updates[key] = data.get(key)
            if not updates:
                return jsonify({'error': 'No updates provided', 'code': 'no_updates'}), 400

            from ..core.tasks import TASK_STATUSES, TASK_PRIORITIES, TASK_VISIBILITY
            if 'status' in updates and updates.get('status') is not None:
                status_clean = str(updates.get('status')).strip().lower()
                if status_clean not in TASK_STATUSES:
                    return jsonify({
                        'error': 'Invalid task status',
                        'code': 'invalid_status',
                        'allowed': list(TASK_STATUSES),
                    }), 400
                updates['status'] = status_clean
            if 'priority' in updates and updates.get('priority') is not None:
                priority_clean = str(updates.get('priority')).strip().lower()
                if priority_clean not in TASK_PRIORITIES:
                    return jsonify({
                        'error': 'Invalid task priority',
                        'code': 'invalid_priority',
                        'allowed': list(TASK_PRIORITIES),
                    }), 400
                updates['priority'] = priority_clean
            if 'visibility' in updates and updates.get('visibility') is not None:
                visibility_clean = str(updates.get('visibility')).strip().lower()
                if visibility_clean not in TASK_VISIBILITY:
                    return jsonify({
                        'error': 'Invalid task visibility',
                        'code': 'invalid_visibility',
                        'allowed': list(TASK_VISIBILITY),
                    }), 400
                updates['visibility'] = visibility_clean

            try:
                admin_id = None
                try:
                    admin_id = db_manager.get_instance_owner_user_id()
                except Exception:
                    admin_id = None
                task = task_manager.update_task(task_id, updates, actor_id=g.api_key_info.user_id,
                                                admin_user_id=admin_id)
            except PermissionError:
                return jsonify({'error': 'Not authorized to update task', 'code': 'not_authorized'}), 403
            if not task:
                return jsonify({'error': 'Not found', 'code': 'not_found'}), 404

            if task.visibility == 'network' and p2p_manager and p2p_manager.is_running():
                try:
                    display_name = None
                    if profile_manager:
                        profile = profile_manager.get_profile(g.api_key_info.user_id)
                        if profile:
                            display_name = profile.display_name or profile.username
                    p2p_manager.broadcast_interaction(
                        item_id=task.id,
                        user_id=g.api_key_info.user_id,
                        action='task_update',
                        item_type='task',
                        display_name=display_name,
                        extra={'task': task.to_dict()},
                    )
                except Exception as p2p_err:
                    logger.warning(f"Failed to broadcast task update: {p2p_err}")

            return jsonify({'task': task.to_dict()})
        except Exception as e:
            logger.error(f"Update task failed: {e}")
            return jsonify({'error': 'Internal server error'}), 500

    # ------------------------------------------------------------------ #
    #  Objectives                                                        #
    # ------------------------------------------------------------------ #

    @api.route('/objectives', methods=['GET'])
    @require_auth(Permission.READ_FEED)
    def list_objectives_api():
        """List objectives."""
        try:
            objective_manager = current_app.config.get('OBJECTIVE_MANAGER')
            if not objective_manager:
                return jsonify({'error': 'Objective manager unavailable'}), 500
            status = request.args.get('status')
            limit = int(request.args.get('limit', 50))
            include_members = request.args.get('include_members', '').lower() in ('1', 'true', 'yes')
            include_tasks = request.args.get('include_tasks', '').lower() in ('1', 'true', 'yes')
            objectives = objective_manager.list_objectives(limit=limit, status=status)
            if include_members or include_tasks:
                detailed = []
                for obj in objectives:
                    full = objective_manager.get_objective(
                        obj.get('id'),
                        include_members=include_members,
                        include_tasks=include_tasks,
                    )
                    if full:
                        detailed.append(full)
                objectives = detailed
            return jsonify({'objectives': objectives, 'count': len(objectives)})
        except Exception as e:
            logger.error(f"List objectives failed: {e}")
            return jsonify({'error': 'Internal server error'}), 500

    @api.route('/objectives/<objective_id>', methods=['GET'])
    @require_auth(Permission.READ_FEED)
    def get_objective_api(objective_id):
        """Get a single objective. Members and tasks included by default."""
        try:
            objective_manager = current_app.config.get('OBJECTIVE_MANAGER')
            if not objective_manager:
                return jsonify({'error': 'Objective manager unavailable'}), 500
            include_members = request.args.get('include_members', 'true').lower() not in ('0', 'false', 'no')
            include_tasks = request.args.get('include_tasks', 'true').lower() not in ('0', 'false', 'no')
            obj = objective_manager.get_objective(
                objective_id,
                include_members=include_members,
                include_tasks=include_tasks,
            )
            if not obj:
                return jsonify({'error': 'Not found'}), 404
            return jsonify({'objective': obj})
        except Exception as e:
            logger.error(f"Get objective failed: {e}")
            return jsonify({'error': 'Internal server error'}), 500

    @api.route('/objectives', methods=['POST'])
    @require_auth(Permission.WRITE_FEED)
    def create_objective_api():
        """Create a new objective."""
        try:
            db_manager, _, _, _, _, _, _, _, _, _, p2p_manager = _get_app_components_any(current_app)
            objective_manager = current_app.config.get('OBJECTIVE_MANAGER')
            if not objective_manager:
                return jsonify({'error': 'Objective manager unavailable'}), 500

            data = request.get_json() or {}
            title = (data.get('title') or '').strip()
            if not title:
                return jsonify({'error': 'title required'}), 400

            objective_id = (data.get('id') or data.get('objective_id') or '').strip()
            if not objective_id:
                objective_id = f"objective_{secrets.token_hex(8)}"

            description = (data.get('description') or '').strip() or None
            status = data.get('status')
            deadline = data.get('deadline') or None
            visibility = (data.get('visibility') or 'network').strip().lower()
            source_type = data.get('source_type') or 'api'
            source_id = data.get('source_id') or None

            raw_members = data.get('members') or []
            members_payload = []
            for member in raw_members:
                if isinstance(member, str):
                    uid = _resolve_handle_to_user_id(db_manager, member, author_id=g.api_key_info.user_id)
                    if uid:
                        members_payload.append({'user_id': uid, 'role': 'contributor'})
                    continue
                if not isinstance(member, dict):
                    continue
                uid = member.get('user_id') or None
                handle = member.get('handle') or member.get('name') or None
                if not uid and handle:
                    uid = _resolve_handle_to_user_id(db_manager, handle, author_id=g.api_key_info.user_id)
                if uid:
                    members_payload.append({'user_id': uid, 'role': member.get('role') or 'contributor'})

            raw_tasks = data.get('tasks') or []
            tasks_payload = []
            for task in raw_tasks:
                if not isinstance(task, dict):
                    continue
                title_val = (task.get('title') or '').strip()
                if not title_val:
                    continue
                assignee = task.get('assigned_to') or None
                if assignee and isinstance(assignee, str) and assignee.startswith('@'):
                    assignee_id = _resolve_handle_to_user_id(db_manager, assignee, author_id=g.api_key_info.user_id)
                else:
                    assignee_id = assignee
                tasks_payload.append({
                    'title': title_val,
                    'status': task.get('status') or 'open',
                    'priority': task.get('priority'),
                    'assigned_to': assignee_id,
                    'due_at': task.get('due_at'),
                    'metadata': task.get('metadata'),
                })

            origin_peer = None
            try:
                if p2p_manager:
                    origin_peer = p2p_manager.get_peer_id()
            except Exception:
                origin_peer = None

            obj = objective_manager.upsert_objective(
                objective_id=objective_id,
                title=title,
                description=description,
                status=status,
                deadline=deadline,
                created_by=g.api_key_info.user_id,
                visibility=visibility,
                origin_peer=origin_peer,
                source_type=source_type,
                source_id=source_id,
                members=members_payload,
                tasks=tasks_payload,
                updated_by=g.api_key_info.user_id,
            )
            if not obj:
                return jsonify({'error': 'Failed to create objective'}), 500
            return jsonify({'objective': obj}), 201
        except Exception as e:
            logger.error(f"Create objective failed: {e}")
            return jsonify({'error': 'Internal server error'}), 500

    @api.route('/objectives/<objective_id>', methods=['PATCH'])
    @require_auth(Permission.WRITE_FEED)
    def update_objective_api(objective_id):
        """Update an objective."""
        try:
            db_manager = current_app.config.get('DB_MANAGER')
            objective_manager = current_app.config.get('OBJECTIVE_MANAGER')
            if not objective_manager:
                return jsonify({'error': 'Objective manager unavailable'}), 500
            data = request.get_json() or {}
            updates = {}
            for key in ('title', 'description', 'status', 'deadline', 'visibility', 'source_type', 'source_id'):
                if key in data:
                    updates[key] = data.get(key)
            members = data.get('members')
            obj = None
            if updates:
                obj = objective_manager.update_objective(objective_id, updates, actor_id=g.api_key_info.user_id)
            if members is not None:
                members_payload = []
                for member in members or []:
                    if isinstance(member, str):
                        uid = _resolve_handle_to_user_id(db_manager, member, author_id=g.api_key_info.user_id)
                        if uid:
                            members_payload.append({'user_id': uid, 'role': 'contributor'})
                        continue
                    if not isinstance(member, dict):
                        continue
                    uid = member.get('user_id') or None
                    member_handle = member.get('handle')
                    if not uid and isinstance(member_handle, str):
                        uid = _resolve_handle_to_user_id(db_manager, member_handle, author_id=g.api_key_info.user_id)
                    if uid:
                        members_payload.append({'user_id': uid, 'role': member.get('role') or 'contributor'})
                obj = objective_manager.set_members(objective_id, members_payload, added_by=g.api_key_info.user_id)
            if not obj:
                obj = objective_manager.get_objective(objective_id, include_members=True, include_tasks=True)
            if not obj:
                return jsonify({'error': 'Not found'}), 404
            return jsonify({'objective': obj})
        except Exception as e:
            logger.error(f"Update objective failed: {e}")
            return jsonify({'error': 'Internal server error'}), 500

    @api.route('/objectives/<objective_id>/tasks', methods=['POST'])
    @require_auth(Permission.WRITE_FEED)
    def add_objective_task_api(objective_id):
        """Add tasks to an objective."""
        try:
            db_manager, _, _, _, _, _, _, _, _, _, p2p_manager = _get_app_components_any(current_app)
            objective_manager = current_app.config.get('OBJECTIVE_MANAGER')
            task_manager = current_app.config.get('TASK_MANAGER')
            if not objective_manager or not task_manager:
                return jsonify({'error': 'Objective manager unavailable'}), 500
            obj = objective_manager.get_objective(objective_id, include_members=False, include_tasks=False)
            if not obj:
                return jsonify({'error': 'Objective not found'}), 404

            data = request.get_json() or {}
            tasks: list[Any] = cast(list[Any], data.get('tasks')) if isinstance(data.get('tasks'), list) else [data]
            created = []
            for task in tasks:
                if not isinstance(task, dict):
                    continue
                title = (task.get('title') or '').strip()
                if not title:
                    continue
                assigned_to = task.get('assigned_to') or None
                if assigned_to and isinstance(assigned_to, str) and assigned_to.startswith('@'):
                    assigned_to = _resolve_handle_to_user_id(db_manager, assigned_to, author_id=g.api_key_info.user_id)
                task_obj = task_manager.create_task(
                    title=title,
                    description=task.get('description'),
                    status=task.get('status'),
                    priority=task.get('priority'),
                    created_by=g.api_key_info.user_id,
                    assigned_to=assigned_to,
                    due_at=task.get('due_at'),
                    visibility=obj.get('visibility') or 'network',
                    metadata=task.get('metadata'),
                    origin_peer=p2p_manager.get_peer_id() if p2p_manager else None,
                    source_type='objective',
                    updated_by=g.api_key_info.user_id,
                    objective_id=objective_id,
                )
                if task_obj:
                    created.append(task_obj.to_dict())
            return jsonify({'tasks': created, 'count': len(created)})
        except Exception as e:
            logger.error(f"Add objective task failed: {e}")
            return jsonify({'error': 'Internal server error'}), 500

    @api.route('/objectives/<objective_id>/tasks', methods=['PATCH'])
    @require_auth(Permission.WRITE_FEED)
    def update_objective_task_api(objective_id):
        """Update a task within an objective."""
        try:
            task_manager = current_app.config.get('TASK_MANAGER')
            if not task_manager:
                return jsonify({'error': 'Task manager unavailable'}), 500
            data = request.get_json() or {}
            task_id = data.get('task_id')
            if not task_id:
                return jsonify({'error': 'task_id required'}), 400
            updates = {}
            for key in ('title', 'description', 'status', 'priority', 'assigned_to', 'due_at', 'visibility', 'metadata'):
                if key in data:
                    updates[key] = data.get(key)
            updates['objective_id'] = objective_id
            task = task_manager.update_task(task_id, updates, actor_id=g.api_key_info.user_id)
            if not task:
                return jsonify({'error': 'Not found'}), 404
            return jsonify({'task': task.to_dict()})
        except PermissionError:
            return jsonify({'error': 'Not authorized'}), 403
        except Exception as e:
            logger.error(f"Update objective task failed: {e}")
            return jsonify({'error': 'Internal server error'}), 500

    # ------------------------------------------------------------------ #
    #  Requests (structured asks)                                        #
    # ------------------------------------------------------------------ #

    @api.route('/requests', methods=['GET'])
    @require_auth(Permission.READ_FEED)
    def list_requests_api():
        """List requests (optional filters)."""
        try:
            request_manager = current_app.config.get('REQUEST_MANAGER')
            if not request_manager:
                return jsonify({'error': 'Request manager unavailable'}), 500
            status = request.args.get('status') or None
            priority = request.args.get('priority') or None
            tag = request.args.get('tag') or None
            limit = int(request.args.get('limit', 50))
            include_members = request.args.get('include_members', '').lower() in ('1', 'true', 'yes')
            requests_list = request_manager.list_requests(
                limit=limit,
                status=status,
                priority=priority,
                tag=tag,
                include_members=include_members,
            )
            return jsonify({'requests': requests_list, 'count': len(requests_list)})
        except Exception as e:
            logger.error(f"List requests failed: {e}")
            return jsonify({'error': 'Internal server error'}), 500

    @api.route('/requests/<request_id>', methods=['GET'])
    @require_auth(Permission.READ_FEED)
    def get_request_api(request_id):
        """Get a single request by ID."""
        try:
            request_manager = current_app.config.get('REQUEST_MANAGER')
            if not request_manager:
                return jsonify({'error': 'Request manager unavailable'}), 500
            include_members = request.args.get('include_members', 'true').lower() not in ('0', 'false', 'no')
            req = request_manager.get_request(request_id, include_members=include_members)
            if not req:
                return jsonify({'error': 'Not found'}), 404
            return jsonify({'request': req})
        except Exception as e:
            logger.error(f"Get request failed: {e}")
            return jsonify({'error': 'Internal server error'}), 500

    @api.route('/requests', methods=['POST'])
    @require_auth(Permission.WRITE_FEED)
    def create_request_api():
        """Create a new request."""
        try:
            db_manager, _, _, _, _, _, _, _, _, _, p2p_manager = _get_app_components_any(current_app)
            request_manager = current_app.config.get('REQUEST_MANAGER')
            if not request_manager:
                return jsonify({'error': 'Request manager unavailable'}), 500

            data = request.get_json() or {}
            title = (data.get('title') or '').strip()
            if not title:
                return jsonify({'error': 'title required'}), 400

            request_id = (data.get('id') or data.get('request_id') or '').strip()
            if not request_id:
                request_id = f"request_{secrets.token_hex(8)}"

            request_text = (data.get('request') or data.get('description') or data.get('ask') or '').strip() or None
            required_output = (data.get('required_output') or data.get('deliverable') or '').strip() or None
            status = data.get('status')
            priority = data.get('priority')
            due_at = data.get('due_at') or data.get('due') or data.get('deadline')
            tags = data.get('tags') or []
            if isinstance(tags, str):
                tags = [t.strip() for t in tags.split(',') if t.strip()]

            visibility = (data.get('visibility') or 'network').strip().lower()
            if visibility not in ('network', 'local'):
                return jsonify({'error': 'Invalid visibility', 'allowed': ['network', 'local']}), 400

            source_type = data.get('source_type') or 'api'
            source_id = data.get('source_id') or None

            from ..core.requests import REQUEST_STATUSES, REQUEST_PRIORITIES
            if status is not None:
                status_clean = str(status).strip().lower()
                if status_clean not in REQUEST_STATUSES:
                    return jsonify({'error': 'Invalid status', 'allowed': list(REQUEST_STATUSES)}), 400
                status = status_clean
            if priority is not None:
                priority_clean = str(priority).strip().lower()
                if priority_clean not in REQUEST_PRIORITIES:
                    return jsonify({'error': 'Invalid priority', 'allowed': list(REQUEST_PRIORITIES)}), 400
                priority = priority_clean

            members_payload = []
            raw_members = data.get('members') or []
            for member in raw_members:
                if isinstance(member, str):
                    uid = _resolve_handle_to_user_id(db_manager, member, author_id=g.api_key_info.user_id)
                    if uid:
                        members_payload.append({'user_id': uid, 'role': 'assignee'})
                    continue
                if not isinstance(member, dict):
                    continue
                uid = member.get('user_id') or None
                handle = member.get('handle') or member.get('name') or None
                if not uid and handle:
                    uid = _resolve_handle_to_user_id(db_manager, handle, author_id=g.api_key_info.user_id)
                if uid:
                    members_payload.append({'user_id': uid, 'role': member.get('role') or 'assignee'})

            origin_peer = None
            try:
                if p2p_manager:
                    origin_peer = p2p_manager.get_peer_id()
            except Exception:
                origin_peer = None

            req = request_manager.upsert_request(
                request_id=request_id,
                title=title,
                created_by=g.api_key_info.user_id,
                request_text=request_text,
                required_output=required_output,
                status=status,
                priority=priority,
                tags=tags,
                due_at=due_at,
                visibility=visibility,
                origin_peer=origin_peer,
                source_type=source_type,
                source_id=source_id,
                actor_id=g.api_key_info.user_id,
                members=members_payload,
                members_defined=('members' in data),
            )
            if not req:
                return jsonify({'error': 'Failed to create request'}), 500
            return jsonify({'request': req}), 201
        except Exception as e:
            logger.error(f"Create request failed: {e}")
            return jsonify({'error': 'Internal server error'}), 500

    @api.route('/requests/<request_id>', methods=['PATCH'])
    @require_auth(Permission.WRITE_FEED)
    def update_request_api(request_id):
        """Update a request."""
        try:
            db_manager = cast(Any, current_app.config.get('DB_MANAGER'))
            request_manager = current_app.config.get('REQUEST_MANAGER')
            if not request_manager:
                return jsonify({'error': 'Request manager unavailable'}), 500

            data = request.get_json() or {}
            updates = {}
            for key in ('title', 'request', 'required_output', 'status', 'priority', 'due_at', 'metadata'):
                if key in data:
                    updates[key] = data.get(key)
            if 'description' in data and 'request' not in updates:
                updates['request'] = data.get('description')
            if 'due' in data and 'due_at' not in updates:
                updates['due_at'] = data.get('due')

            if 'tags' in data:
                tags = data.get('tags') or []
                if isinstance(tags, str):
                    tags = [t.strip() for t in tags.split(',') if t.strip()]
                updates['tags'] = tags

            from ..core.requests import REQUEST_STATUSES, REQUEST_PRIORITIES
            if 'status' in updates and updates.get('status') is not None:
                status_clean = str(updates.get('status')).strip().lower()
                if status_clean not in REQUEST_STATUSES:
                    return jsonify({'error': 'Invalid status', 'allowed': list(REQUEST_STATUSES)}), 400
                updates['status'] = status_clean
            if 'priority' in updates and updates.get('priority') is not None:
                priority_clean = str(updates.get('priority')).strip().lower()
                if priority_clean not in REQUEST_PRIORITIES:
                    return jsonify({'error': 'Invalid priority', 'allowed': list(REQUEST_PRIORITIES)}), 400
                updates['priority'] = priority_clean

            members_payload = None
            replace_members = False
            if 'members' in data:
                replace_members = True
                members_payload = []
                for member in data.get('members') or []:
                    if isinstance(member, str):
                        uid = _resolve_handle_to_user_id(db_manager, member, author_id=g.api_key_info.user_id)
                        if uid:
                            members_payload.append({'user_id': uid, 'role': 'assignee'})
                        continue
                    if not isinstance(member, dict):
                        continue
                    uid = member.get('user_id') or None
                    handle = member.get('handle') or member.get('name') or None
                    if not uid and handle:
                        uid = _resolve_handle_to_user_id(db_manager, handle, author_id=g.api_key_info.user_id)
                    if uid:
                        members_payload.append({'user_id': uid, 'role': member.get('role') or 'assignee'})

            if not updates and not replace_members:
                return jsonify({'error': 'No updates provided'}), 400

            try:
                admin_id = None
                try:
                    admin_id = db_manager.get_instance_owner_user_id()
                except Exception:
                    admin_id = None
                req = request_manager.update_request(
                    request_id,
                    updates,
                    actor_id=g.api_key_info.user_id,
                    admin_user_id=admin_id,
                    members=members_payload,
                    replace_members=replace_members,
                )
            except PermissionError:
                return jsonify({'error': 'Not authorized'}), 403

            if not req:
                return jsonify({'error': 'Not found'}), 404

            return jsonify({'request': req})
        except Exception as e:
            logger.error(f"Update request failed: {e}")
            return jsonify({'error': 'Internal server error'}), 500

    # ------------------------------------------------------------------ #
    #  Contracts (deterministic coordination objects)                    #
    # ------------------------------------------------------------------ #

    @api.route('/contracts', methods=['GET'])
    @require_auth(Permission.READ_FEED)
    def list_contracts_api():
        """List contracts (optional filters)."""
        try:
            contract_manager = current_app.config.get('CONTRACT_MANAGER')
            db_manager = current_app.config.get('DB_MANAGER')
            if not contract_manager:
                return jsonify({'error': 'Contract manager unavailable'}), 500

            status = request.args.get('status') or None
            owner_id = request.args.get('owner_id') or None
            source_type = request.args.get('source_type') or None
            source_id = request.args.get('source_id') or None
            visibility = request.args.get('visibility') or None
            limit = int(request.args.get('limit', 50))

            contracts = contract_manager.list_contracts(
                limit=limit,
                status=status,
                owner_id=owner_id,
                source_type=source_type,
                source_id=source_id,
                visibility=visibility,
            )

            admin_user_id = db_manager.get_instance_owner_user_id() if db_manager else None
            filtered = []
            for contract in contracts:
                vis = (contract.get('visibility') or 'network').lower()
                if vis in ('public', 'network'):
                    filtered.append(contract)
                    continue
                if g.api_key_info and g.api_key_info.user_id in (
                    contract.get('owner_id'),
                    contract.get('created_by'),
                ):
                    filtered.append(contract)
                    continue
                if g.api_key_info and g.api_key_info.user_id in set(contract.get('counterparties') or []):
                    filtered.append(contract)
                    continue
                if admin_user_id and g.api_key_info and g.api_key_info.user_id == admin_user_id:
                    filtered.append(contract)

            return jsonify({'contracts': filtered, 'count': len(filtered)})
        except Exception as e:
            logger.error(f"List contracts failed: {e}")
            return jsonify({'error': 'Internal server error'}), 500

    @api.route('/contracts/<contract_id>', methods=['GET'])
    @require_auth(Permission.READ_FEED)
    def get_contract_api(contract_id):
        """Get a single contract by ID."""
        try:
            contract_manager = current_app.config.get('CONTRACT_MANAGER')
            db_manager = current_app.config.get('DB_MANAGER')
            if not contract_manager:
                return jsonify({'error': 'Contract manager unavailable'}), 500
            contract = contract_manager.get_contract(contract_id)
            if not contract:
                return jsonify({'error': 'Not found'}), 404

            vis = (contract.get('visibility') or 'network').lower()
            if vis not in ('public', 'network'):
                admin_user_id = db_manager.get_instance_owner_user_id() if db_manager else None
                allowed = {
                    contract.get('owner_id'),
                    contract.get('created_by'),
                    admin_user_id,
                }
                allowed.update(set(contract.get('counterparties') or []))
                if not g.api_key_info or g.api_key_info.user_id not in allowed:
                    return jsonify({'error': 'Not authorized'}), 403

            return jsonify({'contract': contract})
        except Exception as e:
            logger.error(f"Get contract failed: {e}")
            return jsonify({'error': 'Internal server error'}), 500

    @api.route('/contracts', methods=['POST'])
    @require_auth(Permission.WRITE_FEED)
    def create_contract_api():
        """Create a contract directly via API."""
        try:
            db_manager, _, _, _, _, _, _, _, _, _, p2p_manager = get_app_components(current_app)
            contract_manager = current_app.config.get('CONTRACT_MANAGER')
            if not contract_manager:
                return jsonify({'error': 'Contract manager unavailable'}), 500

            from ..core.contracts import CONTRACT_STATUSES

            data = request.get_json() or {}
            title = (data.get('title') or '').strip()
            if not title:
                return jsonify({'error': 'title required'}), 400

            contract_id = (data.get('id') or data.get('contract_id') or '').strip()
            if not contract_id:
                contract_id = f"contract_{secrets.token_hex(8)}"

            summary = (data.get('summary') or data.get('description') or '').strip() or None
            terms = (data.get('terms') or data.get('body') or '').strip() or None
            status = (data.get('status') or 'proposed').strip().lower()
            if status not in CONTRACT_STATUSES:
                return jsonify({'error': 'Invalid status', 'allowed': list(CONTRACT_STATUSES)}), 400

            owner = data.get('owner') or data.get('owner_id')
            owner_id = _resolve_handle_to_user_id(db_manager, owner, author_id=g.api_key_info.user_id) if owner else None
            if not owner_id:
                owner_id = g.api_key_info.user_id

            counterparties = []
            raw_counterparties = data.get('counterparties') or data.get('participants') or []
            if isinstance(raw_counterparties, str):
                raw_counterparties = [p.strip() for p in re.split(r"[,;]", raw_counterparties) if p.strip()]
            for raw_cp in raw_counterparties:
                cp_id = _resolve_handle_to_user_id(db_manager, raw_cp, author_id=g.api_key_info.user_id)
                if cp_id:
                    counterparties.append(cp_id)

            visibility = (data.get('visibility') or 'network').strip().lower()
            if visibility not in ('network', 'local'):
                return jsonify({'error': 'Invalid visibility', 'allowed': ['network', 'local']}), 400

            origin_peer = None
            try:
                if p2p_manager:
                    origin_peer = p2p_manager.get_peer_id()
            except Exception:
                origin_peer = None

            contract = contract_manager.upsert_contract(
                contract_id=contract_id,
                title=title,
                summary=summary,
                terms=terms,
                status=status,
                owner_id=owner_id,
                counterparties=counterparties,
                created_by=g.api_key_info.user_id,
                visibility=visibility,
                origin_peer=origin_peer,
                source_type=data.get('source_type') or 'api',
                source_id=data.get('source_id') or None,
                expires_at=data.get('expires_at'),
                ttl_seconds=data.get('ttl_seconds'),
                ttl_mode=data.get('ttl_mode'),
                metadata=data.get('metadata') if isinstance(data.get('metadata'), dict) else None,
                actor_id=g.api_key_info.user_id,
            )
            if not contract:
                return jsonify({'error': 'Failed to create contract'}), 500
            return jsonify({'contract': contract}), 201
        except Exception as e:
            logger.error(f"Create contract failed: {e}")
            return jsonify({'error': 'Internal server error'}), 500

    @api.route('/contracts/<contract_id>', methods=['PATCH'])
    @require_auth(Permission.WRITE_FEED)
    def update_contract_api(contract_id):
        """Update contract state/content."""
        try:
            db_manager = current_app.config.get('DB_MANAGER')
            contract_manager = current_app.config.get('CONTRACT_MANAGER')
            if not contract_manager:
                return jsonify({'error': 'Contract manager unavailable'}), 500

            from ..core.contracts import CONTRACT_STATUSES

            data = request.get_json() or {}
            updates = {}
            for key in ('title', 'summary', 'terms', 'status', 'visibility', 'expires_at', 'ttl_seconds', 'ttl_mode', 'metadata'):
                if key in data:
                    updates[key] = data.get(key)

            if 'description' in data and 'summary' not in updates:
                updates['summary'] = data.get('description')
            if 'owner' in data or 'owner_id' in data:
                owner = data.get('owner') or data.get('owner_id')
                owner_id = _resolve_handle_to_user_id(db_manager, owner, author_id=g.api_key_info.user_id) if owner else None
                updates['owner_id'] = owner_id or owner
            if 'counterparties' in data or 'participants' in data:
                raw_cp = data.get('counterparties')
                if raw_cp is None:
                    raw_cp = data.get('participants')
                if isinstance(raw_cp, str):
                    raw_cp = [p.strip() for p in re.split(r"[,;]", raw_cp) if p.strip()]
                counterparties = []
                for cp in raw_cp or []:
                    cp_id = _resolve_handle_to_user_id(db_manager, cp, author_id=g.api_key_info.user_id)
                    if cp_id:
                        counterparties.append(cp_id)
                updates['counterparties'] = counterparties

            if 'status' in updates and updates.get('status') is not None:
                status_clean = str(updates.get('status')).strip().lower()
                if status_clean not in CONTRACT_STATUSES:
                    return jsonify({'error': 'Invalid status', 'allowed': list(CONTRACT_STATUSES)}), 400
                updates['status'] = status_clean

            if not updates:
                return jsonify({'error': 'No updates provided'}), 400

            admin_user_id = db_manager.get_instance_owner_user_id() if db_manager else None
            try:
                contract = contract_manager.update_contract(
                    contract_id,
                    updates,
                    actor_id=g.api_key_info.user_id,
                    admin_user_id=admin_user_id,
                )
            except PermissionError:
                return jsonify({'error': 'Not authorized'}), 403

            if not contract:
                return jsonify({'error': 'Not found'}), 404

            return jsonify({'contract': contract})
        except Exception as e:
            logger.error(f"Update contract failed: {e}")
            return jsonify({'error': 'Internal server error'}), 500

    # ------------------------------------------------------------------ #
    #  Signals (structured memory)                                       #
    # ------------------------------------------------------------------ #

    @api.route('/signals', methods=['GET'])
    @require_auth(Permission.READ_FEED)
    def list_signals_api():
        """List signals (optional filters)."""
        try:
            signal_manager = current_app.config.get('SIGNAL_MANAGER')
            db_manager = current_app.config.get('DB_MANAGER')
            if not signal_manager:
                return jsonify({'error': 'Signal manager unavailable'}), 500
            status = request.args.get('status') or None
            signal_type = request.args.get('type') or request.args.get('signal_type') or None
            tag = request.args.get('tag') or None
            limit = int(request.args.get('limit', 50))
            signals = signal_manager.list_signals(limit=limit, status=status, signal_type=signal_type, tag=tag)

            admin_user_id = db_manager.get_instance_owner_user_id() if db_manager else None
            filtered = []
            for sig in signals:
                visibility = (sig.get('visibility') or 'network').lower()
                if visibility in ('public', 'network'):
                    filtered.append(sig)
                    continue
                if g.api_key_info and (sig.get('owner_id') == g.api_key_info.user_id or sig.get('created_by') == g.api_key_info.user_id):
                    filtered.append(sig)
                    continue
                if admin_user_id and g.api_key_info and g.api_key_info.user_id == admin_user_id:
                    filtered.append(sig)
            return jsonify({'signals': filtered, 'count': len(filtered)})
        except Exception as e:
            logger.error(f"List signals failed: {e}")
            return jsonify({'error': 'Internal server error'}), 500

    @api.route('/signals/<signal_id>', methods=['GET'])
    @require_auth(Permission.READ_FEED)
    def get_signal_api(signal_id):
        """Get a single signal by ID."""
        try:
            signal_manager = current_app.config.get('SIGNAL_MANAGER')
            db_manager = current_app.config.get('DB_MANAGER')
            if not signal_manager:
                return jsonify({'error': 'Signal manager unavailable'}), 500
            sig = signal_manager.get_signal(signal_id)
            if not sig:
                return jsonify({'error': 'Not found'}), 404
            visibility = (sig.get('visibility') or 'network').lower()
            if visibility not in ('public', 'network'):
                admin_user_id = db_manager.get_instance_owner_user_id() if db_manager else None
                if not g.api_key_info or g.api_key_info.user_id not in (sig.get('owner_id'), sig.get('created_by'), admin_user_id):
                    return jsonify({'error': 'Not authorized'}), 403
            return jsonify({'signal': sig})
        except Exception as e:
            logger.error(f"Get signal failed: {e}")
            return jsonify({'error': 'Internal server error'}), 500

    @api.route('/signals', methods=['POST'])
    @require_auth(Permission.WRITE_FEED)
    def create_signal_api():
        """Create a new signal."""
        try:
            db_manager, _, _, _, _, _, _, _, _, _, p2p_manager = _get_app_components_any(current_app)
            signal_manager = current_app.config.get('SIGNAL_MANAGER')
            if not signal_manager:
                return jsonify({'error': 'Signal manager unavailable'}), 500

            data = request.get_json() or {}
            title = (data.get('title') or '').strip()
            if not title:
                return jsonify({'error': 'title required'}), 400

            signal_id = (data.get('id') or data.get('signal_id') or '').strip()
            if not signal_id:
                signal_id = f"signal_{secrets.token_hex(8)}"

            signal_type = (data.get('type') or data.get('signal_type') or 'signal').strip()
            summary = (data.get('summary') or '').strip() or None
            status = data.get('status') or None
            confidence = data.get('confidence')
            tags = data.get('tags') or []
            if isinstance(tags, str):
                tags = [t.strip() for t in tags.split(',') if t.strip()]
            notes = (data.get('notes') or '').strip() or None
            visibility = (data.get('visibility') or 'network').strip().lower()
            source_type = data.get('source_type') or 'api'
            source_id = data.get('source_id') or None

            owner_id = None
            owner = data.get('owner') or data.get('owner_id')
            if owner:
                owner_id = _resolve_handle_to_user_id(db_manager, owner, author_id=g.api_key_info.user_id)
            if not owner_id:
                owner_id = g.api_key_info.user_id

            data_payload = data.get('data')
            if isinstance(data_payload, str):
                try:
                    data_payload = json.loads(data_payload)
                except Exception:
                    data_payload = {'_raw': data_payload}

            ttl_mode = data.get('ttl_mode')
            ttl_seconds = data.get('ttl_seconds')
            expires_at = data.get('expires_at')
            ttl_raw = data.get('ttl')
            if ttl_raw and not (ttl_seconds or ttl_mode or expires_at):
                ttl_token = str(ttl_raw).strip().lower()
                if ttl_token in ('none', 'no_expiry', 'immortal'):
                    ttl_mode = 'no_expiry'
                else:
                    from ..core.signals import _parse_ttl, _parse_dt
                    parsed = _parse_ttl(ttl_token)
                    if parsed:
                        ttl_seconds = parsed
                    else:
                        dt = _parse_dt(ttl_token)
                        if dt:
                            expires_at = dt.isoformat()

            origin_peer = None
            try:
                if p2p_manager:
                    origin_peer = p2p_manager.get_peer_id()
            except Exception:
                origin_peer = None

            sig = signal_manager.upsert_signal(
                signal_id=signal_id,
                signal_type=signal_type,
                title=title,
                summary=summary,
                status=status,
                confidence=confidence,
                tags=tags,
                data=data_payload,
                notes=notes,
                owner_id=owner_id,
                created_by=g.api_key_info.user_id,
                visibility=visibility,
                origin_peer=origin_peer,
                source_type=source_type,
                source_id=source_id,
                expires_at=expires_at,
                ttl_seconds=ttl_seconds,
                ttl_mode=ttl_mode,
                actor_id=g.api_key_info.user_id,
            )
            if not sig:
                return jsonify({'error': 'Failed to create signal'}), 500
            return jsonify({'signal': sig}), 201
        except Exception as e:
            logger.error(f"Create signal failed: {e}")
            return jsonify({'error': 'Internal server error'}), 500

    @api.route('/signals/<signal_id>', methods=['PATCH'])
    @require_auth(Permission.WRITE_FEED)
    def update_signal_api(signal_id):
        """Update a signal (or submit a proposal if not owner)."""
        try:
            db_manager = current_app.config.get('DB_MANAGER')
            signal_manager = current_app.config.get('SIGNAL_MANAGER')
            if not signal_manager:
                return jsonify({'error': 'Signal manager unavailable'}), 500
            data = request.get_json() or {}
            updates = {}

            for key in ('title', 'summary', 'status', 'confidence', 'notes'):
                if key in data:
                    updates[key] = data.get(key)

            if 'tags' in data:
                tags = data.get('tags') or []
                if isinstance(tags, str):
                    tags = [t.strip() for t in tags.split(',') if t.strip()]
                updates['tags'] = tags

            if 'data' in data:
                payload = data.get('data')
                if isinstance(payload, str):
                    try:
                        payload = json.loads(payload)
                    except Exception:
                        payload = {'_raw': payload}
                updates['data'] = payload

            if 'owner' in data or 'owner_id' in data:
                owner = data.get('owner') or data.get('owner_id')
                if owner:
                    owner_text = str(owner)
                    owner_id = _resolve_handle_to_user_id(db_manager, owner_text, author_id=g.api_key_info.user_id)
                    updates['owner_id'] = owner_id or owner_text

            ttl_mode = data.get('ttl_mode')
            ttl_seconds = data.get('ttl_seconds')
            expires_at = data.get('expires_at')
            ttl_raw = data.get('ttl')
            if ttl_raw and not (ttl_seconds or ttl_mode or expires_at):
                ttl_token = str(ttl_raw).strip().lower()
                if ttl_token in ('none', 'no_expiry', 'immortal'):
                    ttl_mode = 'no_expiry'
                else:
                    from ..core.signals import _parse_ttl, _parse_dt
                    parsed = _parse_ttl(ttl_token)
                    if parsed:
                        ttl_seconds = parsed
                    else:
                        dt = _parse_dt(ttl_token)
                        if dt:
                            expires_at = dt.isoformat()

            if ttl_mode is not None or ttl_seconds is not None or expires_at is not None:
                updates['ttl_mode'] = ttl_mode
                updates['ttl_seconds'] = ttl_seconds
                updates['expires_at'] = expires_at

            if not updates:
                sig = signal_manager.get_signal(signal_id)
                if not sig:
                    return jsonify({'error': 'Not found'}), 404
                return jsonify({'signal': sig})

            result = signal_manager.update_signal(signal_id, updates, actor_id=g.api_key_info.user_id)
            if not result:
                return jsonify({'error': 'Not found'}), 404
            if isinstance(result, dict) and result.get('proposal_version'):
                return jsonify({'proposal': result}), 202
            return jsonify({'signal': result})
        except Exception as e:
            logger.error(f"Update signal failed: {e}")
            return jsonify({'error': 'Internal server error'}), 500

    @api.route('/signals/<signal_id>/lock', methods=['POST'])
    @require_auth(Permission.WRITE_FEED)
    def lock_signal_api(signal_id):
        """Lock or unlock a signal."""
        try:
            signal_manager = current_app.config.get('SIGNAL_MANAGER')
            if not signal_manager:
                return jsonify({'error': 'Signal manager unavailable'}), 500
            data = request.get_json() or {}
            locked = True
            if 'locked' in data:
                locked = bool(data.get('locked'))
            sig = signal_manager.lock_signal(signal_id, actor_id=g.api_key_info.user_id, locked=locked)
            if not sig:
                return jsonify({'error': 'Not found or not authorized'}), 404
            return jsonify({'signal': sig})
        except Exception as e:
            logger.error(f"Lock signal failed: {e}")
            return jsonify({'error': 'Internal server error'}), 500

    @api.route('/signals/<signal_id>/proposals/<int:version>', methods=['POST'])
    @require_auth(Permission.WRITE_FEED)
    def apply_signal_proposal_api(signal_id, version):
        """Accept or reject a pending signal proposal."""
        try:
            signal_manager = current_app.config.get('SIGNAL_MANAGER')
            if not signal_manager:
                return jsonify({'error': 'Signal manager unavailable'}), 500
            data = request.get_json() or {}
            accept = data.get('accept', True)
            sig = signal_manager.apply_proposal(signal_id, version, actor_id=g.api_key_info.user_id, accept=bool(accept))
            if not sig:
                return jsonify({'error': 'Not found or not authorized'}), 404
            return jsonify({'signal': sig})
        except Exception as e:
            logger.error(f"Apply signal proposal failed: {e}")
            return jsonify({'error': 'Internal server error'}), 500

    @api.route('/signals/<signal_id>/proposals', methods=['GET'])
    @require_auth(Permission.READ_FEED)
    def list_signal_proposals_api(signal_id):
        """List pending proposals for a signal (owner/admin only)."""
        try:
            signal_manager = current_app.config.get('SIGNAL_MANAGER')
            db_manager = current_app.config.get('DB_MANAGER')
            if not signal_manager:
                return jsonify({'error': 'Signal manager unavailable'}), 500
            sig = signal_manager.get_signal(signal_id)
            if not sig:
                return jsonify({'error': 'Not found'}), 404
            admin_user_id = db_manager.get_instance_owner_user_id() if db_manager else None
            if g.api_key_info.user_id not in (sig.get('owner_id'), sig.get('created_by'), admin_user_id):
                return jsonify({'error': 'Not authorized'}), 403
            proposals = signal_manager.list_proposals(signal_id, status='pending')
            return jsonify({'proposals': proposals, 'count': len(proposals)})
        except Exception as e:
            logger.error(f"List signal proposals failed: {e}")
            return jsonify({'error': 'Internal server error'}), 500

    # ------------------------------------------------------------------ #
    #  Circles (structured deliberations)                                #
    # ------------------------------------------------------------------ #

    @api.route('/circles', methods=['GET'])
    @require_auth(Permission.READ_FEED)
    def list_circles_api():
        """List recent circles (optional filters)."""
        try:
            circle_manager = current_app.config.get('CIRCLE_MANAGER')
            if not circle_manager:
                return jsonify({'error': 'Circle manager unavailable'}), 500
            limit = int(request.args.get('limit', 50))
            source_type = request.args.get('source_type')
            channel_id = request.args.get('channel_id')
            circles = circle_manager.list_circles(limit=limit, source_type=source_type, channel_id=channel_id)
            payload = []
            for c in circles:
                item = c.to_dict()
                item['entries_count'] = circle_manager.count_entries(c.id)
                payload.append(item)
            return jsonify({'circles': payload, 'count': len(payload)})
        except Exception as e:
            logger.error(f"List circles failed: {e}")
            return jsonify({'error': 'Internal server error'}), 500

    @api.route('/circles/<circle_id>', methods=['GET'])
    @require_auth(Permission.READ_FEED)
    def get_circle_api(circle_id):
        """Get a circle and optional entries."""
        try:
            circle_manager = current_app.config.get('CIRCLE_MANAGER')
            if not circle_manager:
                return jsonify({'error': 'Circle manager unavailable'}), 500
            circle = circle_manager.get_circle(circle_id)
            if not circle:
                return jsonify({'error': 'Not found'}), 404
            include_entries = request.args.get('include_entries', '').strip().lower() in ('1', 'true', 'yes')
            resp = {'circle': circle.to_dict()}
            if include_entries:
                resp['entries'] = circle_manager.list_entries(circle_id)
            return jsonify(resp)
        except Exception as e:
            logger.error(f"Get circle failed: {e}")
            return jsonify({'error': 'Internal server error'}), 500

    @api.route('/circles/<circle_id>/entries', methods=['GET'])
    @require_auth(Permission.READ_FEED)
    def list_circle_entries_api(circle_id):
        """List entries for a circle."""
        try:
            circle_manager = current_app.config.get('CIRCLE_MANAGER')
            if not circle_manager:
                return jsonify({'error': 'Circle manager unavailable'}), 500
            entries = circle_manager.list_entries(circle_id)
            return jsonify({'entries': entries, 'count': len(entries)})
        except Exception as e:
            logger.error(f"List circle entries failed: {e}")
            return jsonify({'error': 'Internal server error'}), 500

    @api.route('/circles/<circle_id>/entries', methods=['POST'])
    @require_auth(Permission.WRITE_FEED)
    def add_circle_entry_api(circle_id):
        """Add an entry to a circle."""
        try:
            db_manager, _, _, _, _, _, _, _, profile_manager, _, p2p_manager = _get_app_components_any(current_app)
            circle_manager = current_app.config.get('CIRCLE_MANAGER')
            if not circle_manager:
                return jsonify({'error': 'Circle manager unavailable'}), 500
            data = request.get_json() or {}
            content = (data.get('content') or '').strip()
            entry_type = (data.get('entry_type') or '').strip().lower()
            if not content:
                return jsonify({'error': 'content required'}), 400
            entry, error = circle_manager.add_entry(
                circle_id=circle_id,
                user_id=g.api_key_info.user_id,
                entry_type=entry_type,
                content=content,
                admin_user_id=db_manager.get_instance_owner_user_id(),
                return_error=True,
            )
            if not entry:
                if error:
                    status = int(error.get('status') or 403)
                    payload = {
                        'error': error.get('message') or 'Not authorized or invalid',
                        'code': error.get('code') or 'circle_entry_error',
                    }
                    for key in ('limit', 'count', 'round_number', 'phase', 'expected_phase', 'suggestions'):
                        if key in error:
                            payload[key] = error[key]
                    return jsonify(payload), status
                return jsonify({'error': 'Not authorized or invalid'}), 403

            circle = circle_manager.get_circle(circle_id)
            if circle and circle.visibility == 'network' and p2p_manager and p2p_manager.is_running():
                try:
                    display_name = None
                    if profile_manager:
                        prof = profile_manager.get_profile(g.api_key_info.user_id)
                        if prof:
                            display_name = prof.display_name or prof.username
                    p2p_manager.broadcast_interaction(
                        item_id=entry['id'],
                        user_id=g.api_key_info.user_id,
                        action='circle_entry',
                        item_type='circle_entry',
                        display_name=display_name,
                        extra={'circle_id': circle_id, 'entry': entry},
                    )
                except Exception as bcast_err:
                    logger.warning(f"Failed to broadcast circle entry: {bcast_err}")

            return jsonify({'entry': entry}), 201
        except Exception as e:
            logger.error(f"Add circle entry failed: {e}")
            return jsonify({'error': 'Internal server error'}), 500

    @api.route('/circles/<circle_id>/entries/<entry_id>', methods=['PATCH'])
    @require_auth(Permission.WRITE_FEED)
    def update_circle_entry_api(circle_id, entry_id):
        """Update a circle entry within the edit window."""
        try:
            db_manager, _, _, _, _, _, _, _, profile_manager, _, p2p_manager = _get_app_components_any(current_app)
            circle_manager = current_app.config.get('CIRCLE_MANAGER')
            if not circle_manager:
                return jsonify({'error': 'Circle manager unavailable'}), 500
            data = request.get_json() or {}
            content = (data.get('content') or '').strip()
            if not content:
                return jsonify({'error': 'content required'}), 400
            entry = circle_manager.update_entry(
                circle_id=circle_id,
                entry_id=entry_id,
                user_id=g.api_key_info.user_id,
                content=content,
                admin_user_id=db_manager.get_instance_owner_user_id(),
            )
            if not entry:
                return jsonify({'error': 'Not authorized or edit window expired'}), 403

            circle = circle_manager.get_circle(circle_id)
            if circle and circle.visibility == 'network' and p2p_manager and p2p_manager.is_running():
                try:
                    display_name = None
                    if profile_manager:
                        prof = profile_manager.get_profile(g.api_key_info.user_id)
                        if prof:
                            display_name = prof.display_name or prof.username
                    p2p_manager.broadcast_interaction(
                        item_id=entry['id'],
                        user_id=g.api_key_info.user_id,
                        action='circle_entry',
                        item_type='circle_entry',
                        display_name=display_name,
                        extra={'circle_id': circle_id, 'entry': entry},
                    )
                except Exception as bcast_err:
                    logger.warning(f"Failed to broadcast circle entry update: {bcast_err}")

            return jsonify({'entry': entry})
        except Exception as e:
            logger.error(f"Update circle entry failed: {e}")
            return jsonify({'error': 'Internal server error'}), 500

    @api.route('/circles/<circle_id>/phase', methods=['PATCH'])
    @require_auth(Permission.WRITE_FEED)
    def update_circle_phase_api(circle_id):
        """Update circle phase (facilitator/admin)."""
        try:
            db_manager, _, _, _, _, _, _, _, profile_manager, _, p2p_manager = _get_app_components_any(current_app)
            circle_manager = current_app.config.get('CIRCLE_MANAGER')
            if not circle_manager:
                return jsonify({'error': 'Circle manager unavailable'}), 500
            data = request.get_json() or {}
            phase = (data.get('phase') or '').strip().lower()
            circle = circle_manager.update_phase(
                circle_id=circle_id,
                new_phase=phase,
                actor_id=g.api_key_info.user_id,
                admin_user_id=db_manager.get_instance_owner_user_id(),
            )
            if not circle:
                return jsonify({'error': 'Not authorized or invalid phase'}), 403

            if circle.visibility == 'network' and p2p_manager and p2p_manager.is_running():
                try:
                    display_name = None
                    if profile_manager:
                        prof = profile_manager.get_profile(g.api_key_info.user_id)
                        if prof:
                            display_name = prof.display_name or prof.username
                    p2p_manager.broadcast_interaction(
                        item_id=circle.id,
                        user_id=g.api_key_info.user_id,
                        action='circle_phase',
                        item_type='circle',
                        display_name=display_name,
                        extra={
                            'circle_id': circle.id,
                            'phase': circle.phase,
                            'updated_at': circle.updated_at.isoformat(),
                            'round_number': circle.round_number,
                        },
                    )
                except Exception as bcast_err:
                    logger.warning(f"Failed to broadcast circle phase: {bcast_err}")

            return jsonify({'circle': circle.to_dict()})
        except Exception as e:
            logger.error(f"Update circle phase failed: {e}")
            return jsonify({'error': 'Internal server error'}), 500

    @api.route('/circles/<circle_id>/vote', methods=['POST'])
    @require_auth(Permission.WRITE_FEED)
    def vote_circle_api(circle_id):
        """Vote on a circle decision."""
        try:
            _, _, _, _, _, _, _, _, profile_manager, _, p2p_manager = _get_app_components_any(current_app)
            circle_manager = current_app.config.get('CIRCLE_MANAGER')
            if not circle_manager:
                return jsonify({'error': 'Circle manager unavailable'}), 500
            data = request.get_json() or {}
            option_index = data.get('option_index')
            if option_index is None:
                return jsonify({'error': 'option_index required'}), 400
            vote = circle_manager.record_vote(circle_id, g.api_key_info.user_id, int(option_index))
            if not vote:
                return jsonify({'error': 'Not authorized or invalid vote'}), 403

            circle = circle_manager.get_circle(circle_id)
            if circle and circle.visibility == 'network' and p2p_manager and p2p_manager.is_running():
                try:
                    display_name = None
                    if profile_manager:
                        prof = profile_manager.get_profile(g.api_key_info.user_id)
                        if prof:
                            display_name = prof.display_name or prof.username
                    p2p_manager.broadcast_interaction(
                        item_id=circle.id,
                        user_id=g.api_key_info.user_id,
                        action='circle_vote',
                        item_type='circle',
                        display_name=display_name,
                        extra={'circle_id': circle.id, 'option_index': int(option_index), 'created_at': datetime.now(timezone.utc).isoformat()},
                    )
                except Exception as bcast_err:
                    logger.warning(f"Failed to broadcast circle vote: {bcast_err}")

            return jsonify({'vote': vote})
        except Exception as e:
            logger.error(f"Vote circle failed: {e}")
            return jsonify({'error': 'Internal server error'}), 500

    # Post access control endpoints
    @api.route('/posts/<post_id>/access', methods=['DELETE'])
    @require_auth(Permission.WRITE_FEED)
    def revoke_post_access(post_id):
        """Revoke a user's access to a post. Caller must be the post author or instance admin."""
        db_manager, _, _, _, _, _, feed_manager, _, _, _, _ = get_app_components(current_app)
        if not db_manager or not feed_manager:
            return jsonify({'error': 'Service unavailable'}), 503
        try:
            data = request.get_json()
            if not data:
                return jsonify({'error': 'JSON data required'}), 400
            
            user_id = data.get('user_id')
            if not user_id:
                return jsonify({'error': 'user_id required'}), 400

            # Ownership guard: only the post author or instance admin may revoke access.
            post = feed_manager.get_post(post_id)
            if not post:
                return jsonify({'error': 'Post not found'}), 404
            owner_id = db_manager.get_instance_owner_user_id()
            is_admin = owner_id is not None and owner_id == g.api_key_info.user_id
            if post.author_id != g.api_key_info.user_id and not is_admin:
                return jsonify({'error': 'Only the post author or admin may revoke access'}), 403
            
            success = db_manager.revoke_post_access(post_id, user_id)
            if success:
                return jsonify({
                    'message': f'Access revoked for {user_id}',
                    'post_id': post_id
                })
            else:
                return jsonify({'error': 'Failed to revoke access'}), 500
                
        except Exception as e:
            logger.error(f"Failed to revoke post access: {e}")
            return jsonify({'error': 'Internal server error'}), 500
    
    @api.route('/posts/<post_id>/access', methods=['GET'])
    @require_auth(Permission.READ_FEED)
    def get_post_recipients(post_id):
        """List users who have access to a post. Caller must be the post author or instance admin."""
        db_manager, _, _, _, _, _, feed_manager, _, _, _, _ = get_app_components(current_app)
        if not db_manager or not feed_manager:
            return jsonify({'error': 'Service unavailable'}), 503
        try:
            # Ownership guard: only the post author or instance admin may enumerate recipients.
            post = feed_manager.get_post(post_id)
            if not post:
                return jsonify({'error': 'Post not found'}), 404
            owner_id = db_manager.get_instance_owner_user_id()
            is_admin = owner_id is not None and owner_id == g.api_key_info.user_id
            if post.author_id != g.api_key_info.user_id and not is_admin:
                return jsonify({'error': 'Only the post author or admin may view access list'}), 403

            recipients = db_manager.get_post_recipients(post_id)
            return jsonify({
                'post_id': post_id,
                'recipients': recipients,
                'count': len(recipients)
            })
        except Exception as e:
            logger.error(f"Failed to get post recipients: {e}")
            return jsonify({'error': 'Internal server error'}), 500

    return api
