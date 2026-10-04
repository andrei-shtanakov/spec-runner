# Retry continues from WIP — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A retry (in-run or a separate `spec-runner retry`) continues from the previous attempt's uncommitted work, saved as a WIP commit before every destructive tree switch, while the harness guard judges against a persisted baseline taken before the first attempt.

**Architecture:** Four new state tables (`task_workspaces`, `harness_baselines`, `harness_baseline_files`, `harness_trust_audit`) behind `ExecutorState` methods. `harness.py` gains a surface snapshot with candidate-level states and trust rules; `HarnessBaseline` becomes a reader/writer of the persisted row. A new `wip.py` owns "save WIP or refuse" and is called before the rescue at both destructive points (`pre_start_hook` branch stage, `integration_pr` run start). A new `harness_cmd.py` holds the audited `harness trust` operator command.

**Tech Stack:** Python 3.11+, SQLite (stdlib `sqlite3`), git CLI via `subprocess`, pytest, uv.

**Spec:** `docs/superpowers/specs/2026-10-04-retry-continues-from-wip-design.md`

## Global Constraints

- Version: `4.5.0` → `5.0.0` (`pyproject.toml`), CHANGELOG `## [5.0.0]` with a migration section.
- `create_git_branch: false`: tree untouched, no WIP; persisted baseline still applies.
- No config key for the retry mode (no `retry_from`).
- WIP commit subject `wip(<TASK>): unfinished work of attempt <N> — not a candidate`; trailers `Spec-Runner-WIP: <TASK>` and `Spec-Runner-WIP-Attempt: <N>`; `N` = recorded attempt count at commit time.
- Refusal kinds: missing/untrusted harness state, started task without workspace under `strict`, trust ownership mismatch → `RefusalKind.POLICY` (exit 1). DB read/write failure, WIP stage/commit failure → `RefusalKind.INSTRUMENT` (exit 2).
- Nothing destructive (`git stash`, `git checkout <main>`, `checkout -- .`, `clean -fd`, integration fork) runs after a refusal.
- Ruff line length 100; mypy clean; `uv run pytest`.
- Every test that needs an agent replaces only `paid_call._spawn` (never `_run_agent_process` when the property is about ordering or commits).

**Spec deviations found while mapping the code (owner to confirm at plan review):**

1. `spec-runner reset` (`cli_info.cmd_reset`) deletes the whole state DB file. "`reset` keeps workspace and baseline" cannot hold without changing `reset`. This plan keeps `reset` as is; after a reset a task whose branch exists is "started", so under `strict` it is refused until `harness trust --bind-branch`. Trust never appears silently.
2. `remedy.abandon` is three separate commits. The workspace/baseline/file deletion is its own single `_immediate` transaction at the end of `abandon`, not atomic with abandon's other writes.
3. No golden fixture and not `schemas/executor-state.schema.json` (legacy JSON-state schema) lists DB tables. The contract is pinned by `docs/state-schema.md` plus a test asserting the four tables exist.

## Review Focus

1. A task branch checked out by hand with uncommitted operator edits and a matching workspace row → those edits become a WIP commit attributed to the agent's attempt; reasonable expectation: they are preserved, labelled WIP, never lost (pinned in Task 5 `test_operator_dirt_on_task_branch_is_preserved_as_wip`).
2. A file deleted by the failed attempt → WIP must record the deletion (`git add -A -- <path>`), not silently resurrect it (Task 4 `test_wip_records_a_deletion`).
3. A path with spaces or a leading dash in the dirty set → staging must treat it literally (Task 4 `test_wip_path_with_space_and_dash`).
4. Detached HEAD at a destructive point → no owner can be proven; must fall back to the rescue stash, never WIP (Task 4 `test_detached_head_falls_back_to_stash`).
5. Two namespaces sharing one DB with the same task id → a workspace row in namespace A must not authorise WIP in namespace B (Task 4 `test_other_namespace_row_is_not_ownership`).

---

### Task 1: State tables and methods

**Files:**
- Modify: `src/spec_runner/state.py` (tables in `_init_db` after `verify_evidence`, ~:689; methods near `complete_with_release` ~:1991)
- Test: `tests/test_task_workspace_state.py`

**Interfaces:**
- Produces:
  - `StoredBaseline` dataclass (frozen): `provenance: str`, `guard_mode: str`, `surface: dict[str, str]`, `files: dict[str, bytes | None]`, `captured_at: str`
  - `ExecutorState.get_workspace(namespace: str, task_id: str) -> dict | None` → keys `branch` (str | None), `bound_by`, `started_at`, `run_id`
  - `ExecutorState.workspace_for_branch(namespace: str, branch: str) -> str | None` (task_id)
  - `ExecutorState.record_workspace(namespace, task_id, *, branch: str | None, run_id: str | None, bound_by: str = "run") -> None` (insert only if absent)
  - `ExecutorState.get_harness_baseline(namespace, task_id) -> StoredBaseline | None`
  - `ExecutorState.store_harness_baseline(namespace, task_id, *, provenance, guard_mode, surface, files, run_id) -> None` (replaces, one transaction)
  - `ExecutorState.trust_harness(namespace, task_id, *, bind: bool, bind_branch: str | None, branch: str | None, surface, files, guard_mode, actor, reason, run_id) -> str | None` (returns replaced provenance; when `bind`, inserts the workspace row with `branch=bind_branch` (may be NULL) and `bound_by='operator'`; binding + snapshot + audit in one transaction)
  - `ExecutorState.harness_trust_audit(namespace: str, task_id: str) -> list[dict]`
  - `ExecutorState.forget_task_workspace(namespace, task_id) -> None` (workspace + baseline + files, one transaction; audit kept)
  - All writes raise on failure (no degraded fallback).

- [ ] **Step 1: Write the failing tests**

```python
"""State surface for retry-from-WIP (spec §2): four tables, atomic writes."""

import sqlite3
from pathlib import Path

import pytest

from spec_runner.config import ExecutorConfig
from spec_runner.state import ExecutorState, StoredBaseline

NS = "ns-a"


def _cfg(tmp_path: Path) -> ExecutorConfig:
    return ExecutorConfig(project_root=tmp_path, state_file=tmp_path / "state.db")


def test_the_four_tables_exist(tmp_path):
    with ExecutorState(_cfg(tmp_path)):
        pass
    conn = sqlite3.connect(tmp_path / "state.db")
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {
        "task_workspaces",
        "harness_baselines",
        "harness_baseline_files",
        "harness_trust_audit",
    } <= names


def test_workspace_is_recorded_once_and_found_by_exact_branch(tmp_path):
    with ExecutorState(_cfg(tmp_path)) as st:
        st.record_workspace(NS, "TASK-1", branch="task/task-1-x", run_id="r1")
        st.record_workspace(NS, "TASK-1", branch="task/other", run_id="r2")
        row = st.get_workspace(NS, "TASK-1")
        assert row["branch"] == "task/task-1-x" and row["bound_by"] == "run"
        assert st.workspace_for_branch(NS, "task/task-1-x") == "TASK-1"
        assert st.workspace_for_branch("ns-b", "task/task-1-x") is None
        assert st.workspace_for_branch(NS, "task/task-1") is None


def test_baseline_round_trips_bytes_and_states(tmp_path):
    surface = {"pyproject.toml": "file", ".github/workflows": "dir", "Makefile": "absent"}
    files = {"pyproject.toml": b"[project]\n", ".github/workflows/ci.yml": None}
    with ExecutorState(_cfg(tmp_path)) as st:
        st.store_harness_baseline(
            NS, "TASK-1", provenance="initial", guard_mode="strict",
            surface=surface, files=files, run_id="r1",
        )
    with ExecutorState(_cfg(tmp_path)) as st:
        got = st.get_harness_baseline(NS, "TASK-1")
    assert isinstance(got, StoredBaseline)
    assert got.provenance == "initial" and got.surface == surface and got.files == files


def test_trust_is_atomic(tmp_path, monkeypatch):
    with ExecutorState(_cfg(tmp_path)) as st:
        real = st._conn.execute

        def failing(sql, *a):
            if sql.lstrip().startswith("INSERT INTO harness_trust_audit"):
                raise sqlite3.OperationalError("disk I/O error")
            return real(sql, *a)

        monkeypatch.setattr(st, "_conn", _Proxy(st._conn, failing))
        with pytest.raises(sqlite3.OperationalError):
            st.trust_harness(
                NS, "TASK-1", bind=True, bind_branch="task/task-1", branch="task/task-1",
                surface={}, files={}, guard_mode="strict", actor="op", reason="checked",
                run_id=None,
            )
    with ExecutorState(_cfg(tmp_path)) as st:
        assert st.get_workspace(NS, "TASK-1") is None
        assert st.get_harness_baseline(NS, "TASK-1") is None
        assert st.harness_trust_audit(NS, "TASK-1") == []


def test_trust_replacement_is_audited(tmp_path):
    with ExecutorState(_cfg(tmp_path)) as st:
        st.store_harness_baseline(
            NS, "TASK-1", provenance="recaptured", guard_mode="warn",
            surface={}, files={}, run_id=None,
        )
        replaced = st.trust_harness(
            NS, "TASK-1", bind=False, bind_branch=None, branch=None, surface={}, files={},
            guard_mode="strict", actor="op", reason="restored and checked", run_id=None,
        )
        assert replaced == "recaptured"
        audit = st.harness_trust_audit(NS, "TASK-1")
        assert audit[-1]["replaced_provenance"] == "recaptured"
        assert st.get_harness_baseline(NS, "TASK-1").provenance == "operator"


def test_forget_keeps_the_audit(tmp_path):
    with ExecutorState(_cfg(tmp_path)) as st:
        st.trust_harness(
            NS, "TASK-1", bind=True, bind_branch="b", branch="b",
            surface={"pyproject.toml": "file"}, files={"pyproject.toml": b"x"},
            guard_mode="strict", actor="op", reason="r", run_id=None,
        )
        st.forget_task_workspace(NS, "TASK-1")
        assert st.get_workspace(NS, "TASK-1") is None
        assert st.get_harness_baseline(NS, "TASK-1") is None
        assert len(st.harness_trust_audit(NS, "TASK-1")) == 1


class _Proxy:
    """Delegates to a sqlite3 connection but routes `execute` through `fn`."""

    def __init__(self, conn, fn):
        self._conn, self._fn = conn, fn

    def execute(self, sql, *a):
        return self._fn(sql, *a)

    def __getattr__(self, name):
        return getattr(self._conn, name)
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_task_workspace_state.py -v`
Expected: FAIL — `ImportError: cannot import name 'StoredBaseline'`.

