#!/usr/bin/env python3
"""
Post a multimedia channel message with source_layout v1: three images, actions,
a minimal Canopy module bundle, and deck default on the module.

Requires an API key with write_files + write_feed (and channel membership in #testing).

Usage:
  python3 scripts/post_source_layout_multimedia_demo.py
  CANOPY_API_KEY=... python3 scripts/post_source_layout_multimedia_demo.py
  CANOPY_BASE=http://127.0.0.1:7770 CANOPY_API_KEY=cnpy_... python3 ...
"""

from __future__ import annotations

import base64
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

# Minimal valid PNG (1×1 transparent) — three uploads get distinct file_ids.
_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)

# Tiny valid Canopy module surface (validated as text/html + .canopy-module.html).
_MINIMAL_MODULE_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Layout demo surface</title>
  <style>
    :root { color-scheme: dark; }
    body { margin: 0; font-family: system-ui, sans-serif; background: #0b1220; color: #e2e8f0;
      padding: 1rem; line-height: 1.5; }
    h1 { font-size: 1.1rem; margin: 0 0 0.5rem; }
    p { margin: 0; opacity: 0.9; }
  </style>
</head>
<body>
  <main>
    <h1>Canopy module (demo)</h1>
    <p>Bound module surface for source_layout multimedia test. Open via deck or card.</p>
  </main>
</body>
</html>
"""


def _read_key() -> str:
    env = (os.environ.get("CANOPY_API_KEY") or "").strip()
    if env:
        return env
    p = Path.home() / ".canopy" / "canopy_dev_bot_api_key"
    try:
        return p.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _req(base: str, key: str, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    url = base.rstrip("/") + path
    headers = {"X-API-Key": key}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    r = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(r, timeout=60) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = (e.read() or b"{}").decode("utf-8")
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, {"error": raw or str(e.reason)}


def _upload_png(base: str, key: str, name: str) -> str:
    st, j = _req(
        base,
        key,
        "POST",
        "/api/v1/files/upload",
        {"filename": name, "content_type": "image/png", "data": _PNG_B64},
    )
    if st != 201:
        raise RuntimeError(f"Upload {name} failed HTTP {st}: {j}")
    fid = j.get("file_id")
    if not fid:
        raise RuntimeError(f"No file_id in upload response: {j}")
    return str(fid)


def _upload_module(base: str, key: str) -> str:
    raw = _MINIMAL_MODULE_HTML.encode("utf-8")
    st, j = _req(
        base,
        key,
        "POST",
        "/api/v1/files/upload",
        {
            "filename": "layout-demo-v1.canopy-module.html",
            "content_type": "text/html",
            "data": base64.b64encode(raw).decode("ascii"),
        },
    )
    if st != 201:
        raise RuntimeError(f"Module upload failed HTTP {st}: {j}")
    fid = j.get("file_id")
    if not fid:
        raise RuntimeError(f"No file_id for module: {j}")
    return str(fid)


def _resolve_testing_channel(base: str, key: str) -> str:
    st, j = _req(base, key, "GET", "/api/v1/channels", None)
    if st != 200:
        raise RuntimeError(f"List channels failed {st}: {j}")
    rows = j if isinstance(j, list) else j.get("channels") or []
    for c in rows:
        name = str(c.get("name") or "").lower()
        if name == "testing":
            return str(c.get("id") or "")
    for c in rows:
        if "test" in str(c.get("name") or "").lower():
            return str(c.get("id") or "")
    raise RuntimeError("No #testing (or similar) channel found.")


def main() -> int:
    base = (os.environ.get("CANOPY_BASE") or "http://127.0.0.1:7770").strip()
    key = _read_key()
    if not key:
        print("Set CANOPY_API_KEY or ~/.canopy/canopy_dev_bot_api_key", file=sys.stderr)
        return 1

    try:
        f1 = _upload_png(base, key, "layout-demo-a.png")
        f2 = _upload_png(base, key, "layout-demo-b.png")
        f3 = _upload_png(base, key, "layout-demo-c.png")
        mod = _upload_module(base, key)
    except RuntimeError as e:
        print(str(e), file=sys.stderr)
        print(
            "\nTip: Dev Bot keys often need the write_files permission to upload attachments.",
            file=sys.stderr,
        )
        return 1

    wid = f"widget:module:{mod}"
    channel_id = _resolve_testing_channel(base, key)

    source_layout = {
        "version": 1,
        "hero": {"ref": f"attachment:{f1}", "label": "Hero still"},
        "lede": True,
        "supporting": [
            {"ref": f"attachment:{f2}", "placement": "right", "label": "Side still"},
            {"ref": f"attachment:{f3}", "placement": "strip", "label": "Strip gallery"},
            {"ref": wid, "placement": "below", "label": "Canopy module"},
        ],
        "actions": [
            {"kind": "link", "label": "Feed", "url": "/feed"},
            {"kind": "link", "label": "Docs", "url": "https://github.com/kwalus/Canopy"},
        ],
        "deck": {"default_ref": wid},
    }

    content = (
        "**Multimedia source_layout** — hero + lede + **side** + **strip** + **module** + deck. "
        "Three image attachments plus a `.canopy-module.html` bundle."
    )

    st, out = _req(
        base,
        key,
        "POST",
        "/api/v1/channels/messages",
        {
            "channel_id": channel_id,
            "content": content,
            "attachments": [{"id": f1}, {"id": f2}, {"id": f3}, {"id": mod}],
            "source_layout": source_layout,
        },
    )
    if st not in (200, 201):
        print(f"Post failed HTTP {st}: {out}", file=sys.stderr)
        return 1
    msg = out.get("message") or {}
    print(json.dumps({"channel_id": channel_id, "message_id": msg.get("id"), "success": True}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
