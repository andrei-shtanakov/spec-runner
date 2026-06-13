import pytest

from spec_runner.preset_cmd import Fragment, list_presets, load_fragment


def test_list_presets_has_six_known_clis():
    assert list_presets() == [
        "claude", "codex", "opencode", "pi", "ollama", "llama-cli",
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
