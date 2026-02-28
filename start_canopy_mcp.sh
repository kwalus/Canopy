#!/bin/bash
# Start Canopy MCP Server as background service

# Auto-detect project directory (where this script lives)
CANOPY_DIR="$(cd "$(dirname "$0")" && pwd)"
PID_FILE="/tmp/canopy_mcp.pid"
LOG_FILE="/tmp/canopy_mcp.log"
SERVER_SCRIPT="canopy_mcp_server.py"
PORT=8030

# Check if server is already running
if [ -f "$PID_FILE" ]; then
    PID=$(cat "$PID_FILE")
    if ps -p $PID > /dev/null 2>&1; then
        echo "Canopy MCP Server is already running with PID $PID."
        exit 0
    else
        echo "Stale PID file found. Removing it."
        rm "$PID_FILE"
    fi
fi

# Check for API key
if [ -z "$CANOPY_API_KEY" ]; then
    echo "⚠️  CANOPY_API_KEY not set."
    echo "   Create API key in Canopy UI: http://localhost:7770 → API Keys"
    echo "   Then set: export CANOPY_API_KEY='your_key_here'"
    echo ""
    echo "   Starting server anyway (some tools may not work without API key)..."
fi

echo "Starting Canopy MCP Server on port $PORT..."
cd "$CANOPY_DIR"
nohup python3 "$SERVER_SCRIPT" --port "$PORT" > "$LOG_FILE" 2>&1 &
echo $! > "$PID_FILE"
echo "Canopy MCP Server started with PID $(cat "$PID_FILE")."
echo "Logs: $LOG_FILE"
echo ""
echo "Register with MCP Manager (if using MCP Manager):"
echo "  python3 infrastructure/mcp_manager/register_all_servers.py"

