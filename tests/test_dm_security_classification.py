import asyncio
import os
import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

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

from canopy.core.messaging import is_local_dm_user
from canopy.network.manager import P2PNetworkManager


class _DbWrapper:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def get_connection(self):
        return self._conn

    def get_user(self, user_id: str):
        row = self._conn.execute(
            "SELECT * FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
        return dict(row) if row else None


class _FakeRouter:
    def __init__(self) -> None:
        self.calls = []

    async def send_dm_broadcast(self, content, metadata):
        self.calls.append({'content': content, 'metadata': metadata})
        return True


class _FakeFuture:
    def __init__(self, value):
        self._value = value

    def result(self, timeout=None):
        return self._value


class _FakeIdentityManager:
    def get_peer(self, peer_id):
        return None


class _FakeP2PView:
    def get_peer_id(self) -> str:
        return 'peer-local'


class TestDmSecurityClassification(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        db_path = Path(self.tempdir.name) / 'dm_security.db'
        self.conn = sqlite3.connect(str(db_path))
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
                created_at TEXT
            );
            CREATE TABLE api_keys (
                id TEXT PRIMARY KEY,
                user_id TEXT,
                key_hash TEXT,
                permissions TEXT,
                created_at TEXT,
                expires_at TEXT,
                revoked INTEGER DEFAULT 0
            );
            CREATE TABLE user_keys (
                user_id TEXT PRIMARY KEY,
                ed25519_public_key TEXT,
                ed25519_private_key TEXT,
                x25519_public_key TEXT,
                x25519_private_key TEXT
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
                    'local-user', 'local_user', 'Local User', 'pk-local', 'pw-local',
                    'human', 'active', None, '', '2026-03-08T00:00:00+00:00'
                ),
                (
                    'remote-human', 'homie', 'Homie', 'pk-remote', None,
                    'human', 'active', None, '', '2026-03-08T00:01:00+00:00'
                ),
                (
                    'remote-shadow', 'peer-remote-shadow', 'Remote Shadow', 'pk-shadow', None,
                    'human', 'active', 'peer-remote', '', '2026-03-08T00:02:00+00:00'
                ),
            ],
        )
        self.conn.commit()
        self.db = _DbWrapper(self.conn)
        self.p2p_view = _FakeP2PView()

    def tearDown(self) -> None:
        self.conn.close()

    def test_registered_user_with_blank_origin_is_local(self) -> None:
        self.assertTrue(is_local_dm_user(self.db, self.p2p_view, 'local-user'))

    def test_unregistered_user_with_blank_origin_is_not_assumed_local(self) -> None:
        self.assertFalse(is_local_dm_user(self.db, self.p2p_view, 'remote-human'))

    def test_security_summary_does_not_downgrade_ambiguous_remote_user_to_local_only(self) -> None:
        manager = P2PNetworkManager.__new__(P2PNetworkManager)
        manager.db = self.db
        manager.local_identity = type('LocalIdentity', (), {'peer_id': 'peer-local'})()
        manager.identity_manager = _FakeIdentityManager()
        manager.peer_supports_capability = lambda peer_id, capability: False

        summary = manager.describe_direct_message_security(['remote-human'])

        self.assertEqual(summary.get('mode'), 'legacy_plaintext')
        self.assertFalse(summary.get('local_only'))
        self.assertIn('remote-human', summary.get('unknown_peer_ids') or [])

    def test_group_summary_with_ambiguous_remote_member_is_not_local_only(self) -> None:
        manager = P2PNetworkManager.__new__(P2PNetworkManager)
        manager.db = self.db
        manager.local_identity = type('LocalIdentity', (), {'peer_id': 'peer-local'})()
        manager.identity_manager = _FakeIdentityManager()
        manager.peer_supports_capability = lambda peer_id, capability: False

        summary = manager.describe_direct_message_security(['remote-human', 'local-user'])

        self.assertEqual(summary.get('mode'), 'legacy_plaintext')
        self.assertFalse(summary.get('local_only'))
        self.assertIn('local-user', summary.get('local_recipient_ids') or [])
        self.assertIn('remote-human', summary.get('unknown_peer_ids') or [])

    def test_broadcast_still_runs_for_ambiguous_remote_user(self) -> None:
        manager = P2PNetworkManager.__new__(P2PNetworkManager)
        manager._running = True
        manager._event_loop = object()
        manager.message_router = _FakeRouter()
        manager.db = self.db
        manager.local_identity = type('LocalIdentity', (), {'peer_id': 'peer-local'})()
        manager.identity_manager = _FakeIdentityManager()
        manager.peer_supports_capability = lambda peer_id, capability: False
        manager.file_manager = None

        def _run_coroutine(coro, _loop):
            loop = asyncio.new_event_loop()
            try:
                return _FakeFuture(loop.run_until_complete(coro))
            finally:
                loop.close()

        with patch('canopy.network.manager.asyncio.run_coroutine_threadsafe', side_effect=_run_coroutine):
            ok = manager.broadcast_direct_message(
                sender_id='local-user',
                recipient_id='remote-human',
                content='still relay this',
                message_id='DM-ambiguous',
                timestamp='2026-03-08T12:00:00+00:00',
                metadata={},
            )

        self.assertTrue(ok)
        self.assertEqual(len(manager.message_router.calls), 1)
        security = manager.message_router.calls[0]['metadata']['metadata']['security']
        self.assertEqual(security.get('mode'), 'legacy_plaintext')
        self.assertFalse(security.get('local_only'))


if __name__ == '__main__':
    unittest.main()
