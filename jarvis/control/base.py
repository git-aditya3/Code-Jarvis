"""
Computer-control foundation: results, capabilities and the key table.

Every platform backend implements :class:`Backend` and returns
:class:`ActionResult`, so the rest of JARVIS never branches on the OS. Missing
tools degrade into a clear, actionable message rather than an exception.

Two decorator backends are provided:

* :class:`NullBackend`   — nothing is available; every call explains why.
* :class:`DryRunBackend` — wraps a real backend but performs nothing, reporting
  exactly what *would* have happened. This is how routines can be rehearsed
  safely, and how the whole pipeline is testable on a machine with no display.
"""

from __future__ import annotations

import platform
import shutil
import sys
from dataclasses import dataclass, field
from typing import Any

# ════════════════════════════════════════════════════════════════════════════
#  Results & capabilities
# ════════════════════════════════════════════════════════════════════════════

@dataclass
class ActionResult:
    """Outcome of one control action."""

    ok: bool = True
    message: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    simulated: bool = False
    hint: str = ""

    @classmethod
    def fail(cls, message: str, hint: str = "", **data: Any) -> ActionResult:
        return cls(ok=False, message=message, error=message, data=data, hint=hint)

    @classmethod
    def done(cls, message: str, hint: str = "", **data: Any) -> ActionResult:
        return cls(ok=True, message=message, data=data, hint=hint)

    def __str__(self) -> str:  # pragma: no cover - convenience for debugging
        state = "ok" if self.ok else "failed"
        return f"[{state}] {self.message}"


@dataclass
class Capability:
    """Whether a control area works on this machine, and what to install if not."""

    name: str
    available: bool
    detail: str = ""
    hint: str = ""

    def line(self) -> str:
        mark = "✓" if self.available else "✗"
        text = f"  {mark} {self.name:12s} {self.detail}"
        if not self.available and self.hint:
            text += f"   → {self.hint}"
        return text


def key_table() -> dict[str, dict[str, Any]]:
    """Canonical key name → per-platform codes.

    ``win`` is a Windows virtual-key code, ``mac`` a macOS key code,
    ``xdo`` the name xdotool understands.
    """
    table: dict[str, dict[str, Any]] = {
        "enter": {"win": 0x0D, "mac": 36, "xdo": "Return"},
        "return": {"win": 0x0D, "mac": 36, "xdo": "Return"},
        "tab": {"win": 0x09, "mac": 48, "xdo": "Tab"},
        "space": {"win": 0x20, "mac": 49, "xdo": "space"},
        "backspace": {"win": 0x08, "mac": 51, "xdo": "BackSpace"},
        "delete": {"win": 0x2E, "mac": 117, "xdo": "Delete"},
        "del": {"win": 0x2E, "mac": 117, "xdo": "Delete"},
        "escape": {"win": 0x1B, "mac": 53, "xdo": "Escape"},
        "esc": {"win": 0x1B, "mac": 53, "xdo": "Escape"},
        "home": {"win": 0x24, "mac": 115, "xdo": "Home"},
        "end": {"win": 0x23, "mac": 119, "xdo": "End"},
        "pageup": {"win": 0x21, "mac": 116, "xdo": "Prior"},
        "pgup": {"win": 0x21, "mac": 116, "xdo": "Prior"},
        "pagedown": {"win": 0x22, "mac": 121, "xdo": "Next"},
        "pgdn": {"win": 0x22, "mac": 121, "xdo": "Next"},
        "up": {"win": 0x26, "mac": 126, "xdo": "Up"},
        "down": {"win": 0x28, "mac": 125, "xdo": "Down"},
        "left": {"win": 0x25, "mac": 123, "xdo": "Left"},
        "right": {"win": 0x27, "mac": 124, "xdo": "Right"},
        "insert": {"win": 0x2D, "mac": 114, "xdo": "Insert"},
        "printscreen": {"win": 0x2C, "mac": None, "xdo": "Print"},
        "capslock": {"win": 0x14, "mac": 57, "xdo": "Caps_Lock"},
        "numlock": {"win": 0x90, "mac": None, "xdo": "Num_Lock"},
        "scrolllock": {"win": 0x91, "mac": None, "xdo": "Scroll_Lock"},
        "pause": {"win": 0x13, "mac": None, "xdo": "Pause"},
        "minus": {"win": 0xBD, "mac": 27, "xdo": "minus"},
        "plus": {"win": 0xBB, "mac": 24, "xdo": "plus"},
        "equals": {"win": 0xBB, "mac": 24, "xdo": "equal"},
        "comma": {"win": 0xBC, "mac": 43, "xdo": "comma"},
        "period": {"win": 0xBE, "mac": 47, "xdo": "period"},
        "slash": {"win": 0xBF, "mac": 44, "xdo": "slash"},
        "backslash": {"win": 0xDC, "mac": 42, "xdo": "backslash"},
        "semicolon": {"win": 0xBA, "mac": 41, "xdo": "semicolon"},
        "quote": {"win": 0xDE, "mac": 39, "xdo": "apostrophe"},
        "backtick": {"win": 0xC0, "mac": 50, "xdo": "grave"},
        "bracketleft": {"win": 0xDB, "mac": 33, "xdo": "bracketleft"},
        "bracketright": {"win": 0xDD, "mac": 30, "xdo": "bracketright"},
    }
    for index in range(1, 25):
        table[f"f{index}"] = {"win": 0x6F + index, "mac": _MAC_FKEYS.get(index), "xdo": f"F{index}"}
    for letter in "abcdefghijklmnopqrstuvwxyz":
        table[letter] = {"win": ord(letter.upper()), "mac": None, "xdo": letter}
    for digit in "0123456789":
        table[digit] = {"win": ord(digit), "mac": None, "xdo": digit}
    return table


