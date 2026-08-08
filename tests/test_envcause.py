from __future__ import annotations

import os
import json
import io
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from envcause.cli import main
from envcause.core import EnvCauseError, diff_envs, parse_dotenv, redact_value, reduce_environment, run_command, write_repro
from envcause.github_action import run as run_github_action


class ParseDotenvTests(unittest.TestCase):
    def test_parse_common_syntax(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / ".env"
            path.write_text(
                "# comment\n"
                "A=1\n"
                "export B=two\n"
                "C='hello world'\n"
                'D="line\\nnext"\n'
                "E=value # trailing comment\n"
                "HASH=x#y\n",
                encoding="utf-8",
            )
            self.assertEqual(
                parse_dotenv(path),
                {
                    "A": "1",
                    "B": "two",
                    "C": "hello world",
                    "D": "line\nnext",
                    "E": "value",
                    "HASH": "x#y",
                },
            )

    def test_invalid_line(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / ".env"
            path.write_text("NOPE\n", encoding="utf-8")
            with self.assertRaises(EnvCauseError):
                parse_dotenv(path)


class ReductionTests(unittest.TestCase):
    def test_candidate_cache_avoids_duplicate_command_runs(self):
        good = {"A": "0", "B": "0"}
        bad = {"A": "1", "B": "1"}
        code = (
            "import os,sys; "
            "sys.exit(1 if os.getenv('A') == '1' and os.getenv('B') == '1' else 0)"
        )
        cached = reduce_environment(good, bad, [sys.executable, "-c", code])
        uncached = reduce_environment(good, bad, [sys.executable, "-c", code], cache=False)
        self.assertGreater(cached.cache_hits, 0)
        self.assertLess(cached.total_runs, uncached.total_runs)

    def test_diff_includes_added_and_removed_keys(self):
        changes = diff_envs({"A": "1", "B": "2"}, {"A": "9", "C": "3"})
        self.assertEqual([c.key for c in changes], ["A", "B", "C"])

    def test_reduces_interacting_pair(self):
        good = {
            "FEATURE_NEW_AUTH": "false",
            "JWT_ALGORITHM": "HS256",
            "CACHE": "true",
            "LOG_LEVEL": "info",
            "REGION": "west",
        }
        bad = {
            "FEATURE_NEW_AUTH": "true",
            "JWT_ALGORITHM": "RS256",
            "CACHE": "false",
            "LOG_LEVEL": "debug",
            "REGION": "east",
        }
        code = (
            "import os,sys; "
            "bad=(os.getenv('FEATURE_NEW_AUTH')=='true' and os.getenv('JWT_ALGORITHM')=='RS256'); "
            "sys.exit(1 if bad else 0)"
        )
        result = reduce_environment(
            good,
            bad,
            [sys.executable, "-c", code],
            process_env=os.environ,
        )
        self.assertEqual({c.key for c in result.changes}, {"FEATURE_NEW_AUTH", "JWT_ALGORITHM"})

    def test_contains_mode_can_ignore_unrelated_exit_code(self):
        good = {"MODE": "good", "NOISE": "0"}
        bad = {"MODE": "bad", "NOISE": "1"}
        code = (
            "import os,sys; "
            "print('TARGET' if os.getenv('MODE')=='bad' else 'OTHER'); "
            "sys.exit(7)"
        )
        result = reduce_environment(
            good,
            bad,
            [sys.executable, "-c", code],
            contains="TARGET",
            process_env=os.environ,
        )
        self.assertEqual([c.key for c in result.changes], ["MODE"])

    def test_regex_matcher(self):
        result = run_command(
            [sys.executable, "-c", "print('error E1042')"],
            os.environ,
            matches=r"error E\d+",
        )
        self.assertTrue(result.matched_failure)

    def test_junit_matcher(self):
        with tempfile.TemporaryDirectory() as td:
            report = Path(td) / "results.xml"
            code = (
                "from pathlib import Path; "
                f"Path({str(report)!r}).write_text('<testsuite><testcase><failure/></testcase></testsuite>')"
            )
            result = run_command([sys.executable, "-c", code], os.environ, junit=report)
            self.assertTrue(result.matched_failure)

    def test_persistent_cache_reuses_candidate_results(self):
        good = {"A": "0", "B": "0"}
        bad = {"A": "1", "B": "1"}
        code = (
            "import os,sys; "
            "sys.exit(1 if os.getenv('A') == '1' and os.getenv('B') == '1' else 0)"
        )
        with tempfile.TemporaryDirectory() as td:
            cache_path = Path(td) / "cache.json"
            first = reduce_environment(good, bad, [sys.executable, "-c", code], cache_path=cache_path)
            second = reduce_environment(good, bad, [sys.executable, "-c", code], cache_path=cache_path)
            self.assertTrue(cache_path.exists())
            self.assertLess(second.total_runs, first.total_runs)
            self.assertGreater(second.cache_hits, first.cache_hits)

    def test_persistent_cache_ignores_volatile_github_metadata(self):
        good = {"A": "0", "B": "0"}
        bad = {"A": "1", "B": "1"}
        code = (
            "import os,sys; "
            "sys.exit(1 if os.getenv('A') == '1' and os.getenv('B') == '1' else 0)"
        )
        with tempfile.TemporaryDirectory() as td:
            cache_path = Path(td) / "cache.json"
            first_env = {**os.environ, "GITHUB_RUN_ID": "100", "GITHUB_OUTPUT": "/tmp/one"}
            second_env = {**os.environ, "GITHUB_RUN_ID": "101", "GITHUB_OUTPUT": "/tmp/two"}
            reduce_environment(
                good, bad, [sys.executable, "-c", code], cache_path=cache_path, process_env=first_env
            )
            second = reduce_environment(
                good, bad, [sys.executable, "-c", code], cache_path=cache_path, process_env=second_env
            )
            self.assertEqual(second.total_runs, 2)

    def test_progress_callback_receives_cached_and_executed_candidates(self):
        events = []
        good = {"A": "0", "B": "0"}
        bad = {"A": "1", "B": "1"}
        code = (
            "import os,sys; "
            "sys.exit(1 if os.getenv('A') == '1' and os.getenv('B') == '1' else 0)"
        )
        reduce_environment(
            good,
            bad,
            [sys.executable, "-c", code],
            progress=lambda *event: events.append(event),
        )
        self.assertTrue(any(event[3] for event in events))
        self.assertTrue(any(not event[3] for event in events))

    def test_secret_redaction_does_not_hide_feature_auth_flag(self):
        self.assertEqual(redact_value("API_TOKEN", "abc"), "<REDACTED>")
        self.assertEqual(redact_value("FEATURE_NEW_AUTH", "true"), "true")

    def test_write_repro(self):
        good = {"A": "1"}
        bad = {"A": "two words"}
        change = diff_envs(good, bad)
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "repro.env"
            write_repro(path, change)
            text = path.read_text(encoding="utf-8")
            self.assertIn('A="two words"', text)

    def test_cli_writes_redacted_json_report(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            good = root / "good.env"
            bad = root / "bad.env"
            report = root / "report.json"
            good.write_text("API_TOKEN=good\n", encoding="utf-8")
            bad.write_text("API_TOKEN=bad\n", encoding="utf-8")
            code = "import os,sys; sys.exit(os.getenv('API_TOKEN') == 'bad')"
            with redirect_stdout(io.StringIO()):
                exit_code = main([
                    "--good", str(good), "--bad", str(bad),
                    "--report-json", str(report), "--", sys.executable, "-c", code,
                ])
            self.assertEqual(exit_code, 0)
            data = json.loads(report.read_text(encoding="utf-8"))
            self.assertEqual(data["changes"][0]["bad"], "<REDACTED>")


class GitHubActionTests(unittest.TestCase):
    def test_action_writes_outputs_and_summary(self):
        project = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            output = root / "output.txt"
            summary = root / "summary.md"
            environment = {
                "GITHUB_WORKSPACE": str(project),
                "GITHUB_OUTPUT": str(output),
                "GITHUB_STEP_SUMMARY": str(summary),
                "INPUT_GOOD": "examples/good.env",
                "INPUT_BAD": "examples/bad.env",
                "INPUT_COMMAND": f"{sys.executable} examples/demo_app.py",
                "INPUT_REPORT_JSON": str(root / "report.json"),
                "INPUT_CACHE_FILE": "",
                "INPUT_PROGRESS": "false",
                "INPUT_STEP_SUMMARY": "true",
                "INPUT_SHOW_VALUES": "false",
            }
            with patch.dict(os.environ, environment, clear=False), redirect_stdout(io.StringIO()):
                self.assertEqual(run_github_action(), 0)

            outputs = output.read_text(encoding="utf-8")
            self.assertIn("failure-inducing-count=2", outputs)
            self.assertIn("report-path=", outputs)
            summary_text = summary.read_text(encoding="utf-8")
            self.assertIn("## EnvCause result", summary_text)
            self.assertIn("FEATURE_NEW_AUTH", summary_text)


if __name__ == "__main__":
    unittest.main()
