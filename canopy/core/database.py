"""
Database management for Canopy local storage.

Implements local-first storage with SQLite for messages, keys, trust scores, and metadata.

Author: Konrad Walus (architecture, design, and direction)
Project: Canopy - Local Mesh Communication
License: Apache 2.0
Development: AI-assisted implementation (Claude, Codex, GitHub Copilot, Cursor IDE, Ollama)
"""

import sqlite3
import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any, Tuple, cast
from pathlib import Path
from contextlib import contextmanager

from .config import Config
from .logging_config import log_performance, LogOperation

logger = logging.getLogger('canopy.database')


class DatabaseManager:
    """Manages local SQLite database operations for Canopy."""
    
    def __init__(self, config: Config):
        """Initialize database manager with configuration."""
        logger.info("Initializing DatabaseManager")
        self.config = config
        self.db_path = Path(config.storage.database_path)

        logger.info(f"Database path: {self.db_path.absolute()}")
        db_existed = self.db_path.exists()
        if db_existed:
            try:
                size_bytes = self.db_path.stat().st_size
            except Exception:
                size_bytes = -1
            logger.info(
                "Using existing database file (exists=%s, size_bytes=%s)",
                True,
                size_bytes,
            )
        else:
            logger.info("No existing database file detected; creating new database on first write")
        
        # Create database directory
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        logger.info(f"Created database directory: {self.db_path.parent.absolute()}")

        # Thread-local storage for connection pooling
        self._local = threading.local()
        self._pool_lock = threading.Lock()
        self._pool_stats = {'created': 0, 'reused': 0, 'closed': 0}

        # WAL checkpoint background thread
        self._checkpoint_thread: Optional[threading.Thread] = None
        self._checkpoint_stop = threading.Event()
        self._checkpoint_interval = 300  # 5 minutes

        # Initialize database
        with LogOperation("Database initialization"):
            self._initialize_database()

        # Enable WAL mode and run integrity check on startup
        self._configure_resilience()

        # Start WAL checkpoint background thread
        self._start_checkpoint_thread()

        logger.info("DatabaseManager initialized successfully")
    
    def _initialize_database(self) -> None:
        """Initialize database with required tables.

        Uses a longer busy_timeout (30s) and retries on lock so startup can
        wait out brief lock storms (e.g. Dropbox, stale WAL/shm).
        """
        logger.info("Ensuring database schema (IF NOT EXISTS)...")
        last_error = None
        for attempt in range(3):
            try:
                with self.get_connection(busy_timeout_ms=30_000) as conn:
                    # Create tables
                    conn.executescript("""
                -- Users and identity management
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY,
                    username TEXT UNIQUE NOT NULL,
                    public_key TEXT NOT NULL,
                    password_hash TEXT,  -- NULL for system/legacy users
                    display_name TEXT,
                    agent_directives TEXT,
                    origin_peer TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                
                -- Per-user crypto keypairs (Ed25519 + X25519)
                CREATE TABLE IF NOT EXISTS user_keys (
                    user_id TEXT PRIMARY KEY,
                    ed25519_public_key TEXT NOT NULL,
                    ed25519_private_key TEXT NOT NULL,  -- Encrypted with user's password-derived key
                    x25519_public_key TEXT NOT NULL,
                    x25519_private_key TEXT NOT NULL,   -- Encrypted with user's password-derived key
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
                );
                
                -- API keys for access control
                CREATE TABLE IF NOT EXISTS api_keys (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    key_hash TEXT UNIQUE NOT NULL,
                    permissions TEXT NOT NULL,  -- JSON blob
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    expires_at TIMESTAMP,
                    revoked BOOLEAN DEFAULT FALSE,
                    FOREIGN KEY (user_id) REFERENCES users (id)
                );
                
                -- Messages for local chat
                CREATE TABLE IF NOT EXISTS messages (
                    id TEXT PRIMARY KEY,
                    sender_id TEXT NOT NULL,
                    recipient_id TEXT,  -- NULL for broadcast messages
                    content TEXT NOT NULL,
                    message_type TEXT DEFAULT 'text',  -- text, file, voice, etc.
                    metadata TEXT,  -- JSON blob for additional data
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    edited_at TIMESTAMP,
                    delivered_at TIMESTAMP,
                    read_at TIMESTAMP,
                    FOREIGN KEY (sender_id) REFERENCES users (id)
                );
                
                -- Trust scores for reputation management
                CREATE TABLE IF NOT EXISTS trust_scores (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    peer_id TEXT NOT NULL,
                    score INTEGER DEFAULT 100,
                    last_interaction TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    compliance_events INTEGER DEFAULT 0,
                    violation_events INTEGER DEFAULT 0,
                    notes TEXT,
                    UNIQUE(peer_id)
                );
                
                -- Delete signals for data removal compliance
                CREATE TABLE IF NOT EXISTS delete_signals (
                    id TEXT PRIMARY KEY,
                    target_peer_id TEXT NOT NULL,
                    data_type TEXT NOT NULL,
                    data_id TEXT NOT NULL,
                    reason TEXT,
                    sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    acknowledged_at TIMESTAMP,
                    complied_at TIMESTAMP,
                    violated_at TIMESTAMP,
                    rejected_at TIMESTAMP,
                    status TEXT DEFAULT 'pending'  -- pending, acknowledged, complied, violated, rejected
                );
                
                -- Local network peers discovery
                CREATE TABLE IF NOT EXISTS peers (
                    id TEXT PRIMARY KEY,
                    address TEXT NOT NULL,
                    port INTEGER NOT NULL,
                    last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    status TEXT DEFAULT 'online',  -- online, offline, blocked
                    capabilities TEXT,  -- JSON blob for peer capabilities
                    UNIQUE(address, port)
                );
                
                -- Feed posts and content sharing
                CREATE TABLE IF NOT EXISTS feed_posts (
                    id TEXT PRIMARY KEY,
                    author_id TEXT NOT NULL,
                    content TEXT NOT NULL,
                    content_type TEXT DEFAULT 'text',
                    metadata TEXT,  -- JSON blob
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    expires_at TIMESTAMP,
                    visibility TEXT DEFAULT 'network',  -- public, network, trusted, private, custom
                    likes INTEGER DEFAULT 0,
                    comments INTEGER DEFAULT 0,
                    shares INTEGER DEFAULT 0,
                    FOREIGN KEY (author_id) REFERENCES users (id)
                );
                
                -- Custom permissions for posts
                CREATE TABLE IF NOT EXISTS post_permissions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    post_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (post_id) REFERENCES feed_posts (id) ON DELETE CASCADE,
                    FOREIGN KEY (user_id) REFERENCES users (id),
                    UNIQUE(post_id, user_id)
                );
                
                -- System configuration and state
                CREATE TABLE IF NOT EXISTS system_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                
                -- Instance authentication (web UI password)
                CREATE TABLE IF NOT EXISTS instance_auth (
                    id INTEGER PRIMARY KEY CHECK (id = 1),  -- Only one row allowed
                    password_hash TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                
                -- Per-recipient wrapped content keys for crypto-enforced permissions
                CREATE TABLE IF NOT EXISTS post_content_keys (
                    post_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    wrapped_key TEXT NOT NULL,  -- Hex-encoded wrapped CEK
                    granted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (post_id, user_id),
                    FOREIGN KEY (post_id) REFERENCES feed_posts(id) ON DELETE CASCADE
                );
                
                -- Indexes for performance
                CREATE INDEX IF NOT EXISTS idx_messages_sender ON messages(sender_id);
                CREATE INDEX IF NOT EXISTS idx_messages_recipient ON messages(recipient_id);
                CREATE INDEX IF NOT EXISTS idx_messages_created_at ON messages(created_at);
                CREATE INDEX IF NOT EXISTS idx_trust_scores_peer ON trust_scores(peer_id);
                CREATE INDEX IF NOT EXISTS idx_delete_signals_peer ON delete_signals(target_peer_id);
                CREATE INDEX IF NOT EXISTS idx_feed_posts_author ON feed_posts(author_id);
                CREATE INDEX IF NOT EXISTS idx_feed_posts_created_at ON feed_posts(created_at);
                -- idx_feed_posts_expires_at is created in migration (so existing DBs without expires_at do not fail here)

                -- User feed algorithm preferences
                CREATE TABLE IF NOT EXISTS user_feed_preferences (
                    user_id TEXT PRIMARY KEY,
                    algorithm_json TEXT NOT NULL DEFAULT '{}',
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users (id)
                );

                -- Agent action inbox (mention-triggered items, pull-first)
                CREATE TABLE IF NOT EXISTS agent_inbox (
                    id TEXT PRIMARY KEY,
                    agent_user_id TEXT NOT NULL,
                    source_type TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    message_id TEXT,
                    channel_id TEXT,
                    sender_user_id TEXT,
                    origin_peer TEXT,
                    trigger_type TEXT NOT NULL,
                    payload_json TEXT,
                    status TEXT DEFAULT 'pending',
                    priority TEXT DEFAULT 'normal',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    handled_at TIMESTAMP,
                    expires_at TIMESTAMP,
                    triggered_by_inbox_id TEXT,
                    depth INTEGER DEFAULT 0,
                    FOREIGN KEY (agent_user_id) REFERENCES users (id)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_inbox_unique
                    ON agent_inbox(agent_user_id, source_type, source_id, trigger_type);
                CREATE INDEX IF NOT EXISTS idx_agent_inbox_status
                    ON agent_inbox(agent_user_id, status, created_at);
                CREATE INDEX IF NOT EXISTS idx_agent_inbox_sender
                    ON agent_inbox(sender_user_id);
                CREATE INDEX IF NOT EXISTS idx_agent_inbox_expires
                        ON agent_inbox(expires_at);

                CREATE TABLE IF NOT EXISTS agent_inbox_config (
                    user_id TEXT PRIMARY KEY,
                    config_json TEXT NOT NULL,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users (id)
                );

                -- Best-effort extracted text context for external content (e.g., YouTube)
                CREATE TABLE IF NOT EXISTS content_contexts (
                    id TEXT PRIMARY KEY,
                    source_type TEXT NOT NULL,   -- feed_post, channel_message, direct_message, url
                    source_id TEXT,
                    source_url TEXT NOT NULL,
                    provider TEXT NOT NULL,      -- youtube, web, unknown
                    owner_user_id TEXT NOT NULL,
                    title TEXT,
                    author TEXT,
                    transcript_lang TEXT,
                    transcript_text TEXT,
                    extracted_text TEXT,
                    summary_text TEXT,
                    owner_note TEXT,
                    status TEXT DEFAULT 'ready', -- ready, partial, unavailable, error
                    error TEXT,
                    metadata TEXT,               -- JSON blob
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

                -- Agent presence (0.4.0: last check-in for status badges)
                CREATE TABLE IF NOT EXISTS agent_presence (
                    user_id TEXT PRIMARY KEY,
                    last_checkin_at TIMESTAMP NOT NULL,
                    last_source TEXT,
                    updated_at TIMESTAMP NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_agent_presence_checkin
                    ON agent_presence(last_checkin_at);
                    """)

                    # Insert default system state
                    conn.execute("""
                        INSERT OR IGNORE INTO system_state (key, value) VALUES 
                        ('initialized', ?),
                        ('version', '0.1.0')
                    """, (datetime.now(timezone.utc).isoformat(),))

                    conn.commit()
                    logger.info("Database schema ensured successfully (CREATE TABLE IF NOT EXISTS)")

                    # Run migrations for existing databases
                    # Ensure migration tracking table exists
                    conn.execute("""
                        CREATE TABLE IF NOT EXISTS schema_migrations (
                            version INTEGER PRIMARY KEY,
                            applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                            description TEXT
                        )
                    """)
                    conn.commit()
                    self._run_migrations(conn)
                break
            except sqlite3.OperationalError as e:
                last_error = e
                err_lower = str(e).lower()
                if ("locked" in err_lower or "busy" in err_lower) and attempt < 2:
                    logger.warning(
                        "Database locked/busy (attempt %s/3), retrying in 2s: %s",
                        attempt + 1, e,
                    )
                    time.sleep(2)
                    continue
                logger.error("Failed to initialize database: %s", e, exc_info=True)
                raise
            except Exception as e:
                logger.error("Failed to initialize database: %s", e, exc_info=True)
                raise
    
    def _run_migrations(self, conn: sqlite3.Connection) -> None:
        """Run database migrations for schema updates on existing databases."""
        try:
            # Check if users table has password_hash column
            cursor = conn.execute("PRAGMA table_info(users)")
            columns = [row[1] for row in cursor.fetchall()]
            
            if 'password_hash' not in columns:
                logger.info("Migration: Adding password_hash column to users table")
                conn.execute("ALTER TABLE users ADD COLUMN password_hash TEXT")
            
            if 'display_name' not in columns:
                logger.info("Migration: Adding display_name column to users table")
                conn.execute("ALTER TABLE users ADD COLUMN display_name TEXT")

            if 'account_type' not in columns:
                logger.info("Migration: Adding account_type column to users table")
                conn.execute("ALTER TABLE users ADD COLUMN account_type TEXT DEFAULT 'human'")

            if 'status' not in columns:
                logger.info("Migration: Adding status column to users table")
                conn.execute("ALTER TABLE users ADD COLUMN status TEXT DEFAULT 'active'")

            if 'origin_peer' not in columns:
                logger.info("Migration: Adding origin_peer column to users table")
                conn.execute("ALTER TABLE users ADD COLUMN origin_peer TEXT")

            if 'agent_directives' not in columns:
                logger.info("Migration: Adding agent_directives column to users table")
                conn.execute("ALTER TABLE users ADD COLUMN agent_directives TEXT")

            # Seed role-specific directives for core agent accounts when unset.
            # Keep this lightweight and advisory (not hard policy).
            _seed_directives = {
                'execution_lead': (
                    "Use structured tools by default for coordination. "
                    "Prefer [objective], [request], [handoff], [signal], and [circle] over free-text planning. "
                    "For execution updates, include owner, next action, and due signal. "
                    "Escalate security/privacy risks immediately in #general."
                ),
                'execution_agent': (
                    "Prioritize reliability and implementation rigor. "
                    "When proposing code changes, publish a [request] or [objective] first, then post concise status updates. "
                    "Use mentions only for explicit handoffs or blockers."
                ),
                'coordination_agent': (
                    "Act as integration coordinator across agents. "
                    "Convert ambiguous asks into structured [request] blocks with required_output and due. "
                    "Route decisions through [circle] entries, then publish a final [handoff] with owner and acceptance criteria."
                ),
            }
            for _uname, _directive in _seed_directives.items():
                conn.execute(
                    """
                    UPDATE users
                    SET agent_directives = ?
                    WHERE lower(username) = ?
                      AND (agent_directives IS NULL OR trim(agent_directives) = '')
                    """,
                    (_directive, _uname),
                )

            # --- Messages: add edited_at for message updates ---
            cursor = conn.execute("PRAGMA table_info(messages)")
            msg_columns = [row[1] for row in cursor.fetchall()]
            if 'edited_at' not in msg_columns:
                logger.info("Migration: Adding edited_at column to messages")
                conn.execute("ALTER TABLE messages ADD COLUMN edited_at TIMESTAMP")

            # --- Feed posts: add source classification columns ---
            cursor = conn.execute("PRAGMA table_info(feed_posts)")
            feed_columns = [row[1] for row in cursor.fetchall()]
            
            if 'source_type' not in feed_columns:
                logger.info("Migration: Adding source_type column to feed_posts")
                conn.execute("ALTER TABLE feed_posts ADD COLUMN source_type TEXT DEFAULT 'human'")
            
            if 'source_agent_id' not in feed_columns:
                logger.info("Migration: Adding source_agent_id column to feed_posts")
                conn.execute("ALTER TABLE feed_posts ADD COLUMN source_agent_id TEXT DEFAULT NULL")
            
            if 'source_url' not in feed_columns:
                logger.info("Migration: Adding source_url column to feed_posts")
                conn.execute("ALTER TABLE feed_posts ADD COLUMN source_url TEXT DEFAULT NULL")
            
            if 'tags' not in feed_columns:
                logger.info("Migration: Adding tags column to feed_posts")
                conn.execute("ALTER TABLE feed_posts ADD COLUMN tags TEXT DEFAULT NULL")

            if 'expires_at' not in feed_columns:
                logger.info("Migration: Adding expires_at column to feed_posts")
                conn.execute("ALTER TABLE feed_posts ADD COLUMN expires_at TIMESTAMP")
                # Backfill existing posts with the default quarterly retention (90 days)
                conn.execute("""
                    UPDATE feed_posts
                    SET expires_at = datetime(created_at, '+90 days')
                    WHERE expires_at IS NULL
                """)

            # Ensure expiry index exists (safe to run repeatedly)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_feed_posts_expires_at ON feed_posts(expires_at)")

            # Migration: Add last_activity_at for feed reply resurfacing
            if 'last_activity_at' not in feed_columns:
                logger.info("Migration: Adding last_activity_at column to feed_posts")
                conn.execute("ALTER TABLE feed_posts ADD COLUMN last_activity_at TIMESTAMP")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_feed_posts_last_activity ON feed_posts(last_activity_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_feed_posts_visibility ON feed_posts(visibility)")
            if 'status' in feed_columns:
                conn.execute("CREATE INDEX IF NOT EXISTS idx_feed_posts_status ON feed_posts(status)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_post_permissions_user ON post_permissions(user_id)")
            # channel_messages is created by ChannelManager — only add index if table exists
            cm_exists = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='channel_messages'"
            ).fetchone()
            if cm_exists:
                conn.execute("CREATE INDEX IF NOT EXISTS idx_channel_messages_thread_id ON channel_messages(thread_id)")

            # Migration: Add violated_at column to delete_signals
            cursor = conn.execute("PRAGMA table_info(delete_signals)")
            ds_columns = [row[1] for row in cursor.fetchall()]
            if 'violated_at' not in ds_columns:
                logger.info("Migration: Adding violated_at column to delete_signals")
                conn.execute("ALTER TABLE delete_signals ADD COLUMN violated_at TIMESTAMP")
            if 'rejected_at' not in ds_columns:
                logger.info("Migration: Adding rejected_at column to delete_signals")
                conn.execute("ALTER TABLE delete_signals ADD COLUMN rejected_at TIMESTAMP")

            # Migration: instance_owner_id in system_state (single source of truth for admin)
            cursor = conn.execute("SELECT value FROM system_state WHERE key = 'instance_owner_id'")
            if cursor.fetchone() is None:
                first = conn.execute(
                    "SELECT id FROM users WHERE password_hash IS NOT NULL ORDER BY created_at ASC LIMIT 1"
                ).fetchone()
                if first:
                    conn.execute(
                        "INSERT OR REPLACE INTO system_state (key, value, updated_at) VALUES ('instance_owner_id', ?, CURRENT_TIMESTAMP)",
                        (first['id'],)
                    )
                    logger.info("Migration: Set instance_owner_id to first registered user")

            # Migration: agent_presence table (0.4.0) for presence badges
            ap_exists = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='agent_presence'"
            ).fetchone()
            if not ap_exists:
                logger.info("Migration: Creating agent_presence table")
                conn.executescript("""
                    CREATE TABLE IF NOT EXISTS agent_presence (
                        user_id TEXT PRIMARY KEY,
                        last_checkin_at TIMESTAMP NOT NULL,
                        last_source TEXT,
                        updated_at TIMESTAMP NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_agent_presence_checkin
                        ON agent_presence(last_checkin_at);
                """)

            # Migration: content_contexts table for best-effort extracted text context
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
        except Exception as e:
            logger.critical(
                f"Database migration failed: {e}. "
                "Refusing to start with an inconsistent schema. "
                "Restore from backup or delete the database file to rebuild."
            )
            raise RuntimeError(f"Database migration failed: {e}") from e

    def _configure_resilience(self) -> None:
        """Enable WAL mode, run integrity check, and create startup backup."""
        try:
            with self.get_connection() as conn:
                # Enable WAL mode for better concurrency and crash resilience
                mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()
                logger.info(f"SQLite journal mode: {mode[0] if mode else 'unknown'}")

                # Quick integrity check — 'ok' means no corruption
                result = conn.execute("PRAGMA quick_check").fetchone()
                status = result[0] if result else 'unknown'
                if status == 'ok':
                    logger.info("Database integrity check: OK")
                else:
                    logger.critical(
                        f"DATABASE INTEGRITY CHECK FAILED: {status}. "
                        "Canopy will not start with a corrupted database. "
                        "Restore from a backup in the data/devices/ directory."
                    )
                    raise RuntimeError(f"Database integrity check failed: {status}")
        except Exception as e:
            logger.error(f"Database resilience configuration failed: {e}")

        # Create a startup backup so we can recover from bad migrations
        try:
            self.backup_database(suffix='startup')
        except Exception as e:
            logger.warning(f"Startup backup failed (non-fatal): {e}")

    def _start_checkpoint_thread(self):
        """Start a background thread that periodically checkpoints WAL."""
        if self._checkpoint_thread and self._checkpoint_thread.is_alive():
            return
        self._checkpoint_stop.clear()
        self._checkpoint_thread = threading.Thread(
            target=self._checkpoint_loop,
            name='canopy-wal-checkpoint',
            daemon=True,
        )
        self._checkpoint_thread.start()
        logger.info(f"WAL checkpoint thread started (interval={self._checkpoint_interval}s)")

    def _checkpoint_loop(self):
        """Periodically run WAL checkpoint to prevent unbounded growth."""
        while not self._checkpoint_stop.wait(timeout=self._checkpoint_interval):
            try:
                conn = self._open_connection(busy_timeout_ms=5000)
                try:
                    result = conn.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
                    if result:
                        logger.debug(f"WAL checkpoint: busy={result[0]}, log={result[1]}, checkpointed={result[2]}")
                finally:
                    conn.close()
            except Exception as e:
                logger.warning(f"WAL checkpoint failed (non-fatal): {e}")

    def stop_checkpoint_thread(self):
        """Stop the WAL checkpoint background thread."""
        self._checkpoint_stop.set()
        if self._checkpoint_thread:
            self._checkpoint_thread.join(timeout=5)
            self._checkpoint_thread = None

    def _get_pooled_connection(self, busy_timeout_ms: int = 3000) -> sqlite3.Connection:
        """Get or create a thread-local pooled connection."""
        conn = getattr(self._local, 'conn', None)
        if conn is not None:
            try:
                conn.execute("SELECT 1")
                with self._pool_lock:
                    self._pool_stats['reused'] += 1
                return cast(sqlite3.Connection, conn)
            except Exception:
                try:
                    conn.close()
                except Exception:
                    pass
                self._local.conn = None

        conn = self._open_connection(busy_timeout_ms=busy_timeout_ms)
        self._local.conn = conn
        with self._pool_lock:
            self._pool_stats['created'] += 1
        return conn

    def close_pooled_connection(self):
        """Close this thread's pooled connection (call on thread shutdown)."""
        conn = getattr(self._local, 'conn', None)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
            self._local.conn = None
            with self._pool_lock:
                self._pool_stats['closed'] += 1

    def get_pool_stats(self) -> dict:
        """Return connection pool statistics."""
        with self._pool_lock:
            return dict(self._pool_stats)

    def backup_database(self, suffix: str = 'backup') -> Optional[Path]:
        """Create a backup copy of the database file.

        Returns the path to the backup file, or None on failure.
        The backup uses SQLite's built-in backup API for a
        consistent snapshot even while the DB is in use.
        """
        import shutil
        from datetime import datetime as _dt

        ts = _dt.now().strftime('%Y%m%d_%H%M%S')
        backup_path = self.db_path.parent / f"{self.db_path.stem}_{suffix}_{ts}.db"

        try:
            # For small DBs a simple file copy is fine.
            # SQLite WAL mode flushes before copy so this is safe.
            src = sqlite3.connect(str(self.db_path))
            dst = sqlite3.connect(str(backup_path))
            src.backup(dst)
            dst.close()
            src.close()

            # Keep at most 3 backups with this suffix
            pattern = f"{self.db_path.stem}_{suffix}_*.db"
            backups = sorted(self.db_path.parent.glob(pattern))
            while len(backups) > 3:
                oldest = backups.pop(0)
                try:
                    oldest.unlink()
                    logger.debug(f"Removed old backup: {oldest.name}")
                except Exception:
                    pass

            logger.info(f"Database backed up to {backup_path.name}")
            return backup_path.resolve()
        except Exception as e:
            logger.error(f"Database backup failed: {e}")
            # Clean up partial backup
            if backup_path.exists():
                try:
                    backup_path.unlink()
                except Exception:
                    pass
            return None

    def _open_connection(self, busy_timeout_ms: int = 3000) -> sqlite3.Connection:
        """Open a new SQLite connection with resilience settings.

        Uses WAL journal mode for better concurrency.  *busy_timeout_ms*
        controls how long SQLite waits for a write lock before raising
        ``database is locked``.  Keep this short (default 3 s) so that
        read-heavy paths (AJAX rendering) fail fast and let per-message
        error handlers catch it, rather than accumulating 30 s waits per
        message.
        """
        conn = sqlite3.connect(
            self.db_path,
            timeout=busy_timeout_ms / 1000.0,
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute(f"PRAGMA busy_timeout = {busy_timeout_ms}")
        return conn

    @contextmanager
    def get_connection(self, busy_timeout_ms: Optional[int] = None, use_pool: bool = False) -> Any:
        """Get a database connection with proper error handling.

        Uses WAL journal mode. busy_timeout_ms controls how long SQLite waits
        for a lock (default 3000). Use a longer value (e.g. 30000) for startup.

        If *use_pool* is True, the connection is reused across calls on the
        same thread (thread-local pooling).  Pooled connections are NOT closed
        on context-manager exit — call close_pooled_connection() explicitly
        when the thread is shutting down.
        """
        conn = None
        timeout = busy_timeout_ms if busy_timeout_ms is not None else 3000
        pooled = False
        try:
            if use_pool:
                conn = self._get_pooled_connection(busy_timeout_ms=timeout)
                pooled = True
            else:
                logger.debug(f"Connecting to database: {self.db_path}")
                conn = self._open_connection(busy_timeout_ms=timeout)
                logger.debug("Database connection established successfully")
            yield conn

        except sqlite3.OperationalError as e:
            if conn and not pooled:
                try:
                    conn.rollback()
                except Exception:
                    pass

            if "duplicate column name" in str(e).lower():
                logger.debug(f"Database operational info: {e}")
            else:
                logger.error(f"Database operational error: {e}")
                logger.error(f"Database path exists: {self.db_path.exists()}")
                logger.error(f"Database directory writable: {self.db_path.parent.is_dir() and os.access(self.db_path.parent, os.W_OK)}")
            raise
        except Exception as e:
            if conn and not pooled:
                try:
                    conn.rollback()
                except Exception:
                    pass
            logger.error(f"Database error: {e}", exc_info=True)
            raise
        finally:
            if conn and not pooled:
                try:
                    conn.close()
                except Exception:
                    pass
                logger.debug("Database connection closed")
    
    # User management methods
    def create_user(self, user_id: str, username: str, public_key: str,
                    password_hash: Optional[str] = None, display_name: Optional[str] = None,
                    account_type: str = 'human', status: str = 'active',
                    origin_peer: Optional[str] = None) -> bool:
        """Create a new user in the database."""
        try:
            with self.get_connection() as conn:
                conn.execute("""
                    INSERT INTO users (id, username, public_key, password_hash, display_name, account_type, status, origin_peer)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (user_id, username, public_key, password_hash, display_name or username, account_type, status, origin_peer))
                conn.commit()
                logger.info(f"Created user: {username}")
                return True
        except sqlite3.IntegrityError as e:
            logger.error(f"User creation failed: {e}")
            return False
    
    def get_user(self, user_id: str) -> Optional[Dict[str, Any]]:
        """Get user by ID."""
        with self.get_connection() as conn:
            cursor = conn.execute(
                "SELECT * FROM users WHERE id = ?", (user_id,)
            )
            row = cursor.fetchone()
            return dict(row) if row else None
    
    def get_user_by_username(self, username: str) -> Optional[Dict[str, Any]]:
        """Get user by username (for login)."""
        with self.get_connection() as conn:
            cursor = conn.execute(
                "SELECT * FROM users WHERE username = ?", (username,)
            )
            row = cursor.fetchone()
            return dict(row) if row else None
    
    def get_all_registered_users(self) -> List[Dict[str, Any]]:
        """Get all users that have passwords (registered accounts, not system users)."""
        with self.get_connection() as conn:
            cursor = conn.execute(
                "SELECT id, username, display_name, created_at FROM users WHERE password_hash IS NOT NULL ORDER BY created_at"
            )
            return [dict(row) for row in cursor.fetchall()]

    def get_all_users_for_admin(self) -> List[Dict[str, Any]]:
        """Get all users with account_type and status for admin UI (password_hash IS NOT NULL)."""
        with self.get_connection() as conn:
            cursor = conn.execute(
                """SELECT id, username, display_name, account_type, status, agent_directives, origin_peer, created_at
                   FROM users WHERE password_hash IS NOT NULL
                   ORDER BY created_at"""
            )
            rows = cursor.fetchall()
            return [dict(row) for row in rows]

    def set_user_agent_directives(self, user_id: str, directives: Optional[str]) -> bool:
        """Set custom agent directives for a user (or clear with None)."""
        try:
            with self.get_connection() as conn:
                cur = conn.execute(
                    """
                    UPDATE users
                    SET agent_directives = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (directives, user_id),
                )
                conn.commit()
                return cast(int, cur.rowcount) > 0
        except Exception as e:
            logger.error(f"Failed to set user agent directives: {e}")
            return False

    def get_system_state(self, key: str) -> Optional[str]:
        """Return value for a system_state key, or None."""
        with self.get_connection() as conn:
            cursor = conn.execute("SELECT value FROM system_state WHERE key = ?", (key,))
            row = cursor.fetchone()
            return row['value'] if row else None

    def set_system_state(self, key: str, value: Optional[str]) -> bool:
        """Set (or clear) a system_state key. Use value=None to delete."""
        try:
            with self.get_connection() as conn:
                if value is None or value == '':
                    conn.execute("DELETE FROM system_state WHERE key = ?", (key,))
                else:
                    conn.execute(
                        "INSERT OR REPLACE INTO system_state (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)",
                        (key, value)
                    )
                conn.commit()
                return True
        except Exception as e:
            logger.error(f"set_system_state failed: {e}")
            return False

    def get_instance_owner_user_id(self) -> Optional[str]:
        """Return the user_id of the instance owner (admin).

        Stored in system_state. If missing (or pointing at a likely shadow/remote
        account), self-heal to a plausible local registered account.
        """
        with self.get_connection() as conn:
            cursor = conn.execute("SELECT value FROM system_state WHERE key = 'instance_owner_id'")
            row = cursor.fetchone()
            if row and row['value']:
                owner_id = cast(str, row['value'])
                owner_row = conn.execute(
                    "SELECT id, username, public_key, password_hash, status FROM users WHERE id = ?",
                    (owner_id,),
                ).fetchone()
                if owner_row:
                    username = (owner_row['username'] or '').strip().lower()
                    has_public_key = bool((owner_row['public_key'] or '').strip())
                    has_password = bool((owner_row['password_hash'] or '').strip())
                    is_active = (owner_row['status'] or 'active') == 'active'
                    # A persisted owner should be a real local account, not a
                    # synthetic shadow user (often peer-* with empty public_key).
                    looks_like_shadow = username.startswith('peer-') and not has_public_key
                    if has_password and is_active and not looks_like_shadow:
                        return owner_id

            # Backward compat / recovery: choose first plausible local registered user.
            cursor = conn.execute(
                "SELECT id, username, public_key FROM users "
                "WHERE password_hash IS NOT NULL AND password_hash != '' "
                "AND COALESCE(status, 'active') = 'active' "
                "ORDER BY created_at ASC"
            )
            first = None
            for candidate in cursor.fetchall():
                username = (candidate['username'] or '').strip().lower()
                has_public_key = bool((candidate['public_key'] or '').strip())
                # Prefer non-shadow rows; allow legacy local rows without public keys
                # if username is not synthetic peer-*.
                if username.startswith('peer-') and not has_public_key:
                    continue
                first = candidate
                break

            # Last-resort: if everything looks shadowed, keep old behavior
            # but still exclude users whose username starts with 'peer-' to
            # prevent a shadow/relay account from being elevated to admin.
            if not first:
                first = conn.execute(
                    "SELECT id FROM users WHERE password_hash IS NOT NULL AND password_hash != '' "
                    "AND username NOT LIKE 'peer-%' "
                    "ORDER BY created_at ASC LIMIT 1"
                ).fetchone()

            if first:
                conn.execute(
                    "INSERT OR REPLACE INTO system_state (key, value, updated_at) VALUES ('instance_owner_id', ?, CURRENT_TIMESTAMP)",
                    (first['id'],)
                )
                conn.commit()
                return cast(str, first['id'])
            return None

    def set_instance_owner_user_id(self, user_id: Optional[str]) -> bool:
        """Set the instance owner (admin). Pass None to clear (e.g. for recovery)."""
        return self.set_system_state('instance_owner_id', user_id or None)

    def get_pending_approval_count(self) -> int:
        """Return count of users with status pending_approval (for admin badge)."""
        try:
            with self.get_connection() as conn:
                cursor = conn.execute(
                    "SELECT COUNT(*) AS n FROM users WHERE status = 'pending_approval'"
                )
                row = cursor.fetchone()
                return row['n'] if row else 0
        except Exception:
            return 0

    def set_user_status(self, user_id: str, status: str) -> bool:
        """Set a user's status (active, pending_approval, suspended)."""
        if status not in ('active', 'pending_approval', 'suspended'):
            return False
        try:
            with self.get_connection() as conn:
                cursor = conn.execute(
                    "UPDATE users SET status = ? WHERE id = ?", (status, user_id)
                )
                conn.commit()
                return cast(int, cursor.rowcount) > 0
        except Exception as e:
            logger.error(f"Failed to set user status: {e}")
            return False

    def delete_user(self, user_id: str) -> bool:
        """Delete a user and all data that references them. System/local_user cannot be deleted.

        With PRAGMA foreign_keys = ON, the DB has FKs from api_keys, user_keys, messages,
        feed_posts, post_permissions, agent_inbox, agent_inbox_config, user_feed_preferences.
        We must remove or reassign all dependent rows before deleting the user row.
        """
        if user_id in ('system', 'local_user'):
            return False
        try:
            with self.get_connection() as conn:
                # API and channel membership
                conn.execute("DELETE FROM api_keys WHERE user_id = ?", (user_id,))
                conn.execute("DELETE FROM user_keys WHERE user_id = ?", (user_id,))
                conn.execute("DELETE FROM channel_members WHERE user_id = ?", (user_id,))
                # Feed and posts
                conn.execute("DELETE FROM post_permissions WHERE user_id = ?", (user_id,))
                conn.execute("DELETE FROM post_content_keys WHERE user_id = ?", (user_id,))
                conn.execute("DELETE FROM agent_inbox WHERE agent_user_id = ? OR sender_user_id = ?", (user_id, user_id))
                conn.execute("DELETE FROM agent_inbox_config WHERE user_id = ?", (user_id,))
                conn.execute("DELETE FROM user_feed_preferences WHERE user_id = ?", (user_id,))
                conn.execute("DELETE FROM messages WHERE sender_id = ?", (user_id,))
                conn.execute("DELETE FROM feed_posts WHERE author_id = ?", (user_id,))
                # Best-effort cleanup (no FK in schema but avoid orphan rows)
                conn.execute("DELETE FROM content_contexts WHERE owner_user_id = ?", (user_id,))
                # mention_events: created in mentions.py, same DB
                conn.execute("DELETE FROM mention_events WHERE user_id = ? OR author_id = ?", (user_id, user_id))
                # Channel messages (table in channels.py, same DB): likes then parent refs then messages
                try:
                    msg_ids = [r[0] for r in conn.execute("SELECT id FROM channel_messages WHERE user_id = ?", (user_id,)).fetchall()]
                    if msg_ids:
                        placeholders = ",".join("?" for _ in msg_ids)
                        conn.execute(f"DELETE FROM likes WHERE message_id IN ({placeholders})", msg_ids)
                        conn.execute(
                            f"UPDATE channel_messages SET parent_message_id = NULL WHERE parent_message_id IN ({placeholders})",
                            msg_ids,
                        )
                        conn.execute(f"DELETE FROM channel_messages WHERE user_id = ?", (user_id,))
                except sqlite3.OperationalError as e:
                    if "no such table" not in str(e).lower():
                        raise
                # Finally remove the user (must be last due to FKs)
                conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
                conn.commit()
                return True
        except Exception as e:
            logger.error(f"Failed to delete user: {e}")
            return False
    
    def has_any_registered_users(self) -> bool:
        """Check if any users with passwords exist (for first-time setup detection)."""
        with self.get_connection() as conn:
            cursor = conn.execute(
                "SELECT 1 FROM users WHERE password_hash IS NOT NULL LIMIT 1"
            )
            return cursor.fetchone() is not None
    
    def store_user_keys(self, user_id: str, ed25519_pub: str, ed25519_priv: str,
                        x25519_pub: str, x25519_priv: str) -> bool:
        """Store crypto keypair for a user."""
        try:
            with self.get_connection() as conn:
                conn.execute("""
                    INSERT OR REPLACE INTO user_keys 
                    (user_id, ed25519_public_key, ed25519_private_key, x25519_public_key, x25519_private_key)
                    VALUES (?, ?, ?, ?, ?)
                """, (user_id, ed25519_pub, ed25519_priv, x25519_pub, x25519_priv))
                conn.commit()
                return True
        except Exception as e:
            logger.error(f"Failed to store user keys: {e}")
            return False
    
    def get_user_keys(self, user_id: str) -> Optional[Dict[str, str]]:
        """Get crypto keypair for a user."""
        with self.get_connection() as conn:
            cursor = conn.execute(
                "SELECT * FROM user_keys WHERE user_id = ?", (user_id,)
            )
            row = cursor.fetchone()
            return dict(row) if row else None
    
    # Message management methods
    def store_message(self, message_id: str, sender_id: str, 
                     recipient_id: Optional[str], content: str,
                     message_type: str = 'text', metadata: Optional[Dict] = None) -> bool:
        """Store a message in the database."""
        try:
            with self.get_connection() as conn:
                conn.execute("""
                    INSERT INTO messages (id, sender_id, recipient_id, content, message_type, metadata)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (
                    message_id, sender_id, recipient_id, content, 
                    message_type, json.dumps(metadata) if metadata else None
                ))
                conn.commit()
                return True
        except Exception as e:
            logger.error(f"Failed to store message: {e}")
            return False
    
    def get_messages(self, user_id: str, limit: int = 50) -> List[Dict[str, Any]]:
        """Get recent messages for a user."""
        with self.get_connection() as conn:
            cursor = conn.execute("""
                SELECT m.*, u.username as sender_username 
                FROM messages m
                LEFT JOIN users u ON m.sender_id = u.id
                WHERE m.recipient_id = ? OR m.recipient_id IS NULL
                ORDER BY m.created_at DESC
                LIMIT ?
            """, (user_id, limit))
            
            messages = []
            for row in cursor.fetchall():
                message = dict(row)
                if message['metadata']:
                    message['metadata'] = json.loads(message['metadata'])
                messages.append(message)
            return messages
    
    # Trust management methods
    def update_trust_score(self, peer_id: str, score_delta: int, reason: Optional[str] = None) -> None:
        """Update trust score for a peer."""
        with self.get_connection() as conn:
            conn.execute("""
                INSERT INTO trust_scores (peer_id, score, notes) 
                VALUES (?, 100 + ?, ?)
                ON CONFLICT(peer_id) DO UPDATE SET
                    score = max(0, min(100, score + ?)),
                    last_interaction = CURRENT_TIMESTAMP,
                    notes = ?
            """, (peer_id, score_delta, reason, score_delta, reason))
            conn.commit()
    
    def get_trust_score(self, peer_id: str) -> int:
        """Get current trust score for a peer."""
        with self.get_connection() as conn:
            cursor = conn.execute(
                "SELECT score FROM trust_scores WHERE peer_id = ?", (peer_id,)
            )
            row = cursor.fetchone()
            return row['score'] if row else 100  # Default trust score
    
    def get_all_trust_scores(self) -> Dict[str, int]:
        """Get all trust scores."""
        with self.get_connection() as conn:
            cursor = conn.execute("SELECT peer_id, score FROM trust_scores")
            return {row['peer_id']: row['score'] for row in cursor.fetchall()}
    
    # Delete signals management
    def create_delete_signal(self, signal_id: str, target_peer_id: str, 
                           data_type: str, data_id: str, reason: Optional[str] = None) -> bool:
        """Create a delete signal for compliance tracking."""
        try:
            with self.get_connection() as conn:
                conn.execute("""
                    INSERT INTO delete_signals (id, target_peer_id, data_type, data_id, reason)
                    VALUES (?, ?, ?, ?, ?)
                """, (signal_id, target_peer_id, data_type, data_id, reason))
                conn.commit()
                return True
        except Exception as e:
            logger.error(f"Failed to create delete signal: {e}")
            return False
    
    def update_delete_signal_status(self, signal_id: str, status: str) -> bool:
        """Update delete signal status."""
        VALID_STATUS_COLUMNS = {
            'pending': 'sent_at',
            'acknowledged': 'acknowledged_at',
            'complied': 'complied_at',
            'violated': 'violated_at',
            'rejected': 'rejected_at',
        }
        try:
            with self.get_connection() as conn:
                timestamp_col = VALID_STATUS_COLUMNS.get(status)
                if timestamp_col:
                    conn.execute(f"""
                        UPDATE delete_signals 
                        SET status = ?, {timestamp_col} = CURRENT_TIMESTAMP
                        WHERE id = ?
                    """, (status, signal_id))
                else:
                    conn.execute("""
                        UPDATE delete_signals 
                        SET status = ?
                        WHERE id = ?
                    """, (status, signal_id))
                conn.commit()
                return True
        except Exception as e:
            logger.error(f"Failed to update delete signal: {e}")
            return False
    
    # Utility methods
    def cleanup_old_data(self, days: int = 30) -> None:
        """Clean up old data to maintain performance."""
        from datetime import timedelta
        cutoff_date = datetime.now(timezone.utc) - timedelta(days=days)
        
        with self.get_connection() as conn:
            # Clean up old messages (keep only recent ones)
            conn.execute("""
                DELETE FROM messages 
                WHERE created_at < ? AND message_type != 'important'
            """, (cutoff_date.isoformat(),))
            
            # Clean up resolved delete signals
            conn.execute("""
                DELETE FROM delete_signals 
                WHERE status = 'complied' AND complied_at < ?
            """, (cutoff_date.isoformat(),))
            
            conn.commit()
            logger.info(f"Cleaned up data older than {days} days")
    
    def get_database_stats(self) -> Dict[str, int]:
        """Get database statistics."""
        with self.get_connection() as conn:
            stats = {}
            tables = ['users', 'messages', 'trust_scores', 'delete_signals', 'peers', 'feed_posts']
            
            for table in tables:
                cursor = conn.execute(f"SELECT COUNT(*) as count FROM {table}")
                stats[table] = cursor.fetchone()['count']
            
            return stats
    
    # Instance authentication methods
    def is_instance_password_set(self) -> bool:
        """Check if an instance password has been set."""
        try:
            with self.get_connection() as conn:
                cursor = conn.execute("SELECT 1 FROM instance_auth WHERE id = 1")
                return cursor.fetchone() is not None
        except Exception:
            return False
    
    def set_instance_password(self, password_hash: str) -> bool:
        """Set or update the instance password."""
        try:
            with self.get_connection() as conn:
                conn.execute("""
                    INSERT INTO instance_auth (id, password_hash) VALUES (1, ?)
                    ON CONFLICT(id) DO UPDATE SET 
                        password_hash = excluded.password_hash,
                        updated_at = CURRENT_TIMESTAMP
                """, (password_hash,))
                conn.commit()
                return True
        except Exception as e:
            logger.error(f"Failed to set instance password: {e}")
            return False
    
    def get_instance_password_hash(self) -> Optional[str]:
        """Get the stored instance password hash."""
        try:
            with self.get_connection() as conn:
                cursor = conn.execute("SELECT password_hash FROM instance_auth WHERE id = 1")
                row = cursor.fetchone()
                return row['password_hash'] if row else None
        except Exception as e:
            logger.error(f"Failed to get instance password: {e}")
            return None
    
    # Post content key methods (crypto-enforced permissions)
    def store_post_content_keys(self, post_id: str, wrapped_keys: Dict[str, str]) -> bool:
        """Store wrapped content keys for a post's recipients."""
        try:
            with self.get_connection() as conn:
                for user_id, wrapped_key in wrapped_keys.items():
                    conn.execute("""
                        INSERT OR REPLACE INTO post_content_keys (post_id, user_id, wrapped_key)
                        VALUES (?, ?, ?)
                    """, (post_id, user_id, wrapped_key))
                conn.commit()
                return True
        except Exception as e:
            logger.error(f"Failed to store post content keys: {e}")
            return False
    
    def get_post_content_key(self, post_id: str, user_id: str) -> Optional[str]:
        """Get the wrapped content key for a specific user and post."""
        try:
            with self.get_connection() as conn:
                cursor = conn.execute("""
                    SELECT wrapped_key FROM post_content_keys 
                    WHERE post_id = ? AND user_id = ?
                """, (post_id, user_id))
                row = cursor.fetchone()
                return row['wrapped_key'] if row else None
        except Exception as e:
            logger.error(f"Failed to get post content key: {e}")
            return None
    
    def revoke_post_access(self, post_id: str, user_id: str) -> bool:
        """Revoke a user's access to a post by removing their wrapped key."""
        try:
            with self.get_connection() as conn:
                conn.execute("""
                    DELETE FROM post_content_keys 
                    WHERE post_id = ? AND user_id = ?
                """, (post_id, user_id))
                # Also remove from post_permissions
                conn.execute("""
                    DELETE FROM post_permissions 
                    WHERE post_id = ? AND user_id = ?
                """, (post_id, user_id))
                conn.commit()
                logger.info(f"Revoked access to post {post_id} for user {user_id}")
                return True
        except Exception as e:
            logger.error(f"Failed to revoke post access: {e}")
            return False
    
    def get_post_recipients(self, post_id: str) -> list:
        """Get all users who have content keys for a post."""
        try:
            with self.get_connection() as conn:
                cursor = conn.execute("""
                    SELECT user_id, granted_at FROM post_content_keys 
                    WHERE post_id = ?
                """, (post_id,))
                return [dict(row) for row in cursor.fetchall()]
        except Exception as e:
            logger.error(f"Failed to get post recipients: {e}")
            return []
