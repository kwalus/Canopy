#!/usr/bin/env python3
"""
Bump the patch version (sub-sub version) in canopy/__init__.py.
Run this before each push to GitHub so the repo version increments.

Usage:
  python scripts/bump_version.py
  # Then push canopy/__init__.py (and your other changed files) via push_one_file_mcp.py
"""
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
INIT = REPO_ROOT / "canopy" / "__init__.py"


def main():
    if not INIT.exists():
        print(f"Not found: {INIT}", file=sys.stderr)
        return 1
    text = INIT.read_text(encoding="utf-8")
    match = re.search(r'__version__\s*=\s*["\'](\d+)\.(\d+)\.(\d+)["\']', text)
    if not match:
        print("Could not find __version__ in canopy/__init__.py", file=sys.stderr)
        return 1
    major, minor, patch = match.group(1), match.group(2), int(match.group(3))
    new_patch = patch + 1
    new_version = f"{major}.{minor}.{new_patch}"
    new_text = re.sub(
        r'(__version__\s*=\s*["\'])\d+\.\d+\.\d+(["\'])',
        rf"\g<1>{new_version}\g<2>",
        text,
        count=1,
    )
    if new_text == text:
        print("Version line unchanged", file=sys.stderr)
        return 1
    INIT.write_text(new_text, encoding="utf-8")
    print(f"Bumped to {new_version}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
