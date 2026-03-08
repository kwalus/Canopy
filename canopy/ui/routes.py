"""
Web UI routes for Canopy.

Provides web interface for managing keys, messages, trust scores,
and all other Canopy functionality through a clean web UI.

Author: Konrad Walus (architecture, design, and direction)
Project: Canopy - Local Mesh Communication
License: Apache 2.0
Development: AI-assisted implementation (Claude, Codex, GitHub Copilot, Cursor IDE, Ollama)
"""

import logging
import os
import json
import secrets
import base64
import time
import re
import socket
import ipaddress
import sqlite3
import tempfile
import html as html_lib
import threading
import xml.etree.ElementTree as ET
from functools import wraps
from flask import Blueprint, render_template, request, jsonify, current_app, session, redirect, url_for, flash, send_file, Response
from datetime import datetime, timezone, timedelta
from werkzeug.utils import secure_filename
from typing import Any, Optional, cast
from urllib.parse import urlparse, parse_qs, urlencode, quote_plus
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

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
from ..security.api_keys import Permission, ApiKeyManager
from ..security.file_access import evaluate_file_access
from ..security.csrf import generate_csrf_token, validate_csrf_request
from ..core.profile import (
    DEFAULT_AGENT_DIRECTIVE_PRESETS,
    MAX_AGENT_DIRECTIVES_LENGTH,
    get_default_agent_directives,
    normalize_agent_directives,
)
from ..core.agent_heartbeat import build_agent_heartbeat_snapshot
from ..core.agent_presence import (
    get_agent_presence_records,
    build_agent_presence_payload,
)
from ..core.file_preview import build_file_preview
from ..core.messaging import (
    build_dm_preview,
    compute_group_id,
    filter_local_dm_targets,
)
from ..network.routing import (
    encrypt_key_for_peer,
    encode_channel_key_material,
)

logger = logging.getLogger(__name__)
_CUSTOM_EMOJI_LOCK = threading.Lock()


def _get_app_components_any(app: Any) -> tuple[Any, ...]:
    return cast(tuple[Any, ...], get_app_components(app))


def _is_private_ip(host: str) -> bool:
    """Return True if host is an RFC-1918 private IP address."""
    import ipaddress
    try:
        addr = ipaddress.ip_address(host)
        return addr.is_private
    except ValueError:
        return False


def _resolve_p2p_stream(stream_id: str, db_manager: Any, p2p_manager: Any) -> Optional[dict[str, Any]]:
    """Find the origin peer for a stream not stored locally and return a remote playback URL.

    Scans channel_messages for a stream attachment matching stream_id, then looks up
    the origin peer's HTTP address, preferring RFC-1918 private IPs for LAN routing.
    Returns a dict with 'playback_url' on success, or None if not found.
    """
    import json as _json
    try:
        with db_manager.get_connection() as conn:
            rows = conn.execute(
                "SELECT origin_peer, attachments FROM channel_messages "
                "WHERE attachments IS NOT NULL AND attachments != '[]' "
                "ORDER BY created_at DESC LIMIT 200"
            ).fetchall()

        origin_peer: Optional[str] = None
        host_addrs: list[str] = []
        for row in rows:
            try:
                atts = _json.loads(row['attachments'] if hasattr(row, 'keys') else row[1])
            except Exception:
                continue
            for att in atts:
                if not isinstance(att, dict):
                    continue
                if str(att.get('stream_id') or '') == stream_id:
                    origin_peer = str(row['origin_peer'] if hasattr(row, 'keys') else row[0] or '')
                    host_addrs = att.get('host_addrs') or []
                    break
            if origin_peer:
                break

        if not origin_peer:
            return None

        # Build candidate URLs: prefer host_addrs embedded in the stream card attachment,
        # fall back to peer_endpoints from the identity manager.
        candidates: list[str] = list(host_addrs)
        if p2p_manager and hasattr(p2p_manager, 'identity_manager'):
            endpoints = p2p_manager.identity_manager.peer_endpoints.get(origin_peer, [])
            for ep in endpoints:
                # ep is typically "host:port" (ws/tcp), convert to http
                try:
                    parts = str(ep).rsplit(':', 1)
                    host = parts[0].lstrip('/')
                    port = int(parts[1]) if len(parts) > 1 else 7771
                    http_port = port - 1 if port > 7770 else 7770
                    candidates.append(f"http://{host}:{http_port}")
                except Exception:
                    continue

        if not candidates:
            return None

        # Sort: private IPs first, then others
        def _sort_key(url: str) -> int:
            try:
                from urllib.parse import urlparse as _up
                host = _up(url).hostname or ''
                return 0 if _is_private_ip(host) else 1
            except Exception:
                return 2

        candidates.sort(key=_sort_key)
        base_url = candidates[0].rstrip('/')
        remote_url = f"{base_url}/api/v1/streams/{stream_id}/manifest.m3u8"
        # Return a local proxy URL so the browser never has to reach the remote IP directly
        # (Chrome blocks cross-origin requests to private IPs from localhost pages)
        proxy_url = f"/api/v1/stream-proxy/{stream_id}/manifest.m3u8"
        logger.info(f"P2P stream resolved: stream_id={stream_id} origin_peer={origin_peer} remote={remote_url} proxy={proxy_url}")
        return {'playback_url': proxy_url, 'origin_peer': origin_peer, 'remote_base': base_url}
    except Exception as e:
        logger.warning(f"_resolve_p2p_stream error: {e}")
        return None


def create_ui_blueprint() -> Blueprint:
    """Create and configure the UI blueprint."""
    ui = Blueprint('ui', __name__, template_folder='templates', static_folder='static')

    @ui.after_request
    def _inject_csrf_cookie(response):
        """Ensure every response from an authenticated session carries a CSRF token.

        The token is stored in the Flask session (server-side) and also exposed via
        a <meta name="csrf-token"> tag in base.html so JavaScript can read it.
        This hook simply ensures the session token is always generated and fresh.
        """
        if _is_authenticated():
            generate_csrf_token()
        return response

    @ui.context_processor
    def _csrf_context():
        """Inject csrf_token() into all Jinja2 templates."""
        return {'csrf_token': generate_csrf_token}

    # --- Authentication helpers ---
    
    def _hash_password(password: str) -> str:
        """Hash a password using bcrypt (new) or SHA-256 (legacy compatibility)."""
        from ..security.password import hash_password
        return hash_password(password)

    def _verify_password(password: str, password_hash: str) -> bool:
        """Verify password against hash, supporting both bcrypt and legacy SHA256."""
        from ..security.password import verify_password, is_legacy_hash, verify_legacy_password

        # Check if it's a legacy hash
        if is_legacy_hash(password_hash):
            salt = current_app.config.get('SECRET_KEY')
            if salt and verify_legacy_password(password, password_hash, salt):
                # Legacy password verified - migration happens at the call site
                return True
            return False

        # New bcrypt hash
        return verify_password(password, password_hash)
    
    def _is_authenticated() -> bool:
        """Check if the current session has a logged-in user."""
        return session.get('authenticated', False) and 'user_id' in session
    
    def _generate_user_keypair():
        """Generate Ed25519 + X25519 keypair for a new user."""
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
        from cryptography.hazmat.primitives import serialization
        import base58
        
        # Generate Ed25519 signing keypair
        ed25519_private = Ed25519PrivateKey.generate()
        ed25519_public = ed25519_private.public_key()
        
        ed25519_priv_bytes = ed25519_private.private_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PrivateFormat.Raw,
            encryption_algorithm=serialization.NoEncryption()
        )
        ed25519_pub_bytes = ed25519_public.public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw
        )
        
        # Generate X25519 key exchange keypair
        x25519_private = X25519PrivateKey.generate()
        x25519_public = x25519_private.public_key()
        
        x25519_priv_bytes = x25519_private.private_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PrivateFormat.Raw,
            encryption_algorithm=serialization.NoEncryption()
        )
        x25519_pub_bytes = x25519_public.public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw
        )
        
        return {
            'ed25519_public': base58.b58encode(ed25519_pub_bytes).decode(),
            'ed25519_private': base58.b58encode(ed25519_priv_bytes).decode(),
            'x25519_public': base58.b58encode(x25519_pub_bytes).decode(),
            'x25519_private': base58.b58encode(x25519_priv_bytes).decode(),
        }
    
    def require_login(f):
        """Decorator to require login for a route."""
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if not _is_authenticated():
                return redirect(url_for('ui.login'))
            return f(*args, **kwargs)
        return decorated_function

    # --- CSRF protection ---

    _CSRF_EXEMPT_PATHS = {'/login', '/register', '/setup'}

    @ui.before_request
    def _enforce_csrf():
        if request.method in ('GET', 'HEAD', 'OPTIONS'):
            return
        if request.path in _CSRF_EXEMPT_PATHS:
            return
        if not _is_authenticated():
            return
        validate_csrf_request()

    def _custom_emoji_dir() -> str:
        """Return the filesystem directory for custom emojis, creating it if needed."""
        base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', 'data', 'custom_emojis'))
        os.makedirs(base_dir, exist_ok=True)
        return base_dir

    def _custom_emoji_index_path() -> str:
        return os.path.join(_custom_emoji_dir(), 'index.json')

    def _load_custom_emojis() -> list:
        """Load custom emojis from disk, skipping missing files."""
        index_path = _custom_emoji_index_path()
        if not os.path.exists(index_path):
            return []
        try:
            with open(index_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except Exception:
            return []
        if not isinstance(data, list):
            return []
        base_dir = _custom_emoji_dir()
        valid = []
        for entry in data:
            filename = entry.get('filename')
            if not filename:
                continue
            file_path = os.path.abspath(os.path.join(base_dir, filename))
            # Reject entries that escape the emoji directory
            if not file_path.startswith(base_dir):
                continue
            if not os.path.isfile(file_path):
                continue
            valid.append(entry)
        return valid

    def _save_custom_emojis(entries: list) -> None:
        index_path = _custom_emoji_index_path()
        with open(index_path, 'w', encoding='utf-8') as f:
            json.dump(entries, f, indent=2)

    def _slugify(value: str) -> str:
        value = value.strip().lower()
        value = re.sub(r'[^a-z0-9_-]+', '-', value)
        value = re.sub(r'-{2,}', '-', value).strip('-')
        return value or 'emoji'

    def _compute_group_id(member_ids: list) -> str:
        """Create a stable group ID from a set of member IDs."""
        return compute_group_id(member_ids)

    @ui.app_context_processor
    def inject_canopy_version():
        """Inject app version for sidebar and anywhere else. Single source: canopy.__version__."""
        try:
            from canopy import __version__
            return {'canopy_version': __version__}
        except Exception:
            return {'canopy_version': '0.0.0'}

    @ui.app_context_processor
    def inject_sidebar_peers():
        """Inject connected peer context and admin context for the sidebar."""
        if not _is_authenticated():
            return {}
        try:
            db_manager, _, trust_manager, _, channel_manager, _, _, _, _, _, p2p_manager = _get_app_components_any(current_app)
            connected_peers = p2p_manager.get_connected_peers() if p2p_manager else []
            peer_profiles = channel_manager.get_all_peer_device_profiles() if channel_manager else {}
            trust_map = {}
            if trust_manager:
                for pid in connected_peers:
                    trust_map[pid] = trust_manager.get_trust_score(pid)
            out = {
                'sidebar_connected_peers': connected_peers,
                'sidebar_peer_profiles': peer_profiles,
                'sidebar_peer_trust': trust_map
            }
            # Admin link and badge (instance owner only)
            owner_id = db_manager.get_instance_owner_user_id()
            if owner_id and session.get('user_id') == owner_id:
                out['is_admin'] = True
                out['admin_pending_count'] = db_manager.get_pending_approval_count()
                out['show_claim_admin'] = False
            else:
                out['is_admin'] = False
                out['admin_pending_count'] = 0
                # Show "Claim admin" when no owner, or when recovery secret is set (take-over)
                out['show_claim_admin'] = (not owner_id) or bool(os.environ.get('CANOPY_ADMIN_CLAIM_SECRET', '').strip())
            return out
        except Exception as e:
            logger.warning(f"Failed to build sidebar peer list: {e}")
            return {}
    
    # --- Auth routes ---
    
    @ui.route('/login', methods=['GET', 'POST'])
    def login():
        """Per-user login page and handler."""
        db_manager = current_app.config.get('DB_MANAGER')
        if not db_manager:
            return render_template('error.html', error='Database not initialized')
        
        has_users = db_manager.has_any_registered_users()
        
        if request.method == 'GET':
            # If already logged in, go to dashboard
            if _is_authenticated():
                return redirect(url_for('ui.dashboard'))
            # If no users exist yet, redirect to first-run setup wizard
            if not has_users:
                return redirect(url_for('ui.setup'))
            return render_template('login.html', show_register=False)
        
        # POST - handle login
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        
        if not username or not password:
            return render_template('login.html', show_register=False,
                                 error='Username and password are required')
        
        # Look up user
        user = db_manager.get_user_by_username(username)
        if not user or not user.get('password_hash'):
            logger.warning(f"Failed login attempt for '{username}' from {request.remote_addr}")
            return render_template('login.html', show_register=False,
                                 error='Invalid username or password')
        
        # Verify password
        if not _verify_password(password, user['password_hash']):
            logger.warning(f"Failed login attempt for '{username}' from {request.remote_addr}")
            return render_template('login.html', show_register=False,
                                 error='Invalid username or password')
        
        # Migrate legacy password to bcrypt if needed
        from ..security.password import is_legacy_hash
        if is_legacy_hash(user['password_hash']):
            try:
                new_hash = _hash_password(password)
                with db_manager.get_connection() as conn:
                    conn.execute(
                        "UPDATE users SET password_hash = ? WHERE id = ?",
                        (new_hash, user['id'])
                    )
                    conn.commit()
                logger.info(f"Migrated password hash for user '{username}' to bcrypt")
            except Exception as e:
                logger.error(f"Failed to migrate password hash for user '{username}': {e}")
        
        # Success - set session
        session['authenticated'] = True
        session['user_id'] = user['id']
        session['username'] = user['username']
        session['display_name'] = user.get('display_name') or user['username']
        session.permanent = True
        logger.info(f"User '{username}' logged in from {request.remote_addr}")
        return redirect(url_for('ui.dashboard'))
    
    @ui.route('/register', methods=['GET', 'POST'])
    def register():
        """User registration page and handler."""
        db_manager = current_app.config.get('DB_MANAGER')
        if not db_manager:
            return render_template('error.html', error='Database not initialized')
        
        has_users = db_manager.has_any_registered_users()
        
        if request.method == 'GET':
            if _is_authenticated():
                return redirect(url_for('ui.dashboard'))
            return render_template('login.html', show_register=True, first_user=not has_users)
        
        # POST - handle registration
        username = request.form.get('username', '').strip()
        display_name = request.form.get('display_name', '').strip() or username
        password = request.form.get('password', '')
        password_confirm = request.form.get('password_confirm', '')
        
        # Validate
        if not username or len(username) < 2:
            return render_template('login.html', show_register=True, first_user=not has_users,
                                 error='Username must be at least 2 characters')
        
        # Validate password strength
        from ..security.password import validate_password_strength
        is_valid, error_msg = validate_password_strength(password)
        if not is_valid:
            return render_template('login.html', show_register=True, first_user=not has_users,
                                 error=error_msg)
        
        if password != password_confirm:
            return render_template('login.html', show_register=True, first_user=not has_users,
                                 error='Passwords do not match')
        
        # Check username availability
        existing = db_manager.get_user_by_username(username)
        if existing:
            return render_template('login.html', show_register=True, first_user=not has_users,
                                 error='Username is already taken')
        
        # Generate user ID and crypto keypair
        user_id = f"user_{secrets.token_hex(8)}"
        keypair = _generate_user_keypair()
        pw_hash = _hash_password(password)
        
        # Create user account
        success = db_manager.create_user(
            user_id=user_id,
            username=username,
            public_key=keypair['ed25519_public'],
            password_hash=pw_hash,
            display_name=display_name
        )
        
        if not success:
            return render_template('login.html', show_register=True, first_user=not has_users,
                                 error='Failed to create account. Username may already be taken.')
        
        # Store crypto keypair
        db_manager.store_user_keys(
            user_id=user_id,
            ed25519_pub=keypair['ed25519_public'],
            ed25519_priv=keypair['ed25519_private'],
            x25519_pub=keypair['x25519_public'],
            x25519_priv=keypair['x25519_private']
        )
        
        # Add user to the general channel and all other public channels
        try:
            with db_manager.get_connection() as conn:
                # Always add to 'general'
                conn.execute("""
                    INSERT OR IGNORE INTO channel_members (channel_id, user_id, role)
                    VALUES ('general', ?, 'member')
                """, (user_id,))
                # Also join existing open/public channels so P2P messages are visible.
                # Do not auto-join targeted/restricted channels.
                try:
                    public_channels = conn.execute(
                        "SELECT id FROM channels "
                        "WHERE channel_type = 'public' "
                        "  AND COALESCE(privacy_mode, 'open') = 'open'"
                    ).fetchall()
                except Exception:
                    # Backward compatibility for legacy schemas without privacy_mode.
                    public_channels = conn.execute(
                        "SELECT id FROM channels WHERE channel_type = 'public'"
                    ).fetchall()
                for (ch_id,) in public_channels:
                    conn.execute("""
                        INSERT OR IGNORE INTO channel_members (channel_id, user_id, role)
                        VALUES (?, ?, 'member')
                    """, (ch_id, user_id))
                conn.commit()
                logger.info(f"Added new user {user_id} to {len(public_channels)} public channels + general")
        except Exception as e:
            logger.warning(f"Could not add user to channels: {e}")
        
        logger.info(f"New user registered: '{username}' ({user_id}) with crypto keypair")
        
        # Auto-login after registration
        session['authenticated'] = True
        session['user_id'] = user_id
        session['username'] = username
        session['display_name'] = display_name
        session.permanent = True
        
        return redirect(url_for('ui.dashboard'))

    @ui.route('/setup', methods=['GET', 'POST'])
    def setup():
        """First-run wizard: create admin/first user and optionally connect a peer."""
        db_manager = current_app.config.get('DB_MANAGER')
        if not db_manager:
            return render_template('error.html', error='Database not initialized')
        has_users = db_manager.has_any_registered_users()
        if request.method == 'GET':
            if _is_authenticated():
                return redirect(url_for('ui.dashboard'))
            if has_users:
                return redirect(url_for('ui.login'))
            return render_template('setup.html')
        # Setup is first-run only; block repeated POSTs once any account exists.
        if has_users:
            logger.warning("Rejected /setup POST after initial setup completed")
            return redirect(url_for('ui.login'))

        # Re-check at POST time to avoid race conditions during concurrent first-run requests.
        if db_manager.has_any_registered_users():
            logger.warning("Rejected /setup POST due to first-user race condition")
            return redirect(url_for('ui.login'))

        # POST: create first user (same validation as register)
        username = request.form.get('username', '').strip()
        display_name = request.form.get('display_name', '').strip() or username
        password = request.form.get('password', '')
        password_confirm = request.form.get('password_confirm', '')
        invite_code_raw = request.form.get('invite_code', '').strip()
        if not username or len(username) < 2:
            return render_template('setup.html', error='Username must be at least 2 characters')
        from ..security.password import validate_password_strength
        is_valid, error_msg = validate_password_strength(password)
        if not is_valid:
            return render_template('setup.html', error=error_msg)
        if password != password_confirm:
            return render_template('setup.html', error='Passwords do not match')
        existing = db_manager.get_user_by_username(username)
        if existing:
            return render_template('setup.html', error='Username is already taken')
        user_id = f"user_{secrets.token_hex(8)}"
        keypair = _generate_user_keypair()
        pw_hash = _hash_password(password)
        success = db_manager.create_user(
            user_id=user_id, username=username, public_key=keypair['ed25519_public'],
            password_hash=pw_hash, display_name=display_name
        )
        if not success:
            return render_template('setup.html', error='Failed to create account.')
        db_manager.store_user_keys(
            user_id=user_id,
            ed25519_pub=keypair['ed25519_public'], ed25519_priv=keypair['ed25519_private'],
            x25519_pub=keypair['x25519_public'], x25519_priv=keypair['x25519_private']
        )
        try:
            with db_manager.get_connection() as conn:
                conn.execute("""
                    INSERT OR IGNORE INTO channel_members (channel_id, user_id, role)
                    VALUES ('general', ?, 'member')
                """, (user_id,))
                try:
                    public_channels = conn.execute(
                        "SELECT id FROM channels "
                        "WHERE channel_type = 'public' "
                        "  AND COALESCE(privacy_mode, 'open') = 'open'"
                    ).fetchall()
                except Exception:
                    # Backward compatibility for legacy schemas without privacy_mode.
                    public_channels = conn.execute(
                        "SELECT id FROM channels WHERE channel_type = 'public'"
                    ).fetchall()
                for (ch_id,) in public_channels:
                    conn.execute("""
                        INSERT OR IGNORE INTO channel_members (channel_id, user_id, role)
                        VALUES (?, ?, 'member')
                    """, (ch_id, user_id))
                conn.commit()
        except Exception as e:
            logger.warning(f"Could not add user to channels: {e}")
        owner_id = db_manager.get_instance_owner_user_id()
        if not owner_id:
            db_manager.set_instance_owner_user_id(user_id)
            logger.info(f"Setup: first user created '{username}' ({user_id}) as instance owner")
        else:
            logger.warning(
                f"Setup: owner already set to {owner_id}; preserving existing owner"
            )
        if invite_code_raw:
            try:
                from ..network.invite import InviteCode, import_invite
                _, _, _, _, _, _, _, _, _, _, p2p_manager = get_app_components(current_app)
                if p2p_manager and p2p_manager.identity_manager and p2p_manager.connection_manager:
                    invite = InviteCode.decode(invite_code_raw)
                    import_invite(
                        p2p_manager.identity_manager,
                        p2p_manager.connection_manager,
                        invite,
                    )
                    logger.info("Setup: peer invite imported; connection will be attempted in background")
            except Exception as e:
                logger.warning(f"Setup: could not import invite: {e}")
        session['authenticated'] = True
        session['user_id'] = user_id
        session['username'] = username
        session['display_name'] = display_name
        session.permanent = True
        return redirect(url_for('ui.dashboard'))

    @ui.route('/logout')
    def logout():
        """Logout and clear session."""
        username = session.get('username', 'unknown')
        session.clear()
        logger.info(f"User '{username}' logged out")
        return redirect(url_for('ui.login'))
    
    # Helper function to get current user
    def get_current_user():
        """Get current user ID from the authenticated session."""
        return session.get('user_id', 'local_user')

    def _is_admin():
        """True if the current session user is the instance owner (first registered user)."""
        if not _is_authenticated():
            return False
        db_manager, _, _, _, _, _, _, _, _, _, _ = _get_app_components_any(current_app)
        owner_id = db_manager.get_instance_owner_user_id()
        return owner_id is not None and session.get('user_id') == owner_id

    def require_admin(f):
        """Decorator to require instance-owner admin for a route."""
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if not _is_authenticated():
                return redirect(url_for('ui.login'))
            if not _is_admin():
                if request.accept_mimetypes.accept_json or request.headers.get('X-Requested-With') == 'XMLHttpRequest' or request.path.startswith('/ajax/'):
                    return jsonify({'error': 'Forbidden: instance owner required on this peer'}), 403
                flash('Access denied. Admin only.', 'error')
                return redirect(url_for('ui.dashboard'))
            return f(*args, **kwargs)
        return decorated_function

    _CTX_URL_PATTERN = re.compile(r'https?://[^\s<>"\']+')
    _CTX_YT_ID_PATTERN = re.compile(r'^[A-Za-z0-9_-]{11}$')

    def _ctx_ensure_schema(db_manager: Any) -> None:
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

    def _ctx_extract_urls(text: str, limit: int = 8) -> list:
        if not text:
            return []
        urls = []
        for match in _CTX_URL_PATTERN.finditer(text):
            candidate = (match.group(0) or '').strip()
            candidate = candidate.rstrip('.,;:!?')
            candidate = candidate.rstrip(')>]}')
            if candidate and candidate not in urls:
                urls.append(candidate)
                if len(urls) >= max(1, int(limit)):
                    break
        return urls

    def _ctx_parse_youtube_video_id(url: str) -> Optional[str]:
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
                    parts = path.split('/')
                    vid = parts[1] if len(parts) > 1 else ''
                elif path.startswith('embed/'):
                    parts = path.split('/')
                    vid = parts[1] if len(parts) > 1 else ''
            if vid and _CTX_YT_ID_PATTERN.match(vid):
                return vid
        except Exception:
            return None
        return None

    def _ctx_is_private_ip(ip_obj: Any) -> bool:
        return bool(
            ip_obj.is_private or
            ip_obj.is_loopback or
            ip_obj.is_link_local or
            ip_obj.is_multicast or
            ip_obj.is_reserved or
            ip_obj.is_unspecified
        )

    def _ctx_is_safe_external_url(url: str) -> tuple:
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

        try:
            literal_ip = ipaddress.ip_address(host)
            if _ctx_is_private_ip(literal_ip):
                return False, 'Private/loopback addresses are blocked for context extraction'
            return True, ''
        except ValueError:
            pass

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
                if _ctx_is_private_ip(resolved_ip):
                    return False, 'Host resolves to private/loopback address and is blocked'
        except socket.gaierror:
            return True, ''
        return True, ''

    def _ctx_http_get_text(url: str, timeout: int = 8, max_bytes: int = 900_000) -> str:
        safe, reason = _ctx_is_safe_external_url(url)
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

    def _ctx_strip_html(text: str) -> str:
        if not text:
            return ''
        cleaned = re.sub(r'(?is)<script[^>]*>.*?</script>', ' ', text)
        cleaned = re.sub(r'(?is)<style[^>]*>.*?</style>', ' ', cleaned)
        cleaned = re.sub(r'(?is)<!--.*?-->', ' ', cleaned)
        cleaned = re.sub(r'(?is)<[^>]+>', ' ', cleaned)
        cleaned = html_lib.unescape(cleaned)
        return re.sub(r'\s+', ' ', cleaned).strip()

    def _ctx_extract_generic_page_context(url: str) -> dict:
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
            html = _ctx_http_get_text(url, timeout=8)
            title_match = re.search(r'(?is)<title[^>]*>(.*?)</title>', html)
            title = _ctx_strip_html(title_match.group(1)) if title_match else ''
            desc = ''
            desc_match = re.search(
                r'(?is)<meta[^>]+(?:name|property)\s*=\s*["\'](?:description|og:description)["\'][^>]*content\s*=\s*["\'](.*?)["\']',
                html,
            )
            if desc_match:
                desc = _ctx_strip_html(desc_match.group(1))
            plain_text = _ctx_strip_html(html)
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

    def _ctx_vtt_to_text(vtt_text: str) -> str:
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

    def _ctx_xml_caption_to_text(xml_text: str) -> str:
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

    def _ctx_extract_youtube_context(url: str, video_id: str) -> dict:
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

        try:
            oembed_url = f"https://www.youtube.com/oembed?url={quote_plus(canonical_url)}&format=json"
            raw = _ctx_http_get_text(oembed_url, timeout=6, max_bytes=128_000)
            obj = json.loads(raw)
            context['title'] = (obj.get('title') or '').strip()
            context['author'] = (obj.get('author_name') or '').strip()
            context['metadata']['oembed'] = {
                'provider_name': obj.get('provider_name'),
                'thumbnail_url': obj.get('thumbnail_url'),
            }
        except Exception as e:
            context['metadata']['oembed_error'] = str(e)

        transcript_text = ''
        transcript_lang = ''
        try:
            track_xml = _ctx_http_get_text(
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
                cap_raw = _ctx_http_get_text(captions_url, timeout=8, max_bytes=1_200_000)
                transcript_text = _ctx_vtt_to_text(cap_raw)
                if not transcript_text:
                    transcript_text = _ctx_xml_caption_to_text(cap_raw)
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

    def _ctx_extract_external_context(url: str) -> dict:
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
        vid = _ctx_parse_youtube_video_id(candidate)
        if vid:
            return _ctx_extract_youtube_context(candidate, vid)
        return _ctx_extract_generic_page_context(candidate)

    def _ctx_build_text_blob(row: dict) -> str:
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

    def _ctx_serialize_row(row: Any, current_user_id: str, admin_user_id: Optional[str]) -> dict[str, Any]:
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
        payload['text_blob'] = _ctx_build_text_blob(payload)
        return payload

    def _ctx_can_access_source(db_manager: Any, feed_manager: Any, user_id: str, source_type: str, source_id: str) -> bool:
        if source_type == 'url':
            return True
        if source_type == 'feed_post':
            post = feed_manager.get_post(source_id) if feed_manager else None
            return bool(post and post.can_view(user_id))
        if source_type == 'channel_message':
            with db_manager.get_connection() as conn:
                row = conn.execute("SELECT channel_id FROM channel_messages WHERE id = ?", (source_id,)).fetchone()
                if not row:
                    return False
                member = conn.execute(
                    "SELECT 1 FROM channel_members WHERE channel_id = ? AND user_id = ?",
                    (row['channel_id'], user_id)
                ).fetchone()
                if member:
                    return True
                return bool(row['channel_id'] == 'general')
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
                    return True
                return bool(recipient_id == user_id)
        return False

    def _ctx_resolve_source_payload(
        db_manager: Any,
        feed_manager: Any,
        user_id: str,
        source_type: str,
        source_id: str,
    ) -> tuple[bool, Any, str]:
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
                'source_url_candidates': _ctx_extract_urls(content),
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
                    'source_url_candidates': _ctx_extract_urls(content),
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
                    'source_url_candidates': _ctx_extract_urls(content),
                }, ''

        if source_type == 'url':
            return True, {
                'content': '',
                'owner_user_id': user_id,
                'source_url_candidates': [],
            }, ''

        return False, 400, 'Unsupported source_type'

    def _agent_directive_presets_payload():
        presets = []
        for preset_id, preset in (DEFAULT_AGENT_DIRECTIVE_PRESETS or {}).items():
            raw_content = preset.get('content') if isinstance(preset, dict) else None
            try:
                content = normalize_agent_directives(raw_content)
            except Exception:
                content = None
            presets.append({
                'id': preset_id,
                'label': preset.get('label') if isinstance(preset, dict) else preset_id,
                'description': preset.get('description') if isinstance(preset, dict) else '',
                'content': content or '',
            })
        return presets

    def _effective_agent_directive_state(user_row: dict) -> dict:
        custom = None
        try:
            custom = normalize_agent_directives(user_row.get('agent_directives'))
        except Exception:
            custom = None
        default = get_default_agent_directives(
            username=user_row.get('username'),
            account_type=user_row.get('account_type'),
        )
        if custom:
            effective = custom
            source = 'custom'
        elif default:
            effective = default
            source = 'default'
        else:
            effective = None
            source = 'none'
        return {
            'custom': custom,
            'default': default,
            'effective': effective,
            'source': source,
            'max_length': MAX_AGENT_DIRECTIVES_LENGTH,
        }

    def _build_agent_heartbeat_snapshot(user_id: str) -> dict[str, Any]:
        """Lightweight heartbeat snapshot used by UI (same shape as API heartbeat)."""
        db_manager, _, _, _, _, _, _, _, _, _, _ = get_app_components(current_app)
        mention_manager = current_app.config.get('MENTION_MANAGER')
        inbox_manager = current_app.config.get('INBOX_MANAGER')
        return build_agent_heartbeat_snapshot(
            db_manager=db_manager,
            user_id=user_id,
            mention_manager=mention_manager,
            inbox_manager=inbox_manager,
        )

    def _normalized_account_type(
        raw_account_type: Any,
        *,
        status: Any = None,
        agent_directives: Any = None,
        has_presence_checkin: bool = False,
    ) -> str:
        """Normalize account_type with conservative agent inference for legacy rows.

        Some historical or partially migrated user rows can carry ``account_type='human'``
        even when they are operational agent accounts. We only infer ``agent`` when there
        is explicit supporting evidence (agent directives or pending-approval state).
        """
        account_type = str(raw_account_type or '').strip().lower()
        if account_type not in ('agent', 'human'):
            account_type = 'human'
        if account_type == 'agent':
            return 'agent'

        if has_presence_checkin:
            return 'agent'

        status_norm = str(status or '').strip().lower()
        directives_norm = None
        try:
            directives_norm = normalize_agent_directives(agent_directives)
        except Exception:
            directives_norm = str(agent_directives or '').strip() or None

        if directives_norm:
            return 'agent'
        if status_norm == 'pending_approval':
            return 'agent'
        return 'human'

    def _annotate_user_presence(users: list[dict[str, Any]], db_manager: Any) -> None:
        """Attach badge-friendly presence fields to user rows in place."""
        if not users or not db_manager:
            return
        user_ids = [str(u.get('id') or u.get('user_id') or '').strip() for u in users]
        user_ids = [uid for uid in user_ids if uid]
        if not user_ids:
            return
        presence_records = get_agent_presence_records(db_manager=db_manager, user_ids=user_ids)
        for user in users:
            uid = str(user.get('id') or user.get('user_id') or '').strip()
            presence_record = presence_records.get(uid) or {}
            account_type = _normalized_account_type(
                user.get('account_type'),
                status=user.get('status'),
                agent_directives=user.get('agent_directives'),
                has_presence_checkin=bool(presence_record.get('last_check_in_at')),
            )
            user['account_type'] = account_type
            origin_peer = str(user.get('origin_peer') or '').strip()
            presence = build_agent_presence_payload(
                last_check_in_at=presence_record.get('last_check_in_at'),
                is_remote=bool(origin_peer),
                account_type=account_type,
            )
            user['presence_state'] = presence.get('state')
            user['presence_label'] = presence.get('label')
            user['presence_color'] = presence.get('color')
            user['presence_age_seconds'] = presence.get('age_seconds')
            user['presence_age_text'] = presence.get('age_text')
            user['last_check_in_at'] = presence.get('last_check_in_at')
            user['last_check_in_source'] = presence_record.get('last_check_in_source')

    def _coerce_int(raw: Any, default: int, minimum: int, maximum: int) -> int:
        try:
            value = int(raw)
        except Exception:
            value = default
        if value < minimum:
            return minimum
        if value > maximum:
            return maximum
        return value

    def _admin_registered_user_row(db_manager: Any, user_id: str) -> Optional[dict[str, Any]]:
        if not user_id:
            return None
        row = db_manager.get_user(user_id) if db_manager else None
        if not row or not row.get('password_hash'):
            return None
        return row

    def _profile_value(profile: Any, field: str, default: Any = None) -> Any:
        if profile is None:
            return default
        if isinstance(profile, dict):
            return profile.get(field, default)
        return getattr(profile, field, default)

    def _broadcast_profile_if_possible(profile_manager: Any, user_id: str) -> None:
        if not profile_manager or not user_id:
            return
        try:
            _, _, _, _, _, _, _, _, _, _, p2p_manager = _get_app_components_any(current_app)
            if not p2p_manager or not p2p_manager.is_running():
                return
            card = profile_manager.get_profile_card(user_id)
            if card:
                p2p_manager.broadcast_profile_update(card)
        except Exception as bcast_err:
            logger.warning(f"Admin profile broadcast failed: {bcast_err}")

    def _count_unacked_mentions(db_manager: Any, user_id: str) -> int:
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
                return int((row['n'] if row else 0) or 0)
        except Exception:
            return 0

    def _build_admin_workspace_snapshot(
        user_row: dict[str, Any],
        inbox_limit: int = 25,
        mention_limit: int = 25,
        audit_limit: int = 25,
    ) -> dict[str, Any]:
        user_id = user_row.get('id')
        db_manager, _, _, _, channel_manager, _, _, _, profile_manager, _, _ = _get_app_components_any(current_app)
        mention_manager = current_app.config.get('MENTION_MANAGER')
        inbox_manager = current_app.config.get('INBOX_MANAGER')

        profile = None
        try:
            if profile_manager and user_id:
                profile = profile_manager.get_profile(user_id)
        except Exception:
            profile = None

        display_name = (
            _profile_value(profile, 'display_name')
            or user_row.get('display_name')
            or user_row.get('username')
            or user_id
        )
        bio = _profile_value(profile, 'bio')
        avatar_file_id = _profile_value(profile, 'avatar_file_id') or user_row.get('avatar_file_id')
        avatar_url = _profile_value(profile, 'avatar_url')
        if not avatar_url and avatar_file_id:
            avatar_url = f"/files/{avatar_file_id}"
        theme_preference = (
            _profile_value(profile, 'theme_preference')
            or user_row.get('theme_preference')
            or 'dark'
        )

        workspace = {
            'user': {
                'id': user_id,
                'username': user_row.get('username'),
                'display_name': display_name,
                'bio': bio or '',
                'avatar_file_id': avatar_file_id,
                'avatar_url': avatar_url,
                'account_type': user_row.get('account_type') or 'human',
                'status': user_row.get('status') or 'active',
                'theme_preference': theme_preference,
                'origin_peer': user_row.get('origin_peer'),
                'created_at': user_row.get('created_at'),
                'is_local': not bool(user_row.get('origin_peer')),
            },
            'inbox': {
                'available': bool(inbox_manager),
                'pending_count': 0,
                'total_count': 0,
                'stats': {},
                'config': {},
                'items': [],
                'audit': [],
            },
            'mentions': {
                'available': bool(mention_manager),
                'unacked_count': 0,
                'items': [],
            },
            'governance': {
                'available': bool(channel_manager),
                'policy': {
                    'enabled': False,
                    'block_public_channels': False,
                    'restrict_to_allowed_channels': False,
                    'allowed_channel_ids': [],
                },
                'channels': [],
            },
        }

        if inbox_manager and user_id:
            try:
                pending_count = inbox_manager.count_items(user_id=user_id, status='pending')
                total_count = inbox_manager.count_items(user_id=user_id)
                items = inbox_manager.list_items(
                    user_id=user_id,
                    status=None,
                    limit=inbox_limit,
                    include_handled=True,
                )
                audit = inbox_manager.list_audit(user_id=user_id, limit=audit_limit)
                stats = inbox_manager.get_stats(user_id=user_id, window_hours=24)
                config = inbox_manager.get_config(user_id)
                workspace['inbox'].update({
                    'pending_count': pending_count,
                    'total_count': total_count,
                    'stats': stats or {},
                    'config': config or {},
                    'items': [
                        {
                            'id': item.get('id'),
                            'source_type': item.get('source_type'),
                            'source_id': item.get('source_id'),
                            'channel_id': item.get('channel_id'),
                            'sender_user_id': item.get('sender_user_id'),
                            'trigger_type': item.get('trigger_type'),
                            'status': item.get('status'),
                            'priority': item.get('priority'),
                            'created_at': item.get('created_at'),
                            'handled_at': item.get('handled_at'),
                            'preview': ((item.get('payload') or {}).get('preview') or '')[:220],
                        }
                        for item in (items or [])
                    ],
                    'audit': [
                        {
                            'id': row.get('id'),
                            'reason': row.get('reason'),
                            'source_type': row.get('source_type'),
                            'source_id': row.get('source_id'),
                            'channel_id': row.get('channel_id'),
                            'sender_user_id': row.get('sender_user_id'),
                            'trigger_type': row.get('trigger_type'),
                            'created_at': row.get('created_at'),
                        }
                        for row in (audit or [])
                    ],
                })
            except Exception as inbox_err:
                logger.warning(f"Admin workspace inbox snapshot failed for {user_id}: {inbox_err}")

        if mention_manager and user_id:
            try:
                mention_items = mention_manager.get_mentions(
                    user_id=user_id,
                    limit=mention_limit,
                    include_acknowledged=True,
                )
                workspace['mentions'].update({
                    'unacked_count': _count_unacked_mentions(db_manager, user_id),
                    'items': [
                        {
                            'id': m.get('id'),
                            'source_type': m.get('source_type'),
                            'source_id': m.get('source_id'),
                            'author_id': m.get('author_id'),
                            'channel_id': m.get('channel_id'),
                            'status': m.get('status'),
                            'created_at': m.get('created_at'),
                            'acknowledged_at': m.get('acknowledged_at'),
                            'preview': (m.get('preview') or '')[:220],
                        }
                        for m in (mention_items or [])
                    ],
                })
            except Exception as mention_err:
                logger.warning(f"Admin workspace mentions snapshot failed for {user_id}: {mention_err}")

        if channel_manager and user_id:
            try:
                policy = channel_manager.get_user_channel_governance(user_id)
                channels = channel_manager.list_channels_for_governance(user_id=user_id)
                if not isinstance(policy, dict):
                    policy = {}
                if not isinstance(channels, list):
                    channels = []
                workspace['governance'].update({
                    'policy': {
                        'enabled': bool(policy.get('enabled', False)),
                        'block_public_channels': bool(policy.get('block_public_channels', False)),
                        'restrict_to_allowed_channels': bool(policy.get('restrict_to_allowed_channels', False)),
                        'allowed_channel_ids': list(policy.get('allowed_channel_ids') or []),
                        'updated_at': policy.get('updated_at'),
                        'updated_by': policy.get('updated_by'),
                    },
                    'channels': channels,
                })
            except Exception as gov_err:
                logger.warning(f"Admin workspace governance snapshot failed for {user_id}: {gov_err}")

        return workspace

    def _serialize_community_notes(notes: Any, viewer_user_id: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        note_type_labels = {
            'context': 'Context',
            'correction': 'Correction',
            'misleading': 'Misleading',
            'outdated': 'Outdated',
            'endorsement': 'Endorsement',
        }
        status_labels = {
            'proposed': 'Proposed',
            'accepted': 'Accepted',
            'rejected': 'Rejected',
        }
        for note in notes or []:
            ratings = note.get('ratings') or {}
            total = int(ratings.get('total') or 0)
            helpful = int(ratings.get('helpful') or 0)
            ratio = (helpful / total * 100.0) if total else None
            status = (note.get('status') or 'proposed').lower()
            note_type = (note.get('note_type') or 'context').lower()
            out.append({
                'id': note.get('id'),
                'target_type': note.get('target_type'),
                'target_id': note.get('target_id'),
                'author_id': note.get('author_id'),
                'content': note.get('content') or '',
                'status': status,
                'status_label': status_labels.get(status, status.title()),
                'note_type': note_type,
                'note_type_label': note_type_labels.get(note_type, note_type.title()),
                'created_at': note.get('created_at'),
                'updated_at': note.get('updated_at'),
                'ratings': {
                    'total': total,
                    'helpful': helpful,
                    'not_helpful': max(0, total - helpful),
                    'helpful_ratio': round(ratio, 1) if ratio is not None else None,
                },
                'can_rate': bool(viewer_user_id and viewer_user_id != note.get('author_id')),
            })
        return out

    def _load_target_notes(
        skill_manager: Any,
        target_type: str,
        target_id: str,
        viewer_user_id: str,
        limit: int = 12,
    ) -> list[dict[str, Any]]:
        if not skill_manager or not target_type or not target_id:
            return []
        try:
            notes = skill_manager.get_community_notes(
                target_type=target_type,
                target_id=target_id,
                limit=max(1, min(int(limit), 50)),
            )
            visible = [n for n in (notes or []) if (n.get('status') or 'proposed') != 'rejected']
            return _serialize_community_notes(visible, viewer_user_id)
        except Exception:
            return []

    def _can_access_note_target(
        *,
        db_manager: Any,
        feed_manager: Any,
        signal_manager: Any,
        skill_manager: Any,
        user_id: str,
        target_type: str,
        target_id: str,
    ) -> bool:
        target_type = (target_type or '').strip().lower()
        target_id = (target_id or '').strip()
        if not target_type or not target_id or not user_id:
            return False

        if _is_admin():
            return True

        if target_type == 'feed_post':
            post = feed_manager.get_post(target_id) if feed_manager else None
            return bool(post and post.can_user_view(user_id))

        if target_type == 'channel_message':
            try:
                with db_manager.get_connection() as conn:
                    row = conn.execute(
                        "SELECT channel_id FROM channel_messages WHERE id = ?",
                        (target_id,)
                    ).fetchone()
                    if not row:
                        return False
                    member = conn.execute(
                        "SELECT 1 FROM channel_members WHERE channel_id = ? AND user_id = ?",
                        (row['channel_id'], user_id)
                    ).fetchone()
                return bool(member)
            except Exception:
                return False

        if target_type == 'signal':
            signal = signal_manager.get_signal(target_id) if signal_manager else None
            if not signal:
                return False
            st = (signal.get('source_type') or '').lower()
            sid = signal.get('source_id')
            sid_str = str(sid or '').strip()
            if not sid_str:
                return False
            if st in ('feed', 'feed_post'):
                return _can_access_note_target(
                    db_manager=db_manager,
                    feed_manager=feed_manager,
                    signal_manager=signal_manager,
                    skill_manager=skill_manager,
                    user_id=user_id,
                    target_type='feed_post',
                    target_id=sid_str,
                )
            if st in ('channel', 'channel_message'):
                return _can_access_note_target(
                    db_manager=db_manager,
                    feed_manager=feed_manager,
                    signal_manager=signal_manager,
                    skill_manager=skill_manager,
                    user_id=user_id,
                    target_type='channel_message',
                    target_id=sid_str,
                )
            return user_id in {signal.get('owner_id'), signal.get('created_by')}

        if target_type == 'skill':
            try:
                with db_manager.get_connection() as conn:
                    row = conn.execute(
                        "SELECT source_type, source_id, author_id FROM skills WHERE id = ?",
                        (target_id,)
                    ).fetchone()
                if not row:
                    return False
                st = (row['source_type'] or '').lower()
                sid = row['source_id']
                sid_str = str(sid or '').strip()
                if not sid_str:
                    return False
                if st in ('feed', 'feed_post'):
                    return _can_access_note_target(
                        db_manager=db_manager,
                        feed_manager=feed_manager,
                        signal_manager=signal_manager,
                        skill_manager=skill_manager,
                        user_id=user_id,
                        target_type='feed_post',
                        target_id=sid_str,
                    )
                if st in ('channel', 'channel_message'):
                    return _can_access_note_target(
                        db_manager=db_manager,
                        feed_manager=feed_manager,
                        signal_manager=signal_manager,
                        skill_manager=skill_manager,
                        user_id=user_id,
                        target_type='channel_message',
                        target_id=sid_str,
                    )
                return bool(row['author_id'] == user_id)
            except Exception:
                return False

        return False

    def _build_inline_skill_payload(skill_manager: Any, spec: Any, source_type: str, source_id: str) -> dict[str, Any]:
        from ..core.skills import derive_skill_id

        skill_id = derive_skill_id(source_type, source_id, spec.name)
        trust_score = None
        trust_components = None
        endorsement_count = 0
        invocation_count = 0

        if skill_manager:
            try:
                trust_data = skill_manager.get_skill_trust_score(skill_id)
                trust_score = trust_data.get('trust_score')
                trust_components = trust_data.get('components') or {}
                invocation_count = int((trust_components or {}).get('invocation_count') or 0)
            except Exception:
                trust_score = None
                trust_components = None
            try:
                endorsement_count = len(skill_manager.get_endorsements(skill_id) or [])
            except Exception:
                endorsement_count = 0

        return {
            'id': skill_id,
            'name': spec.name,
            'version': spec.version or '',
            'description': spec.description or '',
            'inputs': spec.inputs or [],
            'outputs': spec.outputs or [],
            'perms': spec.perms or [],
            'invokes': spec.invokes or '',
            'tags': spec.tags or [],
            'trust_score': trust_score,
            'trust_percent': int(round(trust_score * 100)) if trust_score is not None else None,
            'trust_components': trust_components or {},
            'endorsement_count': endorsement_count,
            'invocation_count': invocation_count,
        }

    # Landing page — redirect to user's preferred page (or smart default)
    @ui.route('/')
    @require_login
    def dashboard():
        """Redirect to the user's preferred landing page."""
        # Check for user preference cookie
        landing = request.cookies.get('canopy_landing')
        if landing in ('feed', 'channels', 'messages'):
            return redirect(url_for(f'ui.{landing}'))

        # Smart default: mobile → feed, desktop → channels
        ua = (request.headers.get('User-Agent') or '').lower()
        is_mobile = any(kw in ua for kw in ('iphone', 'android', 'mobile', 'ipod'))
        if is_mobile:
            return redirect(url_for('ui.feed'))
        return redirect(url_for('ui.channels'))
    
    # Messages interface
    @ui.route('/messages')
    @require_login
    def messages():
        """Messages interface for viewing and sending messages."""
        try:
            db_manager, _, _, message_manager, _, _, _, _, profile_manager, _, _ = _get_app_components_any(current_app)
            user_id = get_current_user()
            conversation_with = (request.args.get('with') or '').strip() or None
            conversation_group = (request.args.get('group') or '').strip() or None
            search_query = request.args.get('search', '').strip()

            if conversation_group and not conversation_group.startswith('group:'):
                conversation_group = None

            user_display_cache: dict[str, dict[str, Any]] = {}

            def _safe_text(value: Any) -> Optional[str]:
                if value is None:
                    return None
                if isinstance(value, str):
                    return value
                if isinstance(value, (int, float, bool)):
                    return str(value)
                return None

            def _user_display(uid: Optional[str]) -> Optional[dict[str, Any]]:
                clean_uid = str(uid or '').strip()
                if not clean_uid:
                    return None
                cached = user_display_cache.get(clean_uid)
                if cached is not None:
                    return cached

                display = {
                    'user_id': clean_uid,
                    'display_name': None,
                    'username': None,
                    'avatar_url': None,
                    'origin_peer': None,
                    'account_type': None,
                }
                try:
                    if profile_manager:
                        profile = profile_manager.get_profile(clean_uid)
                        if profile:
                            display['display_name'] = _safe_text(getattr(profile, 'display_name', None)) or _safe_text(getattr(profile, 'username', None)) or clean_uid
                            display['username'] = _safe_text(getattr(profile, 'username', None)) or clean_uid
                            display['avatar_url'] = _safe_text(getattr(profile, 'avatar_url', None))
                            display['origin_peer'] = _safe_text(getattr(profile, 'origin_peer', None))
                            display['account_type'] = _safe_text(getattr(profile, 'account_type', None))
                    if db_manager:
                        row = db_manager.get_user(clean_uid)
                        if row:
                            display['display_name'] = display.get('display_name') or _safe_text(row.get('display_name')) or _safe_text(row.get('username'))
                            display['username'] = display.get('username') or _safe_text(row.get('username'))
                            display['origin_peer'] = display.get('origin_peer') or _safe_text(row.get('origin_peer'))
                            display['account_type'] = display.get('account_type') or _safe_text(row.get('account_type'))
                            if not display.get('avatar_url') and row.get('avatar_file_id'):
                                display['avatar_url'] = f"/files/{row.get('avatar_file_id')}"
                except Exception:
                    pass

                if not display.get('display_name'):
                    display['display_name'] = clean_uid
                if not display.get('username'):
                    display['username'] = clean_uid

                user_display_cache[clean_uid] = display
                return display

            def _message_meta(message: Any) -> dict[str, Any]:
                meta = getattr(message, 'metadata', None)
                return meta if isinstance(meta, dict) else {}

            def _normalize_members(raw_members: Any, fallback_members: Optional[list[str]] = None) -> list[str]:
                members: list[str] = []
                if isinstance(raw_members, list):
                    for raw in raw_members:
                        uid = str(raw or '').strip()
                        if uid and uid not in members:
                            members.append(uid)
                for raw in fallback_members or []:
                    uid = str(raw or '').strip()
                    if uid and uid not in members:
                        members.append(uid)
                return members

            def _group_thread_identity(meta: dict[str, Any], recipient_id: str) -> tuple[Optional[str], Optional[str], list[str], list[str]]:
                raw_group_id = str(meta.get('group_id') or '').strip()
                group_members = _normalize_members(meta.get('group_members'))
                alias_group_ids: list[str] = []
                if raw_group_id:
                    alias_group_ids.append(raw_group_id)
                if recipient_id.startswith('group:') and recipient_id not in alias_group_ids:
                    alias_group_ids.append(recipient_id)

                canonical_group_key: Optional[str] = None
                if group_members:
                    canonical_group_key = _compute_group_id(group_members)
                    if canonical_group_key not in alias_group_ids:
                        alias_group_ids.append(canonical_group_key)
                elif alias_group_ids:
                    canonical_group_key = alias_group_ids[0]

                display_group_id = raw_group_id or (recipient_id if recipient_id.startswith('group:') else '') or canonical_group_key
                return (display_group_id or None, canonical_group_key, group_members, alias_group_ids)

            def _classify_thread(message: Any) -> Optional[dict[str, Any]]:
                meta = _message_meta(message)
                recipient_id = str(getattr(message, 'recipient_id', None) or '').strip()
                group_id, canonical_group_key, group_members, alias_group_ids = _group_thread_identity(meta, recipient_id)

                if group_members or group_id or canonical_group_key:
                    if not group_members:
                        fallback = [user_id, getattr(message, 'sender_id', None)]
                        if recipient_id and not recipient_id.startswith('group:'):
                            fallback.append(recipient_id)
                        group_members = _normalize_members(group_members, [str(v or '').strip() for v in fallback])
                    if user_id not in group_members:
                        return None
                    if not canonical_group_key:
                        canonical_group_key = _compute_group_id(group_members)
                    if not group_id:
                        group_id = canonical_group_key
                    return {
                        'kind': 'group',
                        'key': f'group-thread:{canonical_group_key}',
                        'group_id': group_id,
                        'canonical_group_key': canonical_group_key,
                        'alias_group_ids': alias_group_ids,
                        'member_ids': group_members,
                    }

                if not recipient_id:
                    return None

                if getattr(message, 'sender_id', None) == user_id:
                    other_user_id = recipient_id
                elif recipient_id == user_id:
                    other_user_id = str(getattr(message, 'sender_id', None) or '').strip()
                else:
                    return None

                if not other_user_id or other_user_id == user_id:
                    return None

                return {
                    'kind': 'direct',
                    'key': f'direct:{other_user_id}',
                    'user_id': other_user_id,
                }

            def _group_thread_matches(left: Optional[dict[str, Any]], right: Optional[dict[str, Any]]) -> bool:
                if not left or not right:
                    return False
                left_key = str(left.get('canonical_group_key') or '').strip()
                right_key = str(right.get('canonical_group_key') or '').strip()
                if left_key and right_key and left_key == right_key:
                    return True

                left_aliases = {
                    str(raw or '').strip()
                    for raw in (left.get('alias_group_ids') or [])
                    if str(raw or '').strip()
                }
                right_aliases = {
                    str(raw or '').strip()
                    for raw in (right.get('alias_group_ids') or [])
                    if str(raw or '').strip()
                }
                left_group_id = str(left.get('group_id') or '').strip()
                right_group_id = str(right.get('group_id') or '').strip()
                if left_group_id:
                    left_aliases.add(left_group_id)
                if right_group_id:
                    right_aliases.add(right_group_id)
                return bool(left_aliases and right_aliases and left_aliases.intersection(right_aliases))

            def _format_thread_title(thread: dict[str, Any]) -> tuple[str, str, list[dict[str, Any]]]:
                if thread.get('kind') == 'direct':
                    other = _user_display(thread.get('user_id')) or {'display_name': thread.get('user_id')}
                    subtitle_parts = []
                    account_type = str(other.get('account_type') or '').strip()
                    origin_peer = str(other.get('origin_peer') or '').strip()
                    if account_type:
                        subtitle_parts.append(account_type.title())
                    if origin_peer:
                        subtitle_parts.append(origin_peer)
                    return (
                        str(other.get('display_name') or thread.get('user_id') or 'Direct message'),
                        ' • '.join(subtitle_parts),
                        [other],
                    )

                members = [member_id for member_id in (thread.get('member_ids') or []) if member_id]
                other_members = [member_id for member_id in members if member_id != user_id]
                previews = [_user_display(member_id) or {'user_id': member_id, 'display_name': member_id} for member_id in other_members]
                if not previews:
                    previews = [_user_display(member_id) or {'user_id': member_id, 'display_name': member_id} for member_id in members[:1]]
                title_names = [str(item.get('display_name') or item.get('user_id') or 'Unknown') for item in previews[:3]]
                title = ', '.join(title_names) if title_names else 'Group DM'
                if len(previews) > 3:
                    title += f" +{len(previews) - 3}"
                subtitle = f"{len(other_members) or len(members)} participant{'s' if (len(other_members) or len(members)) != 1 else ''}"
                return (title, subtitle, previews)

            def _thread_href(thread: dict[str, Any]) -> str:
                if thread.get('kind') == 'group':
                    return url_for(
                        'ui.messages',
                        group=str(thread.get('canonical_group_key') or thread.get('group_id') or ''),
                    )
                return url_for('ui.messages', **{'with': thread.get('user_id')})

            def _day_label(dt: datetime) -> str:
                local_dt = dt.astimezone()
                today = datetime.now(local_dt.tzinfo).date()
                msg_day = local_dt.date()
                if msg_day == today:
                    return 'Today'
                if msg_day == today - timedelta(days=1):
                    return 'Yesterday'
                return local_dt.strftime('%b %d, %Y')

            all_dm_messages = [
                message
                for message in message_manager.get_messages(user_id, limit=400)
                if _classify_thread(message)
            ]

            conversations_by_key: dict[str, dict[str, Any]] = {}
            for message in all_dm_messages:
                thread = _classify_thread(message)
                if not thread:
                    continue
                thread_key = str(thread['key'])
                attachments = (_message_meta(message).get('attachments') or [])
                entry = conversations_by_key.get(thread_key)
                if entry is None:
                    title, subtitle, preview_users = _format_thread_title(thread)
                    entry = {
                        'key': thread_key,
                        'kind': thread.get('kind'),
                        'href': _thread_href(thread),
                        'title': title,
                        'subtitle': subtitle,
                        'preview_users': preview_users,
                        'preview': build_dm_preview(getattr(message, 'content', ''), attachments) or 'Attachment',
                        'updated_at': message.created_at.isoformat(),
                        'updated_dt': message.created_at,
                        'unread_count': 0,
                        'message_count': 0,
                        'is_active': False,
                        'group_id': thread.get('group_id'),
                        'canonical_group_key': thread.get('canonical_group_key'),
                        'alias_group_ids': list(thread.get('alias_group_ids') or []),
                        'user_id': thread.get('user_id'),
                        'member_ids': thread.get('member_ids') or [],
                    }
                    conversations_by_key[thread_key] = entry
                entry['message_count'] += 1
                if getattr(message, 'sender_id', None) != user_id and not getattr(message, 'read_at', None):
                    entry['unread_count'] += 1

            conversation_entries = sorted(
                conversations_by_key.values(),
                key=lambda item: item.get('updated_dt') or datetime.min.replace(tzinfo=timezone.utc),
                reverse=True,
            )

            active_thread: Optional[dict[str, Any]] = None
            if conversation_group:
                group_members = []
                matching_entry = next(
                    (
                        item
                        for item in conversation_entries
                        if item.get('kind') == 'group'
                        and _group_thread_matches(
                            item,
                            {
                                'kind': 'group',
                                'group_id': conversation_group,
                                'canonical_group_key': conversation_group,
                                'alias_group_ids': [conversation_group],
                            },
                        )
                    ),
                    None,
                )
                if matching_entry:
                    group_members = list(matching_entry.get('member_ids') or [])
                    title = str(matching_entry.get('title') or 'Group DM')
                    subtitle = str(matching_entry.get('subtitle') or '')
                    preview_users = list(matching_entry.get('preview_users') or [])
                else:
                    title = 'Group DM'
                    subtitle = ''
                    preview_users = []
                active_thread = {
                    'kind': 'group',
                    'group_id': str((matching_entry or {}).get('group_id') or conversation_group),
                    'canonical_group_key': str((matching_entry or {}).get('canonical_group_key') or conversation_group),
                    'alias_group_ids': list((matching_entry or {}).get('alias_group_ids') or [conversation_group]),
                    'title': title,
                    'subtitle': subtitle,
                    'participant_ids': group_members,
                    'preview_users': preview_users,
                    'href': str((matching_entry or {}).get('href') or url_for('ui.messages', group=conversation_group)),
                }
            elif conversation_with:
                direct_thread = {'kind': 'direct', 'user_id': conversation_with}
                direct_title, direct_subtitle, direct_preview_users = _format_thread_title(direct_thread)
                active_thread = {
                    'kind': 'direct',
                    'user_id': conversation_with,
                    'title': direct_title,
                    'subtitle': direct_subtitle,
                    'participant_ids': [conversation_with],
                    'preview_users': direct_preview_users,
                    'href': url_for('ui.messages', **{'with': conversation_with}),
                }
            elif conversation_entries and not search_query:
                selected = conversation_entries[0]
                selected['is_active'] = True
                if selected.get('kind') == 'group':
                    conversation_group = selected.get('group_id')
                    active_thread = {
                        'kind': 'group',
                        'group_id': conversation_group,
                        'canonical_group_key': selected.get('canonical_group_key'),
                        'alias_group_ids': list(selected.get('alias_group_ids') or []),
                        'title': selected.get('title'),
                        'subtitle': selected.get('subtitle'),
                        'participant_ids': list(selected.get('member_ids') or []),
                        'preview_users': list(selected.get('preview_users') or []),
                        'href': selected.get('href'),
                    }
                else:
                    conversation_with = selected.get('user_id')
                    active_thread = {
                        'kind': 'direct',
                        'user_id': conversation_with,
                        'title': selected.get('title'),
                        'subtitle': selected.get('subtitle'),
                        'participant_ids': [conversation_with] if conversation_with else [],
                        'preview_users': list(selected.get('preview_users') or []),
                        'href': selected.get('href'),
                    }

            for entry in conversation_entries:
                if active_thread and (
                    (entry.get('kind') == 'group' and _group_thread_matches(entry, active_thread))
                    or (entry.get('kind') == 'direct' and entry.get('user_id') == active_thread.get('user_id'))
                ):
                    entry['is_active'] = True

            active_messages: list[Any] = []
            if active_thread and not search_query:
                if active_thread.get('kind') == 'group':
                    requested_group_id = str(active_thread.get('canonical_group_key') or active_thread.get('group_id') or '').strip()
                    active_messages = message_manager.get_group_conversation(user_id, requested_group_id, limit=200)
                    if not active_messages:
                        fallback_group_id = str(active_thread.get('group_id') or '').strip()
                        if fallback_group_id and fallback_group_id != requested_group_id:
                            active_messages = message_manager.get_group_conversation(user_id, fallback_group_id, limit=200)
                else:
                    active_messages = message_manager.get_conversation(user_id, str(active_thread.get('user_id') or ''), limit=200)
                for message in active_messages:
                    if getattr(message, 'sender_id', None) != user_id and not getattr(message, 'read_at', None):
                        message_manager.mark_message_read(message.id, user_id)
                for entry in conversation_entries:
                    if active_thread.get('kind') == 'group' and entry.get('kind') == 'group' and _group_thread_matches(entry, active_thread):
                        entry['unread_count'] = 0
                        break
                    if active_thread.get('kind') == 'direct' and entry.get('kind') == 'direct' and entry.get('user_id') == active_thread.get('user_id'):
                        entry['unread_count'] = 0
                        break

            reply_preview_cache: dict[str, Optional[dict[str, Any]]] = {}

            def _reply_preview(reply_to_id: Optional[str]) -> Optional[dict[str, Any]]:
                clean_id = str(reply_to_id or '').strip()
                if not clean_id:
                    return None
                if clean_id in reply_preview_cache:
                    return reply_preview_cache[clean_id]

                source_message = next((item for item in active_messages if item.id == clean_id), None)
                if source_message is None:
                    try:
                        source_message = message_manager.get_message(clean_id)
                    except Exception:
                        source_message = None
                if not source_message:
                    reply_preview_cache[clean_id] = None
                    return None

                sender = _user_display(getattr(source_message, 'sender_id', None)) or {'display_name': getattr(source_message, 'sender_id', None)}
                attachments = (_message_meta(source_message).get('attachments') or [])
                preview_text = build_dm_preview(getattr(source_message, 'content', ''), attachments) or 'Attachment'
                preview = {
                    'id': clean_id,
                    'sender_name': str(sender.get('display_name') or getattr(source_message, 'sender_id', None) or 'Unknown'),
                    'preview': preview_text,
                }
                reply_preview_cache[clean_id] = preview
                return preview

            message_rows: list[dict[str, Any]] = []
            active_messages_sorted = sorted(active_messages, key=lambda message: message.created_at)
            for index, message in enumerate(active_messages_sorted):
                prev_message = active_messages_sorted[index - 1] if index > 0 else None
                next_message = active_messages_sorted[index + 1] if index + 1 < len(active_messages_sorted) else None
                sender = _user_display(getattr(message, 'sender_id', None)) or {'display_name': getattr(message, 'sender_id', None)}
                meta = _message_meta(message)
                attachments = meta.get('attachments') or []
                reply_to_id = str(meta.get('reply_to') or '').strip() or None
                cluster_start = (
                    prev_message is None
                    or prev_message.sender_id != message.sender_id
                    or (message.created_at - prev_message.created_at) > timedelta(minutes=12)
                    or prev_message.created_at.astimezone().date() != message.created_at.astimezone().date()
                )
                cluster_end = (
                    next_message is None
                    or next_message.sender_id != message.sender_id
                    or (next_message.created_at - message.created_at) > timedelta(minutes=12)
                    or next_message.created_at.astimezone().date() != message.created_at.astimezone().date()
                )
                day_divider = None
                if prev_message is None or prev_message.created_at.astimezone().date() != message.created_at.astimezone().date():
                    day_divider = _day_label(message.created_at)

                message_rows.append({
                    'id': message.id,
                    'sender_id': message.sender_id,
                    'sender_label': sender.get('display_name') or message.sender_id,
                    'sender_avatar_url': sender.get('avatar_url'),
                    'sender_origin_peer': sender.get('origin_peer'),
                    'sender_account_type': sender.get('account_type'),
                    'outbound': message.sender_id == user_id,
                    'content': getattr(message, 'content', '') or '',
                    'attachments': attachments,
                    'message_type': getattr(getattr(message, 'message_type', None), 'value', None) or str(getattr(message, 'message_type', '') or ''),
                    'created_at': message.created_at.isoformat(),
                    'edited_at': message.edited_at.isoformat() if getattr(message, 'edited_at', None) else None,
                    'reply_to': reply_to_id,
                    'reply_preview': _reply_preview(reply_to_id),
                    'cluster_start': cluster_start,
                    'cluster_end': cluster_end,
                    'day_divider': day_divider,
                })

            search_results: list[dict[str, Any]] = []
            if search_query:
                found_messages = [message for message in message_manager.search_messages(user_id, search_query, limit=100) if _classify_thread(message)]
                for message in found_messages:
                    thread = _classify_thread(message)
                    if not thread:
                        continue
                    title, _, _ = _format_thread_title(thread)
                    href = _thread_href(thread)
                    sender = _user_display(getattr(message, 'sender_id', None)) or {'display_name': getattr(message, 'sender_id', None)}
                    search_results.append({
                        'id': message.id,
                        'thread_href': f"{href}#message-{message.id}",
                        'thread_title': title,
                        'sender_label': sender.get('display_name') or message.sender_id,
                        'content': getattr(message, 'content', '') or '',
                        'created_at': message.created_at.isoformat(),
                        'preview': build_dm_preview(getattr(message, 'content', ''), _message_meta(message).get('attachments') or []),
                    })

            direct_conversations = [entry for entry in conversation_entries if entry.get('kind') == 'direct']
            group_conversations = [entry for entry in conversation_entries if entry.get('kind') == 'group']

            composer_recipients: list[dict[str, Any]] = []
            if active_thread:
                if active_thread.get('kind') == 'group':
                    for member_id in active_thread.get('participant_ids') or []:
                        if member_id and member_id != user_id:
                            member = _user_display(member_id) or {'user_id': member_id, 'display_name': member_id}
                            composer_recipients.append({
                                'user_id': member_id,
                                'display_name': member.get('display_name') or member_id,
                                'username': member.get('username') or member_id,
                                'avatar_url': member.get('avatar_url'),
                                'unknown': False,
                            })
                elif active_thread.get('user_id'):
                    member = _user_display(active_thread.get('user_id')) or {'user_id': active_thread.get('user_id'), 'display_name': active_thread.get('user_id')}
                    composer_recipients.append({
                        'user_id': active_thread.get('user_id'),
                        'display_name': member.get('display_name') or active_thread.get('user_id'),
                        'username': member.get('username') or active_thread.get('user_id'),
                        'avatar_url': member.get('avatar_url'),
                        'unknown': False,
                    })

            template_data = {
                'user_id': user_id,
                'search_query': search_query,
                'active_thread': active_thread,
                'message_rows': message_rows,
                'direct_conversations': direct_conversations,
                'group_conversations': group_conversations,
                'conversation_entries': conversation_entries,
                'search_results': search_results,
                'composer_recipients': composer_recipients,
                'conversation_with': conversation_with,
                'conversation_group': conversation_group,
            }
            return render_template('messages.html', **template_data)
                
        except Exception as e:
            logger.error(f"Messages error: {e}")
            flash('Error loading messages', 'error')
            return render_template('error.html', error=str(e))
    
    # API Key management
    @ui.route('/keys')
    @require_login
    def api_keys():
        """API key management interface."""
        try:
            _, api_key_manager, _, _, _, _, _, _, _, _, _ = _get_app_components_any(current_app)
            user_id = get_current_user()
            
            # Get user's API keys
            keys = api_key_manager.list_keys(user_id)
            
            # Get usage statistics
            key_stats = api_key_manager.get_key_usage_stats(user_id)
            
            # Get available permissions
            all_permissions = api_key_manager.get_all_permissions()
            default_permissions = api_key_manager.get_default_permissions()
            
            # Get current time for expiration checks
            from datetime import datetime
            now = datetime.now()
            
            return render_template('api_keys.html',
                                 keys=keys,
                                 key_stats=key_stats,
                                 all_permissions=all_permissions,
                                 default_permissions=default_permissions,
                                 user_id=user_id,
                                 now=now)
                                 
        except Exception as e:
            logger.error(f"API keys error: {e}")
            flash('Error loading API keys', 'error')
            return render_template('error.html', error=str(e))
    
    # Trust management
    @ui.route('/trust')
    @require_login
    def trust_management():
        """Trust scores and network health interface."""
        try:
            _, _, trust_manager, _, channel_manager, _, _, _, _, _, p2p_manager = _get_app_components_any(current_app)
            
            # Get all trust scores
            trust_scores = trust_manager.get_all_trust_scores() if trust_manager else {}

            # Connected and introduced peers
            connected_peers = p2p_manager.get_connected_peers() if p2p_manager else []
            introduced_peers = p2p_manager.get_introduced_peers() if p2p_manager else []

            # Device profiles for peer identification
            peer_device_profiles = channel_manager.get_all_peer_device_profiles() if channel_manager else {}

            # Ensure connected peers have entries
            all_peer_ids = set(trust_scores.keys())
            for pid in connected_peers:
                if pid:
                    all_peer_ids.add(pid)
            if trust_manager:
                for pid in all_peer_ids:
                    if pid not in trust_scores:
                        score = trust_manager.get_trust_score(pid)
                        trust_scores[pid] = {
                            'score': score,
                            'last_interaction': None,
                            'compliance_events': 0,
                            'violation_events': 0,
                            'notes': 'discovered',
                            'is_trusted': score >= 50
                        }
            
            # Get trust statistics
            trust_stats = trust_manager.get_trust_statistics() if trust_manager else {}
            
            # Get pending delete signals
            delete_signals = trust_manager.get_pending_delete_signals() if trust_manager else []
            
            # Get trusted peers
            trusted_peers = trust_manager.get_trusted_peers() if trust_manager else []

            # Build tiered trust buckets for UI
            trust_tiers: dict[str, list[dict[str, Any]]] = {
                'safe': [],
                'guarded': [],
                'restricted': [],
                'quarantine': []
            }
            for peer_id, score_info in trust_scores.items():
                score = score_info.get('score', 0)
                if score >= 80:
                    tier = 'safe'
                elif score >= 60:
                    tier = 'guarded'
                elif score >= 40:
                    tier = 'restricted'
                else:
                    tier = 'quarantine'
                trust_tiers[tier].append({**score_info, 'peer_id': peer_id})

            # Potential peers list (introduced but not yet assigned a trust tier)
            potential_peers = []
            existing_peer_ids = set(trust_scores.keys())
            for peer in introduced_peers:
                pid = peer.get('peer_id') if isinstance(peer, dict) else None
                if pid and pid not in existing_peer_ids:
                    potential_peers.append(peer)
            
            return render_template('trust.html',
                                 trust_scores=trust_scores,
                                 trust_stats=trust_stats,
                                 delete_signals=delete_signals,
                                 trusted_peers=trusted_peers,
                                 trust_tiers=trust_tiers,
                                 connected_peers=connected_peers,
                                 introduced_peers=introduced_peers,
                                 potential_peers=potential_peers,
                                 peer_device_profiles=peer_device_profiles)
                                 
        except Exception as e:
            logger.error(f"Trust management error: {e}")
            flash('Error loading trust data', 'error')
            return render_template('error.html', error=str(e))

    @ui.route('/trust/update', methods=['POST'])
    @require_login
    def trust_update():
        """Update trust score directly (manual tier adjustment)."""
        try:
            _, _, trust_manager, _, _, _, _, _, _, _, _ = _get_app_components_any(current_app)
            if not trust_manager:
                return jsonify({'error': 'Trust manager not available'}), 500

            data = request.get_json() or {}
            peer_id = (data.get('peer_id') or '').strip()
            tier = (data.get('tier') or '').strip().lower()
            score = data.get('score')
            reason = (data.get('reason') or '').strip() or None

            if not peer_id:
                return jsonify({'error': 'peer_id is required'}), 400

            if score is None:
                tier_map = {
                    'safe': 90,
                    'guarded': 65,
                    'restricted': 40,
                    'quarantine': 10
                }
                if tier not in tier_map:
                    return jsonify({'error': 'Invalid tier'}), 400
                score = tier_map[tier]

            reason_value = reason or (f"tier:{tier}" if tier else "manual")
            new_score = trust_manager.set_trust_score(peer_id, score, reason=reason_value)

            return jsonify({
                'success': True,
                'peer_id': peer_id,
                'trust_score': new_score,
                'is_trusted': new_score >= 50
            })
        except Exception as e:
            logger.error(f"Failed to update trust score: {e}")
            return jsonify({'error': 'Failed to update trust score'}), 500
    
    # Feed interface
    @ui.route('/feed')
    @require_login
    def feed():
        """Social feed interface for posts and timeline."""
        try:
            db_manager, _, _, _, _, file_manager, feed_manager, interaction_manager, profile_manager, config, p2p_manager = _get_app_components_any(current_app)
            user_id = get_current_user()
            
            # Get query parameters
            algorithm = request.args.get('algorithm', 'chronological')
            search_query = request.args.get('search', '').strip()
            
            from ..core.polls import parse_poll, resolve_poll_end, describe_poll_status, summarize_poll, poll_edit_window_seconds
            from ..core.tasks import parse_task_blocks, strip_task_blocks, derive_task_id
            from ..core.circles import parse_circle_blocks, strip_circle_blocks, derive_circle_id
            from ..core.handoffs import parse_handoff_blocks, strip_handoff_blocks, derive_handoff_id
            from ..core.objectives import parse_objective_blocks, strip_objective_blocks, derive_objective_id
            from ..core.requests import parse_request_blocks, strip_request_blocks, derive_request_id
            from ..core.signals import parse_signal_blocks, strip_signal_blocks, derive_signal_id
            from ..core.contracts import parse_contract_blocks, strip_contract_blocks, derive_contract_id
            now_dt = datetime.now(timezone.utc)
            task_manager = current_app.config.get('TASK_MANAGER')
            circle_manager = current_app.config.get('CIRCLE_MANAGER')
            handoff_manager = current_app.config.get('HANDOFF_MANAGER')
            skill_manager = current_app.config.get('SKILL_MANAGER')
            objective_manager = current_app.config.get('OBJECTIVE_MANAGER')
            request_manager = current_app.config.get('REQUEST_MANAGER')
            signal_manager = current_app.config.get('SIGNAL_MANAGER')
            contract_manager = current_app.config.get('CONTRACT_MANAGER')
            admin_user_id = db_manager.get_instance_owner_user_id() if db_manager else None
            user_display_cache: dict[str, dict[str, Any]] = {}

            def _user_display(uid: str) -> Optional[dict[str, Any]]:
                if not uid:
                    return None
                if uid in user_display_cache:
                    return user_display_cache[uid]
                display = {
                    'display_name': uid,
                    'avatar_url': None,
                    'origin_peer': None,
                }
                try:
                    if profile_manager:
                        profile = profile_manager.get_profile(uid)
                        if profile:
                            display['display_name'] = profile.display_name or profile.username or uid
                            display['avatar_url'] = profile.avatar_url
                            display['origin_peer'] = getattr(profile, 'origin_peer', None)
                    elif db_manager:
                        row = db_manager.get_user(uid)
                        if row:
                            display['display_name'] = row.get('display_name') or row.get('username') or uid
                            display['origin_peer'] = row.get('origin_peer')
                except Exception:
                    pass
                user_display_cache[uid] = display
                return display


            # Purge expired posts locally and broadcast delete signals for our own posts
            expired_posts = feed_manager.purge_expired_posts()
            if signal_manager:
                try:
                    signal_manager.purge_expired_signals()
                except Exception:
                    pass
            if expired_posts and file_manager:
                for post in expired_posts:
                    owner_id = post.get('author_id')
                    for file_id in post.get('attachment_ids') or []:
                        try:
                            file_info = file_manager.get_file(file_id)
                            if not file_info or file_info.uploaded_by != owner_id:
                                continue
                            if file_manager.is_file_referenced(file_id, exclude_feed_post_id=post.get('id')):
                                continue
                            file_manager.delete_file(file_id, owner_id)
                        except Exception:
                            continue
            if expired_posts and p2p_manager and p2p_manager.is_running():
                import secrets as _sec
                for post in expired_posts:
                    if post.get('author_id') != user_id:
                        continue
                    try:
                        signal_id = f"DS{_sec.token_hex(8)}"
                        p2p_manager.broadcast_delete_signal(
                            signal_id=signal_id,
                            data_type='feed_post',
                            data_id=post.get('id'),
                            reason='expired_ttl',
                        )
                    except Exception as p2p_err:
                        logger.warning(f"Failed to broadcast TTL delete for post {post.get('id')}: {p2p_err}")

            if search_query:
                # Show search results
                posts_obj = feed_manager.search_posts(search_query, user_id, limit=50)
            else:
                # Get user's feed posts
                posts_obj = feed_manager.get_user_feed(user_id, limit=50, algorithm=algorithm)

            # When opening /feed?post=ID or ?focus_post=ID, ensure that post is in the list so the link scrolls to it
            focus_post_id = request.args.get('post') or request.args.get('focus_post')
            if focus_post_id and focus_post_id not in [p.id for p in posts_obj]:
                focus_post = feed_manager.get_post(focus_post_id) if feed_manager else None
                if focus_post and focus_post.can_view(user_id, 50):
                    posts_obj = [focus_post] + posts_obj

            # Batch-check which posts the current user has liked
            post_ids = [p.id for p in posts_obj]
            user_liked_ids = interaction_manager.get_user_liked_ids(post_ids, user_id)

            # Convert Post objects to dicts for template
            posts = []
            for post in posts_obj:
                interactions = interaction_manager.get_post_interactions(post.id) if interaction_manager else {'total_likes': 0, 'comment_count': 0}
                post_dict = {
                    'id': post.id,
                    'author_id': post.author_id,
                    'content': post.content,
                    'created_at': post.created_at,
                    'expires_at': post.expires_at.isoformat() if getattr(post, 'expires_at', None) else None,
                    'post_type': post.post_type.value,
                    'visibility': post.visibility.value,
                    'metadata': post.metadata,
                    'permissions': post.permissions,
                    'likes': interactions['total_likes'],
                    'comments': interactions['comment_count'],
                    'user_has_liked': post.id in user_liked_ids,
                    'source_type': post.source_type or 'human',
                    'source_agent_id': post.source_agent_id,
                    'source_url': post.source_url,
                    'tags': post.tags_list,
                }

                # Inline circle blocks (for [circle]...[/circle] posts)
                display_content = post.content
                circles_payload = []
                circle_specs = parse_circle_blocks(post.content or '')
                if circle_specs and circle_manager:
                    for idx, spec in enumerate(cast(Any, circle_specs)):
                        spec = cast(Any, spec)
                        circle_id = derive_circle_id('feed', post.id, idx, len(circle_specs), override=spec.circle_id)
                        facilitator_id = None
                        if spec.facilitator:
                            facilitator_id = _resolve_handle_to_user_id(
                                db_manager,
                                spec.facilitator,
                                visibility=post.visibility.value,
                                permissions=post.permissions,
                                author_id=post.author_id,
                            )
                        if not facilitator_id:
                            facilitator_id = post.author_id
                        if spec.participants is not None:
                            resolved_participants = _resolve_handle_list(
                                db_manager,
                                spec.participants,
                                visibility=post.visibility.value,
                                permissions=post.permissions,
                                author_id=post.author_id,
                            )
                            spec.participants = resolved_participants

                        circle_obj = circle_manager.upsert_circle(
                            circle_id=circle_id,
                            source_type='feed',
                            source_id=post.id,
                            created_by=post.author_id,
                            spec=spec,
                            facilitator_id=facilitator_id,
                            visibility=post.visibility.value,
                            origin_peer=getattr(post, 'origin_peer', None),
                            created_at=post.created_at.isoformat() if getattr(post, 'created_at', None) else None,
                        )
                        if circle_obj:
                            circle_payload = circle_obj.to_dict()
                            circle_payload['phase_label'] = (circle_payload.get('phase') or 'opinion').replace('_', ' ').title()
                            circle_payload['entries_count'] = circle_manager.count_entries(circle_obj.id)
                            facilitator_display = _user_display(circle_obj.facilitator_id)
                            if facilitator_display:
                                circle_payload['facilitator_name'] = facilitator_display.get('display_name')
                                circle_payload['facilitator_avatar_url'] = facilitator_display.get('avatar_url')
                                circle_payload['facilitator_origin_peer'] = facilitator_display.get('origin_peer')
                            circles_payload.append(circle_payload)
                    display_content = strip_circle_blocks(display_content or '')

                # Inline objective blocks (for [objective]...[/objective] posts)
                objectives_payload = []
                objective_specs = parse_objective_blocks(post.content or '')
                if objective_specs and objective_manager:
                    objective_visibility = 'network' if post.visibility.value in ('public', 'network') else 'local'
                    for idx, spec in enumerate(cast(Any, objective_specs)):
                        spec = cast(Any, spec)
                        objective_id = derive_objective_id('feed', post.id, idx, len(objective_specs), override=spec.objective_id)
                        members_payload = []
                        for member in spec.members or []:
                            uid = _resolve_handle_to_user_id(
                                db_manager,
                                member.handle,
                                visibility=post.visibility.value,
                                permissions=post.permissions,
                                author_id=post.author_id,
                            )
                            if uid:
                                members_payload.append({'user_id': uid, 'role': member.role})
                        tasks_payload = []
                        for t in spec.tasks or []:
                            assignee_id = None
                            if t.assignee:
                                assignee_id = _resolve_handle_to_user_id(
                                    db_manager,
                                    t.assignee,
                                    visibility=post.visibility.value,
                                    permissions=post.permissions,
                                    author_id=post.author_id,
                                )
                            tasks_payload.append({
                                'title': t.title,
                                'status': t.status,
                                'assigned_to': assignee_id,
                                'metadata': {
                                    'inline_objective_task': True,
                                    'source_type': 'feed_post',
                                    'source_id': post.id,
                                    'post_visibility': post.visibility.value,
                                },
                            })
                        objective_obj = objective_manager.upsert_objective(
                            objective_id=objective_id,
                            title=spec.title,
                            description=spec.description,
                            status=spec.status,
                            deadline=spec.deadline.isoformat() if spec.deadline else None,
                            created_by=post.author_id,
                            visibility=objective_visibility,
                            origin_peer=getattr(post, 'origin_peer', None),
                            source_type='feed_post',
                            source_id=post.id,
                            created_at=post.created_at.isoformat() if getattr(post, 'created_at', None) else None,
                            members=members_payload,
                            tasks=tasks_payload,
                            updated_by=post.author_id,
                        )
                        if objective_obj:
                            payload = objective_obj
                            payload['status_label'] = (payload.get('status') or 'pending').replace('_', ' ').title()
                            payload['progress_percent'] = payload.get('progress_percent') or 0
                            payload['tasks_total'] = payload.get('tasks_total') or 0
                            payload['tasks_done'] = payload.get('tasks_done') or 0
                            deadline_dt = None
                            if payload.get('deadline'):
                                try:
                                    deadline_dt = datetime.fromisoformat(payload['deadline'].replace('Z', '+00:00'))
                                except Exception:
                                    deadline_dt = None
                            payload['deadline_label'] = _objective_deadline_label(deadline_dt, now_dt)

                            members_display = []
                            for member in payload.get('members', []) or []:
                                uid = member.get('user_id')
                                display = _user_display(uid) if uid else None
                                members_display.append({
                                    'user_id': uid,
                                    'role': member.get('role') or 'contributor',
                                    'display_name': (display or {}).get('display_name') if display else uid,
                                    'avatar_url': (display or {}).get('avatar_url') if display else None,
                                    'origin_peer': (display or {}).get('origin_peer') if display else None,
                                })
                            payload['members'] = members_display

                            tasks_display = []
                            for task in payload.get('tasks', []) or []:
                                status_val = (task.get('status') or 'open')
                                assignee_id = task.get('assigned_to') or None
                                assignee_display = _user_display(assignee_id) if assignee_id else None
                                tasks_display.append({
                                    'id': task.get('id'),
                                    'title': task.get('title'),
                                    'status': status_val,
                                    'status_label': status_val.replace('_', ' ').title(),
                                    'assignee_id': assignee_id,
                                    'assignee_name': (assignee_display or {}).get('display_name') if assignee_display else assignee_id,
                                    'assignee_avatar_url': (assignee_display or {}).get('avatar_url') if assignee_display else None,
                                    'assignee_origin_peer': (assignee_display or {}).get('origin_peer') if assignee_display else None,
                                })
                            payload['tasks'] = tasks_display

                            objectives_payload.append(payload)
                    display_content = strip_objective_blocks(display_content or '')

                # Inline request blocks (for [request]...[/request] posts)
                requests_payload = []
                request_specs = parse_request_blocks(post.content or '')
                if request_specs and request_manager:
                    request_visibility = 'network' if post.visibility.value in ('public', 'network') else 'local'
                    for idx, spec in enumerate(cast(Any, request_specs)):
                        spec = cast(Any, spec)
                        if not spec.confirmed:
                            continue
                        request_id = derive_request_id('feed', post.id, idx, len(request_specs), override=spec.request_id)
                        members_payload = []
                        for member in spec.members or []:
                            uid = _resolve_handle_to_user_id(
                                db_manager,
                                member.handle,
                                visibility=post.visibility.value,
                                permissions=post.permissions,
                                author_id=post.author_id,
                            )
                            if uid:
                                members_payload.append({'user_id': uid, 'role': member.role})

                        req_obj = request_manager.upsert_request(
                            request_id=request_id,
                            title=spec.title,
                            created_by=post.author_id,
                            request_text=spec.request,
                            required_output=spec.required_output,
                            status=spec.status,
                            priority=spec.priority,
                            tags=spec.tags,
                            due_at=spec.due_at.isoformat() if spec.due_at else None,
                            visibility=request_visibility,
                            origin_peer=getattr(post, 'origin_peer', None),
                            source_type='feed_post',
                            source_id=post.id,
                            created_at=post.created_at.isoformat() if getattr(post, 'created_at', None) else None,
                            actor_id=post.author_id,
                            members=members_payload,
                            members_defined=('members' in spec.fields),
                            fields=spec.fields,
                        )
                        if req_obj:
                            payload = req_obj
                            payload['status_label'] = (payload.get('status') or 'open').replace('_', ' ').title()
                            payload['priority_label'] = (payload.get('priority') or 'normal').replace('_', ' ').title()
                            due_dt = None
                            if payload.get('due_at'):
                                try:
                                    due_dt = datetime.fromisoformat(payload['due_at'].replace('Z', '+00:00'))
                                except Exception:
                                    due_dt = None
                            payload['due_label'] = _request_due_label(due_dt, now_dt)
                            member_ids = [m.get('user_id') for m in payload.get('members', []) if m.get('user_id')]
                            payload['can_manage'] = (
                                user_id == payload.get('created_by')
                                or (admin_user_id and user_id == admin_user_id)
                                or (user_id in member_ids)
                            )

                            members_display = []
                            for member in payload.get('members', []) or []:
                                uid = member.get('user_id')
                                display = _user_display(uid) if uid else None
                                members_display.append({
                                    'user_id': uid,
                                    'role': member.get('role') or 'assignee',
                                    'display_name': (display or {}).get('display_name') if display else uid,
                                    'avatar_url': (display or {}).get('avatar_url') if display else None,
                                    'origin_peer': (display or {}).get('origin_peer') if display else None,
                                })
                            payload['members'] = members_display
                            requests_payload.append(payload)
                    display_content = strip_request_blocks(display_content or '', remove_unconfirmed=False)

                # Inline signal blocks (for [signal]...[/signal] posts)
                signals_payload = []
                signal_specs = parse_signal_blocks(post.content or '')
                if signal_specs and signal_manager:
                    signal_visibility = 'network' if post.visibility.value in ('public', 'network') else 'local'
                    for idx, spec in enumerate(cast(Any, signal_specs)):
                        spec = cast(Any, spec)
                        signal_id = derive_signal_id('feed', post.id, idx, len(signal_specs), override=spec.signal_id)
                        owner_id = None
                        if spec.owner:
                            owner_id = _resolve_handle_to_user_id(
                                db_manager,
                                spec.owner,
                                visibility=post.visibility.value,
                                permissions=post.permissions,
                                author_id=post.author_id,
                            )
                        if not owner_id:
                            owner_id = post.author_id

                        signal_obj = signal_manager.upsert_signal(
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
                            created_by=post.author_id,
                            visibility=signal_visibility,
                            origin_peer=getattr(post, 'origin_peer', None),
                            source_type='feed_post',
                            source_id=post.id,
                            expires_at=spec.expires_at.isoformat() if spec.expires_at else None,
                            ttl_seconds=spec.ttl_seconds,
                            ttl_mode=spec.ttl_mode,
                            created_at=post.created_at.isoformat() if getattr(post, 'created_at', None) else None,
                            actor_id=post.author_id,
                        )
                        if signal_obj:
                            payload = signal_obj
                            payload['status_label'] = (payload.get('status') or 'active').replace('_', ' ').title()
                            payload['type_label'] = (payload.get('type') or 'signal').replace('_', ' ').title()
                            expiry_dt = None
                            if payload.get('expires_at'):
                                try:
                                    expiry_dt = datetime.fromisoformat(payload['expires_at'].replace('Z', '+00:00'))
                                except Exception:
                                    expiry_dt = None
                            payload['expires_label'] = _signal_expiry_label(expiry_dt)
                            payload['expires_at'] = payload.get('expires_at')
                            payload['confidence_percent'] = int(round((payload.get('confidence') or 0) * 100))
                            owner_display = _user_display(payload.get('owner_id'))
                            payload['owner_name'] = (owner_display or {}).get('display_name') if owner_display else payload.get('owner_id')
                            payload['owner_avatar_url'] = (owner_display or {}).get('avatar_url') if owner_display else None
                            payload['owner_origin_peer'] = (owner_display or {}).get('origin_peer') if owner_display else None
                            payload['can_manage'] = user_id == payload.get('owner_id') or (admin_user_id and user_id == admin_user_id)
                            try:
                                if payload.get('data') is not None:
                                    payload['data_pretty'] = json.dumps(payload.get('data'), indent=2)
                            except Exception:
                                payload['data_pretty'] = None
                            signals_payload.append(payload)
                    display_content = strip_signal_blocks(display_content or '')

                # Inline contract blocks (for [contract]...[/contract] posts)
                contracts_payload = []
                contract_specs = parse_contract_blocks(post.content or '')
                if contract_specs and contract_manager:
                    contract_visibility = 'network' if post.visibility.value in ('public', 'network') else 'local'
                    for idx, spec in enumerate(contract_specs):
                        if not spec.confirmed:
                            continue
                        contract_id = derive_contract_id('feed', post.id, idx, len(contract_specs), override=spec.contract_id)
                        owner_id = None
                        if spec.owner:
                            owner_id = _resolve_handle_to_user_id(
                                db_manager,
                                spec.owner,
                                visibility=post.visibility.value,
                                permissions=post.permissions,
                                author_id=post.author_id,
                            )
                        if not owner_id:
                            owner_id = post.author_id

                        counterparties = []
                        for cp in spec.counterparties or []:
                            cp_id = _resolve_handle_to_user_id(
                                db_manager,
                                cp,
                                visibility=post.visibility.value,
                                permissions=post.permissions,
                                author_id=post.author_id,
                            )
                            if cp_id:
                                counterparties.append(cp_id)

                        contract_obj = contract_manager.upsert_contract(
                            contract_id=contract_id,
                            title=spec.title,
                            summary=spec.summary,
                            terms=spec.terms,
                            status=spec.status,
                            owner_id=owner_id,
                            counterparties=counterparties,
                            created_by=post.author_id,
                            visibility=contract_visibility,
                            origin_peer=getattr(post, 'origin_peer', None),
                            source_type='feed_post',
                            source_id=post.id,
                            expires_at=spec.expires_at.isoformat() if spec.expires_at else None,
                            ttl_seconds=spec.ttl_seconds,
                            ttl_mode=spec.ttl_mode,
                            metadata=spec.metadata,
                            created_at=post.created_at.isoformat() if getattr(post, 'created_at', None) else None,
                            actor_id=post.author_id,
                        )
                        if contract_obj:
                            payload = contract_obj
                            payload['status_label'] = (payload.get('status') or 'proposed').replace('_', ' ').title()
                            expiry_dt = None
                            if payload.get('expires_at'):
                                try:
                                    expiry_dt = datetime.fromisoformat(payload['expires_at'].replace('Z', '+00:00'))
                                except Exception:
                                    expiry_dt = None
                            payload['expires_label'] = _signal_expiry_label(expiry_dt)
                            owner_display = _user_display(payload.get('owner_id'))
                            payload['owner_name'] = (owner_display or {}).get('display_name') if owner_display else payload.get('owner_id')
                            payload['owner_avatar_url'] = (owner_display or {}).get('avatar_url') if owner_display else None
                            payload['owner_origin_peer'] = (owner_display or {}).get('origin_peer') if owner_display else None
                            counterparty_payload = []
                            for cp_id in payload.get('counterparties') or []:
                                cp_display = _user_display(cp_id)
                                counterparty_payload.append({
                                    'user_id': cp_id,
                                    'display_name': (cp_display or {}).get('display_name') if cp_display else cp_id,
                                    'avatar_url': (cp_display or {}).get('avatar_url') if cp_display else None,
                                    'origin_peer': (cp_display or {}).get('origin_peer') if cp_display else None,
                                })
                            payload['counterparty_users'] = counterparty_payload
                            allowed_ids = {payload.get('owner_id'), payload.get('created_by')}
                            allowed_ids.update(set(payload.get('counterparties') or []))
                            payload['can_manage'] = (
                                user_id == payload.get('owner_id')
                                or user_id == payload.get('created_by')
                                or (admin_user_id and user_id == admin_user_id)
                            )
                            payload['can_participate'] = bool(user_id and user_id in allowed_ids)
                            contracts_payload.append(payload)
                    display_content = strip_contract_blocks(display_content or '', remove_unconfirmed=False)

                # Inline task blocks (for [task]...[/task] posts)
                inline_tasks = []
                task_specs = parse_task_blocks(post.content or '')
                if task_specs:
                    for idx, spec in enumerate(cast(Any, task_specs)):
                        spec = cast(Any, spec)
                        if not spec.confirmed:
                            continue
                        task_id = derive_task_id('feed', post.id, idx, len(task_specs), override=spec.task_id)
                        task_obj = task_manager.get_task(task_id) if task_manager else None
                        task_payload = task_obj.to_dict() if task_obj else spec.to_dict()
                        task_payload['id'] = task_id
                        task_payload['status'] = (task_payload.get('status') or spec.status or 'open')
                        task_payload['priority'] = (task_payload.get('priority') or spec.priority or 'normal')
                        task_payload['status_label'] = task_payload['status'].replace('_', ' ').title()
                        task_payload['priority_label'] = task_payload['priority'].title()
                        task_payload['due_at_label'] = None
                        if task_payload.get('due_at'):
                            try:
                                due_dt = datetime.fromisoformat(task_payload['due_at'].replace('Z', '+00:00'))
                                task_payload['due_at_label'] = due_dt.date().isoformat()
                            except Exception:
                                task_payload['due_at_label'] = None
                        assignee_id = task_payload.get('assigned_to') or None
                        if assignee_id:
                            display = _user_display(assignee_id)
                            if display:
                                task_payload['assignee_id'] = assignee_id
                                task_payload['assignee_name'] = display.get('display_name') or assignee_id
                                task_payload['assignee_avatar_url'] = display.get('avatar_url')
                                task_payload['assignee_origin_peer'] = display.get('origin_peer')
                                task_payload['assignee_label'] = f"Assigned to {display.get('display_name') or assignee_id}"
                        if not task_payload.get('assignee_label'):
                            task_payload['assignee_label'] = 'Unassigned'
                        inline_tasks.append(task_payload)
                    display_content = strip_task_blocks(display_content or '', remove_unconfirmed=False)

                # Inline handoff blocks (for [handoff]...[/handoff] posts)
                inline_handoffs = []
                handoff_specs = parse_handoff_blocks(post.content or '')
                if handoff_specs:
                    for idx, spec in enumerate(cast(Any, handoff_specs)):
                        spec = cast(Any, spec)
                        if not spec.confirmed:
                            continue
                        handoff_id = derive_handoff_id('feed', post.id, idx, len(handoff_specs), override=spec.handoff_id)
                        handoff_obj = handoff_manager.get_handoff(handoff_id) if handoff_manager else None
                        if not handoff_obj and handoff_manager:
                            handoff_obj = handoff_manager.upsert_handoff(
                                handoff_id=handoff_id,
                                source_type='feed',
                                source_id=post.id,
                                author_id=post.author_id,
                                title=spec.title,
                                summary=spec.summary,
                                next_steps=spec.next_steps,
                                owner=spec.owner,
                                tags=spec.tags,
                                raw=spec.raw,
                                channel_id=None,
                                visibility=post.visibility.value,
                                origin_peer=getattr(post, 'origin_peer', None),
                                permissions=post.permissions,
                                created_at=post.created_at.isoformat() if getattr(post, 'created_at', None) else None,
                                required_capabilities=spec.required_capabilities,
                                escalation_level=spec.escalation_level,
                                return_to=spec.return_to,
                                context_payload=spec.context_payload,
                            )
                        payload = handoff_obj.to_dict() if handoff_obj else spec.to_dict()
                        payload['id'] = handoff_id
                        owner_id = payload.get('owner') or payload.get('owner_id')
                        if owner_id:
                            display = _user_display(owner_id)
                            if display:
                                payload['owner_name'] = display.get('display_name') or owner_id
                                payload['owner_avatar_url'] = display.get('avatar_url')
                                payload['owner_origin_peer'] = display.get('origin_peer')
                        return_to_id = payload.get('return_to')
                        if return_to_id:
                            return_display = _user_display(return_to_id)
                            if return_display:
                                payload['return_to_name'] = return_display.get('display_name') or return_to_id
                        inline_handoffs.append(payload)
                    display_content = strip_handoff_blocks(display_content or '', remove_unconfirmed=False)

                # Inline skills for feed posts
                inline_skills = []
                try:
                    from ..core.skills import parse_skill_blocks as _psb_f, strip_skill_blocks as _ssb_f
                    skill_specs_f = _psb_f(post.content or '')
                    if skill_specs_f:
                        for spec in skill_specs_f:
                            inline_skills.append(
                                _build_inline_skill_payload(skill_manager, spec, 'feed_post', post.id)
                            )
                        display_content = _ssb_f(display_content or '')
                except Exception:
                    pass

                post_dict['display_content'] = display_content
                if circles_payload:
                    post_dict['circles'] = circles_payload
                if objectives_payload:
                    post_dict['objectives'] = objectives_payload
                if requests_payload:
                    post_dict['requests'] = requests_payload
                if signals_payload:
                    post_dict['signals'] = signals_payload
                if contracts_payload:
                    post_dict['contracts'] = contracts_payload
                if inline_tasks:
                    post_dict['inline_tasks'] = inline_tasks
                if inline_handoffs:
                    post_dict['handoffs'] = inline_handoffs
                if inline_skills:
                    post_dict['skills'] = inline_skills
                post_notes = _load_target_notes(skill_manager, 'feed_post', post.id, user_id, limit=8)
                post_dict['community_notes_count'] = len(post_notes)
                if post_notes:
                    post_dict['community_notes'] = post_notes

                poll_spec = parse_poll(post.content or '')
                if poll_spec and interaction_manager:
                    poll_end = resolve_poll_end(post.created_at, post.expires_at, poll_spec)
                    is_closed = bool(poll_end and poll_end <= now_dt)
                    results = interaction_manager.get_poll_results(post.id, 'feed', len(poll_spec.options))
                    user_vote = interaction_manager.get_user_poll_vote(post.id, 'feed', user_id)
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
                    post_dict['poll'] = {
                        'question': poll_spec.question,
                        'options': option_payload,
                        'ends_at': poll_end.isoformat() if poll_end else None,
                        'status_label': status_label,
                        'is_closed': is_closed,
                        'user_vote': user_vote,
                        'total_votes': total_votes,
                    }
                    post_dict['post_type'] = 'poll'

                    # Notify peers once when a local-authored poll closes.
                    if is_closed and interaction_manager and db_manager:
                        author = db_manager.get_user(post.author_id) if db_manager else None
                        is_local_author = author and not author.get('origin_peer')
                        if is_local_author:
                            summary = summarize_poll(poll_spec.question, poll_spec.options, results['counts'])
                            if interaction_manager.mark_poll_closed(post.id, 'feed', summary=summary):
                                if p2p_manager and p2p_manager.is_running():
                                    try:
                                        display_name = None
                                        if profile_manager:
                                            profile = profile_manager.get_profile(post.author_id)
                                            if profile:
                                                display_name = profile.display_name or profile.username
                                        p2p_manager.broadcast_interaction(
                                            item_id=post.id,
                                            user_id=post.author_id,
                                            action='poll_closed',
                                            item_type='poll',
                                            display_name=display_name,
                                            extra={
                                                'poll_id': post.id,
                                                'poll_kind': 'feed',
                                                'preview': summary,
                                                'summary': summary,
                                            }
                                        )
                                    except Exception as p2p_err:
                                        logger.warning(f"Failed to broadcast poll closure: {p2p_err}")
                posts.append(post_dict)

            feed_stats = {
                'total_posts': len(posts),
                'unique_authors': len(set(post['author_id'] for post in posts)),
                'algorithm': algorithm
            }

            template_data = {
                'posts': posts,
                'feed_stats': feed_stats,
                'user_id': user_id,
                'algorithm': algorithm,
                'is_admin': _is_admin(),
                'poll_edit_window_seconds': poll_edit_window_seconds(),
            }
            if search_query:
                template_data['search_query'] = search_query
            return render_template('feed.html', **template_data)
                
        except Exception as e:
            logger.error(f"Feed error: {e}")
            flash('Error loading feed', 'error')
            return render_template('error.html', error=str(e))

    @ui.route('/tasks')
    @require_login
    def tasks_page():
        """Task board interface for collaborative work."""
        user_id = get_current_user()
        display_name = session.get('display_name') or session.get('username') or user_id
        return render_template('tasks.html', current_user_id=user_id, current_user_name=display_name)

    # Channel message deep-link: redirect to /channels?channel=X&focus_message=Y so the UI can scroll to the message
    @ui.route('/channels/locate')
    @require_login
    def channels_locate():
        """Redirect to the channel that contains the given message_id, with focus_message set so the UI scrolls to it."""
        message_id = (request.args.get('message_id') or '').strip()
        if not message_id:
            return redirect(url_for('ui.channels'))
        try:
            db_manager, _, _, _, channel_manager, *_ = _get_app_components_any(current_app)
            user_id = get_current_user()
            if not channel_manager or not channel_manager.db:
                return redirect(url_for('ui.channels'))
            with channel_manager.db.get_connection() as conn:
                row = conn.execute(
                    "SELECT channel_id FROM channel_messages WHERE id = ?",
                    (message_id,),
                ).fetchone()
            if not row:
                return redirect(url_for('ui.channels'))
            channel_id = row['channel_id']
            # Ensure user is a member so they can see the channel
            if channel_manager.get_member_role(channel_id, user_id) is None:
                return redirect(url_for('ui.channels'))
            return redirect(url_for('ui.channels', channel=channel_id, focus_message=message_id))
        except Exception:
            return redirect(url_for('ui.channels'))

    # Channels interface (Slack-style)
    @ui.route('/channels')
    @require_login
    def channels():
        """Slack-style channel interface for real-time messaging."""
        try:
            _, _, _, _, channel_manager, _, _, _, _, config, p2p_manager = _get_app_components_any(current_app)
            from ..core.polls import poll_edit_window_seconds
            user_id = get_current_user()
            
            # Get user's channels
            channels = channel_manager.get_user_channels(user_id)
            logger.debug(f"Channels page: user_id={user_id}, channels_count={len(channels)}")
            for channel in channels:
                logger.debug(f"Channel: id={channel.id}, name={channel.name}, type={channel.channel_type}")
            
            # Gather peer device profiles for origin display
            peer_device_profiles = channel_manager.get_all_peer_device_profiles()
            
            # Also include local device info
            try:
                from canopy.core.device import get_device_profile, get_device_id
                local_device = get_device_profile()
                local_device['device_id'] = get_device_id()
            except Exception:
                local_device = {}

            local_peer_id = None
            try:
                if p2p_manager:
                    local_peer_id = p2p_manager.get_peer_id()
            except Exception:
                local_peer_id = None

            return render_template('channels.html',
                                 channels=channels,
                                 user_id=user_id,
                                 config=config,
                                 peer_device_profiles=peer_device_profiles,
                                 local_device=local_device,
                                 local_peer_id=local_peer_id,
                                 is_admin=_is_admin(),
                                 poll_edit_window_seconds=poll_edit_window_seconds())
                                 
        except Exception as e:
            logger.error(f"Channels error: {e}")
            flash('Error loading channels', 'error')
            return render_template('error.html', error=str(e))

    # Settings
    @ui.route('/settings')
    @require_login
    def settings():
        """Application settings and configuration."""
        try:
            db_manager, _, _, _, _, _, _, _, _, config, _ = _get_app_components_any(current_app)
            user_id = get_current_user()
            
            # Get database statistics
            db_stats = db_manager.get_database_stats()
            # Env-only options (read-only in UI; document in Settings)
            auto_approve_raw = (os.getenv('CANOPY_AUTO_APPROVE_AGENTS') or '').strip().lower()
            auto_approve_agents = auto_approve_raw in ('1', 'true', 'yes')
            return render_template('settings.html',
                                 config=config.to_dict(),
                                 db_stats=db_stats,
                                 user_id=user_id,
                                 auto_approve_agents=auto_approve_agents,
                                 is_admin=_is_admin())
                                 
        except Exception as e:
            logger.error(f"Settings error: {e}")
            flash('Error loading settings', 'error')
            return render_template('error.html', error=str(e))

    # Claim instance admin — when no owner, or recovery with CANOPY_ADMIN_CLAIM_SECRET
    @ui.route('/claim-admin', methods=['GET', 'POST'])
    @require_login
    def claim_admin():
        """Let a logged-in human become instance admin when no owner exists, or with recovery secret."""
        db_manager, _, _, _, _, _, _, _, _, _, _ = _get_app_components_any(current_app)
        owner_id = db_manager.get_instance_owner_user_id()
        current_user_id = session.get('user_id')
        claim_secret = os.environ.get('CANOPY_ADMIN_CLAIM_SECRET', '').strip()
        already_admin = owner_id and current_user_id == owner_id

        if request.method == 'GET':
            if already_admin:
                flash('You are already the instance admin.', 'info')
                return redirect(url_for('ui.admin_page'))
            # No owner: any logged-in user can claim
            if not owner_id:
                return render_template('claim_admin.html', needs_secret=False, claim_secret_configured=False)
            # Owner exists: need recovery secret
            if not claim_secret:
                flash('An admin already exists. To take over, set CANOPY_ADMIN_CLAIM_SECRET and use the recovery flow.', 'warning')
                return redirect(url_for('ui.dashboard'))
            return render_template('claim_admin.html', needs_secret=True, claim_secret_configured=True)

        # POST: perform claim
        if already_admin:
            return redirect(url_for('ui.admin_page'))
        if not owner_id:
            db_manager.set_instance_owner_user_id(current_user_id)
            flash('You are now the instance admin.', 'success')
            return redirect(url_for('ui.admin_page'))
        # Recovery: require secret
        secret = os.environ.get('CANOPY_ADMIN_CLAIM_SECRET', '').strip()
        if not secret:
            flash('Recovery secret is not configured.', 'error')
            return redirect(url_for('ui.dashboard'))
        submitted = (request.form.get('secret') or '').strip()
        if not submitted or submitted != secret:
            flash('Invalid recovery secret.', 'error')
            return render_template('claim_admin.html', needs_secret=True, claim_secret_configured=True)
        db_manager.set_instance_owner_user_id(current_user_id)
        flash('You are now the instance admin. The previous admin has been replaced.', 'success')
        return redirect(url_for('ui.admin_page'))

    # Admin — user/agent management (instance owner only)
    @ui.route('/admin')
    @require_login
    @require_admin
    def admin_page():
        """Admin page: pending agent approvals, all users, approve/suspend/delete."""
        try:
            db_manager, _, _, _, _, _, _, _, _, _, _ = _get_app_components_any(current_app)
            _, api_key_manager, _, _, _, _, _, _, _, _, _ = _get_app_components_any(current_app)
            skill_manager = current_app.config.get('SKILL_MANAGER')
            users = db_manager.get_all_users_for_admin()
            _annotate_user_presence(users, db_manager)
            pending = [
                u for u in users
                if u.get('is_registered') and (u.get('status') or 'active') == 'pending_approval'
            ]
            active_agents = [
                u for u in users
                if u.get('is_registered')
                and (u.get('account_type') or 'human') == 'agent'
                and (u.get('status') or 'active') == 'active'
            ]
            for u in users:
                if not u.get('is_registered'):
                    u['agent_directives_source'] = 'n/a'
                    u['agent_directives_effective'] = ''
                    u['agent_directives_custom'] = ''
                    u['agent_directives_default'] = ''
                    u['agent_directives_preview'] = 'Unregistered shadow/replica user.'
                    u['agent_directives_length'] = 0
                    continue
                state = _effective_agent_directive_state(u)
                u['agent_directives_source'] = state['source']
                u['agent_directives_effective'] = state['effective']
                u['agent_directives_custom'] = state['custom']
                u['agent_directives_default'] = state['default']
                u['agent_directives_preview'] = (state['effective'] or '')[:140]
                u['agent_directives_length'] = len(state['effective'] or '')
            # Show LOCAL users eligible for directive management.
            # Only local users (origin_peer is NULL) — directives are instance-local
            # and have no effect on remote agents that never call this peer's API.
            _owner_id = db_manager.get_instance_owner_user_id()
            _agent_patterns = {'bot', 'claw', 'agent', 'codex', 'copilot', 'assistant'}
            def _looks_like_agent(u):
                uname = (u.get('username') or '').lower()
                dname = (u.get('display_name') or '').lower()
                combined = uname + ' ' + dname
                return any(p in combined for p in _agent_patterns)
            def _is_local(u):
                return not u.get('origin_peer')
            agent_users = [
                u for u in users
                if u.get('is_registered') and _is_local(u) and (
                    (u.get('account_type') or 'human') == 'agent'
                    or u.get('agent_directives')
                    or _looks_like_agent(u)
                )
            ]
            workspace_seed = [u for u in users if u.get('is_registered')]
            workspace_users = sorted(
                workspace_seed,
                key=lambda u: (
                    0 if (u.get('account_type') or 'human') == 'agent' else 1,
                    0 if (u.get('status') or 'active') == 'active' else 1,
                    (u.get('display_name') or u.get('username') or '').lower(),
                ),
            )
            all_permissions = [p.value for p in api_key_manager.get_all_permissions()]
            current_user_id = get_current_user()
            current_user_row = db_manager.get_user(current_user_id) if db_manager else None
            heartbeat_snapshot = _build_agent_heartbeat_snapshot(current_user_id)

            community_note_counts = {
                'total': 0,
                'proposed': 0,
                'accepted': 0,
                'rejected': 0,
            }
            community_note_queue = []
            top_skill_trust = []

            if skill_manager:
                try:
                    with db_manager.get_connection() as conn:
                        rows = conn.execute(
                            "SELECT status, COUNT(*) AS cnt FROM community_notes GROUP BY status"
                        ).fetchall()
                        total = conn.execute(
                            "SELECT COUNT(*) AS cnt FROM community_notes"
                        ).fetchone()
                    community_note_counts['total'] = int(total['cnt']) if total and total['cnt'] is not None else 0
                    for row in rows or []:
                        status = (row['status'] or '').lower()
                        if status in community_note_counts:
                            community_note_counts[status] = int(row['cnt'] or 0)
                except Exception:
                    pass

                try:
                    queued = skill_manager.get_community_notes(status='proposed', limit=10)
                    community_note_queue = _serialize_community_notes(queued, current_user_id)
                except Exception:
                    community_note_queue = []

                try:
                    skills = skill_manager.get_skills(limit=24)
                    for skill in skills or []:
                        trust_data = skill_manager.get_skill_trust_score(skill['id'])
                        trust_score = trust_data.get('trust_score')
                        components = trust_data.get('components') or {}
                        top_skill_trust.append({
                            'id': skill['id'],
                            'name': skill.get('name') or skill.get('id'),
                            'version': skill.get('version') or '',
                            'author_id': skill.get('author_id'),
                            'trust_score': trust_score,
                            'trust_percent': int(round(trust_score * 100)) if trust_score is not None else None,
                            'endorsement_count': int((components or {}).get('endorsement_count') or 0),
                            'invocation_count': int((components or {}).get('invocation_count') or 0),
                            'success_rate': components.get('success_rate'),
                        })
                    top_skill_trust.sort(
                        key=lambda x: (x.get('trust_score') is None, -(x.get('trust_score') or 0.0), -(x.get('endorsement_count') or 0)),
                    )
                    top_skill_trust = top_skill_trust[:8]
                except Exception:
                    top_skill_trust = []

            return render_template('admin.html',
                                 users=users,
                                 pending_count=len(pending),
                                 active_agents_count=len(active_agents),
                                 all_permissions=all_permissions,
                                 agent_users=agent_users,
                                 workspace_users=workspace_users,
                                 directive_presets=_agent_directive_presets_payload(),
                                 directive_max_length=MAX_AGENT_DIRECTIVES_LENGTH,
                                 heartbeat_snapshot=heartbeat_snapshot,
                                 current_user_is_agent=bool((current_user_row or {}).get('account_type') == 'agent'),
                                 community_note_counts=community_note_counts,
                                 community_note_queue=community_note_queue,
                                 top_skill_trust=top_skill_trust)
        except Exception as e:
            logger.error(f"Admin page error: {e}")
            flash('Error loading admin page', 'error')
            return render_template('error.html', error=str(e))

    # Connect / invite page
    @ui.route('/connect')
    @require_login
    def connect_page():
        """Peer connection and invite code page."""
        from ..network.invite import generate_invite, get_local_ips
        try:
            _, _, _, _, _, _, _, _, _, config, p2p_manager = _get_app_components_any(current_app)
            user_id = get_current_user()

            invite_code = None
            peer_id = None
            endpoints = []
            local_ips = get_local_ips()

            if p2p_manager and p2p_manager.identity_manager.local_identity:
                mesh_port = config.network.mesh_port if config else 7771
                invite = generate_invite(p2p_manager.identity_manager, mesh_port)
                invite_code = invite.encode()
                peer_id = invite.peer_id
                endpoints = invite.endpoints

            # Connected / discovered peers
            connected_peers = p2p_manager.get_connected_peers() if p2p_manager else []
            discovered_peers = p2p_manager.get_discovered_peers() if p2p_manager else []

            # Peers introduced by contacts
            introduced_peers = p2p_manager.get_introduced_peers() if p2p_manager else []

            # Relay status
            relay_status = p2p_manager.get_relay_status() if p2p_manager else {}
            active_relays = {}
            try:
                active_relays = dict((relay_status or {}).get('active_relays') or {})
            except Exception:
                active_relays = {}

            # Known peers (for reconnect)
            known_peers = []
            if p2p_manager:
                im = p2p_manager.identity_manager
                connected_set = set(connected_peers)
                relayed_set = set(active_relays.keys())
                for pid, identity in im.known_peers.items():
                    if identity.is_local():
                        continue
                    connection_type = 'offline'
                    if pid in connected_set:
                        connection_type = 'direct'
                    elif pid in relayed_set:
                        connection_type = 'relayed'
                    known_peers.append({
                        'peer_id': pid,
                        'display_name': im.peer_display_names.get(pid, ''),
                        'endpoints': im.peer_endpoints.get(pid, []),
                        'connected': connection_type in {'direct', 'relayed'},
                        'connection_type': connection_type,
                        'relay_via': active_relays.get(pid),
                    })

            # Device profiles for peer identification
            _, _, trust_manager, _, channel_manager, _, _, _, _, _, _ = _get_app_components_any(current_app)
            peer_device_profiles = channel_manager.get_all_peer_device_profiles() if channel_manager else {}

            # Trust scores for connected and known peers
            trust_scores = {}
            if trust_manager:
                all_peer_ids = set(connected_peers)
                all_peer_ids.update(p.get('peer_id', '') for p in known_peers)
                all_peer_ids.update(p.get('peer_id', '') for p in introduced_peers)
                for pid in all_peer_ids:
                    if pid:
                        trust_scores[pid] = trust_manager.get_trust_score(pid)

            # Peer label map for UI (display name fallback)
            peer_labels = {}
            if peer_device_profiles:
                for pid, dev in peer_device_profiles.items():
                    if dev and getattr(dev, 'display_name', None):
                        peer_labels[pid] = dev.display_name
            for pid in connected_peers:
                if pid and pid not in peer_labels:
                    peer_labels[pid] = pid
            for peer in known_peers:
                pid = peer.get('peer_id')
                if not pid:
                    continue
                label = peer.get('display_name') or peer_labels.get(pid) or pid
                peer_labels[pid] = label
            for peer in introduced_peers:
                pid = peer.get('peer_id')
                if not pid:
                    continue
                label = peer.get('display_name') or peer_labels.get(pid) or pid
                peer_labels[pid] = label
            for peer in discovered_peers:
                pid = getattr(peer, 'peer_id', None)
                if pid and pid not in peer_labels:
                    peer_labels[pid] = pid

            return render_template('connect.html',
                                 invite_code=invite_code,
                                 peer_id=peer_id,
                                 endpoints=endpoints,
                                 local_ips=local_ips,
                                 mesh_port=config.network.mesh_port if config else 7771,
                                 connected_peers=connected_peers,
                                 discovered_peers=discovered_peers,
                                 introduced_peers=introduced_peers,
                                 known_peers=known_peers,
                                 relay_status=relay_status,
                                 peer_device_profiles=peer_device_profiles,
                                 trust_scores=trust_scores,
                                 peer_labels=peer_labels,
                                 is_admin=_is_admin(),
                                 user_id=user_id)
        except Exception as e:
            logger.error(f"Connect page error: {e}", exc_info=True)
            flash('Error loading connect page', 'error')
            return render_template('error.html', error=str(e))

    # AJAX endpoints for dynamic UI updates
    @ui.route('/ajax/peer_activity', methods=['GET'])
    @require_login
    def ajax_peer_activity():
        """Return last inbound/outbound activity timestamps for connected peers."""
        try:
            _, _, trust_manager, _, _, _, _, _, _, _, p2p_manager = _get_app_components_any(current_app)
            since_arg = request.args.get('since')
            since = None
            if since_arg is not None and since_arg != '':
                try:
                    since = float(since_arg)
                except ValueError:
                    since = None

            if not p2p_manager or not getattr(p2p_manager, 'connection_manager', None):
                return jsonify({
                    'success': True,
                    'peers': {},
                    'connected_peer_ids': [],
                    'peer_trust': {},
                    'events': [],
                    'server_time': time.time(),
                })

            conn_mgr = p2p_manager.connection_manager
            connected_peer_ids = list(conn_mgr.get_connected_peers() or [])
            peers = {}
            for peer_id in connected_peer_ids:
                conn = conn_mgr.get_connection(peer_id)
                if not conn:
                    continue
                peers[peer_id] = {
                    'connected_at': conn.connected_at,
                    'last_activity': conn.last_activity,
                    'last_inbound_activity': getattr(conn, 'last_inbound_activity', None),
                    'last_outbound_activity': getattr(conn, 'last_outbound_activity', None),
                }

            events = []
            if hasattr(p2p_manager, 'get_activity_events'):
                try:
                    events = p2p_manager.get_activity_events(since=since, limit=50)
                except Exception:
                    events = []

            trust_map = {}
            if trust_manager:
                for peer_id in connected_peer_ids:
                    try:
                        trust_map[peer_id] = trust_manager.get_trust_score(peer_id)
                    except Exception:
                        continue

            return jsonify({
                'success': True,
                'peers': peers,
                'connected_peer_ids': connected_peer_ids,
                'peer_trust': trust_map,
                'events': events,
                'server_time': time.time(),
            })
        except Exception as e:
            logger.error(f"Peer activity error: {e}", exc_info=True)
            return jsonify({'success': False, 'error': 'Failed to get peer activity'}), 500

    @ui.route('/ajax/p2p/diagnostics', methods=['GET'])
    @require_login
    def ajax_p2p_diagnostics():
        """Operational diagnostics for mesh troubleshooting."""
        try:
            *_, p2p_manager = get_app_components(current_app)
            if not p2p_manager:
                return jsonify({'success': False, 'error': 'P2P network unavailable'}), 503
            diagnostics = p2p_manager.get_mesh_diagnostics()
            return jsonify({'success': True, 'diagnostics': diagnostics})
        except Exception as e:
            logger.error(f"P2P diagnostics endpoint failed: {e}", exc_info=True)
            return jsonify({'success': False, 'error': 'Failed to load diagnostics'}), 500

    @ui.route('/ajax/p2p/resync', methods=['POST'])
    @require_login
    def ajax_p2p_resync():
        """Admin-triggered mesh resync: sync connected peers + optional reconnect."""
        try:
            if not _is_admin():
                return jsonify({'success': False, 'error': 'Admin permission required'}), 403
            *_, p2p_manager = get_app_components(current_app)
            if not p2p_manager:
                return jsonify({'success': False, 'error': 'P2P network unavailable'}), 503

            payload = request.get_json(silent=True) or {}
            include_reconnect = payload.get('include_reconnect', True)
            if isinstance(include_reconnect, str):
                include_reconnect = include_reconnect.strip().lower() in (
                    '1', 'true', 'yes', 'on'
                )
            else:
                include_reconnect = bool(include_reconnect)

            result = p2p_manager.resync_mesh(include_reconnect=include_reconnect)
            if result.get('error'):
                return jsonify({'success': False, **result}), 500
            return jsonify({'success': True, **result})
        except Exception as e:
            logger.error(f"P2P resync endpoint failed: {e}", exc_info=True)
            return jsonify({'success': False, 'error': 'Failed to trigger resync'}), 500

    @ui.route('/ajax/agent/heartbeat', methods=['GET'])
    @require_login
    def ajax_agent_heartbeat():
        """Heartbeat snapshot for the current user (UI convenience endpoint)."""
        try:
            user_id = get_current_user()
            return jsonify({
                'success': True,
                'heartbeat': _build_agent_heartbeat_snapshot(user_id),
            })
        except Exception as e:
            logger.error(f"Agent heartbeat UI endpoint failed: {e}", exc_info=True)
            return jsonify({'success': False, 'error': 'Failed to load heartbeat'}), 500

    @ui.route('/ajax/skills/<skill_id>/trust', methods=['GET'])
    @require_login
    def ajax_get_skill_trust(skill_id):
        """Return trust details for a visible skill."""
        try:
            db_manager, _, _, _, _, _, feed_manager, _, _, _, _ = _get_app_components_any(current_app)
            signal_manager = current_app.config.get('SIGNAL_MANAGER')
            skill_manager = current_app.config.get('SKILL_MANAGER')
            user_id = get_current_user()
            if not skill_manager:
                return jsonify({'success': False, 'error': 'Skill manager not available'}), 503

            if not _can_access_note_target(
                db_manager=db_manager,
                feed_manager=feed_manager,
                signal_manager=signal_manager,
                skill_manager=skill_manager,
                user_id=user_id,
                target_type='skill',
                target_id=skill_id,
            ):
                return jsonify({'success': False, 'error': 'Forbidden'}), 403

            trust_data = skill_manager.get_skill_trust_score(skill_id)
            stats = skill_manager.get_invocation_stats(skill_id)
            endorsements = skill_manager.get_endorsements(skill_id)
            return jsonify({
                'success': True,
                'skill_id': skill_id,
                'trust': trust_data,
                'invocation_stats': stats,
                'endorsement_count': len(endorsements or []),
                'endorsements': endorsements or [],
            })
        except Exception as e:
            logger.error(f"Skill trust UI endpoint failed: {e}", exc_info=True)
            return jsonify({'success': False, 'error': 'Failed to load skill trust'}), 500

    @ui.route('/ajax/skills/<skill_id>/endorse', methods=['POST'])
    @require_login
    def ajax_endorse_skill(skill_id):
        """Endorse a skill from UI."""
        try:
            db_manager, _, _, _, _, _, feed_manager, _, _, _, _ = _get_app_components_any(current_app)
            signal_manager = current_app.config.get('SIGNAL_MANAGER')
            skill_manager = current_app.config.get('SKILL_MANAGER')
            user_id = get_current_user()
            if not skill_manager:
                return jsonify({'success': False, 'error': 'Skill manager not available'}), 503

            if not _can_access_note_target(
                db_manager=db_manager,
                feed_manager=feed_manager,
                signal_manager=signal_manager,
                skill_manager=skill_manager,
                user_id=user_id,
                target_type='skill',
                target_id=skill_id,
            ):
                return jsonify({'success': False, 'error': 'Forbidden'}), 403

            data = request.get_json() or {}
            weight = data.get('weight', 1.0)
            comment = (data.get('comment') or '').strip()[:500] or None
            ok = skill_manager.endorse_skill(skill_id, user_id, weight=weight, comment=comment)
            if not ok:
                return jsonify({'success': False, 'error': 'Failed to endorse skill'}), 500

            trust_data = skill_manager.get_skill_trust_score(skill_id)
            endorsements = skill_manager.get_endorsements(skill_id)
            return jsonify({
                'success': True,
                'skill_id': skill_id,
                'trust': trust_data,
                'endorsement_count': len(endorsements or []),
            })
        except Exception as e:
            logger.error(f"Skill endorsement UI endpoint failed: {e}", exc_info=True)
            return jsonify({'success': False, 'error': 'Failed to endorse skill'}), 500

    @ui.route('/ajax/community_notes', methods=['GET'])
    @require_login
    def ajax_list_community_notes():
        """List community notes. Non-admin users must scope to a visible target."""
        try:
            db_manager, _, _, _, _, _, feed_manager, _, _, _, _ = _get_app_components_any(current_app)
            signal_manager = current_app.config.get('SIGNAL_MANAGER')
            skill_manager = current_app.config.get('SKILL_MANAGER')
            user_id = get_current_user()
            if not skill_manager:
                return jsonify({'success': False, 'error': 'Community notes not available'}), 503

            target_type = (request.args.get('target_type') or '').strip().lower() or None
            target_id = (request.args.get('target_id') or '').strip() or None
            status = (request.args.get('status') or '').strip().lower() or None
            limit = max(1, min(int(request.args.get('limit', 25)), 200))

            if not _is_admin():
                if not target_type or not target_id:
                    return jsonify({'success': False, 'error': 'target_type and target_id are required'}), 400
                if not _can_access_note_target(
                    db_manager=db_manager,
                    feed_manager=feed_manager,
                    signal_manager=signal_manager,
                    skill_manager=skill_manager,
                    user_id=user_id,
                    target_type=target_type,
                    target_id=target_id,
                ):
                    return jsonify({'success': False, 'error': 'Forbidden'}), 403

            notes = skill_manager.get_community_notes(
                target_type=target_type,
                target_id=target_id,
                status=status,
                limit=limit,
            )
            if not _is_admin():
                notes = [n for n in (notes or []) if (n.get('status') or 'proposed') != 'rejected']
            payload = _serialize_community_notes(notes, user_id)
            return jsonify({'success': True, 'notes': payload, 'count': len(payload)})
        except Exception as e:
            logger.error(f"List community notes failed: {e}", exc_info=True)
            return jsonify({'success': False, 'error': 'Failed to list community notes'}), 500

    @ui.route('/ajax/community_notes', methods=['POST'])
    @require_login
    def ajax_create_community_note():
        """Create a community note for a visible target."""
        try:
            db_manager, _, _, _, _, _, feed_manager, _, _, _, p2p_manager = get_app_components(current_app)
            signal_manager = current_app.config.get('SIGNAL_MANAGER')
            skill_manager = current_app.config.get('SKILL_MANAGER')
            user_id = get_current_user()
            if not skill_manager:
                return jsonify({'success': False, 'error': 'Community notes not available'}), 503

            data = request.get_json() or {}
            target_type = (data.get('target_type') or '').strip().lower()
            target_id = (data.get('target_id') or '').strip()
            content = (data.get('content') or '').strip()
            note_type = (data.get('note_type') or 'context').strip().lower()

            if not target_type or not target_id:
                return jsonify({'success': False, 'error': 'target_type and target_id are required'}), 400
            if len(content) < 10:
                return jsonify({'success': False, 'error': 'Note must be at least 10 characters'}), 400
            if len(content) > 2000:
                return jsonify({'success': False, 'error': 'Note exceeds 2000 character limit'}), 400

            if not _can_access_note_target(
                db_manager=db_manager,
                feed_manager=feed_manager,
                signal_manager=signal_manager,
                skill_manager=skill_manager,
                user_id=user_id,
                target_type=target_type,
                target_id=target_id,
            ):
                return jsonify({'success': False, 'error': 'Forbidden'}), 403

            note_id = skill_manager.create_community_note(
                target_type=target_type,
                target_id=target_id,
                author_id=user_id,
                content=content,
                note_type=note_type,
                origin_peer=p2p_manager.get_peer_id() if p2p_manager else None,
            )
            if not note_id:
                return jsonify({'success': False, 'error': 'Failed to create note'}), 500

            notes = _load_target_notes(skill_manager, target_type, target_id, user_id, limit=25)
            created = next((n for n in notes if n.get('id') == note_id), None)
            return jsonify({
                'success': True,
                'note_id': note_id,
                'note': created,
                'notes': notes,
            }), 201
        except Exception as e:
            logger.error(f"Create community note failed: {e}", exc_info=True)
            return jsonify({'success': False, 'error': 'Failed to create note'}), 500

    @ui.route('/ajax/community_notes/<note_id>/rate', methods=['POST'])
    @require_login
    def ajax_rate_community_note(note_id):
        """Rate a community note as helpful or not helpful."""
        try:
            db_manager, _, _, _, _, _, feed_manager, _, _, _, _ = _get_app_components_any(current_app)
            signal_manager = current_app.config.get('SIGNAL_MANAGER')
            skill_manager = current_app.config.get('SKILL_MANAGER')
            user_id = get_current_user()
            if not skill_manager:
                return jsonify({'success': False, 'error': 'Community notes not available'}), 503

            with db_manager.get_connection() as conn:
                row = conn.execute(
                    "SELECT id, target_type, target_id, author_id FROM community_notes WHERE id = ?",
                    (note_id,)
                ).fetchone()
            if not row:
                return jsonify({'success': False, 'error': 'Note not found'}), 404

            if not _can_access_note_target(
                db_manager=db_manager,
                feed_manager=feed_manager,
                signal_manager=signal_manager,
                skill_manager=skill_manager,
                user_id=user_id,
                target_type=row['target_type'],
                target_id=row['target_id'],
            ):
                return jsonify({'success': False, 'error': 'Forbidden'}), 403

            data = request.get_json() or {}
            helpful = bool(data.get('helpful', True))
            ok = skill_manager.rate_community_note(note_id, user_id, helpful=helpful)
            if not ok:
                return jsonify({'success': False, 'error': 'Failed to rate note'}), 500

            notes = _load_target_notes(
                skill_manager,
                row['target_type'],
                row['target_id'],
                user_id,
                limit=25,
            )
            updated = next((n for n in notes if n.get('id') == note_id), None)
            return jsonify({'success': True, 'note': updated, 'notes': notes})
        except Exception as e:
            logger.error(f"Rate community note failed: {e}", exc_info=True)
            return jsonify({'success': False, 'error': 'Failed to rate note'}), 500

    @ui.route('/ajax/send_message', methods=['POST'])
    @require_login
    def ajax_send_message():
        """AJAX endpoint to send a message with multimedia support."""
        try:
            db_manager, _, _, message_manager, _, file_manager, _, _, profile_manager, _, p2p_manager = _get_app_components_any(current_app)
            user_id = get_current_user()
            
            data = request.get_json()
            logger.info(f"Send message request: user_id={user_id}, data={data}")
            
            content = data.get('content', '').strip()
            recipient_id = data.get('recipient_id')  # None for broadcast
            recipient_ids = data.get('recipient_ids') or []
            reply_to = str(data.get('reply_to') or '').strip()
            if isinstance(recipient_id, list):
                recipient_ids = recipient_id
                recipient_id = None
            if isinstance(recipient_ids, str):
                recipient_ids = [r.strip() for r in recipient_ids.split(',') if r.strip()]
            elif not isinstance(recipient_ids, list):
                recipient_ids = []

            if recipient_id == 'None' or recipient_id == '':
                recipient_id = None  # Ensure proper null value for broadcast
            recipient_ids = [r for r in recipient_ids if r]
            file_attachments = data.get('attachments', [])
            
            logger.info(f"Parsed data: content='{content}', recipient_id='{recipient_id}', recipient_ids={recipient_ids}, attachments_count={len(file_attachments)}")
            
            if not content and not file_attachments:
                return jsonify({'error': 'Message content or attachments required'}), 400
            
            # Process file attachments if any
            processed_attachments = []
            for attachment in file_attachments:
                try:
                    # Attachment should contain file data as base64
                    file_data = base64.b64decode(attachment['data'])
                    file_info = file_manager.save_file(
                        file_data, 
                        attachment['name'], 
                        attachment['type'], 
                        user_id
                    )
                    
                    if file_info:
                        processed_attachments.append({
                            'id': file_info.id,
                            'name': file_info.original_name,
                            'type': file_info.content_type,
                            'size': file_info.size,
                            'url': file_info.url
                        })
                except Exception as e:
                    logger.error(f"Failed to process attachment {attachment.get('name', 'unknown')}: {e}")
                    continue
            
            # Determine message type based on attachments
            from ..core.messaging import MessageType
            message_type = MessageType.FILE if processed_attachments else MessageType.TEXT

            # Create message metadata
            metadata: dict[str, Any] = {'attachments': processed_attachments} if processed_attachments else {}
            if reply_to:
                metadata['reply_to'] = reply_to

            # Normalize recipients
            recipients_unique = []
            if recipient_ids:
                seen = set()
                for rid in recipient_ids:
                    if rid and rid not in seen:
                        seen.add(rid)
                        recipients_unique.append(rid)
            if recipient_id and not recipient_ids:
                recipients_unique = [recipient_id]

            recipients_unique = [r for r in recipients_unique if r != user_id]

            # Group DM handling
            if len(recipients_unique) > 1:
                group_members = sorted({user_id, *recipients_unique})
                group_id = _compute_group_id(group_members)
                metadata.update({
                    'group_id': group_id,
                    'group_members': group_members,
                    'is_group': True,
                })

                logger.info(f"Creating group DM: group_id={group_id}, members={group_members}")
                message = message_manager.create_message(user_id, content, group_id, message_type, metadata if metadata else None)
                if message and message_manager.send_message(message):
                    logger.info(f"Group message sent successfully: {message.id}")

                    try:
                        inbox_manager = current_app.config.get('INBOX_MANAGER')
                        if inbox_manager:
                            local_target_ids = filter_local_dm_targets(db_manager, p2p_manager, recipients_unique)
                            if local_target_ids:
                                inbox_manager.sync_source_triggers(
                                    source_type='dm',
                                    source_id=message.id,
                                    trigger_type='dm',
                                    target_ids=local_target_ids,
                                    sender_user_id=user_id,
                                    preview=build_dm_preview(content, metadata.get('attachments') or []),
                                    payload={
                                        'content': content,
                                        'message_id': message.id,
                                        'attachments': metadata.get('attachments') or [],
                                        'reply_to': reply_to or None,
                                        'group_id': group_id,
                                        'group_members': group_members,
                                        'is_group': True,
                                    },
                                    message_id=message.id,
                                    source_content=content,
                                )
                    except Exception as inbox_err:
                        logger.warning(f"Failed to create group DM inbox trigger: {inbox_err}")

                    if p2p_manager:
                        try:
                            display_name = None
                            if profile_manager:
                                try:
                                    profile = profile_manager.get_profile(user_id)
                                    if profile:
                                        display_name = profile.display_name or profile.username
                                except Exception:
                                    pass

                            for rid in recipients_unique:
                                p2p_manager.broadcast_direct_message(
                                    sender_id=user_id,
                                    recipient_id=rid,
                                    content=content,
                                    message_id=message.id,
                                    timestamp=message.created_at.strftime('%Y-%m-%d %H:%M:%S'),
                                    display_name=display_name,
                                    metadata=metadata,
                                )
                        except Exception as bcast_err:
                            logger.warning(f"Failed to broadcast group DM over P2P: {bcast_err}")

                    return jsonify({'success': True, 'message': message.to_dict(), 'group_id': group_id})
                return jsonify({'error': 'Failed to send group message'}), 500

            # Single recipient or broadcast
            recipient_id = recipients_unique[0] if recipients_unique else recipient_id

            logger.info(f"Calling message_manager.create_message: user_id={user_id}, recipient_id={recipient_id}, message_type={message_type}")

            message = message_manager.create_message(user_id, content, recipient_id, message_type, metadata if metadata else None)

            if message and message_manager.send_message(message):
                logger.info(f"Message sent successfully: {message.id}")

                try:
                    inbox_manager = current_app.config.get('INBOX_MANAGER')
                    if inbox_manager and recipient_id:
                        local_target_ids = filter_local_dm_targets(db_manager, p2p_manager, [recipient_id])
                        if local_target_ids:
                            inbox_manager.sync_source_triggers(
                                source_type='dm',
                                source_id=message.id,
                                trigger_type='dm',
                                target_ids=local_target_ids,
                                sender_user_id=user_id,
                                preview=build_dm_preview(content, metadata.get('attachments') or []),
                                payload={
                                    'content': content,
                                    'message_id': message.id,
                                    'attachments': metadata.get('attachments') or [],
                                    'reply_to': reply_to or None,
                                },
                                message_id=message.id,
                                source_content=content,
                            )
                except Exception as inbox_err:
                    logger.warning(f"Failed to create DM inbox trigger: {inbox_err}")

                # Broadcast DM over P2P so recipient's node can store it
                if recipient_id and p2p_manager:
                    try:
                        # Get sender display name for shadow user creation on remote
                        display_name = None
                        if profile_manager:
                            try:
                                profile = profile_manager.get_profile(user_id)
                                if profile:
                                    display_name = profile.display_name or profile.username
                            except Exception:
                                pass

                        p2p_manager.broadcast_direct_message(
                            sender_id=user_id,
                            recipient_id=recipient_id,
                            content=content,
                            message_id=message.id,
                            timestamp=message.created_at.strftime('%Y-%m-%d %H:%M:%S'),
                            display_name=display_name,
                            metadata=metadata if metadata else None,
                        )
                    except Exception as bcast_err:
                        logger.warning(f"Failed to broadcast DM over P2P: {bcast_err}")

                return jsonify({
                    'success': True,
                    'message': message.to_dict()
                })
            else:
                logger.error(f"Failed to send message")
                return jsonify({'error': 'Failed to send message'}), 500
                
        except Exception as e:
            logger.error(f"Send message error: {e}")
            return jsonify({'error': 'Internal server error'}), 500
    
    @ui.route('/ajax/generate_key', methods=['POST'])
    @require_login
    def ajax_generate_key():
        """AJAX endpoint to generate an API key."""
        try:
            _, api_key_manager, _, _, _, _, _, _, _, _, _ = _get_app_components_any(current_app)
            user_id = get_current_user()
            
            data = request.get_json() or {}
            permissions_raw = data.get('permissions', [])
            expires_days = data.get('expires_days')

            if permissions_raw is None:
                permissions_raw = []
            if isinstance(permissions_raw, str):
                permissions_raw = [permissions_raw]
            if not isinstance(permissions_raw, list):
                return jsonify({'error': 'permissions must be a list'}), 400

            # Omitted/empty permissions default to the standard agent scope.
            if not permissions_raw:
                permissions = ApiKeyManager.get_default_permissions()
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
                    'success': True,
                    'api_key': api_key,
                    'permissions': permissions_list
                })
            else:
                return jsonify({'error': 'Failed to generate API key'}), 500
                
        except Exception as e:
            logger.error(f"Generate key error: {e}")
            return jsonify({'error': 'Internal server error'}), 500
    
    @ui.route('/ajax/revoke_key', methods=['POST'])
    @require_login
    def ajax_revoke_key():
        """AJAX endpoint to revoke an API key."""
        try:
            _, api_key_manager, _, _, _, _, _, _, _, _, _ = _get_app_components_any(current_app)
            user_id = get_current_user()
            
            data = request.get_json()
            key_id = data.get('key_id')
            
            if not key_id:
                return jsonify({'error': 'Key ID required'}), 400
            
            success = api_key_manager.revoke_key(key_id, user_id)
            
            if success:
                return jsonify({'success': True})
            else:
                return jsonify({'error': 'Key not found or not owned by user'}), 404
                
        except Exception as e:
            logger.error(f"Revoke key error: {e}")
            return jsonify({'error': 'Internal server error'}), 500

    # Admin AJAX (instance owner only)
    @ui.route('/ajax/admin/users', methods=['GET'])
    @require_login
    @require_admin
    def ajax_admin_users():
        """List all users with account_type and status."""
        try:
            db_manager, _, _, _, _, _, _, _, _, _, _ = _get_app_components_any(current_app)
            users = db_manager.get_all_users_for_admin()
            _annotate_user_presence(users, db_manager)
            for u in users:
                if not u.get('is_registered'):
                    u['agent_directives_source'] = 'n/a'
                    u['agent_directives_preview'] = 'Unregistered shadow/replica user.'
                    u['agent_directives_length'] = 0
                    continue
                state = _effective_agent_directive_state(u)
                u['agent_directives_source'] = state['source']
                u['agent_directives_preview'] = (state['effective'] or '')[:140]
                u['agent_directives_length'] = len(state['effective'] or '')
            return jsonify({'success': True, 'users': users})
        except Exception as e:
            logger.error(f"Admin list users error: {e}")
            return jsonify({'error': 'Internal server error'}), 500

    @ui.route('/ajax/admin/identity-portability/status', methods=['GET'])
    @require_login
    @require_admin
    def ajax_admin_identity_portability_status():
        """Admin diagnostics for distributed identity portability."""
        try:
            mgr = current_app.config.get('IDENTITY_PORTABILITY_MANAGER')
            if not mgr:
                return jsonify({'success': False, 'error': 'Identity portability manager unavailable'}), 503
            snapshot = mgr.get_status_snapshot()
            return jsonify({'success': True, 'status': snapshot})
        except Exception as e:
            logger.error(f"Identity portability status error: {e}", exc_info=True)
            return jsonify({'success': False, 'error': 'Internal server error'}), 500

    @ui.route('/ajax/admin/identity-portability/capable-peers', methods=['GET'])
    @require_login
    @require_admin
    def ajax_admin_identity_portability_capable_peers():
        """List connected peers that advertise identity portability capability."""
        try:
            mgr = current_app.config.get('IDENTITY_PORTABILITY_MANAGER')
            if not mgr or not mgr.enabled:
                return jsonify({'success': True, 'enabled': False, 'peers': []})

            snapshot = mgr.get_status_snapshot()
            capable_ids = [str(p or '').strip() for p in (snapshot.get('connected_capable_peers') or [])]
            capable_ids = [p for p in capable_ids if p]
            capable_set = set(capable_ids)

            *_, p2p_manager = _get_app_components_any(current_app)
            connected_set: set[str] = set()
            display_names: dict[str, str] = {}
            endpoint_map: dict[str, list[str]] = {}
            if p2p_manager:
                try:
                    connected_set = {
                        str(pid or '').strip()
                        for pid in (p2p_manager.get_connected_peers() or [])
                        if str(pid or '').strip()
                    }
                except Exception:
                    connected_set = set()
                identity_manager = getattr(p2p_manager, 'identity_manager', None)
                if identity_manager:
                    try:
                        raw_names = getattr(identity_manager, 'peer_display_names', {}) or {}
                        display_names = {
                            str(k or '').strip(): str(v or '').strip()
                            for k, v in raw_names.items()
                            if str(k or '').strip()
                        }
                    except Exception:
                        display_names = {}
                    try:
                        raw_endpoints = getattr(identity_manager, 'peer_endpoints', {}) or {}
                        endpoint_map = {}
                        for peer_id, values in raw_endpoints.items():
                            pid = str(peer_id or '').strip()
                            if not pid:
                                continue
                            eps = []
                            for endpoint in (values or []):
                                text = str(endpoint or '').strip()
                                if text:
                                    eps.append(text)
                            endpoint_map[pid] = eps
                    except Exception:
                        endpoint_map = {}

            peers = []
            for peer_id in sorted(capable_set):
                peers.append({
                    'peer_id': peer_id,
                    'display_name': display_names.get(peer_id) or '',
                    'connected': peer_id in connected_set,
                    'endpoints': endpoint_map.get(peer_id, [])[:5],
                })

            return jsonify({
                'success': True,
                'enabled': True,
                'local_peer_id': snapshot.get('local_peer_id'),
                'peers': peers,
                'count': len(peers),
            })
        except Exception as e:
            logger.error(f"Identity portability capable peers error: {e}", exc_info=True)
            return jsonify({'success': False, 'error': 'Internal server error'}), 500

    @ui.route('/ajax/admin/identity-portability/principals', methods=['GET'])
    @require_login
    @require_admin
    def ajax_admin_identity_portability_principals():
        """List principals and key metadata for admin review."""
        try:
            mgr = current_app.config.get('IDENTITY_PORTABILITY_MANAGER')
            if not mgr or not mgr.enabled:
                return jsonify({'success': True, 'principals': [], 'enabled': False})
            try:
                limit = int(request.args.get('limit', 200))
            except Exception:
                limit = 200
            principals = mgr.list_principals(limit=limit)
            return jsonify({'success': True, 'enabled': True, 'principals': principals, 'count': len(principals)})
        except Exception as e:
            logger.error(f"Identity portability principal list error: {e}", exc_info=True)
            return jsonify({'success': False, 'error': 'Internal server error'}), 500

    @ui.route('/ajax/admin/identity-portability/grants', methods=['GET'])
    @require_login
    @require_admin
    def ajax_admin_identity_portability_grants():
        """List bootstrap grants for admin review."""
        try:
            mgr = current_app.config.get('IDENTITY_PORTABILITY_MANAGER')
            if not mgr or not mgr.enabled:
                return jsonify({'success': True, 'grants': [], 'enabled': False})
            try:
                limit = int(request.args.get('limit', 200))
            except Exception:
                limit = 200
            status_filter = str(request.args.get('status') or '').strip() or None
            grants = mgr.list_grants(limit=limit, status=status_filter)
            return jsonify({'success': True, 'enabled': True, 'grants': grants, 'count': len(grants)})
        except Exception as e:
            logger.error(f"Identity portability grant list error: {e}", exc_info=True)
            return jsonify({'success': False, 'error': 'Internal server error'}), 500

    @ui.route('/ajax/admin/identity-portability/grants', methods=['POST'])
    @require_login
    @require_admin
    def ajax_admin_identity_portability_create_grant():
        """Create a signed bootstrap grant (Phase 1: role is clamped to 'user')."""
        try:
            mgr = current_app.config.get('IDENTITY_PORTABILITY_MANAGER')
            if not mgr or not mgr.enabled:
                return jsonify({'success': False, 'error': 'Identity portability is disabled'}), 400
            data = request.get_json(silent=True) or {}
            local_user_id = str(data.get('local_user_id') or '').strip()
            if not local_user_id:
                return jsonify({'success': False, 'error': 'local_user_id is required'}), 400
            audience_peer = str(data.get('audience_peer') or '').strip() or None
            target_peer_id = str(data.get('target_peer_id') or '').strip() or None
            try:
                expires_in_hours = int(data.get('expires_in_hours', 24))
            except Exception:
                expires_in_hours = 24
            try:
                max_uses = int(data.get('max_uses', 1))
            except Exception:
                max_uses = 1
            sync_to_mesh = bool(data.get('sync_to_mesh', True))

            result = mgr.create_bootstrap_grant(
                local_user_id=local_user_id,
                acting_user_id=get_current_user(),
                audience_peer=audience_peer,
                expires_in_hours=expires_in_hours,
                max_uses=max_uses,
                sync_to_mesh=sync_to_mesh,
                target_peer_id=target_peer_id,
            )
            return jsonify({'success': True, **result})
        except Exception as e:
            logger.error(f"Identity portability create grant error: {e}", exc_info=True)
            return jsonify({'success': False, 'error': str(e)}), 400

    @ui.route('/ajax/admin/identity-portability/grants/import', methods=['POST'])
    @require_login
    @require_admin
    def ajax_admin_identity_portability_import_grant():
        """Import a grant artifact and optionally apply it to a local user."""
        try:
            mgr = current_app.config.get('IDENTITY_PORTABILITY_MANAGER')
            if not mgr or not mgr.enabled:
                return jsonify({'success': False, 'error': 'Identity portability is disabled'}), 400
            data = request.get_json(silent=True) or {}
            artifact = data.get('artifact')
            if not isinstance(artifact, dict):
                return jsonify({'success': False, 'error': 'artifact object is required'}), 400
            source_peer = str(data.get('source_peer') or '').strip() or None
            sync_to_mesh = bool(data.get('sync_to_mesh', False))
            import_result = mgr.import_bootstrap_grant(
                artifact=artifact,
                source_peer=source_peer,
                actor_user_id=get_current_user(),
                sync_to_mesh=sync_to_mesh,
            )
            response: dict[str, Any] = {'success': bool(import_result.get('imported')), 'import': import_result}

            apply_local_user_id = str(data.get('apply_local_user_id') or '').strip()
            if apply_local_user_id and import_result.get('imported'):
                apply_result = mgr.apply_bootstrap_grant(
                    grant_id=str(import_result.get('grant_id') or artifact.get('grant_id') or ''),
                    local_user_id=apply_local_user_id,
                    actor_user_id=get_current_user(),
                    source_peer=source_peer,
                )
                response['apply'] = apply_result
                response['success'] = bool(apply_result.get('applied'))

            if not response['success']:
                return jsonify(response), 400
            return jsonify(response)
        except Exception as e:
            logger.error(f"Identity portability import grant error: {e}", exc_info=True)
            return jsonify({'success': False, 'error': str(e)}), 400

    @ui.route('/ajax/admin/identity-portability/grants/<grant_id>/apply', methods=['POST'])
    @require_login
    @require_admin
    def ajax_admin_identity_portability_apply_grant(grant_id: str):
        """Apply an imported grant to a local user."""
        try:
            mgr = current_app.config.get('IDENTITY_PORTABILITY_MANAGER')
            if not mgr or not mgr.enabled:
                return jsonify({'success': False, 'error': 'Identity portability is disabled'}), 400
            data = request.get_json(silent=True) or {}
            local_user_id = str(data.get('local_user_id') or '').strip()
            if not local_user_id:
                return jsonify({'success': False, 'error': 'local_user_id is required'}), 400
            source_peer = str(data.get('source_peer') or '').strip() or None
            result = mgr.apply_bootstrap_grant(
                grant_id=grant_id,
                local_user_id=local_user_id,
                actor_user_id=get_current_user(),
                source_peer=source_peer,
            )
            if not result.get('applied'):
                return jsonify({'success': False, **result}), 400
            return jsonify({'success': True, **result})
        except Exception as e:
            logger.error(f"Identity portability apply grant error: {e}", exc_info=True)
            return jsonify({'success': False, 'error': str(e)}), 400

    @ui.route('/ajax/admin/identity-portability/grants/<grant_id>/revoke', methods=['POST'])
    @require_login
    @require_admin
    def ajax_admin_identity_portability_revoke_grant(grant_id: str):
        """Revoke a bootstrap grant and propagate a revocation marker."""
        try:
            mgr = current_app.config.get('IDENTITY_PORTABILITY_MANAGER')
            if not mgr or not mgr.enabled:
                return jsonify({'success': False, 'error': 'Identity portability is disabled'}), 400
            data = request.get_json(silent=True) or {}
            reason = str(data.get('reason') or '').strip() or 'revoked_by_admin'
            sync_to_mesh = bool(data.get('sync_to_mesh', True))
            result = mgr.revoke_bootstrap_grant(
                grant_id=grant_id,
                actor_user_id=get_current_user(),
                reason=reason,
                sync_to_mesh=sync_to_mesh,
            )
            if not result.get('revoked'):
                return jsonify({'success': False, **result}), 400
            return jsonify({'success': True, **result})
        except Exception as e:
            logger.error(f"Identity portability revoke grant error: {e}", exc_info=True)
            return jsonify({'success': False, 'error': str(e)}), 400

    @ui.route('/ajax/admin/agent-directives/presets', methods=['GET'])
    @require_login
    @require_admin
    def ajax_admin_agent_directive_presets():
        """Return admin-manageable directive presets."""
        return jsonify({
            'success': True,
            'max_length': MAX_AGENT_DIRECTIVES_LENGTH,
            'presets': _agent_directive_presets_payload(),
        })

    @ui.route('/ajax/admin/users/<user_id>/directive', methods=['GET'])
    @require_login
    @require_admin
    def ajax_admin_get_user_directive(user_id):
        """Get effective agent-directive state for one user."""
        try:
            db_manager, _, _, _, _, _, _, _, _, _, _ = _get_app_components_any(current_app)
            user = db_manager.get_user(user_id)
            if not user or not user.get('password_hash'):
                return jsonify({'error': 'User not found'}), 404
            state = _effective_agent_directive_state(user)
            return jsonify({
                'success': True,
                'user': {
                    'id': user.get('id'),
                    'username': user.get('username'),
                    'display_name': user.get('display_name') or user.get('username'),
                    'account_type': user.get('account_type') or 'human',
                    'status': user.get('status') or 'active',
                },
                'directive': state,
            })
        except Exception as e:
            logger.error(f"Admin get directive error: {e}")
            return jsonify({'error': 'Internal server error'}), 500

    @ui.route('/ajax/admin/users/<user_id>/directive', methods=['POST'])
    @require_login
    @require_admin
    def ajax_admin_set_user_directive(user_id):
        """Set or clear custom agent directives for a single agent user."""
        try:
            db_manager, _, _, _, _, _, _, _, _, _, _ = _get_app_components_any(current_app)
            user = db_manager.get_user(user_id)
            if not user or not user.get('password_hash'):
                return jsonify({'error': 'User not found'}), 404

            # Only allow directive edits for LOCAL users (origin_peer is NULL).
            # Remote agents never call this instance's API, so directives are meaningless.
            if user.get('origin_peer'):
                return jsonify({'error': 'Cannot set directives for remote peer agents — they do not use this instance'}), 403

            data = request.get_json() or {}
            preset_id = (data.get('preset_id') or '').strip()
            use_default = bool(data.get('use_default'))

            if use_default:
                new_custom = None
            elif preset_id:
                preset = (DEFAULT_AGENT_DIRECTIVE_PRESETS or {}).get(preset_id)
                if not preset:
                    return jsonify({'error': 'Unknown preset_id'}), 400
                try:
                    new_custom = normalize_agent_directives(preset.get('content'))
                except ValueError as ve:
                    return jsonify({'error': str(ve)}), 400
            elif 'agent_directives' in data:
                try:
                    new_custom = normalize_agent_directives(data.get('agent_directives'))
                except ValueError as ve:
                    return jsonify({'error': str(ve)}), 400
            else:
                return jsonify({'error': 'Provide agent_directives, preset_id, or use_default'}), 400

            if not db_manager.set_user_agent_directives(user_id, new_custom):
                return jsonify({'error': 'Failed to update directives'}), 500

            updated = db_manager.get_user(user_id) or user
            state = _effective_agent_directive_state(updated)
            return jsonify({
                'success': True,
                'directive': state,
                'message': 'Directives updated',
            })
        except Exception as e:
            logger.error(f"Admin set directive error: {e}")
            return jsonify({'error': 'Internal server error'}), 500

    @ui.route('/ajax/admin/agent-directives/apply-defaults', methods=['POST'])
    @require_login
    @require_admin
    def ajax_admin_apply_default_directives():
        """Apply role defaults to agent accounts that have no custom directives."""
        try:
            db_manager, _, _, _, _, _, _, _, _, _, _ = _get_app_components_any(current_app)
            data = request.get_json() or {}
            overwrite = bool(data.get('overwrite', False))
            users = db_manager.get_all_users_for_admin()
            updated_ids = []
            skipped_ids = []
            for user in users:
                if not user.get('is_registered'):
                    skipped_ids.append(user.get('id'))
                    continue
                if user.get('origin_peer'):
                    skipped_ids.append(user.get('id'))
                    continue
                if (user.get('account_type') or 'human') != 'agent':
                    continue
                if (user.get('status') or 'active') == 'suspended':
                    skipped_ids.append(user.get('id'))
                    continue
                try:
                    custom = normalize_agent_directives(user.get('agent_directives'))
                except Exception:
                    custom = None
                if custom and not overwrite:
                    skipped_ids.append(user.get('id'))
                    continue
                default = get_default_agent_directives(
                    username=user.get('username'),
                    account_type=user.get('account_type'),
                )
                if not default:
                    skipped_ids.append(user.get('id'))
                    continue
                if db_manager.set_user_agent_directives(user.get('id'), default):
                    updated_ids.append(user.get('id'))
                else:
                    skipped_ids.append(user.get('id'))

            return jsonify({
                'success': True,
                'updated_count': len(updated_ids),
                'skipped_count': len(skipped_ids),
                'updated_ids': updated_ids,
            })
        except Exception as e:
            logger.error(f"Admin apply default directives error: {e}")
            return jsonify({'error': 'Internal server error'}), 500

    @ui.route('/ajax/admin/users/<user_id>/approve', methods=['POST'])
    @require_login
    @require_admin
    def ajax_admin_approve(user_id):
        """Set user status to active (approve pending agent)."""
        try:
            db_manager, _, _, _, _, _, _, _, _, _, _ = _get_app_components_any(current_app)
            if db_manager.set_user_status(user_id, 'active'):
                return jsonify({'success': True, 'status': 'active'})
            return jsonify({'error': 'User not found or update failed'}), 400
        except Exception as e:
            logger.error(f"Admin approve error: {e}")
            return jsonify({'error': 'Internal server error'}), 500

    @ui.route('/ajax/admin/users/<user_id>/suspend', methods=['POST'])
    @require_login
    @require_admin
    def ajax_admin_suspend(user_id):
        """Set user status to suspended."""
        try:
            db_manager, _, _, _, _, _, _, _, _, _, _ = _get_app_components_any(current_app)
            owner_id = db_manager.get_instance_owner_user_id()
            if user_id == owner_id:
                return jsonify({'error': 'Cannot suspend the instance owner'}), 400
            if db_manager.set_user_status(user_id, 'suspended'):
                return jsonify({'success': True, 'status': 'suspended'})
            return jsonify({'error': 'User not found or update failed'}), 400
        except Exception as e:
            logger.error(f"Admin suspend error: {e}")
            return jsonify({'error': 'Internal server error'}), 500

    @ui.route('/ajax/admin/users/<user_id>/classification', methods=['POST'])
    @require_login
    @require_admin
    def ajax_admin_update_user_classification(user_id: str):
        """Admin: update account_type/status for local or remote user records."""
        try:
            db_manager, _, _, _, _, _, _, _, _, _, _ = _get_app_components_any(current_app)
            user = db_manager.get_user(user_id)
            if not user:
                return jsonify({'error': 'User not found'}), 404
            if user_id in {'system', 'local_user'}:
                return jsonify({'error': 'Reserved system users cannot be modified'}), 400

            data = request.get_json(silent=True) or {}
            account_type = data.get('account_type', None)
            status = data.get('status', None)
            display_name = data.get('display_name', None)

            if account_type is not None:
                account_type = str(account_type or '').strip().lower()
                if account_type not in {'human', 'agent'}:
                    return jsonify({'error': "account_type must be 'human' or 'agent'"}), 400
            if status is not None:
                status = str(status or '').strip().lower()
                if status not in {'active', 'pending_approval', 'suspended'}:
                    return jsonify({'error': "status must be 'active', 'pending_approval', or 'suspended'"}), 400

            owner_id = db_manager.get_instance_owner_user_id()
            if user_id == owner_id and status in {'suspended', 'pending_approval'}:
                return jsonify({'error': 'Cannot change instance owner to non-active status'}), 400

            updated = db_manager.update_user_admin_fields(
                user_id,
                account_type=account_type,
                status=status,
                display_name=display_name,
            )
            if not updated:
                return jsonify({'error': 'No valid classification updates were applied'}), 400

            refreshed = db_manager.get_user(user_id) or user
            payload = {
                'id': refreshed.get('id'),
                'username': refreshed.get('username'),
                'display_name': refreshed.get('display_name') or refreshed.get('username'),
                'account_type': refreshed.get('account_type') or 'human',
                'status': refreshed.get('status') or 'active',
                'origin_peer': refreshed.get('origin_peer'),
                'is_registered': bool(refreshed.get('password_hash')),
            }
            return jsonify({'success': True, 'user': payload})
        except Exception as e:
            logger.error(f"Admin classification update error: {e}", exc_info=True)
            return jsonify({'error': 'Internal server error'}), 500

    @ui.route('/ajax/admin/users/<user_id>', methods=['DELETE'])
    @require_login
    @require_admin
    def ajax_admin_delete_user(user_id):
        """Delete a user account (and their keys, channel memberships)."""
        try:
            db_manager, _, _, _, _, _, _, _, _, _, _ = _get_app_components_any(current_app)
            user_row = db_manager.get_user(user_id)
            if not user_row:
                return jsonify({'error': 'User not found'}), 404
            if user_id in {'system', 'local_user'}:
                return jsonify({'error': 'Reserved system users cannot be deleted'}), 400
            owner_id = db_manager.get_instance_owner_user_id()
            if user_id == owner_id:
                return jsonify({'error': 'Cannot delete the instance owner'}), 400
            if db_manager.delete_user(user_id):
                return jsonify({
                    'success': True,
                    'deleted_user_id': user_id,
                    'was_remote': bool(str(user_row.get('origin_peer') or '').strip()),
                    'was_registered': bool(user_row.get('password_hash')),
                })
            return jsonify({'error': 'User not found or delete failed (check server logs for details)'}), 400
        except Exception as e:
            logger.error(f"Admin delete user error: {e}", exc_info=True)
            return jsonify({'error': 'Internal server error'}), 500

    @ui.route('/ajax/admin/users/<user_id>/keys', methods=['GET'])
    @require_login
    @require_admin
    def ajax_admin_user_keys(user_id):
        """List API keys for a user (admin only). Keys are masked; no raw key returned."""
        try:
            _, api_key_manager, _, _, _, _, _, _, _, _, _ = _get_app_components_any(current_app)
            keys = api_key_manager.list_keys(user_id)
            return jsonify({
                'keys': [
                    {
                        'id': k.id,
                        'id_suffix': k.id[-8:] if len(k.id) >= 8 else k.id,
                        'permissions': [p.value for p in (k.permissions or set())],
                        'created_at': k.created_at.isoformat() if k.created_at else None,
                        'expires_at': k.expires_at.isoformat() if k.expires_at else None,
                        'revoked': k.revoked,
                    }
                    for k in keys
                ]
            })
        except Exception as e:
            logger.error(f"Admin list keys error: {e}")
            return jsonify({'error': 'Internal server error'}), 500

    @ui.route('/ajax/admin/users/<user_id>/keys', methods=['POST'])
    @require_login
    @require_admin
    def ajax_admin_create_key_for_user(user_id):
        """Create an API key for a user (admin only). Body: permissions (list), expires_days (optional). Returns raw key once."""
        try:
            db_manager, api_key_manager, _, _, _, _, _, _, _, _, _ = _get_app_components_any(current_app)
            user = db_manager.get_user(user_id)
            if not user or not user.get('password_hash'):
                return jsonify({'error': 'User not found or not a registered account'}), 400
            data = request.get_json() or {}
            perms_raw = data.get('permissions', [])
            if not perms_raw:
                perms_raw = [p.value for p in api_key_manager.get_all_permissions()]
            try:
                permissions = [Permission(p) for p in perms_raw]
            except ValueError as e:
                return jsonify({'error': f'Invalid permission: {e}'}), 400
            expires_days = data.get('expires_days')  # None = no expiry
            if expires_days is not None:
                try:
                    expires_days = int(expires_days)
                    if expires_days < 1:
                        expires_days = None
                except (TypeError, ValueError):
                    expires_days = None
            raw_key = api_key_manager.generate_key(user_id, permissions, expires_days)
            if not raw_key:
                return jsonify({'error': 'Failed to generate key'}), 500
            return jsonify({
                'success': True,
                'api_key': raw_key,
                'user_id': user_id,
                'permissions': [p.value for p in permissions],
                'expires_days': expires_days,
                'message': 'Copy this key now; it will not be shown again.',
            })
        except Exception as e:
            logger.error(f"Admin create key error: {e}")
            return jsonify({'error': 'Internal server error'}), 500

    @ui.route('/ajax/admin/users/<user_id>/keys/<key_id>', methods=['DELETE'])
    @require_login
    @require_admin
    def ajax_admin_revoke_key(user_id, key_id):
        """Revoke an API key for a user (admin only)."""
        try:
            _, api_key_manager, _, _, _, _, _, _, _, _, _ = _get_app_components_any(current_app)
            if api_key_manager.revoke_key(key_id, user_id):
                return jsonify({'success': True})
            return jsonify({'error': 'Key not found or already revoked'}), 400
        except Exception as e:
            logger.error(f"Admin revoke key error: {e}")
            return jsonify({'error': 'Internal server error'}), 500

    @ui.route('/ajax/admin/transfer', methods=['POST'])
    @require_login
    @require_admin
    def ajax_admin_transfer():
        """Transfer instance admin to another user. Current user must be admin."""
        try:
            data = request.get_json() or {}
            target_user_id = (data.get('user_id') or '').strip()
            if not target_user_id:
                return jsonify({'error': 'user_id required'}), 400
            db_manager, _, _, _, _, _, _, _, _, _, _ = _get_app_components_any(current_app)
            user = db_manager.get_user(target_user_id)
            if not user or not user.get('password_hash'):
                return jsonify({'error': 'User not found or not a registered account'}), 400
            if not db_manager.set_instance_owner_user_id(target_user_id):
                return jsonify({'error': 'Failed to set instance owner'}), 500
            flash('Instance admin transferred. You are no longer admin.', 'success')
            return jsonify({'success': True, 'new_owner_id': target_user_id})
        except Exception as e:
            logger.error(f"Admin transfer error: {e}")
            return jsonify({'error': 'Internal server error'}), 500

    @ui.route('/ajax/admin/users/<user_id>/workspace', methods=['GET'])
    @require_login
    @require_admin
    def ajax_admin_user_workspace(user_id: str):
        """Return admin debug workspace data for a user (profile, inbox, mentions)."""
        try:
            db_manager, _, _, _, _, _, _, _, _, _, _ = _get_app_components_any(current_app)
            user = _admin_registered_user_row(db_manager, user_id)
            if not user:
                return jsonify({'error': 'User not found'}), 404
            inbox_limit = _coerce_int(request.args.get('inbox_limit'), 25, 1, 100)
            mention_limit = _coerce_int(request.args.get('mention_limit'), 25, 1, 100)
            audit_limit = _coerce_int(request.args.get('audit_limit'), 25, 1, 100)
            workspace = _build_admin_workspace_snapshot(
                user,
                inbox_limit=inbox_limit,
                mention_limit=mention_limit,
                audit_limit=audit_limit,
            )
            return jsonify({'success': True, 'workspace': workspace})
        except Exception as e:
            logger.error(f"Admin workspace load error: {e}", exc_info=True)
            return jsonify({'error': 'Internal server error'}), 500

    @ui.route('/ajax/admin/users/<user_id>/profile', methods=['POST'])
    @require_login
    @require_admin
    def ajax_admin_update_user_profile(user_id: str):
        """Admin: update editable profile fields for a local registered user."""
        try:
            db_manager, _, _, _, _, _, _, _, profile_manager, _, _ = _get_app_components_any(current_app)
            user = _admin_registered_user_row(db_manager, user_id)
            if not user:
                return jsonify({'error': 'User not found'}), 404
            if user.get('origin_peer'):
                return jsonify({'error': 'Remote peer profiles are read-only on this instance'}), 403
            if not profile_manager:
                return jsonify({'error': 'Profile service unavailable'}), 503

            data = request.get_json(silent=True) or {}
            updates: dict[str, Any] = {}

            if 'display_name' in data:
                display_name = (data.get('display_name') or '').strip()
                if len(display_name) > 100:
                    return jsonify({'error': 'Display name too long (max 100 characters)'}), 400
                updates['display_name'] = display_name or None

            if 'bio' in data:
                bio = (data.get('bio') or '').strip()
                if len(bio) > 500:
                    return jsonify({'error': 'Bio too long (max 500 characters)'}), 400
                updates['bio'] = bio or None

            if 'account_type' in data:
                account_type = (data.get('account_type') or '').strip().lower()
                if account_type not in ('human', 'agent'):
                    return jsonify({'error': "account_type must be 'human' or 'agent'"}), 400
                updates['account_type'] = account_type

            if 'theme_preference' in data:
                theme_preference = (data.get('theme_preference') or 'dark').strip().lower()
                if theme_preference not in ['dark', 'light', 'auto', 'liquid-glass', 'eco']:
                    return jsonify({'error': 'Invalid theme_preference'}), 400
                updates['theme_preference'] = theme_preference

            if not updates:
                return jsonify({'error': 'No valid profile fields provided'}), 400

            success = profile_manager.update_profile(user_id, **updates)
            if not success:
                return jsonify({'error': 'Failed to update profile'}), 500

            _broadcast_profile_if_possible(profile_manager, user_id)
            updated_user = db_manager.get_user(user_id) or user
            workspace = _build_admin_workspace_snapshot(updated_user)
            return jsonify({'success': True, 'workspace': workspace, 'message': 'Profile updated'})
        except Exception as e:
            logger.error(f"Admin profile update error: {e}", exc_info=True)
            return jsonify({'error': 'Internal server error'}), 500

    @ui.route('/ajax/admin/users/<user_id>/governance', methods=['POST'])
    @require_login
    @require_admin
    def ajax_admin_update_user_governance(user_id: str):
        """Admin: update per-user channel governance policy for a local user."""
        try:
            db_manager, _, _, _, channel_manager, _, _, _, _, _, _ = _get_app_components_any(current_app)
            user = _admin_registered_user_row(db_manager, user_id)
            if not user:
                return jsonify({'error': 'User not found'}), 404
            if user.get('origin_peer'):
                return jsonify({'error': 'Remote peer users are read-only on this instance'}), 403
            if not channel_manager:
                return jsonify({'error': 'Channel service unavailable'}), 503

            data = request.get_json(silent=True) or {}

            def _to_bool(value: Any) -> bool:
                if isinstance(value, bool):
                    return value
                if value is None:
                    return False
                return str(value).strip().lower() in {'1', 'true', 'yes', 'on'}

            enabled = _to_bool(data.get('enabled'))
            block_public_channels = _to_bool(data.get('block_public_channels'))
            restrict_to_allowed_channels = _to_bool(data.get('restrict_to_allowed_channels'))
            enforce_now = _to_bool(data.get('enforce_now', True))

            raw_allowed = data.get('allowed_channel_ids') or []
            if not isinstance(raw_allowed, list):
                return jsonify({'error': 'allowed_channel_ids must be an array'}), 400
            if len(raw_allowed) > 1024:
                return jsonify({'error': 'Too many allowed channels provided'}), 400

            available_channels = channel_manager.list_channels_for_governance()
            valid_channel_ids = {
                str(row.get('id')).strip()
                for row in (available_channels or [])
                if row and row.get('id')
            }
            allowed_channel_ids = []
            seen = set()
            for cid in raw_allowed:
                channel_id = str(cid or '').strip()
                if not channel_id or channel_id in seen:
                    continue
                if channel_id not in valid_channel_ids:
                    continue
                seen.add(channel_id)
                allowed_channel_ids.append(channel_id)

            if enabled and restrict_to_allowed_channels and not allowed_channel_ids:
                return jsonify({
                    'error': 'Allowlist mode is enabled but no valid allowed channels were selected'
                }), 400

            saved = channel_manager.set_user_channel_governance(
                user_id=user_id,
                enabled=enabled,
                block_public_channels=block_public_channels,
                restrict_to_allowed_channels=restrict_to_allowed_channels,
                allowed_channel_ids=allowed_channel_ids,
                updated_by=get_current_user(),
            )
            if not saved:
                return jsonify({'error': 'Failed to save governance policy'}), 500

            enforcement_result: dict[str, Any] = {
                'enabled': enabled,
                'checked_count': 0,
                'removed_count': 0,
                'removed_channel_ids': [],
            }
            if enforce_now:
                enforcement_result = channel_manager.enforce_user_channel_governance(user_id)

            updated_user = db_manager.get_user(user_id) or user
            workspace = _build_admin_workspace_snapshot(updated_user)
            message = 'Governance policy updated'
            if enforce_now:
                removed = int((enforcement_result or {}).get('removed_count') or 0)
                message = f'Governance policy updated (removed {removed} disallowed memberships)'
            return jsonify({
                'success': True,
                'workspace': workspace,
                'enforcement': enforcement_result,
                'message': message,
            })
        except Exception as e:
            logger.error(f"Admin governance update error: {e}", exc_info=True)
            return jsonify({'error': 'Internal server error'}), 500

    @ui.route('/ajax/admin/users/<user_id>/avatar', methods=['POST'])
    @require_login
    @require_admin
    def ajax_admin_upload_user_avatar(user_id: str):
        """Admin: upload avatar image for a local registered user."""
        try:
            db_manager, _, _, _, _, _, _, _, profile_manager, _, _ = _get_app_components_any(current_app)
            user = _admin_registered_user_row(db_manager, user_id)
            if not user:
                return jsonify({'error': 'User not found'}), 404
            if user.get('origin_peer'):
                return jsonify({'error': 'Remote peer profiles are read-only on this instance'}), 403
            if not profile_manager:
                return jsonify({'error': 'Profile service unavailable'}), 503

            if 'avatar' not in request.files:
                return jsonify({'error': 'No avatar file provided'}), 400
            avatar_file = request.files['avatar']
            if avatar_file.filename == '':
                return jsonify({'error': 'No file selected'}), 400
            if not (avatar_file.content_type or '').startswith('image/'):
                return jsonify({'error': 'Only image files are allowed'}), 400

            avatar_data = avatar_file.read()
            if len(avatar_data) > 5 * 1024 * 1024:
                return jsonify({'error': 'File too large (max 5MB)'}), 400

            file_id = profile_manager.update_avatar(
                user_id,
                avatar_data,
                avatar_file.filename,
                avatar_file.content_type,
            )
            if not file_id:
                return jsonify({'error': 'Failed to upload avatar'}), 500

            _broadcast_profile_if_possible(profile_manager, user_id)
            updated_user = db_manager.get_user(user_id) or user
            workspace = _build_admin_workspace_snapshot(updated_user)
            return jsonify({
                'success': True,
                'avatar_url': f'/files/{file_id}',
                'workspace': workspace,
                'message': 'Avatar updated',
            })
        except Exception as e:
            logger.error(f"Admin avatar upload error: {e}", exc_info=True)
            return jsonify({'error': 'Internal server error'}), 500

    @ui.route('/ajax/admin/users/<user_id>/avatar', methods=['DELETE'])
    @require_login
    @require_admin
    def ajax_admin_clear_user_avatar(user_id: str):
        """Admin: remove avatar from a local registered user profile."""
        try:
            db_manager, _, _, _, _, _, _, _, profile_manager, _, _ = _get_app_components_any(current_app)
            user = _admin_registered_user_row(db_manager, user_id)
            if not user:
                return jsonify({'error': 'User not found'}), 404
            if user.get('origin_peer'):
                return jsonify({'error': 'Remote peer profiles are read-only on this instance'}), 403
            if not profile_manager:
                return jsonify({'error': 'Profile service unavailable'}), 503

            success = profile_manager.update_profile(user_id, avatar_file_id=None)
            if not success:
                return jsonify({'error': 'Failed to clear avatar'}), 500

            _broadcast_profile_if_possible(profile_manager, user_id)
            updated_user = db_manager.get_user(user_id) or user
            workspace = _build_admin_workspace_snapshot(updated_user)
            return jsonify({'success': True, 'workspace': workspace, 'message': 'Avatar removed'})
        except Exception as e:
            logger.error(f"Admin avatar clear error: {e}", exc_info=True)
            return jsonify({'error': 'Internal server error'}), 500

    @ui.route('/ajax/admin/users/<user_id>/inbox/rebuild', methods=['POST'])
    @require_login
    @require_admin
    def ajax_admin_rebuild_user_inbox(user_id: str):
        """Admin: rebuild mention-driven inbox entries from recent channel history."""
        try:
            db_manager, _, _, _, _, _, _, _, profile_manager, _, _ = _get_app_components_any(current_app)
            inbox_manager = current_app.config.get('INBOX_MANAGER')
            if not inbox_manager:
                return jsonify({'error': 'Inbox service unavailable'}), 503

            user = _admin_registered_user_row(db_manager, user_id)
            if not user:
                return jsonify({'error': 'User not found'}), 404

            data = request.get_json(silent=True) or {}
            window_hours = _coerce_int(data.get('window_hours'), 168, 1, 720)
            limit = _coerce_int(data.get('limit'), 2000, 100, 5000)
            display_name = user.get('display_name')
            if profile_manager:
                try:
                    prof = profile_manager.get_profile(user_id)
                    display_name = _profile_value(prof, 'display_name', display_name)
                except Exception:
                    pass

            result = inbox_manager.rebuild_from_channel_messages(
                user_id=user_id,
                username=user.get('username') or user_id,
                display_name=display_name,
                window_hours=window_hours,
                limit=limit,
            )
            workspace = _build_admin_workspace_snapshot(user)
            return jsonify({
                'success': True,
                'result': result,
                'workspace': workspace,
                'message': (
                    f"Inbox rebuild complete: scanned {result.get('scanned', 0)}, "
                    f"created {result.get('created', 0)}, skipped {result.get('skipped', 0)}."
                ),
            })
        except Exception as e:
            logger.error(f"Admin inbox rebuild error: {e}", exc_info=True)
            return jsonify({'error': 'Internal server error'}), 500

    @ui.route('/ajax/admin/channels/reconcile-delete', methods=['POST'])
    @require_login
    @require_admin
    def ajax_admin_reconcile_channel_delete():
        """Admin: rebroadcast channel delete signals to reconcile stale replicas."""
        try:
            db_manager, _, _, _, _, _, _, _, _, _, p2p_manager = _get_app_components_any(current_app)
            if not p2p_manager or not p2p_manager.is_running():
                return jsonify({'error': 'P2P mesh is not running on this peer'}), 503

            data = request.get_json(silent=True) or {}
            raw_ids = data.get('channel_ids')
            raw_text = data.get('channel_ids_text')
            raw_target_peer = str(data.get('target_peer_id') or '').strip()
            raw_reason = str(data.get('reason') or '').strip()

            tokens: list[str] = []
            if isinstance(raw_ids, list):
                for value in raw_ids:
                    token = str(value or '').strip()
                    if token:
                        tokens.append(token)
            if isinstance(raw_text, str):
                tokens.extend([part.strip() for part in re.split(r'[\s,;]+', raw_text) if part.strip()])

            seen: set[str] = set()
            channel_ids: list[str] = []
            for token in tokens:
                if token in seen:
                    continue
                seen.add(token)
                channel_ids.append(token)

            if not channel_ids:
                return jsonify({'error': 'Provide at least one channel ID'}), 400
            if len(channel_ids) > 200:
                return jsonify({'error': 'Too many channel IDs (max 200 per request)'}), 400

            local_peer_id = ''
            try:
                local_peer_id = str(p2p_manager.get_peer_id() or '').strip()
            except Exception:
                local_peer_id = ''

            target_peer = None if raw_target_peer.lower() in {'', '*', 'all'} else raw_target_peer
            reason = raw_reason or 'admin_channel_reconcile_delete'

            placeholders = ','.join('?' for _ in channel_ids)
            channel_rows: dict[str, Any] = {}
            if placeholders:
                with db_manager.get_connection() as conn:
                    rows = conn.execute(
                        f"SELECT id, origin_peer FROM channels WHERE id IN ({placeholders})",
                        tuple(channel_ids),
                    ).fetchall()
                channel_rows = {str(row['id']): row for row in rows or []}

            sent = 0
            skipped = 0
            failed = 0
            details: list[dict[str, Any]] = []

            for channel_id in channel_ids:
                if channel_id == 'general':
                    skipped += 1
                    details.append({
                        'channel_id': channel_id,
                        'status': 'skipped',
                        'reason': 'protected_channel',
                    })
                    continue

                row = channel_rows.get(channel_id)
                origin_peer = str((row['origin_peer'] if row else '') or '').strip()
                if row and origin_peer:
                    if not local_peer_id:
                        skipped += 1
                        details.append({
                            'channel_id': channel_id,
                            'status': 'skipped',
                            'reason': 'local_peer_unknown',
                            'origin_peer': origin_peer,
                        })
                        continue
                    if origin_peer != local_peer_id:
                        skipped += 1
                        details.append({
                            'channel_id': channel_id,
                            'status': 'skipped',
                            'reason': 'not_local_origin',
                            'origin_peer': origin_peer,
                        })
                        continue

                signal_id = f"DS{secrets.token_hex(8)}"
                try:
                    ok = bool(
                        p2p_manager.broadcast_delete_signal(
                            signal_id=signal_id,
                            data_type='channel',
                            data_id=channel_id,
                            reason=reason,
                            target_peer=target_peer,
                        )
                    )
                    if ok:
                        sent += 1
                        details.append({
                            'channel_id': channel_id,
                            'status': 'sent',
                            'signal_id': signal_id,
                            'origin_peer': origin_peer or None,
                            'local_row_found': bool(row),
                        })
                    else:
                        failed += 1
                        details.append({
                            'channel_id': channel_id,
                            'status': 'failed',
                            'reason': 'broadcast_failed',
                            'origin_peer': origin_peer or None,
                            'local_row_found': bool(row),
                        })
                except Exception as bcast_err:
                    failed += 1
                    details.append({
                        'channel_id': channel_id,
                        'status': 'failed',
                        'reason': f'broadcast_error: {bcast_err}',
                        'origin_peer': origin_peer or None,
                        'local_row_found': bool(row),
                    })

            return jsonify({
                'success': failed == 0,
                'requested': len(channel_ids),
                'sent': sent,
                'skipped': skipped,
                'failed': failed,
                'target_peer': target_peer or 'all',
                'reason': reason,
                'details': details,
                'message': f"Delete signal reconciliation complete: sent={sent}, skipped={skipped}, failed={failed}.",
            })
        except Exception as e:
            logger.error(f"Admin channel delete reconcile error: {e}", exc_info=True)
            return jsonify({'error': 'Internal server error'}), 500

    @ui.route('/ajax/admin/channels/member-sync-diagnostics', methods=['GET'])
    @require_login
    @require_admin
    def ajax_admin_channel_member_sync_diagnostics():
        """Admin: inspect private-channel membership propagation and sync delivery health."""
        try:
            db_manager, _, _, _, channel_manager, _, _, _, _, _, p2p_manager = _get_app_components_any(current_app)

            channel_id = str(request.args.get('channel_id') or '').strip()
            target_user_id = str(request.args.get('target_user_id') or '').strip()
            try:
                limit = int(request.args.get('limit', 200))
            except Exception:
                limit = 200
            limit = max(20, min(limit, 500))

            if not channel_id:
                return jsonify({'error': 'channel_id query parameter is required'}), 400

            def _table_columns(conn: Any, table_name: str) -> set[str]:
                cols: set[str] = set()
                try:
                    rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
                    for row in rows or []:
                        if hasattr(row, 'keys'):
                            name = str(row['name'] or '').strip()
                        else:
                            name = str(row[1] or '').strip()
                        if name:
                            cols.add(name)
                except Exception:
                    return set()
                return cols

            local_peer_id = ''
            connected_peers: list[str] = []
            if p2p_manager:
                try:
                    local_peer_id = str(p2p_manager.get_peer_id() or '').strip()
                except Exception:
                    local_peer_id = ''
                try:
                    raw_connected = p2p_manager.get_connected_peers() or []
                    for cp in raw_connected:
                        pid = cp if isinstance(cp, str) else getattr(cp, 'peer_id', None)
                        pid_text = str(pid or '').strip()
                        if pid_text:
                            connected_peers.append(pid_text)
                except Exception:
                    connected_peers = []
            connected_peers = sorted(set(connected_peers))

            with db_manager.get_connection() as conn:
                channel_cols = _table_columns(conn, 'channels')
                if not channel_cols:
                    return jsonify({'error': 'Channel metadata is unavailable on this peer'}), 500

                channel_select = [
                    f"c.{col} AS {col}"
                    for col in (
                        'id',
                        'name',
                        'channel_type',
                        'description',
                        'privacy_mode',
                        'origin_peer',
                        'created_by',
                        'crypto_mode',
                        'created_at',
                    )
                    if col in channel_cols
                ]
                channel_row = conn.execute(
                    f"SELECT {', '.join(channel_select)} FROM channels c WHERE c.id = ?",
                    (channel_id,),
                ).fetchone()
                if not channel_row:
                    return jsonify({'error': f'Channel not found: {channel_id}'}), 404
                channel_payload = dict(channel_row)

                member_cols = _table_columns(conn, 'channel_members')
                user_cols = _table_columns(conn, 'users')
                member_select = ['cm.user_id AS user_id']
                for col in ('role', 'notifications_enabled', 'joined_at'):
                    if col in member_cols:
                        member_select.append(f"cm.{col} AS {col}")
                for col in ('username', 'display_name', 'origin_peer', 'account_type', 'status'):
                    if col in user_cols:
                        member_select.append(f"u.{col} AS {col}")

                member_rows = conn.execute(
                    f"""
                    SELECT {', '.join(member_select)}
                    FROM channel_members cm
                    LEFT JOIN users u ON u.id = cm.user_id
                    WHERE cm.channel_id = ?
                    ORDER BY
                        CASE WHEN cm.role = 'admin' THEN 0 ELSE 1 END,
                        COALESCE(cm.joined_at, '') ASC
                    """,
                    (channel_id,),
                ).fetchall()

                sync_table_exists = bool(
                    conn.execute(
                        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'channel_member_sync_deliveries'"
                    ).fetchone()
                )
                delivery_rows: list[Any] = []
                total_records = 0
                if sync_table_exists:
                    where_clauses = ["d.channel_id = ?"]
                    where_params: list[Any] = [channel_id]
                    if target_user_id:
                        where_clauses.append("d.target_user_id = ?")
                        where_params.append(target_user_id)
                    where_sql = " AND ".join(where_clauses)

                    count_row = conn.execute(
                        f"SELECT COUNT(*) AS cnt FROM channel_member_sync_deliveries d WHERE {where_sql}",
                        tuple(where_params),
                    ).fetchone()
                    total_records = int((count_row['cnt'] if count_row and 'cnt' in count_row.keys() else 0) or 0)

                    delivery_rows = conn.execute(
                        f"""
                        SELECT d.sync_id, d.channel_id, d.target_user_id, d.action, d.role,
                               d.target_peer_id, d.payload_json, d.delivery_state, d.last_error,
                               d.attempt_count, d.last_attempt_at, d.acked_at,
                               d.created_at, d.updated_at
                        FROM channel_member_sync_deliveries d
                        WHERE {where_sql}
                        ORDER BY COALESCE(d.updated_at, d.created_at) DESC
                        LIMIT ?
                        """,
                        tuple(where_params + [limit]),
                    ).fetchall()

            members: list[dict[str, Any]] = []
            member_user_ids: set[str] = set()
            for row in member_rows or []:
                row_dict = dict(row)
                user_id = str(row_dict.get('user_id') or '').strip()
                if not user_id:
                    continue
                member_user_ids.add(user_id)
                member_origin = str(row_dict.get('origin_peer') or '').strip()
                is_local_member = (not member_origin) or bool(local_peer_id and member_origin == local_peer_id)
                members.append({
                    'user_id': user_id,
                    'role': row_dict.get('role') or 'member',
                    'notifications_enabled': bool(int(row_dict.get('notifications_enabled') or 0)) if row_dict.get('notifications_enabled') is not None else True,
                    'joined_at': row_dict.get('joined_at'),
                    'username': row_dict.get('username'),
                    'display_name': row_dict.get('display_name'),
                    'origin_peer': member_origin or None,
                    'account_type': row_dict.get('account_type'),
                    'status': row_dict.get('status'),
                    'is_local_member': is_local_member,
                })

            member_peer_ids: set[str] = set()
            if channel_manager:
                try:
                    member_peer_ids = set(channel_manager.get_member_peer_ids(channel_id, local_peer_id or None) or set())
                except Exception:
                    member_peer_ids = set()
            if not member_peer_ids:
                member_peer_ids = {
                    str(m.get('origin_peer') or '').strip()
                    for m in members
                    if str(m.get('origin_peer') or '').strip()
                }
                if local_peer_id:
                    member_peer_ids.add(local_peer_id)

            state_counts: dict[str, int] = {}
            target_peer_counts: dict[str, int] = {}
            pending_count = 0
            failed_count = 0
            acked_count = 0
            recent_records: list[dict[str, Any]] = []
            for row in delivery_rows or []:
                row_dict = dict(row)
                state = str(row_dict.get('delivery_state') or 'pending').strip().lower() or 'pending'
                state_counts[state] = int(state_counts.get(state, 0) + 1)
                if state == 'failed':
                    failed_count += 1
                if row_dict.get('acked_at'):
                    acked_count += 1
                elif state in {'pending', 'sent', 'failed'}:
                    pending_count += 1

                peer = str(row_dict.get('target_peer_id') or '').strip()
                if peer:
                    target_peer_counts[peer] = int(target_peer_counts.get(peer, 0) + 1)

                payload_summary: dict[str, Any] = {}
                payload_raw = row_dict.get('payload_json')
                if payload_raw:
                    try:
                        payload_data = json.loads(payload_raw)
                        if isinstance(payload_data, dict):
                            for key in ('channel_name', 'channel_type', 'privacy_mode'):
                                if key in payload_data:
                                    payload_summary[key] = payload_data.get(key)
                    except Exception:
                        payload_summary = {}

                recent_records.append({
                    'sync_id': row_dict.get('sync_id'),
                    'target_user_id': row_dict.get('target_user_id'),
                    'action': row_dict.get('action'),
                    'role': row_dict.get('role'),
                    'target_peer_id': row_dict.get('target_peer_id'),
                    'delivery_state': state,
                    'attempt_count': int(row_dict.get('attempt_count') or 0),
                    'last_error': row_dict.get('last_error'),
                    'last_attempt_at': row_dict.get('last_attempt_at'),
                    'acked_at': row_dict.get('acked_at'),
                    'created_at': row_dict.get('created_at'),
                    'updated_at': row_dict.get('updated_at'),
                    'payload_summary': payload_summary,
                })

            warnings: list[str] = []
            privacy_mode = str(channel_payload.get('privacy_mode') or 'open').strip().lower()
            if privacy_mode not in {'private', 'confidential'}:
                warnings.append(
                    f"Channel privacy_mode='{privacy_mode}' is not targeted; member-sync diagnostics are mostly relevant for private/confidential channels."
                )
            if not sync_table_exists:
                warnings.append(
                    "channel_member_sync_deliveries table not found on this peer; delivery tracing is unavailable."
                )
            if privacy_mode in {'private', 'confidential'} and not connected_peers:
                warnings.append("No connected peers on this node; membership updates cannot propagate right now.")
            if privacy_mode in {'private', 'confidential'} and len(member_peer_ids) <= 1:
                warnings.append("Only one member peer is visible for this channel; cross-peer propagation may be limited.")
            if failed_count > 0:
                warnings.append(f"{failed_count} delivery record(s) are in failed state.")
            if pending_count > 0:
                warnings.append(f"{pending_count} delivery record(s) are pending/unacked.")

            target_user_payload = None
            if target_user_id:
                target_user_payload = {
                    'user_id': target_user_id,
                    'is_channel_member': target_user_id in member_user_ids,
                    'origin_peer': None,
                    'is_connected_peer': False,
                }
                try:
                    user_row = db_manager.get_user(target_user_id)
                except Exception:
                    user_row = None
                target_origin = str((user_row or {}).get('origin_peer') or '').strip() if user_row else ''
                target_user_payload['origin_peer'] = target_origin or None
                target_user_payload['is_connected_peer'] = bool(target_origin and target_origin in connected_peers)

                if target_user_id not in member_user_ids:
                    warnings.append(f"Target user {target_user_id} is not currently a member of channel {channel_id}.")
                if not target_origin:
                    warnings.append(
                        f"Target user {target_user_id} has no origin_peer; sync delivery relies on connected-peer fallback."
                    )
                elif local_peer_id and target_origin == local_peer_id:
                    warnings.append(
                        f"Target user {target_user_id} resolves to local peer ({local_peer_id}); if that user is remote, origin metadata is stale."
                    )
                elif target_origin not in connected_peers:
                    warnings.append(
                        f"Target user {target_user_id} origin peer ({target_origin}) is not currently connected."
                    )
                if total_records == 0:
                    warnings.append(
                        f"No member-sync delivery records found for {channel_id} and target user {target_user_id}."
                    )

            diagnostics = {
                'channel': channel_payload,
                'target_user': target_user_payload,
                'local_peer_id': local_peer_id or None,
                'connected_peers': connected_peers,
                'member_peer_ids': sorted(member_peer_ids),
                'members': members,
                'member_sync': {
                    'table_available': sync_table_exists,
                    'total_records': total_records,
                    'returned_records': len(recent_records),
                    'state_counts': state_counts,
                    'pending_count': pending_count,
                    'failed_count': failed_count,
                    'acked_count': acked_count,
                    'target_peer_counts': target_peer_counts,
                    'recent_records': recent_records,
                },
                'warnings': warnings,
            }

            return jsonify({'success': True, 'diagnostics': diagnostics})
        except Exception as e:
            logger.error(f"Admin member-sync diagnostics error: {e}", exc_info=True)
            return jsonify({'error': 'Internal server error'}), 500

    @ui.route('/ajax/get_messages', methods=['GET'])
    @require_login
    def ajax_get_messages():
        """AJAX endpoint to get recent messages."""
        try:
            _, _, _, message_manager, _, _, _, _, _, _, _ = _get_app_components_any(current_app)
            user_id = get_current_user()
            
            limit = int(request.args.get('limit', 20))
            since_str = request.args.get('since')
            
            since = None
            if since_str:
                since = datetime.fromisoformat(since_str)
            
            messages = message_manager.get_messages(user_id, limit, since)
            
            return jsonify({
                'messages': [message.to_dict() for message in messages],
                'count': len(messages)
            })
            
        except Exception as e:
            logger.error(f"Get messages error: {e}")
            return jsonify({'error': 'Internal server error'}), 500

    @ui.route('/ajax/channel_suggestions', methods=['GET'])
    @require_login
    def ajax_channel_suggestions():
        """Return channel suggestions for #channel autocomplete and linkify."""
        try:
            _, _, _, _, channel_manager, _, _, _, _, _, _ = _get_app_components_any(current_app)
            user_id = get_current_user()
            query = (request.args.get('q') or '').strip().lower()
            limit = request.args.get('limit', type=int) or 200
            limit = max(1, min(limit, 500))

            channels = channel_manager.get_user_channels(user_id)
            results = []
            for ch in channels:
                name = (ch.name or '').strip()
                if not name:
                    continue
                if query and query not in name.lower():
                    continue
                results.append({
                    'id': ch.id,
                    'name': name,
                    'privacy_mode': getattr(ch, 'privacy_mode', 'open'),
                    'type': (
                        getattr(getattr(ch, 'channel_type', None), 'value', None)
                        if getattr(ch, 'channel_type', None) is not None
                        else None
                    ),
                })
                if len(results) >= limit:
                    break

            return jsonify({'success': True, 'channels': results, 'count': len(results)})
        except Exception as e:
            logger.error(f"Channel suggestions error: {e}")
            return jsonify({'success': False, 'channels': [], 'count': 0})

    @ui.route('/ajax/channel_sidebar_state', methods=['GET'])
    @require_login
    def ajax_channel_sidebar_state():
        """Return lightweight per-channel sidebar state (unread + mute).

        Includes full metadata so the frontend can dynamically insert
        channels that were added after page load (e.g. via member_sync).
        """
        try:
            _, _, _, _, channel_manager, _, _, _, _, _, _ = _get_app_components_any(current_app)
            user_id = get_current_user()
            channels = channel_manager.get_user_channels(user_id)
            payload = []
            for ch in channels:
                ctype = ch.channel_type
                if hasattr(ctype, 'value'):
                    ctype = ctype.value
                payload.append({
                    'id': ch.id,
                    'name': ch.name,
                    'description': getattr(ch, 'description', '') or '',
                    'channel_type': str(ctype or 'public'),
                    'privacy_mode': getattr(ch, 'privacy_mode', 'open') or 'open',
                    'origin_peer': getattr(ch, 'origin_peer', '') or '',
                    'user_role': getattr(ch, 'user_role', 'member') or 'member',
                    'member_count': int(getattr(ch, 'member_count', 0) or 0),
                    'unread_count': int(getattr(ch, 'unread_count', 0) or 0),
                    'notifications_enabled': bool(getattr(ch, 'notifications_enabled', True)),
                    'crypto_mode': getattr(ch, 'crypto_mode', '') or '',
                })
            return jsonify({'success': True, 'channels': payload, 'count': len(payload)})
        except Exception as e:
            logger.error(f"Channel sidebar state error: {e}")
            return jsonify({'success': False, 'channels': [], 'count': 0})

    @ui.route('/ajax/content_contexts/extract', methods=['POST'])
    @require_login
    def ajax_extract_content_context():
        """Extract and cache best-effort text context for a source item."""
        try:
            db_manager, _, _, _, _, _, feed_manager, _, _, _, _ = _get_app_components_any(current_app)
            _ctx_ensure_schema(db_manager)
            user_id = get_current_user()
            admin_user_id = db_manager.get_instance_owner_user_id() if db_manager else None

            data = request.get_json(silent=True) or {}
            source_type = (data.get('source_type') or 'url').strip().lower()
            source_id = (data.get('source_id') or '').strip()
            url_override = (data.get('url') or '').strip()
            force_refresh = str(data.get('force_refresh', '')).strip().lower() in ('1', 'true', 'yes')

            if source_type not in ('url', 'feed_post', 'channel_message', 'direct_message'):
                return jsonify({'success': False, 'error': 'source_type must be one of: url, feed_post, channel_message, direct_message'}), 400
            if source_type != 'url' and not source_id:
                return jsonify({'success': False, 'error': 'source_id is required for feed_post/channel_message/direct_message'}), 400

            ok, payload_or_code, payload_or_error = _ctx_resolve_source_payload(
                db_manager,
                feed_manager,
                user_id,
                source_type,
                source_id,
            )
            if not ok:
                return jsonify({'success': False, 'error': payload_or_error}), int(payload_or_code)
            source_payload = payload_or_code or {}

            source_url = url_override
            if not source_url:
                candidates = source_payload.get('source_url_candidates') or []
                source_url = candidates[0].strip() if candidates else ''
            if not source_url:
                return jsonify({'success': False, 'error': 'No URL found. Provide url or include a URL in content.'}), 400

            video_id = _ctx_parse_youtube_video_id(source_url)
            canonical_url = f"https://www.youtube.com/watch?v={video_id}" if video_id else source_url

            safe, reason = _ctx_is_safe_external_url(canonical_url)
            if not safe:
                return jsonify({'success': False, 'error': reason}), 400

            source_id_key = source_id or ''
            with db_manager.get_connection() as conn:
                existing = conn.execute(
                    """
                    SELECT *
                    FROM content_contexts
                    WHERE source_type = ? AND source_id = ? AND source_url = ? AND owner_user_id = ?
                    LIMIT 1
                    """,
                    (source_type, source_id_key, canonical_url, user_id)
                ).fetchone()

            if existing and not force_refresh:
                return jsonify({
                    'success': True,
                    'context': _ctx_serialize_row(existing, user_id, admin_user_id),
                    'cached': True,
                    'extracted': False,
                })

            extracted = _ctx_extract_external_context(canonical_url)
            stored_url = (extracted.get('canonical_url') or canonical_url).strip() or canonical_url
            safe_stored, reason_stored = _ctx_is_safe_external_url(stored_url)
            if not safe_stored:
                stored_url = canonical_url

            metadata = extracted.get('metadata') or {}
            metadata.update({
                'requested_url': source_url,
                'source_url_candidates': source_payload.get('source_url_candidates') or [],
                'source_content_len': len(source_payload.get('content') or ''),
                'source_owner_user_id': source_payload.get('owner_user_id'),
                'extracted_by': user_id,
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
                        user_id,
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
                    (source_type, source_id_key, stored_url, user_id)
                ).fetchone()
            if not row:
                return jsonify({'success': False, 'error': 'Context extraction stored no result'}), 500
            return jsonify({
                'success': True,
                'context': _ctx_serialize_row(row, user_id, admin_user_id),
                'cached': False,
                'extracted': True,
            })
        except Exception as e:
            logger.error(f"Extract content context error: {e}", exc_info=True)
            return jsonify({'success': False, 'error': 'Failed to extract content context'}), 500

    @ui.route('/ajax/content_contexts', methods=['GET'])
    @require_login
    def ajax_list_content_contexts():
        """List stored content-context rows for the session user."""
        try:
            db_manager, _, _, _, _, _, feed_manager, _, _, _, _ = _get_app_components_any(current_app)
            _ctx_ensure_schema(db_manager)
            user_id = get_current_user()
            admin_user_id = db_manager.get_instance_owner_user_id() if db_manager else None

            source_type = (request.args.get('source_type') or '').strip().lower()
            source_id = (request.args.get('source_id') or '').strip()
            source_url = (request.args.get('source_url') or '').strip()
            owner_param = (request.args.get('owner_user_id') or '').strip()
            limit = request.args.get('limit', 50)
            try:
                limit_i = max(1, min(int(limit), 200))
            except Exception:
                limit_i = 50

            owner_user_id = user_id
            if owner_param:
                if owner_param != user_id and (not admin_user_id or user_id != admin_user_id):
                    return jsonify({'success': False, 'error': 'Only admin can read other owners context rows'}), 403
                owner_user_id = owner_param

            clauses = ["owner_user_id = ?"]
            params = [owner_user_id]
            if source_type:
                if source_type not in ('url', 'feed_post', 'channel_message', 'direct_message'):
                    return jsonify({'success': False, 'error': 'Invalid source_type filter'}), 400
                clauses.append("source_type = ?")
                params.append(source_type)
            if source_id:
                clauses.append("source_id = ?")
                params.append(source_id)
            if source_url:
                clauses.append("source_url = ?")
                params.append(source_url)

            sql = f"""
                SELECT *
                FROM content_contexts
                WHERE {' AND '.join(clauses)}
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
                    if not _ctx_can_access_source(db_manager, feed_manager, user_id, row_source_type, row_source_id):
                        continue
                contexts.append(_ctx_serialize_row(row, user_id, admin_user_id))

            return jsonify({'success': True, 'contexts': contexts, 'count': len(contexts)})
        except Exception as e:
            logger.error(f"List content contexts error: {e}", exc_info=True)
            return jsonify({'success': False, 'error': 'Failed to list content contexts'}), 500

    @ui.route('/ajax/content_contexts/<context_id>', methods=['GET'])
    @require_login
    def ajax_get_content_context(context_id):
        """Get one content-context row."""
        try:
            db_manager, _, _, _, _, _, feed_manager, _, _, _, _ = _get_app_components_any(current_app)
            _ctx_ensure_schema(db_manager)
            user_id = get_current_user()
            admin_user_id = db_manager.get_instance_owner_user_id() if db_manager else None

            with db_manager.get_connection() as conn:
                row = conn.execute("SELECT * FROM content_contexts WHERE id = ?", (context_id,)).fetchone()
            if not row:
                return jsonify({'success': False, 'error': 'Context not found'}), 404
            if row['owner_user_id'] != user_id and (not admin_user_id or user_id != admin_user_id):
                return jsonify({'success': False, 'error': 'Access denied'}), 403

            source_type = (row['source_type'] or '').strip()
            source_id = (row['source_id'] or '').strip()
            if source_type in ('feed_post', 'channel_message', 'direct_message') and source_id:
                if not _ctx_can_access_source(db_manager, feed_manager, user_id, source_type, source_id):
                    return jsonify({'success': False, 'error': 'Access denied'}), 403

            return jsonify({'success': True, 'context': _ctx_serialize_row(row, user_id, admin_user_id)})
        except Exception as e:
            logger.error(f"Get content context error: {e}", exc_info=True)
            return jsonify({'success': False, 'error': 'Failed to get content context'}), 500

    @ui.route('/ajax/content_contexts/<context_id>/text', methods=['GET'])
    @require_login
    def ajax_get_content_context_text(context_id):
        """Return a plain-text context blob for copy/export."""
        try:
            db_manager, _, _, _, _, _, feed_manager, _, _, _, _ = _get_app_components_any(current_app)
            _ctx_ensure_schema(db_manager)
            user_id = get_current_user()
            admin_user_id = db_manager.get_instance_owner_user_id() if db_manager else None

            with db_manager.get_connection() as conn:
                row = conn.execute("SELECT * FROM content_contexts WHERE id = ?", (context_id,)).fetchone()
            if not row:
                return jsonify({'success': False, 'error': 'Context not found'}), 404
            if row['owner_user_id'] != user_id and (not admin_user_id or user_id != admin_user_id):
                return jsonify({'success': False, 'error': 'Access denied'}), 403

            source_type = (row['source_type'] or '').strip()
            source_id = (row['source_id'] or '').strip()
            if source_type in ('feed_post', 'channel_message', 'direct_message') and source_id:
                if not _ctx_can_access_source(db_manager, feed_manager, user_id, source_type, source_id):
                    return jsonify({'success': False, 'error': 'Access denied'}), 403

            payload = _ctx_serialize_row(row, user_id, admin_user_id)
            return Response(payload.get('text_blob') or '', mimetype='text/plain; charset=utf-8')
        except Exception as e:
            logger.error(f"Get content context text error: {e}", exc_info=True)
            return jsonify({'success': False, 'error': 'Failed to get content context text'}), 500

    @ui.route('/ajax/content_contexts/<context_id>/note', methods=['POST'])
    @require_login
    def ajax_update_content_context_note(context_id):
        """Update owner note for a content-context row (owner/admin)."""
        try:
            db_manager, _, _, _, _, _, feed_manager, _, _, _, _ = _get_app_components_any(current_app)
            _ctx_ensure_schema(db_manager)
            user_id = get_current_user()
            admin_user_id = db_manager.get_instance_owner_user_id() if db_manager else None
            data = request.get_json(silent=True) or {}
            note = data.get('owner_note')
            if note is None and 'note' in data:
                note = data.get('note')
            note_text = (note or '').strip()
            if len(note_text) > 24000:
                return jsonify({'success': False, 'error': 'owner_note is too long (max 24000 chars)'}), 400

            with db_manager.get_connection() as conn:
                row = conn.execute("SELECT * FROM content_contexts WHERE id = ?", (context_id,)).fetchone()
                if not row:
                    return jsonify({'success': False, 'error': 'Context not found'}), 404
                if row['owner_user_id'] != user_id and (not admin_user_id or user_id != admin_user_id):
                    return jsonify({'success': False, 'error': 'Only owner or admin can edit owner_note'}), 403

                source_type = (row['source_type'] or '').strip()
                source_id = (row['source_id'] or '').strip()
                if source_type in ('feed_post', 'channel_message', 'direct_message') and source_id:
                    if not _ctx_can_access_source(db_manager, feed_manager, user_id, source_type, source_id):
                        return jsonify({'success': False, 'error': 'Access denied'}), 403

                conn.execute(
                    "UPDATE content_contexts SET owner_note = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (note_text, context_id)
                )
                conn.commit()
                updated = conn.execute("SELECT * FROM content_contexts WHERE id = ?", (context_id,)).fetchone()

            return jsonify({'success': True, 'context': _ctx_serialize_row(updated, user_id, admin_user_id)})
        except Exception as e:
            logger.error(f"Update content context note error: {e}", exc_info=True)
            return jsonify({'success': False, 'error': 'Failed to update content context note'}), 500

    @ui.route('/ajax/tasks', methods=['GET'])
    @require_login
    def ajax_list_tasks():
        """Return collaborative tasks for the task board."""
        try:
            db_manager, _, _, _, _, _, _, _, profile_manager, _, _ = _get_app_components_any(current_app)
            task_manager = current_app.config.get('TASK_MANAGER')
            if not task_manager:
                return jsonify({'success': False, 'error': 'Task manager unavailable'}), 500

            status = request.args.get('status') or None
            tasks = task_manager.list_tasks(status=status)

            user_ids = set()
            for task in tasks:
                if task.created_by:
                    user_ids.add(task.created_by)
                if task.assigned_to:
                    user_ids.add(task.assigned_to)
                if task.updated_by:
                    user_ids.add(task.updated_by)

            user_info = {}
            for uid in user_ids:
                profile = profile_manager.get_profile(uid) if profile_manager else None
                if profile:
                    user_info[uid] = {
                        'display_name': profile.display_name or profile.username or uid,
                        'avatar_url': profile.avatar_url,
                        'username': profile.username,
                        'origin_peer': getattr(profile, 'origin_peer', None),
                    }
                else:
                    origin_peer = None
                    try:
                        if db_manager:
                            row = db_manager.get_user(uid)
                            if row:
                                origin_peer = row.get('origin_peer')
                    except Exception:
                        origin_peer = None
                    user_info[uid] = {
                        'display_name': uid,
                        'avatar_url': None,
                        'username': uid,
                        'origin_peer': origin_peer,
                    }

            return jsonify({
                'success': True,
                'tasks': [t.to_dict() for t in tasks],
                'users': user_info,
            })
        except Exception as e:
            logger.error(f"List tasks error: {e}", exc_info=True)
            return jsonify({'success': False, 'error': 'Failed to list tasks'}), 500

    @ui.route('/ajax/tasks', methods=['POST'])
    @require_login
    def ajax_create_task():
        """Create a new collaborative task."""
        try:
            db_manager, _, _, _, _, _, _, _, profile_manager, _, p2p_manager = _get_app_components_any(current_app)
            task_manager = current_app.config.get('TASK_MANAGER')
            if not task_manager:
                return jsonify({'success': False, 'error': 'Task manager unavailable'}), 500

            user_id = get_current_user()
            data = request.get_json() or {}
            title = (data.get('title') or '').strip()
            description = (data.get('description') or '').strip() or None
            status = data.get('status')
            priority = data.get('priority')
            assigned_to = data.get('assigned_to') or None
            due_at = data.get('due_at') or None
            visibility = data.get('visibility') or 'network'
            metadata = data.get('metadata') if isinstance(data.get('metadata'), dict) else None

            if not title:
                return jsonify({'success': False, 'error': 'Title is required'}), 400

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
                created_by=user_id,
                assigned_to=assigned_to,
                due_at=due_at,
                visibility=visibility,
                metadata=metadata,
                origin_peer=origin_peer,
                source_type='human',
                updated_by=user_id,
            )

            if not task:
                return jsonify({'success': False, 'error': 'Failed to create task'}), 500

            if visibility == 'network' and p2p_manager and p2p_manager.is_running():
                try:
                    display_name = None
                    if profile_manager:
                        profile = profile_manager.get_profile(user_id)
                        if profile:
                            display_name = profile.display_name or profile.username
                    p2p_manager.broadcast_interaction(
                        item_id=task.id,
                        user_id=user_id,
                        action='task_create',
                        item_type='task',
                        display_name=display_name,
                        extra={'task': task.to_dict()},
                    )
                except Exception as p2p_err:
                    logger.warning(f"Failed to broadcast task create: {p2p_err}")

            return jsonify({'success': True, 'task': task.to_dict()})
        except Exception as e:
            logger.error(f"Create task error: {e}", exc_info=True)
            return jsonify({'success': False, 'error': 'Failed to create task'}), 500

    @ui.route('/ajax/tasks/<task_id>', methods=['POST'])
    @require_login
    def ajax_update_task(task_id):
        """Update a task (status/assignment/metadata)."""
        try:
            db_manager, _, _, _, _, _, _, _, profile_manager, _, p2p_manager = _get_app_components_any(current_app)
            task_manager = current_app.config.get('TASK_MANAGER')
            if not task_manager:
                return jsonify({'success': False, 'error': 'Task manager unavailable'}), 500

            data = request.get_json() or {}
            user_id = get_current_user()

            updates = {}
            for key in ('title', 'description', 'status', 'priority', 'assigned_to', 'due_at', 'visibility', 'metadata'):
                if key in data:
                    updates[key] = data.get(key)

            try:
                task = task_manager.update_task(task_id, updates, actor_id=user_id,
                                                admin_user_id=db_manager.get_instance_owner_user_id() if _is_admin() else None)
            except PermissionError:
                return jsonify({'success': False, 'error': 'Not authorized to update task'}), 403
            if not task:
                return jsonify({'success': False, 'error': 'Task not found'}), 404

            if task.visibility == 'network' and p2p_manager and p2p_manager.is_running():
                try:
                    display_name = None
                    if profile_manager:
                        profile = profile_manager.get_profile(user_id)
                        if profile:
                            display_name = profile.display_name or profile.username
                    p2p_manager.broadcast_interaction(
                        item_id=task.id,
                        user_id=user_id,
                        action='task_update',
                        item_type='task',
                        display_name=display_name,
                        extra={'task': task.to_dict()},
                    )
                except Exception as p2p_err:
                    logger.warning(f"Failed to broadcast task update: {p2p_err}")

            return jsonify({'success': True, 'task': task.to_dict()})
        except Exception as e:
            logger.error(f"Update task error: {e}", exc_info=True)
            return jsonify({'success': False, 'error': 'Failed to update task'}), 500

    @ui.route('/ajax/signals/<signal_id>', methods=['POST'])
    @require_login
    def ajax_update_signal(signal_id):
        """Update or lock a signal."""
        try:
            db_manager = current_app.config.get('DB_MANAGER')
            signal_manager = current_app.config.get('SIGNAL_MANAGER')
            if not signal_manager:
                return jsonify({'success': False, 'error': 'Signal manager unavailable'}), 500

            data = request.get_json() or {}
            user_id = get_current_user()

            if 'locked' in data:
                sig = signal_manager.lock_signal(signal_id, actor_id=user_id, locked=bool(data.get('locked')))
                if not sig:
                    return jsonify({'success': False, 'error': 'Not found or not authorized'}), 404
            else:
                updates: dict[str, Any] = {}
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
                    owner_id = (
                        _resolve_handle_to_user_id(db_manager, str(owner), author_id=user_id)
                        if owner
                        else None
                    )
                    updates['owner_id'] = owner_id or owner

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
                        return jsonify({'success': False, 'error': 'Signal not found'}), 404
                else:
                    result = signal_manager.update_signal(signal_id, updates, actor_id=user_id)
                    if not result:
                        return jsonify({'success': False, 'error': 'Signal not found'}), 404
                    if isinstance(result, dict) and result.get('proposal_version'):
                        return jsonify({'success': True, 'proposal': result})
                    sig = result

            payload = sig
            payload['status_label'] = (payload.get('status') or 'active').replace('_', ' ').title()
            payload['type_label'] = (payload.get('type') or 'signal').replace('_', ' ').title()
            expiry_dt = None
            if payload.get('expires_at'):
                try:
                    expiry_dt = datetime.fromisoformat(payload['expires_at'].replace('Z', '+00:00'))
                except Exception:
                    expiry_dt = None
            payload['expires_label'] = _signal_expiry_label(expiry_dt)
            payload['confidence_percent'] = int(round((payload.get('confidence') or 0) * 100))
            admin_user_id = db_manager.get_instance_owner_user_id() if db_manager else None
            payload['can_manage'] = user_id == payload.get('owner_id') or (admin_user_id and user_id == admin_user_id)

            return jsonify({'success': True, 'signal': payload})
        except Exception as e:
            logger.error(f"Update signal error: {e}", exc_info=True)
            return jsonify({'success': False, 'error': 'Failed to update signal'}), 500

    @ui.route('/ajax/requests/<request_id>', methods=['POST'])
    @require_login
    def ajax_update_request(request_id):
        """Update a request (status, priority, due date, members)."""
        try:
            db_manager, _, _, _, _, _, _, _, profile_manager, _, _ = _get_app_components_any(current_app)
            request_manager = current_app.config.get('REQUEST_MANAGER')
            if not request_manager:
                return jsonify({'success': False, 'error': 'Request manager unavailable'}), 500

            data = request.get_json() or {}
            user_id = get_current_user()
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
                    return jsonify({'success': False, 'error': 'Invalid status', 'allowed': list(REQUEST_STATUSES)}), 400
                updates['status'] = status_clean
            if 'priority' in updates and updates.get('priority') is not None:
                priority_clean = str(updates.get('priority')).strip().lower()
                if priority_clean not in REQUEST_PRIORITIES:
                    return jsonify({'success': False, 'error': 'Invalid priority', 'allowed': list(REQUEST_PRIORITIES)}), 400
                updates['priority'] = priority_clean

            members_payload = None
            replace_members = False
            if 'members' in data:
                replace_members = True
                members_payload = []
                for member in data.get('members') or []:
                    if isinstance(member, str):
                        uid = _resolve_handle_to_user_id(db_manager, member, author_id=user_id)
                        if uid:
                            members_payload.append({'user_id': uid, 'role': 'assignee'})
                        continue
                    if not isinstance(member, dict):
                        continue
                    uid = member.get('user_id') or None
                    handle = member.get('handle') or member.get('name') or None
                    if not uid and handle:
                        uid = _resolve_handle_to_user_id(db_manager, handle, author_id=user_id)
                    if uid:
                        members_payload.append({'user_id': uid, 'role': member.get('role') or 'assignee'})

            if not updates and not replace_members:
                return jsonify({'success': False, 'error': 'No updates provided'}), 400

            try:
                admin_id = None
                if _is_admin():
                    try:
                        admin_id = db_manager.get_instance_owner_user_id()
                    except Exception:
                        admin_id = None
                req = request_manager.update_request(
                    request_id,
                    updates,
                    actor_id=user_id,
                    admin_user_id=admin_id,
                    members=members_payload,
                    replace_members=replace_members,
                )
            except PermissionError:
                return jsonify({'success': False, 'error': 'Not authorized'}), 403

            if not req:
                return jsonify({'success': False, 'error': 'Request not found'}), 404

            req['status_label'] = (req.get('status') or 'open').replace('_', ' ').title()
            req['priority_label'] = (req.get('priority') or 'normal').replace('_', ' ').title()
            due_dt = None
            if req.get('due_at'):
                try:
                    due_dt = datetime.fromisoformat(req['due_at'].replace('Z', '+00:00'))
                except Exception:
                    due_dt = None
            req['due_label'] = _request_due_label(due_dt)

            members_display = []
            for member in req.get('members', []) or []:
                uid = member.get('user_id')
                display = None
                if uid and profile_manager:
                    try:
                        profile = profile_manager.get_profile(uid)
                        if profile:
                            display = {
                                'display_name': profile.display_name or profile.username or uid,
                                'avatar_url': profile.avatar_url,
                                'origin_peer': getattr(profile, 'origin_peer', None),
                            }
                    except Exception:
                        display = None
                members_display.append({
                    'user_id': uid,
                    'role': member.get('role') or 'assignee',
                    'display_name': (display or {}).get('display_name') if display else uid,
                    'avatar_url': (display or {}).get('avatar_url') if display else None,
                    'origin_peer': (display or {}).get('origin_peer') if display else None,
                })
            req['members'] = members_display

            member_ids = [m.get('user_id') for m in req.get('members', []) if m.get('user_id')]
            admin_user_id = None
            try:
                admin_user_id = db_manager.get_instance_owner_user_id()
            except Exception:
                admin_user_id = None
            req['can_manage'] = (
                user_id == req.get('created_by')
                or (admin_user_id and user_id == admin_user_id)
                or (user_id in member_ids)
            )

            return jsonify({'success': True, 'request': req})
        except Exception as e:
            logger.error(f"Update request error: {e}", exc_info=True)
            return jsonify({'success': False, 'error': 'Failed to update request'}), 500

    @ui.route('/ajax/contracts/<contract_id>', methods=['POST'])
    @require_login
    def ajax_update_contract(contract_id):
        """Update a contract (status/content as allowed by role)."""
        try:
            db_manager, _, _, _, _, _, _, _, profile_manager, _, _ = get_app_components(current_app)
            contract_manager = current_app.config.get('CONTRACT_MANAGER')
            if not contract_manager:
                return jsonify({'success': False, 'error': 'Contract manager unavailable'}), 500

            from ..core.contracts import CONTRACT_STATUSES

            data = request.get_json() or {}
            user_id = get_current_user()
            updates = {}

            for key in ('title', 'summary', 'terms', 'status', 'visibility', 'expires_at', 'ttl_seconds', 'ttl_mode', 'metadata'):
                if key in data:
                    updates[key] = data.get(key)
            if 'description' in data and 'summary' not in updates:
                updates['summary'] = data.get('description')
            if 'owner' in data or 'owner_id' in data:
                owner = data.get('owner') or data.get('owner_id')
                owner_id = _resolve_handle_to_user_id(db_manager, owner, author_id=user_id) if owner else None
                updates['owner_id'] = owner_id or owner
            if 'counterparties' in data or 'participants' in data:
                raw_cp = data.get('counterparties')
                if raw_cp is None:
                    raw_cp = data.get('participants')
                if isinstance(raw_cp, str):
                    raw_cp = [p.strip() for p in re.split(r"[,;]", raw_cp) if p.strip()]
                counterparties = []
                for cp in raw_cp or []:
                    cp_id = _resolve_handle_to_user_id(db_manager, cp, author_id=user_id)
                    if cp_id:
                        counterparties.append(cp_id)
                updates['counterparties'] = counterparties

            if 'status' in updates and updates.get('status') is not None:
                status_clean = str(updates.get('status')).strip().lower()
                if status_clean not in CONTRACT_STATUSES:
                    return jsonify({'success': False, 'error': 'Invalid status', 'allowed': list(CONTRACT_STATUSES)}), 400
                updates['status'] = status_clean

            if not updates:
                return jsonify({'success': False, 'error': 'No updates provided'}), 400

            admin_user_id = None
            try:
                if db_manager and _is_admin():
                    admin_user_id = db_manager.get_instance_owner_user_id()
            except Exception:
                admin_user_id = None

            try:
                contract = contract_manager.update_contract(
                    contract_id,
                    updates,
                    actor_id=user_id,
                    admin_user_id=admin_user_id,
                )
            except PermissionError:
                return jsonify({'success': False, 'error': 'Not authorized'}), 403

            if not contract:
                return jsonify({'success': False, 'error': 'Contract not found'}), 404

            contract['status_label'] = (contract.get('status') or 'proposed').replace('_', ' ').title()
            expiry_dt = None
            if contract.get('expires_at'):
                try:
                    expiry_dt = datetime.fromisoformat(contract['expires_at'].replace('Z', '+00:00'))
                except Exception:
                    expiry_dt = None
            contract['expires_label'] = _signal_expiry_label(expiry_dt)

            owner_id = contract.get('owner_id')
            owner_profile = profile_manager.get_profile(owner_id) if profile_manager and owner_id else None
            contract['owner_name'] = (
                (owner_profile.display_name or owner_profile.username)
                if owner_profile else owner_id
            )
            contract['owner_avatar_url'] = owner_profile.avatar_url if owner_profile else None
            contract['owner_origin_peer'] = getattr(owner_profile, 'origin_peer', None) if owner_profile else None

            counterparty_users = []
            for cp_id in contract.get('counterparties') or []:
                cp_profile = profile_manager.get_profile(cp_id) if profile_manager and cp_id else None
                counterparty_users.append({
                    'user_id': cp_id,
                    'display_name': (cp_profile.display_name or cp_profile.username) if cp_profile else cp_id,
                    'avatar_url': cp_profile.avatar_url if cp_profile else None,
                    'origin_peer': getattr(cp_profile, 'origin_peer', None) if cp_profile else None,
                })
            contract['counterparty_users'] = counterparty_users

            allowed_ids = {contract.get('owner_id'), contract.get('created_by')}
            allowed_ids.update(set(contract.get('counterparties') or []))
            contract['can_manage'] = (
                user_id == contract.get('owner_id')
                or user_id == contract.get('created_by')
                or (admin_user_id and user_id == admin_user_id)
            )
            contract['can_participate'] = bool(user_id and user_id in allowed_ids)

            return jsonify({'success': True, 'contract': contract})
        except Exception as e:
            logger.error(f"Update contract error: {e}", exc_info=True)
            return jsonify({'success': False, 'error': 'Failed to update contract'}), 500

    @ui.route('/ajax/circle/<circle_id>', methods=['GET'])
    @require_login
    def ajax_get_circle(circle_id):
        """Fetch circle details + entries for the modal UI."""
        try:
            db_manager, _, trust_manager, _, _, _, _, _, profile_manager, _, _ = _get_app_components_any(current_app)
            user_id = get_current_user()
            circle_manager = current_app.config.get('CIRCLE_MANAGER')
            if not circle_manager:
                return jsonify({'success': False, 'error': 'Circle manager unavailable'}), 500

            circle = circle_manager.get_circle(circle_id)
            if not circle:
                return jsonify({'success': False, 'error': 'Circle not found'}), 404

            circle_payload = circle.to_dict()
            if circle.facilitator_id:
                try:
                    fac_display = None
                    fac_avatar = None
                    fac_origin = None
                    if profile_manager:
                        prof = profile_manager.get_profile(circle.facilitator_id)
                        if prof:
                            fac_display = prof.display_name or prof.username or circle.facilitator_id
                            fac_avatar = prof.avatar_url
                            fac_origin = getattr(prof, 'origin_peer', None)
                    if not fac_display:
                        row = db_manager.get_user(circle.facilitator_id)
                        if row:
                            fac_display = row.get('display_name') or row.get('username') or circle.facilitator_id
                            fac_origin = fac_origin or row.get('origin_peer')
                    circle_payload['facilitator_name'] = fac_display or circle.facilitator_id
                    circle_payload['facilitator_avatar_url'] = fac_avatar
                    circle_payload['facilitator_origin_peer'] = fac_origin
                except Exception:
                    circle_payload['facilitator_name'] = circle.facilitator_id

            entries = circle_manager.list_entries(circle_id)
            entries_payload = []
            for entry in entries:
                display_name = entry['user_id']
                avatar_url = None
                origin_peer = None
                try:
                    if profile_manager:
                        prof = profile_manager.get_profile(entry['user_id'])
                        if prof:
                            display_name = prof.display_name or prof.username or entry['user_id']
                            avatar_url = prof.avatar_url
                            origin_peer = getattr(prof, 'origin_peer', None)
                    if not avatar_url:
                        row = db_manager.get_user(entry['user_id'])
                        if row:
                            origin_peer = origin_peer or row.get('origin_peer')
                except Exception:
                    pass
                can_edit = False
                try:
                    if entry['user_id'] == user_id:
                        created_dt = datetime.fromisoformat(str(entry['created_at']).replace('Z', '+00:00'))
                        if circle.edit_window_seconds <= 0:
                            can_edit = True
                        else:
                            can_edit = datetime.now(timezone.utc) <= created_dt + timedelta(seconds=circle.edit_window_seconds)
                except Exception:
                    can_edit = False

                entries_payload.append({
                    **entry,
                    'display_name': display_name,
                    'avatar_url': avatar_url,
                    'origin_peer': origin_peer,
                    'can_edit': can_edit,
                })

            opinions_used = circle_manager.count_entries(
                circle_id, user_id, 'opinion', round_number=getattr(circle, 'round_number', 1)
            )
            clarify_used = circle_manager.count_entries(circle_id, user_id, 'clarify')
            remaining_opinions = max(0, circle.opinion_limit - opinions_used)
            remaining_clarify = max(0, circle.clarify_limit - clarify_used)

            is_facilitator = user_id == circle.facilitator_id or _is_admin()
            can_post = False
            allowed_entry_type = None
            if circle.phase == 'opinion' and remaining_opinions > 0:
                can_post = True
                allowed_entry_type = 'opinion'
            elif circle.phase == 'clarify' and remaining_clarify > 0:
                can_post = True
                allowed_entry_type = 'clarify'
            elif circle.phase in ('synthesis', 'decision') and is_facilitator:
                can_post = True
                allowed_entry_type = 'summary' if circle.phase == 'synthesis' else 'decision'

            vote_counts = None
            user_vote = None
            if circle.decision_mode == 'vote':
                vote_counts = circle_manager.get_vote_counts(circle_id)
                user_vote = circle_manager.get_user_vote(circle_id, user_id)

            return jsonify({
                'success': True,
                'circle': circle_payload,
                'entries': entries_payload,
                'permissions': {
                    'can_post': can_post,
                    'allowed_entry_type': allowed_entry_type,
                    'remaining_opinions': remaining_opinions,
                    'remaining_clarify': remaining_clarify,
                    'can_moderate': is_facilitator,
                },
                'votes': vote_counts,
                'user_vote': user_vote,
            })
        except Exception as e:
            logger.error(f"Get circle error: {e}", exc_info=True)
            return jsonify({'success': False, 'error': 'Failed to load circle'}), 500

    @ui.route('/ajax/circle/<circle_id>/entries', methods=['POST'])
    @require_login
    def ajax_add_circle_entry(circle_id):
        """Add an entry to a circle (opinion/clarify/summary/decision)."""
        try:
            db_manager, _, _, _, _, _, _, _, profile_manager, _, p2p_manager = _get_app_components_any(current_app)
            user_id = get_current_user()
            circle_manager = current_app.config.get('CIRCLE_MANAGER')
            if not circle_manager:
                return jsonify({'success': False, 'error': 'Circle manager unavailable'}), 500
            data = request.get_json() or {}
            content = (data.get('content') or '').strip()
            entry_type = (data.get('entry_type') or '').strip().lower()
            if not content:
                return jsonify({'success': False, 'error': 'Content required'}), 400

            entry = circle_manager.add_entry(
                circle_id=circle_id,
                user_id=user_id,
                entry_type=entry_type,
                content=content,
                admin_user_id=db_manager.get_instance_owner_user_id() if _is_admin() else None,
            )
            if not entry:
                return jsonify({'success': False, 'error': 'Not authorized or invalid'}), 403

            circle = circle_manager.get_circle(circle_id)
            if circle and circle.visibility == 'network' and p2p_manager and p2p_manager.is_running():
                try:
                    display_name = None
                    if profile_manager:
                        prof = profile_manager.get_profile(user_id)
                        if prof:
                            display_name = prof.display_name or prof.username
                    p2p_manager.broadcast_interaction(
                        item_id=entry['id'],
                        user_id=user_id,
                        action='circle_entry',
                        item_type='circle_entry',
                        display_name=display_name,
                        extra={'circle_id': circle_id, 'entry': entry},
                    )
                except Exception as bcast_err:
                    logger.warning(f"Failed to broadcast circle entry: {bcast_err}")

            return jsonify({'success': True, 'entry': entry})
        except Exception as e:
            logger.error(f"Add circle entry error: {e}", exc_info=True)
            return jsonify({'success': False, 'error': 'Failed to add entry'}), 500

    @ui.route('/ajax/circle/<circle_id>/entries/<entry_id>', methods=['POST'])
    @require_login
    def ajax_update_circle_entry(circle_id, entry_id):
        """Update an existing circle entry within the edit window."""
        try:
            db_manager, _, _, _, _, _, _, _, profile_manager, _, p2p_manager = _get_app_components_any(current_app)
            user_id = get_current_user()
            circle_manager = current_app.config.get('CIRCLE_MANAGER')
            if not circle_manager:
                return jsonify({'success': False, 'error': 'Circle manager unavailable'}), 500
            data = request.get_json() or {}
            content = (data.get('content') or '').strip()
            if not content:
                return jsonify({'success': False, 'error': 'Content required'}), 400

            entry = circle_manager.update_entry(
                circle_id=circle_id,
                entry_id=entry_id,
                user_id=user_id,
                content=content,
                admin_user_id=db_manager.get_instance_owner_user_id() if _is_admin() else None,
            )
            if not entry:
                return jsonify({'success': False, 'error': 'Not authorized or edit window expired'}), 403

            circle = circle_manager.get_circle(circle_id)
            if circle and circle.visibility == 'network' and p2p_manager and p2p_manager.is_running():
                try:
                    display_name = None
                    if profile_manager:
                        prof = profile_manager.get_profile(user_id)
                        if prof:
                            display_name = prof.display_name or prof.username
                    p2p_manager.broadcast_interaction(
                        item_id=entry['id'],
                        user_id=user_id,
                        action='circle_entry',
                        item_type='circle_entry',
                        display_name=display_name,
                        extra={'circle_id': circle_id, 'entry': entry},
                    )
                except Exception as bcast_err:
                    logger.warning(f"Failed to broadcast circle entry update: {bcast_err}")

            return jsonify({'success': True, 'entry': entry})
        except Exception as e:
            logger.error(f"Update circle entry error: {e}", exc_info=True)
            return jsonify({'success': False, 'error': 'Failed to update entry'}), 500

    @ui.route('/ajax/circle/<circle_id>/phase', methods=['POST'])
    @require_login
    def ajax_update_circle_phase(circle_id):
        """Update a circle phase (facilitator/admin only)."""
        try:
            db_manager, _, _, _, _, _, _, _, profile_manager, _, p2p_manager = _get_app_components_any(current_app)
            user_id = get_current_user()
            circle_manager = current_app.config.get('CIRCLE_MANAGER')
            if not circle_manager:
                return jsonify({'success': False, 'error': 'Circle manager unavailable'}), 500
            data = request.get_json() or {}
            phase = (data.get('phase') or '').strip().lower()
            circle = circle_manager.update_phase(
                circle_id=circle_id,
                new_phase=phase,
                actor_id=user_id,
                admin_user_id=db_manager.get_instance_owner_user_id() if _is_admin() else None,
            )
            if not circle:
                return jsonify({'success': False, 'error': 'Not authorized or invalid phase'}), 403

            if circle.visibility == 'network' and p2p_manager and p2p_manager.is_running():
                try:
                    display_name = None
                    if profile_manager:
                        prof = profile_manager.get_profile(user_id)
                        if prof:
                            display_name = prof.display_name or prof.username
                    p2p_manager.broadcast_interaction(
                        item_id=circle.id,
                        user_id=user_id,
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

            return jsonify({'success': True, 'circle': circle.to_dict()})
        except Exception as e:
            logger.error(f"Update circle phase error: {e}", exc_info=True)
            return jsonify({'success': False, 'error': 'Failed to update phase'}), 500

    @ui.route('/ajax/circle/<circle_id>/vote', methods=['POST'])
    @require_login
    def ajax_vote_circle(circle_id):
        """Vote on a circle decision (if decision mode is vote)."""
        try:
            db_manager, _, _, _, _, _, _, _, profile_manager, _, p2p_manager = _get_app_components_any(current_app)
            user_id = get_current_user()
            circle_manager = current_app.config.get('CIRCLE_MANAGER')
            if not circle_manager:
                return jsonify({'success': False, 'error': 'Circle manager unavailable'}), 500
            data = request.get_json() or {}
            option_index = data.get('option_index')
            if option_index is None:
                return jsonify({'success': False, 'error': 'option_index required'}), 400

            vote = circle_manager.record_vote(circle_id, user_id, int(option_index))
            if not vote:
                return jsonify({'success': False, 'error': 'Not authorized or invalid vote'}), 403

            circle = circle_manager.get_circle(circle_id)
            if circle and circle.visibility == 'network' and p2p_manager and p2p_manager.is_running():
                try:
                    display_name = None
                    if profile_manager:
                        prof = profile_manager.get_profile(user_id)
                        if prof:
                            display_name = prof.display_name or prof.username
                    p2p_manager.broadcast_interaction(
                        item_id=circle.id,
                        user_id=user_id,
                        action='circle_vote',
                        item_type='circle',
                        display_name=display_name,
                        extra={'circle_id': circle.id, 'option_index': int(option_index), 'created_at': datetime.now(timezone.utc).isoformat()},
                    )
                except Exception as bcast_err:
                    logger.warning(f"Failed to broadcast circle vote: {bcast_err}")

            return jsonify({'success': True, 'vote': vote})
        except Exception as e:
            logger.error(f"Vote circle error: {e}", exc_info=True)
            return jsonify({'success': False, 'error': 'Failed to vote'}), 500
    
    @ui.route('/ajax/create_post', methods=['POST'])
    @require_login
    def ajax_create_post():
        """AJAX endpoint to create a new feed post."""
        try:
            db_manager, _, _, _, channel_manager, file_manager, feed_manager, _, profile_manager, _, p2p_manager = _get_app_components_any(current_app)
            user_id = get_current_user()
            
            data = request.get_json()
            content = data.get('content', '').strip()
            post_type = data.get('post_type', 'text')
            visibility = data.get('visibility', 'network')
            permissions = data.get('permissions', [])
            metadata = data.get('metadata')
            file_attachments = data.get('attachments', [])
            expires_at = data.get('expires_at')
            ttl_seconds = data.get('ttl_seconds')
            ttl_mode = data.get('ttl_mode')
            
            if not content and not file_attachments:
                return jsonify({'error': 'Post content or attachments required'}), 400
            
            # Process file attachments if any (same as channel messages)
            processed_attachments = []
            for attachment in file_attachments:
                try:
                    # Attachment should contain file data as base64
                    file_data = base64.b64decode(attachment['data'])
                    file_info = file_manager.save_file(
                        file_data, 
                        attachment['name'], 
                        attachment['type'], 
                        user_id
                    )
                    
                    if file_info:
                        processed_attachments.append({
                            'id': file_info.id,
                            'name': file_info.original_name,
                            'type': file_info.content_type,
                            'size': file_info.size,
                            'url': file_info.url
                        })
                except Exception as e:
                    logger.error(f"Failed to process attachment {attachment.get('name', 'unknown')}: {e}")
                    continue
            
            # Create feed post using FeedManager
            from ..core.feed import PostType, PostVisibility
            
            from ..core.polls import parse_poll, poll_edit_lock_reason
            from ..core.tasks import parse_task_blocks, derive_task_id
            from ..core.objectives import parse_objective_blocks, derive_objective_id
            from ..core.requests import parse_request_blocks, derive_request_id
            from ..core.signals import parse_signal_blocks, derive_signal_id
            poll_spec = parse_poll(content) if post_type == 'text' else None

            # Convert post_type to PostType enum
            try:
                post_type_enum = PostType(post_type if post_type in ['text', 'link', 'image', 'video', 'audio', 'poll'] else 'text')
            except ValueError as e:
                return jsonify({'error': f'Invalid post type: {e}'}), 400

            if poll_spec and post_type_enum == PostType.TEXT:
                post_type_enum = PostType.POLL
                post_type = 'poll'
            
            # Convert visibility to PostVisibility enum
            try:
                visibility_enum = PostVisibility(visibility if visibility in ['network', 'trusted', 'public', 'private', 'custom'] else 'network')
            except ValueError as e:
                return jsonify({'error': f'Invalid visibility: {e}'}), 400
            
            # For media posts, add the first attachment URL to metadata for proper display
            final_metadata = metadata or {}
            try:
                origin_peer = p2p_manager.get_peer_id() if p2p_manager else None
                if origin_peer and not final_metadata.get('origin_peer'):
                    final_metadata['origin_peer'] = origin_peer
            except Exception:
                pass
            if post_type_enum in [PostType.IMAGE, PostType.VIDEO, PostType.AUDIO] and processed_attachments:
                first_attachment = processed_attachments[0]
                if post_type_enum == PostType.IMAGE:
                    final_metadata['image_url'] = first_attachment['url']
                elif post_type_enum == PostType.VIDEO:
                    final_metadata['video_url'] = first_attachment['url']
                elif post_type_enum == PostType.AUDIO:
                    final_metadata['audio_url'] = first_attachment['url']
            
            # Store attachments in metadata for display
            if processed_attachments:
                final_metadata['attachments'] = processed_attachments
            
            # Extract source classification and tags
            source_type = data.get('source_type', 'human')
            if source_type not in ('human', 'agent', 'agent_curated', 'system'):
                source_type = 'human'
            tags_raw = data.get('tags', [])
            tags_list = [str(t).strip().lower() for t in tags_raw if str(t).strip()] if isinstance(tags_raw, list) else []

            post = feed_manager.create_post(
                author_id=user_id,
                content=content,
                post_type=post_type_enum,
                visibility=visibility_enum,
                metadata=final_metadata,
                permissions=permissions,
                source_type=source_type,
                tags=tags_list if tags_list else None,
                expires_at=expires_at,
                ttl_seconds=ttl_seconds,
                ttl_mode=ttl_mode,
            )
            
            if post:
                # Inline circle creation from [circle] blocks
                try:
                    circle_manager = current_app.config.get('CIRCLE_MANAGER')
                    circle_specs: list[Any] = []
                    if circle_manager:
                        from ..core.circles import parse_circle_blocks, derive_circle_id
                        circle_specs = parse_circle_blocks(content or '')
                    if circle_specs and circle_manager:
                        for idx, spec in enumerate(cast(Any, circle_specs)):
                            spec = cast(Any, spec)
                            circle_id = derive_circle_id('feed', post.id, idx, len(circle_specs), override=spec.circle_id)
                            facilitator_id = None
                            if spec.facilitator:
                                facilitator_id = _resolve_handle_to_user_id(
                                    db_manager,
                                    spec.facilitator,
                                    visibility=visibility_enum.value,
                                    permissions=permissions,
                                    author_id=user_id,
                                )
                            if not facilitator_id:
                                facilitator_id = user_id
                            if spec.participants is not None:
                                resolved_participants = _resolve_handle_list(
                                    db_manager,
                                    spec.participants,
                                    visibility=visibility_enum.value,
                                    permissions=permissions,
                                    author_id=user_id,
                                )
                                spec.participants = resolved_participants

                            circle_manager.upsert_circle(
                                circle_id=circle_id,
                                source_type='feed',
                                source_id=post.id,
                                created_by=user_id,
                                spec=spec,
                                facilitator_id=facilitator_id,
                                visibility=visibility_enum.value,
                                origin_peer=p2p_manager.get_peer_id() if p2p_manager else None,
                                created_at=post.created_at.isoformat() if getattr(post, 'created_at', None) else None,
                            )
                except Exception as circle_err:
                    logger.warning(f"Inline circle creation failed: {circle_err}")

                # Inline task creation from [task] blocks (auto-confirmed by default)
                try:
                    task_manager = current_app.config.get('TASK_MANAGER')
                    if task_manager:
                        task_specs = parse_task_blocks(content or '')
                        if task_specs:
                            # Determine task visibility from post visibility
                            if visibility_enum.value in ('public', 'network'):
                                task_visibility = 'network'
                            else:
                                task_visibility = 'local'

                            for idx, spec in enumerate(cast(Any, task_specs)):
                                spec = cast(Any, spec)
                                if not spec.confirmed:
                                    continue
                                task_id = derive_task_id('feed', post.id, idx, len(task_specs), override=spec.task_id)
                                assignee_id = _resolve_handle_to_user_id(
                                    db_manager,
                                    spec.assignee,
                                    visibility=visibility_enum.value,
                                    permissions=permissions,
                                    author_id=user_id,
                                )
                                editor_ids = _resolve_handle_list(
                                    db_manager,
                                    spec.editors or [],
                                    visibility=visibility_enum.value,
                                    permissions=permissions,
                                    author_id=user_id,
                                )
                                meta_payload = {
                                    'inline_task': True,
                                    'source_type': 'feed_post',
                                    'source_id': post.id,
                                    'post_visibility': visibility_enum.value,
                                }
                                if editor_ids:
                                    meta_payload['editors'] = editor_ids

                                task = task_manager.create_task(
                                    task_id=task_id,
                                    title=spec.title,
                                    description=spec.description,
                                    status=spec.status,
                                    priority=spec.priority,
                                    created_by=user_id,
                                    assigned_to=assignee_id,
                                    due_at=spec.due_at.isoformat() if spec.due_at else None,
                                    visibility=task_visibility,
                                    metadata=meta_payload,
                                    origin_peer=p2p_manager.get_peer_id() if p2p_manager else None,
                                    source_type='human',
                                    updated_by=user_id,
                                )

                                if task and task.visibility == 'network' and p2p_manager and p2p_manager.is_running():
                                    try:
                                        sender_display = None
                                        if profile_manager:
                                            profile = profile_manager.get_profile(user_id)
                                            if profile:
                                                sender_display = profile.display_name or profile.username
                                        p2p_manager.broadcast_interaction(
                                            item_id=task.id,
                                            user_id=user_id,
                                            action='task_create',
                                            item_type='task',
                                            display_name=sender_display,
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
                        objective_specs = parse_objective_blocks(content or '')
                        if objective_specs:
                            obj_visibility = 'network' if visibility_enum.value in ('public', 'network') else 'local'
                            for idx, spec in enumerate(cast(Any, objective_specs)):
                                spec = cast(Any, spec)
                                objective_id = derive_objective_id('feed', post.id, idx, len(objective_specs), override=spec.objective_id)
                                members_payload = []
                                for member in spec.members or []:
                                    uid = _resolve_handle_to_user_id(
                                        db_manager,
                                        member.handle,
                                        visibility=visibility_enum.value,
                                        permissions=permissions,
                                        author_id=user_id,
                                    )
                                    if uid:
                                        members_payload.append({'user_id': uid, 'role': member.role})
                                tasks_payload = []
                                for t in spec.tasks or []:
                                    assignee_id = None
                                    if t.assignee:
                                        assignee_id = _resolve_handle_to_user_id(
                                            db_manager,
                                            t.assignee,
                                            visibility=visibility_enum.value,
                                            permissions=permissions,
                                            author_id=user_id,
                                        )
                                    tasks_payload.append({
                                        'title': t.title,
                                        'status': t.status,
                                        'assigned_to': assignee_id,
                                        'metadata': {
                                            'inline_objective_task': True,
                                            'source_type': 'feed_post',
                                            'source_id': post.id,
                                            'post_visibility': visibility_enum.value,
                                        },
                                    })
                                objective_manager.upsert_objective(
                                    objective_id=objective_id,
                                    title=spec.title,
                                    description=spec.description,
                                    status=spec.status,
                                    deadline=spec.deadline.isoformat() if spec.deadline else None,
                                    created_by=user_id,
                                    visibility=obj_visibility,
                                    origin_peer=p2p_manager.get_peer_id() if p2p_manager else None,
                                    source_type='feed_post',
                                    source_id=post.id,
                                    created_at=post.created_at.isoformat() if getattr(post, 'created_at', None) else None,
                                    members=members_payload,
                                    tasks=tasks_payload,
                                    updated_by=user_id,
                                )
                except Exception as obj_err:
                    logger.warning(f"Inline objective creation failed: {obj_err}")

                # Inline request creation from [request] blocks
                try:
                    request_manager = current_app.config.get('REQUEST_MANAGER')
                    if request_manager:
                        request_specs = parse_request_blocks(content or '')
                        if request_specs:
                            req_visibility = 'network' if visibility_enum.value in ('public', 'network') else 'local'
                            for idx, spec in enumerate(cast(Any, request_specs)):
                                spec = cast(Any, spec)
                                if not spec.confirmed:
                                    continue
                                request_id = derive_request_id('feed', post.id, idx, len(request_specs), override=spec.request_id)
                                members_payload = []
                                for member in spec.members or []:
                                    uid = _resolve_handle_to_user_id(
                                        db_manager,
                                        member.handle,
                                        visibility=visibility_enum.value,
                                        permissions=permissions,
                                        author_id=user_id,
                                    )
                                    if uid:
                                        members_payload.append({'user_id': uid, 'role': member.role})

                                request_manager.upsert_request(
                                    request_id=request_id,
                                    title=spec.title,
                                    created_by=user_id,
                                    request_text=spec.request,
                                    required_output=spec.required_output,
                                    status=spec.status,
                                    priority=spec.priority,
                                    tags=spec.tags,
                                    due_at=spec.due_at.isoformat() if spec.due_at else None,
                                    visibility=req_visibility,
                                    origin_peer=p2p_manager.get_peer_id() if p2p_manager else None,
                                    source_type='feed_post',
                                    source_id=post.id,
                                    created_at=post.created_at.isoformat() if getattr(post, 'created_at', None) else None,
                                    actor_id=user_id,
                                    members=members_payload,
                                    members_defined=('members' in spec.fields),
                                    fields=spec.fields,
                                )
                except Exception as req_err:
                    logger.warning(f"Inline request creation failed: {req_err}")

                # Inline signal creation from [signal] blocks
                try:
                    signal_manager = current_app.config.get('SIGNAL_MANAGER')
                    if signal_manager:
                        signal_specs = parse_signal_blocks(content or '')
                        if signal_specs:
                            sig_visibility = 'network' if visibility_enum.value in ('public', 'network') else 'local'
                            for idx, spec in enumerate(cast(Any, signal_specs)):
                                spec = cast(Any, spec)
                                signal_id = derive_signal_id('feed', post.id, idx, len(signal_specs), override=spec.signal_id)
                                owner_id = None
                                if spec.owner:
                                    owner_id = _resolve_handle_to_user_id(
                                        db_manager,
                                        spec.owner,
                                        visibility=visibility_enum.value,
                                        permissions=permissions,
                                        author_id=user_id,
                                    )
                                if not owner_id:
                                    owner_id = user_id

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
                                    created_by=user_id,
                                    visibility=sig_visibility,
                                    origin_peer=p2p_manager.get_peer_id() if p2p_manager else None,
                                    source_type='feed_post',
                                    source_id=post.id,
                                    expires_at=spec.expires_at.isoformat() if spec.expires_at else None,
                                    ttl_seconds=spec.ttl_seconds,
                                    ttl_mode=spec.ttl_mode,
                                    created_at=post.created_at.isoformat() if getattr(post, 'created_at', None) else None,
                                    actor_id=user_id,
                                )
                except Exception as sig_err:
                    logger.warning(f"Inline signal creation failed: {sig_err}")

                # Inline contract creation from [contract] blocks
                try:
                    contract_manager = current_app.config.get('CONTRACT_MANAGER')
                    if contract_manager:
                        from ..core.contracts import parse_contract_blocks, derive_contract_id
                        contract_specs = parse_contract_blocks(content or '')
                        if contract_specs:
                            contract_visibility = 'network' if visibility_enum.value in ('public', 'network') else 'local'
                            for idx, spec in enumerate(contract_specs):
                                if not spec.confirmed:
                                    continue
                                contract_id = derive_contract_id('feed', post.id, idx, len(contract_specs), override=spec.contract_id)
                                owner_id = None
                                if spec.owner:
                                    owner_id = _resolve_handle_to_user_id(
                                        db_manager,
                                        spec.owner,
                                        visibility=visibility_enum.value,
                                        permissions=permissions,
                                        author_id=user_id,
                                    )
                                if not owner_id:
                                    owner_id = user_id

                                counterparties = []
                                for cp in spec.counterparties or []:
                                    cp_id = _resolve_handle_to_user_id(
                                        db_manager,
                                        cp,
                                        visibility=visibility_enum.value,
                                        permissions=permissions,
                                        author_id=user_id,
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
                                    created_by=user_id,
                                    visibility=contract_visibility,
                                    origin_peer=p2p_manager.get_peer_id() if p2p_manager else None,
                                    source_type='feed_post',
                                    source_id=post.id,
                                    expires_at=spec.expires_at.isoformat() if spec.expires_at else None,
                                    ttl_seconds=spec.ttl_seconds,
                                    ttl_mode=spec.ttl_mode,
                                    metadata=spec.metadata,
                                    created_at=post.created_at.isoformat() if getattr(post, 'created_at', None) else None,
                                    actor_id=user_id,
                                )
                except Exception as contract_err:
                    logger.warning(f"Inline contract creation failed: {contract_err}")

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
                                author_id=user_id,
                            )
                except Exception as skill_err:
                    logger.warning(f"Inline skill registration failed: {skill_err}")

                # Broadcast to P2P peers
                if p2p_manager and p2p_manager.is_running():
                    try:
                        sender_display = None
                        if profile_manager:
                            profile = profile_manager.get_profile(user_id)
                            if profile:
                                sender_display = profile.display_name or profile.username
                        p2p_manager.broadcast_feed_post(
                            post_id=post.id,
                            author_id=user_id,
                            content=content,
                            post_type=post_type,
                            visibility=visibility,
                            timestamp=post.created_at.isoformat() if hasattr(post.created_at, 'isoformat') else str(post.created_at),
                            metadata=final_metadata,
                            expires_at=post.expires_at.isoformat() if getattr(post, 'expires_at', None) else None,
                            ttl_seconds=ttl_seconds,
                            ttl_mode=ttl_mode,
                            display_name=sender_display,
                        )
                    except Exception as p2p_err:
                        logger.warning(f"Failed to broadcast feed post via P2P: {p2p_err}")

                # Emit mention events for @handles (local + remote)
                try:
                    mention_manager = current_app.config.get('MENTION_MANAGER')
                    mentions = extract_mentions(content or '')
                    if mention_manager and mentions:
                        targets = resolve_mention_targets(
                            db_manager,
                            mentions,
                            visibility=visibility,
                            permissions=permissions,
                            author_id=user_id,
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
                        if isinstance(final_metadata, dict):
                            origin_peer = final_metadata.get('origin_peer')
                        if not origin_peer and p2p_manager:
                            origin_peer = p2p_manager.get_peer_id()

                        if local_targets:
                            record_mention_activity(
                                mention_manager,
                                p2p_manager,
                                target_ids=[cast(str, t.get('user_id')) for t in local_targets if t.get('user_id')],
                                source_type='feed_post',
                                source_id=post.id,
                                author_id=user_id,
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
                                author_id=user_id,
                                target_user_ids=[cast(str, t.get('user_id')) for t in remote_targets if t.get('user_id')],
                                preview=preview,
                                channel_id=None,
                                origin_peer=origin_peer,
                            )
                except Exception as mention_err:
                    logger.warning(f"Feed mention processing failed: {mention_err}")

                return jsonify({
                    'success': True,
                    'post': post.to_dict() if hasattr(post, 'to_dict') else {
                        'id': post.id,
                        'content': post.content,
                        'post_type': post.post_type.value,
                        'visibility': post.visibility.value,
                        'created_at': post.created_at.isoformat(),
                        'expires_at': post.expires_at.isoformat() if getattr(post, 'expires_at', None) else None,
                        'metadata': post.metadata
                    }
                })
            else:
                return jsonify({'error': 'Failed to create post'}), 500
                
        except Exception as e:
            logger.error(f"Create post error: {e}")
            return jsonify({'error': 'Internal server error'}), 500
    
    @ui.route('/ajax/update_post', methods=['POST'])
    @require_login
    def ajax_update_post():
        """AJAX endpoint to update a post."""
        try:
            db_manager, _, _, _, _, file_manager, feed_manager, interaction_manager, profile_manager, _, p2p_manager = _get_app_components_any(current_app)
            user_id = get_current_user()
            from ..core.polls import parse_poll, poll_edit_lock_reason
            
            data = request.get_json()
            post_id = data.get('post_id')
            content = data.get('content', '').strip()
            post_type = data.get('post_type')
            visibility = data.get('visibility')
            permissions = data.get('permissions')
            metadata = data.get('metadata', {})
            new_attachments = data.get('new_attachments', [])
            
            if not post_id:
                return jsonify({'error': 'Post ID required'}), 400
                
            if not content and not metadata.get('attachments') and not new_attachments:
                return jsonify({'error': 'Post content or attachments required'}), 400

            existing_post = feed_manager.get_post(post_id) if feed_manager else None
            if not existing_post:
                return jsonify({'error': 'Post not found'}), 404

            # Guard against editing polls after votes exist
            existing_poll = parse_poll(existing_post.content or '') if existing_post else None
            new_poll = parse_poll(content or '') if content is not None else None
            poll_spec = existing_poll or new_poll
            if poll_spec:
                votes_total = 0
                if interaction_manager:
                    results = interaction_manager.get_poll_results(post_id, 'feed', len(poll_spec.options))
                    votes_total = results.get('total', 0)
                lock_reason = poll_edit_lock_reason(existing_post.created_at, votes_total, now=datetime.now(timezone.utc))
                if lock_reason:
                    return jsonify({'error': lock_reason}), 400
            
            # Process new file attachments if any
            processed_new_attachments = []
            for attachment in new_attachments:
                try:
                    # Attachment should contain file data as base64
                    file_data = base64.b64decode(attachment['data'])
                    file_info = file_manager.save_file(
                        file_data, 
                        attachment['name'], 
                        attachment['type'], 
                        user_id
                    )
                    
                    if file_info:
                        processed_new_attachments.append({
                            'id': file_info.id,
                            'name': file_info.original_name,
                            'type': file_info.content_type,
                            'size': file_info.size,
                            'url': file_info.url
                        })
                except Exception as e:
                    logger.error(f"Failed to process new attachment {attachment.get('name', 'unknown')}: {e}")
                    continue
            
            # Combine existing metadata with new data so we don't drop origin fields
            base_metadata = existing_post.metadata or {}
            final_metadata = dict(base_metadata)
            if metadata:
                final_metadata.update(metadata)
            existing_attachments = final_metadata.get('attachments', [])
            all_attachments = existing_attachments + processed_new_attachments
            
            if all_attachments:
                final_metadata['attachments'] = all_attachments
                
                # Update primary media URLs for display
                first_attachment = all_attachments[0]
                if first_attachment['type'].startswith('image/'):
                    final_metadata['image_url'] = first_attachment['url']
                elif first_attachment['type'].startswith('video/'):
                    final_metadata['video_url'] = first_attachment['url']
                elif first_attachment['type'].startswith('audio/'):
                    final_metadata['audio_url'] = first_attachment['url']
            
            # Convert string types to enums if provided
            from ..core.feed import PostType, PostVisibility
            post_type_enum = None
            visibility_enum = None
            
            if post_type:
                try:
                    post_type_enum = PostType(post_type)
                except ValueError:
                    return jsonify({'error': f'Invalid post type: {post_type}'}), 400

            # Auto-detect polls from content
            if parse_poll(content):
                post_type_enum = PostType.POLL
            
            if visibility:
                try:
                    visibility_enum = PostVisibility(visibility)
                except ValueError:
                    return jsonify({'error': f'Invalid visibility: {visibility}'}), 400
            
            # Mark edit timestamp in metadata
            try:
                final_metadata['edited_at'] = datetime.now(timezone.utc).isoformat()
            except Exception:
                pass

            success = feed_manager.update_post(
                post_id, user_id, content,
                post_type=post_type_enum,
                visibility=visibility_enum,
                metadata=final_metadata,
                permissions=permissions
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
                        author_id=user_id,
                        origin_peer=p2p_manager.get_peer_id() if p2p_manager else None,
                        channel_id=None,
                        visibility=visibility_enum.value if visibility_enum else existing_post.visibility.value,
                        permissions=permissions if permissions is not None else existing_post.permissions,
                        edited_at=final_metadata.get('edited_at') if isinstance(final_metadata, dict) else None,
                    )
                except Exception as mention_sync_err:
                    logger.warning(f"Feed mention refresh failed on post update: {mention_sync_err}")

                # Sync inline circles from edited content (create/update circles)
                try:
                    circle_manager = current_app.config.get('CIRCLE_MANAGER')
                    if circle_manager:
                        from ..core.circles import parse_circle_blocks, derive_circle_id
                        effective_visibility = visibility_enum.value if visibility_enum else existing_post.visibility.value
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
                                        permissions=permissions if permissions is not None else existing_post.permissions,
                                        author_id=user_id,
                                    )
                                if not facilitator_id:
                                    facilitator_id = user_id
                                if spec.participants is not None:
                                    resolved_participants = _resolve_handle_list(
                                        db_manager,
                                        spec.participants,
                                        visibility=effective_visibility,
                                        permissions=permissions if permissions is not None else existing_post.permissions,
                                        author_id=user_id,
                                    )
                                    spec.participants = resolved_participants

                                circle_manager.upsert_circle(
                                    circle_id=circle_id,
                                    source_type='feed',
                                    source_id=post_id,
                                    created_by=user_id,
                                    spec=spec,
                                    facilitator_id=facilitator_id,
                                    visibility=effective_visibility,
                                    origin_peer=p2p_manager.get_peer_id() if p2p_manager else None,
                                    created_at=existing_post.created_at.isoformat() if getattr(existing_post, 'created_at', None) else None,
                                )
                except Exception as circle_err:
                    logger.warning(f"Inline circle sync failed on post update: {circle_err}")

                # Sync inline tasks from edited content (create/update tasks)
                try:
                    task_manager = current_app.config.get('TASK_MANAGER')
                    if task_manager:
                        effective_visibility = visibility_enum.value if visibility_enum else existing_post.visibility.value
                        effective_permissions = permissions if permissions is not None else existing_post.permissions
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
                            actor_id=user_id,
                            task_visibility=task_visibility,
                            base_metadata=base_meta,
                            visibility=effective_visibility,
                            permissions=effective_permissions,
                            p2p_manager=p2p_manager,
                            profile_manager=profile_manager,
                        )
                except Exception as task_err:
                    logger.warning(f"Inline task sync failed on post update: {task_err}")

                # Sync inline objectives from edited content (create/update objectives)
                try:
                    objective_manager = current_app.config.get('OBJECTIVE_MANAGER')
                    if objective_manager:
                        effective_visibility = visibility_enum.value if visibility_enum else existing_post.visibility.value
                        effective_permissions = permissions if permissions is not None else existing_post.permissions
                        objective_visibility = 'network' if effective_visibility in ('public', 'network') else 'local'
                        origin_peer = getattr(existing_post, 'origin_peer', None)
                        if not origin_peer and p2p_manager:
                            try:
                                origin_peer = p2p_manager.get_peer_id()
                            except Exception:
                                origin_peer = None
                        _sync_inline_objectives_from_content(
                            objective_manager=objective_manager,
                            db_manager=db_manager,
                            content=content,
                            scope='feed',
                            source_id=post_id,
                            actor_id=user_id,
                            objective_visibility=objective_visibility,
                            source_type='feed_post',
                            origin_peer=origin_peer,
                            created_at=existing_post.created_at.isoformat()
                            if getattr(existing_post, 'created_at', None) else None,
                            visibility=effective_visibility,
                            permissions=effective_permissions,
                        )
                except Exception as obj_err:
                    logger.warning(f"Inline objective sync failed on post update: {obj_err}")

                # Sync inline requests from edited content (create/update requests)
                try:
                    request_manager = current_app.config.get('REQUEST_MANAGER')
                    if request_manager:
                        effective_visibility = visibility_enum.value if visibility_enum else existing_post.visibility.value
                        effective_permissions = permissions if permissions is not None else existing_post.permissions
                        request_visibility = 'network' if effective_visibility in ('public', 'network') else 'local'
                        origin_peer = getattr(existing_post, 'origin_peer', None)
                        if not origin_peer and p2p_manager:
                            try:
                                origin_peer = p2p_manager.get_peer_id()
                            except Exception:
                                origin_peer = None
                        _sync_inline_requests_from_content(
                            request_manager=request_manager,
                            db_manager=db_manager,
                            content=content,
                            scope='feed',
                            source_id=post_id,
                            actor_id=user_id,
                            visibility=request_visibility,
                            source_type='feed_post',
                            origin_peer=origin_peer,
                            created_at=existing_post.created_at.isoformat()
                            if getattr(existing_post, 'created_at', None) else None,
                            permissions=effective_permissions,
                            channel_id=None,
                        )
                except Exception as req_err:
                    logger.warning(f"Inline request sync failed on post update: {req_err}")

                # Sync inline contracts from edited feed content
                try:
                    contract_manager = current_app.config.get('CONTRACT_MANAGER')
                    if contract_manager:
                        effective_visibility = visibility_enum.value if visibility_enum else existing_post.visibility.value
                        effective_permissions = permissions if permissions is not None else existing_post.permissions
                        contract_visibility = 'network' if effective_visibility in ('public', 'network') else 'local'
                        origin_peer = getattr(existing_post, 'origin_peer', None)
                        if not origin_peer and p2p_manager:
                            try:
                                origin_peer = p2p_manager.get_peer_id()
                            except Exception:
                                origin_peer = None
                        _sync_inline_contracts_from_content(
                            contract_manager=contract_manager,
                            db_manager=db_manager,
                            content=content,
                            scope='feed',
                            source_id=post_id,
                            actor_id=user_id,
                            contract_visibility=contract_visibility,
                            source_type='feed_post',
                            origin_peer=origin_peer,
                            created_at=existing_post.created_at.isoformat()
                            if getattr(existing_post, 'created_at', None) else None,
                            visibility=effective_visibility,
                            permissions=effective_permissions,
                        )
                except Exception as contract_err:
                    logger.warning(f"Inline contract sync failed on post update: {contract_err}")

                # Broadcast edited post to P2P peers so they update locally
                if p2p_manager and p2p_manager.is_running():
                    try:
                        updated = feed_manager.get_post(post_id)
                        if updated:
                            sender_display = None
                            if profile_manager:
                                profile = profile_manager.get_profile(user_id)
                                if profile:
                                    sender_display = profile.display_name or profile.username
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
            else:
                return jsonify({'error': 'Failed to update post or not authorized'}), 403
                
        except Exception as e:
            logger.error(f"Update post error: {e}")
            return jsonify({'error': 'Internal server error'}), 500

    @ui.route('/ajax/get_post_data/<post_id>', methods=['GET'])
    @require_login
    def ajax_get_post_data(post_id):
        """AJAX endpoint to get post data for editing."""
        try:
            _, _, _, _, _, _, feed_manager, _, _, _, _ = _get_app_components_any(current_app)
            user_id = get_current_user()
            
            post = feed_manager.get_post(post_id)
            
            if not post:
                return jsonify({'error': 'Post not found'}), 404
            
            # Check if user can edit (must be author)
            if post.author_id != user_id:
                return jsonify({'error': 'Not authorized to edit this post'}), 403
            
            return jsonify({
                'success': True,
                'post': {
                    'id': post.id,
                    'content': post.content,
                    'post_type': post.post_type.value,
                    'visibility': post.visibility.value,
                    'metadata': post.metadata,
                    'permissions': post.permissions
                }
            })
                
        except Exception as e:
            logger.error(f"Get post data error: {e}")
            return jsonify({'error': 'Internal server error'}), 500

    @ui.route('/ajax/toggle_post_like', methods=['POST'])
    @require_login
    def ajax_toggle_post_like():
        """AJAX endpoint to toggle like on a post."""
        try:
            _, _, _, _, _, _, _, interaction_manager, profile_manager, _, p2p_manager = _get_app_components_any(current_app)
            user_id = get_current_user()
            
            data = request.get_json()
            post_id = data.get('post_id')
            
            if not post_id:
                return jsonify({'error': 'Post ID required'}), 400
            
            from ..core.interactions import InteractionType
            liked = interaction_manager.toggle_post_like(post_id, user_id, InteractionType.LIKE)
            interactions = interaction_manager.get_post_interactions(post_id)
            
            # Broadcast interaction to P2P peers
            if p2p_manager and p2p_manager.is_running():
                try:
                    sender_display = None
                    if profile_manager:
                        profile = profile_manager.get_profile(user_id)
                        if profile:
                            sender_display = profile.display_name or profile.username
                    p2p_manager.broadcast_interaction(
                        item_id=post_id,
                        user_id=user_id,
                        action='like' if liked else 'unlike',
                        item_type='post',
                        display_name=sender_display,
                    )
                except Exception as p2p_err:
                    logger.warning(f"Failed to broadcast post like via P2P: {p2p_err}")

            return jsonify({
                'success': True,
                'liked': liked,
                'interactions': interactions
            })
                
        except Exception as e:
            logger.error(f"Toggle post like error: {e}")
            return jsonify({'error': 'Internal server error'}), 500

    @ui.route('/ajax/vote_poll', methods=['POST'])
    @require_login
    def ajax_vote_poll():
        """AJAX endpoint to vote in a poll (feed or channel)."""
        try:
            db_manager, _, _, _, channel_manager, _, feed_manager, interaction_manager, profile_manager, _, p2p_manager = _get_app_components_any(current_app)
            user_id = get_current_user()
            from ..core.polls import parse_poll, resolve_poll_end, describe_poll_status

            data = request.get_json() or {}
            poll_id = data.get('poll_id')
            item_type = (data.get('item_type') or '').strip().lower()
            option_index = data.get('option_index')

            if not poll_id or item_type not in {'feed', 'channel'}:
                return jsonify({'error': 'Poll ID and item_type required'}), 400
            if option_index is None:
                return jsonify({'error': 'Option index required'}), 400

            now_dt = datetime.now(timezone.utc)
            poll_spec = None
            poll_end = None
            author_id = None
            channel_id = None
            item_expires_at = None

            if item_type == 'feed':
                post = feed_manager.get_post(poll_id) if feed_manager else None
                if not post:
                    return jsonify({'error': 'Poll post not found'}), 404
                # Basic visibility checks (align with feed)
                if post.visibility.value == 'private' and post.author_id != user_id:
                    return jsonify({'error': 'Access denied'}), 403
                if post.visibility.value == 'custom' and user_id not in (post.permissions or []):
                    return jsonify({'error': 'Access denied'}), 403
                poll_spec = parse_poll(post.content or '')
                author_id = post.author_id
                item_expires_at = post.expires_at
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
                    # Ensure current user is a member of the channel
                    member = conn.execute(
                        "SELECT 1 FROM channel_members WHERE channel_id = ? AND user_id = ?",
                        (channel_id, user_id)
                    ).fetchone()
                    if not member:
                        return jsonify({'error': 'Access denied'}), 403
                    poll_spec = parse_poll(row['content'] or '')
                    author_id = row['user_id']
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
            if option_index is None or int(option_index) < 0 or int(option_index) >= len(poll_spec.options):
                return jsonify({'error': 'Invalid poll option'}), 400
            if poll_end and poll_end <= now_dt:
                return jsonify({'error': 'Poll is closed'}), 400

            result = interaction_manager.record_poll_vote(poll_id, item_type, user_id, int(option_index))
            results = interaction_manager.get_poll_results(poll_id, item_type, len(poll_spec.options))
            user_vote = interaction_manager.get_user_poll_vote(poll_id, item_type, user_id)
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

            # Broadcast vote to peers
            if p2p_manager and p2p_manager.is_running():
                try:
                    sender_display = None
                    if profile_manager:
                        profile = profile_manager.get_profile(user_id)
                        if profile:
                            sender_display = profile.display_name or profile.username
                    p2p_manager.broadcast_interaction(
                        item_id=poll_id,
                        user_id=user_id,
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
                },
                'changed': result.get('changed', False)
            })
        except Exception as e:
            logger.error(f"Poll vote error: {e}", exc_info=True)
            return jsonify({'error': 'Internal server error'}), 500

    @ui.route('/ajax/add_post_comment', methods=['POST'])
    @require_login
    def ajax_add_post_comment():
        """AJAX endpoint to add a comment to a post."""
        try:
            _, api_key_manager, _, _, _, file_manager, feed_manager, interaction_manager, _, _, _ = _get_app_components_any(current_app)
            user_id = get_current_user()
            
            # Check API key permissions for writing comments
            api_key = request.headers.get('X-API-Key') or request.args.get('api_key')
            if api_key:
                from ..security.api_keys import Permission
                key_info = api_key_manager.validate_key(api_key, Permission.WRITE_MESSAGES)
                if not key_info:
                    return jsonify({'error': 'Invalid API key or insufficient permissions'}), 403
                user_id = key_info.user_id
            
            data = request.get_json()
            post_id = data.get('post_id')
            content = data.get('content', '').strip()
            parent_comment_id = data.get('parent_comment_id')
            attachments = data.get('attachments', [])
            
            if not post_id:
                return jsonify({'error': 'Post ID required'}), 400

            # Ensure post exists and not expired
            if feed_manager:
                post = feed_manager.get_post(post_id)
                if not post:
                    return jsonify({'error': 'Post not found or expired'}), 404
                
            if not content and not attachments:
                return jsonify({'error': 'Comment content or attachments required'}), 400
            
            # Process file attachments if any
            processed_attachments = []
            for attachment in attachments:
                try:
                    # Attachment should contain file data as base64
                    file_data = base64.b64decode(attachment['data'])
                    file_info = file_manager.save_file(
                        file_data, 
                        attachment['name'], 
                        attachment['type'], 
                        user_id
                    )
                    
                    if file_info:
                        processed_attachments.append({
                            'id': file_info.id,
                            'name': file_info.original_name,
                            'type': file_info.content_type,
                            'size': file_info.size,
                            'url': file_info.url
                        })
                except Exception as e:
                    logger.error(f"Failed to process comment attachment {attachment.get('name', 'unknown')}: {e}")
                    continue
            
            # Add attachment URLs to content if there are images
            if processed_attachments:
                attachment_text = []
                for attachment in processed_attachments:
                    if attachment['type'].startswith('image/'):
                        attachment_text.append(f"[Image: {attachment['name']}]({attachment['url']})")
                    else:
                        attachment_text.append(f"[File: {attachment['name']}]({attachment['url']})")
                
                if content:
                    content = content + "\n\n" + "\n".join(attachment_text)
                else:
                    content = "\n".join(attachment_text)
            
            comment = interaction_manager.add_post_comment(post_id, user_id, content, parent_comment_id)
            
            if comment:
                interactions = interaction_manager.get_post_interactions(post_id)
                return jsonify({
                    'success': True,
                    'comment': comment.to_dict(),
                    'interactions': interactions
                })
            else:
                return jsonify({'error': 'Failed to add comment'}), 500
                
        except Exception as e:
            logger.error(f"Add post comment error: {e}")
            return jsonify({'error': 'Internal server error'}), 500

    @ui.route('/ajax/get_post_comments/<post_id>', methods=['GET'])
    @require_login
    def ajax_get_post_comments(post_id):
        """AJAX endpoint to get comments for a post."""
        try:
            _, _, _, _, _, _, _, interaction_manager, _, _, _ = _get_app_components_any(current_app)
            
            comments = interaction_manager.get_post_comments(post_id)
            interactions = interaction_manager.get_post_interactions(post_id)
            
            return jsonify({
                'success': True,
                'comments': [comment.to_dict() for comment in comments],
                'interactions': interactions
            })
                
        except Exception as e:
            logger.error(f"Get post comments error: {e}")
            return jsonify({'error': 'Internal server error'}), 500

    @ui.route('/ajax/delete_post', methods=['POST'])
    @require_login
    def ajax_delete_post():
        """AJAX endpoint to delete a post."""
        try:
            _, _, _, _, _, _, feed_manager, _, _, _, p2p_manager = _get_app_components_any(current_app)
            user_id = get_current_user()
            
            data = request.get_json()
            post_id = data.get('post_id')
            
            if not post_id:
                return jsonify({'error': 'Post ID required'}), 400
            
            success = feed_manager.delete_post(post_id, user_id, allow_admin=_is_admin())
            
            if success:
                # Broadcast delete signal via P2P
                if p2p_manager and p2p_manager.is_running():
                    try:
                        import secrets as _sec
                        signal_id = f"DS{_sec.token_hex(8)}"
                        p2p_manager.broadcast_delete_signal(
                            signal_id=signal_id,
                            data_type='feed_post',
                            data_id=post_id,
                            reason='user_deleted',
                        )
                    except Exception as p2p_err:
                        logger.warning(f"Failed to broadcast post delete via P2P: {p2p_err}")
                return jsonify({'success': True})
            else:
                return jsonify({'error': 'Failed to delete post or not authorized'}), 403
                
        except Exception as e:
            logger.error(f"Delete post error: {e}")
            return jsonify({'error': 'Internal server error'}), 500

    @ui.route('/ajax/update_post_expiry', methods=['POST'])
    @require_login
    def ajax_update_post_expiry():
        """AJAX endpoint to update a post's expiry."""
        try:
            _, _, _, _, _, _, feed_manager, _, profile_manager, _, p2p_manager = _get_app_components_any(current_app)
            user_id = get_current_user()

            data = request.get_json() or {}
            post_id = data.get('post_id')
            ttl_mode = data.get('ttl_mode')
            ttl_seconds = data.get('ttl_seconds')
            expires_at = data.get('expires_at')

            if not post_id:
                return jsonify({'error': 'Post ID required'}), 400

            expires_dt = feed_manager.update_post_expiry(
                post_id=post_id,
                user_id=user_id,
                expires_at=expires_at,
                ttl_seconds=ttl_seconds,
                ttl_mode=ttl_mode,
                allow_admin=False,
            )
            if expires_dt is None and ttl_mode not in ('none', 'no_expiry', 'immortal'):
                return jsonify({'error': 'Not authorized or invalid expiry'}), 403

            # Broadcast updated expiry to peers (best-effort)
            if p2p_manager and p2p_manager.is_running():
                try:
                    post = feed_manager.get_post(post_id)
                    if post:
                        display_name = None
                        try:
                            profile = profile_manager.get_profile(user_id)
                            if profile:
                                display_name = profile.display_name or profile.username
                        except Exception:
                            pass
                        p2p_manager.broadcast_feed_post(
                            post_id=post.id,
                            author_id=post.author_id,
                            content=post.content,
                            post_type=post.post_type.value,
                            visibility=post.visibility.value,
                            timestamp=post.created_at.isoformat(),
                            metadata=post.metadata or {},
                            expires_at=expires_dt.isoformat() if expires_dt else None,
                            ttl_seconds=ttl_seconds,
                            ttl_mode=ttl_mode,
                            display_name=display_name,
                        )
                except Exception as bcast_err:
                    logger.warning(f"Failed to broadcast post expiry update: {bcast_err}")

            return jsonify({
                'success': True,
                'expires_at': expires_dt.isoformat() if expires_dt else None,
            })
        except Exception as e:
            logger.error(f"Update post expiry error: {e}")
            return jsonify({'error': 'Internal server error'}), 500
    
    @ui.route('/ajax/share_post', methods=['POST'])
    @require_login
    def ajax_share_post():
        """AJAX endpoint to share (repost) a feed post."""
        try:
            _, _, _, _, _, _, feed_manager, _, _, _, _ = _get_app_components_any(current_app)
            user_id = get_current_user()

            data = request.get_json() or {}
            post_id = data.get('post_id')
            comment = data.get('comment', '')

            if not post_id:
                return jsonify({'error': 'Post ID required'}), 400

            shared = feed_manager.share_post(post_id, user_id, comment)
            if shared:
                return jsonify({'success': True, 'post': shared.to_dict()})
            return jsonify({'error': 'Failed to share post'}), 500
        except Exception as e:
            logger.error(f"Share post error: {e}")
            return jsonify({'error': 'Internal server error'}), 500

    # ------------------------------------------------------------------
    #  Feed algorithm preferences
    # ------------------------------------------------------------------

    @ui.route('/ajax/feed_algorithm', methods=['GET'])
    @require_login
    def ajax_get_feed_algorithm():
        """Get the current user's feed algorithm preferences."""
        try:
            _, _, _, _, _, _, feed_manager, _, _, _, _ = _get_app_components_any(current_app)
            user_id = get_current_user()
            algo = feed_manager.get_feed_algorithm(user_id)
            return jsonify({'success': True, 'algorithm': algo.to_dict()})
        except Exception as e:
            logger.error(f"Get feed algorithm error: {e}")
            return jsonify({'error': 'Internal server error'}), 500

    @ui.route('/ajax/feed_algorithm', methods=['POST'])
    @require_login
    def ajax_save_feed_algorithm():
        """Save the current user's feed algorithm preferences."""
        try:
            _, _, _, _, _, _, feed_manager, _, _, _, _ = _get_app_components_any(current_app)
            user_id = get_current_user()
            data = request.get_json() or {}

            from ..core.feed import FeedAlgorithm
            algo = FeedAlgorithm.from_dict(data)
            success = feed_manager.save_feed_algorithm(user_id, algo)
            if success:
                return jsonify({'success': True})
            return jsonify({'error': 'Failed to save preferences'}), 500
        except Exception as e:
            logger.error(f"Save feed algorithm error: {e}")
            return jsonify({'error': 'Internal server error'}), 500

    @ui.route('/ajax/feed_tags', methods=['GET'])
    @require_login
    def ajax_get_feed_tags():
        """Get popular tags across all posts."""
        try:
            _, _, _, _, _, _, feed_manager, _, _, _, _ = _get_app_components_any(current_app)
            tags = feed_manager.get_available_tags()
            return jsonify({'success': True, 'tags': tags})
        except Exception as e:
            logger.error(f"Get feed tags error: {e}")
            return jsonify({'error': 'Internal server error'}), 500

    @ui.route('/ajax/delete_message', methods=['POST'])
    @require_login
    def ajax_delete_message():
        """AJAX endpoint to delete a direct message."""
        try:
            _, _, _, message_manager, _, file_manager, _, _, _, _, p2p_manager = _get_app_components_any(current_app)
            user_id = get_current_user()

            data = request.get_json() or {}
            message_id = data.get('message_id')

            if not message_id:
                return jsonify({'error': 'Message ID required'}), 400

            success = message_manager.delete_message(message_id, user_id, file_manager=file_manager)
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
                # Broadcast delete signal via P2P
                if p2p_manager and p2p_manager.is_running():
                    try:
                        import secrets as _sec
                        signal_id = f"DS{_sec.token_hex(8)}"
                        p2p_manager.broadcast_delete_signal(
                            signal_id=signal_id,
                            data_type='direct_message',
                            data_id=message_id,
                            reason='user_deleted',
                        )
                    except Exception as p2p_err:
                        logger.warning(f"Failed to broadcast DM delete via P2P: {p2p_err}")
                return jsonify({'success': True})
            return jsonify({'error': 'Message not found or not owned by you'}), 403
        except Exception as e:
            logger.error(f"Delete message error: {e}")
            return jsonify({'error': 'Internal server error'}), 500

    @ui.route('/ajax/get_message_data/<message_id>', methods=['GET'])
    @require_login
    def ajax_get_message_data(message_id):
        """AJAX endpoint to get message data for editing."""
        try:
            _, _, _, message_manager, _, _, _, _, _, _, _ = _get_app_components_any(current_app)
            user_id = get_current_user()

            msg = message_manager.get_message(message_id)
            if not msg:
                return jsonify({'error': 'Message not found'}), 404

            if msg.sender_id != user_id:
                return jsonify({'error': 'Not authorized to edit this message'}), 403

            return jsonify({
                'success': True,
                'message': {
                    'id': msg.id,
                    'content': msg.content,
                    'message_type': msg.message_type.value,
                    'recipient_id': msg.recipient_id,
                    'metadata': msg.metadata,
                    'edited_at': msg.edited_at.isoformat() if msg.edited_at else None,
                }
            })
        except Exception as e:
            logger.error(f"Get message data error: {e}")
            return jsonify({'error': 'Internal server error'}), 500

    @ui.route('/ajax/update_message', methods=['POST'])
    @require_login
    def ajax_update_message():
        """AJAX endpoint to update a direct message (own messages only)."""
        try:
            db_manager, _, _, message_manager, _, file_manager, _, _, profile_manager, _, p2p_manager = _get_app_components_any(current_app)
            user_id = get_current_user()

            data = request.get_json() or {}
            message_id = data.get('message_id')
            content = (data.get('content') or '').strip()
            attachments = data.get('attachments')
            new_attachments = data.get('new_attachments') or []

            if not message_id:
                return jsonify({'error': 'Message ID required'}), 400
            if not content and not attachments and not new_attachments:
                return jsonify({'error': 'Message content or attachments required'}), 400

            with db_manager.get_connection() as conn:
                row = conn.execute(
                    "SELECT sender_id, recipient_id, metadata, created_at, message_type "
                    "FROM messages WHERE id = ?",
                    (message_id,)
                ).fetchone()
            if not row:
                return jsonify({'error': 'Message not found'}), 404
            if row['sender_id'] != user_id:
                return jsonify({'error': 'You can only edit your own messages'}), 403

            # Process new file attachments
            processed_new_attachments = []
            for attachment in new_attachments:
                try:
                    file_data = base64.b64decode(attachment['data'])
                    file_info = file_manager.save_file(
                        file_data,
                        attachment['name'],
                        attachment['type'],
                        user_id
                    )
                    if file_info:
                        processed_new_attachments.append({
                            'id': file_info.id,
                            'name': file_info.original_name,
                            'type': file_info.content_type,
                            'size': file_info.size,
                            'url': file_info.url
                        })
                except Exception as e:
                    logger.error(f"Failed to process new attachment {attachment.get('name', 'unknown')}: {e}")
                    continue

            existing_meta = {}
            if row['metadata']:
                try:
                    existing_meta = json.loads(row['metadata'])
                except Exception:
                    existing_meta = {}

            # Merge existing attachments (from request or DB) with new
            if attachments is None:
                final_attachments = existing_meta.get('attachments') or []
            else:
                final_attachments = list(attachments) if isinstance(attachments, list) else []
            final_attachments.extend(processed_new_attachments)

            final_metadata = dict(existing_meta)
            if final_attachments:
                final_metadata['attachments'] = final_attachments
            else:
                final_metadata.pop('attachments', None)

            try:
                final_metadata['edited_at'] = datetime.now(timezone.utc).isoformat()
            except Exception:
                pass

            from ..core.messaging import MessageType
            msg_type = MessageType.FILE if final_attachments else MessageType.TEXT

            success = message_manager.update_message(
                message_id=message_id,
                user_id=user_id,
                content=content,
                message_type=msg_type,
                metadata=final_metadata if final_metadata else None,
                allow_admin=False,
            )
            if not success:
                return jsonify({'error': 'Failed to update message'}), 500

            if row['recipient_id'] and p2p_manager and p2p_manager.is_running():
                try:
                    display_name = None
                    if profile_manager:
                        profile = profile_manager.get_profile(user_id)
                        if profile:
                            display_name = profile.display_name or profile.username

                    group_members: list[str] = []
                    if isinstance(final_metadata, dict):
                        group_members = final_metadata.get('group_members') or []

                    if group_members:
                        for rid in group_members:
                            if rid == user_id:
                                continue
                            p2p_manager.broadcast_direct_message(
                                sender_id=user_id,
                                recipient_id=rid,
                                content=content,
                                message_id=message_id,
                                timestamp=str(row['created_at']),
                                display_name=display_name,
                                metadata=final_metadata if final_metadata else None,
                                update_only=True,
                                edited_at=final_metadata.get('edited_at') if final_metadata else None,
                            )
                    else:
                        p2p_manager.broadcast_direct_message(
                            sender_id=user_id,
                            recipient_id=row['recipient_id'],
                            content=content,
                            message_id=message_id,
                            timestamp=str(row['created_at']),
                            display_name=display_name,
                            metadata=final_metadata if final_metadata else None,
                            update_only=True,
                            edited_at=final_metadata.get('edited_at') if final_metadata else None,
                        )
                except Exception as bcast_err:
                    logger.warning(f"Failed to broadcast DM update via P2P: {bcast_err}")

            try:
                inbox_manager = current_app.config.get('INBOX_MANAGER')
                if inbox_manager:
                    group_members = []
                    if isinstance(final_metadata, dict):
                        group_members = [
                            str(member_id).strip()
                            for member_id in (final_metadata.get('group_members') or [])
                            if str(member_id).strip() and str(member_id).strip() != user_id
                        ]
                    target_ids = group_members or ([str(row['recipient_id']).strip()] if row['recipient_id'] else [])
                    local_target_ids = filter_local_dm_targets(db_manager, p2p_manager, target_ids)
                    if local_target_ids:
                        payload = {
                            'content': content,
                            'message_id': message_id,
                            'edited_at': final_metadata.get('edited_at') if isinstance(final_metadata, dict) else None,
                            'attachments': final_attachments or [],
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
                            sender_user_id=user_id,
                            preview=build_dm_preview(content, final_attachments or []),
                            payload=payload,
                            message_id=message_id,
                            source_content=content,
                        )
            except Exception as inbox_err:
                logger.warning(f"Failed to refresh DM inbox trigger on edit: {inbox_err}")

            return jsonify({'success': True})
        except Exception as e:
            logger.error(f"Update message error: {e}", exc_info=True)
            return jsonify({'error': 'Internal server error'}), 500

    @ui.route('/ajax/reply_message', methods=['POST'])
    @require_login
    def ajax_reply_message():
        """AJAX endpoint to reply to a direct message."""
        try:
            db_manager, _, _, message_manager, _, _, _, _, profile_manager, _, p2p_manager = _get_app_components_any(current_app)
            user_id = get_current_user()

            data = request.get_json() or {}
            original_message_id = data.get('message_id')
            content = data.get('content', '').strip()

            if not original_message_id or not content:
                return jsonify({'error': 'Message ID and content required'}), 400

            # Get the original message to find the sender
            original = message_manager.get_message(original_message_id)
            if not original:
                return jsonify({'error': 'Original message not found'}), 404

            meta = original.metadata or {}
            group_members = meta.get('group_members') if isinstance(meta, dict) else None
            group_id = meta.get('group_id') if isinstance(meta, dict) else None

            if group_members:
                if not group_id:
                    group_id = _compute_group_id(group_members)
                recipients = [member_id for member_id in group_members if member_id and member_id != user_id]
                if not recipients:
                    return jsonify({'error': 'No other group members to reply to'}), 400

                reply_meta = {
                    'reply_to': original_message_id,
                    'group_id': group_id,
                    'group_members': group_members,
                    'is_group': True,
                }
                message = message_manager.create_message(
                    sender_id=user_id,
                    recipient_id=group_id,
                    content=content,
                    metadata=reply_meta,
                )
                if message:
                    message_manager.send_message(message)

                    try:
                        inbox_manager = current_app.config.get('INBOX_MANAGER')
                        if inbox_manager:
                            local_target_ids = filter_local_dm_targets(db_manager, p2p_manager, recipients)
                            if local_target_ids:
                                inbox_manager.sync_source_triggers(
                                    source_type='dm',
                                    source_id=message.id,
                                    trigger_type='dm',
                                    target_ids=local_target_ids,
                                    sender_user_id=user_id,
                                    preview=build_dm_preview(content, []),
                                    payload={
                                        'content': content,
                                        'message_id': message.id,
                                        'reply_to': original_message_id,
                                        'group_id': group_id,
                                        'group_members': group_members,
                                        'is_group': True,
                                    },
                                    message_id=message.id,
                                    source_content=content,
                                )
                    except Exception as inbox_err:
                        logger.warning(f"Failed to create group DM reply inbox trigger: {inbox_err}")

                    if p2p_manager:
                        try:
                            display_name = None
                            if profile_manager:
                                try:
                                    profile = profile_manager.get_profile(user_id)
                                    if profile:
                                        display_name = profile.display_name or profile.username
                                except Exception:
                                    pass

                            for rid in recipients:
                                p2p_manager.broadcast_direct_message(
                                    sender_id=user_id,
                                    recipient_id=rid,
                                    content=content,
                                    message_id=message.id,
                                    timestamp=message.created_at.strftime('%Y-%m-%d %H:%M:%S'),
                                    display_name=display_name,
                                    metadata=reply_meta,
                                )
                        except Exception as bcast_err:
                            logger.warning(f"Failed to broadcast group DM reply over P2P: {bcast_err}")

                    return jsonify({'success': True, 'message': message.to_dict()})
                return jsonify({'error': 'Failed to send reply'}), 500

            # Determine reply recipient (the other party in the conversation)
            recipient_id = original.sender_id if original.sender_id != user_id else original.recipient_id

            message = message_manager.create_message(
                sender_id=user_id,
                recipient_id=recipient_id,
                content=content,
                metadata={'reply_to': original_message_id}
            )
            if message:
                message_manager.send_message(message)

                try:
                    inbox_manager = current_app.config.get('INBOX_MANAGER')
                    if inbox_manager and recipient_id:
                        local_target_ids = filter_local_dm_targets(db_manager, p2p_manager, [recipient_id])
                        if local_target_ids:
                            inbox_manager.sync_source_triggers(
                                source_type='dm',
                                source_id=message.id,
                                trigger_type='dm',
                                target_ids=local_target_ids,
                                sender_user_id=user_id,
                                preview=build_dm_preview(content, []),
                                payload={
                                    'content': content,
                                    'message_id': message.id,
                                    'reply_to': original_message_id,
                                },
                                message_id=message.id,
                                source_content=content,
                            )
                except Exception as inbox_err:
                    logger.warning(f"Failed to create DM reply inbox trigger: {inbox_err}")

                # Broadcast reply over P2P
                if recipient_id and p2p_manager:
                    try:
                        display_name = None
                        if profile_manager:
                            try:
                                profile = profile_manager.get_profile(user_id)
                                if profile:
                                    display_name = profile.display_name or profile.username
                            except Exception:
                                pass

                        p2p_manager.broadcast_direct_message(
                            sender_id=user_id,
                            recipient_id=recipient_id,
                            content=content,
                            message_id=message.id,
                            timestamp=message.created_at.strftime('%Y-%m-%d %H:%M:%S'),
                            display_name=display_name,
                            metadata={'reply_to': original_message_id},
                        )
                    except Exception as bcast_err:
                        logger.warning(f"Failed to broadcast DM reply over P2P: {bcast_err}")

                return jsonify({'success': True, 'message': message.to_dict()})
            return jsonify({'error': 'Failed to send reply'}), 500
        except Exception as e:
            logger.error(f"Reply message error: {e}")
            return jsonify({'error': 'Internal server error'}), 500

    # Error handlers
    @ui.errorhandler(404)
    def page_not_found(e):
        return render_template('error.html', 
                             error='Page not found',
                             error_code=404), 404
    
    @ui.errorhandler(500)
    def internal_error(e):
        return render_template('error.html', 
                             error='Internal server error',
                             error_code=500), 500

    # Channel AJAX endpoints
    @ui.route('/ajax/channel_messages/<channel_id>', methods=['GET'])
    @require_login
    def ajax_get_channel_messages(channel_id):
        """AJAX endpoint to get messages from a channel."""
        try:
            db_manager, _, _, _, channel_manager, file_manager, _, interaction_manager, profile_manager, _, p2p_manager = _get_app_components_any(current_app)
            user_id = get_current_user()
            access = channel_manager.get_channel_access_decision(
                channel_id=channel_id,
                user_id=user_id,
                require_membership=True,
            )
            if not access.get('allowed'):
                if str(access.get('reason') or '').startswith('governance_'):
                    return jsonify({
                        'error': 'Channel access blocked by admin governance policy',
                        'reason': access.get('reason'),
                    }), 403
                return jsonify({'error': 'You are not a member of this channel'}), 403
            # Mark channel as read now that the user is viewing it
            channel_manager.mark_channel_read(channel_id, user_id)
            from ..core.polls import parse_poll, resolve_poll_end, describe_poll_status, summarize_poll
            from ..core.tasks import parse_task_blocks, strip_task_blocks, derive_task_id
            from ..core.circles import parse_circle_blocks, strip_circle_blocks, derive_circle_id
            from ..core.handoffs import parse_handoff_blocks, strip_handoff_blocks, derive_handoff_id
            from ..core.objectives import parse_objective_blocks, strip_objective_blocks, derive_objective_id
            from ..core.requests import parse_request_blocks, strip_request_blocks, derive_request_id
            from ..core.signals import parse_signal_blocks, strip_signal_blocks, derive_signal_id
            from ..core.contracts import parse_contract_blocks, strip_contract_blocks, derive_contract_id
            now_dt = datetime.now(timezone.utc)
            task_manager = current_app.config.get('TASK_MANAGER')
            circle_manager = current_app.config.get('CIRCLE_MANAGER')
            handoff_manager = current_app.config.get('HANDOFF_MANAGER')
            objective_manager = current_app.config.get('OBJECTIVE_MANAGER')
            request_manager = current_app.config.get('REQUEST_MANAGER')
            signal_manager = current_app.config.get('SIGNAL_MANAGER')
            contract_manager = current_app.config.get('CONTRACT_MANAGER')
            skill_manager = current_app.config.get('SKILL_MANAGER')
            admin_user_id = db_manager.get_instance_owner_user_id() if db_manager else None
            user_display_cache: dict[str, dict[str, Any]] = {}

            def _user_display(uid: str) -> Optional[dict[str, Any]]:
                if not uid:
                    return None
                if uid in user_display_cache:
                    return user_display_cache[uid]
                display = {
                    'display_name': uid,
                    'avatar_url': None,
                    'origin_peer': None,
                }
                try:
                    if profile_manager:
                        profile = profile_manager.get_profile(uid)
                        if profile:
                            display['display_name'] = profile.display_name or profile.username or uid
                            display['avatar_url'] = profile.avatar_url
                            display['origin_peer'] = getattr(profile, 'origin_peer', None)
                    elif db_manager:
                        row = db_manager.get_user(uid)
                        if row:
                            display['display_name'] = row.get('display_name') or row.get('username') or uid
                            display['origin_peer'] = row.get('origin_peer')
                except Exception:
                    pass
                user_display_cache[uid] = display
                return display

            limit = int(request.args.get('limit', 50))
            before_message_id = request.args.get('before')
            
            logger.debug(f"Get channel messages request: user_id={user_id}, channel_id={channel_id}, limit={limit}")

            # Purge expired messages locally before returning results
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
                    if msg.get('user_id') != user_id:
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
            if signal_manager:
                try:
                    signal_manager.purge_expired_signals()
                except Exception:
                    pass
            
            messages = channel_manager.get_channel_messages(
                channel_id, user_id, limit, before_message_id
            )
            
            logger.debug(f"Retrieved {len(messages)} messages for channel {channel_id}")
            
            # Batch-check which messages the current user has liked
            msg_ids = [m.id for m in messages]
            user_liked_ids = set()
            if interaction_manager:
                user_liked_ids = interaction_manager.get_user_liked_ids(msg_ids, user_id)
            
            # Build response with like data (skip any corrupt messages)
            messages_data = []
            for message in messages:
                try:
                    msg_dict = message.to_dict()
                    # Ensure attachments have url only when file exists on this instance; flag when not yet synced
                    for att in (msg_dict.get('attachments') or []):
                        if not isinstance(att, dict):
                            continue
                        if att.get('id') and not att.get('url'):
                            if file_manager and file_manager.get_file(att['id']):
                                att['url'] = f"/files/{att['id']}"
                            else:
                                att['not_on_device'] = True  # so UI can show "Not on this device yet"
                    # Add like info
                    if interaction_manager:
                        try:
                            interactions = interaction_manager.get_post_interactions(message.id)
                            msg_dict['like_count'] = interactions['total_likes']
                        except Exception:
                            msg_dict['like_count'] = 0
                    else:
                        msg_dict['like_count'] = 0
                    msg_dict['user_has_liked'] = message.id in user_liked_ids

                    poll_spec = parse_poll(message.content or '')
                    if poll_spec and interaction_manager:
                        poll_end = resolve_poll_end(message.created_at, message.expires_at, poll_spec)
                        is_closed = bool(poll_end and poll_end <= now_dt)
                        results = interaction_manager.get_poll_results(message.id, 'channel', len(poll_spec.options))
                        user_vote = interaction_manager.get_user_poll_vote(message.id, 'channel', user_id)
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
                        msg_dict['poll'] = {
                            'question': poll_spec.question,
                            'options': option_payload,
                            'ends_at': poll_end.isoformat() if poll_end else None,
                            'status_label': status_label,
                            'is_closed': is_closed,
                            'user_vote': user_vote,
                            'total_votes': total_votes,
                        }

                        # Notify peers once when a local-authored poll closes.
                        if is_closed and db_manager:
                            author = db_manager.get_user(message.user_id) if db_manager else None
                            is_local_author = author and not author.get('origin_peer')
                            if is_local_author:
                                summary = summarize_poll(poll_spec.question, poll_spec.options, results['counts'])
                                if interaction_manager.mark_poll_closed(message.id, 'channel', summary=summary):
                                    if p2p_manager and p2p_manager.is_running():
                                        try:
                                            display_name = None
                                            if profile_manager:
                                                profile = profile_manager.get_profile(message.user_id)
                                                if profile:
                                                    display_name = profile.display_name or profile.username
                                            p2p_manager.broadcast_interaction(
                                                item_id=message.id,
                                                user_id=message.user_id,
                                                action='poll_closed',
                                                item_type='poll',
                                                display_name=display_name,
                                                extra={
                                                    'poll_id': message.id,
                                                    'poll_kind': 'channel',
                                                    'channel_id': message.channel_id,
                                                    'preview': summary,
                                                    'summary': summary,
                                                }
                                            )
                                        except Exception as p2p_err:
                                            logger.warning(f"Failed to broadcast poll closure: {p2p_err}")
                    # Inline circles for channel messages (read-only — no upserts during render)
                    circles_payload = []
                    circle_specs = parse_circle_blocks(message.content or '')
                    if circle_specs and circle_manager:
                        for idx, spec in enumerate(cast(Any, circle_specs)):
                            spec = cast(Any, spec)
                            try:
                                circle_id = derive_circle_id('channel', message.id, idx, len(circle_specs), override=spec.circle_id)
                                circle_obj = circle_manager.get_circle(circle_id)
                                if circle_obj:
                                    circle_payload = circle_obj.to_dict()
                                    circle_payload['phase_label'] = (circle_payload.get('phase') or 'opinion').replace('_', ' ').title()
                                    circle_payload['entries_count'] = circle_manager.count_entries(circle_obj.id)
                                    circles_payload.append(circle_payload)
                            except Exception:
                                pass

                    # Inline objectives for channel messages (read-only — no upserts during render)
                    objectives_payload = []
                    objective_specs = parse_objective_blocks(message.content or '')
                    if objective_specs and objective_manager:
                        for idx, spec in enumerate(cast(Any, objective_specs)):
                            spec = cast(Any, spec)
                            try:
                                objective_id = derive_objective_id('channel', message.id, idx, len(objective_specs), override=spec.objective_id)
                                objective_obj = objective_manager.get_objective(objective_id)
                                if objective_obj:
                                    payload = objective_obj
                                    payload['status_label'] = (payload.get('status') or 'pending').replace('_', ' ').title()
                                    payload['progress_percent'] = payload.get('progress_percent') or 0
                                    payload['tasks_total'] = payload.get('tasks_total') or 0
                                    payload['tasks_done'] = payload.get('tasks_done') or 0
                                    deadline_dt = None
                                    if payload.get('deadline'):
                                        try:
                                            deadline_dt = datetime.fromisoformat(payload['deadline'].replace('Z', '+00:00'))
                                        except Exception:
                                            deadline_dt = None
                                    payload['deadline_label'] = _objective_deadline_label(deadline_dt, now_dt)

                                    members_display = []
                                    for member in payload.get('members', []) or []:
                                        uid = member.get('user_id')
                                        display = _user_display(uid) if uid else None
                                        members_display.append({
                                            'user_id': uid,
                                            'role': member.get('role') or 'contributor',
                                            'display_name': (display or {}).get('display_name') if display else uid,
                                            'avatar_url': (display or {}).get('avatar_url') if display else None,
                                            'origin_peer': (display or {}).get('origin_peer') if display else None,
                                        })
                                    payload['members'] = members_display

                                    tasks_display = []
                                    for task in payload.get('tasks', []) or []:
                                        status_val = (task.get('status') or 'open')
                                        assignee_id = task.get('assigned_to') or None
                                        assignee_display = _user_display(assignee_id) if assignee_id else None
                                        tasks_display.append({
                                            'id': task.get('id'),
                                            'title': task.get('title'),
                                            'status': status_val,
                                            'status_label': status_val.replace('_', ' ').title(),
                                            'assignee_id': assignee_id,
                                            'assignee_name': (assignee_display or {}).get('display_name') if assignee_display else assignee_id,
                                            'assignee_avatar_url': (assignee_display or {}).get('avatar_url') if assignee_display else None,
                                            'assignee_origin_peer': (assignee_display or {}).get('origin_peer') if assignee_display else None,
                                        })
                                    payload['tasks'] = tasks_display
                                    objectives_payload.append(payload)
                            except Exception:
                                pass

                    # Inline requests for channel messages (read-only — no upserts during render)
                    requests_payload = []
                    request_specs = parse_request_blocks(message.content or '')
                    if request_specs and request_manager:
                        for idx, spec in enumerate(cast(Any, request_specs)):
                          spec = cast(Any, spec)
                          try:
                            if not spec.confirmed:
                                continue
                            request_id = derive_request_id('channel', message.id, idx, len(request_specs), override=spec.request_id)
                            req_obj = request_manager.get_request(request_id)
                            if req_obj:
                                payload = req_obj
                                payload['status_label'] = (payload.get('status') or 'open').replace('_', ' ').title()
                                payload['priority_label'] = (payload.get('priority') or 'normal').replace('_', ' ').title()
                                due_dt = None
                                if payload.get('due_at'):
                                    try:
                                        due_dt = datetime.fromisoformat(payload['due_at'].replace('Z', '+00:00'))
                                    except Exception:
                                        due_dt = None
                                payload['due_label'] = _request_due_label(due_dt, now_dt)
                                member_ids = [m.get('user_id') for m in payload.get('members', []) if m.get('user_id')]
                                payload['can_manage'] = (
                                    user_id == payload.get('created_by')
                                    or (admin_user_id and user_id == admin_user_id)
                                    or (user_id in member_ids)
                                )

                                members_display = []
                                for member in payload.get('members', []) or []:
                                    uid = member.get('user_id')
                                    display = _user_display(uid) if uid else None
                                    members_display.append({
                                        'user_id': uid,
                                        'role': member.get('role') or 'assignee',
                                        'display_name': (display or {}).get('display_name') if display else uid,
                                        'avatar_url': (display or {}).get('avatar_url') if display else None,
                                        'origin_peer': (display or {}).get('origin_peer') if display else None,
                                    })
                                payload['members'] = members_display
                                requests_payload.append(payload)
                          except Exception as req_err:
                                logger.warning(f"Request render error for message {message.id}: {req_err}")
                                continue

                    # Inline signals for channel messages (read-only — no upserts during render)
                    signals_payload = []
                    signal_specs = parse_signal_blocks(message.content or '')
                    if signal_specs and signal_manager:
                        for idx, spec in enumerate(cast(Any, signal_specs)):
                          spec = cast(Any, spec)
                          try:
                            signal_id = derive_signal_id('channel', message.id, idx, len(signal_specs), override=spec.signal_id)
                            signal_obj = signal_manager.get_signal(signal_id)
                            if signal_obj:
                                payload = signal_obj
                                payload['status_label'] = (payload.get('status') or 'active').replace('_', ' ').title()
                                payload['type_label'] = (payload.get('type') or 'signal').replace('_', ' ').title()
                                expiry_dt = None
                                if payload.get('expires_at'):
                                    try:
                                        expiry_dt = datetime.fromisoformat(payload['expires_at'].replace('Z', '+00:00'))
                                    except Exception:
                                        expiry_dt = None
                                payload['expires_label'] = _signal_expiry_label(expiry_dt, now_dt)
                                payload['confidence_percent'] = int(round((payload.get('confidence') or 0) * 100))
                                owner_display = _user_display(payload.get('owner_id'))
                                payload['owner_name'] = (owner_display or {}).get('display_name') if owner_display else payload.get('owner_id')
                                payload['owner_avatar_url'] = (owner_display or {}).get('avatar_url') if owner_display else None
                                payload['owner_origin_peer'] = (owner_display or {}).get('origin_peer') if owner_display else None
                                payload['can_manage'] = user_id == payload.get('owner_id') or (admin_user_id and user_id == admin_user_id)
                                try:
                                    if payload.get('data') is not None:
                                        payload['data_pretty'] = json.dumps(payload.get('data'), indent=2)
                                except Exception:
                                    payload['data_pretty'] = None
                                signals_payload.append(payload)
                          except Exception as sig_err:
                                logger.warning(f"Signal render error for message {message.id}: {sig_err}")
                                continue

                    # Inline contracts for channel messages
                    contracts_payload = []
                    contract_specs = parse_contract_blocks(message.content or '')
                    if contract_specs and contract_manager:
                        for idx, spec in enumerate(contract_specs):
                            if not spec.confirmed:
                                continue
                            try:
                                contract_id = derive_contract_id('channel', message.id, idx, len(contract_specs), override=spec.contract_id)
                                contract_obj = contract_manager.get_contract(contract_id)
                                if not contract_obj:
                                    owner_id = None
                                    if spec.owner:
                                        owner_id = _resolve_handle_to_user_id(
                                            db_manager,
                                            spec.owner,
                                            channel_id=message.channel_id,
                                            author_id=message.user_id,
                                        )
                                    if not owner_id:
                                        owner_id = message.user_id
                                    counterparties = []
                                    for cp in spec.counterparties or []:
                                        cp_id = _resolve_handle_to_user_id(
                                            db_manager,
                                            cp,
                                            channel_id=message.channel_id,
                                            author_id=message.user_id,
                                        )
                                        if cp_id:
                                            counterparties.append(cp_id)
                                    contract_visibility = 'network'
                                    try:
                                        with db_manager.get_connection() as conn:
                                            prow = conn.execute(
                                                "SELECT privacy_mode FROM channels WHERE id = ?",
                                                (message.channel_id,)
                                            ).fetchone()
                                        if prow and prow['privacy_mode'] and prow['privacy_mode'] != 'open':
                                            contract_visibility = 'local'
                                    except Exception:
                                        contract_visibility = 'local'
                                    contract_obj = contract_manager.upsert_contract(
                                        contract_id=contract_id,
                                        title=spec.title,
                                        summary=spec.summary,
                                        terms=spec.terms,
                                        status=spec.status,
                                        owner_id=owner_id,
                                        counterparties=counterparties,
                                        created_by=message.user_id,
                                        visibility=contract_visibility,
                                        origin_peer=getattr(message, 'origin_peer', None),
                                        source_type='channel_message',
                                        source_id=message.id,
                                        expires_at=spec.expires_at.isoformat() if spec.expires_at else None,
                                        ttl_seconds=spec.ttl_seconds,
                                        ttl_mode=spec.ttl_mode,
                                        metadata=spec.metadata,
                                        created_at=message.created_at.isoformat() if getattr(message, 'created_at', None) else None,
                                        actor_id=message.user_id,
                                    )
                                if contract_obj:
                                    payload = contract_obj
                                    payload['status_label'] = (payload.get('status') or 'proposed').replace('_', ' ').title()
                                    expiry_dt = None
                                    if payload.get('expires_at'):
                                        try:
                                            expiry_dt = datetime.fromisoformat(payload['expires_at'].replace('Z', '+00:00'))
                                        except Exception:
                                            expiry_dt = None
                                    payload['expires_label'] = _signal_expiry_label(expiry_dt, now_dt)
                                    owner_display = _user_display(payload.get('owner_id'))
                                    payload['owner_name'] = (owner_display or {}).get('display_name') if owner_display else payload.get('owner_id')
                                    payload['owner_avatar_url'] = (owner_display or {}).get('avatar_url') if owner_display else None
                                    payload['owner_origin_peer'] = (owner_display or {}).get('origin_peer') if owner_display else None
                                    counterparty_payload = []
                                    for cp_id in payload.get('counterparties') or []:
                                        cp_display = _user_display(cp_id)
                                        counterparty_payload.append({
                                            'user_id': cp_id,
                                            'display_name': (cp_display or {}).get('display_name') if cp_display else cp_id,
                                            'avatar_url': (cp_display or {}).get('avatar_url') if cp_display else None,
                                            'origin_peer': (cp_display or {}).get('origin_peer') if cp_display else None,
                                        })
                                    payload['counterparty_users'] = counterparty_payload
                                    allowed_ids = {payload.get('owner_id'), payload.get('created_by')}
                                    allowed_ids.update(set(payload.get('counterparties') or []))
                                    payload['can_manage'] = (
                                        user_id == payload.get('owner_id')
                                        or user_id == payload.get('created_by')
                                        or (admin_user_id and user_id == admin_user_id)
                                    )
                                    payload['can_participate'] = bool(user_id and user_id in allowed_ids)
                                    contracts_payload.append(payload)
                            except Exception as contract_err:
                                logger.warning(f"Contract render error for message {message.id}: {contract_err}")
                                continue

                    # Inline tasks for channel messages
                    inline_tasks = []
                    display_content = message.content or ''
                    task_specs = parse_task_blocks(message.content or '')
                    if task_specs:
                        for idx, spec in enumerate(cast(Any, task_specs)):
                            spec = cast(Any, spec)
                            if not spec.confirmed:
                                continue
                            task_id = derive_task_id('channel', message.id, idx, len(task_specs), override=spec.task_id)
                            task_obj = task_manager.get_task(task_id) if task_manager else None
                            task_payload = task_obj.to_dict() if task_obj else spec.to_dict()
                            task_payload['id'] = task_id
                            task_payload['status'] = (task_payload.get('status') or spec.status or 'open')
                            task_payload['priority'] = (task_payload.get('priority') or spec.priority or 'normal')
                            task_payload['status_label'] = task_payload['status'].replace('_', ' ').title()
                            task_payload['priority_label'] = task_payload['priority'].title()
                            task_payload['due_at_label'] = None
                            if task_payload.get('due_at'):
                                try:
                                    due_dt = datetime.fromisoformat(task_payload['due_at'].replace('Z', '+00:00'))
                                    task_payload['due_at_label'] = due_dt.date().isoformat()
                                except Exception:
                                    task_payload['due_at_label'] = None
                            inline_tasks.append(task_payload)
                        display_content = strip_task_blocks(display_content or '', remove_unconfirmed=False)

                    if signals_payload:
                        display_content = strip_signal_blocks(display_content or '')
                    if contracts_payload:
                        display_content = strip_contract_blocks(display_content or '', remove_unconfirmed=False)
                    if request_specs:
                        display_content = strip_request_blocks(display_content or '', remove_unconfirmed=False)

                    # Inline handoffs for channel messages
                    inline_handoffs = []
                    handoff_specs = parse_handoff_blocks(message.content or '')
                    if handoff_specs:
                        handoff_visibility = 'network'
                        try:
                            with db_manager.get_connection() as conn:
                                prow = conn.execute(
                                    "SELECT privacy_mode FROM channels WHERE id = ?",
                                    (message.channel_id,)
                                ).fetchone()
                            if prow and prow['privacy_mode'] and prow['privacy_mode'] != 'open':
                                handoff_visibility = 'local'
                        except Exception:
                            handoff_visibility = 'local'

                        for idx, spec in enumerate(cast(Any, handoff_specs)):
                            spec = cast(Any, spec)
                            if not spec.confirmed:
                                continue
                            handoff_id = derive_handoff_id('channel', message.id, idx, len(handoff_specs), override=spec.handoff_id)
                            handoff_obj = handoff_manager.get_handoff(handoff_id) if handoff_manager else None
                            if not handoff_obj and handoff_manager:
                                handoff_obj = handoff_manager.upsert_handoff(
                                    handoff_id=handoff_id,
                                    source_type='channel',
                                    source_id=message.id,
                                    author_id=message.user_id,
                                    title=spec.title,
                                    summary=spec.summary,
                                    next_steps=spec.next_steps,
                                    owner=spec.owner,
                                    tags=spec.tags,
                                    raw=spec.raw,
                                    channel_id=message.channel_id,
                                    visibility=handoff_visibility,
                                    origin_peer=getattr(message, 'origin_peer', None),
                                    created_at=message.created_at.isoformat() if getattr(message, 'created_at', None) else None,
                                    required_capabilities=spec.required_capabilities,
                                    escalation_level=spec.escalation_level,
                                    return_to=spec.return_to,
                                    context_payload=spec.context_payload,
                                )
                            payload = handoff_obj.to_dict() if handoff_obj else spec.to_dict()
                            payload['id'] = handoff_id
                            owner_id = payload.get('owner') or payload.get('owner_id')
                            if owner_id:
                                display = _user_display(owner_id)
                                if display:
                                    payload['owner_name'] = display.get('display_name') or owner_id
                                    payload['owner_avatar_url'] = display.get('avatar_url')
                                    payload['owner_origin_peer'] = display.get('origin_peer')
                            return_to_id = payload.get('return_to')
                            if return_to_id:
                                return_display = _user_display(return_to_id)
                                if return_display:
                                    payload['return_to_name'] = return_display.get('display_name') or return_to_id
                            inline_handoffs.append(payload)

                        display_content = strip_handoff_blocks(display_content or '', remove_unconfirmed=False)

                    if circle_specs:
                        display_content = strip_circle_blocks(display_content or '')
                    if objective_specs:
                        display_content = strip_objective_blocks(display_content or '')

                    # Inline skills for channel messages
                    inline_skills = []
                    try:
                        from ..core.skills import parse_skill_blocks, strip_skill_blocks
                        skill_specs = parse_skill_blocks(message.content or '')
                        if skill_specs:
                            for spec in skill_specs:
                                inline_skills.append(
                                    _build_inline_skill_payload(skill_manager, spec, 'channel_message', message.id)
                                )
                            display_content = strip_skill_blocks(display_content or '')
                    except Exception:
                        pass

                    msg_dict['display_content'] = display_content
                    if circles_payload:
                        msg_dict['circles'] = circles_payload
                    if objectives_payload:
                        msg_dict['objectives'] = objectives_payload
                    if requests_payload:
                        msg_dict['requests'] = requests_payload
                    if signals_payload:
                        msg_dict['signals'] = signals_payload
                    if contracts_payload:
                        msg_dict['contracts'] = contracts_payload
                    if inline_tasks:
                        msg_dict['inline_tasks'] = inline_tasks
                    if inline_handoffs:
                        msg_dict['handoffs'] = inline_handoffs
                    if inline_skills:
                        msg_dict['skills'] = inline_skills
                    message_notes = _load_target_notes(skill_manager, 'channel_message', message.id, user_id, limit=8)
                    msg_dict['community_notes_count'] = len(message_notes)
                    if message_notes:
                        msg_dict['community_notes'] = message_notes

                    messages_data.append(msg_dict)
                except Exception as msg_err:
                    logger.warning(f"Skipping message {getattr(message, 'id', '?')} in response: {msg_err}")
                    continue
            
            return jsonify({
                'messages': messages_data,
                'channel_id': channel_id,
                'count': len(messages_data)
            })
            
        except Exception as e:
            logger.error(f"Get channel messages error: {e}", exc_info=True)
            # Return empty messages instead of 500 so the UI doesn't show an error
            return jsonify({'messages': [], 'channel_id': channel_id, 'count': 0, 'warning': str(e)})

    @ui.route('/ajax/channel_search/<channel_id>', methods=['GET'])
    @require_login
    def ajax_search_channel_messages(channel_id):
        """AJAX endpoint to search messages in a channel."""
        try:
            db_manager, _, _, _, channel_manager, file_manager, _, interaction_manager, profile_manager, _, p2p_manager = _get_app_components_any(current_app)
            user_id = get_current_user()
            access = channel_manager.get_channel_access_decision(
                channel_id=channel_id,
                user_id=user_id,
                require_membership=True,
            )
            if not access.get('allowed'):
                if str(access.get('reason') or '').startswith('governance_'):
                    return jsonify({
                        'error': 'Channel access blocked by admin governance policy',
                        'reason': access.get('reason'),
                    }), 403
                return jsonify({'error': 'You are not a member of this channel'}), 403
            query = request.args.get('q', '').strip()
            limit = int(request.args.get('limit', 50))
            from ..core.polls import parse_poll, resolve_poll_end, describe_poll_status
            from ..core.tasks import parse_task_blocks, strip_task_blocks, derive_task_id
            from ..core.circles import parse_circle_blocks, strip_circle_blocks, derive_circle_id
            from ..core.handoffs import parse_handoff_blocks, strip_handoff_blocks, derive_handoff_id
            from ..core.requests import strip_request_blocks
            now_dt = datetime.now(timezone.utc)
            task_manager = current_app.config.get('TASK_MANAGER')
            circle_manager = current_app.config.get('CIRCLE_MANAGER')
            handoff_manager = current_app.config.get('HANDOFF_MANAGER')
            skill_manager = current_app.config.get('SKILL_MANAGER')
            user_display_cache: dict[str, dict[str, Any]] = {}

            def _user_display(uid: str) -> Optional[dict[str, Any]]:
                if not uid:
                    return None
                if uid in user_display_cache:
                    return user_display_cache[uid]
                display = {
                    'display_name': uid,
                    'avatar_url': None,
                    'origin_peer': None,
                }
                try:
                    if profile_manager:
                        profile = profile_manager.get_profile(uid)
                        if profile:
                            display['display_name'] = profile.display_name or profile.username or uid
                            display['avatar_url'] = profile.avatar_url
                            display['origin_peer'] = getattr(profile, 'origin_peer', None)
                    elif db_manager:
                        row = db_manager.get_user(uid)
                        if row:
                            display['display_name'] = row.get('display_name') or row.get('username') or uid
                            display['origin_peer'] = row.get('origin_peer')
                except Exception:
                    pass
                user_display_cache[uid] = display
                return display

            if not query:
                return jsonify({'messages': [], 'count': 0, 'query': ''})

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
                    if msg.get('user_id') != user_id:
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

            results = channel_manager.search_channel_messages(
                channel_id, query, user_id, limit)
            search_messages = []
            for m in results:
                d = m.to_dict()
                for att in (d.get('attachments') or []):
                    if not isinstance(att, dict):
                        continue
                    if att.get('id') and not att.get('url'):
                        if file_manager and file_manager.get_file(att['id']):
                            att['url'] = f"/files/{att['id']}"
                        else:
                            att['not_on_device'] = True
                poll_spec = parse_poll(m.content or '')
                if poll_spec and interaction_manager:
                    poll_end = resolve_poll_end(m.created_at, m.expires_at, poll_spec)
                    is_closed = bool(poll_end and poll_end <= now_dt)
                    results_data = interaction_manager.get_poll_results(m.id, 'channel', len(poll_spec.options))
                    user_vote = interaction_manager.get_user_poll_vote(m.id, 'channel', user_id)
                    total_votes = results_data.get('total', 0)
                    option_payload = []
                    for idx, label in enumerate(poll_spec.options):
                        count = results_data['counts'][idx] if idx < len(results_data['counts']) else 0
                        percent = (count / total_votes * 100.0) if total_votes else 0.0
                        option_payload.append({
                            'label': label,
                            'count': count,
                            'percent': round(percent, 1),
                            'index': idx
                        })
                    status_label = describe_poll_status(poll_end, now=now_dt)
                    d['poll'] = {
                        'question': poll_spec.question,
                        'options': option_payload,
                        'ends_at': poll_end.isoformat() if poll_end else None,
                        'status_label': status_label,
                        'is_closed': is_closed,
                        'user_vote': user_vote,
                        'total_votes': total_votes,
                    }
                circles_payload = []
                circle_specs = parse_circle_blocks(m.content or '')
                if circle_specs and circle_manager:
                    for idx, spec in enumerate(cast(Any, circle_specs)):
                        spec = cast(Any, spec)
                        circle_id = derive_circle_id('channel', m.id, idx, len(circle_specs), override=spec.circle_id)
                        facilitator_id = None
                        if spec.facilitator:
                            facilitator_id = _resolve_handle_to_user_id(
                                db_manager,
                                spec.facilitator,
                                channel_id=m.channel_id,
                                author_id=m.user_id,
                            )
                        if not facilitator_id:
                            facilitator_id = m.user_id
                        if spec.participants is not None:
                            resolved_participants = _resolve_handle_list(
                                db_manager,
                                spec.participants,
                                channel_id=m.channel_id,
                                author_id=m.user_id,
                            )
                            spec.participants = resolved_participants

                        visibility = 'network'
                        try:
                            with db_manager.get_connection() as conn:
                                prow = conn.execute(
                                    "SELECT privacy_mode FROM channels WHERE id = ?",
                                    (m.channel_id,)
                                ).fetchone()
                            if prow and prow['privacy_mode'] and prow['privacy_mode'] != 'open':
                                visibility = 'local'
                        except Exception as vis_err:
                            logger.debug(f"Circle visibility lookup failed: {vis_err}")
                            visibility = 'network'

                        circle_obj = circle_manager.upsert_circle(
                            circle_id=circle_id,
                            source_type='channel',
                            source_id=m.id,
                            created_by=m.user_id,
                            spec=spec,
                            channel_id=m.channel_id,
                            facilitator_id=facilitator_id,
                            visibility=visibility,
                            origin_peer=getattr(m, 'origin_peer', None),
                            created_at=m.created_at.isoformat() if getattr(m, 'created_at', None) else None,
                        )
                        if circle_obj:
                            circle_payload = circle_obj.to_dict()
                            circle_payload['phase_label'] = (circle_payload.get('phase') or 'opinion').replace('_', ' ').title()
                            circle_payload['entries_count'] = circle_manager.count_entries(circle_obj.id)
                            circles_payload.append(circle_payload)
                inline_tasks = []
                display_content = m.content or ''
                task_specs = parse_task_blocks(m.content or '')
                if task_specs:
                    for idx, spec in enumerate(cast(Any, task_specs)):
                        spec = cast(Any, spec)
                        if not spec.confirmed:
                            continue
                        task_id = derive_task_id('channel', m.id, idx, len(task_specs), override=spec.task_id)
                        task_obj = task_manager.get_task(task_id) if task_manager else None
                        task_payload = task_obj.to_dict() if task_obj else spec.to_dict()
                        task_payload['id'] = task_id
                        task_payload['status'] = (task_payload.get('status') or spec.status or 'open')
                        task_payload['priority'] = (task_payload.get('priority') or spec.priority or 'normal')
                        task_payload['status_label'] = task_payload['status'].replace('_', ' ').title()
                        task_payload['priority_label'] = task_payload['priority'].title()
                        task_payload['due_at_label'] = None
                    if task_payload.get('due_at'):
                        try:
                            due_dt = datetime.fromisoformat(task_payload['due_at'].replace('Z', '+00:00'))
                            task_payload['due_at_label'] = due_dt.date().isoformat()
                        except Exception:
                            task_payload['due_at_label'] = None
                    inline_tasks.append(task_payload)
                    display_content = strip_task_blocks(display_content or '', remove_unconfirmed=False)

                display_content = strip_request_blocks(display_content or '', remove_unconfirmed=False)

                inline_handoffs = []
                handoff_specs = parse_handoff_blocks(m.content or '')
                if handoff_specs:
                    handoff_visibility = 'network'
                    try:
                        with db_manager.get_connection() as conn:
                            prow = conn.execute(
                                "SELECT privacy_mode FROM channels WHERE id = ?",
                                (m.channel_id,)
                            ).fetchone()
                        if prow and prow['privacy_mode'] and prow['privacy_mode'] != 'open':
                            handoff_visibility = 'local'
                    except Exception as vis_err:
                        logger.debug(f"Handoff visibility lookup failed: {vis_err}")
                        handoff_visibility = 'local'

                    for idx, spec in enumerate(cast(Any, handoff_specs)):
                        spec = cast(Any, spec)
                        if not spec.confirmed:
                            continue
                        handoff_id = derive_handoff_id('channel', m.id, idx, len(handoff_specs), override=spec.handoff_id)
                        handoff_obj = handoff_manager.get_handoff(handoff_id) if handoff_manager else None
                        if not handoff_obj and handoff_manager:
                            handoff_obj = handoff_manager.upsert_handoff(
                                handoff_id=handoff_id,
                                source_type='channel',
                                source_id=m.id,
                                author_id=m.user_id,
                                title=spec.title,
                                summary=spec.summary,
                                next_steps=spec.next_steps,
                                owner=spec.owner,
                                tags=spec.tags,
                                raw=spec.raw,
                                channel_id=m.channel_id,
                                visibility=handoff_visibility,
                                origin_peer=getattr(m, 'origin_peer', None),
                                created_at=m.created_at.isoformat() if getattr(m, 'created_at', None) else None,
                                required_capabilities=spec.required_capabilities,
                                escalation_level=spec.escalation_level,
                                return_to=spec.return_to,
                                context_payload=spec.context_payload,
                            )
                        payload = handoff_obj.to_dict() if handoff_obj else spec.to_dict()
                        payload['id'] = handoff_id
                        owner_id = payload.get('owner') or payload.get('owner_id')
                        if owner_id:
                            display = None
                            try:
                                if profile_manager:
                                    profile = profile_manager.get_profile(owner_id)
                                    if profile:
                                        display = {
                                            'display_name': profile.display_name or profile.username or owner_id,
                                            'avatar_url': profile.avatar_url,
                                            'origin_peer': getattr(profile, 'origin_peer', None),
                                        }
                                elif db_manager:
                                    row = db_manager.get_user(owner_id)
                                    if row:
                                        display = {
                                            'display_name': row.get('display_name') or row.get('username') or owner_id,
                                            'avatar_url': None,
                                            'origin_peer': row.get('origin_peer'),
                                        }
                            except Exception:
                                display = None
                            if display:
                                payload['owner_name'] = display.get('display_name') or owner_id
                                payload['owner_avatar_url'] = display.get('avatar_url')
                                payload['owner_origin_peer'] = display.get('origin_peer')
                        return_to_id = payload.get('return_to')
                        if return_to_id:
                            return_display = _user_display(return_to_id)
                            if return_display:
                                payload['return_to_name'] = return_display.get('display_name') or return_to_id
                        inline_handoffs.append(payload)
                    display_content = strip_handoff_blocks(display_content or '', remove_unconfirmed=False)

                if circle_specs:
                    display_content = strip_circle_blocks(display_content or '')

                # Inline skills
                inline_skills = []
                try:
                    from ..core.skills import parse_skill_blocks as _psb2, strip_skill_blocks as _ssb2
                    skill_specs2 = _psb2(m.content or '')
                    if skill_specs2:
                        for spec in skill_specs2:
                            inline_skills.append(
                                _build_inline_skill_payload(skill_manager, spec, 'channel_message', m.id)
                            )
                        display_content = _ssb2(display_content or '')
                except Exception:
                    pass

                d['display_content'] = display_content
                if circles_payload:
                    d['circles'] = circles_payload
                if inline_tasks:
                    d['inline_tasks'] = inline_tasks
                if inline_handoffs:
                    d['handoffs'] = inline_handoffs
                if inline_skills:
                    d['skills'] = inline_skills
                message_notes = _load_target_notes(skill_manager, 'channel_message', m.id, user_id, limit=8)
                d['community_notes_count'] = len(message_notes)
                if message_notes:
                    d['community_notes'] = message_notes

                search_messages.append(d)
            return jsonify({
                'messages': search_messages,
                'query': query,
                'count': len(results),
            })
        except Exception as e:
            logger.error(f"Channel search error: {e}")
            return jsonify({'error': 'Internal server error'}), 500

    @ui.route('/ajax/streams', methods=['GET'])
    @require_login
    def ajax_list_streams():
        """List streams visible to the current user."""
        try:
            stream_manager = current_app.config.get('STREAM_MANAGER')
            if not stream_manager:
                return jsonify({'success': False, 'error': 'Streaming unavailable'}), 503
            user_id = get_current_user()
            channel_id = str(request.args.get('channel_id') or '').strip() or None
            status = str(request.args.get('status') or '').strip().lower() or None
            try:
                limit = int(request.args.get('limit', 100))
            except Exception:
                limit = 100
            streams = stream_manager.list_streams_for_user(
                user_id=user_id,
                channel_id=channel_id,
                status=status,
                limit=limit,
            )
            return jsonify({'success': True, 'streams': streams, 'count': len(streams)})
        except Exception as e:
            logger.error(f"List streams UI failed: {e}", exc_info=True)
            return jsonify({'success': False, 'error': 'Internal server error'}), 500

    @ui.route('/ajax/streams', methods=['POST'])
    @require_login
    def ajax_create_stream():
        """Create a stream, optionally post a stream card into the channel."""
        try:
            db_manager, _, _, _, channel_manager, _, _, _, profile_manager, _, p2p_manager = _get_app_components_any(current_app)
            stream_manager = current_app.config.get('STREAM_MANAGER')
            if not stream_manager:
                return jsonify({'success': False, 'error': 'Streaming unavailable'}), 503

            user_id = get_current_user()
            data = request.get_json(silent=True) or {}
            channel_id = str(data.get('channel_id') or '').strip()
            title = str(data.get('title') or '').strip()
            description = str(data.get('description') or '').strip()
            stream_kind = str(data.get('stream_kind') or '').strip().lower() or None
            media_kind = str(data.get('media_kind') or 'audio').strip().lower()
            protocol_default = 'events-json' if stream_kind == 'telemetry' else 'hls'
            protocol = str(data.get('protocol') or protocol_default).strip().lower()
            relay_allowed = str(data.get('relay_allowed') or '').strip().lower() in {'1', 'true', 'yes', 'on'}
            auto_post = True if data.get('auto_post') is None else str(data.get('auto_post')).strip().lower() in {'1', 'true', 'yes', 'on'}
            start_now = str(data.get('start_now') or '').strip().lower() in {'1', 'true', 'yes', 'on'}

            stream_row, error = stream_manager.create_stream(
                channel_id=channel_id,
                created_by=user_id,
                title=title,
                description=description,
                stream_kind=stream_kind,
                media_kind=media_kind,
                protocol=protocol,
                relay_allowed=relay_allowed,
                origin_peer=(p2p_manager.get_peer_id() if p2p_manager else None),
                metadata={'created_via': 'ui'},
            )
            if error:
                if error in {'channel_not_found', 'not_channel_member'}:
                    return jsonify({'success': False, 'error': 'Channel not found'}), 404
                return jsonify({'success': False, 'error': error}), 400

            posted_message_id = None
            if auto_post and stream_row:
                from ..core.channels import MessageType as ChannelMessageType

                # Embed LAN HTTP addresses so remote peers can route directly to us
                _local_port = current_app.config.get('PORT', 7770)
                _host_addrs: list[str] = []
                try:
                    import socket as _socket
                    _hostname = _socket.gethostname()
                    _local_ips = [i[4][0] for i in _socket.getaddrinfo(_hostname, None)
                                  if i[0].name == 'AF_INET' and not i[4][0].startswith('127.')]
                    _host_addrs = [f"http://{ip}:{_local_port}" for ip in _local_ips]
                except Exception:
                    pass

                attachment = {
                    'name': str(stream_row.get('title') or title or 'Live stream'),
                    'type': 'application/vnd.canopy.stream+json',
                    'kind': 'stream',
                    'stream_id': str(stream_row.get('id') or ''),
                    'title': str(stream_row.get('title') or title or ''),
                    'description': str(stream_row.get('description') or description or ''),
                    'media_kind': str(stream_row.get('media_kind') or media_kind or 'audio'),
                    'stream_kind': str(stream_row.get('stream_kind') or stream_kind or 'media'),
                    'protocol': str(stream_row.get('protocol') or protocol or 'hls'),
                    'status': str(stream_row.get('status') or 'created'),
                    'channel_id': str(stream_row.get('channel_id') or channel_id),
                    'created_by': str(stream_row.get('created_by') or user_id),
                    'relay_allowed': bool(stream_row.get('relay_allowed')),
                    'host_addrs': _host_addrs,
                }
                post_content = str(data.get('post_content') or '').strip()
                if not post_content:
                    stream_kind_value = str(stream_row.get('stream_kind') or stream_kind or 'media').lower()
                    if stream_kind_value == 'telemetry':
                        label = "Telemetry stream"
                    else:
                        label = "Live video stream" if media_kind == "video" else "Live audio stream"
                    post_content = f"{label}: {stream_row.get('title') or title}"
                message = channel_manager.send_message(
                    channel_id=channel_id,
                    user_id=user_id,
                    content=post_content,
                    message_type=ChannelMessageType.FILE,
                    attachments=[attachment],
                    origin_peer=(p2p_manager.get_peer_id() if p2p_manager else None),
                )
                if message:
                    posted_message_id = message.id
                    try:
                        if p2p_manager:
                            display_name = None
                            if profile_manager:
                                profile = profile_manager.get_profile(user_id)
                                if profile:
                                    display_name = profile.display_name or profile.username
                            mode_row = None
                            with db_manager.get_connection() as conn:
                                mode_row = conn.execute(
                                    "SELECT privacy_mode FROM channels WHERE id = ?",
                                    (channel_id,),
                                ).fetchone()
                            channel_mode = str(mode_row['privacy_mode'] if mode_row else 'open').lower()
                            target_peers = None
                            if channel_mode in {'private', 'confidential'}:
                                target_peers = channel_manager.get_target_peer_ids_for_channel(channel_id)
                            p2p_manager.broadcast_channel_message(
                                channel_id=channel_id,
                                user_id=user_id,
                                content=post_content,
                                message_id=message.id,
                                timestamp=message.created_at.isoformat() if getattr(message, 'created_at', None) else datetime.now(timezone.utc).isoformat(),
                                attachments=[attachment],
                                display_name=display_name,
                                security={'privacy_mode': channel_mode},
                                target_peer_ids=target_peers,
                            )
                    except Exception as bcast_err:
                        logger.warning(f"Failed to broadcast stream post card: {bcast_err}")

            if start_now and stream_row:
                started, start_err = stream_manager.start_stream(stream_row['id'], user_id)
                if not start_err and started:
                    stream_row = started

            payload = {'success': True, 'stream': stream_row}
            if posted_message_id:
                payload['posted_message_id'] = posted_message_id
            return jsonify(payload), 201
        except Exception as e:
            logger.error(f"Create stream UI failed: {e}", exc_info=True)
            return jsonify({'success': False, 'error': 'Internal server error'}), 500

    @ui.route('/ajax/streams/<stream_id>', methods=['GET'])
    @require_login
    def ajax_get_stream(stream_id):
        try:
            stream_manager = current_app.config.get('STREAM_MANAGER')
            if not stream_manager:
                return jsonify({'success': False, 'error': 'Streaming unavailable'}), 503
            user_id = get_current_user()
            stream_row = stream_manager.get_stream_for_user(stream_id, user_id)
            if not stream_row:
                return jsonify({'success': False, 'error': 'Not found'}), 404
            return jsonify({'success': True, 'stream': stream_row})
        except Exception as e:
            logger.error(f"Get stream UI failed: {e}", exc_info=True)
            return jsonify({'success': False, 'error': 'Internal server error'}), 500

    @ui.route('/ajax/streams/<stream_id>/start', methods=['POST'])
    @require_login
    def ajax_start_stream(stream_id):
        try:
            stream_manager = current_app.config.get('STREAM_MANAGER')
            if not stream_manager:
                return jsonify({'success': False, 'error': 'Streaming unavailable'}), 503
            user_id = get_current_user()
            stream_row, error = stream_manager.start_stream(stream_id, user_id)
            if error in {'not_found', 'not_authorized'}:
                return jsonify({'success': False, 'error': 'Not found'}), 404
            if error:
                return jsonify({'success': False, 'error': error}), 400
            return jsonify({'success': True, 'stream': stream_row})
        except Exception as e:
            logger.error(f"Start stream UI failed: {e}", exc_info=True)
            return jsonify({'success': False, 'error': 'Internal server error'}), 500

    @ui.route('/ajax/streams/<stream_id>/stop', methods=['POST'])
    @require_login
    def ajax_stop_stream(stream_id):
        try:
            stream_manager = current_app.config.get('STREAM_MANAGER')
            if not stream_manager:
                return jsonify({'success': False, 'error': 'Streaming unavailable'}), 503
            user_id = get_current_user()
            stream_row, error = stream_manager.stop_stream(stream_id, user_id)
            if error in {'not_found', 'not_authorized'}:
                return jsonify({'success': False, 'error': 'Not found'}), 404
            if error:
                return jsonify({'success': False, 'error': error}), 400
            return jsonify({'success': True, 'stream': stream_row})
        except Exception as e:
            logger.error(f"Stop stream UI failed: {e}", exc_info=True)
            return jsonify({'success': False, 'error': 'Internal server error'}), 500

    @ui.route('/ajax/streams/<stream_id>/session', methods=['POST'])
    @require_login
    def ajax_stream_session(stream_id):
        """Issue a short-lived stream playback token for the current user."""
        try:
            stream_manager = current_app.config.get('STREAM_MANAGER')
            if not stream_manager:
                return jsonify({'success': False, 'error': 'Streaming unavailable'}), 503
            user_id = get_current_user()
            logger.info(f"Stream session request: stream_id={stream_id} user_id={user_id} ip={request.remote_addr}")
            data = request.get_json(silent=True) or {}
            ttl_seconds = data.get('ttl_seconds')
            token_payload, error = stream_manager.issue_token(
                stream_id=stream_id,
                user_id=user_id,
                scope='view',
                ttl_seconds=ttl_seconds,
                metadata={'issued_via': 'ui_session'},
            )
            logger.info(f"Stream session result: stream_id={stream_id} user_id={user_id} error={error} token_ok={bool(token_payload)}")
            if error in {'not_found', 'not_authorized'}:
                # Try to route to the peer that owns this stream
                db_manager, _, _, _, _, _, _, _, _, _, p2p_manager = _get_app_components_any(current_app)
                remote = _resolve_p2p_stream(stream_id, db_manager, p2p_manager)
                if remote:
                    return jsonify({
                        'success': True,
                        'stream_kind': 'media',
                        'playback_url': remote['playback_url'],
                        'transport_url': remote['playback_url'],
                        'remote': True,
                        'origin_peer': remote.get('origin_peer'),
                    })
                return jsonify({'success': False, 'error': 'Not found'}), 404
            if error or not token_payload:
                return jsonify({'success': False, 'error': error or 'token_issue_failed'}), 400

            stream_row = stream_manager.get_stream_for_user(stream_id, user_id)
            token_q = quote_plus(str(token_payload.get('token') or ''))
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
            logger.error(f"Create stream session failed: {e}", exc_info=True)
            return jsonify({'success': False, 'error': 'Internal server error'}), 500

    @ui.route('/ajax/streams/<stream_id>/ingest-token', methods=['POST'])
    @require_login
    def ajax_stream_ingest_token(stream_id):
        """Issue a short-lived ingest token for the stream owner (used by browser broadcaster)."""
        try:
            stream_manager = current_app.config.get('STREAM_MANAGER')
            if not stream_manager:
                return jsonify({'success': False, 'error': 'Streaming unavailable'}), 503
            user_id = get_current_user()
            token_payload, error = stream_manager.issue_token(
                stream_id=stream_id,
                user_id=user_id,
                scope='ingest',
                ttl_seconds=4 * 3600,
                metadata={'issued_via': 'browser_broadcaster'},
            )
            if error in {'not_found', 'not_authorized'}:
                return jsonify({'success': False, 'error': 'Not found'}), 404
            if error or not token_payload:
                return jsonify({'success': False, 'error': error or 'token_issue_failed'}), 400
            return jsonify({'success': True, 'token': token_payload.get('token'), 'expires_at': token_payload.get('expires_at')})
        except Exception as e:
            logger.error(f"Ingest token failed: {e}", exc_info=True)
            return jsonify({'success': False, 'error': 'Internal server error'}), 500

    @ui.route('/ajax/streams/<stream_id>/setup', methods=['POST'])
    @require_login
    def ajax_stream_setup(stream_id):
        """Return a setup bundle for stream operators: ingest/view tokens, URLs, and ffmpeg command templates."""
        try:
            stream_manager = current_app.config.get('STREAM_MANAGER')
            if not stream_manager:
                return jsonify({'success': False, 'error': 'Streaming unavailable'}), 503

            user_id = get_current_user()
            data = request.get_json(silent=True) or {}
            ingest_ttl = data.get('ingest_ttl_seconds', 4 * 3600)
            view_ttl = data.get('view_ttl_seconds', 3600)

            ingest_payload, ingest_err = stream_manager.issue_token(
                stream_id=stream_id,
                user_id=user_id,
                scope='ingest',
                ttl_seconds=ingest_ttl,
                metadata={'issued_via': 'ui_setup'},
            )
            if ingest_err in {'not_found', 'not_authorized'}:
                return jsonify({'success': False, 'error': 'Not found'}), 404
            if ingest_err or not ingest_payload:
                return jsonify({'success': False, 'error': ingest_err or 'token_issue_failed'}), 400

            view_payload, view_err = stream_manager.issue_token(
                stream_id=stream_id,
                user_id=user_id,
                scope='view',
                ttl_seconds=view_ttl,
                metadata={'issued_via': 'ui_setup'},
            )
            if view_err or not view_payload:
                return jsonify({'success': False, 'error': view_err or 'view_token_failed'}), 400

            stream_row = stream_manager.get_stream_for_user(stream_id, user_id) or {}
            stream_kind = str(stream_row.get('stream_kind') or 'media').lower()
            protocol = str(stream_row.get('protocol') or 'hls').lower()

            ingest_tok = quote_plus(str(ingest_payload.get('token') or ''))
            view_tok = quote_plus(str(view_payload.get('token') or ''))
            base = f"/api/v1/streams/{stream_id}"

            if stream_kind == 'telemetry' or protocol == 'events-json':
                ingest_bundle = {'events_url': f"{base}/ingest/events?token={ingest_tok}"}
                playback = {'url': f"{base}/events?token={view_tok}"}
                posix_cmd = f"# curl -X POST '{base}/ingest/events?token={ingest_tok}' -H 'Content-Type: application/json' -d '{{\"value\": 1}}'"
                ps_cmd = posix_cmd
            else:
                ingest_bundle = {
                    'manifest_url': f"{base}/ingest/manifest?token={ingest_tok}",
                    'segment_url_template': f"{base}/ingest/segments/seg%06d.ts?token={ingest_tok}",
                }
                playback = {'url': f"{base}/manifest.m3u8?token={view_tok}"}
                posix_cmd = (
                    f"ffmpeg -re -i INPUT -c:v libx264 -preset veryfast -tune zerolatency"
                    f" -c:a aac -b:a 128k -f hls -hls_time 2 -hls_list_size 5"
                    f" -hls_flags delete_segments"
                    f" -hls_segment_filename '{base}/ingest/segments/seg%06d.ts?token={ingest_tok}'"
                    f" '{base}/ingest/manifest?token={ingest_tok}'"
                )
                ps_cmd = posix_cmd.replace("'", '"')

            return jsonify({
                'success': True,
                'setup': {
                    'stream_id': stream_id,
                    'stream_kind': stream_kind,
                    'ingest': ingest_bundle,
                    'playback': playback,
                    'commands': {'posix': posix_cmd, 'powershell': ps_cmd},
                    'ingest_expires_at': ingest_payload.get('expires_at'),
                    'view_expires_at': view_payload.get('expires_at'),
                },
            })
        except Exception as e:
            logger.error(f"Stream setup failed: {e}", exc_info=True)
            return jsonify({'success': False, 'error': 'Internal server error'}), 500

    @ui.route('/ajax/send_channel_message', methods=['POST'])
    @require_login
    def ajax_send_channel_message():
        """AJAX endpoint to send a message to a channel."""
        try:
            db_manager, _, _, _, channel_manager, file_manager, _, _, profile_manager, _, p2p_manager = _get_app_components_any(current_app)
            user_id = get_current_user()
            
            data = request.get_json()
            logger.info(f"Send channel message request: user_id={user_id}, data={data}")
            
            content = data.get('content', '').strip()
            channel_id = data.get('channel_id')
            file_attachments = data.get('attachments', [])
            parent_message_id = data.get('parent_message_id')
            security = data.get('security')
            ttl_mode = data.get('ttl_mode')
            ttl_seconds = data.get('ttl_seconds')
            expires_at = data.get('expires_at')
            
            logger.info(f"Parsed data: content='{content}', channel_id='{channel_id}', attachments_count={len(file_attachments)}")
            
            if not content and not file_attachments:
                return jsonify({'error': 'Message content or attachments required'}), 400
            
            if not channel_id:
                return jsonify({'error': 'Channel ID required'}), 400

            access = channel_manager.get_channel_access_decision(
                channel_id=channel_id,
                user_id=user_id,
                require_membership=True,
            )
            if not access.get('allowed'):
                if str(access.get('reason') or '').startswith('governance_'):
                    return jsonify({
                        'error': 'Channel access blocked by admin governance policy',
                        'reason': access.get('reason'),
                    }), 403
                return jsonify({'error': 'You are not a member of this channel'}), 403

            security_clean = None
            if security is not None:
                security_clean, sec_error = channel_manager.validate_security_metadata(security, strict=True)
                if sec_error:
                    return jsonify({'error': sec_error}), 400
            
            # Process file attachments if any
            processed_attachments = []
            for attachment in file_attachments:
                try:
                    # Attachment should contain file data as base64
                    file_data = base64.b64decode(attachment['data'])
                    file_info = file_manager.save_file(
                        file_data, 
                        attachment['name'], 
                        attachment['type'], 
                        user_id
                    )
                    
                    if file_info:
                        processed_attachments.append({
                            'id': file_info.id,
                            'name': file_info.original_name,
                            'type': file_info.content_type,
                            'size': file_info.size,
                            'url': file_info.url
                        })
                except Exception as e:
                    logger.error(f"Failed to process attachment {attachment.get('name', 'unknown')}: {e}")
                    continue
            
            from ..core.channels import MessageType
            from ..core.tasks import parse_task_blocks, derive_task_id
            from ..core.objectives import parse_objective_blocks, derive_objective_id
            from ..core.signals import parse_signal_blocks, derive_signal_id
            message_type = MessageType.FILE if processed_attachments else MessageType.TEXT
            
            logger.info(f"Calling channel_manager.send_message: channel_id={channel_id}, user_id={user_id}, message_type={message_type}")
            message = channel_manager.send_message(
                channel_id, user_id, content, message_type,
                parent_message_id=parent_message_id,
                attachments=processed_attachments,
                security=security_clean,
                expires_at=expires_at,
                ttl_seconds=ttl_seconds,
                ttl_mode=ttl_mode,
                origin_peer=p2p_manager.get_peer_id() if p2p_manager else None,
            )
            
            if message:
                logger.info(f"Message sent successfully: {message.id}")
                # Inline circle creation from [circle] blocks
                try:
                    circle_manager = current_app.config.get('CIRCLE_MANAGER')
                    if circle_manager:
                        from ..core.circles import parse_circle_blocks, derive_circle_id
                        circle_specs = parse_circle_blocks(content or '')
                        if circle_specs:
                            # Determine channel privacy for circle visibility
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
                                        author_id=user_id,
                                    )
                                if not facilitator_id:
                                    facilitator_id = user_id
                                if spec.participants is not None:
                                    resolved_participants = _resolve_handle_list(
                                        db_manager,
                                        spec.participants,
                                        channel_id=channel_id,
                                        author_id=user_id,
                                    )
                                    spec.participants = resolved_participants

                                circle_manager.upsert_circle(
                                    circle_id=circle_id,
                                    source_type='channel',
                                    source_id=message.id,
                                    created_by=user_id,
                                    spec=spec,
                                    channel_id=channel_id,
                                    facilitator_id=facilitator_id,
                                    visibility=circle_visibility,
                                    origin_peer=p2p_manager.get_peer_id() if p2p_manager else None,
                                    created_at=message.created_at.isoformat() if getattr(message, 'created_at', None) else None,
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
                            if _is_admin():
                                try:
                                    admin_id = db_manager.get_instance_owner_user_id()
                                except Exception:
                                    admin_id = None
                            for resp in responses:
                                topic = (resp.get('topic') or '').strip()
                                body = (resp.get('content') or '').strip()
                                if not topic or not body:
                                    continue
                                circle = circle_manager.find_circle_by_topic(topic, channel_id=channel_id)
                                if not circle:
                                    continue
                                entry, err = circle_manager.add_entry(
                                    circle_id=circle.id,
                                    user_id=user_id,
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
                                        display_name = None
                                        if profile_manager:
                                            prof = profile_manager.get_profile(user_id)
                                            if prof:
                                                display_name = prof.display_name or prof.username
                                        p2p_manager.broadcast_interaction(
                                            item_id=entry['id'],
                                            user_id=user_id,
                                            action='circle_entry',
                                            item_type='circle_entry',
                                            display_name=display_name,
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
                            # Determine channel privacy for task visibility
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
                                    author_id=user_id,
                                )
                                editor_ids = _resolve_handle_list(
                                    db_manager,
                                    spec.editors or [],
                                    channel_id=channel_id,
                                    author_id=user_id,
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
                                    created_by=user_id,
                                    assigned_to=assignee_id,
                                    due_at=spec.due_at.isoformat() if spec.due_at else None,
                                    visibility=task_visibility,
                                    metadata=meta_payload,
                                    origin_peer=p2p_manager.get_peer_id() if p2p_manager else None,
                                    source_type='human',
                                    updated_by=user_id,
                                )

                                if task and task.visibility == 'network' and p2p_manager and p2p_manager.is_running():
                                    try:
                                        sender_display = None
                                        if profile_manager:
                                            sender_profile = profile_manager.get_profile(user_id)
                                            if sender_profile:
                                                sender_display = sender_profile.display_name or sender_profile.username
                                        p2p_manager.broadcast_interaction(
                                            item_id=task.id,
                                            user_id=user_id,
                                            action='task_create',
                                            item_type='task',
                                            display_name=sender_display,
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
                        objective_specs = parse_objective_blocks(content or '')
                        if objective_specs:
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

                            for idx, spec in enumerate(cast(Any, objective_specs)):
                                spec = cast(Any, spec)
                                objective_id = derive_objective_id('channel', message.id, idx, len(objective_specs), override=spec.objective_id)
                                members_payload = []
                                for member in spec.members or []:
                                    uid = _resolve_handle_to_user_id(
                                        db_manager,
                                        member.handle,
                                        channel_id=channel_id,
                                        author_id=user_id,
                                    )
                                    if uid:
                                        members_payload.append({'user_id': uid, 'role': member.role})
                                tasks_payload = []
                                for t in spec.tasks or []:
                                    assignee_id = None
                                    if t.assignee:
                                        assignee_id = _resolve_handle_to_user_id(
                                            db_manager,
                                            t.assignee,
                                            channel_id=channel_id,
                                            author_id=user_id,
                                        )
                                    tasks_payload.append({
                                        'title': t.title,
                                        'status': t.status,
                                        'assigned_to': assignee_id,
                                        'metadata': {
                                            'inline_objective_task': True,
                                            'source_type': 'channel_message',
                                            'source_id': message.id,
                                            'channel_id': channel_id,
                                        },
                                    })
                                objective_manager.upsert_objective(
                                    objective_id=objective_id,
                                    title=spec.title,
                                    description=spec.description,
                                    status=spec.status,
                                    deadline=spec.deadline.isoformat() if spec.deadline else None,
                                    created_by=user_id,
                                    visibility=obj_visibility,
                                    origin_peer=p2p_manager.get_peer_id() if p2p_manager else None,
                                    source_type='channel_message',
                                    source_id=message.id,
                                    created_at=message.created_at.isoformat() if getattr(message, 'created_at', None) else None,
                                    members=members_payload,
                                    tasks=tasks_payload,
                                    updated_by=user_id,
                                )
                except Exception as obj_err:
                    logger.warning(f"Inline objective creation failed: {obj_err}")

                # Inline request creation from [request] blocks
                try:
                    request_manager = current_app.config.get('REQUEST_MANAGER')
                    if request_manager:
                        from ..core.requests import parse_request_blocks, derive_request_id
                        request_specs = parse_request_blocks(content or '')
                        if request_specs:
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

                            for idx, spec in enumerate(cast(Any, request_specs)):
                                spec = cast(Any, spec)
                                if not spec.confirmed:
                                    continue
                                request_id = derive_request_id('channel', message.id, idx, len(request_specs), override=spec.request_id)
                                members_payload = []
                                for member in spec.members or []:
                                    uid = _resolve_handle_to_user_id(
                                        db_manager,
                                        member.handle,
                                        channel_id=channel_id,
                                        author_id=user_id,
                                    )
                                    if uid:
                                        members_payload.append({'user_id': uid, 'role': member.role})

                                request_manager.upsert_request(
                                    request_id=request_id,
                                    title=spec.title,
                                    created_by=user_id,
                                    request_text=spec.request,
                                    required_output=spec.required_output,
                                    status=spec.status,
                                    priority=spec.priority,
                                    tags=spec.tags,
                                    due_at=spec.due_at.isoformat() if spec.due_at else None,
                                    visibility=req_visibility,
                                    origin_peer=p2p_manager.get_peer_id() if p2p_manager else None,
                                    source_type='channel_message',
                                    source_id=message.id,
                                    created_at=message.created_at.isoformat() if getattr(message, 'created_at', None) else None,
                                    actor_id=user_id,
                                    members=members_payload,
                                    members_defined=('members' in spec.fields),
                                    fields=spec.fields,
                                )
                except Exception as req_err:
                    logger.warning(f"Inline request creation failed: {req_err}")

                # Inline signal creation from [signal] blocks
                try:
                    signal_manager = current_app.config.get('SIGNAL_MANAGER')
                    if signal_manager:
                        signal_specs = parse_signal_blocks(content or '')
                        if signal_specs:
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

                            for idx, spec in enumerate(cast(Any, signal_specs)):
                                spec = cast(Any, spec)
                                signal_id = derive_signal_id('channel', message.id, idx, len(signal_specs), override=spec.signal_id)
                                owner_id = None
                                if spec.owner:
                                    owner_id = _resolve_handle_to_user_id(
                                        db_manager,
                                        spec.owner,
                                        channel_id=channel_id,
                                        author_id=user_id,
                                    )
                                if not owner_id:
                                    owner_id = user_id

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
                                    created_by=user_id,
                                    visibility=sig_visibility,
                                    origin_peer=p2p_manager.get_peer_id() if p2p_manager else None,
                                    source_type='channel_message',
                                    source_id=message.id,
                                    expires_at=spec.expires_at.isoformat() if spec.expires_at else None,
                                    ttl_seconds=spec.ttl_seconds,
                                    ttl_mode=spec.ttl_mode,
                                    created_at=message.created_at.isoformat() if getattr(message, 'created_at', None) else None,
                                    actor_id=user_id,
                                )
                except Exception as sig_err:
                    logger.warning(f"Inline signal creation failed: {sig_err}")

                # Inline contract creation from [contract] blocks
                try:
                    contract_manager = current_app.config.get('CONTRACT_MANAGER')
                    if contract_manager:
                        from ..core.contracts import parse_contract_blocks, derive_contract_id
                        contract_specs = parse_contract_blocks(content or '')
                        if contract_specs:
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

                            for idx, spec in enumerate(contract_specs):
                                if not spec.confirmed:
                                    continue
                                contract_id = derive_contract_id('channel', message.id, idx, len(contract_specs), override=spec.contract_id)
                                owner_id = None
                                if spec.owner:
                                    owner_id = _resolve_handle_to_user_id(
                                        db_manager,
                                        spec.owner,
                                        channel_id=channel_id,
                                        author_id=user_id,
                                    )
                                if not owner_id:
                                    owner_id = user_id

                                counterparties = []
                                for cp in spec.counterparties or []:
                                    cp_id = _resolve_handle_to_user_id(
                                        db_manager,
                                        cp,
                                        channel_id=channel_id,
                                        author_id=user_id,
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
                                    created_by=user_id,
                                    visibility=contract_visibility,
                                    origin_peer=p2p_manager.get_peer_id() if p2p_manager else None,
                                    source_type='channel_message',
                                    source_id=message.id,
                                    expires_at=spec.expires_at.isoformat() if spec.expires_at else None,
                                    ttl_seconds=spec.ttl_seconds,
                                    ttl_mode=spec.ttl_mode,
                                    metadata=spec.metadata,
                                    created_at=message.created_at.isoformat() if getattr(message, 'created_at', None) else None,
                                    actor_id=user_id,
                                )
                except Exception as contract_err:
                    logger.warning(f"Inline contract creation failed: {contract_err}")

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
                                author_id=user_id,
                            )
                except Exception as skill_err:
                    logger.warning(f"Inline skill registration failed: {skill_err}")

                # Broadcast to connected P2P peers so they store it too
                if p2p_manager and p2p_manager.is_running():
                    try:
                        # Resolve display_name so remote peers show the
                        # correct sender name even without a full profile sync.
                        sender_display = None
                        try:
                            sender_profile = profile_manager.get_profile(user_id)
                            if sender_profile:
                                sender_display = sender_profile.display_name or sender_profile.username
                        except Exception:
                            pass
                        # For private/confidential channels, use targeted peer sends.
                        _tgt_peers = None
                        _channel_mode = 'open'
                        try:
                            with db_manager.get_connection() as _conn:
                                _pm_row = _conn.execute(
                                    "SELECT privacy_mode FROM channels WHERE id = ?",
                                    (channel_id,)).fetchone()
                            if _pm_row:
                                _channel_mode = (_pm_row['privacy_mode'] or 'open').lower()
                            if _channel_mode in {'private', 'confidential'}:
                                _local_p = p2p_manager.get_peer_id() if p2p_manager else None
                                _tgt_peers = channel_manager.get_member_peer_ids(
                                    channel_id, _local_p)
                        except Exception:
                            pass
                        _security_payload = dict(security_clean or {})
                        _security_payload['privacy_mode'] = _channel_mode
                        p2p_manager.broadcast_channel_message(
                            channel_id=channel_id,
                            user_id=user_id,
                            content=content,
                            message_id=message.id,
                            timestamp=message.created_at.isoformat() if hasattr(message.created_at, 'isoformat') else str(message.created_at),
                            attachments=message.attachments if hasattr(message, 'attachments') and message.attachments else None,
                            display_name=sender_display,
                            expires_at=message.expires_at.isoformat() if getattr(message, 'expires_at', None) else None,
                            ttl_seconds=ttl_seconds,
                            ttl_mode=ttl_mode,
                            parent_message_id=getattr(message, 'parent_message_id', None),
                            security=_security_payload,
                            target_peer_ids=_tgt_peers,
                        )
                    except Exception as bcast_err:
                        logger.warning(f"P2P broadcast of channel message failed (non-fatal): {bcast_err}")

                local_mentioned_user_ids: list[str] = []

                # Emit mention events for @handles
                try:
                    mention_manager = current_app.config.get('MENTION_MANAGER')
                    mentions = extract_mentions(content or '')
                    if mention_manager and mentions:
                        targets = resolve_mention_targets(
                            db_manager,
                            mentions,
                            channel_id=channel_id,
                            author_id=user_id,
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
                                author_id=user_id,
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
                                author_id=user_id,
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
                            author_id=user_id,
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
                })
            else:
                logger.error(f"Failed to send message to channel {channel_id}")
                return jsonify({'error': 'Failed to send message'}), 500
                
        except Exception as e:
            logger.error(f"Send channel message error: {e}")
            return jsonify({'error': 'Internal server error'}), 500

    @ui.route('/ajax/channel_threads/subscription', methods=['GET', 'POST'])
    @require_login
    def ajax_channel_thread_subscription():
        """Get or update per-thread inbox subscription state for current user."""
        try:
            _, _, _, _, channel_manager, _, _, _, _, _, _ = _get_app_components_any(current_app)
            inbox_manager = current_app.config.get('INBOX_MANAGER')
            user_id = get_current_user()

            payload = request.get_json(silent=True) or {}
            if request.method == 'GET':
                channel_id = str(request.args.get('channel_id') or '').strip()
                message_id = str(request.args.get('message_id') or '').strip()
            else:
                channel_id = str(payload.get('channel_id') or '').strip()
                message_id = str(payload.get('message_id') or '').strip()

            if not channel_id or not message_id:
                return jsonify({'error': 'channel_id and message_id are required'}), 400

            access = channel_manager.get_channel_access_decision(
                channel_id=channel_id,
                user_id=user_id,
                require_membership=True,
            )
            if not access.get('allowed'):
                return jsonify({'error': 'You are not a member of this channel'}), 403

            state = channel_manager.get_thread_subscription_state(user_id, channel_id, message_id)
            root_id = state.get('thread_root_message_id')
            if not root_id:
                return jsonify({'error': 'Thread not found'}), 404

            explicit = state.get('explicit_subscribed')
            auto_subscribe = True
            if inbox_manager:
                try:
                    cfg = inbox_manager.get_config(user_id)
                    auto_subscribe = bool(cfg.get('auto_subscribe_own_threads', True))
                except Exception:
                    auto_subscribe = True
            effective = bool(explicit) if explicit is not None else bool(state.get('is_root_author') and auto_subscribe)

            if request.method == 'POST':
                subscribed_raw = payload.get('subscribed')
                if subscribed_raw is None:
                    subscribed = not effective
                elif isinstance(subscribed_raw, bool):
                    subscribed = subscribed_raw
                else:
                    subscribed = str(subscribed_raw).strip().lower() in {'1', 'true', 'yes', 'on'}

                update = channel_manager.set_thread_subscription(
                    user_id=user_id,
                    channel_id=channel_id,
                    message_id=message_id,
                    subscribed=subscribed,
                    source='manual',
                )
                if not update.get('success'):
                    return jsonify({'error': 'Failed to update thread subscription'}), 500

                state = channel_manager.get_thread_subscription_state(user_id, channel_id, message_id)
                explicit = state.get('explicit_subscribed')
                effective = bool(explicit) if explicit is not None else bool(state.get('is_root_author') and auto_subscribe)

            return jsonify({
                'success': True,
                'channel_id': channel_id,
                'message_id': message_id,
                'thread_root_message_id': state.get('thread_root_message_id'),
                'root_author_id': state.get('root_author_id'),
                'is_root_author': bool(state.get('is_root_author')),
                'explicit_subscribed': explicit,
                'auto_subscribe_own_threads': auto_subscribe,
                'subscribed': effective,
            })
        except Exception as e:
            logger.error(f"Channel thread subscription error: {e}", exc_info=True)
            return jsonify({'error': 'Internal server error'}), 500

    @ui.route('/ajax/delete_channel_message', methods=['POST'])
    @require_login
    def ajax_delete_channel_message():
        """AJAX endpoint to delete a channel message (own messages only)."""
        try:
            db_manager, _, _, _, channel_manager, _, _, _, _, _, p2p_manager = _get_app_components_any(current_app)
            user_id = get_current_user()
            
            data = request.get_json(silent=True) or {}
            message_id = data.get('message_id')
            
            if not message_id:
                return jsonify({'error': 'Message ID required'}), 400
            
            # Verify ownership: only allow deleting own messages
            with db_manager.get_connection() as conn:
                msg = conn.execute(
                    "SELECT user_id FROM channel_messages WHERE id = ?",
                    (message_id,)
                ).fetchone()
                
                if not msg:
                    return jsonify({'error': 'Message not found'}), 404
                if msg['user_id'] != user_id:
                    return jsonify({'error': 'You can only delete your own messages'}), 403
                
                conn.execute("DELETE FROM channel_messages WHERE id = ?", (message_id,))
                conn.commit()
            
            # Broadcast delete signal via P2P
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
            logger.error(f"Delete channel message error: {e}")
            return jsonify({'error': 'Internal server error'}), 500

    @ui.route('/ajax/update_channel_privacy', methods=['POST'])
    @require_login
    def ajax_update_channel_privacy():
        """Update a channel's privacy mode (admin only)."""
        try:
            db_manager, _, _, _, channel_manager, _, _, _, _, _, p2p_manager = _get_app_components_any(current_app)
            user_id = get_current_user()
            data = request.get_json() or {}
            channel_id = data.get('channel_id')
            privacy_mode = (data.get('privacy_mode') or '').strip().lower()

            if not channel_id:
                return jsonify({'error': 'Channel ID required'}), 400
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

            # Enforce origin-only privacy changes before DB update
            try:
                with db_manager.get_connection() as conn:
                    row = conn.execute(
                        "SELECT origin_peer FROM channels WHERE id = ?",
                        (channel_id,)
                    ).fetchone()
                origin_peer = None
                if row:
                    try:
                        origin_peer = row['origin_peer']
                    except Exception:
                        origin_peer = row[0]
                is_origin_local = not origin_peer or (local_peer_id and origin_peer == local_peer_id)
                if not is_origin_local:
                    return jsonify({'error': 'Only the channel origin can change privacy'}), 403
            except Exception:
                pass

            success = channel_manager.update_channel_privacy(
                channel_id=channel_id,
                user_id=user_id,
                privacy_mode=privacy_mode,
                allow_admin=_is_admin(),
                local_peer_id=local_peer_id,
            )
            if not success:
                return jsonify({'error': 'Not authorized to update privacy'}), 403

            # Broadcast a channel announce update so peers can sync metadata
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
            logger.error(f"Update channel privacy error: {e}", exc_info=True)
            return jsonify({'error': 'Internal server error'}), 500

    @ui.route('/ajax/update_channel_notifications', methods=['POST'])
    @require_login
    def ajax_update_channel_notifications():
        """Enable/disable notifications for a channel (per user)."""
        try:
            _, _, _, _, channel_manager, _, _, _, _, _, _ = _get_app_components_any(current_app)
            user_id = get_current_user()
            data = request.get_json() or {}
            channel_id = data.get('channel_id')
            enabled = data.get('enabled')

            if not channel_id:
                return jsonify({'error': 'Channel ID required'}), 400
            enabled_flag = bool(enabled) if isinstance(enabled, (bool, int)) else str(enabled).lower() in ('1', 'true', 'yes')

            success = channel_manager.update_channel_notifications(
                channel_id=channel_id,
                user_id=user_id,
                enabled=enabled_flag,
            )
            if not success:
                return jsonify({'error': 'Not authorized to update notifications'}), 403

            return jsonify({'success': True, 'enabled': enabled_flag})
        except Exception as e:
            logger.error(f"Update channel notifications error: {e}", exc_info=True)
            return jsonify({'error': 'Internal server error'}), 500

    @ui.route('/ajax/update_channel_message', methods=['POST'])
    @require_login
    def ajax_update_channel_message():
        """AJAX endpoint to update a channel message (own messages only)."""
        try:
            db_manager, _, _, _, channel_manager, file_manager, _, interaction_manager, profile_manager, _, p2p_manager = _get_app_components_any(current_app)
            user_id = get_current_user()
            from ..core.polls import parse_poll, poll_edit_lock_reason

            data = request.get_json() or {}
            message_id = data.get('message_id')
            content = (data.get('content') or '').strip()
            attachments = data.get('attachments')
            new_attachments = data.get('new_attachments') or []

            if not message_id:
                return jsonify({'error': 'Message ID required'}), 400
            if not content and not attachments and not new_attachments:
                return jsonify({'error': 'Message content or attachments required'}), 400

            # Fetch existing message for ownership + channel info
            with db_manager.get_connection() as conn:
                row = conn.execute(
                    "SELECT id, channel_id, user_id, content, created_at, attachments, expires_at, ttl_seconds, ttl_mode, parent_message_id "
                    "FROM channel_messages WHERE id = ?",
                    (message_id,)
                ).fetchone()
            if not row:
                return jsonify({'error': 'Message not found'}), 404
            if row['user_id'] != user_id:
                return jsonify({'error': 'You can only edit your own messages'}), 403

            existing_poll = parse_poll(row['content'] or '')
            new_poll = parse_poll(content or '') if content is not None else None
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

            # Process new file attachments
            processed_new_attachments = []
            for attachment in new_attachments:
                try:
                    file_data = base64.b64decode(attachment['data'])
                    file_info = file_manager.save_file(
                        file_data,
                        attachment['name'],
                        attachment['type'],
                        user_id
                    )
                    if file_info:
                        processed_new_attachments.append({
                            'id': file_info.id,
                            'name': file_info.original_name,
                            'type': file_info.content_type,
                            'size': file_info.size,
                            'url': file_info.url
                        })
                except Exception as e:
                    logger.error(f"Failed to process new attachment {attachment.get('name', 'unknown')}: {e}")
                    continue

            # Merge existing attachments (from request or DB) with new
            if attachments is None:
                existing_atts = []
                if row['attachments']:
                    try:
                        existing_atts = json.loads(row['attachments'])
                    except Exception:
                        existing_atts = []
                final_attachments = existing_atts
            else:
                final_attachments = list(attachments) if isinstance(attachments, list) else []
            final_attachments.extend(processed_new_attachments)

            success = channel_manager.update_message(
                message_id=message_id,
                user_id=user_id,
                content=content,
                attachments=final_attachments if final_attachments else None,
                allow_admin=False,
            )

            if success:
                # Sync inline circles from edited channel message
                try:
                    circle_manager = current_app.config.get('CIRCLE_MANAGER')
                    if circle_manager:
                        from ..core.circles import parse_circle_blocks, derive_circle_id
                        circle_specs = parse_circle_blocks(content or '')
                        if circle_specs:
                            visibility = 'network'
                            try:
                                with db_manager.get_connection() as conn:
                                    prow = conn.execute(
                                        "SELECT privacy_mode FROM channels WHERE id = ?",
                                        (row['channel_id'],)
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
                                        channel_id=row['channel_id'],
                                        author_id=user_id,
                                    )
                                if not facilitator_id:
                                    facilitator_id = user_id
                                if spec.participants is not None:
                                    resolved_participants = _resolve_handle_list(
                                        db_manager,
                                        spec.participants,
                                        channel_id=row['channel_id'],
                                        author_id=user_id,
                                    )
                                    spec.participants = resolved_participants

                                circle_manager.upsert_circle(
                                    circle_id=circle_id,
                                    source_type='channel',
                                    source_id=message_id,
                                    created_by=user_id,
                                    spec=spec,
                                    channel_id=row['channel_id'],
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
                                    (row['channel_id'],)
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
                            'channel_id': row['channel_id'],
                        }
                        _sync_inline_tasks_from_content(
                            task_manager=task_manager,
                            db_manager=db_manager,
                            content=content,
                            scope='channel',
                            source_id=message_id,
                            actor_id=user_id,
                            task_visibility=task_visibility,
                            base_metadata=base_meta,
                            channel_id=row['channel_id'],
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
                                    (row['channel_id'],)
                                ).fetchone()
                            if prow:
                                privacy_mode = prow['privacy_mode']
                        except Exception:
                            privacy_mode = None
                        objective_visibility = 'local' if privacy_mode and privacy_mode != 'open' else 'network'
                        origin_peer = None
                        if p2p_manager:
                            try:
                                origin_peer = p2p_manager.get_peer_id()
                            except Exception:
                                origin_peer = None
                        _sync_inline_objectives_from_content(
                            objective_manager=objective_manager,
                            db_manager=db_manager,
                            content=content,
                            scope='channel',
                            source_id=message_id,
                            actor_id=user_id,
                            objective_visibility=objective_visibility,
                            source_type='channel_message',
                            origin_peer=origin_peer,
                            created_at=row['created_at'],
                            channel_id=row['channel_id'],
                        )
                except Exception as obj_err:
                    logger.warning(f"Inline objective sync failed on channel edit: {obj_err}")

                # Sync inline requests from edited channel message
                try:
                    request_manager = current_app.config.get('REQUEST_MANAGER')
                    if request_manager:
                        from ..core.requests import parse_request_blocks, derive_request_id
                        request_specs = parse_request_blocks(content or '')
                        if request_specs:
                            privacy_mode = None
                            try:
                                with db_manager.get_connection() as conn:
                                    prow = conn.execute(
                                        "SELECT privacy_mode FROM channels WHERE id = ?",
                                        (row['channel_id'],)
                                    ).fetchone()
                                if prow:
                                    privacy_mode = prow['privacy_mode']
                            except Exception:
                                privacy_mode = None
                            req_visibility = 'local' if privacy_mode and privacy_mode != 'open' else 'network'

                            for idx, spec in enumerate(cast(Any, request_specs)):
                                spec = cast(Any, spec)
                                if not spec.confirmed:
                                    continue
                                request_id = derive_request_id('channel', message_id, idx, len(request_specs), override=spec.request_id)
                                members_payload = []
                                for member in spec.members or []:
                                    uid = _resolve_handle_to_user_id(
                                        db_manager,
                                        member.handle,
                                        channel_id=row['channel_id'],
                                        author_id=user_id,
                                    )
                                    if uid:
                                        members_payload.append({'user_id': uid, 'role': member.role})

                                request_manager.upsert_request(
                                    request_id=request_id,
                                    title=spec.title,
                                    created_by=user_id,
                                    request_text=spec.request,
                                    required_output=spec.required_output,
                                    status=spec.status,
                                    priority=spec.priority,
                                    tags=spec.tags,
                                    due_at=spec.due_at.isoformat() if spec.due_at else None,
                                    visibility=req_visibility,
                                    origin_peer=p2p_manager.get_peer_id() if p2p_manager else None,
                                    source_type='channel_message',
                                    source_id=message_id,
                                    created_at=row['created_at'],
                                    actor_id=user_id,
                                    members=members_payload,
                                    members_defined=('members' in spec.fields),
                                    fields=spec.fields,
                                )
                except Exception as req_err:
                    logger.warning(f"Inline request sync failed on channel edit: {req_err}")

                # Sync inline contracts from edited channel message
                try:
                    contract_manager = current_app.config.get('CONTRACT_MANAGER')
                    if contract_manager:
                        privacy_mode = None
                        try:
                            with db_manager.get_connection() as conn:
                                prow = conn.execute(
                                    "SELECT privacy_mode FROM channels WHERE id = ?",
                                    (row['channel_id'],)
                                ).fetchone()
                            if prow:
                                privacy_mode = prow['privacy_mode']
                        except Exception:
                            privacy_mode = None
                        contract_visibility = 'local' if privacy_mode and privacy_mode != 'open' else 'network'
                        _sync_inline_contracts_from_content(
                            contract_manager=contract_manager,
                            db_manager=db_manager,
                            content=content,
                            scope='channel',
                            source_id=message_id,
                            actor_id=user_id,
                            contract_visibility=contract_visibility,
                            source_type='channel_message',
                            origin_peer=p2p_manager.get_peer_id() if p2p_manager else None,
                            created_at=row['created_at'],
                            channel_id=row['channel_id'],
                        )
                except Exception as contract_err:
                    logger.warning(f"Inline contract sync failed on channel edit: {contract_err}")

                try:
                    sync_edited_mention_activity(
                        db_manager=db_manager,
                        mention_manager=current_app.config.get('MENTION_MANAGER'),
                        inbox_manager=current_app.config.get('INBOX_MANAGER'),
                        p2p_manager=p2p_manager,
                        content=content,
                        source_type='channel_message',
                        source_id=message_id,
                        author_id=user_id,
                        origin_peer=p2p_manager.get_peer_id() if p2p_manager else None,
                        channel_id=row['channel_id'],
                        edited_at=final_metadata.get('edited_at') if isinstance(final_metadata, dict) else None,
                    )
                    inbox_manager = current_app.config.get('INBOX_MANAGER')
                    if inbox_manager:
                        inbox_manager.sync_source_triggers(
                            source_type='channel_message',
                            source_id=message_id,
                            trigger_type='reply',
                            sender_user_id=user_id,
                            origin_peer=p2p_manager.get_peer_id() if p2p_manager else None,
                            channel_id=row['channel_id'],
                            preview=build_preview(content or '') or None,
                            payload={
                                'channel_id': row['channel_id'],
                                'message_id': message_id,
                                'parent_message_id': row['parent_message_id'],
                                'edited_at': final_metadata.get('edited_at') if isinstance(final_metadata, dict) else None,
                            },
                            message_id=message_id,
                            source_content=content,
                        )
                except Exception as mention_sync_err:
                    logger.warning(f"Channel mention/reply refresh failed on channel edit: {mention_sync_err}")

                if p2p_manager and p2p_manager.is_running():
                    try:
                        display_name = None
                        channel_mode = 'open'
                        target_peer_ids = None
                        try:
                            with db_manager.get_connection() as conn:
                                mode_row = conn.execute(
                                    "SELECT privacy_mode FROM channels WHERE id = ?",
                                    (row['channel_id'],)
                                ).fetchone()
                            if mode_row:
                                channel_mode = (mode_row['privacy_mode'] or 'open').lower()
                            if channel_mode in {'private', 'confidential'}:
                                local_peer = p2p_manager.get_peer_id() if p2p_manager else None
                                target_peer_ids = channel_manager.get_member_peer_ids(
                                    row['channel_id'], local_peer
                                )
                        except Exception:
                            target_peer_ids = None
                        if profile_manager:
                            profile = profile_manager.get_profile(user_id)
                            if profile:
                                display_name = profile.display_name or profile.username
                        edited_at = datetime.now(timezone.utc).isoformat()
                        p2p_manager.broadcast_channel_message(
                            channel_id=row['channel_id'],
                            user_id=row['user_id'],
                            content=content,
                            message_id=message_id,
                            timestamp=str(row['created_at']),
                            attachments=final_attachments if final_attachments else None,
                            display_name=display_name,
                            expires_at=row['expires_at'],
                            ttl_seconds=row['ttl_seconds'],
                            ttl_mode=row['ttl_mode'],
                            update_only=True,
                            parent_message_id=row['parent_message_id'],
                            edited_at=edited_at,
                            target_peer_ids=target_peer_ids,
                            security={'privacy_mode': channel_mode},
                        )
                    except Exception as bcast_err:
                        logger.warning(f"Failed to broadcast channel message update: {bcast_err}")
                return jsonify({'success': True})

            return jsonify({'error': 'Failed to update message'}), 500
        except Exception as e:
            logger.error(f"Update channel message error: {e}", exc_info=True)
            return jsonify({'error': 'Internal server error'}), 500

    @ui.route('/ajax/update_channel_message_expiry', methods=['POST'])
    @require_login
    def ajax_update_channel_message_expiry():
        """AJAX endpoint to update a channel message expiry."""
        try:
            db_manager, _, _, _, channel_manager, _, _, _, profile_manager, _, p2p_manager = _get_app_components_any(current_app)
            user_id = get_current_user()

            data = request.get_json() or {}
            message_id = data.get('message_id')
            ttl_mode = data.get('ttl_mode')
            ttl_seconds = data.get('ttl_seconds')
            expires_at = data.get('expires_at')

            if not message_id:
                return jsonify({'error': 'Message ID required'}), 400

            expires_dt = channel_manager.update_message_expiry(
                message_id=message_id,
                user_id=user_id,
                expires_at=expires_at,
                ttl_seconds=ttl_seconds,
                ttl_mode=ttl_mode,
                allow_admin=False,
            )
            if expires_dt is None and ttl_mode not in ('none', 'no_expiry', 'immortal'):
                return jsonify({'error': 'Not authorized or invalid expiry'}), 403

            # Broadcast expiry update to peers (best-effort)
            if p2p_manager and p2p_manager.is_running():
                try:
                    row = None
                    with db_manager.get_connection() as conn:
                        row = conn.execute("""
                            SELECT channel_id, user_id, content, message_type,
                                   attachments, created_at
                            FROM channel_messages
                            WHERE id = ?
                        """, (message_id,)).fetchone()
                    if row:
                        display_name = None
                        channel_mode = 'open'
                        target_peer_ids = None
                        try:
                            with db_manager.get_connection() as conn:
                                mode_row = conn.execute(
                                    "SELECT privacy_mode FROM channels WHERE id = ?",
                                    (row['channel_id'],)
                                ).fetchone()
                            if mode_row:
                                channel_mode = (mode_row['privacy_mode'] or 'open').lower()
                            if channel_mode in {'private', 'confidential'}:
                                local_peer = p2p_manager.get_peer_id() if p2p_manager else None
                                target_peer_ids = channel_manager.get_member_peer_ids(
                                    row['channel_id'], local_peer
                                )
                        except Exception:
                            target_peer_ids = None
                        try:
                            profile = profile_manager.get_profile(user_id)
                            if profile:
                                display_name = profile.display_name or profile.username
                        except Exception:
                            pass
                        attachments = None
                        if row['attachments']:
                            try:
                                attachments = json.loads(row['attachments'])
                            except Exception:
                                attachments = None
                        p2p_manager.broadcast_channel_message(
                            channel_id=row['channel_id'],
                            user_id=row['user_id'],
                            content=row['content'] or '',
                            message_id=message_id,
                            timestamp=str(row['created_at']),
                            attachments=attachments,
                            display_name=display_name,
                            expires_at=expires_dt.isoformat() if expires_dt else None,
                            ttl_seconds=ttl_seconds,
                            ttl_mode=ttl_mode,
                            update_only=True,
                            target_peer_ids=target_peer_ids,
                            security={'privacy_mode': channel_mode},
                        )
                except Exception as bcast_err:
                    logger.warning(f"Failed to broadcast channel expiry update: {bcast_err}")

            return jsonify({
                'success': True,
                'expires_at': expires_dt.isoformat() if expires_dt else None,
            })
        except Exception as e:
            logger.error(f"Update channel message expiry error: {e}")
            return jsonify({'error': 'Internal server error'}), 500

    def _e2e_private_enabled() -> bool:
        cfg = current_app.config.get('CANOPY_CONFIG')
        sec = getattr(cfg, 'security', None)
        return bool(getattr(sec, 'e2e_private_channels', False))

    def _normalize_channel_crypto_mode(raw_mode: Any) -> str:
        mode = str(raw_mode or '').strip().lower()
        if mode in {'e2e_optional', 'e2e_enforced', 'legacy_plaintext'}:
            return mode
        return 'legacy_plaintext'

    def _channel_targets_e2e(privacy_mode: str, crypto_mode: str) -> bool:
        return (
            str(privacy_mode or '').strip().lower() in {'private', 'confidential'}
            and str(crypto_mode or '').strip().lower() in {'e2e_optional', 'e2e_enforced'}
        )

    def _ensure_channel_key_material(channel_manager: Any, channel_id: str,
                                     origin_peer: Optional[str], rotated_from: Optional[str] = None) -> Optional[dict[str, Any]]:
        """Ensure one active local key exists and return it as {'key_id','key_material','metadata'}."""
        active = channel_manager.get_active_channel_key(channel_id)
        if active:
            key_bytes = channel_manager.decode_channel_key_material(active.get('key_material_enc'))
            if key_bytes:
                return {
                    'key_id': active.get('key_id'),
                    'key_material': key_bytes,
                    'metadata': active.get('metadata') or {},
                }

        key_bytes = secrets.token_bytes(32)
        key_id = f"K{secrets.token_hex(8)}"
        metadata = {
            'algorithm': 'chacha20poly1305',
            'key_version': 1,
            'rotated_from': rotated_from,
            'generated_at': datetime.now(timezone.utc).isoformat(),
        }
        stored = channel_manager.upsert_channel_key(
            channel_id=channel_id,
            key_id=key_id,
            key_material_enc=encode_channel_key_material(key_bytes),
            created_by_peer=origin_peer,
            metadata=metadata,
        )
        if not stored:
            return None
        return {'key_id': key_id, 'key_material': key_bytes, 'metadata': metadata}

    def _rotate_channel_key_material(channel_manager: Any, channel_id: str,
                                     origin_peer: Optional[str], rotated_from: Optional[str]) -> Optional[dict[str, Any]]:
        """Create a brand-new channel key for rotation workflows."""
        key_bytes = secrets.token_bytes(32)
        key_id = f"K{secrets.token_hex(8)}"
        previous = rotated_from or None
        metadata = {
            'algorithm': 'chacha20poly1305',
            'key_version': 1,
            'rotated_from': previous,
            'generated_at': datetime.now(timezone.utc).isoformat(),
        }
        stored = channel_manager.upsert_channel_key(
            channel_id=channel_id,
            key_id=key_id,
            key_material_enc=encode_channel_key_material(key_bytes),
            created_by_peer=origin_peer,
            metadata=metadata,
        )
        if not stored:
            return None
        if previous:
            channel_manager.revoke_channel_key(channel_id, previous)
        return {'key_id': key_id, 'key_material': key_bytes, 'metadata': metadata}

    def _send_channel_key_to_peer(
        channel_manager: Any,
        p2p_mgr: Any,
        channel_id: str,
        key_payload: dict[str, Any],
        peer_id: str,
        rotated_from: Optional[str] = None,
    ) -> bool:
        if not p2p_mgr or not peer_id:
            return False
        local_peer = p2p_mgr.get_peer_id() if p2p_mgr else None
        if not local_peer or peer_id == local_peer:
            return False
        recipient_identity = p2p_mgr.identity_manager.get_peer(peer_id)
        local_identity = p2p_mgr.identity_manager.local_identity
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
        except Exception as e:
            channel_manager.upsert_channel_member_key_state(
                channel_id=channel_id,
                key_id=key_payload['key_id'],
                peer_id=peer_id,
                delivery_state='failed',
                last_error=f'wrap_failed:{e}',
            )
            return False

        channel_manager.upsert_channel_member_key_state(
            channel_id=channel_id,
            key_id=key_payload['key_id'],
            peer_id=peer_id,
            delivery_state='pending',
            last_error=None,
        )
        sent = p2p_mgr.send_channel_key_distribution(
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

    def _distribute_channel_key_to_member_peers(
        channel_manager: Any,
        p2p_mgr: Any,
        channel_id: str,
        key_payload: dict[str, Any],
        rotated_from: Optional[str] = None,
    ) -> int:
        if not p2p_mgr:
            return 0
        local_peer = p2p_mgr.get_peer_id() if p2p_mgr else None
        member_peers = channel_manager.get_member_peer_ids(channel_id, local_peer)
        sent_count = 0
        for peer_id in sorted(member_peers):
            if _send_channel_key_to_peer(
                channel_manager=channel_manager,
                p2p_mgr=p2p_mgr,
                channel_id=channel_id,
                key_payload=key_payload,
                peer_id=peer_id,
                rotated_from=rotated_from,
            ):
                sent_count += 1
        return sent_count

    @ui.route('/ajax/create_channel', methods=['POST'])
    @require_login
    def ajax_create_channel():
        """AJAX endpoint to create a new channel."""
        try:
            db_manager, _, _, _, channel_manager, _, _, _, _, _, p2p_manager = _get_app_components_any(current_app)
            user_id = get_current_user()
            
            data = request.get_json()
            logger.info(f"Create channel request: user_id={user_id}, data={data}")
            
            name = data.get('name', '').strip().lstrip('#').strip()
            description = data.get('description', '').strip()
            channel_type_str = data.get('type', 'public')
            privacy_mode = (data.get('privacy_mode') or ('private' if channel_type_str == 'private' else 'open')).strip().lower()
            requested_crypto_mode = _normalize_channel_crypto_mode(data.get('crypto_mode'))
            if privacy_mode not in {'open', 'guarded', 'private', 'confidential'}:
                return jsonify({'error': 'Invalid privacy mode'}), 400
            initial_members = data.get('initial_members') or []
            if not isinstance(initial_members, list):
                initial_members = []
            initial_members = [m for m in initial_members if m and isinstance(m, str)]
            
            logger.info(f"Parsed channel data: name='{name}', type='{channel_type_str}', description='{description}'")
            
            if not name:
                return jsonify({'error': 'Channel name required'}), 400
            
            from ..core.channels import ChannelType
            try:
                channel_type = ChannelType(channel_type_str)
            except ValueError:
                return jsonify({'error': f'Invalid channel type: {channel_type_str}'}), 400

            governance = channel_manager.get_user_channel_governance(user_id)
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
            
            logger.info(f"Calling channel_manager.create_channel: name={name}, type={channel_type}, user_id={user_id}")
            channel = channel_manager.create_channel(
                name, channel_type, user_id, description,
                initial_members=initial_members,
                privacy_mode=privacy_mode,
                origin_peer=p2p_manager.get_peer_id() if p2p_manager else None,
            )
            
            if channel:
                logger.info(f"Channel created successfully: {channel.id}")
                # Broadcast CHANNEL_ANNOUNCE to connected peers
                if p2p_manager and p2p_manager.is_running():
                    try:
                        is_priv = (channel.privacy_mode or '').lower() in {'private', 'confidential'}
                        local_peer = p2p_manager.get_peer_id() if p2p_manager else None
                        m_peer_ids = None
                        m_by_peer: Optional[dict[str, list[str]]] = None
                        if is_priv:
                            m_peer_ids = channel_manager.get_member_peer_ids(
                                channel.id, local_peer)
                            # Build initial_members_by_peer: {peer_id: [user_ids on that peer]}
                            m_by_peer = {}
                            if m_peer_ids:
                                members = channel_manager.get_channel_members_list(channel.id)
                                for m in members:
                                    uid = m.get('user_id')
                                    if uid:
                                        try:
                                            u = db_manager.get_user(uid)
                                            op = (u.get('origin_peer') or '') if u else ''
                                        except Exception:
                                            op = ''
                                        # Users with no origin_peer are local
                                        peer_key = op if op else local_peer
                                        if peer_key and peer_key in m_peer_ids:
                                            m_by_peer.setdefault(peer_key, []).append(uid)
                        p2p_manager.broadcast_channel_announce(
                            channel_id=channel.id,
                            name=channel.name,
                            channel_type=channel.channel_type.value,
                            description=channel.description or '',
                            privacy_mode=channel.privacy_mode,
                            created_by_user_id=channel.created_by,
                            member_peer_ids=m_peer_ids,
                            initial_members_by_peer=m_by_peer,
                        )
                    except Exception as ann_err:
                        logger.warning(f"P2P channel announce failed (non-fatal): {ann_err}")

                # Phase-2 E2E: targeted channels default to e2e_optional when enabled.
                try:
                    if _e2e_private_enabled() and (channel.privacy_mode or '').lower() in {'private', 'confidential'}:
                        crypto_mode = requested_crypto_mode
                        if crypto_mode == 'legacy_plaintext':
                            crypto_mode = 'e2e_optional'
                        channel_manager.set_channel_crypto_mode(channel.id, crypto_mode)
                        channel.crypto_mode = crypto_mode
                        if _channel_targets_e2e(channel.privacy_mode, crypto_mode):
                            key_payload = _ensure_channel_key_material(
                                channel_manager=channel_manager,
                                channel_id=channel.id,
                                origin_peer=(p2p_manager.get_peer_id() if p2p_manager else None),
                                rotated_from=None,
                            )
                            if key_payload and p2p_manager and p2p_manager.is_running():
                                _distribute_channel_key_to_member_peers(
                                    channel_manager=channel_manager,
                                    p2p_mgr=p2p_manager,
                                    channel_id=channel.id,
                                    key_payload=key_payload,
                                )
                except Exception as key_err:
                    logger.warning(f"E2E channel key bootstrap failed for {channel.id}: {key_err}")

                return jsonify({
                    'success': True,
                    'channel': channel.to_dict()
                })
            else:
                logger.error(f"Failed to create channel: {name}")
                return jsonify({'error': 'Failed to create channel'}), 500
                
        except Exception as e:
            logger.error(f"Create channel error: {e}")
            return jsonify({'error': 'Internal server error'}), 500

    @ui.route('/ajax/channel_members/<channel_id>', methods=['GET'])
    @require_login
    def ajax_get_channel_members(channel_id):
        """Return members for a channel (session auth)."""
        try:
            db_manager, _, _, _, channel_manager, _, _, _, _, _, _ = _get_app_components_any(current_app)
            user_id = get_current_user()
            # Ensure requester is a member
            with db_manager.get_connection() as conn:
                row = conn.execute(
                    "SELECT 1 FROM channel_members WHERE channel_id = ? AND user_id = ?",
                    (channel_id, user_id)
                ).fetchone()
                if not row:
                    return jsonify({'error': 'Forbidden'}), 403
            members = channel_manager.get_channel_members_list(channel_id) if channel_manager else []
            return jsonify({'success': True, 'members': members, 'count': len(members)})
        except Exception as e:
            logger.error(f"Get channel members (ui) failed: {e}")
            return jsonify({'error': 'Internal server error'}), 500

    def _trigger_member_sync(db_mgr, ch_mgr, channel_id, target_user_id,
                             action, role='member'):
        """Trigger P2P member sync for private/confidential channels."""
        try:
            _, _, _, _, _, _, _, _, _, _, p2p_mgr = _get_app_components_any(current_app)
            if not p2p_mgr or not p2p_mgr.is_running():
                return
            # Check if this is a targeted channel.
            with db_mgr.get_connection() as conn:
                row = conn.execute(
                    "SELECT privacy_mode, name, channel_type, description, crypto_mode, created_by "
                    "FROM channels WHERE id = ?", (channel_id,)
                ).fetchone()
            mode = (row['privacy_mode'] or 'open') if row else 'open'
            if mode not in {'private', 'confidential'}:
                return
            # Find the target user's origin peer
            try:
                user = db_mgr.get_user(target_user_id)
                target_peer = (user.get('origin_peer') or '') if user else ''
            except Exception:
                target_peer = ''
            local_peer = p2p_mgr.get_peer_id() if p2p_mgr else None

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
                    ch_mgr.queue_member_sync_delivery(
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
                sent = p2p_mgr.broadcast_member_sync(
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
                    ch_mgr.mark_member_sync_delivery_attempt(
                        sync_id=sync_id,
                        sent=bool(sent),
                        error=None if sent else 'send_failed',
                    )
                except Exception:
                    pass
                return bool(sent)

            # Build bounded fallback candidates: target origin first, then
            # known member peers, then currently connected peers.
            # We intentionally cap attempts to avoid burst storms.
            candidates: list[str] = []
            if target_peer and target_peer != local_peer:
                candidates.append(target_peer)
            try:
                member_peers = ch_mgr.get_member_peer_ids(channel_id, local_peer)
                for mp in sorted(member_peers):
                    mp_s = str(mp or '').strip()
                    if mp_s and mp_s != local_peer and mp_s not in candidates:
                        candidates.append(mp_s)
            except Exception:
                pass
            try:
                connected = p2p_mgr.get_connected_peers() or []
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
            # Also broadcast a channel announce so all peers (including
            # the newly added member's peer) discover/refresh the channel
            # metadata.  This is belt-and-suspenders alongside the member
            # sync which also carries channel info.
            if action == 'add':
                try:
                    p2p_mgr.broadcast_channel_announce(
                        channel_id=channel_id,
                        name=row['name'] or '',
                        channel_type=row['channel_type'] or 'private',
                        description=row['description'] or '',
                        privacy_mode=mode,
                        created_by_user_id=row['created_by'] if row and 'created_by' in row.keys() else None,
                    )
                except Exception:
                    pass

                # For E2E private channels, deliver current key to the new
                # remote member peer after membership sync.
                try:
                    crypto_mode = _normalize_channel_crypto_mode(
                        row['crypto_mode'] if row and 'crypto_mode' in row.keys() else 'legacy_plaintext'
                    )
                    if (
                        _e2e_private_enabled()
                        and _channel_targets_e2e(mode, crypto_mode)
                        and target_peer
                        and target_peer != local_peer
                    ):
                        active_key = ch_mgr.get_active_channel_key(channel_id)
                        if active_key:
                            key_bytes = ch_mgr.decode_channel_key_material(
                                active_key.get('key_material_enc')
                            )
                            if key_bytes:
                                key_payload = {
                                    'key_id': active_key['key_id'],
                                    'key_material': key_bytes,
                                    'metadata': active_key.get('metadata') or {},
                                }
                                _send_channel_key_to_peer(
                                    channel_manager=ch_mgr,
                                    p2p_mgr=p2p_mgr,
                                    channel_id=channel_id,
                                    key_payload=key_payload,
                                    peer_id=target_peer,
                                )
                except Exception as key_err:
                    logger.warning(f"E2E member key sync failed for {channel_id}: {key_err}")
        except Exception as e:
            logger.warning(f"Member sync trigger failed (non-fatal): {e}")

    def _notify_channel_added_local(channel_id, target_user_id, added_by):
        """Fire inbox + mention_event notifications for a locally-added channel member."""
        try:
            import secrets as _sec
            db_mgr = current_app.config.get('DB_MANAGER')
            inbox_mgr = current_app.config.get('INBOX_MANAGER')
            mention_mgr = current_app.config.get('MENTION_MANAGER')
            p2p_mgr = current_app.config.get('P2P_MANAGER')
            if not db_mgr:
                return
            with db_mgr.get_connection() as conn:
                ch = conn.execute(
                    "SELECT name FROM channels WHERE id = ?", (channel_id,)
                ).fetchone()
            ch_name = ch['name'] if ch else channel_id[:12]

            source_id = f"channel_add_{channel_id}_{target_user_id}_{_sec.token_hex(4)}"
            preview = f"You were added to #{ch_name}"
            if added_by:
                try:
                    with db_mgr.get_connection() as conn:
                        adder = conn.execute(
                            "SELECT display_name, username FROM users WHERE id = ?",
                            (added_by,),
                        ).fetchone()
                    if adder:
                        adder_name = adder['display_name'] or adder['username'] or added_by[:12]
                        preview = f"{adder_name} added you to #{ch_name}"
                except Exception:
                    pass

            local_peer = p2p_mgr.get_peer_id() if p2p_mgr else None

            if mention_mgr:
                try:
                    mention_mgr.record_mentions(
                        user_ids=[target_user_id],
                        source_type='channel_added',
                        source_id=source_id,
                        author_id=added_by,
                        origin_peer=local_peer,
                        channel_id=channel_id,
                        preview=preview,
                    )
                except Exception:
                    pass

            if inbox_mgr:
                try:
                    inbox_mgr.record_mention_triggers(
                        target_ids=[target_user_id],
                        source_type='channel_added',
                        source_id=source_id,
                        author_id=added_by,
                        origin_peer=local_peer,
                        channel_id=channel_id,
                        preview=preview,
                        trigger_type='channel_added',
                    )
                except Exception:
                    pass

            if p2p_mgr:
                try:
                    import time as _time
                    p2p_mgr.record_activity_event({
                        'id': f"ch_add:{source_id}",
                        'peer_id': local_peer or '',
                        'kind': 'channel_added',
                        'timestamp': _time.time(),
                        'preview': preview,
                        'ref': {
                            'channel_id': channel_id,
                            'channel_name': ch_name,
                            'user_id': target_user_id,
                            'added_by': added_by,
                        },
                    })
                except Exception:
                    pass
        except Exception as e:
            logger.debug(f"Channel-added notification failed (non-fatal): {e}")

    @ui.route('/ajax/channel_members/<channel_id>', methods=['POST'])
    @require_login
    def ajax_add_channel_member(channel_id):
        """Add a member to a channel (session auth)."""
        try:
            db_manager, _, _, _, channel_manager, _, _, _, _, _, _ = _get_app_components_any(current_app)
            user_id = get_current_user()
            data = request.get_json() or {}
            target_user_id = data.get('user_id')
            role = data.get('role', 'member')
            if not target_user_id:
                return jsonify({'error': 'user_id required'}), 400
            ok = channel_manager.add_member(channel_id, target_user_id, user_id, role)
            if ok:
                # Trigger P2P member sync for private channels
                _trigger_member_sync(db_manager, channel_manager, channel_id,
                                     target_user_id, 'add', role)
                _notify_channel_added_local(channel_id, target_user_id, user_id)
                return jsonify({'success': True})
            return jsonify({'error': 'Permission denied or user not found'}), 403
        except Exception as e:
            logger.error(f"Add channel member (ui) failed: {e}")
            return jsonify({'error': 'Internal server error'}), 500

    @ui.route('/ajax/channel_members/<channel_id>/<member_id>', methods=['DELETE'])
    @require_login
    def ajax_remove_channel_member(channel_id, member_id):
        """Remove a member from a channel (session auth)."""
        try:
            db_manager, _, _, _, channel_manager, _, _, _, _, _, _ = _get_app_components_any(current_app)
            user_id = get_current_user()
            ok = channel_manager.remove_member(channel_id, member_id, user_id)
            if ok:
                # Trigger P2P member sync for private channels
                _trigger_member_sync(db_manager, channel_manager, channel_id,
                                     member_id, 'remove')

                # Phase-2 E2E: rotate channel key after member removal.
                try:
                    _, _, _, _, _, _, _, _, _, _, p2p_mgr = _get_app_components_any(current_app)
                    if _e2e_private_enabled() and p2p_mgr and p2p_mgr.is_running():
                        with db_manager.get_connection() as conn:
                            row = conn.execute(
                                "SELECT privacy_mode, crypto_mode FROM channels WHERE id = ?",
                                (channel_id,),
                            ).fetchone()
                        privacy_mode = (row['privacy_mode'] if row and 'privacy_mode' in row.keys() else 'open') or 'open'
                        crypto_mode = _normalize_channel_crypto_mode(
                            row['crypto_mode'] if row and 'crypto_mode' in row.keys() else 'legacy_plaintext'
                        )
                        if _channel_targets_e2e(privacy_mode, crypto_mode):
                            prev_key = channel_manager.get_active_channel_key(channel_id)
                            prev_key_id = prev_key.get('key_id') if prev_key else None
                            new_key_payload = _rotate_channel_key_material(
                                channel_manager=channel_manager,
                                channel_id=channel_id,
                                origin_peer=p2p_mgr.get_peer_id(),
                                rotated_from=prev_key_id,
                            )
                            if new_key_payload:
                                _distribute_channel_key_to_member_peers(
                                    channel_manager=channel_manager,
                                    p2p_mgr=p2p_mgr,
                                    channel_id=channel_id,
                                    key_payload=new_key_payload,
                                    rotated_from=prev_key_id,
                                )
                except Exception as rotate_err:
                    logger.warning(f"E2E key rotation after member removal failed: {rotate_err}")
                return jsonify({'success': True})
            return jsonify({'error': 'Permission denied or user not found'}), 403
        except Exception as e:
            logger.error(f"Remove channel member (ui) failed: {e}")
            return jsonify({'error': 'Internal server error'}), 500

    @ui.route('/ajax/delete_channel', methods=['POST'])
    @require_login
    def ajax_delete_channel():
        """Delete a channel. Node-level admins can force-remove any replica."""
        try:
            db_manager, _, _, _, channel_manager, _, _, _, _, _, p2p_manager = _get_app_components_any(current_app)
            user_id = get_current_user()
            node_admin = _is_admin()
            data = request.get_json() or {}
            channel_id = data.get('channel_id')
            if not channel_id:
                return jsonify({'error': 'Channel ID required'}), 400
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

            raw_origin_peer = (
                channel_row['origin_peer']
                if hasattr(channel_row, 'keys') and 'origin_peer' in channel_row.keys()
                else channel_row[0]
            )
            origin_peer = str(raw_origin_peer or '').strip()
            if origin_peer.lower() == 'none':
                # Legacy/null serialization guard: treat textual "None" as missing origin.
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

            ok = channel_manager.delete_channel(channel_id, user_id, force=node_admin)
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
            return jsonify({'error': 'Not authorized to delete channel'}), 403
        except Exception as e:
            logger.error(f"Delete channel (ui) failed: {e}")
            return jsonify({'error': 'Internal server error'}), 500

    @ui.route('/ajax/files/<file_id>/access', methods=['GET'])
    @require_login
    def ajax_file_access(file_id):
        """Inspect whether the current user can access a file and why."""
        try:
            db_manager, _, trust_manager, _, _, file_manager, feed_manager, _, _, _, _ = _get_app_components_any(current_app)
            user_id = get_current_user()
            file_info = file_manager.get_file(file_id)
            if not file_info:
                return jsonify({'success': False, 'error': 'File not found'}), 404

            owner_id = db_manager.get_instance_owner_user_id()
            access = evaluate_file_access(
                db_manager=db_manager,
                file_id=file_id,
                viewer_user_id=user_id,
                file_uploaded_by=file_info.uploaded_by,
                is_admin=bool(owner_id and owner_id == user_id),
                trust_manager=trust_manager,
                feed_manager=feed_manager,
            )
            return jsonify({
                'success': True,
                'file_id': file_id,
                'filename': file_info.original_name,
                'access': access.to_dict(),
            })
        except Exception as e:
            logger.error(f"UI file access inspect failed: {e}", exc_info=True)
            return jsonify({'success': False, 'error': 'Internal server error'}), 500

    @ui.route('/ajax/files/<file_id>/preview', methods=['GET'])
    @require_login
    def ajax_file_preview(file_id):
        """Return a bounded, read-only JSON preview for supported files."""
        try:
            db_manager, _, trust_manager, _, _, file_manager, feed_manager, _, _, _, _ = _get_app_components_any(current_app)
            user_id = get_current_user()
            result = file_manager.get_file_data(file_id)
            if not result:
                return jsonify({'success': False, 'error': 'File not found'}), 404

            file_data, file_info = result
            owner_id = db_manager.get_instance_owner_user_id()
            access = evaluate_file_access(
                db_manager=db_manager,
                file_id=file_id,
                viewer_user_id=user_id,
                file_uploaded_by=file_info.uploaded_by,
                is_admin=bool(owner_id and owner_id == user_id),
                trust_manager=trust_manager,
                feed_manager=feed_manager,
            )
            if not access.allowed:
                return jsonify({
                    'success': False,
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
            logger.error(f"UI file preview failed: {e}", exc_info=True)
            return jsonify({'success': False, 'error': 'Internal server error'}), 500

    # File serving endpoint
    @ui.route('/files/<file_id>')
    @require_login
    def serve_file(file_id):
        """Serve uploaded files."""
        try:
            logger.debug(f"File serving request for file_id: {file_id}")
            db_manager, _, trust_manager, _, _, file_manager, feed_manager, _, _, _, _ = _get_app_components_any(current_app)
            user_id = get_current_user()
            
            # Get file data
            result = file_manager.get_file_data(file_id)
            if not result:
                # Plain text so browser doesn't show "file not available" as if it were the download
                return Response(
                    'File not found. It may not have synced to this device yet.',
                    status=404,
                    mimetype='text/plain',
                    headers={'Content-Disposition': 'inline; filename="error.txt"'},
                )
            
            file_data, file_info = result

            owner_id = db_manager.get_instance_owner_user_id()
            access = evaluate_file_access(
                db_manager=db_manager,
                file_id=file_id,
                viewer_user_id=user_id,
                file_uploaded_by=file_info.uploaded_by,
                is_admin=bool(owner_id and owner_id == user_id),
                trust_manager=trust_manager,
                feed_manager=feed_manager,
            )
            if not access.allowed:
                return Response(
                    'Access denied.',
                    status=403,
                    mimetype='text/plain',
                    headers={'Content-Disposition': 'inline; filename="error.txt"'},
                )
            
            # Log file access
            file_manager.log_file_access(file_id, user_id, 
                                       request.remote_addr, 
                                       request.headers.get('User-Agent'))
            
            # Return file
            return Response(
                file_data,
                mimetype=file_info.content_type,
                headers={
                    'Content-Disposition': f'inline; filename="{file_info.original_name}"',
                    'Content-Length': str(file_info.size)
                }
            )
            
        except Exception as e:
            logger.error(f"File serving error: {e}")
            return jsonify({'error': 'Internal server error'}), 500

    @ui.route('/files/<file_id>/thumb')
    @require_login
    def serve_file_thumb(file_id):
        """Serve a thumbnail version of an uploaded image (falls back to original)."""
        try:
            db_manager, _, trust_manager, _, _, file_manager, feed_manager, _, _, _, _ = _get_app_components_any(current_app)
            user_id = get_current_user()

            file_info = file_manager.get_file(file_id)
            if not file_info:
                return jsonify({'error': 'File not found'}), 404

            owner_id = db_manager.get_instance_owner_user_id()
            access = evaluate_file_access(
                db_manager=db_manager,
                file_id=file_id,
                viewer_user_id=user_id,
                file_uploaded_by=file_info.uploaded_by,
                is_admin=bool(owner_id and owner_id == user_id),
                trust_manager=trust_manager,
                feed_manager=feed_manager,
            )
            if not access.allowed:
                return jsonify({'error': 'Access denied', 'reason': access.reason}), 403

            result = file_manager.get_thumbnail_data(file_id)
            if not result:
                return jsonify({'error': 'File not found'}), 404

            thumb_data, file_info = result

            return Response(
                thumb_data,
                mimetype=file_info.content_type,
                headers={
                    'Content-Disposition': f'inline; filename="thumb_{file_info.original_name}"',
                    'Content-Length': str(len(thumb_data)),
                    'Cache-Control': 'public, max-age=86400',
                }
            )
        except Exception as e:
            logger.error(f"Thumbnail serving error: {e}")
            return jsonify({'error': 'Internal server error'}), 500

    # Like/Comment endpoints
    @ui.route('/ajax/toggle_like', methods=['POST'])
    @require_login
    def ajax_toggle_like():
        """AJAX endpoint to toggle like on a message."""
        try:
            _, _, _, _, _, _, _, interaction_manager, profile_manager, _, p2p_manager = _get_app_components_any(current_app)
            user_id = get_current_user()
            
            data = request.get_json()
            message_id = data.get('message_id')
            reaction_type = data.get('reaction_type', 'like')
            
            if not message_id:
                return jsonify({'error': 'Message ID required'}), 400
            
            from ..core.interactions import InteractionType
            try:
                reaction_enum = InteractionType(reaction_type)
            except ValueError:
                reaction_enum = InteractionType.LIKE
            
            liked = interaction_manager.toggle_like(message_id, user_id, reaction_enum)
            interactions = interaction_manager.get_message_interactions(message_id)
            
            # Broadcast interaction to P2P peers
            if p2p_manager and p2p_manager.is_running():
                try:
                    sender_display = None
                    if profile_manager:
                        profile = profile_manager.get_profile(user_id)
                        if profile:
                            sender_display = profile.display_name or profile.username
                    p2p_manager.broadcast_interaction(
                        item_id=message_id,
                        user_id=user_id,
                        action='like' if liked else 'unlike',
                        item_type='message',
                        display_name=sender_display,
                    )
                except Exception as p2p_err:
                    logger.warning(f"Failed to broadcast message like via P2P: {p2p_err}")

            return jsonify({
                'success': True,
                'liked': liked,
                'interactions': interactions
            })
            
        except Exception as e:
            logger.error(f"Toggle like error: {e}")
            return jsonify({'error': 'Internal server error'}), 500

    @ui.route('/ajax/add_comment', methods=['POST'])
    @require_login
    def ajax_add_comment():
        """AJAX endpoint to add comment to a message."""
        try:
            _, _, _, _, _, _, _, interaction_manager, _, _, _ = _get_app_components_any(current_app)
            user_id = get_current_user()
            
            data = request.get_json()
            message_id = data.get('message_id')
            content = data.get('content', '').strip()
            parent_comment_id = data.get('parent_comment_id')
            
            if not message_id:
                return jsonify({'error': 'Message ID required'}), 400
            
            if not content:
                return jsonify({'error': 'Comment content required'}), 400
            
            comment = interaction_manager.add_comment(
                message_id, user_id, content, parent_comment_id
            )
            
            if comment:
                return jsonify({
                    'success': True,
                    'comment': comment.to_dict()
                })
            else:
                return jsonify({'error': 'Failed to add comment'}), 500
                
        except Exception as e:
            logger.error(f"Add comment error: {e}")
            return jsonify({'error': 'Internal server error'}), 500

    @ui.route('/ajax/get_comments/<message_id>', methods=['GET'])
    @require_login
    def ajax_get_comments(message_id):
        """AJAX endpoint to get comments for a message."""
        try:
            _, _, _, _, _, _, _, interaction_manager, _, _, _ = _get_app_components_any(current_app)
            
            comments = interaction_manager.get_message_comments(message_id)
            interactions = interaction_manager.get_message_interactions(message_id)
            
            return jsonify({
                'comments': [comment.to_dict() for comment in comments],
                'interactions': interactions
            })
            
        except Exception as e:
            logger.error(f"Get comments error: {e}")
            return jsonify({'error': 'Internal server error'}), 500

    @ui.route('/ajax/delete_comment', methods=['POST'])
    @require_login
    def ajax_delete_comment():
        """AJAX endpoint to delete a comment."""
        try:
            _, _, _, _, _, _, _, interaction_manager, _, _, _ = _get_app_components_any(current_app)
            user_id = get_current_user()
            
            data = request.get_json()
            comment_id = data.get('comment_id')
            
            if not comment_id:
                return jsonify({'error': 'Comment ID required'}), 400
            
            success = interaction_manager.delete_comment(comment_id, user_id)
            
            if success:
                return jsonify({'success': True})
            else:
                return jsonify({'error': 'Failed to delete comment'}), 500
                
        except Exception as e:
            logger.error(f"Delete comment error: {e}")
            return jsonify({'error': 'Internal server error'}), 500

    # Save landing page preference
    @ui.route('/ajax/set_landing', methods=['POST'])
    @require_login
    def ajax_set_landing():
        """Store the user's preferred landing page in a cookie."""
        data = request.get_json(silent=True) or {}
        page = data.get('page', '')
        if page not in ('feed', 'channels', 'messages'):
            return jsonify({'error': 'Invalid page choice'}), 400
        resp = jsonify({'success': True, 'landing': page})
        resp.set_cookie('canopy_landing', page, max_age=365 * 24 * 3600, samesite='Lax')
        return resp

    # Database management AJAX endpoints for Settings page
    @ui.route('/ajax/database_cleanup', methods=['POST'])
    @require_login
    def ajax_database_cleanup():
        """AJAX: Clean up old data from the database."""
        try:
            db_manager, _, _, _, channel_manager, _, _, _, _, _, _ = _get_app_components_any(current_app)
            data = request.get_json() or {}
            days = int(data.get('days', 30))
            db_manager.cleanup_old_data(days)
            pruned = channel_manager.prune_processed_messages(keep_days=7)
            return jsonify({
                'success': True,
                'message': f'Cleaned up data older than {days} days. Pruned {pruned} dedup records.',
            })
        except Exception as e:
            logger.error(f"Database cleanup error: {e}")
            return jsonify({'error': 'Internal server error'}), 500

    @ui.route('/ajax/database_export', methods=['GET'])
    @require_login
    def ajax_database_export():
        """AJAX: Export database as downloadable file."""
        try:
            db_manager, _, _, _, _, _, _, _, _, _, _ = _get_app_components_any(current_app)
            from flask import send_file
            backup_path = db_manager.backup_database(suffix='export')
            if not backup_path:
                return jsonify({'error': 'Export failed: could not create backup'}), 500
            # Resolve to absolute path so send_file finds the file regardless of cwd
            abs_path = backup_path.resolve()
            if not abs_path.exists():
                logger.error(f"Database export: backup file missing at {abs_path}")
                return jsonify({'error': 'Export failed: backup file not found'}), 500
            return send_file(
                str(abs_path),
                mimetype='application/x-sqlite3',
                as_attachment=True,
                download_name=backup_path.name,
            )
        except OSError as e:
            logger.error(f"Database export error: {e}")
            return jsonify({'error': 'Export failed: permission or path error'}), 500
        except Exception as e:
            logger.error(f"Database export error: {e}")
            return jsonify({'error': 'Internal server error'}), 500

    @ui.route('/ajax/database_import', methods=['POST'])
    @require_login
    def ajax_database_import():
        """AJAX: Import a SQLite database backup (admin-only, destructive)."""
        temp_import_path: Optional[str] = None
        try:
            db_manager, _, _, _, _, _, _, _, _, _, _ = _get_app_components_any(current_app)

            if not _is_admin():
                return jsonify({'error': 'Only the instance admin can import a database backup'}), 403

            confirm_phrase = (request.form.get('confirm_phrase') or '').strip()
            if confirm_phrase != 'IMPORT DATABASE':
                return jsonify({'error': 'Confirmation phrase mismatch. Type IMPORT DATABASE to proceed.'}), 400

            upload = request.files.get('database')
            if not upload:
                return jsonify({'error': 'No database file uploaded'}), 400
            if not upload.filename:
                return jsonify({'error': 'No database file selected'}), 400

            safe_name = secure_filename(upload.filename)
            ext = os.path.splitext(safe_name)[1].lower()
            if ext and ext not in {'.db', '.sqlite', '.sqlite3'}:
                return jsonify({'error': 'Unsupported file type. Upload a .db, .sqlite, or .sqlite3 file.'}), 400

            db_path_raw = getattr(db_manager, 'db_path', None)
            if not db_path_raw:
                logger.error("Database import failed: db_manager missing db_path")
                return jsonify({'error': 'Database import unavailable on this node'}), 500

            db_path = os.path.abspath(str(db_path_raw))
            db_dir = os.path.dirname(db_path)
            os.makedirs(db_dir, exist_ok=True)

            fd, temp_import_path = tempfile.mkstemp(prefix='canopy_import_', suffix='.db', dir=db_dir)
            max_import_bytes = 512 * 1024 * 1024  # 512MB hard limit for safety
            bytes_written = 0
            header = b''

            with os.fdopen(fd, 'wb') as temp_file:
                while True:
                    chunk = upload.stream.read(1024 * 1024)
                    if not chunk:
                        break
                    if not header:
                        header = chunk[:16]
                    bytes_written += len(chunk)
                    if bytes_written > max_import_bytes:
                        return jsonify({'error': 'Import file too large (max 512MB).'}), 400
                    temp_file.write(chunk)

            if bytes_written == 0:
                return jsonify({'error': 'Uploaded file was empty'}), 400
            if not header.startswith(b'SQLite format 3\x00'):
                return jsonify({'error': 'Uploaded file is not a valid SQLite database'}), 400

            src_validate = sqlite3.connect(temp_import_path, timeout=10)
            try:
                check = src_validate.execute("PRAGMA quick_check").fetchone()
                status = str(check[0]).lower() if check else 'unknown'
                if status != 'ok':
                    return jsonify({'error': f'Import database failed integrity check: {status}'}), 400

                table_rows = src_validate.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
                table_names = {str(row[0]) for row in table_rows}
                if 'users' not in table_names:
                    return jsonify({'error': 'Import database does not look like a Canopy database (missing users table).'}), 400
            finally:
                src_validate.close()

            backup_path = db_manager.backup_database(suffix='pre_import')
            if not backup_path:
                return jsonify({'error': 'Failed to create pre-import backup. Import aborted.'}), 500

            def _copy_sqlite_db(src_file: str, dst_file: str) -> None:
                src_conn = sqlite3.connect(src_file, timeout=30)
                dst_conn = sqlite3.connect(dst_file, timeout=30)
                try:
                    src_conn.backup(dst_conn)
                    dst_conn.commit()
                finally:
                    try:
                        dst_conn.close()
                    finally:
                        src_conn.close()

            try:
                _copy_sqlite_db(temp_import_path, db_path)

                # Ensure this thread does not keep stale pooled connection state.
                if hasattr(db_manager, 'close_pooled_connection'):
                    try:
                        db_manager.close_pooled_connection()
                    except Exception:
                        pass

                # Bring imported DB up to current schema if needed.
                if hasattr(db_manager, '_initialize_database'):
                    db_manager._initialize_database()

                with db_manager.get_connection(busy_timeout_ms=30_000) as conn:
                    post_check = conn.execute("PRAGMA quick_check").fetchone()
                    post_status = str(post_check[0]).lower() if post_check else 'unknown'
                    if post_status != 'ok':
                        raise RuntimeError(f'post-import integrity check failed: {post_status}')
            except Exception as import_err:
                logger.error(f"Database import failed; attempting rollback: {import_err}")
                try:
                    _copy_sqlite_db(str(backup_path), db_path)
                    logger.warning("Database import rolled back to pre-import backup")
                except Exception as rollback_err:
                    logger.critical(f"Database import rollback failed: {rollback_err}")
                    return jsonify({
                        'error': 'Database import failed and automatic rollback also failed. Manual restore is required.',
                        'details': str(import_err),
                    }), 500
                return jsonify({
                    'error': 'Database import failed. Previous database was restored from backup.',
                    'details': str(import_err),
                }), 500

            return jsonify({
                'success': True,
                'message': f'Database imported successfully. Safety backup created: {os.path.basename(str(backup_path))}. Refresh this page to load the imported state.',
            })
        except Exception as e:
            logger.error(f"Database import error: {e}")
            return jsonify({'error': 'Internal server error'}), 500
        finally:
            if temp_import_path and os.path.exists(temp_import_path):
                try:
                    os.unlink(temp_import_path)
                except Exception:
                    pass

    @ui.route('/ajax/system_reset', methods=['POST'])
    @require_login
    def ajax_system_reset():
        """AJAX: Reset the system by clearing all user data."""
        try:
            db_manager, _, _, _, _, _, _, _, _, _, _ = _get_app_components_any(current_app)
            # Create a final backup before reset
            db_manager.backup_database(suffix='pre_reset')
            with db_manager.get_connection() as conn:
                conn.executescript("""
                    DELETE FROM channel_messages;
                    DELETE FROM channel_members WHERE user_id != 'system';
                    DELETE FROM feed_posts;
                    DELETE FROM messages;
                    DELETE FROM trust_scores;
                    DELETE FROM delete_signals;
                    DELETE FROM processed_messages;
                """)
            return jsonify({
                'success': True,
                'message': 'System reset complete. Messages, posts, and trust data cleared.',
            })
        except Exception as e:
            logger.error(f"System reset error: {e}")
            return jsonify({'error': 'Internal server error'}), 500

    # Profile management
    @ui.route('/profile')
    @require_login
    def profile():
        """User profile management page."""
        try:
            db_manager, _, _, _, _, _, _, _, profile_manager, _, _ = _get_app_components_any(current_app)
            user_id = get_current_user()
            
            # Get or create user profile
            user_profile = profile_manager.ensure_default_profile(user_id, user_id)

            # Build activity stats from actual data (avoid placeholder values).
            stats = {
                'messages': 0,
                'channels': 0,
                'posts': 0,
                'api_keys': 0,
            }
            try:
                with db_manager.get_connection() as conn:
                    def _table_exists(name: str) -> bool:
                        row = conn.execute(
                            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                            (name,),
                        ).fetchone()
                        return bool(row)

                    if _table_exists('channel_messages'):
                        row = conn.execute(
                            "SELECT COUNT(*) AS count FROM channel_messages WHERE user_id = ?",
                            (user_id,),
                        ).fetchone()
                        stats['messages'] += int((row['count'] if row else 0) or 0)

                    if _table_exists('messages'):
                        row = conn.execute(
                            "SELECT COUNT(*) AS count FROM messages WHERE sender_id = ?",
                            (user_id,),
                        ).fetchone()
                        stats['messages'] += int((row['count'] if row else 0) or 0)

                    if _table_exists('channel_members'):
                        row = conn.execute(
                            "SELECT COUNT(DISTINCT channel_id) AS count FROM channel_members WHERE user_id = ?",
                            (user_id,),
                        ).fetchone()
                        stats['channels'] = int((row['count'] if row else 0) or 0)

                    if _table_exists('feed_posts'):
                        row = conn.execute(
                            "SELECT COUNT(*) AS count FROM feed_posts WHERE author_id = ?",
                            (user_id,),
                        ).fetchone()
                        stats['posts'] = int((row['count'] if row else 0) or 0)

                    if _table_exists('api_keys'):
                        row = conn.execute(
                            "SELECT COUNT(*) AS count FROM api_keys WHERE user_id = ? AND COALESCE(revoked, 0) = 0",
                            (user_id,),
                        ).fetchone()
                        stats['api_keys'] = int((row['count'] if row else 0) or 0)
            except Exception as stats_err:
                logger.warning(f"Profile stats query failed for {user_id}: {stats_err}")
            
            return render_template('profile.html',
                                 profile=user_profile,
                                 profile_stats=stats,
                                 user_id=user_id)
                                 
        except Exception as e:
            logger.error(f"Profile error: {e}")
            flash('Error loading profile', 'error')
            return render_template('error.html', error=str(e))

    @ui.route('/ajax/update_profile', methods=['POST'])
    @require_login
    def ajax_update_profile():
        """AJAX endpoint to update user profile."""
        try:
            _, _, _, _, _, _, _, _, profile_manager, _, _ = _get_app_components_any(current_app)
            user_id = get_current_user()
            
            data = request.get_json()
            display_name = data.get('display_name', '').strip()
            bio = data.get('bio', '').strip()
            theme_preference = data.get('theme_preference', 'dark')
            
            # Validate inputs
            if len(display_name) > 100:
                return jsonify({'error': 'Display name too long (max 100 characters)'}), 400
            
            if len(bio) > 500:
                return jsonify({'error': 'Bio too long (max 500 characters)'}), 400
            
            if theme_preference not in ['dark', 'light', 'auto', 'liquid-glass', 'eco']:
                theme_preference = 'dark'
            
            # Update profile
            success = profile_manager.update_profile(
                user_id,
                display_name=display_name or None,
                bio=bio or None,
                theme_preference=theme_preference
            )
            
            if success:
                # Broadcast profile change to connected peers
                try:
                    _, _, _, _, _, _, _, _, _, _, p2p_manager = _get_app_components_any(current_app)
                    if p2p_manager and p2p_manager.is_running():
                        card = profile_manager.get_profile_card(user_id)
                        if card:
                            p2p_manager.broadcast_profile_update(card)
                except Exception as bcast_err:
                    logger.warning(f"Profile broadcast failed: {bcast_err}")
                return jsonify({'success': True})
            else:
                return jsonify({'error': 'Failed to update profile'}), 500
                
        except Exception as e:
            logger.error(f"Update profile error: {e}")
            return jsonify({'error': 'Internal server error'}), 500

    @ui.route('/ajax/change_password', methods=['POST'])
    @require_login
    def ajax_change_password():
        """AJAX endpoint to change the current user's password."""
        try:
            db_manager, _, _, _, _, _, _, _, _, _, _ = _get_app_components_any(current_app)
            user_id = get_current_user()
            
            data = request.get_json()
            current_password = data.get('current_password', '')
            new_password = data.get('new_password', '')
            confirm_password = data.get('confirm_password', '')
            
            if not current_password:
                return jsonify({'error': 'Current password is required'}), 400
            if not new_password or len(new_password) < 8:
                return jsonify({'error': 'New password must be at least 8 characters'}), 400
            if new_password != confirm_password:
                return jsonify({'error': 'New passwords do not match'}), 400
            
            # Verify current password
            user = db_manager.get_user(user_id)
            if not user or not user.get('password_hash'):
                return jsonify({'error': 'Account not found'}), 404
            
            if not _verify_password(current_password, user['password_hash']):
                return jsonify({'error': 'Current password is incorrect'}), 403
            
            # Validate new password strength
            from ..security.password import validate_password_strength
            is_valid, error_msg = validate_password_strength(new_password)
            if not is_valid:
                return jsonify({'error': error_msg}), 400
            
            # Update password
            new_hash = _hash_password(new_password)
            with db_manager.get_connection() as conn:
                conn.execute(
                    "UPDATE users SET password_hash = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (new_hash, user_id)
                )
                conn.commit()
            
            logger.info(f"Password changed for user {user_id}")
            return jsonify({'success': True, 'message': 'Password changed successfully'})
            
        except Exception as e:
            logger.error(f"Change password error: {e}")
            return jsonify({'error': 'Internal server error'}), 500

    @ui.route('/ajax/upload_avatar', methods=['POST'])
    @require_login
    def ajax_upload_avatar():
        """AJAX endpoint to upload user avatar."""
        try:
            _, _, _, _, _, _, _, _, profile_manager, _, _ = _get_app_components_any(current_app)
            user_id = get_current_user()
            
            if 'avatar' not in request.files:
                return jsonify({'error': 'No avatar file provided'}), 400
            
            avatar_file = request.files['avatar']
            if avatar_file.filename == '':
                return jsonify({'error': 'No file selected'}), 400
            
            # Validate file type
            if not (avatar_file.content_type or '').startswith('image/'):
                return jsonify({'error': 'Only image files are allowed'}), 400
            
            # Validate file size (max 5MB)
            avatar_data = avatar_file.read()
            if len(avatar_data) > 5 * 1024 * 1024:
                return jsonify({'error': 'File too large (max 5MB)'}), 400
            
            # Update avatar
            file_id = profile_manager.update_avatar(
                user_id, avatar_data, avatar_file.filename, avatar_file.content_type
            )
            
            if file_id:
                avatar_url = f"/files/{file_id}"
                # Broadcast updated profile (with new avatar) to peers
                try:
                    _, _, _, _, _, _, _, _, _, _, p2p_manager = _get_app_components_any(current_app)
                    if p2p_manager and p2p_manager.is_running():
                        card = profile_manager.get_profile_card(user_id)
                        if card:
                            p2p_manager.broadcast_profile_update(card)
                except Exception as bcast_err:
                    logger.warning(f"Avatar profile broadcast failed: {bcast_err}")
                return jsonify({
                    'success': True,
                    'avatar_url': avatar_url
                })
            else:
                return jsonify({'error': 'Failed to upload avatar'}), 500
                
        except Exception as e:
            logger.error(f"Upload avatar error: {e}")
            return jsonify({'error': 'Internal server error'}), 500

    @ui.route('/ajax/custom_emojis', methods=['GET', 'POST'])
    @require_login
    def ajax_custom_emojis():
        """List or upload custom emojis (local to this node)."""
        if request.method == 'GET':
            with _CUSTOM_EMOJI_LOCK:
                entries = _load_custom_emojis()
            # Return minimal fields for UI
            payload = [{
                'name': e.get('name'),
                'url': e.get('url'),
                'filename': e.get('filename'),
            } for e in entries]
            return jsonify({'emojis': payload})

        # POST upload
        if 'emoji' not in request.files:
            return jsonify({'error': 'No emoji file provided'}), 400

        emoji_file = request.files['emoji']
        if emoji_file.filename == '':
            return jsonify({'error': 'No file selected'}), 400

        if not emoji_file.content_type or not emoji_file.content_type.startswith('image/'):
            return jsonify({'error': 'Only image files are allowed'}), 400

        # Extension whitelist — reject non-image extensions regardless of Content-Type
        _ALLOWED_EMOJI_EXTS = {'.png', '.gif', '.webp', '.jpg', '.jpeg', '.svg'}
        emoji_filename = emoji_file.filename or ''
        raw_filename = secure_filename(emoji_filename)
        raw_ext = os.path.splitext(raw_filename)[1].lower()
        if raw_ext not in _ALLOWED_EMOJI_EXTS:
            return jsonify({'error': f'Unsupported file type. Allowed: {", ".join(sorted(_ALLOWED_EMOJI_EXTS))}'}), 400

        raw = emoji_file.read()
        if len(raw) > 512 * 1024:
            return jsonify({'error': 'File too large (max 512KB)'}), 400

        # Magic-byte validation — verify file header matches an image format
        _IMAGE_MAGIC = {
            b'\x89PNG': '.png',
            b'GIF8': '.gif',
            b'RIFF': '.webp',  # WebP starts with RIFF....WEBP
            b'\xff\xd8\xff': '.jpg',
        }
        header = raw[:8]
        is_valid_image = any(header.startswith(sig) for sig in _IMAGE_MAGIC)
        # SVG is XML-based, check for opening tag
        if not is_valid_image and raw_ext == '.svg':
            is_valid_image = b'<svg' in raw[:1024].lower()
        if not is_valid_image:
            return jsonify({'error': 'File does not appear to be a valid image'}), 400

        display_name = request.form.get('name', '').strip()
        base_name = display_name or os.path.splitext(emoji_filename)[0]
        safe_name = _slugify(base_name)

        filename = raw_filename
        ext = raw_ext or '.png'
        token = secrets.token_hex(4)
        stored_name = f"{safe_name}_{token}{ext}"

        base_dir = _custom_emoji_dir()
        file_path = os.path.join(base_dir, stored_name)
        with open(file_path, 'wb') as f:
            f.write(raw)

        entry = {
            'name': safe_name,
            'filename': stored_name,
            'url': f"/custom_emojis/{stored_name}",
            'created_at': datetime.now(timezone.utc).isoformat(),
        }

        with _CUSTOM_EMOJI_LOCK:
            entries = _load_custom_emojis()
            entries.append(entry)
            _save_custom_emojis(entries)

        return jsonify({'success': True, 'emoji': entry})

    @ui.route('/custom_emojis/<path:filename>')
    @require_login
    def serve_custom_emoji(filename: str) -> Any:
        """Serve custom emoji images stored locally."""
        base_dir = _custom_emoji_dir()
        safe_path = os.path.abspath(os.path.join(base_dir, filename))
        if not safe_path.startswith(base_dir):
            return jsonify({'error': 'Not found'}), 404
        if not os.path.isfile(safe_path):
            return jsonify({'error': 'Not found'}), 404
        return send_file(safe_path, conditional=True)

    @ui.route('/ajax/get_user_display_info', methods=['GET'])
    @require_login
    def ajax_get_user_display_info():
        """AJAX endpoint to get user display information for avatars/names."""
        try:
            db_manager, _, _, _, _, _, _, _, profile_manager, _, _ = _get_app_components_any(current_app)
            
            user_ids = request.args.get('user_ids', '').split(',')
            user_ids = [uid.strip() for uid in user_ids if uid.strip()]
            
            logger.debug(f"Get user display info request: user_ids={user_ids}")
            
            if not user_ids:
                return jsonify({'error': 'No user IDs provided'}), 400

            presence_records = get_agent_presence_records(db_manager=db_manager, user_ids=user_ids)
            user_info = {}
            for user_id in user_ids:
                row = None
                if db_manager:
                    try:
                        row = db_manager.get_user(user_id)
                    except Exception:
                        row = None

                profile = profile_manager.get_profile(user_id)
                if profile:
                    logger.debug(
                        "Found profile for %s: display_name=%s, avatar_file_id=%s, avatar_url=%s",
                        user_id,
                        getattr(profile, 'display_name', None),
                        getattr(profile, 'avatar_file_id', None),
                        getattr(profile, 'avatar_url', None),
                    )
                    display_name = (
                        getattr(profile, 'display_name', None)
                        or getattr(profile, 'username', None)
                        or (row.get('display_name') if row else None)
                        or (row.get('username') if row else None)
                        or user_id
                    )
                    username = (
                        getattr(profile, 'username', None)
                        or (row.get('username') if row else None)
                        or user_id
                    )
                    origin_peer = (
                        getattr(profile, 'origin_peer', None)
                        or (row.get('origin_peer') if row else None)
                    )
                    account_type_raw = (
                        getattr(profile, 'account_type', None)
                        or (row.get('account_type') if row else None)
                    )
                    status = (row.get('status') if row else None) or 'active'
                    avatar_url = getattr(profile, 'avatar_url', None)
                    agent_directives = (
                        getattr(profile, 'agent_directives', None)
                        or (row.get('agent_directives') if row else None)
                    )
                else:
                    logger.warning(f"No profile found for user_id: {user_id}")
                    display_name = (row.get('display_name') if row else None) or user_id
                    username = (row.get('username') if row else None) or user_id
                    origin_peer = (row.get('origin_peer') if row else None)
                    account_type_raw = (row.get('account_type') if row else None)
                    status = (row.get('status') if row else None) or 'active'
                    avatar_url = None
                    agent_directives = (row.get('agent_directives') if row else None)

                account_type = _normalized_account_type(
                    account_type_raw,
                    status=status,
                    agent_directives=agent_directives,
                    has_presence_checkin=bool((presence_records.get(user_id) or {}).get('last_check_in_at')),
                )
                status = str(status or 'active').strip().lower() or 'active'
                origin_peer = str(origin_peer or '').strip() or None

                user_info[user_id] = {
                    'display_name': display_name,
                    'avatar_url': avatar_url,
                    'username': username,
                    'origin_peer': origin_peer,
                    'account_type': account_type,
                    'status': status,
                    'is_remote': bool(origin_peer),
                }
            
            logger.debug(f"Returning user info: {user_info}")
            
            return jsonify({
                'success': True,
                'users': user_info
            })
            
        except Exception as e:
            logger.error(f"Get user display info error: {e}")
            return jsonify({'error': 'Internal server error'}), 500

    @ui.route('/ajax/resync_user_avatar', methods=['POST'])
    @require_login
    def ajax_resync_user_avatar():
        """Trigger a profile re-sync for a user to recover their avatar."""
        try:
            _, _, _, _, _, _, _, _, _, _, p2p_manager = _get_app_components_any(current_app)
            data = request.get_json(silent=True) or {}
            user_id = data.get('user_id', '').strip()
            hint_peer = data.get('origin_peer', '').strip()
            if not user_id:
                return jsonify({'error': 'user_id required'}), 400
            if not p2p_manager or not hasattr(p2p_manager, 'resync_user_avatar'):
                return jsonify({'error': 'P2P manager not available'}), 503
            result = p2p_manager.resync_user_avatar(user_id, hint_peer=hint_peer)
            return jsonify(result)
        except Exception as e:
            logger.error(f"Resync user avatar error: {e}", exc_info=True)
            return jsonify({'error': 'Internal server error'}), 500

    @ui.route('/ajax/connection_diagnostics', methods=['GET'])
    @require_login
    def ajax_connection_diagnostics():
        """Connection diagnostics: per-peer health, recent failures, and local config."""
        try:
            _, _, _, _, _, _, _, _, _, config, p2p_manager = _get_app_components_any(current_app)
            from ..network.invite import generate_invite

            if not p2p_manager:
                return jsonify({'success': False, 'error': 'P2P network unavailable'}), 503

            im = p2p_manager.identity_manager
            conn_mgr = getattr(p2p_manager, 'connection_manager', None)
            active_relays = dict(getattr(p2p_manager, '_active_relays', {}))

            peers: list[dict[str, Any]] = []
            for peer_id in p2p_manager.get_connected_peers():
                is_relayed = peer_id in active_relays
                relay_via = active_relays.get(peer_id)
                relay_via_name: Optional[str] = None
                if relay_via:
                    relay_via_name = (
                        im.peer_display_names.get(relay_via)
                        or relay_via[:12]
                    )
                conn = conn_mgr.get_connection(peer_id) if conn_mgr else None
                latency_ms = getattr(conn, 'last_ping_latency_ms', None) if conn else None
                peers.append({
                    'peer_id': peer_id,
                    'display_name': im.peer_display_names.get(peer_id, ''),
                    'connection_type': 'relayed' if is_relayed else 'direct',
                    'relay_via': relay_via,
                    'relay_via_name': relay_via_name,
                    'latency_ms': latency_ms,
                    'connected_at': conn.connected_at if conn else None,
                    'last_activity': conn.last_activity if conn else None,
                    'endpoints': list(im.peer_endpoints.get(peer_id, [])),
                })

            direct_set = {p['peer_id'] for p in peers}
            for dest_peer, relay_peer in active_relays.items():
                if dest_peer not in direct_set:
                    relay_name = (
                        im.peer_display_names.get(relay_peer)
                        or relay_peer[:12]
                    )
                    peers.append({
                        'peer_id': dest_peer,
                        'display_name': im.peer_display_names.get(dest_peer, ''),
                        'connection_type': 'relayed',
                        'relay_via': relay_peer,
                        'relay_via_name': relay_name,
                        'latency_ms': None,
                        'connected_at': None,
                        'last_activity': None,
                        'endpoints': list(im.peer_endpoints.get(dest_peer, [])),
                    })

            recent_failures: list[dict[str, Any]] = []
            try:
                for event in p2p_manager.get_activity_events(limit=200):
                    kind = event.get('kind')
                    status = (event.get('status') or '').lower()
                    if kind == 'connection' and status in {'failed', 'disconnected'}:
                        recent_failures.append({
                            'peer_id': event.get('peer_id', ''),
                            'endpoint': event.get('endpoint', ''),
                            'reason': event.get('detail', ''),
                            'timestamp': event.get('timestamp'),
                        })
                    if len(recent_failures) >= 5:
                        break
            except Exception:
                pass

            mesh_port = config.network.mesh_port if config else 7771
            relay_status = p2p_manager.get_relay_status() if p2p_manager else {}
            local_endpoints: list[str] = []
            try:
                invite = generate_invite(im, mesh_port)
                local_endpoints = list(invite.endpoints)
            except Exception:
                pass

            return jsonify({
                'success': True,
                'peers': peers,
                'recent_failures': recent_failures,
                'local': {
                    'mesh_port': mesh_port,
                    'endpoints': local_endpoints,
                    'relay_policy': relay_status.get('relay_policy', 'broker_only'),
                },
            })
        except Exception as e:
            logger.error(f"Connection diagnostics error: {e}", exc_info=True)
            return jsonify({'success': False, 'error': 'Failed to load diagnostics'}), 500

    def _clean_mention_handle(display_name, username, user_id):
        """Derive a mention-safe handle from user info.

        Prefers display_name (spaces -> underscores, non-ASCII stripped),
        falls back to username, then user_id.
        """
        import re as _re
        for candidate in (display_name, username):
            if not candidate:
                continue
            h = candidate.replace(' ', '_')
            h = _re.sub(r'[^A-Za-z0-9_.\-]', '', h)
            if h and _re.match(r'^[A-Za-z0-9]', h) and len(h) >= 2:
                return h[:49]
        return user_id or ''

    def _objective_deadline_label(deadline: Optional[datetime], now: Optional[datetime] = None) -> Optional[str]:
        if not deadline:
            return None
        try:
            now_dt = now or datetime.now(timezone.utc)
            dt = deadline if deadline.tzinfo else deadline.replace(tzinfo=timezone.utc)
            delta = dt - now_dt
            seconds = int(delta.total_seconds())
            if seconds <= 0:
                days = abs(seconds) // 86400
                hours = (abs(seconds) % 86400) // 3600
                if days > 0:
                    return f"Overdue by {days}d"
                if hours > 0:
                    return f"Overdue by {hours}h"
                return "Overdue"
            days = seconds // 86400
            hours = (seconds % 86400) // 3600
            if days > 0:
                return f"Due in {days}d"
            if hours > 0:
                return f"Due in {hours}h"
            return "Due soon"
        except Exception:
            return None

    def _request_due_label(due_at: Optional[datetime], now: Optional[datetime] = None) -> Optional[str]:
        if not due_at:
            return None
        try:
            now_dt = now or datetime.now(timezone.utc)
            dt = due_at if due_at.tzinfo else due_at.replace(tzinfo=timezone.utc)
            delta = dt - now_dt
            seconds = int(delta.total_seconds())
            if seconds <= 0:
                days = abs(seconds) // 86400
                hours = (abs(seconds) % 86400) // 3600
                if days > 0:
                    return f"Overdue by {days}d"
                if hours > 0:
                    return f"Overdue by {hours}h"
                return "Overdue"
            days = seconds // 86400
            hours = (seconds % 86400) // 3600
            if days > 0:
                return f"Due in {days}d"
            if hours > 0:
                return f"Due in {hours}h"
            return "Due soon"
        except Exception:
            return None

    def _signal_expiry_label(expires_at: Optional[datetime], now: Optional[datetime] = None) -> Optional[str]:
        if not expires_at:
            return None
        try:
            now_dt = now or datetime.now(timezone.utc)
            dt = expires_at if expires_at.tzinfo else expires_at.replace(tzinfo=timezone.utc)
            delta = dt - now_dt
            seconds = int(delta.total_seconds())
            if seconds <= 0:
                return "Expired"
            days = seconds // 86400
            hours = (seconds % 86400) // 3600
            if days > 0:
                return f"Expires in {days}d"
            if hours > 0:
                return f"Expires in {hours}h"
            return "Expires soon"
        except Exception:
            return None

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
                    # Deduplicate while preserving order
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
                visibility=visibility,
                origin_peer=origin_peer,
                source_type=source_type,
                source_id=source_id,
                created_at=created_at,
                actor_id=actor_id,
                members=members_payload,
                members_defined=('members' in spec.fields),
                fields=spec.fields,
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

    @ui.route('/ajax/mention_suggestions', methods=['GET'])
    @require_login
    def ajax_mention_suggestions():
        """Return mentionable users (optionally scoped to a channel)."""
        try:
            db_manager, _, _, _, channel_manager, _, _, _, profile_manager, _, _ = _get_app_components_any(current_app)
            user_id = get_current_user()
            channel_id = request.args.get('channel_id')
            query = str(request.args.get('q') or '').strip().lower()
            try:
                limit = int(request.args.get('limit', 500))
            except (TypeError, ValueError):
                limit = 500
            limit = max(1, min(limit, 1000))

            users = []
            if channel_id:
                # Ensure requester is a member of the channel
                with db_manager.get_connection() as conn:
                    row = conn.execute(
                        "SELECT 1 FROM channel_members WHERE channel_id = ? AND user_id = ?",
                        (channel_id, user_id)
                    ).fetchone()
                    if not row:
                        return jsonify({'error': 'Forbidden'}), 403

                members = channel_manager.get_channel_members_list(channel_id) if channel_manager else []
                for m in members:
                    uid = m.get('user_id')
                    if not uid or uid in ('system', 'local_user'):
                        continue
                    # Prefer profile display name (e.g. set via MCP canopy_update_profile) so agents show up correctly
                    display_name = m.get('display_name') or m.get('username') or uid
                    prof = None
                    if profile_manager:
                        prof = profile_manager.get_profile(uid)
                        if prof and (prof.display_name or prof.username):
                            display_name = prof.display_name or prof.username
                    username_val = m.get('username') or uid
                    users.append({
                        'user_id': uid,
                        'username': username_val,
                        'display_name': display_name,
                        'handle': _clean_mention_handle(display_name, username_val, uid),
                        'avatar_url': prof.avatar_url if profile_manager and prof else None,
                        'account_type': (m.get('account_type') or '').strip().lower() or None,
                    })
            else:
                all_info = profile_manager.get_all_users_display_info() if profile_manager else {}
                for uid, info in all_info.items():
                    if uid in ('system', 'local_user'):
                        continue
                    uname = info.get('username') or uid
                    dname = info.get('display_name') or info.get('username') or uid
                    users.append({
                        'user_id': uid,
                        'username': uname,
                        'display_name': dname,
                        'handle': _clean_mention_handle(dname, uname, uid),
                        'avatar_url': info.get('avatar_url'),
                        'account_type': (info.get('account_type') or '').strip().lower() or None,
                    })

            deduped_users = []
            seen_user_ids: set[str] = set()
            for user in users:
                uid = str(user.get('user_id') or '').strip()
                if not uid or uid in ('system', 'local_user') or uid in seen_user_ids:
                    continue
                seen_user_ids.add(uid)
                user['user_id'] = uid
                deduped_users.append(user)
            users = deduped_users

            # Ensure account_type is available for mention-builder UIs
            # (agents vs humans) even when upstream profile/member payloads
            # do not include it.
            user_ids = [u.get('user_id') for u in users if u.get('user_id')]
            if user_ids:
                user_meta_map: dict[str, dict[str, Any]] = {}
                origin_peer_map = {}
                with db_manager.get_connection() as conn:
                    placeholders = ",".join("?" for _ in user_ids)
                    table_cols = set()
                    try:
                        for col_row in conn.execute("PRAGMA table_info(users)").fetchall():
                            name = col_row['name'] if isinstance(col_row, sqlite3.Row) else col_row[1]
                            table_cols.add(str(name))
                    except Exception:
                        table_cols = {'id', 'account_type', 'origin_peer', 'status', 'agent_directives'}

                    select_cols = ['id', 'account_type']
                    if 'origin_peer' in table_cols:
                        select_cols.append('origin_peer')
                    if 'status' in table_cols:
                        select_cols.append('status')
                    if 'agent_directives' in table_cols:
                        select_cols.append('agent_directives')

                    rows = conn.execute(
                        f"SELECT {', '.join(select_cols)} FROM users WHERE id IN ({placeholders})",
                        user_ids
                    ).fetchall()
                    for row in rows:
                        meta = dict(row)
                        uid = str(meta.get('id') or '').strip()
                        if not uid:
                            continue
                        user_meta_map[uid] = meta
                        origin_peer_map[uid] = str(meta.get('origin_peer') or '').strip()

                presence_records = get_agent_presence_records(db_manager=db_manager, user_ids=user_ids)
                for user in users:
                    uid = user.get('user_id')
                    meta = user_meta_map.get(uid) or {}
                    presence_record = presence_records.get(uid) or {}
                    account_type_norm = _normalized_account_type(
                        user.get('account_type') or meta.get('account_type'),
                        status=user.get('status') or meta.get('status'),
                        agent_directives=user.get('agent_directives') or meta.get('agent_directives'),
                        has_presence_checkin=bool(presence_record.get('last_check_in_at')),
                    )
                    user['account_type'] = account_type_norm
                    origin_peer = origin_peer_map.get(uid) or ''
                    is_remote = bool(origin_peer)
                    user['is_remote'] = is_remote
                    if origin_peer:
                        user['origin_peer'] = origin_peer

                    presence = build_agent_presence_payload(
                        last_check_in_at=presence_record.get('last_check_in_at'),
                        is_remote=is_remote,
                        account_type=account_type_norm,
                    )
                    user['last_check_in_at'] = presence.get('last_check_in_at')
                    user['last_check_in_source'] = presence_record.get('last_check_in_source')
                    user['presence_state'] = presence.get('state')
                    user['presence_label'] = presence.get('label')
                    user['presence_color'] = presence.get('color')
                    user['presence_age_seconds'] = presence.get('age_seconds')
                    user['presence_age_text'] = presence.get('age_text')

            def _norm(value: Any) -> str:
                return str(value or '').strip().lower()

            if query:
                ranked = []
                for user in users:
                    display_name = _norm(user.get('display_name'))
                    username_val = _norm(user.get('username'))
                    handle_val = _norm(user.get('handle'))
                    uid_val = _norm(user.get('user_id'))

                    rank = None
                    if handle_val.startswith(query) or username_val.startswith(query):
                        rank = 0
                    elif display_name.startswith(query) or uid_val.startswith(query):
                        rank = 1
                    elif query in handle_val or query in username_val:
                        rank = 2
                    elif query in display_name or query in uid_val:
                        rank = 3

                    if rank is None:
                        continue

                    ranked.append((rank, display_name or username_val or uid_val, user))

                ranked.sort(key=lambda item: (item[0], item[1]))
                users = [item[2] for item in ranked]
            else:
                users.sort(
                    key=lambda user: (
                        _norm(user.get('display_name') or user.get('username') or user.get('user_id')),
                        _norm(user.get('user_id')),
                    )
                )

            users = users[:limit]
            return jsonify({'success': True, 'users': users})
        except Exception as e:
            logger.error(f"Mention suggestions error: {e}")
            return jsonify({'error': 'Internal server error'}), 500

    @ui.route('/ajax/trust/delete_signal', methods=['POST'])
    @require_login
    def ajax_trust_delete_signal():
        """Send a delete signal (session-auth wrapper for the trust page)."""
        try:
            _, _, trust_manager, _, _, _, _, _, _, _, p2p_manager = get_app_components(current_app)
            if not trust_manager:
                return jsonify({'error': 'Trust manager not available'}), 500

            data = request.get_json() or {}
            target_peer_id = (data.get('target_peer_id') or '').strip()
            data_type = (data.get('data_type') or '').strip()
            data_id = (data.get('data_id') or '').strip()
            reason = (data.get('reason') or '').strip() or None

            if not all([target_peer_id, data_type, data_id]):
                return jsonify({'error': 'target_peer_id, data_type, and data_id are required'}), 400

            signal = trust_manager.create_delete_signal(target_peer_id, data_type, data_id, reason)
            if not signal:
                return jsonify({'error': 'Failed to create delete signal'}), 500

            if p2p_manager and p2p_manager.is_running():
                try:
                    target = None if target_peer_id in ('*', 'all') else target_peer_id
                    p2p_manager.broadcast_delete_signal(
                        signal_id=signal.id,
                        data_type=data_type,
                        data_id=data_id,
                        reason=reason,
                        target_peer=target,
                    )
                except Exception as bcast_err:
                    logger.warning(f"P2P broadcast of delete signal failed: {bcast_err}")

            return jsonify({'success': True, 'delete_signal': signal.to_dict()}), 201

        except Exception as e:
            logger.error(f"Delete signal error: {e}")
            return jsonify({'error': 'Internal server error'}), 500

    @ui.route('/static/<path:filename>')
    def serve_static(filename):
        """Serve static files from the ui/static directory (absolute path, cwd-independent)."""
        try:
            from flask import send_from_directory
            # Resolve absolute path from this file so it works regardless of process cwd
            _this_dir = os.path.dirname(os.path.abspath(__file__))
            static_dir = os.path.join(_this_dir, 'static')
            return send_from_directory(static_dir, filename)
        except Exception as e:
            logger.error(f"Static file serve error for {filename}: {e}")
            return "File not found", 404
    
    return ui
