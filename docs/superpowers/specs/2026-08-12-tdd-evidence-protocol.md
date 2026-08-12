# What would justify an automatic REFACTORING pass (#141, slice 3b)

**Status:** 3b is **deferred** — a hypothesis to be tested, not a design to be
built. Slices 0–4a are shipped and #141 is functionally complete without it.

**Owner's trigger, verbatim:** at least **three real TDD tasks** show a
repeatable class of post-GREEN defects that a separate refactor pass fixes
better than review, or than simply doing the next ordinary task.

Until that evidence exists, an automatic refactor is one more expensive LLM
call, a new mutation stage, and one more recovery surface — with no demonstrated
benefit. Each of those three costs is real and already paid for elsewhere in
this system; none of them is worth paying twice on a guess.

## Why a protocol rather than "we'll see"

The three runs are only evidence if they are recorded the same way. A vague
memory of "it felt like it needed cleanup" cannot distinguish a refactor pass
from a slightly better review prompt, and that distinction is the entire
decision. So each run is recorded with the same six fields, before anyone
argues about what they mean.

Prefer three runs **different in shape** — a greenfield module, a change to
existing code with real callers, a bug fix with a regression test. A repeatable
class that appears in only one shape is a property of that shape.

## The record, per run

1. **Product verdict** — did spec-runner do the right thing? Terminal state,
   exit code, what the gates decided and against which SHA.
2. **Harness verdict** — did the *stand* mislead? The scaffolding, the config,
   the agent's environment, my own driving. Kept separate from the product
   verdict on purpose: the battle reports repeatedly found my mistakes and the
   tool's failures wearing the same clothes, and a defect attributed to the
   wrong side is worse than an unattributed one.
3. **Phase / checkpoint / remedy lineage** — `spec-runner tdd status TASK-XXX
   --json`. Which phases were entered, which checkpoints exist and their
   verdicts, whether any `abandon`/`repair` happened and why.
4. **Cost, RED against GREEN and review** — from `spec-runner costs --json` and
   the `agent_calls` ledger. The question 3b really asks is "is another agent
   call worth it", and that cannot be answered without knowing what the current
   calls cost.
5. **Interventions** — every point where a human touched the run: an override,
   a manual commit, a `--allow-dirty-spec`, a nudge to the agent. An
   intervention is not a failure, but an unrecorded one turns a run into
   anecdote.
6. **Post-GREEN debt** — did a *repeatable* class of defect survive to green?
   Name it concretely (duplication the tests locked in, a name that stopped
   being true, an abstraction the red forced and the green outgrew), and say
   whether review caught it, would have caught it, or could not have.

## Reading the evidence

- **Three runs, no repeatable class** → 3b stays deferred, and the record says
  why. That is a result, not a failure to gather data.
- **A repeatable class that review already catches** → improve review. Cheaper,
  no new stage, no new recovery surface.
- **A repeatable class review cannot catch, that a later ordinary task fixes
  anyway** → 3b still not justified: the debt is being paid, just later.
- **A repeatable class that survives review and persists across tasks** → the
  trigger is met, and *then* the six open questions from 2026-08-11 need
  answers before any code: is the second LLM call mandatory; what is its input
  and its exit criterion; may it touch claimed test files; does review repeat
  afterwards; what bounds its cost and its cycles; what does "improvement with
  no diff" mean.

## Where the runs are recorded

One file per run under `docs/superpowers/specs/`, named
`YYYY-MM-DD-tdd-run-<slug>.md`, with the six fields as headings. A summary
lands on #141 when the third one is done.

## Related

- Battle report of the surrounding machinery:
  `docs/superpowers/specs/2026-08-11-tdd-battle-report.md`
- Release/verification ritual these runs sit on: `docs/release-runbook.md`
- The claim and remedy contracts the lineage field reads from:
  `docs/superpowers/specs/2026-08-11-claim-and-remedy-contracts.md`
