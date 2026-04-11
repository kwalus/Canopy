"""Regression coverage for admin approval applying agent quarantine."""

import os
import sqlite3
import sys
import types
import unittest
from unittest.mock import patch

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

from canopy.ui.routes import create_ui_blueprint


class _FakeDbManager:
    def __init__(self) -> None:
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            """
            CREATE TABLE channels (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                channel_type TEXT NOT NULL,
                created_by TEXT NOT NULL,
                description TEXT,
                privacy_mode TEXT
            );

            CREATE TABLE channel_members (
                channel_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                role TEXT DEFAULT 'member',
                UNIQUE(channel_id, user_id)
            );
            """
        )
        self.conn.executemany(
            """
            INSERT INTO channels (id, name, channel_type, created_by, description, privacy_mode)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                ('general', 'general', 'public', 'system', 'General', 'open'),
                ('Cnews', 'canopy-news', 'public', 'owner-user', 'News', 'open'),
                ('Cprivate', 'staff', 'public', 'owner-user', 'Private', 'private'),
            ],
        )
        self.conn.commit()
        self.users = {
            'owner-user': {
                'id': 'owner-user',
                'username': 'owner',
                'account_type': 'human',
                'status': 'active',
                'origin_peer': None,
                'password_hash': 'hash-owner',
            },
            'agent-user': {
                'id': 'agent-user',
                'username': 'agent',
                'account_type': 'agent',
                'status': 'pending_approval',
                'origin_peer': None,
                'password_hash': 'hash-agent',
            },
        }

    def get_connection(self):
        return self.conn

    def get_instance_owner_user_id(self):
        return 'owner-user'

    def get_user(self, user_id: str):
        user = self.users.get(user_id)
        return dict(user) if user else None

    def set_user_status(self, user_id: str, status: str) -> bool:
        if user_id not in self.users:
            return False
        self.users[user_id]['status'] = status
        return True

    def update_user_admin_fields(self, user_id: str, *, account_type=None, status=None, display_name=None) -> bool:
        if user_id not in self.users:
            return False
        if account_type is not None:
            self.users[user_id]['account_type'] = account_type
        if status is not None:
            self.users[user_id]['status'] = status
        if display_name is not None:
            self.users[user_id]['display_name'] = display_name
        return True


class _FakeChannelManager:
    def __init__(self, *, should_quarantine_succeed: bool = True, governance_enabled: bool = True) -> None:
        self.quarantine_calls = []
        self.enforcement_calls = []
        self.should_quarantine_succeed = should_quarantine_succeed
        self.governance_enabled = governance_enabled

    def ensure_default_channels_exist(self) -> None:
        return None

    def ensure_agent_quarantine_assignment(self, user_id: str, *, updated_by: str = None, role: str = 'member'):
        self.quarantine_calls.append({
            'user_id': user_id,
            'updated_by': updated_by,
            'role': role,
        })
        return self.should_quarantine_succeed

    def enforce_user_channel_governance(self, user_id: str):
        self.enforcement_calls.append(user_id)
        return {
            'enabled': self.governance_enabled,
            'checked_count': 1,
            'removed_count': 1,
            'removed_channel_ids': ['general'],
        }


class TestAgentQuarantineAdminApprove(unittest.TestCase):
    def setUp(self) -> None:
        self.db_manager = _FakeDbManager()
        self.channel_manager = _FakeChannelManager()
        components = (
            self.db_manager,
            None,
            None,
            None,
            self.channel_manager,
            None,
            None,
            None,
            None,
            None,
            None,
        )
        self.components_patcher = patch('canopy.ui.routes._get_app_components_any', return_value=components)
        self.components_patcher.start()
        self.addCleanup(self.components_patcher.stop)

        app = Flask(__name__)
        app.config['TESTING'] = True
        app.secret_key = 'agent-approve-test'
        app.register_blueprint(create_ui_blueprint())
        self.client = app.test_client()
        with self.client.session_transaction() as sess:
            sess['authenticated'] = True
            sess['user_id'] = 'owner-user'
            sess['username'] = 'owner'
            sess['display_name'] = 'Owner'
            sess['_csrf_token'] = 'csrf-approve'

    def tearDown(self) -> None:
        self.db_manager.conn.close()

    def _membership_ids(self, user_id: str) -> list[str]:
        rows = self.db_manager.conn.execute(
            "SELECT channel_id FROM channel_members WHERE user_id = ? ORDER BY channel_id",
            (user_id,),
        ).fetchall()
        return [row['channel_id'] for row in rows]

    def test_admin_approve_quarantines_agent(self) -> None:
        response = self.client.post(
            '/ajax/admin/users/agent-user/approve',
            headers={'X-CSRFToken': 'csrf-approve', 'X-Requested-With': 'XMLHttpRequest'},
        )
        self.assertEqual(response.status_code, 200)
        payload = response.get_json() or {}
        self.assertTrue(payload.get('success'))
        self.assertEqual(payload.get('status'), 'active')
        self.assertEqual(self.db_manager.users['agent-user']['status'], 'active')
        self.assertEqual(len(self.channel_manager.quarantine_calls), 1)
        self.assertEqual(self.channel_manager.quarantine_calls[0]['user_id'], 'agent-user')
        self.assertEqual(self.channel_manager.quarantine_calls[0]['updated_by'], 'owner-user')
        self.assertEqual(self.channel_manager.enforcement_calls, ['agent-user'])
        self.assertEqual(self._membership_ids('agent-user'), ['Cnews', 'general'])

    def test_admin_approve_reverts_to_pending_if_quarantine_fails(self) -> None:
        failing_channel_manager = _FakeChannelManager(should_quarantine_succeed=False)
        components = (
            self.db_manager,
            None,
            None,
            None,
            failing_channel_manager,
            None,
            None,
            None,
            None,
            None,
            None,
        )
        with patch('canopy.ui.routes._get_app_components_any', return_value=components):
            response = self.client.post(
                '/ajax/admin/users/agent-user/approve',
                headers={'X-CSRFToken': 'csrf-approve', 'X-Requested-With': 'XMLHttpRequest'},
            )
        self.assertEqual(response.status_code, 500)
        self.assertEqual(self.db_manager.users['agent-user']['status'], 'pending_approval')

    def test_admin_classification_active_agent_applies_quarantine(self) -> None:
        response = self.client.post(
            '/ajax/admin/users/agent-user/classification',
            json={'account_type': 'agent', 'status': 'active', 'display_name': 'Agent User'},
            headers={'X-CSRFToken': 'csrf-approve', 'X-Requested-With': 'XMLHttpRequest'},
        )
        self.assertEqual(response.status_code, 200)
        payload = response.get_json() or {}
        self.assertTrue(payload.get('success'))
        self.assertEqual(self.channel_manager.quarantine_calls[-1]['user_id'], 'agent-user')
        self.assertEqual(self.channel_manager.enforcement_calls[-1], 'agent-user')
        self.assertEqual(self._membership_ids('agent-user'), ['Cnews', 'general'])

    def test_admin_classification_reverts_if_quarantine_fails(self) -> None:
        failing_channel_manager = _FakeChannelManager(should_quarantine_succeed=False)
        components = (
            self.db_manager,
            None,
            None,
            None,
            failing_channel_manager,
            None,
            None,
            None,
            None,
            None,
            None,
        )
        with patch('canopy.ui.routes._get_app_components_any', return_value=components):
            response = self.client.post(
                '/ajax/admin/users/agent-user/classification',
                json={'account_type': 'agent', 'status': 'active'},
                headers={'X-CSRFToken': 'csrf-approve', 'X-Requested-With': 'XMLHttpRequest'},
            )
        self.assertEqual(response.status_code, 500)
        self.assertEqual(self.db_manager.users['agent-user']['account_type'], 'agent')
        self.assertEqual(self.db_manager.users['agent-user']['status'], 'pending_approval')


if __name__ == '__main__':
    unittest.main()
