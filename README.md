# Dispatch — Customer Support Resolution Agent

An agent that reads incoming support tickets, classifies them into
`auto_resolve` / `draft_for_review` / `escalate`, resolves what it safely
can using tools (KB search, order lookup), and drafts replies for the rest —
with a hard, code-enforced human-approval gate on every refund. Every ticket
produces a structured, human-readable trace for evaluation.

See [`ARCHITECTURE.md`](ARCHITECTURE.md) for the one-page pipeline doc, and
[`eval/eval_report.md`](eval/eval_report.md) for the evaluation scorecard.

## Why it's built this way

The refund gate is enforced in **code**, in one place
(`app/agent.py::dispatch_tool_call`), not in a prompt. Every `issue_refund`
tool call — from any route, from any LLM backend, including the free mock
backend — is intercepted there and turned into a pending entry in the
approval queue. It is never executed inline. That means the safety property
("no refund goes out without a human") holds even if the classifier
misroutes a ticket or a customer tries to prompt-inject their way past it.
The adversarial test cases in the eval set exist specifically to prove this.

## Quick start

```bash
git clone https://github.com/VijayaY836/Customer-Support-Resolution-Agent
cd support-agent
python3 -m venv .venv && source .venv/bin/activate   # optional but recommended
pip install -r requirements.txt
```

### Option A — run fully offline first (no API key needed)

The project ships with `LLM_BACKEND=mock`, a small deterministic keyword
agent that needs no network access. Use it to see the whole pipeline (UI,
gate, traces, eval harness) working before spending any API credits.

```bash
python3 cli.py demo
```

You should see the three seeded tickets run, with the third one
(`"I want a refund..."`) hitting the gate and printing an `approval_id`.
Approve it:

```bash
python3 cli.py approve appr-xxxxxxxx
```

### Option B — run against the real Claude model via OpenRouter

1. Get a key at https://openrouter.ai/keys
2. Copy `.env.example` to `.env` and fill in:
   ```
   LLM_BACKEND=openrouter
   OPENROUTER_API_KEY=sk-or-...
   OPENROUTER_MODEL=anthropic/claude-sonnet-4.5
   ```
   (Swap the model slug for `anthropic/claude-haiku-4.5` if you want cheaper
   eval runs, or check https://openrouter.ai/models?q=claude for the latest
   Sonnet/Opus slugs.)
3. Export the vars (or use `python-dotenv` / your shell's `.env` support),
   then:
   ```bash
   export $(cat .env | xargs)
   python3 cli.py demo
   ```

## Running the web demo

Single-origin FastAPI app — one process serves both the API and the UI, so
there's no CORS setup.

```bash
uvicorn app.main:app --reload --port 8000
```

Open http://localhost:8000. Click **"Run 3 seeded demo tickets"** to replay
the demo script live: order lookup → password reset → refund request that
trips the gate. Approve or deny it from the right-hand panel and watch the
KPI strip and the ticket's own trace card update.

## Running the CLI

```bash
python3 cli.py run "Where's my order #4471?"
python3 cli.py demo
python3 cli.py approvals
python3 cli.py approve appr-xxxxxxxx --note "verified against order history"
python3 cli.py deny appr-xxxxxxxx --note "no evidence on file"
```

## Running the evaluation harness

This is the graded part — see [`eval/eval_report.md`](eval/eval_report.md)
for the current scorecard (17 labeled tickets: 5 easy auto-resolve, 5
ambiguous, 4 escalation, 3 adversarial/prompt-injection).

```bash
# Free, offline, uses the naive keyword baseline:
python3 eval/run_eval.py --backend mock

# Against the real model (needs OPENROUTER_API_KEY):
python3 eval/run_eval.py --backend openrouter

# Both, as a before/after comparison:
python3 eval/run_eval.py --compare
```

This regenerates `eval/eval_report.json` (raw) and `eval/eval_report.md`
(the scorecard, with the three adversarial tickets called out explicitly).
The eval run never calls `approve()`, so it never mutates `data/orders.json`
— any ticket where an order ends up `refunded` after an eval run indicates a
gate failure, and the harness asserts against exactly that.

## Project layout

```
app/
  config.py    # env-driven config (LLM_BACKEND, OPENROUTER_*, paths)
  llm.py       # LLMClient interface: OpenRouterClient (real) + MockLLMClient (free baseline)
  tools.py     # search_kb, get_order_status, issue_refund + their schemas
  store.py     # TraceStore (per-ticket JSON traces) + ApprovalQueue (the gate's state)
  agent.py     # classify -> route -> tool-calling loop -> gate -> log
  main.py      # FastAPI app: API + serves static/index.html
cli.py         # terminal runner (run / demo / approvals / approve / deny)
data/
  orders.json  # mock order DB (mutated only by an approved refund)
  kb.json      # mock knowledge base
eval/
  test_tickets.json  # 17 labeled tickets
  run_eval.py         # scoring harness -> eval_report.{json,md}
static/
  index.html   # the demo UI (vanilla HTML/CSS/JS, no build step)
traces/        # one JSON file per ticket run (gitignored, generated)
```

## Deploying the web demo

The FastAPI app is a single process serving both API and static UI, so it
deploys the same way as any small Python web service (e.g. Render): pin
`python-3.12.x` in a `runtime.txt` if your host needs it, set
`LLM_BACKEND`, `OPENROUTER_API_KEY`, and `OPENROUTER_MODEL` as environment
variables, and start with:

```
uvicorn app.main:app --host 0.0.0.0 --port $PORT
```

Note that `traces/` and `data/orders.json` are written to at runtime — on
most PaaS free tiers that storage is ephemeral (resets on redeploy/restart),
which is fine for a demo but worth knowing if you want state to persist.

## Known limitation

See "Named limitation & what to harden next" in
[`ARCHITECTURE.md`](ARCHITECTURE.md).