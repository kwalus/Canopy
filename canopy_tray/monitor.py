"""
StatusMonitor - polls the Canopy REST API for peer status and new messages.

Runs in a background thread and fires callbacks when:
- Peer list changes (connect/disconnect)
- New channel messages arrive

Project: Canopy - Local Mesh Communication
License: Apache 2.0
"""
from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable

logger = logging.getLogger(__name__)

# Polling intervals (seconds)
PEER_POLL_INTERVAL = 5
MESSAGE_POLL_INTERVAL = 10
MESSAGE_BATCH_LIMIT = 10
SEEN_MESSAGE_CACHE_LIMIT = 32


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
        self._seen_message_ids: dict[str, list[str]] = {}  # channel_id -> newest-first ids
        self._channels: list[dict] = []
        self._local_user_id: str | None = None
        self._local_identity_checked = False

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
        self._local_identity_checked = False
        self._local_user_id = None

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
            peers_raw = self._get_peer_snapshot()
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
            self._ensure_local_identity()
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
            query = urllib.parse.urlencode({"limit": MESSAGE_BATCH_LIMIT})
            data = self._api_get(f"/channels/{channel_id}/messages?{query}")
            messages = data.get("messages", [])
            if not messages:
                return

            message_ids = [str(msg.get("id") or "") for msg in messages if msg.get("id")]
            if not message_ids:
                return

            newest_seen = self._seen_message_ids.get(channel_id, [])
            newest_id = message_ids[0]
            if newest_seen and newest_id == newest_seen[0]:
                return

            # First poll for this channel -- seed the cache but do not emit toasts for backlog.
            if not newest_seen:
                self._remember_messages(channel_id, message_ids)
                return

            known_ids = set(newest_seen)
            unseen_messages = []
            for message in reversed(messages):
                msg_id = str(message.get("id") or "")
                if not msg_id or msg_id in known_ids:
                    continue
                unseen_messages.append(message)

            self._remember_messages(channel_id, message_ids)

            if not unseen_messages:
                return

            if self.on_new_message:
                for latest in unseen_messages:
                    if str(latest.get("user_id") or "") == (self._local_user_id or ""):
                        continue
                    msg = ChannelMessage(
                        message_id=str(latest.get("id") or ""),
                        channel_id=channel_id,
                        channel_name=channel_name,
                        user_id=str(latest.get("user_id") or ""),
                        display_name=latest.get("display_name",
                                                latest.get("user_id", "Someone")),
                        content=latest.get("content", ""),
                        created_at=latest.get("created_at", ""),
                    )
                    self.on_new_message(msg)

        except Exception as e:
            logger.warning(f"Channel message poll error ({channel_id}): {e}")

    def _get_peer_snapshot(self) -> list[dict[str, Any]]:
        """Return a stable peer snapshot using current APIs with compatibility fallback."""
        try:
            data = self._api_get("/p2p/peers")
            connected = set(data.get("connected_peers") or [])
            discovered = data.get("discovered_peers") or []
            known: dict[str, dict[str, Any]] = {}
            for peer in discovered:
                peer_id = str(peer.get("peer_id") or "").strip()
                if not peer_id:
                    continue
                known[peer_id] = {
                    "peer_id": peer_id,
                    "display_name": (peer.get("display_name") or peer_id),
                    "connected": bool(peer.get("connected")) or peer_id in connected,
                }
            for peer_id in connected:
                peer = known.setdefault(peer_id, {
                    "peer_id": peer_id,
                    "display_name": peer_id,
                    "connected": True,
                })
                peer["connected"] = True
            return sorted(known.values(), key=lambda peer: (peer.get("display_name") or peer.get("peer_id") or "").lower())
        except Exception:
            data = self._api_get("/p2p/known_peers")
            return list(data.get("known_peers") or [])

    def _ensure_local_identity(self) -> None:
        """Populate the local tray user once so self-posts do not raise toasts."""
        if self._local_identity_checked:
            return
        try:
            data = self._api_get("/auth/status")
            user_id = str(data.get("user_id") or "").strip()
            self._local_user_id = user_id or None
            self._local_identity_checked = True
        except Exception as exc:
            logger.debug(f"Tray auth/status unavailable: {exc}")
            self._local_user_id = None

    def _remember_messages(self, channel_id: str, message_ids: list[str]) -> None:
        """Keep a bounded newest-first cache per channel for de-duplication."""
        cache = []
        seen = set()
        for message_id in list(message_ids) + self._seen_message_ids.get(channel_id, []):
            if not message_id or message_id in seen:
                continue
            seen.add(message_id)
            cache.append(message_id)
            if len(cache) >= SEEN_MESSAGE_CACHE_LIMIT:
                break
        self._seen_message_ids[channel_id] = cache
