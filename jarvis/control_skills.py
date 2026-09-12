"""
Voice skills for computer control: the bridge between what you say and what the
:mod:`jarvis.actions` registry does.

Three skills live here.

``control``     — *“open chrome”*, *“set the volume to 20”*, *“press ctrl+s”*,
                  *“read my screen”*, *“run git status”*: one sentence, one action.
``routine``     — *“watch what I do”*, *“save that as work session”*, *“run my
                  work session”*, *“what routines do I have”*: the memory layer.
``plan``        — *“open chrome, then set the volume to 20, then read my screen”*:
                  several steps, shown for approval when the plan is long.

All three read the live controller and action registry out of ``ctx.state``
(placed there by :class:`jarvis.core.Core`), so they work identically from the
window, the CLI and the tests.
"""

from __future__ import annotations

import re
import time
from typing import Any

from .actions import CONFIRM, DANGEROUS, TRUST_LABELS
from .planner import LARGE_STEP_THRESHOLD, Planner, split_steps
from .routines import RoutineResult, RoutineStore
from .skills import Skill, SkillContext, SkillResult

#: Words that mean “the next sentence is a new step”.
MULTI_STEP_HINT = re.compile(r"\s(?:and\s+then|then|after\s+that|followed\s+by)\s", re.IGNORECASE)

RECORD_START = re.compile(
    r"^(?:start\s+)?(?:recording|learning|watching)(?:\s+my\s+(?:steps|actions))?$|"
    r"^(?:watch|see)\s+what\s+i\s+do$|^learn\s+(?:a\s+)?(?:routine|this)$|^teach\s+me$",
    re.IGNORECASE)

RECORD_STOP = re.compile(r"^(?:stop|finish|end)\s+(?:recording|learning|watching)$", re.IGNORECASE)
RECORD_CANCEL = re.compile(r"^(?:cancel|forget|discard)\s+(?:the\s+)?(?:recording|routine|steps|that)$",
                           re.IGNORECASE)

SAVE_ROUTINE = re.compile(
    r"^(?:save|store|keep|remember)\s+(?:that|this|it|these\s+steps|what\s+i\s+did)"
    r"(?:\s+as|\s+called|\s+named)?\s*(?P<name>.*)$", re.IGNORECASE)
CALL_IT = re.compile(r"^(?:call|name)\s+(?:that|this|it)\s+(?P<name>.+)$", re.IGNORECASE)
SAVE_AS = re.compile(r"^save\s+(?:this|that)\s+as\s+(?:a\s+)?(?:routine\s+)?(?P<name>.*)$", re.IGNORECASE)

RUN_ROUTINE = re.compile(
    r"^(?:run|do|start|begin|execute|launch|perform)\s+(?:my\s+|the\s+)?"
    r"(?P<name>.+?)(?:\s+routine|\s+macro)?(?:\s+again)?$", re.IGNORECASE)
DO_MY_THING = re.compile(r"^(?:do|run)\s+my\s+thing$", re.IGNORECASE)

LIST_ROUTINES = re.compile(
    r"^(?:list|show|what|which)\s+(?:are\s+)?(?:my\s+)?routines$|"
    r"^what\s+routines\s+(?:do\s+i\s+have|are\s+saved)$|^my\s+routines$", re.IGNORECASE)

DELETE_ROUTINE = re.compile(r"^(?:delete|remove|forget)\s+(?:the\s+|my\s+)?routine\s+(?P<name>.+)$",
                            re.IGNORECASE)
SUGGESTIONS = re.compile(
    r"^(?:any|what)\s+(?:new\s+)?(?:routine\s+)?suggestions?$|"
    r"^what\s+have\s+you\s+noticed$|^learn\s+my\s+habits$", re.IGNORECASE)

UNDO_LAST = re.compile(r"^(?:undo|remove|drop)\s+(?:the\s+)?last\s+(?:step|action)$", re.IGNORECASE)

ACTIONS_HELP = re.compile(r"^(?:what\s+can\s+you\s+(?:control|do\s+on\s+my\s+computer)|list\s+actions|"
                          r"actions|control\s+help)$", re.IGNORECASE)

