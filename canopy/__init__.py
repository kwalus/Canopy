"""
Canopy - Local Mesh Communication Tool

A privacy-first, local-first communication system that enables secure messaging,
file sharing, and voice calls within your local network.

Project: Canopy - Local Mesh Communication
License: Apache 2.0
"""

__version__ = "0.5.38"
__protocol_version__ = 1
__author__ = "Canopy Contributors"
__license__ = "Apache-2.0"

from .core.app import create_app
from .core.config import Config

__all__ = ["create_app", "Config"]
