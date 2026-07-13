"""M0: per-stage rules + project context injection into generation prompts.

Ports OpenSpec's ``config.yaml`` ``context``/``rules`` idea: a project-wide
``spec_context`` prepended to every generation stage, and per-stage
``spec_rules`` injected only for the matching stage. Wrapped in
``<context>``/``<rules>`` tags. The default path (no config) must produce
byte-identical prompts to the pre-M0 behaviour.
"""

from pathlib import Path

import pytest

from spec_runner.config import ExecutorConfig, build_config, load_config_from_yaml
from spec_runner.prompt import build_gated_generation_prompt, build_generation_prompt
from spec_runner.validate import validate_config


def _build_args(**overrides):
    from argparse import Namespace

    defaults = {
        "max_retries": None,
        "timeout": None,
        "no_tests": False,
        "no_branch": False,
        "no_commit": False,
        "no_review": False,
        "callback_url": "",
        "spec_prefix": "",
        "project_root": None,
        "max_concurrent": 0,
        "budget": None,
        "task_budget": None,
        "hitl_review": False,
        "log_level": None,
    }
    defaults.update(overrides)
    return Namespace(**defaults)


class TestDefaultUnchanged:
    """No context/rules → prompts are byte-identical to the pre-M0 output."""

    def test_generation_prompt_unchanged(self):
        base = build_generation_prompt("requirements", "DESC")
        injected = build_generation_prompt(
            "requirements", "DESC", spec_context=None, spec_rules=None
        )
        assert base == injected
        assert "<context>" not in base
        assert "<rules>" not in base

    def test_gated_prompt_unchanged(self):
        base = build_gated_generation_prompt("requirements", "DESC", {})
        injected = build_gated_generation_prompt(
            "requirements", "DESC", {}, spec_context="", spec_rules={}
        )
        assert base == injected
        assert "<context>" not in base
        assert "<rules>" not in base


class TestContextInjection:
    def test_context_appears_in_generation_prompt(self):
        out = build_generation_prompt("requirements", "DESC", spec_context="Stack: Python")
        # Assert the exact block (ordering + tags), not just loose co-occurrence.
        assert "<context>\nStack: Python\n</context>" in out

    def test_context_appears_in_gated_prompt(self):
        out = build_gated_generation_prompt(
            "requirements", "DESC", {}, spec_context="Stack: Python"
        )
        assert "<context>" in out and "Stack: Python" in out and "</context>" in out


class TestRulesInjection:
    def test_matching_stage_rules_injected(self):
        rules = {"requirements": ["Include rollback plan", "Identify affected teams"]}
        out = build_generation_prompt("requirements", "DESC", spec_rules=rules)
        assert "<rules>" in out
        assert "- Include rollback plan" in out
        assert "- Identify affected teams" in out

    def test_non_matching_stage_rules_absent(self):
        rules = {"design": ["Include sequence diagrams"]}
        out = build_generation_prompt("requirements", "DESC", spec_rules=rules)
        assert "<rules>" not in out
        assert "sequence diagrams" not in out

    def test_gated_matching_stage_rules_injected(self):
        rules = {"requirements": ["Use SHALL/MUST"]}
        out = build_gated_generation_prompt("requirements", "DESC", {}, spec_rules=rules)
        assert "<rules>" in out
        assert "- Use SHALL/MUST" in out


