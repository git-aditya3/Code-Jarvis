#!/usr/bin/env python3
"""
⚡ Screen Code Assistant for Windows
- Scans screen every 5s for code using OCR
- Shows AI-powered errors + suggestions in a floating overlay
- Chat tab lets you ask questions about screen code
"""

# ── INSTALL ────────────────────────────────────────────────────────────────────
# pip install mss pytesseract pillow pyqt6 openai requests pywin32
# Tesseract: https://github.com/UB-Mannheim/tesseract/wiki
# ──────────────────────────────────────────────────────────────────────────────
import sys, os, time, re, json, difflib, threading, textwrap, tempfile, argparse
import numpy as np
import pandas as pd
import requests
from PyQt6.QtCore  import Qt, QTimer, pyqtSignal, QObject, QThread, QPoint
from PyQt6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout,
    QTextEdit, QLineEdit, QPushButton, QLabel,
    QTabWidget, QSizeGrip, QDialog, QFormLayout, QMessageBox, QCheckBox, QFileDialog, QListWidget
)
from PyQt6.QtGui import QFont, QPixmap
import mss
from PIL import Image
import pytesseract

try:
    import win32gui
    HAS_WIN32 = True
except ImportError:
    HAS_WIN32 = False

# ══════════════════════════════════════════════════════════════════════════════
# ⚙️  CONFIG — Edit these before running
# ══════════════════════════════════════════════════════════════════════════════
TESSERACT_CMD  = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
pytesseract.pytesseract.tesseract_cmd = TESSERACT_CMD

LLM_PROVIDER   = "groq"            # "groq" | "openai" | "ollama"
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "YOUR_OPENAI_KEY")
OPENAI_MODEL   = "gpt-4o-mini"     # cheap and fast
GROQ_API_KEY   = os.getenv("GROQ_API_KEY", "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")  # get one free at https://console.groq.com/keys
GROQ_MODEL     = "llama-3.1-70b-versatile"
OLLAMA_MODEL   = "codellama"       # for local Ollama
OLLAMA_URL     = "http://localhost:11434/api/chat"

SCAN_INTERVAL  = 5000              # ms between scans
CHANGE_THRESH  = 0.10              # skip LLM if code changed < 10%
MAX_TOKENS     = 900
# ═══════════════════════════════════════════════════════════════════════ ═══════


# ── OCR & Screen Capture ──────────────────────────────────────────────────────

def screenshot_to_pil() -> Image.Image:
    with mss.mss() as sct:
        mon = sct.monitors[0]            # full virtual desktop
        img = sct.grab(mon)
        return Image.frombytes("RGB", img.size, img.rgb)


def ocr_image(pil: Image.Image) -> str:
    # PSM 3 = fully automatic, good for mixed screen content [web:16]
    return pytesseract.image_to_string(pil, config="--psm 3 --oem 3")


CODE_KEYWORDS = (
    "def ", "class ", "import ", "from ", "return ", "elif ", "yield ",
    "function ", "const ", "let ", "var ", "=>", "async ", "await ",
    "#include", "std::", "namespace ", "nullptr",
    "public ", "private ", "static ", "@Override", "System.out",
    "SELECT ", "INSERT ", "UPDATE ", "CREATE TABLE",
    "fn ", "pub ", "impl ", "struct ", "enum ",
)
CODE_SYMBOLS = re.compile(r'[{}\[\]();=<>+\-*/%&|^~!]')


def extract_code_blocks(raw: str) -> str:
    lines = raw.splitlines()
    kept = []
    for ln in lines:
        score  = len(CODE_SYMBOLS.findall(ln))
        score += 4 if re.match(r'^(\s{2,}|\t)', ln) else 0
        score += 6 if any(k in ln for k in CODE_KEYWORDS) else 0
        if score >= 3:
            kept.append(ln)
    return "\n".join(kept[-300:])          # last 300 relevant lines


def get_active_window_title() -> str:
    if HAS_WIN32:
        hwnd = win32gui.GetForegroundWindow()
        return win32gui.GetWindowText(hwnd)
    return "Unknown"


# ── Language Detection ────────────────────────────────────────────────────────

def detect_language(code: str) -> str:
    checks = {
        "Python":     ["def ", "import ", "print(", "elif ", ":"],
        "JavaScript": ["function ", "const ", "let ", "=>", "console."],
        "TypeScript": ["interface ", ": string", ": number", "readonly "],
        "C/C++":      ["#include", "std::", "->", "nullptr", "cout"],
        "Java":       ["public class", "System.out", "@Override", "extends "],
        "Rust":       ["fn main", "let mut", "println!", "->"],
        "SQL":        ["SELECT ", "FROM ", "WHERE ", "INSERT INTO"],
    }
    scores = {l: sum(k in code for k in kws) for l, kws in checks.items()}
    best   = max(scores, key=scores.get)
    return best if scores[best] > 0 else "Unknown"


# ── LLM Interface (OpenAI + Ollama) ──────────────────────────────────────────

