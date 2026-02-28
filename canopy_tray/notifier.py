"""
Notifier - Windows toast notifications for Canopy events.

Uses winotify for native Windows 10/11 toast notifications.
Rate-limited to avoid notification spam.

Author: Konrad Walus (architecture, design, and direction)
Project: Canopy - Local Mesh Communication
License: Apache 2.0
Development: AI-assisted implementation (Claude, Codex, GitHub Copilot, Cursor IDE, Ollama)
"""

import logging
import os
import time
import webbrowser
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Rate limit: max 1 notification per channel per this many seconds
RATE_LIMIT_SECONDS = 30

# Path to the icon for notifications
_ASSETS_DIR = Path(__file__).parent / "assets"
_ICON_PATH = _ASSETS_DIR / "canopy.ico"
_LOGO_PATH = Path(__file__).parent.parent / "logos" / "canopy_notxt.png"


def _get_notification_icon() -> str:
    """Get the absolute path to an icon for toast notifications."""
    # winotify needs an absolute path to an .ico or .png
    for p in [_ICON_PATH, _LOGO_PATH]:
        if p.exists():
            return str(p.resolve())
    return ""


class Notifier:
    """Sends Windows toast notifications for Canopy events."""

    def __init__(self, app_id: str = "Canopy", base_url: str = "http://localhost:7770"):
        self.app_id = app_id
        self.base_url = base_url
        self._rate_limit: dict[str, float] = {}  # key -> last_notification_time
        self._enabled = True
        self._icon_path = _get_notification_icon()

        # Check if winotify is available
        try:
            import winotify
            self._winotify = winotify
            logger.info("Notifier initialized with winotify")
        except ImportError:
            self._winotify = None
            logger.warning(
                "winotify not installed. Toast notifications disabled. "
                "Install with: pip install winotify"
            )

    @property
    def available(self) -> bool:
        """Whether toast notifications are available on this system."""
        return self._winotify is not None and self._enabled

    def enable(self) -> None:
        self._enabled = True

    def disable(self) -> None:
        self._enabled = False

    def _is_rate_limited(self, key: str) -> bool:
        """Check if a notification for this key is rate-limited."""
        last = self._rate_limit.get(key, 0)
        if time.time() - last < RATE_LIMIT_SECONDS:
            return True
        self._rate_limit[key] = time.time()
        return False

    def notify_new_message(
        self,
        channel_name: str,
        sender_name: str,
        content: str,
        channel_id: str = "",
    ) -> None:
        """Show a toast notification for a new channel message."""
        if not self.available:
            return

        key = f"msg:{channel_name}"
        if self._is_rate_limited(key):
            return

        # Truncate long messages
        if len(content) > 120:
            content = content[:117] + "..."

        title = f"#{channel_name}"
        body = f"{sender_name}: {content}"

        try:
            toast = self._winotify.Notification(
                app_id=self.app_id,
                title=title,
                msg=body,
                icon=self._icon_path,
                duration="short",
            )
            # Add action to open the channel in the browser
            if channel_id:
                url = f"{self.base_url}/channels?channel={channel_id}"
                toast.add_actions(label="Open", launch=url)

            toast.show()
            logger.debug(f"Toast: {title} - {body}")

        except Exception as e:
            logger.debug(f"Failed to show toast notification: {e}")

    def notify_peer_connected(self, peer_name: str) -> None:
        """Show a toast notification when a peer connects."""
        if not self.available:
            return

        key = f"peer:connect:{peer_name}"
        if self._is_rate_limited(key):
            return

        try:
            toast = self._winotify.Notification(
                app_id=self.app_id,
                title="Peer Connected",
                msg=f"{peer_name} joined the mesh",
                icon=self._icon_path,
                duration="short",
            )
            toast.show()
            logger.debug(f"Toast: Peer connected - {peer_name}")

        except Exception as e:
            logger.debug(f"Failed to show peer toast: {e}")

    def notify_peer_disconnected(self, peer_name: str) -> None:
        """Show a toast notification when a peer disconnects."""
        if not self.available:
            return

        key = f"peer:disconnect:{peer_name}"
        if self._is_rate_limited(key):
            return

        try:
            toast = self._winotify.Notification(
                app_id=self.app_id,
                title="Peer Disconnected",
                msg=f"{peer_name} left the mesh",
                icon=self._icon_path,
                duration="short",
            )
            toast.show()
            logger.debug(f"Toast: Peer disconnected - {peer_name}")

        except Exception as e:
            logger.debug(f"Failed to show peer toast: {e}")

    def notify_server_error(self, message: str = "Server stopped unexpectedly") -> None:
        """Show a toast notification for server errors."""
        if not self.available:
            return

        key = "server:error"
        if self._is_rate_limited(key):
            return

        try:
            toast = self._winotify.Notification(
                app_id=self.app_id,
                title="Canopy Server",
                msg=message,
                icon=self._icon_path,
                duration="long",
            )
            toast.show()

        except Exception as e:
            logger.debug(f"Failed to show server error toast: {e}")