AUDIT_QUERY = re.compile(r"^(?:what\s+(?:did|have)\s+you\s+(?:just\s+)?do(?:ne)?|show\s+(?:the\s+)?(?:action\s+|audit\s+)?log|"
                         r"action\s+history|recent\s+actions)$", re.IGNORECASE)

CONFIRM_WORDS = re.compile(r"^(?:yes|yeah|yep|ok|okay|sure|do it|go ahead|confirm|please do|yes please)$",
                           re.IGNORECASE)
DENY_WORDS = re.compile(r"^(?:no|nope|stop|cancel|don'?t|do not|never ?mind|abort|no thanks)$",
                        re.IGNORECASE)


class _ControlBase(Skill):
    """Shared plumbing for the control skills."""

    # ── access to the live wiring ────────────────────────────────────────
    @staticmethod
    def registry(ctx: SkillContext):
        return ctx.state.get("control")

    @staticmethod
    def store(ctx: SkillContext) -> RoutineStore | None:
        return ctx.state.get("routine_store")

    @staticmethod
    def timeline(ctx: SkillContext):
        return ctx.state.get("routine_timeline")

    @staticmethod
    def planner(ctx: SkillContext) -> Planner | None:
        return ctx.state.get("planner")

    def unavailable(self) -> SkillResult:
        return SkillResult(
            text="Computer control is switched off, or no backend is available on this system.",
            speak="Computer control is not available.",
            ok=False,
        )

    @staticmethod
    def _finish(result: Any, registry: Any = None, speak: str = "") -> SkillResult:
        message = getattr(result, "message", "") or "Done."
        if not getattr(result, "ok", False):
            hint = getattr(result, "hint", "") or ""
            text = message + (f"\n\n{hint}" if hint else "")
            return SkillResult(text=text, speak=message.splitlines()[0][:200], ok=False,
                               error=message)
        return SkillResult(text=message, speak=speak or message.splitlines()[0][:200],
                           data={"simulated": bool(getattr(result, "simulated", False))})


# ════════════════════════════════════════════════════════════════════════════
#  control — one sentence, one action
# ════════════════════════════════════════════════════════════════════════════

