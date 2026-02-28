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
# Stable secret key — sessions survive restarts during a test session.
TESTNET_SECRET_KEY="0cc6b6d05b9c6cb3b2d369dcfeae2d83d3b00d95ad47d03b9f857c5c6b0793ac"

# --reset flag: wipe the testnet database and start fresh
if [[ "${1:-}" == "--reset" ]]; then
    echo "⚠️  Resetting testnet — wiping database..."
    rm -f "$TESTNET_DB"
    echo "   Cleared: $TESTNET_DB"
fi

mkdir -p "$TESTNET_DIR"

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
