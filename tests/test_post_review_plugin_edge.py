"""#307: a plugin edge between the review verdict and the final commit.

The gap disputatio measured on 2.34.0: the review verdict is recorded at one
place and `commit_task_work` runs at another, and between them nothing a
project owns can run. `commands.test`/`commands.lint` fire *before* the
candidate commit, the second test run is conditional on `ReviewVerdict.FIXED`
(so a task the reviewer did not touch never reaches it), and plugin `post_done`
fires after the commit **and** the merge — too late for anything it writes to
be delivered with the work.

The consequence they hit: evidence about the whole RED → GREEN → review chain
cannot be made a tracked artefact of the same pull request by any means the
contract offers.

`post_review` closes it. It fires after the verdict, after the `REFACTORING`
record and after the pre-terminal gates have *passed*, immediately before the
DONE flip and the commit — the same ordering `post_done_hook` already relies on
for `tasks.md` itself ("Persist the task's DONE status … BEFORE committing, so
it is included in the commit/merge").

Deliberately generic: nothing here is TDD-specific, and `plugins.py` is
unchanged — `run_plugin_hooks` resolves a hook point by the string in the
manifest, so the point is a call site and nothing else.

Contract: issue #307.
"""

import stat
import subprocess
from pathlib import Path

import yaml

from spec_runner.config import ExecutorConfig
from spec_runner.task import Task

TASKS = """\
# Tasks

### TASK-001: first
🟠 P1 | 🔄 IN_PROGRESS
Est: 1d

- [ ] a
"""


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir(parents=True, exist_ok=True)
    _git(root.parent, "init", "-q", str(root))
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    (root / "spec").mkdir()
    (root / "spec" / "tasks.md").write_text(TASKS)
    (root / "spec" / ".gitignore").write_text(".executor-*\n.*task-history.log\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "spec")
    return root


def _plugin(root: Path, name: str, hooks: dict) -> Path:
    plugin_dir = root / "spec" / "plugins" / name
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.yaml").write_text(
        yaml.dump({"name": name, "description": name, "version": "1.0", "hooks": hooks})
    )
    return plugin_dir


def _script(plugin_dir: Path, name: str, body: str) -> None:
    script = plugin_dir / name
    script.write_text(body)
    script.chmod(script.stat().st_mode | stat.S_IEXEC)


def _cfg(root: Path, **overrides) -> ExecutorConfig:
    defaults: dict = {
        "project_root": root,
        "state_file": root / "spec" / ".executor-state.db",
        "logs_dir": root / "spec" / ".logs",
        "plugins_dir": root / "spec" / "plugins",
        "create_git_branch": False,
        "run_tests_on_done": False,
        "run_lint_on_done": False,
        "run_review": True,
        "auto_commit": True,
    }
    defaults.update(overrides)
    cfg = ExecutorConfig(**defaults)
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)
    return cfg


def _task() -> Task:
    return Task(id="TASK-001", name="first", priority="p1", status="in_progress", estimate="1d")


def _stub_review(monkeypatch, verdict=None):
    from spec_runner import hooks
    from spec_runner.state import ReviewVerdict

    verdict = verdict or ReviewVerdict.PASSED
    monkeypatch.setattr(hooks, "run_code_review", lambda *a, **k: (verdict, None, "ok"))


def _subjects(root: Path) -> list[str]:
    return _git(root, "log", "--format=%s").stdout.strip().splitlines()


