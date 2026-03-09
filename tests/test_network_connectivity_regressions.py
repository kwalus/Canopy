"""Regression tests for P2P connectivity durability and endpoint truth."""

import asyncio
import os
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

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

from canopy.network.discovery import DiscoveredPeer, PeerDiscovery
from canopy.network.invite import InviteCode, import_invite
from canopy.network.manager import P2PNetworkManager
from canopy.network.identity import IdentityManager


class _DummyConfig:
    def __init__(self, tempdir: str) -> None:
        self.storage = types.SimpleNamespace(
            database_path=os.path.join(tempdir, 'canopy.db')
        )
        self.network = types.SimpleNamespace(
            mesh_port=7771,
            enable_tls=False,
            tls_cert_path=None,
            tls_key_path=None,
        )
        self.security = types.SimpleNamespace(
            allow_unverified_relay_messages=False,
            sync_digest_enabled=False,
            sync_digest_require_capability=True,
            sync_digest_max_channels_per_request=200,
            e2e_private_channels=False,
            e2e_private_channels_enforce=False,
            identity_portability_enabled=False,
        )


class _FakeServiceInfo:
    def __init__(self, peer_id: str, addresses: list[bytes], port: int = 7771) -> None:
        self.properties = {
            b'peer_id': peer_id.encode('utf-8'),
            b'version': b'0.4.0',
            b'capabilities': b'chat,files',
        }
        self.addresses = addresses
        self.port = port


class _FakeZeroconf:
    def __init__(self, info: _FakeServiceInfo) -> None:
        self._info = info

    def get_service_info(self, _service_type: str, _name: str):
        return self._info


