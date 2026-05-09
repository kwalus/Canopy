"""Regression tests for /ajax/channel_sidebar_state delta payloads."""

import os
import sqlite3
import sys
import types
import unittest
from types import SimpleNamespace
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
from canopy.core.events import WorkspaceEventManager


class _FakeDbManager:
    def __init__(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(
            """
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
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                acknowledged_at TIMESTAMP,
                status TEXT DEFAULT 'new'
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE messages (
                id TEXT PRIMARY KEY,
                sender_id TEXT,
                recipient_id TEXT,
                content TEXT,
                message_type TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                metadata TEXT,
                read_at TIMESTAMP
            )
            """
        )
        self.conn.executemany(
            """
            INSERT INTO mention_events
            (id, user_id, source_type, source_id, channel_id, acknowledged_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                ('mention-ops-1', 'owner', 'channel_message', 'msg-ops-1', 'ops', None),
                ('mention-ops-2', 'owner', 'channel_message', 'msg-ops-2', 'ops', None),
                ('mention-general-ack', 'owner', 'channel_message', 'msg-general-1', 'general', '2026-05-08 00:00:00'),
                ('mention-feed', 'owner', 'feed_post', 'post-1', None, None),
                ('mention-other-user', 'other', 'channel_message', 'msg-ops-3', 'ops', None),
            ],
        )
        self.conn.commit()

    def get_connection(self):
        return self.conn

    def get_instance_owner_user_id(self) -> str:
        return 'owner'

    def get_pending_approval_count(self) -> int:
        return 0


class TestChannelSidebarStateDelta(unittest.TestCase):
    def setUp(self) -> None:
        self.db_manager = _FakeDbManager()
        self.channel_manager = MagicMock()
        self.channel_manager.DEFAULT_CHANNEL_LIFECYCLE_DAYS = 180
        self.channel_manager.get_user_channels.return_value = [
            SimpleNamespace(
                id='general',
                name='general',
                description='General discussion',
                channel_type='public',
                privacy_mode='open',
                post_policy='open',
                allow_member_replies=True,
                can_post_top_level=True,
                can_reply=True,
                allowed_poster_count=0,
                origin_peer='',
                user_role='owner',
                member_count=4,
                unread_count=2,
                notifications_enabled=True,
                crypto_mode='',
                lifecycle_status='preserved',
                lifecycle_ttl_days=180,
                lifecycle_preserved=True,
                archived_at=None,
                archive_reason=None,
                days_until_archive=None,
                owner_peer_state='local',
                last_activity_at=None,
            ),
            SimpleNamespace(
                id='ops',
                name='ops',
                description='Operations',
                channel_type='private',
                privacy_mode='guarded',
                post_policy='curated',
                allow_member_replies=True,
                can_post_top_level=False,
                can_reply=True,
                allowed_poster_count=2,
                origin_peer='peer-1',
                user_role='member',
                member_count=3,
                unread_count=0,
                notifications_enabled=False,
                crypto_mode='channel_e2e_v1',
                lifecycle_status='cooling',
                lifecycle_ttl_days=90,
                lifecycle_preserved=False,
                archived_at=None,
                archive_reason=None,
                days_until_archive=7,
                owner_peer_state='known',
                last_activity_at=None,
            ),
        ]
        self.channel_manager.describe_channel_lifecycle.side_effect = lambda channel, **kwargs: {
            'status': getattr(channel, 'lifecycle_status', 'active'),
            'ttl_days': getattr(channel, 'lifecycle_ttl_days', 180),
            'preserved': bool(getattr(channel, 'lifecycle_preserved', False)),
            'archived_at': getattr(channel, 'archived_at', None),
            'archive_reason': getattr(channel, 'archive_reason', None),
            'days_until_archive': getattr(channel, 'days_until_archive', None),
            'owner_peer_state': getattr(channel, 'owner_peer_state', 'unknown'),
            'last_activity_at': getattr(channel, 'last_activity_at', None),
        }

        components = (
            self.db_manager,
            MagicMock(),
            MagicMock(),
            MagicMock(),
            self.channel_manager,
            MagicMock(),
            MagicMock(),
            MagicMock(),
            MagicMock(),
            MagicMock(),
            MagicMock(),
        )
        components[-1].get_peer_id.return_value = 'peer-local'

        self.get_components_patcher = patch('canopy.ui.routes.get_app_components', return_value=components)
        self.get_components_any_patcher = patch('canopy.ui.routes._get_app_components_any', return_value=components)
        self.get_components_patcher.start()
        self.get_components_any_patcher.start()
        self.addCleanup(self.get_components_patcher.stop)
        self.addCleanup(self.get_components_any_patcher.stop)

        app = Flask(__name__)
        app.config['TESTING'] = True
        app.secret_key = 'channel-sidebar-secret'
        app.config['WORKSPACE_EVENT_MANAGER'] = MagicMock(spec=WorkspaceEventManager)
        app.config['WORKSPACE_EVENT_MANAGER'].get_latest_seq.return_value = 42
        app.register_blueprint(create_ui_blueprint())

        self.client = app.test_client()
        with self.client.session_transaction() as sess:
            sess['authenticated'] = True
            sess['user_id'] = 'owner'

    def test_channel_sidebar_state_returns_revision(self) -> None:
        response = self.client.get('/ajax/channel_sidebar_state')
        self.assertEqual(response.status_code, 200)
        payload = response.get_json() or {}

        self.assertTrue(payload.get('success'))
        self.assertTrue(payload.get('changed'))
        self.assertEqual(payload.get('count'), 2)
        self.assertTrue(payload.get('rev'))
        self.assertEqual(payload.get('workspace_event_cursor'), 42)
        channels = payload.get('channels') or []
        self.assertEqual(channels[0].get('id'), 'general')
        self.assertEqual(channels[1].get('crypto_mode'), 'channel_e2e_v1')
        self.assertEqual(channels[0].get('mention_count'), 0)
        self.assertEqual(channels[1].get('mention_count'), 2)
        self.assertEqual(channels[0].get('lifecycle_status'), 'preserved')
        self.assertTrue(channels[0].get('lifecycle_preserved'))
        self.assertEqual(channels[1].get('lifecycle_status'), 'cooling')
        self.assertEqual(channels[1].get('days_until_archive'), 7)
        self.assertEqual(channels[0].get('post_policy'), 'open')
        self.assertEqual(channels[1].get('post_policy'), 'curated')
        self.assertFalse(channels[1].get('can_post_top_level'))
        self.assertTrue(channels[1].get('can_reply'))
        self.assertEqual(channels[1].get('allowed_poster_count'), 2)

    def test_channel_sidebar_state_returns_empty_channels_when_revision_matches(self) -> None:
        first = self.client.get('/ajax/channel_sidebar_state')
        self.assertEqual(first.status_code, 200)
        first_payload = first.get_json() or {}

        response = self.client.get(f"/ajax/channel_sidebar_state?rev={first_payload.get('rev')}")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json() or {}

        self.assertTrue(payload.get('success'))
        self.assertFalse(payload.get('changed'))
        self.assertEqual(payload.get('count'), 2)
        self.assertEqual(payload.get('channels'), [])
        self.assertEqual(payload.get('rev'), first_payload.get('rev'))
        self.assertEqual(payload.get('workspace_event_cursor'), 42)

    def tearDown(self) -> None:
        self.db_manager.conn.close()

    def test_channels_page_seeds_cursor_without_advancing_past_sidebar_snapshot(self) -> None:
        workspace_events = self.client.application.config['WORKSPACE_EVENT_MANAGER']
        workspace_events.get_latest_seq.return_value = 42
        original_get_user_channels = self.channel_manager.get_user_channels
        call_counter = {'count': 0}

        def _race_get_user_channels(user_id):
            call_counter['count'] += 1
            if call_counter['count'] >= 2:
                workspace_events.get_latest_seq.return_value = 99
            return original_get_user_channels(user_id)

        with patch.object(self.channel_manager, 'get_user_channels', side_effect=_race_get_user_channels):
            response = self.client.get('/channels')

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn('let channelSidebarEventCursor = Number(42) || 0;', body)
        self.assertIn('let channelThreadEventCursor = Number(42) || 0;', body)
        self.assertIn("const CHANNEL_THREAD_EVENT_TYPES = [", body)
        self.assertIn("function pollChannelThreadEvents() {", body)
        self.assertIn("function refreshChannelMessagesSnapshot(options = {}) {", body)
        self.assertIn("function requestChannelThreadRefresh(options = {}) {", body)
        self.assertIn("if (isSearchActive) {", body)
        self.assertIn("refreshChannelMessagesSnapshot({ forceScroll });", body)
        self.assertIn("function setSidebarChannelMemberCount(channelId, memberCount) {", body)
        self.assertIn("function applySidebarChannelStateUpdate(channelId, payload) {", body)
        self.assertIn("reason === 'notifications_updated'", body)
        self.assertIn("reason === 'lifecycle_updated'", body)
        self.assertIn("reason === 'privacy_updated'", body)
        self.assertIn("reason === 'channel_deleted'", body)
        self.assertIn("startChannelThreadEventPolling();", body)
        self.assertNotIn("Number(channelSidebarEventCursor || 0),\n    );", body)
        self.assertNotIn("Number(channelSidebarEventCursor || 0),\n            );", body)
        self.assertNotIn("channelThreadEventCursor = Number(data.workspace_event_cursor || 0);", body)


if __name__ == '__main__':
    unittest.main()
