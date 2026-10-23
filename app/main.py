"""FastAPI server: REST + SSE streaming + static frontend."""
from __future__ import annotations

import json
import logging
import os
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, HTTPException, Request  # noqa: E402
from fastapi.responses import FileResponse, StreamingResponse  # noqa: E402
from langchain_core.messages import AIMessage, HumanMessage  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

from .graph import HISTORY_TURNS, build_graph  # noqa: E402
from .schema_utils import load_schema  # noqa: E402
from .validator import validate_sql  # noqa: E402

log = logging.getLogger("sql-agent")
ROOT = Path(__file__).resolve().parent.parent
DB_PATH = os.getenv("DB_PATH", str(ROOT / "db" / "sample.db"))
RATE_LIMIT = int(os.getenv("RATE_LIMIT_PER_HOUR", "60"))  # protects your LLM bill on a public URL
GENERIC_ERROR = "Something went wrong while processing your request. Please try again."
QUOTA_ERROR = "The AI model is rate-limited right now (free-tier quota). Please wait a few seconds and try again."


def _error_message(exc: Exception) -> str:
    t = f"{type(exc).__name__} {exc}".lower()
    return QUOTA_ERROR if any(k in t for k in ("429", "resource_exhausted", "quota", "rate limit")) else GENERIC_ERROR

app = FastAPI(title="SQL Query AI Agent")
_graph = None
_hits: dict[str, deque] = defaultdict(deque)


def get_graph():
    global _graph
    if _graph is None:
        _graph = build_graph(DB_PATH)
    return _graph


def set_graph(graph) -> None:  # used by tests
    global _graph
    _graph = graph


def check_rate(request: Request) -> None:
    ip = (request.headers.get("x-forwarded-for") or (request.client.host if request.client else "?"))
    ip = ip.split(",")[0].strip()
    now, q = time.time(), _hits[ip]
    while q and now - q[0] > 3600:
        q.popleft()
    if len(q) >= RATE_LIMIT:
        raise HTTPException(429, "Rate limit reached. Please try again later.")
    q.append(now)


class HistoryItem(BaseModel):
    role: Literal["user", "assistant"]
    text: str = Field(max_length=4000)
    sql: str = Field(default="", max_length=4000)


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    thread_id: str = Field(pattern=r"^[A-Za-z0-9_-]{8,64}$")
    dialect: Literal["sqlite", "postgresql", "mysql"] = "sqlite"
    # The browser keeps the chat transcript. If the server restarted (memory is in-process) it replays the
    # last few turns so follow-ups like "only those from California" still work.
    history: list[HistoryItem] = Field(default_factory=list, max_length=12)


def _config(req: ChatRequest) -> dict:
    return {"configurable": {"thread_id": req.thread_id}}


def _graph_input(graph, req: ChatRequest) -> dict:
    inp: dict = {"messages": [HumanMessage(req.message)], "dialect": req.dialect}
    known = graph.get_state(_config(req))
    if (known is None or not known.values) and req.history:
        msgs, last_sql = [], ""
        for h in req.history[-HISTORY_TURNS:]:
            if h.role == "user":
                msgs.append(HumanMessage(h.text))
            else:
                msgs.append(AIMessage(h.text + (f"\n\n```sql\n{h.sql}\n```" if h.sql else "")))
                last_sql = h.sql or last_sql
        # replayed SQL is client-supplied: only trust it if it passes the same validator as our own output
        if last_sql and validate_sql(last_sql, load_schema(DB_PATH), req.dialect).ok:
            inp["last_sql"] = last_sql
        inp["messages"] = msgs + inp["messages"]
    return inp


@app.get("/api/health")
def health():
    return {"status": "ok", "llm_configured": bool(os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")
                                                       or os.getenv("ANTHROPIC_API_KEY") or os.getenv("OPENAI_API_KEY"))}


@app.get("/api/schema")
def schema():
    s = load_schema(DB_PATH)
    return {t: {"columns": [{"name": c, "type": ty} for c, ty in m["columns"].items()],
                "pk": m["pk"], "fks": [{"column": c, "ref_table": rt, "ref_column": rc}
                                       for c, rt, rc in m["fks"]]} for t, m in s.items()}


@app.post("/api/chat")
def chat(req: ChatRequest, request: Request):
    check_rate(request)
    try:
        graph = get_graph()
        out = graph.invoke(_graph_input(graph, req), _config(req))
        return out["response"]
    except Exception as e:
        log.exception("chat failed")
        return {"type": "error", "message": _error_message(e)}


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


@app.post("/api/chat/stream")
def chat_stream(req: ChatRequest, request: Request):
    """Server-sent events: `step` per graph node, `partial` as soon as SQL / cost / results are ready,
    `token` per streamed explanation token, then `final`."""
    check_rate(request)
    graph = get_graph()

    def gen():
        final = None
        try:
            for mode, chunk in graph.stream(_graph_input(graph, req), _config(req),
                                            stream_mode=["updates", "custom"]):
                if mode == "custom":
                    if isinstance(chunk, dict) and "token" in chunk:
                        yield _sse("token", {"t": chunk["token"]})
                    elif isinstance(chunk, dict) and "partial" in chunk:
                        yield _sse("partial", chunk["partial"])
                    continue
                for node, update in chunk.items():
                    yield _sse("step", {"node": node})
                    if update and "response" in update:
                        final = update["response"]
            yield _sse("final", final or {"type": "error", "message": GENERIC_ERROR})
        except Exception as e:
            log.exception("stream failed")
            yield _sse("final", {"type": "error", "message": _error_message(e)})

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/")
def index():
    return FileResponse(ROOT / "static" / "index.html")
