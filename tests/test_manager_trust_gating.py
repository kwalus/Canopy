"""Unit tests for trusted-peer content delivery gates."""

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

        self.assertEqual(peers, ['peer-trusted', 'peer-unknown'])


if __name__ == '__main__':
    unittest.main()
