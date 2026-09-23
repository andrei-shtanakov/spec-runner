"""#338: repo-local stage profiles and external stages — the model."""

from pathlib import Path

import pytest

from spec_runner.config import ConfigError, ExecutorConfig
from spec_runner.spec import (
    LITE,
    ProfileError,
    ProfileGraphError,
    available_profiles,
    load_profile,
    stage_path,
)

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


def _write_profile(root: Path, name: str, text: str) -> None:
    d = root / "spec" / "profiles"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.yaml").write_text(text)


class TestRepoLocalLookup:
    def test_a_repo_local_profile_loads(self, tmp_path):
        _write_profile(tmp_path, "workstream", WORKSTREAM)
        profile = load_profile("workstream", tmp_path)
        assert profile.names() == ("decomposition", "tasks")
        dec = profile.get("decomposition")
        assert dec is not None and dec.external and dec.path.startswith("workstreams/")
        assert profile.get("tasks").upstream == ("decomposition",)

    def test_a_bundled_profile_still_loads_without_a_root(self):
        assert load_profile("lite") == LITE

    def test_a_local_name_shadowing_a_bundled_one_is_refused(self, tmp_path):
        _write_profile(tmp_path, "lite", WORKSTREAM)
        with pytest.raises(ProfileError, match="rename the local one"):
            load_profile("lite", tmp_path)

    def test_an_unknown_name_is_still_a_plain_value_error(self, tmp_path):
        with pytest.raises(ValueError, match="unknown stage profile"):
            load_profile("nope", tmp_path)

    def test_available_lists_both_sources(self, tmp_path):
        _write_profile(tmp_path, "workstream", WORKSTREAM)
        assert available_profiles(tmp_path) == ["lite", "workstream"]
        assert available_profiles() == ["lite"]


class TestStageFieldRules:
    @pytest.mark.parametrize(
        ("stage_yaml", "message"),
        [
            ("  - name: x\n    external: true\n    upstream: []\n", "must declare `path`"),
            (
                "  - name: x\n    external: true\n    path: a.md\n    template: t.md\n",
                "must not declare",
            ),
            (
                "  - name: x\n    template: t.md\n    marker_prefix: X\n    validator: tasks\n"
                "    path: a.md\n",
                "only on an external stage",
            ),
            (
                '  - name: x\n    external: true\n    path: "a/{nope}.md"\n',
                "unknown placeholder",
            ),
            ("  - name: x\n    external: true\n    path: /abs.md\n", "relative to the project"),
        ],
    )
    def test_refused(self, tmp_path, stage_yaml, message):
        _write_profile(tmp_path, "bad", "name: bad\nstages:\n" + stage_yaml)
        with pytest.raises(ProfileError, match=message):
            load_profile("bad", tmp_path)

    def test_a_non_mapping_profile_is_refused(self, tmp_path):
        _write_profile(tmp_path, "bad", "- just\n- a list\n")
        with pytest.raises(ProfileError, match="mapping"):
            load_profile("bad", tmp_path)


class TestGraphErrorsAreGraphErrors:
    def test_a_cycle_raises_the_graph_error_class(self, tmp_path):
        _write_profile(
            tmp_path,
            "cyc",
            "name: cyc\nstages:\n"
            "  - {name: a, template: t, marker_prefix: A, validator: tasks, upstream: [b]}\n"
            "  - {name: b, template: t, marker_prefix: B, validator: tasks, upstream: [a]}\n",
        )
        with pytest.raises(ProfileGraphError, match="cycle"):
            load_profile("cyc", tmp_path)

    def test_an_unknown_upstream_raises_the_graph_error_class(self, tmp_path):
        _write_profile(
            tmp_path,
            "dang",
            "name: dang\nstages:\n"
            "  - {name: a, template: t, marker_prefix: A, validator: tasks, upstream: [zz]}\n",
        )
        with pytest.raises(ProfileGraphError, match="unknown stage"):
            load_profile("dang", tmp_path)


