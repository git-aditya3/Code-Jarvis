"""
Offline code intelligence.

This is the direct descendant of the original ``code assistance.py`` review
loop, but it no longer needs an API key, a screenshot or Tesseract: given a
snippet or a file it detects the language, parses it where possible, runs a
curated rule set, and returns a structured report (plus a human summary and a
Markdown version you can paste into a PR comment).

LLM review is still available on top when a provider is configured — see
:mod:`jarvis.core`, which asks the brain for deeper suggestions.
"""

from __future__ import annotations

import ast
import json
import re
import tokenize
from dataclasses import dataclass, field
from io import StringIO
from pathlib import Path

SEVERITY_ORDER = {"critical": 0, "warning": 1, "info": 2}
SEVERITY_ICON = {"critical": "✗", "warning": "▲", "info": "·"}

LANGUAGES = {
    "Python": [r"\bdef\s+\w+\s*\(", r"\bimport\s+\w+", r"\belif\b", r"^\s*#(?![a-z]*include)",
               r"\bself\b", r":\s*$"],
    "JavaScript": [r"\bfunction\b", r"\bconst\s+\w+\s*=", r"=>", r"console\.log", r"\blet\s+\w+"],
    "TypeScript": [r":\s*(string|number|boolean)\b", r"\binterface\s+\w+", r"\breadonly\b", r"<\w+>"],
    "C/C++": [r"#include\s*<", r"\bstd::", r"\bnullptr\b", r"\bcout\b", r"->\s*\w+\(\)"],
    "Java": [r"\bpublic\s+class\b", r"System\.out\.print", r"@Override", r"\bextends\b"],
    "Rust": [r"\bfn\s+\w+", r"\blet\s+mut\b", r"println!", r"\bimpl\b"],
    "SQL": [r"\bSELECT\b.+\bFROM\b", r"\bINSERT\s+INTO\b", r"\bCREATE\s+TABLE\b", r"\bWHERE\b"],
    "Shell": [r"^#!", r"\bsudo\b", r"\becho\b", r"\bfi\b|\bdone\b"],
    "HTML/CSS": [r"<html|<div|<span", r"[.#]\w+\s*\{[^}]*:[^}]*;"],
    "JSON": [r'^\s*[\{\[]', r'"[^"]+"\s*:'],
}

SECRET_PATTERNS = [
    (r"(?i)\b(gsk_[A-Za-z0-9]{20,})", "Groq API key"),
    (r"(?i)\b(sk-[A-Za-z0-9]{20,})", "OpenAI-style API key"),
    (r"\bAKIA[0-9A-Z]{16}\b", "AWS access key id"),
    (r"(?i)\bAIza[0-9A-Za-z_\-]{35}\b", "Google API key"),
    (r"\bgh[pousr]_[A-Za-z0-9]{20,}\b", "GitHub token"),
    (r"(?i)(password|passwd|secret|token|api_key)\s*[:=]\s*[\"'][^\"']{6,}[\"']", "hard-coded credential"),
]


@dataclass
class Finding:
    severity: str            # critical | warning | info
    message: str
    line: int = 0
    rule: str = ""
    hint: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "severity": self.severity,
            "message": self.message,
            "line": self.line,
            "rule": self.rule,
            "hint": self.hint,
        }

    def render(self, show_hint: bool = True) -> str:
        where = f"line {self.line}: " if self.line else ""
        text = f"{SEVERITY_ICON.get(self.severity, '•')} {where}{self.message}"
        if show_hint and self.hint:
            text += f"\n    ↳ {self.hint}"
        return text


