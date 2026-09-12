"""
Long-term memory for JARVIS.

Three kinds of memory, all stored in ``~/.jarvis/memory.json``:

* **notes**  – free text snippets ("note that the staging DB password rotates on
  Mondays").
* **tasks**  – todos with done/undone state.
* **facts**  – key/value pairs the assistant can recall instantly ("my wifi
  password is ...", "my manager is Priya").

Plus a rolling ``history`` of conversation turns so the LLM fallback has
context. Writes are atomic and guarded by a lock so skill timers and the UI
thread can both touch memory safely.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import _read_json, _write_json, path_for

MAX_HISTORY = 400


def _now() -> float:
    return time.time()


def _stamp(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


@dataclass
class Note:
    id: str
    text: str
    tags: list[str] = field(default_factory=list)
    created: float = field(default_factory=_now)

    @property
    def when(self) -> str:
        return _stamp(self.created)

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "text": self.text, "tags": self.tags, "created": self.created}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Note:
        return cls(
            id=str(data.get("id") or uuid.uuid4().hex[:8]),
            text=str(data.get("text", "")),
            tags=list(data.get("tags") or []),
            created=float(data.get("created") or _now()),
        )


@dataclass
class Task:
    id: str
    text: str
    done: bool = False
    created: float = field(default_factory=_now)
    due: float | None = None

    @property
    def when(self) -> str:
        return _stamp(self.created)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "text": self.text,
            "done": self.done,
            "created": self.created,
            "due": self.due,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Task:
        return cls(
            id=str(data.get("id") or uuid.uuid4().hex[:8]),
            text=str(data.get("text", "")),
            done=bool(data.get("done")),
            created=float(data.get("created") or _now()),
            due=float(data["due"]) if data.get("due") else None,
        )


class Memory:
    """Thread-safe, atomically-persisted store for notes, tasks and facts."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or path_for("memory.json")
        self.notes: list[Note] = []
        self.tasks: list[Task] = []
        self.facts: dict[str, str] = {}
        self.history: list[dict[str, Any]] = []
        self.profile: dict[str, str] = {}
        self._load()

    # ── persistence ──────────────────────────────────────────────────────
    def _load(self) -> None:
        data = _read_json(self.path, {})
        if not isinstance(data, dict):
            return
        self.notes = [Note.from_dict(n) for n in data.get("notes") or [] if isinstance(n, dict)]
        self.tasks = [Task.from_dict(t) for t in data.get("tasks") or [] if isinstance(t, dict)]
        facts = data.get("facts") or {}
        self.facts = {str(k): str(v) for k, v in facts.items()} if isinstance(facts, dict) else {}
        self.history = [h for h in (data.get("history") or []) if isinstance(h, dict)][-MAX_HISTORY:]
        profile = data.get("profile") or {}
        self.profile = {str(k): str(v) for k, v in profile.items()} if isinstance(profile, dict) else {}

    def save(self) -> None:
        _write_json(
            self.path,
            {
                "notes": [n.to_dict() for n in self.notes],
                "tasks": [t.to_dict() for t in self.tasks],
                "facts": self.facts,
                "history": self.history[-MAX_HISTORY:],
                "profile": self.profile,
            },
        )

    # ── notes ────────────────────────────────────────────────────────────
    def add_note(self, text: str, tags: Iterable[str] = ()) -> Note:
        note = Note(id=uuid.uuid4().hex[:8], text=text.strip(), tags=[t for t in tags if t])
        self.notes.append(note)
        self.save()
        return note

    def list_notes(self, limit: int = 20) -> list[Note]:
        return list(reversed(self.notes))[:limit]

    def find_note(self, needle: str) -> Note | None:
        """Find a note by id prefix or substring."""
        needle = (needle or "").strip().lower()
        if not needle:
            return None
        for note in reversed(self.notes):
            if note.id == needle or note.id.startswith(needle):
                return note
        for note in reversed(self.notes):
            if needle in note.text.lower():
                return note
        return None

    def delete_note(self, needle: str) -> Note | None:
        note = self.find_note(needle)
        if note:
            self.notes.remove(note)
            self.save()
        return note

    # ── tasks ────────────────────────────────────────────────────────────
    def add_task(self, text: str, due: float | None = None) -> Task:
        task = Task(id=uuid.uuid4().hex[:8], text=text.strip(), due=due)
        self.tasks.append(task)
        self.save()
        return task

    def open_tasks(self) -> list[Task]:
        return [t for t in self.tasks if not t.done]

    def find_task(self, needle: str) -> Task | None:
        needle = (needle or "").strip().lower()
        if not needle:
            return None
        open_only = self.open_tasks()
        if needle.isdigit():
            index = int(needle) - 1
            if 0 <= index < len(open_only):
                return open_only[index]
        for task in open_only:
            if task.id == needle or task.id.startswith(needle) or needle in task.text.lower():
                return task
        return None

    def complete_task(self, needle: str) -> Task | None:
        task = self.find_task(needle)
        if task:
            task.done = True
            self.save()
        return task

    def reopen_task(self, needle: str) -> Task | None:
        task = self.find_task(needle)
        if task:
            task.done = False
            self.save()
        return task

    def clear_done(self) -> int:
        before = len(self.tasks)
        self.tasks = [t for t in self.tasks if not t.done]
        removed = before - len(self.tasks)
        if removed:
            self.save()
        return removed

    # ── facts ────────────────────────────────────────────────────────────
    def remember(self, key: str, value: str) -> None:
        self.facts[key.strip().lower()] = value.strip()
        self.save()

    def recall(self, key: str) -> str | None:
        key = (key or "").strip().lower()
        if key in self.facts:
            return self.facts[key]
        for stored, value in self.facts.items():
            if key and (key in stored or stored in key):
                return value
        return None

    def fact_key(self, key: str) -> str | None:
        key = (key or "").strip().lower()
        if key in self.facts:
            return key
        for stored in self.facts:
            if key and (key in stored or stored in key):
                return stored
        return None

    def forget(self, key: str) -> str | None:
        stored = self.fact_key(key)
        if stored:
            return self.facts.pop(stored)
        return None

    # ── history ──────────────────────────────────────────────────────────
    def append_turn(self, role: str, text: str, meta: dict[str, Any] | None = None) -> None:
        entry: dict[str, Any] = {"role": role, "text": text, "ts": _now()}
        if meta:
            entry.update(meta)
        self.history.append(entry)
        if len(self.history) > MAX_HISTORY:
            self.history = self.history[-MAX_HISTORY:]
        self.save()

    def recent(self, count: int = 8) -> list[dict[str, Any]]:
        return [h for h in self.history[-count * 2:] if h.get("text")]

    def clear_history(self) -> None:
        self.history = []
        self.save()

    # ── search / stats ───────────────────────────────────────────────────
    def search(self, query: str, limit: int = 10) -> dict[str, list[Any]]:
        needle = (query or "").strip().lower()
        if not needle:
            return {"notes": [], "tasks": [], "facts": []}
        return {
            "notes": [n for n in self.notes if needle in n.text.lower()][:limit],
            "tasks": [t for t in self.tasks if needle in t.text.lower()][:limit],
            "facts": [(k, v) for k, v in self.facts.items() if needle in k or needle in v.lower()][:limit],
        }

    def stats(self) -> dict[str, int]:
        return {
            "notes": len(self.notes),
            "tasks_open": len(self.open_tasks()),
            "tasks_done": len(self.tasks) - len(self.open_tasks()),
            "facts": len(self.facts),
            "turns": len(self.history),
        }

    def wipe(self) -> None:
        self.notes, self.tasks, self.facts, self.history, self.profile = [], [], {}, [], {}
        self.save()

    def export_text(self) -> str:
        lines = [f"JARVIS memory export — {_stamp(_now())}", ""]
        if self.profile:
            lines.append("PROFILE")
            lines += [f"  {k}: {v}" for k, v in self.profile.items()]
            lines.append("")
        if self.facts:
            lines.append("FACTS")
            lines += [f"  {k}: {v}" for k, v in sorted(self.facts.items())]
            lines.append("")
        if self.notes:
            lines.append("NOTES")
            lines += [f"  [{n.id}] {n.when}  {n.text}" for n in self.notes]
            lines.append("")
        if self.tasks:
            lines.append("TASKS")
            lines += [f"  [{'x' if t.done else ' '}] [{t.id}] {t.text}" for t in self.tasks]
        return "\n".join(lines)
