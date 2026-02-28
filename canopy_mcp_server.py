#!/usr/bin/env python3
"""
Canopy HTTP MCP Server

HTTP-based MCP server for Canopy that integrates with MCP Manager.
This allows agents to access Canopy via MCP Manager (port 8000).

Usage:
    python canopy_mcp_server.py --port 8030
    python canopy_mcp_server.py --host 0.0.0.0 --port 8030   # For WSL/remote access (e.g. OpenClaw)
"""

import argparse
import base64
import json
import logging
import os
import sys
import secrets
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode

try:
    from urllib.request import Request, urlopen
    from urllib.error import HTTPError, URLError
except ImportError:
    Request = urlopen = HTTPError = URLError = None

# Load .env file if python-dotenv is available
try:
    from dotenv import load_dotenv
    canopy_path = Path(__file__).parent
    env_file = canopy_path / ".env"
    if env_file.exists():
        load_dotenv(env_file)
except ImportError:
    pass  # python-dotenv not installed, skip

# Add Canopy to path
canopy_path = Path(__file__).parent
sys.path.insert(0, str(canopy_path))

# Import MCP server framework
# 1. Try the vendored copy bundled with Canopy (canopy/mcp/)
# 2. Fall back to a pip-installed or PYTHONPATH copy
# 3. Fall back to MCP_FRAMEWORK_PATH environment variable
try:
    from canopy.mcp.mcp_server_framework import MCPHTTPServer
except ImportError:
    try:
        from mcp_server_framework import MCPHTTPServer
    except ImportError:
        _fw_path = os.getenv("MCP_FRAMEWORK_PATH")
        if _fw_path and Path(_fw_path).is_dir():
            sys.path.insert(0, _fw_path)
            from mcp_server_framework import MCPHTTPServer
        else:
            raise ImportError(
                "Cannot import mcp_server_framework.\n"
                "The vendored copy should be at canopy/mcp/mcp_server_framework.py.\n"
                "If missing, try: git pull origin main\n"
                "\n"
                "Alternatively, set MCP_FRAMEWORK_PATH to a directory containing\n"
                "mcp_server_framework.py:\n"
                "  Linux/macOS:  export MCP_FRAMEWORK_PATH=/path/to/infrastructure\n"
                "  Windows:      set MCP_FRAMEWORK_PATH=C:\\path\\to\\infrastructure"
            )

# Import Canopy components
from canopy.core.app import create_app
from canopy.core.utils import get_app_components
from canopy.core.messaging import MessageType
from canopy.core.channels import ChannelType
from canopy.core.mentions import build_preview, resolve_mention_targets
from canopy.security.api_keys import Permission

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stderr)]
)
logger = logging.getLogger("canopy-mcp-http")