_MAC_FKEYS = {1: 122, 2: 120, 3: 99, 4: 118, 5: 96, 6: 97, 7: 98, 8: 100,
              9: 101, 10: 109, 11: 103, 12: 111, 13: 105, 14: 107, 15: 113,
              16: 106, 17: 64, 18: 79, 19: 80, 20: 90}

MODIFIER_ALIASES = {
    "ctrl": "ctrl", "control": "ctrl", "ctl": "ctrl",
    "alt": "alt", "option": "alt", "opt": "alt",
    "shift": "shift", "shft": "shift",
    "win": "win", "windows": "win", "super": "win", "meta": "win", "cmd": "win",
    "command": "win", "commandorcontrol": "win", "cmdorctrl": "win",
}

# Windows virtual keys / macOS modifiers for the modifier set
MODIFIER_CODES = {
    "ctrl": {"win": 0x11, "mac": "control down", "xdo": "ctrl"},
    "alt": {"win": 0x12, "mac": "option down", "xdo": "alt"},
    "shift": {"win": 0x10, "mac": "shift down", "xdo": "shift"},
    "win": {"win": 0x5B, "mac": "command down", "xdo": "super"},
}

KEYS = key_table()


def parse_hotkey(combination: str) -> tuple[list[str], str] | None:
    """'Ctrl+Shift+T' → (['ctrl','shift'], 't'). None when unparseable."""
    if not combination or not combination.strip():
        return None
    parts = [part.strip().lower() for part in combination.replace(" ", "").split("+") if part.strip()]
    if not parts:
        return None
    keys: list[str] = []
    for part in parts:
        canonical = MODIFIER_ALIASES.get(part, part)
        if canonical in MODIFIER_CODES or canonical in KEYS:
            keys.append(canonical)
        else:
            return None
    if not keys:
        return None
    return keys[:-1], keys[-1]


