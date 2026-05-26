"""Regression tests for spreadsheet attachment validation and preview support."""

import gzip
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


def _build_docx_bytes(text: str = 'Quarterly planning memo') -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w') as archive:
        archive.writestr('[Content_Types].xml', '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>')
        archive.writestr('word/document.xml', f'''<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body>
</w:document>''')
    return out.getvalue()


def _build_pptx_bytes() -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w') as archive:
        archive.writestr('[Content_Types].xml', '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>')
        archive.writestr('ppt/presentation.xml', '<p:presentation xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"/>')
        archive.writestr('ppt/slides/slide1.xml', '''<?xml version="1.0" encoding="UTF-8"?>
<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"
       xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">
  <p:cSld><p:spTree><p:sp><p:txBody><a:p><a:r><a:t>Demo agenda</a:t></a:r></a:p></p:txBody></p:sp></p:spTree></p:cSld>
</p:sld>''')
    return out.getvalue()


def _build_odt_bytes(text: str = 'OpenDocument briefing note') -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w') as archive:
        archive.writestr('mimetype', 'application/vnd.oasis.opendocument.text')
        archive.writestr('content.xml', f'''<?xml version="1.0" encoding="UTF-8"?>
<office:document-content xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
 xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0">
  <office:body><office:text><text:p>{text}</text:p></office:text></office:body>
</office:document-content>''')
    return out.getvalue()


