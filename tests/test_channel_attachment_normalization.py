"""Regression tests for channel attachment metadata normalization."""

import base64
import json
import unittest
from datetime import datetime, timezone

from canopy.core.channels import Message, MessageType
from canopy.core.large_attachments import (
    LARGE_ATTACHMENT_CHUNK_SIZE,
    coerce_remote_attachment_reference,
)
from canopy.network.routing import MAX_PAYLOAD_BYTES


class TestChannelAttachmentNormalization(unittest.TestCase):
    def test_to_dict_maps_upload_alias_keys_to_canonical_fields(self) -> None:
        msg = Message(
            id='Mabc123',
            channel_id='general',
            user_id='user_1',
            content='',
            message_type=MessageType.FILE,
            created_at=datetime.now(timezone.utc),
            attachments=[
                {
                    'file_id': 'Fmd001',
                    'filename': 'agent_note.md',
                    'content_type': 'text/markdown',
                    'size': '321',
                }
            ],
        )

        payload = msg.to_dict()
        self.assertIn('attachments', payload)
        self.assertEqual(len(payload['attachments']), 1)
        att = payload['attachments'][0]
        self.assertEqual(att.get('id'), 'Fmd001')
        self.assertEqual(att.get('file_id'), 'Fmd001')
        self.assertEqual(att.get('name'), 'agent_note.md')
        self.assertEqual(att.get('type'), 'text/markdown')
        self.assertEqual(att.get('size'), 321)

    def test_to_dict_normalizes_string_attachment_to_file_id(self) -> None:
        msg = Message(
            id='Mabc124',
            channel_id='general',
            user_id='user_1',
            content='',
            message_type=MessageType.FILE,
            created_at=datetime.now(timezone.utc),
            attachments=['Fraw002'],
        )

        payload = msg.to_dict()
        self.assertEqual(len(payload['attachments']), 1)
        att = payload['attachments'][0]
        self.assertEqual(att.get('id'), 'Fraw002')
        self.assertEqual(att.get('file_id'), 'Fraw002')
        self.assertEqual(att.get('name'), 'Fraw002')
        self.assertEqual(att.get('type'), 'application/octet-stream')

    def test_coerce_remote_attachment_reference_upgrades_legacy_metadata_only_attachment(self) -> None:
        normalized = coerce_remote_attachment_reference(
            {
                'id': 'Fold123',
                'name': 'pasted-image.png',
                'type': 'image/png',
                'size': 204800,
            },
            default_source_peer_id='peer-legacy',
        )

        self.assertIsNotNone(normalized)
        assert normalized is not None
        self.assertEqual(normalized.get('origin_file_id'), 'Fold123')
        self.assertEqual(normalized.get('source_peer_id'), 'peer-legacy')
        self.assertTrue(normalized.get('large_attachment'))
        self.assertEqual(normalized.get('storage_mode'), 'remote_large')
        self.assertEqual(normalized.get('download_status'), 'pending')

    def test_large_attachment_chunk_message_fits_under_router_payload_cap(self) -> None:
        data_b64 = base64.b64encode(b'x' * LARGE_ATTACHMENT_CHUNK_SIZE).decode('ascii')
        wire_message = {
            'type': 'p2p_message',
            'message': {
                'id': 'chunk-msg',
                'type': 'large_attachment_chunk',
                'from_peer': 'peer-source',
                'to_peer': 'peer-target',
                'timestamp': 0,
                'ttl': 4,
                'signature': 'sig',
                'encrypted_payload': None,
                'payload': {
                    'content': '',
                    'metadata': {
                        'type': 'large_attachment_chunk',
                        'request_id': 'LAR123',
                        'origin_file_id': 'Forigin123',
                        'file_name': 'pasted-image.png',
                        'content_type': 'image/png',
                        'checksum': 'abc123',
                        'size': LARGE_ATTACHMENT_CHUNK_SIZE,
                        'uploaded_by': 'user_1',
                        'chunk_index': 0,
                        'total_chunks': 4,
                        'data': data_b64,
                        'source_peer_id': 'peer-source',
                    },
                },
            },
        }
        serialized = json.dumps(wire_message, separators=(',', ':')).encode('utf-8')
        self.assertLess(len(serialized), MAX_PAYLOAD_BYTES)


if __name__ == '__main__':
    unittest.main()
