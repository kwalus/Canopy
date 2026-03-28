# Governance Trust Boundary Review — March 2026

**Scope:** Peer-driven control-plane paths touching governance state; trusted-peer assumptions, origin checks, result application, and recovery after reconnect.

---

## Fixed in This PR

### 1. `apply_channel_removal_result` result falsification with matching electorate

**Location:** `canopy/core/channels.py`

When a local proposal record already existed, the code verified that the remote electorate and threshold matched the frozen local values, but it did not cross-check the claimed terminal result against votes already stored locally.

That meant a trusted peer in the electorate could try to claim:

- `result=retired` even though a locally stored `keep` vote made retirement impossible
- `result=rejected` even though locally stored `remove` votes had already met the threshold

The integrated fix now rejects a remote result when locally stored ballots directly contradict it.

### 2. Empty finalizer peer accepted on result application

**Location:** `canopy/core/channels.py`

`apply_channel_removal_result` previously allowed an empty `finalizing_peer_id` to continue into result application. The integrated fix now fails closed when the finalizing peer is missing.

---

## Findings Not Fixed Here

### A. Bootstrap trust path remains strong but trust-list dependent

When a node has no local proposal record, a trusted peer in the electorate can still bootstrap-apply a terminal result during reconnect catch-up. That is necessary for recovery, but it means the safety of that path still depends heavily on the correctness of `trusted_peers`.

**Follow-on idea:** Consider a stronger proof for bootstrap retirement, such as a signed tally or another protocol-level verification step.

### B. Remote proposal electorate is still sender-controlled

`receive_channel_removal_proposal` currently accepts the sender-provided electorate and threshold after only limited checks. A trusted peer could still propose a smaller-than-expected electorate and an easier threshold than the local node would derive.

**Follow-on idea:** Recompute the expected electorate locally and reject materially divergent remote proposals.

### C. Claimed-peer equals sender check is enforced in app handlers, not in `channels.py`

The `from_peer` to claimed-peer consistency check currently lives in `canopy/core/app.py` before `channels.py` methods are called. That is fine for the current path, but any future direct caller could bypass that protection.

**Follow-on idea:** Move that invariant into the lower-level governance methods as an optional `from_peer` assertion.

### D. Governance messages still lack a stronger replay guard

The proposal, vote, and result messages still do not have a dedicated monotonic sequence, nonce, or replay-specific guard beyond the current proposal/vote/result state checks.

**Follow-on idea:** Add an explicit replay strategy if governance traffic becomes more central or more adversarial.
