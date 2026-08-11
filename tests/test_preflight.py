"""`preflight` — read-only diagnostics: what is missing, what blocks (#142a).

There is no zero stage. On a greenfield repo the question "what do I need before
tasks can run" was answered by a task failing, and the answer arrived one failure
at a time. Worse, a gate that is green on an empty project proves nothing: an
empty suite exits 0 and so does a linter with no files, so the instrument has to
be examined before it is believed.

Scope decisions taken from the owner's decomposition of #142:

- **read-only.** `preflight` never writes. Diagnostics that quietly repair the
  tree cannot be part of a gate.
- **`bootstrap` is not here.** Creating `pyproject.toml`, a layout and a
  toolchain turns spec-runner into a project scaffolder — a separate product
  decision, not a natural extension of an executor.
- **no mutation probe.** Certifying the oracle by breaking something belongs in
  a disposable worktree, not in diagnostics of the working tree.
- **zero tests is never proof of health** — it is a blocker with its own status,
  not an `ok`.
"""

import json
from pathlib import Path

import pytest

from spec_runner.config import ExecutorConfig
from spec_runner.preflight import (
    PREFLIGHT_SCHEMA_VERSION,
    Check,
    preflight_to_dict,
    run_preflight,
)

SPEC = (
    "# Spec\n\n## M0\n\n### TASK-001: Demo\n"
    "🔴 P0 | ⬜ TODO | Est: 1d\n\n"
    "**Description:** x\n\n**Checklist:**\n- [ ] work\n\n"
    "**Traces to:** [REQ-1]\n**Depends on:** —\n"
)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / "spec").mkdir()
    (tmp_path / "spec" / "tasks.md").write_text(SPEC)
    (tmp_path / "logs").mkdir()
    return tmp_path


def _cfg(project: Path, **overrides) -> ExecutorConfig:
    defaults: dict = {
        "project_root": project,
        "state_file": project / "state.db",
        "logs_dir": project / "logs",
        "create_git_branch": False,
        "auto_commit": False,
    }
    defaults.update(overrides)
    return ExecutorConfig(**defaults)


def _by_id(report, check_id: str) -> Check:
    found = [c for c in report.checks if c.id == check_id]
    assert found, f"no check {check_id!r} in {[c.id for c in report.checks]}"
    return found[0]


class TestReadOnly:
    def test_nothing_is_written(self, project, monkeypatch):
        """A diagnostic that edits the tree cannot be trusted as a gate."""
        before = {p: p.stat().st_mtime_ns for p in project.rglob("*") if p.is_file()}
        run_preflight(_cfg(project))
        after = {p: p.stat().st_mtime_ns for p in project.rglob("*") if p.is_file()}
        assert set(after) == set(before), "preflight created or removed files"
        assert after == before, "preflight modified a file"


class TestMissingSpec:
    def test_absent_tasks_file_is_a_blocker(self, tmp_path):
        (tmp_path / "spec").mkdir()
        report = run_preflight(_cfg(tmp_path))
        check = _by_id(report, "spec.tasks")
        assert check.status == "missing"
        assert check.blocking
        assert report.verdict == "blocked"

    def test_unparseable_spec_is_broken_not_missing(self, project):
        (project / "spec" / "tasks.md").write_text("### TASK-001: Demo\nnot a meta line\n")
        check = _by_id(run_preflight(_cfg(project)), "spec.validation")
        assert check.status == "broken"
        assert check.blocking


