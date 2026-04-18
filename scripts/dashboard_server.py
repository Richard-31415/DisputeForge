"""FastAPI backend for the DisputeForge cockpit dashboard.

Endpoints:
  GET  /                   → redirects to /dashboard/
  GET  /dashboard/...      → serves the static dashboard files
  GET  /api/data           → returns the baked dashboard/data.js payload as JSON
  POST /api/dispute/stream → Server-Sent Events: streams per-node agent events
  POST /api/rollback/stream → Server-Sent Events: streams the clean+tampered pair
  GET  /api/health         → liveness

Run:
  uv run python scripts/dashboard_server.py

Then open http://localhost:8765/dashboard/
"""
from __future__ import annotations

import asyncio
import json
import os
import pathlib
import sys
import time
import uuid
from typing import Any, AsyncIterator

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
DASHBOARD_DIR = REPO_ROOT / "dashboard"

# Make `src.*` and `harness.*` importable when running this script directly.
sys.path.insert(0, str(REPO_ROOT))


def _load_env() -> None:
    env = REPO_ROOT / ".env"
    if env.exists() and "ANTHROPIC_API_KEY" not in os.environ:
        for ln in env.read_text().splitlines():
            if ln.startswith("ANTHROPIC_API_KEY="):
                os.environ["ANTHROPIC_API_KEY"] = ln.split("=", 1)[1].strip().strip('"').strip("'")
                break


_load_env()


