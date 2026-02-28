"""
Local-first full-text search for Canopy.

Maintains a local FTS5 index across core content types so humans and
agents can query quickly without hitting the mesh.
"""

import json
import logging
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

from .database import DatabaseManager

logger = logging.getLogger('canopy.search')


class SearchManager:
    """Maintains an FTS5 index for local Canopy content."""

    INDEX_VERSION = "5"

    SUPPORTED_TYPES = (
        'feed_post',
        'channel_message',
        'task',
        'request',
        'objective',
        'circle',
        'circle_entry',
        'handoff',
        'skill',
        'signal',
    )

    TYPE_ALIASES = {
        'feed': 'feed_post',
        'post': 'feed_post',
        'posts': 'feed_post',
        'skills': 'skill',
        'feed_posts': 'feed_post',
        'channel': 'channel_message',
        'channels': 'channel_message',
        'message': 'channel_message',
        'messages': 'channel_message',
        'circle_entries': 'circle_entry',
        'handoffs': 'handoff',
        'objective': 'objective',
        'objectives': 'objective',
        'request': 'request',
        'requests': 'request',
        'signal': 'signal',
        'signals': 'signal',
    }

    def __init__(self, db: DatabaseManager):
        self.db = db
        self.enabled = False
        self._ensure_index()
        self._maybe_rebuild_index()

    def _ensure_index(self) -> None:
        """Create the FTS table and triggers if available."""
        try:
            with self.db.get_connection() as conn:
                conn.execute(
                    """
                    CREATE VIRTUAL TABLE IF NOT EXISTS local_search_fts
                    USING fts5(
                        title,
                        body,
                        tags,
                        item_type UNINDEXED,
                        item_id UNINDEXED,
                        channel_id UNINDEXED,
                        source_id UNINDEXED,
                        author_id UNINDEXED,
                        recipient_id UNINDEXED,
                        visibility UNINDEXED,
                        created_at UNINDEXED,
                        updated_at UNINDEXED,
                        expires_at UNINDEXED
                    )
                    """
                )
                self.enabled = True
                self._create_triggers(conn)
                conn.commit()
        except sqlite3.OperationalError as e:
            logger.warning(f"FTS5 unavailable; local search disabled: {e}")
            self.enabled = False
        except Exception as e:
            logger.error(f"Failed to initialize search index: {e}", exc_info=True)
            self.enabled = False

    def _table_exists(self, conn: Any, name: str) -> bool:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
            (name,)
        ).fetchone()
        return row is not None

    def _create_triggers(self, conn: Any) -> None:
        """Create triggers to keep the index in sync."""
        if not self.enabled:
            return

        if self._table_exists(conn, 'feed_posts'):
            conn.executescript(
                """
                CREATE TRIGGER IF NOT EXISTS local_search_feed_ai
                AFTER INSERT ON feed_posts
                BEGIN
                    INSERT INTO local_search_fts(
                        title, body, tags, item_type, item_id, channel_id, source_id,
                        author_id, recipient_id, visibility, created_at, updated_at, expires_at
                    ) VALUES (
                        '',
                        new.content,
                        COALESCE(new.tags, ''),
                        'feed_post',
                        new.id,
                        NULL,
                        NULL,
                        new.author_id,
                        NULL,
                        new.visibility,
                        new.created_at,
                        new.created_at,
                        new.expires_at
                    );
                END;

                CREATE TRIGGER IF NOT EXISTS local_search_feed_au
                AFTER UPDATE ON feed_posts
                BEGIN
                    DELETE FROM local_search_fts
                    WHERE item_type = 'feed_post' AND item_id = old.id;

                    INSERT INTO local_search_fts(
                        title, body, tags, item_type, item_id, channel_id, source_id,
                        author_id, recipient_id, visibility, created_at, updated_at, expires_at
                    ) VALUES (
                        '',
                        new.content,
                        COALESCE(new.tags, ''),
                        'feed_post',
                        new.id,
                        NULL,
                        NULL,
                        new.author_id,
                        NULL,
                        new.visibility,
                        new.created_at,
                        CURRENT_TIMESTAMP,
                        new.expires_at
                    );
                END;

                CREATE TRIGGER IF NOT EXISTS local_search_feed_ad
                AFTER DELETE ON feed_posts
                BEGIN
                    DELETE FROM local_search_fts
                    WHERE item_type = 'feed_post' AND item_id = old.id;
                END;
                """
            )

        if self._table_exists(conn, 'channel_messages'):
            conn.executescript(
                """
                CREATE TRIGGER IF NOT EXISTS local_search_channel_msg_ai
                AFTER INSERT ON channel_messages
                BEGIN
                    INSERT INTO local_search_fts(
                        title, body, tags, item_type, item_id, channel_id, source_id,
                        author_id, recipient_id, visibility, created_at, updated_at, expires_at
                    ) VALUES (
                        '',
                        new.content,
                        '',
                        'channel_message',
                        new.id,
                        new.channel_id,
                        NULL,
                        new.user_id,
                        NULL,
                        'channel',
                        new.created_at,
                        COALESCE(new.edited_at, new.created_at),
                        new.expires_at
                    );
                END;

                CREATE TRIGGER IF NOT EXISTS local_search_channel_msg_au
                AFTER UPDATE ON channel_messages
                BEGIN
                    DELETE FROM local_search_fts
                    WHERE item_type = 'channel_message' AND item_id = old.id;

                    INSERT INTO local_search_fts(
                        title, body, tags, item_type, item_id, channel_id, source_id,
                        author_id, recipient_id, visibility, created_at, updated_at, expires_at
                    ) VALUES (
                        '',
                        new.content,
                        '',
                        'channel_message',
                        new.id,
                        new.channel_id,
                        NULL,
                        new.user_id,
                        NULL,
                        'channel',
                        new.created_at,
                        COALESCE(new.edited_at, new.created_at, CURRENT_TIMESTAMP),
                        new.expires_at
                    );
                END;

                CREATE TRIGGER IF NOT EXISTS local_search_channel_msg_ad
                AFTER DELETE ON channel_messages
                BEGIN
                    DELETE FROM local_search_fts
                    WHERE item_type = 'channel_message' AND item_id = old.id;
                END;
                """
            )

        if self._table_exists(conn, 'tasks'):
            conn.executescript(
                """
                CREATE TRIGGER IF NOT EXISTS local_search_tasks_ai
                AFTER INSERT ON tasks
                BEGIN
                    INSERT INTO local_search_fts(
                        title, body, tags, item_type, item_id, channel_id, source_id,
                        author_id, recipient_id, visibility, created_at, updated_at, expires_at
                    ) VALUES (
                        new.title,
                        COALESCE(new.description, ''),
                        '',
                        'task',
                        new.id,
                        NULL,
                        NULL,
                        new.created_by,
                        new.assigned_to,
                        new.visibility,
                        new.created_at,
                        COALESCE(new.updated_at, new.created_at),
                        NULL
                    );
                END;

                CREATE TRIGGER IF NOT EXISTS local_search_tasks_au
                AFTER UPDATE ON tasks
                BEGIN
                    DELETE FROM local_search_fts
                    WHERE item_type = 'task' AND item_id = old.id;

                    INSERT INTO local_search_fts(
                        title, body, tags, item_type, item_id, channel_id, source_id,
                        author_id, recipient_id, visibility, created_at, updated_at, expires_at
                    ) VALUES (
                        new.title,
                        COALESCE(new.description, ''),
                        '',
                        'task',
                        new.id,
                        NULL,
                        NULL,
                        new.created_by,
                        new.assigned_to,
                        new.visibility,
                        new.created_at,
                        COALESCE(new.updated_at, CURRENT_TIMESTAMP),
                        NULL
                    );
                END;

                CREATE TRIGGER IF NOT EXISTS local_search_tasks_ad
                AFTER DELETE ON tasks
                BEGIN
                    DELETE FROM local_search_fts
                    WHERE item_type = 'task' AND item_id = old.id;
                END;
                """
            )

        if self._table_exists(conn, 'requests'):
            conn.executescript(
                """
                CREATE TRIGGER IF NOT EXISTS local_search_requests_ai
                AFTER INSERT ON requests
                BEGIN
                    INSERT INTO local_search_fts(
                        title, body, tags, item_type, item_id, channel_id, source_id,
                        author_id, recipient_id, visibility, created_at, updated_at, expires_at
                    ) VALUES (
                        new.title,
                        COALESCE(new.request, '') || '\n' || COALESCE(new.required_output, ''),
                        COALESCE(new.tags, ''),
                        'request',
                        new.id,
                        NULL,
                        new.source_id,
                        new.created_by,
                        NULL,
                        COALESCE(new.visibility, 'network'),
                        new.created_at,
                        COALESCE(new.updated_at, new.created_at),
                        new.due_at
                    );
                END;

                CREATE TRIGGER IF NOT EXISTS local_search_requests_au
                AFTER UPDATE ON requests
                BEGIN
                    DELETE FROM local_search_fts
                    WHERE item_type = 'request' AND item_id = old.id;

                    INSERT INTO local_search_fts(
                        title, body, tags, item_type, item_id, channel_id, source_id,
                        author_id, recipient_id, visibility, created_at, updated_at, expires_at
                    ) VALUES (
                        new.title,
                        COALESCE(new.request, '') || '\n' || COALESCE(new.required_output, ''),
                        COALESCE(new.tags, ''),
                        'request',
                        new.id,
                        NULL,
                        new.source_id,
                        new.created_by,
                        NULL,
                        COALESCE(new.visibility, 'network'),
                        new.created_at,
                        COALESCE(new.updated_at, CURRENT_TIMESTAMP),
                        new.due_at
                    );
                END;

                CREATE TRIGGER IF NOT EXISTS local_search_requests_ad
                AFTER DELETE ON requests
                BEGIN
                    DELETE FROM local_search_fts
                    WHERE item_type = 'request' AND item_id = old.id;
                END;
                """
            )

        if self._table_exists(conn, 'objectives'):
            conn.executescript(
                """
                CREATE TRIGGER IF NOT EXISTS local_search_objectives_ai
                AFTER INSERT ON objectives
                BEGIN
                    INSERT INTO local_search_fts(
                        title, body, tags, item_type, item_id, channel_id, source_id,
                        author_id, recipient_id, visibility, created_at, updated_at, expires_at
                    ) VALUES (
                        new.title,
                        COALESCE(new.description, ''),
                        COALESCE(new.status, ''),
                        'objective',
                        new.id,
                        NULL,
                        new.source_id,
                        new.created_by,
                        NULL,
                        COALESCE(new.visibility, 'network'),
                        new.created_at,
                        COALESCE(new.updated_at, new.created_at),
                        new.deadline
                    );
                END;

                CREATE TRIGGER IF NOT EXISTS local_search_objectives_au
                AFTER UPDATE ON objectives
                BEGIN
                    DELETE FROM local_search_fts
                    WHERE item_type = 'objective' AND item_id = old.id;

                    INSERT INTO local_search_fts(
                        title, body, tags, item_type, item_id, channel_id, source_id,
                        author_id, recipient_id, visibility, created_at, updated_at, expires_at
                    ) VALUES (
                        new.title,
                        COALESCE(new.description, ''),
                        COALESCE(new.status, ''),
                        'objective',
                        new.id,
                        NULL,
                        new.source_id,
                        new.created_by,
                        NULL,
                        COALESCE(new.visibility, 'network'),
                        new.created_at,
                        COALESCE(new.updated_at, CURRENT_TIMESTAMP),
                        new.deadline
                    );
                END;

                CREATE TRIGGER IF NOT EXISTS local_search_objectives_ad
                AFTER DELETE ON objectives
                BEGIN
                    DELETE FROM local_search_fts
                    WHERE item_type = 'objective' AND item_id = old.id;
                END;
                """
            )

        if self._table_exists(conn, 'signals'):
            conn.executescript(
                """
                CREATE TRIGGER IF NOT EXISTS local_search_signals_ai
                AFTER INSERT ON signals
                BEGIN
                    INSERT INTO local_search_fts(
                        title, body, tags, item_type, item_id, channel_id, source_id,
                        author_id, recipient_id, visibility, created_at, updated_at, expires_at
                    ) VALUES (
                        new.title,
                        COALESCE(new.summary, '') || '\n' || COALESCE(new.notes, '') || '\n' || COALESCE(new.data, ''),
                        COALESCE(new.tags, ''),
                        'signal',
                        new.id,
                        NULL,
                        new.source_id,
                        new.created_by,
                        new.owner_id,
                        COALESCE(new.visibility, 'network'),
                        new.created_at,
                        COALESCE(new.updated_at, new.created_at),
                        new.expires_at
                    );
                END;

                CREATE TRIGGER IF NOT EXISTS local_search_signals_au
                AFTER UPDATE ON signals
                BEGIN
                    DELETE FROM local_search_fts
                    WHERE item_type = 'signal' AND item_id = old.id;

                    INSERT INTO local_search_fts(
                        title, body, tags, item_type, item_id, channel_id, source_id,
                        author_id, recipient_id, visibility, created_at, updated_at, expires_at
                    ) VALUES (
                        new.title,
                        COALESCE(new.summary, '') || '\n' || COALESCE(new.notes, '') || '\n' || COALESCE(new.data, ''),
                        COALESCE(new.tags, ''),
                        'signal',
                        new.id,
                        NULL,
                        new.source_id,
                        new.created_by,
                        new.owner_id,
                        COALESCE(new.visibility, 'network'),
                        new.created_at,
                        COALESCE(new.updated_at, CURRENT_TIMESTAMP),
                        new.expires_at
                    );
                END;

                CREATE TRIGGER IF NOT EXISTS local_search_signals_ad
                AFTER DELETE ON signals
                BEGIN
                    DELETE FROM local_search_fts
                    WHERE item_type = 'signal' AND item_id = old.id;
                END;
                """
            )

        if self._table_exists(conn, 'circles'):
            conn.executescript(
                """
                CREATE TRIGGER IF NOT EXISTS local_search_circles_ai
                AFTER INSERT ON circles
                BEGIN
                    INSERT INTO local_search_fts(
                        title, body, tags, item_type, item_id, channel_id, source_id,
                        author_id, recipient_id, visibility, created_at, updated_at, expires_at
                    ) VALUES (
                        new.topic,
                        COALESCE(new.description, '') || '\n' || COALESCE(new.summary, '') || '\n' || COALESCE(new.decision, ''),
                        '',
                        'circle',
                        new.id,
                        new.channel_id,
                        new.source_id,
                        new.created_by,
                        new.facilitator_id,
                        new.visibility,
                        new.created_at,
                        COALESCE(new.updated_at, new.created_at),
                        new.ends_at
                    );
                END;

                CREATE TRIGGER IF NOT EXISTS local_search_circles_au
                AFTER UPDATE ON circles
                BEGIN
                    DELETE FROM local_search_fts
                    WHERE item_type = 'circle' AND item_id = old.id;

                    INSERT INTO local_search_fts(
                        title, body, tags, item_type, item_id, channel_id, source_id,
                        author_id, recipient_id, visibility, created_at, updated_at, expires_at
                    ) VALUES (
                        new.topic,
                        COALESCE(new.description, '') || '\n' || COALESCE(new.summary, '') || '\n' || COALESCE(new.decision, ''),
                        '',
                        'circle',
                        new.id,
                        new.channel_id,
                        new.source_id,
                        new.created_by,
                        new.facilitator_id,
                        new.visibility,
                        new.created_at,
                        COALESCE(new.updated_at, CURRENT_TIMESTAMP),
                        new.ends_at
                    );
                END;

                CREATE TRIGGER IF NOT EXISTS local_search_circles_ad
                AFTER DELETE ON circles
                BEGIN
                    DELETE FROM local_search_fts
                    WHERE item_type = 'circle' AND item_id = old.id;
                END;
                """
            )

        if self._table_exists(conn, 'circle_entries'):
            conn.executescript(
                """
                CREATE TRIGGER IF NOT EXISTS local_search_circle_entries_ai
                AFTER INSERT ON circle_entries
                BEGIN
                    INSERT INTO local_search_fts(
                        title, body, tags, item_type, item_id, channel_id, source_id,
                        author_id, recipient_id, visibility, created_at, updated_at, expires_at
                    ) VALUES (
                        '',
                        new.content,
                        '',
                        'circle_entry',
                        new.id,
                        (SELECT channel_id FROM circles WHERE id = new.circle_id),
                        new.circle_id,
                        new.user_id,
                        NULL,
                        COALESCE((SELECT visibility FROM circles WHERE id = new.circle_id), 'network'),
                        new.created_at,
                        COALESCE(new.edited_at, new.created_at),
                        NULL
                    );
                END;

                CREATE TRIGGER IF NOT EXISTS local_search_circle_entries_au
                AFTER UPDATE ON circle_entries
                BEGIN
                    DELETE FROM local_search_fts
                    WHERE item_type = 'circle_entry' AND item_id = old.id;

                    INSERT INTO local_search_fts(
                        title, body, tags, item_type, item_id, channel_id, source_id,
                        author_id, recipient_id, visibility, created_at, updated_at, expires_at
                    ) VALUES (
                        '',
                        new.content,
                        '',
                        'circle_entry',
                        new.id,
                        (SELECT channel_id FROM circles WHERE id = new.circle_id),
                        new.circle_id,
                        new.user_id,
                        NULL,
                        COALESCE((SELECT visibility FROM circles WHERE id = new.circle_id), 'network'),
                        new.created_at,
                        COALESCE(new.edited_at, new.created_at, CURRENT_TIMESTAMP),
                        NULL
                    );
                END;

                CREATE TRIGGER IF NOT EXISTS local_search_circle_entries_ad
                AFTER DELETE ON circle_entries
                BEGIN
                    DELETE FROM local_search_fts
                    WHERE item_type = 'circle_entry' AND item_id = old.id;
                END;
                """
            )

        if self._table_exists(conn, 'handoff_notes'):
            conn.executescript(
                """
                CREATE TRIGGER IF NOT EXISTS local_search_handoffs_ai
                AFTER INSERT ON handoff_notes
                BEGIN
                    INSERT INTO local_search_fts(
                        title, body, tags, item_type, item_id, channel_id, source_id,
                        author_id, recipient_id, visibility, created_at, updated_at, expires_at
                    ) VALUES (
                        new.title,
                        COALESCE(new.summary, '') || '\n' || COALESCE(new.next_steps, '') || '\n' || COALESCE(new.raw, ''),
                        COALESCE(new.tags, ''),
                        'handoff',
                        new.id,
                        new.channel_id,
                        new.source_id,
                        new.author_id,
                        new.owner,
                        new.visibility,
                        new.created_at,
                        COALESCE(new.updated_at, new.created_at),
                        NULL
                    );
                END;

                CREATE TRIGGER IF NOT EXISTS local_search_handoffs_au
                AFTER UPDATE ON handoff_notes
                BEGIN
                    DELETE FROM local_search_fts
                    WHERE item_type = 'handoff' AND item_id = old.id;

                    INSERT INTO local_search_fts(
                        title, body, tags, item_type, item_id, channel_id, source_id,
                        author_id, recipient_id, visibility, created_at, updated_at, expires_at
                    ) VALUES (
                        new.title,
                        COALESCE(new.summary, '') || '\n' || COALESCE(new.next_steps, '') || '\n' || COALESCE(new.raw, ''),
                        COALESCE(new.tags, ''),
                        'handoff',
                        new.id,
                        new.channel_id,
                        new.source_id,
                        new.author_id,
                        new.owner,
                        new.visibility,
                        new.created_at,
                        COALESCE(new.updated_at, CURRENT_TIMESTAMP),
                        NULL
                    );
                END;

                CREATE TRIGGER IF NOT EXISTS local_search_handoffs_ad
                AFTER DELETE ON handoff_notes
                BEGIN
                    DELETE FROM local_search_fts
                    WHERE item_type = 'handoff' AND item_id = old.id;
                END;
                """
            )

        if self._table_exists(conn, 'skills'):
            conn.executescript(
                """
                CREATE TRIGGER IF NOT EXISTS local_search_skills_ai
                AFTER INSERT ON skills
                BEGIN
                    INSERT INTO local_search_fts(
                        title, body, tags, item_type, item_id, channel_id, source_id,
                        author_id, recipient_id, visibility, created_at, updated_at, expires_at
                    ) VALUES (
                        new.name,
                        COALESCE(new.description, '') || ' ' || COALESCE(new.invokes, ''),
                        new.tags,
                        'skill',
                        new.id,
                        new.channel_id,
                        new.source_id,
                        new.author_id,
                        NULL,
                        'network',
                        new.created_at,
                        new.updated_at,
                        NULL
                    );
                END;

                CREATE TRIGGER IF NOT EXISTS local_search_skills_au
                AFTER UPDATE ON skills
                BEGIN
                    DELETE FROM local_search_fts
                    WHERE item_type = 'skill' AND item_id = old.id;

                    INSERT INTO local_search_fts(
                        title, body, tags, item_type, item_id, channel_id, source_id,
                        author_id, recipient_id, visibility, created_at, updated_at, expires_at
                    ) VALUES (
                        new.name,
                        COALESCE(new.description, '') || ' ' || COALESCE(new.invokes, ''),
                        new.tags,
                        'skill',
                        new.id,
                        new.channel_id,
                        new.source_id,
                        new.author_id,
                        NULL,
                        'network',
                        new.created_at,
                        new.updated_at,
                        NULL
                    );
                END;

                CREATE TRIGGER IF NOT EXISTS local_search_skills_ad
                AFTER DELETE ON skills
                BEGIN
                    DELETE FROM local_search_fts
                    WHERE item_type = 'skill' AND item_id = old.id;
                END;
                """
            )

    def _maybe_rebuild_index(self) -> None:
        if not self.enabled:
            return
        try:
            current = self.db.get_system_state('search_index_version')
            if current == self.INDEX_VERSION:
                return
            if self.rebuild_index():
                self.db.set_system_state('search_index_version', self.INDEX_VERSION)
        except Exception as e:
            logger.error(f"Failed to check search index version: {e}")

    def rebuild_index(self) -> bool:
        if not self.enabled:
            return False
        try:
            with self.db.get_connection() as conn:
                conn.execute("DELETE FROM local_search_fts")

                if self._table_exists(conn, 'feed_posts'):
                    conn.execute(
                        """
                        INSERT INTO local_search_fts(
                            title, body, tags, item_type, item_id, channel_id, source_id,
                            author_id, recipient_id, visibility, created_at, updated_at, expires_at
                        )
                        SELECT
                            '',
                            content,
                            COALESCE(tags, ''),
                            'feed_post',
                            id,
                            NULL,
                            NULL,
                            author_id,
                            NULL,
                            visibility,
                            created_at,
                            created_at,
                            expires_at
                        FROM feed_posts
                        """
                    )

                if self._table_exists(conn, 'channel_messages'):
                    conn.execute(
                        """
                        INSERT INTO local_search_fts(
                            title, body, tags, item_type, item_id, channel_id, source_id,
                            author_id, recipient_id, visibility, created_at, updated_at, expires_at
                        )
                        SELECT
                            '',
                            content,
                            '',
                            'channel_message',
                            id,
                            channel_id,
                            NULL,
                            user_id,
                            NULL,
                            'channel',
                            created_at,
                            COALESCE(edited_at, created_at),
                            expires_at
                        FROM channel_messages
                        """
                    )

                if self._table_exists(conn, 'tasks'):
                    conn.execute(
                        """
                        INSERT INTO local_search_fts(
                            title, body, tags, item_type, item_id, channel_id, source_id,
                            author_id, recipient_id, visibility, created_at, updated_at, expires_at
                        )
                        SELECT
                            title,
                            COALESCE(description, ''),
                            '',
                            'task',
                            id,
                            NULL,
                            NULL,
                            created_by,
                            assigned_to,
                            visibility,
                            created_at,
                            COALESCE(updated_at, created_at),
                            NULL
                        FROM tasks
                        """
                    )

                if self._table_exists(conn, 'requests'):
                    conn.execute(
                        """
                        INSERT INTO local_search_fts(
                            title, body, tags, item_type, item_id, channel_id, source_id,
                            author_id, recipient_id, visibility, created_at, updated_at, expires_at
                        )
                        SELECT
                            title,
                            COALESCE(request, '') || '\n' || COALESCE(required_output, ''),
                            COALESCE(tags, ''),
                            'request',
                            id,
                            NULL,
                            source_id,
                            created_by,
                            NULL,
                            COALESCE(visibility, 'network'),
                            created_at,
                            COALESCE(updated_at, created_at),
                            due_at
                        FROM requests
                        """
                    )

                if self._table_exists(conn, 'objectives'):
                    conn.execute(
                        """
                        INSERT INTO local_search_fts(
                            title, body, tags, item_type, item_id, channel_id, source_id,
                            author_id, recipient_id, visibility, created_at, updated_at, expires_at
                        )
                        SELECT
                            title,
                            COALESCE(description, ''),
                            COALESCE(status, ''),
                            'objective',
                            id,
                            NULL,
                            source_id,
                            created_by,
                            NULL,
                            COALESCE(visibility, 'network'),
                            created_at,
                            COALESCE(updated_at, created_at),
                            deadline
                        FROM objectives
                        """
                    )

                if self._table_exists(conn, 'signals'):
                    conn.execute(
                        """
                        INSERT INTO local_search_fts(
                            title, body, tags, item_type, item_id, channel_id, source_id,
                            author_id, recipient_id, visibility, created_at, updated_at, expires_at
                        )
                        SELECT
                            title,
                            COALESCE(summary, '') || '\n' || COALESCE(notes, '') || '\n' || COALESCE(data, ''),
                            COALESCE(tags, ''),
                            'signal',
                            id,
                            NULL,
                            source_id,
                            created_by,
                            owner_id,
                            COALESCE(visibility, 'network'),
                            created_at,
                            COALESCE(updated_at, created_at),
                            expires_at
                        FROM signals
                        """
                    )

                if self._table_exists(conn, 'circles'):
                    conn.execute(
                        """
                        INSERT INTO local_search_fts(
                            title, body, tags, item_type, item_id, channel_id, source_id,
                            author_id, recipient_id, visibility, created_at, updated_at, expires_at
                        )
                        SELECT
                            topic,
                            COALESCE(description, '') || '\n' || COALESCE(summary, '') || '\n' || COALESCE(decision, ''),
                            '',
                            'circle',
                            id,
                            channel_id,
                            source_id,
                            created_by,
                            facilitator_id,
                            visibility,
                            created_at,
                            COALESCE(updated_at, created_at),
                            ends_at
                        FROM circles
                        """
                    )

                if self._table_exists(conn, 'circle_entries'):
                    conn.execute(
                        """
                        INSERT INTO local_search_fts(
                            title, body, tags, item_type, item_id, channel_id, source_id,
                            author_id, recipient_id, visibility, created_at, updated_at, expires_at
                        )
                        SELECT
                            '',
                            e.content,
                            '',
                            'circle_entry',
                            e.id,
                            c.channel_id,
                            e.circle_id,
                            e.user_id,
                            NULL,
                            COALESCE(c.visibility, 'network'),
                            e.created_at,
                            COALESCE(e.edited_at, e.created_at),
                            NULL
                        FROM circle_entries e
                        LEFT JOIN circles c ON c.id = e.circle_id
                        """
                    )

                if self._table_exists(conn, 'handoff_notes'):
                    conn.execute(
                        """
                        INSERT INTO local_search_fts(
                            title, body, tags, item_type, item_id, channel_id, source_id,
                            author_id, recipient_id, visibility, created_at, updated_at, expires_at
                        )
                        SELECT
                            title,
                            COALESCE(summary, '') || '\n' || COALESCE(next_steps, '') || '\n' || COALESCE(raw, ''),
                            COALESCE(tags, ''),
                            'handoff',
                            id,
                            channel_id,
                            source_id,
                            author_id,
                            owner,
                            visibility,
                            created_at,
                            COALESCE(updated_at, created_at),
                            NULL
                        FROM handoff_notes
                        """
                    )

                if self._table_exists(conn, 'skills'):
                    conn.execute(
                        """
                        INSERT INTO local_search_fts(
                            title, body, tags, item_type, item_id, channel_id, source_id,
                            author_id, recipient_id, visibility, created_at, updated_at, expires_at
                        )
                        SELECT
                            name,
                            COALESCE(description, '') || ' ' || COALESCE(invokes, ''),
                            tags,
                            'skill',
                            id,
                            channel_id,
                            source_id,
                            author_id,
                            NULL,
                            'network',
                            created_at,
                            updated_at,
                            NULL
                        FROM skills
                        """
                    )

                conn.commit()
            logger.info("Local search index rebuilt")
            return True
        except Exception as e:
            logger.error(f"Failed to rebuild search index: {e}", exc_info=True)
            return False

    def _normalize_types(self, types: Optional[Sequence[str]]) -> Optional[List[str]]:
        if not types:
            return None
        normalized: List[str] = []
        for entry in types:
            if not entry:
                continue
            key = str(entry).strip().lower()
            if not key:
                continue
            key = self.TYPE_ALIASES.get(key, key)
            if key in self.SUPPORTED_TYPES and key not in normalized:
                normalized.append(key)
        return normalized or None

    def _sanitize_query(self, query: str) -> str:
        tokens = re.findall(r"[A-Za-z0-9_@#\.-]+", query)
        return " ".join(tokens)

    def search(self, query: str, user_id: Optional[str], limit: int = 50,
               types: Optional[Sequence[str]] = None) -> List[Dict[str, Any]]:
        """Search indexed content with basic visibility gating."""
        if not self.enabled:
            return []
        if not query or not query.strip():
            return []

        limit_val = max(1, min(int(limit or 50), 200))
        candidate_limit = max(limit_val * 5, limit_val)
        type_filter = self._normalize_types(types)
        match_query = query.strip()

        results: List[Dict[str, Any]] = []
        with self.db.get_connection() as conn:
            sql = (
                "SELECT item_type, item_id, title, channel_id, source_id, author_id, recipient_id, "
                "visibility, created_at, updated_at, expires_at, "
                "snippet(local_search_fts, 1, '<b>', '</b>', '…', 8) AS snippet, "
                "bm25(local_search_fts) AS score "
                "FROM local_search_fts WHERE local_search_fts MATCH ?"
            )
            params: List[Any] = [match_query]
            if type_filter:
                placeholders = ",".join("?" for _ in type_filter)
                sql += f" AND item_type IN ({placeholders})"
                params.extend(type_filter)
            sql += " ORDER BY score LIMIT ?"
            params.append(candidate_limit)

            try:
                rows = conn.execute(sql, params).fetchall()
            except sqlite3.OperationalError:
                fallback = self._sanitize_query(match_query)
                if not fallback:
                    return []
                params[0] = fallback
                rows = conn.execute(sql, params).fetchall()

            if not rows:
                return []

            # Precompute channel memberships
            member_channels: set = set()
            if user_id:
                try:
                    member_rows = conn.execute(
                        "SELECT channel_id FROM channel_members WHERE user_id = ?",
                        (user_id,)
                    ).fetchall()
                    member_channels = {r['channel_id'] for r in member_rows}
                except Exception:
                    member_channels = set()

            # Gather IDs by type for visibility checks
            feed_ids = [r['item_id'] for r in rows if r['item_type'] == 'feed_post']
            channel_ids = [r['item_id'] for r in rows if r['item_type'] == 'channel_message']
            handoff_ids = [r['item_id'] for r in rows if r['item_type'] == 'handoff']
            signal_ids = [r['item_id'] for r in rows if r['item_type'] == 'signal']

            allowed_feed: set = set()
            if feed_ids:
                placeholders = ",".join("?" for _ in feed_ids)
                query_sql = (
                    "SELECT p.id, p.author_id, p.visibility, p.expires_at, "
                    "CASE WHEN pp.user_id IS NOT NULL THEN 1 ELSE 0 END AS has_custom "
                    "FROM feed_posts p "
                    "LEFT JOIN post_permissions pp ON p.id = pp.post_id AND pp.user_id = ? "
                    f"WHERE p.id IN ({placeholders}) "
                    "AND (p.expires_at IS NULL OR p.expires_at > CURRENT_TIMESTAMP)"
                )
                params = [user_id] + feed_ids
                for row in conn.execute(query_sql, params).fetchall():
                    visibility = (row['visibility'] or '').lower()
                    if visibility in ('public', 'network'):
                        allowed_feed.add(row['id'])
                        continue
                    if user_id and row['author_id'] == user_id:
                        allowed_feed.add(row['id'])
                        continue
                    if visibility == 'custom' and row['has_custom']:
                        allowed_feed.add(row['id'])
                        continue

            allowed_channel_msgs: set = set()
            if channel_ids and member_channels:
                placeholders = ",".join("?" for _ in channel_ids)
                query_sql = (
                    f"SELECT id, channel_id FROM channel_messages WHERE id IN ({placeholders}) "
                    "AND (expires_at IS NULL OR expires_at > CURRENT_TIMESTAMP)"
                )
                for row in conn.execute(query_sql, channel_ids).fetchall():
                    if row['channel_id'] in member_channels:
                        allowed_channel_msgs.add(row['id'])

            allowed_handoffs: set = set()
            if handoff_ids:
                placeholders = ",".join("?" for _ in handoff_ids)
                query_sql = (
                    f"SELECT id, channel_id, visibility, author_id, permissions FROM handoff_notes "
                    f"WHERE id IN ({placeholders})"
                )
                for row in conn.execute(query_sql, handoff_ids).fetchall():
                    if row['channel_id'] and row['channel_id'] not in member_channels:
                        continue
                    visibility = (row['visibility'] or 'network').lower()
                    if visibility == 'private' and user_id and row['author_id'] != user_id:
                        continue
                    if visibility == 'custom':
                        allowed = False
                        if user_id and row['author_id'] == user_id:
                            allowed = True
                        if row['permissions']:
                            try:
                                perms = json.loads(row['permissions'])
                                if user_id and user_id in perms:
                                    allowed = True
                            except Exception:
                                pass
                        if not allowed:
                            continue
                    allowed_handoffs.add(row['id'])

            allowed_signals: set = set()
            if signal_ids:
                admin_user_id = None
                try:
                    admin_user_id = self.db.get_instance_owner_user_id()
                except Exception:
                    admin_user_id = None
                placeholders = ",".join("?" for _ in signal_ids)
                query_sql = (
                    f"SELECT id, owner_id, created_by, visibility, expires_at FROM signals "
                    f"WHERE id IN ({placeholders})"
                )
                now_iso = datetime.now(timezone.utc).isoformat()
                for row in conn.execute(query_sql, signal_ids).fetchall():
                    if row['expires_at'] and row['expires_at'] <= now_iso:
                        continue
                    visibility = (row['visibility'] or 'network').lower()
                    if visibility in ('public', 'network'):
                        allowed_signals.add(row['id'])
                        continue
                    if user_id and (row['owner_id'] == user_id or row['created_by'] == user_id):
                        allowed_signals.add(row['id'])
                        continue
                    if admin_user_id and user_id == admin_user_id:
                        allowed_signals.add(row['id'])
                        continue

            for row in rows:
                item_type = row['item_type']
                item_id = row['item_id']

                if item_type == 'feed_post' and item_id not in allowed_feed:
                    continue
                if item_type == 'channel_message' and item_id not in allowed_channel_msgs:
                    continue
                if item_type == 'handoff' and item_id not in allowed_handoffs:
                    continue
                if item_type == 'signal' and item_id not in allowed_signals:
                    continue
                if item_type in ('circle', 'circle_entry'):
                    channel_id = row['channel_id']
                    if channel_id and channel_id not in member_channels:
                        continue

                results.append({
                    'item_type': item_type,
                    'item_id': item_id,
                    'title': row['title'] or '',
                    'snippet': row['snippet'] or '',
                    'score': row['score'],
                    'channel_id': row['channel_id'],
                    'source_id': row['source_id'],
                    'author_id': row['author_id'],
                    'recipient_id': row['recipient_id'],
                    'visibility': row['visibility'],
                    'created_at': row['created_at'],
                    'updated_at': row['updated_at'],
                })

                if len(results) >= limit_val:
                    break

        return results
