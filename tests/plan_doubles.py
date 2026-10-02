"""Doubles for driving the planning paths without a real agent (#480).

Planning used to accept ``invoke=subprocess.run``, a parameter tests filled
with a fake. The parameter is gone -- planning goes through the same paid-call
seam as every other call -- so the fake now stands in for ``paid_call._spawn``.
These wrappers keep the old calling convention (``invoke=`` a
``subprocess.run``-shaped callable) so the tests' intent reads unchanged, while
the double sits where the seam says it must.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from unittest.mock import patch

from spec_runner import cli_plan, paid_call


def spawn_from_run(invoke: Callable[..., Any]) -> Callable[..., Any]:
    """Adapt a ``subprocess.run``-shaped callable to the ``_spawn`` signature."""

    def _spawn(invocation, *, timeout, cwd, env):
        return invoke(
            invocation.argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd,
            env=env,
        )

    return _spawn


def run_gated_stage(stage, description, config, invoke=None, **kwargs):
    """``cli_plan.run_gated_stage`` with ``invoke`` standing in for the spawn."""
    if invoke is None:
        return cli_plan.run_gated_stage(stage, description, config, **kwargs)
    with patch.object(paid_call, "_spawn", spawn_from_run(invoke)):
        return cli_plan.run_gated_stage(stage, description, config, **kwargs)


def generate_stage_draft(stage, description, config, invoke=None):
    """``cli_plan._generate_stage_draft`` with ``invoke`` standing in for the spawn."""
    if invoke is None:
        return cli_plan._generate_stage_draft(stage, description, config)
    with patch.object(paid_call, "_spawn", spawn_from_run(invoke)):
        return cli_plan._generate_stage_draft(stage, description, config)
