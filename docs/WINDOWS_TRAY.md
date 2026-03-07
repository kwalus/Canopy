# Windows Tray

`canopy_tray` is the lightweight Windows entry point for users who do not want to manage Python directly.
It launches the local Canopy server, keeps runtime data under the user profile, shows peer status in the tray, and raises toast notifications for new channel activity.

## What It Does

- Starts and stops the local Canopy server from the Windows notification area.
- Stores runtime data under `%LOCALAPPDATA%\Canopy` for packaged builds, so updates do not reset the instance.
- Polls the current Canopy REST API surface under `/api/v1`.
- Uses a read-only tray API key tied to the local owner account.
- Opens the exact channel message when a toast notification is clicked.

## Compatibility Notes

The tray app is reviewed against Canopy `0.4.45`.

- Peer status uses `/api/v1/p2p/peers` with fallback to `/api/v1/p2p/known_peers`.
- Message notifications use `/api/v1/channels` and `/api/v1/channels/<channel_id>/messages`.
- Local user identity is resolved through `/api/v1/auth/status` so the tray does not toast the user about their own posts.
- If no owner account exists yet, the tray starts the server but does not mint a placeholder API key.

## Build The Tray Executable

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
