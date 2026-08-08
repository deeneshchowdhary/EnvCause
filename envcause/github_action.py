"""GitHub composite-action adapter for EnvCause."""

from __future__ import annotations

import json
import os
import shlex
import sys
from pathlib import Path

from .cli import main


def _input(name: str) -> str:
    return os.environ.get(f"INPUT_{name}", "").strip()


def _enabled(name: str, *, default: bool = False) -> bool:
    value = _input(name)
    if not value:
        return default
    if value.lower() in {"true", "1", "yes"}:
        return True
    if value.lower() in {"false", "0", "no"}:
        return False
    raise ValueError(f"{name.lower().replace('_', '-')} must be true or false")


def _absolute(path: str) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = Path(os.environ.get("GITHUB_WORKSPACE", Path.cwd())) / candidate
    return candidate.resolve()


def _append_output(name: str, value: object) -> None:
    output_path = os.environ.get("GITHUB_OUTPUT")
    if output_path:
        with Path(output_path).open("a", encoding="utf-8") as output:
            output.write(f"{name}={value}\n")


def _write_summary(report: dict[str, object]) -> None:
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return
    changes = report.get("changes", [])
    lines = [
        "## EnvCause result",
        "",
        f"Found **{report['failure_inducing_count']}** failure-inducing changes "
        f"from **{report['original_difference_count']}** differences after "
        f"**{report['command_executions']}** command executions.",
        "",
        "| Variable | Good state | Bad state |",
        "| --- | --- | --- |",
    ]
    for change in changes if isinstance(changes, list) else []:
        if isinstance(change, dict):
            cells = [str(change.get(key, "")).replace("|", "\\|") for key in ("key", "good", "bad")]
            lines.append(f"| `{'` | `'.join(cells)}` |")
    with Path(summary_path).open("a", encoding="utf-8") as summary:
        summary.write("\n".join(lines) + "\n")


def build_argv() -> tuple[list[str], Path, Path | None]:
    good = _input("GOOD")
    bad = _input("BAD")
    command = _input("COMMAND")
    if not good or not bad or not command:
        raise ValueError("good, bad, and command inputs are required")

    report = _absolute(_input("REPORT_JSON") or "envcause-report.json")
    report.parent.mkdir(parents=True, exist_ok=True)
    argv = ["--good", good, "--bad", bad, "--report-json", str(report)]

    optional_values = {
        "--contains": _input("CONTAINS"),
        "--matches": _input("MATCHES"),
        "--junit": _input("JUNIT"),
        "--repeat": _input("REPEAT"),
        "--timeout": _input("TIMEOUT"),
        "--cwd": _input("CWD"),
        "--max-tests": _input("MAX_TESTS"),
        "--cache-file": _input("CACHE_FILE"),
    }
    for flag, value in optional_values.items():
        if value:
            if flag == "--cache-file":
                cache_path = _absolute(value)
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                value = str(cache_path)
            argv.extend([flag, value])

    repro: Path | None = None
    if _input("WRITE_REPRO"):
        repro = _absolute(_input("WRITE_REPRO"))
        repro.parent.mkdir(parents=True, exist_ok=True)
        argv.extend(["--write-repro", str(repro)])
    if _enabled("PROGRESS", default=True):
        argv.append("--progress")
    if _enabled("SHOW_VALUES"):
        argv.append("--show-values")

    argv.append("--")
    argv.extend(shlex.split(command))
    return argv, report, repro


def run() -> int:
    try:
        argv, report_path, repro_path = build_argv()
        exit_code = main(argv)
        if exit_code != 0:
            return exit_code

        report = json.loads(report_path.read_text(encoding="utf-8"))
        _append_output("report-path", report_path)
        _append_output("repro-path", repro_path or "")
        _append_output("failure-inducing-count", report["failure_inducing_count"])
        _append_output("command-executions", report["command_executions"])
        _append_output("cache-hits", report["cache_hits"])
        if _enabled("STEP_SUMMARY", default=True):
            _write_summary(report)
        return 0
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"envcause action: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(run())
