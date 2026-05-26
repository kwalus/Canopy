# Agent Note: Capsule Human Briefs

Date: 2026-05-25
Branch: `codex/capsule-summary-human-briefs-0.6.235`
Version: `0.6.235`

## Purpose

Konrad reported that the new channel Capsule filter is valuable but that the collapsed Capsule summary is not sufficiently human-friendly. The previous summary was mechanically accurate but cognitively poor: it spliced the first compressed post to the last compressed post, which often produced noisy agent text rather than a useful catch-up surface.

## Implemented change

Updated the channel Capsule renderer in `canopy/ui/templates/channels.html` so each collapsed agent-run Capsule now presents a small operator brief:

- `What changed`: a high-signal extracted sentence, preferring completion/work-product language such as uploaded, added, indexed, built, packaged, verified, or resolved.
- `Needs attention`: shown only when the compressed run contains blocker/access/error/review language such as failed, blocked, unable, 403/404, permission, grant access, owner action, or review needed.
- `Source trail`: compact artifact context, including file sources, Digestion references, work cards, and tagged teammates.
- `Jump back in`: a re-entry prompt explaining how the human can expand the exact trace, reply in context, or resolve a blocker.

The exact source posts remain preserved behind `Show trace`; this patch does not remove source visibility and does not introduce any LLM-generated or network-generated summaries.

## Why this is safe

- Client-side only; no schema migration and no server behavior change.
- Uses the same message payloads already rendered by the channel page.
- Heuristic extraction only; it avoids inventing content and leaves exact source posts expandable.
- Existing Capsule level preferences, compression thresholds, status chips, copy button, and trace expansion behavior are preserved.
- Mobile layout stacks the new brief rows into one column.
- Light-theme styling was added for the new brief rows.

## Files changed

- `canopy/ui/templates/channels.html`
- `tests/test_frontend_regressions.py`
- `CHANGELOG.md`
- Version scope files: `pyproject.toml`, `canopy/__init__.py`, `README.md`, `docs/API_REFERENCE.md`, `docs/AGENT_ONBOARDING.md`, `docs/MCP_QUICKSTART.md`, `uv.lock`

## Test coverage

Regression assertions now confirm the Capsule brief helpers and UI labels exist:

- `getAgentRunHumanBrief(...)`
- `renderAgentRunBriefRows(...)`
- `What changed`
- `Needs attention`
- `Jump back in`
- `agent-run-capsule-brief-row`

## Follow-up considerations

This is intentionally a safe first pass. If users want deeper natural-language summaries later, the same UI surface could optionally accept a server-side or user-key-backed generated Capsule digest. I would keep this heuristic version as the default fallback because it is fast, private, deterministic, and keeps the channel readable even without API keys.
