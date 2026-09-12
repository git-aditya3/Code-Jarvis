"""
The "brain" layer: pluggable LLM providers behind one tiny interface.

**Free by default.** JARVIS ships pointing at a *free cloud model* that needs no
account and no key (Pollinations serves open-weight models over an
OpenAI-compatible API). Add a free-tier key for Groq, Gemini or OpenRouter and
that provider is preferred instead; point it at local Ollama and nothing leaves
the machine. When every model is unreachable the offline skill router still
answers — a dead network is a smaller brain, never a broken assistant.

Speed and resilience are part of the design:

* one pooled HTTP session (TLS handshakes are not repeated);
* identical questions served from an in-memory cache;
* a fresh provider is tried immediately, a *failing* one is skipped for a
  cooldown window instead of being retried on every question;
* streaming replies, so the first words appear while the rest is still arriving.

No credentials are ever baked into this file — keys come from the environment
or from ``~/.jarvis/settings.json`` (see :mod:`jarvis.config`).
"""

from __future__ import annotations

import hashlib
import json
import platform
import threading
import time
from collections import OrderedDict
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

try:  # requests is the only third-party dependency of the brain
    import requests
except Exception:  # pragma: no cover - requests is in requirements.txt
    requests = None  # type: ignore[assignment]

from .config import ENV_KEYS, FREE_LADDER, KEYLESS_PROVIDERS, PROVIDER_LABELS, VERSION, Settings

#: OpenAI-compatible chat endpoints.
COMPATIBLE_ENDPOINTS = {
    "groq": "https://api.groq.com/openai/v1/chat/completions",
    "openrouter": "https://openrouter.ai/api/v1/chat/completions",
    "openai": "https://api.openai.com/v1/chat/completions",
    "pollinations": "https://text.pollinations.ai/openai",
}
#: Pollinations also answers plain GETs — used when the POST form is unavailable.
POLLINATIONS_GET = "https://text.pollinations.ai"
GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta/models"

#: A provider that just failed is skipped for this long before being tried again.
COOLDOWN_SECONDS = 90
#: When the machine has no route out at all, stop trying for this long and answer
#: from the offline router immediately instead of making the user wait.
NETWORK_COOLDOWN = 60
CACHE_SIZE = 128

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
    cached: bool = False
    attempts: list[dict[str, Any]] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)


class BrainError(Exception):
    """Raised internally when a provider cannot answer."""


class _TimedCache:
    """Tiny thread-safe LRU with a TTL — repeated questions answer instantly."""

    def __init__(self, size: int = CACHE_SIZE, ttl: float = 900.0) -> None:
        self.size = size
        self.ttl = ttl
        self._data: OrderedDict[str, tuple[float, Reply]] = OrderedDict()
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0

    def get(self, key: str) -> Reply | None:
        with self._lock:
            item = self._data.get(key)
            if item is None:
                self.misses += 1
                return None
            stored, reply = item
            if time.time() - stored > self.ttl:
                self._data.pop(key, None)
                self.misses += 1
                return None
            self._data.move_to_end(key)
            self.hits += 1
            return reply

    def put(self, key: str, reply: Reply) -> None:
        with self._lock:
            self._data[key] = (time.time(), reply)
            self._data.move_to_end(key)
            while len(self._data) > self.size:
                self._data.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()
            self.hits = 0
            self.misses = 0

    def __len__(self) -> int:
        return len(self._data)


