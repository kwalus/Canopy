"""Regression tests for remaining cross-mesh safety gaps."""

import asyncio
import os
import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

# Ensure repository root is importable when running tests directly.
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

from canopy.network.manager import P2PNetworkManager


LOCAL_MESH_ID = 'mesh-local-aaa'
FOREIGN_MESH_ID = 'mesh-foreign-bbb'
LOCAL_PEER_ID = 'local-peer-0001'
FOREIGN_PEER_ID = 'foreign-peer-0002'
RELAY_PEER_ID = 'relay-peer-0003'


def _make_local_identity(peer_id: str = LOCAL_PEER_ID):
    return SimpleNamespace(peer_id=peer_id)


def _make_config(meshspace_id: str = LOCAL_MESH_ID, meshspace_name: str = 'Local Mesh'):
    meshspace = SimpleNamespace(
        meshspace_id=meshspace_id,
        name=meshspace_name,
        enabled=bool(meshspace_id),
    )
    return SimpleNamespace(
        meshspace=meshspace,
        security=SimpleNamespace(trust_threshold=50),
    )


def _make_identity_manager_with_hint(
    peer_id: str,
    remote_mesh_id: str,
    *,
    cross_mesh_allowed: bool = False,
) -> MagicMock:
    hint = {
        'meshspace_id': remote_mesh_id,
        'meshspace_name': 'Foreign Mesh',
        'meshspace_fingerprint': 'fp-foreign',
        'cross_mesh_allowed': cross_mesh_allowed,
    }
    im = MagicMock()
    im.peer_meshspace_hints = {peer_id: hint}
    im.peer_endpoints = {}
    im.record_endpoint = MagicMock()
    im.get_peer_meshspace_hint = MagicMock(return_value=hint)
    im.record_peer_meshspace_hint = MagicMock()
    return im


def _make_base_manager(*, mesh_id: str = LOCAL_MESH_ID) -> P2PNetworkManager:
    manager = P2PNetworkManager.__new__(P2PNetworkManager)
    manager.local_identity = _make_local_identity()
    manager.config = _make_config(meshspace_id=mesh_id)
    manager._running = True
    manager.message_router = MagicMock()
    manager._active_relays = {}
    manager.peer_versions = {}
    manager._introduced_peers = {}
    manager.connection_manager = MagicMock()
    manager.connection_manager.is_connected = MagicMock(return_value=False)
    manager.discovery = None
    manager._event_loop = None
    manager._activity_lock = __import__('threading').Lock()
    manager._activity_events = __import__('collections').deque(maxlen=200)
    manager._endpoint_health_lock = __import__('threading').Lock()
    manager._endpoint_health = {}
    return manager


