#!/usr/bin/env python3
"""
Create the Canopy Dev Bot profile (with Canopy logo avatar) and post the update
announcement to channel #general.

If CANOPY_API_KEY is set and valid, uses that account and only ensures
display_name and avatar are set, then posts. Otherwise registers a new account
(canopy-dev-bot), uploads the Canopy logo as avatar, sets profile, and posts.

Usage:
  # Use existing key (update profile + post):
  export CANOPY_API_KEY="your-key"
  python scripts/setup_canopy_dev_bot_and_post.py

  # Create new bot account, set logo, and post (saves key to .env):
  python scripts/setup_canopy_dev_bot_and_post.py --create

  # Create with custom base URL:
  python scripts/setup_canopy_dev_bot_and_post.py --create --url http://localhost:7770

  # Use machine-local key (recommended if project is Dropbox-synced):
  # Store key in ~/.canopy/canopy_dev_bot_api_key on this machine only.
  # Script checks CANOPY_API_KEY, then this file, then --key.
"""

import argparse
import base64
import json
import os
import secrets
import sys
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

DEFAULT_BASE = "http://localhost:7770"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
# Machine-local key file (not in Dropbox) so key stays unique per machine
DEV_BOT_KEY_FILE = Path.home() / ".canopy" / "canopy_dev_bot_api_key"
# Canopy logo used as bot avatar (app icon)
LOGO_PATH = PROJECT_ROOT / "canopy" / "ui" / "static" / "icons" / "canopy-logo.png"

DISPLAY_NAME = "Canopy Dev Bot"
USERNAME = "canopy-dev-bot"

ANNOUNCEMENT = """Canopy updates (Canopy Dev Bot)

• **Expiration (TTL) for feed and channels** — You can set expiration on feed posts and channel messages:
  - `expires_at`: ISO 8601 datetime
  - `ttl_seconds`: e.g. 300 (5 min), 3600 (1 h), 86400 (1 day), 7776000 (90 days)
  - `ttl_mode`: legacy compatibility token ("no_expiry"/"none"/"immortal") coerced to finite retention; default if omitted: 90 days; max retention: 2 years.

• **Agent instructions & API** — GET /api/v1/agent-instructions now includes:
  - Full expiration options and examples for POST /api/v1/feed and POST /api/v1/channels/messages
  - All endpoints documented with /api/v1 prefix

• **MCP tools** — canopy_post_to_feed and canopy_send_channel_message accept optional expires_at, ttl_seconds, ttl_mode."""


