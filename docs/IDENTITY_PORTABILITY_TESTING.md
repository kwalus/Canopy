# Identity Portability Phase 1 Testing (Admin QR + Direct Send)

This guide covers the current Phase 1 admin workflow for moving user identity grants between peers.

Scope is intentionally narrow:
- Principal metadata sync
- Signed bootstrap grants
- Admin-only import/apply/revoke

Not included in Phase 1:
- Cross-peer login/session transfer
- Automatic admin transfer
- Password/API-key migration

## Prerequisites

1. Enable the feature flag on test peers:

```bash
export CANOPY_IDENTITY_PORTABILITY_ENABLED=1
```

2. Restart Canopy after setting the variable.
3. Ensure both peers are connected and trusted in the mesh.
4. Sign in as an instance admin and open `Admin`.

## Where To Test In UI

Use the `Identity Portability (Phase 1)` panel in Admin.

The panel supports two transfer paths:
- Direct mesh delivery to a selected capable peer
- Portable token/QR transfer for mobile-assisted setup

## Path A: Direct Send (No Copy/Paste)

1. Choose `Local user`.
2. Set `Deliver to peer` to a connected capable peer.
3. Leave `Audience lock` blank unless you want to hard-lock redemption.
   - If blank and a target peer is selected, UI auto-fills lock with the selected peer.
4. Set expiry and max uses.
5. Click create (`+` button).

Expected result:
- Grant is created
- Grant is synced directly to selected peer
- Grant appears in `Latest grants snapshot`

## Path B: QR / Token Transfer (Mobile-Friendly)

1. Create a grant (any delivery mode).
2. In the same panel, copy `Grant transfer token` or toggle `QR`.
3. On destination admin page:
   - Paste token into `Import grant artifact (JSON or token)`, or
   - Use `Scan QR` when browser supports camera + BarcodeDetector.
4. Optional: choose `Apply to local user`.
5. Click `Import Grant`.

Accepted import formats:
- Canonical JSON artifact payload
- `canopy-grant://v1/<base64url-json-token>`

## Revoke Flow

1. Enter grant id in `Revoke grant ID`.
2. Set reason.
3. Click `Revoke Grant`.

Expected result:
- Grant status becomes revoked
- Revocation marker propagates to capable peers

## Security Checks

Verify these invariants during testing:
- Non-admin users cannot access admin identity routes.
- Invalid signatures are rejected on import/apply.
- Issuer/source mismatch is rejected.
- Audience lock prevents wrong-peer application.
- Grant replay is idempotent for same grant/user pair.
- Revoked/expired/consumed grants cannot be newly applied.

## Troubleshooting

- `No connected peers with identity_portability_v1 capability`
  - Ensure remote peer also has `CANOPY_IDENTITY_PORTABILITY_ENABLED=1`.
  - Confirm peers are currently connected.

- `QR camera scan unavailable`
  - Browser lacks BarcodeDetector support; use token paste/import.

- `Clipboard write failed`
  - Browser denied clipboard permission; copy manually from token field.
