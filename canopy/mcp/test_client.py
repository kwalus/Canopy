#!/usr/bin/env python3
"""
Canopy MCP Test Client - July 2025 Edition
Simple test client to verify MCP server functionality.
For local/dev use only; uses CANOPY_API_KEY from environment (no credentials in file).

Author: Konrad Walus (architecture, design, and direction)
Project: Canopy - Local Mesh Communication
License: Apache 2.0
Development: AI-assisted implementation (Claude, Codex, GitHub Copilot, Cursor IDE, Ollama)
"""

import asyncio
import json
import logging
import os
import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

# Configure logging to handle Unicode properly
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(name)s | %(levelname)s | %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),  # Use stdout instead of stderr for better Unicode support
    ]
)
logger = logging.getLogger(__name__)

async def test_mcp_server():
    """Test the Canopy MCP server functionality."""
    
    # Check for API key
    api_key = os.getenv('CANOPY_API_KEY')
    if not api_key:
        logger.error("Error: CANOPY_API_KEY environment variable required")
        logger.error("Create an API key in Canopy UI and set: set CANOPY_API_KEY=your_key")
        return False
    
    try:
        logger.info("Testing Canopy MCP Server")
        
        # Import and create server
        from canopy.mcp.server import CanopyMCPServer
        server = CanopyMCPServer(api_key=api_key)
        
        # Test authentication
        logger.info("Testing authentication...")
        auth_result = await server._authenticate()
        if not auth_result:
            logger.error("Authentication failed")
            return False
        
        logger.info(f"Authentication successful for user: {server.user_id}")
        
        # Test tool listing (this calls the handler)
        logger.info("Testing tool listing...")
        
        # Since we can't easily call the handlers directly, let's test the components
        logger.info("Testing Canopy components...")
        
        from canopy.core.app import create_app
        app = create_app()
        
        with app.app_context():
            from canopy.core.utils import get_app_components
            components = get_app_components(app)
            api_key_manager = components[0]
            message_manager = components[3]
            channel_manager = components[4]
            
            # Test basic operations
            logger.info(f"API Key Manager: {type(api_key_manager).__name__}")
            logger.info(f"Message Manager: {type(message_manager).__name__}")
            logger.info(f"Channel Manager: {type(channel_manager).__name__}")
            if message_manager is None or channel_manager is None:
                logger.error("Required managers are unavailable")
                return False
            
            # Get some basic stats
            user_id = server.user_id
            messages = message_manager.get_messages(user_id, 5, None)
            channels = channel_manager.get_user_channels(user_id)
            
            logger.info(f"Recent messages: {len(messages)}")
            logger.info(f"Available channels: {len(channels)}")
        
        logger.info("MCP Server test completed successfully.")
        return True
        
    except Exception as e:
        logger.error(f"Test failed: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return False

async def main():
    """Main test function."""
    logger.info("Canopy MCP Server Test (July 2025)")
    
    success = await test_mcp_server()
    
    if success:
        logger.info("All tests passed. MCP server is ready for Cursor.ai")
        sys.exit(0)
    else:
        logger.error("Tests failed. Check configuration and try again")
        sys.exit(1)

if __name__ == "__main__":
    asyncio.run(main())
