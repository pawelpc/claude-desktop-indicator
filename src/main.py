"""Claude Desktop indicator — entry point.

Wires the pieces together:

* :mod:`launcher` — starts Claude Desktop if it is not already running.
* :mod:`detector` — polls the accessibility tree on a worker thread.
* :mod:`indicator` — the always-on-top status window (Tk main thread).
* :mod:`config` — remembers the window position between sessions.

The indicator closes itself when Claude Desktop exits (after a short grace
period so a transient window drop is not mistaken for an exit). Quitting
the indicator never closes Claude Desktop.

Usage:
    python main.py [--debug] [--no-launch]
"""
from __future__ import annotations

import argparse
import ctypes
import logging
import threading

try:
    from src import config, launcher
    from src.detector import Detector
    from src.indicator import IndicatorWindow
except ImportError:  # running directly from the src directory
    import config
    import launcher
    from detector import Detector
    from indicator import IndicatorWindow

logger = logging.getLogger("desktop_wrapper.main")

#: Consecutive "waiting" states (one per detector poll, ~0.5 s each) after
#: Claude Desktop was last seen before the indicator closes itself.
EXIT_GRACE_POLLS = 6


class Supervisor:
    """Routes detector states to the window and closes it on Desktop exit.

    Runs on the detector thread; only thread-safe window methods are used.
    """

    def __init__(self, window: IndicatorWindow):
        self._window = window
        self._seen_running = False
        self._waiting_streak = 0

    def on_state(self, state: dict) -> None:
        """Detector change callback: forward the new state to the window."""
        self._window.update_state(state)

    def on_poll(self, state: dict) -> None:
        """Every-tick callback: time the exit grace period.

        The decision is window-based on purpose: Claude Desktop's X button
        hides the window to the tray while the process keeps running, and
        from the user's point of view the app is closed — so the indicator
        goes away too. (Relaunching the indicator re-activates the app.)
        """
        if state["status"] == "ok":
            self._seen_running = True
            self._waiting_streak = 0
        elif state["status"] == "waiting" and self._seen_running:
            self._waiting_streak += 1
            if self._waiting_streak == EXIT_GRACE_POLLS:
                logger.info("Claude Desktop window gone; closing indicator")
                self._window.request_close()


def _set_dpi_awareness() -> None:
    """Make the process system-DPI aware so the window renders crisply.

    ``SetProcessDpiAwareness`` exists since Windows 8.1 — safely within the
    Windows 10 1903 floor. Falls back to the Vista-era API, then to doing
    nothing (Windows just bitmap-scales the window).
    """
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # PROCESS_SYSTEM_DPI_AWARE
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            logger.warning("could not set DPI awareness")


def main() -> None:
    """Run the indicator until the user quits it or Claude Desktop exits."""
    parser = argparse.ArgumentParser(description="Claude Desktop mode/model indicator")
    parser.add_argument("--debug", action="store_true", help="verbose logging")
    parser.add_argument("--no-launch", action="store_true",
                        help="do not start Claude Desktop if it is not running")
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    # comtypes logs every COM pointer release at DEBUG — far too noisy.
    logging.getLogger("comtypes").setLevel(logging.WARNING)

    _set_dpi_awareness()

    cfg_path = config.get_config_path()
    cfg = config.load_config(cfg_path)

    # Window-based check: a tray-hidden Claude Desktop (X button) has a
    # live process but no window — activating it brings the window back.
    if not args.no_launch and not launcher.claude_desktop_window_present():
        logger.info("no Claude Desktop window; launching/activating")
        aumid = launcher.launch_claude_desktop(cfg.get("claude_aumid"))
        if aumid and aumid != cfg.get("claude_aumid"):
            cfg["claude_aumid"] = aumid
            config.save_config(cfg, cfg_path)

    def save_position(x: int, y: int) -> None:
        config.save_config(config.store_window_position(cfg, x, y), cfg_path)

    detector = Detector()
    window = IndicatorWindow(
        on_quit=detector.stop,
        position=config.load_window_position(cfg),
        on_move=save_position,
    )
    supervisor = Supervisor(window)
    worker = threading.Thread(
        target=detector.run,
        args=(supervisor.on_state,),
        kwargs={"on_poll": supervisor.on_poll},
        name="detector", daemon=True,
    )
    worker.start()
    try:
        window.run()
    finally:
        detector.stop()
        worker.join(timeout=2)
        logger.info("shut down cleanly")


if __name__ == "__main__":
    main()
