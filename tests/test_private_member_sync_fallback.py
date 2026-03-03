"""
Tests for UI _trigger_member_sync bounded fanout logic.

Validates:
- Candidate list ordering: target peer → member peers → connected peers
- max_attempts = 3 cap (no more than 3 broadcast_member_sync calls)
- Stop-on-success: iteration halts after the first successful send
- Edge cases: target_peer is None, target_peer equals local_peer, empty member list
- Channel announce fires after member add
"""
import pytest
from unittest.mock import MagicMock, patch
from flask import Flask

from canopy.ui.routes import create_ui_blueprint


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_channel_row(privacy_mode='private'):
    row = {
        'privacy_mode': privacy_mode,
        'name': 'test-channel',
        'channel_type': 'private',
        'description': 'desc',
    }
    m = MagicMock()
    m.__getitem__ = lambda self, k: row[k]
    m.get = lambda k, d=None: row.get(k, d)
    return m


def _make_app(p2p_mgr, db_mgr, channel_mgr):
    """Minimal Flask app with blueprint and mocked components."""
    app = Flask(__name__)
    app.config['TESTING'] = True
    app.config['SECRET_KEY'] = 'test'
    app.config['P2P_MANAGER'] = p2p_mgr
    app.config['DB_MANAGER'] = db_mgr
    app.config['CHANNEL_MANAGER'] = channel_mgr
    for key in ('API_KEY_MANAGER', 'TRUST_MANAGER', 'MESSAGE_MANAGER',
                'FILE_MANAGER', 'FEED_MANAGER', 'INTERACTION_MANAGER',
                'PROFILE_MANAGER', 'CANOPY_CONFIG'):
        app.config[key] = None
    bp = create_ui_blueprint()
    app.register_blueprint(bp)
    return app


def _make_p2p(local_peer='local-peer', connected=None):
    p2p = MagicMock()
    p2p.is_running.return_value = True
    p2p.get_peer_id.return_value = local_peer
    p2p.get_connected_peers.return_value = connected or []
    p2p.broadcast_member_sync.return_value = True  # succeeds by default
    p2p.broadcast_channel_announce.return_value = True
    return p2p


def _make_db(origin_peer=None, channel_row=None):
    db = MagicMock()
    user = {'origin_peer': origin_peer} if origin_peer else {}
    db.get_user.return_value = user
    conn_ctx = MagicMock()
    conn_ctx.__enter__ = MagicMock(return_value=conn_ctx)
    conn_ctx.__exit__ = MagicMock(return_value=False)
    row = channel_row or _make_channel_row()
    conn_ctx.execute.return_value.fetchone.return_value = row
    db.get_connection.return_value = conn_ctx
    return db


def _make_ch_mgr(member_peers=None):
    ch = MagicMock()
    ch.get_member_peer_ids.return_value = set(member_peers or [])
    ch.add_member.return_value = True
    ch.remove_member.return_value = True
    return ch


def _post_add(client, channel_id='ch1', user_id='user1', role='member'):
    """POST to add a member with auth session headers."""
    with client.session_transaction() as sess:
        sess['authenticated'] = True
        sess['user_id'] = 'admin'
        sess['_csrf_token'] = 'tok'
    return client.post(
        f'/ajax/channel_members/{channel_id}',
        json={'user_id': user_id, 'role': role},
        headers={'X-CSRFToken': 'tok'},
    )


def _delete_remove(client, channel_id='ch1', member_id='user1'):
    """DELETE to remove a member with auth session headers."""
    with client.session_transaction() as sess:
        sess['authenticated'] = True
        sess['user_id'] = 'admin'
        sess['_csrf_token'] = 'tok'
    return client.delete(
        f'/ajax/channel_members/{channel_id}/{member_id}',
        headers={'X-CSRFToken': 'tok'},
    )


# ---------------------------------------------------------------------------
# Tests: candidate list and bounded fanout
# ---------------------------------------------------------------------------

class TestTriggerMemberSyncCandidateList:
    def test_remote_user_target_peer_first(self):
        """Target peer must appear first in the candidate list."""
        p2p = _make_p2p(local_peer='local', connected=['conn1'])
        db = _make_db(origin_peer='remote-peer')
        ch = _make_ch_mgr(member_peers=['local', 'remote-peer', 'member2'])
        app = _make_app(p2p, db, ch)

        with app.test_client() as client:
            _post_add(client)

        first_call = p2p.broadcast_member_sync.call_args_list[0]
        assert first_call.kwargs.get('target_peer_id') == 'remote-peer'

    def test_max_three_attempts(self):
        """Never more than 3 broadcast_member_sync calls regardless of candidate count."""
        p2p = _make_p2p(local_peer='local',
                        connected=['c1', 'c2', 'c3', 'c4', 'c5'])
        p2p.broadcast_member_sync.return_value = False  # all fail → keep trying
        db = _make_db(origin_peer=None)  # local user
        ch = _make_ch_mgr(member_peers=['local', 'm1', 'm2', 'm3'])
        app = _make_app(p2p, db, ch)

        with app.test_client() as client:
            _post_add(client)

        assert p2p.broadcast_member_sync.call_count <= 3

    def test_stop_on_first_success(self):
        """Iteration stops as soon as one send succeeds."""
        p2p = _make_p2p(local_peer='local', connected=['c1', 'c2'])
        p2p.broadcast_member_sync.return_value = True  # first call succeeds
        db = _make_db(origin_peer='remote-peer')
        ch = _make_ch_mgr(member_peers=['local', 'remote-peer', 'm2'])
        app = _make_app(p2p, db, ch)

        with app.test_client() as client:
            _post_add(client)

        assert p2p.broadcast_member_sync.call_count == 1

    def test_deduplication(self):
        """Same peer must not appear twice in candidates."""
        target = 'remote-peer'
        p2p = _make_p2p(local_peer='local', connected=[target, 'c2'])
        p2p.broadcast_member_sync.return_value = False
        db = _make_db(origin_peer=target)
        ch = _make_ch_mgr(member_peers=['local', target])
        app = _make_app(p2p, db, ch)

        with app.test_client() as client:
            _post_add(client)

        called_peers = [c.kwargs['target_peer_id']
                        for c in p2p.broadcast_member_sync.call_args_list]
        assert called_peers.count(target) == 1