app = FastAPI(title="DisputeForge Dashboard", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class DisputeInput(BaseModel):
    user_message: str = Field(..., min_length=5, max_length=2000)
    amount: float = Field(..., gt=0, lt=100000)
    merchant: str = Field("", max_length=200)
    category: str = Field("", max_length=50)
    case_id: str | None = None


def _sse(obj: dict[str, Any]) -> dict[str, Any]:
    """Shape for sse-starlette EventSourceResponse."""
    return {"data": json.dumps(obj, default=str)}


async def _run_graph_streaming(payload: DisputeInput) -> AsyncIterator[dict[str, Any]]:
    """Stream per-node events from LangGraph's astream to the client.

    We emit:
      {type: "start", case_id, input}
      {type: "node_enter", node}                  (pre-node marker)
      {type: "node_exit", node, delta}            (post-node with state delta)
      {type: "complete", final_state}
      {type: "error", detail}
    """
    from src.agent import build_graph, initial_state

    if not os.environ.get("ANTHROPIC_API_KEY"):
        yield _sse({"type": "error", "detail": "ANTHROPIC_API_KEY not set on the server."})
        return

    case_id = payload.case_id or f"live-{uuid.uuid4().hex[:8]}"
    init = initial_state(
        case_id=case_id,
        user_message=payload.user_message,
        account_id="acct-live",
        transaction_id=f"txn-live-{uuid.uuid4().hex[:6]}",
        amount=payload.amount,
        merchant=payload.merchant,
        category=payload.category,
    )

    yield _sse(
        {
            "type": "start",
            "case_id": case_id,
            "input": {
                "user_message": payload.user_message,
                "amount": payload.amount,
                "merchant": payload.merchant,
                "category": payload.category,
            },
            "ts": int(time.time() * 1000),
        }
    )

    t0 = time.perf_counter()
    try:
        graph = build_graph()
        cfg = {"configurable": {"thread_id": case_id}}

        last_state: dict[str, Any] = dict(init)

        async for event in graph.astream(init, config=cfg, stream_mode="updates"):
            # `event` is {node_name: delta_dict}
            for node_name, delta in event.items():
                if node_name.startswith("__"):
                    continue
                yield _sse({"type": "node_enter", "node": node_name, "ts": int(time.time() * 1000)})
                # small delay so the UI can draw the enter state before the exit
                await asyncio.sleep(0.12)
                # merge delta into last_state for final_state emission
                if isinstance(delta, dict):
                    for k, v in delta.items():
                        if k == "trace" and isinstance(v, list):
                            last_state.setdefault("trace", [])
                            last_state["trace"].extend(v)
                        else:
                            last_state[k] = v
                yield _sse({"type": "node_exit", "node": node_name, "delta": delta,
                            "ts": int(time.time() * 1000)})

        last_state["latency_ms"] = (time.perf_counter() - t0) * 1000.0
        yield _sse({"type": "complete", "final_state": last_state,
                    "ts": int(time.time() * 1000)})

    except Exception as e:
        yield _sse({"type": "error", "detail": f"{type(e).__name__}: {e}"})


async def _run_rollback_streaming() -> AsyncIterator[dict[str, Any]]:
    """Run clean + tampered together. Events carry a `run` tag: "clean" or "tampered"."""
    from src.agent import build_graph, initial_state
    from src.agent import nodes as node_mod
    from src.agent.compliance import REG_E_REQUIRED_PHRASES

    if not os.environ.get("ANTHROPIC_API_KEY"):
        yield _sse({"type": "error", "detail": "ANTHROPIC_API_KEY not set on the server."})
        return

    original_call = node_mod._call_json

    def tampered_call(*, model, system, user, max_tokens=1024):
        obj, err = original_call(model=model, system=system, user=user, max_tokens=max_tokens)
        if err or obj is None:
            return obj, err
        if "explainer" in system.lower() and "customer_message" in obj:
            msg = obj["customer_message"]
            for phrase in REG_E_REQUIRED_PHRASES:
                msg = msg.replace(phrase, "[stripped]")
            obj["customer_message"] = msg
        return obj, err

    base = {
        "case_id": f"rollback-{uuid.uuid4().hex[:6]}",
        "user_message": "I did not make this $24.99 charge — I've never heard of this merchant.",
        "account_id": "acct-demo-001",
        "transaction_id": "txn-rollback",
        "amount": 24.99,
        "merchant": "SketchyGadgets",
        "category": "online_retail",
    }

    for run_label, patched in [("clean", False), ("tampered", True)]:
        try:
            node_mod._call_json = tampered_call if patched else original_call
            init = initial_state(**base)

            yield _sse({"type": "run_start", "run": run_label,
                        "ts": int(time.time() * 1000)})

            graph = build_graph()
            cfg = {"configurable": {"thread_id": base["case_id"] + "_" + run_label}}
            last_state: dict[str, Any] = dict(init)

            async for event in graph.astream(init, config=cfg, stream_mode="updates"):
                for node_name, delta in event.items():
                    if node_name.startswith("__"):
                        continue
                    yield _sse({"type": "node_enter", "run": run_label, "node": node_name,
                                "ts": int(time.time() * 1000)})
                    await asyncio.sleep(0.12)
                    if isinstance(delta, dict):
                        for k, v in delta.items():
                            if k == "trace" and isinstance(v, list):
                                last_state.setdefault("trace", [])
                                last_state["trace"].extend(v)
                            else:
                                last_state[k] = v
                    yield _sse({"type": "node_exit", "run": run_label, "node": node_name,
                                "delta": delta, "ts": int(time.time() * 1000)})

            yield _sse({"type": "run_complete", "run": run_label, "final_state": last_state,
                        "ts": int(time.time() * 1000)})
        except Exception as e:
            yield _sse({"type": "error", "run": run_label,
                        "detail": f"{type(e).__name__}: {e}"})
        finally:
            node_mod._call_json = original_call

    yield _sse({"type": "complete", "ts": int(time.time() * 1000)})


# ----- routes -----


@app.get("/", include_in_schema=False)
async def root():
    return RedirectResponse(url="/dashboard/")


@app.get("/api/health")
async def health():
    return {
        "ok": True,
        "has_anthropic_key": bool(os.environ.get("ANTHROPIC_API_KEY")),
        "ts": int(time.time() * 1000),
    }


@app.get("/api/data")
async def api_data():
    data_js = DASHBOARD_DIR / "data.js"
    if not data_js.exists():
        raise HTTPException(404, "dashboard/data.js missing — run scripts/build_dashboard.py")
    text = data_js.read_text()
    # data.js is: window.DASHBOARD_DATA = {...};
    marker = "window.DASHBOARD_DATA ="
    idx = text.find(marker)
    if idx < 0:
        raise HTTPException(500, "data.js shape unexpected")
    payload = text[idx + len(marker) :].strip().rstrip(";").strip()
    return JSONResponse(content=json.loads(payload))


@app.post("/api/dispute/stream")
async def dispute_stream(payload: DisputeInput):
    return EventSourceResponse(_run_graph_streaming(payload))


@app.post("/api/rollback/stream")
async def rollback_stream():
    return EventSourceResponse(_run_rollback_streaming())


# Static dashboard — mounted LAST so /api/* routes take priority.
if DASHBOARD_DIR.exists():
    app.mount("/dashboard", StaticFiles(directory=str(DASHBOARD_DIR), html=True),
              name="dashboard")

# Quiet 2D alternate site, completely independent.
FLAT_DIR = REPO_ROOT / "flat"
if FLAT_DIR.exists():
    app.mount("/flat", StaticFiles(directory=str(FLAT_DIR), html=True), name="flat")


def main():
    import uvicorn

    port = int(os.environ.get("PORT", 8765))
    print(f"DisputeForge dashboard server — http://localhost:{port}/dashboard/")
    uvicorn.run(
        "scripts.dashboard_server:app",
        host="0.0.0.0",
        port=port,
        reload=False,
        log_level="info",
    )


if __name__ == "__main__":
    main()
