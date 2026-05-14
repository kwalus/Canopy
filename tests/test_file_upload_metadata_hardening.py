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
import json
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
        self.conn.execute("CREATE TABLE channel_messages (id TEXT PRIMARY KEY, attachments TEXT, content TEXT)")
        self.conn.execute("CREATE TABLE feed_posts (id TEXT PRIMARY KEY, metadata TEXT, content TEXT)")
        self.conn.execute("CREATE TABLE messages (id TEXT PRIMARY KEY, metadata TEXT, content TEXT)")
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

    def test_python_source_upload_is_normalized_and_saved_as_document(self) -> None:
        py_bytes = b"def hello(name: str) -> str:\n    return f'hello {name}'\n"
        info = self.file_manager.save_file(
            file_data=py_bytes,
            original_name='agent_tool.py',
            content_type='application/octet-stream',
            uploaded_by='user-test',
        )
        self.assertIsNotNone(info)
        assert info is not None
        self.assertEqual(info.content_type, 'text/x-python')
        self.assertEqual(info.original_name, 'agent_tool.py')
        self.assertIn('/documents/', info.file_path.replace('\\', '/'))

    def test_file_manager_rejects_binary_python_source(self) -> None:
        info = self.file_manager.save_file(
            file_data=b'\x00\x01\x02not-python',
            original_name='evil.py',
            content_type='application/octet-stream',
            uploaded_by='user-test',
        )
        self.assertIsNone(info)

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

    def test_user_file_vault_lists_searches_and_counts_owned_files(self) -> None:
        self.conn.execute("INSERT INTO users (id) VALUES (?)", ('other-user',))
        report = self.file_manager.save_file(
            file_data=b"%PDF-1.5\nvault report\n",
            original_name='vault-report.pdf',
            content_type='application/pdf',
            uploaded_by='user-test',
        )
        image = self.file_manager.save_file(
            file_data=(
                b'\x89PNG\r\n\x1a\n'
                b'\x00\x00\x00\rIHDR'
                b'\x00\x00\x00\x01\x00\x00\x00\x01'
                b'\x08\x02\x00\x00\x00\x90wS\xde'
                b'\x00\x00\x00\x00IEND\xaeB`\x82'
            ),
            original_name='diagram.png',
            content_type='image/png',
            uploaded_by='user-test',
        )
        other = self.file_manager.save_file(
            file_data=b"not yours",
            original_name='private-other.txt',
            content_type='text/plain',
            uploaded_by='other-user',
        )
        self.assertIsNotNone(report)
        self.assertIsNotNone(image)
        self.assertIsNotNone(other)

        all_files = self.file_manager.list_user_files('user-test', limit=10)
        self.assertEqual({f.original_name for f in all_files}, {'vault-report.pdf', 'diagram.png'})

        searched = self.file_manager.list_user_files('user-test', query='report', limit=10)
        self.assertEqual([f.original_name for f in searched], ['vault-report.pdf'])

        images = self.file_manager.list_user_files('user-test', category='images', limit=10)
        self.assertEqual([f.original_name for f in images], ['diagram.png'])

        stats = self.file_manager.count_user_files('user-test')
        self.assertEqual(stats['count'], 2)
        self.assertEqual(stats['by_category']['documents']['count'], 1)
        self.assertEqual(stats['by_category']['images']['count'], 1)

    def test_vault_file_id_counts_as_existing_reference(self) -> None:
        file_id = 'Fvaultref001'
        self.conn.execute(
            "INSERT INTO channel_messages (id, attachments, content) VALUES (?, ?, ?)",
            ('M-vault', json.dumps([{'vault_file_id': file_id, 'name': 'vault.pdf'}]), ''),
        )
        self.conn.commit()

        self.assertTrue(self.file_manager.is_file_referenced(file_id))

    def test_user_file_vault_folders_are_owner_scoped_and_move_files(self) -> None:
        self.conn.execute("INSERT INTO users (id) VALUES (?)", ('other-user',))
        root_report = self.file_manager.save_file(
            file_data=b"root report",
            original_name='root-report.txt',
            content_type='text/plain',
            uploaded_by='user-test',
        )
        nested_report = self.file_manager.save_file(
            file_data=b"nested report",
            original_name='nested-report.txt',
            content_type='text/plain',
            uploaded_by='user-test',
        )
        other_file = self.file_manager.save_file(
            file_data=b"private",
            original_name='other.txt',
            content_type='text/plain',
            uploaded_by='other-user',
        )
        self.assertIsNotNone(root_report)
        self.assertIsNotNone(nested_report)
        self.assertIsNotNone(other_file)
        assert nested_report is not None
        assert other_file is not None

        projects = self.file_manager.create_user_folder('user-test', 'Projects')
        nested = self.file_manager.create_user_folder('user-test', 'Drafts', projects.id)
        self.file_manager.move_user_file_to_folder('user-test', nested_report.id, projects.id)

        root_files = self.file_manager.list_user_files('user-test', folder_id='', limit=10)
        self.assertEqual([f.original_name for f in root_files], ['root-report.txt'])
        project_files = self.file_manager.list_user_files('user-test', folder_id=projects.id, limit=10)
        self.assertEqual([f.original_name for f in project_files], ['nested-report.txt'])
        self.assertEqual([f.name for f in self.file_manager.list_user_folders('user-test')], ['Projects'])
        self.assertEqual([f.name for f in self.file_manager.list_user_folders('user-test', projects.id)], ['Drafts'])
        self.assertEqual([f.name for f in self.file_manager.get_user_folder_path('user-test', nested.id)], ['Projects', 'Drafts'])

        with self.assertRaises(ValueError):
            self.file_manager.move_user_file_to_folder('user-test', other_file.id, projects.id)
        with self.assertRaises(ValueError):
            self.file_manager.delete_user_folder('user-test', projects.id)

        self.file_manager.move_user_file_to_folder('user-test', nested_report.id, '')
        self.file_manager.delete_user_folder('user-test', nested.id)
        self.file_manager.delete_user_folder('user-test', projects.id)
        self.assertEqual(self.file_manager.list_user_folders('user-test'), [])


if __name__ == '__main__':
    unittest.main()