@dataclass
class CodeReport:
    language: str = "Unknown"
    lines: int = 0
    findings: list[Finding] = field(default_factory=list)
    metrics: dict[str, object] = field(default_factory=dict)
    parsed: bool = True
    parse_error: str = ""

    # ── convenience ──────────────────────────────────────────────────────
    def by_severity(self, severity: str) -> list[Finding]:
        return [f for f in self.findings if f.severity == severity]

    @property
    def worst(self) -> str:
        for level in ("critical", "warning", "info"):
            if self.by_severity(level):
                return level
        return "clean"

    def counts(self) -> dict[str, int]:
        return {level: len(self.by_severity(level)) for level in SEVERITY_ORDER}

    def summary(self, limit: int = 12) -> str:
        counts = self.counts()
        head = (
            f"{self.language} · {self.lines} lines · "
            f"{counts['critical']} critical, {counts['warning']} warnings, {counts['info']} notes"
        )
        if self.metrics:
            bits = [f"{k}: {v}" for k, v in self.metrics.items() if v not in (0, "", None)]
            if bits:
                head += "\n" + " · ".join(bits)
        if self.parse_error:
            head += f"\n✗ Syntax error — {self.parse_error}"
        if not self.findings:
            head += "\n✅ Nothing to flag. Clean code."
            return head
        ordered = sorted(self.findings, key=lambda f: SEVERITY_ORDER.get(f.severity, 3))[:limit]
        body = "\n".join(f.render() for f in ordered)
        hidden = len(self.findings) - len(ordered)
        if hidden > 0:
            body += f"\n…and {hidden} more."
        return head + "\n" + body

    def to_markdown(self) -> str:
        counts = self.counts()
        lines = [
            f"## Code review — {self.language}",
            "",
            f"- Lines: **{self.lines}**",
            f"- Verdict: **{self.worst}**",
            f"- Critical **{counts['critical']}** · Warnings **{counts['warning']}** · Notes **{counts['info']}**",
        ]
        if self.metrics:
            lines.append("- Metrics: " + ", ".join(f"{k}={v}" for k, v in self.metrics.items()))
        if self.parse_error:
            lines += ["", f"> ✗ Parse error: {self.parse_error}"]
        if not self.findings:
            lines += ["", "✅ Nothing to flag."]
        else:
            lines += ["", "| Sev | Line | Finding | Suggestion |", "| --- | --- | --- | --- |"]
            for finding in sorted(self.findings, key=lambda f: (SEVERITY_ORDER.get(f.severity, 3), f.line)):
                lines.append(
                    f"| {SEVERITY_ICON.get(finding.severity, '•')} {finding.severity} "
                    f"| {finding.line or '—'} | {finding.message} | {finding.hint or '—'} |"
                )
        return "\n".join(lines)

    def to_dict(self) -> dict[str, object]:
        return {
            "language": self.language,
            "lines": self.lines,
            "parsed": self.parsed,
            "parse_error": self.parse_error,
            "metrics": self.metrics,
            "findings": [f.to_dict() for f in self.findings],
        }


# ── language detection ───────────────────────────────────────────────────────

# Signals that identify a language outright — they beat the generic score below.
STRONG_SIGNALS: tuple[tuple[str, str], ...] = (
    (r"#include\s*[<\"]", "C/C++"),
    (r"\bstd::|\bcout\s*<<|\bnullptr\b", "C/C++"),
    (r"\bfn\s+\w+\s*\(|\blet\s+mut\b|println!", "Rust"),
    (r"\bpublic\s+(static\s+)?(class|void)\b|System\.out\.print", "Java"),
    (r"\bSELECT\b[\s\S]{0,200}?\bFROM\b|\bINSERT\s+INTO\b|\bCREATE\s+TABLE\b", "SQL"),
    (r"<!DOCTYPE|<html|<div|<span|</\w+>", "HTML/CSS"),
    (r"\bdef\s+\w+\s*\([^)]*\)\s*:|^\s*(from|import)\s+\w+", "Python"),
    (r"=>|\bfunction\s+\w+\s*\(|\bconsole\.log\b", "JavaScript"),
    (r":\s*(string|number|boolean)\b|\binterface\s+\w+\s*\{", "TypeScript"),
)


def detect_language(code: str, filename: str | None = None) -> str:
    """Best-effort language detection from a filename extension or heuristics."""
    if filename:
        suffix = Path(filename).suffix.lower()
        mapped = {
            ".py": "Python", ".pyw": "Python", ".js": "JavaScript", ".mjs": "JavaScript",
            ".jsx": "JavaScript", ".ts": "TypeScript", ".tsx": "TypeScript",
            ".c": "C/C++", ".h": "C/C++", ".cpp": "C/C++", ".cc": "C/C++", ".hpp": "C/C++",
            ".java": "Java", ".rs": "Rust", ".sql": "SQL", ".sh": "Shell", ".bash": "Shell",
            ".json": "JSON", ".html": "HTML/CSS", ".htm": "HTML/CSS", ".css": "HTML/CSS",
        }
        if suffix in mapped:
            return mapped[suffix]

    if not code.strip():
        return "Unknown"
    for pattern, language in STRONG_SIGNALS:
        if re.search(pattern, code, re.MULTILINE):
            return language
    scores: dict[str, float] = {}
    for language, patterns in LANGUAGES.items():
        score = 0.0
        for pattern in patterns:
            if re.search(pattern, code, re.MULTILINE):
                score += 1.0
        scores[language] = score
    # JSON is easy to confirm outright
    stripped = code.strip()
    if stripped[:1] in "{[":
        try:
            json.loads(stripped)
            return "JSON"
        except ValueError:
            pass
    best = max(scores, key=lambda k: scores[k])
    return best if scores[best] >= 2 else "Unknown"


