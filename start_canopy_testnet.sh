#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# start_canopy_testnet.sh — Launch a clean, isolated Canopy testnet instance
#
# Runs on port 7780 (web) with its own fresh database and NO P2P mesh.
# CANOPY_DISABLE_MESH=true means it never auto-joins the main instance or
# any other peer on the LAN.  Agents connect via HTTP API keys only.
#
# This does NOT touch or interfere with the main instance (7770).
#
# Usage:
#   ./start_canopy_testnet.sh           # start testnet (preserves existing DB)
#   ./start_canopy_testnet.sh --reset   # wipe DB and start completely fresh
# ---------------------------------------------------------------------------

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

TESTNET_DIR="$SCRIPT_DIR/data/testnet"
TESTNET_DB="$TESTNET_DIR/canopy.db"
TESTNET_PORT=7780
TESTNET_SECRET_KEY_FILE="$TESTNET_DIR/secret_key"

# --reset flag: wipe the testnet database and start fresh
if [[ "${1:-}" == "--reset" ]]; then
    echo "⚠️  Resetting testnet — wiping database..."
    rm -f "$TESTNET_DB"
    echo "   Cleared: $TESTNET_DB"
fi

mkdir -p "$TESTNET_DIR"

# Stable secret key — generated once and persisted so sessions survive restarts.
if [[ ! -f "$TESTNET_SECRET_KEY_FILE" ]]; then
    if ! python3 -c "import secrets; print(secrets.token_hex(32))" > "$TESTNET_SECRET_KEY_FILE"; then
        echo "ERROR: Failed to generate secret key. Is python3 installed?" >&2
        exit 1
    fi
    chmod 600 "$TESTNET_SECRET_KEY_FILE"
fi
TESTNET_SECRET_KEY="$(cat "$TESTNET_SECRET_KEY_FILE")"
if [[ ! "$TESTNET_SECRET_KEY" =~ ^[0-9a-f]{64}$ ]]; then
    echo "ERROR: Secret key file '$TESTNET_SECRET_KEY_FILE' is missing or corrupt." >&2
    echo "       Delete the file and re-run to generate a new key." >&2
    exit 1
fi

echo ""
echo "🌿 Canopy TESTNET (standalone, mesh-off)"
echo "   Web UI  : http://localhost:$TESTNET_PORT"
echo "   P2P mesh: DISABLED (will not join main instance or LAN peers)"
echo "   Database: $TESTNET_DB"
echo ""
if [[ ! -f "$TESTNET_DB" ]]; then
    echo "   ➜  First run! Open http://localhost:$TESTNET_PORT and register"
    echo "      the admin account, then set up channels + agent API keys."
fi
echo "   Press Ctrl+C to stop."
echo ""

# CANOPY_DISABLE_MESH prevents auto-joining the main mesh on the same machine
export CANOPY_DISABLE_MESH=true
export CANOPY_PORT="$TESTNET_PORT"
export CANOPY_DATA_DIR="$TESTNET_DIR"
export CANOPY_DATABASE_PATH="$TESTNET_DB"
export CANOPY_SECRET_KEY="$TESTNET_SECRET_KEY"

python3 run.py
