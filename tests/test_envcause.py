from __future__ import annotations

import os
import json
import io
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from envcause.adapters import DockerAdapter, KubernetesAdapter
from envcause.cli import main
from envcause.core import (
    EnvChange,
    EnvCauseError,
    ddmin,
    diff_envs,
    parse_dotenv,
    redact_value,
    reduce_environment,
    run_command,
    write_repro,
)
from envcause.github_action import build_argv as build_github_action_argv, run as run_github_action
from envcause.structured import build_config, diff_configs, load_config


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

    def test_double_quoted_unicode_and_unknown_escapes_are_preserved(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / ".env"
            path.write_text('NAME="café\\q\\nnext"\n', encoding="utf-8")
            self.assertEqual(parse_dotenv(path)["NAME"], "café\\q\nnext")


class StructuredConfigTests(unittest.TestCase):
    def test_nested_diff_and_build_handle_added_and_removed_subtrees(self):
        good = {"database": {"host": "localhost", "legacy": {"enabled": True}}, "workers": 2}
        bad = {"database": {"host": "db.internal", "tls": {"enabled": True}}, "workers": 2}
        changes = diff_configs(good, bad)
        self.assertEqual(
            [change.key for change in changes],
            ["/database/host", "/database/legacy", "/database/tls"],
        )
        self.assertEqual(build_config(good, changes), bad)

    def test_json_cli_reduces_nested_paths_and_leaves_minimal_candidate(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            good_path = root / "good.json"
            bad_path = root / "bad.json"
            candidate = root / "candidate.json"
            report = root / "report.json"
            good = {"auth": {"enabled": False, "algorithm": "HS256"}, "noise": 0}
            bad = {"auth": {"enabled": True, "algorithm": "RS256"}, "noise": 1}
            good_path.write_text(json.dumps(good), encoding="utf-8")
            bad_path.write_text(json.dumps(bad), encoding="utf-8")
            code = (
                "import json,sys; c=json.load(open(sys.argv[1])); "
                "sys.exit(1 if c['auth']['enabled'] and c['auth']['algorithm']=='RS256' else 0)"
            )
            with redirect_stdout(io.StringIO()):
                exit_code = main([
                    "--good", str(good_path), "--bad", str(bad_path),
                    "--config-output", str(candidate), "--report-json", str(report), "--",
                    sys.executable, "-c", code, str(candidate),
                ])
            self.assertEqual(exit_code, 0)
            result = json.loads(report.read_text(encoding="utf-8"))
            self.assertEqual(
                {change["key"] for change in result["changes"]},
                {"/auth/enabled", "/auth/algorithm"},
            )
            self.assertEqual(json.loads(candidate.read_text(encoding="utf-8"))["noise"], 0)

    def test_structured_cli_requires_distinct_candidate_output(self):
        stderr = io.StringIO()
        with tempfile.TemporaryDirectory() as td, redirect_stderr(stderr):
            path = Path(td) / "config.json"
            path.write_text("{}", encoding="utf-8")
            exit_code = main([
                "--good", str(path), "--bad", str(path),
                "--config-output", str(path), "--", sys.executable, "-c", "pass",
            ])
        self.assertEqual(exit_code, 2)
        self.assertIn("must not overwrite", stderr.getvalue())


class ExplainTests(unittest.TestCase):
    def _report(self, root: Path) -> Path:
        report = root / "report.json"
        report.write_text(json.dumps({
            "schema_version": 1,
            "good_config": "good.yaml",
            "bad_config": "bad.yaml",
            "config_format": "yaml",
            "config_output": "candidate.yaml",
            "reproduction_config": None,
            "command": ["python", "reproduce.py", "candidate.yaml"],
            "execution": {"type": "local"},
            "failure_match": {"contains": "connection refused"},
            "original_difference_count": 8,
            "failure_inducing_count": 1,
            "command_executions": 7,
            "cache_hits": 2,
            "changes": [{"key": "/database/host", "good": "localhost", "bad": "db.internal"}],
        }), encoding="utf-8")
        return report

    def test_explain_renders_terminal_diagnosis(self):
        with tempfile.TemporaryDirectory() as td:
            report = self._report(Path(td))
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                self.assertEqual(main(["explain", str(report)]), 0)
            text = stdout.getvalue()
            self.assertIn("EnvCause diagnosis", text)
            self.assertIn("/database/host: localhost -> db.internal", text)
            self.assertIn("python reproduce.py candidate.yaml", text)

    def test_explain_writes_markdown(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            report = self._report(root)
            output = root / "diagnosis.md"
            self.assertEqual(main([
                "explain", str(report), "--format", "markdown", "--output", str(output),
            ]), 0)
            text = output.read_text(encoding="utf-8")
            self.assertIn("# EnvCause diagnosis", text)
            self.assertIn("| `/database/host` | `localhost` | `db.internal` |", text)

    def test_explain_rejects_non_report_json(self):
        with tempfile.TemporaryDirectory() as td:
            report = Path(td) / "bad.json"
            report.write_text("{}", encoding="utf-8")
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                self.assertEqual(main(["explain", str(report)]), 2)
            self.assertIn("Unsupported or incomplete", stderr.getvalue())


class AdapterTests(unittest.TestCase):
    def test_docker_forwards_values_without_putting_them_in_arguments(self):
        adapter = DockerAdapter(
            image="example/app:latest",
            managed_keys=("TOKEN", "REMOVED"),
            run_args=("--network=none",),
        )
        command, host_env, cwd = adapter.prepare(
            ["python", "app.py"],
            {"TOKEN": "super-secret", "PATH": "/bin"},
            "/workspace",
        )
        self.assertEqual(
            command,
            [
                "docker", "run", "--rm", "--network=none", "--env", "TOKEN",
                "--entrypoint", "env", "example/app:latest", "-u", "REMOVED",
                "python", "app.py",
            ],
        )
        self.assertNotIn("super-secret", command)
        self.assertEqual(host_env["TOKEN"], "super-secret")
        self.assertEqual(cwd, "/workspace")

    def test_kubernetes_targets_context_namespace_and_container(self):
        adapter = KubernetesAdapter(
            pod="api-0",
            managed_keys=("MODE", "OLD_FLAG"),
            namespace="staging",
            container="api",
            context="demo",
        )
        command, _, cwd = adapter.prepare(
            ["python", "app.py"],
            {"MODE": "bad", "PATH": "/bin"},
            None,
        )
        self.assertEqual(
            command,
            [
                "kubectl", "--context", "demo", "exec", "--namespace", "staging",
                "api-0", "--container", "api", "--", "env", "-u", "OLD_FLAG",
                "MODE=bad", "python", "app.py",
            ],
        )
        self.assertIsNone(cwd)

    def test_adapter_descriptions_do_not_include_values(self):
        docker = DockerAdapter("app:1", ("TOKEN",))
        kube = KubernetesAdapter("api-0", ("TOKEN",), namespace="prod", container="api")
        self.assertEqual(docker.description, "Docker image app:1")
        self.assertEqual(kube.description, "Kubernetes pod prod/api-0 (container api)")

    def test_cli_reduces_through_docker_adapter(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            good = root / "good.env"
            bad = root / "bad.env"
            good.write_text("A=0\nB=0\nNOISE=good\n", encoding="utf-8")
            bad.write_text("A=1\nB=1\nNOISE=bad\n", encoding="utf-8")

            fake_docker = root / "docker"
            fake_docker.write_text(
                f"#!{sys.executable}\n"
                "import os, subprocess, sys\n"
                "args = sys.argv[1:]\n"
                "i = 2  # skip: run --rm\n"
                "while args[i] == '--env':\n"
                "    i += 2\n"
                "assert args[i:i+2] == ['--entrypoint', 'env']\n"
                "i += 3  # entrypoint, env, image\n"
                "child_env = dict(os.environ)\n"
                "while i < len(args) and args[i] == '-u':\n"
                "    child_env.pop(args[i + 1], None)\n"
                "    i += 2\n"
                "raise SystemExit(subprocess.run(args[i:], env=child_env).returncode)\n",
                encoding="utf-8",
            )
            fake_docker.chmod(0o755)
            code = (
                "import os,sys; "
                "sys.exit(1 if os.getenv('A') == '1' and os.getenv('B') == '1' else 0)"
            )
            process_path = f"{root}{os.pathsep}{os.environ.get('PATH', '')}"
            report = root / "report.json"
            with patch.dict(os.environ, {"PATH": process_path}), redirect_stdout(io.StringIO()):
                exit_code = main([
                    "--good", str(good), "--bad", str(bad),
                    "--docker-image", "example/app:test", "--report-json", str(report), "--",
                    sys.executable, "-c", code,
                ])
            self.assertEqual(exit_code, 0)
            self.assertEqual(json.loads(report.read_text())["execution"]["type"], "docker")

    def test_cli_rejects_remote_junit_for_kubernetes(self):
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            exit_code = main([
                "--good", "good.env", "--bad", "bad.env", "--kube-pod", "api-0",
                "--junit", "results.xml", "--", "python", "app.py",
            ])
        self.assertEqual(exit_code, 2)
        self.assertIn("cannot read a report inside a Kubernetes pod", stderr.getvalue())


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

    def test_verify_repeat_tolerates_a_one_off_baseline_flake(self):
        # The known-good state itself flakes and fails on the very first sample,
        # then behaves correctly on every later sample. A single verification run
        # (the default) is fooled by that one sample; more verification runs are
        # not, and let the reduction proceed to its real, deterministic answer.
        good = {"A": "0", "B": "0"}
        bad = {"A": "1", "B": "1"}
        counter_path = Path(tempfile.mkdtemp()) / "good_state_calls"
        counter_path.write_text("0", encoding="utf-8")
        code = (
            "import os, sys\n"
            "from pathlib import Path\n"
            f"counter_path = Path({str(counter_path)!r})\n"
            "good_state = os.getenv('A') == '0' and os.getenv('B') == '0'\n"
            "bad_state = os.getenv('A') == '1' and os.getenv('B') == '1'\n"
            "n = 0\n"
            "if good_state:\n"
            "    n = int(counter_path.read_text()) + 1\n"
            "    counter_path.write_text(str(n))\n"
            "sys.exit(1 if bad_state or (good_state and n == 1) else 0)\n"
        )
        with self.assertRaises(EnvCauseError) as ctx:
            reduce_environment(good, bad, [sys.executable, "-c", code], cache=False)
        self.assertIn("Known-good", str(ctx.exception))

        counter_path.write_text("0", encoding="utf-8")
        result = reduce_environment(
            good, bad, [sys.executable, "-c", code], verify_repeat=3, cache=False,
        )
        self.assertEqual({c.key for c in result.changes}, {"A", "B"})

    def test_verify_repeat_rejects_values_below_one(self):
        good = {"A": "0"}
        bad = {"A": "1"}
        with self.assertRaises(EnvCauseError):
            reduce_environment(
                good, bad, [sys.executable, "-c", "import sys; sys.exit(0)"],
                verify_repeat=0, cache=False,
            )

    def test_cli_accepts_verify_repeat_flag(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            good = root / "good.env"
            bad = root / "bad.env"
            good.write_text("A=0\n", encoding="utf-8")
            bad.write_text("A=1\n", encoding="utf-8")
            code = "import os,sys; sys.exit(1 if os.getenv('A') == '1' else 0)"
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main([
                    "--good", str(good), "--bad", str(bad),
                    "--verify-repeat", "2", "--", sys.executable, "-c", code,
                ])
            self.assertEqual(exit_code, 0)
            self.assertIn("Verify      : 2 baseline reproductions required", stdout.getvalue())

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

    def test_junit_matcher_does_not_reuse_a_stale_report(self):
        with tempfile.TemporaryDirectory() as td:
            report = Path(td) / "results.xml"
            report.write_text("<testsuite><failure/></testsuite>", encoding="utf-8")
            result = run_command([sys.executable, "-c", "pass"], os.environ, junit=report)
            self.assertFalse(result.matched_failure)
            self.assertIn("could not read JUnit report", result.stderr)

    def test_rejects_non_positive_timeout_and_max_tests(self):
        command = [sys.executable, "-c", "pass"]
        with self.assertRaisesRegex(EnvCauseError, "timeout"):
            reduce_environment({"A": "0"}, {"A": "1"}, command, timeout=0)
        with self.assertRaisesRegex(EnvCauseError, "max-tests"):
            reduce_environment({"A": "0"}, {"A": "1"}, command, max_tests=0)

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
        self.assertEqual(redact_value("/service/api_token", "abc"), "<REDACTED>")
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
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_structured_cli_restores_candidate_after_failed_reduction(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            good = root / "good.json"
            bad = root / "bad.json"
            candidate = root / "candidate.json"
            good.write_text('{"A": 0}', encoding="utf-8")
            bad.write_text('{"A": 1}', encoding="utf-8")
            candidate.write_text("keep me", encoding="utf-8")
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                exit_code = main([
                    "--good", str(good), "--bad", str(bad),
                    "--config-output", str(candidate), "--", sys.executable, "-c", "pass",
                ])
            self.assertEqual(exit_code, 2)
            self.assertEqual(candidate.read_text(encoding="utf-8"), "keep me")

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


class ParallelReductionTests(unittest.TestCase):
    def test_ddmin_tests_a_rounds_chunks_concurrently(self):
        # Four items force a first round with two same-size chunks. If both chunks
        # are not actually dispatched to separate threads, the barrier below never
        # reaches its required party count and times out.
        items = [EnvChange(key=f"K{i}", good="0", bad=str(i)) for i in range(4)]
        barrier = threading.Barrier(2)

        def fails(subset):
            if len(subset) == 2:
                barrier.wait(timeout=2)
            return False

        ddmin(items, fails, max_workers=2)

    def test_parallel_reduction_matches_sequential_result(self):
        good = {f"V{i}": "0" for i in range(6)}
        bad = {f"V{i}": "1" for i in range(6)}
        code = (
            "import os,sys; "
            "sys.exit(1 if os.getenv('V0') == '1' and os.getenv('V3') == '1' else 0)"
        )
        sequential = reduce_environment(good, bad, [sys.executable, "-c", code], cache=False)
        parallel = reduce_environment(
            good, bad, [sys.executable, "-c", code], cache=False, parallel=4,
        )
        self.assertEqual({c.key for c in sequential.changes}, {"V0", "V3"})
        self.assertEqual({c.key for c in parallel.changes}, {"V0", "V3"})

    def test_parallel_rejects_values_below_one(self):
        good = {"A": "0"}
        bad = {"A": "1"}
        with self.assertRaises(EnvCauseError):
            reduce_environment(
                good, bad, [sys.executable, "-c", "import sys; sys.exit(0)"],
                parallel=0, cache=False,
            )

    def test_parallel_rejects_shared_candidate_setup(self):
        good = {"A": "0"}
        bad = {"A": "1"}
        with self.assertRaises(EnvCauseError) as ctx:
            reduce_environment(
                good, bad, [sys.executable, "-c", "import sys; sys.exit(1)"],
                parallel=2, cache=False, candidate_setup=lambda subset: None,
            )
        self.assertIn("--config-output", str(ctx.exception))

    def test_parallel_rejects_junit(self):
        good = {"A": "0"}
        bad = {"A": "1"}
        with self.assertRaises(EnvCauseError) as ctx:
            reduce_environment(
                good, bad, [sys.executable, "-c", "import sys; sys.exit(1)"],
                parallel=2, cache=False, junit="report.xml",
            )
        self.assertIn("--junit", str(ctx.exception))

    def test_parallel_rejects_non_parallel_safe_adapter(self):
        good = {"A": "0"}
        bad = {"A": "1"}
        adapter = KubernetesAdapter(pod="api-0", managed_keys=("A",))
        with self.assertRaises(EnvCauseError) as ctx:
            reduce_environment(
                good, bad, [sys.executable, "-c", "import sys; sys.exit(1)"],
                parallel=2, cache=False, adapter=adapter,
            )
        self.assertIn("Kubernetes pod", str(ctx.exception))

    def test_cli_accepts_parallel_flag(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            good = root / "good.env"
            bad = root / "bad.env"
            good.write_text("A=0\n", encoding="utf-8")
            bad.write_text("A=1\n", encoding="utf-8")
            code = "import os,sys; sys.exit(1 if os.getenv('A') == '1' else 0)"
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main([
                    "--good", str(good), "--bad", str(bad),
                    "--parallel", "2", "--", sys.executable, "-c", code,
                ])
            self.assertEqual(exit_code, 0)
            self.assertIn("Parallel    : up to 2 concurrent candidates", stdout.getvalue())

    def test_cli_rejects_parallel_with_kube_pod(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            good = root / "good.env"
            bad = root / "bad.env"
            good.write_text("A=0\n", encoding="utf-8")
            bad.write_text("A=1\n", encoding="utf-8")
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                exit_code = main([
                    "--good", str(good), "--bad", str(bad), "--kube-pod", "api-0",
                    "--parallel", "2", "--", "python", "app.py",
                ])
        self.assertEqual(exit_code, 2)
        self.assertIn("--parallel is not supported", stderr.getvalue())


class GitHubActionTests(unittest.TestCase):
    def test_action_builds_docker_adapter_arguments(self):
        with tempfile.TemporaryDirectory() as td:
            environment = {
                "GITHUB_WORKSPACE": td,
                "INPUT_GOOD": "good.env",
                "INPUT_BAD": "bad.env",
                "INPUT_COMMAND": "python /app/test.py",
                "INPUT_DOCKER_IMAGE": "example/app:1",
                "INPUT_DOCKER_RUN_ARGS": "--network=host -v './src:/app:ro'",
                "INPUT_REPORT_JSON": "report.json",
                "INPUT_PROGRESS": "false",
            }
            with patch.dict(os.environ, environment, clear=True):
                argv, _, _ = build_github_action_argv()
            self.assertIn("--docker-image", argv)
            self.assertIn("example/app:1", argv)
            self.assertIn("--docker-run-arg=--network=host", argv)
            self.assertIn("--docker-run-arg=-v", argv)
            self.assertIn("--docker-run-arg=./src:/app:ro", argv)

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
