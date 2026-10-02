"""`task_context_files`: the workstream bundle reaches the task prompts.

The reported failure (#480 stage 4.4): TASK-002 ran three attempts against
prompts that said "See spec/<prefix>design.md" — a file that does not exist,
because a workstream's specification lives in `workstreams/<ws>/spec/`. The
last attempt ended `TASK_BLOCKED: the design/requirements documents … are
missing`. These tests pin that the declared files are listed, the sections a
task references are quoted in both the RED and the implementation prompt, and
a declared file that is missing refuses the run before anything is paid.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from spec_runner.config import ConfigError, ExecutorConfig
from spec_runner.prompt import build_red_prompt, build_task_prompt
from spec_runner.task import Task
from spec_runner.task_context import (
    referenced_ids,
    render_task_context,
    sections_by_id,
)

PREFIX = "ws-demo-"
BUNDLE = "workstreams/ws-demo/spec"

BEHAVIOUR = """# Behaviour spec

## Scenarios

### B. Record before spend

#### BEH-05: call-start is acknowledged before Popen

Given a paid call, the call-start row is written first.

```markdown
#### BEH-99: inside a fence, not a heading
```

#### BEH-06: no acknowledgement, no process

Refused before any spend, exit 2.

### C. Unrelated

#### BEH-40: something this task does not reference

Never quoted.
"""

DECOMPOSITION = """# Decomposition

#### DT-02: Run identity and the paid-call seam · type: implement

DEL-10: a new paid_call.py with execute().

#### DT-03: Checkpoint

