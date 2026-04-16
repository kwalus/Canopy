"""Regression coverage for meshspace foundation wiring."""

import base64
import io
import json
import os
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

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

from canopy.core.app import create_app
from canopy.core.meshspaces import MeshspaceRegistryManager, build_meshspace_notification_summary
from canopy.network.invite import InviteCode

_PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4////fwAJ+wP9KobjigAAAABJRU5ErkJggg=="
)


class _FakeP2PNetworkManager:
    def __init__(self, config, db_manager):
        self.config = config
        self.db_manager = db_manager
        self.relay_policy = 'broker_only'
        self.local_identity = SimpleNamespace(peer_id='peer-mesh-test')
        self.identity_manager = SimpleNamespace(
            local_identity=self.local_identity,
            peer_display_names={},
            known_peers={},
        )
        self.connection_manager = SimpleNamespace(is_connected=lambda peer_id: False)
        self.discovery = None
        self.peer_versions = {}
        self._running = False

    def set_relay_policy(self, policy):
        self.relay_policy = policy

    def get_peer_id(self):
        return self.local_identity.peer_id

    def get_connected_peers(self):
        return []

    def get_discovered_peers(self):
        return []

    def get_introduced_peers(self):
        return []

    def get_relay_status(self):
        return {}

    def peer_supports_capability(self, peer_id, capability):
        return False

    def is_running(self):
        return self._running

    def start(self):
        self._running = True


class MeshspaceFoundationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.mesh_root = Path(self.tempdir.name) / 'family-lab'
        self.registry_root = Path(self.tempdir.name) / 'registry'

        self.env_patcher = patch.dict(
            os.environ,
            {
                'CANOPY_TESTING': 'true',
                'CANOPY_DISABLE_MESH': 'true',
                'CANOPY_MESHSPACE_ID': 'family-lab',
                'CANOPY_MESHSPACE_NAME': 'Family Lab',
                'CANOPY_MESHSPACE_ROOT': str(self.mesh_root),
                'CANOPY_MESHSPACE_REGISTRY_ROOT': str(self.registry_root),
                'CANOPY_SECRET_KEY': 'meshspace-secret',
            },
            clear=False,
        )
        self.env_patcher.start()
        self.addCleanup(self.env_patcher.stop)

        self.checkpoint_patcher = patch(
            'canopy.core.database.DatabaseManager._start_checkpoint_thread',
            lambda self: None,
        )
        self.checkpoint_patcher.start()
        self.addCleanup(self.checkpoint_patcher.stop)

        self.logging_patcher = patch(
            'canopy.core.app.setup_logging',
            lambda debug=False: None,
        )
        self.logging_patcher.start()
        self.addCleanup(self.logging_patcher.stop)

        self.p2p_patcher = patch(
            'canopy.core.app.P2PNetworkManager',
            _FakeP2PNetworkManager,
        )
        self.p2p_patcher.start()
        self.addCleanup(self.p2p_patcher.stop)

        self.app = create_app()
        self.client = self.app.test_client()
        self.db_manager = self.app.config['DB_MANAGER']

        with self.db_manager.get_connection() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO users (
                    id, username, public_key, password_hash, display_name,
                    origin_peer, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """,
                ('owner-user', 'owner', 'pk-owner', 'pw-owner', 'Owner', None),
            )
            conn.commit()
        self.db_manager.set_instance_owner_user_id('owner-user')

    def _authenticate(self) -> None:
        with self.client.session_transaction() as sess:
            sess['authenticated'] = True
            sess['user_id'] = 'owner-user'
            sess['username'] = 'owner'
            sess['display_name'] = 'Owner'
            sess['_csrf_token'] = 'csrf-mesh'

    def test_meshspace_mode_sets_isolated_storage_and_cookie_name(self) -> None:
        config = self.app.config['CANOPY_CONFIG']
        self.assertTrue(config.meshspace.enabled)
        self.assertEqual(config.meshspace.meshspace_id, 'family-lab')
        self.assertEqual(config.meshspace.name, 'Family Lab')
        self.assertEqual(config.storage.data_dir, str(self.mesh_root))
        self.assertEqual(config.storage.database_path, str(self.mesh_root / 'canopy.db'))
        self.assertEqual(self.app.config['SESSION_COOKIE_NAME'], 'canopy_session_family-lab')

    def test_meshspace_mode_honors_discovery_port_env_override(self) -> None:
        from canopy.core.config import Config

        with patch.dict(os.environ, {'CANOPY_DISCOVERY_PORT': '7802'}, clear=False):
            config = Config.from_env()
        self.assertEqual(config.network.discovery_port, 7802)

    def test_meshspace_runtime_endpoints_expose_identity_and_headers(self) -> None:
        health = self.client.get('/api/v1/health')
        self.assertEqual(health.status_code, 200)
        payload = health.get_json() or {}
        self.assertEqual((payload.get('meshspace') or {}).get('meshspace_id'), 'family-lab')
        self.assertEqual((payload.get('meshspace') or {}).get('name'), 'Family Lab')
        self.assertTrue(payload.get('ready'))
        self.assertFalse(payload.get('setup_required'))
        self.assertEqual(health.headers.get('X-Canopy-Mesh-ID'), 'family-lab')

        ready = self.client.get('/api/v1/ready')
        self.assertEqual(ready.status_code, 200)

        identity = self.client.get('/api/v1/mesh/identity')
        self.assertEqual(identity.status_code, 200)
        identity_payload = identity.get_json() or {}
        self.assertEqual((identity_payload.get('meshspace') or {}).get('meshspace_id'), 'family-lab')
        self.assertEqual((identity_payload.get('peer') or {}).get('peer_id'), 'peer-mesh-test')

    def test_public_mesh_identity_includes_compact_preview_fields(self) -> None:
        manager = self.app.config.get('MESHSPACE_REGISTRY_MANAGER')
        config = self.app.config['CANOPY_CONFIG']
        meshspace_id = config.meshspace.meshspace_id
        manager.set_meshspace_avatar(
            meshspace_id,
            _PNG_1X1,
            filename='identity.png',
            content_type='image/png',
        )
        with patch('canopy.api.routes._current_local_peer_hint_payload', return_value={
            'peer_label': 'Windy Admin',
            'instance_label': 'Alvin Node',
            'peer_avatar_b64': base64.b64encode(_PNG_1X1).decode('ascii'),
            'peer_avatar_mime': 'image/png',
            'peer_icon': 'bi-laptop',
        }):
            identity = self.client.get('/api/v1/mesh/identity')
        self.assertEqual(identity.status_code, 200)
        payload = identity.get_json() or {}
        self.assertEqual((payload.get('meshspace') or {}).get('meshspace_id'), meshspace_id)
        self.assertTrue((payload.get('meshspace') or {}).get('meshspace_fingerprint'))
        self.assertTrue((payload.get('meshspace') or {}).get('meshspace_avatar_b64'))
        self.assertEqual((payload.get('meshspace') or {}).get('meshspace_avatar_mime'), 'image/png')
        self.assertEqual((payload.get('peer') or {}).get('peer_label'), 'Windy Admin')
        self.assertTrue((payload.get('peer') or {}).get('peer_avatar_b64'))
        self.assertEqual((payload.get('peer') or {}).get('peer_avatar_mime'), 'image/png')
        self.assertEqual((payload.get('peer') or {}).get('peer_icon'), 'bi-laptop')

    def test_meshspace_registry_tracks_current_runtime(self) -> None:
        manager = self.app.config.get('MESHSPACE_REGISTRY_MANAGER')
        record = manager.get_meshspace('family-lab')
        self.assertIsNotNone(record)
        self.assertEqual(record.get('status'), 'running')
        self.assertEqual(record.get('http_port'), 7770)
        self.assertEqual(record.get('mesh_port'), 7771)

    def test_admin_mesh_routes_render_and_create_new_meshspace(self) -> None:
        self._authenticate()

        response = self.client.get('/meshes')
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn('Local meshes', body)
        self.assertIn('Family Lab', body)
        self.assertIn('New mesh', body)

        create_response = self.client.post(
            '/meshes/new',
            data={
                'csrf_token': 'csrf-mesh',
                'name': 'Research Lab',
                'meshspace_id': 'research-lab',
                'description': 'Experimental mesh for testing.',
                'icon': 'bi-bezier2',
                'accent': 'cyan',
            },
            follow_redirects=True,
        )
        self.assertEqual(create_response.status_code, 200)
        create_body = create_response.get_data(as_text=True)
        self.assertIn('Research Lab', create_body)
        self.assertIn('Launch', create_body)
        self.assertIn('Networking', create_body)
        self.assertIn('Enable networking after restart', create_body)
        self.assertIn('CANOPY_MESHSPACE_ID', create_body)
        self.assertIn('research-lab', create_body)
        self.assertIn('python -m canopy.main', create_body)

        manager = self.app.config.get('MESHSPACE_REGISTRY_MANAGER')
        record = manager.get_meshspace('research-lab')
        self.assertIsNotNone(record)
        self.assertEqual(record.get('status'), 'defined')
        self.assertTrue(record.get('network_quarantined'))
        launch_env = manager.launch_environment('research-lab')
        self.assertEqual(launch_env.get('CANOPY_MESHSPACE_SUPERVISED'), 'true')
        self.assertEqual(launch_env.get('CANOPY_MESHSPACE_NETWORK_QUARANTINED'), 'true')

    def test_mesh_header_presence_rail_and_shell_summary_render_for_active_meshes(self) -> None:
        self._authenticate()

        with self.db_manager.get_connection() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO users (
                    id, username, public_key, password_hash, display_name, status,
                    origin_peer, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """,
                ('pending-user', 'pending', 'pk-pending', 'pw-pending', 'Pending', 'pending_approval', None),
            )
            conn.commit()

        manager = self.app.config.get('MESHSPACE_REGISTRY_MANAGER')
        manager.create_meshspace(
            name='Research Lab',
            meshspace_id='research-lab',
            description='Experimental mesh for testing.',
            http_port=18700,
            mesh_port=18701,
            discovery_port=18702,
        )
        manager.update_runtime_state('research-lab', 'running', peer_id='peer-research')

        response = self.client.get('/meshes')
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn('meshspace-current-btn', body)
        self.assertIn('meshspace-rail-orb', body)
        self.assertIn('Research Lab', body)
        self.assertIn('bi-hourglass-split', body)
        self.assertIn('1 pending', body)

        refreshed = manager.get_meshspace('family-lab')
        self.assertIsNotNone(refreshed)
        summary = refreshed.get('summary') or {}
        self.assertEqual(int(summary.get('pending_review_count') or 0), 1)
        self.assertEqual(int(summary.get('unread_count') or 0), 0)
        self.assertEqual(int(summary.get('mention_count') or 0), 0)
        self.assertIn(summary.get('attention_level'), {'warning', 'active', 'critical'})

    def test_meshspace_notification_summary_counts_channel_unread_with_real_manager_signature(self) -> None:
        channel_manager = SimpleNamespace(
            get_user_channels=lambda user_id: [
                SimpleNamespace(unread_count=9),
                SimpleNamespace(unread_count=4),
            ]
        )

        summary = build_meshspace_notification_summary(
            None,
            channel_manager,
            None,
            None,
            'owner-user',
        )

        self.assertEqual(int(summary.get('unread_count') or 0), 13)
        self.assertEqual(int(summary.get('attention_count') or 0), 13)

    def test_meshes_page_uses_authenticated_session_user_not_local_user_fallback(self) -> None:
        self._authenticate()

        response = self.client.get('/meshes')
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn('"owner-user"', body)
        self.assertNotIn('"local_user"', body)

    def test_ajax_meshspaces_snapshot_reflects_current_and_other_mesh_attention(self) -> None:
        self._authenticate()

        manager = self.app.config.get('MESHSPACE_REGISTRY_MANAGER')
        manager.create_meshspace(
            name='Research Lab',
            meshspace_id='research-lab',
            description='Experimental mesh for testing.',
        )
        manager.update_runtime_state('research-lab', 'running', peer_id='peer-research')
        manager.update_shell_summary('research-lab', {
            'active_peer_count': 2,
            'unread_count': 5,
            'mention_count': 1,
            'pending_review_count': 0,
            'attention_count': 6,
            'last_activity_at': '2026-04-02T12:00:00+00:00',
            'has_recent_activity': True,
            'attention_level': 'active',
        })

        with self.db_manager.get_connection() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO users (
                    id, username, public_key, password_hash, display_name,
                    origin_peer, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """,
                ('sender-user', 'sender', 'pk-sender', 'pw-sender', 'Sender', None),
            )
            conn.execute(
                """
                INSERT INTO messages (id, sender_id, recipient_id, content, message_type, metadata)
                VALUES (?, ?, ?, ?, 'text', '{}')
                """,
                ('DM-001', 'sender-user', 'owner-user', 'Unread DM for mesh summary'),
            )
            conn.execute(
                """
                INSERT INTO mention_events (id, user_id, source_type, source_id, author_id, preview, status)
                VALUES (?, ?, ?, ?, ?, ?, 'new')
                """,
                ('MENTION-001', 'owner-user', 'channel_message', 'MSG-001', 'sender-user', 'Mention preview'),
            )
            conn.commit()

        attention_response = self.client.get('/ajax/sidebar_attention_summary')
        self.assertEqual(attention_response.status_code, 200)

        snapshot_response = self.client.get('/ajax/meshspaces_snapshot')
        self.assertEqual(snapshot_response.status_code, 200)
        payload = snapshot_response.get_json() or {}
        self.assertTrue(payload.get('success'))
        meshes = {item['meshspace_id']: item for item in (payload.get('meshspaces') or [])}
        self.assertGreaterEqual(int((meshes['family-lab'].get('unread_count') or 0)), 1)
        self.assertGreaterEqual(int((meshes['family-lab'].get('mention_count') or 0)), 1)
        self.assertNotEqual(str(meshes['family-lab'].get('attention_badge') or '').strip(), '!')
        self.assertEqual(int((meshes['research-lab'].get('unread_count') or 0)), 5)
        self.assertEqual(int((meshes['research-lab'].get('mention_count') or 0)), 1)
        self.assertEqual(str(meshes['research-lab'].get('attention_badge') or ''), '6')

    def test_meshspace_shell_summary_endpoint_requires_loopback(self) -> None:
        manager = self.app.config.get('MESHSPACE_REGISTRY_MANAGER')
        manager.update_shell_summary('family-lab', {
            'active_peer_count': 2,
            'unread_count': 4,
            'mention_count': 1,
            'pending_review_count': 0,
            'attention_count': 5,
            'last_activity_at': '2026-04-03T12:00:00+00:00',
            'last_summary_at': '2026-04-03T12:00:01+00:00',
            'has_recent_activity': True,
            'attention_level': 'active',
        })

        allowed = self.client.get(
            '/api/v1/meshspace/shell_summary',
            environ_overrides={'REMOTE_ADDR': '127.0.0.1'},
        )
        self.assertEqual(allowed.status_code, 200)
        payload = allowed.get_json() or {}
        self.assertTrue(payload.get('shell_safe'))
        self.assertEqual(payload.get('meshspace_id'), 'family-lab')
        self.assertEqual(int(payload.get('attention_count') or 0), 5)

        blocked = self.client.get(
            '/api/v1/meshspace/shell_summary',
            environ_overrides={'REMOTE_ADDR': '203.0.113.8'},
        )
        self.assertEqual(blocked.status_code, 403)

    def test_meshspace_viewer_attention_endpoint_returns_session_counts(self) -> None:
        self._authenticate()

        with self.db_manager.get_connection() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO users (
                    id, username, public_key, password_hash, display_name,
                    origin_peer, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """,
                ('sender-user', 'sender', 'pk-sender', 'pw-sender', 'Sender', None),
            )
            conn.execute(
                """
                INSERT INTO messages (id, sender_id, recipient_id, content, message_type, metadata)
                VALUES (?, ?, ?, ?, 'text', '{}')
                """,
                ('DM-VIEWER-001', 'sender-user', 'owner-user', 'Unread DM for viewer summary'),
            )
            conn.execute(
                """
                INSERT INTO mention_events (id, user_id, source_type, source_id, author_id, preview, status)
                VALUES (?, ?, ?, ?, ?, ?, 'new')
                """,
                ('MENTION-VIEWER-001', 'owner-user', 'channel_message', 'MSG-VIEWER-001', 'sender-user', 'Viewer mention'),
            )
            conn.commit()

        response = self.client.get(
            '/api/v1/meshspace/viewer_attention',
            environ_overrides={'REMOTE_ADDR': '127.0.0.1'},
        )
        self.assertEqual(response.status_code, 200)
        payload = response.get_json() or {}
        self.assertTrue(payload.get('viewer_specific'))
        self.assertEqual(payload.get('meshspace_id'), 'family-lab')
        self.assertGreaterEqual(int(payload.get('unread_count') or 0), 1)
        self.assertGreaterEqual(int(payload.get('mention_count') or 0), 1)
        self.assertGreaterEqual(int(payload.get('attention_count') or 0), 2)

    def test_sidebar_attention_clear_removes_current_mesh_mention_count(self) -> None:
        self._authenticate()

        with self.db_manager.get_connection() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO users (
                    id, username, public_key, password_hash, display_name,
                    origin_peer, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """,
                ('sender-user', 'sender', 'pk-sender', 'pw-sender', 'Sender', None),
            )
            conn.execute(
                """
                INSERT INTO mention_events (id, user_id, source_type, source_id, author_id, preview, status)
                VALUES (?, ?, ?, ?, ?, ?, 'new')
                """,
                ('MENTION-CLEAR-001', 'owner-user', 'channel_message', 'MSG-CLEAR-001', 'sender-user', 'Clear mention'),
            )
            conn.commit()

        before = self.client.get('/ajax/meshspaces_snapshot').get_json() or {}
        before_meshes = {item['meshspace_id']: item for item in (before.get('meshspaces') or [])}
        self.assertEqual(int((before_meshes['family-lab'].get('mention_count') or 0)), 1)

        cleared = self.client.post(
            '/ajax/sidebar_attention_clear',
            json={},
            headers={'X-CSRFToken': 'csrf-mesh'},
        )
        self.assertEqual(cleared.status_code, 200)
        self.assertTrue((cleared.get_json() or {}).get('success'))

        after = self.client.get('/ajax/meshspaces_snapshot').get_json() or {}
        after_meshes = {item['meshspace_id']: item for item in (after.get('meshspaces') or [])}
        self.assertEqual(int((after_meshes['family-lab'].get('mention_count') or 0)), 0)

    def test_probe_meshspace_runtime_includes_live_shell_summary(self) -> None:
        manager = self.app.config.get('MESHSPACE_REGISTRY_MANAGER')
        manager.create_meshspace(
            name='Research Lab',
            meshspace_id='research-lab',
            description='Experimental mesh for testing.',
        )
        manager.update_runtime_state('research-lab', 'running', peer_id='peer-research')
        manager._probe_cache.clear()

        with patch('canopy.core.meshspaces._pid_is_alive', return_value=False), \
             patch('canopy.core.meshspaces._listener_pid_for_port', return_value=4242), \
             patch('canopy.core.meshspaces._port_accepts_connections', return_value=True), \
             patch(
                 'canopy.core.meshspaces._http_json',
                 side_effect=[
                     {'ready': True, 'version': '0.5.51', 'peer': {'peer_id': 'peer-research'}},
                     {
                         'active_peer_count': 3,
                         'unread_count': 5,
                         'mention_count': 2,
                         'pending_review_count': 0,
                         'attention_count': 7,
                         'has_recent_activity': True,
                         'attention_level': 'active',
                         'last_activity_at': '2026-04-03T12:00:00+00:00',
                         'last_summary_at': '2026-04-03T12:00:01+00:00',
                     },
                 ],
             ):
            probe = manager.probe_meshspace_runtime('research-lab')

        self.assertTrue(probe.get('live'))
        self.assertEqual(probe.get('effective_status'), 'running')
        self.assertEqual((probe.get('shell_summary') or {}).get('attention_count'), 7)
        self.assertEqual((probe.get('shell_summary') or {}).get('mention_count'), 2)

    def test_probe_meshspace_runtime_rejects_health_from_different_meshspace(self) -> None:
        manager = self.app.config.get('MESHSPACE_REGISTRY_MANAGER')
        manager.create_meshspace(
            name='Research Lab',
            meshspace_id='research-lab',
            description='Experimental mesh for testing.',
        )
        manager.update_runtime_state('research-lab', 'running', peer_id='peer-research')
        manager._probe_cache.clear()

        with patch('canopy.core.meshspaces._pid_is_alive', return_value=False), \
             patch('canopy.core.meshspaces._listener_pid_for_port', return_value=4242), \
             patch('canopy.core.meshspaces._port_accepts_connections', return_value=True), \
             patch(
                 'canopy.core.meshspaces._http_json',
                 return_value={
                     'ready': True,
                     'version': '0.5.63',
                     'meshspace': {'meshspace_id': 'family-lab'},
                     'peer': {'peer_id': 'peer-family'},
                 },
             ):
            probe = manager.probe_meshspace_runtime('research-lab')

        self.assertFalse(probe.get('live'))
        self.assertTrue(probe.get('wrong_meshspace_detected'))
        self.assertEqual(probe.get('health_meshspace_id'), 'family-lab')

    def test_snapshot_prefers_live_probe_shell_summary_for_other_mesh(self) -> None:
        self._authenticate()

        manager = self.app.config.get('MESHSPACE_REGISTRY_MANAGER')
        manager.create_meshspace(
            name='Research Lab',
            meshspace_id='research-lab',
            description='Experimental mesh for testing.',
        )
        manager.update_runtime_state('research-lab', 'running', peer_id='peer-research')
        manager.update_shell_summary('research-lab', {
            'active_peer_count': 1,
            'unread_count': 1,
            'mention_count': 0,
            'pending_review_count': 0,
            'attention_count': 1,
            'last_activity_at': '2026-04-03T11:00:00+00:00',
            'last_summary_at': '2026-04-03T11:00:01+00:00',
            'has_recent_activity': True,
            'attention_level': 'active',
        })

        with patch.object(manager, 'probe_meshspace_runtime', return_value={
            'live': True,
            'effective_status': 'running',
            'status_mismatch': False,
            'detected_pid': 4242,
            'launch_url': 'http://127.0.0.1:7800',
            'version': '0.5.51',
            'peer_id': 'peer-research',
            'shell_summary': {
                'active_peer_count': 2,
                'unread_count': 7,
                'mention_count': 1,
                'pending_review_count': 0,
                'attention_count': 8,
                'last_activity_at': '2026-04-03T12:00:00+00:00',
                'last_summary_at': '2026-04-03T12:00:01+00:00',
                'has_recent_activity': True,
                'attention_level': 'active',
            },
        }):
            snapshot_response = self.client.get('/ajax/meshspaces_snapshot')

        self.assertEqual(snapshot_response.status_code, 200)
        payload = snapshot_response.get_json() or {}
        meshes = {item['meshspace_id']: item for item in (payload.get('meshspaces') or [])}
        self.assertEqual(int((meshes['research-lab'].get('unread_count') or 0)), 7)
        self.assertEqual(int((meshes['research-lab'].get('mention_count') or 0)), 1)
        self.assertEqual(str(meshes['research-lab'].get('attention_badge') or ''), '8')

        refreshed = manager.get_meshspace('research-lab')
        refreshed_summary = (refreshed or {}).get('summary') or {}
        self.assertEqual(int(refreshed_summary.get('attention_count') or 0), 8)

    def test_snapshot_overlays_cross_mesh_viewer_attention_without_persisting_it(self) -> None:
        self._authenticate()

        manager = self.app.config.get('MESHSPACE_REGISTRY_MANAGER')
        manager.create_meshspace(
            name='Research Lab',
            meshspace_id='research-lab',
            description='Experimental mesh for testing.',
        )
        manager.update_runtime_state('research-lab', 'running', peer_id='peer-research')
        manager.update_shell_summary('research-lab', {
            'active_peer_count': 2,
            'unread_count': 5,
            'mention_count': 1,
            'pending_review_count': 0,
            'attention_count': 6,
            'last_activity_at': '2026-04-03T12:00:00+00:00',
            'last_summary_at': '2026-04-03T12:00:01+00:00',
            'has_recent_activity': True,
            'attention_level': 'active',
        })
        research = manager.get_meshspace('research-lab') or {}
        self.client.set_cookie(str(research.get('session_cookie_name') or 'canopy_session_research-lab'), 'child-session')

        remote_response = MagicMock()
        remote_response.read.return_value = json.dumps({
            'viewer_specific': True,
            'meshspace_id': 'research-lab',
            'unread_count': 2,
            'mention_count': 1,
            'pending_review_count': 1,
            'attention_count': 4,
        }).encode('utf-8')
        remote_response.__enter__.return_value = remote_response
        remote_response.__exit__.return_value = None

        with patch.object(manager, 'probe_meshspace_runtime', return_value={
            'live': True,
            'effective_status': 'running',
            'status_mismatch': False,
            'detected_pid': 4242,
            'launch_url': 'http://127.0.0.1:7800',
            'version': '0.5.57',
            'peer_id': 'peer-research',
            'shell_summary': {
                'active_peer_count': 2,
                'unread_count': 5,
                'mention_count': 1,
                'pending_review_count': 0,
                'attention_count': 6,
                'last_activity_at': '2026-04-03T12:00:00+00:00',
                'last_summary_at': '2026-04-03T12:00:01+00:00',
                'has_recent_activity': True,
                'attention_level': 'active',
            },
        }), patch('canopy.ui.routes.urlopen', return_value=remote_response) as remote_get:
            snapshot_response = self.client.get('/ajax/meshspaces_snapshot')

        self.assertEqual(snapshot_response.status_code, 200)
        payload = snapshot_response.get_json() or {}
        meshes = {item['meshspace_id']: item for item in (payload.get('meshspaces') or [])}
        self.assertEqual(int((meshes['research-lab'].get('unread_count') or 0)), 2)
        self.assertEqual(int((meshes['research-lab'].get('mention_count') or 0)), 1)
        self.assertEqual(int((meshes['research-lab'].get('pending_review_count') or 0)), 1)
        self.assertEqual(str(meshes['research-lab'].get('attention_badge') or ''), '4')
        self.assertIn('2 unread', str(meshes['research-lab'].get('presence_subline') or ''))

        forwarded_request = remote_get.call_args.args[0]
        self.assertEqual(
            forwarded_request.headers.get('Cookie'),
            f"{research.get('session_cookie_name')}=child-session",
        )

        refreshed = manager.get_meshspace('research-lab')
        refreshed_summary = (refreshed or {}).get('summary') or {}
        self.assertEqual(int(refreshed_summary.get('unread_count') or 0), 5)
        self.assertEqual(int(refreshed_summary.get('attention_count') or 0), 6)

    def test_meshspace_snapshot_uses_warning_badge_when_status_has_no_count(self) -> None:
        self._authenticate()

        manager = self.app.config.get('MESHSPACE_REGISTRY_MANAGER')
        manager.create_meshspace(
            name='Research Lab',
            meshspace_id='research-lab',
            description='Experimental mesh for testing.',
        )
        record = manager.get_meshspace('research-lab')
        self.assertIsNotNone(record)
        record['status'] = 'degraded'
        record['summary'] = {
            'active_peer_count': 0,
            'attention_count': 0,
            'unread_count': 0,
            'mention_count': 0,
            'pending_review_count': 0,
            'last_activity_at': '',
            'last_summary_at': '',
            'has_recent_activity': False,
            'attention_level': 'warning',
        }
        manager._upsert_record(record)

        with patch.object(manager, 'probe_meshspace_runtime', return_value={
            'live': False,
            'effective_status': 'degraded',
            'status_mismatch': False,
            'detected_pid': 0,
            'launch_url': 'http://127.0.0.1:7800',
            'version': '',
        }):
            snapshot_response = self.client.get('/ajax/meshspaces_snapshot')
        self.assertEqual(snapshot_response.status_code, 200)
        payload = snapshot_response.get_json() or {}
        meshes = {item['meshspace_id']: item for item in (payload.get('meshspaces') or [])}
        self.assertEqual(str(meshes['research-lab'].get('attention_badge') or ''), '!')

    def test_workspace_event_publish_refreshes_current_mesh_registry_summary(self) -> None:
        self._authenticate()

        workspace_event_manager = self.app.config['WORKSPACE_EVENT_MANAGER']
        manager = self.app.config['MESHSPACE_REGISTRY_MANAGER']

        with self.db_manager.get_connection() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO users (
                    id, username, public_key, password_hash, display_name,
                    origin_peer, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """,
                ('sender-user', 'sender', 'pk-sender', 'pw-sender', 'Sender', None),
            )
            conn.execute(
                """
                INSERT INTO messages (id, sender_id, recipient_id, content, message_type, metadata)
                VALUES (?, ?, ?, ?, 'text', '{}')
                """,
                ('DM-002', 'sender-user', 'owner-user', 'Runtime event unread'),
            )
            conn.commit()

        workspace_event_manager.emit_event(
            event_type='dm.message.created',
            visibility_scope='private',
            actor_user_id='sender-user',
            target_user_id='owner-user',
            message_id='DM-002',
            payload={'message_id': 'DM-002'},
        )
        time.sleep(0.5)
        record = manager.get_meshspace('family-lab')
        summary = (record or {}).get('summary') or {}
        self.assertGreaterEqual(int(summary.get('unread_count') or 0), 1)

    def test_non_owner_shell_requests_do_not_overwrite_owner_mesh_summary(self) -> None:
        self._authenticate()

        manager = self.app.config['MESHSPACE_REGISTRY_MANAGER']

        with self.db_manager.get_connection() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO users (
                    id, username, public_key, password_hash, display_name,
                    origin_peer, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """,
                ('sender-user', 'sender', 'pk-sender', 'pw-sender', 'Sender', None),
            )
            conn.execute(
                """
                INSERT OR REPLACE INTO users (
                    id, username, public_key, password_hash, display_name,
                    origin_peer, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """,
                ('viewer-user', 'viewer', 'pk-viewer', 'pw-viewer', 'Viewer', None),
            )
            conn.execute(
                """
                INSERT INTO messages (id, sender_id, recipient_id, content, message_type, metadata)
                VALUES (?, ?, ?, ?, 'text', '{}')
                """,
                ('DM-003', 'sender-user', 'owner-user', 'Unread DM for owner summary'),
            )
            conn.commit()

        with self.client.session_transaction() as sess:
            sess['authenticated'] = True
            sess['user_id'] = 'viewer-user'
            sess['username'] = 'viewer'
            sess['display_name'] = 'Viewer'
            sess['_csrf_token'] = 'csrf-mesh'

        response = self.client.get('/channels')
        self.assertEqual(response.status_code, 200)

        record = manager.get_meshspace('family-lab')
        summary = (record or {}).get('summary') or {}
        self.assertGreaterEqual(int(summary.get('unread_count') or 0), 1)

    def test_admin_can_toggle_meshspace_network_isolation(self) -> None:
        self._authenticate()

        manager = self.app.config.get('MESHSPACE_REGISTRY_MANAGER')
        manager.create_meshspace(
            name='Research Lab',
            meshspace_id='research-lab',
            description='Experimental mesh for testing.',
        )

        enable_response = self.client.post(
            '/meshes/research-lab/network',
            data={
                'csrf_token': 'csrf-mesh',
                'network_mode': 'enabled',
            },
            follow_redirects=True,
        )
        self.assertEqual(enable_response.status_code, 200)
        record = manager.get_meshspace('research-lab')
        self.assertIsNotNone(record)
        self.assertFalse(record.get('network_quarantined'))
        enable_body = enable_response.get_data(as_text=True)
        self.assertIn('Isolate network after restart', enable_body)

        isolate_response = self.client.post(
            '/meshes/research-lab/network',
            data={
                'csrf_token': 'csrf-mesh',
                'network_mode': 'quarantined',
            },
            follow_redirects=True,
        )
        self.assertEqual(isolate_response.status_code, 200)
        record = manager.get_meshspace('research-lab')
        self.assertIsNotNone(record)
        self.assertTrue(record.get('network_quarantined'))
        isolate_body = isolate_response.get_data(as_text=True)
        self.assertIn('Enable networking after restart', isolate_body)

    def test_stopped_meshspace_port_update_preserves_identity_and_updates_launch_env(self) -> None:
        manager = self.app.config.get('MESHSPACE_REGISTRY_MANAGER')
        manager.create_meshspace(
            name='Research Lab',
            meshspace_id='research-lab',
            description='Experimental mesh for testing.',
            http_port=18700,
            mesh_port=18701,
            discovery_port=18702,
        )
        before = manager.get_meshspace('research-lab')
        self.assertIsNotNone(before)

        with patch('canopy.core.meshspaces._listener_pid_for_port', return_value=0), \
             patch('canopy.core.meshspaces._port_accepts_connections', return_value=False), \
             patch('canopy.core.meshspaces._http_json', return_value={}):
            updated = manager.update_meshspace_ports('research-lab', http_port=18800)

        self.assertIsNotNone(updated)
        self.assertEqual(updated.get('http_port'), 18800)
        self.assertEqual(updated.get('mesh_port'), 18801)
        self.assertEqual(updated.get('discovery_port'), 18802)
        self.assertEqual(updated.get('meshspace_id'), before.get('meshspace_id'))
        self.assertEqual(updated.get('data_dir'), before.get('data_dir'))
        self.assertEqual(updated.get('database_path'), before.get('database_path'))
        self.assertEqual(updated.get('session_cookie_name'), before.get('session_cookie_name'))
        self.assertEqual(updated.get('launch_url'), 'http://127.0.0.1:18800')

        launch_env = manager.launch_environment('research-lab')
        self.assertEqual(launch_env.get('CANOPY_PORT'), '18800')
        self.assertEqual(launch_env.get('CANOPY_MESH_PORT'), '18801')
        self.assertEqual(launch_env.get('CANOPY_DISCOVERY_PORT'), '18802')
        self.assertEqual(launch_env.get('CANOPY_MESHSPACE_ID'), 'research-lab')

    def test_stopped_meshspace_port_update_rejects_registry_conflict(self) -> None:
        manager = self.app.config.get('MESHSPACE_REGISTRY_MANAGER')
        manager.create_meshspace(
            name='Research Lab',
            meshspace_id='research-lab',
            http_port=18700,
            mesh_port=18701,
            discovery_port=18702,
        )
        manager.create_meshspace(
            name='Studio Lab',
            meshspace_id='studio-lab',
            http_port=18800,
            mesh_port=18801,
            discovery_port=18802,
        )

        with self.assertRaises(ValueError) as ctx:
            manager.update_meshspace_ports('studio-lab', http_port=18700)
        self.assertIn("Research Lab", str(ctx.exception))
        self.assertIn("18700", str(ctx.exception))

    def test_stopped_meshspace_port_update_rejects_live_runtime(self) -> None:
        manager = self.app.config.get('MESHSPACE_REGISTRY_MANAGER')
        manager.create_meshspace(
            name='Research Lab',
            meshspace_id='research-lab',
            http_port=18700,
            mesh_port=18701,
            discovery_port=18702,
        )
        manager.update_runtime_state('research-lab', 'running', peer_id='peer-research')

        with patch.object(manager, 'probe_meshspace_runtime', return_value={
            'live': True,
            'effective_status': 'running',
            'detected_pid': 4242,
        }):
            with self.assertRaises(ValueError) as ctx:
                manager.update_meshspace_ports('research-lab', http_port=18800)
        self.assertIn('Stop this meshspace before changing its ports', str(ctx.exception))

    def test_stopped_meshspace_port_update_rejects_host_aware_listener_conflict(self) -> None:
        manager = self.app.config.get('MESHSPACE_REGISTRY_MANAGER')
        manager.create_meshspace(
            name='Research Lab',
            meshspace_id='research-lab',
            http_port=18700,
            mesh_port=18701,
            discovery_port=18702,
        )

        def _fake_listener(port, host=None):
            self.assertEqual(host, '127.0.0.1')
            return 4242 if int(port) == 18801 else 0

        with patch('canopy.core.meshspaces._listener_pid_for_port', side_effect=_fake_listener), \
             patch('canopy.core.meshspaces._port_accepts_connections', return_value=False), \
             patch('canopy.core.meshspaces._http_json', return_value={}):
            with self.assertRaises(ValueError) as ctx:
                manager.update_meshspace_ports('research-lab', http_port=18800)
        self.assertIn('mesh:18801 pid:4242', str(ctx.exception))

    def test_admin_can_update_stopped_meshspace_ports_from_detail(self) -> None:
        self._authenticate()

        manager = self.app.config.get('MESHSPACE_REGISTRY_MANAGER')
        manager.create_meshspace(
            name='Research Lab',
            meshspace_id='research-lab',
            description='Experimental mesh for testing.',
            http_port=18700,
            mesh_port=18701,
            discovery_port=18702,
        )

        with patch('canopy.core.meshspaces._listener_pid_for_port', return_value=0), \
             patch('canopy.core.meshspaces._port_accepts_connections', return_value=False), \
             patch('canopy.core.meshspaces._http_json', return_value={}):
            response = self.client.post(
                '/meshes/research-lab/ports',
                data={
                    'csrf_token': 'csrf-mesh',
                    'http_port': '18800',
                },
                follow_redirects=True,
            )

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn('Updated', body)
        self.assertIn('18800', body)
        self.assertIn('18801', body)
        self.assertIn('18802', body)
        self.assertIn('/meshes/research-lab/ports', body)

        record = manager.get_meshspace('research-lab')
        self.assertEqual(record.get('http_port'), 18800)
        self.assertEqual(record.get('mesh_port'), 18801)
        self.assertEqual(record.get('discovery_port'), 18802)

    def test_admin_can_edit_meshspace_identity_metadata(self) -> None:
        self._authenticate()

        response = self.client.post(
            '/meshes/family-lab/edit',
            data={
                'csrf_token': 'csrf-mesh',
                'name': 'Family HQ',
                'description': 'Private home coordination.',
                'icon': 'bi-house-heart',
                'accent': 'amber',
            },
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn('Family HQ', body)
        self.assertIn('Private home coordination.', body)

        manager = self.app.config.get('MESHSPACE_REGISTRY_MANAGER')
        record = manager.get_meshspace('family-lab')
        self.assertIsNotNone(record)
        self.assertEqual(record.get('name'), 'Family HQ')
        self.assertEqual(record.get('description'), 'Private home coordination.')
        self.assertEqual(record.get('icon'), 'bi-house-heart')
        self.assertEqual(record.get('accent'), 'amber')

        identity = self.client.get('/api/v1/mesh/identity')
        self.assertEqual(identity.status_code, 200)
        identity_payload = identity.get_json() or {}
        self.assertEqual((identity_payload.get('meshspace') or {}).get('name'), 'Family HQ')

    def test_admin_can_upload_and_serve_meshspace_avatar(self) -> None:
        self._authenticate()

        response = self.client.post(
            '/meshes/family-lab/edit',
            data={
                'csrf_token': 'csrf-mesh',
                'name': 'Family Lab',
                'description': 'Private home coordination.',
                'icon': 'bi-house-heart',
                'accent': 'emerald',
                'avatar': (io.BytesIO(_PNG_1X1), 'family.png'),
            },
            content_type='multipart/form-data',
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn('Use crest instead', body)
        self.assertIn('/meshes/family-lab/avatar', body)

        manager = self.app.config.get('MESHSPACE_REGISTRY_MANAGER')
        record = manager.get_meshspace('family-lab')
        self.assertTrue(record.get('avatar_path'))

        avatar_response = self.client.get('/meshes/family-lab/avatar')
        self.assertEqual(avatar_response.status_code, 200)
        self.assertEqual(avatar_response.mimetype, 'image/png')

    def test_meshspace_registry_persists_pid_for_supervised_runtime(self) -> None:
        manager = self.app.config.get('MESHSPACE_REGISTRY_MANAGER')
        record = manager.get_meshspace('family-lab')
        self.assertIsNotNone(record)
        record['pid'] = 4242
        record['status'] = 'running'
        manager._upsert_record(record)
        refreshed = manager.get_meshspace('family-lab')
        self.assertEqual(int((refreshed or {}).get('pid') or 0), 4242)

    def test_admin_can_request_restart_for_supervised_child_meshspace(self) -> None:
        self._authenticate()

        manager = self.app.config.get('MESHSPACE_REGISTRY_MANAGER')
        manager.create_meshspace(
            name='Research Lab',
            meshspace_id='research-lab',
            description='Experimental mesh for testing.',
        )
        manager.update_runtime_state('research-lab', 'running', peer_id='peer-research')

        with patch.object(manager, 'restart_meshspace', return_value={
            **manager.get_meshspace('research-lab'),
            'name': 'Research Lab',
            'pid': 4242,
        }) as restart_mock:
            response = self.client.post(
                '/meshes/research-lab/restart',
                data={'csrf_token': 'csrf-mesh'},
                follow_redirects=True,
            )
        self.assertEqual(response.status_code, 200)
        self.assertIn('Restarting meshspace', response.get_data(as_text=True))
        restart_mock.assert_called_once_with('research-lab')

    def test_meshes_page_uses_live_probe_for_stale_runtime_controls(self) -> None:
        self._authenticate()

        manager = self.app.config.get('MESHSPACE_REGISTRY_MANAGER')
        manager.create_meshspace(
            name='Research Lab',
            meshspace_id='research-lab',
            description='Experimental mesh for testing.',
        )

        with patch.object(manager, 'probe_meshspace_runtime', return_value={
            'live': True,
            'effective_status': 'running',
            'status_mismatch': True,
            'detected_pid': 4242,
            'version': '0.5.42',
            'launch_url': 'http://127.0.0.1:7800',
        }):
            response = self.client.get('/meshes')
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn('A live process was found on this mesh', body)
        self.assertIn('Controls now reflect the live state', body)
        self.assertIn('Refresh state', body)
        self.assertIn('/meshes/research-lab/stop', body)
        self.assertIn('/meshes/research-lab/restart', body)

    def test_admin_can_request_restart_for_current_meshspace(self) -> None:
        self._authenticate()

        class _NoopThread:
            def __init__(self, target=None, daemon=None):
                self.target = target
                self.daemon = daemon

            def start(self):
                return None

        with patch('canopy.ui.routes.schedule_self_restart', return_value=999) as restart_mock, \
             patch('canopy.ui.routes.threading.Thread', _NoopThread):
            response = self.client.post(
                '/meshes/family-lab/restart',
                data={'csrf_token': 'csrf-mesh'},
                follow_redirects=False,
            )
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn('Restarting Family Lab', body)
        restart_mock.assert_called_once()

    def test_admin_can_refresh_meshspace_runtime_state(self) -> None:
        self._authenticate()

        manager = self.app.config.get('MESHSPACE_REGISTRY_MANAGER')
        manager.create_meshspace(
            name='Research Lab',
            meshspace_id='research-lab',
            description='Experimental mesh for testing.',
        )

        with patch.object(manager, 'reconcile_meshspace_runtime', return_value={
            **manager.get_meshspace('research-lab'),
            'name': 'Research Lab',
        }) as reconcile_mock, patch.object(manager, 'probe_meshspace_runtime', return_value={'live': True}):
            response = self.client.post(
                '/meshes/research-lab/refresh',
                data={'csrf_token': 'csrf-mesh'},
                follow_redirects=True,
            )
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn('Refreshed meshspace', body)
        self.assertIn('assigned ports', body)
        reconcile_mock.assert_called_once_with('research-lab')

    def test_meshspace_open_shows_recovery_surface_when_mesh_is_down(self) -> None:
        self._authenticate()

        manager = self.app.config.get('MESHSPACE_REGISTRY_MANAGER')
        manager.create_meshspace(
            name='Research Lab',
            meshspace_id='research-lab',
            description='Experimental mesh for testing.',
            http_port=18700,
            mesh_port=18701,
            discovery_port=18702,
        )

        response = self.client.get('/meshes/research-lab/open')
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn('Research Lab is not running', body)
        self.assertIn('Start mesh', body)
        self.assertIn('/meshes/research-lab/start', body)
        self.assertIn('Open was blocked because no live runtime was detected', body)
        self.assertIn('Try direct open anyway', body)

    def test_meshspace_open_redirects_when_mesh_is_live(self) -> None:
        self._authenticate()

        manager = self.app.config.get('MESHSPACE_REGISTRY_MANAGER')
        manager.create_meshspace(
            name='Research Lab',
            meshspace_id='research-lab',
            description='Experimental mesh for testing.',
        )

        with patch.object(manager, 'probe_meshspace_runtime', return_value={
            'live': True,
            'effective_status': 'running',
            'status_mismatch': False,
            'detected_pid': 4242,
            'launch_url': 'http://127.0.0.1:7800',
            'version': '0.5.43',
        }):
            response = self.client.get('/meshes/research-lab/open', follow_redirects=False)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers.get('Location'), 'http://localhost:7800/login')

    def test_meshspace_direct_url_uses_request_host_for_loopback_launch_url(self) -> None:
        from canopy.ui.routes import _meshspace_direct_url

        target = _meshspace_direct_url(
            {
                'meshspace_id': 'research-lab',
                'launch_url': 'http://127.0.0.1:7800',
            },
            current_meshspace_id='family-lab',
            request_host='198.51.100.77',
        )
        self.assertEqual(target, 'http://198.51.100.77:7800/login')

    def test_meshspace_open_shows_stale_runtime_copy_when_registry_is_stale(self) -> None:
        self._authenticate()

        manager = self.app.config.get('MESHSPACE_REGISTRY_MANAGER')
        manager.create_meshspace(
            name='Research Lab',
            meshspace_id='research-lab',
            description='Experimental mesh for testing.',
        )
        record = manager.get_meshspace('research-lab')
        record['status'] = 'running'
        manager._upsert_record(record)

        with patch.object(manager, 'probe_meshspace_runtime', return_value={
            'live': False,
            'effective_status': 'stopped',
            'status_mismatch': True,
            'detected_pid': 0,
            'launch_url': 'http://127.0.0.1:7800',
            'version': '',
        }):
            response = self.client.get('/meshes/research-lab/open')
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn('no live process was found on its assigned ports', body)
        self.assertIn('Refresh state', body)
        self.assertIn('Try direct open anyway', body)

    def test_meshspace_open_explains_when_different_mesh_answers_on_recorded_port(self) -> None:
        self._authenticate()

        manager = self.app.config.get('MESHSPACE_REGISTRY_MANAGER')
        manager.create_meshspace(
            name='Research Lab',
            meshspace_id='research-lab',
            description='Experimental mesh for testing.',
        )

        with patch.object(manager, 'probe_meshspace_runtime', return_value={
            'live': False,
            'effective_status': 'stopped',
            'status_mismatch': True,
            'detected_pid': 4242,
            'launch_url': 'http://127.0.0.1:7770',
            'version': '0.5.63',
            'peer_id': 'peer-family',
            'health_meshspace_id': 'family-lab',
            'health_meshspace_match': False,
            'wrong_meshspace_detected': True,
            'shell_summary': {},
        }):
            response = self.client.get('/meshes/research-lab/open')
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn('data-mesh-open-reason="wrong-mesh"', body)
        self.assertIn('family-lab', body)
        self.assertNotIn('Try direct open anyway', body)

    def test_meshspace_open_shows_crashed_runtime_copy(self) -> None:
        self._authenticate()

        manager = self.app.config.get('MESHSPACE_REGISTRY_MANAGER')
        manager.create_meshspace(
            name='Research Lab',
            meshspace_id='research-lab',
            description='Experimental mesh for testing.',
        )

        with patch.object(manager, 'probe_meshspace_runtime', return_value={
            'live': False,
            'effective_status': 'crashed',
            'status_mismatch': False,
            'detected_pid': 0,
            'launch_url': 'http://127.0.0.1:7800',
            'version': '',
        }):
            response = self.client.get('/meshes/research-lab/open')
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn('data-mesh-open-reason="crashed"', body)
        self.assertIn('Check the launch log', body)

    def test_meshspace_open_shows_restarting_runtime_copy(self) -> None:
        self._authenticate()

        manager = self.app.config.get('MESHSPACE_REGISTRY_MANAGER')
        manager.create_meshspace(
            name='Research Lab',
            meshspace_id='research-lab',
            description='Experimental mesh for testing.',
        )

        with patch.object(manager, 'probe_meshspace_runtime', return_value={
            'live': False,
            'effective_status': 'starting',
            'status_mismatch': False,
            'detected_pid': 0,
            'launch_url': 'http://127.0.0.1:7800',
            'version': '',
        }):
            response = self.client.get('/meshes/research-lab/open')
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn('data-mesh-open-reason="restarting"', body)
        self.assertIn('currently starting up or shutting down', body)

    def test_port_overlap_warning_shown_on_meshes_and_detail_pages(self) -> None:
        self._authenticate()

        manager = self.app.config.get('MESHSPACE_REGISTRY_MANAGER')
        current_record = manager.get_meshspace('family-lab')
        self.assertIsNotNone(current_record)
        manager.create_meshspace(
            name='Research Lab',
            meshspace_id='research-lab',
            description='Experimental mesh for testing.',
            http_port=int(current_record.get('http_port') or 0),
            mesh_port=7801,
            discovery_port=7802,
        )

        response = self.client.get('/meshes')
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn('data-meshspace-overlap-banner', body)
        self.assertIn('Port conflict', body)
        self.assertIn('Shares a runtime port with another mesh entry', body)

        detail_response = self.client.get('/meshes/research-lab')
        self.assertEqual(detail_response.status_code, 200)
        detail_body = detail_response.get_data(as_text=True)
        self.assertIn('data-meshspace-overlap-callout', detail_body)
        self.assertIn('Family Lab', detail_body)

    def test_update_runtime_state_clears_live_presence_when_mesh_stops(self) -> None:
        manager = self.app.config.get('MESHSPACE_REGISTRY_MANAGER')
        manager.update_shell_summary(
            'family-lab',
            {
                'active_peer_count': 4,
                'attention_count': 2,
                'unread_count': 2,
                'has_recent_activity': True,
                'attention_level': 'active',
            },
        )

        updated = manager.update_runtime_state('family-lab', 'stopped')
        self.assertIsNotNone(updated)
        summary = (updated or {}).get('summary') or {}
        self.assertEqual(int(summary.get('active_peer_count') or 0), 0)
        self.assertFalse(bool(summary.get('has_recent_activity')))
        self.assertEqual(int(summary.get('unread_count') or 0), 2)
        self.assertIn(str(summary.get('attention_level') or ''), {'active', 'warning'})

    def test_meshspace_creation_rejects_overlapping_roots(self) -> None:
        manager = self.app.config.get('MESHSPACE_REGISTRY_MANAGER')
        with self.assertRaises(ValueError):
            manager.create_meshspace(
                name='Nested Mesh',
                meshspace_id='nested-mesh',
                data_dir=str(self.mesh_root / 'nested'),
            )

    def test_sidebar_summary_unread_matches_meshspace_notification_unread_count(self) -> None:
        self._authenticate()

        with self.db_manager.get_connection() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO users (
                    id, username, public_key, password_hash, display_name,
                    origin_peer, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """,
                ('sender-parity', 'sender_parity', 'pk-parity', 'pw-parity', 'Sender Parity', None),
            )
            conn.execute(
                "INSERT INTO messages (id, sender_id, recipient_id, content, message_type, metadata)"
                " VALUES (?, ?, ?, ?, 'text', '{}')",
                ('PAR-DM-1', 'sender-parity', 'owner-user', 'Parity DM 1'),
            )
            conn.execute(
                "INSERT INTO messages (id, sender_id, recipient_id, content, message_type, metadata)"
                " VALUES (?, ?, ?, ?, 'text', '{}')",
                ('PAR-DM-2', 'sender-parity', 'owner-user', 'Parity DM 2'),
            )
            conn.commit()

        sidebar_summary = (self.client.get('/ajax/sidebar_attention_summary').get_json() or {}).get('summary') or {}
        sidebar_total = int(sidebar_summary.get('total') or 0)
        self.assertEqual(
            sidebar_total,
            int(sidebar_summary.get('messages') or 0)
            + int(sidebar_summary.get('channels') or 0)
            + int(sidebar_summary.get('feed') or 0),
        )

        payload = self.client.get('/ajax/meshspaces_snapshot').get_json() or {}
        meshes = {item['meshspace_id']: item for item in (payload.get('meshspaces') or [])}
        current_mesh = meshes.get('family-lab') or {}
        self.assertEqual(int(current_mesh.get('unread_count') or 0), sidebar_total)


class LegacyMeshspaceAdoptionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.legacy_root = Path(self.tempdir.name) / 'legacy-current'
        self.registry_root = Path(self.tempdir.name) / 'registry'

        self.env_patcher = patch.dict(
            os.environ,
            {
                'CANOPY_TESTING': 'true',
                'CANOPY_DISABLE_MESH': 'true',
                'CANOPY_DATA_DIR': str(self.legacy_root),
                'CANOPY_MESHSPACE_REGISTRY_ROOT': str(self.registry_root),
                'CANOPY_SECRET_KEY': 'legacy-secret',
            },
            clear=False,
        )
        self.env_patcher.start()
        self.addCleanup(self.env_patcher.stop)

        self.checkpoint_patcher = patch(
            'canopy.core.database.DatabaseManager._start_checkpoint_thread',
            lambda self: None,
        )
        self.checkpoint_patcher.start()
        self.addCleanup(self.checkpoint_patcher.stop)

        self.logging_patcher = patch(
            'canopy.core.app.setup_logging',
            lambda debug=False: None,
        )
        self.logging_patcher.start()
        self.addCleanup(self.logging_patcher.stop)

        self.p2p_patcher = patch(
            'canopy.core.app.P2PNetworkManager',
            _FakeP2PNetworkManager,
        )
        self.p2p_patcher.start()
        self.addCleanup(self.p2p_patcher.stop)

        def _fake_apply_device_paths(config):
            self.legacy_root.mkdir(parents=True, exist_ok=True)
            config.storage.data_dir = str(self.legacy_root)
            config.storage.database_path = str(self.legacy_root / 'canopy.db')
            config.secret_key = 'legacy-secret'

        self.device_path_patcher = patch(
            'canopy.core.config._apply_device_paths',
            _fake_apply_device_paths,
        )
        self.device_path_patcher.start()
        self.addCleanup(self.device_path_patcher.stop)

        self.app = create_app()
        self.client = self.app.test_client()
        self.db_manager = self.app.config['DB_MANAGER']

        with self.db_manager.get_connection() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO users (
                    id, username, public_key, password_hash, display_name,
                    origin_peer, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """,
                ('owner-user', 'owner', 'pk-owner', 'pw-owner', 'Owner', None),
            )
            conn.commit()
        self.db_manager.set_instance_owner_user_id('owner-user')

    def _authenticate(self) -> None:
        with self.client.session_transaction() as sess:
            sess['authenticated'] = True
            sess['user_id'] = 'owner-user'
            sess['username'] = 'owner'
            sess['display_name'] = 'Owner'
            sess['_csrf_token'] = 'csrf-mesh'

    def test_legacy_runtime_is_adopted_and_prompts_for_naming(self) -> None:
        config = self.app.config['CANOPY_CONFIG']
        self.assertFalse(config.meshspace.enabled)
        self.assertEqual(config.meshspace.runtime_mode, 'legacy-default')
        self.assertEqual(config.meshspace.name, 'Default Mesh')

        manager = self.app.config.get('MESHSPACE_REGISTRY_MANAGER')
        record = manager.get_meshspace(config.meshspace.meshspace_id)
        self.assertIsNotNone(record)
        self.assertTrue(record.get('is_default'))
        self.assertEqual(record.get('data_dir'), str(self.legacy_root))

        self._authenticate()
        response = self.client.get('/meshes')
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn('Default Mesh', body)
        self.assertIn('Rename the default mesh before adding more so each world stays clear.', body)
        self.assertIn('Needs name', body)

    def test_invite_generation_uses_device_profile_as_peer_identity_without_bloating_invite_payload(self) -> None:
        self._authenticate()
        manager = self.app.config.get('MESHSPACE_REGISTRY_MANAGER')
        config = self.app.config['CANOPY_CONFIG']
        meshspace_id = config.meshspace.meshspace_id
        manager.update_meshspace_metadata(meshspace_id, name='Windy Mesh')
        manager.set_meshspace_avatar(
            meshspace_id,
            _PNG_1X1,
            filename='windy.png',
            content_type='image/png',
        )

        fake_invite = SimpleNamespace(
            encode=lambda: 'canopy:test',
            peer_id='peer-mesh-test',
            endpoints=['ws://192.168.1.12:7771'],
            meshspace_id=meshspace_id,
            mesh_name='Windy Mesh',
            meshspace_fingerprint='ABCD-1234',
            to_dict=lambda: {'mn': 'Windy Mesh'},
        )
        with patch('canopy.network.invite.generate_invite', return_value=fake_invite) as generate_invite_mock, \
             patch('canopy.core.device.get_device_profile', return_value={'display_name': 'Alvin Node', 'icon': 'bi-server'}), \
             patch('canopy.core.device.get_device_label', return_value='Alvin Device'):
            response = self.client.get('/api/v1/p2p/invite')
        self.assertEqual(response.status_code, 200)
        payload = response.get_json() or {}
        self.assertEqual(payload.get('meshspace_name'), 'Windy Mesh')
        self.assertEqual(payload.get('peer_label'), 'Alvin Node')
        self.assertEqual(payload.get('instance_label'), 'Alvin Node')
        self.assertEqual(generate_invite_mock.call_args.kwargs.get('mesh_name'), 'Windy Mesh')
        self.assertEqual(generate_invite_mock.call_args.kwargs.get('peer_label'), 'Alvin Node')
        self.assertEqual(generate_invite_mock.call_args.kwargs.get('instance_label'), 'Alvin Node')
        self.assertEqual(generate_invite_mock.call_args.kwargs.get('peer_icon'), 'bi-server')
        self.assertIsNone(generate_invite_mock.call_args.kwargs.get('meshspace_avatar_b64'))
        self.assertIsNone(generate_invite_mock.call_args.kwargs.get('meshspace_avatar_mime'))
        self.assertIsNone(generate_invite_mock.call_args.kwargs.get('meshspace_id_aliases'))

    def test_invite_generation_keeps_live_ws_scheme_until_tls_listener_restarts(self) -> None:
        self._authenticate()
        config = self.app.config['CANOPY_CONFIG']
        config.network.enable_tls = True
        config.network.transport_security_mode = 'self_signed'

        fake_invite = SimpleNamespace(
            encode=lambda: 'canopy:test',
            peer_id='peer-mesh-test',
            endpoints=['ws://192.168.1.12:7771'],
            meshspace_id=config.meshspace.meshspace_id,
            mesh_name=config.meshspace.name,
            meshspace_fingerprint='ABCD-1234',
            to_dict=lambda: {'mn': config.meshspace.name},
        )
        with patch('canopy.network.invite.generate_invite', return_value=fake_invite) as generate_invite_mock:
            response = self.client.get('/api/v1/p2p/invite')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(generate_invite_mock.call_args.kwargs.get('endpoint_scheme'), 'ws')

    def test_connect_page_uses_device_profile_as_primary_peer_hint(self) -> None:
        self._authenticate()
        fake_invite = SimpleNamespace(
            encode=lambda: 'canopy:test',
            peer_id='peer-mesh-test',
            endpoints=['ws://192.168.1.12:7771'],
        )
        with patch('canopy.core.device.get_device_profile', return_value={'display_name': 'Alvin Node'}), \
             patch('canopy.core.device.get_device_label', return_value='Alvin Device'), \
             patch('canopy.network.invite.generate_invite', return_value=fake_invite):
            response = self.client.get('/connect')

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn('Alvin Node', body)
        self.assertIn('Node hint Alvin Node', body)

    def test_invite_preview_probe_fetches_remote_mesh_identity_from_advertised_endpoint(self) -> None:
        self._authenticate()

        invite = (
            'canopy:'
            'eyJ2IjoxLCJwaWQiOiJwZWVyLXdpbmR5IiwiZXBrIjoiMTExMTExMTExMTExMTExMTExMTExMTExMTExMTEiLCJ4cGsiOiIxMTExMTExMTExMTExMTExMTExMTExMTExMTExMTExIiwiZXAiOlsid3M6Ly8xOTIuMTY4LjEuMTU5Ojc3NzEiXSwibW4iOiJHb2xkR2FuZyIsIm1pZCI6ImdvbGRnYW5nIiwibWYiOiI5M0Q4LTkzMTQifQ'
        )

        class _FakeResponse:
            def __init__(self, payload, url):
                self._payload = payload
                self.status = 200
                self.headers = {'Content-Type': 'application/json'}
                self._url = url

            def read(self, *_args, **_kwargs):
                return json.dumps(self._payload).encode('utf-8')

            def geturl(self):
                return self._url

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

        remote_payload = {
            'meshspace': {
                'meshspace_id': 'goldgang',
                'meshspace_id_aliases': ['legacy-gold'],
                'name': 'GoldGang',
                'meshspace_fingerprint': '93D8-9314',
                'meshspace_avatar_b64': base64.b64encode(_PNG_1X1).decode('ascii'),
                'meshspace_avatar_mime': 'image/png',
            },
            'peer': {
                'peer_id': 'peer-windy',
                'peer_label': 'Maddog',
                'peer_avatar_b64': base64.b64encode(_PNG_1X1).decode('ascii'),
                'peer_avatar_mime': 'image/png',
                'instance_label': 'WINDYLAPTOP',
                'peer_icon': 'bi-laptop',
            },
            'version': '0.6.18',
            'ready': True,
        }
        remote_url = 'http://192.168.1.159:7770/api/v1/mesh/identity'
        with patch('canopy.api.routes.urlopen', return_value=_FakeResponse(remote_payload, remote_url)) as remote_get:
            response = self.client.post(
                '/api/v1/p2p/invite/preview',
                json={'invite_code': invite},
                headers={'X-CSRFToken': 'csrf-mesh'},
            )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json() or {}
        self.assertTrue(payload.get('ok'))
        self.assertEqual(payload.get('meshspace_name'), 'GoldGang')
        self.assertEqual(payload.get('meshspace_id'), 'goldgang')
        self.assertTrue(payload.get('meshspace_avatar_b64'))
        self.assertEqual(payload.get('peer_label'), 'Maddog')
        self.assertTrue(payload.get('peer_avatar_b64'))
        self.assertEqual(payload.get('instance_label'), 'WINDYLAPTOP')
        self.assertEqual(payload.get('peer_icon'), 'bi-laptop')
        self.assertEqual(payload.get('source_endpoint'), 'ws://192.168.1.159:7771')
        self.assertEqual(payload.get('source_url'), remote_url)
        request_obj = remote_get.call_args.args[0]
        self.assertEqual(request_obj.full_url, remote_url)

    def test_invite_preview_probe_tolerates_remote_fetch_failure(self) -> None:
        from urllib.error import URLError

        self._authenticate()
        invite = (
            'canopy:'
            'eyJ2IjoxLCJwaWQiOiJwZWVyLXdpbmR5IiwiZXBrIjoiMTExMTExMTExMTExMTExMTExMTExMTExMTExMTEiLCJ4cGsiOiIxMTExMTExMTExMTExMTExMTExMTExMTExMTExMTExIiwiZXAiOlsid3M6Ly8xOTIuMTY4LjEuMTU5Ojc3NzEiXSwibW4iOiJHb2xkR2FuZyIsIm1pZCI6ImdvbGRnYW5nIiwibWYiOiI5M0Q4LTkzMTQifQ'
        )
        with patch('canopy.api.routes.urlopen', side_effect=URLError('connection refused')):
            response = self.client.post(
                '/api/v1/p2p/invite/preview',
                json={'invite_code': invite},
                headers={'X-CSRFToken': 'csrf-mesh'},
            )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json() or {}
        self.assertFalse(payload.get('ok'))
        self.assertEqual(payload.get('status'), 'unavailable')

    def test_admin_can_promote_legacy_meshspace_to_explicit_identity(self) -> None:
        self._authenticate()
        config = self.app.config['CANOPY_CONFIG']
        old_meshspace_id = config.meshspace.meshspace_id

        class _NoopThread:
            def __init__(self, target=None, daemon=None):
                self.target = target
                self.daemon = daemon

            def start(self):
                return None

        with patch('canopy.ui.routes.schedule_self_restart', return_value=999) as restart_mock, \
             patch('canopy.ui.routes.threading.Thread', _NoopThread):
            response = self.client.post(
                f'/meshes/{old_meshspace_id}/promote',
                data={
                    'csrf_token': 'csrf-mesh',
                    'stable_meshspace_id': 'windy-mesh',
                    'stable_meshspace_name': 'Windy Mesh',
                },
                follow_redirects=False,
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn('Restarting Windy Mesh', response.get_data(as_text=True))
        restart_mock.assert_called_once()

        manager = self.app.config.get('MESHSPACE_REGISTRY_MANAGER')
        promoted = manager.get_meshspace('windy-mesh')
        self.assertIsNotNone(promoted)
        self.assertEqual(promoted.get('runtime_mode'), 'meshspace')
        self.assertIn(old_meshspace_id, promoted.get('meshspace_id_aliases') or [])
        self.assertIsNone(manager.get_meshspace(old_meshspace_id))

        self.assertTrue(config.meshspace.enabled)
        self.assertEqual(config.meshspace.meshspace_id, 'windy-mesh')
        self.assertIn(old_meshspace_id, config.meshspace.meshspace_id_aliases)

    def test_promoted_mesh_accepts_old_alias_invite_without_cross_mesh_confirmation(self) -> None:
        self._authenticate()
        config = self.app.config['CANOPY_CONFIG']
        old_meshspace_id = config.meshspace.meshspace_id

        class _NoopThread:
            def __init__(self, target=None, daemon=None):
                self.target = target
                self.daemon = daemon

            def start(self):
                return None

        with patch('canopy.ui.routes.schedule_self_restart', return_value=999), \
             patch('canopy.ui.routes.threading.Thread', _NoopThread):
            promote_response = self.client.post(
                f'/meshes/{old_meshspace_id}/promote',
                data={
                    'csrf_token': 'csrf-mesh',
                    'stable_meshspace_id': 'windy-mesh',
                    'stable_meshspace_name': 'Windy Mesh',
                },
                follow_redirects=False,
            )
        self.assertEqual(promote_response.status_code, 200)

        invite = InviteCode(
            peer_id='peer-windy-remote',
            ed25519_public_key_b58='11111111111111111111111111111111',
            x25519_public_key_b58='11111111111111111111111111111111',
            endpoints=['ws://192.168.1.80:7771'],
            mesh_name='Windy Mesh',
            meshspace_id=old_meshspace_id,
        )
        with patch('canopy.network.invite.InviteCode.decode', return_value=invite), \
             patch('canopy.network.invite.import_invite', return_value={'peer_id': invite.peer_id, 'endpoints': invite.endpoints}):
            response = self.client.post(
                '/api/v1/p2p/invite/import',
                json={'invite_code': 'canopy:test'},
                headers={'X-CSRFToken': 'csrf-mesh'},
            )
        self.assertEqual(response.status_code, 200)
        payload = response.get_json() or {}
        self.assertNotEqual(payload.get('status'), 'confirmation_required')


class MeshspaceRegistryManagerIsolationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.registry_root = Path(self.tempdir.name) / 'registry'

    def test_custom_registry_root_is_used_for_registry_writes(self) -> None:
        manager = MeshspaceRegistryManager(registry_root=self.registry_root)
        manager.create_meshspace(name='Research Lab', meshspace_id='research-lab')

        registry_path = self.registry_root / 'registry.json'
        self.assertTrue(registry_path.exists())

        payload = json.loads(registry_path.read_text())
        mesh_ids = {item.get('meshspace_id') for item in payload.get('meshspaces', [])}
        self.assertIn('research-lab', mesh_ids)

    def test_testing_legacy_runtime_uses_data_local_registry_root(self) -> None:
        from canopy.core.config import Config

        legacy_root = Path(self.tempdir.name) / 'legacy-current'
        with patch.dict(
            os.environ,
            {
                'CANOPY_TESTING': 'true',
                'CANOPY_DATA_DIR': str(legacy_root),
                'CANOPY_DATABASE_PATH': str(legacy_root / 'canopy.db'),
                'CANOPY_SECRET_KEY': 'test-secret',
            },
            clear=False,
        ):
            config = Config.from_env()
        self.assertEqual(config.meshspace.registry_root, str(legacy_root / '.meshspaces-registry'))

    def test_list_meshspaces_prunes_stopped_temp_legacy_records(self) -> None:
        manager = MeshspaceRegistryManager(registry_root=self.registry_root)
        self.registry_root.mkdir(parents=True, exist_ok=True)
        registry_path = self.registry_root / 'registry.json'
        registry_path.write_text(json.dumps({
            'version': 1,
            'meshspaces': [
                {
                    'meshspace_id': 'legacy-device-temp',
                    'name': 'Default Mesh',
                    'runtime_mode': 'legacy-default',
                    'is_default': True,
                    'status': 'stopped',
                    'data_dir': str(Path(tempfile.gettempdir()) / 'canopy-bad-legacy'),
                    'database_path': str(Path(tempfile.gettempdir()) / 'canopy-bad-legacy' / 'canopy.db'),
                    'http_port': 7770,
                    'mesh_port': 7771,
                    'discovery_port': 7772,
                },
                {
                    'meshspace_id': 'research-lab',
                    'name': 'Research Lab',
                    'runtime_mode': 'meshspace',
                    'is_default': False,
                    'status': 'defined',
                    'data_dir': str(Path(self.tempdir.name) / 'research-lab'),
                    'database_path': str(Path(self.tempdir.name) / 'research-lab' / 'canopy.db'),
                    'http_port': 7800,
                    'mesh_port': 7801,
                    'discovery_port': 7802,
                },
            ],
        }))

        records = manager.list_meshspaces()
        self.assertEqual([record.get('meshspace_id') for record in records], ['research-lab'])

        payload = json.loads(registry_path.read_text())
        mesh_ids = {item.get('meshspace_id') for item in payload.get('meshspaces', [])}
        self.assertEqual(mesh_ids, {'research-lab'})


class WindowsMeshspacePortProbeTest(unittest.TestCase):
    """``_listener_pid_for_port`` uses netstat on Windows; keep parsing covered in CI (Linux)."""

    def test_windows_listener_pid_parses_ipv4_netstat_line(self) -> None:
        from canopy.core.meshspaces import _windows_listener_pid_for_port

        fake_out = (
            "  TCP    0.0.0.0:7800           0.0.0.0:0              LISTENING       12345\n"
        )
        with patch("canopy.core.meshspaces.subprocess.run") as mock_run:
            mock_run.return_value = SimpleNamespace(stdout=fake_out)
            self.assertEqual(_windows_listener_pid_for_port(7800), 12345)

    def test_windows_listener_pid_parses_ipv6_netstat_line(self) -> None:
        from canopy.core.meshspaces import _windows_listener_pid_for_port

        fake_out = "  TCP    [::]:7800              [::]:0                 LISTENING       99999\n"
        with patch("canopy.core.meshspaces.subprocess.run") as mock_run:
            mock_run.return_value = SimpleNamespace(stdout=fake_out)
            self.assertEqual(_windows_listener_pid_for_port(7800), 99999)

    def test_windows_listener_ignores_non_matching_port(self) -> None:
        from canopy.core.meshspaces import _windows_listener_pid_for_port

        fake_out = "  TCP    0.0.0.0:7801           0.0.0.0:0              LISTENING       1\n"
        with patch("canopy.core.meshspaces.subprocess.run") as mock_run:
            mock_run.return_value = SimpleNamespace(stdout=fake_out)
            self.assertEqual(_windows_listener_pid_for_port(7800), 0)

    def test_windows_listener_pid_runs_netstat_hidden(self) -> None:
        from canopy.core.meshspaces import _WINDOWS_NO_WINDOW, _windows_listener_pid_for_port

        fake_out = "  TCP    0.0.0.0:7800           0.0.0.0:0              LISTENING       12345\n"
        fake_startupinfo = SimpleNamespace(dwFlags=0, wShowWindow=None)
        with patch("canopy.core.meshspaces._is_windows_platform", return_value=True), \
             patch("canopy.core.meshspaces.subprocess.STARTUPINFO", return_value=fake_startupinfo, create=True), \
             patch("canopy.core.meshspaces.subprocess.run") as mock_run:
            mock_run.return_value = SimpleNamespace(stdout=fake_out)
            self.assertEqual(_windows_listener_pid_for_port(7800), 12345)
            _, kwargs = mock_run.call_args
            self.assertEqual(kwargs.get("creationflags"), _WINDOWS_NO_WINDOW)
            self.assertIs(kwargs.get("startupinfo"), fake_startupinfo)
            self.assertNotEqual(fake_startupinfo.dwFlags, 0)
            self.assertEqual(fake_startupinfo.wShowWindow, 0)

    def test_pid_is_alive_handles_non_oserror_probe_failure(self) -> None:
        from canopy.core.meshspaces import _pid_is_alive

        with patch("canopy.core.meshspaces._is_windows_platform", return_value=False), \
             patch("canopy.core.meshspaces.os.kill", side_effect=SystemError("winerror 87 wrapper")):
            self.assertFalse(_pid_is_alive(12345))

    def test_pid_is_alive_returns_true_on_permission_error(self) -> None:
        from canopy.core.meshspaces import _pid_is_alive

        with patch("canopy.core.meshspaces._is_windows_platform", return_value=False), \
             patch("canopy.core.meshspaces.os.kill", side_effect=PermissionError("eperm")):
            self.assertTrue(_pid_is_alive(12345))

    def test_ss_listener_pid_for_port_parses_pid(self) -> None:
        from canopy.core.meshspaces import _ss_listener_pid_for_port

        fake_out = 'LISTEN 0 4096 0.0.0.0:7800 0.0.0.0:* users:((\"python\",pid=4321,fd=6))\\n'
        with patch("canopy.core.meshspaces.shutil.which", return_value="/usr/bin/ss"), \
             patch("canopy.core.meshspaces.subprocess.run", return_value=SimpleNamespace(stdout=fake_out)):
            self.assertEqual(_ss_listener_pid_for_port(7800), 4321)

    def test_windows_preferred_python_executable_prefers_pythonw(self) -> None:
        from canopy.core.meshspaces import _windows_preferred_python_executable

        with patch("canopy.core.meshspaces._is_windows_platform", return_value=True), \
             patch("canopy.core.meshspaces.os.path.exists", return_value=True):
            resolved = _windows_preferred_python_executable(r"C:\Python311\python.exe")
        self.assertEqual(resolved, r"C:\Python311\pythonw.exe")


class WindowsMeshspaceLaunchTest(MeshspaceFoundationTest):
    def test_ensure_runtime_registered_preserves_launcher_metadata(self) -> None:
        manager = self.app.config.get('MESHSPACE_REGISTRY_MANAGER')
        record = manager.get_meshspace('family-lab')
        self.assertIsNotNone(record)
        record['pid'] = 54321
        record['launch_log_path'] = str(self.registry_root / 'runlogs' / 'family-lab.out')
        record['last_started_at'] = '2026-04-02T01:02:03+00:00'
        manager._upsert_record(record)

        refreshed = manager.ensure_runtime_registered(self.app.config['CANOPY_CONFIG'], peer_id='peer-mesh-test')
        self.assertEqual(int(refreshed.get('pid') or 0), 54321)
        self.assertTrue(str(refreshed.get('launch_log_path') or '').endswith('family-lab.out'))
        self.assertEqual(refreshed.get('last_started_at'), '2026-04-02T01:02:03+00:00')

    def test_windows_spawn_uses_detached_launch_flags_and_scrubs_runtime_env(self) -> None:
        manager = self.app.config.get('MESHSPACE_REGISTRY_MANAGER')
        manager.create_meshspace(
            name='Research Lab',
            meshspace_id='research-lab',
            description='Experimental mesh for testing.',
        )

        with patch.dict(
            os.environ,
            {
                'CANOPY_MESHSPACE_ID': 'wrong-mesh',
                'CANOPY_DATA_DIR': 'C:/wrong-data',
                'CANOPY_PORT': '9999',
            },
            clear=False,
        ), patch('canopy.core.meshspaces._is_windows_platform', return_value=True), patch('canopy.core.meshspaces.subprocess.Popen') as popen_mock:
            popen_mock.return_value = SimpleNamespace(pid=4242)
            launched = manager._spawn_meshspace_process('research-lab')

        self.assertEqual(launched.get('pid'), 4242)
        self.assertEqual(popen_mock.call_count, 1)
        _, kwargs = popen_mock.call_args
        self.assertEqual(kwargs.get('stdin'), __import__('subprocess').DEVNULL)
        self.assertEqual(kwargs.get('stderr'), __import__('subprocess').STDOUT)
        self.assertNotIn('start_new_session', kwargs)
        self.assertEqual(
            kwargs.get('creationflags'),
            __import__('canopy.core.meshspaces', fromlist=['_windows_creation_flags'])._windows_creation_flags(include_breakaway=True),
        )
        child_env = kwargs.get('env') or {}
        self.assertEqual(child_env.get('CANOPY_MESHSPACE_ID'), 'research-lab')
        self.assertEqual(child_env.get('CANOPY_PORT'), '7800')
        self.assertNotIn('CANOPY_DATA_DIR', child_env)
        stdout_handle = kwargs.get('stdout')
        self.assertTrue(stdout_handle is not None and str(getattr(stdout_handle, 'name', '')).endswith('research-lab.out'))

        stored = manager.get_meshspace('research-lab')
        self.assertTrue(str((stored or {}).get('launch_log_path') or '').endswith('research-lab.out'))
        self.assertTrue(Path(str((stored or {}).get('launch_log_path'))).exists())

    def test_windows_spawn_retries_without_breakaway_flag(self) -> None:
        manager = self.app.config.get('MESHSPACE_REGISTRY_MANAGER')
        manager.create_meshspace(
            name='Research Lab',
            meshspace_id='research-lab',
            description='Experimental mesh for testing.',
        )

        with patch('canopy.core.meshspaces._is_windows_platform', return_value=True), patch('canopy.core.meshspaces.subprocess.Popen') as popen_mock:
            popen_mock.side_effect = [OSError('job breakaway denied'), SimpleNamespace(pid=4343)]
            launched = manager._spawn_meshspace_process('research-lab')

        self.assertEqual(launched.get('pid'), 4343)
        self.assertEqual(popen_mock.call_count, 2)
        first_kwargs = popen_mock.call_args_list[0].kwargs
        second_kwargs = popen_mock.call_args_list[1].kwargs
        meshspaces_mod = __import__('canopy.core.meshspaces', fromlist=['_windows_creation_flags'])
        self.assertEqual(
            first_kwargs.get('creationflags'),
            meshspaces_mod._windows_creation_flags(include_breakaway=True),
        )
        self.assertEqual(
            second_kwargs.get('creationflags'),
            meshspaces_mod._windows_creation_flags(include_breakaway=False),
        )

    def test_windows_stop_converges_to_stopped_when_signal_failure_finds_no_live_runtime(self) -> None:
        manager = self.app.config.get('MESHSPACE_REGISTRY_MANAGER')
        manager.create_meshspace(
            name='Research Lab',
            meshspace_id='research-lab',
            description='Experimental mesh for testing.',
        )
        record = manager.get_meshspace('research-lab')
        self.assertIsNotNone(record)
        record['status'] = 'running'
        record['pid'] = 73312
        manager._upsert_record(record)

        with patch('canopy.core.meshspaces._is_windows_platform', return_value=True), \
             patch.object(manager, 'reconcile_meshspace_runtime', side_effect=[record, record]), \
             patch.object(manager, 'probe_meshspace_runtime', side_effect=[
                 {'live': True, 'detected_pid': 73312},
                 {'live': False, 'effective_status': 'stopped'},
             ]), \
             patch('canopy.core.meshspaces.os.kill', side_effect=SystemError('returned a result with an error set')):
            stopped = manager.stop_meshspace('research-lab')

        self.assertEqual(stopped.get('status'), 'stopped')
        self.assertEqual(int(stopped.get('pid') or 0), 0)
        stored = manager.get_meshspace('research-lab')
        self.assertEqual((stored or {}).get('status'), 'stopped')
        self.assertEqual(int((stored or {}).get('pid') or 0), 0)

    def test_windows_stop_raises_clean_error_when_runtime_still_looks_live_after_signal_failure(self) -> None:
        manager = self.app.config.get('MESHSPACE_REGISTRY_MANAGER')
        manager.create_meshspace(
            name='Research Lab',
            meshspace_id='research-lab',
            description='Experimental mesh for testing.',
        )
        record = manager.get_meshspace('research-lab')
        self.assertIsNotNone(record)
        record['status'] = 'running'
        record['pid'] = 73312
        manager._upsert_record(record)

        with patch('canopy.core.meshspaces._is_windows_platform', return_value=True), \
             patch.object(manager, 'reconcile_meshspace_runtime', side_effect=[record, record]), \
             patch.object(manager, 'probe_meshspace_runtime', side_effect=[
                 {'live': True, 'detected_pid': 73312},
                 {'live': True, 'detected_pid': 73312},
             ]), \
             patch('canopy.core.meshspaces.os.kill', side_effect=SystemError('returned a result with an error set')):
            with self.assertRaisesRegex(ValueError, 'could not be stopped cleanly'):
                manager.stop_meshspace('research-lab')

    def test_viewer_attention_cache_uses_backoff_ttl_after_failure(self) -> None:
        self._authenticate()

        manager = self.app.config.get('MESHSPACE_REGISTRY_MANAGER')
        manager.create_meshspace(
            name='Research Lab',
            meshspace_id='research-lab',
            description='Experimental mesh for testing.',
        )
        manager.update_runtime_state('research-lab', 'running', peer_id='peer-research')
        manager.update_shell_summary('research-lab', {
            'active_peer_count': 1,
            'unread_count': 3,
            'mention_count': 0,
            'pending_review_count': 0,
            'attention_count': 3,
            'last_activity_at': '2026-04-04T10:00:00+00:00',
            'has_recent_activity': True,
            'attention_level': 'active',
        })
        research = manager.get_meshspace('research-lab') or {}
        self.client.set_cookie(str(research.get('session_cookie_name') or 'canopy_session_research-lab'), 'sess-tok')
        self.app.config.pop('MESHSPACE_VIEWER_ATTENTION_CACHE', None)

        from urllib.error import URLError

        probe_return = {
            'live': True,
            'effective_status': 'running',
            'status_mismatch': False,
            'detected_pid': 9001,
            'launch_url': 'http://127.0.0.1:7801',
            'version': '0.5.59',
            'peer_id': 'peer-research',
            'shell_summary': None,
        }

        with patch.object(manager, 'probe_meshspace_runtime', return_value=probe_return), \
             patch('canopy.ui.routes.urlopen', side_effect=URLError('connection refused')) as mock_urlopen:
            with patch('canopy.ui.routes.time') as mock_time:
                mock_time.time.return_value = 1000.0
                self.client.get('/ajax/meshspaces_snapshot')

            first_count = mock_urlopen.call_count

            with patch('canopy.ui.routes.time') as mock_time:
                mock_time.time.return_value = 1003.0
                self.client.get('/ajax/meshspaces_snapshot')

            self.assertEqual(first_count, mock_urlopen.call_count)

        cache = self.app.config.get('MESHSPACE_VIEWER_ATTENTION_CACHE') or {}
        entry = next(iter(cache.values()), None)
        self.assertIsNotNone(entry)
        self.assertGreater(int((entry or {}).get('error_count') or 0), 0)
        self.assertGreater(float((entry or {}).get('backoff_ttl') or 0.0), 0.0)

    def test_viewer_attention_backoff_ttl_escalates_on_repeated_failures(self) -> None:
        self._authenticate()

        manager = self.app.config.get('MESHSPACE_REGISTRY_MANAGER')
        manager.create_meshspace(
            name='Research Lab',
            meshspace_id='research-lab',
            description='Experimental mesh for testing.',
        )
        manager.update_runtime_state('research-lab', 'running', peer_id='peer-research')
        manager.update_shell_summary('research-lab', {
            'active_peer_count': 1,
            'unread_count': 2,
            'mention_count': 0,
            'pending_review_count': 0,
            'attention_count': 2,
            'last_activity_at': '2026-04-04T10:00:00+00:00',
            'has_recent_activity': True,
            'attention_level': 'active',
        })
        research = manager.get_meshspace('research-lab') or {}
        self.client.set_cookie(str(research.get('session_cookie_name') or 'canopy_session_research-lab'), 'sess-tok2')
        self.app.config.pop('MESHSPACE_VIEWER_ATTENTION_CACHE', None)

        from urllib.error import URLError

        probe_return = {
            'live': True,
            'effective_status': 'running',
            'status_mismatch': False,
            'detected_pid': 9002,
            'launch_url': 'http://127.0.0.1:7802',
            'version': '0.5.59',
            'peer_id': 'peer-research',
            'shell_summary': None,
        }
        backoff_ttls = []

        with patch.object(manager, 'probe_meshspace_runtime', return_value=probe_return), \
             patch('canopy.ui.routes.urlopen', side_effect=URLError('connection refused')):
            for i in range(4):
                with patch('canopy.ui.routes.time') as mock_time:
                    mock_time.time.return_value = 1000.0 + i * 200.0
                    self.client.get('/ajax/meshspaces_snapshot')
                cache = self.app.config.get('MESHSPACE_VIEWER_ATTENTION_CACHE') or {}
                entry = next(iter(cache.values()), None)
                backoff_ttls.append(float((entry or {}).get('backoff_ttl') or 0.0))

        self.assertEqual(backoff_ttls, [5.0, 10.0, 20.0, 40.0])

    def test_viewer_attention_backoff_resets_on_success(self) -> None:
        self._authenticate()

        manager = self.app.config.get('MESHSPACE_REGISTRY_MANAGER')
        manager.create_meshspace(
            name='Research Lab',
            meshspace_id='research-lab',
            description='Experimental mesh for testing.',
        )
        manager.update_runtime_state('research-lab', 'running', peer_id='peer-research')
        manager.update_shell_summary('research-lab', {
            'active_peer_count': 1,
            'unread_count': 1,
            'mention_count': 0,
            'pending_review_count': 0,
            'attention_count': 1,
            'last_activity_at': '2026-04-04T10:00:00+00:00',
            'has_recent_activity': True,
            'attention_level': 'active',
        })
        research = manager.get_meshspace('research-lab') or {}
        self.client.set_cookie(str(research.get('session_cookie_name') or 'canopy_session_research-lab'), 'sess-tok3')
        self.app.config.pop('MESHSPACE_VIEWER_ATTENTION_CACHE', None)

        from urllib.error import URLError

        probe_return = {
            'live': True,
            'effective_status': 'running',
            'status_mismatch': False,
            'detected_pid': 9003,
            'launch_url': 'http://127.0.0.1:7803',
            'version': '0.5.59',
            'peer_id': 'peer-research',
            'shell_summary': None,
        }

        with patch.object(manager, 'probe_meshspace_runtime', return_value=probe_return), \
             patch('canopy.ui.routes.urlopen', side_effect=URLError('connection refused')):
            with patch('canopy.ui.routes.time') as mock_time:
                mock_time.time.return_value = 2000.0
                self.client.get('/ajax/meshspaces_snapshot')

        cache = self.app.config.get('MESHSPACE_VIEWER_ATTENTION_CACHE') or {}
        failed_entry = next(iter(cache.values()), None)
        self.assertGreater(int((failed_entry or {}).get('error_count') or 0), 0)

        success_response = MagicMock()
        success_response.read.return_value = json.dumps({
            'viewer_specific': True,
            'meshspace_id': 'research-lab',
            'unread_count': 1,
            'mention_count': 0,
            'pending_review_count': 0,
            'attention_count': 1,
        }).encode('utf-8')
        success_response.__enter__.return_value = success_response
        success_response.__exit__.return_value = None

        with patch.object(manager, 'probe_meshspace_runtime', return_value=probe_return), \
             patch('canopy.ui.routes.urlopen', return_value=success_response):
            with patch('canopy.ui.routes.time') as mock_time:
                mock_time.time.return_value = 2500.0
                self.client.get('/ajax/meshspaces_snapshot')

        cache = self.app.config.get('MESHSPACE_VIEWER_ATTENTION_CACHE') or {}
        success_entry = next(iter(cache.values()), None)
        self.assertEqual(int((success_entry or {}).get('error_count') or 0), 0)
        self.assertAlmostEqual(float((success_entry or {}).get('backoff_ttl') or 0.0), 5.0)


if __name__ == '__main__':
    unittest.main()
