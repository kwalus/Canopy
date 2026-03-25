# Canopy Source Layout v1

`source_layout` is an **optional, additive composition manifest** for channel messages, feed posts, and direct messages.

It exists to solve one specific product problem:

- Canopy Modules, embeds, cards, and attachments can already be rich
- the source post itself is still mostly a flat body + attachment stack
- without composition semantics, strong deck items still render like a dump

`source_layout` lets a source item declare:

- which item is the **hero**
- where the **lede** text belongs
- which supporting items belong in a **side rail**, **strip**, or **below** region
- which **CTA links** should render as an action row
- which item should be the **default deck entry**

It is intentionally constrained. It is not arbitrary HTML layout and it is not executable code.

---

## Compatibility

`source_layout` is:

- optional
- backward compatible
- ignored when absent or invalid

Old messages, posts, and DMs continue to render with existing Canopy behavior.

New sources can progressively opt in.

---

## Supported fields

```json
{
  "version": 1,
  "hero": {
    "ref": "attachment:F123",
    "label": "Main module"
  },
  "lede": {
    "kind": "rich_text",
    "ref": "content:lede"
  },
  "supporting": [
    {
      "ref": "attachment:F456",
      "placement": "right",
      "label": "Side video"
    },
    {
      "ref": "attachment:F789",
      "placement": "strip",
      "label": "Reference card"
    }
  ],
  "actions": [
    {
      "kind": "link",
      "label": "Open full brief",
      "url": "https://example.com/brief"
    }
  ],
  "deck": {
    "default_ref": "attachment:F123"
  }
}
```

### Allowed reference prefixes

- `attachment:`
- `widget:`
- `content:`

### Supported placements

- `right`
- `strip`
- `below`

### Supported lede kinds

- `rich_text`

### Supported action kinds

- `link`

---

## Reference model

### `attachment:<file_id>`

Targets a real attachment card or image cell.

Examples:

- module attachment
- SVG score card
- image panel
- file-backed support card

### `widget:<manifest_key>`

Targets a deck/widget surface that exposes a sanitized manifest key.

This is primarily useful when a source includes rich embed widgets and you want a specific widget to be the hero or default deck entry.

### `content:lede`

Targets the source body text.

In v1, the only supported content ref is:

- `content:lede`

---

## Rendering model

When `source_layout` is present, Canopy builds these zones inside the source:

- `hero`
- `lede`
- `actions`
- `side`
- `strip`
- `below`

The renderer then moves referenced nodes into those zones.

Any remaining unclaimed source nodes fall through into `below`.

This keeps the model safe:

- no arbitrary positioning
- no arbitrary CSS
- no arbitrary DOM injection

---

## Deck integration

`deck.default_ref` gives the source a preferred default deck target.

That means:

- a module can open as the default deck item instead of a random first media item
- a map or chart can become the default deck hero when that is the intended focus

If the referenced target does not exist, Canopy falls back to the normal deck selection logic.

---

## Authoring guidance

Use `source_layout` when you want the source item itself to feel designed, not dumped.

Good use cases:

- lesson post with a hero module, right-side video, and score-card strip
- shopping post with a hero buyer module, support cards, and CTA row
- security/news/event source with a module hero and compact supporting panels

Avoid using it when the source only has:

- plain text
- a single attachment
- no real need for composition

---

## Current implementation boundaries

v1 is intentionally small:

- no nested layout trees
- no arbitrary block types
- no server-side authoring DSL
- no executable source-level code

If you need executable behavior, use a `Canopy Module` for the hero or supporting surface and use `source_layout` only to compose it into the source cleanly.

That separation is deliberate.

---

## Relationship to Canopy Module

`Canopy Module` solves:

- safe executable experience in the deck
- bounded, capability-scoped interactivity

`source_layout` solves:

- how the post, feed item, or DM itself presents that module and its supporting material

The two primitives are meant to work together.

---

## Suggested patterns

### Lesson source

- `hero`: module
- `lede`: narrative brief
- `supporting.right`: video
- `supporting.strip`: reference cards
- `deck.default_ref`: module

### Commerce source

- `hero`: buyer module
- `lede`: short decision brief
- `supporting.strip`: shortlist cards
- `actions`: store links
- `deck.default_ref`: module

### Operations source

- `hero`: station module
- `lede`: operator brief
- `supporting.right`: stream or map
- `supporting.strip`: compact status cards

---

## API usage

`source_layout` is accepted as an optional top-level field on:

- channel message create/update
- DM create/update
- feed create/update

For feed posts, the API accepts a top-level `source_layout` field and persists the normalized result under `metadata.source_layout`.

Canopy normalizes invalid shapes away instead of hard-failing the source.

That keeps authoring resilient while preserving mesh stability.

> **Lenient parsing:** unknown keys and missing optional fields inside an otherwise valid `source_layout` object are ignored. Only structurally invalid manifests, such as a non-object value or a missing/invalid `version`, are discarded wholesale.
