from pathlib import Path

import pytest

from spec_runner.cli import _build_parser
from spec_runner.config import load_config_from_yaml
from spec_runner.preset_cmd import (
    Fragment,
    apply_to_config,
    cmd_config,
    compose,
    list_presets,
    load_fragment,
)


def test_list_presets_has_known_clis():
    assert list_presets() == [
        "claude",
        "codex",
        "opencode",
        "pi",
        "ollama",
        "llama-cli",
        "qwen",
        "copilot",
    ]


def test_load_fragment_claude_keeps_skip_permissions_true():
    frag = load_fragment("claude")
    assert frag == Fragment(command="claude", model="", skip_permissions=True, note="")


def test_load_fragment_codex_is_skip_permissions_false():
    assert load_fragment("codex").skip_permissions is False


def test_load_fragment_llama_cli_command_is_llama_cli():
    # bare "llama" would fall through auto-detect to the claude branch
    assert load_fragment("llama-cli").command == "llama-cli"


def test_load_fragment_pi_has_model_note():
    assert "pi --list-models" in load_fragment("pi").note


def test_load_fragment_unknown_raises():
    with pytest.raises(ValueError, match="Unknown preset"):
        load_fragment("nope")


def test_compose_mono_codex():
    frag = load_fragment("codex")
    profile = compose(frag, frag)
    assert profile == {
        "claude_command": "codex",
        "claude_model": "",
        "command_template": "",
        "skip_permissions": False,
        "review_command": "codex",
        "review_model": "",
        "review_command_template": "",
    }


def test_compose_multi_claude_exec_codex_review():
    profile = compose(load_fragment("claude"), load_fragment("codex"))
    assert profile["claude_command"] == "claude"
    assert profile["skip_permissions"] is True  # from exec (claude)
    assert profile["review_command"] == "codex"


def test_compose_clears_templates():
    profile = compose(load_fragment("pi"), load_fragment("claude"))
    assert profile["command_template"] == ""
    assert profile["review_command_template"] == ""


def test_compose_model_override_applies_to_both_slots():
    profile = compose(load_fragment("codex"), load_fragment("codex"), model_override="o3")
    assert profile["claude_model"] == "o3"
    assert profile["review_model"] == "o3"


def test_compose_review_model_override_targets_review_only():
    profile = compose(
        load_fragment("claude"),
        load_fragment("codex"),
        model_override="sonnet",
        review_model_override="o3",
    )
    assert profile["claude_model"] == "sonnet"
    assert profile["review_model"] == "o3"


def test_fresh_write_creates_flat_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    profile = compose(load_fragment("codex"), load_fragment("codex"))
    written = apply_to_config(profile, apply_changes=False, dry_run=False)
    assert written == Path("spec-runner.config.yaml")
    text = written.read_text()
    # flat v2.0 — no executor: wrapper
    assert "executor:" not in text
    assert "claude_command:" in text
    # round-trips through the real loader
    loaded = load_config_from_yaml(written)
    assert loaded["claude_command"] == "codex"
    assert loaded["review_command"] == "codex"


