"""
StatusMonitor - polls the Canopy REST API for peer status and new messages.

Runs in a background thread and fires callbacks when:
- Peer list changes (connect/disconnect)
- New channel messages arrive

Author: Konrad Walus (architecture, design, and direction)
Project: Canopy - Local Mesh Communication
License: Apache 2.0
Development: AI-assisted implementation (Claude, Codex, GitHub Copilot, Cursor IDE, Ollama)
"""
from __future__ import annotations

import json
import logging
import threading
import time
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from typing import Any, Callable

logger = logging.getLogger(__name__)

# Polling intervals (seconds)
PEER_POLL_INTERVAL = 5
MESSAGE_POLL_INTERVAL = 10


@dataclass
class PeerInfo:
    """Information about a connected/known peer."""
    peer_id: str
    display_name: str
    status: str  # "connected", "disconnected", etc.


@dataclass
class ChannelMessage:
    """A channel message from the API."""
    message_id: str
    channel_id: str
    channel_name: str
    user_id: str
    display_name: str
    content: str
    created_at: str


class StatusMonitor:
    """Polls the Canopy API and fires callbacks on state changes."""

    def __init__(self, api_base: str = "http://localhost:7770/api/v1", api_key: str | None = None):
        self.api_base = api_base
        self.api_key = api_key
        self._thread: threading.Thread | None = None
        self._running = False

        # Current state
        self.peers: list[PeerInfo] = []
        self.connected_count: int = 0

        # Tracking for new message detection
        self._last_message_ids: dict[str, str] = {}  # channel_id -> last message_id
        self._channels: list[dict] = []

        # Callbacks
        self.on_peer_change: Callable[[list[PeerInfo]], None] | None = None
        self.on_new_message: Callable[[ChannelMessage], None] | None = None
        self.on_status_update: Callable[[int, int], None] | None = None  # (connected, total)
        self.on_server_down: Callable[[], None] | None = None

        # Auth session cookie (set after login)
        self._session_cookie: str | None = None

    def set_api_key(self, api_key: str | None) -> None:
        """Set/replace the API key used for authenticated endpoints."""
        self.api_key = api_key

    def start(self) -> None:
        """Start polling in a background thread."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._poll_loop,
            name="canopy-status-monitor",
            daemon=True,
        )
        self._thread.start()
        logger.info("StatusMonitor started")

    def stop(self) -> None:
        """Stop the polling thread."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=3)
            self._thread = None
        logger.info("StatusMonitor stopped")

    def _poll_loop(self) -> None:
        """Main polling loop."""
        peer_tick = 0.0
        msg_tick = 0.0

        while self._running:
            try:
                now = time.time()

                # Poll peers every PEER_POLL_INTERVAL seconds
                if now - peer_tick >= PEER_POLL_INTERVAL:
                    peer_tick = now
                    self._poll_peers()

                # Poll messages every MESSAGE_POLL_INTERVAL seconds
                if now - msg_tick >= MESSAGE_POLL_INTERVAL:
                    msg_tick = now
                    self._poll_messages()

            except Exception as e:
                logger.warning(f"Monitor poll error: {e}")

            time.sleep(1)

    def _api_get(self, path: str, timeout: int = 5) -> Any:
        """Make a GET request to the Canopy API."""
        url = f"{self.api_base}{path}"
        req = urllib.request.Request(url)
        if self._session_cookie:
            req.add_header("Cookie", self._session_cookie)
        if self.api_key:
            req.add_header("X-API-Key", self.api_key)
        resp = urllib.request.urlopen(req, timeout=timeout)
        return json.loads(resp.read())

    def _poll_peers(self) -> None:
        """Poll peer status and fire callbacks on changes."""
        try:
            data = self._api_get("/p2p/known_peers")
            peers_raw = data.get("known_peers", [])
            new_peers: list[PeerInfo] = []
            for p in peers_raw:
                peer_id = p.get("peer_id", "unknown")
                display_name = (p.get("display_name") or "").strip() or peer_id
                connected = bool(p.get("connected"))
                new_peers.append(PeerInfo(
                    peer_id=peer_id,
                    display_name=display_name,
                    status="connected" if connected else "disconnected",
                ))

            new_connected = sum(1 for p in new_peers if p.status == "connected")
            old_connected = self.connected_count

            old_snapshot = [(p.peer_id, p.display_name, p.status) for p in self.peers]
            new_snapshot = [(p.peer_id, p.display_name, p.status) for p in new_peers]

            self.peers = new_peers
            self.connected_count = new_connected

            # Fire callbacks on changes
            if new_snapshot != old_snapshot:
                if self.on_peer_change:
                    self.on_peer_change(new_peers)
            if new_connected != old_connected:
                if self.on_status_update:
                    self.on_status_update(new_connected, len(new_peers))

        except urllib.error.URLError:
            # Server is probably down
            if self.connected_count > 0 or self.peers:
                self.peers = []
                self.connected_count = 0
                if self.on_server_down:
                    self.on_server_down()
        except Exception as e:
            logger.warning(f"Peer poll error: {e}")

    def _poll_messages(self) -> None:
        """Poll for new messages across channels."""
        try:
            # Get channel list
            data = self._api_get("/channels")
            channels = data.get("channels", [])
            self._channels = channels

            for ch in channels:
                # Respect per-channel notification setting when available.
                try:
                    if ch.get("notifications_enabled") is False:
                        continue
                except Exception:
                    pass
                channel_id = ch.get("id", "")
                channel_name = ch.get("name", channel_id)
                self._check_channel_messages(channel_id, channel_name)

        except urllib.error.URLError:
            pass  # Server down, skip message poll
        except Exception as e:
            logger.warning(f"Message poll error: {e}")

    def _check_channel_messages(self, channel_id: str, channel_name: str) -> None:
        """Check a single channel for new messages."""
        try:
            data = self._api_get(f"/channels/{channel_id}/messages?limit=1")
            messages = data.get("messages", [])
            if not messages:
                return

            latest = messages[0]
            msg_id = latest.get("id", "")

            # Skip if we've already seen this message
            prev_id = self._last_message_ids.get(channel_id)
            if prev_id == msg_id:
                return

            # First poll for this channel -- just record, don't notify
            if prev_id is None:
                self._last_message_ids[channel_id] = msg_id
                return

            # New message detected
            self._last_message_ids[channel_id] = msg_id

            if self.on_new_message:
                msg = ChannelMessage(
                    message_id=msg_id,
                    channel_id=channel_id,
                    channel_name=channel_name,
                    user_id=latest.get("user_id", ""),
                    display_name=latest.get("display_name",
                                            latest.get("user_id", "Someone")),
                    content=latest.get("content", ""),
                    created_at=latest.get("created_at", ""),
                )
                self.on_new_message(msg)

        except Exception as e:
            logger.warning(f"Channel message poll error ({channel_id}): {e}")
