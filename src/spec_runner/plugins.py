"""Plugin discovery and loading for spec-runner.

Scans a plugins directory for subdirectories containing plugin.yaml
manifests, parses them into PluginInfo/PluginHook dataclasses.
"""

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .logging import get_logger

log = get_logger("plugins")


@dataclass
class PluginHook:
    """A single hook entry within a plugin manifest.

    Attributes:
        command: Shell command to execute (relative to plugin dir).
        run_on: When to run: "always", "on_success", or "on_failure".
        blocking: If True, hook failure blocks task completion.
    """

    command: str
    run_on: str = "always"
    blocking: bool = False


@dataclass
class PluginInfo:
    """Parsed plugin manifest with metadata and hooks.

    Attributes:
        name: Plugin name from manifest.
        description: Human-readable description.
        version: Plugin version string.
        path: Absolute path to plugin directory.
        hooks: Mapping of hook point name to PluginHook.
    """

    name: str
    description: str
    version: str
    path: Path
    hooks: dict[str, PluginHook] = field(default_factory=dict)


def _parse_hooks(raw_hooks: dict) -> dict[str, PluginHook]:
    """Parse raw hook dicts from YAML into PluginHook instances.

    Args:
        raw_hooks: Mapping of hook name to hook config dict.

    Returns:
        Mapping of hook name to PluginHook.
    """
    hooks: dict[str, PluginHook] = {}
    for name, config in raw_hooks.items():
        if not isinstance(config, dict) or "command" not in config:
            log.warning("skipping invalid hook", hook=name)
            continue
        hooks[name] = PluginHook(
            command=config["command"],
            run_on=config.get("run_on", "always"),
            blocking=config.get("blocking", False),
        )
    return hooks


def _load_plugin(plugin_dir: Path) -> PluginInfo | None:
    """Load a single plugin from its directory.

    Args:
        plugin_dir: Path to plugin directory containing plugin.yaml.

    Returns:
        PluginInfo if manifest is valid, None otherwise.
    """
    manifest_path = plugin_dir / "plugin.yaml"
    if not manifest_path.exists():
        return None

    try:
        with open(manifest_path) as f:
            data = yaml.safe_load(f) or {}
    except Exception as exc:
        log.warning("failed to parse plugin manifest", path=str(manifest_path), error=str(exc))
        return None

    name = data.get("name")
    if not name:
        log.warning("plugin manifest missing name", path=str(manifest_path))
        return None

    raw_hooks = data.get("hooks", {})
    hooks = _parse_hooks(raw_hooks) if isinstance(raw_hooks, dict) else {}

    return PluginInfo(
        name=name,
        description=data.get("description", ""),
        version=str(data.get("version", "")),
        path=plugin_dir,
        hooks=hooks,
    )


def discover_plugins(plugins_dir: Path) -> list[PluginInfo]:
    """Scan a directory for plugins and return them sorted by name.

    Each subdirectory containing a valid plugin.yaml is loaded as a plugin.
    Directories without manifests or with invalid manifests are skipped.

    Args:
        plugins_dir: Path to the plugins directory.

    Returns:
        List of PluginInfo sorted alphabetically by name.
    """
    if not plugins_dir.is_dir():
        return []

    plugins: list[PluginInfo] = []
    for entry in sorted(plugins_dir.iterdir()):
        if not entry.is_dir():
            continue
        plugin = _load_plugin(entry)
        if plugin is not None:
            plugins.append(plugin)
            log.debug("discovered plugin", name=plugin.name, hooks=list(plugin.hooks.keys()))

    return sorted(plugins, key=lambda p: p.name)
