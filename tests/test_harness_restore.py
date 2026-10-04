"""`restore_surface` undoes a refused step — and never writes outside the project.

The step being undone controlled the tree. A harness file it replaced with a
symlink, or a directory on the way to one, must not turn the undo into a
write or an unlink somewhere else.
"""

from pathlib import Path

import pytest

from spec_runner.config import ExecutorConfig
from spec_runner.harness import restore_surface, snapshot_contents

PYPROJECT = "[project]\nname = 'demo'\n"


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    (root / "pyproject.toml").write_text(PYPROJECT)
    (root / ".github" / "workflows").mkdir(parents=True)
    (root / ".github" / "workflows" / "ci.yml").write_text("on: push\n")
    return root


def _cfg(project: Path, **overrides) -> ExecutorConfig:
    return ExecutorConfig(project_root=project, harness_guard="strict", **overrides)


def test_a_modified_file_is_restored(project):
    cfg = _cfg(project)
    before = snapshot_contents(cfg)
    (project / "pyproject.toml").write_text("tampered\n")

    assert restore_surface(cfg, before) == []
    assert (project / "pyproject.toml").read_text() == PYPROJECT


def test_created_and_deleted_files_are_undone(project):
    cfg = _cfg(project)
    before = snapshot_contents(cfg)
    (project / "conftest.py").write_text("x = 1\n")
    (project / ".github" / "workflows" / "ci.yml").unlink()

    assert restore_surface(cfg, before) == []
    assert not (project / "conftest.py").exists()
    assert (project / ".github" / "workflows" / "ci.yml").read_text() == "on: push\n"


def test_an_allowed_edit_is_left_alone(project):
    cfg = _cfg(project, harness_allow=["pyproject.toml"])
    before = snapshot_contents(cfg)
    (project / "pyproject.toml").write_text("allowed\n")

    assert restore_surface(cfg, before) == []
    assert (project / "pyproject.toml").read_text() == "allowed\n"


def test_a_file_replaced_by_a_symlink_is_not_written_through(project, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("precious\n")
    cfg = _cfg(project)
    before = snapshot_contents(cfg)
    (project / "pyproject.toml").unlink()
    (project / "pyproject.toml").symlink_to(outside)

    assert restore_surface(cfg, before) == []
    assert outside.read_text() == "precious\n"
    assert not (project / "pyproject.toml").is_symlink()
    assert (project / "pyproject.toml").read_text() == PYPROJECT


def test_a_directory_replaced_by_a_symlink_is_not_followed(project, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "workflows").mkdir(parents=True)
    (elsewhere / "workflows" / "ci.yml").write_text("on: push\n")
    victim = elsewhere / "workflows" / "deploy.yml"
    cfg = _cfg(project)
    before = snapshot_contents(cfg)

    import shutil

    shutil.rmtree(project / ".github")
    (project / ".github").symlink_to(elsewhere, target_is_directory=True)
    victim.write_text("keep me\n")

    unrestored = restore_surface(cfg, before)

    assert victim.read_text() == "keep me\n", "the undo deleted a file outside the project"
    assert ".github/workflows/deploy.yml" in unrestored


def test_a_config_outside_the_root_is_never_written(project, tmp_path):
    """Its directories were not named by anything the step could not touch:
    the refusal stands, and the file is reported, not rewritten."""
    outside = tmp_path / "ops" / "spec-runner.config.yaml"
    outside.parent.mkdir()
    outside.write_text("harness_guard: strict\n")
    cfg = _cfg(project, config_path=outside)
    before = snapshot_contents(cfg)
    outside.write_text("harness_guard: off\n")

    unrestored = restore_surface(cfg, before)

    assert str(outside) in unrestored
    assert outside.read_text() == "harness_guard: off\n"
