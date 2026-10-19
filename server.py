"""MCP server exposing League of Legends review tools.

Register with Claude Code:
    claude mcp add lol-review -- <repo>/.venv/bin/python <repo>/server.py
"""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer

from lol_review import analysis as A
from lol_review import lcu
from lol_review.riot import Riot, RiotError
from lol_review.trace import Tracer, traced

TRACE = Tracer("mcp")
riot = Riot()
riot.tracer = TRACE
mcp = MCPServer(
    name="lol-review",
    instructions=(
        "Tools for reviewing a League of Legends match from Riot match-v5 data. "
        "Games played on this computer (any server, including the Tencent client) are read from the running client: "
        "use client_recent_matches, whose match ids start with LCU_ and work with every match tool. "
        "Start with find_player and list_recent_matches, then get_match_summary and turning_points. "
        "Cite every claim with the mm:ss times the tools return. Ability usage, exact positioning "
        "and ward locations are not in the data; say so instead of guessing."
    ),
)
PLAN: list[dict] = []


def tool(fn):
    """Register fn as an MCP tool and record every call in the trace."""
    return mcp.tool()(traced(TRACE)(fn))


def _game(match_id: str) -> A.Game:
    if match_id.upper().startswith("LCU_"):  # a game read from the local client (any server, including 国服)
        gid = int(match_id.split("_", 1)[1])
        client = lcu.default()
        return A.Game(*lcu.to_match_v5(client.game(gid), client.timeline(gid), client))
    return A.Game(riot.match(match_id), riot.timeline(match_id))


def _err(e: Exception) -> dict:
    return {"error": str(e)}


@tool
def find_player(riot_id: str) -> dict:
    """Look up a player by Riot ID such as 'Player#TAG'. Returns the puuid and current ranked entries."""
    try:
        name, _, tag = riot_id.partition("#")
        acct = riot.account_by_riot_id(name, tag)
        ranks = [
            {"queue": r["queueType"], "tier": f"{r['tier']} {r['rank']}", "lp": r["leaguePoints"], "wins": r["wins"], "losses": r["losses"]}
            for r in riot.league_entries(acct["puuid"])
        ]
        return {"riot_id": f"{acct['gameName']}#{acct['tagLine']}", "puuid": acct["puuid"], "ranked": ranks}
    except (RiotError, KeyError) as e:
        return _err(e)


@tool
def list_recent_matches(puuid: str, count: int = 5, ranked_only: bool = False) -> dict:
    """List the player's most recent matches with champion, role, result and KDA. ranked_only keeps Ranked Solo/Duo (queue 420)."""
    try:
        ids = riot.match_ids(puuid, count=count, queue=420 if ranked_only else None)
        rows = []
        for mid in ids:
            m = riot.match(mid)
            me = next(p for p in m["info"]["participants"] if p["puuid"] == puuid)
            rows.append({
                "match_id": mid, "queue": m["info"]["queueId"], "champion": me["championName"],
                "role": me.get("teamPosition"), "win": me["win"],
                "kda": f"{me['kills']}/{me['deaths']}/{me['assists']}",
                "duration": A.mmss(m["info"]["gameDuration"] * 1000),
            })
        return {"matches": rows}
    except (RiotError, StopIteration) as e:
        return _err(e)


@tool
def client_recent_matches(count: int = 10, ranked_only: bool = False) -> dict:
    """Recent games of the account logged into the League client on this computer, read from the client itself (works on every server, including 国服, and needs no Riot API key). The match_id values (LCU_...) work with every match tool."""
    try:
        c = lcu.default()
        me = c.me() or {}
        rows = [lcu.history_row(g, c) for g in c.history(me["puuid"], max(count * 2, count) if ranked_only else count)]
        if ranked_only:
            rows = [r for r in rows if r["queue_id"] in (420, 440)]
        return {"player": f"{me.get('gameName', '')}#{me.get('tagLine', '')}", "puuid": me.get("puuid"), "matches": rows[:count]}
    except (RiotError, KeyError) as e:
        return _err(e)


@tool
def client_player(puuid: str = "") -> dict:
    """Rank, recent record, streak, main champions and tags for one player, read from the local client. Empty puuid = the logged-in account."""
    from lol_review import assistant

    try:
        c = lcu.default()
        return assistant.get().player(puuid or (c.me() or {})["puuid"])
    except (RiotError, KeyError) as e:
        return _err(e)


