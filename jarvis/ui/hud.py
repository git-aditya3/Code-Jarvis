"""
The arc-reactor style HUD.

Pure QPainter drawing on a ~30 fps timer: counter-rotating rings, tick marks, a
pulsing core and a level meter that reacts to microphone input. The widget is
driven by :meth:`HudWidget.set_state` and :meth:`HudWidget.set_level` — no Qt
widgets inside, so it stays cheap to repaint and easy to reuse behind a splash
screen later.
"""

from __future__ import annotations

import math
import time

from PyQt6.QtCore import QPointF, QRectF, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QConicalGradient, QFont, QPainter, QPen, QRadialGradient
from PyQt6.QtWidgets import QSizePolicy, QWidget

from .theme import accent_colors

STATE_LABELS = {
    "idle": "STANDING BY",
    "listening": "LISTENING",
    "capturing": "LISTENING",
    "thinking": "PROCESSING",
    "speaking": "SPEAKING",
    "error": "ATTENTION",
}

STATE_COLORS = {
    "idle": "#5b7396",
    "listening": "#34d399",
    "capturing": "#34d399",
    "thinking": "#fbbf24",
    "speaking": "#22d3ee",
    "error": "#f87171",
}


class HudWidget(QWidget):
    """Animated reactor core."""

    clicked = pyqtSignal()

    def __init__(self, accent: str = "cyan", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumSize(220, 220)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.accent_name = accent
        self.state = "idle"
        self.level = 0.0
        self._display_level = 0.0
        self._angle = 0.0
        self._ticks = 0.0
        self._pulse = 0.0
        self._last = time.time()
        self._fps_timer = QTimer(self)
        self._fps_timer.timeout.connect(self._tick)
        self._fps_timer.start(33)

    # ── public API ───────────────────────────────────────────────────────
    def set_state(self, state: str) -> None:
        state = state if state in STATE_LABELS else "idle"
        if state != self.state:
            self.state = state
            if state in {"thinking", "error"}:
                self._pulse = 1.0
            self.update()

    def set_level(self, level: float) -> None:
        """0..1 microphone level (drives the meter and core brightness)."""
        self.level = max(0.0, min(1.0, float(level)))

    def set_accent(self, accent: str) -> None:
        self.accent_name = accent
        self.update()

    def _accent(self) -> QColor:
        return QColor(accent_colors(self.accent_name)["accent"])

    def state_label(self) -> str:
        return STATE_LABELS.get(self.state, "STANDING BY")

    def state_color(self) -> str:
        return STATE_COLORS.get(self.state, "#5b7396")

    # ── animation ────────────────────────────────────────────────────────
    def _tick(self) -> None:
        now = time.time()
        delta = min(0.1, now - self._last)
        self._last = now

        speed = {"idle": 12.0, "listening": 46.0, "capturing": 46.0,
                 "thinking": 90.0, "speaking": 60.0, "error": 30.0}.get(self.state, 12.0)
        self._angle = (self._angle + speed * delta) % 360
        self._ticks = (self._ticks + speed * 0.6 * delta) % 360
        self._pulse = max(0.0, self._pulse - delta * 1.6)

        target = self.level if self.state in {"listening", "capturing", "speaking"} else 0.0
        self._display_level += (target - self._display_level) * min(1.0, delta * 9.0)
        self.update()

    # ── painting ─────────────────────────────────────────────────────────
    def paintEvent(self, event) -> None:  # noqa: N802 (Qt naming)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        side = min(self.width(), self.height())
        if side < 40:
            return
        centre = QPointF(self.width() / 2, self.height() / 2)
        painter.translate(centre)

        accent = self._accent()
        state_colour = QColor(self.state_color())
        radius = side / 2 - 6
        level = self._display_level
        breathe = 1.0 + 0.02 * math.sin(time.time() * 1.4)
        pulse = self._pulse

        # outer glow
        glow = QRadialGradient(QPointF(0, 0), radius * 1.15)
        glow.setColorAt(0.55, QColor(accent.red(), accent.green(), accent.blue(), 26 + int(60 * level)))
        glow.setColorAt(1.0, QColor(accent.red(), accent.green(), accent.blue(), 0))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(glow)
        painter.drawEllipse(QPointF(0, 0), radius * 1.15, radius * 1.15)

        # tick ring
        painter.save()
        painter.rotate(self._ticks)
        tick_pen = QPen(QColor(accent.red(), accent.green(), accent.blue(), 120))
        tick_pen.setWidthF(1.4)
        painter.setPen(tick_pen)
        for index in range(72):
            painter.save()
            painter.rotate(index * 5)
            length = radius * (0.10 if index % 6 else 0.16)
            alpha = 200 if index % 6 == 0 else 90
            colour = QColor(accent)
            colour.setAlpha(alpha)
            pen = QPen(colour)
            pen.setWidthF(1.8 if index % 6 == 0 else 1.0)
            painter.setPen(pen)
            painter.drawLine(QPointF(radius * 0.86, 0), QPointF(radius * 0.86 - length, 0))
            painter.restore()
        painter.restore()

        # dashed outer ring (counter-rotation)
        painter.save()
        painter.rotate(-self._angle * 0.7)
        ring_colour = QColor(state_colour)
        ring_colour.setAlpha(190)
        pen = QPen(ring_colour)
        pen.setWidthF(2.4)
        pen.setDashPattern([6, 5, 15, 5])
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawEllipse(QPointF(0, 0), radius * 0.78, radius * 0.78)
        painter.restore()

        # segmented arc ring
        painter.save()
        painter.rotate(self._angle)
        arc_pen = QPen(QColor(accent.red(), accent.green(), accent.blue(), 230))
        arc_pen.setWidthF(3.0)
        arc_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(arc_pen)
        segments = 3
        for index in range(segments):
            painter.drawArc(
                QRectF(-radius * 0.63, -radius * 0.63, radius * 1.26, radius * 1.26),
                int(index * (360 / segments) * 16),
                int((360 / segments - 26) * 16),
            )
        painter.restore()

        # level meter: bars around the core sway with the microphone
        bars = 48
        painter.save()
        for index in range(bars):
            angle = index * (360 / bars)
            amplitude = level * radius * 0.30 * (
                0.35 + 0.65 * abs(math.sin(index * 0.9 + time.time() * 6.0))
            )
            if amplitude < 0.6:
                continue
            painter.save()
            painter.rotate(angle)
            colour = QColor(accent)
            colour.setAlpha(int(120 + 120 * level))
            pen = QPen(colour)
            pen.setWidthF(2.0)
            painter.setPen(pen)
            painter.drawLine(QPointF(radius * 0.36, 0), QPointF(radius * 0.36 + amplitude, 0))
            painter.restore()
        painter.restore()

        # core
        core_radius = radius * 0.30 * breathe * (1.0 + 0.10 * level + 0.12 * pulse)
        core = QRadialGradient(QPointF(0, 0), core_radius)
        core.setColorAt(0.0, QColor(255, 255, 255, 235))
        core.setColorAt(0.35, QColor(state_colour.red(), state_colour.green(), state_colour.blue(), 220))
        core.setColorAt(1.0, QColor(state_colour.red(), state_colour.green(), state_colour.blue(), 40))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(core)
        painter.drawEllipse(QPointF(0, 0), core_radius, core_radius)

        # spinning highlight inside the core
        conical = QConicalGradient(QPointF(0, 0), -self._angle * 2)
        conical.setColorAt(0.0, QColor(255, 255, 255, 180))
        conical.setColorAt(0.5, QColor(255, 255, 255, 0))
        conical.setColorAt(1.0, QColor(255, 255, 255, 180))
        painter.setBrush(conical)
        painter.drawEllipse(QPointF(0, 0), core_radius * 0.62, core_radius * 0.62)

        # state text
        painter.resetTransform()
        painter.translate(centre)
        font = QFont(self.font())
        font.setPointSizeF(max(8.0, side * 0.038))
        font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 2.0)
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(QColor(self.state_color()))
        label = self.state_label()
        metrics = painter.fontMetrics()
        width = metrics.horizontalAdvance(label)
        baseline = radius * 0.60 + metrics.height() / 2
        pill = QRectF(-width / 2 - 10, baseline - metrics.height() + 2, width + 20, metrics.height() + 6)
        backdrop = QColor(5, 7, 13, 205)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(backdrop)
        painter.drawRoundedRect(pill, pill.height() / 2, pill.height() / 2)
        painter.setPen(QColor(self.state_color()))
        painter.drawText(QPointF(-width / 2, baseline), label)
        painter.end()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)
