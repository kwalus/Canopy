#!/usr/bin/env python3
"""
One-off migration: copy files from the Dropbox device folder into canopy_data/files
and update the files table so file_path points to the new location.

Run with CANOPY_DATA_DIR in use (or set it here). Safe to run while Canopy is
stopped or running (SQLite allows concurrent read; we do a short UPDATE).

Usage:
  python scripts/migrate_files_to_canopy_data.py

Paths (edit if needed):
  OLD_FILES: source device files folder (e.g. Dropbox data/devices/<id>/files)
  CANOPY_DATA: target data dir (e.g. C:\\Users\\konra\\canopy_data)
"""
import os
import shutil
import sqlite3
import sys
from pathlib import Path

# Source: Dropbox project device folder that had the files
OLD_FILES = Path(r"D:\Dropbox\Python Toolbox\Canopy\data\devices\dec97423\files")
# Target: local canopy data dir (same as CANOPY_DATA_DIR)
CANOPY_DATA = Path(os.environ.get("CANOPY_DATA_DIR", r"C:\Users\konra\canopy_data"))
NEW_FILES = CANOPY_DATA / "files"
DB_PATH = CANOPY_DATA / "canopy.db"


def normalize_path(p: str) -> str:
    """Use forward slashes for DB consistency with FileManager."""
    return (p or "").replace("\\", "/").strip()


def main() -> None:
    if not OLD_FILES.exists():
        print(f"Source folder does not exist: {OLD_FILES}", file=sys.stderr)
        sys.exit(1)
    if not CANOPY_DATA.exists():
        print(f"Target data dir does not exist: {CANOPY_DATA}", file=sys.stderr)
        sys.exit(1)
    if not DB_PATH.exists():
        print(f"Database not found: {DB_PATH}", file=sys.stderr)
        sys.exit(1)

    # 1) Copy files preserving directory structure
    new_paths = []
    for src in OLD_FILES.rglob("*"):
        if not src.is_file():
            continue
        rel = src.relative_to(OLD_FILES)
        dst = NEW_FILES / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        new_paths.append(dst)

    print(f"Copied {len(new_paths)} files to {NEW_FILES}")

    # 2) Path prefixes we might see in the DB (normalized)
    old_prefixes = [
        normalize_path(str(OLD_FILES)),
        "data/devices/dec97423/files",
        "data\\devices\\dec97423\\files".replace("\\", "/"),
    ]
    new_prefix = normalize_path(str(NEW_FILES))

    # 3) Backup DB then update file_path
    backup = CANOPY_DATA / "canopy.db.pre_file_migration"
    if not backup.exists():
        shutil.copy2(DB_PATH, backup)
        print(f"Backed up database to {backup}")

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
            for prefix in old_prefixes:
                if new_path.startswith(prefix):
                    # Replace prefix with new root; keep trailing path
                    rest = new_path[len(prefix) :].lstrip("/")
                    new_path = f"{new_prefix}/{rest}" if rest else new_prefix
                    break
            else:
                # No old prefix matched; leave path unchanged
                continue
            conn.execute("UPDATE files SET file_path = ? WHERE id = ?", (new_path, row["id"]))
            updated += 1
        conn.commit()
        print(f"Updated file_path for {updated} rows in files table.")
    finally:
        conn.close()

    print("Done. Restart Canopy if it is running so it picks up the new paths.")


if __name__ == "__main__":
    main()
