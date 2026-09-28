"""#593: a wrapper's flag value is not the executable.

`executable_of` skipped a wrapper's flags but not their values, so
`uv run --frozen --group governance pytest -q` ran `governance` as far as the
harness could tell: no adapter was inferred (a verify_first task refused at
`validate`), and a declared `tdd_runner: pytest` was refused as "does not run
pytest". The scoped command had the same blind spot from the other side — it
dropped `governance` as a stray test path, leaving `uv run --group pytest`.
"""

from __future__ import annotations

import pytest

from spec_runner.config import ExecutorConfig
from spec_runner.tdd_runners import PytestAdapter, Selector, executable_of, infer_adapter

ISSUE_COMMAND = "uv run --frozen --group governance pytest -q"

ADAPTER = PytestAdapter()


class TestTheExecutableIsFoundPastAWrappersFlagValue:
    @pytest.mark.parametrize(
        "command",
        [
            ISSUE_COMMAND,
            "uv run --frozen --group=governance pytest -q",
            "uv run --with pytest-xdist pytest",
            "uv run -w pytest-xdist pytest",
            "uv run --python 3.12 pytest",
            "uv run -p 3.12 pytest",
            "uv run --extra test --only-group ci pytest tests/",
            "uv --directory sub run pytest",
            "uv run --env-file .env.test python -m pytest",
            "python -W error -m pytest",
            "python -X dev -m pytest",
            "poetry -C sub run pytest",
            "poetry --directory sub run pytest",
        ],
    )
    def test_the_value_is_skipped_with_its_flag(self, command):
        assert executable_of(command) == "pytest"

    def test_the_issue_command_infers_pytest(self):
        adapter = infer_adapter(ISSUE_COMMAND)
        assert adapter is not None and adapter.name == "pytest"

    def test_a_declared_pytest_runner_accepts_it(self):
        assert ADAPTER.validate_command(ISSUE_COMMAND) is None

    def test_config_resolves_the_runner_either_way(self):
        assert ExecutorConfig(test_command=ISSUE_COMMAND).resolve_tdd_runner() == "pytest"
        declared = ExecutorConfig(test_command=ISSUE_COMMAND, tdd_runner="pytest")
        assert declared.resolve_tdd_runner() == "pytest"

    def test_an_unknown_wrapper_flag_is_still_boolean(self):
        """The allowlist is curated, not guessed: a flag it does not name
        consumes nothing, so the next token is read as the executable — the
        same terminal policy as `_flag_takes_separate_value`."""
        assert executable_of("uv run --frozen pytest") == "pytest"


class TestTheScopedCommandKeepsAWrappersFlagValue:
    def _scoped(self, command: str) -> list[str]:
        selector = ADAPTER.parse_selector("tests/test_x.py::test_y")
        assert isinstance(selector, Selector)
        return ADAPTER.build_scoped_command(command, selector)

    def test_the_group_name_survives(self):
        assert self._scoped(ISSUE_COMMAND + " tests/") == [
            "uv",
            "run",
            "--frozen",
            "--group",
            "governance",
            "pytest",
            "-q",
            "tests/test_x.py::test_y",
        ]

    def test_uvs_short_python_flag_keeps_its_value(self):
        assert self._scoped("uv run -p 3.12 pytest tests/") == [
            "uv",
            "run",
            "-p",
            "3.12",
            "pytest",
            "tests/test_x.py::test_y",
        ]

    def test_a_wrapper_flag_means_nothing_past_the_executable(self):
        """The vocabulary is positional: after `pytest` a flag is pytest's.
        Protecting `tests/` behind a wrapper's spelling there would run the
        whole suite beside the selector — the silent outcome the scoped
        command exists to prevent."""
        assert self._scoped("uv run pytest --project tests/") == [
            "uv",
            "run",
            "pytest",
            "--project",
            "tests/test_x.py::test_y",
        ]
