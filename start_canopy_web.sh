#!/bin/bash
# Start Canopy Web UI as background service
# Compatible with macOS and Linux

# Auto-detect project directory (where this script lives)
CANOPY_DIR="$(cd "$(dirname "$0")" && pwd)"
PID_FILE="/tmp/canopy_web.pid"
LOG_FILE="/tmp/canopy_web.log"
HOST="0.0.0.0"
PORT=7770

# Check if server is already running
if [ -f "$PID_FILE" ]; then
    PID=$(cat "$PID_FILE")
    if kill -0 "$PID" 2>/dev/null; then
        echo "Canopy Web UI is already running with PID $PID."
        echo "Access at: http://localhost:$PORT"
        exit 0
    else
        echo "Stale PID file found. Removing it."
        rm "$PID_FILE"
    fi
fi

# Check if port is already in use (compatible with macOS and Linux)
if lsof -i :"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
    echo "WARNING: Port $PORT is already in use."
    echo "   Canopy may already be running, or another service is using the port."
    exit 1
fi

echo "Starting Canopy Web UI on $HOST:$PORT..."
cd "$CANOPY_DIR"

# Use venv if present; otherwise fall back to system python (backward compatible)
if [ -f "$CANOPY_DIR/venv/bin/python3" ]; then
    PYTHON="$CANOPY_DIR/venv/bin/python3"
else
    echo "Note: No venv found. Using system python. For a clean setup, run ./install.sh first."
    PYTHON="python3"
fi

nohup "$PYTHON" -m canopy.main --host "$HOST" --port "$PORT" > "$LOG_FILE" 2>&1 &
echo $! > "$PID_FILE"
sleep 2

if kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    echo "Canopy Web UI started with PID $(cat "$PID_FILE")."
    echo "   Access at: http://localhost:$PORT"
    echo "   Logs: $LOG_FILE"
else
    echo "Failed to start Canopy Web UI."
    echo "   Check logs: $LOG_FILE"
    rm "$PID_FILE"
    exit 1
fi
