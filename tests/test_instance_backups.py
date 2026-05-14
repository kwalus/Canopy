"""Regression tests for local instance backup snapshots."""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import time
import unittest
import zipfile
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

if 'zeroconf' not in sys.modules:
    import types

    zeroconf_stub = types.ModuleType('zeroconf')

    class _Dummy:
        def __init__(self, *args, **kwargs):
            pass

    zeroconf_stub.ServiceBrowser = _Dummy
    zeroconf_stub.ServiceInfo = _Dummy
    zeroconf_stub.Zeroconf = _Dummy
    zeroconf_stub.ServiceStateChange = _Dummy
    sys.modules['zeroconf'] = zeroconf_stub

from canopy.core.backups import BackupManager


class _FakeDbManager:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.execute("CREATE TABLE IF NOT EXISTS system_state (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
            conn.execute("CREATE TABLE IF NOT EXISTS users (id TEXT PRIMARY KEY, username TEXT)")
            conn.execute("INSERT OR REPLACE INTO users (id, username) VALUES (?, ?)", ('u1', 'Backup User'))
            conn.commit()
        finally:
            conn.close()

    @contextmanager
    def get_connection(self, *args, **kwargs):
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    def get_system_state(self, key: str):
        with self.get_connection() as conn:
            row = conn.execute("SELECT value FROM system_state WHERE key = ?", (key,)).fetchone()
            return row['value'] if row else None

    def set_system_state(self, key: str, value: str | None) -> bool:
        with self.get_connection() as conn:
            if value is None or value == '':
                conn.execute("DELETE FROM system_state WHERE key = ?", (key,))
            else:
                conn.execute(
                    "INSERT OR REPLACE INTO system_state (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)",
                    (key, value),
                )
            conn.commit()
        return True


class TestInstanceBackups(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        self.data_dir = self.root / 'node-data'
        self.data_dir.mkdir()
        self.files_root = self.data_dir / 'files'
        (self.files_root / 'documents').mkdir(parents=True)
        (self.files_root / 'documents' / 'report.md').write_text('# Backup me\n', encoding='utf-8')
        (self.data_dir / 'secret_key.json').write_text('{"secret_key":"test"}\n', encoding='utf-8')
        self.db_manager = _FakeDbManager(self.data_dir / 'canopy.db')
        self.file_manager = SimpleNamespace(storage_path=self.files_root)
        self.config = SimpleNamespace(
            testing=True,
            storage=SimpleNamespace(data_dir=str(self.data_dir), database_path=str(self.data_dir / 'canopy.db')),
            meshspace=SimpleNamespace(meshspace_id='mesh-test', name='Test Mesh'),
        )
        self.manager = BackupManager(self.db_manager, self.file_manager, self.config)
        self.backup_root = self.root / 'backups'

    def test_manual_backup_contains_database_files_metadata_and_manifest(self) -> None:
        settings = self.manager.save_settings({
            'enabled': True,
            'backup_root': str(self.backup_root),
            'interval_hours': 24,
            'retention_count': 3,
            'include_files': True,
            'include_large_attachments': False,
        })
        self.assertTrue(settings['include_files'])

        result = self.manager.run_backup(trigger='manual')

        self.assertTrue(result['success'])
        backup_path = Path(result['backup_path'])
        self.assertTrue(backup_path.exists())
        with zipfile.ZipFile(backup_path) as zf:
            names = set(zf.namelist())
            self.assertIn('manifest.json', names)
            self.assertIn('RESTORE_README.txt', names)
            self.assertIn('database/canopy.db', names)
            self.assertIn('files/documents/report.md', names)
            self.assertIn('metadata/secret_key.json', names)
            manifest = json.loads(zf.read('manifest.json').decode('utf-8'))
        self.assertEqual(manifest['kind'], 'canopy_instance_backup_v1')
        self.assertEqual(manifest['files']['files'], 1)
        self.assertTrue(manifest['contains_sensitive_identity_material'])

        status = self.manager.get_status()
        self.assertEqual(status['backup_count'], 1)
        self.assertEqual(status['last_backup_name'], backup_path.name)

    def test_backup_root_inside_files_is_excluded_from_snapshot(self) -> None:
        backup_root = self.files_root / 'backups'
        backup_root.mkdir(parents=True)
        (backup_root / 'canopy-backup-old.zip').write_text('old backup bytes', encoding='utf-8')
        self.manager.save_settings({
            'backup_root': str(backup_root),
            'include_files': True,
            'retention_count': 5,
        })

        result = self.manager.run_backup(trigger='manual')

        self.assertTrue(result['success'])
        with zipfile.ZipFile(result['backup_path']) as zf:
            names = set(zf.namelist())
        self.assertIn('files/documents/report.md', names)
        self.assertNotIn('files/backups/canopy-backup-old.zip', names)

    def test_resolve_backup_path_rejects_traversal_and_unknown_names(self) -> None:
        self.manager.save_settings({'backup_root': str(self.backup_root)})
        self.backup_root.mkdir(parents=True, exist_ok=True)
        valid = self.backup_root / 'canopy-backup-20260514-120000-manual.zip'
        valid.write_text('placeholder', encoding='utf-8')

        self.assertEqual(self.manager.resolve_backup_path(valid.name), valid.resolve())
        self.assertIsNone(self.manager.resolve_backup_path('../canopy-backup-20260514-120000-manual.zip'))
        self.assertIsNone(self.manager.resolve_backup_path('canopy-backup-20260514-120000-manual.zip/extra'))
        self.assertIsNone(self.manager.resolve_backup_path('not-a-backup.zip'))

    def test_retention_prunes_old_complete_snapshots(self) -> None:
        self.manager.save_settings({
            'backup_root': str(self.backup_root),
            'retention_count': 2,
            'include_files': True,
        })

        for _ in range(3):
            result = self.manager.run_backup(trigger='manual')
            self.assertTrue(result['success'])
            time.sleep(0.02)

        backups = sorted(self.backup_root.glob('canopy-backup-*.zip'))
        self.assertEqual(len(backups), 2)
        self.assertEqual(self.manager.get_status()['backup_count'], 2)


if __name__ == '__main__':
    unittest.main()
