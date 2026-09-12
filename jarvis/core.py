"""
The JARVIS core: routing, context and orchestration.

``Core.ask("what's the weather")`` does the following, in order:

1. strip a wake word if the utterance came from the microphone,
2. handle slash commands (``/help``, ``/clear``, ``/provider groq`` …),
3. route the text to the best offline skill,
4. if no skill matches and a language model is configured, ask the LLM with
   your memory facts and the recent conversation as context,
5. otherwise answer honestly about what it can do offline.

The core has **no Qt imports and no I/O of its own** — it talks to a
:class:`~jarvis.host.Host`, which makes the whole assistant testable headlessly.
"""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from .brain import Brain, Reply
from .config import PROVIDER_LABELS, VERSION, Settings
from .dev_skills import default_dev_skills
from .host import Host
from .memory import Memory
from .skills import (
    BriefingSkill,
    HelpSkill,
    SkillContext,
    SkillRegistry,
    SkillResult,
    default_skills,
)


@dataclass
class Response:
    """Everything the UI needs in order to display and speak one answer."""

    text: str
    speak: str = ""
    ok: bool = True
    skill: str = ""
    provider: str = "offline"
    error: str = ""
    duration: float = 0.0
    data: dict[str, Any] = field(default_factory=dict)
    ui_action: dict[str, Any] = field(default_factory=dict)

    @staticmethod
    def from_skill(result: SkillResult, duration: float = 0.0) -> Response:
        skill = str(result.data.get("skill", ""))
        return Response(
            text=result.text,
            speak=result.speak or result.spoken(),
            ok=result.ok,
            skill=skill,
            error=result.error,
            duration=duration,
            data=result.data,
        )


OFFLINE_FALLBACK = (
    "That one is outside my offline skill set, so I won't guess.\n\n"
    "Options:\n"
    "• Say /help to see the {count} things I can do right now.\n"
    "• Add an LLM key in Settings (Groq has a free tier) for open-ended answers.\n"
    "• Or run Ollama locally for a fully private brain."
)


