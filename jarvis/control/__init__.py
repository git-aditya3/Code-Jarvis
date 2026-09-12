"""
The :class:`Controller` — one façade over every platform backend.

Skills, actions, routines and the planner all drive the computer through this
object, never through a platform API directly. It also owns the simulation
switch (``dry_run``), so a routine can be rehearsed end-to-end without anything
happening on the machine.

    controller = Controller(settings)
    controller.type_text("hello")
    controller.press("ctrl+s")
    controller.focus_window("chrome")
    controller.volume(35)
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .backends import backend_capabilities, control_install_hints, open_path_with_default, pick_backend
from .base import (
    KEYS,
    MODIFIER_ALIASES,
    ActionResult,
    Backend,
    Capability,
    DryRunBackend,
    NullBackend,
    current_platform,
    human_error,
    os_name,
    parse_hotkey,
    which,
)

__all__ = [
    "KEYS",
    "MODIFIER_ALIASES",
    "ActionResult",
    "Capability",
    "Controller",
    "DryRunBackend",
    "NullBackend",
    "backend_capabilities",
    "control_install_hints",
    "current_platform",
    "human_error",
    "open_path_with_default",
    "os_name",
    "parse_hotkey",
    "which",
]


@dataclass
class ControlReport:
    """Human-readable summary of what this machine can do."""

    platform: str
    backend: str
    simulated: bool
    capabilities: dict[str, Capability]

    def lines(self) -> list[str]:
        header = f"Computer control — {self.platform} (backend: {self.backend}"
        header += ", simulation mode)" if self.simulated else ")"
        rows = [header]
        for capability in self.capabilities.values():
            rows.append(capability.line())
        ready = [name for name, cap in self.capabilities.items() if cap.available]
        missing = [name for name, cap in self.capabilities.items() if not cap.available]
        rows.append(f"  ready: {', '.join(ready) or 'nothing'}")
        if missing:
            rows.append(f"  needs setup: {', '.join(missing)}")
            for hint in control_install_hints():
                rows.append(f"    • {hint}")
        return rows


class Controller:
    """Cross-platform computer control with an optional simulation layer."""

    def __init__(self, settings: Any = None, backend: Backend | None = None,
                 simulate: bool | None = None) -> None:
        self.settings = settings
        self._real: Backend = backend or pick_backend()
        if simulate is None:
            simulate = self._simulate_requested(settings)
        self.simulate = bool(simulate)
        self._backend: Backend = DryRunBackend(self._real) if self.simulate else self._real

    # ── configuration ────────────────────────────────────────────────────
    @staticmethod
    def _simulate_requested(settings: Any) -> bool:
        env = os.environ.get("JARVIS_DRY_RUN", "").strip().lower()
        if env in {"1", "true", "yes", "on"}:
            return True
        if settings is not None:
            try:
                return bool(settings.get("dry_run", False))
            except Exception:
                return False
        return False

    @property
    def backend(self) -> Backend:
        """The active backend (a DryRunBackend wrapper when simulating)."""
        return self._backend

    @property
    def real_backend(self) -> Backend:
        """The underlying platform backend, even while simulating."""
        return self._real

    @property
    def backend_name(self) -> str:
        return self._backend.name

    def set_simulation(self, enabled: bool) -> None:
        self.simulate = bool(enabled)
        self._backend = DryRunBackend(self._real) if self.simulate else self._real

    def capabilities(self) -> dict[str, Capability]:
        caps = backend_capabilities(self._real)
        if self.simulate:
            return {
                name: Capability(cap.name, cap.available, cap.detail + " · simulated", cap.hint)
                for name, cap in caps.items()
            }
        return caps

    def report(self) -> ControlReport:
        return ControlReport(
            platform=f"{current_platform()} ({os_name()})",
            backend=self._backend.name,
            simulated=self.simulate,
            capabilities=self.capabilities(),
        )

    def report_text(self) -> str:
        return "\n".join(self.report().lines())

    def available(self, capability: str) -> bool:
        return bool(self.capabilities().get(capability, Capability(capability, False)).available)

    # ── keyboard ─────────────────────────────────────────────────────────
    def type_text(self, text: str, interval: float = 0.0) -> ActionResult:
        return self._backend.type_text(text, interval)

    def press(self, combination: str) -> ActionResult:
        return self._backend.press_hotkey(combination)

    def press_key(self, key: str) -> ActionResult:
        return self._backend.press_hotkey(key)

    # ── mouse ────────────────────────────────────────────────────────────
    def move_mouse(self, x: int, y: int, relative: bool = False) -> ActionResult:
        return self._backend.mouse_move(int(x), int(y), relative)

    def click(self, button: str = "left", clicks: int = 1) -> ActionResult:
        return self._backend.mouse_click(button, clicks)

    def scroll(self, amount: int) -> ActionResult:
        return self._backend.mouse_scroll(int(amount))

    def drag(self, x1: int, y1: int, x2: int, y2: int, duration: float = 0.4) -> ActionResult:
        return self._backend.mouse_drag(int(x1), int(y1), int(x2), int(y2), float(duration))

    def click_at(self, x: int, y: int, button: str = "left", clicks: int = 1) -> ActionResult:
        moved = self._backend.mouse_move(int(x), int(y), False)
        if not moved.ok:
            return moved
        clicked = self._backend.mouse_click(button, clicks)
        if clicked.simulated and moved.simulated:
            return ActionResult(ok=True, message=f"[simulation] would click {button} at {x},{y}",
                                simulated=True, data=clicked.data)
        return clicked

    def pointer(self) -> ActionResult:
        return self._backend.mouse_position()

    # ── windows ──────────────────────────────────────────────────────────
    def windows(self) -> ActionResult:
        return self._backend.list_windows()

    def active_window(self) -> ActionResult:
        return self._backend.active_window()

    def focus_window(self, title: str) -> ActionResult:
        return self._backend.focus_window(title)

    def close_window(self, title: str) -> ActionResult:
        return self._backend.close_window(title)

    def window_action(self, title: str, action: str) -> ActionResult:
        if not title:                        # "this window" means whatever is in front
            active = self._backend.active_window()
            if active.ok:
                title = str(active.data.get("title") or "")
        return self._backend.window_action(title, action)

    def window_control(self, title: str, action: str) -> ActionResult:
        """Place a window: fullscreen, always-on-top, snaps, workspaces."""
        return self._backend.window_action(title, action)

    def switch_window(self) -> ActionResult:
        return self._backend.switch_window()

    # ── media & system switches ──────────────────────────────────────────
    def media(self, action: str = "play_pause") -> ActionResult:
        return self._backend.media(action)

    def system_toggle(self, kind: str, state: bool | None = None) -> ActionResult:
        return self._backend.system_toggle(kind, state)

    # ── audio & display ──────────────────────────────────────────────────
    def volume(self, level: int | None = None, mute: str | None = None) -> ActionResult:
        return self._backend.volume(level, mute)

    def brightness(self, level: int | None = None) -> ActionResult:
        return self._backend.brightness(level)

    # ── processes ────────────────────────────────────────────────────────
    def processes(self, limit: int = 12, sort_by: str = "cpu") -> ActionResult:
        return self._backend.list_processes(limit, sort_by)

    def kill_process(self, target: str, force: bool = False) -> ActionResult:
        return self._backend.kill_process(target, force)

    def start_process(self, command: str, args: list[str] | None = None,
                      cwd: str | None = None) -> ActionResult:
        return self._backend.start_process(command, args, cwd)

    # ── shell ────────────────────────────────────────────────────────────
    def shell(self, command: str, timeout: float | None = None, cwd: str | None = None) -> ActionResult:
        limit = float(timeout if timeout is not None else self._setting("shell_timeout", 20))
        return self._backend.run_shell(command, limit, cwd)

    def _setting(self, key: str, default: Any) -> Any:
        if self.settings is None:
            return default
        try:
            value = self.settings.get(key, default)
            return default if value is None else value
        except Exception:
            return default

    # ── screen ───────────────────────────────────────────────────────────
    def read_screen(self) -> ActionResult:
        return self._backend.screen_text()

    def screenshot(self, path: str | Path | None = None) -> ActionResult:
        from .screen import capture_screen

        shot = capture_screen(path)
        if not shot.ok:
            return ActionResult.fail(f"Screen capture failed: {shot.error}",
                                     hint="pip install pillow mss, and run on a machine with a display.")
        return ActionResult.done(f"Screenshot saved to {shot.path}", path=shot.path,
                                 size=list(shot.size))

    # ── power (execution is authorised by the policy layer first) ────────
    def power(self, action: str) -> ActionResult:
        action = (action or "").lower().strip()
        try:
            if action in ("lock", "lock screen"):
                return self._lock()
            if action in ("sleep", "suspend"):
                return self._suspend()
            if action in ("restart", "reboot"):
                return self._invoke(["shutdown", "/r", "/t", "30"]) if os_name() == "Windows" else \
                    self._invoke(["shutdown", "-r", "+1"])
            if action in ("shutdown", "poweroff", "power off"):
                return self._invoke(["shutdown", "/s", "/t", "30"]) if os_name() == "Windows" else \
                    self._invoke(["shutdown", "-h", "+1"])
            if action in ("signout", "log out", "logout"):
                if os_name() == "Windows":
                    return self._invoke(["shutdown", "/l"])
                if os_name() == "macOS":
                    return self._invoke(["osascript", "-e",
                                         'tell application "System Events" to log out'])
                return self._invoke(["loginctl", "terminate-user", os.environ.get("USER", "")])
        except Exception as exc:
            return ActionResult.fail(f"Power action failed: {human_error(exc)}")
        return ActionResult.fail(f"I don't know the power action “{action}”.")

    def _invoke(self, argv: list[str], timeout: float = 10.0) -> ActionResult:
        import subprocess

        if self.simulate:
            return ActionResult(message=f"[simulation] would run {' '.join(argv)}", simulated=True)
        try:
            subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except FileNotFoundError:
            return ActionResult.fail(f"'{argv[0]}' is not available on this system.")
        return ActionResult.done(f"Ran {' '.join(argv)}")

    def _lock(self) -> ActionResult:
        if os_name() == "Windows":
            if self.simulate:
                return ActionResult(message="[simulation] would lock the screen", simulated=True)
            import ctypes

            ctypes.windll.user32.LockWorkStation()  # type: ignore[attr-defined]
            return ActionResult.done("Screen locked.")
        if os_name() == "macOS":
            return self._invoke(
                ["/System/Library/CoreServices/Menu Extras/User.menu/Contents/Resources/CGSession",
                 "-suspend"]
            )
        for candidate in (["loginctl", "lock-session"], ["xdg-screensaver", "lock"],
                          ["gnome-screensaver-command", "-l"]):
            if which(candidate[0]):
                return self._invoke(candidate)
        return ActionResult.fail("I could not find a lock command on this system.")

    def _suspend(self) -> ActionResult:
        if os_name() == "Windows":
            return self._invoke(["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"])
        if os_name() == "macOS":
            return self._invoke(["pmset", "sleepnow"])
        if which("systemctl"):
            return self._invoke(["systemctl", "suspend"])
        return ActionResult.fail("I could not find a suspend command on this system.")
