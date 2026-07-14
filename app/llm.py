"""
Two interchangeable LLM backends behind one interface:

    chat(messages, tools=None, mode="resolve") -> {
        "content": str | None,
        "tool_calls": [{"id": str, "name": str, "arguments": dict}],
    }

`OpenRouterClient` calls the real Claude model through OpenRouter's
OpenAI-compatible /chat/completions endpoint.

`MockLLMClient` is a small deterministic, keyword-driven stand-in that needs
no API key and no network. It exists for two reasons:
  1. So the whole pipeline (classification, tool loop, gate, logging, UI,
     eval harness) can be developed and demoed offline for free.
  2. It doubles as the "naive baseline" in the evaluation report -- the
     before/after comparison is naive-keyword-agent vs real-Claude-agent.

Select via LLM_BACKEND=mock|openrouter in the environment (see config.py).
"""
import json
import re

import requests

from . import config


class LLMError(RuntimeError):
    pass


def _extract_json(text: str) -> dict:
    """Best-effort JSON extraction from a model response that should be pure
    JSON but might come wrapped in prose or a ```json fence."""
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    else:
        brace = re.search(r"\{.*\}", text, re.DOTALL)
        if brace:
            text = brace.group(0)
    return json.loads(text)


class OpenRouterClient:
    def __init__(self):
        if not config.OPENROUTER_API_KEY:
            raise LLMError(
                "OPENROUTER_API_KEY is not set. Export it or put it in a .env file "
                "(see .env.example), or set LLM_BACKEND=mock to run without one."
            )

    def chat(self, messages, tools=None, mode="resolve") -> dict:
        payload = {
            "model": config.OPENROUTER_MODEL,
            "messages": messages,
            "temperature": 0,
            "max_tokens": config.MAX_TOKENS_CLASSIFY if mode == "classify" else config.MAX_TOKENS_RESOLVE,
        }
        if tools:
            payload["tools"] = tools

        headers = {
            "Authorization": f"Bearer {config.OPENROUTER_API_KEY}",
            "Content-Type": "application/json",
            "HTTP-Referer": config.OPENROUTER_SITE_URL,
            "X-Title": config.OPENROUTER_APP_NAME,
        }

        resp = requests.post(
            f"{config.OPENROUTER_BASE_URL}/chat/completions",
            headers=headers,
            json=payload,
            timeout=60,
        )
        if resp.status_code != 200:
            raise LLMError(f"OpenRouter error {resp.status_code}: {resp.text[:500]}")

        data = resp.json()
        try:
            message = data["choices"][0]["message"]
        except (KeyError, IndexError) as e:
            raise LLMError(f"Unexpected OpenRouter response shape: {data}") from e

        tool_calls = []
        for tc in message.get("tool_calls") or []:
            try:
                args = json.loads(tc["function"]["arguments"])
            except (json.JSONDecodeError, KeyError):
                args = {}
            tool_calls.append({"id": tc.get("id", ""), "name": tc["function"]["name"], "arguments": args})

        return {"content": message.get("content"), "tool_calls": tool_calls}


