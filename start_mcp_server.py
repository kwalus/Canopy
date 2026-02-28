#!/usr/bin/env python3
"""
Canopy MCP Server - July 2025 Edition
Optimized for Cursor.ai integration with proper tool definitions and security.
"""

import asyncio
import os
import sys
import logging
from pathlib import Path

# Add the project root to Python path
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))

from canopy.mcp.server import CanopyMCPServer

# Configure logging for MCP server
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(name)s | %(levelname)s | %(message)s',
    handlers=[
        logging.StreamHandler(sys.stderr),  # MCP uses stderr for logging
        logging.FileHandler('logs/mcp_server.log', mode='a')
    ]
)

logger = logging.getLogger(__name__)

async def main():
    """Main entry point for Canopy MCP Server."""
    try:
        # Ensure logs directory exists
        os.makedirs('logs', exist_ok=True)
        
        # Check for required API key
        api_key = os.getenv('CANOPY_API_KEY')
        if not api_key:
            logger.error("Error: CANOPY_API_KEY environment variable is required")
            logger.error("Please create an API key in Canopy UI (http://localhost:7770 → API Keys)")
            logger.error("Then set it as: set CANOPY_API_KEY=your_key_here")
            sys.exit(1)
        
        logger.info("Starting Canopy MCP Server (July 2025 Edition)")
        logger.info(f"Project root: {project_root}")
        logger.info("API Key configured")
        
        # Initialize and run the MCP server
        server = CanopyMCPServer(api_key=api_key)
        
        # For Cursor.ai 2025: Use stdio transport (standard for local MCP servers)
        from mcp.server.stdio import stdio_server
        
        logger.info("Running MCP server with stdio transport for Cursor.ai")
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream)
            
    except KeyboardInterrupt:
        logger.info("Canopy MCP Server stopped by user")
    except BaseException as e:
        # Log full chain (TaskGroup/ExceptionGroup expose .exceptions)
        logger.error(f"Fatal error starting MCP server: {e}")
        if hasattr(e, "exceptions"):
            for i, sub in enumerate(e.exceptions):
                logger.error(f"  Sub-exception {i}: {type(sub).__name__}: {sub}")
        logger.exception("Full traceback:")
        sys.exit(1)

if __name__ == "__main__":
    asyncio.run(main())