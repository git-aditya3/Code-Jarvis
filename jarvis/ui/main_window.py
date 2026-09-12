"""
The JARVIS desktop window.

Layout
──────
┌───────────────────────────────────────────────────────────────┐
│ ◈ JARVIS                    [status pill]      pin  –  ×      │
├───────────────┬───────────────────────────────────────────────┤
│               │  Conversation │ Skills │ Memory │ Settings    │
│   arc HUD     │                                               │
│   state text  │   transcript                                  │
│   mic button  │                                               │
│   quick chips │   [ ask JARVIS … ]                       ➤    │
├───────────────┴───────────────────────────────────────────────┤
│ brain status · voice status · memory counts                   │
└───────────────────────────────────────────────────────────────┘

The window is frameless (custom title bar) and implements
:class:`jarvis.host.Host`, so every skill and the core can drive the UI
without importing Qt themselves.
"""

from __future__ import annotations

import html
import json
import re
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from PyQt6.QtCore import QEvent, QObject, QPoint, Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtGui import (
    QAction,
    QColor,
    QFont,
    QGuiApplication,
    QIcon,
    QKeySequence,
    QPainter,
    QPixmap,
    QShortcut,
)
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizeGrip,
    QSlider,
    QSplitter,
    QTabWidget,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from ..actions import TRUST_LABELS
from ..config import (
    PROVIDER_LABELS,
    PROVIDER_MODELS,
    VERSION,
    Settings,
    jarvis_home,
)
from ..control import control_install_hints
from ..core import Core, Response
from ..host import open_with_os
from ..memory import Memory
from ..voice import SpeechToText, TextToSpeech, WakeWordListener
from ..voice import probe as probe_voice
from .hud import HudWidget
from .theme import accent_colors, stylesheet

# ════════════════════════════════════════════════════════════════════════════
#  Thread ↔ GUI marshalling
# ════════════════════════════════════════════════════════════════════════════

class MainThreadInvoker(QObject):
    """Run callables on the GUI thread from worker threads (optionally blocking).

    Qt widgets and the clipboard may only be touched from the GUI thread while
    the core and skills run in worker threads, so every host method funnels
    through here.
    """

    request = pyqtSignal(int)

    def __init__(self) -> None:
        super().__init__()
        self._lock = threading.Lock()
        self._jobs: dict[int, dict[str, Any]] = {}
        self._counter = 0
        self.request.connect(self._run, Qt.ConnectionType.QueuedConnection)

    def _run(self, token: int) -> None:
        with self._lock:
            job = self._jobs.get(token)
        if job is None:
            return
        try:
            job["result"] = job["fn"](*job["args"])
        except Exception as exc:  # never let a UI callback kill the event loop
            job["error"] = exc
        finally:
            if job["event"] is not None:
                job["event"].set()          # the caller picks the result up itself
            else:
                with self._lock:            # fire-and-forget jobs must not pile up
                    self._jobs.pop(token, None)

    def call(self, fn: Callable[..., Any], *args: Any, blocking: bool = False, timeout: float = 10.0) -> Any:
        app = QApplication.instance()
        if app is None or QThread.currentThread() is app.thread():
            return fn(*args)
        event = threading.Event() if blocking else None
        with self._lock:
            self._counter += 1
            token = self._counter
            self._jobs[token] = {"fn": fn, "args": args, "event": event, "result": None, "error": None}
        self.request.emit(token)
        if not blocking:
            return None
        arrived = event.wait(timeout)
        with self._lock:
            job = self._jobs.pop(token, None)
        if job is None or not arrived:
            return None                      # timed out: the answer never came
        if job["error"] is not None:
            raise job["error"]
        return job["result"]


# ════════════════════════════════════════════════════════════════════════════
#  Small widgets
# ════════════════════════════════════════════════════════════════════════════

class TitleBar(QWidget):
    """Draggable title bar with window controls."""

    def __init__(self, window: JarvisWindow) -> None:
        super().__init__(window)
        self.setObjectName("TitleBar")
        self.window_ref = window
        self._drag_offset: QPoint | None = None

        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 10, 10, 6)
        layout.setSpacing(8)

        mark = QLabel("◈")
        mark.setStyleSheet("font-size: 16px;")
        layout.addWidget(mark)

        titles = QVBoxLayout()
        titles.setSpacing(0)
        self.title = QLabel("J A R V I S")
        self.title.setObjectName("TitleText")
        self.subtitle = QLabel(VERSION)
        self.subtitle.setObjectName("TitleSub")
        titles.addWidget(self.title)
        titles.addWidget(self.subtitle)
        layout.addLayout(titles)
        layout.addSpacing(12)

        self.status_pill = QLabel("OFFLINE SKILLS")
        self.status_pill.setObjectName("PanelAlt")
        self.status_pill.setStyleSheet("padding: 4px 10px; border-radius: 9px; font-size: 10px; letter-spacing: 1px;")
        layout.addWidget(self.status_pill)
        layout.addStretch(1)

        self.pin = QPushButton("▲")
        self.pin.setObjectName("Ghost")
        self.pin.setCheckable(True)
        self.pin.setToolTip("Keep JARVIS on top")
        self.pin.setChecked(True)
        self.pin.clicked.connect(lambda: window.set_always_on_top(self.pin.isChecked()))
        layout.addWidget(self.pin)

        minimise = QPushButton("—")
        minimise.setObjectName("Ghost")
        minimise.setToolTip("Minimise")
        minimise.clicked.connect(window.showMinimized)
        layout.addWidget(minimise)

        close = QPushButton("✕")
        close.setObjectName("Ghost")
        close.setToolTip("Hide to tray (Ctrl+Q quits)")
        close.clicked.connect(window.hide)
        layout.addWidget(close)

    # dragging
    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_offset = event.globalPosition().toPoint() - self.window_ref.frameGeometry().topLeft()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._drag_offset is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.window_ref.move(event.globalPosition().toPoint() - self._drag_offset)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        self._drag_offset = None
        super().mouseReleaseEvent(event)


