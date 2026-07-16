# Dispatch — Customer Support Resolution Agent

An agent that reads incoming support tickets, classifies them into
`auto_resolve` / `draft_for_review` / `escalate`, resolves what it safely
can using tools (KB search, order lookup), and drafts replies for the rest —
with a hard, code-enforced human-approval gate on every refund. Every ticket
produces a structured, human-readable trace for evaluation.

See [`ARCHITECTURE.md`](ARCHITECTURE.md) for the one-page pipeline doc, and
[`eval/eval_report.md`](eval/eval_report.md) for the evaluation scorecard.

## Which model this actually uses

This project runs on **OpenRouter**, which can route to any model on their
platform. The agent itself, in every evaluation run in this repo
(`eval/eval_report.md`), is **`openai/gpt-4.1-mini`** — not Claude. A
separate, independent model, **`openai/gpt-4.1`**, is used only as an
evaluation judge (see below) — never to run the agent.

The code's *default* fallback (`OPENROUTER_MODEL` in `app/config.py`) is
still set to an Anthropic slug (`anthropic/claude-sonnet-4.5`) from an
earlier version of this project. **The actual model used for every number in
this repo's eval report is `openai/gpt-4.1-mini`**, set via `.env` /
environment variable, which overrides that default. If you re-run the eval
yourself without setting `OPENROUTER_MODEL` explicitly, you'll get Claude
instead — same architecture, different model, and the numbers will differ
from what's documented here. See `.env.example`.

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
cd Customer-Support-Resolution-Agent
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

### Option B — run against a real LLM via OpenRouter

1. Get a key at https://openrouter.ai/keys
2. Copy `.env.example` to `.env` and fill in:
   ```
   LLM_BACKEND=openrouter
   OPENROUTER_API_KEY=sk-or-...
   OPENROUTER_MODEL=openai/gpt-4.1-mini
   OPENROUTER_JUDGE_MODEL=openai/gpt-4.1
   ```
   (`OPENROUTER_MODEL` is the agent's model; `OPENROUTER_JUDGE_MODEL` is a
   separate, stronger model used only by the eval harness's LLM-as-judge
   pass, never to run the agent itself. Check
   https://openrouter.ai/models for current slugs/pricing for either.)
3. Export the vars (`source .env` with `set -a`/`set +a` on macOS/Linux, or
   `$env:VAR = "value"` per line on Windows PowerShell — see below), then:
   ```bash
   python3 cli.py demo
   ```

**Loading `.env` correctly (bash/zsh):**
```bash
set -a; source .env; set +a
```
**PowerShell:**
```powershell
Get-Content .env | ForEach-Object {
    if ($_ -match '^\s*([^#][^=]*)=(.*)$') {
        [System.Environment]::SetEnvironmentVariable($matches[1].Trim(), $matches[2].Trim(), "Process")
    }
}
```
Neither `uvicorn` nor `cli.py` read `.env` automatically — only
`eval_dashboard.py` does, via `python-dotenv`. You must export the
variables into your shell yourself for the CLI and web server.

## Running the web demo

Single-origin FastAPI app — one process serves both the API and the UI, so
there's no CORS setup.

```bash
uvicorn app.main:app --reload --port 8000
```

Open http://localhost:8000. There are two ways to run the seeded tickets,
independently of each other and of the server's `LLM_BACKEND` default:

- **"Run 3 seeded demo tickets — Mock"** — free, deterministic, no key
  needed regardless of server config.
- **"Run 3 seeded demo tickets — Real LLM"** — requires
  `OPENROUTER_API_KEY` to be set on the server process (see Option B above).

Each ticket card in the trace feed is tagged with which backend actually
ran it (⚙️ mock / 🧠 real LLM), so it's unambiguous which one produced a
given result on screen. The free-text ticket box has a matching dropdown to
send any custom ticket to either backend. Approve or deny refund requests
from the right-hand panel and watch the KPI strip and the ticket's own
trace card update.

This per-request backend choice is implemented by temporarily overriding
`config.LLM_BACKEND` for the duration of one request only
(`app/main.py::run_ticket`) — safe for one person clicking buttons in a demo,
not safe for concurrent multi-user traffic (see `ARCHITECTURE.md`).

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

# Also run an LLM-as-judge pass (blinded, GPT-4.1 by default) comparing
# mock vs real-LLM replies per ticket. Requires --compare. Costs one extra
# API call per ticket:
python3 eval/run_eval.py --compare --judge

# Also run the name-based governance/fairness check (12 tickets, matched
# complaints, different customer names). Meaningful only against openrouter:
python3 eval/run_eval.py --backend openrouter --fairness
```

This regenerates `eval/eval_report.json` / `.md` (the main scorecard, with
the three adversarial tickets called out explicitly) and, when the relevant
flags are used, `eval/fairness_report.json` / `.md` (the governance check,
written separately so it never overwrites the main scorecard).

The eval run never calls `approve()`, so it never mutates `data/orders.json`
— any ticket where an order ends up `refunded` after an eval run indicates a
gate failure, and the harness asserts against exactly that.

### Evaluation dashboard (Streamlit)

A visual frontend over the same `run_eval.py` harness — no separate scoring
logic, just a nicer view with live buttons instead of reading a markdown
file.

```bash
streamlit run eval_dashboard.py
```

Sidebar options: run a single backend or compare both; optionally also run
the LLM-as-judge pass (compare mode only) and/or the governance/fairness
check. Results include the scorecard, layer pass rates, judge verdicts,
fairness results, adversarial cases, and a per-ticket table, with JSON/MD
export buttons that include all of the above, not just the base scorecard.

**Keep this in a separate environment from your main app if you can.**
`streamlit` and `fastapi` both depend on `starlette`, and installing both
into the same environment has, in testing, sometimes pulled an
incompatible `starlette` version that breaks the FastAPI web demo (`Router
.__init__() got an unexpected keyword argument 'on_startup'`). It doesn't
always happen — pip's resolver sometimes picks a compatible version — but
don't rely on it. Safest setup:
```bash
python3 -m venv .venv-dashboard
source .venv-dashboard/bin/activate
pip install -r requirements.txt
streamlit run eval_dashboard.py
```
leaving your main `.venv` (used for `uvicorn`/`cli.py`) untouched.

## Project layout

```
app/
  config.py    # env-driven config (LLM_BACKEND, OPENROUTER_*, judge model, paths)
  llm.py       # OpenRouterClient (real) + MockLLMClient (free baseline) + judge_compare()
  tools.py     # search_kb, get_order_status, issue_refund + their schemas
  store.py     # TraceStore (per-ticket JSON traces) + ApprovalQueue (the gate's state)
  agent.py     # classify -> route -> tool-calling loop -> gate -> log
  main.py      # FastAPI app: API + serves static/index.html, per-request backend override
cli.py         # terminal runner (run / demo / approvals / approve / deny)
eval_dashboard.py  # Streamlit frontend over eval/run_eval.py
data/
  orders.json  # mock order DB (mutated only by an approved refund)
  kb.json      # mock knowledge base
eval/
  test_tickets.json      # 17 labeled tickets (main scorecard)
  fairness_tickets.json  # 12 tickets, 4 matched-name groups (governance check)
  run_eval.py             # scoring harness + judge + fairness -> eval_report.{json,md}, fairness_report.{json,md}
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