def call_llm(code: str, question: str = None) -> dict:
    if not code.strip():
        return {"errors": "⚠️ No code detected on screen.", "suggestions": "", "answer": ""}

    lang = detect_language(code)

    if question:
        system  = (
            f"You are an expert coding assistant. Language detected: {lang}.\n"
            "Answer the user's question about the code on their screen. "
            "Be concise. Use code blocks with ``` when needed."
        )
        user_msg = f"Code on screen:\n```{lang.lower()}\n{code[:3000]}\n```\n\nQuestion: {question}"
    else:
        system  = (
            f"You are an expert code reviewer. Language: {lang}.\n"
            "Respond in strict JSON with exactly two keys:\n"
            '  "errors":      string — list bugs/type errors/syntax issues. Use "✅ No errors found." if clean.\n'
            '  "suggestions": string — 2-3 improvement tips with short explanations.\n'
            "Use ✅ ✗ 💡 emojis. Keep it under 220 words total."
        )
        user_msg = f"Code:\n```{lang.lower()}\n{code[:3000]}\n```"

    try:
        if LLM_PROVIDER == "openai":
            import openai
            client  = openai.OpenAI(api_key=OPENAI_API_KEY)
            kwargs  = dict(
                model    = OPENAI_MODEL,
                messages = [{"role": "system", "content": system},
                            {"role": "user",   "content": user_msg}],
                max_tokens  = MAX_TOKENS,
                temperature = 0.2,
            )
            if not question:
                kwargs["response_format"] = {"type": "json_object"}
            resp = client.chat.completions.create(**kwargs)
            text = resp.choices.message.content

        elif LLM_PROVIDER == "groq":
            if not GROQ_API_KEY or "YOUR_GROQ_KEY" in GROQ_API_KEY:
                return {"errors": "❌ Missing GROQ_API_KEY. Set the environment variable GROQ_API_KEY (free key at console.groq.com).", "suggestions": "", "answer": ""}
            payload = {
                "model": GROQ_MODEL,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user_msg},
                ],
                "max_tokens": MAX_TOKENS,
                "temperature": 0.2,
            }
            # groq supports OpenAI-compatible endpoint
            headers = {
                "Authorization": f"Bearer {GROQ_API_KEY}",
                "Content-Type": "application/json",
            }
            r = requests.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers=headers,
                json=payload,
                timeout=60,
            )
            r.raise_for_status()
            text = r.json()["choices"][0]["message"]["content"]

        elif LLM_PROVIDER == "ollama":
            payload = {
                "model":   OLLAMA_MODEL,
                "messages":[{"role": "system", "content": system},
                            {"role": "user",   "content": user_msg}],
                "stream":  False,
            }
            r    = requests.post(OLLAMA_URL, json=payload, timeout=60)
            r.raise_for_status()
            text = r.json()["message"]["content"]
        else:
            return {"errors": "❌ LLM provider not configured.", "suggestions": "", "answer": ""}

    except Exception as e:
        return {"errors": f"❌ LLM Error: {e}", "suggestions": "", "answer": ""}

    if question:
        return {"errors": "", "suggestions": "", "answer": text}

    try:
        clean  = re.sub(r"```(?:json)?\n?", "", text).strip().rstrip("```")
        parsed = json.loads(clean)
        return {
            "errors":      parsed.get("errors", ""),
            "suggestions": parsed.get("suggestions", ""),
            "answer":      "",
        }
    except Exception:
        return {"errors": text, "suggestions": "", "answer": ""}


