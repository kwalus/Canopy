"""Regression tests for spreadsheet attachment validation and preview support."""

import io
import os
import sys
import tempfile
import types
import unittest
import zipfile
from datetime import datetime, timezone
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

from openpyxl import Workbook

from canopy.api.routes import create_api_blueprint
from canopy.core.file_preview import build_file_preview
from canopy.core.files import FileInfo
from canopy.security.api_keys import ApiKeyInfo, Permission
from canopy.security.file_validation import validate_file_upload


def _build_workbook_bytes() -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = 'Budget'
    ws.append(['Item', 'Qty', 'Price'])
    ws.append(['Apples', 3, 1.25])
    ws.append(['Oranges', 2, 2.0])
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


class _FakeApiKeyManager:
    def validate_key(self, raw_key, required_permission=None):
        perms = {
            Permission.READ_FILES,
            Permission.READ_FEED,
            Permission.READ_MESSAGES,
        }
        if raw_key != 'test-key':
            return None
        if required_permission and required_permission not in perms:
            return None
        return ApiKeyInfo(
            id='key-1',
            user_id='user-owner',
            key_hash='hash',
            permissions=perms,
            created_at=datetime.now(timezone.utc),
        )


class _FakeDbManager:
    def get_instance_owner_user_id(self):
        return 'user-owner'

    def get_user(self, user_id):
        return {'id': user_id, 'origin_peer': None}


class _FakeFileManager:
    def __init__(self, file_bytes: bytes):
        self._file_bytes = file_bytes

    def get_file_data(self, file_id):
        return (
            self._file_bytes,
            FileInfo(
                id=file_id,
                original_name='budget.xlsx',
                stored_name='budget.xlsx',
                file_path='/tmp/budget.xlsx',
                content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                size=len(self._file_bytes),
                uploaded_by='user-owner',
                uploaded_at=datetime.now(timezone.utc),
                url=f'/files/{file_id}',
                checksum='checksum',
            ),
        )


class _AllowedAccess:
    allowed = True
    reason = None

    def to_dict(self):
        return {'allowed': True}


class TestSpreadsheetPreviewSupport(unittest.TestCase):
    def test_validate_file_upload_accepts_canopy_module_bundle(self):
        module_bytes = b"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Module</title>
</head>
<body>
  <div id="app">Piano Lab</div>
  <script>
    window.addEventListener('load', function () {
      document.getElementById('app').setAttribute('data-ready', '1');
    });
  </script>
</body>
</html>
"""
        is_valid, error, validated_type = validate_file_upload(
            module_bytes,
            'text/html',
            'piano-lab-v1.canopy-module.html',
        )
        self.assertTrue(is_valid, error)
        self.assertIsNone(error)
        self.assertEqual(validated_type, 'text/html')

    def test_validate_file_upload_accepts_canopy_module_with_generic_octet_stream_metadata(self):
        module_bytes = b"""<!doctype html>
<html><head><meta charset="utf-8"></head><body><script>window.x=1;</script></body></html>
"""
        is_valid, error, validated_type = validate_file_upload(
            module_bytes,
            'application/octet-stream',
            'lesson.canopy-module.html',
        )
        self.assertTrue(is_valid, error)
        self.assertEqual(validated_type, 'text/html')

    def test_validate_file_upload_rejects_canopy_module_with_external_script(self):
        module_bytes = b"""<!doctype html>
<html><head><meta charset="utf-8"></head><body>
<script src="https://example.com/app.js"></script>
</body></html>
"""
        is_valid, error, _ = validate_file_upload(
            module_bytes,
            'text/html',
            'unsafe.canopy-module.html',
        )
        self.assertFalse(is_valid)
        self.assertIn('external scripts', str(error).lower())

    def test_validate_file_upload_rejects_canopy_module_with_inline_event_handler(self):
        module_bytes = b"""<!doctype html>
<html><body><button onclick="alert('x')">Run</button></body></html>
"""
        is_valid, error, _ = validate_file_upload(
            module_bytes,
            'text/html',
            'unsafe.canopy-module.html',
        )
        self.assertFalse(is_valid)
        self.assertIn('inline event handler', str(error).lower())

    def test_validate_file_upload_rejects_canopy_module_with_external_image_source(self):
        module_bytes = b"""<!doctype html>