class Core:
    """Route utterances, keep context, talk to the brain."""

    def __init__(self, settings: Settings, memory: Memory, host: Host) -> None:
        self.settings = settings
        self.memory = memory
        self.host = host
        self.brain = Brain(settings)
        self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="jarvis-core")
        self._context_cache: dict[str, Any] = {"ts": 0.0, "lines": {}}

        registry = SkillRegistry()
        for skill in default_skills() + default_dev_skills():
            registry.register(skill)
        self.registry = registry
        # help needs the final skill list
        for skill in registry.skills:
            if isinstance(skill, HelpSkill):
                skill._get_registry = lambda: self.registry  # type: ignore[assignment]

        self.ctx = SkillContext(
            settings=settings,
            memory=memory,
            host=host,
            llm=self._llm_call,
            state=self._state(),
        )
        self._lock = threading.Lock()

    # ── state shared with skills ─────────────────────────────────────────
    def _state(self) -> dict[str, Any]:
        return {
            "llm_ready": self.brain.ready,
            "provider_label": PROVIDER_LABELS.get(self.brain.provider, self.brain.provider),
            "provider": self.brain.provider,
            "voice_status": {"tts": "unknown", "stt": "unknown", "wake": "off"},
            "cwd": None,
            "timers_summary": "",
            "weather_line": "",
            "system_line": "",
        }

    def refresh_state(self, **updates: Any) -> None:
        with self._lock:
            self.ctx.state.update(updates)
            self.ctx.state["llm_ready"] = self.brain.ready
            self.ctx.state["provider_label"] = PROVIDER_LABELS.get(self.brain.provider, self.brain.provider)

    def set_voice_status(self, status: dict[str, Any]) -> None:
        self.refresh_state(voice_status=status)

    # ── body of the assistant ────────────────────────────────────────────
    def ask(self, text: str, source: str = "text", remember: bool = True) -> Response:
        started = time.time()
        raw = (text or "").strip()
        if not raw:
            return Response(text="I didn't catch that.", ok=False, speak="I did not catch that.")

        cleaned, woken = self.strip_wake_word(raw)
        if woken:
            cleaned = cleaned or raw
        if not cleaned:
            return Response(text="Yes?", speak="Yes?")

        self.memory.append_turn("user", cleaned)
        self.host.log(cleaned, level="user")

        # slash commands first
        if cleaned.startswith("/"):
            response = self._command(cleaned)
            response.duration = time.time() - started
            self._finish(response, remember)
            return response

        self.ctx.state["timers_summary"] = self._timer_summary()
        self.ctx.state["cwd"] = str(self._cwd())

        result = self.registry.handle(cleaned, self.ctx)
        if result is not None and result.ok:
            response = Response.from_skill(result, time.time() - started)
            response.provider = "skills"
            self._finish(response, remember)
            return response

        if result is not None and not result.ok and result.data.get("score", 0) >= 0.9:
            # A confident skill said it cannot do this — report that, do not
            # silently jump to the LLM (the user asked for something specific).
            response = Response.from_skill(result, time.time() - started)
            self._finish(response, remember)
            return response

        # LLM fallback
        if self.brain.enabled:
            reply = self._llm_call(cleaned)
            if getattr(reply, "ok", False):
                response = Response(
                    text=reply.text,
                    speak=reply.text,
                    skill="llm",
                    provider=reply.provider,
                    duration=time.time() - started,
                    data={"model": getattr(reply, "model", "")},
                )
                self._finish(response, remember)
                return response
            error = getattr(reply, "error", "") or "The language model did not answer."
            response = Response(
                text=f"{error}\n\nMeanwhile, here is what I can do offline: /help",
                ok=False,
                skill="llm",
                provider=reply.provider,
                error=error,
                duration=time.time() - started,
            )
            self._finish(response, remember)
            return response

        fallback = OFFLINE_FALLBACK.format(count=len(self.registry.skills))
        response = Response(text=fallback, skill="fallback", duration=time.time() - started)
        self._finish(response, remember)
        return response

    def ask_async(self, text: str, callback: Callable[[Response], None], source: str = "text") -> None:
        """Run :meth:`ask` on a worker thread; ``callback`` fires with the response."""

        def work() -> None:
            try:
                response = self.ask(text, source=source)
            except Exception as exc:  # pragma: no cover - defensive
                response = Response(text=f"Something went wrong: {exc}", ok=False, error=str(exc))
            try:
                callback(response)
            except Exception:
                pass

        self._executor.submit(work)

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)

    # ── wake word ────────────────────────────────────────────────────────
    def strip_wake_word(self, text: str) -> tuple[str, bool]:
        wake = str(self.settings.get("wake_word", "jarvis")).strip().lower()
        if not wake:
            return text, False
        pattern = rf"^\s*(hey\s+|ok\s+|okay\s+|yo\s+)?{re.escape(wake)}[\s,\.!:-]*(.*)$"
        match = re.match(pattern, text, flags=re.IGNORECASE | re.DOTALL)
        if match:
            return match.group(2).strip(), True
        return text, False

    # ── slash commands ───────────────────────────────────────────────────
    def _command(self, text: str) -> Response:
        parts = text[1:].split(maxsplit=1)
        command = parts[0].lower()
        argument = parts[1].strip() if len(parts) > 1 else ""

        if command in {"help", "skills", "commands"}:
            return Response(text=self.registry.help_text(), skill="help", provider="skills")

        if command in {"clear", "cls", "reset", "new"}:
            self.memory.clear_history()
            return Response(
                text="Conversation cleared. Memory (notes, tasks, facts) is untouched.",
                speak="Cleared.",
                ui_action={"clear_conversation": True},
                skill="clear",
            )

        if command in {"brief", "briefing", "morning"}:
            skill = self.registry.find("briefing")
            if isinstance(skill, BriefingSkill):
                self.ctx.state["timers_summary"] = self._timer_summary()
                self.ctx.state["weather_line"] = self._context_cache["lines"].get("weather", "")
                self.ctx.state["system_line"] = self._context_cache["lines"].get("system", "")
                return Response.from_skill(skill.run("brief me", self.ctx))
            return Response(text="Briefing skill missing.", ok=False)

        if command in {"llm", "brain"}:
            if argument in {"on", "off"}:
                self.settings["provider"] = "ollama" if argument == "on" else "offline"
                if argument == "on" and not self.brain.ready:
                    return Response(
                        text="Switched to Ollama, but it is not reachable. Start `ollama serve` or pick "
                             "Groq/OpenAI in Settings and paste a key.",
                        ok=False,
                        ui_action={"refresh_settings": True},
                    )
                self.settings.save()
                self.refresh_state()
                return Response(
                    text=f"Brain switched to {self.brain.provider}. {self.brain.status()}",
                    ui_action={"refresh_settings": True},
                )
            return Response(text=f"LLM: {self.brain.status()}\nUse /llm on|off or the Settings tab.")

        if command == "provider":
            provider = argument.lower().strip()
            if provider not in PROVIDER_LABELS:
                return Response(text=f"Providers: {', '.join(PROVIDER_LABELS)}", ok=False)
            self.settings["provider"] = provider
            self.settings.save()
            self.refresh_state()
            return Response(text=f"Provider set to {provider}. {self.brain.status()}",
                            ui_action={"refresh_settings": True})

        if command in {"voice", "tts"}:
            if argument in {"on", "off"}:
                self.settings["voice_replies"] = argument == "on"
                self.settings.save()
                return Response(text=f"Spoken replies {'enabled' if argument == 'on' else 'disabled'}.",
                                ui_action={"refresh_settings": True})
            return Response(text=f"Spoken replies are {'on' if self.settings.get('voice_replies') else 'off'}.")

        if command in {"memory", "memos"}:
            stats = self.memory.stats()
            summary = (
                f"Memory: {stats['notes']} notes · {stats['tasks_open']} open tasks · "
                f"{stats['tasks_done']} done · {stats['facts']} facts · {stats['turns']} turns\n"
                f"Stored at {self.memory.path}"
            )
            if argument == "export":
                exported = self.memory.path.parent / "memory-export.txt"
                exported.write_text(self.memory.export_text(), encoding="utf-8")
                summary += f"\nExported to {exported}"
            return Response(text=summary, ui_action={"refresh_memory": True})

        if command in {"timers", "reminders"}:
            return Response(text=self._timer_summary(), ui_action={"focus": "timers"})

        if command in {"status", "diagnostics"}:
            skill = self.registry.find("status")
            if skill:
                return Response.from_skill(skill.run("status", self.ctx))
            return Response(text=self.brain.status())

        if command in {"quit", "exit", "bye"}:
            return Response(text="Shutting down. Goodbye.", speak="Goodbye.", ui_action={"quit": True})

        if command in {"settings", "prefs"}:
            return Response(text="Opening Settings.", ui_action={"focus": "settings"})

        if command == "version":
            return Response(text=f"JARVIS v{VERSION}")

        return Response(text=f"Unknown command “/{command}”. Try /help.", ok=False)

    # ── helpers ──────────────────────────────────────────────────────────
    def _llm_call(self, question: str, extra_system: str = "") -> Reply:
        return self.brain.ask(
            question,
            history=self.memory.recent(8),
            facts=self.memory.facts,
            extra_system=extra_system,
        )

    def _finish(self, response: Response, remember: bool) -> None:
        if response.ui_action.get("clear_conversation"):
            remember = False
        if remember and response.text:
            self.memory.append_turn("jarvis", response.text, {"skill": response.skill, "ok": response.ok})
        self.host.log(
            response.text,
            level="error" if not response.ok else "jarvis",
            meta={
                "skill": response.skill or response.provider,
                "duration": round(response.duration, 2),
                "ok": response.ok,
            },
        )
        if remember and response.speak and self.settings.get("voice_replies"):
            self.host.speak(response.speak)

    def _timer_summary(self) -> str:
        skill = self.registry.find("timer")
        if skill is None:
            return ""
        labels = getattr(skill, "_labels", None)
        if callable(labels):
            try:
                text = labels(self.ctx)
            except Exception:
                return ""
            if text and text != "No timers running.":
                return "Timers:\n" + text
        return ""

    def _cwd(self):
        import os

        return os.getcwd()

    # ── proactive context (used by the briefing and the dashboard) ───────
    def context_lines(self, max_age: float = 600.0) -> dict[str, str]:
        """Cached one-liners: weather + system. Refreshed at most every 10 min."""
        now = time.time()
        if now - self._context_cache["ts"] > max_age or not self._context_cache["lines"]:
            lines: dict[str, str] = {}
            weather = self.registry.find("weather")
            if weather:
                try:
                    result = weather.run(f"weather in {self.settings.get('city')}", self.ctx)
                    if result.ok and result.text:
                        lines["weather"] = result.text.splitlines()[0]
                except Exception:
                    pass
            system = self.registry.find("system")
            if system:
                try:
                    result = system.run("system status", self.ctx)
                    if result.ok:
                        keep = [ln for ln in result.text.splitlines() if ln.startswith(("CPU", "Memory", "Battery"))]
                        lines["system"] = " · ".join(keep)
                        lines["system_full"] = result.text
                except Exception:
                    pass
            self._context_cache = {"ts": now, "lines": lines}
        return dict(self._context_cache["lines"])

    def greeting(self) -> Response:
        name = self.settings.get("user_name") or ""
        hour = time.localtime().tm_hour
        part = "morning" if hour < 12 else ("afternoon" if hour < 17 else "evening")
        capabilities = len(self.registry.skills)
        brain = PROVIDER_LABELS.get(self.brain.provider, self.brain.provider)
        lines = [
            f"Good {part}{', ' + name if name else ''}. JARVIS v{VERSION} is online.",
            f"{capabilities} skills loaded · brain: {brain}",
        ]
        voice = self.ctx.state.get("voice_status", {})
        if voice:
            lines.append(f"Voice in: {voice.get('stt', 'unknown')} · Voice out: {voice.get('tts', 'unknown')}")
        lines.append("Say “help” for the full list, or just ask.")
        return Response(text="\n".join(lines), speak=f"Good {part}. JARVIS is online and ready.",
                        skill="system", provider="skills")
