"""
Cross-platform screen capture + OCR.

Shared by every backend's ``screen_text()`` so the "read my screen" feature (the
original ``code assistance.py`` idea) works the same way everywhere. Uses mss or
Pillow for the capture and Tesseract for the recognition; both are already used
by the legacy script, so nothing new is required.
"""

from __future__ import annotations

import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

TESSERACT_HINTS = {
    "win32": r"install from https://github.com/UB-Mannheim/tesseract/wiki and set "
             r"TESSERACT_CMD (e.g. C:\Program Files\Tesseract-OCR\tesseract.exe)",
    "darwin": "brew install tesseract",
    "linux": "sudo apt install tesseract-ocr   (or your distro's package)",
}


@dataclass
class ScreenCapture:
    ok: bool
    image: object | None = None
    path: str = ""
    error: str = ""
    size: tuple[int, int] = (0, 0)


def capture_screen(path: str | Path | None = None) -> ScreenCapture:
    """Grab the whole desktop. Tries mss first, then Pillow."""
    target = Path(path) if path else None
    try:
        import mss
        from PIL import Image

        with mss.mss() as sct:
            monitor = sct.monitors[0]
            shot = sct.grab(monitor)
            image = Image.frombytes("RGB", shot.size, shot.rgb)
        if target:
            target.parent.mkdir(parents=True, exist_ok=True)
            image.save(target)
        return ScreenCapture(True, image, str(target) if target else "", "", image.size)
    except Exception:
        pass

    try:
        from PIL import ImageGrab

        image = ImageGrab.grab()
        if target:
            target.parent.mkdir(parents=True, exist_ok=True)
            image.save(target)
        return ScreenCapture(True, image, str(target) if target else "", "", image.size)
    except Exception as exc:
        return ScreenCapture(False, None, "", f"{type(exc).__name__}: {exc}")


def tesseract_available() -> tuple[bool, str]:
    try:
        import pytesseract

        version = pytesseract.get_tesseract_version()
        return True, f"tesseract {version}"
    except Exception as exc:
        return False, str(exc)


def ocr(image, lang: str = "eng") -> tuple[bool, str]:
    """Run OCR on a PIL image. Returns (ok, text_or_error)."""
    try:
        import pytesseract
    except Exception:
        return False, ("OCR needs pytesseract: pip install pytesseract "
                       f"({TESSERACT_HINTS.get(sys.platform, '')})")

    try:
        text = pytesseract.image_to_string(image, lang=lang, config="--psm 3 --oem 3")
        return True, text.strip()
    except Exception as exc:
        hint = TESSERACT_HINTS.get(sys.platform, "")
        return False, f"OCR failed: {exc}" + (f" — {hint}" if hint else "")


def available() -> bool:
    """Can we capture the screen at all on this machine?"""
    for module in ("mss", "PIL"):
        try:
            __import__(module)
        except Exception:
            return False
    return True


def locate(image, needle: str, lang: str = "eng") -> list[dict]:
    """Find ``needle`` in a screenshot and return where it is.

    Matching is case-insensitive and works word by word, so “save” finds the save
    button even inside “Save changes”. Each hit is a dict with the matched text and
    the centre point (``x``, ``y``) plus the bounding box, ready to be clicked.
    """
    needle = (needle or "").strip().lower()
    if not needle or image is None:
        return []
    try:
        import pytesseract
        from pytesseract import Output
    except Exception:
        return []
    try:
        data = pytesseract.image_to_data(image, lang=lang, config="--psm 3",
                                         output_type=Output.DICT)
    except Exception:
        return []

    words: list[dict] = []
    count = len(data.get("text", []))
    for index in range(count):
        text = str(data["text"][index]).strip()
        try:
            confidence = float(data["conf"][index])
        except (TypeError, ValueError):
            confidence = -1
        if not text or confidence < 30:
            continue
        left, top = int(data["left"][index]), int(data["top"][index])
        width, height = int(data["width"][index]), int(data["height"][index])
        words.append({
            "text": text, "left": left, "top": top, "width": width, "height": height,
            "x": left + width // 2, "y": top + height // 2,
            "line": (data["block_num"][index], data["par_num"][index], data["line_num"][index]),
        })

    hits: list[dict] = []
    # 1. whole phrases on one line (“save as”, “file edit view”)
    lines: dict[tuple, list[dict]] = {}
    for word in words:
        lines.setdefault(word["line"], []).append(word)
    for members in lines.values():
        members.sort(key=lambda item: item["left"])
        joined = " ".join(item["text"] for item in members)
        position = joined.lower().find(needle)
        if position >= 0:
            hits.append(_span_hit(members, joined, position, needle))
            break
    # 2. single words (“save”, “cancel”)
    if not hits:
        for word in words:
            if needle == word["text"].lower() or needle in word["text"].lower():
                hits.append(dict(word))
    return hits


def _span_hit(members: list[dict], joined: str, position: int, needle: str) -> dict:
    """Turn a character offset inside a line into the bounding box of those words."""
    cursor = 0
    chosen: list[dict] = []
    for word in members:
        start = cursor
        end = cursor + len(word["text"])
        if end > position and start < position + len(needle):
            chosen.append(word)
        cursor = end + 1
    if not chosen:
        chosen = members[:1]
    left = min(item["left"] for item in chosen)
    top = min(item["top"] for item in chosen)
    right = max(item["left"] + item["width"] for item in chosen)
    bottom = max(item["top"] + item["height"] for item in chosen)
    return {
        "text": joined[position:position + len(needle)],
        "left": left, "top": top, "width": right - left, "height": bottom - top,
        "x": (left + right) // 2, "y": (top + bottom) // 2,
    }


def screen_text() -> tuple[bool, str, str]:
    """Capture and OCR in one call: (ok, text, error_or_path)."""
    shot = capture_screen()
    if not shot.ok:
        return False, "", (f"Screen capture failed ({shot.error}). Install mss/Pillow, "
                           "or run JARVIS on a machine with a display.")
    ok, result = ocr(shot.image)
    if not ok:
        return False, "", result
    return True, result, shot.path


def tesseract_command() -> str | None:
    """Locate the tesseract binary (Windows installs are often not on PATH)."""
    found = shutil.which("tesseract")
    if found:
        return found
    if sys.platform.startswith("win"):
        for candidate in (
            r"C:\Program Files\Tesseract-OCR\tesseract.exe",
            r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
        ):
            if Path(candidate).exists():
                return candidate
    return None
