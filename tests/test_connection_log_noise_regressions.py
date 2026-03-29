"""Regression tests for expected reconnect failure logging."""

import os
import sys
import types
import unittest
from unittest.mock import AsyncMock, patch

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

from canopy.network.connection import ConnectionManager


class _FakeIdentityManager:
    local_identity = None


class TestConnectionLogNoiseRegressions(unittest.IsolatedAsyncioTestCase):
    async def test_expected_connect_refusal_logs_warning_without_traceback(self):
        manager = ConnectionManager(
            local_peer_id='peer-local',
            identity_manager=_FakeIdentityManager(),
        )
        manager._disconnect_connection = AsyncMock()

        error = ConnectionRefusedError(61, "Connect call failed ('127.0.0.1', 7771)")

        with patch('canopy.network.connection.websockets.connect', side_effect=error), patch(
            'canopy.network.connection.logger'
        ) as logger:
            ok = await manager.connect_to_peer('peer-remote', '127.0.0.1', 7771)

        self.assertFalse(ok)
        logger.warning.assert_called_once()
        logger.error.assert_not_called()


if __name__ == '__main__':
    unittest.main()
