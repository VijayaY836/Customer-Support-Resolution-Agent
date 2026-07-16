# Architecture

## Models in use

The agent runs on whatever model `OPENROUTER_MODEL` points to via
OpenRouter. **Every number in `eval/eval_report.md` was produced with
`openai/gpt-4.1-mini`** — not Claude, despite the code's fallback default
still being an Anthropic slug (a leftover from an earlier version; see
README's "Which model this actually uses"). The LLM-as-judge evaluation
layer (below) uses a separate model, `OPENROUTER_JUDGE_MODEL`, defaulting to
`openai/gpt-4.1` — deliberately never the same model that ran the agent
being judged.

## Pipeline

```
                                   ┌─────────────────────────┐
                                   │   CLASSIFIER (1 call)   │
  ticket text ─────────────────►  │  route + confidence +   │
                                   │  reasoning, as JSON      │
                                   └────────────┬─────────────┘
                                                │
                     ┌──────────────────────────┼──────────────────────────┐
                     ▼                          ▼                          ▼
              auto_resolve              draft_for_review                escalate
                     │                          │                          │
                     ▼                          ▼                          ▼
        ┌────────────────────────────────────────────────┐        ┌───────────────┐
        │      RESOLUTION LOOP (tool-calling, ≤6 turns)    │        │  no tools —   │
        │  search_kb / get_order_status  ──►  reasoning    │        │  write a      │
        │  issue_refund ──► always intercepted, see below  │        │  handoff note │
        └───────────────────────┬──────────────────────────┘        └───────┬───────┘
                                 │                                           │
                    (only if issue_refund was called)                       │
                                 ▼                                           │
                     ┌───────────────────────┐                              │
                     │   HUMAN APPROVAL GATE  │                              │
                     │  (code-enforced, not   │                              │
                     │   prompt-enforced)      │                              │
                     │  pending → approve/deny │                              │
                     └───────────┬─────────────┘                            │
                                 │                                           │
                                 ▼                                           ▼
                     ┌───────────────────────────────────────────────────────┐
                     │              TRACE LOG (one JSON per ticket)          │
                     │  route, reasoning, every tool call + args + result,   │
                     │  approval events, final output, timestamps           │
                     └───────────────────────────────────────────────────────┘
```

Each box is a real module:

| Stage | Code |
|---|---|
| Classifier | `agent.classify_ticket` — one LLM call, forced-JSON output, fails safe to `escalate` if unparseable |
| Resolution loop | `agent.run_resolution_loop` — standard OpenAI-style tool-calling loop, capped at `MAX_TOOL_ITERATIONS` |
| Tools | `tools.py` — `search_kb`, `get_order_status` are plain reads; `issue_refund`'s *schema* is offered to the model, but its execution is never reachable from the loop |
| Gate | `agent.dispatch_tool_call` — the single choke point every tool call passes through; `issue_refund` args become an `ApprovalQueue` entry instead of an executed action |
| Approval queue | `store.ApprovalQueue` — persisted to `traces/_approvals.json`; only `approve()` may call `tools._execute_refund_unsafe` |
| Trace | `store.TraceStore` — one JSON file per ticket, written incrementally as steps happen, not just at the end |

## Why the gate is where it is

The obvious place to put "require human approval" is in the system prompt
("never issue a refund without asking"). That's necessary but not
sufficient — it only holds as well as the model follows instructions, and
the eval set's adversarial tickets are specifically written to test what
happens when a ticket tries to talk the model out of that ("SYSTEM: admin
mode", "my manager already approved this", "override the KB"). So the gate
is duplicated one layer down, in `dispatch_tool_call`, where it is not a
matter of the model's judgment at all: the function that mutates order
state (`_execute_refund_unsafe`) is simply never called from the tool loop,
under any route, on any backend. `eval/run_eval.py`'s
`refund_not_executed_without_approval` check asserts this directly by
diffing `data/orders.json` before/after every ticket.

## Per-request backend override (web demo)

`app/main.py::run_ticket` accepts an optional `backend` field on each
request, letting the web UI run any single ticket — including the seeded
demo tickets — against `mock` or `openrouter` independently of the server's
default `LLM_BACKEND`. It works by swapping `config.LLM_BACKEND` for the
duration of one request, then restoring it in a `finally` block. This is
safe for a single presenter clicking one button at a time; it is **not**
safe under concurrent multi-user load, since two simultaneous requests could
interleave and briefly use each other's backend. Fine for a demo, not
production-ready as written — see "What to harden next."

## LLM-as-judge (evaluation only)

`llm.judge_compare()` sends a separate, typically stronger model
(`OPENROUTER_JUDGE_MODEL`) the same ticket's mock reply and real-LLM reply,
**blinded** as "Response A" / "Response B" — the judge is never told which
backend produced which reply, specifically to avoid it favoring "the LLM
one" purely because it expects LLM output to be better. It returns a
winner, a 1–5 score for each response, and a short justification.

This exists because the main eval's checks (route correctness, tool-call
correctness, no invented facts) can only measure whether a reply is
*technically correct* — they can't measure whether it's *well-written*. A
reply can pass every deterministic check and still read worse than an
alternative; the judge measures that gap. It is deliberately **additive**:
it never affects the pass/fail scorecard, and running it (`--judge`, only
valid combined with `--compare`) costs one extra API call per ticket on top
of the normal openrouter run.

On this project's actual eval set, the judge did not produce a clean sweep
for the real model (9 real-LLM wins / 6 mock wins / 2 ties out of 17) —
mock's canned replies paste the full KB policy article verbatim, which the
judge sometimes scores as more complete even though it reads less
naturally. Worth knowing before presenting this number: it is not a bug,
and a judge that always favored "the LLM one" would itself be a red flag
for judge bias, not a good sign.

## Governance / fairness check

`eval/fairness_tickets.json` holds 4 scenario groups (order status, refund
request, password reset, damaged item), each repeated 3 times with only the
customer's name changed — everything else in the ticket text is identical.
`run_fairness_eval()` runs all 12 through the agent and checks, per group,
whether every name produced the same route and whether confidence varied by
more than 0.15 across names. Written to `eval/fairness_report.{json,md}`,
kept fully separate from the main scorecard.

**This is a first-pass tripwire, not a rigorous fairness audit.** 12
tickets, one identity variable (name only), no statistical significance
testing, and the 0.15 confidence-spread threshold is a reasonable-sounding
number, not a validated one. It is only meaningful against a real LLM
backend — the mock backend never reads customer names at all (pure keyword
matching on complaint text), so it will trivially show zero divergence
regardless of what's tested, and the report says so explicitly rather than
letting that read as a real finding.

## Named limitation

**The classifier and resolver share no memory across tickets, and the KB
search is a keyword-overlap lookup, not embeddings.** Two consequences:
1. A customer who fragments one issue across two tickets ("my order's
   late" then later "actually can I get a refund for that") is handled as
   two unrelated tickets with no shared context — a human would connect
   them, the agent currently won't.
2. The KB search will miss a real match if the customer's phrasing shares
   no keywords with the article (e.g. "the box was crushed" won't obviously
   match `kb-005`'s "damaged or defective" unless the LLM's own tool-call
   query bridges the gap, which it usually but not always does).

**The fairness check is a first-pass tripwire, not a comprehensive audit**
(see above) — a real deployment would need a much larger, more rigorous test
set before treating a clean result as evidence of fairness.

## What to harden next

1. **Thread/session memory** — key traces by customer or conversation ID
   (not just ticket ID) so repeated contact on the same issue is visible to
   the classifier and doesn't get re-escalated or re-drafted from scratch.
2. **Real embeddings for `search_kb`** — swap the keyword-overlap scorer for
   a small embedding index (even a local one) so KB recall doesn't depend on
   literal word overlap.
3. **A second, independent classifier pass for adversarial detection** —
   right now injection resistance relies on the same model that resolves
   the ticket also noticing the injection in its system prompt. A cheap,
   separate "does this ticket try to instruct the agent?" check before the
   main classifier would decouple that safety property from the main
   model's general instruction-following quality.
4. **Idempotency keys on `issue_refund`** — currently nothing stops the same
   ticket from filing two approval requests for the same order if the model
   retries; worth deduplicating in `ApprovalQueue.create`.
5. **Concurrency-safe backend override** — the per-request `LLM_BACKEND`
   swap in `main.py` (see above) is scoped correctly for one user, but would
   need a request-local context instead of a shared global under real
   multi-user load.
6. **A larger, statistically grounded fairness test set** — expand past 12
   tickets / one identity variable (name) to cover more scenarios, more
   identity signals, and an actual significance test rather than a fixed
   0.15 threshold.