class TestWhatThePluginWritesIsDelivered:
    """The acceptance criterion in the words the request used: what the plugin
    puts in the working tree travels with the work."""

    def test_the_file_it_wrote_is_in_the_commit(self, tmp_path, monkeypatch):
        from spec_runner import hooks

        root = _repo(tmp_path)
        plugin_dir = _plugin(root, "evidence", {"post_review": {"command": "./export.sh"}})
        _script(
            plugin_dir,
            "export.sh",
            '#!/bin/bash\nmkdir -p "$SR_PROJECT_ROOT/spec/.tdd-evidence"\n'
            'printf \'{"task":"%s"}\\n\' "$SR_TASK_ID" '
            '> "$SR_PROJECT_ROOT/spec/.tdd-evidence/$SR_TASK_ID.json"\n',
        )
        _stub_review(monkeypatch)

        ok, error, *_ = hooks.post_done_hook(_task(), _cfg(root), True)

        assert ok is True, error
        committed = _git(root, "show", "HEAD:spec/.tdd-evidence/TASK-001.json").stdout
        assert '"task":"TASK-001"' in committed

    def test_it_gets_the_same_environment_the_other_hooks_get(self, tmp_path, monkeypatch):
        from spec_runner import hooks

        root = _repo(tmp_path)
        plugin_dir = _plugin(root, "env", {"post_review": {"command": "./env.sh"}})
        _script(
            plugin_dir,
            "env.sh",
            '#!/bin/bash\nprintf "%s|%s|%s\\n" "$SR_TASK_ID" "$SR_TASK_NAME" '
            '"$SR_TASK_STATUS" > "$SR_PROJECT_ROOT/env.txt"\n',
        )
        _stub_review(monkeypatch)

        hooks.post_done_hook(_task(), _cfg(root), True)

        assert (root / "env.txt").read_text().strip() == "TASK-001|first|success"

    def test_run_on_failure_never_fires_here(self, tmp_path, monkeypatch):
        """The point exists only on the success path, so `on_failure` is a
        hook that can never run — and must not run *because* of the status
        this site reports."""
        from spec_runner import hooks

        root = _repo(tmp_path)
        plugin_dir = _plugin(
            root, "never", {"post_review": {"command": "./x.sh", "run_on": "on_failure"}}
        )
        _script(plugin_dir, "x.sh", '#!/bin/bash\ntouch "$SR_PROJECT_ROOT/ran.txt"\n')
        _stub_review(monkeypatch)

        hooks.post_done_hook(_task(), _cfg(root), True)

        assert not (root / "ran.txt").exists()


