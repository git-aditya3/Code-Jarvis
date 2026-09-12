"""
The part of JARVIS that changes because of *you*.

Everything here is local and derived from your own usage:

``aliases``    — “when I say *chill*, open Spotify and mute” (taught, or learned from a
                 phrase that failed followed by one that worked).
``actions``    — what you actually do, how often, and at which hours.
``approvals``  — how often you say yes to each confirmation, which lets JARVIS stop
                 asking about ordinary things you always approve.
``hours``      — time-of-day patterns, the raw material for ritual suggestions
                 (“every morning around 9 you open Chrome and Slack”).

Writing is **batched**: observations update memory immediately and the file is
flushed at most once every ``save_interval`` seconds, so learning never sits in
the way of answering.
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any

from .config import path_for

#: Hard caps — a profile must stay tiny and fast to load.
MAX_ACTIONS = 80
MAX_PHRASES = 300
MAX_ALIASES = 120
MAX_DAYS = 21
MIN_RITUAL_DAYS = 2


def _hour(ts: float | None = None) -> str:
    return time.strftime("%H", time.localtime(ts if ts is not None else time.time()))


def _day(ts: float | None = None) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(ts if ts is not None else time.time()))


def _trim(mapping: dict[str, Any], limit: int, key: str = "count") -> None:
    if len(mapping) <= limit:
        return
    ordered = sorted(mapping.items(), key=lambda item: item[1].get(key, 0), reverse=True)
    for name, _ in ordered[limit:]:
        mapping.pop(name, None)


def _blank() -> dict[str, Any]:
    return {
        "version": 1,
        "created": time.time(),
        "actions": {},        # name -> {count, ok, failed, hours: {h: n}, last, apps: {target: n}}
        "phrases": {},        # utterance -> {skill, count, ok, last}
        "aliases": {},        # phrase -> {action, args, routine, learned, hits, taught}
        "approvals": {},      # action -> {yes, no, refused}
        "hours": {},          # hour -> {action: {n, days: [...]}}
        "days": {},           # date -> count
        "sessions": {"count": 0, "last": 0.0},
    }


class BehaviourProfile:
    """Learns how the user works and answers questions about them."""

    def __init__(self, path: Any = None, save_interval: float = 5.0,
                 enabled: bool = True) -> None:
        self.path = path or path_for("profile.json")
        self.save_interval = max(1.0, float(save_interval))
        self.enabled = bool(enabled)
        self._lock = threading.RLock()
        self._dirty = False
        self._last_save = 0.0
        self.data = _blank()
        self._load()
        self.data["sessions"]["count"] = int(self.data["sessions"].get("count", 0)) + 1
        self.data["sessions"]["last"] = time.time()
        self._dirty = True

    # ── persistence ──────────────────────────────────────────────────────
    def _load(self) -> None:
        try:
            with open(self.path, encoding="utf-8") as handle:
                stored = json.load(handle)
        except (OSError, ValueError):
            return
        if not isinstance(stored, dict):
            return
        blank = _blank()
        for key, value in stored.items():
            if key in blank and isinstance(value, type(blank[key])):
                blank[key] = value
            elif key in blank:
                blank[key] = value              # tolerate small shape changes
        self.data = blank

    def flush(self, force: bool = False) -> None:
        """Write the profile if enough time has passed (or when forced)."""
        if not self.enabled:
            return
        with self._lock:
            if not self._dirty and not force:
                return
            now = time.time()
            if not force and (now - self._last_save) < self.save_interval:
                return
            payload = json.dumps(self.data, indent=2, ensure_ascii=False)
            self._dirty = False
            self._last_save = now
        try:
            from .config import _write_json  # local import keeps startup light
            _write_json(self.path, json.loads(payload))
        except OSError:
            pass

    def save(self) -> None:
        self.flush(force=True)

    def _touch(self) -> None:
        self._dirty = True
        self.flush()

    # ── observations ─────────────────────────────────────────────────────
    def observe_action(self, action: str, args: dict[str, Any] | None = None, *,
                       ok: bool = True, source: str = "voice", ts: float | None = None) -> None:
        if not self.enabled or not action:
            return
        args = args or {}
        stamp = ts if ts is not None else time.time()
        hour, day = _hour(stamp), _day(stamp)
        with self._lock:
            entry = self.data["actions"].setdefault(action, {"count": 0, "ok": 0, "failed": 0,
                                                            "hours": {}, "last": 0, "apps": {}})
            entry["count"] = int(entry.get("count", 0)) + 1
            entry["ok" if ok else "failed"] = int(entry.get("ok" if ok else "failed", 0)) + 1
            entry["last"] = stamp
            hours = entry.setdefault("hours", {})
            hours[hour] = int(hours.get(hour, 0)) + 1
            target = args.get("target") or args.get("title") or args.get("name")
            if isinstance(target, str) and target:
                apps = entry.setdefault("apps", {})
                apps[target.lower()] = int(apps.get(target.lower(), 0)) + 1
            self.data["days"][day] = int(self.data["days"].get(day, 0)) + 1

            bucket = self.data["hours"].setdefault(hour, {})
            slot = bucket.setdefault(action, {"n": 0, "days": []})
            slot["n"] = int(slot.get("n", 0)) + 1
            if day not in slot["days"]:
                slot["days"].append(day)
                del slot["days"][:-MAX_DAYS]
            if len(self.data["days"]) > MAX_DAYS * 2:
                for old in sorted(self.data["days"])[:-MAX_DAYS]:
                    self.data["days"].pop(old, None)
            _trim(self.data["actions"], MAX_ACTIONS)
        self._touch()

    def observe_utterance(self, text: str, *, skill: str = "", ok: bool = True) -> None:
        """Remember the phrasings that work for this user (and the ones that don't)."""
        if not self.enabled:
            return
        phrase = " ".join((text or "").lower().split())[:120]
        if not phrase:
            return
        with self._lock:
            entry = self.data["phrases"].setdefault(phrase, {"skill": skill, "count": 0, "ok": 0,
                                                             "last": 0})
            entry["count"] = int(entry.get("count", 0)) + 1
            entry["ok"] = int(entry.get("ok", 0)) + (1 if ok else 0)
            entry["skill"] = skill or entry.get("skill", "")
            entry["last"] = time.time()
            _trim(self.data["phrases"], MAX_PHRASES)
        self._touch()

    def count_approval(self, action: str, approved: bool) -> None:
        if not self.enabled or not action:
            return
        with self._lock:
            entry = self.data["approvals"].setdefault(action, {"yes": 0, "no": 0, "refused": 0})
            entry["yes" if approved else "no"] = int(entry.get("yes" if approved else "no", 0)) + 1
        self._touch()

    def count_refusal(self, action: str) -> None:
        if not self.enabled or not action:
            return
        with self._lock:
            entry = self.data["approvals"].setdefault(action, {"yes": 0, "no": 0, "refused": 0})
            entry["refused"] = int(entry.get("refused", 0)) + 1
        self._touch()

    # ── aliases: “when I say X, do Y” ────────────────────────────────────
    def learn_alias(self, phrase: str, *, action: str = "", args: dict[str, Any] | None = None,
                    routine: str = "", taught: bool = False) -> None:
        phrase = " ".join((phrase or "").lower().split())[:120]
        if not self.enabled or not phrase or not (action or routine):
            return
        with self._lock:
            self.data["aliases"][phrase] = {
                "action": action, "args": dict(args or {}), "routine": routine,
                "learned": time.time(), "hits": int(
                    (self.data["aliases"].get(phrase) or {}).get("hits", 0)),
                "taught": bool(taught),
            }
            _trim(self.data["aliases"], MAX_ALIASES, key="hits")
        self._touch()

    def alias(self, text: str) -> dict[str, Any] | None:
        phrase = " ".join((text or "").lower().split())[:120]
        if not phrase:
            return None
        with self._lock:
            found = self.data["aliases"].get(phrase)
            if found is None:
                # tolerate “jarvis, <phrase>” and a trailing “please”
                shortened = phrase.removeprefix("jarvis ").removesuffix(" please").strip()
                found = self.data["aliases"].get(shortened)
            if found is None:
                return None
            found["hits"] = int(found.get("hits", 0)) + 1
            result = dict(found)
        self._touch()
        return result

    def forget_alias(self, phrase: str) -> bool:
        with self._lock:
            removed = self.data["aliases"].pop(" ".join((phrase or "").lower().split()), None)
        if removed is not None:
            self._touch()
        return removed is not None

    def aliases(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return {key: dict(value) for key, value in self.data["aliases"].items()}

    # ── learned trust ────────────────────────────────────────────────────
    def approvals(self, action: str) -> dict[str, int]:
        with self._lock:
            return dict(self.data["approvals"].get(action) or {"yes": 0, "no": 0, "refused": 0})

    def learned_trust(self, action: str, minimum: int = 3) -> bool:
        """True when this action has been approved often and never refused."""
        stats = self.approvals(action)
        return (int(stats.get("yes", 0)) >= minimum and int(stats.get("no", 0)) == 0
                and int(stats.get("refused", 0)) == 0)

    def trust_candidates(self, minimum: int = 3) -> list[str]:
        with self._lock:
            return sorted(name for name in self.data["approvals"]
                          if self.learned_trust(name, minimum))

    # ── patterns worth knowing ───────────────────────────────────────────
    def top_actions(self, limit: int = 8) -> list[tuple[str, int]]:
        with self._lock:
            items = [(name, int(entry.get("count", 0))) for name, entry in self.data["actions"].items()]
        return sorted(items, key=lambda item: item[1], reverse=True)[:limit]

    def top_apps(self, limit: int = 8) -> list[tuple[str, int]]:
        totals: dict[str, int] = {}
        with self._lock:
            for entry in self.data["actions"].values():
                for app, count in (entry.get("apps") or {}).items():
                    totals[app] = totals.get(app, 0) + int(count)
        return sorted(totals.items(), key=lambda item: item[1], reverse=True)[:limit]

    def busiest_hours(self, limit: int = 4) -> list[tuple[int, int]]:
        with self._lock:
            bucket = self.data["hours"]
            totals = {int(hour): sum(int(slot.get("n", 0)) for slot in actions.values())
                      for hour, actions in bucket.items() if hour.isdigit()}
        return sorted(totals.items(), key=lambda item: item[1], reverse=True)[:limit]

    def rituals(self, hour: int | None = None, tolerance: int = 1) -> list[dict[str, Any]]:
        """Actions this user repeats at this time of day, on different days."""
        now_hour = int(_hour()) if hour is None else int(hour)
        # hour keys are zero-padded (“09”), so build the window the same way
        wanted = {f"{(now_hour + offset) % 24:02d}" for offset in range(-tolerance, tolerance + 1)}
        found: list[dict[str, Any]] = []
        with self._lock:
            for hour_key, actions in self.data["hours"].items():
                if hour_key not in wanted:
                    continue
                for action, slot in actions.items():
                    days = slot.get("days") or []
                    if len(days) >= MIN_RITUAL_DAYS:
                        found.append({"hour": int(hour_key), "action": action,
                                      "count": int(slot.get("n", 0)), "days": len(days)})
        found.sort(key=lambda item: (item["days"], item["count"]), reverse=True)
        return found

    def prompt_bits(self, limit: int = 6) -> str:
        """A compact description of the user for the model's system prompt."""
        apps = ", ".join(f"{name} ({count}×)" for name, count in self.top_apps(limit))
        actions = ", ".join(f"{name} ({count}×)" for name, count in self.top_actions(limit))
        hours = ", ".join(f"{hour:02d}:00" for hour, _ in self.busiest_hours(3))
        bits = []
        if apps:
            bits.append(f"Apps they use most: {apps}.")
        if actions:
            bits.append(f"Actions they ask for most: {actions}.")
        if hours:
            bits.append(f"Busiest hours: {hours}.")
        return " ".join(bits)

    def summary(self, limit: int = 6) -> str:
        """Human-readable “what have you learned about me?”."""
        lines: list[str] = []
        apps = self.top_apps(limit)
        if apps:
            lines.append("Apps you reach for most: "
                         + ", ".join(f"{name} ({count}×)" for name, count in apps))
        actions = self.top_actions(limit)
        if actions:
            lines.append("Commands you use most: "
                         + ", ".join(f"{name.replace('_', ' ')} ({count}×)" for name, count in actions))
        hours = self.busiest_hours(3)
        if hours:
            lines.append("Busiest hours: " + ", ".join(f"{hour:02d}:00" for hour, _ in hours))
        rituals = self.rituals()
        if rituals:
            described = ", ".join(f"{item['action'].replace('_', ' ')} around {item['hour']:02d}:00"
                                  for item in rituals[:3])
            lines.append("Things you do at this time of day: " + described)
        trusted = self.trust_candidates()
        if trusted:
            lines.append("I stopped asking about: "
                         + ", ".join(name.replace("_", " ") for name in trusted[:5]))
        learned = [phrase for phrase, info in self.aliases().items() if not info.get("taught")]
        taught = [phrase for phrase, info in self.aliases().items() if info.get("taught")]
        if taught:
            lines.append("Phrases you taught me: " + ", ".join(f"“{p}”" for p in taught[:5]))
        if learned:
            lines.append("Phrases I picked up from you: " + ", ".join(f"“{p}”" for p in learned[:5]))
        if not lines:
            lines.append("Nothing yet — I need a little more time with you.")
        return "\n".join(lines)

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {
                "actions_seen": sum(int(entry.get("count", 0)) for entry in self.data["actions"].values()),
                "distinct_actions": len(self.data["actions"]),
                "phrases": len(self.data["phrases"]),
                "aliases": len(self.data["aliases"]),
                "trusted": len(self.trust_candidates()),
                "days_used": len(self.data["days"]),
                "sessions": int(self.data["sessions"].get("count", 0)),
                "rituals": len(self.rituals()),
            }

    def reset(self) -> None:
        with self._lock:
            self.data = _blank()
            self.data["sessions"]["count"] = 1
            self.data["sessions"]["last"] = time.time()
        self.flush(force=True)
