# AGENT NOTE - Wolkow VPS performance diagnostics pass

Date: 2026-05-29
Branch: `codex/performance-wolkow-diagnostics-0.6.286`
Target version: `0.6.286`

## Source diagnostic reviewed

Reviewed `/Users/konradwalus/Downloads/canopy-diagnostics-20260529T190612Z.txt` from the Wolkow VPS.

The bundle was from `canopy_version: 0.6.283`, so it did not yet include the later `0.6.284` long-DM attachment conversion or `0.6.285` Digestion query-readiness patches.

## Main signals found

- `workspace_events` is large for this instance: 47,636 rows, mostly `channel.message.created` events.
- `file_access_log` is also large: 43,260 rows.
- Digestion tables are now meaningful contributors to DB size: 9,004 chunks and 8,870 embeddings.
- Recent slow/error requests were dominated by optional capsule LLM enrichment: `POST /ajax/canopy_llm/capsule_summary` around 4.5 seconds each during OpenAI timeout periods.
- The request summary showed `GET /ajax/sidebar_attention_snapshot` as a high aggregate-cost endpoint: 1,426 calls, average about 209 ms, recent p95 about 240 ms.
- One datapoint extraction request ran for about 14.4 minutes. This is expected for large extraction work and was not changed in this patch.
- The diagnostic included `Message content too long: 20050 > 4096`, which is addressed by the later `0.6.284` DM long-draft conversion patch rather than this performance patch.

## Patch implemented

### 1. Capsule LLM fast fallback after provider trouble

Files:
- `canopy/ui/routes.py`
- `canopy/ui/templates/channels.html`
- `tests/test_canopy_llm_compose.py`

Change:
- Added a user-scoped server-side cooldown for optional capsule LLM enrichment after provider failures such as OpenAI/Bedrock timeout, provider cooldown, bad response, or provider response errors.
- The route still validates channel read access before returning fast fallback.
- During cooldown, the endpoint returns deterministic fallback immediately with `reason: provider_cooldown` and `cooldown_remaining_seconds`.
- The browser now respects `cooldown_remaining_seconds` when provided, so it does not keep asking the server for optional summaries during provider trouble.

Reasoning:
- Capsules already have deterministic summaries. LLM polish is optional.
- It is better to preserve responsive channel rendering than spend request workers waiting on repeated low-priority LLM timeouts.
- Scope is per viewer, not global, so one user's provider/key trouble does not suppress another user's capsule enrichment.

Tunable:
- `CANOPY_CAPSULE_LLM_FAILURE_COOLDOWN_SECONDS`, default `120`, bounded `15..900`.

### 2. Sidebar attention snapshot micro-cache

Files:
- `canopy/ui/routes.py`
- `tests/test_sidebar_attention_summary.py`

Change:
- Added a short cursor-aware process cache for `/ajax/sidebar_attention_snapshot`.
- Cache key includes meshspace, user, and latest workspace-event cursor.
- `force=1` bypasses the cache.
- Existing attention-clear invalidation now clears this snapshot cache too.

Reasoning:
- The endpoint is polled frequently and does expensive summary/activity aggregation.
- A 1.25-second cache is enough to collapse duplicate browser/tab bursts without materially delaying notification state.
- Cursor-keying means new workspace events naturally produce a fresh snapshot.

Tunable:
- `CANOPY_SIDEBAR_ATTENTION_SNAPSHOT_TTL_SECONDS`, default `1.25`, bounded `0..10`.

## Verification

Passed:

```bash
python -m py_compile canopy/ui/routes.py canopy/core/canopy_ai.py canopy/__init__.py
pytest tests/test_canopy_llm_compose.py tests/test_sidebar_attention_summary.py tests/test_frontend_regressions.py -q
pytest tests/test_ui_polish_regressions.py -q
git diff --check
```

Results:
- `237 passed` for compose/sidebar/frontend regression suite.
- `33 passed` for UI polish regression suite.

## Safety notes

- No channel ordering, message payload, DM, Digestion, or notification-count semantics were changed.
- The sidebar cache is intentionally tiny and force-bypassable.
- Capsule LLM cooldown only affects optional enrichment; deterministic capsules remain visible.
- The long-running datapoint extraction request was observed but not converted to async in this patch because that would be a larger behavioral change.

## Follow-up candidates

- Consider a future retention or compaction strategy for `file_access_log` and older `workspace_events` if large VPS instances continue to grow.
- Consider moving large datapoint extraction fully into a job/progress model if operators report request worker starvation during extraction.
- After Wolkow updates past `0.6.284`, confirm long DM failures now offer attachment conversion instead of generic failure.
