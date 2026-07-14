"""
Single-origin FastAPI app: serves the demo UI (static/) and the JSON API it
talks to, on one port, so there's no CORS to configure.

Run with:  uvicorn app.main:app --reload --port 8000
"""
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import agent, config, llm
from .store import ApprovalQueue, TraceStore

app = FastAPI(title="Customer Support Resolution Agent")

trace_store = TraceStore()
approval_queue = ApprovalQueue()

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


class RunTicketRequest(BaseModel):
    ticket_text: str
    ticket_id: str | None = None


class ResolveApprovalRequest(BaseModel):
    note: str | None = None


DEMO_TICKETS = [
    {"ticket_id": "demo-1", "ticket_text": "Where's my order #4471?"},
    {"ticket_id": "demo-2", "ticket_text": "How do I reset my password?"},
    {"ticket_id": "demo-3", "ticket_text": "I want a refund, this is unacceptable. Order #4488."},
]


@app.get("/")
def index():
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.get("/api/demo-tickets")
def get_demo_tickets():
    return DEMO_TICKETS


@app.post("/api/tickets/run")
def run_ticket(req: RunTicketRequest):
    ticket_id = req.ticket_id or f"t-{uuid.uuid4().hex[:8]}"
    try:
        client = llm.get_client()
        trace = agent.run_ticket(ticket_id, req.ticket_text, trace_store, approval_queue, client)
    except llm.LLMError as e:
        raise HTTPException(status_code=502, detail=str(e))
    return trace


@app.get("/api/tickets")
def list_tickets():
    return trace_store.list_all()


@app.get("/api/tickets/{ticket_id}")
def get_ticket(ticket_id: str):
    try:
        return trace_store.load(ticket_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="No such ticket trace")


@app.get("/api/approvals")
def list_approvals():
    return approval_queue.list_all()


@app.post("/api/approvals/{approval_id}/approve")
def approve(approval_id: str, req: ResolveApprovalRequest):
    try:
        return approval_queue.approve(approval_id, req.note or "")
    except KeyError:
        raise HTTPException(status_code=404, detail="No such approval request")


@app.post("/api/approvals/{approval_id}/deny")
def deny(approval_id: str, req: ResolveApprovalRequest):
    try:
        return approval_queue.deny(approval_id, req.note or "")
    except KeyError:
        raise HTTPException(status_code=404, detail="No such approval request")


@app.get("/api/kpi")
def kpi():
    traces = trace_store.list_all()
    total = len(traces)
    by_route = {"auto_resolve": 0, "draft_for_review": 0, "escalate": 0}
    for t in traces:
        if t.get("route") in by_route:
            by_route[t["route"]] += 1
    pct_auto = (by_route["auto_resolve"] / total * 100) if total else 0.0
    return {
        "total_tickets": total,
        "by_route": by_route,
        "pct_auto_resolved": round(pct_auto, 1),
        "llm_backend": config.LLM_BACKEND,
        "model": config.OPENROUTER_MODEL if config.LLM_BACKEND == "openrouter" else "mock (naive baseline)",
    }


@app.get("/api/config")
def get_config():
    return {"llm_backend": config.LLM_BACKEND, "model": config.OPENROUTER_MODEL}