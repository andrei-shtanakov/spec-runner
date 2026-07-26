"""Spec frontmatter: SpecMeta dataclass and parse/split/strip/read/write helpers."""

from __future__ import annotations

import contextlib
import datetime
import os
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass, field, fields
from importlib.resources import files
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

if TYPE_CHECKING:
    from .config import ExecutorConfig, ExecutorLock

# Version of the SpecMeta frontmatter contract that consumers pin against
# (steward vendors a pinned copy — DEC-003). v1 was the implicit historical
# contract, inferred from behaviour before it was ever declared here; v2 is
# the first version this repo declares. Adding an optional field is
# non-breaking and does not bump; removing or renaming one bumps.
SPEC_META_CONTRACT: int = 2

_FM_DELIM = "---"


@dataclass(frozen=True)
class StageDef:
    """One stage in a spec-generation profile.

    Consolidates the data that previously lived in three scattered maps
    (``prompt.py`` templates/markers, ``validate.py`` validator dispatch):
    the stage name, its bundled template filename, the ``marker_prefix`` used
    to bracket generated output (``{prefix}_READY`` / ``{prefix}_END``), the
    validator-registry key, its direct ``upstream`` stage(s), and optional
    generation instruction text (``prompt_text``).
    """

    name: str
    template: str
    marker_prefix: str
    validator_key: str
    upstream: tuple[str, ...] = ()
    prompt_text: str = ""

    @property
    def requires(self) -> tuple[str, ...]:
        """Alias for :attr:`upstream` — the stages this one depends on (M4)."""
        return self.upstream


@dataclass(frozen=True)
class StageProfile:
    """A spec-generation profile: a DAG of stages linked by ``requires`` edges.

    List order is the presentation/tie-break order; the actual dependencies
    come from each stage's ``requires``/``upstream`` (M4). A linear profile
    (each stage requiring only its predecessor) behaves exactly as the old
    ordered-list model.
    """

    name: str
    stages: tuple[StageDef, ...] = field(default_factory=tuple)

    def names(self) -> tuple[str, ...]:
        """Return the stage names in profile order."""
        return tuple(s.name for s in self.stages)

    def edges(self) -> dict[str, tuple[str, ...]]:
        """Return ``{stage: direct requires}`` for every stage."""
        return {s.name: s.upstream for s in self.stages}


def load_profile(name: str) -> StageProfile:
    """Load a bundled stage profile by name from ``spec_runner/profiles``.

    Args:
        name: Profile name (e.g. ``"lite"``); resolves ``profiles/{name}.yaml``.

    Returns:
        The parsed :class:`StageProfile`.

    Raises:
        ValueError: If the profile file cannot be found.
    """
    resource = files("spec_runner") / "profiles" / f"{name}.yaml"
    try:
        raw = resource.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError) as exc:
        raise ValueError(f"unknown stage profile: {name!r}") from exc
    data = yaml.safe_load(raw) or {}
    stages = tuple(
        StageDef(
            name=s["name"],
            template=s["template"],
            marker_prefix=s["marker_prefix"],
            validator_key=s["validator"],
            # Accept both spellings; ``requires`` is the M4 canonical key,
            # ``upstream`` the historical one.
            upstream=tuple(s.get("requires") or s.get("upstream") or ()),
            prompt_text=s.get("prompt_text", ""),
        )
        for s in data.get("stages", [])
    )
    profile = StageProfile(name=data.get("profile", name), stages=stages)
    validate_profile_graph(profile)
    return profile


class ProfileGraphError(ValueError):
    """A profile exists but its ``requires`` graph is invalid (cycle/unknown ref).

    Distinct from the "profile not found" ``ValueError`` so callers can tell a
    genuine graph error from an unknown-profile-name error (M4).
    """


def validate_profile_graph(profile: StageProfile) -> None:
    """Validate a profile's dependency graph (M4).

    Raises :class:`ProfileGraphError` when a stage ``requires`` an unknown
    stage or the ``requires`` edges form a cycle.
    """
    names = set(profile.names())
    edges = profile.edges()
    for stage, deps in edges.items():
        for dep in deps:
            if dep not in names:
                raise ValueError(
                    f"stage {stage!r} requires unknown stage {dep!r} in profile {profile.name!r}"
                )

    # Cycle detection via DFS with a recursion stack.
    WHITE, GREY, BLACK = 0, 1, 2
    color = dict.fromkeys(names, WHITE)

    def visit(node: str) -> None:
        color[node] = GREY
        for dep in edges.get(node, ()):
            if color[dep] == GREY:
                raise ValueError(f"dependency cycle through {dep!r} in profile {profile.name!r}")
            if color[dep] == WHITE:
                visit(dep)
        color[node] = BLACK

    for node in names:
        if color[node] == WHITE:
            visit(node)


