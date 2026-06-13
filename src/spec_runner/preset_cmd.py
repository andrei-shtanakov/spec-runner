"""spec-runner config — apply CLI profile presets to spec-runner.config.yaml."""

from __future__ import annotations

from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path

import yaml

CONFIG_FILE = Path("spec-runner.config.yaml")
LEGACY_CONFIG_FILE = Path("spec/executor.config.yaml")

# CLI names recognised by runner.build_cli_invocation auto-detect.
PRESET_NAMES = ["claude", "codex", "opencode", "pi", "ollama", "llama-cli"]

# The 7 CLI-profile keys the composer manages (top-level executor-mapping keys).
PROFILE_KEYS = [
    "claude_command",
    "claude_model",
    "command_template",
    "skip_permissions",
    "review_command",
    "review_model",
    "review_command_template",
]


@dataclass(frozen=True)
class Fragment:
    """Slot-neutral description of how to invoke one CLI."""

    command: str
    model: str = ""
    skip_permissions: bool = False
    note: str = ""


def list_presets() -> list[str]:
    """Return the available preset names."""
    return list(PRESET_NAMES)


def load_fragment(name: str) -> Fragment:
    """Load a preset fragment by CLI name from bundled package data."""
    if name == "copilot":
        raise ValueError(
            "copilot is not supported in v1 (no auto-detect); set "
            "command_template manually in spec-runner.config.yaml."
        )
    if name not in PRESET_NAMES:
        valid = ", ".join(PRESET_NAMES)
        raise ValueError(f"Unknown preset '{name}'. Valid presets: {valid}")
    resource = files("spec_runner") / "presets" / f"{name}.yaml"
    data = yaml.safe_load(resource.read_text()) or {}
    if "command" not in data:
        raise ValueError(f"Preset file for '{name}' is missing required 'command' key")
    return Fragment(
        command=data["command"],
        model=data.get("model", ""),
        skip_permissions=bool(data.get("skip_permissions", False)),
        note=data.get("note", ""),
    )


def compose(
    exec_frag: Fragment,
    review_frag: Fragment,
    model_override: str = "",
    review_model_override: str = "",
) -> dict[str, object]:
    """Map an (exec, review) fragment pair into the 7 CLI-profile keys.

    `command_template` / `review_command_template` are always cleared to "" so a
    stale template from a previously configured CLI does not leak. Model
    precedence: per-slot override > shared --model override > fragment default.
    """
    exec_model = model_override or exec_frag.model
    review_model = review_model_override or model_override or review_frag.model
    return {
        "claude_command": exec_frag.command,
        "claude_model": exec_model,
        "command_template": "",
        "skip_permissions": exec_frag.skip_permissions,
        "review_command": review_frag.command,
        "review_model": review_model,
        "review_command_template": "",
    }
