"""#214: the passes that can violate a claim are told which files are frozen.

The byte-lock is a rule the harness invented. The pilot's GREEN pass received
exactly the prompt a `standard` task gets, wrote four more tests into the file
the red had frozen, and the claims gate refused the merge — ~$1.3 of
implementation spent on a candidate rejected for a rule the agent never saw.

Two things are proven here, and they are different:

- the block **reaches** every agent-facing prompt, including one rendered from
  a project's own template, because a constraint delivered as a template
  variable disappears for exactly the projects that customised the most;
- the instrument does not become polite. `check_claims` still decides, and it
  now decides **before** review, so a candidate that is already unmergeable
  does not buy a verdict nothing can act on.
"""

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from spec_runner.claims import (
    ESCAPE_REVIEW,
    ESCAPE_TASK,
    FROZEN_HEADER,
    Claim,
    ClaimStatus,
    append_frozen_files,
)
from spec_runner.config import ExecutorConfig
from spec_runner.hooks import post_done_hook
from spec_runner.prompt import build_red_prompt, build_task_prompt
from spec_runner.review import REVIEW_ROLES, build_review_prompt, run_parallel_review
from spec_runner.state import ExecutorState
from spec_runner.task import Task
from spec_runner.tdd import resolve_namespace

CLAIMED = "tests/test_catalog.py"


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    (root / CLAIMED).parent.mkdir(parents=True, exist_ok=True)
    (root / CLAIMED).write_text("def test_y():\n    assert False\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    return root


def _cfg(root: Path, **overrides) -> ExecutorConfig:
    defaults: dict = {
        "project_root": root,
        "state_file": root / ".state.db",
        "logs_dir": root / ".logs",
        "execution_mode": "tdd",
    }
    defaults.update(overrides)
    cfg = ExecutorConfig(**defaults)
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)
    return cfg


def _task(task_id: str = "TASK-001") -> Task:
    return Task(id=task_id, name="Catalog fallback", priority="p1", status="todo", estimate="1d")


def _blob(root: Path, path: str) -> str:
    return _git(root, "hash-object", str(root / path)).stdout.strip()


def _freeze(cfg: ExecutorConfig, path: str = CLAIMED, task_id: str = "TASK-001") -> None:
    """Record one active claim, the way a confirmed red would."""
    with ExecutorState(cfg) as state:
        state.record_claim(
            Claim(
                namespace=resolve_namespace(cfg),
                task_id=task_id,
                checkpoint_id="cp-1",
                checkpoint_sha=_git(cfg.project_root, "rev-parse", "HEAD").stdout.strip(),
                path=path,
                blob_sha=_blob(Path(cfg.project_root), path),
                created_at="2026-08-12T00:00:00",
                status=ClaimStatus.ACTIVE,
            )
        )


