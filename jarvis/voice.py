"""
Voice I/O for JARVIS: speech out (TTS) and speech in (STT + wake word).

Everything here is optional and auto-detected. The app runs perfectly well with
no audio libraries installed — it simply reports what is missing and stays on
text. Install the extras you want:

    pip install pyttsx3                      # speech out (Windows SAPI / espeak / macOS say)
    pip install sounddevice faster-whisper   # speech in + wake word, fully offline
    pip install SpeechRecognition            # speech in via Google's web API (needs internet)
    pip install vosk                         # lightweight offline speech in (needs a model folder)

Backend priority
────────────────
TTS   pyttsx3 → Qt text-to-speech (ships with PyQt6) → OS command (PowerShell `say`/`espeak`) → silent
STT   faster-whisper → vosk → SpeechRecognition (Google) → unavailable
Wake  continuous listening with an energy gate, then fuzzy match on the wake word
"""

from __future__ import annotations

import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import wave
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import Settings

SAMPLE_RATE = 16_000
WAKE_FUZZY_TOLERANCE = 1  # edit distance, catches "jarvus", "jarviz"


def _levenshtein(a: str, b: str) -> int:
    if a == b:
        return 0
    if not a or not b:
        return len(a) or len(b)
    previous = list(range(len(b) + 1))
    for i, char_a in enumerate(a, 1):
        current = [i]
        for j, char_b in enumerate(b, 1):
            current.append(min(
                previous[j] + 1,
                current[j - 1] + 1,
                previous[j - 1] + (char_a != char_b),
            ))
        previous = current
    return previous[-1]


def contains_wake_word(transcript: str, wake_word: str = "jarvis") -> bool:
    """Fuzzy wake-word detection so accents and mics do not need perfection."""
    if not transcript:
        return False
    words = re.findall(r"[a-z']+", transcript.lower())
    target = wake_word.lower().strip()
    for index, word in enumerate(words):
        if word == target:
            return True
        if abs(len(word) - len(target)) <= WAKE_FUZZY_TOLERANCE and _levenshtein(word, target) <= WAKE_FUZZY_TOLERANCE:
            return True
        if index and words[index - 1] in {"hey", "ok", "okay", "yo"} and word.startswith(target[:4]):
            return True
    return False


def split_wake_command(transcript: str, wake_word: str = "jarvis") -> str:
    """Return whatever the speaker said after the wake word."""
    if not transcript:
        return ""
    pattern = rf"(?i)\b(hey\s+|ok\s+|okay\s+|yo\s+)?{re.escape(wake_word)}\b[\s,\.!:-]*"
    parts = re.split(pattern, transcript, maxsplit=1)
    if len(parts) >= 3:
        return parts[-1].strip()
    return ""


# ════════════════════════════════════════════════════════════════════════════
#  Text to speech
# ════════════════════════════════════════════════════════════════════════════

