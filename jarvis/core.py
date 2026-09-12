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

import json
import re
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from .actions import ActionRegistry
from .brain import Brain, Reply
from .config import PROVIDER_LABELS, VERSION, Settings
from .control import Controller
from .control_skills import default_control_skills, maybe_suggest_routine
from .dev_skills import default_dev_skills
from .host import Host
from .learning import BehaviourProfile
from .memory import Memory
from .planner import Planner
from .routines import RoutineRecorder, RoutineRunner, RoutineStore, Timeline
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

        # ── computer control ─────────────────────────────────────────────
        # The controller, action registry, routine store and planner are built
        # once here and shared through the skill context, so the window, the CLI
        # and the tests all drive exactly the same machinery.
        # ── learning ─────────────────────────────────────────────────────
        # The behaviour profile is what makes JARVIS adapt: it remembers the
        # phrasings that work, the apps you live in, when you do things, and
        # which confirmations you always approve.
        self.profile = BehaviourProfile(
            save_interval=float(settings.get("profile_save_interval", 5) or 5),
            enabled=bool(settings.get("learn_habits", True)),
        )
        self._last_miss: tuple[str, float] | None = None
        self._last_ritual_offer = 0.0
        # Thread-local so a streaming answer in the window cannot leak its
        # callback into a parallel CLI question.
        self._local = threading.local()
        self.control_enabled = bool(settings.get("control_enabled", True))
        self.controller = Controller(settings) if self.control_enabled else None
        self.actions: ActionRegistry | None = None
        self.audit = None
        self.timeline: Timeline | None = None
        self.routine_store: RoutineStore | None = None
        self.recorder: RoutineRecorder | None = None
        self.routine_runner: RoutineRunner | None = None
        self.planner: Planner | None = None
        if self.control_enabled:
            self.actions = ActionRegistry(settings, memory, host, self.controller, core=self,
                                          profile=self.profile)
            self.audit = self.actions.audit
            self.timeline = Timeline(limit=int(settings.get("routine_log_limit", 200) or 200))
            self.routine_store = RoutineStore(memory)
            self.recorder = RoutineRecorder(self.timeline)
            self.actions.recorder = self.recorder
            self.actions.timeline = self.timeline
            self.routine_runner = RoutineRunner(self.actions, self.timeline, self.routine_store)
            self.planner = Planner(self.actions, llm=self._llm_call if self.brain.enabled else None)
            for skill in default_control_skills():
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
        self.ctx.state.update({
            "control": self.actions,
            "controller": self.controller,
            "routine_store": self.routine_store,
            "routine_timeline": self.timeline,
            "routine_recorder": self.recorder,
            "routine_runner": self.routine_runner,
            "planner": self.planner,
            "profile": self.profile,
            "brain": self.brain,
        })
        self._lock = threading.Lock()

    # ── state shared with skills ─────────────────────────────────────────
    def _state(self) -> dict[str, Any]:
        return {
            "llm_ready": self.brain.ready,
            "provider_label": PROVIDER_LABELS.get(self.brain.provider, self.brain.provider),
            "provider": self.brain.provider,
            "brain_status": self.brain.status(),
            "voice_status": {"tts": "unknown", "stt": "unknown", "wake": "off"},
            "cwd": None,
            "profile": self.profile,
            "brain": self.brain,
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
        self._local.utterance = cleaned
        if not cleaned:
            return Response(text="Yes?", speak="Yes?")

        # A control action waiting for a spoken “yes” gets first refusal: the user
        # is answering the question JARVIS just asked, not starting a new request.
        pending = self._resolve_pending(cleaned)
        if pending is not None:
            pending.duration = time.time() - started
            self.memory.append_turn("user", cleaned)
            self.host.log(cleaned, level="user")
            self._finish(pending, remember)
            return pending

        self.memory.append_turn("user", cleaned)
        self.host.log(cleaned, level="user")

        # slash commands first
        if cleaned.startswith("/"):
            response = self._command(cleaned)
            response.duration = time.time() - started
            self._finish(response, remember)
            return response

        alias_response = self._maybe_run_alias(cleaned)
        if alias_response is not None:
            alias_response.duration = time.time() - started
            self._finish(alias_response, remember)
            return alias_response

        routine_response = self._maybe_run_routine(cleaned)
        if routine_response is not None:
            routine_response.duration = time.time() - started
            self._finish(routine_response, remember)
            return routine_response

        self.ctx.state["timers_summary"] = self._timer_summary()
        self.ctx.state["cwd"] = str(self._cwd())

        result = self.registry.handle(cleaned, self.ctx)
        if result is not None and result.ok:
            response = Response.from_skill(result, time.time() - started)
            response.provider = "skills"
            self._maybe_offer_routine(response)
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

    def ask_async(self, text: str, callback: Callable[[Response], None], source: str = "text",
                  on_chunk: Callable[[str], None] | None = None) -> None:
        """Run :meth:`ask` on a worker thread; ``callback`` fires with the response.

        ``on_chunk`` (optional) receives streamed pieces of a model answer as they
        arrive, so the window can start showing text while the rest is still on
        its way.
        """

        def work() -> None:
            if on_chunk is not None and self.settings.get("stream_replies", True):
                self._local.stream_hook = on_chunk
            try:
                response = self.ask(text, source=source)
            except Exception as exc:  # pragma: no cover - defensive
                response = Response(text=f"Something went wrong: {exc}", ok=False, error=str(exc))
            finally:
                self._local.stream_hook = None
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

        if command in {"actions", "control"}:
            if self.actions is None:
                return Response(text="Computer control is disabled in settings.", ok=False)
            text = self.actions.catalogue()
            if self.controller is not None:
                text += "\n\n" + self.controller.report_text()
            return Response(text=text, ui_action={"focus": "settings"})

        if command in {"audit", "log"}:
            if self.audit is None:
                return Response(text="Computer control is disabled in settings.", ok=False)
            entries = self.audit.tail(15)
            if not entries:
                return Response(text=f"Nothing logged yet. The log lives at {self.audit.path}")
            lines = [f"Action log — {self.audit.path}"]
            marks = {"done": "✓", "failed": "✗", "refused": "⛔", "declined": "✗", "auto": "·",
                     "awaiting_voice_confirmation": "…"}
            for entry in entries:
                args = ", ".join(f"{key}={value}" for key, value in list((entry.get("args") or {}).items())[:2])
                lines.append(f"  {marks.get(str(entry.get('outcome')), '·')} {str(entry.get('when', ''))[11:16]} "
                             f"{entry.get('action')}({args}) — {str(entry.get('message', ''))[:60]}")
            return Response(text="\n".join(lines))

        if command in {"routines", "macros"}:
            if self.routine_store is None:
                return Response(text="Computer control is disabled in settings.", ok=False)
            routines = self.routine_store.all()
            if not routines:
                return Response(
                    text="No routines saved yet.\n\n"
                         "• Say “watch what I do”, perform the steps, then “save that as work session”\n"
                         "• Or let me notice a habit: run the same thing twice and I will offer to save it.",
                )
            lines = [f"{len(routines)} routine(s) in {self.memory.path}:"]
            for routine in routines:
                lines.append(f"• {routine.summary(self.actions)}")
            return Response(text="\n".join(lines), ui_action={"refresh_memory": True})

        if command in {"profile", "learned", "learning"}:
            stats = self.profile.stats()
            head = (f"Behaviour profile — {stats['distinct_actions']} actions, "
                    f"{stats['phrases']} phrases, {stats['aliases']} aliases, "
                    f"{stats['trusted']} learned approvals\n"
                    f"File: {self.profile.path}\n")
            if argument == "reset":
                self.profile.reset()
                return Response(text="Behaviour profile reset — I'll start learning again from now.",
                                skill="profile")
            if argument == "off":
                self.settings["learn_habits"] = False
                return Response(text="Learning switched off. /profile on to resume.", skill="profile")
            if argument == "on":
                self.settings["learn_habits"] = True
                return Response(text="Learning switched on.", skill="profile")
            return Response(text=head + "\n" + self.profile.summary(), skill="profile")

        if command in {"aliases", "shortcuts"}:
            aliases = self.profile.aliases()
            if not aliases:
                return Response(text="No aliases yet. Teach one with:\n"
                                     "  /teach chill = open spotify, then mute\n"
                                     "or just say the short phrase and then the real command — "
                                     "I'll connect the two.")
            lines = [f"{len(aliases)} alias(es):"]
            for phrase, info in sorted(aliases.items()):
                target = info.get("routine") or info.get("action") or "?"
                if info.get("action") and info.get("args"):
                    target = f"{info['action']}({', '.join(f'{k}={v}' for k, v in info['args'].items())})"
                if info.get("routine"):
                    target = f"routine “{info['routine']}”"
                lines.append(f"• “{phrase}” → {target}  ({info.get('hits', 0)} uses"
                             f"{', taught' if info.get('taught') else ', learned'})")
            return Response(text="\n".join(lines), skill="profile")

        if command == "teach":
            return self._teach_alias(argument)

        if command in {"forget", "unlearn"}:
            if not argument:
                return Response(text="What should I forget? /forget chill", ok=False)
            removed = self.profile.forget_alias(argument)
            return Response(
                text=(f"Forgotten: “{argument}”." if removed
                      else f"I have no alias called “{argument}”."),
                ok=removed, skill="profile")

        if command in {"stats", "usage"}:
            stats = self.profile.stats()
            return Response(text=self.profile.summary() + "\n\n" + json.dumps(stats, indent=2),
                            skill="profile")

        if command in {"cloud", "free"}:
            lines = ["Free providers (no credit card, no key needed for the first one):"]
            for name, label in PROVIDER_LABELS.items():
                if name == "offline":
                    continue
                ready = self.brain.usable(name)
                lines.append(f"  {'●' if ready else '○'} {name:<12} {label}"
                             f"{'  ← in use' if name == self.brain.provider else ''}")
            lines.append("\nSwitch with /provider <name>. The ladder is: "
                         + " → ".join(self.brain.ladder()))
            lines.append("Connection: " + ("the last attempt failed — no route out right now"
                                           if self.brain.offline
                                           else "no connection problem recorded"))
            return Response(text="\n".join(lines), skill="brain")

        if command in {"settings", "prefs"}:
            return Response(text="Opening Settings.", ui_action={"focus": "settings"})

        if command == "version":
            return Response(text=f"JARVIS v{VERSION}")

        return Response(text=f"Unknown command “/{command}”. Try /help.", ok=False)

    RUN_ROUTINE = re.compile(
        r"^(?:please\s+)?(?:run|do|start|begin|execute|launch|perform|replay)\s+"
        r"(?:my\s+|the\s+)?(?P<name>.+?)(?:\s+routine|\s+macro)?(?:\s+again)?$",
        re.IGNORECASE)

    def _maybe_run_routine(self, text: str) -> Response | None:
        """“run my work session” → the saved routine, if one goes by that name.

        Routines are the user's own vocabulary, so they get looked up before the
        general “open/run something” grammar: a saved name always wins, and an
        unknown name simply falls through to the normal routing.
        """
        if self.routine_store is None or self.actions is None:
            return None
        match = self.RUN_ROUTINE.match(text.strip())
        if not match:
            return None
        name = match.group("name").strip().strip("“”\"'")
        if not name:
            return None
        routine = self.routine_store.get(name)
        if routine is None:
            return None
        skill = self.registry.find("routine")
        if skill is None or not hasattr(skill, "run_routine"):
            return None
        result = skill.run_routine(routine, self.ctx, self.actions, self.routine_store)
        return Response(
            text=result.text, speak=result.speak, ok=result.ok, skill="routine",
            provider="skills", data=dict(result.data),
        )

    # ── spoken confirmations ─────────────────────────────────────────────
    YES_WORDS = {"yes", "yep", "yeah", "ok", "okay", "sure", "do it", "go ahead", "confirm",
                 "yes please", "please do", "affirmative", "y"}
    NO_WORDS = {"no", "nope", "stop", "cancel", "don't", "dont", "do not", "never mind",
                "nevermind", "abort", "no thanks", "n"}

    def _resolve_pending(self, text: str) -> Response | None:
        """Answer the last “may I?” question when the reply is yes or no."""
        if self.actions is None or not self.actions.pending:
            return None
        pending = self.actions.pending
        if time.time() - float(pending.get("asked", 0)) > 180:
            self.actions.pending = None
            return None
        answer = re.sub(r"[.!?]+$", "", text.strip().lower())
        if answer in self.YES_WORDS:
            self.actions.pending = None
            result = self.actions.execute(pending["action"], pending["args"],
                                          source=pending.get("source", "voice"), confirmed=True)
            return Response(
                text=result.message,
                speak=(result.message.splitlines() or ["Done."])[0][:200] if result.ok
                else "That did not work.",
                ok=result.ok, skill="control", provider="skills",
                data={"action": pending["action"], "confirmed": True},
            )
        if answer in self.NO_WORDS:
            self.actions.pending = None
            self.host.log(f"Cancelled: {pending.get('summary', '')}", level="info")
            return Response(text=f"Cancelled — nothing happened. ({pending.get('summary', '')})",
                            speak="Cancelled.", skill="control", provider="skills")
        # Anything else is a new request; drop the pending action so it cannot
        # surprise the user later.
        self.actions.pending = None
        self.host.log(f"Pending action dropped: {pending.get('summary', '')}", level="info")
        return None

    # ── helpers ──────────────────────────────────────────────────────────
    def _maybe_offer_routine(self, response: Response) -> None:
        """After a control action, offer to save it if it has become a habit."""
        if self.actions is None or self.recorder is None or self.recorder.active:
            return
        if not str(response.skill).startswith(("control", "plan")):
            return
        try:
            offer = maybe_suggest_routine(self.ctx)
        except Exception:
            return
        if offer:
            response.text = response.text.rstrip() + "\n\n— Habit spotted —\n" + offer
            return
        ritual = self._maybe_suggest_ritual(response)
        if ritual:
            response.text = response.text.rstrip() + "\n\n— Pattern spotted —\n" + ritual

    # ── behaviour learning ───────────────────────────────────────────────
    def _maybe_run_alias(self, text: str) -> Response | None:
        """A phrase you taught me (“chill”) runs exactly what it was taught."""
        if self.actions is None or not self.settings.get("learn_habits", True):
            return None
        try:
            found = self.profile.alias(text)
        except Exception:
            return None
        if not found:
            return None

        routine_name = str(found.get("routine") or "")
        if routine_name:
            if self.routine_store is None or self.routine_runner is None:
                return None
            routine = self.routine_store.get(routine_name)
            if routine is None:
                self.profile.forget_alias(text)          # nothing to run any more
                return None
            result = self.routine_runner.run(routine, source="alias")
            return Response(
                text=f"“{text}” → {routine.name}\n\n{result.report(self.actions)}",
                speak=result.spoken, ok=result.ok, skill="routine", provider="skills",
                error="" if result.ok else "some steps could not run",
                data={"alias": text, "routine": routine.name},
            )

        action = str(found.get("action") or "")
        if not action:
            return None
        result = self.actions.execute(action, dict(found.get("args") or {}), source="alias")
        message = result.message or ("Done." if result.ok else "That did not work.")
        hint = getattr(result, "hint", "") or ""
        return Response(
            text=f"“{text}” → {action.replace('_', ' ')}\n{message}" + (f"\n\n{hint}" if hint else ""),
            speak=message.splitlines()[0][:200], ok=result.ok, skill="control",
            provider="skills", error="" if result.ok else message,
            data={"alias": text, "action": action},
        )

    def _teach_alias(self, argument: str) -> Response:
        """``/teach <phrase> = <command>`` — wire a short phrase to a real command."""
        if "=" not in argument:
            return Response(
                text="Format: /teach <phrase> = <command>\n"
                     "Example: /teach chill = open spotify, then mute",
                ok=False, skill="profile")
        phrase, _, command = argument.partition("=")
        phrase, command = phrase.strip(), command.strip()
        if not phrase or not command:
            return Response(text="I need both halves — the phrase and what it should do.",
                            ok=False, skill="profile")
        if self.planner is None:
            return Response(text="Computer control is disabled, so aliases have nothing to run.",
                            ok=False, skill="profile")

        try:
            plan = self.planner.plan(command, use_llm=False)
        except Exception:
            plan = None
        steps = [(step.action, dict(step.args)) for step in (getattr(plan, "steps", None) or [])]
        if not steps:
            single = self.planner.parse(command)
            steps = [single] if single else []
        if not steps:
            return Response(
                text=f"I couldn't turn “{command}” into actions. Try a plainer command, "
                     "for example: /teach chill = open spotify",
                ok=False, skill="profile")

        if len(steps) == 1:
            action, args = steps[0]
            self.profile.learn_alias(phrase, action=action, args=args, taught=True)
            return Response(text=f"Got it — “{phrase}” now runs {action.replace('_', ' ')} "
                                 f"({', '.join(f'{k}={v}' for k, v in args.items()) or 'no arguments'}).",
                            skill="profile")

        routine = self._alias_routine(phrase, steps, taught=True)
        if routine is None:
            return Response(text="I couldn't save that sequence.", ok=False, skill="profile")
        return Response(text=f"Got it — “{phrase}” now runs {len(routine.steps)} steps:\n"
                             + routine.outline(self.actions), skill="profile")

    def _alias_routine(self, phrase: str, steps: list[tuple[str, dict[str, Any]]],
                       taught: bool = False) -> Any:
        """Turn a learned sequence into a saved routine named after the phrase."""
        if self.routine_store is None:
            return None
        from .routines import Routine, Step
        name = re.sub(r"[^\w -]+", "", phrase).strip()[:40] or "learned routine"
        existing = self.routine_store.get(name)
        routine = existing or Routine(name=name, description=f"Learned from “{phrase}”",
                                      source="taught" if taught else "learned")
        routine.steps = [Step(action=action, args=dict(args)) for action, args in steps]
        try:
            self.routine_store.save(routine)
        except Exception:
            return None
        self.profile.learn_alias(phrase, routine=routine.name,
                                 taught=taught or bool(existing and existing.source == "taught"))
        return routine

    def _learn_from_response(self, response: Response) -> None:
        """Everything JARVIS learns from one answered request."""
        if not self.settings.get("learn_habits", True):
            return
        utterance = str(getattr(self._local, "utterance", "") or "").strip()
        if not utterance:
            return
        try:
            self.profile.observe_utterance(utterance, skill=response.skill, ok=response.ok)
        except Exception:
            pass

        # Nothing understood it? Remember the phrase in case the next command is
        # the user showing me what they meant.
        if not response.ok or response.skill in {"llm", "fallback"}:
            if len(utterance.split()) <= 7:
                self._last_miss = (utterance, time.time())
            return
        if self._last_miss is None or self.actions is None:
            return
        missed, when = self._last_miss
        if time.time() - when > 120 or missed.lower() == utterance.lower():
            return
        if response.data.get("alias") or response.skill in {"routine"}:
            return

        steps = self.actions.recent_actions(since=when)
        if not steps:
            return
        self._last_miss = None
        try:
            if len(steps) == 1:
                action, args = steps[0]
                self.profile.learn_alias(missed, action=action, args=args)
                learned = f"{action.replace('_', ' ')}"
            else:
                routine = self._alias_routine(missed, steps)
                if routine is None:
                    return
                learned = f"{len(routine.steps)} steps"
        except Exception:
            return
        response.text = (response.text.rstrip()
                         + f"\n\n— Learned —\nI'll take “{missed}” to mean {learned} from now on. "
                           f"(/forget {missed} undoes it.)")

    def _maybe_suggest_ritual(self, response: Response) -> str:
        """Notice “you do this every morning” and offer to keep it as a routine."""
        if not self.settings.get("ritual_suggestions", True) or self.routine_store is None:
            return ""
        if time.time() - self._last_ritual_offer < 1800:
            return ""
        try:
            rituals = self.profile.rituals(tolerance=1)
        except Exception:
            return ""
        for item in rituals:
            action = str(item.get("action", ""))
            if not action or item.get("days", 0) < 2:
                continue
            if self.routine_store.get(action) is not None:
                continue
            self._last_ritual_offer = time.time()
            hour = int(item.get("hour", 0))
            when = f"{hour:02d}:00"
            return (f"You do {action.replace('_', ' ')} around {when} on "
                    f"{item.get('days')} different days — say “save my {when} routine” and "
                    f"I'll keep it, or “watch what I do” and I'll record the whole thing.")
        return ""

    def _llm_call(self, question: str, extra_system: str = "") -> Reply:
        """Ask the brain, streaming into the window when one is listening."""
        history = self.memory.recent(8)
        facts = self.memory.facts
        profile = self.profile.prompt_bits()
        hook = getattr(self._local, "stream_hook", None)
        if hook is not None and self.brain.enabled:
            pieces: list[str] = []
            try:
                for chunk in self.brain.ask_stream(question, history=history, facts=facts,
                                                   extra_system=extra_system, profile=profile):
                    pieces.append(chunk)
                    try:
                        hook(chunk)
                    except Exception:
                        pass
            except Exception as exc:
                if not pieces:
                    # Nothing arrived, so fall back to the blocking path to get
                    # the full error (and the next provider in the ladder).
                    reply = self.brain.ask(question, history=history, facts=facts,
                                           extra_system=extra_system, profile=profile)
                    reply.meta["stream_failed"] = str(exc)[:200]
                    return reply
            if pieces:
                return Reply(text="".join(pieces), provider=self.brain.provider, ok=True,
                             model=self.settings.model_for(), meta={"streamed": True})
        return self.brain.ask(question, history=history, facts=facts,
                              extra_system=extra_system, profile=profile)

    def _finish(self, response: Response, remember: bool) -> None:
        self._learn_from_response(response)
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
            if self.actions is not None:
                report = self.controller.report() if self.controller else None
                ready = [name for name, cap in (report.capabilities.items() if report else []) if cap.available]
                lines["control"] = (f"{len(self.actions.actions)} actions · "
                                    f"{len(ready)} capabilities ready"
                                    + (" · simulation mode" if getattr(self.controller, "simulate", False)
                                       else ""))
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