class ControlSkill(_ControlBase):
    name = "control"
    title = "Computer control"
    description = "open apps, type, press keys, click, run commands, manage files and windows"
    examples = ("open chrome", "set the volume to 20", "press ctrl+s", "read my screen",
                "run git status")
    priority = 20
    patterns = (
        # Weights sit just above the older offline skills (media/power/apps/
        # clipboard/screenshot) so the *action* layer owns these phrases: it is the
        # one that applies the safety policy, writes the audit log and feeds
        # routine memory. Everything the parser cannot handle still falls through
        # to the original skills.
        (r"^(?:please\s+)?(?:open|launch|start|focus|switch to|activate)\s+\S", 0.96),
        # “run git status” belongs to the project skill (0.95, repository aware);
        # anything else typed after “run” is an explicit request for the shell, so
        # it scores just above the environment skill (0.92) and the launcher
        # (0.85) — and it is the version that audits and confirms.
        (r"^(?:run|execute)\s+\S", 0.92),
        (r"^(?:please\s+)?(?:close|quit|minimi[sz]e|maximi[sz]e)\s+\S", 0.96),
        # “write down …” is a note for the memory skill, not typing into a window.
        (r"^(?:type|write)\s+(?!down\b|a\s+note\b|about\b|a\s+reminder\b)\S", 0.96),
        (r"^(?:press|hit|send|key|shortcut|hotkey)\b", 0.96),
        (r"^(?:click|double click|right click|scroll|move (?:the )?(?:mouse|pointer|cursor))\b", 0.96),
        (r"^(?:set\s+)?(?:the\s+)?(?:volume|brightness|screen brightness)\b", 0.96),
        (r"^(?:volume|sound|brightness)\s+(?:up|down|louder|quieter|higher|lower)\b", 0.96),
        (r"^(?:mute|unmute|toggle mute|dim|darken|brighten)\b", 0.96),
        (r"^(?:lock|sleep|suspend|shut\s?down|restart|reboot|log\s?out|sign\s?out|power\s?off)\b", 0.97),
        (r"^(?:kill|stop|force quit|terminate|end)\s+\S+\s*(?:process|app|application)?$", 0.96),
        (r"^(?:what(?:'s| is)? (?:running|open|on my screen|in focus))\b", 0.92),
        (r"^(?:list|show)\s+(?:me\s+)?(?:the\s+)?(?:windows?|processes|open windows)", 0.90),
        (r"^(?:create|make)\s+(?:a\s+)?(?:new\s+)?folder\b", 0.96),
        (r"^(?:copy|move|rename|delete|remove|trash|zip|compress|archive)\s+\S+\s+(?:to|into|as)\s+",
         0.96),
        (r"^(?:delete|remove|trash)\s+[~/.]", 0.96),
        (r"^(?:take\s+a\s+)?screenshot$|^capture\s+(?:the\s+)?screen$|"
         r"^show\s+me\s+(?:a\s+)?screenshot$", 0.98),
        (r"^(?:read|scan|ocr)\s+(?:my\s+|the\s+)?screen$", 0.98),
        (r"^what(?:'s| is) on (?:my|the) screen$", 0.96),
        (r"^(?:what(?:'s| is) (?:on|in)\s+my\s+clipboard|read my clipboard)$", 0.96),
        (r"^copy\s+.+\s+to\s+(?:my\s+|the\s+)?clipboard$", 0.96),
        (r"^(?:run|execute)\s+(?:the\s+)?(?:command|shell|terminal)\b", 0.97),
        (r"^in the terminal\b", 0.97),
        (r"^(?:notify me|say|speak)\b", 0.90),
        (r"^what can you (?:control|do on my computer)$", 0.97),
        (r"^(?:list actions|actions|control help)$", 0.97),
        (r"^(?:what (?:did|have) you (?:just )?do(?:ne)?|show (?:the )?(?:action |audit )?log|"
         r"action history|recent actions)$", 0.96),
        (r"^wait(?:\s+(?:for\s+)?\d{1,3}\s*(?:seconds|secs|s|minutes|mins)?)?$", 0.80),
        # ── media keys ───────────────────────────────────────────────────
        (r"^(?:next|skip|previous|last)\s+(?:song|track|music|video)$", 0.96),
        (r"^(?:play|pause|resume)\s+(?:the\s+)?(?:music|song|video|playback|media)$", 0.96),
        (r"^(?:play\s*/?\s*pause|pause|resume)$", 0.94),
        (r"^stop\s+(?:the\s+)?(?:music|song|video|playback|media)$", 0.96),
        # ── window placement & workspaces ────────────────────────────────
        (r"^(?:snap|push|pin|unpin|keep|put|take)\s+\S+\s+(?:to\s+|on\s+|off\s+)?(?:the\s+)?(?:left|right|top)\b",
         0.95),
        (r"^(?:make|put|set)\s+.+\s+full\s?screen$|^full\s?screen\b", 0.95),
        (r"^show\s+(?:me\s+)?(?:the\s+)?desktop$|^minimi[sz]e\s+all\s+windows$", 0.96),
        (r"^(?:go\s+to|switch\s+to|show)\s+(?:my\s+|the\s+)?(?:virtual\s+)?(?:workspace|desktop|space)\s*\d",
         0.96),
        (r"^move\s+\S+\s+to\s+\d+\s*,\s*\d+", 0.95),
        # ── mouse: coordinates and dragging ──────────────────────────────
        (r"^click\s+(?:at\s+)?\d+\s*[, ]\s*\d+$", 0.96),
        (r"^drag\s+(?:from\s+)?\d+\s*[, ]\s*\d+\s+to\s+", 0.96),
        (r"^click\s+(?:on\s+)?(?:the\s+)?[^\d\s]", 0.93),
        # ── screen reading & OCR ─────────────────────────────────────────
        (r"^(?:where(?:'s| is)|find|locate)\s+.+\s+on\s+(?:my\s+|the\s+)?screen$", 0.96),
        (r"^copy\s+(?:what(?:'s| is)\s+on\s+)?(?:my\s+|the\s+)?screen", 0.96),
        (r"^(?:what(?:'s| is)\s+)?(?:my\s+|the\s+)?(?:screen|display)\s+(?:size|resolution)$", 0.95),
        # ── system switches ──────────────────────────────────────────────
        (r"^(?:turn|switch|enable|disable|toggle)\s+(?:on\s+|off\s+)?(?:the\s+)?(?:wi-?fi|wireless|"
         r"bluetooth|night\s?light|dark\s?mode|do\s+not\s+disturb|dnd|power\s+sav)", 0.97),
        (r"^(?:wi-?fi|bluetooth|dark\s?mode|night\s?light)\s+(?:on|off)$", 0.96),
        (r"^power\s+sav(?:er|ing)(?:\s+mode)?$", 0.96),
        (r"^(?:make\s+)?(?:the\s+)?(?:screen|display)?\s*(?:brighter|dimmer)|^brightness\s+(?:up|down)",
         0.96),
        # ── files, folders, disk ─────────────────────────────────────────
        (r"^(?:duplicate|unzip|extract|unpack)\s+\S", 0.96),
        (r"^(?:find|locate|search\s+for)\s+(?:all\s+)?(?:files?|[\w*.?~-]+)\b", 0.90),
        (r"^(?:how\s+big\s+is|size\s+of|info\s+(?:on|about)|details\s+of)\s+\S", 0.94),
        (r"^(?:how\s+much\s+)?(?:disk|drive|storage)\s+space", 0.95),
        (r"^empty\s+(?:the\s+|my\s+)?(?:trash|recycle\s+bin|bin|wastebasket)$", 0.96),
        (r"^(?:add|append)\s+.+\s+to\s+(?:~/|/[\w.]|[\w-]+\.\w{1,6}$)", 0.90),
        (r"^read\s+[~/\w][\w./\\-]*\.\w{1,6}\b", 0.93),
        # ── processes, apps, terminal, clipboard history ─────────────────
        (r"^is\s+\S+\s+(?:running|open|up)$", 0.94),
        (r"^(?:what|which)\s+(?:apps|applications|programs)\s+(?:are\s+)?(?:open|running)$", 0.95),
        (r"^(?:open|start|launch)\s+(?:a\s+|the\s+)?(?:terminal|shell|console|command\s+prompt)$", 0.97),
        (r"^(?:what\s+did\s+i\s+copy(?:\s+earlier)?|clipboard\s+history|"
         r"show\s+(?:me\s+)?(?:my\s+)?clipboard\s+history)$", 0.96),
        (r"^(?:put\s+back|restore)\s+(?:what\s+i\s+copied|my\s+clipboard|the\s+clipboard)$", 0.95),
        (r"^(?:what|which)\s+shortcuts?\s+(?:do\s+you|are\s+there|can\s+i)", 0.96),
    )

    def match(self, text: str) -> float:
        score = super().match(text)
        if score and MULTI_STEP_HINT.search(text):
            return 0.0        # the plan skill handles “a, then b, then c”
        return score

    def run(self, text: str, ctx: SkillContext) -> SkillResult:
        registry = self.registry(ctx)
        if registry is None:
            return self.unavailable()

        if ACTIONS_HELP.match(text.strip()):
            caps = ctx.state.get("control_report") or ""
            return SkillResult(text=registry.catalogue() + ("\n\n" + caps if caps else ""),
                               speak="I can control apps, windows, keys, the mouse, files and power.")

        if AUDIT_QUERY.match(text.strip()):
            return self._audit(registry)

        planner = self.planner(ctx)
        parsed = planner.parse(text) if planner else None
        if not parsed:
            return SkillResult(
                text="I understood the intent but not the details. Try naming the app or the value, "
                     "for example “open chrome” or “set the volume to 20”.",
                speak="I did not catch the details of that one.",
                ok=False,
                error="unparsed",
            )
        action, args = parsed
        result = registry.execute(action, args, source="voice")
        response = self._finish(result)
        response.data["action"] = action
        return response

    @staticmethod
    def _audit(registry: Any) -> SkillResult:
        entries = registry.audit.tail(20)
        if not entries:
            return SkillResult(text="I have not done anything on this machine yet.",
                               speak="Nothing in the action log yet.")
        lines = ["Recent actions (newest last):"]
        for entry in entries[-12:]:
            mark = {"done": "✓", "failed": "✗", "refused": "⛔", "declined": "✗"}.get(
                str(entry.get("outcome")), "·")
            when = str(entry.get("when", ""))[11:16]
            args = ", ".join(f"{key}={value}" for key, value in list((entry.get("args") or {}).items())[:2])
            lines.append(f"  {mark} {when} {entry.get('action')}({args}) — "
                         f"{str(entry.get('message', ''))[:70]}")
        lines.append(f"\nFull log: {registry.audit.path}")
        return SkillResult(text="\n".join(lines), speak=f"{len(entries)} actions in my log.")


