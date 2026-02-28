#!/bin/bash
# One-command setup for Canopy: detect Python, create venv, install deps, init DB.
# Compatible with macOS and Linux.

set -e
CANOPY_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$CANOPY_DIR"

# Detect Python 3.10+
for py in python3.12 python3.11 python3.10 python3; do
    if command -v "$py" >/dev/null 2>&1; then
        ver=$("$py" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null || true)
        if [ -n "$ver" ]; then
            major=$(echo "$ver" | cut -d. -f1)
            minor=$(echo "$ver" | cut -d. -f2)
            if [ "$major" -eq 3 ] && [ "$minor" -ge 10 ]; then
                PYTHON="$py"
                break
            fi
        fi
    fi
done

if [ -z "$PYTHON" ]; then
    echo "Canopy requires Python 3.10 or newer."
    echo "Install it from python.org or your package manager, then run this script again."
    exit 1
fi

echo "Using: $($PYTHON --version)"

# Create venv if missing
if [ ! -d "$CANOPY_DIR/venv" ]; then
    echo "Creating virtual environment..."
    "$PYTHON" -m venv "$CANOPY_DIR/venv"
fi

# Use venv Python
PYTHON="$CANOPY_DIR/venv/bin/python"
PIP="$CANOPY_DIR/venv/bin/pip"

echo "Installing dependencies..."
"$PIP" install -q --upgrade pip
"$PIP" install -q -r requirements.txt

# Trigger DB init and migrations (create_app runs migrations on first load)
echo "Initializing database..."
"$PYTHON" -c "
from canopy.core.app import create_app
from canopy.core.config import Config
create_app(Config.from_env())
print('Database ready.')
"

echo ""
echo "Ready. Run ./start_canopy_web.sh to launch."
echo ""
