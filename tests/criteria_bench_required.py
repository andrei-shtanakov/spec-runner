"""The criteria-probe job's switch: under it, the bench's skips are failures (#603 R-B18).

Locally the bench, the probe's run mode and the real selector runs skip below
CPython 3.12 (no `sys.monitoring`) and their xdist/forked rows skip without those
plugins. `.github/workflows/criteria-probe.yml` sets `SPEC_RUNNER_REQUIRE_CRITERIA_BENCH=1`:
a skipped bench there is a green job that measured nothing, so an old interpreter is
a collection error and a missing plugin a test failure — the guard lives next to the
tests, like `SPEC_RUNNER_REQUIRE_EXUNIT` in test_exunit_adapter.py.
"""

from __future__ import annotations

import importlib
import os
import sys
from types import ModuleType

import pytest

REQUIRE_ENV = "SPEC_RUNNER_REQUIRE_CRITERIA_BENCH"


def required() -> bool:
    """Whether this run is the criteria-probe job (the switch is set to "1")."""
    return os.environ.get(REQUIRE_ENV) == "1"


def needs_312(reason: str, version: tuple[int, int] | None = None) -> pytest.MarkDecorator:
    """`skipif` below CPython 3.12; under the switch, raise instead (a collection error)."""
    too_old = (version or (sys.version_info[0], sys.version_info[1])) < (3, 12)
    if too_old and required():
        raise RuntimeError(f"{REQUIRE_ENV}=1 but this is Python < 3.12 — {reason}")
    return pytest.mark.skipif(too_old, reason=reason)


def import_plugin(name: str) -> ModuleType:
    """`pytest.importorskip(name)`; under the switch a missing plugin fails the test."""
    if not required():
        return pytest.importorskip(name)
    try:
        return importlib.import_module(name)
    except ImportError as exc:
        pytest.fail(f"{REQUIRE_ENV}=1 but {name} is not importable: {exc}")
