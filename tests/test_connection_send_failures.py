"""Regression tests for dead-connection send handling."""

import asyncio
import os
import sys
import types
import unittest
from unittest.mock import patch

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

from canopy.network.connection import ConnectionManager, ConnectionState, PeerConnection


class _FakeIdentityManager:
    local_identity = None


class _BlockingWebSocket:
    def __init__(self) -> None:
        self.closed = False
        self.send_calls = 0
        self.first_send_started = asyncio.Event()

    async def send(self, _message: str) -> None:
        self.send_calls += 1
        self.first_send_started.set()
        await asyncio.sleep(3600)

    async def close(self) -> None:
        self.closed = True


class TestConnectionSendFailures(unittest.IsolatedAsyncioTestCase):
    async def test_timeout_marks_connection_dead_before_waiting_senders_retry(self):
        manager = ConnectionManager(
            local_peer_id='peer-local',
            identity_manager=_FakeIdentityManager(),
        )
        websocket = _BlockingWebSocket()
        connection = PeerConnection(
            peer_id='peer-remote',
            address='127.0.0.1',
            port=7771,
            state=ConnectionState.AUTHENTICATED,
            websocket=websocket,
        )
        manager.connections['peer-remote'] = connection

        wait_for_calls = 0

        async def fake_wait_for(awaitable, timeout):
            nonlocal wait_for_calls
            wait_for_calls += 1
            task = asyncio.create_task(awaitable)
            await websocket.first_send_started.wait()
            await asyncio.sleep(0)
            task.cancel()
            raise asyncio.TimeoutError()

        with patch('canopy.network.connection.asyncio.wait_for', new=fake_wait_for):
            first = asyncio.create_task(
                manager.send_to_peer('peer-remote', {'type': 'p2p_message', 'message': {'id': 'one'}})
            )
            await websocket.first_send_started.wait()
            second = asyncio.create_task(
                manager.send_to_peer('peer-remote', {'type': 'p2p_message', 'message': {'id': 'two'}})
            )
            first_ok = await first
            second_ok = await second

        self.assertFalse(first_ok)
        self.assertFalse(second_ok)
        self.assertEqual(wait_for_calls, 1)
        self.assertEqual(websocket.send_calls, 1)
        self.assertEqual(connection.state, ConnectionState.DISCONNECTED)

    async def test_send_timeout_fires_on_peer_disconnected_callback(self):
        manager = ConnectionManager(
            local_peer_id='peer-local',
            identity_manager=_FakeIdentityManager(),
        )
        websocket = _BlockingWebSocket()
        connection = PeerConnection(
            peer_id='peer-remote',
            address='127.0.0.1',
            port=7771,
            state=ConnectionState.AUTHENTICATED,
            websocket=websocket,
        )
        manager.connections['peer-remote'] = connection

        disconnected: list[str] = []
        manager.on_peer_disconnected = disconnected.append

        async def fake_wait_for(awaitable, timeout):
            task = asyncio.create_task(awaitable)
            await websocket.first_send_started.wait()
            await asyncio.sleep(0)
            task.cancel()
            raise asyncio.TimeoutError()

        with patch('canopy.network.connection.asyncio.wait_for', new=fake_wait_for):
            ok = await manager.send_to_peer('peer-remote', {'type': 'p2p_message', 'message': {'id': 'one'}})
            await asyncio.sleep(0)

        self.assertFalse(ok)
        self.assertEqual(disconnected, ['peer-remote'])

    async def test_send_connection_closed_fires_on_peer_disconnected_callback(self):
        manager = ConnectionManager(
            local_peer_id='peer-local',
            identity_manager=_FakeIdentityManager(),
        )

        class _ClosedWebSocket:
            closed = False

            async def send(self, _message: str) -> None:
                raise RuntimeError('socket already closed')

            async def close(self) -> None:
                self.closed = True

        websocket = _ClosedWebSocket()
        connection = PeerConnection(
            peer_id='peer-remote',
            address='127.0.0.1',
            port=7771,
            state=ConnectionState.AUTHENTICATED,
            websocket=websocket,
        )
        manager.connections['peer-remote'] = connection

        disconnected: list[str] = []
        manager.on_peer_disconnected = disconnected.append

        class _FakeConnectionClosed(RuntimeError):
            def __init__(self, message: str) -> None:
                super().__init__(message)
                self.rcvd = None

        websocket_ns = types.SimpleNamespace(
            exceptions=types.SimpleNamespace(ConnectionClosed=_FakeConnectionClosed)
        )
        with patch('canopy.network.connection.websockets', websocket_ns):
            ok = await manager.send_to_peer('peer-remote', {'type': 'p2p_message', 'message': {'id': 'two'}})
        await asyncio.sleep(0)

        self.assertFalse(ok)
        self.assertEqual(disconnected, ['peer-remote'])


if __name__ == '__main__':
    unittest.main()