def available_profiles() -> list[str]:
    """Return the sorted names of bundled stage profiles (``profiles/*.yaml``)."""
    prof_dir = files("spec_runner") / "profiles"
    names = [
        entry.name[: -len(".yaml")] for entry in prof_dir.iterdir() if entry.name.endswith(".yaml")
    ]
    return sorted(names)


#: Built-in default profile — the canonical requirements→design→tasks chain.
LITE: StageProfile = load_profile("lite")

#: Canonical stage names. Kept as a backward-compatible export, now derived
#: from the ``lite`` profile (DESIGN-302). Deprecated in favour of
#: ``StageProfile.names()`` for profile-aware callers.
STAGES: tuple[str, ...] = LITE.names()


class SpecLockError(RuntimeError):
    """Raised when a spec-file lock cannot be acquired (another mutation in progress)."""


class SpecMetaError(Exception):
    """Raised when a managed spec's frontmatter cannot be parsed faithfully."""


@dataclass
class SpecMeta:
    """Frontmatter state for one spec document.

    ``extra`` holds foreign frontmatter keys verbatim so spec-runner is a
    lossless intermediary for extending layers (steward). It is an internal
    field, not a wire field: see :func:`canonical_fields`.
    """

    spec_stage: str
    status: str = "draft"  # draft | approved | stale
    version: int = 1
    generated_by: str = ""
    generated_at: str = ""
    source_prompt_version: str = ""
    validation: str = ""  # pass | fail | warn | ""
    approved_by: str | None = None
    approved_at: str | None = None
    owner_role: str | None = None  # CODEOWNERS role(s), "@role[,@role]"; steward owns the semantics
    extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Copy: a caller-owned mapping must not mutate metadata via an alias.
        self.extra = dict(self.extra)


def split_frontmatter(text: str) -> tuple[dict | None, str]:
    """Split a leading ``---\\n...\\n---`` YAML block from the body.

    Returns ``(meta_dict, body)`` or ``(None, text)`` when no frontmatter.
    """
    if not text.startswith(_FM_DELIM + "\n"):
        return None, text
    end = text.find("\n" + _FM_DELIM, len(_FM_DELIM) + 1)
    if end == -1:
        return None, text
    raw = text[len(_FM_DELIM) + 1 : end]
    # Body starts after the closing delimiter's line.
    after = text.find("\n", end + 1)
    body = text[after + 1 :] if after != -1 else ""
    try:
        loaded = yaml.safe_load(raw)
    except yaml.YAMLError:
        return None, text
    if not isinstance(loaded, dict):
        return None, text
    return loaded, body


def strip_frontmatter(text: str) -> str:
    """Return the document body with any leading frontmatter removed."""
    _, body = split_frontmatter(text)
    return body


def split_frontmatter_raw(text: str) -> tuple[str, str]:
    """Split the verbatim leading frontmatter block from the body.

    Returns ``("", text)`` when no frontmatter is present, else
    ``(raw_prefix, body)`` such that ``raw_prefix + body == text`` exactly,
    where ``raw_prefix`` is the leading ``---\\n...\\n---\\n`` block verbatim
    (including delimiters).
    """
    meta, body = split_frontmatter(text)
    if meta is None:
        return "", text
    return text[: len(text) - len(body)], body


_STATUS_VALUES = frozenset({"draft", "approved", "stale"})
_STR_FIELDS = frozenset(
    {
        "spec_stage",
        "status",
        "generated_by",
        "source_prompt_version",
        "validation",
    }
)
_NULLABLE_STR_FIELDS = frozenset({"approved_by", "owner_role"})
#: Timestamp wire fields: accept YAML's native date scalars and normalize them
#: to a string, so a hand-written `generated_at: 2026-07-05` is not a hard
#: error. ``approved_at`` is additionally nullable.
_TIMESTAMP_FIELDS = frozenset({"generated_at", "approved_at"})
_NULLABLE_TIMESTAMP_FIELDS = frozenset({"approved_at"})


def canonical_fields() -> frozenset[str]:
    """Frontmatter (wire) field names: every SpecMeta field except ``extra``.

    Derived by subtraction so an internal dataclass field can never silently
    widen the wire contract.
    """
    return frozenset(f.name for f in fields(SpecMeta)) - {"extra"}


