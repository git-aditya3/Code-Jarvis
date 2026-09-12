"""
Flowchart generation — the "draw me the flow" skill from the original tool,
rebuilt to emit Mermaid (renders in GitHub, VS Code, Obsidian, mermaid.live)
plus Graphviz DOT, and a PNG when Graphviz happens to be installed.

For Python we walk the real AST, so the diagram reflects the code rather than
guesses: functions, branches, loops, returns and raised exceptions.

Mermaid is the default because every code host renders it; DOT is offered for
people who want a rendered image or a Graphviz pipeline.
"""

from __future__ import annotations

import ast
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from .config import path_for

MAX_NODES = 220


@dataclass
class Diagram:
    language: str
    mermaid: str
    dot: str
    title: str
    nodes: int = 0
    note: str = ""
    paths: dict[str, str] = field(default_factory=dict)

    def summary(self) -> str:
        lines = [f"Flowchart for {self.title} — {self.nodes} nodes."]
        if self.note:
            lines.append(self.note)
        if self.paths:
            lines.append("Saved: " + ", ".join(f"{k} → {v}" for k, v in self.paths.items()))
        lines.append("Mermaid source:\n```mermaid\n" + self.mermaid.strip() + "\n```")
        return "\n".join(lines)


class _PyFlow:
    """Walk a Python AST and build Mermaid nodes/edges."""

    def __init__(self) -> None:
        self.lines: list[str] = []
        self.node_id = 0
        self.nodes = 0
        self.edge_count = 0

    # ── helpers ──────────────────────────────────────────────────────────
    def new(self, label: str, shape: str = "rect") -> str:
        self.node_id += 1
        self.nodes += 1
        node = f"n{self.node_id}"
        safe = label.replace('"', "'").replace("\n", " ")[:70]
        if shape == "diamond":
            self.lines.append(f'    {node}{{"{safe}"}}')
        elif shape == "round":
            self.lines.append(f'    {node}("{safe}")')
        elif shape == "stadium":
            self.lines.append(f'    {node}(["{safe}"])')
        else:
            self.lines.append(f'    {node}["{safe}"]')
        return node

    def edge(self, src: str, dst: str, label: str = "") -> None:
        self.edge_count += 1
        if label:
            self.lines.append(f"    {src} -->|{label}| {dst}")
        else:
            self.lines.append(f"    {src} --> {dst}")

    # ── traversal ────────────────────────────────────────────────────────
    def emit_body(self, body: list[ast.stmt], current: str, depth: int = 0) -> list[str]:
        """Return the list of node ids execution may continue from."""
        tails = [current]
        for statement in body:
            if self.nodes > MAX_NODES:
                break
            new_tails: list[str] = []
            for tail in tails:
                new_tails.extend(self.emit_stmt(statement, tail, depth))
            tails = new_tails or tails
        return tails

    def emit_stmt(self, node: ast.stmt, current: str, depth: int) -> list[str]:
        if isinstance(node, _Collapsed):
            step = self.new(node.label)
            self.edge(current, step)
            return [step]

        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            name = self.new(f"def {node.name}()", "stadium")
            if depth == 0:
                self.edge(current, name, "call")
            else:
                self.edge(current, name)
            args = ", ".join(a.arg for a in node.args.args) or "no args"
            body_first = self.new(f"prepare args: {args[:60]}")
            self.edge(name, body_first)
            self.emit_body(node.body, body_first, depth + 1)
            return [name]

        if isinstance(node, ast.If):
            test = _expr_text(node.test)
            decision = self.new(f"{test}", "diamond")
            self.edge(current, decision)
            yes_tail = self.emit_body(node.body, decision, depth + 1)
            self.edge(decision, yes_tail[-1] if yes_tail else decision, "yes")
            tails = list(yes_tail)
            if node.orelse:
                if len(node.orelse) == 1 and isinstance(node.orelse[0], ast.If):
                    # elif chain
                    elif_node = self.new(f"{_expr_text(node.orelse[0].test)}", "diamond")
                    self.edge(decision, elif_node, "no")
                    tails.extend(self.emit_stmt(node.orelse[0], elif_node, depth + 1))
                else:
                    else_entry = self.new("else branch")
                    self.edge(decision, else_entry, "no")
                    tails.extend(self.emit_body(node.orelse, else_entry, depth + 1))
            else:
                tails.append(decision)
            return tails

        if isinstance(node, ast.For):
            loop = self.new(f"for {_expr_text(node.target)} in {_expr_text(node.iter)}", "diamond")
            self.edge(current, loop)
            body_tail = self.emit_body(node.body, loop, depth + 1)
            self.edge(body_tail[-1] if body_tail else loop, loop, "next item")
            return [loop]

        if isinstance(node, (ast.While, ast.AsyncFor)):
            loop = self.new(
                f"while {_expr_text(node.test)}" if isinstance(node, ast.While) else "async for", "diamond"
            )
            self.edge(current, loop)
            body_tail = self.emit_body(node.body, loop, depth + 1)
            self.edge(body_tail[-1] if body_tail else loop, loop, "repeat")
            return [loop]

        if isinstance(node, ast.Try):
            guard = self.new("try block")
            self.edge(current, guard)
            tails = self.emit_body(node.body, guard, depth + 1)
            for handler in node.handlers:
                kind = _expr_text(handler.type) if handler.type else "any exception"
                catch = self.new(f"except {kind}", "diamond")
                self.edge(guard, catch, "error")
                tails.extend(self.emit_body(handler.body, catch, depth + 1))
            if node.finalbody:
                cleanup = self.new("finally")
                for tail in tails:
                    self.edge(tail, cleanup)
                tails = self.emit_body(node.finalbody, cleanup, depth + 1)
            return tails

        if isinstance(node, (ast.With, ast.AsyncWith)):
            label = "with " + ", ".join(_expr_text(item.context_expr) for item in node.items)
            ctx = self.new(label[:60])
            self.edge(current, ctx)
            return self.emit_body(node.body, ctx, depth + 1)

        if isinstance(node, ast.Return):
            ret = self.new(f"return {_expr_text(node.value)}" if node.value else "return", "round")
            self.edge(current, ret)
            return [ret]

        if isinstance(node, ast.Raise):
            raise_node = self.new(f"raise {_expr_text(node.exc)}" if node.exc else "raise", "round")
            self.edge(current, raise_node)
            return [raise_node]

        if isinstance(node, (ast.Break, ast.Continue, ast.Pass)):
            kind = type(node).__name__.lower()
            leaf = self.new(kind, "round")
            self.edge(current, leaf)
            return [leaf]

        # default: a processing step
        step = self.new(_statement_text(node))
        self.edge(current, step)
        return [step]


