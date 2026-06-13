from pathlib import Path

import pytest

from spec_runner.config import load_config_from_yaml
from spec_runner.preset_cmd import Fragment, apply_to_config, compose, list_presets, load_fragment


def test_list_presets_has_six_known_clis():
    assert list_presets() == [
        "claude",
        "codex",
        "opencode",
        "pi",
        "ollama",
        "llama-cli",
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


def test_load_fragment_copilot_rejected_with_hint():
    with pytest.raises(ValueError, match="copilot is not supported"):
        load_fragment("copilot")


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
