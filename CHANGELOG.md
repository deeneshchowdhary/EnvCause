# Changelog

All notable changes to EnvCause are documented here.

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
