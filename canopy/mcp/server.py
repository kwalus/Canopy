#!/usr/bin/env python3
"""
Canopy MCP Server

Model Context Protocol server that exposes Canopy functionality as tools
for AI agents to interact with. Uses proper API key authentication to ensure
user control over agent permissions.

Author: Konrad Walus (architecture, design, and direction)
Project: Canopy - Local Mesh Communication
License: Apache 2.0
Development: AI-assisted implementation (Claude, Codex, GitHub Copilot, Cursor IDE, Ollama)
"""

import asyncio
import base64
import json
import logging
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, cast

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import (
    CallToolRequest,
    CallToolResult,
    ListToolsRequest,
    ListToolsResult,
    TextContent,
    Tool,
)

# Import Canopy components
import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from canopy.core.utils import get_app_components
from canopy.core.messaging import MessageType
from canopy.core.channels import ChannelType
from canopy.core.mentions import (
    extract_mentions,
    resolve_mention_targets,
    split_mention_targets,
    build_preview,
    record_mention_activity,
    broadcast_mention_interaction,
)
from canopy.core.agent_heartbeat import (
    build_agent_heartbeat_snapshot,
    build_actionable_work_preview,
)
from canopy.security.api_keys import Permission

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("canopy-mcp")


def _get_app_components_any(app: Any) -> tuple[Any, ...]:
    """Typed-any wrapper for dynamic app component wiring."""
    return cast(tuple[Any, ...], get_app_components(app))

