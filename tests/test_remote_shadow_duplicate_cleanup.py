"""Regression tests for duplicate remote-shadow repair helpers."""

import os
import sqlite3
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from canopy.core.database import DatabaseManager


class _RemoteShadowCleanupHarness(DatabaseManager):
    """Reuse DatabaseManager duplicate repair logic with an in-memory DB."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def get_connection(self, busy_timeout_ms: int = 5000):
        return self._conn


class TestRemoteShadowDuplicateCleanup(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        self.conn.execute('PRAGMA foreign_keys = ON')
        self.conn.executescript(
            """
            CREATE TABLE users (
                id TEXT PRIMARY KEY,
                username TEXT UNIQUE NOT NULL,
                public_key TEXT NOT NULL,
                password_hash TEXT,
                display_name TEXT,
                agent_directives TEXT,
                origin_peer TEXT,
                avatar_file_id TEXT,
                profile_updated_at TEXT,
                created_at TEXT,
                updated_at TEXT,
                account_type TEXT,
                status TEXT
            );
            CREATE TABLE system_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE trust_scores (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                peer_id TEXT NOT NULL,
                score INTEGER DEFAULT 0,
                last_interaction TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                compliance_events INTEGER DEFAULT 0,
                violation_events INTEGER DEFAULT 0,
                notes TEXT,
                manually_penalized BOOLEAN NOT NULL DEFAULT 0,
                UNIQUE(peer_id)
            );
            CREATE TABLE delete_signals (
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
                status TEXT DEFAULT 'pending'
            );
            CREATE TABLE channels (
                id TEXT PRIMARY KEY,
                created_by TEXT NOT NULL,
                FOREIGN KEY (created_by) REFERENCES users(id)
            );
            CREATE TABLE api_keys (id TEXT PRIMARY KEY, user_id TEXT);
            CREATE TABLE user_keys (user_id TEXT PRIMARY KEY);
            CREATE TABLE channel_members (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                joined_at TEXT,
                role TEXT DEFAULT 'member',
                notifications_enabled BOOLEAN DEFAULT TRUE,
                last_read_at TEXT,
                UNIQUE(channel_id, user_id)
            );
            CREATE TABLE channel_messages (
                id TEXT PRIMARY KEY,
                channel_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                content TEXT NOT NULL
            );
            CREATE TABLE files (
                id TEXT PRIMARY KEY,
                uploaded_by TEXT NOT NULL
            );
            CREATE TABLE peer_device_profiles (
                peer_id TEXT PRIMARY KEY,
                display_name TEXT,
                description TEXT,
                avatar_b64 TEXT,
                avatar_mime TEXT,
                updated_at TEXT
            );
            CREATE TABLE remote_attachment_transfers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                origin_peer_id TEXT NOT NULL,
                origin_file_id TEXT NOT NULL,
                local_file_id TEXT,
                status TEXT
            );
            CREATE TABLE messages (
                id TEXT PRIMARY KEY,
                sender_id TEXT NOT NULL,
                recipient_id TEXT
            );
            CREATE TABLE feed_posts (
                id TEXT PRIMARY KEY,
                author_id TEXT NOT NULL
            );
            CREATE TABLE post_content_keys (post_id TEXT NOT NULL, user_id TEXT NOT NULL, PRIMARY KEY (post_id, user_id));
            CREATE TABLE agent_inbox (id TEXT PRIMARY KEY, agent_user_id TEXT, sender_user_id TEXT);
            CREATE TABLE agent_inbox_config (user_id TEXT PRIMARY KEY);
            CREATE TABLE user_feed_preferences (user_id TEXT PRIMARY KEY);
            CREATE TABLE content_contexts (id TEXT PRIMARY KEY, owner_user_id TEXT);
            CREATE TABLE mention_events (id TEXT PRIMARY KEY, user_id TEXT, author_id TEXT);
            CREATE TABLE likes (
                id TEXT PRIMARY KEY,
                message_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                UNIQUE(message_id, user_id)
            );
            CREATE TABLE post_permissions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                post_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                UNIQUE(post_id, user_id)
            );
            """
        )
        self.db = _RemoteShadowCleanupHarness(self.conn)

    def tearDown(self) -> None:
        self.conn.close()

    def _insert_user(
        self,
        user_id: str,
        username: str,
        display_name: str,
        *,
        origin_peer: str = 'peer-123',
        profile_updated_at: str = '',
        created_at: str = '2026-03-29 12:00:00',
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO users (
                id, username, public_key, password_hash, display_name, agent_directives,
                origin_peer, avatar_file_id, profile_updated_at, created_at, updated_at,
                account_type, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                username,
                '',
                None,
                display_name,
                None,
                origin_peer,
                None,
                profile_updated_at,
                created_at,
                created_at,
                'human',
                'active',
            ),
        )

    def test_list_duplicate_groups_suggests_freshest_remote_shadow_row(self) -> None:
        self._insert_user(
            'windy-stale',
            'peer-windy',
            'Windy',
            profile_updated_at='2026-03-29T12:00:00+00:00',
            created_at='2026-03-29 12:00:00',
        )
        self._insert_user(
            'windy-current',
            'Windy.123abc',
            'Windy',
            profile_updated_at='2026-03-29T13:00:00+00:00',
            created_at='2026-03-29 12:05:00',
        )
        self.conn.commit()

        groups = self.db.list_remote_shadow_duplicate_groups()

        self.assertEqual(len(groups), 1)
        group = groups[0]
        self.assertEqual(group['display_name'], 'Windy')
        self.assertEqual(group['canonical_user_id'], 'windy-current')
        self.assertEqual(group['duplicate_count'], 1)

    def test_repair_remote_shadow_duplicate_group_repoints_rows_and_preserves_admin_membership(self) -> None:
        self._insert_user(
            'maddog-stale',
            'peer-maddog',
            'Maddog',
            profile_updated_at='2026-03-29T12:00:00+00:00',
            created_at='2026-03-29 12:00:00',
        )
        self._insert_user(
            'maddog-current',
            'Maddog.826sN3',
            'Maddog',
            profile_updated_at='2026-03-29T13:00:00+00:00',
            created_at='2026-03-29 12:10:00',
        )
        self.conn.execute("INSERT INTO channels (id, created_by) VALUES (?, ?)", ('chan-owned', 'maddog-stale'))
        self.conn.executemany(
            """
            INSERT INTO channel_members (channel_id, user_id, joined_at, role, notifications_enabled, last_read_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                ('chan-shared', 'maddog-stale', '2026-03-29 12:01:00', 'admin', 1, '2026-03-29 13:10:00'),
                ('chan-shared', 'maddog-current', '2026-03-29 12:02:00', 'member', 0, '2026-03-29 13:00:00'),
                ('chan-source-only', 'maddog-stale', '2026-03-29 12:03:00', 'member', 1, '2026-03-29 13:05:00'),
            ],
        )
        self.conn.execute(
            "INSERT INTO channel_messages (id, channel_id, user_id, content) VALUES (?, ?, ?, ?)",
            ('msg-1', 'chan-shared', 'maddog-stale', 'hello'),
        )
        self.conn.execute(
            "INSERT INTO files (id, uploaded_by) VALUES (?, ?)",
            ('file-1', 'maddog-stale'),
        )
        self.conn.execute(
            "INSERT INTO messages (id, sender_id, recipient_id) VALUES (?, ?, ?)",
            ('dm-1', 'maddog-stale', 'maddog-stale'),
        )
        self.conn.execute(
            "INSERT INTO feed_posts (id, author_id) VALUES (?, ?)",
            ('post-1', 'maddog-stale'),
        )
        self.conn.executemany(
            "INSERT INTO likes (id, message_id, user_id) VALUES (?, ?, ?)",
            [
                ('like-source', 'msg-1', 'maddog-stale'),
                ('like-canonical', 'msg-1', 'maddog-current'),
            ],
        )
        self.conn.executemany(
            "INSERT INTO post_permissions (post_id, user_id) VALUES (?, ?)",
            [
                ('post-1', 'maddog-stale'),
                ('post-1', 'maddog-current'),
            ],
        )
        self.conn.commit()

        result = self.db.repair_remote_shadow_duplicate_group('maddog-stale')

        self.assertTrue(result['success'])
        self.assertEqual(result['canonical_user_id'], 'maddog-current')
        self.assertEqual(result['merged_count'], 1)
        stale_row = self.conn.execute("SELECT 1 FROM users WHERE id = ?", ('maddog-stale',)).fetchone()
        self.assertIsNone(stale_row)

        created_by = self.conn.execute("SELECT created_by FROM channels WHERE id = ?", ('chan-owned',)).fetchone()
        assert created_by is not None
        self.assertEqual(created_by['created_by'], 'maddog-current')

        message_row = self.conn.execute("SELECT user_id FROM channel_messages WHERE id = ?", ('msg-1',)).fetchone()
        assert message_row is not None
        self.assertEqual(message_row['user_id'], 'maddog-current')

        file_row = self.conn.execute("SELECT uploaded_by FROM files WHERE id = ?", ('file-1',)).fetchone()
        assert file_row is not None
        self.assertEqual(file_row['uploaded_by'], 'maddog-current')

        dm_row = self.conn.execute("SELECT sender_id, recipient_id FROM messages WHERE id = ?", ('dm-1',)).fetchone()
        assert dm_row is not None
        self.assertEqual(dm_row['sender_id'], 'maddog-current')
        self.assertEqual(dm_row['recipient_id'], 'maddog-current')

        post_row = self.conn.execute("SELECT author_id FROM feed_posts WHERE id = ?", ('post-1',)).fetchone()
        assert post_row is not None
        self.assertEqual(post_row['author_id'], 'maddog-current')

        shared_member = self.conn.execute(
            "SELECT role, notifications_enabled, last_read_at FROM channel_members WHERE channel_id = ? AND user_id = ?",
            ('chan-shared', 'maddog-current'),
        ).fetchone()
        assert shared_member is not None
        self.assertEqual(shared_member['role'], 'admin')
        self.assertEqual(int(shared_member['notifications_enabled']), 1)
        self.assertEqual(shared_member['last_read_at'], '2026-03-29 13:10:00')

        source_only_member = self.conn.execute(
            "SELECT 1 FROM channel_members WHERE channel_id = ? AND user_id = ?",
            ('chan-source-only', 'maddog-current'),
        ).fetchone()
        self.assertIsNotNone(source_only_member)

        like_rows = self.conn.execute(
            "SELECT COUNT(*) AS c FROM likes WHERE message_id = ? AND user_id = ?",
            ('msg-1', 'maddog-current'),
        ).fetchone()
        assert like_rows is not None
        self.assertEqual(like_rows['c'], 1)

        permission_rows = self.conn.execute(
            "SELECT COUNT(*) AS c FROM post_permissions WHERE post_id = ? AND user_id = ?",
            ('post-1', 'maddog-current'),
        ).fetchone()
        assert permission_rows is not None
        self.assertEqual(permission_rows['c'], 1)

    def test_list_cross_peer_same_name_groups_surfaces_forgettable_remote_rows(self) -> None:
        self.conn.execute(
            """
            INSERT INTO users (
                id, username, public_key, password_hash, display_name, agent_directives,
                origin_peer, avatar_file_id, profile_updated_at, created_at, updated_at,
                account_type, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                'local-maddogm1',
                'MaddogM1',
                'pk-local',
                'pw-local',
                'MaddogM1',
                None,
                '',
                None,
                '',
                '2026-03-29 12:00:00',
                '2026-03-29 12:00:00',
                'human',
                'active',
            ),
        )
        self._insert_user(
            'remote-maddogm1',
            'MaddogM1.8uWrAM',
            'MaddogM1',
            origin_peer='peer-8uWrAM',
            created_at='2026-03-29 12:10:00',
        )
        self.conn.execute(
            "INSERT INTO channel_messages (id, channel_id, user_id, content) VALUES (?, ?, ?, ?)",
            ('msg-cross-peer', 'chan-cross-peer', 'remote-maddogm1', 'remote history'),
        )
        self.conn.commit()

        groups = self.db.list_cross_peer_same_name_groups()

        self.assertEqual(len(groups), 1)
        group = groups[0]
        self.assertEqual(group['display_name'], 'MaddogM1')
        self.assertEqual(group['peer_count'], 2)
        self.assertEqual(group['forgettable_count'], 1)
        forgettable = [row for row in group['users'] if row.get('forgettable')]
        self.assertEqual(len(forgettable), 1)
        self.assertEqual(forgettable[0]['id'], 'remote-maddogm1')

    def test_forget_remote_shadow_user_deletes_selected_remote_row_only(self) -> None:
        self.conn.execute(
            """
            INSERT INTO users (
                id, username, public_key, password_hash, display_name, agent_directives,
                origin_peer, avatar_file_id, profile_updated_at, created_at, updated_at,
                account_type, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                'local-homie',
                'Homie_O_Stasis',
                'pk-local',
                'pw-local',
                'Homie_O_Stasis',
                None,
                '',
                None,
                '',
                '2026-03-29 11:55:00',
                '2026-03-29 11:55:00',
                'human',
                'active',
            ),
        )
        self._insert_user(
            'remote-homie-old',
            'Homie_O_Stasis.old',
            'Homie_O_Stasis',
            origin_peer='peer-old',
            created_at='2026-03-29 12:05:00',
        )
        self.conn.execute("INSERT INTO files (id, uploaded_by) VALUES (?, ?)", ('file-old', 'remote-homie-old'))
        self.conn.commit()

        result = self.db.forget_remote_shadow_user('remote-homie-old')

        self.assertTrue(result['success'])
        self.assertEqual(result['deleted_user_id'], 'remote-homie-old')
        remote_row = self.conn.execute("SELECT 1 FROM users WHERE id = ?", ('remote-homie-old',)).fetchone()
        local_row = self.conn.execute("SELECT 1 FROM users WHERE id = ?", ('local-homie',)).fetchone()
        file_row = self.conn.execute("SELECT uploaded_by FROM files WHERE id = ?", ('file-old',)).fetchone()
        self.assertIsNone(remote_row)
        self.assertIsNotNone(local_row)
        self.assertIsNotNone(file_row)
        assert file_row is not None
        self.assertEqual(file_row['uploaded_by'], 'system')

    def test_forget_peer_residue_removes_trust_profile_and_shadow_users(self) -> None:
        self.conn.execute(
            """
            INSERT INTO users (
                id, username, public_key, password_hash, display_name, agent_directives,
                origin_peer, avatar_file_id, profile_updated_at, created_at, updated_at,
                account_type, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                'local-owner',
                'LocalOwner',
                'pk-local',
                'pw-local',
                'LocalOwner',
                None,
                '',
                None,
                '',
                '2026-03-29 11:50:00',
                '2026-03-29 11:50:00',
                'human',
                'active',
            ),
        )
        self._insert_user(
            'peer-gone-user',
            'PeerGone.shadow',
            'PeerGone',
            origin_peer='peer-gone',
            created_at='2026-03-29 12:00:00',
        )
        self.conn.execute("INSERT INTO trust_scores (peer_id, score, notes) VALUES (?, ?, ?)", ('peer-gone', 40, 'old peer'))
        self.conn.execute(
            "INSERT INTO delete_signals (id, target_peer_id, data_type, data_id, reason, status) VALUES (?, ?, ?, ?, ?, ?)",
            ('sig-1', 'peer-gone', 'file', 'file-1', 'cleanup', 'pending'),
        )
        self.conn.execute(
            "INSERT INTO peer_device_profiles (peer_id, display_name, description, updated_at) VALUES (?, ?, ?, ?)",
            ('peer-gone', 'Peer Gone', 'stale profile', '2026-03-29 12:05:00'),
        )
        self.conn.execute(
            "INSERT INTO remote_attachment_transfers (origin_peer_id, origin_file_id, local_file_id, status) VALUES (?, ?, ?, ?)",
            ('peer-gone', 'origin-file-1', 'local-file-1', 'complete'),
        )
        self.conn.execute("INSERT INTO files (id, uploaded_by) VALUES (?, ?)", ('file-peer-gone', 'peer-gone-user'))
        self.conn.commit()

        result = self.db.forget_peer_residue('peer-gone')

        self.assertTrue(result['success'])
        self.assertEqual(result['cleanup']['trust_scores_deleted'], 1)
        self.assertEqual(result['cleanup']['delete_signals_deleted'], 1)
        self.assertEqual(result['cleanup']['peer_profiles_deleted'], 1)
        self.assertEqual(result['cleanup']['remote_attachment_transfers_deleted'], 1)
        self.assertEqual(result['cleanup']['deleted_shadow_users'], 1)
        user_row = self.conn.execute("SELECT 1 FROM users WHERE id = ?", ('peer-gone-user',)).fetchone()
        trust_row = self.conn.execute("SELECT 1 FROM trust_scores WHERE peer_id = ?", ('peer-gone',)).fetchone()
        signal_row = self.conn.execute("SELECT 1 FROM delete_signals WHERE target_peer_id = ?", ('peer-gone',)).fetchone()
        profile_row = self.conn.execute("SELECT 1 FROM peer_device_profiles WHERE peer_id = ?", ('peer-gone',)).fetchone()
        transfer_row = self.conn.execute("SELECT 1 FROM remote_attachment_transfers WHERE origin_peer_id = ?", ('peer-gone',)).fetchone()
        self.assertIsNone(user_row)
        self.assertIsNone(trust_row)
        self.assertIsNone(signal_row)
        self.assertIsNone(profile_row)
        self.assertIsNone(transfer_row)


if __name__ == '__main__':
    unittest.main()
