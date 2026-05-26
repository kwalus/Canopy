# Agent Note: Expanded Composer Emoji Picker

Date: 2026-05-26
Branch: `codex/expanded-composer-emojis-0.6.244`
Version bump: `0.6.243` -> `0.6.244`

## Request
The composer text input bar had an emoji button, but the available standard emoji set felt too limited. The requested change was to add several commonly used emoji without disrupting the existing custom emoji flow.

## Implementation
- Expanded the shared `CanopyEmojiPicker` standard emoji list in `canopy/ui/templates/base.html`.
- The shared picker is used by:
  - Channel composer
  - Feed composer
  - Direct-message composer
  - Deck Inbox composer
- Added common everyday symbols across the main user intents:
  - quick acknowledgement: thumbs down, clap, raised hands, strong, wave
  - emotion/reaction: grinning, smile, sweat smile, wink, heart eyes, sob, mind blown, thinking
  - work/status: warning, lock, tools, paperclip, speech, star
  - existing Canopy-specific/common set preserved: 100%, IDK, beer, Canopy logo, rocket, fire, idea, pin, etc.
- Added lightweight keyword matching so search can find an emoji by human intent as well as internal name, e.g. `smile`, `approve`, `ship`, `warning`, `private`, `fix`, `file`.
- Kept the change centralized rather than touching each composer separately, reducing drift between Channels, DMs, Feed, and Deck Inbox.

## Notes / Boundaries
- This patch intentionally targets composer insertion, not the reaction-count palette semantics.
- Custom emoji upload behavior is unchanged.
- The older channel-local emoji picker code remains present but the visible channel composer button uses the shared global picker. I left the older code untouched to avoid a larger cleanup risk in this small patch.

## Verification
- Added regression coverage in `tests/test_frontend_regressions.py` to assert the expanded common emoji entries and keyword search behavior are present in the shared picker.
- Recommended validation:
  - Open a channel, DM, Feed composer, and Deck Inbox composer.
  - Click the emoji button.
  - Confirm new symbols appear.
  - Search terms such as `smile`, `hello`, `warning`, `fix`, and `ship` return expected entries.
