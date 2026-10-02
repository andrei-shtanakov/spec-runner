"""BEH-26: a bounded prompt/result keeps the proof of the original bytes."""

import hashlib

from spec_runner.evidence import (
    PROMPT_LIMIT,
    RESULT_LIMIT,
    bound_evidence,
    call_result_for,
)

MIB = 1024 * 1024


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


class TestPrompt:
    def test_three_mib_prompt_keeps_head_tail_and_proof(self):
        text = "HEAD" + "x" * (3 * MIB) + "\nFROZEN FILES: tests/test_a.py"
        bounded = bound_evidence(text, PROMPT_LIMIT)

        assert bounded.truncated
        assert len(bounded.text.encode()) <= MIB
        assert bounded.text.startswith("HEAD")
        assert bounded.text.endswith("FROZEN FILES: tests/test_a.py")
        assert "TRUNCATED" in bounded.text
        assert bounded.full_sha256 == _sha(text)
        assert bounded.full_size == len(text.encode())

    def test_in_budget_prompt_is_unchanged(self):
        text = "a small prompt"
        bounded = bound_evidence(text, PROMPT_LIMIT)

        assert not bounded.truncated
        assert bounded.text == text
        assert bounded.full_sha256 == _sha(bounded.text)
        assert bounded.full_size == len(bounded.text.encode())


class TestResult:
    def test_ten_mib_result_is_bounded_to_four(self):
        text = "R" * (10 * MIB)
        record = call_result_for(
            run_id="r",
            pipeline_id=None,
            call_id="c",
            provenance="green",
            outcome="ok",
            cost_usd=None,
            returncode=0,
            result=text,
            timestamp="t",
        )

        assert record.result_truncated
        assert len(record.result.encode()) <= RESULT_LIMIT
        assert record.result_full_sha256 == _sha(text)
        assert record.result_full_size == 10 * MIB
