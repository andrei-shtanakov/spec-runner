"""MCP launch scope: holder, contradiction predicate, and the parent side of
the startup handshake (spec-runner#485).

Deliberately does not import the ``mcp`` SDK: ``mcp`` stays an optional
dependency (see `mcp_server.py`'s module docstring), and every symbol here
must be testable without it installed.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .config import ExecutorConfig, _resolve_config_path, build_config, load_config_from_yaml

if TYPE_CHECKING:
    import subprocess


@dataclass(frozen=True)
class LaunchScope:
    """The config + resolved identity a server (or `_build_config("")`
    fallback, #485 §1.2) is serving."""

    config: ExecutorConfig
    project_root: Path
    namespace: dict[str, str]
    config_path: Path

    @classmethod
    def of(cls, config: ExecutorConfig) -> LaunchScope:
        if config.change_id:
            namespace: dict[str, str] = {"kind": "change", "id": config.change_id}
        elif config.spec_prefix:
            namespace = {"kind": "prefix", "prefix": config.spec_prefix}
        else:
            namespace = {"kind": "flat"}
        return cls(
            config=config,
            project_root=config.project_root,
            namespace=namespace,
            config_path=_resolve_config_path(config.project_root),
        )


class ScopeContradiction(Exception):
    """A tool-level `spec_prefix` contradicts the launch namespace (FR-02)."""

    def __init__(self, namespace: dict[str, str], requested_spec_prefix: str) -> None:
        self.namespace = namespace
        self.requested_spec_prefix = requested_spec_prefix
        super().__init__(str(self))

    def __str__(self) -> str:
        return (
            f"tool-level spec_prefix {self.requested_spec_prefix!r} contradicts "
            f"the launch namespace {self.namespace!r}"
        )

    def to_dict(self) -> dict:
        return {
            "status": "error",
            "error": str(self),
            "launch_namespace": self.namespace,
            "requested_spec_prefix": self.requested_spec_prefix,
        }


def _rebuild_for_namespace(project_root: Path, *, spec_prefix: str = "") -> ExecutorConfig:
    """Rebuild a config for `project_root` under a different `spec_prefix` --
    the flat-launch "refinement" branch (§1.3, Q-01 working assumption).

    Reads the same YAML the launch scope reads (by `project_root`, not CWD),
    same as `mcp_server._build_config` does for the flat/no-namespace case.
    """
    config_path = _resolve_config_path(project_root)
    yaml_config = load_config_from_yaml(config_path)
    args = argparse.Namespace(
        spec_prefix=spec_prefix,
        project_root=str(project_root),
        max_retries=None,
        timeout=None,
        no_tests=False,
        no_branch=False,
        no_commit=False,
        no_review=False,
        hitl_review=False,
        callback_url="",
        log_level=None,
        budget=None,
        task_budget=None,
    )
    return build_config(yaml_config, args)


def resolve_tool_config(scope: LaunchScope, spec_prefix: str = "") -> ExecutorConfig:
    """The one predicate every tool goes through (§1.3, BEH-05/06/07).

    - empty prefix -> the launch scope's config, always.
    - `change` scope + any prefix -> contradiction.
    - `prefix` scope + matching prefix -> the launch scope's config;
      mismatching prefix -> contradiction.
    - `flat` scope + non-empty prefix -> refinement: rebuild for that prefix
      under the same `project_root` (working assumption Q-01).
    """
    if not spec_prefix:
        return scope.config

    kind = scope.namespace["kind"]
    if kind == "change":
        raise ScopeContradiction(scope.namespace, spec_prefix)
    if kind == "prefix":
        if spec_prefix == scope.namespace["prefix"]:
            return scope.config
        raise ScopeContradiction(scope.namespace, spec_prefix)

    return _rebuild_for_namespace(scope.project_root, spec_prefix=spec_prefix)


# === Startup handshake (§3.2) ===


@dataclass(frozen=True)
class Ready:
    pid: int


@dataclass(frozen=True)
class Exited:
    returncode: int
    log_tail: str


@dataclass(frozen=True)
class TimedOut:
    pid: int
    terminated: bool


WaitOutcome = Ready | Exited | TimedOut


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False
    except OSError:
        return False


def _read_ready_pid(ready_file: Path) -> int | None:
    try:
        content = ready_file.read_text()
    except OSError:
        return None
    for line in content.splitlines():
        if line.startswith("PID:"):
            try:
                return int(line.split(":", 1)[1].strip())
            except ValueError:
                return None
    return None


def clear_stale_ready_file(ready_file: Path) -> None:
    """Remove a ready file left by a dead process, before `Popen` (§3.2)."""
    pid = _read_ready_pid(ready_file)
    if pid is not None and not _pid_alive(pid):
        with contextlib.suppress(OSError):
            ready_file.unlink()


def _read_log_tail(log_file: Path | None, lines: int = 20) -> str:
    if log_file is None or not log_file.exists():
        return ""
    try:
        text = log_file.read_text(errors="replace")
    except OSError:
        return ""
    return "\n".join(text.splitlines()[-lines:])


def _terminate(proc: subprocess.Popen, *, grace_seconds: float = 5.0) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=grace_seconds)
    except Exception:
        proc.kill()
        with contextlib.suppress(Exception):
            proc.wait(timeout=grace_seconds)


def wait_for_ready(
    proc: subprocess.Popen,
    ready_file: Path,
    timeout: float,
    *,
    log_file: Path | None = None,
    poll_interval: float = 0.05,
) -> WaitOutcome:
    """Poll for the child publishing `ready_file`, or dying, or timing out.

    (а) `ready_file` exists with `PID == proc.pid` -> unlink -> `Ready`.
    (б) `proc.poll()` is not `None` -> `Exited(returncode, log_tail)`.
    (в) elapsed > timeout -> terminate (SIGTERM -> SIGKILL) -> `TimedOut`.

    In (б)/(в), a `ready_file` written under our own pid is removed too
    (§3.2) -- the caller must never see a `started`-shaped ready file after
    an error response.
    """
    deadline = time.monotonic() + timeout
    while True:
        if ready_file.exists():
            pid = _read_ready_pid(ready_file)
            if pid == proc.pid:
                with contextlib.suppress(OSError):
                    ready_file.unlink()
                return Ready(pid=proc.pid)

        returncode = proc.poll()
        if returncode is not None:
            _cleanup_own_ready_file(ready_file, proc.pid)
            return Exited(returncode=returncode, log_tail=_read_log_tail(log_file))

        if time.monotonic() >= deadline:
            _terminate(proc)
            _cleanup_own_ready_file(ready_file, proc.pid)
            return TimedOut(pid=proc.pid, terminated=True)

        time.sleep(poll_interval)


def _cleanup_own_ready_file(ready_file: Path, pid: int) -> None:
    if _read_ready_pid(ready_file) == pid:
        with contextlib.suppress(OSError):
            ready_file.unlink()
