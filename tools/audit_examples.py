#!/usr/bin/env python3
"""Audit the example phrase of every action, in dry-run mode.

For each registered action we take its first example phrase — the one the HUD
palette and the README show — and ask JARVIS what that phrase actually does.
Three outcomes are possible:

``action``   the phrase reached its own action (or a documented synonym);
``skill``    the phrase was answered by one of the offline skills instead
             (fine: “read notes.txt” is the notes skill's job);
``fallback`` nothing understood it, which is a gap worth closing.

Run it after touching ``jarvis/planner.py``, ``jarvis/control_skills.py`` or the
example phrases in ``jarvis/actions.py``::

    python tools/audit_examples.py            # report
    python tools/audit_examples.py --strict   # non-zero exit if anything is a gap
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

#: Phrases that are served by an equally good neighbour. “start notepad” opening
#: ``open_app`` is not a defect; it is the alias table working.
SYNONYMS = {
    "start_app": "open_app",
    "nudge_brightness": "set_brightness",
    "switch_window": "switch_window",
    "append_file": "append_file",
}


def build_core(home: str):
    os.environ["JARVIS_HOME"] = home
    from jarvis.config import Settings
    from jarvis.core import Core
    from jarvis.host import HeadlessHost
    from jarvis.memory import Memory

    settings = Settings()
    for key, value in {
        "dry_run": True,               # rehearse: nothing on the machine is touched
        "trust_level": "trusted",
        "allow_dangerous": True,
        "provider": "offline",         # no network in an audit
        "voice_replies": False,
        "learn_habits": False,
        "first_run_done": True,
    }.items():
        settings[key] = value
    return Core(settings, Memory(), HeadlessHost(verbose=False))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--strict", action="store_true", help="exit 1 on any gap")
    parser.add_argument("--all-examples", action="store_true", help="check every example, not just the first")
    args = parser.parse_args()

    workdir = tempfile.mkdtemp(prefix="jarvis-audit-")
    os.chdir(workdir)
    core = build_core(os.path.join(workdir, "home"))

    from jarvis.actions import build_actions
    from jarvis.control import Controller

    spec = build_actions(Controller(core.settings, simulate=True))
    results: list[tuple[str, str, str, str]] = []      # action, phrase, outcome, detail
    gaps: list[tuple[str, str]] = []

    for name, action in sorted(spec.items()):
        phrases = list(action.examples or ())
        if not phrases:
            gaps.append((name, ""))
            continue
        for phrase in (phrases if args.all_examples else phrases[:1]):
            core.actions.recent.clear()
            response = core.ask(phrase)
            ran = [entry["action"] for entry in core.actions.recent]
            if SYNONYMS.get(name, name) in ran or ran:
                results.append((name, phrase, "action", ran[0]))
            elif response.skill not in ("fallback", "", None):
                results.append((name, phrase, "skill", response.skill))
            else:
                results.append((name, phrase, "fallback", ""))
                gaps.append((name, phrase))

    counts = Counter(kind for _n, _p, kind, _d in results)
    width = max(len(name) for name, *_ in results) if results else 8
    print(f"Example-phrase audit — {len(results)} phrases, {len(spec)} actions\n")
    for name, phrase, kind, detail in results:
        mark = {"action": "✓", "skill": "·", "fallback": "✗"}[kind]
        note = "" if kind == "action" else f"  ({kind}: {detail or 'nothing'})"
        print(f"  {mark} {name:<{width}}  “{phrase}”{note}")

    print(f"\nreach their action: {counts['action']}   answered by a skill: {counts['skill']}"
          f"   no route: {counts['fallback']}   without examples: {len([1 for n, p in gaps if not p])}")
    if gaps:
        print("\nGaps")
        for name, phrase in gaps:
            print(f"  ✗ {name:<{width}}  {phrase!r}")
    return 1 if (args.strict and gaps) else 0


if __name__ == "__main__":
    raise SystemExit(main())