class TestTheBlockReachesThePrompt:
    def test_the_green_prompt_names_the_frozen_file(self, tmp_path):
        cfg = _cfg(_repo(tmp_path))
        _freeze(cfg)

        prompt = build_task_prompt(_task(), cfg)

        assert FROZEN_HEADER in prompt
        assert f"- {CLAIMED}" in prompt
        assert "TASK_BLOCKED: <reason>" in prompt

    def test_a_custom_task_template_still_carries_it(self, tmp_path):
        """The whole point of appending after rendering.

        A project template that never mentions the block is the normal case —
        it was written before the block existed. Enforcement that lived in a
        template variable would be missing exactly here, while the gate went on
        rejecting the result.
        """
        root = _repo(tmp_path)
        cfg = _cfg(root)
        cfg.prompts_dir.mkdir(parents=True)
        (cfg.prompts_dir / "task.md").write_text("Do {{TASK_ID}}. Nothing else.")
        _freeze(cfg)

        prompt = build_task_prompt(_task(), cfg)

        assert prompt.startswith("Do TASK-001. Nothing else.")
        assert FROZEN_HEADER in prompt
        assert f"- {CLAIMED}" in prompt

    def test_the_review_prompt_carries_it_with_the_review_escape(self, tmp_path):
        """A reviewer told to emit TASK_BLOCKED would produce no verdict.

        `review` parses REVIEW_PASSED/FAILED/FIXED and nothing else, so the
        implementation pass's escape hatch would read as "no marker" — and
        under `review_policy: required` that blocks the task for a reason that
        is not the truth.
        """
        cfg = _cfg(_repo(tmp_path), create_git_branch=False, auto_commit=False)
        _freeze(cfg)

        prompt = build_review_prompt(_task(), cfg)

        assert FROZEN_HEADER in prompt
        assert "REVIEW_FAILED" in prompt.split(FROZEN_HEADER)[1]
        assert "TASK_BLOCKED" not in prompt.split(FROZEN_HEADER)[1]

    def test_a_custom_review_template_still_carries_it(self, tmp_path):
        root = _repo(tmp_path)
        cfg = _cfg(root, create_git_branch=False, auto_commit=False)
        cfg.prompts_dir.mkdir(parents=True)
        (cfg.prompts_dir / "review.md").write_text("Review {{TASK_ID}}.")
        _freeze(cfg)

        prompt = build_review_prompt(_task(), cfg)

        assert prompt.startswith("Review TASK-001.")
        assert f"- {CLAIMED}" in prompt

    def test_every_parallel_role_receives_it(self, tmp_path):
        """Five roles are five agents that can each edit the tree."""
        cfg = _cfg(
            _repo(tmp_path),
            create_git_branch=False,
            auto_commit=False,
            review_parallel=True,
            review_roles=list(REVIEW_ROLES),
        )
        _freeze(cfg)
        seen: list[str] = []

        def _record(cmd, **kwargs):
            seen.append(" ".join(cmd))
            return MagicMock(returncode=0, stdout="REVIEW_PASSED", stderr="")

        with patch("spec_runner.review.subprocess.run", side_effect=_record):
            run_parallel_review(_task(), cfg)

        assert len(seen) == len(REVIEW_ROLES)
        for prompt in seen:
            assert FROZEN_HEADER in prompt
            assert CLAIMED in prompt

    def test_the_red_prompt_lists_a_neighbours_claim(self, tmp_path):
        """The authoring pass is not exempt either.

        `check_claims` judges every active claim in the namespace, whoever made
        it, so a red authored on top of another workstream's frozen test is a
        candidate the gate refuses just as surely.
        """
        cfg = _cfg(_repo(tmp_path))
        _freeze(cfg, task_id="TASK-999")

        prompt = build_red_prompt(_task("TASK-001"), cfg)

        assert f"- {CLAIMED}" in prompt

    def test_all_active_claims_are_listed_not_only_this_tasks(self, tmp_path):
        root = _repo(tmp_path)
        (root / "tests/test_other.py").write_text("def test_z():\n    assert False\n")
        _git(root, "add", "-A")
        _git(root, "commit", "-qm", "second test")
        cfg = _cfg(root)
        _freeze(cfg, task_id="TASK-001")
        _freeze(cfg, path="tests/test_other.py", task_id="TASK-002")

        prompt = build_task_prompt(_task("TASK-001"), cfg)

        assert f"- {CLAIMED}" in prompt
        assert "- tests/test_other.py" in prompt


class TestDormancy:
    def test_nothing_is_appended_when_nothing_is_frozen(self, tmp_path):
        cfg = _cfg(_repo(tmp_path))

        assert FROZEN_HEADER not in build_task_prompt(_task(), cfg)

    def test_a_standard_task_reads_no_claims_at_all(self, tmp_path):
        """Not merely "no block": no state-DB open.

        The claims gate skips a task whose mode is not `tdd`, so a lock nothing
        will check is noise — and paying for it with a database open on every
        prompt of every ordinary run is how a dormant feature stops being
        dormant (#164 criterion 8).
        """
        cfg = _cfg(_repo(tmp_path), execution_mode="standard")
        _freeze(cfg)

        with patch("spec_runner.claims.active_claim_paths") as reader:
            prompt = build_task_prompt(_task(), cfg)

        reader.assert_not_called()
        assert FROZEN_HEADER not in prompt

    def test_a_per_task_tdd_override_does_get_the_block(self, tmp_path):
        """The mode is per task, so the block follows the task, not the config."""
        cfg = _cfg(_repo(tmp_path), execution_mode="standard")
        _freeze(cfg)
        task = _task()
        task.execution_mode = "tdd"  # what `**Mode:** tdd` in tasks.md parses to

        assert FROZEN_HEADER in build_task_prompt(task, cfg)


class TestTheBlockItself:
    def test_it_is_appended_after_the_body_never_woven_into_it(self, tmp_path):
        cfg = _cfg(_repo(tmp_path))
        _freeze(cfg)

        out = append_frozen_files("BODY", cfg, _task())

        assert out.startswith("BODY\n\n")
        assert out.rstrip().endswith(ESCAPE_TASK)

    def test_the_escape_is_the_callers_choice(self, tmp_path):
        cfg = _cfg(_repo(tmp_path))
        _freeze(cfg)

        assert ESCAPE_REVIEW in append_frozen_files("BODY", cfg, _task(), escape=ESCAPE_REVIEW)

    def test_an_unreadable_state_degrades_to_no_block_not_to_a_crash(self, tmp_path):
        """Degraded, not a hole: the gate still checks the candidate commit.

        What is lost is the agent's chance to comply, so it must be visible —
        but a prompt that cannot be built is a task that cannot run at all.
        """
        cfg = _cfg(_repo(tmp_path))
        _freeze(cfg)

        with patch("spec_runner.state.ExecutorState.__init__", side_effect=RuntimeError("no db")):
            prompt = build_task_prompt(_task(), cfg)

        assert FROZEN_HEADER not in prompt


