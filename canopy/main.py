"""
Main entry point for Canopy application.

Uses waitress as the production WSGI server (cross-platform: Windows, macOS, Linux).
Falls back to Flask's development server when --debug is specified.

Author: Konrad Walus (architecture, design, and direction)
Project: Canopy - Local Mesh Communication
License: Apache 2.0
Development: AI-assisted implementation (Claude, Codex, GitHub Copilot, Cursor IDE, Ollama)
"""

import logging
import os
import signal
import sys
import argparse
from pathlib import Path
from typing import Any

from .core.app import create_app
from .core.config import Config

logger = logging.getLogger(__name__)

# Number of threads waitress uses to handle concurrent HTTP requests.
# The P2P mesh runs on its own asyncio thread and is unaffected by this value.
_WAITRESS_THREADS = 8


def _install_signal_handlers(app):
    """Install SIGTERM/SIGINT handlers for graceful shutdown.

    Ensures background threads (P2P network, WAL checkpoint) get a chance to
    clean up when the process is stopped by a process manager (systemd, Docker).
    """
    def _shutdown(signum, frame):
        print(f"\nCanopy received signal {signum}, shutting down gracefully...")
        shutdown_fn = app.config.get('SHUTDOWN_FUNCTION')
        if callable(shutdown_fn):
            try:
                shutdown_fn()
            except Exception as e:
                logger.error(f"Error during shutdown: {e}")
        sys.exit(0)

    try:
        signal.signal(signal.SIGTERM, _shutdown)
        signal.signal(signal.SIGINT, _shutdown)
    except (OSError, ValueError):
        # signal handlers can only be set from the main thread; safe to skip
        pass


def _serve_production(app: Any, host: str, port: int, threads: int) -> None:
    """Serve using waitress — production-grade, cross-platform WSGI server."""
    try:
        from waitress import serve
    except ImportError:
        print("ERROR: waitress is not installed. Run: pip install waitress")
        print("       Or install all dependencies: pip install -r requirements.txt")
        sys.exit(1)

    thread_count = max(1, int(threads))
    print(f"  Server: waitress ({thread_count} threads)")
    serve(
        app,
        host=host,
        port=port,
        threads=thread_count,
        # Disable response buffering so SSE streaming works correctly
        asyncore_use_poll=True,
        channel_timeout=120,
        connection_limit=1000,
    )


def _serve_debug(app: Any, host: str, port: int) -> None:
    """Serve using Flask's dev server — only for local development."""
    print("  Server: Flask dev server (debug mode — do not use in production)")
    app.run(
        host=host,
        port=port,
        debug=True,
        threaded=True,
        use_reloader=False,  # reloader conflicts with background P2P thread
    )


def main():
    """Main entry point for the Canopy application."""

    parser = argparse.ArgumentParser(description='Canopy - Local Mesh Communication Tool')
    parser.add_argument('--host', default=None,
                        help='Host to bind to (default: 0.0.0.0 for LAN access)')
    parser.add_argument('--port', type=int, default=None,
                        help='Port to bind to (default: 7770, or CANOPY_PORT env var)')
    parser.add_argument('--debug', action='store_true',
                        help='Enable debug mode (uses Flask dev server, not for production)')
    parser.add_argument('--threads', type=int, default=_WAITRESS_THREADS,
                        help=f'Number of waitress worker threads (default: {_WAITRESS_THREADS})')
    parser.add_argument('--config', help='Path to configuration file')

    args = parser.parse_args()

    try:
        config = Config.from_env()

        if args.host is not None:
            config.network.host = args.host
        if args.port is not None:
            config.network.port = args.port
        # Fall back to 7770 if neither --port nor CANOPY_PORT was given
        if args.port is None and not os.getenv('CANOPY_PORT'):
            config.network.port = config.network.port or 7770
        if args.debug:
            config.debug = True

        app = create_app(config)

        data_dir = Path(config.storage.database_path).parent
        data_dir.mkdir(parents=True, exist_ok=True)

        _install_signal_handlers(app)

        print(">> Canopy - Local Mesh Communication Tool")
        print("=" * 50)
        print(f"  Host:     {config.network.host}")
        print(f"  Port:     {config.network.port}")
        print(f"  Debug:    {config.debug}")
        print(f"  Database: {config.storage.database_path}")
        print("=" * 50)
        print(f"  Open: http://localhost:{config.network.port}")
        print("  Press Ctrl+C to stop")
        print()

        if config.debug:
            _serve_debug(app, config.network.host, config.network.port)
        else:
            _serve_production(app, config.network.host, config.network.port, args.threads)

    except KeyboardInterrupt:
        print("\n\nCanopy stopped by user")
        sys.exit(0)

    except Exception as e:
        print(f"\nERROR: Failed to start Canopy: {e}")
        logger.error(f"Startup error: {e}", exc_info=True)
        sys.exit(1)


if __name__ == '__main__':
    main()
