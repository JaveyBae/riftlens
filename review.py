"""Run the review skeleton for a player's latest match, with a full trace.

    .venv/bin/python review.py "Player#TAG" [--index 0] [--ranked]

Fixed skeleton for every match, then role-specific drill-down. Every Riot
request, tool call and routing decision is written to traces/<run_id>.jsonl;
the combined tool outputs go to reports/<run_id>.json for the model to read.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from lol_review import analysis as A
from lol_review.riot import Riot
from lol_review.trace import Tracer

ROOT = Path(__file__).resolve().parent


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("riot_id")
    ap.add_argument("--index", type=int, default=0, help="0 = most recent match")
    ap.add_argument("--ranked", action="store_true")
    args = ap.parse_args()

    tr = Tracer(args.riot_id.split("#")[0])
    riot = Riot()
    riot.tracer = tr
    tr.note(f"请求：复盘 {args.riot_id} 的第 {args.index + 1} 近一局" + ("（仅排位）" if args.ranked else ""), kind="request")

    name, _, tag = args.riot_id.partition("#")
    acct, _ = tr.call("find_player", riot.account_by_riot_id, name, tag)
    ids, _ = tr.call("list_recent_matches", riot.match_ids, acct["puuid"], args.index + 1, 420 if args.ranked else None)
    mid = ids[args.index]
    g = A.Game(riot.match(mid), riot.timeline(mid))
    pid = next(p for p, d in g.parts.items() if d["puuid"] == acct["puuid"])
    role = g.parts[pid].get("teamPosition") or "UNKNOWN"

    out: dict[str, dict] = {}
    steps: dict[str, str] = {}

    def run(key: str, fn, *a, **kw):
        res, step = tr.call(key, fn, *a, **kw)
        out[key], steps[key] = res, step
        return res

    tr.note("固定骨架：每局都执行 摘要 → 转折点 → 团战 → 资源 → 个人死亡 → 对线 → 出装 → 规则失误")
    run("match_summary", A.match_summary, g)
    run("turning_points", A.turning_points, g)
    run("segment_fights", A.segment_fights, g)
    run("objective_control", A.objective_control, g)
    deaths = run("analyze_deaths", A.analyze_deaths, g, pid)
    run("lane_phase_stats", A.lane_phase_stats, g, pid)
    run("item_timeline", A.item_timeline, g, pid, riot.items(g.info["gameVersion"]))
    run("detect_mistakes", A.detect_mistakes, g, pid)

    tr.note(f"位置 = {role}，死亡 {len(deaths['deaths'])} 次，据此选择深挖工具")
    if role == "JUNGLE":
        tr.note("打野：死亡类规则覆盖不到参团和资源争夺，加跑 jungle_stats")
        run("jungle_stats", A.jungle_stats, g, pid)
    if len(deaths["deaths"]) == 0:
        tr.note("零死亡：死亡分析为空，不能据此认为没有可改进之处")
    tr.note("加跑 kill_log（15 分钟前全部击杀，含助攻），用于核对'单杀'等说法")
    run("kill_log", A.kill_log, g, None, 15)

    report = {"run_id": tr.run_id, "match_id": mid, "player_id": pid, "role": role, "evidence_steps": steps, "tools": out}
    (ROOT / "reports").mkdir(exist_ok=True)
    rpath = ROOT / "reports" / f"{tr.run_id}.json"
    rpath.write_text(json.dumps(report, ensure_ascii=False, indent=1))
    tr.note(f"工具输出已汇总到 {rpath.name}，交给模型撰写复盘", kind="handoff")
    print(json.dumps({"trace": str(tr.path), "report": str(rpath), "match_id": mid, "role": role, "steps": steps}, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
