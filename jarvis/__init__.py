"""JARVIS — a local-first personal assistant (voice, skills, code intelligence)."""

from __future__ import annotations

__version__ = "1.0.0"

from .config import APP_NAME, VERSION, Settings, jarvis_home
from .core import Core, Response
from .host import HeadlessHost, Host
from .memory import Memory

__all__ = [
    "APP_NAME",
    "VERSION",
    "Core",
    "HeadlessHost",
    "Host",
    "Memory",
    "Response",
    "Settings",
    "__version__",
    "jarvis_home",
]
