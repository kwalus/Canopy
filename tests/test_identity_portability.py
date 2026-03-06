"""Unit tests for distributed-auth Phase 1 identity portability manager."""

import os
import sqlite3
import sys
import types
import unittest
from dataclasses import dataclass
from datetime import datetime, timezone

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

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

from canopy.core.identity_portability import IdentityPortabilityManager


def _iso_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@dataclass
class _FakeSecurityConfig:
    identity_portability_enabled: bool = True


@dataclass
class _FakeConfig:
    security: _FakeSecurityConfig


class _SigningIdentity:
    def __init__(self) -> None:
        self._private = Ed25519PrivateKey.generate()
        self._public = self._private.public_key()

    def sign(self, payload: bytes) -> bytes:
        return self._private.sign(payload)

    def verify(self, payload: bytes, signature: bytes) -> bool:
        try:
            self._public.verify(signature, payload)
            return True
        except Exception:
            return False


class _FakeIdentityManager:
    def __init__(self, local_peer_id: str, local_identity: _SigningIdentity) -> None:
        self.local_identity = local_identity
        self._local_peer_id = local_peer_id
        self._peers = {local_peer_id: local_identity}

    def get_peer(self, peer_id: str):
        return self._peers.get(peer_id)


class _FakeP2PManager:
    def __init__(self, local_peer_id: str = 'peer-local') -> None:
        self._local_peer_id = local_peer_id
        self.local_identity = _SigningIdentity()
        self.identity_manager = _FakeIdentityManager(local_peer_id, self.local_identity)
        self._capable = {'peer-1', 'peer-2'}
        self._connected = {'peer-1', 'peer-2'}
        self.sent_principal = []
        self.sent_grants = []
        self.sent_revokes = []

    def get_peer_id(self) -> str:
        return self._local_peer_id

    def peer_supports_capability(self, peer_id: str, capability: str) -> bool:
        return capability == 'identity_portability_v1' and peer_id in self._capable

    def get_connected_peers(self):
        return list(self._connected)

    def send_principal_announce(self, to_peer, principal, keys=None):
        self.sent_principal.append((to_peer, principal, keys or []))
        return True

    def send_bootstrap_grant_sync(self, to_peer, grant):
        self.sent_grants.append((to_peer, grant))
        return True

    def send_bootstrap_grant_revoke(self, to_peer, grant_id, revoked_at, reason=None, issuer_peer_id=None):
        self.sent_revokes.append((to_peer, grant_id, revoked_at, reason, issuer_peer_id))
        return True


class _FakeP2PManagerNoLocalPeer(_FakeP2PManager):
    def __init__(self) -> None:
        super().__init__(local_peer_id='peer-local')

    def get_peer_id(self):
        return None


