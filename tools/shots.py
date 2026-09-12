"""Regenerate the README screenshots offscreen (no display needed).

Run with:
  PYTHONPATH=. LD_LIBRARY_PATH=/tmp/qtstubs QT_QPA_PLATFORM=offscreen python3 tools/shots.py
"""

from __future__ import annotations

import os
import pathlib
import sys
import tempfile

HOME = pathlib.Path(tempfile.mkdtemp(prefix="jarvis-shots-"))
os.environ["JARVIS_HOME"] = str(HOME)
os.environ["JARVIS_DRY_RUN"] = "1"

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication

from jarvis.config import Settings
from jarvis.memory import Memory
from jarvis.ui.main_window import ConfirmDialog, JarvisWindow

DOCS = pathlib.Path(__file__).resolve().parent.parent / "docs"
DOCS.mkdir(parents=True, exist_ok=True)


app = QApplication(sys.argv)
settings = Settings(path=HOME / "settings.json")
settings["dry_run"] = True
memory = Memory(path=HOME / "memory.json")
memory.remember("wifi password", "hunter2 (example)")
memory.add_note("staging DB rotates on Monday")
memory.add_task("call the bank", due=None)
memory.remember("routine.work session", '{"steps": [{"action": "open_app", "args": {"target": "chrome"}},'
                                        ' {"action": "open_app", "args": {"target": "slack"}}],'
                                        ' "times_run": 4, "source": "recorded", "name": "work session"}')

window = JarvisWindow(settings, memory)
window.resize(1180, 760)
window.show()
window.connect_core()

#: tab name → file, in the order the README shows them
shots = {
    "dashboard": "jarvis-window.png",
    "routines": "jarvis-routines.png",
    "actions": "jarvis-actions.png",
    "learning": "jarvis-learning.png",
    "memory": "jarvis-memory.png",
    "settings": "jarvis-settings.png",
}


def seed_learning() -> None:
    """Give the LEARNING panel something true to show."""
    profile = getattr(getattr(window, "core", None), "profile", None)
    if profile is None:
        return
    for action in ("open_app", "set_volume", "write_file", "read_screen",
                   "media_control", "screenshot"):
        for _ in range(2):
            profile.observe_action(action, {"target": "chrome"} if action == "open_app" else {})
    profile.observe_utterance("open chrome", skill="control")
    profile.observe_utterance("chill", skill="fallback", ok=False)
    profile.learn_alias("chill", action="open_app", args={"target": "spotify"})
    profile.learn_alias("focus mode", action="set_volume", args={"level": 15}, taught=True)
    for _ in range(4):
        profile.count_approval("write_file", True)
    profile.observe_action("zip_path", {"source": "report.md"})
    for _ in range(3):
        profile.observe_action("read_screen")


def capture() -> None:
    seed_learning()
    for tab, name in shots.items():
        window.goto_tab(tab)
        app.processEvents()
        window.grab().save(str(DOCS / name))
        print("wrote", DOCS / name, flush=True)
    dialog = ConfirmDialog("Close Chrome?",
                           "close_window(title=chrome)\n\nrisk: confirm", "confirm", window)
    dialog.setModal(False)
    dialog.show()
    app.processEvents()
    dialog.grab().save(str(DOCS / "jarvis-approval.png"))
    print("wrote", DOCS / "jarvis-approval.png", flush=True)
    window.close()
    app.quit()


QTimer.singleShot(400, capture)
app.exec()
print("done", flush=True)
os._exit(0)
