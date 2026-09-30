"""Bounded process groups under one measurement deadline (#603).

Every step of `verify --criteria` (git, uv, the product's pytest) runs through
`run_bounded`: one `Deadline` for the whole measurement, an optional local limit
per step, output captured as exact bytes, and the launched process group killed
however the step ends. POSIX only (macOS, Linux). A descendant that leaves the
group with `setsid`/`setpgid` is outside the containment boundary.

Design: docs/superpowers/specs/2026-09-29-criteria-closure-verify-design.md §3.3
"""

from __future__ import annotations

import os
import signal
import subprocess
import tempfile
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Literal

from spec_runner.criteria_contract import CriteriaError, ErrorKind


@dataclass
class Deadline:
    """One wall-clock budget shared by every step of a measurement."""

    seconds: float
    started: float = field(default_factory=time.monotonic)

    def remaining(self) -> float:
        """Seconds left; never negative."""
        return max(0.0, self.seconds - (time.monotonic() - self.started))

    def check(self) -> None:
        """Raise TIMEOUT when the budget is exhausted."""
        if self.remaining() <= 0:
            raise CriteriaError(ErrorKind.TIMEOUT, f"the {self.seconds:g}s deadline is exhausted")


@dataclass(frozen=True)
class Finished:
    """A finished step: exact output bytes, and which limit (if any) killed it."""

    returncode: int | None
    stdout: bytes
    stderr: bytes
    pid: int
    timed_out: Literal["local", "global"] | None


def run_bounded(
    argv: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str] | None,
    deadline: Deadline,
    local_timeout: float | None = None,
    stdin: bytes | None = None,
) -> Finished:
    """Run `argv` in its own process group under `deadline` and an optional local limit."""
    deadline.check()
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        if stdin is None:
            return _run(argv, cwd, env, deadline, local_timeout, subprocess.DEVNULL, out, err)
        with tempfile.TemporaryFile() as feed:
            feed.write(stdin)
            feed.seek(0)
            return _run(argv, cwd, env, deadline, local_timeout, feed, out, err)


def _run(
    argv: Sequence[str],
    cwd: Path,
    env: Mapping[str, str] | None,
    deadline: Deadline,
    local_timeout: float | None,
    stdin: IO[bytes] | int,
    out: IO[bytes],
    err: IO[bytes],
) -> Finished:
    proc = subprocess.Popen(
        list(argv),
        cwd=cwd,
        env=None if env is None else dict(env),
        stdin=stdin,
        stdout=out,
        stderr=err,
        start_new_session=True,
    )
    timed_out: Literal["local", "global"] | None = None
    returncode: int | None = None
    try:
        local_left = float("inf") if local_timeout is None else local_timeout
        global_left = deadline.remaining()
        try:
            returncode = proc.wait(timeout=min(local_left, global_left))
        except subprocess.TimeoutExpired:
            timed_out = "local" if local_left < global_left else "global"
    finally:
        _kill_group(proc)
    return Finished(returncode, _read(out), _read(err), proc.pid, timed_out)


def _kill_group(proc: subprocess.Popen[bytes]) -> None:
    """SIGKILL what is left of the group, then reap the direct child."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    finally:
        proc.wait()


def _read(handle: IO[bytes]) -> bytes:
    handle.seek(0)
    return handle.read()


def c_locale_env() -> dict[str, str]:
    """A copy of the process environment with an untranslated (C) locale and no `GIT_*`.

    git's and uv's stderr is matched by wording; a translated message would turn a
    product property (exit 3) into a retryable machine failure (exit 2). Every
    inherited `GIT_*` is dropped: started from a git hook, `GIT_DIR`/`GIT_WORK_TREE`
    would point `reset --hard`/`clean -ffdx` at the hook's repository.
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(LC_ALL="C", LANG="C")
    return env


def run_or_raise(
    argv: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str] | None,
    deadline: Deadline,
    kind: ErrorKind,
    what: str,
    local_timeout: float | None = None,
    stdin: bytes | None = None,
) -> Finished:
    """`run_bounded`, raising TIMEOUT on the global limit and `kind` on any other failure."""
    done = run_bounded(
        argv, cwd=cwd, env=env, deadline=deadline, local_timeout=local_timeout, stdin=stdin
    )
    if done.timed_out == "global":
        raise CriteriaError(ErrorKind.TIMEOUT, f"{what}: the measurement deadline expired")
    if done.timed_out == "local":
        raise CriteriaError(kind, f"{what}: no answer within {local_timeout:g}s")
    if done.returncode != 0:
        tail = done.stderr.decode("utf-8", errors="replace").strip()[-500:]
        raise CriteriaError(kind, f"{what}: exit {done.returncode}: {tail}")
    return done
