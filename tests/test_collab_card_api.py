"""Regression tests for agent collaboration-card API tooling."""

from __future__ import annotations

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
from canopy.core.collab_cards import CollabCardManager, InputCardSpec, TelemetryCardSpec
from canopy.security.api_keys import ApiKeyInfo, Permission


class _FakeDbManager:
    def __init__(self, conn: sqlite3.Connection, db_path: Path) -> None:
        self._conn = conn
        self.db_path = db_path

    def get_connection(self) -> sqlite3.Connection:
        return self._conn

    def get_user(self, user_id: str):
        row = self._conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return dict(row) if row else None

    def get_instance_owner_user_id(self) -> str:
        return 'human-owner'


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
    def get_peer_id(self) -> str:
        return 'peer-local'

    def broadcast_interaction(self, **kwargs):
        return True


class TestCollabCardApi(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.db_file = Path(self.tempdir.name) / 'collab_cards.db'
        self.conn = sqlite3.connect(str(self.db_file))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            """
            CREATE TABLE users (
                id TEXT PRIMARY KEY,
                username TEXT,
                display_name TEXT
            );
            CREATE TABLE feed_posts (
                id TEXT PRIMARY KEY,
                author_id TEXT,
                visibility TEXT
            );
            CREATE TABLE post_permissions (
                post_id TEXT,
                user_id TEXT
            );
            CREATE TABLE channel_messages (
                id TEXT PRIMARY KEY,
                channel_id TEXT
            );
            CREATE TABLE channel_members (
                channel_id TEXT,
                user_id TEXT
            );
            """
        )
        self.conn.executemany(
            "INSERT INTO users (id, username, display_name) VALUES (?, ?, ?)",
            [
                ('human-owner', 'kwalus', 'Konrad'),
                ('agent-a', 'agent_a', 'Agent A'),
                ('agent-b', 'agent_b', 'Agent B'),
            ],
        )
        self.conn.executemany(
            "INSERT INTO channel_messages (id, channel_id) VALUES (?, ?)",
            [
                ('msg-input', 'agent-school'),
                ('msg-telemetry', 'agent-school'),
            ],
        )
        self.conn.executemany(
            "INSERT INTO channel_members (channel_id, user_id) VALUES (?, ?)",
            [
                ('agent-school', 'human-owner'),
                ('agent-school', 'agent-a'),
                ('agent-school', 'agent-b'),
            ],
        )
        self.conn.commit()

        self.db_manager = _FakeDbManager(self.conn, self.db_file)
        self.collab_card_manager = CollabCardManager(self.db_manager)
        self.input_card = self.collab_card_manager.upsert_input_card(
            card_id='input-card-api',
            spec=InputCardSpec(
                title='Choose deployment path',
                prompt='Proceed or hold?',
                kind='choice',
                options=['Proceed', 'Hold'],
                permissions=['agent-a'],
                editors=['agent-b'],
            ),
            created_by='human-owner',
            owner_id='human-owner',
            source_type='channel_message',
            source_id='msg-input',
            channel_id='agent-school',
        )
        self.telemetry_card = self.collab_card_manager.upsert_telemetry_card(
            card_id='telemetry-card-api',
            spec=TelemetryCardSpec(
                title='Build run',
                status='running',
                progress=5,
                stage='queued',
                editors=['agent-b'],
            ),
            created_by='human-owner',
            owner_id='human-owner',
            source_type='channel_message',
            source_id='msg-telemetry',
            channel_id='agent-school',
        )
        self.public_responses_card = self.collab_card_manager.upsert_card(
            card_id='public-responses-card-api',
            card_type='input',
            title='Published operator decision',
            prompt='Read the collected decision.',
            status='open',
            created_by='human-owner',
            owner_id='human-owner',
            source_type='channel_message',
            source_id='msg-input',
            channel_id='agent-school',
            visibility='network',
            permissions=['human-owner'],
            editors=[],
            config={'kind': 'text', 'responses_visible': 'all'},
            actor_id='human-owner',
        )
        self.collab_card_manager.submit_response(
            'public-responses-card-api',
            responder_id='human-owner',
            value='Proceed with the low-risk path.',
            response_type='text',
        )

        self.api_key_manager = _FakeApiKeyManager(
            {
                'key-agent-a': 'agent-a',
                'key-agent-b': 'agent-b',
                'key-owner': 'human-owner',
            }
        )
        components = (
            self.db_manager,
            self.api_key_manager,
            MagicMock(),
            MagicMock(),
            MagicMock(),
            MagicMock(),
            MagicMock(),
            MagicMock(),
            MagicMock(),
            MagicMock(),
            _FakeP2PManager(),
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
        app.config['COLLAB_CARD_MANAGER'] = self.collab_card_manager
        api_bp = create_api_blueprint()
        app.register_blueprint(api_bp, url_prefix='/api/v1')
        self.client = app.test_client()

    def tearDown(self) -> None:
        self.conn.close()

    def _headers(self, key: str) -> dict[str, str]:
        return {'X-API-Key': key, 'Content-Type': 'application/json'}

    def test_agent_can_discover_assigned_input_card_and_read_own_saved_response(self) -> None:
        discover = self.client.get(
            '/api/v1/agents/me/collab-cards?role=respond',
            headers=self._headers('key-agent-a'),
        )
        self.assertEqual(discover.status_code, 200)
        payload = discover.get_json() or {}
        self.assertEqual(payload.get('count'), 1)
        card = payload['cards'][0]
        self.assertEqual(card.get('id'), 'input-card-api')
        self.assertTrue(card.get('needs_response'))
        self.assertIn('respond', card.get('agent_actions') or [])

        response = self.client.post(
            '/api/v1/collab-cards/input-card-api/responses',
            json={'value': 'Proceed', 'response_type': 'choice', 'comment': 'Backup complete.'},
            headers=self._headers('key-agent-a'),
        )
        self.assertEqual(response.status_code, 200)
        response_payload = response.get_json() or {}
        self.assertEqual((response_payload.get('response') or {}).get('value'), 'Proceed')
        self.assertEqual((response_payload.get('card') or {}).get('response_count'), 1)

        collected = self.client.get(
            '/api/v1/collab-cards/input-card-api/responses',
            headers=self._headers('key-agent-a'),
        )
        self.assertEqual(collected.status_code, 200)
        collected_payload = collected.get_json() or {}
        self.assertFalse(collected_payload.get('can_collect'))
        self.assertEqual(collected_payload.get('response_count'), 1)
        self.assertEqual((collected_payload.get('my_response') or {}).get('value'), 'Proceed')
        self.assertEqual(len(collected_payload.get('responses') or []), 1)

    def test_editor_can_collect_all_input_responses_and_update_telemetry(self) -> None:
        response = self.client.post(
            '/api/v1/collab-cards/input-card-api/responses',
            json={'value': 'Hold', 'response_type': 'choice'},
            headers=self._headers('key-agent-a'),
        )
        self.assertEqual(response.status_code, 200)

        discover = self.client.get(
            '/api/v1/agents/me/collab-cards?role=update',
            headers=self._headers('key-agent-b'),
        )
        self.assertEqual(discover.status_code, 200)
        update_cards = discover.get_json().get('cards') or []
        update_ids = {card.get('id') for card in update_cards}
        self.assertIn('input-card-api', update_ids)
        self.assertIn('telemetry-card-api', update_ids)
        telemetry = next(card for card in update_cards if card.get('id') == 'telemetry-card-api')
        self.assertIn('update_telemetry', telemetry.get('agent_actions') or [])

        telemetry_update = self.client.patch(
            '/api/v1/collab-cards/telemetry-card-api/telemetry',
            json={'progress': 64, 'stage': 'tests running'},
            headers=self._headers('key-agent-b'),
        )
        self.assertEqual(telemetry_update.status_code, 200)
        updated_card = telemetry_update.get_json().get('card') or {}
        self.assertEqual((updated_card.get('telemetry') or {}).get('progress'), 64)
        self.assertEqual((updated_card.get('telemetry') or {}).get('stage'), 'tests running')

        collected = self.client.get(
            '/api/v1/collab-cards/input-card-api/responses?scope=all',
            headers=self._headers('key-agent-b'),
        )
        self.assertEqual(collected.status_code, 200)
        collected_payload = collected.get_json() or {}
        self.assertTrue(collected_payload.get('can_collect'))
        self.assertEqual(collected_payload.get('response_count'), 1)
        self.assertEqual((collected_payload.get('responses') or [])[0].get('value'), 'Hold')

    def test_response_visible_all_is_advertised_as_collectable_action(self) -> None:
        discover = self.client.get(
            '/api/v1/agents/me/collab-cards?role=actionable',
            headers=self._headers('key-agent-a'),
        )
        self.assertEqual(discover.status_code, 200)
        cards = discover.get_json().get('cards') or []
        public_card = next(card for card in cards if card.get('id') == 'public-responses-card-api')
        self.assertFalse(public_card.get('can_update'))
        self.assertFalse(public_card.get('can_respond'))
        self.assertIn('collect_responses', public_card.get('agent_actions') or [])

        collected = self.client.get(
            '/api/v1/collab-cards/public-responses-card-api/responses?scope=all',
            headers=self._headers('key-agent-a'),
        )
        self.assertEqual(collected.status_code, 200)
        payload = collected.get_json() or {}
        self.assertTrue(payload.get('can_collect'))
        self.assertEqual((payload.get('responses') or [])[0].get('value'), 'Proceed with the low-risk path.')

