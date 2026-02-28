"""
Icon helpers for the Canopy system tray application.

Loads the Canopy logo and generates tinted variants for different states:
- Green: connected to peers
- Grey: no peers connected
- Red tint: server is down
- Badge dot: unread messages

Author: Konrad Walus (architecture, design, and direction)
Project: Canopy - Local Mesh Communication
License: Apache 2.0
Development: AI-assisted implementation (Claude, Codex, GitHub Copilot, Cursor IDE, Ollama)
"""

import os
import logging
from pathlib import Path
from typing import Optional
from PIL import Image, ImageDraw, ImageEnhance

logger = logging.getLogger(__name__)

# Icon size for the system tray (Windows uses 16x16 or 32x32 typically)
TRAY_ICON_SIZE = (64, 64)

# Paths to icon assets
_ASSETS_DIR = Path(__file__).parent / "assets"
_ICO_PATH = _ASSETS_DIR / "canopy.ico"

# Fallback: source PNG from the logos folder
_LOGO_DIR = Path(__file__).parent.parent / "logos"
_PNG_PATH = _LOGO_DIR / "canopy_notxt.png"
_LANCZOS = getattr(getattr(Image, "Resampling", Image), "LANCZOS")


def _find_logo() -> Path:
    """Find the best available logo file."""
    if _PNG_PATH.exists():
        return _PNG_PATH
    if _ICO_PATH.exists():
        return _ICO_PATH
    raise FileNotFoundError(
        f"No Canopy logo found. Checked:\n  {_PNG_PATH}\n  {_ICO_PATH}"
    )


def _load_base_icon() -> Image.Image:
    """Load and resize the base Canopy logo for tray use."""
    logo_path = _find_logo()
    img = Image.open(logo_path).convert("RGBA")
    img = img.resize(TRAY_ICON_SIZE, _LANCZOS)
    return img


def get_icon_connected() -> Image.Image:
    """Return the normal (connected) tray icon -- full-color logo."""
    return _load_base_icon()


def get_icon_disconnected() -> Image.Image:
    """Return a desaturated (grey) icon for when no peers are connected."""
    img = _load_base_icon()
    # Desaturate by converting to greyscale and back to RGBA
    alpha = img.split()[3]
    grey = img.convert("L").convert("RGBA")
    # Restore original alpha channel
    r, g, b, _ = grey.split()
    grey = Image.merge("RGBA", (r, g, b, alpha))
    return grey


def get_icon_error() -> Image.Image:
    """Return a red-tinted icon for server error state."""
    img = _load_base_icon()
    alpha = img.split()[3]
    # Create a red overlay
    red_overlay = Image.new("RGBA", img.size, (255, 60, 60, 80))
    img = Image.alpha_composite(img, red_overlay)
    # Restore original alpha so the background stays transparent
    r, g, b, _ = img.split()
    img = Image.merge("RGBA", (r, g, b, alpha))
    return img


def get_icon_with_badge(base_icon: Optional[Image.Image] = None) -> Image.Image:
    """Add a notification badge (red dot) to the icon."""
    if base_icon is None:
        base_icon = get_icon_connected()
    img = base_icon.copy()
    draw = ImageDraw.Draw(img)
    w, h = img.size
    # Draw a red circle in the bottom-right corner
    badge_radius = max(w // 6, 4)
    x = w - badge_radius - 2
    y = h - badge_radius - 2
    draw.ellipse(
        [x - badge_radius, y - badge_radius, x + badge_radius, y + badge_radius],
        fill=(255, 40, 40, 255),
        outline=(255, 255, 255, 255),
        width=max(badge_radius // 4, 1),
    )
    return img


def get_ico_path() -> str:
    """Return the path to the .ico file (for PyInstaller/exe icon)."""
    return str(_ICO_PATH)
