"""Unit tests for trusted-peer content delivery gates."""

import asyncio
import os
import sys
import types
import unittest
from types import SimpleNamespace

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

from canopy.network.manager import P2PNetworkManager


class TestManagerTrustGating(unittest.TestCase):
    def test_network_feed_targets_only_explicitly_trusted_peers(self) -> None:
        manager = P2PNetworkManager.__new__(P2PNetworkManager)
        manager.config = SimpleNamespace(security=SimpleNamespace(trust_threshold=50))
        manager.local_identity = SimpleNamespace(peer_id='peer-local')
        manager.get_connected_peers = lambda: ['peer-trusted', 'peer-untrusted', 'peer-unknown']
        manager.get_trust_score = lambda peer_id: {'peer-trusted': 75, 'peer-untrusted': 0}.get(peer_id, 0)
        manager.has_explicit_trust_score = lambda peer_id: peer_id in {'peer-trusted', 'peer-untrusted'}
        manager.get_peer_id = lambda: 'peer-local'

        peers = manager._get_feed_post_target_peers('network')

        self.assertEqual(peers, ['peer-trusted'])

    def test_untrusted_peer_gets_public_only_post_connect_bootstrap(self) -> None:
        manager = P2PNetworkManager.__new__(P2PNetworkManager)
        manager.on_peer_connected = None
        manager._cancel_reconnect = lambda peer_id: None
        manager._refresh_peer_version_info = lambda peer_id: None
        manager._peer_is_trusted_for_content = lambda peer_id: False

        calls = []

        async def _record(name, peer_id):
            calls.append((name, peer_id))

        manager._send_channel_sync_to_peer = lambda peer_id: _record('channel_sync', peer_id)
        manager._send_catchup_request = lambda peer_id: _record('catchup', peer_id)
        manager._send_membership_recovery_query = lambda peer_id: _record('membership', peer_id)
        manager._retry_missing_channel_key_requests_for_peer = lambda peer_id: _record('keys', peer_id)
        manager._send_profile_to_peer = lambda peer_id: _record('profile', peer_id)
        manager._send_peer_announcement_to = lambda peer_id: _record('peer_announce', peer_id)
        manager._announce_new_peer_to_others = lambda peer_id: _record('announce_others', peer_id)
        manager.message_router = SimpleNamespace(flush_pending_messages=lambda peer_id: _record('flush', peer_id))

        asyncio.run(manager._run_post_connect_sync_impl('peer-guest'))

        self.assertEqual(calls, [('channel_sync', 'peer-guest'), ('catchup', 'peer-guest')])

    def test_untrusted_catchup_request_sends_only_public_channel_timestamps(self) -> None:
        manager = P2PNetworkManager.__new__(P2PNetworkManager)
        manager.message_router = SimpleNamespace()
        manager._peer_is_trusted_for_content = lambda peer_id: False
        manager.get_channel_latest_timestamps = lambda: {
            'Cpublic': '2026-03-30 12:00:00',
            'Cprivate': '2026-03-30 12:01:00',
        }
        manager.get_public_channels_for_sync = lambda: [{'id': 'Cpublic'}]
        manager.get_channel_sync_digests = None
        manager.sync_digest_enabled = False
        manager.get_feed_latest_timestamp = None
        manager.get_circle_entries_latest_timestamp = None
        manager.get_circle_votes_latest_timestamp = None
        manager.get_circles_latest_timestamp = None
        manager.get_tasks_latest_timestamp = None

        sent: list[tuple[str, dict, dict | None, dict | None]] = []

        async def _send_catchup_request(peer_id, channel_timestamps, extra_timestamps=None, digest=None):
            sent.append((peer_id, channel_timestamps, extra_timestamps, digest))

        manager.message_router.send_catchup_request = _send_catchup_request

        asyncio.run(manager._send_catchup_request('peer-guest'))

        self.assertEqual(
            sent,
            [('peer-guest', {'Cpublic': '2026-03-30 12:00:00'}, None, None)],
        )


if __name__ == '__main__':
    unittest.main()
