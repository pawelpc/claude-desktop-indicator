"""Display component for the Claude Desktop status indicator.

Renders a small (240x240 px), borderless, always-on-top window that shows
the current state of Claude Desktop at a glance:

    * the window background color encodes the *mode* (chat / cowork / code)
    * a shape drawn in the center encodes the *model family*
      (opus / sonnet / haiku / fable)
    * text inside that shape shows the *model version*

The visual encoding itself (colors and shapes) lives in :mod:`colors` and
must not be duplicated here.

This module is display-only: it exposes :class:`IndicatorWindow`, whose
``update_state`` method is safe to call from a worker thread. State updates
are handed off through a :class:`queue.Queue` and drained on the Tk main
thread via periodic ``root.after`` polling, which is the only
thread-safe way to touch Tkinter widgets.
"""

from __future__ import annotations

import ctypes
import math
import queue
import threading
import tkinter as tk
from typing import Any, Callable, Optional

try:
    from src.colors import (
        FAMILY_SHAPES,
        MODE_COLORS,
        TEXT_COLOR,
        UNKNOWN_FAMILY_SHAPE,
        UNKNOWN_MODE_COLOR,
        WAITING_COLOR,
    )
except ImportError:  # running directly from the src/ directory
    from colors import (
        FAMILY_SHAPES,
        MODE_COLORS,
        TEXT_COLOR,
        UNKNOWN_FAMILY_SHAPE,
        UNKNOWN_MODE_COLOR,
        WAITING_COLOR,
    )

# --- Layout constants -------------------------------------------------------

WINDOW_SIZE: int = 240
"""Width and height of the indicator window, in pixels."""

_EDGE_MARGIN_X: int = 40
_TOP_MARGIN_Y: int = 60

_CENTER_X: int = 120
_SHAPE_CENTER_Y: int = 104
_SHAPE_RADIUS: int = 68
_UNKNOWN_SQUARE_SIDE: int = 110
_TRIANGLE_TEXT_Y_SHIFT: int = 14
_SUMMARY_Y: int = 212

_POLL_INTERVAL_MS: int = 100

_VALID_STATUSES = frozenset({"ok", "waiting", "error"})


def _rect_on_any_monitor(x: int, y: int, w: int, h: int) -> bool:
    """Whether the rectangle intersects any connected monitor.

    Uses ``MonitorFromRect`` so saved positions on secondary monitors are
    honored, while positions on a since-disconnected monitor are rejected.
    Returns True on any failure — better to restore an odd position than to
    discard a valid one.
    """

    class _RECT(ctypes.Structure):
        _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                    ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

    try:
        rect = _RECT(x, y, x + w, y + h)
        monitor_default_to_null = 0
        return bool(ctypes.windll.user32.MonitorFromRect(
            ctypes.byref(rect), monitor_default_to_null))
    except Exception:
        return True


def _regular_polygon_points(
    cx: float, cy: float, radius: float, sides: int, point_up: bool = True
) -> list[float]:
    """Compute the flattened vertex list of a regular polygon.

    Args:
        cx: X coordinate of the polygon's center.
        cy: Y coordinate of the polygon's center.
        radius: Distance from center to each vertex.
        sides: Number of vertices/sides.
        point_up: If True, the first vertex points straight up (canvas
            coordinates, where +y is down).

    Returns:
        A flat list ``[x0, y0, x1, y1, ...]`` suitable for
        ``Canvas.create_polygon``.
    """
    start_angle = -90.0 if point_up else 0.0
    step = 360.0 / sides
    points: list[float] = []
    for i in range(sides):
        angle = math.radians(start_angle + i * step)
        points.append(cx + radius * math.cos(angle))
        points.append(cy + radius * math.sin(angle))
    return points


