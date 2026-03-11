"""Regression tests for structured tool feedback in the UI composer routes."""

import json
import os
import sqlite3
import sys
import tempfile
import types
import unittest
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
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

from canopy.ui.routes import create_ui_blueprint


@dataclass
class _FakeHandoff:
    id: str
    title: str
    summary: str
    next_steps: List[str] = field(default_factory=list)
    owner: Optional[str] = None
    tags: List[str] = field(default_factory=list)
    raw: str = ''
    visibility: str = 'local'
    origin_peer: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    required_capabilities: List[str] = field(default_factory=list)
    escalation_level: Optional[str] = None
    return_to: Optional[str] = None
    context_payload: Optional[Dict[str, Any]] = None
    source_type: Optional[str] = None
    source_id: Optional[str] = None
    channel_id: Optional[str] = None
    author_id: Optional[str] = None


class _FakeHandoffManager:
    def __init__(self) -> None:
        self.items: dict[str, _FakeHandoff] = {}

    def upsert_handoff(self, **kwargs):
        item = _FakeHandoff(
            id=kwargs['handoff_id'],
            title=kwargs['title'],
            summary=kwargs.get('summary') or '',
            next_steps=list(kwargs.get('next_steps') or []),
            owner=kwargs.get('owner'),
            tags=list(kwargs.get('tags') or []),
            raw=kwargs.get('raw') or '',
            visibility=kwargs.get('visibility') or 'local',
            origin_peer=kwargs.get('origin_peer'),
            created_at=kwargs.get('created_at'),
            updated_at=kwargs.get('updated_at'),
            required_capabilities=list(kwargs.get('required_capabilities') or []),
            escalation_level=kwargs.get('escalation_level'),
            return_to=kwargs.get('return_to'),
            context_payload=kwargs.get('context_payload'),
            source_type=kwargs.get('source_type'),
            source_id=kwargs.get('source_id'),
            channel_id=kwargs.get('channel_id'),
            author_id=kwargs.get('author_id'),
        )
        self.items[item.id] = item
        return item

    def get_handoff(self, handoff_id: str):
        return self.items.get(handoff_id)


class _FakeDbManager:
    def __init__(self, conn: sqlite3.Connection, db_path: Path) -> None:
        self._conn = conn
        self.db_path = db_path

    def get_connection(self) -> sqlite3.Connection:
        return self._conn

    def get_user(self, user_id: str):
        row = self._conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return dict(row) if row else None

    def get_instance_owner_user_id(self):
        return 'owner'


@dataclass
class _FakePost:
    id: str
    content: str
    post_type: object
    visibility: object
    created_at: datetime
    expires_at: None = None
    metadata: Optional[Dict[str, Any]] = None

    def to_dict(self):
        return {
            'id': self.id,
            'content': self.content,
            'post_type': getattr(self.post_type, 'value', self.post_type),
            'visibility': getattr(self.visibility, 'value', self.visibility),
            'created_at': self.created_at.isoformat(),
            'expires_at': None,
            'metadata': self.metadata or {},
        }


class _FakeFeedManager:
    def __init__(self) -> None:
        self.last_post = None

    def create_post(self, *, author_id, content, post_type, visibility, metadata, permissions, source_type, tags, expires_at, ttl_seconds, ttl_mode):
        post = _FakePost(
            id='POST-1',
            content=content,
            post_type=post_type,
            visibility=visibility,
            created_at=datetime.now(timezone.utc),
            metadata=metadata or {},
        )
        self.last_post = post
        return post


@dataclass
class _FakeMessage:
    id: str
    content: str
    channel_id: str
    author_id: str
    created_at: datetime
    parent_message_id: Optional[str] = None
    attachments: list = field(default_factory=list)
    expires_at: None = None

    def to_dict(self):
        return {
            'id': self.id,
            'content': self.content,
            'channel_id': self.channel_id,
            'author_id': self.author_id,
            'created_at': self.created_at.isoformat(),
            'parent_message_id': self.parent_message_id,
            'attachments': self.attachments,
        }


class _FakeChannelManager:
    def __init__(self) -> None:
        self.last_message = None

    def get_channel_access_decision(self, **kwargs):
        return {'allowed': True}

    def send_message(self, channel_id, user_id, content, message_type=None, **kwargs):
        message = _FakeMessage(
            id='MSG-1',
            content=content,
            channel_id=channel_id,
            author_id=user_id,
            created_at=datetime.now(timezone.utc),
            parent_message_id=kwargs.get('parent_message_id'),
            attachments=list(kwargs.get('attachments') or []),
        )
        self.last_message = message
        return message


