# Security policy

## Reporting a vulnerability

Please do not open a public issue for a security vulnerability or accidental secret exposure. Use [GitHub private vulnerability reporting](https://github.com/deeneshchowdhary/EnvCause/security/advisories/new) instead.

Include the affected version, impact, reproduction steps, and any suggested mitigation. Do not include real production credentials. The maintainer will acknowledge a report as soon as practical and coordinate disclosure after a fix is available.

## Secrets

EnvCause is designed to process environment files locally. Terminal and JSON output redact values for secret-looking variable names by default, but generated reproduction `.env` files contain real values. Treat those files as sensitive and do not upload them as public CI artifacts.
