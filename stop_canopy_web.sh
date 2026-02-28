#!/bin/bash
# Stop Canopy Web UI
# Compatible with macOS and Linux

PID_FILE="/tmp/canopy_web.pid"

if [ -f "$PID_FILE" ]; then
    PID=$(cat "$PID_FILE")
    if kill -0 "$PID" 2>/dev/null; then
        echo "Stopping Canopy Web UI (PID: $PID)..."
        kill "$PID"
        # Give it a moment to shut down
        sleep 2
        if kill -0 "$PID" 2>/dev/null; then
            echo "Canopy Web UI (PID: $PID) did not stop gracefully. Force killing..."
            kill -9 "$PID"
        fi
        rm "$PID_FILE"
        echo "Canopy Web UI stopped."
    else
        echo "No running Canopy Web UI found with PID from $PID_FILE. Removing stale PID file."
        rm "$PID_FILE"
    fi
else
    echo "Canopy Web UI is not running (PID file not found)."
fi
