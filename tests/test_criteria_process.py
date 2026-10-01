"""Bounded process groups under one measurement deadline (#603, design §3.3)."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from spec_runner.criteria_contract import CriteriaError, ErrorKind
from spec_runner.criteria_process import Deadline, run_bounded, run_or_raise


def _sh(script: str) -> list[str]:
    return ["sh", "-c", script]


def _running(pid: int) -> bool:
    """Alive and not a zombie (kill(pid, 0) alone cannot tell them apart)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    state = subprocess.run(
        ["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True
    ).stdout.strip()
    return bool(state) and not state.startswith("Z")


def test_within_both_limits(tmp_path: Path) -> None:
    done = run_bounded(
        _sh("echo out; echo err >&2"),
        cwd=tmp_path,
        env=None,
        deadline=Deadline(60),
        local_timeout=30,
    )
    assert done.returncode == 0
    assert done.timed_out is None
    assert done.stdout == b"out\n"
    assert done.stderr == b"err\n"


def test_local_timeout(tmp_path: Path) -> None:
    started = time.monotonic()
    done = run_bounded(
        ["sleep", "30"], cwd=tmp_path, env=None, deadline=Deadline(60), local_timeout=1
    )
    assert done.timed_out == "local"
    assert done.returncode is None
    assert time.monotonic() - started < 5


def test_global_timeout_and_raise(tmp_path: Path) -> None:
    done = run_bounded(["sleep", "30"], cwd=tmp_path, env=None, deadline=Deadline(1))
    assert done.timed_out == "global"
    with pytest.raises(CriteriaError) as info:
        run_or_raise(
            ["sleep", "30"],
            cwd=tmp_path,
            env=None,
            deadline=Deadline(1),
            kind=ErrorKind.CLONE_FAILED,
            what="sleeping",
        )
    assert info.value.kind is ErrorKind.TIMEOUT


def test_local_timeout_and_failure_raise_the_callers_kind(tmp_path: Path) -> None:
    for argv, local in ((["sleep", "30"], 0.5), (_sh("echo boom >&2; exit 3"), None)):
        with pytest.raises(CriteriaError) as info:
            run_or_raise(
                argv,
                cwd=tmp_path,
                env=None,
                deadline=Deadline(60),
                kind=ErrorKind.CLONE_FAILED,
                what="cloning",
                local_timeout=local,
            )
        assert info.value.kind is ErrorKind.CLONE_FAILED
        assert info.value.detail.startswith("cloning: ")


def test_run_or_raise_returns_on_success(tmp_path: Path) -> None:
    done = run_or_raise(
        _sh("printf ok"),
        cwd=tmp_path,
        env=None,
        deadline=Deadline(60),
        kind=ErrorKind.CLONE_FAILED,
        what="x",
    )
    assert done.stdout == b"ok"


def test_background_descendant_holding_output_is_killed(tmp_path: Path) -> None:
    started = time.monotonic()
    done = run_bounded(
        _sh("sleep 60 & echo $! > pid; exit 0"),
        cwd=tmp_path,
        env=None,
        deadline=Deadline(90),
        local_timeout=30,
    )
    # "did not wait for the descendant", not a speed bound: waiting would take >= 30 s
    assert time.monotonic() - started < 10
    assert done.returncode == 0
    assert done.timed_out is None
    pid = int((tmp_path / "pid").read_text())
    for _ in range(50):
        if not _running(pid):
            break
        time.sleep(0.1)
    assert not _running(pid)


def test_large_io_does_not_deadlock(tmp_path: Path) -> None:
    size = 1_000_000
    script = (
        "import sys;"
        f"d=sys.stdin.buffer.read();assert len(d)=={size};"
        f"sys.stdout.buffer.write(b'o'*{size});sys.stderr.buffer.write(b'e'*{size})"
    )
    done = run_bounded(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env=None,
        deadline=Deadline(60),
        stdin=b"i" * size,
    )
    assert done.returncode == 0
    assert done.stdout == b"o" * size
    assert done.stderr == b"e" * size


def test_output_bytes_are_exact(tmp_path: Path) -> None:
    payload = b"a\r\nb\rc\x00d\xc3\xa9\xe2\x82\xac\xff\xfe\n"
    done = run_bounded(
        [sys.executable, "-c", f"import sys;sys.stdout.buffer.write({payload!r})"],
        cwd=tmp_path,
        env=None,
        deadline=Deadline(60),
    )
    assert done.stdout == payload


def test_stdin_defaults_to_devnull(tmp_path: Path) -> None:
    done = run_bounded(["cat"], cwd=tmp_path, env=None, deadline=Deadline(10))
    assert done.returncode == 0
    assert done.stdout == b""


def test_exhausted_deadline_raises_timeout() -> None:
    with pytest.raises(CriteriaError) as info:
        Deadline(0).check()
    assert info.value.kind is ErrorKind.TIMEOUT
    assert Deadline(0).remaining() == 0.0


def test_missing_executable_is_exit_127(tmp_path: Path) -> None:
    done = run_bounded(
        ["/nonexistent/spec-runner-bin"], cwd=tmp_path, env=None, deadline=Deadline(60)
    )
    assert done.returncode == 127
    assert done.stdout == b""
    assert done.pid == -1
    assert done.timed_out is None
    assert done.stderr.startswith(b"cannot launch /nonexistent/spec-runner-bin: ")


def test_missing_cwd_is_exit_127(tmp_path: Path) -> None:
    done = run_bounded(["true"], cwd=tmp_path / "absent", env=None, deadline=Deadline(60))
    assert done.returncode == 127
    assert done.stderr.startswith(b"cannot launch true: ")


def test_unlaunchable_never_swallows_the_global_timeout(tmp_path: Path) -> None:
    with pytest.raises(CriteriaError) as info:
        run_bounded(["/nonexistent/x"], cwd=tmp_path, env=None, deadline=Deadline(0))
    assert info.value.kind is ErrorKind.TIMEOUT


def test_run_or_raise_on_unlaunchable_raises_the_callers_kind(tmp_path: Path) -> None:
    with pytest.raises(CriteriaError) as info:
        run_or_raise(
            ["/nonexistent/spec-runner-bin"],
            cwd=tmp_path,
            env=None,
            deadline=Deadline(60),
            kind=ErrorKind.CLONE_FAILED,
            what="cloning",
        )
    assert info.value.kind is ErrorKind.CLONE_FAILED
    assert "cannot launch /nonexistent/spec-runner-bin" in info.value.detail


_WRITE_3_MIB = (
    "import sys;[sys.stdout.buffer.write(bytes([i % 251]) * 1024) for i in range(3 * 1024)]"
)


def test_output_keeps_only_the_tail_with_a_marker(tmp_path: Path) -> None:
    done = run_bounded(
        [sys.executable, "-c", _WRITE_3_MIB],
        cwd=tmp_path,
        env=None,
        deadline=Deadline(60),
        max_output=1 << 20,
    )
    marker = f"[spec-runner: {2 << 20} bytes dropped]\n".encode()
    assert done.stdout.startswith(marker)
    assert len(done.stdout) == len(marker) + (1 << 20)
    tail = b"".join(bytes([i % 251]) * 1024 for i in range(2 * 1024, 3 * 1024))
    assert done.stdout[len(marker) :] == tail


def test_default_bound_is_one_mebibyte(tmp_path: Path) -> None:
    done = run_bounded(
        [sys.executable, "-c", _WRITE_3_MIB], cwd=tmp_path, env=None, deadline=Deadline(60)
    )
    assert len(done.stdout) < (1 << 20) + 100


def test_unbounded_returns_everything(tmp_path: Path) -> None:
    done = run_bounded(
        [sys.executable, "-c", _WRITE_3_MIB],
        cwd=tmp_path,
        env=None,
        deadline=Deadline(60),
        max_output=None,
    )
    assert len(done.stdout) == 3 << 20
    assert not done.stdout.startswith(b"[spec-runner")


def test_output_under_the_bound_has_no_marker(tmp_path: Path) -> None:
    done = run_bounded(
        _sh("printf abc"), cwd=tmp_path, env=None, deadline=Deadline(60), max_output=3
    )
    assert done.stdout == b"abc"
