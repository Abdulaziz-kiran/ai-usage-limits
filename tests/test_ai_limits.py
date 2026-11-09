"""All I/O uses a temporary directory; processes are mocked and forbidden by default."""
from datetime import timezone
import ast
import contextlib
import io
import json
import os
from pathlib import Path
import signal
import runpy
import subprocess
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

import ai_limits as m

FIXTURES = Path(__file__).parent / "fixtures"


class LimitTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        environment = patch.dict(os.environ, {"CODEX_HOME": str(self.root / ".codex")})
        environment.start()
        self.addCleanup(environment.stop)
        self.patch_value("AGY", "/test/bin/agy")
        self.now = 1794211200.0
        paths = dict(ROOT=self.root, HOME=self.root, DATA=self.root / "data")
        for name, file in (("CACHE", "limits.json"), ("CLAUDE_CACHE", "claude-usage.json"),
                          ("MODELS_FILE", "known-models.json"), ("LOCK", "refreshing"),
                          ("SETTINGS", "settings.json"), ("HELLO_STATE", "hello.json"),
                          ("HELLO_LOG", "hello-log.jsonl")):
            paths[name] = paths["DATA"] / file
        for name, value in paths.items():
            self.patch_value(name, value)
        self.timestamp = patch.object(m.time, "time", return_value=self.now)
        self.timestamp.start()
        self.addCleanup(self.timestamp.stop)
        self.process = patch.object(m.subprocess, "Popen",
                                    side_effect=AssertionError("Real process"))
        self.popen = self.process.start()
        self.addCleanup(self.process.stop)
        self.addCleanup(m.release_lock)

    def patch_value(self, name, value):
        patcher = patch.object(m, name, value)
        patcher.start()
        self.addCleanup(patcher.stop)

    def limit(self, **changes):
        return dict(dict(source="Codex", name="Codex", window="weekly", remaining=40,
                         reset=self.now + 3600, data_time=self.now), **changes)

    def data(self, *limits, **other):
        return dict(dict(version=2, timestamp=self.now, codex={"limits": list(limits)}), **other)

    def write_json(self, path, data):
        m.atomic_json(path, data)

    def cli(self, *args):
        output, error = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(error):
            code = m.main(list(args))
        return code, output.getvalue(), error.getvalue()

    def models(self, source="agy", **items):
        return {"successful": [source], "models": {source + ":" + k: v for k, v in items.items()},
                "errors": {}}

    def mock_collection(self, agy=None, codex=None, claude=None, models=None):
        for name, value in (("agy_usage", agy), ("codex_usage", codex),
                          ("claude_usage", claude), ("models", models)):
            default = ({"successful": [], "models": {}, "errors": {}} if name == "models"
                          else {"error": "missing"})
            self.patch_value(name, Mock(return_value=default if value is None else value))

    def test_warning_messages_and_boundaries(self):
        for h, expected in ((11.99, "12"), (12, "24"), (23.99, "24")):
            with self.subTest(h=h):
                x = self.limit(reset=self.now + h * 3600, remaining=30)
                self.assertTrue(m.warnings(self.data(x))[0].startswith(
                    "RESETS IN " + expected + "H: Codex weekly 30% left,"))
        for x in (self.limit(reset=self.now + 86400), self.limit(remaining=29),
                  self.limit(reset=self.now, remaining=1),
                  self.limit(window="5-hour", remaining=100)):
            self.assertEqual([], m.warnings(self.data(x)))
        x = self.limit(remaining=9, data_time=self.now - 10800)
        warning = m.warnings(self.data(x))[0]
        self.assertEqual("LOW: Codex weekly 9% left, resets " +
                         m.format_date(x["reset"]) + " (1h 0m) (measured 3h 0m ago)", warning)
        self.assertNotIn("Codex Codex", warning)
        self.assertFalse(m.warnings(self.data(self.limit(remaining=10))))
        new = {"models_status": {"new": {"agy:x": "X"}}}
        self.assertEqual(["NEW MODEL: X (agy:x): review which models you use, "
                          "then run ai_limits.py --accept-models"], m.warnings(new))

    def test_claude_warning_and_reminder(self):
        x = self.limit(source="Claude", name="Weekly", remaining=65, reset=self.now + 9 * 3600)
        data = self.data(claude={"saved_at": self.now, "limits": [x]})
        self.assertTrue(m.warnings(data)[0].startswith("RESETS IN 12H"))
        self.assertEqual("", m.claude_stale_hint(data))
        for age in (21600, 21601):
            x["data_time"] = self.now - age
            data["claude"]["saved_at"] = x["data_time"]
            self.assertEqual(age == 21600, bool(m.warnings(data)))
            self.assertEqual(age > 21600, bool(m.claude_stale_hint(data)))
        data["claude"]["saved_at"] = self.now
        x["reset"] = self.now - 1
        self.assertTrue(m.claude_stale_hint(data))
        self.assertEqual("", m.claude_stale_hint({}))

    def test_new_model_priority_age_and_color(self):
        data = self.data(*(self.limit(name=str(i), remaining=1) for i in range(4)),
                         timestamp=self.now - 10800,
                         models_status={"new": {"agy:x": "X"}})
        warnings = m.warnings(data)
        self.assertTrue(warnings[0].startswith("NEW MODEL: X (agy:x)"))
        self.assertTrue(warnings[0].endswith("(measured 3h 0m ago)"))
        self.assertEqual("#0A84FF", m.warning_color(warnings[0]))
        self.patch_value("prepare", Mock(return_value=(data, True)))
        self.assertIn(warnings[0], self.cli("--short")[1])

    def test_short_truncation_reminder_and_silence(self):
        data = {"models_status": {"new": {str(i): "X" for i in range(5)}},
                "claude": {"saved_at": self.now - 25200}}
        self.patch_value("prepare", Mock(return_value=(data, True)))
        code, output, error = self.cli("--short")
        self.assertEqual((0, ""), (code, error))
        self.assertEqual(3, output.count("NEW MODEL"))
        self.assertIn("(+2 warnings: ai_limits.py)", output)
        self.assertIn("CLAUDE DATA STALE", output)
        self.assertEqual(3, len(output.splitlines()))
        self.assertEqual((0, "", ""), self.cli("--short", "--quiet"))
        m.prepare.return_value = ({"claude": {"saved_at": self.now}}, True)
        self.assertEqual((0, "", ""), self.cli("--short"))
        m.prepare.side_effect = KeyboardInterrupt
        self.assertEqual((0, "", ""), self.cli("--short"))
        self.assertNotIn("Traceback", self.cli("--menubar")[1])

    def test_agy_fixture_and_command(self):
        with patch.object(m, "run_agy", return_value=(FIXTURES / "agy-usage.json").read_text()
                          ) as call:
            data = m.agy_usage()
        self.assertEqual(5, len(data["limits"]))
        self.assertEqual(77, data["limits"][0]["remaining"])
        self.assertEqual(1, data["limits"][2]["remaining"])
        call.assert_called_once_with(
            ["-p", "/usage", "--output-format", "json", "--print-timeout", "60s"], 40)

    def test_agy_invalid_fields_source_error(self):
        fixtures = json.loads((FIXTURES / "agy-usage.json").read_text())
        bucket = fixtures["command"]["data"]["groups"][0]["buckets"][0]
        for field, value in (("remaining_fraction", None), ("remaining_fraction", float("nan")),
                            ("remaining_fraction", True), ("remaining_fraction", 2),
                            ("reset_time", "invalid"), ("window", [])):
            raw = json.loads(json.dumps(fixtures))
            raw["command"]["data"]["groups"][0]["buckets"][0][field] = value
            with self.subTest(field=field, value=value), patch.object(
                    m, "run_agy", return_value=json.dumps(raw)):
                self.assertIn("error", m.safe(m.agy_usage))
        self.assertIn("remaining_fraction", bucket)

    def test_agy_process_and_timeout(self):
        process = Mock(returncode=0, pid=43210)
        process.communicate.return_value = ("response", "")
        self.popen.side_effect = None
        self.popen.return_value = process
        self.assertEqual("response", m.run_agy(["models"], 30))
        kw = self.popen.call_args.kwargs
        self.assertTrue(kw["start_new_session"])
        self.assertEqual(str(m.HOME), kw["cwd"])
        self.assertEqual(subprocess.DEVNULL, kw["stdin"])
        process.returncode = 1
        with self.assertRaises(RuntimeError):
            m.run_agy(["models"], 1)
        process.communicate.side_effect = [subprocess.TimeoutExpired("agy", 1), ("", "")]
        with patch.object(m.os, "killpg") as kill:
            with self.assertRaises(TimeoutError):
                m.run_agy(["models"], 1)
            kill.assert_called_once_with(43210, signal.SIGKILL)
        process.communicate.side_effect = subprocess.TimeoutExpired("agy", 1)
        with patch.object(m.os, "killpg", side_effect=ProcessLookupError):
            with self.assertRaises(TimeoutError):
                m.run_agy(["models"], 1)
        process.stdout.close.assert_called_once()
        process.stderr.close.assert_called_once()

    def write_codex(self, name, lines, age=0):
        path = m.HOME / ".codex/sessions" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(lines)
        os.utime(path, (self.now - age, self.now - age))
        return path

    def codex_line(self, **kw):
        window = dict(used_percent=25, resets_at=self.now + 3600, window_minutes=10080)
        window.update(kw)
        return json.dumps({"payload": {"rate_limits": {"primary": window,
                                                      "secondary": {"used_percent": None,
                                                                    "resets_at": None}}}})

    def test_codex_fixture(self):
        self.write_codex("a.jsonl", (FIXTURES / "codex-line.json").read_text())
        data = m.codex_usage()
        self.assertEqual(50, data["limits"][0]["remaining"])
        self.assertEqual(m.iso_epoch("2026-11-07T13:05:49.818Z"), data["data_time"])

    def test_codex_large_file_null_invalid_line(self):
        path = self.write_codex("a.jsonl", "é" * 600000 + "\n" + self.codex_line() +
                             '\n{invalid "rate_limits"\n{"rate_limits":{}}\n', age=100)
        with patch.object(m, "tail", wraps=m.tail) as read:
            data = m.codex_usage()
            read.assert_called_once_with(path)
        self.assertEqual(75, data["limits"][0]["remaining"])
        self.assertEqual(1, len(data["limits"]))
        self.assertEqual(self.now - 100, data["data_time"])
        self.assertLess(sum(map(len, m.tail(path))), 512 * 1024)

    def test_codex_file_error_and_most_new_record(self):
        self.write_codex("a.jsonl", self.codex_line(), age=50)
        newer = json.loads(self.codex_line(used_percent=10))
        newer["timestamp"] = m.datetime.fromtimestamp(self.now, timezone.utc).isoformat()
        self.write_codex("b.jsonl", json.dumps(newer), age=100)
        invalid = self.write_codex("c.jsonl", "", age=0)
        invalid.write_bytes(b'\xff"rate_limits"')
        self.assertEqual(90, m.codex_usage()["limits"][0]["remaining"])
        with patch.object(m, "tail", side_effect=OSError):
            self.assertIn("error", m.safe(m.codex_usage))

    def test_codex_five_file_and_duration_budget(self):
        for i in range(8):
            self.write_codex(str(i) + ".jsonl", '"rate_limits"', age=i)
        with patch.object(m, "tail", wraps=m.tail) as read:
            self.assertIn("error", m.safe(m.codex_usage))
            self.assertEqual(5, read.call_count)
        with patch.object(m.time, "monotonic", side_effect=[0, 2, 2]):
            self.assertIn("error", m.safe(m.codex_usage))
        for minutes, name in ((300, "5-hour"), (17, "17 minutes")):
            self.write_codex("0.jsonl", self.codex_line(window_minutes=minutes))
            self.assertEqual(name, m.codex_usage()["limits"][0]["window"])

    def test_claude_validation_existing_record_preserves(self):
        self.write_json(m.CLAUDE_CACHE, {"original": 1})
        fixtures = dict(label="Weekly", percentUsed=3, resetsAt="2027-01-01T00:00:00Z")
        windows = [[], None, [None], [dict(fixtures, label="")],
                      [dict(fixtures, percentUsed=None)], [dict(fixtures, percentUsed=True)],
                      [dict(fixtures, percentUsed=101)], [dict(fixtures, resetsAt="invalid")],
                      [dict(fixtures, resetsAt=None)]]
        for items in windows:
            with self.subTest(items=items):
                with patch.object(m.sys, "stdin", io.StringIO(json.dumps({"windows": items}))):
                    self.assertEqual(1, self.cli("--claude-save")[0])
                self.assertEqual({"original": 1}, m.read_json(m.CLAUDE_CACHE))
        with patch.object(m.sys, "stdin", io.StringIO("{}")):
            self.assertEqual((1, "", ""), self.cli("--claude-save", "--quiet"))
        for wrapped in (False, True):
            data = {"windows": [dict(fixtures, resetsAt=None), fixtures]}
            m.claude_save(json.dumps({"plan": data} if wrapped else data))
            self.assertEqual(1, len(m.read_json(m.CLAUDE_CACHE)["windows"]))
            self.assertEqual(97, m.claude_usage()["limits"][0]["remaining"])

    def test_claude_missing_and_invalid(self):
        self.assertEqual({}, m.claude_usage())
        for data in ({}, {"saved_at": None}, {"saved_at": self.now, "windows": []}):
            self.write_json(m.CLAUDE_CACHE, data)
            self.assertIn("error", m.safe(m.claude_usage))

    def test_models_fixtures_and_empty_source(self):
        with patch.object(m, "run_agy", return_value=(FIXTURES / "agy-models.txt").read_text()):
            self.assertEqual(13, len(m.agy_models()))
        for raw in ("", "Fetching available models...", "info"):
            with patch.object(m, "run_agy", return_value=raw):
                with self.assertRaises(ValueError):
                    m.agy_models()
        (m.codex_home() / "sessions").mkdir(parents=True)
        with self.assertRaises(ValueError):
            m.codex_models()
        self.write_json(m.HOME / ".codex/models_cache.json", {"models": [{"slug": "x"}]})
        self.write_json(m.HOME / ".codex/models_cache.codex_work_desktop.json",
                 {"models": [{"slug": "y", "display_name": "Y"}]})
        self.assertEqual({"codex:x": "x", "codex:y": "Y"}, m.codex_models())
        self.patch_value("agy_models", Mock(side_effect=ValueError))
        result = m.models()
        self.assertEqual(["codex"], result["successful"])
        self.assertIn("agy", result["errors"])

    def test_model_baseline_new_removed_and_partial_accept_models(self):
        data = self.models("codex", old="Old")
        self.assertEqual({"codex": 1}, m.model_status(data)["baseline_created"])
        self.assertIn("agy baseline created (1 model)",
                      m.render_text({"models_status": m.model_status(self.models(x="X"))}))
        data = self.models("codex", new="New")
        status = m.model_status(data)
        self.assertEqual({"codex:new": "New"}, status["new"])
        self.assertEqual({"codex:old": "Old"}, status["removed"])
        self.assertIn("Removed model", m.render_text({"models_status": status}))
        m.model_status(data, True)
        self.assertEqual({"agy:x": "X", "codex:new": "New"},
                         m.read_json(m.MODELS_FILE)["models"])

    def test_invalid_model_baseline_preserved(self):
        for raw in ("{", "[]", '{"models": null}', '{"models": {"agy:x": null}}'):
            m.MODELS_FILE.parent.mkdir(parents=True, exist_ok=True)
            m.MODELS_FILE.write_text(raw)
            self.assertIn("error", m.safe(m.model_status, self.models(x="X")))
            self.assertEqual(raw, m.MODELS_FILE.read_text())
            self.assertIn("error", m.safe(m.model_status, self.models(x="X"), True))

    def test_accept_models_failed_and_successful(self):
        self.write_json(m.MODELS_FILE, {"models": {"agy:old": "Old", "codex:x": "X"}})
        before = m.MODELS_FILE.read_bytes()
        self.patch_value("models", Mock(return_value={"successful": [], "models": {},
                                                  "errors": {"agy": "error"}}))
        self.assertEqual(1, self.cli("--accept-models")[0])
        self.assertEqual((1, "", ""), self.cli("--accept-models", "--quiet"))
        self.assertEqual(before, m.MODELS_FILE.read_bytes())
        data = self.data(self.limit(), models_status={"new": {"agy:new": "New"}})
        self.write_json(m.CACHE, data)
        m.models.return_value = self.models(new="New")
        code, output, _ = self.cli("--accept-models")
        self.assertEqual(0, code)
        self.assertIn("Compared sources: agy", output)
        self.assertIn("Added: New", output)
        self.assertIn("Removed: Old", output)
        self.assertFalse(m.warnings(m.read_cache())[1:])
        self.assertNotIn("NEW MODEL", str(m.warnings(m.read_cache())))
        self.assertIn("codex:x", m.read_json(m.MODELS_FILE)["models"])
        self.assertEqual(1, len(m.read_cache()["codex"]["limits"]))

    def test_lock_ownership_staleness_and_separate_file_descriptor(self):
        self.assertTrue(m.acquire_lock())
        self.assertFalse(m.acquire_lock())
        # Another call cannot acquire the lock after forgetting modulee ownership.
        owner = m._lock
        m._lock = None
        try:
            self.assertFalse(m.acquire_lock())
        finally:
            m._lock = owner
        m.LOCK.write_text("other")
        m.release_lock()
        self.assertTrue(m.LOCK.exists())
        os.utime(m.LOCK, (self.now - 181, self.now - 181))
        self.assertTrue(m.acquire_lock())
        m.release_lock()
        self.assertFalse(m.LOCK.exists())
        self.assertIsNone(m._lock)

    def test_lock_active_stale_file_and_inode(self):
        self.assertTrue(m.acquire_lock())
        os.utime(m.LOCK, (self.now - 1000, self.now - 1000))
        owner = m._lock
        m._lock = None
        try:
            self.assertFalse(m.acquire_lock())
        finally:
            m._lock = owner
        m.LOCK.unlink()
        m.LOCK.write_text(str(os.getpid()))
        m.release_lock()
        self.assertTrue(m.LOCK.exists())

    def test_collection_last_good_data_and_lock_finally(self):
        old = {"limits": [self.limit(source="Antigravity", name="Gemini")],
                "data_time": self.now - 100}
        self.write_json(m.CACHE, self.data(self.limit(), agy=old))
        self.mock_collection(codex={"limits": [self.limit()]})
        result = m.collect()
        self.assertEqual(old["limits"], result["agy"]["limits"])
        self.assertEqual("missing", result["agy"]["last_error"])
        self.assertNotIn("warnings", m.read_json(m.CACHE))
        self.assertFalse(m.LOCK.exists())
        with patch.object(m, "atomic_json", side_effect=OSError):
            with self.assertRaises(OSError):
                m.collect()
        self.assertFalse(m.LOCK.exists())

    def test_refresh_locked_source_uncalled(self):
        self.mock_collection()
        self.assertTrue(m.acquire_lock())
        self.assertEqual("refresh already in progress", m.collect()["note"])
        m.agy_usage.assert_not_called()
        self.assertIn("refresh already in progress", self.cli("--refresh")[1])
        self.assertEqual((0, "", ""), self.cli("--refresh", "--quiet"))

    def test_cache_invalid_fields_source_per_isolated(self):
        for raw in ("{", "[]", "{}", '{"timestamp":null,"version":2}',
                    '{"timestamp":100,"version":1}'):
            m.CACHE.parent.mkdir(parents=True, exist_ok=True)
            m.CACHE.write_text(raw)
            self.assertIsNone(m.read_cache())
        for invalid in (None, [], {"limits": [None]}, {"limits": "x"},
                      {"data_time": None}, {"note": []}):
            self.write_json(m.CACHE, self.data(self.limit(), agy=invalid))
            data = m.read_cache()
            self.assertIn("error", data["agy"])
            self.assertEqual(1, len(data["codex"]["limits"]))
        self.write_json(m.CACHE, self.data(self.limit(), models_status={"new": []},
                                  model_sources=[]))
        data = m.read_cache()
        self.assertIn("error", data["models_status"])
        self.assertEqual(1, len(data["codex"]["limits"]))
        self.assertIn("Codex", m.menubar(data))

    def test_cache_freshness_reset_and_future(self):
        self.assertTrue(m.cache_fresh(self.data()))
        for data in (None, self.data(timestamp=self.now - 1800),
                     self.data(timestamp=self.now + 1),
                     self.data(agy={"limits": [self.limit(reset=self.now)]})):
            self.assertFalse(m.cache_fresh(data))

    def test_fast_modes_never_agy_waits(self):
        self.mock_collection(codex={"limits": [self.limit()]})
        self.patch_value("refresh_background", Mock())
        started = time.perf_counter()
        for mode in ("--short", "--menubar"):
            for suffix in ([], ["--refresh"], ["--accept-models"], ["--claude-save"]):
                self.assertEqual(0, self.cli(mode, *suffix)[0])
        self.assertLess(time.perf_counter() - started, 2)
        m.agy_usage.assert_not_called()
        m.models.assert_not_called()
        self.write_json(m.CACHE, self.data(self.limit()))
        m.refresh_background.reset_mock()
        self.cli("--short")
        m.refresh_background.assert_not_called()
        self.write_json(m.CACHE, self.data(timestamp=self.now - 1801))
        self.cli("--menubar")
        m.refresh_background.assert_called_once()

    def test_background_command_and_quiet_error(self):
        self.popen.side_effect = None
        m.refresh_background()
        self.assertEqual([m.sys.executable, str(m.ROOT / "ai_limits.py"), "--refresh",
                          "--quiet"], self.popen.call_args.args[0])
        kw = self.popen.call_args.kwargs
        self.assertTrue(kw["start_new_session"])
        for name in ("stdin", "stdout", "stderr"):
            self.assertEqual(subprocess.DEVNULL, kw[name])
        self.mock_collection()
        self.popen.side_effect = OSError
        self.assertEqual(0, self.cli("--short")[0])

    def test_normal_json_fresh_and_refresh(self):
        self.mock_collection(codex={"limits": [self.limit()]})
        self.write_json(m.CACHE, self.data(self.limit()))
        self.assertEqual(0, self.cli("--json")[0])
        m.agy_usage.assert_not_called()
        code, output, _ = self.cli("--json", "--refresh")
        self.assertEqual(0, code)
        raw = json.loads(output)
        self.assertIn("warnings", raw)
        self.assertIn("reset_text", raw["codex"]["limits"][0])
        self.assertNotIn("warnings", m.read_json(m.CACHE))
        m.agy_usage.assert_called_once()
        self.assertIn("cache", self.cli()[1])

    def test_table_durations_boundaries(self):
        for duration, render_text in ((59 * 60, "59m"), (3600, "1h 0m"),
                            (86340, "23h 59m"), (86400, "1d 0h")):
            self.assertEqual(render_text, m.format_duration(duration))
        self.assertEqual("0m", m.format_duration(-1))

    def test_menubar_table_alignment_names_colors(self):
        claude = [self.limit(source="Claude", name=name, reset=self.now + duration,
                             data_time=self.now - age)
                  for name, duration, age in (("5-hour limit", 3540, 0),
                                        ("Weekly · all models", 3600, 0),
                                        ("Weekly · Sonnet", 86340, 25200),
                                        ("Weekly · Opus", -1, 0))]
        agy = [self.limit(source="Antigravity", name=name, reset=self.now + 172800)
               for name in ("Gemini Models", "Claude and GPT models", "Gemini 4 Argon", "Other")]
        data = self.data(self.limit(remaining=9, reset=self.now + 86400),
                         claude={"limits": claude, "saved_at": self.now,
                                 "data_time": self.now - 7200},
                         agy={"limits": agy, "data_time": self.now - 180},
                         codex={"limits": [self.limit(remaining=9, reset=self.now + 86400)],
                                "data_time": self.now - 720})
        lines = m.menubar(data).splitlines()
        table = lines[2:12]
        title = table[0].split(" | ")[0]
        duration_start, format_date_start = title.index("Time left"), title.index("Resets")
        percent_last = title.index("Left") + len("Left")
        self.assertEqual("AI | color=red", lines[0])
        self.assertIn("color=gray", table[0])
        durations = ["Time left", "59m", "1h 0m", "23h 59m (stale record)",
                   "no fresh data", "1d 0h"] + ["2d 0h"] * 4
        for line, duration in zip(table, durations):
            self.assertIn(" | font=Menlo size=12 trim=false", line)
            render_text = line.split(" | ")[0]
            self.assertEqual(duration, render_text[duration_start:format_date_start].rstrip())
            self.assertEqual(render_text[format_date_start:],
                             render_text[format_date_start:].rstrip())
        for line in table[1:]:
            self.assertTrue(line[:percent_last].endswith(("40%", "9%", "-")))
        for name in ("5-hour", "Weekly (Sonnet)", "Weekly (Opus)", "Gemini Weekly",
                   "Claude/GPT Weekly", "Argon Weekly", "Other Weekly"):
            self.assertIn(name, "\n".join(table))
        self.assertIn("color=gray", table[3])
        self.assertIn("color=gray", table[4])
        self.assertNotIn("color=", table[5])
        measurement = next(s for s in lines if s.startswith("Measured:"))
        self.assertIn("Claude 2h 0m ago · Codex 12m ago · Antigravity 3m ago", measurement)
        self.assertNotIn("measured", "\n".join(table))
        self.assertIn("param2=--refresh param3=--quiet", lines[-1])

    def test_menubar_reminder_title_excluded_and_expired(self):
        x = self.limit(source="Claude", name="Weekly Sonnet", remaining=1,
                       data_time=self.now - 25200, reset=self.now + 172800)
        data = self.data(self.limit(reset=self.now + 172800),
                         claude={"saved_at": self.now - 25200, "limits": [x]},
                         agy={"error": "interrupted"})
        output = m.menubar(data)
        self.assertEqual("AI", output.splitlines()[0])
        self.assertIn("agy: interrupted | color=gray", output)
        self.assertIn("CLAUDE DATA STALE", output)
        self.assertIn("Weekly (Sonnet)", output)
        self.assertTrue(next(s for s in output.splitlines() if s.startswith(
            "CLAUDE DATA STALE")).endswith(" | color=gray"))
        expired = self.data(self.limit(reset=self.now - 1, remaining=4))
        self.assertIn("reset, no fresh data", m.render_text(expired))
        self.assertNotIn("(0m)", m.render_text(expired))
        self.assertEqual("AI", m.menubar(expired).splitlines()[0])
        self.assertTrue(m.json_data(expired)["codex"]["limits"][0]["expired"])

    def test_atomic_write_error_old_record_preserves(self):
        self.write_json(m.CACHE, {"original": 1})
        with patch.object(m.os, "replace", side_effect=OSError):
            with self.assertRaises(OSError):
                self.write_json(m.CACHE, {"new": 2})
        self.assertEqual({"original": 1}, m.read_json(m.CACHE))
        self.assertEqual([m.CACHE], list(m.DATA.iterdir()))

    def test_deep_invalid_cache_fast_local_data_unblocked(self):
        m.CACHE.parent.mkdir(parents=True, exist_ok=True)
        m.CACHE.write_text("[" * 2000 + "]" * 2000)
        self.assertIsNone(m.read_cache())
        self.mock_collection(codex={"limits": [self.limit(remaining=1)]})
        self.patch_value("refresh_background", Mock())
        self.assertIn("LOW: Codex", self.cli("--short")[1])
        m.agy_usage.assert_not_called()

    def test_codex_expired_percent_and_quiet_error(self):
        self.write_codex("a.jsonl", self.codex_line(resets_at=self.now - 1))
        result = m.codex_usage()
        self.assertEqual(100, result["limits"][0]["remaining"])
        self.assertFalse(m.warnings({"codex": result}))
        self.patch_value("prepare", Mock(side_effect=OSError))
        self.assertEqual((0, "", ""), self.cli("--quiet"))
        self.assertEqual(1, self.cli("--json")[0])

    def test_agy_path_fallback_and_found_path(self):
        for found, available, expected in (
                ("/test/bin/agy", (), "/test/bin/agy"),
                (None, ("/opt/homebrew/bin/agy",), "/opt/homebrew/bin/agy"),
                (None, ("/usr/local/bin/agy",), "/usr/local/bin/agy"),
                (None, (), None)):
            with patch.object(m.shutil, "which", return_value=found) as lookup, \
                    patch.object(m.os.path, "isfile", side_effect=lambda p: p in available), \
                    patch.object(m.os, "access", return_value=True):
                module = runpy.run_path(m.__file__, run_name="ai_limits_path_test")
            lookup.assert_called_once_with("agy")
            self.assertEqual(expected, module["AGY"])
        self.patch_value("AGY", "/opt/homebrew/bin/agy")
        self.popen.side_effect = None
        self.popen.return_value = Mock(returncode=0)
        self.popen.return_value.communicate.return_value = ("response", "")
        m.run_agy(["models"], 30)
        self.assertEqual(["/opt/homebrew/bin/agy", "models"], self.popen.call_args.args[0])

    def test_refresh_line_plain_paths(self):
        line = m.menubar({}).splitlines()[-1]
        self.assertEqual(
            f'Refresh | bash="{m.sys.executable}" param1="{m.ROOT / "ai_limits.py"}" '
            "param2=--refresh param3=--quiet terminal=false refresh=true", line)

    def test_refresh_line_spaces_paths(self):
        self.patch_value("ROOT", self.root / "project with spaces")
        with patch.object(m.sys, "executable", "/test/Python App/python3"):
            self.assertEqual(
                f'Refresh | bash="/test/Python App/python3" '
                f'param1="{m.ROOT / "ai_limits.py"}" param2=--refresh '
                'param3=--quiet terminal=false refresh=true',
                m.menubar({}).splitlines()[-1])

    def test_claude_weekly_prefix_added_label(self):
        self.assertEqual("Weekly (Opus)", m.table_name(
            self.limit(source="Claude", name="Claude Weekly · Opus")))

    def test_cli_errors_explained(self):
        with patch.object(m.sys, "stdin", io.StringIO('{"windows": []}')):
            code, output, error = self.cli("--claude-save")
        self.assertEqual((1, ""), (code, output))
        self.assertIn("Could not save Claude usage: Claude windows are empty or invalid", error)
        self.assertNotIn("ValueError", error)
        self.assertTrue(m.acquire_lock())
        code, output, error = self.cli("--accept-models")
        self.assertEqual((1, ""), (code, output))
        self.assertIn("refresh already in progress", error)
        self.assertNotIn("ValueError", error)
        self.assertEqual((1, "", ""), self.cli("--accept-models", "--quiet"))

    def test_normal_mode_locked_cache_age(self):
        self.mock_collection()
        self.write_json(m.CACHE, self.data(timestamp=self.now - 3600))
        self.assertTrue(m.acquire_lock())
        for args in ([], ["--refresh"]):
            code, output, error = self.cli(*args)
            self.assertEqual((0, ""), (code, error))
            self.assertIn("Data: cache, 1h 0m ago", output)
            self.assertIn("refresh already in progress", output)
            self.assertNotIn("Data: now", output)
        m.agy_usage.assert_not_called()

    def test_codex_partial_utf8_last_line_previous_record_preserves(self):
        path = self.write_codex("a.jsonl", self.codex_line() + "\n")
        with path.open("ab") as file:
            file.write(b'{"rate_limits": "\xc4')
        self.assertIn("\ufffd", m.tail(path)[-1])
        self.assertEqual(75, m.codex_usage()["limits"][0]["remaining"])

    def test_new_model_measurement_age(self):
        for age in (0, 7200, 7201, 10800):
            data = self.data(timestamp=self.now - age,
                             models_status={"new": {"agy:x": "X"}})
            warning = m.warnings(data)[0]
            self.assertEqual(age > 7200, " (measured " in warning)
            if age > 7200:
                self.assertTrue(warning.endswith(f" (measured {m.format_duration(age)} ago)"))

    def test_render_text_warning_title_and_new_model(self):
        self.assertIn("\nWarnings\nNone\n", m.render_text({}) + "\n")
        data = self.data(models_status={"new": {"agy:x": "X"}})
        output = m.render_text(data)
        self.assertIn("\nNew: X (agy:x)\n", output)
        self.assertIn("\nWarnings\nNEW MODEL: X", output)

    def test_menu_title_warning_by_colored(self):
        for data, title in (({}, "AI"),
                             (self.data(self.limit(reset=self.now + 172800)), "AI"),
                             (self.data(self.limit(remaining=1)), "AI"),
                             (self.data(self.limit()), "AI | color=red"),
                             (self.data(self.limit(reset=self.now + 12 * 3600)),
                              "AI | color=orange"),
                             ({"models_status": {"new": {"agy:x": "X"}}},
                              "AI | color=#0A84FF")):
            with self.subTest(title=title, data=data):
                self.assertEqual(title, m.menubar(data).splitlines()[0])
        self.patch_value("prepare", Mock(side_effect=ValueError("invalid")))
        self.assertEqual("AI", self.cli("--menubar")[1].splitlines()[0])

    def test_menu_warning_colors_and_priority(self):
        low = self.limit(remaining=9)
        red = self.limit(reset=self.now + 11 * 3600)
        orange = self.limit(reset=self.now + 12 * 3600)
        new = {"models_status": {"new": {"agy:x": "X"}}}
        for limits, color in (((low,), "#0A84FF"),
                               ((low, orange), "orange"),
                               ((orange, low, red), "red"),
                               ((red, low, orange), "red")):
            with self.subTest(limits=limits):
                data = self.data(*limits, **new)
                lines = m.menubar(data).splitlines()
                self.assertEqual(f"AI | color={color}", lines[0])
                for warning in m.warnings(data):
                    if warning.startswith("LOW:"):
                        self.assertIn(warning, lines)
                    elif warning.startswith("RESETS IN 12H"):
                        self.assertIn(warning + " | color=red", lines)
                    elif warning.startswith("RESETS IN 24H"):
                        self.assertIn(warning + " | color=orange", lines)
                    else:
                        self.assertIn(warning + " | color=#0A84FF", lines)

    def test_absent_agy_usage_and_models_are_silent(self):
        self.patch_value("AGY", None)
        self.assertEqual({}, m.agy_usage())
        self.assertEqual({}, m.agy_models())
        data = m.collect()
        self.assertEqual([], m.source_notes(data))
        self.assertEqual([], data["model_sources"]["successful"])
        self.assertEqual({}, data["model_sources"]["errors"])
        self.assertNotIn("Antigravity", m.render_text(data))
        self.popen.assert_not_called()

    def test_absent_codex_sessions_are_silent(self):
        self.assertFalse((m.codex_home() / "sessions").exists())
        self.assertEqual({}, m.codex_usage())
        self.assertEqual({}, m.codex_models())
        self.assertEqual([], m.source_notes({"codex": m.codex_usage()}))
        self.assertNotIn("Codex:", m.render_text({"codex": m.codex_usage()}))

    def test_unsaved_claude_is_silent_and_saved_stale_record_has_hint(self):
        data = {"claude": m.claude_usage()}
        self.assertEqual({}, data["claude"])
        self.assertEqual([], m.source_notes(data))
        self.assertEqual("", m.claude_stale_hint(data))
        self.assertNotIn("Claude", m.render_text(data))
        self.assertNotIn("CLAUDE", m.menubar(data))
        m.claude_save(json.dumps({"windows": [
            {"label": "Weekly", "percentUsed": 42, "resetsAt": "2027-01-01T00:00:00Z"}]}))
        record = m.read_json(m.CLAUDE_CACHE)
        record["saved_at"] = self.now - 25200
        self.write_json(m.CLAUDE_CACHE, record)
        data["claude"] = m.claude_usage()
        self.assertEqual(
            "CLAUDE DATA STALE: last saved 7h 0m ago; "
            "save fresh usage with ai_limits.py --claude-save", m.claude_stale_hint(data))

    def test_codex_home_override_and_default(self):
        custom = self.root / "custom codex"
        with patch.dict(os.environ, {"CODEX_HOME": str(custom)}):
            self.assertEqual(custom, m.codex_home())
            self.write_json(custom / "models_cache.json", {"models": [{"slug": "custom"}]})
            sessions = custom / "sessions"
            sessions.mkdir()
            (sessions / "a.jsonl").write_text(self.codex_line())
            self.assertEqual(75, m.codex_usage()["limits"][0]["remaining"])
            self.assertEqual({"codex:custom": "custom"}, m.codex_models())
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(self.root / ".codex", m.codex_home())

    def test_one_or_two_sources_can_be_used(self):
        self.patch_value("AGY", None)
        self.write_codex("a.jsonl", self.codex_line())
        self.write_json(m.codex_home() / "models_cache.json", {"models": [{"slug": "x"}]})
        data = m.collect()
        self.assertEqual(["Codex"], [x["source"] for x in m.all_limits(data)])
        self.assertEqual([], m.source_notes(data))
        m.claude_save(json.dumps({"plan": {"windows": [
            {"label": "Weekly", "percentUsed": 42, "resetsAt": "2027-01-01T00:00:00Z"}]}}))
        data = m.collect()
        self.assertEqual(["Claude", "Codex"], [x["source"] for x in m.all_limits(data)])
        self.assertEqual([], m.source_notes(data))

    def test_local_date_format_with_fixed_english_months(self):
        epoch = m.iso_epoch("2026-11-14T04:43:20Z")
        try:
            for zone, expected in (("UTC0", "Nov 14 04:43"), ("EST5", "Nov 13 23:43")):
                with patch.dict(os.environ, {"TZ": zone}):
                    time.tzset()
                    self.assertEqual(expected, m.format_date(epoch))
            with patch.dict(os.environ, {"TZ": "UTC0"}):
                time.tzset()
                for month, name in enumerate(
                        ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
                         "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"), 1):
                    epoch = m.datetime(2026, month, 3, 4, 5, tzinfo=timezone.utc).timestamp()
                    self.assertEqual(f"{name} 03 04:05", m.format_date(epoch))
            local = Mock(month=3, day=3, hour=4, minute=5)
            local.strftime.side_effect = AssertionError("Locale-dependent formatting")
            with patch.object(m, "datetime") as dates:
                dates.fromtimestamp.return_value = local
                self.assertEqual("Mar 03 04:05", m.format_date(epoch))
                dates.fromtimestamp.assert_called_once_with(epoch)
                local.strftime.assert_not_called()
        finally:
            time.tzset()

    def test_all_agy_call_sites_only_request_usage_models_or_fixed_hello(self):
        tree = ast.parse(Path(m.__file__).read_text())
        calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Name) and node.func.id == "run_agy"]
        self.assertEqual(3, len(calls))
        commands = []
        for call in calls:
            command = call.args[0]
            if any(isinstance(node, ast.Name) for node in ast.walk(command)):
                self.assertEqual("send_hello", next(
                    node.name for node in tree.body if isinstance(node, ast.FunctionDef)
                    and call in ast.walk(node)))
                self.assertEqual("model", command.elts[3].id)
                command.elts[3] = ast.Constant(value="test-model")
            commands.append(ast.literal_eval(command))
        self.assertIn(["models"], commands)
        self.assertIn(["-p", "/usage", "--output-format", "json",
                       "--print-timeout", "60s"], commands)
        self.assertIn(["-p", "hello", "--model", "test-model", "--output-format", "json",
                       "--print-timeout", "60s"], commands)

    def test_agy_rejects_arbitrary_or_incomplete_prompt_commands_before_spawning(self):
        for args in (["-p", "hello"], ["-p", "/usage", "--model", "x"], [],
                     ["chat"], ["models", "--other"],
                     ["-p", "another prompt", "--model", "x", "--output-format", "json",
                      "--print-timeout", "60s"],
                     ["-p", "hello", "--model", "x", "--output-format", "json",
                      "--print-timeout", "60s", "--other"]):
            with self.subTest(args=args), self.assertRaises(ValueError):
                m.run_agy(args, 1)
        self.popen.assert_not_called()

    def test_cli_help_describes_each_flag(self):
        with self.assertRaises(SystemExit) as exit_code:
            with contextlib.redirect_stdout(io.StringIO()) as output:
                m.main(["--help"])
        self.assertEqual(0, exit_code.exception.code)
        for flag, description in (
                ("--json", "Print structured JSON"), ("--short", "Print up to three warnings"),
                ("--menubar", "Print the SwiftBar menu"), ("--refresh", "Refresh cached usage"),
                ("--quiet", "Suppress output"), ("--accept-models", "Accept current model lists"),
                ("--claude-save", "Save Claude usage JSON")):
            self.assertIn(flag, output.getvalue())
            self.assertIn(description, output.getvalue())
        self.popen.assert_not_called()

    def hello_pair(self):
        x = self.limit(source="Antigravity", name="Gemini Models", remaining=100,
                       reset=self.now + 7 * 86400)
        return x, dict(x, reset=x["reset"] - 600, data_time=self.now - 600)

    def enable_hello(self):
        self.write_json(m.SETTINGS, {"auto_hello": True})

    def test_weekly_not_started_detection(self):
        self.enable_hello()
        x, old = self.hello_pair()
        data = self.models(**{"flash-low": "X"})
        cases = [
            ("sliding", old, True),
            ("fixed", dict(old, reset=x["reset"]), False),
            ("fixed short interval", dict(old, reset=x["reset"],
                                          data_time=self.now - 60), False),
            ("first reset", dict(old, reset=x["reset"] - 7 * 86400), False),
            ("previous 99", dict(old, remaining=99), False),
            ("another group", dict(old, name="Gemini 4 Argon"), False),
            ("short interval", dict(old, data_time=self.now - 59), False),
            ("tolerance boundary", dict(old, reset=x["reset"] - 900), False),
            ("exactly 60 seconds", dict(old, reset=x["reset"] - 60,
                                        data_time=self.now - 60), True),
            ("old schema", {"name": "Gemini Models", "remaining": 100}, False),
            ("invalid", None, False),
        ]
        for name, previous, expected in cases:
            with self.subTest(name=name):
                self.assertEqual(expected, m.weekly_not_started(x, previous))
        for change in ({"remaining": 99}, {"window": "5-hour"}, {"source": "Codex"}):
            self.assertFalse(m.weekly_not_started(dict(x, **change), old))
        with patch.object(m, "run_agy", return_value="{}") as call:
            for _, previous, expected in cases:
                if not expected:
                    m.send_hello([x], data, [previous])
            m.send_hello([dict(x, remaining=99)], data, [old])
            m.send_hello([x], data, [])
            call.assert_not_called()
            m.send_hello([x], data, [old])
            call.assert_called_once()

    def test_hello_model_selection_and_fallbacks(self):
        data = self.models(**{"gemini-3.8-flash-low": "X", "other-flash-low": "Y",
                              "argon-high": "Z", "argon-low": "A", "sonnet-low": "S",
                              "gpt-oss-high": "G"})
        self.assertEqual("gemini-3.8-flash-low", m.hello_model("Gemini Models", data))
        self.assertEqual("argon-low", m.hello_model("Gemini 4 Argon", data))
        self.assertEqual("gpt-oss-high", m.hello_model("Claude and GPT models", data))
        del data["models"]["agy:gpt-oss-high"]
        self.assertEqual("sonnet-low", m.hello_model("Claude and GPT models", data))
        del data["models"]["agy:gemini-3.8-flash-low"]
        self.assertEqual("other-flash-low", m.hello_model("Gemini Models", data))
        self.assertIsNone(m.hello_model("Unknown", data))
        for group, model in (("Gemini 4 Argon", "gemini-4-argon-low"),
                             ("Claude and GPT models", "gpt-oss-120b-medium")):
            data["models"]["agy:" + model] = "Preferred"
            self.assertEqual(model, m.hello_model(group, data))
        data["successful"] = []
        self.assertIsNone(m.hello_model("Gemini Models", data))

    def test_hello_cooldown_command_state_and_log(self):
        self.enable_hello()
        x, old = self.hello_pair()
        data = self.models(**{"gemini-3.8-flash-low": "X"})
        with patch.object(m, "run_agy", return_value="{}") as call:
            m.send_hello([x, x], data, [old])
            m.send_hello([x], data, [old])
            call.assert_called_once_with(
                ["-p", "hello", "--model", "gemini-3.8-flash-low",
                 "--output-format", "json", "--print-timeout", "60s"], 90)
            self.write_json(m.HELLO_STATE, {"Gemini Models": self.now - 21599})
            m.send_hello([x], data, [old])
            self.assertEqual(1, call.call_count)
            self.write_json(m.HELLO_STATE, {"Gemini Models": self.now - 21600})
            m.send_hello([x], data, [old])
            self.assertEqual(2, call.call_count)
        self.assertEqual({"Gemini Models": self.now}, m.read_json(m.HELLO_STATE))
        self.assertEqual("sent", m.last_hello()["result"])
        self.assertEqual(2, len(m.HELLO_LOG.read_text().splitlines()))
        self.assertIn("Antigravity:\nLast automatic message: Gemini Models " +
                      m.format_date(self.now) + " (gemini-3.8-flash-low, sent)",
                      m.render_text({}))
        self.assertNotIn("Last automatic message", m.menubar({}))
        self.assertEqual(m.last_hello(), m.json_data({})["last_hello"])
        self.write_json(m.SETTINGS, {"auto_hello": False})
        self.assertIsNone(m.last_hello())
        self.assertIsNone(m.json_data({})["last_hello"])
        self.assertNotIn("Last automatic message", m.render_text({}))

    def test_hello_invalid_state_no_model_and_error(self):
        self.enable_hello()
        x, old = self.hello_pair()
        for state in ([], {"Gemini Models": None}, {"Gemini Models": "invalid"},
                      {"Gemini Models": True}, {"Gemini Models": float("nan")}):
            m.HELLO_STATE.write_text(json.dumps(state))
            with patch.object(m, "run_agy", side_effect=RuntimeError) as call:
                m.send_hello([x], self.models(), [old])
                call.assert_not_called()
                self.assertEqual("no model", m.last_hello()["result"])
                m.send_hello([x], self.models(**{"flash-low": "X"}), [old])
                self.assertEqual("error: RuntimeError", m.last_hello()["result"])
        with m.HELLO_LOG.open("a") as file:
            file.write('\n{\n[]\n{"time":null}\n')
        self.assertEqual("error: RuntimeError", m.last_hello()["result"])

    def test_hello_only_fresh_collection_and_under_lock(self):
        self.enable_hello()
        x, previous = self.hello_pair()
        self.mock_collection(agy={"limits": [x]}, models=self.models(**{"flash-low": "X"}))

        def check(*_):
            self.assertTrue(m.LOCK.exists())
            self.assertIsNotNone(m._lock)

        with patch.object(m, "send_hello", side_effect=check) as send:
            m.collect()
            send.assert_called_once()
            self.assertEqual([], send.call_args.args[2])
            self.write_json(m.CACHE, self.data(agy={"limits": [previous]}))
            m.collect()
            self.assertEqual([previous], send.call_args.args[2])
            m.agy_usage.return_value = {"error": "interrupted"}
            send.reset_mock()
            result = m.collect()
            send.assert_not_called()
            self.assertEqual("interrupted", result["agy"]["last_error"])
            m.agy_usage.return_value = {"limits": [x]}
            m.collect()
            self.assertEqual([], send.call_args.args[2])
            for error in ("error", "last_error"):
                self.write_json(m.CACHE, self.data(agy={"limits": [previous], error: "failed"}))
                m.collect()
                self.assertEqual([], send.call_args.args[2])
            send.reset_mock()
            self.write_json(m.SETTINGS, {"auto_hello": False})
            m.collect()
            send.assert_not_called()
        self.enable_hello()
        with patch.object(m, "send_hello", side_effect=OSError):
            self.assertIn("codex", m.collect())

    def test_auto_hello_default_off_even_when_weekly_not_started(self):
        x, previous = self.hello_pair()
        self.write_json(m.CACHE, self.data(agy={"limits": [previous]}))
        self.mock_collection(agy={"limits": [x]}, models=self.models(**{"flash-low": "X"}))
        with patch.object(m, "run_agy") as call:
            self.assertFalse(m.auto_hello_enabled())
            m.collect()
            m.send_hello([x], self.models(**{"flash-low": "X"}), [previous])
            call.assert_not_called()
        self.assertFalse(m.HELLO_STATE.exists())
        self.assertFalse(m.HELLO_LOG.exists())
        self.assertIsNone(m.json_data({})["last_hello"])
        self.assertNotIn("Last automatic message", m.render_text({}))

    def test_auto_hello_invalid_settings_are_silently_off(self):
        x, previous = self.hello_pair()
        self.mock_collection(agy={"limits": [x]}, models=self.models(**{"flash-low": "X"}))
        m.DATA.mkdir(parents=True, exist_ok=True)
        settings = ["{", "[]", "null", "true", "1", '\"true\"', "{}",
                    '{"auto_hello": false}', '{"auto_hello": 1}', '{"auto_hello": 1.0}',
                    '{"auto_hello": "true"}', '{"auto_hello": null}',
                    '{"auto_hello": []}', '{"auto_hello": {}}',
                    "[" * 2000 + "]" * 2000]
        for raw in settings:
            with self.subTest(raw=raw[:60]), patch.object(m, "run_agy") as call:
                m.SETTINGS.write_text(raw)
                self.write_json(m.CACHE, self.data(agy={"limits": [previous]}))
                self.assertFalse(m.auto_hello_enabled())
                m.collect()
                call.assert_not_called()
                self.assertFalse(m.HELLO_STATE.exists())
                self.assertFalse(m.HELLO_LOG.exists())

    def test_auto_hello_enabled_collection_sends(self):
        x, previous = self.hello_pair()
        self.mock_collection(agy={"limits": [x]}, models=self.models(**{"flash-low": "X"}))
        self.write_json(m.CACHE, self.data(agy={"limits": [previous]}))
        self.enable_hello()
        with patch.object(m, "run_agy", return_value="{}") as call:
            m.collect()
            call.assert_called_once()
        self.assertEqual("sent", m.last_hello()["result"])
        self.assertTrue(m.HELLO_STATE.exists())
        self.assertTrue(m.HELLO_LOG.exists())

    def test_agy_accepts_exact_hello_command(self):
        self.popen.side_effect = None
        self.popen.return_value = Mock(returncode=0)
        self.popen.return_value.communicate.return_value = ("{}", "")
        args = ["-p", "hello", "--model", "flash-low", "--output-format", "json",
                "--print-timeout", "60s"]
        self.assertEqual("{}", m.run_agy(args, 90))
        self.assertEqual([m.AGY] + args, self.popen.call_args.args[0])

    def test_line_length(self):
        for path in (Path(m.__file__), Path(__file__)):
            for number, line in enumerate(path.read_text().splitlines(), 1):
                self.assertLessEqual(len(line), 100, f"{path.name}:{number}")


if __name__ == "__main__":
    unittest.main()
