"""Regression tests for bounded P2P attachment inlining."""

import os
import sys
import types
import unittest
from types import SimpleNamespace
from typing import Dict, Optional

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
    ATTACHMENT_PAYLOAD_TARGET_BYTES,
    P2PNetworkManager,
    P2P_INLINE_ATTACHMENT_MAX_BYTES,
)
from canopy.network.routing import MAX_PAYLOAD_BYTES


class _FakeFileManager:
    def __init__(self, payload: Optional[bytes] = None, payloads: Optional[Dict[str, bytes]] = None) -> None:
        self.payload = payload if payload is not None else b''
        self.payloads = dict(payloads or {})

    def get_file_data(self, file_id: str):
        payload = self.payloads.get(file_id, self.payload)
        return payload, SimpleNamespace(
            id=file_id,
            original_name='sample.bin',
            content_type='image/png' if file_id.endswith('.png') or file_id.startswith('Fimg') else 'application/octet-stream',
            size=len(payload),
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

    def test_existing_remote_reference_survives_without_local_file_id(self) -> None:
        manager = P2PNetworkManager.__new__(P2PNetworkManager)
        manager.file_manager = None
        manager.get_peer_id = lambda: 'peer-local'

        entry = manager._build_p2p_attachment_entry({
            'name': 'thumb_homies-claw-avatar.png',
            'type': 'image/png',
            'size': 863841,
            'checksum': 'abc123',
            'origin_file_id': 'Forigin-remote',
            'source_peer_id': 'peer-remote',
            'large_attachment': True,
            'storage_mode': 'remote_large',
            'download_status': 'pending',
        })

        self.assertIsNotNone(entry)
        assert entry is not None
        self.assertEqual(entry.get('origin_file_id'), 'Forigin-remote')
        self.assertEqual(entry.get('source_peer_id'), 'peer-remote')
        self.assertTrue(entry.get('large_attachment'))
        self.assertEqual(entry.get('storage_mode'), 'remote_large')
        self.assertEqual(entry.get('download_status'), 'pending')
        self.assertEqual(entry.get('checksum'), 'abc123')
        self.assertNotIn('data', entry)

    def test_existing_remote_reference_survives_missing_local_blob(self) -> None:
        class _MissingFileManager:
            def get_file_data(self, file_id: str):
                return None

        manager = P2PNetworkManager.__new__(P2PNetworkManager)
        manager.file_manager = _MissingFileManager()
        manager.get_peer_id = lambda: 'peer-local'

        entry = manager._build_p2p_attachment_entry({
            'id': 'Flocal-missing',
            'name': 'thumb_homies-claw-avatar.png',
            'type': 'image/png',
            'size': 863841,
            'origin_file_id': 'Forigin-remote',
            'source_peer_id': 'peer-remote',
            'large_attachment': True,
        })

        self.assertIsNotNone(entry)
        assert entry is not None
        self.assertEqual(entry.get('id'), 'Flocal-missing')
        self.assertEqual(entry.get('origin_file_id'), 'Forigin-remote')
        self.assertEqual(entry.get('source_peer_id'), 'peer-remote')
        self.assertTrue(entry.get('large_attachment'))

    def test_force_metadata_only_skips_inline_blob(self) -> None:
        manager = self._make_manager(b'x' * 1024)

        entry = manager._build_p2p_attachment_entry(
            {'id': 'Fsmall', 'name': 'image.png', 'type': 'image/png'},
            force_metadata_only=True,
        )

        self.assertIsNotNone(entry)
        assert entry is not None
        self.assertNotIn('data', entry)
        self.assertTrue(entry.get('large_attachment'))
        self.assertEqual(entry.get('origin_file_id'), 'Fsmall')
        self.assertEqual(entry.get('source_peer_id'), 'peer-local')

    def test_payload_budget_demotes_inline_image_blobs(self) -> None:
        payload_map = {
            'Fimg1': b'a' * (220 * 1024),
            'Fimg2': b'b' * (220 * 1024),
            'Fimg3': b'c' * (220 * 1024),
            'Fimg4': b'd' * (220 * 1024),
        }
        manager = P2PNetworkManager.__new__(P2PNetworkManager)
        manager.file_manager = _FakeFileManager(payloads=payload_map)
        manager.get_peer_id = lambda: 'peer-local'

        attachment_container = {}
        attachments = [
            {'id': file_id, 'name': f'{file_id}.png', 'type': 'image/png', 'size': len(payload)}
            for file_id, payload in payload_map.items()
        ]

        entries = manager._prepare_p2p_attachment_entries(
            content='Image-heavy message',
            attachment_container=attachment_container,
            attachments=attachments,
            context_label='test:image_budget',
        )

        payload_size = manager._estimate_message_payload_bytes('Image-heavy message', attachment_container)
        self.assertLessEqual(payload_size, ATTACHMENT_PAYLOAD_TARGET_BYTES)
        self.assertLessEqual(payload_size, MAX_PAYLOAD_BYTES)
        self.assertTrue(any(not entry.get('data') and entry.get('large_attachment') for entry in entries))
        self.assertTrue(any(entry.get('data') for entry in entries))
