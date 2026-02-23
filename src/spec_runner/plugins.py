"""Plugin discovery and loading for spec-runner.

Scans a plugins directory for subdirectories containing plugin.yaml
manifests, parses them into PluginInfo/PluginHook dataclasses.
Executes plugin hooks as subprocesses with env vars and run_on filtering.
"""

import os
import subprocess
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


def _should_run(hook: PluginHook, task_status: str) -> bool:
    """Check if hook should run based on run_on filter and task status.

    Args:
        hook: The plugin hook to evaluate.
        task_status: Current task status ("success" or "failed").

    Returns:
        True if the hook should execute.
    """
    if hook.run_on == "always":
        return True
    if hook.run_on == "on_success" and task_status == "success":
        return True
    return hook.run_on == "on_failure" and task_status == "failed"


def run_plugin_hooks(
    event: str,
    plugins: list[PluginInfo],
    task_env: dict[str, str] | None = None,
    timeout_seconds: int = 60,
) -> list[tuple[str, bool, bool]]:
    """Run all plugin hooks for given event.

    Args:
        event: Hook event name (pre_start, post_done).
        plugins: Discovered plugins.
        task_env: Environment variables (SR_TASK_ID, etc.).
        timeout_seconds: Per-plugin timeout.

    Returns:
        List of (plugin_name, success, is_blocking) tuples.
        Only includes plugins that actually ran (not skipped by run_on filter).
    """
    results: list[tuple[str, bool, bool]] = []
    env = {**os.environ, **(task_env or {})}
    task_status = (task_env or {}).get("SR_TASK_STATUS", "success")

    for plugin in plugins:
        hook = plugin.hooks.get(event)
        if not hook:
            continue
        if not _should_run(hook, task_status):
            continue

        try:
            result = subprocess.run(
                hook.command,
                shell=True,
                env=env,
                cwd=str(plugin.path),
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
            )
            success = result.returncode == 0
            if not success:
                level = "error" if hook.blocking else "warning"
                getattr(log, level)(
                    "Plugin hook failed",
                    plugin=plugin.name,
                    hook_event=event,
                    returncode=result.returncode,
                    stderr=result.stderr[:500],
                )
            else:
                log.info(
                    "Plugin hook succeeded",
                    plugin=plugin.name,
                    hook_event=event,
                )
            results.append((plugin.name, success, hook.blocking))
        except subprocess.TimeoutExpired:
            log.error(
                "Plugin hook timed out",
                plugin=plugin.name,
                hook_event=event,
            )
            results.append((plugin.name, False, hook.blocking))
        except Exception as e:
            log.error(
                "Plugin hook error",
                plugin=plugin.name,
                error=str(e),
            )
            results.append((plugin.name, False, hook.blocking))

    return results