def _expr_text(node: ast.AST | None) -> str:
    if node is None:
        return ""
    try:
        return ast.unparse(node)
    except Exception:  # pragma: no cover - very old Python
        return type(node).__name__


def _statement_text(node: ast.stmt) -> str:
    text = _expr_text(node)
    text = re.sub(r"\s+", " ", text)
    if len(text) > 64:
        text = text[:61] + "…"
    return text or type(node).__name__


class _Collapsed(ast.stmt):
    """A synthetic statement used to compress a run of imports into one node."""

    _fields: tuple[str, ...] = ()

    def __init__(self, label: str) -> None:
        self.label = label


def _collapse(body: list[ast.stmt]) -> list[ast.stmt]:
    """Drop docstrings and merge consecutive imports into a single step."""
    out: list[ast.stmt] = []
    pending: list[str] = []

    def flush() -> None:
        if pending:
            label = "import " + ", ".join(pending[:6]) + ("…" if len(pending) > 6 else "")
            out.append(_Collapsed(label))
            pending.clear()

    for node in body:
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) \
                and isinstance(node.value.value, str):
            continue  # docstring
        if isinstance(node, ast.Import):
            pending.extend(alias.name for alias in node.names)
            continue
        if isinstance(node, ast.ImportFrom):
            pending.append(node.module or "")
            continue
        flush()
        out.append(node)
    flush()
    return out


def python_flow(code: str, title: str = "module") -> tuple[str, str, int]:
    """Return (mermaid, dot, node_count) for Python source."""
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        raise ValueError(f"Cannot build a flow chart — syntax error: {exc.msg} (line {exc.lineno})") from None

    docstring = ast.get_docstring(tree)
    if docstring:
        first = docstring.strip().splitlines()[0][:70]
        title = f"{title}: {first}" if title in ("module", "") else title

    flow = _PyFlow()
    flow.lines.append("flowchart TD")
    start = flow.new(f"start {title}", "stadium")
    flow.emit_body(_collapse(tree.body), start)

    mermaid = "\n".join(flow.lines)
    dot_lines = ["digraph flow {", '  rankdir=TB; node [shape=box, style="rounded", fontname="Helvetica"];']
    for line in flow.lines[1:]:
        strip = line.strip()
        match = re.match(r"(n\d+)\[\"(.+)\"\]$", strip)
        if match:
            dot_lines.append(f'  {match.group(1)} [label="{match.group(2)}"];')
            continue
        match = re.match(r'(n\d+)\{(?:"(.+)"|"(.+)")?\}$', strip)
        if match:
            dot_lines.append(f'  {match.group(1)} [label="{match.group(2) or ""}", shape=diamond];')
            continue
        match = re.match(r'(n\d+)\((?:\[)?"(.+)"(?:\]?)\)$', strip)
        if match:
            dot_lines.append(f'  {match.group(1)} [label="{match.group(2)}", shape=ellipse];')
            continue
        match = re.match(r"(n\d+) -->(?:\|(.+)\|)? (n\d+)$", strip)
        if match:
            label = f' [label="{match.group(2)}"]' if match.group(2) else ""
            dot_lines.append(f"  {match.group(1)} -> {match.group(3)}{label};")
    dot_lines.append("}")
    return mermaid, "\n".join(dot_lines), flow.nodes