def _coerce_canonical(key: str, value: object) -> object:
    """Validate one canonical field against the v2 matrix, returning its value.

    Only the two timestamp fields change their value: YAML parses a bare
    ``2026-07-05`` into a ``datetime.date``, which is normalized here to a
    string so the next write canonicalizes the file (design §3.3).

    Raises:
        SpecMetaError: if the value violates the matrix.
    """
    if key in _TIMESTAMP_FIELDS:
        if value is None:
            if key in _NULLABLE_TIMESTAMP_FIELDS:
                return None
            raise SpecMetaError(f"frontmatter field {key!r} must not be null")
        if isinstance(value, str):
            return value
        # datetime BEFORE date: datetime.datetime subclasses datetime.date.
        if isinstance(value, datetime.datetime | datetime.date):
            return value.isoformat()
        raise SpecMetaError(
            f"frontmatter field {key!r} must be a string or a date, got {type(value).__name__}"
        )
    if key in _STR_FIELDS:
        if not isinstance(value, str):
            raise SpecMetaError(
                f"frontmatter field {key!r} must be a string, got {type(value).__name__}"
            )
        if key == "status" and value not in _STATUS_VALUES:
            raise SpecMetaError(
                f"frontmatter field 'status' must be one of {sorted(_STATUS_VALUES)}, got {value!r}"
            )
    elif key in _NULLABLE_STR_FIELDS:
        if value is not None and not isinstance(value, str):
            raise SpecMetaError(
                f"frontmatter field {key!r} must be a string or null, got {type(value).__name__}"
            )
    elif key == "version":
        # type() not isinstance(): isinstance(True, int) is True.
        if type(value) is not int:
            raise SpecMetaError(
                f"frontmatter field 'version' must be an integer, got {type(value).__name__}"
            )
    return value


def meta_from_dict(d: dict) -> SpecMeta:
    """Build a SpecMeta from a frontmatter dict.

    Canonical fields are validated against the v2 matrix. Unknown *string*
    keys are preserved verbatim (see ``SpecMeta.extra``). A non-string key
    raises, since it cannot be round-tripped faithfully.

    Raises:
        SpecMetaError: on a non-string key or a malformed canonical field.
    """
    canonical = canonical_fields()
    known: dict[str, object] = {}
    extra: dict[str, Any] = {}
    for key, value in d.items():
        if not isinstance(key, str):
            raise SpecMetaError(f"frontmatter key {key!r} is not a string")
        if key in canonical:
            known[key] = _coerce_canonical(key, value)
        else:
            extra[key] = value
    if "spec_stage" not in known:
        raise SpecMetaError("frontmatter is missing required field 'spec_stage'")
    return SpecMeta(**known, extra=extra)  # type: ignore[arg-type]


def _canonical_order() -> tuple[str, ...]:
    """Canonical field names in declaration order (the frontmatter order)."""
    return tuple(f.name for f in fields(SpecMeta) if f.name != "extra")


# Fields added in contract v2 and later are omitted when None, so existing
# documents do not gain new null keys on their next write. The v1 nullable
# fields (approved_by/approved_at) keep emitting null as they always have.
_OMIT_WHEN_NONE = frozenset({"owner_role"})


def meta_to_dict(m: SpecMeta) -> dict:
    """Serialize a SpecMeta to a flat frontmatter dict.

    Extras are written first and canonical fields last, so a canonical field
    can never be shadowed by a foreign key. Extras holding a canonical name,
    or a non-string key, are a programming error and raise rather than
    silently corrupting the document.

    Raises:
        SpecMetaError: on a non-string or canonical-shadowing key in ``extra``.
    """
    canonical = canonical_fields()
    for key in m.extra:
        if not isinstance(key, str):
            raise SpecMetaError(f"extra frontmatter key {key!r} is not a string")
        if key in canonical:
            raise SpecMetaError(f"extra frontmatter key {key!r} shadows a canonical field")
    out: dict[str, Any] = dict(m.extra)
    for name in _canonical_order():
        value = getattr(m, name)
        if value is None and name in _OMIT_WHEN_NONE:
            continue
        out[name] = value
    return out


def _render(meta: SpecMeta, body: str) -> str:
    """Render frontmatter + body back into document text."""
    fm = yaml.safe_dump(meta_to_dict(meta), sort_keys=False).rstrip("\n")
    return f"{_FM_DELIM}\n{fm}\n{_FM_DELIM}\n{body}"