- [ ] **Step 3: Implement**

In `_init_db`, before the final `self._conn.commit()`:

```python
        # Retry-from-WIP (spec 2026-10-04 §2). `task_workspaces`: the task
        # started, and on which exact branch (NULL without per-task branches).
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS task_workspaces (
                namespace TEXT NOT NULL,
                task_id TEXT NOT NULL,
                branch TEXT,
                started_at TEXT NOT NULL,
                run_id TEXT,
                bound_by TEXT NOT NULL,
                PRIMARY KEY (namespace, task_id),
                CHECK (bound_by IN ('run', 'operator'))
            )
        """)
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS harness_baselines (
                namespace TEXT NOT NULL,
                task_id TEXT NOT NULL,
                captured_at TEXT NOT NULL,
                run_id TEXT,
                guard_mode TEXT NOT NULL,
                provenance TEXT NOT NULL,
                surface TEXT NOT NULL,
                PRIMARY KEY (namespace, task_id),
                CHECK (provenance IN ('initial', 'operator', 'recaptured'))
            )
        """)
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS harness_baseline_files (
                namespace TEXT NOT NULL,
                task_id TEXT NOT NULL,
                path TEXT NOT NULL,
                state TEXT NOT NULL,
                digest TEXT,
                content BLOB,
                PRIMARY KEY (namespace, task_id, path),
                CHECK (state IN ('present', 'unreadable'))
            )
        """)
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS harness_trust_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                namespace TEXT NOT NULL,
                task_id TEXT NOT NULL,
                at TEXT NOT NULL,
                actor TEXT NOT NULL,
                reason TEXT NOT NULL,
                branch TEXT,
                bound_branch INTEGER NOT NULL,
                replaced_provenance TEXT
            )
        """)
```

Module-level dataclass next to `RetryContext`:

```python
@dataclass(frozen=True)
class StoredBaseline:
    """A persisted harness baseline (spec 2026-10-04 §2)."""

    provenance: str
    guard_mode: str
    surface: dict[str, str]
    files: dict[str, bytes | None]
    captured_at: str
```

Methods (after `complete_with_release`); `json`, `hashlib`, `datetime` imports at the top of `state.py` if missing:

```python
    # === Task workspaces and harness baselines (spec 2026-10-04) ===

    def get_workspace(self, namespace: str, task_id: str) -> dict | None:
        """The started-task record, or None."""
        assert self._conn is not None
        row = self._conn.execute(
            "SELECT branch, bound_by, started_at, run_id FROM task_workspaces "
            "WHERE namespace = ? AND task_id = ?",
            (namespace, task_id),
        ).fetchone()
        if row is None:
            return None
        return {"branch": row[0], "bound_by": row[1], "started_at": row[2], "run_id": row[3]}

    def workspace_for_branch(self, namespace: str, branch: str) -> str | None:
        """The task whose recorded branch is exactly `branch` in `namespace`."""
        assert self._conn is not None
        row = self._conn.execute(
            "SELECT task_id FROM task_workspaces WHERE namespace = ? AND branch = ?",
            (namespace, branch),
        ).fetchone()
        return row[0] if row else None

    def record_workspace(
        self,
        namespace: str,
        task_id: str,
        *,
        branch: str | None,
        run_id: str | None,
        bound_by: str = "run",
    ) -> None:
        """Record that the task started; an existing record is kept."""
        assert self._conn is not None
        with self._immediate():
            self._conn.execute(
                "INSERT OR IGNORE INTO task_workspaces "
                "(namespace, task_id, branch, started_at, run_id, bound_by) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (namespace, task_id, branch, datetime.now().isoformat(), run_id, bound_by),
            )

    def get_harness_baseline(self, namespace: str, task_id: str) -> StoredBaseline | None:
        """The persisted baseline, or None."""
        assert self._conn is not None
        meta = self._conn.execute(
            "SELECT provenance, guard_mode, surface, captured_at FROM harness_baselines "
            "WHERE namespace = ? AND task_id = ?",
            (namespace, task_id),
        ).fetchone()
        if meta is None:
            return None
        files: dict[str, bytes | None] = {}
        for path, state, content in self._conn.execute(
            "SELECT path, state, content FROM harness_baseline_files "
            "WHERE namespace = ? AND task_id = ?",
            (namespace, task_id),
        ):
            files[path] = bytes(content) if state == "present" else None
        return StoredBaseline(
            provenance=meta[0],
            guard_mode=meta[1],
            surface=json.loads(meta[2]),
            files=files,
            captured_at=meta[3],
        )

    def _write_baseline(
        self,
        namespace: str,
        task_id: str,
        *,
        provenance: str,
        guard_mode: str,
        surface: dict[str, str],
        files: dict[str, bytes | None],
        run_id: str | None,
    ) -> None:
        """Replace the baseline rows; the caller holds the transaction."""
        assert self._conn is not None
        self._conn.execute(
            "DELETE FROM harness_baseline_files WHERE namespace = ? AND task_id = ?",
            (namespace, task_id),
        )
        self._conn.execute(
            "INSERT OR REPLACE INTO harness_baselines "
            "(namespace, task_id, captured_at, run_id, guard_mode, provenance, surface) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                namespace, task_id, datetime.now().isoformat(), run_id,
                guard_mode, provenance, json.dumps(surface, sort_keys=True),
            ),
        )
        for path, data in sorted(files.items()):
            self._conn.execute(
                "INSERT INTO harness_baseline_files "
                "(namespace, task_id, path, state, digest, content) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    namespace, task_id, path,
                    "unreadable" if data is None else "present",
                    None if data is None else hashlib.sha256(data).hexdigest(),
                    data,
                ),
            )

    def store_harness_baseline(
        self,
        namespace: str,
        task_id: str,
        *,
        provenance: str,
        guard_mode: str,
        surface: dict[str, str],
        files: dict[str, bytes | None],
        run_id: str | None,
    ) -> None:
        """Write a baseline in one transaction (replacing any)."""
        with self._immediate():
            self._write_baseline(
                namespace, task_id, provenance=provenance, guard_mode=guard_mode,
                surface=surface, files=files, run_id=run_id,
            )

    def trust_harness(
        self,
        namespace: str,
        task_id: str,
        *,
        bind: bool,
        bind_branch: str | None,
        branch: str | None,
        surface: dict[str, str],
        files: dict[str, bytes | None],
        guard_mode: str,
        actor: str,
        reason: str,
        run_id: str | None,
    ) -> str | None:
        """Binding (if asked) + operator snapshot + audit, all or nothing.

        Returns the provenance of the snapshot it replaced, if any.
        """
        assert self._conn is not None
        with self._immediate():
            prior = self._conn.execute(
                "SELECT provenance FROM harness_baselines WHERE namespace = ? AND task_id = ?",
                (namespace, task_id),
            ).fetchone()
            if bind:
                self._conn.execute(
                    "INSERT INTO task_workspaces "
                    "(namespace, task_id, branch, started_at, run_id, bound_by) "
                    "VALUES (?, ?, ?, ?, ?, 'operator')",
                    (namespace, task_id, bind_branch, datetime.now().isoformat(), run_id),
                )
            self._write_baseline(
                namespace, task_id, provenance="operator", guard_mode=guard_mode,
                surface=surface, files=files, run_id=run_id,
            )
            self._conn.execute(
                "INSERT INTO harness_trust_audit "
                "(namespace, task_id, at, actor, reason, branch, bound_branch, "
                "replaced_provenance) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    namespace, task_id, datetime.now().isoformat(), actor, reason,
                    branch, 1 if bind else 0,
                    prior[0] if prior else None,
                ),
            )
        return prior[0] if prior else None

    def harness_trust_audit(self, namespace: str, task_id: str) -> list[dict]:
        """Every `harness trust` event for the task, oldest first."""
        assert self._conn is not None
        rows = self._conn.execute(
            "SELECT at, actor, reason, branch, bound_branch, replaced_provenance "
            "FROM harness_trust_audit WHERE namespace = ? AND task_id = ? ORDER BY id",
            (namespace, task_id),
        ).fetchall()
        keys = ("at", "actor", "reason", "branch", "bound_branch", "replaced_provenance")
        return [dict(zip(keys, r, strict=True)) for r in rows]

    def forget_task_workspace(self, namespace: str, task_id: str) -> None:
        """Drop workspace, baseline and file rows together; keep the audit."""
        assert self._conn is not None
        with self._immediate():
            for table in ("harness_baseline_files", "harness_baselines", "task_workspaces"):
                self._conn.execute(
                    f"DELETE FROM {table} WHERE namespace = ? AND task_id = ?",
                    (namespace, task_id),
                )
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_task_workspace_state.py tests/test_state.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/spec_runner/state.py tests/test_task_workspace_state.py
git commit -m "feat(state): task_workspaces, harness_baselines(+files), harness_trust_audit"
```

---

### Task 2: Surface snapshot with candidate states and trust rules

**Files:**
- Modify: `src/spec_runner/harness.py`
- Test: `tests/test_harness_surface_snapshot.py`

**Interfaces:**
- Consumes: `StoredBaseline` (Task 1).
- Produces:
  - `surface_snapshot(config) -> tuple[dict[str, str], dict[str, bytes | None]]` — `(surface, files)`; surface maps every candidate key (same keying as `_surface_key`, candidates = `HARNESS_CANDIDATES` + control-plane keys + `harness_files`) to `"file" | "dir" | "absent"`; files maps every file under the surface to bytes or None (unreadable).
  - `trust_refusal(config, stored: StoredBaseline | None, *, started: bool) -> str | None` — the strict-mode refusal text, or None. Only evaluated when `config.harness_guard == "strict"`.
  - `TRUST_REMEDY` constant (the operator instruction, used in refusal texts).

- [ ] **Step 1: Write the failing tests**