def generate_flow_dot(code: str, lang: str = None, highlight: str | None = None):
    """Simple heuristic extractor that converts assignments, calls, and returns
    into a DOT graph representing data flow between variables and functions.
    This is intentionally lightweight and best-effort for small snippets.
    """
    # if language is Python, use AST for better accuracy
    if lang == "Python":
        try:
            import ast
            tree = ast.parse(code)
            funcs = set()
            vars = set()
            edges = set()
            node_lines = {}

            class Visitor(ast.NodeVisitor):
                def __init__(self):
                    self.current_func = None

                def visit_FunctionDef(self, node: ast.FunctionDef):
                    funcs.add(node.name)
                    node_lines[node.name] = node.lineno
                    prev = self.current_func
                    self.current_func = node.name
                    self.generic_visit(node)
                    self.current_func = prev

                def visit_Return(self, node: ast.Return):
                    if node.value is not None and self.current_func:
                        names = [n.id for n in ast.walk(node.value) if isinstance(n, ast.Name)]
                        for n in names:
                            edges.add((self.current_func, n))
                            vars.add(n)

                def visit_Assign(self, node: ast.Assign):
                    targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
                    value = node.value
                    names_in_value = [n.id for n in ast.walk(value) if isinstance(n, ast.Name)]
                    for t in targets:
                        vars.add(t)
                        for v in names_in_value:
                            if v != t:
                                edges.add((v, t))
                                vars.add(v)
                    # function call assignments
                    if isinstance(value, ast.Call) and isinstance(value.func, ast.Name):
                        fn = value.func.id
                        funcs.add(fn)
                        for t in targets:
                            edges.add((fn, t))
                            node_lines.setdefault(fn, getattr(value.func, 'lineno', None))

                def visit_Expr(self, node: ast.Expr):
                    # calls as statements
                    if isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Name):
                        fn = node.value.func.id
                        funcs.add(fn)
                        args = [n.id for n in ast.walk(node.value) if isinstance(n, ast.Name)]
                        for a in args:
                            edges.add((a, fn))
                            vars.add(a)

            Visitor().visit(tree)

            # build dot
            dot = "digraph G {\n  rankdir=LR;\n  node [shape=box];\n"
            for f in sorted(funcs):
                style = 'style=filled, fillcolor="#bd93f9"'
                if highlight and highlight == f:
                    style = 'style=filled, fillcolor="#50fa7b"'
                dot += f'  "{f}" [shape=oval, {style}];\n'
            for v in sorted(vars - funcs):
                style = ''
                if highlight and highlight == v:
                    style = ', style=filled, fillcolor="#50fa7b"'
                dot += f'  "{v}" [shape=box{style}];\n'
            for a, b in edges:
                dot += f'  "{a}" -> "{b}";\n'
            dot += "}\n"
            return dot, node_lines
        except Exception:
            # fall back to heuristic below
            pass

    # fallback heuristic parser (original implementation)
    lines = code.splitlines()
    funcs = set()
    vars = set()
    edges = set()
    current_func = None

    for ln in lines:
        s = ln.strip()
        if not s:
            continue
        m_def = re.match(r"def\s+([A-Za-z_]\w*)\s*\((.*?)\)\s*:\s*", s)
        if m_def:
            fn = m_def.group(1)
            funcs.add(fn)
            current_func = fn
            continue

        m_ret = re.match(r"return\s+(.+)", s)
        if m_ret and current_func:
            rhs = m_ret.group(1)
            vars_in = re.findall(r"\b([A-Za-z_]\w*)\b", rhs)
            for v in vars_in:
                edges.add((current_func, v))
                vars.add(v)
            continue

        m_assign = re.match(r"([A-Za-z_]\w*)\s*=\s*(.+)", s)
        if m_assign:
            lhs = m_assign.group(1)
            rhs = m_assign.group(2)
            vars.add(lhs)
            m_call = re.match(r"([A-Za-z_]\w*)\s*\((.*)\)", rhs)
            if m_call:
                fn = m_call.group(1)
                funcs.add(fn)
                edges.add((fn, lhs))
                args = m_call.group(2)
                args_vars = re.findall(r"\b([A-Za-z_]\w*)\b", args)
                for a in args_vars:
                    if a != lhs and not re.match(r"^\d+$", a):
                        edges.add((a, fn))
                        vars.add(a)
            else:
                vars_rhs = re.findall(r"\b([A-Za-z_]\w*)\b", rhs)
                for v in vars_rhs:
                    if v != lhs and not re.match(r"^\d+$", v):
                        edges.add((v, lhs))
                        vars.add(v)
            continue

        m_call_stmt = re.match(r"([A-Za-z_]\w*)\s*\((.*)\)", s)
        if m_call_stmt:
            fn = m_call_stmt.group(1)
            funcs.add(fn)
            args = m_call_stmt.group(2)
            args_vars = re.findall(r"\b([A-Za-z_]\w*)\b", args)
            for a in args_vars:
                if not re.match(r"^\d+$", a):
                    edges.add((a, fn))
                    vars.add(a)

    dot = "digraph G {\n  rankdir=LR;\n  node [shape=box];\n"
    for f in sorted(funcs):
        style = 'style=filled, fillcolor="#bd93f9"'
        if highlight and highlight == f:
            style = 'style=filled, fillcolor="#50fa7b"'
        dot += f'  "{f}" [shape=oval, {style}];\n'
    for v in sorted(vars - funcs):
        style = ''
        if highlight and highlight == v:
            style = ', style=filled, fillcolor="#50fa7b"'
        dot += f'  "{v}" [shape=box{style}];\n'
    for a, b in edges:
        dot += f'  "{a}" -> "{b}";\n'
    dot += "}\n"
    return dot, {}


def render_dot_to_png(dot: str) -> str | None:
    """Try to render DOT to a PNG using python-graphviz. Returns path to PNG
    or None if rendering failed.
    """
    try:
        from graphviz import Source
        src = Source(dot)
        png_bytes = src.pipe(format="png")
        if not png_bytes:
            return None
        out = os.path.join(tempfile.gettempdir(), f"code_flow_{int(time.time())}.png")
        with open(out, "wb") as f:
            f.write(png_bytes)
        return out
    except Exception:
        return None


def format_code_html(code: str, lang: str | None = None) -> str | None:
    """Return syntax-highlighted HTML for the code if pygments is available; otherwise None."""
    if not code:
        return None
    try:
        import pygments
        from pygments import highlight
        from pygments.lexers import get_lexer_by_name, guess_lexer
        from pygments.formatters import HtmlFormatter

        formatter = HtmlFormatter(nowrap=True)
        lexer = None
        if lang and lang.lower() != "unknown":
            try:
                lexer = get_lexer_by_name(lang.lower())
            except Exception:
                lexer = None
        if lexer is None:
            try:
                lexer = guess_lexer(code)
            except Exception:
                lexer = None
        if lexer is None:
            return None
        return f"<pre style='font-family: Consolas, monospace; margin:0'>{highlight(code, lexer, formatter)}</pre>"
    except Exception:
        return None


