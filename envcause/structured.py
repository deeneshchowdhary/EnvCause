from __future__ import annotations

import copy
import json
import os
import tempfile
from pathlib import Path
from typing import Mapping, Sequence

from .core import EnvCauseError, EnvChange, _UNSET


_FORMATS = {".json": "json", ".yaml": "yaml", ".yml": "yaml", ".toml": "toml"}


def detect_format(path: str | os.PathLike[str], requested: str = "auto") -> str:
    if requested != "auto":
        return requested
    try:
        return _FORMATS[Path(path).suffix.lower()]
    except KeyError as exc:
        raise EnvCauseError(
            f"Cannot detect config format from {path}; use --format json, yaml, or toml"
        ) from exc


def load_config(path: str | os.PathLike[str], format_name: str = "auto") -> object:
    config_path = Path(path)
    if not config_path.exists():
        raise EnvCauseError(f"Configuration file not found: {config_path}")
    format_name = detect_format(config_path, format_name)
    try:
        if format_name == "json":
            value = json.loads(config_path.read_text(encoding="utf-8"))
        elif format_name == "yaml":
            try:
                import yaml
            except ImportError as exc:
                raise EnvCauseError("YAML support requires PyYAML") from exc
            value = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        else:
            try:
                import tomllib
            except ImportError:  # Python 3.10
                import tomli as tomllib
            with config_path.open("rb") as handle:
                value = tomllib.load(handle)
    except (OSError, ValueError) as exc:
        raise EnvCauseError(f"Could not parse {format_name.upper()} config {config_path}: {exc}") from exc
    if not isinstance(value, dict):
        raise EnvCauseError(f"Top level of {config_path} must be an object/table")
    return value


def _escape(part: str) -> str:
    return part.replace("~", "~0").replace("/", "~1")


def _unescape(part: str) -> str:
    return part.replace("~1", "/").replace("~0", "~")


def diff_configs(good: Mapping[str, object], bad: Mapping[str, object]) -> list[EnvChange]:
    changes: list[EnvChange] = []

    def walk(left: object, right: object, parts: tuple[str, ...]) -> None:
        if isinstance(left, dict) and isinstance(right, dict):
            for key in sorted(set(left) | set(right)):
                child = parts + (str(key),)
                if key not in left:
                    changes.append(EnvChange(_pointer(child), _UNSET, copy.deepcopy(right[key])))
                elif key not in right:
                    changes.append(EnvChange(_pointer(child), copy.deepcopy(left[key]), _UNSET))
                else:
                    walk(left[key], right[key], child)
        elif left != right or type(left) is not type(right):
            changes.append(EnvChange(_pointer(parts), copy.deepcopy(left), copy.deepcopy(right)))

    walk(good, bad, ())
    return changes


def _pointer(parts: Sequence[str]) -> str:
    return "/" + "/".join(_escape(part) for part in parts)


def build_config(good: Mapping[str, object], changes: Sequence[EnvChange]) -> dict[str, object]:
    result = copy.deepcopy(dict(good))
    for change in changes:
        parts = [_unescape(part) for part in change.key.split("/")[1:]]
        parent: dict[str, object] = result
        for part in parts[:-1]:
            child = parent.get(part)
            if not isinstance(child, dict):
                child = {}
                parent[part] = child
            parent = child
        key = parts[-1]
        if change.bad is _UNSET:
            parent.pop(key, None)
        else:
            parent[key] = copy.deepcopy(change.bad)
    return result


def write_config(path: str | os.PathLike[str], value: Mapping[str, object], format_name: str = "auto") -> None:
    output = Path(path)
    format_name = detect_format(output, format_name)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        if format_name == "json":
            text = json.dumps(value, indent=2, ensure_ascii=False) + "\n"
        elif format_name == "yaml":
            try:
                import yaml
            except ImportError as exc:
                raise EnvCauseError("YAML support requires PyYAML") from exc
            text = yaml.safe_dump(dict(value), sort_keys=False, allow_unicode=True)
        else:
            try:
                import tomli_w
            except ImportError as exc:
                raise EnvCauseError("TOML output requires tomli-w") from exc
            text = tomli_w.dumps(dict(value))
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{output.name}.", suffix=".tmp", dir=output.parent
        )
        temporary = Path(temporary_name)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
        temporary.replace(output)
    except OSError as exc:
        try:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise EnvCauseError(f"Could not write candidate config {output}: {exc}") from exc
