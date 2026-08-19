"""#301 (second half): a refusal that only a manual re-run could explain.

The reported run failed on the TDD gate after 2.5 minutes and left the caller
with `spec-runner exited with code 1`. The reason was excellent — it named the
cause *and* the fix — and it was reachable only by reproducing the run by hand
in the same worktree.

Measured before writing any of this, on the end-to-end harness of
`test_infrastructure_classification`: the text **is** on stderr, **is** in the
jsonl, and **is** in `attempts.error`. What is missing is the machine-readable
half the issue asks for by name — `error_kind` and `error_stage` — which the
RED-refusal site never passed, so `status` showed a bare failure and a consumer
reading the DB got NULLs. Recording them is opt-in per call site (both are
optional keyword arguments), and only 3 of 11 sites opted in.

Three properties, one per channel:

- **attempts**: every recorded failure names its kind and the stage it hit.
- **the log**: a failing run's last word carries the reason in full, as fields.
- **the artefact**: a process that logged nothing leaves no file to misread —
  the 0-byte jsonl the reporter opened was a different invocation's.
"""

import ast
import json
import sqlite3
import sys
from pathlib import Path

import pytest

from spec_runner.errors import ERROR_KINDS
from tests.test_infrastructure_classification import _run_cli, _tdd_project

SCHEMA = Path(__file__).resolve().parents[1] / "schemas" / "executor-state.schema.json"
EXECUTION_PY = Path(__file__).resolve().parents[1] / "src" / "spec_runner" / "execution.py"


def _attempts(root: Path) -> list[dict]:
    db = root / "spec" / ".executor-state.db"
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM attempts ORDER BY rowid")]
    finally:
        conn.close()


class TestEveryRecordedFailureNamesItsKindAndStage:
    """The issue's explicit minimum: `error_kind` / `error_stage` and the text."""

    @pytest.mark.slow
    def test_a_refused_red_records_the_gates_own_kind(self, tmp_path):
        # The reported shape: the gate answered, and the answer was no.
        root = _tdd_project(tmp_path, assertion="True", test_command=f"{sys.executable} -m pytest")

        result = _run_cli(root, "run", "--task=TASK-001")

        assert result.returncode == 1
        (row,) = _attempts(root)
        assert row["error_code"] == "HOOK_FAILURE"
        assert row["error_kind"] == "policy", "a gate said no — that is what happened"
        assert row["error_stage"] == "tests"
        assert "did not fail on replay" in row["error"]

    @pytest.mark.slow
    def test_a_broken_instrument_records_a_different_kind(self, tmp_path):
        # Same site, opposite meaning: nothing was learned about the work.
        root = _tdd_project(
            tmp_path, assertion="False", test_command=f"{sys.executable} -m pytest && true"
        )

        result = _run_cli(root, "run", "--task=TASK-001")

        assert result.returncode == 2
        (row,) = _attempts(root)
        assert row["error_code"] == "INFRASTRUCTURE"
        assert row["error_kind"] == "instrument"
        assert row["error_stage"] == "tests"

    def test_the_kind_comes_from_the_refusal_not_from_its_words(self):
        """`RefusalKind` already answers this; a second classifier would drift
        from it exactly as the prefix match did in #230."""
        from spec_runner.execution import _refusal_error_kind
        from spec_runner.phases import Refusal, RefusalKind

        assert _refusal_error_kind(Refusal("boom", RefusalKind.INSTRUMENT)) == "instrument"
        assert _refusal_error_kind(Refusal("boom", RefusalKind.POLICY)) == "policy"
        assert _refusal_error_kind(Refusal("boom", RefusalKind.BUDGET)) == "budget"
        # An untyped refusal string is still a refusal — it just cannot say
        # more than that.
        assert _refusal_error_kind("Tests failed: 3 of 40") == "hook_failure"


class TestNoFailureSiteCanForgetThem:
    """The defect was structural: both fields are optional keyword arguments,
    so a site records them only if its author remembered. Nothing checked."""

    def test_every_recorded_failure_in_execution_passes_both(self):
        tree = ast.parse(EXECUTION_PY.read_text())
        missing = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (isinstance(func, ast.Attribute) and func.attr == "record_attempt"):
                continue
            # `success` is the second positional argument.
            if len(node.args) < 2:
                continue
            success = node.args[1]
            if not (isinstance(success, ast.Constant) and success.value is False):
                continue
            names = {kw.arg for kw in node.keywords}
            if not {"error_kind", "error_stage"} <= names:
                missing.append(node.lineno)
        assert not missing, (
            f"execution.py records a failure without error_kind/error_stage at lines {missing} — "
            "a caller reading the DB gets NULLs and `status` shows a bare failure"
        )


class TestTheDeclaredVocabularyIsTheOneOnDisk:
    """The schema pinned five kinds. The code already wrote `blocked` and
    `api_error`, neither of them in that enum — a consumer validating rows
    spec-runner itself writes would have rejected them. The enum drifted
    because nothing compared it to the code."""

    def test_schema_enum_matches_the_declared_kinds(self):
        schema = json.loads(SCHEMA.read_text())
        enum = schema["definitions"]["TaskAttempt"]["properties"]["error_kind"]["enum"]
        assert set(enum) == set(ERROR_KINDS) | {None}

    def test_every_kind_the_classifier_can_return_is_declared(self):
        from spec_runner.errors import PATTERNS

        assert {p.kind for p in PATTERNS} <= set(ERROR_KINDS)

    def test_every_kind_literal_in_execution_is_declared(self):
        """Catches the drift at its source: a new site inventing a word."""
        tree = ast.parse(EXECUTION_PY.read_text())
        written = set()
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.keyword)
                and node.arg == "error_kind"
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)
            ):
                written.add(node.value.value)
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
                targets = {t.id for t in node.targets if isinstance(t, ast.Name)}
                if "error_kind" in targets and isinstance(node.value.value, str):
                    written.add(node.value.value)
        assert written, "no literal kinds found — the walk stopped working"
        assert written <= set(ERROR_KINDS), f"undeclared: {written - set(ERROR_KINDS)}"


