# Architecture

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