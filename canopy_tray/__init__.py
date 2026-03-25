"""
Canopy Tray - Windows system tray application for Canopy.

Manages the Canopy server lifecycle, shows connection status,
and sends desktop notifications for new messages.

Project: Canopy - Local Mesh Communication
License: Apache 2.0
"""

from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
import re

try:
    __version__ = version("canopy")
except PackageNotFoundError:
    init_path = Path(__file__).resolve().parents[1] / "canopy" / "__init__.py"
    match = re.search(r'__version__\s*=\s*"([^"]+)"', init_path.read_text(encoding="utf-8"))
    __version__ = match.group(1) if match else "0.0.0"
