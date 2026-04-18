# Connect Page FAQ and Feature Reference

This document explains exactly what each section/button on the **Connect** page does, and clarifies common confusion around endpoint addresses, preview-only peers, mesh review, public IP usage, and authentication errors.

---

## Page purpose

The Connect page is the operational control center for:

- generating shareable invite codes,
- reviewing, importing, and connecting to peer invites,
- monitoring peer connectivity state,
- reconnect/disconnect/forget workflows,
- sending first-contact peers into Trust for review,
- mesh diagnostics and connection history.

It is intentionally action-first. The important value is whether you can connect, reconnect, relay, and diagnose peers from one place, not how many decorative counters fit at the top of the page.

---

## Section-by-section reference

### 1) Your Invite Code

What it shows:

- **Peer ID**: your local cryptographic identity.
- **Reachable at**: one or more `ws://host:port` or `wss://host:port` endpoint candidates included in your invite.
- **Invite code**: compact `canopy:...` payload containing identity + endpoint list.
- **Peer-facing identity hints**: the remote side will see the peer name/avatar/node hint built from **Settings -> Device Profile** plus the active mesh hint.

Buttons/actions:

- **Copy**: copies the full invite code.
- **Regenerate** (with public IP/hostname and optional port): regenerates invite with a public endpoint prepended.
- If Admin -> Transport Security declares local TLS or an external TLS terminator, generated invites can advertise the matching `wss://` path instead of plain `ws://`.

Why this makes sense:

- Invite codes need both identity and reachability hints.
- Including multiple endpoint candidates increases the chance another peer can connect.
- Keeping the human-facing peer identity in **Device Profile** helps people recognize the machine or node they are being asked to trust.

---

### 2) Import Friend's Invite

Action:

- Paste a `canopy:...` invite, click **Review Invite**, then click **Connect to this peer** if the review looks right.

Behavior:

- Decodes the invite locally so you can review:
  - **Peer identity**
  - **Mesh hint**
  - **Meshspace ID**
  - **Node hint**
  - **Reachability**
- Can enrich the preview with a safe remote fetch of mesh/peer art when the remote node is reachable.
- Registers the remote peer identity locally when imported.
- Attempts to connect to each endpoint in the invite.
- Returns either connected status or imported-but-not-connected state.
- If transport succeeds on first contact, the peer may still remain **preview-only** until an admin approves sync.

Why this makes sense:

- Import and transport review are separated from trust/sync approval.
- This lets operators verify who they are connecting to before history and channels begin syncing.
- Import succeeds independently of immediate network success, so later reconnect is still possible when endpoint/network conditions improve.

### What "preview-only" means

`preview-only` means the peer connection is real enough to review, but Canopy is still pausing channel/history sync until an admin approves it in **Trust**.

While a peer is preview-only:

- transport can be connected
- human-readable peer labels and node hints can still appear
- mesh hints can still be compared
- channel sync, history catch-up, and wider mesh expansion stay paused

This is intentional. It prevents a first-contact mistake from immediately contaminating the workspace with the wrong peer or wrong mesh.

---

### What the Trust actions mean

When a peer shows up in **Trust**, the common actions are:

- **Allow peer sync**: trust this specific peer enough to exchange channels, history, and other workspace content with it.
- **Allow mesh sync**: trust that peer's mesh as part of the same shared workspace, so future peers from that mesh do not stay stuck in review-only state.
- **Treat as same mesh**: confirm that a mismatched mesh name/ID is really just another identifier for the same workspace.
- **Keep bridge**: keep the connection between two different meshes without treating them as one shared workspace.
- **Refresh profile**: clear stale preview identity hints and reconnect so Canopy can relearn the current name/avatar without automatically approving sync.

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
- A peer can appear here even while still preview-only. Connected does not automatically mean approved for sync.

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

### Cross-mesh review

If the invite preview or Trust page says the remote peer advertises a different mesh identity:

- **Treat as same mesh** when the remote mesh ID/name is just a compatibility mismatch or alias for the same real workspace.
- **Keep bridge** when the peer should stay connected as an intentional cross-mesh bridge, not as part of the same workspace.

This decision belongs on the **Trust** page because it affects sync scope, not just transport reachability.

---

### 7) Connection History

Records connection attempts/outcomes for operator visibility.

Action:

- **Clear** removes displayed history entries.

Why this makes sense:

- Helpful for debugging intermittent routes and first-run setup problems.

