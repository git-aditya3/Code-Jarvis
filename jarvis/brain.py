"""
The "brain" layer: pluggable LLM providers behind one tiny interface.

JARVIS works with **no key at all** (the offline skill router answers a large
set of everyday requests). Add a key for Groq, OpenAI, or point it at a local
Ollama server and the same interface starts handling open-ended questions.

No credentials are ever baked into this file — keys come from the environment
or from ``~/.jarvis/settings.json`` (see :mod:`jarvis.config`).
"""

from __future__ import annotations

import json
import platform
import time
from dataclasses import dataclass, field
from typing import Any

try:  # requests is the only third-party dependency of the brain
    import requests
except Exception:  # pragma: no cover - requests is in requirements.txt
    requests = None  # type: ignore[assignment]

from .config import ENV_KEYS, PROVIDER_LABELS, VERSION, Settings

REQUEST_TIMEOUT = 60

PERSONA = (
    "You are JARVIS, a calm, precise personal assistant running on the user's own computer. "
    "You are helpful, brief and a little dry in tone, like a trusted chief of staff. "
    "Answer in the user's language. Prefer short paragraphs and bullet lists over long prose. "
    "Use fenced code blocks for code. Never invent facts: if you are unsure, say so plainly "
    "and suggest how the user can find out."
)


@dataclass
class Reply:
    """Result of asking the brain for an answer."""

    text: str
    provider: str = "offline"
    ok: bool = True
    error: str = ""
    model: str = ""
    elapsed: float = 0.0
    meta: dict[str, Any] = field(default_factory=dict)


class BrainError(Exception):
    """Raised internally when a provider cannot answer."""


