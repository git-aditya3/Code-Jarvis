"""
Developer skills — the part of the old ``code assistance.py`` that survived,
rebuilt to run on the clipboard, on files, or on code you paste in.
"""

from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path

from .analyzers import analyze_code, analyze_file, detect_language, extract_code_from_text
from .flowchart import build_diagram, describe_python_structure, graphviz_available
from .skills import Skill, SkillContext, SkillResult
from .synthetic import generate, list_kinds, resolve_kind, to_sql_schema

CODE_EXTENSIONS = {
    ".py", ".pyw", ".js", ".mjs", ".jsx", ".ts", ".tsx", ".java", ".c", ".h", ".cpp", ".cc",
    ".hpp", ".cs", ".go", ".rs", ".rb", ".php", ".swift", ".kt", ".sql", ".sh", ".bash",
    ".json", ".yaml", ".yml", ".toml", ".html", ".css", ".scss", ".md",
}


def _resolve_path(raw: str) -> Path:
    """Turn a spoken path into a real one (~ expansion, quotes, common folders)."""
    cleaned = (raw or "").strip().strip("\"'`")
    cleaned = re.sub(r"^(the )?(file|folder|directory|path)\s+", "", cleaned, flags=re.IGNORECASE)
    cleaned = cleaned.strip()
    aliases = {
        "downloads": "~/Downloads", "documents": "~/Documents", "desktop": "~/Desktop",
        "pictures": "~/Pictures", "home": "~", "project": ".",
    }
    if cleaned.lower() in aliases:
        cleaned = aliases[cleaned.lower()]
    cleaned = re.sub(r"\s+(folder|directory|file)$", "", cleaned, flags=re.IGNORECASE)
    return Path(os.path.expanduser(cleaned))


def _is_probably_code(path: Path) -> bool:
    return path.suffix.lower() in CODE_EXTENSIONS


# ════════════════════════════════════════════════════════════════════════════
#  Code review
# ════════════════════════════════════════════════════════════════════════════

