"""Run tracing: every Riot request, tool call and decision becomes one JSON line.

A trace file lives at traces/<run_id>.jsonl. Each record has a step id (S1,
S2, ...) that a written review can cite as evidence.
"""

from __future__ import annotations

import functools
import json
import os
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TRACES = Path(os.environ.get("RIFTLENS_STATE_DIR", ROOT)).resolve() / "traces"


class Tracer:
    def __init__(self, run_name: str = "run"):
        TRACES.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.run_id = f"{stamp}-{run_name}"
        self.path = TRACES / f"{self.run_id}.jsonl"
        self.n = 0
        self.t0 = time.perf_counter()

    def rotate(self, run_name: str) -> None:
        """Start a new trace file (one per chat turn in the web app)."""
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.run_id = f"{stamp}-{run_name}"
        self.path = TRACES / f"{self.run_id}.jsonl"
        self.n = 0
        self.t0 = time.perf_counter()

    def records(self) -> list[dict]:
        if not self.path.exists():
            return []
        return [json.loads(l) for l in self.path.read_text(encoding="utf-8").splitlines() if l.strip()]

    def _write(self, rec: dict) -> str:
        self.n += 1
        rec = {"step": f"S{self.n}", "t_ms": round((time.perf_counter() - self.t0) * 1000), **rec}
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return rec["step"]

    def api(self, url: str, cache_hit: bool, status: int | None, latency_ms: float) -> str:
        return self._write({"type": "api", "url": url, "cache_hit": cache_hit, "status": status, "latency_ms": round(latency_ms)})

    def tool(self, name: str, args: dict, output, latency_ms: float, error: str | None = None) -> str:
        text = json.dumps(output, ensure_ascii=False) if output is not None else ""
        return self._write({
            "type": "tool", "name": name, "args": args, "latency_ms": round(latency_ms),
            "output_chars": len(text), "status": "error" if error else "ok", "error": error, "output": output,
        })

    def note(self, text: str, kind: str = "decision") -> str:
        return self._write({"type": kind, "text": text})

    def call(self, name: str, fn, *args, **kwargs):
        """Run fn, record it as a tool step, and return (output, step_id)."""
        t = time.perf_counter()
        try:
            out = fn(*args, **kwargs)
        except Exception as e:  # recorded, then re-raised
            self.tool(name, kwargs or {"args": [str(a) for a in args]}, None, (time.perf_counter() - t) * 1000, error=str(e))
            raise
        step = self.tool(name, kwargs or {"args": [str(a)[:60] for a in args]}, out, (time.perf_counter() - t) * 1000)
        return out, step


def traced(tracer: Tracer):
    """Decorator for MCP tool functions: records arguments, output and latency."""

    def wrap(fn):
        @functools.wraps(fn)
        def inner(*args, **kwargs):
            t = time.perf_counter()
            out = fn(*args, **kwargs)
            err = out.get("error") if isinstance(out, dict) else None
            tracer.tool(fn.__name__, kwargs, out, (time.perf_counter() - t) * 1000, error=err)
            return out

        return inner

    return wrap
