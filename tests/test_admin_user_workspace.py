"""Regression tests for admin user workspace and profile controls."""

import os
import sqlite3
import sys
import types
import unittest
from io import BytesIO
from types import SimpleNamespace
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

from canopy.ui.routes import create_ui_blueprint
from canopy.security.api_keys import Permission


class _FakeDbManager:
    def __init__(self, conn: sqlite3.Connection, owner_user_id: str) -> None:
        self._conn = conn
        self._owner_user_id = owner_user_id

    def get_connection(self) -> sqlite3.Connection:
        return self._conn

    def get_instance_owner_user_id(self):
        return self._owner_user_id

    def get_user(self, user_id: str):
        row = self._conn.execute(
            "SELECT * FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
        return dict(row) if row else None

    def get_all_users_for_admin(self):
        rows = self._conn.execute("SELECT * FROM users ORDER BY username ASC").fetchall()
        return [dict(row) for row in rows]

    def get_pending_approval_count(self):
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM users WHERE COALESCE(status, 'active') = 'pending_approval'"
        ).fetchone()
        return int((row['n'] if row else 0) or 0)


class _FakeProfileManager:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def get_profile(self, user_id: str):
        row = self._conn.execute(
            """
            SELECT display_name, bio, avatar_file_id, theme_preference
            FROM users
            WHERE id = ?
            """,
            (user_id,),
        ).fetchone()
        if not row:
            return None
        avatar_file_id = row['avatar_file_id']
        return SimpleNamespace(
            display_name=row['display_name'],
            bio=row['bio'],
            avatar_file_id=avatar_file_id,
            avatar_url=(f"/files/{avatar_file_id}" if avatar_file_id else None),
            theme_preference=row['theme_preference'] or 'dark',
        )

    def update_profile(self, user_id: str, **updates):
        valid = {}
        for key, value in (updates or {}).items():
            if key in {'display_name', 'bio', 'account_type', 'theme_preference', 'avatar_file_id'}:
                valid[key] = value
        if not valid:
            return False
        set_sql = ", ".join(f"{k} = ?" for k in valid.keys())
        values = list(valid.values()) + [user_id]
        cur = self._conn.execute(
            f"UPDATE users SET {set_sql} WHERE id = ?",
            values,
        )
        self._conn.commit()
        return cur.rowcount > 0

    def update_avatar(self, user_id: str, avatar_data: bytes, filename: str, content_type: str):
        if not avatar_data or not content_type.startswith('image/'):
            return None
        file_id = 'file-admin-avatar'
        ok = self.update_profile(user_id, avatar_file_id=file_id)
        return file_id if ok else None

    def get_profile_card(self, user_id: str):
        return None


class _FakeInboxManager:
    def __init__(self) -> None:
        self.rebuild_calls = []

    def count_items(self, user_id: str, status=None, include_handled: bool = False):
        if include_handled:
            return 5
        if status == 'pending':
            return 3
        if status == 'seen':
            return 0
        return 3

    def list_items(self, user_id: str, status=None, limit: int = 50, include_handled: bool = False, since=None):
        return [
            {
                'id': 'INB-1',
                'source_type': 'channel_message',
                'source_id': 'msg-1',
                'channel_id': 'general',
                'sender_user_id': 'user-alpha',
                'trigger_type': 'mention',
                'status': 'pending',
                'priority': 'normal',
                'created_at': '2026-02-22T10:00:00+00:00',
                'seen_at': None,
                'handled_at': None,
                'completed_at': None,
                'completion_ref': None,
                'last_resolution_status': None,
                'last_resolution_at': None,
                'last_completion_ref': None,
                'payload': {'preview': 'Please check this issue.'},
            },
            {
                'id': 'INB-2',
                'source_type': 'feed_post',
                'source_id': 'post-9',
                'channel_id': None,
                'sender_user_id': 'user-beta',
                'trigger_type': 'mention',
                'status': 'completed',
                'priority': 'normal',
                'created_at': '2026-02-22T09:00:00+00:00',
                'seen_at': '2026-02-22T09:01:00+00:00',
                'handled_at': '2026-02-22T09:04:00+00:00',
                'completed_at': '2026-02-22T09:04:00+00:00',
                'completion_ref': None,
                'last_resolution_status': 'completed',
                'last_resolution_at': '2026-02-22T09:04:00+00:00',
                'last_completion_ref': None,
                'payload': {'preview': 'Responded privately without linking evidence.'},
            },
        ][: max(1, limit)]

    def list_audit(self, user_id: str, limit: int = 50, since=None):
        return [
            {
                'id': 'AUD-1',
                'reason': 'cooldown',
                'source_type': 'channel_message',
                'source_id': 'msg-older',
                'channel_id': 'general',
                'sender_user_id': 'user-alpha',
                'trigger_type': 'mention',
                'created_at': '2026-02-22T09:45:00+00:00',
            }
        ][: max(1, limit)]

    def get_stats(self, user_id: str, window_hours: int = 24):
        return {
            'window_hours': window_hours,
            'status_counts': {'pending': 3, 'completed': 2},
            'rejection_counts': {'cooldown': 2},
            'discrepancy_counts': {
                'completed_without_completion_ref': 1,
                'skipped_without_completion_ref': 0,
            },
        }

    def get_config(self, user_id: str):
        return {
            'allowed_trigger_types': ['mention', 'dm'],
            'max_pending': 500,
        }

    def rebuild_from_channel_messages(self, user_id: str, username: str, display_name=None, window_hours: int = 168, limit: int = 2000):
        self.rebuild_calls.append({
            'user_id': user_id,
            'username': username,
            'display_name': display_name,
            'window_hours': window_hours,
            'limit': limit,
        })
        return {'scanned': 10, 'created': 4, 'skipped': 6}