class TestNetworkConnectivityRegressions(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.mkdtemp(prefix='canopy-connectivity-')

    def tearDown(self) -> None:
        shutil.rmtree(self.tempdir, ignore_errors=True)

    def _build_manager(self) -> P2PNetworkManager:
        manager = P2PNetworkManager(_DummyConfig(self.tempdir), MagicMock())
        manager.local_identity = types.SimpleNamespace(peer_id='local-peer')
        return manager

    def test_import_invite_persists_only_sanitized_endpoints_for_reconnect(self) -> None:
        identity_manager = IdentityManager(Path(self.tempdir) / 'peer_identity.json')
        identity_manager.initialize()
        identity_manager.verify_peer_id = lambda _pid, _pub: True  # type: ignore[assignment]

        invite = InviteCode(
            peer_id='peer-remote',
            ed25519_public_key_b58='11111111111111111111111111111111',
            x25519_public_key_b58='11111111111111111111111111111111',
            endpoints=[
                ' ws://192.168.1.50:7771 ',
                'ws://[2001:db8::10]:7771',
                'ws://192.168.1.50:7771',
                'localhost:7771',
                'ws://0.0.0.0:7771',
                'not-an-endpoint',
            ],
        )

        imported = import_invite(identity_manager, None, invite)

        self.assertEqual(
            identity_manager.peer_endpoints.get('peer-remote'),
            ['ws://192.168.1.50:7771', 'ws://[2001:db8::10]:7771'],
        )
        self.assertEqual(
            imported['endpoints'],
            ['ws://192.168.1.50:7771', 'ws://[2001:db8::10]:7771'],
        )

    def test_discovery_preserves_all_advertised_addresses(self) -> None:
        discovery = PeerDiscovery('local-peer')
        captured: list[DiscoveredPeer] = []
        discovery.on_peer_discovered(lambda peer, added: captured.append(peer) if added else None)

        zeroconf = _FakeZeroconf(
            _FakeServiceInfo(
                peer_id='peer-remote',
                addresses=[b'\x0a\x00\x00\x02', b'\xc0\xa8\x01\x64'],
            )
        )

        discovery._on_service_added(zeroconf, discovery.service_type, 'peer-remote._canopy._tcp.local.')

        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0].address, '10.0.0.2')
        self.assertEqual(captured[0].addresses, ['10.0.0.2', '192.168.1.100'])

    async def test_connect_to_discovered_peer_tries_all_advertised_addresses(self) -> None:
        manager = self._build_manager()
        attempts: list[str] = []
        sync_calls: list[str] = []

        async def _connect(peer_id: str, endpoint: str) -> bool:
            attempts.append(endpoint)
            return endpoint.endswith('192.168.1.100:7771')

        async def _sync(peer_id: str) -> None:
            sync_calls.append(peer_id)

        manager._connect_to_endpoint = _connect  # type: ignore[assignment]
        manager._run_post_connect_sync = _sync  # type: ignore[assignment]
        manager.connection_manager = types.SimpleNamespace(enable_tls=False)

        peer = DiscoveredPeer(
            peer_id='peer-remote',
            address='10.0.0.2',
            addresses=['10.0.0.2', '192.168.1.100'],
            port=7771,
            discovered_at=0.0,
        )

        await manager._connect_to_discovered_peer(peer)

        self.assertEqual(
            attempts,
            ['ws://10.0.0.2:7771', 'ws://192.168.1.100:7771'],
        )
        self.assertEqual(sync_calls, ['peer-remote'])

    def test_discovered_peer_endpoints_format_ipv6_for_dialing(self) -> None:
        manager = self._build_manager()
        peer = DiscoveredPeer(
            peer_id='peer-remote',
            address='2001:db8::10',
            addresses=['2001:db8::10'],
            port=7771,
            discovered_at=0.0,
        )

        self.assertEqual(
            manager._discovered_peer_endpoints(peer),
            ['ws://[2001:db8::10]:7771'],
        )

    async def test_peer_announcement_uses_stored_endpoints_not_socket_origin(self) -> None:
        manager = self._build_manager()
        manager.identity_manager.peer_display_names['peer-remote'] = 'Remote Node'
        manager.identity_manager.peer_endpoints['peer-remote'] = ['ws://192.168.1.55:7771']
        manager.identity_manager.known_peers['peer-remote'] = types.SimpleNamespace(
            ed25519_public_key=b'1' * 32,
            x25519_public_key=b'2' * 32,
        )
        manager.connection_manager = types.SimpleNamespace(
            get_connected_peers=lambda: ['peer-remote'],
            get_connection=lambda peer_id: types.SimpleNamespace(capabilities={}),
            connections={'peer-remote': types.SimpleNamespace(address='10.99.0.8')},
        )
        manager.get_peer_device_profile = None

        captured: list[list[dict]] = []

        class _Router:
            async def send_peer_announcement(self, to_peer: str, introduced_peers: list[dict]) -> bool:
                captured.append(introduced_peers)
                return True

        manager.message_router = _Router()

        await manager._send_peer_announcement_to('peer-target')

        self.assertEqual(len(captured), 1)
        self.assertEqual(
            captured[0][0]['endpoints'],
            ['ws://192.168.1.55:7771'],
        )

    async def test_reconnect_keeps_retrying_after_backoff_cap(self) -> None:
        manager = self._build_manager()
        manager._running = True
        manager._event_loop = asyncio.get_running_loop()
        manager.connection_manager = types.SimpleNamespace(
            is_connected=lambda _peer_id: False
        )
        manager.identity_manager.peer_endpoints['peer-remote'] = ['ws://192.168.1.50:7771']
        manager._record_connection_event = lambda *args, **kwargs: None  # type: ignore[assignment]

        async def _connect_to_endpoint(peer_id: str, endpoint: str) -> bool:
            return False

        manager._connect_to_endpoint = _connect_to_endpoint  # type: ignore[assignment]

        attempts: list[int] = []
        original_schedule = manager._schedule_reconnect

        def _wrapped_schedule(peer_id: str, attempt: int = 1) -> None:
            attempts.append(attempt)
            if attempt > manager._RECONNECT_MAX_BACKOFF_STAGE + 1:
                return
            original_schedule(peer_id, attempt)

        manager._schedule_reconnect = _wrapped_schedule  # type: ignore[assignment]

        async def _fast_sleep(_delay: float) -> None:
            return None

        original_sleep = asyncio.sleep
        with patch('canopy.network.manager.asyncio.sleep', new=_fast_sleep):
            manager._schedule_reconnect('peer-remote', attempt=manager._RECONNECT_MAX_BACKOFF_STAGE)
            await original_sleep(0.05)

        for task in list(manager._reconnect_tasks.values()):
            try:
                task.cancel()
            except Exception:
                pass

        self.assertIn(manager._RECONNECT_MAX_BACKOFF_STAGE + 1, attempts)

    async def test_startup_reconnect_prefers_discovered_endpoints_over_stale_persisted(self) -> None:
        manager = self._build_manager()
        manager.identity_manager.known_peers['peer-remote'] = types.SimpleNamespace()
        manager.identity_manager.peer_endpoints['peer-remote'] = ['ws://10.0.0.2:7771']
        manager.discovery = types.SimpleNamespace(
            get_peer=lambda peer_id: DiscoveredPeer(
                peer_id=peer_id,
                address='192.168.1.100',
                addresses=['192.168.1.100'],
                port=7771,
                discovered_at=0.0,
            )
        )
        attempts: list[str] = []
        sync_calls: list[str] = []
        connected_peers: set[str] = set()

        async def _connect(peer_id: str, endpoint: str) -> bool:
            attempts.append(endpoint)
            if endpoint == 'ws://192.168.1.100:7771':
                connected_peers.add(peer_id)
                return True
            return False

        async def _enqueue(peer_id: str) -> None:
            sync_calls.append(peer_id)

        manager._connect_to_endpoint = _connect  # type: ignore[assignment]
        manager._enqueue_sync = _enqueue  # type: ignore[assignment]
        manager.connection_manager = types.SimpleNamespace(
            is_connected=lambda peer_id: peer_id in connected_peers
        )

        original_sleep = asyncio.sleep

        async def _fast_sleep(_delay: float) -> None:
            return None

        with patch('canopy.network.manager.asyncio.sleep', new=_fast_sleep):
            await manager._reconnect_known_peers()
        await original_sleep(0)

        self.assertEqual(attempts, ['ws://192.168.1.100:7771'])
        self.assertEqual(sync_calls, ['peer-remote'])
        self.assertIn(
            'ws://192.168.1.100:7771',
            manager.identity_manager.peer_endpoints.get('peer-remote', []),
        )


if __name__ == '__main__':
    unittest.main()
