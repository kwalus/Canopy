"""Tests for canopy_tray.notifier message deep-link behavior."""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from canopy_tray.notifier import Notifier


class _FakeNotification:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.actions = []
        self.shown = False

    def add_actions(self, label, launch):
        self.actions.append((label, launch))

    def show(self):
        self.shown = True


class _FakeWinotify:
    def __init__(self):
        self.notifications = []

    def Notification(self, **kwargs):
        toast = _FakeNotification(**kwargs)
        self.notifications.append(toast)
        return toast


class TestCanopyTrayNotifier(unittest.TestCase):
    def test_message_toast_links_to_exact_message(self):
        notifier = Notifier(base_url="http://localhost:7770")
        fake_winotify = _FakeWinotify()
        notifier._winotify = fake_winotify

        notifier.notify_new_message(
            channel_name="general",
            sender_name="Alpha",
            content="hello",
            channel_id="general",
            message_id="M123",
        )

        self.assertEqual(len(fake_winotify.notifications), 1)
        toast = fake_winotify.notifications[0]
        self.assertTrue(toast.shown)
        self.assertEqual(
            toast.actions,
            [("Open", "http://localhost:7770/channels?focus_channel=general&focus_message=M123")],
        )


if __name__ == "__main__":
    unittest.main()