class _FakeMentionManager:
    def get_mentions(self, user_id: str, since=None, limit: int = 50, include_acknowledged: bool = False):
        rows = [
            {
                'id': 'MN-1',
                'source_type': 'channel_message',
                'source_id': 'msg-1',
                'author_id': 'user-alpha',
                'channel_id': 'general',
                'preview': 'Please check this issue.',
                'created_at': '2026-02-22T10:00:00+00:00',
                'acknowledged_at': None,
                'status': 'new',
            },
            {
                'id': 'MN-2',
                'source_type': 'feed_post',
                'source_id': 'post-1',
                'author_id': 'user-beta',
                'channel_id': None,
                'preview': 'You were mentioned in a post.',
                'created_at': '2026-02-22T09:00:00+00:00',
                'acknowledged_at': '2026-02-22T09:10:00+00:00',
                'status': 'acknowledged',
            },
        ]
        if include_acknowledged:
            return rows[: max(1, limit)]
        return [r for r in rows if not r.get('acknowledged_at')][: max(1, limit)]


class _FakeWorkspaceEventManager:
    def get_diagnostics(self, *, limit: int = 50):
        return {
            'count': 3,
            'oldest_created_at': '2026-02-22T08:00:00+00:00',
            'latest_created_at': '2026-02-22T10:00:00+00:00',
            'latest_seq': 33,
            'type_counts': {
                'dm.message.created': 1,
                'mention.created': 1,
                'inbox.item.created': 1,
            },
            'items': [
                {
                    'seq': 33,
                    'event_id': 'EVT-33',
                    'event_type': 'dm.message.created',
                    'actor_user_id': 'agent-local',
                    'target_user_id': None,
                    'channel_id': None,
                    'post_id': None,
                    'message_id': 'DM-33',
                    'visibility_scope': 'dm',
                    'dedupe_key': 'dm:33',
                    'created_at': '2026-02-22T10:00:00+00:00',
                    'payload_keys': ['preview', 'sender_id', 'recipient_id'],
                    'payload_preview': 'hello from remote',
                }
            ][: max(1, limit)],
        }