def read_spec_meta(path: Path, stages: Sequence[str] = STAGES) -> SpecMeta | None:
    """Return the SpecMeta for ``path``, or None if missing/unmanaged.

    A frontmatter block that lacks a recognized ``spec_stage`` (e.g. an
    unrelated or partial frontmatter block on a non-spec file) is treated as
    unmanaged rather than raising: only frontmatter that actually looks like
    spec meta is considered managed. ``stages`` supplies the recognized stage
    names (default = the ``lite`` profile; DESIGN-303).
    """
    if not path.exists():
        return None
    meta_dict, _ = split_frontmatter(path.read_text())
    if meta_dict is None:
        return None
    if meta_dict.get("spec_stage") not in stages:
        return None
    return meta_from_dict(meta_dict)


def read_spec_body(path: Path) -> str:
    """Return the document body (frontmatter stripped); '' if missing."""
    if not path.exists():
        return ""
    return strip_frontmatter(path.read_text())


def write_spec(
    path: Path,
    meta: SpecMeta,
    body: str,
    lock: ExecutorLock | None = None,
) -> None:
    """Atomically write frontmatter + body, optionally under a file lock."""
    path.parent.mkdir(parents=True, exist_ok=True)
    acquired = False
    if lock is not None:
        acquired = lock.acquire()
        if not acquired:
            raise SpecLockError(
                f"could not acquire spec lock {lock.lock_path}; another spec mutation in progress"
            )
    try:
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(_render(meta, body))
            os.replace(tmp, str(path))
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp)
            raise
    finally:
        if lock is not None and acquired:
            lock.release()


def _order_and_edges(graph: StageProfile | Sequence[str]) -> tuple[tuple[str, ...], dict | None]:
    """Normalize a stage-graph arg into (ordered names, edges-or-None).

    A stage-graph arg is either a full :class:`StageProfile` (DAG edges
    honored) or a bare ordered name list (legacy linear behavior). ``edges``
    is ``None`` in the linear case, so callers fall back to list-order
    semantics.
    """
    if isinstance(graph, StageProfile):
        return graph.names(), graph.edges()
    return tuple(graph), None


def downstream_stages(stage: str, graph: StageProfile | Sequence[str] = STAGES) -> list[str]:
    """Stages that depend on ``stage``, directly or transitively (M4).

    With a :class:`StageProfile`, follows ``requires`` edges so *sibling*
    stages (which merely share an upstream) are excluded. With a bare name
    sequence, keeps the historical "everything after in list order" behavior.
    Results are returned in profile/list order.
    """
    names, edges = _order_and_edges(graph)
    if edges is None:
        i = names.index(stage)
        return list(names[i + 1 :])

    dependents: set[str] = set()
    frontier = [stage]
    while frontier:
        current = frontier.pop()
        for node, deps in edges.items():
            if current in deps and node not in dependents:
                dependents.add(node)
                frontier.append(node)
    return [n for n in names if n in dependents]


def ancestor_stages(stage: str, graph: StageProfile | Sequence[str] = STAGES) -> list[str]:
    """Stages that ``stage`` depends on, directly or transitively (M4).

    The mirror image of :func:`downstream_stages`: with a :class:`StageProfile`,
    follows ``requires`` edges upward so a stage's generation context includes
    every ancestor's output — not just its direct requires — while excluding
    *siblings* (stages that merely share an ancestor but aren't on the path to
    ``stage``). With a bare name sequence, keeps the historical "everything
    before in list order" behavior. Results are returned in profile/list order,
    which is a stable topological order for a well-formed profile (each
    stage's own requires appear no later than the stage itself).
    """
    names, edges = _order_and_edges(graph)
    if edges is None:
        i = names.index(stage)
        return list(names[:i])

    ancestors: set[str] = set()
    frontier = [stage]
    while frontier:
        current = frontier.pop()
        for dep in edges.get(current, ()):
            if dep not in ancestors:
                ancestors.add(dep)
                frontier.append(dep)
    return [n for n in names if n in ancestors]


def _deps_satisfied(deps: tuple[str, ...], metas: dict[str, SpecMeta | None]) -> bool:
    """True when every dependency in ``deps`` is present and approved."""
    return all((m := metas.get(d)) is not None and m.status == "approved" for d in deps)


def resolve_next_stage(
    metas: dict[str, SpecMeta | None], graph: StageProfile | Sequence[str] = STAGES
) -> tuple[str, str]:
    """Compute ``(action, stage)`` from current per-stage metas.

    A stale stage anywhere takes priority. Otherwise the first stage in order
    that is generatable (missing, with all ``requires`` approved) is generated,
    then the first draft stage awaits approval. If every stage is approved the
    pipeline is done; if the only remaining work is dependency-blocked, returns
    ``("blocked", stage)``. In legacy linear mode a missing stage is always
    generatable (no dependency gate), preserving the old behavior.
    """
    names, edges = _order_and_edges(graph)
    for stage in names:
        m = metas.get(stage)
        if m is not None and m.status == "stale":
            return ("stale", stage)

    first_blocked: str | None = None
    for stage in names:
        m = metas.get(stage)
        if m is None:
            deps = edges.get(stage, ()) if edges is not None else ()
            if edges is None or _deps_satisfied(deps, metas):
                return ("generate", stage)
            if first_blocked is None:
                first_blocked = stage
        elif m.status == "draft":
            return ("await_approval", stage)

    if first_blocked is not None:
        return ("blocked", first_blocked)
    return ("done", names[-1])


