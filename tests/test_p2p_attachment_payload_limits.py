"""Regression tests for conservative P2P attachment inlining."""

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

from canopy.network.manager import P2PNetworkManager, P2P_INLINE_ATTACHMENT_MAX_BYTES


class _FakeFileManager:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def get_file_data(self, file_id: str):
        return self.payload, SimpleNamespace(
            id=file_id,
            original_name='sample.bin',
            content_type='application/octet-stream',
            size=len(self.payload),
            checksum='abc123',
        )


class TestP2PAttachmentPayloadLimits(unittest.TestCase):
    def _make_manager(self, payload: bytes) -> P2PNetworkManager:
        manager = P2PNetworkManager.__new__(P2PNetworkManager)
        manager.file_manager = _FakeFileManager(payload)
        manager.get_peer_id = lambda: 'peer-local'
        return manager

    def test_small_attachment_stays_inlined(self) -> None:
        manager = self._make_manager(b'hello world')

        entry = manager._build_p2p_attachment_entry({'id': 'Fsmall', 'name': 'hello.txt'})

        self.assertIsNotNone(entry)
        assert entry is not None
        self.assertEqual(entry.get('id'), 'Fsmall')
        self.assertIn('data', entry)
        self.assertNotIn('large_attachment', entry)

    def test_attachment_above_inline_cap_uses_metadata_reference(self) -> None:
        manager = self._make_manager(b'x' * (P2P_INLINE_ATTACHMENT_MAX_BYTES + 1))

        entry = manager._build_p2p_attachment_entry({'id': 'Flarge', 'name': 'large.bin'})

        self.assertIsNotNone(entry)
        assert entry is not None
        self.assertNotIn('data', entry)
        self.assertTrue(entry.get('large_attachment'))
        self.assertEqual(entry.get('origin_file_id'), 'Flarge')
        self.assertEqual(entry.get('source_peer_id'), 'peer-local')