# ── regex rule engine ────────────────────────────────────────────────────────

@dataclass
class Rule:
    rule: str
    severity: str
    pattern: str
    message: str
    hint: str = ""
    languages: tuple[str, ...] = ()
    flags: int = re.MULTILINE

    def apply(self, code: str, language: str, report: CodeReport) -> None:
        if self.languages and language not in self.languages:
            return
        for match in re.finditer(self.pattern, code, self.flags):
            line = code.count("\n", 0, match.start()) + 1
            report.findings.append(
                Finding(self.severity, self.message, line, self.rule, self.hint)
            )


RULES: tuple[Rule, ...] = (
    # secrets — language independent
    *[
        Rule(
            rule="secret",
            severity="critical",
            pattern=pattern,
            message=f"{label} committed in source",
            hint="Move it to an environment variable or a secrets manager and rotate the key.",
            languages=(),
        )
        for pattern, label in SECRET_PATTERNS
    ],
    # shell / process safety
    Rule("shell-injection", "critical", r"\bos\.system\s*\(",
         "os.system() executes a shell command",
         "Use subprocess.run([...], shell=False, check=True) instead.", ("Python",)),
    Rule("shell-true", "warning", r"subprocess\.[A-Za-z_]+\([^)]*shell\s*=\s*True",
         "subprocess called with shell=True",
         "Prefer a list of arguments; shell=True invites command injection.", ("Python",)),
    Rule("eval-exec", "critical", r"\b(eval|exec)\s*\(",
         "eval()/exec() runs arbitrary code",
         "Parse the data instead (ast.literal_eval, json.loads) unless it is truly required.", ("Python",)),
    Rule("pickle", "warning", r"\bpickle\.loads?\s*\(",
         "pickle can execute code during deserialisation",
         "Use JSON or another data-only format for untrusted input.", ("Python",)),
    # python specifics
    Rule("bare-except", "warning", r"^\s*except\s*:",
         "Bare 'except:' swallows every error, including KeyboardInterrupt",
         "Catch the specific exception you expect, e.g. except ValueError as exc:"),
    Rule("except-pass", "warning", r"except[^\n]*:\s*\n\s*pass\b",
         "Exception silently ignored",
         "Log it, re-raise it, or handle it explicitly."),
    Rule("eq-none", "info", r"[=!]=\s*None\b",
         "Comparing to None with == / !=",
         "Use 'is None' / 'is not None'.", ("Python",)),
    Rule("mutable-default", "warning", r"def\s+\w+\([^)]*=\s*(\[\]|\{\})",
         "Mutable default argument",
         "Default to None and build the list/dict inside the function.", ("Python",)),
    Rule("wildcard-import", "info", r"^\s*from\s+[\w.]+\s+import\s+\*",
         "Wildcard import hides where names come from",
         "Import the specific names you use."),
    Rule("global-stmt", "info", r"^\s*global\s+\w+",
         "Module-level 'global' makes state harder to reason about",
         "Prefer passing values or storing state on an object.", ("Python",)),
    Rule("todo", "info", r"#\s*(TODO|FIXME|XXX|HACK)\b.*",
         "Unfinished work left in the code",
         "Track it in an issue; the note will outlive everyone's memory."),
    Rule("print-debug", "info", r"^\s*print\s*\(",
         "print() debugging left in",
         "Use the logging module so output can be controlled.", ("Python",)),
    # javascript / typescript
    Rule("js-var", "info", r"\bvar\s+\w+", "`var` is function-scoped", "Use let/const.", ("JavaScript", "TypeScript")),
    Rule("js-loose-eq", "warning", r"[^=!<>]==[^=]", "Loose equality coerces types",
         "Use === / !==.", ("JavaScript", "TypeScript")),
    Rule("js-eval", "critical", r"\beval\s*\(", "eval() executes arbitrary code",
         "Avoid it; parse data with JSON.parse.", ("JavaScript", "TypeScript")),
    Rule("js-innerhtml", "warning", r"\.innerHTML\s*=", "innerHTML can inject markup",
         "Use textContent, or sanitise before assigning.", ("JavaScript", "TypeScript")),
    Rule("js-console", "info", r"\bconsole\.(log|debug)\s*\(", "Debug logging left in",
         "Remove it or route it through a logger.", ("JavaScript", "TypeScript")),
    # C / C++
    Rule("c-gets", "critical", r"\bgets\s*\(", "gets() cannot bound its input",
         "Use fgets() with an explicit size.", ("C/C++",)),
    Rule("c-strcpy", "warning", r"\bstrcpy\s*\(|\bstrcat\s*\(", "Unbounded string copy",
         "Use strncpy/strncat or snprintf with the buffer size.", ("C/C++",)),
    Rule("c-scanf", "warning", r'\bscanf\s*\(\s*"%s"', "scanf(\"%s\") has no width limit",
         "Use a width, e.g. scanf(\"%63s\", buf).", ("C/C++",)),
    # java
    Rule("java-string-eq", "warning", r"\w+\s*==\s*\"", "String compared with ==",
         "Use .equals() for value comparison.", ("Java",)),
    Rule("java-empty-catch", "warning", r"catch\s*\([^)]*\)\s*\{\s*\}",
         "Empty catch block hides failures", "At least log the exception.", ("Java",)),
    # sql
    Rule("sql-select-star", "info", r"\bSELECT\s+\*", "SELECT * pulls every column",
         "Name the columns you need — it survives schema changes.", ("SQL",)),
    Rule("sql-delete", "warning", r"\bDELETE\s+FROM\s+\w+\s*(;|$)", "DELETE without WHERE",
         "Add a WHERE clause, or you will empty the table.", ("SQL",)),
    Rule("sql-drop", "warning", r"\bDROP\s+(TABLE|DATABASE)\b", "Destructive DDL",
         "Double-check the target and keep a backup.", ("SQL",)),
    # generality
    Rule("long-line", "info", r"^.{131,}$", "Line longer than 130 characters",
         "Wrap it — long lines are hard to review."),
    Rule("trailing-space", "info", r"[ \t]+$", "Trailing whitespace", "Most editors strip this on save."),
    Rule("mixed-indent", "warning", r"^ \t|\t ", "Mixed tabs and spaces for indentation",
         "Pick one and configure your editor."),
)


