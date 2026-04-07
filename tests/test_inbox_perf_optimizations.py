"""Regression tests for inbox performance optimizations.

Covers:
- _get_account_type caching (no extra DB round-trips on repeated calls)
- _rate_limit_ok consolidated channel COUNT query (burst+hourly in one query)
- New composite indexes exist after _ensure_tables
"""

import sqlite3
import sys
import os
import tempfile
import time
import types
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

# Stub optional dependencies before importing canopy modules
if 'zeroconf' not in sys.modules:
    zeroconf_stub = types.ModuleType('zeroconf')

    class _Dummy:
        def __init__(self, *args, **kwargs):
            pass

    zeroconf_stub.ServiceBrowser = _Dummy
    zeroconf_stub.ServiceInfo = _Dummy
    zeroconf_stub.Zeroconf = _Dummy
    zeroconf_stub.ServiceStateChange = _Dummy
    sys.modules['zeroconf'] = zeroconf_stub

from canopy.core.inbox import InboxManager


# ---------------------------------------------------------------------------
# Minimal fake DB that supports get_connection() and records execute() calls
# ---------------------------------------------------------------------------

class _CountingConnection:
    """Wraps a real sqlite3 connection and counts execute calls."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self.execute_count = 0

    def execute(self, sql: str, params=()) -> sqlite3.Cursor:
        self.execute_count += 1
        return self._conn.execute(sql, params)

    def executescript(self, sql: str):
        return self._conn.executescript(sql)

    def commit(self):
        return self._conn.commit()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


class _FakeDbManager:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._real_conn = conn
        self.counting_conn = _CountingConnection(conn)

    def get_connection(self):
        return self.counting_conn

    def get_user(self, user_id: str):
        row = self._real_conn.execute(
            "SELECT * FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        return dict(row) if row else None

    def get_instance_owner_user_id(self):
        return 'owner-user'


def _build_db() -> tuple:
    """Return (conn, db_manager) with minimal schema."""
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE users (
            id TEXT PRIMARY KEY,
            username TEXT,
            display_name TEXT,
            account_type TEXT
        );
        """
    )
    conn.executemany(
        "INSERT INTO users (id, username, display_name, account_type) VALUES (?, ?, ?, ?)",
        [
            ('human-1', 'human1', 'Human One', 'human'),
            ('agent-1', 'agent1', 'Agent One', 'agent'),
        ],
    )
    conn.commit()
    db = _FakeDbManager(conn)
    return conn, db


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestAccountTypeCache(unittest.TestCase):
    """_get_account_type should cache results and not re-query the DB."""

    def setUp(self):
        self.conn, self.db = _build_db()
        self.mgr = InboxManager(self.db)

    def _db_execute_count_for_account_type(self) -> int:
        """Return number of execute() calls targeting 'account_type' column."""
        # We measure by counting calls to the underlying conn.execute after resetting.
        return self.db.counting_conn.execute_count

    def test_first_call_queries_db(self):
        before = self.db.counting_conn.execute_count
        result = self.mgr._get_account_type('human-1')
        after = self.db.counting_conn.execute_count
        self.assertEqual(result, 'human')
        self.assertGreater(after, before, "Expected at least one DB execute on first call")

    def test_second_call_uses_cache(self):
        self.mgr._get_account_type('human-1')  # prime cache
        before = self.db.counting_conn.execute_count
        result = self.mgr._get_account_type('human-1')
        after = self.db.counting_conn.execute_count
        self.assertEqual(result, 'human')
        self.assertEqual(before, after, "Second call should not hit the DB (cache hit)")

    def test_cache_returns_agent_type(self):
        result = self.mgr._get_account_type('agent-1')
        self.assertEqual(result, 'agent')

    def test_cache_expires_and_re_queries(self):
        """After TTL expiry the next call should hit the DB again."""
        self.mgr._get_account_type('human-1')  # prime cache
        # Manually expire the cache entry by backdating it past the TTL.
        _EXPIRY_MARGIN_SECONDS = 1
        user_type, _ = self.mgr._account_type_cache['human-1']
        expired_time = time.monotonic() - (InboxManager._ACCOUNT_TYPE_CACHE_TTL + _EXPIRY_MARGIN_SECONDS)
        self.mgr._account_type_cache['human-1'] = (user_type, expired_time)

        before = self.db.counting_conn.execute_count
        result = self.mgr._get_account_type('human-1')
        after = self.db.counting_conn.execute_count
        self.assertEqual(result, 'human')
        self.assertGreater(after, before, "Expired cache should trigger a fresh DB query")

    def test_unknown_user_returns_human(self):
        result = self.mgr._get_account_type('no-such-user')
        self.assertEqual(result, 'human')

    def test_empty_user_id_returns_human_without_db(self):
        before = self.db.counting_conn.execute_count
        result = self.mgr._get_account_type('')
        after = self.db.counting_conn.execute_count
        self.assertEqual(result, 'human')
        self.assertEqual(before, after, "Empty user_id should not query DB")


