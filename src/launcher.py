"""Finding and starting Claude Desktop.

Claude Desktop ships two ways on Windows:

* **Microsoft Store (MSIX)** — the exe lives under the ACL-protected
  ``WindowsApps`` folder and cannot be started by path; it must be
  activated through its Application User Model ID (AUMID) via the
  ``shell:AppsFolder`` namespace.
* **Direct download (Squirrel)** — installs under
  ``%LOCALAPPDATA%\\AnthropicClaude`` and can be started by exe path.

Both are handled here. Process detection matters too: the *Claude Code CLI*
also runs as ``claude.exe`` (under ``%APPDATA%\\Claude\\claude-code``), so a
bare process-name check would false-positive — paths are always verified.

TOS note: this module only *starts* the app the same way a desktop shortcut
would, and observes process existence through public Windows APIs. No
injection, no flags that alter app behavior, no internals.
"""
from __future__ import annotations

import ctypes
import logging
import os
import subprocess
from ctypes import wintypes
from pathlib import Path
from typing import Optional

logger = logging.getLogger("desktop_wrapper.launcher")

#: Path fragments that identify the *Desktop* app's claude.exe (lowercase).
#: The Claude Code CLI is also claude.exe but lives under claude-code\.
_DESKTOP_PATH_MARKERS = ("\\windowsapps\\claude_", "\\anthropicclaude\\")

_TH32CS_SNAPPROCESS = 0x2
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value


class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", ctypes.c_wchar * 260),
    ]


def _iter_processes():
    """Yield ``(pid, exe_name)`` for all processes (Toolhelp snapshot)."""
    kernel32 = ctypes.windll.kernel32
    snap = kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
    if snap == _INVALID_HANDLE_VALUE:
        return
    try:
        entry = _PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
        if not kernel32.Process32FirstW(snap, ctypes.byref(entry)):
            return
        while True:
            yield entry.th32ProcessID, entry.szExeFile
            if not kernel32.Process32NextW(snap, ctypes.byref(entry)):
                break
    finally:
        kernel32.CloseHandle(snap)


def _process_image_path(pid: int) -> Optional[str]:
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        buf = ctypes.create_unicode_buffer(4096)
        size = wintypes.DWORD(len(buf))
        if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return buf.value
        return None
    finally:
        kernel32.CloseHandle(handle)


def _is_desktop_path(path: str) -> bool:
    """True if an exe path belongs to the Claude *Desktop* app."""
    p = path.lower()
    return p.endswith("\\claude.exe") and any(m in p for m in _DESKTOP_PATH_MARKERS)


def claude_desktop_running() -> bool:
    """Whether any Claude Desktop process is alive (path-verified).

    Note: Claude Desktop's X button hides the window to the system tray and
    leaves the process running, so "process alive" does NOT mean "window on
    screen" — use :func:`claude_desktop_window_present` for that.
    """
    for pid, exe in _iter_processes():
        if exe.lower() != "claude.exe":
            continue
        path = _process_image_path(pid)
        if path and _is_desktop_path(path):
            return True
    return False


def claude_desktop_window_present() -> bool:
    """Whether a visible Claude Desktop top-level window exists.

    This is the signal that matters for launch/activate decisions: with the
    app hidden in the tray the process lives on, but the user sees no
    window — activating the app (same as clicking its Start-menu tile)
    brings it back.
    """
    user32 = ctypes.windll.user32
    found: list[int] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _cb(hwnd, _lparam):
        try:
            if not user32.IsWindowVisible(hwnd):
                return True
            cls = ctypes.create_unicode_buffer(64)
            user32.GetClassNameW(hwnd, cls, 64)
            if cls.value != "Chrome_WidgetWin_1":
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            if not length:
                return True
            title = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, title, length + 1)
            if not title.value.startswith("Claude"):
                return True
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            path = _process_image_path(pid.value)
            if path and _is_desktop_path(path):
                found.append(hwnd)
                return False  # stop enumerating
        except Exception:  # never let a callback error break EnumWindows
            pass
        return True

    user32.EnumWindows(_cb, 0)
    return bool(found)


def _find_store_aumid() -> Optional[str]:
    """AUMID of the Store-installed Claude app, via ``Get-StartApps``."""
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "(Get-StartApps | Where-Object {$_.Name -eq 'Claude'}).AppID"],
            capture_output=True, text=True, timeout=30,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        logger.warning("Get-StartApps failed: %s", e)
        return None
    aumid = (out.stdout or "").strip().splitlines()
    aumid = aumid[0].strip() if aumid else ""
    # A Store AUMID looks like PackageFamilyName!AppId; a plain exe path in
    # Get-StartApps output would not help us here.
    if aumid and "!" in aumid:
        return aumid
    return None


def _squirrel_exe() -> Optional[Path]:
    """Path to a direct-download (Squirrel) install, if present."""
    localappdata = os.environ.get("LOCALAPPDATA")
    if not localappdata:
        return None
    root = Path(localappdata) / "AnthropicClaude"
    if not root.is_dir():
        return None
    top = root / "claude.exe"
    if top.is_file():
        return top
    versions = sorted(root.glob("app-*/claude.exe"), reverse=True)
    return versions[0] if versions else None


def launch_claude_desktop(cached_aumid: Optional[str] = None) -> Optional[str]:
    """Start Claude Desktop if an install can be found.

    Args:
        cached_aumid: A previously discovered Store AUMID (skips the slow
            ``Get-StartApps`` query when provided).

    Returns:
        The AUMID used (worth caching), or ``None`` if launched by path or
        not launched at all. Check :func:`claude_desktop_running` afterwards
        to know whether the launch took.
    """
    aumid = cached_aumid or _find_store_aumid()
    if aumid:
        try:
            # Same activation path a Start-menu tile uses.
            os.startfile(f"shell:AppsFolder\\{aumid}")
            logger.info("launched Claude Desktop via AUMID %s", aumid)
            return aumid
        except OSError as e:
            logger.warning("AUMID launch failed (%s): %s", aumid, e)
    exe = _squirrel_exe()
    if exe:
        try:
            subprocess.Popen([str(exe)], close_fds=True)
            logger.info("launched Claude Desktop from %s", exe)
        except OSError as e:
            logger.warning("exe launch failed (%s): %s", exe, e)
        return None
    logger.warning("no Claude Desktop installation found to launch")
    return None
