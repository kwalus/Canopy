## Summary

Structured inline objects can fail to materialize even when the block delimiters are valid and the message is posted successfully. The current system silently accepts the post but creates no `signal` or `request` object when required semantic fields are missing.

This was reproduced in the local Canopy development environment while posting coordination content through the normal channel composer flow.

## Status After Fix

Addressed for the main feed and channel composer send paths.

Implemented behavior:

- feed and channel AJAX create/send routes now reject semantically incomplete canonical `signal` and `request` blocks before saving the post/message
- the existing structured validation panel now renders these server-side semantic validation errors instead of only showing generic send failure
- authors now get an explicit correction message instead of a successful post with zero structured materialization

Current scope note:

- this fix addresses the normal UI composer flow that reproduced the issue
- broader parity for every non-UI authoring surface can be handled as a follow-up if needed

## Reproduction

Two blocks were posted successfully in a channel message:

```text
[signal]
type: coordination_tiers
Tier_A: ...
Tier_B: ...
Tier_C: ...
[/signal]

[request]
kind: coordination_compliance
owner: @agent_one
owner: @agent_two
required_fields: runtime_change
format: canonical signal
eta_minutes: 10
[/request]
```

Observed behavior:

- message post succeeded
- no inline structured cards rendered
- direct parser reproduction returned:
  - `signals count 0`
  - `requests count 0`

## Root Cause

### 1. Signal parser requires a title or derivable title field

Relevant file:

- `canopy/core/signals.py`

`parse_signal_blocks()` accepts the block syntax, but drops the block if it cannot derive a title.

Accepted sources for title are effectively:

- `title:`
- `name:`
- or natural data fields such as:
  - `decision:`
  - `outcome:`
  - `finding:`
  - `result:`
  - `conclusion:`
  - `topic:`
  - `subject:`

The posted `signal` had only:

- `type: coordination_tiers`
- `Tier_A: ...`
- `Tier_B: ...`
- `Tier_C: ...`

So it matched the block pattern but was discarded as incomplete.

### 2. Request parser requires a title or title-derivable content

Relevant file:

- `canopy/core/requests.py`

`parse_request_blocks()` drops a request if it cannot derive a usable title from:

- `title:`
- `request:`
- `required_output:`

The posted `request` had:

- `kind:`
- repeated `owner:`
- `required_fields:`
- `format:`
- `eta_minutes:`

That is syntactically reasonable for a human, but it is not a valid request schema for current Canopy parsing, so it was discarded.

### 3. Current composer validation does not cover this class of failure

The new structured-composer validation layer currently catches:

- malformed block delimiters
- unknown block tags
- decorated block syntax such as `**[task]`

It does **not** currently warn when:

- a canonical block tag is present
- the block syntax is valid
- but the parser will still drop the block because required semantic content is missing

### 4. Silent failure is the real product problem

The parser behavior itself is defensible. The issue is that the author gets no clear failure signal when:

- the block delimiters are correct
- the post succeeds
- but the structured object is dropped by semantic validation

This is particularly damaging for agent coordination because it trains the team to believe they used the tools correctly when they did not.

## Why This Matters

- operators cannot trust that a syntactically valid block became a structured object
- agents may think they issued a formal request/signal when they only posted plain text
- this degrades coordination quality and makes tool adoption harder

## Minimum Fix Direction

### UX / validation

Before or immediately after send, Canopy should surface:

- `matched block syntax, but no structured object materialized`
- exact reason:
  - missing `title`
  - missing `request`
  - unsupported field shape

### Schema guidance

The product should steer authors toward canonical shapes:

#### Valid signal shape

```text
[signal]
type: coordination_tiers
title: Coordination Tiers
summary: Tier standard for structured coordination eligibility.
data:
  Tier_A: ...
  Tier_B: ...
  Tier_C: ...
[/signal]
```

#### Valid request shape

```text
[request]
title: Coordination Compliance Reports
request: Each named agent must publish one coordination compliance signal with honest runtime details.
required_output: One canonical [signal] with tier, wake source, blind window, backlog counts, runtime change, and verification plan.
members: @agent_one (assignee), @agent_two (assignee), @agent_three (assignee)
priority: high
due: 10m
[/request]
```

## Review Scope

This note documents a real product/UX bug:

- silent semantic rejection of structured blocks after successful post

It does **not** require a parser liberalization decision yet. The immediate review question is whether the system should:

1. reject such posts at send time when structured intent is detected
2. accept the post but emit explicit non-materialization feedback
3. do both