class TestWhereInTheOrderItSits:
    """Not "somewhere after review" — one place, and the tests say which."""

    def _timeline(self, tmp_path, monkeypatch, *, gate_status=None, verdict=None):
        from spec_runner import gates as gates_mod
        from spec_runner import hooks
        from spec_runner.gates import GateRegistry, GateResult, GateStatus
        from spec_runner.state import PhaseOutcome, ReviewVerdict

        root = _repo(tmp_path)
        plugin_dir = _plugin(root, "probe", {"post_review": {"command": "./p.sh"}})
        _script(plugin_dir, "p.sh", '#!/bin/bash\ntouch "$SR_PROJECT_ROOT/plugin_ran"\n')

        events: list[str] = []

        def _review(*a, **k):
            events.append("review")
            return (verdict or ReviewVerdict.PASSED, None, "ok")

        monkeypatch.setattr(hooks, "run_code_review", _review)

        real_phase = hooks._record_tdd_phase

        def _phase(config, task, phase, detail=None):
            events.append(f"phase:{phase.name}")
            return real_phase(config, task, phase, detail)

        monkeypatch.setattr(hooks, "_record_tdd_phase", _phase)

        real_plugins = hooks.run_plugin_hooks_for

        def _plugins(event, *a, **k):
            events.append(f"plugin:{event}")
            return real_plugins(event, *a, **k)

        monkeypatch.setattr(hooks, "run_plugin_hooks_for", _plugins)

        real_status = hooks.update_task_status

        def _status(path, task_id, status):
            events.append(f"status:{status}")
            return real_status(path, task_id, status)

        monkeypatch.setattr(hooks, "update_task_status", _status)

        real_commit = hooks.commit_task_work

        def _commit(task, config):
            events.append("commit")
            return real_commit(task, config)

        monkeypatch.setattr(hooks, "commit_task_work", _commit)

        registry = GateRegistry()
        if gate_status is not None:

            def _evaluate(ctx):
                events.append("gate")
                outcome = (
                    PhaseOutcome.PASS
                    if gate_status is GateStatus.SATISFIED
                    else PhaseOutcome.UNEXPECTED_FAIL
                )
                return GateResult(gate_status, outcome, "judged in a test")

            registry.register("probe", "review", _evaluate)
        monkeypatch.setattr(gates_mod, "REGISTRY", registry)

        cfg = _cfg(root)
        result = hooks.post_done_hook(_task(), cfg, True)
        return root, cfg, result, events

    def test_it_runs_after_the_verdict_and_before_the_done_flip(self, tmp_path, monkeypatch):
        _root, _cfg_, _result, events = self._timeline(tmp_path, monkeypatch)

        assert events.index("review") < events.index("plugin:post_review")
        assert events.index("plugin:post_review") < events.index("status:done")

    def test_the_final_commit_is_still_ahead_of_it(self, tmp_path, monkeypatch):
        """`commit_task_work` runs twice — the pre-review candidate carries the
        work, the final one carries the bookkeeping. The edge sits between them,
        so the commit that sweeps up its write is the *final* one."""
        _root, _cfg_, _result, events = self._timeline(tmp_path, monkeypatch)

        edge = events.index("plugin:post_review")
        commits = [i for i, e in enumerate(events) if e == "commit"]
        assert len(commits) == 2, events
        assert commits[0] < edge < commits[1]

    def test_it_runs_after_the_refactoring_record(self, tmp_path, monkeypatch):
        from spec_runner.gates import GateStatus

        _root, _cfg_, _result, events = self._timeline(
            tmp_path, monkeypatch, gate_status=GateStatus.SATISFIED
        )

        assert events.index("phase:REFACTORING") < events.index("plugin:post_review")

    def test_it_runs_only_after_the_gates_have_passed(self, tmp_path, monkeypatch):
        from spec_runner.gates import GateStatus

        _root, _cfg_, _result, events = self._timeline(
            tmp_path, monkeypatch, gate_status=GateStatus.SATISFIED
        )

        assert events.index("gate") < events.index("plugin:post_review")

    def test_a_blocking_gate_means_it_never_runs(self, tmp_path, monkeypatch):
        """A gate said the work must not proceed. Exporting evidence about it
        as though it had is exactly the artefact-reads-as-finished defect the
        gates exist to prevent."""
        from spec_runner.gates import GateStatus

        root, _cfg_, (ok, _error, *_), events = self._timeline(
            tmp_path, monkeypatch, gate_status=GateStatus.UNSATISFIED
        )

        assert ok is False
        assert "plugin:post_review" not in events
        assert not (root / "plugin_ran").exists()

    def test_a_rejecting_hitl_gate_means_it_never_runs(self, tmp_path, monkeypatch):
        from spec_runner import hooks
        from spec_runner.state import ReviewVerdict

        root = _repo(tmp_path)
        plugin_dir = _plugin(root, "probe", {"post_review": {"command": "./p.sh"}})
        _script(plugin_dir, "p.sh", '#!/bin/bash\ntouch "$SR_PROJECT_ROOT/plugin_ran"\n')
        _stub_review(monkeypatch)
        monkeypatch.setattr(hooks, "prompt_hitl_verdict", lambda: "reject")

        ok, _error, verdict, *_ = hooks.post_done_hook(_task(), _cfg(root, hitl_review=True), True)

        assert ok is False and verdict == ReviewVerdict.REJECTED.value
        assert not (root / "plugin_ran").exists()