class CodeReviewSkill(Skill):
    name = "code_review"
    title = "Code review"
    description = "offline lint/review of a file, the clipboard, or pasted code"
    examples = ("review app.py", "review this code: def f(): ...", "check the code in ~/project/main.py")
    patterns = (
        (r"\b(review|analyse|analyze|lint|check|audit|scan)\b.*\b(code|file|script|function|module|snippet|this|clipboard)\b", 0.95),
        (r"\b(any|find) (bugs?|errors?|issues?|problems?)\b.*\b(code|file|script|here|this)\b", 0.92),
        (r"\bwhat('s| is) wrong with (this|the) (code|file|script)\b", 0.95),
        (r"\breview (this|the) (code|file|script)\b", 0.98),
        (r"\bcode review\b", 0.95),
    )
    priority = 7

    def run(self, text: str, ctx: SkillContext) -> SkillResult:
        path_match = re.search(
            r"\b(?:review|analyse|analyze|lint|check|audit|scan)\s+(?:the\s+)?(?:code|file|script|module|snippet)?\s*"
            r"((?:~|\.|/|[A-Za-z]:)[^\s]+|[\w./-]+\.(?:py|js|ts|java|c|cpp|rs|go|rb|php|sql|sh|json|html|css))\b",
            text, re.IGNORECASE,
        )
        inline_block = bool(re.search(r"```", text))
        colon_code = re.search(r":\s*(\S.*)$", text, re.DOTALL)

        if inline_block:
            code = extract_code_from_text(text)
            if code.strip():
                return self._review(code, None, ctx)

        if colon_code:
            candidate = colon_code.group(1).strip()
            looks_like_code = bool(re.search(
                r"[{}()\[\];=]|\b(def|class|import|from|return|function|const|let|var|public|void|"
                r"SELECT|printf|echo|fn|impl)\b", candidate))
            if looks_like_code:
                code = extract_code_from_text(candidate)
                if code.strip():
                    return self._review(code, None, ctx)

        if path_match:
            target = _resolve_path(path_match.group(1))
            if target.is_file():
                return self._review_file(target, ctx)
            if not re.search(r"\bclipboard\b", text, re.IGNORECASE):
                return SkillResult(
                    text=f"No file at {target}. Copy the code and say “review my clipboard” instead.",
                    speak="I could not find that file.", ok=False,
                )

        content = ctx.host.get_clipboard()
        if content.strip():
            return self._review(extract_code_from_text(content), "clipboard", ctx)
        if re.search(r"\bclipboard\b", text, re.IGNORECASE):
            return SkillResult(
                text="Your clipboard is empty — copy some code first, then say “review my clipboard”.",
                speak="Your clipboard is empty.", ok=False,
            )
        return SkillResult(
            text="Give me something to review: “review app.py”, “review my clipboard”, "
                 "or paste code after a colon.",
            speak="What should I review?", ok=False,
        )

    # ── helpers ──────────────────────────────────────────────────────────
    def _review_file(self, target: Path, ctx: SkillContext) -> SkillResult:
        if target.stat().st_size > 2_000_000:
            return SkillResult(text=f"{target.name} is too large to review ({target.stat().st_size/1e6:.1f} MB).",
                               ok=False, speak="That file is too large to review.")
        if not _is_probably_code(target):
            try:
                code = target.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                return SkillResult(text=f"Could not read {target}: {exc}", ok=False)
            suffix = target.suffix or "no extension"
            extra = " Ask me to “summarise the file” for a closer look." if ctx.llm_ready() else ""
            return SkillResult(
                text=(
                    f"{target.name} does not look like source code ({suffix}). "
                    f"It has {len(code.splitlines())} lines and {len(code.split())} words.{extra}"
                ),
                speak=f"{target.name} is not a code file.",
            )
        report = analyze_file(target)
        text_out = f"{target}\n" + report.summary()
        deeper = self._deeper_review(report, target, ctx)
        return SkillResult(
            text=text_out + deeper,
            speak=f"Reviewed {target.name}. "
                  f"{report.counts()['critical']} critical, {report.counts()['warning']} warnings.",
            data={"report": report.to_dict(), "path": str(target)},
            followups=["explain that file", "make a flowchart of it"],
        )

    def _review(self, code: str, label: str | None, ctx: SkillContext) -> SkillResult:
        report = analyze_code(code, filename=label + ".txt" if label == "clipboard" else None)
        headline = f"Review of {'the clipboard' if label == 'clipboard' else 'your snippet'}"
        text_out = f"{headline}\n{report.summary()}"
        deeper = self._deeper_review(report, None, ctx, code=code)
        return SkillResult(
            text=text_out + deeper,
            speak=f"{report.language} review done. "
                  f"{report.counts()['critical']} critical issues, {report.counts()['warning']} warnings.",
            data={"report": report.to_dict()},
        )

    def _deeper_review(self, report, target: Path | None, ctx: SkillContext, code: str = "") -> str:
        """Optional LLM pass — only when a key is configured."""
        if not ctx.llm_ready():
            return ""
        snippet = code or ""
        if not snippet and target is not None:
            try:
                snippet = target.read_text(encoding="utf-8", errors="replace")
            except OSError:
                snippet = ""
        if not snippet:
            return ""
        answer = ctx.ask_llm(
            "Review this code and give the three most valuable improvements as a numbered list, "
            f"one sentence each. Language: {report.language}\n\n```\n{snippet[:3500]}\n```",
            "Be specific and concise. Do not repeat syntax errors already found by a linter.",
        )
        return f"\n\nDeeper review (LLM):\n{answer}" if answer else ""


