# Changelog

## 0.4.1 - 2026-09-04

- Prevent stale JUnit XML from being reused across candidate runs.
- Preserve non-ASCII text and unknown escapes in double-quoted dotenv values.
- Validate positive timeout and reduction-test limits before execution.
- Write new secret-bearing reproduction and structured candidate files with owner-only permissions on POSIX systems.
- Restore an existing structured candidate file when reduction fails or is interrupted.
- Add production-safe operation guidance.

All notable changes to EnvCause are documented here.

## 0.4.0 - 2026-08-22

### Added

- `--verify-repeat` to verify the known-good and known-bad baselines a
  different number of times than `--repeat` uses during reduction, for
  nondeterministic systems
- `--parallel` to test multiple candidates concurrently during reduction, for
  `.env` and Docker reduction

## 0.3.0 - 2026-08-13

### Added

- Nested JSON, YAML, and TOML configuration reduction
- Atomic candidate-file writes and JSON Pointer paths in reports
- Structured configuration inputs for the reusable GitHub Action
- `envcause explain` terminal and Markdown diagnosis reports

### Changed

- Arrays are reduced as atomic values to keep candidate documents valid
- Secret redaction now recognizes nested structured-config paths

## 0.2.0 - 2026-08-09

### Added

- Docker adapter for reducing configurations inside fresh image containers
- Kubernetes adapter for reducing configurations inside an existing pod/container
- Docker and Kubernetes inputs for the reusable GitHub Action

### Changed

- Recommend `pipx` as the primary CLI installation method

## 0.1.0 - 2026-08-08

Initial public release.

### Added

- Dependency-free `.env` parsing and comparison
- 1-minimal failure-inducing change reduction using `ddmin`
- Exit-code, literal output, regular-expression, and JUnit XML failure matching
- Repeated runs and per-command timeouts for unreliable failures
- In-memory and persistent candidate-result caching
- Secret-aware terminal and JSON reports
- Minimal reproduction `.env` generation
- Live reduction progress
- Reusable GitHub Action with outputs and job summaries
