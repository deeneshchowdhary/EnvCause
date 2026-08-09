from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .adapters import DockerAdapter, KubernetesAdapter
from .core import EnvCauseError, diff_envs, parse_dotenv, redact_value, reduce_environment, shell_assignment, write_repro


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="envcause",
        description="Find the smallest configuration change set that reproduces a failure.",
        epilog="Example: envcause --good .env.local --bad .env.staging -- pytest -q",
    )
    parser.add_argument("--good", required=True, help="Known-good .env file")
    parser.add_argument("--bad", required=True, help="Known-bad .env file")
    execution = parser.add_mutually_exclusive_group()
    execution.add_argument(
        "--docker-image",
        metavar="IMAGE",
        help="Run each candidate in a fresh Docker container from IMAGE",
    )
    execution.add_argument(
        "--kube-pod",
        metavar="POD",
        help="Run each candidate in an existing Kubernetes pod",
    )
    parser.add_argument(
        "--docker-run-arg",
        action="append",
        default=[],
        metavar="ARG",
        help="Additional docker run argument; repeat as needed (use = before values starting with --)",
    )
    parser.add_argument("--kube-namespace", metavar="NAME", help="Kubernetes namespace")
    parser.add_argument("--kube-container", metavar="NAME", help="Container within the Kubernetes pod")
    parser.add_argument("--kube-context", metavar="NAME", help="kubectl context")
    matchers = parser.add_mutually_exclusive_group()
    matchers.add_argument(
        "--contains",
        help="Treat the run as failing only when stdout/stderr contains this text",
    )
    matchers.add_argument(
        "--matches",
        metavar="REGEX",
        help="Treat the run as failing only when stdout/stderr matches this regex",
    )
    matchers.add_argument(
        "--junit",
        metavar="PATH",
        help="Treat the run as failing when this JUnit XML report contains a failure or error",
    )
    parser.add_argument("--repeat", type=int, default=1, help="Require failure to reproduce N times (default: 1)")
    parser.add_argument("--timeout", type=float, help="Per-run timeout in seconds")
    parser.add_argument("--cwd", help="Working directory for the reproduction command")
    parser.add_argument("--max-tests", type=int, help="Maximum candidate subsets tested during reduction")
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="Disable in-memory candidate result caching",
    )
    parser.add_argument(
        "--cache-file",
        metavar="PATH",
        help="Reuse candidate results across invocations using this cache file",
    )
    parser.add_argument(
        "--progress",
        action="store_true",
        help="Show each candidate test as the reduction runs",
    )
    parser.add_argument("--write-repro", metavar="PATH", help="Write the reduced bad configuration to a .env file")
    parser.add_argument("--report-json", metavar="PATH", help="Write a machine-readable reduction report")
    parser.add_argument(
        "--show-values",
        action="store_true",
        help="Show values in the report (secret-looking keys are redacted by default)",
    )
    parser.add_argument(
        "command",
        nargs=argparse.REMAINDER,
        help="Command to run; place it after --, e.g. -- pytest tests/test_login.py",
    )
    return parser


