param(
  [string]$PythonExe = "python",
  [switch]$SkipInstaller
)

$ErrorActionPreference = "Stop"

function Find-Iscc {
  $candidates = @(
    "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
    "${env:ProgramFiles}\Inno Setup 6\ISCC.exe",
    (Get-Command ISCC.exe -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source -ErrorAction SilentlyContinue)
  ) | Where-Object { $_ -and (Test-Path $_) }

  if ($candidates.Count -gt 0) {
    return $candidates[0]
  }
  return $null
}

Write-Host "Building Canopy Tray for Windows..." -ForegroundColor Cyan

$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

$VenvPython = Join-Path $RepoRoot "venv\Scripts\python.exe"
$VenvPip = Join-Path $RepoRoot "venv\Scripts\pip.exe"
$PyInstaller = Join-Path $RepoRoot "venv\Scripts\pyinstaller.exe"

if (-Not (Test-Path $VenvPython)) {
  Write-Host "Creating venv..." -ForegroundColor Cyan
  & $PythonExe -m venv venv
}

Write-Host "Installing tray build dependencies..." -ForegroundColor Cyan
& $VenvPip install --upgrade pip
& $VenvPip install -e ".[tray,tray-build]"

$Version = & $VenvPython -c "from canopy_tray import __version__; print(__version__)"
if (-Not $Version) {
  throw "Failed to resolve canopy_tray version"
}

Write-Host "Running PyInstaller..." -ForegroundColor Cyan
& $PyInstaller .\canopy_tray\build.spec --clean --noconfirm

$DistRoot = Join-Path $RepoRoot "dist"
$TrayDist = Join-Path $DistRoot "Canopy"
$ExePath = Join-Path $TrayDist "Canopy.exe"

if (-Not (Test-Path $ExePath)) {
  throw "Expected tray executable not found at $ExePath"
}

Write-Host ""
Write-Host "Tray build complete." -ForegroundColor Green
Write-Host "Output: $ExePath" -ForegroundColor Green

if ($SkipInstaller) {
  Write-Host "Installer step skipped." -ForegroundColor Yellow
  exit 0
}

$Iscc = Find-Iscc
if (-Not $Iscc) {
  Write-Host "Inno Setup not found. Install Inno Setup 6 and rerun without -SkipInstaller to produce a .exe installer." -ForegroundColor Yellow
  exit 0
}

$InstallerScript = Join-Path $RepoRoot "scripts\canopy_tray_installer.iss"
if (-Not (Test-Path $InstallerScript)) {
  throw "Installer script missing: $InstallerScript"
}

Write-Host "Building Inno Setup installer..." -ForegroundColor Cyan
& $Iscc `
  "/DAppVersion=$Version" `
  "/DBuildRoot=$RepoRoot" `
  "/DSourceDir=$TrayDist" `
  $InstallerScript

$InstallerPath = Join-Path $DistRoot "CanopyTraySetup-$Version.exe"
if (Test-Path $InstallerPath) {
  Write-Host "Installer: $InstallerPath" -ForegroundColor Green
} else {
  Write-Host "Inno Setup completed but expected installer path was not found: $InstallerPath" -ForegroundColor Yellow
}
