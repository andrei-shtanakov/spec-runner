"""`**Touches:**` — a task's declared write scope, checked by `preflight`
against the harness surface (harness-guard-companions #4).

TASK-022 (#137) had to change `pyproject.toml` under `harness_guard: strict`:
an unfulfillable contract from the start, found only after paid attempts. The
conflict is static. `preflight` never guesses, so the task states its scope and
the check compares what was stated. Design:
docs/superpowers/specs/2026-09-28-touches-preflight-design.md.
"""

from pathlib import Path, PurePosixPath

import pytest

from spec_runner.config import ExecutorConfig
from spec_runner.preflight import run_preflight
from spec_runner.task import parse_tasks
from spec_runner.validate import validate_task_fields


def _task_md(task_id: str, status: str, body: str) -> str:
    return f"### {task_id}: t\n\U0001f7e0 P1 | {status}\nEst: 1d\n{body}\n"


TODO = "⬜ TODO"
DONE = "✅ DONE"


def _write_tasks(root: Path, *tasks: str) -> Path:
    (root / "spec").mkdir(exist_ok=True)
    path = root / "spec" / "tasks.md"
    path.write_text("".join(tasks))
    return path


def _cfg(root: Path, **overrides) -> ExecutorConfig:
    return ExecutorConfig(
        project_root=root,
        state_file=root / "state.db",
        logs_dir=root / "logs",
        create_git_branch=False,
        auto_commit=False,
        run_tests_on_done=False,
        run_lint_on_done=False,
        harness_guard=overrides.pop("harness_guard", "strict"),
        **overrides,
    )


def _touches_check(root: Path, **overrides):
    (check,) = [
        c for c in run_preflight(_cfg(root, **overrides)).checks if c.id == "harness.touches"
    ]
    return check


class TestParsing:
    def test_absent_line_is_none(self, tmp_path):
        (task,) = parse_tasks(_write_tasks(tmp_path, _task_md("TASK-001", TODO, "")))
        assert task.touches is None and task.touches_error is None

    def test_paths_are_normalised(self, tmp_path):
        (task,) = parse_tasks(
            _write_tasks(
                tmp_path,
                _task_md("TASK-001", TODO, "**Touches:** ./src/app.py, docs/, pyproject.toml\n"),
            )
        )
        assert task.touches == [
            PurePosixPath("src/app.py"),
            PurePosixPath("docs"),
            PurePosixPath("pyproject.toml"),
        ]

    @pytest.mark.parametrize(
        "declared,why",
        [
            ("", "empty"),
            ("/etc/passwd", "absolute"),
            ("../other/file.py", ".."),
            ("src/*.py", "glob"),
            ("src/app.py, ", "empty"),
        ],
    )
    def test_an_unusable_line_is_a_named_validate_error(self, tmp_path, declared, why):
        path = _write_tasks(tmp_path, _task_md("TASK-001", TODO, f"**Touches:** {declared}\n"))
        (task,) = parse_tasks(path)
        assert task.touches is None
        assert task.touches_error and why in task.touches_error
        result = validate_task_fields([task])
        assert any(e.startswith("TASK-001: **Touches:**") for e in result.errors)


