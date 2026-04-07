# Public Release Audit - Canopy 0.6.0

> **Canopy-Dev internal - do not include in the public `kwalus/Canopy` promotion PR.**
> This document is a working audit log for the private dev mirror only.

**Date:** 2026-04-07  
**Scope:** Public-release readiness for the 0.6.0 line - repo hygiene, docs clarity, public-safe wording, accidental internal-only leakage, first-run/setup friction, and obvious release blockers.

---

## Files to Exclude from Public Canopy Promotion

The following files live in `kwalus/Canopy-Dev` for good reasons but must **not** be copied or promoted into the public `kwalus/Canopy` repo:

| File or pattern | Reason |
|------|--------|
| `docs/PUBLIC_RELEASE_AUDIT.md` | Canopy-Dev-internal audit log; references private dev-mirror workflow details |
| `scripts/push_to_github.py`, `scripts/push_one_file_mcp.py`, `scripts/deploy_to_github_mcp.py`, `scripts/push_docs_to_github.py`, `scripts/validate_github_sync.py` | Internal GitHub/MCP manager helpers for the private dev-mirror workflow |
| `scripts/sync_to_github_folder.py`, `scripts/list_pushable_files.py` | Internal promotion/mirror helpers used to curate files into the separate push-folder workflow |
| `scripts/assign_copilot_branch_safe.py`, `scripts/create_*_mcp.py`, `scripts/merge_copilot_prs.py`, `scripts/cleanup_github_repo.py`, `scripts/meshspace*.json`, `scripts/meshspaces*.json` | Internal Copilot, issue, PR, and MCP orchestration tooling/specs |
| `scripts/post_*.py`, `scripts/fetch_recent_general_messages.py`, `scripts/windy_*.py`, `scripts/approve_windy_and_post.py`, `scripts/setup_windy_and_post_general.py`, `scripts/set_windy_avatar.py` | Internal bot/persona/channel automation scripts and local messaging helpers |
| `scripts/recover_db_lock.py`, `scripts/test_channel_image_and_delete.py`, `scripts/perplex_posts/*.json` | Local recovery/test helpers and cached internal exports, not public release assets |

When assembling the public promotion PR, omit these files entirely. Public-safe scripts should be limited to generic contributor and packaging utilities such as `scripts/bump_version.py`, `scripts/build_tray_windows.ps1`, `scripts/canopy_tray_installer.iss`, `scripts/check_jinja_templates.py`, `scripts/filter_python_diagnostics_to_diff.py`, and the migration helpers.

---

## What Was Audited

- All committed docs, scripts, config files, and repo metadata
- `.gitignore` rules and any gaps in coverage
- Secrets, tokens, credentials, and personal/private path references
- Agent scratch artifacts, internal-only setup guides, and local-machine details

---

## Findings and Changes

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
| Internal workflow scripts under `scripts/` | Keep in `Canopy-Dev` for maintainer use, but exclude the internal push, Copilot, posting, cache-export, and local-recovery helpers from public promotion (see section above) |
| `docs/SECURITY_ASSESSMENT.md` | Public-appropriate security documentation; no sensitive specifics |
| `docs/RECOVERY_LOCK_STORM.md` | Mentions Dropbox as a general WAL-contention scenario; appropriate operational context |
| Version alignment | `pyproject.toml`, README badges, CHANGELOG, QUICKSTART, AGENT_ONBOARDING, WINDOWS_TRAY all reference `0.6.0` consistently |
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

No gaps identified that require additions.

---

## Summary

Three changes were made in the 0.6.0 release pass. Personal machine paths (a Dropbox repo path and a hardcoded username) were removed from `scripts/start_canopy_dev.ps1` and replaced with portable defaults. The internal "Canopy-Dev" repo name was removed from the public release announcement draft. No credentials, tokens, or other genuinely sensitive material were found in committed files. The `.gitignore` rules are thorough and correctly exclude local-only artifacts. Version references, Meshspaces public explanation, first-run UX, and Windows nontechnical clarity are all in good shape for the 0.6.0 release line.

The public promotion must exclude this audit log and the broader internal workflow script set used for dev-mirror pushes, Copilot orchestration, bot posting, cached exports, and local recovery/testing helpers. Public-safe scripts should stay limited to generic contributor, migration, and Windows packaging utilities.
