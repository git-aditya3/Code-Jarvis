"""Qt interface for JARVIS (imported only when the desktop app runs)."""

from __future__ import annotations

__all__ = ["HudWidget", "JarvisWindow", "app_icon"]


def __getattr__(name: str):
    """Import Qt lazily so ``import jarvis.ui`` never fails without PyQt6."""
    if name in {"JarvisWindow", "app_icon"}:
        from .main_window import JarvisWindow, app_icon

        return {"JarvisWindow": JarvisWindow, "app_icon": app_icon}[name]
    if name == "HudWidget":
        from .hud import HudWidget

        return HudWidget
    raise AttributeError(name)
