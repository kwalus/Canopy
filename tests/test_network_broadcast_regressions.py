import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from canopy.network.routing import MessageRouter, MessageType


class _FakeConnectionManager:
    def __init__(self, release_event: asyncio.Event) -> None:
        self.release_event = release_event
        self.events: list[str] = []

    def get_connected_peers(self):
        return ['peer-slow', 'peer-fast']

    async def send_to_peer(self, peer_id, _message):
        if peer_id == 'peer-slow':
            self.events.append('slow-start')
            await self.release_event.wait()
            self.events.append('slow-end')
            return False
        self.events.append('fast-start')
        return True


class TestNetworkBroadcastRegressions(unittest.IsolatedAsyncioTestCase):
    async def test_route_broadcast_starts_other_peers_before_slow_peer_finishes(self):
        release_event = asyncio.Event()
        connection_manager = _FakeConnectionManager(release_event)
        router = MessageRouter('peer-local', identity_manager=None, connection_manager=connection_manager)
        delivered: list[str] = []

        async def _deliver_local(message):
            delivered.append(message.id)
            return True

        router._deliver_local = _deliver_local  # type: ignore[method-assign]
        message = router.create_message(
            MessageType.DIRECT_MESSAGE,
            None,
            {'content': 'hello'},
        )

        task = asyncio.create_task(router._route_broadcast(message))
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        self.assertIn('slow-start', connection_manager.events)
        self.assertIn(
            'fast-start',
            connection_manager.events,
            "fast peers should not wait behind a slow peer during broadcast",
        )

        release_event.set()
        result = await task

        self.assertTrue(result)
        self.assertEqual(delivered, [message.id])


if __name__ == '__main__':
    unittest.main()
