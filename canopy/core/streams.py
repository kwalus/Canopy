"""
Live stream management for Canopy.

Provides stream lifecycle, scoped access tokens, and storage helpers for
manifest/segment ingestion and playback.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import secrets
import shutil
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote_plus

from .database import DatabaseManager
from .channels import ChannelManager

logger = logging.getLogger("canopy.streams")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _db_ts(value: datetime) -> str:
    dt = value.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _parse_ts(value: Any) -> Optional[datetime]:
    if not value:
        return None
    txt = str(value).strip()
    if not txt:
        return None
    try:
        if "T" in txt:
            return datetime.fromisoformat(txt.replace("Z", "+00:00"))
        return datetime.strptime(txt, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except Exception:
        return None


@dataclass
class StreamRecord:
    id: str
    channel_id: str
    created_by: str
    title: str
    description: str
    media_kind: str
    protocol: str
    status: str
    visibility_mode: str
    relay_allowed: bool
    origin_peer: Optional[str]
    metadata: dict[str, Any]
    playlist_path: str
    created_at: Optional[str]
    updated_at: Optional[str]
    started_at: Optional[str]
    ended_at: Optional[str]
    stream_kind: str = "media"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "channel_id": self.channel_id,
            "created_by": self.created_by,
            "title": self.title,
            "description": self.description,
            "media_kind": self.media_kind,
            "protocol": self.protocol,
            "status": self.status,
            "visibility_mode": self.visibility_mode,
            "relay_allowed": bool(self.relay_allowed),
            "origin_peer": self.origin_peer,
            "metadata": dict(self.metadata or {}),
            "playlist_path": self.playlist_path,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "stream_kind": self.stream_kind,
        }


class StreamManager:
    STREAM_KINDS = {"media", "telemetry"}
    MEDIA_KINDS = {"audio", "video", "data"}
    PROTOCOLS = {"hls", "events-json"}
    STATUSES = {"created", "live", "stopped"}
    TOKEN_SCOPES = {"view", "ingest"}
    SAFE_SEGMENT_NAME = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._\-]{0,127}$")
    MAX_TITLE_CHARS = 140
    MAX_DESCRIPTION_CHARS = 4000
    MAX_MANIFEST_BYTES = 512 * 1024
    MAX_SEGMENT_BYTES = 20 * 1024 * 1024
    DEFAULT_TOKEN_TTL_SECONDS = 15 * 60
    MIN_TOKEN_TTL_SECONDS = 30
    MAX_TOKEN_TTL_SECONDS = 24 * 60 * 60
    DEFAULT_LATENCY_MODE = "hls"
    MAX_EVENT_PAYLOAD_CHARS = 512 * 1024
    DEFAULT_EVENT_RETENTION_MAX = 5000
    MAX_EVENT_RETENTION_MAX = 100000

    def __init__(self, db: DatabaseManager, channel_manager: ChannelManager, data_root: str) -> None:
        self.db = db
        self.channel_manager = channel_manager
        self.storage_root = Path(data_root) / "streams"
        self.storage_root.mkdir(parents=True, exist_ok=True)
        self._exclude_from_dropbox(self.storage_root)
        self._ensure_tables()

    @staticmethod
    def _exclude_from_dropbox(path: Path) -> None:
        """Mark a directory so Dropbox does not sync it (macOS xattr, no-op elsewhere)."""
        try:
            import subprocess
            subprocess.run(
                ["xattr", "-w", "com.dropbox.ignored", "1", str(path)],
                capture_output=True, timeout=5
            )
        except Exception:
            pass

    def _ensure_tables(self) -> None:
        with self.db.get_connection() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS streams (
                    id TEXT PRIMARY KEY,
                    channel_id TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    title TEXT NOT NULL,
                    description TEXT,
                    media_kind TEXT NOT NULL,
                    protocol TEXT NOT NULL DEFAULT 'hls',
                    status TEXT NOT NULL DEFAULT 'created',
                    visibility_mode TEXT NOT NULL DEFAULT 'open',
                    relay_allowed INTEGER NOT NULL DEFAULT 0,
                    origin_peer TEXT,
                    metadata TEXT,
                    playlist_path TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    started_at TIMESTAMP,
                    ended_at TIMESTAMP,
                    FOREIGN KEY(channel_id) REFERENCES channels(id) ON DELETE CASCADE,
                    FOREIGN KEY(created_by) REFERENCES users(id)
                );

                CREATE INDEX IF NOT EXISTS idx_streams_channel_status
                ON streams(channel_id, status, created_at DESC);

                CREATE INDEX IF NOT EXISTS idx_streams_creator
                ON streams(created_by, created_at DESC);

                CREATE TABLE IF NOT EXISTS stream_access_tokens (
                    id TEXT PRIMARY KEY,
                    stream_id TEXT NOT NULL,
                    user_id TEXT,
                    scope TEXT NOT NULL,
                    token_hash TEXT NOT NULL,
                    expires_at TIMESTAMP NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    revoked_at TIMESTAMP,
                    metadata TEXT,
                    FOREIGN KEY(stream_id) REFERENCES streams(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_stream_tokens_stream_scope
                ON stream_access_tokens(stream_id, scope, expires_at);

                CREATE TABLE IF NOT EXISTS stream_events (
                    id TEXT PRIMARY KEY,
                    stream_id TEXT NOT NULL,
                    seq INTEGER NOT NULL,
                    event_ts TIMESTAMP NOT NULL,
                    content_type TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    metadata TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(stream_id) REFERENCES streams(id) ON DELETE CASCADE,
                    UNIQUE(stream_id, seq)
                );

                CREATE INDEX IF NOT EXISTS idx_stream_events_stream_seq
                ON stream_events(stream_id, seq DESC);
                """
            )
            conn.commit()

    def get_runtime_health(self) -> dict[str, Any]:
        ffmpeg_path = shutil.which("ffmpeg")
        ffprobe_path = shutil.which("ffprobe")
        with self.db.get_connection() as conn:
            counts = conn.execute(
                """
                SELECT
                    COUNT(*) AS total_streams,
                    SUM(CASE WHEN status = 'live' THEN 1 ELSE 0 END) AS live_streams
                FROM streams
                """
            ).fetchone()
        total_streams = int(counts["total_streams"] or 0) if counts else 0
        live_streams = int(counts["live_streams"] or 0) if counts else 0
        return {
            "stream_manager_ready": True,
            "storage_root": str(self.storage_root),
            "ffmpeg_found": bool(ffmpeg_path),
            "ffmpeg_path": ffmpeg_path,
            "ffprobe_found": bool(ffprobe_path),
            "ffprobe_path": ffprobe_path,
            "default_token_ttl_seconds": self.DEFAULT_TOKEN_TTL_SECONDS,
            "max_token_ttl_seconds": self.MAX_TOKEN_TTL_SECONDS,
            "latency_mode_supported": self.DEFAULT_LATENCY_MODE,
            "streams_total": total_streams,
            "streams_live": live_streams,
            "remote_proxy_mode": "sync_probe_cached",
        }

    def _extract_stream_kind(self, row: Any, metadata: Optional[dict[str, Any]] = None) -> str:
        meta = metadata or {}
        kind = str(meta.get("stream_kind") or "").strip().lower()
        if kind in self.STREAM_KINDS:
            return kind
        protocol = str(row["protocol"] or "").strip().lower()
        media_kind = str(row["media_kind"] or "").strip().lower()
        if protocol == "events-json" or media_kind == "data":
            return "telemetry"
        return "media"

    def _event_retention_max(self, row: Any, metadata: Optional[dict[str, Any]] = None) -> int:
        meta = metadata or {}
        raw = meta.get("retention_max_events", self.DEFAULT_EVENT_RETENTION_MAX)
        try:
            parsed = int(raw)
        except Exception:
            parsed = self.DEFAULT_EVENT_RETENTION_MAX
        return max(1, min(parsed, self.MAX_EVENT_RETENTION_MAX))

    def _row_to_stream(self, row: Any) -> StreamRecord:
        metadata: dict[str, Any] = {}
        try:
            raw_metadata = row["metadata"]
            if raw_metadata:
                parsed = json.loads(raw_metadata)
                if isinstance(parsed, dict):
                    metadata = parsed
        except Exception:
            metadata = {}

        return StreamRecord(
            id=str(row["id"]),
            channel_id=str(row["channel_id"]),
            created_by=str(row["created_by"]),
            title=str(row["title"] or ""),
            description=str(row["description"] or ""),
            media_kind=str(row["media_kind"] or "audio"),
            protocol=str(row["protocol"] or "hls"),
            status=str(row["status"] or "created"),
            visibility_mode=str(row["visibility_mode"] or "open"),
            relay_allowed=bool(row["relay_allowed"]),
            origin_peer=row["origin_peer"],
            metadata=metadata,
            playlist_path=str(row["playlist_path"] or ""),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            started_at=row["started_at"],
            ended_at=row["ended_at"],
            stream_kind=self._extract_stream_kind(row, metadata),
        )

    def _stream_paths(self, stream_id: str) -> tuple[Path, Path, Path]:
        root = self.storage_root / stream_id / "hls"
        segments = root / "segments"
        manifest = root / "master.m3u8"
        return root, segments, manifest

    def _ensure_stream_dirs(self, stream_id: str) -> tuple[Path, Path, Path]:
        root, segments, manifest = self._stream_paths(stream_id)
        segments.mkdir(parents=True, exist_ok=True)
        return root, segments, manifest

    def _can_view_stream(self, conn: Any, stream_row: Any, user_id: str) -> bool:
        if not user_id:
            return False
        # Open-visibility streams are accessible to any authenticated user.
        visibility = str(stream_row["visibility_mode"] or "open").lower()
        if visibility == "open":
            return True
        # Channel-scoped or private streams require channel membership.
        member = conn.execute(
            "SELECT 1 FROM channel_members WHERE channel_id = ? AND user_id = ?",
            (stream_row["channel_id"], user_id),
        ).fetchone()
        return bool(member)

    def _can_manage_stream(self, conn: Any, stream_row: Any, user_id: str) -> bool:
        if not user_id:
            return False
        if str(stream_row["created_by"] or "") == user_id:
            return True
        row = conn.execute(
            "SELECT role FROM channel_members WHERE channel_id = ? AND user_id = ?",
            (stream_row["channel_id"], user_id),
        ).fetchone()
        if not row:
            return False
        role = str(row["role"] if "role" in row.keys() else row[0] or "").lower()
        return role == "admin"

    def create_stream(
        self,
        *,
        channel_id: str,
        created_by: str,
        title: str,
        description: str = "",
        stream_kind: Optional[str] = None,
        media_kind: str = "audio",
        protocol: str = "hls",
        relay_allowed: bool = False,
        origin_peer: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> tuple[Optional[dict[str, Any]], Optional[str]]:
        cid = str(channel_id or "").strip()
        uid = str(created_by or "").strip()
        raw_stream_kind = str(stream_kind or "").strip().lower()
        mkind = str(media_kind or "").strip().lower()
        proto = str(protocol or "").strip().lower()
        ttl = str(title or "").strip()
        desc = str(description or "").strip()
        if not cid or not uid:
            return None, "missing_identity"
        if raw_stream_kind and raw_stream_kind not in self.STREAM_KINDS:
            return None, "invalid_stream_kind"
        kind = raw_stream_kind or ("telemetry" if proto == "events-json" else "media")
        if kind == "media":
            if mkind not in {"audio", "video"}:
                return None, "invalid_media_kind"
            if proto != "hls":
                return None, "invalid_protocol_for_stream_kind"
        else:
            mkind = "data"
            if proto != "events-json":
                return None, "invalid_protocol_for_stream_kind"
        if proto not in self.PROTOCOLS:
            return None, "invalid_protocol"
        if not ttl:
            return None, "title_required"
        if len(ttl) > self.MAX_TITLE_CHARS:
            return None, "title_too_long"
        if len(desc) > self.MAX_DESCRIPTION_CHARS:
            return None, "description_too_long"

        with self.db.get_connection() as conn:
            channel_row = conn.execute(
                "SELECT id, COALESCE(privacy_mode, 'open') AS privacy_mode FROM channels WHERE id = ?",
                (cid,),
            ).fetchone()
            if not channel_row:
                return None, "channel_not_found"
            member = conn.execute(
                "SELECT 1 FROM channel_members WHERE channel_id = ? AND user_id = ?",
                (cid, uid),
            ).fetchone()
            if not member:
                return None, "not_channel_member"

            stream_id = f"ST{secrets.token_hex(8)}"
            self._ensure_stream_dirs(stream_id)
            now = _db_ts(_utcnow())
            safe_meta: dict[str, Any] = {}
            if isinstance(metadata, dict):
                safe_meta = dict(metadata)
            safe_meta.setdefault("version", 1)
            safe_meta["stream_kind"] = kind
            if kind == "media":
                safe_meta.setdefault("ingest", "hls_push")
                safe_meta.setdefault("latency_mode", self.DEFAULT_LATENCY_MODE)
            else:
                safe_meta.setdefault("ingest", "event_push")
                safe_meta.setdefault("retention_max_events", self.DEFAULT_EVENT_RETENTION_MAX)
            playlist_path = "hls/master.m3u8" if kind == "media" else "events/stream.jsonl"
            conn.execute(
                """
                INSERT INTO streams (
                    id, channel_id, created_by, title, description, media_kind, protocol,
                    status, visibility_mode, relay_allowed, origin_peer, metadata,
                    playlist_path, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'created', ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    stream_id,
                    cid,
                    uid,
                    ttl,
                    desc,
                    mkind,
                    proto,
                    str(channel_row["privacy_mode"] or "open").lower(),
                    1 if relay_allowed else 0,
                    origin_peer,
                    json.dumps(safe_meta),
                    playlist_path,
                    now,
                    now,
                ),
            )
            conn.commit()

            row = conn.execute("SELECT * FROM streams WHERE id = ?", (stream_id,)).fetchone()
            if not row:
                return None, "create_failed"
            return self._row_to_stream(row).to_dict(), None

    def get_stream(self, stream_id: str) -> Optional[dict[str, Any]]:
        sid = str(stream_id or "").strip()
        if not sid:
            return None
        with self.db.get_connection() as conn:
            row = conn.execute("SELECT * FROM streams WHERE id = ?", (sid,)).fetchone()
            if not row:
                return None
            return self._row_to_stream(row).to_dict()

    def get_stream_for_user(self, stream_id: str, user_id: str) -> Optional[dict[str, Any]]:
        sid = str(stream_id or "").strip()
        uid = str(user_id or "").strip()
        if not sid or not uid:
            return None
        with self.db.get_connection() as conn:
            row = conn.execute("SELECT * FROM streams WHERE id = ?", (sid,)).fetchone()
            if not row:
                return None
            if not self._can_view_stream(conn, row, uid):
                return None
            return self._row_to_stream(row).to_dict()

    def list_streams_for_user(
        self,
        user_id: str,
        *,
        channel_id: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        uid = str(user_id or "").strip()
        if not uid:
            return []
        try:
            parsed_limit = int(limit or 100)
        except Exception:
            parsed_limit = 100
        limit_i = max(1, min(parsed_limit, 500))
        params: list[Any] = [uid]
        where: list[str] = []
        if channel_id:
            where.append("s.channel_id = ?")
            params.append(str(channel_id).strip())
        if status:
            where.append("s.status = ?")
            params.append(str(status).strip().lower())
        clause = f" AND {' AND '.join(where)}" if where else ""

        with self.db.get_connection() as conn:
            rows = conn.execute(
                f"""
                SELECT s.*
                FROM streams s
                INNER JOIN channel_members cm
                    ON cm.channel_id = s.channel_id
                   AND cm.user_id = ?
                WHERE 1=1 {clause}
                ORDER BY s.created_at DESC
                LIMIT ?
                """,
                tuple(params + [limit_i]),
            ).fetchall()
            return [self._row_to_stream(row).to_dict() for row in rows]

    def _set_status(self, stream_id: str, user_id: str, status: str) -> tuple[Optional[dict[str, Any]], Optional[str]]:
        sid = str(stream_id or "").strip()
        uid = str(user_id or "").strip()
        next_status = str(status or "").strip().lower()
        if next_status not in self.STATUSES:
            return None, "invalid_status"
        if not sid or not uid:
            return None, "missing_identity"

        with self.db.get_connection() as conn:
            row = conn.execute("SELECT * FROM streams WHERE id = ?", (sid,)).fetchone()
            if not row:
                return None, "not_found"
            if not self._can_manage_stream(conn, row, uid):
                return None, "not_authorized"

            now = _db_ts(_utcnow())
            started_at = row["started_at"]
            ended_at = row["ended_at"]
            if next_status == "live" and not started_at:
                started_at = now
                ended_at = None
            if next_status == "stopped":
                ended_at = now

            conn.execute(
                """
                UPDATE streams
                SET status = ?, started_at = ?, ended_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (next_status, started_at, ended_at, now, sid),
            )
            conn.commit()
            out = conn.execute("SELECT * FROM streams WHERE id = ?", (sid,)).fetchone()
            if not out:
                return None, "update_failed"
            stream_payload = self._row_to_stream(out).to_dict()
        try:
            if hasattr(self.channel_manager, "update_stream_attachment_status"):
                self.channel_manager.update_stream_attachment_status(sid, next_status)
        except Exception as sync_err:
            logger.warning(f"Failed to sync stream attachment status for {sid}: {sync_err}")
        return stream_payload, None

    def start_stream(self, stream_id: str, user_id: str) -> tuple[Optional[dict[str, Any]], Optional[str]]:
        return self._set_status(stream_id, user_id, "live")

    def stop_stream(self, stream_id: str, user_id: str) -> tuple[Optional[dict[str, Any]], Optional[str]]:
        return self._set_status(stream_id, user_id, "stopped")

    def issue_token(
        self,
        *,
        stream_id: str,
        user_id: str,
        scope: str,
        ttl_seconds: Optional[int] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> tuple[Optional[dict[str, Any]], Optional[str]]:
        sid = str(stream_id or "").strip()
        uid = str(user_id or "").strip()
        token_scope = str(scope or "").strip().lower()
        if not sid or not uid:
            return None, "missing_identity"
        if token_scope not in self.TOKEN_SCOPES:
            return None, "invalid_scope"

        if ttl_seconds is None:
            parsed_ttl = self.DEFAULT_TOKEN_TTL_SECONDS
        else:
            try:
                parsed_ttl = int(ttl_seconds)
            except Exception:
                return None, "invalid_ttl"
        ttl = parsed_ttl
        ttl = max(self.MIN_TOKEN_TTL_SECONDS, min(ttl, self.MAX_TOKEN_TTL_SECONDS))

        with self.db.get_connection() as conn:
            row = conn.execute("SELECT * FROM streams WHERE id = ?", (sid,)).fetchone()
            if not row:
                return None, "not_found"

            if token_scope == "view":
                if not self._can_view_stream(conn, row, uid):
                    return None, "not_authorized"
            else:
                if not self._can_manage_stream(conn, row, uid):
                    return None, "not_authorized"

            token_id = f"STK{secrets.token_hex(8)}"
            token_secret = secrets.token_urlsafe(32)
            token_hash = hashlib.sha256(token_secret.encode("utf-8")).hexdigest()
            expires_at = _utcnow() + timedelta(seconds=ttl)
            payload = metadata if isinstance(metadata, dict) else {}
            conn.execute(
                """
                INSERT INTO stream_access_tokens (
                    id, stream_id, user_id, scope, token_hash, expires_at, metadata
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    token_id,
                    sid,
                    uid,
                    token_scope,
                    token_hash,
                    _db_ts(expires_at),
                    json.dumps(payload),
                ),
            )
            conn.commit()

        return {
            "token": f"{token_id}.{token_secret}",
            "token_id": token_id,
            "stream_id": sid,
            "scope": token_scope,
            "expires_at": expires_at.isoformat(),
            "ttl_seconds": ttl,
        }, None

    def revoke_token(self, token_id: str) -> Optional[str]:
        tid = str(token_id or "").strip()
        if not tid:
            return "missing_token_id"
        now = _db_ts(_utcnow())
        with self.db.get_connection() as conn:
            row = conn.execute(
                "SELECT id FROM stream_access_tokens WHERE id = ?",
                (tid,),
            ).fetchone()
            if not row:
                return "not_found"
            conn.execute(
                "UPDATE stream_access_tokens SET revoked_at = ? WHERE id = ?",
                (now, tid),
            )
            conn.commit()
        return None

    def validate_token(
        self,
        *,
        stream_id: str,
        token: str,
        scope: str,
    ) -> tuple[Optional[dict[str, Any]], Optional[str]]:
        sid = str(stream_id or "").strip()
        scope_norm = str(scope or "").strip().lower()
        if not sid:
            return None, "missing_stream_id"
        if scope_norm not in self.TOKEN_SCOPES:
            return None, "invalid_scope"

        raw = str(token or "").strip()
        if "." not in raw:
            return None, "invalid_token"
        token_id, token_secret = raw.split(".", 1)
        token_id = token_id.strip()
        token_secret = token_secret.strip()
        if not token_id or not token_secret:
            return None, "invalid_token"

        with self.db.get_connection() as conn:
            row = conn.execute(
                """
                SELECT id, stream_id, user_id, scope, token_hash, expires_at, revoked_at, metadata
                FROM stream_access_tokens
                WHERE id = ? AND scope = ?
                """,
                (token_id, scope_norm),
            ).fetchone()
            if not row:
                return None, "invalid_token"
            if str(row["stream_id"] or "") != sid:
                return None, "invalid_token"
            if row["revoked_at"]:
                return None, "revoked_token"
            exp = _parse_ts(row["expires_at"])
            if not exp or exp <= _utcnow():
                return None, "expired_token"
            expected_hash = str(row["token_hash"] or "")
            supplied_hash = hashlib.sha256(token_secret.encode("utf-8")).hexdigest()
            if not secrets.compare_digest(expected_hash, supplied_hash):
                return None, "invalid_token"
            meta: dict[str, Any] = {}
            try:
                parsed_meta = json.loads(row["metadata"] or "{}")
                if isinstance(parsed_meta, dict):
                    meta = parsed_meta
            except Exception:
                meta = {}
            return {
                "id": row["id"],
                "stream_id": row["stream_id"],
                "user_id": row["user_id"],
                "scope": row["scope"],
                "expires_at": exp.isoformat(),
                "metadata": meta,
            }, None

    def refresh_token(
        self,
        *,
        stream_id: str,
        current_token: str,
        scope: str,
        user_id: str,
        ttl_seconds: Optional[int] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> tuple[Optional[dict[str, Any]], Optional[str]]:
        sid = str(stream_id or "").strip()
        uid = str(user_id or "").strip()
        if not sid or not uid:
            return None, "missing_identity"
        token_data, token_err = self.validate_token(
            stream_id=sid,
            token=current_token,
            scope=scope,
        )
        if token_err or not token_data:
            return None, token_err or "invalid_token"
        token_owner = str(token_data.get("user_id") or "").strip()
        if token_owner != uid:
            return None, "not_authorized"
        revoke_err = self.revoke_token(str(token_data.get("id") or ""))
        if revoke_err and revoke_err != "not_found":
            return None, revoke_err
        merged_metadata = dict(metadata or {})
        merged_metadata["refresh_of"] = str(token_data.get("id") or "")
        merged_metadata["refreshed_at"] = _utcnow().isoformat()
        return self.issue_token(
            stream_id=sid,
            user_id=uid,
            scope=scope,
            ttl_seconds=ttl_seconds,
            metadata=merged_metadata,
        )

    def _load_stream_row(self, stream_id: str) -> Optional[Any]:
        sid = str(stream_id or "").strip()
        if not sid:
            return None
        with self.db.get_connection() as conn:
            return conn.execute("SELECT * FROM streams WHERE id = ?", (sid,)).fetchone()

    def store_manifest(
        self,
        *,
        stream_id: str,
        manifest_bytes: bytes,
    ) -> Optional[str]:
        sid = str(stream_id or "").strip()
        if not sid:
            return "missing_stream_id"
        if not isinstance(manifest_bytes, (bytes, bytearray)):
            return "invalid_manifest"
        if len(manifest_bytes) <= 0:
            return "empty_ingest_payload"
        if len(manifest_bytes) > self.MAX_MANIFEST_BYTES:
            return "manifest_size_invalid"
        try:
            manifest_text = manifest_bytes.decode("utf-8")
        except Exception:
            return "manifest_not_utf8"
        if not manifest_text.lstrip().startswith("#EXTM3U"):
            return "manifest_invalid"
        row = self._load_stream_row(sid)
        if not row:
            return "not_found"

        _, _, manifest_path = self._ensure_stream_dirs(sid)
        tmp_path = manifest_path.with_suffix(".tmp")
        tmp_path.write_bytes(manifest_bytes)
        tmp_path.replace(manifest_path)

        with self.db.get_connection() as conn:
            conn.execute(
                "UPDATE streams SET updated_at = ? WHERE id = ?",
                (_db_ts(_utcnow()), sid),
            )
            conn.commit()
        return None

    def store_segment(
        self,
        *,
        stream_id: str,
        segment_name: str,
        segment_bytes: bytes,
    ) -> Optional[str]:
        sid = str(stream_id or "").strip()
        fname = str(segment_name or "").strip()
        if not sid:
            return "missing_stream_id"
        if not fname or "/" in fname or "\\" in fname:
            return "invalid_segment_name"
        if not self.SAFE_SEGMENT_NAME.match(fname):
            return "invalid_segment_name"
        if not isinstance(segment_bytes, (bytes, bytearray)):
            return "invalid_segment"
        if len(segment_bytes) <= 0:
            return "empty_ingest_payload"
        if len(segment_bytes) > self.MAX_SEGMENT_BYTES:
            return "segment_size_invalid"
        if not self._load_stream_row(sid):
            return "not_found"

        _, segments_dir, _ = self._ensure_stream_dirs(sid)
        target = segments_dir / fname
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_bytes(segment_bytes)
        tmp.replace(target)

        with self.db.get_connection() as conn:
            conn.execute(
                "UPDATE streams SET updated_at = ? WHERE id = ?",
                (_db_ts(_utcnow()), sid),
            )
            conn.commit()
        return None

    def render_manifest_for_token(
        self,
        *,
        stream_id: str,
        token: str,
        api_base_path: str = "/api/v1/streams",
    ) -> tuple[Optional[bytes], Optional[str]]:
        sid = str(stream_id or "").strip()
        raw_token = str(token or "").strip()
        if not sid:
            return None, "missing_stream_id"

        root, segments, manifest_path = self._stream_paths(sid)
        if not root.exists() or not segments.exists() or not manifest_path.exists():
            return None, "manifest_not_found"

        try:
            text = manifest_path.read_text(encoding="utf-8")
        except Exception:
            return None, "manifest_read_error"

        token_q = quote_plus(raw_token.replace("\n", "").replace("\r", "")) if raw_token else ""
        out_lines: list[str] = []
        _ext_map_re = re.compile(r'(#EXT-X-MAP:URI=")([^"]+)(".*)')
        for line in text.splitlines():
            raw = line.strip()
            if not raw:
                out_lines.append(line)
                continue
            # Rewrite #EXT-X-MAP:URI="init.mp4" to full segment path + optional token
            if raw.startswith("#EXT-X-MAP:"):
                m = _ext_map_re.match(raw)
                if m:
                    uri = m.group(2)
                    if uri.startswith("/") or uri.startswith("http"):
                        out_lines.append(raw)
                    else:
                        safe_uri = uri.split("?", 1)[0].split("#", 1)[0]
                        if "/" not in safe_uri and "\\" not in safe_uri and self.SAFE_SEGMENT_NAME.match(safe_uri):
                            seg_url = f"{api_base_path}/{sid}/segments/{safe_uri}"
                            if token_q:
                                seg_url += f"?token={token_q}"
                            out_lines.append(f'{m.group(1)}{seg_url}{m.group(3)}')
                        else:
                            out_lines.append(raw)
                else:
                    out_lines.append(raw)
                continue
            if raw.startswith("#"):
                out_lines.append(line)
                continue
            if raw.startswith("http://") or raw.startswith("https://") or raw.startswith("/"):
                out_lines.append(raw)
                continue
            safe = raw.split("?", 1)[0]
            safe = safe.split("#", 1)[0]
            if "/" in safe or "\\" in safe or not self.SAFE_SEGMENT_NAME.match(safe):
                continue
            seg_url = f"{api_base_path}/{sid}/segments/{safe}"
            if token_q:
                seg_url += f"?token={token_q}"
            out_lines.append(seg_url)
        rendered = "\n".join(out_lines).encode("utf-8")
        return rendered, None

    def get_segment_data(self, *, stream_id: str, segment_name: str) -> tuple[Optional[bytes], Optional[str], Optional[str]]:
        sid = str(stream_id or "").strip()
        fname = str(segment_name or "").strip()
        if not sid:
            return None, None, "missing_stream_id"
        if not fname or "/" in fname or "\\" in fname or not self.SAFE_SEGMENT_NAME.match(fname):
            return None, None, "invalid_segment_name"
        _, segments_dir, _ = self._stream_paths(sid)
        seg_path = segments_dir / fname
        if not seg_path.exists() or not seg_path.is_file():
            return None, None, "segment_not_found"
        try:
            data = seg_path.read_bytes()
        except Exception:
            return None, None, "segment_read_error"

        ext = seg_path.suffix.lower()
        mimetype = "application/octet-stream"
        if ext in {".ts"}:
            mimetype = "video/mp2t"
        elif ext in {".m4s"}:
            mimetype = "video/iso.segment"
        elif ext in {".mp4"}:
            mimetype = "video/mp4"
        elif ext in {".aac"}:
            mimetype = "audio/aac"
        elif ext in {".mp3"}:
            mimetype = "audio/mpeg"
        elif ext in {".webm"}:
            mimetype = "video/webm"
        return data, mimetype, None

    def list_segments(self, *, stream_id: str) -> tuple[list[str], Optional[str]]:
        """Return sorted list of segment filenames on disk for a stream."""
        sid = str(stream_id or "").strip()
        if not sid:
            return [], "missing_stream_id"
        _, segments_dir, _ = self._stream_paths(sid)
        if not segments_dir.exists():
            return [], None
        try:
            names = sorted(
                f.name for f in segments_dir.iterdir()
                if f.is_file() and self.SAFE_SEGMENT_NAME.match(f.name)
            )
            return names, None
        except Exception:
            return [], "list_failed"

    def cleanup_stale_streams(self, max_idle_minutes: int = 30) -> list[str]:
        """Mark live streams with no segment activity recently as stopped."""
        cutoff = _utcnow() - timedelta(minutes=max_idle_minutes)
        stopped_ids: list[str] = []
        with self.db.get_connection() as conn:
            rows = conn.execute(
                "SELECT id, updated_at FROM streams WHERE status = 'live'"
            ).fetchall()
            for row in rows:
                try:
                    ua_str = str(row["updated_at"] or "").replace("Z", "+00:00")
                    ua = datetime.fromisoformat(ua_str)
                    if ua.tzinfo is None:
                        ua = ua.replace(tzinfo=timezone.utc)
                except Exception:
                    continue
                if ua < cutoff:
                    conn.execute(
                        "UPDATE streams SET status = 'stopped', ended_at = ?, updated_at = ? WHERE id = ?",
                        (_db_ts(_utcnow()), _db_ts(_utcnow()), str(row["id"])),
                    )
                    stopped_ids.append(str(row["id"]))
            if stopped_ids:
                conn.commit()
        return stopped_ids

    def store_event(
        self,
        *,
        stream_id: str,
        event_payload: Any,
        content_type: str = "application/json",
        event_ts: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> tuple[Optional[dict[str, Any]], Optional[str]]:
        sid = str(stream_id or "").strip()
        if not sid:
            return None, "missing_stream_id"

        ctype = str(content_type or "application/json").split(";", 1)[0].strip().lower() or "application/json"
        if len(ctype) > 120:
            return None, "invalid_content_type"

        payload_text = ""
        try:
            if isinstance(event_payload, (dict, list)):
                payload_text = json.dumps(event_payload, separators=(",", ":"), ensure_ascii=False)
            elif isinstance(event_payload, (bytes, bytearray)):
                payload_text = bytes(event_payload).decode("utf-8")
            elif event_payload is None:
                return None, "invalid_event_payload"
            else:
                payload_text = str(event_payload)
        except Exception:
            return None, "invalid_event_payload"

        if not payload_text:
            return None, "invalid_event_payload"
        if len(payload_text) > self.MAX_EVENT_PAYLOAD_CHARS:
            return None, "event_payload_too_large"

        parsed_event_ts = _parse_ts(event_ts) if event_ts else None
        event_ts_db = _db_ts(parsed_event_ts or _utcnow())
        metadata_json = json.dumps(metadata) if isinstance(metadata, dict) else None

        with self.db.get_connection() as conn:
            stream_row = conn.execute("SELECT * FROM streams WHERE id = ?", (sid,)).fetchone()
            if not stream_row:
                return None, "not_found"

            stream_metadata: dict[str, Any] = {}
            try:
                parsed = json.loads(stream_row["metadata"] or "{}")
                if isinstance(parsed, dict):
                    stream_metadata = parsed
            except Exception:
                stream_metadata = {}

            if self._extract_stream_kind(stream_row, stream_metadata) != "telemetry":
                return None, "wrong_stream_kind"

            seq_row = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 AS next_seq FROM stream_events WHERE stream_id = ?",
                (sid,),
            ).fetchone()
            next_seq = int(seq_row["next_seq"] if seq_row and "next_seq" in seq_row.keys() else 1)
            event_id = f"STE{secrets.token_hex(8)}"

            conn.execute(
                """
                INSERT INTO stream_events (id, stream_id, seq, event_ts, content_type, payload, metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (event_id, sid, next_seq, event_ts_db, ctype, payload_text, metadata_json),
            )

            retention_max = self._event_retention_max(stream_row, stream_metadata)
            cutoff = next_seq - retention_max
            if cutoff > 0:
                conn.execute(
                    "DELETE FROM stream_events WHERE stream_id = ? AND seq <= ?",
                    (sid, cutoff),
                )

            conn.execute(
                "UPDATE streams SET updated_at = ? WHERE id = ?",
                (_db_ts(_utcnow()), sid),
            )
            conn.commit()

            row = conn.execute(
                """
                SELECT id, stream_id, seq, event_ts, content_type, payload, metadata, created_at
                FROM stream_events
                WHERE id = ?
                """,
                (event_id,),
            ).fetchone()
            if not row:
                return None, "event_store_failed"
            return self._row_to_event(row), None

    def _row_to_event(self, row: Any) -> dict[str, Any]:
        payload_text = str(row["payload"] or "")
        payload_value: Any = payload_text
        ctype = str(row["content_type"] or "")
        if "json" in ctype:
            try:
                payload_value = json.loads(payload_text)
            except Exception:
                payload_value = payload_text

        metadata: dict[str, Any] = {}
        try:
            parsed_meta = json.loads(row["metadata"] or "{}")
            if isinstance(parsed_meta, dict):
                metadata = parsed_meta
        except Exception:
            metadata = {}

        return {
            "id": str(row["id"]),
            "stream_id": str(row["stream_id"]),
            "seq": int(row["seq"] or 0),
            "event_ts": str(row["event_ts"] or ""),
            "content_type": ctype,
            "payload": payload_value,
            "metadata": metadata,
            "created_at": str(row["created_at"] or ""),
        }

    def list_events(
        self,
        *,
        stream_id: str,
        after_seq: int = 0,
        limit: int = 100,
    ) -> tuple[Optional[list[dict[str, Any]]], Optional[str]]:
        sid = str(stream_id or "").strip()
        if not sid:
            return None, "missing_stream_id"
        try:
            after = max(0, int(after_seq or 0))
        except Exception:
            after = 0
        try:
            lim = max(1, min(int(limit or 100), 500))
        except Exception:
            lim = 100

        with self.db.get_connection() as conn:
            stream_row = conn.execute("SELECT * FROM streams WHERE id = ?", (sid,)).fetchone()
            if not stream_row:
                return None, "not_found"
            stream_metadata: dict[str, Any] = {}
            try:
                parsed = json.loads(stream_row["metadata"] or "{}")
                if isinstance(parsed, dict):
                    stream_metadata = parsed
            except Exception:
                stream_metadata = {}

            if self._extract_stream_kind(stream_row, stream_metadata) != "telemetry":
                return None, "wrong_stream_kind"

            rows = conn.execute(
                """
                SELECT id, stream_id, seq, event_ts, content_type, payload, metadata, created_at
                FROM stream_events
                WHERE stream_id = ? AND seq > ?
                ORDER BY seq ASC
                LIMIT ?
                """,
                (sid, after, lim),
            ).fetchall()
            return [self._row_to_event(row) for row in rows], None