class TestConnectToEndpointPostHandshakeCrossMeshGuard(unittest.IsolatedAsyncioTestCase):
    def _make_manager_no_prior_hint(self) -> P2PNetworkManager:
        manager = _make_base_manager()
        im = MagicMock()
        im.peer_meshspace_hints = {}
        im.peer_endpoints = {}
        im.record_endpoint = MagicMock()
        im.get_peer_meshspace_hint = MagicMock(return_value={})
        im.record_peer_meshspace_hint = MagicMock()
        manager.identity_manager = im
        manager._record_connection_event = MagicMock()
        manager._record_endpoint_result = MagicMock()
        return manager

    async def test_outgoing_connection_to_cross_mesh_peer_is_disconnected_post_handshake(self):
        manager = self._make_manager_no_prior_hint()

        foreign_conn = SimpleNamespace(
            meshspace_id=FOREIGN_MESH_ID,
            meshspace_name='Foreign Mesh',
            meshspace_fingerprint='fp-foreign',
        )
        manager.connection_manager.connect_to_peer = AsyncMock(return_value=True)
        manager.connection_manager.get_connection = MagicMock(return_value=foreign_conn)
        manager.connection_manager.disconnect_peer = AsyncMock()

        def _side_effect_record_hint(peer_id, meta, *, source, cross_mesh_allowed=None):
            if meta and meta.get('meshspace_id'):
                hint = {
                    'meshspace_id': meta['meshspace_id'],
                    'meshspace_name': meta.get('meshspace_name', ''),
                    'meshspace_fingerprint': meta.get('meshspace_fingerprint', ''),
                    'cross_mesh_allowed': bool(cross_mesh_allowed),
                }
                manager.identity_manager.peer_meshspace_hints[peer_id] = hint
                manager.identity_manager.get_peer_meshspace_hint.return_value = hint

        manager._record_peer_meshspace_hint = _side_effect_record_hint

        result = await manager._connect_to_endpoint(FOREIGN_PEER_ID, 'ws://10.0.0.2:7771')

        self.assertFalse(result)
        manager.connection_manager.disconnect_peer.assert_called_once_with(FOREIGN_PEER_ID)
        event_call = manager._record_connection_event.call_args
        self.assertEqual(event_call.kwargs.get('status') or event_call.args[1], 'blocked')

    async def test_outgoing_connection_to_same_mesh_peer_is_kept(self):
        manager = self._make_manager_no_prior_hint()

        same_conn = SimpleNamespace(
            meshspace_id=LOCAL_MESH_ID,
            meshspace_name='Local Mesh',
            meshspace_fingerprint='fp-local',
        )
        manager.connection_manager.connect_to_peer = AsyncMock(return_value=True)
        manager.connection_manager.get_connection = MagicMock(return_value=same_conn)
        manager.connection_manager.disconnect_peer = AsyncMock()
        manager.identity_manager.record_endpoint = MagicMock()

        def _side_effect_record_hint(peer_id, meta, *, source, cross_mesh_allowed=None):
            if meta and meta.get('meshspace_id'):
                hint = {
                    'meshspace_id': meta['meshspace_id'],
                    'meshspace_name': meta.get('meshspace_name', ''),
                    'cross_mesh_allowed': bool(cross_mesh_allowed),
                }
                manager.identity_manager.peer_meshspace_hints[peer_id] = hint
                manager.identity_manager.get_peer_meshspace_hint.return_value = hint

        manager._record_peer_meshspace_hint = _side_effect_record_hint

        result = await manager._connect_to_endpoint('same-mesh-peer', 'ws://10.0.0.3:7771')

        self.assertTrue(result)
        manager.connection_manager.disconnect_peer.assert_not_called()

    async def test_allow_cross_mesh_flag_bypasses_post_handshake_guard(self):
        manager = self._make_manager_no_prior_hint()

        foreign_conn = SimpleNamespace(
            meshspace_id=FOREIGN_MESH_ID,
            meshspace_name='Foreign Mesh',
            meshspace_fingerprint='fp-foreign',
        )
        manager.connection_manager.connect_to_peer = AsyncMock(return_value=True)
        manager.connection_manager.get_connection = MagicMock(return_value=foreign_conn)
        manager.connection_manager.disconnect_peer = AsyncMock()
        manager.identity_manager.record_endpoint = MagicMock()

        def _side_effect_record_hint(peer_id, meta, *, source, cross_mesh_allowed=None):
            if meta and meta.get('meshspace_id'):
                hint = {
                    'meshspace_id': meta['meshspace_id'],
                    'cross_mesh_allowed': bool(cross_mesh_allowed),
                }
                manager.identity_manager.peer_meshspace_hints[peer_id] = hint
                manager.identity_manager.get_peer_meshspace_hint.return_value = hint

        manager._record_peer_meshspace_hint = _side_effect_record_hint
        manager.mark_peer_cross_mesh_allowed = MagicMock()

        result = await manager._connect_to_endpoint(
            FOREIGN_PEER_ID,
            'ws://10.0.0.4:7771',
            allow_cross_mesh=True,
        )

        self.assertTrue(result)
        manager.connection_manager.disconnect_peer.assert_not_called()

    async def test_allow_mesh_review_keeps_transport_without_approving_bridge(self):
        manager = self._make_manager_no_prior_hint()

        foreign_conn = SimpleNamespace(
            meshspace_id=FOREIGN_MESH_ID,
            meshspace_name='Foreign Mesh',
            meshspace_fingerprint='fp-foreign',
        )
        manager.connection_manager.connect_to_peer = AsyncMock(return_value=True)
        manager.connection_manager.get_connection = MagicMock(return_value=foreign_conn)
        manager.connection_manager.disconnect_peer = AsyncMock()
        manager.identity_manager.record_endpoint = MagicMock()

        def _side_effect_record_hint(peer_id, meta, *, source, cross_mesh_allowed=None):
            if meta and meta.get('meshspace_id'):
                hint = {
                    'meshspace_id': meta['meshspace_id'],
                    'meshspace_name': meta.get('meshspace_name', ''),
                    'meshspace_fingerprint': meta.get('meshspace_fingerprint', ''),
                    'cross_mesh_allowed': bool(cross_mesh_allowed),
                }
                manager.identity_manager.peer_meshspace_hints[peer_id] = hint
                manager.identity_manager.get_peer_meshspace_hint.return_value = hint

        manager._record_peer_meshspace_hint = _side_effect_record_hint
        manager.mark_peer_cross_mesh_allowed = MagicMock()

        result = await manager._connect_to_endpoint(
            FOREIGN_PEER_ID,
            'ws://10.0.0.5:7771',
            allow_mesh_review=True,
        )

        self.assertTrue(result)
        manager.mark_peer_cross_mesh_allowed.assert_not_called()
        manager.connection_manager.disconnect_peer.assert_not_called()
        statuses = [
            (call.kwargs.get('status') or (call.args[1] if len(call.args) > 1 else ''))
            for call in manager._record_connection_event.call_args_list
        ]
        self.assertIn('mesh_review_required', statuses)

    async def test_no_boundary_no_disconnect(self):
        manager = _make_base_manager(mesh_id='')
        im = MagicMock()
        im.peer_meshspace_hints = {}
        im.peer_endpoints = {}
        im.record_endpoint = MagicMock()
        im.get_peer_meshspace_hint = MagicMock(return_value={})
        im.record_peer_meshspace_hint = MagicMock()
        manager.identity_manager = im
        manager._record_connection_event = MagicMock()
        manager._record_endpoint_result = MagicMock()

        foreign_conn = SimpleNamespace(
            meshspace_id=FOREIGN_MESH_ID,
            meshspace_name='Foreign Mesh',
            meshspace_fingerprint='fp-foreign',
        )
        manager.connection_manager.connect_to_peer = AsyncMock(return_value=True)
        manager.connection_manager.get_connection = MagicMock(return_value=foreign_conn)
        manager.connection_manager.disconnect_peer = AsyncMock()
        manager.identity_manager.record_endpoint = MagicMock()
        manager._record_peer_meshspace_hint = lambda *args, **kwargs: None

        result = await manager._connect_to_endpoint(FOREIGN_PEER_ID, 'ws://10.0.0.5:7771')

        self.assertTrue(result)
        manager.connection_manager.disconnect_peer.assert_not_called()

    async def test_brokered_connection_preserves_explicit_cross_mesh_flag(self):
        manager = self._make_manager_no_prior_hint()
        manager.connection_manager.is_connected = MagicMock(side_effect=[False, False, True])
        manager._connect_to_endpoint = AsyncMock(return_value=True)
        manager.message_router.remove_route = MagicMock()
        manager._active_relays = {FOREIGN_PEER_ID: RELAY_PEER_ID}
        manager._run_post_connect_sync = AsyncMock()

        await manager._attempt_brokered_connection(
            FOREIGN_PEER_ID,
            ['ws://10.0.0.7:7771', 'ws://10.0.0.8:7771'],
            broker_peer=RELAY_PEER_ID,
            allow_cross_mesh=True,
        )

        self.assertGreaterEqual(manager._connect_to_endpoint.await_count, 1)
        first_call = manager._connect_to_endpoint.await_args_list[0]
        self.assertEqual(
            first_call.args,
            (FOREIGN_PEER_ID, 'ws://10.0.0.7:7771'),
        )
        self.assertTrue(first_call.kwargs.get('allow_cross_mesh'))
        manager.message_router.remove_route.assert_called_once_with(FOREIGN_PEER_ID)
        self.assertNotIn(FOREIGN_PEER_ID, manager._active_relays)
        manager._run_post_connect_sync.assert_awaited_once_with(FOREIGN_PEER_ID)