@pytest.fixture
def registered_gates():
    """The TDD gates, attached as a real `tdd` run attaches them.

    The registry is process-wide, so a suite that registers without removing
    is how order-dependent tests are born. Registering here rather than
    leaning on whatever a previous test left behind is also what caught the
    early check evaluating a gate the registry had never been asked for.
    """
    from spec_runner.gates import REGISTRY, ensure_red_gate

    ensure_red_gate()
    yield
    REGISTRY.unregister("tdd.red", "tests")
    REGISTRY.unregister("tdd.claims", "tests")


@pytest.mark.usefixtures("registered_gates")
class TestTheGateStillDecides:
    """The prompt prevents; `check_claims` refuses. The second is the authority."""

    def _ready_for_review(self, tmp_path):
        root = _repo(tmp_path)
        cfg = _cfg(
            root,
            create_git_branch=False,
            auto_commit=True,
            run_tests_on_done=False,
            run_lint_on_done=False,
            run_review=True,
        )
        _freeze(cfg)
        return root, cfg

    def test_a_violated_claim_stops_the_run_before_the_reviewer_is_paid(self, tmp_path):
        root, cfg = self._ready_for_review(tmp_path)
        # The GREEN pass edits the frozen test — the pilot's exact move.
        (root / CLAIMED).write_text(
            "def test_y():\n    assert False\n\ndef test_extra():\n    pass\n"
        )

        with patch("spec_runner.hooks.run_code_review") as review:
            success, error, verdict, _findings, _no_op = post_done_hook(_task(), cfg, True)

        review.assert_not_called()
        assert success is False
        assert "claim violated" in (error or "")
        assert CLAIMED in (error or "")
        # A review that never ran is `skipped`, never `passed`: an advisory
        # policy must not read the absence of a verdict as a good one.
        assert verdict == "skipped"

    def test_an_intact_claim_lets_review_run(self, tmp_path):
        root, cfg = self._ready_for_review(tmp_path)
        (root / "src.py").write_text("VALUE = 1\n")

        with patch("spec_runner.hooks.run_code_review") as review:
            review.return_value = (MagicMock(value="passed"), None, "ok")
            post_done_hook(_task(), cfg, True)

        review.assert_called_once()

    def test_a_standard_task_is_not_checked_early(self, tmp_path):
        root, cfg = self._ready_for_review(tmp_path)
        cfg.execution_mode = "standard"
        (root / CLAIMED).write_text("mutated\n")

        with patch("spec_runner.hooks.run_code_review") as review:
            review.return_value = (MagicMock(value="passed"), None, "ok")
            post_done_hook(_task(), cfg, True)

        review.assert_called_once()

    def test_with_review_off_the_early_check_does_not_run_at_all(self, tmp_path):
        """It buys one thing — the review call — so it costs nothing when
        there is no review call to buy (Copilot, PR #215).

        The violation is still refused, by the merge-time gate that was always
        the authority: the task does not finish, and it does not merge.
        """
        root, cfg = self._ready_for_review(tmp_path)
        cfg.run_review = False
        (root / CLAIMED).write_text("mutated\n")

        with patch("spec_runner.hooks._claims_intact_before_review") as early:
            success, error, _verdict, _findings, _no_op = post_done_hook(_task(), cfg, True)

        early.assert_not_called()
        assert success is False
        assert "claim violated" in (error or "")

    def test_an_unreadable_candidate_is_an_instrument_error_not_a_verdict(self, tmp_path):
        root, cfg = self._ready_for_review(tmp_path)
        (root / CLAIMED).write_text("mutated\n")

        from spec_runner.claims import ClaimCheckError
        from spec_runner.hooks import GATE_INSTRUMENT_ERROR_PREFIX

        with (
            patch("spec_runner.claims.check_claims", side_effect=ClaimCheckError("no tree")),
            patch("spec_runner.hooks.run_code_review") as review,
        ):
            success, error, _verdict, _findings, _no_op = post_done_hook(_task(), cfg, True)

        review.assert_not_called()
        assert success is False
        assert (error or "").startswith(GATE_INSTRUMENT_ERROR_PREFIX)


@pytest.mark.parametrize("escape", [ESCAPE_TASK, ESCAPE_REVIEW])
def test_every_escape_names_the_operator_route(escape):
    """Neither pass may resolve a claim on its own — that is a remedy."""
    assert "operator" in escape
