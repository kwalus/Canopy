# Public Release Audit - Canopy 0.6.114

> **Canopy-Dev internal - do not include in the public `kwalus/Canopy` promotion PR.**
> This document is a working audit log for the private dev mirror only.

**Date:** 2026-05-14
**Scope:** Public-release readiness for the 0.6.x line through `0.6.114` - repo hygiene, docs clarity, public-safe wording, accidental internal-only leakage, first-run/setup friction, and obvious release blockers.

---

## Files to Exclude from Public Canopy Promotion

The following files live in `kwalus/Canopy-Dev` for good reasons but must **not** be copied or promoted into the public `kwalus/Canopy` repo:

| File or pattern | Reason |
|------|--------|
| `docs/PUBLIC_RELEASE_AUDIT.md` | Canopy-Dev-internal audit log; references private dev-mirror workflow details |
| `scripts/push_to_github.py`, `scripts/push_one_file_mcp.py`, `scripts/deploy_to_github_mcp.py`, `scripts/push_docs_to_github.py`, `scripts/validate_github_sync.py` | Internal GitHub/MCP manager helpers for the private dev-mirror workflow |
| `scripts/sync_to_github_folder.py`, `scripts/list_pushable_files.py` | Internal promotion/mirror helpers used to curate files into the separate push-folder workflow |
| `scripts/assign_copilot_branch_safe.py`, `scripts/create_*_mcp.py`, `scripts/merge_copilot_prs.py`, `scripts/cleanup_github_repo.py`, `scripts/meshspace*.json`, `scripts/meshspaces*.json` | Internal Copilot, issue, PR, and MCP orchestration tooling/specs |
| `scripts/post_*.py`, `scripts/fetch_recent_*.py`, `scripts/windy_*.py`, `scripts/approve_windy_and_post.py`, `scripts/setup_windy_and_post_general.py`, `scripts/set_windy_avatar.py` | Internal bot/persona/channel automation scripts and local messaging helpers |
| `scripts/recover_db_lock.py`, `scripts/test_channel_image_and_delete.py`, `scripts/perplex_posts/*.json` | Local recovery/test helpers and cached internal exports, not public release assets |
| `scripts/start_canopy_dev.ps1`, `scripts/bump_version.py` | Local development and release-maintenance helpers, not part of the first public product-facing repo payload |
| `docs/GITHUB_RELEASE*`, `docs/RELEASE_*`, `docs/TEAM_ANNOUNCEMENT_*`, `docs/*_PLAN.md`, `docs/*DESIGN_REVIEW.md`, `docs/*TESTING.md`, `docs/hardening-review/*.md` | Release-process templates, implementation plans, test/review notes, and internal announcement material rather than core public product docs |
| `publications/`, `traffic_tool.zip` | Internal publication artifacts and packaged local tools that are outside the public product/repo scope |
| `screenshots/`, `media/`, `breakout-hero.png`, `life-lab-hero.png`, `windy-*.canopy-module.html`, `canopy/ui/static/modules/` | Demo/showcase and promotional assets that made the public PR unnecessarily noisy; exclude them from the first public release payload unless intentionally curated later |

When assembling the public promotion PR, omit these files entirely. Public-safe scripts should be limited to generic contributor and packaging utilities such as `scripts/bump_version.py`, `scripts/build_tray_windows.ps1`, `scripts/canopy_tray_installer.iss`, `scripts/check_jinja_templates.py`, `scripts/filter_python_diagnostics_to_diff.py`, and the migration helpers.

---

## What Was Audited

- All committed docs, scripts, config files, and repo metadata
- `.gitignore` rules and any gaps in coverage
- Secrets, tokens, credentials, and personal/private path references
- Agent scratch artifacts, internal-only setup guides, and local-machine details

---

## Current 0.6.114 Pass

### 1. Documentation drift - Updated (2026-05-14)

