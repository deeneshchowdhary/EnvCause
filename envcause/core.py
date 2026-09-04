from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shlex
import subprocess
import tempfile
import threading
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Mapping, Protocol, Sequence


_UNSET = object()
_SECRET_RE = re.compile(r"(^|_)(secret|token|password|passwd|pwd|credential|cookie|api_key|private_key|access_key|client_secret)($|_)", re.I)
_CACHE_IGNORED_ENV = {
    # GitHub creates new command-file paths and counters for each step/run.
    # They do not describe the program-under-test environment and would make
    # an actions/cache-restored EnvCause cache miss every time.
    "ACTIONS_RUNTIME_TOKEN",
    "ACTIONS_RUNTIME_URL",
    "ACTIONS_RESULTS_URL",
    "GITHUB_ACTION",
    "GITHUB_ENV",
    "GITHUB_OUTPUT",
    "GITHUB_PATH",
    "GITHUB_RUN_ATTEMPT",
    "GITHUB_RUN_ID",
    "GITHUB_RUN_NUMBER",
    "GITHUB_STATE",
    "GITHUB_STEP_SUMMARY",
    "RUNNER_TEMP",
    "RUNNER_TRACKING_ID",
}


class EnvCauseError(RuntimeError):
    """Raised for invalid inputs or an unusable reproduction command."""


class ExecutionAdapter(Protocol):
    """Prepare a candidate command for an alternate execution environment."""

    @property
    def cache_identity(self) -> Mapping[str, object]: ...

    @property
    def parallel_safe(self) -> bool: ...

    def prepare(
        self,
        command: Sequence[str],
        env: Mapping[str, str],
        cwd: str | None,
    ) -> tuple[list[str], dict[str, str], str | None]: ...


@dataclass(frozen=True)
class EnvChange:
    key: str
    good: object
    bad: object

    @property
    def good_is_unset(self) -> bool:
        return self.good is _UNSET

    @property
    def bad_is_unset(self) -> bool:
        return self.bad is _UNSET


@dataclass
class RunResult:
    returncode: int
    stdout: str
    stderr: str
    duration_s: float
    matched_failure: bool

    @property
    def combined_output(self) -> str:
        if self.stdout and self.stderr:
            return self.stdout + "\n" + self.stderr
        return self.stdout or self.stderr


@dataclass
class ReductionResult:
    changes: list[EnvChange]
    total_runs: int
    good_result: RunResult
    bad_result: RunResult
    cache_hits: int = 0