# ════════════════════════════════════════════════════════════════════════════
#  routine — record, save from memory, replay
# ════════════════════════════════════════════════════════════════════════════

class RoutineSkill(_ControlBase):
    name = "routine"
    title = "Routines"
    description = "record your steps, save them to memory, and run them again by name"
    examples = ("watch what I do", "save that as work session", "run my work session",
                "what routines do I have")
    priority = 30          # beats the control skill for “run my work session”
    patterns = (
        (r"^watch what i do$", 1.0),
        (r"^(?:start|begin)\s+(?:recording|learning)\b", 1.0),
        (r"^(?:stop|finish|end)\s+(?:recording|learning)\b", 1.0),
        (r"^(?:cancel|discard)\s+(?:the\s+)?(?:recording|steps)\b", 0.95),
        (r"^(?:save|store|keep)\s+(?:that|this|it|these steps|what i did)\b", 1.0),
        (r"^call\s+(?:that|this|it)\s+\S+", 0.98),
        (r"^save\s+(?:this|that)\s+as\s+", 0.98),
        (r"^(?:list|show)\s+(?:my\s+)?routines$", 1.0),
        (r"^what\s+routines\s+(?:do i have|are saved)$", 1.0),
        (r"^my routines$", 1.0),
        (r"^(?:delete|remove|forget)\s+(?:the\s+|my\s+)?routine\s+\S+", 0.98),
        (r"^(?:any|what)\s+(?:new\s+)?(?:routine\s+)?suggestions?$", 1.0),
        (r"^what have you noticed$", 1.0),
        (r"^(?:undo|drop)\s+(?:the\s+)?last\s+(?:step|action)$", 1.0),
        (r"^(?:run|do|start)\s+(?:my|the)?\s*\S*\s*routine\b", 0.90),
        (r"^do my thing$", 1.0),
    )

    def run(self, text: str, ctx: SkillContext) -> SkillResult:
        registry = self.registry(ctx)
        store = self.store(ctx)
        if registry is None or store is None:
            return self.unavailable()
        cleaned = text.strip()
        recorder = getattr(registry, "recorder", None)

        # ── recording ────────────────────────────────────────────────────
        if RECORD_START.match(cleaned):
            if recorder is None:
                return SkillResult(text="Recording is unavailable right now.", ok=False)
            if recorder.active:
                return SkillResult(text=recorder.transcript(registry),
                                   speak="Already recording.")
            recorder.start()
            return SkillResult(
                text="Recording. Do the steps you want — by voice (“open chrome”, “set the volume "
                     "to 20”) or by typing in the box for anything I cannot see. Say “stop recording” "
                     "when you are done, then “save that as <name>”.\n\n"
                     "While I am recording, even dry-run steps are captured: they are the steps you "
                     "mean to teach me, so nothing you demonstrate is lost.",
                speak="Recording. Show me the steps, then tell me to save them.",
            )

        if RECORD_STOP.match(cleaned):
            if recorder is None or not recorder.active:
                return SkillResult(text="I am not recording anything right now.", ok=False)
            recorder.stop()
            steps = recorder.transcript(registry)
            return SkillResult(
                text=steps + "\n\nSay “save that as <name>” to keep it, or “cancel recording” to "
                             "throw it away.",
                speak=f"Stopped. I captured {recorder.count} steps.",
            )

        if RECORD_CANCEL.match(cleaned):
            if recorder is None:
                return SkillResult(text="Nothing to cancel.", ok=False)
            had = recorder.count if recorder.active else 0
            recorder.cancel()
            if had:
                return SkillResult(text=f"Discarded {had} recorded steps.", speak="Threw that away.")
            routine = store.delete(cleaned.replace("routine", "").strip() or "routine")
            return SkillResult(text="Cancelled." if not routine else f"Forgot {routine.name}.",
                               speak="Cancelled.")

        if UNDO_LAST.match(cleaned):
            if recorder is None or not recorder.active:
                return SkillResult(text="I am not recording, so there is no last step to drop.",
                                   ok=False)
            if recorder.undo_last():
                return SkillResult(text="Dropped the last recorded step.\n\n"
                                        + recorder.transcript(registry), speak="Dropped it.")
            return SkillResult(text="There is nothing recorded yet.", ok=False)

        # ── saving ───────────────────────────────────────────────────────
        save_match = SAVE_ROUTINE.match(cleaned) or SAVE_AS.match(cleaned) or CALL_IT.match(cleaned)
        if save_match:
            return self._save(save_match.group("name"), ctx, registry, store)

        # ── listing and suggestions ──────────────────────────────────────
        if SUGGESTIONS.match(cleaned):
            return self.show_suggestions(ctx, registry, store, limit=3)

        if LIST_ROUTINES.match(cleaned):
            return self.list_routines(ctx, registry, store)

        delete_match = DELETE_ROUTINE.match(cleaned)
        if delete_match:
            routine = store.delete(delete_match.group("name"))
            if not routine:
                return SkillResult(text=f"I have no routine called “{delete_match.group('name')}”.",
                                   ok=False)
            ctx.host.notify("Routine removed", routine.name)
            return SkillResult(text=f"Deleted the routine **{routine.name}**.",
                               speak=f"Deleted {routine.name}.")

        # ── running ──────────────────────────────────────────────────────
        if DO_MY_THING.match(cleaned):
            only = store.all()
            if len(only) == 1:
                return self.run_routine(only[0], ctx, registry, store)
            return self.list_routines(ctx, registry, store)

        name = re.sub(r"^(?:run|do|start|begin|execute|launch|perform)\s+", "", cleaned,
                      flags=re.IGNORECASE)
        name = re.sub(r"^(?:my|the)\s+", "", name, flags=re.IGNORECASE)
        name = re.sub(r"\s+(?:routine|macro)$", "", name, flags=re.IGNORECASE)
        name = re.sub(r"\s+again$", "", name, flags=re.IGNORECASE).strip()
        routine = store.get(name) if name else None
        if routine is None:
            return self.list_routines(ctx, registry, store,
                                      note=f"I have no routine called “{name}”." if name else "")
        return self.run_routine(routine, ctx, registry, store)

    # ── helpers ──────────────────────────────────────────────────────────
    def _save(self, raw_name: str, ctx: SkillContext, registry: Any, store: RoutineStore) -> SkillResult:
        recorder = getattr(registry, "recorder", None)
        name = (raw_name or "").strip().strip("“”\"'").rstrip(".!")
        if recorder is None or not recorder.steps:
            return SkillResult(
                text="I have nothing recorded to save. Say “watch what I do”, perform the steps, "
                     "then “save that as <name>”.",
                speak="There is nothing recorded yet.",
                ok=False,
            )
        if not name:
            return SkillResult(text="What should I call it? Say “save that as work session”.",
                               speak="What should I name it?", ok=False)
        if name.lower() in {"that", "this", "it"}:
            name = "routine"
        existing = store.get(name)
        if existing:
            name = f"{name} {time.strftime('%d%b').lower()}"
        routine = recorder.to_routine(name, description=f"Recorded from {recorder.count} steps")
        store.save(routine)
        recorder.cancel()
        ctx.host.refresh_memory()
        risk = routine.risk()
        warning = ""
        if risk in (CONFIRM, DANGEROUS):
            warning = (f"\n\nIt contains {(risk == 'dangerous' and 'destructive') or 'state-changing'} "
                       "steps, so JARVIS will still ask before those parts run.")
        return SkillResult(
            text=f"Saved **{routine.name}** to memory ({len(routine.steps)} steps)."
                 f"{warning}\n\n{routine.outline(registry)}\n\n"
                 f"Run it any time with “run my {routine.name}”.",
            speak=f"Saved {routine.name}. Say run my {routine.name} whenever you want it.",
            data={"routine": routine.name},
        )

    def run_routine(self, routine: Any, ctx: SkillContext, registry: Any,
                     store: RoutineStore) -> SkillResult:
        runner = ctx.state.get("routine_runner")
        if runner is None:
            from .routines import RoutineRunner

            runner = RoutineRunner(registry, self.timeline(ctx), store)
        long_routine = len(routine.steps) >= LARGE_STEP_THRESHOLD
        if long_routine and not self._approved_long(ctx, routine, registry):
            return SkillResult(text=f"Not run — you did not approve the {len(routine.steps)}-step "
                                    f"routine **{routine.name}**.",
                               speak="Cancelled.", ok=False)
        ctx.host.log(f"Running routine: {routine.name}", level="info")
        result: RoutineResult = runner.run(routine, source="routine")
        return SkillResult(
            text=result.report(registry),
            speak=result.spoken,
            ok=result.ok,
            data={"routine": routine.name, "steps": len(result.outcomes),
                  "failed": not result.ok},
        )

    def _approved_long(self, ctx: SkillContext, routine: Any, registry: Any) -> bool:
        confirmer = getattr(ctx.host, "confirm", None)
        if not callable(confirmer):
            return False
        detail = (f"This routine has {len(routine.steps)} steps "
                  f"({routine.risk()} risk):\n\n{routine.outline(registry)}")
        return bool(confirmer(f"Run “{routine.name}”?", detail, routine.risk()))

    def list_routines(self, ctx: SkillContext, registry: Any, store: RoutineStore,
                      note: str = "") -> SkillResult:
        routines = store.all()
        if not routines:
            return SkillResult(
                text=(note + "\n\n" if note else "")
                + "I do not have any routines yet.\n\n"
                  "Two ways to make one:\n"
                  "• “watch what I do” → perform the steps → “save that as work session”\n"
                  "• Use something twice and I will offer to save it for you.",
                speak="No routines yet. Say watch what I do to teach me one.",
            )
        lines = [note, "Routines in memory:"] if note else ["Routines in memory:"]
        for routine in routines:
            lines.append(f"• {routine.summary(registry)}")
        lines.append("\nRun one with “run my <name>”, or ask for a walkthrough: "
                     "“what is in my <name> routine”.")
        return SkillResult(text="\n".join(line for line in lines if line is not None),
                           speak=f"You have {len(routines)} routines saved.")

    def show_suggestions(self, ctx: SkillContext, registry: Any, store: RoutineStore,
                         limit: int = 2) -> SkillResult:
        suggestions = ctx.state.get("routine_suggestions") or []
        if not suggestions:
            from .routines import suggestions as find

            timeline = self.timeline(ctx)
            suggestions = find(timeline, store, min_count=2) if timeline else []
        if not suggestions:
            return SkillResult(
                text="No patterns yet. Once you repeat the same few actions a couple of times, "
                     "I will offer to save them as a routine.",
                speak="Nothing to suggest yet.",
            )
        lines = ["Things you keep doing — want any of these saved as routines?"]
        for index, suggestion in enumerate(suggestions[:limit], start=1):
            lines.append(f"\n{index}. {suggestion.describe(registry)}")
        lines.append("\nSay “save that as <name>” straight after running it, and I will keep it.")
        return SkillResult(text="\n".join(lines),
                           speak=f"I noticed {len(suggestions)} habits worth saving.")