**What was found:** Core public docs had fallen behind the current feature surface:
- `docs/API_REFERENCE.md` still declared the `0.6.79` release line.
- `docs/MCP_QUICKSTART.md` still declared the `0.6.0` release line.
- `docs/AGENT_ONBOARDING.md` still declared the `0.6.79` release line.
- `docs/MENTIONS.md` still declared the `0.6.0` release line.
- `docs/QUICKSTART.md` and `docs/PEER_CONNECT_GUIDE.md` still declared older 0.6.x release scopes.
- Recent File Vault, MCP Vault tooling, pasted Vault-link hydration, and Bedrock compose behavior needed clearer public-facing notes.

**Change made:** Updated version scopes and added concise coverage for:
- user-scoped File Vault APIs, folder operations, checksum-protected updates, attachment saves, delete reference guards, and pasted Vault-link hydration;
- agent onboarding guidance for File Vault scopes and MCP import boundaries;
- MCP `canopy_vault_*` tools, pending remote-large attachment saves, and `CANOPY_MCP_FILE_IMPORT_DIR` import boundaries;
- `@Canopy` compose provider behavior for OpenAI Responses versus AWS Bedrock.

### 2. Internal automation ignore coverage - Tightened (2026-05-14)

**What was found:** Several internal scripts are still tracked in the private development mirror for maintainer use. They must not be promoted to the public repo, and the ignore rules only covered a subset of their naming patterns.

**Change made:** Expanded `.gitignore` with broad local-only patterns for internal posting, Copilot/MCP orchestration, GitHub push, sync, validation, and local test helpers. Because some files are already tracked in the dev mirror, public promotion must still use an allowlist/curated copy rather than relying on `.gitignore` alone.

---

## Previous Findings and Changes

### 1. Personal machine paths in `docs/REPOST_V1_DESIGN_REVIEW.md` - Fixed (2026-03-24)

**What was found:** Eight absolute file-path references embedded as code-review citations contained the developer's macOS username and Dropbox folder structure.

**Change made:** Replaced every occurrence of the personal absolute prefix with the project-relative path (for example `canopy/core/feed.py:993`). No other content was modified.

---

### 2. GitHub push workflow posture - Reviewed (2026-03-24)

**Current workflow:** Routine pushes now target the private dev mirror (`kwalus/Canopy-Dev`) by default through `scripts/push_to_github.py` and the local MCP Manager on `localhost:8000`. Public `kwalus/Canopy` pushes are explicit opt-in actions used only for release-reviewed promotions.

**What this means for public review:**

- The repo's default automation path is now safer for day-to-day development because it lands on `Canopy-Dev`, not the public repo.
- Public pushes require an explicit repo override and should be treated as a separate review gate, not a normal development sync.
- When syncing to the separate push-folder mirror, copy only the changed files for the reviewed release instead of performing a blanket folder sync.

**Change made:** Public-facing workflow language was refreshed so release reviewers are looking at the current promotion model rather than older local-folder assumptions.

---

### 3. Hardcoded personal paths in `scripts/start_canopy_dev.ps1` - Fixed (2026-04-07)

**What was found:** The PowerShell launch script contained two hardcoded personal machine values:
- Default `$RepoPath` was set to `d:\Dropbox\Python Toolbox\Canopy` (a personal Dropbox path).
- `CANOPY_DATA_DIR` was hardcoded to `C:\Users\konra\canopy_data` (a personal username) in both the PowerShell assignment and the embedded Python subprocess block.

**Change made:**
- Default `$RepoPath` now uses `Split-Path $PSScriptRoot -Parent`, resolving the repo root from the script's own location so the script runs correctly from any clone.
- `CANOPY_DATA_DIR` now defaults to `$env:LOCALAPPDATA\Canopy` when the environment variable is not already set, consistent with the documented Windows runtime data location. If `CANOPY_DATA_DIR` is already set in the environment it is left unchanged.
- The embedded Python subprocess block now references `$dataDir` (the resolved PowerShell variable) instead of the former hardcoded path, keeping both code paths consistent.
- The script comment was updated to remove the "Canopy-Dev" internal repo name.

