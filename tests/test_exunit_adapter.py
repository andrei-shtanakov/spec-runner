"""#198 build order §3: the ExUnit adapter, against a real `mix` project.

The acceptance matrix from the issue, plus the three rows measurement added.
Every case runs the real toolchain — a fake `mix` would be a fake of exactly
the behaviour under test, and the behaviour under test is the surprising part:

    mix test test/x_test.exs:999    →  runs the LAST test in the file,
                                       prints "1 test, 1 failure", exits 2

So an out-of-range line looks exactly like a confirmed red. Nothing but the
real runner would have shown that, and nothing but the real runner can show it
has stopped happening.

These are marked `slow` and skip when `elixir`/`mix` are absent — locally.
The dedicated CI job sets `SPEC_RUNNER_REQUIRE_EXUNIT=1`, which turns that skip
into a collection error, because a green suite that quietly tested nothing is
the same class of problem as everything else in this issue. The guard lives
here rather than in the workflow: a check that counts how many tests ran is one
more thing to keep in step with the file it is counting.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from spec_runner.config import ExecutorConfig
from spec_runner.tdd import RedOutcome, verify_red

_TOOLCHAIN_MISSING = shutil.which("mix") is None or shutil.which("elixir") is None

#: Set by the dedicated CI job. With it, a missing toolchain is a **collection
#: error** rather than a skip — the guard lives here, next to the tests, rather
#: than in a workflow counting how many of them ran.
REQUIRED = os.environ.get("SPEC_RUNNER_REQUIRE_EXUNIT") == "1"

if _TOOLCHAIN_MISSING and REQUIRED:
    raise RuntimeError(
        "SPEC_RUNNER_REQUIRE_EXUNIT=1 but `mix`/`elixir` are not on PATH — the "
        "ExUnit contract matrix must run, and a skipped matrix is a green suite "
        "that checked nothing"
    )

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(
        _TOOLCHAIN_MISSING,
        reason="the ExUnit contract matrix needs a real Elixir toolchain",
    ),
]

TESTS = """\
defmodule ProbeTest do
  use ExUnit.Case

  test "passes" do
    assert 1 == 1
  end

  test "fails" do
    assert 1 == 2
  end

  test "calls missing module" do
    assert Missing.thing() == 1
  end