@tool
def client_recent_form(count: int = 10, ranked_only: bool = False) -> dict:
    """Trends across the logged-in account's recent Summoner's Rift games, read from the local client: win rate, wins vs losses, champion pool, streak."""
    try:
        c = lcu.default()
        me = c.me() or {}
        matches = []
        for g in c.history(me["puuid"], min(count * 2, 40)):
            if g.get("mapId") not in (None, 11) or g.get("gameDuration", 0) < 300:
                continue
            if ranked_only and g.get("queueId") not in (420, 440):
                continue
            matches.append(lcu.to_match_v5(c.game(g["gameId"]), None, c)[0])
            if len(matches) >= count:
                break
        return A.recent_form(matches, me["puuid"])
    except (RiotError, KeyError) as e:
        return _err(e)


_TEAM_KILLS: dict[int, dict[int, int]] = {}  # gameId -> {teamId: kills}; finished games never change


def _team_kills(c: lcu.LCU, g: dict) -> int | None:
    gid = g.get("gameId")
    if gid not in _TEAM_KILLS:
        full = c.game(gid)
        tk: dict[int, int] = {}
        for p in full.get("participants", []):
            tk[p["teamId"]] = tk.get(p["teamId"], 0) + (p.get("stats") or {}).get("kills", 0)
        _TEAM_KILLS[gid] = tk
    team = ((g.get("participants") or [{}])[0]).get("teamId")
    return _TEAM_KILLS[gid].get(team)


@tool
def client_coach_data(count: int = 50, position: str = "", ranked_only: bool = True) -> dict:
    """Multi-game coaching data for the logged-in account, read from the local client: up to 100 recent Summoner's Rift games (ranked solo/flex by default), optionally one position (TOP/JUNGLE/MIDDLE/BOTTOM/UTILITY). Returns averages, playstyle tags with the numbers behind them, wins-vs-losses averages, and records per champion and per position. Use it for "analyse my recent games / what are my strengths and weaknesses"."""
    from collections import defaultdict

    from lol_review import playstyle

    try:
        c = lcu.default()
        me = c.me() or {}
        pos = (position or "").upper()
        pos = {"MID": "MIDDLE", "ADC": "BOTTOM", "BOT": "BOTTOM", "SUPPORT": "UTILITY", "SUP": "UTILITY"}.get(pos, pos)
        raw = [g for g in c.history_pages(me["puuid"], min(max(count, 5), 100) * (3 if ranked_only or pos else 1))
               if not ranked_only or g.get("queueId") in (420, 440)]
        rows = []
        for g in raw:
            r = playstyle.stat_row(g)
            if r and (not pos or r["role"] == pos):
                rows.append((g, r))
            if len(rows) >= min(count, 100):
                break
        for g, r in rows[:20]:  # kill participation needs the full game; the newest 20 are enough
            try:
                r["team_kills"] = _team_kills(c, g)
            except RiotError:
                pass
        stats = [r for _, r in rows]
        prof = playstyle.profile(stats)

        def avg(rs):
            n = len(rs) or 1
            m = sum(r["minutes"] for r in rs) or 1
            return {"games": len(rs), "kda": f"{sum(r['kills'] for r in rs) / n:.1f}/{sum(r['deaths'] for r in rs) / n:.1f}/{sum(r['assists'] for r in rs) / n:.1f}",
                    "cs_per_min": round(sum(r["cs"] for r in rs) / m, 1), "damage_per_min": round(sum(r["damage"] for r in rs) / m),
                    "vision": round(sum(r["vision"] for r in rs) / n, 1), "wards": round(sum(r["wards"] for r in rs) / n, 1)}

        by_champ, by_pos = defaultdict(list), defaultdict(list)
        for r in stats:
            by_champ[r["champion_id"]].append(r)
            by_pos[r["role"] or "?"].append(r)
        record = lambda rs: {"games": len(rs), "wins": sum(r["win"] for r in rs), "win_rate": round(sum(r["win"] for r in rs) / len(rs), 2)}  # noqa: E731
        return {
            "player": f"{me.get('gameName', '')}#{me.get('tagLine', '')}", "scope": pos or "全部位置",
            "queues": "单双排 + 灵活组排" if ranked_only else "全部召唤师峡谷对局", "games": len(stats),
            "overall": {**record(stats), **avg(stats)} if stats else {},
            "playstyle_tags": [f"{t['text']}（{t['desc']}）" for t in prof["tags"]], "averages": prof["averages"],
            "wins_vs_losses": {"wins": avg([r for r in stats if r["win"]]), "losses": avg([r for r in stats if not r["win"]])},
            "champions": sorted(({"champion": c.champ_name(cid), **record(rs), **avg(rs)} for cid, rs in by_champ.items()), key=lambda x: -x["games"])[:10],
            "positions": {p: record(rs) for p, rs in by_pos.items()},
            "note": "数据来自本机客户端；参团率只按最近 20 局计算。没有技能、走位和插眼位置数据。",
        }
    except (RiotError, KeyError) as e:
        return _err(e)


