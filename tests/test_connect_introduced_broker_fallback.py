"""Regression tests for introduced-peer broker fallback behavior."""

import os
import sys
import types
import unittest
from unittest.mock import MagicMock, patch

from flask import Flask

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

from canopy.api.routes import create_api_blueprint


class _ImmediateFuture:
    def __init__(self, result):
        self._result = result

    def result(self, timeout=None):
        return self._result


class TestConnectIntroducedBrokerFallback(unittest.TestCase):
    def setUp(self) -> None:
        self.api_key_manager = MagicMock()
        self.api_key_manager.validate_key.return_value = None

        self.p2p_manager = MagicMock()
        self.p2p_manager.relay_policy = 'broker_only'
        self.p2p_manager._event_loop = MagicMock()
        self.p2p_manager._event_loop.is_closed.return_value = False
        self.p2p_manager._introduced_peers = {}
        self.p2p_manager.get_peer_id.return_value = 'local-peer'
        self.p2p_manager.get_connected_peers.return_value = []
        self.p2p_manager.send_broker_request.return_value = False
        self.p2p_manager.get_introduced_peer_broker_candidates = MagicMock(return_value=[])
        self.p2p_manager.identity_manager = types.SimpleNamespace(peer_endpoints={})

        self.p2p_manager.connection_manager = MagicMock()
        self.p2p_manager.connection_manager.connect_to_peer = MagicMock()

        self.db_manager = MagicMock()
        self.db_manager.get_instance_owner_user_id.return_value = 'admin-user'

        # Order must match get_app_components in canopy.core.utils
        components = (
            self.db_manager,       # db_manager
            self.api_key_manager,  # api_key_manager
            MagicMock(),           # trust_manager
            MagicMock(),           # message_manager
            MagicMock(),           # channel_manager
            MagicMock(),           # file_manager
            MagicMock(),           # feed_manager
            MagicMock(),           # interaction_manager
            MagicMock(),           # profile_manager
            MagicMock(),           # config
            self.p2p_manager,      # p2p_manager
        )

        self.get_components_patcher = patch(
            'canopy.api.routes.get_app_components',
            return_value=components,
        )
        self.get_components_patcher.start()
        self.addCleanup(self.get_components_patcher.stop)

        app = Flask(__name__)
        app.config['TESTING'] = True
        app.secret_key = 'test-secret'
        app.register_blueprint(create_api_blueprint(), url_prefix='/api/v1')

        self.app = app
        self.client = app.test_client()

    def _set_authenticated_session(self, csrf_token: str = 'csrf-test-token') -> None:
        with self.client.session_transaction() as sess:
            sess['authenticated'] = True
            sess['user_id'] = 'test-user'
            sess['_csrf_token'] = csrf_token

    def _post_connect_introduced(self, peer_id: str, csrf_token: str = 'csrf-pass', extra_payload=None):
        self._set_authenticated_session(csrf_token=csrf_token)
        payload = {'peer_id': peer_id}
        if isinstance(extra_payload, dict):
            payload.update(extra_payload)
        return self.client.post(
            '/api/v1/p2p/connect_introduced',
            json=payload,
            headers={'X-CSRFToken': csrf_token},
        )

    def test_connect_introduced_tries_multiple_brokers_until_one_succeeds(self) -> None:
        peer_id = 'peer-target'
        self.p2p_manager._introduced_peers[peer_id] = {
            'peer_id': peer_id,
            'endpoints': ['ws://203.0.113.10:7771'],
            'introduced_by': 'broker-a',
            'introduced_via': ['broker-a', 'broker-b'],
        }
        self.p2p_manager.get_connected_peers.return_value = ['broker-a', 'broker-b']
        self.p2p_manager.send_broker_request.side_effect = [False, True]

        with patch('asyncio.run_coroutine_threadsafe', return_value=_ImmediateFuture(False)):
            response = self._post_connect_introduced(peer_id)

        self.assertEqual(response.status_code, 202)
        payload = response.get_json() or {}
        self.assertEqual(payload.get('status'), 'brokering')
        self.assertEqual(payload.get('via_peer'), 'broker-b')
        self.assertEqual(payload.get('attempted_brokers'), ['broker-a', 'broker-b'])

        call_args = self.p2p_manager.send_broker_request.call_args_list
        self.assertEqual(len(call_args), 2)
        self.assertEqual(call_args[0].kwargs.get('via_peer_id'), 'broker-a')
        self.assertEqual(call_args[1].kwargs.get('via_peer_id'), 'broker-b')

    def test_connect_introduced_falls_back_to_other_connected_peer(self) -> None:
        peer_id = 'peer-target'
        self.p2p_manager._introduced_peers[peer_id] = {
            'peer_id': peer_id,
            'endpoints': ['ws://203.0.113.10:7771'],
            'introduced_by': 'offline-broker',
        }
        self.p2p_manager.get_connected_peers.return_value = ['online-broker']

        def _send_broker_request(**kwargs):
            return kwargs.get('via_peer_id') == 'online-broker'

        self.p2p_manager.send_broker_request.side_effect = _send_broker_request

        with patch('asyncio.run_coroutine_threadsafe', return_value=_ImmediateFuture(False)):
            response = self._post_connect_introduced(peer_id)

        self.assertEqual(response.status_code, 202)
        payload = response.get_json() or {}
        self.assertEqual(payload.get('status'), 'brokering')
        self.assertEqual(payload.get('via_peer'), 'online-broker')
        self.assertEqual(payload.get('attempted_brokers'), ['online-broker'])

    def test_connect_introduced_failure_includes_relay_guidance(self) -> None:
        peer_id = 'peer-target'
        self.p2p_manager._introduced_peers[peer_id] = {
            'peer_id': peer_id,
            'endpoints': ['ws://203.0.113.10:7771'],
            'introduced_by': 'offline-broker',
            'introduced_via': ['offline-broker', 'offline-broker-2'],
        }
        self.p2p_manager.get_connected_peers.return_value = []
        self.p2p_manager.send_broker_request.return_value = False

        with patch('asyncio.run_coroutine_threadsafe', return_value=_ImmediateFuture(False)):
            response = self._post_connect_introduced(peer_id)

        self.assertEqual(response.status_code, 502)
        payload = response.get_json() or {}
        self.assertEqual(payload.get('status'), 'failed')
        self.assertEqual(payload.get('relay_policy'), 'broker_only')
        self.assertIn('Relay policy is broker_only', payload.get('message', ''))
        self.assertEqual(payload.get('attempted_brokers'), [])

    def test_connect_introduced_without_direct_endpoints_uses_broker(self) -> None:
        peer_id = 'peer-target'
        self.p2p_manager._introduced_peers[peer_id] = {
            'peer_id': peer_id,
            'endpoints': [],
            'introduced_by': 'broker-a',
            'introduced_via': ['broker-a'],
        }
        self.p2p_manager.get_introduced_peer_broker_candidates.return_value = ['broker-a']
        self.p2p_manager.send_broker_request.return_value = True

        response = self._post_connect_introduced(peer_id)

        self.assertEqual(response.status_code, 202)
        payload = response.get_json() or {}
        self.assertEqual(payload.get('status'), 'brokering')
        self.assertEqual(payload.get('via_peer'), 'broker-a')
        self.assertEqual(payload.get('diagnostic_code'), 'introduced_peer_broker_connect')
        self.assertFalse(payload.get('direct_attempted'))
        self.assertEqual(payload.get('direct_attempt_count'), 0)
        self.assertIn('No direct endpoints were announced', payload.get('message', ''))

    def test_connect_introduced_cross_mesh_candidate_requires_explicit_confirmation(self) -> None:
        peer_id = 'peer-target'
        self.p2p_manager._introduced_peers[peer_id] = {
            'peer_id': peer_id,
            'endpoints': ['ws://203.0.113.10:7771'],
            'introduced_by': 'broker-a',
        }
        self.p2p_manager.get_introduced_peer_meshspace_status.return_value = {
            'requires_explicit_meshspace_connect': True,
            'remote_meshspace_label': 'Private Mesh',
        }

        response = self._post_connect_introduced(peer_id)

        self.assertEqual(response.status_code, 409)
        payload = response.get_json() or {}
        self.assertEqual(payload.get('diagnostic_code'), 'introduced_cross_mesh_confirmation_required')
        self.assertEqual(payload.get('status'), 'confirmation_required')
        self.p2p_manager.connection_manager.connect_to_peer.assert_not_called()

    def test_connect_introduced_cross_mesh_explicit_action_requires_admin(self) -> None:
        peer_id = 'peer-target'
        self.p2p_manager._introduced_peers[peer_id] = {
            'peer_id': peer_id,
            'endpoints': ['ws://203.0.113.10:7771'],
            'introduced_by': 'broker-a',
        }
        self.p2p_manager.get_introduced_peer_meshspace_status.return_value = {
            'requires_explicit_meshspace_connect': True,
            'remote_meshspace_label': 'Private Mesh',
        }

        response = self._post_connect_introduced(
            peer_id,
            extra_payload={'allow_cross_mesh': True},
        )

        self.assertEqual(response.status_code, 403)
        payload = response.get_json() or {}
        self.assertEqual(payload.get('diagnostic_code'), 'cross_mesh_admin_required')
        self.p2p_manager.connection_manager.connect_to_peer.assert_not_called()

    def test_connect_introduced_force_broker_skips_direct_attempts(self) -> None:
        peer_id = 'peer-target'
        self.p2p_manager._introduced_peers[peer_id] = {
            'peer_id': peer_id,
            'endpoints': ['ws://203.0.113.10:7771'],
            'introduced_via': ['broker-a'],
        }
        self.p2p_manager.get_connected_peers.return_value = ['broker-a']
        self.p2p_manager.send_broker_request.return_value = True

        with patch('asyncio.run_coroutine_threadsafe') as run_coro:
            response = self._post_connect_introduced(
                peer_id,
                extra_payload={'force_broker': True},
            )

        self.assertEqual(response.status_code, 202)
        payload = response.get_json() or {}
        self.assertEqual(payload.get('status'), 'brokering')
        self.assertTrue(payload.get('forced_failover'))
        self.assertFalse(payload.get('direct_attempted'))
        self.assertEqual(payload.get('direct_attempt_count'), 0)
        self.assertEqual(payload.get('attempted_brokers'), ['broker-a'])
        run_coro.assert_not_called()

    def test_connect_introduced_force_broker_preserves_explicit_cross_mesh_intent(self) -> None:
        peer_id = 'peer-target'
        self.db_manager.get_instance_owner_user_id.return_value = 'test-user'
        self.p2p_manager._introduced_peers[peer_id] = {
            'peer_id': peer_id,
            'endpoints': ['ws://203.0.113.10:7771'],
            'introduced_via': ['broker-a'],
        }
        self.p2p_manager.get_introduced_peer_meshspace_status.return_value = {
            'requires_explicit_meshspace_connect': True,
            'remote_meshspace_label': 'Private Mesh',
        }
        self.p2p_manager.get_connected_peers.return_value = ['broker-a']
        self.p2p_manager.send_broker_request.return_value = True

        with patch('asyncio.run_coroutine_threadsafe') as run_coro:
            response = self._post_connect_introduced(
                peer_id,
                extra_payload={'force_broker': True, 'allow_cross_mesh': True},
            )

        self.assertEqual(response.status_code, 202)
        payload = response.get_json() or {}
        self.assertEqual(payload.get('status'), 'brokering')
        self.assertTrue(payload.get('forced_failover'))
        self.assertEqual(payload.get('attempted_brokers'), ['broker-a'])
        self.assertTrue(self.p2p_manager.send_broker_request.call_args.kwargs.get('allow_cross_mesh'))
        run_coro.assert_not_called()

    def test_connect_introduced_prefers_public_endpoint_over_stale_lan_history(self) -> None:
        peer_id = 'peer-target'
        self.p2p_manager._introduced_peers[peer_id] = {
            'peer_id': peer_id,
            'endpoints': ['ws://198.51.100.159:7771', 'wss://vps.example.com:443'],
        }

        with patch(
            'asyncio.run_coroutine_threadsafe',
            side_effect=[_ImmediateFuture(False), _ImmediateFuture(True)],
        ):
            response = self._post_connect_introduced(peer_id)

        self.assertEqual(response.status_code, 200)
        call_args = self.p2p_manager.connection_manager.connect_to_peer.call_args_list
        self.assertEqual(call_args[0].args[1:], ('vps.example.com', 443))
        self.assertEqual(call_args[0].kwargs.get('scheme'), 'wss')
        self.assertEqual(call_args[1].args[1:], ('198.51.100.159', 7771))
        self.assertEqual(call_args[1].kwargs.get('scheme'), 'ws')


if __name__ == '__main__':
    unittest.main()
