"""
Platform backends for computer control.

Windows uses ctypes against user32 (no third-party package needed), macOS uses
AppleScript through ``osascript``, and Linux drives the standard X11 tools
(``xdotool``, ``wmctrl``, ``pactl``, ``brightnessctl``). Each backend reports
what is missing instead of failing silently, so ``--doctor`` can tell the user
exactly which package to install.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .base import (
    KEYS,
    MODIFIER_CODES,
    ActionResult,
    Capability,
    human_error,
    os_name,
    parse_hotkey,
    which,
)
from .screen import screen_text

# ════════════════════════════════════════════════════════════════════════════
#  helpers
# ════════════════════════════════════════════════════════════════════════════

MAX_OUTPUT = 6000
CRITICAL_PROCESS_NAMES = {
    "system", "system idle process", "registry", "memory compression", "csrss.exe",
    "wininit.exe", "winlogon.exe", "services.exe", "lsass.exe", "smss.exe",
    "init", "systemd", "kthreadd", "launchd", "kernel_task",
}


def _truncate(text: str, limit: int = MAX_OUTPUT) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…[{len(text) - limit} more characters]"


def _processes_list(limit: int = 12, sort_by: str = "cpu") -> ActionResult:
    """Shared psutil-based process listing (works on all three platforms)."""
    try:
        import psutil
    except Exception:
        return ActionResult.fail("Process listing needs psutil: pip install psutil")

    rows: list[dict[str, Any]] = []
    for process in psutil.process_iter(["pid", "name", "cpu_percent", "memory_percent", "username"]):
        try:
            info = process.info
            rows.append({
                "pid": info.get("pid"),
                "name": info.get("name") or "?",
                "cpu": round(float(info.get("cpu_percent") or 0.0), 1),
                "memory": round(float(info.get("memory_percent") or 0.0), 1),
                "user": (info.get("username") or "").split("\\")[-1],
            })
        except Exception:
            continue

    key = "memory" if sort_by.startswith("mem") else "cpu"
    rows.sort(key=lambda row: (row[key] or 0, row["cpu"]), reverse=True)
    top = rows[: max(1, min(limit, 60))]
    lines = [f"{len(rows)} processes running; top {len(top)} by {key}:"]
    lines += [f"  {row['name']} (pid {row['pid']}) — {row['cpu']:.0f}% cpu, {row['memory']:.1f}% mem"
              for row in top]
    return ActionResult.done("\n".join(lines), processes=top, total=len(rows))


def _kill_process(target: str, force: bool = False) -> ActionResult:
    """Terminate by pid or by name (all matches). Refuses critical processes."""
    try:
        import psutil
    except Exception:
        return ActionResult.fail("Stopping processes needs psutil: pip install psutil")

    target = (target or "").strip()
    if not target:
        return ActionResult.fail("Which process should I stop?")

    victims: list[Any] = []
    if target.isdigit():
        try:
            victims = [psutil.Process(int(target))]
        except Exception as exc:
            return ActionResult.fail(f"No process with pid {target}: {exc}")
    else:
        needle = target.lower().removesuffix(".exe")
        for process in psutil.process_iter(["name", "pid"]):
            try:
                name = (process.info.get("name") or "")
                if name.lower().removesuffix(".exe") == needle or needle in name.lower():
                    victims.append(process)
            except Exception:
                continue
    if not victims:
        return ActionResult.fail(f"No running process matches “{target}”.")

    stopped: list[str] = []
    refused: list[str] = []
    for process in victims[:20]:
        try:
            name = process.name()
            if name.lower() in CRITICAL_PROCESS_NAMES:
                refused.append(name)
                continue
            if process.pid == os.getpid():
                refused.append(f"{name} (that is me)")
                continue
            if force:
                process.kill()
            else:
                process.terminate()
            stopped.append(f"{name} (pid {process.pid})")
        except Exception:
            continue

    if not stopped and refused:
        return ActionResult.fail(
            "I will not stop " + ", ".join(refused) + " — that is a critical system process.",
            hint="Stopping those would destabilise the machine.",
        )
    message = "Stopped: " + ", ".join(stopped) if stopped else "Nothing was stopped."
    if refused:
        message += "\nRefused (critical): " + ", ".join(refused)
    return ActionResult.done(message, stopped=stopped, refused=refused)


def _start_process(command: str, args: list[str] | None = None, cwd: str | None = None) -> ActionResult:
    """Launch detached, without a shell."""
    if not command:
        return ActionResult.fail("What should I launch?")
    argv = [command, *(args or [])]
    try:
        kwargs: dict[str, Any] = {
            "cwd": os.path.expanduser(cwd) if cwd else None,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "stdin": subprocess.DEVNULL,
        }
        if sys.platform.startswith("win"):
            kwargs["creationflags"] = getattr(subprocess, "DETACHED_PROCESS", 0) | \
                getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        else:
            kwargs["start_new_session"] = True
        process = subprocess.Popen(argv, **kwargs)
        return ActionResult.done(f"Launched {command} (pid {process.pid}).", pid=process.pid)
    except FileNotFoundError:
        return ActionResult.fail(f"'{command}' is not installed or not on PATH.",
                                 hint="Check the program name, or use a full path.")
    except Exception as exc:
        return ActionResult.fail(f"Could not launch {command}: {human_error(exc)}")


def _run_shell(command: str, timeout: float = 20.0, cwd: str | None = None) -> ActionResult:
    """Run a shell command and capture its output (authorisation happens earlier)."""
    if not command.strip():
        return ActionResult.fail("Which command should I run?")
    started = time.time()
    shell_argv = ["cmd.exe", "/c", command] if sys.platform.startswith("win") else \
                 ["/bin/sh", "-c", command]
    try:
        completed = subprocess.run(
            shell_argv,
            cwd=os.path.expanduser(cwd) if cwd else None,
            capture_output=True,
            text=True,
            timeout=max(1.0, float(timeout)),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform.startswith("win") else 0,
        )
    except subprocess.TimeoutExpired:
        return ActionResult.fail(f"The command did not finish within {timeout:.0f}s and was stopped.")
    except Exception as exc:
        return ActionResult.fail(f"Command failed to start: {human_error(exc)}")

    output = _truncate((completed.stdout or "") + (("\n" + completed.stderr) if completed.stderr else ""))
    seconds = time.time() - started
    if completed.returncode == 0:
        return ActionResult.done(
            f"Command finished in {seconds:.1f}s.\n{output or '(no output)'}",
            returncode=0, output=output, seconds=round(seconds, 2),
        )
    return ActionResult(
        ok=False,
        message=f"Command exited with code {completed.returncode}.\n{output or '(no output)'}",
        error=f"exit code {completed.returncode}",
        data={"returncode": completed.returncode, "output": output, "seconds": round(seconds, 2)},
    )


def _screen_text_result() -> ActionResult:
    ok, text, detail = screen_text()
    if not ok:
        return ActionResult.fail(detail, hint="Install mss + pillow + pytesseract and Tesseract.")
    if not text:
        return ActionResult.done("I captured the screen but found no readable text.", text="")
    preview = text if len(text) < 4000 else text[:4000] + "…"
    return ActionResult.done(
        f"Read {len(text)} characters from the screen.\n{preview}",
        text=text, characters=len(text),
    )


# ════════════════════════════════════════════════════════════════════════════
#  Windows
# ════════════════════════════════════════════════════════════════════════════

class WindowsBackend:
    """ctypes-based control of Windows (no extra pip packages required)."""

    name = "windows"

    # ── ctypes plumbing ──────────────────────────────────────────────────
    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        self.ctypes = ctypes
        self.wintypes = wintypes
        self.user32 = ctypes.windll.user32
        self.kernel32 = ctypes.windll.kernel32

        class KEYBDINPUT(ctypes.Structure):
            _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                        ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                        ("dwExtraInfo", ctypes.c_void_p)]

        class MOUSEINPUT(ctypes.Structure):
            _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                        ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                        ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_void_p)]

        class HARDWAREINPUT(ctypes.Structure):
            _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD),
                        ("wParamH", wintypes.WORD)]

        class _INPUTunion(ctypes.Union):
            _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT), ("hi", HARDWAREINPUT)]

        class INPUT(ctypes.Structure):
            _anonymous_ = ("u",)
            _fields_ = [("type", wintypes.DWORD), ("u", _INPUTunion)]

        self._INPUT = INPUT
        self._KEYBDINPUT = KEYBDINPUT
        self._MOUSEINPUT = MOUSEINPUT
        self.user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
        self.user32.SendInput.restype = wintypes.UINT

    # ── low level ────────────────────────────────────────────────────────
    def _send(self, inputs: list[Any]) -> bool:
        ctypes = self.ctypes
        count = len(inputs)
        array = (self._INPUT * count)(*inputs)
        sent = self.user32.SendInput(count, array, ctypes.sizeof(self._INPUT))
        return sent == count

    def _key_event(self, vk: int, up: bool = False) -> Any:

        flags = 0x0002 if up else 0x0000  # KEYEVENTF_KEYUP
        return self._INPUT(type=1, ki=self._KEYBDINPUT(wVk=vk, wScan=0, dwFlags=flags,
                                                       time=0, dwExtraInfo=None))

    def _unicode_event(self, char: str, up: bool = False) -> Any:
        flags = 0x0004 | (0x0002 if up else 0)  # KEYEVENTF_UNICODE [+ KEYUP]
        return self._INPUT(type=1, ki=self._KEYBDINPUT(wVk=0, wScan=ord(char), dwFlags=flags,
                                                       time=0, dwExtraInfo=None))

    # ── keyboard ─────────────────────────────────────────────────────────
    def type_text(self, text: str, interval: float = 0.0) -> ActionResult:
        try:
            for char in text:
                if char == "\n":
                    self._send([self._key_event(0x0D), self._key_event(0x0D, True)])
                elif char == "\t":
                    self._send([self._key_event(0x09), self._key_event(0x09, True)])
                else:
                    self._send([self._unicode_event(char), self._unicode_event(char, True)])
                if interval:
                    time.sleep(interval)
        except Exception as exc:
            return ActionResult.fail(f"Typing failed: {human_error(exc)}",
                                     hint="Some windows block synthetic input; try clicking it first.")
        preview = text if len(text) <= 60 else text[:57] + "…"
        return ActionResult.done(f"Typed {len(text)} characters: {preview!r}")

    def press_hotkey(self, combination: str) -> ActionResult:
        parsed = parse_hotkey(combination)
        if not parsed:
            return ActionResult.fail(f"I don't know the key combination “{combination}”.")
        modifiers, key = parsed
        codes: list[int] = []
        for modifier in modifiers:
            codes.append(MODIFIER_CODES[modifier]["win"])
        if key in KEYS:
            vk = KEYS[key]["win"]
            if vk is None:
                return ActionResult.fail(f"'{key}' has no Windows key code.")
            codes.append(vk)
        elif len(key) == 1:
            codes.append(ord(key.upper()))
        else:
            return ActionResult.fail(f"I don't know the key “{key}”.")
        try:
            for code in codes:
                self._send([self._key_event(code)])
            for code in reversed(codes):
                self._send([self._key_event(code, True)])
        except Exception as exc:
            return ActionResult.fail(f"Key press failed: {human_error(exc)}")
        return ActionResult.done(f"Pressed {combination}.")

    # ── mouse ────────────────────────────────────────────────────────────
    def mouse_move(self, x: int, y: int, relative: bool = False) -> ActionResult:
        try:
            if relative:
                self.user32.mouse_event(0x0001, int(x), int(y), 0, 0)  # MOUSEEVENTF_MOVE
            else:
                self.user32.SetCursorPos(int(x), int(y))
        except Exception as exc:
            return ActionResult.fail(f"Mouse move failed: {human_error(exc)}")
        return ActionResult.done(f"Moved the pointer to {x},{y}" + (" (relative)" if relative else ""))

    def mouse_click(self, button: str = "left", clicks: int = 1) -> ActionResult:
        flags = {"left": (0x0002, 0x0004), "right": (0x0008, 0x0010),
                 "middle": (0x0020, 0x0040)}.get(button.lower())
        if not flags:
            return ActionResult.fail(f"Unknown mouse button “{button}”.")
        try:
            for _ in range(max(1, min(int(clicks), 3))):
                self.user32.mouse_event(flags[0], 0, 0, 0, 0)
                self.user32.mouse_event(flags[1], 0, 0, 0, 0)
                time.sleep(0.05)
        except Exception as exc:
            return ActionResult.fail(f"Click failed: {human_error(exc)}")
        return ActionResult.done(f"{button.title()}-clicked {clicks}x.")

    def mouse_scroll(self, amount: int) -> ActionResult:
        try:
            self.user32.mouse_event(0x0800, 0, 0, int(amount) * 120, 0)  # MOUSEEVENTF_WHEEL
        except Exception as exc:
            return ActionResult.fail(f"Scroll failed: {human_error(exc)}")
        return ActionResult.done(f"Scrolled {'down' if amount < 0 else 'up'} {abs(int(amount))} notches.")

    def mouse_position(self) -> ActionResult:
        from ctypes import wintypes

        point = wintypes.POINT()
        self.user32.GetCursorPos(self.ctypes.byref(point))
        return ActionResult.done(f"Pointer at {point.x},{point.y}", x=point.x, y=point.y)

    # ── windows ──────────────────────────────────────────────────────────
    def list_windows(self) -> ActionResult:
        from ctypes import wintypes

        windows: list[dict[str, Any]] = []
        user32 = self.user32

        def callback(hwnd, _lparam):
            if not user32.IsWindowVisible(hwnd):
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            if length == 0:
                return True
            buffer = self.ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buffer, length + 1)
            title = buffer.value.strip()
            if not title:
                return True
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, self.ctypes.byref(pid))
            windows.append({"id": int(hwnd), "title": title, "pid": int(pid.value)})
            return True

        enum_proc = self.ctypes.WINFUNCTYPE(self.ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
        try:
            user32.EnumWindows(enum_proc(callback), 0)
        except Exception as exc:
            return ActionResult.fail(f"Could not list windows: {human_error(exc)}")
        if not windows:
            return ActionResult.done("No visible windows.", windows=[])
        lines = [f"{len(windows)} open windows:"] + [f"  {w['title'][:70]}" for w in windows[:25]]
        return ActionResult.done("\n".join(lines), windows=windows)

    def active_window(self) -> ActionResult:
        hwnd = self.user32.GetForegroundWindow()
        length = self.user32.GetWindowTextLengthW(hwnd)
        buffer = self.ctypes.create_unicode_buffer(length + 1)
        self.user32.GetWindowTextW(hwnd, buffer, length + 1)
        title = buffer.value.strip() or "(unknown)"
        return ActionResult.done(f"Active window: {title}", title=title, id=int(hwnd))

    def _find_window(self, title: str) -> tuple[int, str] | None:
        found = self.list_windows()
        if not found.ok:
            return None
        needle = title.lower().strip()
        windows = found.data.get("windows", [])
        for window in windows:  # exact match first
            if window["title"].lower() == needle:
                return window["id"], window["title"]
        for window in windows:  # then substring
            if needle in window["title"].lower():
                return window["id"], window["title"]
        return None

    def focus_window(self, title: str) -> ActionResult:
        match = self._find_window(title)
        if not match:
            return ActionResult.fail(f"No open window matches “{title}”.")
        hwnd, actual = match
        try:
            self.user32.ShowWindow(hwnd, 9)  # SW_RESTORE
            self.user32.SetForegroundWindow(hwnd)
        except Exception as exc:
            return ActionResult.fail(f"Could not focus {actual}: {human_error(exc)}")
        return ActionResult.done(f"Focused: {actual}", title=actual)

    def close_window(self, title: str) -> ActionResult:
        match = self._find_window(title)
        if not match:
            return ActionResult.fail(f"No open window matches “{title}”.")
        hwnd, actual = match
        try:
            self.user32.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE (graceful)
        except Exception as exc:
            return ActionResult.fail(f"Could not close {actual}: {human_error(exc)}")
        return ActionResult.done(f"Asked “{actual}” to close.", title=actual)

    def window_action(self, title: str, action: str) -> ActionResult:
        match = self._find_window(title)
        if not match:
            return ActionResult.fail(f"No open window matches “{title}”.")
        hwnd, actual = match
        # SW_* codes, then the keyboard shortcut Windows itself uses for the rest.
        codes = {"minimize": 6, "maximize": 3, "restore": 9, "hide": 0}
        shortcuts = {
            "snap_left": "win+left", "snap_right": "win+right",
            "maximize_vertically": "win+up", "minimize_all": "win+d",
            "center": "win+left|win+right",          # no direct API: left then widen
        }
        if action in codes:
            self.user32.ShowWindow(hwnd, codes[action])
            return ActionResult.done(f"{action.title()}d: {actual}", title=actual)
        if action == "fullscreen":
            self.user32.ShowWindow(hwnd, 3)                      # maximize first
            self.user32.SetForegroundWindow(hwnd)
            self.press_hotkey("f11")
            return ActionResult.done(f"Told “{actual}” to go fullscreen.", title=actual)
        if action == "always_on_top":
            # WS_EX_TOPMOST on a window that is already foreground
            self.user32.SetWindowPos(hwnd, -1, 0, 0, 0, 0, 0x0001 | 0x0002)  # HWND_TOPMOST
            return ActionResult.done(f"“{actual}” is now always on top.", title=actual)
        if action == "no_longer_on_top":
            self.user32.SetWindowPos(hwnd, -2, 0, 0, 0, 0, 0x0001 | 0x0002)  # HWND_NOTOPMOST
            return ActionResult.done(f"“{actual}” no longer floats above the rest.", title=actual)
        if action.startswith("switch_workspace:"):
            self.user32.SetForegroundWindow(hwnd)
            return self._press_workspace(action.split(":", 1)[1])
        if action.startswith("move_to_workspace:"):
            self.user32.SetForegroundWindow(hwnd)
            return self._move_to_workspace(action.split(":", 1)[1], actual, hwnd)
        if action in shortcuts:
            self.user32.SetForegroundWindow(hwnd)
            for combo in shortcuts[action].split("|"):
                self.press_hotkey(combo)
            return ActionResult.done(f"{action.replace('_', ' ').title()}: {actual}", title=actual)
        return ActionResult.fail(f"I don't know the window action “{action}”.")

    def _press_workspace(self, number: str) -> ActionResult:
        """Switch virtual desktop with the Ctrl+Win+←/→ shortcuts Windows exposes."""
        try:
            target = int(number)
        except ValueError:
            return ActionResult.fail(f"“{number}” is not a workspace number.")
        if target < 1 or target > 9:
            return ActionResult.fail("I can switch between workspaces 1 to 9.")
        for _ in range(target - 1):
            self.press_hotkey("ctrl+win+right")
        return ActionResult.done(f"Switched to workspace {target}.")
    def _move_to_workspace(self, number: str, actual: str, hwnd: int) -> ActionResult:
        """Move the focused window to another desktop (Ctrl+Win+Shift+←/→ … to the right one)."""
        try:
            target = int(number)
        except ValueError:
            return ActionResult.fail(f"“{number}” is not a workspace number.")
        if target < 1 or target > 9:
            return ActionResult.fail("I can move windows to workspaces 1 to 9.")
        for _ in range(target - 1):
            self.press_hotkey("ctrl+win+shift+right")
        return ActionResult.done(f"Moved “{actual}” to workspace {target}.", title=actual)

    # ── audio & display ──────────────────────────────────────────────────
    def volume(self, level: int | None = None, mute: str | None = None) -> ActionResult:
        interface = self._volume_interface()
        if interface is not None:
            try:
                if mute == "toggle":
                    current = bool(interface.GetMute())
                    interface.SetMute(not current, None)
                    return ActionResult.done(f"Mute {'off' if current else 'on'}.")
                if mute in ("on", "off"):
                    interface.SetMute(mute == "on", None)
                    return ActionResult.done(f"Mute {'on' if mute == 'on' else 'off'}.")
                if level is not None:
                    value = max(0, min(100, int(level)))
                    interface.SetMasterVolumeLevelScalar(value / 100.0, None)
                    return ActionResult.done(f"Volume set to {value}%.", level=value)
                current = round(float(interface.GetMasterVolumeLevelScalar()) * 100)
                muted = bool(interface.GetMute())
                return ActionResult.done(
                    f"Volume is {current}%{' (muted)' if muted else ''}.", level=current, muted=muted
                )
            except Exception as exc:
                return ActionResult.fail(f"Volume control failed: {human_error(exc)}")
        # No pycaw: approximate with the media keys (Windows steps ~2% per press).
        if level is not None:
            value = max(0, min(100, int(level)))
            for _ in range(50):                       # wind up to maximum first
                self._press_vk(0xAF)                  # VK_VOLUME_UP
            for _ in range(int((100 - value) / 2)):   # then come down to the target
                self._press_vk(0xAE)                  # VK_VOLUME_DOWN
            return ActionResult.done(
                f"Volume set to about {value}% using the media keys. "
                "Install pycaw (pip install pycaw) for exact levels.",
                level=value, approximate=True,
            )
        if mute == "toggle":
            self._press_vk(0xAD)                      # VK_VOLUME_MUTE
            return ActionResult.done("Toggled mute.")
        if mute in ("on", "off"):
            return ActionResult.fail(
                "Setting mute on or off needs pycaw: pip install pycaw",
                hint="Without it I can only toggle mute, not force a state.",
            )
        return ActionResult.fail(
            "I need pycaw to read the current volume: pip install pycaw",
            hint="Media keys can change the volume but cannot report it.",
        )

    def _press_vk(self, vk: int) -> None:
        self._send([self._key_event(vk)])
        self._send([self._key_event(vk, True)])

    #: Media transport keys (the same VK codes on every Windows since Vista).
    MEDIA_KEYS = {"play_pause": 0xB3, "play": 0xB3, "pause": 0xB3, "next": 0xB0,
                  "previous": 0xB1, "stop": 0xB2}

    def media(self, action: str = "play_pause") -> ActionResult:
        key = self.MEDIA_KEYS.get((action or "play_pause").lower())
        if key is None:
            return ActionResult.fail(f"I don't know the media action “{action}”.")
        label = {"play": "play/pause", "pause": "play/pause"}.get(
            action.lower(), action.replace("_", "/"))
        self._press_vk(key)
        return ActionResult.done(f"Pressed {label}.")

    def system_toggle(self, kind: str, state: bool | None = None) -> ActionResult:
        kind = (kind or "").lower()
        want_on = state is not False
        if kind == "dark_mode":
            value = "0" if want_on else "1"          # AppsUseLightTheme: 0 means dark
            script = ("$p = 'HKCU:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Themes\\Personalize'; "
                      f"Set-ItemProperty -Path $p -Name AppsUseLightTheme -Value {value}; "
                      f"Set-ItemProperty -Path $p -Name SystemUsesLightTheme -Value {value}")
        elif kind == "wifi":
            script = ('Get-NetAdapter -Physical | Where-Object { $_.Status -eq "Up" } | '
                      + ("Enable-NetAdapter" if want_on else "Disable-NetAdapter")
                      + " -Confirm:$false")
        elif kind == "bluetooth":
            return ActionResult.fail(
                "Windows has no reliable command line switch for Bluetooth.",
                hint="Use the Action Center, or Settings → Bluetooth & devices.")
        elif kind == "nightlight":
            return ActionResult.fail(
                "Windows 11 exposes Night light only through its own UI.",
                hint="Settings → System → Display → Night light.")
        elif kind == "power_saver":
            script = ("(Get-CimInstance -Namespace root\\cimv2\\power -ClassName "
                      "Win32_PowerPlan | Where-Object { $_.ElementName -eq '"
                      + ("Power saver" if want_on else "Balanced") + "' }).Activate()")
        else:
            return ActionResult.fail(f"I cannot switch “{kind}” on Windows.")
        ok, output = self._powershell(script)
        if not ok:
            return ActionResult.fail(f"Could not change {kind}: {output}")
        return ActionResult.done(f"{kind.replace('_', ' ').title()} turned "
                                 f"{'on' if want_on else 'off'}.")

    def _volume_interface(self):
        try:
            from pycaw.pycaw import AudioUtilities

            speakers = AudioUtilities.GetSpeakers()
            if hasattr(speakers, "EndpointVolume"):
                return speakers.EndpointVolume
            from ctypes import POINTER, cast

            from comtypes import CLSCTX_ALL
            from pycaw.pycaw import IAudioEndpointVolume

            interface = speakers.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
            return cast(interface, POINTER(IAudioEndpointVolume))
        except Exception:
            return None

    def _powershell(self, script: str, timeout: float = 15.0) -> tuple[bool, str]:
        try:
            completed = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                capture_output=True, text=True, timeout=timeout,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            return completed.returncode == 0, (completed.stdout or completed.stderr).strip()
        except Exception as exc:
            return False, human_error(exc)

    def brightness(self, level: int | None = None) -> ActionResult:
        if level is None:
            ok, output = self._powershell(
                "(Get-CimInstance -Namespace root/WMI -ClassName WmiMonitorBrightness).CurrentBrightness"
            )
            if ok and output.strip().isdigit():
                return ActionResult.done(f"Brightness is {output.strip()}%.", level=int(output.strip()))
            return ActionResult.fail(
                "Could not read the brightness (common on desktops with external monitors).",
                hint="Laptop internal displays support this; external monitors need their own tool.",
            )
        value = max(5, min(100, int(level)))
        ok, output = self._powershell(
            "$m = Get-CimInstance -Namespace root/WMI -ClassName WmiMonitorBrightnessMethods; "
            f"$m.WmiSetBrightness(1, {value})"
        )
        if ok:
            return ActionResult.done(f"Brightness set to {value}%.", level=value)
        return ActionResult.fail(f"Could not set the brightness: {output[:200]}")

    # ── processes / shell / screen ───────────────────────────────────────
    def list_processes(self, limit: int = 12, sort_by: str = "cpu") -> ActionResult:
        return _processes_list(limit, sort_by)

    def kill_process(self, target: str, force: bool = False) -> ActionResult:
        return _kill_process(target, force)

    def start_process(self, command: str, args: list[str] | None = None,
                      cwd: str | None = None) -> ActionResult:
        return _start_process(command, args, cwd)

    def run_shell(self, command: str, timeout: float = 20.0, cwd: str | None = None) -> ActionResult:
        return _run_shell(command, timeout, cwd)

    def screen_text(self) -> ActionResult:
        return _screen_text_result()

    # ── capabilities ─────────────────────────────────────────────────────
    def capabilities(self) -> dict[str, Capability]:
        volume_ok = self._volume_interface() is not None
        return {
            "keyboard": Capability("keyboard", True, "typing + hotkeys via SendInput"),
            "mouse": Capability("mouse", True, "move, click, scroll via user32"),
            "windows": Capability("windows", True, "list, focus, minimise, close"),
            "volume": Capability("volume", True,
                                 "precise control (pycaw)" if volume_ok
                                 else "media keys only (2% steps)",
                                 "" if volume_ok else "pip install pycaw for exact levels"),
            "brightness": Capability("brightness", True, "WMI (internal displays)"),
            "processes": Capability("processes", _has_psutil(), "psutil",
                                    "" if _has_psutil() else "pip install psutil"),
            "shell": Capability("shell", True, "cmd.exe (every command is confirmed)"),
            "screen_text": Capability("screen_text", True, "OCR via Tesseract",
                                      "pip install mss pillow pytesseract + Tesseract"),
            "media_keys": Capability("media_keys", True, "play/pause + track keys"),
            "system_switches": Capability(
                "system_switches", True, "Wi-Fi, dark mode, power plan (Bluetooth: Control Centre)"),
            "drag": Capability("drag", True, "press, move, release"),
        }


# ════════════════════════════════════════════════════════════════════════════
#  macOS
# ════════════════════════════════════════════════════════════════════════════

class MacBackend:
    """AppleScript-driven control of macOS."""

    name = "macos"

    def _osascript(self, script: str, timeout: float = 15.0) -> tuple[bool, str]:
        try:
            completed = subprocess.run(["osascript", "-e", script], capture_output=True,
                                       text=True, timeout=timeout)
            output = (completed.stdout or "").strip()
            error = (completed.stderr or "").strip()
            if completed.returncode == 0:
                return True, output
            return False, error or output or f"exit code {completed.returncode}"
        except Exception as exc:
            return False, human_error(exc)

    @staticmethod
    def _escape(text: str) -> str:
        return text.replace("\\", "\\\\").replace('"', '\\"')

    def type_text(self, text: str, interval: float = 0.0) -> ActionResult:
        ok, output = self._osascript(
            f'tell application "System Events" to keystroke "{self._escape(text)}"'
        )
        if not ok:
            return ActionResult.fail(
                f"Typing failed: {output}",
                hint="Grant Accessibility permission: System Settings → Privacy & Security → "
                     "Accessibility → enable your terminal / JARVIS.",
            )
        preview = text if len(text) <= 60 else text[:57] + "…"
        return ActionResult.done(f"Typed {len(text)} characters: {preview!r}")

    def press_hotkey(self, combination: str) -> ActionResult:
        parsed = parse_hotkey(combination)
        if not parsed:
            return ActionResult.fail(f"I don't know the key combination “{combination}”.")
        modifiers, key = parsed
        using = "{" + ", ".join(MODIFIER_CODES[m]["mac"] for m in modifiers) + "}" if modifiers else ""
        if key in KEYS and KEYS[key]["mac"] is not None:
            script = f'tell application "System Events" to key code {KEYS[key]["mac"]}'
            if using:
                script += f" using {using}"
        elif len(key) == 1:
            script = f'tell application "System Events" to keystroke "{key}"'
            if using:
                script += f" using {using}"
        else:
            return ActionResult.fail(f"I don't know the key “{key}” on macOS.")
        ok, output = self._osascript(script)
        if not ok:
            return ActionResult.fail(f"Key press failed: {output}",
                                     hint="Enable Accessibility permission for your terminal.")
        return ActionResult.done(f"Pressed {combination}.")

    def mouse_move(self, x: int, y: int, relative: bool = False) -> ActionResult:
        cliclick = which("cliclick")
        if not cliclick:
            return ActionResult.fail("Mouse control on macOS needs cliclick: brew install cliclick")
        if relative:
            # cliclick accepts signed offsets: m:+40,-20
            target = f"m:{x:+d},{y:+d}"
        else:
            target = f"m:{int(x)},{int(y)}"
        try:
            subprocess.run([cliclick, target], check=True, timeout=10,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as exc:
            return ActionResult.fail(f"Mouse move failed: {human_error(exc)}")
        return ActionResult.done(f"Moved the pointer to {x},{y}" + (" (relative)" if relative else ""))

    def mouse_click(self, button: str = "left", clicks: int = 1) -> ActionResult:
        cliclick = which("cliclick")
        if not cliclick:
            return ActionResult.fail("Mouse clicks on macOS need cliclick: brew install cliclick")
        command = {"left": "c", "right": "rc", "middle": "c"}.get(button.lower(), "c")
        try:
            for _ in range(max(1, min(int(clicks), 3))):
                subprocess.run([cliclick, command], check=True, timeout=10,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as exc:
            return ActionResult.fail(f"Click failed: {human_error(exc)}")
        return ActionResult.done(f"{button.title()}-clicked {clicks}x.")

    def mouse_scroll(self, amount: int) -> ActionResult:
        cliclick = which("cliclick")
        if not cliclick:
            return ActionResult.fail("Scrolling on macOS needs cliclick: brew install cliclick")
        direction = "d" if amount < 0 else "u"
        try:
            subprocess.run([cliclick, f"{direction}:{abs(int(amount)) * 3}"], check=True,
                           timeout=10, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as exc:
            return ActionResult.fail(f"Scroll failed: {human_error(exc)}")
        return ActionResult.done(f"Scrolled {abs(int(amount))} notches.")

    def mouse_position(self) -> ActionResult:
        ok, output = self._osascript(
            'tell application "System Events" to get position of mouse'
        )
        if not ok:
            return ActionResult.fail(f"Could not read the pointer: {output}")
        try:
            x, y = [int(float(part)) for part in output.replace(" ", "").split(",")[:2]]
        except Exception:
            return ActionResult.fail(f"Unexpected position reply: {output}")
        return ActionResult.done(f"Pointer at {x},{y}", x=x, y=y)

    def list_windows(self) -> ActionResult:
        ok, output = self._osascript(
            'tell application "System Events" to get name of every process whose background only is false'
        )
        if not ok:
            return ActionResult.fail(f"Could not list windows: {output}",
                                     hint="Grant Accessibility permission for your terminal.")
        names = [name.strip() for name in output.split(",") if name.strip()]
        windows = [{"title": name, "id": name} for name in names]
        if not windows:
            return ActionResult.done("No visible applications.", windows=[])
        lines = [f"{len(windows)} applications with windows:"] + [f"  {w['title']}" for w in windows[:25]]
        return ActionResult.done("\n".join(lines), windows=windows)

    def active_window(self) -> ActionResult:
        ok, output = self._osascript(
            'tell application "System Events" to get name of first process whose frontmost is true'
        )
        if not ok:
            return ActionResult.fail(f"Could not read the active app: {output}")
        return ActionResult.done(f"Active application: {output}", title=output)

    def focus_window(self, title: str) -> ActionResult:
        ok, output = self._osascript(
            f'tell application "System Events" to set frontmost of process "{self._escape(title)}" to true'
        )
        if not ok:
            return ActionResult.fail(f"Could not focus “{title}”: {output}")
        return ActionResult.done(f"Focused: {title}", title=title)

    def close_window(self, title: str) -> ActionResult:
        focus = self.focus_window(title)
        if not focus.ok:
            return focus
        ok, output = self._osascript(
            'tell application "System Events" to keystroke "w" using command down'
        )
        if not ok:
            return ActionResult.fail(f"Could not close “{title}”: {output}")
        return ActionResult.done(f"Closed the window of {title}.")

    def window_action(self, title: str, action: str) -> ActionResult:
        focus = self.focus_window(title)
        if not focus.ok:
            return focus
        if action == "minimize":
            ok, output = self._osascript(
                'tell application "System Events" to keystroke "m" using command down'
            )
        elif action in ("maximize", "restore", "fullscreen"):
            ok, output = self._osascript(
                'tell application "System Events" to keystroke "f" using {control down, command down}'
            )
        elif action in ("snap_left", "snap_right"):
            # macOS has no built-in split screen shortcut: place the window with AX.
            side = "0" if action == "snap_left" else "(the width of window 1 of application process "
            script = (
                'tell application "System Events"\n'
                '  tell (first application process whose frontmost is true)\n'
                f'    set position of window 1 to {{{side}, 0}}\n'
                '  end tell\n'
                'end tell'
            )
            ok, output = self._osascript(script)
        elif action.startswith("switch_workspace:"):
            number = action.split(":", 1)[1].strip()
            ok, output = self._osascript(
                'tell application "System Events" to key code '
                f'{17 + (int(number) - 1 if number.isdigit() else 0)} using control down'
            )
        elif action.startswith("move_to_workspace:"):
            return ActionResult.fail(
                "Moving a window to another Space is not scriptable on macOS.",
                hint="Drag it to the next desktop, or use Mission Control.")
        elif action == "always_on_top":
            return ActionResult.fail(
                "Keeping a window on top is per-app on macOS.",
                hint="Most apps have their own “Always on top” option, e.g. in the Window menu.")
        else:
            return ActionResult.fail(f"I don't know the window action “{action}” on macOS.")
        if not ok:
            return ActionResult.fail(f"{action} failed: {output}")
        return ActionResult.done(f"{action.replace('_', ' ').title()}d: {title}.")

    # ── media & system switches ──────────────────────────────────────────
    def _media_key(self, code: int, label: str) -> ActionResult:
        ok, output = self._osascript(f'tell application "System Events" to key code {code}')
        return ActionResult.done(f"Pressed {label}.") if ok \
            else ActionResult.fail(f"The media key did not work: {output}")

    def media(self, action: str = "play_pause") -> ActionResult:
        codes = {"play_pause": 16, "play": 16, "pause": 16, "next": 17, "previous": 18}
        key = (action or "play_pause").lower()
        code = codes.get(key)
        if code is None:
            return ActionResult.fail(f"I don't know the media action “{action}”.")
        label = {"play": "play/pause", "pause": "play/pause"}.get(key, key)
        return self._media_key(code, label)

    def system_toggle(self, kind: str, state: bool | None = None) -> ActionResult:
        kind = (kind or "").lower()
        want_on = state is not False
        if kind == "dnd":
            # Focus mode lives in Control Centre; the menu bar shortcut is the honest option.
            ok, output = self._osascript(
                'tell application "System Events" to key code 45 using {command down, shift down}',
            )
            if ok:
                return ActionResult.done("Toggled Do Not Disturb / Focus.")
            return ActionResult.fail(f"Could not toggle Focus: {output}")
        if kind == "dark_mode":
            ok, output = self._osascript([
                'tell application "System Events"',
                '  tell appearance preferences',
                f'    set dark mode to {"true" if want_on else "false"}',
                '  end tell',
                'end tell',
            ])
            return ActionResult.done(f"Dark mode {'on' if want_on else 'off'}.") if ok \
                else ActionResult.fail(f"Could not change the appearance: {output}")
        if kind == "nightlight":
            return ActionResult.fail("macOS Night Shift is not scriptable here.",
                                     hint="System Settings → Displays → Night Shift.")
        if kind == "bluetooth":
            return ActionResult.fail("Turning Bluetooth off on macOS needs the menubar or a signed tool.",
                                     hint="Control Centre → Bluetooth, or `blueutil` if you install it.")
        if kind == "wifi":
            ok, output = self._osascript(
                'do shell script "/usr/sbin/networksetup -setairportpower en0 '
                + ("on" if want_on else "off") + '"')
            return ActionResult.done(f"Wi-Fi turned {'on' if want_on else 'off'}.") if ok \
                else ActionResult.fail(f"Could not change Wi-Fi: {output}")
        return ActionResult.fail(f"I cannot switch “{kind}” on macOS.")

    def volume(self, level: int | None = None, mute: str | None = None) -> ActionResult:
        if mute == "toggle":
            ok, output = self._osascript(
                'set s to (get volume settings)\n'
                'if output muted of s then\n set volume without output muted\nelse\n'
                ' set volume with output muted\nend if'
            )
            return ActionResult.done("Toggled mute.") if ok else ActionResult.fail(output)
        if mute in ("on", "off"):
            script = "set volume with output muted" if mute == "on" else "set volume without output muted"
            ok, output = self._osascript(script)
            return ActionResult.done(f"Mute {mute}.") if ok else ActionResult.fail(output)
        if level is not None:
            value = max(0, min(100, int(level)))
            ok, output = self._osascript(f"set volume output volume {value}")
            return ActionResult.done(f"Volume set to {value}%.", level=value) if ok \
                else ActionResult.fail(f"Volume control failed: {output}")
        ok, output = self._osascript("output volume of (get volume settings)")
        if ok and output.strip().isdigit():
            return ActionResult.done(f"Volume is {output.strip()}%.", level=int(output.strip()))
        return ActionResult.fail(f"Could not read the volume: {output}")

    def brightness(self, level: int | None = None) -> ActionResult:
        tool = which("brightness")
        if not tool:
            return ActionResult.fail("Brightness control on macOS needs the `brightness` CLI: "
                                     "brew install brightness")
        if level is None:
            try:
                output = subprocess.run([tool, "-l"], capture_output=True, text=True, timeout=8).stdout
                for line in output.splitlines():
                    if "brightness" in line.lower():
                        value = float(line.split()[-1])
                        return ActionResult.done(f"Brightness is {round(value * 100)}%.",
                                                 level=round(value * 100))
            except Exception as exc:
                return ActionResult.fail(f"Could not read the brightness: {human_error(exc)}")
            return ActionResult.fail("Could not read the brightness.")
        try:
            subprocess.run([tool, f"{max(0, min(100, int(level))) / 100:.2f}"], check=True, timeout=8)
        except Exception as exc:
            return ActionResult.fail(f"Could not set the brightness: {human_error(exc)}")
        return ActionResult.done(f"Brightness set to {level}%.", level=int(level))

    def list_processes(self, limit: int = 12, sort_by: str = "cpu") -> ActionResult:
        return _processes_list(limit, sort_by)

    def kill_process(self, target: str, force: bool = False) -> ActionResult:
        return _kill_process(target, force)

    def start_process(self, command: str, args: list[str] | None = None,
                      cwd: str | None = None) -> ActionResult:
        return _start_process(command, args, cwd)

    def run_shell(self, command: str, timeout: float = 20.0, cwd: str | None = None) -> ActionResult:
        return _run_shell(command, timeout, cwd)

    def screen_text(self) -> ActionResult:
        return _screen_text_result()

    def capabilities(self) -> dict[str, Capability]:
        cliclick = bool(which("cliclick"))
        return {
            "keyboard": Capability("keyboard", True, "AppleScript keystrokes (needs Accessibility)"),
            "mouse": Capability("mouse", cliclick, "cliclick" if cliclick else "needs cliclick",
                                "" if cliclick else "brew install cliclick"),
            "windows": Capability("windows", True, "System Events"),
            "volume": Capability("volume", True, "osascript"),
            "brightness": Capability("brightness", bool(which("brightness")), "brightness CLI",
                                     "" if which("brightness") else "brew install brightness"),
            "processes": Capability("processes", _has_psutil(), "psutil",
                                    "" if _has_psutil() else "pip install psutil"),
            "shell": Capability("shell", True, "zsh (every command is confirmed)"),
            "media_keys": Capability("media_keys", True, "transport keys"),
            "system_switches": Capability("system_switches", True, "Dark mode, Wi-Fi, Focus"),
            "drag": Capability("drag", True, "position + size via System Events"),
            "screen_text": Capability("screen_text", True, "OCR via Tesseract",
                                      "pip install mss pillow pytesseract + brew install tesseract"),
        }


# ════════════════════════════════════════════════════════════════════════════
#  Linux
# ════════════════════════════════════════════════════════════════════════════

class LinuxBackend:
    """X11 control via xdotool / wmctrl, audio via pactl or amixer."""

    name = "linux"

    def __init__(self) -> None:
        self.xdotool = which("xdotool")
        self.wmctrl = which("wmctrl")
        self.pactl = which("pactl")
        self.amixer = which("amixer")
        self.brightnessctl = which("brightnessctl")
        self.xrandr = which("xrandr")
        self.playerctl = which("playerctl")
        self.nmcli = which("nmcli")
        self.powerprofilesctl = which("powerprofilesctl")
        self.display = os.environ.get("DISPLAY")
        self.wayland = bool(os.environ.get("WAYLAND_DISPLAY"))
        self.session = os.environ.get("XDG_SESSION_TYPE", "").lower()

    # ── helpers ──────────────────────────────────────────────────────────
    def _run(self, argv: list[str], timeout: float = 10.0) -> tuple[bool, str]:
        try:
            completed = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
            output = (completed.stdout or "").strip()
            error = (completed.stderr or "").strip()
            if completed.returncode == 0:
                return True, output
            return False, error or output or f"exit code {completed.returncode}"
        except Exception as exc:
            return False, human_error(exc)

    def _needs_x11(self, what: str) -> ActionResult:
        if not self.display:
            hint = ("You are on Wayland — xdotool needs X11 or XWayland. "
                    "Try `ydotool` (needs root) or run an X11 session."
                    if self.wayland or self.session == "wayland" else
                    "No DISPLAY variable found — is this a desktop session?")
            return ActionResult.fail(f"{what} needs an X11 display, which I cannot see.", hint=hint)
        return ActionResult.fail(f"{what} needs xdotool: sudo apt install xdotool",
                                 hint="Install xdotool (and wmctrl for window control).")

    # ── keyboard ─────────────────────────────────────────────────────────
    def type_text(self, text: str, interval: float = 0.0) -> ActionResult:
        if not self.xdotool:
            return self._needs_x11("Typing")
        delay = int(max(0.0, interval) * 1000) or 8
        ok, output = self._run([self.xdotool, "type", "--clearmodifiers", "--delay", str(delay), "--", text])
        if not ok:
            return ActionResult.fail(f"Typing failed: {output}")
        preview = text if len(text) <= 60 else text[:57] + "…"
        return ActionResult.done(f"Typed {len(text)} characters: {preview!r}")

    def press_hotkey(self, combination: str) -> ActionResult:
        if not self.xdotool:
            return self._needs_x11("Key presses")
        parsed = parse_hotkey(combination)
        if not parsed:
            return ActionResult.fail(f"I don't know the key combination “{combination}”.")
        modifiers, key = parsed
        names = [MODIFIER_CODES[m]["xdo"] for m in modifiers]
        if key in KEYS:
            names.append(str(KEYS[key]["xdo"]))
        elif len(key) == 1:
            names.append(key)
        else:
            return ActionResult.fail(f"I don't know the key “{key}”.")
        ok, output = self._run([self.xdotool, "key", "--clearmodifiers", "+".join(names)])
        if not ok:
            return ActionResult.fail(f"Key press failed: {output}")
        return ActionResult.done(f"Pressed {combination}.")

    # ── mouse ────────────────────────────────────────────────────────────
    def mouse_move(self, x: int, y: int, relative: bool = False) -> ActionResult:
        if not self.xdotool:
            return self._needs_x11("Mouse control")
        argv = [self.xdotool, "mousemove_relative", "--", str(x), str(y)] if relative else \
               [self.xdotool, "mousemove", str(x), str(y)]
        ok, output = self._run(argv)
        return ActionResult.done(f"Moved the pointer to {x},{y}") if ok \
            else ActionResult.fail(f"Mouse move failed: {output}")

    def mouse_click(self, button: str = "left", clicks: int = 1) -> ActionResult:
        if not self.xdotool:
            return self._needs_x11("Mouse clicks")
        code = {"left": "1", "middle": "2", "right": "3"}.get(button.lower())
        if not code:
            return ActionResult.fail(f"Unknown mouse button “{button}”.")
        argv = [self.xdotool, "click", "--repeat", str(max(1, min(int(clicks), 3))), code]
        ok, output = self._run(argv)
        return ActionResult.done(f"{button.title()}-clicked {clicks}x.") if ok \
            else ActionResult.fail(f"Click failed: {output}")

    def mouse_scroll(self, amount: int) -> ActionResult:
        if not self.xdotool:
            return self._needs_x11("Scrolling")
        button = "5" if amount < 0 else "4"
        argv = [self.xdotool, "click", "--repeat", str(max(1, min(abs(int(amount)), 40))), button]
        ok, output = self._run(argv)
        return ActionResult.done(f"Scrolled {abs(int(amount))} notches.") if ok \
            else ActionResult.fail(f"Scroll failed: {output}")

    def mouse_drag(self, x1: int, y1: int, x2: int, y2: int, duration: float = 0.4) -> ActionResult:
        if not self.xdotool:
            return self._needs_x11("Dragging")
        steps = max(4, min(30, int(duration * 30)))
        sequence = [
            [self.xdotool, "mousemove", str(x1), str(y1)],
            [self.xdotool, "mousedown", "1"],
        ]
        for index in range(1, steps + 1):
            sequence.append([self.xdotool, "mousemove",
                             str(int(x1 + (x2 - x1) * index / steps)),
                             str(int(y1 + (y2 - y1) * index / steps))])
        sequence.append([self.xdotool, "mouseup", "1"])
        for argv in sequence:
            ok, output = self._run(argv)
            if not ok:
                self._run([self.xdotool, "mouseup", "1"])     # never leave the button held
                return ActionResult.fail(f"Drag failed: {output}")
        return ActionResult.done(f"Dragged from {x1},{y1} to {x2},{y2}.")

    def mouse_position(self) -> ActionResult:
        if not self.xdotool:
            return self._needs_x11("Mouse position")
        ok, output = self._run([self.xdotool, "getmouselocation", "--shell"])
        if not ok:
            return ActionResult.fail(f"Could not read the pointer: {output}")
        values: dict[str, str] = {}
        for line in output.splitlines():
            if "=" in line:
                key, _, value = line.partition("=")
                values[key.strip()] = value.strip()
        try:
            x, y = int(values["X"]), int(values["Y"])
        except Exception:
            return ActionResult.fail(f"Unexpected pointer reply: {output}")
        return ActionResult.done(f"Pointer at {x},{y}", x=x, y=y)

    # ── windows ──────────────────────────────────────────────────────────
    def _wmctrl_rows(self) -> list[dict[str, Any]]:
        if not (self.wmctrl and self.display):
            return []
        ok, output = self._run([self.wmctrl, "-lp"])
        if not ok:
            return []
        rows: list[dict[str, Any]] = []
        for line in output.splitlines():
            parts = line.split(None, 4)
            if len(parts) < 5:
                continue
            rows.append({"id": parts[0], "pid": parts[2], "title": parts[4].strip()})
        return rows

    def list_windows(self) -> ActionResult:
        if self.wmctrl:
            rows = self._wmctrl_rows()
            if rows:
                lines = [f"{len(rows)} open windows:"]
                lines += [f"  {row['title'][:70]} (id {row['id']})" for row in rows[:25]]
                return ActionResult.done("\n".join(lines), windows=rows)
        if self.xdotool:
            ok, output = self._run([self.xdotool, "search", "--onlyvisible", "--name", "."])
            if ok:
                titles: list[dict[str, Any]] = []
                for window_id in output.split()[:25]:
                    good, name = self._run([self.xdotool, "getwindowname", window_id])
                    if good and name:
                        titles.append({"id": window_id, "title": name})
                if titles:
                    lines = [f"{len(titles)} windows (xdotool):"]
                    lines += [f"  {row['title'][:70]}" for row in titles]
                    return ActionResult.done("\n".join(lines), windows=titles)
        return self._needs_x11("Window listing")

    def active_window(self) -> ActionResult:
        if self.xdotool:
            ok, output = self._run([self.xdotool, "getactivewindow", "getwindowname"])
            if ok and output:
                return ActionResult.done(f"Active window: {output}", title=output)
        if self.wmctrl:
            ok, output = self._run(["xprop", "-root", "_NET_ACTIVE_WINDOW"])
            if ok:
                return ActionResult.done(f"Active window id: {output.split()[-1]}")
        return self._needs_x11("Reading the active window")

    def _match_window(self, title: str) -> dict[str, Any] | None:
        needle = title.lower().strip()
        rows = self._wmctrl_rows()
        if self.xdotool and not rows:
            found = self.list_windows()
            rows = found.data.get("windows", []) if found.ok else []
        for row in rows:
            if row["title"].lower() == needle:
                return row
        for row in rows:
            if needle in row["title"].lower():
                return row
        return None

    def focus_window(self, title: str) -> ActionResult:
        row = self._match_window(title)
        if not row:
            return ActionResult.fail(f"No open window matches “{title}”.")
        for argv in ([self.wmctrl, "-i", "-a", row["id"]] if self.wmctrl and row["id"].startswith("0x")
                     else [self.xdotool, "windowactivate", "--sync", str(row["id"])]):
            if argv and argv[0]:
                ok, _output = self._run(argv)
                if ok:
                    return ActionResult.done(f"Focused: {row['title']}")
        return ActionResult.fail(f"Could not focus “{row['title']}”.")

    def close_window(self, title: str) -> ActionResult:
        row = self._match_window(title)
        if not row:
            return ActionResult.fail(f"No open window matches “{title}”.")
        if self.wmctrl and row["id"].startswith("0x"):
            ok, output = self._run([self.wmctrl, "-i", "-c", row["id"]])
            if ok:
                return ActionResult.done(f"Asked “{row['title']}” to close.")
            return ActionResult.fail(f"Could not close it: {output}")
        ok, output = self._run([self.xdotool, "windowkill", str(row["id"])])
        return ActionResult.done(f"Closed “{row['title']}”.") if ok \
            else ActionResult.fail(f"Could not close it: {output}")

    def window_action(self, title: str, action: str) -> ActionResult:
        row = self._match_window(title)
        if not row:
            return ActionResult.fail(f"No open window matches “{title}”.")
        if action == "minimize" and self.xdotool:
            ok, output = self._run([self.xdotool, "windowminimize", str(row["id"])])
        elif action in ("maximize", "restore") and self.wmctrl:
            flag = "add" if action == "maximize" else "remove"
            ok, output = self._run([self.wmctrl, "-i", "-r", row["id"], "-b",
                                    f"{flag},maximized_vert,maximized_horz"])
        elif action == "fullscreen" and self.wmctrl:
            ok, output = self._run([self.wmctrl, "-i", "-r", row["id"], "-b", "add,fullscreen"])
        elif action == "no_longer_fullscreen" and self.wmctrl:
            ok, output = self._run([self.wmctrl, "-i", "-r", row["id"], "-b", "remove,fullscreen"])
        elif action == "always_on_top" and self.wmctrl:
            ok, output = self._run([self.wmctrl, "-i", "-r", row["id"], "-b", "add,above"])
        elif action == "no_longer_on_top" and self.wmctrl:
            ok, output = self._run([self.wmctrl, "-i", "-r", row["id"], "-b", "remove,above"])
        elif action.startswith("switch_workspace:") and self.wmctrl:
            number = action.split(":", 1)[1].strip()
            ok, output = self._run([self.wmctrl, "-s", number])
            return ActionResult.done(f"Switched to workspace {number}.") if ok \
                else ActionResult.fail(output)
        elif action.startswith("move_to_workspace:") and self.wmctrl:
            number = action.split(":", 1)[1].strip()
            ok, output = self._run([self.wmctrl, "-i", "-r", row["id"], "-t", number])
            return ActionResult.done(f"Moved “{row['title']}” to workspace {number}.") if ok \
                else ActionResult.fail(output)
        elif action in ("snap_left", "snap_right", "center") and self._super_key():
            keys = {"snap_left": "super+left", "snap_right": "super+right", "center": None}[action]
            if keys is None:
                return ActionResult.fail("Centring windows needs a window manager shortcut.")
            self.focus_window(row["title"])
            return self.press_hotkey(keys)
        else:
            return ActionResult.fail(f"I cannot {action} windows with the tools installed here.",
                                     hint="Install wmctrl and xdotool for window placement.")
        return ActionResult.done(f"{action.replace('_', ' ').title()}d: {row['title']}") if ok \
            else ActionResult.fail(f"{action} failed: {output}")

    def _super_key(self) -> bool:
        return bool(self.xdotool)

    # ── media & system switches ──────────────────────────────────────────
    MEDIA_KEYS = {"play_pause": "XF86AudioPlay", "play": "XF86AudioPlay", "pause": "XF86AudioPause",
                  "next": "XF86AudioNext", "previous": "XF86AudioPrev", "stop": "XF86AudioStop"}

    def media(self, action: str = "play_pause") -> ActionResult:
        key = (action or "play_pause").lower()
        if key == "play_pause" and self.playerctl:
            ok, output = self._run([self.playerctl, "play-pause"])
            if ok:
                return ActionResult.done("Toggled playback.")
        if key in ("next", "previous") and self.playerctl:
            ok, output = self._run([self.playerctl, f"{key}"] if key == "next" else [self.playerctl, "previous"])
            if ok:
                return ActionResult.done(f"Went to the {key} track.")
        name = self.MEDIA_KEYS.get(key)
        if name is None:
            return ActionResult.fail(f"I don't know the media action “{action}”.")
        if not self.xdotool:
            return ActionResult.fail(
                "Media keys need xdotool (XDG) or playerctl here.",
                hint="sudo apt install xdotool playerctl")
        ok, output = self._run([self.xdotool, "key", "--clearmodifiers", name])
        return ActionResult.done(f"Pressed {key.replace('_', '/')}.") if ok \
            else ActionResult.fail(f"The media key did not work: {output}")

    def system_toggle(self, kind: str, state: bool | None = None) -> ActionResult:
        kind = (kind or "").lower()
        want_on = state is not False
        on_off = "on" if want_on else "off"
        if kind == "wifi":
            if self.nmcli:
                ok, output = self._run([self.nmcli, "radio", "wifi", on_off])
                return ActionResult.done(f"Wi-Fi turned {on_off}.") if ok \
                    else ActionResult.fail(f"Could not change Wi-Fi: {output}")
            if which("rfkill"):
                return self._run_result(["rfkill", "unblock" if want_on else "block", "wifi"],
                                        f"Wi-Fi {on_off}", "Wi-Fi")
            return ActionResult.fail("Wi-Fi switching needs NetworkManager (nmcli) or rfkill.")
        if kind == "bluetooth":
            if which("rfkill"):
                return self._run_result(["rfkill", "unblock" if want_on else "block", "bluetooth"],
                                        f"Bluetooth {on_off}", "Bluetooth")
            if which("bluetoothctl"):
                return self._run_result(["bluetoothctl", "power", on_off],
                                        f"Bluetooth {on_off}", "Bluetooth")
            return ActionResult.fail("Bluetooth switching needs rfkill or bluetoothctl.")
        if kind == "dark_mode":
            if which("gsettings"):
                scheme = "prefer-dark" if want_on else "prefer-light"
                return self._run_result(
                    ["gsettings", "set", "org.gnome.desktop.interface", "color-scheme", scheme],
                    f"Dark mode {on_off}", "Dark mode")
            if which("xfconf-query"):
                return self._run_result(
                    ["xfconf-query", "-c", "xsettings", "-p", "/Net/ThemeName", "-s",
                     "Adwaita-dark" if want_on else "Adwaita"],
                    f"Dark mode {on_off}", "Dark mode")
            return ActionResult.fail("Dark mode switching needs gsettings (GNOME) or xfconf (XFCE).")
        if kind == "nightlight":
            if which("gsettings"):
                value = "true" if want_on else "false"
                return self._run_result(
                    ["gsettings", "set", "org.gnome.settings-daemon.plugins.color", "night-light-enabled",
                     value], f"Night light {on_off}", "Night light")
            return ActionResult.fail("Night light switching needs GNOME's gsettings.")
        if kind == "power_saver":
            if self.powerprofilesctl:
                profile = "power-saver" if want_on else "balanced"
                ok, output = self._run([self.powerprofilesctl, "set", profile])
                return ActionResult.done(f"Power profile: {profile}.") if ok \
                    else ActionResult.fail(f"Could not change the power profile: {output}")
            if which("tlp"):
                return self._run_result(["tlp", "bat" if want_on else "ac"],
                                        "Power profile set.", "Power profile")
            return ActionResult.fail("Power profiles need power-profiles-daemon or tlp.")
        if kind == "dnd":
            return ActionResult.fail(
                "Do Not Disturb is desktop-specific on Linux.",
                hint="GNOME: Settings → Notifications; KDE: the notification applet.")
        return ActionResult.fail(f"I cannot switch “{kind}” on Linux.")

    def _run_result(self, argv: list[str], success: str, what: str) -> ActionResult:
        ok, output = self._run(argv)
        return ActionResult.done(success) if ok else ActionResult.fail(f"{what} failed: {output}")

    # ── audio & display ──────────────────────────────────────────────────
    def volume(self, level: int | None = None, mute: str | None = None) -> ActionResult:
        if self.pactl:
            if mute == "toggle":
                ok, output = self._run([self.pactl, "set-sink-mute", "@DEFAULT_SINK@", "toggle"])
                return ActionResult.done("Toggled mute.") if ok else ActionResult.fail(output)
            if mute in ("on", "off"):
                ok, output = self._run([self.pactl, "set-sink-mute", "@DEFAULT_SINK@",
                                        "1" if mute == "on" else "0"])
                return ActionResult.done(f"Mute {mute}.") if ok else ActionResult.fail(output)
            if level is not None:
                value = max(0, min(100, int(level)))
                ok, output = self._run([self.pactl, "set-sink-volume", "@DEFAULT_SINK@", f"{value}%"])
                return ActionResult.done(f"Volume set to {value}%.", level=value) if ok \
                    else ActionResult.fail(f"Volume control failed: {output}")
            ok, output = self._run([self.pactl, "get-sink-volume", "@DEFAULT_SINK@"])
            if ok and "%" in output:
                percent = output.split("/")[0].split()[-1]
                muted = self._run([self.pactl, "get-sink-mute", "@DEFAULT_SINK@"])[1]
                return ActionResult.done(f"Volume is {percent}{' (muted)' if 'yes' in muted else ''}.")
        if self.amixer:
            if mute == "toggle":
                self._run([self.amixer, "set", "Master", "toggle"])
                return ActionResult.done("Toggled mute.")
            if level is not None:
                value = max(0, min(100, int(level)))
                ok, output = self._run([self.amixer, "-q", "set", "Master", f"{value}%"])
                return ActionResult.done(f"Volume set to {value}%.") if ok \
                    else ActionResult.fail(f"Volume control failed: {output}")
            ok, output = self._run([self.amixer, "get", "Master"])
            if ok and "%" in output:
                percent = output.split("[")[1].split("%")[0]
                return ActionResult.done(f"Volume is {percent}%.")
        return ActionResult.fail("Volume control needs pactl or amixer: "
                                 "sudo apt install pulseaudio-utils alsa-utils")

    def brightness(self, level: int | None = None) -> ActionResult:
        if self.brightnessctl:
            if level is None:
                ok, current = self._run([self.brightnessctl, "get"])
                good, maximum = self._run([self.brightnessctl, "max"])
                if ok and good and maximum.isdigit() and int(maximum):
                    percent = round(int(current) / int(maximum) * 100)
                    return ActionResult.done(f"Brightness is {percent}%.", level=percent)
                return ActionResult.fail("Could not read the brightness.")
            value = max(1, min(100, int(level)))
            ok, output = self._run([self.brightnessctl, "set", f"{value}%"])
            return ActionResult.done(f"Brightness set to {value}%.", level=value) if ok \
                else ActionResult.fail(f"Could not set the brightness: {output}",
                                       hint="brightnessctl may need udev rules or sudo.")
        if self.xrandr and self.display:
            ok, output = self._run([self.xrandr, "--query"])
            if ok:
                output_name = ""
                for line in output.splitlines():
                    if " connected" in line:
                        output_name = line.split()[0]
                        break
                if not output_name:
                    return ActionResult.fail("No connected display found via xrandr.")
                if level is None:
                    return ActionResult.done(
                        "I can only set brightness through xrandr on this system, not read it.",
                        hint="Install brightnessctl for precise control.",
                    )
                value = max(0.15, min(1.0, int(level) / 100))
                ok, output = self._run([self.xrandr, "--output", output_name, "--brightness", f"{value:.2f}"])
                return ActionResult.done(f"Brightness set to {int(value * 100)}% (via xrandr).") if ok \
                    else ActionResult.fail(f"Could not set the brightness: {output}")
        return ActionResult.fail("Brightness control needs brightnessctl: sudo apt install brightnessctl",
                                 hint="On Wayland, `busctl` and GNOME's own slider also work.")

    def list_processes(self, limit: int = 12, sort_by: str = "cpu") -> ActionResult:
        return _processes_list(limit, sort_by)

    def kill_process(self, target: str, force: bool = False) -> ActionResult:
        return _kill_process(target, force)

    def start_process(self, command: str, args: list[str] | None = None,
                      cwd: str | None = None) -> ActionResult:
        return _start_process(command, args, cwd)

    def run_shell(self, command: str, timeout: float = 20.0, cwd: str | None = None) -> ActionResult:
        return _run_shell(command, timeout, cwd)

    def screen_text(self) -> ActionResult:
        return _screen_text_result()

    def capabilities(self) -> dict[str, Capability]:
        x11 = bool(self.display)
        note = ("Wayland session detected — xdotool will not work; use an X11 session or ydotool."
                if self.wayland or self.session == "wayland" else "")
        return {
            "keyboard": Capability("keyboard", bool(self.xdotool) and x11,
                                   "xdotool" if self.xdotool else "needs xdotool",
                                   "" if self.xdotool else "sudo apt install xdotool"),
            "mouse": Capability("mouse", bool(self.xdotool) and x11, "xdotool" if self.xdotool else "needs xdotool",
                                "" if self.xdotool else "sudo apt install xdotool"),
            "windows": Capability("windows", bool((self.wmctrl or self.xdotool) and x11),
                                  "wmctrl" if self.wmctrl else "xdotool",
                                  "" if (self.wmctrl or self.xdotool) else "sudo apt install wmctrl xdotool"),
            "volume": Capability("volume", bool(self.pactl or self.amixer),
                                 "pactl" if self.pactl else ("amixer" if self.amixer else "not found"),
                                 "" if (self.pactl or self.amixer) else "sudo apt install pulseaudio-utils"),
            "brightness": Capability("brightness", bool(self.brightnessctl or (self.xrandr and x11)),
                                     "brightnessctl" if self.brightnessctl else "xrandr (set only)",
                                     "" if self.brightnessctl else "sudo apt install brightnessctl"),
            "processes": Capability("processes", _has_psutil(), "psutil",
                                    "" if _has_psutil() else "pip install psutil"),
            "shell": Capability("shell", True, "/bin/sh (every command is confirmed)"),
            "screen_text": Capability("screen_text", True, "OCR via Tesseract",
                                      "pip install mss pillow pytesseract + tesseract-ocr"),
            "session_note": Capability("session", x11, note or "X11 session"),
            "media_keys": Capability("media_keys", bool(self.playerctl or self.xdotool),
                                     "playerctl" if self.playerctl else (
                                         "xdotool keys" if self.xdotool else "not found"),
                                     "" if (self.playerctl or self.xdotool)
                                     else "sudo apt install playerctl xdotool"),
            "system_switches": Capability(
                "system_switches", bool(self.nmcli or which("gsettings") or self.powerprofilesctl),
                "nmcli/gsettings/power-profiles-daemon",
                "" if self.nmcli else "sudo apt install network-manager for Wi-Fi switching"),
            "drag": Capability("drag", bool(self.xdotool) and x11,
                               "xdotool" if self.xdotool else "needs xdotool",
                               "" if self.xdotool else "sudo apt install xdotool"),
        }


def _has_psutil() -> bool:
    try:
        import psutil  # noqa: F401

        return True
    except Exception:
        return False


def pick_backend() -> Any:
    """Return the backend for this platform, or a NullBackend explaining why not."""
    try:
        if sys.platform.startswith("win"):
            return WindowsBackend()
        if sys.platform == "darwin":
            return MacBackend()
        return LinuxBackend()
    except Exception as exc:
        from .base import NullBackend

        return NullBackend(f"({os_name()} backend failed to start: {exc})")


def backend_capabilities(backend: Any) -> dict[str, Capability]:
    """Best-effort capability report for any backend."""
    getter = getattr(backend, "capabilities", None)
    if callable(getter):
        try:
            return dict(getter())
        except Exception as exc:
            return {"backend": Capability("backend", False, f"probe failed: {exc}")}
    if backend.name == "simulation":
        return backend_capabilities(backend.inner)
    return {"backend": Capability("backend", False, "no capability information")}


def control_install_hints() -> list[str]:
    """What to install so more control works, per platform."""
    if sys.platform.startswith("win"):
        return [
            "pycaw            exact volume control (pip install pycaw)",
            "Tesseract        reading text off the screen (screenshot OCR)",
        ]
    if sys.platform == "darwin":
        return [
            "cliclick         mouse control (brew install cliclick)",
            "brightness       display brightness (brew install brightness)",
            "Accessibility    System Settings → Privacy & Security → Accessibility for keystrokes",
        ]
    return [
        "xdotool wmctrl   keyboard, mouse and window control (sudo apt install xdotool wmctrl)",
        "pactl / amixer   volume control (sudo apt install pulseaudio-utils)",
        "brightnessctl    display brightness (sudo apt install brightnessctl)",
        "tesseract-ocr    reading text off the screen",
    ]


def open_path_with_default(path: str) -> bool:
    """Small utility used by the file actions."""
    if not path:
        return False
    try:
        target = Path(path).expanduser()
        if sys.platform.startswith("win"):
            os.startfile(str(target))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(target)])
        else:
            opener = shutil.which("xdg-open")
            if not opener:
                return False
            subprocess.Popen([opener, str(target)], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        return True
    except Exception:
        return False
