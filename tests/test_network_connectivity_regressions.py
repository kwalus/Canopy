"""Regression tests for P2P connectivity durability and endpoint truth."""

import asyncio
import os
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

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
from canopy.network.connection import ConnectionManager
from canopy.network.invite import InviteCode, classify_invite_endpoints, import_invite, generate_invite
from canopy.network.manager import P2PNetworkManager
from canopy.network.identity import IdentityManager
from canopy.core.config import (
    Config,
    apply_transport_security_settings,
    load_transport_security_settings,
    save_transport_security_settings,
)


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
            require_verified_wss=False,
            external_tls_endpoint='',
            transport_security_mode='',
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
        self.meshspace = types.SimpleNamespace(
            enabled=True,
            meshspace_id='family-mesh',
            meshspace_id_aliases=[],
            name='Family Mesh',
            registry_root='',
            network_quarantined=False,
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
                ' ws://198.51.100.50:7771 ',
                'ws://[2001:db8::10]:7771',
                'ws://198.51.100.50:7771',
                'localhost:7771',
                'ws://0.0.0.0:7771',
                'not-an-endpoint',
            ],
            mesh_name='Research Mesh',
            meshspace_id='research-mesh',
            meshspace_fingerprint='A123-B456',
        )

        imported = import_invite(identity_manager, None, invite)

        self.assertEqual(
            identity_manager.peer_endpoints.get('peer-remote'),
            ['ws://198.51.100.50:7771', 'ws://[2001:db8::10]:7771'],
        )
        self.assertEqual(
            imported['endpoints'],
            ['ws://198.51.100.50:7771', 'ws://[2001:db8::10]:7771'],
        )
        self.assertEqual(
            identity_manager.get_peer_meshspace_hint('peer-remote').get('meshspace_id'),
            'research-mesh',
        )

    def test_identity_manager_mesh_sync_approval_inherits_to_later_peer(self) -> None:
        identity_manager = IdentityManager(Path(self.tempdir) / 'peer_identity.json')
        identity_manager.initialize()

        identity_manager.record_peer_meshspace_hint(
            'peer-alpha',
            meshspace_id='family-mesh',
            meshspace_name='Family Mesh',
            meshspace_fingerprint='ABCD-1234',
            source='handshake',
        )
        identity_manager.record_peer_meshspace_hint(
            'peer-beta',
            meshspace_id='family-mesh',
            meshspace_name='Family Mesh',
            meshspace_fingerprint='ABCD-1234',
            source='handshake',
        )

        identity_manager.set_peer_sync_approval('peer-alpha', 'mesh')

        status = identity_manager.get_peer_sync_approval_status('peer-beta')
        self.assertFalse(status.get('preview_only'))
        self.assertEqual(status.get('effective_scope'), 'mesh')
        self.assertEqual(status.get('inherited_from_peer_id'), 'peer-alpha')

    def test_generate_invite_accepts_explicit_external_mesh_endpoint(self) -> None:
        identity_manager = IdentityManager(Path(self.tempdir) / 'peer_identity.json')
        identity_manager.initialize()

        with patch('canopy.network.invite.get_local_ips', return_value=['192.168.1.10']):
            invite = generate_invite(
                identity_manager,
                7771,
                external_endpoint='wss://demo.ngrok-free.app:443',
            )

        self.assertEqual(invite.endpoints[0], 'wss://demo.ngrok-free.app:443')
        self.assertIn('ws://192.168.1.10:7771', invite.endpoints[1:])

    def test_generate_invite_uses_wss_for_local_tls_listener_endpoints(self) -> None:
        identity_manager = IdentityManager(Path(self.tempdir) / 'peer_identity.json')
        identity_manager.initialize()

        with patch('canopy.network.invite.get_local_ips', return_value=['192.168.1.10']):
            invite = generate_invite(
                identity_manager,
                7771,
                public_host='mesh.example.com',
                endpoint_scheme='wss',
            )

        self.assertIn('wss://mesh.example.com:7771', invite.endpoints)
        self.assertIn('wss://192.168.1.10:7771', invite.endpoints)
        self.assertNotIn('ws://mesh.example.com:7771', invite.endpoints)

    def test_transport_security_settings_persist_per_data_dir(self) -> None:
        config = Config()
        config.storage.data_dir = self.tempdir

        path = save_transport_security_settings(config, {
            'mode': 'external_terminator',
            'external_tls_endpoint': 'wss://mesh.example.com:443',
            'require_verified_wss': True,
        })
        loaded = load_transport_security_settings(config)
        applied = apply_transport_security_settings(config, loaded)

        self.assertTrue(path.exists())
        self.assertEqual(applied.get('mode'), 'external_terminator')
        self.assertFalse(config.network.enable_tls)
        self.assertEqual(config.network.external_tls_endpoint, 'wss://mesh.example.com:443')
        self.assertTrue(config.network.require_verified_wss)

    def test_generate_invite_suppresses_same_host_plain_public_wss_fallback_by_default(self) -> None:
        identity_manager = IdentityManager(Path(self.tempdir) / 'peer_identity.json')
        identity_manager.initialize()

        with patch('canopy.network.invite.get_local_ips', return_value=['50.6.243.52', '192.168.1.10']):
            invite = generate_invite(
                identity_manager,
                7771,
                external_endpoint='wss://50.6.243.52:7771',
                public_host='50.6.243.52',
            )

        self.assertIn('wss://50.6.243.52:7771', invite.endpoints)
        self.assertNotIn('ws://50.6.243.52:7771', invite.endpoints)
        self.assertIn('ws://192.168.1.10:7771', invite.endpoints)

    def test_generate_invite_allows_same_host_plain_fallback_when_explicitly_requested(self) -> None:
        identity_manager = IdentityManager(Path(self.tempdir) / 'peer_identity.json')
        identity_manager.initialize()

        with patch('canopy.network.invite.get_local_ips', return_value=['50.6.243.52']):
            invite = generate_invite(
                identity_manager,
                7771,
                external_endpoint='wss://50.6.243.52:7771',
                allow_plain_fallback=True,
            )

        self.assertEqual(invite.endpoints[0], 'wss://50.6.243.52:7771')
        self.assertIn('ws://50.6.243.52:7771', invite.endpoints)

    def test_classify_invite_endpoints_marks_secure_public_and_lan_fallbacks(self) -> None:
        classification = classify_invite_endpoints([
            'wss://50.6.243.52:7771',
            'ws://192.168.1.10:7771',
        ])

        self.assertEqual(classification.get('recommended_endpoint'), 'wss://50.6.243.52:7771')
        self.assertEqual(classification.get('recommended_transport'), 'wss')
        self.assertEqual(classification.get('invite_policy'), 'secure_public')
        self.assertFalse(classification.get('has_public_plain_fallback'))
        self.assertTrue(classification.get('has_lan_fallback'))

    def test_invite_encode_decode_preserves_preview_metadata(self) -> None:
        invite = InviteCode(
            peer_id='peer-remote',
            ed25519_public_key_b58='ed-key',
            x25519_public_key_b58='x-key',
            endpoints=['wss://mesh.example:443'],
            mesh_name='Family Mesh',
            meshspace_id='family-mesh',
            meshspace_fingerprint='E4A1-22B0',
            peer_label='Kitchen Node',
            instance_label='Mac Mini',
            peer_icon='bi-laptop',
            generated_at='2026-04-10T12:00:00+00:00',
        )

        decoded = InviteCode.decode(invite.encode())

        self.assertEqual(decoded.peer_id, 'peer-remote')
        self.assertEqual(decoded.endpoints, ['wss://mesh.example:443'])
        self.assertEqual(decoded.mesh_name, 'Family Mesh')
        self.assertEqual(decoded.meshspace_id, 'family-mesh')
        self.assertEqual(decoded.meshspace_fingerprint, 'E4A1-22B0')
        self.assertEqual(decoded.peer_label, 'Kitchen Node')
        self.assertEqual(decoded.instance_label, 'Mac Mini')
        self.assertEqual(decoded.peer_icon, 'bi-laptop')
        self.assertEqual(decoded.generated_at, '2026-04-10T12:00:00+00:00')

    def test_invite_decode_raw_json_does_not_inherit_base64_padding(self) -> None:
        raw_json = (
            '{"v":1,"pid":"peer-remote","epk":"ed-key","xpk":"x-key",'
            '"ep":["wss://mesh.example:443"],"mn":"Family Mesh"}'
        )

        decoded = InviteCode.decode(raw_json)

        self.assertEqual(decoded.peer_id, 'peer-remote')
        self.assertEqual(decoded.mesh_name, 'Family Mesh')
        self.assertEqual(decoded.endpoints, ['wss://mesh.example:443'])

    def test_generate_invite_accepts_preview_metadata(self) -> None:
        identity_manager = IdentityManager(Path(self.tempdir) / 'peer_identity.json')
        identity_manager.initialize()

        invite = generate_invite(
            identity_manager,
            7771,
            mesh_name='Business Mesh',
            meshspace_id='business-mesh',
            peer_label='VPS Bridge',
            instance_label='canopy-vps-1',
            peer_icon='bi-server',
            generated_at='2026-04-10T12:00:00+00:00',
        )

        self.assertEqual(invite.mesh_name, 'Business Mesh')
        self.assertEqual(invite.meshspace_id, 'business-mesh')
        self.assertRegex(invite.meshspace_fingerprint or '', r'^[0-9A-F]{4}-[0-9A-F]{4}$')
        self.assertEqual(invite.peer_label, 'VPS Bridge')
        self.assertEqual(invite.instance_label, 'canopy-vps-1')
        self.assertEqual(invite.peer_icon, 'bi-server')
        self.assertEqual(invite.generated_at, '2026-04-10T12:00:00+00:00')
        self.assertEqual(InviteCode.decode(invite.encode()).mesh_name, 'Business Mesh')
        self.assertEqual(InviteCode.decode(invite.encode()).meshspace_id, 'business-mesh')

    def test_invite_decode_accepts_labeled_copy_block(self) -> None:
        invite = InviteCode(
            peer_id='peer-remote',
            ed25519_public_key_b58='ed-key',
            x25519_public_key_b58='x-key',
            endpoints=['wss://mesh.example:443'],
            mesh_name='Family Mesh',
            meshspace_id='family-mesh',
            meshspace_fingerprint='E4A1-22B0',
        )

        decoded = InviteCode.decode(
            f"Canopy invite for Family Mesh [E4A1-22B0]\n{invite.encode()}\nVerify before connecting."
        )

        self.assertEqual(decoded.peer_id, 'peer-remote')
        self.assertEqual(decoded.meshspace_id, 'family-mesh')

    def test_generate_invite_defaults_wss_external_endpoint_to_443(self) -> None:
        identity_manager = IdentityManager(Path(self.tempdir) / 'peer_identity.json')
        identity_manager.initialize()

        invite = generate_invite(
            identity_manager,
            7771,
            external_endpoint='wss://demo.ngrok-free.app',
        )

        self.assertEqual(invite.endpoints[0], 'wss://demo.ngrok-free.app:443')

    def test_generate_invite_formats_ipv6_public_host(self) -> None:
        identity_manager = IdentityManager(Path(self.tempdir) / 'peer_identity.json')
        identity_manager.initialize()

        invite = generate_invite(
            identity_manager,
            7771,
            public_host='2001:db8::10',
            public_port=9001,
        )

        self.assertIn('ws://[2001:db8::10]:9001', invite.endpoints)

    def test_discovery_preserves_all_advertised_addresses(self) -> None:
        discovery = PeerDiscovery('local-peer')
        captured: list[DiscoveredPeer] = []
        discovery.on_peer_discovered(lambda peer, added: captured.append(peer) if added else None)

        zeroconf = _FakeZeroconf(
            _FakeServiceInfo(
                peer_id='peer-remote',
                addresses=[b'\xc0\x00\x02\x02', b'\xc6\x33\x64\x64'],
            )
        )

        discovery._on_service_added(zeroconf, discovery.service_type, 'peer-remote._canopy._tcp.local.')

        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0].address, '192.0.2.2')
        self.assertEqual(captured[0].addresses, ['192.0.2.2', '198.51.100.100'])

    async def test_connect_to_discovered_peer_tries_all_advertised_addresses(self) -> None:
        manager = self._build_manager()
        attempts: list[str] = []
        sync_calls: list[str] = []

        async def _connect(peer_id: str, endpoint: str) -> bool:
            attempts.append(endpoint)
            return endpoint.endswith('198.51.100.100:7771')

        async def _sync(peer_id: str) -> None:
            sync_calls.append(peer_id)

        manager._connect_to_endpoint = _connect  # type: ignore[assignment]
        manager._run_post_connect_sync = _sync  # type: ignore[assignment]
        manager.connection_manager = types.SimpleNamespace(enable_tls=False)

        peer = DiscoveredPeer(
            peer_id='peer-remote',
            address='192.0.2.2',
            addresses=['192.0.2.2', '198.51.100.100'],
            port=7771,
            discovered_at=0.0,
        )

        await manager._connect_to_discovered_peer(peer)

        self.assertEqual(
            attempts,
            ['ws://192.0.2.2:7771', 'ws://198.51.100.100:7771'],
        )
        self.assertEqual(sync_calls, ['peer-remote'])

    async def test_connect_to_endpoint_preserves_explicit_wss_scheme(self) -> None:
        manager = self._build_manager()
        captured: list[tuple[str, str, int, str | None, bool]] = []

        async def _connect(peer_id: str, host: str, port: int, scheme=None, scheme_explicit=False) -> bool:
            captured.append((peer_id, host, port, scheme, scheme_explicit))
            return True

        manager.connection_manager = types.SimpleNamespace(
            enable_tls=False,
            connect_to_peer=_connect,
        )
        manager._record_connection_event = lambda *args, **kwargs: None  # type: ignore[assignment]
        manager._record_endpoint_result = lambda *args, **kwargs: None  # type: ignore[assignment]

        ok = await manager._connect_to_endpoint('peer-remote', 'wss://demo.ngrok-free.app:443')

        self.assertTrue(ok)
        self.assertEqual(
            captured,
            [('peer-remote', 'demo.ngrok-free.app', 443, 'wss', True)],
        )

    async def test_connect_to_endpoint_defaults_missing_wss_port_to_443(self) -> None:
        manager = self._build_manager()
        captured: list[tuple[str, str, int, str | None, bool]] = []

        async def _connect(peer_id: str, host: str, port: int, scheme=None, scheme_explicit=False) -> bool:
            captured.append((peer_id, host, port, scheme, scheme_explicit))
            return True

        manager.connection_manager = types.SimpleNamespace(
            enable_tls=False,
            connect_to_peer=_connect,
        )
        manager._record_connection_event = lambda *args, **kwargs: None  # type: ignore[assignment]
        manager._record_endpoint_result = lambda *args, **kwargs: None  # type: ignore[assignment]

        ok = await manager._connect_to_endpoint('peer-remote', 'wss://demo.ngrok-free.app')

        self.assertTrue(ok)
        self.assertEqual(
            captured,
            [('peer-remote', 'demo.ngrok-free.app', 443, 'wss', True)],
        )

    async def test_explicit_wss_connect_does_not_retry_plain_ws_after_tls_failure(self) -> None:
        manager = ConnectionManager(
            local_peer_id='local-peer',
            identity_manager=types.SimpleNamespace(),
            enable_tls=False,
        )
        attempts: list[str] = []

        async def _fail_connect(uri: str, **_kwargs):
            attempts.append(uri)
            raise OSError('tls failed')

        with patch('canopy.network.connection.websockets.connect', new=_fail_connect):
            ok = await manager.connect_to_peer(
                'peer-remote',
                'demo.ngrok-free.app',
                443,
                scheme='wss',
                scheme_explicit=True,
            )

        self.assertFalse(ok)
        self.assertEqual(attempts, ['wss://demo.ngrok-free.app:443/p2p'])
        failure = manager.get_last_connect_failure('peer-remote', 'demo.ngrok-free.app', 443)
        self.assertEqual(failure.get('reason'), 'wss_downgrade_blocked')

    async def test_inferred_tls_connect_can_still_retry_plain_ws_for_legacy_peers(self) -> None:
        manager = ConnectionManager(
            local_peer_id='local-peer',
            identity_manager=types.SimpleNamespace(),
            enable_tls=False,
        )
        manager.enable_tls = True
        manager._client_ssl = None
        attempts: list[str] = []

        class _FakeWebSocket:
            async def close(self):
                return None

        async def _connect(uri: str, **_kwargs):
            attempts.append(uri)
            if uri.startswith('wss://'):
                raise OSError('tls failed')
            return _FakeWebSocket()

        async def _handshake(_connection):
            return False

        manager._perform_handshake = _handshake  # type: ignore[assignment]

        with patch('canopy.network.connection.websockets.connect', new=_connect):
            ok = await manager.connect_to_peer(
                'peer-remote',
                'demo.ngrok-free.app',
                7771,
                scheme=None,
                scheme_explicit=False,
            )

        self.assertFalse(ok)
        self.assertEqual(
            attempts,
            ['wss://demo.ngrok-free.app:7771/p2p', 'ws://demo.ngrok-free.app:7771/p2p'],
        )

    async def test_verified_wss_mode_blocks_inferred_plain_ws_downgrade(self) -> None:
        manager = ConnectionManager(
            local_peer_id='local-peer',
            identity_manager=types.SimpleNamespace(),
            enable_tls=False,
            require_verified_wss=True,
        )
        manager.enable_tls = True
        attempts: list[str] = []

        async def _fail_connect(uri: str, **_kwargs):
            attempts.append(uri)
            raise OSError('certificate verify failed')

        with patch('canopy.network.connection.websockets.connect', new=_fail_connect):
            ok = await manager.connect_to_peer(
                'peer-remote',
                'secure.example.com',
                443,
                scheme=None,
                scheme_explicit=False,
            )

        self.assertFalse(ok)
        self.assertEqual(attempts, ['wss://secure.example.com:443/p2p'])
        failure = manager.get_last_connect_failure('peer-remote', 'secure.example.com', 443)
        self.assertEqual(failure.get('reason'), 'wss_downgrade_blocked')

    def test_transport_security_status_reports_plain_invite_endpoints(self) -> None:
        manager = ConnectionManager(
            local_peer_id='local-peer',
            identity_manager=types.SimpleNamespace(),
            enable_tls=False,
        )

        status = manager.get_transport_security_status(
            advertised_endpoints=['ws://192.168.1.10:7771']
        )

        self.assertEqual(status.get('mesh_scheme'), 'ws')
        self.assertFalse(status.get('tls_enabled'))
        self.assertEqual(status.get('tls_cert_mode'), 'disabled')
        self.assertEqual(status.get('client_verification_mode'), 'permissive')
        self.assertEqual(status.get('advertised_secure_count'), 0)
        self.assertEqual(status.get('advertised_plain_count'), 1)
        self.assertFalse(status.get('explicit_wss_downgrade_allowed'))

    def test_transport_security_status_reports_self_signed_tls_listener(self) -> None:
        cert_path = Path(self.tempdir) / 'tls' / 'canopy.crt'
        key_path = Path(self.tempdir) / 'tls' / 'canopy.key'
        manager = ConnectionManager(
            local_peer_id='local-peer',
            identity_manager=types.SimpleNamespace(),
            enable_tls=True,
            tls_cert_path=str(cert_path),
            tls_key_path=str(key_path),
        )

        status = manager.get_transport_security_status(
            advertised_endpoints=['wss://mesh.example.com:443']
        )

        self.assertEqual(status.get('mesh_scheme'), 'wss')
        self.assertTrue(status.get('tls_enabled'))
        self.assertEqual(status.get('tls_cert_mode'), 'self_signed')
        self.assertTrue(status.get('tls_cert_path_present'))
        self.assertTrue(status.get('tls_key_path_present'))
        self.assertEqual(status.get('advertised_secure_count'), 1)

    def test_parse_endpoint_rejects_non_websocket_schemes(self) -> None:
        manager = self._build_manager()
        self.assertIsNone(manager._parse_endpoint('https://demo.ngrok-free.app'))

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
        manager.identity_manager.peer_endpoints['peer-remote'] = ['ws://198.51.100.55:7771']
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
            ['ws://198.51.100.55:7771'],
        )
        self.assertEqual(captured[0][0]['display_name'], 'Remote Node')
        self.assertEqual(captured[0][0]['peer_label'], 'Remote Node')
        self.assertEqual(captured[0][0]['instance_label'], 'Remote Node')

    def test_current_connection_endpoint_preserves_wss_scheme_from_endpoint_uri(self) -> None:
        manager = self._build_manager()
        manager.connection_manager = types.SimpleNamespace(
            get_connection=lambda peer_id: types.SimpleNamespace(
                endpoint_uri='wss://demo.ngrok-free.app:443',
                address='10.99.0.8',
                port=7771,
            )
        )

        self.assertEqual(
            manager._current_connection_endpoint('peer-remote'),
            'wss://demo.ngrok-free.app:443',
        )

    def test_incoming_authenticated_persists_peer_advertised_endpoints(self) -> None:
        manager = self._build_manager()
        manager._event_loop = None
        manager._introduced_peers = {
            'peer-remote': {
                'peer_id': 'peer-remote',
                'introduced_by': 'broker-a',
                'endpoints': [],
            }
        }

        manager._on_incoming_peer_authenticated(
            'peer-remote',
            {
                'advertised_endpoints': ['wss://demo.example.com:443'],
                'capabilities': [],
            },
        )

        self.assertEqual(
            manager.identity_manager.peer_endpoints.get('peer-remote'),
            ['wss://demo.example.com:443'],
        )
        self.assertEqual(
            manager._introduced_peers['peer-remote']['endpoints'],
            ['wss://demo.example.com:443'],
        )
        self.assertIn(
            'peer_advertised',
            manager._introduced_peers['peer-remote'].get('endpoint_sources', []),
        )

    def test_incoming_cross_mesh_peer_is_disconnected_until_approved(self) -> None:
        manager = self._build_manager()
        manager._event_loop = MagicMock()
        manager._event_loop.is_closed.return_value = False
        manager.connection_manager = types.SimpleNamespace(disconnect_peer=AsyncMock())
        manager._remember_peer_advertised_endpoints = MagicMock()
        manager._remember_discovered_peer_endpoints = MagicMock()
        manager._refresh_peer_version_info = MagicMock()
        manager._record_connection_event = MagicMock()

        with patch('asyncio.run_coroutine_threadsafe') as run_threadsafe:
            manager._on_incoming_peer_authenticated(
                'peer-remote',
                {
                    'meshspace_id': 'other-mesh',
                    'meshspace_name': 'Other Mesh',
                    'meshspace_fingerprint': 'ABCD-1234',
                    'advertised_endpoints': ['wss://demo.example.com:443'],
                },
            )

        self.assertEqual(
            manager.identity_manager.get_peer_meshspace_hint('peer-remote').get('meshspace_id'),
            'other-mesh',
        )
        manager._remember_peer_advertised_endpoints.assert_not_called()
        manager._remember_discovered_peer_endpoints.assert_not_called()
        run_threadsafe.assert_called_once()
        disconnect_coro = run_threadsafe.call_args.args[0]
        self.assertTrue(asyncio.iscoroutine(disconnect_coro))
        disconnect_coro.close()
        self.assertEqual(run_threadsafe.call_args.args[1], manager._event_loop)
        blocked_calls = [
            call for call in manager._record_connection_event.call_args_list
            if call.kwargs.get('status') == 'blocked'
        ]
        self.assertEqual(len(blocked_calls), 1)
        self.assertIn('Incoming connection rejected', blocked_calls[0].kwargs.get('detail', ''))

    def test_disconnected_cleanup_skips_reconnect_for_cross_mesh_peer(self) -> None:
        manager = self._build_manager()
        manager.identity_manager.known_peers['peer-remote'] = types.SimpleNamespace()
        manager.identity_manager.peer_endpoints['peer-remote'] = ['ws://198.51.100.50:7771']
        manager.identity_manager.record_peer_meshspace_hint(
            'peer-remote',
            meshspace_id='other-mesh',
            meshspace_name='Other Mesh',
            meshspace_fingerprint='DEAD-BEEF',
            source='invite',
        )
        manager.message_router = None

        schedule_calls: list[str] = []
        manager._schedule_reconnect = lambda peer_id, attempt=1: schedule_calls.append(peer_id)  # type: ignore[assignment]

        manager.on_peer_disconnected_cleanup('peer-remote')

        self.assertEqual(schedule_calls, [])

    def test_cross_mesh_status_exposes_pending_bridge_and_alias_relationships(self) -> None:
        manager = self._build_manager()
        manager.identity_manager.record_peer_meshspace_hint(
            'peer-remote',
            meshspace_id='family-mesh-legacy',
            meshspace_name='Family Mesh',
            source='invite',
        )

        pending = manager.get_peer_cross_mesh_status('peer-remote')
        self.assertTrue(pending.get('requires_manual_confirmation'))
        self.assertEqual(pending.get('mesh_relationship'), 'mesh_mismatch_pending_review')

        manager.mark_peer_cross_mesh_allowed('peer-remote', True)
        bridge = manager.get_peer_cross_mesh_status('peer-remote')
        self.assertFalse(bridge.get('requires_manual_confirmation'))
        self.assertEqual(bridge.get('mesh_relationship'), 'cross_mesh_bridge_allowed')
        self.assertNotIn('family-mesh-legacy', manager.config.meshspace.meshspace_id_aliases)

        manager.identity_manager.record_peer_meshspace_hint(
            'peer-alias',
            meshspace_id='family-mesh-old',
            meshspace_name='Family Mesh',
            source='invite',
        )
        alias_status = manager.mark_peer_meshspace_equivalent('peer-alias')
        self.assertFalse(alias_status.get('requires_manual_confirmation'))
        self.assertEqual(alias_status.get('mesh_relationship'), 'same_mesh_alias_confirmed')
        self.assertIn('family-mesh-old', manager.config.meshspace.meshspace_id_aliases)

    def test_get_introduced_peers_marks_broker_only_when_no_direct_endpoints(self) -> None:
        manager = self._build_manager()
        manager._introduced_peers = {
            'peer-remote': {
                'peer_id': 'peer-remote',
                'introduced_by': 'broker-a',
                'endpoints': [],
            }
        }
        manager.get_connected_peers = lambda: ['broker-a']
        manager.get_peer_id = lambda: 'local-peer'

        introduced = manager.get_introduced_peers()

        self.assertEqual(len(introduced), 1)
        self.assertEqual(introduced[0]['connect_strategy'], 'broker_only')
        self.assertEqual(introduced[0]['endpoint_sources'], ['none'])
        self.assertEqual(introduced[0]['broker_candidates'], ['broker-a'])

    def test_get_introduced_peers_marks_unreachable_without_connected_broker(self) -> None:
        manager = self._build_manager()
        manager._introduced_peers = {
            'peer-remote': {
                'peer_id': 'peer-remote',
                'introduced_by': 'offline-broker',
                'introduced_via': ['offline-broker'],
                'endpoints': [],
            }
        }
        manager.get_connected_peers = lambda: []
        manager.get_peer_id = lambda: 'local-peer'

        introduced = manager.get_introduced_peers()

        self.assertEqual(len(introduced), 1)
        self.assertEqual(introduced[0]['connect_strategy'], 'unreachable')
        self.assertEqual(introduced[0]['broker_candidates'], [])

    def test_get_introduced_peers_requires_explicit_review_for_cross_mesh_candidate(self) -> None:
        manager = self._build_manager()
        manager._introduced_peers = {
            'peer-remote': {
                'peer_id': 'peer-remote',
                'introduced_by': 'broker-a',
                'endpoints': ['ws://198.51.100.12:7771'],
                'meshspace_hint': {
                    'meshspace_id': 'business-mesh',
                    'meshspace_name': 'Business Mesh',
                },
            }
        }
        manager.get_connected_peers = lambda: ['broker-a']
        manager.get_peer_id = lambda: 'local-peer'

        introduced = manager.get_introduced_peers()

        self.assertEqual(len(introduced), 1)
        self.assertEqual(introduced[0]['connect_strategy'], 'explicit_meshspace_required')
        self.assertTrue(introduced[0]['requires_explicit_meshspace_connect'])
        self.assertEqual(introduced[0]['meshspace_relationship'], 'cross_mesh_candidate')
        self.assertEqual(introduced[0]['remote_meshspace_label'], 'Business Mesh')

    def test_store_introduced_peers_persists_announced_meshspace_hint(self) -> None:
        manager = self._build_manager()
        manager.store_introduced_peers(
            [
                {
                    'peer_id': 'peer-remote',
                    'display_name': 'Remote Business Node',
                    'endpoints': ['ws://198.51.100.12:7771'],
                    'ed25519_public_key': '',
                    'x25519_public_key': '',
                    'meshspace_id': 'business-mesh',
                    'meshspace_name': 'Business Mesh',
                }
            ],
            'broker-a',
        )

        introduced = manager.get_introduced_peers()

        self.assertEqual(introduced[0]['remote_meshspace_id'], 'business-mesh')
        self.assertEqual(
            manager.identity_manager.get_peer_meshspace_hint('peer-remote').get('meshspace_id'),
            'business-mesh',
        )

    def test_send_broker_request_prefers_advertised_endpoints(self) -> None:
        manager = self._build_manager()
        manager._running = True
        manager._event_loop = MagicMock()
        manager.message_router = types.SimpleNamespace(send_broker_request=MagicMock())
        manager.local_identity = types.SimpleNamespace(
            peer_id='local-peer',
            ed25519_public_key=b'\x01' * 32,
            x25519_public_key=b'\x02' * 32,
        )
        manager._record_connection_event = MagicMock()
        manager._get_local_advertised_endpoints = lambda: ['wss://demo.example.com:443']  # type: ignore[assignment]

        future = MagicMock()
        future.result.return_value = True

        with patch('asyncio.run_coroutine_threadsafe', return_value=future):
            sent = manager.send_broker_request('peer-target', 'broker-a', allow_cross_mesh=True)

        self.assertTrue(sent)
        self.assertEqual(
            manager.message_router.send_broker_request.call_args.kwargs['requester_endpoints'],
            ['wss://demo.example.com:443'],
        )
        self.assertTrue(
            manager.message_router.send_broker_request.call_args.kwargs['allow_cross_mesh']
        )

    def test_operator_wss_endpoint_is_reused_for_handshake_advertisements(self) -> None:
        manager = self._build_manager()
        manager.identity_manager.initialize()
        manager.local_identity = manager.identity_manager.local_identity
        manager.remember_operator_advertised_endpoint(
            external_endpoint='wss://50.6.243.52:7771',
            allow_plain_fallback=False,
        )

        with patch('canopy.network.invite.get_local_ips', return_value=['50.6.243.52', '192.168.1.10']):
            endpoints = manager._get_local_advertised_endpoints()

        self.assertEqual(endpoints[0], 'wss://50.6.243.52:7771')
        self.assertNotIn('ws://50.6.243.52:7771', endpoints)
        self.assertIn('ws://192.168.1.10:7771', endpoints)

    def test_local_tls_config_advertises_wss_handshake_endpoints(self) -> None:
        manager = self._build_manager()
        manager.config.network.enable_tls = True
        manager.config.network.transport_security_mode = 'self_signed'
        manager.identity_manager.initialize()
        manager.local_identity = manager.identity_manager.local_identity

        with patch('canopy.network.invite.get_local_ips', return_value=['192.168.1.10']):
            endpoints = manager._get_local_advertised_endpoints()

        self.assertEqual(endpoints, ['wss://192.168.1.10:7771'])

    def test_pending_tls_config_does_not_advertise_wss_until_listener_restarts(self) -> None:
        manager = self._build_manager()
        manager.config.network.enable_tls = True
        manager.config.network.transport_security_mode = 'self_signed'
        manager.identity_manager.initialize()
        manager.local_identity = manager.identity_manager.local_identity
        manager.connection_manager = types.SimpleNamespace(enable_tls=False)

        with patch('canopy.network.invite.get_local_ips', return_value=['192.168.1.10']):
            endpoints = manager._get_local_advertised_endpoints()

        self.assertEqual(endpoints, ['ws://192.168.1.10:7771'])

    def test_clearing_operator_endpoint_restores_default_handshake_advertisements(self) -> None:
        manager = self._build_manager()
        manager.identity_manager.initialize()
        manager.local_identity = manager.identity_manager.local_identity
        manager.remember_operator_advertised_endpoint(
            external_endpoint='wss://50.6.243.52:7771',
            allow_plain_fallback=False,
        )
        manager.remember_operator_advertised_endpoint()

        with patch('canopy.network.invite.get_local_ips', return_value=['50.6.243.52', '192.168.1.10']):
            endpoints = manager._get_local_advertised_endpoints()

        self.assertNotIn('wss://50.6.243.52:7771', endpoints)
        self.assertIn('ws://50.6.243.52:7771', endpoints)
        self.assertIn('ws://192.168.1.10:7771', endpoints)

    def test_peer_active_transport_reports_current_endpoint_scheme(self) -> None:
        manager = self._build_manager()
        manager.connection_manager = types.SimpleNamespace(
            get_connection=lambda peer_id: types.SimpleNamespace(
                endpoint_uri='wss://demo.ngrok-free.app:443',
                address='10.0.0.10',
                port=7771,
            )
        )

        status = manager.get_peer_active_transport('peer-remote')

        self.assertEqual(status.get('active_endpoint'), 'wss://demo.ngrok-free.app:443')
        self.assertEqual(status.get('active_transport'), 'wss')
        self.assertTrue(status.get('active_transport_secure'))

    def test_connectable_endpoints_prefer_public_over_stale_private_storage(self) -> None:
        manager = self._build_manager()
        manager.identity_manager.peer_endpoints['peer-remote'] = [
            'ws://198.51.100.159:7771',
            'wss://vps.example.com:443',
        ]

        endpoints = manager._get_connectable_peer_endpoints('peer-remote', prefer_discovered=True)

        self.assertEqual(
            endpoints,
            ['wss://vps.example.com:443', 'ws://198.51.100.159:7771'],
        )

    async def test_reconnect_keeps_retrying_after_backoff_cap(self) -> None:
        manager = self._build_manager()
        manager._running = True
        manager._event_loop = asyncio.get_running_loop()
        manager.connection_manager = types.SimpleNamespace(
            is_connected=lambda _peer_id: False
        )
        manager.identity_manager.peer_endpoints['peer-remote'] = ['ws://198.51.100.50:7771']
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
        manager.identity_manager.peer_endpoints['peer-remote'] = ['ws://192.0.2.2:7771']
        manager.discovery = types.SimpleNamespace(
            get_peer=lambda peer_id: DiscoveredPeer(
                peer_id=peer_id,
                address='198.51.100.100',
                addresses=['198.51.100.100'],
                port=7771,
                discovered_at=0.0,
            )
        )
        attempts: list[str] = []
        sync_calls: list[str] = []
        connected_peers: set[str] = set()

        async def _connect(peer_id: str, endpoint: str) -> bool:
            attempts.append(endpoint)
            if endpoint == 'ws://198.51.100.100:7771':
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

        self.assertEqual(attempts, ['ws://198.51.100.100:7771'])
        self.assertEqual(sync_calls, ['peer-remote'])
        self.assertIn(
            'ws://198.51.100.100:7771',
            manager.identity_manager.peer_endpoints.get('peer-remote', []),
        )

    async def test_startup_reconnect_skips_unapproved_cross_mesh_peer(self) -> None:
        manager = self._build_manager()
        manager.identity_manager.known_peers['peer-remote'] = types.SimpleNamespace()
        manager.identity_manager.peer_endpoints['peer-remote'] = ['ws://198.51.100.50:7771']
        manager.identity_manager.record_peer_meshspace_hint(
            'peer-remote',
            meshspace_id='business-mesh',
            meshspace_name='Business Mesh',
            meshspace_fingerprint='7D4A-22BE',
            source='invite',
        )
        attempts: list[str] = []

        async def _connect(peer_id: str, endpoint: str) -> bool:
            attempts.append(endpoint)
            return True

        manager._connect_to_endpoint = _connect  # type: ignore[assignment]
        manager.connection_manager = types.SimpleNamespace(
            is_connected=lambda _peer_id: False
        )

        async def _fast_sleep(_delay: float) -> None:
            return None

        with patch('canopy.network.manager.asyncio.sleep', new=_fast_sleep):
            await manager._reconnect_known_peers()

        self.assertEqual(attempts, [])

    async def test_enqueue_sync_coalesces_duplicate_peer_requests(self) -> None:
        manager = self._build_manager()
        manager._sync_queue = asyncio.Queue()
        manager._queued_sync_peers = set()
        manager._syncs_in_progress = set()
        manager._resync_after_current = set()

        await manager._enqueue_sync('peer-remote')
        await manager._enqueue_sync('peer-remote')

        self.assertEqual(manager._sync_queue.qsize(), 1)
        self.assertEqual(manager._queued_sync_peers, {'peer-remote'})

    async def test_enqueue_sync_marks_one_rerun_when_peer_already_in_progress(self) -> None:
        manager = self._build_manager()
        manager._sync_queue = asyncio.Queue()
        manager._queued_sync_peers = set()
        manager._syncs_in_progress = {'peer-remote'}
        manager._resync_after_current = set()

        await manager._enqueue_sync('peer-remote')

        self.assertEqual(manager._sync_queue.qsize(), 0)
        self.assertEqual(manager._resync_after_current, {'peer-remote'})

    def test_catchup_timeconstants_are_tuned_for_fast_repair(self) -> None:
        manager = self._build_manager()

        self.assertEqual(manager._STARTUP_GRACE_PERIOD, 8.0)
        self.assertEqual(manager._POST_CONNECT_SETTLE_WINDOW_S, 1.0)
        self.assertEqual(manager._MAX_CONCURRENT_CATCHUPS_STARTUP, 3)
        self.assertEqual(manager._MAX_CONCURRENT_CATCHUPS_NORMAL, 6)
        self.assertEqual(manager.PERIODIC_CATCHUP_INITIAL_DELAY, 20)
        self.assertEqual(manager.PERIODIC_CATCHUP_INTERVAL, 90)
        self.assertEqual(manager.PERIODIC_CATCHUP_PEER_STAGGER, 0.75)

    async def test_wait_for_connection_settle_sleeps_until_window_elapses(self) -> None:
        manager = self._build_manager()
        manager._POST_CONNECT_SETTLE_WINDOW_S = 1.5
        conn = types.SimpleNamespace(
            connected_at=100.0,
            is_connected=lambda: True,
        )
        manager.connection_manager = types.SimpleNamespace(
            get_connection=lambda peer_id: conn if peer_id == 'peer-remote' else None
        )

        sleeps: list[float] = []

        async def _fake_sleep(delay: float) -> None:
            sleeps.append(delay)
            conn.connected_at = 98.0

        with patch('canopy.network.manager.time.time', return_value=100.2), patch(
            'canopy.network.manager.asyncio.sleep',
            new=_fake_sleep,
        ):
            ok = await manager._wait_for_connection_settle('peer-remote')

        self.assertTrue(ok)
        self.assertEqual(len(sleeps), 1)
        self.assertAlmostEqual(sleeps[0], 1.3, places=1)

    async def test_post_connect_sync_impl_preserves_reconnect_task_when_settle_fails(self) -> None:
        manager = self._build_manager()
        manager.on_peer_connected = None
        manager._peer_is_trusted_for_content = lambda peer_id: False
        manager._refresh_peer_version_info = lambda peer_id: None
        cancelled: list[str] = []
        manager._cancel_reconnect = lambda peer_id: cancelled.append(peer_id)
        manager.connection_manager = types.SimpleNamespace(
            get_connection=lambda peer_id: None,
        )
        manager._post_connect_retry_counts = {}
        manager._post_connect_retry_tokens = {}
        manager._POST_CONNECT_SYNC_MAX_RETRIES = 3
        manager._resync_after_current = set()

        async def _settle(peer_id: str) -> bool:
            return False

        manager._wait_for_connection_settle = _settle  # type: ignore[assignment]

        await manager._run_post_connect_sync_impl('peer-remote')

        self.assertEqual(cancelled, [])
        self.assertEqual(manager._post_connect_retry_counts.get('peer-remote'), 1)
        self.assertEqual(manager._resync_after_current, {'peer-remote'})

    async def test_post_connect_sync_impl_cancels_reconnect_task_after_settle_succeeds(self) -> None:
        manager = self._build_manager()
        manager.on_peer_connected = None
        manager._peer_is_trusted_for_content = lambda peer_id: True
        manager.get_peer_sync_status = lambda peer_id: {'preview_only': False}
        manager._refresh_peer_version_info = lambda peer_id: None
        cancelled: list[str] = []
        calls: list[tuple[str, str]] = []
        manager._cancel_reconnect = lambda peer_id: cancelled.append(peer_id)
        manager._post_connect_retry_counts = {'peer-remote': 2}
        manager._post_connect_retry_tokens = {'peer-remote': 'out:1:deadbeef'}

        async def _settle(peer_id: str) -> bool:
            return True

        async def _record(name: str, peer_id: str) -> None:
            calls.append((name, peer_id))

        manager._wait_for_connection_settle = _settle  # type: ignore[assignment]
        manager._send_channel_sync_to_peer = lambda peer_id: _record('channel_sync', peer_id)  # type: ignore[assignment]
        manager._send_public_channel_metadata_replay_to_peer = lambda peer_id: _record('metadata_replay', peer_id)  # type: ignore[assignment]
        manager._send_profile_to_peer = lambda peer_id: _record('profile', peer_id)  # type: ignore[assignment]
        manager._send_membership_recovery_query = lambda peer_id: _record('membership', peer_id)  # type: ignore[assignment]
        manager._retry_missing_channel_key_requests_for_peer = lambda peer_id: _record('keys', peer_id)  # type: ignore[assignment]
        manager._send_peer_announcement_to = lambda peer_id: _record('peer_announce', peer_id)  # type: ignore[assignment]
        manager._announce_new_peer_to_others = lambda peer_id: _record('announce_others', peer_id)  # type: ignore[assignment]
        manager._send_catchup_request = lambda peer_id: _record('catchup', peer_id)  # type: ignore[assignment]
        manager.message_router = None

        await manager._run_post_connect_sync_impl('peer-remote')

        self.assertEqual(cancelled, ['peer-remote'])
        self.assertEqual(
            calls,
            [
                ('channel_sync', 'peer-remote'),
                ('metadata_replay', 'peer-remote'),
                ('profile', 'peer-remote'),
                ('membership', 'peer-remote'),
                ('keys', 'peer-remote'),
                ('peer_announce', 'peer-remote'),
                ('announce_others', 'peer-remote'),
                ('catchup', 'peer-remote'),
            ],
        )
        self.assertEqual(manager._post_connect_retry_counts, {})
        self.assertEqual(manager._post_connect_retry_tokens, {})

    def test_request_post_connect_sync_retry_resets_counter_when_connection_changes(self) -> None:
        manager = self._build_manager()
        conn_a = types.SimpleNamespace(
            is_outbound=True,
            connected_at=1.0,
        )
        conn_b = types.SimpleNamespace(
            is_outbound=False,
            connected_at=2.0,
        )
        current = {'conn': conn_a}
        manager.connection_manager = types.SimpleNamespace(
            get_connection=lambda peer_id: current['conn'],
        )
        manager._post_connect_retry_counts = {}
        manager._post_connect_retry_tokens = {}
        manager._POST_CONNECT_SYNC_MAX_RETRIES = 3
        manager._resync_after_current = set()

        manager._request_post_connect_sync_retry('peer-remote', 'connection_changed_before_settle')
        self.assertEqual(manager._post_connect_retry_counts.get('peer-remote'), 1)

        current['conn'] = conn_b
        manager._request_post_connect_sync_retry('peer-remote', 'connection_changed_before_settle')
        self.assertEqual(manager._post_connect_retry_counts.get('peer-remote'), 1)


if __name__ == '__main__':
    unittest.main()
