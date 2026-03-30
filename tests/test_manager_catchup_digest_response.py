"""Tests for digest-only catch-up response handling in P2P manager."""

import os
import sys
import types
import unittest

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

from canopy.network.manager import (
    CATCHUP_EXTRA_TARGET_PAYLOAD_BYTES,
    LEGACY_BULK_SYNC_MAX_PAYLOAD_BYTES,
    P2PNetworkManager,
)


class _DummyRouter:
    def __init__(self) -> None:
        self.calls = []

    async def send_catchup_response(self, peer_id, messages, extra_data=None):
        self.calls.append((peer_id, list(messages or []), extra_data))
        return True


class TestManagerCatchupDigestResponse(unittest.IsolatedAsyncioTestCase):
    async def test_digest_only_extra_data_is_sent(self) -> None:
        manager = P2PNetworkManager.__new__(P2PNetworkManager)
        manager._running = True
        manager.message_router = _DummyRouter()
        manager.peer_versions = {}
        manager._introduced_peers = {}
        manager.connection_manager = None

        await manager.send_catchup_response_async(
            peer_id='peer-remote',
            messages=[],
            extra_data={
                'digest': {
                    'version': 1,
                    'channels': {'general': {'status': 'match', 'remote_root': 'abc'}},
                }
            },
        )

        self.assertEqual(len(manager.message_router.calls), 1)
        peer_id, sent_messages, extra_data = manager.message_router.calls[0]
        self.assertEqual(peer_id, 'peer-remote')
        self.assertEqual(sent_messages, [])
        self.assertIn('digest', extra_data)

    async def test_large_extra_data_is_split_under_payload_budget(self) -> None:
        manager = P2PNetworkManager.__new__(P2PNetworkManager)
        manager._running = True
        manager.message_router = _DummyRouter()
        manager.peer_versions = {}
        manager._introduced_peers = {}
        manager.connection_manager = None

        large_posts = [
            {
                'id': f'feed-{idx}',
                'author_id': 'user-1',
                'content': 'x' * (180 * 1024),
                'visibility': 'network',
                'created_at': f'2026-03-30T12:{idx:02d}:00+00:00',
            }
            for idx in range(8)
        ]

        await manager.send_catchup_response_async(
            peer_id='peer-remote',
            messages=[],
            extra_data={
                'digest': {
                    'version': 1,
                    'channels': {'general': {'status': 'match', 'remote_root': 'abc'}},
                },
                'feed_posts': large_posts,
            },
        )

        self.assertGreater(len(manager.message_router.calls), 1)
        digest_chunks = 0
        total_posts = 0
        for peer_id, sent_messages, extra_data in manager.message_router.calls:
            self.assertEqual(peer_id, 'peer-remote')
            self.assertEqual(sent_messages, [])
            payload_bytes = manager._estimate_catchup_response_payload_bytes([], extra_data)
            self.assertLessEqual(payload_bytes, CATCHUP_EXTRA_TARGET_PAYLOAD_BYTES)
            if 'digest' in (extra_data or {}):
                digest_chunks += 1
            total_posts += len((extra_data or {}).get('feed_posts') or [])

        self.assertEqual(digest_chunks, 1)
        self.assertEqual(total_posts, len(large_posts))

    async def test_legacy_peer_extra_data_uses_legacy_budget(self) -> None:
        manager = P2PNetworkManager.__new__(P2PNetworkManager)
        manager._running = True
        manager.message_router = _DummyRouter()
        manager.peer_versions = {}
        manager._introduced_peers = {}
        manager.connection_manager = None

        large_posts = [
            {
                'id': f'feed-{idx}',
                'author_id': 'user-1',
                'content': 'x' * (140 * 1024),
                'visibility': 'network',
                'created_at': f'2026-03-30T12:{idx:02d}:00+00:00',
            }
            for idx in range(8)
        ]

        await manager.send_catchup_response_async(
            peer_id='peer-legacy',
            messages=[],
            extra_data={'feed_posts': large_posts},
        )

        legacy_target = max(64 * 1024, LEGACY_BULK_SYNC_MAX_PAYLOAD_BYTES - (64 * 1024))
        self.assertGreater(len(manager.message_router.calls), 1)
        for _, sent_messages, extra_data in manager.message_router.calls:
            self.assertEqual(sent_messages, [])
            payload_bytes = manager._estimate_catchup_response_payload_bytes([], extra_data)
            self.assertLessEqual(payload_bytes, legacy_target)


if __name__ == '__main__':
    unittest.main()
