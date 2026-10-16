"""Plot one player's per-minute curve against a champion benchmark band.

    .venv/bin/python benchmark_view.py benchmarks/LeeSin_JUNGLE_DIAMOND_16.20.json <match_id> "Player#TAG" [gold|xp|cs]
"""

from __future__ import annotations

import html
import json
import sys
from pathlib import Path

from lol_review import analysis as A
from lol_review.benchmark import compare, curve
from lol_review.riot import ROOT, Riot

LABEL = {"gold": "总经济", "xp": "经验", "cs": "补刀（含野怪）"}


def svg(rows: list[dict], key: str) -> str:
    W, H, x0, x1, y0, y1 = 760, 340, 64, 740, 20, 290
    top = max(max(r["p75"], r["you"]) for r in rows) * 1.05
    last = rows[-1]["minute"]
    X = lambda m: x0 + m / last * (x1 - x0)
    Y = lambda v: y1 - v / top * (y1 - y0)
    step = 2000 if key != "cs" else 50
    if key == "xp":
        step = 2500
    g = [f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="{LABEL[key]}对比">']
    v = 0
    while v <= top:
        g.append(f'<line x1="{x0}" y1="{Y(v):.1f}" x2="{x1}" y2="{Y(v):.1f}" stroke="var(--rule)" stroke-dasharray="3 4"/>'
                 f'<text x="{x0 - 8}" y="{Y(v) + 4:.1f}" text-anchor="end" class="m">{v:,}</text>')
        v += step
    for m in range(0, last + 1, 5):
        g.append(f'<text x="{X(m):.1f}" y="{y1 + 18}" text-anchor="middle" class="m">{m}</text>')
    band = " ".join(f"{X(r['minute']):.1f},{Y(r['p75']):.1f}" for r in rows) + " " + \
           " ".join(f"{X(r['minute']):.1f},{Y(r['p25']):.1f}" for r in reversed(rows))
    g.append(f'<polygon points="{band}" fill="var(--band)"/>')
    g.append('<polyline points="' + " ".join(f"{X(r['minute']):.1f},{Y(r['p50']):.1f}" for r in rows) +
             '" fill="none" stroke="var(--med)" stroke-width="2" stroke-dasharray="6 4"/>')
    g.append('<polyline points="' + " ".join(f"{X(r['minute']):.1f},{Y(r['you']):.1f}" for r in rows) +
             '" fill="none" stroke="var(--you)" stroke-width="2.8"/>')
    for r in rows:
        g.append(f'<circle cx="{X(r["minute"]):.1f}" cy="{Y(r["you"]):.1f}" r="2.6" fill="var(--you)"/>')
    g.append(f'<text x="{(x0 + x1) / 2}" y="{H - 6}" text-anchor="middle" class="m">分钟</text></svg>')
    return "\n".join(g)


def main() -> None:
    bpath, mid, player = sys.argv[1], sys.argv[2], sys.argv[3]
    keys = sys.argv[4:] or ["gold", "xp", "cs"]
    bench = json.loads(Path(bpath).read_text())
    riot = Riot()
    g = A.Game(riot.match(mid), riot.timeline(mid))
    pid = g.find(player)
    target = curve({"info": {"frames": g.frames}}, pid)

    css = """:root{--bg:#f4f5f2;--paper:#fff;--ink:#1c2120;--muted:#5d6762;--rule:#d7dcd6;--you:#c0420f;--med:#2b6f78;--band:#d6e9eb}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#121514;--paper:#1a1e1c;--ink:#e4e8e4;--muted:#9aa49e;--rule:#343b37;--you:#f0884a;--med:#6bb4be;--band:#1d3a3e;color-scheme:dark}}
:root[data-theme="dark"]{--bg:#121514;--paper:#1a1e1c;--ink:#e4e8e4;--muted:#9aa49e;--rule:#343b37;--you:#f0884a;--med:#6bb4be;--band:#1d3a3e;color-scheme:dark}
body{background:var(--bg);color:var(--ink);font-family:"PingFang SC","Noto Sans SC",system-ui,sans-serif;margin:0;line-height:1.6;padding-block:28px 48px;padding-left:max(16px,env(safe-area-inset-left,0px));padding-right:max(16px,env(safe-area-inset-right,0px))}
.wrap{max-width:900px;margin:0 auto;display:flex;flex-direction:column;gap:20px}
h1{margin:0;font-size:26px}h2{margin:0;font-size:18px}.card{background:var(--paper);border:1px solid var(--rule);border-radius:8px;padding:16px 18px;display:flex;flex-direction:column;gap:10px;min-width:0}
.muted{color:var(--muted)}svg{max-width:100%;height:auto;display:block}svg text{font-size:11px;fill:var(--ink)}svg .m{fill:var(--muted)}
.legend{display:flex;flex-wrap:wrap;gap:6px 16px;font-size:13px}.legend i{display:inline-block;width:18px;height:3px;vertical-align:middle;margin-right:6px}
table{border-collapse:collapse;width:100%;font-size:13.5px;font-variant-numeric:tabular-nums}td,th{padding:5px 8px;border-bottom:1px solid var(--rule);text-align:right}th{font-size:12px;color:var(--muted);font-weight:500}td:first-child,th:first-child{text-align:left}
.tbl{overflow-x:auto}.pos{color:#2e7a4b}.neg{color:#b23b3b}"""
    parts = ['<title>李青经济对比</title>', '<meta name="claude:viewport-fit-sides" content="cover">', f"<style>{css}</style>", '<div class="wrap">',
             f'<header><div class="muted" style="font-size:13px">{html.escape(mid)} · {html.escape(player)}</div>'
             f'<h1>{html.escape(g.name(pid))} 对比 {bench["tier"]} {bench["champion"]} {bench["role"]}</h1>'
             f'<p class="muted">基准：{bench["n_games"]} 场 {bench["patch"]} 版本单双排（其中胜 {bench["wins"]} 场），共扫描 {bench["matches_scanned"]} 场对局。{html.escape(bench["sampling"])}</p></header>']
    for key in keys:
        rows = compare(bench, target, key)
        parts.append(f'<section class="card"><h2>{LABEL[key]}</h2>'
                     '<div class="legend"><span><i style="background:var(--you)"></i>本局</span>'
                     '<span><i style="background:var(--med)"></i>基准中位数</span>'
                     '<span><i style="background:var(--band);height:10px"></i>基准 P25–P75</span></div>')
        parts.append(svg(rows, key))
        parts.append('<div class="tbl"><table><thead><tr><th>分钟</th><th>本局</th><th>P25</th><th>中位数</th><th>P75</th><th>比中位数</th><th>区间</th><th>样本</th></tr></thead><tbody>')
        for r in rows:
            if r["minute"] % 2 and r["minute"] != rows[-1]["minute"]:
                continue
            cls = "pos" if r["vs_median"] > 0 else "neg"
            parts.append(f'<tr><td>{r["minute"]}</td><td>{r["you"]:,}</td><td>{r["p25"]:,}</td><td>{r["p50"]:,}</td><td>{r["p75"]:,}</td>'
                         f'<td class="{cls}">{r["vs_median"]:+,}</td><td>{r["band"]}</td><td>{r["n"]}</td></tr>')
        parts.append("</tbody></table></div></section>")
    parts.append('<p class="muted" style="font-size:13px">样本量随时间减少：比赛越晚，还在进行的对局越少，后期分位数可信度更低。本局是灵活排位，基准取自单双排。</p></div>')
    out = ROOT / "reports" / f"bench_{mid}_{pid}.html"
    out.write_text("\n".join(parts), encoding="utf-8")
    print(out)
    print(json.dumps(compare(bench, target, "gold"), ensure_ascii=False))


if __name__ == "__main__":
    main()
