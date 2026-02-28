# Connect Page FAQ and Feature Reference

This document explains exactly what each section/button on the **Connect** page does, and clarifies common confusion around endpoint addresses, public IP usage, and authentication errors.

---

## Page purpose

The Connect page is the operational control center for:

- generating shareable invite codes,
- importing and connecting to peer invites,
- monitoring peer connectivity state,
- reconnect/disconnect/forget workflows,
- mesh diagnostics and connection history.

---

## Section-by-section reference

### 1) Your Invite Code

What it shows:

- **Peer ID**: your local cryptographic identity.
- **Reachable at**: one or more `ws://host:port` endpoint candidates included in your invite.
- **Invite code**: compact `canopy:...` payload containing identity + endpoint list.

Buttons/actions:

- **Copy**: copies the full invite code.
- **Regenerate** (with public IP/hostname and optional port): regenerates invite with a public endpoint prepended.

Why this makes sense:

- Invite codes need both identity and reachability hints.
- Including multiple endpoint candidates increases the chance another peer can connect.

---

### 2) Import Friend's Invite

Action:

- Paste a `canopy:...` invite and click **Connect**.

Behavior:

- Registers the remote peer identity locally.
- Attempts to connect to each endpoint in the invite.
- Returns either connected status or imported-but-not-connected state.

Why this makes sense:

- Import succeeds independently of immediate network success.
- This allows later reconnect when endpoint/network conditions improve.

---

### 3) Connected Peers

Shows peers with active mesh links.

Common actions:

- **Refresh**: reload current state.
- Row menu:
  - **Copy ID**
  - **Disconnect**
  - **Forget** (removes stored endpoint knowledge)

Why this makes sense:

- `Disconnect` is temporary.
- `Forget` is persistent cleanup and should be a deliberate choice.

---

### 4) Discovered Peers (LAN)

Shows mDNS-discovered peers on local network.

Why this makes sense:

- It provides local visibility even before manual invite exchange.
- Discovery status helps diagnose LAN setup issues.

---

### 5) Peers via Your Contacts (Introduced peers)

Shows peers introduced through already-connected peers.

Action:

- **Connect** attempts direct connection to introduced peer endpoints.

Why this makes sense:

- Enables mesh expansion and broker-assisted topology growth.

---

### 6) Known Peers

Shows previously seen/imported peers, including offline ones.

Actions:

- **Reconnect** per peer.
- **Reconnect All** for bulk retries.
- **Disconnect** when active.
- **Forget** to drop remembered endpoints.

Why this makes sense:

- Persisted peer memory enables recovery after restarts/network interruptions.

---

### 7) Connection History

Records connection attempts/outcomes for operator visibility.

Action:

- **Clear** removes displayed history entries.

Why this makes sense:

- Helpful for debugging intermittent routes and first-run setup problems.

---

### 8) Mesh Diagnostics

Displays runtime metrics such as connected/known counts, pending queues, reconnect tasks, and recent failures.

Actions:

- **Refresh** diagnostics.
- **Resync Mesh** (admin-only) to force synchronization routines.

Why this makes sense:

- Offers immediate operational visibility during onboarding and incident response.

---

## FAQ

### Why do I see 2+ `ws://` addresses?

Because your machine likely has multiple local interfaces/IPs (for example: WiFi NIC + VM bridge + host adapter). Canopy adds multiple endpoint candidates to improve connectivity.

These are usually local/private addresses, not automatically your public internet address.

### How do these relate to public IP?

- `Reachable at` addresses shown by default are local candidates.
- If peers are outside your LAN, use **Regenerate** with your public IP/hostname (and forwarded port).
- Regenerated invite includes the public endpoint plus local fallbacks.

### Why do I get an "API key required" error in web UI?

On Connect page actions, this almost always indicates session/auth expiry, not a normal requirement to paste an API key in the UI.

Fix:

1. Reload page.
2. Sign in again.
3. Retry action.

For scripts/CLI/integrations, include `X-API-Key`.

### If I'm behind NAT/router, can I still connect?

Yes. Typical options:

- Port forward mesh port (`7771`) and regenerate invite with public endpoint.
- Use VPN overlay (Tailscale/WireGuard).
- Use a mutually connected peer that can relay/broker.

### Does "Forget peer" delete shared message history?

No. It removes remembered endpoint/peer connectivity info so auto-reconnect does not continue. Content retention is governed by message storage/TTL/deletion behavior separately.

---

## Related docs

- [PEER_CONNECT_GUIDE.md](PEER_CONNECT_GUIDE.md)
- [QUICKSTART.md](QUICKSTART.md)
- [API_REFERENCE.md](API_REFERENCE.md)