class IndicatorWindow:
    """A 240x240 always-on-top window that displays Claude Desktop's state.

    The window is chromeless (no title bar), stays on top of other windows,
    can be dragged with the left mouse button, and offers a "Quit" action
    on right-click.

    All drawing happens on the Tk main thread. :meth:`update_state` is the
    only method meant to be called from other threads; it merely enqueues
    the new state for the main thread to pick up.
    """

    def __init__(
        self,
        on_quit: Optional[Callable[[], None]] = None,
        position: Optional[tuple[int, int]] = None,
        on_move: Optional[Callable[[int, int], None]] = None,
    ) -> None:
        """Create and configure the indicator window (does not show a mainloop).

        Args:
            on_quit: Optional callback invoked after the window has been
                closed via the right-click "Quit" menu item.
            position: Initial ``(x, y)`` for the window's top-left corner,
                e.g. restored from config. Honored on any connected monitor;
                ignored (default corner) if its monitor is disconnected.
                ``None`` uses the default spot near the top-right corner.
            on_move: Optional callback invoked with the new ``(x, y)`` when
                the user finishes dragging the window (for persistence).
        """
        self._on_quit = on_quit
        self._on_move = on_move
        self._queue: "queue.Queue[dict[str, Any]]" = queue.Queue()
        self._last_drawn: Optional[tuple[str, Optional[str], Optional[str], Optional[str]]] = None
        self._drag_offset: tuple[int, int] = (0, 0)
        self._closed = False
        self._close_requested = threading.Event()

        self.root = tk.Tk()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.resizable(False, False)

        screen_w = self.root.winfo_screenwidth()
        if position is not None and _rect_on_any_monitor(
                int(position[0]), int(position[1]), WINDOW_SIZE, WINDOW_SIZE):
            # Saved position still lands on a connected monitor (primary or
            # secondary) — restore it exactly.
            x, y = int(position[0]), int(position[1])
        else:
            # No saved position, or its monitor is gone: default corner.
            x = screen_w - WINDOW_SIZE - _EDGE_MARGIN_X
            y = _TOP_MARGIN_Y
        self.root.geometry(f"{WINDOW_SIZE}x{WINDOW_SIZE}+{x}+{y}")

        self.canvas = tk.Canvas(
            self.root,
            width=WINDOW_SIZE,
            height=WINDOW_SIZE,
            highlightthickness=0,
            bg=WAITING_COLOR,
        )
        self.canvas.pack(fill="both", expand=True)

        self._bind_events()

        # Paint an initial "waiting" frame so the window is never blank.
        self._redraw("waiting", None, None, None)
        self._last_drawn = ("waiting", None, None, None)

        self.root.after(_POLL_INTERVAL_MS, self._poll_queue)

    # -- public API -----------------------------------------------------

    def update_state(self, state: dict[str, Any]) -> None:
        """Queue a new state to be rendered on the Tk main thread.

        Thread-safe: this may be called from a worker thread. It only
        pushes onto an internal :class:`queue.Queue`; no Tk calls happen
        here.

        Args:
            state: A dict with keys ``status`` (``"ok"`` | ``"waiting"`` |
                ``"error"``), ``mode`` (``"chat"`` | ``"cowork"`` |
                ``"code"`` | ``None``), ``family`` (``"opus"`` | ``"sonnet"``
                | ``"haiku"`` | ``"fable"`` | ``None``), and ``version``
                (a version string or ``None``).
        """
        self._queue.put(dict(state))

    def run(self) -> None:
        """Enter the Tk mainloop. Blocks until the window is closed."""
        self.root.mainloop()

    def close(self) -> None:
        """Destroy the window. Safe to call from the Tk thread, idempotent."""
        if self._closed:
            return
        self._closed = True
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def request_close(self) -> None:
        """Ask the Tk thread to close the window. Thread-safe.

        Used by the supervisor when Claude Desktop exits: the actual
        ``close()`` happens on the next queue-poll tick (≤100 ms later).
        """
        self._close_requested.set()

    # -- event wiring -----------------------------------------------------

    def _bind_events(self) -> None:
        """Wire up dragging and the right-click context menu."""
        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_motion)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<Button-3>", self._on_right_click)

    def _on_press(self, event: "tk.Event[Any]") -> None:
        """Record the click offset within the window at drag start."""
        self._drag_offset = (event.x, event.y)

    def _on_motion(self, event: "tk.Event[Any]") -> None:
        """Move the window to follow the mouse while dragging."""
        offset_x, offset_y = self._drag_offset
        new_x = self.root.winfo_x() + (event.x - offset_x)
        new_y = self.root.winfo_y() + (event.y - offset_y)
        self.root.geometry(f"+{new_x}+{new_y}")

    def _on_release(self, event: "tk.Event[Any]") -> None:
        """Report the final window position after a drag (for persistence)."""
        if self._on_move is not None:
            self._on_move(self.root.winfo_x(), self.root.winfo_y())

    def _on_right_click(self, event: "tk.Event[Any]") -> None:
        """Show a context menu with a single "Quit" action."""
        menu = tk.Menu(self.root, tearoff=0)
        menu.add_command(label="Quit", command=self._handle_quit)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _handle_quit(self) -> None:
        """Close the window and notify the owner via the ``on_quit`` callback."""
        self.close()
        if self._on_quit is not None:
            self._on_quit()

    # -- queue draining / redraw scheduling --------------------------------

    def _poll_queue(self) -> None:
        """Periodic callback (via ``root.after``) that drains the state queue."""
        if self._closed:
            return
        if self._close_requested.is_set():
            self.close()
            return
        self._drain_and_redraw()
        self.root.after(_POLL_INTERVAL_MS, self._poll_queue)

    def _drain_and_redraw(self) -> bool:
        """Consume all pending states and redraw once if the state changed.

        Only the most recently queued state matters, so every pending item
        is drained and only the last one is applied. This is also the
        method exercised directly by tests, since it performs one
        synchronous drain-and-redraw cycle without needing the mainloop.

        Returns:
            True if a redraw was performed, False if there was nothing new
            to draw or the effective state was unchanged.
        """
        latest: Optional[dict[str, Any]] = None
        try:
            while True:
                latest = self._queue.get_nowait()
        except queue.Empty:
            pass

        if latest is None:
            return False

        status = latest.get("status")
        if status not in _VALID_STATUSES:
            status = "error"
        mode = latest.get("mode")
        family = latest.get("family")
        version = latest.get("version")

        key = (status, mode, family, version)
        if key == self._last_drawn:
            return False

        self._last_drawn = key
        self._redraw(status, mode, family, version)
        return True

    # -- rendering ----------------------------------------------------------

    def _redraw(
        self,
        status: str,
        mode: Optional[str],
        family: Optional[str],
        version: Optional[str],
    ) -> None:
        """Clear the canvas and render the given (already-normalized) state."""
        self.canvas.delete("all")
        if status == "waiting":
            self._draw_waiting()
        elif status == "error":
            self._draw_error()
        else:  # "ok"
            self._draw_ok(mode, family, version)

    def _draw_waiting(self) -> None:
        """Render the 'waiting for Claude Desktop' frame."""
        self.canvas.configure(bg=WAITING_COLOR)
        self.canvas.create_text(
            _CENTER_X,
            WINDOW_SIZE // 2,
            text="Waiting for\nClaude Desktop…",
            fill=TEXT_COLOR,
            font=("Segoe UI", 16),
            justify="center",
        )

    def _draw_error(self) -> None:
        """Render the detection-error frame: unknown shape + 'detection error'."""
        self.canvas.configure(bg=UNKNOWN_MODE_COLOR)
        self._draw_family_shape(None, None)
        self._draw_summary("detection error")

    def _draw_ok(
        self, mode: Optional[str], family: Optional[str], version: Optional[str]
    ) -> None:
        """Render a normal 'ok' frame: mode background, family shape, summary."""
        bg = MODE_COLORS.get(mode, UNKNOWN_MODE_COLOR) if mode else UNKNOWN_MODE_COLOR
        self.canvas.configure(bg=bg)
        self._draw_family_shape(family, version)
        self._draw_summary(self._format_summary(mode, family, version))

    def _draw_family_shape(self, family: Optional[str], version: Optional[str]) -> None:
        """Draw the center shape for ``family`` and the version text inside it."""
        if family is not None and family in FAMILY_SHAPES:
            shape_name, fill = FAMILY_SHAPES[family]
        else:
            shape_name, fill = UNKNOWN_FAMILY_SHAPE

        cx, cy, r = _CENTER_X, _SHAPE_CENTER_Y, _SHAPE_RADIUS

        if shape_name == "circle":
            self.canvas.create_oval(cx - r, cy - r, cx + r, cy + r, fill=fill, outline=fill)
        elif shape_name == "diamond":
            points = [cx, cy - r, cx + r, cy, cx, cy + r, cx - r, cy]
            self.canvas.create_polygon(points, fill=fill, outline=fill)
        elif shape_name == "triangle":
            points = _regular_polygon_points(cx, cy, r, 3)
            self.canvas.create_polygon(points, fill=fill, outline=fill)
        elif shape_name == "pentagon":
            points = _regular_polygon_points(cx, cy, r, 5)
            self.canvas.create_polygon(points, fill=fill, outline=fill)
        else:  # "square" - unknown family
            half = _UNKNOWN_SQUARE_SIDE / 2
            self.canvas.create_rectangle(
                cx - half, cy - half, cx + half, cy + half, fill=fill, outline=fill
            )

        text = version if version else "?"
        text_y = cy + _TRIANGLE_TEXT_Y_SHIFT if shape_name == "triangle" else cy
        font_size = 44 if len(text) <= 3 else 32
        self.canvas.create_text(
            cx, text_y, text=text, fill=TEXT_COLOR, font=("Segoe UI", font_size, "bold")
        )

    def _draw_summary(self, text: str) -> None:
        """Draw the small summary label near the bottom of the window."""
        self.canvas.create_text(
            _CENTER_X, _SUMMARY_Y, text=text, fill=TEXT_COLOR, font=("Segoe UI", 13)
        )

    @staticmethod
    def _format_summary(
        mode: Optional[str], family: Optional[str], version: Optional[str]
    ) -> str:
        """Build the "{Mode} · {Family} {version}" summary string.

        Unknown parts are substituted with ``"?"``. When the family itself
        is unknown there is no meaningful version to pair it with, so the
        whole "family version" segment collapses to a single ``"?"``
        (matching the spec's example ``"Code · ?"``).
        """
        mode_str = mode.capitalize() if mode else "?"
        if family:
            family_str = family.capitalize()
            version_str = version if version else "?"
            family_version = f"{family_str} {version_str}"
        else:
            family_version = "?"
        return f"{mode_str} · {family_version}"