class ExplainCodeSkill(Skill):
    name = "explain_code"
    title = "Explain code"
    description = "plain-language explanation of a file or snippet"
    examples = ("explain jarvis/core.py", "what does this code do")
    patterns = (
        (r"\bexplain\b.*\b(code|file|function|script|this|module)\b", 0.93),
        (r"\bwhat does (this|the) (code|function|script|file) do\b", 0.95),
        (r"\bwalk me through\b", 0.9),
    )
    priority = 6

    def run(self, text: str, ctx: SkillContext) -> SkillResult:
        path_match = re.search(r"([\w./-]+\.(?:py|js|ts|java|c|cpp|rs|go|rb|php|sql|sh))\b", text)

        if path_match:
            target = _resolve_path(path_match.group(1))
            if not target.is_file():
                return SkillResult(text=f"No file at {target}.", ok=False, speak="I could not find that file.")
            language = detect_language("", target.name)
            try:
                code = target.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                return SkillResult(text=f"Could not read {target}: {exc}", ok=False)
            structure = describe_python_structure(code) if language == "Python" else {}
            summary = [
                f"{target.name} — {language}, {len(code.splitlines())} lines, "
                f"{len(code.split())} words.",
            ]
            if structure.get("imports"):
                summary.append("Imports: " + ", ".join(sorted(set(structure["imports"]))[:12]))
            if structure.get("classes"):
                summary.append("Classes: " + ", ".join(structure["classes"][:12]))
            if structure.get("functions"):
                summary.append("Functions: " + ", ".join(structure["functions"][:16]))
            answer = ""
            if ctx.llm_ready():
                answer = ctx.ask_llm(
                    f"Explain what this {language} file does for a new teammate, in at most 150 words. "
                    f"Then list the 3 most important functions with one-line descriptions.\n\n"
                    f"```\n{code[:4000]}\n```",
                    "No preamble. Be concrete about behaviour, not style.",
                )
            body = "\n".join(summary)
            if answer:
                body += "\n\n" + answer
            else:
                body += ("\n\n(Add an LLM key in Settings for a narrated explanation; "
                         "the structural map above is generated offline.)")
            return SkillResult(text=body, speak=f"Here is what {target.name} does, on screen.",
                               data={"path": str(target), "structure": structure})

        content = ctx.host.get_clipboard()
        code = extract_code_from_text(content)
        if not code.strip():
            return SkillResult(
                text="Point me at a file (“explain app.py”) or copy the code and say “explain my clipboard”.",
                speak="Which code should I explain?", ok=False,
            )
        report = analyze_code(code)
        structure = describe_python_structure(code) if report.language == "Python" else {}
        if ctx.llm_ready():
            answer = ctx.ask_llm(
                f"Explain this {report.language} code in at most 120 words.\n\n```\n{code[:3500]}\n```",
                "No preamble, no restating the code line by line.",
            )
            if answer:
                return SkillResult(text=answer, speak="Explanation is on screen.", data={"source": "llm"})
        lines = [f"Clipboard holds {report.language} code, {report.lines} lines."]
        if structure.get("functions"):
            lines.append("Functions: " + ", ".join(structure["functions"][:16]))
        if structure.get("classes"):
            lines.append("Classes: " + ", ".join(structure["classes"][:12]))
        lines.append(report.summary())
        return SkillResult(text="\n".join(lines), speak="Here is the structural breakdown.")


# ════════════════════════════════════════════════════════════════════════════
#  Synthetic data
# ════════════════════════════════════════════════════════════════════════════

class SyntheticDataSkill(Skill):
    name = "synthetic"
    title = "Synthetic data"
    description = "generate reproducible test datasets (CSV/JSON/JSONL/SQL)"
    examples = ("generate 500 rows of patient data as csv",
                "make me test data for payments", "synthetic users 1000 rows seed 7")
    patterns = (
        (r"\b(synthetic|fake|dummy|mock|sample|test) (data|dataset|records|rows)\b", 0.95),
        (r"\bgenerate\b.*\b(rows|records|data|dataset)\b", 0.9),
        (r"\bmake\b.*\b(test data|sample data|dummy data)\b", 0.92),
        (r"\bseed the database\b", 0.8),
    )
    priority = 7

    def run(self, text: str, ctx: SkillContext) -> SkillResult:
        lowered = text.lower()
        if re.search(r"\b(kinds?|what|which|list|available|types?)\b", lowered) and \
                not re.search(r"\d", lowered):
            lines = ["I can generate these datasets:"]
            lines += [f"• {name} — {description}" for name, description in list_kinds()]
            lines.append("Say for example: “generate 500 rows of patient data as csv”.")
            return SkillResult(text="\n".join(lines), speak="I can generate users, transactions, health, "
                                                           "logs, employees or sensor data.")

        if re.search(r"\b(sql|schema|create table|ddl)\b", lowered) and re.search(
                r"\b(schema|create table|ddl)\b", lowered):
            kind = resolve_kind(lowered) or "users"
            return SkillResult(text=to_sql_schema(kind), data={"kind": kind})

        kind = resolve_kind(lowered)
        if not kind:
            lines = ["Which dataset? Pick one of:"]
            lines += [f"• {name} — {description}" for name, description in list_kinds()]
            return SkillResult(text="\n".join(lines), speak="Which dataset should I generate?", ok=False)

        rows_match = re.search(r"(\d[\d,]*)\s*(?:rows?|records?|entries|lines)?", lowered)
        rows = int(rows_match.group(1).replace(",", "")) if rows_match else 200
        rows = max(1, min(rows, 100_000))
        seed_match = re.search(r"\bseed\s*(\d+)", lowered)
        seed = int(seed_match.group(1)) if seed_match else 42
        fmt = "csv"
        for candidate in ("jsonl", "json", "sql", "csv"):
            if candidate in lowered:
                fmt = candidate
                break

        result = generate(kind, rows=rows, seed=seed, fmt=fmt)
        return SkillResult(
            text=result["text"],
            speak=f"Generated {result['rows']:,} rows of synthetic data — {result['description']} "
                  f"and saved the {fmt} file.",
            data=result,
            followups=["open the synthetic data folder"],
        )


