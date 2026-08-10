"""Prompt templates come from the project, never from the current directory (#153).

`PROMPTS_DIR` was `Path("spec/prompts")` — a relative path, resolved against the
*process* CWD rather than `config.project_root`. Running spec-runner against
another project from a directory that happens to have `spec/prompts/` silently
used **that** directory's templates, with no diagnostic: a custom template
replaces the built-in prompt wholesale, and nothing in the log said which one
was chosen.

Not hypothetical — it substituted a result already: a test asserting that the
built-in prompt documents the new `TASK_BLOCKED` marker received this
repository's own template instead, because pytest runs from the repo root.

The CWD behaviour is not kept as a fallback. It only ever worked by accident,
and "accidentally correct" is exactly how the wrong project's template gets
picked up.
"""

from pathlib import Path

import pytest

from spec_runner.config import ExecutorConfig
from spec_runner.prompt import build_task_prompt, load_prompt_template
from spec_runner.task import Task

FOREIGN_MARKER = "TEMPLATE FROM A DIFFERENT PROJECT"
PROJECT_MARKER = "TEMPLATE FROM THE TARGET PROJECT"


def _task() -> Task:
    return Task(id="TASK-001", name="Demo", priority="p0", status="todo", estimate="1d")


def _cfg(root: Path, **overrides) -> ExecutorConfig:
    defaults: dict = {
        "project_root": root,
        "state_file": root / "state.db",
        "logs_dir": root / "logs",
        "create_git_branch": False,
        "auto_commit": False,
    }
    defaults.update(overrides)
    return ExecutorConfig(**defaults)


def _write_template(prompts_dir: Path, marker: str, name: str = "task.txt") -> Path:
    prompts_dir.mkdir(parents=True, exist_ok=True)
    p = prompts_dir / name
    p.write_text(f"{marker}\n{{{{TASK_ID}}}} {{{{TASK_NAME}}}}\n")
    return p


class TestResolvedAgainstProjectRoot:
    def test_prompts_dir_hangs_off_the_spec_dir(self, tmp_path):
        cfg = _cfg(tmp_path)
        assert cfg.prompts_dir == tmp_path / "spec" / "prompts"

    def test_spec_prefix_namespaces_it(self, tmp_path):
        cfg = _cfg(tmp_path, spec_prefix="phase2-")
        assert cfg.prompts_dir == tmp_path / "spec" / "phase2-prompts"

    def test_change_id_moves_it_into_the_change_dir(self, tmp_path):
        cfg = _cfg(tmp_path, change_id="dark-mode")
        assert cfg.prompts_dir == tmp_path / "spec" / "changes" / "dark-mode" / "prompts"


class TestForeignDirectoryIsNotConsulted:
    def test_running_from_another_project_uses_the_target_template(self, tmp_path, monkeypatch):
        """The regression itself: cwd has templates, the target project has its own."""
        foreign = tmp_path / "foreign"
        _write_template(foreign / "spec" / "prompts", FOREIGN_MARKER)
        monkeypatch.chdir(foreign)

        target = tmp_path / "target"
        _write_template(target / "spec" / "prompts", PROJECT_MARKER)

        prompt = build_task_prompt(_task(), _cfg(target))
        assert PROJECT_MARKER in prompt
        assert FOREIGN_MARKER not in prompt, "picked up the current directory's template"

    def test_cwd_templates_are_not_a_fallback(self, tmp_path, monkeypatch):
        """Target has no templates: the built-in prompt wins, not the cwd's."""
        foreign = tmp_path / "foreign"
        _write_template(foreign / "spec" / "prompts", FOREIGN_MARKER)
        monkeypatch.chdir(foreign)

        target = tmp_path / "target"
        (target / "spec").mkdir(parents=True)

        prompt = build_task_prompt(_task(), _cfg(target))
        assert FOREIGN_MARKER not in prompt
        assert "TASK_COMPLETE" in prompt, "expected the built-in prompt"

    def test_loader_without_a_directory_finds_nothing(self, tmp_path, monkeypatch):
        """No implicit search: no directory given means no project template."""
        _write_template(tmp_path / "spec" / "prompts", FOREIGN_MARKER)
        monkeypatch.chdir(tmp_path)
        assert load_prompt_template("task") is None


class TestPrecedence:
    def test_project_template_beats_the_builtin(self, tmp_path):
        root = tmp_path / "p"
        _write_template(root / "spec" / "prompts", PROJECT_MARKER)
        assert PROJECT_MARKER in build_task_prompt(_task(), _cfg(root))

    def test_cli_specific_template_beats_the_generic_one(self, tmp_path):
        d = tmp_path / "spec" / "prompts"
        _write_template(d, "GENERIC", name="review.md")
        _write_template(d, "CODEX-SPECIFIC", name="review.codex.md")
        text = load_prompt_template("review", cli_name="/usr/bin/codex", prompts_dir=d)
        assert text is not None and "CODEX-SPECIFIC" in text

    def test_missing_directory_is_not_an_error(self, tmp_path):
        assert load_prompt_template("task", prompts_dir=tmp_path / "nope") is None


class TestSourceIsRecorded:
    """Which template was used has to be visible: a custom one replaces the
    built-in prompt wholesale, so the answer changes what the agent was told."""

    @staticmethod
    def _captured(monkeypatch) -> list[tuple[str, dict]]:
        """Capture on the module logger, not via capsys.

        structlog's sink is global state other test modules initialise, so a
        stderr assertion passes alone and fails in a full-suite run — the same
        trap already documented in `test_run_reconciliation`.
        """
        import spec_runner.prompt as prompt_mod

        events: list[tuple[str, dict]] = []
        monkeypatch.setattr(
            prompt_mod.logger, "info", lambda event, **kw: events.append((event, kw))
        )
        return events

    def test_project_template_logs_its_path(self, tmp_path, monkeypatch):
        root = tmp_path / "p"
        path = _write_template(root / "spec" / "prompts", PROJECT_MARKER)
        events = self._captured(monkeypatch)

        build_task_prompt(_task(), _cfg(root))

        resolved = [kw for ev, kw in events if ev == "Prompt template resolved"]
        assert resolved and resolved[0]["template"] == "task"
        assert resolved[0]["source"] == str(path)

    def test_builtin_fallback_is_logged_too(self, tmp_path, monkeypatch):
        root = tmp_path / "p"
        (root / "spec").mkdir(parents=True)
        events = self._captured(monkeypatch)

        build_task_prompt(_task(), _cfg(root))

        resolved = [kw for ev, kw in events if ev == "Prompt template resolved"]
        assert resolved and resolved[0]["source"] == "built-in"


def test_module_level_prompts_dir_is_gone():
    """A module constant is what made the path CWD-relative in the first place;
    leaving it around invites the same defect back."""
    import spec_runner.prompt as prompt_mod

    assert not hasattr(prompt_mod, "PROMPTS_DIR")


@pytest.mark.parametrize("caller", ["review", "plan"])
def test_other_callers_also_resolve_from_the_project(caller):
    """`review` and `plan` load templates through the same loader; neither may
    keep an implicit CWD lookup."""
    import inspect

    from spec_runner import cli_plan, review

    src = inspect.getsource(review if caller == "review" else cli_plan)
    assert "load_prompt_template(" in src
    assert "prompts_dir=" in src, f"{caller} calls the loader without a directory"
