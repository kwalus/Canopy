"""Regression tests for agent-facing File Vault API endpoints."""

import os
import sqlite3
import sys
import tempfile
import types
import unittest
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from flask import Flask

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

from canopy.api.routes import create_api_blueprint
from canopy.core.files import FileManager
from canopy.security.api_keys import ApiKeyInfo, Permission


class _FakeDbManager:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    @contextmanager
    def get_connection(self, *args, **kwargs):
        yield self.conn

    def get_instance_owner_user_id(self):
        return 'user-test'

    def get_user(self, user_id):
        return {'id': user_id, 'origin_peer': None}


class _FakeApiKeyManager:
    def validate_key(self, raw_key: str, required_permission=None):
        key_perms = {
            'vault-key': {Permission.READ_FILES, Permission.WRITE_FILES},
            'read-key': {Permission.READ_FILES},
            'write-key': {Permission.WRITE_FILES},
        }
        perms = key_perms.get(raw_key)
        if not perms:
            return None
        if required_permission and required_permission not in perms:
            return None
        return ApiKeyInfo(
            id=f'key-{raw_key}',
            user_id='user-test',
            key_hash='hash',
            permissions=perms,
            created_at=datetime.now(timezone.utc),
        )


class _AllowedAccess:
    allowed = True
    reason = 'allowed-for-test'

    def to_dict(self):
        return {'allowed': True, 'reason': self.reason}