def _line_of(code: str, index: int) -> int:
    return code.count("\n", 0, index) + 1


# ── python AST analysis ──────────────────────────────────────────────────────

class _PythonAstVisitor(ast.NodeVisitor):
    def __init__(self, code: str, report: CodeReport) -> None:
        self.code = code
        self.report = report
        self.functions = 0
        self.classes = 0
        self.complexity: dict[str, int] = {}
        self._stack: list[str] = []
        self.unused_hints: dict[str, int] = {}

    # complexity accounting
    def _branch(self) -> None:
        if self._stack:
            self.complexity[self._stack[-1]] += 1

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    def _visit_function(self, node: ast.AST) -> None:
        self.functions += 1
        name = node.name
        self._stack.append(name)
        self.complexity.setdefault(name, 1)
        args = node.args
        if args.defaults:
            for default in args.defaults:
                if isinstance(default, (ast.List, ast.Dict, ast.Set)):
                    self.report.findings.append(Finding(
                        "warning", f"Function '{name}' uses a mutable default argument",
                        node.lineno, "ast-mutable-default",
                        "Default to None and create the container inside the function.",
                    ))
        if node.end_lineno and node.end_lineno - node.lineno > 60:
            self.report.findings.append(Finding(
                "warning", f"Function '{name}' is {node.end_lineno - node.lineno} lines long",
                node.lineno, "ast-long-function",
                "Split it into smaller, named steps.",
            ))
        self.generic_visit(node)
        self._stack.pop()

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.classes += 1
        self.generic_visit(node)

    def visit_If(self, node: ast.If) -> None:
        self._branch()
        self.generic_visit(node)

    def visit_For(self, node: ast.For) -> None:
        self._branch()
        if (isinstance(node.iter, ast.Call) and isinstance(node.iter.func, ast.Name)
                and node.iter.func.id == "range" and node.iter.args):
            arg = node.iter.args[0]
            if (isinstance(arg, ast.BinOp) and isinstance(arg.op, ast.Sub)
                    and isinstance(arg.right, ast.Constant) and arg.right.value == 1):
                self.report.findings.append(Finding(
                    "warning", f"range(len(...) - 1) in the loop over '{ast.unparse(node.target)}' may skip the last item",
                    node.lineno, "ast-off-by-one",
                    "Iterate the sequence directly, or use enumerate()/zip().",
                ))
        self.generic_visit(node)

    def visit_While(self, node: ast.While) -> None:
        self._branch()
        if isinstance(node.test, ast.Constant) and node.test.value is True:
            has_break = any(isinstance(n, ast.Break) for n in ast.walk(node))
            if not has_break:
                self.report.findings.append(Finding(
                    "critical", "while True loop with no break — this cannot terminate",
                    node.lineno, "ast-infinite-loop", "Add an exit condition or a break."))
        self.generic_visit(node)

    def visit_Try(self, node: ast.Try) -> None:
        for handler in node.handlers:
            if handler.type is None:
                self.report.findings.append(Finding(
                    "warning", "Bare 'except:' catches everything",
                    handler.lineno, "ast-bare-except",
                    "Catch the specific exception classes you can handle.",
                ))
            elif (isinstance(handler.type, ast.Name) and handler.type.id == "Exception"
                  and len(handler.body) == 1 and isinstance(handler.body[0], ast.Pass)):
                    self.report.findings.append(Finding(
                        "warning", "'except Exception: pass' hides real failures",
                        handler.lineno, "ast-silent-except", "Log the exception or re-raise it."))
        self.generic_visit(node)

    def visit_Compare(self, node: ast.Compare) -> None:
        for op, comparator in zip(node.ops, node.comparators, strict=False):
            if isinstance(op, (ast.Eq, ast.NotEq)) and isinstance(comparator, ast.Constant) \
                    and comparator.value is None:
                self.report.findings.append(Finding(
                    "info", "Comparison to None with == / !=", node.lineno, "ast-eq-none",
                    "Use 'is None' / 'is not None'."))
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else (func.id if isinstance(func, ast.Name) else "")
        if name in {"eval", "exec"}:
            self.report.findings.append(Finding(
                "critical", f"{name}() executes arbitrary code", node.lineno, "ast-eval",
                "Use ast.literal_eval or a real parser."))
        if name == "input" and self._stack:
            self.report.findings.append(Finding(
                "info", "input() inside a function blocks callers", node.lineno, "ast-input",
                "Pass the value in as a parameter instead."))
        if name in {"execute", "executemany"} and node.args:
            first = node.args[0]
            if isinstance(first, (ast.JoinedStr, ast.BinOp)):
                self.report.findings.append(Finding(
                    "critical", "SQL built with string interpolation can be injected into",
                    node.lineno, "ast-sql-injection",
                    "Use parameterised queries (cursor.execute(sql, params))."))
        self.generic_visit(node)