---

### 4. "Canopy-Dev" internal repo name in `docs/GITHUB_RELEASE_ANNOUNCEMENT_DRAFT.md` - Fixed (2026-04-07)

**What was found:** The addendum section of the release announcement draft used the internal development repo name: "Use these bullets when announcing or testing **Canopy-Dev** / recent mesh builds."

**Change made:** Replaced with "Use these bullets when announcing or testing recent Canopy builds." - no internal naming in the public announcement template.

---

## Clean - No Action Required

| Area | Status |
|------|--------|
| Hardcoded credentials / API tokens | None found - env vars and placeholder values only |
| `.env` / `secrets.env` files committed | None found - properly excluded by `.gitignore` |
| `.cursor/` and `.cursorrules` | Not committed - excluded by `.gitignore` |
| `agent_note/` and `AGENT_NOTE_*.md` files | Not committed - excluded by `.gitignore` |
| Internal push/MCP/setup docs | Not committed - excluded by `.gitignore` |
| Cryptographic key files (`*.key`, `*.pem`, `*.cert`) | Not committed - excluded by `.gitignore` |
| Database files and `data/` directory | Not committed - excluded by `.gitignore` |
| `config/production.ini` / `config/secrets.env` | Not committed - excluded by `.gitignore` |
| `cursor-mcp-config.json` (live) | Not committed - excluded by `.gitignore` |
| Internal workflow scripts under `scripts/` | Keep in the private development mirror for maintainer use, but exclude the internal push, Copilot, posting, cache-export, and local-recovery helpers from public promotion (see section above) |
| `docs/SECURITY_ASSESSMENT.md` | Public-appropriate security documentation; no sensitive specifics |
| `docs/RECOVERY_LOCK_STORM.md` | Mentions Dropbox as a general WAL-contention scenario; appropriate operational context |
| Version alignment | Current app/package/README badge versions are aligned to `0.6.114`; API, MCP, agent onboarding, quickstart, peer-connect, and mentions docs now declare the same release scope |
| Meshspaces public explanation | README, QUICKSTART, and AGENT_ONBOARDING explain Meshspaces as the supported multi-workspace path with clear scope |
| First-run UX | QUICKSTART covers Windows nontechnical, technical repo, and agent operator paths; install rough edges section is present |
| Windows/nontechnical clarity | WINDOWS_TRAY.md covers install, verify, upgrade, and rollback with no Python setup required |

---

## `.gitignore` Coverage

The existing `.gitignore` is comprehensive. It covers:

- Python build artifacts and virtual environments
- IDE folders (`.vscode/`, `.idea/`, `.cursor/`)
- Environment and secret config files
- Agent note archives and internal milestone docs
- Internal GitHub push / MCP setup docs
- Cryptographic material (`*.key`, `*.pem`, `*.cert`)
- Database and data files
- OS metadata files

The 2026-05-14 pass added broader patterns for internal posting, Copilot/MCP orchestration, GitHub push/sync/validation helpers, and local channel test helpers. These patterns protect new local files with the same names, but tracked files in the private development mirror still require explicit exclusion during public promotion.

---

## Summary

The current pass refreshed the audit for `0.6.114`, updated drifted public docs, and tightened ignore coverage for local-only automation and generated backup artifacts. Earlier 0.6.0 cleanup removed personal machine paths from `scripts/start_canopy_dev.ps1` and removed the internal development repo name from the public release announcement draft. No credentials, tokens, or other genuinely sensitive material were found in committed files during the earlier audit.

The public promotion must exclude this audit log and the broader internal workflow script set used for dev-mirror pushes, Copilot orchestration, bot posting, cached exports, and local recovery/testing helpers. Public-safe scripts should stay limited to generic contributor, migration, and Windows packaging utilities.
