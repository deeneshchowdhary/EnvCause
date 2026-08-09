"""Execution adapters for containerized reproduction commands."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence


PreparedCommand = tuple[list[str], dict[str, str], str | None]


@dataclass(frozen=True)
class DockerAdapter:
    """Run each reproduction command in a fresh Docker container."""

    image: str
    managed_keys: tuple[str, ...]
    run_args: tuple[str, ...] = ()
    executable: str = "docker"

    def __post_init__(self) -> None:
        if not self.image:
            raise ValueError("Docker image must not be empty")

    @property
    def description(self) -> str:
        return f"Docker image {self.image}"

    @property
    def cache_identity(self) -> Mapping[str, object]:
        return {
            "type": "docker",
            "image": self.image,
            "run_args": list(self.run_args),
            "executable": self.executable,
        }

    def prepare(
        self,
        command: Sequence[str],
        env: Mapping[str, str],
        cwd: str | None,
    ) -> PreparedCommand:
        keys = sorted(set(self.managed_keys))
        present = [key for key in keys if key in env]
        absent = [key for key in keys if key not in env]

        # `--env KEY` forwards through the Docker client's environment without
        # placing the value in the local process arguments.
        actual = [self.executable, "run", "--rm", *self.run_args]
        for key in present:
            actual.extend(["--env", key])
        # Override any image ENTRYPOINT so `env` always controls the candidate
        # environment before it executes the reproduction command.
        actual.extend(["--entrypoint", "env", self.image])
        for key in absent:
            actual.extend(["-u", key])
        actual.extend(command)
        return actual, dict(env), cwd


@dataclass(frozen=True)
class KubernetesAdapter:
    """Run each reproduction command in an existing Kubernetes pod."""

    pod: str
    managed_keys: tuple[str, ...]
    namespace: str | None = None
    container: str | None = None
    context: str | None = None
    executable: str = "kubectl"

    def __post_init__(self) -> None:
        if not self.pod:
            raise ValueError("Kubernetes pod must not be empty")

    @property
    def description(self) -> str:
        target = self.pod
        if self.namespace:
            target = f"{self.namespace}/{target}"
        if self.container:
            target = f"{target} (container {self.container})"
        return f"Kubernetes pod {target}"

    @property
    def cache_identity(self) -> Mapping[str, object]:
        return {
            "type": "kubernetes",
            "pod": self.pod,
            "namespace": self.namespace,
            "container": self.container,
            "context": self.context,
            "executable": self.executable,
        }

    def prepare(
        self,
        command: Sequence[str],
        env: Mapping[str, str],
        cwd: str | None,
    ) -> PreparedCommand:
        keys = sorted(set(self.managed_keys))
        actual = [self.executable]
        if self.context:
            actual.extend(["--context", self.context])
        actual.append("exec")
        if self.namespace:
            actual.extend(["--namespace", self.namespace])
        actual.append(self.pod)
        if self.container:
            actual.extend(["--container", self.container])
        actual.extend(["--", "env"])
        for key in keys:
            if key not in env:
                actual.extend(["-u", key])
        for key in keys:
            if key in env:
                actual.append(f"{key}={env[key]}")
        actual.extend(command)

        # cwd applies to the local kubectl process. EnvCause deliberately does
        # not add a remote shell merely to change the pod's working directory.
        return actual, dict(env), cwd
