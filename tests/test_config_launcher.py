"""Tests for config persistence, launcher path logic, and the supervisor.

All run without Claude Desktop; UIA/process/launch calls are mocked.

Run:  python -m unittest discover tests -v
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import config  # noqa: E402
import launcher  # noqa: E402
import main  # noqa: E402


class ConfigRoundTrip(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "config.json"

    def tearDown(self):
        self._tmp.cleanup()

    def test_missing_file_yields_empty(self):
        self.assertEqual(config.load_config(self.path), {})

    def test_round_trip(self):
        data = config.store_window_position({"claude_aumid": "X!Y"}, 12, 34)
        self.assertTrue(config.save_config(data, self.path))
        loaded = config.load_config(self.path)
        self.assertEqual(loaded["window_x"], 12)
        self.assertEqual(loaded["window_y"], 34)
        self.assertEqual(loaded["claude_aumid"], "X!Y")
        self.assertEqual(config.load_window_position(loaded), (12, 34))

    def test_corrupt_file_yields_empty(self):
        self.path.write_text("{not json", encoding="utf-8")
        self.assertEqual(config.load_config(self.path), {})

    def test_non_object_json_yields_empty(self):
        self.path.write_text(json.dumps([1, 2]), encoding="utf-8")
        self.assertEqual(config.load_config(self.path), {})

    def test_position_helpers_reject_junk(self):
        self.assertIsNone(config.load_window_position({}))
        self.assertIsNone(config.load_window_position({"window_x": "a", "window_y": 2}))

    def test_get_config_path_prefers_writable_dir(self):
        p = config.get_config_path(Path(self._tmp.name))
        self.assertEqual(p, Path(self._tmp.name) / config.CONFIG_FILENAME)

    def test_save_failure_returns_false(self):
        bad = Path(self._tmp.name) / "no_such_dir" / "config.json"
        self.assertFalse(config.save_config({"a": 1}, bad))


class LauncherPathLogic(unittest.TestCase):
    def test_desktop_paths_accepted(self):
        self.assertTrue(launcher._is_desktop_path(
            r"C:\Program Files\WindowsApps\Claude_1.2.3_x64__pzs8sxrjxfjjc\app\claude.exe"))
        self.assertTrue(launcher._is_desktop_path(
            r"C:\Users\p\AppData\Local\AnthropicClaude\app-1.2.3\claude.exe"))

    def test_claude_code_cli_rejected(self):
        # Same exe name, different product — must not count as Desktop.
        self.assertFalse(launcher._is_desktop_path(
            r"C:\Users\p\AppData\Roaming\Claude\claude-code\2.1.209\claude.exe"))

    def test_other_exes_rejected(self):
        self.assertFalse(launcher._is_desktop_path(
            r"C:\Program Files\Google\Chrome\Application\chrome.exe"))
        self.assertFalse(launcher._is_desktop_path(""))


class SupervisorAutoClose(unittest.TestCase):
    def _window(self):
        w = mock.Mock()
        return w

    def test_closes_after_grace_when_window_gone(self):
        w = self._window()
        s = main.Supervisor(w)
        s.on_poll({"status": "ok"})
        for _ in range(main.EXIT_GRACE_POLLS):
            s.on_poll({"status": "waiting"})
        w.request_close.assert_called_once()

    def test_close_is_window_based_not_process_based(self):
        # Paul's live-test catch: the Desktop X button hides the window to
        # the tray with the process still alive — the indicator must close
        # anyway, so the supervisor must not consult the process list.
        w = self._window()
        s = main.Supervisor(w)
        s.on_poll({"status": "ok"})
        with mock.patch.object(main.launcher, "claude_desktop_running",
                               return_value=True) as running:
            for _ in range(main.EXIT_GRACE_POLLS):
                s.on_poll({"status": "waiting"})
        w.request_close.assert_called_once()
        running.assert_not_called()

    def test_never_closes_if_never_seen_running(self):
        w = self._window()
        s = main.Supervisor(w)
        for _ in range(main.EXIT_GRACE_POLLS * 3):
            s.on_poll({"status": "waiting"})
        w.request_close.assert_not_called()

    def test_recovery_resets_streak(self):
        w = self._window()
        s = main.Supervisor(w)
        s.on_poll({"status": "ok"})
        for _ in range(main.EXIT_GRACE_POLLS - 1):
            s.on_poll({"status": "waiting"})
        s.on_poll({"status": "ok"})  # window came back
        for _ in range(main.EXIT_GRACE_POLLS - 1):
            s.on_poll({"status": "waiting"})
        w.request_close.assert_not_called()


if __name__ == "__main__":
    unittest.main()