# ════════════════════════════════════════════════════════════════════════════
#  Flowcharts
# ════════════════════════════════════════════════════════════════════════════

class FlowchartSkill(Skill):
    name = "flowchart"
    title = "Flowchart"
    description = "Mermaid + Graphviz diagrams from code, saved to disk"
    examples = ("make a flowchart of jarvis/core.py", "diagram this code", "draw the flow")
    patterns = (
        (r"\b(flow ?chart|flow diagram|diagram|graph)\b", 0.93),
        (r"\bdraw\b.*\b(flow|diagram|chart)\b", 0.93),
        (r"\bvisuali[sz]e\b.*\b(flow|code|logic)\b", 0.9),
    )
    priority = 6

    def run(self, text: str, ctx: SkillContext) -> SkillResult:
        path_match = re.search(r"([\w./-]+\.(?:py|js|ts|java|c|cpp|rs|go|rb|php|sh))\b", text)
        if path_match:
            target = _resolve_path(path_match.group(1))
            if not target.is_file():
                return SkillResult(text=f"No file at {target}.", ok=False, speak="I could not find that file.")
            code = target.read_text(encoding="utf-8", errors="replace")
            title = target.stem
        else:
            code = extract_code_from_text(ctx.host.get_clipboard())
            title = "clipboard"
            if not code.strip():
                return SkillResult(
                    text="Point me at a file (“flowchart for jarvis/core.py”) or copy code and say "
                         "“diagram my clipboard”.",
                    speak="What should I diagram?", ok=False,
                )
        try:
            diagram = build_diagram(code, title=title, render_png=graphviz_available())
        except ValueError as exc:
            return SkillResult(text=str(exc), ok=False, speak="I could not build that diagram.")
        note = "" if graphviz_available() else (
            "\n(Graphviz not installed, so no PNG — the Mermaid source above renders on GitHub, "
            "in VS Code, or at mermaid.live. Install graphviz for a PNG.)"
        )
        return SkillResult(
            text=diagram.summary() + note,
            speak=f"Diagram built with {diagram.nodes} nodes and saved to your diagrams folder.",
            data={"paths": diagram.paths, "nodes": diagram.nodes, "mermaid": diagram.mermaid},
        )


# ════════════════════════════════════════════════════════════════════════════
#  Files
# ════════════════════════════════════════════════════════════════════════════