def human_error(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


def os_name() -> str:
    if sys.platform.startswith("win"):
        return "Windows"
    if sys.platform == "darwin":
        return "macOS"
    return "Linux"


def which(*commands: str) -> str | None:
    for command in commands:
        found = shutil.which(command)
        if found:
            return found
    return None


# ════════════════════════════════════════════════════════════════════════════
#  Backend interface
# ════════════════════════════════════════════════════════════════════════════

class Backend:
    """What a platform must be able to do (implementations may say 'not available')."""

    name = "base"

    # ── keyboard ─────────────────────────────────────────────────────────
    def type_text(self, text: str, interval: float = 0.0) -> ActionResult:
        return ActionResult.fail(f"Typing is not supported on {os_name()} yet.")

    def press_hotkey(self, combination: str) -> ActionResult:
        return ActionResult.fail(f"Key presses are not supported on {os_name()} yet.")

    # ── mouse ────────────────────────────────────────────────────────────
    def mouse_move(self, x: int, y: int, relative: bool = False) -> ActionResult:
        return ActionResult.fail("Mouse control is not available.")

    def mouse_click(self, button: str = "left", clicks: int = 1) -> ActionResult:
        return ActionResult.fail("Mouse clicks are not available.")

    def mouse_scroll(self, amount: int) -> ActionResult:
        return ActionResult.fail("Scrolling is not available.")

    def mouse_position(self) -> ActionResult:
        return ActionResult.fail("Reading the mouse position is not available.")

    def mouse_drag(self, x1: int, y1: int, x2: int, y2: int, duration: float = 0.4) -> ActionResult:
        return ActionResult.fail("Dragging is not available.")

    # ── windows ──────────────────────────────────────────────────────────
    def list_windows(self) -> ActionResult:
        return ActionResult.fail("Window listing is not available.")

    def active_window(self) -> ActionResult:
        return ActionResult.fail("Reading the active window is not available.")

    def focus_window(self, title: str) -> ActionResult:
        return ActionResult.fail("Focusing windows is not available.")

    def close_window(self, title: str) -> ActionResult:
        return ActionResult.fail("Closing windows is not available.")

    def window_action(self, title: str, action: str) -> ActionResult:
        """Resize/place a window.

        ``action`` is one of ``minimize``, ``maximize``, ``restore``, ``fullscreen``,
        ``always_on_top``, ``no_longer_on_top``, ``snap_left``, ``snap_right``,
        ``center``, ``switch_workspace:N`` (go to workspace N) or ``move_to_workspace:N``.
        """
        return ActionResult.fail(f"Window action '{action}' is not available.")

    def switch_window(self) -> ActionResult:
        return self.press_hotkey("alt+tab")

    # ── audio & display ──────────────────────────────────────────────────
    def volume(self, level: int | None = None, mute: str | None = None) -> ActionResult:
        return ActionResult.fail("Volume control is not available.")

    def brightness(self, level: int | None = None) -> ActionResult:
        return ActionResult.fail("Brightness control is not available.")

    def media(self, action: str = "play_pause") -> ActionResult:
        """Transport keys: play_pause, next, previous, stop.

        These are *media key presses* rather than app APIs, so they work with
        whatever player has focus — Spotify, a browser tab, VLC, anything.
        """
        return ActionResult.fail("Media keys are not available.")

    def system_toggle(self, kind: str, state: bool | None = None) -> ActionResult:
        """Switch a system-level setting on/off.

        Supported kinds (where the OS allows it): ``wifi``, ``bluetooth``,
        ``nightlight``, ``dark_mode``, ``dnd`` (do not disturb), ``power_saver``.
        """
        return ActionResult.fail(f"'{kind}' is not switchable on {os_name()} through me.")

    # ── processes ────────────────────────────────────────────────────────
    def list_processes(self, limit: int = 12, sort_by: str = "cpu") -> ActionResult:
        return ActionResult.fail("Process listing is not available.")

    def kill_process(self, target: str, force: bool = False) -> ActionResult:
        return ActionResult.fail("Stopping processes is not available.")

    def start_process(self, command: str, args: list[str] | None = None,
                      cwd: str | None = None) -> ActionResult:
        return ActionResult.fail("Starting processes is not available.")

    # ── shell ────────────────────────────────────────────────────────────
    def run_shell(self, command: str, timeout: float = 20.0, cwd: str | None = None) -> ActionResult:
        return ActionResult.fail("Running shell commands is not available.")

    # ── screen ───────────────────────────────────────────────────────────
    def screen_text(self) -> ActionResult:
        return ActionResult.fail("Reading the screen is not available.")


class NullBackend(Backend):
    """Used when a platform backend cannot initialise."""

    name = "none"

    def __init__(self, reason: str = "") -> None:
        self.reason = reason

    def _no(self, what: str) -> ActionResult:
        detail = f" {self.reason}" if self.reason else ""
        return ActionResult.fail(
            f"{what} is unavailable on this system.{detail}",
            hint="Run `python run_jarvis.py --doctor` to see what this machine supports.",
        )

    def type_text(self, text: str, interval: float = 0.0) -> ActionResult:
        return self._no("Typing")

    def press_hotkey(self, combination: str) -> ActionResult:
        return self._no("Key presses")

    def mouse_move(self, x: int, y: int, relative: bool = False) -> ActionResult:
        return self._no("Mouse control")

    def mouse_click(self, button: str = "left", clicks: int = 1) -> ActionResult:
        return self._no("Mouse clicks")

    def mouse_scroll(self, amount: int) -> ActionResult:
        return self._no("Scrolling")

    def mouse_position(self) -> ActionResult:
        return self._no("Mouse position")

    def list_windows(self) -> ActionResult:
        return self._no("Window listing")

    def active_window(self) -> ActionResult:
        return self._no("Active window")

    def focus_window(self, title: str) -> ActionResult:
        return self._no("Focusing windows")

    def close_window(self, title: str) -> ActionResult:
        return self._no("Closing windows")

    def window_action(self, title: str, action: str) -> ActionResult:
        return self._no("Window control")

    def volume(self, level: int | None = None, mute: str | None = None) -> ActionResult:
        return self._no("Volume control")

    def brightness(self, level: int | None = None) -> ActionResult:
        return self._no("Brightness control")

    def list_processes(self, limit: int = 12, sort_by: str = "cpu") -> ActionResult:
        return self._no("Process listing")

    def kill_process(self, target: str, force: bool = False) -> ActionResult:
        return self._no("Stopping processes")

    def start_process(self, command: str, args: list[str] | None = None,
                      cwd: str | None = None) -> ActionResult:
        return self._no("Starting processes")

    def run_shell(self, command: str, timeout: float = 20.0, cwd: str | None = None) -> ActionResult:
        return self._no("Running shell commands")

    def screen_text(self) -> ActionResult:
        return self._no("Reading the screen")


class DryRunBackend:
    """Wraps a backend and performs nothing — reports what would have happened.

    Set ``JARVIS_DRY_RUN=1`` (or the ``dry_run`` setting) to rehearse a routine
    without touching the machine. Every reply carries ``simulated=True`` so the
    UI and the audit log can mark it clearly.
    """

    name = "simulation"

    def __init__(self, inner: Backend) -> None:
        self.inner = inner
        self._log: list[str] = []

    @property
    def log(self) -> list[str]:
        return list(self._log)

    def _simulate(self, what: str, **data: Any) -> ActionResult:
        self._log.append(what)
        return ActionResult(ok=True, message=f"[simulation] {what}", data=data, simulated=True)

    # ── keyboard ─────────────────────────────────────────────────────────
    def type_text(self, text: str, interval: float = 0.0) -> ActionResult:
        preview = text if len(text) <= 60 else text[:57] + "…"
        return self._simulate(f"would type {preview!r}", text=text)

    def press_hotkey(self, combination: str) -> ActionResult:
        return self._simulate(f"would press {combination}", keys=combination)

    # ── mouse ────────────────────────────────────────────────────────────
    def mouse_move(self, x: int, y: int, relative: bool = False) -> ActionResult:
        return self._simulate(f"would move the mouse to {x},{y}" + (" (relative)" if relative else ""))

    def mouse_click(self, button: str = "left", clicks: int = 1) -> ActionResult:
        return self._simulate(f"would {button}-click {clicks}x")

    def mouse_scroll(self, amount: int) -> ActionResult:
        return self._simulate(f"would scroll {amount}")

    def mouse_position(self) -> ActionResult:
        return self._simulate("would read the mouse position")

    def mouse_drag(self, x1: int, y1: int, x2: int, y2: int, duration: float = 0.4) -> ActionResult:
        return self._simulate(f"would drag from {x1},{y1} to {x2},{y2}")

    # ── windows ──────────────────────────────────────────────────────────
    def list_windows(self) -> ActionResult:
        return ActionResult(ok=True, message="[simulation] no window list", data={"windows": []},
                            simulated=True)

    def active_window(self) -> ActionResult:
        inner = self.inner.active_window()
        if inner.ok:
            return ActionResult(ok=True, message=f"[simulation] active window is {inner.data.get('title')}",
                                data=inner.data, simulated=True)
        return self._simulate("would read the active window")

    def focus_window(self, title: str) -> ActionResult:
        return self._simulate(f"would focus the window matching {title!r}")

    def close_window(self, title: str) -> ActionResult:
        return self._simulate(f"would close the window matching {title!r}")

    def window_action(self, title: str, action: str) -> ActionResult:
        return self._simulate(f"would {action} the window matching {title!r}")

    def volume(self, level: int | None = None, mute: str | None = None) -> ActionResult:
        if mute:
            return self._simulate(f"would set mute={mute}")
        if level is not None:
            return self._simulate(f"would set the volume to {level}%")
        inner = self.inner.volume()
        return ActionResult(ok=True, message=f"[simulation] {inner.message}", data=inner.data, simulated=True)

    def brightness(self, level: int | None = None) -> ActionResult:
        if level is not None:
            return self._simulate(f"would set the brightness to {level}%")
        return self._simulate("would read the brightness")

    def media(self, action: str = "play_pause") -> ActionResult:
        return self._simulate(f"would press the {action.replace('_', '/')} media key")

    def system_toggle(self, kind: str, state: bool | None = None) -> ActionResult:
        if state is None:
            return self._simulate(f"would toggle {kind}")
        return self._simulate(f"would turn {kind} {'on' if state else 'off'}")

    # ── processes ────────────────────────────────────────────────────────
    def list_processes(self, limit: int = 12, sort_by: str = "cpu") -> ActionResult:
        return self.inner.list_processes(limit=limit, sort_by=sort_by)

    def kill_process(self, target: str, force: bool = False) -> ActionResult:
        return self._simulate(f"would stop the process {target!r}" + (" (forced)" if force else ""))

    def start_process(self, command: str, args: list[str] | None = None,
                      cwd: str | None = None) -> ActionResult:
        suffix = " " + " ".join(args) if args else ""
        return self._simulate(f"would launch {command}{suffix}")

    # ── shell & screen ───────────────────────────────────────────────────
    def run_shell(self, command: str, timeout: float = 20.0, cwd: str | None = None) -> ActionResult:
        return self._simulate(f"would run the command: {command}")

    def screen_text(self) -> ActionResult:
        inner = self.inner.screen_text()
        if inner.ok:
            return ActionResult(ok=True, message="[simulation] screen text read", data=inner.data,
                                simulated=True)
        return self._simulate("would read the text on screen")


def current_platform() -> str:
    return platform.system()
