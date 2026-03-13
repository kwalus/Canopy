"""
Canopy - Local Mesh Communication Tool

A privacy-first, local-first communication system that enables secure messaging,
file sharing, and voice calls within your local network.

Author: Konrad Walus (architecture, design, and direction)
Collaborator: A. Herdzik (QA, design contributions, cross-platform testing)
Project: Canopy - Local Mesh Communication
License: Apache 2.0
Development: AI-assisted implementation (Claude, Codex, GitHub Copilot, Cursor IDE, Ollama)
"""

__version__ = "0.4.80"
__protocol_version__ = 1
__author__ = "Canopy Contributors"
__license__ = "Apache-2.0"

from .core.app import create_app
from .core.config import Config

__all__ = ["create_app", "Config"]
