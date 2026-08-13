# EnvCause launch kit

Ready-to-edit copy for announcing EnvCause. Verify community rules before
posting, and answer comments with concrete examples rather than repeating the
announcement.

## Article

### Git bisect for configuration failures

A deployment breaks after eight configuration values change. Looking at the
diff tells you what changed, but not which changes caused the failure. Testing
each value independently can also miss the answer when two individually safe
settings fail only in combination.

EnvCause applies delta debugging to that problem. Give it a known-good config, a
known-bad config, and a command that reproduces the failure. It repeatedly builds
candidate configurations and runs the command until it finds a 1-minimal
failure-inducing change set.

```bash
envcause \
  --good config/good.yaml \
  --bad config/bad.yaml \
  --config-output /tmp/candidate.yaml \
  -- python reproduce.py /tmp/candidate.yaml
```

If seven paths changed but only `/auth/enabled` and `/auth/algorithm` are needed
to trigger the failure, the final candidate contains those bad states on top of
the good baseline. Removing either remaining change stops reproduction.

EnvCause supports `.env`, JSON, YAML, and TOML, as well as literal/regex output
matching, JUnit reports, repeated runs, candidate caching, Docker, Kubernetes,
and GitHub Actions. It runs locally and redacts secret-looking values in terminal
and JSON reports by default.

It does not claim to prove the globally smallest possible set. The `ddmin`
result is 1-minimal, which keeps the number of command runs practical while
still producing a useful reproduction.

Project: https://github.com/deeneshchowdhary/EnvCause

Install: `pipx install envcause`

## Short announcement

I built EnvCause: git bisect for configuration failures.

Give it known-good and known-bad `.env`, JSON, YAML, or TOML files plus a failing
command. It tests combinations and returns a 1-minimal set of changes that still
reproduces the failure—including failures caused by interacting settings.

It runs locally and supports Docker, Kubernetes, GitHub Actions, JUnit, regex
matching, flaky-run repetition, and caching.

https://github.com/deeneshchowdhary/EnvCause

## Community-specific opening lines

- Python: “I packaged a local delta-debugging CLI for reducing configuration
  failures, with no service or account required.”
- DevOps/SRE: “When a deployment has a large config diff, this identifies the
  smallest combination that still reproduces the incident.”
- Kubernetes: “EnvCause can test `.env` candidates in an existing pod and reduce
  host-side JSON/YAML/TOML candidate files used by reproduction commands.”
- Testing: “This uses classic `ddmin`, so it catches interacting changes that
  one-variable-at-a-time testing misses.”

## Suggested title variants

- Show HN: EnvCause – Git bisect for configuration failures
- Find the two settings that broke your deployment
- Delta debugging for `.env`, JSON, YAML, and TOML
