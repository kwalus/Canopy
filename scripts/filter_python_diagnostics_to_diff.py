#!/usr/bin/env python3
"""Filter flake8/mypy diagnostics down to changed lines in a git diff.

This lets CI fail on new issues introduced by a PR without being blocked by
large amounts of legacy lint/type debt elsewhere in the repository.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path


HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
DIAG_RE = re.compile(r"^(?P<path>[^:\n]+):(?P<line>\d+):")


def _git_diff(base: str, head: str) -> str:
    cmd = ["git", "diff", "--unified=0", "--no-color", base]
    if head:
        cmd.append(head)
    result = subprocess.run(
        cmd,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def _normalize_path(raw: str) -> str:
    path = raw.strip()
    if path.startswith("a/") or path.startswith("b/"):
        path = path[2:]
    return path


def _collect_changed_lines(diff_text: str) -> dict[str, set[int]]:
    changed: dict[str, set[int]] = defaultdict(set)
    current_file: str | None = None

    for line in diff_text.splitlines():
        if line.startswith("+++ "):
            candidate = line[4:].strip()
            current_file = None if candidate == "/dev/null" else _normalize_path(candidate)
            continue

        match = HUNK_RE.match(line)
        if not match or not current_file:
            continue

        start = int(match.group(1))
        count = int(match.group(2) or "1")
        if count <= 0:
            continue
        changed[current_file].update(range(start, start + count))

    return changed


def _all_python_files() -> dict[str, set[int]]:
    repo_root = Path.cwd()
    changed: dict[str, set[int]] = {}
    for path in repo_root.rglob("*.py"):
        if ".git/" in str(path):
            continue
        rel = path.relative_to(repo_root).as_posix()
        try:
            line_count = sum(1 for _ in path.open("r", encoding="utf-8", errors="ignore"))
        except Exception:
            line_count = 0
        changed[rel] = set(range(1, line_count + 1))
    return changed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", required=False, default="")
    parser.add_argument("--head", required=False, default="")
    args = parser.parse_args()

    base = str(args.base or "").strip()
    head = str(args.head or "").strip()

    if not base or set(base) == {"0"}:
        changed = _all_python_files()
    else:
        changed = _collect_changed_lines(_git_diff(base, head))

    matched_lines: list[str] = []
    for raw_line in sys.stdin.read().splitlines():
        match = DIAG_RE.match(raw_line)
        if not match:
            continue
        path = _normalize_path(match.group("path"))
        line_no = int(match.group("line"))
        if line_no in changed.get(path, set()):
            matched_lines.append(raw_line)

    if matched_lines:
        sys.stdout.write("\n".join(matched_lines) + "\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
