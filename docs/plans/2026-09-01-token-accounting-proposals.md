# Proposals: counting tokens, not dollars

**Date:** 2026-09-01
**Status:** Proposals only — nothing decided, nothing scheduled. Written so the
measurements behind them survive the conversation that produced them.
**Origin:** #330 (an operator facing two contradictory ceilings) turned into a
wider question: what is the ceiling worth at all, when the unit it is measured
in only exists for one of the eight supported CLIs?

**Non-goals of this document:** choosing between the proposals, sizing them, or
committing to any. P1 and P2 are cheap and independently correct; P3–P5 are
directions, and P5 in particular is a business decision before it is an
engineering one.

## What was measured

Every claim below was checked against the code on `master` (2026-09-01) or run
under pytest. None is inferred from the prose.

1. **Cost is reported, never computed.** There is no price table anywhere:
   `grep -riE "price|per_token|pricing|1e6"` over `src/` returns only the word
   *unpriced*. Tokens are never multiplied by a rate, and no module knows what
   any model or provider charges.

2. **Two extraction paths, keyed on `result_format`** (`runner.py`,
   `build_cli_invocation` → `parse_cli_result`):
   - `claude_json` — only for an explicit claude binary with `json_output`.
     Cost is `total_cost_usd`; tokens are `usage.input_tokens` /
     `usage.output_tokens`.
   - `text` — everything else (codex, opencode, pi, ollama, llama-cli,
     llama-server, qwen, copilot, **and claude driven by a `command_template`**,
     which bypasses `json_output` deliberately). `parse_token_usage` regexes
     stderr for `input_tokens: N`, `output_tokens: N`, `cost: $X`, and returns
     `None` for whatever is not printed.

3. **Tokens are already stored, and nothing gates on them.** Both ledgers carry
   `input_tokens`/`output_tokens` (`attempts`, `agent_calls`), `total_tokens()`
   sums them exactly as `total_cost()` sums money, and `costs` prints them.
   `grep -riE "token_budget|max_tokens|token_limit"` over `src/` and `schemas/`
   is empty: the only unit allowed to stop anything is the dollar.

4. **The claude token figure is incomplete today.** `_parse_claude_json` reads
   `usage.input_tokens` and `usage.output_tokens` only;
   `cache_creation_input_tokens` and `cache_read_input_tokens` appear **zero**
   times in `src/` and `tests/`. With prompt caching on, cache reads are
   normally the bulk of the input. `total_cost_usd` has no such gap — it is
   computed on the provider's side, with caching already priced in. So today
   the dollar is the only field that knows the whole story, and our token count
   is a systematically low fragment of it.

5. **The unpriced invariant has a blind spot on the most expensive call.**
   `unmeasured_calls()` counts rows in `agent_calls` only. The exec pass records
   its cost into `attempts.cost_usd`, and `cost_usd = cli_result.cost_usd` means
   an unreported cost is stored NULL there. Measured under pytest: an attempt
   with `cost_usd=None` under `budget_usd=0.01` leaves `total_cost()` at `0.0`,
   `unmeasured_calls()` at `0`, and `check_before_call` returning `None` — the
   guard proceeds forever. One unpriced row in the *ledger*, by contrast,
   refuses the next call immediately (`unpriced | 1 earlier call(s) reported no
   cost … Recorded spend is a floor ($0.00 for this run)`).

6. **Therefore a USD ceiling has three regimes, and names none of them.**
   *Real* (claude in JSON mode — the guarantee holds: no new paid call once
   recorded spend reaches the limit, overshoot bounded by one call). *Silent*
   (a non-reporting CLI with no ledger rows — spend reads $0.00 and the ceiling
   never binds). *Hair-trigger* (a non-reporting CLI with review or TDD — the
   first unpriced ledger row stops everything after it). The operator is told
   which regime they are in by nothing.

## P1 — Read the cache token fields

Add `cache_creation_input_tokens` and `cache_read_input_tokens` to
`_parse_claude_json` and decide, once, how they enter `input_tokens`.

Buys: a token figure that means what the provider means by it. This is a
**prerequisite** for every proposal below — a ceiling built on today's count
would be a ceiling on a fraction of the traffic.

Costs: a schema question, not a code one. `attempts`/`agent_calls` have two
token columns; either the cache tokens are folded into `input_tokens` (cheap,
lossy, and changes the meaning of an existing column that Maestro's vendored
copy pins) or they get their own columns (a schema change, hence a version
decision). The lossy option is not obviously wrong, but it is a contract edit
either way, and that is the whole of this proposal's cost.