```python
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
    assert "created .github/workflows/deploy.yml" in harness_violations(
        cfg, content_hashes(files)
    )


def test_absent_dir_created_later_is_created(tmp_path):
    cfg = _cfg(tmp_path)
    surface, files = surface_snapshot(cfg)
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    (tmp_path / ".github" / "workflows" / "ci.yml").write_text("x")
    assert trust_refusal(cfg, _stored(surface, files), started=True) is None
    assert "created .github/workflows/ci.yml" in harness_violations(
        cfg, content_hashes(files)
    )


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
    assert "harness trust" in trust_refusal(cfg, _stored(surface, files, "recaptured"), started=True)
    assert "harness trust" in trust_refusal(cfg, None, started=True)


def test_not_started_without_baseline_is_not_refused(tmp_path):
    assert trust_refusal(_cfg(tmp_path), None, started=False) is None


def test_initial_and_operator_are_trusted(tmp_path):
    cfg = _cfg(tmp_path)
    surface, files = surface_snapshot(cfg)
    for provenance in ("initial", "operator"):
        assert trust_refusal(cfg, _stored(surface, files, provenance), started=True) is None
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_harness_surface_snapshot.py -v`
Expected: FAIL — `ImportError: cannot import name 'surface_snapshot'`.

- [ ] **Step 3: Implement** (in `harness.py`, after `snapshot_contents`):

```python
TRUST_REMEDY = (
    "restore the harness files of this task's tree to a state you have checked "
    "(e.g. against the main branch), then confirm it with "
    "`spec-runner harness trust <TASK> --reason \"…\"`"
    " (add `--bind-branch <current branch>` if the task has no workspace record)"
)


def _candidates(config: ExecutorConfig) -> list[str]:
    """Every surface candidate key, in a stable order, without duplicates."""
    keys = [*HARNESS_CANDIDATES, *_control_plane_keys(config), *config.harness_files]
    return list(dict.fromkeys(_surface_key(config, config.project_root / k) for k in keys))


def surface_snapshot(
    config: ExecutorConfig,
) -> tuple[dict[str, str], dict[str, bytes | None]]:
    """Candidate states and file bytes — the persisted baseline's shape.

    A directory candidate is snapshotted whole: every file under it gets an
    entry, so a file that appears there later is `created`, not unknown.
    """
    surface: dict[str, str] = {}
    files: dict[str, bytes | None] = {}
    for key in _candidates(config):
        path = Path(key) if Path(key).is_absolute() else config.project_root / key
        surface[key] = "dir" if path.is_dir() else "file" if path.is_file() else "absent"
        for f in _iter_files(path):
            try:
                files[_surface_key(config, f)] = f.read_bytes()
            except OSError:
                files[_surface_key(config, f)] = None
    return surface, files


def trust_refusal(
    config: ExecutorConfig, stored: "StoredBaseline | None", *, started: bool
) -> str | None:
    """Why `strict` cannot trust this task's harness baseline, or None."""
    if config.harness_guard != "strict":
        return None
    if stored is None:
        if not started:
            return None
        return f"this task started without a trusted harness baseline — {TRUST_REMEDY}"
    if stored.provenance not in ("initial", "operator"):
        return (
            f"the harness baseline was re-captured automatically ({stored.provenance}) "
            f"and is not trusted under strict — {TRUST_REMEDY}"
        )
    unknown = [k for k in _candidates(config) if k not in stored.surface]
    if unknown:
        return (
            f"the harness surface grew since the baseline ({', '.join(unknown)}); "
            f"the new surface cannot be checked — {TRUST_REMEDY}"
        )
    unreadable = sorted(p for p, data in stored.files.items() if data is None)
    if unreadable:
        return (
            f"baseline holds unreadable files ({', '.join(unreadable)}); fix them, "
            f"then {TRUST_REMEDY}"
        )
    return None
```

Add `from typing import TYPE_CHECKING` and under it `from .state import StoredBaseline` (avoid an import cycle: `state` does not import `harness`, but keep it type-only).

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_harness_surface_snapshot.py tests/test_harness_restore.py tests/test_harness_guard.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/spec_runner/harness.py tests/test_harness_surface_snapshot.py
git commit -m "feat(harness): candidate-level surface snapshot and strict trust rules"
```

---

### Task 3: Persisted baseline in the execution path

**Files:**
- Modify: `src/spec_runner/harness.py` (`HarnessBaseline`), `src/spec_runner/execution.py` (`_execute_task` ~:651-715, `run_with_retries` ~:1600-1630), `src/spec_runner/hooks.py` (`pre_start_hook` signature gains `state`)
- Test: `tests/test_trusted_baseline.py`

**Interfaces:**
- Consumes: Task 1 state methods, Task 2 `surface_snapshot`, `trust_refusal`.
- Produces:
  - `task_started(config, state, task) -> bool` in `harness.py` — workspace row, or recorded attempts, or (when `create_git_branch`) the task branch exists. Called **before** `pre_start_hook`.
  - `HarnessBaseline.prepare(config, state, task, *, started: bool) -> str | None` — reads the persisted row; returns a POLICY refusal text (strict) or None. Raises `HarnessStateError` on DB read failure.
  - `HarnessBaseline.capture(config, state, task) -> dict[str, str] | None` — after `pre_start_hook`: writes the `initial` (not started) or `recaptured` (warn, started, none) snapshot once, returns file hashes for `harness_violations`; `None` under `off`. Raises `HarnessStateError` on write failure.
  - `class HarnessStateError(RuntimeError)` in `harness.py`.
  - `pre_start_hook(task, config, *, reporter=None, state=None)`; after a successful branch stage (or always, when `create_git_branch` is false) it calls `state.record_workspace(...)` if `state` is given.

- [ ] **Step 1: Write the failing tests**

```python
"""The harness baseline is persisted before the first attempt and reused (spec §2)."""

import subprocess
from pathlib import Path

import pytest

from spec_runner import paid_call
from spec_runner.config import ExecutorConfig
from spec_runner.state import ExecutorState
from spec_runner.task import Task
from spec_runner.tdd import resolve_namespace

PYPROJECT = "[project]\nname = 'demo'\n"


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / "pyproject.toml").write_text(PYPROJECT)
    (tmp_path / "spec").mkdir()
    (tmp_path / "spec" / "tasks.md").write_text(
        "# Spec\n\n## M0\n\n### TASK-050: work\n🔴 P0 | ⬜ TODO | Est: 1d\n\n"
        "**Description:** work\n\n**Checklist:**\n- [ ] do it\n\n"
        "**Traces to:** [REQ-0]\n**Depends on:** —\n"
    )
    (tmp_path / "logs").mkdir()
    return tmp_path


def _cfg(project: Path, **kw) -> ExecutorConfig:
    base = dict(
        state_file=project / "state.db", project_root=project, logs_dir=project / "logs",
        create_git_branch=False, auto_commit=False, run_tests_on_done=False,
        run_review=False, harness_guard="strict", max_retries=1, retry_delay_seconds=0,
    )
    base.update(kw)
    return ExecutorConfig(**base)


def _task() -> Task:
    return Task(id="TASK-050", name="work", priority="p0", status="todo",
                description="work", estimate="1d")


def _agent(monkeypatch, write=None):
    def _spawn(invocation, *, timeout, cwd, env):
        if write:
            write()
        return subprocess.CompletedProcess(invocation.argv, 0, "TASK_COMPLETE\n", "")

    monkeypatch.setattr(paid_call, "_spawn", _spawn)


@pytest.fixture(autouse=True)
def no_hooks(monkeypatch):
    from spec_runner import execution

    monkeypatch.setattr(
        execution, "post_done_hook", lambda *a, **k: (False, "Tests failed", "skipped", "", False)
    )


def _run(cfg):
    from spec_runner.execution import run_with_retries

    with ExecutorState(cfg) as state:
        return run_with_retries(_task(), cfg, state)


def test_a_separate_retry_reuses_the_original_baseline(project, monkeypatch):
    cfg = _cfg(project)
    _agent(monkeypatch)
    _run(cfg)
    with ExecutorState(cfg) as st:
        first = st.get_harness_baseline(resolve_namespace(cfg), "TASK-050")
    assert first.provenance == "initial"
    (project / "pyproject.toml").write_text(PYPROJECT + "# drift\n")
    _run(cfg)  # new ExecutorState, new connection, same DB file
    with ExecutorState(cfg) as st:
        again = st.get_harness_baseline(resolve_namespace(cfg), "TASK-050")
        attempts = st.get_task_state("TASK-050").attempts
    assert again.captured_at == first.captured_at and again.provenance == "initial"
    assert attempts[-1].error_kind == "harness_guard"


def test_strict_refuses_a_started_task_without_baseline(project, monkeypatch):
    cfg = _cfg(project, harness_guard="off")
    _agent(monkeypatch)
    _run(cfg)  # started under off: workspace row, no snapshot
    with ExecutorState(cfg) as st:
        assert st.get_workspace(resolve_namespace(cfg), "TASK-050") is not None
    called: list[int] = []
    _agent(monkeypatch, write=lambda: called.append(1))
    result = _run(_cfg(project))
    assert result is not True and called == []


def test_warn_recaptures_but_strict_does_not_trust_it(project, monkeypatch):
    _agent(monkeypatch)
    _run(_cfg(project, harness_guard="off"))
    _run(_cfg(project, harness_guard="warn"))
    with ExecutorState(_cfg(project)) as st:
        stored = st.get_harness_baseline(resolve_namespace(_cfg(project)), "TASK-050")
    assert stored.provenance == "recaptured"
    called: list[int] = []
    _agent(monkeypatch, write=lambda: called.append(1))
    assert _run(_cfg(project)) is not True and called == []


def test_db_failure_refuses_before_the_agent(project, monkeypatch):
    from spec_runner.state import ExecutorState as ES

    def boom(self, *a, **k):
        raise __import__("sqlite3").OperationalError("disk I/O error")

    monkeypatch.setattr(ES, "get_harness_baseline", boom)
    called: list[int] = []
    _agent(monkeypatch, write=lambda: called.append(1))
    assert _run(_cfg(project)) is not True and called == []
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_trusted_baseline.py -v`
Expected: FAIL (baseline not persisted: `first` is None).

- [ ] **Step 3: Implement**

`harness.py` — replace `HarnessBaseline`:

```python
class HarnessStateError(RuntimeError):
    """The persisted harness state could not be read or written."""


def task_started(config: ExecutorConfig, state: "ExecutorState", task) -> bool:
    """Whether the task started before this attempt (call before `pre_start`)."""
    from .tdd import resolve_namespace

    namespace = resolve_namespace(config)
    if state.get_workspace(namespace, task.id) is not None:
        return True
    if state.get_task_state(task.id).attempt_count > 0:
        return True
    if config.create_git_branch:
        from .git_ops import _git, get_task_branch_name

        probe = _git(config, "rev-parse", "--verify", "--quiet", get_task_branch_name(task))
        return probe.returncode == 0
    return False


class HarnessBaseline:
    """The task's persisted harness baseline (#137, spec 2026-10-04 §2).

    Captured once, after `pre_start_hook` (so `uv sync` is preparation, not a
    mutation) and before any agent call, then read by every later attempt and
    every later invocation — never re-captured from carried work.
    """

    __slots__ = ("_hashes", "_loaded", "_stored", "_started")

    def __init__(self) -> None:
        self._hashes: dict[str, str] | None = None
        self._loaded = False
        self._stored: StoredBaseline | None = None
        self._started = False

    def prepare(self, config, state, task, *, started: bool) -> str | None:
        """Read the persisted baseline; the strict refusal, if any."""
        from .tdd import resolve_namespace

        self._started = started
        if config.harness_guard == "off":
            return None
        try:
            self._stored = state.get_harness_baseline(resolve_namespace(config), task.id)
        except Exception as exc:
            raise HarnessStateError(f"could not read the harness baseline: {exc}") from exc
        return trust_refusal(config, self._stored, started=started)

    def capture(self, config, state, task) -> dict[str, str] | None:
        """File hashes to judge against; writes the first snapshot once."""
        from .tdd import resolve_namespace

        if config.harness_guard == "off":
            return None
        if self._loaded:
            return self._hashes
        if self._stored is None:
            provenance = "recaptured" if self._started else "initial"
            if provenance == "recaptured":
                logger.warning(
                    "No harness baseline for a started task; re-captured under warn",
                    task_id=task.id,
                )
            surface, files = surface_snapshot(config)
            try:
                state.store_harness_baseline(
                    resolve_namespace(config), task.id, provenance=provenance,
                    guard_mode=config.harness_guard, surface=surface, files=files,
                    run_id=None,
                )
            except Exception as exc:
                raise HarnessStateError(f"could not store the harness baseline: {exc}") from exc
            self._stored = StoredBaseline(provenance, config.harness_guard, surface, files, "")
        self._hashes = content_hashes(self._stored.files)
        self._loaded = True
        return self._hashes
```

(Note: under `strict` a started task with `_stored is None` never reaches `capture` — `prepare` refused.)

`execution.py` `_execute_task` — before `pre_start_hook` (:688):

```python
    from .harness import HarnessStateError, task_started
    from .phases import Refusal, RefusalKind

    baseline = harness_baseline or HarnessBaseline()
    try:
        started = task_started(config, state, task)
        trust = baseline.prepare(config, state, task, started=started)
    except HarnessStateError as exc:
        return _refuse_task(
            task, config, state, Refusal(str(exc), RefusalKind.INSTRUMENT),
            kind=RefusalKind.INSTRUMENT, stage="setup",
        )
    if trust is not None:
        return _refuse_task(
            task, config, state, Refusal(trust, RefusalKind.POLICY),
            kind=RefusalKind.POLICY, stage="setup",
        )
```

and pass `state=state` to `pre_start_hook`. Replace the capture line (:708):

```python
    try:
        harness_before = baseline.capture(config, state, task)
    except HarnessStateError as exc:
        return _refuse_task(
            task, config, state, Refusal(str(exc), RefusalKind.INSTRUMENT),
            kind=RefusalKind.INSTRUMENT, stage="setup",
        )
```

Check `_refuse_task`'s signature at `execution.py:49` before writing; match its parameters exactly (it records the attempt and returns the sentinel the caller expects). If it requires the task to be `in_progress`, call it the same way the `control_refusal` branch (:~760) does.

`hooks.py` `pre_start_hook` — signature `(task, config, *, reporter=None, state=None)`. At the end of the function, before plugin hooks:

```python
    if state is not None:
        from .git_ops import current_branch
        from .tdd import resolve_namespace

        branch = current_branch(config) if config.create_git_branch else None
        if not config.create_git_branch or branch == get_task_branch_name(task):
            state.record_workspace(
                resolve_namespace(config), task.id, branch=branch, run_id=None
            )
```

`record_workspace` errors propagate; wrap the `pre_start_hook` call site in `_execute_task` so an exception becomes an INSTRUMENT refusal (same pattern as above).

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_trusted_baseline.py tests/test_harness_guard_retry.py tests/test_harness_guard_before_red.py tests/test_harness_guard_after_post_done.py tests/test_config_under_guard.py tests/test_execution.py -v`
Expected: PASS. Existing harness tests that build `HarnessBaseline()` directly or call `capture(config)` must be updated to the new signature in this step.

- [ ] **Step 5: Commit**

```bash
git add -A src/spec_runner tests
git commit -m "feat(harness): persisted baseline before the first attempt; strict trust refusals"
```

---

### Task 4: `wip.py` — save WIP or refuse

**Files:**
- Create: `src/spec_runner/wip.py`
- Test: `tests/test_wip_save.py`

**Interfaces:**
- Consumes: Task 1 `workspace_for_branch`, `get_task_state`; `git_ops.uncommitted_work_paths`, `spec_contract_paths`, `current_branch`.
- Produces:
  - `WIP_TRAILER = "Spec-Runner-WIP"`, `WIP_ATTEMPT_TRAILER = "Spec-Runner-WIP-Attempt"`
  - `@dataclass(frozen=True) class WipResult: saved_sha: str | None; refusal: Refusal | None`
  - `save_wip(config: ExecutorConfig, state: ExecutorState) -> WipResult` — owner from `current_branch` + `workspace_for_branch(resolve_namespace(config), branch)`; no owner → `WipResult(None, None)` (caller falls back to rescue). Refusals: partially staged path (POLICY), stage/commit failure (INSTRUMENT).
  - `wip_commits(config, task_id: str, base: str) -> list[tuple[str, int, list[str]]]` — `(sha, attempt, files)` of this task's WIP commits in `base..HEAD`, oldest first (used by Tasks 6–7).
  - `is_wip_of(config, sha: str, task_id: str) -> bool`.

- [ ] **Step 1: Write the failing tests**

```python
"""save_wip: composition, ownership, index safety (spec §1)."""

import subprocess
from pathlib import Path

import pytest

from spec_runner.config import ExecutorConfig
from spec_runner.state import ExecutorState
from spec_runner.tdd import resolve_namespace
from spec_runner.wip import save_wip, wip_commits

BRANCH = "task/task-060-work"


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, check=True
    ).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "t@e.c")
    _git(tmp_path, "config", "user.name", "T")
    (tmp_path / "spec").mkdir()
    (tmp_path / "spec" / "tasks.md").write_text("# tasks\n")
    (tmp_path / "app.py").write_text("x = 1\n")
    (tmp_path / ".gitignore").write_text("state.db*\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "base")
    _git(tmp_path, "checkout", "-qb", BRANCH)
    return tmp_path


def _cfg(root: Path) -> ExecutorConfig:
    return ExecutorConfig(project_root=root, state_file=root / "state.db",
                          create_git_branch=True)


def _own(cfg, task="TASK-060", branch=BRANCH, ns=None):
    with ExecutorState(cfg) as st:
        st.record_workspace(ns or resolve_namespace(cfg), task, branch=branch, run_id=None)


def _save(cfg):
    with ExecutorState(cfg) as st:
        return save_wip(cfg, st)


def test_owned_dirt_becomes_a_wip_commit(repo):
    cfg = _cfg(repo)
    _own(cfg)
    (repo / "app.py").write_text("x = 2\n")
    (repo / "new.py").write_text("y = 1\n")
    result = _save(cfg)
    assert result.refusal is None and result.saved_sha
    body = _git(repo, "log", "-1", "--format=%B")
    assert body.startswith("wip(TASK-060): unfinished work of attempt 0 — not a candidate")
    assert "Spec-Runner-WIP: TASK-060" in body and "Spec-Runner-WIP-Attempt: 0" in body
    assert _git(repo, "status", "--porcelain") == ""
    assert wip_commits(cfg, "TASK-060", "main")[0][1] == 0


def test_spec_and_staged_runtime_do_not_ride_along(repo):
    cfg = _cfg(repo)
    _own(cfg)
    (repo / "app.py").write_text("x = 2\n")
    (repo / "spec" / "tasks.md").write_text("# tasks\nflip\n")
    _git(repo, "add", "spec/tasks.md")
    result = _save(cfg)
    assert result.saved_sha
    files = _git(repo, "show", "--name-only", "--format=", "HEAD").split()
    assert files == ["app.py"]
    assert "spec/tasks.md" in _git(repo, "status", "--porcelain")


def test_nothing_eligible_makes_no_commit(repo):
    cfg = _cfg(repo)
    _own(cfg)
    head = _git(repo, "rev-parse", "HEAD")
    (repo / "spec" / "tasks.md").write_text("# tasks\nflip\n")
    assert _save(cfg).saved_sha is None
    assert _git(repo, "rev-parse", "HEAD") == head


def test_partially_staged_path_is_refused_untouched(repo):
    cfg = _cfg(repo)
    _own(cfg)
    (repo / "app.py").write_text("x = 2\n")
    _git(repo, "add", "app.py")
    (repo / "app.py").write_text("x = 3\n")
    before_index = _git(repo, "show", ":app.py")
    result = _save(cfg)
    assert result.refusal is not None and "app.py" in str(result.refusal)
    assert _git(repo, "show", ":app.py") == before_index
    assert (repo / "app.py").read_text() == "x = 3\n"


def test_wip_records_a_deletion(repo):
    cfg = _cfg(repo)
    _own(cfg)
    (repo / "app.py").unlink()
    assert _save(cfg).saved_sha
    assert "app.py" not in _git(repo, "ls-files")


def test_wip_path_with_space_and_dash(repo):
    cfg = _cfg(repo)
    _own(cfg)
    (repo / "a file.py").write_text("1\n")
    (repo / "-dash.py").write_text("2\n")
    assert _save(cfg).saved_sha
    tracked = _git(repo, "ls-files").splitlines()
    assert "a file.py" in tracked and "-dash.py" in tracked


def test_no_row_for_exact_branch_is_not_owned(repo):
    cfg = _cfg(repo)
    _own(cfg, branch="task/task-060")
    (repo / "app.py").write_text("x = 2\n")
    assert _save(cfg) .saved_sha is None


def test_other_namespace_row_is_not_ownership(repo):
    cfg = _cfg(repo)
    _own(cfg, ns="someone-else")
    (repo / "app.py").write_text("x = 2\n")
    assert _save(cfg).saved_sha is None


def test_detached_head_falls_back_to_stash(repo):
    cfg = _cfg(repo)
    _own(cfg)
    _git(repo, "checkout", "-q", "--detach")
    (repo / "app.py").write_text("x = 2\n")
    assert _save(cfg).saved_sha is None


def test_commit_failure_is_refused_with_work_in_tree(repo):
    cfg = _cfg(repo)
    _own(cfg)
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)
    (repo / "app.py").write_text("x = 2\n")
    result = _save(cfg)
    assert result.refusal is not None and result.saved_sha is None
    assert (repo / "app.py").read_text() == "x = 2\n"
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_wip_save.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'spec_runner.wip'`.

- [ ] **Step 3: Implement** `src/spec_runner/wip.py`:

```python
"""Carry a failed attempt's work forward as a WIP commit (spec 2026-10-04 §1).

Called before every destructive tree switch. Owns nothing else: whether the
work is trusted is the harness baseline's question, never this commit's.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass

from .config import ExecutorConfig
from .git_ops import current_branch, spec_contract_paths, uncommitted_work_paths
from .logging import get_logger
from .phases import Refusal, RefusalKind
from .state import ExecutorState
from .tdd import resolve_namespace

logger = get_logger("wip")

WIP_TRAILER = "Spec-Runner-WIP"
WIP_ATTEMPT_TRAILER = "Spec-Runner-WIP-Attempt"


@dataclass(frozen=True)
class WipResult:
    """What `save_wip` did: a commit, a refusal, or neither (not ours)."""

    saved_sha: str | None
    refusal: Refusal | None


def _git(config: ExecutorConfig, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=config.project_root, capture_output=True, text=True
    )


def owner(config: ExecutorConfig, state: ExecutorState) -> str | None:
    """The started task whose recorded branch is exactly the current one."""
    branch = current_branch(config)
    if branch is None:
        return None
    return state.workspace_for_branch(resolve_namespace(config), branch)


def _partially_staged(config: ExecutorConfig, paths: list[str]) -> list[str]:
    staged = set(_git(config, "diff", "--cached", "--name-only", "-z").stdout.split("\0"))
    unstaged = set(_git(config, "diff", "--name-only", "-z").stdout.split("\0"))
    return sorted(p for p in paths if p in staged and p in unstaged)


def save_wip(config: ExecutorConfig, state: ExecutorState) -> WipResult:
    """Commit the owned task's eligible changes as WIP, or refuse."""
    task_id = owner(config, state)
    if task_id is None:
        return WipResult(None, None)
    paths = uncommitted_work_paths(config, spec_contract_paths(config), strict=True)
    if not paths:
        return WipResult(None, None)
    split = _partially_staged(config, paths)
    if split:
        return WipResult(None, Refusal(
            f"cannot save {task_id}'s work as WIP: {', '.join(split)} "
            "differ between HEAD, the index and the working tree; commit or unstage "
            "them, then retry — the work is in the tree and nothing destructive ran",
            RefusalKind.POLICY,
        ))
    attempt = state.get_task_state(task_id).attempt_count
    message = (
        f"wip({task_id}): unfinished work of attempt {attempt} — not a candidate\n\n"
        f"{WIP_TRAILER}: {task_id}\n{WIP_ATTEMPT_TRAILER}: {attempt}\n"
    )
    added = _git(config, "add", "-A", "--", *paths)
    committed = (
        _git(config, "commit", "--only", "-m", message, "--", *paths)
        if added.returncode == 0
        else added
    )
    if committed.returncode != 0:
        return WipResult(None, Refusal(
            f"could not save {task_id}'s work as WIP "
            f"({committed.stderr.strip()[:200] or 'git failed'}); the work is in the "
            "tree and nothing destructive ran",
            RefusalKind.INSTRUMENT,
        ))
    sha = _git(config, "rev-parse", "HEAD").stdout.strip()
    logger.info("Saved WIP", task_id=task_id, sha=sha, paths=len(paths))
    return WipResult(sha, None)