class TestUiStructuredToolFeedback(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)

        db_path = Path(self.tempdir.name) / 'structured-ui.db'
        self.conn = sqlite3.connect(str(db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            """
            CREATE TABLE users (
                id TEXT PRIMARY KEY,
                username TEXT,
                display_name TEXT,
                avatar_file_id TEXT,
                account_type TEXT,
                origin_peer TEXT,
                created_at TEXT
            );
            CREATE TABLE channels (
                id TEXT PRIMARY KEY,
                name TEXT,
                privacy_mode TEXT
            );
            """
        )
        self.conn.execute(
            "INSERT INTO users (id, username, display_name, account_type, origin_peer, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            ('owner', 'owner', 'Owner', 'human', None, '2026-03-10T00:00:00+00:00'),
        )
        self.conn.execute(
            "INSERT INTO channels (id, name, privacy_mode) VALUES (?, ?, ?)",
            ('CHAN-1', 'architecture', 'open'),
        )
        self.conn.commit()

        self.db_manager = _FakeDbManager(self.conn, db_path)
        self.feed_manager = _FakeFeedManager()
        self.channel_manager = _FakeChannelManager()
        self.handoff_manager = _FakeHandoffManager()

        components = (
            self.db_manager,
            MagicMock(),
            MagicMock(),
            MagicMock(),
            self.channel_manager,
            MagicMock(),
            self.feed_manager,
            MagicMock(),
            MagicMock(),
            MagicMock(),
            None,
        )

        self.get_components_patcher = patch('canopy.ui.routes.get_app_components', return_value=components)
        self.get_components_any_patcher = patch('canopy.ui.routes._get_app_components_any', return_value=components)
        self.csrf_patcher = patch('canopy.ui.routes.validate_csrf_request', return_value=None)
        self.get_components_patcher.start()
        self.get_components_any_patcher.start()
        self.csrf_patcher.start()
        self.addCleanup(self.get_components_patcher.stop)
        self.addCleanup(self.get_components_any_patcher.stop)
        self.addCleanup(self.csrf_patcher.stop)

        app = Flask(__name__)
        app.config['TESTING'] = True
        app.secret_key = 'structured-ui'
        app.config['HANDOFF_MANAGER'] = self.handoff_manager
        app.register_blueprint(create_ui_blueprint())

        self.client = app.test_client()
        with self.client.session_transaction() as sess:
            sess['authenticated'] = True
            sess['user_id'] = 'owner'
            sess['username'] = 'owner'
            sess['display_name'] = 'Owner'
            sess['_csrf_token'] = 'csrf-structured-ui'

    def tearDown(self) -> None:
        self.conn.close()

    def test_create_post_returns_structured_object_feedback(self) -> None:
        response = self.client.post(
            '/ajax/create_post',
            json={
                'content': '[handoff]\ntitle: Feed coordination\nsummary: Hand this to the team.\n[/handoff]',
                'post_type': 'text',
                'visibility': 'network',
                'attachments': [],
                'source_type': 'human',
            },
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json() or {}
        self.assertTrue(payload.get('success'))
        structured = payload.get('structured_objects') or []
        self.assertEqual(len(structured), 1)
        self.assertEqual(structured[0].get('type'), 'handoff')
        self.assertEqual(structured[0].get('title'), 'Feed coordination')

    def test_send_channel_message_returns_structured_object_feedback(self) -> None:
        response = self.client.post(
            '/ajax/send_channel_message',
            json={
                'channel_id': 'CHAN-1',
                'content': '[handoff]\ntitle: Channel coordination\nsummary: Hand this to the next owner.\n[/handoff]',
                'attachments': [],
            },
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json() or {}
        self.assertTrue(payload.get('success'))
        structured = payload.get('structured_objects') or []
        self.assertEqual(len(structured), 1)
        self.assertEqual(structured[0].get('type'), 'handoff')
        self.assertEqual(structured[0].get('title'), 'Channel coordination')
        self.assertIsNotNone(self.handoff_manager.get_handoff(structured[0].get('id')))

    def test_create_post_rejects_semantically_incomplete_signal_block(self) -> None:
        response = self.client.post(
            '/ajax/create_post',
            json={
                'content': '[signal]\ntype: coordination_tiers\nTier_A: one\nTier_B: two\nTier_C: three\n[/signal]',
                'post_type': 'text',
                'visibility': 'network',
                'attachments': [],
                'source_type': 'human',
            },
        )

        self.assertEqual(response.status_code, 400)
        payload = response.get_json() or {}
        self.assertIn('structured_validation', payload)
        self.assertIn('signal', json.dumps(payload.get('structured_validation') or {}))
        self.assertIsNone(self.feed_manager.last_post)

    def test_send_channel_message_rejects_semantically_incomplete_request_block(self) -> None:
        response = self.client.post(
            '/ajax/send_channel_message',
            json={
                'channel_id': 'CHAN-1',
                'content': '[request]\nkind: coordination_compliance\nowner: @agent_one\nowner: @agent_two\nrequired_fields: runtime_change\nformat: canonical signal\neta_minutes: 10\n[/request]',
                'attachments': [],
            },
        )

        self.assertEqual(response.status_code, 400)
        payload = response.get_json() or {}
        self.assertIn('structured_validation', payload)
        self.assertIn('request', json.dumps(payload.get('structured_validation') or {}))
        self.assertIsNone(self.channel_manager.last_message)


if __name__ == '__main__':
    unittest.main()
