"""
The action layer: everything JARVIS can *do* to the machine, in one registry.

Voice commands, recorded routines and LLM-generated plans all end up here, which
means the safety rules only have to be right once.

Every action carries a **risk tier**:

===========  ==================================================================
``safe``     never interrupts: reading state, moving the mouse, notifications
``confirm``  asks first unless you are in trusted mode or approved the plan:
             typing text, closing windows, writing files, running a command
``dangerous`` always asks, and only runs at all if ``allow_dangerous`` is on:
             deleting, killing processes, powering the machine off
===========  ==================================================================

Some tiers are *adaptive*: typing is only ``confirm`` until the focused window
looks like a terminal, at which point it becomes ``dangerous`` because those
keystrokes are about to become commands. That decision lives in
:meth:`Action.risk_for`, next to the action it protects.

Alongside the policy sits an append-only **audit log** (``~/.jarvis/audit.log``)
recording every action, its authorisation and its outcome, so "what did JARVIS
do at 3pm?" always has an answer.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import time
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import Settings, path_for
from .control import ActionResult, Controller, human_error, open_path_with_default, os_name
from .host import Host
from .memory import Memory

# ════════════════════════════════════════════════════════════════════════════
#  Risk, policy and decisions
# ════════════════════════════════════════════════════════════════════════════

SAFE = "safe"
CONFIRM = "confirm"
DANGEROUS = "dangerous"
BLOCKED = "blocked"

RISK_ORDER = {SAFE: 0, CONFIRM: 1, DANGEROUS: 2, BLOCKED: 3}
RISK_BADGE = {SAFE: "safe", CONFIRM: "needs confirmation", DANGEROUS: "destructive", BLOCKED: "refused"}

TRUST_LEVELS = ("ask_all", "ask_risky", "trusted")
TRUST_LABELS = {
    "ask_all": "Ask me for everything (safest)",
    "ask_risky": "Ask for anything that changes things (recommended)",
    "trusted": "Only ask for destructive actions",
}

#: Commands JARVIS refuses outright, whatever the trust level.
SHELL_BLOCKED: tuple[tuple[str, str], ...] = (
    (r"\b(mkfs(\.\w+)?|diskpart|fdisk|parted)\b", "disk formatting or partitioning"),
    (r"\bdd\b[^\n]*\bof=/dev/(sd|nvme|hd|disk)", "raw writes to a disk device"),
    (r"rm\s+(-[a-z]*\s+)*-?[rf]{2}\s+(/|/\*|~|\$HOME|/home|\*)(\s|$)", "wiping a whole filesystem"),
    (r"\b(rd|rmdir)\s+/s\s+/q\s+[a-z]:\\?\s*$", "deleting an entire drive"),
    (r"\bdel\s+/[fsq]\s+[a-z]:\\?\s*$", "deleting an entire drive"),
    (r":\(\)\s*\{.*\};\s*:", "a fork bomb"),
    (r"(curl|wget|iwr|invoke-webrequest)[^|]*\|\s*(ba|z|k)?sh\b", "piping a download straight into a shell"),
    (r"vssadmin\s+delete\s+shadows", "deleting shadow copies (ransomware behaviour)"),
    (r"\bbcdedit\b", "editing boot configuration"),
    (r"\b(netsh\s+advfirewall|ufw\s+disable|iptables\s+-F|set-mppreference.*disable)", "disabling the firewall"),
    (r"\b(sc|net)\s+stop\s+(windefend|wuauserv|sense|mdm|firewall)", "stopping security services"),
    (r"\breg\s+delete\s+hklm", "deleting registry hives"),
    (r"\b(history\s+-c|clear-history)\b", "wiping shell history"),
    (r"(/etc/shadow|/etc/sudoers)", "reading or rewriting system credential files"),
    (r"\bchmod\s+-r\s+777\s+/(\s|$)", "making the whole filesystem world-writable"),
)

#: Paths JARVIS will not read, copy or write: credential stores are yours alone.
SENSITIVE_PATHS = (
    "~/.ssh", "~/.aws", "~/.gnupg", "~/.kube", "~/.docker/config.json",
    "~/.netrc", "~/.git-credentials", "~/.config/gcloud", "~/.config/gh",
    "~/Library/Keychains", "~/Library/Application Support/Google/Chrome/Default/Login Data",
    "~/Library/Application Support/Google/Chrome/Default/Cookies",
    "/etc/shadow", "/etc/sudoers", "AppData/Local/Google/Chrome/User Data/Default/Login Data",
    "AppData/Roaming/Mozilla/Firefox/Profiles", "AppData/Local/Microsoft/Credentials",
)

#: Friendly app names → per-platform commands.
APP_ALIASES: dict[str, dict[str, str]] = {
    "chrome": {"Windows": "chrome.exe", "macOS": "Google Chrome", "Linux": "google-chrome"},
    "google chrome": {"Windows": "chrome.exe", "macOS": "Google Chrome", "Linux": "google-chrome"},
    "firefox": {"Windows": "firefox.exe", "macOS": "Firefox", "Linux": "firefox"},
    "edge": {"Windows": "msedge.exe", "macOS": "Microsoft Edge", "Linux": "microsoft-edge"},
    "browser": {"Windows": "chrome.exe", "macOS": "Google Chrome", "Linux": "xdg-open https://"},
    "vscode": {"Windows": "code.cmd", "macOS": "Visual Studio Code", "Linux": "code"},
    "vs code": {"Windows": "code.cmd", "macOS": "Visual Studio Code", "Linux": "code"},
    "code": {"Windows": "code.cmd", "macOS": "Visual Studio Code", "Linux": "code"},
    "editor": {"Windows": "notepad.exe", "macOS": "TextEdit", "Linux": "gedit"},
    "notepad": {"Windows": "notepad.exe", "macOS": "TextEdit", "Linux": "gedit"},
    "terminal": {"Windows": "wt.exe", "macOS": "Terminal", "Linux": "x-terminal-emulator"},
    "console": {"Windows": "wt.exe", "macOS": "Terminal", "Linux": "x-terminal-emulator"},
    "powershell": {"Windows": "powershell.exe", "macOS": "Terminal", "Linux": "bash"},
    "command prompt": {"Windows": "cmd.exe", "macOS": "Terminal", "Linux": "bash"},
    "files": {"Windows": "explorer.exe", "macOS": "Finder", "Linux": "nautilus"},
    "file manager": {"Windows": "explorer.exe", "macOS": "Finder", "Linux": "nautilus"},
    "explorer": {"Windows": "explorer.exe", "macOS": "Finder", "Linux": "nautilus"},
    "calculator": {"Windows": "calc.exe", "macOS": "Calculator", "Linux": "gnome-calculator"},
    "calendar": {"Windows": "outlook.exe", "macOS": "Calendar", "Linux": "gnome-calendar"},
    "camera": {"Windows": "microsoft.windows.camera:", "macOS": "Photo Booth", "Linux": "cheese"},
    "settings": {"Windows": "ms-settings:", "macOS": "System Settings", "Linux": "gnome-control-center"},
    "spotify": {"Windows": "Spotify.exe", "macOS": "Spotify", "Linux": "spotify"},
    "music": {"Windows": "Spotify.exe", "macOS": "Music", "Linux": "spotify"},
    "slack": {"Windows": "slack.exe", "macOS": "Slack", "Linux": "slack"},
    "discord": {"Windows": "Discord.exe", "macOS": "Discord", "Linux": "discord"},
    "zoom": {"Windows": "Zoom.exe", "macOS": "zoom.us", "Linux": "zoom"},
    "teams": {"Windows": "ms-teams.exe", "macOS": "Microsoft Teams", "Linux": "teams"},
    "mail": {"Windows": "outlook.exe", "macOS": "Mail", "Linux": "thunderbird"},
    "outlook": {"Windows": "outlook.exe", "macOS": "Microsoft Outlook", "Linux": "thunderbird"},
    "word": {"Windows": "winword.exe", "macOS": "Microsoft Word", "Linux": "libreoffice --writer"},
    "excel": {"Windows": "excel.exe", "macOS": "Microsoft Excel", "Linux": "libreoffice --calc"},
    "vlc": {"Windows": "vlc.exe", "macOS": "VLC", "Linux": "vlc"},
    "steam": {"Windows": "steam.exe", "macOS": "Steam", "Linux": "steam"},
    "obs": {"Windows": "obs64.exe", "macOS": "OBS", "Linux": "obs"},
    "gimp": {"Windows": "gimp-2.10.exe", "macOS": "GIMP", "Linux": "gimp"},
    "task manager": {"Windows": "taskmgr.exe", "macOS": "Activity Monitor", "Linux": "gnome-system-monitor"},
}

#: “open my downloads” — folders people name instead of typing a path.
WELL_KNOWN_FOLDERS: dict[str, str] = {
    "downloads": "Downloads", "download": "Downloads", "documents": "Documents",
    "desktop": "Desktop", "pictures": "Pictures", "photos": "Pictures",
    "videos": "Videos", "movies": "Videos", "music folder": "Music",
    "home folder": "", "home": "", "home directory": "", "my documents": "Documents",
    "my downloads": "Downloads", "my desktop": "Desktop", "my pictures": "Pictures",
}

#: “open youtube” — sites that have no app but do have a URL.
WEB_SHORTCUTS: dict[str, str] = {
    "youtube": "https://youtube.com", "gmail": "https://mail.google.com",
    "google": "https://google.com", "google maps": "https://maps.google.com",
    "maps": "https://maps.google.com", "github": "https://github.com",
    "chatgpt": "https://chat.openai.com", "reddit": "https://reddit.com",
    "wikipedia": "https://wikipedia.org", "linkedin": "https://linkedin.com",
    "twitter": "https://twitter.com", "x": "https://x.com",
    "instagram": "https://instagram.com", "whatsapp": "https://web.whatsapp.com",
    "netflix": "https://netflix.com", "drive": "https://drive.google.com",
    "calendar": "https://calendar.google.com", "stack overflow": "https://stackoverflow.com",
    "hacker news": "https://news.ycombinator.com", "amazon": "https://amazon.in",
}

#: Windows that turn typed text into executed commands.
TERMINAL_HINTS = (
    "terminal", "powershell", "command prompt", "cmd.exe", "bash", "zsh", "konsole",
    "gnome-terminal", "iterm", "wt.exe", "windows terminal", "xterm", "kitty", "alacritty",
    "hyper", "python", "ipython", "node", "ssh",
)

#: Executables and launchers — never “open a file”, always start the program.
APP_EXEC = re.compile(r"\.(exe|app|cmd|bat|sh|desktop|lnk)$", re.IGNORECASE)

#: File types that should be handed to the desktop's default application.
DOCUMENT = re.compile(
    r"\.(pdf|txt|md|markdown|rst|docx?|xlsx?|pptx?|odt|ods|odp|csv|tsv|json|ya?ml|toml|ini|conf|"
    r"log|png|jpe?g|gif|webp|svg|bmp|tiff?|mp3|wav|m4a|flac|ogg|mp4|mov|avi|mkv|webm|"
    r"zip|tar|gz|tgz|bz2|xz|7z|rar|py|js|ts|tsx|jsx|html?|css|scss|sh|ps1|bat|ipynb|db|sqlite)$",
    re.IGNORECASE)

SECRETISH = re.compile(r"(sk-[A-Za-z0-9]{16,}|gsk_[A-Za-z0-9]{16,}|AKIA[0-9A-Z]{12,}|"
                       r"gh[pousr]_[A-Za-z0-9]{16,})")


@dataclass
class Decision:
    """What the policy decided about one action invocation."""

    risk: str = SAFE
    allowed: bool = True
    needs_confirmation: bool = False
    reason: str = ""

    @property
    def blocked(self) -> bool:
        return not self.allowed


class Policy:
    """Turns (action, args, settings) into a decision. Never executes anything."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    # ── shell ────────────────────────────────────────────────────────────
    @staticmethod
    def check_command(command: str) -> tuple[str, str]:
        """(risk, reason) for a shell command."""
        lowered = (command or "").lower()
        for pattern, reason in SHELL_BLOCKED:
            if re.search(pattern, lowered):
                return BLOCKED, f"refused: {reason}"
        dangerous_markers = (
            r"\brm\b", r"\bdel\b", r"\bformat\b", r"\bkill(all)?\b", r"\btaskkill\b",
            r"\bsystemctl\s+(stop|disable|mask)\b", r"\bservice\s+\S+\s+stop\b",
            r"\bchown\s+-R\b", r"\bchmod\s+-R\b", r"\bmv\b.*\*", r">\s*/dev/sd",
            r"\bshutdown\b", r"\breboot\b", r"\btruncate\b", r"\bwipe\b", r"\bshred\b",
            r"\bcurl\b.*\b-o\b", r"\bgit\s+clean\s+-[a-z]*f", r"\bgit\s+reset\s+--hard\b",
            r"\bpip\s+uninstall\b", r"\bnpm\s+(unpublish|cache\s+clean)\b",
            r"\bpkill\b", r"\bkillall\b", r"\bxkill\b", r"\bmv\s+/dev/null\b",
        )
        for pattern in dangerous_markers:
            if re.search(pattern, lowered):
                return DANGEROUS, "this command can destroy data or stop services"
        return CONFIRM, ""

    # ── paths ────────────────────────────────────────────────────────────
    @staticmethod
    def expand(path: str) -> Path:
        return Path(os.path.expandvars(os.path.expanduser(str(path or "")))).resolve()

    @staticmethod
    def _path_forms(path: Any) -> list[str]:
        """The path as written *and* as resolved, normalised for comparison.

        Both matter: on Linux a Windows path resolves to something meaningless, so
        the text the user actually wrote is what has to be checked.
        """
        raw = str(path or "").strip()
        forms = [raw.replace("\\", "/").lower()]
        try:
            resolved = Path(os.path.expandvars(os.path.expanduser(raw))).resolve()
            forms.append(str(resolved).replace("\\", "/").lower())
        except Exception:
            pass
        return forms

    @staticmethod
    def is_sensitive(path: Any) -> bool:
        forms = Policy._path_forms(path)
        for entry in SENSITIVE_PATHS:
            needle = str(Path(os.path.expanduser(entry))).replace("\\", "/").lower()
            if any(form.startswith(needle) or needle in form for form in forms):
                return True
        return False

    @staticmethod
    def is_system_path(path: Any) -> bool:
        """Filesystem roots and OS directories: never a target for deletion."""
        for form in Policy._path_forms(path):
            text = form.rstrip("/") or "/"
            if text in {"/", "/etc", "/usr", "/bin", "/sbin", "/boot", "/lib", "/lib64", "/var",
                        "/opt", "/proc", "/sys", "/dev", "/root", "/srv", "/mnt", "/media",
                        "/system", "/applications", "/library", "c:", "c:/windows"}:
                return True
            if re.fullmatch(r"[a-z]:", text) or re.fullmatch(r"[a-z]:/", text):
                return True
            for prefix in ("c:/windows", "c:/program files", "c:/programdata", "c:/users/default",
                           "/system/", "/library/", "/usr/", "/etc/", "/bin/", "/sbin/", "/boot/",
                           "/lib/", "/var/", "/opt/", "/proc/", "/sys/", "/dev/", "/root/",
                           "/srv/", "/system volume information"):
                if text.startswith(prefix):
                    return True
        try:
            if Path(os.path.expanduser(str(path))).resolve() == Path.home().resolve():
                return True
        except Exception:
            pass
        return False

    @staticmethod
    def check_path(path: str, mode: str = "write") -> tuple[str, str]:
        """(risk, reason) for touching a path. ``mode`` is read/write/delete."""
        if not str(path or "").strip():
            return BLOCKED, "no path given"
        if Policy.is_sensitive(path):
            return BLOCKED, "refused: that is a credential store — I never read or write those"
        if mode in ("write", "delete") and Policy.is_system_path(path):
            return BLOCKED, f"refused: {path} is a system location"
        if mode == "delete":
            target = Policy.expand(path)
            try:
                in_home = str(target).startswith(str(Path.home()))
            except Exception:
                in_home = False
            if not in_home and not str(target).startswith(str(Path(os.environ.get("TEMP", "/tmp")))):
                return DANGEROUS, "this deletes something outside your home folder"
            return DANGEROUS, "deleting files cannot be undone by me"
        return CONFIRM, ""

    # ── the decision ─────────────────────────────────────────────────────
    def evaluate(self, action: Action, args: dict[str, Any],
                 approved_plan: bool = False) -> Decision:
        risk, reason = action.risk, ""
        try:
            computed = action.risk_for(args) if action.risk_for else None
            if computed:
                risk, reason = computed[0] or risk, computed[1] if len(computed) > 1 else ""
        except Exception as exc:  # a broken rule must fail closed
            return Decision(BLOCKED, False, False, f"risk check failed: {exc}")

        if risk == BLOCKED:
            return Decision(BLOCKED, False, False, reason or "refused by policy")

        if risk == DANGEROUS and not self.settings.get("allow_dangerous", False):
            return Decision(
                DANGEROUS, False, False,
                (reason or "this is a destructive action")
                + " — enable “Allow destructive actions” in Settings → Automation if you want me to do these",
            )

        trust = str(self.settings.get("trust_level", "ask_risky"))
        needs = False
        if risk == DANGEROUS:
            needs = True                                   # always asks, even when trusted
        elif risk == CONFIRM:
            needs = not (trust == "trusted" or approved_plan)
        elif risk == SAFE:
            needs = trust == "ask_all"
        return Decision(risk, True, needs, reason)