class TestRelayOfferCrossMeshGuard(unittest.TestCase):
    def _make_manager_with_cross_mesh_hint(self) -> P2PNetworkManager:
        manager = _make_base_manager()
        manager.identity_manager = _make_identity_manager_with_hint(
            FOREIGN_PEER_ID,
            FOREIGN_MESH_ID,
        )
        manager._record_connection_event = MagicMock()
        manager.connection_manager.is_connected = MagicMock(
            side_effect=lambda pid: pid == RELAY_PEER_ID
        )
        return manager

    def test_relay_offer_for_cross_mesh_target_is_declined(self):
        manager = self._make_manager_with_cross_mesh_hint()

        manager._on_relay_offer(relay_peer=RELAY_PEER_ID, target_peer=FOREIGN_PEER_ID)

        manager.message_router.update_routing_table.assert_not_called()
        self.assertNotIn(FOREIGN_PEER_ID, manager._active_relays)

    def test_relay_offer_for_same_mesh_target_is_accepted(self):
        manager = _make_base_manager()
        im = MagicMock()
        im.peer_meshspace_hints = {}
        im.get_peer_meshspace_hint = MagicMock(return_value={})
        manager.identity_manager = im
        manager.connection_manager.is_connected = MagicMock(
            side_effect=lambda pid: pid == RELAY_PEER_ID
        )
        manager._record_connection_event = MagicMock()

        manager._on_relay_offer(relay_peer=RELAY_PEER_ID, target_peer='same-mesh-peer')

        manager.message_router.update_routing_table.assert_called_once_with(
            'same-mesh-peer',
            RELAY_PEER_ID,
        )
        self.assertEqual(manager._active_relays.get('same-mesh-peer'), RELAY_PEER_ID)

    def test_relay_offer_for_approved_cross_mesh_target_is_accepted(self):
        manager = _make_base_manager()
        im = _make_identity_manager_with_hint(
            FOREIGN_PEER_ID,
            FOREIGN_MESH_ID,
            cross_mesh_allowed=True,
        )
        manager.identity_manager = im
        manager.connection_manager.is_connected = MagicMock(
            side_effect=lambda pid: pid == RELAY_PEER_ID
        )
        manager._record_connection_event = MagicMock()

        manager._on_relay_offer(relay_peer=RELAY_PEER_ID, target_peer=FOREIGN_PEER_ID)

        manager.message_router.update_routing_table.assert_called_once_with(
            FOREIGN_PEER_ID,
            RELAY_PEER_ID,
        )
        self.assertEqual(manager._active_relays.get(FOREIGN_PEER_ID), RELAY_PEER_ID)

    def test_relay_offer_declined_when_relay_peer_not_connected(self):
        manager = _make_base_manager()
        im = MagicMock()
        im.peer_meshspace_hints = {}
        im.get_peer_meshspace_hint = MagicMock(return_value={})
        manager.identity_manager = im
        manager.connection_manager.is_connected = MagicMock(return_value=False)

        manager._on_relay_offer(relay_peer='unconnected-relay', target_peer='any-peer')

        manager.message_router.update_routing_table.assert_not_called()
        self.assertNotIn('any-peer', manager._active_relays)


if __name__ == '__main__':
    unittest.main()