class _FakeChannelManager:
    def __init__(self) -> None:
        self.policies = {
            'agent-local': {
                'enabled': True,
                'block_public_channels': True,
                'restrict_to_allowed_channels': True,
                'allowed_channel_ids': ['Cprivate'],
                'updated_at': '2026-02-22T08:00:00+00:00',
                'updated_by': 'admin-user',
            }
        }
        self.channels = [
            {
                'id': 'general',
                'name': 'general',
                'channel_type': 'public',
                'privacy_mode': 'open',
                'origin_peer': None,
                'member_count': 10,
                'members': {'agent-local'},
            },
            {
                'id': 'Cprivate',
                'name': 'private-test',
                'channel_type': 'private',
                'privacy_mode': 'private',
                'origin_peer': None,
                'member_count': 2,
                'members': {'agent-local'},
            },
            {
                'id': 'Cpublic',
                'name': 'public-room',
                'channel_type': 'public',
                'privacy_mode': 'open',
                'origin_peer': None,
                'member_count': 6,
                'members': {'agent-local'},
            },
            {
                'id': 'CremotePrivate',
                'name': 'ops-room',
                'channel_type': 'private',
                'privacy_mode': 'private',
                'origin_peer': 'peer-xyz',
                'member_count': 2,
                'members': {'agent-local'},
            },
            {
                'id': 'CremotePrivateOther',
                'name': 'ops-room-secret',
                'channel_type': 'private',
                'privacy_mode': 'private',
                'origin_peer': 'peer-xyz',
                'member_count': 2,
                'members': set(),
            },
            {
                'id': 'CremotePublic',
                'name': 'ops-room',
                'channel_type': 'public',
                'privacy_mode': 'open',
                'origin_peer': 'peer-xyz',
                'member_count': 5,
                'members': {'agent-local'},
            },
        ]
        self.saved_payloads = []
        self.enforce_calls = []

    def _policy_defaults(self, user_id: str):
        base = {
            'user_id': user_id,
            'enabled': False,
            'block_public_channels': False,
            'restrict_to_allowed_channels': False,
            'allowed_channel_ids': [],
            'updated_at': None,
            'updated_by': None,
        }
        base.update(self.policies.get(user_id, {}))
        return base

    def get_user_channel_governance(self, user_id: str):
        return self._policy_defaults(user_id)

    def list_channels_for_governance(self, user_id: str = None):
        rows = []
        policy = self._policy_defaults(user_id) if user_id else None
        allowed_set = set((policy or {}).get('allowed_channel_ids') or [])
        for row in self.channels:
            payload = {
                'id': row['id'],
                'name': row['name'],
                'channel_type': row['channel_type'],
                'privacy_mode': row['privacy_mode'],
                'origin_peer': row.get('origin_peer'),
                'member_count': row['member_count'],
                'is_public_open': row['privacy_mode'] == 'open' and row['channel_type'] in {'public', 'general'},
            }
            if user_id:
                payload['is_member'] = user_id in row.get('members', set())
                allowed = True
                reason = 'ok'
                if policy.get('enabled'):
                    if payload['is_public_open'] and policy.get('block_public_channels'):
                        allowed = False
                        reason = 'governance_public_channels_blocked'
                    if allowed and policy.get('restrict_to_allowed_channels') and row['id'] not in allowed_set:
                        allowed = False
                        reason = 'governance_channel_not_allowlisted'
                payload['governance_allowed'] = allowed
                payload['governance_reason'] = reason
            rows.append(payload)
        return rows

    def set_user_channel_governance(
        self,
        user_id: str,
        *,
        enabled: bool,
        block_public_channels: bool,
        restrict_to_allowed_channels: bool,
        allowed_channel_ids=None,
        updated_by=None,
    ):
        payload = {
            'user_id': user_id,
            'enabled': bool(enabled),
            'block_public_channels': bool(block_public_channels),
            'restrict_to_allowed_channels': bool(restrict_to_allowed_channels),
            'allowed_channel_ids': list(allowed_channel_ids or []),
            'updated_by': updated_by,
        }
        self.saved_payloads.append(payload)
        self.policies[user_id] = {
            **payload,
            'updated_at': '2026-02-23T00:00:00+00:00',
        }
        return True

    def enforce_user_channel_governance(self, user_id: str):
        self.enforce_calls.append(user_id)
        policy = self._policy_defaults(user_id)
        return {
            'user_id': user_id,
            'enabled': policy.get('enabled', False),
            'checked_count': 3,
            'removed_count': 2 if policy.get('enabled') else 0,
            'removed_channel_ids': ['general', 'Cpublic'] if policy.get('enabled') else [],
        }


