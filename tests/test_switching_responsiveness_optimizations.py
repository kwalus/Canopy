"""Regression tests for switching-path optimization helpers."""

import os
import sys
import types
import unittest
from unittest.mock import MagicMock, patch

from flask import Flask, session

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

from canopy.ui import routes as ui_routes


class TestSwitchingResponsivenessOptimizations(unittest.TestCase):
    def test_current_session_meshspace_attention_is_cached_per_request(self) -> None:
        app = Flask(__name__)
        app.secret_key = 'switching-optimizations-test'

        fake_components = (
            MagicMock(),
            None,
            None,
            None,
            MagicMock(),
            None,
            MagicMock(),
            None,
            None,
            None,
            MagicMock(),
        )

        with patch('canopy.ui.routes._session_is_authenticated', return_value=True), \
             patch('canopy.ui.routes._get_app_components_any', return_value=fake_components), \
             patch(
                 'canopy.ui.routes.build_meshspace_notification_summary',
                 side_effect=[
                     {'unread_count': 3, 'mention_count': 1, 'pending_review_count': 0, 'attention_count': 4},
                     {'unread_count': 5, 'mention_count': 2, 'pending_review_count': 1, 'attention_count': 8},
                 ],
             ) as summary_mock:
            with app.test_request_context('/'):
                session['user_id'] = 'owner'
                first = ui_routes._current_session_meshspace_attention()
                self.assertEqual(summary_mock.call_count, 1)
                self.assertEqual(first['unread_count'], 3)

                first['unread_count'] = 99
                second = ui_routes._current_session_meshspace_attention()
                self.assertEqual(summary_mock.call_count, 1)
                self.assertEqual(second['unread_count'], 3)

            with app.test_request_context('/'):
                session['user_id'] = 'owner'
                third = ui_routes._current_session_meshspace_attention()
                self.assertEqual(summary_mock.call_count, 2)
                self.assertEqual(third['unread_count'], 5)


if __name__ == '__main__':
    unittest.main()