class Brain:
    """Route prompts to Groq / OpenAI / Ollama, with human-readable failures."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    # ── capability ───────────────────────────────────────────────────────
    @property
    def provider(self) -> str:
        return self.settings.provider

    @property
    def enabled(self) -> bool:
        """True when an LLM (not the offline router) should handle questions."""
        return self.provider != "offline"

    @property
    def ready(self) -> bool:
        if not self.enabled:
            return False
        if self.provider == "ollama":
            return self._ollama_reachable()
        return bool(self.settings.api_key(self.provider))

    def status(self) -> str:
        if not self.enabled:
            return "Offline skills only — add a key in Settings for open-ended questions."
        if self.provider == "ollama":
            return "Ollama: reachable" if self._ollama_reachable() else "Ollama: not reachable"
        source = self.settings.key_source(self.provider)
        if source:
            return f"{PROVIDER_LABELS.get(self.provider, self.provider)} — key from {source}"
        return f"{PROVIDER_LABELS.get(self.provider, self.provider)} — no key configured"

    def describe(self) -> dict[str, str]:
        return {
            "provider": self.provider,
            "model": self.settings.model_for(),
            "status": self.status(),
            "key_source": self.settings.key_source(self.provider),
        }

    # ── context helpers ──────────────────────────────────────────────────
    def system_prompt(self, memory_facts: dict[str, str] | None = None, extra: str = "") -> str:
        now = time.strftime("%A, %d %B %Y, %H:%M")
        bits = [PERSONA, f"Current local time: {now} ({time.tzname[0]})."]
        name = self.settings.get("user_name")
        if name:
            bits.append(f"The user's name is {name}.")
        city = self.settings.get("city")
        if city:
            bits.append(f"The user is in {city}. Use metric units." if self.settings.get("units") == "metric"
                        else f"The user is in {city}. Use imperial units.")
        bits.append(
            "The user's machine: "
            f"{platform.system()} {platform.release()} running Python {platform.python_version()}."
        )
        if memory_facts:
            facts = "; ".join(f"{k} = {v}" for k, v in list(memory_facts.items())[:40])
            bits.append(
                "Durable facts the user asked you to remember (trust these): "
                f"{facts}"
            )
        bits.append(f"JARVIS app version {VERSION}.")
        if extra:
            bits.append(extra)
        return "\n".join(bits)

    def build_messages(
        self,
        question: str,
        history: list[dict[str, Any]] | None = None,
        facts: dict[str, str] | None = None,
        extra_system: str = "",
        max_turns: int = 8,
    ) -> list[dict[str, str]]:
        messages: list[dict[str, str]] = [{"role": "system", "content": self.system_prompt(facts, extra_system)}]
        for turn in (history or [])[-max_turns:]:
            role = "assistant" if turn.get("role") == "jarvis" else "user"
            text = str(turn.get("text", "")).strip()
            if text:
                messages.append({"role": role, "content": text[:4000]})
        messages.append({"role": "user", "content": question})
        return messages

    # ── the one method the rest of the app uses ──────────────────────────
    def ask(
        self,
        question: str,
        history: list[dict[str, Any]] | None = None,
        facts: dict[str, str] | None = None,
        extra_system: str = "",
    ) -> Reply:
        if not self.enabled:
            return Reply(
                text="",
                provider="offline",
                ok=False,
                error="No language model is configured.",
            )
        if requests is None:
            return Reply(text="", provider=self.provider, ok=False,
                         error="The 'requests' package is missing — run: pip install requests")
        if self.provider != "ollama" and not self.settings.api_key(self.provider):
            return Reply(
                text="", provider=self.provider, ok=False,
                error=(f"No API key is configured for {self.provider}. Open Settings and paste one, or set "
                       f"${ENV_KEYS.get(self.provider, ('GROQ_API_KEY',))[0]} in your environment."),
            )
        messages = self.build_messages(question, history, facts, extra_system)
        started = time.time()
        try:
            if self.provider == "ollama":
                text, model = self._ask_ollama(messages)
            else:
                text, model = self._ask_openai_compatible(messages)
        except BrainError as exc:
            return Reply(text="", provider=self.provider, ok=False, error=str(exc),
                         elapsed=time.time() - started)
        return Reply(
            text=text.strip(),
            provider=self.provider,
            ok=bool(text.strip()),
            error="" if text.strip() else "The model returned an empty answer.",
            model=model,
            elapsed=time.time() - started,
        )

    # ── providers ────────────────────────────────────────────────────────
    def _endpoint(self) -> tuple[str, dict[str, str]]:
        provider = self.provider
        key = self.settings.api_key(provider)
        if provider == "groq":
            return (
                "https://api.groq.com/openai/v1/chat/completions",
                {"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            )
        if provider == "openai":
            return (
                "https://api.openai.com/v1/chat/completions",
                {"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            )
        raise BrainError(f"Unknown provider: {provider}")

    def _ask_openai_compatible(self, messages: list[dict[str, str]]) -> tuple[str, str]:
        url, headers = self._endpoint()
        model = self.settings.model_for()
        payload = {
            "model": model,
            "messages": messages,
            "max_tokens": int(self.settings.get("max_tokens", 900)),
            "temperature": float(self.settings.get("temperature", 0.3)),
        }
        try:
            response = requests.post(url, headers=headers, json=payload, timeout=REQUEST_TIMEOUT)
        except requests.exceptions.Timeout:
            raise BrainError("The request timed out. Check your connection and try again.") from None
        except requests.exceptions.RequestException as exc:
            raise BrainError(f"Could not reach {self.provider}: {exc}") from None

        if response.status_code in (401, 403):
            raise BrainError(
                f"{self.provider} rejected the API key (HTTP {response.status_code}). "
                "Open Settings and paste a fresh key, or set the environment variable."
            )
        if response.status_code == 429:
            raise BrainError(
                f"{self.provider} is rate-limiting this key (HTTP 429). Wait a moment or switch models."
            )
        if response.status_code >= 400:
            detail = ""
            try:
                detail = response.json().get("error", {}).get("message", "")
            except Exception:
                detail = response.text[:200]
            raise BrainError(f"{self.provider} error {response.status_code}: {detail}")

        try:
            data = response.json()
            text = data["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError):
            raise BrainError(f"Unexpected reply from {self.provider}.") from None
        return str(text), model

    def _ollama_base(self) -> str:
        return str(self.settings.get("ollama_url") or "http://localhost:11434").rstrip("/")

    def _ollama_reachable(self) -> bool:
        if requests is None:
            return False
        try:
            response = requests.get(f"{self._ollama_base()}/api/tags", timeout=2.5)
            return response.status_code == 200
        except requests.exceptions.RequestException:
            return False

    def _ask_ollama(self, messages: list[dict[str, str]]) -> tuple[str, str]:
        model = self.settings.model_for("ollama")
        payload = {
            "model": model,
            "messages": messages,
            "stream": False,
            "options": {"temperature": float(self.settings.get("temperature", 0.3))},
        }
        try:
            response = requests.post(f"{self._ollama_base()}/api/chat", json=payload, timeout=REQUEST_TIMEOUT * 2)
        except requests.exceptions.RequestException as exc:
            raise BrainError(
                f"Ollama is not answering at {self._ollama_base()} ({exc}). "
                "Start it with `ollama serve` and pull a model, e.g. `ollama pull llama3.1`."
            ) from None
        if response.status_code == 404:
            raise BrainError(f"Ollama has no model named '{model}'. Run: ollama pull {model}")
        if response.status_code >= 400:
            raise BrainError(f"Ollama error {response.status_code}: {response.text[:200]}")
        try:
            data = response.json()
        except ValueError:
            raise BrainError("Ollama returned a non-JSON reply.") from None
        text = (data.get("message") or {}).get("content") or data.get("response") or ""
        if not text:
            raise BrainError(f"Ollama model '{model}' returned nothing. Try `ollama pull {model}`.")
        return str(text), model

    def list_ollama_models(self) -> list[str]:
        if requests is None:
            return []
        try:
            response = requests.get(f"{self._ollama_base()}/api/tags", timeout=3)
            data = response.json()
            return [m.get("name", "") for m in data.get("models", []) if m.get("name")]
        except Exception:
            return []

    # ── diagnostics ──────────────────────────────────────────────────────
    def test(self) -> Reply:
        """One-shot connectivity test used by the Settings panel."""
        if not self.enabled:
            return Reply(text="", provider="offline", ok=False,
                         error="Switch the brain to Groq, OpenAI or Ollama to test a connection.")
        reply = self.ask(
            "Reply with exactly: JARVIS online.",
            extra_system="This is a connectivity self-test. Keep the answer to one short line.",
        )
        if not reply.ok and reply.error:
            return reply
        return reply

    def save_debug(self, payload: dict[str, Any]) -> None:  # pragma: no cover - dev helper
        print(json.dumps(payload, indent=2)[:2000])