class _FakeDbManager:
    def __init__(self) -> None:
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        self.conn.execute('PRAGMA foreign_keys = ON')
        self.conn.executescript(
            """
            CREATE TABLE users (
                id TEXT PRIMARY KEY,
                username TEXT UNIQUE NOT NULL,
                public_key TEXT NOT NULL,
                password_hash TEXT,
                display_name TEXT,
                origin_peer TEXT,
                status TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE mesh_principals (
                principal_id TEXT PRIMARY KEY,
                display_name TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                origin_peer TEXT NOT NULL,
                status TEXT DEFAULT 'active',
                metadata_json TEXT
            );
            CREATE TABLE mesh_principal_keys (
                id TEXT PRIMARY KEY,
                principal_id TEXT NOT NULL,
                key_type TEXT NOT NULL,
                key_data TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                revoked_at TIMESTAMP,
                metadata_json TEXT,
                FOREIGN KEY (principal_id) REFERENCES mesh_principals(principal_id) ON DELETE CASCADE,
                UNIQUE(principal_id, key_type, key_data)
            );
            CREATE TABLE mesh_principal_links (
                principal_id TEXT NOT NULL,
                local_user_id TEXT NOT NULL,
                linked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                linked_by TEXT,
                source TEXT DEFAULT 'local',
                PRIMARY KEY (principal_id, local_user_id),
                FOREIGN KEY (principal_id) REFERENCES mesh_principals(principal_id) ON DELETE CASCADE,
                FOREIGN KEY (local_user_id) REFERENCES users(id) ON DELETE CASCADE
            );
            CREATE TABLE mesh_bootstrap_grants (
                grant_id TEXT PRIMARY KEY,
                principal_id TEXT NOT NULL,
                granted_role TEXT DEFAULT 'user',
                audience_peer TEXT,
                max_uses INTEGER DEFAULT 1,
                uses_consumed INTEGER DEFAULT 0,
                expires_at TIMESTAMP NOT NULL,
                created_by TEXT NOT NULL,
                issuer_peer_id TEXT NOT NULL,
                issued_at TIMESTAMP NOT NULL,
                signature TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                status TEXT DEFAULT 'active',
                revoked_at TIMESTAMP,
                revoked_reason TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (principal_id) REFERENCES mesh_principals(principal_id) ON DELETE CASCADE
            );
            CREATE TABLE mesh_bootstrap_grant_applications (
                grant_id TEXT NOT NULL,
                local_user_id TEXT NOT NULL,
                applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                applied_by TEXT,
                source_peer TEXT,
                PRIMARY KEY (grant_id, local_user_id),
                FOREIGN KEY (grant_id) REFERENCES mesh_bootstrap_grants(grant_id) ON DELETE CASCADE,
                FOREIGN KEY (local_user_id) REFERENCES users(id) ON DELETE CASCADE
            );
            CREATE TABLE mesh_bootstrap_grant_revocations (
                grant_id TEXT PRIMARY KEY,
                issuer_peer_id TEXT NOT NULL,
                revoked_at TIMESTAMP NOT NULL,
                reason TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE mesh_principal_audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                principal_id TEXT,
                grant_id TEXT,
                action TEXT NOT NULL,
                source_peer TEXT,
                actor_user_id TEXT,
                details_json TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        self.conn.executemany(
            """
            INSERT INTO users (id, username, public_key, password_hash, display_name, origin_peer, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                ('user-a', 'alice', 'alice-pk', 'hash-a', 'Alice', None, 'active', _iso_now()),
                ('user-b', 'bob', 'bob-pk', 'hash-b', 'Bob', None, 'active', _iso_now()),
            ],
        )
        self.conn.commit()

    def get_connection(self):
        return self.conn


