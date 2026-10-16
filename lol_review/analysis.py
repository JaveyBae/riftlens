"""Deterministic analysis over Riot match-v5 data.

Every function takes the raw match and timeline JSON and returns a compact,
JSON-serializable summary. Times are reported as "mm:ss" so the agent can cite
them. Nothing here calls a language model.

Data limits worth stating in any report: positions exist only in the
once-per-minute frames and on event records; ward placements carry no
coordinates; ability casts are not recorded at all.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict

MAP_SIZE = 14870  # Summoner's Rift; blue base bottom-left, river on x + y = MAP_SIZE
OBJECTIVE_NAMES = {
    "DRAGON": "小龙", "BARON_NASHOR": "男爵", "RIFTHERALD": "峡谷先锋",
    "HORDE": "虚空幼虫", "ATAKHAN": "阿塔坎", "ELDER_DRAGON": "远古龙",
}


def mmss(ms: float) -> str:
    s = int(ms // 1000)
    return f"{s // 60:02d}:{s % 60:02d}"


# ---------------------------------------------------------------- helpers
class Game:
    """Index over one match + timeline."""

    def __init__(self, match: dict, timeline: dict):
        self.match = match
        self.info = match["info"]
        self.frames = timeline["info"]["frames"]
        self.events = [e for f in self.frames for e in f.get("events", [])]
        self.parts = {p["participantId"]: p for p in self.info["participants"]}
        self.duration_min = self.info["gameDuration"] / 60

    def team(self, pid: int) -> int:
        return self.parts[pid]["teamId"]

    def name(self, pid: int) -> str:
        if pid not in self.parts:
            return "小兵/防御塔/野怪" if pid == 0 else f"#{pid}"
        p = self.parts[pid]
        return f"{p['championName']}({'蓝' if p['teamId'] == 100 else '红'})"

    def find(self, player: str) -> int:
        """Resolve 'name#tag', a champion name, or a participant id."""
        if player.isdigit():
            return int(player)
        game_name, _, tag = player.partition("#")
        for pid, p in self.parts.items():
            if p.get("riotIdGameName", "").lower() == game_name.lower() and (not tag or p.get("riotIdTagline", "").lower() == tag.lower()):
                return pid
        for pid, p in self.parts.items():
            if p["championName"].lower() == player.lower():
                return pid
        raise ValueError(f"player {player!r} not found in this match")

    def opponent(self, pid: int) -> int | None:
        role = self.parts[pid].get("teamPosition")
        for q, p in self.parts.items():
            if q != pid and p["teamId"] != self.team(pid) and role and p.get("teamPosition") == role:
                return q
        return None

    def frame_at(self, ms: float) -> dict:
        best = self.frames[0]
        for f in self.frames:
            if f["timestamp"] <= ms:
                best = f
        return best

    def gold_diff(self) -> list[tuple[int, int, int]]:
        """(minute, blue-red gold, blue-red xp) per frame."""
        out = []
        for i, f in enumerate(self.frames):
            g = {100: 0, 200: 0}
            x = {100: 0, 200: 0}
            for pid_s, pf in f["participantFrames"].items():
                t = self.team(int(pid_s))
                g[t] += pf["totalGold"]
                x[t] += pf["xp"]
            out.append((i, g[100] - g[200], x[100] - x[200]))
        return out


def side(team: int | None) -> str:
    return {100: "蓝方", 200: "红方"}.get(team, "无人（中立/小兵）")


def enemy_half(team: int, x: float, y: float) -> bool:
    return (x + y > MAP_SIZE) if team == 100 else (x + y < MAP_SIZE)


def dist(a: dict, b: dict) -> float:
    return math.hypot(a["x"] - b["x"], a["y"] - b["y"])


def _objective_events(g: Game) -> list[dict]:
    out = []
    for e in g.events:
        if e["type"] == "ELITE_MONSTER_KILL":
            kind = e.get("monsterType", "")
            if kind == "DRAGON" and e.get("monsterSubType") == "ELDER_DRAGON":
                kind = "ELDER_DRAGON"
            team = e.get("killerTeamId") or (g.team(e["killerId"]) if e.get("killerId") else None)
            if team not in (100, 200):  # 300 = no champion got the kill (minions, neutral)
                team = None
            out.append({"t": e["timestamp"], "team": team, "what": OBJECTIVE_NAMES.get(kind, kind)})
        elif e["type"] == "BUILDING_KILL":
            lost = e.get("teamId")
            taker = 200 if lost == 100 else 100
            what = "水晶" if e.get("buildingType") == "INHIBITOR_BUILDING" else f"{e.get('laneType', '').replace('_LANE', '').lower()}塔"
            out.append({"t": e["timestamp"], "team": taker, "what": what})
    return out


# ---------------------------------------------------------------- tools
def match_summary(g: Game) -> dict:
    teams = {t["teamId"]: t for t in g.info["teams"]}
    players = []
    for pid, p in g.parts.items():
        cs = p["totalMinionsKilled"] + p["neutralMinionsKilled"]
        players.append({
            "id": pid, "player": f"{p.get('riotIdGameName')}#{p.get('riotIdTagline')}",
            "team": "蓝" if p["teamId"] == 100 else "红", "role": p.get("teamPosition"),
            "champion": p["championName"], "kda": f"{p['kills']}/{p['deaths']}/{p['assists']}",
            "cs": cs, "cs_per_min": round(cs / g.duration_min, 1), "gold": p["goldEarned"],
            "damage": p["totalDamageDealtToChampions"], "vision_score": p["visionScore"],
        })
    return {
        "match_id": g.match["metadata"]["matchId"], "queue": g.info["queueId"],
        "patch": ".".join(g.info["gameVersion"].split(".")[:2]),
        "duration": mmss(g.info["gameDuration"] * 1000),
        "winner": "蓝" if teams[100]["win"] else "红",
        "players": players,
    }


def gold_xp_curve(g: Game) -> dict:
    rows = g.gold_diff()
    return {"note": "正数表示蓝方领先", "per_minute": [{"min": m, "gold_diff": d, "xp_diff": x} for m, d, x in rows]}


def turning_points(g: Game, top_k: int = 4, window: int = 2) -> dict:
    """Largest gold-difference swings over `window` minutes, with what happened inside.

    This is a proxy for win-probability swings until a trained model replaces it.
    """
    rows = g.gold_diff()
    swings = []
    for i in range(len(rows) - window):
        swings.append((abs(rows[i + window][1] - rows[i][1]), i))
    swings.sort(reverse=True)
    chosen: list[int] = []
    for _, i in swings:
        if all(abs(i - j) >= window + 1 for j in chosen):
            chosen.append(i)
        if len(chosen) == top_k:
            break
    objs = _objective_events(g)
    out = []
    for i in sorted(chosen):
        lo, hi = rows[i][0] * 60000, rows[i + window][0] * 60000
        kills = Counter(g.team(e["killerId"]) for e in g.events if e["type"] == "CHAMPION_KILL" and lo <= e["timestamp"] < hi and e.get("killerId"))
        out.append({
            "window": f"{mmss(lo)}–{mmss(hi)}",
            "gold_swing_to": "蓝" if rows[i + window][1] - rows[i][1] > 0 else "红",
            "gold_swing": abs(rows[i + window][1] - rows[i][1]),
            "kills": {"蓝": kills.get(100, 0), "红": kills.get(200, 0)},
            "objectives": [f"{mmss(o['t'])} {side(o['team'])}拿下{o['what']}" for o in objs if lo <= o["t"] < hi],
        })
    return {"method": f"{window} 分钟窗口内经济差变化最大的时段（胜率模型的代理指标）", "turning_points": out}


def segment_fights(g: Game, gap_s: int = 15) -> dict:
    kills = sorted((e for e in g.events if e["type"] == "CHAMPION_KILL"), key=lambda e: e["timestamp"])
    fights, cur = [], []
    for e in kills:
        if cur and e["timestamp"] - cur[-1]["timestamp"] > gap_s * 1000:
            fights.append(cur)
            cur = []
        cur.append(e)
    if cur:
        fights.append(cur)
    objs = _objective_events(g)
    out = []
    for f in fights:
        if len(f) < 2:
            continue
        involved = {e["victimId"] for e in f} | {e.get("killerId") for e in f} | {a for e in f for a in e.get("assistingParticipantIds", [])}
        involved.discard(0)
        involved.discard(None)
        deaths = Counter(g.team(e["victimId"]) for e in f)
        start, end = f[0]["timestamp"], f[-1]["timestamp"]
        out.append({
            "time": f"{mmss(start)}–{mmss(end)}",
            "participants": {"蓝": sum(1 for p in involved if g.team(p) == 100), "红": sum(1 for p in involved if g.team(p) == 200)},
            "deaths": {"蓝": deaths.get(100, 0), "红": deaths.get(200, 0)},
            "winner": "蓝" if deaths.get(200, 0) > deaths.get(100, 0) else ("红" if deaths.get(100, 0) > deaths.get(200, 0) else "平"),
            "victims": [f"{mmss(e['timestamp'])} {g.name(e['victimId'])}" for e in f],
            "objectives_after_60s": [f"{mmss(o['t'])} {side(o['team'])}拿下{o['what']}" for o in objs if end <= o["t"] <= end + 60000],
        })
    return {"rule": f"相邻击杀间隔 ≤ {gap_s} 秒且至少 2 次击杀视为一场团战", "fights": out}


def analyze_deaths(g: Game, pid: int, near: int = 2500) -> dict:
    team = g.team(pid)
    objs = _objective_events(g)
    out = []
    for e in g.events:
        if e["type"] != "CHAMPION_KILL" or e["victimId"] != pid:
            continue
        pos = e.get("position", {"x": 0, "y": 0})
        frame = g.frame_at(e["timestamp"])
        allies_near = sum(
            1 for q_s, pf in frame["participantFrames"].items()
            if int(q_s) != pid and g.team(int(q_s)) == team and "position" in pf and dist(pf["position"], pos) <= near
        )
        enemies = [e.get("killerId")] + e.get("assistingParticipantIds", [])
        lost = [f"{mmss(o['t'])} 对方拿下{o['what']}" for o in objs if o["team"] not in (team, None) and e["timestamp"] <= o["t"] <= e["timestamp"] + 90000]
        out.append({
            "time": mmss(e["timestamp"]),
            "killer": g.name(e.get("killerId", 0)),
            "enemies_involved": len([x for x in enemies if x]),
            "in_enemy_half": enemy_half(team, pos["x"], pos["y"]),
            "allies_within_2500_at_last_frame": allies_near,
            "enemy_objectives_within_90s": lost,
        })
    return {
        "player": g.name(pid), "deaths": out,
        "caveat": "队友距离用死亡前最近一分钟的位置快照估算；时间线没有插眼坐标，无法判断死亡点附近是否有视野。",
    }


def objective_control(g: Game) -> dict:
    objs = _objective_events(g)
    tally = defaultdict(lambda: Counter())
    for o in objs:
        tally[o["what"]][side(o["team"])] += 1
    return {
        "events": [f"{mmss(o['t'])} {side(o['team'])}拿下{o['what']}" for o in objs],
        "tally": {k: dict(v) for k, v in tally.items()},
    }


def lane_phase_stats(g: Game, pid: int) -> dict:
    opp = g.opponent(pid)
    out = {"player": g.name(pid), "opponent": g.name(opp) if opp else None, "checkpoints": []}
    for minute in (10, 15):
        if minute >= len(g.frames):
            continue
        f = g.frames[minute]["participantFrames"]

        def snap(q):
            pf = f[str(q)]
            return {"cs": pf["minionsKilled"] + pf["jungleMinionsKilled"], "gold": pf["totalGold"], "xp": pf["xp"], "level": pf["level"]}

        me = snap(pid)
        row = {"minute": minute, "me": me}
        if opp:
            them = snap(opp)
            row["opponent"] = them
            row["diff"] = {k: me[k] - them[k] for k in ("cs", "gold", "xp", "level")}
        out["checkpoints"].append(row)
    return out


def item_timeline(g: Game, pid: int, items: dict) -> dict:
    def completed(item_id: int) -> bool:
        it = items.get(str(item_id))
        if not it:
            return False
        is_boots = "Boots" in it.get("tags", []) and it.get("depth", 1) >= 2
        return is_boots or (not it.get("into") and it["gold"]["total"] >= 2200)

    rows = [
        {"time": mmss(e["timestamp"]), "item": items[str(e["itemId"])]["name"]}
        for e in g.events
        if e["type"] == "ITEM_PURCHASED" and e.get("participantId") == pid and completed(e["itemId"])
    ]
    return {"player": g.name(pid), "completed_items": rows, "rule": "鞋子或总价 ≥ 2200 且不可再合成的装备"}


def kill_log(g: Game, pid: int | None = None, before_min: float | None = None) -> dict:
    """Every champion kill with killer, victim and assists; `solo` means no assists."""
    rows = []
    for e in g.events:
        if e["type"] != "CHAMPION_KILL":
            continue
        if before_min is not None and e["timestamp"] >= before_min * 60000:
            continue
        killer, victim, assists = e.get("killerId", 0), e["victimId"], e.get("assistingParticipantIds", [])
        if pid is not None and pid not in (killer, victim, *assists):
            continue
        rows.append({
            "time": mmss(e["timestamp"]), "killer": g.name(killer), "victim": g.name(victim),
            "assists": [g.name(a) for a in assists], "solo": killer != 0 and not assists,
        })
    return {"kills": rows, "count": len(rows)}


def _quadrant(team: int, pos: dict) -> str:
    side = "敌方" if enemy_half(team, pos["x"], pos["y"]) else "己方"
    return side + ("上半区" if pos["y"] > pos["x"] else "下半区")


def jungle_stats(g: Game, pid: int) -> dict:
    """Jungle-specific view: involvement, objectives, invades, early route, vision."""
    team = g.team(pid)
    p = g.parts[pid]
    ch = p.get("challenges", {})
    team_kills = sum(q["kills"] for q in g.parts.values() if q["teamId"] == team)
    team_dmg = sum(q["totalDamageDealtToChampions"] for q in g.parts.values() if q["teamId"] == team)

    involved, missed_before_15 = [], 0
    for e in g.events:
        if e["type"] != "CHAMPION_KILL" or g.team(e["victimId"]) == team:
            continue
        mine = e.get("killerId") == pid or pid in e.get("assistingParticipantIds", [])
        if mine:
            involved.append(mmss(e["timestamp"]))
        elif e["timestamp"] < 15 * 60000:
            missed_before_15 += 1
    before_15 = sum(1 for t in involved if t < "15:00")

    epic = []
    for e in g.events:
        if e["type"] != "ELITE_MONSTER_KILL":
            continue
        kind = e.get("monsterType", "")
        if kind == "DRAGON" and e.get("monsterSubType") == "ELDER_DRAGON":
            kind = "ELDER_DRAGON"
        ours = (e.get("killerTeamId") or g.team(e.get("killerId", 1))) == team
        in_assist = pid in e.get("assistingParticipantIds", [])
        if e.get("killerId") == pid:
            outcome = "你拿下"
        elif ours:
            outcome = "队友拿下"
        elif in_assist:
            outcome = "对方拿下，但你也打了伤害（可能惩戒拼输）"
        else:
            outcome = "对方拿下"
        epic.append({"time": mmss(e["timestamp"]), "objective": OBJECTIVE_NAMES.get(kind, kind), "outcome": outcome})

    route = []
    for m in range(1, min(11, len(g.frames))):
        pf = g.frames[m]["participantFrames"][str(pid)]
        route.append({"minute": m, "area": _quadrant(team, pf["position"]), "jungle_cs": pf["jungleMinionsKilled"]})

    return {
        "player": g.name(pid),
        "kill_participation": round((p["kills"] + p["assists"]) / max(team_kills, 1), 3),
        "damage_share": round(p["totalDamageDealtToChampions"] / max(team_dmg, 1), 3),
        "takedown_times": involved,
        "takedowns_before_15": before_15,
        "team_kills_before_15_without_you": missed_before_15,
        "epic_monsters": epic,
        "invade": {"enemy_jungle_monsters": ch.get("enemyJungleMonsterKills"), "buffs_stolen": ch.get("buffsStolen"),
                   "scuttle_crabs": ch.get("scuttleCrabKills")},
        "jungle_cs_before_10": ch.get("jungleCsBefore10Minutes"),
        "early_route": route,
        "vision": {"wards_placed": p.get("wardsPlaced"), "wards_killed": p.get("wardsKilled"),
                   "control_wards_bought": p.get("visionWardsBoughtInGame"), "vision_score": p["visionScore"]},
        "caveat": "位置是每分钟一次的快照；'你也打了伤害'是从助攻名单推断的。",
    }


SR_QUEUES = {400: "匹配", 420: "单双排", 430: "匹配", 440: "灵活排位", 490: "快速匹配"}


def _game_row(match: dict, puuid: str) -> dict | None:
    info = match["info"]
    me = next((p for p in info["participants"] if p["puuid"] == puuid), None)
    if me is None:
        return None
    mins = info["gameDuration"] / 60
    ch = me.get("challenges", {})
    team = [p for p in info["participants"] if p["teamId"] == me["teamId"]]
    team_kills = sum(p["kills"] for p in team) or 1
    cs = me["totalMinionsKilled"] + me["neutralMinionsKilled"]
    return {
        "match_id": match["metadata"]["matchId"], "queue": SR_QUEUES.get(info["queueId"], info["queueId"]),
        "champion": me["championName"], "role": me.get("teamPosition") or "?", "win": me["win"],
        "minutes": round(mins, 1), "kills": me["kills"], "deaths": me["deaths"], "assists": me["assists"],
        "kda": round((me["kills"] + me["assists"]) / max(me["deaths"], 1), 2),
        "cs_per_min": round(cs / mins, 1), "gold_per_min": round(me["goldEarned"] / mins),
        "kill_participation": round((me["kills"] + me["assists"]) / team_kills, 2),
        "damage_share": round(ch.get("teamDamagePercentage", 0), 3),
        "vision_per_min": round(me["visionScore"] / mins, 2),
        "deaths_per_10min": round(me["deaths"] / mins * 10, 1),
        "solo_kills": ch.get("soloKills", 0),
        "max_cs_lead_vs_lane": ch.get("maxCsAdvantageOnLaneOpponent"),
        "max_level_lead_vs_lane": ch.get("maxLevelLeadLaneOpponent"),
        "lane_gold_xp_advantage": ch.get("laningPhaseGoldExpAdvantage"),
    }


def recent_form(matches: list[dict], puuid: str) -> dict:
    """Trends across several games from end-of-game stats only (no timelines)."""
    rows = [r for r in (_game_row(m, puuid) for m in matches) if r]
    if not rows:
        return {"games": 0, "note": "没有召唤师峡谷对局"}

    def avg(rs, k):
        vals = [r[k] for r in rs if isinstance(r.get(k), (int, float))]
        return round(sum(vals) / len(vals), 2) if vals else None

    keys = ["kda", "deaths_per_10min", "cs_per_min", "gold_per_min", "kill_participation", "damage_share",
            "vision_per_min", "solo_kills", "max_cs_lead_vs_lane"]
    wins, losses = [r for r in rows if r["win"]], [r for r in rows if not r["win"]]
    split = {k: {"赢": avg(wins, k), "输": avg(losses, k)} for k in keys}

    by_champ: dict[str, list] = {}
    for r in rows:
        by_champ.setdefault(f"{r['champion']}（{r['role']}）", []).append(r)
    champs = sorted(
        ({"champion": c, "games": len(rs), "wins": sum(x["win"] for x in rs), "kda": avg(rs, "kda"),
          "deaths_per_10min": avg(rs, "deaths_per_10min")} for c, rs in by_champ.items()),
        key=lambda x: -x["games"])

    streak, last = 0, rows[0]["win"]
    for r in rows:
        if r["win"] != last:
            break
        streak += 1

    flags = []
    if wins and losses:
        dw, dl = split["deaths_per_10min"]["赢"], split["deaths_per_10min"]["输"]
        if dl and dw and dl >= dw * 1.5:
            flags.append(f"输的局每 10 分钟死亡 {dl}，是赢的局（{dw}）的 {dl / dw:.1f} 倍：输局主要和死得多有关")
        kw, kl = split["kill_participation"]["赢"], split["kill_participation"]["输"]
        if kw and kl and kw - kl >= 0.12:
            flags.append(f"输的局参团率 {kl:.0%}，比赢的局低 {kw - kl:.0%}")
    for c in champs:
        if c["games"] >= 3 and c["wins"] / c["games"] <= 0.34:
            flags.append(f"{c['champion']} {c['games']} 局只赢 {c['wins']} 局")
    non_sup = [r for r in rows if r["role"] != "UTILITY"]
    if non_sup and avg(non_sup, "vision_per_min") is not None and avg(non_sup, "vision_per_min") < 0.6:
        flags.append(f"非辅助位置平均每分钟视野分 {avg(non_sup, 'vision_per_min')}，偏低")

    return {
        "games": len(rows), "wins": len(wins), "win_rate": round(len(wins) / len(rows), 2),
        "current_streak": f"{streak} 连{'胜' if last else '败'}",
        "overall": {k: avg(rows, k) for k in keys},
        "win_vs_loss": split, "by_champion": champs,
        "flags": flags, "games_detail": rows,
        "note": "只用终局统计，不含时间线；样本少时趋势仅供参考。",
    }


def detect_mistakes(g: Game, pid: int) -> dict:
    deaths = analyze_deaths(g, pid)["deaths"]
    flags = []
    for d in deaths:
        if d["enemy_objectives_within_90s"]:
            flags.append({"rule": "资源前阵亡", "time": d["time"], "evidence": d["enemy_objectives_within_90s"]})
        if d["in_enemy_half"] and d["allies_within_2500_at_last_frame"] == 0 and d["enemies_involved"] >= 2:
            flags.append({"rule": "孤身深入敌方半场被多人击杀", "time": d["time"], "evidence": f"{d['enemies_involved']} 名敌人参与，附近无队友"})
    killers = Counter(d["killer"] for d in deaths)
    for k, n in killers.items():
        if n >= 3:
            flags.append({"rule": "被同一名敌人反复击杀", "time": None, "evidence": f"{k} 击杀你 {n} 次"})
    lane = lane_phase_stats(g, pid)
    role = g.parts[pid].get("teamPosition")
    for cp in lane["checkpoints"]:
        if cp["minute"] == 10 and "diff" in cp and role != "UTILITY" and cp["diff"]["cs"] <= -15:
            flags.append({"rule": "对线期补刀落后", "time": "10:00", "evidence": f"补刀落后对位 {-cp['diff']['cs']}"})
    if role == "JUNGLE":
        js = jungle_stats(g, pid)
        total_15 = js["takedowns_before_15"] + js["team_kills_before_15_without_you"]
        if total_15 >= 6 and js["takedowns_before_15"] / total_15 < 0.35:
            flags.append({"rule": "打野前期参团过少", "time": "00:00–15:00",
                          "evidence": f"15 分钟前己方击杀 {total_15} 次，你参与 {js['takedowns_before_15']} 次"})
        for m in js["epic_monsters"]:
            if m["outcome"].startswith("对方拿下，但你也打了伤害"):
                flags.append({"rule": "资源争夺失败", "time": m["time"], "evidence": f"{m['objective']}被对方拿下，你在助攻名单中"})
    return {"player": g.name(pid), "flags": flags, "note": "规则检测只覆盖可从数据确定的失误，团战走位和技能释放无法判断。"}
