"""Playstyle profile, scout score and premade detection from client match history.

The tag rules and thresholds follow LoL Desktop Assistant (LOL-AI-Intelligence,
AGPL-3.0) `compute_playstyle_profile`, so the same player gets the same tags.
Kill participation needs team kills, which the client's match list does not
include; tags that depend on it are skipped unless full games were fetched.
"""

from __future__ import annotations

from collections import Counter

MIN_GAMES = 5
SR_MAPS = (None, 11)


def stat_row(g: dict, team_kills: int | None = None) -> dict | None:
    """One Summoner's Rift game from the client's match list (the queried player's participant only)."""
    if g.get("mapId") not in SR_MAPS or g.get("gameDuration", 0) < 300:
        return None
    p = (g.get("participants") or [{}])[0]
    s = p.get("stats") or {}
    from lol_review.lcu import _position

    return {
        "game_id": g.get("gameId"), "queue_id": g.get("queueId"), "champion_id": p.get("championId"), "role": _position(p),
        "minutes": max(g.get("gameDuration", 0) / 60, 1), "win": bool(s.get("win")),
        "kills": s.get("kills", 0), "deaths": s.get("deaths", 0), "assists": s.get("assists", 0),
        "cs": s.get("totalMinionsKilled", 0) + s.get("neutralMinionsKilled", 0),
        "damage": s.get("totalDamageDealtToChampions", 0), "taken": s.get("totalDamageTaken", 0),
        "objectives": s.get("damageDealtToObjectives", 0), "turrets": s.get("damageDealtToTurrets", 0),
        "vision": s.get("visionScore", 0), "wards": s.get("wardsPlaced", 0), "pinks": s.get("visionWardsBoughtInGame", 0),
        "spree": s.get("largestKillingSpree", 0),
        "multikills": s.get("doubleKills", 0) + s.get("tripleKills", 0) + s.get("quadraKills", 0) + s.get("pentaKills", 0),
        "dead_seconds": s.get("totalTimeSpentDead"),
        "first_blood": bool(s.get("firstBloodKill") or s.get("firstBloodAssist")),
        "first_tower": bool(s.get("firstTowerKill") or s.get("firstTowerAssist")),
        "team_kills": team_kills,
    }


def _tag(text: str, tone: str, desc: str) -> dict:
    return {"text": text, "tone": tone, "desc": desc}


