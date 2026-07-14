"""
Two pieces of state live here:

1. TraceStore -- one JSON file per ticket under traces/, capturing the full
   trajectory (route, reasoning, every tool call + result, final output).
   This is what the evaluation harness reads.

2. ApprovalQueue -- the single choke point every `issue_refund` call must
   pass through. A pending entry sits here until a human calls approve() or
   deny(). Only approve() is allowed to touch tools._execute_refund_unsafe.
"""
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import config, tools


def _now():
    return datetime.now(timezone.utc).isoformat()


class TraceStore:
    def __init__(self, traces_dir: Path = config.TRACES_DIR):
        self.traces_dir = traces_dir
        self.traces_dir.mkdir(parents=True, exist_ok=True)

    def new_trace(self, ticket_id: str, ticket_text: str) -> dict:
        trace = {
            "ticket_id": ticket_id,
            "ticket_text": ticket_text,
            "started_at": _now(),
            "finished_at": None,
            "route": None,
            "route_confidence": None,
            "route_reasoning": None,
            "steps": [],       # tool calls, in order
            "approval_events": [],
            "final_output": None,
            "llm_backend": config.LLM_BACKEND,
        }
        self.save(trace)
        return trace

    def add_step(self, trace: dict, step_type: str, **fields):
        trace["steps"].append({"type": step_type, "at": _now(), **fields})
        self.save(trace)

    def finish(self, trace: dict, final_output: str):
        trace["final_output"] = final_output
        trace["finished_at"] = _now()
        self.save(trace)

    def save(self, trace: dict):
        path = self.traces_dir / f"{trace['ticket_id']}.json"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(trace, f, indent=2)

    def load(self, ticket_id: str) -> dict:
        path = self.traces_dir / f"{ticket_id}.json"
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def list_all(self):
        out = []
        for p in sorted(self.traces_dir.glob("*.json")):
            if p.name.startswith("_"):
                continue
            with open(p, "r", encoding="utf-8") as f:
                out.append(json.load(f))
        return out


class ApprovalQueue:
    """
    Persisted to a single JSON file so pending approvals survive a server
    restart mid-demo. Every entry is one requested `issue_refund` call.
    """

    def __init__(self, path: Path = config.APPROVALS_PATH):
        self.path = path
        if not self.path.exists():
            self._write({})

    def _read(self) -> dict:
        with open(self.path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _write(self, data: dict):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

    def create(self, ticket_id: str, order_id: str, amount: float, reason: str) -> dict:
        data = self._read()
        approval_id = f"appr-{uuid.uuid4().hex[:8]}"
        entry = {
            "approval_id": approval_id,
            "ticket_id": ticket_id,
            "order_id": order_id,
            "amount": amount,
            "reason": reason,
            "status": "pending",  # pending | approved | denied
            "created_at": _now(),
            "resolved_at": None,
            "resolution_note": None,
        }
        data[approval_id] = entry
        self._write(data)
        return entry

    def get(self, approval_id: str) -> dict | None:
        return self._read().get(approval_id)

    def list_pending(self):
        return [v for v in self._read().values() if v["status"] == "pending"]

    def list_all(self):
        return list(self._read().values())

    def approve(self, approval_id: str, note: str = "") -> dict:
        data = self._read()
        entry = data.get(approval_id)
        if not entry:
            raise KeyError(f"No approval request {approval_id}")
        if entry["status"] != "pending":
            return entry

        result = tools._execute_refund_unsafe(entry["order_id"], entry["amount"], entry["reason"])
        entry["status"] = "approved" if result.get("success") else "failed"
        entry["resolved_at"] = _now()
        entry["resolution_note"] = note or ("Approved by human reviewer." if result.get("success") else result.get("error"))
        entry["execution_result"] = result
        data[approval_id] = entry
        self._write(data)
        return entry

    def deny(self, approval_id: str, note: str = "") -> dict:
        data = self._read()
        entry = data.get(approval_id)
        if not entry:
            raise KeyError(f"No approval request {approval_id}")
        if entry["status"] != "pending":
            return entry

        entry["status"] = "denied"
        entry["resolved_at"] = _now()
        entry["resolution_note"] = note or "Denied by human reviewer."
        data[approval_id] = entry
        self._write(data)
        return entry