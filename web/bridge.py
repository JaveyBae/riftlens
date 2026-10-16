"""File mailbox between the web UI and an external model (Claude Code acting as the model).

    inbox.jsonl   one line per user message: {"id", "text", "history", "ts"}
    outbox.jsonl  one line per event:        {"msg_id", "event", "data", "ts"}

The web server appends to the inbox and streams outbox events for that id.
The model side uses the CLI below to see pending messages, call tools (each
call is mirrored to the UI) and post the answer.

    .venv/bin/python -m web.bridge pending
    .venv/bin/python -m web.bridge tool  <msg_id> <tool_name> '<json args>'
    .venv/bin/python -m web.bridge note  <msg_id> "<text>"
    .venv/bin/python -m web.bridge answer <msg_id> <markdown_file | ->   ("-" reads stdin)
    .venv/bin/python -m web.bridge tools
    .venv/bin/python -m web.bridge remember <msg_id> "<long-term fact about the user>"
"""

from __future__ import annotations

import json
import os
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
from lol_review.riot import STATE

BOX = STATE / "bridge"
INBOX, OUTBOX = BOX / "inbox.jsonl", BOX / "outbox.jsonl"


def _append(path: Path, rec: dict) -> None:
    BOX.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _read(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def post_user(text: str, history: list[dict], conversation_id: str | None = None, memory: str = "") -> str:
    mid = uuid.uuid4().hex[:8]
    _append(INBOX, {"id": mid, "text": text, "history": history[-12:], "conversation_id": conversation_id,
                    "memory": memory, "ts": time.time()})
    return mid


def emit(msg_id: str, event: str, data) -> None:
    _append(OUTBOX, {"msg_id": msg_id, "event": event, "data": data, "ts": time.time()})


def events_after(msg_id: str, offset: int) -> tuple[list[dict], int]:
    rows = [r for r in _read(OUTBOX) if r["msg_id"] == msg_id]
    return rows[offset:], len(rows)


def pending() -> list[dict]:
    done = {r["msg_id"] for r in _read(OUTBOX) if r["event"] == "answer"}
    return [m for m in _read(INBOX) if m["id"] not in done]


def short(obj, limit: int = 160) -> str:
    text = " ".join(obj.split()) if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _cli() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "pending"
    if cmd == "pending":
        print(json.dumps(pending(), ensure_ascii=False, indent=1))
    elif cmd == "tool":
        import server as S  # imported lazily: loads the tools and the tracer

        msg_id, name = sys.argv[2], sys.argv[3]
        args = json.loads(sys.argv[4]) if len(sys.argv) > 4 else {}
        # one trace file per user message, continued across CLI invocations
        S.TRACE.path = S.TRACE.path.parent / f"bridge-{msg_id}.jsonl"
        S.TRACE.run_id = S.TRACE.path.stem
        S.TRACE.n = len(S.TRACE.records())
        emit(msg_id, "tool_call", {"name": name, "args": args})
        fn = getattr(S, name, None)
        out = fn(**args) if fn else {"error": f"unknown tool {name}"}
        err = out.get("error") if isinstance(out, dict) else None
        emit(msg_id, "tool_result", {"name": name, "preview": short(out), "error": err})
        print(json.dumps(out, ensure_ascii=False, indent=1))
    elif cmd == "tools":
        import asyncio
        import server as S

        for t in asyncio.run(S.mcp.list_tools()):
            params = ", ".join(f"{k}: {v.get('type', 'any')}" for k, v in t.input_schema.get("properties", {}).items())
            print(f"- {t.name}({params}) — {(t.description or '').split('. ')[0]}")
    elif cmd == "remember":
        from web import store

        msg_id, fact = sys.argv[2], sys.argv[3]
        mem_id = store.add_memory(fact, msg_id)
        emit(msg_id, "memory", {"id": mem_id, "text": fact})
        print("remembered", mem_id)
    elif cmd == "note":
        emit(sys.argv[2], "note", {"text": sys.argv[3]})
    elif cmd == "answer":
        msg_id, src = sys.argv[2], sys.argv[3]
        text = sys.stdin.read() if src == "-" else Path(src).read_text(encoding="utf-8")
        emit(msg_id, "answer", {"text": text})
        if not os.environ.get("BRIDGE_DEFER_DONE"):  # the headless worker emits done after usage
            emit(msg_id, "done", {"steps": len([r for r in _read(OUTBOX) if r["msg_id"] == msg_id])})
        print("answered", msg_id)
    else:
        print(__doc__)


if __name__ == "__main__":
    _cli()