class TestTheRunSaysWhyItFailed:
    """The log channel. `Execution summary` counted failures and named none."""

    @pytest.mark.slow
    def test_the_reason_is_in_the_log_as_fields_and_in_full(self, tmp_path):
        root = _tdd_project(tmp_path, assertion="True", test_command=f"{sys.executable} -m pytest")

        result = _run_cli(root, "run", "--task=TASK-001")

        records = [
            json.loads(line)
            for path in root.rglob("*.jsonl")
            for line in path.read_text().splitlines()
            if line.strip()
        ]
        failures = [r for r in records if r["Attributes"].get("event") == "Task failed"]
        assert failures, "the run's log never named the failure"
        attrs = failures[-1]["Attributes"]
        assert attrs["task_id"] == "TASK-001"
        assert attrs["error_kind"] == "policy"
        assert attrs["error_stage"] == "tests"
        assert attrs["error_code"] == "HOOK_FAILURE"
        # In full: the actionable half of the reported message was past any
        # truncation point ("…write the failing test in a file of its own").
        assert "did not fail on replay" in attrs["error"]
        assert not attrs["error"].endswith("...")
        assert failures[-1]["SeverityText"] == "ERROR"
        # And the same line reached the caller's stderr.
        assert "Task failed" in result.stderr

    @pytest.mark.slow
    def test_a_successful_run_says_nothing_of_the_sort(self, tmp_path):
        root = _tdd_project(tmp_path, assertion="False", test_command=f"{sys.executable} -m pytest")

        result = _run_cli(root, "run", "--task=TASK-001")

        assert result.returncode == 0, result.stderr[-2000:]
        assert "Task failed" not in result.stderr


class TestStatusShowsTheWholeReason:
    """50 characters cut the reported message mid-diagnosis: what an operator
    needs ("write the failing test in a file of its own") is at the end."""

    @pytest.mark.slow
    def test_status_prints_the_full_last_error(self, tmp_path):
        root = _tdd_project(tmp_path, assertion="True", test_command=f"{sys.executable} -m pytest")
        _run_cli(root, "run", "--task=TASK-001")

        status = _run_cli(root, "status")

        assert "did not fail on replay" in status.stdout
        assert "[policy]" in status.stdout
        assert "[at: tests]" in status.stdout


class TestAnEmptyLogFileIsNeverLeftBehind:
    """What the reporter actually opened. Every invocation created
    `logs/<new ULID>/spec-runner-<pid>.jsonl` at init — so a command that logs
    nothing (`status` does not log at all) left a 0-byte file that reads as
    "the run wrote nothing", and a run's own file sat in a different ULID
    directory."""

    def test_a_process_that_logs_nothing_creates_no_file(self, tmp_path):
        from spec_runner import obs

        obs.init_logging("probe", log_dir=tmp_path / "logs")

        assert (
            not list((tmp_path / "logs").glob("*.jsonl")) if (tmp_path / "logs").exists() else True
        )

    def test_the_file_appears_with_its_first_record(self, tmp_path):
        from spec_runner import obs

        obs.init_logging("probe", log_dir=tmp_path / "logs")
        obs.get_logger("t").info("something happened")

        (path,) = (tmp_path / "logs").glob("*.jsonl")
        assert path.stat().st_size > 0
        assert json.loads(path.read_text().splitlines()[0])["Body"] == "something happened"

    def test_concurrent_first_writes_open_the_file_once(self, tmp_path, monkeypatch):
        """`obs.py` is vendored into projects whose threading we do not control
        (Copilot, PR #303). structlog serializes writes per file object today,
        so the open is already reached by one thread at a time — but that is
        the caller's library's property, not this class's."""
        import threading

        from spec_runner.obs import _LazyFile

        real_open = Path.open
        opens = []

        def counting_open(self, *args, **kwargs):
            opens.append(self)
            return real_open(self, *args, **kwargs)

        monkeypatch.setattr(Path, "open", counting_open)

        sink = _LazyFile(tmp_path / "deep" / "probe.jsonl")
        start = threading.Barrier(8)

        def hammer() -> None:
            start.wait()
            for _ in range(25):
                sink.write("line\n")

        threads = [threading.Thread(target=hammer) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        sink.flush()

        assert len(opens) == 1, f"opened {len(opens)} handles — one of them leaks its writes"
        assert (tmp_path / "deep" / "probe.jsonl").read_text().count("line\n") == 200

    @pytest.mark.slow
    def test_a_real_command_that_logs_nothing_leaves_no_decoy(self, tmp_path):
        root = _tdd_project(tmp_path, assertion="False", test_command="true")

        _run_cli(root, "status")

        assert not list(root.rglob("*.jsonl")), "an empty log file is a decoy, not a record"