def _build_ods_bytes() -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w') as archive:
        archive.writestr('mimetype', 'application/vnd.oasis.opendocument.spreadsheet')
        archive.writestr('content.xml', '''<?xml version="1.0" encoding="UTF-8"?>
<office:document-content xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
 xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0"
 xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0">
  <office:body><office:spreadsheet>
    <table:table table:name="Budget">
      <table:table-row>
        <table:table-cell><text:p>Item</text:p></table:table-cell>
        <table:table-cell><text:p>Cost</text:p></table:table-cell>
      </table:table-row>
      <table:table-row>
        <table:table-cell><text:p>Compute</text:p></table:table-cell>
        <table:table-cell><text:p>1200</text:p></table:table-cell>
      </table:table-row>
    </table:table>
  </office:spreadsheet></office:body>
</office:document-content>''')
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

    def test_validate_file_upload_accepts_canopy_module_webgl_declaration(self):
        module_bytes = b"""<!doctype html>
<html><head>
  <meta charset="utf-8">
  <meta name="canopy-module-required-capabilities" content="module.render.webgl">
</head><body>
  <canvas id="viz"></canvas>
  <script>
    const gl = document.getElementById('viz').getContext('webgl2')
      || document.getElementById('viz').getContext('webgl');
    window.webglReady = !!gl;
  </script>
</body></html>
"""
        is_valid, error, validated_type = validate_file_upload(
            module_bytes,
            'text/html',
            'isosurface.canopy-module.html',
        )
        self.assertTrue(is_valid, error)
        self.assertEqual(validated_type, 'text/html')

    def test_validate_file_upload_accepts_canopy_module_source_attachment_read_declaration(self):
        module_bytes = b"""<!doctype html>
<html><head>
  <meta charset="utf-8">
  <meta name="canopy-module-required-capabilities" content="source.attachments.read">
</head><body>
  <script>
    async function loadData() {
      const listing = await window.CanopyModule.source.attachments.list();
      const first = (listing.attachments || [])[0];
      return first ? window.CanopyModule.source.attachments.readText(first.attachment_id) : '';
    }
  </script>
</body></html>
"""
        is_valid, error, validated_type = validate_file_upload(
            module_bytes,
            'text/html',
            'data-loader.canopy-module.html',
        )
        self.assertTrue(is_valid, error)
        self.assertEqual(validated_type, 'text/html')

    def test_validate_file_upload_accepts_canopy_module_wasm_declaration(self):
        module_bytes = b"""<!doctype html>
<html><head>
  <meta charset="utf-8">
  <meta name="canopy-module-required-capabilities" content="module.render.wasm source.attachments.read">
</head><body>
  <script>
    async function boot(bytes) {
      return WebAssembly.instantiate(bytes, {});
    }
  </script>
</body></html>
"""
        is_valid, error, validated_type = validate_file_upload(
            module_bytes,
            'text/html',
            'doom-loader.canopy-module.html',
        )
        self.assertTrue(is_valid, error)
        self.assertEqual(validated_type, 'text/html')

    def test_validate_file_upload_accepts_gzip_runtime_asset_with_generic_metadata(self):
        asset_bytes = gzip.compress(b'wasm-runtime-bytes')
        is_valid, error, validated_type = validate_file_upload(
            asset_bytes,
            'application/octet-stream',
            'doom-runtime.gz',
        )
        self.assertTrue(is_valid, error)
        self.assertEqual(validated_type, 'application/gzip')

    def test_validate_file_upload_accepts_python_source_with_generic_metadata(self):
        source_bytes = b"def hello(name: str) -> str:\n    return f'hello {name}'\n"
        is_valid, error, validated_type = validate_file_upload(
            source_bytes,
            'application/octet-stream',
            'agent_tool.py',
        )
        self.assertTrue(is_valid, error)
        self.assertEqual(validated_type, 'text/x-python')

    def test_validate_file_upload_accepts_python_source_with_mime_alias(self):
        source_bytes = b"from __future__ import annotations\n\nprint('canopy')\n"
        is_valid, error, validated_type = validate_file_upload(
            source_bytes,
            'application/x-python-code; charset=utf-8',
            'shared_patch.py',
        )
        self.assertTrue(is_valid, error)
        self.assertEqual(validated_type, 'text/x-python')

    def test_validate_file_upload_rejects_binary_python_source(self):
        is_valid, error, _ = validate_file_upload(
            b'\x00\x01\x02not-python',
            'text/x-python',
            'evil.py',
        )
        self.assertFalse(is_valid)
        self.assertIn('binary data', str(error).lower())

    def test_validate_file_upload_keeps_python_source_cap_when_global_override_is_larger(self):
        oversized_source = b'#' * (2 * 1024 * 1024 + 1)
        is_valid, error, _ = validate_file_upload(
            oversized_source,
            'text/x-python',
            'large_agent_tool.py',
            max_size_override=100 * 1024 * 1024,
        )
        self.assertFalse(is_valid)
        self.assertIn('exceeds maximum', str(error).lower())

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

    def test_validate_file_upload_allows_js_event_property_assignments_inside_script(self):
        module_bytes = b"""<!doctype html>
<html><body><script>
const worker = {};
worker.onmessage = function () { return 'ok'; };
const xhr = {};
xhr.onload = function () { return 'ok'; };
const img = new Image();
img.src = blobUrl;
</script></body></html>
"""
        is_valid, error, validated_type = validate_file_upload(
            module_bytes,
            'text/html',
            'emscripten-loader.canopy-module.html',
        )
        self.assertTrue(is_valid, error)
        self.assertIsNone(error)
        self.assertEqual(validated_type, 'text/html')

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

    def test_build_file_preview_returns_code_language_metadata(self):
        preview = build_file_preview(
            b"def summarize(items):\n    return {'count': len(items)}\n",
            'agent_tool.py',
            'text/x-python; charset=utf-8',
        )
        self.assertTrue(preview['previewable'])
        self.assertEqual(preview['kind'], 'code')
        self.assertEqual(preview['language'], 'python')
        self.assertEqual(preview['language_label'], 'Python')
        self.assertIn('summarize', preview['text'])

    def test_build_file_preview_returns_plain_text_metadata_for_notes(self):
        preview = build_file_preview(
            b'Plain handoff note for the next agent.',
            'handoff.txt',
            'text/plain',
        )
        self.assertTrue(preview['previewable'])
        self.assertEqual(preview['kind'], 'text')
        self.assertEqual(preview['language'], 'plain')
        self.assertEqual(preview['language_label'], 'Plain text')

    def test_validate_file_upload_accepts_utf8_source_code_formats(self):
        cases = [
            (b"export const answer = 42;\n", 'application/octet-stream', 'solver.ts', 'application/typescript'),
            (b"fn main() { println!(\"hi\"); }\n", 'application/octet-stream', 'main.rs', 'text/x-rust'),
            (b"package main\nfunc main() {}\n", 'text/x-go', 'worker.go', 'text/x-go'),
        ]
        for file_data, content_type, filename, expected_type in cases:
            with self.subTest(filename=filename):
                is_valid, error, validated_type = validate_file_upload(file_data, content_type, filename)
                self.assertTrue(is_valid, error)
                self.assertIsNone(error)
                self.assertEqual(validated_type, expected_type)

    def test_validate_file_upload_rejects_binary_source_code_payload(self):
        is_valid, error, _ = validate_file_upload(
            b'const ok = true;\x00\xff',
            'application/octet-stream',
            'bad.js',
        )
        self.assertFalse(is_valid)
        self.assertIn('source file', str(error).lower())

    def test_validate_file_upload_rejects_invalid_utf8_source_like_text_plain(self):
        is_valid, error, _ = validate_file_upload(
            b'fun main() {\xff}\n',
            'text/plain',
            'worker.kt',
        )
        self.assertFalse(is_valid)
        self.assertIn('valid utf-8', str(error).lower())

    def test_validate_file_upload_accepts_docx_with_generic_metadata(self):
        docx_bytes = _build_docx_bytes()
        is_valid, error, validated_type = validate_file_upload(
            docx_bytes,
            'application/octet-stream',
            'planning-memo.docx',
        )
        self.assertTrue(is_valid, error)
        self.assertEqual(
            validated_type,
            'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        )

    def test_validate_file_upload_rejects_zip_masquerading_as_docx(self):
        out = io.BytesIO()
        with zipfile.ZipFile(out, 'w') as archive:
            archive.writestr('notes.txt', 'not a word document')
        is_valid, error, _ = validate_file_upload(
            out.getvalue(),
            'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
            'fake.docx',
        )
        self.assertFalse(is_valid)
        self.assertIn('word document', str(error).lower())

    def test_build_file_preview_returns_docx_text(self):
        docx_bytes = _build_docx_bytes('Review milestone and owner list')
        preview = build_file_preview(
            docx_bytes,
            'planning-memo.docx',
            'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        )
        self.assertTrue(preview['previewable'])
        self.assertEqual(preview['kind'], 'document')
        self.assertIn('Review milestone', preview['text'])

    def test_build_file_preview_returns_pptx_slide_text(self):
        pptx_bytes = _build_pptx_bytes()
        preview = build_file_preview(
            pptx_bytes,
            'demo-brief.pptx',
            'application/vnd.openxmlformats-officedocument.presentationml.presentation',
        )
        self.assertTrue(preview['previewable'])
        self.assertEqual(preview['kind'], 'document')
        self.assertIn('Slide 1', preview['text'])
        self.assertIn('Demo agenda', preview['text'])

    def test_build_file_preview_returns_rtf_text(self):
        preview = build_file_preview(
            b'{\\rtf1\\ansi Business handoff note\\par Next step}',
            'handoff.rtf',
            'application/rtf',
        )
        self.assertTrue(preview['previewable'])
        self.assertEqual(preview['kind'], 'document')
        self.assertIn('Business handoff note', preview['text'])

    def test_build_file_preview_returns_odt_text(self):
        odt_bytes = _build_odt_bytes()
        preview = build_file_preview(
            odt_bytes,
            'briefing.odt',
            'application/vnd.oasis.opendocument.text',
        )
        self.assertTrue(preview['previewable'])
        self.assertEqual(preview['kind'], 'document')
        self.assertIn('OpenDocument briefing note', preview['text'])

    def test_build_file_preview_returns_ods_grid_and_text(self):
        ods_bytes = _build_ods_bytes()
        preview = build_file_preview(
            ods_bytes,
            'budget.ods',
            'application/vnd.oasis.opendocument.spreadsheet',
        )
        self.assertTrue(preview['previewable'])
        self.assertEqual(preview['kind'], 'spreadsheet')
        self.assertEqual(preview['sheets'][0]['name'], 'Budget')
        self.assertEqual(preview['sheets'][0]['rows'][1][0]['display'], 'Compute')
        self.assertIn('Compute', preview['text'])

    def test_validate_file_upload_accepts_ods_with_generic_metadata(self):
        ods_bytes = _build_ods_bytes()
        is_valid, error, validated_type = validate_file_upload(
            ods_bytes,
            'application/octet-stream',
            'budget.ods',
        )
        self.assertTrue(is_valid, error)
        self.assertEqual(validated_type, 'application/vnd.oasis.opendocument.spreadsheet')

    def test_build_file_preview_returns_eml_text(self):
        preview = build_file_preview(
            b"From: lead@example.test\r\nTo: team@example.test\r\nSubject: Launch plan\r\n\r\nPlease review the Q3 launch plan.",
            'launch-plan.eml',
            'message/rfc822',
        )
        self.assertTrue(preview['previewable'])
        self.assertEqual(preview['kind'], 'document')
        self.assertEqual(preview['document_format'], 'eml')
        self.assertIn('Q3 launch plan', preview['text'])

    def test_build_file_preview_returns_html_only_eml_text(self):
        preview = build_file_preview(
            b"From: lead@example.test\r\nTo: team@example.test\r\nSubject: HTML plan\r\nContent-Type: text/html; charset=utf-8\r\n\r\n<html><body><h1>Roadmap</h1><p>Please review the Q4 launch plan.</p><script>ignore()</script></body></html>",
            'html-plan.eml',
            'message/rfc822',
        )
        self.assertTrue(preview['previewable'])
        self.assertEqual(preview['kind'], 'document')
        self.assertIn('Q4 launch plan', preview['text'])
        self.assertNotIn('ignore()', preview['text'])

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

    def test_build_file_preview_returns_csv_grid_with_raw_text(self):
        preview = build_file_preview(
            b"item,count\nalpha,3\nbeta,5\n",
            'agent-results.csv',
            'text/csv',
        )
        self.assertTrue(preview['previewable'])
        self.assertEqual(preview['kind'], 'spreadsheet')
        self.assertEqual(preview['sheets'][0]['rows'][1][0]['display'], 'alpha')
        self.assertIn('item,count', preview['text'])
        self.assertIn('max_chars', preview['limits'])

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
