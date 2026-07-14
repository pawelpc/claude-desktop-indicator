"""Detection of Claude Desktop's current mode and model.

Reads the Claude Desktop window's accessibility tree through the Windows
UI Automation API (via the ``uiautomation`` package). Pure read-only
observation: no clicks, no writes, no injection, no process internals.

Detection design (validated by the Phase 0 spike, see
``_context/phase0-findings.md``):

* The Claude Desktop main window is a top-level ``Chrome_WidgetWin_1``
  window whose owning process image is ``claude.exe``. The process check
  matters: Chrome browser windows share the class name and may contain
  "Claude" in their title.
* The sidebar exposes a pill group ``GroupControl(Name='Mode',
  ClassName='df-pills')`` with "Home" and "Code" pills. The active pill's
  UIA AriaProperties contain ``current=page``; the sliding
  ``df-pill-indicator`` rect overlaps the active pill (fallback signal).
* Chat vs Cowork (both live under "Home") has two signals, in priority
  order. Un-started sessions (URL ``/new``) carry a composer toggle — a
  ``GroupControl(Name='Surface')`` with "Chat"/"Cowork" radio buttons whose
  AriaProperties contain ``checked=true`` on the active one; started
  sessions lose the toggle but gain a distinctive page URL, exposed as the
  UIA Value of the ``RootWebArea`` document: ``/cowork/...`` means Cowork,
  anything else means Chat.
* The model is the composer button with ``haspopup=menu`` whose name is
  ``"Fable 5"`` (Code view) or ``"Model: Opus 4.6 High"`` style (Home
  views). Sidebar session titles may contain model names, so the name
  pattern is anchored to the full string.
* Element references are cached between polls; any COM error or shape
  mismatch triggers a re-find on the next poll. Right after Claude Desktop
  starts, the renderer accessibility tree is empty until UIA queries wake
  it up, so early polls legitimately find nothing ("warm-up").
"""
from __future__ import annotations

import ctypes
import logging
import re
import threading
from ctypes import wintypes
from typing import Callable, Optional
from urllib.parse import urlparse

import uiautomation as auto

logger = logging.getLogger("desktop_wrapper.detector")

#: Poll interval in seconds.
POLL_INTERVAL = 0.5

#: Full-string pattern for the composer's model button text. Allows an
#: optional "Model: " prefix (Home surfaces) and one optional trailing word
#: (the effort level, e.g. "High"). Two or more trailing words — such as a
#: session title like "Sonnet 5 personality comparison" — do not match.
MODEL_BUTTON_RE = re.compile(
    r"^(?:Model:\s*)?(Opus|Sonnet|Haiku|Fable)\s+(\d+(?:\.\d+)?)(?:\s+\w+)?$",
    re.IGNORECASE,
)

_WINDOW_CLASS = "Chrome_WidgetWin_1"
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def parse_model_button_name(name: str) -> Optional[tuple[str, str]]:
    """Parse a composer model-button label into ``(family, version)``.

    Returns lowercase family and the version string, or ``None`` if the
    label does not match the anchored pattern.

    >>> parse_model_button_name("Fable 5")
    ('fable', '5')
    >>> parse_model_button_name("Model: Opus 4.6 High")
    ('opus', '4.6')
    >>> parse_model_button_name("Sonnet 5 personality comparison") is None
    True
    """
    m = MODEL_BUTTON_RE.match(name.strip())
    if not m:
        return None
    return m.group(1).lower(), m.group(2)


def resolve_mode(
    active_pill: Optional[str],
    url: Optional[str],
    surface: Optional[str] = None,
) -> Optional[str]:
    """Combine sidebar pill, page URL, and composer toggle into a mode.

    * Code pill active -> ``"code"``.
    * Home pill active -> the composer's Chat/Cowork toggle (``surface``)
      wins when present — it is the only signal on un-started sessions,
      whose URL is still ``/new``. Otherwise ``"cowork"`` if the URL path
      starts with ``/cowork``, else ``"chat"`` (the default Home surface).
    * No recognizable pill -> ``None``.
    """
    pill = (active_pill or "").strip().lower()
    if pill == "code":
        return "code"
    if pill == "home":
        if surface in ("chat", "cowork"):
            return surface
        if url:
            try:
                path = urlparse(url).path or "/"
            except ValueError:
                path = "/"
            if path.startswith("/cowork"):
                return "cowork"
        return "chat"
    return None


