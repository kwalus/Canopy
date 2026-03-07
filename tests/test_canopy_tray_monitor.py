"""Tests for canopy_tray.monitor compatibility and notification behavior."""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from canopy_tray.monitor import StatusMonitor


class _StubMonitor(StatusMonitor):
    def __init__(self, responses):
        super().__init__(api_base="http://localhost:7770/api/v1", api_key="tray-key")
        self._responses = responses

    def _api_get(self, path: str, timeout: int = 5):
        value = self._responses[path]
        if isinstance(value, list):
            if len(value) > 1:
                return value.pop(0)
            return value[0]
        if isinstance(value, Exception):
            raise value
        return value


class TestCanopyTrayMonitor(unittest.TestCase):
    def test_poll_peers_uses_current_p2p_endpoint(self):
        monitor = _StubMonitor(
            {
                "/p2p/peers": {
                    "connected_peers": ["peer-a"],
                    "discovered_peers": [
                        {"peer_id": "peer-a", "display_name": "Alpha", "connected": True},
                        {"peer_id": "peer-b", "display_name": "Beta", "connected": False},
                    ],
                }
            }
        )

        updates = []
        monitor.on_status_update = lambda connected, total: updates.append((connected, total))
        monitor._poll_peers()

        self.assertEqual(monitor.connected_count, 1)
        self.assertEqual([(peer.peer_id, peer.status) for peer in monitor.peers], [
            ("peer-a", "connected"),
            ("peer-b", "disconnected"),
        ])
        self.assertEqual(updates, [(1, 2)])

    def test_poll_peers_falls_back_to_known_peers(self):
        monitor = _StubMonitor(
            {
                "/p2p/peers": Exception("route unavailable"),
                "/p2p/known_peers": {
                    "known_peers": [
                        {"peer_id": "peer-z", "display_name": "Zed", "connected": True},
                    ]
                },
            }
        )

        monitor._poll_peers()

        self.assertEqual(monitor.connected_count, 1)
        self.assertEqual(monitor.peers[0].peer_id, "peer-z")

    def test_first_message_poll_seeds_without_notification_and_second_poll_emits_in_order(self):
        monitor = _StubMonitor(
            {
                "/auth/status": {"user_id": "owner"},
                "/channels": [
                    {"channels": [{"id": "general", "name": "general"}]},
                    {"channels": [{"id": "general", "name": "general"}]},
                ],
                "/channels/general/messages?limit=10": [
                    {
                        "messages": [
                            {"id": "m2", "user_id": "peer-a", "display_name": "Alpha", "content": "two", "created_at": "2026-03-06T10:00:02Z"},
                            {"id": "m1", "user_id": "peer-a", "display_name": "Alpha", "content": "one", "created_at": "2026-03-06T10:00:01Z"},
                        ]
                    },
                    {
                        "messages": [
                            {"id": "m4", "user_id": "peer-b", "display_name": "Beta", "content": "four", "created_at": "2026-03-06T10:00:04Z"},
                            {"id": "m3", "user_id": "peer-a", "display_name": "Alpha", "content": "three", "created_at": "2026-03-06T10:00:03Z"},
                            {"id": "m2", "user_id": "peer-a", "display_name": "Alpha", "content": "two", "created_at": "2026-03-06T10:00:02Z"},
                            {"id": "m1", "user_id": "peer-a", "display_name": "Alpha", "content": "one", "created_at": "2026-03-06T10:00:01Z"},
                        ]
                    },
                ],
            }
        )

        seen = []
        monitor.on_new_message = lambda message: seen.append((message.message_id, message.content))

        monitor._poll_messages()
        self.assertEqual(seen, [])

        monitor._poll_messages()
        self.assertEqual(seen, [("m3", "three"), ("m4", "four")])

    def test_self_authored_messages_do_not_trigger_notifications(self):
        monitor = _StubMonitor(
            {
                "/auth/status": {"user_id": "owner"},
                "/channels": [
                    {"channels": [{"id": "general", "name": "general"}]},
                    {"channels": [{"id": "general", "name": "general"}]},
                ],
                "/channels/general/messages?limit=10": [
                    {
                        "messages": [
                            {"id": "m1", "user_id": "peer-a", "display_name": "Alpha", "content": "seed", "created_at": "2026-03-06T10:00:01Z"},
                        ]
                    },
                    {
                        "messages": [
                            {"id": "m3", "user_id": "peer-b", "display_name": "Beta", "content": "hello", "created_at": "2026-03-06T10:00:03Z"},
                            {"id": "m2", "user_id": "owner", "display_name": "Owner", "content": "self", "created_at": "2026-03-06T10:00:02Z"},
                            {"id": "m1", "user_id": "peer-a", "display_name": "Alpha", "content": "seed", "created_at": "2026-03-06T10:00:01Z"},
                        ]
                    },
                ],
            }
        )

        seen = []
        monitor.on_new_message = lambda message: seen.append(message.message_id)

        monitor._poll_messages()
        monitor._poll_messages()

        self.assertEqual(seen, ["m3"])

    def test_local_identity_retry_after_transient_auth_status_failure(self):
        monitor = _StubMonitor(
            {
                "/auth/status": [
                    Exception("temporary failure"),
                    {"user_id": "owner"},
                ],
                "/channels": [
                    {"channels": [{"id": "general", "name": "general"}]},
                    {"channels": [{"id": "general", "name": "general"}]},
                    {"channels": [{"id": "general", "name": "general"}]},
                ],
                "/channels/general/messages?limit=10": [
                    {
                        "messages": [
                            {"id": "m1", "user_id": "peer-a", "display_name": "Alpha", "content": "seed", "created_at": "2026-03-06T10:00:01Z"},
                        ]
                    },
                    {
                        "messages": [
                            {"id": "m2", "user_id": "owner", "display_name": "Owner", "content": "self", "created_at": "2026-03-06T10:00:02Z"},
                            {"id": "m1", "user_id": "peer-a", "display_name": "Alpha", "content": "seed", "created_at": "2026-03-06T10:00:01Z"},
                        ]
                    },
                    {
                        "messages": [
                            {"id": "m3", "user_id": "peer-b", "display_name": "Beta", "content": "hello", "created_at": "2026-03-06T10:00:03Z"},
                            {"id": "m2", "user_id": "owner", "display_name": "Owner", "content": "self", "created_at": "2026-03-06T10:00:02Z"},
                            {"id": "m1", "user_id": "peer-a", "display_name": "Alpha", "content": "seed", "created_at": "2026-03-06T10:00:01Z"},
                        ]
                    },
                ],
            }
        )

        seen = []
        monitor.on_new_message = lambda message: seen.append(message.message_id)

        monitor._poll_messages()
        self.assertFalse(monitor._local_identity_checked)

        monitor._poll_messages()
        self.assertTrue(monitor._local_identity_checked)
        self.assertEqual(seen, [])

        monitor._poll_messages()
        self.assertEqual(seen, ["m3"])
        self.assertEqual(monitor._local_user_id, "owner")


if __name__ == "__main__":
    unittest.main()
