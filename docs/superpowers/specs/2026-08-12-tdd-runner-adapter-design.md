# TDD runner adapters — design (#198 step 2)

**Status:** design, for sign-off. No code until it is approved.

Step 1 (#199, shipped) made a confirmed RED reachable only for pytest and
refused every other runner. That is safe and it is not enough: TDD mode is
unusable outside Python, and the pilot that found the defect — three real TDD
tasks on **kapelle**, an Elixir product — cannot start.

This is the extension. It is written after measuring ExUnit rather than before,
and the measurements changed the contract twice; both changes are marked
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

This is why the owner's two proofs are the contract and not a refinement:

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

    def parse_selector(self, raw: str) -> Selector | SelectorRefusal
    def build_command(self, test_command: str, selector: Selector) -> list[str]
    def classify(self, result: CompletedProcess) -> RunOutcome
    def prove_selected(self, selector: Selector, result: CompletedProcess) -> bool
```

`Selector` is a frozen dataclass — `path` plus a `locator` whose shape is the
adapter's business (`node_id` for pytest, `line` for ExUnit) — so nothing
outside an adapter ever splits a selector on `::` again.

`parse_selector` returns a refusal rather than raising, because a malformed
selector is a normal outcome of asking an agent for one and belongs in the
checkpoint record like any other verdict.

### RunOutcome

```
PASS                        the selected test ran and passed
TEST_FAILED                 the selected test ran and failed as a test
SELECTION_FAILED            the runner ran but did not select what was asked
COLLECTION_OR_COMPILE_ERROR the test could not be built or collected
RUNNER_ERROR                the runner itself failed (usage, crash, timeout)
UNRECOGNIZED                the adapter cannot read this output
```

Mapping to what the checkpoint stores:

| RunOutcome | + `prove_selected` | `RedOutcome` |
|---|---|---|
| `TEST_FAILED` | true | **`expected_fail`** |
| `TEST_FAILED` | false | `unverifiable` |
| `PASS` | true | `not_red` |
| `PASS` | false | `unverifiable` |
| everything else | — | `unverifiable` |

`prove_selected` is consulted for `PASS` too. "The test you named passes" and
"some other test passed" are different facts, and only the first refutes a
claimed red.

`UNRECOGNIZED` exists so that an adapter meeting output it was not written for
says so, instead of falling back to the exit code — the failure mode of the
original defect, one level up.

## 3. pytest adapter

- **Canonical selector:** the node id, `path::test` (`path::Class::test` too).
- `build_command`: `<test_command> <node_id>`.
- **classify**, from the measured table already in `tdd.py`: exit 0 → `PASS`;
  1 → `TEST_FAILED`; 4 → `COLLECTION_OR_COMPILE_ERROR` (an unresolvable node id
  and a syntax error both give 4 — measured, pytest 8); 5 → `SELECTION_FAILED`
  ("no tests collected"); anything else → `RUNNER_ERROR`.
- **prove_selected:** the node id appears in the reported failure header, or
  the summary reports exactly one test collected. pytest exits 4 rather than 1
  for a bad node id, so the proof is a guard rather than the load-bearing
  check — which is precisely why pytest was safe to keep in step 1.

Behaviour for existing pytest projects does not change; the contract tests from
`test_red_checkpoint.py` are the regression proof.

## 4. ExUnit adapter

- **Canonical selector (v1):** `path:line`, where **line is the `test "..." do`
  line**. A pytest-style `path::name` is refused by `parse_selector` before
  anything runs — it is what the current RED prompt asks for, and it is how the
  defect got in.
- `build_command`: `<test_command> <path>:<line>` (`mix test test/x_test.exs:9`).
- **classify**, measured:

  | observation | outcome |
  |---|---|
  | exit 2 and the summary reports ≥1 failure | `TEST_FAILED` |
  | exit 0 and the summary reports ≥1 test, 0 failures | `PASS` |
  | exit 0 and the summary reports **0 tests** | `SELECTION_FAILED` |
  | exit 1 and `Compilation error in file` | `COLLECTION_OR_COMPILE_ERROR` |
  | exit 1 and `Paths given to "mix test" did not match` | `SELECTION_FAILED` |
  | anything else | `UNRECOGNIZED` |

- **prove_selected:** the failure block names a location, and it must be the
  requested one:

  ```
    1) test calls missing module (ProbeTest)
       test/probe_test.exs:9
  ```

  The reported `path:line` must equal the requested `path:line`. This is what
  catches `:999`, which otherwise looks exactly like a real red.

  Consequence, deliberate: a selector pointing *inside* a test body (`:4` for a
  test defined at 3) is refused as unproven even though ExUnit runs the right
  test. The agent is told to report the definition line; a rule that accepts
  "near enough" cannot distinguish near-enough from the `:999` case, since both
  resolve backwards to some other test.

### The compile-error nuance

The owner's point, and it is the interesting case: *a test calling a missing
module is an honest RED only if it is a runtime failure, not a compile error of
the file*. Measured — Elixir treats an undefined module as a **compile-time
warning and a runtime `UndefinedFunctionError`**, so the ordinary
write-the-test-first shape does produce a real red:

```
warning: Missing.thing/0 is undefined (module Missing is not available…)
  1) test calls missing module (ProbeTest)
     test/probe_test.exs:9
     ** (UndefinedFunctionError) function Missing.thing/0 is undefined