class FileSkill(Skill):
    name = "files"
    title = "Files"
    description = "find, read, summarise and inspect files and folders"
    examples = ("find file config.json", "read ~/notes.md", "summarise report.pdf",
                "list files in ~/Downloads", "how big is ~/projects")
    patterns = (
        (r"\b(find|locate|search for|where is)\b.*\b(file|folder|directory|\.\w{2,4}\b)", 0.9),
        (r"\b(read|open|show|cat|display)\b.*\b(file|\.\w{2,4}\b)", 0.82),
        (r"\b(summarise|summarize|tl;?dr|explain)\b.*\b(file|document|report|pdf|\.\w{2,4}\b)", 0.9),
        (r"\blist\b.*\b(files|folder|directory|contents)\b", 0.9),
        (r"\b(how (big|large)|size of)\b.*\b(folder|directory|file|project)\b", 0.88),
        (r"\bwhat('s| is) in\b.*\b(folder|directory)\b", 0.85),
    )

    MAX_READ_BYTES = 400_000

    def run(self, text: str, ctx: SkillContext) -> SkillResult:
        lowered = text.lower()
        path_match = re.search(r"((?:~|\.|/|[A-Za-z]:)[^\s\"']+|[\w.-]+\.(?:py|js|ts|txt|md|json|csv|log|yml|yaml|pdf|html|sql))", text)

        if re.search(r"\b(list|what's in|what is in|contents)\b", lowered):
            folder = _resolve_path(path_match.group(1)) if path_match else Path.home()
            if not folder.is_dir():
                return SkillResult(text=f"{folder} is not a directory.", ok=False, speak="That is not a folder.")
            try:
                entries = sorted(folder.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
            except OSError as exc:
                return SkillResult(text=f"Could not list {folder}: {exc}", ok=False)
            files = [e for e in entries if e.is_file()]
            folders = [e for e in entries if e.is_dir()]
            lines = [f"{folder} — {len(files)} files, {len(folders)} folders"]
            lines += [f"  [dir]  {f.name}/" for f in folders[:12]]
            lines += [f"  [file] {f.name}  ({f.stat().st_size / 1024:.1f} KB)" for f in files[:20]]
            if len(files) + len(folders) > 32:
                lines.append(f"  …and {len(files) + len(folders) - 32} more")
            return SkillResult(text="\n".join(lines), speak=f"{len(files)} files and {len(folders)} folders there.")

        if re.search(r"\b(how (big|large)|size of|disk usage)\b", lowered):
            target = _resolve_path(path_match.group(1)) if path_match else Path.home()
            if not target.exists():
                return SkillResult(text=f"No such path: {target}", ok=False)
            total, count = _folder_size(target)
            biggest = sorted(_biggest_files(target, limit=5), key=lambda kv: -kv[1])
            lines = [f"{target}: {total / 1e6:.1f} MB across {count} files"]
            lines += [f"  {path.name}: {size / 1e6:.2f} MB" for path, size in biggest]
            return SkillResult(text="\n".join(lines), speak=f"That folder is {total / 1e6:.0f} megabytes.")

        if re.search(r"\b(find|locate|search for|where is)\b", lowered):
            if not path_match:
                return SkillResult(text="What should I look for? e.g. “find file config.json in ~/projects”.",
                                   ok=False, speak="What file should I find?")
            needle = path_match.group(1)
            root_match = re.search(r"\bin\s+((?:~|\.|/|[A-Za-z]:)[^\s\"']+)", text)
            root = _resolve_path(root_match.group(1)) if root_match else Path.home()
            matches = _search_files(root, needle, limit=25)
            if not matches:
                return SkillResult(text=f"No matches for “{needle}” under {root}.",
                                   speak="I found no matching files.")
            lines = [f"{len(matches)} match(es) for “{needle}” under {root}:"]
            lines += [f"  {m}" for m in matches[:20]]
            return SkillResult(text="\n".join(lines), speak=f"Found {len(matches)} matching files.")

        if path_match:
            target = _resolve_path(path_match.group(1))
            if target.is_file():
                return self._read(target, ctx)
            if target.is_dir():
                entries = [e.name for e in list(target.iterdir())[:20]]
                return SkillResult(text=f"{target} holds: {', '.join(entries)}",
                                   speak=f"That folder holds {len(entries)} entries.")
            return SkillResult(text=f"No file or folder at {target}.", ok=False,
                               speak="I could not find that path.")
        return SkillResult(text="Try “find file config.json”, “read ~/notes.md” or “list files in ~/Downloads”.",
                           ok=False, speak="What file do you need?")

    def _read(self, target: Path, ctx: SkillContext) -> SkillResult:
        try:
            size = target.stat().st_size
            if size > self.MAX_READ_BYTES:
                with open(target, encoding="utf-8", errors="replace") as fh:
                    head = fh.read(4000)
                return SkillResult(
                    text=f"{target.name} is {size / 1e6:.1f} MB — showing the first 4,000 characters:\n\n{head}",
                    speak=f"{target.name} is too large to read in full; here is the beginning.",
                )
            content = target.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return SkillResult(text=f"Could not read {target}: {exc}", ok=False, speak="I could not read that file.")

        if re.search(r"\b(summarise|summarize|tl;?dr)\b", _last_question(ctx, target)):
            return self._summarise(target, content, ctx)
        preview = content[:2500] + ("…" if len(content) > 2500 else "")
        return SkillResult(
            text=f"{target} ({len(content.splitlines())} lines, {len(content.split())} words):\n\n{preview}",
            speak=f"{target.name} has {len(content.splitlines())} lines.",
            data={"path": str(target)},
        )

    def _summarise(self, target: Path, content: str, ctx: SkillContext) -> SkillResult:
        if ctx.llm_ready():
            answer = ctx.ask_llm(
                f"Summarise this document in at most 120 words, then list up to 3 action items.\n\n"
                f"File: {target.name}\n\n{content[:6000]}",
                "Plain text. No preamble.",
            )
            if answer:
                return SkillResult(text=f"{target.name}\n{answer}", speak="Summary is on screen.",
                                   data={"path": str(target), "source": "llm"})
        words = Counter(re.findall(r"[A-Za-z][A-Za-z'-]{3,}", content.lower()))
        stop = {"this", "that", "with", "from", "have", "will", "your", "they", "there", "their",
                "which", "about", "would", "should", "these", "those", "been", "were", "when"}
        keywords = [w for w, _ in words.most_common(40) if w not in stop][:10]
        lines = [f"{target.name} — {len(content.splitlines())} lines, {len(content.split())} words.",
                 "Frequent terms: " + ", ".join(keywords)]
        if target.suffix.lower() == ".py":
            structure = describe_python_structure(content)
            lines.append("Functions: " + ", ".join(structure["functions"][:12]) or "none")
            lines.append("Classes: " + ", ".join(structure["classes"][:12]) or "none")
        lines.append("(Offline summary — add an LLM key for a written digest.)")
        return SkillResult(text="\n".join(lines), speak="Here is a keyword summary of that file.")


def _last_question(ctx: SkillContext, target: Path) -> str:
    recent = ctx.memory.recent(1)
    return recent[-1].get("text", "") if recent else ""


def _folder_size(path: Path) -> tuple[int, int]:
    total = 0
    count = 0
    if path.is_file():
        return path.stat().st_size, 1
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += (Path(root) / name).stat().st_size
                count += 1
            except OSError:
                continue
        if count > 60_000:
            break
    return total, count


def _biggest_files(path: Path, limit: int = 5) -> list[tuple[Path, int]]:
    found: list[tuple[Path, int]] = []
    if path.is_file():
        return [(path, path.stat().st_size)]
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                full = Path(root) / name
                found.append((full, full.stat().st_size))
            except OSError:
                continue
    found.sort(key=lambda kv: -kv[1])
    return found[:limit]


def _search_files(root: Path, needle: str, limit: int = 25) -> list[str]:
    """Case-insensitive name search, glob-style needles supported."""
    pattern = needle.lower()
    wildcard = "*" in needle
    matches: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".") and d not in
                       {"node_modules", "__pycache__", ".git", "venv", ".venv", "dist", "build"}]
        for name in filenames:
            hit = name.lower().endswith(pattern.lstrip("*")) if wildcard else (pattern in name.lower())
            if hit:
                matches.append(str(Path(dirpath) / name))
                if len(matches) >= limit:
                    return matches
    return matches


