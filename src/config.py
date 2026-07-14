"""Persistent settings for the indicator (window position, cached values).

Settings live in a small JSON file. Per the design spec the preferred
location is alongside the executable; if that directory is not writable
(e.g. the exe was installed into Program Files), the file falls back to
``%APPDATA%\\claude-desktop-indicator\\config.json``.

All functions are tolerant: a missing or corrupt config file yields an
empty dict, and failed saves are logged but never raised — losing a window
position must not break the indicator.
"""
from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("desktop_wrapper.config")

CONFIG_FILENAME = "config.json"


def _program_dir() -> Path:
    """Directory of the running program (exe dir when frozen by PyInstaller)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent  # project root, not src/


def _appdata_dir() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home())
    return Path(base) / "claude-desktop-indicator"


def _dir_writable(directory: Path) -> bool:
    probe = directory / ".write_probe"
    try:
        probe.touch()
        probe.unlink()
        return True
    except OSError:
        return False


def get_config_path(preferred_dir: Optional[Path] = None) -> Path:
    """Resolve where the config file lives (see module docstring).

    Args:
        preferred_dir: Override for the primary location (used by tests).
    """
    primary = (preferred_dir or _program_dir())
    if _dir_writable(primary):
        return primary / CONFIG_FILENAME
    fallback = _appdata_dir()
    try:
        fallback.mkdir(parents=True, exist_ok=True)
    except OSError:
        logger.warning("cannot create %s; config will not persist", fallback)
    return fallback / CONFIG_FILENAME


def load_config(path: Optional[Path] = None) -> dict[str, Any]:
    """Read the config file; returns ``{}`` when missing or unreadable."""
    path = path or get_config_path()
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
        logger.warning("config %s is not a JSON object; ignoring", path)
    except FileNotFoundError:
        pass
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("could not read config %s: %s", path, e)
    return {}


def save_config(data: dict[str, Any], path: Optional[Path] = None) -> bool:
    """Write the config file atomically; returns False (and logs) on failure."""
    path = path or get_config_path()
    tmp = path.with_suffix(".json.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)
        return True
    except OSError as e:
        logger.warning("could not save config %s: %s", path, e)
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return False


def load_window_position(cfg: dict[str, Any]) -> Optional[tuple[int, int]]:
    """Extract a saved window position, or ``None`` if absent/invalid."""
    x, y = cfg.get("window_x"), cfg.get("window_y")
    if isinstance(x, int) and isinstance(y, int):
        return x, y
    return None


def store_window_position(cfg: dict[str, Any], x: int, y: int) -> dict[str, Any]:
    """Return ``cfg`` updated with a window position."""
    cfg = dict(cfg)
    cfg["window_x"] = int(x)
    cfg["window_y"] = int(y)
    return cfg
