from pathlib import Path

from spec_runner.config import ExecutorLock
from spec_runner.spec import SpecMeta, read_spec_meta, write_spec


def test_write_under_lock_serializes(tmp_path: Path):
    p = tmp_path / "requirements.md"
    lock = ExecutorLock(tmp_path / ".spec.lock")
    write_spec(p, SpecMeta(spec_stage="requirements", version=1), "a\n", lock=lock)
    # Lock must be released after write (re-acquire succeeds).
    assert lock.acquire() is True
    lock.release()
    assert read_spec_meta(p).version == 1
