"""RED for TASK-002: one seam for every paid call (BEH-05, BEH-44)."""

import importlib
import importlib.util


class TestPaidCallSeam:
    def test_paid_call_module_exposes_the_single_spawn_seam(self) -> None:
        spec = importlib.util.find_spec("spec_runner.paid_call")
        assert spec is not None, "spec_runner.paid_call does not exist yet"

        module = importlib.import_module("spec_runner.paid_call")
        for name in ("PaidCall", "CallOutcome", "execute", "_spawn"):
            assert hasattr(module, name), f"paid_call.{name} is missing"
