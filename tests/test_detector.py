"""Unit tests for the detection logic that runs without Claude Desktop.

The UIA-dependent code paths are exercised manually (Phase 1 step 4); these
tests cover the pure parsing/decision functions, including the hazard cases
observed in the Phase 0 accessibility dumps.

Run:  python -m unittest discover tests -v
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from detector import (  # noqa: E402
    Detector,
    _hwnd_visible,
    _rect_overlap_x,
    parse_model_button_name,
    resolve_mode,
)


class ParseModelButtonName(unittest.TestCase):
    """Labels observed in the Phase 0 flip test, plus hazards."""

    def test_code_composer_plain(self):
        self.assertEqual(parse_model_button_name("Fable 5"), ("fable", "5"))

    def test_home_chat_with_prefix_and_effort(self):
        self.assertEqual(parse_model_button_name("Model: Opus 4.6 High"), ("opus", "4.6"))
        self.assertEqual(parse_model_button_name("Model: Sonnet 5 Medium"), ("sonnet", "5"))

    def test_cowork_prefix_no_effort(self):
        self.assertEqual(parse_model_button_name("Model: Opus 4.8"), ("opus", "4.8"))

    def test_all_families(self):
        self.assertEqual(parse_model_button_name("Opus 4.8"), ("opus", "4.8"))
        self.assertEqual(parse_model_button_name("Sonnet 5"), ("sonnet", "5"))
        self.assertEqual(parse_model_button_name("Haiku 4.5"), ("haiku", "4.5"))
        self.assertEqual(parse_model_button_name("Fable 5"), ("fable", "5"))

    def test_case_insensitive_and_whitespace(self):
        self.assertEqual(parse_model_button_name("  fable 5  "), ("fable", "5"))
        self.assertEqual(parse_model_button_name("MODEL: OPUS 4.8"), ("opus", "4.8"))

    def test_rejects_session_titles_containing_model_names(self):
        # Real sidebar buttons observed in the Phase 0 dumps.
        self.assertIsNone(parse_model_button_name("Sonnet 5 personality comparison"))
        self.assertIsNone(parse_model_button_name("Sonnet 5 knowledge cutoff differences"))
        self.assertIsNone(
            parse_model_button_name("Mark as unread Sonnet 5 personality comparison"))
        self.assertIsNone(
            parse_model_button_name("More options for Sonnet 5 personality comparison"))

    def test_rejects_menu_item_labels(self):
        # Dropdown radio items carry a description after the version.
        self.assertIsNone(
            parse_model_button_name("Sonnet 5 Most efficient for everyday tasks"))
        self.assertIsNone(
            parse_model_button_name("Fable 5 Included until July 19 For your toughest challenges"))

    def test_rejects_family_without_version(self):
        self.assertIsNone(parse_model_button_name("Opus"))
        self.assertIsNone(parse_model_button_name("Model: Fable"))

    def test_rejects_noise(self):
        self.assertIsNone(parse_model_button_name(""))
        self.assertIsNone(parse_model_button_name("Effort: High"))
        self.assertIsNone(parse_model_button_name("Usage: context 53.9k, plan 51%"))

    def test_version_variants(self):
        self.assertEqual(parse_model_button_name("Opus 4.10"), ("opus", "4.10"))
        self.assertEqual(parse_model_button_name("Haiku 6"), ("haiku", "6"))


class ResolveMode(unittest.TestCase):
    """Pill + URL combinations from the Phase 0 flip test."""

    def test_code_pill_wins_regardless_of_url(self):
        self.assertEqual(resolve_mode("Code", None), "code")
        self.assertEqual(
            resolve_mode("Code", "https://claude.ai/epitaxy/local_x"), "code")

    def test_home_cowork_url(self):
        self.assertEqual(
            resolve_mode("Home", "https://claude.ai/cowork/local_71de0ea9"), "cowork")

    def test_home_chat_urls(self):
        self.assertEqual(resolve_mode("Home", "https://claude.ai/new"), "chat")
        self.assertEqual(resolve_mode("Home", "https://claude.ai/chat/abc-123"), "chat")

    def test_home_without_url_defaults_to_chat(self):
        self.assertEqual(resolve_mode("Home", None), "chat")

    def test_case_insensitive_pills(self):
        self.assertEqual(resolve_mode("code", None), "code")
        self.assertEqual(resolve_mode("HOME", "https://claude.ai/cowork/x"), "cowork")

    def test_unknown_pill(self):
        self.assertIsNone(resolve_mode(None, None))
        self.assertIsNone(resolve_mode("", "https://claude.ai/new"))
        self.assertIsNone(resolve_mode("Settings", None))

    def test_malformed_url_defaults_to_chat(self):
        self.assertEqual(resolve_mode("Home", "not a url"), "chat")

    def test_surface_toggle_decides_unstarted_sessions(self):
        # Paul's live-test catch: a new session toggled to Cowork still has
        # URL /new — the composer's Surface radio group is the only signal.
        self.assertEqual(
            resolve_mode("Home", "https://claude.ai/new", "cowork"), "cowork")
        self.assertEqual(
            resolve_mode("Home", "https://claude.ai/new", "chat"), "chat")
        self.assertEqual(resolve_mode("Home", None, "cowork"), "cowork")

    def test_surface_toggle_beats_url(self):
        # Present toggle is fresher than any (stale) URL reading.
        self.assertEqual(
            resolve_mode("Home", "https://claude.ai/cowork/x", "chat"), "chat")

    def test_surface_ignored_for_code_pill(self):
        self.assertEqual(resolve_mode("Code", None, "cowork"), "code")

    def test_bogus_surface_falls_back_to_url(self):
        self.assertEqual(
            resolve_mode("Home", "https://claude.ai/cowork/x", "banana"), "cowork")


class UrlIsDecisive(unittest.TestCase):
    def test_started_session_urls_are_decisive(self):
        self.assertTrue(Detector._url_is_decisive("https://claude.ai/chat/abc"))
        self.assertTrue(Detector._url_is_decisive("https://claude.ai/cowork/local_x"))

    def test_new_and_unknown_are_not(self):
        self.assertFalse(Detector._url_is_decisive("https://claude.ai/new"))
        self.assertFalse(Detector._url_is_decisive("https://claude.ai/recents"))
        self.assertFalse(Detector._url_is_decisive(None))
        self.assertFalse(Detector._url_is_decisive(""))


class UrlIsClaude(unittest.TestCase):
    def test_accepts_claude_ai(self):
        self.assertTrue(Detector._url_is_claude("https://claude.ai/new"))
        self.assertTrue(Detector._url_is_claude("https://www.claude.ai/chat/x"))

    def test_rejects_other_hosts(self):
        # e.g. a Browser-pane preview of an external site inside a session.
        self.assertFalse(Detector._url_is_claude("https://example.com/claude.ai"))
        self.assertFalse(Detector._url_is_claude("https://notclaude.ai/new"))
        self.assertFalse(Detector._url_is_claude(""))
        self.assertFalse(Detector._url_is_claude("about:blank"))


class HwndVisible(unittest.TestCase):
    """Regression for the tray-hide bug: Claude Desktop's X button leaves a
    live but *hidden* HWND whose accessibility tree is still readable — the
    detector must treat such a window as gone."""

    def test_invalid_handle(self):
        self.assertFalse(_hwnd_visible(0))

    def test_hidden_window_is_not_visible(self):
        import ctypes
        import tkinter as tk

        root = tk.Tk()
        try:
            root.update()
            hwnd = ctypes.windll.user32.GetAncestor(root.winfo_id(), 2)  # GA_ROOT
            self.assertTrue(_hwnd_visible(hwnd))
            root.withdraw()  # hide like minimize-to-tray: HWND lives on
            root.update()
            self.assertFalse(_hwnd_visible(hwnd))
        finally:
            root.destroy()


class _Rect:
    def __init__(self, left, top, right, bottom):
        self.left, self.top, self.right, self.bottom = left, top, right, bottom


class RectOverlap(unittest.TestCase):
    def test_overlap_matches_phase0_geometry(self):
        # Indicator (220..420) over Code pill (219..419), Home pill (17..220).
        indicator = _Rect(220, 61, 420, 87)
        code = _Rect(219, 61, 419, 87)
        home = _Rect(17, 61, 220, 87)
        self.assertGreater(_rect_overlap_x(indicator, code),
                           _rect_overlap_x(indicator, home))

    def test_disjoint(self):
        self.assertEqual(_rect_overlap_x(_Rect(0, 0, 10, 10), _Rect(20, 0, 30, 10)), 0)


if __name__ == "__main__":
    unittest.main()
