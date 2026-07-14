# Claude Desktop Indicator

A small (240x240 px), always-on-top Windows indicator that shows at a glance
which mode (Chat / Cowork / Code) and which model (family + version) Claude
Desktop is currently set to. The window background color encodes the mode, a
centered shape encodes the model family, the version number is drawn inside
that shape, and a text summary sits below it — so the current state is
readable both peripherally (color/shape) and explicitly (text).

Detection is pure read-only Windows UI Automation — the same public
accessibility API screen readers use. No code injection, no network
interception, no DevTools protocol. See [How it works](#how-it-works) and
[Privacy & Anthropic ToS](#privacy--anthropic-tos) below.

## Visual encoding

**Background color -> mode**

| Mode   | Color      | Hex       |
|--------|-----------|-----------|
| Chat   | Deep blue | `#1E40AF` |
| Cowork | Violet    | `#7C3AED` |
| Code   | Teal      | `#0E7490` |

**Center shape + fill color -> model family**

| Family | Shape    | Color  | Hex       |
|--------|----------|--------|-----------|
| Opus   | Circle   | Red    | `#DC2626` |
| Sonnet | Diamond  | Orange | `#EA580C` |
| Haiku  | Triangle | Green  | `#16A34A` |
| Fable  | Pentagon | Gold   | `#CA8A04` |

The model's version number (e.g. "4.8", "5") is drawn inside the shape. A
gray square with "?" means that particular piece of state couldn't be read.
Mode and family colors are drawn from opposite sides of the color wheel
(cool backgrounds, warm shapes) so the two channels never collide.

## Requirements

- Windows 10, version 1903 or later
- Python 3.10+ (only if running from source — not needed for the prebuilt exe)
- [Claude Desktop](https://claude.ai/download) installed (Microsoft Store or
  direct-download build)

## Install & run from source

```
pip install uiautomation
python src/main.py
```

(or `pip install -r requirements.txt`)

Flags:

- `--debug` — verbose logging
- `--no-launch` — don't start Claude Desktop if it isn't already running

## Building a standalone exe

```
pip install pyinstaller
pyinstaller --onefile --noconsole --name ClaudeIndicator --icon assets/icon.ico src/main.py
```

The built exe is written to `dist/ClaudeIndicator.exe`.

## Creating a desktop shortcut

Point a `.lnk` file at the built exe with the custom icon:

```powershell
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut("$env:USERPROFILE\Desktop\Claude Indicator.lnk")
$shortcut.TargetPath = "C:\path\to\dist\ClaudeIndicator.exe"
$shortcut.IconLocation = "C:\path\to\assets\icon.ico"
$shortcut.Save()
```

Double-clicking the shortcut starts Claude Desktop (if it isn't already
running) and then shows the indicator.

## How it works

A background thread polls the Claude Desktop window's UI Automation tree
every 500 ms and reports state changes to the indicator window, which redraws
on the Tk main thread. Specifically, it reads:

- The sidebar's Home/Code pill group, using the `aria-current` accessibility
  property to find the active pill (falls back to the sliding pill-indicator
  rectangle if that property is unavailable).
- On an un-started session (a fresh `/new` page), the composer's Chat/Cowork
  toggle, via its `checked` accessibility property.
- On a started session, the page URL exposed as the value of the window's
  `RootWebArea` document (`/cowork/...` means Cowork, otherwise Chat).
- The composer's model button (the one with a `haspopup=menu` accessibility
  property and a name matching a model label like "Opus 4.8" or "Fable 5"),
  which supplies both the model family and version.

If no Claude Desktop window is present at startup, the indicator launches or
re-activates the app (unless started with `--no-launch`) — Microsoft Store
installs are activated by AUMID via `shell:AppsFolder`, direct-download
Squirrel installs by exe path. Activation also restores a window that was
hidden to the system tray with the X button. When the Claude Desktop window
goes away for a few seconds (closed with X, or quit from the tray), the
indicator closes itself — and quitting the indicator (via its right-click
menu) never affects Claude Desktop. Any element that can't be read comes back
as `None` and renders as "?" instead of crashing the poll loop.

The window is draggable with the left mouse button; its position is saved to
`config.json` (next to the exe, or under `%APPDATA%\claude-desktop-indicator`
if that location isn't writable) and restored on the next launch — on any
connected monitor. If its monitor has since been disconnected, it falls back
to the top-right corner of the primary screen.

## Limitations

- Tested against Claude Desktop version 1.20186.9.0. A future UI redesign
  in Claude Desktop can break detection of one or more fields — the affected
  field degrades to "?" rather than crashing the indicator.
- Windows only.
- Closing Claude Desktop's window with X hides it to the tray with the
  process still running; the indicator treats that the same as an exit and
  closes (relaunching the indicator brings both back).

## Privacy & Anthropic ToS

This tool reads only the accessibility tree of the Claude Desktop window
through public Windows UI Automation APIs — the same mechanism screen readers
and other assistive technology use. It performs no code injection, does not
hook or modify Electron/IPC internals, does not intercept or proxy network
traffic, and never reads OAuth tokens, session cookies, or other Claude
Desktop internal state. Nothing the indicator observes is logged to disk or
transmitted anywhere; state lives only in memory and in the small
`config.json` position file described above.

## Running tests

```
python -m unittest discover tests
```

Tests mock the accessibility tree, so they run without Claude Desktop
present.

## License

MIT — see [LICENSE](LICENSE).