@tool
def client_game_plan() -> dict:
    """The game the user is in right now (champ select or loading screen / in game), read from the local client: all known picks, my lane opponent and the OP.GG matchup win rate, both team compositions (AD/AP, tanks, healing, hard CC), every visible player's rank, recent record, scout score, playstyle tags, champion mastery, premades and the user's notes, the build adjusted to the enemy composition, and rule-based tips. Use it for "how should I play this game / what should I build / who is the weak lane"."""
    from lol_review import assistant, gameplan

    try:
        a = assistant.get()
        plan = a.state.get("plan")
        ctx = gameplan.context(a.state)
        if not ctx:
            return {"error": "现在不在英雄选择或对局中（或者还没有锁定英雄）。"}
        b = plan["build"] if plan and plan.get("context", {}).get("enemy_cids") == ctx["enemy_cids"] else gameplan.build(ctx["me"]["champion_id"], ctx["me"]["position"], ctx["enemy_cids"])
        slim_build = {"core": [i["name"] for i in b["core"]], "boots": [i["name"] for i in b["boots"]], "boots_why": b["boots_why"],
                      "adjust": [{"item": x["item"]["name"], "why": x["why"]} for x in b["adjust"]], "late": [i["name"] for i in b["late"]], "source": b["source"]}
        return {**{k: v for k, v in ctx.items() if k != "enemy_cids"}, "build": slim_build, "tips": gameplan.rules(ctx, b),
                "note": "选人阶段看不到对方玩家，只有英雄；进入加载界面后才有十人战绩。"}
    except Exception as e:
        return _err(e)


@tool
def opgg_champion(champion: str, position: str = "", mode: str = "ranked") -> dict:
    """OP.GG statistics for one champion (Chinese or English name): tier, win/pick/ban rate, most popular runes, summoner spells, items, skill order, and the hardest/easiest matchups. position: TOP/JUNGLE/MID/ADC/SUPPORT (empty = main position). mode: ranked or aram. Global Emerald+ data for the current patch."""
    from lol_review import opgg

    try:
        cid = opgg.champion_id(champion)
        if cid is None:
            return {"error": f"找不到英雄 {champion}"}
        d = opgg.champion(cid, position, mode=mode)

        def names(xs):
            return [x["name"] for x in xs]

        return {
            "champion": d["name"], "position": d["position"], "stats": d["stats"], "source": "OP.GG（全球 翡翠+）",
            "runes": [{"keystone": r["primary"][0]["name"], "primary": names(r["primary"]), "secondary": names(r["secondary"]),
                       "win_rate": r["win_rate"], "pick_rate": r["pick_rate"]} for r in d["runes"][:2]],
            "spells": [{"spells": names(s["spells"]), "win_rate": s["win_rate"], "pick_rate": s["pick_rate"]} for s in d["spells"]],
            "core_items": [{"items": names(b["items"]), "win_rate": b["win_rate"], "pick_rate": b["pick_rate"]} for b in d["core_items"][:3]],
            "boots": [{"items": names(b["items"]), "win_rate": b["win_rate"]} for b in d["boots"][:2]],
            "skill_order": d["skills"][0]["order"] if d["skills"] else None,
            "hardest_matchups": [{"vs": c["name"], "win_rate": c["win_rate"], "games": c["play"]} for c in d["counters"]["hard"][:5]],
            "easiest_matchups": [{"vs": c["name"], "win_rate": c["win_rate"], "games": c["play"]} for c in d["counters"]["easy"][:5]],
            "note": d["note"],
        }
    except Exception as e:  # OP.GG is unofficial; never break the agent
        return _err(e)