def _process_image_path(pid: int) -> Optional[str]:
    """Return the full executable path for a PID, or ``None``.

    Uses ``QueryFullProcessImageNameW`` (available since Vista, well within
    the Windows 10 1903 compatibility floor).
    """
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


def _is_claude_process(pid: int) -> bool:
    path = _process_image_path(pid)
    return bool(path) and path.replace("/", "\\").split("\\")[-1].lower() == "claude.exe"


def _aria_properties(ctrl) -> str:
    try:
        return ctrl.GetPropertyValue(auto.PropertyId.AriaPropertiesProperty) or ""
    except Exception:
        return ""


def _rect_overlap_x(a, b) -> int:
    """Horizontal overlap in pixels between two BoundingRectangles."""
    return max(0, min(a.right, b.right) - max(a.left, b.left))


def _hwnd_visible(hwnd: int) -> bool:
    """Whether a window handle refers to a live, *visible* window.

    Claude Desktop's X button hides the main window to the tray: the HWND
    (and its whole accessibility tree) lives on, invisible. Without this
    filter the detector would keep reading the hidden window forever and
    the indicator would never notice the app "closing". Minimized windows
    still count as visible (WS_VISIBLE stays set), which is what we want.
    """
    try:
        user32 = ctypes.windll.user32
        return bool(hwnd) and bool(user32.IsWindow(hwnd)) \
            and bool(user32.IsWindowVisible(hwnd))
    except Exception:
        return False