class _FakeMeshspaceRegistryManager:
    def __init__(self) -> None:
        self.record = {
            'meshspace_id': 'default-mesh',
            'name': 'Default Mesh',
            'default_agent_permissions': [
                Permission.READ_MESSAGES.value,
                Permission.WRITE_MESSAGES.value,
                Permission.READ_FEED.value,
                Permission.WRITE_FEED.value,
            ],
        }

    def get_meshspace(self, meshspace_id: str):
        if meshspace_id == self.record['meshspace_id']:
            return dict(self.record)
        return None

    def list_meshspaces(self):
        return [dict(self.record)]

    def update_meshspace_metadata(self, meshspace_id: str, **kwargs):
        if meshspace_id != self.record['meshspace_id']:
            return None
        if 'default_agent_permissions' in kwargs and kwargs['default_agent_permissions'] is not None:
            self.record['default_agent_permissions'] = list(kwargs['default_agent_permissions'])
        return dict(self.record)

    def update_shell_summary(self, meshspace_id: str, summary):
        if meshspace_id != self.record['meshspace_id']:
            return None
        self.record['summary'] = dict(summary or {})
        return dict(self.record)

    def launch_environment(self, meshspace_id: str):
        if meshspace_id != self.record['meshspace_id']:
            return None
        return {
            'CANOPY_MESHSPACE_ID': self.record['meshspace_id'],
            'CANOPY_MESHSPACE_NAME': self.record['name'],
            'CANOPY_PORT': '7800',
            'CANOPY_MESH_PORT': '7801',
            'CANOPY_DISCOVERY_PORT': '7802',
        }


