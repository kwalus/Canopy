"""Tests for canopy_tray.server tray API-key provisioning."""

import os
import sys
import tempfile
import types
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Provide a lightweight zeroconf stub for environments without optional deps.
if "zeroconf" not in sys.modules:
    zeroconf_stub = types.ModuleType("zeroconf")

    class _Dummy:
        def __init__(self, *args, **kwargs):
            pass

    zeroconf_stub.ServiceBrowser = _Dummy
    zeroconf_stub.ServiceInfo = _Dummy
    zeroconf_stub.Zeroconf = _Dummy
    zeroconf_stub.ServiceStateChange = _Dummy
    sys.modules["zeroconf"] = zeroconf_stub

from canopy.security.api_keys import Permission
from canopy_tray.server import ServerManager


class _FakeDbManager:
    def __init__(self, owner_user_id):
        self._owner_user_id = owner_user_id

    def get_instance_owner_user_id(self):
        return self._owner_user_id


class _FakeApiKeyManager:
    def __init__(self):
        self.generated = []

    def validate_key(self, raw_key, required_permission=None):
        return None

    def generate_key(self, user_id, permissions, expires_days=None):
        self.generated.append((user_id, tuple(permissions), expires_days))
        return "tray-key"


class TestCanopyTrayServer(unittest.TestCase):
    def test_does_not_generate_placeholder_key_without_owner(self):
        with tempfile.TemporaryDirectory() as tempdir:
            os.environ["CANOPY_TRAY_HOME"] = tempdir
            manager = ServerManager()
            api_key_manager = _FakeApiKeyManager()
            manager._app = type("FakeApp", (), {"config": {
                "API_KEY_MANAGER": api_key_manager,
                "DB_MANAGER": _FakeDbManager(None),
            }})()

            raw_key = manager._ensure_tray_api_key(Permission.READ_FEED)

            self.assertIsNone(raw_key)
            self.assertEqual(api_key_manager.generated, [])

    def tearDown(self):
        os.environ.pop("CANOPY_TRAY_HOME", None)


if __name__ == "__main__":
    unittest.main()
