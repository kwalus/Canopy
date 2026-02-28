"""
Lightweight poll parsing and helpers for Canopy.

Polls are defined using a simple text format so humans and agents can create them
without new UI controls. Example formats:

1) Block format:
[poll]
What should we ship next?
- Reliability
- New UI polish
- MCP improvements
duration: 3d
[/poll]

2) Inline format:
poll: What should we ship next?
- Reliability
- New UI polish
duration: 3d

Author: Konrad Walus (architecture, design, and direction)
Project: Canopy - Local Mesh Communication
License: Apache 2.0
Development: AI-assisted implementation (Claude, Codex, GitHub Copilot, Cursor IDE, Ollama)
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import List, Optional, Tuple

DEFAULT_POLL_DURATION_SECONDS = 7 * 24 * 3600  # 7 days
POLL_EDIT_WINDOW_SECONDS = 10 * 60  # 10 minutes


@dataclass
class PollSpec:
    question: str
    options: List[str]
    duration_seconds: Optional[int] = None
    expires_at: Optional[datetime] = None


_POLL_BLOCK_PATTERNS = [
    re.compile(r"(?is)\[poll\](.*?)\[/poll\]"),
    re.compile(r"(?is)::poll\s*(.*?)\s*::endpoll"),
]


def _parse_datetime(value: str) -> Optional[datetime]:
    if not value:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except Exception:
        try:
            dt = datetime.strptime(raw, "%Y-%m-%d")
        except Exception:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _parse_duration_seconds(value: str) -> Optional[int]:
    if not value:
        return None
    raw = str(value).strip().lower()
    if not raw:
        return None
    if raw in {"none", "no", "never"}:
        return None
    if raw in {"quarter", "q"}:
        return 90 * 24 * 3600
    if raw in {"year", "yr", "y"}:
        return 365 * 24 * 3600
    m = re.match(r"^(\d+)\s*([a-z]+)?$", raw)
    if not m:
        return None
    value_num = int(m.group(1))
    unit = (m.group(2) or "s").strip()
    if unit in {"s", "sec", "secs", "second", "seconds"}:
        return value_num
    if unit in {"m", "min", "mins", "minute", "minutes"}:
        return value_num * 60
    if unit in {"h", "hr", "hrs", "hour", "hours"}:
        return value_num * 3600
    if unit in {"d", "day", "days"}:
        return value_num * 24 * 3600
    if unit in {"w", "wk", "wks", "week", "weeks"}:
        return value_num * 7 * 24 * 3600
    if unit in {"mo", "mon", "month", "months"}:
        return value_num * 30 * 24 * 3600
    return None


def parse_poll(text: str) -> Optional[PollSpec]:
    """Parse poll spec from a text blob. Returns PollSpec or None."""
    if not text:
        return None

    block = None
    for pattern in _POLL_BLOCK_PATTERNS:
        match = pattern.search(text)
        if match:
            block = match.group(1)
            break

    if block is None:
        lines = text.splitlines()
        for idx, line in enumerate(lines):
            stripped = line.strip()
            if not stripped:
                continue
            lower = stripped.lower()
            if lower.startswith("poll:"):
                question_line = stripped.split(":", 1)[1].strip()
                block_lines = [question_line] + lines[idx + 1 :]
                block = "\n".join(block_lines)
            elif lower == "poll":
                block = "\n".join(lines[idx + 1 :])
            break

    if block is None:
        return None

    question = None
    options: List[str] = []
    duration_seconds = None
    expires_at = None

    for line in block.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        lower = stripped.lower()

        if lower.startswith(("duration:", "duration ", "ttl:", "ttl ", "expires:", "expires ", "ends:", "end:")):
            key, val = (stripped.split(":", 1) + [""])[:2]
            key = key.strip().lower()
            val = val.strip()
            if key in {"expires", "ends", "end"}:
                expires_at = _parse_datetime(val)
            else:
                duration_seconds = _parse_duration_seconds(val)
            continue

        if stripped[0] in {"-", "*", "\u2022"}:
            option = stripped.lstrip("-* \u2022").strip()
            if option:
                options.append(option)
            continue

        if question is None:
            if lower.startswith("question:"):
                question = stripped.split(":", 1)[1].strip()
            else:
                question = stripped
            continue

    if not question or len(options) < 2:
        return None

    return PollSpec(
        question=question,
        options=options,
        duration_seconds=duration_seconds,
        expires_at=expires_at,
    )


def resolve_poll_end(created_at: datetime,
                     item_expires_at: Optional[datetime],
                     spec: PollSpec) -> Optional[datetime]:
    """Resolve the poll end time based on spec and surrounding item expiry."""
    if created_at is None:
        created_at = datetime.now(timezone.utc)
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)

    end_dt = None
    if spec.expires_at:
        end_dt = spec.expires_at
    elif spec.duration_seconds:
        end_dt = created_at + timedelta(seconds=spec.duration_seconds)
    else:
        end_dt = created_at + timedelta(seconds=DEFAULT_POLL_DURATION_SECONDS)

    if end_dt and end_dt.tzinfo is None:
        end_dt = end_dt.replace(tzinfo=timezone.utc)

    if item_expires_at:
        exp_dt = item_expires_at
        if exp_dt.tzinfo is None:
            exp_dt = exp_dt.replace(tzinfo=timezone.utc)
        if end_dt and exp_dt < end_dt:
            end_dt = exp_dt

    return end_dt


def summarize_poll(question: str, options: List[str], counts: List[int]) -> str:
    """Build a concise summary for notifications."""
    question = (question or "").strip()
    total = sum(counts or [])
    if not options:
        return f"Poll closed: {question}" if question else "Poll closed"
    if total <= 0:
        return f"Poll closed: {question}" if question else "Poll closed"
    top_index = max(range(len(options)), key=lambda i: counts[i] if i < len(counts) else 0)
    top_label = options[top_index] if top_index < len(options) else "Top option"
    top_votes = counts[top_index] if top_index < len(counts) else 0
    if question:
        return f"Poll closed: {question} — {top_label} ({top_votes} votes)"
    return f"Poll closed — {top_label} ({top_votes} votes)"


def describe_poll_status(end_dt: Optional[datetime], now: Optional[datetime] = None) -> str:
    """Return a human-friendly status string for poll timing."""
    if not end_dt:
        return "No expiry"
    if end_dt.tzinfo is None:
        end_dt = end_dt.replace(tzinfo=timezone.utc)
    now_dt = now or datetime.now(timezone.utc)
    if now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=timezone.utc)
    if end_dt <= now_dt:
        return "Closed"
    delta = end_dt - now_dt
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return "Ends in <1m"
    minutes = seconds // 60
    if minutes < 60:
        return f"Ends in {minutes}m"
    hours = minutes // 60
    if hours < 48:
        return f"Ends in {hours}h"
    days = hours // 24
    return f"Ends in {days}d"


def poll_edit_window_seconds() -> int:
    """Return the poll edit window in seconds."""
    return POLL_EDIT_WINDOW_SECONDS


def poll_edit_lock_reason(created_at: Optional[datetime],
                          votes_total: int = 0,
                          now: Optional[datetime] = None) -> Optional[str]:
    """Return a human-friendly reason if poll edits should be locked."""
    if votes_total and votes_total > 0:
        return "Polls cannot be edited after votes are cast"
    if not created_at:
        return None
    created_dt = created_at
    if created_dt.tzinfo is None:
        created_dt = created_dt.replace(tzinfo=timezone.utc)
    now_dt = now or datetime.now(timezone.utc)
    if now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=timezone.utc)
    if now_dt - created_dt > timedelta(seconds=POLL_EDIT_WINDOW_SECONDS):
        minutes = max(1, int(POLL_EDIT_WINDOW_SECONDS // 60))
        return f"Polls can only be edited within {minutes} minutes of posting"
    return None
