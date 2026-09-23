"""#338: external stages — admission, refusals, the issue's observable."""

from pathlib import Path

import pytest

from spec_runner.config import ExecutorConfig
from spec_runner.spec import git_blob_hash, split_frontmatter

WORKSTREAM = """\
name: workstream
stages:
  - name: decomposition
    external: true
    path: "workstreams/{ws}/spec/30-decomposition.md"
    upstream: []
  - name: tasks
    template: tasks.template.md
    marker_prefix: SPEC_TASKS
    validator: tasks
    upstream: [decomposition]
"""

TASKS = """\
---
spec_stage: tasks
status: draft
version: 1
---
# Tasks

### TASK-001: demo
P1 | TODO   Est: 1h

**Traces to:** [DT-01]
"""


def _project(tmp_path: Path, decomposition: str | None) -> tuple[ExecutorConfig, Path]:
    (tmp_path / "spec" / "profiles").mkdir(parents=True)
    (tmp_path / "spec" / "profiles" / "workstream.yaml").write_text(WORKSTREAM)
    (tmp_path / "spec" / "ws-tasks.md").write_text(TASKS)
    ext = tmp_path / "workstreams" / "ws" / "spec" / "30-decomposition.md"
    if decomposition is not None:
        ext.parent.mkdir(parents=True)
        ext.write_text(decomposition)
    cfg = ExecutorConfig(project_root=tmp_path, spec_prefix="ws-", spec_profile="workstream")
    return cfg, ext


def _approve(cfg: ExecutorConfig, stage: str) -> int:
    from argparse import Namespace

    from spec_runner.spec_commands import cmd_spec_approve

    return cmd_spec_approve(Namespace(stage=stage), cfg)


APPROVED = "---\nspec_stage: decomposition\nstatus: approved\n---\n## DT-01\nbody\n"


class TestTheIssuesObservable:
    def test_approve_tasks_traces_and_pins_the_external_upstream(self, tmp_path):
        cfg, ext = _project(tmp_path, APPROVED)
        before = ext.read_bytes()
        assert _approve(cfg, "tasks") == 0
        meta, _ = split_frontmatter((tmp_path / "spec" / "ws-tasks.md").read_text())
        assert meta["traces_to"][0] == "decomposition"
        assert "design" not in meta["traces_to"]
        assert meta["upstream_hashes"] == {"decomposition": git_blob_hash(before)}
        assert ext.read_bytes() == before


class TestAdmission:
    @pytest.mark.parametrize(
        ("decomposition", "expect_rc", "needle"),
        [
            ("---\nspec_stage: decomposition\nstatus: draft\n---\nx\n", 1, "draft"),
            ("---\nstatus: true\n---\nx\n", 1, "True"),
            ("---\nspec_stage: decomposition\n---\nx\n", 0, "approved"),
            ("no frontmatter at all\n", 0, "approved"),
            ("---\nstatus: [unclosed\n---\nx\n", 1, "malformed"),
            ("---\n- a list\n---\nx\n", 1, "malformed"),
        ],
    )
    def test_the_admission_table(self, tmp_path, capsys, decomposition, expect_rc, needle):
        cfg, ext = _project(tmp_path, decomposition)
        before = ext.read_bytes()
        rc = _approve(cfg, "tasks")
        out = capsys.readouterr().out
        assert rc == expect_rc
        assert needle in out
        assert ext.read_bytes() == before

    def test_a_missing_external_file_is_refused_naming_the_path(self, tmp_path, capsys):
        cfg, ext = _project(tmp_path, None)
        assert _approve(cfg, "tasks") == 1
        assert "30-decomposition.md" in capsys.readouterr().out

    def test_an_external_path_that_is_a_directory_is_refused(self, tmp_path, capsys):
        cfg, ext = _project(tmp_path, None)
        ext.mkdir(parents=True)
        assert _approve(cfg, "tasks") == 1
        assert "not a file" in capsys.readouterr().out


class TestExternalTargetsAreRefused:
    @pytest.mark.parametrize("command", ["approve", "reject", "adopt", "check"])
    def test_every_spec_command_refuses_an_external_target(self, tmp_path, capsys, command):
        from argparse import Namespace

        import spec_runner.spec_commands as sc

        cfg, ext = _project(tmp_path, APPROVED)
        before = ext.read_bytes()
        handler = getattr(sc, f"cmd_spec_{command}")
        rc = handler(Namespace(stage="decomposition", force=False), cfg)
        assert rc == 1
        assert "is external" in capsys.readouterr().out
        assert ext.read_bytes() == before