def generate_synthetic_health_data(rows: int = 20000) -> str:
    """Create synthetic data from pre-existing data feched from internet and sliced """
    rng = np.random.default_rng(42)
    age = rng.integers(18, 85, size=rows)
    resting_hr = rng.normal(72, 10, size=rows).clip(45, 130)
    sbp = rng.normal(120, 15, size=rows).clip(85, 200)
    dbp = rng.normal(78, 10, size=rows).clip(50, 130)
    bmi = rng.normal(26, 5, size=rows).clip(16, 45)

    risk_score = (
        0.015 * (age - 50) +
        0.03 * (resting_hr - 70) +
        0.04 * (sbp - 120) +
        0.03 * (dbp - 80) +
        0.02 * (bmi - 25)
    )
    risk_score += rng.normal(0, 0.5, size=rows)
    labels = np.digitize(risk_score, bins=[1.5, 3.5])  # 0,1,2 classes

    df = pd.DataFrame({
        "age": age,
        "resting_hr": resting_hr.round(1),
        "sbp": sbp.round(1),
        "dbp": dbp.round(1),
        "bmi": bmi.round(1),
        "risk_label": labels,
    })

    out = os.path.join(tempfile.gettempdir(), f"synthetic_health_{rows}_{int(time.time())}.csv")
    df.to_csv(out, index=False)
    return out


# ── Worker Thread ─────────────────────────────────────────────────────────────

class ScanWorker(QObject):
    result_ready  = pyqtSignal(dict)
    chat_ready    = pyqtSignal(str)
    status_update = pyqtSignal(str)
    scanning      = pyqtSignal(bool)

    def __init__(self):
        super().__init__()
        self._last_code = ""

    def scan(self):
        # notify UI that scanning is starting
        try:
            self.scanning.emit(True)
        except Exception:
            pass
        self.status_update.emit("🔍 Scanning screen…")
        try:
            pil  = screenshot_to_pil()
            raw  = ocr_image(pil)
            code = raw  # show exactly what was recognized on screen

            # Skip LLM if code hasn't changed much
            ratio = 1.0
            if self._last_code:
                ratio = 1 - difflib.SequenceMatcher(None, self._last_code, code).ratio()

            if ratio >= CHANGE_THRESH or not self._last_code:
                self._last_code = code
                self.status_update.emit("🤖 Analyzing with AI…")
                result = call_llm(code)
                result["window"] = get_active_window_title()
                result["lang"]   = detect_language(code)
                result["code"]   = code
                self.result_ready.emit(result)
                self.status_update.emit(f"✅ Updated @ {time.strftime('%H:%M:%S')}")
            else:
                self.status_update.emit(f"⏸ No changes @ {time.strftime('%H:%M:%S')}")
        except Exception as e:
            self.status_update.emit(f"❌ {str(e)[:80]}")
        finally:
            try:
                self.scanning.emit(False)
            except Exception:
                pass

    def ask_chat(self, question: str):
        self.status_update.emit("💬 Thinking…")
        try:
            result = call_llm(self._last_code, question=question)
            self.chat_ready.emit(result.get("answer", "(no answer)"))
            self.status_update.emit(f"✅ Chat answered @ {time.strftime('%H:%M:%S')}")
        except Exception as e:
            self.chat_ready.emit(f"❌ Error: {e}")


# ── Preferences helpers ───────────────────────────────────────────────────────

PREFS_FILE_NAME = "code_assistant_prefs.json"

def prefs_path() -> str:
    base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, PREFS_FILE_NAME)

def load_prefs() -> dict:
    try:
        p = prefs_path()
        if os.path.exists(p):
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {}