# ════════════════════════════════════════════════════════════════════════════
#  Audit log
# ════════════════════════════════════════════════════════════════════════════

def redact(value: Any, limit: int = 80) -> Any:
    """Keep audit entries useful without storing whole documents or secrets."""
    if isinstance(value, str):
        text = SECRETISH.sub("<redacted>", value)
        if len(text) > limit:
            return text[:limit] + f"…({len(text)} chars)"
        return text
    if isinstance(value, dict):
        return {key: redact(item, limit) for key, item in list(value.items())[:12]}
    if isinstance(value, (list, tuple)):
        return [redact(item, limit) for item in list(value)[:12]]
    return value


class AuditLog:
    """Append-only JSONL record of everything JARVIS attempted."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or path_for("audit.log")

    def record(self, action: str, args: dict[str, Any], decision: Decision,
               result: ActionResult | None, source: str = "voice",
               extra: dict[str, Any] | None = None) -> None:
        entry = {
            "ts": time.time(),
            "when": time.strftime("%Y-%m-%d %H:%M:%S"),
            "action": action,
            "args": redact(args or {}),
            "risk": decision.risk,
            "allowed": decision.allowed,
            "confirmed": None if decision.needs_confirmation is None else decision.needs_confirmation,
            "reason": decision.reason,
            "source": source,
            "ok": None if result is None else bool(result.ok),
            "message": redact(result.message if result else "", 200),
            "simulated": bool(getattr(result, "simulated", False)),
        }
        if extra:
            entry.update(extra)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def tail(self, limit: int = 60) -> list[dict[str, Any]]:
        try:
            lines = self.path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return []
        entries: list[dict[str, Any]] = []
        for line in lines[-limit:]:
            try:
                entries.append(json.loads(line))
            except ValueError:
                continue
        return entries

    def clear(self) -> None:
        try:
            self.path.write_text("", encoding="utf-8")
        except OSError:
            pass


# ════════════════════════════════════════════════════════════════════════════
#  Actions
# ════════════════════════════════════════════════════════════════════════════

@dataclass
class ActionContext:
    """Everything an action may use."""

    settings: Settings
    memory: Memory
    host: Host
    controller: Controller
    core: Any = None
    recorder: Any = None
    approved_plan: bool = False

    def require(self, value: Any, name: str) -> Any:
        if value in (None, ""):
            raise ValueError(f"missing required parameter '{name}'")
        return value


@dataclass
class Action:
    """One thing JARVIS can do to the machine."""

    name: str
    title: str
    description: str
    run: Callable[[ActionContext, dict[str, Any]], ActionResult]
    params: dict[str, str] = field(default_factory=dict)
    risk: str = SAFE
    risk_for: Callable[[dict[str, Any]], tuple[str, str]] | None = None
    examples: tuple[str, ...] = ()
    group: str = "general"

    def invoke(self, ctx: ActionContext, args: dict[str, Any]) -> ActionResult:
        try:
            result = self.run(ctx, args or {})
        except (ValueError, KeyError, TypeError) as exc:
            return ActionResult.fail(f"{self.title}: {exc}")
        except Exception as exc:
            return ActionResult.fail(f"{self.title} failed: {human_error(exc)}")
        if not isinstance(result, ActionResult):
            return ActionResult.done(str(result))
        return result

    def catalogue_line(self) -> str:
        params = ", ".join(f"{key}" for key in self.params) or "—"
        suffix = f"  e.g. {self.examples[0]}" if self.examples else ""
        return f"  {self.name}({params}) [{self.risk}] — {self.description}{suffix}"


# ── keyboard ────────────────────────────────────────────────────────────────

def _press_risk(args: dict[str, Any]) -> tuple[str, str]:
    keys = str(args.get("keys", "")).lower()
    if "win" in keys.split("+") or "command" in keys.split("+"):
        return DANGEROUS, f"{keys} opens system-level shortcuts on this platform"
    return CONFIRM, ""


# ── filesystem helpers ──────────────────────────────────────────────────────

def _resolve(path: str) -> Path:
    return Policy.expand(path)


def _ensure_parent(path: Path) -> None:
    if path.parent and not path.parent.exists():
        path.parent.mkdir(parents=True, exist_ok=True)


def _delete(path: Path, use_trash: bool) -> tuple[bool, str]:
    if use_trash:
        try:
            from send2trash import send2trash

            send2trash(str(path))
            return True, "moved to the recycle bin / trash"
        except Exception:
            pass
    if path.is_dir():
        shutil.rmtree(path)
        return True, "deleted permanently (no trash available)"
    path.unlink()
    return True, "deleted permanently (no trash available)"


def build_actions(controller: Controller, audit: AuditLog | None = None) -> dict[str, Action]:
    """Create the standard action set bound to a controller."""

    actions: dict[str, Action] = {}

    def register(action: Action) -> None:
        actions[action.name] = action

    # ── apps: focus it, launch it, or open a path — whichever applies ────
    def open_app(ctx: ActionContext, args: dict[str, Any]) -> ActionResult:
        target = str(ctx.require(args.get("target"), "target")).strip()
        lowered = target.lower().rstrip("?")
        if lowered.startswith(("http://", "https://")):
            return ActionResult.done(f"Opened {target}") if ctx.host.open_target(target) \
                else ActionResult.fail(f"Could not open {target}")
        folder = WELL_KNOWN_FOLDERS.get(lowered)
        if folder is not None:
            path = Path.home() / folder if folder else Path.home()
            if open_path_with_default(str(path)):
                return ActionResult.done(f"Opened {path}")
            return ActionResult.fail(f"I could not open {path}.")
        site = WEB_SHORTCUTS.get(lowered)
        if site:
            return ActionResult.done(f"Opened {site}") if ctx.host.open_target(site) \
                else ActionResult.fail(f"Could not open {site}")
        if DOCUMENT.search(target) and not APP_EXEC.search(target):
            for base in (None, Path.home() / "Downloads", Path.home() / "Documents",
                         Path.home() / "Desktop", Path.home(), Path.cwd()):
                candidate = Policy.expand(target) if base is None else base / target
                if candidate.exists() and open_path_with_default(str(candidate)):
                    return ActionResult.done(f"Opened {candidate}")
        if "/" in target or "\\" in target or target.startswith("~"):
            path = Policy.expand(target)
            if path.exists():
                if open_path_with_default(str(path)):
                    return ActionResult.done(f"Opened {path}")
                return ActionResult.fail(f"Nothing on this system knows how to open {path}")
        focused = controller.focus_window(target)          # already running? switch to it
        if focused.ok:
            return ActionResult.done(focused.message, window=True)
        command = command_for_app(lowered)
        if command is None:
            return ActionResult.fail(
                focused.message or f"I could not find an app called \u201c{target}\u201d.",
                hint="Say the exact executable name, use \u201crun <command>\u201d for the "
                     "shell, or check your saved routines with \u201cwhat routines do I have\u201d.",
            )
        if os_name() == "macOS" and " " in command:
            launched = controller.start_process("open", ["-a", command])
        else:
            parts = command.split()
            launched = controller.start_process(parts[0], parts[1:] or None)
        if launched.ok:
            return ActionResult.done(f"Started {target}.")
        return ActionResult.fail(
            f"I could not start \u201c{target}\u201d: {launched.message}",
            hint="Check the app name, or tell me the exact command to run.",
        )

    register(Action(
        "open_app", "Open an app",
        "switch to an app that is already running, or launch it (also opens files, folders and URLs)",
        open_app, {"target": "app name, file, folder or URL"},
        risk=CONFIRM, examples=("open chrome", "open my music"), group="apps",
    ))

    def type_text(ctx: ActionContext, args: dict[str, Any]) -> ActionResult:
        text = str(args.get("text", ""))
        if not text:
            return ActionResult.fail("I need some text to type.")
        active = controller.active_window()
        title = (active.data.get("title") or "") if active.ok else ""
        if not args.get("_approved_terminal") and title:
            lowered = title.lower()
            if any(hint in lowered for hint in TERMINAL_HINTS):
                ctx.host.notify(
                    "Typing into a terminal", f"Focused window: {title}", level="alarm"
                )
        return controller.type_text(text, float(args.get("interval", 0) or 0))

    register(Action(
        "type_text", "Type text",
        "type text into whatever window is focused",
        type_text, {"text": "what to type", "interval": "seconds between keystrokes"},
        risk=CONFIRM, examples=("type hello world",), group="keyboard",
    ))
    def type_into(ctx: ActionContext, args: dict[str, Any]) -> ActionResult:
        """Focus a window first, then type — “type my address into the form”. """
        target = str(ctx.require(args.get("target"), "target")).strip()
        text = str(ctx.require(args.get("text"), "text"))
        focused = controller.focus_window(target)
        if not focused.ok:
            return ActionResult.fail(f"I could not find a window called “{target}”.",
                                     hint="Open it first, or say “open <app>” and then type.")
        typed = controller.type_text(text, float(args.get("interval", 0) or 0))
        if not typed.ok:
            return typed
        return ActionResult.done(f"Typed {len(text)} characters into {target}.",
                                 target=target, characters=len(text))

    register(Action(
        "type_into", "Type into an app",
        "switch to a window and type into it, without you having to click it first",
        type_into, {"target": "window or app title", "text": "what to type"},
        risk=CONFIRM, examples=("type hello into notepad",), group="keyboard",
    ))
    register(Action(
        "press_keys", "Press keys",
        "press a key or shortcut such as ctrl+s or alt+tab",
        lambda ctx, args: controller.press(str(ctx.require(args.get("keys"), "keys"))),
        {"keys": "e.g. ctrl+shift+t"},
        risk=CONFIRM, risk_for=_press_risk, examples=("press ctrl+s",), group="keyboard",
    ))

    # ── mouse ───────────────────────────────────────────────────────────
    register(Action(
        "move_mouse", "Move the mouse",
        "move the pointer to coordinates",
        lambda ctx, args: controller.move_mouse(int(args.get("x", 0)), int(args.get("y", 0)),
                                                bool(args.get("relative"))),
        {"x": "pixels", "y": "pixels", "relative": "true to move by an offset"},
        risk=SAFE, group="mouse",
    ))
    register(Action(
        "click", "Click",
        "click a mouse button at the current pointer position",
        lambda ctx, args: controller.click(str(args.get("button", "left")), int(args.get("clicks", 1))),
        {"button": "left|right|middle", "clicks": "1-3"},
        risk=SAFE, group="mouse",
    ))
    register(Action(
        "scroll", "Scroll",
        "scroll the focused window up or down",
        lambda ctx, args: controller.scroll(int(ctx.require(args.get("amount"), "amount"))),
        {"amount": "positive scrolls up, negative scrolls down"},
        risk=SAFE, group="mouse",
    ))
    register(Action(
        "pointer_position", "Pointer position",
        "read where the mouse pointer is",
        lambda ctx, args: controller.pointer(), risk=SAFE, group="mouse",
    ))

    # ── windows ─────────────────────────────────────────────────────────
    register(Action(
        "list_windows", "List windows",
        "list the open windows and applications",
        lambda ctx, args: controller.windows(), risk=SAFE, group="windows",
    ))
    register(Action(
        "active_window", "Active window",
        "name the window in focus",
        lambda ctx, args: controller.active_window(), risk=SAFE, group="windows",
    ))
    register(Action(
        "focus_window", "Focus a window",
        "bring a window to the front (partial title is enough)",
        lambda ctx, args: controller.focus_window(str(ctx.require(args.get("title"), "title"))),
        {"title": "window title or part of it"},
        risk=SAFE, examples=("switch to chrome",), group="windows",
    ))
    register(Action(
        "minimize_window", "Minimise a window",
        "minimise a window",
        lambda ctx, args: controller.window_action(str(args.get("title", "")), "minimize"),
        {"title": "window title"}, risk=SAFE, group="windows",
    ))
    register(Action(
        "maximize_window", "Maximise a window",
        "maximise (or restore) a window",
        lambda ctx, args: controller.window_action(str(args.get("title", "")), "maximize"),
        {"title": "window title"}, risk=SAFE, group="windows",
    ))
    register(Action(
        "close_window", "Close a window",
        "ask a window to close — unsaved work may prompt",
        lambda ctx, args: controller.close_window(str(ctx.require(args.get("title"), "title"))),
        {"title": "window title"}, risk=CONFIRM, examples=("close notepad",), group="windows",
    ))
    register(Action(
        "switch_window", "Switch window",
        "cycle to the next window (alt+tab / cmd+tab)",
        lambda ctx, args: controller.switch_window(), risk=SAFE, group="windows",
    ))

    # ── audio & display ─────────────────────────────────────────────────
    register(Action(
        "get_volume", "Read the volume",
        "report the current system volume",
        lambda ctx, args: controller.volume(), risk=SAFE, group="audio",
    ))
    register(Action(
        "set_volume", "Set the volume",
        "set the system volume to a percentage, or nudge it up/down",
        lambda ctx, args: controller.volume(level=int(args["level"])) if args.get("level") is not None
        else controller.volume(mute=str(args["mute"])) if args.get("mute")
        else ActionResult.fail("Tell me a level (0-100), or 'mute'."),
        {"level": "0-100", "mute": "toggle|on|off"},
        risk=SAFE, examples=("set the volume to 30",), group="audio",
    ))
    register(Action(
        "nudge_volume", "Change the volume",
        "turn the volume up or down by a number of percent",
        lambda ctx, args: _nudge_volume(controller, int(ctx.require(args.get("delta"), "delta"))),
        {"delta": "percent, negative to lower"}, risk=SAFE, examples=("volume down 10",),
        group="audio",
    ))
    register(Action(
        "get_brightness", "Read the brightness",
        "report the display brightness",
        lambda ctx, args: controller.brightness(), risk=SAFE, group="display",
    ))
    register(Action(
        "set_brightness", "Set the brightness",
        "set the display brightness to a percentage",
        lambda ctx, args: controller.brightness(int(ctx.require(args.get("level"), "level"))),
        {"level": "5-100"}, risk=SAFE, examples=("set brightness to 40",), group="display",
    ))

    # ── processes ───────────────────────────────────────────────────────
    register(Action(
        "list_processes", "List processes",
        "show the busiest processes",
        lambda ctx, args: controller.processes(int(args.get("limit", 12) or 12),
                                               str(args.get("sort_by", "cpu") or "cpu")),
        {"limit": "how many", "sort_by": "cpu|memory"}, risk=SAFE, group="processes",
    ))
    register(Action(
        "kill_process", "Stop a process",
        "terminate a process by name or pid",
        lambda ctx, args: controller.kill_process(str(ctx.require(args.get("target"), "target")),
                                                  bool(args.get("force"))),
        {"target": "process name or pid", "force": "true to kill instead of asking politely"},
        risk=DANGEROUS, examples=("kill chrome",), group="processes",
    ))
    register(Action(
        "start_app", "Start an app",
        "launch a program (detached, no shell)",
        lambda ctx, args: controller.start_process(str(ctx.require(args.get("command"), "command")),
                                                   args.get("args") or None, args.get("cwd")),
        {"command": "executable", "args": "list of arguments", "cwd": "working directory"},
        risk=CONFIRM, examples=("start notepad",), group="processes",
    ))

    # ── shell ───────────────────────────────────────────────────────────
    def command_risk(args: dict[str, Any]) -> tuple[str, str]:
        return Policy.check_command(str(args.get("command", "")))

    register(Action(
        "run_command", "Run a command",
        "run a shell command and capture its output",
        lambda ctx, args: controller.shell(str(ctx.require(args.get("command"), "command")),
                                           args.get("timeout"), args.get("cwd")),
        {"command": "the command line", "timeout": "seconds", "cwd": "working directory"},
        risk=CONFIRM, risk_for=command_risk, examples=("run ls -la",), group="shell",
    ))

    # ── filesystem ──────────────────────────────────────────────────────
    register(Action(
        "list_dir", "List a folder",
        "list what is inside a folder",
        lambda ctx, args: _list_dir(str(args.get("path", "~"))),
        {"path": "folder path"}, risk=SAFE, group="files",
    ))
    register(Action(
        "open_path", "Open a file or folder",
        "open a path with the system default application",
        lambda ctx, args: ActionResult.done(f"Opened {args.get('path')}")
        if open_path_with_default(str(ctx.require(args.get("path"), "path")))
        else ActionResult.fail(f"Could not open {args.get('path')} — does it exist?"),
        {"path": "file or folder"}, risk=SAFE, group="files",
    ))
    register(Action(
        "make_dir", "Create a folder",
        "create a folder (and any missing parents)",
        lambda ctx, args: _make_dir(ctx, str(ctx.require(args.get("path"), "path"))),
        {"path": "folder to create"}, risk=CONFIRM, group="files",
        risk_for=lambda args: Policy.check_path(str(args.get("path", "")), "write"),
    ))
    register(Action(
        "write_file", "Write a file",
        "write text to a file, creating or overwriting it",
        lambda ctx, args: _write_file(ctx, str(ctx.require(args.get("path"), "path")),
                                      str(args.get("content", "")), bool(args.get("append"))),
        {"path": "file to write", "content": "text", "append": "true to add instead of overwrite"},
        risk=CONFIRM, group="files",
        risk_for=lambda args: Policy.check_path(str(args.get("path", "")), "write"),
    ))
    register(Action(
        "copy_path", "Copy",
        "copy a file or folder",
        lambda ctx, args: _transfer(ctx, "copy", str(ctx.require(args.get("source"), "source")),
                                    str(ctx.require(args.get("destination"), "destination"))),
        {"source": "what to copy", "destination": "where to put it"},
        risk=CONFIRM, group="files",
        risk_for=lambda args: Policy.check_path(str(args.get("source", "")), "read"),
    ))
    register(Action(
        "move_path", "Move or rename",
        "move or rename a file or folder",
        lambda ctx, args: _transfer(ctx, "move", str(ctx.require(args.get("source"), "source")),
                                    str(ctx.require(args.get("destination"), "destination"))),
        {"source": "current path", "destination": "new path"},
        risk=CONFIRM, group="files",
        risk_for=lambda args: Policy.check_path(str(args.get("source", "")), "write"),
    ))
    register(Action(
        "delete_path", "Delete",
        "delete a file or folder (recycle bin when available)",
        lambda ctx, args: _delete_path(ctx, str(ctx.require(args.get("path"), "path"))),
        {"path": "what to delete"}, risk=DANGEROUS, group="files",
        risk_for=lambda args: Policy.check_path(str(args.get("path", "")), "delete"),
    ))
    register(Action(
        "zip_path", "Zip",
        "compress a file or folder into a .zip archive",
        lambda ctx, args: _zip(ctx, str(ctx.require(args.get("source"), "source")),
                               str(args.get("destination") or "")),
        {"source": "file or folder", "destination": "optional .zip path"},
        risk=CONFIRM, group="files",
    ))

    # ── screen ──────────────────────────────────────────────────────────
    register(Action(
        "read_screen", "Read the screen",
        "capture the screen and OCR the text on it",
        lambda ctx, args: controller.read_screen(), risk=SAFE, group="screen",
    ))
    register(Action(
        "screenshot", "Screenshot",
        "save a screenshot to your JARVIS folder",
        lambda ctx, args: controller.screenshot(args.get("path")), risk=SAFE, group="screen",
    ))

    # ── power ───────────────────────────────────────────────────────────
    def power_risk(args: dict[str, Any]) -> tuple[str, str]:
        action = str(args.get("action", "")).lower()
        if action in ("lock", "lock screen"):
            return SAFE, ""          # you asked for it; locking is instant and harmless
        if action in ("sleep", "suspend"):
            return CONFIRM, "the machine will go to sleep"
        return DANGEROUS, f"'{action}' ends your session"

    register(Action(
        "power", "Power action",
        "lock, sleep, restart, shut down or sign out",
        lambda ctx, args: controller.power(str(ctx.require(args.get("action"), "action"))),
        {"action": "lock|sleep|restart|shutdown|signout"},
        risk=DANGEROUS, risk_for=power_risk, examples=("lock my screen",), group="power",
    ))

    # ── assistant plumbing (handy inside routines) ──────────────────────
    register(Action(
        "notify", "Notify",
        "show a desktop notification",
        lambda ctx, args: (ctx.host.notify(str(args.get("title", "JARVIS")), str(args.get("text", "")),
                                          str(args.get("level", "info"))),
                           ActionResult.done("Notification shown."))[1],
        {"title": "heading", "text": "body", "level": "info|alarm"}, risk=SAFE, group="assistant",
    ))
    register(Action(
        "speak", "Speak",
        "say something out loud",
        lambda ctx, args: (ctx.host.speak(str(args.get("text", ""))), ActionResult.done("Said it."))[1],
        {"text": "what to say"}, risk=SAFE, group="assistant",
    ))
    register(Action(
        "wait", "Wait",
        "pause for a number of seconds (useful between GUI steps)",
        lambda ctx, args: (time.sleep(min(float(args.get("seconds", 1) or 1), 300)),
                           ActionResult.done(f"Waited {args.get('seconds', 1)}s."))[1],
        {"seconds": "0-300"}, risk=SAFE, group="assistant",
    ))
    register(Action(
        "ask_user", "Ask for input",
        "ask the user a question and use their answer",
        lambda ctx, args: _ask_user(ctx, str(args.get("prompt", "What should I use?"))),
        {"prompt": "the question"}, risk=SAFE, group="assistant",
    ))
    register(Action(
        "remember", "Remember",
        "store a fact in memory",
        lambda ctx, args: _remember(ctx, str(ctx.require(args.get("key"), "key")),
                                    str(ctx.require(args.get("value"), "value"))),
        {"key": "name", "value": "what to remember"}, risk=SAFE, group="assistant",
    ))
    def clipboard_read(ctx: ActionContext, args: dict[str, Any]) -> ActionResult:
        text = ctx.host.get_clipboard()
        _digest_clipboard(ctx, text)          # so “put back what I copied” works later
        preview = text[:600]
        return ActionResult.done(f"Clipboard ({len(text)} chars): {preview}", text=text)

    register(Action(
        "clipboard_read", "Read the clipboard",
        "read the current clipboard text",
        clipboard_read, risk=SAFE, examples=("read my clipboard",), group="assistant",
    ))

    def clipboard_write(ctx: ActionContext, args: dict[str, Any]) -> ActionResult:
        text = str(args.get("text", ""))
        if not ctx.host.set_clipboard(text):
            return ActionResult.fail("Clipboard write failed.")
        _digest_clipboard(ctx, text)
        return ActionResult.done("Copied to the clipboard.", text=text)

    register(Action(
        "clipboard_write", "Write the clipboard",
        "put text on the clipboard (JARVIS remembers what it copied)",
        clipboard_write, {"text": "what to copy"}, risk=SAFE,
        examples=("copy hello to my clipboard",), group="assistant",
    ))
    register(Action(
        "open_url", "Open a URL",
        "open a link in the default browser",
        lambda ctx, args: ActionResult.done(f"Opened {args.get('url')}")
        if ctx.host.open_target(str(ctx.require(args.get("url"), "url")))
        else ActionResult.fail(f"Could not open {args.get('url')}"),
        {"url": "http(s) link"}, risk=SAFE, group="assistant",
    ))
    register(Action(
        "run_skill", "Run a JARVIS skill",
        "answer or act using a built-in skill, e.g. 'brief me' or 'system status'",
        lambda ctx, args: _run_skill(ctx, str(ctx.require(args.get("text"), "text"))),
        {"text": "what to ask JARVIS"}, risk=SAFE, group="assistant",
    ))



    # ── windows: placement, snapping, desktops ──────────────────────────
    def window_control(ctx: ActionContext, args: dict[str, Any]) -> ActionResult:
        title = str(args.get("title") or "").strip()
        action = str(ctx.require(args.get("action"), "action")).strip().lower().replace(" ", "_")
        if not title:
            active = controller.active_window()
            if not active.ok:
                return active
            title = str(active.data.get("title") or "")
            if not title:
                return ActionResult.fail("I could not tell which window has focus.")
        return controller.window_control(title, action)

    register(Action(
        "window_control", "Arrange a window",
        "fullscreen, snap left/right, keep on top, send to another workspace",
        window_control, {"title": "window (blank = the focused one)",
                         "action": "fullscreen|snap_left|snap_right|always_on_top|no_longer_on_top|"
                                   "minimize|maximize|restore|center|fullscreen"},
        risk=CONFIRM, examples=("snap chrome to the left", "make this window fullscreen"),
        group="windows",
    ))

    def move_window(ctx: ActionContext, args: dict[str, Any]) -> ActionResult:
        title = str(args.get("title") or "").strip()
        width = int(args.get("width") or 1200)
        height = int(args.get("height") or 800)
        x, y = int(args.get("x") or 0), int(args.get("y") or 0)
        if not title:
            active = controller.active_window()
            title = str(active.data.get("title") or "") if active.ok else ""
        if not title:
            return ActionResult.fail("I could not tell which window to move.")
        return controller.window_control(title, f"move:{x},{y},{width},{height}")

    register(Action(
        "move_window", "Move and resize a window",
        "put a window at exact coordinates with an exact size",
        move_window, {"title": "window", "x": "pixels from the left", "y": "pixels from the top",
                      "width": "pixels", "height": "pixels"},
        risk=CONFIRM, examples=("move chrome to 0,0 1920x1080",), group="windows",
    ))

    register(Action(
        "show_desktop", "Show the desktop",
        "hide every window so you can see the desktop",
        lambda ctx, args: controller.press("win+d" if os_name() != "macOS" else "f11"),
        risk=CONFIRM, examples=("show me the desktop",), group="windows",
    ))

    def switch_desktop(ctx: ActionContext, args: dict[str, Any]) -> ActionResult:
        number = str(ctx.require(args.get("number"), "number")).strip()
        active = controller.active_window()
        title = str(active.data.get("title") or "") if active.ok else "workspace"
        return controller.window_control(title, f"switch_workspace:{number}")

    register(Action(
        "switch_desktop", "Switch workspace",
        "jump to another virtual desktop / workspace",
        switch_desktop, {"number": "1-9"}, risk=CONFIRM,
        examples=("go to workspace 2",), group="windows",
    ))

    def list_apps(ctx: ActionContext, args: dict[str, Any]) -> ActionResult:
        result = controller.windows()
        if not result.ok:
            return result
        apps = _apps_from_windows(result)
        if not apps:
            return ActionResult.done("I could not see any open windows.", apps=[])
        return ActionResult.done("Running apps: " + ", ".join(apps), apps=apps)

    register(Action(
        "list_apps", "List running apps",
        "list the applications that have windows open",
        list_apps, risk=SAFE, examples=("what apps are open",), group="windows",
    ))

    # ── keyboard: named everyday commands ────────────────────────────────
    def press_shortcut(ctx: ActionContext, args: dict[str, Any]) -> ActionResult:
        name = str(ctx.require(args.get("name"), "name")).strip().lower()
        table = shortcut_table()
        keys = table.get(name)
        if keys is None:
            close = [key for key in table if name in key or key in name]
            hint = f"Did you mean: {', '.join(close[:4])}?" if close else \
                "Try “list shortcuts” to see what I know."
            return ActionResult.fail(f"I don't know a shortcut called “{name}”.", hint=hint)
        result = controller.press(keys)
        if result.ok:
            return ActionResult.done(f"{name.title()} → {keys}", keys=keys)
        return result

    register(Action(
        "press_shortcut", "Press an everyday shortcut",
        "save, copy, paste, undo, find, print, new tab, refresh and friends",
        press_shortcut, {"name": "e.g. save, copy, paste, undo, find, print, new tab"},
        risk=CONFIRM, examples=("press save", "hit undo"), group="keyboard",
    ))
    register(Action(
        "list_shortcuts", "List shortcuts",
        "show the named shortcuts JARVIS understands",
        lambda ctx, args: ActionResult.done(
            "Shortcuts I know: " + ", ".join(sorted(shortcut_table())),
            shortcuts=shortcut_table(),
        ),
        risk=SAFE, examples=("what shortcuts do you know",), group="keyboard",
    ))

    # ── mouse: exact placement and dragging ──────────────────────────────
    register(Action(
        "click_at", "Click at coordinates",
        "move the pointer to x,y and click there",
        lambda ctx, args: controller.click_at(int(args.get("x", 0)), int(args.get("y", 0)),
                                              str(args.get("button", "left")),
                                              int(args.get("clicks", 1))),
        {"x": "pixels", "y": "pixels", "button": "left|right|middle", "clicks": "1-3"},
        risk=CONFIRM, examples=("click at 400,300",), group="mouse",
    ))
    register(Action(
        "drag_mouse", "Drag the mouse",
        "press, move across the screen and release — for sliders, selections and files",
        lambda ctx, args: controller.drag(int(args.get("x1", 0)), int(args.get("y1", 0)),
                                          int(args.get("x2", 0)), int(args.get("y2", 0)),
                                          float(args.get("duration", 0.4) or 0.4)),
        {"x1": "start x", "y1": "start y", "x2": "end x", "y2": "end y", "duration": "seconds"},
        risk=CONFIRM, examples=("drag from 100,100 to 500,400",), group="mouse",
    ))

    # ── screen: find things by reading them ─────────────────────────────
    register(Action(
        "find_on_screen", "Find text on screen",
        "read the screen with OCR and report where a word or phrase is",
        lambda ctx, args: _find_text_on_screen(
            controller, str(ctx.require(args.get("text"), "text"))),
        {"text": "word or phrase to look for"}, risk=SAFE,
        examples=("where is the save button",), group="screen",
    ))

    register(Action(
        "click_on_screen", "Click text on screen",
        "find a word or phrase on screen with OCR and click it",
        lambda ctx, args: _click_text(controller, str(ctx.require(args.get("text"), "text")),
                                      int(args.get("clicks", 1)),
                                      str(args.get("button", "left"))),
        {"text": "label to click", "button": "left|right|middle", "clicks": "1-3"},
        risk=CONFIRM, examples=("click the save button",), group="screen",
    ))

    def copy_screen_text(ctx: ActionContext, args: dict[str, Any]) -> ActionResult:
        text, error = _screen_text(controller, ctx)
        if error:
            return error
        if not ctx.host.set_clipboard(text):
            return ActionResult.fail("I read the screen but could not reach the clipboard.")
        _digest_clipboard(ctx, text)
        return ActionResult.done(f"Copied {len(text)} characters of on-screen text.",
                                 characters=len(text))

    register(Action(
        "copy_screen_text", "Copy the text on screen",
        "read the screen and put everything it says on the clipboard",
        copy_screen_text, risk=CONFIRM,
        examples=("copy what's on my screen",), group="screen",
    ))
    register(Action(
        "screen_size", "Screen size",
        "report the resolution of the display",
        lambda ctx, args: _screen_resolution(), risk=SAFE,
        examples=("what's my screen resolution",), group="screen",
    ))

    # ── audio, media and system switches ─────────────────────────────────
    def media_control(ctx: ActionContext, args: dict[str, Any]) -> ActionResult:
        action = str(args.get("action", "play_pause")).strip().lower()
        if action not in ("play_pause", "play", "pause", "next", "previous", "stop"):
            return ActionResult.fail(f"I don't know the media action “{action}”.",
                                     hint="Try play, pause, next, previous or stop.")
        return controller.media(action)

    register(Action(
        "media_control", "Control media playback",
        "play, pause, skip track — works with whatever player is open",
        media_control,
        {"action": "play_pause|next|previous|stop"}, risk=SAFE,
        examples=("next song", "pause the music"), group="audio",
    ))
    register(Action(
        "mute_control", "Mute or unmute",
        "turn the sound off, bring it back, or toggle it",
        lambda ctx, args: controller.volume(mute=str(args.get("action", "toggle"))),
        {"action": "mute|unmute|toggle"}, risk=SAFE,
        examples=("mute", "unmute the sound"), group="audio",
    ))
    register(Action(
        "nudge_brightness", "Adjust the brightness",
        "make the screen a little brighter or dimmer",
        lambda ctx, args: _nudge_brightness(controller, int(args.get("delta", 10))),
        {"delta": "percentage points, e.g. -10"}, risk=SAFE,
        examples=("dim the screen", "make the screen brighter"), group="display",
    ))
    register(Action(
        "system_switch", "Switch a system setting",
        "Wi-Fi, Bluetooth, night light, dark mode, Do Not Disturb, power saving",
        lambda ctx, args: controller.system_toggle(
            str(ctx.require(args.get("kind"), "kind")),
            None if str(args.get("state", "")).lower() in ("", "toggle") else
            str(args["state"]).lower() in ("on", "true", "1", "yes", "enable", "enabled"),
        ),
        {"kind": "wifi|bluetooth|nightlight|dark_mode|dnd|power_saver", "state": "on|off|toggle"},
        risk=CONFIRM, examples=("turn off wifi", "turn on dark mode"), group="system",
    ))

    # ── processes: find before you kill ──────────────────────────────────
    register(Action(
        "find_process", "Find a process",
        "look for running processes by name and show what they are using",
        lambda ctx, args: _find_process(controller, str(ctx.require(args.get("name"), "name")),
                                        int(args.get("limit", 10))),
        {"name": "part of the process name", "limit": "how many to show"},
        risk=SAFE, examples=("is chrome running",), group="processes",
    ))

    # ── files: read, append, search, rename, unpack ──────────────────────
    register(Action(
        "read_file", "Read a file",
        "show the contents of a text file",
        lambda ctx, args: _read_lines(_resolve(str(ctx.require(args.get("path"), "path"))),
                                      int(args.get("start", 1) or 1), int(args.get("count", 60) or 60)),
        {"path": "file to read", "start": "first line", "count": "how many lines"},
        risk=SAFE, examples=("read notes.txt",), group="files",
    ))
    register(Action(
        "append_file", "Add to a file",
        "add a line or a block of text to the end of a file without touching what is there",
        lambda ctx, args: _append_file(_resolve(str(ctx.require(args.get("path"), "path"))),
                                       str(args.get("content", ""))),
        {"path": "file to append to", "content": "text to add"},
        risk=CONFIRM, examples=("add 'buy milk' to my todo list",), group="files",
    ))
    register(Action(
        "find_files", "Find files",
        "search a folder (and everything inside it) for files matching a pattern",
        lambda ctx, args: _find_files(str(args.get("root") or "~"), str(args.get("pattern") or "*"),
                                      int(args.get("limit", 40) or 40)),
        {"pattern": "shell-style pattern, e.g. *.pdf", "root": "folder to search (defaults to home)",
         "limit": "max results"}, risk=SAFE,
        examples=("find all pdf files in downloads",), group="files",
    ))
    register(Action(
        "file_info", "Inspect a file",
        "size, type and when it last changed",
        lambda ctx, args: _file_info(_resolve(str(ctx.require(args.get("path"), "path")))),
        {"path": "file or folder"}, risk=SAFE,
        examples=("how big is that folder",), group="files",
    ))
    register(Action(
        "disk_usage", "Disk space",
        "how much space is free on the drive that holds a path",
        lambda ctx, args: _disk_usage(str(args.get("path") or "~")),
        {"path": "any path on the drive"}, risk=SAFE,
        examples=("how much disk space is left",), group="files",
    ))
    register(Action(
        "rename_path", "Rename",
        "give a file or folder a new name in the same place",
        lambda ctx, args: _rename(ctx, str(ctx.require(args.get("path"), "path")),
                                  str(ctx.require(args.get("name"), "name"))),
        {"path": "what to rename", "name": "the new name"},
        risk=CONFIRM, examples=("rename report.md to final.md",), group="files",
    ))
    register(Action(
        "duplicate_path", "Duplicate",
        "copy a file or folder next to itself (or to a destination)",
        lambda ctx, args: _duplicate(ctx, str(ctx.require(args.get("path"), "path")),
                                     str(args.get("destination") or "")),
        {"path": "what to duplicate", "destination": "optional new location"},
        risk=CONFIRM, examples=("duplicate report.md",), group="files",
    ))
    register(Action(
        "unzip_path", "Extract an archive",
        "unpack a .zip file into a folder",
        lambda ctx, args: _unzip(ctx, str(ctx.require(args.get("path"), "path")),
                                 str(args.get("destination") or "")),
        {"path": "the .zip file", "destination": "folder to extract into"},
        risk=CONFIRM, examples=("extract backup.zip",), group="files",
    ))

    # ── clipboard history ────────────────────────────────────────────────
    register(Action(
        "clipboard_history", "Clipboard history",
        "the last twenty things that went through the clipboard",
        lambda ctx, args: ActionResult.done(
            _clipboard_history_text(_history(ctx), str(args.get("level", "verbose"))),
            entries=list(reversed(_history(ctx).entries)),
        ),
        {"level": "verbose|brief"}, risk=SAFE,
        examples=("what did I copy earlier",), group="clipboard",
    ))
    register(Action(
        "clipboard_restore", "Restore the clipboard",
        "put one of the recent clipboard entries back on the clipboard",
        lambda ctx, args: _clipboard_restore(ctx, int(args.get("index", 1) or 1)),
        {"index": "1 = the last thing copied"}, risk=SAFE,
        examples=("put back what I copied before",), group="clipboard",
    ))
    register(Action(
        "clipboard_forget", "Forget clipboard history",
        "clear JARVIS's clipboard history (the clipboard itself is untouched)",
        lambda ctx, args: (lambda history: (history.clear(),
                                            ActionResult.done("Clipboard history cleared."))[1])(
            _history(ctx)),
        risk=CONFIRM, examples=("forget my clipboard history",), group="clipboard",
    ))

    # ── terminal and housekeeping ───────────────────────────────────────
    register(Action(
        "open_terminal", "Open a terminal",
        "start the system terminal so you can work in it",
        lambda ctx, args: _open_terminal(controller),
        risk=CONFIRM, examples=("open a terminal",), group="system",
    ))
    register(Action(
        "empty_trash", "Empty the trash",
        "permanently delete everything in the recycle bin / trash",
        lambda ctx, args: _empty_trash(controller),
        risk=DANGEROUS, examples=("empty the trash",), group="system",
    ))

    return actions


# ── named shortcuts: “save this” → the right keys for this OS ───────────────

def shortcut_table() -> dict[str, str]:
    """Everyday commands, spelled out per platform.

    People say “save this file”, not “press ctrl+s” — so JARVIS keeps the mapping
    (including the places where macOS differs) and presses the right keys.
    """
    command = "win" if os_name() == "macOS" else "ctrl"
    return {
        "save": f"{command}+s", "save as": f"{command}+shift+s",
        "copy": f"{command}+c", "copy selection": f"{command}+c",
        "paste": f"{command}+v", "cut": f"{command}+x",
        "undo": f"{command}+z", "redo": f"{command}+shift+z",
        "select all": f"{command}+a", "select everything": f"{command}+a",
        "find": f"{command}+f", "find in page": f"{command}+f",
        "replace": f"{command}+h", "print": f"{command}+p",
        "new tab": f"{command}+t", "close tab": f"{command}+w",
        "reopen tab": f"{command}+shift+t", "next tab": "ctrl+tab",
        "previous tab": "ctrl+shift+tab", "refresh": f"{command}+r",
        "reload": f"{command}+r", "zoom in": f"{command}+plus",
        "zoom out": f"{command}+minus", "reset zoom": f"{command}+0",
        "new window": f"{command}+n", "new document": f"{command}+n",
        "open file": f"{command}+o", "fullscreen": "f11" if os_name() != "macOS" else "ctrl+win+f",
        "escape": "escape", "switch apps": "alt+tab" if os_name() != "macOS" else "win+tab",
        "show desktop": "win+d" if os_name() != "macOS" else "f11",
        "lock screen": "win+l" if os_name() == "Windows" else f"{command}+ctrl+q",
        "task manager": "ctrl+shift+escape", "settings": f"{command}+comma",
        "clear line": "ctrl+u", "cancel": "escape", "confirm": "return",
        "move to line start": "home", "move to line end": "end",
        "scroll to top": "ctrl+home", "scroll to bottom": "ctrl+end",
    }


def keyboard_platform_note() -> str:
    return "macOS uses ⌘ where Windows/Linux use Ctrl." if os_name() == "macOS" else ""


# ── clipboard history ───────────────────────────────────────────────────────

class ClipboardHistory:
    """The last twenty things you or JARVIS put on the clipboard.

    Kept in ``~/.jarvis/clipboard.json`` — small, local, and never sent anywhere.
    """

    LIMIT = 20

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or path_for("clipboard.json")
        self.entries: list[dict[str, Any]] = []
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.entries = []
            return
        entries = data.get("entries") if isinstance(data, dict) else data
        self.entries = [item for item in (entries or []) if isinstance(item, dict)][-self.LIMIT:]

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps({"version": 1, "entries": self.entries[-self.LIMIT:]},
                                            indent=2, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass

    def add(self, text: str) -> None:
        text = (text or "").strip()
        if not text:
            return
        if SECRETISH.search(text):
            return                                  # looks like a key or token: forget it
        if self.entries and self.entries[-1].get("text") == text:
            return                                    # de-duplicate the top of the stack
        self.entries.append({"ts": time.time(), "text": text[:4000],
                             "when": time.strftime("%H:%M:%S")})
        del self.entries[:-self.LIMIT]
        self.save()

    def latest(self, index: int = 1) -> dict[str, Any] | None:
        if not self.entries:
            return None
        index = max(1, min(int(index), len(self.entries)))
        return self.entries[-index]

    def clear(self) -> None:
        self.entries = []
        self.save()


def _clipboard_history_text(history: ClipboardHistory, level: str = "verbose") -> str:
    if not history.entries:
        return "I have not seen anything on the clipboard yet."
    lines = []
    for offset, entry in enumerate(reversed(history.entries), start=1):
        text = str(entry.get("text", ""))
        if level == "brief":
            text = text.splitlines()[0][:60]
        else:
            text = text[:160].replace("\n", " ⏎ ")
        lines.append(f"  {offset}. [{entry.get('when', '')}] {text}")
    return "Clipboard history (newest first):\n" + "\n".join(lines)


# ── screen reading: OCR as a tool the assistant can act on ──────────────────

def _screen_text(controller: Controller, ctx: ActionContext) -> tuple[str, ActionResult | None]:
    result = controller.read_screen()
    if not result.ok:
        return "", result
    text = str(result.data.get("text") or "")
    if not text.strip() and result.simulated:
        return "", ActionResult.done(result.message, simulated=True)
    if not text.strip():
        return "", ActionResult.fail("I read the screen but found no text on it.",
                                     hint="Try scrolling the text into view first.")
    return text, None


def _find_on_screen(controller: Controller, needle: str) -> tuple[Any, ActionResult | None]:
    """Locate text on screen (OCR) and return its centre point."""
    from .control import screen as screen_module

    if not needle.strip():
        return None, ActionResult.fail("What should I look for on the screen?")
    if not screen_module.available():
        return None, ActionResult.fail(
            "Reading the screen needs Pillow, mss and Tesseract.",
            hint="pip install pillow mss  +  install tesseract-ocr")
    capture = screen_module.capture_screen()
    if not capture.ok:
        return None, ActionResult.fail(f"Screen capture failed: {capture.error}")
    words = screen_module.locate(capture.image, needle)
    if not words:
        return None, ActionResult.fail(
            f"I could not find “{needle}” on the screen.",
            hint="Make sure it is visible, and that the window is not covered.")
    return words[0], None


def _click_text(controller: Controller, needle: str, clicks: int = 1,
                button: str = "left") -> ActionResult:
    found, error = _find_on_screen(controller, needle)
    if error:
        return error
    x, y, text = int(found["x"]), int(found["y"]), str(found.get("text", needle))
    result = controller.click_at(x, y, button, clicks)
    if not result.ok:
        return result
    return ActionResult.done(f"Clicked “{text}” at {x},{y}.", x=x, y=y, text=text)


def _nudge_brightness(controller: Controller, delta: int) -> ActionResult:
    current = controller.brightness()
    if not current.ok:
        return current
    level = int(current.data.get("level") or 50)
    target = max(5, min(100, level + int(delta)))
    result = controller.brightness(target)
    if not result.ok:
        return result
    return ActionResult.done(f"Brightness {level}% → {target}%.", level=target)


def _human_size(size: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def _folder_size(path: Path) -> int:
    total = 0
    try:
        for item in path.rglob("*"):
            if item.is_file():
                total += item.stat().st_size
    except OSError:
        pass
    return total


def _read_lines(path: Path, start: int = 1, count: int = 60) -> ActionResult:
    if not path.exists():
        return ActionResult.fail(f"There is no file at {path}.")
    if path.is_dir():
        return ActionResult.fail(f"{path} is a folder, not a file.")
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        return ActionResult.fail(f"I could not read {path}: {human_error(exc)}")
    first = max(1, int(start or 1))
    window = lines[first - 1:first - 1 + max(1, int(count or 60))]
    if not window:
        return ActionResult.fail(f"{path} has only {len(lines)} line(s).")
    body = "\n".join(window)
    return ActionResult.done(
        f"{path.name} — lines {first}-{first + len(window) - 1} of {len(lines)}:\n{body}",
        path=str(path), total=len(lines), text=body,
    )


def _find_files(root: str, pattern: str, limit: int = 40) -> ActionResult:
    base = _resolve(root or "~")
    if not base.exists():
        return ActionResult.fail(f"There is no folder at {base}.")
    if not base.is_dir():
        return ActionResult.fail(f"{base} is a file, not a folder.")
    matches: list[Path] = []
    try:
        for path in base.rglob(pattern or "*"):
            matches.append(path)
            if len(matches) >= max(1, int(limit or 40)):
                break
    except OSError as exc:
        return ActionResult.fail(f"Searching {base} failed: {human_error(exc)}")
    if not matches:
        return ActionResult.fail(f"I found nothing matching {pattern} under {base}.",
                                 hint="Patterns work like shell globs: *.pdf, report*, notes?.txt")
    listed = "\n".join(f"  {item}" for item in matches)
    return ActionResult.done(f"Found {len(matches)} match(es) for {pattern} in {base}:\n{listed}",
                             matches=[str(item) for item in matches], count=len(matches))


def _file_info(path: Path) -> ActionResult:
    if not path.exists():
        return ActionResult.fail(f"There is nothing at {path}.")
    try:
        stat = path.stat()
        kind = "folder" if path.is_dir() else (path.suffix.lstrip(".") or "file")
        size = _folder_size(path) if path.is_dir() else stat.st_size
        modified = time.strftime("%Y-%m-%d %H:%M", time.localtime(stat.st_mtime))
    except OSError as exc:
        return ActionResult.fail(f"I could not inspect {path}: {human_error(exc)}")
    return ActionResult.done(
        f"{path.name} — {kind}, {_human_size(size)}, changed {modified}",
        path=str(path), size=size, kind=kind, modified=modified,
    )


def _history(ctx: ActionContext) -> ClipboardHistory:
    history = getattr(ctx, "_clipboard_history", None)
    if history is None:
        history = ClipboardHistory()
        ctx._clipboard_history = history      # remembered for this session
    return history


def _digest_clipboard(ctx: ActionContext, text: str) -> None:
    try:
        _history(ctx).add(text)
    except Exception:
        pass


def _terminal_command() -> str:
    return {
        "Windows": "wt.exe",
        "macOS": "Terminal",
        "Linux": "x-terminal-emulator",
    }.get(os_name(), "x-terminal-emulator")


def _open_terminal(controller: Controller) -> ActionResult:
    command = _terminal_command()
    if os_name() == "macOS":
        launched = controller.start_process("open", ["-a", command])
    else:
        launched = controller.start_process(command, None)
    if launched.ok:
        return ActionResult.done("Opened a terminal.")
    for fallback in ("gnome-terminal", "konsole", "xfce4-terminal", "xterm", "cmd.exe"):
        if controller.start_process(fallback, None).ok:
            return ActionResult.done(f"Opened {fallback}.")
    return ActionResult.fail(f"I could not open a terminal: {launched.message}")


def _empty_trash(controller: Controller) -> ActionResult:
    if os_name() == "Windows":
        command = ('powershell -NoProfile -Command "Clear-RecycleBin -Force '
                   '-ErrorAction SilentlyContinue"')
    elif os_name() == "macOS":
        command = "osascript -e 'tell application \"Finder\" to empty trash'"
    else:
        command = ("rm -rf ~/.local/share/Trash/files/* ~/.local/share/Trash/info/*")
    return controller.shell(command, timeout=30.0)


def _screen_resolution() -> ActionResult:
    from .control import screen as screen_module

    if not screen_module.available():
        return ActionResult.fail(
            "Reading the screen size needs Pillow and mss.",
            hint="pip install pillow mss",
        )
    capture = screen_module.capture_screen()
    if not capture.ok:
        return capture
    width, height = capture.image.size
    return ActionResult.done(f"The screen is {width}×{height}.", width=width, height=height)


def _apps_from_windows(result: ActionResult) -> list[str]:
    seen: dict[str, None] = {}
    for row in (result.data or {}).get("windows", []) or []:
        label = str(row.get("app") or row.get("class") or row.get("process")
                    or row.get("title") or "").strip()
        if not label:
            continue
        label = label.split(" — ")[0].split(" - ")[0].strip()
        if label and label not in seen:
            seen[label] = None
    return list(seen)


def _find_process(controller: Controller, name: str, limit: int = 10) -> ActionResult:
    listing = controller.processes(limit=400, sort_by="memory")
    if not listing.ok:
        return listing
    rows = listing.data.get("processes", []) if isinstance(listing.data, dict) else []
    needle = name.strip().lower()
    matches = [row for row in rows if needle in str(row.get("name", "")).lower()
               or needle in str(row.get("cmdline", "")).lower()]
    if not matches:
        return ActionResult.done(f"Nothing called “{name}” is running right now.", count=0)
    shown = matches[:max(1, int(limit or 10))]
    lines = [f"  {row.get('name')} (pid {row.get('pid')}, {row.get('memory')} MB, cpu {row.get('cpu')}%)"
             for row in shown]
    return ActionResult.done(f"Found {len(matches)} match(es) for “{name}”:\n" + "\n".join(lines),
                             count=len(matches), matches=shown)


def _append_file(path: Path, content: str) -> ActionResult:
    if not content:
        return ActionResult.fail("There is nothing to add.")
    if path.exists() and path.is_dir():
        return ActionResult.fail(f"{path} is a folder, not a file.")
    formatted = content if content.endswith("\n") else content + "\n"
    try:
        _ensure_parent(path)
        existing = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
        separator = "" if (existing.endswith("\n") or not existing) else "\n"
        with path.open("a", encoding="utf-8") as handle:
            handle.write(separator + formatted)
    except OSError as exc:
        return ActionResult.fail(f"I could not write to {path}: {human_error(exc)}")
    return ActionResult.done(f"Added {len(content)} characters to {path.name}.", path=str(path))


def _disk_usage(path: str) -> ActionResult:
    target = _resolve(path or "~")
    while not target.exists() and target != target.parent:
        target = target.parent
    try:
        usage = shutil.disk_usage(str(target))
    except OSError as exc:
        return ActionResult.fail(f"I could not read the disk usage: {human_error(exc)}")
    free, total = _human_size(usage.free), _human_size(usage.total)
    return ActionResult.done(f"{free} free of {total} ({usage.free * 100 // usage.total}% left).",
                             free=usage.free, total=usage.total)


def _rename(ctx: ActionContext, path: str, name: str) -> ActionResult:
    source = _resolve(path)
    if not source.exists():
        return ActionResult.fail(f"There is nothing at {source}.")
    name = name.strip().strip("/\\")
    if not name:
        return ActionResult.fail("I need the new name.")
    target = source.with_name(name)
    if target.exists():
        return ActionResult.fail(f"{target} already exists — pick another name or delete it first.")
    try:
        source.rename(target)
    except OSError as exc:
        return ActionResult.fail(f"Rename failed: {human_error(exc)}")
    return ActionResult.done(f"{source.name} → {target.name}", path=str(target))


def _duplicate(ctx: ActionContext, path: str, destination: str = "") -> ActionResult:
    source = _resolve(path)
    if not source.exists():
        return ActionResult.fail(f"There is nothing at {source}.")
    if destination:
        target = _resolve(destination)
    else:
        stem, suffix = (source.stem, source.suffix) if source.is_file() else (source.name, "")
        target = source.with_name(f"{stem} copy{suffix}")
    if target.exists():
        counter = 2
        while True:
            candidate = target.with_name(f"{target.stem} {counter}{target.suffix}")
            if not candidate.exists():
                target = candidate
                break
            counter += 1
    try:
        _ensure_parent(target)
        if source.is_dir():
            shutil.copytree(source, target)
        else:
            shutil.copy2(source, target)
    except OSError as exc:
        return ActionResult.fail(f"Copy failed: {human_error(exc)}")
    return ActionResult.done(f"Duplicated {source.name} → {target.name}", path=str(target))


def _unzip(ctx: ActionContext, path: str, destination: str = "") -> ActionResult:
    source = _resolve(path)
    if source.suffix.lower() != ".zip" or not source.is_file():
        return ActionResult.fail(f"{source} is not a .zip file.")
    target = _resolve(destination) if destination else source.with_suffix("")
    try:
        with zipfile.ZipFile(source) as archive:
            names = archive.namelist()
            for name in names:            # refuse “zip slip” paths that escape the folder
                if name.startswith("/") or ".." in Path(name).parts:
                    return ActionResult.fail(f"{source.name} contains an unsafe path ({name}).")
            target.mkdir(parents=True, exist_ok=True)
            archive.extractall(target)
    except (OSError, zipfile.BadZipFile) as exc:
        return ActionResult.fail(f"Extract failed: {human_error(exc)}")
    return ActionResult.done(f"Extracted {len(names)} item(s) into {target}", path=str(target))


def _clipboard_restore(ctx: ActionContext, index: int) -> ActionResult:
    entry = _history(ctx).latest(index)
    if entry is None:
        return ActionResult.fail("I have not seen anything on the clipboard yet.")
    if not ctx.host.set_clipboard(str(entry.get("text", ""))):
        return ActionResult.fail("I could not reach the clipboard.")
    preview = str(entry.get("text", ""))[:80].replace("\n", " ")
    return ActionResult.done(f"Clipboard restored to: {preview}", text=entry.get("text", ""))


def _find_text_on_screen(controller: Controller, text: str) -> ActionResult:
    if not text.strip():
        return ActionResult.fail("What should I look for on the screen?")
    found, error = _find_on_screen(controller, text)
    if error:
        return error
    return ActionResult.done(f"Found “{found['text']}” at {found['x']},{found['y']}.",
                             x=found["x"], y=found["y"], text=found["text"])


# ── small helpers used by the actions above ─────────────────────────────────

def command_for_app(name: str) -> str | None:
    """Resolve a friendly app name to something the OS can launch."""
    name = (name or "").strip().lower()
    if not name:
        return None
    aliases = APP_ALIASES.get(name)
    if aliases:
        return aliases.get(os_name(), aliases.get("Linux"))
    if re.fullmatch(r"[\w .+-]{1,40}\.(exe|app|cmd|bat|sh|desktop)", name):
        return name
    return None


def _nudge_volume(controller: Controller, delta: int) -> ActionResult:
    """Read the volume, move it, then confirm what it became."""
    current = controller.volume()
    if not current.ok:
        return ActionResult.fail(
            current.message or "I cannot read the current volume on this system.",
            hint="Install pycaw (Windows) or PulseAudio's pactl (Linux) for precise control.",
        )
    level = current.data.get("level")
    if level is None:
        return ActionResult.fail("I could read the volume but not its level.")
    target = max(0, min(100, int(level) + int(delta)))
    return controller.volume(level=target)


# ── filesystem action helpers ───────────────────────────────────────────────

def _list_dir(path: str) -> ActionResult:
    target = _resolve(path)
    if not target.exists():
        return ActionResult.fail(f"There is no folder at {target}.")
    if not target.is_dir():
        return ActionResult.fail(f"{target} is a file, not a folder.")
    entries = sorted(target.iterdir(), key=lambda item: (item.is_file(), item.name.lower()))
    lines = [f"{target} — {sum(1 for e in entries if e.is_file())} files, "
             f"{sum(1 for e in entries if e.is_dir())} folders"]
    lines += [f"  [dir]  {entry.name}/" for entry in entries if entry.is_dir()][:15]
    lines += [f"  [file] {entry.name} ({entry.stat().st_size / 1024:.1f} KB)"
              for entry in entries if entry.is_file()][:20]
    return ActionResult.done("\n".join(lines), path=str(target))


def _make_dir(ctx: ActionContext, path: str) -> ActionResult:
    target = _resolve(path)
    if target.exists():
        return ActionResult.done(f"{target} already exists.")
    target.mkdir(parents=True, exist_ok=False)
    return ActionResult.done(f"Created {target}", path=str(target))


def _write_file(ctx: ActionContext, path: str, content: str, append: bool) -> ActionResult:
    target = _resolve(path)
    if target.is_dir():
        return ActionResult.fail(f"{target} is a folder.")
    _ensure_parent(target)
    if append:
        with target.open("a", encoding="utf-8") as handle:
            handle.write(content)
    else:
        target.write_text(content, encoding="utf-8")
    verb = "Appended to" if append else "Wrote"
    return ActionResult.done(f"{verb} {target} ({len(content)} characters).", path=str(target))


def _transfer(ctx: ActionContext, kind: str, source: str, destination: str) -> ActionResult:
    src, dst = _resolve(source), _resolve(destination)
    if not src.exists():
        return ActionResult.fail(f"There is nothing at {src}.")
    if src == dst:
        return ActionResult.fail("Source and destination are the same path.")
    if dst.is_dir():
        dst = dst / src.name
    _ensure_parent(dst)
    if kind == "copy":
        if src.is_dir():
            shutil.copytree(src, dst, dirs_exist_ok=True)
            what = "folder"
        else:
            shutil.copy2(src, dst)
            what = "file"
        return ActionResult.done(f"Copied {what} {src.name} → {dst}", path=str(dst))
    shutil.move(str(src), str(dst))
    return ActionResult.done(f"Moved {src.name} → {dst}", path=str(dst))


def _delete_path(ctx: ActionContext, path: str) -> ActionResult:
    target = _resolve(path)
    if not target.exists():
        return ActionResult.fail(f"There is nothing at {target}.")
    use_trash = bool(ctx.settings.get("use_trash", True))
    ok, detail = _delete(target, use_trash)
    if not ok:
        return ActionResult.fail(f"Could not delete {target}.")
    return ActionResult.done(f"Deleted {target} — {detail}", path=str(target))


def _zip(ctx: ActionContext, source: str, destination: str) -> ActionResult:
    src = _resolve(source)
    if not src.exists():
        return ActionResult.fail(f"There is nothing at {src}.")
    dst = _resolve(destination) if destination else src.with_suffix(".zip")
    if dst.suffix.lower() != ".zip":
        dst = dst.with_suffix(".zip")
    _ensure_parent(dst)
    with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as archive:
        if src.is_dir():
            for root, _dirs, files in os.walk(src):
                for name in files:
                    full = Path(root) / name
                    archive.write(full, full.relative_to(src.parent))
        else:
            archive.write(src, src.name)
    return ActionResult.done(f"Archived {src.name} → {dst.name} ({dst.stat().st_size / 1024:.1f} KB)",
                             path=str(dst))


def _ask_user(ctx: ActionContext, prompt: str) -> ActionResult:
    asker = getattr(ctx.host, "ask_text", None)
    if not callable(asker):
        return ActionResult.fail("I cannot ask questions through this interface.")
    answer = asker(prompt) or ""
    if not answer.strip():
        return ActionResult.fail("No answer given.")
    return ActionResult.done(f"Answer: {answer}", answer=answer.strip())


def _remember(ctx: ActionContext, key: str, value: str) -> ActionResult:
    ctx.memory.remember(key, value)
    ctx.host.refresh_memory()
    return ActionResult.done(f"Remembered {key} = {value}")


def _run_skill(ctx: ActionContext, text: str) -> ActionResult:
    core = ctx.core
    if core is None:
        return ActionResult.fail("Skills are not available in this context.")
    response = core.ask(text, source="routine", remember=False)
    return ActionResult(ok=bool(response.ok), message=response.text,
                        data={"skill": response.skill}, error=response.error)


# ════════════════════════════════════════════════════════════════════════════
#  Registry
# ════════════════════════════════════════════════════════════════════════════

class ActionRegistry:
    """Looks up actions, enforces policy, asks for confirmation, writes the audit log."""

    def __init__(self, settings: Settings, memory: Memory, host: Host, controller: Controller,
                 core: Any = None, audit: AuditLog | None = None) -> None:
        self.settings = settings
        self.memory = memory
        self.host = host
        self.controller = controller
        self.core = core
        self.audit = audit or AuditLog()
        self.policy = Policy(settings)
        self.actions = build_actions(controller, self.audit)
        self.recorder: Any = None          # set by jarvis.routines.RoutineRecorder
        self.timeline: Any = None          # set by the core (jarvis.routines.Timeline)
        self.voice_confirm = bool(settings.get("voice_confirm", False))
        self.pending: dict[str, Any] | None = None   # action waiting for a spoken “yes”

    # ── introspection ────────────────────────────────────────────────────
    def get(self, name: str) -> Action | None:
        return self.actions.get((name or "").strip().lower())

    def names(self) -> list[str]:
        return sorted(self.actions)

    def catalogue(self, groups: tuple[str, ...] | None = None) -> str:
        lines = ["Available actions:"]
        for action in sorted(self.actions.values(), key=lambda item: (item.group, item.name)):
            if groups and action.group not in groups:
                continue
            lines.append(action.catalogue_line())
        return "\n".join(lines)

    def context(self, approved_plan: bool = False, recorder: Any = None) -> ActionContext:
        return ActionContext(
            settings=self.settings,
            memory=self.memory,
            host=self.host,
            controller=self.controller,
            core=self.core,
            recorder=recorder if recorder is not None else self.recorder,
            approved_plan=approved_plan,
        )

    # ── execution ────────────────────────────────────────────────────────
    def execute(self, name: str, args: dict[str, Any] | None = None, source: str = "voice",
                approved_plan: bool | None = None, ctx: ActionContext | None = None,
                confirmed: bool | None = None) -> ActionResult:
        """Run one action.

        ``approved_plan`` marks "the user already approved a plan containing this
        step", ``confirmed`` marks "the user just answered yes to this exact
        action". Both skip the question; neither skips the safety rules above it.
        """
        action = self.get(name)
        args = dict(args or {})
        if action is None:
            known = ", ".join(self.names()[:12])
            return ActionResult.fail(f"I don't have an action called '{name}'. "
                                     f"Some I do have: {known}…")

        context = ctx or self.context(approved_plan=bool(approved_plan))
        if approved_plan is not None:
            context.approved_plan = bool(approved_plan)
        decision = self.policy.evaluate(action, args, context.approved_plan)

        if decision.blocked:
            self.audit.record(action.name, args, decision, None, source,
                              {"outcome": "refused"})
            return ActionResult.fail(f"I won't do that — {decision.reason}",
                                     hint="Safety rules live in jarvis/actions.py.")

        was_confirmed = None
        if confirmed is True:
            decision.needs_confirmation = False
        if decision.needs_confirmation:
            question = action.title
            detail = self.describe_call(action, args)
            if self.voice_confirm and source in ("voice", "plan", "routine"):
                # Hands-free: remember the request and let the user answer out loud.
                self.pending = {
                    "action": action.name, "args": args, "risk": decision.risk,
                    "source": source, "asked": time.time(),
                    "summary": f"{question}: {detail.splitlines()[0]}",
                }
                self.audit.record(action.name, args, decision, None, source,
                                  {"outcome": "awaiting_voice_confirmation"})
                return ActionResult(
                    ok=False,
                    message=f"Say “yes” to confirm or “no” to cancel: {detail}",
                    hint=f"Waiting on: {action.title} ({RISK_BADGE[decision.risk]}).",
                    data={"pending": True, "action": action.name},
                )
            was_confirmed = self._confirm(question, detail, decision.risk)
            if not was_confirmed:
                self.audit.record(action.name, args, decision, None, source,
                                  {"outcome": "declined"})
                return ActionResult.fail("Cancelled — you said no.", hint="Nothing was changed.")

        result = action.invoke(context, args)

        if decision.risk in (CONFIRM, DANGEROUS):
            self.audit.record(action.name, args, decision, result, source,
                              {"outcome": "done" if result.ok else "failed",
                               "confirmed": True if confirmed is True else was_confirmed})
        else:
            self.audit.record(action.name, args, decision, result, source, {"outcome": "auto"})

        # Habit learning. Two different things are recorded here:
        #  * the timeline — only real, non-rehearsed actions, because a dry run must
        #    not look like a habit;
        #  * the recorder — always, because saying “watch what I do” is an explicit
        #    request to be taught, and in dry-run mode the steps are still the ones
        #    the user means to teach.
        rehearsal = bool(getattr(self.controller, "simulate", False))
        if self.timeline is not None and not rehearsal and not source.startswith("routine"):
            try:
                self.timeline.add(action.name, args, source=source, ok=result.ok,
                                  message=getattr(result, "message", ""))
            except Exception:
                pass
        if self.recorder is not None and getattr(self.recorder, "active", False):
            try:
                self.recorder.capture(action, args, result)
            except Exception:
                pass
        return result

    def _confirm(self, title: str, detail: str, risk: str) -> bool:
        confirmer = getattr(self.host, "confirm", None)
        if not callable(confirmer):
            return False  # fail closed when the front-end cannot ask
        try:
            return bool(confirmer(title, detail, risk))
        except Exception:
            return False

    @staticmethod
    def describe_call(action: Action, args: dict[str, Any]) -> str:
        shown = {key: value for key, value in args.items() if not key.startswith("_")}
        if not shown:
            return action.description
        parts = []
        for key, value in shown.items():
            text = str(value)
            if len(text) > 160:
                text = text[:157] + "…"
            parts.append(f"{key} = {text}")
        return action.description + "\n\n" + "\n".join(parts)

    # ── dry run summary ──────────────────────────────────────────────────
    def simulate(self, name: str, args: dict[str, Any] | None = None) -> ActionResult:
        """Check what would happen without executing (used by plan previews)."""
        action = self.get(name)
        if action is None:
            return ActionResult.fail(f"No action called '{name}'.")
        decision = self.policy.evaluate(action, args or {})
        if decision.blocked:
            return ActionResult.fail(f"Would be refused: {decision.reason}")
        needs = "confirmation" if decision.needs_confirmation else "no confirmation"
        return ActionResult.done(f"{action.title} — {RISK_BADGE[decision.risk]}, {needs}.")