class TestTheRestOfTheLifecycle:
    def test_status_shows_the_external_stage(self, tmp_path, capsys):
        from argparse import Namespace

        from spec_runner.spec_commands import cmd_spec_status

        cfg, _ = _project(tmp_path, "---\nstatus: draft\n---\nx\n")
        cmd_spec_status(Namespace(), cfg)
        out = capsys.readouterr().out
        assert "decomposition" in out and "external" in out and "status: draft" in out
        assert "next: waiting → decomposition" in out

    def test_next_stage_is_never_generate_for_an_external_stage(self, tmp_path):
        from spec_runner.spec import profile_metas, resolve_next_stage

        cfg, _ = _project(tmp_path, None)
        profile = cfg.resolve_spec_profile()
        assert resolve_next_stage(profile_metas(cfg, profile), profile) == (
            "waiting",
            "decomposition",
        )

    def test_the_stale_cascade_never_writes_into_an_external_file(self, tmp_path):
        """An external stage *downstream* of a managed one: approving the
        managed stage cascades `stale` to its dependents, and must skip the
        external file rather than stamp a status into it."""
        from spec_runner.config import ExecutorLock
        from spec_runner.spec import mark_downstream_stale

        cfg, _ = _project(tmp_path, APPROVED)
        (tmp_path / "spec" / "profiles" / "workstream.yaml").write_text(
            WORKSTREAM
            + "  - name: audit\n    external: true\n"
            + '    path: "workstreams/{ws}/spec/40-audit.md"\n    upstream: [tasks]\n'
        )
        audit = tmp_path / "workstreams" / "ws" / "spec" / "40-audit.md"
        audit.write_text("---\nspec_stage: audit\nstatus: approved\n---\nbody\n")
        before = audit.read_bytes()
        profile = cfg.resolve_spec_profile()
        mark_downstream_stale(cfg, "tasks", ExecutorLock(cfg.spec_lock_file), profile)
        assert audit.read_bytes() == before

    def test_plan_gated_refuses_an_external_target(self, tmp_path, capsys):
        from spec_runner.cli_plan import run_gated_stage

        cfg, ext = _project(tmp_path, APPROVED)
        assert run_gated_stage("decomposition", "d", cfg, invoke=_never) == 1
        assert "is external" in capsys.readouterr().out

    def test_plan_gated_gates_on_admission(self, tmp_path, capsys):
        from spec_runner.cli_plan import run_gated_stage

        cfg, ext = _project(tmp_path, "---\nstatus: draft\n---\nx\n")
        assert run_gated_stage("tasks", "d", cfg, invoke=_never) == 2
        assert "draft" in capsys.readouterr().out


def _never(*_a, **_k):
    raise AssertionError("no generation may run")


class TestCliStageNames:
    def test_a_profile_stage_name_is_accepted_by_the_cli(self, tmp_path, monkeypatch, capsys):
        from spec_runner.cli import main

        cfg, _ = _project(tmp_path, APPROVED)
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(
            "sys.argv",
            [
                "spec-runner",
                "spec",
                "approve",
                "decomposition",
                "--spec-prefix",
                "ws-",
                "--profile",
                "workstream",
            ],
        )
        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 1
        assert "is external" in capsys.readouterr().out

    def test_an_unknown_stage_names_the_profile_stages(self, tmp_path, monkeypatch):
        from spec_runner.cli import main

        _project(tmp_path, APPROVED)
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(
            "sys.argv",
            [
                "spec-runner",
                "spec",
                "approve",
                "design",
                "--spec-prefix",
                "ws-",
                "--profile",
                "workstream",
            ],
        )
        with pytest.raises(SystemExit) as exc:
            main()
        assert "decomposition" in str(exc.value) and "tasks" in str(exc.value)

    def test_lite_still_rejects_an_unknown_stage(self, tmp_path, monkeypatch):
        from spec_runner.cli import main

        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr("sys.argv", ["spec-runner", "spec", "approve", "decomposition"])
        with pytest.raises(SystemExit) as exc:
            main()
        assert "requirements" in str(exc.value)


class TestCrlfFrontmatter:
    def test_a_draft_saved_with_crlf_is_not_admitted(self, tmp_path, capsys):
        """Final review: `---\\r\\n` read as "no frontmatter" admitted a draft —
        the admission rule failing open."""
        cfg, ext = _project(tmp_path, None)
        ext.parent.mkdir(parents=True)
        ext.write_bytes(b"---\r\nspec_stage: decomposition\r\nstatus: draft\r\n---\r\nx\r\n")
        assert _approve(cfg, "tasks") == 1
        assert "draft" in capsys.readouterr().out