def save_prefs(data: dict):
    try:
        p = prefs_path()
        with open(p, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass


def ensure_api_key(prefs: dict) -> bool:
    """Ensure GROQ_API_KEY is set. Uses prefs if present; otherwise prompts the user.
    Returns True if a key is available, False if the user cancelled.
    """
    global GROQ_API_KEY

    # use saved key if present and no env key provided
    saved_key = prefs.get("groq_api_key")
    if (not GROQ_API_KEY or "YOUR_GROQ_KEY" in GROQ_API_KEY) and saved_key:
        GROQ_API_KEY = saved_key
        return True

    # if already configured, nothing to do
    if GROQ_API_KEY and "YOUR_GROQ_KEY" not in GROQ_API_KEY:
        return True

    dlg = ApiKeyDialog(initial_key=saved_key or "")
    if dlg.exec() != QDialog.DialogCode.Accepted:
        return False
    key, remember = dlg.values()
    GROQ_API_KEY = key
    if remember:
        prefs["groq_api_key"] = key
    else:
        prefs.pop("groq_api_key", None)
    return True


def ensure_tesseract() -> bool:
    """Check Tesseract path and prompt the user if missing."""
    if os.path.exists(TESSERACT_CMD):
        return True
    msg = (
        "Tesseract not found at:\n"
        f"  {TESSERACT_CMD}\n\n"
        "Install from https://github.com/UB-Mannheim/tesseract/wiki or update TESSERACT_CMD.\n"
        "Continue anyway? (OCR will fail without Tesseract.)"
    )
    reply = QMessageBox.question(
        None,
        "Tesseract missing",
        msg,
        QMessageBox.StandardButton.No | QMessageBox.StandardButton.Yes,
        QMessageBox.StandardButton.No,
    )
    return reply == QMessageBox.StandardButton.Yes

# ── Stylesheet ────────────────────────────────────────────────────────────────

STYLE = """
QWidget {
    background: #ffffff;
    color: #000000;
    font-family: 'Segoe UI', Arial, sans-serif;
    font-size: 12px;
}
QTabWidget::pane  { border: 1px solid #000000; border-radius: 6px; }
QTabBar::tab      { background: #ffffff; color: #000000; padding: 6px 14px; border-radius: 6px 6px 0 0; border: 1px solid #000000; margin-right: 4px; }
QTabBar::tab:selected { background: #d32f2f; color: #ffffff; font-weight: bold; }
QTextEdit         { background: #ffffff; border: 1px solid #000000; border-radius: 6px; padding: 8px; color: #000000; }
QLineEdit         { background: #ffffff; border: 1px solid #000000; border-radius: 6px; padding: 6px 10px; color: #000000; }
QLineEdit:focus   { border: 1px solid #d32f2f; }
QPushButton       { background: #d32f2f; color: #ffffff; border: none; border-radius: 6px;
                    padding: 6px 14px; font-weight: bold; }
QPushButton:hover { background: #ff4d4d; }
QPushButton#close { background: #000000; color: #ffffff; padding: 4px 9px; font-size: 11px; }
QPushButton#mini  { background: #ffffff; color: #000000; padding: 4px 9px; font-size: 11px; border: 1px solid #000000; }
QPushButton#clear { background: #ffffff; color: #d32f2f; border: 1px solid #d32f2f; }
QLabel#status     { color: #000000; font-size: 11px; padding: 2px 8px; }
QLabel#title      { color: #d32f2f; font-weight: bold; font-size: 13px; }
QSizeGrip { width: 14px; height: 14px; }
"""


class UserInfoDialog(QDialog):
    def __init__(self, parent=None, initial_name: str = "", initial_email: str = "", initial_remember: bool = False):
        super().__init__(parent)
        self.setWindowTitle("User details")
        self.setModal(True)
        self.name_input = QLineEdit()
        self.email_input = QLineEdit()
        self.name_input.setText(initial_name)
        self.email_input.setText(initial_email)
        self.remember_cb = QCheckBox("Remember me")
        self.remember_cb.setChecked(initial_remember)

        form = QFormLayout(self)
        form.addRow("Name:", self.name_input)
        form.addRow("Email:", self.email_input)
        form.addRow(self.remember_cb)

        btn = QPushButton("OK")
        btn.clicked.connect(self._on_ok)
        form.addRow(btn)

    def _on_ok(self):
        name = self.name_input.text().strip()
        email = self.email_input.text().strip()
        if not name:
            QMessageBox.warning(self, "Missing name", "Please enter your name.")
            return
        if not email or "@" not in email:
            QMessageBox.warning(self, "Invalid email", "Please enter a valid email address.")
            return
        self.accept()

    def values(self):
        return (
            self.name_input.text().strip(),
            self.email_input.text().strip(),
            bool(self.remember_cb.isChecked()),
        )


class ApiKeyDialog(QDialog):
    def __init__(self, parent=None, initial_key: str = ""):
        super().__init__(parent)
        self.setWindowTitle("Groq API Key")
        self.setModal(True)
        self.input = QLineEdit()
        self.input.setEchoMode(QLineEdit.EchoMode.Normal)
        self.input.setText(initial_key)
        self.remember_cb = QCheckBox("Save key locally (plaintext)")
        self.remember_cb.setChecked(True)

        form = QFormLayout(self)
        form.addRow("API Key:", self.input)
        form.addRow(self.remember_cb)

        btn = QPushButton("OK")
        btn.clicked.connect(self._on_ok)
        form.addRow(btn)

    def _on_ok(self):
        key = self.input.text().strip()
        if not key:
            QMessageBox.warning(self, "Missing key", "Please paste your Groq API key (free at console.groq.com).")
            return
        self.accept()

    def values(self):
        return self.input.text().strip(), bool(self.remember_cb.isChecked())


# ── Title Bar (draggable) ─────────────────────────────────────────────────────

class TitleBar(QWidget):
    def __init__(self, parent):
        super().__init__(parent)    
        self._parent   = parent
        self._drag_pos = None
        self.setFixedHeight(34)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 0, 6, 0)
        lay.setSpacing(5)

        icon  = QLabel("⚡"); icon.setFont(QFont("Segoe UI Emoji", 13))
        title = QLabel("Code Assistant"); title.setObjectName("title")

        mini  = QPushButton("─"); mini.setObjectName("mini");  mini.setFixedSize(24, 20)
        close = QPushButton("✕"); close.setObjectName("close"); close.setFixedSize(24, 20)
        clear = QPushButton("🗑"); clear.setObjectName("clear"); clear.setFixedSize(24, 20)
        clear.setToolTip("Clear saved user info")
        mini.clicked.connect(parent.showMinimized)
        close.clicked.connect(parent.close)
        clear.clicked.connect(parent.clear_saved_prefs)

        lay.addWidget(icon)
        lay.addWidget(title)
        lay.addStretch()
        lay.addWidget(clear)
        lay.addWidget(mini)
        lay.addWidget(close)

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = e.globalPosition().toPoint() - self._parent.frameGeometry().topLeft()

    def mouseMoveEvent(self, e):
        if self._drag_pos and e.buttons() == Qt.MouseButton.LeftButton:
            self._parent.move(e.globalPosition().toPoint() - self._drag_pos)   #[1]