class TestMistypedConfigDoesNotCrash:
    """Mis-typed config that bypasses validation must not crash/garble prompts."""

    def test_non_string_context_coerced(self):
        out = build_generation_prompt("requirements", "DESC", spec_context=123)
        assert "<context>\n123\n</context>" in out

    def test_non_dict_rules_ignored(self):
        # spec_rules as a string used to raise on .get(); now silently ignored.
        out = build_generation_prompt("requirements", "DESC", spec_rules="Use MUST")
        assert "<rules>" not in out

    def test_single_string_rule_not_iterated_per_char(self):
        # A stage whose rules are one string → one bullet, not one per char.
        rules = {"requirements": "Use SHALL"}
        out = build_generation_prompt("requirements", "DESC", spec_rules=rules)
        assert "- Use SHALL" in out
        assert "- U\n" not in out  # not character-by-character

    def test_single_string_rule_gated_not_iterated(self):
        rules = {"requirements": "Use SHALL"}
        out = build_gated_generation_prompt("requirements", "DESC", {}, spec_rules=rules)
        assert "- Use SHALL" in out
        assert "- U\n" not in out

    def test_scalar_stage_rule_not_iterated(self):
        # A non-str, non-list scalar (e.g. 123) is not iterable — must not crash.
        out = build_generation_prompt("requirements", "DESC", spec_rules={"requirements": 123})
        assert "- 123" in out

    def test_scalar_stage_rule_gated_not_iterated(self):
        out = build_gated_generation_prompt(
            "requirements", "DESC", {}, spec_rules={"requirements": 123}
        )
        assert "- 123" in out


class TestConfigLoading:
    def test_fields_default_empty(self):
        cfg = ExecutorConfig()
        assert cfg.spec_context == ""
        assert cfg.spec_rules == {}

    def test_loads_from_yaml(self, tmp_path: Path):
        cfg_file = tmp_path / "spec-runner.config.yaml"
        cfg_file.write_text(
            "spec_context: |\n"
            "  Tech stack: Python\n"
            "spec_rules:\n"
            "  requirements:\n"
            "    - Include rollback plan\n"
        )
        yaml_cfg = load_config_from_yaml(cfg_file)
        config = build_config(yaml_cfg, _build_args())
        assert "Tech stack: Python" in config.spec_context
        assert config.spec_rules == {"requirements": ["Include rollback plan"]}


class TestValidation:
    def test_unknown_stage_in_rules_warns(self, tmp_path: Path):
        cfg = tmp_path / "spec-runner.config.yaml"
        cfg.write_text("spec_rules:\n  nonsense:\n    - do a thing\n")
        result = validate_config(cfg)
        assert result.ok  # warning, not error
        assert any("nonsense" in w for w in result.warnings)

    def test_known_stage_in_rules_no_warning(self, tmp_path: Path):
        cfg = tmp_path / "spec-runner.config.yaml"
        cfg.write_text("spec_rules:\n  design:\n    - Include diagrams\n")
        result = validate_config(cfg)
        assert not any("design" in w for w in result.warnings)

    def test_oversized_context_errors(self, tmp_path: Path):
        cfg = tmp_path / "spec-runner.config.yaml"
        big = "x" * (50 * 1024 + 1)
        cfg.write_text(f"spec_context: {big}\n")
        result = validate_config(cfg)
        assert not result.ok
        assert any("50" in e or "50KB" in e.replace(" ", "") for e in result.errors)

    def test_context_at_limit_ok(self, tmp_path: Path):
        cfg = tmp_path / "spec-runner.config.yaml"
        ok_ctx = "x" * (50 * 1024)
        cfg.write_text(f"spec_context: {ok_ctx}\n")
        result = validate_config(cfg)
        assert result.ok

    def test_non_string_context_errors(self, tmp_path: Path):
        cfg = tmp_path / "spec-runner.config.yaml"
        cfg.write_text("spec_context: 123\n")
        result = validate_config(cfg)
        assert not result.ok
        assert any("spec_context must be a string" in e for e in result.errors)

    def test_non_dict_rules_errors(self, tmp_path: Path):
        cfg = tmp_path / "spec-runner.config.yaml"
        cfg.write_text('spec_rules: "Use MUST"\n')
        result = validate_config(cfg)
        assert not result.ok
        assert any("spec_rules must be a mapping" in e for e in result.errors)

    def test_stage_rules_not_a_list_errors(self, tmp_path: Path):
        cfg = tmp_path / "spec-runner.config.yaml"
        cfg.write_text("spec_rules:\n  requirements: Use MUST\n")
        result = validate_config(cfg)
        assert not result.ok
        assert any("must be a list" in e for e in result.errors)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
