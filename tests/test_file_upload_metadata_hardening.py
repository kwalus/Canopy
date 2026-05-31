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

    def test_default_attachment_folder_groups_posted_media_by_type(self) -> None:
        folder_id = self.file_manager.ensure_default_attachment_folder(
            'user-test',
            'field-photo.png',
            'image/png',
            root_name='Posted Attachments',
        )
        self.assertTrue(folder_id)
        path = self.file_manager.get_user_folder_path('user-test', folder_id)
        self.assertEqual([folder.name for folder in path], ['Posted Attachments', 'Images'])

        png_1x1 = (
            b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR'
            b'\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00'
            b'\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\xf8\x0f\x00\x01\x01\x01\x00'
            b'\x18\xdd\x8d\xb0\x00\x00\x00\x00IEND\xaeB`\x82'
        )
        info = self.file_manager.save_file(
            file_data=png_1x1,
            original_name='field-photo.png',
            content_type='image/png',
            uploaded_by='user-test',
            vault_folder_id=folder_id,
        )
        self.assertIsNotNone(info)
        assert info is not None
        self.assertEqual(info.vault_folder_id, folder_id)

    def test_default_attachment_folder_uses_business_document_bucket(self) -> None:
        folder_id = self.file_manager.ensure_default_attachment_folder(
            'user-test',
            'meeting-notes.docx',
            'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
            root_name='Saved Attachments',
        )
        self.assertTrue(folder_id)
        path = self.file_manager.get_user_folder_path('user-test', folder_id)
        self.assertEqual([folder.name for folder in path], ['Saved Attachments', 'Documents'])

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
        thumb_bytes, _, thumb_mimetype = thumb
        self.assertEqual(thumb_mimetype, "image/jpeg")
        opened = Image.open(io.BytesIO(thumb_bytes))
        self.assertGreater(opened.size[1], opened.size[0])

    def test_thumbnail_endpoint_data_does_not_fall_back_to_large_original(self) -> None:
        image_dir = self.storage_root / "images"
        image_dir.mkdir(parents=True, exist_ok=True)
        file_path = image_dir / "Flarge.jpg"
        file_path.write_bytes(b"not-a-real-jpeg" * 90000)
        checksum = hashlib.sha256(file_path.read_bytes()).hexdigest()

        self.conn.execute(
            """
            INSERT INTO files (
                id, original_name, stored_name, file_path, content_type,
                size, uploaded_by, uploaded_at, checksum
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "Flarge",
                "phone-large.jpg",
                "Flarge.jpg",
                str(file_path),
                "image/jpeg",
                file_path.stat().st_size,
                "user-test",
                datetime.now(timezone.utc).isoformat(),
                checksum,
            ),
        )
        self.conn.commit()

        self.assertIsNone(self.file_manager.get_thumbnail_data("Flarge"))

    def test_svg_preview_uses_original_without_raster_thumbnail(self) -> None:
        svg_bytes = (
            b'<svg xmlns="http://www.w3.org/2000/svg" width="64" height="32">'
            b'<rect width="64" height="32" fill="#16a34a"/></svg>'
        )
        info = self.file_manager.save_file(
            file_data=svg_bytes,
            original_name="diagram.svg",
            content_type="image/svg+xml",
            uploaded_by="user-test",
        )
        self.assertIsNotNone(info)
        assert info is not None
        original_path = Path(info.file_path)
        self.assertFalse(self.file_manager._thumb_path_for(original_path).exists())

        thumb = self.file_manager.get_thumbnail_data(info.id)
        self.assertIsNotNone(thumb)
        assert thumb is not None
        thumb_bytes, _, thumb_mimetype = thumb
        self.assertEqual(thumb_bytes, svg_bytes)
        self.assertEqual(thumb_mimetype, "image/svg+xml")

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

    def test_legacy_files_table_migrates_vault_folder_column_before_index(self) -> None:
        legacy = sqlite3.connect(':memory:')
        legacy.row_factory = sqlite3.Row
        self.addCleanup(legacy.close)
        legacy.execute("CREATE TABLE users (id TEXT PRIMARY KEY)")
        legacy.execute("INSERT INTO users (id) VALUES (?)", ('legacy-user',))
        legacy.execute(
            """
            CREATE TABLE files (
                id TEXT PRIMARY KEY,
                original_name TEXT NOT NULL,
                stored_name TEXT NOT NULL,
                file_path TEXT NOT NULL,
                content_type TEXT NOT NULL,
                size INTEGER NOT NULL,
                uploaded_by TEXT NOT NULL,
                uploaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                checksum TEXT NOT NULL
            )
            """
        )
        legacy.commit()

        FileManager(_FakeDbManager(legacy), str(Path(self.tempdir.name) / "legacy-files"))

        columns = {row['name'] for row in legacy.execute("PRAGMA table_info(files)").fetchall()}
        self.assertIn('vault_folder_id', columns)
        indexes = {row['name'] for row in legacy.execute("PRAGMA index_list(files)").fetchall()}
        self.assertIn('idx_files_vault_folder', indexes)

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

    def test_copy_file_to_user_vault_clones_accessible_attachment_into_user_scope(self) -> None:
        self.conn.execute("INSERT INTO users (id) VALUES (?)", ('other-user',))
        source = self.file_manager.save_file(
            file_data=b"shared attachment body",
            original_name='shared-report.txt',
            content_type='text/plain',
            uploaded_by='other-user',
        )
        self.assertIsNotNone(source)
        assert source is not None

        inbox = self.file_manager.create_user_folder('user-test', 'Saved Attachments')
        copied = self.file_manager.copy_file_to_user_vault(
            source.id,
            'user-test',
            vault_folder_id=inbox.id,
        )

        self.assertIsNotNone(copied)
        assert copied is not None
        self.assertNotEqual(copied.id, source.id)
        self.assertEqual(copied.original_name, source.original_name)
        self.assertEqual(copied.content_type, source.content_type)
        self.assertEqual(copied.uploaded_by, 'user-test')
        self.assertEqual(copied.vault_folder_id, inbox.id)
        self.assertEqual(Path(copied.file_path).read_bytes(), b"shared attachment body")
        self.assertEqual(copied.checksum, source.checksum)

        folder_files = self.file_manager.list_user_files('user-test', folder_id=inbox.id, limit=10)
        self.assertEqual([file.id for file in folder_files], [copied.id])
        other_files = self.file_manager.list_user_files('other-user', limit=10)
        self.assertEqual([file.id for file in other_files], [source.id])

        same = self.file_manager.copy_file_to_user_vault(copied.id, 'user-test')
        self.assertIsNotNone(same)
        assert same is not None
        self.assertEqual(same.id, copied.id)

        archived = self.file_manager.create_user_folder('user-test', 'Archive')
        moved = self.file_manager.copy_file_to_user_vault(
            copied.id,
            'user-test',
            vault_folder_id=archived.id,
        )
        self.assertIsNotNone(moved)
        assert moved is not None
        self.assertEqual(moved.id, copied.id)
        self.assertEqual(moved.vault_folder_id, archived.id)

        batch_folder = self.file_manager.create_user_folder('user-test', 'Saved Batch')
        duplicated = self.file_manager.copy_file_to_user_vault(
            copied.id,
            'user-test',
            vault_folder_id=batch_folder.id,
            duplicate_if_owned=True,
        )
        self.assertIsNotNone(duplicated)
        assert duplicated is not None
        self.assertNotEqual(duplicated.id, copied.id)
        self.assertEqual(duplicated.uploaded_by, 'user-test')
        self.assertEqual(duplicated.vault_folder_id, batch_folder.id)
        self.assertEqual(Path(duplicated.file_path).read_bytes(), b"shared attachment body")
        original_after_duplicate = self.file_manager.get_file(copied.id)
        self.assertIsNotNone(original_after_duplicate)
        assert original_after_duplicate is not None
        self.assertEqual(original_after_duplicate.vault_folder_id, archived.id)

    def test_replace_user_file_content_preserves_id_and_updates_bytes(self) -> None:
        original = self.file_manager.save_file(
            file_data=b"# Draft\n\nold body\n",
            original_name='agent-draft.md',
            content_type='text/markdown',
            uploaded_by='user-test',
        )
        self.assertIsNotNone(original)
        assert original is not None
        original_path = Path(original.file_path)

        updated = self.file_manager.replace_user_file_content(
            'user-test',
            original.id,
            b"# Draft\n\nnew body\n",
            original_name='agent-draft.md',
            content_type='text/markdown',
        )

        self.assertIsNotNone(updated)
        assert updated is not None
        self.assertEqual(updated.id, original.id)
        self.assertEqual(updated.original_name, 'agent-draft.md')
        self.assertNotEqual(updated.checksum, original.checksum)
        self.assertEqual(Path(updated.file_path).read_bytes(), b"# Draft\n\nnew body\n")
        self.assertEqual(self.file_manager.get_file_data(original.id)[0], b"# Draft\n\nnew body\n")  # type: ignore[index]
        self.assertTrue(Path(updated.file_path).exists())
        self.assertEqual(Path(updated.file_path), original_path)

        denied = self.file_manager.replace_user_file_content(
            'other-user',
            original.id,
            b"takeover",
            original_name='agent-draft.md',
            content_type='text/markdown',
        )
        self.assertIsNone(denied)


if __name__ == '__main__':
    unittest.main()
