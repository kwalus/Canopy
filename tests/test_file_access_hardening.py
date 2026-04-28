"""Tests for file access control hardening.

These tests cover:
- evaluate_file_access deny-by-default paths (db_manager=None guard,
  unreferenced files, ambiguous evidence)
- FileManager.delete_file owner-only and is_admin bypass
"""
import sys
import os
import types
import unittest
from unittest.mock import MagicMock, patch
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone

# Ensure the repo root is on the path so we can import canopy packages
# without installing the full application.
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

from canopy.security.file_access import evaluate_file_access, FileAccessResult


# ---------------------------------------------------------------------------
# Helpers / stubs
# ---------------------------------------------------------------------------

def _make_db_manager(rows=None):
    """Return a minimal db_manager stub.

    ``rows`` is a dict mapping table name to list of row-dicts returned
    by fetchall() calls for that table.
    """
    rows = rows or {}

    @contextmanager
    def _get_connection():
        conn = MagicMock()

        def _execute(sql, params=()):
            result = MagicMock()
            for table, table_rows in rows.items():
                if table in sql:
                    result.fetchall.return_value = table_rows
                    result.fetchone.return_value = table_rows[0] if table_rows else None
                    return result
            result.fetchall.return_value = []
            result.fetchone.return_value = None
            return result

        conn.execute.side_effect = _execute
        yield conn

    db = MagicMock()
    db.get_connection = _get_connection
    return db


# ---------------------------------------------------------------------------
# Tests for evaluate_file_access
# ---------------------------------------------------------------------------

class TestEvaluateFileAccessDenyByDefault(unittest.TestCase):

    def test_missing_file_id_denies(self):
        result = evaluate_file_access(
            db_manager=_make_db_manager(),
            file_id='',
            viewer_user_id='user1',
        )
        self.assertFalse(result.allowed)
        self.assertEqual(result.reason, 'missing-identity')

    def test_missing_viewer_id_denies(self):
        result = evaluate_file_access(
            db_manager=_make_db_manager(),
            file_id='file1',
            viewer_user_id='',
        )
        self.assertFalse(result.allowed)
        self.assertEqual(result.reason, 'missing-identity')

    def test_none_db_manager_denies(self):
        """db_manager=None must not grant access (explicit guard, fix §A)."""
        result = evaluate_file_access(
            db_manager=None,
            file_id='file1',
            viewer_user_id='user1',
        )
        self.assertFalse(result.allowed)
        self.assertEqual(result.reason, 'missing-db')

    def test_unreferenced_file_denies(self):
        """A file_id that appears in no channel/feed/DM row is denied."""
        result = evaluate_file_access(
            db_manager=_make_db_manager(),  # all queries return []
            file_id='Funknown123',
            viewer_user_id='user1',
        )
        self.assertFalse(result.allowed)
        self.assertEqual(result.reason, 'unreferenced')

    def test_is_admin_grants(self):
        """Instance admin always gets access."""
        result = evaluate_file_access(
            db_manager=_make_db_manager(),
            file_id='file1',
            viewer_user_id='admin',
            is_admin=True,
        )
        self.assertTrue(result.allowed)
        self.assertEqual(result.reason, 'admin')

    def test_owner_grants(self):
        """File owner always gets access."""
        result = evaluate_file_access(
            db_manager=_make_db_manager(),
            file_id='file1',
            viewer_user_id='user1',
            file_uploaded_by='user1',
        )
        self.assertTrue(result.allowed)
        self.assertEqual(result.reason, 'owner')

    def test_profile_avatar_grants_to_authenticated_local_user(self):
        """A file used as a profile avatar should be viewable by signed-in local users."""
        result = evaluate_file_access(
            db_manager=_make_db_manager(rows={
                'users': [
                    {
                        'id': 'owner-user',
                        'origin_peer': '',
                        'avatar_file_id': 'avatar-file',
                    }
                ]
            }),
            file_id='avatar-file',
            viewer_user_id='viewer-user',
        )
        self.assertTrue(result.allowed)
        self.assertEqual(result.reason, 'profile-avatar')
        self.assertEqual(len(result.evidences), 1)
        self.assertEqual(result.evidences[0].source_type, 'user_profile_avatar')
        self.assertTrue(result.evidences[0].can_view)

    def test_channel_membership_missing_denies(self):
        """File referenced in a channel message but viewer not a member → denied."""
        # channel_messages returns a row; channel_members returns nothing
        db = _make_db_manager()

        @contextmanager
        def _get_connection():
            conn = MagicMock()
            call_count = [0]

            def _execute(sql, params=()):
                result = MagicMock()
                if 'channel_messages' in sql:
                    row = MagicMock()
                    row.__getitem__ = lambda self, k: {
                        'id': 'msg1',
                        'channel_id': 'chan1',
                        'attachments': f'[{{"id":"file1"}}]',
                        'content': '',
                    }[k]
                    result.fetchall.return_value = [row]
                    return result
                if 'channel_members' in sql:
                    result.fetchone.return_value = None  # not a member
                    return result
                result.fetchall.return_value = []
                result.fetchone.return_value = None
                return result

            conn.execute.side_effect = _execute
            yield conn

        db.get_connection = _get_connection
        result = evaluate_file_access(
            db_manager=db,
            file_id='file1',
            viewer_user_id='user_not_member',
        )
        self.assertFalse(result.allowed)
        # Evidence should record the channel message with can_view=False
        self.assertTrue(len(result.evidences) > 0)
        self.assertFalse(result.evidences[0].can_view)


# ---------------------------------------------------------------------------
# Tests for FileManager.delete_file ownership check
# ---------------------------------------------------------------------------

class TestDeleteFileOwnership(unittest.TestCase):

    def _make_file_manager(self, file_owner: str):
        """Stub FileManager with a file owned by file_owner."""
        from canopy.core.files import FileInfo

        file_info = FileInfo(
            id='file1',
            original_name='test.png',
            stored_name='file1.png',
            file_path='/tmp/test_canopy_file1.png',
            content_type='image/png',
            size=100,
            uploaded_by=file_owner,
            uploaded_at=datetime.now(timezone.utc),
            url='/files/file1',
            checksum='abc',
        )

        db = MagicMock()

        @contextmanager
        def _conn():
            conn = MagicMock()
            conn.execute.return_value = MagicMock()
            yield conn

        db.get_connection = _conn

        from canopy.core.files import FileManager
        fm = FileManager.__new__(FileManager)
        fm.db = db
        fm.storage_path = '/tmp'
        fm.get_file = MagicMock(return_value=file_info)
        return fm

    def test_owner_can_delete(self):
        fm = self._make_file_manager('alice')
        result = fm.delete_file('file1', 'alice')
        self.assertTrue(result)

    def test_non_owner_cannot_delete(self):
        fm = self._make_file_manager('alice')
        result = fm.delete_file('file1', 'bob')
        self.assertFalse(result)

    def test_admin_can_delete_other_users_file(self):
        """is_admin=True allows deletion of files uploaded by other users."""
        fm = self._make_file_manager('alice')
        result = fm.delete_file('file1', 'admin_user', is_admin=True)
        self.assertTrue(result)

    def test_non_admin_flag_cannot_delete_others(self):
        """is_admin=False (default) must not allow cross-user deletion."""
        fm = self._make_file_manager('alice')
        result = fm.delete_file('file1', 'bob', is_admin=False)
        self.assertFalse(result)


if __name__ == '__main__':
    unittest.main()
