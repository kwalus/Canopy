"""Tests for Workstream v1 manager and API."""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import contextmanager
from datetime import datetime, timezone

from flask import Flask

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

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

from canopy.api.workstreams import create_workstream_api_blueprint
from canopy.core.workstreams import WorkstreamManager
from canopy.security.api_keys import ApiKeyInfo, Permission


class _TempDb:
    def __init__(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, 'workstreams.db')
        self._bootstrap()

    def cleanup(self) -> None:
        self.tmp.cleanup()

    @contextmanager
    def get_connection(self, *args, **kwargs):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys = ON')
        try:
            yield conn
        finally:
            conn.close()

    def _bootstrap(self) -> None:
        with self.get_connection() as conn:
            conn.executescript(
                """
                CREATE TABLE users (
                    id TEXT PRIMARY KEY,
                    username TEXT UNIQUE NOT NULL,
                    public_key TEXT NOT NULL,
                    display_name TEXT,
                    account_type TEXT DEFAULT 'human'
                );
                CREATE TABLE profiles (
                    user_id TEXT PRIMARY KEY,
                    username TEXT,
                    display_name TEXT,
                    avatar_url TEXT,
                    avatar_file_id TEXT
                );
                CREATE TABLE channel_members (
                    channel_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    role TEXT DEFAULT 'member',
                    PRIMARY KEY(channel_id, user_id)
                );
                """
            )
            for user_id, username, display, account_type in [
                ('u_owner', 'owner', 'Owner Human', 'human'),
                ('u_agent', 'agent', 'Agent One', 'agent'),
                ('u_other', 'other', 'Other User', 'human'),
            ]:
                conn.execute(
                    'INSERT INTO users (id, username, public_key, display_name, account_type) VALUES (?, ?, ?, ?, ?)',
                    (user_id, username, 'pub', display, account_type),
                )
                conn.execute(
                    'INSERT INTO profiles (user_id, username, display_name) VALUES (?, ?, ?)',
                    (user_id, username, display),
                )
            conn.execute("INSERT INTO channel_members (channel_id, user_id, role) VALUES ('C1', 'u_owner', 'admin')")
            conn.execute("INSERT INTO channel_members (channel_id, user_id, role) VALUES ('C1', 'u_agent', 'member')")
            conn.commit()


class _FakeApiKeyManager:
    def validate_key(self, raw_key, required_permission=None):
        user_id = {'owner-key': 'u_owner', 'agent-key': 'u_agent', 'other-key': 'u_other'}.get(raw_key)
        if not user_id:
            return None
        perms = {Permission.READ_FEED, Permission.WRITE_FEED}
        if required_permission and required_permission not in perms:
            return None
        return ApiKeyInfo(
            id=f'key-{user_id}',
            user_id=user_id,
            key_hash='hash',
            permissions=perms,
            created_at=datetime.now(timezone.utc),
        )


class _FakeChannelManager:
    def get_channel_access_decision(self, channel_id, user_id, require_membership=True):
        return {'allowed': channel_id == 'C1' and user_id in {'u_owner', 'u_agent'}, 'reason': 'ok' if user_id in {'u_owner', 'u_agent'} else 'not_member'}


class WorkstreamManagerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.db = _TempDb()
        self.manager = WorkstreamManager(self.db)

    def tearDown(self) -> None:
        self.db.cleanup()

    def test_create_workstream_hydrates_participants_and_artifacts(self) -> None:
        ws = self.manager.create_workstream(
            title='Run cellular automata review',
            owner_user_id='u_owner',
            created_by='u_owner',
            channel_id='C1',
            objective='Coordinate agents and evidence.',
            participants=[{'user_id': 'u_agent', 'role': 'lead'}],
            artifacts=[{'artifact_type': 'digestion', 'ref_id': 'Dgabc123456', 'title': 'Evidence corpus'}],
        )

        self.assertTrue(ws.id.startswith('Ws'))
        self.assertEqual(ws.status, 'active')
        self.assertEqual({p.user_id for p in ws.participants}, {'u_owner', 'u_agent'})
        self.assertEqual(ws.artifacts[0].artifact_type, 'digestion')
        self.assertEqual(ws.owner['display_name'], 'Owner Human')

    def test_events_are_idempotent_by_dedupe_key(self) -> None:
        ws = self.manager.create_workstream(title='Idempotent run', owner_user_id='u_owner', created_by='u_owner')
        first = self.manager.add_event(ws.id, actor_user_id='u_owner', title='Checkpoint', dedupe_key='abc')
        second = self.manager.add_event(ws.id, actor_user_id='u_owner', title='Checkpoint retry', dedupe_key='abc')
        self.assertEqual(first.id, second.id)

    def test_channel_members_can_view_but_not_edit_until_claimed(self) -> None:
        ws = self.manager.create_workstream(title='Visible in channel', owner_user_id='u_owner', created_by='u_owner', channel_id='C1')
        self.assertTrue(self.manager.user_can_view(ws.id, 'u_agent'))
        self.assertFalse(self.manager.user_can_edit(ws.id, 'u_agent'))
        self.manager.set_participants(ws.id, actor_user_id='u_owner', participants=[{'user_id': 'u_agent', 'role': 'contributor'}])
        self.assertTrue(self.manager.user_can_edit(ws.id, 'u_agent'))

    def test_claim_preserves_specific_role_and_reviewer_can_contribute(self) -> None:
        ws = self.manager.create_workstream(
            title='Review stream',
            owner_user_id='u_owner',
            created_by='u_owner',
            participants=[{'user_id': 'u_agent', 'role': 'reviewer'}],
        )

        claimed = self.manager.claim_workstream(ws.id, actor_user_id='u_agent')
        roles = {p.user_id: p.role for p in claimed.participants}
        self.assertEqual(roles['u_agent'], 'reviewer')
        self.assertFalse(self.manager.user_can_edit(ws.id, 'u_agent'))
        self.assertTrue(self.manager.user_can_contribute(ws.id, 'u_agent'))

    def test_blocker_events_store_event_state_without_forcing_status(self) -> None:
        ws = self.manager.create_workstream(title='Blocked run', owner_user_id='u_owner', created_by='u_owner')
        blocker = self.manager.add_event(
            ws.id,
            actor_user_id='u_owner',
            event_type='blocker',
            title='Need source file',
            dedupe_key='blocker:file',
        )
        resolved = self.manager.add_event(
            ws.id,
            actor_user_id='u_owner',
            event_type='blocker',
            title='Source file attached',
            event_state='resolved',
            status='active',
            dedupe_key='blocker:file:resolved',
        )

        self.assertEqual(blocker.metadata['event_state'], 'open')
        self.assertEqual(resolved.metadata['event_state'], 'resolved')
        self.assertEqual(self.manager.get_workstream(ws.id).status, 'active')


class WorkstreamApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.db = _TempDb()
        self.manager = WorkstreamManager(self.db)
        app = Flask(__name__)
        app.secret_key = 'workstream-api-test'
        app.config['TESTING'] = True
        app.config['WORKSTREAM_MANAGER'] = self.manager
        app.config['API_KEY_MANAGER'] = _FakeApiKeyManager()
        app.config['CHANNEL_MANAGER'] = _FakeChannelManager()
        app.register_blueprint(create_workstream_api_blueprint(), url_prefix='/api/v1')
        self.client = app.test_client()

    def tearDown(self) -> None:
        self.db.cleanup()

    def test_create_get_and_add_artifact(self) -> None:
        created = self.client.post(
            '/api/v1/workstreams',
            json={
                'title': 'API Workstream',
                'channel_id': 'C1',
                'participants': [{'user_id': 'u_agent', 'role': 'lead'}],
            },
            headers={'X-API-Key': 'owner-key'},
        )
        self.assertEqual(created.status_code, 201)
        ws_id = created.get_json()['workstream']['id']

        artifact = self.client.post(
            f'/api/v1/workstreams/{ws_id}/artifacts',
            json={'artifact_type': 'file', 'ref_id': 'Fabc123456789', 'title': 'Work product'},
            headers={'X-API-Key': 'agent-key'},
        )
        self.assertEqual(artifact.status_code, 201)

        fetched = self.client.get(f'/api/v1/workstreams/{ws_id}', headers={'X-API-Key': 'agent-key'})
        self.assertEqual(fetched.status_code, 200)
        payload = fetched.get_json()['workstream']
        self.assertEqual(payload['artifacts'][0]['title'], 'Work product')
        self.assertIn('agent_reference', fetched.get_json())

    def test_reviewer_claim_can_add_review_without_role_downgrade(self) -> None:
        created = self.client.post(
            '/api/v1/workstreams',
            json={
                'title': 'API Review Workstream',
                'channel_id': 'C1',
                'participants': [{'user_id': 'u_agent', 'role': 'reviewer'}],
            },
            headers={'X-API-Key': 'owner-key'},
        )
        self.assertEqual(created.status_code, 201)
        ws_id = created.get_json()['workstream']['id']

        claimed = self.client.post(f'/api/v1/workstreams/{ws_id}/claim', headers={'X-API-Key': 'agent-key'})
        self.assertEqual(claimed.status_code, 200)
        roles = {p['user_id']: p['role'] for p in claimed.get_json()['workstream']['participants']}
        self.assertEqual(roles['u_agent'], 'reviewer')

        review = self.client.post(
            f'/api/v1/workstreams/{ws_id}/events',
            json={'event_type': 'review', 'title': 'Review complete', 'event_state': 'confirmed'},
            headers={'X-API-Key': 'agent-key'},
        )
        self.assertEqual(review.status_code, 201)
        self.assertEqual(review.get_json()['event']['metadata']['event_state'], 'confirmed')

    def test_summary_get_omits_heavy_details_for_link_preview(self) -> None:
        created = self.client.post(
            '/api/v1/workstreams',
            json={'title': 'Previewable Workstream', 'channel_id': 'C1'},
            headers={'X-API-Key': 'owner-key'},
        )
        self.assertEqual(created.status_code, 201)
        ws_id = created.get_json()['workstream']['id']

        fetched = self.client.get(f'/api/v1/workstreams/{ws_id}?summary=1', headers={'X-API-Key': 'agent-key'})
        self.assertEqual(fetched.status_code, 200)
        payload = fetched.get_json()
        self.assertEqual(payload['workstream']['title'], 'Previewable Workstream')
        self.assertNotIn('events', payload['workstream'])
        self.assertIsNone(payload['agent_reference'])

    def test_non_member_cannot_create_channel_workstream(self) -> None:
        response = self.client.post(
            '/api/v1/workstreams',
            json={'title': 'Nope', 'channel_id': 'C1'},
            headers={'X-API-Key': 'other-key'},
        )
        self.assertEqual(response.status_code, 403)


if __name__ == '__main__':
    unittest.main()
