"""Regression tests for trust-driven profile/avatar recovery."""

import base64
import os
import sqlite3
import sys
import tempfile
import types
import unittest
from unittest.mock import MagicMock, call, patch

from flask import Flask

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
from canopy.ui.routes import create_ui_blueprint


class _FakeP2PNetworkManager:
    def __init__(self, config, db_manager):
        self.config = config
        self.db_manager = db_manager
        self.local_identity = types.SimpleNamespace(peer_id='peer-local', x25519_private_key=None)
        self.identity_manager = types.SimpleNamespace(
            local_identity=self.local_identity,
            peer_display_names={},
            known_peers={},
        )
        self.connection_manager = types.SimpleNamespace(get_connected_peers=lambda: [], get_connection=lambda peer_id: None)
        self.discovery = None
        self.peer_versions = {}
        self._running = False
        self.on_profile_sync = None

    def get_peer_id(self):
        return self.local_identity.peer_id

    def is_running(self):
        return self._running

    def start(self):
        self._running = True


class _FakeTrustManager:
    def __init__(self) -> None:
        self.scores = {}

    def get_trust_score(self, peer_id: str) -> int:
        return int(self.scores.get(peer_id, 0))

    def set_trust_score(self, peer_id: str, score: int, reason=None) -> int:
        self.scores[peer_id] = int(score)
        return int(score)