Not this task.
"""


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _repo(tmp_path: Path, files: list[str] | None = None, **overrides: object) -> ExecutorConfig:
    root = tmp_path / "repo"
    bundle = root / BUNDLE
    bundle.mkdir(parents=True)
    (bundle / "15-behaviour-spec.md").write_text(BEHAVIOUR)
    (bundle / "30-decomposition.md").write_text(DECOMPOSITION)
    (root / "tests").mkdir()
    _git(tmp_path, "init", "-q", str(root))
    _git(root, "config", "user.email", "o@e.c")
    _git(root, "config", "user.name", "O")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    declared = (
        files
        if files is not None
        else [
            "workstreams/{ws}/spec/15-behaviour-spec.md",
            "workstreams/{ws}/spec/30-decomposition.md",
        ]
    )
    kwargs: dict = {
        "project_root": root,
        "state_file": root / ".state.db",
        "logs_dir": root / ".logs",
        "spec_prefix": PREFIX,
        "task_context_files": declared,
        "test_command": "python -m pytest",
        "lint_command": "",
    }
    kwargs.update(overrides)
    return ExecutorConfig(**kwargs)


def _task() -> Task:
    return Task(
        id="TASK-002",
        name="Run identity and the paid-call seam",
        priority="p2",
        status="todo",
        estimate="0.5d",
        description=(
            "Implement BEH-05, BEH-06.\nSource: workstreams/ws-demo/spec/30-decomposition.md#DT-02"
        ),
        checklist=[("implement BEH-05 per DEL-10", False)],
        traces_to=["FR-01"],
        depends_on=["TASK-001"],
    )


class TestTheReportedFailure:
    """Both prompts carry the files and the sections the task cites."""

    @pytest.mark.parametrize("build", [build_red_prompt, build_task_prompt])
    def test_prompt_lists_files_and_quotes_referenced_sections(self, tmp_path, build):
        prompt = build(_task(), _repo(tmp_path))

        assert "## Specification context" in prompt
        assert f"`{BUNDLE}/15-behaviour-spec.md`" in prompt
        assert f"`{BUNDLE}/30-decomposition.md`" in prompt
        assert "Given a paid call, the call-start row is written first." in prompt
        assert "Refused before any spend, exit 2." in prompt
        assert "DEL-10: a new paid_call.py with execute()." in prompt

    @pytest.mark.parametrize("build", [build_red_prompt, build_task_prompt])
    def test_unreferenced_sections_are_not_quoted(self, tmp_path, build):
        prompt = build(_task(), _repo(tmp_path))

        assert "Never quoted." not in prompt
        assert "Not this task." not in prompt

    @pytest.mark.parametrize("build", [build_red_prompt, build_task_prompt])
    def test_nothing_declared_adds_nothing(self, tmp_path, build):
        prompt = build(_task(), _repo(tmp_path, files=[]))

        assert "Specification context" not in prompt

    def test_a_custom_template_receives_it_as_a_variable(self, tmp_path):
        cfg = _repo(tmp_path)
        prompts = cfg.prompts_dir
        prompts.mkdir(parents=True, exist_ok=True)
        (prompts / "task.md").write_text("CTX>{{TASK_CONTEXT}}<CTX")

        prompt = build_task_prompt(_task(), cfg)

        assert "CTX>## Specification context" in prompt


class TestResolution:
    def test_placeholders_resolve_for_the_namespace(self, tmp_path):
        cfg = _repo(tmp_path)
        root = Path(cfg.project_root).resolve()

        assert cfg.resolve_task_context_files() == [
            root / BUNDLE / "15-behaviour-spec.md",
            root / BUNDLE / "30-decomposition.md",
        ]

    def test_placeholder_entries_skip_a_run_without_prefix(self, tmp_path):
        cfg = _repo(tmp_path, spec_prefix="")

        assert cfg.resolve_task_context_files() == []

    def test_plain_entries_apply_without_prefix(self, tmp_path):
        cfg = _repo(tmp_path, files=[f"{BUNDLE}/30-decomposition.md"], spec_prefix="")

        assert [p.name for p in cfg.resolve_task_context_files()] == ["30-decomposition.md"]

    def test_a_missing_file_is_refused(self, tmp_path):
        cfg = _repo(tmp_path, files=["workstreams/{ws}/spec/20-design.md"])

        with pytest.raises(ConfigError, match="20-design.md.*not a file"):
            cfg.resolve_task_context_files()

    def test_an_escape_from_the_project_is_refused(self, tmp_path):
        cfg = _repo(tmp_path, files=["../outside.md"])

        with pytest.raises(ConfigError, match="outside the project"):
            cfg.resolve_task_context_files()

    def test_an_unknown_placeholder_is_refused(self, tmp_path):
        cfg = _repo(tmp_path, files=["workstreams/{name}/spec/x.md"])

        with pytest.raises(ConfigError, match="unknown placeholder"):
            cfg.resolve_task_context_files()

    def test_a_single_string_is_one_entry(self, tmp_path):
        cfg = _repo(tmp_path, files=f"{BUNDLE}/30-decomposition.md")  # type: ignore[arg-type]

        assert cfg.task_context_files == [f"{BUNDLE}/30-decomposition.md"]

    def test_an_empty_entry_is_refused(self, tmp_path):
        with pytest.raises(ConfigError, match="task_context_files has an empty entry"):
            _repo(tmp_path, files=[" "])


class TestSections:
    def test_a_section_runs_to_the_next_heading_of_its_level(self):
        sections = sections_by_id(BEHAVIOUR)

        assert sections["BEH-05"].startswith("#### BEH-05:")
        assert "BEH-06" not in sections["BEH-05"]
        assert sections["BEH-06"].endswith("Refused before any spend, exit 2.")

    def test_a_fenced_heading_is_not_a_section(self):
        assert "BEH-99" not in sections_by_id(BEHAVIOUR)

    def test_referenced_ids_skip_task_ids_and_keep_order(self):
        assert referenced_ids(_task()) == ["BEH-05", "BEH-06", "DT-02", "DEL-10", "FR-01"]

    def test_what_does_not_fit_is_named(self, tmp_path, monkeypatch):
        from spec_runner import task_context

        monkeypatch.setattr(task_context, "MAX_QUOTED_CHARS", 80)
        text = render_task_context(_task(), _repo(tmp_path))

        assert "Not quoted for length" in text
        assert "BEH-05 (workstreams/ws-demo/spec/15-behaviour-spec.md)" in text


class TestStartup:
    def _write(self, root: Path, entry: str) -> None:
        (root / "spec-runner.config.yaml").write_text(
            f"executor:\n  task_context_files:\n  - {entry}\n"
        )

    def test_run_refuses_before_any_work(self, tmp_path, monkeypatch, capsys):
        from spec_runner import cli

        self._write(tmp_path, "workstreams/{ws}/spec/20-design.md")
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr("sys.argv", ["spec-runner", "run", "--spec-prefix", PREFIX])

        with pytest.raises(SystemExit) as exc:
            cli.main()

        assert "20-design.md" in str(exc.value.code)
        assert "not a file" in str(exc.value.code)

    def test_a_read_only_command_is_not_refused(self, tmp_path, monkeypatch):
        from spec_runner import cli

        self._write(tmp_path, "workstreams/{ws}/spec/20-design.md")
        (tmp_path / "spec").mkdir()
        (tmp_path / "spec" / f"{PREFIX}tasks.md").write_text("# Tasks\n")
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr("sys.argv", ["spec-runner", "status", "--spec-prefix", PREFIX])

        try:
            cli.main()
        except SystemExit as exc:
            assert "task_context_files" not in str(exc.code)
