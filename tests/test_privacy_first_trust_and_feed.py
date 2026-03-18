from __future__ import annotations

import sqlite3
import sys
import tempfile
import types
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

# Allow focused trust/feed tests to import Canopy modules without the
# optional local-network discovery dependency installed.
if 'zeroconf' not in sys.modules:
    zeroconf_stub = types.ModuleType('zeroconf')

    class _DummyServiceBrowser:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def cancel(self) -> None:
            return None

    class _DummyServiceInfo:
        def __init__(self, *args, **kwargs) -> None:
            pass

    class _DummyZeroconf:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def unregister_service(self, *args, **kwargs) -> None:
            return None

        def close(self) -> None:
            return None

    class _DummyServiceStateChange:
        Added = "added"
        Removed = "removed"
        Updated = "updated"

    zeroconf_stub.ServiceBrowser = _DummyServiceBrowser
    zeroconf_stub.ServiceInfo = _DummyServiceInfo
    zeroconf_stub.Zeroconf = _DummyZeroconf
    zeroconf_stub.ServiceStateChange = _DummyServiceStateChange
    sys.modules['zeroconf'] = zeroconf_stub

from canopy.core.feed import FeedManager, PostVisibility
from canopy.core.database import DatabaseManager
from canopy.network.manager import P2PNetworkManager
from canopy.security.trust import TrustManager


class _SqliteDb:
    def __init__(self) -> None:
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.path = self._tmp.name
        self._tmp.close()
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute(
            """
            CREATE TABLE trust_scores (
                peer_id TEXT PRIMARY KEY,
                score INTEGER,
                last_interaction TIMESTAMP,
                compliance_events INTEGER DEFAULT 0,
                violation_events INTEGER DEFAULT 0,
                notes TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE delete_signals (
                id TEXT PRIMARY KEY,
                target_peer_id TEXT,
                data_type TEXT,
                data_id TEXT,
                reason TEXT,
                sent_at TEXT,
                acknowledged_at TEXT,
                complied_at TEXT,
                status TEXT
            )
            """
        )
        conn.commit()
        conn.close()

    @contextmanager
    def get_connection(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()


class PrivacyFirstTrustTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db = _SqliteDb()
        self.trust_manager = TrustManager(self.db)

    def test_unknown_peer_is_pending_and_not_trusted(self) -> None:
        self.assertEqual(self.trust_manager.get_trust_score("peer-unknown"), 0)
        self.assertFalse(self.trust_manager.has_explicit_trust_score("peer-unknown"))
        self.assertFalse(self.trust_manager.is_peer_trusted("peer-unknown"))

    def test_manual_score_creates_explicit_trust_row(self) -> None:
        self.trust_manager.set_trust_score("peer-safe", 85, reason="manual review")
        self.assertTrue(self.trust_manager.has_explicit_trust_score("peer-safe"))
        self.assertEqual(self.trust_manager.get_trust_score("peer-safe"), 85)
        self.assertTrue(self.trust_manager.is_peer_trusted("peer-safe"))

    def test_database_helper_does_not_seed_unknown_peers_as_fully_trusted(self) -> None:
        db_manager = object.__new__(DatabaseManager)
        db_manager.get_connection = self.db.get_connection  # type: ignore[attr-defined]
        db_manager.update_trust_score("peer-review", 5, reason="first contact")
        self.assertEqual(db_manager.get_trust_score("peer-review"), 5)


class FeedTargetingPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manager = object.__new__(P2PNetworkManager)
        self.manager.config = SimpleNamespace(security=SimpleNamespace(trust_threshold=50))
        self.manager.connection_manager = SimpleNamespace(
            get_connected_peers=lambda: ["peer-safe", "peer-review", "peer-low"]
        )
        trust_map = {
            "peer-safe": 90,
            "peer-review": 50,
            "peer-low": 10,
        }
        self.manager.get_trust_score = lambda peer_id: trust_map.get(peer_id, 0)

    def test_target_peers_for_public_and_network_include_all_connected(self) -> None:
        self.assertEqual(
            self.manager._get_feed_post_target_peers("public"),
            ["peer-safe", "peer-review", "peer-low"],
        )
        self.assertEqual(
            self.manager._get_feed_post_target_peers("network"),
            ["peer-safe", "peer-review", "peer-low"],
        )

    def test_target_peers_for_trusted_only_include_explicitly_trusted_peers(self) -> None:
        self.assertEqual(
            self.manager._get_feed_post_target_peers("trusted"),
            ["peer-safe", "peer-review"],
        )

    def test_target_peers_for_private_and_custom_are_local_only(self) -> None:
        self.assertEqual(self.manager._get_feed_post_target_peers("private"), [])
        self.assertEqual(self.manager._get_feed_post_target_peers("custom"), [])

    def test_visibility_narrowing_revokes_peers_outside_new_scope(self) -> None:
        targets, revoke = self.manager._get_feed_post_target_delta("public", "trusted")
        self.assertEqual(targets, ["peer-safe", "peer-review"])
        self.assertEqual(revoke, ["peer-low"])

        targets, revoke = self.manager._get_feed_post_target_delta("trusted", "private")
        self.assertEqual(targets, [])
        self.assertEqual(revoke, ["peer-safe", "peer-review"])


ROOT = Path(__file__).resolve().parents[1]


class PrivacyFirstComposerRegressionTests(unittest.TestCase):
    def test_feed_manager_default_visibility_is_private(self) -> None:
        source = (ROOT / 'canopy' / 'core' / 'feed.py').read_text(encoding='utf-8')
        self.assertIn("visibility: PostVisibility = PostVisibility.PRIVATE", source)

    def test_feed_template_defaults_to_private_with_explicit_hint(self) -> None:
        source = (ROOT / 'canopy' / 'ui' / 'templates' / 'feed.html').read_text(encoding='utf-8')
        self.assertIn('<option value="private" selected>Only Me</option>', source)
        self.assertIn('Default is private. `Trusted` only reaches peers you have explicitly reviewed.', source)

    def test_route_defaults_use_private_visibility(self) -> None:
        ui_routes = (ROOT / 'canopy' / 'ui' / 'routes.py').read_text(encoding='utf-8')
        api_routes = (ROOT / 'canopy' / 'api' / 'routes.py').read_text(encoding='utf-8')
        mcp_server = (ROOT / 'canopy' / 'mcp' / 'server.py').read_text(encoding='utf-8')
        self.assertIn("data.get('visibility', 'private')", ui_routes)
        self.assertIn("data.get('visibility', 'private')", api_routes)
        self.assertIn('(args.get("visibility") or "private").lower()', mcp_server)

    def test_feed_queries_include_trusted_visibility(self) -> None:
        feed_source = (ROOT / 'canopy' / 'core' / 'feed.py').read_text(encoding='utf-8')
        self.assertIn("p.visibility = 'trusted' OR", feed_source)

    def test_feed_updates_include_previous_visibility_for_revoke_logic(self) -> None:
        ui_routes = (ROOT / 'canopy' / 'ui' / 'routes.py').read_text(encoding='utf-8')
        api_routes = (ROOT / 'canopy' / 'api' / 'routes.py').read_text(encoding='utf-8')
        mcp_server = (ROOT / 'canopy' / 'mcp' / 'server.py').read_text(encoding='utf-8')
        self.assertIn("previous_visibility=existing_post.visibility.value", ui_routes)
        self.assertIn("previous_visibility=post.visibility.value", api_routes)
        self.assertIn("previous_visibility=previous_post.visibility.value", mcp_server)


if __name__ == "__main__":
    unittest.main()