1 test, 1 failure          exit 2
```

A file that will not *compile* never reaches ExUnit: exit 1 with
`== Compilation error in file …`, no summary line. The two are distinguished by
the presence of a run summary, not by guessing from the message — which is why
`classify` keys on the summary rather than on error text.

## 5. Selecting the adapter

No deeper heuristics on `test_command`. An explicit key:

```yaml
execution_mode: tdd
tdd_runner: exunit        # pytest | exunit
```

- Stated → that adapter, no inference.
- Absent → inference is allowed **only** when the command is unambiguously
  pytest (the token check shipped in #199). Anything else is refused with a
  message naming `tdd_runner` and its accepted values.
- An unknown value is a `ConfigError` at load and in `validate`, like
  `execution_mode` and `spec_profile` before it.
- Stated but contradicted by the command (`tdd_runner: pytest` with
  `mix test`): the stated value wins and the run proceeds. The operator's
  declaration is the authority; second-guessing it would reintroduce inference
  through the back door. The mismatch is logged.

`tdd_runner` is a new public config key, so the release carrying it is a
**minor**, per the owner's step 4.

## 6. Acceptance — the #198 matrix as contract tests

Run against a real `mix` project, not a fake:

| ExUnit case | verdict |
|---|---|
| selected test passes | `not_red` |
| assertion failure | `expected_fail` |
| missing module inside a genuinely executed test | `expected_fail` |
| nonexistent file | `unverifiable` |
| compile error in the test file | `unverifiable` |
| pytest-style `path::name` | refused by selector validation, nothing runs |
| unknown runner | `unverifiable` |
| **line past the end of the file (`:999`)** | **`unverifiable`** — added from measurement; it is the case that looks most like success |
| **line before the first test (`0 tests`)** | **`unverifiable`**, not `not_red` |

pytest keeps its existing matrix unchanged.

The Elixir contract tests need `mix` on the runner. They are marked `slow` and
skipped when `mix` is absent, and the skip is **loud** in CI rather than silent
— a green suite that quietly tested nothing is the same class of problem as
everything else in this document.

## 7. What this does not do

- **No third runner.** Go, Jest and RSpec stay refused until someone measures
  them. The list is of what was measured, not of what probably behaves alike.
- **No mutation probing.** Whether the red *would* have passed after the fix is
  a separate policy (#159, closed as out of scope).
- **No change to the prompt's contract beyond the selector.** The RED prompt
  gains a per-runner selector instruction; nothing else moves.

## 8. Open question for sign-off

The `:4` consequence in §4 — refusing a selector that points inside a test body
even though ExUnit resolves it correctly. The alternative is to parse the test
file for test spans and accept any line within one, which buys tolerance for a
mistake the agent is instructed not to make, and costs a second parser of
someone else's language. Recommendation: refuse, and say why in the message.