# ════════════════════════════════════════════════════════════════════════════
#  Project insight (git aware)
# ════════════════════════════════════════════════════════════════════════════

class ProjectSkill(Skill):
    name = "project"
    title = "Project insight"
    description = "git status, recent commits and TODO scan for a repo"
    examples = ("what's the status of this project", "git status", "show me the last commits")
    patterns = (
        (r"\bgit (status|log|diff|branch)\b", 0.95),
        (r"\b(project|repo|repository) (status|state|summary)\b", 0.9),
        (r"\bwhat('s| is) (changed|the status) (in|of)? ?(this|the)? ?(project|repo)?\b", 0.85),
        (r"\b(uncommitted|pending) changes\b", 0.9),
    )

    def run(self, text: str, ctx: SkillContext) -> SkillResult:
        cwd = Path(ctx.state.get("cwd") or os.getcwd())
        if not shutil.which("git"):
            return SkillResult(text="git is not installed (or not on PATH).", ok=False,
                               speak="Git is not available on this machine.")
        if not (cwd / ".git").exists() and not _find_git_root(cwd):
            return SkillResult(text=f"{cwd} is not inside a git repository.", ok=False,
                               speak="This folder is not a git repository.")
        root = _find_git_root(cwd) or cwd
        lines = [f"Repository: {root}"]
        for label, command in (
            ("Branch", ["git", "rev-parse", "--abbrev-ref", "HEAD"]),
            ("Last commit", ["git", "log", "-1", "--pretty=%h %ad %s", "--date=short"]),
            ("Changes", ["git", "status", "--short"]),
        ):
            try:
                result = subprocess.run(command, cwd=root, capture_output=True, text=True, timeout=10)
                output = (result.stdout or "").strip()
                if label == "Changes":
                    changed = output.splitlines() if output else []
                    lines.append(f"Uncommitted changes: {len(changed)}")
                    lines += [f"  {line}" for line in changed[:12]]
                else:
                    lines.append(f"{label}: {output or '(none)'}")
            except Exception as exc:
                lines.append(f"{label}: unavailable ({exc})")
        try:
            commits = subprocess.run(
                ["git", "log", "-5", "--pretty=%h %ad %s", "--date=short"],
                cwd=root, capture_output=True, text=True, timeout=10,
            ).stdout.strip()
            if commits:
                lines.append("Recent commits:\n" + "\n".join(f"  {line}" for line in commits.splitlines()))
        except Exception:
            pass
        todos = _scan_todos(root, limit=8)
        if todos:
            lines.append("TODOs found:")
            lines += [f"  {line}" for line in todos]
        return SkillResult(text="\n".join(lines), speak="Repository status is on screen.",
                           data={"repo": str(root)})


