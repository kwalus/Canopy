"""Unit tests for trusted-peer content delivery gates."""

import asyncio
import json
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

from canopy.network.manager import (
    CHANNEL_SYNC_TARGET_PAYLOAD_BYTES,
    LEGACY_BULK_SYNC_CAPABILITY,
    LEGACY_BULK_SYNC_MAX_PAYLOAD_BYTES,
    P2PNetworkManager,
)
from canopy.network.routing import MAX_PAYLOAD_BYTES


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
        manager._send_public_channel_metadata_replay_to_peer = lambda peer_id: _record('metadata_replay', peer_id)
        manager._send_catchup_request = lambda peer_id: _record('catchup', peer_id)
        manager._send_membership_recovery_query = lambda peer_id: _record('membership', peer_id)
        manager._retry_missing_channel_key_requests_for_peer = lambda peer_id: _record('keys', peer_id)
        manager._send_profile_to_peer = lambda peer_id: _record('profile', peer_id)
        manager._send_peer_announcement_to = lambda peer_id: _record('peer_announce', peer_id)
        manager._announce_new_peer_to_others = lambda peer_id: _record('announce_others', peer_id)
        manager.message_router = SimpleNamespace(flush_pending_messages=lambda peer_id: _record('flush', peer_id))

        asyncio.run(manager._run_post_connect_sync_impl('peer-guest'))

        self.assertEqual(calls, [('channel_sync', 'peer-guest'), ('metadata_replay', 'peer-guest'), ('catchup', 'peer-guest')])

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

        manager.get_channel_history_bounds = lambda: {
            'Cpublic': {
                'latest': '2026-03-30 12:00:00',
                'oldest': '2026-03-29 12:00:00',
                'message_count': 4,
            },
            'Cprivate': {
                'latest': '2026-03-30 12:01:00',
                'oldest': '2026-03-29 12:01:00',
                'message_count': 2,
            },
        }

        sent: list[tuple[str, dict, dict | None, dict | None, dict | None]] = []

        async def _send_catchup_request(peer_id, channel_timestamps, extra_timestamps=None, digest=None, channel_ranges=None):
            sent.append((peer_id, channel_timestamps, extra_timestamps, digest, channel_ranges))

        manager.message_router.send_catchup_request = _send_catchup_request

        asyncio.run(manager._send_catchup_request('peer-guest'))

        self.assertEqual(
            sent,
            [('peer-guest', {'Cpublic': '2026-03-30 12:00:00'}, None, None, {
                'Cpublic': {
                    'latest': '2026-03-30 12:00:00',
                    'oldest': '2026-03-29 12:00:00',
                    'message_count': 4,
                }
            })],
        )

    def test_channel_sync_batches_stay_under_payload_budget(self) -> None:
        manager = P2PNetworkManager.__new__(P2PNetworkManager)
        channels = [
            {
                'id': f'C{i:03d}',
                'name': f'public-{i}',
                'type': 'public',
                'desc': 'x' * 30000,
                'origin_peer': 'peer-local',
                'privacy_mode': 'open',
            }
            for i in range(60)
        ]

        batches = manager._chunk_channel_sync_batches(channels)

        self.assertGreater(len(batches), 1)
        self.assertEqual(sum(len(batch) for batch in batches), len(channels))
        for batch in batches:
            payload_size = manager._estimate_channel_sync_payload_bytes(batch)
            self.assertLessEqual(payload_size, CHANNEL_SYNC_TARGET_PAYLOAD_BYTES)
            self.assertLess(payload_size, MAX_PAYLOAD_BYTES)

    def test_send_channel_sync_to_peer_sends_multiple_batches(self) -> None:
        manager = P2PNetworkManager.__new__(P2PNetworkManager)
        manager.peer_versions = {}
        manager._introduced_peers = {}
        manager.connection_manager = None
        manager.message_router = SimpleNamespace()
        manager.get_public_channels_for_sync = lambda: [
            {
                'id': f'C{i:03d}',
                'name': f'public-{i}',
                'type': 'public',
                'desc': 'x' * 30000,
                'origin_peer': 'peer-local',
                'privacy_mode': 'open',
            }
            for i in range(60)
        ]

        sent_batches: list[list[dict]] = []

        async def _send_channel_sync(peer_id, channels):
            sent_batches.append(channels)
            return True

        manager.message_router.send_channel_sync = _send_channel_sync

        asyncio.run(manager._send_channel_sync_to_peer('peer-guest'))

        self.assertGreater(len(sent_batches), 1)
        self.assertEqual(sum(len(batch) for batch in sent_batches), 60)
        for batch in sent_batches:
            payload = {'content': '', 'metadata': {'type': 'channel_sync', 'channels': batch}}
            self.assertLess(len(json.dumps(payload).encode('utf-8')), MAX_PAYLOAD_BYTES)

    def test_legacy_peer_channel_sync_batches_stay_under_legacy_budget(self) -> None:
        manager = P2PNetworkManager.__new__(P2PNetworkManager)
        manager.peer_versions = {}
        manager._introduced_peers = {}
        manager.connection_manager = None
        manager.message_router = SimpleNamespace()
        manager.get_public_channels_for_sync = lambda: [
            {
                'id': f'C{i:03d}',
                'name': f'public-{i}',
                'type': 'public',
                'desc': 'x' * 30000,
                'origin_peer': 'peer-local',
                'privacy_mode': 'open',
            }
            for i in range(60)
        ]

        sent_batches: list[list[dict]] = []

        async def _send_channel_sync(peer_id, channels):
            sent_batches.append(channels)
            return True

        manager.message_router.send_channel_sync = _send_channel_sync

        asyncio.run(manager._send_channel_sync_to_peer('peer-legacy'))

        self.assertGreater(len(sent_batches), 1)
        for batch in sent_batches:
            payload = {'content': '', 'metadata': {'type': 'channel_sync', 'channels': batch}}
            self.assertLessEqual(
                len(json.dumps(payload).encode('utf-8')),
                int(LEGACY_BULK_SYNC_MAX_PAYLOAD_BYTES * 0.75),
            )

    def test_modern_peer_channel_sync_uses_higher_budget_capability(self) -> None:
        manager = P2PNetworkManager.__new__(P2PNetworkManager)
        manager.peer_versions = {'peer-modern': {'capabilities': [LEGACY_BULK_SYNC_CAPABILITY]}}
        manager._introduced_peers = {}
        manager.connection_manager = None

        target = manager._get_channel_sync_target_payload_bytes('peer-modern')

        self.assertEqual(target, CHANNEL_SYNC_TARGET_PAYLOAD_BYTES)

    def test_public_channel_metadata_replay_sends_small_per_channel_announces(self) -> None:
        manager = P2PNetworkManager.__new__(P2PNetworkManager)
        manager.message_router = SimpleNamespace()
        manager.get_public_channels_for_sync = lambda: [
            {
                'id': 'C001',
                'name': 'breaking-news',
                'type': 'public',
                'desc': 'news',
                'origin_peer': 'peer-origin',
                'privacy_mode': 'open',
                'post_policy': 'open',
                'allow_member_replies': True,
                'allowed_poster_user_ids': [],
                'last_activity_at': None,
                'lifecycle_ttl_days': 180,
                'lifecycle_preserved': False,
                'lifecycle_archived_at': None,
                'lifecycle_archive_reason': None,
            },
            {
                'id': 'C002',
                'name': 'general',
                'type': 'general',
                'desc': 'default',
                'origin_peer': '',
                'privacy_mode': 'open',
                'post_policy': 'open',
                'allow_member_replies': True,
                'allowed_poster_user_ids': [],
                'last_activity_at': None,
                'lifecycle_ttl_days': 180,
                'lifecycle_preserved': True,
                'lifecycle_archived_at': None,
                'lifecycle_archive_reason': None,
            },
        ]
        manager.get_peer_id = lambda: 'peer-local'

        sent = []

        async def _send_channel_announce(**kwargs):
            sent.append(kwargs)
            return True

        manager.message_router.send_channel_announce = _send_channel_announce

        asyncio.run(manager._send_public_channel_metadata_replay_to_peer('peer-guest'))

        self.assertEqual(len(sent), 2)
        self.assertEqual({item['channel_id'] for item in sent}, {'C001', 'C002'})
        self.assertTrue(all(item['to_peer'] == 'peer-guest' for item in sent))

    def test_targeted_public_channel_metadata_replay_filters_to_requested_ids(self) -> None:
        manager = P2PNetworkManager.__new__(P2PNetworkManager)
        manager.message_router = SimpleNamespace()
        manager.get_public_channels_for_sync = lambda: [
            {
                'id': 'C001',
                'name': 'breaking-news',
                'type': 'public',
                'desc': 'news',
                'origin_peer': 'peer-origin',
                'privacy_mode': 'open',
            },
            {
                'id': 'C002',
                'name': 'canopy-radio',
                'type': 'public',
                'desc': 'radio',
                'origin_peer': 'peer-origin',
                'privacy_mode': 'open',
            },
        ]
        manager.get_peer_id = lambda: 'peer-local'

        sent = []

        async def _send_channel_announce(**kwargs):
            sent.append(kwargs)
            return True

        manager.message_router.send_channel_announce = _send_channel_announce

        asyncio.run(
            manager._send_public_channel_metadata_replay_to_peer(
                'peer-guest',
                channel_ids=['C002'],
                reason='metadata_request_response',
                request_id='req-123',
            )
        )

        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]['channel_id'], 'C002')
        self.assertEqual(sent[0]['to_peer'], 'peer-guest')
        self.assertEqual(sent[0]['created_by_peer'], 'peer-origin')


if __name__ == '__main__':
    unittest.main()
