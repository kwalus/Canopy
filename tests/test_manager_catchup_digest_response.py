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

from canopy.network.manager import P2PNetworkManager


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


if __name__ == '__main__':
    unittest.main()