class MockLLMClient:
    """Deterministic, free, offline. See module docstring."""

    ESCALATE_WORDS = [
        "sue", "lawsuit", "lawyer", "legal action", "consumer court",
        "unauthorized", "fraud", "hacked", "hack", "data breach",
        "disgusting", "scam", "report you", "worst company", "furious",
    ]
    REFUND_WORDS = ["refund", "money back", "reimburse", "compensat"]
    STATUS_WORDS = ["where is my order", "order status", "tracking", "shipped", "eta", "arrive"]
    PASSWORD_WORDS = ["password", "reset", "login", "can't sign in", "locked out"]
    INJECTION_WORDS = ["as the admin", "as an administrator", "override", "system:", "ignore previous", "already approved by", "manager already approved"]

    def chat(self, messages, tools=None, mode="resolve") -> dict:
        if mode == "classify":
            return self._classify(messages)
        return self._resolve(messages, tools or [])

    # -- classification --------------------------------------------------
    def _ticket_text(self, messages) -> str:
        for m in messages:
            if m["role"] == "user":
                return m["content"]
        return ""

    def _classify(self, messages) -> dict:
        text = self._ticket_text(messages).lower()

        if any(w in text for w in self.ESCALATE_WORDS):
            route, conf, why = "escalate", 0.9, "Message contains legal/fraud/anger keywords."
        elif any(w in text for w in self.INJECTION_WORDS):
            # naive baseline: doesn't reliably catch injection framing, often
            # under-escalates -- this is deliberately the weak point the real
            # model should improve on.
            route, conf, why = "draft_for_review", 0.5, "Refund-adjacent request with unclear authorization."
        elif any(w in text for w in self.REFUND_WORDS):
            route, conf, why = "draft_for_review", 0.6, "Refund request needs policy check and human sign-off."
        elif any(w in text for w in self.STATUS_WORDS) or re.search(r"#?\d{4}\b", text):
            route, conf, why = "auto_resolve", 0.85, "Looks like a routine order-status lookup."
        elif any(w in text for w in self.PASSWORD_WORDS):
            route, conf, why = "auto_resolve", 0.9, "Standard password reset, fully covered by KB."
        else:
            route, conf, why = "draft_for_review", 0.4, "No strong keyword signal; low-confidence fallback."

        return {"content": json.dumps({"route": route, "confidence": conf, "reasoning": why}), "tool_calls": []}

    # -- resolution / tool loop ------------------------------------------
    def _resolve(self, messages, tools) -> dict:
        text = self._ticket_text(messages).lower()

        if not tools:
            # No tools were offered (e.g. escalate route) -- a real model
            # literally cannot emit a tool_call in this case, so the mock
            # must not either. Go straight to a handoff-style draft.
            tool_msgs = [m for m in messages if m["role"] == "tool"]
            return {"content": self._draft_reply(text, tool_msgs), "tool_calls": []}

        tool_msgs = [m for m in messages if m["role"] == "tool"]
        called_names = [m.get("name") for m in tool_msgs]

        order_match = re.search(r"#?(\d{4})\b", text)
        order_id = order_match.group(1) if order_match else None

        if order_id and "get_order_status" not in called_names:
            return self._tool_call("get_order_status", {"order_id": order_id})

        if any(w in text for w in self.PASSWORD_WORDS + self.REFUND_WORDS + self.STATUS_WORDS) and "search_kb" not in called_names:
            query = "refund policy" if any(w in text for w in self.REFUND_WORDS) else (
                "password reset" if any(w in text for w in self.PASSWORD_WORDS) else "shipping delivery"
            )
            return self._tool_call("search_kb", {"query": query})

        if any(w in text for w in self.REFUND_WORDS) and "issue_refund" not in called_names:
            amount = 0.0
            for m in tool_msgs:
                if m.get("name") == "get_order_status":
                    try:
                        amount = json.loads(m["content"]).get("amount_paid", 0.0)
                    except Exception:
                        pass
            return self._tool_call("issue_refund", {
                "order_id": order_id or "unknown",
                "amount": amount,
                "reason": "Customer requested refund: " + text[:120],
            })

        # nothing left to do -- draft a final reply from whatever we gathered
        return {"content": self._draft_reply(text, tool_msgs), "tool_calls": []}

    def _tool_call(self, name, args) -> dict:
        return {"content": None, "tool_calls": [{"id": f"mock-{name}", "name": name, "arguments": args}]}

    def _draft_reply(self, text, tool_msgs) -> str:
        kb_snippets, order_info = [], None
        for m in tool_msgs:
            try:
                payload = json.loads(m["content"])
            except Exception:
                continue
            if m.get("name") == "search_kb":
                kb_snippets = [r["content"] for r in payload.get("results", [])]
            if m.get("name") == "get_order_status" and payload.get("found"):
                order_info = payload

        lines = ["Hi there, thanks for reaching out."]
        if order_info:
            lines.append(
                f"Your order #{order_info['order_id']} is currently '{order_info['status']}'"
                + (f", tracking number {order_info.get('tracking_number')}." if order_info.get("tracking_number") else ".")
            )
        if kb_snippets:
            lines.append(kb_snippets[0])
        if any(w in text for w in self.REFUND_WORDS):
            lines.append("I've logged your refund request -- it's pending review by a human agent before anything is processed.")
        lines.append("Let us know if you need anything else.")
        return " ".join(lines)


def get_client():
    if config.LLM_BACKEND == "openrouter":
        return OpenRouterClient()
    return MockLLMClient()