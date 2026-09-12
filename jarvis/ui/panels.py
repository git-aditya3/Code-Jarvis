"""
The modern panels: dashboard, routines, actions, learning and the ⌘K palette.

These widgets are deliberately thin. They read live state out of the
:class:`~jarvis.core.Core` that the window already owns (``window.core``) and
send anything they want to *do* back through ``window.submit()`` — the same path
a typed sentence takes. Nothing in here runs an action behind the policy layer's
back, so the safety rules, the audit log and behaviour learning all still apply
to a button press exactly as they do to a spoken command.
"""

from __future__ import annotations

import html
import time
from collections.abc import Callable
from typing import Any

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QScrollArea,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from ..config import PROVIDER_LABELS
from .theme import accent_colors

RISK_TONE = {"safe": "ok", "confirm": "warn", "dangerous": "error", "blocked": "error"}

#: Accent colours for the tiles, cycled for visual rhythm.
TILE_TONES = ("accent", "ok", "warn", "info")


def _esc(text: Any) -> str:
    return html.escape(str(text if text is not None else ""))


# ════════════════════════════════════════════════════════════════════════════
#  Building blocks
# ════════════════════════════════════════════════════════════════════════════

class Card(QFrame):
    """A titled card: the unit every panel is built out of."""

    def __init__(self, title: str = "", subtitle: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Card")
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(14, 12, 14, 12)
        self.body.setSpacing(8)

        header = QHBoxLayout()
        header.setSpacing(8)
        self.title_label = QLabel(title.upper())
        self.title_label.setObjectName("CardTitle")
        header.addWidget(self.title_label)
        self.subtitle_label = QLabel(subtitle)
        self.subtitle_label.setObjectName("Hint")
        header.addWidget(self.subtitle_label)
        header.addStretch(1)
        self.header = header
        self.body.addLayout(header)

    def set_title(self, title: str, subtitle: str = "") -> None:
        self.title_label.setText(title.upper())
        self.subtitle_label.setText(subtitle)

    def add(self, widget: QWidget, stretch: int = 0) -> QWidget:
        self.body.addWidget(widget, stretch)
        return widget

    def add_row(self, *widgets: QWidget) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(8)
        for widget in widgets:
            row.addWidget(widget)
        self.body.addLayout(row)
        return row

    def add_action(self, label: str, handler: Callable[[], None], primary: bool = False) -> QPushButton:
        button = QPushButton(label)
        button.setObjectName("Primary" if primary else "Chip")
        button.clicked.connect(lambda _=False: handler())
        self.header.addWidget(button)
        return button

    def clear_body(self) -> None:
        """Remove everything except the header (used when refreshing)."""
        while self.body.count() > 1:
            item = self.body.takeAt(self.body.count() - 1)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
            elif item.layout() is not None:
                _drop_layout(item.layout())


def _drop_layout(layout: Any) -> None:
    while layout.count():
        item = layout.takeAt(0)
        widget = item.widget()
        if widget is not None:
            widget.deleteLater()
        elif item.layout() is not None:
            _drop_layout(item.layout())


class StatTile(QFrame):
    """A big number with a label — the dashboard's top row."""

    def __init__(self, label: str, value: str = "—", hint: str = "", tone: str = "accent",
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Tile")
        self.setProperty("tone", tone)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(2)

        self.value_label = QLabel(value)
        self.value_label.setObjectName("TileValue")
        self.value_label.setProperty("tone", tone)
        layout.addWidget(self.value_label)

        self.caption = QLabel(label.upper())
        self.caption.setObjectName("TileLabel")
        layout.addWidget(self.caption)

        self.hint_label = QLabel(hint)
        self.hint_label.setObjectName("Hint")
        self.hint_label.setWordWrap(True)
        layout.addWidget(self.hint_label)

    def set(self, value: str, hint: str = "") -> None:
        self.value_label.setText(value)
        if hint:
            self.hint_label.setText(hint)


class Pill(QLabel):
    """A small status pill: ``● Pollinations · free``."""

    def __init__(self, text: str = "", tone: str = "muted", parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self.setObjectName("Pill")
        self.set_tone(tone)

    def set_tone(self, tone: str) -> None:
        self.setProperty("tone", tone)
        style = self.style()
        if style is not None:
            style.unpolish(self)
            style.polish(self)


def _wrap_scroll(widget: QWidget) -> QScrollArea:
    area = QScrollArea()
    area.setWidgetResizable(True)
    area.setFrameShape(QFrame.Shape.NoFrame)
    area.setWidget(widget)
    return area


def _panel_column() -> tuple[QWidget, QVBoxLayout]:
    page = QWidget()
    layout = QVBoxLayout(page)
    layout.setContentsMargins(12, 10, 12, 10)
    layout.setSpacing(10)
    return page, layout


# ════════════════════════════════════════════════════════════════════════════
#  Dashboard
# ════════════════════════════════════════════════════════════════════════════

class DashboardPanel(QWidget):
    """Everything worth seeing at a glance: brain, capability, habits, activity."""

    def __init__(self, window: Any) -> None:
        super().__init__()
        self.window = window
        page, layout = _panel_column()
        layout.setContentsMargins(0, 0, 0, 0)

        self.tiles = {
            "brain": StatTile("Brain", "—", "free cloud", "accent"),
            "actions": StatTile("Actions", "—", "things I can do", "info"),
            "routines": StatTile("Routines", "—", "saved to memory", "ok"),
            "learned": StatTile("Learned", "—", "habits remembered", "warn"),
        }
        tile_row = QHBoxLayout()
        tile_row.setSpacing(10)
        for tile in self.tiles.values():
            tile_row.addWidget(tile)
        layout.addLayout(tile_row)

        self.brain_card = Card("Brain", "free models first")
        self.brain_pills = QVBoxLayout()
        self.brain_card.body.addLayout(self.brain_pills)
        self.brain_note = QLabel("")
        self.brain_note.setObjectName("Hint")
        self.brain_note.setWordWrap(True)
        self.brain_card.add(self.brain_note)
        self.brain_card.add_action("Settings", lambda: self.window.goto_tab("settings"))
        layout.addWidget(self.brain_card)

        row = QHBoxLayout()
        row.setSpacing(10)

        self.habits_card = Card("What I learned", "from your own use")
        self.habits_text = QLabel("Not enough history yet.")
        self.habits_text.setObjectName("Body")
        self.habits_text.setWordWrap(True)
        self.habits_card.add(self.habits_text)
        self.habits_card.add_action("More", lambda: self.window.goto_tab("learning"))
        row.addWidget(self.habits_card, 3)

        self.activity_card = Card("Recent activity")
        self.activity_list = QTextBrowser()
        self.activity_list.setObjectName("CardBody")
        self.activity_list.setMinimumHeight(150)
        self.activity_card.add(self.activity_list, 1)
        self.activity_card.add_action("Log", lambda: self.window.goto_tab("actions"))
        row.addWidget(self.activity_card, 4)

        layout.addLayout(row, 1)

        self.quick_card = Card("Do something", "one click, same rules as voice")
        grid = QGridLayout()
        grid.setSpacing(6)
        quick = [
            ("Brief me", "brief me"),
            ("What's open?", "what's open"),
            ("Read my screen", "read the screen"),
            ("System status", "system status"),
            ("Snapshot", "take a screenshot"),
            ("Show my routines", "what routines do I have"),
            ("What have you learned?", "/profile"),
            ("Free models", "/cloud"),
        ]
        for index, (label, command) in enumerate(quick):
            button = QPushButton(label)
            button.setObjectName("Chip")
            button.clicked.connect(lambda _=False, text=command: self.window.submit(text))
            grid.addWidget(button, index // 4, index % 4)
        self.quick_card.body.addLayout(grid)
        layout.addWidget(self.quick_card)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(_wrap_scroll(page))

    # ── refresh ──────────────────────────────────────────────────────────
    def refresh(self) -> None:
        core = getattr(self.window, "core", None)
        if core is None:
            return
        brain = core.brain
        profile = core.profile
        registry = core.actions

        ladder = brain.ladder()
        self.tiles["brain"].set(
            "offline" if not brain.enabled else PROVIDER_LABELS.get(brain.provider, brain.provider).split("·")[0].strip(),
            f"{brain.settings.model_for()}" + ("" if brain.ready else " · needs a key"))
        count = len(registry.actions) if registry else 0
        self.tiles["actions"].set(str(count), "including files, windows, keys, media")
        routines = core.routine_store.all() if core.routine_store else []
        self.tiles["routines"].set(str(len(routines)), "run any of them by name")
        stats = profile.stats()
        self.tiles["learned"].set(str(stats["actions_seen"]),
                                  f"{stats['aliases']} phrases · {stats['trusted']} trusted")

        _drop_layout(self.brain_pills)
        for name, label in PROVIDER_LABELS.items():
            if name == "offline":
                continue
            ready = brain.usable(name)
            suffix = "  · in use" if name == brain.provider else ""
            pill = Pill(f"{'●' if ready else '○'} {label}{suffix}",
                        "ok" if name == brain.provider else ("info" if ready else "muted"))
            self.brain_pills.addWidget(pill)
        self.brain_card.set_title("Brain", f"{len(ladder)} provider(s) in the ladder")
        if brain.offline:
            self.brain_note.setText("No route to the internet right now — every offline skill still "
                                    "works, and I retry the cloud automatically.")
        elif not brain.ready:
            self.brain_note.setText("Add a free key (Groq, Gemini or OpenRouter) in Settings for "
                                    "faster answers; the keyless free pool covers you until then.")
        else:
            self.brain_note.setText(f"Answers come from {label_of(brain.provider)}. "
                                    "Identical questions are cached, so repeats are instant.")

        self.habits_text.setText(_learned_html(profile))
        self.habits_text.setTextFormat(Qt.TextFormat.RichText)

        feed = registry.activity(limit=12) if registry else []
        if feed:
            lines = []
            for entry in feed:
                when = time.strftime("%H:%M:%S", time.localtime(entry.get("ts", 0)))
                mark = "✓" if entry.get("ok") else "✗"
                args = ", ".join(f"{key}={value}" for key, value in list(entry.get("args", {}).items())[:2])
                lines.append(f"<tr><td style='color:#7d90b3'>{when}</td><td>{mark}</td>"
                             f"<td><b>{_esc(entry.get('action'))}</b>"
                             f"<span style='color:#7d90b3'>{_esc('(' + args + ')' if args else '')}</span>"
                             f"<br><span style='color:#7d90b3'>{_esc(str(entry.get('message', ''))[:90])}</span>"
                             f"</td></tr>")
            self.activity_list.setHtml(
                "<table cellspacing='0' cellpadding='3'>" + "".join(lines) + "</table>")
        else:
            self.activity_list.setHtml(
                "<span style='color:#7d90b3'>Nothing yet. Ask me to open something and it will "
                "show up here — with the audit line behind it.</span>")


def label_of(provider: str) -> str:
    return PROVIDER_LABELS.get(provider, provider).split("·")[0].strip()


def _learned_html(profile: Any) -> str:
    apps = profile.top_apps(5)
    actions = profile.top_actions(5)
    if not apps and not actions:
        return "Nothing yet — a few commands and I'll start noticing patterns."
    parts = []
    if apps:
        parts.append("<b>Apps</b>: " + ", ".join(f"{_esc(name)} ×{count}" for name, count in apps))
    if actions:
        parts.append("<b>Commands</b>: "
                     + ", ".join(f"{_esc(name.replace('_', ' '))} ×{count}" for name, count in actions))
    hours = profile.busiest_hours(3)
    if hours:
        parts.append("<b>Busiest</b>: " + ", ".join(f"{hour:02d}:00" for hour, _ in hours))
    aliases = profile.aliases()
    if aliases:
        parts.append("<b>Phrases</b>: " + ", ".join(f"“{_esc(p)}”" for p in list(aliases)[:5]))
    return "<br>".join(parts)


# ════════════════════════════════════════════════════════════════════════════
#  Routines
# ════════════════════════════════════════════════════════════════════════════

class RoutinesPanel(QWidget):
    """Record, keep, run and delete the multi-step things you do often."""

    def __init__(self, window: Any) -> None:
        super().__init__()
        self.window = window
        page, layout = _panel_column()

        self.record_card = Card("Teach me a routine", "recorded steps are stored in memory")
        self.record_hint = QLabel("Say “watch what I do”, perform the steps, then “save that as "
                                  "work session”. Or press start here.")
        self.record_hint.setObjectName("Body")
        self.record_hint.setWordWrap(True)
        self.record_card.add(self.record_hint)
        row = QHBoxLayout()
        row.setSpacing(8)
        self.start_button = QPushButton("● Start recording")
        self.start_button.setObjectName("Primary")
        self.start_button.clicked.connect(lambda: self.window.submit("watch what I do"))
        self.stop_button = QPushButton("■ Stop and save")
        self.stop_button.clicked.connect(lambda: self.window.submit("stop recording"))
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setObjectName("Chip")
        self.cancel_button.clicked.connect(lambda: self.window.submit("cancel the recording"))
        self.suggest_button = QPushButton("What have you noticed?")
        self.suggest_button.setObjectName("Chip")
        self.suggest_button.clicked.connect(lambda: self.window.submit("what have you noticed"))
        for button in (self.start_button, self.stop_button, self.cancel_button, self.suggest_button):
            row.addWidget(button)
        row.addStretch(1)
        self.record_card.body.addLayout(row)
        layout.addWidget(self.record_card)

        self.list_card = Card("Saved routines", "click one to run it")
        self.routine_list = QListWidget()
        self.routine_list.setMinimumHeight(200)
        self.routine_list.itemDoubleClicked.connect(self._run_item)
        self.list_card.add(self.routine_list, 1)
        self.list_card.add_action("Run", self._run_selected, primary=True)
        self.list_card.add_action("Delete", self._delete_selected)
        layout.addWidget(self.list_card, 1)

        self.suggest_card = Card("Suggestions", "patterns worth keeping")
        self.suggest_text = QLabel("—")
        self.suggest_text.setObjectName("Body")
        self.suggest_text.setWordWrap(True)
        self.suggest_card.add(self.suggest_text)
        layout.addWidget(self.suggest_card)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(_wrap_scroll(page))

    def refresh(self) -> None:
        core = getattr(self.window, "core", None)
        store = getattr(core, "routine_store", None)
        self.routine_list.clear()
        if store is None:
            self.list_card.set_title("Saved routines", "computer control is off")
            return
        routines = store.all()
        self.list_card.set_title("Saved routines", f"{len(routines)} in memory")
        for routine in routines:
            item = QListWidgetItem(f"{routine.name}   ·   {len(routine.steps)} steps"
                                   + (f"   ·   run {routine.times_run}×" if routine.times_run else ""))
            item.setData(Qt.ItemDataRole.UserRole, routine.name)
            item.setToolTip(routine.outline(core.actions) if core.actions else "")
            self.routine_list.addItem(item)
        if not routines:
            self.routine_list.addItem("No routines yet — record one, or run the same thing twice "
                                      "and I'll offer to save it.")

        recorder = getattr(core, "recorder", None)
        recording = bool(recorder is not None and getattr(recorder, "active", False))
        self.start_button.setEnabled(not recording)
        self.stop_button.setEnabled(recording)
        count = recorder.count() if recording and hasattr(recorder, "count") else 0
        self.record_card.set_title("Teach me a routine",
                                   f"● recording {count} step(s)" if recording else "recorded steps go to memory")
        self.record_hint.setText(
            "Recording — do the steps, then “stop recording” and “save that as <name>”."
            if recording else
            "Say “watch what I do”, perform the steps, then “save that as work session”. "
            "Or press start here.")
        self._refresh_suggestions()

    def _refresh_suggestions(self) -> None:
        core = getattr(self.window, "core", None)
        timeline = getattr(core, "timeline", None)
        store = getattr(core, "routine_store", None)
        if timeline is None or store is None:
            self.suggest_text.setText("Computer control is off.")
            return
        try:
            from ..routines import suggestions
            found = suggestions(timeline, store, max=4)
        except Exception:
            found = []
        if not found:
            self.suggest_text.setText("Nothing yet. Repeat a couple of steps and I'll notice.")
            return
        lines = []
        for suggestion in found:
            try:
                lines.append("• " + suggestion.describe(core.actions, limit=4))
            except Exception:
                lines.append("• " + str(getattr(suggestion, "name", "")))
        self.suggest_text.setText("\n".join(lines))

    def _run_item(self, item: QListWidgetItem) -> None:
        name = item.data(Qt.ItemDataRole.UserRole)
        if name:
            self.window.submit(f"run my {name}")

    def _run_selected(self) -> None:
        item = self.routine_list.currentItem()
        if item is not None:
            self._run_item(item)

    def _delete_selected(self) -> None:
        item = self.routine_list.currentItem()
        if item is None:
            return
        name = item.data(Qt.ItemDataRole.UserRole)
        if name:
            self.window.submit(f"delete routine {name}")


# ════════════════════════════════════════════════════════════════════════════
#  Actions: the palette of everything JARVIS can do to the machine
# ════════════════════════════════════════════════════════════════════════════

class ActionsPanel(QWidget):
    """A searchable palette of the whole control surface, plus the audit trail."""

    def __init__(self, window: Any) -> None:
        super().__init__()
        self.window = window
        page, layout = _panel_column()

        self.capability_card = Card("Capability", "what works on this machine")
        self.capability_text = QLabel("—")
        self.capability_text.setObjectName("Body")
        self.capability_text.setWordWrap(True)
        self.capability_card.add(self.capability_text)
        layout.addWidget(self.capability_card)

        self.palette_card = Card("Action palette", "click to fill the box, Enter to run")
        self.search = QLineEdit()
        self.search.setPlaceholderText("Filter 76 actions…  (try “volume”, “window”, “file”)")
        self.search.textChanged.connect(self.refresh_list)
        self.palette_card.add(self.search)
        self.action_list = QListWidget()
        self.action_list.setMinimumHeight(240)
        self.action_list.itemClicked.connect(self._pick)
        self.palette_card.add(self.action_list, 1)
        layout.addWidget(self.palette_card, 2)

        self.audit_card = Card("What I did", "every attempted action, newest last")
        self.audit_view = QTextBrowser()
        self.audit_view.setMinimumHeight(150)
        self.audit_card.add(self.audit_view, 1)
        self.audit_card.add_action("Refresh", self._refresh_audit)
        self.audit_card.add_action("Clear", self._clear_audit)
        layout.addWidget(self.audit_card, 1)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(_wrap_scroll(page))

    def refresh(self) -> None:
        core = getattr(self.window, "core", None)
        registry = getattr(core, "actions", None)
        controller = getattr(core, "controller", None)
        if registry is None:
            self.capability_text.setText("Computer control is switched off in Settings → Automation.")
            self.action_list.clear()
            return
        groups: dict[str, int] = {}
        for action in registry.actions.values():
            groups[action.group] = groups.get(action.group, 0) + 1
        summary = " · ".join(f"{name} {count}" for name, count in sorted(groups.items()))
        report = controller.report_text() if controller is not None else ""
        self.capability_card.set_title("Capability", f"{len(registry.actions)} actions")
        self.capability_text.setText(f"{summary}\n\n{report}")
        self.refresh_list()
        self._refresh_audit()

    def refresh_list(self) -> None:
        core = getattr(self.window, "core", None)
        registry = getattr(core, "actions", None)
        self.action_list.clear()
        if registry is None:
            return
        needle = self.search.text().strip().lower()
        for action in sorted(registry.actions.values(), key=lambda item: (item.group, item.name)):
            haystack = f"{action.name} {action.title} {action.description} {action.group}".lower()
            if needle and needle not in haystack:
                continue
            example = action.examples[0] if action.examples else action.title.lower()
            item = QListWidgetItem(f"[{action.risk}]  {action.title}  —  {action.description}")
            item.setData(Qt.ItemDataRole.UserRole, example)
            item.setToolTip(f"Say or type: “{example}”\nParameters: "
                            f"{', '.join(action.params) or 'none'}\nGroup: {action.group} · risk: {action.risk}")
            self.action_list.addItem(item)

    def _pick(self, item: QListWidgetItem) -> None:
        example = item.data(Qt.ItemDataRole.UserRole) or ""
        if not example:
            return
        self.window.goto_tab("conversation")
        self.window.input.setText(example)
        self.window.input.setFocus()
        self.window.input.selectAll()

    def _refresh_audit(self) -> None:
        core = getattr(self.window, "core", None)
        audit = getattr(core, "audit", None)
        if audit is None:
            self.audit_view.setHtml("<span style='color:#7d90b3'>No audit log.</span>")
            return
        entries = audit.tail(40)
        if not entries:
            self.audit_view.setHtml(
                "<span style='color:#7d90b3'>Empty. Every action shows up here with its arguments, "
                "risk level and outcome.</span>")
            return
        marks = {"done": ("✓", "#34d399"), "auto": ("·", "#7d90b3"), "failed": ("✗", "#f87171"),
                 "refused": ("⛔", "#fbbf24"), "declined": ("✗", "#f87171"),
                 "awaiting_voice_confirmation": ("…", "#7d90b3")}
        rows = []
        for entry in reversed(entries[-30:]):
            mark, colour = marks.get(str(entry.get("outcome")), ("·", "#7d90b3"))
            args = ", ".join(f"{key}={value}" for key, value in list((entry.get("args") or {}).items())[:3])
            when = str(entry.get("when", ""))[11:19]
            extra = " · learned" if entry.get("learned") else ""
            rows.append(
                f"<tr><td style='color:#7d90b3'>{when}</td>"
                f"<td style='color:{colour}'>{mark}</td>"
                f"<td><b>{_esc(entry.get('action'))}</b> "
                f"<span style='color:#7d90b3'>{_esc('(' + args + ')' if args else '')} "
                f"[{_esc(entry.get('risk'))}{extra}]</span><br>"
                f"<span style='color:#7d90b3'>{_esc(str(entry.get('message', ''))[:110])}</span></td></tr>")
        self.audit_view.setHtml("<table cellspacing='0' cellpadding='3'>" + "".join(rows) + "</table>")

    def _clear_audit(self) -> None:
        core = getattr(self.window, "core", None)
        audit = getattr(core, "audit", None)
        if audit is not None:
            audit.clear()
        self._refresh_audit()


# ════════════════════════════════════════════════════════════════════════════
#  Learning: the behaviour profile
# ════════════════════════════════════════════════════════════════════════════

class LearningPanel(QWidget):
    """What JARVIS has learned, and the switches that control it."""

    def __init__(self, window: Any) -> None:
        super().__init__()
        self.window = window
        page, layout = _panel_column()

        self.summary_card = Card("What I know about you", "kept on this machine only")
        self.summary_text = QLabel("—")
        self.summary_text.setObjectName("Body")
        self.summary_text.setWordWrap(True)
        self.summary_card.add(self.summary_text)
        layout.addWidget(self.summary_card)

        self.hours_card = Card("When you work", "activity by hour")
        self.hours_row = QHBoxLayout()
        self.hours_row.setSpacing(3)
        self.hours_labels: list[QLabel] = []
        for hour in range(24):
            label = QLabel(f"{hour:02d}")
            label.setObjectName("HourCell")
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            label.setFixedHeight(30)
            label.setToolTip(f"{hour:02d}:00")
            self.hours_labels.append(label)
            self.hours_row.addWidget(label)
        self.hours_card.body.addLayout(self.hours_row)
        layout.addWidget(self.hours_card)

        row = QHBoxLayout()
        row.setSpacing(10)
        self.alias_card = Card("Your phrases", "taught or picked up")
        self.alias_list = QListWidget()
        self.alias_list.setMinimumHeight(140)
        self.alias_card.add(self.alias_list, 1)
        alias_row = self.alias_card.add_row()
        self.alias_input = QLineEdit()
        self.alias_input.setPlaceholderText("teach: chill = open spotify, then mute")
        alias_row.addWidget(self.alias_input, 1)
        teach = QPushButton("Teach")
        teach.setObjectName("Primary")
        teach.clicked.connect(self._teach)
        alias_row.addWidget(teach)
        self.alias_card.add_action("Forget", self._forget_selected)
        row.addWidget(self.alias_card, 3)

        self.trust_card = Card("Learned approvals", "things I stopped asking about")
        self.trust_text = QLabel("—")
        self.trust_text.setObjectName("Body")
        self.trust_text.setWordWrap(True)
        self.trust_card.add(self.trust_text)
        self.trust_card.add_action("Reset", self._reset)
        row.addWidget(self.trust_card, 2)
        layout.addLayout(row, 1)

        self.switches_card = Card("Learning switches", "tune how quickly I adapt")
        grid = QGridLayout()
        grid.setSpacing(6)
        self.switches: dict[str, QCheckBox] = {}
        for key, label in (
            ("learn_habits", "Learn from what I do (phrases, aliases, habits)"),
            ("learned_trust", "Stop confirming ordinary actions I always approve"),
            ("ritual_suggestions", "Suggest routines from time-of-day patterns"),
            ("stream_replies", "Stream answers as they arrive"),
            ("response_cache", "Cache identical questions (instant repeats)"),
            ("free_fallback", "Fall back through other free providers automatically"),
        ):
            box = QCheckBox(label)
            box.toggled.connect(lambda state, name=key: self._toggle(name, state))
            self.switches[key] = box
            grid.addWidget(box, len(self.switches) // 2, len(self.switches) % 2)
        self.switches_card.body.addLayout(grid)
        layout.addWidget(self.switches_card)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(_wrap_scroll(page))

    # ── refresh ──────────────────────────────────────────────────────────
    def refresh(self) -> None:
        core = getattr(self.window, "core", None)
        profile = getattr(core, "profile", None)
        settings = self.window.settings
        for key, box in self.switches.items():
            box.blockSignals(True)
            box.setChecked(bool(settings.get(key, True)))
            box.blockSignals(False)
        if profile is None:
            return
        stats = profile.stats()
        self.summary_card.set_title("What I know about you",
                                    f"{stats['days_used']} day(s) · {stats['sessions']} session(s)")
        self.summary_text.setText(_esc(profile.summary()).replace("\n", "<br>"))

        hours = dict(profile.busiest_hours(24))
        peak = max(hours.values()) if hours else 0
        colours = accent_colors(str(settings.get("accent", "cyan")))
        for hour, label in enumerate(self.hours_labels):
            count = int(hours.get(hour, 0))
            intensity = 0.0 if not peak else count / peak
            background = "transparent" if count == 0 else colours["accent"]
            alpha = max(0.16, min(0.9, intensity))
            label.setStyleSheet(
                f"#HourCell {{ background: {background};"
                f" border-radius: 5px; color: "
                f"{'#04121b' if alpha > 0.5 else '#dce7ff'};"
                f" border: 1px solid rgba(34,211,238,{alpha:.2f}); }}")
            label.setToolTip(f"{hour:02d}:00 — {count} action(s)")

        self.alias_list.clear()
        aliases = profile.aliases()
        for phrase, info in sorted(aliases.items()):
            target = (f"routine “{info['routine']}”" if info.get("routine")
                      else str(info.get("action", "")).replace("_", " "))
            origin = "taught" if info.get("taught") else "learned"
            item = QListWidgetItem(f"“{phrase}” → {target}   ({origin}, {info.get('hits', 0)} uses)")
            item.setData(Qt.ItemDataRole.UserRole, phrase)
            self.alias_list.addItem(item)
        if not aliases:
            self.alias_list.addItem("None yet — say a nickname and then the real command, "
                                    "or teach one with the box below.")

        trusted = profile.trust_candidates()
        self.trust_text.setText(
            "\n".join(f"• {name.replace('_', ' ')} — approved every time"
                      for name in trusted) if trusted
            else "Nothing yet. After you approve the same ordinary action a few times without "
                 "ever saying no, I stop asking — destructive actions always ask.")

    # ── actions ──────────────────────────────────────────────────────────
    def _teach(self) -> None:
        text = self.alias_input.text().strip()
        if not text:
            return
        self.alias_input.clear()
        self.window.submit(f"/teach {text}")

    def _forget_selected(self) -> None:
        item = self.alias_list.currentItem()
        if item is None:
            return
        phrase = item.data(Qt.ItemDataRole.UserRole)
        if phrase:
            self.window.submit(f"/forget {phrase}")

    def _reset(self) -> None:
        self.window.submit("/profile reset")

    def _toggle(self, key: str, state: bool) -> None:
        self.window.save_setting(key, bool(state))


# ════════════════════════════════════════════════════════════════════════════
#  ⌘K palette
# ════════════════════════════════════════════════════════════════════════════

class CommandPalette(QDialog):
    """One box that can reach every skill, action, routine, tab and setting."""

    def __init__(self, window: Any, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.window = window
        self.setObjectName("Palette")
        self.setWindowTitle("Command palette")
        self.setMinimumWidth(620)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 12)
        layout.setSpacing(8)

        self.field = QLineEdit()
        self.field.setPlaceholderText("Type a command, skill, action, routine or setting…")
        self.field.textChanged.connect(self.refill)
        self.field.returnPressed.connect(self.run_current)
        layout.addWidget(self.field)

        self.results = QListWidget()
        self.results.setMinimumHeight(320)
        self.results.itemActivated.connect(lambda _item: self.run_current())
        self.results.itemClicked.connect(lambda _item: self.run_current())
        layout.addWidget(self.results, 1)

        hint = QLabel("Enter runs the highlighted row · Esc closes · Ctrl+K opens this anywhere")
        hint.setObjectName("Hint")
        layout.addWidget(hint)
        self.entries: list[tuple[str, str, str]] = []

    # ── content ──────────────────────────────────────────────────────────
    def collect(self) -> list[tuple[str, str, str]]:
        entries: list[tuple[str, str, str]] = []
        core = getattr(self.window, "core", None)

        for label in ("dashboard", "conversation", "routines", "actions", "learning",
                      "skills", "memory", "settings"):
            entries.append((f"Go to {label}", "tab", label))

        for command, blurb in (
            ("/help", "every skill"),
            ("/actions", "what I can do to the computer"),
            ("/audit", "the action log"),
            ("/routines", "saved routines"),
            ("/profile", "what I learned about you"),
            ("/cloud", "free model providers"),
            ("/aliases", "your taught phrases"),
            ("/stats", "usage numbers"),
            ("/brief", "morning briefing"),
            ("/voice on", "spoken replies on"),
            ("/voice off", "spoken replies off"),
        ):
            entries.append((f"{command}  —  {blurb}", "command", command))

        if core is not None:
            for skill in sorted(core.registry.skills, key=lambda item: item.title):
                example = skill.examples[0] if skill.examples else ""
                if example:
                    entries.append((f"Ask: “{example}”  —  {skill.title}", "command", example))
            registry = getattr(core, "actions", None)
            if registry is not None:
                for action in sorted(registry.actions.values(), key=lambda item: item.name):
                    example = action.examples[0] if action.examples else action.title.lower()
                    entries.append((f"Action: {action.title}  [{action.risk}]  —  “{example}”",
                                    "command", example))
            store = getattr(core, "routine_store", None)
            if store is not None:
                for routine in store.all():
                    entries.append((f"Run routine: {routine.name}  ({len(routine.steps)} steps)",
                                    "command", f"run my {routine.name}"))
            profile = getattr(core, "profile", None)
            if profile is not None:
                for phrase, info in profile.aliases().items():
                    target = info.get("routine") or info.get("action") or ""
                    entries.append((f"Alias: “{phrase}” → {target}", "command", phrase))
            for name in PROVIDER_LABELS:
                entries.append((f"Provider: {PROVIDER_LABELS[name]}", "command", f"/provider {name}"))
        return entries

    def open(self) -> None:
        self.entries = self.collect()
        self.field.clear()
        self.refill("")
        self.field.setFocus()
        self.exec()

    def refill(self, needle: str) -> None:
        needle = (needle or "").strip().lower()
        self.results.clear()
        scored: list[tuple[int, str, str, str]] = []
        for label, kind, payload in self.entries:
            score = _fuzzy(needle, label.lower())
            if score is None:
                continue
            scored.append((score, label, kind, payload))
        scored.sort(key=lambda item: item[0])
        for _score, label, kind, payload in scored[:200]:
            item = QListWidgetItem(f"{label}   ·   {kind}")
            item.setData(Qt.ItemDataRole.UserRole, payload)
            item.setData(Qt.ItemDataRole.UserRole + 1, kind)
            self.results.addItem(item)
        if self.results.count():
            self.results.setCurrentRow(0)

    def run_current(self) -> None:
        item = self.results.currentItem()
        if item is None:
            return
        payload = item.data(Qt.ItemDataRole.UserRole)
        kind = item.data(Qt.ItemDataRole.UserRole + 1)
        self.accept()
        if kind == "tab":
            self.window.goto_tab(str(payload))
            return
        self.window.goto_tab("conversation")
        self.window.submit(str(payload))


def _fuzzy(needle: str, haystack: str) -> int | None:
    """Cheap subsequence score: lower is better, ``None`` means no match."""
    if not needle:
        return 0
    if needle in haystack:
        return haystack.index(needle)
    position = 0
    gaps = 0
    for char in needle:
        found = haystack.find(char, position)
        if found < 0:
            return None
        gaps += found - position
        position = found + 1
    return 100 + gaps
