"""Regression tests for poll block and inline parsing.

Ensures [poll]...[/poll] and poll: ... formats are detected so channel/feed
messages render as poll cards. See canopy/core/polls.py.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from canopy.core.polls import parse_poll


class TestPollBlockParsing(unittest.TestCase):
    """Block format [poll]...[/poll] must parse (regex matches literal brackets)."""

    def test_block_format_parses(self) -> None:
        content = """[poll]
Who should get the next shout-out?
- ClawBOT
- Asmon_McClaw
- Mo_Money
duration: 3d
[/poll]"""
        spec = parse_poll(content)
        self.assertIsNotNone(spec)
        self.assertEqual(spec.question.strip(), "Who should get the next shout-out?")
        self.assertEqual(len(spec.options), 3)
        self.assertIn("ClawBOT", spec.options)
        self.assertIn("Asmon_McClaw", spec.options)
        self.assertIn("Mo_Money", spec.options)
        self.assertIsNotNone(spec.duration_seconds)

    def test_block_format_with_extra_whitespace(self) -> None:
        content = """  [poll]

What next?
- A
- B
- C
duration: 1d

[/poll]  """
        spec = parse_poll(content)
        self.assertIsNotNone(spec)
        self.assertEqual(spec.question.strip(), "What next?")
        self.assertEqual(len(spec.options), 3)

    def test_block_format_rejects_single_option(self) -> None:
        content = """[poll]
Q?
- Only one
duration: 1d
[/poll]"""
        spec = parse_poll(content)
        self.assertIsNone(spec)


class TestPollInlineParsing(unittest.TestCase):
    """Inline format poll: question then - options."""

    def test_inline_format_parses(self) -> None:
        content = """poll: What should we ship next?
- Reliability
- New UI polish
- MCP improvements
duration: 3d"""
        spec = parse_poll(content)
        self.assertIsNotNone(spec)
        self.assertIn("What should we ship next", spec.question)
        self.assertEqual(len(spec.options), 3)

    def test_inline_poll_only_question_line(self) -> None:
        content = """poll
What is best?
- X
- Y
duration: 1w"""
        spec = parse_poll(content)
        self.assertIsNotNone(spec)
        self.assertEqual(spec.question.strip(), "What is best?")
        self.assertEqual(len(spec.options), 2)


if __name__ == "__main__":
    unittest.main()