def _python_checks(code: str, report: CodeReport) -> None:
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        report.parsed = False
        line = exc.lineno or 0
        report.parse_error = f"{exc.msg} (line {line})"
        report.findings.append(Finding(
            "critical", f"Syntax error: {exc.msg}", line, "syntax", "Fix this before anything else runs."))
        return

    visitor = _PythonAstVisitor(code, report)
    visitor.visit(tree)
    report.metrics.update({
        "functions": visitor.functions,
        "classes": visitor.classes,
        "imports": sum(isinstance(n, (ast.Import, ast.ImportFrom)) for n in ast.walk(tree)),
    })
    hot = {name: score for name, score in visitor.complexity.items() if score > 10}
    for name, score in sorted(hot.items(), key=lambda kv: -kv[1])[:3]:
        report.findings.append(Finding(
            "warning", f"'{name}' has high cyclomatic complexity ({score})", 0, "ast-complexity",
            "Break the branches into helper functions."))
    if hot:
        report.metrics["max_complexity"] = max(hot.values())

    # unused imports (text-level heuristic: name is never referenced again)
    imported: dict[str, int] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported[(alias.asname or alias.name).split(".")[0]] = node.lineno
        elif isinstance(node, ast.ImportFrom):
            if node.module == "__future__":  # compiler directives, not names
                continue
            for alias in node.names:
                if alias.name != "*":
                    imported[alias.asname or alias.name] = node.lineno
    used = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    used |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    for name, line in sorted(imported.items(), key=lambda kv: kv[1]):
        # cheap text check first, then the AST name set
        if name not in used and len(re.findall(rf"\b{re.escape(name)}\b", code)) <= 1:
            report.findings.append(Finding(
                "info", f"Import '{name}' looks unused", line, "ast-unused-import",
                "Remove it — or confirm it is imported for a side effect."))
    report.metrics["unused_imports"] = sum(1 for f in report.findings if f.rule == "ast-unused-import")

    # docstring coverage for public functions
    public = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
              and not n.name.startswith("_")]
    documented = [n for n in public if ast.get_docstring(n)]
    if public:
        report.metrics["documented_functions"] = f"{len(documented)}/{len(public)}"


