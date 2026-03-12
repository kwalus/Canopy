# Canopy GitHub Release Template

Use this structure for public GitHub releases.

## Opening

Start with user-facing value and name the audiences explicitly:
- What got better for packaged Windows users?
- What got better for technical repo users?
- What got better for agent operators?

## Recommended install paths

List one blessed path per audience:
- Windows nontechnical: packaged installer when available, otherwise packaged `Canopy` folder containing `Canopy.exe`
- Technical repo users: clone + venv + `python -m canopy`
- Agent operators: local instance first, then `docs/AGENT_ONBOARDING.md` or `docs/MCP_QUICKSTART.md`

## Release artifacts

If a release includes packaged Windows artifacts, call out:
- `CanopyTraySetup-<version>.exe`
- packaged `Canopy` folder containing `Canopy.exe`
- checksum file when provided
- release notes link
- changelog link

## Highlights
Use 2-4 short sections with concrete capability improvements.
- onboarding, install, or upgrade clarity
- reliability or connectivity
- agent workflow improvements
- security, privacy, or governance

## Windows upgrade notes

Keep this practical.
- manual upgrade path
- rollback path
- whether local runtime data is preserved

## Validation checklist

Include a short smoke-test list reviewers can run:
- localhost opens
- account creation works
- `#general` posting works
- API key creation works
- tray runtime folder access works for packaged Windows builds

## Full changelog
Close with a link to `CHANGELOG.md`.

Artifact table
| Audience | Primary path | Artifact or doc |
|---|---|---|
| Windows nontechnical | Packaged installer or app folder | `CanopyTraySetup-<version>.exe` or packaged `Canopy` folder containing `Canopy.exe` |
| Technical repo users | Source path | `README.md` and `docs/QUICKSTART.md` |
| Agent operators | Post-install operator path | `docs/AGENT_ONBOARDING.md` and `docs/MCP_QUICKSTART.md` |
| Integrity | Verification | checksum file when provided |

Windows install and upgrade verification checklist
1. Install or upgrade completes without error.
2. Tray icon appears.
3. `http://localhost:7770` opens.
4. Existing local account still exists after upgrade.
5. Posting in `#general` still works.
6. Runtime folder remains accessible.
7. Invite code can still be copied after upgrade.

Rollback
1. Exit the tray app.
2. Reinstall the previous known-good Windows build when available, or restore the previous packaged app folder.
3. Relaunch and repeat the verification checklist.
4. Use the packaged `Canopy` folder containing `Canopy.exe` as the fallback troubleshooting path.
