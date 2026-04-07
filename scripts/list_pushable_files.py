#!/usr/bin/env python3
"""List file paths that should be pushed to GitHub (respect .gitignore and exclude). No git calls."""
import fnmatch
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# Directory names (or path segments) to exclude - anything under these is skipped
EXCLUDE_DIRS = {
    ".git",
    "__pycache__",
    ".venv", "venv", "env", "ENV", "env.bak", "venv.bak",
    ".vscode", ".idea", ".cursor", ".canopy_runtime",
    "data", "logs", "agent_note", "agents",
    "research and references",
    "flask_session", "peers", "network_state",
    "backup", "backups", "htmlcov", ".tox", ".pytest_cache",
    "site", ".ipynb_checkpoints", ".mypy_cache",
    "develop-eggs", "dist", "downloads", "eggs", "lib", "parts", "sdist", "var", "wheels", "build",
    ".tmp_demo_assets", "canopy.egg-info",
    "provisional", "provisional_next", "patents",
    "node_modules",
    "tmp", "temp",
}

# File glob patterns to exclude (matched against full relative path or name)
EXCLUDE_GLOBS = [
    "*.pyc", "*.pyo", "*$py.class", "*.so", "*.egg", "*.egg-info", ".DS_Store", "*.log",
    ".env", ".env.*", "*.db", "*.db-journal", "cursor-mcp-config.json", "tray_state.json",
    ".cursorrules", "*.swp", "*.swo", "*~", "*.tmp", "*.temp", "*.backup",
    "*.key", "*.pem", "*.cert", ".coverage", ".python-version",
    "P2P_MILESTONE.md", "API_KEY_SETUP.md", "GITHUB_PUSH_RULES.md", "CURSOR_MCP_SETUP.md",
    "START_CANOPY_MCP.md", "QUICK_START.txt", "AGENT_NOTE_*.md", "AGENT_REPLY_*.md",
    "GITHUB_PUSH_GUIDE_FOR_WINDOWS_AGENT.md", "GITHUB_PUSH_RULES.md",
    "docs/DEPLOY_GITHUB.md", "docs/COPILOT_*.md", "docs/TRACE_*.md",
    "docs/PUBLIC_RELEASE_AUDIT.md", "scripts/push_to_github.py", "scripts/push_one_file_mcp.py",
    "scripts/deploy_to_github_mcp.py", "scripts/push_docs_to_github.py", "scripts/validate_github_sync.py",
    "scripts/sync_to_github_folder.py", "scripts/list_pushable_files.py",
    "scripts/push_canopy_dev_from_git_status.py", "scripts/push_lineage_variants_v1_pr.py",
    "scripts/assign_copilot_branch_safe.py", "scripts/create_*_mcp.py", "scripts/merge_copilot_prs.py",
    "scripts/cleanup_github_repo.py", "scripts/post_*.py", "scripts/fetch_recent_general_messages.py",
    "scripts/windy_*.py", "scripts/approve_windy_and_post.py", "scripts/setup_windy_and_post_general.py",
    "scripts/set_windy_avatar.py", "scripts/recover_db_lock.py", "scripts/test_channel_image_and_delete.py",
    "scripts/meshspace*.json", "scripts/meshspaces*.json", "scripts/perplex_posts/*.json",
    "scripts/announce_*.txt", "docs/P2P_ARCHITECTURE.md", "docs/P2P_IMPLEMENTATION.md",
    "build_handover_zip.py", "PATENT_*.md", "PROVISIONAL_*", "CANOPY_*PROVISIONAL_PATENT.*",
    "README_INTERNAL.md", "*.aux", "*.fdb_latexmk", "*.fls", "*.out", "*.toc", "*.bbl", "*.blg", "*.textClipping",
    "filing_checklist.md", "prior_art_*.md", "identifier_dedup_addendum.md",
    "CANOPY_PROVISIONAL_PATENT.*", ".dmypy.json", "dmypy.json",
]

# Exact path prefixes to exclude (relative to repo)
EXCLUDE_PREFIXES = (
    "config/production.ini",
    "config/secrets.env",
    ".env.local",
    ".env.production",
)


def should_exclude(rel_path: str) -> bool:
    parts = Path(rel_path).parts
    for part in parts:
        if part in EXCLUDE_DIRS:
            return True
    for prefix in EXCLUDE_PREFIXES:
        if rel_path == prefix or rel_path.startswith(prefix + "/"):
            return True
    for pattern in EXCLUDE_GLOBS:
        if fnmatch.fnmatch(rel_path, pattern) or fnmatch.fnmatch(Path(rel_path).name, pattern):
            return True
    return False


def main():
    out = []
    for path in REPO.rglob("*"):
        if not path.is_file():
            continue
        try:
            rel = path.relative_to(REPO).as_posix()
        except ValueError:
            continue
        if should_exclude(rel):
            continue
        out.append(rel)
    out.sort()
    for p in out:
        print(p)


if __name__ == "__main__":
    main()
