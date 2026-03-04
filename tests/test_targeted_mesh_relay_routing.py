"""Routing tests for targeted mesh-relay fallback paths."""

import os
import sys
import types
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

# Provide a lightweight zeroconf stub for environments without optional deps.
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

from canopy.network.routing import MessageRouter, MessageType, P2PMessage


class _DummyIdentityManager:
    local_identity = None


class _DummyConnectionManager:
    def __init__(self, connected_peers):
        self._connected = list(connected_peers)
        self.sent = []

    def get_connected_peers(self):
        return list(self._connected)

    def is_connected(self, peer_id):
        return peer_id in self._connected

    async def send_to_peer(self, peer_id, payload):
        self.sent.append((peer_id, payload))
        return True


def _targeted_message(msg_type: MessageType, to_peer: str = "peer-target") -> P2PMessage:
    return P2PMessage(
        id="MTEST123",
        type=msg_type,
        from_peer="peer-local",
        to_peer=to_peer,
        timestamp=1000.0,
        ttl=4,
        payload={"content": "", "metadata": {"type": msg_type.value}},
    )


class TestTargetedMeshRelayRouting(unittest.IsolatedAsyncioTestCase):
    async def test_route_to_peer_prefers_direct_link(self):
        conn = _DummyConnectionManager(["peer-target", "peer-relay"])
        router = MessageRouter("peer-local", _DummyIdentityManager(), conn)

        sent = await router._route_to_peer(_targeted_message(MessageType.MEMBER_SYNC))

        self.assertTrue(sent)
        self.assertEqual(len(conn.sent), 1)
        self.assertEqual(conn.sent[0][0], "peer-target")

    async def test_route_to_peer_uses_routing_table_next_hop(self):
        conn = _DummyConnectionManager(["peer-relay"])
        router = MessageRouter("peer-local", _DummyIdentityManager(), conn)
        router.update_routing_table("peer-target", "peer-relay")

        sent = await router._route_to_peer(_targeted_message(MessageType.CHANNEL_KEY_REQUEST))

        self.assertTrue(sent)
        self.assertEqual(len(conn.sent), 1)
        self.assertEqual(conn.sent[0][0], "peer-relay")

    async def test_route_to_peer_mesh_relays_supported_targeted_types(self):
        conn = _DummyConnectionManager(["peer-relay-a", "peer-relay-b"])
        router = MessageRouter("peer-local", _DummyIdentityManager(), conn)

        sent = await router._route_to_peer(_targeted_message(MessageType.CHANNEL_KEY_DISTRIBUTION))

        self.assertTrue(sent)
        self.assertNotIn("peer-target", router.pending_messages)
        peers = {peer for peer, _ in conn.sent}
        self.assertEqual(peers, {"peer-relay-a", "peer-relay-b"})

    async def test_route_to_peer_mesh_relays_membership_query(self):
        conn = _DummyConnectionManager(["peer-relay-a", "peer-relay-b"])
        router = MessageRouter("peer-local", _DummyIdentityManager(), conn)

        sent = await router._route_to_peer(_targeted_message(MessageType.CHANNEL_MEMBERSHIP_QUERY))

        self.assertTrue(sent)
        self.assertNotIn("peer-target", router.pending_messages)
        peers = {peer for peer, _ in conn.sent}
        self.assertEqual(peers, {"peer-relay-a", "peer-relay-b"})

    async def test_mesh_fallback_excludes_immediate_upstream_peer(self):
        conn = _DummyConnectionManager(["peer-relay-a", "peer-relay-b"])
        router = MessageRouter("peer-local", _DummyIdentityManager(), conn)
        message = _targeted_message(MessageType.MEMBER_SYNC, to_peer="peer-target")
        setattr(message, "_via_peer", "peer-relay-a")

        sent = await router._route_to_peer(message)

        self.assertTrue(sent)
        peers = {peer for peer, _ in conn.sent}
        self.assertEqual(peers, {"peer-relay-b"})

    async def test_non_relay_targeted_types_queue_when_unreachable(self):
        conn = _DummyConnectionManager(["peer-relay-a"])
        router = MessageRouter("peer-local", _DummyIdentityManager(), conn)

        sent = await router._route_to_peer(_targeted_message(MessageType.CHANNEL_SYNC))

        self.assertFalse(sent)
        self.assertIn("peer-target", router.pending_messages)
        self.assertEqual(len(conn.sent), 0)


if __name__ == "__main__":
    unittest.main()