class TestRateLimitConsolidatedQuery(unittest.TestCase):
    """_rate_limit_ok should issue at most 2 DB queries when both channel
    burst and hourly limits are active (previously 3)."""

    def setUp(self):
        self.conn, self.db = _build_db()
        self.mgr = InboxManager(self.db)

    def _count_executes(self, fn) -> int:
        before = self.db.counting_conn.execute_count
        fn()
        return self.db.counting_conn.execute_count - before

    def _make_config(self, *, burst_limit=3, burst_window=60,
                     hourly_limit=20, hourly_window=3600,
                     sender_hourly_limit=10, sender_hourly_window=3600):
        return {
            'channel_burst_limit': burst_limit,
            'channel_burst_window_seconds': burst_window,
            'channel_hourly_limit': hourly_limit,
            'channel_hourly_window_seconds': hourly_window,
            'sender_hourly_limit': sender_hourly_limit,
            'sender_hourly_window_seconds': sender_hourly_window,
        }

    def test_both_channel_limits_use_single_query(self):
        config = self._make_config()
        executes = self._count_executes(
            lambda: self.mgr._rate_limit_ok('human-1', 'chan-1', None, config)
        )
        # One consolidated channel query (no sender).
        self.assertEqual(executes, 1, "Burst+hourly should be a single DB query")

    def test_burst_only_uses_single_query(self):
        config = self._make_config(hourly_limit=0)
        executes = self._count_executes(
            lambda: self.mgr._rate_limit_ok('human-1', 'chan-1', None, config)
        )
        self.assertEqual(executes, 1)

    def test_hourly_only_uses_single_query(self):
        config = self._make_config(burst_limit=0)
        executes = self._count_executes(
            lambda: self.mgr._rate_limit_ok('human-1', 'chan-1', None, config)
        )
        self.assertEqual(executes, 1)

    def test_channel_and_sender_limits_use_two_queries(self):
        config = self._make_config()
        executes = self._count_executes(
            lambda: self.mgr._rate_limit_ok('human-1', 'chan-1', 'agent-1', config)
        )
        # One consolidated channel query + one sender query.
        self.assertEqual(executes, 2)

    def test_no_channel_no_sender_skips_db(self):
        config = self._make_config()
        executes = self._count_executes(
            lambda: self.mgr._rate_limit_ok('human-1', None, None, config)
        )
        self.assertEqual(executes, 0, "No channel/sender means no DB queries needed")

    def test_rate_limit_ok_returns_true_when_below_limits(self):
        config = self._make_config(burst_limit=10, hourly_limit=100)
        result = self.mgr._rate_limit_ok('human-1', 'chan-1', 'agent-1', config)
        self.assertTrue(result)

    def test_rate_limit_enforced_when_burst_exceeded(self):
        """Insert rows exceeding burst limit and verify _rate_limit_ok returns False."""
        # Insert 5 rows all within the last 10 seconds for human-1 / chan-1
        now = datetime.now(timezone.utc)
        for i in range(5):
            created = (now - timedelta(seconds=i)).isoformat()
            self.conn.execute(
                """
                INSERT INTO agent_inbox
                    (id, agent_user_id, source_type, source_id, trigger_type,
                     channel_id, status, created_at)
                VALUES (?, 'human-1', 'channel_message', ?, 'mention', 'chan-1', 'pending', ?)
                """,
                (f"INB{i:04d}", f"src-{i}", created),
            )
        self.conn.commit()

        # burst_window=60 covers all 5 rows (inserted within last 10 s)
        config = self._make_config(burst_limit=3, burst_window=60, hourly_limit=100)
        result = self.mgr._rate_limit_ok('human-1', 'chan-1', None, config)
        self.assertFalse(result, "Should be rate-limited when burst count >= burst_limit")

    def test_rate_limit_not_enforced_when_in_different_channel(self):
        """Rows in a different channel should not affect rate limiting for chan-1."""
        now = datetime.now(timezone.utc)
        for i in range(10):
            created = (now - timedelta(seconds=i)).isoformat()
            self.conn.execute(
                """
                INSERT INTO agent_inbox
                    (id, agent_user_id, source_type, source_id, trigger_type,
                     channel_id, status, created_at)
                VALUES (?, 'human-1', 'channel_message', ?, 'mention', 'chan-OTHER', 'pending', ?)
                """,
                (f"INBO{i:04d}", f"src-o-{i}", created),
            )
        self.conn.commit()

        config = self._make_config(burst_limit=3, burst_window=60, hourly_limit=5)
        result = self.mgr._rate_limit_ok('human-1', 'chan-1', None, config)
        self.assertTrue(result, "Different channel rows should not trigger rate limit for chan-1")


class TestCompositeIndexesExist(unittest.TestCase):
    """_ensure_tables should create the two new composite indexes."""

    def setUp(self):
        self.conn, self.db = _build_db()
        self.mgr = InboxManager(self.db)

    def _index_names(self) -> set:
        rows = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='agent_inbox'"
        ).fetchall()
        return {row[0] for row in rows}

    def test_channel_created_index_exists(self):
        self.assertIn(
            'idx_agent_inbox_channel_created',
            self._index_names(),
            "Missing idx_agent_inbox_channel_created composite index",
        )

    def test_sender_created_index_exists(self):
        self.assertIn(
            'idx_agent_inbox_sender_created',
            self._index_names(),
            "Missing idx_agent_inbox_sender_created composite index",
        )


if __name__ == '__main__':
    unittest.main()
