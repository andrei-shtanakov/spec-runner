"""The launch scope reads the config the parent loaded (review of PR #596).

`paths.root` in the YAML moves `project_root` away from the directory the
config was read from. `LaunchScope.of` derived the config path from
`project_root`, so the flat-launch refinement rebuilt the parent's config from
a file that does not exist — a degraded copy of defaults — and the
reproducibility check then compared that copy with a child built the same
way. The two readers answer different questions and now each reads its own
file: the parent's rebuild reads what the parent loaded, the child simulation
reads what the child will find by its `--project-root`.
"""

from pathlib import Path

from spec_runner.cli import _build_parser
from spec_runner.config import ExecutorConfig, build_config, load_config_from_yaml
from spec_runner.mcp_launch import (
    Irreproducible,
    LaunchScope,
    child_argv,
    resolve_tool_config,
    simulate_child_config,
)

CONFIG = "spec-runner.config.yaml"


def _parent(tmp_path: Path, monkeypatch) -> ExecutorConfig:
    """A parent as `main()` builds it: `<tmp>/spec-runner.config.yaml`,
    carrying `paths.root: ./app`, loaded from the CWD and stamped."""
    (tmp_path / "app").mkdir()
    loaded = tmp_path / CONFIG
    loaded.write_text('paths:\n  root: "./app"\nreview_policy: required\n')
    monkeypatch.chdir(tmp_path)
    args = _build_parser().parse_args(["run"])
    parent = build_config(load_config_from_yaml(loaded), args, detect_subdir=False)
    parent.config_path = loaded.resolve()
    assert parent.project_root == (tmp_path / "app").resolve()
    return parent


class TestTheParentIsRebuiltFromItsOwnFile:
    def test_the_scope_names_the_loaded_file(self, tmp_path, monkeypatch):
        parent = _parent(tmp_path, monkeypatch)
        assert LaunchScope.of(parent).config_path == (tmp_path / CONFIG).resolve()

    def test_the_refinement_keeps_the_parents_policy(self, tmp_path, monkeypatch):
        rebuilt = resolve_tool_config(LaunchScope.of(_parent(tmp_path, monkeypatch)), "phase2-")
        assert rebuilt.review_policy == "required"
        assert rebuilt.spec_prefix == "phase2-"

    def test_without_a_stamp_the_root_is_used(self, tmp_path):
        """Configs built outside `main()` carry no stamp: the old lookup."""
        (tmp_path / CONFIG).write_text("max_retries: 7\n")
        scope = LaunchScope.of(ExecutorConfig(project_root=tmp_path))
        assert scope.config_path == tmp_path / CONFIG


class TestTheChildIsSimulatedFromWhatItWillFind:
    def test_a_child_that_would_miss_the_parents_file_is_refused(self, tmp_path, monkeypatch):
        """The child resolves its YAML by `--project-root` (`app/`), where
        there is none: it would run under `advisory`. Reading the parent's
        file here instead would certify a child that does not exist."""
        parent = _parent(tmp_path, monkeypatch)
        result = simulate_child_config(LaunchScope.of(parent), child_argv(parent, "TASK-001"))
        assert isinstance(result, Irreproducible)
        (diff,) = [d for d in result.diffs if d.field == "review_policy"]
        assert str((tmp_path / "app" / CONFIG).resolve()) in diff.reason
        assert str((tmp_path / CONFIG).resolve()) in diff.reason
        assert "vanished" not in diff.reason

    def test_a_child_that_finds_the_same_policy_proceeds(self, tmp_path, monkeypatch):
        parent = _parent(tmp_path, monkeypatch)
        (tmp_path / "app" / CONFIG).write_text("review_policy: required\n")
        result = simulate_child_config(LaunchScope.of(parent), child_argv(parent, "TASK-001"))
        assert not isinstance(result, Irreproducible), result


class TestTheProgrammaticEntryPoint:
    def test_build_config_stamps_the_file_it_read(self, tmp_path, monkeypatch):
        """`run_server(None)` builds its scope from `_build_config("")`; the
        refinement there must keep the YAML-only policy too."""
        from spec_runner.mcp_server import _build_config

        (tmp_path / "app").mkdir()
        (tmp_path / CONFIG).write_text('paths:\n  root: "./app"\nreview_policy: required\n')
        monkeypatch.chdir(tmp_path)
        config = _build_config("")
        assert config.config_path == (tmp_path / CONFIG).resolve()
        rebuilt = resolve_tool_config(LaunchScope.of(config), "p-")
        assert rebuilt.review_policy == "required"
