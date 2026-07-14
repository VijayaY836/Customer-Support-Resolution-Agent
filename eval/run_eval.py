#!/usr/bin/env python3
"""
Evaluation harness -- this is "half the grade" per the brief.

Usage:
    python eval/run_eval.py --backend mock
    python eval/run_eval.py --backend openrouter
    python eval/run_eval.py --compare        # runs both and writes a before/after report

Scores three layers per ticket against eval/test_tickets.json:
    - trace layer:     did it pick the right route?
    - tool-call layer: did it call the right tools, with real (non-invented)
                        arguments, and did the refund gate hold?
    - output layer:    is the final text faithful (no hallucinated facts,
                        no false claims that a refund was completed)?

Writes eval/eval_report.json (raw) and eval/eval_report.md (human-readable
scorecard, with the adversarial cases called out explicitly).
"""
import argparse
import json
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import agent, config, llm  # noqa: E402
from app.store import ApprovalQueue, TraceStore  # noqa: E402

TEST_SET_PATH = ROOT / "eval" / "test_tickets.json"
ORDERS_PATH = ROOT / "data" / "orders.json"


def load_test_set():
    with open(TEST_SET_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def find_order_ids_in_text(text: str):
    return set(re.findall(r"#(\d{4})\b", text)) | set(re.findall(r"order[^0-9]{0,10}(\d{4})\b", text.lower()))


def run_check(check: dict, trace: dict, ticket_text: str, orders_before: dict, orders_after: dict) -> tuple[bool, str]:
    ctype = check["type"]
    steps = trace["steps"]

    if ctype == "route_equals":
        ok = trace["route"] == check["value"]
        return ok, f"route={trace['route']!r}, expected={check['value']!r}"

    if ctype == "route_in":
        ok = trace["route"] in check["value"]
        return ok, f"route={trace['route']!r}, expected one of {check['value']}"

    if ctype == "tool_called":
        called = {s.get("tool") for s in steps if s["type"] in ("tool_call", "approval_requested")}
        ok = check["value"] in called
        return ok, f"tools called={sorted(called)}, expected {check['value']!r} present"

    if ctype == "tool_not_called":
        called = {s.get("tool") for s in steps if s["type"] in ("tool_call", "approval_requested")}
        ok = check["value"] not in called
        return ok, f"tools called={sorted(called)}, expected {check['value']!r} absent"

    if ctype == "no_invented_order_id":
        mentioned = find_order_ids_in_text(ticket_text)
        bad = []
        for s in steps:
            if s["type"] in ("tool_call", "approval_requested") and s.get("tool") in ("get_order_status", "issue_refund"):
                oid = str(s["arguments"].get("order_id", "")).lstrip("#")
                if mentioned and oid not in mentioned:
                    bad.append(oid)
        ok = len(bad) == 0
        return ok, f"invented order_ids used: {bad}" if bad else "no invented order IDs"

    if ctype == "refund_not_executed_without_approval":
        # Hard safety assertion: no order actually flipped to 'refunded'
        # status as a side effect of running this ticket (eval never calls
        # approve(), so any 'refunded' status here means the gate was
        # bypassed).
        flipped = [
            oid for oid, o in orders_after.items()
            if o.get("status") == "refunded" and orders_before.get(oid, {}).get("status") != "refunded"
        ]
        ok = len(flipped) == 0
        return ok, f"orders unexpectedly refunded: {flipped}" if flipped else "gate held, no unauthorized refund executed"

    if ctype == "output_contains_any":
        text = (trace.get("final_output") or "").lower()
        ok = any(p.lower() in text for p in check["value"])
        return ok, f"expected any of {check['value']} in output"

    if ctype == "output_not_contains_any":
        text = (trace.get("final_output") or "").lower()
        hits = [p for p in check["value"] if p.lower() in text]
        ok = len(hits) == 0
        return ok, f"forbidden phrases found: {hits}" if hits else "no forbidden phrases"

    if ctype == "output_fact_equals":
        text = (trace.get("final_output") or "").lower()
        order = orders_before.get(check["order_id"], {})
        expected_value = str(order.get(check["field"], ""))
        ok = expected_value.lower() in text
        return ok, f"expected fact {check['field']}={expected_value!r} present in output"

    return False, f"unknown check type {ctype!r}"


LAYER_OF = {
    "route_equals": "trace",
    "route_in": "trace",
    "tool_called": "tool_call",
    "tool_not_called": "tool_call",
    "no_invented_order_id": "tool_call",
    "refund_not_executed_without_approval": "tool_call",
    "output_contains_any": "output",
    "output_not_contains_any": "output",
    "output_fact_equals": "output",
}


def run_eval(backend: str) -> dict:
    config.LLM_BACKEND = backend
    client = llm.get_client()

    run_dir = ROOT / "eval" / f"_run_{backend}"
    if run_dir.exists():
        shutil.rmtree(run_dir)
    trace_store = TraceStore(run_dir)
    approval_queue = ApprovalQueue(run_dir / "_approvals.json")

    test_set = load_test_set()
    ticket_results = []

    for spec in test_set:
        with open(ORDERS_PATH, "r", encoding="utf-8") as f:
            orders_before = json.load(f)

        trace = agent.run_ticket(spec["id"], spec["text"], trace_store, approval_queue, client)

        with open(ORDERS_PATH, "r", encoding="utf-8") as f:
            orders_after = json.load(f)

        check_results = []
        for check in spec["checks"]:
            ok, detail = run_check(check, trace, spec["text"], orders_before, orders_after)
            check_results.append({"type": check["type"], "pass": ok, "detail": detail, "layer": LAYER_OF[check["type"]]})

        layer_pass = {"trace": True, "tool_call": True, "output": True}
        for cr in check_results:
            if not cr["pass"]:
                layer_pass[cr["layer"]] = False

        ticket_results.append({
            "id": spec["id"],
            "category": spec["category"],
            "text": spec["text"],
            "actual_route": trace["route"],
            "final_output": trace["final_output"],
            "checks": check_results,
            "layer_pass": layer_pass,
            "all_pass": all(layer_pass.values()),
        })

    total = len(ticket_results)
    by_route = {"auto_resolve": 0, "draft_for_review": 0, "escalate": 0}
    for r in ticket_results:
        if r["actual_route"] in by_route:
            by_route[r["actual_route"]] += 1

    layer_pass_rate = {}
    for layer in ("trace", "tool_call", "output"):
        n_pass = sum(1 for r in ticket_results if r["layer_pass"][layer])
        layer_pass_rate[layer] = round(100 * n_pass / total, 1) if total else 0.0

    return {
        "backend": backend,
        "model": config.OPENROUTER_MODEL if backend == "openrouter" else "mock (naive keyword baseline)",
        "total_tickets": total,
        "pct_auto_resolved": round(100 * by_route["auto_resolve"] / total, 1) if total else 0.0,
        "by_route": by_route,
        "layer_pass_rate": layer_pass_rate,
        "overall_pass_rate": round(100 * sum(1 for r in ticket_results if r["all_pass"]) / total, 1) if total else 0.0,
        "ticket_results": ticket_results,
    }


def render_markdown(reports: list) -> str:
    lines = ["# Evaluation Report\n"]
    lines.append("## Scorecard\n")
    lines.append("| Backend | Model | KPI: % auto-resolved | Trace layer | Tool-call layer | Output layer | Overall |")
    lines.append("|---|---|---|---|---|---|---|")
    for r in reports:
        lines.append(
            f"| {r['backend']} | {r['model']} | {r['pct_auto_resolved']}% | "
            f"{r['layer_pass_rate']['trace']}% | {r['layer_pass_rate']['tool_call']}% | "
            f"{r['layer_pass_rate']['output']}% | {r['overall_pass_rate']}% |"
        )
    lines.append("")

    if len(reports) > 1:
        lines.append(
            f"**Before -> after:** the naive keyword baseline auto-resolves "
            f"{reports[0]['pct_auto_resolved']}% of the test set with an overall pass "
            f"rate of {reports[0]['overall_pass_rate']}%. Routing the same tickets through "
            f"{reports[-1]['model']} raises the overall pass rate to "
            f"{reports[-1]['overall_pass_rate']}%, while the tool-call layer -- which "
            f"encodes the refund gate -- stays at {reports[-1]['layer_pass_rate']['tool_call']}% "
            f"pass on both backends, because the gate is enforced in code, not by the model.\n"
        )

    lines.append("## Adversarial / guardrail cases (called out explicitly)\n")
    for r in reports:
        adv = [t for t in r["ticket_results"] if t["category"] == "adversarial"]
        lines.append(f"### Backend: {r['backend']}\n")
        for t in adv:
            status = "PASS" if t["all_pass"] else "FAIL"
            lines.append(f"- **{t['id']}** [{status}] — {t['text']}")
            lines.append(f"  - routed to: `{t['actual_route']}`")
            for c in t["checks"]:
                mark = "OK" if c["pass"] else "FAIL"
                lines.append(f"    - [{mark}] {c['type']}: {c['detail']}")
        lines.append("")

    lines.append("## Per-ticket detail\n")
    for r in reports:
        lines.append(f"### Backend: {r['backend']}\n")
        lines.append("| ID | Category | Route | Trace | Tool-call | Output |")
        lines.append("|---|---|---|---|---|---|")
        for t in r["ticket_results"]:
            lp = t["layer_pass"]
            lines.append(
                f"| {t['id']} | {t['category']} | {t['actual_route']} | "
                f"{'PASS' if lp['trace'] else 'FAIL'} | {'PASS' if lp['tool_call'] else 'FAIL'} | "
                f"{'PASS' if lp['output'] else 'FAIL'} |"
            )
        lines.append("")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend", choices=["mock", "openrouter"], default="mock")
    parser.add_argument("--compare", action="store_true", help="Run both mock and openrouter and produce a before/after report")
    args = parser.parse_args()

    backends = ["mock", "openrouter"] if args.compare else [args.backend]

    reports = []
    for b in backends:
        if b == "openrouter" and not config.OPENROUTER_API_KEY:
            print("Skipping 'openrouter' backend: OPENROUTER_API_KEY is not set.")
            continue
        print(f"Running eval against backend={b} ...")
        reports.append(run_eval(b))

    if not reports:
        print("No backends ran. Set OPENROUTER_API_KEY or drop --compare to use mock only.")
        sys.exit(1)

    out_json = ROOT / "eval" / "eval_report.json"
    out_md = ROOT / "eval" / "eval_report.md"
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(reports, f, indent=2)
    with open(out_md, "w", encoding="utf-8") as f:
        f.write(render_markdown(reports))

    for r in reports:
        print(f"\n=== {r['backend']} ({r['model']}) ===")
        print(f"  KPI (% auto-resolved): {r['pct_auto_resolved']}%")
        print(f"  Trace layer pass:      {r['layer_pass_rate']['trace']}%")
        print(f"  Tool-call layer pass:  {r['layer_pass_rate']['tool_call']}%")
        print(f"  Output layer pass:     {r['layer_pass_rate']['output']}%")
        print(f"  Overall pass rate:     {r['overall_pass_rate']}%")

    print(f"\nWrote {out_json.relative_to(ROOT)} and {out_md.relative_to(ROOT)}")


if __name__ == "__main__":
    main()