def _find_git_root(start: Path) -> Path | None:
    current = start.resolve()
    for candidate in [current, *current.parents]:
        if (candidate / ".git").exists():
            return candidate
    return None


def _scan_todos(root: Path, limit: int = 10) -> list[str]:
    pattern = re.compile(r"\b(TODO|FIXME|HACK|XXX)\b[:\s]*(.{0,60})")
    hits: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in {".git", "node_modules", "__pycache__", ".venv", "venv"}]
        for name in filenames:
            if Path(name).suffix.lower() not in CODE_EXTENSIONS:
                continue
            try:
                text = (Path(dirpath) / name).read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for match in pattern.finditer(text):
                line = text.count("\n", 0, match.start()) + 1
                hits.append(f"{Path(dirpath).name}/{name}:{line} {match.group(1)} {match.group(2).strip()}")
                if len(hits) >= limit:
                    return hits
    return hits


class EnvironmentSkill(Skill):
    name = "environment"
    title = "Environment"
    description = "Python, interpreters, packages and PATH diagnostics"
    examples = ("what python am i running", "is docker installed", "which python")
    patterns = (
        (r"\b(python|node|npm|java|docker|git|ffmpeg) (version|--version)\b", 0.92),
        (r"\bwhich (python|node|npm|java|docker|git|ffmpeg)\b", 0.92),
        (r"\bis (python|node|docker|git|ffmpeg) installed\b", 0.92),
        (r"\bwhat python am i running\b", 0.95),
        (r"\b(virtual ?env|venv|interpreter)\b", 0.8),
    )

    def run(self, text: str, ctx: SkillContext) -> SkillResult:
        lowered = text.lower()
        tools = ["python", "python3", "pip", "node", "npm", "java", "docker", "git", "ffmpeg", "tesseract", "code"]
        wanted = [tool for tool in tools if re.search(rf"\b{tool}\b", lowered)]
        if not wanted:
            wanted = ["python", "node", "git", "docker"]
        lines = [f"Platform: {platform.platform()}",
                 f"Python in use: {sys.executable} ({platform.python_version()})",
                 f"Virtual env: {os.environ.get('VIRTUAL_ENV') or 'none'}"]
        for tool in wanted:
            path = shutil.which(tool)
            if not path:
                lines.append(f"{tool}: not found on PATH")
                continue
            version = ""
            for flag in ("--version", "-version", "-v"):
                try:
                    result = subprocess.run([path, flag], capture_output=True, text=True, timeout=6)
                    output = (result.stdout or result.stderr).strip().splitlines()
                    if output:
                        version = output[0][:80]
                        break
                except Exception:
                    continue
            lines.append(f"{tool}: {path}" + (f" — {version}" if version else ""))
        return SkillResult(text="\n".join(lines), speak="Environment details are on screen.",
                           data={"python": sys.executable})


def default_dev_skills() -> list[Skill]:
    return [
        CodeReviewSkill(),
        ExplainCodeSkill(),
        SyntheticDataSkill(),
        FlowchartSkill(),
        FileSkill(),
        ProjectSkill(),
        EnvironmentSkill(),
    ]