def profile(rows: list[dict]) -> dict:
    """Averages and playstyle tags for recent games, newest first. tone: good / bad / warn / info."""
    rows = [r for r in rows if r]
    n = len(rows)
    if n < MIN_GAMES:
        return {"games": n, "tags": [], "averages": {}}
    mins = sum(r["minutes"] for r in rows)
    avg = lambda k: sum(r[k] for r in rows) / n  # noqa: E731
    cspm, dpm, dtpm = sum(r["cs"] for r in rows) / mins, sum(r["damage"] for r in rows) / mins, sum(r["taken"] for r in rows) / mins
    kills, deaths, assists = avg("kills"), avg("deaths"), avg("assists")
    vision, wards, pinks, spree, multi = avg("vision"), avg("wards"), avg("pinks"), avg("spree"), avg("multikills")
    obj, turret = avg("objectives"), avg("turrets")
    fb, ft = avg("first_blood"), avg("first_tower")
    kp_rows = [r for r in rows if r.get("team_kills")]
    kp = sum((r["kills"] + r["assists"]) / r["team_kills"] for r in kp_rows) / len(kp_rows) if len(kp_rows) >= MIN_GAMES else None
    dead_rows = [r["dead_seconds"] for r in rows if r.get("dead_seconds") is not None]
    dead = sum(dead_rows) / len(dead_rows) if dead_rows else None

    roles = Counter(r["role"] for r in rows if r["role"])
    main_role, main_games = roles.most_common(1)[0] if roles else ("", 0)
    distinct_roles = sum(1 for c in roles.values() if c / n >= 0.15)
    champs = Counter(r["champion_id"] for r in rows)
    top_share = champs.most_common(1)[0][1] / n if champs else 0
    streak, last = 0, rows[0]["win"]
    for r in rows:
        if r["win"] != last:
            break
        streak += 1
    carry = main_role in ("MIDDLE", "BOTTOM", "TOP")
    T = []

    dmg_line = {"MIDDLE": 650, "BOTTOM": 650, "TOP": 520, "JUNGLE": 480}.get(main_role, 600)
    dealer = main_role != "UTILITY" and dpm >= dmg_line
    if dealer:
        T.append(_tag("输出核心", "good", f"每分钟对英雄伤害 {dpm:.0f}"))
        if deaths >= 6:
            T.append(_tag("玻璃大炮", "warn", f"伤害高，但场均阵亡 {deaths:.1f}"))
    if not dealer and dtpm >= 950 and main_role in ("TOP", "JUNGLE", "UTILITY"):
        T.append(_tag("钢铁前排", "info", f"每分钟承受伤害 {dtpm:.0f}"))
    duelist = spree >= 5 or multi >= 1.2
    if duelist:
        T.append(_tag("致命决斗者", "good", "经常连杀、多杀"))
    if not duelist and deaths >= 6 and spree <= 2 and kills < deaths:
        T.append(_tag("决斗弱势", "bad", "小规模交手吃亏，难以滚雪球"))
    if kp is not None and kp >= 0.62 and kills >= 7:
        T.append(_tag("众矢之的", "info", f"参团率 {kp:.0%}，场均 {kills:.1f} 杀"))
    if kills >= 8 and fb >= 0.3:
        T.append(_tag("嗜血", "good", "把前期人头滚成高击杀"))
    if fb >= 0.35 or ft >= 0.45:
        T.append(_tag("开场就干", "good", "经常参与一血或一塔"))

    if deaths <= 3.5:
        T.append(_tag("稳健求生", "good", f"场均阵亡 {deaths:.1f}"))
    elif deaths >= 7.5:
        T.append(_tag("莽夫", "bad", f"场均阵亡 {deaths:.1f}"))
    if dead is not None and dead >= 300:
        T.append(_tag("早已阵亡", "bad", f"每局约 {dead / 60:.1f} 分钟在等复活"))
    if kp is not None and deaths <= 3 and kp >= 0.5:
        T.append(_tag("逃生大师", "good", "常在团里却极少阵亡"))

    farmer = cspm >= 6.0 if main_role == "JUNGLE" else (False if main_role == "UTILITY" else cspm >= 7.2)
    if farmer:
        T.append(_tag("补刀机器", "good", f"每分钟补刀 {cspm:.1f}"))
    phobia = False if main_role in ("UTILITY", "") else (cspm <= 3.5 if main_role == "JUNGLE" else cspm <= 4.5)
    if phobia:
        T.append(_tag("恐补症", "bad", f"每分钟补刀只有 {cspm:.1f}"))
    if carry and cspm >= 7 and deaths <= 4:
        T.append(_tag("对线强势", "good", "补刀好、死得少"))
    if carry and cspm <= 5 and deaths >= 5:
        T.append(_tag("对线乏力", "bad", "补刀少、容易阵亡"))

    vis_good = {"UTILITY": 40, "JUNGLE": 28}.get(main_role, 24)
    vis_bad = {"UTILITY": 25, "JUNGLE": 15}.get(main_role, 12)
    if vision >= vis_good:
        T.append(_tag("视野大师", "good", f"场均视野分 {vision:.0f}"))
    visionless = vision <= vis_bad
    if visionless:
        T.append(_tag("视野缺失", "bad", f"场均视野分 {vision:.0f}"))
    if not visionless and main_role != "UTILITY" and wards <= 7:
        T.append(_tag("前期失明", "warn", f"场均插眼 {wards:.1f}"))
    if pinks <= 1:
        T.append(_tag("不买真眼", "warn", f"场均控制守卫 {pinks:.1f}"))

    if kp is not None and kp >= 0.55 and assists >= kills:
        T.append(_tag("节奏发动机", "good", f"参团率 {kp:.0%}"))
    if kp is not None and kp >= 0.55 and cspm <= 6 and main_role in ("MIDDLE", "UTILITY", "JUNGLE"):
        T.append(_tag("游走威胁", "info", "经常游走影响其他路"))
    if obj >= 11000:
        T.append(_tag("资源型", "good", "重视小龙、男爵和防御塔"))
    split = turret >= 3500 and kp is not None and kp <= 0.5
    if split:
        T.append(_tag("分推流", "info", "压制边路、专注推塔"))
    elif turret >= 3500:
        T.append(_tag("拆塔狂魔", "good", f"场均对塔伤害 {turret:.0f}"))

    if top_share >= 0.5 and len(champs) <= 4:
        T.append(_tag("单英雄", "info", "主玩单一英雄"))
    elif len(champs) >= 10:
        T.append(_tag("多面手", "info", f"近 {n} 局用了 {len(champs)} 个英雄"))
    if main_games / n >= 0.7:
        T.append(_tag("位置专精", "info", "专精单一位置"))
    elif distinct_roles >= 3:
        T.append(_tag("全能补位", "info", "能胜任多个位置"))
    if streak >= 3:
        T.append(_tag(f"{streak} 连{'胜' if last else '败'}", "good" if last else "bad", f"当前 {streak} 连{'胜' if last else '败'}"))

    return {
        "games": n, "tags": T, "main_role": main_role,
        "averages": {"kills": round(kills, 1), "deaths": round(deaths, 1), "assists": round(assists, 1),
                     "cs_per_min": round(cspm, 1), "damage_per_min": round(dpm), "vision": round(vision, 1),
                     "kill_participation": round(kp, 2) if kp is not None else None,
                     "win_rate": round(sum(r["win"] for r in rows) / n, 2)},
    }