# ── Main Floating Window ──────────────────────────────────────────────────────

class AssistantWindow(QWidget):
    _chat_signal = pyqtSignal(str)
    _scan_signal = pyqtSignal()

    def __init__(self, user_name: str = None, user_email: str = None):
        super().__init__()
        self.user_name = user_name
        self.user_email = user_email
        self.setWindowFlags(
            Qt.WindowType.Tool |
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowStaysOnTopHint    # always on top[11]
        )
        self.resize(500, 430)
        self.setStyleSheet(STYLE)
        self.setMinimumSize(360, 280)

        # Position: bottom-right corner of primary screen
        screen = QApplication.primaryScreen().geometry()
        self.move(screen.width() - 520, screen.height() - 470)

        self._build_ui()
        # Show collected user info in the status bar
        if self.user_name or self.user_email:
            info = f"User: {self.user_name or ''}"
            if self.user_email:
                info += f" • {self.user_email}"
            self.status.setText(info)

        self._setup_worker()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 4)
        root.setSpacing(0)

        # Draggable title bar
        root.addWidget(TitleBar(self))

        # Status line + refresh
        status_row = QHBoxLayout()
        self.status = QLabel("Starting…"); self.status.setObjectName("status")
        refresh_btn = QPushButton("Refresh now")
        refresh_btn.setObjectName("refresh")
        refresh_btn.clicked.connect(self._request_scan)
        status_row.addWidget(self.status, stretch=1)
        status_row.addWidget(refresh_btn)
        root.addLayout(status_row)

        # ── Tabs ──────────────────────────────────────────────────────────────
        tabs = QTabWidget()
        root.addWidget(tabs, stretch=1)

        # Tab 1 — Errors
        self.errors_view = QTextEdit()
        self.errors_view.setReadOnly(True)
        self.errors_view.setPlaceholderText("Errors & bugs will appear here…")
        tabs.addTab(self.errors_view, "🐛 Errors")

        # Tab 2 — Suggestions
        self.sugg_view = QTextEdit()
        self.sugg_view.setReadOnly(True)
        self.sugg_view.setPlaceholderText("Improvement suggestions…")
        tabs.addTab(self.sugg_view, "💡 Suggestions")

        # Tab 3 — Detected Code (filtered)
        self.code_view = QTextEdit()
        self.code_view.setReadOnly(True)
        self.code_view.setPlaceholderText("Extracted code from screen…")
        tabs.addTab(self.code_view, "📄 Code")

        # Tab 4 — Flowchart
        flow_w = QWidget()
        flow_l = QVBoxLayout(flow_w)
        flow_l.setContentsMargins(4, 4, 4, 4)

        self.flow_img = QLabel()
        self.flow_img.setMinimumHeight(200)
        self.flow_img.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.flow_nodes_list = QListWidget()
        self.flow_nodes_list.setMaximumHeight(100)
        self.flow_nodes_list.itemClicked.connect(lambda it: self._on_node_selected(it.text()))

        self.flow_dot = QTextEdit()
        self.flow_dot.setReadOnly(False)
        self.flow_dot.setPlaceholderText("DOT graph (editable)…")

        btn_row2 = QHBoxLayout()
        render_btn = QPushButton("Render")
        save_btn   = QPushButton("Save DOT")
        render_btn.clicked.connect(lambda: self._render_flow())
        save_btn.clicked.connect(lambda: self._save_dot())
        btn_row2.addWidget(render_btn)
        btn_row2.addWidget(save_btn)

        flow_l.addWidget(self.flow_img)
        flow_l.addWidget(self.flow_nodes_list)
        flow_l.addWidget(self.flow_dot)
        flow_l.addLayout(btn_row2)

        tabs.addTab(flow_w, "🔀 Flowchart")

        # Tab 5 — Data
        data_w = QWidget()
        data_l = QVBoxLayout(data_w)
        data_l.setContentsMargins(4, 4, 4, 4)

        self.data_status = QTextEdit()
        self.data_status.setReadOnly(True)
        self.data_status.setPlaceholderText("Synthetic data generated from actual data sets ")

        gen_btn = QPushButton("Generate data")
        gen_btn.clicked.connect(self._generate_data)

        data_l.addWidget(self.data_status)
        data_l.addWidget(gen_btn)

        tabs.addTab(data_w, "📊 Data")

        # Tab 4 — Chat
        chat_w   = QWidget()
        chat_lay = QVBoxLayout(chat_w)
        chat_lay.setContentsMargins(4, 4, 4, 4)

        self.chat_history = QTextEdit()
        self.chat_history.setReadOnly(True)
        self.chat_history.setPlaceholderText("Ask anything about the code on screen…")

        row = QHBoxLayout()
        self.chat_input = QLineEdit()
        self.chat_input.setPlaceholderText("Type a question and press Enter…")
        self.chat_input.returnPressed.connect(self._send_chat)
        send_btn = QPushButton("Send")
        send_btn.clicked.connect(self._send_chat)
        row.addWidget(self.chat_input, stretch=1)
        row.addWidget(send_btn)

        chat_lay.addWidget(self.chat_history, stretch=1)
        chat_lay.addLayout(row)
        tabs.addTab(chat_w, "💬 Chat")

        # Resize grip (bottom-right)
        grip_row = QHBoxLayout()
        grip_row.addStretch()
        grip_row.addWidget(
            QSizeGrip(self), 0,
            Qt.AlignmentFlag.AlignBottom | Qt.AlignmentFlag.AlignRight
        )
        root.addLayout(grip_row)

        # transient overlay shown during scanning (delayed to avoid flicker)
        self.scan_overlay = QLabel("", self)
        self.scan_overlay.setObjectName("scan_overlay")
        self.scan_overlay.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.scan_overlay.setStyleSheet(
            "background: rgba(255,255,255,0.96); color: #000000; font-weight: bold; "
            "border: 2px solid #d32f2f; border-radius: 8px; padding: 12px;"
        )
        self.scan_overlay.hide()

        # timer used to delay showing the overlay by 2 seconds
        self._scan_delay_timer = QTimer(self)
        self._scan_delay_timer.setSingleShot(True)
        self._scan_delay_timer.timeout.connect(lambda: self._show_scan_overlay_if_still_scanning())
        self._is_scanning = False

    def _setup_worker(self):
        self.thread = QThread()
        self.worker = ScanWorker()
        self.worker.moveToThread(self.thread)

        # attach user info to worker for future use
        if hasattr(self, 'user_name'):
            self.worker.user_name = self.user_name
        if hasattr(self, 'user_email'):
            self.worker.user_email = self.user_email

        self.worker.result_ready.connect(self._on_result)
        self.worker.chat_ready.connect(self._on_chat_answer)
        self.worker.status_update.connect(self.status.setText)
        self._chat_signal.connect(self.worker.ask_chat)
        self.worker.scanning.connect(self._on_scanning)
        self._scan_signal.connect(self.worker.scan)

        self.thread.start()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.worker.scan)
        self.timer.start(SCAN_INTERVAL)                    #  mss is fast enough for this[12]
        QTimer.singleShot(800, self.worker.scan)           # immediate first scan

    def _request_scan(self):
        try:
            self.status.setText("🔄 Manual refresh…")
            self._scan_signal.emit()
        except Exception:
            pass

    def _render_flow(self):
        dot = self.flow_dot.toPlainText().strip()
        if not dot:
            QMessageBox.information(self, "No DOT", "No DOT graph to render.")
            return
        out = render_dot_to_png(dot)
        if not out:
            QMessageBox.warning(self, "Render failed", "Could not render graph. Install python-graphviz and system graphviz.")
            return
        try:
            pix = QPixmap(out)
            self.flow_img.setPixmap(pix.scaled(self.flow_img.width(), self.flow_img.height(), Qt.AspectRatioMode.KeepAspectRatio))
        except Exception as e:
            QMessageBox.warning(self, "Display failed", f"Could not display rendered image: {e}")

    def _on_node_selected(self, name: str):
        # regenerate DOT with the selected node highlighted
        try:
            code = self.code_view.toPlainText()
            lang = detect_language(code)
            dot, mapping = generate_flow_dot(code, lang=lang, highlight=name)
            self.flow_dot.setPlainText(dot)
            out = render_dot_to_png(dot)
            if out:
                pix = QPixmap(out)
                self.flow_img.setPixmap(pix.scaled(self.flow_img.width(), self.flow_img.height(), Qt.AspectRatioMode.KeepAspectRatio))

            # if mapping provides line numbers, navigate code view to first occurrence
            if isinstance(mapping, dict) and mapping.get(name):
                lineno = mapping.get(name)
                if lineno:
                    # move cursor to line
                    text = self.code_view.toPlainText()
                    lines = text.splitlines()
                    pos = sum(len(l) + 1 for l in lines[: max(0, lineno-1)])
                    cursor = self.code_view.textCursor()
                    cursor.setPosition(pos)
                    self.code_view.setTextCursor(cursor)
                    self.code_view.setFocus()
        except Exception:
            pass

    def _save_dot(self):
        path, _ = QFileDialog.getSaveFileName(self, "Save DOT", os.path.expanduser("~"), "DOT files (*.dot);;All files (*.*)")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(self.flow_dot.toPlainText())
            QMessageBox.information(self, "Saved", f"DOT saved to: {path}")
        except Exception as e:
            QMessageBox.warning(self, "Save failed", f"Could not save DOT: {e}")

    def _generate_data(self):
        try:
            path = generate_synthetic_health_data()
            self.data_status.setPlainText(f"✅ Generated synthetic data at: {path}")
            QMessageBox.information(self, "Data generated", f"Saved to: {path}")
        except Exception as e:
            self.data_status.setPlainText(f"❌ Failed to generate data: {e}")
            QMessageBox.warning(self, "Data generation failed", str(e))

    def _on_scanning(self, on: bool):
        try:
            if on:
                # start delayed timer to show overlay after 2s
                self._is_scanning = True
                self._scan_delay_timer.start(2000)
            else:
                # cancel pending show and hide overlay immediately
                self._is_scanning = False
                try:
                    if self._scan_delay_timer.isActive():
                        self._scan_delay_timer.stop()
                except Exception:
                    pass
                self.scan_overlay.hide()
        except Exception:
            pass

    def _show_scan_overlay_if_still_scanning(self):
        try:
            if getattr(self, '_is_scanning', False):
                self.scan_overlay.setText("🔍 Scanning screen…")
                self.scan_overlay.show()
                self.scan_overlay.raise_()
        except Exception:
            pass

    def resizeEvent(self, e):
        super().resizeEvent(e)
        try:
            # position overlay within the main window (below title/status)
            if hasattr(self, 'scan_overlay'):
                x = 10
                y = 44
                w = max(120, self.width() - 20)
                h = max(40, self.height() - 80)
                self.scan_overlay.setGeometry(x, y, w, 60)
        except Exception:
            pass

    def clear_saved_prefs(self):
        try:
            p = prefs_path()
            if os.path.exists(p):
                os.remove(p)
            QMessageBox.information(self, "Cleared", "Saved user info cleared.")
        except Exception as e:
            QMessageBox.warning(self, "Error", f"Could not remove prefs: {e}")

        # Update UI and clear stored values
        try:
            self.status.setText("Saved info cleared.")
        except Exception:
            pass
        self.user_name = ""
        self.user_email = ""
        if hasattr(self, 'worker'):
            try:
                self.worker.user_name = ""
                self.worker.user_email = ""
            except Exception:
                pass

    def _on_result(self, data: dict):
        hdr = f"📂 {data.get('window','')}\n🔤 Language: {data.get('lang','')}\n{'─'*38}\n\n"
        self.errors_view.setPlainText(hdr + (data.get("errors","").strip()      or "✅ No errors detected."))
        self.sugg_view.setPlainText(  hdr + (data.get("suggestions","").strip() or "💡 No suggestions."))
        code_txt = data.get("code", "")[:8000]
        lang = data.get("lang", None)
        html = format_code_html(code_txt, lang=lang)
        if html:
            self.code_view.setHtml(html)
        else:
            self.code_view.setPlainText(code_txt)
        # generate a flow DOT for the detected code and show it
        try:
            code = data.get("code", "")
            lang = data.get("lang", None)
            dot, mapping = generate_flow_dot(code, lang=lang)
            self.flow_dot.setPlainText(dot)
            # populate node list
            self.flow_nodes_list.clear()
            for n in sorted(list(mapping.keys()) if mapping else []):
                self.flow_nodes_list.addItem(n)
            # try to auto-render but ignore failures
            out = render_dot_to_png(dot)
            if out:
                pix = QPixmap(out)
                self.flow_img.setPixmap(pix.scaled(self.flow_img.width(), self.flow_img.height(), Qt.AspectRatioMode.KeepAspectRatio))
        except Exception:
            pass

    def _send_chat(self):
        q = self.chat_input.text().strip()
        if not q:
            return
        self.chat_history.append(f"<b style='color:#bd93f9'>You:</b> {q}<br>")
        self.chat_input.clear()
        self._chat_signal.emit(q)

    def _on_chat_answer(self, answer: str):
        self.chat_history.append(
            f"<b style='color:#50fa7b'>Assistant:</b> {answer}<br>"
            f"<span style='color:#44475a'>{'─'*40}</span><br>"
        )