class TestTrustProfileRecovery(unittest.TestCase):
    def test_recover_peer_profile_state_backfills_missing_remote_user_avatar(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)

        env_patcher = patch.dict(
            os.environ,
            {
                'CANOPY_TESTING': 'true',
                'CANOPY_DISABLE_MESH': 'true',
                'CANOPY_DATA_DIR': tempdir.name,
                'CANOPY_DATABASE_PATH': os.path.join(tempdir.name, 'canopy.db'),
                'CANOPY_SECRET_KEY': 'test-secret',
            },
            clear=False,
        )
        env_patcher.start()
        self.addCleanup(env_patcher.stop)

        checkpoint_patcher = patch(
            'canopy.core.database.DatabaseManager._start_checkpoint_thread',
            lambda self: None,
        )
        checkpoint_patcher.start()
        self.addCleanup(checkpoint_patcher.stop)

        logging_patcher = patch(
            'canopy.core.app.setup_logging',
            lambda debug=False: None,
        )
        logging_patcher.start()
        self.addCleanup(logging_patcher.stop)

        p2p_patcher = patch(
            'canopy.core.app.P2PNetworkManager',
            _FakeP2PNetworkManager,
        )
        p2p_patcher.start()
        self.addCleanup(p2p_patcher.stop)

        app = create_app()
        db_manager = app.config['DB_MANAGER']
        channel_manager = app.config['CHANNEL_MANAGER']
        trust_manager = app.config['TRUST_MANAGER']
        p2p_manager = app.config['P2P_MANAGER']

        with app.app_context():
            with db_manager.get_connection() as conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO users (
                        id, username, public_key, password_hash, display_name,
                        origin_peer, account_type, status, created_at, updated_at, avatar_file_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, ?)
                    """,
                    (
                        'remote-user',
                        'remote_user',
                        'remote-public-key',
                        None,
                        'Remote User',
                        'peer-trusted',
                        'human',
                        'active',
                        None,
                    ),
                )
                conn.commit()

            trust_manager.set_trust_score('peer-trusted', 90, reason='approved')
            channel_manager.store_peer_device_profile(
                peer_id='peer-trusted',
                display_name='Trusted Device',
                description='Recovered via trust promotion',
                avatar_b64=base64.b64encode(b'fake-avatar-bytes').decode('ascii'),
                avatar_mime='image/png',
            )

            result = p2p_manager.recover_peer_profile_state(trigger_sync=False)

            self.assertTrue(result.get('ok'))
            self.assertEqual(result.get('recovered_user_count'), 1)
            self.assertIn('remote-user', result.get('recovered_user_ids') or [])

            with db_manager.get_connection() as conn:
                row = conn.execute(
                    "SELECT avatar_file_id FROM users WHERE id = ?",
                    ('remote-user',),
                ).fetchone()
            self.assertIsNotNone(row)
            self.assertTrue(row['avatar_file_id'])

    def test_trust_update_triggers_profile_recovery_when_peer_becomes_trusted(self) -> None:
        trust_manager = _FakeTrustManager()
        p2p_manager = MagicMock()
        p2p_manager.recover_peer_profile_state.return_value = {
            'ok': True,
            'recovered_user_count': 2,
            'sync_triggered_for': ['peer-1'],
        }

        components = (
            MagicMock(),   # db_manager
            MagicMock(),   # api_key_manager
            trust_manager, # trust_manager
            MagicMock(),   # message_manager
            MagicMock(),   # channel_manager
            MagicMock(),   # file_manager
            MagicMock(),   # feed_manager
            MagicMock(),   # interaction_manager
            MagicMock(),   # profile_manager
            MagicMock(),   # config
            p2p_manager,   # p2p_manager
        )

        patcher = patch('canopy.ui.routes._get_app_components_any', return_value=components)
        patcher.start()
        self.addCleanup(patcher.stop)

        app = Flask(__name__)
        app.config['TESTING'] = True
        app.secret_key = 'test-secret'
        app.register_blueprint(create_ui_blueprint())
        client = app.test_client()

        token = 'csrf-trust-recovery'
        with client.session_transaction() as sess:
            sess['authenticated'] = True
            sess['user_id'] = 'owner'
            sess['username'] = 'owner'
            sess['_csrf_token'] = token

        response = client.post(
            '/trust/update',
            json={'peer_id': 'peer-1', 'tier': 'safe'},
            headers={'X-CSRFToken': token},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json() or {}
        self.assertTrue(payload.get('is_trusted'))
        self.assertEqual(payload.get('profile_recovery', {}).get('recovered_user_count'), 2)
        self.assertEqual(
            p2p_manager.method_calls[:2],
            [
                call.clear_peer_profile_cache('peer-1'),
                call.recover_peer_profile_state('peer-1', trigger_sync=True),
            ],
        )
        p2p_manager.recover_peer_profile_state.assert_called_once_with('peer-1', trigger_sync=True)

    def test_trust_peer_action_refresh_profile_clears_cache_and_recovers(self) -> None:
        trust_manager = _FakeTrustManager()
        p2p_manager = MagicMock()
        p2p_manager.clear_peer_profile_cache.return_value = {'cleared_hashes': 1, 'cleared_relays': 0}
        p2p_manager.recover_peer_profile_state.return_value = {
            'ok': True,
            'skipped_untrusted': [],
            'recovered_user_count': 1,
        }

        components = (
            MagicMock(),
            MagicMock(),
            trust_manager,
            MagicMock(),
            MagicMock(),
            MagicMock(),
            MagicMock(),
            MagicMock(),
            MagicMock(),
            MagicMock(),
            p2p_manager,
        )

        patcher = patch('canopy.ui.routes._get_app_components_any', return_value=components)
        patcher.start()
        self.addCleanup(patcher.stop)

        app = Flask(__name__)
        app.config['TESTING'] = True
        app.secret_key = 'test-secret'
        app.register_blueprint(create_ui_blueprint())
        client = app.test_client()

        token = 'csrf-trust-peer-action-refresh'
        with client.session_transaction() as sess:
            sess['authenticated'] = True
            sess['user_id'] = 'owner'
            sess['username'] = 'owner'
            sess['_csrf_token'] = token

        response = client.post(
            '/trust/peer_action',
            json={'peer_id': 'peer-1', 'action': 'refresh_profile'},
            headers={'X-CSRFToken': token},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json() or {}
        self.assertTrue(payload.get('success'))
        p2p_manager.clear_peer_profile_cache.assert_called_once_with('peer-1')
        p2p_manager.recover_peer_profile_state.assert_called_once_with('peer-1', trigger_sync=True)

    def test_trust_peer_action_sync_now_triggers_peer_sync(self) -> None:
        trust_manager = _FakeTrustManager()
        p2p_manager = MagicMock()
        p2p_manager.trigger_peer_sync.return_value = True

        components = (
            MagicMock(),
            MagicMock(),
            trust_manager,
            MagicMock(),
            MagicMock(),
            MagicMock(),
            MagicMock(),
            MagicMock(),
            MagicMock(),
            MagicMock(),
            p2p_manager,
        )

        patcher = patch('canopy.ui.routes._get_app_components_any', return_value=components)
        patcher.start()
        self.addCleanup(patcher.stop)

        app = Flask(__name__)
        app.config['TESTING'] = True
        app.secret_key = 'test-secret'
        app.register_blueprint(create_ui_blueprint())
        client = app.test_client()

        token = 'csrf-trust-peer-action-sync'
        with client.session_transaction() as sess:
            sess['authenticated'] = True
            sess['user_id'] = 'owner'
            sess['username'] = 'owner'
            sess['_csrf_token'] = token

        response = client.post(
            '/trust/peer_action',
            json={'peer_id': 'peer-1', 'action': 'sync_now'},
            headers={'X-CSRFToken': token},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json() or {}
        self.assertTrue(payload.get('success'))
        p2p_manager.trigger_peer_sync.assert_called_once_with('peer-1')

    def test_trust_view_marks_introduced_peer_with_no_endpoints_for_attention(self) -> None:
        trust_manager = _FakeTrustManager()
        trust_manager.get_all_trust_scores = MagicMock(return_value={})
        trust_manager.get_trust_statistics = MagicMock(return_value={})
        trust_manager.get_pending_delete_signals = MagicMock(return_value=[])
        trust_manager.get_trusted_peers = MagicMock(return_value=[])

        channel_manager = MagicMock()
        channel_manager.get_all_peer_device_profiles.return_value = {}

        p2p_manager = MagicMock()
        p2p_manager.get_connected_peers.return_value = []
        p2p_manager.get_introduced_peers.return_value = [
            {'peer_id': 'peer-1', 'introduced_by': 'peer-2', 'public_identity': {'node_name': 'Peer One'}}
        ]
        p2p_manager.get_peer_id.return_value = 'peer-local'
        p2p_manager.get_peer_public_identity.return_value = {'node_name': 'Peer One'}
        p2p_manager.identity_manager = types.SimpleNamespace(
            known_peers={},
            peer_display_names={},
            peer_endpoints={},
        )

        components = (
            MagicMock(),
            MagicMock(),
            trust_manager,
            MagicMock(),
            channel_manager,
            MagicMock(),
            MagicMock(),
            MagicMock(),
            MagicMock(),
            MagicMock(),
            p2p_manager,
        )

        patcher = patch('canopy.ui.routes._get_app_components_any', return_value=components)
        patcher.start()
        self.addCleanup(patcher.stop)

        app = Flask(__name__)
        app.config['TESTING'] = True
        app.secret_key = 'test-secret'
        app.register_blueprint(create_ui_blueprint())
        client = app.test_client()

        with client.session_transaction() as sess:
            sess['authenticated'] = True
            sess['user_id'] = 'owner'
            sess['username'] = 'owner'
            sess['_csrf_token'] = 'csrf-trust-view'

        response = client.get('/trust')

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn('No endpoints', body)
        self.assertNotIn("runTrustPeerAction('peer-1', 'connect_introduced', this)", body)


if __name__ == '__main__':
    unittest.main()
