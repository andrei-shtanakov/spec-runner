"""The conftest belt must keep `subprocess.Popen` a subscriptable class
(spec-runner#510).

`mcp` evaluates `subprocess.Popen[bytes]` in an annotation at import time;
a belt that replaced the name with a plain function broke the lazy
`import mcp` of every test that first resolves `spec_runner.mcp_run_server`
under the belt — green in the full suite (mcp already imported at
collection), red in isolation (verify-first replays one file at a time).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from tests.conftest import PaidBinaryReached


def test_belt_keeps_popen_a_subscriptable_class() -> None:
    belted = subprocess.Popen
    assert isinstance(belted, type), "the belt must install a class, not a function"
    assert getattr(belted, "belted_door", None) == "subprocess.Popen"
    # What mcp/os/win32/utilities.py does at import time.
    alias = belted[bytes]  # type: ignore[index]
    assert alias.__origin__ is belted


def test_belt_class_still_refuses_a_paid_binary() -> None:
    """Negative control: turning the wrapper into a class must not open the door."""
    with pytest.raises(PaidBinaryReached):
        subprocess.Popen(["claude", "--version"])


def test_lazy_mcp_import_passes_in_isolation() -> None:
    """The exact replay verify-first performs: the file alone, fresh process."""
    repo = Path(__file__).resolve().parent.parent
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "tests/test_lazy_mcp_import.py",
            "-q",
            "-p",
            "no:cacheprovider",
        ],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-2000:]
