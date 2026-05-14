"""Regression tests for admin-controlled instance Git updates."""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
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

from canopy.core.updates import UpdateError, UpdateManager


class _FakeDbManager:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS system_state "
                "(key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
            )
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


@unittest.skipIf(shutil.which('git') is None, 'git is required for instance updater tests')
class TestInstanceUpdates(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        self.remote = self.root / 'remote'
        self.local = self.root / 'local'
        self.db_manager = _FakeDbManager(self.root / 'settings.db')
        self.config = SimpleNamespace(testing=True)
        self.remote.mkdir(parents=True)
        self._git(['init', '-b', 'main'], cwd=self.remote)
        self._git(['config', 'user.email', 'canopy@example.test'], cwd=self.remote)
        self._git(['config', 'user.name', 'Canopy Test'], cwd=self.remote)
        (self.remote / 'README.md').write_text('one\n', encoding='utf-8')
        self._git(['add', 'README.md'], cwd=self.remote)
        self._git(['commit', '-m', 'initial'], cwd=self.remote)
        self._git(['clone', str(self.remote), str(self.local)], cwd=self.root)
        self.manager = UpdateManager(self.db_manager, self.config, 'test-secret', project_root=self.local)

    def _git(self, args: list[str], *, cwd: Path) -> str:
        result = subprocess.run(['git', *args], cwd=str(cwd), text=True, capture_output=True, check=True)
        return result.stdout.strip()

    def _commit_remote_change(self, text: str = 'two\n') -> str:
        (self.remote / 'README.md').write_text(text, encoding='utf-8')
        self._git(['add', 'README.md'], cwd=self.remote)
        self._git(['commit', '-m', 'remote update'], cwd=self.remote)
        return self._git(['rev-parse', 'HEAD'], cwd=self.remote)

    def test_settings_store_private_token_without_exposing_secret(self) -> None:
        settings = self.manager.save_settings(
            'admin-user',
            {
                'repo_url': str(self.remote),
                'branch': 'main',
                'github_token': 'ghp_private_secret',
                'check_enabled': True,
            },
        )

        self.assertTrue(settings['github_token_configured'])
        self.assertTrue(settings['github_token_saved'])
        self.assertNotIn('ghp_private_secret', json.dumps(settings))
        raw = self.db_manager.get_system_state('instance_update_settings_v1') or ''
        self.assertNotIn('ghp_private_secret', raw)

    def test_check_and_apply_fast_forward_update_from_configured_repo(self) -> None:
        remote_head = self._commit_remote_change()
        self.manager.save_settings('admin-user', {'repo_url': str(self.remote), 'branch': 'main'})

        check = self.manager.check_for_updates()

        self.assertTrue(check['success'])
        self.assertTrue(check['update_available'])
        self.assertEqual(check['behind_count'], 1)
        self.assertTrue(check['can_apply'])
        self.assertEqual(check['remote_commit'], remote_head)

        applied = self.manager.apply_update()

        self.assertTrue(applied['success'])
        self.assertTrue(applied['changed'])
        self.assertEqual(applied['after_commit'], remote_head)
        self.assertTrue(applied['restart_required'])

    def test_apply_refuses_dirty_worktree(self) -> None:
        self._commit_remote_change()
        self.manager.save_settings('admin-user', {'repo_url': str(self.remote), 'branch': 'main'})
        (self.local / 'local-only.txt').write_text('dirty\n', encoding='utf-8')

        with self.assertRaises(UpdateError) as ctx:
            self.manager.apply_update()

        self.assertEqual(ctx.exception.reason, 'dirty_worktree')

    def test_rejects_credentials_embedded_in_https_url(self) -> None:
        with self.assertRaises(UpdateError) as ctx:
            self.manager.save_settings('admin-user', {'repo_url': 'https://token:secret@github.com/kwalus/Canopy.git'})

        self.assertEqual(ctx.exception.reason, 'repo_url_contains_credentials')

    def test_rejects_plain_http_update_repositories(self) -> None:
        with self.assertRaises(UpdateError) as ctx:
            self.manager.save_settings('admin-user', {'repo_url': 'http://github.com/kwalus/Canopy.git'})

        self.assertEqual(ctx.exception.reason, 'insecure_repo_url')


if __name__ == '__main__':
    unittest.main()
