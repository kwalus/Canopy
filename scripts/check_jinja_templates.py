#!/usr/bin/env python3
"""Parse shipped Jinja templates to catch syntax regressions in CI."""

from __future__ import annotations

import sys
from pathlib import Path

from jinja2 import Environment, FileSystemLoader


TEMPLATE_ROOT = Path(__file__).resolve().parents[1] / "canopy" / "ui" / "templates"


def main() -> int:
    env = Environment(loader=FileSystemLoader(str(TEMPLATE_ROOT)))
    failures: list[str] = []
    for path in sorted(TEMPLATE_ROOT.glob("*.html")):
        try:
            env.get_template(path.name)
        except Exception as exc:
            failures.append(f"{path.name}: {exc}")
    if failures:
        sys.stderr.write("Jinja template parse failures:\n")
        sys.stderr.write("\n".join(failures) + "\n")
        return 1
    print(f"Parsed {len(list(TEMPLATE_ROOT.glob('*.html')))} templates successfully")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