def parse_dotenv(path: str | os.PathLike[str]) -> dict[str, str]:
    """Parse a pragmatic subset of .env syntax without external dependencies.

    Supported:
      KEY=value
      export KEY=value
      quoted values with basic backslash escapes in double quotes
      inline comments for unquoted values when preceded by whitespace
      blank lines and comments

    Variable interpolation is intentionally not performed; the command receives
    literal values from the file.
    """
    file_path = Path(path)
    if not file_path.exists():
        raise EnvCauseError(f"Environment file not found: {file_path}")

    result: dict[str, str] = {}
    for line_no, raw in enumerate(file_path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise EnvCauseError(f"{file_path}:{line_no}: expected KEY=VALUE")

        key, value = line.split("=", 1)
        key = key.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise EnvCauseError(f"{file_path}:{line_no}: invalid variable name {key!r}")

        value = value.strip()
        if value.startswith('"'):
            value = _parse_double_quoted(value, file_path, line_no)
        elif value.startswith("'"):
            value = _parse_single_quoted(value, file_path, line_no)
        else:
            # Treat `value # comment` as value, but preserve hashes without leading space.
            match = re.search(r"\s+#", value)
            if match:
                value = value[: match.start()].rstrip()
        result[key] = value
    return result


def _parse_double_quoted(value: str, path: Path, line_no: int) -> str:
    if len(value) < 2 or not value.endswith('"'):
        raise EnvCauseError(f"{path}:{line_no}: unterminated double-quoted value")
    inner = value[1:-1]
    escapes = {"n": "\n", "r": "\r", "t": "\t", '"': '"', "\\": "\\"}
    parsed: list[str] = []
    index = 0
    while index < len(inner):
        character = inner[index]
        if character != "\\":
            parsed.append(character)
            index += 1
            continue
        if index + 1 >= len(inner):
            raise EnvCauseError(f"{path}:{line_no}: trailing backslash in quoted value")
        escaped = inner[index + 1]
        # Preserve unknown escapes literally. This matches common dotenv parsers
        # and, importantly, does not reinterpret already-decoded UTF-8 text.
        parsed.append(escapes.get(escaped, "\\" + escaped))
        index += 2
    return "".join(parsed)


def _parse_single_quoted(value: str, path: Path, line_no: int) -> str:
    if len(value) < 2 or not value.endswith("'"):
        raise EnvCauseError(f"{path}:{line_no}: unterminated single-quoted value")
    return value[1:-1]


def diff_envs(good: Mapping[str, str], bad: Mapping[str, str]) -> list[EnvChange]:
    keys = sorted(set(good) | set(bad))
    changes: list[EnvChange] = []
    for key in keys:
        good_value: object = good.get(key, _UNSET)
        bad_value: object = bad.get(key, _UNSET)
        if good_value != bad_value:
            changes.append(EnvChange(key=key, good=good_value, bad=bad_value))
    return changes


def build_environment(
    process_env: Mapping[str, str],
    good: Mapping[str, str],
    changes: Sequence[EnvChange],
    bad_keys: Iterable[str],
) -> dict[str, str]:
    """Build an execution environment from the good baseline plus chosen bad states."""
    env = dict(process_env)

    # Make the provided good config authoritative for every key it mentions.
    env.update(good)

    bad_key_set = set(bad_keys)
    for change in changes:
        if change.key in bad_key_set:
            if change.bad is _UNSET:
                env.pop(change.key, None)
            else:
                env[change.key] = str(change.bad)
        else:
            if change.good is _UNSET:
                env.pop(change.key, None)
            else:
                env[change.key] = str(change.good)
    return env


def run_command(
    command: Sequence[str],
    env: Mapping[str, str],
    *,
    contains: str | None = None,
    matches: str | None = None,
    junit: str | os.PathLike[str] | None = None,
    timeout: float | None = None,
    cwd: str | None = None,
    adapter: ExecutionAdapter | None = None,
) -> RunResult:
    started = time.monotonic()
    actual_command = list(command)
    actual_env = dict(env)
    actual_cwd = cwd
    if adapter is not None:
        actual_command, actual_env, actual_cwd = adapter.prepare(command, env, cwd)
    report_path: Path | None = None
    if junit is not None:
        report_path = Path(cwd, junit) if cwd and not Path(junit).is_absolute() else Path(junit)
        try:
            report_path.unlink(missing_ok=True)
        except OSError as exc:
            raise EnvCauseError(f"Could not remove stale JUnit report {report_path}: {exc}") from exc
    try:
        completed = subprocess.run(
            actual_command,
            env=actual_env,
            cwd=actual_cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
        if report_path is not None:
            try:
                root = ET.parse(report_path).getroot()
                matched_failure = any(
                    element.tag.rsplit("}", 1)[-1] in {"failure", "error"}
                    for element in root.iter()
                )
            except (OSError, ET.ParseError) as exc:
                matched_failure = False
                stderr += ("\n" if stderr else "") + f"envcause: could not read JUnit report {report_path}: {exc}"
        elif matches is not None:
            matched_failure = re.search(matches, stdout + "\n" + stderr) is not None
        elif contains is None:
            matched_failure = completed.returncode != 0
        else:
            matched_failure = contains in (stdout + "\n" + stderr)
        return RunResult(
            returncode=completed.returncode,
            stdout=stdout,
            stderr=stderr,
            duration_s=time.monotonic() - started,
            matched_failure=matched_failure,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr = exc.stderr.decode() if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        # A timeout is considered a failure when using exit-code mode. With
        # --contains, only the requested text should count as the target failure.
        if matches is not None:
            matched_failure = re.search(matches, stdout + "\n" + stderr) is not None
        elif contains is not None:
            matched_failure = contains in (stdout + "\n" + stderr)
        else:
            matched_failure = junit is None
        return RunResult(
            returncode=124,
            stdout=stdout,
            stderr=stderr + ("\n" if stderr else "") + "envcause: command timed out",
            duration_s=time.monotonic() - started,
            matched_failure=matched_failure,
        )
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise EnvCauseError(f"Could not execute {actual_command[0]!r}: {exc}") from exc


def reproduce(
    command: Sequence[str],
    env: Mapping[str, str],
    *,
    contains: str | None,
    matches: str | None = None,
    junit: str | os.PathLike[str] | None = None,
    timeout: float | None,
    cwd: str | None,
    repeat: int,
    adapter: ExecutionAdapter | None = None,
    before_run: Callable[[], None] | None = None,
) -> tuple[bool, RunResult, int]:
    """Return true only if the target failure reproduces on every repeat."""
    if repeat < 1:
        raise EnvCauseError("--repeat must be at least 1")
    last: RunResult | None = None
    for run_no in range(1, repeat + 1):
        if before_run is not None:
            before_run()
        last = run_command(
            command,
            env,
            contains=contains,
            matches=matches,
            junit=junit,
            timeout=timeout,
            cwd=cwd,
            adapter=adapter,
        )
        if not last.matched_failure:
            return False, last, run_no
    assert last is not None
    return True, last, repeat


def ddmin(
    items: list[EnvChange],
    fails,
    *,
    max_tests: int | None = None,
    max_workers: int = 1,
) -> tuple[list[EnvChange], int]:
    """Classic delta debugging reduction.

    `fails(candidate)` must return True when candidate reproduces the target failure.
    The result is 1-minimal: removing any single remaining change no longer reproduces.

    With `max_workers` above 1, every chunk (or complement) within a round is tested
    concurrently instead of stopping at the first match; the first match in the
    original order still wins, so the result is identical to the sequential run.
    """
    if not items:
        return [], 0

    candidate = list(items)
    n = 2
    tests = 0
    test_lock = threading.Lock()

    def check(subset: list[EnvChange]) -> bool:
        nonlocal tests
        with test_lock:
            if max_tests is not None and tests >= max_tests:
                raise EnvCauseError(f"Maximum reduction tests reached ({max_tests})")
            tests += 1
        return bool(fails(subset))

    def first_match(subsets: list[list[EnvChange]]) -> list[EnvChange] | None:
        if max_workers <= 1 or len(subsets) <= 1:
            for subset in subsets:
                if check(subset):
                    return subset
            return None
        with ThreadPoolExecutor(max_workers=min(max_workers, len(subsets))) as executor:
            results = list(executor.map(check, subsets))
        for subset, result in zip(subsets, results):
            if result:
                return subset
        return None

    while len(candidate) >= 2:
        chunk_size = math.ceil(len(candidate) / n)
        chunks = [candidate[i : i + chunk_size] for i in range(0, len(candidate), chunk_size)]

        # First see whether one chunk alone is sufficient.
        match = first_match(chunks)
        if match is not None:
            candidate = match
            n = max(n - 1, 2)
            continue

        # Then see whether removing a chunk preserves the failure.
        complements = []
        for chunk in chunks:
            chunk_ids = {id(x) for x in chunk}
            complement = [x for x in candidate if id(x) not in chunk_ids]
            if complement:
                complements.append(complement)
        match = first_match(complements)
        if match is not None:
            candidate = match
            n = max(n - 1, 2)
            continue

        if n >= len(candidate):
            break
        n = min(len(candidate), n * 2)

    # ddmin should be 1-minimal already, but this final pass makes the guarantee
    # explicit and easier to reason about for small configuration sets. Each check
    # depends on the candidate left by the previous one, so it stays sequential
    # even when max_workers is above 1.
    i = 0
    while i < len(candidate):
        complement = candidate[:i] + candidate[i + 1 :]
        if complement and check(complement):
            candidate = complement
        else:
            i += 1

    return candidate, tests


def reduce_environment(
    good: Mapping[str, str],
    bad: Mapping[str, str],
    command: Sequence[str],
    *,
    contains: str | None = None,
    matches: str | None = None,
    junit: str | os.PathLike[str] | None = None,
    timeout: float | None = None,
    cwd: str | None = None,
    repeat: int = 1,
    verify_repeat: int | None = None,
    max_tests: int | None = None,
    parallel: int = 1,
    process_env: Mapping[str, str] | None = None,
    cache: bool = True,
    cache_path: str | os.PathLike[str] | None = None,
    progress: Callable[[int, int, int, bool], None] | None = None,
    adapter: ExecutionAdapter | None = None,
    changes_override: Sequence[EnvChange] | None = None,
    candidate_setup: Callable[[Sequence[EnvChange]], None] | None = None,
    inject_environment: bool = True,
    cache_identity: Mapping[str, object] | None = None,
) -> ReductionResult:
    if not command:
        raise EnvCauseError("No reproduction command supplied")
    if sum(value is not None for value in (contains, matches, junit)) > 1:
        raise EnvCauseError("Only one failure matcher may be used")
    if matches is not None:
        try:
            re.compile(matches)
        except re.error as exc:
            raise EnvCauseError(f"Invalid failure regex: {exc}") from exc

    if timeout is not None and timeout <= 0:
        raise EnvCauseError("--timeout must be greater than 0")
    if max_tests is not None and max_tests < 1:
        raise EnvCauseError("--max-tests must be at least 1")

    if verify_repeat is not None and verify_repeat < 1:
        raise EnvCauseError("--verify-repeat must be at least 1")
    baseline_repeat = repeat if verify_repeat is None else verify_repeat

    if parallel < 1:
        raise EnvCauseError("--parallel must be at least 1")
    if parallel > 1:
        if candidate_setup is not None:
            raise EnvCauseError(
                "--parallel cannot be combined with a shared candidate configuration file "
                "(JSON/YAML/TOML reduction writes each candidate to --config-output)"
            )
        if junit is not None:
            raise EnvCauseError(
                "--parallel cannot be combined with --junit because concurrent runs would share the report path"
            )
        if adapter is not None and not adapter.parallel_safe:
            raise EnvCauseError(
                f"--parallel is not supported with {adapter.description} because candidates share the same execution target"
            )

    process_env = process_env or os.environ
    changes = list(changes_override) if changes_override is not None else diff_envs(good, bad)
    if not changes:
        raise EnvCauseError("Good and bad configuration files contain no differences")

    def env_for(subset: Sequence[EnvChange]) -> dict[str, str]:
        if not inject_environment:
            return dict(process_env)
        return build_environment(process_env, good, changes, (c.key for c in subset))

    def setup_for(subset: Sequence[EnvChange]) -> Callable[[], None] | None:
        if candidate_setup is None:
            return None
        return lambda: candidate_setup(subset)

    good_failed, good_result, good_runs = reproduce(
        command,
        env_for([]),
        contains=contains,
        matches=matches,
        junit=junit,
        timeout=timeout,
        cwd=cwd,
        repeat=baseline_repeat,
        adapter=adapter,
        before_run=setup_for([]),
    )
    if good_failed:
        target = _failure_description(contains, matches, junit)
        raise EnvCauseError(f"Known-good configuration already reproduces {target}")

    bad_failed, bad_result, bad_runs = reproduce(
        command,
        env_for(changes),
        contains=contains,
        matches=matches,
        junit=junit,
        timeout=timeout,
        cwd=cwd,
        repeat=baseline_repeat,
        adapter=adapter,
        before_run=setup_for(changes),
    )
    if not bad_failed:
        target = _failure_description(contains, matches, junit)
        raise EnvCauseError(f"Known-bad configuration does not reproduce {target}")

    command_runs = good_runs + bad_runs
    cache_hits = 0
    candidate_cache: dict[frozenset[str], bool] = {}
    persistent_cache = _load_cache(cache_path) if cache and cache_path else {}
    logical_tests = 0
    state_lock = threading.Lock()

    def fails(subset: Sequence[EnvChange]) -> bool:
        nonlocal command_runs, cache_hits, logical_tests
        cache_key = frozenset(change.key for change in subset)
        persistent_key = _candidate_hash(
            command,
            env_for(subset),
            contains=contains,
            matches=matches,
            junit=junit,
            timeout=timeout,
            cwd=cwd,
            repeat=repeat,
            adapter_identity=adapter.cache_identity if adapter else None,
            extra_identity={**(cache_identity or {}), "changes": sorted(change.key for change in subset)},
        )

        with state_lock:
            logical_tests += 1
            test_no = logical_tests
            if cache and cache_key in candidate_cache:
                cache_hits += 1
                result = candidate_cache[cache_key]
                if progress:
                    progress(test_no, command_runs, len(subset), True)
                return result
            if cache and persistent_key in persistent_cache:
                cache_hits += 1
                result = persistent_cache[persistent_key]
                candidate_cache[cache_key] = result
                if progress:
                    progress(test_no, command_runs, len(subset), True)
                return result

        failed, _, used = reproduce(
            command,
            env_for(subset),
            contains=contains,
            matches=matches,
            junit=junit,
            timeout=timeout,
            cwd=cwd,
            repeat=repeat,
            adapter=adapter,
            before_run=setup_for(subset),
        )

        with state_lock:
            command_runs += used
            if cache:
                candidate_cache[cache_key] = failed
                persistent_cache[persistent_key] = failed
            if progress:
                progress(test_no, command_runs, len(subset), False)
        return failed

    reduced, _logical_tests = ddmin(changes, fails, max_tests=max_tests, max_workers=parallel)
    if cache and cache_path:
        _write_cache(cache_path, persistent_cache)
    return ReductionResult(
        changes=reduced,
        total_runs=command_runs,
        good_result=good_result,
        bad_result=bad_result,
        cache_hits=cache_hits,
    )


def _failure_description(
    contains: str | None,
    matches: str | None,
    junit: str | os.PathLike[str] | None,
) -> str:
    if contains is not None:
        return f"output containing {contains!r}"
    if matches is not None:
        return f"output matching {matches!r}"
    if junit is not None:
        return f"a failing JUnit report at {junit}"
    return "a non-zero exit code"


def _candidate_hash(
    command: Sequence[str],
    env: Mapping[str, str],
    *,
    contains: str | None,
    matches: str | None,
    junit: str | os.PathLike[str] | None,
    timeout: float | None,
    cwd: str | None,
    repeat: int,
    adapter_identity: Mapping[str, object] | None,
    extra_identity: Mapping[str, object] | None = None,
) -> str:
    payload = {
        "command": list(command),
        "env": sorted((key, value) for key, value in env.items() if key not in _CACHE_IGNORED_ENV),
        "contains": contains,
        "matches": matches,
        "junit": str(junit) if junit is not None else None,
        "timeout": timeout,
        "cwd": cwd,
        "repeat": repeat,
        "adapter": adapter_identity,
        "extra": extra_identity,
    }
    serialized = json.dumps(payload, separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _load_cache(path: str | os.PathLike[str]) -> dict[str, bool]:
    cache_path = Path(path)
    if not cache_path.exists():
        return {}
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
        entries = data["entries"]
        if data.get("schema_version") != 1 or not isinstance(entries, dict):
            raise ValueError("unsupported cache format")
        return {key: value for key, value in entries.items() if isinstance(value, bool)}
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise EnvCauseError(f"Could not read cache {cache_path}: {exc}") from exc


def _write_cache(path: str | os.PathLike[str], entries: Mapping[str, bool]) -> None:
    cache_path = Path(path)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = cache_path.with_name(cache_path.name + ".tmp")
    payload = {"schema_version": 1, "entries": dict(entries)}
    temp_path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    temp_path.replace(cache_path)


def redact_value(key: str, value: object, *, show_values: bool = False) -> str:
    if value is _UNSET:
        return "<UNSET>"
    value_s = str(value)
    if show_values:
        return value_s
    if _SECRET_RE.search(key.replace("/", "_").replace(".", "_")):
        return "<REDACTED>"
    if len(value_s) > 80:
        return value_s[:77] + "..."
    return value_s


def shell_assignment(change: EnvChange, *, show_values: bool = False) -> str:
    if change.bad is _UNSET:
        return f"unset {change.key}"
    value = redact_value(change.key, change.bad, show_values=show_values)
    if value == "<REDACTED>":
        return f"{change.key}=<REDACTED>"
    return f"{change.key}={shlex.quote(value)}"


def write_repro(path: str | os.PathLike[str], changes: Sequence[EnvChange]) -> None:
    lines = ["# Minimal failure-inducing configuration generated by envcause"]
    for change in changes:
        if change.bad is _UNSET:
            lines.append(f"# UNSET {change.key}")
        else:
            value = str(change.bad)
            # Quote values when doing so avoids ambiguity.
            if not value or re.search(r"\s|#|['\"]", value):
                escaped = value.replace("\\", "\\\\").replace('"', '\\"')
                lines.append(f'{change.key}="{escaped}"')
            else:
                lines.append(f"{change.key}={value}")
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{output.name}.", suffix=".tmp", dir=output.parent
        )
        temporary = Path(temporary_name)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
        temporary.replace(output)
    except OSError as exc:
        try:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise EnvCauseError(f"Could not write reproduction config {output}: {exc}") from exc
