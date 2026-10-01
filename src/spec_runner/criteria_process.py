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

#: Retained output per stream for output read only as a tail (the product's pytest, uv,
#: the interpreter checks). Never for git: its stdout is parsed as complete data (R-B20).
DEFAULT_MAX_OUTPUT = 1 << 20
#: The shell's "command not found / cannot execute" status, for a launch failure.
LAUNCH_FAILED = 127


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
    max_output: int | None = None,
) -> Finished:
    """Run `argv` in its own process group under `deadline` and an optional local limit.

    By default all output is kept. With `max_output` (`DEFAULT_MAX_OUTPUT` for output
    read only as a tail), each of stdout/stderr keeps its last `max_output` bytes and
    dropped bytes are announced by a marker line at the head. A command that
    cannot be launched at all (missing executable, bad cwd) is exit 127 with the
    reason on stderr, so every caller's non-zero mapping applies. An empty `argv` is a
    programming error (ValueError), raised before anything is launched.
    """
    if not argv:
        raise ValueError("run_bounded: empty argv")
    deadline.check()
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        if stdin is None:
            args = (argv, cwd, env, deadline, local_timeout, subprocess.DEVNULL, out, err)
            return _run(*args, max_output)
        with tempfile.TemporaryFile() as feed:
            feed.write(stdin)
            feed.seek(0)
            return _run(argv, cwd, env, deadline, local_timeout, feed, out, err, max_output)


def _run(
    argv: Sequence[str],
    cwd: Path,
    env: Mapping[str, str] | None,
    deadline: Deadline,
    local_timeout: float | None,
    stdin: IO[bytes] | int,
    out: IO[bytes],
    err: IO[bytes],
    max_output: int | None,
) -> Finished:
    try:
        proc = subprocess.Popen(
            list(argv),
            cwd=cwd,
            env=None if env is None else dict(env),
            stdin=stdin,
            stdout=out,
            stderr=err,
            start_new_session=True,
        )
    except OSError as exc:
        message = f"cannot launch {argv[0]}: {exc}"
        return Finished(LAUNCH_FAILED, b"", message.encode(), -1, None)
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
    return Finished(returncode, _read(out, max_output), _read(err, max_output), proc.pid, timed_out)


def _kill_group(proc: subprocess.Popen[bytes]) -> None:
    """SIGKILL what is left of the group, then reap the direct child."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    finally:
        proc.wait()


def _read(handle: IO[bytes], limit: int | None) -> bytes:
    """The whole capture, or its last `limit` bytes behind a marker (seek, no full read)."""
    size = handle.seek(0, os.SEEK_END)
    if limit is None or size <= limit:
        handle.seek(0)
        return handle.read()
    handle.seek(size - limit)
    marker = f"[spec-runner: {size - limit} bytes dropped]\n".encode()
    return marker + handle.read()


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


def checkout_git(checkout: Path) -> list[str]:
    """`git` aimed explicitly at `checkout`'s own repository, with literal pathspecs.

    No repository discovery: were `checkout/.git` gone, discovery would walk up to an
    enclosing repository and `reset --hard`/`clean -ffdx` would act on it; with an
    explicit `--git-dir` git refuses instead. `--literal-pathspecs` makes every path
    argument a path, never pathspec magic (`:(top)`, `:!x`).
    """
    return [
        "git",
        f"--git-dir={checkout / '.git'}",
        f"--work-tree={checkout}",
        "--literal-pathspecs",
    ]


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
    max_output: int | None = None,
) -> Finished:
    """`run_bounded`, raising TIMEOUT on the global limit and `kind` on any other failure."""
    done = run_bounded(
        argv,
        cwd=cwd,
        env=env,
        deadline=deadline,
        local_timeout=local_timeout,
        stdin=stdin,
        max_output=max_output,
    )
    if done.timed_out == "global":
        raise CriteriaError(ErrorKind.TIMEOUT, f"{what}: the measurement deadline expired")
    if done.timed_out == "local":
        raise CriteriaError(kind, f"{what}: no answer within {local_timeout:g}s")
    if done.returncode != 0:
        tail = done.stderr.decode("utf-8", errors="replace").strip()[-500:]
        raise CriteriaError(kind, f"{what}: exit {done.returncode}: {tail}")
    return done
