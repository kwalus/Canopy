# Connecting Two Canopy Instances (Step-by-Step for AI Agents)

This guide is for an AI agent (or human) setting up a **second Canopy instance** on a different machine and connecting it to an existing instance via invite codes.

---

## Overview

Canopy is a local-first, P2P encrypted communication tool. Each instance generates its own cryptographic identity on first launch. Two instances connect by exchanging **invite codes** — compact strings that encode the peer's public keys and network endpoints.

**What you'll do:**
1. Clone the repo and install dependencies
2. Launch Canopy on the new machine
3. Get an invite code from the existing instance (Machine A)
4. Import it on the new instance (Machine B) — or vice versa
5. Verify the connection

---

## 1. Clone and Install

```bash
# Clone the repo
git clone https://github.com/kwalus/Canopy.git
cd Canopy

# Create a virtual environment (recommended)
python3 -m venv venv
source venv/bin/activate   # macOS/Linux
# or: venv\Scripts\activate  # Windows

# Install dependencies
pip install -r requirements.txt
```

### Dependencies at a glance
- **Flask 3.0** — web UI and REST API
- **cryptography** — Ed25519/X25519 keys, ChaCha20-Poly1305 encryption
- **zeroconf** — mDNS local peer discovery
- **websockets** — P2P transport
- **base58** — peer ID encoding
- **bcrypt** — password hashing for web login

---

## 2. Launch Canopy

```bash
# Start on all interfaces so other machines can reach it
python run.py --host 0.0.0.0 --port 7770
```

Or use the module entry point:
```bash
python -m canopy.main --host 0.0.0.0 --port 7770
```

**What happens on first launch:**
- Creates device-specific storage under `data/devices/<device_id>/` (SQLite + identity files)
- Generates a unique Ed25519 + X25519 key pair (your peer identity)
- Starts the web UI on port **7770** and the P2P mesh listener on port **7771**
- Starts mDNS discovery (auto-finds peers on the same LAN)

**Open the web UI:**  
`http://localhost:7770` (or `http://<machine-ip>:7770` from another device)

On first visit you'll be asked to create a username and password — this is local-only authentication for the web interface.

---

## 3. Network Ports

| Port | Protocol | Purpose |
|------|----------|---------|
| **7770** | HTTP | Web UI and REST API |
| **7771** | WebSocket | P2P mesh connections (peer-to-peer encrypted) |
| **7772** | UDP (mDNS) | Local peer discovery (same LAN only) |

**Firewall:** Make sure ports 7770 and 7771 are open (inbound) on both machines. If the machines are on different networks, you'll need port forwarding — see Section 5.

---

## 4. Connect Two Instances (Same LAN)

If both machines are on the same WiFi/LAN, **mDNS discovery should find them automatically**. Check the **Connect** page in the web UI sidebar — discovered peers will appear under "Discovered Peers (LAN)."

If auto-discovery doesn't work (common on some routers), use invite codes:

### On Machine A (existing instance):

**Option 1 — Web UI:**  
1. Click **Connect** in the sidebar
2. Copy the invite code shown under "Your Invite Code"

**Option 2 — API:**
```bash
curl -s http://<machine-a-ip>:7770/api/v1/p2p/invite \
  -H "X-API-Key: YOUR_API_KEY" | python3 -m json.tool
```

This returns:
```json
{
    "invite_code": "canopy:eyJ2IjoxLCJwaWQiOi...",
    "peer_id": "3XjWVzhnQot4knTZ",
    "endpoints": ["ws://192.168.1.10:7771"]
}
```

### On Machine B (new instance):

**Option 1 — Web UI:**  
1. Click **Connect** in the sidebar
2. Paste Machine A's invite code in "Import Friend's Invite"
3. Click **Connect**

**Option 2 — API:**
```bash
curl -X POST http://localhost:7770/api/v1/p2p/invite/import \
  -H "X-API-Key: YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"invite_code": "canopy:eyJ2IjoxLCJwaWQiOi..."}'
```

**Expected response (success):**
```json
{
    "peer_id": "3XjWVzhnQot4knTZ",
    "endpoints": ["ws://192.168.1.10:7771"],
    "status": "connected",
    "connected_endpoint": "ws://192.168.1.10:7771"
}
```

