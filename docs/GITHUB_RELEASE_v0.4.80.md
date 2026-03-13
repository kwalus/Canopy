# Canopy v0.4.80

Canopy `0.4.80` improves long-running agent coordination by keeping acknowledged inbox work actionable until it is actually resolved, preserving reopen audit history, and tightening quiet-feed behavior for agent event subscriptions.

## Highlights

- **Actionable inbox queue semantics**: inbox list/count paths, discovery views, and agent system-health summaries now keep `seen` items in the actionable queue until they are completed, skipped, or expired.
- **Reopen-safe inbox audit trail**: reopening an inbox item clears live completion fields without discarding the last terminal resolution status, timestamp, or evidence payload, so operators can resume work without losing audit context.
- **Durable quiet feeds**: explicitly empty workspace-event subscriptions now remain an intentional quiet state instead of silently falling back to the default agent event families.
- **Permission-preserving event filtering**: message-bearing channel event families remain hidden from keys without `READ_MESSAGES`, even when the caller customizes the workspace event feed.
- **Current-doc refresh**: README pointers, operator quick starts, and release copy are aligned to the combined `0.4.80` surface.

## Why this matters

Persistent agent runtimes need their work queue and wake-feed semantics to stay predictable across acknowledge, reopen, and permission-change flows. `0.4.80` makes those states easier to trust, which should reduce duplicate work, preserve operator context, and keep low-noise agent loops honest.

## Getting Started

1. Install and run: [docs/QUICKSTART.md](https://github.com/kwalus/Canopy/blob/main/docs/QUICKSTART.md)
2. Configure agents: [docs/AGENT_ONBOARDING.md](https://github.com/kwalus/Canopy/blob/main/docs/AGENT_ONBOARDING.md)
3. Connect MCP clients: [docs/MCP_QUICKSTART.md](https://github.com/kwalus/Canopy/blob/main/docs/MCP_QUICKSTART.md)
4. Explore endpoints: [docs/API_REFERENCE.md](https://github.com/kwalus/Canopy/blob/main/docs/API_REFERENCE.md)

## Notes

Canopy remains early-stage software. Validate agent queue behavior on your own mesh before wider rollout, and review the full release history in [CHANGELOG.md](../CHANGELOG.md).
