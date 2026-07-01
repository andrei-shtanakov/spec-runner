from pathlib import Path
from types import SimpleNamespace

from spec_runner import spec_commands
from spec_runner.spec import read_spec_meta
from tests.test_spec_commands import GOOD_REQ, _cfg  # reuse helpers


def test_adopt_invalid_file_becomes_draft(tmp_path: Path):
    cfg = _cfg(tmp_path)
    cfg.requirements_file.parent.mkdir(parents=True, exist_ok=True)
    cfg.requirements_file.write_text("no scope, unmanaged\n")
    args = SimpleNamespace(stage="requirements", force=False)
    spec_commands.cmd_spec_adopt(args, cfg)
    assert read_spec_meta(cfg.requirements_file).status == "draft"


def test_adopt_force_invalid_becomes_approved(tmp_path: Path):
    cfg = _cfg(tmp_path)
    cfg.requirements_file.parent.mkdir(parents=True, exist_ok=True)
    cfg.requirements_file.write_text("no scope, unmanaged\n")
    args = SimpleNamespace(stage="requirements", force=True)
    spec_commands.cmd_spec_adopt(args, cfg)
    assert read_spec_meta(cfg.requirements_file).status == "approved"


def test_adopt_valid_becomes_approved(tmp_path: Path):
    cfg = _cfg(tmp_path)
    cfg.requirements_file.parent.mkdir(parents=True, exist_ok=True)
    cfg.requirements_file.write_text(GOOD_REQ)
    args = SimpleNamespace(stage="requirements", force=False)
    spec_commands.cmd_spec_adopt(args, cfg)
    assert read_spec_meta(cfg.requirements_file).status == "approved"
