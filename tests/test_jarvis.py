"""
Test suite for JARVIS.

Run everything:

    python -m unittest discover -s tests -v

These tests never touch the network or the real ``~/.jarvis`` folder: they point
``JARVIS_HOME`` at a temporary directory and use the headless host.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

TEMP_HOME = tempfile.mkdtemp(prefix="jarvis-tests-")
os.environ["JARVIS_HOME"] = TEMP_HOME

from jarvis.analyzers import analyze_code, detect_language, extract_code_from_text  # noqa: E402
from jarvis.config import Settings  # noqa: E402
from jarvis.core import Core  # noqa: E402
from jarvis.flowchart import build_diagram, python_flow  # noqa: E402
from jarvis.host import HeadlessHost  # noqa: E402
from jarvis.memory import Memory  # noqa: E402
from jarvis.skills import parse_duration, safe_eval  # noqa: E402
from jarvis.synthetic import generate, generate_dataset, resolve_kind  # noqa: E402
from jarvis.voice import contains_wake_word, split_wake_command  # noqa: E402


class JarvisTestCase(unittest.TestCase):
    """Base class: isolated settings, memory, host and core per test."""

    def setUp(self) -> None:
        self.home = Path(tempfile.mkdtemp(prefix="jarvis-case-"))
        self._previous = os.environ.get("JARVIS_HOME")
        os.environ["JARVIS_HOME"] = str(self.home)
        self.settings = Settings()
        self.settings["voice_replies"] = False
        self.memory = Memory()
        self.host = HeadlessHost(verbose=False)
        self.core = Core(self.settings, self.memory, self.host)

    def tearDown(self) -> None:
        self.core.shutdown()
        if self._previous:
            os.environ["JARVIS_HOME"] = self._previous


# ════════════════════════════════════════════════════════════════════════════
class TestRouting(JarvisTestCase):
    def test_time(self):
        response = self.core.ask("what is the time")
        self.assertEqual(response.skill, "time")
        self.assertIn(":", response.text)

    def test_math_percentage(self):
        response = self.core.ask("what is 18% of 2400")
        self.assertEqual(response.skill, "math")
        self.assertIn("432", response.text)

    def test_math_expression(self):
        response = self.core.ask("calculate 12 * (7 + 5) / 4")
        self.assertEqual(response.skill, "math")
        self.assertIn("36", response.text)

    def test_unit_conversion(self):
        response = self.core.ask("convert 12 km to miles")
        self.assertEqual(response.skill, "math")
        self.assertIn("7.4", response.text)

    def test_help_beats_generic_skill(self):
        response = self.core.ask("what can you do with my clipboard")
        self.assertEqual(response.skill, "help")

    def test_identity(self):
        self.assertEqual(self.core.ask("who are you").skill, "identity")

    def test_offline_fallback_is_honest(self):
        response = self.core.ask("draft a legal contract for my startup")
        self.assertEqual(response.skill, "fallback")
        self.assertIn("/help", response.text)

    def test_slash_commands(self):
        self.assertIn("JARVIS v", self.core.ask("/help").text)
        self.core.ask("hello there")
        self.assertTrue(self.memory.history)
        self.core.ask("/clear")
        self.assertFalse(self.memory.history)

    def test_unknown_slash_command(self):
        response = self.core.ask("/nonsense")
        self.assertFalse(response.ok)

    def test_wake_word_is_stripped(self):
        response = self.core.ask("Jarvis, what is the time")
        self.assertEqual(response.skill, "time")

    def test_skills_are_counted(self):
        self.assertGreaterEqual(len(self.core.registry.skills), 20)

    def test_every_skill_has_metadata_and_runs(self):
        """Each skill must match its own first example without raising."""
        for skill in self.core.registry.skills:
            self.assertTrue(skill.title, skill.name)
            self.assertTrue(skill.description, skill.name)
            if not skill.examples:
                continue
            example = skill.examples[0]
            with self.subTest(skill=skill.name):
                result = skill.run(example, self.core.ctx)
                self.assertIsInstance(result.text, str)


# ════════════════════════════════════════════════════════════════════════════
class TestMemorySkills(JarvisTestCase):
    def test_note_task_fact_roundtrip(self):
        self.core.ask("note that the staging database rotates on Monday")
        self.core.ask("add task Call the bank tomorrow")
        self.core.ask("remember my locker code is 4417")

        self.assertEqual(len(self.memory.notes), 1)
        self.assertIn("staging database", self.memory.notes[0].text)
        self.assertEqual(len(self.memory.open_tasks()), 1)
        self.assertEqual(self.memory.recall("locker code"), "4417")

        listed = self.core.ask("what are my tasks")
        self.assertIn("Call the bank", listed.text)
        recalled = self.core.ask("what do you remember about my locker code")
        self.assertIn("4417", recalled.text)

    def test_memory_persists_to_disk(self):
        self.core.ask("remember my project is called Code-Jarvis")
        reloaded = Memory()
        self.assertEqual(reloaded.recall("project"), "Code-Jarvis")

    def test_complete_task(self):
        self.core.ask("add task water the plants")
        self.core.ask("complete task water the plants")
        self.assertEqual(len(self.memory.open_tasks()), 0)

    def test_window_title_fact_does_not_override_notes(self):
        """'write down' style phrasing must still create a note."""
        self.core.ask("write down buy milk on the way home")
        self.assertTrue(self.memory.notes)


class TestTimers(JarvisTestCase):
    def test_timer_fires_notification(self):
        response = self.core.ask("set a timer for 1 second to stretch")
        self.assertEqual(response.skill, "timer")
        time.sleep(1.6)
        titles = [title for title, _text, _level in self.host.notifications]
        self.assertIn("JARVIS reminder", titles)
        self.assertTrue(any("stretch" in text for text in self.host.spoken))

    def test_timer_listing(self):
        self.core.ask("set a timer for 30 seconds")
        listing = self.core.ask("list timers")
        self.assertIn("left", listing.text)


# ════════════════════════════════════════════════════════════════════════════
class TestClipboardCodeReview(JarvisTestCase):
    """The flagship workflow: review whatever is on the clipboard."""

    SNIPPET = (
        "import os\n"
        "API_KEY = 'sk-abcdefghijklmnopqrstuvwxyz012345'\n"
        "def load(items=[], path=None):\n"
        "    if path == None:\n"
        "        path = []\n"
        "    os.system('cat ' + str(path))\n"
        "    return path[:100]\n"
    )

    def test_review_finds_planted_issues(self):
        self.host.set_clipboard(self.SNIPPET)
        response = self.core.ask("review my clipboard")
        self.assertEqual(response.skill, "code_review")
        self.assertEqual(response.data["report"]["language"], "Python")
        rules = {finding["rule"] for finding in response.data["report"]["findings"]}
        self.assertIn("secret", rules)
        self.assertIn("shell-injection", rules)
        self.assertIn("ast-eq-none", rules)
        self.assertIn("ast-mutable-default", rules)
        self.assertIn("critical", response.text.lower())

    def test_review_reports_empty_clipboard_clearly(self):
        self.host.set_clipboard("")
        response = self.core.ask("review my clipboard")
        self.assertFalse(response.ok)
        self.assertIn("empty", response.text.lower())

    def test_flowchart_from_clipboard(self):
        self.host.set_clipboard("def pick(items):\n    for i in items:\n        if i:\n            return i\n")
        response = self.core.ask("make a flowchart of my clipboard")
        self.assertEqual(response.skill, "flowchart")
        self.assertIn("flowchart TD", response.data["mermaid"])


class TestAnalyzers(unittest.TestCase):
    def test_language_detection(self):
        self.assertEqual(detect_language("def f(x):\n    return x\n"), "Python")
        self.assertEqual(detect_language("SELECT * FROM users;"), "SQL")
        self.assertEqual(detect_language('{"a": 1}'), "JSON")
        self.assertEqual(detect_language("#include <stdio.h>"), "C/C++")
        self.assertEqual(detect_language("", "main.rs"), "Rust")

    def test_secrets_and_unsafe_calls(self):
        code = '''
import os
API_KEY = "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"
def run(user=[], n=None):
    try:
        os.system("ls " + str(n))
    except:
        pass
    if n == None:
        eval("1+1")
'''
        report = analyze_code(code, filename="demo.py")
        rules = {finding.rule for finding in report.findings}
        self.assertIn("secret", rules)
        self.assertIn("shell-injection", rules)
        self.assertIn("ast-bare-except", rules)
        self.assertIn("ast-eval", rules)
        self.assertIn("ast-mutable-default", rules)
        self.assertEqual(report.worst, "critical")
        self.assertFalse(report.parsed is False)  # valid Python still parses

    def test_syntax_error_is_reported(self):
        report = analyze_code("def broken(:\n    pass\n")
        self.assertFalse(report.parsed)
        self.assertTrue(report.parse_error)
        self.assertEqual(report.counts()["critical"], 1)

    def test_clean_code_has_no_criticals(self):
        report = analyze_code("def add(a: int, b: int) -> int:\n    return a + b\n")
        self.assertEqual(report.counts()["critical"], 0)

    def test_metrics_and_markdown(self):
        report = analyze_code("def f():\n    return 1\n\ndef g():\n    return 2\n")
        self.assertEqual(report.metrics.get("functions"), 2)
        self.assertIn("Code review", report.to_markdown())

    def test_extract_fenced_block(self):
        text = "please check\n```python\nprint('hi')\n```\nthanks"
        self.assertEqual(extract_code_from_text(text).strip(), "print('hi')")


class TestSyntheticData(unittest.TestCase):
    def test_resolve_kind(self):
        self.assertEqual(resolve_kind("patient health records"), "health")
        self.assertEqual(resolve_kind("payments"), "transactions")
        self.assertIsNone(resolve_kind("kittens"))

    def test_generation_is_deterministic(self):
        first, _ = generate_dataset("users", rows=25, seed=7)
        second, _ = generate_dataset("users", rows=25, seed=7)
        third, _ = generate_dataset("users", rows=25, seed=8)
        self.assertEqual(first, second)
        self.assertNotEqual(first, third)

    def test_files_are_written(self):
        for fmt in ("csv", "json", "jsonl", "sql"):
            with self.subTest(fmt=fmt):
                result = generate("logs", rows=10, seed=1, fmt=fmt)
                path = Path(result["path"])
                self.assertTrue(path.exists())
                self.assertGreater(path.stat().st_size, 20)

    def test_unknown_kind_raises(self):
        with self.assertRaises(KeyError):
            generate_dataset("dragons", rows=1)


class TestFlowchart(unittest.TestCase):
    def test_python_flow_has_decision_and_loop(self):
        code = """
