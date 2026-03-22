"""Unit coverage for source_layout normalization and channel persistence."""

import json
import os
import sqlite3
import sys
import types
import unittest
from unittest.mock import MagicMock

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

from canopy.core.channels import ChannelManager, ChannelType
from canopy.core.source_layout import normalize_source_layout


class _FakeDbManager:
    def __init__(self) -> None:
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(
            """
            CREATE TABLE users (
                id TEXT PRIMARY KEY,
                username TEXT,
                public_key TEXT,
                password_hash TEXT,
                origin_peer TEXT
            )
            """
        )
        self.conn.execute(
            """
            INSERT INTO users (id, username, public_key, password_hash, origin_peer)
            VALUES (?, ?, ?, ?, ?)
            """,
            ('owner-user', 'owner', 'pk-owner', 'hash-owner', None),
        )
        self.conn.commit()

    def get_connection(self):
        return self.conn

    def get_instance_owner_user_id(self):
        return 'owner-user'

    def get_user(self, user_id: str):
        row = self.conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return dict(row) if row else None


class TestSourceLayout(unittest.TestCase):
    def setUp(self) -> None:
        self.db = _FakeDbManager()
        self.channel_manager = ChannelManager(self.db, MagicMock())

    def tearDown(self) -> None:
        self.db.conn.close()

    def test_normalize_source_layout_filters_invalid_entries_and_keeps_supported_fields(self) -> None:
        normalized = normalize_source_layout(
            {
                'hero': {'ref': 'attachment:F-module', 'label': 'Hero module'},
                'lede': True,
                'supporting': [
                    {'ref': 'attachment:F-video', 'placement': 'right', 'label': 'Video'},
                    {'ref': 'widget:queue', 'placement': 'strip'},
                    {'ref': 'mailto:nope', 'placement': 'below'},
                    {'ref': 'attachment:F-ignore', 'placement': 'left'},
                ],
                'actions': [
                    {'kind': 'link', 'label': 'Open brief', 'url': '/brief'},
                    {'kind': 'link', 'label': 'External', 'url': 'https://example.com/demo'},
                    {'kind': 'link', 'label': 'Bad', 'url': 'javascript:alert(1)'},
                    {'kind': 'link', 'label': 'Proto-relative', 'url': '//evil.example/phish'},
                ],
                'deck': {'default_ref': 'widget:queue'},
                'ignored': {'x': 1},
            }
        )

        self.assertEqual(
            normalized,
            {
                'version': 1,
                'hero': {'ref': 'attachment:F-module', 'label': 'Hero module'},
                'lede': {'kind': 'rich_text', 'ref': 'content:lede'},
                'supporting': [
                    {'ref': 'attachment:F-video', 'placement': 'right', 'label': 'Video'},
                    {'ref': 'widget:queue', 'placement': 'strip'},
                ],
                'actions': [
                    {'kind': 'link', 'label': 'Open brief', 'url': '/brief'},
                    {'kind': 'link', 'label': 'External', 'url': 'https://example.com/demo'},
                ],
                'deck': {'default_ref': 'widget:queue'},
            },
        )

    def test_channel_message_persists_source_layout_and_preserves_it_on_edit_when_omitted(self) -> None:
        channel = self.channel_manager.create_channel(
            name='source-layout',
            channel_type=ChannelType.PUBLIC,
            created_by='owner-user',
            description='source layout test',
            privacy_mode='open',
        )
        self.assertIsNotNone(channel)
        assert channel is not None

        source_layout = {
            'hero': {'ref': 'attachment:F-module'},
            'lede': True,
            'supporting': [{'ref': 'attachment:F-card', 'placement': 'strip'}],
            'deck': {'default_ref': 'attachment:F-module'},
        }
        message = self.channel_manager.send_message(
            channel_id=channel.id,
            user_id='owner-user',
            content='Structured source',
            attachments=[{'id': 'F-module', 'name': 'module.canopy-module.html', 'type': 'text/html'}],
            source_layout=source_layout,
        )
        self.assertIsNotNone(message)
        assert message is not None
        self.assertEqual(message.source_layout, normalize_source_layout(source_layout))

        row = self.db.conn.execute(
            "SELECT source_layout FROM channel_messages WHERE id = ?",
            (message.id,),
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(
            json.loads(row['source_layout']),
            normalize_source_layout(source_layout),
        )

        edited = self.channel_manager.update_message(
            message_id=message.id,
            user_id='owner-user',
            content='Structured source, revised',
            attachments=None,
            source_layout=None,
        )
        self.assertTrue(edited)

        messages = self.channel_manager.get_channel_messages(
            channel_id=channel.id,
            user_id='owner-user',
            limit=10,
        )
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].source_layout, normalize_source_layout(source_layout))
