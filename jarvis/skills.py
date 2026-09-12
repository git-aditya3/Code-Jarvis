"""
The skill layer — JARVIS's offline capability set.

Every skill is a small, testable class: a list of regex patterns (with weights
so that specific intents beat generic ones) and a ``run`` method that returns a
:class:`SkillResult`. Skills talk to the outside world only through a
:class:`~jarvis.host.Host`, so the whole layer is usable from the desktop app,
the CLI and the test suite.

Design rule: **never invent data**. If a skill cannot do something offline it
says so and suggests the LLM route (or the exact command the user can run).
"""

from __future__ import annotations

import ast
import math
import operator as op
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

try:
    import requests
except Exception:  # pragma: no cover
    requests = None  # type: ignore[assignment]

from .config import VERSION, Settings
from .host import Host
from .memory import Memory

CITY_TIMEZONES = {
    "tiruchirappalli": "Asia/Kolkata", "trichy": "Asia/Kolkata", "chennai": "Asia/Kolkata",
    "mumbai": "Asia/Kolkata", "delhi": "Asia/Kolkata", "new delhi": "Asia/Kolkata",
    "bengaluru": "Asia/Kolkata", "bangalore": "Asia/Kolkata", "hyderabad": "Asia/Kolkata",
    "kolkata": "Asia/Kolkata", "pune": "Asia/Kolkata", "kochi": "Asia/Kolkata",
    "london": "Europe/London", "paris": "Europe/Paris", "berlin": "Europe/Berlin",
    "madrid": "Europe/Madrid", "rome": "Europe/Rome", "amsterdam": "Europe/Amsterdam",
    "moscow": "Europe/Moscow", "dubai": "Asia/Dubai", "singapore": "Asia/Singapore",
    "tokyo": "Asia/Tokyo", "seoul": "Asia/Seoul", "shanghai": "Asia/Shanghai",
    "beijing": "Asia/Shanghai", "hong kong": "Asia/Hong_Kong", "sydney": "Australia/Sydney",
    "melbourne": "Australia/Melbourne", "auckland": "Pacific/Auckland",
    "new york": "America/New_York", "boston": "America/New_York", "washington": "America/New_York",
    "chicago": "America/Chicago", "denver": "America/Denver", "los angeles": "America/Los_Angeles",
    "san francisco": "America/Los_Angeles", "seattle": "America/Los_Angeles",
    "toronto": "America/Toronto", "vancouver": "America/Vancouver",
    "sao paulo": "America/Sao_Paulo", "mexico city": "America/Mexico_City",
    "johannesburg": "Africa/Johannesburg", "cairo": "Africa/Cairo", "nairobi": "Africa/Nairobi",
}

SAFE_APPS = {
    # friendly name: per-platform commands (None = not available on that OS)
    "notepad": {"win": ["notepad.exe"], "linux": ["gedit", "kate", "mousepad", "xed"], "mac": ["open", "-a", "TextEdit"]},
    "calculator": {"win": ["calc.exe"], "linux": ["gnome-calculator", "kcalc", "galculator"], "mac": ["open", "-a", "Calculator"]},
    "terminal": {"win": ["cmd.exe"], "linux": ["x-terminal-emulator", "gnome-terminal", "konsole", "xterm"], "mac": ["open", "-a", "Terminal"]},
    "command prompt": {"win": ["cmd.exe"], "linux": [], "mac": []},
    "powershell": {"win": ["powershell.exe"], "linux": [], "mac": []},
    "explorer": {"win": ["explorer.exe"], "linux": ["xdg-open", "."], "mac": ["open", "."]},
    "file explorer": {"win": ["explorer.exe"], "linux": ["xdg-open", "."], "mac": ["open", "."]},
    "task manager": {"win": ["taskmgr.exe"], "linux": ["gnome-system-monitor", "ksysguard"], "mac": ["open", "-a", "Activity Monitor"]},
    "vs code": {"win": ["code"], "linux": ["code"], "mac": ["open", "-a", "Visual Studio Code"]},
    "vscode": {"win": ["code"], "linux": ["code"], "mac": ["open", "-a", "Visual Studio Code"]},
    "browser": {"win": ["explorer.exe", "https://www.google.com"], "linux": ["xdg-open", "https://www.google.com"], "mac": ["open", "https://www.google.com"]},
    "chrome": {"win": ["chrome"], "linux": ["google-chrome", "chromium"], "mac": ["open", "-a", "Google Chrome"]},
    "edge": {"win": ["msedge"], "linux": ["microsoft-edge"], "mac": ["open", "-a", "Microsoft Edge"]},
    "firefox": {"win": ["firefox"], "linux": ["firefox"], "mac": ["open", "-a", "Firefox"]},
    "spotify": {"win": ["spotify"], "linux": ["spotify"], "mac": ["open", "-a", "Spotify"]},
    "paint": {"win": ["mspaint.exe"], "linux": [], "mac": []},
    "snipping tool": {"win": ["snippingtool.exe"], "linux": [], "mac": []},
}


# ════════════════════════════════════════════════════════════════════════════
#  Core types
# ════════════════════════════════════════════════════════════════════════════