# ── Entry Point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Code Assistant (GUI or CLI)")
    parser.add_argument("--cli", action="store_true", help="Run one-shot headless OCR and exit")
    parser.add_argument("--llm", action="store_true", help="Call LLM in CLI mode (requires API keys)")
    args = parser.parse_args()

    if args.cli:
        # headless one-shot: check tesseract, run OCR, print results
        if not os.path.exists(TESSERACT_CMD):
            print(f"Tesseract not found at: {TESSERACT_CMD}")
            sys.exit(1)
        # one-shot run
        pil = screenshot_to_pil()
        raw = ocr_image(pil)
        code = extract_code_blocks(raw)
        lang = detect_language(code)
        print("Detected language:", lang)
        print("--- Extracted code (first 2000 chars) ---")
        print(code[:2000])
        if args.llm:
            print("\nCalling LLM (this may require configured API keys)...")
            res = call_llm(code)
            print(json.dumps(res, indent=2, ensure_ascii=False))
        sys.exit(0)

    app = QApplication(sys.argv)
    app.setStyle("Fusion")

    # Check Tesseract presence
    if not ensure_tesseract():
        sys.exit(1)
    # Load saved prefs and prompt user for name/email before starting scans
    prefs = load_prefs()
    initial_name = prefs.get("name", "")
    initial_email = prefs.get("email", "")
    initial_remember = bool(initial_name or initial_email)

    dlg = UserInfoDialog(
        initial_name=initial_name,
        initial_email=initial_email,
        initial_remember=initial_remember,
    )
    if dlg.exec() != QDialog.DialogCode.Accepted:
        sys.exit(0)
    name, email, remember = dlg.values()

    # update prefs for user info
    prefs["name"] = name if remember else ""
    prefs["email"] = email if remember else ""
    if not remember:
        prefs.pop("groq_api_key", None)  # optional: keep api unless user unchecks remember? we keep unless cleared elsewhere

    # ensure API key (prompt if missing)
    if not ensure_api_key(prefs):
        sys.exit(0)

    # persist prefs
    save_prefs(prefs)

    win = AssistantWindow(user_name=name, user_email=email)
    win.show()
    sys.exit(app.exec())