def scout_score(games: int, wins: int, kda: float | None) -> int | None:
    """Composite from match volume, wins and average KDA. Only meaningful compared with the other nine players."""
    if not games:
        return None
    return round(games * 408 + wins * 777 + (kda or 0) * 1200)


def premade_groups(histories: dict[str, set], teams: list[list[str]], min_shared: int = 2) -> list[list[str]]:
    """Players on the same team who appear in ≥ min_shared of the same recent games (League Akari's rule)."""
    groups = []
    for team in teams:
        members = [p for p in team if p and histories.get(p)]
        parent = {p: p for p in members}

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        for i, a in enumerate(members):
            for b in members[i + 1:]:
                if len(histories[a] & histories[b]) >= min_shared:
                    parent[find(a)] = find(b)
        by_root: dict[str, list[str]] = {}
        for p in members:
            by_root.setdefault(find(p), []).append(p)
        groups += [g for g in by_root.values() if len(g) >= 2]
    return groups


def performance_scores(participants: list[dict], minutes: float) -> dict[int, float]:
    """0-10 rating per participantId for a scoreboard (KDA, kill share, damage/gold share, CS, vision, result)."""
    teams: dict[int, dict] = {}
    for p in participants:
        t = teams.setdefault(p["teamId"], {"kills": 0, "damage": 0, "gold": 0})
        t["kills"] += p["kills"]
        t["damage"] += p["damage"]
        t["gold"] += p["gold"]
    cap = lambda v, m: min(max(v / m, 0.0), 1.0)  # noqa: E731
    out = {}
    for p in participants:
        t = teams[p["teamId"]]
        kda = (p["kills"] + p["assists"]) / max(p["deaths"], 1)
        score = (cap(kda, 12) * 0.22 + (min((p["kills"] + p["assists"]) / t["kills"], 1) if t["kills"] else 0) * 0.18
                 + cap(p["damage"] / t["damage"] if t["damage"] else 0, 0.35) * 0.20 + cap(p["gold"] / t["gold"] if t["gold"] else 0, 0.28) * 0.12
                 + cap(p["cs"] / minutes, 10) * 0.10 + cap(p["vision"] / minutes, 2) * 0.10 + (1.0 if p["win"] else 0.35) * 0.08)
        out[p["participantId"]] = round(score * 10, 1)
    return out
