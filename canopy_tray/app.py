"""
TrayApp - main orchestrator for the Canopy system tray application.

Creates a pystray icon with a context menu, manages the server lifecycle,
monitors status, and dispatches notifications.

Author: Konrad Walus (architecture, design, and direction)
Project: Canopy - Local Mesh Communication
License: Apache 2.0
Development: AI-assisted implementation (Claude, Codex, GitHub Copilot, Cursor IDE, Ollama)
"""

import logging
import os
import sys
import subprocess
import threading
import time
import webbrowser
from typing import Any, Optional

import pystray
from pystray import MenuItem, Menu

from .server import ServerManager
from .monitor import StatusMonitor, PeerInfo, ChannelMessage
from .notifier import Notifier
from .icons import (
    get_icon_connected,
    get_icon_disconnected,
    get_icon_error,
    get_icon_with_badge,
)

logger = logging.getLogger(__name__)


class TrayApp:
    """Canopy system tray application."""

    def __init__(self, host: str = "127.0.0.1", port: int = 7770):
        self.host = host
        self.port = port

        # Components
        self.server = ServerManager(host=host, port=port)
        self.monitor = StatusMonitor(api_base=f"http://localhost:{port}/api/v1")
        self.notifier = Notifier(
            app_id="Canopy",
            base_url=f"http://localhost:{port}",
        )

        # State
        self._icon: Optional[pystray.Icon] = None
        self._has_unread = False
        self._previous_peers: dict[str, str] = {}  # peer_id -> status

        # Wire up monitor callbacks
        self.monitor.on_peer_change = self._on_peer_change
        self.monitor.on_new_message = self._on_new_message
        self.monitor.on_status_update = self._on_status_update
        self.monitor.on_server_down = self._on_server_down

    def run(self) -> None:
        """Start the tray app (blocking -- runs pystray in the main thread)."""
        logger.info("Starting Canopy Tray App...")

        # Start the Flask server
        if not self.server.start():
            logger.error("Failed to start Canopy server. Showing error icon.")
            self._run_tray(initial_state="error")
            return

        # Attach the tray's polling key (required for channels/messages endpoints).
        if self.server.tray_api_key:
            self.monitor.set_api_key(self.server.tray_api_key)
        else:
            logger.warning(
                "Tray API key not available yet; message notifications may be disabled. "
                "Open Canopy UI and ensure at least one user account exists."
            )
            try:
                self.notifier.notify_server_error(
                    "Canopy is running, but tray notifications are disabled until a user is registered."
                )
            except Exception:
                pass

        # Start the status monitor
        self.monitor.start()

        # Run the tray icon (blocks until quit)
        self._run_tray(initial_state="disconnected")

    def _run_tray(self, initial_state: str = "disconnected") -> None:
        """Create and run the pystray icon."""
        if initial_state == "error":
            icon_image = get_icon_error()
        elif initial_state == "connected":
            icon_image = get_icon_connected()
        else:
            icon_image = get_icon_disconnected()

        self._icon = pystray.Icon(
            name="Canopy",
            icon=icon_image,
            title="Canopy - Starting...",
            menu=self._build_menu(),
        )

        # pystray.run() blocks until stop() is called
        self._icon.run(setup=self._on_tray_setup)

    def _on_tray_setup(self, icon: pystray.Icon) -> None:
        """Called when the tray icon is ready."""
        icon.visible = True
        logger.info("Tray icon visible")

    def _build_menu(self) -> Menu:
        """Build the right-click context menu."""
        return Menu(
            MenuItem(
                self._status_text,
                None,
                enabled=False,
            ),
            Menu.SEPARATOR,
            MenuItem(
                "Open Canopy UI",
                self._action_open_ui,
                default=True,  # Double-click action
            ),
            MenuItem(
                "Open Canopy Folder",
                self._action_open_canopy_folder,
            ),
            Menu.SEPARATOR,
            MenuItem(
                "Peers",
                self._build_peers_submenu(),
            ),
            Menu.SEPARATOR,
            MenuItem(
                "Reconnect All Peers",
                self._action_reconnect_all,
            ),
            Menu.SEPARATOR,
            MenuItem(
                lambda item: "Stop Server" if self.server.is_running else "Start Server",
                self._action_toggle_server,
            ),
            MenuItem(
                "Start with Windows",
                self._action_toggle_autostart,
                checked=lambda item: self._is_autostart_enabled(),
            ),
            Menu.SEPARATOR,
            MenuItem(
                "Quit Canopy",
                self._action_quit,
            ),
        )

    def _status_text(self, item: Any) -> str:
        """Dynamic status text for the menu header."""
        if not self.server.is_running:
            return "Canopy - Server stopped"
        n = self.monitor.connected_count
        if n == 0:
            return "Canopy - No peers"
        elif n == 1:
            return "Canopy - 1 peer connected"
        else:
            return f"Canopy - {n} peers connected"

    def _build_peers_submenu(self) -> Menu:
        """Build the peers submenu (dynamically updated)."""
        items = []
        if self.monitor.peers:
            for peer in self.monitor.peers:
                status_icon = "+" if peer.status == "connected" else "-"
                label = f"{status_icon} {peer.display_name} ({peer.status})"
                items.append(MenuItem(label, None, enabled=False))
        else:
            items.append(MenuItem("No peers discovered", None, enabled=False))
        return Menu(*items)

    # ── Actions ─────────────────────────────────────────────────

    def _action_open_ui(self, icon: Any, item: Any) -> None:
        """Open the Canopy web UI in the default browser."""
        url = f"http://localhost:{self.port}"
        logger.info(f"Opening browser to {url}")
        webbrowser.open(url)
        self._has_unread = False
        self._update_icon()

    def _action_open_canopy_folder(self, icon: Any, item: Any) -> None:
        """Open the Canopy tray runtime folder (data/logs) in the OS file explorer."""
        path = os.environ.get("CANOPY_TRAY_HOME") or os.getcwd()
        try:
            if sys.platform == "win32":
                os.startfile(path)  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", path])
            else:
                subprocess.Popen(["xdg-open", path])
        except Exception as e:
            logger.debug(f"Failed to open folder {path}: {e}")

    def _action_reconnect_all(self, icon: Any, item: Any) -> None:
        """Trigger a reconnect to all known peers."""
        import urllib.request

        try:
            headers = {"Content-Type": "application/json"}
            if self.server.tray_api_key:
                headers["X-API-Key"] = self.server.tray_api_key
            req = urllib.request.Request(
                f"http://localhost:{self.port}/api/v1/p2p/reconnect_all",
                data=b"",
                method="POST",
                headers=headers,
            )
            urllib.request.urlopen(req, timeout=5)
            logger.info("Reconnect all triggered")
        except Exception as e:
            logger.warning(f"Reconnect all failed: {e}")

    def _action_toggle_server(self, icon: Any, item: Any) -> None:
        """Start or stop the Canopy server."""
        if self.server.is_running:
            self.monitor.stop()
            self.server.stop()
            self._update_icon()
        else:
            if self.server.start():
                if self.server.tray_api_key:
                    self.monitor.set_api_key(self.server.tray_api_key)
                self.monitor.start()
            else:
                try:
                    self.notifier.notify_server_error(
                        "Failed to start the Canopy server. Check logs for details."
                    )
                except Exception:
                    pass
            self._update_icon()

    def _action_toggle_autostart(self, icon: Any, item: Any) -> None:
        """Toggle Windows auto-start."""
        if self._is_autostart_enabled():
            self._disable_autostart()
        else:
            self._enable_autostart()

    def _action_quit(self, icon: Any, item: Any) -> None:
        """Quit the application."""
        logger.info("Quitting Canopy Tray App...")
        self.monitor.stop()
        self.server.stop()
        if self._icon:
            self._icon.stop()

    # ── Monitor Callbacks ───────────────────────────────────────

    def _on_peer_change(self, peers: list[PeerInfo]) -> None:
        """Called when the peer list changes."""
        new_peers = {p.peer_id: p.status for p in peers}

        # Detect newly connected / disconnected peers
        for peer in peers:
            old_status = self._previous_peers.get(peer.peer_id)
            if peer.status == "connected" and old_status != "connected":
                self.notifier.notify_peer_connected(peer.display_name)
            elif peer.status != "connected" and old_status == "connected":
                self.notifier.notify_peer_disconnected(peer.display_name)

        self._previous_peers = new_peers
        self._update_icon()
        # Refresh the menu
        if self._icon:
            self._icon.menu = self._build_menu()

    def _on_new_message(self, msg: ChannelMessage) -> None:
        """Called when a new channel message is detected."""
        self._has_unread = True
        self._update_icon()
        self.notifier.notify_new_message(
            channel_name=msg.channel_name,
            sender_name=msg.display_name,
            content=msg.content,
            channel_id=msg.channel_id,
        )

    def _on_status_update(self, connected: int, total: int) -> None:
        """Called when connection counts change."""
        if self._icon:
            if connected == 0:
                self._icon.title = "Canopy - No peers"
            elif connected == 1:
                self._icon.title = "Canopy - 1 peer connected"
            else:
                self._icon.title = f"Canopy - {connected} peers connected"

    def _on_server_down(self) -> None:
        """Called when the server stops responding."""
        self.notifier.notify_server_error()
        self._update_icon()

    # ── Icon State ──────────────────────────────────────────────

    def _update_icon(self) -> None:
        """Update the tray icon based on current state."""
        if not self._icon:
            return

        if not self.server.is_running:
            self._icon.icon = get_icon_error()
            self._icon.title = "Canopy - Server stopped"
        elif self.monitor.connected_count > 0:
            base = get_icon_connected()
            if self._has_unread:
                self._icon.icon = get_icon_with_badge(base)
            else:
                self._icon.icon = base
        else:
            base = get_icon_disconnected()
            if self._has_unread:
                self._icon.icon = get_icon_with_badge(base)
            else:
                self._icon.icon = base

    # ── Auto-Start (Windows Registry) ──────────────────────────

    _REG_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
    _REG_VALUE = "Canopy"

    def _is_autostart_enabled(self) -> bool:
        """Check if Canopy is set to auto-start with Windows."""
        try:
            winreg = __import__("winreg")
            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER, self._REG_KEY, 0, winreg.KEY_READ
            )
            try:
                winreg.QueryValueEx(key, self._REG_VALUE)
                return True
            except FileNotFoundError:
                return False
            finally:
                winreg.CloseKey(key)
        except Exception:
            return False

    def _enable_autostart(self) -> None:
        """Add Canopy to Windows auto-start."""
        try:
            winreg = __import__("winreg")

            # Determine the command to run
            if getattr(sys, "frozen", False):
                # Running as PyInstaller .exe
                cmd = f'"{sys.executable}"'
            else:
                # Running as Python script
                cmd = f'"{sys.executable}" -m canopy_tray'

            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER, self._REG_KEY, 0, winreg.KEY_SET_VALUE
            )
            winreg.SetValueEx(key, self._REG_VALUE, 0, winreg.REG_SZ, cmd)
            winreg.CloseKey(key)
            logger.info(f"Auto-start enabled: {cmd}")
        except Exception as e:
            logger.error(f"Failed to enable auto-start: {e}")

    def _disable_autostart(self) -> None:
        """Remove Canopy from Windows auto-start."""
        try:
            winreg = __import__("winreg")

            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER, self._REG_KEY, 0, winreg.KEY_SET_VALUE
            )
            try:
                winreg.DeleteValue(key, self._REG_VALUE)
                logger.info("Auto-start disabled")
            except FileNotFoundError:
                pass
            finally:
                winreg.CloseKey(key)
        except Exception as e:
            logger.error(f"Failed to disable auto-start: {e}")
