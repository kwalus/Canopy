"""Version and docs consistency checks for the 0.4.0 release."""

import os
import re
import sys
import unittest

# Ensure repository root is importable when running tests directly.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

REPO_ROOT = os.path.join(os.path.dirname(__file__), '..')
EXPECTED_VERSION = "0.4.0"


class TestVersionConsistency(unittest.TestCase):
    """Confirms version metadata is aligned across package files."""

    def test_canopy_init_version(self):
        """canopy/__init__.py must declare __version__ == EXPECTED_VERSION."""
        import canopy
        self.assertEqual(canopy.__version__, EXPECTED_VERSION)

    def test_pyproject_version(self):
        """pyproject.toml must declare version = EXPECTED_VERSION."""
        pyproject_path = os.path.join(REPO_ROOT, "pyproject.toml")
        with open(pyproject_path) as f:
            content = f.read()
        match = re.search(r'^version\s*=\s*"([^"]+)"', content, re.MULTILINE)
        self.assertIsNotNone(match, "version field not found in pyproject.toml")
        self.assertEqual(match.group(1), EXPECTED_VERSION)

    def test_readme_badge_version(self):
        """README.md version badge must reference EXPECTED_VERSION."""
        readme_path = os.path.join(REPO_ROOT, "README.md")
        with open(readme_path) as f:
            content = f.read()
        self.assertIn(f"version-{EXPECTED_VERSION}", content)

    def test_no_stale_version_in_readme(self):
        """README.md must not contain stale 0.3.x version references outside the changelog."""
        readme_path = os.path.join(REPO_ROOT, "README.md")
        with open(readme_path) as f:
            content = f.read()
        # 0.3.x references are only acceptable in a changelog/history context
        stale = re.findall(r'\b0\.3\.\d+\b', content)
        self.assertEqual(stale, [], f"Stale 0.3.x references found in README.md: {stale}")

    def test_no_stale_version_in_security_md(self):
        """SECURITY.md must not contain stale 0.3.x version references."""
        security_path = os.path.join(REPO_ROOT, "SECURITY.md")
        with open(security_path) as f:
            content = f.read()
        stale = re.findall(r'\b0\.3\.\d+\b', content)
        self.assertEqual(stale, [], f"Stale 0.3.x references found in SECURITY.md: {stale}")

    def test_changelog_has_040_section(self):
        """CHANGELOG.md must have a [0.4.0] section covering key 0.4.0 features."""
        # Character window after the version header where features should be documented.
        CHANGELOG_SECTION_SEARCH_WINDOW = 2000
        changelog_path = os.path.join(REPO_ROOT, "CHANGELOG.md")
        with open(changelog_path) as f:
            content = f.read()
        self.assertIn("[0.4.0]", content)
        section_start = content.index("[0.4.0]")
        section = content[section_start:section_start + CHANGELOG_SECTION_SEARCH_WINDOW].lower()
        # Confirm the four headline features are documented
        self.assertIn("claim", section)
        self.assertIn("heartbeat", section)
        self.assertIn("agents", section)
        self.assertIn("system-health", section)


class TestDocumentationLinks(unittest.TestCase):
    """Confirms local links in release-facing docs resolve to real files."""

    def _check_local_links(self, doc_path):
        """Return a list of missing local link targets found in the given markdown file."""
        base_dir = os.path.dirname(doc_path)
        with open(doc_path) as f:
            content = f.read()
        # Extract markdown link targets: [text](target)
        link_pattern = re.compile(r'\[[^\]]*\]\(([^)]+)\)')
        missing = []
        for target in link_pattern.findall(content):
            # Skip absolute URLs and fragment-only links
            if target.startswith(('http://', 'https://', '#')):
                continue
            # Resolve relative to the document's directory or repo root
            candidates = [
                os.path.join(base_dir, target),
                os.path.join(REPO_ROOT, target),
            ]
            if not any(os.path.exists(p) for p in candidates):
                missing.append(target)
        return missing

    def test_readme_local_links(self):
        """All local links in README.md must resolve."""
        missing = self._check_local_links(os.path.join(REPO_ROOT, "README.md"))
        self.assertEqual(missing, [], f"Broken local links in README.md: {missing}")

    def test_release_notes_local_links(self):
        """All local links in docs/RELEASE_NOTES_0.4.0.md must resolve."""
        missing = self._check_local_links(
            os.path.join(REPO_ROOT, "docs", "RELEASE_NOTES_0.4.0.md")
        )
        self.assertEqual(missing, [], f"Broken local links in RELEASE_NOTES_0.4.0.md: {missing}")


if __name__ == "__main__":
    unittest.main()