class TestIdentityPortabilityManager(unittest.TestCase):
    def setUp(self) -> None:
        self.db = _FakeDbManager()
        self.p2p = _FakeP2PManager(local_peer_id='peer-local')
        cfg = _FakeConfig(security=_FakeSecurityConfig(identity_portability_enabled=True))
        self.manager = IdentityPortabilityManager(self.db, cfg, self.p2p)

    def test_create_and_apply_grant_is_idempotent(self) -> None:
        created = self.manager.create_bootstrap_grant(
            local_user_id='user-a',
            acting_user_id='user-a',
            audience_peer='peer-local',
            expires_in_hours=12,
            max_uses=1,
            sync_to_mesh=True,
        )
        grant = created.get('grant') or {}
        artifact = created.get('artifact') or {}
        grant_id = grant.get('grant_id')
        self.assertTrue(grant_id)
        self.assertEqual(artifact.get('granted_role'), 'user')
        self.assertGreaterEqual(len(self.p2p.sent_grants), 1)

        first_apply = self.manager.apply_bootstrap_grant(
            grant_id=grant_id,
            local_user_id='user-b',
            actor_user_id='user-b',
            source_peer='peer-local',
        )
        self.assertTrue(first_apply.get('applied'))
        self.assertEqual(first_apply.get('status'), 'consumed')

        second_apply = self.manager.apply_bootstrap_grant(
            grant_id=grant_id,
            local_user_id='user-b',
            actor_user_id='user-b',
            source_peer='peer-local',
        )
        self.assertTrue(second_apply.get('applied'))
        self.assertTrue(second_apply.get('idempotent'))

        with self.db.get_connection() as conn:
            row = conn.execute(
                "SELECT uses_consumed, status FROM mesh_bootstrap_grants WHERE grant_id = ?",
                (grant_id,),
            ).fetchone()
            self.assertEqual(int(row['uses_consumed']), 1)
            self.assertEqual(row['status'], 'consumed')
            link = conn.execute(
                "SELECT 1 FROM mesh_principal_links WHERE local_user_id = ?",
                ('user-b',),
            ).fetchone()
            self.assertIsNotNone(link)

    def test_import_rejects_bad_signature_and_issuer_source_mismatch(self) -> None:
        created = self.manager.create_bootstrap_grant(
            local_user_id='user-a',
            acting_user_id='user-a',
            audience_peer='peer-local',
            expires_in_hours=8,
            max_uses=1,
            sync_to_mesh=False,
        )
        artifact = dict(created.get('artifact') or {})
        self.assertIn('signature', artifact)

        tampered = dict(artifact)
        tampered['principal_id'] = 'PRNtampered'
        tampered_result = self.manager.import_bootstrap_grant(
            tampered,
            source_peer='peer-local',
            actor_user_id='user-a',
        )
        self.assertFalse(tampered_result.get('imported'))
        self.assertEqual(tampered_result.get('reason'), 'bad_signature')

        mismatch_result = self.manager.import_bootstrap_grant(
            artifact,
            source_peer='peer-other',
            actor_user_id='user-a',
        )
        self.assertFalse(mismatch_result.get('imported'))
        self.assertEqual(mismatch_result.get('reason'), 'issuer_source_mismatch')

    def test_revoked_grant_cannot_be_applied(self) -> None:
        created = self.manager.create_bootstrap_grant(
            local_user_id='user-a',
            acting_user_id='user-a',
            audience_peer='peer-local',
            expires_in_hours=8,
            max_uses=1,
            sync_to_mesh=False,
        )
        grant_id = (created.get('grant') or {}).get('grant_id')
        self.assertTrue(grant_id)

        revoked = self.manager.revoke_bootstrap_grant(
            grant_id=grant_id,
            actor_user_id='user-a',
            reason='test_revoke',
            sync_to_mesh=True,
        )
        self.assertTrue(revoked.get('revoked'))
        self.assertGreaterEqual(len(self.p2p.sent_revokes), 1)

        applied = self.manager.apply_bootstrap_grant(
            grant_id=grant_id,
            local_user_id='user-b',
            actor_user_id='user-b',
            source_peer='peer-local',
        )
        self.assertFalse(applied.get('applied'))
        self.assertEqual(applied.get('reason'), 'revoked')

    def test_targeted_grant_sync_is_scoped_and_sanitized(self) -> None:
        created = self.manager.create_bootstrap_grant(
            local_user_id='user-a',
            acting_user_id='user-a',
            audience_peer='peer-1',
            expires_in_hours=4,
            max_uses=1,
            sync_to_mesh=True,
            target_peer_id='peer-1',
        )

        self.assertEqual(len(self.p2p.sent_principal), 1)
        self.assertEqual(self.p2p.sent_principal[0][0], 'peer-1')
        principal_payload = self.p2p.sent_principal[0][1] or {}
        key_payloads = self.p2p.sent_principal[0][2] or []
        metadata = principal_payload.get('metadata') or {}
        self.assertNotIn('local_user_id', metadata)
        self.assertEqual(len(key_payloads), 1)
        key_metadata = (key_payloads[0] or {}).get('metadata') or {}
        self.assertNotIn('user_id', key_metadata)

        self.assertEqual(len(self.p2p.sent_grants), 1)
        self.assertEqual(self.p2p.sent_grants[0][0], 'peer-1')
        self.assertEqual((created.get('artifact') or {}).get('audience_peer'), 'peer-1')

    def test_offline_grant_does_not_sync_principal_or_grant(self) -> None:
        self.manager.create_bootstrap_grant(
            local_user_id='user-a',
            acting_user_id='user-a',
            audience_peer='peer-1',
            expires_in_hours=4,
            max_uses=1,
            sync_to_mesh=False,
            target_peer_id='peer-1',
        )

        self.assertEqual(self.p2p.sent_principal, [])
        self.assertEqual(self.p2p.sent_grants, [])

    def test_missing_local_peer_blocks_targeted_import_revoke_and_create(self) -> None:
        created = self.manager.create_bootstrap_grant(
            local_user_id='user-a',
            acting_user_id='user-a',
            audience_peer='peer-target',
            expires_in_hours=8,
            max_uses=1,
            sync_to_mesh=False,
        )
        artifact = dict(created.get('artifact') or {})
        grant_id = str((created.get('grant') or {}).get('grant_id') or '')
        self.assertTrue(grant_id)

        no_peer_p2p = _FakeP2PManagerNoLocalPeer()
        no_peer_p2p.local_identity = self.p2p.local_identity
        no_peer_p2p.identity_manager = self.p2p.identity_manager
        no_peer_manager = IdentityPortabilityManager(
            self.db,
            _FakeConfig(security=_FakeSecurityConfig(identity_portability_enabled=True)),
            no_peer_p2p,
        )

        imported = no_peer_manager.import_bootstrap_grant(
            artifact,
            source_peer='peer-local',
            actor_user_id='user-a',
        )
        self.assertFalse(imported.get('imported'))
        self.assertEqual(imported.get('reason'), 'local_peer_unavailable')

        revoked = no_peer_manager.revoke_bootstrap_grant(
            grant_id=grant_id,
            actor_user_id='user-a',
            sync_to_mesh=False,
        )
        self.assertFalse(revoked.get('revoked'))
        self.assertEqual(revoked.get('reason'), 'local_peer_unavailable')

        with self.assertRaises(RuntimeError):
            no_peer_manager.create_bootstrap_grant(
                local_user_id='user-a',
                acting_user_id='user-a',
                audience_peer='peer-target',
                expires_in_hours=1,
                max_uses=1,
                sync_to_mesh=False,
            )

    def test_status_snapshot_handles_sqlite_rows(self) -> None:
        created = self.manager.create_bootstrap_grant(
            local_user_id='user-a',
            acting_user_id='user-a',
            audience_peer='peer-local',
            expires_in_hours=8,
            max_uses=1,
            sync_to_mesh=False,
        )
        self.assertTrue((created.get('grant') or {}).get('grant_id'))

        status = self.manager.get_status_snapshot()
        counts = status.get('counts') or {}
        self.assertEqual(counts.get('principals'), 1)
        self.assertEqual(counts.get('links'), 1)
        self.assertEqual(counts.get('grants'), 1)
        self.assertEqual(counts.get('revocations'), 0)

    def test_conflicting_origin_peer_is_not_overwritten(self) -> None:
        principal_id = 'PRNconflict0001'
        first = self.manager.handle_principal_announce(
            principal={
                'principal_id': principal_id,
                'display_name': 'Alice Origin A',
                'origin_peer': 'peer-a',
                'status': 'active',
                'updated_at': _iso_now(),
            },
            keys=[],
            from_peer='peer-a',
        )
        self.assertTrue(first.get('handled'))

        second = self.manager.handle_principal_announce(
            principal={
                'principal_id': principal_id,
                'display_name': 'Alice Origin B',
                'origin_peer': 'peer-b',
                'status': 'active',
                'updated_at': _iso_now(),
            },
            keys=[],
            from_peer='peer-b',
        )
        self.assertTrue(second.get('handled'))

        with self.db.get_connection() as conn:
            row = conn.execute(
                "SELECT origin_peer FROM mesh_principals WHERE principal_id = ?",
                (principal_id,),
            ).fetchone()
        self.assertEqual(str(row['origin_peer']), 'peer-a')


if __name__ == '__main__':
    unittest.main()
