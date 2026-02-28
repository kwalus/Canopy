"""
Entry point for the Canopy system tray application.

Usage:
    python -m canopy_tray
    python -m canopy_tray --port 7770

Author: Konrad Walus (architecture, design, and direction)
Project: Canopy - Local Mesh Communication
License: Apache 2.0
Development: AI-assisted implementation (Claude, Codex, GitHub Copilot, Cursor IDE, Ollama)
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path


def _default_tray_home() -> Path:
    """Return a stable, user-writable runtime directory for packaged builds.

    We keep Canopy's relative `./data/` + `./logs/` under this folder so updates
    (new exe in a different location) do not reset identity/state.
    """
    if sys.platform == "win32":
        base = (
            os.environ.get("LOCALAPPDATA")
            or os.environ.get("APPDATA")
            or str(Path.home() / "AppData" / "Local")
        )
        return Path(base) / "Canopy"
    # macOS/Linux: keep in ~/.canopy/tray
    return Path.home() / ".canopy" / "tray"


def main():
    parser = argparse.ArgumentParser(
        description="Canopy - System Tray Application",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("CANOPY_PORT", "7770")),
        help="Port for the Canopy web server (default: 7770)",
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("CANOPY_HOST", "127.0.0.1"),
        help="Host to bind the web server to (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging",
    )
    parser.add_argument(
        "--tray-home",
        default=os.environ.get("CANOPY_TRAY_HOME", ""),
        help="Runtime directory for the tray app (data/logs). "
             "Defaults to a per-user location when running as a packaged .exe.",
    )
    args = parser.parse_args()

    # For packaged builds, force a stable runtime directory so data/logs persist
    # across updates and never try to write next to the .exe.
    tray_home: Path | None = None
    if args.tray_home:
        tray_home = Path(args.tray_home).expanduser()
    elif getattr(sys, "frozen", False):
        tray_home = _default_tray_home()

    if tray_home:
        try:
            tray_home.mkdir(parents=True, exist_ok=True)
            # Keep Canopy's relative paths (./data, ./logs) under this folder.
            os.chdir(str(tray_home))
            # Only publish the env var after chdir succeeds — other code reads it.
            os.environ["CANOPY_TRAY_HOME"] = str(tray_home)
        except Exception as exc:
            # Best-effort: if this fails, Canopy will fall back to current CWD.
            logging.getLogger("canopy_tray").warning(
                f"Failed to set tray home to {tray_home}: {exc}"
            )
            tray_home = None

    # Configure logging
    level = logging.DEBUG if args.debug else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)-5s | %(name)-20s | %(message)s",
        datefmt="%H:%M:%S",
    )

    # Suppress noisy loggers in non-debug mode
    if not args.debug:
        logging.getLogger("werkzeug").setLevel(logging.WARNING)
        logging.getLogger("urllib3").setLevel(logging.WARNING)

    logger = logging.getLogger("canopy_tray")
    extra = f", home={tray_home}" if tray_home else ""
    logger.info(f"Canopy Tray v0.1.0 starting (port={args.port}{extra})")

    # Ensure the project root is on sys.path so canopy package can be imported
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if project_root not in sys.path:
        sys.path.insert(0, project_root)

    from canopy_tray.app import TrayApp

    app = TrayApp(host=args.host, port=args.port)
    app.run()


if __name__ == "__main__":
    main()
