"""Regression tests for universal input and telemetry collaboration cards."""

from __future__ import annotations

import sqlite3
import sys
import types
import unittest

if "zeroconf" not in sys.modules:
    zeroconf_stub = types.ModuleType("zeroconf")

    class _Dummy:
        def __init__(self, *args, **kwargs):
            pass

    zeroconf_stub.ServiceBrowser = _Dummy
    zeroconf_stub.ServiceInfo = _Dummy
    zeroconf_stub.Zeroconf = _Dummy
    zeroconf_stub.ServiceStateChange = _Dummy
    sys.modules["zeroconf"] = zeroconf_stub

from canopy.core.collab_cards import (
    CollabCardManager,
    InputCardSpec,
    TelemetryCardSpec,
    derive_collab_card_id,
    parse_collab_card_blocks,
    strip_collab_card_blocks,
)


class _FakeDbManager:
    def __init__(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(
            """
            CREATE TABLE users (
                id TEXT PRIMARY KEY,
                username TEXT,
                display_name TEXT
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE feed_posts (
                id TEXT PRIMARY KEY,
                author_id TEXT,
                visibility TEXT
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE post_permissions (
                post_id TEXT,
                user_id TEXT
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE channel_messages (
                id TEXT PRIMARY KEY,
                channel_id TEXT
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE channel_members (
                channel_id TEXT,
                user_id TEXT
            )
            """
        )
        for uid, username, display in (
            ("owner", "owner", "Owner"),
            ("alice", "alice", "Alice Agent"),
            ("bob", "bob", "Bob Human"),
        ):
            self.conn.execute(
                "INSERT INTO users (id, username, display_name) VALUES (?, ?, ?)",
                (uid, username, display),
            )
        self.conn.commit()

    def get_connection(self):
        return self.conn

    def get_user(self, user_id: str):
        row = self.conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return dict(row) if row else None


class TestCollabCards(unittest.TestCase):
    def setUp(self) -> None:
        self.db = _FakeDbManager()
        self.manager = CollabCardManager(self.db)

    def tearDown(self) -> None:
        self.db.conn.close()

    def test_parse_and_strip_input_and_telemetry_blocks(self) -> None:
        content = """
Useful lead text.

[input-card]
title: Deployment decision
prompt: Pick the safer path.
kind: choice
options: hold, proceed, escalate
targets: @alice, bob
[/input-card]

[telemetry-card]
title: Build pipeline
status: running
progress: 42%
stage: packaging
metrics:
- tests: 128 passed
- warnings: 1
[/telemetry-card]
"""
        specs = parse_collab_card_blocks(content)

        self.assertEqual(len(specs), 2)
        self.assertIsInstance(specs[0], InputCardSpec)
        self.assertEqual(specs[0].title, "Deployment decision")
        self.assertEqual(specs[0].options, ["hold", "proceed", "escalate"])
        self.assertEqual(specs[0].permissions, ["@alice", "bob"])
        self.assertIsInstance(specs[1], TelemetryCardSpec)
        self.assertEqual(specs[1].progress, 42)
        self.assertEqual(specs[1].metrics[0]["label"], "tests")

        stripped = strip_collab_card_blocks(content)
        self.assertIn("Useful lead text.", stripped)
        self.assertNotIn("[input-card]", stripped)
        self.assertNotIn("[telemetry-card]", stripped)

    def test_fenced_card_examples_do_not_create_live_cards(self) -> None:
        content = """
Teaching example:

```text
[input-card]
title: Example only
prompt: This should stay as text.
[/input-card]
```

Live card:

[telemetry-card]
title: Real run
status: running
progress: 12%
[/telemetry-card]
"""
        specs = parse_collab_card_blocks(content)

        self.assertEqual(len(specs), 1)
        self.assertIsInstance(specs[0], TelemetryCardSpec)
        self.assertEqual(specs[0].title, "Real run")

        stripped = strip_collab_card_blocks(content)
        self.assertIn("[input-card]", stripped)
        self.assertIn("Example only", stripped)
        self.assertNotIn("[telemetry-card]", stripped)

    def test_input_card_permissions_and_response(self) -> None:
        self.db.conn.execute(
            "INSERT INTO channel_messages (id, channel_id) VALUES (?, ?)",
            ("msg-1", "ops-channel"),
        )
        self.db.conn.execute(
            "INSERT INTO channel_members (channel_id, user_id) VALUES (?, ?)",
            ("ops-channel", "alice"),
        )
        self.db.conn.execute(
            "INSERT INTO channel_members (channel_id, user_id) VALUES (?, ?)",
            ("ops-channel", "bob"),
        )
        self.db.conn.commit()

        spec = InputCardSpec(
            title="Approve window",
            prompt="Can we restart the node?",
            kind="approval",
            permissions=["alice"],
        )
        card_id = derive_collab_card_id("channel", "msg-1", "input")
        card = self.manager.upsert_input_card(
            card_id=card_id,
            spec=spec,
            created_by="owner",
            owner_id="owner",
            source_type="channel_message",
            source_id="msg-1",
            channel_id="ops-channel",
            permissions=["alice"],
        )

        self.assertTrue(self.manager.can_respond(card, "alice"))
        self.assertFalse(self.manager.can_respond(card, "bob"))

        updated = self.manager.submit_response(
            card_id,
            responder_id="alice",
            value="approved",
            response_type="approval",
            comment="Proceed after backup.",
        )
        self.assertEqual(updated["response_count"], 1)
        self.assertEqual(updated["my_response"]["value"], "approved")
        self.assertEqual(updated["my_response"]["comment"], "Proceed after backup.")
        self.assertTrue(updated["can_respond"])

    def test_telemetry_runtime_update_is_not_reset_by_render_upsert(self) -> None:
        self.db.conn.execute(
            "INSERT INTO feed_posts (id, author_id, visibility) VALUES (?, ?, ?)",
            ("post-1", "owner", "network"),
        )
        self.db.conn.commit()

        spec = TelemetryCardSpec(
            title="Agent run",
            status="running",
            progress=10,
            stage="queued",
            editors=["alice"],
        )
        card_id = derive_collab_card_id("feed", "post-1", "telemetry")
        self.manager.upsert_telemetry_card(
            card_id=card_id,
            spec=spec,
            created_by="owner",
            owner_id="owner",
            source_type="feed_post",
            source_id="post-1",
            editors=["alice"],
        )

        updated = self.manager.update_telemetry(
            card_id,
            actor_id="alice",
            progress=80,
            stage="final review",
        )
        self.assertEqual(updated["telemetry"]["progress"], 80)
        self.assertEqual(updated["telemetry"]["stage"], "final review")

        # Rendering or re-syncing the original inline block must not rewind live state.
        self.manager.upsert_telemetry_card(
            card_id=card_id,
            spec=spec,
            created_by="owner",
            owner_id="owner",
            source_type="feed_post",
            source_id="post-1",
            editors=["alice"],
            actor_id="owner",
        )
        card = self.manager.get_card(card_id, viewer_id="owner")
        self.assertEqual(card["telemetry"]["progress"], 80)
        self.assertEqual(card["telemetry"]["stage"], "final review")

        snapshot = dict(card)
        snapshot["telemetry"] = {"progress": 95, "stage": "synced"}
        snapshot["status"] = "complete"
        self.manager.ingest_card_snapshot(snapshot)
        synced = self.manager.get_card(card_id, viewer_id="owner")
        self.assertEqual(synced["telemetry"]["progress"], 95)
        self.assertEqual(synced["telemetry"]["stage"], "synced")
        self.assertEqual(synced["status"], "complete")

    def test_changed_card_snapshots_include_responses_for_mesh_catchup(self) -> None:
        self.db.conn.execute(
            "INSERT INTO channel_messages (id, channel_id) VALUES (?, ?)",
            ("msg-catchup", "general"),
        )
        self.db.conn.execute(
            "INSERT INTO channel_members (channel_id, user_id) VALUES (?, ?)",
            ("general", "alice"),
        )
        self.db.conn.commit()

        spec = InputCardSpec(title="Mesh decision", prompt="Approve catchup?")
        card_id = derive_collab_card_id("channel", "msg-catchup", "input")
        self.manager.upsert_input_card(
            card_id=card_id,
            spec=spec,
            created_by="owner",
            owner_id="owner",
            source_type="channel_message",
            source_id="msg-catchup",
            channel_id="general",
            permissions=["alice"],
        )

        self.manager.submit_response(
            card_id,
            responder_id="alice",
            value="approved",
            response_type="approval",
            comment="Looks safe.",
        )

        latest = self.manager.get_cards_latest_timestamp()
        self.assertTrue(latest)

        snapshots = self.manager.get_cards_since("1970-01-01 00:00:00")
        snapshot = next(card for card in snapshots if card["id"] == card_id)
        self.assertEqual(snapshot["visibility"], "network")
        self.assertEqual(snapshot["response_count"], 1)
        self.assertEqual(snapshot["responses"][0]["responder_id"], "alice")
        self.assertEqual(snapshot["responses"][0]["value"], "approved")

    def test_local_cards_are_excluded_from_mesh_catchup_snapshots(self) -> None:
        self.manager.upsert_card(
            card_id="local-card",
            card_type="telemetry",
            title="Private local telemetry",
            created_by="owner",
            owner_id="owner",
            source_type="api",
            source_id="local-card",
            visibility="local",
            telemetry={"progress": 25},
        )
        snapshots = self.manager.get_cards_since("1970-01-01 00:00:00")
        self.assertNotIn("local-card", {card["id"] for card in snapshots})
        self.assertIsNone(self.manager.get_cards_latest_timestamp())

    def test_channel_source_visibility_blocks_direct_card_response_by_non_member(self) -> None:
        self.db.conn.execute(
            "INSERT INTO channel_messages (id, channel_id) VALUES (?, ?)",
            ("msg-private", "secret-channel"),
        )
        self.db.conn.execute(
            "INSERT INTO channel_members (channel_id, user_id) VALUES (?, ?)",
            ("secret-channel", "alice"),
        )
        self.db.conn.commit()

        spec = InputCardSpec(title="Private decision", prompt="Members only?")
        card_id = derive_collab_card_id("channel", "msg-private", "input")
        card = self.manager.upsert_input_card(
            card_id=card_id,
            spec=spec,
            created_by="owner",
            owner_id="owner",
            source_type="channel_message",
            source_id="msg-private",
            channel_id="secret-channel",
        )

        self.assertIsNotNone(card)
        self.assertIsNone(self.manager.get_card(card_id, viewer_id="bob"))
        self.assertIsNotNone(self.manager.get_card(card_id, viewer_id="alice"))
        with self.assertRaises(PermissionError):
            self.manager.submit_response(card_id, responder_id="bob", value="leaked")

    def test_custom_feed_source_visibility_filters_card_reads(self) -> None:
        self.db.conn.execute(
            "INSERT INTO feed_posts (id, author_id, visibility) VALUES (?, ?, ?)",
            ("post-custom", "owner", "custom"),
        )
        self.db.conn.execute(
            "INSERT INTO post_permissions (post_id, user_id) VALUES (?, ?)",
            ("post-custom", "alice"),
        )
        self.db.conn.commit()

        spec = InputCardSpec(title="Custom audience", prompt="Allowed?")
        card_id = derive_collab_card_id("feed", "post-custom", "input")
        self.manager.upsert_input_card(
            card_id=card_id,
            spec=spec,
            created_by="owner",
            owner_id="owner",
            source_type="feed_post",
            source_id="post-custom",
        )

        self.assertIsNone(self.manager.get_card(card_id, viewer_id="bob"))
        self.assertIsNotNone(self.manager.get_card(card_id, viewer_id="alice"))
        cards = self.manager.list_cards(viewer_id="bob")
        self.assertEqual(cards, [])

    def test_collab_card_tables_are_created_on_fresh_database(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE users (id TEXT PRIMARY KEY, username TEXT, display_name TEXT)")
        conn.execute("INSERT INTO users (id, username, display_name) VALUES ('owner', 'owner', 'Owner')")
        conn.commit()

        class _FreshDb:
            def get_connection(self):
                return conn

            def get_user(self, user_id: str):
                row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
                return dict(row) if row else None

        CollabCardManager(_FreshDb())
        tables = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        }
        self.assertIn("collab_cards", tables)
        self.assertIn("collab_card_responses", tables)
        conn.close()

    def test_collab_card_manager_backfills_missing_columns_for_upgrades(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE users (id TEXT PRIMARY KEY, username TEXT, display_name TEXT)")
        conn.execute("CREATE TABLE feed_posts (id TEXT PRIMARY KEY, author_id TEXT, visibility TEXT)")
        conn.execute("CREATE TABLE post_permissions (post_id TEXT, user_id TEXT)")
        conn.execute("CREATE TABLE channel_messages (id TEXT PRIMARY KEY, channel_id TEXT)")
        conn.execute("CREATE TABLE channel_members (channel_id TEXT, user_id TEXT)")
        conn.execute("INSERT INTO users (id, username, display_name) VALUES ('owner', 'owner', 'Owner')")
        conn.execute(
            "CREATE TABLE collab_cards (id TEXT PRIMARY KEY, card_type TEXT NOT NULL, title TEXT NOT NULL)"
        )
        conn.execute(
            "CREATE TABLE collab_card_responses (id TEXT PRIMARY KEY, card_id TEXT NOT NULL, responder_id TEXT NOT NULL)"
        )
        conn.commit()

        class _UpgradeDb:
            def get_connection(self):
                return conn

            def get_user(self, user_id: str):
                row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
                return dict(row) if row else None

        manager = CollabCardManager(_UpgradeDb())

        card_columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(collab_cards)").fetchall()
        }
        self.assertIn("telemetry", card_columns)
        self.assertIn("closed_at", card_columns)
        self.assertIn("expires_at", card_columns)

        response_columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(collab_card_responses)").fetchall()
        }
        self.assertIn("response_type", response_columns)
        self.assertIn("metadata", response_columns)
        self.assertIn("updated_at", response_columns)

        conn.execute("INSERT INTO feed_posts (id, author_id, visibility) VALUES (?, ?, ?)", ("post-upgrade", "owner", "network"))
        conn.commit()
        card = manager.upsert_telemetry_card(
            card_id="telemetry-upgrade",
            spec=TelemetryCardSpec(title="Upgrade check", status="running"),
            created_by="owner",
            owner_id="owner",
            source_type="feed_post",
            source_id="post-upgrade",
        )
        self.assertEqual(card["id"], "telemetry-upgrade")
        conn.close()


if __name__ == "__main__":
    unittest.main()