class Brain:
    """Route prompts to a free cloud model, a keyed provider or local Ollama."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._session: Any = None
        self._lock = threading.Lock()
        self._failed: dict[str, float] = {}          # provider -> when it failed
        self._ollama_checked: tuple[float, bool] = (0.0, False)
        self._no_network_until = 0.0
        self._cache = _TimedCache(ttl=float(settings.get("cache_ttl", 900) or 900))
        self.last_attempts: list[dict[str, Any]] = []

    # ── capability ───────────────────────────────────────────────────────
    @property
    def provider(self) -> str:
        return self.settings.provider

    @property
    def enabled(self) -> bool:
        """True when a model (not just the offline router) may handle questions."""
        return self.provider != "offline"

    @property
    def ready(self) -> bool:
        """Can the *configured* provider answer right now?"""
        if not self.enabled:
            return False
        if self.provider == "ollama":
            return self._ollama_reachable()
        if self.provider in KEYLESS_PROVIDERS:
            return requests is not None
        return bool(self.settings.api_key(self.provider))

    def usable(self, provider: str) -> bool:
        """Every provider that could plausibly answer — not just the configured one."""
        if provider == "offline":
            return False
        if provider not in COMPATIBLE_ENDPOINTS and provider not in ("gemini", "ollama"):
            return False
        if provider == "ollama":
            return self._ollama_ok()
        if provider in KEYLESS_PROVIDERS:
            return True
        return bool(self.settings.api_key(provider))

    def key_needed(self, provider: str) -> str:
        return (ENV_KEYS.get(provider) or ("",))[0]

    def in_cooldown(self, provider: str) -> bool:
        failed_at = self._failed.get(provider)
        return failed_at is not None and (time.time() - failed_at) < COOLDOWN_SECONDS

    def note_failure(self, provider: str) -> None:
        self._failed[provider] = time.time()

    def note_success(self, provider: str) -> None:
        self._failed.pop(provider, None)
        self._no_network_until = 0.0

    def note_offline(self) -> None:
        """The network itself is unreachable: back off, do not stall every question."""
        self._no_network_until = time.time() + NETWORK_COOLDOWN

    @property
    def offline(self) -> bool:
        return time.time() < self._no_network_until

    @staticmethod
    def short_error(exc: Exception) -> str:
        """A readable one-liner instead of a requests stack trace."""
        if isinstance(exc, requests.exceptions.Timeout):
            return "the request timed out"
        if isinstance(exc, requests.exceptions.SSLError):
            return "the secure connection failed (TLS)"
        if isinstance(exc, requests.exceptions.ConnectionError):
            return "the network is unreachable"
        text = str(exc).split("(Caused by")[0].strip()
        return text[:160] or type(exc).__name__

    # ── the ladder ───────────────────────────────────────────────────────
    def ladder(self, include_offline: bool = False) -> list[str]:
        """Providers to try, best first.

        Order: what you configured, then the other free providers (keyed ones
        first because they are usually faster than the keyless pool), then
        local Ollama. Providers in cooldown are pushed to the back rather than
        dropped, so a transient outage self-heals.
        """
        order: list[str] = []
        if self.provider != "offline":
            order.append(self.provider)
        if self.settings.get("free_fallback", True):
            for provider in FREE_LADDER:
                if provider not in order:
                    order.append(provider)
        if not order and self.enabled:
            order.append(self.provider)
        explicit_key = self.provider not in KEYLESS_PROVIDERS and self.settings.has_key(self.provider)
        if not explicit_key and any(self.settings.has_key(name)
                                   for name in ("groq", "gemini", "openrouter")):
            # The keyless pool is the built-in default: as soon as a real key exists,
            # the keyed free tiers are tried first because they are usually faster.
            order.sort(key=lambda name: (not self.settings.has_key(name)))
        ready = [p for p in order if self.usable(p) and not self.in_cooldown(p)]
        cooling = [p for p in order if self.usable(p) and self.in_cooldown(p)]
        if include_offline:
            return ready + cooling + ["offline"]
        return ready + cooling

    def status(self) -> str:
        if not self.enabled:
            return "Offline skills only — pick a provider in Settings."
        if self.provider == "ollama":
            return "Ollama: reachable" if self._ollama_reachable() else "Ollama: not reachable"
        if self.provider in KEYLESS_PROVIDERS:
            return f"{PROVIDER_LABELS.get(self.provider, self.provider)} — no key needed"
        source = self.settings.key_source(self.provider)
        if source:
            return f"{PROVIDER_LABELS.get(self.provider, self.provider)} — key from {source}"
        return (f"{PROVIDER_LABELS.get(self.provider, self.provider)} — no key yet; "
                f"free keys: {self.key_needed(self.provider)}")

    def describe(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.settings.model_for(),
            "status": self.status(),
            "key_source": self.settings.key_source(self.provider),
            "ladder": self.ladder(),
            "cache": {"entries": len(self._cache), "hits": self._cache.hits,
                      "misses": self._cache.misses},
        }

    # ── context helpers ──────────────────────────────────────────────────
    def system_prompt(self, memory_facts: dict[str, str] | None = None, extra: str = "",
                      profile: str = "") -> str:
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
        if profile:
            bits.append("What you have learned about how this user works "
                        "(use it to be more useful, never mention it unprompted):\n" + profile)
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
        profile: str = "",
    ) -> list[dict[str, str]]:
        messages: list[dict[str, str]] = [
            {"role": "system", "content": self.system_prompt(facts, extra_system, profile)}
        ]
        for turn in (history or [])[-max_turns:]:
            role = "assistant" if turn.get("role") == "jarvis" else "user"
            text = str(turn.get("text", "")).strip()
            if text:
                messages.append({"role": role, "content": text[:4000]})
        messages.append({"role": "user", "content": question})
        return messages

    # ── HTTP plumbing ────────────────────────────────────────────────────
    def session(self) -> Any:
        """One pooled session: keeps TLS connections warm between questions."""
        if self._session is None:
            self._session = requests.Session()
            self._session.headers.update({"User-Agent": f"JARVIS/{VERSION}"})
        return self._session

    @property
    def timeout(self) -> float:
        return max(5.0, float(self.settings.get("llm_timeout", 25) or 25))

    def _headers(self, provider: str) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        key = self.settings.api_key(provider) if provider not in KEYLESS_PROVIDERS else ""
        if key:
            headers["Authorization"] = f"Bearer {key}"
        if provider == "openrouter":
            headers["HTTP-Referer"] = "https://github.com/git-aditya3/Code-Jarvis"
            headers["X-Title"] = "JARVIS"
        return headers

    def _payload(self, messages: list[dict[str, str]], stream: bool = False) -> dict[str, Any]:
        return {
            "model": self.settings.model_for(self.provider),
            "messages": messages,
            "max_tokens": int(self.settings.get("max_tokens", 900)),
            "temperature": float(self.settings.get("temperature", 0.3)),
            **({"stream": True} if stream else {}),
        }

    # ── the one method the rest of the app uses ──────────────────────────
    def ask(
        self,
        question: str,
        history: list[dict[str, Any]] | None = None,
        facts: dict[str, str] | None = None,
        extra_system: str = "",
        profile: str = "",
        use_cache: bool = True,
    ) -> Reply:
        if not self.enabled:
            return Reply(text="", provider="offline", ok=False,
                         error="No language model is configured.")
        if requests is None:
            return Reply(text="", provider=self.provider, ok=False,
                         error="The 'requests' package is missing — run: pip install requests")
        if self.offline:
            return Reply(
                text="", provider=self.provider, ok=False,
                error=("I can't reach any free model right now — the network looks down. "
                       "Everything offline still works: try /help, or keep going and I'll "
                       "retry the cloud in a minute."),
                attempts=[{"provider": self.provider, "ok": False, "skipped": "network down"}])

        messages = self.build_messages(question, history, facts, extra_system, profile=profile)
        key = self._cache_key(messages)
        if use_cache and self.settings.get("response_cache", True):
            cached = self._cache.get(key)
            if cached is not None:
                return Reply(text=cached.text, provider=cached.provider, ok=True,
                             model=cached.model, elapsed=cached.elapsed, cached=True,
                             attempts=[{"provider": cached.provider, "cached": True}])

        started = time.time()
        attempts: list[dict[str, Any]] = []
        best_error = ""
        for provider in self.ladder():
            attempt_started = time.time()
            try:
                text, model = self._ask_provider(provider, messages)
            except BrainError as exc:
                self.note_failure(provider)
                attempts.append({"provider": provider, "ok": False, "error": str(exc)[:200],
                                 "elapsed": round(time.time() - attempt_started, 2)})
                best_error = str(exc)
                continue
            self.note_success(provider)
            attempts.append({"provider": provider, "ok": True, "model": model,
                             "elapsed": round(time.time() - attempt_started, 2)})
            reply = Reply(text=text.strip(), provider=provider, ok=bool(text.strip()),
                          error="" if text.strip() else "The model returned an empty answer.",
                          model=model, elapsed=time.time() - started, attempts=attempts)
            if reply.ok and use_cache and self.settings.get("response_cache", True):
                self._cache.put(key, reply)
            self.last_attempts = attempts
            return reply

        self.last_attempts = attempts
        if not attempts:
            return Reply(text="", provider=self.provider, ok=False,
                         error="No provider is available. Add a key in Settings or check your connection.",
                         elapsed=time.time() - started, attempts=attempts)
        return Reply(
            text="", provider=self.provider, ok=False, elapsed=time.time() - started,
            error=f"Every model I tried failed. Last error: {best_error}", attempts=attempts,
        )

    def ask_stream(
        self,
        question: str,
        history: list[dict[str, Any]] | None = None,
        facts: dict[str, str] | None = None,
        extra_system: str = "",
        profile: str = "",
    ) -> Iterator[str]:
        """Yield answer chunks as they arrive.

        The first provider that produces a chunk wins; if it fails before its
        first token the next one in the ladder is tried, so the user never sees
        a half-finished answer from a dead provider.
        """
        if not self.enabled or requests is None:
            return
        if self.offline:
            raise BrainError("The network looks down, so no cloud model is reachable.")
        messages = self.build_messages(question, history, facts, extra_system, profile=profile)
        key = self._cache_key(messages)
        if self.settings.get("response_cache", True):
            cached = self._cache.get(key)
            if cached is not None:
                yield cached.text
                return

        collected: list[str] = []
        for provider in self.ladder():
            produced = False
            try:
                for chunk in self._stream_provider(provider, messages):
                    if not chunk:
                        continue
                    produced = True
                    collected.append(chunk)
                    yield chunk
            except BrainError:
                self.note_failure(provider)
                if produced:
                    return              # a half answer is still an answer: keep it
                continue
            if produced:
                self.note_success(provider)
                if self.settings.get("response_cache", True):
                    self._cache.put(key, Reply(text="".join(collected), provider=provider,
                                               model=self.settings.model_for(provider)))
                return
        if not collected:
            raise BrainError("No model could answer that just now.")

    def _cache_key(self, messages: list[dict[str, str]]) -> str:
        digest = hashlib.sha1()
        for message in messages:
            digest.update(message["role"].encode("utf-8", "ignore"))
            digest.update(message["content"].encode("utf-8", "ignore"))
        return f"{self.provider}:{self.settings.model_for()}:{digest.hexdigest()}"

    def clear_cache(self) -> None:
        self._cache.clear()

    # ── per-provider calls ───────────────────────────────────────────────
    def _ask_provider(self, provider: str, messages: list[dict[str, str]]) -> tuple[str, str]:
        if provider == "ollama":
            return self._ask_ollama(messages, provider)
        if provider == "gemini":
            return self._ask_gemini(messages, provider)
        if provider in COMPATIBLE_ENDPOINTS:
            return self._ask_compatible(messages, provider)
        raise BrainError(f"I don't know how to talk to “{provider}”.")

    def _stream_provider(self, provider: str, messages: list[dict[str, str]]) -> Iterator[str]:
        if provider == "ollama":
            yield from self._stream_ollama(messages, provider)
            return
        if provider == "gemini":
            yield from self._stream_gemini(messages, provider)
            return
        yield from self._stream_compatible(messages, provider)

    # openai-compatible ────────────────────────────────────────────────────
    def _compatible_error(self, provider: str, response: Any) -> BrainError:
        label = PROVIDER_LABELS.get(provider, provider)
        if response.status_code in (401, 403):
            env_name = self.key_needed(provider)
            return BrainError(
                f"{label} rejected the key (HTTP {response.status_code}). Paste a fresh one in "
                f"Settings" + (f" or set ${env_name}." if env_name else ".")
            )
        if response.status_code == 429:
            return BrainError(f"{label} is rate-limiting (HTTP 429).")
        if response.status_code in (402, 404) and provider == "pollinations":
            return BrainError("The free model pool refused that model — try another model name.")
        detail = ""
        try:
            detail = response.json().get("error", {}).get("message", "")
        except Exception:
            detail = (response.text or "")[:200]
        return BrainError(f"{label} error {response.status_code}: {detail}")

    def _ask_compatible(self, messages: list[dict[str, str]], provider: str) -> tuple[str, str]:
        model = self.settings.model_for(provider)
        payload = {
            "model": model, "messages": messages,
            "max_tokens": int(self.settings.get("max_tokens", 900)),
            "temperature": float(self.settings.get("temperature", 0.3)),
        }
        try:
            response = self.session().post(COMPATIBLE_ENDPOINTS[provider], json=payload,
                                           headers=self._headers(provider),
                                           timeout=(4, self.timeout))
        except requests.exceptions.Timeout:
            raise BrainError(f"{PROVIDER_LABELS.get(provider, provider)} timed out.") from None
        except requests.exceptions.RequestException as exc:
            # A connection error means the machine is offline or the host is blocked.
            # Note it globally: the next question then answers from skills instantly.
            self.note_offline()
            raise BrainError(f"Could not reach {provider}: {self.short_error(exc)}") from None
        if response.status_code >= 400:
            error = self._compatible_error(provider, response)
            if provider == "pollinations":
                try:
                    return self._ask_pollinations_get(messages)
                except BrainError:
                    pass
            raise error
        try:
            data = response.json()
            text = data["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError):
            raise BrainError(f"Unexpected reply from {provider}.") from None
        return str(text), model

    def _ask_pollinations_get(self, messages: list[dict[str, str]]) -> tuple[str, str]:
        """The plain-text GET endpoint, used if the OpenAI-compatible form is unavailable."""
        if requests is None:
            raise BrainError("The 'requests' package is missing.")
        prompt = "\n\n".join(f"{m['role']}: {m['content']}" for m in messages if m["content"])
        prompt = prompt[:1500]          # the GET form travels in the path, so keep it short
        model = self.settings.model_for("pollinations")
        try:
            response = self.session().get(f"{POLLINATIONS_GET}/{prompt}",
                                          params={"model": model},
                                          timeout=(5, self.timeout))
        except requests.exceptions.RequestException as exc:
            self.note_offline()
            raise BrainError(f"Could not reach the free model pool: {self.short_error(exc)}") from None
        if response.status_code >= 400:
            raise BrainError(f"The free model pool returned HTTP {response.status_code}.")
        text = (response.text or "").strip()
        if not text:
            raise BrainError("The free model pool returned an empty answer.")
        return text, model

    def _stream_compatible(self, messages: list[dict[str, str]], provider: str) -> Iterator[str]:
        model = self.settings.model_for(provider)
        payload = {
            "model": model, "messages": messages, "stream": True,
            "max_tokens": int(self.settings.get("max_tokens", 900)),
            "temperature": float(self.settings.get("temperature", 0.3)),
        }
        try:
            with self.session().post(COMPATIBLE_ENDPOINTS[provider], json=payload,
                                     headers=self._headers(provider), stream=True,
                                     timeout=(5, self.timeout)) as response:
                if response.status_code >= 400:
                    raise self._compatible_error(provider, response)
                for line in response.iter_lines(decode_unicode=True):
                    if not line:
                        continue
                    if not line.startswith("data:"):
                        continue
                    body = line[5:].strip()
                    if body in ("", "[DONE]"):
                        continue
                    try:
                        chunk = json.loads(body)
                        piece = chunk["choices"][0].get("delta", {}).get("content")
                    except (ValueError, KeyError, IndexError):
                        continue
                    if piece:
                        yield str(piece)
        except requests.exceptions.RequestException as exc:
            self.note_offline()
            raise BrainError(f"{provider} stream failed: {self.short_error(exc)}") from None

    # gemini ──────────────────────────────────────────────────────────────
    def _gemini_body(self, messages: list[dict[str, str]]) -> dict[str, Any]:
        system = "\n".join(m["content"] for m in messages if m["role"] == "system")
        contents = [
            {"role": "model" if m["role"] == "assistant" else "user", "parts": [{"text": m["content"]}]}
            for m in messages if m["role"] != "system" and m["content"]
        ]
        body: dict[str, Any] = {
            "contents": contents,
            "generationConfig": {
                "temperature": float(self.settings.get("temperature", 0.3)),
                "maxOutputTokens": int(self.settings.get("max_tokens", 900)),
            },
            "safetySettings": [],
        }
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        return body

    def _gemini_request(self, provider: str, stream: bool) -> tuple[str, dict[str, Any], dict[str, str]]:
        model = self.settings.model_for(provider)
        key = self.settings.api_key(provider)
        method = "streamGenerateContent" if stream else "generateContent"
        suffix = "&alt=sse" if stream else ""
        url = f"{GEMINI_BASE}/{model}:{method}?key={key}{suffix}"
        return url, {"Content-Type": "application/json"}, {}

    def _ask_gemini(self, messages: list[dict[str, str]], provider: str) -> tuple[str, str]:
        url, headers, _ = self._gemini_request(provider, stream=False)
        try:
            response = self.session().post(url, json=self._gemini_body(messages), headers=headers,
                                           timeout=(5, self.timeout))
        except requests.exceptions.Timeout:
            raise BrainError("Gemini timed out.") from None
        except requests.exceptions.RequestException as exc:
            self.note_offline()
            raise BrainError(f"Could not reach Gemini: {self.short_error(exc)}") from None
        if response.status_code >= 400:
            raise self._compatible_error(provider, response)
        try:
            data = response.json()
            parts = data["candidates"][0]["content"]["parts"]
            text = "".join(str(part.get("text", "")) for part in parts)
        except (ValueError, KeyError, IndexError):
            raise BrainError("Unexpected reply from Gemini.") from None
        return text, self.settings.model_for(provider)

    def _stream_gemini(self, messages: list[dict[str, str]], provider: str) -> Iterator[str]:
        url, headers, _ = self._gemini_request(provider, stream=True)
        try:
            with self.session().post(url, json=self._gemini_body(messages), headers=headers,
                                     stream=True, timeout=(5, self.timeout)) as response:
                if response.status_code >= 400:
                    raise self._compatible_error(provider, response)
                for line in response.iter_lines(decode_unicode=True):
                    if not line or not line.startswith("data:"):
                        continue
                    body = line[5:].strip()
                    if not body or body == "[DONE]":
                        continue
                    try:
                        data = json.loads(body)
                        parts = data["candidates"][0]["content"]["parts"]
                        piece = "".join(str(part.get("text", "")) for part in parts)
                    except (ValueError, KeyError, IndexError):
                        continue
                    if piece:
                        yield piece
        except requests.exceptions.RequestException as exc:
            self.note_offline()
            raise BrainError(f"Gemini stream failed: {self.short_error(exc)}") from None

    # ollama ──────────────────────────────────────────────────────────────
    def _ollama_base(self) -> str:
        return str(self.settings.get("ollama_url") or "http://localhost:11434").rstrip("/")

    def _ollama_ok(self, ttl: float = 30.0) -> bool:
        """Reachability with a short cache, so the ladder does not probe every question."""
        checked, value = self._ollama_checked
        if time.time() - checked < ttl:
            return value
        value = self._ollama_reachable()
        self._ollama_checked = (time.time(), value)
        return value

    def _ollama_reachable(self) -> bool:
        if requests is None:
            return False
        try:
            response = self.session().get(f"{self._ollama_base()}/api/tags", timeout=2.5)
            return response.status_code == 200
        except requests.exceptions.RequestException:
            return False

    def _ollama_payload(self, messages: list[dict[str, str]], provider: str,
                        stream: bool) -> dict[str, Any]:
        return {
            "model": self.settings.model_for(provider),
            "messages": messages,
            "stream": stream,
            "options": {"temperature": float(self.settings.get("temperature", 0.3))},
        }

    def _ask_ollama(self, messages: list[dict[str, str]], provider: str = "ollama") -> tuple[str, str]:
        model = self.settings.model_for(provider)
        try:
            response = self.session().post(f"{self._ollama_base()}/api/chat",
                                           json=self._ollama_payload(messages, provider, False),
                                           timeout=(5, self.timeout * 2))
        except requests.exceptions.RequestException as exc:
            raise BrainError(
                f"Ollama is not answering at {self._ollama_base()} "
                f"({self.short_error(exc)}). "
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

    def _stream_ollama(self, messages: list[dict[str, str]], provider: str) -> Iterator[str]:
        try:
            with self.session().post(f"{self._ollama_base()}/api/chat",
                                     json=self._ollama_payload(messages, provider, True),
                                     stream=True, timeout=(5, self.timeout * 2)) as response:
                if response.status_code >= 400:
                    raise BrainError(f"Ollama error {response.status_code}")
                for line in response.iter_lines(decode_unicode=True):
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                    except ValueError:
                        continue
                    piece = (data.get("message") or {}).get("content") or ""
                    if piece:
                        yield str(piece)
                    if data.get("done"):
                        return
        except requests.exceptions.RequestException as exc:
            raise BrainError(f"Ollama stream failed: {self.short_error(exc)}") from None

    def list_ollama_models(self) -> list[str]:
        if requests is None:
            return []
        try:
            response = self.session().get(f"{self._ollama_base()}/api/tags", timeout=3)
            data = response.json()
            return [m.get("name", "") for m in data.get("models", []) if m.get("name")]
        except Exception:
            return []

    # ── diagnostics ──────────────────────────────────────────────────────
    def test(self) -> Reply:
        """One-shot connectivity test used by the Settings panel."""
        if not self.enabled:
            return Reply(text="", provider="offline", ok=False,
                         error="Pick a provider (the free cloud default works with no key).")
        self.clear_cache()
        return self.ask(
            "Reply with exactly: JARVIS online.",
            extra_system="This is a connectivity self-test. Keep the answer to one short line.",
            use_cache=False,
        )

    def save_debug(self, payload: dict[str, Any]) -> None:  # pragma: no cover - dev helper
        print(json.dumps(payload, indent=2)[:2000])
