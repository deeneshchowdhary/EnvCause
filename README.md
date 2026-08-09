# EnvCause

**Git bisect finds the bad commit. EnvCause finds the bad configuration.**

[![CI](https://github.com/deeneshchowdhary/EnvCause/actions/workflows/ci.yml/badge.svg)](https://github.com/deeneshchowdhary/EnvCause/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/envcause.svg)](https://pypi.org/project/envcause/)
[![Python](https://img.shields.io/pypi/pyversions/envcause.svg)](https://pypi.org/project/envcause/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

EnvCause compares a known-good `.env` file with a known-bad one, repeatedly runs your reproduction command, and uses delta debugging to reduce all changed variables to a **1-minimal failure-inducing set**.

It is deliberately local and dependency-free: your environment values are not sent anywhere.

![EnvCause finds two failure-inducing settings among many configuration changes](https://raw.githubusercontent.com/deeneshchowdhary/EnvCause/master/assets/envcause-demo.gif)

## Install

```bash
pipx install envcause
```

Alternatively, install it inside a virtual environment with `python -m pip install envcause`.

## Example

```bash
envcause \
  --good examples/good.env \
  --bad examples/bad.env \
  -- python examples/demo_app.py
```

Example output:

```text
Original differing variables : 8
Failure-inducing variables    : 2

1-minimal failure-inducing change set:
  FEATURE_NEW_AUTH: false -> true
  JWT_ALGORITHM: HS256 -> RS256
```

## Why this is useful

Configuration failures often come from many changes landing together: feature flags, URLs, credentials, timeouts, pool sizes, provider choices, or deployment-specific switches. Testing them manually is slow, and checking one variable at a time misses failures caused by combinations.

EnvCause searches combinations automatically.

## Usage

```text
envcause --good GOOD.env --bad BAD.env [options] -- COMMAND [ARGS...]
```

By default, a non-zero process exit code means the failure reproduced.

### Match a specific error instead

```bash
envcause \
  --good .env.local \
  --bad .env.staging \
  --contains "Connection refused" \
  -- npm test
```

This is useful when the command can fail for unrelated reasons.

For patterns that vary between runs, use a Python regular expression:

```bash
envcause --good good.env --bad bad.env --matches 'HTTP (500|503)' -- pytest -q
```

`--contains` and `--matches` search the combined stdout and stderr.

### Match failures from JUnit XML

```bash
envcause \
  --good good.env \
  --bad bad.env \
  --junit test-results.xml \
  -- pytest --junitxml=test-results.xml
```

A candidate fails when the report contains a `<failure>` or `<error>` element. The command should overwrite the report on every run. Relative report paths are resolved from `--cwd` when supplied.

### Reduce flaky failures

```bash
envcause --good good.env --bad bad.env --repeat 3 -- pytest -q
```

A candidate counts as failing only if it reproduces on every repeat.

### Write a small reproduction file

```bash
envcause \
  --good good.env \
  --bad bad.env \
  --write-repro minimal.env \
  -- pytest -q
```

The generated file contains the actual bad-state values. Terminal output redacts values whose variable names look secret-sensitive unless `--show-values` is supplied.

### Save a machine-readable report

```bash
envcause --good good.env --bad bad.env --report-json result.json -- pytest -q
```

The JSON report includes the command, matching mode, run and cache counts, and the reduced changes. Secret-looking values remain redacted unless `--show-values` is supplied.

### Candidate caching

EnvCause caches candidate results in memory during each reduction, avoiding duplicate command executions when the delta-debugging search revisits a change set. Use `--no-cache` when the reproduction command is stateful and every candidate must be rerun.

To reuse results across invocations, provide a cache file:

```bash
envcause --good good.env --bad bad.env --cache-file .envcause-cache.json -- pytest -q
```

The cache stores SHA-256 fingerprints and pass/fail outcomes, not raw environment values. Fingerprints include the relevant execution environment, command, matcher, working directory, timeout, and repeat count. Volatile GitHub runner bookkeeping such as per-step output paths and run counters is ignored. Known-good and known-bad configurations are always verified with fresh runs before cached candidates are used.

### Follow long reductions

```bash
envcause --good good.env --bad bad.env --progress -- pytest -q
```

Progress is written to stderr and shows the candidate number, number of changed variables, command-run count, and whether the result came from cache.

## Docker and Kubernetes

### Run candidates in Docker

Use `--docker-image` to start a fresh container for every candidate:

```bash
envcause \
  --good good.env \
  --bad bad.env \
  --docker-image my-app:debug \
  -- python /app/reproduce.py
```

Only variables named by the good or bad files are forwarded into the container. Variables absent from a candidate are explicitly unset, even when the image defines them. Values are forwarded through the Docker client's environment rather than included in its local command-line arguments.

Pass Docker options by repeating `--docker-run-arg`. Use the `=` form for values beginning with `--`:

```bash
envcause \
  --good good.env \
  --bad bad.env \
  --docker-image my-app:debug \
  --docker-run-arg=--network=host \
  --docker-run-arg=--volume \
  --docker-run-arg="$PWD:/workspace:ro" \
  --docker-run-arg=--workdir=/workspace \
  -- pytest -q
```

The image must contain the `env` utility. `--cwd` controls where the local Docker client runs; use Docker's `--workdir` argument to change the container working directory. JUnit matching requires the report path to be bind-mounted to the host.

### Run candidates in Kubernetes

Use `--kube-pod` to execute candidates in an existing pod:

```bash
envcause \
  --good good.env \
  --bad bad.env \
  --kube-pod api-7c9d8f6d4-x2k9m \
  --kube-namespace staging \
  --kube-container api \
  --matches 'connection refused' \
  -- python /app/reproduce.py
```

`--kube-context` can select a non-current kubectl context. The target container must contain the `env` utility. Commands run in the pod's existing working directory and should avoid changing shared state because the same pod is reused across candidates.

Kubernetes environment assignments are part of the `kubectl exec` request and may be visible in local process inspection or cluster audit records. Use sanitized configuration files when that visibility is not acceptable. JUnit matching is not supported for pods because the report is remote; use exit-code, `--contains`, or `--matches` mode.

## GitHub Actions

EnvCause can run directly in a workflow as a composite action:

```yaml
jobs:
  diagnose-config:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7

      - name: Restore EnvCause candidate cache
        uses: actions/cache@v5
        with:
          path: .envcause-cache.json
          key: envcause-${{ runner.os }}-${{ github.ref_name }}

      - name: Reduce the failing configuration
        id: envcause
        uses: deeneshchowdhary/EnvCause@v1
        with:
          good: config/good.env
          bad: config/bad.env
          command: pytest -q
          matches: 'Connection refused|HTTP 503'

      - name: Upload the redacted report
        uses: actions/upload-artifact@v7
        with:
          name: envcause-report
          path: ${{ steps.envcause.outputs.report-path }}
```

The action installs no project dependencies of its own and executes the command without a shell. The `command` input supports shell-style quoting for arguments, but shell operators such as pipes and redirects are not interpreted.

By default it:

- writes `envcause-report.json` with secret-looking values redacted
- uses `.envcause-cache.json` for candidate caching
- shows reduction progress in the action log
- adds a result table to the GitHub job summary

Available outputs are `report-path`, `repro-path`, `failure-inducing-count`, `command-executions`, and `cache-hits`. Set `write-repro` to create a minimal `.env` file; unlike the default JSON report, that file contains the real bad-state values and should be handled as a secret-bearing artifact. Set `show-values: "true"` only when exposing configuration values in logs and summaries is acceptable.

The repository's own [CI workflow](.github/workflows/ci.yml) exercises the action locally on every push and pull request.

## How the configuration model works

EnvCause starts from the **good file as the baseline**. Each differing variable can then be switched independently into its state from the bad file.

This also handles variables that exist in only one file:

- present only in `bad.env` → candidate change sets the variable
- present only in `good.env` → candidate change unsets the variable

Variables inherited from the parent shell remain available unless overridden by the supplied files.

## Important limitation: 1-minimal is not globally smallest

EnvCause uses the classic `ddmin` delta-debugging strategy. The result is **1-minimal**: removing any one remaining change stops reproducing the failure. There may theoretically be another unrelated failure-inducing set with fewer variables.

That tradeoff keeps the number of command executions practical.

## Safety

`.env` files commonly contain secrets. EnvCause:

- runs locally
- has no telemetry or network code
- redacts values for names containing terms such as `SECRET`, `TOKEN`, `PASSWORD`, `KEY`, or `AUTH`
- shows variable names by default because names themselves can still be sensitive in some organizations

Use `--show-values` only when appropriate.

## MVP roadmap

Potential next steps:

- JSON / YAML / TOML config reduction
- parallel candidate execution
- `envcause explain` reports
- multiple known-good / known-bad runs for nondeterministic systems

## Development

```bash
python -m unittest discover -s tests -v
```

No runtime dependencies are required.

Contributions are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md) for setup and pull-request guidance.
