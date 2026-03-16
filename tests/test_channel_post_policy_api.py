"""API regression tests for curated channel posting policy."""

import os
import sys
import types
import unittest
from datetime import datetime, timezone
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

from canopy.api.routes import create_api_blueprint
from canopy.security.api_keys import ApiKeyInfo, Permission


class _FakeDbManager:
    def __init__(self) -> None:
        self.row = {
            'name': 'ops',
            'channel_type': 'public',
            'description': 'Operations',
            'created_by': 'owner-user',
            'privacy_mode': 'open',
            'post_policy': 'curated',
            'allow_member_replies': 1,
            'last_activity_at': '2026-03-16T01:02:03+00:00',
            'lifecycle_ttl_days': 180,
            'lifecycle_preserved': 0,
            'lifecycle_archived_at': None,
            'lifecycle_archive_reason': None,
        }

    def get_connection(self):
        conn = MagicMock()
        conn.__enter__.return_value = conn
        conn.__exit__.return_value = False
        conn.execute.return_value.fetchone.return_value = self.row
        return conn

    def get_instance_owner_user_id(self):
        return 'owner-user'

    def get_user(self, user_id: str):
        return {'id': user_id, 'origin_peer': ''}


class _FakeApiKeyManager:
    def validate_key(self, raw_key: str, required_permission=None):
        if raw_key != 'good-key':
            return None
        perms = {Permission.WRITE_FEED, Permission.READ_FEED}
        if required_permission and required_permission not in perms:
            return None
        return ApiKeyInfo(
            id='key-owner',
            user_id='owner-user',
            key_hash='hash',
            permissions=perms,
            created_at=datetime.now(timezone.utc),
        )


class TestChannelPostPolicyApi(unittest.TestCase):
    def setUp(self) -> None:
        self.db_manager = _FakeDbManager()
        self.api_key_manager = _FakeApiKeyManager()
        self.channel_manager = MagicMock()
        self.channel_manager.DEFAULT_CHANNEL_LIFECYCLE_DAYS = 180
        self.channel_manager.POST_POLICY_OPEN = 'open'
        self.channel_manager.get_user_channel_governance.return_value = {}
        self.channel_manager.get_channel_allowed_poster_ids.return_value = ['member-user']
        self.channel_manager.get_channel_members_list.return_value = [
            {'user_id': 'owner-user', 'role': 'admin'},
            {'user_id': 'member-user', 'role': 'member'},
        ]
        self.channel_manager.get_member_peer_ids.return_value = []
        self.channel_manager.get_channel_posting_state.return_value = {
            'post_policy': 'curated',
            'allow_member_replies': True,
            'is_admin_like': True,
            'can_post_top_level': True,
            'can_reply': True,
            'allowed_poster_count': 1,
        }
        self.channel_manager.update_channel_post_policy.return_value = {
            'post_policy': 'curated',
            'allow_member_replies': True,
            'is_admin_like': True,
            'can_post_top_level': True,
            'can_reply': True,
            'allowed_poster_count': 1,
        }
        self.channel_manager.grant_channel_post_permission.return_value = {
            'post_policy': 'curated',
            'allow_member_replies': True,
            'is_admin_like': True,
            'can_post_top_level': True,
            'can_reply': True,
            'allowed_poster_count': 1,
        }
        self.channel_manager.revoke_channel_post_permission.return_value = {
            'post_policy': 'curated',
            'allow_member_replies': True,
            'is_admin_like': True,
            'can_post_top_level': True,
            'can_reply': True,
            'allowed_poster_count': 0,
        }
        self.channel_manager.get_channel_access_decision.return_value = {'allowed': True}

        channel = SimpleNamespace(
            id='C123',
            name='ops',
            description='Operations',
            privacy_mode='open',
            post_policy='curated',
            allow_member_replies=True,
            created_by='owner-user',
            last_activity_at=datetime.now(timezone.utc),
            lifecycle_ttl_days=180,
            lifecycle_preserved=False,
            archived_at=None,
            archive_reason=None,
            to_dict=lambda: {
                'id': 'C123',
                'name': 'ops',
                'post_policy': 'curated',
                'allow_member_replies': True,
            },
        )
        self.channel_manager.create_channel.return_value = channel

        self.p2p_manager = MagicMock()
        self.p2p_manager.is_running.return_value = True
        self.p2p_manager.get_peer_id.return_value = 'peer-local'

        components = (
            self.db_manager,
            self.api_key_manager,
            MagicMock(),
            MagicMock(),
            self.channel_manager,
            MagicMock(),
            MagicMock(),
            MagicMock(),
            MagicMock(),
            MagicMock(),
            self.p2p_manager,
        )

        self.get_components_patcher = patch('canopy.api.routes.get_app_components', return_value=components)
        self.get_components_any_patcher = patch('canopy.api.routes._get_app_components_any', return_value=components)
        self.get_components_patcher.start()
        self.get_components_any_patcher.start()
        self.addCleanup(self.get_components_patcher.stop)
        self.addCleanup(self.get_components_any_patcher.stop)

        app = Flask(__name__)
        app.config['TESTING'] = True
        app.secret_key = 'channel-post-policy-api'
        app.register_blueprint(create_api_blueprint(), url_prefix='/api/v1')
        self.client = app.test_client()

    def test_create_channel_accepts_curated_post_policy_and_broadcasts_metadata(self) -> None:
        response = self.client.post(
            '/api/v1/channels',
            json={
                'name': 'ops',
                'type': 'public',
                'privacy_mode': 'open',
                'post_policy': 'curated',
                'allow_member_replies': True,
            },
            headers={'X-API-Key': 'good-key'},
        )

        self.assertEqual(response.status_code, 201)
        self.channel_manager.create_channel.assert_called_once()
        kwargs = self.channel_manager.create_channel.call_args.kwargs
        self.assertEqual(kwargs['post_policy'], 'curated')
        self.assertTrue(kwargs['allow_member_replies'])

        self.p2p_manager.broadcast_channel_announce.assert_called_once()
        announce = self.p2p_manager.broadcast_channel_announce.call_args.kwargs
        self.assertEqual(announce['post_policy'], 'curated')
        self.assertTrue(announce['allow_member_replies'])
        self.assertEqual(announce['allowed_poster_user_ids'], ['member-user'])

    def test_get_channel_members_includes_posting_policy(self) -> None:
        response = self.client.get(
            '/api/v1/channels/C123/members',
            headers={'X-API-Key': 'good-key'},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json() or {}
        self.assertEqual(payload['policy']['post_policy'], 'curated')
        self.assertEqual(payload['policy']['allowed_poster_count'], 1)
        self.assertTrue(payload['policy']['can_manage'])

    def test_update_channel_post_policy_endpoint_broadcasts_curated_metadata(self) -> None:
        response = self.client.patch(
            '/api/v1/channels/C123/post-policy',
            json={'post_policy': 'curated', 'allow_member_replies': True},
            headers={'X-API-Key': 'good-key'},
        )

        self.assertEqual(response.status_code, 200)
        self.channel_manager.update_channel_post_policy.assert_called_once()
        self.p2p_manager.broadcast_channel_announce.assert_called_once()
        announce = self.p2p_manager.broadcast_channel_announce.call_args.kwargs
        self.assertEqual(announce['post_policy'], 'curated')
        self.assertEqual(announce['allowed_poster_user_ids'], ['member-user'])


if __name__ == '__main__':
    unittest.main()