---

### 8) Mesh Diagnostics

Displays runtime diagnostics such as pending queues, reconnect tasks, route state, and recent failures.

Actions:

- **Refresh** diagnostics.
- **Resync Mesh** (admin-only) to force synchronization routines.

Why this makes sense:

- Offers immediate operational visibility during onboarding and incident response without making the page depend on vanity summary tiles.

---

## FAQ

### Why do I see 2+ `ws://` addresses?

Because your machine likely has multiple local interfaces/IPs (for example: WiFi NIC + VM bridge + host adapter). Canopy adds multiple endpoint candidates to improve connectivity.

These are usually local/private addresses, not automatically your public internet address.

### How do these relate to public IP?

- `Reachable at` addresses shown by default are local candidates.
- If peers are outside your LAN, use **Regenerate** with your public IP/hostname (and forwarded port).
- Regenerated invite includes the public endpoint plus local fallbacks.

### How do I know whether my invite is using `wss://`?

Open **Connect -> Your Invite Code -> Transport security**.

That panel shows:

- whether the local mesh listener is `Secure WebSocket (wss)` or `Plain WebSocket (ws)`,
- which listener endpoint is active,
- whether the TLS certificate is disabled, self-signed, or operator-provided,
- whether outbound `wss://` verification is permissive or verified,
- how many invite endpoints are secure vs plain.

The `Reachable at` list also labels each advertised endpoint. If the endpoint shown to your peer starts with `wss://`, the invite is telling their node to connect with TLS-wrapped WebSocket transport. The panel now also shows the recommended endpoint and invite mode: secure public, mixed secure/plain, plain public, or LAN-only.

### Why do I still see both `wss://` and `ws://` endpoints?

For an explicitly generated public `wss://...` invite, Canopy suppresses same-host public `ws://...` fallback by default. LAN/private `ws://...` fallbacks may still appear for same-network convenience, and a plain public fallback can be intentionally enabled with the compatibility option. If a mixed invite is shown, check the connected peer card or diagnostics for **Active transport**; the live session is only WSS if the active endpoint is `wss://...`.

### Does `wss://` replace Canopy's peer verification?

No. `wss://` protects the WebSocket transport with TLS. Canopy still verifies the remote peer identity using the public keys in the invite and its application-layer handshake. Treat `wss://` as the transport-security layer, not the peer-trust decision.

### Can an explicit `wss://` invite fall back to `ws://`?

No. When an invite or direct action explicitly chooses `wss://`, Canopy does not silently retry the same endpoint as plain `ws://` if TLS fails. This is intentional so operators can rely on the meaning of a secure endpoint. Fix the VPS, reverse proxy, tunnel, certificate, or endpoint instead.

### Why does the UI say certificate verification is permissive?

Many current Canopy meshes use self-signed transport certificates, so the default outbound `wss://` client mode accepts those certificates while Canopy verifies peer identity separately at the application layer. For stricter public-internet deployments, set `CANOPY_REQUIRE_VERIFIED_WSS=true` and use an externally trusted certificate or TLS-terminating proxy. In verified mode, failed `wss://` certificate/hostname checks are not downgraded to plain `ws://`.

### Why do I get an "API key required" error in web UI?

On Connect page actions, this almost always indicates session/auth expiry, not a normal requirement to paste an API key in the UI.

Fix:

1. Reload page.
2. Sign in again.
3. Retry action.

For scripts/CLI/integrations, include `X-API-Key`.

### Why does the invite review show a peer name/avatar that is different from the local user profile?

That is expected. Canopy now separates:

- **Device Profile**: what remote peers see during connection review
- **Profile**: your in-app user/account identity
- **Mesh**: the active workspace identity

If you want other operators to recognize this machine during invite review, update **Settings -> Device Profile**.

### What should I do if the peer label or avatar looks stale?

Open **Trust** and use **Refresh profile** on that peer.

For preview-only peers, this clears cached preview identity and safely reconnects so Canopy can relearn fresh label and avatar hints without approving sync. For trusted peers, it can also recover richer profile state.

### When should I leave a peer preview-only?

Leave a peer preview-only when:

- the peer name or avatar is missing or suspicious
- the mesh hint is unexpected
- the peer was introduced through another contact and you want to confirm it first
- the operator has not yet decided whether this is the same mesh or a bridge

Approve sync only when the human-facing identity and mesh context are good enough for that workspace.

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