class TestAdminUserWorkspace(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            """
            CREATE TABLE users (
                id TEXT PRIMARY KEY,
                username TEXT,
                password_hash TEXT,
                display_name TEXT,
                account_type TEXT,
                status TEXT,
                origin_peer TEXT,
                bio TEXT,
                avatar_file_id TEXT,
                theme_preference TEXT,
                created_at TEXT
            );
            CREATE TABLE mention_events (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                source_type TEXT NOT NULL,
                source_id TEXT NOT NULL,
                author_id TEXT,
                origin_peer TEXT,
                channel_id TEXT,
                preview TEXT,
                metadata TEXT,
                created_at TEXT,
                acknowledged_at TEXT,
                status TEXT
            );
            CREATE TABLE agent_inbox (
                id TEXT PRIMARY KEY,
                agent_user_id TEXT NOT NULL,
                status TEXT,
                created_at TEXT
            );
            CREATE TABLE agent_runtime_state (
                user_id TEXT PRIMARY KEY,
                last_event_fetch_at TEXT,
                last_event_cursor_seen INTEGER,
                last_inbox_fetch_at TEXT,
                updated_at TEXT
            );
            CREATE TABLE agent_event_subscription_state (
                user_id TEXT PRIMARY KEY,
                custom_enabled INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT
            );
            CREATE TABLE agent_event_subscriptions (
                user_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                updated_at TEXT,
                PRIMARY KEY (user_id, event_type)
            );
            """
        )
        self.conn.executemany(
            """
            INSERT INTO users (
                id, username, password_hash, display_name, account_type, status,
                origin_peer, bio, avatar_file_id, theme_preference, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    'admin-user', 'admin', 'pw-admin', 'Admin',
                    'human', 'active', None, 'Admin bio', None, 'dark',
                    '2026-02-20T08:00:00+00:00'
                ),
                (
                    'agent-local', 'agent_local', 'pw-agent', 'Agent Local',
                    'agent', 'active', None, 'Local agent', None, 'dark',
                    '2026-02-20T08:10:00+00:00'
                ),
                (
                    'agent-remote', 'agent_remote', 'pw-remote', 'Agent Remote',
                    'agent', 'active', 'peer-xyz', 'Remote agent', None, 'dark',
                    '2026-02-20T08:15:00+00:00'
                ),
            ],
        )
        self.conn.executemany(
            """
            INSERT INTO mention_events (
                id, user_id, source_type, source_id, author_id, origin_peer,
                channel_id, preview, metadata, created_at, acknowledged_at, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    'MN-L-1', 'agent-local', 'channel_message', 'msg-1', 'user-alpha', None,
                    'general', 'Please check this issue.', '{}',
                    '2026-02-22T10:00:00+00:00', None, 'new'
                ),
                (
                    'MN-L-2', 'agent-local', 'feed_post', 'post-1', 'user-beta', None,
                    None, 'Older mention', '{}',
                    '2026-02-22T09:00:00+00:00', '2026-02-22T09:05:00+00:00', 'acknowledged'
                ),
            ],
        )
        self.conn.executemany(
            """
            INSERT INTO agent_inbox (
                id, agent_user_id, status, created_at
            ) VALUES (?, ?, ?, ?)
            """,
            [
                ('INB-L-1', 'agent-local', 'pending', '2026-02-22T08:30:00+00:00'),
                ('INB-L-2', 'agent-local', 'handled', '2026-02-22T09:30:00+00:00'),
            ],
        )
        self.conn.execute(
            """
            INSERT INTO agent_runtime_state (
                user_id, last_event_fetch_at, last_event_cursor_seen, last_inbox_fetch_at, updated_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                'agent-local',
                '2026-02-22T10:15:00+00:00',
                57,
                '2026-02-22T10:10:00+00:00',
                '2026-02-22T10:15:00+00:00',
            ),
        )
        self.conn.execute(
            """
            INSERT INTO agent_event_subscription_state (user_id, custom_enabled, updated_at)
            VALUES (?, ?, ?)
            """,
            ('agent-local', 1, '2026-02-22T10:12:00+00:00'),
        )
        self.conn.executemany(
            """
            INSERT INTO agent_event_subscriptions (user_id, event_type, updated_at)
            VALUES (?, ?, ?)
            """,
            [
                ('agent-local', 'mention.created', '2026-02-22T10:12:00+00:00'),
                ('agent-local', 'inbox.item.created', '2026-02-22T10:12:00+00:00'),
            ],
        )
        self.conn.commit()

        self.db_manager = _FakeDbManager(self.conn, owner_user_id='admin-user')
        self.profile_manager = _FakeProfileManager(self.conn)
        self.inbox_manager = _FakeInboxManager()
        self.mention_manager = _FakeMentionManager()
        self.workspace_event_manager = _FakeWorkspaceEventManager()
        self.channel_manager = _FakeChannelManager()
        self.api_key_manager = MagicMock()
        self.api_key_manager.get_all_permissions.return_value = list(Permission)
        self.api_key_manager.get_default_permissions.return_value = [
            Permission.READ_MESSAGES,
            Permission.WRITE_MESSAGES,
            Permission.READ_FEED,
            Permission.WRITE_FEED,
        ]
        self.meshspace_registry_manager = _FakeMeshspaceRegistryManager()

        p2p_manager = MagicMock()
        p2p_manager.is_running.return_value = False
        p2p_manager.get_peer_id.return_value = 'peer-local'

        components = (
            self.db_manager,          # db_manager
            self.api_key_manager,     # api_key_manager
            MagicMock(),              # trust_manager
            MagicMock(),              # message_manager
            self.channel_manager,     # channel_manager
            MagicMock(),              # file_manager
            MagicMock(),              # feed_manager
            MagicMock(),              # interaction_manager
            self.profile_manager,     # profile_manager
            MagicMock(),              # config
            p2p_manager,              # p2p_manager
        )

        self.get_components_patcher = patch(
            'canopy.ui.routes.get_app_components',
            return_value=components,
        )
        self.get_components_patcher.start()
        self.addCleanup(self.get_components_patcher.stop)

        app = Flask(__name__)
        app.config['TESTING'] = True
        app.secret_key = 'test-secret'
        app.config['INBOX_MANAGER'] = self.inbox_manager
        app.config['MENTION_MANAGER'] = self.mention_manager
        app.config['WORKSPACE_EVENT_MANAGER'] = self.workspace_event_manager
        app.config['CANOPY_CONFIG'] = SimpleNamespace(
            meshspace=SimpleNamespace(meshspace_id='default-mesh', name='Default Mesh')
        )
        app.config['MESHSPACE_RECORD'] = dict(self.meshspace_registry_manager.record)
        app.config['MESHSPACE_REGISTRY_MANAGER'] = self.meshspace_registry_manager
        app.register_blueprint(create_ui_blueprint())

        self.client = app.test_client()

    def tearDown(self) -> None:
        self.conn.close()

    def _set_authenticated_session(self, user_id: str = 'admin-user', csrf_token: str = 'csrf-ok') -> None:
        with self.client.session_transaction() as sess:
            sess['authenticated'] = True
            sess['user_id'] = user_id
            sess['_csrf_token'] = csrf_token

    def test_workspace_requires_admin(self) -> None:
        self._set_authenticated_session(user_id='agent-local')

        response = self.client.get('/ajax/admin/users/agent-local/workspace')
        self.assertEqual(response.status_code, 403)
        payload = response.get_json() or {}
        self.assertIn('forbidden', (payload.get('error') or '').lower())

    def test_workspace_snapshot_includes_inbox_and_mentions(self) -> None:
        self._set_authenticated_session()

        response = self.client.get('/ajax/admin/users/agent-local/workspace')
        self.assertEqual(response.status_code, 200)
        payload = response.get_json() or {}
        self.assertTrue(payload.get('success'))
        workspace = payload.get('workspace') or {}
        self.assertEqual((workspace.get('user') or {}).get('id'), 'agent-local')
        self.assertEqual((workspace.get('inbox') or {}).get('pending_count'), 3)
        discrepancies = (workspace.get('inbox') or {}).get('discrepancies') or {}
        self.assertEqual(discrepancies.get('completed_without_completion_ref'), 1)
        self.assertEqual(len(discrepancies.get('items') or []), 1)
        self.assertEqual((discrepancies.get('items') or [])[0].get('id'), 'INB-2')
        self.assertEqual((workspace.get('mentions') or {}).get('unacked_count'), 1)
        runtime = workspace.get('runtime') or {}
        self.assertEqual(runtime.get('last_event_cursor_seen'), 57)
        self.assertEqual(runtime.get('last_event_fetch_at'), '2026-02-22T10:15:00+00:00')
        self.assertEqual(runtime.get('last_inbox_fetch_at'), '2026-02-22T10:10:00+00:00')
        self.assertEqual(runtime.get('event_subscription_source'), 'stored')
        self.assertTrue(runtime.get('event_subscription_custom_enabled'))
        self.assertEqual(
            runtime.get('event_subscription_types'),
            ['inbox.item.created', 'mention.created'],
        )
        self.assertEqual(runtime.get('event_subscription_count'), 2)
        self.assertEqual(runtime.get('event_subscription_updated_at'), '2026-02-22T10:12:00+00:00')
        self.assertIsNotNone(runtime.get('oldest_pending_inbox_age_seconds'))
        self.assertIsNotNone(runtime.get('oldest_unacked_mention_age_seconds'))
        self.assertRegex(runtime.get('oldest_pending_inbox_age_text') or '', r'^\d+[smhd]$')
        self.assertRegex(runtime.get('oldest_unacked_mention_age_text') or '', r'^\d+[smhd]$')
        governance = workspace.get('governance') or {}
        self.assertTrue(governance.get('available'))
        self.assertTrue((governance.get('policy') or {}).get('enabled'))
        self.assertGreaterEqual(len(governance.get('channels') or []), 1)
        channel_ids = {row.get('id') for row in (governance.get('channels') or [])}
        self.assertIn('CremotePrivate', channel_ids)
        self.assertNotIn('CremotePrivateOther', channel_ids)
        self.assertIn('CremotePublic', channel_ids)

    def test_admin_page_renders_governance_manager_and_permission_presets(self) -> None:
        self._set_authenticated_session()

        response = self.client.get('/admin')

        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn('Stage current memberships', html)
        self.assertIn('Quarantine preset', html)
        self.assertIn('Agent default', html)
        self.assertIn('Meshspace Agent API Template', html)
        self.assertIn('Read only', html)
        self.assertNotIn('Subsystem Visibility', html)
        self.assertNotIn('Mesh Sync Diagnostics', html)
        self.assertNotIn('Workspace Event Journal', html)
        self.assertNotIn('Identity Portability (Phase 1)', html)

    def test_admin_workspace_event_status_snapshot(self) -> None:
        self._set_authenticated_session()

        response = self.client.get('/ajax/admin/workspace-events/status?limit=10')
        self.assertEqual(response.status_code, 200)
        payload = response.get_json() or {}
        self.assertTrue(payload.get('success'))
        diagnostics = payload.get('diagnostics') or {}
        heartbeat = payload.get('heartbeat') or {}
        self.assertEqual(diagnostics.get('count'), 3)
        self.assertEqual(diagnostics.get('latest_seq'), 33)
        self.assertEqual((diagnostics.get('type_counts') or {}).get('dm.message.created'), 1)
        self.assertEqual((diagnostics.get('items') or [])[0].get('payload_preview'), 'hello from remote')
        self.assertIn('supported_types', diagnostics)
        self.assertIn('workspace_event_seq', heartbeat)

    def test_admin_can_update_meshspace_agent_permission_template(self) -> None:
        csrf_token = 'csrf-meshspace-template'
        self._set_authenticated_session(csrf_token=csrf_token)

        response = self.client.post(
            '/ajax/admin/meshspace/agent-permissions',
            json={
                'permissions': [
                    Permission.READ_MESSAGES.value,
                    Permission.WRITE_MESSAGES.value,
                    Permission.READ_FILES.value,
                    Permission.WRITE_FILES.value,
                ]
            },
            headers={'X-CSRFToken': csrf_token},
        )
        self.assertEqual(response.status_code, 200)
        payload = response.get_json() or {}
        self.assertTrue(payload.get('success'))
        self.assertEqual(
            payload.get('default_agent_permissions'),
            [
                Permission.READ_MESSAGES.value,
                Permission.WRITE_MESSAGES.value,
                Permission.READ_FILES.value,
                Permission.WRITE_FILES.value,
            ],
        )

    def test_agent_self_service_key_generation_uses_meshspace_template_defaults(self) -> None:
        csrf_token = 'csrf-self-key'
        self._set_authenticated_session(user_id='agent-local', csrf_token=csrf_token)
        self.api_key_manager.generate_key.return_value = 'raw-agent-key'
        self.meshspace_registry_manager.record['default_agent_permissions'] = [
            Permission.READ_MESSAGES.value,
            Permission.WRITE_MESSAGES.value,
            Permission.READ_FILES.value,
            Permission.WRITE_FILES.value,
        ]

        response = self.client.post(
            '/ajax/generate_key',
            json={},
            headers={'X-CSRFToken': csrf_token},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json() or {}
        self.assertTrue(payload.get('success'))
        self.assertEqual(payload.get('api_key'), 'raw-agent-key')
        self.assertEqual(
            payload.get('permissions'),
            [
                Permission.READ_MESSAGES.value,
                Permission.WRITE_MESSAGES.value,
                Permission.READ_FILES.value,
                Permission.WRITE_FILES.value,
            ],
        )
        self.api_key_manager.generate_key.assert_called_once()
        _, permissions, expires_days = self.api_key_manager.generate_key.call_args[0]
        self.assertEqual(expires_days, None)
        self.assertEqual(
            permissions,
            [
                Permission.READ_MESSAGES,
                Permission.WRITE_MESSAGES,
                Permission.READ_FILES,
                Permission.WRITE_FILES,
            ],
        )

    def test_admin_can_update_local_user_profile(self) -> None:
        csrf_token = 'csrf-profile'
        self._set_authenticated_session(csrf_token=csrf_token)

        response = self.client.post(
            '/ajax/admin/users/agent-local/profile',
            json={
                'display_name': 'Ops Agent',
                'bio': 'Updated by admin',
                'account_type': 'agent',
                'theme_preference': 'eco',
            },
            headers={'X-CSRFToken': csrf_token},
        )
        self.assertEqual(response.status_code, 200)
        payload = response.get_json() or {}
        self.assertTrue(payload.get('success'))

        row = self.conn.execute(
            "SELECT display_name, bio, account_type, theme_preference FROM users WHERE id = 'agent-local'"
        ).fetchone()
        self.assertEqual(row['display_name'], 'Ops Agent')
        self.assertEqual(row['bio'], 'Updated by admin')
        self.assertEqual(row['account_type'], 'agent')
        self.assertEqual(row['theme_preference'], 'eco')

    def test_admin_profile_update_rejects_remote_user(self) -> None:
        csrf_token = 'csrf-remote'
        self._set_authenticated_session(csrf_token=csrf_token)

        response = self.client.post(
            '/ajax/admin/users/agent-remote/profile',
            json={'display_name': 'Remote Rename'},
            headers={'X-CSRFToken': csrf_token},
        )
        self.assertEqual(response.status_code, 403)
        payload = response.get_json() or {}
        self.assertIn('remote', (payload.get('error') or '').lower())

    def test_admin_can_upload_avatar_for_local_user(self) -> None:
        csrf_token = 'csrf-avatar'
        self._set_authenticated_session(csrf_token=csrf_token)

        response = self.client.post(
            '/ajax/admin/users/agent-local/avatar',
            data={
                'avatar': (BytesIO(b'\x89PNG\r\n\x1a\nadmin-avatar'), 'avatar.png', 'image/png'),
            },
            content_type='multipart/form-data',
            headers={'X-CSRFToken': csrf_token},
        )
        self.assertEqual(response.status_code, 200)
        payload = response.get_json() or {}
        self.assertTrue(payload.get('success'))
        self.assertEqual(payload.get('avatar_url'), '/files/file-admin-avatar')

        row = self.conn.execute(
            "SELECT avatar_file_id FROM users WHERE id = 'agent-local'"
        ).fetchone()
        self.assertEqual(row['avatar_file_id'], 'file-admin-avatar')

    def test_admin_can_rebuild_user_inbox(self) -> None:
        csrf_token = 'csrf-rebuild'
        self._set_authenticated_session(csrf_token=csrf_token)

        response = self.client.post(
            '/ajax/admin/users/agent-local/inbox/rebuild',
            json={'window_hours': 36, 'limit': 400},
            headers={'X-CSRFToken': csrf_token},
        )
        self.assertEqual(response.status_code, 200)
        payload = response.get_json() or {}
        self.assertTrue(payload.get('success'))
        result = payload.get('result') or {}
        self.assertEqual(result.get('created'), 4)

        self.assertEqual(len(self.inbox_manager.rebuild_calls), 1)
        call = self.inbox_manager.rebuild_calls[0]
        self.assertEqual(call['user_id'], 'agent-local')
        self.assertEqual(call['window_hours'], 36)
        self.assertEqual(call['limit'], 400)

    def test_admin_can_update_local_user_governance(self) -> None:
        csrf_token = 'csrf-governance'
        self._set_authenticated_session(csrf_token=csrf_token)

        response = self.client.post(
            '/ajax/admin/users/agent-local/governance',
            json={
                'enabled': True,
                'block_public_channels': True,
                'restrict_to_allowed_channels': True,
                'allowed_channel_ids': ['Cprivate'],
                'enforce_now': True,
            },
            headers={'X-CSRFToken': csrf_token},
        )
        self.assertEqual(response.status_code, 200)
        payload = response.get_json() or {}
        self.assertTrue(payload.get('success'))
        self.assertEqual(len(self.channel_manager.saved_payloads), 1)
        self.assertEqual(self.channel_manager.saved_payloads[0]['user_id'], 'agent-local')
        self.assertEqual(self.channel_manager.saved_payloads[0]['allowed_channel_ids'], ['Cprivate'])
        self.assertEqual(self.channel_manager.enforce_calls, ['agent-local'])
        enforcement = payload.get('enforcement') or {}
        self.assertEqual(enforcement.get('removed_count'), 2)

    def test_admin_governance_update_ignores_remote_private_channel_ids(self) -> None:
        csrf_token = 'csrf-governance-filter'
        self._set_authenticated_session(csrf_token=csrf_token)

        response = self.client.post(
            '/ajax/admin/users/agent-local/governance',
            json={
                'enabled': True,
                'block_public_channels': False,
                'restrict_to_allowed_channels': True,
                'allowed_channel_ids': ['Cprivate', 'CremotePrivate', 'CremotePrivateOther', 'CremotePublic'],
                'enforce_now': False,
            },
            headers={'X-CSRFToken': csrf_token},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json() or {}
        self.assertTrue(payload.get('success'))
        self.assertEqual(
            self.channel_manager.saved_payloads[-1]['allowed_channel_ids'],
            ['Cprivate', 'CremotePrivate', 'CremotePublic'],
        )

    def test_admin_governance_update_rejects_remote_user(self) -> None:
        csrf_token = 'csrf-governance-remote'
        self._set_authenticated_session(csrf_token=csrf_token)

        response = self.client.post(
            '/ajax/admin/users/agent-remote/governance',
            json={
                'enabled': True,
                'block_public_channels': True,
                'restrict_to_allowed_channels': False,
                'allowed_channel_ids': [],
                'enforce_now': True,
            },
            headers={'X-CSRFToken': csrf_token},
        )
        self.assertEqual(response.status_code, 403)
        payload = response.get_json() or {}
        self.assertIn('remote', (payload.get('error') or '').lower())


if __name__ == '__main__':
    unittest.main()
