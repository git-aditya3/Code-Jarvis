"""
Entry point for JARVIS.

    python run_jarvis.py                     # desktop window (default)
    python run_jarvis.py --ask "weather"     # one-shot answer in the terminal
    python run_jarvis.py --chat              # terminal chat loop
    python run_jarvis.py --doctor            # environment + voice diagnostics
    python run_jarvis.py --skills            # list every skill
"""

from __future__ import annotations

import argparse
import platform
import shutil
import sys
import time
from pathlib import Path

from .config import PROVIDER_LABELS, VERSION, Settings, jarvis_home
from .core import Core
from .host import HeadlessHost
from .memory import Memory


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jarvis",
        description=f"JARVIS v{VERSION} — local-first personal assistant",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--ask", metavar="TEXT", help="answer one request and exit")
    mode.add_argument("--chat", action="store_true", help="terminal chat loop")
    mode.add_argument("--skills", action="store_true", help="list available skills")
    mode.add_argument("--doctor", action="store_true", help="check dependencies and voice support")
    parser.add_argument("--provider", choices=list(PROVIDER_LABELS), help="override the brain for this run")
    parser.add_argument("--model", help="override the model name")
    parser.add_argument("--no-voice", action="store_true", help="never speak, even if voice is configured")
    parser.add_argument("--wake", action="store_true", help="start wake-word listening on launch")
    parser.add_argument("--data-dir", help="use a different folder for settings/memory")
    parser.add_argument("--version", action="version", version=f"JARVIS {VERSION}")
    return parser


def make_core(args: argparse.Namespace, host=None) -> Core:
    if args.data_dir:
        import os

        os.environ["JARVIS_HOME"] = str(Path(args.data_dir).expanduser())
    settings = Settings()
    if args.provider:
        settings["provider"] = args.provider
    if args.model:
        models = dict(settings.as_dict().get("models") or {})
        models[settings.provider] = args.model
        settings["models"] = models
    if getattr(args, "no_voice", False):
        settings["voice_replies"] = False
    if getattr(args, "wake", False):
        settings["wake_word_enabled"] = True
    settings.save()
    memory = Memory()
    return Core(settings, memory, host or HeadlessHost(verbose=False))


# ── CLI modes ────────────────────────────────────────────────────────────────

def cmd_ask(args: argparse.Namespace) -> int:
    host = HeadlessHost(verbose=False)
    core = make_core(args, host)
    response = core.ask(args.ask)
    print(response.text)
    if response.data.get("path"):
        print(f"\n(saved: {response.data['path']})")
    return 0 if response.ok else 1


