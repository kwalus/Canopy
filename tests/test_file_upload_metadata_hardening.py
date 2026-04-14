"""Regression tests for generic upload metadata normalization."""

import os
import sqlite3
import sys
import tempfile
import types
import unittest
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import io
from pathlib import Path

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

from canopy.core.files import FileManager


class _FakeDbManager:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    @contextmanager
    def get_connection(self, *args, **kwargs):
        yield self.conn


class TestFileUploadMetadataHardening(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.storage_root = Path(self.tempdir.name) / "files"

        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("CREATE TABLE users (id TEXT PRIMARY KEY)")
        self.conn.execute("INSERT INTO users (id) VALUES (?)", ('user-test',))
        self.conn.commit()

        self.file_manager = FileManager(_FakeDbManager(self.conn), str(self.storage_root))

    def tearDown(self) -> None:
        self.conn.close()

    def test_generic_pdf_upload_is_normalized(self) -> None:
        pdf_bytes = b"%PDF-1.5\n%test\n1 0 obj\n<<>>\nendobj\n"
        info = self.file_manager.save_file(
            file_data=pdf_bytes,
            original_name='file',
            content_type='application/octet-stream',
            uploaded_by='user-test',
        )
        self.assertIsNotNone(info)
        assert info is not None
        self.assertEqual(info.content_type, 'application/pdf')
        self.assertTrue(info.original_name.endswith('.pdf'))
        self.assertEqual(info.original_name, 'file.pdf')

    def test_generic_markdown_upload_is_normalized(self) -> None:
        md_bytes = b"# Title\\n\\n- item 1\\n- item 2\\n"
        info = self.file_manager.save_file(
            file_data=md_bytes,
            original_name='file',
            content_type='application/octet-stream',
            uploaded_by='user-test',
        )
        self.assertIsNotNone(info)
        assert info is not None
        self.assertEqual(info.content_type, 'text/markdown')
        self.assertTrue(info.original_name.endswith('.md'))
        self.assertEqual(info.original_name, 'file.md')

    def test_get_file_backfills_legacy_generic_metadata(self) -> None:
        file_id = "Flegacymeta001"
        payload = b"# Backfill Test\\n\\nLegacy markdown body\\n"
        checksum = hashlib.sha256(payload).hexdigest()
        disk_path = self.storage_root / "documents" / f"{file_id}.bin"
        disk_path.parent.mkdir(parents=True, exist_ok=True)
        disk_path.write_bytes(payload)

        self.conn.execute(
            """
            INSERT INTO files (
                id, original_name, stored_name, file_path, content_type,
                size, uploaded_by, uploaded_at, checksum
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                file_id,
                "file",
                f"{file_id}.bin",
                str(disk_path),
                "application/octet-stream",
                len(payload),
                "user-test",
                datetime.now(timezone.utc).isoformat(),
                checksum,
            ),
        )
        self.conn.commit()

        info = self.file_manager.get_file(file_id)
        self.assertIsNotNone(info)
        assert info is not None
        self.assertEqual(info.original_name, "file.md")
        self.assertEqual(info.content_type, "text/markdown")

        row = self.conn.execute(
            "SELECT original_name, content_type FROM files WHERE id = ?",
            (file_id,),
        ).fetchone()
        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual(row["original_name"], "file.md")
        self.assertEqual(row["content_type"], "text/markdown")

    def test_image_thumbnail_applies_exif_orientation(self) -> None:
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("Pillow unavailable")

        image = Image.new("RGB", (1200, 800), (32, 96, 160))
        exif = Image.Exif()
        exif[274] = 6  # rotate 90 degrees clockwise for display
        raw = io.BytesIO()
        image.save(raw, format="JPEG", exif=exif)

        info = self.file_manager.save_file(
            file_data=raw.getvalue(),
            original_name="phone-portrait.jpg",
            content_type="image/jpeg",
            uploaded_by="user-test",
        )
        self.assertIsNotNone(info)
        assert info is not None

        thumb = self.file_manager.get_thumbnail_data(info.id)
        self.assertIsNotNone(thumb)
        assert thumb is not None
        thumb_bytes, _ = thumb
        opened = Image.open(io.BytesIO(thumb_bytes))
        self.assertGreater(opened.size[1], opened.size[0])


if __name__ == '__main__':
    unittest.main()
