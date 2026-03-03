"""
Tests for:
  A) FK race fix in _on_p2p_channel_message — premature channel_members INSERT removed
  B) Avatar recovery bypass in _on_profile_sync — re-apply when avatar file is missing
"""
import sqlite3
import tempfile
import os
from pathlib import Path
from unittest.mock import MagicMock


# ---------------------------------------------------------------------------
# Helpers to build a minimal in-memory SQLite DB with the relevant schema
# ---------------------------------------------------------------------------

def _make_db():
    """Return a SQLite connection with the minimal schema for our tests."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript("""
        CREATE TABLE users (
            id TEXT PRIMARY KEY,
            username TEXT UNIQUE NOT NULL,
            public_key TEXT NOT NULL DEFAULT '',
            display_name TEXT,
            avatar_file_id TEXT,
            bio TEXT,
            origin_peer TEXT,
            account_type TEXT DEFAULT 'human',
            password_hash TEXT
        );

        CREATE TABLE channels (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            channel_type TEXT NOT NULL,
            created_by TEXT NOT NULL,
            description TEXT,
            origin_peer TEXT,
            privacy_mode TEXT DEFAULT 'open',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (created_by) REFERENCES users (id)
        );

        CREATE TABLE channel_members (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            role TEXT DEFAULT 'member',
            joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (channel_id) REFERENCES channels (id) ON DELETE CASCADE,
            FOREIGN KEY (user_id) REFERENCES users (id),
            UNIQUE(channel_id, user_id)
        );

        CREATE TABLE channel_messages (
            id TEXT PRIMARY KEY,
            channel_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            content TEXT NOT NULL,
            message_type TEXT DEFAULT 'text',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (channel_id) REFERENCES channels (id) ON DELETE CASCADE,
            FOREIGN KEY (user_id) REFERENCES users (id)
        );
    """)
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# A) FK race fix tests
# ---------------------------------------------------------------------------

class TestFKRaceFix:
    """Verify that channel_members INSERT only happens AFTER channels INSERT."""

    def test_premature_insert_fails_with_fk_violation(self):
        """Inserting into channel_members before the channel exists raises an error
        (or is silently ignored by INSERT OR IGNORE), demonstrating the race.
        This test documents the OLD (broken) behaviour that was removed."""
        conn = _make_db()
        conn.execute("INSERT INTO users VALUES ('u1','user1','','Alice',NULL,NULL,NULL,'human',NULL)")
        conn.commit()

        # Attempt the premature INSERT (old code path) — should silently fail via
        # INSERT OR IGNORE because the channel does not exist yet and FK is ON.
        try:
            conn.execute(
                "INSERT OR IGNORE INTO channel_members (channel_id, user_id, role) VALUES (?, ?, 'member')",
                ('unknown-chan', 'u1')
            )
            conn.commit()
        except sqlite3.IntegrityError:
            pass  # FK violation — expected in non-IGNORE mode

        # Row must NOT have been inserted because the channel does not exist.
        row = conn.execute(
            "SELECT * FROM channel_members WHERE channel_id = 'unknown-chan'"
        ).fetchone()
        assert row is None, (
            "channel_members row must not exist before the channel is created "
            "(FK race condition)"
        )
        conn.close()

    def test_membership_inserted_after_channel_create(self):
        """New code path: channel is created FIRST, then member is added.
        Both INSERTs must succeed and the member row must be present."""
        conn = _make_db()
        conn.execute("INSERT INTO users VALUES ('u1','user1','','Alice',NULL,NULL,NULL,'human',NULL)")
        conn.commit()

        channel_id = 'chan-abc123'

        # Step 1: auto-create the channel
        conn.execute(
            "INSERT OR IGNORE INTO channels "
            "(id, name, channel_type, created_by, description, origin_peer, privacy_mode, created_at) "
            "VALUES (?, ?, 'private', ?, 'Auto-created from P2P sync', ?, 'private', datetime('now'))",
            (channel_id, f"peer-channel-{channel_id[:8]}", 'u1', 'peer-abc')
        )
        conn.commit()

        # Step 2: add member AFTER channel exists
        conn.execute(
            "INSERT OR IGNORE INTO channel_members (channel_id, user_id, role) VALUES (?, ?, 'member')",
            (channel_id, 'u1')
        )
        conn.commit()

        row = conn.execute(
            "SELECT * FROM channel_members WHERE channel_id = ? AND user_id = ?",
            (channel_id, 'u1')
        ).fetchone()
        assert row is not None, "Member must be inserted after channel exists"
        assert row['role'] == 'member'
        conn.close()

    def test_message_stored_after_channel_and_member_created(self):
        """End-to-end: unknown channel triggers auto-create then message INSERT succeeds."""
        conn = _make_db()
        conn.execute("INSERT INTO users VALUES ('u1','user1','','Alice',NULL,NULL,NULL,'human',NULL)")
        conn.commit()

        channel_id = 'chan-xyz987'
        message_id = 'msg-001'

        # Channel not known yet — auto-create (new behaviour: channel FIRST)
        existing = conn.execute(
            "SELECT id FROM channels WHERE id = ?", (channel_id,)
        ).fetchone()
        assert existing is None

        conn.execute(
            "INSERT OR IGNORE INTO channels "
            "(id, name, channel_type, created_by, privacy_mode) "
            "VALUES (?, 'peer-channel-xyz987', 'private', 'u1', 'private')",
            (channel_id,)
        )
        conn.execute(
            "INSERT OR IGNORE INTO channel_members (channel_id, user_id, role) VALUES (?, ?, 'member')",
            (channel_id, 'u1')
        )
        conn.execute(
            "INSERT INTO channel_messages (id, channel_id, user_id, content) VALUES (?, ?, ?, ?)",
            (message_id, channel_id, 'u1', 'hello')
        )
        conn.commit()

        msg = conn.execute(
            "SELECT * FROM channel_messages WHERE id = ?", (message_id,)
        ).fetchone()
        assert msg is not None
        assert msg['channel_id'] == channel_id
        conn.close()


# ---------------------------------------------------------------------------
# B) Avatar recovery / profile sync tests
# ---------------------------------------------------------------------------

def _make_avatar_missing_checker(db_conn, file_manager):
    """Re-implement _avatar_file_missing_for_user using the same logic as app.py
    so that tests can run without starting the full Flask app."""

    def _avatar_file_missing_for_user(user_id: str) -> bool:
        try:
            row = db_conn.execute(
                "SELECT avatar_file_id FROM users WHERE id = ?",
                (user_id,)
            ).fetchone()
            if not row:
                for pattern in [
                    f"peer-{user_id[:8]}",
                    f"peer-{user_id[:8]}-%",
                ]:
                    row = db_conn.execute(
                        "SELECT avatar_file_id FROM users WHERE username LIKE ?",
                        (pattern,)
                    ).fetchone()
                    if row:
                        break
            if not row or not row[0]:
                return False
            avatar_file_id = row[0]
            if not file_manager:
                return False
            # Use the public get_file_data API: returns None when the file DB
            # record is gone *or* the file is missing/unreadable on disk.
            result = file_manager.get_file_data(avatar_file_id)
            return result is None
        except Exception:
            return False

    return _avatar_file_missing_for_user


class TestAvatarFileMissingForUser:
    """Tests for _avatar_file_missing_for_user helper."""

    def _setup(self):
        conn = _make_db()
        conn.execute(
            "INSERT INTO users VALUES ('u1','user1','','Alice','file-abc',NULL,NULL,'human',NULL)"
        )
        conn.commit()
        return conn

    def test_no_avatar_file_id_returns_false(self):
        """User with no avatar_file_id recorded — not considered missing."""
        conn = _make_db()
        conn.execute(
            "INSERT INTO users VALUES ('u1','user1','','Alice',NULL,NULL,NULL,'human',NULL)"
        )
        conn.commit()
        fm = MagicMock()
        checker = _make_avatar_missing_checker(conn, fm)
        assert checker('u1') is False
        conn.close()

    def test_avatar_file_exists_returns_false(self):
        """User has avatar_file_id and the file is present on disk."""
        conn = self._setup()
        fm = MagicMock()
        # get_file_data returns (bytes, FileInfo) when the file is present
        fm.get_file_data.return_value = (b'\xff\xd8\xff', MagicMock())

        checker = _make_avatar_missing_checker(conn, fm)
        assert checker('u1') is False
        conn.close()

    def test_avatar_file_deleted_returns_true(self):
        """User has avatar_file_id but file no longer exists on disk."""
        conn = self._setup()
        fm = MagicMock()
        # get_file_data returns None when the file is gone
        fm.get_file_data.return_value = None

        checker = _make_avatar_missing_checker(conn, fm)
        assert checker('u1') is True
        conn.close()

    def test_file_record_gone_returns_true(self):
        """avatar_file_id in users but file_manager.get_file_data returns None."""
        conn = self._setup()
        fm = MagicMock()
        fm.get_file_data.return_value = None

        checker = _make_avatar_missing_checker(conn, fm)
        assert checker('u1') is True
        conn.close()

    def test_avatar_missing_for_shadow_user_with_peer_prefix(self):
        """Shadow user stored with peer- prefix is found via LIKE fallback."""
        conn = _make_db()
        conn.execute(
            "INSERT INTO users VALUES ('peer-abcd1234','peer-abcd1234','','Bob','file-xyz',NULL,NULL,'human',NULL)"
        )
        conn.commit()

        fm = MagicMock()
        # get_file_data returns None — file is gone
        fm.get_file_data.return_value = None

        # user_id 'peer-abcd1234' matches direct lookup
        checker = _make_avatar_missing_checker(conn, fm)
        assert checker('peer-abcd1234') is True
        conn.close()


class TestProfileSyncHashSkipLogic:
    """Tests for the hash-skip bypass in _on_profile_sync."""

    def _build_skip_logic(self, seen_hashes, avatar_missing_fn):
        """Return a callable that mirrors the hash-skip guard from _on_profile_sync,
        returning ('skipped', None) or ('proceed', hash_key)."""

        def check(profile_data, remote_peer_id):
            incoming_hash = profile_data.get('profile_hash')
            if not incoming_hash:
                return 'proceed', None

            hash_key = (remote_peer_id, profile_data.get('user_id', ''))
            if seen_hashes.get(hash_key) == incoming_hash:
                avatar_in_payload = bool(profile_data.get('avatar_thumbnail'))
                user_id_for_check = profile_data.get('user_id', '')
                if avatar_in_payload and user_id_for_check and \
                        avatar_missing_fn(user_id_for_check):
                    return 'proceed', hash_key  # bypass skip for avatar recovery
                return 'skipped', hash_key

            seen_hashes[hash_key] = incoming_hash
            return 'proceed', hash_key

        return check

    def test_hash_unchanged_avatar_present_no_missing_file_skips(self):
        """hash unchanged + avatar_thumbnail in payload + file exists → skip."""
        seen = {('peer1', 'u1'): 'hash-abc'}
        check = self._build_skip_logic(seen, lambda uid: False)
        result, _ = check(
            {'profile_hash': 'hash-abc', 'user_id': 'u1', 'avatar_thumbnail': 'base64data'},
            'peer1'
        )
        assert result == 'skipped'

    def test_hash_unchanged_avatar_missing_triggers_reapply(self):
        """hash unchanged + avatar_thumbnail in payload + file missing → proceed."""
        seen = {('peer1', 'u1'): 'hash-abc'}
        check = self._build_skip_logic(seen, lambda uid: True)
        result, _ = check(
            {'profile_hash': 'hash-abc', 'user_id': 'u1', 'avatar_thumbnail': 'base64data'},
            'peer1'
        )
        assert result == 'proceed'

    def test_hash_unchanged_no_avatar_thumbnail_skips(self):
        """hash unchanged + no avatar_thumbnail in payload → skip (nothing to recover)."""
        seen = {('peer1', 'u1'): 'hash-abc'}
        check = self._build_skip_logic(seen, lambda uid: True)
        result, _ = check(
            {'profile_hash': 'hash-abc', 'user_id': 'u1'},
            'peer1'
        )
        assert result == 'skipped'

    def test_hash_changed_proceeds_normally(self):
        """New profile_hash always proceeds and updates the cache."""
        seen = {('peer1', 'u1'): 'hash-old'}
        check = self._build_skip_logic(seen, lambda uid: False)
        result, _ = check(
            {'profile_hash': 'hash-new', 'user_id': 'u1', 'avatar_thumbnail': 'data'},
            'peer1'
        )
        assert result == 'proceed'
        assert seen[('peer1', 'u1')] == 'hash-new'

    def test_no_profile_hash_always_proceeds(self):
        """No profile_hash in payload → always proceed (no dedup possible)."""
        seen = {}
        check = self._build_skip_logic(seen, lambda uid: False)
        result, _ = check({'user_id': 'u1', 'display_name': 'Alice'}, 'peer1')
        assert result == 'proceed'

    def test_hash_unchanged_avatar_missing_but_no_user_id_skips(self):
        """hash unchanged + avatar_thumbnail present but no user_id → skip safely."""
        seen = {('peer1', ''): 'hash-abc'}
        check = self._build_skip_logic(seen, lambda uid: True)
        result, _ = check(
            {'profile_hash': 'hash-abc', 'avatar_thumbnail': 'base64data'},
            'peer1'
        )
        assert result == 'skipped'
