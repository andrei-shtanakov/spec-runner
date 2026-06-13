import pytest

from spec_runner.preset_cmd import Fragment, compose, list_presets, load_fragment


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
