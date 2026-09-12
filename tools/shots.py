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


def shoot(window: JarvisWindow, index: int, name: str) -> None:
    window.tabs.setCurrentIndex(index)
    app.processEvents()
    window.grab().save(str(DOCS / name))
    print("wrote", DOCS / name)


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

shots = {0: "jarvis-window.png", 1: "jarvis-memory.png", 3: "jarvis-settings.png"}


def capture() -> None:
    for index, name in shots.items():
        shoot(window, index, name)
    dialog = ConfirmDialog("Close Chrome?",
                           "close_window(title=chrome)\n\nrisk: confirm", "confirm", window)
    dialog.setModal(False)
    dialog.show()
    app.processEvents()
    dialog.grab().save(str(DOCS / "jarvis-approval.png"))
    print("wrote", DOCS / "jarvis-approval.png")
    window.close()
    app.quit()


QTimer.singleShot(400, capture)
app.exec()
print("done")
os._exit(0)
