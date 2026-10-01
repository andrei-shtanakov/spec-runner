"""#603 R-B18: the criteria-probe job's switch turns the bench's skips into failures."""

from __future__ import annotations

import pytest

from tests.criteria_bench_required import REQUIRE_ENV, import_plugin, needs_312

ABSENT = "spec_runner_no_such_plugin_for_the_bench"


class TestNeeds312:
    def test_old_python_skips_without_the_switch(self, monkeypatch) -> None:
        monkeypatch.delenv(REQUIRE_ENV, raising=False)
        mark = needs_312("why", version=(3, 11))
        assert mark.args == (True,) and mark.kwargs == {"reason": "why"}

    def test_old_python_raises_under_the_switch(self, monkeypatch) -> None:
        monkeypatch.setenv(REQUIRE_ENV, "1")
        with pytest.raises(RuntimeError, match=REQUIRE_ENV):
            needs_312("why", version=(3, 11))

    @pytest.mark.parametrize("switch", ["1", "0", None])
    def test_new_python_never_skips(self, monkeypatch, switch) -> None:
        if switch is None:
            monkeypatch.delenv(REQUIRE_ENV, raising=False)
        else:
            monkeypatch.setenv(REQUIRE_ENV, switch)
        assert needs_312("why", version=(3, 12)).args == (False,)


class TestImportPlugin:
    def test_missing_plugin_skips_without_the_switch(self, monkeypatch) -> None:
        monkeypatch.delenv(REQUIRE_ENV, raising=False)
        with pytest.raises(pytest.skip.Exception):
            import_plugin(ABSENT)

    def test_missing_plugin_fails_under_the_switch(self, monkeypatch) -> None:
        monkeypatch.setenv(REQUIRE_ENV, "1")
        with pytest.raises(pytest.fail.Exception, match=ABSENT):
            import_plugin(ABSENT)

    def test_present_module_is_returned(self, monkeypatch) -> None:
        monkeypatch.setenv(REQUIRE_ENV, "1")
        assert import_plugin("json").__name__ == "json"
