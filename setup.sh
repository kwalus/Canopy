#!/bin/bash
# ============================================================
# Canopy Setup & Start Script
# ============================================================
# Works on macOS and Linux (including VMs synced via Dropbox).
#
# Usage:
#   ./setup.sh              # Install deps + start server
#   ./setup.sh --start      # Just start (skip install)
#   ./setup.sh --stop       # Stop the server
#   ./setup.sh --restart    # Stop + start
#   ./setup.sh --status     # Check if running
#   ./setup.sh --install    # Install deps only (no start)
# ============================================================

set -e

CANOPY_DIR="$(cd "$(dirname "$0")" && pwd)"
PID_FILE="$CANOPY_DIR/data/canopy_web.pid"
LOG_FILE="$CANOPY_DIR/logs/canopy.log"
HOST="0.0.0.0"
PORT=7770

# Colors (if terminal supports them)
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

info()  { echo -e "${CYAN}[INFO]${NC} $1"; }
ok()    { echo -e "${GREEN}[OK]${NC} $1"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
err()   { echo -e "${RED}[ERROR]${NC} $1"; }

# ----------------------------------------------------------
# Find Python
# ----------------------------------------------------------
find_python() {
    if [ -f "$CANOPY_DIR/venv/bin/python3" ]; then
        echo "$CANOPY_DIR/venv/bin/python3"
    elif command -v python3 &>/dev/null; then
        echo "python3"
    elif command -v python &>/dev/null; then
        echo "python"
    else
        err "Python 3 not found. Please install Python 3.9+."
        exit 1
    fi
}

PYTHON=$(find_python)

# ----------------------------------------------------------
# Install dependencies
# ----------------------------------------------------------
do_install() {
    info "Installing Canopy dependencies..."
    cd "$CANOPY_DIR"

    # Create venv if it doesn't exist
    if [ ! -d "$CANOPY_DIR/venv" ]; then
        info "Creating virtual environment..."
        $PYTHON -m venv venv 2>/dev/null || {
            warn "Could not create venv, installing to system Python"
        }
    fi

    # Re-detect python after venv creation
    PYTHON=$(find_python)

    # Install requirements
    if [ -f "$CANOPY_DIR/requirements.txt" ]; then
        info "Installing from requirements.txt..."
        $PYTHON -m pip install --quiet --upgrade pip 2>/dev/null || true
        $PYTHON -m pip install --quiet -r requirements.txt 2>&1 | tail -5
        ok "Dependencies installed"
    else
        err "requirements.txt not found in $CANOPY_DIR"
        exit 1
    fi

    # Ensure data and logs directories exist
    mkdir -p "$CANOPY_DIR/data"
    mkdir -p "$CANOPY_DIR/logs"
}

# ----------------------------------------------------------
# Stop server
# ----------------------------------------------------------
do_stop() {
    # Kill by PID file
    if [ -f "$PID_FILE" ]; then
        PID=$(cat "$PID_FILE")
        if kill -0 "$PID" 2>/dev/null; then
            info "Stopping Canopy (PID: $PID)..."
            kill "$PID" 2>/dev/null
            sleep 2
            if kill -0 "$PID" 2>/dev/null; then
                warn "Force killing PID $PID..."
                kill -9 "$PID" 2>/dev/null
            fi
            ok "Canopy stopped"
        fi
        rm -f "$PID_FILE"
    fi

    # Also kill anything on the port (catches orphan processes)
    if lsof -ti :"$PORT" >/dev/null 2>&1; then
        info "Killing process on port $PORT..."
        lsof -ti :"$PORT" | xargs kill 2>/dev/null
        sleep 1
        lsof -ti :"$PORT" | xargs kill -9 2>/dev/null || true
    fi
}

# ----------------------------------------------------------
# Start server
# ----------------------------------------------------------
do_start() {
    cd "$CANOPY_DIR"
    PYTHON=$(find_python)

    # Check if already running
    if [ -f "$PID_FILE" ]; then
        PID=$(cat "$PID_FILE")
        if kill -0 "$PID" 2>/dev/null; then
            ok "Canopy is already running (PID: $PID)"
            echo "   Access at: http://localhost:$PORT"
            return 0
        fi
        rm -f "$PID_FILE"
    fi

    # Check port
    if lsof -ti :"$PORT" >/dev/null 2>&1; then
        warn "Port $PORT is in use. Killing existing process..."
        lsof -ti :"$PORT" | xargs kill 2>/dev/null
        sleep 2
    fi

    # Ensure directories
    mkdir -p "$CANOPY_DIR/data"
    mkdir -p "$CANOPY_DIR/logs"

    info "Starting Canopy on $HOST:$PORT..."
    nohup $PYTHON -m canopy.main --host "$HOST" --port "$PORT" >> "$LOG_FILE" 2>&1 &
    echo $! > "$PID_FILE"
    sleep 3

    if kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
        ok "Canopy started (PID: $(cat "$PID_FILE"))"
        echo "   Web UI:  http://localhost:$PORT"
        echo "   Logs:    $LOG_FILE"
        echo "   PID:     $PID_FILE"
    else
        err "Failed to start. Check logs:"
        echo "   tail -30 $LOG_FILE"
        rm -f "$PID_FILE"
        exit 1
    fi
}

# ----------------------------------------------------------
# Status check
# ----------------------------------------------------------
do_status() {
    if [ -f "$PID_FILE" ]; then
        PID=$(cat "$PID_FILE")
        if kill -0 "$PID" 2>/dev/null; then
            ok "Canopy is running (PID: $PID)"
            echo "   http://localhost:$PORT"
            return 0
        fi
    fi

    if lsof -ti :"$PORT" >/dev/null 2>&1; then
        PID=$(lsof -ti :"$PORT")
        warn "Something is running on port $PORT (PID: $PID) but not tracked by PID file"
        return 0
    fi

    info "Canopy is not running"
    return 1
}

# ----------------------------------------------------------
# Main
# ----------------------------------------------------------
case "${1:-}" in
    --stop)
        do_stop
        ;;
    --start)
        do_start
        ;;
    --restart)
        do_stop
        do_start
        ;;
    --status)
        do_status
        ;;
    --install)
        do_install
        ;;
    --help|-h)
        echo "Canopy Setup & Start Script"
        echo ""
        echo "Usage: ./setup.sh [OPTION]"
        echo ""
        echo "Options:"
        echo "  (none)       Install dependencies + start server"
        echo "  --start      Start server (skip install)"
        echo "  --stop       Stop server"
        echo "  --restart    Stop + start server"
        echo "  --status     Check if server is running"
        echo "  --install    Install dependencies only"
        echo "  --help       Show this help"
        ;;
    *)
        # Default: install + start
        do_install
        do_start
        ;;
esac