class _Pyttsx3Backend:
    name = "pyttsx3"

    def __init__(self, rate: int, voice: str = "") -> None:
        import pyttsx3

        self._queue: queue.Queue[tuple[str, str]] = queue.Queue()
        self._engine = pyttsx3.init()
        self._engine.setProperty("rate", rate)
        if voice:
            self._engine.setProperty("voice", voice)
        self._thread = threading.Thread(target=self._worker, daemon=True, name="jarvis-tts")
        self._thread.start()

    def _worker(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            text, voice = item
            try:
                if voice:
                    self._engine.setProperty("voice", voice)
                self._engine.say(text)
                self._engine.runAndWait()
            except Exception:
                continue

    def say(self, text: str, voice: str = "") -> None:
        self._queue.put((text, voice))

    def stop(self) -> None:
        try:
            self._engine.stop()
        except Exception:
            pass

    def voices(self) -> list[tuple[str, str]]:
        try:
            return [(v.id, v.name) for v in self._engine.getProperty("voices")]
        except Exception:
            return []


class _QtTtsBackend:
    """Qt text-to-speech — ships with PyQt6, no extra pip install needed."""

    name = "qt"

    def __init__(self, rate: int, voice: str = "") -> None:
        from PyQt6.QtCore import QLocale  # noqa: F401
        from PyQt6.QtTextToSpeech import QTextToSpeech

        self._tts = QTextToSpeech()
        self._tts.setRate(max(-1.0, min(1.0, (rate - 175) / 100.0)))
        if voice:
            self.set_voice(voice)

    def say(self, text: str, voice: str = "") -> None:
        if voice:
            self.set_voice(voice)
        self._tts.say(text)

    def stop(self) -> None:
        try:
            self._tts.stop()
        except Exception:
            pass

    def set_voice(self, voice_id: str) -> None:
        try:

            for candidate in self._tts.availableVoices():
                if voice_id in (candidate.name(), str(candidate.name())):
                    self._tts.setVoice(candidate)
                    return
        except Exception:
            pass

    def voices(self) -> list[tuple[str, str]]:
        try:
            return [(v.name(), v.name()) for v in self._tts.availableVoices()]
        except Exception:
            return []


class _SystemTtsBackend:
    """Last resort: the OS speech command."""

    def __init__(self, rate: int) -> None:
        self.rate = rate
        self.name = self._detect() or "system"

    @staticmethod
    def _detect() -> str | None:
        if sys.platform.startswith("win"):
            return "powershell"
        if sys.platform == "darwin" and shutil.which("say"):
            return "say"
        for candidate in ("espeak-ng", "espeak", "spd-say"):
            if shutil.which(candidate):
                return candidate
        return None

    def say(self, text: str, voice: str = "") -> None:
        text = text[:1200]
        try:
            if self.name == "powershell":
                safe = text.replace("'", "''")
                command = (
                    "Add-Type -AssemblyName System.Speech; "
                    "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
                    f"$s.Rate = {max(-10, min(10, (self.rate - 175) // 25))}; "
                    f"$s.Speak('{safe}');"
                )
                subprocess.Popen(
                    ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            elif self.name == "say":
                subprocess.Popen(["say", text], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                speed = max(80, min(450, self.rate))
                subprocess.Popen(
                    [self.name, "-s", str(speed), text] if self.name.startswith("espeak")
                    else [self.name, "-r", str(speed), text],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
        except Exception:
            pass

    def stop(self) -> None:
        return None

    def voices(self) -> list[tuple[str, str]]:
        return []


class TextToSpeech:
    """Speak text through whichever backend is available."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.error = ""
        self.backend: Any = None
        self._backend_name = "none"
        self._init_backend()

    def _init_backend(self) -> None:
        preference = str(self.settings.get("tts_backend", "auto"))
        rate = int(self.settings.get("tts_rate", 175))
        voice = str(self.settings.get("tts_voice", ""))
        order = ["pyttsx3", "qt", "system"]
        if preference in order:
            order = [preference] + [name for name in order if name != preference]

        errors: list[str] = []
        for name in order:
            try:
                if name == "pyttsx3":
                    self.backend = _Pyttsx3Backend(rate, voice)
                elif name == "qt":
                    self.backend = _QtTtsBackend(rate, voice)
                elif name == "system":
                    backend = _SystemTtsBackend(rate)
                    if backend.name == "system":
                        raise RuntimeError("no system speech command found")
                    self.backend = backend
                self._backend_name = name
                return
            except Exception as exc:
                errors.append(f"{name}: {exc}")
        self.error = "; ".join(errors) or "no TTS backend available"

    @property
    def available(self) -> bool:
        return self.backend is not None

    @property
    def backend_name(self) -> str:
        return self._backend_name

    def describe(self) -> str:
        if self.available:
            return f"{self._backend_name} (spoken replies ready)"
        return "unavailable — pip install pyttsx3"

    def speak(self, text: str, block: bool = False) -> bool:
        if not self.available or not (text or "").strip():
            return False
        voice = str(self.settings.get("tts_voice", ""))
        try:
            self.backend.say(text.strip(), voice)
            return True
        except Exception as exc:
            self.error = str(exc)
            return False

    def stop(self) -> None:
        if self.available:
            try:
                self.backend.stop()
            except Exception:
                pass

    def voices(self) -> list[tuple[str, str]]:
        try:
            return self.backend.voices() if self.available else []
        except Exception:
            return []


# ════════════════════════════════════════════════════════════════════════════
#  Speech to text
# ════════════════════════════════════════════════════════════════════════════

@dataclass
class AudioDevice:
    index: int
    name: str


class Recorder:
    """Microphone capture with silence detection (sounddevice preferred)."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.error = ""
        try:
            import sounddevice

            self._sd = sounddevice
        except Exception as exc:
            self._sd = None
            self.error = f"sounddevice unavailable ({exc})"

    @property
    def available(self) -> bool:
        return self._sd is not None

    def devices(self) -> list[AudioDevice]:
        if not self.available:
            return []
        try:
            return [
                AudioDevice(index, info["name"])
                for index, info in enumerate(self._sd.query_devices())
                if info.get("max_input_channels", 0) > 0
            ]
        except Exception:
            return []

    def record(self, max_seconds: float = 8.0, silence_seconds: float = 1.0,
               min_seconds: float = 0.4, on_level: Callable[[float], None] | None = None) -> str | None:
        """Record until the speaker stops, return a path to a 16 kHz mono WAV."""
        if not self.available:
            return None
        import numpy as np

        device = self.settings.get("mic_index")
        try:
            device = int(device) if device is not None and str(device) != "" else None
        except (TypeError, ValueError):
            device = None
        block = 1024
        captured: list[Any] = []
        silent_for = 0.0
        spoke = False
        started = time.time()
        threshold = 0.012  # RMS gate, works well for normal speech on laptop mics

        try:
            with self._sd.InputStream(
                samplerate=SAMPLE_RATE, channels=1, dtype="float32",
                blocksize=block, device=device,
            ) as stream:
                while time.time() - started < max_seconds:
                    data, _overflow = stream.read(block)
                    samples = data[:, 0].copy()
                    captured.append(samples)
                    level = float(np.sqrt(np.mean(samples ** 2)))
                    if on_level:
                        on_level(min(1.0, level * 12))
                    if level > threshold:
                        spoke = True
                        silent_for = 0.0
                    elif spoke:
                        silent_for += block / SAMPLE_RATE
                    if spoke and silent_for >= silence_seconds and (time.time() - started) > min_seconds:
                        break
        except Exception as exc:
            self.error = str(exc)
            return None

        if not captured or not spoke:
            return None
        audio = np.concatenate(captured)
        peak = float(np.max(np.abs(audio))) or 1.0
        audio = (audio / peak) * 0.9
        pcm = (audio * 32767).astype("int16")
        handle, path = tempfile.mkstemp(prefix="jarvis-", suffix=".wav")
        os.close(handle)
        with wave.open(path, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(SAMPLE_RATE)
            wav.writeframes(pcm.tobytes())
        return path


class _WhisperBackend:
    name = "faster-whisper"

    def __init__(self, model_size: str) -> None:
        from faster_whisper import WhisperModel

        self._model = WhisperModel(model_size, device="cpu", compute_type="int8")

    def transcribe(self, wav_path: str) -> str:
        segments, _info = self._model.transcribe(wav_path, beam_size=1, language=None, vad_filter=True)
        return " ".join(segment.text.strip() for segment in segments).strip()


class _VoskBackend:
    name = "vosk"

    def __init__(self, model_path: str) -> None:
        import json as _json

        import vosk

        self._json = _json
        if not model_path or not Path(model_path).exists():
            raise RuntimeError("vosk needs a model folder — set vosk_model_path in Settings")
        vosk.SetLogLevel(-1)
        self._model = vosk.Model(model_path)

    def transcribe(self, wav_path: str) -> str:
        import vosk

        with wave.open(wav_path, "rb") as wav:
            recogniser = vosk.KaldiRecognizer(self._model, wav.getframerate())
            words: list[str] = []
            while True:
                chunk = wav.readframes(4000)
                if not chunk:
                    break
                if recogniser.AcceptWaveform(chunk):
                    words.append(self._json.loads(recogniser.Result()).get("text", ""))
            words.append(self._json.loads(recogniser.FinalResult()).get("text", ""))
        return " ".join(word for word in words if word).strip()


class _GoogleBackend:
    name = "google-web"

    def __init__(self) -> None:
        import speech_recognition as sr

        self._sr = sr

    def transcribe(self, wav_path: str) -> str:
        recogniser = self._sr.Recognizer()
        with self._sr.AudioFile(wav_path) as source:
            audio = recogniser.record(source)
        return recogniser.recognize_google(audio)


class SpeechToText:
    """Turn recorded audio into text using the best available backend."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.recorder = Recorder(settings)
        self.backend: Any = None
        self.error = ""
        self._init_backend()

    def _init_backend(self) -> None:
        preference = str(self.settings.get("stt_backend", "auto")).lower()
        candidates: list[str] = []
        if preference in {"faster-whisper", "vosk", "google"}:
            candidates.append(preference)
        candidates += [name for name in ("faster-whisper", "vosk", "google") if name not in candidates]

        errors: list[str] = []
        for name in candidates:
            try:
                if name == "faster-whisper":
                    self.backend = _WhisperBackend(str(self.settings.get("whisper_model", "base")))
                elif name == "vosk":
                    self.backend = _VoskBackend(str(self.settings.get("vosk_model_path", "")))
                elif name == "google":
                    self.backend = _GoogleBackend()
                return
            except Exception as exc:
                errors.append(f"{name}: {exc}")
        self.error = "; ".join(errors) or "no speech-to-text backend installed"

    @property
    def available(self) -> bool:
        return self.backend is not None and self.recorder.available

    def describe(self) -> str:
        if self.available:
            return f"{self.backend.name} + {self.recorder.error or 'microphone'}"
        if not self.recorder.available:
            return "unavailable — pip install sounddevice"
        return "unavailable — pip install faster-whisper (or vosk / SpeechRecognition)"

    def listen(self, max_seconds: float = 8.0, on_level: Callable[[float], None] | None = None) -> str:
        """Record one utterance and transcribe it (empty string if nothing heard)."""
        if not self.available:
            return ""
        path = self.recorder.record(max_seconds=max_seconds, on_level=on_level)
        if not path:
            return ""
        try:
            return self.backend.transcribe(path)
        except Exception as exc:
            self.error = str(exc)
            return ""
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass


# ════════════════════════════════════════════════════════════════════════════
#  Wake word listener
# ════════════════════════════════════════════════════════════════════════════

@dataclass
class WakeWordListener:
    """Background thread that listens for “Jarvis …” and hands over the command."""

    stt: SpeechToText
    settings: Settings
    on_command: Callable[[str], None]
    on_state: Callable[[str], None] | None = None
    on_level: Callable[[float], None] | None = None
    running: bool = False
    _stop: threading.Event = field(default_factory=threading.Event)
    _thread: threading.Thread | None = None

    @property
    def supported(self) -> bool:
        return self.stt.available

    def start(self) -> bool:
        if self.running or not self.supported:
            return False
        self._stop.clear()
        self.running = True
        self._thread = threading.Thread(target=self._loop, daemon=True, name="jarvis-wake")
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()
        self.running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)

    def _state(self, value: str) -> None:
        if self.on_state:
            try:
                self.on_state(value)
            except Exception:
                pass

    def _loop(self) -> None:
        wake_word = str(self.settings.get("wake_word", "jarvis"))
        while not self._stop.is_set():
            try:
                self._state("listening")
                window = self.stt.listen(max_seconds=5.0, on_level=self.on_level)
                if self._stop.is_set():
                    break
                if not window:
                    self._state("idle")
                    continue
                if not contains_wake_word(window, wake_word):
                    self._state("idle")
                    continue
                inline = split_wake_command(window, wake_word)
                self._state("capturing")
                if inline and len(inline.split()) >= 2:
                    command = inline
                else:
                    follow_up = self.stt.listen(max_seconds=8.0, on_level=self.on_level)
                    command = follow_up or inline
                if command:
                    self._state("thinking")
                    self.on_command(command)
                else:
                    self._state("idle")
            except Exception:
                self._stop.wait(1.0)
        self._state("idle")
        self.running = False


# ════════════════════════════════════════════════════════════════════════════
#  Capability report used by the UI and settings panel
# ════════════════════════════════════════════════════════════════════════════

def probe(settings: Settings, tts: TextToSpeech | None = None, stt: SpeechToText | None = None) -> dict[str, Any]:
    tts = tts or TextToSpeech(settings)
    stt = stt or SpeechToText(settings)
    report = {
        "tts": tts.describe(),
        "tts_backend": tts.backend_name,
        "stt": stt.describe(),
        "stt_backend": getattr(stt.backend, "name", "none") if stt.backend else "none",
        "mic": "ready" if stt.recorder.available else "unavailable",
        "wake": "off",
        "errors": [e for e in (tts.error, stt.error, stt.recorder.error) if e],
    }
    if tts.available and stt.available:
        report["summary"] = "Voice in and out are ready."
    elif tts.available:
        report["summary"] = "I can speak; install sounddevice + faster-whisper to hear you too."
    elif stt.available:
        report["summary"] = "I can hear you; pip install pyttsx3 and I will answer out loud."
    else:
        report["summary"] = ("Voice is off. Install the extras: pip install pyttsx3 sounddevice "
                             "faster-whisper — or keep typing, everything else still works.")
    return report


VOICE_INSTALL_HINT = (
    "Voice needs optional packages:\n"
    "  pip install pyttsx3                      (speech out)\n"
    "  pip install sounddevice faster-whisper   (speech in + wake word, offline)\n"
    "Everything else works without them."
)