def generic_flow(code: str, title: str = "script") -> tuple[str, str, int]:
    """Best-effort diagram for non-Python code: functions and control keywords."""
    functions = re.findall(
        r"(?:function|def|fn)\s+(\w+)|(\w+)\s*=\s*(?:async\s*)?\(", code
    )
    names = [next((g for g in groups if g), "") for groups in functions][:14]
    lines = ["flowchart TD", '    start(["start"])']
    nodes = 1
    previous = "start"
    for name in names:
        if not name:
            continue
        nodes += 1
        node = f"f{nodes}"
        lines.append(f'    {node}(["{name}()"])')
        lines.append(f"    {previous} --> {node}")
        previous = node
    has_branch = bool(re.search(r"\b(if|else|switch|case)\b", code))
    if has_branch:
        nodes += 1
        lines.append(f'    b{nodes}{{"conditional logic"}}')
        lines.append(f"    {previous} --> b{nodes}")
        node = f"e{nodes}"
        nodes += 1
        lines.append(f'    {node}(["continue"])')
        lines.append(f"    b{nodes} --> {node}")
        lines.append(f"    b{nodes} --> {previous}")
    lines.append('    done(["done"])')
    lines.append(f"    {previous} --> done")
    dot = "digraph flow {\n  rankdir=TB;\n" + "\n".join(
        f"  {re.sub(r'^ +', '', x)}" for x in lines[1:] if "flowchart" not in x
    ) + "\n}"
    return "\n".join(lines), dot, nodes


def build_diagram(
    code: str,
    title: str = "module",
    language: str | None = None,
    save: bool = True,
    render_png: bool = True,
) -> Diagram:
    """Create Mermaid + DOT (and a PNG when Graphviz is available)."""
    from .analyzers import detect_language

    language = language or detect_language(code)
    if language == "Python":
        mermaid, dot, nodes = python_flow(code, title)
        note = "Built from the real AST."
    else:
        mermaid, dot, nodes = generic_flow(code, title)
        note = "Approximate flow for a non-Python language (function order + branches)."

    diagram = Diagram(language=language, mermaid=mermaid, dot=dot, title=title, nodes=nodes, note=note)
    if not save:
        return diagram

    folder = path_for("diagrams")
    folder.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    stem = re.sub(r"[^A-Za-z0-9_-]+", "-", title)[:40] or "diagram"
    mermaid_path = folder / f"{stem}-{stamp}.mmd"
    mermaid_path.write_text(mermaid, encoding="utf-8")
    dot_path = folder / f"{stem}-{stamp}.dot"
    dot_path.write_text(dot, encoding="utf-8")
    diagram.paths = {"mermaid": str(mermaid_path), "dot": str(dot_path)}

    if render_png:
        png = _render_dot(dot, folder / f"{stem}-{stamp}.png")
        if png:
            diagram.paths["png"] = str(png)
    return diagram


def graphviz_available() -> bool:
    return bool(shutil.which("dot")) or _has_python_graphviz()


def _has_python_graphviz() -> bool:
    try:
        import graphviz  # noqa: F401
        return True
    except Exception:
        return False


def _render_dot(dot: str, target: Path) -> str | None:
    """Render with the graphviz Python package, else the `dot` binary."""
    try:
        import graphviz

        source = graphviz.Source(dot)
        rendered = source.render(filename=str(target.with_suffix("")), format="png", cleanup=True)
        return rendered
    except Exception:
        pass
    if shutil.which("dot"):
        try:
            subprocess.run(
                ["dot", "-Tpng", "-o", str(target)],
                input=dot.encode(),
                check=True,
                timeout=30,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return str(target)
        except Exception:
            return None
    return None


def describe_python_structure(code: str) -> dict[str, list[str]]:
    """Quick structural map used by the 'explain this file' skill."""
    result: dict[str, list[str]] = {"classes": [], "functions": [], "imports": []}
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return result
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            result["classes"].append(node.name)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            result["functions"].append(node.name)
        elif isinstance(node, ast.Import):
            result["imports"] += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            result["imports"].append(node.module or "")
    return result
