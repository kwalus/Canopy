# Public Release Audit - Canopy-Dev

**Date:** 2026-03-24  
**Scope:** Sensitive or internal material that should not ship to a public-facing repository.

---

## What Was Audited

- All committed docs, scripts, config files, and repo metadata
- `.gitignore` rules and any gaps in coverage
- Secrets, tokens, credentials, and personal/private path references
- Agent scratch artifacts, internal-only setup guides, and local-machine details

---

## Findings and Changes

### 1. Personal machine paths in `docs/REPOST_V1_DESIGN_REVIEW.md` - Fixed

**What was found:** Eight absolute file-path references embedded as code-review citations contained the developer's macOS username and Dropbox folder structure.

**Change made:** Replaced every occurrence of the personal absolute prefix with the project-relative path (for example `canopy/core/feed.py:993`). No other content was modified.

---

### 2. GitHub push workflow posture - Reviewed

**Current workflow:** Routine pushes now target the private dev mirror (`kwalus/Canopy-Dev`) by default through `scripts/push_to_github.py` and the local MCP Manager on `localhost:8000`. Public `kwalus/Canopy` pushes are explicit opt-in actions used only for release-reviewed promotions.

**What this means for public review:**

- The repo's default automation path is now safer for day-to-day development because it lands on `Canopy-Dev`, not the public repo.
- Public pushes require an explicit repo override and should be treated as a separate review gate, not a normal development sync.
- When syncing to the separate push-folder mirror, copy only the changed files for the reviewed release instead of performing a blanket folder sync.

**Change made:** Public-facing workflow language was refreshed so release reviewers are looking at the current promotion model rather than older local-folder assumptions.

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
| `scripts/push_to_github.py` | Uses a local MCP Manager endpoint (`localhost:8000`); no credentials hardcoded; defaults to `Canopy-Dev` and requires explicit opt-in for public `Canopy` pushes |
| `docs/SECURITY_ASSESSMENT.md` | Public-appropriate security documentation; no sensitive specifics |
| `docs/RECOVERY_LOCK_STORM.md` | Mentions Dropbox as a general WAL-contention scenario; appropriate operational context |

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

One low-risk hardening change was made: personal absolute paths in a design-review document were replaced with project-relative paths. No credentials, tokens, or other genuinely sensitive material were found in committed files. The `.gitignore` rules are thorough and correctly exclude local-only artifacts.