# ════════════════════════════════════════════════════════════════════════════
#  plan — several steps from one sentence
# ════════════════════════════════════════════════════════════════════════════

class PlanSkill(_ControlBase):
    name = "plan"
    title = "Multi-step tasks"
    description = "do several things from one request, in order"
    examples = ("open chrome, then set the volume to 20 and then read my screen",
                "open my editor then start a timer for 25 minutes")
    priority = 40
    patterns = (
        (r"\s(?:and\s+then|then|after\s+that|followed\s+by)\s", 0.92),
        (r"^(?:step|first)\s*1\s*[.:)]", 0.95),
        (r"\s*;\s*\S+", 0.70),
        (r"\s+\|\s+", 0.70),
    )

    def run(self, text: str, ctx: SkillContext) -> SkillResult:
        registry = self.registry(ctx)
        planner = self.planner(ctx)
        if registry is None or planner is None:
            return self.unavailable()
        plan = planner.plan(text)
        if not plan.steps:
            return SkillResult(text="I could not turn that into steps. Try “open chrome, then set "
                                    "the volume to 20”.", ok=False, speak="I could not break that down.")
        if plan.leftovers:
            listing = "\n".join(f"  ? {item}" for item in plan.leftovers)
            return SkillResult(
                text=f"I understand {len(plan.steps)} of these steps, but not:\n{listing}\n\n"
                     "Run the plan as it stands, or rephrase the missing part?",
                speak="Part of that did not make sense to me.",
                ok=False,
            )
        outline = plan.outline(registry)
        needs_approval = plan.large or any(
            (registry.get(step.action) and registry.get(step.action).risk in (CONFIRM, DANGEROUS))
            for step in plan.steps
        )
        approved = False
        if needs_approval:
            confirmer = getattr(ctx.host, "confirm", None)
            approved = bool(confirmer and confirmer(
                f"Run this {plan.size}-step plan?", outline + "\n\nYou can also say “save this as "
                "<name>” to keep it as a routine instead of running it now.", "confirm"))
            if not approved:
                return SkillResult(
                    text="Plan dropped — nothing was changed.\n\n" + outline
                         + "\n\nTip: say “save this as <name>” first if you want it stored as a "
                           "routine.",
                    speak="Cancelled.",
                    ok=False,
                    error="declined",
                )
        result = planner.run(plan, source="plan", approved=approved)
        return SkillResult(text=result.report(registry), speak=result.spoken, ok=result.ok,
                           data={"plan": plan.name, "steps": plan.size})