class TestMissingTool:
    def test_absent_agent_cli_is_reported_missing(self, project, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda name: None)
        check = _by_id(run_preflight(_cfg(project)), "agent.cli")
        assert check.status == "missing"
        assert check.blocking
        assert "claude" in check.detail

    def test_absent_test_runner_is_reported_missing(self, project, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda name: None)
        check = _by_id(run_preflight(_cfg(project)), "tests.runner")
        assert check.status == "missing"

    def test_present_tool_is_ok(self, project, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
        assert _by_id(run_preflight(_cfg(project)), "agent.cli").status == "ok"


class TestEmptySuiteIsNotHealth:
    """`0 passed` and exit 0 is indistinguishable from "all good" — which is
    exactly why it cannot be reported as ok."""

    def test_empty_suite_is_its_own_status_and_blocks(self, project, monkeypatch):
        _fake_collect(monkeypatch, returncode=5, stdout="no tests ran\n")
        check = _by_id(run_preflight(_cfg(project)), "tests.suite")
        assert check.status == "empty"
        assert check.blocking

    def test_populated_suite_is_ok(self, project, monkeypatch):
        _fake_collect(monkeypatch, returncode=0, stdout="12 tests collected in 0.1s\n")
        check = _by_id(run_preflight(_cfg(project)), "tests.suite")
        assert check.status == "ok"
        assert not check.blocking

    def test_missing_test_path_is_empty_not_broken(self, project, monkeypatch):
        """On greenfield the configured `tests/` simply does not exist yet.
        pytest calls that a usage error (exit 4); for our purpose it is "there
        is no suite", and the raw reason is kept in the detail."""
        _fake_collect(
            monkeypatch, returncode=4, stdout="ERROR: file or directory not found: tests/\n"
        )
        check = _by_id(run_preflight(_cfg(project)), "tests.suite")
        assert check.status == "empty"
        assert check.blocking
        assert "tests/" in check.detail

    def test_collection_error_is_a_broken_oracle(self, project, monkeypatch):
        """Import errors during collection mean the gate cannot run at all —
        distinct from having no tests."""
        _fake_collect(monkeypatch, returncode=2, stderr="ImportError: no module\n")
        check = _by_id(run_preflight(_cfg(project)), "tests.suite")
        assert check.status == "broken"
        assert check.blocking

    def test_composite_command_is_unavailable_not_guessed(self, project, monkeypatch):
        """Same rule as #139: a shell chain is several programs and this must
        not guess which one collects tests."""
        cfg = _cfg(project, test_command="pin_check.py && uv run pytest -q && pyrefly check")
        check = _by_id(run_preflight(cfg), "tests.suite")
        assert check.status == "unavailable"
        assert not check.blocking
        assert "composite" in check.detail.lower()

    def test_non_pytest_command_is_unavailable(self, project):
        cfg = _cfg(project, test_command="cargo test")
        assert _by_id(run_preflight(cfg), "tests.suite").status == "unavailable"

    def test_tests_disabled_is_skipped_not_blocking(self, project):
        cfg = _cfg(project, run_tests_on_done=False)
        for check_id in ("tests.runner", "tests.suite"):
            check = _by_id(run_preflight(cfg), check_id)
            assert check.status == "skipped"
            assert not check.blocking


class TestGitUnavailable:
    def test_no_repo_is_unavailable_when_automation_is_off(self, project):
        check = _by_id(run_preflight(_cfg(project)), "git.repo")
        assert check.status in {"unavailable", "missing"}
        assert not check.blocking, "git is only required when automation needs it"

    def test_no_repo_blocks_when_automation_is_on(self, project):
        cfg = _cfg(project, create_git_branch=True, auto_commit=True)
        check = _by_id(run_preflight(cfg), "git.repo")
        assert check.blocking


class TestMachineOutput:
    def test_payload_is_versioned(self, project):
        payload = preflight_to_dict(run_preflight(_cfg(project)))
        assert payload["schema_version"] == PREFLIGHT_SCHEMA_VERSION

    def test_payload_matches_the_published_schema(self, project):
        schema = json.loads(Path("schemas/preflight-result.schema.json").read_text())
        payload = preflight_to_dict(run_preflight(_cfg(project)))
        for key in schema["required"]:
            assert key in payload, f"schema requires {key!r}"
        statuses = schema["$defs"]["check"]["properties"]["status"]["enum"]
        for check in payload["checks"]:
            assert check["status"] in statuses, check

    def test_blockers_are_listed_separately(self, tmp_path):
        (tmp_path / "spec").mkdir()
        payload = preflight_to_dict(run_preflight(_cfg(tmp_path)))
        assert payload["verdict"] == "blocked"
        assert "spec.tasks" in payload["blockers"]

    def test_every_check_carries_a_reason(self, project):
        for check in preflight_to_dict(run_preflight(_cfg(project)))["checks"]:
            assert check["detail"], f"{check['id']} says nothing about why"


class TestExitCodes:
    def test_ready_exits_zero(self, project, monkeypatch, capsys):
        from spec_runner.preflight import cmd_preflight

        monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
        _fake_collect(monkeypatch, returncode=0, stdout="3 tests collected\n")
        args = _args(json_output=False)
        cmd_preflight(args, _cfg(project))  # must not raise

    def test_blockers_exit_one(self, tmp_path, monkeypatch):
        from spec_runner.preflight import cmd_preflight

        (tmp_path / "spec").mkdir()
        with pytest.raises(SystemExit) as exc:
            cmd_preflight(_args(json_output=False), _cfg(tmp_path))
        assert exc.value.code == 1

    def test_json_mode_prints_exactly_one_document(self, tmp_path, capsys):
        from spec_runner.preflight import cmd_preflight

        (tmp_path / "spec").mkdir()
        with pytest.raises(SystemExit):
            cmd_preflight(_args(json_output=True), _cfg(tmp_path))
        out = capsys.readouterr().out
        payload = json.loads(out)  # the whole of stdout, or this raises
        assert payload["verdict"] == "blocked"
        assert payload["exit_code"] == 1


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _args(*, json_output: bool):
    import argparse

    return argparse.Namespace(json=json_output)


def _fake_collect(monkeypatch, *, returncode=0, stdout="", stderr=""):
    import subprocess

    from spec_runner import preflight as pf

    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(
        pf.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(
            args=["collect"], returncode=returncode, stdout=stdout, stderr=stderr
        ),
    )