class Toast(QFrame):
    """In-window notification bubble (reminders, errors, confirmations)."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setObjectName("PanelAlt")
        self.setVisible(False)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        self.label = QLabel("")
        self.label.setWordWrap(True)
        layout.addWidget(self.label)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.hide)

    def show_message(self, title: str, text: str, level: str = "info", seconds: int = 7,
                     accent: str = "cyan") -> None:
        colours = accent_colors(accent)
        border = {"info": colours["accent"], "alarm": "#fbbf24", "error": "#f87171"}.get(level, colours["accent"])
        icon = {"info": "◈", "alarm": "!", "error": "!"}.get(level, "◈")
        self.label.setText(f"<b>{icon} {html.escape(title)}</b><br>{html.escape(text)}")
        self.setStyleSheet(f"border: 1px solid {border}; border-radius: 10px; padding: 4px;")
        self.adjustSize()
        self.place()
        self.setVisible(True)
        self.raise_()
        self._timer.start(seconds * 1000)

    def place(self) -> None:
        parent = self.parentWidget()
        if not parent:
            return
        self.adjustSize()
        x = max(12, parent.width() - self.width() - 22)
        y = max(12, parent.height() - self.height() - 46)
        self.move(x, y)


def render_message(text: str, role: str = "jarvis", accent: str = "cyan",
                   detail: str = "") -> str:
    """Turn a plain-text answer into compact HTML for the transcript."""
    colours = accent_colors(accent)
    body = html.escape(text or "")
    # fenced code blocks -> <pre>
    blocks: list[str] = []

    def stash(match: re.Match[str]) -> str:
        blocks.append(match.group(1))
        return f"\x00{len(blocks) - 1}\x00"

    body = re.sub(r"```[a-zA-Z0-9_+-]*\n?(.*?)```", stash, body, flags=re.DOTALL)
    body = re.sub(r"`([^`\n]+)`", r"<span style='color:#fcd34d;'>\1</span>", body)
    body = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", body)
    body = re.sub(r"(https?://[^\s<]+)", r"<a href='\1' style='color:#67e8f9;'>\1</a>", body)
    body = body.replace("\n", "<br>")
    for index, code in enumerate(blocks):
        # `body` was escaped before the blocks were lifted out, so the block
        # content is already HTML-safe — escaping again would show entities.
        body = body.replace(
            f"\x00{index}\x00",
            "<pre style='background:#070c17;border:1px solid #1b2740;border-radius:8px;"
            "padding:8px;color:#cbd5e1;font-family:Consolas,monospace;font-size:12px;"
            "white-space:pre-wrap;'>" + code.strip() + "</pre>",
        )
    if role == "user":
        label, colour = "YOU", "#93c5fd"
    elif role == "system":
        label, colour = "SYSTEM", "#7d90b3"
    elif role == "error":
        label, colour = "ERROR", "#f87171"
    else:
        label, colour = "JARVIS", colours["accent"]
    header = (
        f"<div style='color:{colour};font-size:10px;letter-spacing:2px;margin:10px 0 2px 0;'>"
        f"{label}{(' · ' + html.escape(detail)) if detail else ''}</div>"
    )
    return header + f"<div style='margin:0 0 8px 0;'>{body}</div>"


# ════════════════════════════════════════════════════════════════════════════
#  Approval dialog for computer-control actions
# ════════════════════════════════════════════════════════════════════════════

#: How each risk tier is presented. The wording matters as much as the colour —
#: “destructive” has to look different from “this changes something”.
RISK_STYLE = {
    "safe": ("JARVIS wants to do something", "#39d0ff", "Do it"),
    "confirm": ("JARVIS needs your approval", "#ffb347", "Do it"),
    "dangerous": ("Careful — this cannot be undone", "#ff5f6d", "Yes, I understand"),
}


class ConfirmDialog(QDialog):
    """Ask the user to approve one action before JARVIS performs it.

    It shows exactly what will run, in the same words the audit log will use, so an
    approval is never a guess. Dangerous actions get a red frame and a button that
    spells out the consequence, and “No” is the default button in every case.
    """

    def __init__(self, title: str, detail: str, risk: str = "confirm",
                 parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("ConfirmDialog")          # so it gets the app's dark skin too
        heading, colour, accept_label = RISK_STYLE.get(risk, RISK_STYLE["confirm"])
        self.setWindowTitle("JARVIS — approval needed")
        self.setModal(True)
        self.setMinimumWidth(460)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 14)
        layout.setSpacing(10)

        badge = QLabel(heading.upper())
        badge.setStyleSheet(f"color: {colour}; font-weight: 600; letter-spacing: 1px;")
        layout.addWidget(badge)

        headline = QLabel(title)
        headline.setWordWrap(True)
        headline.setStyleSheet("font-size: 15px; font-weight: 600;")
        layout.addWidget(headline)

        body = QTextBrowser()
        body.setPlainText(detail)
        body.setStyleSheet(
            f"border: 1px solid {colour}; border-radius: 6px; "
            "background: rgba(255,255,255,0.03); padding: 6px;"
        )
        body.setMinimumHeight(90)
        body.setMaximumHeight(280)
        layout.addWidget(body)

        hint = QLabel("Choose “No” and nothing happens. Every decision is written to the audit log.")
        hint.setObjectName("Hint")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self.trust_check = QCheckBox("Stop asking me about ordinary (non-destructive) actions")
        self.trust_check.setVisible(risk == "confirm")
        layout.addWidget(self.trust_check)

        buttons = QDialogButtonBox()
        approve = buttons.addButton(accept_label, QDialogButtonBox.ButtonRole.AcceptRole)
        text_colour = "#06121b" if risk != "dangerous" else "#ffffff"
        approve.setStyleSheet(f"background: {colour}; color: {text_colour}; font-weight: 700; "
                              "padding: 6px 14px; border-radius: 6px;")
        decline = buttons.addButton("No", QDialogButtonBox.ButtonRole.RejectRole)
        decline.setDefault(True)          # the safe choice is the default
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    @property
    def trust_requested(self) -> bool:
        return bool(self.trust_check.isVisible() and self.trust_check.isChecked())


# ════════════════════════════════════════════════════════════════════════════
#  The assistant window
# ════════════════════════════════════════════════════════════════════════════

class JarvisWindow(QWidget):
    """Main application window; also the app's :class:`~jarvis.host.Host`."""

    ui_log = pyqtSignal(str, str)
    ui_toast = pyqtSignal(str, str, str)
    ui_memory_changed = pyqtSignal()
    ui_state = pyqtSignal(str)

    def __init__(self, settings: Settings, memory: Memory) -> None:
        super().__init__()
        self.settings = settings
        self.memory = memory
        self.invoker = MainThreadInvoker()
        self.accent = str(settings.get("accent", "cyan"))
        self.connected = False
        self.last_response: Response | None = None
        self._history: list[str] = []
        self._history_index = -1
        self._speak_timer: QTimer | None = None
        self.tray: Any = None
        self._listening = False

        self.setWindowTitle("JARVIS")
        self.setMinimumSize(880, 620)
        self.resize(1060, 720)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Window)

        # voice engines (built lazily so a missing mic never blocks startup)
        self.tts = TextToSpeech(settings)
        self.stt = SpeechToText(settings)
        self.wake: WakeWordListener | None = None

        self.core = Core(settings, memory, self)
        self.core.refresh_state(voice_status=probe_voice(settings, self.tts, self.stt))

        self._build_ui()
        self._wire_signals()
        self._install_shortcuts()
        self.setStyleSheet(stylesheet(self.accent))
        self.apply_settings_to_ui()
        self.set_always_on_top(bool(settings.get("always_on_top", True)))
        self.setWindowOpacity(float(settings.get("opacity", 0.97)))

        if settings.get("connect_on_launch", True):
            QTimer.singleShot(300, self.connect_core)

    # ── construction ─────────────────────────────────────────────────────
    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        self.frame = QWidget(self)
        self.frame.setObjectName("Window")
        outer.addWidget(self.frame)

        root = QVBoxLayout(self.frame)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.title_bar = TitleBar(self)
        root.addWidget(self.title_bar)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)
        splitter.setHandleWidth(1)
        root.addWidget(splitter, 1)

        splitter.addWidget(self._build_left_panel())
        splitter.addWidget(self._build_right_panel())
        splitter.setSizes([330, 730])

        root.addWidget(self._build_status_bar())

        # resize grip + toast overlay live on the frame
        self.grip = QSizeGrip(self.frame)
        self.grip.setFixedSize(16, 16)
        self.toast = Toast(self.frame)

    def _build_left_panel(self) -> QWidget:
        panel = QWidget()
        panel.setObjectName("Panel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(10)

        self.hud = HudWidget(self.accent)
        self.hud.clicked.connect(self.toggle_mic)
        layout.addWidget(self.hud, 1)

        self.state_label = QLabel("STANDING BY")
        self.state_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.state_label.setObjectName("SectionTitle")
        layout.addWidget(self.state_label)

        mic_row = QHBoxLayout()
        mic_row.addStretch(1)
        self.mic_button = QPushButton("◉")
        self.mic_button.setObjectName("Mic")
        self.mic_button.setFixedSize(52, 52)
        self.mic_button.setToolTip("Click to speak (Ctrl+M)")
        self.mic_button.clicked.connect(self.toggle_mic)
        mic_row.addWidget(self.mic_button)
        mic_row.addStretch(1)
        layout.addLayout(mic_row)

        self.voice_label = QLabel("checking voice…")
        self.voice_label.setObjectName("Hint")
        self.voice_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.voice_label.setWordWrap(True)
        layout.addWidget(self.voice_label)

        chips_title = QLabel("QUICK COMMANDS")
        chips_title.setObjectName("SectionTitle")
        layout.addWidget(chips_title)

        self.chips: list[QPushButton] = []
        chip_grid = QGridLayout()
        chip_grid.setSpacing(6)
        quick = ["Brief me", "System status", "Weather", "Timers", "What are my tasks?", "Help"]
        for index, label in enumerate(quick):
            chip = QPushButton(label)
            chip.setObjectName("Chip")
            chip.clicked.connect(lambda _=False, text=label: self.submit(text))
            chip_grid.addWidget(chip, index // 2, index % 2)
            self.chips.append(chip)
        layout.addLayout(chip_grid)

        return panel

    def _build_right_panel(self) -> QWidget:
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.tabs.addTab(self._build_conversation_tab(), "CONVERSATION")
        self.tabs.addTab(self._build_skills_tab(), "SKILLS")
        self.tabs.addTab(self._build_memory_tab(), "MEMORY")
        self.tabs.addTab(self._build_settings_tab(), "SETTINGS")
        return self.tabs

    def _build_conversation_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(8)

        self.transcript = QTextBrowser()
        self.transcript.setOpenExternalLinks(True)
        self.transcript.setFont(QFont("Segoe UI", 10))
        layout.addWidget(self.transcript, 1)

        entry_row = QHBoxLayout()
        entry_row.setSpacing(8)
        self.input = QLineEdit()
        self.input.setPlaceholderText("Ask JARVIS anything…  (/help for commands)")
        self.input.returnPressed.connect(self.on_submit)
        self.input.installEventFilter(self)
        entry_row.addWidget(self.input, 1)

        self.send_button = QPushButton("SEND")
        self.send_button.setObjectName("Primary")
        self.send_button.clicked.connect(self.on_submit)
        entry_row.addWidget(self.send_button)
        layout.addLayout(entry_row)

        self.hint = QLabel("Ctrl+Space focuses this box · ↑ recalls history · voice: Ctrl+M")
        self.hint.setObjectName("Hint")
        layout.addWidget(self.hint)
        return page

    def _build_skills_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(8)

        self.skill_search = QLineEdit()
        self.skill_search.setPlaceholderText("Filter skills…")
        self.skill_search.textChanged.connect(self.refresh_skills)
        layout.addWidget(self.skill_search)

        self.skill_list = QListWidget()
        self.skill_list.itemDoubleClicked.connect(self._use_skill_example)
        layout.addWidget(self.skill_list, 1)

        note = QLabel("Double-click a skill to drop its example into the command box.")
        note.setObjectName("Hint")
        layout.addWidget(note)
        self.refresh_skills()
        return page

    def _build_memory_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(8)

        self.memory_stats = QLabel("")
        self.memory_stats.setObjectName("Muted")
        layout.addWidget(self.memory_stats)

        add_row = QHBoxLayout()
        self.memory_input = QLineEdit()
        self.memory_input.setPlaceholderText("New note…  (or a task: /task buy milk)")
        self.memory_input.returnPressed.connect(self.add_memory_entry)
        add_row.addWidget(self.memory_input, 1)
        add_note = QPushButton("Add note")
        add_note.clicked.connect(self.add_memory_entry)
        add_row.addWidget(add_note)
        layout.addLayout(add_row)

        self.memory_tabs = QTabWidget()
        self.memory_tabs.setDocumentMode(True)
        self.note_list = QListWidget()
        self.task_list = QListWidget()
        self.fact_list = QListWidget()
        self.memory_tabs.addTab(self.note_list, "NOTES")
        self.memory_tabs.addTab(self.task_list, "TASKS")
        self.memory_tabs.addTab(self.fact_list, "FACTS")
        layout.addWidget(self.memory_tabs, 1)

        buttons = QHBoxLayout()
        delete_note = QPushButton("Delete selected note")
        delete_note.clicked.connect(lambda: self._delete_memory("note"))
        buttons.addWidget(delete_note)
        toggle_task = QPushButton("Toggle task done")
        toggle_task.clicked.connect(lambda: self._delete_memory("task"))
        buttons.addWidget(toggle_task)
        forget_fact = QPushButton("Forget fact")
        forget_fact.clicked.connect(lambda: self._delete_memory("fact"))
        buttons.addWidget(forget_fact)
        buttons.addStretch(1)
        export = QPushButton("Export memory")
        export.clicked.connect(self.export_memory)
        buttons.addWidget(export)
        layout.addLayout(buttons)

        self.refresh_memory()
        return page

    def _build_settings_tab(self) -> QWidget:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(12)

        # ── brain ────────────────────────────────────────────────────────
        brain_box = QFrame()
        brain_box.setObjectName("PanelAlt")
        brain_layout = QFormLayout(brain_box)
        brain_layout.setContentsMargins(12, 10, 12, 12)
        brain_layout.setSpacing(8)

        self.provider_combo = QComboBox()
        for key, label in PROVIDER_LABELS.items():
            self.provider_combo.addItem(label, key)
        self.provider_combo.currentIndexChanged.connect(self.on_provider_changed)
        brain_layout.addRow("Brain", self.provider_combo)

        self.model_combo = QComboBox()
        self.model_combo.setEditable(True)
        self.model_combo.currentTextChanged.connect(self.on_model_changed)
        brain_layout.addRow("Model", self.model_combo)

        key_row = QWidget()
        key_layout = QHBoxLayout(key_row)
        key_layout.setContentsMargins(0, 0, 0, 0)
        key_layout.setSpacing(6)
        self.key_input = QLineEdit()
        self.key_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.key_input.setPlaceholderText("paste API key (stored in ~/.jarvis/settings.json)")
        key_layout.addWidget(self.key_input, 1)
        save_key = QPushButton("Save")
        save_key.clicked.connect(self.save_api_key)
        key_layout.addWidget(save_key)
        clear_key = QPushButton("Clear")
        clear_key.clicked.connect(self.clear_api_key)
        key_layout.addWidget(clear_key)
        brain_layout.addRow("API key", key_row)

        self.ollama_input = QLineEdit()
        self.ollama_input.setPlaceholderText("http://localhost:11434")
        self.ollama_input.editingFinished.connect(self.save_ollama_url)
        brain_layout.addRow("Ollama URL", self.ollama_input)

        test_row = QWidget()
        test_layout = QHBoxLayout(test_row)
        test_layout.setContentsMargins(0, 0, 0, 0)
        self.test_button = QPushButton("Test connection")
        self.test_button.clicked.connect(self.test_brain)
        test_layout.addWidget(self.test_button)
        self.brain_status = QLabel("")
        self.brain_status.setObjectName("Hint")
        self.brain_status.setWordWrap(True)
        test_layout.addWidget(self.brain_status, 1)
        brain_layout.addRow("", test_row)

        layout.addWidget(self._section("LANGUAGE MODEL", brain_box))

        # ── voice ────────────────────────────────────────────────────────
        voice_box = QFrame()
        voice_box.setObjectName("PanelAlt")
        voice_layout = QFormLayout(voice_box)
        voice_layout.setContentsMargins(12, 10, 12, 12)
        voice_layout.setSpacing(8)

        self.voice_replies_check = QCheckBox("Speak answers out loud")
        self.voice_replies_check.stateChanged.connect(self.on_voice_toggle)
        voice_layout.addRow("", self.voice_replies_check)

        self.tts_backend_combo = QComboBox()
        self.tts_backend_combo.addItems(["auto", "pyttsx3", "qt", "system", "none"])
        self.tts_backend_combo.currentTextChanged.connect(lambda value: self.save_setting("tts_backend", value, rebuild_voice=True))
        voice_layout.addRow("Speech engine", self.tts_backend_combo)

        self.voice_combo = QComboBox()
        self.voice_combo.currentIndexChanged.connect(self.on_voice_selected)
        refresh_voices = QPushButton("Reload voices")
        refresh_voices.clicked.connect(self.reload_voices)
        voice_row = QWidget()
        voice_row_layout = QHBoxLayout(voice_row)
        voice_row_layout.setContentsMargins(0, 0, 0, 0)
        voice_row_layout.addWidget(self.voice_combo, 1)
        voice_row_layout.addWidget(refresh_voices)
        voice_layout.addRow("Voice", voice_row)

        self.rate_slider = QSlider(Qt.Orientation.Horizontal)
        self.rate_slider.setRange(90, 320)
        self.rate_slider.valueChanged.connect(lambda value: self.save_setting("tts_rate", value))
        voice_layout.addRow("Rate", self.rate_slider)

        self.wake_check = QCheckBox("Wake word listening (say “Jarvis …”)")
        self.wake_check.stateChanged.connect(self.on_wake_toggle)
        voice_layout.addRow("", self.wake_check)

        self.wake_word_input = QLineEdit()
        self.wake_word_input.editingFinished.connect(
            lambda: self.save_setting("wake_word", self.wake_word_input.text().strip() or "jarvis")
        )
        voice_layout.addRow("Wake word", self.wake_word_input)

        self.stt_backend_combo = QComboBox()
        self.stt_backend_combo.addItems(["auto", "faster-whisper", "vosk", "google"])
        self.stt_backend_combo.currentTextChanged.connect(
            lambda value: self.save_setting("stt_backend", value, rebuild_voice=True)
        )
        voice_layout.addRow("Speech input", self.stt_backend_combo)

        self.whisper_combo = QComboBox()
        self.whisper_combo.addItems(["tiny", "base", "small"])
        self.whisper_combo.currentTextChanged.connect(
            lambda value: self.save_setting("whisper_model", value, rebuild_voice=True)
        )
        voice_layout.addRow("Whisper model", self.whisper_combo)

        self.mic_combo = QComboBox()
        self.mic_combo.currentIndexChanged.connect(self.on_mic_selected)
        voice_layout.addRow("Microphone", self.mic_combo)

        voice_status_row = QWidget()
        voice_status_layout = QHBoxLayout(voice_status_row)
        voice_status_layout.setContentsMargins(0, 0, 0, 0)
        check_voice = QPushButton("Re-check voice")
        check_voice.clicked.connect(self.recheck_voice)
        voice_status_layout.addWidget(check_voice)
        self.voice_status_label = QLabel("")
        self.voice_status_label.setObjectName("Hint")
        self.voice_status_label.setWordWrap(True)
        voice_status_layout.addWidget(self.voice_status_label, 1)
        voice_layout.addRow("", voice_status_row)

        layout.addWidget(self._section("VOICE", voice_box))

        # ── personal ─────────────────────────────────────────────────────
        personal_box = QFrame()
        personal_box.setObjectName("PanelAlt")
        personal_layout = QFormLayout(personal_box)
        personal_layout.setContentsMargins(12, 10, 12, 12)
        personal_layout.setSpacing(8)

        self.name_input = QLineEdit()
        self.name_input.editingFinished.connect(lambda: self.save_setting("user_name", self.name_input.text().strip()))
        personal_layout.addRow("Your name", self.name_input)

        self.city_input = QLineEdit()
        self.city_input.editingFinished.connect(lambda: self.save_setting("city", self.city_input.text().strip()))
        personal_layout.addRow("City (weather)", self.city_input)

        self.units_combo = QComboBox()
        self.units_combo.addItems(["metric", "imperial"])
        self.units_combo.currentTextChanged.connect(lambda value: self.save_setting("units", value))
        personal_layout.addRow("Units", self.units_combo)

        layout.addWidget(self._section("PERSONAL", personal_box))

        # ── interface ────────────────────────────────────────────────────
        ui_box = QFrame()
        ui_box.setObjectName("PanelAlt")
        ui_layout = QFormLayout(ui_box)
        ui_layout.setContentsMargins(12, 10, 12, 12)
        ui_layout.setSpacing(8)

        self.accent_combo = QComboBox()
        self.accent_combo.addItems(["cyan", "amber", "violet", "emerald"])
        self.accent_combo.currentTextChanged.connect(self.on_accent_changed)
        ui_layout.addRow("Accent", self.accent_combo)

        self.top_check = QCheckBox("Always on top")
        self.top_check.stateChanged.connect(lambda state: self.set_always_on_top(bool(state)) or
                                            self.save_setting("always_on_top", bool(state)))
        ui_layout.addRow("", self.top_check)

        self.opacity_slider = QSlider(Qt.Orientation.Horizontal)
        self.opacity_slider.setRange(55, 100)
        self.opacity_slider.valueChanged.connect(self.on_opacity_changed)
        ui_layout.addRow("Opacity", self.opacity_slider)

        self.startup_check = QCheckBox("Greet me on launch")
        self.startup_check.stateChanged.connect(lambda state: self.save_setting("connect_on_launch", bool(state)))
        ui_layout.addRow("", self.startup_check)

        # ── computer control ─────────────────────────────────────────────
        control_box = QFrame()
        control_box.setObjectName("PanelAlt")
        control_layout = QVBoxLayout(control_box)
        control_layout.setContentsMargins(12, 10, 12, 12)
        control_layout.setSpacing(8)

        self.trust_combo = QComboBox()
        for key, label in TRUST_LABELS.items():
            self.trust_combo.addItem(label, key)
        self.trust_combo.currentIndexChanged.connect(
            lambda: self.save_setting("trust_level", self.trust_combo.currentData())
        )
        trust_row = QFormLayout()
        trust_row.addRow("Ask before acting", self.trust_combo)
        control_layout.addLayout(trust_row)

        self.dangerous_check = QCheckBox("Allow destructive actions (delete, stop processes, shut down)")
        self.dangerous_check.stateChanged.connect(
            lambda state: self.save_setting("allow_dangerous", bool(state))
        )
        control_layout.addWidget(self.dangerous_check)

        self.dry_run_check = QCheckBox("Rehearse instead of acting (dry run — nothing on this machine changes)")
        self.dry_run_check.stateChanged.connect(self.on_dry_run_changed)
        control_layout.addWidget(self.dry_run_check)

        self.routine_watch_check = QCheckBox("Learn my habits and offer to save them as routines")
        self.routine_watch_check.stateChanged.connect(
            lambda state: self.save_setting("routine_watch", bool(state))
        )
        control_layout.addWidget(self.routine_watch_check)

        self.voice_confirm_check = QCheckBox("Ask out loud for risky actions (answer “yes” with your voice)")
        self.voice_confirm_check.stateChanged.connect(self.on_voice_confirm_changed)
        control_layout.addWidget(self.voice_confirm_check)

        self.control_status = QLabel("")
        self.control_status.setObjectName("Hint")
        self.control_status.setWordWrap(True)
        control_layout.addWidget(self.control_status)

        control_buttons = QHBoxLayout()
        show_actions = QPushButton("What can you control?")
        show_actions.clicked.connect(lambda: self.submit("what can you control"))
        control_buttons.addWidget(show_actions)
        show_audit = QPushButton("Action history")
        show_audit.clicked.connect(lambda: self.submit("show the action log"))
        control_buttons.addWidget(show_audit)
        control_buttons.addStretch(1)
        control_layout.addLayout(control_buttons)

        layout.addWidget(self._section("COMPUTER CONTROL", control_box))

        layout.addWidget(self._section("INTERFACE", ui_box))

        # ── data ─────────────────────────────────────────────────────────
        data_box = QFrame()
        data_box.setObjectName("PanelAlt")
        data_layout = QVBoxLayout(data_box)
        data_layout.setContentsMargins(12, 10, 12, 12)
        data_layout.setSpacing(8)

        self.data_path_label = QLabel(str(jarvis_home()))
        self.data_path_label.setObjectName("Hint")
        self.data_path_label.setWordWrap(True)
        data_layout.addWidget(self.data_path_label)

        data_buttons = QHBoxLayout()
        open_folder = QPushButton("Open data folder")
        open_folder.clicked.connect(lambda: open_with_os(str(jarvis_home())))
        data_buttons.addWidget(open_folder)
        reset = QPushButton("Reset settings")
        reset.clicked.connect(self.reset_settings)
        data_buttons.addWidget(reset)
        data_buttons.addStretch(1)
        data_layout.addLayout(data_buttons)

        layout.addWidget(self._section("DATA", data_box))
        layout.addStretch(1)
        scroll.setWidget(page)
        return scroll

    def _section(self, title: str, box: QWidget) -> QWidget:
        wrapper = QWidget()
        layout = QVBoxLayout(wrapper)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        label = QLabel(title)
        label.setObjectName("SectionTitle")
        layout.addWidget(label)
        layout.addWidget(box)
        return wrapper

    def _build_status_bar(self) -> QWidget:
        bar = QWidget()
        bar.setObjectName("Panel")
        bar.setFixedHeight(34)
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(14, 2, 14, 2)
        layout.setSpacing(14)

        self.status_brain = QLabel("brain: offline skills")
        self.status_voice = QLabel("voice: checking")
        self.status_memory = QLabel("memory: –")
        self.status_last = QLabel("")
        self.status_last.setObjectName("Hint")
        for widget in (self.status_brain, self.status_voice):
            widget.setObjectName("Hint")
            layout.addWidget(widget)
        layout.addWidget(self.status_memory)
        layout.addStretch(1)
        layout.addWidget(self.status_last)
        self.status_memory.setObjectName("Hint")
        return bar

    def _wire_signals(self) -> None:
        self.ui_log.connect(self.append_log)
        self.ui_toast.connect(lambda title, text, level: self.toast.show_message(title, text, level, accent=self.accent))
        self.ui_memory_changed.connect(self._refresh_memory_ui)
        self.ui_state.connect(self.set_state)

    def _install_shortcuts(self) -> None:
        def add(sequence: str, handler: Callable[[], None]) -> None:
            shortcut = QShortcut(QKeySequence(sequence), self)
            shortcut.activated.connect(handler)

        add("Ctrl+Space", self.focus_input)
        add("Ctrl+M", self.toggle_mic)
        add("Ctrl+Q", self.quit_app)
        add("Ctrl+B", lambda: self.submit("brief me"))
        add("F1", lambda: self.submit("help"))
        add("Esc", self.hide)

    # ── settings plumbing ────────────────────────────────────────────────
    def save_setting(self, key: str, value: Any, rebuild_voice: bool = False) -> None:
        self.settings[key] = value
        self.settings.save()
        if rebuild_voice:
            self.tts = TextToSpeech(self.settings)
            self.stt = SpeechToText(self.settings)
            self.core.refresh_state(voice_status=probe_voice(self.settings, self.tts, self.stt))
            self.recheck_voice()

    def apply_settings_to_ui(self) -> None:
        provider = self.settings.provider
        index = self.provider_combo.findData(provider)
        if index >= 0:
            self.provider_combo.blockSignals(True)
            self.provider_combo.setCurrentIndex(index)
            self.provider_combo.blockSignals(False)
        self.reload_models(provider)

        key = self.settings.api_key(provider) if provider != "ollama" else ""
        self.key_input.setText(key)
        self.ollama_input.setText(str(self.settings.get("ollama_url", "http://localhost:11434")))

        self.voice_replies_check.blockSignals(True)
        self.voice_replies_check.setChecked(bool(self.settings.get("voice_replies")))
        self.voice_replies_check.blockSignals(False)
        self.tts_backend_combo.setCurrentText(str(self.settings.get("tts_backend", "auto")))
        self.rate_slider.blockSignals(True)
        self.rate_slider.setValue(int(self.settings.get("tts_rate", 175)))
        self.rate_slider.blockSignals(False)

        self.wake_check.blockSignals(True)
        self.wake_check.setChecked(bool(self.settings.get("wake_word_enabled")))
        self.wake_check.blockSignals(False)
        self.wake_word_input.setText(str(self.settings.get("wake_word", "jarvis")))
        self.stt_backend_combo.setCurrentText(str(self.settings.get("stt_backend", "auto")))
        self.whisper_combo.setCurrentText(str(self.settings.get("whisper_model", "base")))

        self.name_input.setText(str(self.settings.get("user_name", "")))
        self.city_input.setText(str(self.settings.get("city", "")))
        self.units_combo.setCurrentText(str(self.settings.get("units", "metric")))

        self.accent_combo.setCurrentText(self.accent)
        self.top_check.setChecked(bool(self.settings.get("always_on_top", True)))
        self.opacity_slider.blockSignals(True)
        self.opacity_slider.setValue(int(float(self.settings.get("opacity", 0.97)) * 100))
        self.opacity_slider.blockSignals(False)
        self.startup_check.setChecked(bool(self.settings.get("connect_on_launch", True)))

        for widget, key, default in (
            (self.trust_combo, "trust_level", "ask_risky"),
            (self.dangerous_check, "allow_dangerous", False),
            (self.dry_run_check, "dry_run", False),
            (self.routine_watch_check, "routine_watch", True),
            (self.voice_confirm_check, "voice_confirm", False),
        ):
            widget.blockSignals(True)
            if isinstance(widget, QComboBox):
                index = widget.findData(str(self.settings.get(key, default)))
                widget.setCurrentIndex(index if index >= 0 else 0)
            else:
                widget.setChecked(bool(self.settings.get(key, default)))
            widget.blockSignals(False)
        core = getattr(self, "core", None)
        if core is not None and getattr(core, "controller", None) is not None:
            core.controller.set_simulation(bool(self.settings.get("dry_run", False)))
        self.refresh_control_status()

        self.reload_voices()
        self.refresh_mic_list()
        self.recheck_voice()
        self.update_status_bar()

    def reload_models(self, provider: str) -> None:
        self.model_combo.blockSignals(True)
        self.model_combo.clear()
        models = list((self.settings.as_dict().get("models") or {}).get(provider) or [])
        models += [m for m in PROVIDER_MODELS.get(provider, []) if m not in models]
        if provider == "ollama":
            live = self.core.brain.list_ollama_models()
            models = live + [m for m in models if m not in live]
        self.model_combo.addItems(models)
        self.model_combo.setCurrentText(self.settings.model_for(provider))
        self.model_combo.blockSignals(False)

    def on_provider_changed(self) -> None:
        provider = self.provider_combo.currentData()
        self.settings["provider"] = provider
        self.settings.save()
        self.reload_models(provider)
        self.key_input.setEnabled(provider in ("groq", "openai"))
        self.ollama_input.setEnabled(provider == "ollama")
        self.key_input.setText(self.settings.api_key(provider) if provider in ("groq", "openai") else "")
        self.recheck_voice()
        self.update_status_bar()

    def on_model_changed(self, model: str) -> None:
        if not model:
            return
        models = dict(self.settings.as_dict().get("models") or {})
        models[self.settings.provider] = model
        self.settings["models"] = models
        self.settings.save()

    def save_api_key(self) -> None:
        provider = self.settings.provider
        key = self.key_input.text().strip()
        self.settings.set_api_key(provider, key)
        self.settings.save()
        self.brain_status.setText("Key saved to settings.json (0600)." if key else "Key cleared.")
        self.update_status_bar()

    def clear_api_key(self) -> None:
        self.key_input.setText("")
        self.settings.set_api_key(self.settings.provider, "")
        self.settings.save()
        self.brain_status.setText("Key cleared.")
        self.update_status_bar()

    def save_ollama_url(self) -> None:
        self.save_setting("ollama_url", self.ollama_input.text().strip() or "http://localhost:11434")
        self.reload_models("ollama")

    def test_brain(self) -> None:
        self.brain_status.setText("Testing…")
        self.test_button.setEnabled(False)

        def done(reply) -> None:
            self.test_button.setEnabled(True)
            if reply.ok:
                snippet = (reply.text or "").strip().splitlines()[0][:90]
                self.brain_status.setText(f"✓ {reply.provider} replied in {reply.elapsed:.1f}s — {snippet}")
            else:
                self.brain_status.setText(f"✗ {reply.error}")
            self.update_status_bar()

        def work() -> None:
            reply = self.core.brain.test()
            self.invoker.call(done, reply)

        threading.Thread(target=work, daemon=True).start()

    def on_voice_toggle(self, state: int) -> None:
        self.save_setting("voice_replies", bool(state))
        if state and not self.tts.available:
            self.toast.show_message(
                "Voice output unavailable",
                "Install it with: pip install pyttsx3 (or pick the Qt engine).",
                level="info", accent=self.accent,
            )
        self.update_status_bar()

    def on_voice_selected(self, index: int) -> None:
        if index < 0:
            return
        voice_id = self.voice_combo.itemData(index) or ""
        self.save_setting("tts_voice", voice_id)

    def reload_voices(self) -> None:
        self.voice_combo.blockSignals(True)
        self.voice_combo.clear()
        self.voice_combo.addItem("(system default)", "")
        for voice_id, name in self.tts.voices():
            self.voice_combo.addItem(name, voice_id)
        saved = str(self.settings.get("tts_voice", ""))
        if saved:
            for index in range(self.voice_combo.count()):
                if self.voice_combo.itemData(index) == saved:
                    self.voice_combo.setCurrentIndex(index)
                    break
        self.voice_combo.blockSignals(False)

    def on_mic_selected(self, index: int) -> None:
        device = self.mic_combo.itemData(index)
        self.save_setting("mic_index", device)

    def refresh_mic_list(self) -> None:
        self.mic_combo.blockSignals(True)
        self.mic_combo.clear()
        self.mic_combo.addItem("(system default)", None)
        for device in self.stt.recorder.devices():
            self.mic_combo.addItem(f"{device.index}: {device.name}", device.index)
        self.mic_combo.blockSignals(False)

    def recheck_voice(self) -> None:
        report = probe_voice(self.settings, self.tts, self.stt)
        self.core.refresh_state(voice_status=report)
        self.voice_status_label.setText(f"Out: {report['tts']}\nIn: {report['stt']}")
        self.voice_label.setText(report["summary"])
        self.update_status_bar()

    def on_wake_toggle(self, state: int) -> None:
        enabled = bool(state)
        self.save_setting("wake_word_enabled", enabled)
        if enabled:
            if not self.stt.available:
                self.wake_check.blockSignals(True)
                self.wake_check.setChecked(False)
                self.wake_check.blockSignals(False)
                self.settings["wake_word_enabled"] = False
                self.settings.save()
                QMessageBox.information(
                    self, "Wake word needs a speech engine",
                    "Install the speech-input extras first:\n\n"
                    "    pip install sounddevice faster-whisper\n\n"
                    "(or vosk / SpeechRecognition), then re-check voice.",
                )
                return
            self.start_wake_listener()
        else:
            self.stop_wake_listener()
        self.update_status_bar()

    def on_voice_confirm_changed(self, state: int) -> None:
        self.save_setting("voice_confirm", bool(state))
        core = getattr(self, "core", None)
        if core is not None and getattr(core, "actions", None) is not None:
            core.actions.voice_confirm = bool(state)

    def on_dry_run_changed(self, state: int) -> None:
        self.save_setting("dry_run", bool(state))
        core = getattr(self, "core", None)
        if core is not None and getattr(core, "controller", None) is not None:
            core.controller.set_simulation(bool(state))
        self.refresh_control_status()

    def refresh_control_status(self) -> None:
        """One live line describing the control surface (capabilities, mode)."""
        label = getattr(self, "control_status", None)
        core = getattr(self, "core", None)
        if label is None or core is None or getattr(core, "actions", None) is None:
            if label is not None:
                label.setText("Computer control is disabled in settings.")
            return
        report = core.controller.report()
        ready = [name for name, cap in report.capabilities.items() if cap.available]
        missing = [name for name, cap in report.capabilities.items() if not cap.available]
        routines = len(core.routine_store.all()) if core.routine_store else 0
        text = (f"{len(core.actions.actions)} actions · backend: {report.backend}"
                + (" · DRY RUN" if report.simulated else "")
                + f"\nready: {', '.join(ready) or 'nothing'}"
                + f"\nroutines in memory: {routines}"
                + f"\naudit log: {core.audit.path}")
        if missing:
            text += f"\nneeds setup: {', '.join(missing)}"
            hints = control_install_hints()
            if hints:
                text += "\n" + "\n".join(f"• {hint}" for hint in hints)
        label.setText(text)

    def on_accent_changed(self, accent: str) -> None:
        self.accent = accent
        self.save_setting("accent", accent)
        self.setStyleSheet(stylesheet(accent))
        self.hud.set_accent(accent)
        self.update()

    def on_opacity_changed(self, value: int) -> None:
        self.save_setting("opacity", value / 100)
        self.setWindowOpacity(value / 100)

    def reset_settings(self) -> None:
        confirm = QMessageBox.question(
            self, "Reset settings",
            "Reset all preferences to defaults?\n\nNotes, tasks and facts are kept.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if confirm == QMessageBox.StandardButton.Yes:
            self.settings.reset()
            self.settings.save()
            self.apply_settings_to_ui()
            self.core.refresh_state()
            self.toast.show_message("Settings reset", "Defaults restored.", accent=self.accent)

    # ── host implementation (called from any thread) ─────────────────────
    def notify(self, title: str, text: str, level: str = "info") -> None:
        self.ui_toast.emit(title, text, level)
        if self.tray and self.tray.isVisible() and level == "alarm":
            try:
                self.invoker.call(self.tray.showMessage, title, text)
            except Exception:
                pass

    def speak(self, text: str) -> None:
        if not self.settings.get("voice_replies") or not text.strip():
            return
        self.invoker.call(self._speak_now, text)

    def _speak_now(self, text: str) -> None:
        if not self.tts.speak(text):
            return
        self.set_state("speaking")
        seconds = max(1.2, min(45.0, len(text) / 13.0))
        if self._speak_timer:
            self._speak_timer.stop()
        self._speak_timer = QTimer(self)
        self._speak_timer.setSingleShot(True)
        self._speak_timer.timeout.connect(lambda: self.set_state("idle"))
        self._speak_timer.start(int(seconds * 1000))
        # animate the meter while the voice is talking
        steps = int(seconds * 12)
        for step in range(steps):
            level = 0.35 + 0.45 * abs(__import__("math").sin(step * 0.7))
            QTimer.singleShot(int(step * 80), lambda value=level: self.hud.set_level(value))

    def get_clipboard(self) -> str:
        try:
            return self.invoker.call(lambda: QGuiApplication.clipboard().text(), blocking=True) or ""
        except Exception:
            return ""

    def set_clipboard(self, text: str) -> bool:
        def write() -> bool:
            clipboard = QGuiApplication.clipboard()
            clipboard.setText(text)
            return True
        try:
            return bool(self.invoker.call(write, blocking=True))
        except Exception:
            return False

    def screenshot(self, path: Path | None = None) -> str | None:
        def grab() -> str | None:
            folder = jarvis_home() / "screenshots"
            folder.mkdir(parents=True, exist_ok=True)
            target = path or folder / f"shot-{time.strftime('%Y%m%d-%H%M%S')}.png"
            try:
                screen = QGuiApplication.primaryScreen()
                if screen is None:
                    raise RuntimeError("no screen")
                pixmap = screen.grabWindow(0)
                if pixmap.isNull():
                    raise RuntimeError("null pixmap")
                if pixmap.save(str(target)):
                    return str(target)
            except Exception:
                pass
            try:  # fall back to mss/PIL (handles multi-monitor desktops well)
                import mss
                from PIL import Image

                with mss.mss() as sct:
                    shot = sct.grab(sct.monitors[0])
                    Image.frombytes("RGB", shot.size, shot.rgb).save(target)
                return str(target)
            except Exception:
                return None
        try:
            return self.invoker.call(grab, blocking=True, timeout=15)
        except Exception:
            return None

    def open_target(self, target: str, kind: str = "auto") -> bool:
        return open_with_os(target)

    # ── approval: the human half of the safety policy ────────────────────
    def confirm(self, title: str, detail: str, risk: str = "confirm") -> bool:
        """Ask the user to approve a computer-control action.

        This is called from worker threads (the core runs skills off the GUI
        thread), so it marshals onto the GUI thread and *blocks* until the dialog
        closes. Anything that goes wrong answers “no”: the policy fails closed.
        """
        def ask() -> bool:
            dialog = ConfirmDialog(title, detail, risk, self)
            accepted = dialog.exec() == QDialog.DialogCode.Accepted
            if accepted and dialog.trust_requested:
                self.save_setting("trust_level", "trusted")
                self.apply_settings_to_ui()
            return accepted

        try:
            return bool(self.invoker.call(ask, blocking=True, timeout=300))
        except Exception:
            return False

    def ask_text(self, prompt: str, default: str = "") -> str | None:
        """Free-text question for the ``ask_user`` action (used inside routines)."""
        def ask() -> str | None:
            dialog = QDialog(self)
            dialog.setWindowTitle("JARVIS — one question")
            dialog.setModal(True)
            layout = QVBoxLayout(dialog)
            label = QLabel(prompt)
            label.setWordWrap(True)
            layout.addWidget(label)
            field = QLineEdit(default)
            layout.addWidget(field)
            buttons = QDialogButtonBox()
            buttons.addButton("OK", QDialogButtonBox.ButtonRole.AcceptRole)
            buttons.addButton("Cancel", QDialogButtonBox.ButtonRole.RejectRole)
            buttons.accepted.connect(dialog.accept)
            buttons.rejected.connect(dialog.reject)
            layout.addWidget(buttons)
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return None
            return field.text()

        try:
            return self.invoker.call(ask, blocking=True, timeout=300)
        except Exception:
            return None

    def schedule(self, delay: float, callback: Callable[[], None]) -> Any:
        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.timeout.connect(callback)
        self.invoker.call(timer.start, int(delay * 1000))
        return timer

    def cancel_schedule(self, handle: Any) -> bool:
        try:
            self.invoker.call(handle.stop)
            return True
        except Exception:
            return False

    def log(self, text: str, level: str = "info", meta: dict[str, Any] | None = None) -> None:
        label = ""
        if meta:
            label = str(meta.get("skill") or "")
            if meta.get("duration"):
                label += f" · {float(meta['duration']):.1f}s"
        self.ui_log.emit(f"{text}\x00{label}", level)

    def refresh_memory(self) -> None:
        """Host protocol method: rebuild the memory tab, from any thread."""
        app = QApplication.instance()
        if app is not None and QThread.currentThread() is not app.thread():
            self.invoker.call(self._refresh_memory_ui)
        else:
            self._refresh_memory_ui()

    # ── conversation ─────────────────────────────────────────────────────
    def _insert_html(self, markup: str) -> None:
        """Append HTML at the end of the transcript (QTextBrowser has no appendHtml)."""
        cursor = self.transcript.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        if not self.transcript.document().isEmpty():
            cursor.insertBlock()
        cursor.insertHtml(markup)
        self.transcript.setTextCursor(cursor)
        bar = self.transcript.verticalScrollBar()
        bar.setValue(bar.maximum())

    def append_log(self, text: str, level: str = "info") -> None:
        role = {"user": "user", "error": "error", "alarm": "system"}.get(level, "jarvis")
        if "\x00" in text:  # label travels with the message, so no shared state
            text, detail = text.split("\x00", 1)
        elif level == "jarvis" and self.last_response:
            detail = self.last_response.skill or self.last_response.provider
            if self.last_response.duration:
                detail += f" · {self.last_response.duration:.1f}s"
        else:
            detail = {"alarm": "reminder", "error": "needs attention"}.get(level, "")
        if level == "user":
            text = f"You: {text}"
        self._insert_html(render_message(text, role, self.accent, detail))
        self.transcript.verticalScrollBar().setValue(self.transcript.verticalScrollBar().maximum())

    def set_state(self, state: str) -> None:
        self.hud.set_state(state)
        self.state_label.setText(self.hud.state_label())
        colour = self.hud.state_color()
        self.state_label.setStyleSheet(f"color: {colour};")
        self.mic_button.setProperty("active", "true" if state in ("listening", "capturing") else "false")
        self.mic_button.style().unpolish(self.mic_button)
        self.mic_button.style().polish(self.mic_button)

    def focus_input(self) -> None:
        self.tabs.setCurrentIndex(0)
        self.input.setFocus()
        self.input.selectAll()

    def on_submit(self) -> None:
        text = self.input.text().strip()
        if not text:
            return
        self.input.clear()
        self.submit(text)

    def submit(self, text: str) -> None:
        text = (text or "").strip()
        if not text:
            return
        self._history.append(text)
        self._history_index = len(self._history)
        self.set_state("thinking")
        self.status_last.setText("thinking…")
        started = time.time()

        def finished(response: Response) -> None:
            # ask_async calls back from a worker thread: hop to the GUI thread.
            self.invoker.call(apply_response, response)

        def apply_response(response: Response) -> None:
            self.last_response = response
            self.set_state("idle")
            self.status_last.setText(f"answered in {time.time() - started:.2f}s"
                                     if response.ok else "needs attention")
            if response.ui_action.get("clear_conversation"):
                self.transcript.clear()
            if response.ui_action.get("refresh_settings"):
                self.apply_settings_to_ui()
            if response.ui_action.get("refresh_memory"):
                self.refresh_memory()
            if response.ui_action.get("quit"):
                self.quit_app()
            self.update_status_bar()

        self.core.ask_async(text, finished)

    def eventFilter(self, source: QObject, event: QEvent) -> bool:  # noqa: N802
        if source is self.input and event.type() == QEvent.Type.KeyPress:
            key = event.key()
            if key == Qt.Key.Key_Up and self._history:
                self._history_index = max(0, self._history_index - 1)
                self.input.setText(self._history[self._history_index])
                return True
            if key == Qt.Key.Key_Down and self._history:
                self._history_index = min(len(self._history), self._history_index + 1)
                self.input.setText("" if self._history_index >= len(self._history)
                                   else self._history[self._history_index])
                return True
        return super().eventFilter(source, event)

    # ── microphone ───────────────────────────────────────────────────────
    def toggle_mic(self) -> None:
        if not self.stt.available:
            QMessageBox.information(
                self, "Speech input unavailable",
                "JARVIS could not find a speech-to-text engine or microphone.\n\n"
                "Install the optional voice extras:\n\n"
                "    pip install sounddevice faster-whisper\n\n"
                "Everything else keeps working — just type your requests.",
            )
            return
        if getattr(self, "_listening", False):
            return
        self._listening = True
        self.set_state("listening")
        self.mic_button.setEnabled(False)

        def work() -> None:
            transcript = self.stt.listen(max_seconds=9.0)

            def done() -> None:
                self._listening = False
                self.mic_button.setEnabled(True)
                self.set_state("idle")
                self.hud.set_level(0.0)
                if transcript.strip():
                    self.input.setText(transcript.strip())
                    self.on_submit()
                else:
                    self.append_log("I didn't hear anything — try again.", "system")

            self.invoker.call(done)

        threading.Thread(target=work, daemon=True).start()

    def start_wake_listener(self) -> None:
        if self.wake and self.wake.running:
            return
        self.wake = WakeWordListener(
            stt=self.stt,
            settings=self.settings,
            on_command=lambda command: self.invoker.call(self.submit, command),
            on_state=lambda state: self.invoker.call(self.set_state, state),
            on_level=lambda level: self.invoker.call(self.hud.set_level, level),
        )
        if not self.wake.start():
            self.append_log("Wake-word listening could not start (no speech engine).", "system")

    def stop_wake_listener(self) -> None:
        if self.wake:
            self.wake.stop()
            self.wake = None
        self.set_state("idle")

    # ── skills / memory tabs ─────────────────────────────────────────────
    def refresh_skills(self) -> None:
        needle = self.skill_search.text().strip().lower() if hasattr(self, "skill_search") else ""
        self.skill_list.clear()
        for skill in sorted(self.core.registry.skills, key=lambda s: s.title):
            example = skill.examples[0] if skill.examples else ""
            haystack = f"{skill.title} {skill.name} {skill.description} {example}".lower()
            if needle and needle not in haystack:
                continue
            item = QListWidgetItem(f"{skill.title} — {skill.description}")
            item.setData(Qt.ItemDataRole.UserRole, example)
            item.setToolTip(f"Try: {example}" if example else skill.description)
            self.skill_list.addItem(item)

    def _use_skill_example(self, item: QListWidgetItem) -> None:
        example = item.data(Qt.ItemDataRole.UserRole) or ""
        if example:
            self.tabs.setCurrentIndex(0)
            self.input.setText(example)
            self.input.setFocus()
            self.input.selectAll()

    def _refresh_memory_ui(self) -> None:
        if not hasattr(self, "memory_stats"):  # layout still building
            return
        stats = self.memory.stats()
        self.memory_stats.setText(
            f"{stats['notes']} notes · {stats['tasks_open']} open tasks · "
            f"{stats['tasks_done']} done · {stats['facts']} facts · {stats['turns']} conversation turns"
        )
        self.note_list.clear()
        for note in self.memory.list_notes(200):
            item = QListWidgetItem(f"[{note.id}] {note.when} — {note.text}")
            item.setData(Qt.ItemDataRole.UserRole, note.id)
            self.note_list.addItem(item)
        self.task_list.clear()
        for task in self.memory.tasks:
            prefix = "✓" if task.done else "•"
            item = QListWidgetItem(f"{prefix} [{task.id}] {task.text}")
            item.setData(Qt.ItemDataRole.UserRole, task.id)
            item.setForeground(QColor("#4c5b78") if task.done else QColor("#dce7ff"))
            self.task_list.addItem(item)
        self.fact_list.clear()
        routines: list[tuple[str, str]] = []
        for key, value in sorted(self.memory.facts.items()):
            if key.startswith("routine."):
                routines.append((key, value))          # shown below, not as raw JSON
                continue
            item = QListWidgetItem(f"{key} = {value}")
            item.setData(Qt.ItemDataRole.UserRole, key)
            self.fact_list.addItem(item)
        for key, value in routines:
            summary = self._routine_summary(value)
            item = QListWidgetItem(f"▶ {key} — {summary}")
            item.setData(Qt.ItemDataRole.UserRole, key)
            item.setForeground(QColor("#7ef0d0"))
            self.fact_list.addItem(item)
        self.update_status_bar()

    @staticmethod
    def _routine_summary(payload: str) -> str:
        """One readable line for a routine fact (the stored value is JSON)."""
        try:
            data = json.loads(payload)
        except (ValueError, TypeError):
            return "routine (unreadable entry)"
        steps = data.get("steps") or []
        origin = {"recorded": "recorded", "learned": "learned", "planned": "from a plan",
                  "taught": "taught"}.get(str(data.get("source", "")), "saved")
        runs = data.get("times_run") or 0
        return (f"{len(steps)} steps · {origin}"
                + (f" · run {runs}×" if runs else "")
                + " · say “run my " + str(data.get("name", "")).split(" routine")[0] + "”")

    def add_memory_entry(self) -> None:
        text = self.memory_input.text().strip()
        if not text:
            return
        self.memory_input.clear()
        if text.lower().startswith("/task"):
            self.memory.add_task(text[5:].strip())
        elif text.lower().startswith("/fact"):
            body = text[5:].strip()
            if "=" in body:
                key, _, value = body.partition("=")
                self.memory.remember(key, value)
        else:
            self.memory.add_note(text)
        self.refresh_memory()

    def _delete_memory(self, kind: str) -> None:
        if kind == "note":
            item = self.note_list.currentItem()
            if item:
                self.memory.delete_note(item.data(Qt.ItemDataRole.UserRole))
        elif kind == "task":
            item = self.task_list.currentItem()
            if item:
                task = self.memory.find_task(item.data(Qt.ItemDataRole.UserRole))
                if task:
                    task.done = not task.done
                    self.memory.save()
        else:
            item = self.fact_list.currentItem()
            if item:
                self.memory.forget(item.data(Qt.ItemDataRole.UserRole))
        self.refresh_memory()

    def export_memory(self) -> None:
        target = jarvis_home() / "memory-export.txt"
        target.write_text(self.memory.export_text(), encoding="utf-8")
        self.toast.show_message("Memory exported", str(target), accent=self.accent)

    # ── lifecycle ────────────────────────────────────────────────────────
    def connect_core(self) -> None:
        if self.connected:
            return
        self.connected = True
        response = self.core.greeting()
        self._insert_html(render_message(response.text, "system", self.accent, f"v{VERSION}"))
        # weather + system context is a network call: warm it off the GUI thread
        QTimer.singleShot(1200, lambda: threading.Thread(
            target=self.core.context_lines, daemon=True, name="jarvis-context").start())

    def set_always_on_top(self, on: bool) -> bool:
        self.settings["always_on_top"] = on
        flags = self.windowFlags()
        if on:
            flags |= Qt.WindowType.WindowStaysOnTopHint
        else:
            flags &= ~Qt.WindowType.WindowStaysOnTopHint
        self.setWindowFlags(flags | Qt.WindowType.FramelessWindowHint)
        self.show()
        return on

    def update_status_bar(self) -> None:
        if not hasattr(self, "status_brain"):  # still building the layout
            return
        brain = self.core.brain
        label = PROVIDER_LABELS.get(brain.provider, brain.provider)
        model = self.settings.model_for()
        self.status_brain.setText(f"brain: {label}" + (f" · {model}" if brain.enabled else ""))
        if self.title_bar:
            text = "OFFLINE SKILLS" if not brain.enabled else brain.provider.upper()
            if brain.enabled and not brain.ready and brain.provider != "ollama":
                text += " · NO KEY"
            self.title_bar.status_pill.setText(text)
        self.status_voice.setText(
            f"voice: out {('ready' if self.tts.available else 'off')} · in {('ready' if self.stt.available else 'off')}"
        )
        self.status_memory.setText(f"memory: {self.memory.stats()['turns']} turns")
        if self.brain_status and brain.enabled:
            self.brain_status.setText(brain.status())

    def quit_app(self) -> None:
        try:
            self.stop_wake_listener()
            self.core.shutdown()
            if self.tts.available:
                self.tts.stop()
            if self.tray:
                self.tray.hide()
        finally:
            QApplication.quit()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self.grip.move(self.frame.width() - self.grip.width() - 6, self.frame.height() - self.grip.height() - 6)
        if hasattr(self, "toast") and self.toast.isVisible():
            self.toast.place()

    def closeEvent(self, event) -> None:  # noqa: N802
        event.ignore()
        self.hide()

    # ── system tray ──────────────────────────────────────────────────────
    def install_tray(self, icon: QIcon) -> None:
        from PyQt6.QtWidgets import QSystemTrayIcon

        self.tray = None
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        try:
            tray = QSystemTrayIcon(icon, self)
        except Exception:
            return
        menu = self._tray_menu()
        tray.setContextMenu(menu)
        tray.setToolTip("JARVIS — personal assistant")
        tray.activated.connect(lambda reason: self.show() if reason == QSystemTrayIcon.ActivationReason.Trigger else None)
        tray.show()
        self.tray = tray

    def _tray_menu(self):
        from PyQt6.QtWidgets import QMenu

        menu = QMenu(self)
        show = QAction("Show JARVIS", self)
        show.triggered.connect(lambda: (self.show(), self.raise_(), self.activateWindow()))
        menu.addAction(show)
        brief = QAction("Run briefing", self)
        brief.triggered.connect(lambda: self.submit("brief me"))
        menu.addAction(brief)
        mute = QAction("Toggle spoken replies", self)
        mute.triggered.connect(lambda: self.voice_replies_check.setChecked(not self.voice_replies_check.isChecked()))
        menu.addAction(mute)
        menu.addSeparator()
        quit_action = QAction("Quit", self)
        quit_action.triggered.connect(self.quit_app)
        menu.addAction(quit_action)
        return menu


def app_icon(accent: str = "cyan") -> QIcon:
    """Draw a small reactor icon in code so no asset files are needed."""
    colours = accent_colors(accent)
    size = 64
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setBrush(QColor("#05070d"))
    painter.setPen(QColor(colours["accent_dim"]))
    painter.drawEllipse(2, 2, size - 4, size - 4)
    painter.setPen(QColor(colours["accent"]))
    for radius in (26, 19):
        painter.drawEllipse(size // 2 - radius, size // 2 - radius, radius * 2, radius * 2)
    painter.setBrush(QColor(colours["accent"]))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawEllipse(size // 2 - 7, size // 2 - 7, 14, 14)
    painter.end()
    return QIcon(pixmap)