def is_wip_of(config: ExecutorConfig, sha: str, task_id: str) -> bool:
    """Whether `sha` carries this task's WIP trailer."""
    body = _git(config, "log", "-1", "--format=%(trailers:key=" + WIP_TRAILER + ",valueonly)", sha)
    return body.returncode == 0 and task_id in body.stdout.split()


def wip_commits(config: ExecutorConfig, task_id: str, base: str) -> list[tuple[str, int, list[str]]]:
    """This task's WIP commits in `base..HEAD`, oldest first."""
    shas = _git(config, "rev-list", "--reverse", f"{base}..HEAD").stdout.split()
    found: list[tuple[str, int, list[str]]] = []
    for sha in shas:
        if not is_wip_of(config, sha, task_id):
            continue
        attempt = _git(
            config, "log", "-1",
            "--format=%(trailers:key=" + WIP_ATTEMPT_TRAILER + ",valueonly)", sha,
        ).stdout.strip()
        files = _git(config, "show", "--name-only", "--format=", sha).stdout.split("\n")
        found.append((sha, int(attempt or 0), [f for f in files if f]))
    return found
```

Check `uncommitted_work_paths` returns paths with `-z`-safe parsing for names with spaces; if it uses non-`-z` porcelain, a quoted name (`"a file.py"`) breaks `git add`. If so, switch that function to `git status --porcelain -z` in this step (its other callers only display paths).

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_wip_save.py tests/test_rescue_uncommitted.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/spec_runner/wip.py src/spec_runner/git_ops.py tests/test_wip_save.py
git commit -m "feat(wip): save an owned task's work as a WIP commit, or refuse"
```

---

### Task 5: Wire WIP into both destructive points

**Files:**
- Modify: `src/spec_runner/hooks.py` (`pre_start_hook` branch stage ~:286), `src/spec_runner/cli.py` (`_maybe_start_integration` ~:347)
- Test: `tests/test_retry_continues_from_wip.py`

**Interfaces:**
- Consumes: Task 4 `save_wip`, `owner`; Task 3 `pre_start_hook(..., state=...)`.
- Produces: behaviour only.

Rules implemented:
- `pre_start_hook` branch stage, before `rescue_uncommitted`: if `state` is given, `result = save_wip(config, state)`; a refusal → log, `reporter.record_for("branch", ERROR, …)`, return False (the caller's `"HOOK_ERROR"` path; ensure the attempt is recorded with the refusal's kind — pass the refusal text up via a `PreStartRefusal` attribute or by returning the `Refusal`; mirror how `rescue_detail` is reported today). Then the existing `rescue_uncommitted` stashes what remains (spec contract paths); its failure keeps today's refusal.
- `_maybe_start_integration`: before `rescue_run_uncommitted`, open `with ExecutorState(config) as st:` and call `save_wip(config, st)`; a refusal → `_refuse_integration(str(refusal))`. A DB error opening/reading → `_refuse_integration(...)` too. Then today's `rescue_run_uncommitted`; spec/config dirt stays for `_enforce_clean_spec` (which already ran earlier) and the checkout.
- Under `strict`, at `_maybe_start_integration`: if the current branch starts with `task/`, has eligible dirt, and `owner()` is None → `_refuse_integration("uncommitted work on <branch> belongs to no recorded task …" + TRUST_REMEDY)`.

- [ ] **Step 1: Write the failing tests**

```python
"""End to end: a retry continues from the previous attempt's work (spec §1, §4)."""

import subprocess
from pathlib import Path

import pytest

from spec_runner import paid_call
from spec_runner.config import ExecutorConfig
from spec_runner.state import ExecutorState
from spec_runner.task import Task


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True,
                          check=True).stdout


TASKS = (
    "# Spec\n\n## M0\n\n### TASK-070: work\n🔴 P0 | ⬜ TODO | Est: 1d\n\n"
    "**Description:** work\n\n**Checklist:**\n- [ ] do it\n\n"
    "**Traces to:** [REQ-0]\n**Depends on:** —\n"
)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "t@e.c")
    _git(tmp_path, "config", "user.name", "T")
    (tmp_path / "spec").mkdir()
    (tmp_path / "spec" / "tasks.md").write_text(TASKS)
    (tmp_path / "pyproject.toml").write_text("[project]\nname='d'\n")
    (tmp_path / ".gitignore").write_text("spec/.executor*\nlogs/\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "base")
    return tmp_path


def _cfg(repo: Path, **kw) -> ExecutorConfig:
    base = dict(
        project_root=repo, state_file=repo / "spec" / ".executor-state.db",
        logs_dir=repo / "logs", create_git_branch=True, auto_commit=True,
        run_tests_on_done=False, run_review=False, task_timeout_minutes=1,
        max_retries=2, retry_delay_seconds=0, sync_deps=False, harness_guard="strict",
    )
    base.update(kw)
    return ExecutorConfig(**base)


def _task() -> Task:
    return Task(id="TASK-070", name="work", priority="p0", status="todo",
                description="work", estimate="1d")


def test_attempt_two_sees_attempt_ones_file(repo, monkeypatch):
    prompts: list[str] = []
    calls: list[int] = []

    def _spawn(invocation, *, timeout, cwd, env):
        calls.append(1)
        prompts.append(" ".join(invocation.argv))
        if len(calls) == 1:
            (repo / "feature.py").write_text("def f():\n    return 1\n")
            raise subprocess.TimeoutExpired(invocation.argv, timeout)
        assert (repo / "feature.py").exists(), "attempt 2 started from scratch"
        return subprocess.CompletedProcess(invocation.argv, 0, "TASK_COMPLETE\n", "")

    monkeypatch.setattr(paid_call, "_spawn", _spawn)
    from spec_runner.execution import run_with_retries

    cfg = _cfg(repo)
    with ExecutorState(cfg) as st:
        run_with_retries(_task(), cfg, st)
    log = _git(repo, "log", "--all", "--format=%s%n%b")
    assert "wip(TASK-070): unfinished work of attempt 1 — not a candidate" in log
    assert "Spec-Runner-WIP-Attempt: 1" in log
    assert "continuing unfinished work of attempt 1" in prompts[1]


def test_operator_dirt_on_task_branch_is_preserved_as_wip(repo, monkeypatch):
    """Review Focus 1: an operator's edits on the recorded branch are kept."""
    # first attempt creates branch + workspace, then fails
    def _fail(invocation, *, timeout, cwd, env):
        raise subprocess.TimeoutExpired(invocation.argv, timeout)

    monkeypatch.setattr(paid_call, "_spawn", _fail)
    from spec_runner.execution import run_with_retries

    cfg = _cfg(repo, max_retries=1)
    with ExecutorState(cfg) as st:
        run_with_retries(_task(), cfg, st)
    (repo / "operator_note.py").write_text("# by hand\n")
    seen: list[bool] = []

    def _look(invocation, *, timeout, cwd, env):
        seen.append((repo / "operator_note.py").exists())
        return subprocess.CompletedProcess(invocation.argv, 0, "TASK_COMPLETE\n", "")

    monkeypatch.setattr(paid_call, "_spawn", _look)
    with ExecutorState(cfg) as st:
        st.get_task_state("TASK-070").attempts = []
        run_with_retries(_task(), cfg, st)
    assert seen == [True]


def test_failing_rescue_after_wip_blocks_the_cleanup(repo, monkeypatch):
    from spec_runner import hooks

    def _fail(invocation, *, timeout, cwd, env):
        (repo / "feature.py").write_text("x\n")
        (repo / "spec" / "tasks.md").write_text(TASKS + "\n<!-- flip -->\n")
        raise subprocess.TimeoutExpired(invocation.argv, timeout)

    monkeypatch.setattr(paid_call, "_spawn", _fail)
    monkeypatch.setattr(hooks, "rescue_uncommitted", lambda *a, **k: (False, "stash failed"))
    from spec_runner.execution import run_with_retries

    cfg = _cfg(repo)
    with ExecutorState(cfg) as st:
        run_with_retries(_task(), cfg, st)
    assert "<!-- flip -->" in (repo / "spec" / "tasks.md").read_text()
    assert "wip(TASK-070)" in _git(repo, "log", "-1", "--format=%s")


def test_create_git_branch_false_is_untouched(repo, monkeypatch):
    calls: list[int] = []

    def _spawn(invocation, *, timeout, cwd, env):
        calls.append(1)
        if len(calls) == 1:
            (repo / "feature.py").write_text("x\n")
            raise subprocess.TimeoutExpired(invocation.argv, timeout)
        return subprocess.CompletedProcess(invocation.argv, 0, "TASK_COMPLETE\n", "")

    monkeypatch.setattr(paid_call, "_spawn", _spawn)
    from spec_runner.execution import run_with_retries

    cfg = _cfg(repo, create_git_branch=False, auto_commit=False)
    with ExecutorState(cfg) as st:
        run_with_retries(_task(), cfg, st)
    assert "wip(" not in _git(repo, "log", "--all", "--format=%s")
    assert (repo / "feature.py").exists()
```