def cmd_chat(args: argparse.Namespace) -> int:
    host = HeadlessHost(verbose=False)
    core = make_core(args, host)
    print(f"JARVIS {VERSION} — type /help for commands, /quit to exit.")
    print(f"brain: {core.brain.status()}\n")
    while True:
        try:
            text = input("you › ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not text:
            continue
        if text in {"/quit", "/exit", "quit", "exit"}:
            break
        started = time.time()
        response = core.ask(text)
        print(f"jarvis › {response.text}")
        print(f"        [{response.skill or response.provider} · {time.time() - started:.2f}s]\n")
    core.shutdown()
    return 0


def cmd_skills(args: argparse.Namespace) -> int:
    core = make_core(args)
    print(core.registry.help_text())
    core.shutdown()
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    from .voice import VOICE_INSTALL_HINT, probe

    settings = Settings()
    memory = Memory()
    core = Core(settings, memory, HeadlessHost(verbose=False))
    report = probe(settings)

    def tick(value: bool) -> str:
        return "✓" if value else "✗"

    print(f"JARVIS {VERSION} — diagnostics\n")
    print(f"Platform      : {platform.platform()}")
    print(f"Python        : {platform.python_version()} ({sys.executable})")
    print(f"Data folder   : {jarvis_home()}")
    print(f"Settings file : {settings.path}")
    print(f"Memory file   : {memory.path}")

    # import name -> (purpose, pip name)
    optional = {
        "requests": "LLM + weather + Wikipedia",
        "psutil": "CPU / memory / battery detail",
        "PyQt6": "desktop interface",
        "pyttsx3": "spoken replies",
        "sounddevice": "microphone input",
        "faster_whisper": "offline speech recognition",
        "vosk": "lightweight offline speech recognition",
        "speech_recognition": "Google speech recognition",
        "PIL": "screenshot fallback",
        "mss": "screenshot fallback",
        "graphviz": "rendering flowcharts to PNG",
    }
    pip_names = {
        "PIL": "pillow", "speech_recognition": "SpeechRecognition",
        "faster_whisper": "faster-whisper", "PyQt6": "PyQt6",
    }
    print("\nPython packages")
    for module, purpose in optional.items():
        pip_name = pip_names.get(module, module.replace("_", "-"))
        try:
            __import__(module)
            print(f"  {tick(True)} {module:18s} {purpose}")
        except Exception:
            print(f"  {tick(False)} {module:18s} {purpose}   → pip install {pip_name}")

    print("\nExternal tools")
    for tool, purpose in (("dot", "Graphviz PNG rendering"), ("tesseract", "screen OCR"),
                          ("git", "repository insight"), ("ollama", "local language model")):
        path = shutil.which(tool)
        print(f"  {tick(bool(path))} {tool:10s} {path or '→ not on PATH   (' + purpose + ')'}")

    print("\nBrain")
    print(f"  provider    : {core.brain.provider} ({PROVIDER_LABELS.get(core.brain.provider, '')})")
    print(f"  status      : {core.brain.status()}")
    if core.brain.provider == "ollama":
        models = core.brain.list_ollama_models()
        print(f"  models      : {', '.join(models) if models else 'none found'}")

    print("\nVoice")
    print(f"  output      : {report['tts']}")
    print(f"  input       : {report['stt']}")
    print(f"  microphone  : {report['mic']}")
    print(f"  {report['summary']}")
    if not (report["tts"].startswith(("unavailable",)) and report["mic"] == "unavailable"):
        pass
    else:
        print("\n" + VOICE_INSTALL_HINT)

    print(f"\nSkills loaded : {len(core.registry.skills)}")
    core.shutdown()
    return 0


# ── GUI ──────────────────────────────────────────────────────────────────────

def cmd_gui(args: argparse.Namespace) -> int:
    try:
        from PyQt6.QtWidgets import QApplication
    except Exception as exc:
        print("The desktop interface needs PyQt6:\n\n    pip install PyQt6\n")
        print(f"(import error: {exc})")
        print("\nYou can still use the text modes:  python run_jarvis.py --chat")
        return 2

    from .ui.main_window import JarvisWindow, app_icon

    settings = Settings()
    if args.provider:
        settings["provider"] = args.provider
    if args.model:
        models = dict(settings.as_dict().get("models") or {})
        models[settings.provider] = args.model
        settings["models"] = models
    if args.no_voice:
        settings["voice_replies"] = False
    if args.wake:
        settings["wake_word_enabled"] = True
    settings.save()

    memory = Memory()
    app = QApplication(sys.argv)
    app.setApplicationName("JARVIS")
    app.setApplicationVersion(VERSION)
    app.setStyle("Fusion")

    window = JarvisWindow(settings, memory)
    icon = app_icon(settings.get("accent", "cyan"))
    app.setWindowIcon(icon)
    window.install_tray(icon)
    if settings.get("start_minimised"):
        window.showMinimized()
    else:
        window.show()

    if settings.get("wake_word_enabled") and window.stt.available:
        window.start_wake_listener()

    return app.exec()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.ask:
        return cmd_ask(args)
    if args.chat:
        return cmd_chat(args)
    if args.skills:
        return cmd_skills(args)
    if args.doctor:
        return cmd_doctor(args)
    return cmd_gui(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
