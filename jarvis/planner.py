"""
The planner: it turns sentences into action calls, and long requests into plans.

Two levels of understanding, neither of which needs an API key.

**Level 1 — the parser.** :func:`parse` is a hand-written grammar for the phrases
people actually say to a desktop assistant: *“open chrome”*, *“set the volume to
20”*, *“press ctrl+shift+t”*, *“copy ~/Downloads/a.pdf to ~/Documents”*, *“kill
spotify”*. It is deterministic, instant, offline and testable — the default.

**Level 2 — the planner.** :meth:`Planner.plan` splits a request into clauses
(*“…, then …”*, *“after that …”*, *“and then …”*), parses each one, and returns a
runnable :class:`Plan`. If the configured language model is available and some
clause did not parse, the planner asks it for a JSON plan and **validates every
step against the action registry** before anything runs — an LLM can suggest a
step, but it can never invent an action that does not exist.

A plan longer than :data:`LARGE_STEP_THRESHOLD` steps is shown to you in full and
runs only after you approve it.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .actions import APP_ALIASES, APP_EXEC, RISK_BADGE, WELL_KNOWN_FOLDERS, ActionRegistry
from .routines import Routine, Step

#: Plans at least this long are shown for approval before running.
LARGE_STEP_THRESHOLD = 6

#: Splitting markers for multi-step requests (order matters).
SEQUENCE_MARKERS = (
    r"\s+and\s+then\s+", r"\s+then\s+", r"\s+after\s+that\s+", r"\s+afterwards\s+",
    r"\s+followed\s+by\s+", r"\s+next\s+", r"\s+and\s+after\s+that\s+", r"\s*;\s*",
    r"\s*\|\s*", r",\s+then\s+",
)

#: “step 1 … step 2 …” / “first, … second, …” style requests.
STEP_MARKER = re.compile(
    r"(?:^|\s)(?:step|phase)\s*\d*\s*[.:)]?\s+"
    r"|(?:^|\s)(?:first|second|third|fourth|fifth|last)\s*\d*\s*[.:),]\s*",
    re.IGNORECASE)

URLISH = re.compile(r"^(https?://|www\.)\S+$", re.IGNORECASE)
PATHISH = re.compile(r"^(~|\.|/|[A-Za-z]:\\|\\\\)")
FILENAME = re.compile(r"\.\w{1,5}$")

NUMBER_WORDS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
    "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90, "hundred": 100, "half": 50,
    "quarter": 25, "max": 100, "maximum": 100, "full": 100, "min": 5, "minimum": 5, "low": 15,
    "high": 85, "loud": 90, "quiet": 20,
}

MODIFIER_WORDS = {
    "control": "ctrl", "ctl": "ctrl", "option": "alt", "opt": "alt", "command": "win",
    "cmd": "win", "super": "win", "windows": "win", "shift": "shift", "alt": "alt", "ctrl": "ctrl",
}

KEY_ALIASES = {
    "enter": "return", "return key": "return", "escape": "esc", "esc key": "esc",
    "space bar": "space", "spacebar": "space", "backspace key": "backspace",
    "full stop": "period", "dot": "period", "slash": "slash", "tab key": "tab",
    "arrow up": "up", "arrow down": "down", "arrow left": "left", "arrow right": "right",
    "delete key": "delete", "caps lock": "capslock",
}


def _number(text: str, default: int | None = None) -> int | None:
    """Read a number from words or digits (``"thirty percent"`` → 30)."""
    found = re.search(r"(-?\d{1,3})", text)
    if found:
        return int(found.group(1))
    words = re.findall(r"[a-z]+", text.lower())
    for word in words:
        if word in NUMBER_WORDS:
            return NUMBER_WORDS[word]
    return default


def _clean_target(text: str) -> str:
    text = text.strip().strip("“”\"'")
    text = re.sub(r"^(?:the|my|that|this)\s+", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+(please|for me|now|thanks)$", "", text, flags=re.IGNORECASE)
    text = text.strip()
    folder = WELL_KNOWN_FOLDERS.get(text.lower())
    if folder is not None:
        return str(Path.home() / folder) if folder else str(Path.home())
    return text


def normalize_keys(text: str) -> str:
    """“press control shift and t” → ``ctrl+shift+t``."""
    text = text.lower().strip()
    text = re.sub(r"\s+(together|simultaneously|at once)$", "", text)
    parts = re.split(r"\s*(?:\+|plus|and|,)\s*|\s+", text)
    keys: list[str] = []
    for part in parts:
        part = part.strip()
        if not part:
            continue
        if part in MODIFIER_WORDS:
            key = MODIFIER_WORDS[part]
            if key not in keys:
                keys.append(key)
            continue
        if part in KEY_ALIASES:
            part = KEY_ALIASES[part]
        if part in {"key", "the", "press", "hit", "push", "send", "keys", "button"}:
            continue
        keys.append(part)
    return "+".join(keys)


# ════════════════════════════════════════════════════════════════════════════
#  Level 1 — parse one clause into an action call
# ════════════════════════════════════════════════════════════════════════════

# (regex, builder) — first match wins, so order is deliberate.
_RULES: list[tuple[str, Any]] = []


def _rule(pattern: str):
    def wrap(function):
        _RULES.append((pattern, function))
        return function
    return wrap


# ── apps and windows ────────────────────────────────────────────────────────
# ── decided before the generic “open / run / focus / move” rules ────────────

#: Titles that mean “whatever window is in front right now”.
SELF_WINDOW = {
    "this", "this window", "this one", "this app", "this application", "it", "the window",
    "the app", "the application", "current window", "the current window", "active window",
    "the active window", "focused window", "the focused window", "my window", "that window",
    "the front window", "the open window",
}


def _clean_title(text: str) -> str:
    """Turn “the chrome window” into “chrome”, and “this window” into “” (focused)."""
    title = (text or "").strip().strip("'\"")
    title = re.sub(r"^(?:the|my)\s+", "", title, flags=re.IGNORECASE).strip()
    title = re.sub(r"\s+window$", "", title, flags=re.IGNORECASE).strip()
    if not title or title.lower() in SELF_WINDOW:
        return ""
    return title


@_rule(r"^(?:open|start|launch|show)\s+(?:me\s+)?(?:a\s+|the\s+)?(?:new\s+)?"
       r"(?:terminal|shell|console|command\s+prompt|powershell)$")
def _terminal(_match: re.Match) -> tuple[str, dict[str, Any]]:
    return "open_terminal", {}


@_rule(r"^in\s+(?:the\s+)?terminal,?\s+(?:run\s+)?(?P<command>.+)$")
@_rule(r"^(?:run|execute)\s+(?P<command>\S+\s+.+)$")            # “run git status”, “run ls -la”
@_rule(r"^(?:run|execute)\s+(?:a\s+|the\s+)?(?:shell\s+|terminal\s+)?command\s*[:,-]?\s*(?P<command>.+)$")
def _shell_command(match: re.Match) -> tuple[str, dict[str, Any]]:
    return "run_command", {"command": match.group("command").strip()}


@_rule(r"^(?:show|go\s+to|hide)\s+(?:me\s+)?(?:everything|all\s+windows|the\s+desktop|desktop)$")
@_rule(r"^minimi[sz]e\s+all\s+windows$")
def _desktop(_match: re.Match) -> tuple[str, dict[str, Any]]:
    return "show_desktop", {}


@_rule(r"^(?:go\s+to|switch\s+to|show|open)\s+(?:my\s+|the\s+)?(?:virtual\s+)?"
       r"(?:workspace|desktop|space)\s*(?:number\s*)?(?P<number>\d{1,2})$")
def _workspace(match: re.Match) -> tuple[str, dict[str, Any]]:
    return "switch_desktop", {"number": match.group("number")}


@_rule(r"^move\s+(?!(?:the\s+)?(?:mouse|pointer|cursor)\b)(?P<title>.+?)\s+to\s+"
       r"(?P<x>\d{1,5})\s*[, ]\s*(?P<y>\d{1,5})"
       r"(?:\s+(?P<width>\d{2,5})\s*[x×]\s*(?P<height>\d{2,5}))?$")
def _move_window(match: re.Match) -> tuple[str, dict[str, Any]]:
    groups = match.groupdict()
    args: dict[str, Any] = {"title": _clean_title(groups["title"]), "x": int(groups["x"]),
                            "y": int(groups["y"])}
    if groups.get("width"):
        args["width"] = int(groups["width"])
        args["height"] = int(groups["height"])
    return "move_window", args


@_rule(r"^rename\s+(?P<path>\S+)\s+(?:to|as)\s+(?P<name>[^\s/\\]+)$")
def _rename(match: re.Match) -> tuple[str, dict[str, Any]]:
    return "rename_path", {"path": _clean_target(match.group("path")),
                           "name": match.group("name").strip()}


@_rule(r"^(?:snap|push|put)\s+(?P<title>.+?)\s+(?:to\s+|on\s+)?(?:the\s+)?(?P<side>left|right)"
       r"(?:\s+(?:side|half))?$")
@_rule(r"^snap\s+(?:this\s+)?window\s+(?:to\s+the\s+)?(?P<side>left|right)$")
def _snap(match: re.Match) -> tuple[str, dict[str, Any]]:
    return "window_control", {"title": _clean_title(match.group("title") or "this"),
                              "action": f"snap_{match.group('side').lower()}"}


@_rule(r"^(?:make|put|set)\s+(?P<title>.+?)\s+full\s?screen$")
@_rule(r"^full\s?screen\s+(?P<title>.+?)$")
def _fullscreen(match: re.Match) -> tuple[str, dict[str, Any]]:
    return "window_control", {"title": _clean_title(match.group("title")), "action": "fullscreen"}


@_rule(r"^(?:keep|pin|put|make)\s+(?P<title>.+?)\s+(?:always\s+)?on\s+top$")
def _on_top(match: re.Match) -> tuple[str, dict[str, Any]]:
    return "window_control", {"title": _clean_title(match.group("title")), "action": "always_on_top"}


@_rule(r"^(?:take|remove|unpin)\s+(?P<title>.+?)\s+off\s+top$")
def _off_top(match: re.Match) -> tuple[str, dict[str, Any]]:
    return "window_control", {"title": _clean_title(match.group("title")),
                              "action": "no_longer_on_top"}


@_rule(r"^click\s+(?:at\s+)?(?P<x>\d{2,5})\s*[, ]\s*(?P<y>\d{2,5})$")
def _click_at(match: re.Match) -> tuple[str, dict[str, Any]]:
    return "click_at", {"x": int(match.group("x")), "y": int(match.group("y"))}


@_rule(r"^drag\s+(?:from\s+)?(?P<x1>\d{1,5})\s*[, ]\s*(?P<y1>\d{1,5})\s+to\s+"
       r"(?P<x2>\d{1,5})\s*[, ]\s*(?P<y2>\d{1,5})"
       r"(?:\s+(?:in\s+|over\s+)?(?P<duration>[\d.]+)\s*(?:seconds|secs|s))?$")
def _drag(match: re.Match) -> tuple[str, dict[str, Any]]:
    args: dict[str, Any] = {key: int(match.group(key)) for key in ("x1", "y1", "x2", "y2")}
    if match.group("duration"):
        args["duration"] = float(match.group("duration"))
    return "drag_mouse", args


@_rule(r"^click\s+(?:on\s+)?(?:the\s+)?(?P<text>.+?)(?:\s+(?:button|link|tab|icon|menu))?$")
def _click_text(match: re.Match) -> tuple[str, dict[str, Any]] | None:
    text = match.group("text").strip()
    if not text or text.lower() in ("here", "there", "it", "this", "that"):
        return None
    return "click_on_screen", {"text": text}


@_rule(r"^(?:where(?:'s| is)|find|locate)\s+(?P<text>.+?)\s+on\s+(?:my\s+|the\s+)?screen$")
def _find_on_screen(match: re.Match) -> tuple[str, dict[str, Any]]:
    return "find_on_screen", {"text": match.group("text").strip()}


@_rule(r"^copy\s+(?:what(?:'s| is)\s+on\s+)?(?:my\s+|the\s+)?screen(?:\s+text)?$")
@_rule(r"^(?:grab|copy)\s+the\s+text\s+on\s+(?:my\s+|the\s+)?screen$")
def _copy_screen(_match: re.Match) -> tuple[str, dict[str, Any]]:
    return "copy_screen_text", {}


@_rule(r"^(?:what(?:'s| is)\s+)?(?:my\s+|the\s+)?(?:screen|display)\s+(?:size|resolution)$")
def _screen_size(_match: re.Match) -> tuple[str, dict[str, Any]]:
    return "screen_size", {}


@_rule(r"^(?:next|skip)(?:\s+(?:song|track|music|video))?$")
def _next_track(_match: re.Match) -> tuple[str, dict[str, Any]]:
    return "media_control", {"action": "next"}


@_rule(r"^(?:previous|last|go\s+back|back)(?:\s+(?:song|track|music|video))$")
def _previous_track(_match: re.Match) -> tuple[str, dict[str, Any]]:
    return "media_control", {"action": "previous"}


@_rule(r"^(?:play(?:\s*/\s*|\s+or\s+)pause|pause|resume|play|toggle\s+(?:the\s+)?playback)$")
@_rule(r"^(?:pause|resume|stop|play|skip)\s+(?:the\s+)?(?:music|song|video|playback|media)$")
def _playback(match: re.Match) -> tuple[str, dict[str, Any]]:
    text = match.group(0).lower()
    return "media_control", {"action": "stop" if "stop" in text else "play_pause"}


@_rule(r"^(?:press|hit|do|use)\s+(?:the\s+)?(?P<name>save\s+as|save|copy\s+selection|copy|paste|cut|"
       r"undo|redo|select\s+all|select\s+everything|find(?:\s+in\s+page)?|replace|print|"
       r"new\s+(?:tab|window|document)|close\s+tab|reopen\s+tab|next\s+tab|previous\s+tab|"
       r"refresh|reload|zoom\s+in|zoom\s+out|reset\s+zoom|open\s+file|escape|cancel|confirm|"
       r"show\s+desktop|task\s+manager|settings|clear\s+line)(?:\s+(?:shortcut|keys?))?$")
def _shortcut(match: re.Match) -> tuple[str, dict[str, Any]]:
    return "press_shortcut", {"name": " ".join(match.group("name").lower().split())}


@_rule(r"^(?:what|which)\s+shortcuts?\s+(?:do\s+you\s+(?:know|have)|are\s+there|can\s+i\s+use)$")
def _list_shortcuts(_match: re.Match) -> tuple[str, dict[str, Any]]:
    return "list_shortcuts", {}


@_rule(r"^(?P<state>turn\s+on|turn\s+off|switch\s+on|switch\s+off|enable|disable|toggle)\s+"
       r"(?:the\s+)?(?P<kind>wi-?fi|wireless|bluetooth|night\s?light|dark\s?mode|"
       r"do\s+not\s+disturb|dnd|power\s+sav(?:er|ing)(?:\s+mode)?)$")
@_rule(r"^(?P<kind>wi-?fi|bluetooth|night\s?light|dark\s?mode)\s+(?P<state>on|off)$")
def _system_switch(match: re.Match) -> tuple[str, dict[str, Any]]:
    kind = re.sub(r"\s+", " ", match.group("kind").lower())
    kind = {"wi-fi": "wifi", "wireless": "wifi", "night light": "nightlight",
            "dark mode": "dark_mode", "do not disturb": "dnd", "power saver": "power_saver",
            "power saving": "power_saver", "power saving mode": "power_saver",
            "power saver mode": "power_saver"}.get(kind, kind)
    state_text = match.group("state").lower()
    if state_text.startswith(("turn on", "switch on", "enable")) or state_text == "on":
        state = "on"
    elif state_text.startswith(("turn off", "switch off", "disable")) or state_text == "off":
        state = "off"
    else:
        state = "toggle"
    return "system_switch", {"kind": kind, "state": state}


@_rule(r"^power\s+sav(?:er|ing)(?:\s+mode)?$")
def _power_saver(_match: re.Match) -> tuple[str, dict[str, Any]]:
    return "system_switch", {"kind": "power_saver", "state": "on"}


@_rule(r"^(?:make\s+)?(?:the\s+)?(?:screen|display)?\s*(?P<direction>brighter|dimmer)"
       r"(?:\s+by\s+(?P<amount>\d{1,3}))?$")
@_rule(r"^(?:screen\s+)?brightness\s+(?P<direction>up|down)(?:\s+(?:by\s+)?(?P<amount>\d{1,3}))?$")
@_rule(r"^(?:turn|make)\s+(?:the\s+)?(?:screen|brightness)\s+(?P<direction>brighter|dimmer|up|down)"
       r"(?:\s+by\s+(?P<amount>\d{1,3}))?$")
def _brightness_nudge(match: re.Match) -> tuple[str, dict[str, Any]]:
    amount = int(match.group("amount") or 10)
    if match.group("direction").lower() in ("dimmer", "down"):
        amount = -amount
    return "nudge_brightness", {"delta": amount}


@_rule(r"^(?:please\s+)?(?:open|launch|start|run)\s+(?:up\s+)?(?:the\s+|my\s+)?"
       r"(?P<target>.+?)(?:\s+(?:app|application|program))?$")
def _open(match: re.Match) -> tuple[str, dict[str, Any]] | None:
    target = _clean_target(match.group("target"))
    lowered = target.lower()
    if not target:
        return None
    if lowered.startswith(("command", "shell")):
        command = re.sub(r"^(the\s+)?(command|shell)\s*", "", target, flags=re.IGNORECASE)
        return "run_command", {"command": command} if command else None
    if URLISH.match(target) or lowered.startswith(("http://", "https://")):
        return "open_url", {"url": target if "://" in target else "https://" + target}
    if PATHISH.match(target) or "/" in target or "\\" in target:
        return "open_path", {"path": target}
    # A document keeps its real name; an app name is matched case-insensitively.
    # (``search``, not ``match``: the extension sits at the end of the name.)
    if FILENAME.search(target) and not APP_EXEC.search(target):
        return "open_path", {"path": target}
    return "open_app", {"target": lowered if lowered in APP_ALIASES else target}


@_rule(r"^(?:switch|change)\s+(?:to|over to)\s+(?P<title>.+)$")
@_rule(r"^(?:focus|activate|bring up|show me)\s+(?P<title>.+?)(?:\s+window)?$")
@_rule(r"^go\s+to\s+(?P<title>.+?)(?:\s+window)?$")
def _focus(match: re.Match) -> tuple[str, dict[str, Any]]:
    return "focus_window", {"title": _clean_target(match.group("title"))}


@_rule(r"^(?:close|quit|exit)\s+(?P<title>.+?)(?:\s+window)?$")
def _close(match: re.Match) -> tuple[str, dict[str, Any]] | None:
    title = _clean_target(match.group("title"))
    if title.lower() in ("the window", "window", "this", "it"):
        return "close_window", {"title": ""}
    return "close_window", {"title": title}


@_rule(r"^(?:minimi[sz]e|hide)\s+(?P<title>.+?)(?:\s+window)?$")
def _minimize(match: re.Match) -> tuple[str, dict[str, Any]]:
    return "minimize_window", {"title": _clean_target(match.group("title"))}


@_rule(r"^(?:maximi[sz]e|full\s?screen|enlarge)\s*(?P<title>.*?)(?:\s+window)?$")
def _maximize(match: re.Match) -> tuple[str, dict[str, Any]]:
    return "maximize_window", {"title": _clean_target(match.group("title") or "")}


@_rule(r"^(?:next window|switch windows?|alt[\s+]?tab|window switch)$")
def _switch(_match: re.Match) -> tuple[str, dict[str, Any]]:
    return "switch_window", {}


@_rule(r"^(?:what(?:'s| is)? (?:open|running)(?: right now)?|list (?:the )?windows|"
       r"show (?:me )?(?:the )?open windows)$")
def _windows(_match: re.Match) -> tuple[str, dict[str, Any]]:
    return "list_windows", {}


@_rule(r"^(?:what(?:'s| is)? (?:in focus|the active window|focused)|active window|current window)$")
def _active(_match: re.Match) -> tuple[str, dict[str, Any]]:
    return "active_window", {}


# ── audio and display ───────────────────────────────────────────────────────
@_rule(r"^(?:mute|silence)(?:\s+(?:the\s+)?(?:sound|audio|volume|pc|computer|it))?$")
def _mute(_match: re.Match) -> tuple[str, dict[str, Any]]:
    return "set_volume", {"mute": "on"}


@_rule(r"^unmute(?:\s+(?:it|the\s+sound|the\s+audio))?$")
def _unmute(_match: re.Match) -> tuple[str, dict[str, Any]]:
    return "set_volume", {"mute": "off"}


@_rule(r"^toggle\s+mute$")
def _mute_toggle(_match: re.Match) -> tuple[str, dict[str, Any]]:
    return "set_volume", {"mute": "toggle"}


@_rule(r"^(?:what(?:'s| is)?(?: the)? volume|volume level|how loud is it)$")
def _get_volume(_match: re.Match) -> tuple[str, dict[str, Any]]:
    return "get_volume", {}


@_rule(r"^(?:volume|sound)\s+(?P<direction>up|down|louder|quieter|higher|lower)"
       r"(?:\s+(?:by\s+)?(?P<amount>\d{1,3}))?$")
@_rule(r"^(?:turn\s+(?:it|the\s+volume)\s+)(?P<direction>up|down|louder|quieter)$")
def _volume_nudge(match: re.Match) -> tuple[str, dict[str, Any]]:
    step = int(match.groupdict().get("amount") or 10)
    direction = match.group("direction")
    return "nudge_volume", {"delta": step if direction in ("up", "louder", "higher") else -step}


@_rule(r"^(?:set\s+)?(?:the\s+)?volume\s*(?:to|=|at)?\s*(?P<level>[\w\s%]+)$")
@_rule(r"^(?:turn\s+(?:the\s+)?volume\s+(?:to|up to|down to))\s*(?P<level>[\w\s%]+)$")
def _volume(match: re.Match) -> tuple[str, dict[str, Any]] | None:
    text = match.group("level")
    if re.search(r"\b(up|down|louder|quieter|higher|lower)\b", text, re.IGNORECASE):
        return None                     # handled by the nudge rule above
    level = _number(text)
    if level is None:
        return None
    return "set_volume", {"level": max(0, min(100, level))}


@_rule(r"^(?:what(?:'s| is)?(?: the)? brightness|screen brightness)$")
def _get_brightness(_match: re.Match) -> tuple[str, dict[str, Any]]:
    return "get_brightness", {}


@_rule(r"^(?:set\s+)?(?:the\s+)?(?:screen\s+)?brightness\s*(?:to|=|at)?\s*(?P<level>[\w\s%]+)$")
def _brightness(match: re.Match) -> tuple[str, dict[str, Any]] | None:
    level = _number(match.group("level"))
    if level is None:
        return None
    if level <= 10 and "percent" not in match.group("level") and level != 100:
        level *= 10                      # “brightness 5” almost always means 50
    return "set_brightness", {"level": max(5, min(100, level))}


@_rule(r"^(?:dim|darken)\s+(?:the\s+)?(?:screen|display)?\s*(?P<level>\d{1,3})?$")
def _dim(match: re.Match) -> tuple[str, dict[str, Any]]:
    level = int(match.group("level") or 25)
    return "set_brightness", {"level": max(5, min(60, level))}


@_rule(r"^(?:brighten|bright)\s+(?:the\s+)?(?:screen|display)?\s*(?P<level>\d{1,3})?$")
def _brighten(match: re.Match) -> tuple[str, dict[str, Any]]:
    return "set_brightness", {"level": min(100, int(match.group("level") or 90))}


# ── keyboard and mouse ──────────────────────────────────────────────────────
@_rule(r"^(?:type|write|enter)\s+(?P<text>.+?)\s+(?:in|into|inside)\s+(?P<target>.+)$")
def _type_into(match: re.Match) -> tuple[str, dict[str, Any]]:
    return "type_into", {"text": match.group("text").strip(),
                         "target": _clean_title(match.group("target"))}


@_rule(r"^(?:type|write|enter|say)\s+(?P<text>.+)$")
def _type(match: re.Match) -> tuple[str, dict[str, Any]]:
    text = re.sub(r"^(?:this|out|the following)\s*[:,-]?\s*", "", match.group("text"),
                  flags=re.IGNORECASE)
    return "type_text", {"text": text}


@_rule(r"^(?:press|hit|send|key)\s+(?P<keys>[\w\s+,\-]+?)(?:\s+key(?:s)?)?$")
@_rule(r"^(?:shortcut|hotkey)\s+(?P<keys>[\w\s+,\-]+)$")
def _keys(match: re.Match) -> tuple[str, dict[str, Any]] | None:
    keys = normalize_keys(match.group("keys"))
    return ("press_keys", {"keys": keys}) if keys else None


@_rule(r"^(?:scroll|page)\s+(?P<direction>up|down)(?:\s+(?P<amount>\d{1,3}))?$")
def _scroll(match: re.Match) -> tuple[str, dict[str, Any]]:
    amount = int(match.group("amount") or 3)
    return "scroll", {"amount": amount if match.group("direction") == "up" else -amount}


@_rule(r"^(?P<clicktype>double|triple|right|middle|left)?\s*click(?:s)?(?:\s+(?P<clicks>\d+)\s+times)?$")
def _click(match: re.Match) -> tuple[str, dict[str, Any]]:
    kind = (match.group("clicktype") or "left").lower()
    clicks = int(match.group("clicks") or 0) or (2 if kind == "double" else (3 if kind == "triple" else 1))
    button = {"double": "left", "triple": "left", "right": "right", "middle": "middle"}.get(kind, "left")
    return "click", {"button": button, "clicks": min(clicks, 3)}


@_rule(r"^move\s+(?:the\s+)?(?:mouse|pointer|cursor)\s+(?:to\s+)?"
       r"(?P<x>-?\d{1,5})\s*[, ]\s*(?P<y>-?\d{1,5})$")
def _move(match: re.Match) -> tuple[str, dict[str, Any]]:
    return "move_mouse", {"x": int(match.group("x")), "y": int(match.group("y"))}


@_rule(r"^(?:where(?:'s| is) (?:the )?(?:mouse|pointer|cursor)|pointer position)$")
def _pointer(_match: re.Match) -> tuple[str, dict[str, Any]]:
    return "pointer_position", {}


# ── processes and shell ─────────────────────────────────────────────────────
@_rule(r"^(?:kill|stop|force quit|end|terminate)\s+(?:the\s+)?(?P<target>.+?)(?:\s+process)?$")
def _kill(match: re.Match) -> tuple[str, dict[str, Any]]:
    target = _clean_target(match.group("target"))
    return "kill_process", {"target": target, "force": target.lower() not in ("", "it")}


@_rule(r"^(?:what(?:'s| is)? (?:running|using (?:the )?cpu)|list processes|top processes|"
       r"busiest processes|running processes)$")
def _processes(_match: re.Match) -> tuple[str, dict[str, Any]]:
    return "list_processes", {"limit": 12}


@_rule(r"^(?:run|execute)\s+(?:the\s+)?(?:command|shell|terminal)?\s*[:,-]?\s*(?P<command>.+)$")
@_rule(r"^in the terminal,?\s+(?:run\s+)?(?P<command>.+)$")
def _shell(match: re.Match) -> tuple[str, dict[str, Any]]:
    return "run_command", {"command": match.group("command").strip()}


# ── files ───────────────────────────────────────────────────────────────────
@_rule(r"^(?:list|show)\s+(?:me\s+)?(?:the\s+)?(?:files?|folders?|contents?)\s+"
       r"(?:in|of|at)\s+(?P<path>.+)$")
@_rule(r"^what(?:'s| is) in\s+(?P<path>.+)$")
def _list_dir(match: re.Match) -> tuple[str, dict[str, Any]]:
    return "list_dir", {"path": _clean_target(match.group("path"))}


@_rule(r"^(?:create|make|new)\s+(?:a\s+)?(?:new\s+)?folder\s+(?:called\s+|named\s+|at\s+)?(?P<path>.+)$")
@_rule(r"^mkdir\s+(?P<path>.+)$")
def _mkdir(match: re.Match) -> tuple[str, dict[str, Any]]:
    return "make_dir", {"path": _clean_target(match.group("path"))}


@_rule(r"^copy\s+(?P<text>.+?)\s+to\s+(?:my\s+|the\s+)?clipboard$")
def _clip_write(match: re.Match) -> tuple[str, dict[str, Any]]:
    return "clipboard_write", {"text": match.group("text").strip()}


@_rule(r"^(?:copy|duplicate)\s+(?P<source>.+?)\s+(?:to|into)\s+(?P<destination>.+)$")
def _copy(match: re.Match) -> tuple[str, dict[str, Any]]:
    return "copy_path", {"source": _clean_target(match.group("source")),
                         "destination": _clean_target(match.group("destination"))}


@_rule(r"^(?:move|rename)\s+(?P<source>.+?)\s+(?:to|into|as)\s+(?P<destination>.+)$")
def _move(match: re.Match) -> tuple[str, dict[str, Any]]:
    return "move_path", {"source": _clean_target(match.group("source")),
                         "destination": _clean_target(match.group("destination"))}


@_rule(r"^(?:delete|remove|trash|bin)\s+(?P<path>.+)$")

def _delete(match: re.Match) -> tuple[str, dict[str, Any]]:
    path = _clean_target(match.group("path"))
    path = re.sub(r"^(?:the|that|this)\s+", "", path)
    return "delete_path", {"path": path}


@_rule(r"^(?:zip|compress|archive)\s+(?P<source>.+?)(?:\s+(?:to|into|as)\s+(?P<destination>.+))?$")
def _zip(match: re.Match) -> tuple[str, dict[str, Any]]:
    return "zip_path", {"source": _clean_target(match.group("source")),
                        "destination": _clean_target(match.group("destination") or "")}


# ── screen, clipboard, power ────────────────────────────────────────────────
@_rule(r"^(?:read|scan|ocr)\s+(?:my\s+|the\s+)?screen$")
@_rule(r"^what(?:'s| is) on (?:my|the) screen$")
def _read_screen(_match: re.Match) -> tuple[str, dict[str, Any]]:
    return "read_screen", {}


@_rule(r"^(?:take\s+a\s+)?screenshot$")
@_rule(r"^capture\s+(?:the\s+)?screen$")
def _screenshot(_match: re.Match) -> tuple[str, dict[str, Any]]:
    return "screenshot", {}


@_rule(r"^(?:read|what(?:'s| is) (?:on|in))\s+(?:my\s+)?clipboard$")
def _clip_read(_match: re.Match) -> tuple[str, dict[str, Any]]:
    return "clipboard_read", {}


@_rule(r"^(?:lock|lock up|secure)\s+(?:my\s+|the\s+)?(?:screen|computer|pc|machine)$")
def _lock(_match: re.Match) -> tuple[str, dict[str, Any]]:
    return "power", {"action": "lock"}


@_rule(r"^(?:go to\s+)?sleep(?:\s+(?:mode|now|the\s+pc))?$")
@_rule(r"^suspend$")
def _sleep(_match: re.Match) -> tuple[str, dict[str, Any]]:
    return "power", {"action": "sleep"}


@_rule(r"^(?:shut\s?down|power\s?off|turn off)\s*(?:my\s+|the\s+)?(?:computer|pc|machine|system)?$")
def _shutdown(_match: re.Match) -> tuple[str, dict[str, Any]]:
    return "power", {"action": "shutdown"}


@_rule(r"^(?:restart|reboot)\s*(?:my\s+|the\s+)?(?:computer|pc|machine|system)?$")
def _restart(_match: re.Match) -> tuple[str, dict[str, Any]]:
    return "power", {"action": "restart"}


@_rule(r"^(?:log\s?out|sign\s?out)\s*(?:of\s+my\s+(?:account|session))?$")
def _logout(_match: re.Match) -> tuple[str, dict[str, Any]]:
    return "power", {"action": "signout"}


# ── assistant ───────────────────────────────────────────────────────────────
@_rule(r"^(?:notify me|send (?:me )?a notification)(?:\s+(?:that|saying|about))?\s*[:,-]?\s*(?P<text>.+)$")
def _notify(match: re.Match) -> tuple[str, dict[str, Any]]:
    return "notify", {"title": "JARVIS", "text": match.group("text").strip()}


@_rule(r"^(?:say|speak)\s+(?P<text>.+)$")
def _say(match: re.Match) -> tuple[str, dict[str, Any]]:
    return "speak", {"text": match.group("text").strip()}


@_rule(r"^wait(?:\s+(?:for\s+)?(?P<seconds>\d{1,3})\s*(?:seconds|secs|s|minutes|mins)?)?$")
def _wait(match: re.Match) -> tuple[str, dict[str, Any]]:
    return "wait", {"seconds": int(match.group("seconds") or 1)}


@_rule(r"^(?:open|go to)\s+(?P<url>(?:https?://|www\.)\S+)$")
def _url(match: re.Match) -> tuple[str, dict[str, Any]]:
    url = match.group("url")
    return "open_url", {"url": url if "://" in url else "https://" + url}


# ── files: search, read, append, duplicate, extract, lookups ────────────────
@_rule(r"^(?:find|locate|search\s+for)\s+(?:all\s+)?(?:files?\s+)?(?:called\s+|named\s+|matching\s+)?"
       r"(?P<pattern>[\w*.?~-]+?)(?:\s+files?)?(?:\s+(?:in|under|inside)\s+(?P<root>.+))?$")
def _find_files(match: re.Match) -> tuple[str, dict[str, Any]]:
    args: dict[str, Any] = {"pattern": match.group("pattern").strip("\"'")}
    if match.group("root"):
        args["root"] = _clean_target(match.group("root"))
    return "find_files", args


@_rule(r"^(?:read|show\s+me)\s+(?:the\s+)?(?:file\s+)?(?P<path>[~./\\\w-]+\.\w{1,6})"
       r"(?:\s+lines?\s+(?P<start>\d+)(?:\s*(?:-|to|\.\.)\s*(?P<end>\d+))?)?$")
def _read_file(match: re.Match) -> tuple[str, dict[str, Any]]:
    args: dict[str, Any] = {"path": _clean_target(match.group("path"))}
    if match.group("start"):
        args["start"] = int(match.group("start"))
        if match.group("end"):
            args["count"] = max(1, int(match.group("end")) - int(match.group("start")) + 1)
    return "read_file", args


@_rule(r"^(?:add|append)\s+(?P<content>.+?)\s+to\s+(?:the\s+end\s+of\s+)?"
       r"(?P<path>(?:[~/][\w./\\-]*)|(?:[\w-]+\.\w{1,6}))$")
def _append_file(match: re.Match) -> tuple[str, dict[str, Any]]:
    return "append_file", {"path": _clean_target(match.group("path")),
                           "content": match.group("content").strip()}


@_rule(r"^(?:duplicate|make\s+(?:a\s+)?copy\s+of)\s+(?P<path>.+?)"
       r"(?:\s+(?:to|into)\s+(?P<destination>.+))?$")
def _duplicate(match: re.Match) -> tuple[str, dict[str, Any]]:
    return "duplicate_path", {"path": _clean_target(match.group("path")),
                              "destination": _clean_target(match.group("destination") or "")}


@_rule(r"^(?:extract|unzip|unpack)\s+(?P<path>.+?)(?:\s+(?:to|into)\s+(?P<destination>.+))?$")
def _unzip(match: re.Match) -> tuple[str, dict[str, Any]]:
    return "unzip_path", {"path": _clean_target(match.group("path")),
                          "destination": _clean_target(match.group("destination") or "")}


@_rule(r"^(?:how\s+much\s+)?(?:disk|drive|storage)\s+space(?:\s+(?:is\s+)?(?:left|free|available))?"
       r"(?:\s+(?:on|in)\s+(?P<path>.+))?$")
@_rule(r"^free\s+space(?:\s+(?:on|in)\s+(?P<path>.+))?$")
def _disk_usage(match: re.Match) -> tuple[str, dict[str, Any]]:
    return "disk_usage", {"path": _clean_target(match.group("path") or "")}


@_rule(r"^(?:how\s+big\s+is|size\s+of|info\s+(?:on|about)|details\s+of)\s+(?P<path>.+)$")
def _file_info(match: re.Match) -> tuple[str, dict[str, Any]]:
    return "file_info", {"path": _clean_target(match.group("path"))}


@_rule(r"^(?:is|are)\s+(?P<name>[\w .-]{2,40}?)\s+(?:running|open|up)$")
def _find_process(match: re.Match) -> tuple[str, dict[str, Any]]:
    return "find_process", {"name": match.group("name").strip()}


@_rule(r"^(?:what|which)\s+(?:apps|applications|programs)\s+(?:are\s+)?(?:open|running)$")
def _list_apps(_match: re.Match) -> tuple[str, dict[str, Any]]:
    return "list_apps", {}


@_rule(r"^(?:what\s+did\s+i\s+copy(?:\s+earlier)?|clipboard\s+history|"
       r"show\s+(?:me\s+)?(?:my\s+)?clipboard\s+history)$")
def _clip_history(_match: re.Match) -> tuple[str, dict[str, Any]]:
    return "clipboard_history", {}


@_rule(r"^(?:put\s+back|restore)\s+(?:what\s+i\s+copied|the\s+last\s+thing\s+i\s+copied|"
       r"my\s+clipboard|the\s+clipboard)$")
def _clip_restore(_match: re.Match) -> tuple[str, dict[str, Any]]:
    return "clipboard_restore", {"index": 1}


@_rule(r"^empty\s+(?:the\s+|my\s+)?(?:trash|recycle\s+bin|bin|wastebasket)$")
def _empty_trash(_match: re.Match) -> tuple[str, dict[str, Any]]:
    return "empty_trash", {}


@_rule(r"^(?:start|show)\s+(?:the\s+)?screen\s?saver$")
def _screensaver(_match: re.Match) -> tuple[str, dict[str, Any]]:
    return "power", {"action": "screensaver"}


# ════════════════════════════════════════════════════════════════════════════
#  Level 2 — plans
# ════════════════════════════════════════════════════════════════════════════

@dataclass
class Plan:
    """A parsed, runnable sequence of steps."""

    request: str
    steps: list[Step] = field(default_factory=list)
    leftovers: list[str] = field(default_factory=list)
    name: str = ""
    source: str = "parsed"
    notes: list[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return not self.leftovers

    @property
    def size(self) -> int:
        return sum(step.times for step in self.steps)

    @property
    def large(self) -> bool:
        return self.size >= LARGE_STEP_THRESHOLD

    def outline(self, registry: ActionRegistry | None = None, show_risk: bool = True) -> str:
        lines = [f"Plan — {self.size} step(s)" + (f" for “{self.name}”" if self.name else "")]
        for index, step in enumerate(self.steps, start=1):
            action = registry.get(step.action) if registry else None
            risk = f"  [{RISK_BADGE[action.risk] if action else '?'}]" if show_risk else ""
            lines.append(f"  {index}. {step.describe(registry)}{risk}")
        if self.leftovers:
            lines.append("Not understood:")
            lines += [f"  ? {item}" for item in self.leftovers]
        if self.notes:
            lines += [f"  · {note}" for note in self.notes]
        return "\n".join(lines)

    def to_routine(self, name: str = "") -> Routine:
        return Routine(name=name or self.name or "planned routine",
                       steps=[Step(step.action, dict(step.args), step.note, step.optional, step.times)
                              for step in self.steps],
                       description=f"Planned from: “{self.request}”",
                       source=self.source)


def split_steps(text: str) -> list[str]:
    """Break a request into clauses: “open chrome then set volume to 20” → 2 clauses."""
    if STEP_MARKER.search(text):
        pieces = [piece for piece in STEP_MARKER.split(text) if piece.strip()]
        if len(pieces) > 1:
            return pieces
    pattern = "|".join(SEQUENCE_MARKERS)
    pieces = [piece.strip(" ,.") for piece in re.split(pattern, text, flags=re.IGNORECASE)]
    return [piece for piece in pieces if piece]


def parse(text: str) -> tuple[str, dict[str, Any]] | None:
    """Parse one clause into ``(action_name, args)``, or ``None`` if unknown."""
    cleaned = (text or "").strip().strip(".?!").strip()
    if not cleaned:
        return None
    # Match case-insensitively but *keep the original wording* in the captured
    # values: “open ~/Downloads” must not become “~/downloads”, and “type Hello”
    # must type Hello, not hello.
    cleaned = re.sub(r"^(?:jarvis|hey jarvis|ok jarvis|please)\s+", "", cleaned, flags=re.IGNORECASE)
    for pattern, builder in _RULES:
        match = re.match(pattern, cleaned, re.IGNORECASE)
        if not match:
            continue
        built = builder(match)
        if built:
            action, args = built
            return action, {key: value for key, value in args.items() if value is not None}
    return None


class Planner:
    """Parses requests into plans, with an optional language-model fallback."""

    def __init__(self, registry: ActionRegistry, llm: Any = None, max_steps: int = 24) -> None:
        self.registry = registry
        self.llm = llm
        self.max_steps = max_steps

    # ── parsing ──────────────────────────────────────────────────────────
    def parse(self, clause: str) -> tuple[str, dict[str, Any]] | None:
        return parse(clause)

    def plan(self, request: str, use_llm: bool = True) -> Plan:
        plan = Plan(request=request.strip())
        clauses = split_steps(request)
        for clause in clauses:
            parsed = parse(clause)
            if parsed:
                action, args = parsed
                plan.steps.append(Step(action=action, args=args))
            else:
                plan.leftovers.append(clause.strip())

        if plan.leftovers and use_llm and self.llm is not None:
            repaired = self._llm_plan("\n".join(plan.leftovers))
            if repaired:
                plan.steps.extend(repaired)
                plan.notes.append(f"({len(repaired)} step(s) came from the language model)")
                plan.leftovers = []

        if not plan.name:
            plan.name = self.guess_name(plan)
        plan.steps = plan.steps[: self.max_steps]
        return plan

    def guess_name(self, plan: Plan) -> str:
        """A short, speakable name: “open chrome + volume + read screen routine”."""
        labels: list[str] = []
        for step in plan.steps[:3]:
            for key in ("target", "title", "command", "path", "action"):
                value = str(step.args.get(key) or "").strip()
                if not value:
                    continue
                words = [word for word in re.sub(r"[^a-zA-Z0-9]+", " ", value).lower().split()
                         if not word.isdigit()]
                if words:
                    labels.append(words[0])
                break
            else:
                labels.append(step.action.replace("_", " "))
        seen: list[str] = []
        for item in labels:
            if item and item not in seen:
                seen.append(item)
        return (" + ".join(seen[:3]) + " routine") if seen else "routine"

    # ── LLM-assisted planning ────────────────────────────────────────────
    def catalogue(self, limit: int = 40) -> str:
        lines = []
        for action in sorted(self.registry.actions.values(), key=lambda item: item.name)[:limit]:
            params = ", ".join(action.params) or "-"
            lines.append(f"- {action.name}({params}): {action.description}")
        return "\n".join(lines)

    def _llm_plan(self, request: str) -> list[Step]:
        if self.llm is None:
            return []
        system = (
            "You convert a user's request into a JSON plan for a desktop assistant.\n"
            "Reply with ONLY JSON: {\"steps\":[{\"action\":\"name\",\"args\":{...}}]}\n"
            "Use only these actions:\n" + self.catalogue() + "\n"
            "Rules: never invent action names; omit a step you cannot express; "
            "prefer several small deterministic steps over one clever one; "
            "use 'wait' between GUI steps that need time; for anything destructive, "
            "still emit the step (the app asks the user for confirmation)."
        )
        try:
            reply = self.llm(request, system)
        except Exception:
            return []
        text = getattr(reply, "text", "") or ""
        data = _extract_json(text)
        if not isinstance(data, dict):
            return []
        steps: list[Step] = []
        for raw in (data.get("steps") or [])[: self.max_steps]:
            if not isinstance(raw, dict):
                continue
            name = str(raw.get("action", "")).strip()
            if not self.registry.get(name):
                continue                        # an invented action is dropped, not run
            args = raw.get("args") if isinstance(raw.get("args"), dict) else {}
            steps.append(Step(action=name, args=args, note=str(raw.get("note", ""))[:80],
                              optional=bool(raw.get("optional"))))
        return steps

    # ── execution ────────────────────────────────────────────────────────
    def run(self, plan: Plan, source: str = "plan", approved: bool = False,
            stop_on_failure: bool = True) -> Any:
        from .routines import RoutineRunner

        runner = RoutineRunner(self.registry)
        routine = plan.to_routine()
        return runner.run(routine, source=source, approved_plan=approved,
                          stop_on_failure=stop_on_failure)


def _extract_json(text: str) -> Any:
    """Pull the first JSON object out of an LLM reply."""
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE)
    try:
        return json.loads(text)
    except ValueError:
        pass
    match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except ValueError:
            return None
    return None