def _cfg(root: Path, prefix: str = "ws-", **kw) -> ExecutorConfig:
    return ExecutorConfig(project_root=root, spec_prefix=prefix, spec_profile="workstream", **kw)


class TestPathResolution:
    def test_ws_and_prefix_are_substituted_against_the_project_root(self, tmp_path):
        _write_profile(tmp_path, "workstream", WORKSTREAM)
        cfg = _cfg(tmp_path)
        profile = cfg.resolve_spec_profile()
        assert (
            stage_path(cfg, "decomposition", profile)
            == (tmp_path / "workstreams/ws/spec/30-decomposition.md").resolve()
        )
        assert stage_path(cfg, "tasks", profile) == cfg.spec_dir / "ws-tasks.md"

    def test_a_managed_path_is_unchanged_under_lite(self, tmp_path):
        cfg = ExecutorConfig(project_root=tmp_path)
        assert stage_path(cfg, "tasks") == cfg.spec_dir / "tasks.md"

    def test_an_empty_prefix_with_a_placeholder_is_refused(self, tmp_path):
        _write_profile(tmp_path, "workstream", WORKSTREAM)
        with pytest.raises(ConfigError, match="requires --spec-prefix"):
            _cfg(tmp_path, prefix="").resolve_spec_profile()

    def test_a_path_escaping_the_project_is_refused(self, tmp_path):
        _write_profile(
            tmp_path,
            "workstream",
            WORKSTREAM.replace("workstreams/{ws}/spec/30-decomposition.md", "../outside.md"),
        )
        with pytest.raises(ConfigError, match="outside the project"):
            _cfg(tmp_path).resolve_spec_profile()

    def test_an_external_path_on_a_managed_file_is_refused(self, tmp_path):
        _write_profile(
            tmp_path,
            "workstream",
            WORKSTREAM.replace(
                "workstreams/{ws}/spec/30-decomposition.md", "spec/{prefix}tasks.md"
            ),
        )
        with pytest.raises(ConfigError, match="decomposition.*tasks|tasks.*decomposition"):
            _cfg(tmp_path).resolve_spec_profile()

    def test_a_symlink_onto_a_managed_file_is_a_collision(self, tmp_path):
        _write_profile(tmp_path, "workstream", WORKSTREAM)
        (tmp_path / "spec").mkdir(exist_ok=True)
        (tmp_path / "spec" / "ws-tasks.md").write_text("x\n")
        link = tmp_path / "workstreams" / "ws" / "spec" / "30-decomposition.md"
        link.parent.mkdir(parents=True)
        link.symlink_to(tmp_path / "spec" / "ws-tasks.md")
        with pytest.raises(ConfigError, match="same file"):
            _cfg(tmp_path).resolve_spec_profile()

    def test_a_change_folder_still_finds_the_profile_and_the_root_path(self, tmp_path):
        """`--change` excludes `--spec-prefix`, so the profile here has no
        placeholder; what is checked is where the profile and the path are
        looked up — the project root, never the change directory."""
        _write_profile(
            tmp_path,
            "workstream",
            WORKSTREAM.replace("workstreams/{ws}/spec/", "workstreams/fixed/spec/"),
        )
        cfg = ExecutorConfig(project_root=tmp_path, spec_profile="workstream", change_id="add-x")
        profile = cfg.resolve_spec_profile()
        assert (
            stage_path(cfg, "decomposition", profile)
            == (tmp_path / "workstreams/fixed/spec/30-decomposition.md").resolve()
        )
        assert stage_path(cfg, "tasks", profile).parent == cfg.spec_dir

    def test_graph_errors_are_reported_as_graph_errors(self, tmp_path):
        _write_profile(
            tmp_path,
            "workstream",
            "name: workstream\nstages:\n"
            "  - {name: a, template: t, marker_prefix: A, validator: tasks, upstream: [a]}\n",
        )
        with pytest.raises(ConfigError, match="cycle") as exc:
            _cfg(tmp_path).resolve_spec_profile()
        assert "unknown spec_profile" not in str(exc.value)
