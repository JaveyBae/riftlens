"""MCP server exposing League of Legends review tools.

Register with Claude Code:
    claude mcp add lol-review -- <repo>/.venv/bin/python <repo>/server.py
"""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer

from lol_review import analysis as A
from lol_review.riot import Riot, RiotError
from lol_review.trace import Tracer, traced

TRACE = Tracer("mcp")
riot = Riot()
riot.tracer = TRACE
mcp = MCPServer(
    name="lol-review",
    instructions=(
        "Tools for reviewing a League of Legends match from Riot match-v5 data. "
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
