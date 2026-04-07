"""Regression tests for @mention extraction boundaries."""

import os
import sys
import types
import unittest

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

from canopy.core.mentions import extract_mentions, _normalize_display_handle, resolve_mention_targets


class TestMentionExtraction(unittest.TestCase):
    def test_extracts_markdown_wrapped_mentions(self) -> None:
        text = (
            "1. **@Maddog** asked **@Asmon_McClaw** and **@Mo_Money**.\n"
            "2. Also ping @Codex_Agent."
        )
        self.assertEqual(
            extract_mentions(text),
            ['Maddog', 'Asmon_McClaw', 'Mo_Money', 'Codex_Agent'],
        )

    def test_ignores_email_addresses(self) -> None:
        text = "contact me at alice@example.com then ping @real_user"
        self.assertEqual(extract_mentions(text), ['real_user'])

    def test_ignores_embedded_non_boundary_at_signs(self) -> None:
        text = "foo@bar should not match, but (@agent_ok) should."
        self.assertEqual(extract_mentions(text), ['agent_ok'])


class TestNormalizeDisplayHandle(unittest.TestCase):
    """Regression: display_name with spaces/trim normalizes like SQL so mention resolution matches."""

    def test_collapse_spaces_and_underscore(self) -> None:
        self.assertEqual(_normalize_display_handle("Codex  Agent"), "Codex_Agent")
        self.assertEqual(_normalize_display_handle("  Codex  Agent  "), "Codex_Agent")

    def test_empty_or_none(self) -> None:
        self.assertEqual(_normalize_display_handle(""), "")
        self.assertEqual(_normalize_display_handle(None), "")


if __name__ == '__main__':
    unittest.main()