def check(items):
    for item in items:
        if item > 10:
            return "big"
        else:
            print(item)
    return "none"
"""
        mermaid, dot, nodes = python_flow(code, "check")
        self.assertIn("flowchart TD", mermaid)
        self.assertIn("-->", mermaid)
        self.assertIn("digraph flow", dot)
        self.assertGreater(nodes, 3)

    def test_syntax_error_raises_value_error(self):
        with self.assertRaises(ValueError):
            python_flow("def broken(:\n  pass")

    def test_build_diagram_writes_files(self):
        diagram = build_diagram("x = 1\nif x:\n    print(x)\n", title="tiny", render_png=False)
        self.assertIn("mermaid", diagram.paths)
        self.assertTrue(Path(diagram.paths["mermaid"]).exists())


class TestVoiceHelpers(unittest.TestCase):
    def test_wake_word_matching(self):
        self.assertTrue(contains_wake_word("jarvis are you there"))
        self.assertTrue(contains_wake_word("Hey Jarvis, weather"))
        self.assertTrue(contains_wake_word("jarvus what time is it"))  # tolerance for accents
        self.assertFalse(contains_wake_word("the service is down"))

    def test_split_wake_command(self):
        self.assertEqual(split_wake_command("jarvis set a timer", "jarvis"), "set a timer")
        self.assertEqual(split_wake_command("hey jarvis, weather in chennai", "jarvis"), "weather in chennai")
        self.assertEqual(split_wake_command("no wake word here", "jarvis"), "")


class TestSafeMath(unittest.TestCase):
    def test_allows_arithmetic(self):
        self.assertEqual(safe_eval("2 + 3 * 4"), 14)
        self.assertEqual(safe_eval("sqrt(81)"), 9)

    def test_blocks_code_execution(self):
        for expression in ("__import__('os').system('echo hi')", "open('/etc/passwd')", "1 if True else 2"):
            with self.subTest(expression=expression):
                with self.assertRaises(ValueError):
                    safe_eval(expression)

    def test_duration_parsing(self):
        self.assertEqual(parse_duration("in 10 minutes"), 600)
        self.assertEqual(parse_duration("timer for 1 hour 30 minutes"), 5400)
        self.assertEqual(parse_duration("45 seconds"), 45)
        self.assertIsNone(parse_duration("later"))


class TestSettings(JarvisTestCase):
    def test_defaults_and_roundtrip(self):
        self.settings["city"] = "Chennai"
        self.settings.save()
        self.assertEqual(Settings()["city"], "Chennai")

    def test_api_key_from_environment_wins(self):
        os.environ["GROQ_API_KEY"] = "env-key-value"
        try:
            self.settings.set_api_key("groq", "stored-key")
            self.assertEqual(self.settings.api_key("groq"), "env-key-value")
            self.assertIn("env", self.settings.key_source("groq"))
        finally:
            os.environ.pop("GROQ_API_KEY", None)
        self.assertEqual(self.settings.api_key("groq"), "stored-key")

    def test_settings_file_permissions(self):
        self.settings.set_api_key("openai", "sk-test-value")
        self.settings.save()
        mode = oct(self.settings.path.stat().st_mode)[-3:]
        if sys.platform != "win32":
            self.assertIn(mode, {"600", "640", "660"})

    def test_missing_key_is_reported_clearly(self):
        self.settings["provider"] = "groq"
        self.settings.set_api_key("groq", "")
        self.assertFalse(self.core.brain.ready)
        response = self.core.ask("tell me a story")
        self.assertFalse(response.ok)
        self.assertIn("No API key", response.text)


class TestHostContract(JarvisTestCase):
    def test_clipboard_roundtrip(self):
        self.assertTrue(self.host.set_clipboard("hello jarvis"))
        self.assertEqual(self.host.get_clipboard(), "hello jarvis")

    def test_notifications_and_logging(self):
        self.host.notify("Test", "body", "alarm")
        self.host.log("something happened")
        self.assertEqual(self.host.notifications[0][2], "alarm")
        self.assertTrue(self.host.logs)

    def test_schedule_and_cancel(self):
        flag: list[bool] = []
        self.host.schedule(0.1, lambda: flag.append(True))
        time.sleep(0.25)
        self.assertTrue(flag)
        handle2 = self.host.schedule(5.0, lambda: flag.append(False))
        self.assertTrue(self.host.cancel_schedule(handle2))


class TestClipping(unittest.TestCase):
    """JSON/CSV output must stay machine-readable."""

    def test_synthetic_json_is_valid(self):
        result = generate("sensors", rows=5, seed=3, fmt="json")
        data = json.loads(Path(result["path"]).read_text())
        self.assertEqual(len(data), 5)
        self.assertIn("temperature_c", data[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