class TestABlockingFailureStaysResumable:
    """Same shape as every other pre-terminal refusal: the task is not done,
    nothing is merged, and the harness-written status flip is committed so the
    next run does not meet the dirty-spec guard."""

    def _run(self, tmp_path, monkeypatch, **cfg_overrides):
        from spec_runner import hooks

        root = _repo(tmp_path)
        plugin_dir = _plugin(
            root, "breaks", {"post_review": {"command": "./fail.sh", "blocking": True}}
        )
        _script(
            plugin_dir,
            "fail.sh",
            '#!/bin/bash\necho "exporter broke" >&2\n'
            'printf "half\\n" > "$SR_PROJECT_ROOT/partial.json"\nexit 1\n',
        )
        _stub_review(monkeypatch)
        cfg = _cfg(root, **cfg_overrides)
        return root, cfg, hooks.post_done_hook(_task(), cfg, True)

    def test_the_task_is_not_marked_done(self, tmp_path, monkeypatch):
        root, _cfg_, (ok, error, *_) = self._run(tmp_path, monkeypatch)

        assert ok is False
        assert "breaks" in (error or "")
        assert "✅ DONE" not in (root / "spec" / "tasks.md").read_text()

    def test_the_status_flip_is_committed_so_the_next_run_starts(self, tmp_path, monkeypatch):
        from spec_runner.git_ops import spec_dirty_paths

        root, cfg, (ok, _error, *_) = self._run(tmp_path, monkeypatch)

        assert ok is False
        assert spec_dirty_paths(cfg) == [], "the deadlock: a dirty spec after a blocked stop"
        assert "🔍 REVIEW" in _git(root, "show", "HEAD:spec/tasks.md").stdout

    def test_the_half_written_evidence_is_not_committed(self, tmp_path, monkeypatch):
        """The exporter failed, so what it left behind is not evidence. The
        bookkeeping commit carries the status line and nothing else."""
        root, _cfg_, _result = self._run(tmp_path, monkeypatch)

        assert (root / "partial.json").exists(), "left in the tree for the operator to see"
        names = _git(root, "show", "--name-only", "--format=", "HEAD").stdout.split()
        assert names == ["spec/tasks.md"]

    def test_a_non_blocking_failure_does_not_stop_the_task(self, tmp_path, monkeypatch):
        from spec_runner import hooks

        root = _repo(tmp_path)
        plugin_dir = _plugin(root, "noisy", {"post_review": {"command": "./fail.sh"}})
        _script(plugin_dir, "fail.sh", "#!/bin/bash\nexit 1\n")
        _stub_review(monkeypatch)

        ok, error, *_ = hooks.post_done_hook(_task(), _cfg(root), True)

        assert ok is True, error
        assert "✅ DONE" in _git(root, "show", "HEAD:spec/tasks.md").stdout


class TestNothingChangesForAProjectThatDeclaresNone:
    """Dormancy: a project without the hook cannot tell the call site is
    there."""

    def test_a_run_without_plugins_is_unchanged(self, tmp_path, monkeypatch):
        from spec_runner import hooks

        root = _repo(tmp_path)
        _stub_review(monkeypatch)
        before = len(_subjects(root))

        ok, error, *_ = hooks.post_done_hook(_task(), _cfg(root), True)

        assert ok is True, error
        assert len(_subjects(root)) == before + 1, "one commit, as before"
        assert "✅ DONE" in _git(root, "show", "HEAD:spec/tasks.md").stdout

    def test_a_plugin_with_only_post_done_is_untouched(self, tmp_path, monkeypatch):
        from spec_runner import hooks

        root = _repo(tmp_path)
        plugin_dir = _plugin(root, "legacy", {"post_done": {"command": "./done.sh"}})
        _script(plugin_dir, "done.sh", '#!/bin/bash\ntouch "$SR_PROJECT_ROOT/after_merge"\n')
        _stub_review(monkeypatch)

        ok, error, *_ = hooks.post_done_hook(_task(), _cfg(root), True)

        assert ok is True, error
        assert (root / "after_merge").exists()
        # post_done still runs after the commit — its write is not in it.
        names = _git(root, "show", "--name-only", "--format=", "HEAD").stdout.split()
        assert "after_merge" not in names


class TestTheGenericPluginApiIsUnchanged:
    def test_plugins_module_declares_no_hook_point_vocabulary(self):
        """`run_plugin_hooks` resolves the point by the manifest string, so a
        new point is a call site and nothing else. If this ever becomes a
        whitelist, `post_review` has to be added to it — and this test is the
        reminder."""
        import inspect

        from spec_runner import plugins

        source = inspect.getsource(plugins)
        assert "post_review" not in source

    def test_an_arbitrary_point_still_resolves(self, tmp_path):
        from spec_runner.plugins import discover_plugins, run_plugin_hooks

        root = _repo(tmp_path)
        plugin_dir = _plugin(root, "any", {"post_review": {"command": "./p.sh"}})
        _script(plugin_dir, "p.sh", "#!/bin/bash\nexit 0\n")

        results = run_plugin_hooks(
            "post_review",
            discover_plugins(plugin_dir.parent),
            task_env={"SR_TASK_ID": "TASK-001"},
        )

        assert results == [("any", True, False)]
