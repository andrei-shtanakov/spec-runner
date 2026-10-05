"""Candidate-level surface snapshot and strict trust rules (spec §2)."""

from pathlib import Path

from spec_runner.config import ExecutorConfig
from spec_runner.harness import (
    content_hashes,
    harness_violations,
    surface_snapshot,
    trust_refusal,
)
from spec_runner.state import StoredBaseline


def _cfg(root: Path, **kw) -> ExecutorConfig:
    return ExecutorConfig(project_root=root, harness_guard="strict", **kw)


def _stored(surface, files, provenance="initial") -> StoredBaseline:
    return StoredBaseline(provenance, "strict", surface, files, "t")


def test_candidates_carry_file_dir_absent(tmp_path):
    (tmp_path / "pyproject.toml").write_text("x")
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    (tmp_path / ".github" / "workflows" / "ci.yml").write_text("on: push")
    surface, files = surface_snapshot(_cfg(tmp_path))
    assert surface["pyproject.toml"] == "file"
    assert surface[".github/workflows"] == "dir"
    assert surface["Makefile"] == "absent"
    assert files[".github/workflows/ci.yml"] == b"on: push"


def test_new_file_in_recorded_dir_is_created_not_unknown(tmp_path):
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    cfg = _cfg(tmp_path)
    surface, files = surface_snapshot(cfg)
    (tmp_path / ".github" / "workflows" / "deploy.yml").write_text("x")
    assert trust_refusal(cfg, _stored(surface, files), started=True) is None
    assert "created .github/workflows/deploy.yml" in harness_violations(cfg, content_hashes(files))


def test_absent_dir_created_later_is_created(tmp_path):
    cfg = _cfg(tmp_path)
    surface, files = surface_snapshot(cfg)
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    (tmp_path / ".github" / "workflows" / "ci.yml").write_text("x")
    assert trust_refusal(cfg, _stored(surface, files), started=True) is None
    assert "created .github/workflows/ci.yml" in harness_violations(cfg, content_hashes(files))


def test_unknown_candidate_is_refused(tmp_path):
    cfg = _cfg(tmp_path)
    surface, files = surface_snapshot(cfg)
    grown = _cfg(tmp_path, harness_files=["tools/ci"])
    assert "tools/ci" in trust_refusal(grown, _stored(surface, files), started=True)


def test_unreadable_is_refused(tmp_path):
    cfg = _cfg(tmp_path)
    surface, _ = surface_snapshot(cfg)
    stored = _stored(surface, {"pyproject.toml": None})
    assert "unreadable" in trust_refusal(cfg, stored, started=True)


def test_recaptured_and_missing_are_refused(tmp_path):
    cfg = _cfg(tmp_path)
    surface, files = surface_snapshot(cfg)
    assert "harness trust" in trust_refusal(
        cfg, _stored(surface, files, "recaptured"), started=True
    )
    assert "harness trust" in trust_refusal(cfg, None, started=True)


def test_not_started_without_baseline_is_not_refused(tmp_path):
    assert trust_refusal(_cfg(tmp_path), None, started=False) is None


def test_initial_and_operator_are_trusted(tmp_path):
    cfg = _cfg(tmp_path)
    surface, files = surface_snapshot(cfg)
    for provenance in ("initial", "operator"):
        assert trust_refusal(cfg, _stored(surface, files, provenance), started=True) is None