Cannot do: anything for a CLI that reports no usage at all.

## P2 — Make the unpriced invariant cover the exec pass

Count an attempt with a NULL `cost_usd` as an unmeasured call, the way a ledger
row already is.

Buys: the *silent* regime stops existing. A project on a CLI that reports
nothing gets the documented honest answer — "this CLI cannot be combined with a
budget" — instead of a ceiling that quietly never fires. This is a defect in
its own right, independent of tokens; it is listed here because any new unit
would inherit the same blind spot.

Costs: projects currently running unpriced CLIs under a nominal budget will
start being refused where they used to proceed. That is the correct behaviour
and still a behaviour change, so it wants a release note rather than a patch.

Cannot do: distinguish "free" from "unknown". A local ollama run genuinely
costs nothing and will be refused exactly like an unreported paid call. If that
matters, it is a separate proposal: a per-CLI declaration that its calls are
free, which is an operator's claim rather than a measurement.

## P3 — Count our own tokens as a floor

We hold the exact prompt of every paid call (`prompts_log` writes it to disk)
and the answer text. Tokenising both gives a number that needs no cooperation
from the CLI.

Buys: the only figure available at all for codex / opencode / pi / ollama /
llama — a floor instead of a zero, printed as such.

Cannot do — and this is the load-bearing caveat: an agentic CLI makes many
model calls inside one invocation of ours (file reads, tool calls, repeated
passes), and we see only the first input and the last output. For claude and
codex the self-count understates by a large and unknown factor. It is honest as
a floor, worthless as a ceiling's denominator. For non-agentic local runs
(ollama, llama-cli) it is close to the truth.

Open question if this is ever taken: which tokeniser, and whether a wrong-model
tokeniser's error is smaller than the error it replaces.

## P4 — A token ceiling as a second unit

Add a token-denominated ceiling alongside the USD one. The machinery is
unit-agnostic: `budget authorize` (monotonic, CAS, audited, reserved stages),
`effective_limits`, the pre-call guard, and the three refusal kinds all work on
"a number and a recorded total". Only the number changes.

Buys: a unit that exists without a price table, that survives a provider or
model swap unchanged, that means the same thing on a subscription as on an API
key, and that matches what providers actually ration (rate limits and context
are counted in tokens; dollars are our own invention).

Requires: P1 and P2 first, or the ceiling is measured against a fraction of the
traffic at one site and against silence at another.

Open questions: whether the two units coexist (both bind, first to trip wins)
or the token one replaces USD; whether an authorization can raise one and not
the other; and what `--json-result` / `costs` show, since both are pinned
surfaces.

## P5 — A proxy in front of the CLIs (LiteLLM + Langfuse)

The observability shape is right and maps onto what we already store without
inventing a vocabulary:

```
session   = the budget domain (one state DB, i.e. one --spec-prefix)
trace     = one task attempt
generation= one paid call — exactly our agent_calls(task_id, provenance) row,
            where provenance is already red_authoring / green / review:<role>
```

Buys: usage and cost from the provider's own accounting, including cache
tokens; one ledger across every CLI instead of per-CLI parsing; a session view
that answers "what did this task actually cost" without our summing anything;
and the end of the `parse_token_usage` stderr regexes.

Blocked on two things, neither of them ours to decide:

1. **We do not own the HTTP client.** Our agents are CLI subprocesses. A proxy
   sees their traffic only where the CLI allows the endpoint to be moved
   (`ANTHROPIC_BASE_URL` for claude, `OPENAI_BASE_URL` for codex); ollama and
   llama-cli are local and would need adapters or stay outside. A CLI that
   pins its endpoint cannot be observed this way at all.
2. **It changes who is billed.** Routing claude through a proxy means an API
   key instead of a subscription — work that is currently covered by a flat
   plan starts being metered per token. That is a cost decision, and it should
   be made as one rather than arrived at as a side effect of wanting better
   numbers.

If it is ever taken, the honest scope is a *mode*, not a migration: projects
that opt in get provider-side accounting, the CLI-parsing path stays for those
that do not, and the two must produce numbers that can be compared — which is
its own piece of work.

## Relation to #330

The accepted item is about an operator seeing two ceilings and being told about
neither domain. It stands on its own and does not wait for any of this. But the
discussion that produced these proposals also settles its part 3: if the
ceiling is a deliberately-raised lifetime line on a domain, and the domain is
the state file, then per-prefix ceilings are the correct semantics and the
docstring is the thing that is wrong — option A, not option B.
