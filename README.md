# Code-Jarvis

**JARVIS — a local-first voice assistant for your desktop, with a developer copilot built in.**

Talk to it, type to it, or let it watch your clipboard. It answers out loud, remembers what you
tell it, reviews your code, draws flowcharts of it, and generates reproducible test datasets —
**with no API key required**. Plug in a Groq/OpenAI key or run Ollama locally and the same
assistant starts answering open-ended questions too.

![JARVIS desktop window](docs/jarvis-window.png)

---

## Contents

- [What it does](#what-it-does)
- [Quick start](#quick-start)
- [Talking to it](#talking-to-it)
- [Voice setup](#voice-setup)
- [Adding a language model (optional)](#adding-a-language-model-optional)
- [The skills](#the-skills)
- [Developer tools](#developer-tools)
- [Command line](#command-line)
- [Where your data lives](#where-your-data-lives)
- [Architecture](#architecture)
- [Tests and linting](#tests-and-linting)
- [Troubleshooting](#troubleshooting)
- [Security note](#security-note)
- [Roadmap](#roadmap)
- [Project history](#project-history)
- [License](#license)

---

## What it does

| Area | Examples |
| --- | --- |
| 🎙 **Voice** | Say *"Jarvis, what's my battery at?"* — or enable the wake word and leave it listening. |
| 🧠 **Memory** | *"note that the staging DB rotates on Monday"*, *"add task call the bank"*, *"remember my locker code is 4417"*. |
| ⏰ **Timers** | *"remind me in 25 minutes to stretch"* — speaks up and shows a notification when it fires. |
| 💻 **Code review** | *"review app.py"* / *"review my clipboard"* — offline lint + AST analysis: secrets, `eval`, bare `except`, mutable defaults, injection, complexity, unused imports… |
| 📊 **Flowcharts** | *"make a flowchart of jarvis/core.py"* — Mermaid + Graphviz output saved to disk. |
| 🧪 **Synthetic data** | *"generate 500 rows of patient data as csv"* — deterministic (seedable) datasets in CSV/JSON/JSONL/SQL. |
| 📰 **Briefing** | *"brief me"* — time, tasks, weather and system state in one answer. |
| 🌤 **Weather** | *"will it rain today in Chennai"* — Open-Meteo, no key needed. |
| 📚 **Knowledge** | *"who was Ada Lovelace"* — Wikipedia summaries offline-safe, or the LLM when configured. |
| 🖥 **System** | *"system status"*, *"how much battery is left"*, *"top processes"*, *"free up my disk"* info. |
| 🎛 **Media & apps** | *"volume up"*, *"open chrome"*, *"search youtube for lofi beats"*. |
| 📋 **Clipboard & screen** | *"what's on my clipboard"*, *"take a screenshot"*. |
| 🗂 **Files & repos** | *"find file config.json"*, *"how big is ~/projects"*, *"git status"*, *"list the TODOs in this repo"*. |

Everything in that table works **offline and key-free**. It will never invent an answer it
cannot back up: when a request is outside its skills and no model is configured, it says so and
points you at `/help`.

## Quick start

```bash
git clone https://github.com/git-aditya3/Code-Jarvis.git
cd Code-Jarvis

python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

pip install -r requirements.txt  # optional but recommended
python run_jarvis.py
```

That is the whole install. If you would rather not install anything yet:

```bash
python run_jarvis.py --doctor    # what is available on this machine?
python run_jarvis.py --chat      # text chat, no GUI needed
python run_jarvis.py --ask "what is 18% of 2400"
```

**Requirements:** Python 3.10+. Nothing else is mandatory — JARVIS degrades gracefully and
`--doctor` tells you exactly which optional package unlocks which feature.

## Talking to it

| Shortcut | Action |
| --- | --- |
| `Ctrl` `Space` | Focus the command box |
| `Ctrl` `M` | Hold a conversation with the microphone |
| `Ctrl` `B` | Run the daily briefing |
| `F1` | List every skill |
| `↑` / `↓` in the box | Recall previous commands |
| `Esc` | Hide to the system tray |
| `Ctrl` `Q` | Quit |

Slash commands work in the box, the tray, and the terminal:

```
/help              list every skill
/status            diagnostics: brain, voice, memory
/brief             time + tasks + weather + system
/timers            what is counting down
/memory            memory stats  (/memory export writes a text file)
/clear             clear the conversation (notes and tasks are kept)
/provider groq     switch brain
/llm on|off        quick brain toggle
/voice on|off      spoken replies
/settings          jump to the Settings tab
/quit
```

Useful phrasing:

```
review app.py                          what's on my clipboard
explain jarvis/core.py                 make a flowchart of jarvis/core.py
generate 1000 rows of logs as jsonl    find file config.json in ~/projects
brief me                               remind me in 10 minutes to check the build
note that the demo is Friday           what are my tasks
remember my project is called Jarvis   what do you remember about my project
```

## Voice setup

Voice is optional and auto-detected. Install whichever halves you want:

```bash
pip install pyttsx3                      # 🗣 speech out — uses the OS voice
pip install sounddevice faster-whisper   # 👂 speech in + wake word — fully offline
```

- **Speech out** falls back automatically in this order: `pyttsx3` → Qt text-to-speech (ships
  with PyQt6) → the OS command (`say` on macOS, PowerShell speech on Windows, `espeak` on Linux).
  Pick a specific voice and rate in **Settings → Voice**.
- **Speech in** uses `faster-whisper` locally (no cloud, no key). `vosk` and Google's web API via
  `SpeechRecognition` are supported as alternatives.
- **Wake word** (*"Jarvis, …"*) is a toggle in Settings. It listens in the background with an
  energy gate, tolerates slight mispronunciations ("jarvus", "jarviz"), and can take the rest of
  the sentence in the same breath: *"Jarvis, what's the weather"*.
- **Camera-shy?** Everything voice-related can stay switched off; the text interface is complete.

## Adding a language model (optional)

JARVIS is fully usable without one. With a model configured, anything outside the skill set
becomes answerable, and the code-review and explain skills get a second, deeper pass.

| Provider | How | Notes |
| --- | --- | --- |
| **Groq** | paste a key in **Settings → Language model** | Free tier; very fast. `llama-3.3-70b-versatile` by default. |
| **OpenAI** | paste a key | `gpt-4o-mini` by default. |
| **Ollama** | `ollama serve` + `ollama pull llama3.1` | 100% local and private; nothing leaves your machine. |

Keys are read from the environment first, then from settings:

```bash
export GROQ_API_KEY="..."     # powershell: $env:GROQ_API_KEY="..."
export OPENAI_API_KEY="..."
```

There is **no key in the source code** — see [Security note](#security-note).

## The skills

24 skills ship by default. Run `--skills` or `/help` for the live list; each one is a small class
in [`jarvis/skills.py`](jarvis/skills.py) and [`jarvis/dev_skills.py`](jarvis/dev_skills.py) that
declares regex patterns, a weight and a `run()` method, so adding your own is a dozen lines.

| Skill | Ask it |
| --- | --- |
| Clock | `what's the time`, `date in Tokyo` |
| Calculator | `calculate 15*240+8`, `what is 18% of 2400`, `convert 12 km to miles` |
| Weather | `weather`, `will it rain today`, `forecast for Chennai` |
| Knowledge | `who was Ada Lovelace`, `what is quantum entanglement` |
| Network | `is the internet up`, `what's my ip` |
| System monitor | `system status`, `battery`, `top processes` |
| Media keys | `volume up`, `mute`, `next track` |
| Power | `lock my screen` (shutdown is deliberately left to you) |
| Launcher | `open chrome`, `open downloads`, `search youtube for lofi` |
| Notes / tasks / facts | `note that …`, `add task …`, `what are my tasks` |
| Timers | `set a timer for 10 minutes`, `list timers`, `cancel all timers` |
| Clipboard | `what's on my clipboard`, `copy hello world to clipboard` |
| Screenshot | `take a screenshot` |
| Briefing | `brief me`, `good morning` |
| Files | `find file config.json`, `read ~/notes.md`, `summarise report.md`, `list files in ~/Downloads` |
| Project insight | `git status`, `what changed in this repo`, `list the TODOs` |
| Environment | `what python am i running`, `is docker installed` |
| Diagnostics | `status`, `/status` |

## Developer tools

The original point of this repository — code assistance — is now the strongest part of the
assistant, and it no longer needs OCR, a screenshot or an API key.

**Review** (`review app.py`, `review my clipboard`, or paste code after a colon)

- Secrets: Groq/OpenAI/AWS/Google/GitHub token patterns and hard-coded credentials
- Danger: `eval`/`exec`, `os.system`, `shell=True`, `pickle.loads`, `innerHTML`, `gets`, `strcpy`,
  SQL built by string interpolation
- Python AST pass: bare/`except Exception: pass`, mutable default arguments, `== None`,
  `range(len(x) - 1)` off-by-ones, `while True` with no `break`, long functions, high cyclomatic
  complexity, unused imports, docstring coverage
- Output as a readable summary **or** a Markdown table you can paste into a pull request

**Flowcharts** (`make a flowchart of jarvis/core.py`) walk the real AST — functions, branches,
loops, `try/except`, returns — and emit Mermaid (renders on GitHub, in VS Code, in Obsidian) plus
Graphviz DOT, with PNG rendering when `graphviz` is installed.

**Synthetic data** (`generate 500 rows of patient data as csv`) covers users, transactions, health,
logs, employees and sensors, with a `seed` for reproducible fixtures:

```bash
python run_jarvis.py --ask "generate 200 rows of payment data as sql seed 7"
```

## Command line

```
python run_jarvis.py                     # desktop window (default)
python run_jarvis.py --ask "TEXT"        # one-shot answer, script friendly
python run_jarvis.py --chat              # terminal chat
python run_jarvis.py --skills            # list skills
python run_jarvis.py --doctor            # dependency + voice diagnostics
python run_jarvis.py --provider ollama --model qwen2.5    # one-off overrides
python run_jarvis.py --no-voice --data-dir ./sandbox      # quiet, isolated run
```

## Where your data lives

Everything is local, in `~/.jarvis` (override with `JARVIS_HOME`):

```
~/.jarvis/
├── settings.json      user preferences (0600 — includes any saved API key)
├── memory.json        notes, tasks, facts, conversation history
├── diagrams/          generated .mmd / .dot / .png flowcharts
├── synthetic/         generated datasets
└── screenshots/       captured screenshots
```

Nothing is uploaded anywhere. The only network calls JARVIS ever makes are the ones you ask for:
weather, Wikipedia, your configured LLM provider, and the public-IP lookup. Delete the folder and
JARVIS forgets everything.

![Settings](docs/jarvis-settings.png)

## Architecture

```
run_jarvis.py ──► jarvis/run.py ──► jarvis/ui/main_window.py  (Qt window, HUD)
                                        │
                                        │  implements Host
                                        ▼
   jarvis/core.py ──► jarvis/brain.py       (Groq / OpenAI / Ollama)
        │        ──► jarvis/skills.py       (offline skills)
        │        ──► jarvis/dev_skills.py   (code review, data, diagrams, files, git)
        │        ──► jarvis/memory.py       (notes / tasks / facts / history)
        │        ──► jarvis/voice.py        (TTS, STT, wake word)
        └───────► jarvis/analyzers.py, synthetic.py, flowchart.py
```

Two ideas keep it simple and testable:

1. **The core never imports Qt.** Skills talk to a `Host` — six methods for notifications,
   speech, clipboard, screenshots, opening things and scheduling. The desktop window implements
   `Host`; `HeadlessHost` implements it for the CLI and the test suite.
2. **Every answer is honest about its source.** `Core.ask()` tries slash commands → offline
   skills → the configured LLM → and finally says plainly that it cannot help offline. Each
   reply carries the skill that produced it, which the transcript shows as a small label.

Request flow for `"jarvis, review my clipboard"`: strip wake word → route to the highest-weighted
skill (`clipboard_review`, weight 0.99) → run it → reply rendered in the transcript, spoken
aloud, and stored in conversation history for the next turn's context.

## Tests and linting

```bash
python -m unittest discover -s tests -v    # 44 tests, no network, no real home dir touched
ruff check .                               # configured in pyproject.toml
```

The suite covers routing and skill selection, memory persistence, timer firing, the analyser rule
set, deterministic dataset generation, flowchart generation, wake-word matching, the safe
arithmetic evaluator, settings/key handling and the `Host` contract.

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| Window does not open | `pip install PyQt6`; on headless Linux use `--chat` |
| No speech | `pip install pyttsx3`, or pick a different engine in Settings. Try PyQt6's built-in engine (`qt`). |
| Mic button does nothing | `pip install sounddevice faster-whisper`, then **Re-check voice** |
| Wake word never triggers | Check the wake word in Settings; try "Hey Jarvis". It needs a mic level above the noise gate. |
| LLM answers nothing | Settings → **Test connection**; check the key is saved and the provider matches |
| No PNG for flowcharts | `pip install graphviz` **and** the system Graphviz (`dot`) — Mermaid output works without it |
| Weather fails | No internet, or a city name Open-Meteo cannot geocode — try a larger nearby city |
| Where is my data? | `~/.jarvis`; open it from Settings → Data |

## Security note

⚠️ **The original `code assistance.py` in this repository contained a live Groq API key
hard-coded at line 43.** It has been removed and the script now reads the key from the
environment, but the old key is in the git history and **must be revoked** at
[console.groq.com/keys](https://console.groq.com/keys). Treat any key that has ever been
committed as compromised.

How JARVIS handles secrets now:

- no credentials in the source, ever;
- `GROQ_API_KEY` / `OPENAI_API_KEY` from the environment take precedence over anything stored;
- keys saved through Settings land in `~/.jarvis/settings.json` with `0600` permissions
  (and `~/.jarvis` is never inside this repository);
- the offline code reviewer actively flags committed credentials, so the same mistake gets
  caught next time.

## Roadmap

- [ ] Wake-word model with an always-on low-power detector (currently energy-gated listening)
- [ ] Screen-aware mode: optional OCR review of code visible on screen (the original
      `code assistance.py` idea, reborn as a skill rather than the whole app)
- [ ] Calendar and email skills (ICS first, so it stays key-free)
- [ ] Plugin folder so third-party skills can be dropped in without editing the repo
- [ ] Packaging: `pipx install code-jarvis`, plus a standalone PyInstaller build

## Project history

This started as `code assistance.py` — a Windows-only PyQt6 overlay that OCR'd your screen every
five seconds and sent the extracted code to an LLM. That script is still here (it now reads its
key from the environment), but the idea has been reshaped into a real assistant: the screen-OCR
review loop became an offline analyser plus a clipboard skill, the hard-coded key became a
pluggable provider, and the overlay became a voice-first HUD with memory, skills and tests.

## License

MIT — see [LICENSE](LICENSE).