Add `test_off_still_saves_wip`: the first test of this file with `harness_guard="off"` — the WIP commit still appears (the workspace row is written in every mode; no snapshot exists).

Add `test_db_failure_refuses_before_the_integration_fork`: write a config with `integration_pr: true`, leave dirt on the recorded task branch, monkeypatch `spec_runner.state.ExecutorState.workspace_for_branch` to raise `sqlite3.OperationalError`, run `cli.main(["retry", "TASK-070", "--force"])` under `pytest.raises(SystemExit)`; assert the exit code is non-zero, `git stash list` is empty, the current branch is unchanged and the dirt is still in the tree.

Add an `integration_pr` test: run `spec_runner.cli.main(["retry", "TASK-070", "--force"])` after a failed first run with `integration_pr: true` in a written `spec-runner.config.yaml`, assert the WIP commit exists on the task branch and `git stash list` has no `spec-runner rescue: run` entry. Use the CLI config shape the existing `tests/test_integration_pr_never_touches_main.py` uses (read it first and copy its config writer).

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_retry_continues_from_wip.py -v`
Expected: FAIL — `attempt 2 started from scratch`.

- [ ] **Step 3: Implement** (as described under "Rules implemented"; in `pre_start_hook`, immediately before `rescued, rescue_detail = rescue_uncommitted(task, config)`):

```python
            if state is not None:
                from .wip import save_wip

                saved = save_wip(config, state)
                if saved.refusal is not None:
                    logger.error("Refusing to start: WIP not saved", task_id=task.id)
                    if reporter:
                        reporter.record_for("branch", PhaseOutcome.ERROR, str(saved.refusal))
                    return False
```

In `_maybe_start_integration`, before `rescued, detail = rescue_run_uncommitted(config)`:

```python
    from .harness import TRUST_REMEDY
    from .wip import owner, save_wip

    try:
        with ExecutorState(config) as st:
            saved = save_wip(config, st)
            stray = owner(config, st) is None
    except Exception as exc:  # the DB is the authority for ownership
        _refuse_integration(f"could not read the task workspace records: {exc}")
    if saved.refusal is not None:
        _refuse_integration(str(saved.refusal))
    branch = current_branch(config) or ""
    if (
        config.harness_guard == "strict"
        and stray
        and branch.startswith("task/")
        and uncommitted_work_paths(config, spec_contract_paths(config))
    ):
        _refuse_integration(
            f"uncommitted work on {branch} belongs to no recorded task; {TRUST_REMEDY}"
        )
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_retry_continues_from_wip.py tests/test_rescue_uncommitted.py tests/test_integration_pr_never_touches_main.py tests/test_hooks.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add -A src/spec_runner tests
git commit -m "feat(wip): save WIP before the branch stage and the integration fork"
```

---

### Task 6: Red adoption, explicit candidate, cumulative no-op

**Files:**
- Modify: `src/spec_runner/tdd.py` (`_unregistered_red` :2301), `src/spec_runner/hooks.py` (candidate ~:1415, no-op ~:1997)
- Test: `tests/test_wip_and_gates.py`

**Interfaces:**
- Consumes: Task 4 `is_wip_of`, `wip_commits`; `review.task_base(config)`.
- Produces: behaviour only.

- [ ] **Step 1: Write the failing tests**

```python
"""WIP never feeds a gate; red adoption sees through a WIP chain (spec §4)."""

import subprocess
from pathlib import Path

from spec_runner.config import ExecutorConfig
from spec_runner.task import Task
from spec_runner.wip import WIP_ATTEMPT_TRAILER, WIP_TRAILER

SEL = "tests/test_x.py::test_x"


def _git(root, *a):
    return subprocess.run(["git", *a], cwd=root, capture_output=True, text=True,
                          check=True).stdout


def _repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "t@e.c")
    _git(tmp_path, "config", "user.name", "T")
    (tmp_path / "a.py").write_text("1\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "base")
    _git(tmp_path, "checkout", "-qb", "task/task-080-w")
    return tmp_path


def _commit(root, name, msg):
    (root / name).write_text(name)
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", msg)


def _wip(root, name, task="TASK-080", attempt=1):
    _commit(root, name, f"wip({task}): x\n\n{WIP_TRAILER}: {task}\n{WIP_ATTEMPT_TRAILER}: {attempt}")


def _task():
    return Task(id="TASK-080", name="w", priority="p0", status="todo", estimate="1d")


class _St:
    def checkpoint_exists_for_commit(self, ns, sha):
        return False


def test_red_found_through_a_wip_chain(tmp_path):
    from spec_runner.tdd import _unregistered_red

    root = _repo(tmp_path)
    _commit(root, "t.py", f"TASK-080: red for {SEL}")
    red = _git(root, "rev-parse", "HEAD").strip()
    _wip(root, "w1.py")
    _wip(root, "w2.py", attempt=2)
    cfg = ExecutorConfig(project_root=root, create_git_branch=True)
    assert _unregistered_red(cfg, _St(), _task(), SEL) == red


def test_chain_stops_at_another_commit(tmp_path):
    from spec_runner.tdd import _unregistered_red

    root = _repo(tmp_path)
    _commit(root, "t.py", f"TASK-080: red for {SEL}")
    _commit(root, "o.py", "someone else")
    _wip(root, "w1.py")
    cfg = ExecutorConfig(project_root=root, create_git_branch=True)
    assert _unregistered_red(cfg, _St(), _task(), SEL) == ""


def test_other_tasks_wip_is_not_skipped(tmp_path):
    from spec_runner.tdd import _unregistered_red

    root = _repo(tmp_path)
    _commit(root, "t.py", f"TASK-080: red for {SEL}")
    _wip(root, "w1.py", task="TASK-999")
    cfg = ExecutorConfig(project_root=root, create_git_branch=True)
    assert _unregistered_red(cfg, _St(), _task(), SEL) == ""


def test_candidate_over_wip_is_explicit(tmp_path):
    from spec_runner.hooks import commit_candidate_over_wip

    root = _repo(tmp_path)
    _wip(root, "w1.py")
    cfg = ExecutorConfig(project_root=root, create_git_branch=True, auto_commit=True)
    commit_candidate_over_wip(_task(), cfg)
    assert _git(root, "log", "-1", "--format=%s").strip() == "TASK-080: candidate"


def test_noop_is_cumulative(tmp_path):
    from spec_runner.hooks import task_changed_since_base

    root = _repo(tmp_path)
    cfg = ExecutorConfig(project_root=root, create_git_branch=True)
    _wip(root, "w1.py")
    assert task_changed_since_base(cfg) is True
    (root / "w1.py").unlink()
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "undo")
    assert task_changed_since_base(cfg) is False
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_wip_and_gates.py -v`
Expected: FAIL (`ImportError: commit_candidate_over_wip`; red through chain returns "").

- [ ] **Step 3: Implement**

`tdd.py` `_unregistered_red` — replace the HEAD read:

```python
    from .wip import is_wip_of

    head = _head(config)
    while head and is_wip_of(config, head, task.id):
        parent = _parent_of(config, head)
        if not parent:
            return ""
        head = parent
    if not head:
        return ""
```

(the rest — subject equality, `checkpoint_exists_for_commit` — unchanged). Apply the same walk in `_pending_unregistered_red` (:2353) if it reads HEAD the same way.

`hooks.py`:

```python
def commit_candidate_over_wip(task: Task, config: ExecutorConfig) -> None:
    """Make HEAD a candidate when it is a WIP commit (spec 2026-10-04 §4).

    A gate verdict is bound to a SHA; it must never be a WIP commit.
    """
    from .wip import is_wip_of

    head = _head_sha(config)
    if head and is_wip_of(config, head, task.id):
        subprocess.run(
            ["git", "commit", "--allow-empty", "-m", f"{task.id}: candidate"],
            capture_output=True, text=True, cwd=config.project_root,
        )