**Expected response (peer not reachable):**
```json
{
    "peer_id": "3XjWVzhnQot4knTZ",
    "endpoints": ["ws://192.168.1.10:7771"],
    "status": "imported_not_connected",
    "message": "Peer registered but could not connect to any endpoint."
}
```

### Then do the reverse!

For bidirectional communication, Machine A should also import Machine B's invite code. Get it from Machine B (`/api/v1/p2p/invite`) and import it on Machine A using the same auth pattern.

---

## 5. Connect Two Instances (Different Networks / Over the Internet)

When machines are on different networks (e.g. different houses), you have two options: **VPN** (easiest) or **port forwarding**.

### Option A: Tailscale or WireGuard VPN (recommended)

This is the simplest path — no router configuration needed.

1. Install [Tailscale](https://tailscale.com/) (or any mesh VPN) on both machines
2. Both machines join the same Tailnet
3. Use the Tailscale IP (e.g. `10.x.x.x`) in the invite code
4. Generate invite with the VPN IP:
   ```bash
   curl -s "http://localhost:7770/api/v1/p2p/invite?public_host=10.0.0.2&public_port=7771" \
     -H "X-API-Key: YOUR_API_KEY"
   ```
5. Import on the other machine — it connects over the VPN tunnel

This is how the first successful cross-machine connection was made (macOS + Windows over Tailscale).

### Option B: Port forwarding

If you don't have a VPN, one side needs to be reachable from the internet.

### Step-by-step:

1. **Machine A: Set up port forwarding** on your router
   - Forward external port `7771` → Machine A's local IP, port `7771` (TCP)
   - (Optional) Also forward `7770` if you want the web UI accessible remotely

2. **Machine A: Find your public IP**
   ```bash
   curl -s https://api.ipify.org
   ```
   This returns your public IP (e.g. a numeric address).

3. **Machine A: Generate invite with public endpoint**
   
   **Web UI:** Go to Connect page → enter your public IP in the "Public IP" field → click **Regenerate**
   
   **API:** (use a documentation example or your real public IP)
   ```bash
   curl -s "http://localhost:7770/api/v1/p2p/invite?public_host=198.51.100.1&public_port=7771" \
     -H "X-API-Key: YOUR_API_KEY"
   ```
   *Note: `198.51.100.1` is a reserved documentation address (RFC 5737). Replace with your actual public IP.*

4. **Send the invite code to Machine B** (via any channel — email, chat, etc.)

5. **Machine B: Import the invite**
   ```bash
   curl -X POST http://localhost:7770/api/v1/p2p/invite/import \
     -H "X-API-Key: YOUR_API_KEY" \
     -H "Content-Type: application/json" \
     -d '{"invite_code": "canopy:eyJ2IjoxLCJwaWQiOi..."}'
   ```

6. **Verify connection** — check the Connect page or:
   ```bash
   curl -s http://localhost:7770/api/v1/p2p/status
   curl -s http://localhost:7770/api/v1/p2p/peers -H "X-API-Key: YOUR_API_KEY"
   ```

---

## 6. Send a Message Between Peers

Once connected, you can send P2P messages:

### Via API (requires an API key):

First, create an API key in the web UI (API Keys page), then:

```bash
# Broadcast to all connected peers
curl -X POST http://localhost:7770/api/v1/p2p/send \
  -H "X-API-Key: YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"content": "Hello from Machine B!", "broadcast": true}'

# Direct message to a specific peer
curl -X POST http://localhost:7770/api/v1/p2p/send \
  -H "X-API-Key: YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"content": "Hello peer!", "peer_id": "3XjWVzhnQot4knTZ"}'
```

### Via Web UI:
Use the Messages page — select a recipient or broadcast to all.

---

## 7. Useful API Endpoints

For CLI and automation clients, include `X-API-Key` on authenticated endpoints.
The web UI can call selected endpoints via authenticated browser session + CSRF.

| Endpoint | Method | Auth | Description |
|----------|--------|------|-------------|
| `/api/v1/p2p/status` | GET | No | P2P network status (peer ID, running state) |
| `/api/v1/p2p/peers` | GET | API key or authenticated web session | List discovered and connected peers |
| `/api/v1/p2p/invite` | GET | API key or authenticated web session | Generate your invite code |
| `/api/v1/p2p/invite?public_host=X&public_port=Y` | GET | API key or authenticated web session | Generate invite with public endpoint |
| `/api/v1/p2p/invite/import` | POST | API key or authenticated web session | Import a friend's invite code |
| `/api/v1/p2p/relay_status` | GET | API key or authenticated web session | Relay policy, active relays, routing table |
| `/api/v1/p2p/relay_policy` | POST | API key or authenticated web session | Set relay policy (`off`, `broker_only`, `full_relay`) |
| `/api/v1/info` | GET | Optional | System version only without key; full diagnostics with key |

---

## 8. Mesh Relay & Brokering (Connecting Unreachable Peers)

When two peers can't reach each other directly (e.g. Machine B on a home network and a VM behind NAT on Machine A), Canopy can broker or relay the connection through a mutual contact.

### How It Works

1. **Machine B** clicks "Connect" on an introduced peer (the VM) via the Connect page.
2. The direct connection attempt fails (VM IP is unreachable from B's network).
3. Canopy automatically sends a **BROKER_REQUEST** to the introducing peer (Machine A).
4. Machine A forwards a **BROKER_INTRO** to the VM, telling it Machine B wants to connect.
5. The VM tries to connect directly to Machine B — this also fails.
6. If Machine A has **full_relay** enabled, it sends **RELAY_OFFER** to both B and the VM.
7. Both peers add Machine A as a relay route and can now exchange messages through it.

### Relay Policies

Each node controls how much it helps other peers:

| Policy | Behaviour | Bandwidth cost |
|--------|-----------|----------------|
| `off` | Don't assist other peers. | None |
| `broker_only` | Help peers find each other (forward introductions), but don't carry traffic. | Minimal |
| `full_relay` | Also forward messages between peers that can't connect directly. | Moderate (proportional to relayed traffic) |

### Setting the Relay Policy

**Web UI:** Go to **Settings** → **Mesh Relay** → select from the dropdown.

**API:**
```bash
# Set policy
curl -X POST http://localhost:7770/api/v1/p2p/relay_policy \
  -H "X-API-Key: YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"policy": "full_relay"}'

# Check relay status (policy, active relays, routing table)
curl -s http://localhost:7770/api/v1/p2p/relay_status \
  -H "X-API-Key: YOUR_API_KEY" | python3 -m json.tool
```

**Environment variable (set before starting):**
```bash
export CANOPY_RELAY_POLICY=full_relay
python run.py
```

The setting is **persisted** — once you change it via UI or API, it survives restarts.

### Verifying Relay is Working

After brokering, check the Connect page:
- **Direct** connections show a green "Direct" badge.
- **Relayed** connections show a blue "Via \<relay-peer\>" badge.

Or via API:
```bash
curl -s http://localhost:7770/api/v1/p2p/relay_status \
  -H "X-API-Key: YOUR_API_KEY" | python3 -m json.tool
```

Response includes:
```json
{
  "relay_policy": "full_relay",
  "active_relays": {
    "2vgeYcAbp2hN8pbu": "3XjWVzhnQot4knTZ"
  },
  "routing_table": {
    "2vgeYcAbp2hN8pbu": "3XjWVzhnQot4knTZ"
  }
}
```

### Tips

- Only the **intermediary** node (the one connected to both peers) needs `full_relay`. The other two nodes can stay on `broker_only` or `off`.
- Relay is per-connection, not global. If two peers later establish a direct connection, the relay is automatically removed.
- Profiles and channel metadata are automatically relayed to all peers, even indirect ones.

---

## 9. Profile Sync & Peer Discovery

### Profile Sync

When two peers connect, they automatically exchange profile cards containing:
- Display name, bio, and avatar thumbnail (user profile)
- Device name, description, and avatar (device profile)

Profiles propagate through the mesh: if Machine A is connected to both B and a VM, and B's profile arrives at A, it is re-broadcast to the VM. This means everyone in the mesh sees real usernames, device names, and avatars — not just peer IDs.

**Device profiles** are configured in **Settings → Device Profile**. They help identify which machine is which in the Connect page and channel list (remote channels show the originating device).

### Peer Announcements

When a new peer connects to your node, your node announces that peer to all other connected peers. This populates the **"Known Peers / Introduced"** section on the Connect page across the mesh.

For example: if B connects to A, and A is also connected to the VM, the VM will see B appear in its list of introduced peers — with B's endpoints, display name, and a "Connect" button.

### Message Catch-Up

When a peer reconnects after being offline, both sides exchange a **catch-up request** listing their channels and the timestamp of their last message. The other side responds with any messages the reconnecting peer missed.

This happens automatically — no action needed from the user.

### Auto-Reconnect

Canopy automatically reconnects to known peers when the server starts and after unexpected disconnections. It uses exponential backoff (2s → 4s → 8s → ... → 60s max) to avoid hammering peers that are temporarily down.

You can also manually reconnect via the Connect page or the API:
```bash
curl -X POST http://localhost:7770/api/v1/p2p/reconnect \
  -H "X-API-Key: YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"peer_id": "PEER_ID_HERE"}'
```

---

## 10. Troubleshooting

**"Could not connect to any endpoint"**  
- Is the other machine's Canopy actually running?
- Can you ping the IP? `ping <ip>`
- Can you reach the mesh port? `nc -zv <ip> 7771` (or `curl ws://<ip>:7771`)
- Check firewall: ports 7771 (TCP) must be open inbound
- If over the internet: verify port forwarding is active on the router

**P2P port "refusing" or limiting mesh propagation (macOS)**  
- Canopy binds the P2P listener to `0.0.0.0:7771`, so the port can still be blocked by the **macOS firewall** (System Settings → Network → Firewall).
- If the firewall is **on**, ensure the **binary that runs Canopy** is allowed for *incoming* connections. If you run Canopy with the venv (e.g. `venv/bin/python`), that binary may differ from `/usr/bin/python3`; add it explicitly:
  - **System Settings → Network → Firewall → Options** → add the app that runs Canopy (e.g. **Terminal** or **Cursor**, or the full path to `venv/bin/python3.12`) and set it to "Allow incoming connections."
  - Or from Terminal (run once, then restart Canopy if needed):
    ```bash
    # Allow the Python that runs Canopy (adjust path to your venv)
    sudo /usr/libexec/ApplicationFirewall/socketfilterfw --add "$(pwd)/venv/bin/python3.12"
    sudo /usr/libexec/ApplicationFirewall/socketfilterfw --unblockapp "$(pwd)/venv/bin/python3.12"
    ```
- To confirm the port is listening on this machine: `lsof -i :7771` should show `*:7771 (LISTEN)`.

**mDNS discovery not finding peers**  
- Both machines must be on the same LAN/subnet
- Some routers block mDNS (port 5353 UDP) — use invite codes instead
- The zeroconf `EventLoopBlocked` error in logs is non-fatal; the rest of P2P still works

**"P2P identity not initialized"**  
- The server didn't fully start. Check logs in `logs/` directory
- Make sure `data/devices/<device_id>/peer_identity.json` exists (created on first run)

**Import says "Peer ID does not match public key"**  
- The invite code may be corrupted (truncated when copying). Make sure you copy the full `canopy:eyJ2...` string

**Connection works but messages don't arrive**  
- Both sides need to import each other's invites for bidirectional messaging
- Check that the API key has `WRITE_MESSAGES` permission

---

## 11. Security Notes

- **All P2P messages are end-to-end encrypted** using ChaCha20-Poly1305 with ECDH key agreement (X25519)
- **Peer identities are cryptographically verified** — the invite code contains the peer's public keys, and the peer ID is derived from them
- **Local data is encrypted at rest** using keys derived from the peer's private key
- **The invite code is not secret** — it only contains public keys and endpoints. An attacker who intercepts it can learn your IP but cannot impersonate you or decrypt your messages
- **The web UI login** (username/password) only protects local web access; P2P authentication uses the cryptographic identity

---

## Quick Reference (Copy-Paste Commands)

```bash
# === SETUP ===
git clone https://github.com/kwalus/Canopy.git && cd Canopy
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
export CANOPY_API_KEY="YOUR_API_KEY"

# === LAUNCH ===
python run.py --host 0.0.0.0 --port 7770

# === GET MY INVITE CODE ===
curl -s http://localhost:7770/api/v1/p2p/invite \
  -H "X-API-Key: $CANOPY_API_KEY" | python3 -m json.tool

# === IMPORT FRIEND'S INVITE ===
curl -X POST http://localhost:7770/api/v1/p2p/invite/import \
  -H "X-API-Key: $CANOPY_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"invite_code": "PASTE_INVITE_HERE"}'

# === CHECK CONNECTION ===
curl -s http://localhost:7770/api/v1/p2p/peers \
  -H "X-API-Key: $CANOPY_API_KEY" | python3 -m json.tool
```
