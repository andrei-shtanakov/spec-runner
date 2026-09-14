# Behaviour bundle → tasks artifact: review contract

This is the repository-local review surface for the bundle and tasks artifact
produced by the devtools behaviour conveyor. A terminal reviewer must be able
to judge changes to `spec_stage`, `checked_by`, scenario traces, and generated
tasks without reading a sibling devtools checkout.

## Provenance and trust

The rules below are vendored from `andrei-shtanakov/devtools` commit
`c7794c59663cef42986f6a837fe588a151767cd0`:

| Upstream file | SHA-256 at the pinned commit |
|---|---|
| `profiles/team-exp.yaml` | `3189a9bb2fde6967b0a680f6659f854740362c95305a5c68884c563c54f457f7` |
| `governance/bundle_dag.py` | `18729f97ad289698a731c852a0bf46cffbfd21023928379c555cdb77cff88afe` |
| `governance/bundle_state.py` | `6a2d27d989bc6f9e0eb178e58530248a9707bacfb71585275a56b91bc8961f05` |
| `governance/decomposition_guard.py` | `b3fcaa3c07734c4eadc7fde8feac5b001f9e702e1f552a40b946145f8fa6dbdb` |
| `governance/task_bridge.py` | `ee7963ede05785be892249b699206db856691589ce2be361d3726c5e86b8f61f05` |
| `tests/governance_fixtures/bundles.py` | `bd20bb9136ff4dbd7c6dae7e22ba32cf1b6d23f4b6ef459f68d469afa0de301e` |

This vendored reading, together with `spec/FORMAT.md`, is the contract for
review inside spec-runner until a human-reviewed PR updates it. Upstream drift
does not silently change the rules here. The review kit reads both this
manifest and its files from the PR merge-base: edits in the reviewed patch are
proposals for later reviews, not trusted instructions for their own review.

## Bundle graph and artifact identity

The current full `team-exp` graph is:

```text
charter → requirements → behaviour-spec
requirements + behaviour-spec → design
requirements + behaviour-spec → acceptance
design + acceptance → decomposition → tasks (delegated)
```

In edge-list form, the direct upstreams are:

- `charter: []`
- `requirements: [charter]`
- `behaviour-spec: [requirements]`
- `design: [requirements, behaviour-spec]`
- `acceptance: [requirements, behaviour-spec]`
- `decomposition: [design, acceptance]`
- `tasks: [decomposition]`, with `delegate: spec-runner`

Three exact legacy shapes remain valid for already-created workstreams:

- legacy 3: `charter → requirements → behaviour-spec`; tasks anchor to
  `behaviour-spec`;
- legacy 4: the same chain plus `design`; tasks anchor to `design`;
- legacy 5: `charter → requirements → behaviour-spec → design → decomposition`
  without `acceptance`; `decomposition` has only `design` upstream and tasks
  anchor to `decomposition`.

Do not report a legacy artifact as defective merely because it is not the
current seven-node graph. Its exact file composition and tasks anchor must
agree with one of the legacy shapes.

A bundle node is identified by YAML frontmatter `spec_stage`, not by its
filename. Filenames such as `15-behaviour-spec.md` are a readable convention,
not node identity. A Markdown file without parseable frontmatter/`spec_stage`
is not a node. Unknown or duplicate node declarations are findings, not extra
prose to ignore.

For every direct upstream edge present in the active graph, the downstream
node declares that node in `traces_to` and carries its current Git blob id in
`upstream_hashes`. A missing pin is not “unknown = green”; a stale pin is not
approval of the new upstream bytes. A delegated node such as `tasks` lives
outside the bundle and must not be treated as a missing bundle file.

## Behaviour-spec DSL

The bridge parses behaviour scenarios from this narrow surface:

```markdown
#### BEH-01: Observable outcome
`traces: [FR-01, NFR-02]`
- **checked_by**: `status: planned` `kind: integration` `owner: qa` `target: tests/test_x.py::test_case`
```

- A scenario header is exactly `#### BEH-NN: title`; a lowercase suffix such
  as `BEH-18a` is valid.
- Scenario `traces` is the inline code-span list inside that scenario block.
  It is distinct from frontmatter `traces_to`, which describes node-to-node
  graph edges.
- A check binding starts with `- **checked_by**:` and carries backtick
  `key: value` spans. The bridge consumes both `kind` and `target`; losing
  either changes the delivered task contract.
- Binding statuses are `planned`, `materialized`, and `waived`. A planned
  binding needs `kind`, `owner`, and `target`; a materialized one needs
  `kind`, `owner`, and `ref`; a waived one needs `reason`.
- When present, `kind` is one of `atp`, `contract`, `integration`, `e2e`, and
  `manual`. `pytest` is not a kind.
- A Must-linked behaviour must have a complete binding for its declared
  status. Do not infer one from nearby prose or a different scenario.
- The parser is deliberately tolerant of prose around the DSL. Review the
  structural lines above, not stylistic variations in scenario prose.

## Delivered tasks artifact

The bridge creates `spec/<workstream>-tasks.md` as a managed draft. Its
frontmatter has `spec_stage: tasks`, `status: draft`, `owner_role:
stream-owner`, a positive `version`, and generator metadata. `traces_to` is
exactly the terminal node of the active full or legacy graph, and
`upstream_hashes` contains the current Git blob id for that same node. Human
approval may change workflow fields such as status and approver data; it must
not silently move the artifact to spec-runner's unrelated default `lite`
profile or replace the conveyor anchor.

For every rendered task:

- `Source:` points back to the owning source: a `BEH-*` anchor on the legacy
  grouping path, or a `DT-*` anchor when an approved decomposition exists.
- Every selected behaviour is represented once in the task checklist.
- The verification checklist item preserves both `checked_by.target` and
  `checked_by.kind`; a target alone is incomplete.
- `**Traces to:**` is the stable-order union of the selected scenarios'
  `traces`. Each id has its own brackets: `[FR-01], [FR-02]`, never
  `[FR-01, FR-02]`, because the spec-runner parser reads bracketed references.
- On the decomposition path, one `DT-*` becomes one task and `depends_on`
  becomes `**Depends on:**` task references. The bridge must not regroup those
  tasks with its legacy feature/file heuristic.
- A decomposition task with `type: verify` adds `**Mode:** verify_first` and
  `**Verifies:**`. The verifies group is the stable-order union of the task's
  own scenario `checked_by` targets followed by its declared extra verifies;
  deduplication is by the full selector, not merely by filename.

`spec/FORMAT.md` in the same review context is authoritative for the syntax
that spec-runner itself parses after delivery. A patch is wrong if it produces
a document that looks plausible to the conveyor but loses meaning when read
by that parser.