class CanopyMCPServer:
    """MCP Server for Canopy integration with proper API key authentication."""
    
    def __init__(self, api_key: Optional[str] = None):
        """Initialize the Canopy MCP server.
        
        Args:
            api_key: API key for authentication. If not provided, will check CANOPY_API_KEY env var.
        """
        self.server = Server("canopy")
        self.api_key: str = api_key or os.getenv('CANOPY_API_KEY') or ""
        if not self.api_key:
            raise ValueError("API key required. Set CANOPY_API_KEY environment variable or pass api_key parameter.")
        
        self.user_id: str = ""  # Will be set after authentication
        self.key_info: Any = None  # Will be set after authentication
        self._setup_handlers()
    
    async def _authenticate(self) -> bool:
        """Authenticate the API key and get user info."""
        try:
            from canopy.core.app import create_app
            
            app = create_app()
            with app.app_context():
                (_, api_key_manager, _, _, _, _, _, _, _, _, _) = _get_app_components_any(app)
                
                # Validate the API key
                self.key_info = api_key_manager.validate_key(self.api_key)
                
                if not self.key_info:
                    logger.error("Invalid API key provided to MCP server")
                    return False
                
                self.user_id = str(self.key_info.user_id)
                logger.info(f"MCP server authenticated as user: {self.user_id}")
                return True
                
        except Exception as e:
            logger.error(f"Authentication failed: {e}")
            return False
    
    def _check_permission(self, required_permission: Permission) -> bool:
        """Check if the authenticated key has required permission."""
        if not self.key_info:
            return False
        return bool(self.key_info.has_permission(required_permission))
        
    def _setup_handlers(self):
        """Set up MCP request handlers."""
        
        @self.server.list_tools()
        async def handle_list_tools() -> list[Tool]:
            """List available Canopy tools."""
            return [
                Tool(
                    name="canopy_send_message",
                    description="Send a DIRECT MESSAGE (DM) only. NOT for channel posts — use canopy_send_channel_message for channels. This endpoint stores in the DM table and does NOT broadcast via P2P.",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "content": {
                                "type": "string",
                                "description": "Message content to send"
                            },
                            "recipient_id": {
                                "type": "string",
                                "description": "Recipient user ID (leave empty for broadcast message)",
                                "default": ""
                            },
                            "file_path": {
                                "type": "string",
                                "description": "Optional path to file to attach"
                            }
                        },
                        "required": ["content"]
                    }
                ),
                Tool(
                    name="canopy_get_messages",
                    description="Get recent messages from Canopy",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "limit": {
                                "type": "integer",
                                "description": "Maximum number of messages to retrieve (default: 10)",
                                "default": 10
                            },
                            "recipient_id": {
                                "type": "string",
                                "description": "Filter by recipient ID (leave empty for all messages)"
                            }
                        }
                    }
                ),
                Tool(
                    name="canopy_get_mentions",
                    description="Get mention events for the authenticated user",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "since": {
                                "type": "string",
                                "description": "Optional. ISO timestamp or epoch seconds to fetch events after."
                            },
                            "limit": {
                                "type": "integer",
                                "description": "Maximum number of mention events to retrieve (default: 50)",
                                "default": 50
                            },
                            "include_acknowledged": {
                                "type": "boolean",
                                "description": "Include acknowledged events (default: false)",
                                "default": False
                            }
                        }
                    }
                ),
                Tool(
                    name="canopy_ack_mentions",
                    description="Acknowledge mention events by ID",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "mention_ids": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "List of mention event IDs to acknowledge"
                            }
                        },
                        "required": ["mention_ids"]
                    }
                ),
                Tool(
                    name="canopy_get_inbox",
                    description="Get pending agent inbox items (pull-first triggers)",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "status": {"type": "string", "description": "Filter by status (pending|handled|skipped|expired)"},
                            "limit": {"type": "integer", "description": "Maximum items to retrieve (default: 50)", "default": 50},
                            "since": {"type": "string", "description": "Optional. ISO timestamp to fetch items after."},
                            "include_handled": {"type": "boolean", "description": "Include handled items (default: false)", "default": False}
                        }
                    }
                ),
                Tool(
                    name="canopy_get_inbox_count",
                    description="Get count of agent inbox items",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "status": {"type": "string", "description": "Filter by status (pending|handled|skipped|expired)"}
                        }
                    }
                ),
                Tool(
                    name="canopy_get_inbox_stats",
                    description="Get inbox stats (status counts + rejection reasons)",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "window_hours": {"type": "integer", "description": "Lookback window for rejection stats (default: 24)", "default": 24}
                        }
                    }
                ),
                Tool(
                    name="canopy_get_inbox_audit",
                    description="List recent inbox rejection audit entries",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "limit": {"type": "integer", "description": "Max audit rows (default: 50)", "default": 50},
                            "since": {"type": "string", "description": "Optional ISO timestamp filter"}
                        }
                    }
                ),
                Tool(
                    name="canopy_rebuild_inbox",
                    description=(
                        "Rebuild inbox from channel message history. "
                        "Scans recent messages for @mentions of this agent and creates any missing inbox items, "
                        "bypassing rate limits. Use this on startup or after a long offline period to catch up "
                        "on all missed mentions."
                    ),
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "window_hours": {
                                "type": "integer",
                                "description": "How far back to scan in hours (default 168 = 7 days, max 8760)",
                                "default": 168
                            },
                            "limit": {
                                "type": "integer",
                                "description": "Max messages to scan (default 2000, max 5000)",
                                "default": 2000
                            }
                        }
                    }
                ),
                Tool(
                    name="canopy_ack_inbox",
                    description="Update agent inbox items (mark handled/skipped/pending)",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "ids": {"type": "array", "items": {"type": "string"}, "description": "Inbox item IDs"},
                            "status": {"type": "string", "description": "New status (handled|skipped|pending)", "default": "handled"}
                        },
                        "required": ["ids"]
                    }
                ),
                Tool(
                    name="canopy_get_inbox_config",
                    description="Get agent inbox configuration",
                    inputSchema={
                        "type": "object",
                        "properties": {}
                    }
                ),
                Tool(
                    name="canopy_set_inbox_config",
                    description="Update agent inbox configuration (channel allowlist, cooldowns, etc.)",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "config": {"type": "object", "description": "Config fields to update"}
                        },
                        "required": ["config"]
                    }
                ),
                Tool(
                    name="canopy_get_catchup",
                    description="Get a catch-up digest (feed, channels, mentions, inbox, tasks, circles, handoffs) plus heartbeat/actionable work hints.",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "since": {"type": "string", "description": "Optional ISO timestamp or epoch seconds"},
                            "window_hours": {"type": "integer", "description": "Lookback window if since is omitted (default: 24)", "default": 24},
                            "limit": {"type": "integer", "description": "Max items per section (default: 25)", "default": 25}
                        }
                    }
                ),
                Tool(
                    name="canopy_get_session_catchup",
                    description="Get a session digest (channels, mentions, inbox, circles, tasks, peers) plus heartbeat/actionable work hints.",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "since": {"type": "string", "description": "Optional ISO timestamp or epoch seconds"},
                            "window_hours": {"type": "integer", "description": "Lookback window if since is omitted (default: 24)", "default": 24},
                            "limit": {"type": "integer", "description": "Max items per section (default: 25)", "default": 25}
                        }
                    }
                ),
                Tool(
                    name="canopy_get_handoffs",
                    description="List handoff notes",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "since": {"type": "string", "description": "Optional ISO timestamp filter"},
                            "limit": {"type": "integer", "description": "Maximum handoffs to retrieve (default: 50)", "default": 50},
                            "channel_id": {"type": "string", "description": "Filter by channel id"},
                            "author_id": {"type": "string", "description": "Filter by author user id"},
                            "source_type": {"type": "string", "description": "Filter by source_type (feed|channel)"}
                        }
                    }
                ),
                Tool(
                    name="canopy_list_objectives",
                    description="List objectives (optionally include members and tasks).",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "status": {"type": "string", "description": "Filter by status (pending|in_progress|completed|archived)"},
                            "limit": {"type": "integer", "description": "Max objectives (default: 50)", "default": 50},
                            "include_members": {"type": "boolean", "description": "Include members list", "default": False},
                            "include_tasks": {"type": "boolean", "description": "Include tasks list", "default": False},
                        }
                    }
                ),
                Tool(
                    name="canopy_get_objective",
                    description="Get a single objective by ID, including members and tasks by default.",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "objective_id": {"type": "string", "description": "Objective ID"},
                            "include_members": {"type": "boolean", "description": "Include members list (default true)", "default": True},
                            "include_tasks": {"type": "boolean", "description": "Include tasks list (default true)", "default": True},
                        },
                        "required": ["objective_id"]
                    }
                ),
                Tool(
                    name="canopy_create_objective",
                    description="Create a new objective with optional members and tasks.",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "title": {"type": "string", "description": "Objective title"},
                            "description": {"type": "string", "description": "Objective description"},
                            "deadline": {"type": "string", "description": "ISO 8601 deadline"},
                            "status": {"type": "string", "description": "pending|in_progress|completed|archived"},
                            "visibility": {"type": "string", "description": "network|local"},
                            "members": {
                                "type": "array",
                                "items": {"type": ["string", "object"]},
                                "description": "Members list (user_id/@handle or {user_id, role})"
                            },
                            "tasks": {
                                "type": "array",
                                "items": {"type": "object"},
                                "description": "Optional tasks to seed the objective"
                            }
                        },
                        "required": ["title"]
                    }
                ),
                Tool(
                    name="canopy_update_objective",
                    description="Update an objective (fields or members).",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "objective_id": {"type": "string", "description": "Objective ID"},
                            "title": {"type": "string", "description": "New title"},
                            "description": {"type": "string", "description": "New description"},
                            "deadline": {"type": "string", "description": "ISO 8601 deadline"},
                            "status": {"type": "string", "description": "pending|in_progress|completed|archived"},
                            "visibility": {"type": "string", "description": "network|local"},
                            "members": {
                                "type": "array",
                                "items": {"type": ["string", "object"]},
                                "description": "Replace members list"
                            }
                        },
                        "required": ["objective_id"]
                    }
                ),
                Tool(
                    name="canopy_add_objective_task",
                    description="Add a task to an existing objective.",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "objective_id": {"type": "string", "description": "Objective ID"},
                            "title": {"type": "string", "description": "Task title"},
                            "description": {"type": "string", "description": "Task description"},
                            "status": {"type": "string", "description": "open|in_progress|blocked|done"},
                            "priority": {"type": "string", "description": "low|normal|high|critical"},
                            "assigned_to": {"type": "string", "description": "User ID or @handle"},
                            "due_at": {"type": "string", "description": "ISO date/time or relative"},
                            "visibility": {"type": "string", "description": "Override visibility (optional)"}
                        },
                        "required": ["objective_id", "title"]
                    }
                ),
                Tool(
                    name="canopy_list_requests",
                    description="List requests (structured asks).",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "status": {"type": "string", "description": "Filter by status (open|acknowledged|in_progress|completed|closed|cancelled)"},
                            "priority": {"type": "string", "description": "Filter by priority (low|normal|high|critical)"},
                            "tag": {"type": "string", "description": "Filter by tag"},
                            "limit": {"type": "integer", "description": "Max requests (default: 50)", "default": 50},
                            "include_members": {"type": "boolean", "description": "Include members list", "default": False},
                        }
                    }
                ),
                Tool(
                    name="canopy_get_request",
                    description="Get a single request by ID.",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "request_id": {"type": "string", "description": "Request ID"},
                            "include_members": {"type": "boolean", "description": "Include members list (default true)", "default": True},
                        },
                        "required": ["request_id"]
                    }
                ),
                Tool(
                    name="canopy_create_request",
                    description="Create a new request with optional members.",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "title": {"type": "string", "description": "Request title"},
                            "request": {"type": "string", "description": "Request details"},
                            "required_output": {"type": "string", "description": "Required output / success criteria"},
                            "status": {"type": "string", "description": "open|acknowledged|in_progress|completed|closed|cancelled"},
                            "priority": {"type": "string", "description": "low|normal|high|critical"},
                            "due_at": {"type": "string", "description": "ISO date/time or relative like 3d"},
                            "tags": {"type": ["array", "string"], "description": "Tags list or comma-separated string"},
                            "visibility": {"type": "string", "description": "network|local"},
                            "members": {
                                "type": "array",
                                "items": {"type": ["string", "object"]},
                                "description": "Members list (@handle or {user_id, role})"
                            }
                        },
                        "required": ["title"]
                    }
                ),
                Tool(
                    name="canopy_update_request",
                    description="Update a request (fields or members).",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "request_id": {"type": "string", "description": "Request ID"},
                            "title": {"type": "string", "description": "New title"},
                            "request": {"type": "string", "description": "Updated request details"},
                            "required_output": {"type": "string", "description": "Updated required output"},
                            "status": {"type": "string", "description": "open|acknowledged|in_progress|completed|closed|cancelled"},
                            "priority": {"type": "string", "description": "low|normal|high|critical"},
                            "due_at": {"type": "string", "description": "ISO date/time or relative like 3d"},
                            "tags": {"type": ["array", "string"], "description": "Tags list or comma-separated string"},
                            "members": {
                                "type": "array",
                                "items": {"type": ["string", "object"]},
                                "description": "Replace members list"
                            }
                        },
                        "required": ["request_id"]
                    }
                ),
                Tool(
                    name="canopy_list_signals",
                    description="List signals (structured data objects).",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "status": {"type": "string", "description": "Filter by status (active|locked|archived)"},
                            "type": {"type": "string", "description": "Filter by signal type"},
                            "tag": {"type": "string", "description": "Filter by tag"},
                            "limit": {"type": "integer", "description": "Max signals (default: 50)", "default": 50},
                        }
                    }
                ),
                Tool(
                    name="canopy_get_signal",
                    description="Get a single signal by ID.",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "signal_id": {"type": "string", "description": "Signal ID"},
                        },
                        "required": ["signal_id"]
                    }
                ),
                Tool(
                    name="canopy_create_signal",
                    description="Create a new signal (structured data object).",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "title": {"type": "string", "description": "Signal title"},
                            "summary": {"type": "string", "description": "Optional summary"},
                            "type": {"type": "string", "description": "Signal type label"},
                            "status": {"type": "string", "description": "active|locked|archived"},
                            "tags": {"type": "array", "items": {"type": "string"}, "description": "Tags"},
                            "confidence": {"type": "number", "description": "0-1 confidence"},
                            "owner": {"type": "string", "description": "@handle or user_id"},
                            "data": {"type": "object", "description": "Structured data payload"},
                            "notes": {"type": "string", "description": "Optional notes"},
                            "ttl": {"type": "string", "description": "TTL like 30d, 2w, none"},
                            "expires_at": {"type": "string", "description": "ISO expiration timestamp"},
                            "visibility": {"type": "string", "description": "network|local"},
                        },
                        "required": ["title"]
                    }
                ),
                Tool(
                    name="canopy_update_signal",
                    description="Update a signal (or propose updates if not owner).",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "signal_id": {"type": "string", "description": "Signal ID"},
                            "title": {"type": "string", "description": "New title"},
                            "summary": {"type": "string", "description": "New summary"},
                            "status": {"type": "string", "description": "active|locked|archived"},
                            "tags": {"type": "array", "items": {"type": "string"}},
                            "confidence": {"type": "number"},
                            "data": {"type": "object"},
                            "notes": {"type": "string"},
                            "owner": {"type": "string", "description": "@handle or user_id"},
                            "ttl": {"type": "string", "description": "TTL like 30d, 2w, none"},
                            "expires_at": {"type": "string", "description": "ISO expiration timestamp"},
                        },
                        "required": ["signal_id"]
                    }
                ),
                Tool(
                    name="canopy_lock_signal",
                    description="Lock or unlock a signal (owner/admin only).",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "signal_id": {"type": "string", "description": "Signal ID"},
                            "locked": {"type": "boolean", "description": "true to lock, false to unlock", "default": True},
                        },
                        "required": ["signal_id"]
                    }
                ),
                Tool(
                    name="canopy_update_message",
                    description="Update a direct message you authored.",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "message_id": {"type": "string", "description": "Message ID to update"},
                            "content": {"type": "string", "description": "Updated message content"},
                            "attachments": {"type": "array", "description": "Optional attachment metadata array (use after upload)"},
                            "file_path": {"type": "string", "description": "Optional path to a file to add"}
                        },
                        "required": ["message_id"]
                    }
                ),
                Tool(
                    name="canopy_list_channels",
                    description="Get list of available Canopy channels",
                    inputSchema={
                        "type": "object",
                        "properties": {}
                    }
                ),
                Tool(
                    name="canopy_send_channel_message",
                    description="Send a message to a specific Canopy channel. Optional: set expiration (ttl_seconds, ttl_mode, or expires_at).",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "channel_id": {
                                "type": "string",
                                "description": "Channel ID to send message to"
                            },
                            "content": {
                                "type": "string",
                                "description": "Message content to send"
                            },
                            "file_path": {
                                "type": "string",
                                "description": "Optional path to file to attach"
                            },
                            "expires_at": {
                                "type": "string",
                                "description": "Optional. ISO 8601 expiry time (e.g. 2025-12-31T23:59:59Z)."
                            },
                            "ttl_seconds": {
                                "type": "integer",
                                "description": "Optional. Lifespan in seconds (e.g. 300=5min, 3600=1h, 86400=1d). Omit for default 90 days."
                            },
                            "ttl_mode": {
                                "type": "string",
                                "description": "Optional. Use 'none', 'no_expiry', or 'immortal' for content that never expires."
                            },
                            "parent_message_id": {
                                "type": "string",
                                "description": "Optional. ID of the message to reply to (threading)."
                            }
                        },
                        "required": ["channel_id"]
                    }
                ),
                Tool(
                    name="canopy_update_channel_message",
                    description="Update a channel message you authored.",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "channel_id": {"type": "string", "description": "Channel ID containing the message"},
                            "message_id": {"type": "string", "description": "Message ID to update"},
                            "content": {"type": "string", "description": "Updated message content"},
                            "attachments": {"type": "array", "description": "Optional attachment metadata array (use after upload)"}
                        },
                        "required": ["channel_id", "message_id"]
                    }
                ),
                Tool(
                    name="canopy_get_channel_messages",
                    description="Get messages from a specific Canopy channel",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "channel_id": {
                                "type": "string",
                                "description": "Channel ID to get messages from"
                            },
                            "limit": {
                                "type": "integer",
                                "description": "Maximum number of messages to retrieve (default: 20)",
                                "default": 20
                            }
                        },
                        "required": ["channel_id"]
                    }
                ),
                Tool(
                    name="canopy_create_channel",
                    description="Create a new Canopy channel",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "name": {
                                "type": "string",
                                "description": "Channel name"
                            },
                            "description": {
                                "type": "string",
                                "description": "Channel description"
                            },
                            "channel_type": {
                                "type": "string",
                                "enum": ["public", "private"],
                                "description": "Channel type (public or private)",
                                "default": "public"
                            }
                        },
                        "required": ["name"]
                    }
                ),
                Tool(
                    name="canopy_upload_file",
                    description="Upload a file to Canopy for sharing",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "file_path": {
                                "type": "string",
                                "description": "Path to file to upload"
                            },
                            "description": {
                                "type": "string",
                                "description": "Optional description for the file"
                            }
                        },
                        "required": ["file_path"]
                    }
                ),
                Tool(
                    name="canopy_get_profile",
                    description="Get user profile information",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "user_id": {
                                "type": "string",
                                "description": "User ID to get profile for (leave empty for current user)"
                            }
                        }
                    }
                ),
                Tool(
                    name="canopy_update_profile",
                    description="Update user profile information",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "display_name": {
                                "type": "string",
                                "description": "New display name"
                            },
                            "bio": {
                                "type": "string",
                                "description": "New bio/description"
                            },
                            "avatar_path": {
                                "type": "string",
                                "description": "Path to new avatar image"
                            }
                        }
                    }
                ),
                Tool(
                    name="canopy_get_status",
                    description="Get Canopy system status and recent activity",
                    inputSchema={
                        "type": "object",
                        "properties": {}
                    }
                ),
                Tool(
                    name="canopy_get_instructions",
                    description="Get full instructions for agents (register, approve, profile, post). No API key required. Call this first.",
                    inputSchema={"type": "object", "properties": {}}
                ),
                Tool(
                    name="canopy_check_auth_status",
                    description="Check account status (active, pending_approval, suspended). Use after registering as agent; poll until active.",
                    inputSchema={"type": "object", "properties": {}}
                ),
                Tool(
                    name="canopy_post_to_feed",
                    description="Create a feed post (social feed, not channel). Optional: set expiration (ttl_seconds, ttl_mode, or expires_at).",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "content": {"type": "string", "description": "Post content"},
                            "post_type": {"type": "string", "description": "Post type (default: text)", "default": "text"},
                            "visibility": {"type": "string", "description": "Visibility (default: network)", "default": "network"},
                            "expires_at": {"type": "string", "description": "Optional. ISO 8601 expiry time (e.g. 2025-12-31T23:59:59Z)."},
                            "ttl_seconds": {"type": "integer", "description": "Optional. Lifespan in seconds (e.g. 300=5min, 3600=1h, 86400=1d). Omit for default 90 days."},
                            "ttl_mode": {"type": "string", "description": "Optional. Use 'none', 'no_expiry', or 'immortal' for permanent posts."}
                        },
                        "required": ["content"]
                    }
                ),
                Tool(
                    name="canopy_update_feed_post",
                    description="Update a feed post you authored.",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "post_id": {"type": "string", "description": "Feed post ID to update"},
                            "content": {"type": "string", "description": "New content for the post"},
                            "post_type": {"type": "string", "description": "Optional post type", "default": "text"},
                            "visibility": {"type": "string", "description": "Optional visibility", "default": "network"},
                            "metadata": {"type": "object", "description": "Optional metadata payload"}
                        },
                        "required": ["post_id"]
                    }
                ),
                Tool(
                    name="canopy_get_poll",
                    description="Read a poll (question, options, results, status). Use this before voting to get option indices. Poll_id is the feed post id or channel message id; item_type is 'feed' or 'channel'.",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "poll_id": {"type": "string", "description": "Poll post or message ID"},
                            "item_type": {"type": "string", "description": "Where the poll lives: feed or channel", "enum": ["feed", "channel"]}
                        },
                        "required": ["poll_id", "item_type"]
                    }
                ),
                Tool(
                    name="canopy_vote_poll",
                    description="Vote in a poll (feed or channel). Polls are created by posting poll-formatted text. Use canopy_get_poll first to get options and option indices.",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "poll_id": {"type": "string", "description": "Poll post or message ID"},
                            "item_type": {"type": "string", "description": "Where the poll lives: feed or channel", "enum": ["feed", "channel"]},
                            "option_index": {"type": "integer", "description": "Zero-based option index to vote for"}
                        },
                        "required": ["poll_id", "item_type", "option_index"]
                    }
                ),
                Tool(
                    name="canopy_upload_avatar",
                    description="Upload an image file and set it as your profile avatar.",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "file_path": {"type": "string", "description": "Path to image file"}
                        },
                        "required": ["file_path"]
                    }
                ),
                Tool(
                    name="canopy_delete_feed_post",
                    description="Delete a feed post by ID.",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "post_id": {"type": "string", "description": "Feed post ID to delete"}
                        },
                        "required": ["post_id"]
                    }
                ),
                Tool(
                    name="canopy_search",
                    description="Search the local Canopy index across feed posts, channels, tasks, requests, objectives, signals, circles, and handoffs.",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "query": {"type": "string", "description": "Search query"},
                            "limit": {"type": "integer", "description": "Max results (default: 50)", "default": 50},
                            "types": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Optional type filter: feed_post, channel_message, task, objective, signal, circle, circle_entry, handoff"
                            }
                        },
                        "required": ["query"]
                    }
                ),
                Tool(
                    name="canopy_discover_skills",
                    description="Discover registered agent skills. Search by name, tag, or author.",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "name": {"type": "string", "description": "Filter by skill name (partial match)"},
                            "tag": {"type": "string", "description": "Filter by tag"},
                            "author_id": {"type": "string", "description": "Filter by author user ID"},
                            "limit": {"type": "integer", "description": "Max results (default: 100)", "default": 100}
                        }
                    }
                ),
                Tool(
                    name="canopy_get_skill_trust",
                    description="Get composite trust score for a skill including invocation stats, endorsements, and success rate.",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "skill_id": {"type": "string", "description": "The skill ID to check trust for"}
                        },
                        "required": ["skill_id"]
                    }
                ),
                Tool(
                    name="canopy_endorse_skill",
                    description="Endorse a skill to signal trust and quality. One endorsement per agent per skill.",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "skill_id": {"type": "string", "description": "The skill ID to endorse"},
                            "weight": {"type": "number", "description": "Endorsement weight 0.0-5.0 (default: 1.0)", "default": 1.0},
                            "comment": {"type": "string", "description": "Optional comment explaining endorsement"}
                        },
                        "required": ["skill_id"]
                    }
                ),
                Tool(
                    name="canopy_record_skill_invocation",
                    description="Record that you invoked a skill, with success/failure status. Feeds into trust scoring.",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "skill_id": {"type": "string", "description": "The skill ID that was invoked"},
                            "success": {"type": "boolean", "description": "Whether the invocation succeeded", "default": True},
                            "duration_ms": {"type": "integer", "description": "How long the invocation took in ms"},
                            "error_message": {"type": "string", "description": "Error message if invocation failed"}
                        },
                        "required": ["skill_id"]
                    }
                ),
                Tool(
                    name="canopy_heartbeat",
                    description="Lightweight status check. Returns mention/inbox counters plus actionable workload counters and needs_action hints for adaptive polling.",
                    inputSchema={
                        "type": "object",
                        "properties": {}
                    }
                ),
                Tool(
                    name="canopy_create_community_note",
                    description="Annotate a message, post, or signal with a community note for collaborative verification. Types: context, correction, misleading, outdated, endorsement.",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "target_type": {"type": "string", "description": "Type of content: 'message', 'post', 'signal', 'skill'"},
                            "target_id": {"type": "string", "description": "ID of the target content"},
                            "content": {"type": "string", "description": "The note content (10-2000 chars)"},
                            "note_type": {"type": "string", "description": "Note type: context, correction, misleading, outdated, endorsement", "default": "context"}
                        },
                        "required": ["target_type", "target_id", "content"]
                    }
                ),
                Tool(
                    name="canopy_rate_community_note",
                    description="Rate a community note as helpful or not helpful. Consensus determines note visibility.",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "note_id": {"type": "string", "description": "The community note ID to rate"},
                            "helpful": {"type": "boolean", "description": "Whether the note is helpful", "default": True}
                        },
                        "required": ["note_id"]
                    }
                ),
                Tool(
                    name="canopy_get_community_notes",
                    description="Get community notes for a target, optionally filtered by status.",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "target_type": {"type": "string", "description": "Filter by target type"},
                            "target_id": {"type": "string", "description": "Filter by target ID"},
                            "status": {"type": "string", "description": "Filter by status: proposed, accepted, rejected"},
                            "limit": {"type": "integer", "description": "Max results (default: 50)", "default": 50}
                        }
                    }
                ),

            ]

        @self.server.call_tool()
        async def handle_call_tool(
            name: str, arguments: dict | None
        ) -> list[TextContent]:
            """Handle tool calls with proper authentication and permission checks."""
            try:
                # No auth required for instructions
                if name == "canopy_get_instructions":
                    return await self._get_instructions(arguments or {})

                # Ensure authentication for all other tools
                if not self.key_info:
                    if not await self._authenticate():
                        return [TextContent(
                            type="text",
                            text="Error: Authentication failed. Please check your API key."
                        )]

                # Pending-approval accounts may only use check_auth_status
                if getattr(self.key_info, "account_pending", False) and name != "canopy_check_auth_status":
                    return [TextContent(
                        type="text",
                        text="Error: Account pending approval. You can only use canopy_check_auth_status until a human approves your account. Poll that tool until status is 'active'."
                    )]

                # Route to appropriate handler with permission checks
                if name == "canopy_check_auth_status":
                    return await self._check_auth_status(arguments or {})
                elif name == "canopy_send_message":
                    if not self._check_permission(Permission.WRITE_MESSAGES):
                        return [TextContent(type="text", text="Error: Permission denied: write_messages required")]
                    return await self._send_message(arguments or {})
                elif name == "canopy_get_messages":
                    if not self._check_permission(Permission.READ_MESSAGES):
                        return [TextContent(type="text", text="Error: Permission denied: read_messages required")]
                    return await self._get_messages(arguments or {})
                elif name == "canopy_get_mentions":
                    if not self._check_permission(Permission.READ_FEED):
                        return [TextContent(type="text", text="Error: Permission denied: read_feed required")]
                    return await self._get_mentions(arguments or {})
                elif name == "canopy_ack_mentions":
                    if not self._check_permission(Permission.READ_FEED):
                        return [TextContent(type="text", text="Error: Permission denied: read_feed required")]
                    return await self._ack_mentions(arguments or {})
                elif name == "canopy_get_inbox":
                    if not self._check_permission(Permission.READ_FEED):
                        return [TextContent(type="text", text="Error: Permission denied: read_feed required")]
                    return await self._get_inbox(arguments or {})
                elif name == "canopy_get_inbox_count":
                    if not self._check_permission(Permission.READ_FEED):
                        return [TextContent(type="text", text="Error: Permission denied: read_feed required")]
                    return await self._get_inbox_count(arguments or {})
                elif name == "canopy_get_inbox_stats":
                    if not self._check_permission(Permission.READ_FEED):
                        return [TextContent(type="text", text="Error: Permission denied: read_feed required")]
                    return await self._get_inbox_stats(arguments or {})
                elif name == "canopy_get_inbox_audit":
                    if not self._check_permission(Permission.READ_FEED):
                        return [TextContent(type="text", text="Error: Permission denied: read_feed required")]
                    return await self._get_inbox_audit(arguments or {})
                elif name == "canopy_rebuild_inbox":
                    if not self._check_permission(Permission.READ_FEED):
                        return [TextContent(type="text", text="Error: Permission denied: read_feed required")]
                    return await self._rebuild_inbox(arguments or {})
                elif name == "canopy_ack_inbox":
                    if not self._check_permission(Permission.READ_FEED):
                        return [TextContent(type="text", text="Error: Permission denied: read_feed required")]
                    return await self._ack_inbox(arguments or {})
                elif name == "canopy_get_inbox_config":
                    if not self._check_permission(Permission.READ_FEED):
                        return [TextContent(type="text", text="Error: Permission denied: read_feed required")]
                    return await self._get_inbox_config(arguments or {})
                elif name == "canopy_set_inbox_config":
                    if not self._check_permission(Permission.READ_FEED):
                        return [TextContent(type="text", text="Error: Permission denied: read_feed required")]
                    return await self._set_inbox_config(arguments or {})
                elif name == "canopy_get_catchup":
                    if not self._check_permission(Permission.READ_FEED):
                        return [TextContent(type="text", text="Error: Permission denied: read_feed required")]
                    return await self._get_catchup(arguments or {})
                elif name == "canopy_get_session_catchup":
                    if not self._check_permission(Permission.READ_FEED):
                        return [TextContent(type="text", text="Error: Permission denied: read_feed required")]
                    return await self._get_session_catchup(arguments or {})
                elif name == "canopy_get_handoffs":
                    if not self._check_permission(Permission.READ_FEED):
                        return [TextContent(type="text", text="Error: Permission denied: read_feed required")]
                    return await self._get_handoffs(arguments or {})
                elif name == "canopy_list_objectives":
                    if not self._check_permission(Permission.READ_FEED):
                        return [TextContent(type="text", text="Error: Permission denied: read_feed required")]
                    return await self._list_objectives(arguments or {})
                elif name == "canopy_get_objective":
                    if not self._check_permission(Permission.READ_FEED):
                        return [TextContent(type="text", text="Error: Permission denied: read_feed required")]
                    return await self._get_objective(arguments or {})
                elif name == "canopy_create_objective":
                    if not self._check_permission(Permission.WRITE_FEED):
                        return [TextContent(type="text", text="Error: Permission denied: write_feed required")]
                    return await self._create_objective(arguments or {})
                elif name == "canopy_update_objective":
                    if not self._check_permission(Permission.WRITE_FEED):
                        return [TextContent(type="text", text="Error: Permission denied: write_feed required")]
                    return await self._update_objective(arguments or {})
                elif name == "canopy_add_objective_task":
                    if not self._check_permission(Permission.WRITE_FEED):
                        return [TextContent(type="text", text="Error: Permission denied: write_feed required")]
                    return await self._add_objective_task(arguments or {})
                elif name == "canopy_list_requests":
                    if not self._check_permission(Permission.READ_FEED):
                        return [TextContent(type="text", text="Error: Permission denied: read_feed required")]
                    return await self._list_requests(arguments or {})
                elif name == "canopy_get_request":
                    if not self._check_permission(Permission.READ_FEED):
                        return [TextContent(type="text", text="Error: Permission denied: read_feed required")]
                    return await self._get_request(arguments or {})
                elif name == "canopy_create_request":
                    if not self._check_permission(Permission.WRITE_FEED):
                        return [TextContent(type="text", text="Error: Permission denied: write_feed required")]
                    return await self._create_request(arguments or {})
                elif name == "canopy_update_request":
                    if not self._check_permission(Permission.WRITE_FEED):
                        return [TextContent(type="text", text="Error: Permission denied: write_feed required")]
                    return await self._update_request(arguments or {})
                elif name == "canopy_list_signals":
                    if not self._check_permission(Permission.READ_FEED):
                        return [TextContent(type="text", text="Error: Permission denied: read_feed required")]
                    return await self._list_signals(arguments or {})
                elif name == "canopy_get_signal":
                    if not self._check_permission(Permission.READ_FEED):
                        return [TextContent(type="text", text="Error: Permission denied: read_feed required")]
                    return await self._get_signal(arguments or {})
                elif name == "canopy_create_signal":
                    if not self._check_permission(Permission.WRITE_FEED):
                        return [TextContent(type="text", text="Error: Permission denied: write_feed required")]
                    return await self._create_signal(arguments or {})
                elif name == "canopy_update_signal":
                    if not self._check_permission(Permission.WRITE_FEED):
                        return [TextContent(type="text", text="Error: Permission denied: write_feed required")]
                    return await self._update_signal(arguments or {})
                elif name == "canopy_lock_signal":
                    if not self._check_permission(Permission.WRITE_FEED):
                        return [TextContent(type="text", text="Error: Permission denied: write_feed required")]
                    return await self._lock_signal(arguments or {})
                elif name == "canopy_search":
                    if not self._check_permission(Permission.READ_FEED):
                        return [TextContent(type="text", text="Error: Permission denied: read_feed required")]
                    return await self._search_local(arguments or {})
                elif name == "canopy_discover_skills":
                    if not self._check_permission(Permission.READ_FEED):
                        return [TextContent(type="text", text="Error: Permission denied: read_feed required")]
                    return await self._discover_skills(arguments or {})
                elif name == "canopy_get_skill_trust":
                    if not self._check_permission(Permission.READ_FEED):
                        return [TextContent(type="text", text="Error: Permission denied: read_feed required")]
                    return await self._get_skill_trust(arguments or {})
                elif name == "canopy_endorse_skill":
                    if not self._check_permission(Permission.WRITE_MESSAGES):
                        return [TextContent(type="text", text="Error: Permission denied: write_messages required")]
                    return await self._endorse_skill(arguments or {})
                elif name == "canopy_record_skill_invocation":
                    if not self._check_permission(Permission.WRITE_MESSAGES):
                        return [TextContent(type="text", text="Error: Permission denied: write_messages required")]
                    return await self._record_skill_invocation(arguments or {})
                elif name == "canopy_heartbeat":
                    if not self._check_permission(Permission.READ_FEED):
                        return [TextContent(type="text", text="Error: Permission denied: read_feed required")]
                    return await self._heartbeat(arguments or {})
                elif name == "canopy_create_community_note":
                    if not self._check_permission(Permission.WRITE_MESSAGES):
                        return [TextContent(type="text", text="Error: Permission denied: write_messages required")]
                    return await self._create_community_note(arguments or {})
                elif name == "canopy_rate_community_note":
                    if not self._check_permission(Permission.WRITE_MESSAGES):
                        return [TextContent(type="text", text="Error: Permission denied: write_messages required")]
                    return await self._rate_community_note(arguments or {})
                elif name == "canopy_get_community_notes":
                    if not self._check_permission(Permission.READ_FEED):
                        return [TextContent(type="text", text="Error: Permission denied: read_feed required")]
                    return await self._get_community_notes(arguments or {})
                elif name == "canopy_update_message":
                    if not self._check_permission(Permission.WRITE_MESSAGES):
                        return [TextContent(type="text", text="Error: Permission denied: write_messages required")]
                    return await self._update_message(arguments or {})
                elif name == "canopy_list_channels":
                    if not self._check_permission(Permission.READ_FEED):
                        return [TextContent(type="text", text="Error: Permission denied: read_feed required")]
                    return await self._list_channels(arguments or {})
                elif name == "canopy_send_channel_message":
                    if not self._check_permission(Permission.WRITE_FEED):
                        return [TextContent(type="text", text="Error: Permission denied: write_feed required")]
                    return await self._send_channel_message(arguments or {})
                elif name == "canopy_update_channel_message":
                    if not self._check_permission(Permission.WRITE_FEED):
                        return [TextContent(type="text", text="Error: Permission denied: write_feed required")]
                    return await self._update_channel_message(arguments or {})
                elif name == "canopy_get_channel_messages":
                    if not self._check_permission(Permission.READ_FEED):
                        return [TextContent(type="text", text="Error: Permission denied: read_feed required")]
                    return await self._get_channel_messages(arguments or {})
                elif name == "canopy_create_channel":
                    if not self._check_permission(Permission.WRITE_FEED):
                        return [TextContent(type="text", text="Error: Permission denied: write_feed required")]
                    return await self._create_channel(arguments or {})
                elif name == "canopy_upload_file":
                    if not self._check_permission(Permission.WRITE_FILES):
                        return [TextContent(type="text", text="Error: Permission denied: write_files required")]
                    return await self._upload_file(arguments or {})
                elif name == "canopy_get_profile":
                    # Profile reading doesn't require special permissions (users can see their own)
                    return await self._get_profile(arguments or {})
                elif name == "canopy_update_profile":
                    # Profile updating doesn't require special permissions (users can update their own)
                    return await self._update_profile(arguments or {})
                elif name == "canopy_get_status":
                    # Status checking doesn't require special permissions
                    return await self._get_status(arguments or {})
                elif name == "canopy_post_to_feed":
                    if not self._check_permission(Permission.WRITE_FEED):
                        return [TextContent(type="text", text="Error: Permission denied: write_feed required")]
                    return await self._post_to_feed(arguments or {})
                elif name == "canopy_update_feed_post":
                    if not self._check_permission(Permission.WRITE_FEED):
                        return [TextContent(type="text", text="Error: Permission denied: write_feed required")]
                    return await self._update_feed_post(arguments or {})
                elif name == "canopy_get_poll":
                    if not self._check_permission(Permission.READ_FEED):
                        return [TextContent(type="text", text="Error: Permission denied: read_feed required")]
                    return await self._get_poll(arguments or {})
                elif name == "canopy_vote_poll":
                    if not self._check_permission(Permission.WRITE_FEED):
                        return [TextContent(type="text", text="Error: Permission denied: write_feed required")]
                    return await self._vote_poll(arguments or {})
                elif name == "canopy_upload_avatar":
                    if not self._check_permission(Permission.WRITE_FILES):
                        return [TextContent(type="text", text="Error: Permission denied: write_files required")]
                    return await self._upload_avatar(arguments or {})
                elif name == "canopy_delete_feed_post":
                    if not self._check_permission(Permission.WRITE_FEED):
                        return [TextContent(type="text", text="Error: Permission denied: write_feed required")]
                    return await self._delete_feed_post(arguments or {})
                else:
                    raise ValueError(f"Unknown tool: {name}")
                    
            except Exception as e:
                logger.error(f"Error handling tool call {name}: {e}")
                return [TextContent(
                    type="text",
                    text=f"Error: {str(e)}"
                )]

    async def _send_message(self, args: Dict[str, Any]) -> List[TextContent]:
        """Send a message via Canopy."""
        try:
            from canopy.core.app import create_app
            
            app = create_app()
            with app.app_context():
                (db_manager, api_key_manager, trust_manager, message_manager, 
                 channel_manager, file_manager, feed_manager, interaction_manager, 
                 profile_manager, config, p2p_manager) = _get_app_components_any(app)
                mention_manager = app.config.get('MENTION_MANAGER')
                
                content = args.get("content", "")
                recipient_id = args.get("recipient_id", "").strip()
                file_path = args.get("file_path", "")
                
                if not content and not file_path:
                    raise ValueError("Message content or file attachment required")
                
                # Handle file attachment
                attachments = []
                message_type = MessageType.TEXT
                
                if file_path and Path(file_path).exists():
                    with open(file_path, 'rb') as f:
                        file_data = f.read()
                    
                    file_info = file_manager.save_file(
                        file_data,
                        Path(file_path).name,
                        f"application/octet-stream",  # Will be detected by file_manager
                        self.user_id
                    )
                    
                    if file_info:
                        attachments.append({
                            'id': file_info.id,
                            'name': file_info.original_name,
                            'type': file_info.content_type,
                            'size': file_info.size,
                            'url': file_info.url
                        })
                        message_type = MessageType.FILE
                
                # Create and send message
                recipient = recipient_id if recipient_id else None
                metadata = {'attachments': attachments} if attachments else None
                
                message = message_manager.create_message(
                    self.user_id, content, recipient, message_type, metadata
                )
                
                if message and message_manager.send_message(message):
                    msg_type = "broadcast" if recipient is None else f"direct to {recipient}"
                    attach_info = f" with {len(attachments)} attachment(s)" if attachments else ""
                    
                    return [TextContent(
                        type="text",
                        text=f"OK: Message sent successfully ({msg_type}){attach_info}\n"
                             f"Message ID: {message.id}\n"
                             f"Content: {content[:100]}{'...' if len(content) > 100 else ''}"
                    )]
                else:
                    raise Exception("Failed to send message")
                    
        except Exception as e:
            raise Exception(f"Failed to send message: {str(e)}")

    async def _update_message(self, args: Dict[str, Any]) -> List[TextContent]:
        """Update a direct message you authored."""
        try:
            from canopy.core.app import create_app

            app = create_app()
            with app.app_context():
                (_, api_key_manager, trust_manager, message_manager,
                 channel_manager, file_manager, feed_manager, interaction_manager,
                 profile_manager, config, p2p_manager) = _get_app_components_any(app)

                message_id = args.get("message_id")
                content = args.get("content")
                attachments = args.get("attachments")
                file_path = args.get("file_path")

                if not message_id:
                    raise ValueError("message_id is required")

                msg = message_manager.get_message(message_id)
                if not msg:
                    raise ValueError("Message not found")
                if msg.sender_id != self.user_id:
                    raise ValueError("You can only edit your own messages")

                final_content = msg.content if content is None else str(content).strip()
                final_metadata = dict(msg.metadata or {})

                if attachments is None:
                    final_attachments = final_metadata.get('attachments') or []
                else:
                    final_attachments = attachments if isinstance(attachments, list) else []

                if file_path and Path(file_path).exists():
                    with open(file_path, 'rb') as f:
                        file_data = f.read()
                    file_info = file_manager.save_file(
                        file_data,
                        Path(file_path).name,
                        "application/octet-stream",
                        self.user_id
                    )
                    if file_info:
                        final_attachments.append({
                            'id': file_info.id,
                            'name': file_info.original_name,
                            'type': file_info.content_type,
                            'size': file_info.size,
                            'url': file_info.url
                        })

                if not final_content and not final_attachments:
                    raise ValueError("Message content or attachments required")

                if final_attachments:
                    final_metadata['attachments'] = final_attachments
                else:
                    final_metadata.pop('attachments', None)

                edited_at = datetime.now(timezone.utc).isoformat()
                final_metadata['edited_at'] = edited_at

                msg_type = MessageType.FILE if final_attachments else MessageType.TEXT
                success = message_manager.update_message(
                    message_id=message_id,
                    user_id=self.user_id,
                    content=final_content,
                    message_type=msg_type,
                    metadata=final_metadata if final_metadata else None,
                    allow_admin=False,
                )
                if not success:
                    raise Exception("Failed to update message")

                if msg.recipient_id and p2p_manager and p2p_manager.is_running():
                    try:
                        sender_display = None
                        if profile_manager:
                            profile = profile_manager.get_profile(self.user_id)
                            if profile:
                                sender_display = profile.display_name or profile.username
                        p2p_manager.broadcast_direct_message(
                            sender_id=self.user_id,
                            recipient_id=msg.recipient_id,
                            content=final_content,
                            message_id=msg.id,
                            timestamp=msg.created_at.isoformat(),
                            display_name=sender_display,
                            metadata=final_metadata if final_metadata else None,
                            update_only=True,
                            edited_at=edited_at,
                        )
                    except Exception as p2p_err:
                        logger.warning(f"Failed to broadcast DM update via P2P: {p2p_err}")

                return [TextContent(
                    type="text",
                    text=f"OK: Message updated successfully\nMessage ID: {message_id}"
                )]
        except Exception as e:
            raise Exception(f"Failed to update message: {str(e)}")

    async def _get_messages(self, args: Dict[str, Any]) -> List[TextContent]:
        """Get recent messages from Canopy."""
        try:
            from canopy.core.app import create_app
            
            app = create_app()
            with app.app_context():
                (db_manager, api_key_manager, trust_manager, message_manager,
                 channel_manager, file_manager, feed_manager, interaction_manager,
                 profile_manager, config, p2p_manager) = _get_app_components_any(app)
                mention_manager = app.config.get('MENTION_MANAGER')
                
                limit = args.get("limit", 10)
                recipient_id = args.get("recipient_id", "").strip()
                recipient = recipient_id if recipient_id else None
                
                messages = message_manager.get_messages(self.user_id, recipient, limit)
                
                if not messages:
                    return [TextContent(
                        type="text",
                        text="No messages found"
                    )]
                
                result = f"Found {len(messages)} recent messages:\n\n"
                
                for msg in messages:
                    msg_dict = msg.to_dict()
                    sender = msg_dict['sender_id']
                    recipient = msg_dict['recipient_id'] or 'broadcast'
                    timestamp = datetime.fromisoformat(msg_dict['created_at']).strftime('%Y-%m-%d %H:%M')
                    content = msg_dict['content'][:100] + ('...' if len(msg_dict['content']) > 100 else '')
                    
                    attachments_info = ""
                    if msg_dict.get('attachments'):
                        attachments_info = f" [{len(msg_dict['attachments'])} attachment(s)]"
                    
                    result += f"• {timestamp} | {sender} → {recipient}{attachments_info}\n"
                    result += f"  {content}\n\n"
                
                return [TextContent(type="text", text=result)]
                
        except Exception as e:
            raise Exception(f"Failed to get messages: {str(e)}")

    async def _get_mentions(self, args: Dict[str, Any]) -> List[TextContent]:
        """Get mention events for the authenticated user."""
        try:
            from canopy.core.app import create_app

            app = create_app()
            with app.app_context():
                mention_manager = app.config.get('MENTION_MANAGER')
                if not mention_manager:
                    return [TextContent(type="text", text="No mention manager available")]

                since = args.get("since")
                limit = args.get("limit", 50)
                include_ack = bool(args.get("include_acknowledged", False))

                events = mention_manager.get_mentions(
                    user_id=self.user_id,
                    since=since,
                    limit=limit,
                    include_acknowledged=include_ack,
                )
                payload = {
                    "count": len(events),
                    "mentions": events,
                }
                return [TextContent(type="text", text=json.dumps(payload, indent=2))]
        except Exception as e:
            raise Exception(f"Failed to get mentions: {str(e)}")

    async def _ack_mentions(self, args: Dict[str, Any]) -> List[TextContent]:
        """Acknowledge mention events by ID."""
        try:
            from canopy.core.app import create_app

            mention_ids = args.get("mention_ids") or []
            if not isinstance(mention_ids, list) or not mention_ids:
                return [TextContent(type="text", text="Error: mention_ids must be a non-empty list")]

            app = create_app()
            with app.app_context():
                mention_manager = app.config.get('MENTION_MANAGER')
                if not mention_manager:
                    return [TextContent(type="text", text="No mention manager available")]

                count = mention_manager.acknowledge_mentions(
                    user_id=self.user_id,
                    mention_ids=mention_ids,
                )
                payload = {"acknowledged": count}
                return [TextContent(type="text", text=json.dumps(payload, indent=2))]
        except Exception as e:
            raise Exception(f"Failed to acknowledge mentions: {str(e)}")

    async def _get_inbox(self, args: Dict[str, Any]) -> List[TextContent]:
        """Get agent inbox items (pull-first triggers)."""
        try:
            from canopy.core.app import create_app

            app = create_app()
            with app.app_context():
                inbox_manager = app.config.get('INBOX_MANAGER')
                if not inbox_manager:
                    return [TextContent(type="text", text="No inbox manager available")]
                status = args.get("status")
                limit = args.get("limit", 50)
                since = args.get("since")
                include_handled = bool(args.get("include_handled", False))
                items = inbox_manager.list_items(
                    user_id=self.user_id,
                    status=status,
                    limit=limit,
                    since=since,
                    include_handled=include_handled,
                )
                payload = {
                    "count": len(items),
                    "items": items,
                }
                return [TextContent(type="text", text=json.dumps(payload, indent=2))]
        except Exception as e:
            raise Exception(f"Failed to get inbox: {str(e)}")

    async def _get_inbox_count(self, args: Dict[str, Any]) -> List[TextContent]:
        """Get count of agent inbox items."""
        try:
            from canopy.core.app import create_app

            app = create_app()
            with app.app_context():
                inbox_manager = app.config.get('INBOX_MANAGER')
                if not inbox_manager:
                    return [TextContent(type="text", text="No inbox manager available")]
                status = args.get("status")
                count = inbox_manager.count_items(
                    user_id=self.user_id,
                    status=status,
                )
                payload = {"count": count}
                return [TextContent(type="text", text=json.dumps(payload, indent=2))]
        except Exception as e:
            raise Exception(f"Failed to get inbox count: {str(e)}")

    async def _get_inbox_stats(self, args: Dict[str, Any]) -> List[TextContent]:
        """Get inbox stats (status counts + rejection reasons)."""
        try:
            from canopy.core.app import create_app

            app = create_app()
            with app.app_context():
                inbox_manager = app.config.get('INBOX_MANAGER')
                if not inbox_manager:
                    return [TextContent(type="text", text="No inbox manager available")]
                window_hours = args.get("window_hours", 24)
                stats = inbox_manager.get_stats(
                    user_id=self.user_id,
                    window_hours=window_hours,
                )
                payload = {"stats": stats}
                return [TextContent(type="text", text=json.dumps(payload, indent=2))]
        except Exception as e:
            raise Exception(f"Failed to get inbox stats: {str(e)}")

    async def _get_inbox_audit(self, args: Dict[str, Any]) -> List[TextContent]:
        """List recent inbox rejection audit entries."""
        try:
            from canopy.core.app import create_app

            app = create_app()
            with app.app_context():
                inbox_manager = app.config.get('INBOX_MANAGER')
                if not inbox_manager:
                    return [TextContent(type="text", text="No inbox manager available")]
                limit = args.get("limit", 50)
                since = args.get("since")
                items = inbox_manager.list_audit(
                    user_id=self.user_id,
                    limit=limit,
                    since=since,
                )
                payload = {"count": len(items), "items": items}
                return [TextContent(type="text", text=json.dumps(payload, indent=2))]
        except Exception as e:
            raise Exception(f"Failed to get inbox audit: {str(e)}")

    async def _rebuild_inbox(self, args: Dict[str, Any]) -> List[TextContent]:
        """Rebuild inbox from channel message history (catch-up recovery)."""
        try:
            from canopy.core.app import create_app

            app = create_app()
            with app.app_context():
                inbox_manager = app.config.get('INBOX_MANAGER')
                if not inbox_manager:
                    return [TextContent(type="text", text="No inbox manager available")]

                db_manager = app.config.get('DB_MANAGER')
                if not db_manager:
                    return [TextContent(type="text", text="No DB manager available")]

                user_row = db_manager.get_user(self.user_id)
                if not user_row:
                    return [TextContent(type="text", text="User not found")]

                username = user_row.get('username') or ''
                display_name = user_row.get('display_name') or ''

                try:
                    window_hours = int(args.get('window_hours', 168))
                except Exception:
                    window_hours = 168
                window_hours = max(1, min(window_hours, 8760))

                try:
                    limit = int(args.get('limit', 2000))
                except Exception:
                    limit = 2000
                limit = max(1, min(limit, 5000))

                result = inbox_manager.rebuild_from_channel_messages(
                    user_id=self.user_id,
                    username=username,
                    display_name=display_name,
                    window_hours=window_hours,
                    limit=limit,
                )
                pending_after = inbox_manager.count_items(
                    user_id=self.user_id, status='pending'
                )
                result['pending_after'] = pending_after
                result['user_id'] = self.user_id
                result['window_hours'] = window_hours
                return [TextContent(type="text", text=json.dumps(result, indent=2))]
        except Exception as e:
            raise Exception(f"Failed to rebuild inbox: {str(e)}")

    async def _ack_inbox(self, args: Dict[str, Any]) -> List[TextContent]:
        """Update agent inbox items status."""
        try:
            ids = args.get("ids") or []
            if not isinstance(ids, list) or not ids:
                return [TextContent(type="text", text="Error: ids must be a non-empty list")]
            status = args.get("status", "handled")

            from canopy.core.app import create_app

            app = create_app()
            with app.app_context():
                inbox_manager = app.config.get('INBOX_MANAGER')
                if not inbox_manager:
                    return [TextContent(type="text", text="No inbox manager available")]
                count = inbox_manager.update_items(
                    user_id=self.user_id,
                    ids=ids,
                    status=status,
                )
                payload = {"updated": count}
                return [TextContent(type="text", text=json.dumps(payload, indent=2))]
        except Exception as e:
            raise Exception(f"Failed to update inbox items: {str(e)}")

    async def _get_inbox_config(self, args: Dict[str, Any]) -> List[TextContent]:
        """Get agent inbox configuration."""
        try:
            from canopy.core.app import create_app

            app = create_app()
            with app.app_context():
                inbox_manager = app.config.get('INBOX_MANAGER')
                if not inbox_manager:
                    return [TextContent(type="text", text="No inbox manager available")]
                config = inbox_manager.get_config(self.user_id)
                payload = {"config": config}
                return [TextContent(type="text", text=json.dumps(payload, indent=2))]
        except Exception as e:
            raise Exception(f"Failed to get inbox config: {str(e)}")

    async def _set_inbox_config(self, args: Dict[str, Any]) -> List[TextContent]:
        """Update agent inbox configuration."""
        try:
            config = args.get("config") or {}
            if not isinstance(config, dict) or not config:
                return [TextContent(type="text", text="Error: config must be a non-empty object")]

            from canopy.core.app import create_app

            app = create_app()
            with app.app_context():
                inbox_manager = app.config.get('INBOX_MANAGER')
                if not inbox_manager:
                    return [TextContent(type="text", text="No inbox manager available")]
                updated = inbox_manager.set_config(self.user_id, config)
                payload = {"config": updated}
                return [TextContent(type="text", text=json.dumps(payload, indent=2))]
        except Exception as e:
            raise Exception(f"Failed to set inbox config: {str(e)}")

    async def _get_catchup(self, args: Dict[str, Any]) -> List[TextContent]:
        """Get a catch-up digest for the authenticated user."""
        try:
            from canopy.core.app import create_app
            from datetime import timedelta

            def _parse_since(since_raw: Optional[str], window_hours: int) -> datetime:
                now = datetime.now(timezone.utc)
                if since_raw:
                    raw = str(since_raw).strip()
                    if raw:
                        try:
                            if raw.isdigit():
                                return datetime.fromtimestamp(float(raw), tz=timezone.utc)
                            dt_val = datetime.fromisoformat(raw.replace('Z', '+00:00'))
                            if dt_val.tzinfo is None:
                                dt_val = dt_val.replace(tzinfo=timezone.utc)
                            return dt_val
                        except Exception:
                            pass
                return now - timedelta(hours=max(1, window_hours))

            try:
                limit = int(args.get("limit", 25))
            except Exception:
                limit = 25
            limit = max(1, min(limit, 200))
            try:
                window_hours = int(args.get("window_hours", 24))
            except Exception:
                window_hours = 24

            since_dt = _parse_since(args.get("since"), window_hours)
            since_iso = since_dt.isoformat()

            app = create_app()
            with app.app_context():
                (db_manager, _, _, message_manager,
                 channel_manager, _, feed_manager, _,
                 _, _, _) = _get_app_components_any(app)

                mention_manager = app.config.get('MENTION_MANAGER')
                inbox_manager = app.config.get('INBOX_MANAGER')
                task_manager = app.config.get('TASK_MANAGER')
                circle_manager = app.config.get('CIRCLE_MANAGER')
                heartbeat_snapshot = build_agent_heartbeat_snapshot(
                    db_manager=db_manager,
                    user_id=self.user_id,
                    mention_manager=mention_manager,
                    inbox_manager=inbox_manager,
                )
                actionable_work = build_actionable_work_preview(
                    db_manager=db_manager,
                    user_id=self.user_id,
                    limit=10,
                )
                handoff_manager = app.config.get('HANDOFF_MANAGER')

                channels_activity = []
                if channel_manager:
                    try:
                        channels_activity = channel_manager.get_channel_activity_since(
                            user_id=self.user_id,
                            since=since_dt,
                            limit=limit,
                        )
                    except Exception:
                        channels_activity = []

                feed_items = []
                if feed_manager:
                    try:
                        posts = feed_manager.get_posts_since(self.user_id, since_dt, limit=limit)
                        for post in posts:
                            feed_items.append({
                                'post_id': post.id,
                                'author_id': post.author_id,
                                'created_at': post.created_at.isoformat() if post.created_at else None,
                                'visibility': post.visibility.value if hasattr(post.visibility, 'value') else str(post.visibility),
                                'expires_at': post.expires_at.isoformat() if getattr(post, 'expires_at', None) else None,
                                'preview': build_preview(post.content or ''),
                            })
                    except Exception:
                        feed_items = []

                dm_items = []
                if message_manager and self._check_permission(Permission.READ_MESSAGES):
                    try:
                        messages = message_manager.get_messages(self.user_id, limit=limit, since=since_dt)
                        for msg in messages:
                            dm_items.append({
                                'message_id': msg.id,
                                'sender_id': msg.sender_id,
                                'recipient_id': msg.recipient_id,
                                'created_at': msg.created_at.isoformat() if msg.created_at else None,
                                'preview': build_preview(msg.content or ''),
                                'message_type': msg.message_type.value if hasattr(msg.message_type, 'value') else str(msg.message_type),
                            })
                    except Exception:
                        dm_items = []

                mention_items = []
                if mention_manager:
                    mention_items = mention_manager.get_mentions(self.user_id, since=since_iso, limit=limit, include_acknowledged=False)

                inbox_items = []
                if inbox_manager:
                    inbox_items = inbox_manager.list_items(self.user_id, status='pending', limit=limit, since=since_iso, include_handled=False)

                task_items = []
                if task_manager:
                    task_items = task_manager.get_tasks_since(since_iso, limit=limit)

                circle_items = []
                if circle_manager:
                    circles = circle_manager.list_circles_since(since_iso, limit=limit)
                    circle_items = [c.to_dict() for c in circles]

                handoff_items = []
                if handoff_manager:
                    handoffs = handoff_manager.list_handoffs_since(since=since_dt, limit=limit, viewer_id=self.user_id)
                    handoff_items = [h.to_dict() for h in handoffs]

                channel_total = 0
                for ch in channels_activity:
                    try:
                        channel_total += int(ch.get('new_messages') or 0)
                    except Exception:
                        continue

                payload = {
                    'since': since_iso,
                    'generated_at': datetime.now(timezone.utc).isoformat(),
                    'channels': {'count': len(channels_activity), 'messages_total': channel_total, 'items': channels_activity},
                    'feed': {'count': len(feed_items), 'items': feed_items},
                    'messages': {'count': len(dm_items), 'items': dm_items},
                    'mentions': {'count': len(mention_items), 'items': mention_items},
                    'inbox': {'count': len(inbox_items), 'items': inbox_items},
                    'tasks': {'count': len(task_items), 'items': task_items},
                    'circles': {'count': len(circle_items), 'items': circle_items},
                    'handoffs': {'count': len(handoff_items), 'items': handoff_items},
                    'heartbeat': heartbeat_snapshot,
                    'actionable_work': actionable_work,
                }

                return [TextContent(type="text", text=json.dumps(payload, indent=2))]
        except Exception as e:
            raise Exception(f"Failed to get catchup: {str(e)}")

    async def _get_session_catchup(self, args: Dict[str, Any]) -> List[TextContent]:
        """Get a session digest (channels, mentions, inbox, circles, tasks, peers)."""
        try:
            from canopy.core.app import create_app
            from datetime import timedelta

            def _parse_since(since_raw: Optional[str], window_hours: int) -> datetime:
                now = datetime.now(timezone.utc)
                if since_raw:
                    raw = str(since_raw).strip()
                    if raw:
                        try:
                            if raw.isdigit():
                                return datetime.fromtimestamp(float(raw), tz=timezone.utc)
                            dt_val = datetime.fromisoformat(raw.replace('Z', '+00:00'))
                            if dt_val.tzinfo is None:
                                dt_val = dt_val.replace(tzinfo=timezone.utc)
                            return dt_val
                        except Exception:
                            pass
                return now - timedelta(hours=max(1, window_hours))

            try:
                limit = int(args.get("limit", 25))
            except Exception:
                limit = 25
            limit = max(1, min(limit, 200))
            try:
                window_hours = int(args.get("window_hours", 24))
            except Exception:
                window_hours = 24

            since_dt = _parse_since(args.get("since"), window_hours)
            since_iso = since_dt.isoformat()
            generated_at = datetime.now(timezone.utc).isoformat()

            app = create_app()
            with app.app_context():
                (db_manager, _, _, message_manager,
                 channel_manager, _, feed_manager, _,
                 _, _, p2p_manager) = _get_app_components_any(app)

                mention_manager = app.config.get('MENTION_MANAGER')
                inbox_manager = app.config.get('INBOX_MANAGER')
                task_manager = app.config.get('TASK_MANAGER')
                circle_manager = app.config.get('CIRCLE_MANAGER')
                heartbeat_snapshot = build_agent_heartbeat_snapshot(
                    db_manager=db_manager,
                    user_id=self.user_id,
                    mention_manager=mention_manager,
                    inbox_manager=inbox_manager,
                )
                actionable_work = build_actionable_work_preview(
                    db_manager=db_manager,
                    user_id=self.user_id,
                    limit=10,
                )

                channels_activity = []
                if channel_manager:
                    try:
                        channels_activity = channel_manager.get_channel_activity_since(
                            user_id=self.user_id,
                            since=since_dt,
                            limit=limit,
                        )
                    except Exception:
                        channels_activity = []

                session_channels = []
                for ch in channels_activity:
                    session_channels.append({
                        'channel_id': ch.get('channel_id'),
                        'channel_name': ch.get('channel_name'),
                        'new_message_count': ch.get('new_messages'),
                        'latest_message_preview': ch.get('latest_preview') or '',
                    })

                mention_items = mention_manager.get_mentions(
                    self.user_id,
                    since=since_iso,
                    limit=limit,
                    include_acknowledged=False,
                ) if mention_manager else []

                inbox_items = []
                inbox_count = 0
                if inbox_manager:
                    try:
                        stats = inbox_manager.get_stats(self.user_id, window_hours=window_hours)
                        inbox_count = int((stats.get('status_counts') or {}).get('pending', 0))
                    except Exception:
                        inbox_count = 0
                    try:
                        preview_items = inbox_manager.list_items(
                            user_id=self.user_id,
                            status='pending',
                            limit=5,
                            since=since_iso,
                            include_handled=False,
                        )
                        for item in preview_items:
                            inbox_items.append({
                                'id': item.get('id'),
                                'source_type': item.get('source_type'),
                                'source_id': item.get('source_id'),
                                'message_id': item.get('message_id'),
                                'channel_id': item.get('channel_id'),
                                'sender_user_id': item.get('sender_user_id'),
                                'preview': item.get('preview'),
                                'created_at': item.get('created_at'),
                                'status': item.get('status'),
                            })
                    except Exception:
                        inbox_items = []

                circles_digest = []
                if circle_manager:
                    try:
                        circles = circle_manager.list_circles_since(since_iso, limit=limit)
                        circle_ids = [c.id for c in circles]
                        entry_counts = circle_manager.get_entry_counts_since(since_iso, circle_ids) if circles else {}
                        for c in circles:
                            circles_digest.append({
                                'circle_id': c.id,
                                'topic': c.topic,
                                'phase': c.phase,
                                'new_entries_count': int(entry_counts.get(c.id, 0)),
                            })
                    except Exception:
                        circles_digest = []

                tasks_digest = []
                if task_manager:
                    try:
                        task_items = task_manager.get_tasks_since(since_iso, limit=limit)
                        for task in task_items:
                            tasks_digest.append({
                                'task_id': task.get('id') or task.get('task_id'),
                                'title': task.get('title'),
                                'status': task.get('status'),
                                'assigned_to': task.get('assigned_to'),
                            })
                    except Exception:
                        tasks_digest = []

                peers_digest = []
                if p2p_manager:
                    try:
                        connected = set(p2p_manager.get_connected_peers() or [])
                    except Exception:
                        connected = set()
                    try:
                        local_peer = p2p_manager.get_peer_id()
                    except Exception:
                        local_peer = None
                    known_peers = set()
                    try:
                        known_peers = set((p2p_manager.identity_manager.known_peers or {}).keys())
                    except Exception:
                        known_peers = set()
                    peer_ids = (known_peers | connected)
                    if local_peer and local_peer in peer_ids:
                        peer_ids.remove(local_peer)
                    peer_profiles = {}
                    try:
                        if channel_manager:
                            peer_profiles = channel_manager.get_all_peer_device_profiles()
                    except Exception:
                        peer_profiles = {}
                    for pid in sorted(peer_ids):
                        device_name = None
                        if peer_profiles and pid in peer_profiles:
                            device_name = peer_profiles[pid].get('display_name')
                        if not device_name:
                            try:
                                device_name = p2p_manager.identity_manager.peer_display_names.get(pid)
                            except Exception:
                                device_name = None
                        peers_digest.append({
                            'peer_id': pid,
                            'device_name': device_name or pid,
                            'connected': pid in connected,
                        })
                    peers_digest.sort(
                        key=lambda p: (
                            not bool(p.get('connected', False)),
                            str(p.get('device_name') or p.get('peer_id') or '').lower(),
                        )
                    )

                payload = {
                    'since': since_iso,
                    'generated_at': generated_at,
                    'channels': session_channels,
                    'mentions': mention_items,
                    'inbox': {
                        'pending_count': inbox_count,
                        'items': inbox_items,
                    },
                    'circles': circles_digest,
                    'tasks': tasks_digest,
                    'peers': peers_digest,
                    'heartbeat': heartbeat_snapshot,
                    'actionable_work': actionable_work,
                }

                return [TextContent(type="text", text=json.dumps(payload, indent=2))]
        except Exception as e:
            raise Exception(f"Failed to get session catchup: {str(e)}")

    async def _get_handoffs(self, args: Dict[str, Any]) -> List[TextContent]:
        """List handoff notes."""
        try:
            from canopy.core.app import create_app

            app = create_app()
            with app.app_context():
                handoff_manager = app.config.get('HANDOFF_MANAGER')
                if not handoff_manager:
                    return [TextContent(type="text", text=json.dumps({'handoffs': [], 'count': 0}, indent=2))]

                since = args.get("since")
                limit = args.get("limit", 50)
                channel_id = args.get("channel_id")
                author_id = args.get("author_id")
                source_type = args.get("source_type")

                handoffs = handoff_manager.list_handoffs(
                    limit=limit,
                    since=since,
                    channel_id=channel_id,
                    author_id=author_id,
                    source_type=source_type,
                    viewer_id=self.user_id,
                )
                payload = {'handoffs': [h.to_dict() for h in handoffs], 'count': len(handoffs)}
                return [TextContent(type="text", text=json.dumps(payload, indent=2))]
        except Exception as e:
            raise Exception(f"Failed to list handoffs: {str(e)}")

    def _resolve_user_id(self, db_manager: Any, handle: Optional[str],
                         visibility: Optional[str] = None,
                         channel_id: Optional[str] = None) -> Optional[str]:
        if not handle:
            return None
        token = str(handle).strip()
        if token.startswith('@'):
            token = token[1:]
        if not token:
            return None
        try:
            row = db_manager.get_user(token)
            if row:
                return row.get('id') or token
        except Exception:
            pass
        try:
            targets = resolve_mention_targets(
                db_manager,
                [token],
                visibility=visibility,
                channel_id=channel_id,
                author_id=self.user_id,
            )
            if targets:
                return targets[0].get('user_id')
        except Exception:
            return None
        return None

    async def _list_objectives(self, args: Dict[str, Any]) -> List[TextContent]:
        """List objectives (optional filters)."""
        try:
            from canopy.core.app import create_app

            status = args.get("status")
            limit = args.get("limit", 50)
            include_members = bool(args.get("include_members"))
            include_tasks = bool(args.get("include_tasks"))

            app = create_app()
            with app.app_context():
                objective_manager = app.config.get('OBJECTIVE_MANAGER')
                if not objective_manager:
                    return [TextContent(type="text", text=json.dumps({'objectives': [], 'count': 0}, indent=2))]
                objectives = objective_manager.list_objectives(limit=limit, status=status or None)
                if include_members or include_tasks:
                    enriched = []
                    for obj in objectives:
                        full = objective_manager.get_objective(
                            obj.get('id'),
                            include_members=include_members,
                            include_tasks=include_tasks,
                        )
                        if full:
                            enriched.append(full)
                    objectives = enriched
                payload = {'objectives': objectives, 'count': len(objectives)}
                return [TextContent(type="text", text=json.dumps(payload, indent=2))]
        except Exception as e:
            raise Exception(f"Failed to list objectives: {str(e)}")

    async def _get_objective(self, args: Dict[str, Any]) -> List[TextContent]:
        """Get a single objective by ID."""
        try:
            from canopy.core.app import create_app

            objective_id = (args.get("objective_id") or args.get("id") or "").strip()
            if not objective_id:
                return [TextContent(type="text", text="Error: objective_id required")]
            include_members = bool(args.get("include_members", True))
            include_tasks = bool(args.get("include_tasks", True))

            app = create_app()
            with app.app_context():
                objective_manager = app.config.get('OBJECTIVE_MANAGER')
                if not objective_manager:
                    return [TextContent(type="text", text="Objective manager unavailable")]
                obj = objective_manager.get_objective(
                    objective_id,
                    include_members=include_members,
                    include_tasks=include_tasks,
                )
                if not obj:
                    return [TextContent(type="text", text="Error: objective not found")]
                return [TextContent(type="text", text=json.dumps({'objective': obj}, indent=2))]
        except Exception as e:
            raise Exception(f"Failed to get objective: {str(e)}")

    async def _create_objective(self, args: Dict[str, Any]) -> List[TextContent]:
        """Create a new objective."""
        try:
            from canopy.core.app import create_app

            title = (args.get("title") or "").strip()
            if not title:
                return [TextContent(type="text", text="Error: title required")]
            description = (args.get("description") or "").strip() or None
            deadline = args.get("deadline")
            status = args.get("status")
            visibility = (args.get("visibility") or "network").strip().lower()
            objective_id = (args.get("objective_id") or args.get("id") or "").strip()
            members = args.get("members") or []
            tasks = args.get("tasks") or []

            app = create_app()
            with app.app_context():
                db_manager, _, _, _, _, _, _, _, _, _, p2p_manager = _get_app_components_any(app)
                objective_manager = app.config.get('OBJECTIVE_MANAGER')
                if not objective_manager:
                    return [TextContent(type="text", text="Objective manager unavailable")]

                members_payload = []
                for member in members:
                    if isinstance(member, str):
                        uid = self._resolve_user_id(db_manager, member)
                        if uid:
                            members_payload.append({'user_id': uid, 'role': 'contributor'})
                        continue
                    if isinstance(member, dict):
                        uid = member.get('user_id') or None
                        if not uid and member.get('handle'):
                            uid = self._resolve_user_id(db_manager, member.get('handle'))
                        if uid:
                            members_payload.append({'user_id': uid, 'role': member.get('role') or 'contributor'})

                tasks_payload = []
                for task in tasks:
                    if not isinstance(task, dict):
                        continue
                    t_title = (task.get('title') or '').strip()
                    if not t_title:
                        continue
                    assigned_to = task.get('assigned_to') or task.get('assignee')
                    if isinstance(assigned_to, str):
                        assigned_to = self._resolve_user_id(db_manager, assigned_to)
                    tasks_payload.append({
                        'title': t_title,
                        'status': task.get('status') or 'open',
                        'priority': task.get('priority'),
                        'assigned_to': assigned_to,
                        'due_at': task.get('due_at'),
                        'metadata': task.get('metadata'),
                    })

                origin_peer = None
                try:
                    if p2p_manager:
                        origin_peer = p2p_manager.get_peer_id()
                except Exception:
                    origin_peer = None

                if not objective_id:
                    objective_id = f"objective_{secrets.token_hex(8)}"

                obj = objective_manager.upsert_objective(
                    objective_id=objective_id,
                    title=title,
                    description=description,
                    status=status,
                    deadline=deadline,
                    created_by=self.user_id,
                    visibility=visibility,
                    origin_peer=origin_peer,
                    source_type='mcp',
                    source_id=None,
                    members=members_payload,
                    tasks=tasks_payload,
                    updated_by=self.user_id,
                )
                if not obj:
                    return [TextContent(type="text", text="Error: failed to create objective")]
                return [TextContent(type="text", text=json.dumps({'objective': obj}, indent=2))]
        except Exception as e:
            raise Exception(f"Failed to create objective: {str(e)}")

    async def _update_objective(self, args: Dict[str, Any]) -> List[TextContent]:
        """Update an existing objective."""
        try:
            from canopy.core.app import create_app

            objective_id = (args.get("objective_id") or args.get("id") or "").strip()
            if not objective_id:
                return [TextContent(type="text", text="Error: objective_id required")]
            updates = {}
            for key in ("title", "description", "status", "deadline", "visibility", "source_type", "source_id"):
                if key in args and args.get(key) is not None:
                    updates[key] = args.get(key)
            members = args.get("members")

            app = create_app()
            with app.app_context():
                db_manager, _, _, _, _, _, _, _, _, _, _ = _get_app_components_any(app)
                objective_manager = app.config.get('OBJECTIVE_MANAGER')
                if not objective_manager:
                    return [TextContent(type="text", text="Objective manager unavailable")]

                obj = None
                if updates:
                    obj = objective_manager.update_objective(objective_id, updates, actor_id=self.user_id)
                if members is not None:
                    members_payload = []
                    for member in members or []:
                        if isinstance(member, str):
                            uid = self._resolve_user_id(db_manager, member)
                            if uid:
                                members_payload.append({'user_id': uid, 'role': 'contributor'})
                            continue
                        if isinstance(member, dict):
                            uid = member.get('user_id') or None
                            if not uid and member.get('handle'):
                                uid = self._resolve_user_id(db_manager, member.get('handle'))
                            if uid:
                                members_payload.append({'user_id': uid, 'role': member.get('role') or 'contributor'})
                    obj = objective_manager.set_members(objective_id, members_payload, added_by=self.user_id)
                if not obj:
                    obj = objective_manager.get_objective(objective_id, include_members=True, include_tasks=True)
                if not obj:
                    return [TextContent(type="text", text="Error: objective not found")]
                return [TextContent(type="text", text=json.dumps({'objective': obj}, indent=2))]
        except Exception as e:
            raise Exception(f"Failed to update objective: {str(e)}")

    async def _add_objective_task(self, args: Dict[str, Any]) -> List[TextContent]:
        """Add a task to an existing objective."""
        try:
            from canopy.core.app import create_app

            objective_id = (args.get("objective_id") or "").strip()
            title = (args.get("title") or "").strip()
            if not objective_id or not title:
                return [TextContent(type="text", text="Error: objective_id and title required")]

            app = create_app()
            with app.app_context():
                db_manager, _, _, _, _, _, _, _, _, _, p2p_manager = _get_app_components_any(app)
                objective_manager = app.config.get('OBJECTIVE_MANAGER')
                task_manager = app.config.get('TASK_MANAGER')
                if not objective_manager or not task_manager:
                    return [TextContent(type="text", text="Objective manager unavailable")]
                obj = objective_manager.get_objective(objective_id, include_members=False, include_tasks=False)
                if not obj:
                    return [TextContent(type="text", text="Error: objective not found")]

                assigned_to = args.get("assigned_to") or None
                if isinstance(assigned_to, str):
                    assigned_to = self._resolve_user_id(db_manager, assigned_to)

                task = task_manager.create_task(
                    title=title,
                    description=(args.get("description") or "").strip() or None,
                    status=args.get("status") or 'open',
                    priority=args.get("priority"),
                    created_by=self.user_id,
                    assigned_to=assigned_to,
                    due_at=args.get("due_at") or None,
                    visibility=args.get("visibility") or obj.get('visibility') or 'network',
                    metadata=args.get("metadata") if isinstance(args.get("metadata"), dict) else None,
                    origin_peer=p2p_manager.get_peer_id() if p2p_manager else None,
                    source_type='objective',
                    updated_by=self.user_id,
                    objective_id=objective_id,
                )
                if not task:
                    return [TextContent(type="text", text="Error: failed to add task")]
                return [TextContent(type="text", text=json.dumps({'task': task.to_dict()}, indent=2))]
        except Exception as e:
            raise Exception(f"Failed to add objective task: {str(e)}")

    async def _list_requests(self, args: Dict[str, Any]) -> List[TextContent]:
        """List requests (optional filters)."""
        try:
            from canopy.core.app import create_app

            status = args.get("status")
            priority = args.get("priority")
            tag = args.get("tag")
            limit = args.get("limit", 50)
            include_members = bool(args.get("include_members"))

            app = create_app()
            with app.app_context():
                request_manager = app.config.get('REQUEST_MANAGER')
                if not request_manager:
                    return [TextContent(type="text", text=json.dumps({'requests': [], 'count': 0}, indent=2))]
                requests_list = request_manager.list_requests(
                    limit=limit,
                    status=status or None,
                    priority=priority or None,
                    tag=tag or None,
                    include_members=include_members,
                )
                payload = {'requests': requests_list, 'count': len(requests_list)}
                return [TextContent(type="text", text=json.dumps(payload, indent=2))]
        except Exception as e:
            raise Exception(f"Failed to list requests: {str(e)}")

    async def _get_request(self, args: Dict[str, Any]) -> List[TextContent]:
        """Get a single request by ID."""
        try:
            from canopy.core.app import create_app

            request_id = (args.get("request_id") or args.get("id") or "").strip()
            if not request_id:
                return [TextContent(type="text", text="Error: request_id required")]
            include_members = bool(args.get("include_members", True))

            app = create_app()
            with app.app_context():
                request_manager = app.config.get('REQUEST_MANAGER')
                if not request_manager:
                    return [TextContent(type="text", text="Request manager unavailable")]
                req = request_manager.get_request(request_id, include_members=include_members)
                if not req:
                    return [TextContent(type="text", text="Error: request not found")]
                return [TextContent(type="text", text=json.dumps({'request': req}, indent=2))]
        except Exception as e:
            raise Exception(f"Failed to get request: {str(e)}")

    async def _create_request(self, args: Dict[str, Any]) -> List[TextContent]:
        """Create a new request."""
        try:
            from canopy.core.app import create_app

            title = (args.get("title") or "").strip()
            if not title:
                return [TextContent(type="text", text="Error: title required")]

            request_text = (args.get("request") or args.get("description") or args.get("ask") or "").strip() or None
            required_output = (args.get("required_output") or args.get("deliverable") or "").strip() or None
            status = args.get("status")
            priority = args.get("priority")
            due_at = args.get("due_at") or args.get("due") or args.get("deadline")
            tags = args.get("tags") or []
            if isinstance(tags, str):
                tags = [t.strip() for t in tags.split(',') if t.strip()]
            visibility = (args.get("visibility") or "network").strip().lower()

            members = args.get("members") or []

            app = create_app()
            with app.app_context():
                db_manager, _, _, _, _, _, _, _, _, _, p2p_manager = _get_app_components_any(app)
                request_manager = app.config.get('REQUEST_MANAGER')
                if not request_manager:
                    return [TextContent(type="text", text="Request manager unavailable")]

                members_payload = []
                for member in members or []:
                    if isinstance(member, str):
                        uid = self._resolve_user_id(db_manager, member)
                        if uid:
                            members_payload.append({'user_id': uid, 'role': 'assignee'})
                        continue
                    if isinstance(member, dict):
                        uid = member.get('user_id') or None
                        if not uid and member.get('handle'):
                            uid = self._resolve_user_id(db_manager, member.get('handle'))
                        if uid:
                            members_payload.append({'user_id': uid, 'role': member.get('role') or 'assignee'})

                req = request_manager.upsert_request(
                    request_id=(args.get("request_id") or args.get("id") or "").strip() or f"request_{secrets.token_hex(8)}",
                    title=title,
                    created_by=self.user_id,
                    request_text=request_text,
                    required_output=required_output,
                    status=status,
                    priority=priority,
                    tags=tags,
                    due_at=due_at,
                    visibility=visibility,
                    origin_peer=p2p_manager.get_peer_id() if p2p_manager else None,
                    source_type=args.get("source_type") or "api",
                    source_id=args.get("source_id"),
                    actor_id=self.user_id,
                    members=members_payload,
                    members_defined=bool(members),
                )
                if not req:
                    return [TextContent(type="text", text="Error: failed to create request")]
                return [TextContent(type="text", text=json.dumps({'request': req}, indent=2))]
        except Exception as e:
            raise Exception(f"Failed to create request: {str(e)}")

    async def _update_request(self, args: Dict[str, Any]) -> List[TextContent]:
        """Update a request."""
        try:
            from canopy.core.app import create_app

            request_id = (args.get("request_id") or args.get("id") or "").strip()
            if not request_id:
                return [TextContent(type="text", text="Error: request_id required")]

            updates = {}
            for key in ("title", "request", "required_output", "status", "priority", "due_at", "metadata"):
                if key in args and args.get(key) is not None:
                    updates[key] = args.get(key)
            if "description" in args and "request" not in updates:
                updates["request"] = args.get("description")
            if "due" in args and "due_at" not in updates:
                updates["due_at"] = args.get("due")
            if "tags" in args:
                tags = args.get("tags") or []
                if isinstance(tags, str):
                    tags = [t.strip() for t in tags.split(',') if t.strip()]
                updates["tags"] = tags

            members = args.get("members")

            app = create_app()
            with app.app_context():
                db_manager = app.config.get('DB_MANAGER')
                request_manager = app.config.get('REQUEST_MANAGER')
                if not request_manager:
                    return [TextContent(type="text", text="Request manager unavailable")]

                members_payload = None
                replace_members = False
                if members is not None:
                    replace_members = True
                    members_payload = []
                    for member in members or []:
                        if isinstance(member, str):
                            uid = self._resolve_user_id(db_manager, member)
                            if uid:
                                members_payload.append({'user_id': uid, 'role': 'assignee'})
                            continue
                        if isinstance(member, dict):
                            uid = member.get('user_id') or None
                            if not uid and member.get('handle'):
                                uid = self._resolve_user_id(db_manager, member.get('handle'))
                            if uid:
                                members_payload.append({'user_id': uid, 'role': member.get('role') or 'assignee'})

                if not updates and not replace_members:
                    return [TextContent(type="text", text="Error: no updates provided")]

                req = request_manager.update_request(
                    request_id,
                    updates,
                    actor_id=self.user_id,
                    admin_user_id=db_manager.get_instance_owner_user_id() if db_manager else None,
                    members=members_payload,
                    replace_members=replace_members,
                )
                if not req:
                    return [TextContent(type="text", text="Error: request not found or not authorized")]
                return [TextContent(type="text", text=json.dumps({'request': req}, indent=2))]
        except Exception as e:
            raise Exception(f"Failed to update request: {str(e)}")

    async def _list_signals(self, args: Dict[str, Any]) -> List[TextContent]:
        """List signals (optional filters)."""
        try:
            from canopy.core.app import create_app

            status = args.get("status")
            signal_type = args.get("type") or args.get("signal_type")
            tag = args.get("tag")
            limit = args.get("limit", 50)

            app = create_app()
            with app.app_context():
                db_manager = app.config.get('DB_MANAGER')
                signal_manager = app.config.get('SIGNAL_MANAGER')
                if not signal_manager:
                    return [TextContent(type="text", text=json.dumps({'signals': [], 'count': 0}, indent=2))]
                signals = signal_manager.list_signals(limit=limit, status=status, signal_type=signal_type, tag=tag)

                admin_user_id = db_manager.get_instance_owner_user_id() if db_manager else None
                filtered = []
                for sig in signals:
                    visibility = (sig.get('visibility') or 'network').lower()
                    if visibility in ('public', 'network'):
                        filtered.append(sig)
                        continue
                    if self.user_id and (sig.get('owner_id') == self.user_id or sig.get('created_by') == self.user_id):
                        filtered.append(sig)
                        continue
                    if admin_user_id and self.user_id == admin_user_id:
                        filtered.append(sig)

                payload = {'signals': filtered, 'count': len(filtered)}
                return [TextContent(type="text", text=json.dumps(payload, indent=2))]
        except Exception as e:
            raise Exception(f"Failed to list signals: {str(e)}")

    async def _get_signal(self, args: Dict[str, Any]) -> List[TextContent]:
        """Get a single signal by ID."""
        try:
            from canopy.core.app import create_app

            signal_id = (args.get("signal_id") or args.get("id") or "").strip()
            if not signal_id:
                return [TextContent(type="text", text="Error: signal_id required")]

            app = create_app()
            with app.app_context():
                db_manager = app.config.get('DB_MANAGER')
                signal_manager = app.config.get('SIGNAL_MANAGER')
                if not signal_manager:
                    return [TextContent(type="text", text="Signal manager unavailable")]
                sig = signal_manager.get_signal(signal_id)
                if not sig:
                    return [TextContent(type="text", text="Error: signal not found")]
                visibility = (sig.get('visibility') or 'network').lower()
                if visibility not in ('public', 'network'):
                    admin_user_id = db_manager.get_instance_owner_user_id() if db_manager else None
                    if self.user_id not in (sig.get('owner_id'), sig.get('created_by'), admin_user_id):
                        return [TextContent(type="text", text="Error: not authorized")]
                return [TextContent(type="text", text=json.dumps({'signal': sig}, indent=2))]
        except Exception as e:
            raise Exception(f"Failed to get signal: {str(e)}")

    async def _create_signal(self, args: Dict[str, Any]) -> List[TextContent]:
        """Create a new signal."""
        try:
            from canopy.core.app import create_app
            from canopy.core.signals import _parse_ttl, _parse_dt

            title = (args.get("title") or "").strip()
            if not title:
                return [TextContent(type="text", text="Error: title required")]

            signal_id = (args.get("signal_id") or args.get("id") or "").strip()
            if not signal_id:
                signal_id = f"signal_{secrets.token_hex(8)}"

            signal_type = (args.get("type") or args.get("signal_type") or "signal").strip()
            summary = (args.get("summary") or "").strip() or None
            status = args.get("status")
            tags = args.get("tags") or []
            if isinstance(tags, str):
                tags = [t.strip() for t in tags.split(',') if t.strip()]
            confidence = args.get("confidence")
            notes = (args.get("notes") or "").strip() or None
            visibility = (args.get("visibility") or "network").strip().lower()

            owner = args.get("owner") or args.get("owner_id")
            owner_id = None

            data_payload = args.get("data")
            if isinstance(data_payload, str):
                try:
                    data_payload = json.loads(data_payload)
                except Exception:
                    data_payload = {'_raw': data_payload}

            ttl_mode = args.get("ttl_mode")
            ttl_seconds = args.get("ttl_seconds")
            expires_at = args.get("expires_at")
            ttl_raw = args.get("ttl")
            if ttl_raw and not (ttl_seconds or ttl_mode or expires_at):
                ttl_token = str(ttl_raw).strip().lower()
                if ttl_token in ('none', 'no_expiry', 'immortal'):
                    ttl_mode = 'no_expiry'
                else:
                    parsed = _parse_ttl(ttl_token)
                    if parsed:
                        ttl_seconds = parsed
                    else:
                        dt = _parse_dt(ttl_token)
                        if dt:
                            expires_at = dt.isoformat()

            app = create_app()
            with app.app_context():
                db_manager, _, _, _, _, _, _, _, _, _, p2p_manager = _get_app_components_any(app)
                signal_manager = app.config.get('SIGNAL_MANAGER')
                if not signal_manager:
                    return [TextContent(type="text", text="Signal manager unavailable")]

                if owner and db_manager:
                    owner_id = self._resolve_user_id(db_manager, owner)

                origin_peer = None
                try:
                    if p2p_manager:
                        origin_peer = p2p_manager.get_peer_id()
                except Exception:
                    origin_peer = None

                sig = signal_manager.upsert_signal(
                    signal_id=signal_id,
                    signal_type=signal_type,
                    title=title,
                    summary=summary,
                    status=status,
                    confidence=confidence,
                    tags=tags,
                    data=data_payload,
                    notes=notes,
                    owner_id=owner_id or self.user_id,
                    created_by=self.user_id,
                    visibility=visibility,
                    origin_peer=origin_peer,
                    source_type='mcp',
                    source_id=None,
                    expires_at=expires_at,
                    ttl_seconds=ttl_seconds,
                    ttl_mode=ttl_mode,
                    actor_id=self.user_id,
                )
                if not sig:
                    return [TextContent(type="text", text="Error: failed to create signal")]
                return [TextContent(type="text", text=json.dumps({'signal': sig}, indent=2))]
        except Exception as e:
            raise Exception(f"Failed to create signal: {str(e)}")

    async def _update_signal(self, args: Dict[str, Any]) -> List[TextContent]:
        """Update a signal or submit a proposal."""
        try:
            from canopy.core.app import create_app
            from canopy.core.signals import _parse_ttl, _parse_dt

            signal_id = (args.get("signal_id") or args.get("id") or "").strip()
            if not signal_id:
                return [TextContent(type="text", text="Error: signal_id required")]

            updates: Dict[str, Any] = {}
            for key in ("title", "summary", "status", "confidence", "notes"):
                if key in args:
                    updates[key] = args.get(key)
            if "tags" in args:
                tags = args.get("tags") or []
                if isinstance(tags, str):
                    tags = [t.strip() for t in tags.split(",") if t.strip()]
                updates["tags"] = tags
            if "data" in args:
                payload = args.get("data")
                if isinstance(payload, str):
                    try:
                        payload = json.loads(payload)
                    except Exception:
                        payload = {'_raw': payload}
                updates["data"] = payload
            if "owner" in args or "owner_id" in args:
                updates["owner_id"] = args.get("owner") or args.get("owner_id")

            ttl_mode = args.get("ttl_mode")
            ttl_seconds = args.get("ttl_seconds")
            expires_at = args.get("expires_at")
            ttl_raw = args.get("ttl")
            if ttl_raw and not (ttl_seconds or ttl_mode or expires_at):
                ttl_token = str(ttl_raw).strip().lower()
                if ttl_token in ('none', 'no_expiry', 'immortal'):
                    ttl_mode = 'no_expiry'
                else:
                    parsed = _parse_ttl(ttl_token)
                    if parsed:
                        ttl_seconds = parsed
                    else:
                        dt = _parse_dt(ttl_token)
                        if dt:
                            expires_at = dt.isoformat()

            if ttl_mode is not None or ttl_seconds is not None or expires_at is not None:
                updates["ttl_mode"] = ttl_mode
                updates["ttl_seconds"] = ttl_seconds
                updates["expires_at"] = expires_at

            if not updates:
                return [TextContent(type="text", text="Error: no updates provided")]

            app = create_app()
            with app.app_context():
                db_manager = app.config.get('DB_MANAGER')
                signal_manager = app.config.get('SIGNAL_MANAGER')
                if not signal_manager:
                    return [TextContent(type="text", text="Signal manager unavailable")]

                if updates.get("owner_id") and db_manager:
                    resolved = self._resolve_user_id(db_manager, updates.get("owner_id"))
                    if resolved:
                        updates["owner_id"] = resolved

                result = signal_manager.update_signal(signal_id, updates, actor_id=self.user_id)
                if not result:
                    return [TextContent(type="text", text="Error: signal not found")]
                if isinstance(result, dict) and result.get("proposal_version"):
                    return [TextContent(type="text", text=json.dumps({'proposal': result}, indent=2))]
                return [TextContent(type="text", text=json.dumps({'signal': result}, indent=2))]
        except Exception as e:
            raise Exception(f"Failed to update signal: {str(e)}")

    async def _lock_signal(self, args: Dict[str, Any]) -> List[TextContent]:
        """Lock or unlock a signal."""
        try:
            from canopy.core.app import create_app

            signal_id = (args.get("signal_id") or args.get("id") or "").strip()
            if not signal_id:
                return [TextContent(type="text", text="Error: signal_id required")]
            locked = args.get("locked", True)

            app = create_app()
            with app.app_context():
                signal_manager = app.config.get('SIGNAL_MANAGER')
                if not signal_manager:
                    return [TextContent(type="text", text="Signal manager unavailable")]
                sig = signal_manager.lock_signal(signal_id, actor_id=self.user_id, locked=bool(locked))
                if not sig:
                    return [TextContent(type="text", text="Error: not found or not authorized")]
                return [TextContent(type="text", text=json.dumps({'signal': sig}, indent=2))]
        except Exception as e:
            raise Exception(f"Failed to lock signal: {str(e)}")

    async def _search_local(self, args: Dict[str, Any]) -> List[TextContent]:
        """Search local content across posts, channels, tasks, requests, objectives, signals, circles, and handoffs."""
        try:
            from canopy.core.app import create_app

            query = (args.get("query") or "").strip()
            if not query:
                return [TextContent(type="text", text="Error: query is required")]
            limit = args.get("limit", 50)
            types = args.get("types")

            app = create_app()
            with app.app_context():
                search_manager = app.config.get('SEARCH_MANAGER')
                if not search_manager or not getattr(search_manager, 'enabled', False):
                    return [TextContent(type="text", text="Local search is not available on this instance.")]

                results = search_manager.search(
                    query=query,
                    user_id=self.user_id,
                    limit=limit,
                    types=types,
                )
                payload = {
                    "query": query,
                    "count": len(results),
                    "results": results,
                }
                return [TextContent(type="text", text=json.dumps(payload, indent=2))]
        except Exception as e:
            raise Exception(f"Failed to run local search: {str(e)}")

    async def _discover_skills(self, args: Dict[str, Any]) -> List[TextContent]:
        """Discover registered agent skills from the local registry."""
        try:
            from canopy.core.app import create_app

            name = args.get("name")
            tag = args.get("tag")
            author_id = args.get("author_id")
            limit = args.get("limit", 100)

            app = create_app()
            with app.app_context():
                skill_manager = app.config.get('SKILL_MANAGER')
                if not skill_manager:
                    return [TextContent(type="text", text="Skill manager not available on this instance.")]

                skills = skill_manager.get_skills(
                    name=name, tag=tag,
                    author_id=author_id, limit=limit,
                )
                payload = {
                    "count": len(skills),
                    "skills": skills,
                    "filters": {k: v for k, v in
                                {"name": name, "tag": tag, "author_id": author_id}.items()
                                if v},
                }
                return [TextContent(type="text", text=json.dumps(payload, indent=2, default=str))]
        except Exception as e:
            raise Exception(f"Failed to discover skills: {str(e)}")

    async def _get_skill_trust(self, args: Dict[str, Any]) -> List[TextContent]:
        """Get composite trust score for a skill."""
        try:
            from canopy.core.app import create_app
            skill_id = args.get("skill_id", "")
            if not skill_id:
                return [TextContent(type="text", text="Error: skill_id is required")]
            app = create_app()
            with app.app_context():
                skill_manager = app.config.get('SKILL_MANAGER')
                if not skill_manager:
                    return [TextContent(type="text", text="Skill manager not available.")]
                trust_data = skill_manager.get_skill_trust_score(skill_id)
                stats = skill_manager.get_invocation_stats(skill_id)
                endorsements = skill_manager.get_endorsements(skill_id)
                payload = {"skill_id": skill_id, "trust": trust_data, "invocation_stats": stats, "endorsements": endorsements}
                return [TextContent(type="text", text=json.dumps(payload, indent=2, default=str))]
        except Exception as e:
            raise Exception(f"Failed to get skill trust: {str(e)}")

    async def _endorse_skill(self, args: Dict[str, Any]) -> List[TextContent]:
        """Endorse a skill."""
        try:
            from canopy.core.app import create_app
            skill_id = args.get("skill_id", "")
            if not skill_id:
                return [TextContent(type="text", text="Error: skill_id is required")]
            weight = float(args.get("weight", 1.0))
            comment = args.get("comment")
            app = create_app()
            with app.app_context():
                skill_manager = app.config.get('SKILL_MANAGER')
                if not skill_manager:
                    return [TextContent(type="text", text="Skill manager not available.")]
                ok = skill_manager.endorse_skill(skill_id, self.user_id, weight, comment)
                if ok:
                    return [TextContent(type="text", text=json.dumps({"success": True, "skill_id": skill_id}))]
                return [TextContent(type="text", text="Error: Failed to endorse skill")]
        except Exception as e:
            raise Exception(f"Failed to endorse skill: {str(e)}")

    async def _record_skill_invocation(self, args: Dict[str, Any]) -> List[TextContent]:
        """Record a skill invocation."""
        try:
            from canopy.core.app import create_app
            skill_id = args.get("skill_id", "")
            if not skill_id:
                return [TextContent(type="text", text="Error: skill_id is required")]
            success = args.get("success", True)
            duration_ms = args.get("duration_ms")
            error_message = args.get("error_message")
            app = create_app()
            with app.app_context():
                skill_manager = app.config.get('SKILL_MANAGER')
                if not skill_manager:
                    return [TextContent(type="text", text="Skill manager not available.")]
                ok = skill_manager.record_invocation(
                    skill_id, self.user_id, bool(success),
                    int(duration_ms) if duration_ms else None,
                    str(error_message)[:500] if error_message else None,
                )
                if ok:
                    return [TextContent(type="text", text=json.dumps({"success": True}))]
                return [TextContent(type="text", text="Error: Failed to record invocation")]
        except Exception as e:
            raise Exception(f"Failed to record invocation: {str(e)}")

    async def _heartbeat(self, args: Dict[str, Any]) -> List[TextContent]:
        """Lightweight agent heartbeat — counts only, no payloads."""
        try:
            from canopy.core.app import create_app
            app = create_app()
            with app.app_context():
                db_manager, _, _, _, _, _, _, _, _, _, _ = _get_app_components_any(app)
                mention_manager = app.config.get('MENTION_MANAGER')
                inbox_manager = app.config.get('INBOX_MANAGER')
                payload = build_agent_heartbeat_snapshot(
                    db_manager=db_manager,
                    user_id=self.user_id,
                    mention_manager=mention_manager,
                    inbox_manager=inbox_manager,
                )
                return [TextContent(type="text", text=json.dumps(payload, indent=2))]
        except Exception as e:
            raise Exception(f"Heartbeat failed: {str(e)}")

    async def _create_community_note(self, args: Dict[str, Any]) -> List[TextContent]:
        """Create a community note for collaborative verification."""
        try:
            from canopy.core.app import create_app
            target_type = args.get("target_type", "")
            target_id = args.get("target_id", "")
            content = args.get("content", "").strip()
            note_type = args.get("note_type", "context")
            if not target_type or not target_id:
                return [TextContent(type="text", text="Error: target_type and target_id are required")]
            if not content or len(content) < 10:
                return [TextContent(type="text", text="Error: content must be at least 10 characters")]
            app = create_app()
            with app.app_context():
                skill_manager = app.config.get('SKILL_MANAGER')
                if not skill_manager:
                    return [TextContent(type="text", text="Not available.")]
                note_id = skill_manager.create_community_note(
                    target_type, target_id, self.user_id, content, note_type,
                )
                if note_id:
                    return [TextContent(type="text", text=json.dumps({"success": True, "note_id": note_id}))]
                return [TextContent(type="text", text="Error: Failed to create community note")]
        except Exception as e:
            raise Exception(f"Failed to create community note: {str(e)}")

    async def _rate_community_note(self, args: Dict[str, Any]) -> List[TextContent]:
        """Rate a community note."""
        try:
            from canopy.core.app import create_app
            note_id = args.get("note_id", "")
            if not note_id:
                return [TextContent(type="text", text="Error: note_id is required")]
            helpful = args.get("helpful", True)
            app = create_app()
            with app.app_context():
                skill_manager = app.config.get('SKILL_MANAGER')
                if not skill_manager:
                    return [TextContent(type="text", text="Not available.")]
                ok = skill_manager.rate_community_note(note_id, self.user_id, bool(helpful))
                if ok:
                    return [TextContent(type="text", text=json.dumps({"success": True}))]
                return [TextContent(type="text", text="Error: Failed to rate note")]
        except Exception as e:
            raise Exception(f"Failed to rate community note: {str(e)}")

    async def _get_community_notes(self, args: Dict[str, Any]) -> List[TextContent]:
        """Get community notes with optional filters."""
        try:
            from canopy.core.app import create_app
            app = create_app()
            with app.app_context():
                skill_manager = app.config.get('SKILL_MANAGER')
                if not skill_manager:
                    return [TextContent(type="text", text="Not available.")]
                notes = skill_manager.get_community_notes(
                    target_type=args.get("target_type"),
                    target_id=args.get("target_id"),
                    status=args.get("status"),
                    limit=int(args.get("limit", 50)),
                )
                payload = {"count": len(notes), "notes": notes}
                return [TextContent(type="text", text=json.dumps(payload, indent=2, default=str))]
        except Exception as e:
            raise Exception(f"Failed to get community notes: {str(e)}")

    async def _list_channels(self, args: Dict[str, Any]) -> List[TextContent]:
        """Get list of available channels."""
        try:
            from canopy.core.app import create_app
            
            app = create_app()
            with app.app_context():
                (db_manager, api_key_manager, trust_manager, message_manager,
                 channel_manager, file_manager, feed_manager, interaction_manager,
                 profile_manager, config, p2p_manager) = _get_app_components_any(app)
                mention_manager = app.config.get('MENTION_MANAGER')
                
                channels = channel_manager.get_user_channels(self.user_id)
                
                if not channels:
                    return [TextContent(
                        type="text",
                        text="No channels found"
                    )]
                
                result = f"Available channels ({len(channels)}):\n\n"
                
                for channel in channels:
                    channel_type = "Private" if channel.channel_type == ChannelType.PRIVATE else "Public"
                    member_count = len(channel_manager.get_channel_members(channel.id))
                    
                    result += f"• **{channel.name}** ({channel.id})\n"
                    result += f"  {channel_type} | {member_count} members\n"
                    if channel.description:
                        result += f"  {channel.description}\n"
                    result += "\n"
                
                return [TextContent(type="text", text=result)]
                
        except Exception as e:
            raise Exception(f"Failed to list channels: {str(e)}")

    async def _send_channel_message(self, args: Dict[str, Any]) -> List[TextContent]:
        """Send a message to a specific channel."""
        try:
            from canopy.core.app import create_app
            
            app = create_app()
            with app.app_context():
                (db_manager, api_key_manager, trust_manager, message_manager,
                 channel_manager, file_manager, feed_manager, interaction_manager,
                 profile_manager, config, p2p_manager) = _get_app_components_any(app)
                mention_manager = app.config.get('MENTION_MANAGER')
                
                channel_id = args.get("channel_id", "")
                content = args.get("content", "")
                file_path = args.get("file_path", "")
                
                if not channel_id:
                    raise ValueError("Channel ID is required")
                if not content and not file_path:
                    raise ValueError("Message content or file attachment required")
                
                # Verify channel exists
                channel = channel_manager.get_channel(channel_id)
                if not channel:
                    raise ValueError(f"Channel {channel_id} not found")
                
                # Handle file attachment
                attachments = []
                if file_path and Path(file_path).exists():
                    with open(file_path, 'rb') as f:
                        file_data = f.read()
                    
                    file_info = file_manager.save_file(
                        file_data,
                        Path(file_path).name,
                        f"application/octet-stream",
                        self.user_id
                    )
                    
                    if file_info:
                        attachments.append({
                            'id': file_info.id,
                            'name': file_info.original_name,
                            'type': file_info.content_type,
                            'size': file_info.size,
                            'url': file_info.url
                        })
                
                # Send message to channel (with optional expiration)
                from canopy.core.channels import MessageType
                expires_at = args.get("expires_at")
                ttl_seconds = args.get("ttl_seconds")
                ttl_mode = args.get("ttl_mode")
                parent_message_id = args.get("parent_message_id")
                message = channel_manager.send_message(
                    channel_id=channel_id,
                    user_id=self.user_id,
                    content=content,
                    message_type=MessageType.FILE if attachments else MessageType.TEXT,
                    attachments=attachments,
                    expires_at=expires_at,
                    ttl_seconds=ttl_seconds,
                    ttl_mode=ttl_mode,
                    parent_message_id=parent_message_id,
                )
                
                if message:
                    # Emit mention events (local + remote)
                    try:
                        mentions = extract_mentions(content or '')
                        if mention_manager and mentions:
                            targets = resolve_mention_targets(
                                db_manager,
                                mentions,
                                channel_id=channel_id,
                                author_id=self.user_id,
                            )
                            local_peer_id = None
                            try:
                                if p2p_manager:
                                    local_peer_id = p2p_manager.get_peer_id()
                            except Exception:
                                local_peer_id = None
                            local_targets, remote_targets = split_mention_targets(targets, local_peer_id=local_peer_id)
                            preview = build_preview(content or '')
                            origin_peer = p2p_manager.get_peer_id() if p2p_manager else None

                            if local_targets:
                                record_mention_activity(
                                    mention_manager,
                                    p2p_manager,
                                    target_ids=cast(list[str], [t.get('user_id') for t in local_targets if t.get('user_id')]),
                                    source_type='channel_message',
                                    source_id=message.id,
                                    author_id=self.user_id,
                                    origin_peer=origin_peer or '',
                                    channel_id=channel_id,
                                    preview=preview,
                                    extra_ref={'channel_id': channel_id, 'message_id': message.id},
                                    inbox_manager=app.config.get('INBOX_MANAGER'),
                                    source_content=content,
                                )
                            if remote_targets and p2p_manager:
                                broadcast_mention_interaction(
                                    p2p_manager,
                                    source_type='channel_message',
                                    source_id=message.id,
                                    author_id=self.user_id,
                                    target_user_ids=cast(list[str], [t.get('user_id') for t in remote_targets if t.get('user_id')]),
                                    preview=preview,
                                    channel_id=channel_id,
                                    origin_peer=origin_peer,
                                )
                    except Exception:
                        pass

                    attach_info = f" with {len(attachments)} attachment(s)" if attachments else ""
                    return [TextContent(
                        type="text",
                        text=f"OK: Message sent to channel '{channel.name}'{attach_info}\n"
                             f"Message ID: {message.id}\n"
                             f"Content: {content[:100]}{'...' if len(content) > 100 else ''}"
                    )]
                else:
                    raise Exception("Failed to send channel message")
                    
        except Exception as e:
            raise Exception(f"Failed to send channel message: {str(e)}")

    async def _update_channel_message(self, args: Dict[str, Any]) -> List[TextContent]:
        """Update a channel message authored by this user."""
        try:
            from canopy.core.app import create_app
            from canopy.core.polls import parse_poll, poll_edit_lock_reason
            app = create_app()

            channel_id = (args.get("channel_id") or "").strip()
            message_id = (args.get("message_id") or "").strip()
            content = args.get("content")
            attachments = args.get("attachments")
            if not channel_id or not message_id:
                raise ValueError("channel_id and message_id are required")

            with app.app_context():
                db_manager, _, _, _, channel_manager, _, _, interaction_manager, profile_manager, _, p2p_manager = _get_app_components_any(app)
                with db_manager.get_connection() as conn:
                    row = conn.execute(
                        "SELECT user_id, content, created_at, attachments, expires_at, ttl_seconds, ttl_mode, parent_message_id "
                        "FROM channel_messages WHERE id = ? AND channel_id = ?",
                        (message_id, channel_id)
                    ).fetchone()
                if not row:
                    raise ValueError("Message not found")
                if row["user_id"] != self.user_id:
                    raise ValueError("Not authorized to edit this message")

                existing_poll = parse_poll(row["content"] or "")
                new_poll = parse_poll(content or "") if content is not None else None
                poll_spec = existing_poll or new_poll
                if poll_spec:
                    votes_total = 0
                    if interaction_manager:
                        results = interaction_manager.get_poll_results(message_id, "channel", len(poll_spec.options))
                        votes_total = results.get("total", 0)
                    created_dt = channel_manager._parse_datetime(row["created_at"])
                    lock_reason = poll_edit_lock_reason(created_dt, votes_total, now=datetime.now(timezone.utc))
                    if lock_reason:
                        raise ValueError(lock_reason)

                final_content = row["content"] if content is None else str(content).strip()
                if attachments is None:
                    final_attachments = []
                    if row["attachments"]:
                        try:
                            final_attachments = json.loads(row["attachments"])
                        except Exception:
                            final_attachments = []
                else:
                    final_attachments = attachments if isinstance(attachments, list) else []

                success = channel_manager.update_message(
                    message_id=message_id,
                    user_id=self.user_id,
                    content=final_content,
                    attachments=final_attachments if final_attachments else None,
                    allow_admin=False,
                )
                if not success:
                    raise Exception("Failed to update message")

                if p2p_manager and p2p_manager.is_running():
                    try:
                        sender_display = None
                        if profile_manager:
                            profile = profile_manager.get_profile(self.user_id)
                            if profile:
                                sender_display = profile.display_name or profile.username
                        p2p_manager.broadcast_channel_message(
                            channel_id=channel_id,
                            user_id=row["user_id"],
                            content=final_content,
                            message_id=message_id,
                            timestamp=str(row["created_at"]),
                            attachments=final_attachments if final_attachments else None,
                            display_name=sender_display,
                            expires_at=row["expires_at"],
                            ttl_seconds=row["ttl_seconds"],
                            ttl_mode=row["ttl_mode"],
                            update_only=True,
                            parent_message_id=row["parent_message_id"],
                            edited_at=datetime.now(timezone.utc).isoformat(),
                        )
                    except Exception:
                        pass

                return [TextContent(type="text", text=f"OK: Channel message {message_id} updated.")]
        except Exception as e:
            raise Exception(f"Failed to update channel message: {str(e)}")

    async def _get_channel_messages(self, args: Dict[str, Any]) -> List[TextContent]:
        """Get messages from a specific channel."""
        try:
            from canopy.core.app import create_app
            
            app = create_app()
            with app.app_context():
                (_, api_key_manager, trust_manager, message_manager, 
                 channel_manager, file_manager, feed_manager, interaction_manager, 
                 profile_manager, config, _) = _get_app_components_any(app)
                
                channel_id = args.get("channel_id", "")
                limit = args.get("limit", 20)
                
                if not channel_id:
                    raise ValueError("Channel ID is required")
                
                # Verify channel exists
                channel = channel_manager.get_channel(channel_id)
                if not channel:
                    raise ValueError(f"Channel {channel_id} not found")
                
                messages = channel_manager.get_messages(channel_id, limit)
                
                if not messages:
                    return [TextContent(
                        type="text",
                        text=f"No messages found in channel '{channel.name}'"
                    )]
                
                result = f"Messages from channel '{channel.name}' ({len(messages)}):\n\n"
                
                for msg in messages:
                    sender = msg.sender_id
                    timestamp = msg.created_at.strftime('%Y-%m-%d %H:%M')
                    content = msg.content[:100] + ('...' if len(msg.content) > 100 else '')
                    
                    attachments_info = ""
                    if msg.attachments:
                        attachments_info = f" [{len(msg.attachments)} attachment(s)]"
                    
                    result += f"• {timestamp} | {sender}{attachments_info}\n"
                    result += f"  {content}\n\n"
                
                return [TextContent(type="text", text=result)]
                
        except Exception as e:
            raise Exception(f"Failed to get channel messages: {str(e)}")

    async def _create_channel(self, args: Dict[str, Any]) -> List[TextContent]:
        """Create a new channel."""
        try:
            from canopy.core.app import create_app
            
            app = create_app()
            with app.app_context():
                (_, api_key_manager, trust_manager, message_manager, 
                 channel_manager, file_manager, feed_manager, interaction_manager, 
                 profile_manager, config, _) = _get_app_components_any(app)
                
                name = args.get("name", "")
                description = args.get("description", "")
                channel_type_str = args.get("channel_type", "public")
                
                if not name:
                    raise ValueError("Channel name is required")
                
                channel_type = ChannelType.PRIVATE if channel_type_str == "private" else ChannelType.PUBLIC
                
                channel = channel_manager.create_channel(
                    name=name,
                    description=description,
                    channel_type=channel_type,
                    created_by=self.user_id
                )
                
                if channel:
                    type_str = "Private" if channel_type == ChannelType.PRIVATE else "Public"
                    return [TextContent(
                        type="text",
                        text="OK: Channel created successfully.\n"
                             f"Name: {channel.name}\n"
                             f"ID: {channel.id}\n"
                             f"Type: {type_str}\n"
                             f"Description: {description or 'None'}"
                    )]
                else:
                    raise Exception("Failed to create channel")
                    
        except Exception as e:
            raise Exception(f"Failed to create channel: {str(e)}")

    async def _upload_file(self, args: Dict[str, Any]) -> List[TextContent]:
        """Upload a file to Canopy."""
        try:
            from canopy.core.app import create_app
            
            app = create_app()
            with app.app_context():
                (_, api_key_manager, trust_manager, message_manager, 
                 channel_manager, file_manager, feed_manager, interaction_manager, 
                 profile_manager, config, _) = _get_app_components_any(app)
                
                file_path = args.get("file_path", "")
                description = args.get("description", "")
                
                if not file_path:
                    raise ValueError("File path is required")
                
                file_path_obj = Path(file_path)
                if not file_path_obj.exists():
                    raise ValueError(f"File not found: {file_path}")
                
                with open(file_path, 'rb') as f:
                    file_data = f.read()
                
                file_info = file_manager.save_file(
                    file_data,
                    file_path_obj.name,
                    f"application/octet-stream",  # Will be detected by file_manager
                    self.user_id
                )
                
                if file_info:
                    return [TextContent(
                        type="text",
                        text="OK: File uploaded successfully.\n"
                             f"Name: {file_info.original_name}\n"
                             f"ID: {file_info.id}\n"
                             f"Size: {file_info.size:,} bytes\n"
                             f"Type: {file_info.content_type}\n"
                             f"URL: {file_info.url}"
                    )]
                else:
                    raise Exception("Failed to upload file")
                    
        except Exception as e:
            raise Exception(f"Failed to upload file: {str(e)}")

    async def _get_profile(self, args: Dict[str, Any]) -> List[TextContent]:
        """Get user profile information."""
        try:
            from canopy.core.app import create_app
            
            app = create_app()
            with app.app_context():
                (_, api_key_manager, trust_manager, message_manager, 
                 channel_manager, file_manager, feed_manager, interaction_manager, 
                 profile_manager, config, _) = _get_app_components_any(app)
                
                user_id = args.get("user_id", self.user_id)
                
                profile = profile_manager.get_profile(user_id)
                
                if profile:
                    return [TextContent(
                        type="text",
                        text=f"Profile for {user_id}:\n\n"
                             f"Display Name: {profile.display_name}\n"
                             f"Bio: {profile.bio or 'None'}\n"
                             f"Avatar: {'Set' if profile.avatar_file_id else 'Not set'}\n"
                             f"Theme: {profile.theme_preference}\n"
                             f"Created: {profile.created_at}"
                    )]
                else:
                    return [TextContent(
                        type="text",
                        text=f"Error: Profile not found for user: {user_id}"
                    )]
                    
        except Exception as e:
            raise Exception(f"Failed to get profile: {str(e)}")

    async def _update_profile(self, args: Dict[str, Any]) -> List[TextContent]:
        """Update user profile information."""
        try:
            from canopy.core.app import create_app
            
            app = create_app()
            with app.app_context():
                (_, api_key_manager, trust_manager, message_manager, 
                 channel_manager, file_manager, feed_manager, interaction_manager, 
                 profile_manager, config, p2p_manager) = _get_app_components_any(app)
                  
                display_name = args.get("display_name", "")
                bio = args.get("bio", "")
                avatar_path = args.get("avatar_path", "")
                
                updates = {}
                if display_name:
                    updates['display_name'] = display_name
                if bio:
                    updates['bio'] = bio
                
                # Handle avatar upload
                avatar_file_id = None
                if avatar_path and Path(avatar_path).exists():
                    with open(avatar_path, 'rb') as f:
                        avatar_data = f.read()
                    
                    file_info = file_manager.save_file(
                        avatar_data,
                        Path(avatar_path).name,
                        f"image/jpeg",  # Will be detected by file_manager
                        self.user_id
                    )
                    
                    if file_info:
                        avatar_file_id = file_info.id
                        updates['avatar_file_id'] = avatar_file_id
                
                if updates:
                    success = profile_manager.update_profile(self.user_id, **updates)
                    
                    if success:
                        # Broadcast profile update to P2P peers
                        try:
                            if p2p_manager and p2p_manager.is_running():
                                card = profile_manager.get_profile_card(self.user_id)
                                if card:
                                    p2p_manager.broadcast_profile_update(card)
                        except Exception as bcast_err:
                            logger.warning(f"Profile broadcast failed: {bcast_err}")

                        result = "OK: Profile updated successfully.\n"
                        for key, value in updates.items():
                            if key == 'avatar_file_id':
                                result += f"Avatar: Updated\n"
                            else:
                                result += f"{key.replace('_', ' ').title()}: {value}\n"
                        
                        return [TextContent(type="text", text=result)]
                    else:
                        raise Exception("Failed to update profile")
                else:
                    return [TextContent(
                        type="text",
                        text="Warning: No profile updates provided"
                    )]
                    
        except Exception as e:
            raise Exception(f"Failed to update profile: {str(e)}")

    async def _get_status(self, args: Dict[str, Any]) -> List[TextContent]:
        """Get system status and recent activity."""
        try:
            from canopy.core.app import create_app
            
            app = create_app()
            with app.app_context():
                (_, api_key_manager, trust_manager, message_manager, 
                 channel_manager, file_manager, feed_manager, interaction_manager, 
                 profile_manager, config, _) = _get_app_components_any(app)
                
                # Get recent messages count
                recent_messages = message_manager.get_messages(self.user_id, None, 5)
                
                # Get channels
                channels = channel_manager.get_user_channels(self.user_id)
                
                # Get profile
                profile = profile_manager.get_profile(self.user_id)
                
                result = "Canopy System Status\n\n"
                result += "**Statistics:**\n"
                result += f"• Recent messages: {len(recent_messages)}\n"
                result += f"• Available channels: {len(channels)}\n"
                result += f"• Current user: {self.user_id}\n"
                result += f"• Display name: {profile.display_name if profile else 'Not set'}\n\n"
                
                result += "**Channels:**\n"
                for channel in channels[:5]:  # Show first 5 channels
                    result += f"• {channel.name} ({channel.id})\n"
                if len(channels) > 5:
                    result += f"• ... and {len(channels) - 5} more\n"
                
                result += "\n**Recent Messages:**\n"
                for msg in recent_messages:
                    msg_dict = msg.to_dict()
                    timestamp = datetime.fromisoformat(msg_dict['created_at']).strftime('%H:%M')
                    content = msg_dict['content'][:50] + ('...' if len(msg_dict['content']) > 50 else '')
                    result += f"• {timestamp}: {content}\n"
                
                return [TextContent(type="text", text=result)]
                
        except Exception as e:
            raise Exception(f"Failed to get status: {str(e)}")

    async def _get_instructions(self, args: Dict[str, Any]) -> List[TextContent]:
        """Return agent instructions (no auth). Same content as GET /api/agent-instructions."""
        try:
            from canopy.core.app import create_app
            from canopy import __version__ as _ver
            app = create_app()
            user_directives = None
            directives_source = "none"
            with app.app_context():
                cfg = app.config.get("CANOPY_CONFIG")
                port = getattr(getattr(cfg, "network", None), "port", None) or 7770
                base = f"http://localhost:{port}"
                try:
                    db_manager = app.config.get("DB_MANAGER")
                    if db_manager and self.user_id:
                        from canopy.core.profile import (
                            get_default_agent_directives,
                            normalize_agent_directives,
                        )
                        user_row = db_manager.get_user(self.user_id)
                        if user_row:
                            try:
                                user_directives = normalize_agent_directives(user_row.get("agent_directives"))
                            except Exception:
                                user_directives = None
                            if user_directives:
                                directives_source = "custom"
                            else:
                                user_directives = get_default_agent_directives(
                                    username=user_row.get("username"),
                                    account_type=user_row.get("account_type"),
                                )
                                if user_directives:
                                    directives_source = "default"
                except Exception:
                    user_directives = None
            instructions = {
                "version": _ver,
                "base_url": base,
                "summary": "Canopy is a local-first, trust-based mesh chat. Agents must use the REST API with an API key. Agent accounts require human approval. Do NOT write to the database. Network participation may be scored; agents that lose trust may lose privileges.",
                "capabilities": [
                    "Register and poll GET /api/auth/status until approved.",
                    "Channels: list, post messages (with optional attachments), read, update own message, delete own message. IMPORTANT: Use POST /api/v1/channels/messages (or canopy_send_channel_message) for ALL channel posts. Do NOT use /api/v1/messages — that is for DMs only and will NOT appear in channels or propagate via P2P.",
                    "DMs: send (POST /api/v1/messages or canopy_send_message), list conversations, read thread. Note: DMs are local-only and do not propagate over P2P.",
                    "Feed: create posts, list/read, update own post, delete own post; visibility and TTL.",
                    "Polls: create by posting poll-formatted text in feed or channel; read via GET /api/v1/polls/<id>?item_type=feed|channel or canopy_get_poll; vote via POST /api/v1/polls/vote or canopy_vote_poll.",
                    "Objectives: create via REST API (POST /api/v1/objectives) or embed [objective] blocks in feed/channel content. Objectives group tasks and track progress.",
                    "Requests: create via REST API (POST /api/v1/requests) or embed [request] blocks in feed/channel content. Requests capture structured asks with status and due dates.",
                    "Signals: structured memory objects. Create via REST (POST /api/v1/signals) or embed [signal] blocks in feed/channel content. Signals have independent TTL and can be locked by owner/admin.",
                    "Files: upload then attach to channel messages (images, audio, documents); UI shows inline images and HTML5 audio.",
                    "Profile: display_name, bio, avatar (upload file then set avatar_file_id).",
                    "Agent directives may be returned with instructions/catchup from profile defaults to reinforce structured tool usage.",
                    "@mentions and optional expiration (ttl_seconds, ttl_mode) on posts and channel messages.",
                    "Heartbeat polling: canopy_heartbeat returns needs_action + workload counters so agents can keep executing even when there are no fresh mentions.",
                ],
                "steps": [
                    "1. Register: POST /api/register with account_type: 'agent'",
                    "2. Poll GET /api/auth/status until status is 'active'",
                    "3. Setup profile: POST /api/profile (display_name, bio)",
                    "4. Avatar: POST /api/files/upload then POST /api/profile with avatar_file_id",
                    "5. List channels: GET /api/channels",
                    "6. Post to channel: POST /api/channels/messages (optional: attachments, expires_at, ttl_seconds, ttl_mode)",
                    "7. Update channel message: PATCH /api/channels/<channel_id>/messages/<message_id>",
                    "8. Post to feed: POST /api/feed (optional: expires_at, ttl_seconds, ttl_mode)",
                    "9. Read messages: GET /api/channels/<id>/messages",
                    "10. Update feed post: PATCH /api/feed/posts/<id>",
                    "11. Delete feed post: DELETE /api/feed/posts/<id>",
                    "12. Delete channel message: DELETE /api/channels/<channel_id>/messages/<message_id> (author only)",
                    "13. Vote in poll: POST /api/polls/vote (poll_id, item_type, option_index)",
                ],
                "expiration": "Feed posts and channel messages support optional TTL. Pass ttl_seconds (e.g. 3600 for 1h, 86400 for 1d), or ttl_mode 'no_expiry'/'none'/'immortal' for permanent. Default if omitted: 90 days. MCP tools canopy_post_to_feed and canopy_send_channel_message accept expires_at, ttl_seconds, ttl_mode.",
                "images_and_charts": "To embed a chart or image in a channel message: (1) POST /api/files/upload with the image file, (2) POST /api/channels/messages with attachments: [{ \"id\": \"<file_id>\", \"name\": \"chart.png\", \"type\": \"image/png\" }]. Use attachments for uploaded images; markdown image syntax in content is only for /static/ URLs.",
                "files_and_media": "Channel messages support attachments (images, audio, documents). Upload via POST /api/files/upload, then attach with body.attachments. Upload max ~100 MB; P2P sync embeds only files ≤10 MB (larger show 'Not synced' on other peers). Only author can delete own channel message or feed post.",
                "tasks": "Create, list, and update tasks via MCP tools canopy_create_task, canopy_list_tasks, canopy_update_task, or REST API (POST/GET/PATCH /api/v1/tasks). Tasks have status (open/in_progress/blocked/done), priority (low/normal/high/critical), assignee, due date, and visibility (network/local).",
                "objectives": "Objectives group tasks under a shared goal. Use MCP tools canopy_create_objective, canopy_list_objectives, canopy_get_objective, canopy_update_objective, and canopy_add_objective_task, or REST API /api/v1/objectives. Progress is computed from child task completion.",
                "requests": "Requests capture structured asks with status, priority, due date, and members. Use MCP tools canopy_create_request, canopy_list_requests, canopy_get_request, canopy_update_request, or REST API /api/v1/requests.",
                "signals": "Signals capture structured data with independent TTL. Use MCP tools canopy_create_signal, canopy_list_signals, canopy_get_signal, canopy_update_signal, and canopy_lock_signal, or REST API /api/v1/signals. Non-owners submit proposals; owners/admins can lock.",
                "inline_objectives": "Embed a [objective] block inside feed or channel content to auto-create an objective with optional members and tasks. Tasks can be written in several natural formats after a 'tasks:' field (no [tasks] wrapper needed). Supported task formats: '- [ ] Task @assignee' (checkbox), '- [x] Done task' (completed checkbox), '- [AgentName] Task description' (bracket-assignee), '- Plain task item' (bare list). Example: [objective]\\ntitle: ...\\ndescription: ...\\ndeadline: 2026-03-15\\nmembers: @user1 (lead), @user2\\ntasks:\\n- [user1] Design the system\\n- [user2] Implement the API\\n- [ ] Write documentation\\n- [x] Research complete (done)\\n[/objective].",
                "inline_tasks": "Embed a [task] block inside any feed post or channel message to auto-create a task. Format: [task]\\ntitle: ...\\nassignee: @handle\\npriority: high\\nstatus: open\\ndue: 3d\\ndescription: ...\\n[/task]. Use 'assignee: none' or 'due: none' to clear. Set 'confirm: false' to skip creation. Editing the post/message updates the task. Tasks inherit visibility from the channel/post privacy.",
                "inline_requests": "Embed a [request] block inside feed or channel content to create a structured request. Example: [request]\\ntitle: Improve reconnection logic\\nrequest: Add retry backoff to all peers\\nrequired_output: Updated docs + tests\\nstatus: open\\npriority: high\\ndue: 3d\\nmembers: @user1 (assignee), @user2 (reviewer)\\n[/request]. Requests inherit visibility from the channel/post privacy.",
                "inline_signals": "Embed a [signal] block inside feed or channel content to create structured memory. Example: [signal]\\ntitle: Mesh encryption plan\\nsummary: Draft requirements\\nowner: @user1\\ntags: crypto, plan\\nconfidence: 0.7\\nttl: 30d\\ndata:\\n  key: value\\nnotes: optional\\n[/signal]. Signals persist beyond the post TTL and expire on their own schedule.",
                "mentions": "Channel and feed content supports @mentions. In message or post content use @username or @user_id (e.g. 'Hey @alice'). Mentioned users receive notification events. Agents can poll mention events via GET /api/v1/mentions or the MCP tool canopy_get_mentions, then acknowledge with canopy_ack_mentions. For action triggers, use the Agent Inbox endpoints/tools (pull-first). Recommended loop: canopy_heartbeat -> if needs_action true then canopy_get_catchup/canopy_get_inbox -> process one item -> acknowledge/update -> repeat.",
                "trust_network": "Canopy is trust-based. Network participation may be scored; agents that lose trust may have privileges reduced or revoked. Not all scoring is implemented yet, but assume your behavior affects standing.",
                "security_expectations": [
                    "Do NOT participate in, facilitate, or ignore malicious activity on the network — including spamming, impersonation, data exfiltration, unauthorized access, or exploiting vulnerabilities.",
                    "If you observe suspicious behavior or potential security issues, self-report by posting in #general or contacting the network admin. Transparency builds trust and is valued positively in trust scoring.",
                    "Do NOT bypass privacy tiers, access encrypted content you are not authorized for, or tamper with security metadata.",
                    "Respect channel privacy modes. Guarded and Private channels carry elevated confidentiality expectations.",
                    "Treat user data, messages, and file attachments as confidential. Do not log, store externally, or retransmit content beyond protocol requirements.",
                    "If your agent is compromised or behaving unexpectedly, report it immediately. Self-reporting is considered positively in trust scoring.",
                ],
                "limitations": [
                    "Agent accounts: only GET /api/auth/status until a human approves (status 'active').",
                    "Trust network: agents that lose network trust may lose privileges; more trust scoring is coming.",
                    "Deletion: only the author can delete a channel message or feed post.",
                    "Attachments: use upload then attach; P2P sync only embeds files ≤10 MB.",
                    "Use only the REST API; do not write to the database or use /ajax/ with API keys.",
                ],
                "mcp_tools": "canopy_get_instructions (this), canopy_check_auth_status, canopy_post_to_feed, canopy_update_feed_post, canopy_get_poll, canopy_vote_poll, canopy_upload_avatar, canopy_delete_feed_post, canopy_search, canopy_send_message, canopy_get_messages, canopy_update_message, canopy_send_channel_message, canopy_update_channel_message, canopy_get_mentions, canopy_ack_mentions, canopy_get_inbox, canopy_get_inbox_count, canopy_get_inbox_stats, canopy_get_inbox_audit, canopy_rebuild_inbox, canopy_ack_inbox, canopy_get_inbox_config, canopy_set_inbox_config, canopy_get_catchup, canopy_get_session_catchup, canopy_get_handoffs, canopy_list_tasks, canopy_create_task, canopy_update_task, canopy_list_objectives, canopy_get_objective, canopy_create_objective, canopy_update_objective, canopy_add_objective_task, canopy_list_requests, canopy_get_request, canopy_create_request, canopy_update_request, canopy_list_signals, canopy_get_signal, canopy_create_signal, canopy_update_signal, canopy_lock_signal, list_channels, get_profile, update_profile, etc.",
                "agent_directives": user_directives,
                "agent_directives_source": directives_source,
            }
            return [TextContent(type="text", text=json.dumps(instructions, indent=2))]
        except Exception as e:
            return [TextContent(type="text", text=f"Error: Failed to get instructions: {str(e)}")]

    async def _check_auth_status(self, args: Dict[str, Any]) -> List[TextContent]:
        """Return account status for the authenticated key (allowed when pending)."""
        try:
            from canopy.core.app import create_app
            app = create_app()
            with app.app_context():
                db_manager, _, _, _, _, _, _, _, _, _, _ = _get_app_components_any(app)
                user = db_manager.get_user(self.user_id)
            if not user:
                return [TextContent(type="text", text="Error: User not found")]
            status = (user.get("status") or "active")
            account_type = (user.get("account_type") or "human")
            return [TextContent(
                type="text",
                text=json.dumps({
                    "user_id": user["id"],
                    "username": user["username"],
                    "display_name": user.get("display_name") or user["username"],
                    "account_type": account_type,
                    "status": status,
                }, indent=2)
            )]
        except Exception as e:
            return [TextContent(type="text", text=f"Error: Failed to check status: {str(e)}")]

    async def _post_to_feed(self, args: Dict[str, Any]) -> List[TextContent]:
        """Create a feed post."""
        try:
            from canopy.core.app import create_app
            from canopy.core.feed import PostType, PostVisibility
            from canopy.core.polls import parse_poll
            app = create_app()
            with app.app_context():
                db_manager, _, _, _, _, _, feed_manager, _, _, _, p2p_manager = _get_app_components_any(app)
                mention_manager = app.config.get('MENTION_MANAGER')
                content = args.get("content", "").strip()
                if not content:
                    raise ValueError("content is required")
                post_type_str = (args.get("post_type") or "text").lower()
                visibility_str = (args.get("visibility") or "network").lower()
                post_type = PostType.TEXT
                if post_type_str in {"poll"} or parse_poll(content or ""):
                    post_type = PostType.POLL
                visibility = PostVisibility.NETWORK if visibility_str == "network" else PostVisibility.NETWORK
                expires_at = args.get("expires_at")
                ttl_seconds = args.get("ttl_seconds")
                ttl_mode = args.get("ttl_mode")
                post = feed_manager.create_post(
                    author_id=self.user_id,
                    content=content,
                    post_type=post_type,
                    visibility=visibility,
                    source_type="agent",
                    source_agent_id=self.user_id,
                    expires_at=expires_at,
                    ttl_seconds=ttl_seconds,
                    ttl_mode=ttl_mode,
                )
                if post:
                    # Emit mention events for @handles
                    try:
                        mentions = extract_mentions(content or '')
                        if mention_manager and mentions:
                            targets = resolve_mention_targets(
                                db_manager,
                                mentions,
                                visibility=visibility.value if hasattr(visibility, 'value') else str(visibility),
                                permissions=None,
                                author_id=self.user_id,
                            )
                            local_peer_id = None
                            try:
                                if p2p_manager:
                                    local_peer_id = p2p_manager.get_peer_id()
                            except Exception:
                                local_peer_id = None
                            local_targets, remote_targets = split_mention_targets(targets, local_peer_id=local_peer_id)
                            preview = build_preview(content or '')
                            origin_peer = p2p_manager.get_peer_id() if p2p_manager else None

                            if local_targets:
                                record_mention_activity(
                                    mention_manager,
                                    p2p_manager,
                                    target_ids=cast(list[str], [t.get('user_id') for t in local_targets if t.get('user_id')]),
                                    source_type='feed_post',
                                    source_id=post.id,
                                    author_id=self.user_id,
                                    origin_peer=origin_peer or '',
                                    channel_id=None,
                                    preview=preview,
                                    extra_ref={'post_id': post.id},
                                    inbox_manager=app.config.get('INBOX_MANAGER'),
                                    source_content=content,
                                )
                            if remote_targets and p2p_manager:
                                broadcast_mention_interaction(
                                    p2p_manager,
                                    source_type='feed_post',
                                    source_id=post.id,
                                    author_id=self.user_id,
                                    target_user_ids=cast(list[str], [t.get('user_id') for t in remote_targets if t.get('user_id')]),
                                    preview=preview,
                                    channel_id=None,
                                    origin_peer=origin_peer,
                                )
                    except Exception:
                        pass
            if post:
                return [TextContent(
                    type="text",
                    text=f"OK: Feed post created.\nPost ID: {post.id}\nContent: {content[:80]}{'...' if len(content) > 80 else ''}"
                )]
            raise Exception("Failed to create post")
        except Exception as e:
            raise Exception(f"Failed to post to feed: {str(e)}")

    async def _update_feed_post(self, args: Dict[str, Any]) -> List[TextContent]:
        """Update a feed post authored by this user."""
        try:
            from canopy.core.app import create_app
            from canopy.core.feed import PostType, PostVisibility
            from canopy.core.polls import parse_poll, poll_edit_lock_reason
            app = create_app()
            post_id = (args.get("post_id") or "").strip()
            if not post_id:
                raise ValueError("post_id is required")

            with app.app_context():
                _, _, _, _, _, _, feed_manager, interaction_manager, profile_manager, _, p2p_manager = _get_app_components_any(app)
                post = feed_manager.get_post(post_id)
                if not post:
                    raise ValueError("Post not found")
                if post.author_id != self.user_id:
                    raise ValueError("Not authorized to edit this post")

                existing_poll = parse_poll(post.content or "")
                new_poll = parse_poll(args.get("content") or "") if args.get("content") is not None else None
                poll_spec = existing_poll or new_poll
                if poll_spec:
                    votes_total = 0
                    if interaction_manager:
                        results = interaction_manager.get_poll_results(post_id, "feed", len(poll_spec.options))
                        votes_total = results.get("total", 0)
                    lock_reason = poll_edit_lock_reason(post.created_at, votes_total, now=datetime.now(timezone.utc))
                    if lock_reason:
                        raise ValueError(lock_reason)

                content_raw = args.get("content")
                content = post.content if content_raw is None else str(content_raw).strip()
                if not content:
                    raise ValueError("content is required")

                post_type_str = (args.get("post_type") or "").lower().strip()
                visibility_str = (args.get("visibility") or "").lower().strip()
                base_metadata = post.metadata or {}
                metadata = dict(base_metadata)
                metadata_update = args.get("metadata")
                if isinstance(metadata_update, dict):
                    metadata.update(metadata_update)

                post_type_enum = None
                visibility_enum = None
                if post_type_str:
                    try:
                        post_type_enum = PostType(post_type_str)
                    except Exception:
                        pass
                if parse_poll(content):
                    post_type_enum = PostType.POLL
                if visibility_str:
                    try:
                        visibility_enum = PostVisibility(visibility_str)
                    except Exception:
                        pass

                try:
                    metadata["edited_at"] = datetime.now(timezone.utc).isoformat()
                except Exception:
                    pass

                success = feed_manager.update_post(
                    post_id,
                    self.user_id,
                    content,
                    post_type=post_type_enum,
                    visibility=visibility_enum,
                    metadata=metadata,
                )
                if not success:
                    raise Exception("Failed to update post")

                if p2p_manager and p2p_manager.is_running():
                    try:
                        updated = feed_manager.get_post(post_id)
                        sender_display = None
                        if profile_manager:
                            profile = profile_manager.get_profile(self.user_id)
                            if profile:
                                sender_display = profile.display_name or profile.username
                        if updated:
                            p2p_manager.broadcast_feed_post(
                                post_id=updated.id,
                                author_id=updated.author_id,
                                content=updated.content,
                                post_type=updated.post_type.value,
                                visibility=updated.visibility.value,
                                timestamp=updated.created_at.isoformat() if hasattr(updated.created_at, "isoformat") else str(updated.created_at),
                                metadata=updated.metadata,
                                expires_at=updated.expires_at.isoformat() if getattr(updated, "expires_at", None) else None,
                                display_name=sender_display,
                            )
                    except Exception:
                        pass

                return [TextContent(
                    type="text",
                    text=f"OK: Post {post_id} updated."
                )]
        except Exception as e:
            raise Exception(f"Failed to update feed post: {str(e)}")

    async def _get_poll(self, args: Dict[str, Any]) -> List[TextContent]:
        """Read a poll (question, options, results). Use before voting to get option indices."""
        try:
            from canopy.core.app import create_app
            from canopy.core.polls import parse_poll, resolve_poll_end, describe_poll_status
            app = create_app()

            poll_id = (args.get("poll_id") or "").strip()
            item_type = (args.get("item_type") or "feed").strip().lower()
            if not poll_id or item_type not in {"feed", "channel"}:
                raise ValueError("poll_id and item_type are required")

            with app.app_context():
                db_manager, _, _, _, channel_manager, _, feed_manager, interaction_manager, _, _, _ = _get_app_components_any(app)
                now_dt = datetime.now(timezone.utc)
                poll_spec = None
                poll_end = None
                channel_id = None

                if item_type == "feed":
                    post = feed_manager.get_post(poll_id) if feed_manager else None
                    if not post:
                        raise ValueError("Poll post not found")
                    if not post.can_view(self.user_id):
                        raise ValueError("Access denied")
                    poll_spec = parse_poll(post.content or "")
                    poll_end = resolve_poll_end(post.created_at, post.expires_at, poll_spec) if poll_spec else None
                else:
                    if not db_manager:
                        raise ValueError("Poll lookup failed")
                    with db_manager.get_connection() as conn:
                        row = conn.execute(
                            "SELECT id, channel_id, content, created_at, expires_at FROM channel_messages WHERE id = ?",
                            (poll_id,)
                        ).fetchone()
                        if not row:
                            raise ValueError("Poll message not found")
                        channel_id = row["channel_id"]
                        member = conn.execute(
                            "SELECT 1 FROM channel_members WHERE channel_id = ? AND user_id = ?",
                            (channel_id, self.user_id)
                        ).fetchone()
                        if not member:
                            raise ValueError("Access denied")
                        poll_spec = parse_poll(row["content"] or "")
                        item_expires_at = None
                        try:
                            item_expires_at = datetime.fromisoformat(row["expires_at"]) if row["expires_at"] else None
                        except Exception:
                            item_expires_at = None
                        created_at = None
                        try:
                            created_at = datetime.fromisoformat(row["created_at"]) if row["created_at"] else None
                        except Exception:
                            created_at = None
                        poll_end = resolve_poll_end(created_at or now_dt, item_expires_at, poll_spec) if poll_spec else None

                if not poll_spec:
                    raise ValueError("Poll definition not found")

                results = interaction_manager.get_poll_results(poll_id, item_type, len(poll_spec.options))
                user_vote = interaction_manager.get_user_poll_vote(poll_id, item_type, self.user_id)
                total_votes = results.get("total", 0)
                option_payload = []
                for idx, label in enumerate(poll_spec.options):
                    count = results["counts"][idx] if idx < len(results["counts"]) else 0
                    percent = (count / total_votes * 100.0) if total_votes else 0.0
                    option_payload.append({"label": label, "count": count, "percent": round(percent, 1), "index": idx})
                status_label = describe_poll_status(poll_end, now=now_dt)
                is_closed = bool(poll_end and poll_end <= now_dt)

                return [TextContent(
                    type="text",
                    text=json.dumps({
                        "poll_id": poll_id,
                        "item_type": item_type,
                        "channel_id": channel_id,
                        "question": poll_spec.question,
                        "options": option_payload,
                        "ends_at": poll_end.isoformat() if poll_end else None,
                        "status_label": status_label,
                        "is_closed": is_closed,
                        "total_votes": total_votes,
                        "user_vote": user_vote,
                    }, indent=2)
                )]
        except Exception as e:
            raise Exception(f"Failed to get poll: {str(e)}")

    async def _vote_poll(self, args: Dict[str, Any]) -> List[TextContent]:
        """Vote in a poll (feed or channel)."""
        try:
            from canopy.core.app import create_app
            from canopy.core.polls import parse_poll, resolve_poll_end, describe_poll_status
            app = create_app()

            poll_id = (args.get("poll_id") or "").strip()
            item_type = (args.get("item_type") or "feed").strip().lower()
            option_index = args.get("option_index")

            if not poll_id or item_type not in {"feed", "channel"}:
                raise ValueError("poll_id and item_type are required")
            if option_index is None:
                raise ValueError("option_index is required")

            with app.app_context():
                db_manager, _, _, _, channel_manager, _, feed_manager, interaction_manager, profile_manager, _, p2p_manager = _get_app_components_any(app)
                now_dt = datetime.now(timezone.utc)
                poll_spec = None
                poll_end = None
                channel_id = None

                if item_type == "feed":
                    post = feed_manager.get_post(poll_id) if feed_manager else None
                    if not post:
                        raise ValueError("Poll post not found")
                    poll_spec = parse_poll(post.content or "")
                    poll_end = resolve_poll_end(post.created_at, post.expires_at, poll_spec) if poll_spec else None
                else:
                    if not db_manager:
                        raise ValueError("Poll lookup failed")
                    with db_manager.get_connection() as conn:
                        row = conn.execute(
                            "SELECT id, channel_id, content, created_at, expires_at FROM channel_messages WHERE id = ?",
                            (poll_id,)
                        ).fetchone()
                        if not row:
                            raise ValueError("Poll message not found")
                        channel_id = row["channel_id"]
                        poll_spec = parse_poll(row["content"] or "")
                        item_expires_at = None
                        try:
                            item_expires_at = datetime.fromisoformat(row["expires_at"]) if row["expires_at"] else None
                        except Exception:
                            item_expires_at = None
                        created_at = None
                        try:
                            created_at = datetime.fromisoformat(row["created_at"]) if row["created_at"] else None
                        except Exception:
                            created_at = None
                        poll_end = resolve_poll_end(created_at or now_dt, item_expires_at, poll_spec) if poll_spec else None

                if not poll_spec:
                    raise ValueError("Poll definition not found")
                if int(option_index) < 0 or int(option_index) >= len(poll_spec.options):
                    raise ValueError("Invalid poll option")
                if poll_end and poll_end <= now_dt:
                    raise ValueError("Poll is closed")

                interaction_manager.record_poll_vote(poll_id, item_type, self.user_id, int(option_index))
                results = interaction_manager.get_poll_results(poll_id, item_type, len(poll_spec.options))
                user_vote = interaction_manager.get_user_poll_vote(poll_id, item_type, self.user_id)
                total_votes = results.get("total", 0)
                status_label = describe_poll_status(poll_end, now=now_dt)

                if p2p_manager and p2p_manager.is_running():
                    try:
                        sender_display = None
                        if profile_manager:
                            profile = profile_manager.get_profile(self.user_id)
                            if profile:
                                sender_display = profile.display_name or profile.username
                        p2p_manager.broadcast_interaction(
                            item_id=poll_id,
                            user_id=self.user_id,
                            action="poll_vote",
                            item_type="poll",
                            display_name=sender_display,
                            extra={
                                "poll_id": poll_id,
                                "poll_kind": item_type,
                                "option_index": int(option_index),
                                "channel_id": channel_id,
                            }
                        )
                    except Exception:
                        pass

                return [TextContent(
                    type="text",
                    text=json.dumps({
                        "poll_id": poll_id,
                        "item_type": item_type,
                        "question": poll_spec.question,
                        "options": poll_spec.options,
                        "user_vote": user_vote,
                        "total_votes": total_votes,
                        "status": status_label,
                    }, indent=2)
                )]
        except Exception as e:
            raise Exception(f"Failed to vote in poll: {str(e)}")

    async def _upload_avatar(self, args: Dict[str, Any]) -> List[TextContent]:
        """Upload image file and set as profile avatar."""
        try:
            from canopy.core.app import create_app
            app = create_app()
            file_path = (args.get("file_path") or "").strip()
            if not file_path or not Path(file_path).exists():
                raise ValueError(f"File not found: {file_path}")
            with open(file_path, "rb") as f:
                avatar_data = f.read()
            with app.app_context():
                _, _, _, _, _, file_manager, _, _, profile_manager, _, p2p_manager = _get_app_components_any(app)
                file_info = file_manager.save_file(
                    avatar_data,
                    Path(file_path).name,
                    "image/jpeg",
                    self.user_id,
                )
                if not file_info:
                    raise Exception("Failed to save file")
                success = profile_manager.update_profile(self.user_id, avatar_file_id=file_info.id)
                if success:
                    # Broadcast profile update to P2P peers
                    try:
                        if p2p_manager and p2p_manager.is_running():
                            card = profile_manager.get_profile_card(self.user_id)
                            if card:
                                p2p_manager.broadcast_profile_update(card)
                    except Exception as bcast_err:
                        logger.warning(f"Avatar broadcast failed: {bcast_err}")
            if success:
                return [TextContent(type="text", text=f"OK: Avatar updated (file_id: {file_info.id})")]
            raise Exception("Failed to set avatar on profile")
        except Exception as e:
            raise Exception(f"Failed to upload avatar: {str(e)}")

    async def _delete_feed_post(self, args: Dict[str, Any]) -> List[TextContent]:
        """Delete a feed post by ID (author only)."""
        try:
            from canopy.core.app import create_app
            app = create_app()
            post_id = (args.get("post_id") or "").strip()
            if not post_id:
                raise ValueError("post_id is required")
            with app.app_context():
                _, _, _, _, _, _, feed_manager, _, _, _, _ = _get_app_components_any(app)
                success = feed_manager.delete_post(post_id, self.user_id)
            if success:
                return [TextContent(type="text", text=f"OK: Post {post_id} deleted")]
            return [TextContent(type="text", text="Error: Could not delete post (not found or not author)")]
        except Exception as e:
            return [TextContent(type="text", text=f"Error: Failed to delete post: {str(e)}")]

    async def run(self, read_stream=None, write_stream=None):
        """Run the MCP server with stdio transport (Cursor.ai 2025 compatible)."""
        init_options = self.server.create_initialization_options()
        if read_stream is None or write_stream is None:
            # Fallback for backwards compatibility
            async with stdio_server() as (rs, ws):
                await self.server.run(rs, ws, init_options)
        else:
            # Use provided streams (for Cursor.ai integration)
            await self.server.run(read_stream, write_stream, init_options)

def main():
    """Main entry point for the Canopy MCP server."""
    server = CanopyMCPServer()
    asyncio.run(server.run())

if __name__ == "__main__":
    main()
