# Start Canopy with local data dir. Default mode is silent/detached.
param(
    [string]$RepoPath = (Split-Path $PSScriptRoot -Parent),
    [switch]$Console
)

$dataDir = if ($env:CANOPY_DATA_DIR) { $env:CANOPY_DATA_DIR } else { Join-Path $env:LOCALAPPDATA "Canopy" }
$env:CANOPY_DATA_DIR = $dataDir
Set-Location $RepoPath
$logPath = Join-Path $RepoPath "canopy_console.log"

if ($Console) {
    & python -m canopy --host 0.0.0.0 --port 7770 2>&1 | Tee-Object -FilePath $logPath
    exit $LASTEXITCODE
}

$python = (Get-Command python -ErrorAction Stop).Source
$cmd = @"
import os, sys, subprocess, pathlib
log_path = pathlib.Path(r"$logPath")
log_path.parent.mkdir(parents=True, exist_ok=True)
env = dict(os.environ)
for key in (
    "CANOPY_MESHSPACE_ID",
    "CANOPY_MESHSPACE_NAME",
    "CANOPY_MESHSPACE_ROOT",
    "CANOPY_MESHSPACE_SUPERVISED",
    "CANOPY_MESHSPACE_NETWORK_QUARANTINED",
    "CANOPY_PORT",
    "CANOPY_MESH_PORT",
    "CANOPY_DISCOVERY_PORT",
):
    env.pop(key, None)
env["CANOPY_DATA_DIR"] = r"$dataDir"
python_exe = sys.executable
candidate = pathlib.Path(python_exe)
if candidate.name.lower() == "python.exe":
    pythonw = candidate.with_name("pythonw.exe")
    if pythonw.exists():
        python_exe = str(pythonw)
flags = getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
flags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
flags |= getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
flags_with_breakaway = flags | getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0x01000000)
fh = open(log_path, "ab")
kwargs = {
    "cwd": r"$RepoPath",
    "env": env,
    "stdin": subprocess.DEVNULL,
    "stdout": fh,
    "stderr": subprocess.STDOUT,
}
try:
    kwargs["creationflags"] = flags_with_breakaway
    subprocess.Popen([python_exe, "-m", "canopy", "--host", "0.0.0.0", "--port", "7770"], **kwargs)
except OSError:
    kwargs["creationflags"] = flags
    subprocess.Popen([python_exe, "-m", "canopy", "--host", "0.0.0.0", "--port", "7770"], **kwargs)
print(log_path)
"@

& $python -c $cmd
