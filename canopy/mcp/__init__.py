"""
Canopy MCP Module

Model Context Protocol integration for Canopy, allowing AI agents to interact
with Canopy functionality through standardized MCP tools.

Author: Konrad Walus (architecture, design, and direction)
Project: Canopy - Local Mesh Communication
License: Apache 2.0
Development: AI-assisted implementation (Claude, Codex, GitHub Copilot, Cursor IDE, Ollama)
"""

from typing import Any

CanopyMCPServer: Any

try:
    from .server import CanopyMCPServer as _CanopyMCPServer
    CanopyMCPServer = _CanopyMCPServer
except ImportError:
    # mcp pip package not installed — CanopyMCPServer unavailable but
    # other submodules (e.g. mcp_server_framework) can still be imported.
    CanopyMCPServer = None

__all__ = ['CanopyMCPServer']
__version__ = '1.0.0'
