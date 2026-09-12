"""Allow ``python -m jarvis`` as an alias for the launcher."""

from __future__ import annotations

import sys

from .run import main

if __name__ == "__main__":
    sys.exit(main())
