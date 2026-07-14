"""Unit tests for the indicator window.

These run without Claude Desktop: states are injected through
``update_state`` and applied synchronously with ``_drain_and_redraw``.
A real (briefly visible) Tk window is created per test class — tkinter has
no true headless mode on Windows.

Run:  python -m unittest discover tests -v
"""
import sys
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import colors  # noqa: E402
from indicator import IndicatorWindow  # noqa: E402


def _canvas_texts(window) -> list[str]:
    """All text strings currently drawn on the canvas."""
    c = window.canvas
    return [c.itemcget(i, "text") for i in c.find_all()
            if c.type(i) == "text"]


def _apply(window, state) -> bool:
    """Queue a state and drain it synchronously; returns True if redrawn."""
    window.update_state(state)
    return window._drain_and_redraw()


class IndicatorRendering(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.win = IndicatorWindow()

    @classmethod
    def tearDownClass(cls):
        cls.win.close()

    def test_initial_frame_is_waiting(self):
        # A fresh window (before any state) shows the waiting frame.
        # (Needs its own window: the shared one is mutated by other tests.)
        w = IndicatorWindow()
        try:
            self.assertEqual(w.canvas["background"], colors.WAITING_COLOR)
        finally:
            w.close()

    def test_ok_state_full(self):
        redrawn = _apply(self.win, {"status": "ok", "mode": "code",
                                    "family": "fable", "version": "5"})
        self.assertTrue(redrawn)
        self.assertEqual(self.win.canvas["background"], colors.MODE_COLORS["code"])
        texts = _canvas_texts(self.win)
        self.assertIn("5", texts)
        self.assertIn("Code · Fable 5", texts)
        polygons = [i for i in self.win.canvas.find_all()
                    if self.win.canvas.type(i) == "polygon"]
        self.assertEqual(len(polygons), 1)  # the pentagon

    def test_duplicate_state_not_redrawn(self):
        state = {"status": "ok", "mode": "chat", "family": "opus", "version": "4.8"}
        self.assertTrue(_apply(self.win, state))
        self.assertFalse(_apply(self.win, dict(state)))

    def test_all_mode_backgrounds(self):
        for mode, color in colors.MODE_COLORS.items():
            _apply(self.win, {"status": "ok", "mode": mode,
                              "family": "opus", "version": "4.8"})
            self.assertEqual(self.win.canvas["background"], color)

    def test_unknown_family_square_and_question_mark(self):
        _apply(self.win, {"status": "ok", "mode": "code",
                          "family": None, "version": None})
        texts = _canvas_texts(self.win)
        self.assertIn("?", texts)
        self.assertIn("Code · ?", texts)
        rects = [i for i in self.win.canvas.find_all()
                 if self.win.canvas.type(i) == "rectangle"]
        self.assertEqual(len(rects), 1)  # the unknown-family square

    def test_unknown_mode_background(self):
        _apply(self.win, {"status": "ok", "mode": None,
                          "family": "haiku", "version": "4.5"})
        self.assertEqual(self.win.canvas["background"], colors.UNKNOWN_MODE_COLOR)
        self.assertIn("? · Haiku 4.5", _canvas_texts(self.win))

    def test_waiting_state(self):
        _apply(self.win, {"status": "waiting", "mode": None,
                          "family": None, "version": None})
        self.assertEqual(self.win.canvas["background"], colors.WAITING_COLOR)
        self.assertTrue(any("Waiting" in t for t in _canvas_texts(self.win)))

    def test_error_state(self):
        _apply(self.win, {"status": "error", "mode": None,
                          "family": None, "version": None})
        self.assertEqual(self.win.canvas["background"], colors.UNKNOWN_MODE_COLOR)
        self.assertIn("detection error", _canvas_texts(self.win))

    def test_bogus_status_treated_as_error(self):
        _apply(self.win, {"status": "bogus", "mode": "code",
                          "family": "fable", "version": "5"})
        self.assertIn("detection error", _canvas_texts(self.win))

    def test_only_latest_queued_state_wins(self):
        self.win.update_state({"status": "ok", "mode": "chat",
                               "family": "opus", "version": "4.8"})
        self.win.update_state({"status": "ok", "mode": "cowork",
                               "family": "sonnet", "version": "5"})
        self.win._drain_and_redraw()
        self.assertEqual(self.win.canvas["background"], colors.MODE_COLORS["cowork"])
        self.assertIn("Cowork · Sonnet 5", _canvas_texts(self.win))

    def test_window_geometry_and_style(self):
        self.win.root.update_idletasks()
        self.assertEqual(self.win.root.winfo_width(), 240)
        self.assertEqual(self.win.root.winfo_height(), 240)
        self.assertTrue(self.win.root.overrideredirect())


class IndicatorPosition(unittest.TestCase):
    """Multi-monitor position restore (Paul's live-test catch: saved
    positions on a secondary monitor must not be clamped to the primary)."""

    def test_position_on_connected_monitor_restored_exactly(self):
        import indicator as ind
        with unittest.mock.patch.object(ind, "_rect_on_any_monitor", return_value=True):
            w = IndicatorWindow(position=(-500, 300))  # e.g. monitor left of primary
        try:
            w.root.update_idletasks()
            self.assertEqual((w.root.winfo_x(), w.root.winfo_y()), (-500, 300))
        finally:
            w.close()

    def test_position_on_disconnected_monitor_falls_back_to_default(self):
        import indicator as ind
        with unittest.mock.patch.object(ind, "_rect_on_any_monitor", return_value=False):
            w = IndicatorWindow(position=(-5000, 300))
        try:
            w.root.update_idletasks()
            screen_w = w.root.winfo_screenwidth()
            self.assertEqual((w.root.winfo_x(), w.root.winfo_y()),
                             (screen_w - 240 - 40, 60))
        finally:
            w.close()

    def test_real_monitor_check_accepts_primary_and_rejects_far_space(self):
        import indicator as ind
        # (0,0) is always on the primary monitor; 100k px away is nowhere.
        self.assertTrue(ind._rect_on_any_monitor(0, 0, 240, 240))
        self.assertFalse(ind._rect_on_any_monitor(100000, 100000, 240, 240))


class IndicatorLifecycle(unittest.TestCase):
    def test_close_is_idempotent(self):
        w = IndicatorWindow()
        w.close()
        w.close()  # second close must not raise

    def test_update_after_close_does_not_raise(self):
        w = IndicatorWindow()
        w.close()
        w.update_state({"status": "ok", "mode": "code",
                        "family": "fable", "version": "5"})


if __name__ == "__main__":
    unittest.main()