if __name__ == "__main__":
    # Self-test / demo: cycle through every representative state forever,
    # driven entirely by root.after scheduling (never time.sleep), so the
    # Tk event loop keeps handling drag/right-click while the demo runs.
    _FAMILY_VERSIONS: dict[str, str] = {
        "opus": "4.8",
        "sonnet": "5",
        "haiku": "4.5",
        "fable": "5",
    }
    _MODES: list[str] = ["chat", "cowork", "code"]

    demo_states: list[dict[str, Any]] = []
    for demo_mode in _MODES:
        for demo_family, demo_version in _FAMILY_VERSIONS.items():
            demo_states.append(
                {
                    "status": "ok",
                    "mode": demo_mode,
                    "family": demo_family,
                    "version": demo_version,
                }
            )
    demo_states.append({"status": "ok", "mode": "code", "family": None, "version": None})
    demo_states.append({"status": "ok", "mode": None, "family": "opus", "version": "4.8"})
    demo_states.append({"status": "waiting", "mode": None, "family": None, "version": None})
    demo_states.append({"status": "error", "mode": None, "family": None, "version": None})

    window = IndicatorWindow()

    def _cycle(index: int = 0) -> None:
        """Advance the demo to the next state and reschedule itself."""
        state = demo_states[index % len(demo_states)]
        print(f"[demo] {state}")
        window.update_state(state)
        window.root.after(1200, _cycle, index + 1)

    window.root.after(0, _cycle)
    window.run()
