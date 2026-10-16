"""Render a trace (and optional review with evidence links) as one self-contained HTML page.

    .venv/bin/python trace_view.py traces/<run_id>.jsonl [reports/<run_id>.review.json]
"""

from __future__ import annotations

import html
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

CSS = """
:root{--bg:#f3f4f1;--paper:#fff;--ink:#1c2120;--muted:#5d6762;--rule:#d7dcd6;--api:#2b6f78;--api-s:#dcecee;--tool:#7a4fc4;--tool-s:#efe8fa;
--dec:#b8662a;--dec-s:#f6e6d8;--ok:#2e7a4b;--ok-s:#e0f0e5;--inf:#a0700f;--inf-s:#f6ecd2;
--mono:ui-monospace,Menlo,Consolas,monospace;--sans:"PingFang SC","Noto Sans SC",system-ui,sans-serif}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#121514;--paper:#1a1e1c;--ink:#e4e8e4;--muted:#9aa49e;--rule:#343b37;
--api:#6bb4be;--api-s:#173236;--tool:#a888e8;--tool-s:#2c2342;--dec:#e0935a;--dec-s:#3a2818;--ok:#64c18a;--ok-s:#173322;--inf:#d9a84a;--inf-s:#352a12;color-scheme:dark}}
:root[data-theme="dark"]{--bg:#121514;--paper:#1a1e1c;--ink:#e4e8e4;--muted:#9aa49e;--rule:#343b37;
--api:#6bb4be;--api-s:#173236;--tool:#a888e8;--tool-s:#2c2342;--dec:#e0935a;--dec-s:#3a2818;--ok:#64c18a;--ok-s:#173322;--inf:#d9a84a;--inf-s:#352a12;color-scheme:dark}
body{background:var(--bg);color:var(--ink);font-family:var(--sans);margin:0;padding-block:28px 48px;
padding-left:max(16px,env(safe-area-inset-left,0px));padding-right:max(16px,env(safe-area-inset-right,0px));line-height:1.6}
.wrap{max-width:1040px;margin:0 auto;display:flex;flex-direction:column;gap:22px}
h1{font-size:26px;margin:0}h2{font-size:18px;margin:0}
.muted{color:var(--muted)}.mono{font-family:var(--mono);font-size:12.5px}
.card{background:var(--paper);border:1px solid var(--rule);border-radius:8px;padding:16px 18px;display:flex;flex-direction:column;gap:10px;min-width:0}
.stats{display:flex;flex-wrap:wrap;gap:8px 18px;font-size:14px}
.stats b{font-variant-numeric:tabular-nums}
table{border-collapse:collapse;width:100%;font-size:14px}
td,th{padding:7px 8px;border-bottom:1px solid var(--rule);text-align:left;vertical-align:top}
th{font-size:12px;color:var(--muted);font-weight:500}
.tag{font-family:var(--mono);font-size:11px;border-radius:3px;padding:1px 6px;white-space:nowrap}
.t-api{background:var(--api-s);color:var(--api)}.t-tool{background:var(--tool-s);color:var(--tool)}
.t-decision,.t-request,.t-handoff{background:var(--dec-s);color:var(--dec)}
.s-verified{background:var(--ok-s);color:var(--ok)}.s-inference{background:var(--inf-s);color:var(--inf)}
a.ev{font-family:var(--mono);font-size:12px;color:var(--tool);text-decoration:none;margin-right:6px}
a.ev:hover,a.ev:focus-visible{text-decoration:underline}
.step{display:grid;grid-template-columns:52px 64px 1fr auto;gap:10px;align-items:baseline;padding:6px 0;border-bottom:1px solid var(--rule);scroll-margin-top:16px}
.step:target{background:var(--tool-s)}
.step>*{min-width:0}
details summary{cursor:pointer}
pre{background:var(--bg);border-radius:6px;padding:10px;overflow-x:auto;font-family:var(--mono);font-size:12px;max-height:340px;margin:6px 0 0}
ul{margin:0;padding-left:20px}
@media (max-width:640px){.step{grid-template-columns:44px 1fr}.step .lat{grid-column:2}}
"""


