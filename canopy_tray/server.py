"""
ServerManager - manages the Canopy Flask server lifecycle.

Starts the Flask server in a background daemon thread and provides
start/stop/status controls for the tray application.

Author: Konrad Walus (architecture, design, and direction)
Project: Canopy - Local Mesh Communication
License: Apache 2.0
Development: AI-assisted implementation (Claude, Codex, GitHub Copilot, Cursor IDE, Ollama)
"""
from __future__ import annotations

import logging
import os
import sys
import threading
import time
import urllib.request
import urllib.error
import json
from pathlib import Path
from typing import Any, Dict, cast

logger = logging.getLogger(__name__)

# How long to wait for the server to become healthy (seconds)
_STARTUP_TIMEOUT = 30
_HEALTH_POLL_INTERVAL = 0.5

_TRAY_STATE_FILENAME = "tray_state.json"


class ServerManager:
    """Manages the Canopy Flask server in a background thread."""

    def __init__(self, host: str = "127.0.0.1", port: int = 7770):
        self.host = host
        self.port = port
        self._thread: threading.Thread | None = None
        self._app: Any = None
        self._httpd: Any = None
        self._running = False
        self._ready = threading.Event()
        self._error: str | None = None
        self.tray_api_key: str | None = None
        self.tray_user_id: str | None = None

    @property
    def is_running(self) -> bool:
        return (
            self._running
            and self._thread is not None
            and self._thread.is_alive()
            and self._httpd is not None
        )

    @property
    def is_ready(self) -> bool:
        return self._ready.is_set()

    @property
    def url(self) -> str:
        return f"http://localhost:{self.port}"

    @property
    def api_url(self) -> str:
        return f"{self.url}/api/v1"

    def start(self) -> bool:
        """Start the Canopy server in a background thread.
        
        Returns True if the server started successfully.
        """
        if self.is_running:
            logger.info("Server is already running")
            return True

        self._error = None
        self._ready.clear()
        self._running = True
        self._httpd = None

        self._thread = threading.Thread(
            target=self._run_server,
            name="canopy-flask-server",
            daemon=True,
        )
        self._thread.start()

        # Wait for the server to become healthy
        logger.info(f"Waiting for server to start on port {self.port}...")
        if self._ready.wait(timeout=_STARTUP_TIMEOUT):
            logger.info(f"Canopy server is ready at {self.url}")
            return True
        else:
            logger.error(
                f"Server failed to start within {_STARTUP_TIMEOUT}s. "
                f"Error: {self._error or 'timeout'}"
            )
            self._running = False
            return False

    def stop(self) -> None:
        """Stop the HTTP server (and mark the server as down).

        We use a stoppable WSGI server (werkzeug.make_server) so "Stop Server"
        in the tray actually does what it says. Canopy's background threads
        (P2P mesh, TTL maintenance, etc.) are created by `create_app()` and may
        continue running until process exit; "Quit" is the cleanest full stop.
        """
        logger.info("Stopping Canopy server...")
        self._running = False
        self._ready.clear()
        try:
            if self._httpd is not None:
                self._httpd.shutdown()
        except Exception as e:
            logger.debug(f"HTTP shutdown error: {e}")

        if self._thread:
            self._thread.join(timeout=5)
            if self._thread.is_alive():
                logger.warning("Server thread did not exit within 5s — it may still be running")
        self._thread = None
        self._httpd = None

    def health_check(self) -> bool:
        """Check if the server is responding to health requests."""
        try:
            resp = urllib.request.urlopen(
                f"{self.api_url}/health", timeout=3
            )
            return bool(resp.status == 200)
        except Exception:
            return False

    def _run_server(self) -> None:
        """Run the Flask server (called in background thread)."""
        try:
            # Import Canopy components
            from canopy.core.app import create_app
            from canopy.core.config import Config
            from werkzeug.serving import make_server
            from canopy.security.api_keys import Permission

            if self._app is None:
                config = Config.from_env()
                config.network.host = self.host
                config.network.port = self.port
                self._app = create_app(config)

                # Provision a persistent READ_FEED key for tray polling.
                # This keeps the tray monitor working even when the API is locked
                # down for agents (API-key only).
                try:
                    self.tray_api_key = self._ensure_tray_api_key(
                        required_permission=Permission.READ_FEED
                    )
                except Exception as e:
                    logger.warning(f"Tray API key provisioning failed: {e}")
            else:
                # App already exists (e.g. after Stop/Start). Ensure we still have a key.
                if not self.tray_api_key:
                    try:
                        self.tray_api_key = self._ensure_tray_api_key(
                            required_permission=Permission.READ_FEED
                        )
                    except Exception:
                        pass

            # Start a health-check watcher that sets _ready once healthy
            watcher = threading.Thread(
                target=self._watch_for_ready,
                daemon=True,
            )
            watcher.start()

            # Serve the WSGI app with a stoppable in-process server.
            self._httpd = make_server(self.host, self.port, cast(Any, self._app), threaded=True)
            self._httpd.serve_forever()

        except Exception as e:
            self._error = str(e)
            logger.error(f"Server thread crashed: {e}", exc_info=True)
            self._running = False
        finally:
            # If we exit serve_forever(), reflect that in state.
            self._ready.clear()
            self._running = False
            self._httpd = None

    def _watch_for_ready(self) -> None:
        """Poll health endpoint until the server is ready."""
        deadline = time.time() + _STARTUP_TIMEOUT
        while time.time() < deadline and self._running:
            try:
                if self.health_check():
                    self._ready.set()
                    return
            except Exception:
                pass  # Server not yet accepting connections
            time.sleep(_HEALTH_POLL_INTERVAL)

    # ── Tray State / API Key ─────────────────────────────────────────

    def _tray_state_path(self) -> Path:
        """Return the JSON state file path for the tray app."""
        home = os.environ.get("CANOPY_TRAY_HOME")
        if home:
            return Path(home) / _TRAY_STATE_FILENAME
        # Dev fallback: keep state in the current working directory
        return Path(os.getcwd()) / _TRAY_STATE_FILENAME

    def _load_tray_state(self) -> dict:
        path = self._tray_state_path()
        try:
            if path.exists():
                return cast(Dict[str, Any], json.loads(path.read_text(encoding="utf-8")))
        except Exception:
            pass
        return {}

    def _save_tray_state(self, state: dict) -> None:
        path = self._tray_state_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(state, indent=2), encoding="utf-8")
            try:
                os.chmod(path, 0o600)
            except Exception:
                pass
        except Exception as e:
            logger.debug(f"Failed to save tray state: {e}")

    def _ensure_tray_api_key(self, required_permission: Any) -> str | None:
        """Ensure we have a persistent API key for tray polling.

        The key is stored in `tray_state.json` under CANOPY_TRAY_HOME (or CWD in dev).
        """
        if not self._app:
            return None

        api_key_manager = self._app.config.get("API_KEY_MANAGER")
        db_manager = self._app.config.get("DB_MANAGER")
        if not api_key_manager or not db_manager:
            return None

        desired_user_id = db_manager.get_instance_owner_user_id() or "local_user"

        # Prefer existing stored key if it is still valid and matches the desired user.
        state = self._load_tray_state()
        raw_key = (state.get("api_key") or "").strip()
        if raw_key:
            info = api_key_manager.validate_key(raw_key, required_permission=required_permission)
            if info and info.user_id == desired_user_id:
                self.tray_user_id = desired_user_id
                return raw_key

        # Otherwise, generate a new key with minimal permissions for read-only polling.
        raw_key = cast(str | None, api_key_manager.generate_key(
            user_id=desired_user_id,
            permissions=[required_permission],
            expires_days=None,
        ))
        if raw_key:
            self.tray_user_id = desired_user_id
            self._save_tray_state({
                "api_key": raw_key,
                "user_id": desired_user_id,
                "created_at": time.time(),
                "port": self.port,
            })
        return raw_key