class CanopyMCPHTTPServer(MCPHTTPServer):
    """HTTP-based MCP server for Canopy."""
    
    def __init__(self, port: int = 8030, host: str = "localhost", api_key: Optional[str] = None):
        """Initialize Canopy MCP HTTP server.
        
        Args:
            port: Port to run HTTP server on
            host: Host to bind to (default localhost; use 0.0.0.0 for WSL/remote access)
            api_key: API key for authentication (or use CANOPY_API_KEY env var)
        """
        super().__init__("Canopy", "1.0.0", port, host)
        
        # Prefer agent API key if available, otherwise use main API key
        self.api_key = api_key or os.getenv('CANOPY_AGENT_API_KEY') or os.getenv('CANOPY_API_KEY')
        if not self.api_key:
            logger.warning("Warning: CANOPY_API_KEY or CANOPY_AGENT_API_KEY not set. Some tools may not work.")
            logger.warning("   Create API key in Canopy UI: http://localhost:7770 → API Keys")
        
        self.user_id = None
        self.key_info = None
        self.app = None
        self._initialize_canopy()
        self._register_tools()
    
    def _initialize_canopy(self):
        """Initialize Canopy Flask app and authenticate."""
        try:
            # Use a different P2P mesh port so MCP server can run alongside Canopy web app (7771)
            if not os.getenv('CANOPY_MESH_PORT'):
                os.environ['CANOPY_MESH_PORT'] = '7773'
            self.app = create_app()
            with self.app.app_context():
                if self.api_key:
                    (_, api_key_manager, _, _, _, _, _, _, _, _, _) = get_app_components(self.app)

                    # --- Diagnostic: show database key inventory ---
                    try:
                        with api_key_manager.db.get_connection() as conn:
                            row = conn.execute(
                                "SELECT COUNT(*) AS cnt FROM api_keys WHERE revoked = 0"
                            ).fetchone()
                            total_active = row['cnt'] if row else 0
                            logger.info(f"API key database: {total_active} active key(s) in {api_key_manager.db.db_path}")
                    except Exception as diag_err:
                        logger.warning(f"Could not read key inventory: {diag_err}")

                    self.key_info = api_key_manager.validate_key(self.api_key)
                    if self.key_info:
                        self.user_id = self.key_info.user_id
                        perms = sorted(p.value for p in self.key_info.permissions)
                        logger.info(f"Authenticated as user: {self.user_id}")
                        logger.info(f"Key permissions: {', '.join(perms)}")
                    else:
                        logger.warning("API key validation FAILED — tools requiring permissions will not work.")
                        # Detailed diagnostics to help troubleshoot
                        try:
                            import hashlib as _hl
                            key_hash = _hl.sha256(self.api_key.encode()).hexdigest()
                            with api_key_manager.db.get_connection() as conn:
                                row = conn.execute(
                                    "SELECT id, permissions, revoked, expires_at FROM api_keys WHERE key_hash = ?",
                                    (key_hash,),
                                ).fetchone()
                                if row:
                                    logger.warning(
                                        f"  Key hash found (id={row['id']}), "
                                        f"revoked={bool(row['revoked'])}, "
                                        f"expires_at={row['expires_at']}, "
                                        f"permissions={row['permissions']}"
                                    )
                                else:
                                    logger.warning(
                                        f"  Key hash NOT found in database at: "
                                        f"{api_key_manager.db.db_path.absolute()}"
                                    )
                                    logger.warning(
                                        "  This usually means the key was created in a "
                                        "different Canopy instance (different database). "
                                        "Make sure the web server and MCP server share "
                                        "the same data directory."
                                    )
                                    # List first few key IDs to help identify the DB
                                    rows = conn.execute(
                                        "SELECT id, user_id FROM api_keys LIMIT 5"
                                    ).fetchall()
                                    if rows:
                                        for r in rows:
                                            logger.warning(f"  DB key: id={r['id']}, user={r['user_id']}")
                                    else:
                                        logger.warning("  Database has ZERO API keys — is the web server running?")
                        except Exception as inner_err:
                            logger.warning(f"  Diagnostic query failed: {inner_err}")
                else:
                    logger.warning("No API key configured. Set CANOPY_API_KEY env var.")
        except Exception as e:
            logger.error(f"Failed to initialize Canopy: {e}")
            logger.error("   Make sure Canopy is properly installed and database exists")
    
    def _check_permission(self, required_permission: Permission) -> bool:
        """Check if authenticated key has required permission."""
        if not self.key_info:
            return False
        return self.key_info.has_permission(required_permission)

    def _send_channel_message_via_api(
        self,
        channel_id: str,
        content: str,
        attachments_list: List[Dict[str, Any]],
        ttl_seconds: int = 0,
        ttl_mode: str = "",
        expires_at: str = "",
        parent_message_id: str = "",
    ) -> Tuple[bool, Optional[str], Optional[str]]:
        """POST channel message to main Canopy API so P2P broadcast runs. Returns (success, message_id, error)."""
        if not self.api_key or not (Request and urlopen):
            return False, None, "API key or urllib missing"
        base = (os.getenv("CANOPY_API_BASE_URL") or "http://127.0.0.1:7770").rstrip("/")
        url = f"{base}/api/v1/channels/messages"
        body = {
            "channel_id": channel_id,
            "content": content,
            "attachments": attachments_list,
        }
        if ttl_seconds:
            body["ttl_seconds"] = ttl_seconds
        if ttl_mode:
            body["ttl_mode"] = ttl_mode
        if expires_at:
            body["expires_at"] = expires_at
        if parent_message_id:
            body["parent_message_id"] = parent_message_id
        try:
            req = Request(
                url,
                data=json.dumps(body).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "X-API-Key": self.api_key,
                },
                method="POST",
            )
            with urlopen(req, timeout=15) as resp:
                if resp.status != 201:
                    return False, None, f"API returned {resp.status}"
                data = json.loads(resp.read().decode("utf-8"))
                msg = (data or {}).get("message") or {}
                mid = msg.get("id") if isinstance(msg, dict) else None
                return True, mid, None
        except HTTPError as e:
            return False, None, f"API error: {e.code} {e.reason}"
        except URLError as e:
            return False, None, f"API unreachable: {e.reason}"
        except Exception as e:
            return False, None, str(e)

    def _api_call(self, method: str, path: str, body: Optional[dict] = None) -> Tuple[bool, Optional[dict], Optional[Any]]:
        """Make an HTTP request to the main Canopy web server API.

        The MCP server runs its own P2P mesh (port 7773) which is separate
        from the web server's mesh (port 7771).  Other peers connect to the
        web server, not the MCP server — so P2P broadcasts from here never
        reach them.  By proxying write operations through the web server's
        REST API we let *its* P2P mesh handle the broadcast.

        Returns (success, response_dict, error_string).
        """
        if not self.api_key or not (Request and urlopen):
            return False, None, "API key or urllib missing"
        base = (os.getenv("CANOPY_API_BASE_URL") or "http://127.0.0.1:7770").rstrip("/")
        url = f"{base}{path}"
        try:
            data = json.dumps(body).encode("utf-8") if body else None
            req = Request(
                url,
                data=data,
                headers={
                    "Content-Type": "application/json",
                    "X-API-Key": self.api_key,
                },
                method=method,
            )
            with urlopen(req, timeout=15) as resp:
                resp_data = json.loads(resp.read().decode("utf-8"))
                return True, resp_data, None
        except HTTPError as e:
            body_text = ""
            body_json = None
            try:
                body_text = e.read().decode("utf-8", errors="replace")
                try:
                    body_json = json.loads(body_text)
                except Exception:
                    body_json = None
            except Exception:
                pass
            return False, None, {
                "status": e.code,
                "reason": e.reason,
                "body": body_json if body_json is not None else body_text,
            }
        except URLError as e:
            return False, None, f"API unreachable: {e.reason}"
        except Exception as e:
            return False, None, str(e)

    def _create_task_via_api(self, body: dict) -> Tuple[bool, Optional[dict], Optional[str]]:
        """POST task to main Canopy API so the web server handles P2P broadcast."""
        return self._api_call("POST", "/api/v1/tasks", body)

    def _update_task_via_api(self, task_id: str, body: dict) -> Tuple[bool, Optional[dict], Optional[str]]:
        """PATCH task via main Canopy API so the web server handles P2P broadcast."""
        return self._api_call("PATCH", f"/api/v1/tasks/{task_id}", body)

    def _list_objectives_via_api(self, params: dict) -> Tuple[bool, Optional[dict], Optional[str]]:
        """GET objectives via main Canopy API."""
        query = urlencode({k: v for k, v in params.items() if v is not None and v != ""})
        path = "/api/v1/objectives"
        if query:
            path = f"{path}?{query}"
        return self._api_call("GET", path, None)

    def _get_objective_via_api(self, objective_id: str, params: dict) -> Tuple[bool, Optional[dict], Optional[str]]:
        """GET objective via main Canopy API."""
        query = urlencode({k: v for k, v in params.items() if v is not None and v != ""})
        path = f"/api/v1/objectives/{objective_id}"
        if query:
            path = f"{path}?{query}"
        return self._api_call("GET", path, None)

    def _create_objective_via_api(self, body: dict) -> Tuple[bool, Optional[dict], Optional[str]]:
        """POST objective via main Canopy API."""
        return self._api_call("POST", "/api/v1/objectives", body)

    def _update_objective_via_api(self, objective_id: str, body: dict) -> Tuple[bool, Optional[dict], Optional[str]]:
        """PATCH objective via main Canopy API."""
        return self._api_call("PATCH", f"/api/v1/objectives/{objective_id}", body)

    def _add_objective_task_via_api(self, objective_id: str, body: dict) -> Tuple[bool, Optional[dict], Optional[str]]:
        """POST objective task via main Canopy API."""
        return self._api_call("POST", f"/api/v1/objectives/{objective_id}/tasks", body)

    def _list_requests_via_api(self, params: dict) -> Tuple[bool, Optional[dict], Optional[str]]:
        """GET requests via main Canopy API."""
        query = urlencode({k: v for k, v in params.items() if v is not None and v != ""})
        path = "/api/v1/requests"
        if query:
            path = f"{path}?{query}"
        return self._api_call("GET", path, None)

    def _get_request_via_api(self, request_id: str, params: dict) -> Tuple[bool, Optional[dict], Optional[str]]:
        """GET request via main Canopy API."""
        query = urlencode({k: v for k, v in params.items() if v is not None and v != ""})
        path = f"/api/v1/requests/{request_id}"
        if query:
            path = f"{path}?{query}"
        return self._api_call("GET", path, None)

    def _create_request_via_api(self, body: dict) -> Tuple[bool, Optional[dict], Optional[str]]:
        """POST request via main Canopy API."""
        return self._api_call("POST", "/api/v1/requests", body)

    def _update_request_via_api(self, request_id: str, body: dict) -> Tuple[bool, Optional[dict], Optional[str]]:
        """PATCH request via main Canopy API."""
        return self._api_call("PATCH", f"/api/v1/requests/{request_id}", body)

    def _list_signals_via_api(self, params: dict) -> Tuple[bool, Optional[dict], Optional[str]]:
        """GET signals via main Canopy API."""
        query = urlencode({k: v for k, v in params.items() if v is not None and v != ""})
        path = "/api/v1/signals"
        if query:
            path = f"{path}?{query}"
        return self._api_call("GET", path, None)

    def _get_signal_via_api(self, signal_id: str, params: dict) -> Tuple[bool, Optional[dict], Optional[str]]:
        """GET signal via main Canopy API."""
        query = urlencode({k: v for k, v in params.items() if v is not None and v != ""})
        path = f"/api/v1/signals/{signal_id}"
        if query:
            path = f"{path}?{query}"
        return self._api_call("GET", path, None)

    def _create_signal_via_api(self, body: dict) -> Tuple[bool, Optional[dict], Optional[str]]:
        """POST signal via main Canopy API."""
        return self._api_call("POST", "/api/v1/signals", body)

    def _update_signal_via_api(self, signal_id: str, body: dict) -> Tuple[bool, Optional[dict], Optional[str]]:
        """PATCH signal via main Canopy API."""
        return self._api_call("PATCH", f"/api/v1/signals/{signal_id}", body)

    def _lock_signal_via_api(self, signal_id: str, body: dict) -> Tuple[bool, Optional[dict], Optional[str]]:
        """POST signal lock/unlock via main Canopy API."""
        return self._api_call("POST", f"/api/v1/signals/{signal_id}/lock", body)

    def _update_profile_via_api(self, body: dict) -> Tuple[bool, Optional[dict], Optional[str]]:
        """POST profile update via main Canopy API so the web server handles P2P broadcast."""
        return self._api_call("POST", "/api/v1/profile", body)

    def _extract_content_context_via_api(self, body: dict) -> Tuple[bool, Optional[dict], Optional[str]]:
        """POST content-context extraction via main Canopy API."""
        return self._api_call("POST", "/api/v1/content-contexts/extract", body)

    def _list_content_contexts_via_api(self, params: dict) -> Tuple[bool, Optional[dict], Optional[str]]:
        """GET content contexts via main Canopy API."""
        query = urlencode({k: v for k, v in params.items() if v is not None and v != ""})
        path = "/api/v1/content-contexts"
        if query:
            path = f"{path}?{query}"
        return self._api_call("GET", path, None)

    def _get_content_context_via_api(self, context_id: str) -> Tuple[bool, Optional[dict], Optional[str]]:
        """GET content context by ID via main Canopy API."""
        return self._api_call("GET", f"/api/v1/content-contexts/{context_id}", None)

    def _get_content_context_text_via_api(self, context_id: str) -> Tuple[bool, Optional[dict], Optional[str]]:
        """GET content context text endpoint. Falls back to JSON error details if any."""
        # _api_call expects JSON response; use raw urllib call to support text/plain response.
        if not self.api_key or not (Request and urlopen):
            return False, None, "API key or urllib missing"
        base = (os.getenv("CANOPY_API_BASE_URL") or "http://127.0.0.1:7770").rstrip("/")
        url = f"{base}/api/v1/content-contexts/{context_id}/text"
        try:
            req = Request(
                url,
                headers={
                    "X-API-Key": self.api_key,
                },
                method="GET",
            )
            with urlopen(req, timeout=15) as resp:
                text = resp.read().decode("utf-8", errors="replace")
                return True, {"text": text}, None
        except HTTPError as e:
            body_text = ""
            try:
                body_text = e.read().decode("utf-8", errors="replace")
            except Exception:
                body_text = str(e)
            return False, None, f"API error: {e.code} {e.reason} {body_text}".strip()
        except URLError as e:
            return False, None, f"API unreachable: {e.reason}"
        except Exception as e:
            return False, None, str(e)

    def _update_content_context_note_via_api(self, context_id: str, body: dict) -> Tuple[bool, Optional[dict], Optional[str]]:
        """PATCH owner note for content context via main Canopy API."""
        return self._api_call("PATCH", f"/api/v1/content-contexts/{context_id}/note", body)

    def _get_app_components(self):
        """Get Canopy app components."""
        with self.app.app_context():
            return get_app_components(self.app)
    
    def _register_tools(self):
        """Register all Canopy MCP tools."""
        
        @self.tool
        def canopy_send_message(
            content: str,
            recipient_id: str = "",
            file_path: str = ""
        ) -> Dict[str, Any]:
            """Send a DIRECT MESSAGE (DM) only. NOT for channel posts — use canopy_send_channel_message for channels. This stores in the DM table and does NOT broadcast via P2P."""
            if not self.key_info or not self._check_permission(Permission.WRITE_MESSAGES):
                return {"success": False, "error": "Permission denied: write_messages required"}
            
            try:
                with self.app.app_context():
                    (_, _, _, message_manager, _, file_manager, _, _, _, _, _) = self._get_app_components()
                    
                    # Handle file attachment
                    attachments = []
                    message_type = MessageType.TEXT
                    
                    if file_path and Path(file_path).exists():
                        with open(file_path, 'rb') as f:
                            file_data = f.read()
                        
                        file_info = file_manager.save_file(
                            file_data,
                            Path(file_path).name,
                            "application/octet-stream",
                            self.user_id
                        )
                        
                        if file_info:
                            attachments.append({
                                'id': file_info.id,
                                'name': file_info.original_name,
                                'type': file_info.content_type,
                                'size': file_info.size,
                                'url': file_info.url
                            })
                            message_type = MessageType.FILE
                    
                    # Create and send message
                    recipient = recipient_id if recipient_id else None
                    metadata = {'attachments': attachments} if attachments else None
                    
                    message = message_manager.create_message(
                        self.user_id, content, recipient, message_type, metadata
                    )
                    
                    if message and message_manager.send_message(message):
                        return {
                            "success": True,
                            "message_id": message.id,
                            "type": "broadcast" if recipient is None else "direct",
                            "attachments": len(attachments)
                        }
                    else:
                        return {"success": False, "error": "Failed to send message"}
            
            except Exception as e:
                logger.error(f"Error sending message: {e}")
                return {"success": False, "error": str(e)}
        
        @self.tool
        def canopy_get_messages(limit: int = 10, recipient_id: str = "") -> Dict[str, Any]:
            """Get recent messages from Canopy."""
            if not self.key_info or not self._check_permission(Permission.READ_MESSAGES):
                return {"success": False, "error": "Permission denied: read_messages required"}
            
            try:
                with self.app.app_context():
                    (_, _, _, message_manager, _, _, _, _, _, _, _) = self._get_app_components()
                    
                    # Get messages (broadcast or direct)
                    messages = message_manager.get_messages(
                        user_id=self.user_id,
                        limit=limit
                    )
                    
                    # Filter by recipient if specified
                    if recipient_id:
                        messages = [m for m in messages if m.recipient_id == recipient_id or m.sender_id == recipient_id]
                    
                    return {
                        "success": True,
                        "messages": [msg.to_dict() for msg in messages],
                        "count": len(messages)
                    }
            
            except Exception as e:
                logger.error(f"Error getting messages: {e}")
                return {"success": False, "error": str(e)}

        @self.tool
        def canopy_get_mentions(since: str = "", limit: int = 50, include_acknowledged: bool = False) -> Dict[str, Any]:
            """Get mention events for the authenticated user."""
            if not self.key_info or not self._check_permission(Permission.READ_FEED):
                return {"success": False, "error": "Permission denied: read_feed required"}

            try:
                with self.app.app_context():
                    mention_manager = self.app.config.get('MENTION_MANAGER')
                    if not mention_manager:
                        return {"success": True, "mentions": [], "count": 0}

                    events = mention_manager.get_mentions(
                        user_id=self.user_id,
                        since=since or None,
                        limit=limit,
                        include_acknowledged=include_acknowledged,
                    )
                    return {"success": True, "mentions": events, "count": len(events)}
            except Exception as e:
                logger.error(f"Error getting mentions: {e}")
                return {"success": False, "error": str(e)}

        @self.tool
        def canopy_ack_mentions(mention_ids: List[str]) -> Dict[str, Any]:
            """Acknowledge mention events by ID."""
            if not self.key_info or not self._check_permission(Permission.READ_FEED):
                return {"success": False, "error": "Permission denied: read_feed required"}

            if not isinstance(mention_ids, list) or not mention_ids:
                return {"success": False, "error": "mention_ids must be a non-empty list"}

            try:
                with self.app.app_context():
                    mention_manager = self.app.config.get('MENTION_MANAGER')
                    if not mention_manager:
                        return {"success": True, "acknowledged": 0}

                    count = mention_manager.acknowledge_mentions(
                        user_id=self.user_id,
                        mention_ids=mention_ids,
                    )
                    return {"success": True, "acknowledged": count}
            except Exception as e:
                logger.error(f"Error acknowledging mentions: {e}")
                return {"success": False, "error": str(e)}

        @self.tool
        def canopy_extract_content_context(
            source_type: str = "url",
            source_id: str = "",
            url: str = "",
            force_refresh: bool = False,
        ) -> Dict[str, Any]:
            """
            Extract best-effort text context from external content.
            source_type: url | feed_post | channel_message | direct_message.
            For source_type=url, provide url directly.
            For source_type=feed_post/channel_message/direct_message, provide source_id; url is optional override.
            """
            if not self.key_info or not self._check_permission(Permission.READ_FEED):
                return {"success": False, "error": "Permission denied: read_feed required"}

            source_type_norm = (source_type or "").strip().lower()
            if source_type_norm not in ("url", "feed_post", "channel_message", "direct_message"):
                return {"success": False, "error": "source_type must be one of: url, feed_post, channel_message, direct_message"}
            if source_type_norm != "url" and not (source_id or "").strip():
                return {"success": False, "error": "source_id is required for feed_post/channel_message/direct_message"}

            body: Dict[str, Any] = {
                "source_type": source_type_norm,
                "force_refresh": bool(force_refresh),
            }
            if source_id:
                body["source_id"] = source_id.strip()
            if url:
                body["url"] = url.strip()

            try:
                ok, resp, err = self._extract_content_context_via_api(body)
                if ok and resp:
                    return {
                        "success": True,
                        "context": (resp or {}).get("context"),
                        "cached": bool((resp or {}).get("cached")),
                        "extracted": bool((resp or {}).get("extracted", True)),
                    }
                return {"success": False, "error": err or "Context extraction failed"}
            except Exception as e:
                logger.error(f"Error extracting content context: {e}")
                return {"success": False, "error": str(e)}

        @self.tool
        def canopy_list_content_contexts(
            source_type: str = "",
            source_id: str = "",
            source_url: str = "",
            owner_user_id: str = "",
            limit: int = 50,
        ) -> Dict[str, Any]:
            """List stored content-context rows (owner-scoped by default)."""
            if not self.key_info or not self._check_permission(Permission.READ_FEED):
                return {"success": False, "error": "Permission denied: read_feed required"}

            params: Dict[str, Any] = {"limit": max(1, min(int(limit or 50), 200))}
            if source_type:
                params["source_type"] = source_type.strip().lower()
            if source_id:
                params["source_id"] = source_id.strip()
            if source_url:
                params["source_url"] = source_url.strip()
            if owner_user_id:
                params["owner_user_id"] = owner_user_id.strip()

            try:
                ok, resp, err = self._list_content_contexts_via_api(params)
                if ok and resp:
                    contexts = (resp or {}).get("contexts") or []
                    return {"success": True, "contexts": contexts, "count": len(contexts)}
                return {"success": False, "error": err or "Failed to list content contexts"}
            except Exception as e:
                logger.error(f"Error listing content contexts: {e}")
                return {"success": False, "error": str(e)}

        @self.tool
        def canopy_get_content_context(context_id: str, as_text: bool = False) -> Dict[str, Any]:
            """Get one content-context row by ID. Set as_text=true to return the text blob directly."""
            if not self.key_info or not self._check_permission(Permission.READ_FEED):
                return {"success": False, "error": "Permission denied: read_feed required"}
            if not (context_id or "").strip():
                return {"success": False, "error": "context_id required"}

            try:
                context_id = context_id.strip()
                if as_text:
                    ok_txt, resp_txt, err_txt = self._get_content_context_text_via_api(context_id)
                    if ok_txt and resp_txt:
                        return {"success": True, "context_id": context_id, "text": (resp_txt or {}).get("text", "")}
                    return {"success": False, "error": err_txt or "Failed to get context text"}

                ok, resp, err = self._get_content_context_via_api(context_id)
                if ok and resp:
                    return {"success": True, "context": (resp or {}).get("context")}
                return {"success": False, "error": err or "Failed to get content context"}
            except Exception as e:
                logger.error(f"Error getting content context: {e}")
                return {"success": False, "error": str(e)}

        @self.tool
        def canopy_update_content_context_note(context_id: str, owner_note: str) -> Dict[str, Any]:
            """Update the owner note for a content-context row (owner/admin only)."""
            if not self.key_info or not self._check_permission(Permission.WRITE_FEED):
                return {"success": False, "error": "Permission denied: write_feed required"}
            if not (context_id or "").strip():
                return {"success": False, "error": "context_id required"}

            body = {"owner_note": (owner_note or "").strip()}
            try:
                ok, resp, err = self._update_content_context_note_via_api(context_id.strip(), body)
                if ok and resp:
                    return {"success": True, "context": (resp or {}).get("context")}
                return {"success": False, "error": err or "Failed to update content context note"}
            except Exception as e:
                logger.error(f"Error updating content context note: {e}")
                return {"success": False, "error": str(e)}

        @self.tool
        def canopy_get_inbox(status: str = "", limit: int = 50, since: str = "", include_handled: bool = False) -> Dict[str, Any]:
            """Get agent inbox items (pull-first triggers)."""
            if not self.key_info or not self._check_permission(Permission.READ_FEED):
                return {"success": False, "error": "Permission denied: read_feed required"}
            try:
                with self.app.app_context():
                    inbox_manager = self.app.config.get('INBOX_MANAGER')
                    if not inbox_manager:
                        return {"success": True, "items": [], "count": 0}
                    items = inbox_manager.list_items(
                        user_id=self.user_id,
                        status=status or None,
                        limit=limit,
                        since=since or None,
                        include_handled=include_handled,
                    )
                    return {"success": True, "items": items, "count": len(items)}
            except Exception as e:
                logger.error(f"Error getting inbox: {e}")
                return {"success": False, "error": str(e)}

        @self.tool
        def canopy_get_inbox_count(status: str = "") -> Dict[str, Any]:
            """Get count of agent inbox items."""
            if not self.key_info or not self._check_permission(Permission.READ_FEED):
                return {"success": False, "error": "Permission denied: read_feed required"}
            try:
                with self.app.app_context():
                    inbox_manager = self.app.config.get('INBOX_MANAGER')
                    if not inbox_manager:
                        return {"success": True, "count": 0}
                    count = inbox_manager.count_items(
                        user_id=self.user_id,
                        status=status or None,
                    )
                    return {"success": True, "count": count}
            except Exception as e:
                logger.error(f"Error getting inbox count: {e}")
                return {"success": False, "error": str(e)}

        @self.tool
        def canopy_get_inbox_stats(window_hours: int = 24) -> Dict[str, Any]:
            """Get inbox stats (status counts + rejection reasons)."""
            if not self.key_info or not self._check_permission(Permission.READ_FEED):
                return {"success": False, "error": "Permission denied: read_feed required"}
            try:
                with self.app.app_context():
                    inbox_manager = self.app.config.get('INBOX_MANAGER')
                    if not inbox_manager:
                        return {"success": True, "stats": {}}
                    stats = inbox_manager.get_stats(
                        user_id=self.user_id,
                        window_hours=window_hours,
                    )
                    return {"success": True, "stats": stats}
            except Exception as e:
                logger.error(f"Error getting inbox stats: {e}")
                return {"success": False, "error": str(e)}

        @self.tool
        def canopy_get_inbox_audit(limit: int = 50, since: str = "") -> Dict[str, Any]:
            """List recent inbox rejection audit entries."""
            if not self.key_info or not self._check_permission(Permission.READ_FEED):
                return {"success": False, "error": "Permission denied: read_feed required"}
            try:
                with self.app.app_context():
                    inbox_manager = self.app.config.get('INBOX_MANAGER')
                    if not inbox_manager:
                        return {"success": True, "items": [], "count": 0}
                    items = inbox_manager.list_audit(
                        user_id=self.user_id,
                        limit=limit,
                        since=since or None,
                    )
                    return {"success": True, "items": items, "count": len(items)}
            except Exception as e:
                logger.error(f"Error getting inbox audit: {e}")
                return {"success": False, "error": str(e)}

        @self.tool
        def canopy_ack_inbox(ids: List[str], status: str = "handled") -> Dict[str, Any]:
            """Update agent inbox items (mark handled/skipped/pending)."""
            if not self.key_info or not self._check_permission(Permission.READ_FEED):
                return {"success": False, "error": "Permission denied: read_feed required"}
            if not isinstance(ids, list) or not ids:
                return {"success": False, "error": "ids must be a non-empty list"}
            try:
                with self.app.app_context():
                    inbox_manager = self.app.config.get('INBOX_MANAGER')
                    if not inbox_manager:
                        return {"success": True, "updated": 0}
                    updated = inbox_manager.update_items(
                        user_id=self.user_id,
                        ids=ids,
                        status=status,
                    )
                    return {"success": True, "updated": updated}
            except Exception as e:
                logger.error(f"Error updating inbox: {e}")
                return {"success": False, "error": str(e)}

        @self.tool
        def canopy_get_inbox_config() -> Dict[str, Any]:
            """Get agent inbox configuration."""
            if not self.key_info or not self._check_permission(Permission.READ_FEED):
                return {"success": False, "error": "Permission denied: read_feed required"}
            try:
                with self.app.app_context():
                    inbox_manager = self.app.config.get('INBOX_MANAGER')
                    if not inbox_manager:
                        return {"success": True, "config": {}}
                    config = inbox_manager.get_config(self.user_id)
                    return {"success": True, "config": config}
            except Exception as e:
                logger.error(f"Error getting inbox config: {e}")
                return {"success": False, "error": str(e)}

        @self.tool
        def canopy_set_inbox_config(config: Dict[str, Any]) -> Dict[str, Any]:
            """Update agent inbox configuration."""
            if not self.key_info or not self._check_permission(Permission.READ_FEED):
                return {"success": False, "error": "Permission denied: read_feed required"}
            if not isinstance(config, dict) or not config:
                return {"success": False, "error": "config must be a non-empty object"}
            try:
                with self.app.app_context():
                    inbox_manager = self.app.config.get('INBOX_MANAGER')
                    if not inbox_manager:
                        return {"success": True, "config": {}}
                    updated = inbox_manager.set_config(self.user_id, config)
                    return {"success": True, "config": updated}
            except Exception as e:
                logger.error(f"Error updating inbox config: {e}")
                return {"success": False, "error": str(e)}

        @self.tool
        def canopy_get_catchup(since: str = "", window_hours: int = 24, limit: int = 25) -> Dict[str, Any]:
            """Get a catch-up digest (feed, channels, mentions, inbox, tasks, circles, handoffs)."""
            if not self.key_info or not self._check_permission(Permission.READ_FEED):
                return {"success": False, "error": "Permission denied: read_feed required"}
            try:
                with self.app.app_context():
                    (_, _, _, message_manager, channel_manager,
                     _, feed_manager, _, _, _, _) = get_app_components(self.app)
                    mention_manager = self.app.config.get('MENTION_MANAGER')
                    inbox_manager = self.app.config.get('INBOX_MANAGER')
                    task_manager = self.app.config.get('TASK_MANAGER')
                    circle_manager = self.app.config.get('CIRCLE_MANAGER')
                    handoff_manager = self.app.config.get('HANDOFF_MANAGER')

                    def _parse_since(raw: str, hours: int) -> datetime:
                        now = datetime.now(timezone.utc)
                        if raw:
                            s = str(raw).strip()
                            if s:
                                try:
                                    if s.isdigit():
                                        return datetime.fromtimestamp(float(s), tz=timezone.utc)
                                    dt_val = datetime.fromisoformat(s.replace('Z', '+00:00'))
                                    if dt_val.tzinfo is None:
                                        dt_val = dt_val.replace(tzinfo=timezone.utc)
                                    return dt_val
                                except Exception:
                                    pass
                        return now - timedelta(hours=max(1, int(hours or 24)))

                    since_dt = _parse_since(since, window_hours)
                    since_iso = since_dt.isoformat()

                    channels_activity = []
                    if channel_manager:
                        try:
                            channels_activity = channel_manager.get_channel_activity_since(self.user_id, since_dt, limit=limit)
                        except Exception:
                            channels_activity = []

                    feed_items = []
                    if feed_manager:
                        try:
                            posts = feed_manager.get_posts_since(self.user_id, since_dt, limit=limit)
                            for post in posts:
                                feed_items.append({
                                    'post_id': post.id,
                                    'author_id': post.author_id,
                                    'created_at': post.created_at.isoformat() if post.created_at else None,
                                    'visibility': post.visibility.value if hasattr(post.visibility, 'value') else str(post.visibility),
                                    'expires_at': post.expires_at.isoformat() if getattr(post, 'expires_at', None) else None,
                                    'preview': build_preview(post.content or ''),
                                })
                        except Exception:
                            feed_items = []

                    dm_items = []
                    if message_manager and self._check_permission(Permission.READ_MESSAGES):
                        try:
                            messages = message_manager.get_messages(self.user_id, limit=limit, since=since_dt)
                            for msg in messages:
                                dm_items.append({
                                    'message_id': msg.id,
                                    'sender_id': msg.sender_id,
                                    'recipient_id': msg.recipient_id,
                                    'created_at': msg.created_at.isoformat() if msg.created_at else None,
                                    'preview': build_preview(msg.content or ''),
                                    'message_type': msg.message_type.value if hasattr(msg.message_type, 'value') else str(msg.message_type),
                                })
                        except Exception:
                            dm_items = []

                    mention_items = mention_manager.get_mentions(self.user_id, since=since_iso, limit=limit, include_acknowledged=False) if mention_manager else []
                    inbox_items = inbox_manager.list_items(self.user_id, status='pending', limit=limit, since=since_iso, include_handled=False) if inbox_manager else []
                    task_items = task_manager.get_tasks_since(since_iso, limit=limit) if task_manager else []
                    circle_items = [c.to_dict() for c in circle_manager.list_circles_since(since_iso, limit=limit)] if circle_manager else []
                    handoff_items = [h.to_dict() for h in handoff_manager.list_handoffs_since(since_dt, limit=limit, viewer_id=self.user_id)] if handoff_manager else []

                    channel_total = 0
                    for ch in channels_activity:
                        try:
                            channel_total += int(ch.get('new_messages') or 0)
                        except Exception:
                            continue

                    return {
                        "success": True,
                        "since": since_iso,
                        "generated_at": datetime.now(timezone.utc).isoformat(),
                        "channels": {"count": len(channels_activity), "messages_total": channel_total, "items": channels_activity},
                        "feed": {"count": len(feed_items), "items": feed_items},
                        "messages": {"count": len(dm_items), "items": dm_items},
                        "mentions": {"count": len(mention_items), "items": mention_items},
                        "inbox": {"count": len(inbox_items), "items": inbox_items},
                        "tasks": {"count": len(task_items), "items": task_items},
                        "circles": {"count": len(circle_items), "items": circle_items},
                        "handoffs": {"count": len(handoff_items), "items": handoff_items},
                    }
            except Exception as e:
                logger.error(f"Error getting catchup: {e}")
                return {"success": False, "error": str(e)}

        @self.tool
        def canopy_get_session_catchup(since: str = "", window_hours: int = 24, limit: int = 25) -> Dict[str, Any]:
            """Get a session digest (channels, mentions, inbox, circles, tasks, peers)."""
            if not self.key_info or not self._check_permission(Permission.READ_FEED):
                return {"success": False, "error": "Permission denied: read_feed required"}
            try:
                with self.app.app_context():
                    (_, _, _, message_manager, channel_manager,
                     _, feed_manager, _, _, _, p2p_manager) = get_app_components(self.app)
                    mention_manager = self.app.config.get('MENTION_MANAGER')
                    inbox_manager = self.app.config.get('INBOX_MANAGER')
                    task_manager = self.app.config.get('TASK_MANAGER')
                    circle_manager = self.app.config.get('CIRCLE_MANAGER')

                    def _parse_since(raw: str, hours: int) -> datetime:
                        now = datetime.now(timezone.utc)
                        if raw:
                            s = str(raw).strip()
                            if s:
                                try:
                                    if s.isdigit():
                                        return datetime.fromtimestamp(float(s), tz=timezone.utc)
                                    dt_val = datetime.fromisoformat(s.replace('Z', '+00:00'))
                                    if dt_val.tzinfo is None:
                                        dt_val = dt_val.replace(tzinfo=timezone.utc)
                                    return dt_val
                                except Exception:
                                    pass
                        return now - timedelta(hours=max(1, int(hours or 24)))

                    since_dt = _parse_since(since, window_hours)
                    since_iso = since_dt.isoformat()

                    channels_activity = []
                    if channel_manager:
                        try:
                            channels_activity = channel_manager.get_channel_activity_since(self.user_id, since_dt, limit=limit)
                        except Exception:
                            channels_activity = []

                    session_channels = []
                    for ch in channels_activity:
                        session_channels.append({
                            'channel_id': ch.get('channel_id'),
                            'channel_name': ch.get('channel_name'),
                            'new_message_count': ch.get('new_messages'),
                            'latest_message_preview': ch.get('latest_preview') or '',
                        })

                    mention_items = mention_manager.get_mentions(self.user_id, since=since_iso, limit=limit, include_acknowledged=False) if mention_manager else []

                    inbox_items = []
                    inbox_count = 0
                    if inbox_manager:
                        try:
                            stats = inbox_manager.get_stats(self.user_id, window_hours=window_hours)
                            inbox_count = int((stats.get('status_counts') or {}).get('pending', 0))
                        except Exception:
                            inbox_count = 0
                        try:
                            preview_items = inbox_manager.list_items(
                                self.user_id,
                                status='pending',
                                limit=5,
                                since=since_iso,
                                include_handled=False,
                            )
                            for item in preview_items:
                                inbox_items.append({
                                    'id': item.get('id'),
                                    'source_type': item.get('source_type'),
                                    'source_id': item.get('source_id'),
                                    'message_id': item.get('message_id'),
                                    'channel_id': item.get('channel_id'),
                                    'sender_user_id': item.get('sender_user_id'),
                                    'preview': item.get('preview'),
                                    'created_at': item.get('created_at'),
                                    'status': item.get('status'),
                                })
                        except Exception:
                            inbox_items = []

                    circles_digest = []
                    if circle_manager:
                        try:
                            circles = circle_manager.list_circles_since(since_iso, limit=limit)
                            circle_ids = [c.id for c in circles]
                            entry_counts = circle_manager.get_entry_counts_since(since_iso, circle_ids) if circles else {}
                            for c in circles:
                                circles_digest.append({
                                    'circle_id': c.id,
                                    'topic': c.topic,
                                    'phase': c.phase,
                                    'new_entries_count': int(entry_counts.get(c.id, 0)),
                                })
                        except Exception:
                            circles_digest = []

                    tasks_digest = []
                    if task_manager:
                        try:
                            task_items = task_manager.get_tasks_since(since_iso, limit=limit)
                            for task in task_items:
                                tasks_digest.append({
                                    'task_id': task.get('id') or task.get('task_id'),
                                    'title': task.get('title'),
                                    'status': task.get('status'),
                                    'assigned_to': task.get('assigned_to'),
                                })
                        except Exception:
                            tasks_digest = []

                    peers_digest = []
                    if p2p_manager:
                        try:
                            connected = set(p2p_manager.get_connected_peers() or [])
                        except Exception:
                            connected = set()
                        try:
                            local_peer = p2p_manager.get_peer_id()
                        except Exception:
                            local_peer = None
                        known_peers = set()
                        try:
                            known_peers = set((p2p_manager.identity_manager.known_peers or {}).keys())
                        except Exception:
                            known_peers = set()
                        peer_ids = (known_peers | connected)
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
                            peers_digest.append({
                                'peer_id': pid,
                                'device_name': device_name or pid,
                                'connected': pid in connected,
                            })
                        peers_digest.sort(key=lambda p: (not p.get('connected', False), (p.get('device_name') or p.get('peer_id') or '').lower()))

                    digest = {
                        'since': since_iso,
                        'generated_at': datetime.now(timezone.utc).isoformat(),
                        'channels': session_channels,
                        'mentions': mention_items,
                        'inbox': {
                            'pending_count': inbox_count,
                            'items': inbox_items,
                        },
                        'circles': circles_digest,
                        'tasks': tasks_digest,
                        'peers': peers_digest,
                    }

                    return {"success": True, "session": digest}
            except Exception as e:
                logger.error(f"Error getting session catchup: {e}")
                return {"success": False, "error": str(e)}

        @self.tool
        def canopy_get_handoffs(since: str = "", limit: int = 50, channel_id: str = "", author_id: str = "", source_type: str = "") -> Dict[str, Any]:
            """List handoff notes."""
            if not self.key_info or not self._check_permission(Permission.READ_FEED):
                return {"success": False, "error": "Permission denied: read_feed required"}
            try:
                with self.app.app_context():
                    handoff_manager = self.app.config.get('HANDOFF_MANAGER')
                    if not handoff_manager:
                        return {"success": True, "handoffs": [], "count": 0}
                    handoffs = handoff_manager.list_handoffs(
                        limit=limit,
                        since=since or None,
                        channel_id=channel_id or None,
                        author_id=author_id or None,
                        source_type=source_type or None,
                        viewer_id=self.user_id,
                    )
                    return {"success": True, "handoffs": [h.to_dict() for h in handoffs], "count": len(handoffs)}
            except Exception as e:
                logger.error(f"Error getting handoffs: {e}")
                return {"success": False, "error": str(e)}

        @self.tool
        def canopy_search(query: str, limit: int = 50, types: Optional[List[str]] = None) -> Dict[str, Any]:
            """Search local content across posts, channels, tasks, requests, objectives, circles, and handoffs."""
            if not self.key_info or not self._check_permission(Permission.READ_FEED):
                return {"success": False, "error": "Permission denied: read_feed required"}
            if not query or not str(query).strip():
                return {"success": False, "error": "Query is required"}
            try:
                with self.app.app_context():
                    search_manager = self.app.config.get('SEARCH_MANAGER')
                    if not search_manager or not getattr(search_manager, 'enabled', False):
                        return {"success": False, "error": "Local search not available"}
                    results = search_manager.search(
                        query=str(query).strip(),
                        user_id=self.user_id,
                        limit=limit,
                        types=types,
                    )
                    return {"success": True, "results": results, "count": len(results), "query": query}
            except Exception as e:
                logger.error(f"Error running local search: {e}")
                return {"success": False, "error": str(e)}

        @self.tool
        def canopy_discover_skills(name: str = "", tag: str = "", author_id: str = "", limit: int = 100) -> Dict[str, Any]:
            """Discover registered agent skills. Search by name, tag, or author."""
            if not self.key_info or not self._check_permission(Permission.READ_FEED):
                return {"success": False, "error": "Permission denied: read_feed required"}
            try:
                with self.app.app_context():
                    skill_manager = self.app.config.get('SKILL_MANAGER')
                    if not skill_manager:
                        return {"success": False, "error": "Skill manager unavailable"}
                    skills = skill_manager.get_skills(
                        name=name or None, tag=tag or None,
                        author_id=author_id or None, limit=limit,
                    )
                    return {"success": True, "skills": skills, "count": len(skills)}
            except Exception as e:
                logger.error(f"Error discovering skills: {e}")
                return {"success": False, "error": str(e)}

        @self.tool
        def canopy_list_tasks(status: str = "") -> Dict[str, Any]:
            """List collaborative tasks (optionally filter by status)."""
            if not self.key_info or not self._check_permission(Permission.READ_FEED):
                return {"success": False, "error": "Permission denied: read_feed required"}
            try:
                with self.app.app_context():
                    task_manager = self.app.config.get('TASK_MANAGER')
                    if not task_manager:
                        return {"success": False, "error": "Task manager unavailable"}
                    tasks = task_manager.list_tasks(status=status or None)
                    return {"success": True, "tasks": [t.to_dict() for t in tasks], "count": len(tasks)}
            except Exception as e:
                logger.error(f"Error listing tasks: {e}")
                return {"success": False, "error": str(e)}

        @self.tool
        def canopy_create_task(
            title: str,
            description: str = "",
            status: str = "open",
            priority: str = "normal",
            assigned_to: str = "",
            due_at: str = "",
            visibility: str = "network",
        ) -> Dict[str, Any]:
            """Create a collaborative task."""
            if not self.key_info or not self._check_permission(Permission.WRITE_FEED):
                return {"success": False, "error": "Permission denied: write_feed required"}
            if not title:
                return {"success": False, "error": "title required"}
            try:
                # Prefer creating via main Canopy API so P2P broadcast reaches peers
                body: Dict[str, Any] = {"title": title.strip(), "status": status, "priority": priority}
                if description:
                    body["description"] = description.strip()
                if assigned_to:
                    body["assigned_to"] = assigned_to
                if due_at:
                    body["due_at"] = due_at
                if visibility:
                    body["visibility"] = visibility

                ok, resp_data, err = self._create_task_via_api(body)
                if ok and resp_data:
                    task_data = resp_data.get("task") or resp_data
                    return {"success": True, "task": task_data}
                if not ok and err:
                    if isinstance(err, dict) and err.get("status") and int(err.get("status") or 0) < 500:
                        body = err.get("body")
                        if isinstance(body, dict):
                            return {
                                "success": False,
                                "error": body.get("error") or "Task create rejected",
                                "details": body,
                                "status": err.get("status"),
                            }
                        return {
                            "success": False,
                            "error": f"Task create rejected ({err.get('status')})",
                            "details": err,
                        }
                    logger.debug("Task create via API failed (%s), falling back to direct", err)

                # Fallback: create directly (stored but P2P broadcast may not reach peers)
                with self.app.app_context():
                    (_, _, _, _, _, _, _, _, profile_manager, _, p2p_manager) = self._get_app_components()
                    task_manager = self.app.config.get('TASK_MANAGER')
                    if not task_manager:
                        return {"success": False, "error": "Task manager unavailable"}

                    origin_peer = None
                    try:
                        if p2p_manager:
                            origin_peer = p2p_manager.get_peer_id()
                    except Exception:
                        origin_peer = None

                    task = task_manager.create_task(
                        title=title.strip(),
                        description=(description or "").strip() or None,
                        status=status,
                        priority=priority,
                        created_by=self.user_id,
                        assigned_to=assigned_to or None,
                        due_at=due_at or None,
                        visibility=visibility or 'network',
                        origin_peer=origin_peer,
                        source_type='agent',
                        updated_by=self.user_id,
                    )
                    if not task:
                        return {"success": False, "error": "Task creation failed"}

                    if task.visibility == 'network' and p2p_manager and p2p_manager.is_running():
                        try:
                            display_name = None
                            if profile_manager:
                                profile = profile_manager.get_profile(self.user_id)
                                if profile:
                                    display_name = profile.display_name or profile.username
                            p2p_manager.broadcast_interaction(
                                item_id=task.id,
                                user_id=self.user_id,
                                action='task_create',
                                item_type='task',
                                display_name=display_name,
                                extra={'task': task.to_dict()},
                            )
                        except Exception as p2p_err:
                            logger.warning(f"Failed to broadcast task create: {p2p_err}")

                    return {"success": True, "task": task.to_dict()}
            except Exception as e:
                logger.error(f"Error creating task: {e}")
                return {"success": False, "error": str(e)}

        @self.tool
        def canopy_update_task(
            task_id: str,
            title: str = "",
            description: str = "",
            status: str = "",
            priority: str = "",
            assigned_to: str = "",
            due_at: str = "",
            visibility: str = "",
        ) -> Dict[str, Any]:
            """Update a task (status, assignee, priority, title, description)."""
            if not self.key_info or not self._check_permission(Permission.WRITE_FEED):
                return {"success": False, "error": "Permission denied: write_feed required"}
            if not task_id:
                return {"success": False, "error": "task_id required"}
            try:
                clear_tokens = {'none', 'null', 'clear', 'unset', '-', 'n/a'}

                def _is_clear(value: str) -> bool:
                    try:
                        return str(value).strip().lower() in clear_tokens
                    except Exception:
                        return False

                # Build updates dict
                updates: Dict[str, Any] = {}
                if title:
                    updates['title'] = title.strip()
                if description != "":
                    updates['description'] = (description or "").strip() or None
                if status:
                    updates['status'] = status
                if priority:
                    updates['priority'] = priority
                if assigned_to != "":
                    if _is_clear(assigned_to):
                        updates['assigned_to'] = None
                    else:
                        updates['assigned_to'] = assigned_to or None
                if due_at != "":
                    if _is_clear(due_at):
                        updates['due_at'] = None
                    else:
                        updates['due_at'] = due_at or None
                if visibility:
                    updates['visibility'] = visibility

                # Prefer updating via main Canopy API so P2P broadcast reaches peers
                ok, resp_data, err = self._update_task_via_api(task_id, updates)
                if ok and resp_data:
                    task_data = resp_data.get("task") or resp_data
                    return {"success": True, "task": task_data}
                if not ok and err:
                    if isinstance(err, dict) and err.get("status") and int(err.get("status") or 0) < 500:
                        body = err.get("body")
                        if isinstance(body, dict):
                            return {
                                "success": False,
                                "error": body.get("error") or "Task update rejected",
                                "details": body,
                                "status": err.get("status"),
                            }
                        return {
                            "success": False,
                            "error": f"Task update rejected ({err.get('status')})",
                            "details": err,
                        }
                    logger.debug("Task update via API failed (%s), falling back to direct", err)

                # Fallback: update directly (stored but P2P broadcast may not reach peers)
                with self.app.app_context():
                    (_, _, _, _, _, _, _, _, profile_manager, _, p2p_manager) = self._get_app_components()
                    task_manager = self.app.config.get('TASK_MANAGER')
                    if not task_manager:
                        return {"success": False, "error": "Task manager unavailable"}

                    task = task_manager.update_task(task_id, updates, actor_id=self.user_id)
                    if not task:
                        return {"success": False, "error": "Task not found"}

                    if task.visibility == 'network' and p2p_manager and p2p_manager.is_running():
                        try:
                            display_name = None
                            if profile_manager:
                                profile = profile_manager.get_profile(self.user_id)
                                if profile:
                                    display_name = profile.display_name or profile.username
                            p2p_manager.broadcast_interaction(
                                item_id=task.id,
                                user_id=self.user_id,
                                action='task_update',
                                item_type='task',
                                display_name=display_name,
                                extra={'task': task.to_dict()},
                            )
                        except Exception as p2p_err:
                            logger.warning(f"Failed to broadcast task update: {p2p_err}")

                    return {"success": True, "task": task.to_dict()}
            except Exception as e:
                logger.error(f"Error updating task: {e}")
                return {"success": False, "error": str(e)}

        @self.tool
        def canopy_list_objectives(
            status: str = "",
            limit: int = 50,
            include_members: bool = False,
            include_tasks: bool = False,
        ) -> Dict[str, Any]:
            """List objectives (optionally include members and tasks)."""
            if not self.key_info or not self._check_permission(Permission.READ_FEED):
                return {"success": False, "error": "Permission denied: read_feed required"}
            try:
                params = {
                    "status": status or None,
                    "limit": limit,
                    "include_members": "1" if include_members else None,
                    "include_tasks": "1" if include_tasks else None,
                }
                ok, resp, err = self._list_objectives_via_api(params)
                if ok and resp:
                    objectives = resp.get("objectives") if isinstance(resp, dict) else None
                    if objectives is None:
                        objectives = resp
                    return {"success": True, "objectives": objectives, "count": len(objectives or [])}
                if not ok and err:
                    logger.debug("Objective list via API failed (%s), falling back to direct", err)

                with self.app.app_context():
                    objective_manager = self.app.config.get('OBJECTIVE_MANAGER')
                    if not objective_manager:
                        return {"success": False, "error": "Objective manager unavailable"}
                    objectives = objective_manager.list_objectives(limit=limit, status=status or None)
                    if include_members or include_tasks:
                        enriched = []
                        for obj in objectives:
                            full = objective_manager.get_objective(
                                obj.get('id'),
                                include_members=include_members,
                                include_tasks=include_tasks,
                            )
                            if full:
                                enriched.append(full)
                        objectives = enriched
                    return {"success": True, "objectives": objectives, "count": len(objectives)}
            except Exception as e:
                logger.error(f"Error listing objectives: {e}")
                return {"success": False, "error": str(e)}

        @self.tool
        def canopy_get_objective(
            objective_id: str,
            include_members: bool = True,
            include_tasks: bool = True,
        ) -> Dict[str, Any]:
            """Get a single objective by ID, including members and tasks by default."""
            if not self.key_info or not self._check_permission(Permission.READ_FEED):
                return {"success": False, "error": "Permission denied: read_feed required"}
            if not objective_id:
                return {"success": False, "error": "objective_id required"}
            try:
                params = {
                    "include_members": "1" if include_members else None,
                    "include_tasks": "1" if include_tasks else None,
                }
                ok, resp, err = self._get_objective_via_api(objective_id, params)
                if ok and resp:
                    objective = resp.get("objective") if isinstance(resp, dict) else resp
                    return {"success": True, "objective": objective}
                if not ok and err:
                    logger.debug("Objective get via API failed (%s), falling back to direct", err)

                with self.app.app_context():
                    objective_manager = self.app.config.get('OBJECTIVE_MANAGER')
                    if not objective_manager:
                        return {"success": False, "error": "Objective manager unavailable"}
                    obj = objective_manager.get_objective(
                        objective_id,
                        include_members=include_members,
                        include_tasks=include_tasks,
                    )
                    if not obj:
                        return {"success": False, "error": "Objective not found"}
                    return {"success": True, "objective": obj}
            except Exception as e:
                logger.error(f"Error getting objective: {e}")
                return {"success": False, "error": str(e)}

        @self.tool
        def canopy_create_objective(
            title: str,
            description: str = "",
            deadline: str = "",
            status: str = "",
            visibility: str = "network",
            members: list = None,
            tasks: list = None,
        ) -> Dict[str, Any]:
            """Create a new objective with optional members and tasks."""
            if not self.key_info or not self._check_permission(Permission.WRITE_FEED):
                return {"success": False, "error": "Permission denied: write_feed required"}
            if not title:
                return {"success": False, "error": "title required"}
            members = members or []
            tasks = tasks or []
            try:
                body: Dict[str, Any] = {
                    "title": title.strip(),
                    "visibility": visibility or "network",
                }
                if description:
                    body["description"] = description.strip()
                if deadline:
                    body["deadline"] = deadline
                if status:
                    body["status"] = status
                if members:
                    body["members"] = members
                if tasks:
                    body["tasks"] = tasks

                ok, resp, err = self._create_objective_via_api(body)
                if ok and resp:
                    obj = resp.get("objective") if isinstance(resp, dict) else resp
                    return {"success": True, "objective": obj}
                if not ok and err:
                    logger.debug("Objective create via API failed (%s), falling back to direct", err)

                with self.app.app_context():
                    db_manager, _, _, _, _, _, _, _, _, _, p2p_manager = self._get_app_components()
                    objective_manager = self.app.config.get('OBJECTIVE_MANAGER')
                    if not objective_manager:
                        return {"success": False, "error": "Objective manager unavailable"}

                    def _resolve_handle(handle: str) -> Optional[str]:
                        if not handle:
                            return None
                        token = handle.strip()
                        if token.startswith('@'):
                            token = token[1:]
                        if not token:
                            return None
                        row = db_manager.get_user(token)
                        if row:
                            return row.get('id') or token
                        try:
                            targets = resolve_mention_targets(db_manager, [token], author_id=self.user_id)
                            if targets:
                                return targets[0].get('user_id')
                        except Exception:
                            return None
                        return None

                    members_payload = []
                    for member in members or []:
                        if isinstance(member, str):
                            uid = _resolve_handle(member)
                            if uid:
                                members_payload.append({'user_id': uid, 'role': 'contributor'})
                        elif isinstance(member, dict):
                            uid = member.get('user_id') or None
                            if not uid and member.get('handle'):
                                uid = _resolve_handle(member.get('handle'))
                            if uid:
                                members_payload.append({'user_id': uid, 'role': member.get('role') or 'contributor'})

                    tasks_payload = []
                    for task in tasks or []:
                        if not isinstance(task, dict):
                            continue
                        t_title = (task.get('title') or '').strip()
                        if not t_title:
                            continue
                        assigned_to = task.get('assigned_to') or task.get('assignee')
                        if isinstance(assigned_to, str):
                            assigned_to = _resolve_handle(assigned_to)
                        tasks_payload.append({
                            'title': t_title,
                            'status': task.get('status') or 'open',
                            'priority': task.get('priority'),
                            'assigned_to': assigned_to,
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
                        objective_id=f"objective_{secrets.token_hex(8)}",
                        title=title.strip(),
                        description=(description or "").strip() or None,
                        status=status or None,
                        deadline=deadline or None,
                        created_by=self.user_id,
                        visibility=visibility or "network",
                        origin_peer=origin_peer,
                        source_type='mcp',
                        source_id=None,
                        members=members_payload,
                        tasks=tasks_payload,
                        updated_by=self.user_id,
                    )
                    if not obj:
                        return {"success": False, "error": "Objective creation failed"}
                    return {"success": True, "objective": obj}
            except Exception as e:
                logger.error(f"Error creating objective: {e}")
                return {"success": False, "error": str(e)}

        @self.tool
        def canopy_update_objective(
            objective_id: str,
            title: str = "",
            description: str = "",
            deadline: str = "",
            status: str = "",
            visibility: str = "",
            members: list = None,
        ) -> Dict[str, Any]:
            """Update an existing objective."""
            if not self.key_info or not self._check_permission(Permission.WRITE_FEED):
                return {"success": False, "error": "Permission denied: write_feed required"}
            if not objective_id:
                return {"success": False, "error": "objective_id required"}
            members = members if members is not None else None
            try:
                body: Dict[str, Any] = {}
                if title:
                    body["title"] = title.strip()
                if description != "":
                    body["description"] = (description or "").strip() or None
                if deadline:
                    body["deadline"] = deadline
                if status:
                    body["status"] = status
                if visibility:
                    body["visibility"] = visibility
                if members is not None:
                    body["members"] = members

                ok, resp, err = self._update_objective_via_api(objective_id, body)
                if ok and resp:
                    obj = resp.get("objective") if isinstance(resp, dict) else resp
                    return {"success": True, "objective": obj}
                if not ok and err:
                    logger.debug("Objective update via API failed (%s), falling back to direct", err)

                with self.app.app_context():
                    db_manager, _, _, _, _, _, _, _, _, _, _ = self._get_app_components()
                    objective_manager = self.app.config.get('OBJECTIVE_MANAGER')
                    if not objective_manager:
                        return {"success": False, "error": "Objective manager unavailable"}

                    updates = {}
                    if title:
                        updates['title'] = title.strip()
                    if description != "":
                        updates['description'] = (description or "").strip() or None
                    if deadline:
                        updates['deadline'] = deadline
                    if status:
                        updates['status'] = status
                    if visibility:
                        updates['visibility'] = visibility

                    obj = None
                    if updates:
                        obj = objective_manager.update_objective(objective_id, updates, actor_id=self.user_id)

                    if members is not None:
                        def _resolve_handle(handle: str) -> Optional[str]:
                            if not handle:
                                return None
                            token = handle.strip()
                            if token.startswith('@'):
                                token = token[1:]
                            if not token:
                                return None
                            row = db_manager.get_user(token)
                            if row:
                                return row.get('id') or token
                            try:
                                targets = resolve_mention_targets(db_manager, [token], author_id=self.user_id)
                                if targets:
                                    return targets[0].get('user_id')
                            except Exception:
                                return None
                            return None

                        members_payload = []
                        for member in members or []:
                            if isinstance(member, str):
                                uid = _resolve_handle(member)
                                if uid:
                                    members_payload.append({'user_id': uid, 'role': 'contributor'})
                            elif isinstance(member, dict):
                                uid = member.get('user_id') or None
                                if not uid and member.get('handle'):
                                    uid = _resolve_handle(member.get('handle'))
                                if uid:
                                    members_payload.append({'user_id': uid, 'role': member.get('role') or 'contributor'})
                        obj = objective_manager.set_members(objective_id, members_payload, added_by=self.user_id)

                    if not obj:
                        obj = objective_manager.get_objective(objective_id, include_members=True, include_tasks=True)
                    if not obj:
                        return {"success": False, "error": "Objective not found"}
                    return {"success": True, "objective": obj}
            except Exception as e:
                logger.error(f"Error updating objective: {e}")
                return {"success": False, "error": str(e)}

        @self.tool
        def canopy_add_objective_task(
            objective_id: str,
            title: str,
            description: str = "",
            status: str = "open",
            priority: str = "",
            assigned_to: str = "",
            due_at: str = "",
            visibility: str = "",
        ) -> Dict[str, Any]:
            """Add a task to an objective."""
            if not self.key_info or not self._check_permission(Permission.WRITE_FEED):
                return {"success": False, "error": "Permission denied: write_feed required"}
            if not objective_id or not title:
                return {"success": False, "error": "objective_id and title required"}
            try:
                body: Dict[str, Any] = {
                    "title": title.strip(),
                }
                if description:
                    body["description"] = description.strip()
                if status:
                    body["status"] = status
                if priority:
                    body["priority"] = priority
                if assigned_to:
                    body["assigned_to"] = assigned_to
                if due_at:
                    body["due_at"] = due_at
                if visibility:
                    body["visibility"] = visibility

                ok, resp, err = self._add_objective_task_via_api(objective_id, body)
                if ok and resp:
                    tasks = resp.get("tasks") if isinstance(resp, dict) else None
                    if tasks is not None:
                        return {"success": True, "tasks": tasks, "count": len(tasks)}
                    task = resp.get("task") if isinstance(resp, dict) else resp
                    return {"success": True, "task": task}
                if not ok and err:
                    logger.debug("Objective task via API failed (%s), falling back to direct", err)

                with self.app.app_context():
                    db_manager, _, _, _, _, _, _, _, _, _, p2p_manager = self._get_app_components()
                    objective_manager = self.app.config.get('OBJECTIVE_MANAGER')
                    task_manager = self.app.config.get('TASK_MANAGER')
                    if not objective_manager or not task_manager:
                        return {"success": False, "error": "Objective manager unavailable"}
                    obj = objective_manager.get_objective(objective_id, include_members=False, include_tasks=False)
                    if not obj:
                        return {"success": False, "error": "Objective not found"}

                    assigned_id = assigned_to
                    if assigned_to:
                        token = assigned_to
                        if token.startswith('@'):
                            token = token[1:]
                        row = db_manager.get_user(token)
                        if row:
                            assigned_id = row.get('id') or token
                        else:
                            try:
                                targets = resolve_mention_targets(db_manager, [token], author_id=self.user_id)
                                if targets:
                                    assigned_id = targets[0].get('user_id')
                            except Exception:
                                assigned_id = None

                    task = task_manager.create_task(
                        title=title.strip(),
                        description=(description or "").strip() or None,
                        status=status or 'open',
                        priority=priority or None,
                        created_by=self.user_id,
                        assigned_to=assigned_id or None,
                        due_at=due_at or None,
                        visibility=visibility or obj.get('visibility') or 'network',
                        origin_peer=p2p_manager.get_peer_id() if p2p_manager else None,
                        source_type='objective',
                        updated_by=self.user_id,
                        objective_id=objective_id,
                    )
                    if not task:
                        return {"success": False, "error": "Task creation failed"}
                    return {"success": True, "task": task.to_dict()}
            except Exception as e:
                logger.error(f"Error adding objective task: {e}")
                return {"success": False, "error": str(e)}

        @self.tool
        def canopy_list_requests(
            status: str = "",
            priority: str = "",
            tag: str = "",
            limit: int = 50,
            include_members: bool = False,
        ) -> Dict[str, Any]:
            """List requests (structured asks)."""
            if not self.key_info or not self._check_permission(Permission.READ_FEED):
                return {"success": False, "error": "Permission denied: read_feed required"}
            try:
                params = {
                    "status": status or None,
                    "priority": priority or None,
                    "tag": tag or None,
                    "limit": limit,
                    "include_members": "1" if include_members else None,
                }
                ok, resp, err = self._list_requests_via_api(params)
                if ok and resp:
                    requests_list = resp.get("requests") if isinstance(resp, dict) else None
                    if requests_list is None:
                        requests_list = resp
                    return {"success": True, "requests": requests_list, "count": len(requests_list or [])}
                if not ok and err:
                    logger.debug("Request list via API failed (%s), falling back to direct", err)

                with self.app.app_context():
                    request_manager = self.app.config.get('REQUEST_MANAGER')
                    if not request_manager:
                        return {"success": False, "error": "Request manager unavailable"}
                    requests_list = request_manager.list_requests(
                        limit=limit,
                        status=status or None,
                        priority=priority or None,
                        tag=tag or None,
                        include_members=include_members,
                    )
                    return {"success": True, "requests": requests_list, "count": len(requests_list)}
            except Exception as e:
                logger.error(f"Error listing requests: {e}")
                return {"success": False, "error": str(e)}

        @self.tool
        def canopy_get_request(
            request_id: str,
            include_members: bool = True,
        ) -> Dict[str, Any]:
            """Get a single request by ID."""
            if not self.key_info or not self._check_permission(Permission.READ_FEED):
                return {"success": False, "error": "Permission denied: read_feed required"}
            if not request_id:
                return {"success": False, "error": "request_id required"}
            try:
                params = {
                    "include_members": "1" if include_members else None,
                }
                ok, resp, err = self._get_request_via_api(request_id, params)
                if ok and resp:
                    request_data = resp.get("request") if isinstance(resp, dict) else resp
                    return {"success": True, "request": request_data}
                if not ok and err:
                    logger.debug("Request get via API failed (%s), falling back to direct", err)

                with self.app.app_context():
                    request_manager = self.app.config.get('REQUEST_MANAGER')
                    if not request_manager:
                        return {"success": False, "error": "Request manager unavailable"}
                    req = request_manager.get_request(request_id, include_members=include_members)
                    if not req:
                        return {"success": False, "error": "Request not found"}
                    return {"success": True, "request": req}
            except Exception as e:
                logger.error(f"Error getting request: {e}")
                return {"success": False, "error": str(e)}

        @self.tool
        def canopy_create_request(
            title: str,
            request: str = "",
            required_output: str = "",
            status: str = "",
            priority: str = "",
            due_at: str = "",
            tags: Any = None,
            visibility: str = "network",
            members: list = None,
        ) -> Dict[str, Any]:
            """Create a new request with optional members."""
            if not self.key_info or not self._check_permission(Permission.WRITE_FEED):
                return {"success": False, "error": "Permission denied: write_feed required"}
            if not title:
                return {"success": False, "error": "title required"}
            try:
                body: Dict[str, Any] = {
                    "title": title.strip(),
                    "visibility": visibility or "network",
                }
                if request:
                    body["request"] = request.strip()
                if required_output:
                    body["required_output"] = required_output.strip()
                if status:
                    body["status"] = status
                if priority:
                    body["priority"] = priority
                if due_at:
                    body["due_at"] = due_at
                if tags:
                    body["tags"] = tags
                if members:
                    body["members"] = members

                ok, resp, err = self._create_request_via_api(body)
                if ok and resp:
                    request_data = resp.get("request") if isinstance(resp, dict) else resp
                    return {"success": True, "request": request_data}
                if not ok and err:
                    logger.debug("Request create via API failed (%s), falling back to direct", err)

                with self.app.app_context():
                    db_manager = self.app.config.get('DB_MANAGER')
                    request_manager = self.app.config.get('REQUEST_MANAGER')
                    if not request_manager:
                        return {"success": False, "error": "Request manager unavailable"}
                    members_payload = []
                    for member in members or []:
                        if isinstance(member, str):
                            uid = self._resolve_user_id(db_manager, member)
                            if uid:
                                members_payload.append({'user_id': uid, 'role': 'assignee'})
                            continue
                        if isinstance(member, dict):
                            uid = member.get('user_id') or None
                            if not uid and member.get('handle'):
                                uid = self._resolve_user_id(db_manager, member.get('handle'))
                            if uid:
                                members_payload.append({'user_id': uid, 'role': member.get('role') or 'assignee'})

                    req = request_manager.upsert_request(
                        request_id=f"request_{secrets.token_hex(8)}",
                        title=title.strip(),
                        created_by=self.user_id,
                        request_text=request.strip() if request else None,
                        required_output=required_output.strip() if required_output else None,
                        status=status or None,
                        priority=priority or None,
                        tags=tags if tags is not None else None,
                        due_at=due_at or None,
                        visibility=visibility or "network",
                        actor_id=self.user_id,
                        members=members_payload,
                        members_defined=bool(members),
                    )
                    if not req:
                        return {"success": False, "error": "Request creation failed"}
                    return {"success": True, "request": req}
            except Exception as e:
                logger.error(f"Error creating request: {e}")
                return {"success": False, "error": str(e)}

        @self.tool
        def canopy_update_request(
            request_id: str,
            title: str = "",
            request: str = "",
            required_output: str = "",
            status: str = "",
            priority: str = "",
            due_at: str = "",
            tags: Any = None,
            members: list = None,
        ) -> Dict[str, Any]:
            """Update a request."""
            if not self.key_info or not self._check_permission(Permission.WRITE_FEED):
                return {"success": False, "error": "Permission denied: write_feed required"}
            if not request_id:
                return {"success": False, "error": "request_id required"}
            try:
                body: Dict[str, Any] = {}
                if title:
                    body["title"] = title.strip()
                if request:
                    body["request"] = request.strip()
                if required_output:
                    body["required_output"] = required_output.strip()
                if status:
                    body["status"] = status
                if priority:
                    body["priority"] = priority
                if due_at:
                    body["due_at"] = due_at
                if tags is not None:
                    body["tags"] = tags
                if members is not None:
                    body["members"] = members

                ok, resp, err = self._update_request_via_api(request_id, body)
                if ok and resp:
                    request_data = resp.get("request") if isinstance(resp, dict) else resp
                    return {"success": True, "request": request_data}
                if not ok and err:
                    logger.debug("Request update via API failed (%s), falling back to direct", err)

                with self.app.app_context():
                    db_manager = self.app.config.get('DB_MANAGER')
                    request_manager = self.app.config.get('REQUEST_MANAGER')
                    if not request_manager:
                        return {"success": False, "error": "Request manager unavailable"}

                    updates = {}
                    for key in ("title", "request", "required_output", "status", "priority", "due_at", "tags"):
                        if key in body:
                            updates[key] = body.get(key)
                    members_payload = None
                    replace_members = False
                    if members is not None:
                        replace_members = True
                        members_payload = []
                        for member in members or []:
                            if isinstance(member, str):
                                uid = self._resolve_user_id(db_manager, member)
                                if uid:
                                    members_payload.append({'user_id': uid, 'role': 'assignee'})
                                continue
                            if isinstance(member, dict):
                                uid = member.get('user_id') or None
                                if not uid and member.get('handle'):
                                    uid = self._resolve_user_id(db_manager, member.get('handle'))
                                if uid:
                                    members_payload.append({'user_id': uid, 'role': member.get('role') or 'assignee'})

                    req = request_manager.update_request(
                        request_id,
                        updates,
                        actor_id=self.user_id,
                        admin_user_id=db_manager.get_instance_owner_user_id() if db_manager else None,
                        members=members_payload,
                        replace_members=replace_members,
                    )
                    if not req:
                        return {"success": False, "error": "Request not found or not authorized"}
                    return {"success": True, "request": req}
            except Exception as e:
                logger.error(f"Error updating request: {e}")
                return {"success": False, "error": str(e)}

        @self.tool
        def canopy_list_signals(
            status: str = "",
            signal_type: str = "",
            tag: str = "",
            limit: int = 50,
        ) -> Dict[str, Any]:
            """List signals (structured data objects)."""
            if not self.key_info or not self._check_permission(Permission.READ_FEED):
                return {"success": False, "error": "Permission denied: read_feed required"}
            try:
                params = {
                    "status": status or None,
                    "type": signal_type or None,
                    "tag": tag or None,
                    "limit": limit,
                }
                ok, resp, err = self._list_signals_via_api(params)
                if ok and resp:
                    signals = resp.get("signals") if isinstance(resp, dict) else resp
                    return {"success": True, "signals": signals, "count": len(signals or [])}
                if not ok and err:
                    logger.debug("Signal list via API failed (%s), falling back to direct", err)

                with self.app.app_context():
                    db_manager = self.app.config.get('DB_MANAGER')
                    signal_manager = self.app.config.get('SIGNAL_MANAGER')
                    if not signal_manager:
                        return {"success": False, "error": "Signal manager unavailable"}
                    signals = signal_manager.list_signals(limit=limit, status=status or None,
                                                         signal_type=signal_type or None, tag=tag or None)
                    admin_user_id = db_manager.get_instance_owner_user_id() if db_manager else None
                    filtered = []
                    for sig in signals:
                        visibility = (sig.get('visibility') or 'network').lower()
                        if visibility in ('public', 'network'):
                            filtered.append(sig)
                            continue
                        if self.user_id and (sig.get('owner_id') == self.user_id or sig.get('created_by') == self.user_id):
                            filtered.append(sig)
                            continue
                        if admin_user_id and self.user_id == admin_user_id:
                            filtered.append(sig)
                    return {"success": True, "signals": filtered, "count": len(filtered)}
            except Exception as e:
                logger.error(f"Error listing signals: {e}")
                return {"success": False, "error": str(e)}

        @self.tool
        def canopy_get_signal(signal_id: str) -> Dict[str, Any]:
            """Get a signal by ID."""
            if not self.key_info or not self._check_permission(Permission.READ_FEED):
                return {"success": False, "error": "Permission denied: read_feed required"}
            if not signal_id:
                return {"success": False, "error": "signal_id required"}
            try:
                ok, resp, err = self._get_signal_via_api(signal_id, {})
                if ok and resp:
                    sig = resp.get("signal") if isinstance(resp, dict) else resp
                    return {"success": True, "signal": sig}
                if not ok and err:
                    logger.debug("Signal get via API failed (%s), falling back to direct", err)

                with self.app.app_context():
                    db_manager = self.app.config.get('DB_MANAGER')
                    signal_manager = self.app.config.get('SIGNAL_MANAGER')
                    if not signal_manager:
                        return {"success": False, "error": "Signal manager unavailable"}
                    sig = signal_manager.get_signal(signal_id)
                    if not sig:
                        return {"success": False, "error": "Signal not found"}
                    visibility = (sig.get('visibility') or 'network').lower()
                    admin_user_id = db_manager.get_instance_owner_user_id() if db_manager else None
                    if visibility not in ('public', 'network') and self.user_id not in (sig.get('owner_id'), sig.get('created_by'), admin_user_id):
                        return {"success": False, "error": "Not authorized"}
                    return {"success": True, "signal": sig}
            except Exception as e:
                logger.error(f"Error getting signal: {e}")
                return {"success": False, "error": str(e)}

        @self.tool
        def canopy_create_signal(
            title: str,
            summary: str = "",
            signal_type: str = "",
            status: str = "",
            tags: list = None,
            confidence: float = 0.0,
            owner: str = "",
            data: dict = None,
            notes: str = "",
            ttl: str = "",
            expires_at: str = "",
            visibility: str = "network",
        ) -> Dict[str, Any]:
            """Create a new signal."""
            if not self.key_info or not self._check_permission(Permission.WRITE_FEED):
                return {"success": False, "error": "Permission denied: write_feed required"}
            if not title:
                return {"success": False, "error": "title required"}
            try:
                body: Dict[str, Any] = {
                    "title": title.strip(),
                    "visibility": visibility or "network",
                }
                if summary:
                    body["summary"] = summary.strip()
                if signal_type:
                    body["type"] = signal_type
                if status:
                    body["status"] = status
                if tags:
                    body["tags"] = tags
                if confidence:
                    body["confidence"] = confidence
                if owner:
                    body["owner"] = owner
                if data is not None:
                    body["data"] = data
                if notes:
                    body["notes"] = notes
                if ttl:
                    body["ttl"] = ttl
                if expires_at:
                    body["expires_at"] = expires_at

                ok, resp, err = self._create_signal_via_api(body)
                if ok and resp:
                    sig = resp.get("signal") if isinstance(resp, dict) else resp
                    return {"success": True, "signal": sig}
                if not ok and err:
                    logger.debug("Signal create via API failed (%s), falling back to direct", err)

                with self.app.app_context():
                    db_manager, _, _, _, _, _, _, _, _, _, p2p_manager = self._get_app_components()
                    signal_manager = self.app.config.get('SIGNAL_MANAGER')
                    if not signal_manager:
                        return {"success": False, "error": "Signal manager unavailable"}

                    ttl_seconds_val = None
                    ttl_mode_val = None
                    expires_val = expires_at or None
                    if ttl and not expires_val:
                        from canopy.core.signals import _parse_ttl, _parse_dt
                        ttl_token = str(ttl).strip().lower()
                        if ttl_token in ('none', 'no_expiry', 'immortal'):
                            ttl_mode_val = 'no_expiry'
                        else:
                            parsed = _parse_ttl(ttl_token)
                            if parsed:
                                ttl_seconds_val = parsed
                            else:
                                dt = _parse_dt(ttl_token)
                                if dt:
                                    expires_val = dt.isoformat()

                    owner_id = None
                    if owner:
                        token = owner
                        if token.startswith('@'):
                            token = token[1:]
                        row = db_manager.get_user(token)
                        if row:
                            owner_id = row.get('id') or token
                        else:
                            try:
                                targets = resolve_mention_targets(db_manager, [token], author_id=self.user_id)
                                if targets:
                                    owner_id = targets[0].get('user_id')
                            except Exception:
                                owner_id = None

                    sig = signal_manager.upsert_signal(
                        signal_id=f"signal_{secrets.token_hex(8)}",
                        signal_type=signal_type or "signal",
                        title=title.strip(),
                        summary=(summary or "").strip() or None,
                        status=status or None,
                        confidence=confidence or None,
                        tags=tags or [],
                        data=data or None,
                        notes=(notes or "").strip() or None,
                        owner_id=owner_id or self.user_id,
                        created_by=self.user_id,
                        visibility=visibility or "network",
                        origin_peer=p2p_manager.get_peer_id() if p2p_manager else None,
                        source_type="mcp",
                        source_id=None,
                        expires_at=expires_val,
                        ttl_seconds=ttl_seconds_val,
                        ttl_mode=ttl_mode_val,
                        actor_id=self.user_id,
                    )
                    if not sig:
                        return {"success": False, "error": "Signal creation failed"}
                    return {"success": True, "signal": sig}
            except Exception as e:
                logger.error(f"Error creating signal: {e}")
                return {"success": False, "error": str(e)}

        @self.tool
        def canopy_update_signal(
            signal_id: str,
            title: str = "",
            summary: str = "",
            status: str = "",
            tags: list = None,
            confidence: float = 0.0,
            data: dict = None,
            notes: str = "",
            owner: str = "",
            ttl: str = "",
            expires_at: str = "",
        ) -> Dict[str, Any]:
            """Update a signal (or submit a proposal if not owner)."""
            if not self.key_info or not self._check_permission(Permission.WRITE_FEED):
                return {"success": False, "error": "Permission denied: write_feed required"}
            if not signal_id:
                return {"success": False, "error": "signal_id required"}
            try:
                body: Dict[str, Any] = {}
                if title:
                    body["title"] = title.strip()
                if summary != "":
                    body["summary"] = (summary or "").strip() or None
                if status:
                    body["status"] = status
                if tags is not None:
                    body["tags"] = tags
                if confidence:
                    body["confidence"] = confidence
                if data is not None:
                    body["data"] = data
                if notes != "":
                    body["notes"] = (notes or "").strip() or None
                if owner:
                    body["owner"] = owner
                if ttl:
                    body["ttl"] = ttl
                if expires_at:
                    body["expires_at"] = expires_at

                ok, resp, err = self._update_signal_via_api(signal_id, body)
                if ok and resp:
                    if isinstance(resp, dict) and resp.get("proposal"):
                        return {"success": True, "proposal": resp.get("proposal")}
                    sig = resp.get("signal") if isinstance(resp, dict) else resp
                    return {"success": True, "signal": sig}
                if not ok and err:
                    logger.debug("Signal update via API failed (%s), falling back to direct", err)

                with self.app.app_context():
                    db_manager = self.app.config.get('DB_MANAGER')
                    signal_manager = self.app.config.get('SIGNAL_MANAGER')
                    if not signal_manager:
                        return {"success": False, "error": "Signal manager unavailable"}

                    updates = {}
                    if title:
                        updates["title"] = title.strip()
                    if summary != "":
                        updates["summary"] = (summary or "").strip() or None
                    if status:
                        updates["status"] = status
                    if tags is not None:
                        updates["tags"] = tags
                    if confidence:
                        updates["confidence"] = confidence
                    if data is not None:
                        updates["data"] = data
                    if notes != "":
                        updates["notes"] = (notes or "").strip() or None
                    if owner:
                        updates["owner_id"] = owner
                    if ttl:
                        from canopy.core.signals import _parse_ttl, _parse_dt
                        ttl_token = str(ttl).strip().lower()
                        if ttl_token in ('none', 'no_expiry', 'immortal'):
                            updates["ttl_mode"] = 'no_expiry'
                        else:
                            parsed = _parse_ttl(ttl_token)
                            if parsed:
                                updates["ttl_seconds"] = parsed
                            else:
                                dt = _parse_dt(ttl_token)
                                if dt:
                                    updates["expires_at"] = dt.isoformat()
                    if expires_at:
                        updates["expires_at"] = expires_at

                    result = signal_manager.update_signal(signal_id, updates, actor_id=self.user_id)
                    if not result:
                        return {"success": False, "error": "Signal not found"}
                    if isinstance(result, dict) and result.get("proposal_version"):
                        return {"success": True, "proposal": result}
                    return {"success": True, "signal": result}
            except Exception as e:
                logger.error(f"Error updating signal: {e}")
                return {"success": False, "error": str(e)}

        @self.tool
        def canopy_lock_signal(signal_id: str, locked: bool = True) -> Dict[str, Any]:
            """Lock or unlock a signal."""
            if not self.key_info or not self._check_permission(Permission.WRITE_FEED):
                return {"success": False, "error": "Permission denied: write_feed required"}
            if not signal_id:
                return {"success": False, "error": "signal_id required"}
            try:
                ok, resp, err = self._lock_signal_via_api(signal_id, {"locked": bool(locked)})
                if ok and resp:
                    sig = resp.get("signal") if isinstance(resp, dict) else resp
                    return {"success": True, "signal": sig}
                if not ok and err:
                    logger.debug("Signal lock via API failed (%s), falling back to direct", err)

                with self.app.app_context():
                    signal_manager = self.app.config.get('SIGNAL_MANAGER')
                    if not signal_manager:
                        return {"success": False, "error": "Signal manager unavailable"}
                    sig = signal_manager.lock_signal(signal_id, actor_id=self.user_id, locked=bool(locked))
                    if not sig:
                        return {"success": False, "error": "Not found or not authorized"}
                    return {"success": True, "signal": sig}
            except Exception as e:
                logger.error(f"Error locking signal: {e}")
                return {"success": False, "error": str(e)}

        @self.tool
        def canopy_list_circles(limit: int = 50, source_type: str = "", channel_id: str = "") -> Dict[str, Any]:
            """List recent Circle deliberations."""
            if not self.key_info or not self._check_permission(Permission.READ_FEED):
                return {"success": False, "error": "Permission denied: read_feed required"}
            try:
                with self.app.app_context():
                    circle_manager = self.app.config.get('CIRCLE_MANAGER')
                    if not circle_manager:
                        return {"success": False, "error": "Circle manager unavailable"}
                    circles = circle_manager.list_circles(
                        limit=limit,
                        source_type=source_type or None,
                        channel_id=channel_id or None,
                    )
                    payload = []
                    for c in circles:
                        item = c.to_dict()
                        item['entries_count'] = circle_manager.count_entries(c.id)
                        payload.append(item)
                    return {"success": True, "circles": payload, "count": len(payload)}
            except Exception as e:
                logger.error(f"Error listing circles: {e}")
                return {"success": False, "error": str(e)}

        @self.tool
        def canopy_get_circle(circle_id: str, include_entries: bool = False) -> Dict[str, Any]:
            """Get a Circle by ID (optionally include entries)."""
            if not self.key_info or not self._check_permission(Permission.READ_FEED):
                return {"success": False, "error": "Permission denied: read_feed required"}
            if not circle_id:
                return {"success": False, "error": "circle_id required"}
            try:
                with self.app.app_context():
                    circle_manager = self.app.config.get('CIRCLE_MANAGER')
                    if not circle_manager:
                        return {"success": False, "error": "Circle manager unavailable"}
                    circle = circle_manager.get_circle(circle_id)
                    if not circle:
                        return {"success": False, "error": "Circle not found"}
                    resp = {"success": True, "circle": circle.to_dict()}
                    if include_entries:
                        resp["entries"] = circle_manager.list_entries(circle_id)
                    return resp
            except Exception as e:
                logger.error(f"Error getting circle: {e}")
                return {"success": False, "error": str(e)}

        @self.tool
        def canopy_add_circle_entry(circle_id: str, content: str, entry_type: str = "opinion") -> Dict[str, Any]:
            """Add an entry to a Circle (opinion/clarify/summary/decision)."""
            if not self.key_info or not self._check_permission(Permission.WRITE_FEED):
                return {"success": False, "error": "Permission denied: write_feed required"}
            if not circle_id or not content:
                return {"success": False, "error": "circle_id and content required"}
            try:
                # Prefer routing through web server API for P2P broadcast
                ok, resp, err = self._api_call("POST", f"/api/v1/circles/{circle_id}/entries",
                                               {"content": content.strip(), "entry_type": entry_type})
                if ok and resp:
                    entry = resp.get("entry") or resp
                    return {"success": True, "entry": entry}
                if not ok and err:
                    if isinstance(err, dict) and err.get("status") and int(err.get("status") or 0) < 500:
                        body = err.get("body")
                        if isinstance(body, dict):
                            return {
                                "success": False,
                                "error": body.get("error") or "Circle entry rejected",
                                "details": body,
                                "status": err.get("status"),
                            }
                        return {
                            "success": False,
                            "error": f"Circle entry rejected ({err.get('status')})",
                            "details": err,
                        }
                    logger.debug("Circle entry via API failed (%s), falling back to direct", err)

                # Fallback: direct (P2P broadcast may not reach peers)
                with self.app.app_context():
                    db_manager, _, _, _, _, _, _, _, profile_manager, _, p2p_manager = self._get_app_components()
                    circle_manager = self.app.config.get('CIRCLE_MANAGER')
                    if not circle_manager:
                        return {"success": False, "error": "Circle manager unavailable"}
                    entry = circle_manager.add_entry(
                        circle_id=circle_id,
                        user_id=self.user_id,
                        entry_type=entry_type,
                        content=content.strip(),
                        admin_user_id=db_manager.get_instance_owner_user_id(),
                    )
                    if not entry:
                        return {"success": False, "error": "Not authorized or invalid"}

                    circle = circle_manager.get_circle(circle_id)
                    if circle and circle.visibility == 'network' and p2p_manager and p2p_manager.is_running():
                        try:
                            display_name = None
                            if profile_manager:
                                prof = profile_manager.get_profile(self.user_id)
                                if prof:
                                    display_name = prof.display_name or prof.username
                            p2p_manager.broadcast_interaction(
                                item_id=entry['id'],
                                user_id=self.user_id,
                                action='circle_entry',
                                item_type='circle_entry',
                                display_name=display_name,
                                extra={'circle_id': circle_id, 'entry': entry},
                            )
                        except Exception as bcast_err:
                            logger.warning(f"Failed to broadcast circle entry: {bcast_err}")

                    return {"success": True, "entry": entry}
            except Exception as e:
                logger.error(f"Error adding circle entry: {e}")
                return {"success": False, "error": str(e)}

        @self.tool
        def canopy_update_circle_phase(circle_id: str, phase: str) -> Dict[str, Any]:
            """Update Circle phase (facilitator/admin only)."""
            if not self.key_info or not self._check_permission(Permission.WRITE_FEED):
                return {"success": False, "error": "Permission denied: write_feed required"}
            if not circle_id or not phase:
                return {"success": False, "error": "circle_id and phase required"}
            try:
                # Prefer routing through web server API for P2P broadcast
                ok, resp, err = self._api_call("PATCH", f"/api/v1/circles/{circle_id}/phase",
                                               {"phase": phase})
                if ok and resp:
                    circle_data = resp.get("circle") or resp
                    return {"success": True, "circle": circle_data}
                if not ok and err:
                    logger.debug("Circle phase via API failed (%s), falling back to direct", err)

                # Fallback: direct
                with self.app.app_context():
                    db_manager, _, _, _, _, _, _, _, profile_manager, _, p2p_manager = self._get_app_components()
                    circle_manager = self.app.config.get('CIRCLE_MANAGER')
                    if not circle_manager:
                        return {"success": False, "error": "Circle manager unavailable"}
                    circle = circle_manager.update_phase(
                        circle_id=circle_id,
                        new_phase=phase,
                        actor_id=self.user_id,
                        admin_user_id=db_manager.get_instance_owner_user_id(),
                    )
                    if not circle:
                        return {"success": False, "error": "Not authorized or invalid phase"}

                    if circle.visibility == 'network' and p2p_manager and p2p_manager.is_running():
                        try:
                            display_name = None
                            if profile_manager:
                                prof = profile_manager.get_profile(self.user_id)
                                if prof:
                                    display_name = prof.display_name or prof.username
                            p2p_manager.broadcast_interaction(
                                item_id=circle.id,
                                user_id=self.user_id,
                                action='circle_phase',
                                item_type='circle',
                                display_name=display_name,
                                extra={'circle_id': circle.id, 'phase': circle.phase, 'updated_at': circle.updated_at.isoformat()},
                            )
                        except Exception as bcast_err:
                            logger.warning(f"Failed to broadcast circle phase: {bcast_err}")

                    return {"success": True, "circle": circle.to_dict()}
            except Exception as e:
                logger.error(f"Error updating circle phase: {e}")
                return {"success": False, "error": str(e)}

        @self.tool
        def canopy_vote_circle(circle_id: str, option_index: int) -> Dict[str, Any]:
            """Vote on a Circle decision (if decision mode is vote)."""
            if not self.key_info or not self._check_permission(Permission.WRITE_FEED):
                return {"success": False, "error": "Permission denied: write_feed required"}
            if not circle_id:
                return {"success": False, "error": "circle_id required"}
            try:
                # Prefer routing through web server API for P2P broadcast
                ok, resp, err = self._api_call("POST", f"/api/v1/circles/{circle_id}/vote",
                                               {"option_index": int(option_index)})
                if ok and resp:
                    vote_data = resp.get("vote") or resp
                    return {"success": True, "vote": vote_data}
                if not ok and err:
                    logger.debug("Circle vote via API failed (%s), falling back to direct", err)

                # Fallback: direct
                with self.app.app_context():
                    (_, _, _, _, _, _, _, _, profile_manager, _, p2p_manager) = self._get_app_components()
                    circle_manager = self.app.config.get('CIRCLE_MANAGER')
                    if not circle_manager:
                        return {"success": False, "error": "Circle manager unavailable"}
                    vote = circle_manager.record_vote(circle_id, self.user_id, int(option_index))
                    if not vote:
                        return {"success": False, "error": "Not authorized or invalid vote"}

                    circle = circle_manager.get_circle(circle_id)
                    if circle and circle.visibility == 'network' and p2p_manager and p2p_manager.is_running():
                        try:
                            display_name = None
                            if profile_manager:
                                prof = profile_manager.get_profile(self.user_id)
                                if prof:
                                    display_name = prof.display_name or prof.username
                            p2p_manager.broadcast_interaction(
                                item_id=circle.id,
                                user_id=self.user_id,
                                action='circle_vote',
                                item_type='circle',
                                display_name=display_name,
                                extra={'circle_id': circle.id, 'option_index': int(option_index), 'created_at': datetime.now(timezone.utc).isoformat()},
                            )
                        except Exception as bcast_err:
                            logger.warning(f"Failed to broadcast circle vote: {bcast_err}")

                    return {"success": True, "vote": vote}
            except Exception as e:
                logger.error(f"Error voting on circle: {e}")
                return {"success": False, "error": str(e)}
        
        @self.tool
        def canopy_list_channels() -> Dict[str, Any]:
            """Get list of available Canopy channels."""
            if not self.key_info or not self._check_permission(Permission.READ_FEED):
                return {"success": False, "error": "Permission denied: read_feed required"}
            
            try:
                with self.app.app_context():
                    (_, _, _, _, channel_manager, _, _, _, _, _, _) = self._get_app_components()
                    
                    channels = channel_manager.get_user_channels(self.user_id)
                    
                    return {
                        "success": True,
                        "channels": [ch.to_dict() for ch in channels],
                        "count": len(channels)
                    }
            
            except Exception as e:
                logger.error(f"Error listing channels: {e}")
                return {"success": False, "error": str(e)}
        
        @self.tool
        def canopy_send_channel_message(
            channel_id: str,
            content: str,
            file_path: str = "",
            attachments: str = "",
            ttl_seconds: int = 0,
            ttl_mode: str = "",
            expires_at: str = "",
            parent_message_id: str = ""
        ) -> Dict[str, Any]:
            """Send message to a specific channel. Optional: file_path (local path to upload and attach), attachments (JSON array of already-uploaded files: [{\"id\": \"file_id\", \"name\": \"audio.mp3\", \"type\": \"audio/mpeg\"}] — use file IDs from canopy_upload_file for audio/images/docs), ttl_seconds, ttl_mode ('no_expiry'/'quarter'), expires_at (ISO), parent_message_id (reply threading)."""
            if not self.key_info or not self._check_permission(Permission.WRITE_FEED):
                return {"success": False, "error": "Permission denied: write_feed required"}
            
            try:
                with self.app.app_context():
                    (_, _, _, _, channel_manager, file_manager, _, _, _, _, _) = self._get_app_components()
                    
                    # Build attachments list
                    attachments_list: List[Dict[str, Any]] = []
                    message_type = MessageType.TEXT
                    
                    # 1) Local file_path: upload and attach
                    if file_path and Path(file_path).exists():
                        with open(file_path, 'rb') as f:
                            file_data = f.read()
                        file_info = file_manager.save_file(
                            file_data,
                            Path(file_path).name,
                            "application/octet-stream",
                            self.user_id
                        )
                        if file_info:
                            attachments_list.append({
                                'id': file_info.id,
                                'name': file_info.original_name,
                                'type': file_info.content_type or "application/octet-stream",
                            })
                            message_type = MessageType.FILE
                    
                    # 2) attachments param: already-uploaded files (JSON array of {"id","name","type"} or list of file_id strings)
                    if attachments and (attachments.strip() if isinstance(attachments, str) else True):
                        try:
                            parsed = json.loads(attachments) if isinstance(attachments, str) else attachments
                            if isinstance(parsed, list):
                                for item in parsed:
                                    if isinstance(item, str) and item.strip():
                                        fi = file_manager.get_file(item.strip())
                                        if fi and fi.uploaded_by == self.user_id:
                                            attachments_list.append({
                                                "id": fi.id,
                                                "name": fi.original_name,
                                                "type": fi.content_type or "application/octet-stream",
                                            })
                                            message_type = MessageType.FILE
                                    elif isinstance(item, dict) and item.get("id"):
                                        att = {"id": item["id"], "name": item.get("name") or item["id"], "type": item.get("type") or "application/octet-stream"}
                                        fi = file_manager.get_file(item["id"])
                                        if fi and fi.uploaded_by == self.user_id:
                                            att["name"] = att.get("name") or fi.original_name
                                            att["type"] = att.get("type") or fi.content_type or "application/octet-stream"
                                        attachments_list.append(att)
                                        message_type = MessageType.FILE
                        except (json.JSONDecodeError, TypeError):
                            pass
                    
                    # Prefer sending via main Canopy API so P2P broadcast runs (peers get message + file)
                    ok, message_id, err = self._send_channel_message_via_api(
                        channel_id=channel_id,
                        content=content,
                        attachments_list=attachments_list,
                        ttl_seconds=ttl_seconds or 0,
                        ttl_mode=ttl_mode or "",
                        expires_at=expires_at or "",
                        parent_message_id=parent_message_id or "",
                    )
                    if ok and message_id:
                        return {
                            "success": True,
                            "message_id": message_id,
                            "channel_id": channel_id,
                            "attachments": len(attachments_list),
                        }
                    if not ok and err:
                        logger.debug("Channel message via API failed (%s), falling back to direct send", err)

                    # Fallback: send directly (message stored but no P2P broadcast)
                    kw: Dict[str, Any] = {
                        "channel_id": channel_id,
                        "user_id": self.user_id,
                        "content": content,
                        "message_type": message_type,
                        "attachments": attachments_list,
                    }
                    if ttl_seconds:
                        kw["ttl_seconds"] = ttl_seconds
                    if ttl_mode:
                        kw["ttl_mode"] = ttl_mode
                    if expires_at:
                        kw["expires_at"] = expires_at
                    if parent_message_id:
                        kw["parent_message_id"] = parent_message_id
                    message = channel_manager.send_message(**kw)
                    
                    if message:
                        return {
                            "success": True,
                            "message_id": message.id,
                            "channel_id": channel_id,
                            "attachments": len(attachments_list),
                        }
                    return {"success": False, "error": "Failed to send message to channel"}
            
            except Exception as e:
                logger.error(f"Error sending channel message: {e}")
                return {"success": False, "error": str(e)}
        
        @self.tool
        def canopy_get_channel_messages(channel_id: str, limit: int = 20) -> Dict[str, Any]:
            """Get messages from a specific channel."""
            if not self.key_info or not self._check_permission(Permission.READ_FEED):
                return {"success": False, "error": "Permission denied: read_feed required"}
            
            try:
                with self.app.app_context():
                    (_, _, _, _, channel_manager, _, _, _, _, _, _) = self._get_app_components()
                    
                    messages = channel_manager.get_channel_messages(
                        channel_id=channel_id,
                        user_id=self.user_id,
                        limit=limit
                    )
                    
                    return {
                        "success": True,
                        "channel_id": channel_id,
                        "messages": [msg.to_dict() for msg in messages],
                        "count": len(messages)
                    }
            
            except Exception as e:
                logger.error(f"Error getting channel messages: {e}")
                return {"success": False, "error": str(e)}
        
        @self.tool
        def canopy_create_channel(
            name: str,
            description: str = "",
            channel_type: str = "public"
        ) -> Dict[str, Any]:
            """Create a new channel."""
            if not self.key_info or not self._check_permission(Permission.WRITE_FEED):
                return {"success": False, "error": "Permission denied: write_feed required"}
            
            try:
                with self.app.app_context():
                    (_, _, _, _, channel_manager, _, _, _, _, _, _) = self._get_app_components()
                    
                    channel_type_enum = ChannelType.PUBLIC if channel_type == "public" else ChannelType.PRIVATE
                    
                    channel = channel_manager.create_channel(
                        name=name,
                        channel_type=channel_type_enum,
                        created_by=self.user_id,
                        description=description
                    )
                    
                    if channel:
                        return {
                            "success": True,
                            "channel_id": channel.id,
                            "name": channel.name,
                            "type": channel_type
                        }
                    else:
                        return {"success": False, "error": "Failed to create channel"}
            
            except Exception as e:
                logger.error(f"Error creating channel: {e}")
                return {"success": False, "error": str(e)}
        
        @self.tool
        def canopy_upload_file(
            file_path: str = "",
            file_content_base64: str = "",
            filename: str = "",
            content_type: str = "application/octet-stream",
            description: str = "",
        ) -> Dict[str, Any]:
            """Upload a file to Canopy for sharing. Use either:
            - file_path: path to a file on the machine where the MCP server runs (e.g. Windows).
            - file_content_base64 + filename: send file content as base64 (use when the file lives on another host, e.g. agent in WSL, MCP on Windows). Optional content_type (e.g. audio/mpeg for .mp3).
            """
            if not self.key_info or not self._check_permission(Permission.WRITE_FILES):
                return {"success": False, "error": "Permission denied: write_files required"}
            
            try:
                file_data = None
                original_name = ""
                mime_type = content_type or "application/octet-stream"

                if file_content_base64 and filename:
                    try:
                        file_data = base64.b64decode(file_content_base64)
                    except Exception as e:
                        return {"success": False, "error": f"Invalid base64: {e}"}
                    original_name = filename.strip() or "upload"
                elif file_path:
                    path = Path(file_path)
                    if not path.exists():
                        return {"success": False, "error": f"File not found: {file_path}. Tip: if the file is on another machine (e.g. WSL), use file_content_base64 and filename instead."}
                    with open(path, "rb") as f:
                        file_data = f.read()
                    original_name = path.name
                else:
                    return {"success": False, "error": "Provide either file_path or (file_content_base64 and filename)."}

                with self.app.app_context():
                    (_, _, _, _, _, file_manager, _, _, _, _, _) = self._get_app_components()
                    file_info = file_manager.save_file(
                        file_data,
                        original_name,
                        mime_type,
                        self.user_id,
                    )
                    if file_info:
                        return {
                            "success": True,
                            "file_id": file_info.id,
                            "name": file_info.original_name,
                            "size": file_info.size,
                            "url": file_info.url,
                        }
                    return {"success": False, "error": "Failed to upload file"}
            except Exception as e:
                logger.error(f"Error uploading file: {e}")
                return {"success": False, "error": str(e)}
        
        @self.tool
        def canopy_get_profile(user_id: str = "") -> Dict[str, Any]:
            """Get user profile information."""
            try:
                with self.app.app_context():
                    (_, _, _, _, _, _, _, _, profile_manager, _, _) = self._get_app_components()
                    
                    target_user_id = user_id if user_id else self.user_id
                    profile = profile_manager.get_profile(target_user_id)
                    
                    if profile:
                        return {
                            "success": True,
                            "profile": profile.to_dict()
                        }
                    else:
                        return {"success": False, "error": "Profile not found"}
            
            except Exception as e:
                logger.error(f"Error getting profile: {e}")
                return {"success": False, "error": str(e)}

        @self.tool
        def canopy_update_profile(
            display_name: str = "",
            bio: str = "",
            avatar_file_id: str = "",
        ) -> Dict[str, Any]:
            """Update your Canopy profile (display name, bio, avatar). Use this to build your character.
            For avatar: first upload an image with canopy_upload_file (or file_content_base64 + filename),
            then pass the returned file_id here as avatar_file_id. You can set one or more fields at once."""
            if not self.key_info:
                return {"success": False, "error": "Not authenticated"}
            try:
                # Build updates
                updates: Dict[str, Any] = {}
                if display_name is not None and (display_name := (display_name or "").strip()):
                    updates["display_name"] = display_name
                if bio is not None:
                    updates["bio"] = (bio or "").strip() or None
                if avatar_file_id is not None and (avatar_file_id := (avatar_file_id or "").strip()):
                    updates["avatar_file_id"] = avatar_file_id
                if not updates:
                    return {"success": False, "error": "Provide at least one of display_name, bio, avatar_file_id"}

                # Prefer updating via main Canopy API so P2P broadcast reaches peers
                ok, resp_data, err = self._update_profile_via_api(updates)
                if ok:
                    return {"success": True, "updated": list(updates.keys())}
                if err:
                    logger.debug("Profile update via API failed (%s), falling back to direct", err)

                # Fallback: update directly (stored but P2P broadcast may not reach peers)
                with self.app.app_context():
                    (_, _, _, _, _, _, _, _, profile_manager, _, p2p_manager) = self._get_app_components()
                    if not profile_manager.update_profile(self.user_id, **updates):
                        return {"success": False, "error": "Profile update failed"}
                    try:
                        if p2p_manager and p2p_manager.is_running():
                            card = profile_manager.get_profile_card(self.user_id)
                            if card:
                                p2p_manager.broadcast_profile_update(card)
                    except Exception as bcast_err:
                        logger.warning(f"Profile broadcast failed: {bcast_err}")
                    return {"success": True, "updated": list(updates.keys())}
            except Exception as e:
                logger.error(f"Error updating profile: {e}")
                return {"success": False, "error": str(e)}
        
        @self.tool
        def canopy_get_status() -> Dict[str, Any]:
            """Get Canopy system status and recent activity."""
            try:
                with self.app.app_context():
                    (_, _, _, message_manager, channel_manager, _, _, _, profile_manager, _, _) = self._get_app_components()
                    
                    # Get recent messages
                    recent_messages = message_manager.get_messages(self.user_id, 5) if self.user_id else []
                    
                    # Get channels
                    channels = channel_manager.get_user_channels(self.user_id) if self.user_id else []
                    
                    # Get profile
                    profile = profile_manager.get_profile(self.user_id) if self.user_id else None
                    
                    return {
                        "success": True,
                        "user_id": self.user_id,
                        "display_name": profile.display_name if profile else "Unknown",
                        "recent_messages": len(recent_messages),
                        "channels_count": len(channels),
                        "channels": [ch.name for ch in channels[:5]],
                        "authenticated": self.key_info is not None
                    }
            
            except Exception as e:
                logger.error(f"Error getting status: {e}")
                return {"success": False, "error": str(e)}
        
        # New team-focused tools
        @self.tool
        def canopy_send_to_team(content: str, priority: str = "normal") -> Dict[str, Any]:
            """Send message to entire team (posts to #general channel)."""
            if not self.key_info or not self._check_permission(Permission.WRITE_FEED):
                return {"success": False, "error": "Permission denied: write_feed required"}
            
            try:
                with self.app.app_context():
                    (_, _, _, _, channel_manager, _, _, _, _, _, _) = self._get_app_components()
                    
                    # Find or create #general channel
                    channels = channel_manager.get_user_channels(user_id=self.user_id)
                    general_channel = None
                    for ch in channels:
                        if ch.name.lower() == "general":
                            general_channel = ch
                            break
                    
                    if not general_channel:
                        # Create #general channel if it doesn't exist
                        general_channel = channel_manager.create_channel(
                            name="general",
                            channel_type=ChannelType.PUBLIC,
                            created_by=self.user_id,
                            description="General team communication"
                        )
                    
                    # Send message with priority in attachments metadata
                    attachments = [{"priority": priority}] if priority != "normal" else None
                    message = channel_manager.send_message(
                        channel_id=general_channel.id,
                        user_id=self.user_id,
                        content=content,
                        attachments=attachments
                    )
                    
                    if message:
                        return {
                            "success": True,
                            "message_id": message.id,
                            "channel": "general",
                            "priority": priority
                        }
                    else:
                        return {"success": False, "error": "Failed to send team message"}
            
            except Exception as e:
                logger.error(f"Error sending team message: {e}")
                return {"success": False, "error": str(e)}
        
        logger.info("Registered all Canopy MCP tools")


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(description='Canopy HTTP MCP Server')
    parser.add_argument('--port', type=int, default=8030, help='Port to run server on (default: 8030)')
    parser.add_argument('--host', type=str, default='localhost',
                        help='Host to bind to (default: localhost; use 0.0.0.0 for WSL/remote access)')
    parser.add_argument('--api-key', type=str, help='Canopy API key (or use CANOPY_API_KEY env var)')
    args = parser.parse_args()
    
    logger.info("Starting Canopy HTTP MCP Server")
    logger.info(f"   Host: {args.host}, Port: {args.port}")
    agent_key = os.getenv('CANOPY_AGENT_API_KEY')
    main_key = os.getenv('CANOPY_API_KEY')
    key_status = "Agent key" if agent_key else ("Main key" if main_key else "Not set")
    logger.info(f"   API Key: {key_status} (some tools may not work if not set)")
    
    server = CanopyMCPHTTPServer(port=args.port, host=args.host, api_key=args.api_key)
    server.run()


if __name__ == "__main__":
    main()