end
"""

#: Definition lines in TESTS, measured by Elixir itself in the fixture below.
PASSES, FAILS, MISSING_MODULE = 4, 8, 12


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


@pytest.fixture(scope="module")
def project(tmp_path_factory) -> Path:
    """A real mix project, committed — the replay checks out a commit."""
    root = tmp_path_factory.mktemp("exunit") / "probe"
    subprocess.run(
        ["mix", "new", "probe", "--app", "probe"],
        cwd=root.parent,
        capture_output=True,
        text=True,
        check=True,
    )
    (root / "test" / "probe_test.exs").write_text(TESTS)
    _git(root.parent, "init", "-q", str(root))
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    return root


def _cfg(root: Path) -> ExecutorConfig:
    cfg = ExecutorConfig(
        project_root=root,
        state_file=root / ".state.db",
        logs_dir=root / ".logs",
        test_command="mix test",
        tdd_runner="exunit",
    )
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)
    return cfg


def _head(root: Path) -> str:
    return _git(root, "rev-parse", "HEAD").stdout.strip()


def _verify(root: Path, selector: str):
    sha = _head(root)
    return verify_red(_cfg(root), sha=sha, selector=selector, baseline_sha=sha)


class TestElixirIsTheAuthorityOnWhereTestsAre:
    def test_the_definition_lines_come_from_elixirs_own_parser(self, project):
        from pathlib import PurePosixPath

        from spec_runner.tdd_runners import definition_lines

        assert definition_lines(project, PurePosixPath("test/probe_test.exs")) == [
            PASSES,
            FAILS,
            MISSING_MODULE,
        ]


class TestTheAcceptanceMatrix:
    def test_a_passing_test_is_not_red(self, project):
        assert _verify(project, f"test/probe_test.exs:{PASSES}").outcome is RedOutcome.NOT_RED

    def test_an_assertion_failure_is_a_confirmed_red(self, project):
        assert _verify(project, f"test/probe_test.exs:{FAILS}").outcome is RedOutcome.EXPECTED_FAIL

    def test_a_missing_module_in_a_real_run_is_a_confirmed_red(self, project):
        """The ordinary write-the-test-first shape. Elixir treats an undefined
        module as a compile-time *warning* and a runtime error, so the test
        genuinely runs and genuinely fails — unlike a file that will not
        compile, which never reaches ExUnit at all."""
        result = _verify(project, f"test/probe_test.exs:{MISSING_MODULE}")
        assert result.outcome is RedOutcome.EXPECTED_FAIL

    def test_a_nonexistent_file_is_unverifiable(self, project):
        assert _verify(project, "test/nope_test.exs:3").outcome is RedOutcome.UNVERIFIABLE

    def test_a_pytest_style_selector_is_refused_before_anything_runs(self, project):
        result = _verify(project, "test/probe_test.exs::calls missing module")
        assert result.outcome is RedOutcome.UNVERIFIABLE
        assert "path:line" in (result.detail or "")

    def test_a_line_past_the_end_of_the_file_is_unverifiable(self, project):
        """The case that looks most like success: ExUnit runs the *last* test
        in the file and reports an ordinary "1 test, 1 failure"."""
        result = _verify(project, "test/probe_test.exs:999")
        assert result.outcome is RedOutcome.UNVERIFIABLE
        assert "not a `test" in (result.detail or "")

    def test_a_line_before_the_first_test_is_unverifiable(self, project):
        assert _verify(project, "test/probe_test.exs:1").outcome is RedOutcome.UNVERIFIABLE

    def test_a_line_inside_a_test_body_is_unverifiable(self, project):
        """Accepted consequence of requiring the definition line: ExUnit would
        resolve this correctly, and a rule that accepts "near enough" cannot
        tell it apart from `:999`."""
        result = _verify(project, f"test/probe_test.exs:{FAILS + 1}")
        assert result.outcome is RedOutcome.UNVERIFIABLE


class TestACompileErrorIsNotARed:
    def test_a_file_that_does_not_compile_is_unverifiable(self, project, tmp_path):
        """Exit 1 with `Compilation error in file` and **no run summary** — the
        structural difference from a runtime failure, and the reason `classify`
        keys on the summary rather than on message text."""
        broken = project / "test" / "broken_test.exs"
        broken.write_text(
            'defmodule B do\n  use ExUnit.Case\n  test "x" do\n    assert (1 ==\n  end\nend\n'
        )
        _git(project, "add", "-A")
        _git(project, "commit", "-qm", "broken")
        try:
            result = _verify(project, "test/broken_test.exs:3")
            assert result.outcome is RedOutcome.UNVERIFIABLE
        finally:
            broken.unlink()
            _git(project, "add", "-A")
            _git(project, "commit", "-qm", "remove broken")

    def test_an_unparseable_file_is_caught_at_preflight(self, project):
        """Before `mix` is invoked at all: the AST is what refuses."""
        from pathlib import PurePosixPath

        from spec_runner.tdd_runners import definition_lines

        bad = project / "test" / "unparseable_test.exs"
        bad.write_text('defmodule U do\n  test "x" do\n    assert (1 ==\n  end\nend\n')
        try:
            assert definition_lines(project, PurePosixPath("test/unparseable_test.exs")) == (
                "unparseable_test_file"
            )
        finally:
            bad.unlink()


class TestTheProofIsTheTrace:
    """The proof of selection is `--trace`'s per-test **timed** entry, which
    carries the definition line. Two measurements shaped this:

    1. Counting the summary instead disagreed between Elixir 1.18 and 1.19 —
       the CI job caught it, the local run did not.
    2. `--trace` prints a *start* line for every test and a *result* line for
       each; only the result carries a timing. Reading "no (excluded)" as
       executed marked all three tests as run and refuted everything.
    """

    def test_the_trace_names_the_line_that_actually_ran(self, project):
        from spec_runner.tdd_runners import ExUnitAdapter, Selector

        adapter = ExUnitAdapter()
        selector = adapter.parse_selector(f"test/probe_test.exs:{FAILS}")
        assert isinstance(selector, Selector)
        argv = adapter.build_command("mix test", selector)
        assert "--trace" in argv, "the proof depends on it"
        result = subprocess.run(argv, cwd=project, capture_output=True, text=True)
        assert f"[L#{FAILS}]" in result.stdout

    def test_an_out_of_range_line_is_refuted_by_the_trace(self, project):
        """`:999` runs the last test in the file. The trace says `[L#12]`, so
        the claim is refuted outright instead of inferred from a count."""
        from spec_runner.tdd_runners import ExUnitAdapter, SelectionProof, Selector

        adapter = ExUnitAdapter()
        selector = adapter.parse_selector("test/probe_test.exs:999")
        assert isinstance(selector, Selector)
        argv = adapter.build_command("mix test", selector)
        result = subprocess.run(argv, cwd=project, capture_output=True, text=True)
        assert adapter.prove_selected(selector, result) is SelectionProof.REFUTED

    def test_a_start_line_alone_is_not_an_execution(self, project):
        """Pinning the second measurement: every test gets a start line, and
        only the timed result means it ran."""
        from spec_runner.tdd_runners import ExUnitAdapter, SelectionProof, Selector

        adapter = ExUnitAdapter()
        selector = adapter.parse_selector(f"test/probe_test.exs:{PASSES}")
        assert isinstance(selector, Selector)
        starts_only = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=(
                f"  * test passes [L#{PASSES}]\n"
                f"  * test passes (0.00ms) [L#{PASSES}]\n"
                f"  * test fails [L#{FAILS}]\n"
                f"  * test fails (excluded) [L#{FAILS}]\n"
            ),
            stderr="",
        )
        assert adapter.prove_selected(selector, starts_only) is SelectionProof.PROVEN


class TestTheProofIsAboutTheRequestedTest:
    def test_the_failure_location_must_match(self, project):
        """A red confirmed at line 8 must be the test defined at line 8. This
        is the second guard behind preflight, and it fails differently: one
        refuses before the run, the other refutes after it."""
        from spec_runner.tdd_runners import ExUnitAdapter, Selector

        adapter = ExUnitAdapter()
        selector = adapter.parse_selector(f"test/probe_test.exs:{FAILS}")
        assert isinstance(selector, Selector)
        elsewhere = subprocess.CompletedProcess(
            args=[],
            returncode=2,
            stdout="  1) test other (ProbeTest)\n     test/probe_test.exs:12\n1 test, 1 failure\n",
            stderr="",
        )
        from spec_runner.tdd_runners import SelectionProof

        assert adapter.prove_selected(selector, elsewhere) is SelectionProof.REFUTED


class TestPreflightRefusalsAreDistinct:
    """Raised in review: one code for two causes sends an operator to fix the
    wrong thing. A missing toolchain and a preflight that did not complete are
    both refusals, and they are not the same refusal."""

    def test_a_missing_toolchain_says_so(self, project, monkeypatch):
        import spec_runner.tdd_runners as runners

        def _no_elixir(*_a, **_k):
            raise FileNotFoundError("elixir")

        monkeypatch.setattr(runners.subprocess, "run", _no_elixir)
        from pathlib import PurePosixPath

        assert runners.definition_lines(project, PurePosixPath("test/probe_test.exs")) == (
            "runner_toolchain_missing"
        )

    def test_a_timeout_does_not_claim_the_toolchain_is_missing(self, project, monkeypatch):
        import spec_runner.tdd_runners as runners

        def _times_out(*_a, **_k):
            raise subprocess.TimeoutExpired(cmd="elixir", timeout=1)

        monkeypatch.setattr(runners.subprocess, "run", _times_out)
        from pathlib import PurePosixPath

        code = runners.definition_lines(project, PurePosixPath("test/probe_test.exs"))
        assert code == "preflight_failed"
        assert "PATH" not in runners._PREFLIGHT_MESSAGES[code]


class TestTheReplayEnvironment:
    """#207, the invariants the owner required. Every one of them is measured
    against a real `mix`, because the defect they answer was invisible to
    reasoning: a `git worktree` carries tracked files only, and Elixir keeps
    `deps/` and `_build/` inside the project.
    """

    def _prepared(self, project: Path, worktree: Path):
        from spec_runner.tdd_runners import ExUnitAdapter, Selector

        adapter = ExUnitAdapter()
        selector = adapter.parse_selector(f"test/probe_test.exs:{FAILS}")
        assert isinstance(selector, Selector)
        return adapter, adapter.prepare_replay(project, worktree, selector)

    def test_the_build_is_private_new_and_under_the_canonical_build(self, project):
        from spec_runner.tdd_runners import REPLAY_BUILD_PREFIX, ReplayEnvironment

        _adapter, prepared = self._prepared(project, project)
        assert isinstance(prepared, ReplayEnvironment)
        build = Path(prepared.env["MIX_BUILD_PATH"])
        assert build.parent == (project / "_build").resolve()
        assert build.name.startswith(REPLAY_BUILD_PREFIX)
        assert prepared.cleanup_paths == (build,)
        shutil.rmtree(build, ignore_errors=True)

    def test_the_canonical_build_is_never_the_replay_build(self, project):
        from spec_runner.tdd_runners import ReplayEnvironment

        _adapter, prepared = self._prepared(project, project)
        assert isinstance(prepared, ReplayEnvironment)
        assert Path(prepared.env["MIX_BUILD_PATH"]).name != "test"
        assert "test" not in Path(prepared.env["MIX_BUILD_PATH"]).parts[-1:]
        shutil.rmtree(prepared.env["MIX_BUILD_PATH"], ignore_errors=True)

    def test_two_replays_get_different_build_paths(self, project):
        """Two workstreams replaying at once must not share compile state."""
        from spec_runner.tdd_runners import ReplayEnvironment

        _a, first = self._prepared(project, project)
        _b, second = self._prepared(project, project)
        assert isinstance(first, ReplayEnvironment) and isinstance(second, ReplayEnvironment)
        assert first.env["MIX_BUILD_PATH"] != second.env["MIX_BUILD_PATH"]
        for prepared in (first, second):
            shutil.rmtree(prepared.env["MIX_BUILD_PATH"], ignore_errors=True)

    def test_the_environment_identity_records_the_toolchain(self, project):
        """The same lock compiled by a different Elixir is a different
        environment, and a verdict that changes with a toolchain upgrade should
        be visible as one."""
        from spec_runner.tdd_runners import ReplayEnvironment

        _adapter, prepared = self._prepared(project, project)
        assert isinstance(prepared, ReplayEnvironment)
        for field in (
            "runner=exunit",
            "mix.lock=",
            "elixir=",
            "otp=",
            "mix_env=test",
            "deps_source=",
        ):
            assert field in prepared.environment_id, prepared.environment_id
        shutil.rmtree(prepared.env["MIX_BUILD_PATH"], ignore_errors=True)

    def test_a_declared_but_uninstalled_dependency_is_refused(self, project, tmp_path):
        """`mix deps` is the authority, not the existence of a directory. No
        network: the check reports status, it does not fetch."""
        from spec_runner.tdd_runners import ReplayEnvironmentRefusal

        worktree = tmp_path / "declared"
        shutil.copytree(project, worktree, ignore=shutil.ignore_patterns("_build", ".git"))
        mix_exs = worktree / "mix.exs"
        mix_exs.write_text(
            mix_exs.read_text().replace(
                "defp deps do\n    [\n", 'defp deps do\n    [\n      {:jason, "~> 1.4"},\n'
            )
        )
        _adapter, prepared = self._prepared(project, worktree)
        assert isinstance(prepared, ReplayEnvironmentRefusal)
        assert prepared.code == "environment_unavailable"
        assert "will not fetch" in prepared.message

    def test_nothing_is_fetched(self, project, monkeypatch):
        """The refusal must never become a download inside a gate."""
        import spec_runner.tdd_runners as runners

        seen: list[list[str]] = []
        real = runners.subprocess.run

        def _record(argv, *a, **k):
            if isinstance(argv, list):
                seen.append(argv)
            return real(argv, *a, **k)

        monkeypatch.setattr(runners.subprocess, "run", _record)
        _adapter, prepared = self._prepared(project, project)
        assert seen, "nothing ran at all"
        for argv in seen:
            joined = " ".join(argv)
            assert "deps.get" not in joined and "hex" not in joined, joined
        if hasattr(prepared, "env"):
            shutil.rmtree(prepared.env["MIX_BUILD_PATH"], ignore_errors=True)


@pytest.mark.slow
class TestTheReplayLeavesNothingBehind:
    def test_the_canonical_project_stays_clean_and_the_build_is_removed(self, project):
        """Success path: after a real replay the project is byte-clean by git's
        own account, and no replay build survives."""
        from spec_runner.tdd_runners import REPLAY_BUILD_PREFIX

        before = _git(project, "status", "--porcelain").stdout
        result = _verify(project, f"test/probe_test.exs:{FAILS}")
        assert result.outcome is RedOutcome.EXPECTED_FAIL
        assert _git(project, "status", "--porcelain").stdout == before
        leftovers = list((project / "_build").glob(f"{REPLAY_BUILD_PREFIX}*"))
        assert leftovers == [], leftovers

    def test_a_crashed_replay_leaves_a_build_that_the_next_one_does_not_reuse(self, project):
        """A build left by a killed process is state the next replay could
        read. It gets its own path regardless — and the stale one is visible
        rather than silently inherited."""
        from spec_runner.tdd_runners import REPLAY_BUILD_PREFIX, ReplayEnvironment

        stale = project / "_build" / f"{REPLAY_BUILD_PREFIX}stale"
        stale.mkdir(parents=True, exist_ok=True)
        try:
            from spec_runner.tdd_runners import ExUnitAdapter, Selector

            adapter = ExUnitAdapter()
            selector = adapter.parse_selector(f"test/probe_test.exs:{FAILS}")
            assert isinstance(selector, Selector)
            prepared = adapter.prepare_replay(project, project, selector)
            assert isinstance(prepared, ReplayEnvironment)
            assert Path(prepared.env["MIX_BUILD_PATH"]) != stale
            shutil.rmtree(prepared.env["MIX_BUILD_PATH"], ignore_errors=True)
        finally:
            shutil.rmtree(stale, ignore_errors=True)

    def test_a_failing_replay_still_removes_its_build(self, project, monkeypatch):
        """Timeout and crash take the same path as success."""
        import spec_runner.tdd as tdd_mod
        from spec_runner.tdd_runners import REPLAY_BUILD_PREFIX

        def _explode(*_a, **_k):
            raise subprocess.TimeoutExpired(cmd="mix", timeout=1)

        monkeypatch.setattr(tdd_mod, "_run_selector", _explode)
        result = _verify(project, f"test/probe_test.exs:{FAILS}")
        assert result.outcome is RedOutcome.UNVERIFIABLE
        assert list((project / "_build").glob(f"{REPLAY_BUILD_PREFIX}*")) == []