def test_fresh_write_renders_skip_permissions_bool(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    profile = compose(load_fragment("claude"), load_fragment("claude"))
    written = apply_to_config(profile, apply_changes=False, dry_run=False)
    assert load_config_from_yaml(written)["skip_permissions"] is True


def test_dry_run_writes_nothing_and_prints_keys(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    profile = compose(load_fragment("codex"), load_fragment("codex"))
    result = apply_to_config(profile, apply_changes=False, dry_run=True)
    assert result is None
    assert not Path("spec-runner.config.yaml").exists()
    out = capsys.readouterr().out
    assert "claude_command:" in out
    assert "review_command_template:" in out


def test_refuse_without_apply_exits_1_and_leaves_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = Path("spec-runner.config.yaml")
    cfg.write_text("claude_command: claude\nbudget_usd: 5.0\n")
    original = cfg.read_text()
    profile = compose(load_fragment("codex"), load_fragment("codex"))
    with pytest.raises(SystemExit) as exc:
        apply_to_config(profile, apply_changes=False, dry_run=False)
    assert exc.value.code == 1
    assert cfg.read_text() == original


def test_apply_merges_flat_preserving_other_keys(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = Path("spec-runner.config.yaml")
    cfg.write_text("claude_command: claude\nbudget_usd: 10.0\ntelegram_bot_token: secret123\n")
    profile = compose(load_fragment("codex"), load_fragment("codex"))
    apply_to_config(profile, apply_changes=True, dry_run=False)
    loaded = load_config_from_yaml(cfg)
    assert loaded["claude_command"] == "codex"
    assert loaded["review_command"] == "codex"
    assert loaded["budget_usd"] == 10.0
    assert loaded["telegram_bot_token"] == "secret123"
    assert "executor:" not in cfg.read_text()  # flat stays flat
    assert Path("spec-runner.config.yaml.bak").exists()


def test_apply_merges_wrapped_preserving_wrapper(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = Path("spec-runner.config.yaml")
    cfg.write_text("executor:\n  claude_command: claude\n  budget_usd: 7.0\n")
    profile = compose(load_fragment("pi"), load_fragment("claude"))
    apply_to_config(profile, apply_changes=True, dry_run=False)
    assert "executor:" in cfg.read_text()  # wrapped stays wrapped
    loaded = load_config_from_yaml(cfg)
    assert loaded["claude_command"] == "pi"
    assert loaded["review_command"] == "claude"
    assert loaded["budget_usd"] == 7.0
    assert Path("spec-runner.config.yaml.bak").exists()


def test_apply_malformed_yaml_aborts_without_writing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = Path("spec-runner.config.yaml")
    cfg.write_text("claude_command: [unclosed\n")
    original = cfg.read_text()
    profile = compose(load_fragment("codex"), load_fragment("codex"))
    with pytest.raises(SystemExit) as exc:
        apply_to_config(profile, apply_changes=True, dry_run=False)
    assert exc.value.code == 1
    assert cfg.read_text() == original
    assert not Path("spec-runner.config.yaml.bak").exists()


# Task 6: cmd_config entry point + argparse wiring


def test_config_subcommand_parses_and_lists(capsys):
    parser = _build_parser()
    args = parser.parse_args(["config", "--list-presets"])
    assert args.command == "config"
    cmd_config(args, None)
    out = capsys.readouterr().out.split()
    assert out == ["claude", "codex", "opencode", "pi", "ollama", "llama-cli", "qwen", "copilot"]


def test_config_requires_a_cli_selection(capsys):
    parser = _build_parser()
    args = parser.parse_args(["config"])
    with pytest.raises(SystemExit) as exc:
        cmd_config(args, None)
    assert exc.value.code == 2
    assert "Specify --preset" in capsys.readouterr().err


def test_config_preset_writes_mono(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    parser = _build_parser()
    args = parser.parse_args(["config", "--preset", "codex"])
    cmd_config(args, None)
    loaded = load_config_from_yaml(Path("spec-runner.config.yaml"))
    assert loaded["claude_command"] == "codex"
    assert loaded["review_command"] == "codex"


def test_config_preset_qwen_writes_template(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    parser = _build_parser()
    args = parser.parse_args(["config", "--preset", "qwen"])
    cmd_config(args, None)
    loaded = load_config_from_yaml(Path("spec-runner.config.yaml"))
    assert loaded["claude_command"] == "qwen"
    assert "--approval-mode yolo" in loaded["command_template"]
    assert "--approval-mode plan" in loaded["review_command_template"]


def test_config_preset_copilot_no_longer_rejected(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    parser = _build_parser()
    args = parser.parse_args(["config", "--preset", "copilot"])
    cmd_config(args, None)  # must NOT raise SystemExit
    loaded = load_config_from_yaml(Path("spec-runner.config.yaml"))
    assert loaded["claude_command"] == "copilot"
    assert "--allow-all-tools" in loaded["command_template"]
    assert "--allow-tool='shell'" in loaded["review_command_template"]


def test_config_multi_exec_review_through_parser(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    parser = _build_parser()
    args = parser.parse_args(
        ["config", "--exec", "claude", "--review", "codex", "--model", "sonnet"]
    )
    cmd_config(args, None)
    loaded = load_config_from_yaml(Path("spec-runner.config.yaml"))
    assert loaded["claude_command"] == "claude"
    assert loaded["claude_model"] == "sonnet"
    assert loaded["review_command"] == "codex"
    assert loaded["review_model"] == "sonnet"


def test_config_dry_run_through_parser_writes_nothing(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    parser = _build_parser()
    args = parser.parse_args(["config", "--preset", "codex", "--dry-run"])
    cmd_config(args, None)
    assert not Path("spec-runner.config.yaml").exists()
    assert "claude_command:" in capsys.readouterr().out


def test_config_apply_through_parser_merges(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    Path("spec-runner.config.yaml").write_text("claude_command: claude\nbudget_usd: 3.0\n")
    parser = _build_parser()
    args = parser.parse_args(["config", "--preset", "codex", "--apply"])
    cmd_config(args, None)
    loaded = load_config_from_yaml(Path("spec-runner.config.yaml"))
    assert loaded["claude_command"] == "codex"
    assert loaded["budget_usd"] == 3.0


def test_load_fragment_qwen_has_templates():
    frag = load_fragment("qwen")
    assert frag.command == "qwen"
    assert "--approval-mode yolo" in frag.exec_template
    assert "--approval-mode plan" in frag.review_template


def test_load_fragment_copilot_has_templates_and_is_not_rejected():
    frag = load_fragment("copilot")
    assert frag.command == "copilot"
    assert "--allow-all-tools" in frag.exec_template
    assert "--allow-tool='shell'" in frag.review_template


def test_auto_detect_presets_have_empty_templates():
    for name in ["claude", "codex", "opencode", "pi", "ollama", "llama-cli"]:
        frag = load_fragment(name)
        assert frag.exec_template == ""
        assert frag.review_template == ""


def test_compose_exec_template_lands_in_command_template():
    profile = compose(load_fragment("qwen"), load_fragment("claude"))
    assert "--approval-mode yolo" in profile["command_template"]
    # review slot is an auto-detect CLI → template cleared
    assert profile["review_command_template"] == ""


def test_compose_review_template_lands_in_review_command_template():
    profile = compose(load_fragment("claude"), load_fragment("copilot"))
    assert profile["command_template"] == ""  # exec is auto-detect
    # template stored raw; shlex.split at runtime strips the single quotes -> --allow-tool=shell
    assert "--allow-tool='shell'" in profile["review_command_template"]


def test_compose_mono_copilot_fills_both_template_slots():
    profile = compose(load_fragment("copilot"), load_fragment("copilot"))
    assert "--allow-all-tools" in profile["command_template"]
    assert "--allow-tool='shell'" in profile["review_command_template"]


# --- Revision 5 / v2.7.0: model-aware templates for qwen/copilot ---


def test_qwen_copilot_fragments_have_model_flag():
    assert load_fragment("qwen").model_flag == "--model"
    assert load_fragment("copilot").model_flag == "--model"


def test_auto_detect_fragments_have_no_model_flag():
    for name in ["claude", "codex", "opencode", "pi", "ollama", "llama-cli"]:
        assert load_fragment(name).model_flag == ""


def test_compose_templated_preset_with_model_appends_flag():
    profile = compose(
        load_fragment("qwen"), load_fragment("qwen"), model_override="qwen-coder-plus"
    )
    assert profile["command_template"].endswith("--model {model}")
    assert profile["review_command_template"].endswith("--model {model}")
    assert profile["claude_model"] == "qwen-coder-plus"
    assert profile["review_model"] == "qwen-coder-plus"


def test_compose_templated_preset_without_model_has_no_flag():
    # anti-trap regression: empty model must NOT produce a dangling --model
    profile = compose(load_fragment("copilot"), load_fragment("copilot"))
    assert "--model" not in profile["command_template"]
    assert "--model" not in profile["review_command_template"]


def test_compose_auto_detect_preset_with_model_keeps_empty_template():
    profile = compose(load_fragment("claude"), load_fragment("claude"), model_override="sonnet")
    assert profile["command_template"] == ""
    assert profile["review_command_template"] == ""
    assert profile["claude_model"] == "sonnet"  # model flows via auto-detect, not the template


def test_compose_multi_exec_qwen_review_claude_with_model():
    profile = compose(
        load_fragment("qwen"), load_fragment("claude"), model_override="qwen-coder-plus"
    )
    assert profile["command_template"].endswith("--model {model}")  # exec qwen templated
    assert profile["review_command_template"] == ""  # review claude auto-detect
    assert profile["review_model"] == "qwen-coder-plus"


def test_config_preset_qwen_with_model_through_parser(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    parser = _build_parser()
    args = parser.parse_args(["config", "--preset", "qwen", "--model", "qwen-coder-plus"])
    cmd_config(args, None)
    loaded = load_config_from_yaml(Path("spec-runner.config.yaml"))
    assert "--model {model}" in loaded["command_template"]
    assert loaded["claude_model"] == "qwen-coder-plus"