<html><body><img src="https://example.com/demo.png"></body></html>
"""
        is_valid, error, _ = validate_file_upload(
            module_bytes,
            'text/html',
            'unsafe.canopy-module.html',
        )
        self.assertFalse(is_valid)
        self.assertIn('self-contained', str(error).lower())

    def test_build_file_preview_disables_generic_preview_for_canopy_module(self):
        module_bytes = b"""<!doctype html><html><body><script>console.log('hi')</script></body></html>"""
        preview = build_file_preview(
            module_bytes,
            'piano-lab-v1.canopy-module.html',
            'text/html',
        )
        self.assertFalse(preview['previewable'])
        self.assertEqual(preview['kind'], 'module')
        self.assertIn('deck', preview['error'].lower())

    def test_validate_file_upload_accepts_real_xlsx(self):
        workbook_bytes = _build_workbook_bytes()
        is_valid, error, validated_type = validate_file_upload(
            workbook_bytes,
            'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            'budget.xlsx',
        )
        self.assertTrue(is_valid, error)
        self.assertIsNone(error)
        self.assertEqual(
            validated_type,
            'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        )

    def test_validate_file_upload_rejects_zip_masquerading_as_xlsx(self):
        out = io.BytesIO()
        with zipfile.ZipFile(out, 'w') as archive:
            archive.writestr('notes.txt', 'not a workbook')
        is_valid, error, _ = validate_file_upload(
            out.getvalue(),
            'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            'fake.xlsx',
        )
        self.assertFalse(is_valid)
        self.assertIn('malformed', str(error).lower())

    def test_build_file_preview_returns_workbook_grid(self):
        workbook_bytes = _build_workbook_bytes()
        preview = build_file_preview(
            workbook_bytes,
            'budget.xlsx',
            'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        )
        self.assertTrue(preview['previewable'])
        self.assertEqual(preview['kind'], 'spreadsheet')
        self.assertEqual(preview['sheet_count'], 1)
        self.assertEqual(preview['sheets'][0]['name'], 'Budget')
        self.assertEqual(preview['sheets'][0]['rows'][0][0]['display'], 'Item')
        self.assertEqual(preview['sheets'][0]['rows'][1][1]['display'], '3')

    def test_build_file_preview_marks_xlsm_as_macro_disabled(self):
        workbook_bytes = _build_workbook_bytes()
        preview = build_file_preview(
            workbook_bytes,
            'budget.xlsm',
            'application/vnd.ms-excel.sheet.macroenabled.12',
        )
        self.assertTrue(preview['previewable'])
        self.assertTrue(preview['macro_enabled'])
        self.assertIn('never executes', preview['warning'])

    def test_file_preview_api_returns_spreadsheet_payload(self):
        workbook_bytes = _build_workbook_bytes()
        app = Flask(__name__)
        app.config['TESTING'] = True
        app.secret_key = 'test-secret'

        components = (
            _FakeDbManager(),           # db_manager
            _FakeApiKeyManager(),       # api_key_manager
            MagicMock(),                # trust_manager
            MagicMock(),                # message_manager
            MagicMock(),                # channel_manager
            _FakeFileManager(workbook_bytes),  # file_manager
            MagicMock(),                # feed_manager
            MagicMock(),                # interaction_manager
            MagicMock(),                # profile_manager
            MagicMock(),                # config
            MagicMock(),                # p2p_manager
        )

        with patch('canopy.api.routes.get_app_components', return_value=components), \
             patch('canopy.api.routes.evaluate_file_access', return_value=_AllowedAccess()):
            api_bp = create_api_blueprint()
            app.register_blueprint(api_bp, url_prefix='/api/v1')
            client = app.test_client()
            response = client.get(
                '/api/v1/files/Fpreview/preview',
                headers={'X-API-Key': 'test-key'},
            )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload['success'])
        self.assertEqual(payload['kind'], 'spreadsheet')
        self.assertEqual(payload['file_id'], 'Fpreview')
        self.assertEqual(payload['sheets'][0]['rows'][0][0]['display'], 'Item')


if __name__ == '__main__':
    unittest.main()