def esc(x) -> str:
    return html.escape(str(x))


def main() -> None:
    tpath = Path(sys.argv[1])
    rows = [json.loads(l) for l in tpath.read_text(encoding="utf-8").splitlines() if l.strip()]
    review = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8")) if len(sys.argv) > 2 else None
    run_id = tpath.stem

    n_api = sum(r["type"] == "api" for r in rows)
    n_net = sum(r["type"] == "api" and not r["cache_hit"] for r in rows)
    n_tool = sum(r["type"] == "tool" for r in rows)
    n_dec = sum(r["type"] in ("decision",) for r in rows)
    chars = sum(r.get("output_chars", 0) for r in rows if r["type"] == "tool")
    total_ms = rows[-1]["t_ms"] if rows else 0

    parts = [f"<title>LoL 复盘轨迹</title>",
             '<meta name="claude:viewport-fit-sides" content="cover">',
             f"<style>{CSS}</style>", '<div class="wrap">',
             f'<header><div class="mono muted">trace · {esc(run_id)}</div><h1>LoL 复盘轨迹</h1></header>',
             '<div class="card"><div class="stats">'
             f"<span>总步数 <b>{len(rows)}</b></span><span>API 请求 <b>{n_api}</b>（联网 {n_net}，缓存 {n_api - n_net}）</span>"
             f"<span>工具调用 <b>{n_tool}</b></span><span>决策 <b>{n_dec}</b></span>"
             f"<span>工具输出合计 <b>{chars:,}</b> 字符（≈ 交给模型的上下文量）</span><span>耗时 <b>{total_ms} ms</b></span>"
             "</div></div>"]

    if review:
        parts.append('<section class="card"><h2>复盘结论与证据</h2>')
        parts.append(f"<p><b>{esc(review['headline'])}</b></p>")
        parts.append("<table><thead><tr><th>结论</th><th>证据步骤</th><th>状态</th></tr></thead><tbody>")
        for c in review["claims"]:
            ev = "".join(f'<a class="ev" href="#{esc(s)}">{esc(s)}</a>' for s in c["evidence"])
            label = {"verified": "已核对", "inference": "推断"}.get(c["status"], c["status"])
            parts.append(f'<tr><td>{esc(c["text"])}</td><td>{ev}</td><td><span class="tag s-{esc(c["status"])}">{label}</span></td></tr>')
        parts.append("</tbody></table>")
        for title, key in (("建议", "advice"), ("数据局限", "limits"), ("更正", "corrections")):
            if review.get(key):
                parts.append(f"<h2>{title}</h2><ul>" + "".join(f"<li>{esc(x)}</li>" for x in review[key]) + "</ul>")
        parts.append(f'<p class="muted mono">{esc(review.get("author", ""))}</p></section>')

    parts.append('<section class="card"><h2>逐步轨迹</h2>')
    for r in rows:
        t = r["type"]
        if t == "api":
            body = f'<span class="mono">{esc(r["url"].split(".com")[-1])}</span>'
            lat = "缓存" if r["cache_hit"] else f'HTTP {r["status"]} · {r["latency_ms"]} ms'
        elif t == "tool":
            out = json.dumps(r.get("output"), ensure_ascii=False, indent=1)
            body = (f'<details><summary><b>{esc(r["name"])}</b> <span class="muted">{r["output_chars"]:,} 字符'
                    f'{" · 出错：" + esc(r["error"]) if r.get("error") else ""}</span></summary><pre>{esc(out)}</pre></details>')
            lat = f'{r["latency_ms"]} ms'
        else:
            body = esc(r["text"])
            lat = ""
        parts.append(f'<div class="step" id="{esc(r["step"])}"><span class="mono">{esc(r["step"])}</span>'
                     f'<span><span class="tag t-{esc(t)}">{esc(t)}</span></span><div>{body}</div><span class="mono muted lat">{lat}</span></div>')
    parts.append("</section></div>")

    out = ROOT / "reports" / f"{run_id}.html"
    out.write_text("\n".join(parts), encoding="utf-8")
    print(out)


if __name__ == "__main__":
    main()
