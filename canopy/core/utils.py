"""
Utility functions for Canopy core functionality.

Author: Konrad Walus (architecture, design, and direction)
Project: Canopy - Local Mesh Communication
License: Apache 2.0
Development: AI-assisted implementation (Claude, Codex, GitHub Copilot, Cursor IDE, Ollama)
"""

from typing import Tuple, Optional
from flask import Flask

from .database import DatabaseManager
from .files import FileManager
from .interactions import InteractionManager
from .profile import ProfileManager
from .feed import FeedManager
from ..security.api_keys import ApiKeyManager
from ..security.trust import TrustManager
from .messaging import MessageManager
from .channels import ChannelManager
from .config import Config
from ..network.manager import P2PNetworkManager


def get_app_components(app: Flask) -> Tuple[Optional[DatabaseManager], Optional[ApiKeyManager],
                                                 Optional[TrustManager], Optional[MessageManager],
                                                 Optional[ChannelManager], Optional[FileManager], 
                                                 Optional[FeedManager], Optional[InteractionManager], 
                                                 Optional[ProfileManager], Optional[Config], Optional[P2PNetworkManager]]:
    """Get core application components from Flask app."""
    return (
        app.config.get('DB_MANAGER'),
        app.config.get('API_KEY_MANAGER'),
        app.config.get('TRUST_MANAGER'),
        app.config.get('MESSAGE_MANAGER'),
        app.config.get('CHANNEL_MANAGER'),
        app.config.get('FILE_MANAGER'),
        app.config.get('FEED_MANAGER'),
        app.config.get('INTERACTION_MANAGER'),
        app.config.get('PROFILE_MANAGER'),
        app.config.get('CANOPY_CONFIG'),
        app.config.get('P2P_MANAGER')
    )