@tool
def get_match_summary(match_id: str) -> dict:
    """All ten players' champion, role, KDA, CS, gold, damage and vision score, plus patch, duration and winner."""
    try:
        return A.match_summary(_game(match_id))
    except RiotError as e:
        return _err(e)


@tool
def gold_xp_curve(match_id: str) -> dict:
    """Blue-minus-red team gold and XP difference for every minute."""
    try:
        return A.gold_xp_curve(_game(match_id))
    except RiotError as e:
        return _err(e)


@tool
def turning_points(match_id: str, top_k: int = 4) -> dict:
    """The time windows where the gold lead swung most, with the kills and objectives inside each window. Use this to find where the game was won or lost."""
    try:
        return A.turning_points(_game(match_id), top_k=top_k)
    except RiotError as e:
        return _err(e)


@tool
def segment_fights(match_id: str) -> dict:
    """Group kills into team fights: time, players involved per side, deaths per side, winner, and objectives taken within 60 s after."""
    try:
        return A.segment_fights(_game(match_id))
    except RiotError as e:
        return _err(e)


@tool
def analyze_deaths(match_id: str, player: str) -> dict:
    """Every death of one player (Riot ID, champion name or participant id): killer, number of enemies, whether it was in the enemy half, allies nearby, and enemy objectives taken within 90 s."""
    try:
        g = _game(match_id)
        return A.analyze_deaths(g, g.find(player))
    except (RiotError, ValueError) as e:
        return _err(e)


@tool
def objective_control(match_id: str) -> dict:
    """Every dragon, grubs, herald, baron, tower and inhibitor taken, in order, with a per-type tally."""
    try:
        return A.objective_control(_game(match_id))
    except RiotError as e:
        return _err(e)


@tool
def lane_phase_stats(match_id: str, player: str) -> dict:
    """CS, gold, XP and level at 10 and 15 minutes for the player and their lane opponent."""
    try:
        g = _game(match_id)
        return A.lane_phase_stats(g, g.find(player))
    except (RiotError, ValueError) as e:
        return _err(e)


@tool
def item_timeline(match_id: str, player: str) -> dict:
    """When the player completed boots and each finished item."""
    try:
        g = _game(match_id)
        return A.item_timeline(g, g.find(player), riot.items(g.info["gameVersion"]))
    except (RiotError, ValueError) as e:
        return _err(e)


@tool
def detect_mistakes(match_id: str, player: str) -> dict:
    """Rule-based mistakes that the data can prove: dying before an enemy objective, dying alone deep in the enemy half to several enemies, repeated deaths to one enemy, CS deficit at 10 minutes."""
    try:
        g = _game(match_id)
        return A.detect_mistakes(g, g.find(player))
    except (RiotError, ValueError) as e:
        return _err(e)


@tool
def recent_form(riot_id: str, count: int = 10, ranked_only: bool = False) -> dict:
    """Trends over the player's last `count` Summoner's Rift games using end-of-game stats only (no timelines): win rate, averages, wins vs losses, per-champion results and flags. Arena and other modes are skipped. Use for 'how have my last 5/10 games gone'."""
    try:
        name, _, tag = riot_id.partition("#")
        puuid = riot.account_by_riot_id(name, tag)["puuid"]
        queues = {420, 440} if ranked_only else set(A.SR_QUEUES)
        matches, scanned = [], 0
        for mid in riot.match_ids(puuid, count=min(count * 3, 60)):
            scanned += 1
            m = riot.match(mid)
            if m["info"]["queueId"] in queues and m["info"]["gameDuration"] >= 15 * 60:
                matches.append(m)
            if len(matches) >= count:
                break
        out = A.recent_form(matches, puuid)
        out["scanned_matches"] = scanned
        return out
    except RiotError as e:
        return _err(e)


