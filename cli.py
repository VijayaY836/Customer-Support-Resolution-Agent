#!/usr/bin/env python3
"""
CLI runner -- the terminal-native way to demo the agent without the web UI.

Usage:
    python cli.py demo                     # runs the 3 seeded demo tickets
    python cli.py run "Where's my order #4471?"
    python cli.py approvals                # list pending refund approvals
    python cli.py approve appr-xxxxxxxx
    python cli.py deny appr-xxxxxxxx --note "customer unverified"
"""
import argparse
import json
import sys
import uuid

from app import agent, llm
from app.store import ApprovalQueue, TraceStore

DEMO_TICKETS = [
    "Where's my order #4471?",
    "How do I reset my password?",
    "I want a refund, this is unacceptable. Order #4488.",
]

DIV = "-" * 70


def _print_trace(trace: dict):
    print(DIV)
    print(f"TICKET {trace['ticket_id']}: {trace['ticket_text']!r}")
    print(f"  route -> {trace['route']}  (confidence {trace['route_confidence']})")
    print(f"  why   -> {trace['route_reasoning']}")
    for step in trace["steps"]:
        if step["type"] == "classification":
            continue
        if step["type"] == "tool_call":
            print(f"  [tool] {step['tool']}({step['arguments']})")
            print(f"         -> {json.dumps(step['result'])[:160]}")
        elif step["type"] == "approval_requested":
            print(f"  [GATE] issue_refund({step['arguments']}) -> BLOCKED, pending human approval")
            print(f"         approval_id = {step['approval_id']}")
            print(f"         >>> run: python cli.py approve {step['approval_id']}   (or deny)")
    print(f"  FINAL OUTPUT:\n    {trace['final_output']}")
    print(DIV)


def cmd_run(args):
    client = llm.get_client()
    ticket_id = args.ticket_id or f"t-{uuid.uuid4().hex[:8]}"
    trace = agent.run_ticket(ticket_id, args.text, TraceStore(), ApprovalQueue(), client)
    _print_trace(trace)


def cmd_demo(args):
    client = llm.get_client()
    print(f"Running seeded demo tickets against LLM_BACKEND={llm.config.LLM_BACKEND!r} ...\n")
    for i, text in enumerate(DEMO_TICKETS, 1):
        trace = agent.run_ticket(f"demo-{i}", text, TraceStore(), ApprovalQueue(), client)
        _print_trace(trace)


def cmd_approvals(args):
    q = ApprovalQueue()
    pending = q.list_pending()
    if not pending:
        print("No pending approvals.")
        return
    for p in pending:
        print(DIV)
        print(f"approval_id: {p['approval_id']}")
        print(f"  ticket:  {p['ticket_id']}")
        print(f"  order:   {p['order_id']}")
        print(f"  amount:  {p['amount']}")
        print(f"  reason:  {p['reason']}")
    print(DIV)


def cmd_approve(args):
    q = ApprovalQueue()
    entry = q.approve(args.approval_id, args.note or "")
    print(json.dumps(entry, indent=2))


def cmd_deny(args):
    q = ApprovalQueue()
    entry = q.deny(args.approval_id, args.note or "")
    print(json.dumps(entry, indent=2))


def main():
    parser = argparse.ArgumentParser(description="Customer Support Resolution Agent CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="Run a single ticket")
    p_run.add_argument("text", help="The ticket text")
    p_run.add_argument("--ticket-id", default=None)
    p_run.set_defaults(func=cmd_run)

    p_demo = sub.add_parser("demo", help="Run the 3 seeded demo tickets")
    p_demo.set_defaults(func=cmd_demo)

    p_appr = sub.add_parser("approvals", help="List pending refund approvals")
    p_appr.set_defaults(func=cmd_approvals)

    p_ok = sub.add_parser("approve", help="Approve a pending refund")
    p_ok.add_argument("approval_id")
    p_ok.add_argument("--note", default=None)
    p_ok.set_defaults(func=cmd_approve)

    p_no = sub.add_parser("deny", help="Deny a pending refund")
    p_no.add_argument("approval_id")
    p_no.add_argument("--note", default=None)
    p_no.set_defaults(func=cmd_deny)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    sys.exit(main())