class TestAgentVaultApi(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("CREATE TABLE users (id TEXT PRIMARY KEY, avatar_file_id TEXT, origin_peer TEXT)")
        self.conn.execute("INSERT INTO users (id) VALUES (?)", ('user-test',))
        self.conn.execute("INSERT INTO users (id) VALUES (?)", ('other-user',))
        self.conn.execute("CREATE TABLE channel_messages (id TEXT PRIMARY KEY, attachments TEXT, content TEXT)")
        self.conn.execute("CREATE TABLE feed_posts (id TEXT PRIMARY KEY, metadata TEXT, content TEXT)")
        self.conn.execute("CREATE TABLE messages (id TEXT PRIMARY KEY, metadata TEXT, content TEXT)")
        self.conn.commit()

        self.db_manager = _FakeDbManager(self.conn)
        self.file_manager = FileManager(self.db_manager, str(Path(self.tempdir.name) / 'files'))
        self.components = (
            self.db_manager,
            _FakeApiKeyManager(),
            MagicMock(),
            MagicMock(),
            MagicMock(),
            self.file_manager,
            MagicMock(),
            MagicMock(),
            MagicMock(),
            MagicMock(),
            MagicMock(),
        )

        self.get_components_patcher = patch('canopy.api.routes.get_app_components', return_value=self.components)
        self.get_components_any_patcher = patch('canopy.api.routes._get_app_components_any', return_value=self.components)
        self.get_components_patcher.start()
        self.get_components_any_patcher.start()
        self.addCleanup(self.get_components_patcher.stop)
        self.addCleanup(self.get_components_any_patcher.stop)

        app = Flask(__name__)
        app.config['TESTING'] = True
        app.secret_key = 'agent-vault-api'
        app.register_blueprint(create_api_blueprint(), url_prefix='/api/v1')
        self.client = app.test_client()

    def tearDown(self) -> None:
        self.conn.close()

    def _json(self, response):
        payload = response.get_json()
        self.assertIsInstance(payload, dict)
        return payload

    def test_vault_api_text_file_lifecycle_with_diff_and_checksum(self) -> None:
        folder_response = self.client.post(
            '/api/v1/vault/folders',
            json={'name': 'Working Drafts'},
            headers={'X-API-Key': 'write-key'},
        )
        self.assertEqual(folder_response.status_code, 201)
        folder_id = self._json(folder_response)['folder']['id']

        create_response = self.client.post(
            '/api/v1/vault/files',
            json={
                'filename': 'agent-plan.md',
                'content_type': 'text/markdown',
                'content': '# Plan\n\nold line\n',
                'folder_id': folder_id,
            },
            headers={'X-API-Key': 'write-key'},
        )
        self.assertEqual(create_response.status_code, 201)
        created = self._json(create_response)['file']
        file_id = created['id']
        self.assertEqual(created['folder_id'], folder_id)

        list_response = self.client.get(
            f'/api/v1/vault/files?folder_id={folder_id}',
            headers={'X-API-Key': 'read-key'},
        )
        self.assertEqual(list_response.status_code, 200)
        listed = self._json(list_response)['files']
        self.assertEqual([item['id'] for item in listed], [file_id])
        checksum = listed[0]['checksum']

        read_response = self.client.get(
            f'/api/v1/vault/files/{file_id}/content?mode=text',
            headers={'X-API-Key': 'read-key'},
        )
        self.assertEqual(read_response.status_code, 200)
        self.assertEqual(self._json(read_response)['content'], '# Plan\n\nold line\n')

        diff_response = self.client.post(
            f'/api/v1/vault/files/{file_id}/diff',
            json={'content': '# Plan\n\nnew line\n'},
            headers={'X-API-Key': 'read-key'},
        )
        self.assertEqual(diff_response.status_code, 200)
        diff_payload = self._json(diff_response)
        self.assertIn('-old line', diff_payload['diff'])
        self.assertIn('+new line', diff_payload['diff'])

        stale_response = self.client.patch(
            f'/api/v1/vault/files/{file_id}/content',
            json={'content': '# Plan\n\nbad overwrite\n', 'if_match_checksum': 'not-current'},
            headers={'X-API-Key': 'write-key'},
        )
        self.assertEqual(stale_response.status_code, 409)

        update_response = self.client.patch(
            f'/api/v1/vault/files/{file_id}/content',
            json={
                'filename': 'agent-plan.md',
                'content_type': 'text/markdown',
                'content': '# Plan\n\nnew line\n',
                'if_match_checksum': checksum,
            },
            headers={'X-API-Key': 'write-key'},
        )
        self.assertEqual(update_response.status_code, 200)
        updated = self._json(update_response)['file']
        self.assertEqual(updated['id'], file_id)
        self.assertNotEqual(updated['checksum'], checksum)
        self.assertEqual(updated['attachment']['vault_file_id'], file_id)

        final_read = self.client.get(
            f'/api/v1/vault/files/{file_id}/content?mode=text',
            headers={'X-API-Key': 'read-key'},
        )
        self.assertEqual(final_read.status_code, 200)
        self.assertEqual(self._json(final_read)['content'], '# Plan\n\nnew line\n')

    def test_save_attachment_to_vault_requires_read_and_copies_accessible_file(self) -> None:
        source = self.file_manager.save_file(
            b'shared attachment body',
            'shared-report.txt',
            'text/plain',
            'other-user',
        )
        self.assertIsNotNone(source)
        assert source is not None

        denied = self.client.post(
            '/api/v1/vault/save-attachment',
            json={'file_id': source.id},
            headers={'X-API-Key': 'write-key'},
        )
        self.assertEqual(denied.status_code, 403)

        with patch('canopy.api.routes.evaluate_file_access', return_value=_AllowedAccess()):
            saved_response = self.client.post(
                '/api/v1/vault/save-attachment',
                json={'file_id': source.id},
                headers={'X-API-Key': 'vault-key'},
            )
        self.assertEqual(saved_response.status_code, 200)
        payload = self._json(saved_response)
        self.assertTrue(payload['success'])
        self.assertNotEqual(payload['file_id'], source.id)
        self.assertEqual(payload['file']['name'], 'shared-report.txt')

        copied = self.file_manager.get_file(payload['file_id'])
        self.assertIsNotNone(copied)
        assert copied is not None
        self.assertEqual(copied.uploaded_by, 'user-test')
        self.assertEqual(self.file_manager.get_file_data(copied.id)[0], b'shared attachment body')  # type: ignore[index]


if __name__ == '__main__':
    unittest.main()
