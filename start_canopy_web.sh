#!/bin/bash
# Start Canopy Web UI as background service
# Compatible with macOS and Linux

# Auto-detect project directory (where this script lives)
CANOPY_DIR="$(cd "$(dirname "$0")" && pwd)"
INSTANCE_NAME="$(basename "$CANOPY_DIR" | tr '[:upper:]' '[:lower:]' | tr -cs 'a-z0-9._-' '_')"
PID_FILE="/tmp/${INSTANCE_NAME}_canopy_web.pid"
LOG_FILE="/tmp/${INSTANCE_NAME}_canopy_web.log"
HOST="0.0.0.0"
PORT=7770
STARTUP_TIMEOUT_SECONDS=20

healthcheck() {
    curl -fsS "http://127.0.0.1:${PORT}/login" >/dev/null 2>&1
}

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

# Use local virtualenv if present; otherwise fall back to system python.
if [ -x "$CANOPY_DIR/.venv/bin/python3" ]; then
    PYTHON="$CANOPY_DIR/.venv/bin/python3"
elif [ -x "$CANOPY_DIR/.venv/bin/python" ]; then
    PYTHON="$CANOPY_DIR/.venv/bin/python"
elif [ -x "$CANOPY_DIR/venv/bin/python3" ]; then
    PYTHON="$CANOPY_DIR/venv/bin/python3"
elif [ -x "$CANOPY_DIR/venv/bin/python" ]; then
    PYTHON="$CANOPY_DIR/venv/bin/python"
else
    echo "Note: No local virtualenv found. Using system python. For a clean setup, run ./install.sh first."
    PYTHON="python3"
fi

nohup "$PYTHON" -m canopy.main --host "$HOST" --port "$PORT" > "$LOG_FILE" 2>&1 &
echo $! > "$PID_FILE"
PID="$(cat "$PID_FILE")"

for _ in $(seq 1 "$STARTUP_TIMEOUT_SECONDS"); do
    if ! kill -0 "$PID" 2>/dev/null; then
        break
    fi
    if healthcheck; then
        echo "Canopy Web UI started with PID $PID."
        echo "   Access at: http://localhost:$PORT"
        echo "   Logs: $LOG_FILE"
        exit 0
    fi
    sleep 1
done

echo "Failed to start Canopy Web UI."
echo "   Check logs: $LOG_FILE"
if kill -0 "$PID" 2>/dev/null; then
    kill "$PID" 2>/dev/null || true
    sleep 1
    kill -9 "$PID" 2>/dev/null || true
fi
rm -f "$PID_FILE"
exit 1