def task_changed_since_base(config: ExecutorConfig) -> bool:
    """Whether the task's cumulative diff against its base is non-empty."""
    from .review import task_base

    diff = subprocess.run(
        ["git", "diff", "--quiet", task_base(config), "HEAD", "--"],
        capture_output=True, cwd=config.project_root,
    )
    return diff.returncode == 1
```

Call `commit_candidate_over_wip(task, config)` right after `committed_pre_review = commit_task_work(task, config) == "committed"` (when not committed) and before `review_checkpoint_sha = _head_sha(config)`; and after the final `commit_task_work` when `final == "empty"`. In the no-op computation, when the branch carries WIP of this task (`wip_commits(config, task.id, task_base(config))` non-empty), set `no_op = not task_changed_since_base(config)`.

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_wip_and_gates.py tests/test_rejected_red_is_adopted.py tests/test_red_gate.py tests/test_hooks.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add -A src/spec_runner tests
git commit -m "feat(wip): red adoption through WIP, explicit candidate, cumulative no-op"
```

---

### Task 7: Continuation in the retry prompt

**Files:**
- Modify: `src/spec_runner/state.py` (`RetryContext` :182), `src/spec_runner/execution.py` (RetryContext construction :906), `src/spec_runner/prompt.py` (:704)
- Test: `tests/test_retry_prompt_continuation.py`

**Interfaces:**
- Consumes: Task 4 `wip_commits`.
- Produces: `RetryContext.continuation: tuple[tuple[str, int, tuple[str, ...]], ...] = ()`.

- [ ] **Step 1: Write the failing test**

```python
from spec_runner.config import ExecutorConfig
from spec_runner.prompt import build_task_prompt
from spec_runner.state import ErrorCode, RetryContext
from spec_runner.task import Task


def test_prompt_names_the_continuation(tmp_path):
    task = Task(id="TASK-090", name="w", priority="p0", status="todo", estimate="1d")
    ctx = RetryContext(
        attempt_number=2, max_attempts=3, previous_error_code=ErrorCode.TIMEOUT,
        previous_error="Timeout after 60 minutes", what_was_tried="x", test_failures=None,
        continuation=(("abc1234def", 1, ("feature.py",)),),
    )
    text = build_task_prompt(task, ExecutorConfig(project_root=tmp_path), None, retry_context=ctx)
    assert "continuing unfinished work of attempt 1" in text
    assert "abc1234" in text and "feature.py" in text
    assert "not verified" in text and "revise the approach" in text


def test_no_continuation_no_section(tmp_path):
    task = Task(id="TASK-090", name="w", priority="p0", status="todo", estimate="1d")
    ctx = RetryContext(2, 3, ErrorCode.TIMEOUT, "t", "x", None)
    text = build_task_prompt(task, ExecutorConfig(project_root=tmp_path), None, retry_context=ctx)
    assert "unfinished work" not in text
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_retry_prompt_continuation.py -v`
Expected: FAIL — `TypeError: unexpected keyword argument 'continuation'`.

- [ ] **Step 3: Implement**

`state.py` `RetryContext`: add `continuation: tuple[tuple[str, int, tuple[str, ...]], ...] = ()` as the last field.

`prompt.py`, inside `if retry_context:` after the test-failures block:

```python
        for sha, attempt, files in retry_context.continuation:
            shown = ", ".join(files[:20]) + (" …" if len(files) > 20 else "")
            attempts_section += (
                f"\n**You are continuing unfinished work of attempt {attempt}**, "
                f"committed as `{sha[:12]}` ({shown}). It is not verified — review it "
                "before relying on it. You may revise the approach.\n"
            )
```

`execution.py` RetryContext construction: compute

```python
            continuation: tuple = ()
            if config.create_git_branch:
                from .review import task_base
                from .wip import wip_commits

                continuation = tuple(
                    (sha, n, tuple(files))
                    for sha, n, files in wip_commits(config, task_id, task_base(config))
                )
```

and pass `continuation=continuation`. Also, when there are no failed attempts in memory (a separate `retry` after `_finish_failed_task` cleared them) but `continuation` is non-empty, build a `RetryContext` anyway with `previous_error_code=ErrorCode.UNKNOWN`, `previous_error="previous attempt did not finish"`.

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_retry_prompt_continuation.py tests/test_prompt.py tests/test_retry_continues_from_wip.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add -A src/spec_runner tests
git commit -m "feat(prompt): retry prompt names the WIP it continues"
```

---

### Task 8: Lifecycle and `harness trust`

**Files:**
- Modify: `src/spec_runner/execution.py` (DONE site :1178), `src/spec_runner/remedy.py` (`abandon` :141)
- Create: `src/spec_runner/harness_cmd.py`
- Modify: `src/spec_runner/cli.py` (parser after `budget` block :2641; dispatch after :2878)
- Test: `tests/test_harness_trust.py`

**Interfaces:**
- Consumes: Task 1 `trust_harness`, `forget_task_workspace`, `get_workspace`; Task 2 `surface_snapshot`; `remedy._guard`, `remedy.resolve_actor`, `RemedyError`.
- Produces: `cmd_harness(args, config) -> int`; `trust(config, state, task_id, *, reason, bind_branch=None, actor=None) -> str` (message).

- [ ] **Step 1: Write the failing tests**

```python
"""`harness trust`: audited, guarded, atomic (spec §3); lifecycle (spec §2)."""

import subprocess
from pathlib import Path

import pytest

from spec_runner.config import ExecutorConfig
from spec_runner.harness_cmd import TrustError, trust
from spec_runner.state import ExecutorState
from spec_runner.tdd import resolve_namespace