def stage_readiness(
    metas: dict[str, SpecMeta | None], graph: StageProfile | Sequence[str] = STAGES
) -> dict[str, dict]:
    """Return per-stage ``{state, missing_deps}`` for the whole graph (M4).

    ``state`` is one of ``done`` (approved), ``draft``, ``stale``, ``ready``
    (missing but all deps approved), or ``blocked`` (missing with unapproved
    deps). ``missing_deps`` lists the unapproved dependencies (in ``requires``
    order); empty unless blocked. This exposes DAG parallelism — several stages
    can be ``ready`` at once.
    """
    names, edges = _order_and_edges(graph)
    result: dict[str, dict] = {}
    for stage in names:
        m = metas.get(stage)
        if m is not None and m.status == "approved":
            result[stage] = {"state": "done", "missing_deps": []}
        elif m is not None and m.status == "stale":
            result[stage] = {"state": "stale", "missing_deps": []}
        elif m is not None and m.status == "draft":
            result[stage] = {"state": "draft", "missing_deps": []}
        else:
            deps = edges.get(stage, ()) if edges is not None else ()
            missing = [
                d
                for d in deps
                if not ((mm := metas.get(d)) is not None and mm.status == "approved")
            ]
            result[stage] = {
                "state": "blocked" if missing else "ready",
                "missing_deps": missing,
            }
    return result


def stage_path(config: ExecutorConfig, stage: str) -> Path:
    """Map a stage name to its spec file path via the ``spec/<prefix><name>.md``
    convention (M4).

    Byte-identical to the former hard-coded map for the ``lite`` stages
    (``requirements`` / ``design`` / ``tasks`` all follow this convention on
    ``config``), and it now resolves custom-profile stage names too. Builds
    on ``config.spec_dir`` so a change-scoped config (``--change``, M2)
    redirects stages into ``spec/changes/<id>/``.
    """
    return config.spec_dir / f"{config.spec_prefix}{stage}.md"


def _spec_lock(config: ExecutorConfig) -> ExecutorLock:
    """Build an ``ExecutorLock`` bound to ``config``'s spec lock file."""
    from .config import ExecutorLock

    return ExecutorLock(config.spec_lock_file)


def mark_downstream_stale(
    config: ExecutorConfig,
    stage: str,
    lock: ExecutorLock,
    graph: StageProfile | Sequence[str] = STAGES,
) -> None:
    """Flip every not-already-stale stage that depends on ``stage`` to ``stale``.

    With a :class:`StageProfile`, "depends on" follows ``requires`` edges, so a
    *sibling* stage (sharing an upstream but not depending on ``stage``) is not
    stale-cascaded — unlike the old list-order behavior. Writes are serialized
    through the caller-supplied ``lock``.
    """
    names, _ = _order_and_edges(graph)
    for ds in downstream_stages(stage, graph):
        ds_path = stage_path(config, ds)
        ds_meta = read_spec_meta(ds_path, names)
        if ds_meta is not None and ds_meta.status != "stale":
            ds_meta.status = "stale"
            write_spec(ds_path, ds_meta, read_spec_body(ds_path), lock=lock)


def apply_approval(
    config: ExecutorConfig,
    stage: str,
    approver: str,
    now: str,
    fresh_validation: str,
) -> None:
    """Approve a stage: bump version, record approver, cascade stale downstream.

    Always cascades a ``stale`` status to every downstream stage that isn't
    already stale, since approval bumps the version and any generated
    downstream content may now be out of sync with the newly approved stage.
    """
    profile = config.resolve_spec_profile()
    path = stage_path(config, stage)
    meta = read_spec_meta(path, profile.names())
    if meta is None:
        raise ValueError(f"{stage} is unmanaged (no frontmatter)")
    lock = _spec_lock(config)
    meta.status = "approved"
    meta.version += 1
    meta.approved_by = approver
    meta.approved_at = now
    meta.validation = fresh_validation
    write_spec(path, meta, read_spec_body(path), lock=lock)
    mark_downstream_stale(config, stage, lock, profile)