def _strip_separator(command: list[str]) -> list[str]:
    return command[1:] if command and command[0] == "--" else command


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    command = _strip_separator(args.command)
    if args.no_cache and args.cache_file:
        print("envcause: error: --no-cache cannot be combined with --cache-file", file=sys.stderr)
        return 2
    if args.docker_run_arg and not args.docker_image:
        print("envcause: error: --docker-run-arg requires --docker-image", file=sys.stderr)
        return 2
    if any((args.kube_namespace, args.kube_container, args.kube_context)) and not args.kube_pod:
        print("envcause: error: Kubernetes options require --kube-pod", file=sys.stderr)
        return 2
    if args.junit and args.kube_pod:
        print(
            "envcause: error: --junit cannot read a report inside a Kubernetes pod; "
            "use exit-code, --contains, or --matches mode",
            file=sys.stderr,
        )
        return 2
    if not command:
        print("envcause: error: provide a reproduction command after --", file=sys.stderr)
        return 2

    try:
        good = parse_dotenv(args.good)
        bad = parse_dotenv(args.bad)
        all_changes = diff_envs(good, bad)
        managed_keys = tuple(sorted(set(good) | set(bad)))
        adapter = None
        if args.docker_image:
            adapter = DockerAdapter(
                image=args.docker_image,
                managed_keys=managed_keys,
                run_args=tuple(args.docker_run_arg),
            )
        elif args.kube_pod:
            adapter = KubernetesAdapter(
                pod=args.kube_pod,
                managed_keys=managed_keys,
                namespace=args.kube_namespace,
                container=args.kube_container,
                context=args.kube_context,
            )

        print("EnvCause")
        print("=" * 72)
        print(f"Good config : {Path(args.good)}")
        print(f"Bad config  : {Path(args.bad)}")
        print(f"Differences : {len(all_changes)}")
        print(f"Command     : {' '.join(command)}")
        print(f"Execution   : {adapter.description if adapter else 'local process'}")
        if args.contains:
            print(f"Failure     : output contains {args.contains!r}")
        elif args.matches:
            print(f"Failure     : output matches {args.matches!r}")
        elif args.junit:
            print(f"Failure     : JUnit report {args.junit} contains a failure or error")
        else:
            print("Failure     : non-zero exit code")
        if args.repeat > 1:
            print(f"Repeat      : {args.repeat} consecutive reproductions required")
        print()
        print("Verifying known-good and known-bad configs, then reducing...")

        def show_progress(test_no: int, command_runs: int, candidate_size: int, cached: bool) -> None:
            source = "cached" if cached else f"{command_runs} command runs"
            print(
                f"[candidate {test_no}] {candidate_size} changed variables ({source})",
                file=sys.stderr,
            )

        result = reduce_environment(
            good,
            bad,
            command,
            contains=args.contains,
            matches=args.matches,
            junit=args.junit,
            timeout=args.timeout,
            cwd=args.cwd,
            repeat=args.repeat,
            max_tests=args.max_tests,
            cache=not args.no_cache,
            cache_path=args.cache_file,
            progress=show_progress if args.progress else None,
            adapter=adapter,
        )

        print()
        print("Result")
        print("-" * 72)
        print(f"Original differing variables : {len(all_changes)}")
        print(f"Failure-inducing variables    : {len(result.changes)}")
        print(f"Command executions            : {result.total_runs}")
        if result.cache_hits:
            print(f"Cached candidate results      : {result.cache_hits}")
        print()
        print("1-minimal failure-inducing change set:")
        for change in result.changes:
            good_value = redact_value(change.key, change.good, show_values=args.show_values)
            bad_value = redact_value(change.key, change.bad, show_values=args.show_values)
            print(f"  {change.key}: {good_value} -> {bad_value}")

        print()
        print("Bad-state reproduction:")
        for change in result.changes:
            print(f"  {shell_assignment(change, show_values=args.show_values)}")

        if args.write_repro:
            write_repro(args.write_repro, result.changes)
            print()
            print(f"Wrote reproduction config: {args.write_repro}")
            if not args.show_values:
                print("Note: the file contains real values even though terminal output may be redacted.")

        if args.report_json:
            report = {
                "schema_version": 1,
                "good_config": str(Path(args.good)),
                "bad_config": str(Path(args.bad)),
                "command": command,
                "execution": dict(adapter.cache_identity) if adapter else {"type": "local"},
                "failure_match": (
                    {"contains": args.contains}
                    if args.contains
                    else {"matches": args.matches}
                    if args.matches
                    else {"junit": args.junit}
                    if args.junit
                    else {"nonzero_exit": True}
                ),
                "original_difference_count": len(all_changes),
                "failure_inducing_count": len(result.changes),
                "command_executions": result.total_runs,
                "cache_hits": result.cache_hits,
                "changes": [
                    {
                        "key": change.key,
                        "good": redact_value(change.key, change.good, show_values=args.show_values),
                        "bad": redact_value(change.key, change.bad, show_values=args.show_values),
                    }
                    for change in result.changes
                ],
            }
            Path(args.report_json).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
            print()
            print(f"Wrote JSON report: {args.report_json}")
        return 0

    except EnvCauseError as exc:
        print(f"envcause: {exc}", file=sys.stderr)
        return 2
    except FileNotFoundError as exc:
        print(f"envcause: command not found: {exc.filename}", file=sys.stderr)
        return 127


if __name__ == "__main__":
    raise SystemExit(main())
