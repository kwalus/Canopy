"""Unit tests for API attachment normalization helpers."""

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

from canopy.api.routes import (
    _is_generic_upload_metadata,
    _normalize_channel_attachments,
)


class _FakeFileInfo:
    def __init__(self, file_id: str, name: str, ctype: str, size: int) -> None:
        self.id = file_id
        self.original_name = name
        self.content_type = ctype
        self.size = size


class _FakeFileManager:
    def __init__(self) -> None:
        self._items = {
            'F1': _FakeFileInfo('F1', 'report.md', 'text/markdown', 1234),
            'F2': _FakeFileInfo('F2', 'plot.pdf', 'application/pdf', 2048),
        }

    def get_file(self, file_id: str):
        return self._items.get(file_id)


class TestApiAttachmentNormalizationHelpers(unittest.TestCase):
    def test_generic_upload_metadata_detection(self) -> None:
        self.assertTrue(_is_generic_upload_metadata('file', 'application/octet-stream'))
        self.assertTrue(_is_generic_upload_metadata('upload', ''))
        self.assertFalse(_is_generic_upload_metadata('report.md', 'text/markdown'))

    def test_attachment_normalization_hydrates_from_file_info(self) -> None:
        file_manager = _FakeFileManager()
        normalized = _normalize_channel_attachments([{'id': 'F1'}], file_manager)
        self.assertEqual(len(normalized), 1)
        att = normalized[0]
        self.assertEqual(att.get('id'), 'F1')
        self.assertEqual(att.get('name'), 'report.md')
        self.assertEqual(att.get('type'), 'text/markdown')
        self.assertEqual(att.get('size'), 1234)

    def test_attachment_normalization_maps_alias_fields(self) -> None:
        file_manager = _FakeFileManager()
        normalized = _normalize_channel_attachments(
            [{'file_id': 'F2', 'filename': 'legacy.pdf', 'content_type': 'application/pdf'}],
            file_manager,
        )
        self.assertEqual(len(normalized), 1)
        att = normalized[0]
        self.assertEqual(att.get('id'), 'F2')
        self.assertEqual(att.get('file_id'), 'F2')
        self.assertEqual(att.get('name'), 'legacy.pdf')
        self.assertEqual(att.get('type'), 'application/pdf')

    def test_attachment_normalization_keeps_valid_layout_hint(self) -> None:
        file_manager = _FakeFileManager()
        normalized = _normalize_channel_attachments(
            [{'id': 'F1', 'layout_hint': ' HERO '}],
            file_manager,
        )
        self.assertEqual(normalized[0].get('layout_hint'), 'hero')

    def test_attachment_normalization_strips_invalid_layout_hint(self) -> None:
        file_manager = _FakeFileManager()
        normalized = _normalize_channel_attachments(
            [{'id': 'F1', 'layout_hint': 'masonry'}],
            file_manager,
        )
        self.assertNotIn('layout_hint', normalized[0])


if __name__ == '__main__':
    unittest.main()
