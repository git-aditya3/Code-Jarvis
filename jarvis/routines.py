"""
Routines: JARVIS learns what you do, then offers to do it for you.

Three things live here.

**The timeline** (:class:`Timeline`) — every action JARVIS performs is appended to
``~/.jarvis/routines.json``. Nothing else reads this file, and it never leaves
your machine.

**The recorder** (:class:`RoutineRecorder`) — say *"watch what I do"*, perform the
steps once (by voice or by hand — you can add clicks and typing to the same
routine), then say *"save that as work session"*. The steps are written into
memory as ``routine.work session``.

**The pattern spotter** (:func:`suggestions`) — the interesting part. If you run
the same thing again and again, JARVIS notices: *"You've asked me to open Spotify
and set the volume to 30 four times this week. Save that as a routine?"* Accept,
and it becomes a named routine you can run with one sentence forever after.

Routines are stored as ordinary memory facts (``routine.<name>`` holding a JSON
body), so they appear in the Memory tab, travel with ``memory.json`` exports, and
can be edited or deleted like any other fact.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .actions import RISK_ORDER, Action, ActionContext, ActionRegistry
from .config import path_for

ROUTINE_PREFIX = "routine."
#: Actions that only *look* at things; a routine made of them is boring, so they
#: never appear in suggestions on their own.
PASSIVE_ACTIONS = frozenset({
    "list_windows", "active_window", "pointer_position", "list_processes",
    "read_screen", "list_dir", "get_volume", "get_brightness", "clipboard_read",
    "notify", "speak", "wait", "ask_user", "remember", "screenshot",
})


# ════════════════════════════════════════════════════════════════════════════
#  Steps and routines
# ════════════════════════════════════════════════════════════════════════════

@dataclass
class Step:
    """One action invocation inside a routine."""

    action: str
    args: dict[str, Any] = field(default_factory=dict)
    note: str = ""
    optional: bool = False
    times: int = 1

    def to_dict(self) -> dict[str, Any]:
        body: dict[str, Any] = {"action": self.action, "args": self.args}
        if self.note:
            body["note"] = self.note
        if self.optional:
            body["optional"] = True
        if self.times != 1:
            body["times"] = self.times
        return body

    @staticmethod
    def from_dict(data: Any) -> Step:
        if isinstance(data, str):
            return Step(action=data.strip())
        if not isinstance(data, dict):
            return Step(action=str(data))
        args = data.get("args") if isinstance(data.get("args"), dict) else {}
        return Step(
            action=str(data.get("action", "")).strip(),
            args=args,
            note=str(data.get("note", "")),
            optional=bool(data.get("optional")),
            times=max(1, int(data.get("times", 1) or 1)),
        )

    def describe(self, registry: ActionRegistry | None = None) -> str:
        action = registry.get(self.action) if registry else None
        title = action.title if action else self.action
        shown = ", ".join(f"{key}={_short(value)}" for key, value in self.args.items()
                          if not key.startswith("_"))
        label = f"{title} ({shown})" if shown else title
        if self.times > 1:
            label += f" ×{self.times}"
        if self.optional:
            label += " [optional]"
        if self.note:
            label += f"  — {self.note}"
        return label

    #: Two steps are the same if they do the same thing to the same target.
    #: Values are normalised to text so “volume 30” and “volume "30"” (what comes
    #: back out of the saved timeline) still count as the same habit.
    @property
    def signature(self) -> str:
        return f"{self.action}:{json.dumps(normalise_args(self.args), sort_keys=True)}"


@dataclass
class Routine:
    """A named, re-runnable sequence of steps."""

    name: str
    steps: list[Step] = field(default_factory=list)
    description: str = ""
    source: str = "taught"            # recorded | taught | planned | builtin | learned
    created: float = field(default_factory=time.time)
    times_run: int = 0
    last_run: float = 0.0
    last_result: str = ""
    triggers: list[str] = field(default_factory=list)
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])

    @property
    def key(self) -> str:
        return ROUTINE_PREFIX + self.name.strip().lower()

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "source": self.source,
            "created": self.created,
            "times_run": self.times_run,
            "last_run": self.last_run,
            "last_result": self.last_result,
            "triggers": self.triggers,
            "steps": [step.to_dict() for step in self.steps],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)

    @staticmethod
    def from_dict(data: dict[str, Any]) -> Routine:
        routine = Routine(
            name=str(data.get("name", "routine")).strip() or "routine",
            description=str(data.get("description", "")),
            source=str(data.get("source", "taught")),
            created=float(data.get("created", time.time()) or time.time()),
            times_run=int(data.get("times_run", 0) or 0),
            last_run=float(data.get("last_run", 0) or 0),
            last_result=str(data.get("last_result", "")),
            triggers=[str(item) for item in (data.get("triggers") or [])],
        )
        if data.get("id"):
            routine.id = str(data["id"])
        routine.steps = [Step.from_dict(item) for item in (data.get("steps") or [])]
        return routine

    @staticmethod
    def from_json(text: str) -> Routine | None:
        try:
            data = json.loads(text)
        except (ValueError, TypeError):
            return None
        return Routine.from_dict(data) if isinstance(data, dict) else None

    def summary(self, registry: ActionRegistry | None = None) -> str:
        when = ""
        if self.last_run:
            when = " · last run " + time.strftime("%d %b %H:%M", time.localtime(self.last_run))
        runs = f" · run {self.times_run}×" if self.times_run else ""
        return (f"**{self.name}** — {len(self.steps)} steps{self._origin()}{runs}{when}"
                + (f"\n   {self.description}" if self.description else ""))

    def _origin(self) -> str:
        return {
            "recorded": " (recorded from your steps)",
            "learned": " (learned from your habits)",
            "planned": " (from a plan)",
            "builtin": " (built in)",
        }.get(self.source, "")

    def outline(self, registry: ActionRegistry | None = None, limit: int = 12) -> str:
        lines = [f"{index}. {step.describe(registry)}"
                 for index, step in enumerate(self.steps[:limit], start=1)]
        if len(self.steps) > limit:
            lines.append(f"   … and {len(self.steps) - limit} more")
        return "\n".join(lines)

    def risk(self) -> str:
        rank = 0
        for step in self.steps:
            rank = max(rank, RISK_ORDER.get(_step_risk(step), 0))
        return {0: "safe", 1: "confirm", 2: "dangerous", 3: "blocked"}[rank]


def _step_risk(step: Step) -> str:
    """Conservative risk label for a recorded step.

    The action layer always has the last word — this only decides how a routine is
    described in the UI (and whether it looks scary before you run it).
    """
    from .actions import CONFIRM, DANGEROUS, SAFE, Policy

    if step.action in ("delete_path", "kill_process", "power"):
        return DANGEROUS
    if step.action == "run_command":
        return Policy.check_command(str(step.args.get("command", "")))[0]
    if step.action in ("type_text", "press_keys", "close_window", "write_file",
                       "open_app", "start_app", "copy_path", "move_path", "zip_path"):
        return CONFIRM
    return SAFE


def normalise_args(args: dict[str, Any] | None) -> dict[str, Any]:
    """Make argument values comparable: drop private keys, numbers become text."""
    clean: dict[str, Any] = {}
    for key, value in (args or {}).items():
        if str(key).startswith("_"):
            continue
        if isinstance(value, bool):
            clean[str(key)] = value
        elif isinstance(value, (int, float)):
            clean[str(key)] = str(value)
        elif isinstance(value, (list, tuple)):
            clean[str(key)] = [str(item) for item in value]
        elif isinstance(value, dict):
            clean[str(key)] = normalise_args(value)
        else:
            clean[str(key)] = str(value).strip()
    return clean


def _short(value: Any, limit: int = 40) -> str:
    text = str(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


# ════════════════════════════════════════════════════════════════════════════
#  Storage
# ════════════════════════════════════════════════════════════════════════════

class RoutineStore:
    """Routines live in memory as ``routine.<name>`` facts."""

    def __init__(self, memory: Any) -> None:
        self.memory = memory

    # ── reading ──────────────────────────────────────────────────────────
    def all(self) -> list[Routine]:
        found: list[Routine] = []
        for key, value in list(self.memory.facts.items()):
            if not key.startswith(ROUTINE_PREFIX):
                continue
            routine = Routine.from_json(value)
            if routine:
                if not routine.name:
                    routine.name = key[len(ROUTINE_PREFIX):]
                found.append(routine)
        return sorted(found, key=lambda item: (item.name.lower()))

    def names(self) -> list[str]:
        return [routine.name for routine in self.all()]

    def get(self, name: str) -> Routine | None:
        needle = (name or "").strip().lower()
        needle = re.sub(r"^(my|the|routine|macro)\s+", "", needle)
        needle = re.sub(r"\s+(routine|macro|again)$", "", needle)
        if not needle:
            return None
        routines = self.all()
        for routine in routines:                       # exact
            if routine.name.lower() == needle:
                return routine
        for routine in routines:                       # contained either way
            low = routine.name.lower()
            if needle in low or low in needle:
                return routine
        for routine in routines:                       # same words, any order
            if set(needle.split()) == set(routine.name.lower().split()):
                return routine
        for routine in routines:                       # individual words
            words = needle.split()
            if words and all(word in routine.name.lower() for word in words):
                return routine
        return None

    def exists(self, name: str) -> bool:
        return self.get(name) is not None

    # ── writing ──────────────────────────────────────────────────────────
    def save(self, routine: Routine) -> Routine:
        routine.name = routine.name.strip()[:60] or "routine"
        self.memory.remember(routine.key, routine.to_json())
        return routine

    def delete(self, name: str) -> Routine | None:
        routine = self.get(name)
        if routine:
            self.memory.forget(routine.key)
        return routine

    def rename(self, old: str, new: str) -> Routine | None:
        routine = self.get(old)
        if not routine:
            return None
        self.memory.forget(routine.key)
        routine.name = new
        return self.save(routine)


# ════════════════════════════════════════════════════════════════════════════
#  Timeline: what JARVIS did, and when
# ════════════════════════════════════════════════════════════════════════════

class Timeline:
    """An append-only log of action calls, used for pattern detection."""

    def __init__(self, path: Path | None = None, limit: int = 200) -> None:
        self.path = path or path_for("routines.json")
        self.limit = limit
        self._entries: list[dict[str, Any]] = []
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self._entries = []
            return
        if isinstance(data, dict):
            entries = data.get("entries")
            if isinstance(entries, list):
                self._entries = [item for item in entries if isinstance(item, dict)]
                return
        self._entries = entries if isinstance(data, list) else []  # type: ignore[assignment]

    def save(self) -> None:
        payload = {"version": 1, "updated": time.time(), "entries": self._entries[-self.limit:]}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass

    # ── recording ────────────────────────────────────────────────────────
    def add(self, action: str, args: dict[str, Any] | None = None, source: str = "voice",
            ok: bool = True, message: str = "", routine: str = "") -> None:
        clean = {key: value for key, value in (args or {}).items() if not key.startswith("_")}
        # The signature uses the values exactly as they were passed (so “volume 30”
        # matches itself later); the stored copy is shortened for easy reading.
        signature = Step(action=action, args=clean).signature
        self._entries.append({
            "ts": time.time(),
            "action": action,
            "args": {key: _short(value, 80) for key, value in clean.items()},
            "signature": signature,
            "source": source,
            "ok": bool(ok),
            "message": _short(message, 120),
            "routine": routine,
        })
        del self._entries[:-self.limit]
        self.save()

    def entries(self, limit: int | None = None, source: str | None = None,
                since: float | None = None) -> list[dict[str, Any]]:
        rows = self._entries
        if source:
            rows = [row for row in rows if row.get("source") == source]
        if since:
            rows = [row for row in rows if float(row.get("ts", 0)) >= since]
        return rows[-limit:] if limit else list(rows)

    def steps(self, limit: int = 40, source: str | None = "voice") -> list[Step]:
        out: list[Step] = []
        for row in self.entries(limit=limit, source=source):
            if not row.get("ok"):
                continue
            out.append(Step(action=str(row.get("action", "")), args=dict(row.get("args") or {})))
        return out

    def collapse(self, steps: list[Step]) -> list[Step]:
        """Merge runs of identical steps (``scroll`` ten times → ``scroll ×10``)."""
        merged: list[Step] = []
        for step in steps:
            if merged and merged[-1].action == step.action and merged[-1].args == step.args:
                merged[-1].times += 1
            else:
                merged.append(Step(step.action, dict(step.args), step.note, step.optional))
        return merged

    def clear(self) -> None:
        self._entries = []
        self.save()


# ════════════════════════════════════════════════════════════════════════════
#  Recorder: "watch what I do" … "save that as work session"
# ════════════════════════════════════════════════════════════════════════════

class RoutineRecorder:
    """Collects steps while you demonstrate them."""

    MIN_STEPS_TO_SAVE = 2

    def __init__(self, timeline: Timeline, label: str = "") -> None:
        self.timeline = timeline
        self.label = label
        self.active = False
        self.steps: list[Step] = []
        self.started = 0.0

    def start(self, label: str = "") -> str:
        self.label = label.strip()
        self.steps = []
        self.active = True
        self.started = time.time()
        return f"Recording — {len(self.steps)} steps so far."

    def stop(self) -> None:
        self.active = False

    def cancel(self) -> None:
        self.active = False
        self.steps = []

    def capture(self, action: Action | str, args: dict[str, Any] | None = None,
                result: Any = None) -> None:
        name = action.name if isinstance(action, Action) else str(action)
        if not self.active:
            return
        if result is not None and not getattr(result, "ok", True):
            return
        args = {key: value for key, value in (args or {}).items() if not key.startswith("_")}
        if not args and name in PASSIVE_ACTIONS:
            return                     # ignore pure lookups while recording
        step = Step(action=name, args=args)
        if self.steps and self.steps[-1].signature == step.signature:
            self.steps[-1].times += 1
        else:
            self.steps.append(step)

    # ── saving ───────────────────────────────────────────────────────────
    @property
    def count(self) -> int:
        return sum(step.times for step in self.steps)

    def transcript(self, registry: ActionRegistry | None = None) -> str:
        if not self.steps:
            return "Nothing recorded yet."
        lines = [f"Recorded {self.count} steps:"]
        lines += [f"  {index}. {step.describe(registry)}"
                  for index, step in enumerate(self.steps, start=1)]
        return "\n".join(lines)

    def to_routine(self, name: str, description: str = "", source: str = "recorded") -> Routine:
        routine = Routine(
            name=name.strip()[:60] or "recorded routine",
            steps=[Step(step.action, dict(step.args), step.note, step.optional, step.times)
                   for step in self.steps],
            description=description or f"Recorded on {time.strftime('%d %b %Y %H:%M')}",
            source=source,
            triggers=[],
        )
        return routine

    def undo_last(self) -> bool:
        """Drop the most recent step — useful when a demonstration goes wrong."""
        if not self.steps:
            return False
        self.steps.pop()
        return True


# ════════════════════════════════════════════════════════════════════════════
#  Pattern spotting: "you keep doing this — want a routine?"
# ════════════════════════════════════════════════════════════════════════════

@dataclass
class Suggestion:
    """A routine JARVIS thinks you would like, with the evidence for it."""

    key: str
    steps: list[Step]
    count: int
    suggested_name: str
    reason: str

    def describe(self, registry: ActionRegistry | None = None, limit: int = 5) -> str:
        lines = [f"**{self.suggested_name}** — seen {self.count}× ({self.reason})"]
        lines += [f"   • {step.describe(registry)}" for step in self.steps[:limit]]
        if len(self.steps) > limit:
            lines.append(f"   • … and {len(self.steps) - limit} more")
        return "\n".join(lines)


def _name_for(steps: list[Step]) -> str:
    """Guess a friendly name from the steps.

    “open spotify + set volume 30 + notify” → ``spotify + volume routine``: the
    first meaningful word of each step, numbers and filler dropped.
    """
    keys = ("target", "title", "app", "command", "path", "query", "action",
            "text", "key", "level", "amount", "url")
    ignore = {"jarvis", "the", "a", "my", "me", "it", "routine", "this", "that"}
    labels: list[str] = []
    for step in steps[:3]:
        for key in keys:
            value = str(step.args.get(key) or "").strip()
            if not value:
                continue
            words = [word for word in re.sub(r"[^a-zA-Z0-9]+", " ", value).lower().split()
                     if not word.isdigit() and word not in ignore]
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


def _already_saved(gram: tuple[str, ...], routines: list[Routine]) -> bool:
    """True when a saved routine already contains this exact sequence of steps."""
    for routine in routines:
        signatures = [step.signature for step in routine.steps]
        if not signatures:
            continue
        if list(gram) == signatures:
            return True
        # Same steps, different starting point — a habit you already saved, just
        # noticed from another offset in the cycle (avoids nagging twice).
        if len(gram) == len(signatures) and set(gram) == set(signatures):
            return True
        # a one-step suggestion is pointless if any saved routine already uses it
        if len(gram) == 1 and gram[0] in signatures:
            return True
        if len(gram) == 1 and signatures[0] == gram[0]:
            return True
        if len(gram) > 1 and len(signatures) >= len(gram):
            for start in range(len(signatures) - len(gram) + 1):
                if tuple(signatures[start:start + len(gram)]) == gram:
                    return True
    return False


def suggestions(timeline: Timeline, store: RoutineStore, min_count: int = 2,
                window: int = 60, max_suggestions: int = 4) -> list[Suggestion]:
    """Find repeated habits worth turning into routines.

    Two shapes are detected:

    * a *sequence* of two to four actions repeated together
      (“open Spotify, set the volume to 30, notify me”),
    * a single *setup* action repeated on its own
      (“switch to VS Code” five times).

    Anything already saved as a routine is skipped, so JARVIS never nags twice.
    """
    rows = [row for row in timeline.entries(limit=window, source="voice") if row.get("ok")]
    signatures = [str(row.get("signature", "")) for row in rows]
    saved_routines = store.all()
    saved = {routine.name.lower() for routine in saved_routines}
    found: list[Suggestion] = []
    consumed: set[int] = set()
    covered: set[str] = set()      # steps already explained by a suggestion or a routine

    # ── sequences of 2-4 steps that always appear together ───────────────
    for size in (3, 2):
        counts: Counter[tuple[str, ...]] = Counter()
        positions: dict[tuple[str, ...], list[int]] = {}
        for start in range(len(signatures) - size + 1):
            window_slice = tuple(signatures[start:start + size])
            if len(set(window_slice)) < size:
                continue
            if all(signature.split(":")[0] in PASSIVE_ACTIONS for signature in window_slice):
                continue                     # “list windows twice” is not a routine
            if any(start + offset in consumed for offset in range(size)):
                continue
            counts[window_slice] += 1
            positions.setdefault(window_slice, []).append(start)
        for gram, count in counts.most_common():
            if count < min_count or len(found) >= max_suggestions:
                continue
            if set(gram) <= covered:
                continue                 # same habit seen from a rotated offset
            if _already_saved(gram, saved_routines):
                covered.update(gram)
                continue
            start = positions[gram][-1]
            steps = [Step(action=str(rows[start + offset]["action"]),
                          args=dict(rows[start + offset].get("args") or {}))
                     for offset in range(size)]
            suggested = _name_for(steps)
            if suggested.lower() in saved:
                continue
            found.append(Suggestion(
                key="|".join(gram), steps=steps, count=count, suggested_name=suggested,
                reason=f"you ran this exact sequence {count} times",
            ))
            for index in positions[gram]:
                consumed.update(range(index, index + size))
            covered.update(gram)
            break        # one suggestion per size keeps the list readable

    # ── one action you keep repeating on its own ─────────────────────────
    if len(found) < max_suggestions:
        singles: Counter[str] = Counter()
        for index, row in enumerate(rows):
            if index in consumed:
                continue
            action = str(row.get("action", ""))
            if action in PASSIVE_ACTIONS:
                continue
            singles[str(row.get("signature", ""))] += 1
        for signature, count in singles.most_common(20):
            if count < max(min_count, 3) or len(found) >= max_suggestions:
                break
            if _already_saved((signature,), saved_routines):
                continue
            row = next((item for item in reversed(rows)
                        if str(item.get("signature", "")) == signature), None)
            if row is None:
                continue
            step = Step(action=str(row["action"]), args=dict(row.get("args") or {}))
            suggested = _name_for([step])
            if suggested.lower() in saved:
                continue
            found.append(Suggestion(
                key=signature, steps=[step], count=count, suggested_name=suggested,
                reason=f"a single step you repeated {count} times",
            ))
    return found


# ════════════════════════════════════════════════════════════════════════════
#  Runner
# ════════════════════════════════════════════════════════════════════════════

@dataclass
class StepOutcome:
    step: Step
    result: Any

    @property
    def ok(self) -> bool:
        return bool(getattr(self.result, "ok", False))


@dataclass
class RoutineResult:
    routine: Routine
    outcomes: list[StepOutcome] = field(default_factory=list)
    skipped: int = 0
    stopped_early: bool = False

    @property
    def ok(self) -> bool:
        """True when nothing required failed and the routine was not cut short.

        An explicitly optional step is allowed to fail: it is reported, marked
        with ✗, and the rest of the routine carries on.
        """
        if self.stopped_early:
            return False
        return all(outcome.ok or outcome.step.optional for outcome in self.outcomes)

    @property
    def done(self) -> int:
        return sum(1 for outcome in self.outcomes if outcome.ok)

    def report(self, registry: ActionRegistry | None = None) -> str:
        head = f"Ran **{self.routine.name}** — {self.done}/{len(self.outcomes)} steps done"
        if self.ok:
            head = f"Done: **{self.routine.name}** — {self.done} steps completed."
        lines = [head]
        for index, outcome in enumerate(self.outcomes, start=1):
            mark = "✓" if outcome.ok else "✗"
            detail = getattr(outcome.result, "message", "") or ""
            detail = detail.splitlines()[0][:120]
            lines.append(f"  {mark} {index}. {outcome.step.describe(registry)}"
                         + (f"\n       {detail}" if detail else ""))
        if self.stopped_early:
            lines.append("  (stopped at the first failure — ask me to continue if you want to)")
        if self.skipped:
            lines.append(f"  ({self.skipped} optional step(s) did not run)")
        return "\n".join(lines)

    @property
    def spoken(self) -> str:
        if self.ok:
            return f"Routine {self.routine.name} finished, {self.done} steps done."
        if self.stopped_early:
            failed = next((outcome for outcome in self.outcomes if not outcome.ok), None)
            where = failed.step.describe() if failed else "a step"
            return f"Routine {self.routine.name} stopped at {where}."
        return f"Routine {self.routine.name} finished with some problems."


class RoutineRunner:
    """Executes routines through the action registry (so policy still applies)."""

    def __init__(self, registry: ActionRegistry, timeline: Timeline | None = None,
                 store: RoutineStore | None = None) -> None:
        self.registry = registry
        self.timeline = timeline
        self.store = store

    def run(self, routine: Routine, source: str = "routine", approved_plan: bool = False,
            stop_on_failure: bool = True, ctx: ActionContext | None = None) -> RoutineResult:
        result = RoutineResult(routine=routine)
        context = ctx or self.registry.context(approved_plan=approved_plan)
        routine_source = f"routine:{routine.name}"
        for step in routine.steps:
            if not self.registry.get(step.action):
                from .control import ActionResult

                result.outcomes.append(StepOutcome(
                    step, ActionResult.fail(f"I no longer have an action called '{step.action}'."),
                ))
                if not step.optional:
                    if stop_on_failure:
                        result.stopped_early = True
                        break
                    continue
                result.skipped += 1
                continue
            step_ok = True
            for _ in range(max(1, step.times)):
                outcome = self.registry.execute(step.action, step.args, source=routine_source,
                                                approved_plan=approved_plan, ctx=context)
                result.outcomes.append(StepOutcome(step, outcome))
                if self.timeline is not None:
                    self.timeline.add(step.action, step.args, source=f"routine:{routine.name}",
                                      ok=outcome.ok, message=getattr(outcome, "message", "")[:120],
                                      routine=routine.name)
                if not outcome.ok:
                    step_ok = False
                    break
            if not step_ok:
                if not step.optional:
                    if stop_on_failure:
                        result.stopped_early = True
                        break
                    continue
                result.skipped += 1

        routine.times_run += 1
        routine.last_run = time.time()
        routine.last_result = "ok" if result.ok else ("stopped" if result.stopped_early else "partial")
        if self.store is not None:
            self.store.save(routine)
        return result


# ════════════════════════════════════════════════════════════════════════════
#  Plain-language routines ("when I say X, do Y")
# ════════════════════════════════════════════════════════════════════════════

def routine_from_utterances(name: str, phrases: list[str], planner: Any,
                            description: str = "") -> tuple[Routine | None, list[str]]:
    """Build a routine from spoken steps, e.g. ["open chrome", "set volume to 20"]."""
    steps: list[Step] = []
    problems: list[str] = []
    for phrase in phrases:
        parsed = planner.parse(phrase) if hasattr(planner, "parse") else None
        if not parsed:
            problems.append(f"I don't know how to “{phrase}”")
            continue
        action, args = parsed
        steps.append(Step(action=action, args=args))
    if not steps:
        return None, problems
    return Routine(name=name, steps=steps, description=description, source="taught",
                   triggers=list(phrases)), problems