class Detector:
    """Polls the Claude Desktop accessibility tree for mode/model state.

    Emits state dicts with keys:

    * ``status``: ``"ok"`` (window found), ``"waiting"`` (Claude Desktop
      not running / window not found), ``"error"`` (unexpected failure).
    * ``mode``: ``"chat" | "cowork" | "code" | None``
    * ``family``: ``"opus" | "sonnet" | "haiku" | "fable" | None``
    * ``version``: version string or ``None``

    ``poll()`` never raises; unknown parts come back as ``None`` (the
    indicator renders them as "?"). Use :meth:`run` on a worker thread for
    continuous polling — it handles per-thread COM initialization.
    """

    def __init__(self, interval: float = POLL_INTERVAL):
        self.interval = interval
        self._stop = threading.Event()
        # Cached UIA element references (invalidated on any failure).
        self._win = None
        self._pills = None
        self._model_btn = None
        self._webarea = None
        self._surface_radios: Optional[list] = None
        self._last_mode: Optional[str] = None

    # ------------------------------------------------------------------
    # Window
    # ------------------------------------------------------------------

    def _find_window(self):
        """Locate the Claude Desktop main window, or ``None``."""
        try:
            for w in auto.GetRootControl().GetChildren():
                try:
                    if (
                        w.ClassName == _WINDOW_CLASS
                        and (w.Name or "").startswith("Claude")
                        and _hwnd_visible(w.NativeWindowHandle)
                        and _is_claude_process(w.ProcessId)
                    ):
                        return w
                except Exception:
                    continue
        except Exception:
            logger.exception("enumerating top-level windows failed")
        return None

    def _window_alive(self) -> bool:
        """Cached window still valid — exists AND is visible (not tray-hidden)."""
        if self._win is None:
            return False
        try:
            return _hwnd_visible(self._win.NativeWindowHandle)
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Mode
    # ------------------------------------------------------------------

    def _find_pills(self):
        pills = self._win.GroupControl(searchDepth=25, Name="Mode", ClassName="df-pills")
        return pills if pills.Exists(0.5, 0.1) else None

    def _read_active_pill(self) -> Optional[str]:
        """Name of the active sidebar pill ("Home"/"Code"), or ``None``."""
        if self._pills is None:
            self._pills = self._find_pills()
            if self._pills is None:
                logger.warning("Mode pill group not found (renderer warm-up?)")
                return None
        try:
            children = self._pills.GetChildren()
        except Exception:
            self._pills = None
            return None
        indicator_rect = None
        buttons = []
        for c in children:
            cls = c.ClassName or ""
            if "df-pill-indicator" in cls:
                indicator_rect = c.BoundingRectangle
            elif c.ControlTypeName == "ButtonControl":
                buttons.append(c)
        # Primary signal: aria-current="page" on the active pill.
        for b in buttons:
            if "current=" in _aria_properties(b):
                return b.Name
        # Fallback: the sliding indicator overlaps the active pill.
        if indicator_rect is not None:
            best, best_overlap = None, 0
            for b in buttons:
                try:
                    overlap = _rect_overlap_x(indicator_rect, b.BoundingRectangle)
                except Exception:
                    continue
                if overlap > best_overlap:
                    best, best_overlap = b, overlap
            if best is not None and best_overlap > 0:
                return best.Name
        return None

    def _read_url(self) -> Optional[str]:
        """URL of the main claude.ai document (largest matching RootWebArea)."""
        if self._webarea is not None:
            try:
                url = self._webarea.GetValuePattern().Value
                if url and self._url_is_claude(url):
                    return url
            except Exception:
                pass
            self._webarea = None
        best, best_area = None, 0
        try:
            docs = self._collect_webareas(self._win)
        except Exception:
            return None
        for d in docs:
            try:
                url = d.GetValuePattern().Value
            except Exception:
                continue
            if not url or not self._url_is_claude(url):
                continue
            r = d.BoundingRectangle
            area = max(0, r.right - r.left) * max(0, r.bottom - r.top)
            if area > best_area:
                best, best_area = d, area
        if best is not None:
            self._webarea = best
            try:
                return best.GetValuePattern().Value
            except Exception:
                self._webarea = None
        return None

    def _read_surface_toggle(self) -> Optional[str]:
        """State of the composer's Chat/Cowork toggle, or ``None``.

        The toggle (``GroupControl(Name='Surface')`` with Chat/Cowork radio
        buttons) only exists on un-started Home sessions; returning ``None``
        simply means "no toggle visible, decide by URL".
        """
        if self._surface_radios is not None:
            result = self._checked_radio(self._surface_radios)
            if result is not None:
                return result
            self._surface_radios = None  # stale (navigated away) — re-find
        group = self._win.GroupControl(searchDepth=45, Name="Surface")
        if not group.Exists(0.2, 0.1):
            return None
        try:
            radios = [c for c in group.GetChildren()
                      if c.ControlTypeName == "RadioButtonControl"
                      and (c.Name or "").lower() in ("chat", "cowork")]
        except Exception:
            return None
        if not radios:
            return None
        self._surface_radios = radios
        return self._checked_radio(radios)

    @staticmethod
    def _checked_radio(radios: list) -> Optional[str]:
        """Name (lowercased) of the checked radio button, or ``None``."""
        try:
            for r in radios:
                if "checked=true" in _aria_properties(r):
                    return (r.Name or "").lower() or None
        except Exception:
            pass
        return None

    @staticmethod
    def _url_is_claude(url: str) -> bool:
        try:
            host = urlparse(url).hostname or ""
        except ValueError:
            return False
        return host == "claude.ai" or host.endswith(".claude.ai")

    @staticmethod
    def _url_is_decisive(url: Optional[str]) -> bool:
        """True when the URL alone identifies chat vs cowork.

        Started sessions live under ``/chat/`` or ``/cowork/``; anything
        else (``/new``, warm-up ``None``, other pages) needs the composer
        toggle checked as well.
        """
        if not url:
            return False
        try:
            path = urlparse(url).path or "/"
        except ValueError:
            return False
        return path.startswith("/chat") or path.startswith("/cowork")

    def _collect_webareas(self, ctrl, depth: int = 0) -> list:
        """BFS the native window shell for RootWebArea documents.

        Does not descend into the (large) web content trees: recursion stops
        at each RootWebArea document node.
        """
        found = []
        if depth > 12:
            return found
        for c in ctrl.GetChildren():
            try:
                if (
                    c.ControlTypeName == "DocumentControl"
                    and c.AutomationId == "RootWebArea"
                ):
                    found.append(c)
                    continue  # do not descend into web content
                found.extend(self._collect_webareas(c, depth + 1))
            except Exception:
                continue
        return found

    # ------------------------------------------------------------------
    # Model
    # ------------------------------------------------------------------

    def _find_model_buttons(self) -> list:
        """All buttons matching the anchored model pattern with a menu popup."""
        matches = []

        def walk(c, depth=0):
            if depth > 45 or len(matches) > 4:
                return
            try:
                children = c.GetChildren()
            except Exception:
                return
            for ch in children:
                try:
                    if ch.ControlTypeName == "ButtonControl":
                        name = ch.Name or ""
                        if parse_model_button_name(name) and "haspopup=menu" in _aria_properties(ch):
                            matches.append(ch)
                except Exception:
                    pass
                walk(ch, depth + 1)

        walk(self._win)
        return matches

    def _pick_focused(self, buttons: list):
        """Q3 decision: with several composer buttons, the focused pane wins.

        Approximated as the button nearest to the currently focused control.
        Returns ``None`` when focus gives no usable signal.
        """
        try:
            focused = auto.GetFocusedControl()
            fr = focused.BoundingRectangle
            fx, fy = (fr.left + fr.right) / 2, (fr.top + fr.bottom) / 2
        except Exception:
            return None
        best, best_d = None, None
        for b in buttons:
            try:
                r = b.BoundingRectangle
            except Exception:
                continue
            bx, by = (r.left + r.right) / 2, (r.top + r.bottom) / 2
            d = (bx - fx) ** 2 + (by - fy) ** 2
            if best_d is None or d < best_d:
                best, best_d = b, d
        return best

    def _read_model(self, mode: Optional[str]) -> Optional[tuple[str, str]]:
        # A mode/view switch swaps the composer, so drop the cached button.
        if mode != self._last_mode:
            self._model_btn = None
        if self._model_btn is not None:
            try:
                parsed = parse_model_button_name(self._model_btn.Name or "")
                if parsed:
                    return parsed
            except Exception:
                pass
            self._model_btn = None
        buttons = self._find_model_buttons()
        if not buttons:
            logger.warning("model button not found")
            return None
        btn = buttons[0] if len(buttons) == 1 else self._pick_focused(buttons)
        if btn is None:
            logger.warning("%d model buttons, focus ambiguous", len(buttons))
            return None
        self._model_btn = btn
        return parse_model_button_name(btn.Name or "")

    # ------------------------------------------------------------------
    # Polling
    # ------------------------------------------------------------------

    def poll(self) -> dict:
        """Take one reading. Never raises."""
        try:
            if not self._window_alive():
                self._win = self._find_window()
                self._pills = self._model_btn = self._webarea = None
                self._surface_radios = None
                if self._win is None:
                    return {"status": "waiting", "mode": None,
                            "family": None, "version": None}
            active_pill = self._read_active_pill()
            url, surface = None, None
            if (active_pill or "").lower() == "home":
                url = self._read_url()
                # Only hunt for the composer toggle when the URL cannot
                # decide (un-started sessions sit on /new) — the search is
                # the expensive part of the poll.
                if not self._url_is_decisive(url):
                    surface = self._read_surface_toggle()
            mode = resolve_mode(active_pill, url, surface)
            model = self._read_model(mode)
            self._last_mode = mode
            family, version = model if model else (None, None)
            return {"status": "ok", "mode": mode,
                    "family": family, "version": version}
        except Exception:
            logger.exception("poll failed")
            self._win = None
            self._pills = self._model_btn = self._webarea = None
            self._surface_radios = None
            return {"status": "error", "mode": None,
                    "family": None, "version": None}

    def run(
        self,
        callback: Callable[[dict], None],
        on_poll: Optional[Callable[[dict], None]] = None,
    ) -> None:
        """Poll continuously on the calling thread until :meth:`stop`.

        Intended for a worker thread; initializes COM for that thread.
        ``callback`` fires only when the state actually changes; ``on_poll``
        (if given) fires on every poll tick — used e.g. to time how long
        Claude Desktop has been gone.
        """
        last = None
        with auto.UIAutomationInitializerInThread():
            while not self._stop.is_set():
                state = self.poll()
                if state != last:
                    logger.info("state: %s", state)
                    try:
                        callback(state)
                    except Exception:
                        logger.exception("state callback failed")
                    last = state
                if on_poll is not None:
                    try:
                        on_poll(state)
                    except Exception:
                        logger.exception("poll callback failed")
                self._stop.wait(self.interval)

    def stop(self) -> None:
        """Ask :meth:`run` to exit after the current poll."""
        self._stop.set()
