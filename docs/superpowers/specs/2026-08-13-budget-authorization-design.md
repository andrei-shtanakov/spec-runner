# Budget authorization — design

**Status:** **signed off by the owner 2026-08-13.** Ready to implement; no code ships with this document.
**Issue:** #230 (F-26), part 2. Part 1 — typed infrastructure classification — shipped in #236.
**Related:** [the pre-call budget guard](../../../src/spec_runner/budget.py) (#213), [TDD remedies](2026-08-11-tdd-lifecycle-design.md) (#141 slice 3, whose command shape this follows)

---

## 1. What is already decided

The owner settled the direction on 2026-08-13, after the three candidates in
#230 were weighed against the guarantee #213 established.

**Rejected for now, kept as trigger hypotheses:**

- **Refunding infrastructure attempts.** The money is spent; a refund turns the
  cap from a wallet bound into a progress bound. A deterministic instrument
  failure then loops: every attempt refunded, real spend bounded only by
  `max_retries`. That is the opposite of what #213 built.
- **A separate infrastructure budget.** Classification would have to happen per
  *call*, and at the moment a RED authoring call is paid nobody knows yet
  whether the replay will fail for environment reasons — so it means
  retroactive reclassification, and `task_budget_usd` stops bounding the money
  because there are now two numbers.

Both become answerable once part 1's classification has produced statistics:
`costs` can then say how much a project actually loses to broken instruments,
and the choice stops being a guess from one run.

**Chosen: an explicit, audited authorization that raises the absolute limit.**
It is the only candidate after which the sentence *"the number bounds the
money"* is still true. A human raises a specific ceiling, deliberately, with a
reason, and the record says who and when.

---

## 2. The command

```
spec-runner budget authorize TASK-101 \
  --task-budget 6.00 \
  --run-budget 6.00 \
  --reason "Continuing the kapelle TDD pilot after F-25…F-28 were fixed"
```

Shape follows `tdd abandon` / `tdd repair` (#141 slice 3), which already solved
the same problem: an operator-only mutation of state that a later reader must be
able to audit.

| Property | Value | Why |
|---|---|---|
| `--reason` | **required** | an unexplained ceiling raise is indistinguishable from a mistake six weeks later |
| actor | recorded, `resolve_actor(config, --actor)` | "someone raised it" is not an audit trail |
| agent guardrail | refuse when `SPEC_RUNNER_AGENT=1` | an agent must not raise its own budget; a guardrail, not a boundary — the agent runs arbitrary shell — but the audit row then carries a human's name or nothing happens |
| live-run refusal | refuse while the PID-checked `ExecutorLock` is held | the guard reads limits mid-run; changing them under a running loop makes "what was authorised when the call started" unanswerable |
| monotonic | new limit must be **strictly greater** than the current effective one; a lower or equal value is refused | this command exists to unblock, never to tighten. Lowering is **not supported at all** — not by this command and not by any flag on it (§6) |
| CAS | `--after <auth-id>` when a previous authorization exists for the scope | the operator authorises against a state they have seen, exactly as `tdd repair --checkpoint` does |

**Both axes are needed, and neither implies the other.** Raising only
`task_budget_usd` leaves `budget_usd` refusing the very next call; raising only
the run budget leaves the task's own cap in place. The command therefore accepts
either or both, and refuses a call that names neither.

## 3. The record

Append-only table `budget_authorizations`, in the same family as
`tdd_remedies`:

| Column | Note |
|---|---|
| `id` | authorization id, referenced by a later `--after` |
| `domain_id` | the budget domain (§5) this decision belongs to |
| `scope` | `task` or `run` |
| `task_id` | set for `scope='task'`; NULL for `run` |
| `namespace` | the TDD workstream — set for `scope='task'`, **NULL for `scope='run'`**: `budget_usd` bounds the whole domain, and a namespaced run ceiling would give each namespace its own "global" cap (owner, sign-off) |
| `previous_limit_usd` | what the guard would have used a moment earlier — config value or an earlier authorization |
| `new_limit_usd` | the absolute ceiling from now on |
| `recorded_spend_usd` | spend at the moment of the decision |
| `unmeasured_calls` | how many calls in scope had no reported cost — because `recorded_spend` is a floor whenever this is non-zero (#213/#216), and a decision made against a floor must say so |
| `actor`, `reason`, `timestamp` | who, why, when |

Nothing is ever updated or deleted. A later authorization is a new row; the
effective limit is the newest row for the scope.

`recorded_spend_usd` and `unmeasured_calls` are the two columns that make the
record honest rather than decorative: they capture *what the human was looking
at*. "$6.00 authorised" means one thing against a recorded $2.53 and another
against a recorded $2.53 with four unpriced calls.

## 4. How the guard reads it

One resolver, used by both existing call sites — `budget.check_before_call`
(the pre-call guard) and `execution._check_task_budget` (between attempts):

```python
def effective_limits(config, state, task_id) -> tuple[float | None, float | None]
```

Precedence: **the newest authorization for the scope wins; otherwise the config
value.** Not `max(config, authorised)` — an operator who edits the YAML after
authorising deserves an answer that does not depend on which number is bigger.
What keeps this honest is that the authorised limit is *always displayed as
such*: `status`, `costs` and any refusal message name it, its actor and its
timestamp, so a config file that disagrees with the live ceiling can never be
read as the truth.

The guarantee from #213 is unchanged and must stay quotable: *once recorded
spend reaches the limit, no new paid call starts; the overshoot is bounded by
one call.* This design changes **which number** the limit is, never whether it
binds.

## 5. The budget domain — the boundary that matters most

**A budget lives in a state DB, and an authorization belongs to that same
domain.** The pilot proved why this must be written down: attempts 1–3 of
kapelle TASK-101 each ran with a different state file
(`.executor-m2-state.attempt1.db`, `.attempt2.db`, then a fresh
`.executor-m2-state.db`). The cap that refused at `$2.53 >= $1.82` had never
seen the `$1.19` spent by the first two attempts — the owner had encoded that
history by *lowering the ceiling by hand*.

Rules, to be enforced and documented:

1. **A new state file starts a new budget domain.** It inherits no
   authorization and no spend. Mechanically: a `budget_domain_id` minted lazily
   in `executor_meta` on first use; authorizations carry it; a fresh DB has a
   different id and therefore no authorizations at all.
2. **One canonical DB per pilot.** Resume and repair happen inside it. Rotating
   the state file mid-pilot is not a neutral act — it resets the financial
   record while leaving the work in place.
3. **Moving to a new DB requires an explicit opening balance** — an
   authorization row recording the prior spend as its `recorded_spend_usd`, or a
   fresh owner decision. Never a silent restart at zero.
4. **Archived DBs are evidence, not runtime inputs.** They are never summed
   into a live guard's arithmetic without a deliberate consolidation step.

### What this means for kapelle right now

Actual spend across the four state files is **at least $4.35**, and the review
call of attempt 3 was never priced (2.28.3 recorded no review cost), so the true
total is unprovable. The next paid attempt therefore cannot be authorised as
"the remainder of a budget": there is no provable remainder. It has to be a
**new additional amount over an acknowledged floor**, and the authorization row
should say exactly that — `recorded_spend_usd` = what this domain can prove,
`reason` naming the unprovable part.

## 6. Deliberate non-goals

- **No lowering.** Monotonic by construction. Lowering a ceiling an operator
  already authorised is either a mistake or a new decision; the answer to both
  is a new domain (a new state file, §5), not a quiet edit. If a real need
  appears, it is a separate decision — not a flag added later "because it is
  symmetric".
- **No automatic extension.** Nothing in the run may raise its own ceiling, for
  any reason, including an infrastructure failure. That is the whole difference
  between this and the refund model.
- **No `--force` past a live run.** The refusal in §2 has no override: the guard
  reading a limit mid-call is exactly the case the refusal protects.
- **No new interop surface in this step.** `--json-result` and the state-DB
  contract are untouched. Whether `costs --json` should expose the authorised
  limit is a real question — it is additive, and the schema is vendored by
  spec-runner-vscode with `additionalProperties: false`, so it is decided at
  implementation time with a schema-sync issue, not assumed here.

## 7. Settled at sign-off (2026-08-13)

All three recommendations approved, with one correction that changes the schema.

1. **A task authorization is scoped `(domain_id, namespace, task_id)`.** Two
   workstreams sharing one state DB is rare — but so was a state file per
   attempt, until it happened.
2. **A run authorization covers the whole budget domain**, not an invocation's
   `run_id`. A run id changes every invocation, so a ceiling that expired with
   it would have to be re-authorised constantly, which trains people to
   authorise without reading.
3. **A refusal quotes the authorization id, the effective limit, the actor and
   the timestamp.** An operator who has to go looking for the id before they can
   pass `--after` is an operator who will skip the CAS.

**Correction (owner):** for a run-scope row, `namespace` is **NULL**.
`budget_usd` bounds the whole DB domain, so a namespaced run ceiling would let
several namespaces each hold an independent "global" cap — three workstreams,
three global limits, no global limit. The column therefore means: set for
`scope='task'`, NULL for `scope='run'`, and the resolver reads a run
authorization by `domain_id` alone.

That constraint is worth enforcing rather than documenting: a `CHECK` that
`scope = 'run'` implies `namespace IS NULL` and `task_id IS NULL`, so the schema
cannot hold a row whose meaning is ambiguous.
