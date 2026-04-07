"""Regression tests for channel removal voting UI routes."""

import os
import sqlite3
import sys
import tempfile
import types
import unittest
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

from canopy.ui.routes import create_ui_blueprint


class _FakeDbManager:
    def __init__(self, conn: sqlite3.Connection, db_path: Path) -> None:
        self._conn = conn
        self.db_path = db_path

    def get_connection(self) -> sqlite3.Connection:
        return self._conn

    def get_instance_owner_user_id(self):
        return 'owner-user'


class TestChannelRemovalVoteRoutes(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)

        db_path = Path(self.tempdir.name) / 'channel-removal-routes.db'
        self.conn = sqlite3.connect(str(db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            """
            CREATE TABLE channel_members (
                channel_id TEXT,
                user_id TEXT,
                role TEXT
            );
            """
        )
        self.conn.executemany(
            "INSERT INTO channel_members (channel_id, user_id, role) VALUES (?, ?, ?)",
            [
                ('chan-1', 'owner-user', 'admin'),
                ('chan-1', 'member-user', 'member'),
            ],
        )
        self.conn.commit()

        self.db_manager = _FakeDbManager(self.conn, db_path)
        self.channel_manager = MagicMock()
        self.channel_manager.get_channel_removal_status.return_value = {
            'channel_exists': True,
            'active_proposal': None,
        }
        self.trust_manager = MagicMock()
        self.trust_manager.get_trusted_peers.return_value = ['peer-a']
        self.p2p_manager = MagicMock()
        self.p2p_manager.get_peer_id.return_value = 'peer-local'
        self.p2p_manager.get_connected_peers.return_value = ['peer-a']
        self.p2p_manager.is_running.return_value = True

        components = (
            self.db_manager,
            MagicMock(),
            self.trust_manager,
            MagicMock(),
            self.channel_manager,
            MagicMock(),
            MagicMock(),
            MagicMock(),
            MagicMock(),
            MagicMock(),
            self.p2p_manager,
        )

        self.get_components_any_patcher = patch(
            'canopy.ui.routes._get_app_components_any',
            return_value=components,
        )
        self.get_components_patcher = patch(
            'canopy.ui.routes.get_app_components',
            return_value=components,
        )
        self.get_components_any_patcher.start()
        self.get_components_patcher.start()
        self.addCleanup(self.get_components_any_patcher.stop)
        self.addCleanup(self.get_components_patcher.stop)

        app = Flask(__name__)
        app.config['TESTING'] = True
        app.secret_key = 'channel-removal-route-secret'
        app.register_blueprint(create_ui_blueprint())
        self.client = app.test_client()
        with self.client.session_transaction() as sess:
            sess['authenticated'] = True
            sess['user_id'] = 'owner-user'
            sess['_csrf_token'] = 'csrf-channel-removal'

    def tearDown(self) -> None:
        self.conn.close()

    def test_status_route_hides_channel_from_non_member(self) -> None:
        with self.client.session_transaction() as sess:
            sess['user_id'] = 'outsider-user'

        response = self.client.get('/ajax/channel_removal_status/chan-1')

        self.assertEqual(response.status_code, 404)
        self.channel_manager.get_channel_removal_status.assert_not_called()

    def test_vote_route_starts_vote_and_broadcasts_proposal_and_ballot(self) -> None:
        pre_status = {'channel_exists': True, 'active_proposal': None}
        post_status = {
            'channel_exists': True,
            'active_proposal': {
                'proposal_id': 'CRP1',
                'channel_name': 'chan-1',
                'channel_origin_peer': 'peer-origin',
                'channel_privacy_mode': 'open',
                'threshold_count': 2,
                'opened_at': '2026-03-28 10:00:00',
            },
        }
        self.channel_manager.get_channel_removal_status.return_value = pre_status
        self.channel_manager.start_channel_removal_vote.return_value = {
            'ok': True,
            'proposal_id': 'CRP1',
            'status': post_status,
            'electorate_peer_ids': ['peer-local', 'peer-a'],
            'finalization': None,
        }

        response = self.client.post(
            '/ajax/channel_removal_vote',
            json={'channel_id': 'chan-1', 'vote': 'remove'},
            headers={'X-CSRFToken': 'csrf-channel-removal'},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json() or {}
        self.assertTrue(payload.get('success'))
        self.channel_manager.start_channel_removal_vote.assert_called_once()
        self.p2p_manager.broadcast_channel_removal_proposal.assert_called_once()
        self.p2p_manager.broadcast_channel_removal_vote.assert_called_once()
        self.p2p_manager.broadcast_channel_removal_result.assert_not_called()

    def test_vote_route_casts_vote_and_broadcasts_result_when_finalized(self) -> None:
        pre_status = {
            'channel_exists': True,
            'active_proposal': {
                'proposal_id': 'CRP1',
                'electorate': [
                    {'peer_id': 'peer-local'},
                    {'peer_id': 'peer-a'},
                ],
            },
        }
        post_status = {
            'channel_exists': True,
            'active_proposal': None,
            'retired': True,
        }
        self.channel_manager.get_channel_removal_status.return_value = pre_status
        self.channel_manager.cast_channel_removal_vote.return_value = {
            'ok': True,
            'status': post_status,
            'finalization': {
                'proposal_id': 'CRP1',
                'result': 'retired',
                'electorate_peer_ids': ['peer-local', 'peer-a'],
                'threshold_count': 2,
                'tombstone_id': 'CRT1',
                'finalized_at': '2026-03-28 10:05:00',
            },
        }

        response = self.client.post(
            '/ajax/channel_removal_vote',
            json={'channel_id': 'chan-1', 'vote': 'remove', 'proposal_id': 'CRP1'},
            headers={'X-CSRFToken': 'csrf-channel-removal'},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json() or {}
        self.assertTrue(payload.get('success'))
        self.channel_manager.cast_channel_removal_vote.assert_called_once()
        self.p2p_manager.broadcast_channel_removal_proposal.assert_not_called()
        self.p2p_manager.broadcast_channel_removal_vote.assert_called_once()
        self.p2p_manager.broadcast_channel_removal_result.assert_called_once()

    def test_vote_route_skips_mesh_broadcast_when_vote_is_unchanged(self) -> None:
        pre_status = {
            'channel_exists': True,
            'active_proposal': {
                'proposal_id': 'CRP1',
                'electorate': [
                    {'peer_id': 'peer-local'},
                    {'peer_id': 'peer-a'},
                ],
                'local_vote': 'remove',
            },
        }
        post_status = {
            'channel_exists': True,
            'active_proposal': {
                'proposal_id': 'CRP1',
                'electorate': [
                    {'peer_id': 'peer-local', 'vote': 'remove'},
                    {'peer_id': 'peer-a', 'vote': None},
                ],
                'local_vote': 'remove',
            },
        }
        self.channel_manager.get_channel_removal_status.return_value = pre_status
        self.channel_manager.cast_channel_removal_vote.return_value = {
            'ok': True,
            'status': post_status,
            'finalization': None,
            'changed': False,
        }

        response = self.client.post(
            '/ajax/channel_removal_vote',
            json={'channel_id': 'chan-1', 'vote': 'remove', 'proposal_id': 'CRP1'},
            headers={'X-CSRFToken': 'csrf-channel-removal'},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json() or {}
        self.assertTrue(payload.get('success'))
        self.assertFalse(payload.get('changed'))
        self.p2p_manager.broadcast_channel_removal_proposal.assert_not_called()
        self.p2p_manager.broadcast_channel_removal_vote.assert_not_called()
        self.p2p_manager.broadcast_channel_removal_result.assert_not_called()
