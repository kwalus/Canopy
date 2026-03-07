"""Regression tests for agent reliability endpoints (claims, discovery, heartbeat cursors)."""

import os
import sqlite3
import sys
import tempfile
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
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

from canopy.api.routes import create_api_blueprint
from canopy.core.agent_heartbeat import build_agent_heartbeat_snapshot
from canopy.core.mentions import MentionManager
from canopy.security.api_keys import ApiKeyInfo, Permission


class _FakeDbManager:
    def __init__(self, conn: sqlite3.Connection, db_path: Path) -> None:
        self._conn = conn
        self.db_path = db_path

    def get_connection(self) -> sqlite3.Connection:
        return self._conn

    def get_user(self, user_id: str):
        row = self._conn.execute(
            "SELECT * FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
        return dict(row) if row else None


class _FakeApiKeyManager:
    def __init__(self, key_to_user: dict[str, str]) -> None:
        self._key_to_user = key_to_user

    def validate_key(self, raw_key: str, required_permission=None):
        user_id = self._key_to_user.get(raw_key)
        if not user_id:
            return None
        perms = {
            Permission.READ_FEED,
            Permission.WRITE_FEED,
            Permission.READ_MESSAGES,
            Permission.WRITE_MESSAGES,
            Permission.MANAGE_KEYS,
        }
        if required_permission and required_permission not in perms:
            return None
        return ApiKeyInfo(
            id=f"key-{user_id}",
            user_id=user_id,
            key_hash="hash",
            permissions=perms,
            created_at=datetime.now(timezone.utc),
        )


class _FakeP2PManager:
    def get_mesh_diagnostics(self):
        return {
            'connected_peers': ['peer-alpha'],
            'known_peers_count': 2,
            'pending_messages': {'total': 7},
            'sync': {'queue_depth': 3},
        }

    def get_connected_peers(self):
        return ['peer-alpha']

    def get_discovered_peers(self):
        return [
            {
                'peer_id': 'peer-alpha',
                'address': '192.168.1.11',
                'port': 7771,
                'connected': True,
            }
        ]

    def get_peer_versions(self):
        return {
            'peer-alpha': {
                'canopy_version': '0.4.30',
                'protocol_version': 1,
                'compatible_protocol': True,
            }
        }


class TestAgentReliabilityEndpoints(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)

        self.db_file = Path(self.tempdir.name) / 'test_canopy.db'
        self.conn = sqlite3.connect(str(self.db_file))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            """
            CREATE TABLE users (
                id TEXT PRIMARY KEY,
                username TEXT,
                display_name TEXT,
                public_key TEXT,
                password_hash TEXT,
                account_type TEXT,
                status TEXT,
                origin_peer TEXT,
                bio TEXT,
                agent_directives TEXT,
                created_at TEXT
            );
            CREATE TABLE agent_inbox (
                id TEXT PRIMARY KEY,
                agent_user_id TEXT,
                source_type TEXT,
                source_id TEXT,
                status TEXT,
                created_at TEXT
            );
            CREATE TABLE tasks (
                id TEXT PRIMARY KEY,
                title TEXT,
                status TEXT,
                priority TEXT,
                objective_id TEXT,
                assigned_to TEXT,
                due_at TEXT,
                updated_at TEXT
            );
            CREATE TABLE objectives (
                id TEXT PRIMARY KEY,
                title TEXT,
                status TEXT,
                updated_at TEXT
            );
            CREATE TABLE objective_members (
                objective_id TEXT,
                user_id TEXT,
                role TEXT
            );
            CREATE TABLE requests (
                id TEXT PRIMARY KEY,
                title TEXT,
                status TEXT,
                priority TEXT,
                due_at TEXT,
                updated_at TEXT
            );
            CREATE TABLE request_members (
                request_id TEXT,
                user_id TEXT,
                role TEXT
            );
            CREATE TABLE handoff_notes (
                id TEXT PRIMARY KEY,
                owner TEXT
            );
            """
        )
        self.conn.executemany(
            """
            INSERT INTO users (
                id, username, display_name, public_key, password_hash,
                account_type, status, origin_peer, bio, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    'agent-a', 'Agent_A.alpha1', 'Agent A', 'pk-a', 'pw-a',
                    'agent', 'active', None, 'Coordination and QA',
                    '2026-02-23T10:00:00+00:00'
                ),
                (
                    'agent-b', 'Agent_B.beta2', 'Agent B', 'pk-b', 'pw-b',
                    'agent', 'active', None, 'Implementation and testing',
                    '2026-02-23T10:01:00+00:00'
                ),
                (
                    'forge-agent', 'Forge_McClaw.74ugCK', 'Forge McClaw', 'pk-f', 'pw-f',
                    'agent', 'active', None, 'Build and systems',
                    '2026-02-23T10:02:00+00:00'
                ),
                (
                    'human-owner', 'maddog', 'Maddog', 'pk-h', 'pw-h',
                    'human', 'active', None, 'Owner account',
                    '2026-02-23T10:03:00+00:00'
                ),
            ],
        )
        self.conn.commit()

        self.db_manager = _FakeDbManager(self.conn, self.db_file)
        self.mention_manager = MentionManager(self.db_manager)

        self.api_key_manager = _FakeApiKeyManager(
            {
                'key-agent-a': 'agent-a',
                'key-agent-b': 'agent-b',
                'key-owner': 'human-owner',
            }
        )
        p2p_manager = _FakeP2PManager()

        self.message_manager = MagicMock()
        self.message_manager.get_messages.return_value = []

        components = (
            self.db_manager,              # db_manager
            self.api_key_manager,         # api_key_manager
            MagicMock(),                  # trust_manager
            self.message_manager,         # message_manager
            MagicMock(),                  # channel_manager
            MagicMock(),                  # file_manager
            MagicMock(),                  # feed_manager
            MagicMock(),                  # interaction_manager
            MagicMock(),                  # profile_manager
            MagicMock(),                  # config
            p2p_manager,                  # p2p_manager
        )

        self.get_components_patcher = patch(
            'canopy.api.routes.get_app_components',
            return_value=components,
        )
        self.get_components_patcher.start()
        self.addCleanup(self.get_components_patcher.stop)

        app = Flask(__name__)
        app.config['TESTING'] = True
        app.secret_key = 'test-secret'
        app.config['MENTION_MANAGER'] = self.mention_manager
        api_bp = create_api_blueprint()
        app.register_blueprint(api_bp, url_prefix='/api/v1')
        app.register_blueprint(api_bp, url_prefix='/api', name='api_legacy')

        self.client = app.test_client()

    def tearDown(self) -> None:
        self.conn.close()

    def _headers(self, key: str) -> dict[str, str]:
        return {
            'X-API-Key': key,
            'Content-Type': 'application/json',
        }

    def test_mentions_claim_prevents_duplicate_replies_until_owner_acks(self) -> None:
        mention_ids = self.mention_manager.record_mentions(
            user_ids=['agent-a', 'agent-b'],
            source_type='channel_message',
            source_id='msg-claim-1',
            author_id='human-owner',
            channel_id='general',
            preview='Please investigate this incident.',
        )
        self.assertEqual(len(mention_ids), 2)
        mention_a, mention_b = mention_ids

        claim_resp = self.client.post(
            '/api/v1/mentions/claim',
            json={'mention_id': mention_a, 'ttl_seconds': 120},
            headers=self._headers('key-agent-a'),
        )
        self.assertEqual(claim_resp.status_code, 200)
        claim_data = claim_resp.get_json() or {}
        self.assertTrue(claim_data.get('claimed'))

        blocked_resp = self.client.post(
            '/api/v1/mentions/claim',
            json={'mention_id': mention_b, 'ttl_seconds': 120},
            headers=self._headers('key-agent-b'),
        )
        self.assertEqual(blocked_resp.status_code, 409)
        blocked_data = blocked_resp.get_json() or {}
        self.assertEqual(blocked_data.get('reason'), 'already_claimed')
        self.assertEqual(blocked_data.get('action_hint'), 'retry_after_ttl')
        self.assertIsInstance(blocked_data.get('retry_after_seconds'), int)
        self.assertGreaterEqual(blocked_data.get('retry_after_seconds'), 0)
        self.assertIn('Retry-After', blocked_resp.headers)

        mentions_resp = self.client.get(
            '/api/v1/mentions?include_acknowledged=1',
            headers=self._headers('key-agent-b'),
        )
        self.assertEqual(mentions_resp.status_code, 200)
        mentions_payload = mentions_resp.get_json() or {}
        mentions = mentions_payload.get('mentions') or []
        self.assertEqual(len(mentions), 1)
        claim_payload = mentions[0].get('claim') or {}
        self.assertEqual(claim_payload.get('claimed_by_user_id'), 'agent-a')
        self.assertTrue(claim_payload.get('active'))

        ack_resp = self.client.post(
            '/api/v1/mentions/ack',
            json={'mention_ids': [mention_a]},
            headers=self._headers('key-agent-a'),
        )
        self.assertEqual(ack_resp.status_code, 200)
        self.assertEqual((ack_resp.get_json() or {}).get('acknowledged'), 1)

        claim_after_ack = self.client.post(
            '/api/v1/mentions/claim',
            json={'mention_id': mention_b, 'ttl_seconds': 120},
            headers=self._headers('key-agent-b'),
        )
        self.assertEqual(claim_after_ack.status_code, 200)
        self.assertTrue((claim_after_ack.get_json() or {}).get('claimed'))

    def test_mentions_claim_supports_inbox_id_resolution(self) -> None:
        mention_ids = self.mention_manager.record_mentions(
            user_ids=['agent-a', 'agent-b'],
            source_type='channel_message',
            source_id='msg-claim-inbox-1',
            author_id='human-owner',
            channel_id='general',
            preview='Please claim via inbox id.',
        )
        self.assertEqual(len(mention_ids), 2)
        mention_a, mention_b = mention_ids

        self.conn.executemany(
            """
            INSERT INTO agent_inbox (id, agent_user_id, source_type, source_id, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                ('INB-claim-a', 'agent-a', 'channel_message', 'msg-claim-inbox-1', 'pending', '2026-02-23 11:30:00.000000'),
                ('INB-claim-b', 'agent-b', 'channel_message', 'msg-claim-inbox-1', 'pending', '2026-02-23 11:30:01.000000'),
            ],
        )
        self.conn.commit()

        claim_resp = self.client.post(
            '/api/v1/mentions/claim',
            json={'inbox_id': 'INB-claim-a', 'ttl_seconds': 120},
            headers=self._headers('key-agent-a'),
        )
        self.assertEqual(claim_resp.status_code, 200)
        claim_payload = claim_resp.get_json() or {}
        self.assertTrue(claim_payload.get('claimed'))
        self.assertEqual(claim_payload.get('inbox_id'), 'INB-claim-a')
        self.assertEqual((claim_payload.get('claim') or {}).get('metadata', {}).get('inbox_id'), 'INB-claim-a')

        blocked_resp = self.client.post(
            '/api/v1/mentions/claim',
            json={'inbox_id': 'INB-claim-b', 'ttl_seconds': 120},
            headers=self._headers('key-agent-b'),
        )
        self.assertEqual(blocked_resp.status_code, 409)
        blocked_payload = blocked_resp.get_json() or {}
        self.assertEqual(blocked_payload.get('reason'), 'already_claimed')
        self.assertEqual(blocked_payload.get('action_hint'), 'retry_after_ttl')
        self.assertIsInstance(blocked_payload.get('retry_after_seconds'), int)
        self.assertIn('Retry-After', blocked_resp.headers)

        get_resp = self.client.get(
            '/api/v1/mentions/claim?inbox_id=INB-claim-b',
            headers=self._headers('key-agent-b'),
        )
        self.assertEqual(get_resp.status_code, 200)
        get_payload = get_resp.get_json() or {}
        self.assertTrue(get_payload.get('claimed'))
        self.assertEqual((get_payload.get('claim') or {}).get('claimed_by_user_id'), 'agent-a')

        ack_resp = self.client.post(
            '/api/v1/mentions/ack',
            json={'mention_ids': [mention_a]},
            headers=self._headers('key-agent-a'),
        )
        self.assertEqual(ack_resp.status_code, 200)
        self.assertEqual((ack_resp.get_json() or {}).get('acknowledged'), 1)

        claim_after_release = self.client.post(
            '/api/v1/mentions/claim',
            json={'inbox_id': 'INB-claim-b', 'ttl_seconds': 120},
            headers=self._headers('key-agent-b'),
        )
        self.assertEqual(claim_after_release.status_code, 200)
        self.assertTrue((claim_after_release.get_json() or {}).get('claimed'))

        _ = mention_b  # keep both generated mention ids intentionally exercised in flow

    def test_legacy_agent_prefix_and_aliases_keep_claim_ack_and_messages_live(self) -> None:
        mention_ids = self.mention_manager.record_mentions(
            user_ids=['agent-a'],
            source_type='channel_message',
            source_id='msg-legacy-1',
            author_id='human-owner',
            channel_id='general',
            preview='Legacy agent compatibility path test.',
        )
        mention_id = mention_ids[0]

        instructions_resp = self.client.get('/api/agent-instructions')
        self.assertEqual(instructions_resp.status_code, 200)
        instructions_payload = instructions_resp.get_json() or {}
        self.assertEqual(instructions_payload.get('api_prefix'), '/api/v1')
        self.assertIn('/api', instructions_payload.get('api_aliases') or [])
        mentions_meta = instructions_payload.get('mentions') or {}
        self.assertEqual((mentions_meta.get('claim') or {}).get('path'), '/api/v1/mentions/claim')
        self.assertIn('/api/v1/claim', (mentions_meta.get('claim') or {}).get('aliases') or [])
        self.assertIn('/api/acknowledge', mentions_meta.get('ack_aliases') or [])

        claim_resp = self.client.post(
            '/api/v1/claim',
            json={'mention_id': mention_id, 'ttl_seconds': 120},
            headers=self._headers('key-agent-a'),
        )
        self.assertEqual(claim_resp.status_code, 200)
        self.assertTrue((claim_resp.get_json() or {}).get('claimed'))

        ack_resp = self.client.post(
            '/api/acknowledge',
            json={'mention_ids': [mention_id]},
            headers=self._headers('key-agent-a'),
        )
        self.assertEqual(ack_resp.status_code, 200)
        self.assertEqual((ack_resp.get_json() or {}).get('acknowledged'), 1)

        inbox_resp = self.client.get(
            '/api/agents/me/inbox',
            headers=self._headers('key-agent-a'),
        )
        self.assertEqual(inbox_resp.status_code, 200)
        self.assertEqual((inbox_resp.get_json() or {}).get('count'), 0)

        messages_resp = self.client.get(
            '/api/messages',
            headers=self._headers('key-agent-a'),
        )
        self.assertEqual(messages_resp.status_code, 200)
        self.assertEqual((messages_resp.get_json() or {}).get('count'), 0)

    def test_agents_endpoint_exposes_stable_handles_and_workload_counts(self) -> None:
        self.mention_manager.record_mentions(
            user_ids=['forge-agent'],
            source_type='channel_message',
            source_id='msg-forge-1',
            author_id='human-owner',
            channel_id='general',
            preview='Forge please handle the mesh issue.',
        )
        self.conn.execute(
            """
            INSERT INTO agent_inbox (id, agent_user_id, source_type, source_id, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            ('INB-forge-1', 'forge-agent', 'channel_message', 'msg-forge-1', 'pending', '2026-02-23 11:01:00.000000'),
        )
        self.conn.commit()

        response = self.client.get(
            '/api/v1/agents?include_humans=1&include_skills=0&limit=20',
            headers=self._headers('key-agent-a'),
        )
        self.assertEqual(response.status_code, 200)
        payload = response.get_json() or {}
        agents = payload.get('agents') or []
        self.assertGreaterEqual(len(agents), 4)

        forge = next((a for a in agents if a.get('user_id') == 'forge-agent'), None)
        self.assertIsNotNone(forge)
        self.assertEqual(forge.get('stable_handle'), 'Forge_McClaw')
        self.assertIn('Forge_McClaw.74ugCK', forge.get('mention_handles') or [])
        self.assertEqual(forge.get('unacked_mentions'), 1)
        self.assertEqual(forge.get('pending_inbox'), 1)
        self.assertIn(forge.get('presence_state'), {'online', 'recent', 'idle', 'offline', 'no_checkin', 'remote_unknown'})
        self.assertIn('last_check_in_at', forge)

    def test_agents_endpoint_active_and_remote_filters_handle_mixed_case_values(self) -> None:
        self.conn.execute(
            """
            UPDATE users
            SET account_type = ?, status = ?, origin_peer = ?
            WHERE id = ?
            """,
            ('Agent', 'Active', '  ', 'forge-agent'),
        )
        self.conn.execute(
            """
            UPDATE users
            SET account_type = ?, status = ?, origin_peer = ?
            WHERE id = ?
            """,
            ('Agent', 'ACTIVE', 'peer-xyz', 'agent-b'),
        )
        self.conn.execute(
            """
            UPDATE users
            SET account_type = ?, status = ?
            WHERE id = ?
            """,
            ('Agent', 'disabled', 'agent-a'),
        )
        self.conn.commit()

        active_resp = self.client.get(
            '/api/v1/agents?include_humans=0&active_only=1&include_remote=1&include_skills=0&limit=50',
            headers=self._headers('key-agent-a'),
        )
        self.assertEqual(active_resp.status_code, 200)
        active_payload = active_resp.get_json() or {}
        active_ids = {a.get('user_id') for a in (active_payload.get('agents') or [])}
        self.assertIn('forge-agent', active_ids)
        self.assertIn('agent-b', active_ids)
        self.assertNotIn('agent-a', active_ids)

        for agent in active_payload.get('agents') or []:
            self.assertEqual(agent.get('account_type'), 'agent')
            self.assertEqual(agent.get('status'), 'active')

        local_only_resp = self.client.get(
            '/api/v1/agents?include_humans=0&active_only=1&include_remote=0&include_skills=0&limit=50',
            headers=self._headers('key-agent-a'),
        )
        self.assertEqual(local_only_resp.status_code, 200)
        local_only_payload = local_only_resp.get_json() or {}
        local_only_ids = {a.get('user_id') for a in (local_only_payload.get('agents') or [])}
        self.assertIn('forge-agent', local_only_ids)
        self.assertNotIn('agent-b', local_only_ids)

    def test_heartbeat_updates_presence_for_agent_discovery_badges(self) -> None:
        before_resp = self.client.get(
            '/api/v1/agents?include_humans=0&include_skills=0&limit=20',
            headers=self._headers('key-agent-a'),
        )
        self.assertEqual(before_resp.status_code, 200)
        before_agents = (before_resp.get_json() or {}).get('agents') or []
        before_agent_a = next((a for a in before_agents if a.get('user_id') == 'agent-a'), None)
        self.assertIsNotNone(before_agent_a)
        self.assertIn(before_agent_a.get('presence_state'), {'no_checkin', 'remote_unknown', 'offline', 'recent', 'idle', 'online'})

        heartbeat_resp = self.client.get(
            '/api/v1/agents/me/heartbeat',
            headers=self._headers('key-agent-a'),
        )
        self.assertEqual(heartbeat_resp.status_code, 200)

        after_resp = self.client.get(
            '/api/v1/agents?include_humans=0&include_skills=0&limit=20',
            headers=self._headers('key-agent-a'),
        )
        self.assertEqual(after_resp.status_code, 200)
        after_agents = (after_resp.get_json() or {}).get('agents') or []
        after_agent_a = next((a for a in after_agents if a.get('user_id') == 'agent-a'), None)
        self.assertIsNotNone(after_agent_a)
        self.assertEqual(after_agent_a.get('presence_state'), 'online')
        self.assertEqual(after_agent_a.get('presence_label'), 'Online')
        self.assertTrue(after_agent_a.get('last_check_in_at'))

    def test_system_health_reports_peer_and_queue_metrics(self) -> None:
        self.mention_manager.record_mentions(
            user_ids=['agent-a'],
            source_type='channel_message',
            source_id='msg-health-1',
            author_id='human-owner',
            channel_id='general',
            preview='health check',
        )
        self.conn.execute(
            """
            INSERT INTO agent_inbox (id, agent_user_id, source_type, source_id, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            ('INB-health-1', 'agent-a', 'channel_message', 'msg-health-1', 'pending', '2026-02-23 11:05:00.000000'),
        )
        self.conn.commit()

        response = self.client.get(
            '/api/v1/agents/system-health',
            headers=self._headers('key-agent-a'),
        )
        self.assertEqual(response.status_code, 200)
        payload = response.get_json() or {}

        self.assertIn('uptime_seconds', payload)
        self.assertIn('queues', payload)
        self.assertIn('peers', payload)
        self.assertEqual((payload.get('queues') or {}).get('unacked_mentions'), 1)
        self.assertEqual((payload.get('queues') or {}).get('pending_inbox'), 1)
        self.assertEqual((payload.get('queues') or {}).get('pending_p2p_messages'), 7)
        self.assertEqual((payload.get('peers') or {}).get('connected_count'), 1)

    def test_p2p_peers_exposes_connected_peer_version_metadata(self) -> None:
        response = self.client.get(
            '/api/v1/p2p/peers',
            headers=self._headers('key-owner'),
        )
        self.assertEqual(response.status_code, 200)
        payload = response.get_json() or {}

        self.assertEqual(payload.get('connected_peers'), ['peer-alpha'])
        self.assertEqual(payload.get('total_connected'), 1)
        self.assertEqual(payload.get('total_discovered'), 1)

        details = payload.get('connected_peer_details') or []
        self.assertEqual(len(details), 1)
        self.assertEqual(details[0].get('peer_id'), 'peer-alpha')
        self.assertEqual(details[0].get('canopy_version'), '0.4.30')
        self.assertEqual(details[0].get('protocol_version'), 1)
        self.assertTrue(details[0].get('compatible_protocol'))

        versions = payload.get('peer_versions') or {}
        self.assertIn('peer-alpha', versions)
        self.assertEqual((versions.get('peer-alpha') or {}).get('protocol_version'), 1)

    def test_heartbeat_includes_cursor_fields_for_incremental_polling(self) -> None:
        self.conn.execute(
            """
            INSERT INTO mention_events (
                id, user_id, source_type, source_id, author_id, channel_id,
                preview, metadata, created_at, acknowledged_at, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                'MN-older', 'agent-a', 'channel_message', 'msg-older', 'human-owner',
                'general', 'older mention', '{}',
                '2026-02-23 11:00:00.000000', None, 'new'
            ),
        )
        self.conn.execute(
            """
            INSERT INTO mention_events (
                id, user_id, source_type, source_id, author_id, channel_id,
                preview, metadata, created_at, acknowledged_at, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                'MN-latest', 'agent-a', 'channel_message', 'msg-latest', 'human-owner',
                'general', 'latest mention', '{}',
                '2026-02-23 11:10:00.000000', None, 'new'
            ),
        )
        self.conn.execute(
            """
            INSERT INTO agent_inbox (id, agent_user_id, source_type, source_id, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            ('INB-latest', 'agent-a', 'channel_message', 'msg-latest', 'pending', '2026-02-23 11:11:00.000000'),
        )
        self.conn.commit()

        snapshot = build_agent_heartbeat_snapshot(
            db_manager=self.db_manager,
            user_id='agent-a',
            mention_manager=self.mention_manager,
            inbox_manager=None,
        )

        self.assertEqual(snapshot.get('last_mention_id'), 'MN-latest')
        self.assertEqual(snapshot.get('last_inbox_id'), 'INB-latest')
        self.assertIsInstance(snapshot.get('last_mention_seq'), int)
        self.assertIsInstance(snapshot.get('last_inbox_seq'), int)
        self.assertIsInstance(snapshot.get('last_event_seq'), int)
        self.assertGreaterEqual(snapshot.get('last_event_seq'), snapshot.get('last_mention_seq'))


if __name__ == '__main__':
    unittest.main()