# ---------------------------------------------------------------------------
# Tests: edge cases
# ---------------------------------------------------------------------------

class TestTriggerMemberSyncEdgeCases:
    def test_target_peer_none_uses_member_peers(self):
        """Local user (no origin_peer): candidates come from member peers."""
        p2p = _make_p2p(local_peer='local', connected=[])
        db = _make_db(origin_peer=None)  # local user
        ch = _make_ch_mgr(member_peers=['local', 'm1', 'm2'])
        app = _make_app(p2p, db, ch)

        with app.test_client() as client:
            _post_add(client)

        called_peers = {c.kwargs['target_peer_id']
                        for c in p2p.broadcast_member_sync.call_args_list}
        assert 'm1' in called_peers or 'm2' in called_peers
        assert 'local' not in called_peers

    def test_target_peer_equals_local_peer_not_sent_to_self(self):
        """Peer equal to local_peer must never appear as a send target."""
        local = 'local-peer'
        p2p = _make_p2p(local_peer=local, connected=[])
        db = _make_db(origin_peer=local)  # origin_peer == local
        ch = _make_ch_mgr(member_peers=[local, 'other'])
        app = _make_app(p2p, db, ch)

        with app.test_client() as client:
            _post_add(client)

        called_peers = [c.kwargs['target_peer_id']
                        for c in p2p.broadcast_member_sync.call_args_list]
        assert local not in called_peers

    def test_empty_member_list_no_send(self):
        """With no remote member peers and no connected peers, nothing is sent."""
        p2p = _make_p2p(local_peer='local', connected=[])
        db = _make_db(origin_peer=None)
        ch = _make_ch_mgr(member_peers=['local'])  # only local member
        app = _make_app(p2p, db, ch)

        with app.test_client() as client:
            _post_add(client)

        p2p.broadcast_member_sync.assert_not_called()

    def test_non_private_channel_skipped(self):
        """Open channels must not trigger any member sync."""
        p2p = _make_p2p(local_peer='local', connected=['c1'])
        open_row = _make_channel_row(privacy_mode='open')
        db = _make_db(origin_peer='remote', channel_row=open_row)
        ch = _make_ch_mgr(member_peers=['local', 'remote'])
        app = _make_app(p2p, db, ch)

        with app.test_client() as client:
            _post_add(client)

        p2p.broadcast_member_sync.assert_not_called()

    def test_p2p_not_running_skipped(self):
        """When P2P manager is not running, no sync is attempted."""
        p2p = _make_p2p()
        p2p.is_running.return_value = False
        db = _make_db(origin_peer='remote')
        ch = _make_ch_mgr(member_peers=['local', 'remote'])
        app = _make_app(p2p, db, ch)

        with app.test_client() as client:
            _post_add(client)

        p2p.broadcast_member_sync.assert_not_called()


# ---------------------------------------------------------------------------
# Tests: channel announce after member add
# ---------------------------------------------------------------------------

class TestChannelAnnounceAfterMemberAdd:
    def test_announce_fires_on_add(self):
        """broadcast_channel_announce must be called when a member is added."""
        p2p = _make_p2p(local_peer='local', connected=[])
        db = _make_db(origin_peer='remote')
        ch = _make_ch_mgr(member_peers=['local', 'remote'])
        app = _make_app(p2p, db, ch)

        with app.test_client() as client:
            resp = _post_add(client)

        assert resp.status_code == 200
        p2p.broadcast_channel_announce.assert_called_once()

    def test_announce_not_fired_on_remove(self):
        """broadcast_channel_announce must NOT be called when a member is removed."""
        p2p = _make_p2p(local_peer='local', connected=[])
        db = _make_db(origin_peer='remote')
        ch = _make_ch_mgr(member_peers=['local', 'remote'])
        app = _make_app(p2p, db, ch)

        with app.test_client() as client:
            resp = _delete_remove(client)

        assert resp.status_code == 200
        p2p.broadcast_channel_announce.assert_not_called()