def _git(root, *a):
    return subprocess.run(["git", *a], cwd=root, capture_output=True, text=True,
                          check=True).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "t@e.c")
    _git(tmp_path, "config", "user.name", "T")
    (tmp_path / "pyproject.toml").write_text("[project]\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "base")
    _git(tmp_path, "checkout", "-qb", "task/task-100-w")
    return tmp_path


def _cfg(root, **kw):
    return ExecutorConfig(project_root=root, state_file=root / "state.db",
                          create_git_branch=True, harness_guard="strict", **kw)


def _trust(cfg, **kw):
    with ExecutorState(cfg) as st:
        return trust(cfg, st, "TASK-100", **kw)


def test_reason_is_required(repo):
    with pytest.raises(TrustError):
        _trust(_cfg(repo), reason="  ", bind_branch="task/task-100-w")


def test_refused_under_an_agent(repo, monkeypatch):
    monkeypatch.setenv("SPEC_RUNNER_AGENT", "1")
    with pytest.raises(TrustError):
        _trust(_cfg(repo), reason="checked", bind_branch="task/task-100-w")


def test_refused_while_the_lock_is_held(repo):
    from spec_runner.config import ExecutorLock

    cfg = _cfg(repo)
    lock = ExecutorLock(cfg.state_file.with_suffix(".lock"))
    assert lock.acquire()
    try:
        with pytest.raises(TrustError):
            _trust(cfg, reason="checked", bind_branch="task/task-100-w")
    finally:
        lock.release()


def test_without_a_workspace_bind_branch_is_required_and_must_be_current(repo):
    cfg = _cfg(repo)
    with pytest.raises(TrustError, match="--bind-branch"):
        _trust(cfg, reason="checked")
    with pytest.raises(TrustError, match="current branch"):
        _trust(cfg, reason="checked", bind_branch="task/other")
    _trust(cfg, reason="checked", bind_branch="task/task-100-w")
    with ExecutorState(cfg) as st:
        ns = resolve_namespace(cfg)
        assert st.get_workspace(ns, "TASK-100")["bound_by"] == "operator"
        assert st.get_harness_baseline(ns, "TASK-100").provenance == "operator"
        assert st.harness_trust_audit(ns, "TASK-100")[0]["bound_branch"] == 1


def test_with_a_workspace_the_branch_must_match(repo):
    cfg = _cfg(repo)
    with ExecutorState(cfg) as st:
        st.record_workspace(resolve_namespace(cfg), "TASK-100", branch="task/else", run_id=None)
    with pytest.raises(TrustError, match="recorded branch"):
        _trust(cfg, reason="checked")


def test_create_git_branch_false_needs_no_bind(repo):
    cfg = _cfg(repo, create_git_branch=False)
    _trust(cfg, reason="checked")
    with ExecutorState(cfg) as st:
        assert st.get_workspace(resolve_namespace(cfg), "TASK-100")["branch"] is None


def test_abandon_forgets_workspace_and_baseline(repo, monkeypatch):
    from spec_runner import remedy

    cfg = _cfg(repo)
    _trust(cfg, reason="checked", bind_branch="task/task-100-w")
    with ExecutorState(cfg) as st:
        remedy._forget_workspace(cfg, st, "TASK-100")
        ns = resolve_namespace(cfg)
        assert st.get_workspace(ns, "TASK-100") is None
        assert st.get_harness_baseline(ns, "TASK-100") is None
        assert len(st.harness_trust_audit(ns, "TASK-100")) == 1
```

Plus, in `tests/test_trusted_baseline.py`, add `test_done_forgets_workspace_and_baseline`: a run whose `post_done_hook` stub returns success → after `run_with_retries`, `get_workspace` and `get_harness_baseline` are None. And `test_abandon_end_to_end` using the existing abandon test fixture shape from `tests/test_remedy.py` (read it; reuse its red/checkpoint setup) asserting both rows are gone after `remedy.abandon(...)`.

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_harness_trust.py -v`
Expected: FAIL — `ModuleNotFoundError: spec_runner.harness_cmd`.

- [ ] **Step 3: Implement**

`src/spec_runner/harness_cmd.py`:

```python
"""`spec-runner harness trust` — an operator confirms a task's harness (spec §3).

Not a way around a refusal: the operator restores and checks the harness
first, then states that it is trusted, with a reason that is kept.
"""

from __future__ import annotations

import argparse

from .config import ExecutorConfig
from .git_ops import current_branch
from .harness import surface_snapshot
from .remedy import RemedyError, _guard, resolve_actor
from .state import ExecutorState


class TrustError(RuntimeError):
    """`harness trust` refused."""


def trust(
    config: ExecutorConfig,
    state: ExecutorState,
    task_id: str,
    *,
    reason: str,
    bind_branch: str | None = None,
    actor: str | None = None,
) -> str:
    """Record an operator-trusted baseline for `task_id`; return a summary."""
    try:
        namespace = _guard(config, reason)
    except RemedyError as exc:
        raise TrustError(str(exc)) from exc
    branch = current_branch(config) if config.create_git_branch else None
    workspace = state.get_workspace(namespace, task_id)
    bind = workspace is None
    if workspace is not None:
        if workspace["branch"] is not None and workspace["branch"] != branch:
            raise TrustError(
                f"{task_id}'s recorded branch is {workspace['branch']}, "
                f"the current branch is {branch}"
            )
    elif config.create_git_branch:
        if bind_branch is None:
            raise TrustError(
                f"{task_id} has no workspace record; name its branch with "
                "--bind-branch <current branch>"
            )
        if bind_branch != branch:
            raise TrustError(f"--bind-branch {bind_branch} is not the current branch ({branch})")
    surface, files = surface_snapshot(config)
    replaced = state.trust_harness(
        namespace, task_id,
        bind=bind,
        bind_branch=bind_branch if (bind and config.create_git_branch) else None,
        branch=branch, surface=surface, files=files, guard_mode=config.harness_guard,
        actor=resolve_actor(config, actor), reason=reason.strip(), run_id=None,
    )
    note = f" (replaced a {replaced} baseline)" if replaced else ""
    return f"✅ {task_id}: harness baseline trusted{note}"


def cmd_harness(args: argparse.Namespace, config: ExecutorConfig) -> int:
    """Dispatch `spec-runner harness <command>`."""
    if args.harness_command != "trust":
        print("usage: spec-runner harness trust TASK --reason …")
        return 1
    with ExecutorState(config) as state:
        try:
            print(trust(
                config, state, args.task_id, reason=args.reason,
                bind_branch=args.bind_branch, actor=args.actor,
            ))
        except TrustError as exc:
            print(f"⛔ {exc}")
            return 1
    return 0
```

`cli.py` parser (after the budget block):

```python
    harness_parser = subparsers.add_parser(
        "harness", parents=[common], help="Harness guard state (operator, audited)"
    )
    harness_sub = harness_parser.add_subparsers(dest="harness_command", required=True)
    harness_trust = harness_sub.add_parser(
        "trust", parents=[common],
        help="Confirm this task's restored and checked harness as its trusted baseline",
    )
    harness_trust.add_argument("task_id")
    harness_trust.add_argument("--reason", required=True, help="Why — recorded, and not optional")
    harness_trust.add_argument("--bind-branch", dest="bind_branch",
                               help="Bind the task to the current branch (no workspace record)")
    harness_trust.add_argument("--actor", help="Who (default: git user.email)")
```

dispatch (after the budget branch):

```python
    if args.command == "harness":
        from .harness_cmd import cmd_harness

        raise SystemExit(cmd_harness(args, config))
```

`execution.py` DONE site (:1178-1180), after `_release_claims`:

```python
                _forget_workspace(state, config, task)
```

with

```python
def _forget_workspace(state, config, task) -> None:
    """DONE: the task's workspace and harness baseline are spent (spec §2)."""
    try:
        state.forget_task_workspace(resolve_namespace(config), task.id)
    except Exception as exc:  # the task is done; a stale row only costs a re-trust
        logger.error("Could not drop the task workspace", task_id=task.id, error=str(exc))
```

(call it for every mode, outside the `tdd/verify_first` condition).

`remedy.py`: add `def _forget_workspace(config, state, task_id) -> None: state.forget_task_workspace(resolve_namespace(config), task_id)` and call it at the end of `abandon` after `_record(...)`.

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_harness_trust.py tests/test_task_workspace_state.py tests/test_trusted_baseline.py tests/test_remedy.py tests/test_cli_flags.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add -A src/spec_runner tests
git commit -m "feat(harness): audited harness trust; workspace forgotten at DONE and abandon"
```

---

### Task 9: Harness edits in WIP or red never become the oracle (cross-cutting regressions)

**Files:**
- Test: `tests/test_wip_harness_not_trusted.py`

**Interfaces:**
- Consumes: everything above.

- [ ] **Step 1: Write the tests** (they should pass if Tasks 1–8 are right; any failure is a defect in those tasks, fixed there)

```python
"""A harness edit carried in WIP or a red commit is never the next oracle (spec §4)."""

import subprocess
from pathlib import Path

from spec_runner import paid_call
from spec_runner.state import ExecutorState
from spec_runner.tdd import resolve_namespace

# reuse the repo/_cfg/_task fixtures of tests/test_retry_continues_from_wip.py
from tests.test_retry_continues_from_wip import _cfg, _git, _task, repo  # noqa: F401


def test_harness_edit_in_wip_is_refused_by_the_next_invocation(repo, monkeypatch):
    def _edit_and_time_out(invocation, *, timeout, cwd, env):
        (repo / "pyproject.toml").write_text("[project]\nname='d'\n# addopts\n")
        raise subprocess.TimeoutExpired(invocation.argv, timeout)

    monkeypatch.setattr(paid_call, "_spawn", _edit_and_time_out)
    from spec_runner.execution import run_with_retries

    cfg = _cfg(repo, max_retries=1)
    with ExecutorState(cfg) as st:
        run_with_retries(_task(), cfg, st)
        first = st.get_harness_baseline(resolve_namespace(cfg), "TASK-070")

    def _idle(invocation, *, timeout, cwd, env):
        return subprocess.CompletedProcess(invocation.argv, 0, "TASK_COMPLETE\n", "")

    monkeypatch.setattr(paid_call, "_spawn", _idle)
    with ExecutorState(cfg) as st:  # a separate invocation, same DB
        st.get_task_state("TASK-070").attempts = []
        run_with_retries(_task(), cfg, st)
        again = st.get_harness_baseline(resolve_namespace(cfg), "TASK-070")
        last = st.get_task_state("TASK-070").attempts[-1]
    assert "# addopts" in _git(repo, "show", "HEAD:pyproject.toml") or "# addopts" in (
        repo / "pyproject.toml"
    ).read_text()
    assert again.captured_at == first.captured_at
    assert last.error_kind == "harness_guard"
```

Add the red-commit variant: stub `execution._run_red_phase_gate` to write `pyproject.toml`, `git add -A && git commit -m "TASK-070: red for x"`, return None, with `execution_mode="tdd"`; assert the next separate invocation refuses (`harness_guard`) and the baseline row is unchanged.

- [ ] **Step 2: Run**

Run: `uv run pytest tests/test_wip_harness_not_trusted.py -v`
Expected: PASS (fix in the owning task otherwise).

- [ ] **Step 3: Commit**

```bash
git add tests/test_wip_harness_not_trusted.py
git commit -m "test(wip): harness edits in WIP or red never become the oracle"
```

---

### Task 10: Contract, docs, version

**Files:**
- Modify: `docs/state-schema.md`, `CLAUDE.md`, `README.md`, `CHANGELOG.md`, `pyproject.toml`, `TODO.md`
- Test: existing `tests/test_json_result_contract.py`, `scripts/check_changelog_links.py`

- [ ] **Step 1: `docs/state-schema.md`** — four sections in the experimental-table format (copy the `tdd_claims` section's shape):

```markdown
### `task_workspaces` (experimental, 5.0.0)

The fact that a task started, and on which exact branch. Columns: `namespace`,
`task_id`, `branch` (NULL without per-task branches), `started_at`, `run_id`,
`bound_by` (`run` · `operator`). Key `(namespace, task_id)`. WIP is saved only
for the task whose row names the current branch exactly. Deleted at DONE and
by `tdd abandon`.

### `harness_baselines` (experimental, 5.0.0)

The harness guard's trusted baseline, taken before the first agent call.
Columns: `namespace`, `task_id`, `captured_at`, `run_id`, `guard_mode`,
`provenance` (`initial` · `operator` · `recaptured`), `surface` (JSON: every
candidate → `file` · `dir` · `absent`). Key `(namespace, task_id)`. Under
`strict` only `initial`/`operator` are trusted. Deleted at DONE and by
`tdd abandon`.

### `harness_baseline_files` (experimental, 5.0.0)

One row per file the surface held at capture. Columns: `namespace`, `task_id`,
`path`, `state` (`present` · `unreadable`), `digest` (sha256), `content` (BLOB).
Key `(namespace, task_id, path)`. Deleted with its baseline.

### `harness_trust_audit` (experimental, 5.0.0)

Append-only record of `spec-runner harness trust`. Columns: `id`, `namespace`,
`task_id`, `at`, `actor`, `reason`, `branch`, `bound_branch` (1 when the
command created the workspace binding), `replaced_provenance`. Never deleted.
```

- [ ] **Step 2: CLAUDE.md** — CLI block: `spec-runner harness trust TASK-001 --reason "..." [--bind-branch <branch>]   # Confirm a restored, checked harness as the task's trusted baseline (audited)`; module table rows for `wip.py` (~170 lines) and `harness_cmd.py` (~100 lines); `harness.py` description gains the persisted baseline. README: a short "Retries continue from WIP" paragraph and the command.

- [ ] **Step 3: CHANGELOG + version** — `pyproject.toml` `version = "5.0.0"`; CHANGELOG: rename `## [Unreleased]` content into `## [5.0.0] — <merge date>` following `docs/release-runbook.md` §1 (compare links), with:

```markdown
### Changed (breaking)

- **Retries continue from the previous attempt's work.** Under
  `create_git_branch: true` (`integration_pr` included) the uncommitted work
  of a failed attempt is saved as a `wip(TASK): … — not a candidate` commit
  before the next destructive tree switch, instead of a rescue stash. …
- **The harness baseline is persisted** (`harness_baselines`) before the first
  agent call and reused by every later attempt and invocation. …

### Migration

A task started before 5.0.0 has no trusted harness state. Under
`harness_guard: strict` its next start is refused. First restore the task's
harness files and check them (for instance against the main branch); only
then confirm them:
`spec-runner harness trust TASK-X --bind-branch <its branch> --reason "…"`.
```

- [ ] **Step 4: TODO.md** — mark `retry-continues-from-wip` done with the PR number and a one-paragraph summary.

- [ ] **Step 5: Verify**

Run: `uv run pytest tests/ -q && uv run ruff check . && uv run ruff format --check . && uv run mypy src && python scripts/check_changelog_links.py`
Expected: all pass except the known local-only `test_claims_released_at_completion` failure.

- [ ] **Step 6: Commit**

```bash
git add -A docs CLAUDE.md README.md CHANGELOG.md pyproject.toml TODO.md
git commit -m "docs: 5.0.0 — WIP retries, persisted harness baseline, harness trust"
```
