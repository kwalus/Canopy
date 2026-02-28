# Recovering from a database lock storm

If Canopy hangs at startup on “Creating database tables…” and never binds to port 7770, you may be hitting a **SQLite lock storm** (e.g. stale WAL/shm files, Dropbox locking, or a previous crash).

## Quick recovery

1. **Stop all Canopy processes**
   ```bash
   pkill -f canopy.main
   sleep 2
   ```

2. **Run the recovery script** (stops Canopy if needed, then renames stale WAL/shm so SQLite can open cleanly):
   ```bash
   cd "/path/to/Canopy"
   ./venv/bin/python scripts/recover_db_lock.py
   ```

3. **Start Canopy again**
   ```bash
   ./venv/bin/python -m canopy.main --host 0.0.0.0 --port 7770
   ```

## Manual recovery

If you prefer to do it by hand:

1. Stop Canopy: `pkill -f canopy.main` and wait a few seconds.
2. Go to your device DB directory, e.g. `data/devices/<device_id>/`.
3. If you see `canopy.db-wal` and/or `canopy.db-shm`, rename them (e.g. to `canopy.db-wal.bak` and `canopy.db-shm.bak`) so no process is using them.
4. Start Canopy again.

## Why this happens

- **WAL mode**: SQLite uses `-wal` and `-shm` files. If a process crashes or is killed, they can be left in a state that blocks the next connection.
- **Dropbox**: The DB lives under Dropbox; sync or scanning can briefly lock files and add contention.
- **Multiple starts**: Starting Canopy several times in a row can pile up lock waits (e.g. 3 s timeout in code) and look like a hang.

Using the script or manual steps clears the stale WAL/shm so the next startup can open the DB cleanly. Your existing `canopy.db` data is kept; only the journal files are renamed.
