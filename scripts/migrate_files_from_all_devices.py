#!/usr/bin/env python3
"""
Copy files from ALL device folders (Dropbox data/devices/*/files) into
canopy_data/files and update the files table for any path still pointing
at those devices. Use this to fix avatars/files that lived in a device
folder other than dec97423.

Usage:
  python scripts/migrate_files_from_all_devices.py
"""
import os
import shutil
import sqlite3
import sys
from pathlib import Path

PROJECT_DEVICES = Path(r"D:\Dropbox\Python Toolbox\Canopy\data\devices")
CANOPY_DATA = Path(os.environ.get("CANOPY_DATA_DIR", r"C:\Users\konra\canopy_data"))
NEW_FILES = CANOPY_DATA / "files"
DB_PATH = CANOPY_DATA / "canopy.db"


def normalize_path(p: str) -> str:
    return (p or "").replace("\\", "/").strip()


def main() -> None:
    if not PROJECT_DEVICES.exists():
        print(f"Project devices folder does not exist: {PROJECT_DEVICES}", file=sys.stderr)
        sys.exit(1)
    if not CANOPY_DATA.exists():
        print(f"Target data dir does not exist: {CANOPY_DATA}", file=sys.stderr)
        sys.exit(1)
    if not DB_PATH.exists():
        print(f"Database not found: {DB_PATH}", file=sys.stderr)
        sys.exit(1)

    new_prefix = normalize_path(str(NEW_FILES))
    old_prefixes = []

    # 1) Copy from every device's files folder
    device_dirs = [d for d in PROJECT_DEVICES.iterdir() if d.is_dir()]
    total_copied = 0
    for device_dir in device_dirs:
        old_files = device_dir / "files"
        if not old_files.is_dir():
            continue
        device_id = device_dir.name
        old_prefixes.append(normalize_path(str(old_files)))
        old_prefixes.append(f"data/devices/{device_id}/files")
        for src in old_files.rglob("*"):
            if not src.is_file():
                continue
            rel = src.relative_to(old_files)
            dst = NEW_FILES / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            total_copied += 1
        print(f"  {device_id}: copied from {old_files}")

    print(f"Copied {total_copied} files from {len(device_dirs)} device(s) into {NEW_FILES}")

    # 2) Update DB: any file_path that starts with an old device prefix -> new_prefix
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.execute("SELECT id, file_path FROM files")
        rows = cur.fetchall()
        updated = 0
        for row in rows:
            old_path = (row["file_path"] or "").strip()
            if not old_path:
                continue
            new_path = normalize_path(old_path)
            if new_path.startswith(new_prefix):
                continue  # already migrated
            for prefix in old_prefixes:
                if new_path.startswith(prefix):
                    rest = new_path[len(prefix) :].lstrip("/")
                    new_path = f"{new_prefix}/{rest}" if rest else new_prefix
                    break
            else:
                continue
            conn.execute("UPDATE files SET file_path = ? WHERE id = ?", (new_path, row["id"]))
            updated += 1
        conn.commit()
        print(f"Updated file_path for {updated} rows in files table.")
    finally:
        conn.close()

    print("Done. Restart Canopy if it is running.")


if __name__ == "__main__":
    main()
