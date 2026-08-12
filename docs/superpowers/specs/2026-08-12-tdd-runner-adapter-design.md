# TDD runner adapters — design (#198 step 2)

**Status:** **approved** 2026-08-12 (owner, after four corrections). Being
implemented in the build order of §8; each step links back here.

Step 1 (#199, shipped) made a confirmed RED reachable only for pytest and
refused every other runner. That is safe and it is not enough: TDD mode is
unusable outside Python, and the pilot that found the defect — three real TDD
tasks on **kapelle**, an Elixir product — cannot start.

This is the extension. It is written after measuring ExUnit rather than before,
and the measurements changed the contract three times; each is marked
**measured** below.

## 1. Why an exit code cannot be the answer

The step-1 fix framed the defect as "pytest's exit codes are not universal".
That is true and it is the smaller half. Measured on Elixir 1.x / OTP 28:

```
$ mix test test/probe_test.exs:999          # a line past the end of the file
Including tags: [location: {"test/probe_test.exs", 999}]
  1) test calls missing module (ProbeTest)
     test/probe_test.exs:9
1 test, 1 failure (2 excluded)
exit 2
```

**A selector that resolves to nothing runs a different test.** ExUnit's
`path:line` filter selects the nearest test at or before the line, so an
out-of-range line silently runs the *last* test in the file — and reports a
perfectly ordinary "1 test, 1 failure" while doing it. Measured across the
file:

| requested line | what ran |
|---|---|
| 1–2 (before the first test) | **0 tests**, exit **0** |
| 3 (a `test do` line) | that test |
| 4–5 (inside its body) | the test at 3 |
| 6, 7 | the test at 6 |
| 9, 12, **999** | the test at 9 — the last one in the file |

So even a *correct* per-runner exit-code table would produce false reds on
ExUnit. `exit 2` means "some test failed", not "the test you named failed", and
`exit 0` means "nothing failed", not "your test passed" — with 0 tests run it
is `0 tests, 0 failures` and exit 0.

This is why the two proofs are the contract and not a refinement:

```
EXPECTED_FAIL  ⟺  the runner selected the requested test
              AND  that test failed as a test failure
```

Neither is derivable from an exit code on ExUnit. One of them is not derivable
from an exit code on any runner.

## 2. The adapter

```python
class TddRunnerAdapter(Protocol):
    name: str                                    # "pytest" | "exunit"

    def parse_selector(self, raw: str) -> Selector | SelectorRefusal: ...

    def validate_command(self, test_command: str) -> str | None: ...
    """A refusal, or None when this command can carry this adapter's selector."""

    def preflight(self, root: Path, selector: Selector) -> SelectorRefusal | None: ...
    """Check the selector against the source, before anything is executed."""

    def build_command(self, test_command: str, selector: Selector) -> list[str]: ...
    """argv, never a shell string."""

    def classify(self, result: CompletedProcess) -> RunOutcome: ...
    def prove_selected(self, selector: Selector, result: CompletedProcess) -> SelectionProof: ...
```

```python
@dataclass(frozen=True)
class Selector:
    runner: str
    path: PurePosixPath                    # project-relative, normalised
    locator: PytestNodeId | ExUnitDefinitionLine

@dataclass(frozen=True)
class SelectorRefusal:
    code: str                              # stable, e.g. "not_a_definition_line"
    message: str                           # what a human needs to fix it
```

`path` is normalised to a project-relative `PurePosixPath` at parse time, so
`./test/x_test.exs` and `test/x_test.exs` are the same selector and comparisons
against runner output never turn on a leading `./`.

`parse_selector` returns a refusal rather than raising: a malformed selector is
a normal outcome of asking an agent for one, and it belongs in the checkpoint
record like any other verdict, with a stable `code` that tests and operators can
match on.

`build_command` returns **argv**. The selector comes from agent output; it is
never concatenated into a shell string. (Today's `_run_selector` builds a shell
string with `shlex.quote`, which is correct but one edit away from not being.)

### Observation and proof are orthogonal

```
RunOutcome                       what the run did, as a whole
    TESTS_PASSED                 the run executed tests, none failed
    TESTS_FAILED                 the run executed tests, at least one failed
    SELECTION_FAILED             the runner ran but selected nothing (or no such path)
    COLLECTION_OR_COMPILE_ERROR  the test could not be built or collected
    RUNNER_ERROR                 the runner itself failed (usage, crash, timeout)
    UNRECOGNIZED                 the adapter cannot read this output

SelectionProof                   whether *the requested test* is what ran
    PROVEN | REFUTED | UNKNOWN
```

The names are plural on purpose. `classify` describes the run; it never claims
which test executed — that is `prove_selected`'s job, and keeping them apart is
what stops an adapter from quietly asserting identity in the easy path and
having it checked only in the hard one.

| RunOutcome | SelectionProof | `RedOutcome` |
|---|---|---|
| `TESTS_FAILED` | `PROVEN` | **`expected_fail`** |
| `TESTS_PASSED` | `PROVEN` | `not_red` |
| anything else | anything else | `unverifiable` |

`UNRECOGNIZED` exists so that an adapter seeing output it was not written for
says so, instead of falling back to the exit code — the failure mode of the
original defect, one level up.

## 3. pytest adapter

- **Canonical selector:** the node id, `path::test` (`path::Class::test` too).
- `validate_command`: the token check shipped in #199.
- `preflight`: none required — see below.
- `build_command`: `[*shlex.split(test_command), node_id]`.
- **classify**, from the measured table already in `tdd.py`: exit 0 →
  `TESTS_PASSED`; 1 → `TESTS_FAILED`; 4 → `COLLECTION_OR_COMPILE_ERROR` (an
  unresolvable node id and a syntax error both give 4 — measured, pytest 8);
  5 → `SELECTION_FAILED`; anything else → `RUNNER_ERROR`.
- **prove_selected:** the node id appears in the reported failure header, or the
  summary reports exactly one test collected; otherwise `UNKNOWN`.

pytest needs no preflight because a node id that names nothing **cannot** be
mistaken for a red: it exits 4, never 1. That property is exactly what ExUnit
lacks, and it is why pytest was safe to keep in step 1.

Behaviour for existing pytest projects does not change; the contract tests in
`test_red_checkpoint.py` are the regression proof.

## 4. ExUnit adapter

- **Canonical selector (v1):** `path:line`, where **line is the
  `test "..." do` line**. A pytest-style `path::name` is refused by
  `parse_selector` before anything runs — it is what the current RED prompt
  asks for, and it is how the defect got in.
- `build_command`: `[*shlex.split(test_command), f"{path}:{line}"]`.

### 4.1 Preflight — the definition line is proven before `mix test` runs

**This is the load-bearing part, and it replaces the earlier draft's
after-the-fact proof.** For a *failing* test ExUnit prints the exact
`path:line`, so proof after the run is possible. For a *passing* test there is
no failure block, and `1 test, 0 failures` proves nothing: `:999` can select the
last test just as easily when that test passes. Under the earlier draft, a
`not_red` verdict — which retires a claimed red and sends an operator to
`repair` — would have rested on nothing.

So the requested line is proven to be a definition line **before** the runner is
invoked, using Elixir's own parser rather than a Python one:

```elixir
{:ok, ast} = Code.string_to_quoted(File.read!(path))
Macro.prewalk(ast, [], fn
  {:test, meta, [_name | _]} = node, acc -> {node, [meta[:line] | acc]}
  node, acc -> {node, acc}
end)
```

The authority stays with Elixir. Measured on a real file:

| source | definition lines returned |
|---|---|
| three plain tests at 3, 6, 9 | `3, 6, 9` |
| `@tag :slow` above a test | the `test` line, not the tag line |
| a test inside `describe` | found |
| bodiless `test "not implemented"` | found |
| a file that will not parse | `:error` → refused as `COLLECTION_OR_COMPILE_ERROR`, nothing runs |

Preflight refuses, with a stable code and nothing executed:

| case | code |
|---|---|
| line is inside a test body (`:4` for a test at 3) | `not_a_definition_line` |
| line before the first test | `not_a_definition_line` |
| line past the end (`:999`) | `not_a_definition_line` |
| file has no `test` macros | `no_tests_in_file` |
| file does not parse | `unparseable_test_file` |
| `elixir` not on PATH | `runner_toolchain_missing` |

The last one matters: preflight needs the Elixir toolchain, and a missing one is
an instrument error (`unverifiable`), never a pass through.

### 4.2 classify, measured

| observation | outcome |
|---|---|
| exit 2 and the summary reports ≥1 failure | `TESTS_FAILED` |
| exit 0 and the summary reports ≥1 test, 0 failures | `TESTS_PASSED` |
| exit 0 and the summary reports **0 tests** | `SELECTION_FAILED` |
| exit 1 and `Compilation error in file` | `COLLECTION_OR_COMPILE_ERROR` |
| exit 1 and `Paths given to "mix test" did not match` | `SELECTION_FAILED` |
| anything else | `UNRECOGNIZED` |

### 4.3 prove_selected

- `TESTS_FAILED`: the failure block names a location, and it must be **exactly**
  the requested project-relative `path:line`:

  ```
    1) test calls missing module (ProbeTest)
       test/probe_test.exs:9
  ```

  Equal → `PROVEN`; a different location → `REFUTED`; no location found →
  `UNKNOWN`. This is what catches `:999` even if preflight were bypassed —
  belt and braces, since the two mechanisms fail in different ways.

- `TESTS_PASSED`: `PROVEN` only when preflight validated the definition line
  **and** the summary reports exactly one test. Otherwise `UNKNOWN`.

### 4.4 The compile-error nuance

*A test calling a missing module is an honest RED only if it is a runtime
failure, not a compile error of the file.* Measured — Elixir treats an undefined
module as a **compile-time warning and a runtime `UndefinedFunctionError`**, so
the ordinary write-the-test-first shape does produce a real red:

```
warning: Missing.thing/0 is undefined (module Missing is not available…)
  1) test calls missing module (ProbeTest)
     test/probe_test.exs:9
     ** (UndefinedFunctionError) function Missing.thing/0 is undefined
1 test, 1 failure          exit 2
```

A file that will not *compile* never reaches ExUnit: exit 1 with
`== Compilation error in file …`, and **no run summary**. The two are
distinguished by the presence of a summary, not by guessing from message text —
which is why `classify` keys on the summary.

## 5. Selecting the adapter — and refusing a mismatch

```yaml
execution_mode: tdd
tdd_runner: exunit        # pytest | exunit
```

- **Stated → that adapter**, and the command is then checked against it.
- **Absent →** inference is allowed **only** when the command is unambiguously
  pytest (the token check shipped in #199). Anything else is refused with a
  message naming `tdd_runner` and its accepted values.
- **An unknown value** is a `ConfigError` at load and in `validate`, like
  `execution_mode` and `spec_profile` before it.
- **Stated but incompatible with the command** — `tdd_runner: pytest` with
  `mix test` — is a **`ConfigError` at load and an error in `validate`.** The
  declaration chooses the semantics; it cannot prove the command can carry
  them. Letting it through would mean interpreting one runner's exit codes as
  another's on the strength of a typo, which is #198 returning through an
  explicit config key.

A non-standard wrapper (`make test`, `./scripts/test.sh`) is not resolved by
ignoring the mismatch. Each adapter states its **command contract** —
`validate_command` — and a project whose wrapper does not satisfy it is refused
with what is missing. Widening a contract is a deliberate change to an adapter,
reviewed as such.

`tdd_runner` joins `gates.POLICY_KEYS`, so it is inside the `config_hash` a gate
verdict is bound to: changing the adapter changes what "confirmed" meant, and
must invalidate an existing checkpoint verdict rather than inherit it.

`tdd_runner` is a new public config key, so the release carrying it is a
**minor**.

## 6. Acceptance — contract tests that actually run

| ExUnit case | verdict |
|---|---|
| selected test passes | `not_red` |
| assertion failure | `expected_fail` |
| missing module inside a genuinely executed test | `expected_fail` |
| nonexistent file | `unverifiable` |
| compile error in the test file | `unverifiable` |
| pytest-style `path::name` | refused by `parse_selector`, nothing runs |
| unknown runner | `unverifiable` |
| **line past the end of the file (`:999`)** | **`unverifiable`** — measured; the case that looks most like success |
| **line before the first test (`0 tests`)** | **`unverifiable`**, not `not_red` |
| **line inside a test body (`:4`)** | **`unverifiable`** — refused at preflight |
| `tdd_runner: pytest` with `mix test` | refused by config validation |

pytest keeps its existing matrix unchanged.

### The CI job is required, and may not skip

A suite that skips loudly is still a green suite that checked nothing — the same
class of problem as everything else in this document. So the ExUnit contract
matrix gets **its own required job**:

- pinned Elixir/OTP versions (`erlef/setup-beam`), so a verdict change caused by
  a toolchain upgrade is visible as a version bump rather than as a mystery;
- runs the contract matrix against a real fixture `mix` project;
- **asserts the number of contract tests that ran** and fails if any were
  skipped — the guard that a `mix`-missing environment cannot masquerade as a
  pass;
- required for merge.

The ordinary Python matrix may keep skipping these when `mix` is absent, because
the dedicated job is what makes the claim.

## 7. What this does not do

- **No third runner.** Go, Jest and RSpec stay refused until someone measures
  them. The list is of what was measured, not of what probably behaves alike.
- **No mutation probing.** Whether the red *would* have passed after the fix is
  a separate policy (#159, closed as out of scope).
- **No change to the prompt's contract beyond the selector.** The RED prompt
  gains a per-runner selector instruction; nothing else moves.
- **No tolerance for near-miss selectors.** A line inside a test body is
  refused even though ExUnit resolves it correctly (owner's call, accepted): a
  rule that accepts "near enough" cannot distinguish near-enough from `:999`,
  since both resolve backwards to some other test.

## 8. Build order

1. Types and the registry (`Selector`, `SelectorRefusal`, `RunOutcome`,
   `SelectionProof`, adapter lookup) with pytest as the only adapter —
   behaviour-preserving, proven by the existing pytest tests.
2. `tdd_runner` config key, validation, mismatch refusal, `POLICY_KEYS`.
3. The ExUnit adapter plus its preflight.
4. The required CI job and the contract matrix.
5. Minor release, then the published-artifact matrix, then the kapelle pilot.
