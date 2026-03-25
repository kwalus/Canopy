# Windows Tray

`canopy_tray` is the packaged Windows path for users who do not want to manage Python directly. This is the recommended Canopy install path for nontechnical Windows users when a published packaged build is available.

## What this path is for

Choose the Windows tray path when you want:
- a per-user installer or packaged app folder
- a tray icon that starts and stops the local server
- runtime data kept outside the repo
- a documented manual upgrade and rollback path

## Typical release artifacts for Windows users

When a packaged Windows release is available, it will usually include:
- `CanopyTraySetup-<version>.exe` when the installer build is produced
- packaged `Canopy` folder containing `Canopy.exe` as the fallback distribution
- a checksum file when the release process provides one

## Compatibility Notes

The tray app is reviewed against Canopy `0.5.0`.

- Peer status uses `/api/v1/p2p/peers` with fallback to `/api/v1/p2p/known_peers`.
- Message notifications use `/api/v1/channels` and `/api/v1/channels/<channel_id>/messages`.
- Local user identity is resolved through `/api/v1/auth/status` so the tray does not toast the user about their own posts.
- If no owner account exists yet, the tray starts the server but does not mint a placeholder API key.

## Install

1. Download `CanopyTraySetup-<version>.exe` from the release page when that installer artifact is available.
2. If the installer artifact is not available, use the packaged `Canopy` folder containing `Canopy.exe`.
3. Launch Canopy from the Start menu, tray shortcut, or packaged app folder.
4. Open `http://localhost:7770`.
5. Create your local account.

## Verify

After install, verify:
- the tray icon appears
- `http://localhost:7770` opens
- the tray menu can open the runtime folder
- your local account can post in `#general`
- the runtime data remains under `%LOCALAPPDATA%\Canopy`

## Upgrade

This path is manual in v1.

1. Exit the Canopy tray app.
2. Download the new `CanopyTraySetup-<version>.exe` when it is available.
3. Run the installer over the existing per-user install, or replace the packaged app folder with the new build.
4. Relaunch Canopy.
5. Verify `http://localhost:7770` opens and the existing local data is still present.

## Rollback

If the new build regresses:
1. Exit the tray app.
2. Reinstall the previous known-good `CanopyTraySetup-<previous>.exe` when available, or restore the previous packaged `Canopy` folder.
3. Relaunch and repeat the verification checklist.
4. Use the packaged `Canopy` folder containing `Canopy.exe` as the fallback troubleshooting path if the installer route is unavailable.

## Maintainer build path

This section is for maintainers packaging the release artifacts above, not for end users installing Canopy on Windows.

From a Windows PowerShell prompt at the repository root:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\build_tray_windows.ps1
```

This script:

1. creates `venv` if needed
2. installs `.[tray,tray-build]`
3. builds the tray bundle with PyInstaller
4. optionally builds a Windows installer when Inno Setup 6 is available

Outputs:

- `dist\Canopy\Canopy.exe`
- `dist\CanopyTraySetup-<version>.exe` when Inno Setup is installed

To skip installer creation:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\build_tray_windows.ps1 -SkipInstaller
```

## Installer Requirements

The optional installer step uses [Inno Setup 6](https://jrsoftware.org/isinfo.php).
If `ISCC.exe` is on the machine, the build script will detect it automatically.

The installer is per-user and installs into:

```text
%LOCALAPPDATA%\Canopy Tray
```

## Runtime Behavior

- The tray app can launch Canopy automatically at Windows login using the built-in `Start with Windows` menu item.
- The tray app opens the Canopy web UI at `http://localhost:7770`.
- The tray app exposes `Reconnect All Peers` from the tray menu.
- Toast notifications are rate-limited per channel to reduce spam.

## Operational Notes

- The tray notification key is read-only and stored in `tray_state.json` under the tray runtime directory.
- If the local owner account changes, the tray will rotate to a matching key automatically on next startup.
- If you need to troubleshoot the tray runtime, open the tray menu and use `Open Canopy Folder`.
