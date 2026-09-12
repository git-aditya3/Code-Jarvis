#!/usr/bin/env python3
"""
JARVIS launcher.

    python run_jarvis.py                     # open the desktop app
    python run_jarvis.py --ask "weather"     # one-shot answer in the terminal
    python run_jarvis.py --chat              # terminal chat
    python run_jarvis.py --doctor            # check what is installed
    python run_jarvis.py --help
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from jarvis.run import main

if __name__ == "__main__":
    raise SystemExit(main())
