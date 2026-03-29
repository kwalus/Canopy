"""Local-first personal bookmarks for saved Canopy sources."""

from __future__ import annotations

import json
import logging
import secrets
from typing import Any, Iterable, Optional

logger = logging.getLogger('canopy.bookmarks')


class BookmarkManager:
    """Manage private user bookmarks stored only on the local node."""

    def __init__(self, db_manager: Any):
        self.db = db_manager
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        with self.db.get_connection() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS user_bookmarks (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    source_type TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    container_type TEXT,
                    container_id TEXT,
                    source_author_id TEXT,
                    title TEXT,
                    preview TEXT,
                    source_href TEXT NOT NULL,
                    hero_ref TEXT,
                    deck_default_ref TEXT,
                    source_layout_json TEXT,
                    snapshot_json TEXT NOT NULL DEFAULT '{}',
                    note TEXT,
                    tags_json TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_opened_at TIMESTAMP,
                    archived_at TIMESTAMP,
                    UNIQUE(user_id, source_type, source_id)
                );

                CREATE INDEX IF NOT EXISTS idx_user_bookmarks_user_created
                    ON user_bookmarks(user_id, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_user_bookmarks_user_opened
                    ON user_bookmarks(user_id, last_opened_at DESC);
                CREATE INDEX IF NOT EXISTS idx_user_bookmarks_user_source
                    ON user_bookmarks(user_id, source_type, source_id);
                """
            )
            self._migrate_columns(conn)
            conn.commit()

    def _migrate_columns(self, conn: Any) -> None:
        rows = conn.execute("PRAGMA table_info(user_bookmarks)").fetchall()
        columns = {str(row[1]) for row in rows}
        required_columns = {
            'container_type': 'TEXT',
            'container_id': 'TEXT',
            'source_author_id': 'TEXT',
            'title': 'TEXT',
            'preview': 'TEXT',
            'source_href': "TEXT NOT NULL DEFAULT '/'",
            'hero_ref': 'TEXT',
            'deck_default_ref': 'TEXT',
            'source_layout_json': 'TEXT',
            'snapshot_json': "TEXT NOT NULL DEFAULT '{}'",
            'note': 'TEXT',
            'tags_json': 'TEXT',
            'updated_at': 'TIMESTAMP DEFAULT CURRENT_TIMESTAMP',
            'last_opened_at': 'TIMESTAMP',
            'archived_at': 'TIMESTAMP',
        }
        for name, spec in required_columns.items():
            if name in columns:
                continue
            conn.execute(f"ALTER TABLE user_bookmarks ADD COLUMN {name} {spec}")
            logger.info("Added %s column to user_bookmarks table", name)

    @staticmethod
    def _json_dumps(value: Any, fallback: str = '{}') -> str:
        try:
            return json.dumps(value or json.loads(fallback), separators=(',', ':'), ensure_ascii=True)
        except Exception:
            return fallback

    @staticmethod
    def _json_loads(value: Any, fallback: Any) -> Any:
        raw = str(value or '').strip()
        if not raw:
            return fallback
        try:
            return json.loads(raw)
        except Exception:
            return fallback

    def _serialize_row(self, row: Any) -> Optional[dict[str, Any]]:
        if not row:
            return None
        payload = dict(row)
        payload['source_layout'] = self._json_loads(payload.get('source_layout_json'), None)
        payload['snapshot'] = self._json_loads(payload.get('snapshot_json'), {})
        payload['tags'] = self._json_loads(payload.get('tags_json'), [])
        return payload

    def get_bookmark(self, bookmark_id: str, user_id: str) -> Optional[dict[str, Any]]:
        bookmark_id = str(bookmark_id or '').strip()
        user_id = str(user_id or '').strip()
        if not bookmark_id or not user_id:
            return None
        with self.db.get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM user_bookmarks WHERE id = ? AND user_id = ?",
                (bookmark_id, user_id),
            ).fetchone()
        return self._serialize_row(row)

    def get_bookmark_for_source(self, user_id: str, source_type: str, source_id: str) -> Optional[dict[str, Any]]:
        user_id = str(user_id or '').strip()
        source_type = str(source_type or '').strip()
        source_id = str(source_id or '').strip()
        if not user_id or not source_type or not source_id:
            return None
        with self.db.get_connection() as conn:
            row = conn.execute(
                """
                SELECT * FROM user_bookmarks
                WHERE user_id = ? AND source_type = ? AND source_id = ?
                """,
                (user_id, source_type, source_id),
            ).fetchone()
        return self._serialize_row(row)

    def get_bookmark_map(
        self,
        user_id: str,
        source_refs: Iterable[tuple[str, str]],
    ) -> dict[tuple[str, str], dict[str, Any]]:
        refs = [
            (str(source_type or '').strip(), str(source_id or '').strip())
            for source_type, source_id in source_refs
            if str(source_type or '').strip() and str(source_id or '').strip()
        ]
        if not user_id or not refs:
            return {}
        grouped: dict[str, list[str]] = {}
        for source_type, source_id in refs:
            grouped.setdefault(source_type, []).append(source_id)
        result: dict[tuple[str, str], dict[str, Any]] = {}
        with self.db.get_connection() as conn:
            for source_type, source_ids in grouped.items():
                unique_ids = sorted(set(source_ids))
                for idx in range(0, len(unique_ids), 150):
                    chunk = unique_ids[idx: idx + 150]
                    placeholders = ','.join('?' * len(chunk))
                    rows = conn.execute(
                        f"""
                        SELECT * FROM user_bookmarks
                        WHERE user_id = ? AND source_type = ? AND archived_at IS NULL
                          AND source_id IN ({placeholders})
                        """,
                        [user_id, source_type] + chunk,
                    ).fetchall()
                    for row in rows:
                        payload = self._serialize_row(row)
                        if payload:
                            result[(payload['source_type'], payload['source_id'])] = payload
        return result

    def list_bookmarks(
        self,
        user_id: str,
        *,
        include_archived: bool = False,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        user_id = str(user_id or '').strip()
        if not user_id:
            return []
        limit = max(1, min(int(limit or 200), 500))
        where_clause = '' if include_archived else 'AND archived_at IS NULL'
        with self.db.get_connection() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM user_bookmarks
                WHERE user_id = ? {where_clause}
                ORDER BY COALESCE(last_opened_at, created_at) DESC, created_at DESC
                LIMIT ?
                """,
                (user_id, limit),
            ).fetchall()
        return [payload for row in rows if (payload := self._serialize_row(row))]

    def upsert_bookmark(
        self,
        *,
        user_id: str,
        source_type: str,
        source_id: str,
        source_href: str,
        title: str,
        preview: str,
        snapshot: Optional[dict[str, Any]] = None,
        source_layout: Optional[dict[str, Any]] = None,
        hero_ref: Optional[str] = None,
        deck_default_ref: Optional[str] = None,
        container_type: Optional[str] = None,
        container_id: Optional[str] = None,
        source_author_id: Optional[str] = None,
    ) -> dict[str, Any]:
        user_id = str(user_id or '').strip()
        source_type = str(source_type or '').strip()
        source_id = str(source_id or '').strip()
        source_href = str(source_href or '').strip() or '/'
        title = str(title or '').strip()[:160]
        preview = str(preview or '').strip()[:600]
        if not user_id or not source_type or not source_id:
            raise ValueError('user_id, source_type, and source_id are required')

        snapshot_json = self._json_dumps(snapshot or {}, '{}')
        source_layout_json = self._json_dumps(source_layout, 'null') if source_layout else None

        with self.db.get_connection() as conn:
            existing_row = conn.execute(
                """
                SELECT id FROM user_bookmarks
                WHERE user_id = ? AND source_type = ? AND source_id = ?
                """,
                (user_id, source_type, source_id),
            ).fetchone()
            bookmark_id = existing_row['id'] if existing_row else f"BK{secrets.token_hex(12)}"
            if existing_row:
                conn.execute(
                    """
                    UPDATE user_bookmarks
                    SET container_type = ?,
                        container_id = ?,
                        source_author_id = ?,
                        title = ?,
                        preview = ?,
                        source_href = ?,
                        hero_ref = ?,
                        deck_default_ref = ?,
                        source_layout_json = ?,
                        snapshot_json = ?,
                        updated_at = CURRENT_TIMESTAMP,
                        archived_at = NULL
                    WHERE id = ? AND user_id = ?
                    """,
                    (
                        container_type,
                        container_id,
                        source_author_id,
                        title,
                        preview,
                        source_href,
                        hero_ref,
                        deck_default_ref,
                        source_layout_json,
                        snapshot_json,
                        bookmark_id,
                        user_id,
                    ),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO user_bookmarks (
                        id, user_id, source_type, source_id, container_type, container_id,
                        source_author_id, title, preview, source_href, hero_ref,
                        deck_default_ref, source_layout_json, snapshot_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        bookmark_id,
                        user_id,
                        source_type,
                        source_id,
                        container_type,
                        container_id,
                        source_author_id,
                        title,
                        preview,
                        source_href,
                        hero_ref,
                        deck_default_ref,
                        source_layout_json,
                        snapshot_json,
                    ),
                )
            conn.commit()
        created = self.get_bookmark(bookmark_id, user_id)
        if not created:
            raise RuntimeError('bookmark upsert failed')
        return created

    def remove_bookmark(self, bookmark_id: str, user_id: str) -> bool:
        bookmark_id = str(bookmark_id or '').strip()
        user_id = str(user_id or '').strip()
        if not bookmark_id or not user_id:
            return False
        with self.db.get_connection() as conn:
            cur = conn.execute(
                "DELETE FROM user_bookmarks WHERE id = ? AND user_id = ?",
                (bookmark_id, user_id),
            )
            conn.commit()
            return bool((cur.rowcount or 0) > 0)

    def remove_bookmark_for_source(self, user_id: str, source_type: str, source_id: str) -> bool:
        user_id = str(user_id or '').strip()
        source_type = str(source_type or '').strip()
        source_id = str(source_id or '').strip()
        if not user_id or not source_type or not source_id:
            return False
        with self.db.get_connection() as conn:
            cur = conn.execute(
                "DELETE FROM user_bookmarks WHERE user_id = ? AND source_type = ? AND source_id = ?",
                (user_id, source_type, source_id),
            )
            conn.commit()
            return bool((cur.rowcount or 0) > 0)

    def touch_bookmark_opened(self, bookmark_id: str, user_id: str) -> bool:
        bookmark_id = str(bookmark_id or '').strip()
        user_id = str(user_id or '').strip()
        if not bookmark_id or not user_id:
            return False
        with self.db.get_connection() as conn:
            cur = conn.execute(
                """
                UPDATE user_bookmarks
                SET last_opened_at = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND user_id = ?
                """,
                (bookmark_id, user_id),
            )
            conn.commit()
            return bool((cur.rowcount or 0) > 0)

    def update_bookmark_metadata(
        self,
        bookmark_id: str,
        user_id: str,
        *,
        note: Optional[str] = None,
        tags: Optional[list[str]] = None,
    ) -> Optional[dict[str, Any]]:
        bookmark_id = str(bookmark_id or '').strip()
        user_id = str(user_id or '').strip()
        if not bookmark_id or not user_id:
            return None
        clean_note = None if note is None else str(note).strip()[:4000]
        clean_tags = None
        if tags is not None:
            clean_tags = []
            for item in tags:
                tag = str(item or '').strip()
                if not tag or tag in clean_tags:
                    continue
                clean_tags.append(tag[:64])
        with self.db.get_connection() as conn:
            row = conn.execute(
                "SELECT note, tags_json FROM user_bookmarks WHERE id = ? AND user_id = ?",
                (bookmark_id, user_id),
            ).fetchone()
            if not row:
                return None
            next_note = clean_note if note is not None else row['note']
            next_tags_json = (
                self._json_dumps(clean_tags or [], '[]')
                if tags is not None
                else row['tags_json']
            )
            conn.execute(
                """
                UPDATE user_bookmarks
                SET note = ?,
                    tags_json = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND user_id = ?
                """,
                (next_note, next_tags_json, bookmark_id, user_id),
            )
            conn.commit()
        return self.get_bookmark(bookmark_id, user_id)