@dataclass
class SkillResult:
    """What a skill hands back to the core."""

    text: str = ""
    speak: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    followups: list[str] = field(default_factory=list)
    ok: bool = True
    error: str = ""

    def spoken(self, limit: int = 320) -> str:
        """Short version for text-to-speech."""
        if self.speak:
            return self.speak
        text = self.text
        # strip code blocks and markdown so the voice does not read punctuation soup
        text = re.sub(r"```.*?```", " (code block omitted) ", text, flags=re.DOTALL)
        text = re.sub(r"[*_`#>|]", "", text)
        text = re.sub(r"\s+", " ", text).strip()
        if len(text) <= limit:
            return text
        cut = text[:limit]
        return cut[: cut.rfind(".") + 1] if "." in cut[limit // 2:] else cut + "…"


@dataclass
class SkillContext:
    """Everything a skill may use."""

    settings: Settings
    memory: Memory
    host: Host
    llm: Callable[[str, str], Any] | None = None
    state: dict[str, Any] = field(default_factory=dict)

    def ask_llm(self, question: str, extra_system: str = "") -> str:
        """Ask the configured LLM (empty string when unavailable / failed)."""
        if not self.llm:
            return ""
        try:
            reply = self.llm(question, extra_system)
        except Exception:
            return ""
        return getattr(reply, "text", "") or ""

    def llm_ready(self) -> bool:
        return bool(self.state.get("llm_ready"))

    @property
    def units(self) -> str:
        return str(self.settings.get("units", "metric"))

    @property
    def city(self) -> str:
        return str(self.settings.get("city", "Tiruchirappalli"))


class Skill:
    """Base class: declare patterns, weight them, implement ``run``."""

    name: str = "skill"
    title: str = "Skill"
    description: str = ""
    examples: Sequence[str] = ()
    priority: int = 0
    patterns: Sequence[tuple[str, float]] = ()

    # ── matching ─────────────────────────────────────────────────────────
    def match(self, text: str) -> float:
        lowered = text.lower().strip()
        best = 0.0
        for pattern, weight in self.patterns:
            if re.search(pattern, lowered):
                best = max(best, weight)
        return best

    # ── execution ────────────────────────────────────────────────────────
    def run(self, text: str, ctx: SkillContext) -> SkillResult:  # pragma: no cover - abstract
        raise NotImplementedError

    def help_line(self) -> str:
        example = f"  e.g. “{self.examples[0]}”" if self.examples else ""
        return f"• {self.title} — {self.description}{example}"


class SkillRegistry:
    """Holds skills and routes an utterance to the best match."""

    def __init__(self, skills: Iterable[Skill] | None = None) -> None:
        self.skills: list[Skill] = list(skills or [])
        self.last_skill: Skill | None = None

    def register(self, skill: Skill) -> None:
        self.skills.append(skill)

    def route(self, text: str) -> tuple[Skill | None, float]:
        best: tuple[Skill | None, float] = (None, 0.0)
        for skill in self.skills:
            try:
                score = skill.match(text)
            except Exception:
                continue
            if score <= 0:
                continue
            score += skill.priority * 0.001
            if score > best[1]:
                best = (skill, score)
        self.last_skill = best[0]
        return best

    def handle(self, text: str, ctx: SkillContext) -> SkillResult | None:
        skill, score = self.route(text)
        if not skill or score < 0.35:
            return None
        try:
            result = skill.run(text, ctx)
        except Exception as exc:  # a broken skill must never take the app down
            return SkillResult(
                text=f"“{skill.title}” hit an error: {exc}",
                speak=f"Sorry, the {skill.title} skill failed.",
                ok=False,
                error=str(exc),
                data={"skill": skill.name},
            )
        result.data.setdefault("skill", skill.name)
        result.data.setdefault("score", score)
        return result

    def help_text(self) -> str:
        lines = [f"JARVIS v{VERSION} — {len(self.skills)} skills online."]
        for skill in sorted(self.skills, key=lambda s: s.title):
            lines.append(skill.help_line())
        lines.append("\nAdd an LLM key in Settings for open-ended questions beyond these.")
        return "\n".join(lines)

    def find(self, name: str) -> Skill | None:
        lowered = name.strip().lower()
        for skill in self.skills:
            if lowered in (skill.name.lower(), skill.title.lower()):
                return skill
        return None


# ════════════════════════════════════════════════════════════════════════════
#  Identity & help
# ════════════════════════════════════════════════════════════════════════════

class IdentitySkill(Skill):
    name = "identity"
    title = "Identity"
    description = "who JARVIS is and what it is running on"
    examples = ("who are you", "what can you do")
    patterns = ((r"\b(who|what) are you\b", 0.9), (r"\byour name\b", 0.8), (r"\bintroduce yourself\b", 0.9))

    def run(self, text: str, ctx: SkillContext) -> SkillResult:
        name = ctx.settings.get("user_name") or "sir"
        brain = ctx.state.get("provider_label", "offline skills")
        lines = [
            f"I am JARVIS — your local assistant, {name}.",
            f"Brain: {brain}. Machine: {platform.system()} {platform.release()}.",
            f"I keep your notes, tasks and facts on this computer only ({ctx.memory.path}).",
        ]
        return SkillResult(text="\n".join(lines),
                           speak=f"I am JARVIS, running locally on your {platform.system()} machine.")


class HelpSkill(Skill):
    name = "help"
    title = "Help"
    description = "list everything JARVIS can do"
    examples = ("help", "what can you do")
    patterns = (
        (r"^\s*(help|/help)\s*$", 0.95),
        (r"\bwhat can you (do|help)\b", 0.97),
        (r"\b(list|show) (your )?(skills|commands|capabilities)\b", 0.9),
        (r"\bhow do i use you\b", 0.85),
    )

    def __init__(self, registry_getter: Callable[[], SkillRegistry] | None = None) -> None:
        self._get_registry = registry_getter

    def run(self, text: str, ctx: SkillContext) -> SkillResult:
        registry = self._get_registry() if self._get_registry else None
        body = registry.help_text() if registry else "Skill list unavailable."
        return SkillResult(text=body, speak="Here is what I can do. Have a look at the console.")


class StatusSkill(Skill):
    name = "status"
    title = "Diagnostics"
    description = "provider, voice and memory status"
    examples = ("status", "diagnostics")
    patterns = (
        (r"\b(diagnostics|status report|self test|self-test)\b", 0.9),
        (r"^\s*(status|/status)\s*$", 0.85),
    )

    def run(self, text: str, ctx: SkillContext) -> SkillResult:
        caps = ctx.state.get("voice_status", {})
        stats = ctx.memory.stats()
        lines = [
            f"JARVIS v{VERSION} — diagnostics",
            f"Brain: {ctx.state.get('provider_label', 'offline')}",
            f"Voice output: {caps.get('tts', 'unknown')}",
            f"Voice input: {caps.get('stt', 'unknown')}",
            f"Wake word: {caps.get('wake', 'off')}",
            f"Memory: {stats['notes']} notes, {stats['tasks_open']} open tasks, {stats['facts']} facts, {stats['turns']} turns",
            f"Data folder: {ctx.memory.path.parent}",
        ]
        return SkillResult(text="\n".join(lines),
                           speak="Diagnostics are on screen. " + str(caps.get("summary", "")))


# ════════════════════════════════════════════════════════════════════════════
#  Time, maths, units
# ════════════════════════════════════════════════════════════════════════════

class TimeSkill(Skill):
    name = "time"
    title = "Clock"
    description = "time, date and time zones"
    examples = ("what's the time", "date in Tokyo")
    patterns = (
        (r"\bwhat('s| is) the (time|date|day)\b", 0.9),
        (r"\b(current|time|today'?s?) (time|date|day|date today)\b", 0.85),
        (r"\b(time|date|day|clock)\b.*\bin\s+[a-z]", 0.9),
        (r"\bwhat day is it\b", 0.9),
        (r"^\s*time\s*$", 0.8),
        (r"^\s*date\s*$", 0.8),
    )

    def run(self, text: str, ctx: SkillContext) -> SkillResult:
        lowered = text.lower()
        match = re.search(r"\b(?:time|date|day|clock)\b[^\n]*?\bin\s+([a-z\s]+?)(?:[?.!]|$)", lowered)
        if match:
            city = match.group(1).strip()
            city = re.sub(r"\b(right now|now|today|please|currently)\b", "", city).strip()
            zone = CITY_TIMEZONES.get(city)
            if not zone:
                for known, tz in CITY_TIMEZONES.items():
                    if known in city or city in known:
                        zone, city = tz, known
                        break
            if not zone and city:
                try:
                    from zoneinfo import ZoneInfo

                    ZoneInfo(city.title().replace(" ", "_"))
                    zone = city.title().replace(" ", "_")
                except Exception:
                    zone = None
            if zone:
                try:
                    import datetime as _dt
                    from zoneinfo import ZoneInfo

                    now = _dt.datetime.now(ZoneInfo(zone))
                    return SkillResult(
                        text=f"In {city.title()} it is {now.strftime('%H:%M')} on {now.strftime('%A, %d %B %Y')} ({zone}).",
                        speak=f"It is {now.strftime('%H:%M')} in {city.title()}.",
                        data={"timezone": zone},
                    )
                except Exception as exc:
                    return SkillResult(text=f"Could not resolve {city}: {exc}", ok=False)
            return SkillResult(
                text=f"I don't have a time-zone mapping for “{city}”. Try a major city, "
                     "or add it to CITY_TIMEZONES in jarvis/skills.py.",
                speak=f"I don't know the time zone for {city}.",
                ok=False,
            )

        import datetime as _dt

        now = _dt.datetime.now()
        if re.search(r"\b(date|day)\b", lowered) and not re.search(r"\btime\b", lowered):
            return SkillResult(
                text=f"Today is {now.strftime('%A, %d %B %Y')}.",
                speak=f"Today is {now.strftime('%A, the %d of %B %Y')}.",
            )
        return SkillResult(
            text=f"It is {now.strftime('%H:%M:%S')} on {now.strftime('%A, %d %B %Y')} ({time.tzname[0]}).",
            speak=f"It is {now.strftime('%I:%M %p').lstrip('0')}.",
            data={"iso": now.isoformat()},
        )


_ALLOWED_OPS: dict[type, Callable[..., Any]] = {
    ast.Add: op.add, ast.Sub: op.sub, ast.Mult: op.mul, ast.Div: op.truediv,
    ast.FloorDiv: op.floordiv, ast.Mod: op.mod, ast.Pow: op.pow, ast.USub: op.neg,
    ast.UAdd: op.pos, ast.Call: None,  # type: ignore[dict-item]
}


def safe_eval(expression: str) -> float:
    """Evaluate arithmetic safely (no names, no attribute access, no calls but sqrt)."""
    node = ast.parse(expression, mode="eval").body

    def walk(current: ast.AST) -> float:
        if isinstance(current, ast.Constant):
            if isinstance(current.value, (int, float)):
                return float(current.value)
            raise ValueError("Only numbers are allowed")
        if isinstance(current, ast.BinOp) and type(current.op) in _ALLOWED_OPS:
            left, right = walk(current.left), walk(current.right)
            if isinstance(current.op, ast.Pow) and (abs(right) > 64 or abs(left) > 10_000):
                raise ValueError("Exponent too large")
            return float(_ALLOWED_OPS[type(current.op)](left, right))
        if isinstance(current, ast.UnaryOp) and type(current.op) in _ALLOWED_OPS:
            return float(_ALLOWED_OPS[type(current.op)](walk(current.operand)))
        if isinstance(current, ast.Call):
            func = current.func
            name = func.id if isinstance(func, ast.Name) else ""
            if name not in {"sqrt", "abs", "round", "sin", "cos", "tan", "log", "log10", "exp"}:
                raise ValueError(f"Function '{name}' is not allowed")
            if len(current.args) > 2:
                raise ValueError("Too many arguments")
            args = [walk(a) for a in current.args]
            return float(getattr(math, name)(*args))
        raise ValueError("Unsupported expression")
    return walk(node)


UNIT_CONVERSIONS: dict[tuple[str, str], tuple[float, str]] = {
    ("km", "miles"): (0.621371, "miles"), ("miles", "km"): (1.609344, "km"),
    ("m", "feet"): (3.28084, "feet"), ("feet", "m"): (0.3048, "m"),
    ("kg", "pounds"): (2.20462, "pounds"), ("pounds", "kg"): (0.453592, "kg"),
    ("g", "ounces"): (0.035274, "ounces"), ("ounces", "g"): (28.3495, "g"),
    ("c", "f"): (0.0, "f"), ("f", "c"): (0.0, "c"),  # handled specially
    ("litres", "gallons"): (0.264172, "gallons"), ("gallons", "litres"): (3.78541, "litres"),
    ("gb", "mb"): (1024.0, "MB"), ("mb", "gb"): (1 / 1024.0, "GB"),
}


class MathSkill(Skill):
    name = "math"
    title = "Calculator"
    description = "arithmetic, percentages and unit conversion"
    examples = ("calculate 15 * 240 + 8", "convert 12 km to miles", "what is 18% of 2400")
    patterns = (
        (r"\b(calculate|compute|what is|whats|how much is)\b.*\d\s*[\+\-\*/^%]", 0.92),
        (r"^\s*[\d\s\.\+\-\*/\(\)%^]+\s*$", 0.8),
        (r"\bpercent(age)? of\b", 0.9),
        (r"\bconvert\b.*\b(to|into)\b", 0.9),
        (r"\bsqrt\b|\bsquare root\b", 0.85),
    )

    def run(self, text: str, ctx: SkillContext) -> SkillResult:
        lowered = text.lower().strip()

        # unit conversion
        match = re.search(
            r"(?:convert\s+)?(-?\d+(?:\.\d+)?)\s*([a-z°]+)\s*(?:to|into|in)\s*([a-z°]+)", lowered
        )
        if (match and "convert" in lowered) or (match and (match.group(2), match.group(3)) in UNIT_CONVERSIONS):
            value = float(match.group(1))
            src = match.group(2).strip("°").lower()
            dst = match.group(3).strip("°").lower()
            src = {"celsius": "c", "fahrenheit": "f", "kms": "km", "kilometres": "km", "kilometers": "km",
                   "kilograms": "kg", "kilos": "kg", "pounds": "pounds", "lbs": "pounds",
                   "liters": "litres", "litres": "litres", "meters": "m", "metres": "m"}.get(src, src)
            dst = {"celsius": "c", "fahrenheit": "f", "kms": "km", "kilometres": "km", "kilometers": "km",
                   "kilograms": "kg", "kilos": "kg", "lbs": "pounds",
                   "liters": "litres", "metres": "m", "meters": "m"}.get(dst, dst)
            if (src, dst) == ("c", "f"):
                result = value * 9 / 5 + 32
            elif (src, dst) == ("f", "c"):
                result = (value - 32) * 5 / 9
            elif (src, dst) in UNIT_CONVERSIONS:
                factor, _ = UNIT_CONVERSIONS[(src, dst)]
                result = value * factor
            else:
                result = None
            if result is not None:
                pretty = f"{result:,.2f}".rstrip("0").rstrip(".")
                return SkillResult(
                    text=f"{value:g} {src} = {pretty} {dst}",
                    speak=f"{value:g} {src} is {pretty} {dst}.",
                    data={"from": src, "to": dst, "result": result},
                )

        # percentage
        match = re.search(r"(\d+(?:\.\d+)?)\s*%\s*(?:of)\s*(\d+(?:\.\d+)?)", lowered)
        if match:
            part = float(match.group(1)) / 100 * float(match.group(2))
            return SkillResult(
                text=f"{match.group(1)}% of {match.group(2)} = {part:,.4g}",
                speak=f"That is {part:,.2f}".replace(".00", ""),
                data={"result": part},
            )

        # plain arithmetic
        expr = lowered
        expr = re.sub(r"^(calculate|compute|what is|what's|whats|how much is|eval)\b", "", expr).strip()
        expr = expr.replace("^", "**").replace("x", "*") if re.fullmatch(r"[\d\s\.\+\-\*/\(\)\^x]+", expr) else expr
        expr = re.sub(r"(\d),(\d)", r"\1\2", expr)
        expr = expr.rstrip("?= ")
        if re.search(r"\bsqrt\b|\bsquare root of\b", expr):
            number = re.search(r"(-?\d+(?:\.\d+)?)", expr)
            if number:
                root = math.sqrt(abs(float(number.group(1))))
                return SkillResult(text=f"√{number.group(1)} = {root:,.6g}", data={"result": root})
        try:
            result = safe_eval(expr)
        except Exception as exc:
            return SkillResult(
                text=f"I could not evaluate “{expr}” ({exc}). Try something like “calculate 18 * 42 / 7”.",
                speak="I could not evaluate that expression.", ok=False,
            )
        pretty = f"{result:,.10g}"
        return SkillResult(text=f"{expr} = {pretty}", speak=f"The answer is {pretty}.", data={"result": result})


# ════════════════════════════════════════════════════════════════════════════
#  Weather & knowledge (free, no API key)
# ════════════════════════════════════════════════════════════════════════════

WEATHER_CODES = {
    0: "clear sky", 1: "mainly clear", 2: "partly cloudy", 3: "overcast", 45: "fog",
    48: "freezing fog", 51: "light drizzle", 53: "drizzle", 55: "heavy drizzle",
    56: "freezing drizzle", 57: "freezing drizzle", 61: "light rain", 63: "rain",
    65: "heavy rain", 66: "freezing rain", 67: "freezing rain", 71: "light snow",
    73: "snow", 75: "heavy snow", 77: "snow grains", 80: "light showers",
    81: "showers", 82: "violent showers", 85: "snow showers", 86: "snow showers",
    95: "thunderstorm", 96: "thunderstorm with hail", 99: "severe thunderstorm with hail",
}


class WeatherSkill(Skill):
    name = "weather"
    title = "Weather"
    description = "current conditions and forecast (Open-Meteo, no key needed)"
    examples = ("what's the weather", "will it rain today", "forecast for Chennai")
    patterns = (
        (r"\bweather\b", 0.95),
        (r"\b(rain|raining|temperature|humid|forecast)\b", 0.8),
        (r"\bhow (hot|cold|warm) is it\b", 0.9),
    )
    priority = 5

    def _geocode(self, place: str) -> tuple[float, float, str] | None:
        if requests is None:
            return None
        response = requests.get(
            "https://geocoding-api.open-meteo.com/v1/search",
            params={"name": place, "count": 1, "language": "en", "format": "json"},
            timeout=8,
        )
        response.raise_for_status()
        results = response.json().get("results") or []
        if not results:
            return None
        top = results[0]
        label = ", ".join(x for x in (top.get("name"), top.get("admin1"), top.get("country")) if x)
        return float(top["latitude"]), float(top["longitude"]), label

    def run(self, text: str, ctx: SkillContext) -> SkillResult:
        if requests is None:
            return SkillResult(text="The requests package is missing — run: pip install requests",
                               ok=False, speak="I cannot reach the weather service.")
        lowered = text.lower()
        place = ctx.city
        match = re.search(r"\b(?:in|for|at)\s+([a-z\s,'-]{2,40})$", lowered)
        if match:
            place = match.group(1).strip(" ,'")
            place = re.sub(r"\b(today|tomorrow|now|please)\b", "", place).strip()

        try:
            location = self._geocode(place)
        except Exception as exc:
            return SkillResult(
                text=f"I could not look up “{place}” ({exc}). Check your internet connection.",
                speak="I could not reach the weather service.", ok=False,
            )
        if not location:
            return SkillResult(text=f"I could not find a place called “{place}”.",
                               speak=f"I could not find {place}.", ok=False)
        lat, lon, label = location
        imperial = ctx.units == "imperial"
        try:
            response = requests.get(
                "https://api.open-meteo.com/v1/forecast",
                params={
                    "latitude": lat,
                    "longitude": lon,
                    "current": "temperature_2m,relative_humidity_2m,apparent_temperature,weather_code,wind_speed_10m",
                    "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
                    "forecast_days": 3,
                    "timezone": "auto",
                    "temperature_unit": "fahrenheit" if imperial else "celsius",
                    "wind_speed_unit": "mph" if imperial else "kmh",
                },
                timeout=10,
            )
            response.raise_for_status()
            data = response.json()
        except Exception as exc:
            return SkillResult(text=f"Weather lookup failed: {exc}",
                               speak="I could not fetch the weather.", ok=False)

        current = data.get("current") or {}
        daily = data.get("daily") or {}
        unit = "°F" if imperial else "°C"
        speed_unit = "mph" if imperial else "km/h"
        condition = WEATHER_CODES.get(int(current.get("weather_code", -1)), "unknown conditions")
        summary_line = (
            f"{label} — {condition}, {current.get('temperature_2m')}{unit} "
            f"(feels like {current.get('apparent_temperature')}{unit}), "
            f"humidity {current.get('relative_humidity_2m')}%, "
            f"wind {current.get('wind_speed_10m')} {speed_unit}."
        )
        lines = [summary_line]
        days = daily.get("time") or []
        for index, day in enumerate(days[1:3], start=1):
            if index >= len(days):
                continue
            code = int((daily.get("weather_code") or [0])[index])
            lines.append(
                f"{day}: {WEATHER_CODES.get(code, 'unknown')}, "
                f"{(daily.get('temperature_2m_min') or [0])[index]}{unit} – "
                f"{(daily.get('temperature_2m_max') or [0])[index]}{unit}, "
                f"rain chance {(daily.get('precipitation_probability_max') or [0])[index]}%"
            )
        spoken = (
            f"{label}: {condition}, {current.get('temperature_2m')} degrees, "
            f"feels like {current.get('apparent_temperature')}."
        )
        return SkillResult(text="\n".join(lines), speak=spoken,
                           data={"place": label, "current": current, "daily": daily})


class WikiSkill(Skill):
    name = "wiki"
    title = "Knowledge (Wikipedia)"
    description = "definitions and summaries from Wikipedia, no key needed"
    examples = ("who was Ada Lovelace", "what is quantum entanglement")
    patterns = (
        (r"\b(who|what) (is|was|are|were)\b", 0.5),
        (r"\btell me about\b", 0.55),
        (r"\bdefine\b", 0.6),
        (r"\bwiki(pedia)?\b", 0.75),
        (r"\bexplain\b", 0.4),
    )

    def run(self, text: str, ctx: SkillContext) -> SkillResult:
        topic = re.sub(
            r"\b(who|what|when|where)\s+(is|was|are|were)\b|\btell me about\b|\bdefine\b|"
            r"\bexplain\b|\bwiki(pedia)?\b|\bfor me\b|\?|please\b",
            "", text, flags=re.IGNORECASE,
        ).strip(" ,.")
        if not topic:
            return SkillResult(text="Tell me what you'd like looked up, e.g. “who was Ada Lovelace”.",
                               speak="What would you like me to look up?", ok=False)

        # The LLM handles this better when it is configured.
        if ctx.llm_ready():
            answer = ctx.ask_llm(
                f"Answer briefly (max 120 words) about: {topic}",
                "Prefer a definition-style answer. If you are unsure, say so.",
            )
            if answer:
                return SkillResult(text=answer, data={"topic": topic, "source": "llm"})

        if requests is None:
            return SkillResult(text="The requests package is missing — run: pip install requests", ok=False)
        try:
            response = requests.get(
                f"https://en.wikipedia.org/api/rest_v1/page/summary/{topic.replace(' ', '_')}",
                headers={"User-Agent": f"JARVIS/{VERSION} (personal assistant)"},
                timeout=10,
            )
            if response.status_code == 404:
                search = requests.get(
                    "https://en.wikipedia.org/w/rest.php/v1/search/page",
                    params={"q": topic, "limit": 1},
                    headers={"User-Agent": f"JARVIS/{VERSION}"},
                    timeout=10,
                )
                hits = (search.json().get("pages") or []) if search.status_code == 200 else []
                if hits:
                    key = hits[0].get("key", "")
                    response = requests.get(
                        f"https://en.wikipedia.org/api/rest_v1/page/summary/{key}",
                        headers={"User-Agent": f"JARVIS/{VERSION}"},
                        timeout=10,
                    )
            response.raise_for_status()
            data = response.json()
        except Exception as exc:
            return SkillResult(
                text=f"I could not reach Wikipedia ({exc}).",
                speak="I could not reach Wikipedia.", ok=False,
            )

        extract = (data.get("extract") or "").strip()
        if not extract:
            return SkillResult(text=f"I found no summary for “{topic}”.", ok=False)
        title = data.get("title", topic)
        link = (data.get("content_urls", {}).get("desktop", {}) or {}).get("page", "")
        return SkillResult(
            text=f"{title}\n{extract}" + (f"\n{link}" if link else ""),
            speak=extract[:300],
            data={"topic": title, "url": link, "source": "wikipedia"},
        )


class NetworkSkill(Skill):
    name = "network"
    title = "Network"
    description = "connectivity, public IP and DNS checks"
    examples = ("is the internet up", "what's my ip", "ping google")
    patterns = (
        (r"\b(internet|connection|network|wifi|wi-fi)\b.*\b(up|down|working|status|check|connected)\b", 0.9),
        (r"\bis the internet (up|working|down)\b", 0.95),
        (r"\b(my|public) (ip|ip address)\b", 0.9),
        (r"\bping\b", 0.85),
        (r"\bdns\b.*\b(check|working)\b", 0.8),
    )

    def run(self, text: str, ctx: SkillContext) -> SkillResult:
        lowered = text.lower()
        lines: list[str] = []
        if "ip" in lowered and requests is not None:
            try:
                ip = requests.get("https://api.ipify.org", timeout=6).text.strip()
                lines.append(f"Public IP: {ip}")
            except Exception as exc:
                lines.append(f"Public IP lookup failed: {exc}")
        target = "1.1.1.1"
        match = re.search(r"ping\s+([a-z0-9\.\-]+)", lowered)
        if match:
            target = match.group(1)
        host = target if not target[0].isalpha() else "one.one.one.one"
        started = time.time()
        try:
            with socket.create_connection((host, 443), timeout=4) as connection:
                connection.settimeout(4)
                lines.append(
                    f"TCP 443 to {target}: reachable in {(time.time() - started) * 1000:.0f} ms"
                )
        except Exception as exc:
            lines.append(f"TCP 443 to {target}: FAILED ({exc})")
        try:
            socket.gethostbyname("example.com")
            lines.append("DNS: resolving (example.com OK)")
        except Exception as exc:
            lines.append(f"DNS: FAILED ({exc})")
        if not lines:
            lines.append("Network stack is up; no failures detected.")
        return SkillResult(text="\n".join(lines), speak="Network check done — see the console.")


# ════════════════════════════════════════════════════════════════════════════
#  System
# ════════════════════════════════════════════════════════════════════════════

def _human_bytes(value: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(value) < 1024 or unit == "TB":
            return f"{value:,.1f} {unit}" if unit != "B" else f"{value:,.0f} B"
        value /= 1024
    return f"{value:,.1f} TB"


class SystemSkill(Skill):
    name = "system"
    title = "System monitor"
    description = "CPU, memory, disk, battery, uptime, top processes"
    examples = ("system status", "how much battery is left", "disk space")
    patterns = (
        (r"\b(cpu|processor)\b", 0.9),
        (r"\b(ram|memory) (usage|use|load|left|free)\b", 0.9),
        (r"\b(disk|storage|drive|space)\b", 0.85),
        (r"\b(battery|charge|power level)\b", 0.95),
        (r"\b(system|machine|pc|laptop|computer) (status|health|usage|stats|info|report)\b", 0.92),
        (r"\bhow('s| is) my (system|pc|laptop|machine|computer)\b", 0.92),
        (r"\buptime\b", 0.9),
        (r"\b(top|heavy|busiest) (processes|programs|apps)\b", 0.88),
    )

    def run(self, text: str, ctx: SkillContext) -> SkillResult:
        lowered = text.lower()
        try:
            import psutil
        except Exception:
            return self._fallback(lowered, ctx)

        lines: list[str] = []
        wanted = {
            "cpu": bool(re.search(r"\bcpu|processor\b", lowered)),
            "mem": bool(re.search(r"\bram|memory\b", lowered)),
            "disk": bool(re.search(r"\bdisk|storage|drive|space\b", lowered)),
            "bat": bool(re.search(r"\bbattery|charge|power level\b", lowered)),
            "up": bool(re.search(r"\buptime\b", lowered)),
            "proc": bool(re.search(r"\bprocess", lowered)),
        }
        if not any(wanted.values()):
            wanted = dict.fromkeys(wanted, True)

        if wanted["cpu"]:
            load = psutil.cpu_percent(interval=0.35)
            freq = ""
            try:
                speed = psutil.cpu_freq()
                if speed and speed.current:
                    freq = f" @ {speed.current / 1000:.2f} GHz"
            except Exception:
                pass
            lines.append(f"CPU: {load:.0f}% across {psutil.cpu_count(logical=True)} threads{freq}")
        if wanted["mem"]:
            mem = psutil.virtual_memory()
            lines.append(
                f"Memory: {_human_bytes(mem.used)} / {_human_bytes(mem.total)} used ({mem.percent:.0f}%)"
            )
        if wanted["disk"]:
            for part in psutil.disk_partitions(all=False)[:4]:
                try:
                    usage = psutil.disk_usage(part.mountpoint)
                except Exception:
                    continue
                lines.append(
                    f"Disk {part.mountpoint}: {_human_bytes(usage.free)} free of "
                    f"{_human_bytes(usage.total)} ({usage.percent:.0f}% used)"
                )
        if wanted["bat"]:
            try:
                battery = psutil.sensors_battery()
                if battery:
                    state = "charging" if battery.power_plugged else "on battery"
                    lines.append(f"Battery: {battery.percent:.0f}% ({state})")
                else:
                    lines.append("Battery: none detected (desktop?)")
            except Exception:
                lines.append("Battery: unavailable on this platform")
        if wanted["up"]:
            seconds = time.time() - psutil.boot_time()
            hours, minutes = divmod(int(seconds // 60), 60)
            lines.append(f"Uptime: {hours}h {minutes}m")
        if wanted["proc"]:
            processes = []
            for process in psutil.process_iter(["name", "cpu_percent", "memory_percent"]):
                try:
                    processes.append(process.info)
                except Exception:
                    continue
            processes.sort(key=lambda p: (p.get("cpu_percent") or 0, p.get("memory_percent") or 0), reverse=True)
            top = processes[:5]
            if top:
                lines.append("Top processes: " + ", ".join(
                    f"{p.get('name', '?')} ({p.get('cpu_percent') or 0:.0f}% cpu)" for p in top
                ))

        if not lines:
            lines.append("Nothing to report.")
        return SkillResult(text="\n".join(lines), speak=" ".join(lines[:2]),
                           data={"raw": lines})

    def _fallback(self, lowered: str, ctx: SkillContext) -> SkillResult:
        """Works without psutil: stdlib-only best effort."""
        lines = [f"Platform: {platform.platform()}", f"Python: {platform.python_version()}"]
        try:
            usage = shutil.disk_usage(os.path.expanduser("~"))
            lines.append(f"Home disk: {_human_bytes(usage.free)} free of {_human_bytes(usage.total)}")
        except Exception:
            pass
        if sys.platform.startswith("linux"):
            try:
                with open("/proc/loadavg") as fh:
                    lines.append("Load average: " + fh.read().split()[:3].__str__().strip("[]'"))
                with open("/proc/meminfo") as fh:
                    info = dict(
                        line.split(":") for line in fh.read().splitlines() if ":" in line
                    )
                total = int(info.get("MemTotal", "0 kB").split()[0]) * 1024
                available = int(info.get("MemAvailable", "0 kB").split()[0]) * 1024
                lines.append(f"Memory: {_human_bytes(total - available)} / {_human_bytes(total)} used")
            except Exception:
                pass
        lines.append("Install psutil for full CPU/battery detail: pip install psutil")
        return SkillResult(text="\n".join(lines), speak="Here is the basic system information.")


VOLUME_KEYS = {
    "up": {"win": 0xAF, "mac": "volume up", "linux": "+5%"},
    "down": {"win": 0xAE, "mac": "volume down", "linux": "-5%"},
    "mute": {"win": 0xAD, "mac": "mute", "linux": "toggle"},
    "play": {"win": 0xB3, "mac": "play", "linux": "play-pause"},
    "next": {"win": 0xB0, "mac": "next", "linux": "next"},
    "prev": {"win": 0xB1, "mac": "previous", "linux": "previous"},
}


class MediaSkill(Skill):
    name = "media"
    title = "Media keys"
    description = "volume, mute and playback control"
    examples = ("volume up", "mute", "next track")
    patterns = (
        # A direction is required: reading or setting a level is the control
        # layer's job (it can report the number, and it is audited).
        (r"\b(volume|sound)\s+(up|down|louder|quieter|increase|decrease)\b", 0.9),
        (r"\b(mute|unmute)\b", 0.95),
        (r"\b(next|previous|skip) (track|song)\b", 0.9),
        (r"\b(pause|resume|play) (music|song|track|audio|media)\b", 0.9),
    )

    def _press(self, key: str) -> bool:
        lowered = key.lower()
        if sys.platform.startswith("win"):
            try:
                import ctypes

                code = VOLUME_KEYS[lowered]["win"]
                for flag in (0, 2):  # key down, key up
                    ctypes.windll.user32.keybd_event(code, 0, flag, 0)  # type: ignore[attr-defined]
                return True
            except Exception:
                return False
        if sys.platform == "darwin":
            scripts = {
                "up": 'set volume output volume ((output volume of (get volume settings)) + 10)',
                "down": 'set volume output volume ((output volume of (get volume settings)) - 10)',
                "mute": "set volume with output muted",
                "play": 'tell application "Music" to playpause',
                "next": 'tell application "Music" to next track',
                "prev": 'tell application "Music" to previous track',
            }
            if not shutil.which("osascript"):
                return False
            try:
                subprocess.run(["osascript", "-e", scripts[lowered]], check=False, timeout=6,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return True
            except Exception:
                return False
        # linux
        action = VOLUME_KEYS[lowered]["linux"]
        commands = {
            "toggle": [["pactl", "set-sink-mute", "@DEFAULT_SINK@", "toggle"]],
            "play-pause": [["playerctl", "play-pause"]],
            "next": [["playerctl", "next"]],
            "previous": [["playerctl", "previous"]],
        }.get(action) or [["pactl", "set-sink-volume", "@DEFAULT_SINK@", action]]
        for command in commands:
            if shutil.which(command[0]):
                try:
                    subprocess.run(command, check=False, timeout=5)
                    return True
                except Exception:
                    continue
        return False

    def run(self, text: str, ctx: SkillContext) -> SkillResult:
        lowered = text.lower()
        if "mute" in lowered:
            key = "mute"
        elif re.search(r"\bnext\b", lowered):
            key = "next"
        elif re.search(r"\b(previous|prev|back)\b", lowered):
            key = "prev"
        elif re.search(r"\b(pause|resume|play)\b", lowered) and re.search(r"\b(music|song|track|audio|media)\b", lowered):
            key = "play"
        elif re.search(r"\b(up|louder|increase|raise)\b", lowered):
            key = "up"
        else:
            key = "down"
        if self._press(key):
            return SkillResult(text=f"Media key sent: {key}.", speak=f"{key.title()} done.")
        return SkillResult(
            text=f"I could not send the '{key}' media key on {platform.system()}. "
                 "On Linux install playerctl/pactl; on Windows I use the system media keys.",
            speak="I could not control media playback on this system.", ok=False,
        )


class PowerSkill(Skill):
    name = "power"
    title = "Power & session"
    description = "lock the screen, sleep, show power commands"
    examples = ("lock my screen", "sleep the computer")
    patterns = (
        (r"\block\b.*\b(screen|computer|pc|session|windows)\b", 0.95),
        (r"\b(sleep|suspend|hibernate)\b.*\b(pc|computer|laptop|system)?\b", 0.9),
        (r"\b(shutdown|shut down|restart|reboot)\b", 0.6),
        (r"\b(turn off|blank) (the )?(screen|display)\b", 0.9),
    )

    def run(self, text: str, ctx: SkillContext) -> SkillResult:
        lowered = text.lower()
        if re.search(r"\b(shutdown|shut down|restart|reboot)\b", lowered):
            commands = {
                "Windows": "shutdown /s /t 60   (cancel with: shutdown /a)",
                "Darwin": "sudo shutdown -h +1",
                "Linux": "sudo shutdown -h +1",
            }
            return SkillResult(
                text=("I deliberately do not power off your machine — here is the command if you want it:\n"
                      f"  {commands.get(platform.system(), 'shutdown -h now')}"),
                speak="I do not shut down your machine on my own. The command is on screen if you want it.",
            )
        try:
            if sys.platform.startswith("win"):
                if "lock" in lowered:
                    import ctypes

                    ctypes.windll.user32.LockWorkStation()  # type: ignore[attr-defined]
                    return SkillResult(text="Screen locked.", speak="Locking your screen.")
                if re.search(r"blank|turn off", lowered):
                    import ctypes

                    ctypes.windll.user32.SendMessageW(0xFFFF, 0x0112, 0xF170, 2)  # type: ignore[attr-defined]
                    return SkillResult(text="Display turned off.", speak="Display off.")
                subprocess.Popen(["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"])
                return SkillResult(text="Suspending…", speak="Suspending the computer.")
            if sys.platform == "darwin":
                if "lock" in lowered:
                    subprocess.Popen(["/System/Library/CoreServices/Menu Extras/User.menu/Contents/Resources/CGSession", "-suspend"])
                    return SkillResult(text="Screen locked.", speak="Locking your screen.")
                subprocess.Popen(["pmset", "sleepnow"])
                return SkillResult(text="Suspending…", speak="Suspending the computer.")
            # linux
            if "lock" in lowered:
                for command in (["loginctl", "lock-session"], ["xdg-screensaver", "lock"], ["gnome-screensaver-command", "-l"]):
                    if shutil.which(command[0]):
                        subprocess.Popen(command)
                        return SkillResult(text="Screen locked.", speak="Locking your screen.")
            if shutil.which("systemctl"):
                subprocess.Popen(["systemctl", "suspend"])
                return SkillResult(text="Suspending…", speak="Suspending the computer.")
            return SkillResult(text="I could not find a supported lock/suspend command on this system.",
                               speak="That is not supported here.", ok=False)
        except Exception as exc:
            return SkillResult(text=f"Power action failed: {exc}", ok=False,
                               speak="That power action failed.")


# ════════════════════════════════════════════════════════════════════════════
#  Launching apps, URLs and searches
# ════════════════════════════════════════════════════════════════════════════

def platform_key() -> str:
    if sys.platform.startswith("win"):
        return "win"
    if sys.platform == "darwin":
        return "mac"
    return "linux"


class AppSkill(Skill):
    name = "apps"
    title = "Launcher"
    description = "open apps, websites, files and searches"
    examples = ("open chrome", "search youtube for lofi beats", "open my downloads folder")
    patterns = (
        (r"^\s*(open|launch|start|run)\s+\S", 0.85),
        (r"\b(search|google|look up)\b.*\b(for|about)?\s*\S", 0.8),
        (r"\b(youtube|spotify)\b.*\b(play|search)\b", 0.85),
        (r"\bopen (my )?(downloads|documents|desktop|pictures|home)\b", 0.9),
        (r"\bgo to\b.*\b(website|site|page)\b", 0.8),
    )

    SPECIAL_FOLDERS = {
        "downloads": "~/Downloads", "documents": "~/Documents", "desktop": "~/Desktop",
        "pictures": "~/Pictures", "home": "~", "music": "~/Music", "videos": "~/Videos",
    }

    def run(self, text: str, ctx: SkillContext) -> SkillResult:
        lowered = text.lower().strip()

        # web searches: "search youtube for X", "google X", "look up X on wikipedia"
        sites = {"youtube": "youtube", "google": "google", "wikipedia": "wikipedia",
                 "stackoverflow": "stackoverflow", "stack overflow": "stackoverflow", "github": "github"}
        site: str | None = None
        query = ""
        site_hit = re.search(r"\b(youtube|google|wikipedia|stack ?overflow|github)\b", lowered)
        if site_hit and re.search(r"\b(open|search|play|find|look up|show|go to)\b", lowered):
            site = sites[site_hit.group(1)]
            tail = lowered[site_hit.end():]
            query = re.sub(r"^\s*(for|search|about|:|-)\s*", "", tail).strip(" ?.")
        if not query:
            search_hit = re.search(r"\b(search|google|look ?up)\b\s*(?:for|about)?\s*(.+)$", lowered)
            if search_hit:
                query = search_hit.group(2).strip(" ?.")
                on_site = re.search(r"\bon\s+(youtube|google|wikipedia|stack ?overflow|github)\b", query)
                if on_site:
                    site = sites[on_site.group(1)]
                    query = re.sub(r"\bon\s+(youtube|google|wikipedia|stack ?overflow|github)\b", "", query).strip(" ?.")
        if query:
            site = site or "google"
            slug = query.replace(" ", "+")
            urls = {
                "youtube": f"https://www.youtube.com/results?search_query={slug}",
                "google": f"https://www.google.com/search?q={slug}",
                "wikipedia": f"https://en.wikipedia.org/w/index.php?search={slug}",
                "stackoverflow": f"https://stackoverflow.com/search?q={slug}",
                "github": f"https://github.com/search?q={slug}",
            }
            url = urls[site]
            if ctx.host.open_target(url):
                return SkillResult(text=f"Opened {site.title()} for “{query}”.",
                                   speak=f"Searching {site} for {query}.", data={"url": url})

        # folder intents
        folder = re.search(r"\bopen (my )?(downloads|documents|desktop|pictures|home|music|videos)\b", lowered)
        if folder:
            path = os.path.expanduser(self.SPECIAL_FOLDERS[folder.group(2)])
            if ctx.host.open_target(path):
                return SkillResult(text=f"Opened {path}", speak=f"Opening your {folder.group(2)} folder.")
            return SkillResult(text=f"Could not open {path}", ok=False)

        # explicit URL
        url = re.search(r"(https?://\S+|www\.\S+|\S+\.(com|org|net|io|dev|ai|in)\b/?\S*)", lowered)
        if url and re.search(r"\b(open|go to|launch|visit|browse)\b", lowered):
            target = url.group(1)
            if not target.startswith("http"):
                target = "https://" + target
            if ctx.host.open_target(target):
                return SkillResult(text=f"Opened {target}", speak="Opening it now.", data={"url": target})

        # app launch
        app_match = re.search(r"\b(?:open|launch|start|run)\s+(?:the\s+)?([a-z0-9\.\+ _-]{2,30})", lowered)
        if app_match:
            name = app_match.group(1).strip()
            name = re.sub(r"\b(app|application|program|please)\b", "", name).strip()
            entry = SAFE_APPS.get(name)
            if not entry:
                for known, candidate in SAFE_APPS.items():
                    if known in name or name in known:
                        entry, name = candidate, known
                        break
            if entry:
                commands = entry.get(platform_key()) or []
                for command in commands:
                    if command.startswith("http"):
                        if ctx.host.open_target(command):
                            return SkillResult(text=f"Opened {name}.", speak=f"Opening {name}.")
                        continue
                    binary = shutil.which(command)
                    if not binary and not command.lower().endswith(".exe"):
                        continue
                    try:
                        subprocess.Popen([binary or command], stdout=subprocess.DEVNULL,
                                         stderr=subprocess.DEVNULL)
                        return SkillResult(text=f"Launched {name}.", speak=f"Launching {name}.")
                    except Exception:
                        continue
                if re.fullmatch(r"[\w .+-]{2,30}", name) and ctx.host.open_target(name):
                    return SkillResult(text=f"Tried to launch “{name}”.", speak=f"Launching {name}.")
                return SkillResult(
                    text=f"I know the app “{name}” but none of its commands are installed on this machine. "
                         f"Tried: {', '.join(commands) or 'nothing'}.",
                    ok=False, speak=f"I could not launch {name}.",
                )
            return SkillResult(
                text=f"“{name}” is not in my safe-app list. Add it to SAFE_APPS in jarvis/skills.py, "
                     "or open it yourself.",
                speak=f"I do not have {name} in my launcher list.", ok=False,
            )
        return SkillResult(text="Tell me what to open, e.g. “open chrome”.", ok=False,
                           speak="What should I open?")


# ════════════════════════════════════════════════════════════════════════════
#  Memory: notes, tasks, facts
# ════════════════════════════════════════════════════════════════════════════

class NotesSkill(Skill):
    name = "memory"
    title = "Notes, tasks & facts"
    description = "save notes, manage todos, remember facts"
    examples = ("note that the deploy is on Friday", "add task call the bank",
                "remember my locker code is 4417", "what are my tasks",
                "search my notes for invoice")
    patterns = (
        (r"\b(note|jot|write) (this |that |it )?(down|please)?", 0.85),
        (r"\b(remember|memorise|memorize) (that|my|the)\b", 0.92),
        (r"\bwhat (do you remember|did i tell you|do you know) about\b", 0.92),
        (r"\bforget (about )?\b", 0.9),
        (r"\b(add|create|new) (a )?(task|todo|to-do|reminder)\b", 0.92),
        # “add milk to my todo list” / “put the invoice on my task list”
        (r"\b(add|put)\b.+\bto (my |the )?(todo|to-do|task)s? list\b", 0.95),
        (r"\b(my|list|show|what are my) (tasks|todos|to-dos|reminders)\b", 0.92),
        (r"\b(mark|complete|finish|done with)\b.*\b(task|todo|\d)\b", 0.85),
        (r"\b(delete|remove) (task|note)\b", 0.85),
        (r"\b(search|find) my (notes|memory|tasks)\b", 0.9),
        (r"\bmy (notes|memory)\b", 0.8),
        (r"\bwhat do you know about me\b", 0.95),
    )
    priority = 3

    def run(self, text: str, ctx: SkillContext) -> SkillResult:
        lowered = text.lower().strip()
        memory = ctx.memory

        # facts: remember X is Y / remember my X is Y  (case preserved from `text`)
        match = re.search(
            r"\b(?:remember|memorise|memorize|save) (?:that )?(?:my |the )?(.+?)\s+(?:is|are|=)\s+(.+)$",
            text, flags=re.IGNORECASE,
        )
        if match:
            key = match.group(1).strip(" my")
            key = re.sub(r"^\s*(that|the|my)\s+", "", key, flags=re.IGNORECASE).strip()
            value = re.sub(r"^\s*(called|named)\s+", "", match.group(2).strip(" ."), flags=re.IGNORECASE)
            value = value.strip(" '\"")
            memory.remember(key, value)
            ctx.host.refresh_memory()
            return SkillResult(text=f"Remembered: {key} = {value}", speak=f"Noted. {key} is {value}.")

        # recall a fact
        match = re.search(r"\bwhat (?:do you remember|did i tell you|do you know) about (?:my )?(.+?)\?*$", lowered)
        if match:
            key = match.group(1).strip(" ?.")
            value = memory.recall(key)
            if value:
                return SkillResult(text=f"{key}: {value}", speak=f"{key} is {value}.")
            found = memory.search(key)
            if found["notes"] or found["tasks"]:
                lines = [f"Nothing stored as a fact for “{key}”, but I found:"]
                lines += [f"• note [{n.id}] {n.text}" for n in found["notes"]]
                lines += [f"• task [{t.id}] {t.text}{' ✓' if t.done else ''}" for t in found["tasks"]]
                return SkillResult(text="\n".join(lines), speak=f"I have notes mentioning {key}.")
            return SkillResult(text=f"I have nothing stored about “{key}”.", speak=f"I don't know anything about {key} yet.")

        if lowered.startswith("forget") or " forget " in f" {lowered} ":
            key = re.sub(r"^.*?forget (about )?", "", lowered).strip(" ?.")
            removed = memory.forget(key)
            ctx.host.refresh_memory()
            if removed is not None:
                return SkillResult(text=f"Forgotten: {key}", speak=f"I forgot {key}.")
            return SkillResult(text=f"I had nothing stored for “{key}”.", ok=False, speak="I had nothing stored for that.")

        if re.search(r"\bwhat do you know about me\b", lowered):
            stats = memory.stats()
            facts = "\n".join(f"• {k}: {v}" for k, v in list(memory.facts.items())[:20]) or "• (no facts yet)"
            return SkillResult(
                text=f"I know {stats['facts']} facts, {stats['notes']} notes and {stats['tasks_open']} open tasks.\n"
                     f"Facts:\n{facts}",
                speak=f"I remember {stats['facts']} facts about you and {stats['tasks_open']} open tasks.",
            )

        # add task — “add a task: call the bank” or “add milk to my todo list”
        match = (re.search(r"\b(?:add|create|new) (?:a )?(?:task|todo|to-do|reminder)\s*(?:to|:)?\s*(.+)$",
                           text, flags=re.IGNORECASE)
                 or re.search(r"\b(?:add|put)\s+(.+?)\s+(?:to|on)\s+(?:my\s+|the\s+)?"
                              r"(?:todo|to-do|task)s?\s+list\b", text, flags=re.IGNORECASE))
        if match:
            task = memory.add_task(match.group(1).strip(" ."))
            ctx.host.refresh_memory()
            return SkillResult(text=f"Task added: [{task.id}] {task.text}", speak=f"Added the task: {task.text}")

        # complete task
        match = re.search(r"\b(?:mark|complete|finish|done with)\s*(?:task\s*)?([\w ]+?)\s*(?:as )?(?:done|complete[d]?)?$", lowered)
        if match and re.search(r"\b(task|todo|\d)\b", lowered):
            needle = match.group(1).strip(" #.")
            task = memory.complete_task(needle)
            ctx.host.refresh_memory()
            if task:
                return SkillResult(text=f"Done: {task.text}", speak=f"Marked {task.text} as done.")
            return SkillResult(text=f"No open task matches “{needle}”.", ok=False,
                               speak="I could not find that task.")

        # list tasks
        if re.search(r"\b(tasks|todos|to-dos|reminders)\b", lowered) and re.search(
                r"\b(my|list|show|what|any|pending|open|all)\b", lowered):
            open_tasks = memory.open_tasks()
            if not open_tasks:
                return SkillResult(text="No open tasks. Nice.", speak="You have no open tasks.")
            lines = [f"{index}. [{task.id}] {task.text}" for index, task in enumerate(open_tasks, 1)]
            return SkillResult(
                text=f"{len(open_tasks)} open tasks:\n" + "\n".join(lines),
                speak=f"You have {len(open_tasks)} open tasks. The first is: {open_tasks[0].text}",
            )

        # delete note
        match = re.search(r"\b(?:delete|remove) note\s*(.+)$", lowered)
        if match:
            note = memory.delete_note(match.group(1).strip(" ."))
            ctx.host.refresh_memory()
            if note:
                return SkillResult(text=f"Deleted note: {note.text}", speak="Note deleted.")
            return SkillResult(text="No matching note.", ok=False, speak="I could not find that note.")

        # search memory
        match = re.search(r"\b(?:search|find) my (?:notes|memory|tasks)\b(?: for)?\s*(.*)$", lowered)
        if match:
            query = match.group(1).strip(" ?.")
            if not query:
                return SkillResult(text="Tell me what to search for.", ok=False)
            found = memory.search(query)
            if not any(found.values()):
                return SkillResult(text=f"Nothing in memory matches “{query}”.", speak="Nothing matched.")
            lines = [f"Matches for “{query}”:"]
            lines += [f"• note [{n.id}] {n.when}: {n.text}" for n in found["notes"]]
            lines += [f"• task [{t.id}] {'✓' if t.done else '•'} {t.text}" for t in found["tasks"]]
            lines += [f"• fact {k} = {v}" for k, v in found["facts"]]
            return SkillResult(text="\n".join(lines), speak=f"Found {len(lines) - 1} matches.")

        # list notes
        if re.search(r"\b(notes|memory)\b", lowered):
            notes = memory.list_notes(10)
            if not notes:
                return SkillResult(text="No notes saved yet. Say “note that …” to add one.",
                                   speak="You have no notes yet.")
            lines = [f"{index}. [{note.id}] {note.when} — {note.text}" for index, note in enumerate(notes, 1)]
            return SkillResult(text="Recent notes:\n" + "\n".join(lines),
                               speak=f"You have {len(memory.notes)} notes. The latest is: {notes[0].text}")

        # add note
        match = re.search(r"\b(?:note|jot|write) (?:this |that |it )?(?:down )?(?:that |:)?\s*(.+)$",
                          text, flags=re.IGNORECASE)
        if match:
            body = match.group(1).strip(" .")
            if body:
                note = memory.add_note(body)
                ctx.host.refresh_memory()
                return SkillResult(text=f"Saved note [{note.id}]: {note.text}", speak="Noted.")
        return SkillResult(
            text="I can save notes, tasks and facts. Try “note that …”, “add task …”, "
                 "“remember my wifi password is …”, or “what are my tasks”.",
            speak="Tell me what to remember.",
        )


# ════════════════════════════════════════════════════════════════════════════
#  Timers & reminders
# ════════════════════════════════════════════════════════════════════════════

DURATION_UNITS = {
    "second": 1, "seconds": 1, "sec": 1, "secs": 1, "s": 1,
    "minute": 60, "minutes": 60, "min": 60, "mins": 60, "m": 60,
    "hour": 3600, "hours": 3600, "hr": 3600, "hrs": 3600, "h": 3600,
}


def parse_duration(text: str) -> int | None:
    """'in 10 minutes' / 'for 45s' / '1 hour 30 minutes' -> seconds."""
    total = 0
    found = False
    for amount, unit in re.findall(r"(\d+(?:\.\d+)?)\s*(seconds?|secs?|s|minutes?|mins?|m|hours?|hrs?|h)\b", text.lower()):
        total += float(amount) * DURATION_UNITS[unit]
        found = True
    if not found:
        match = re.search(r"\bin (\d+)\b", text.lower())
        if match:
            total = float(match.group(1)) * 60
            found = True
    return int(total) if found and total > 0 else None


class TimerSkill(Skill):
    name = "timer"
    title = "Timers & reminders"
    description = "countdown timers and spoken reminders"
    examples = ("set a timer for 10 minutes", "remind me in 2 minutes to stretch",
                "list timers", "cancel all timers")
    patterns = (
        (r"\b(remind me|reminder) (in|at|to|about)\b", 0.95),
        (r"\b(set )?(a )?timer\b", 0.95),
        (r"\b(list|show|cancel|stop|clear)\b.*\b(timers?|reminders?|alarms?|countdowns?)\b", 0.95),
        (r"^\s*(timers?|reminders?|alarms?|countdowns?)\s*\??\s*$", 0.95),
        (r"\bin \d+ (second|minute|hour)", 0.9),
        (r"\bwake me in\b", 0.9),
    )
    priority = 4

    def __init__(self) -> None:
        self.active: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def _labels(self, ctx: SkillContext) -> str:
        with self._lock:
            snapshot = dict(self.active)
        if not snapshot:
            return "No timers running."
        lines = []
        now = time.time()
        for key, entry in sorted(snapshot.items(), key=lambda kv: kv[1]["due"]):
            remaining = max(0, entry["due"] - now)
            lines.append(f"{key}: {int(remaining // 60)}m {int(remaining % 60)}s left — {entry['label']}")
        return "\n".join(lines)

    def run(self, text: str, ctx: SkillContext) -> SkillResult:
        lowered = text.lower()

        if re.search(r"\b(list|show)\b", lowered):
            return SkillResult(text=self._labels(ctx), speak=self._labels(ctx))

        if re.search(r"\b(cancel|stop|clear|delete|forget)\b", lowered):
            if not self.active:
                return SkillResult(text="No timers to cancel.", speak="There are no timers running.")
            target = re.search(r"(\d+)", lowered)
            if target and target.group(1) in self.active:
                handle = self.active.pop(target.group(1))
                ctx.host.cancel_schedule(handle["handle"])
                ctx.host.refresh_memory()
                return SkillResult(text=f"Cancelled timer {target.group(1)}.", speak="Timer cancelled.")
            for entry in self.active.values():
                ctx.host.cancel_schedule(entry["handle"])
            count = len(self.active)
            self.active.clear()
            ctx.host.refresh_memory()
            return SkillResult(text=f"Cancelled {count} timer(s).", speak=f"Cancelled {count} timers.")

        seconds = parse_duration(lowered)
        if not seconds:
            return SkillResult(
                text="Tell me how long, e.g. “set a timer for 10 minutes” or “remind me in 30 seconds to stretch”.",
                speak="How long should the timer be?", ok=False,
            )
        if seconds > 24 * 3600:
            return SkillResult(text="That is more than a day — I can only hold timers up to 24 hours.",
                               speak="That timer is too long.", ok=False)

        label = re.sub(
            r"\b(set )?(a )?(timer|reminder|alarm|countdown)\b|\bremind me\b|\bin\b|"
            r"\d+(\.\d+)?\s*(seconds?|secs?|s|minutes?|mins?|m|hours?|hrs?|h)\b|\bfor\b|\bto\b|\bplease\b",
            " ", lowered,
        )
        label = re.sub(r"\s+", " ", label).strip(" .,")
        key = str(len(self.active) + 1)

        def fire() -> None:
            with self._lock:
                self.active.pop(key, None)
            ctx.host.notify("JARVIS reminder", label or f"Timer {key} finished", level="alarm")
            ctx.host.speak(f"Reminder: {label}" if label else f"Timer {key} finished.")
            ctx.host.log(f"⏰ Reminder fired: {label or key}", level="alarm")
            ctx.host.refresh_memory()

        handle = ctx.host.schedule(float(seconds), fire)
        with self._lock:
            self.active[key] = {"handle": handle, "due": time.time() + seconds, "label": label or "timer"}
        ctx.host.refresh_memory()

        pretty = f"{seconds // 3600}h {(seconds % 3600) // 60}m {seconds % 60}s".strip()
        pretty = re.sub(r"\b0[hms]\b", "", pretty).strip() or f"{seconds}s"
        message = f"Timer {key} set for {pretty}" + (f" — {label}" if label else "")
        return SkillResult(text=message, speak=message + ".", data={"seconds": seconds, "label": label})


# ════════════════════════════════════════════════════════════════════════════
#  Clipboard & screenshots
# ════════════════════════════════════════════════════════════════════════════

class ClipboardSkill(Skill):
    name = "clipboard"
    title = "Clipboard"
    description = "read from and write to the clipboard"
    examples = ("what's on my clipboard", "copy hello world to clipboard")
    patterns = (
        (r"\b(what('s| is) (on|in)|read|show|see|check) (my )?(the )?(clipboard|clip board)\b", 0.95),
        (r"\b(copy|put|save|set)\b.*\bclipboard\b", 0.95),
        (r"\bcopy (this|that|the following)\b", 0.8),
        (r"\b(clipboard|clip board)\b", 0.7),
        (r"\bpaste\b", 0.7),
    )

    def run(self, text: str, ctx: SkillContext) -> SkillResult:
        lowered = text.lower()
        if re.search(r"\b(copy|put|save)\b", lowered) or re.search(r"\bset (the )?clipboard\b", lowered):
            payload = re.sub(
                r"^.*?\b(copy|put|save|set)\b( this| that| it| the following| to (the )?clipboard)*\s*:?\s*",
                "", text, flags=re.IGNORECASE,
            ).strip()
            payload = re.sub(r"\bto (the )?clipboard\b|\binto (the )?clipboard\b", "", payload, flags=re.IGNORECASE).strip()
            if not payload:
                return SkillResult(text="Tell me what to copy, e.g. “copy hello world to clipboard”.",
                                   speak="What should I copy?", ok=False)
            if ctx.host.set_clipboard(payload):
                preview = payload if len(payload) < 80 else payload[:77] + "…"
                return SkillResult(text=f"Copied to clipboard: {preview}", speak="Copied to your clipboard.",
                                   data={"length": len(payload)})
            return SkillResult(text="Clipboard access failed on this system.", ok=False,
                               speak="I could not write to the clipboard.")

        content = ctx.host.get_clipboard()
        if not content:
            return SkillResult(text="The clipboard is empty (or inaccessible on this system).",
                               speak="Your clipboard looks empty.")
        preview = content if len(content) < 600 else content[:600] + "…"
        return SkillResult(text=f"Clipboard ({len(content)} characters):\n{preview}",
                           speak=f"Your clipboard has {len(content)} characters." + (f" It starts with {content[:80]}." if content else ""),
                           data={"content": content})


class ClipboardReviewSkill(Skill):
    """“review whatever is on my clipboard” — the original tool's workflow, minus OCR."""

    name = "clipboard_review"
    title = "Review clipboard code"
    description = "run the offline code reviewer on the clipboard"
    examples = ("review my clipboard", "check the code on my clipboard")
    patterns = ((r"\b(review|check|analyse|analyze|scan)\b.*\bclipboard\b", 0.99),)
    priority = 6

    def run(self, text: str, ctx: SkillContext) -> SkillResult:
        from .analyzers import analyze_code, extract_code_from_text

        content = ctx.host.get_clipboard()
        if not content.strip():
            return SkillResult(text="The clipboard is empty — copy some code first.", ok=False,
                               speak="Your clipboard is empty.")
        code = extract_code_from_text(content)
        report = analyze_code(code)
        deeper = ""
        if ctx.llm_ready() and report.counts()["critical"]:
            deeper = ctx.ask_llm(
                f"Review this {report.language} code and give the three most important fixes:\n\n{code[:3000]}",
                "Be concise: a numbered list, each item one sentence.",
            )
        text_out = report.summary()
        if deeper:
            text_out += f"\n\nDeeper review:\n{deeper}"
        return SkillResult(text=text_out, speak=f"Reviewed the clipboard. {report.worst} issues found.",
                           data={"report": report.to_dict()})


class ScreenshotSkill(Skill):
    name = "screenshot"
    title = "Screenshot"
    description = "capture the screen to a file"
    examples = ("take a screenshot", "screenshot the screen")
    patterns = (
        (r"\b(screenshot|screen shot|screen grab|capture) (the )?(screen|display|desktop)\b", 0.95),
        (r"\btake a (screenshot|screen shot|photo of the screen)\b", 0.97),
    )

    def run(self, text: str, ctx: SkillContext) -> SkillResult:
        path = ctx.host.screenshot()
        if path:
            return SkillResult(text=f"Screenshot saved to {path}", speak="Screenshot saved.",
                               data={"path": path}, followups=["open that folder"])
        return SkillResult(
            text="I could not grab the screen. Install Pillow + mss (pip install pillow mss) "
                 "or run me on a machine with a display.",
            ok=False, speak="The screenshot failed.",
        )


# ════════════════════════════════════════════════════════════════════════════
#  Briefing
# ════════════════════════════════════════════════════════════════════════════

class BriefingSkill(Skill):
    name = "briefing"
    title = "Daily briefing"
    description = "time, weather, tasks and system state in one answer"
    examples = ("brief me", "good morning")
    patterns = (
        (r"\bbrief(ing)?\b", 0.95),
        (r"\bgood (morning|afternoon|evening|night)\b", 0.9),
        (r"\bcatch me up\b", 0.9),
        (r"\bwhat('s| is) (on )?(my )?(agenda|schedule|plan) (today|for today)\b", 0.9),
        (r"\bstart my day\b", 0.95),
    )

    def run(self, text: str, ctx: SkillContext) -> SkillResult:
        import datetime as _dt

        now = _dt.datetime.now()
        greeting = {range(4, 12): "Good morning", range(12, 17): "Good afternoon",
                    range(17, 22): "Good evening"}.get(
            next((r for r in (range(4, 12), range(12, 17), range(17, 22), range(22, 24)) if now.hour in r), None),
            "Hello",
        )
        name = ctx.settings.get("user_name") or ""
        lines = [f"{greeting}{', ' + name if name else ''}. It is {now.strftime('%H:%M on %A, %d %B')}."]

        open_tasks = ctx.memory.open_tasks()
        if open_tasks:
            lines.append(f"You have {len(open_tasks)} open task(s):")
            lines += [f"• {task.text}" for task in open_tasks[:5]]
        else:
            lines.append("No open tasks.")

        timers = ctx.state.get("timers_summary")
        if timers:
            lines.append(timers)

        weather = ctx.state.get("weather_line")
        if weather:
            lines.append(weather)

        system = ctx.state.get("system_line")
        if system:
            lines.append(system)

        return SkillResult(
            text="\n".join(lines),
            speak=f"{greeting}. It is {now.strftime('%H:%M')}. "
                  + (f"You have {len(open_tasks)} open tasks." if open_tasks else "You have no open tasks."),
        )


def default_skills() -> list[Skill]:
    """Skills minus the dev-oriented ones (those live in :mod:`jarvis.dev_skills`)."""
    return [
        HelpSkill(),
        IdentitySkill(),
        StatusSkill(),
        TimeSkill(),
        MathSkill(),
        WeatherSkill(),
        NetworkSkill(),
        WikiSkill(),
        SystemSkill(),
        MediaSkill(),
        PowerSkill(),
        AppSkill(),
        NotesSkill(),
        TimerSkill(),
        ClipboardSkill(),
        ScreenshotSkill(),
        BriefingSkill(),
    ]