# ════════════════════════════════════════════════════════════════════════════
#  habit suggestions without being asked
# ════════════════════════════════════════════════════════════════════════════

def maybe_suggest_routine(ctx: SkillContext, threshold: int | None = None) -> str | None:
    """Return an offer-to-save message when a habit has just repeated enough times.

    Called by the core after every *successful* action, but deliberately quiet:
    each suggestion is offered once, and never while a recording is in progress.
    """
    registry = ctx.state.get("control")
    store = ctx.state.get("routine_store")
    timeline = ctx.state.get("routine_timeline")
    if registry is None or store is None or timeline is None:
        return None
    if not ctx.settings.get("routine_watch", True):
        return None
    recorder = getattr(registry, "recorder", None)
    if recorder is not None and getattr(recorder, "active", False):
        return None
    limit = int(threshold if threshold is not None else ctx.settings.get("routine_suggest_after", 2) or 2)
    offered: set[str] = ctx.state.setdefault("routine_offered", set())  # type: ignore[arg-type]
    from .routines import suggestions as find

    for suggestion in find(timeline, store, min_count=limit, max_suggestions=3):
        if suggestion.key in offered or store.get(suggestion.suggested_name):
            continue
        offered.add(suggestion.key)
        return suggestion.describe(registry)
    return None


def default_control_skills() -> list[Skill]:
    """The three skills the core registers when control is enabled."""
    return [PlanSkill(), RoutineSkill(), ControlSkill()]


def control_help_lines() -> list[str]:
    """Short lines for the CLI/UI “what can you control?” answer."""
    return [
        f"• {skill.title} — {skill.description}" for skill in default_control_skills()
    ] + ["• Trust levels: " + " · ".join(f"{key} = {value}" for key, value in TRUST_LABELS.items()),
         f"• Plans of {LARGE_STEP_THRESHOLD}+ steps are always shown to you first."]


__all__ = [
    "ControlSkill",
    "PlanSkill",
    "RoutineSkill",
    "control_help_lines",
    "default_control_skills",
    "maybe_suggest_routine",
    "split_steps",
]
