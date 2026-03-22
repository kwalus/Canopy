"""Typed source composition metadata for posts and messages."""

from __future__ import annotations

from typing import Any, Dict, Optional


_ALLOWED_REF_PREFIXES = ("attachment:", "widget:", "content:")
_ALLOWED_PLACEMENTS = {"right", "strip", "below"}
_ALLOWED_LEDE_KINDS = {"rich_text"}
_ALLOWED_ACTION_KINDS = {"link"}


def _normalize_ref(value: Any) -> Optional[str]:
    ref = str(value or "").strip()
    if not ref or len(ref) > 256:
        return None
    if not ref.startswith(_ALLOWED_REF_PREFIXES):
        return None
    return ref


def _normalize_hero(raw: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(raw, dict):
        return None
    ref = _normalize_ref(raw.get("ref"))
    if not ref:
        return None
    payload: Dict[str, Any] = {"ref": ref}
    label = str(raw.get("label") or "").strip()
    if label:
        payload["label"] = label[:120]
    return payload


def _normalize_lede(raw: Any) -> Optional[Dict[str, Any]]:
    if raw is True:
        return {"kind": "rich_text", "ref": "content:lede"}
    if not isinstance(raw, dict):
        return None
    kind = str(raw.get("kind") or "rich_text").strip().lower()
    if kind not in _ALLOWED_LEDE_KINDS:
        return None
    ref = _normalize_ref(raw.get("ref") or "content:lede")
    if ref != "content:lede":
        return None
    return {"kind": kind, "ref": ref}


def _normalize_supporting(raw: Any) -> list[Dict[str, Any]]:
    items: list[Dict[str, Any]] = []
    if not isinstance(raw, list):
        return items
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        ref = _normalize_ref(entry.get("ref"))
        placement = str(entry.get("placement") or "").strip().lower()
        if not ref or placement not in _ALLOWED_PLACEMENTS:
            continue
        payload: Dict[str, Any] = {"ref": ref, "placement": placement}
        label = str(entry.get("label") or "").strip()
        if label:
            payload["label"] = label[:120]
        items.append(payload)
    return items


def _normalize_actions(raw: Any) -> list[Dict[str, Any]]:
    items: list[Dict[str, Any]] = []
    if not isinstance(raw, list):
        return items
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        kind = str(entry.get("kind") or "link").strip().lower()
        label = str(entry.get("label") or "").strip()
        url = str(entry.get("url") or "").strip()
        if kind not in _ALLOWED_ACTION_KINDS or not label or not url:
            continue
        # Absolute https/http, or same-origin path only — reject protocol-relative "//evil"
        # and other schemes (javascript:, data:, etc.).
        if url.startswith("https://") or url.startswith("http://"):
            pass
        elif url.startswith("/") and not url.startswith("//"):
            pass
        else:
            continue
        items.append(
            {
                "kind": kind,
                "label": label[:80],
                "url": url[:2048],
            }
        )
    return items


def _normalize_deck(raw: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(raw, dict):
        return None
    default_ref = _normalize_ref(raw.get("default_ref"))
    if not default_ref:
        return None
    return {"default_ref": default_ref}


def normalize_source_layout(raw: Any) -> Optional[Dict[str, Any]]:
    """Return a normalized source_layout payload or ``None``."""
    if not isinstance(raw, dict):
        return None

    layout: Dict[str, Any] = {"version": 1}

    hero = _normalize_hero(raw.get("hero"))
    if hero:
        layout["hero"] = hero

    lede = _normalize_lede(raw.get("lede"))
    if lede:
        layout["lede"] = lede

    supporting = _normalize_supporting(raw.get("supporting"))
    if supporting:
        layout["supporting"] = supporting

    actions = _normalize_actions(raw.get("actions"))
    if actions:
        layout["actions"] = actions

    deck = _normalize_deck(raw.get("deck"))
    if deck:
        layout["deck"] = deck

    if len(layout) == 1:
        return None
    return layout