class TestThePreflightCheck:
    def test_task_022_is_blocked_before_any_run(self, tmp_path):
        _write_tasks(
            tmp_path,
            _task_md("TASK-022", TODO, "**Touches:** src/cli.py, pyproject.toml\n"),
        )
        check = _touches_check(tmp_path)
        assert check.status == "broken" and check.blocking
        assert "TASK-022" in check.detail and "pyproject.toml" in check.detail

    @pytest.mark.parametrize(
        "touches,surface",
        [
            (".github/workflows", ".github/workflows"),  # the harness dir itself
            (".github/workflows/ci.yml", ".github/workflows/ci.yml"),  # a file in it
            ("spec-runner.config.yaml", "spec-runner.config.yaml"),  # the policy itself
            ("scripts/verify.sh", "scripts/verify.sh"),  # harness_files
        ],
    )
    def test_a_definite_conflict_blocks(self, tmp_path, touches, surface):
        _write_tasks(tmp_path, _task_md("TASK-001", TODO, f"**Touches:** {touches}\n"))
        check = _touches_check(tmp_path, harness_files=["scripts/verify.sh"])
        assert check.status == "broken" and check.blocking, check.detail
        assert surface in check.detail

    @pytest.mark.parametrize(
        "touches,reached",
        [
            (".github/", ".github/workflows"),  # may edit only CODEOWNERS
            ("spec/", "spec/executor.config.yaml"),  # legacy config, v2 project
        ],
    )
    def test_a_directory_holding_harness_paths_is_not_a_verdict(self, tmp_path, touches, reached):
        """Review of this change: a declared directory that merely contains a
        harness path says nothing about whether the task writes it — not a
        blocker, and says so."""
        _write_tasks(tmp_path, _task_md("TASK-001", TODO, f"**Touches:** {touches}\n"))
        check = _touches_check(tmp_path)
        assert check.status == "unavailable" and not check.blocking, check.detail
        assert reached in check.detail

    def test_a_harness_dir_under_a_file_glob_is_not_a_verdict(self, tmp_path):
        """Review of this change: the guard matches `harness_allow` against
        concrete files; for a declared harness directory that depends on the
        names the task will write."""
        (tmp_path / ".github" / "workflows").mkdir(parents=True)
        _write_tasks(tmp_path, _task_md("TASK-001", TODO, "**Touches:** .github/workflows\n"))
        check = _touches_check(tmp_path, harness_allow=[".github/workflows/*.yml"])
        assert check.status == "unavailable" and not check.blocking, check.detail

    def test_a_file_under_the_glob_is_exempt(self, tmp_path):
        _write_tasks(
            tmp_path, _task_md("TASK-001", TODO, "**Touches:** .github/workflows/ci.yml\n")
        )
        check = _touches_check(tmp_path, harness_allow=[".github/workflows/*.yml"])
        assert check.status == "ok", check.detail

    def test_an_empty_allow_entry_is_refused_at_load(self, tmp_path):
        """Review of this change: `Path.match("")` raises — in preflight and
        in the guard's first comparison alike."""
        from spec_runner.config import ConfigError

        with pytest.raises(ConfigError, match="harness_allow has an empty entry"):
            _cfg(tmp_path, harness_allow="")

    def test_a_scope_clear_of_the_harness_is_ok(self, tmp_path):
        _write_tasks(tmp_path, _task_md("TASK-001", TODO, "**Touches:** src/, docs/guide.md\n"))
        check = _touches_check(tmp_path)
        assert check.status == "ok" and not check.blocking

    def test_harness_allow_exempts_ordinary_harness_files(self, tmp_path):
        _write_tasks(tmp_path, _task_md("TASK-001", TODO, "**Touches:** pyproject.toml, uv.lock\n"))
        check = _touches_check(tmp_path, harness_allow=["pyproject.toml", "uv.lock"])
        assert check.status == "ok"

    def test_harness_allow_never_exempts_the_config(self, tmp_path):
        _write_tasks(tmp_path, _task_md("TASK-001", TODO, "**Touches:** spec-runner.config.yaml\n"))
        check = _touches_check(tmp_path, harness_allow=["*"])
        assert check.status == "broken" and check.blocking

    def test_a_done_task_is_not_checked(self, tmp_path):
        _write_tasks(tmp_path, _task_md("TASK-001", DONE, "**Touches:** pyproject.toml\n"))
        check = _touches_check(tmp_path)
        assert check.status == "skipped" and not check.blocking

    def test_nothing_declared_is_skipped_not_guessed(self, tmp_path):
        """A task whose prose names pyproject.toml declares nothing."""
        _write_tasks(
            tmp_path,
            _task_md("TASK-001", TODO, "**Description:** do not touch pyproject.toml\n"),
        )
        check = _touches_check(tmp_path)
        assert check.status == "skipped" and not check.blocking

    @pytest.mark.parametrize("mode", ["warn", "off"])
    def test_outside_strict_nothing_would_refuse(self, tmp_path, mode):
        _write_tasks(tmp_path, _task_md("TASK-001", TODO, "**Touches:** pyproject.toml\n"))
        check = _touches_check(tmp_path, harness_guard=mode)
        assert check.status == "skipped" and not check.blocking

    def test_the_verdict_is_blocked(self, tmp_path):
        _write_tasks(tmp_path, _task_md("TASK-001", TODO, "**Touches:** pyproject.toml\n"))
        report = run_preflight(_cfg(tmp_path))
        assert "harness.touches" in [c.id for c in report.blockers]
