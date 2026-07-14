"""
The three tools the agent can call, plus the JSON schemas describing them to
the model (OpenAI-compatible `tools` format, which is what OpenRouter expects
regardless of which underlying model you route to).

IMPORTANT: `issue_refund` is defined here as a *schema* the model can call,
but the function that actually mutates order state (`_execute_refund_unsafe`)
is never invoked directly from the tool-call loop. It is only ever invoked by
the approval queue in store.py, after a human has approved the request. See
agent.py's `dispatch_tool_call` for where that gate is enforced -- in code,
not in a prompt, so it holds no matter what the model decides to do.
"""
import json
from datetime import datetime, timezone

from . import config

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "search_kb",
            "description": (
                "Search the internal knowledge base for policy docs and FAQs "
                "(refund policy, shipping policy, password reset steps, etc). "
                "Always search the KB before stating a policy as fact."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Free-text search query, e.g. 'refund policy damaged item'",
                    }
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_order_status",
            "description": "Look up an order's shipping/delivery status, items, and amount paid by order ID.",
            "parameters": {
                "type": "object",
                "properties": {
                    "order_id": {
                        "type": "string",
                        "description": "The numeric order ID, e.g. '4471'",
                    }
                },
                "required": ["order_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "issue_refund",
            "description": (
                "Request a refund be issued for an order. This NEVER executes immediately -- "
                "every call is routed to a human approval queue and blocked until a human "
                "explicitly approves it. Call this to *request* a refund, not to confirm one "
                "has happened. Never tell a customer a refund is complete unless a tool result "
                "explicitly says it was approved and processed."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "order_id": {"type": "string", "description": "The order ID to refund"},
                    "amount": {"type": "number", "description": "Amount to refund, in INR"},
                    "reason": {"type": "string", "description": "Why this refund is being requested"},
                },
                "required": ["order_id", "amount", "reason"],
            },
        },
    },
]

TOOL_NAMES = {schema["function"]["name"] for schema in TOOL_SCHEMAS}


def _load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _now():
    return datetime.now(timezone.utc).isoformat()


def search_kb(query: str) -> dict:
    """Naive keyword-overlap search over the mock KB. Returns top matches."""
    articles = _load_json(config.KB_PATH)
    q_words = {w.strip(".,?!'\"").lower() for w in query.split()}

    scored = []
    for a in articles:
        kw_hits = sum(1 for kw in a["keywords"] if kw in query.lower())
        word_hits = len(q_words.intersection({w.lower() for w in a["title"].split()}))
        score = kw_hits * 2 + word_hits
        if score > 0:
            scored.append((score, a))

    scored.sort(key=lambda x: -x[0])
    top = [a for _, a in scored[:2]]

    if not top:
        return {"query": query, "results": [], "note": "No matching KB articles found."}

    return {
        "query": query,
        "results": [{"id": a["id"], "title": a["title"], "content": a["content"]} for a in top],
    }


def get_order_status(order_id: str) -> dict:
    orders = _load_json(config.ORDERS_PATH)
    order_id = str(order_id).strip().lstrip("#")
    order = orders.get(order_id)
    if not order:
        return {"order_id": order_id, "found": False, "error": "No order with this ID exists."}
    return {"found": True, **order}


def _execute_refund_unsafe(order_id: str, amount: float, reason: str) -> dict:
    """
    Actually mutate mock order state to reflect an issued refund.

    Named `_unsafe` and prefixed with an underscore as a loud signal: this
    function must ONLY ever be called from the approval queue's `approve()`
    path (store.py), never directly from the tool-call loop. It performs no
    permission check of its own -- the gate lives one layer up, in the
    dispatcher, on purpose (single point of enforcement, easy to audit).
    """
    orders = _load_json(config.ORDERS_PATH)
    order_id = str(order_id).strip().lstrip("#")
    order = orders.get(order_id)
    if not order:
        return {"success": False, "error": f"Order {order_id} not found."}

    order["status"] = "refunded"
    order["refund_amount"] = amount
    order["refund_reason"] = reason
    order["refunded_at"] = _now()

    with open(config.ORDERS_PATH, "w", encoding="utf-8") as f:
        json.dump(orders, f, indent=2)

    return {"success": True, "order_id": order_id, "amount": amount, "refunded_at": order["refunded_at"]}