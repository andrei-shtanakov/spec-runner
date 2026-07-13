from pathlib import Path
from types import SimpleNamespace

from spec_runner import spec_commands
from spec_runner.config import ExecutorLock
from spec_runner.spec import SpecMeta, read_spec_meta, write_spec


def _cfg(tmp_path: Path):
    from spec_runner.spec import LITE

    spec = tmp_path / "spec"
    return SimpleNamespace(
        project_root=tmp_path,
        spec_prefix="",
        resolve_spec_profile=lambda: LITE,
        requirements_file=spec / "requirements.md",
        design_file=spec / "design.md",
        tasks_file=spec / "tasks.md",
        spec_lock_file=spec / ".spec.lock",
    )


GOOD_REQ = """# Requirements

## Out of Scope
- none

#### REQ-001: X
**Acceptance Criteria:**
GIVEN a WHEN b THEN c
"""


def test_approve_blocks_on_validation_fail(tmp_path: Path):
    cfg = _cfg(tmp_path)
    write_spec(cfg.requirements_file, SpecMeta("requirements", "draft"), "no scope here\n")
    args = SimpleNamespace(stage="requirements", force=False)
    rc = spec_commands.cmd_spec_approve(args, cfg)
    assert rc != 0
    assert read_spec_meta(cfg.requirements_file).status == "draft"


def test_approve_revalidates_ignoring_stale_cache_toctou(tmp_path: Path):
    cfg = _cfg(tmp_path)
    # Cached validation says pass, but the body is actually invalid now.
    write_spec(
        cfg.requirements_file,
        SpecMeta("requirements", "draft", validation="pass"),
        "no scope here\n",
    )
    args = SimpleNamespace(stage="requirements", force=False)
    rc = spec_commands.cmd_spec_approve(args, cfg)
    assert rc != 0  # re-validation caught it despite the stale 'pass'
    assert read_spec_meta(cfg.requirements_file).status == "draft"


def test_approve_succeeds_on_valid(tmp_path: Path):
    cfg = _cfg(tmp_path)
    write_spec(cfg.requirements_file, SpecMeta("requirements", "draft"), GOOD_REQ)
    args = SimpleNamespace(stage="requirements", force=False)
    rc = spec_commands.cmd_spec_approve(args, cfg)
    assert rc == 0
    m = read_spec_meta(cfg.requirements_file)
    assert m.status == "approved" and m.version == 2


def test_reject_returns_to_draft(tmp_path: Path):
    cfg = _cfg(tmp_path)
    write_spec(cfg.requirements_file, SpecMeta("requirements", "approved", version=3), GOOD_REQ)
    args = SimpleNamespace(stage="requirements")
    assert spec_commands.cmd_spec_reject(args, cfg) == 0
    assert read_spec_meta(cfg.requirements_file).status == "draft"


def test_reject_writes_under_lock_and_releases(tmp_path: Path):
    cfg = _cfg(tmp_path)
    write_spec(cfg.requirements_file, SpecMeta("requirements", "approved", version=3), GOOD_REQ)
    args = SimpleNamespace(stage="requirements")
    assert spec_commands.cmd_spec_reject(args, cfg) == 0
    # The command must release the lock after writing.
    fresh_lock = ExecutorLock(cfg.spec_lock_file)
    assert fresh_lock.acquire()
    fresh_lock.release()


def test_adopt_writes_under_lock_and_releases(tmp_path: Path):
    cfg = _cfg(tmp_path)
    cfg.requirements_file.parent.mkdir(parents=True, exist_ok=True)
    cfg.requirements_file.write_text(GOOD_REQ)
    args = SimpleNamespace(stage="requirements", force=False)
    assert spec_commands.cmd_spec_adopt(args, cfg) == 0
    fresh_lock = ExecutorLock(cfg.spec_lock_file)
    assert fresh_lock.acquire()
    fresh_lock.release()


def test_check_writes_under_lock_and_releases(tmp_path: Path):
    cfg = _cfg(tmp_path)
    write_spec(cfg.requirements_file, SpecMeta("requirements", "draft"), GOOD_REQ)
    args = SimpleNamespace(stage="requirements")
    spec_commands.cmd_spec_check(args, cfg)
    fresh_lock = ExecutorLock(cfg.spec_lock_file)
    assert fresh_lock.acquire()
    fresh_lock.release()


def test_menu_refuses_approve_when_validation_fails(tmp_path):
    cfg = _cfg(tmp_path)
    write_spec(cfg.requirements_file, SpecMeta("requirements", "draft"), "no scope\n")
    # User picks 'a' (approve) but validation fails -> menu returns to prompt; feed 's' next.
    answers = iter(["a", "s"])
    action = spec_commands.run_checkpoint_menu(
        "requirements", cfg, input_fn=lambda _: next(answers)
    )
    assert action == "stop"


def test_menu_approve_when_valid(tmp_path):
    cfg = _cfg(tmp_path)
    write_spec(cfg.requirements_file, SpecMeta("requirements", "draft"), GOOD_REQ)
    action = spec_commands.run_checkpoint_menu("requirements", cfg, input_fn=lambda _: "a")
    assert action == "approved"


def test_menu_edit_returns_edit(tmp_path):
    cfg = _cfg(tmp_path)
    write_spec(cfg.requirements_file, SpecMeta("requirements", "draft"), GOOD_REQ)
    action = spec_commands.run_checkpoint_menu("requirements", cfg, input_fn=lambda _: "e")
    assert action == "edit"


def test_menu_regenerate_returns_regenerate(tmp_path):
    cfg = _cfg(tmp_path)
    write_spec(cfg.requirements_file, SpecMeta("requirements", "draft"), GOOD_REQ)
    action = spec_commands.run_checkpoint_menu("requirements", cfg, input_fn=lambda _: "r")
    assert action == "regenerate"


def test_menu_abort_returns_abort(tmp_path):
    cfg = _cfg(tmp_path)
    write_spec(cfg.requirements_file, SpecMeta("requirements", "draft"), GOOD_REQ)
    action = spec_commands.run_checkpoint_menu("requirements", cfg, input_fn=lambda _: "q")
    assert action == "abort"
