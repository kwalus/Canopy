# Agent Note: Capsule Signal Density Polish

Date: 2026-05-29
Branch: `codex/capsule-signal-polish-0.6.273`
Version: `0.6.273`

## Context
Konrad reviewed screenshots of agent-run Capsules and flagged three UX problems:

- The direct file/workproduct name element looked overly pill-shaped and cramped long text.
- The bottom metadata chip strip (`Needs human`, post count, compression level, agent count, file refs, mentions, etc.) was adding static fill and duplicating facts already represented elsewhere in the Capsule.
- Run Map node summaries were being squeezed/clamped too aggressively, especially at intermediate and narrow widths.

The goal of this pass was not to change Capsule behavior or trace semantics, but to improve signal-to-noise and readability in the collapsed Capsule.

## Implemented Changes

### 1. Removed redundant collapsed metadata chip strip
- `renderAgentRunCapsule(...)` no longer emits the bottom `agent-run-capsule-meta` status/count chip rail.
- The underlying information remains available through:
  - Work Effort tag and lede.
  - Run Map stop count and per-node signal counts.
  - Files & workproducts rail.
  - Avatar stack.
  - Trace button.
- The CSS class remains defensively hidden so older cached or enriched markup does not reintroduce the visual clutter.

### 2. File/workproduct name plate refinement
- Capsule artifact links/buttons now render as compact rectangular rounded-corner name plates instead of inheriting a pill-like entity-link treatment.
- Explicitly overrides `canopy-entity-link` / `canopy-file-ref` styling inside Capsule artifacts.
- Preserves:
  - direct file opening behavior,
  - save-to-vault/source buttons,
  - hover marquee for long filenames,
  - accessible titles and labels.

### 3. Run Map readability pass
- Increased Run Map node height and switched alignment to the top so avatar, signal, author, and summary text line up more predictably.
- Map excerpts now allow up to 3 lines in normal Capsule layout and 2 lines at narrow container widths instead of collapsing to one line.
- Node headings can wrap safely so signal labels and time/count markers do not crush the summary body.

## Files Changed
- `canopy/ui/templates/channels.html`
- `tests/test_frontend_regressions.py`
- `CHANGELOG.md`
- `pyproject.toml`
- `canopy/__init__.py`
- `README.md`

## Validation
- `venv/bin/python -m pytest tests/test_frontend_regressions.py -q`
- Result: `178 passed in 1.50s`

## Review Notes
This is intentionally a conservative UI refinement:

- No backend/API changes.
- No changes to Capsule grouping logic.
- No changes to LLM enrichment payloads.
- No changes to trace preservation or source jumping.

The main review item is visual: verify Capsules in dense channels at desktop, split-pane, Deck-constrained, and mobile widths. The design intent is that collapsed Capsules should now feel less like a static tag collection and more like a compact dashboard of dynamic work effort, source map, and immediately actionable outputs.