NICKNAMES = {  # common Chinese player nicknames → Data Dragon id
    "鳄鱼": "Renekton", "瞎子": "LeeSin", "男枪": "Graves", "女枪": "MissFortune", "剑魔": "Aatrox", "剑圣": "MasterYi",
    "刀妹": "Irelia", "蛇女": "Cassiopeia", "狗头": "Nasus", "螳螂": "Khazix", "男刀": "Talon", "鸟人": "Anivia",
    "大嘴": "KogMaw", "小炮": "Tristana", "EZ": "Ezreal", "卡萨": "Kaisa", "卡莎": "Kaisa", "老鼠": "Twitch",
    "石头人": "Malphite", "蜘蛛": "Elise", "龙王": "AurelionSol", "锤石": "Thresh", "风女": "Janna", "猴子": "MonkeyKing",
    "武器": "Jax", "诺手": "Darius", "青钢影": "Camille", "剑姬": "Fiora", "腕豪": "Sett", "奶妈": "Sona", "VN": "Vayne",
}


def _resolve_champion(name: str, version: str) -> str | None:
    q = name.strip().lower().replace(" ", "")
    if name.strip() in NICKNAMES:
        return NICKNAMES[name.strip()]
    for cid, c in riot.champions(version).items():
        if q in (cid.lower(), c["name"].lower(), c["title"].lower(), c["name"].lower() + c["title"].lower()):
            return cid
    return None


@tool
def champion_info(champion: str) -> dict:
    """Official champion data from Riot Data Dragon (Chinese, current patch): roles, base stats, passive and Q/W/E/R with cooldowns, costs, ranges and descriptions. Accepts English id (Renekton), Chinese name or title (荒漠屠夫 / 雷克顿) or a common nickname (鳄鱼). Use for ability facts before giving matchup advice."""
    import re

    try:
        version = riot._get("https://ddragon.leagueoflegends.com/api/versions.json", "ddragon_versions", ttl=86400, auth=False)[0]
        cid = _resolve_champion(champion, version)
        if not cid:
            return {"error": f"找不到英雄 {champion!r}，请用英文名或官方中文名"}
        c = riot.champion(cid, version)
        clean = lambda s: re.sub(r"<[^>]+>", "", s or "")
        return {
            "id": cid, "name": c["name"], "title": c["title"], "patch": version, "tags": c["tags"],
            "resource": c.get("partype"),
            "stats": {k: c["stats"][k] for k in ("hp", "hpperlevel", "armor", "armorperlevel", "spellblock",
                                                 "attackdamage", "attackrange", "movespeed")},
            "passive": {"name": c["passive"]["name"], "description": clean(c["passive"]["description"])},
            "spells": [{"key": k, "name": s["name"], "cooldown": s["cooldownBurn"], "cost": s["costBurn"],
                        "range": s["rangeBurn"], "description": clean(s["description"])}
                       for k, s in zip("QWER", c["spells"])],
            "source": f"Riot Data Dragon {version} zh_CN",
        }
    except RiotError as e:
        return _err(e)


@tool
def kill_log(match_id: str, player: str = "", before_minute: float = 0) -> dict:
    """Every champion kill with killer, victim and assists, optionally only those involving `player` or before `before_minute`. Use it to verify claims such as 'solo kill' (solo = no assists)."""
    try:
        g = _game(match_id)
        return A.kill_log(g, g.find(player) if player else None, before_minute or None)
    except (RiotError, ValueError) as e:
        return _err(e)


@tool
def jungle_stats(match_id: str, player: str) -> dict:
    """Jungler view: kill participation, takedowns before 15 min vs team kills without you, who took each epic monster (including ones lost while you were fighting for it), invades and stolen buffs, minute-by-minute route for the first 10 minutes, and vision."""
    try:
        g = _game(match_id)
        return A.jungle_stats(g, g.find(player))
    except (RiotError, ValueError) as e:
        return _err(e)


@tool
def update_plan(steps: list[dict]) -> str:
    """Record or update your review plan. Each step: {"description": str, "status": "pending" | "in_progress" | "done"}. List the directions you will investigate before drilling down, and update statuses as you go."""
    PLAN[:] = steps
    marks = {"pending": "[ ]", "in_progress": "[~]", "done": "[x]"}
    return "\n".join(f"{marks.get(s.get('status'), '[ ]')} {s.get('description', '')}" for s in steps)


if __name__ == "__main__":
    mcp.run()