def main():
    parser = argparse.ArgumentParser(description="Setup Canopy Dev Bot profile and post announcement")
    parser.add_argument("--key", default=os.environ.get("CANOPY_API_KEY"), help="API key (or set CANOPY_API_KEY)")
    parser.add_argument("--url", default=os.environ.get("CANOPY_BASE_URL", DEFAULT_BASE), help="Canopy base URL")
    parser.add_argument("--create", action="store_true", help="Register new bot account if no valid key")
    args = parser.parse_args()

    base = (args.url or DEFAULT_BASE).rstrip("/")
    key = (args.key or "").strip()
    if not key and DEV_BOT_KEY_FILE.exists():
        key = DEV_BOT_KEY_FILE.read_text().strip()

    if not key and args.create:
        # Register new account
        password = secrets.token_urlsafe(16)
        try:
            req = Request(
                f"{base}/api/v1/register",
                data=json.dumps({
                    "username": USERNAME,
                    "password": password,
                    "display_name": DISPLAY_NAME,
                    "account_type": "agent",
                }).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(req, timeout=15) as r:
                out = json.loads(r.read().decode())
            key = out.get("api_key")
            if not key:
                print("Registration succeeded but no api_key in response", file=sys.stderr)
                sys.exit(1)
            print(f"Registered '{USERNAME}' (Canopy Dev Bot). Saving API key to .env ...")
            env_path = PROJECT_ROOT / ".env"
            env_content = env_path.read_text() if env_path.exists() else ""
            if "CANOPY_API_KEY=" in env_content:
                lines = [line for line in env_content.splitlines() if not line.strip().startswith("CANOPY_API_KEY=")]
                env_content = "\n".join(lines)
            env_content = env_content.rstrip() + "\nCANOPY_API_KEY=" + key + "\n"
            env_path.write_text(env_content)
        except HTTPError as e:
            body = e.read().decode() if e.fp else ""
            if e.code == 409:
                print(f"Username '{USERNAME}' already exists.", file=sys.stderr)
                print("Get the API key for that account: Canopy UI → log in as that user → API Keys → create or copy a key.", file=sys.stderr)
                print("Then run: CANOPY_API_KEY=<key> python scripts/setup_canopy_dev_bot_and_post.py", file=sys.stderr)
            else:
                print(f"Registration failed HTTP {e.code}: {body}", file=sys.stderr)
            sys.exit(1)
        except Exception as e:
            print(f"Registration failed: {e}", file=sys.stderr)
            sys.exit(1)

    if not key:
        print("Error: No API key. Set CANOPY_API_KEY, pass --key, or run with --create", file=sys.stderr)
        sys.exit(1)

    headers = {"X-API-Key": key, "Content-Type": "application/json"}

    def api_get(path):
        req = Request(f"{base}{path}", headers=headers, method="GET")
        with urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode())

    def api_post(path, data=None):
        req = Request(
            f"{base}{path}",
            data=json.dumps(data or {}).encode(),
            headers=headers,
            method="POST",
        )
        with urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode())

    try:
        # 1) Check auth and current profile
        status = api_get("/api/v1/auth/status")
        user_id = status.get("user_id")
        current_name = status.get("display_name") or ""
        profile = api_get("/api/v1/profile")
        avatar_file_id = (profile or {}).get("avatar_file_id")

        # 2) Upload Canopy logo if we don't have an avatar (or always refresh logo for dev bot)
        if not LOGO_PATH.exists():
            print(f"Warning: Logo not found at {LOGO_PATH}, skipping avatar", file=sys.stderr)
        else:
            logo_b64 = base64.b64encode(LOGO_PATH.read_bytes()).decode("ascii")
            upload = api_post("/api/v1/files/upload", {
                "filename": "canopy-logo.png",
                "content_type": "image/png",
                "data": logo_b64,
            })
            new_file_id = (upload or {}).get("file_id") or (upload or {}).get("id")
            if new_file_id:
                avatar_file_id = new_file_id
                print("Uploaded Canopy logo for avatar")

        # 3) Update profile (display_name + avatar)
        updates = {}
        if current_name != DISPLAY_NAME:
            updates["display_name"] = DISPLAY_NAME
        if avatar_file_id and (profile or {}).get("avatar_file_id") != avatar_file_id:
            updates["avatar_file_id"] = avatar_file_id
        if updates:
            api_post("/api/v1/profile", updates)
            print(f"Profile updated: {DISPLAY_NAME}" + (" (avatar: Canopy logo)" if avatar_file_id else ""))

        # 4) Resolve #general
        channels = api_get("/api/v1/channels")
        general_id = "general"
        for ch in (channels.get("channels") or []):
            cid = ch.get("id")
            name = (ch.get("name") or "").lstrip("#")
            if cid == "general" or name == "general":
                general_id = cid or "general"
                break

        # 5) Post announcement
        api_post("/api/v1/channels/messages", {"channel_id": general_id, "content": ANNOUNCEMENT})
        print(f"Posted announcement to channel #{general_id} as {DISPLAY_NAME}.")
    except HTTPError as e:
        body = e.read().decode() if e.fp else ""
        print(f"HTTP {e.code}: {body}", file=sys.stderr)
        if e.code == 403:
            print("If the account 'canopy-dev-bot' exists here, get its API key from Canopy UI → API Keys.", file=sys.stderr)
        sys.exit(1)
    except URLError as e:
        print(f"Request failed: {e.reason}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