def _comment_ratio(code: str, language: str) -> float:
    lines = [line.strip() for line in code.splitlines() if line.strip()]
    if not lines:
        return 0.0
    marker = "--" if language == "SQL" else ("//" if language in {"JavaScript", "TypeScript", "Java", "C/C++", "Rust"} else "#")
    comments = sum(1 for line in lines if line.startswith(marker))
    return round(comments / len(lines), 3)


# ── public API ───────────────────────────────────────────────────────────────

def analyze_code(code: str, filename: str | None = None, language: str | None = None) -> CodeReport:
    """Run the full offline review and return a :class:`CodeReport`."""
    code = code.replace("\r\n", "\n")
    language = language or detect_language(code, filename)
    report = CodeReport(language=language, lines=len(code.splitlines()))

    if not code.strip():
        report.findings.append(Finding("info", "Nothing to review — the input is empty", 0, "empty"))
        return report

    # The AST pass below catches these more precisely for Python, so skip the
    # regex versions to avoid reporting the same issue twice.
    ast_covered = {"bare-except", "except-pass", "mutable-default", "eval-exec", "eq-none"}
    for rule in RULES:
        if language == "Python" and rule.rule in ast_covered:
            continue
        rule.apply(code, language, report)

    if language == "Python":
        _python_checks(code, report)
    elif language == "JSON":
        try:
            json.loads(code)
        except ValueError as exc:
            report.parsed = False
            report.parse_error = str(exc)
            report.findings.append(Finding("critical", f"Invalid JSON: {exc}", 0, "json"))
    elif language == "Shell":
        report.metrics["pipes"] = code.count("|")

    report.metrics["comment_ratio"] = _comment_ratio(code, language)
    report.metrics["longest_line"] = max((len(line) for line in code.splitlines()), default=0)

    # final de-duplication: same rule on the same line = one finding
    seen: set[tuple[str, int, str]] = set()
    unique: list[Finding] = []
    for finding in report.findings:
        token = (finding.rule, finding.line, finding.message[:40])
        if token in seen:
            continue
        seen.add(token)
        unique.append(finding)
    report.findings = unique
    report.findings.sort(key=lambda f: (SEVERITY_ORDER.get(f.severity, 3), f.line))
    return report


def analyze_file(path: str | Path) -> CodeReport:
    target = Path(path).expanduser()
    if not target.is_file():
        report = CodeReport(language="Unknown")
        report.parsed = False
        report.parse_error = f"No such file: {target}"
        report.findings.append(Finding("critical", f"No such file: {target}", 0, "io"))
        return report
    try:
        code = target.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        report = CodeReport(language="Unknown")
        report.parsed = False
        report.parse_error = str(exc)
        return report
    report = analyze_code(code, filename=target.name)
    report.metrics["file"] = target.name
    report.metrics["size_kb"] = round(target.stat().st_size / 1024, 1)
    return report


def extract_code_from_text(text: str) -> str:
    """Pull the largest fenced code block out of pasted text, else return it all."""
    blocks = re.findall(r"```[a-zA-Z0-9_+-]*\n(.*?)```", text, re.DOTALL)
    if blocks:
        return max(blocks, key=len).strip("\n")
    return text


def review_text_offline(code: str, filename: str | None = None) -> str:
    """Convenience: analyse and render, used by the ``review code`` skill."""
    return analyze_code(extract_code_from_text(code), filename).summary()


def quick_syntax_check(path: str | Path) -> tuple[bool, str]:
    """Compile a Python file without importing it (used by the file watcher skill)."""
    target = Path(path).expanduser()
    try:
        with tokenize.open(str(target)) as fh:
            source = fh.read()
        compile(source, str(target), "exec")
        return True, ""
    except (OSError, SyntaxError, UnicodeDecodeError) as exc:
        return False, str(exc)


def format_exception_text(exc: BaseException) -> str:
    """Render an exception the way the assistant would read it out loud."""
    import traceback

    buffer = StringIO()
    traceback.print_exception(type(exc), exc, exc.__traceback__, file=buffer)
    return buffer.getvalue().strip()
