"""
JARVIS configuration & local storage paths.

Everything the assistant persists lives in a single directory (``~/.jarvis`` by
default, override with the ``JARVIS_HOME`` environment variable):

    settings.json   user preferences (safe to edit by hand)
    memory.json     notes / tasks / facts the assistant remembers
    history.json    recent conversation turns
    diagrams/       generated flowcharts
    synthetic/      generated datasets
    screenshots/    captured screenshots

Secrets are never hard-coded. API keys are read from the environment first and
fall back to ``settings.json`` (which is written with 0600 permissions).
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

APP_NAME = "JARVIS"
APP_TAGLINE = "Just A Rather Very Intelligent System"
VERSION = "1.0.0"

# Environment variables checked for each provider's key.
ENV_KEYS = {
    "groq": ("GROQ_API_KEY", "JARVIS_GROQ_API_KEY"),
    "openai": ("OPENAI_API_KEY", "JARVIS_OPENAI_API_KEY"),
}

PROVIDER_LABELS = {
    "offline": "Offline skills (no key needed)",
    "groq": "Groq Cloud (free tier available)",
    "openai": "OpenAI",
    "ollama": "Ollama (local, private)",
}

PROVIDER_MODELS = {
    "groq": ["llama-3.3-70b-versatile", "llama-3.1-8b-instant", "mixtral-8x7b-32768"],
    "openai": ["gpt-4o-mini", "gpt-4o", "gpt-4.1-mini"],
    "ollama": ["llama3.1", "qwen2.5", "codellama", "mistral"],
}

DEFAULT_SETTINGS: dict[str, Any] = {
    # identity
    "user_name": "",
    "city": "Tiruchirappalli",
    "units": "metric",              # metric | imperial
    # brain
    "provider": "offline",          # offline | groq | openai | ollama
    "models": dict(PROVIDER_MODELS),
    "ollama_url": "http://localhost:11434",
    "api_keys": {},                 # {provider: key}  (0600 on disk)
    "max_tokens": 900,
    "temperature": 0.3,
    # voice
    "voice_replies": True,
    "wake_word_enabled": False,
    "wake_word": "jarvis",
    "tts_backend": "auto",          # auto | pyttsx3 | qt | system | none
    "tts_voice": "",
    "tts_rate": 175,
    "stt_backend": "auto",          # auto | faster-whisper | vosk | google
    "whisper_model": "base",        # tiny | base | small
    "vosk_model_path": "",
    "mic_index": None,
    "silence_seconds": 1.1,
    # automation & control
    "trust_level": "ask_risky",     # ask_all | ask_risky | trusted
    "allow_dangerous": False,       # destructive actions (delete, kill, power) need this on
    "dry_run": False,               # rehearse actions without touching the machine
    "use_trash": True,              # prefer the recycle bin over permanent deletion
    "routine_watch": True,          # notice repeated commands and offer to save them
    "routine_suggest_after": 2,     # offer after this many repeats
    "routine_autosave": False,      # save without asking (off by default, on purpose)
    "voice_confirm": False,         # ask for risky actions out loud ("say yes to continue")
    "routine_log_limit": 200,       # remembered commands kept for pattern spotting
    "shell_timeout": 20,            # seconds before a command is abandoned
    # interface
    "always_on_top": True,
    "opacity": 0.97,
    "accent": "cyan",               # cyan | amber | violet | emerald
    "start_minimised": False,
    "connect_on_launch": True,
}


def jarvis_home() -> Path:
    """Return (and create) the assistant's data directory."""
    root = Path(os.environ.get("JARVIS_HOME", Path.home() / ".jarvis")).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    return root


def path_for(name: str) -> Path:
    return jarvis_home() / name


def _write_json(path: Path, payload: Any, private: bool = False) -> None:
    """Atomically write JSON so a crash can never truncate the user's data."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, ensure_ascii=False)
        if private:
            os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _read_json(path: Path, default: Any) -> Any:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


class Settings:
    """Dict-like preferences with defaults, validation and atomic saving."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or path_for("settings.json")
        self._data = dict(DEFAULT_SETTINGS)
        stored = _read_json(self.path, {})
        if isinstance(stored, dict):
            self._data.update(stored)
        # nested dicts need merging rather than replacing
        models = dict(PROVIDER_MODELS)
        models.update(self._data.get("models") or {})
        self._data["models"] = models
        if not isinstance(self._data.get("api_keys"), dict):
            self._data["api_keys"] = {}

    # ── dict interface ───────────────────────────────────────────────────
    def __getitem__(self, key: str) -> Any:
        return self._data.get(key, DEFAULT_SETTINGS.get(key))

    def __setitem__(self, key: str, value: Any) -> None:
        self._data[key] = value

    def get(self, key: str, default: Any = None) -> Any:
        value = self._data.get(key, DEFAULT_SETTINGS.get(key, default))
        return default if value is None else value

    def update(self, **kwargs: Any) -> None:
        self._data.update(kwargs)

    def as_dict(self) -> dict[str, Any]:
        return dict(self._data)

    def reset(self) -> None:
        keys = dict(self._data.get("api_keys") or {})
        self._data = dict(DEFAULT_SETTINGS)
        self._data["api_keys"] = keys
        self._data["models"] = dict(PROVIDER_MODELS)

    def save(self) -> None:
        _write_json(self.path, self._data, private=True)

    # ── providers ────────────────────────────────────────────────────────
    @property
    def provider(self) -> str:
        return str(self._data.get("provider") or "offline").lower()

    def model_for(self, provider: str | None = None) -> str:
        provider = (provider or self.provider).lower()
        models = self._data.get("models") or {}
        model = models.get(provider)
        if model:
            return str(model)
        chain = PROVIDER_MODELS.get(provider) or ["llama3.1"]
        return chain[0]

    def api_key(self, provider: str | None = None) -> str:
        """Environment wins over the saved value; never raises."""
        provider = (provider or self.provider).lower()
        for env_name in ENV_KEYS.get(provider, ()):
            value = os.environ.get(env_name, "").strip()
            if value:
                return value
        key = (self._data.get("api_keys") or {}).get(provider, "")
        return str(key).strip()

    def set_api_key(self, provider: str, key: str) -> None:
        keys = dict(self._data.get("api_keys") or {})
        key = (key or "").strip()
        if key:
            keys[provider] = key
        else:
            keys.pop(provider, None)
        self._data["api_keys"] = keys

    def key_source(self, provider: str | None = None) -> str:
        """Where a provider's key comes from: 'env', 'settings' or ''."""
        provider = (provider or self.provider).lower()
        for env_name in ENV_KEYS.get(provider, ()):
            if os.environ.get(env_name, "").strip():
                return f"env:${env_name}"
        if (self._data.get("api_keys") or {}).get(provider):
            return "settings.json"
        return ""

    def has_key(self, provider: str | None = None) -> bool:
        provider = (provider or self.provider).lower()
        if provider == "ollama":
            return True
        return bool(self.api_key(provider))
