"""
The ``Host`` interface: everything JARVIS needs from the outside world.

Skills never import Qt, never import pyttsx3 and never touch the network stack of
a particular UI. They call a *host* — the desktop window implements this for the
real app, tests use :class:`HeadlessHost`, and a future web or CLI front-end can
implement the same six methods.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from .config import path_for

ScheduleHandle = Any


@runtime_checkable
class Host(Protocol):
    """What a front-end must provide for skills to work."""

    def notify(self, title: str, text: str, level: str = "info") -> None:
        """Show a desktop notification / toast."""

    def speak(self, text: str) -> None:
        """Read text aloud if voice output is enabled."""

    def get_clipboard(self) -> str:
        ...

    def set_clipboard(self, text: str) -> bool:
        ...

    def screenshot(self, path: Path | None = None) -> str | None:
        ...

    def open_target(self, target: str, kind: str = "auto") -> bool:
        """Open a URL, file or folder with the OS default handler."""

    def schedule(self, delay: float, callback: Callable[[], None]) -> ScheduleHandle:
        """Run ``callback`` after ``delay`` seconds; returns a cancellable handle."""

    def cancel_schedule(self, handle: ScheduleHandle) -> bool:
        ...

    def log(self, text: str, level: str = "info", meta: dict[str, Any] | None = None) -> None:
        """Append a line to the transcript / log.

        ``meta`` carries the answering skill and timing so a front-end can label
        the message without reading shared state (which would race across threads).
        """

    def refresh_memory(self) -> None:
        """Tell the UI that memory changed (used to redraw lists)."""

    def confirm(self, title: str, detail: str, risk: str = "confirm") -> bool:
        """Ask the user to approve an action. Front-ends that cannot ask must return False
        (the action layer fails closed when nothing can authorise a risky step)."""

    def ask_text(self, prompt: str, default: str = "") -> str | None:
        """Ask the user for a piece of text (used by the ``ask_user`` action)."""


def open_with_os(target: str) -> bool:
    """Open a URL/path with the platform default handler (no shell involved)."""
    target = (target or "").strip()
    if not target:
        return False
    try:
        if target.startswith(("http://", "https://")):
            return bool(webbrowser.open(target))
        path = Path(target).expanduser()
        if not path.exists():
            return bool(webbrowser.open(target)) if "://" in target else False
        if sys.platform.startswith("win"):
            import os
            os.startfile(str(path))  # type: ignore[attr-defined]
            return True
        if sys.platform == "darwin":
            subprocess.Popen(["open", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        opener = shutil.which("xdg-open")
        if opener:
            subprocess.Popen([opener, str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
    except Exception:
        return False
    return False


class HeadlessHost:
    """A no-UI host: notifications and speech go to stdout, handy for CLI tests."""

    def __init__(self, verbose: bool = True, interactive: bool = False) -> None:
        self.verbose = verbose
        #: When interactive (the CLI chat), risky actions are approved in the
        #: terminal; otherwise nothing can approve them and they are refused.
        self.interactive = interactive
        self.notifications: list[tuple[str, str, str]] = []
        self.spoken: list[str] = []
        self.logs: list[str] = []
        self.opened: list[str] = []
        self.confirmations: list[tuple[str, str, str]] = []
        self.prompts: list[str] = []
        self.confirm_answer: bool = False   # set True to approve actions in tests
        self.text_answer: str = ""          # what ask_text returns
        self._clipboard = ""
        self._timers: list[tuple[threading.Timer, float, Callable[[], None]]] = []
        self._lock = threading.Lock()

    # ── notifications / speech ───────────────────────────────────────────
    def notify(self, title: str, text: str, level: str = "info") -> None:
        self.notifications.append((title, text, level))
        if self.verbose:
            print(f"[notify:{level}] {title} — {text}")

    def speak(self, text: str) -> None:
        self.spoken.append(text)
        if self.verbose:
            print(f"[speak] {text}")

    def confirm(self, title: str, detail: str, risk: str = "confirm") -> bool:
        """Nothing can prompt in a headless run, so the answer is whatever the
        caller configured (default: refuse, which is the safe direction)."""
        self.confirmations.append((title, detail, risk))
        if self.interactive:
            label = "DANGEROUS" if risk == "dangerous" else "needs approval"
            print(f"\n[{label}] {title}")
            for line in detail.splitlines():
                print(f"    {line}")
            try:
                reply = input("    approve? [y/N] ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                return False
            return reply in {"y", "yes", "ok", "do it", "sure"}
        if self.verbose:
            print(f"[confirm:{risk}] {title} — {detail[:80]}")
        return bool(self.confirm_answer)

    def ask_text(self, prompt: str, default: str = "") -> str | None:
        self.prompts.append(prompt)
        if self.interactive:
            try:
                return input(f"\n[question] {prompt}\n    > ")
            except (EOFError, KeyboardInterrupt):
                return None
        if self.verbose:
            print(f"[ask] {prompt}")
        return self.text_answer

    def log(self, text: str, level: str = "info", meta: dict[str, Any] | None = None) -> None:
        self.logs.append(text)

    def refresh_memory(self) -> None:
        return None

    # ── clipboard ────────────────────────────────────────────────────────
    def get_clipboard(self) -> str:
        if self._clipboard:
            return self._clipboard
        try:
            import tkinter

            root = tkinter.Tk()
            root.withdraw()
            value = root.clipboard_get()
            root.destroy()
            return value
        except Exception:
            return ""

    def set_clipboard(self, text: str) -> bool:
        self._clipboard = text
        try:
            import tkinter

            root = tkinter.Tk()
            root.withdraw()
            root.clipboard_clear()
            root.clipboard_append(text)
            root.update()
            root.destroy()
            return True
        except Exception:
            return bool(text)

    # ── screenshots ──────────────────────────────────────────────────────
    def screenshot(self, path: Path | None = None) -> str | None:
        folder = path_for("screenshots")
        folder.mkdir(parents=True, exist_ok=True)
        target = path or folder / f"shot-{time.strftime('%Y%m%d-%H%M%S')}.png"
        try:
            import mss
            from PIL import Image

            with mss.mss() as sct:
                shot = sct.grab(sct.monitors[0])
                Image.frombytes("RGB", shot.size, shot.rgb).save(target)
            return str(target)
        except Exception:
            try:
                from PIL import ImageGrab

                ImageGrab.grab().save(target)
                return str(target)
            except Exception:
                return None

    # ── opening things ───────────────────────────────────────────────────
    def open_target(self, target: str, kind: str = "auto") -> bool:
        ok = open_with_os(target)
        if ok:
            self.opened.append(target)
        return ok

    # ── scheduling ───────────────────────────────────────────────────────
    def schedule(self, delay: float, callback: Callable[[], None]) -> ScheduleHandle:
        def wrapped() -> None:
            try:
                callback()
            finally:
                with self._lock:
                    self._timers = [t for t in self._timers if t[0].is_alive()]

        timer = threading.Timer(delay, wrapped)
        timer.daemon = True
        timer.start()
        with self._lock:
            self._timers.append((timer, time.time() + delay, callback))
        return timer

    def cancel_schedule(self, handle: ScheduleHandle) -> bool:
        try:
            handle.cancel()
            with self._lock:
                self._timers = [t for t in self._timers if t[0] is not handle]
            return True
        except Exception:
            return False
