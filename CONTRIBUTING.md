# Contributing to EnvCause

Thanks for helping improve EnvCause.

## Development setup

EnvCause requires Python 3.10 or newer and has no runtime dependencies.

```bash
git clone https://github.com/deeneshchowdhary/EnvCause.git
cd EnvCause
python -m pip install -e .
python -m unittest discover -s tests -v
```

## Proposing a change

1. Open an issue for substantial features so the approach can be discussed first.
2. Keep changes focused and include tests for new behavior.
3. Run the full test suite before opening a pull request.
4. Update the README or changelog when behavior visible to users changes.

Please never include real credentials or production `.env` files in issues, tests, or pull requests.
