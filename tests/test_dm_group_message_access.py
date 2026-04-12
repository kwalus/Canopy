"""Regression tests for group-DM visibility and read semantics."""

import json
import os
import sqlite3
import sys
import types
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

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

from canopy.core.messaging import MessageManager
from canopy.core.messaging import compute_group_id
from canopy.core.messaging import compute_group_thread_id


class _FakeDbManager:
    def __init__(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            """
            CREATE TABLE users (
                id TEXT PRIMARY KEY,
                username TEXT
            );
            CREATE TABLE messages (
                id TEXT PRIMARY KEY,
                sender_id TEXT,
                recipient_id TEXT,
                content TEXT,
                message_type TEXT,
                status TEXT,
                created_at TEXT,
                delivered_at TEXT,
                read_at TEXT,
                edited_at TEXT,
                metadata TEXT
            );
            """
        )
        self.conn.executemany(
            "INSERT INTO users (id, username) VALUES (?, ?)",
            [
                ("agent-local", "agent-local"),
                ("peer-a", "peer-a"),
                ("peer-b", "peer-b"),
                ("other-user", "other-user"),
            ],
        )
        self.conn.executemany(
            """
            INSERT INTO messages (
                id, sender_id, recipient_id, content, message_type, status,
                created_at, delivered_at, read_at, edited_at, metadata
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    "DM-group-visible",
                    "peer-a",
                    "group:abc123",
                    "group update for local agent",
                    "text",
                    "delivered",
                    "2026-03-07T10:00:00+00:00",
                    "2026-03-07T10:00:01+00:00",
                    None,
                    None,
                    json.dumps(
                        {
                            "group_id": "group:abc123",
                            "group_members": ["agent-local", "peer-a", "peer-b"],
                            "reply_to": "DM-root",
                        }
                    ),
                ),
                (
                    "DM-group-hidden",
                    "peer-a",
                    "group:def456",
                    "group update for someone else",
                    "text",
                    "delivered",
                    "2026-03-07T10:01:00+00:00",
                    "2026-03-07T10:01:01+00:00",
                    None,
                    None,
                    json.dumps(
                        {
                            "group_id": "group:def456",
                            "group_members": ["other-user", "peer-a"],
                        }
                    ),
                ),
                (
                    "DM-direct",
                    "peer-b",
                    "agent-local",
                    "direct hello",
                    "text",
                    "delivered",
                    "2026-03-07T10:02:00+00:00",
                    "2026-03-07T10:02:01+00:00",
                    None,
                    None,
                    None,
                ),
                (
                    "DM-group-relayed",
                    "peer-b",
                    "agent-local",
                    "same group delivered through relay alias",
                    "text",
                    "delivered",
                    "2026-03-07T10:03:00+00:00",
                    "2026-03-07T10:03:01+00:00",
                    None,
                    None,
                    json.dumps(
                        {
                            "group_id": "group:relay-alias",
                            "group_members": ["agent-local", "peer-a", "peer-b"],
                        }
                    ),
                ),
                # Message sent to a group recipient but WITHOUT a group_members list.
                # A non-member who knows the group ID must NOT be able to read it.
                (
                    "DM-group-no-members-meta",
                    "peer-a",
                    "group:orphan-group",
                    "message with no group_members in metadata",
                    "text",
                    "delivered",
                    "2026-03-07T10:04:00+00:00",
                    "2026-03-07T10:04:01+00:00",
                    None,
                    None,
                    json.dumps({"group_id": "group:orphan-group"}),
                ),
            ],
        )
        self.conn.commit()

    def get_connection(self):
        return self.conn


class TestDmGroupMessageAccess(unittest.TestCase):
    def setUp(self) -> None:
        self.db = _FakeDbManager()
        self.message_manager = MessageManager(self.db, MagicMock())

    def tearDown(self) -> None:
        self.db.conn.close()

    def test_get_messages_includes_group_dms_for_member(self) -> None:
        messages = self.message_manager.get_messages("agent-local", limit=20)
        ids = [message.id for message in messages]

        self.assertIn("DM-group-visible", ids)
        self.assertIn("DM-direct", ids)
        self.assertNotIn("DM-group-hidden", ids)

    def test_search_messages_includes_group_dms_for_member(self) -> None:
        messages = self.message_manager.search_messages("agent-local", "group update", limit=20)
        ids = [message.id for message in messages]

        self.assertEqual(ids, ["DM-group-visible"])

    def test_mark_message_read_allows_group_members(self) -> None:
        success = self.message_manager.mark_message_read("DM-group-visible", "agent-local")
        self.assertTrue(success)

        read_at = self.db.conn.execute(
            "SELECT read_at FROM messages WHERE id = ?",
            ("DM-group-visible",),
        ).fetchone()["read_at"]
        self.assertIsNotNone(read_at)

    def test_mark_message_read_rejects_non_members(self) -> None:
        success = self.message_manager.mark_message_read("DM-group-hidden", "agent-local")
        self.assertFalse(success)

    def test_get_group_conversation_filters_to_member_groups(self) -> None:
        visible = self.message_manager.get_group_conversation("agent-local", "group:abc123", limit=20)
        hidden = self.message_manager.get_group_conversation("agent-local", "group:def456", limit=20)

        self.assertEqual([message.id for message in visible], ["DM-group-visible", "DM-group-relayed"])
        self.assertEqual(hidden, [])

    def test_get_group_conversation_accepts_canonical_group_identifier(self) -> None:
        canonical_group_id = compute_group_id(["agent-local", "peer-a", "peer-b"])
        visible = self.message_manager.get_group_conversation("agent-local", canonical_group_id, limit=20)

        self.assertEqual([message.id for message in visible], ["DM-group-visible", "DM-group-relayed"])

    def test_explicit_group_thread_does_not_merge_with_same_member_legacy_group(self) -> None:
        base_group_id = compute_group_id(["agent-local", "peer-a", "peer-b"])
        fresh_group_id = compute_group_thread_id(["agent-local", "peer-a", "peer-b"], "fresh-1")
        self.db.conn.execute(
            """
            INSERT INTO messages (
                id, sender_id, recipient_id, content, message_type, status,
                created_at, delivered_at, read_at, edited_at, metadata
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "DM-group-fresh",
                "peer-a",
                fresh_group_id,
                "fresh same-member discussion",
                "text",
                "delivered",
                "2026-03-07T10:05:00+00:00",
                "2026-03-07T10:05:01+00:00",
                None,
                None,
                json.dumps(
                    {
                        "group_id": fresh_group_id,
                        "base_group_id": base_group_id,
                        "group_thread_id": "fresh-1",
                        "group_members": ["agent-local", "peer-a", "peer-b"],
                    }
                ),
            ),
        )
        self.db.conn.commit()

        legacy = self.message_manager.get_group_conversation("agent-local", base_group_id, limit=20)
        fresh = self.message_manager.get_group_conversation("agent-local", fresh_group_id, limit=20)

        self.assertEqual([message.id for message in legacy], ["DM-group-visible", "DM-group-relayed"])
        self.assertEqual([message.id for message in fresh], ["DM-group-fresh"])

    # --- Security regression tests ---

    def test_get_group_conversation_blocks_non_member_from_empty_group_members_message(self) -> None:
        """A non-member must not read a group message that has no group_members list.

        Regression: the SQL WHERE clause uses `recipient_id LIKE 'group:%'` which
        fetches all group-recipient rows regardless of membership.  Without an
        explicit guard on the Python side, a user who knows the group_id can read
        messages from groups they don't belong to.
        """
        # "agent-local" is NOT a member of "group:orphan-group"
        messages = self.message_manager.get_group_conversation(
            "agent-local", "group:orphan-group", limit=20
        )
        self.assertEqual(messages, [], "non-member must not see group messages with empty group_members")

    def test_get_group_conversation_sender_can_read_own_group_message_without_members_list(self) -> None:
        """The original sender of a group message may retrieve it even when
        group_members metadata is absent (e.g. an in-transit/legacy message)."""
        messages = self.message_manager.get_group_conversation(
            "peer-a", "group:orphan-group", limit=20
        )
        ids = [m.id for m in messages]
        self.assertIn("DM-group-no-members-meta", ids, "sender must still see their own group message")


if __name__ == "__main__":
    unittest.main()
