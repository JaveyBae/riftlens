"""League Client (LCU) API: the local HTTPS API the running client exposes.

Works on every server, including the Tencent (国服) client, because it talks to
the client on this machine instead of Riot's web API. The client must be
running and logged in.

Connection details come from the LeagueClientUx process command line
(--app-port / --remoting-auth-token) or from the `lockfile` in the install
folder. The Tencent client usually runs elevated, in which case RiftLens must
run as administrator to read them.

`to_match_v5` converts the client's match-history format (the old match-v4
shape) into the match-v5 shape `analysis.Game` expects, so every review tool
also works on games read from the client.
"""

from __future__ import annotations

import base64
import os
import re
import subprocess
from pathlib import Path

import requests
import urllib3

from lol_review.riot import RiotError

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)  # the client uses a self-signed cert

PROCESS = "LeagueClientUx.exe"
COMMON_DIRS = [
    r"C:\Riot Games\League of Legends",
    r"D:\Riot Games\League of Legends",
    r"C:\Program Files\Riot Games\League of Legends",
    r"D:\WeGameApps\英雄联盟\LeagueClient",
    r"C:\WeGameApps\英雄联盟\LeagueClient",
    r"D:\英雄联盟\LeagueClient",
    r"C:\英雄联盟\LeagueClient",
]
QUEUES = {420: "单双排", 440: "灵活排位", 400: "匹配", 430: "匹配", 490: "快速匹配", 450: "大乱斗",
          1700: "斗魂竞技场", 1900: "无限火力", 900: "无限乱斗", 0: "自定义"}
SMITE = 11


class LCUError(RiotError):
    """Subclass of RiotError so every tool's error handling also covers the client."""


# ---------------------------------------------------------------- discovery
def _from_cmdline(cmd: str) -> tuple[int, str] | None:
    port = re.search(r"--app-port=(\d+)", cmd)
    token = re.search(r"--remoting-auth-token=([\w-]+)", cmd)
    return (int(port.group(1)), token.group(1)) if port and token else None


def _from_lockfile(folder: str | Path) -> tuple[int, str] | None:
    path = Path(folder) / "lockfile"
    try:
        _name, _pid, port, password, _proto = path.read_text(encoding="utf-8").strip().split(":")
        return int(port), password
    except (OSError, ValueError):
        return None


def discover(install_dir: str | None = None) -> tuple[int, str] | None:
    """(port, password) of the running client, or None if it is not running or not readable."""
    try:
        import psutil

        for p in psutil.process_iter(["name"]):
            if (p.info.get("name") or "").lower() != PROCESS.lower():
                continue
            try:
                found = _from_cmdline(" ".join(p.cmdline()))
                if found:
                    return found
                found = _from_lockfile(Path(p.exe()).parent)
                if found:
                    return found
            except (psutil.AccessDenied, psutil.NoSuchProcess):
                pass
    except ImportError:
        pass
    if os.name == "nt":  # WMI sometimes succeeds where psutil is denied
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 f"(Get-CimInstance Win32_Process -Filter \"name='{PROCESS}'\").CommandLine"],
                capture_output=True, text=True, timeout=8, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            found = _from_cmdline(out.stdout or "")
            if found:
                return found
        except (OSError, subprocess.SubprocessError):
            pass
    for folder in ([install_dir] if install_dir else []) + COMMON_DIRS:
        found = _from_lockfile(folder)
        if found:
            return found
    return None


def client_running() -> bool:
    try:
        import psutil

        return any((p.info.get("name") or "").lower() == PROCESS.lower() for p in psutil.process_iter(["name"]))
    except ImportError:
        return False


# ---------------------------------------------------------------- client
class LCU:
    def __init__(self, install_dir: str | None = None) -> None:
        self.install_dir = install_dir
        self.port: int | None = None
        self.session = requests.Session()
        self.session.verify = False
        self._champs: dict[int, dict] | None = None

    @property
    def connected(self) -> bool:
        return self.port is not None

    def connect(self) -> bool:
        found = discover(self.install_dir)
        if not found:
            self.port = None
            return False
        port, password = found
        if port != self.port:
            self._champs = None
        self.port = port
        self.session.headers["Authorization"] = "Basic " + base64.b64encode(f"riot:{password}".encode()).decode()
        return True

    def request(self, method: str, path: str, **kw):
        if not self.port and not self.connect():
            raise LCUError("没有找到正在运行的英雄联盟客户端。请先登录客户端；国服客户端需要以管理员身份运行 RiftLens。")
        try:
            r = self.session.request(method, f"https://127.0.0.1:{self.port}{path}", timeout=kw.pop("timeout", 10), **kw)
        except requests.RequestException as e:
            self.port = None  # client closed or restarted; rediscover next time
            raise LCUError(f"连接客户端失败：{e}") from e
        if r.status_code == 404:
            return None
        if r.status_code >= 400:
            raise LCUError(f"客户端返回 {r.status_code}：{r.text[:200]}")
        if not r.content:
            return None
        ctype = r.headers.get("Content-Type", "")
        return r.json() if "json" in ctype else r.content

    def get(self, path: str, **kw):
        return self.request("GET", path, **kw)

    # ---- endpoints -------------------------------------------------------
    def me(self) -> dict | None:
        return self.get("/lol-summoner/v1/current-summoner")

    def phase(self) -> str:
        return self.get("/lol-gameflow/v1/gameflow-phase") or "None"

    def gameflow_session(self) -> dict | None:
        return self.get("/lol-gameflow/v1/session")

    def accept_ready_check(self) -> None:
        self.request("POST", "/lol-matchmaking/v1/ready-check/accept")

    def ready_check(self) -> dict | None:
        return self.get("/lol-matchmaking/v1/ready-check")

    def champ_select(self) -> dict | None:
        return self.get("/lol-champ-select/v1/session")

    def pickable(self) -> set[int]:
        return set(self.get("/lol-champ-select/v1/pickable-champion-ids") or [])

    def bannable(self) -> set[int]:
        return set(self.get("/lol-champ-select/v1/bannable-champion-ids") or [])

    def do_action(self, action_id: int, champion_id: int, complete: bool) -> None:
        self.request("PATCH", f"/lol-champ-select/v1/session/actions/{action_id}",
                     json={"championId": champion_id, "completed": complete})

    def summoner(self, puuid: str) -> dict | None:
        return self.get(f"/lol-summoner/v2/summoners/puuid/{puuid}")

    def ranked(self, puuid: str) -> dict | None:
        return self.get(f"/lol-ranked/v1/ranked-stats/{puuid}")

    def history(self, puuid: str, count: int = 20) -> list[dict]:
        data = self.get(f"/lol-match-history/v1/products/lol/{puuid}/matches?begIndex=0&endIndex={max(count - 1, 0)}") or {}
        return (data.get("games") or {}).get("games") or []

    def game(self, game_id: int) -> dict:
        g = self.get(f"/lol-match-history/v1/games/{game_id}")
        if not g:
            raise LCUError(f"客户端里找不到对局 {game_id}")
        return g

    def timeline(self, game_id: int) -> dict:
        t = self.get(f"/lol-match-history/v1/game-timelines/{game_id}")
        if not t:
            raise LCUError(f"客户端里找不到对局 {game_id} 的时间线")
        return t

    def end_of_game(self) -> dict | None:
        return self.get("/lol-end-of-game/v1/eog-stats-block")

    def apply_runes(self, name: str, primary_style: int, sub_style: int, perks: list[int]) -> dict:
        """Replace RiftLens's own rune page (or free a slot) and make the new page current."""
        pages = self.get("/lol-perks/v1/pages") or []
        for p in pages:
            if p.get("isDeletable") and str(p.get("name", "")).startswith("RiftLens"):
                self.request("DELETE", f"/lol-perks/v1/pages/{p['id']}")
        body = {"name": name[:25], "primaryStyleId": primary_style, "subStyleId": sub_style, "selectedPerkIds": perks, "current": True}
        try:
            return self.request("POST", "/lol-perks/v1/pages", json=body) or {}
        except LCUError:  # page limit reached: replace the current editable page
            cur = self.get("/lol-perks/v1/currentpage") or {}
            if not cur.get("isDeletable"):
                raise LCUError("符文页已满，而且当前页不能删除。请在客户端删掉一页再试。")
            self.request("DELETE", f"/lol-perks/v1/pages/{cur['id']}")
            return self.request("POST", "/lol-perks/v1/pages", json=body) or {}

    def apply_item_set(self, title: str, champion_id: int, blocks: list[dict]) -> None:
        """Write RiftLens's recommended build into the client's item sets (shown in the in-game shop).

        Only sets whose title starts with "RiftLens" are replaced; the user's other sets are kept as they are, and
        the first time the original list is saved to data/itemsets_backup_<summonerId>.json.
        """
        import json
        import time
        import uuid

        from lol_review.riot import STATE

        me = self.me() or {}
        sid = me.get("summonerId")
        if not sid:
            raise LCUError("读不到召唤师 id")
        path = f"/lol-item-sets/v1/item-sets/{sid}/sets"
        cur = self.get(path) or {}
        backup = STATE / "data" / f"itemsets_backup_{sid}.json"
        if not backup.exists():
            backup.parent.mkdir(parents=True, exist_ok=True)
            backup.write_text(json.dumps(cur, ensure_ascii=False), encoding="utf-8")
        sets = [s for s in cur.get("itemSets") or [] if not str(s.get("title", "")).startswith("RiftLens")]
        sets.append({"title": title[:75], "associatedChampions": [champion_id], "associatedMaps": [11], "blocks": blocks,
                     "map": "any", "mode": "any", "preferredItemSlots": [], "sortrank": 0, "startedFrom": "blank",
                     "type": "custom", "uid": str(uuid.uuid4())})
        self.request("PUT", path, json={"accountId": cur.get("accountId") or me.get("accountId"), "itemSets": sets,
                                         "timestamp": int(time.time() * 1000)})

    def apply_spells(self, spell1: int, spell2: int) -> None:
        """Summoner spells (D, F). Only works during champ select."""
        self.request("PATCH", "/lol-champ-select/v1/session/my-selection", json={"spell1Id": spell1, "spell2Id": spell2})

    def history_pages(self, puuid: str, count: int) -> list[dict]:
        """Up to `count` recent games, fetched 20 at a time (some servers cap one page at 20)."""
        out, seen = [], set()
        for beg in range(0, count, 20):
            data = self.get(f"/lol-match-history/v1/products/lol/{puuid}/matches?begIndex={beg}&endIndex={min(beg + 19, count - 1)}") or {}
            page = [g for g in (data.get("games") or {}).get("games") or [] if g.get("gameId") not in seen]
            seen |= {g.get("gameId") for g in page}
            out += page
            if len(page) < 20:
                break
        out.sort(key=lambda g: -g.get("gameCreation", 0))
        return out[:count]

    def lookup(self, riot_id: str) -> dict | None:
        """Summoner for `name#TAG` on the client's own server."""
        name, _, tag = riot_id.strip().partition("#")
        if not name:
            return None
        if tag:
            try:
                hits = self.get("/player-account/aliases/v1/lookup", params={"gameName": name, "tagLine": tag}) or []
                if isinstance(hits, list) and hits and hits[0].get("puuid"):
                    return self.summoner(hits[0]["puuid"])
            except LCUError:
                pass
        try:
            return self.get("/lol-summoner/v1/summoners", params={"name": riot_id.strip()})
        except LCUError:
            return None

    def mastery(self, puuid: str, champion_id: int | None = None) -> list[dict] | dict | None:
        """Champion mastery for any player (the client exposes it under a few different paths by version)."""
        paths = ([f"/lol-champion-mastery/v1/{puuid}/champion-mastery/{champion_id}"] if champion_id else []) + [
            f"/lol-collections/v1/inventories/{puuid}/champion-mastery", f"/lol-champion-mastery/v1/{puuid}/champion-mastery"]
        for path in paths:
            try:
                data = self.get(path)
            except LCUError:
                continue
            if isinstance(data, dict) and data.get("championId"):
                return data
            if isinstance(data, list):
                if champion_id:
                    return next((m for m in data if m.get("championId") == champion_id), None)
                return data
        return None

    def honor_ballot(self) -> dict | None:
        try:
            return self.get("/lol-honor-v2/v1/ballot")
        except LCUError:
            return None

    def honor(self, puuid: str, game_id: int, summoner_id: int | None = None, category: str = "HEART") -> None:
        try:
            self.request("POST", "/lol-honor/v1/honor", json={"honorType": category, "recipientPuuid": puuid})
        except LCUError:  # older clients
            self.request("POST", "/lol-honor-v2/v1/honor-player",
                         json={"honorCategory": category, "summonerId": summoner_id, "puuid": puuid, "gameId": game_id})

    def submit_honor(self) -> None:
        try:
            self.request("POST", "/lol-honor/v1/ballot")
        except LCUError:
            pass

    def champions(self) -> dict[int, dict]:
        """championId -> {name (client language), alias (English id like 'LeeSin')}."""
        if self._champs is None:
            rows = self.get("/lol-game-data/assets/v1/champion-summary.json") or []
            self._champs = {c["id"]: {"name": c.get("name", ""), "alias": c.get("alias", "")} for c in rows if c.get("id", -1) > 0}
        return self._champs

    def champ_name(self, cid: int) -> str:
        return self.champions().get(cid, {}).get("name") or (f"#{cid}" if cid else "")

    def champ_alias(self, cid: int) -> str:
        return self.champions().get(cid, {}).get("alias") or str(cid)


# ---------------------------------------------------------------- match-v4 -> match-v5
def _position(part: dict) -> str:
    tl = part.get("timeline") or {}
    lane, role = (tl.get("lane") or "").upper(), (tl.get("role") or "").upper()
    if SMITE in (part.get("spell1Id"), part.get("spell2Id")) or lane == "JUNGLE":
        return "JUNGLE"
    if lane in ("MIDDLE", "MID"):
        return "MIDDLE"
    if lane == "TOP":
        return "TOP"
    if lane in ("BOTTOM", "BOT"):
        return "UTILITY" if "SUPPORT" in role else "BOTTOM"
    return ""


def to_match_v5(game: dict, timeline: dict | None, lcu: LCU) -> tuple[dict, dict]:
    ids = {pi["participantId"]: pi.get("player") or {} for pi in game.get("participantIdentities", [])}
    parts = []
    for p in game.get("participants", []):
        s = p.get("stats") or {}
        who = ids.get(p["participantId"], {})
        name = who.get("gameName") or who.get("summonerName") or ""
        parts.append({
            **s,
            "participantId": p["participantId"], "teamId": p["teamId"], "championId": p["championId"],
            "championName": lcu.champ_alias(p["championId"]), "championNameLocal": lcu.champ_name(p["championId"]),
            "puuid": who.get("puuid", ""), "riotIdGameName": name, "riotIdTagline": who.get("tagLine", ""),
            "teamPosition": _position(p), "win": bool(s.get("win")),
            "kills": s.get("kills", 0), "deaths": s.get("deaths", 0), "assists": s.get("assists", 0),
            "totalMinionsKilled": s.get("totalMinionsKilled", 0), "neutralMinionsKilled": s.get("neutralMinionsKilled", 0),
            "goldEarned": s.get("goldEarned", 0), "totalDamageDealtToChampions": s.get("totalDamageDealtToChampions", 0),
            "visionScore": s.get("visionScore", 0), "challenges": {},
        })
    teams = [{"teamId": t["teamId"], "win": str(t.get("win", "")).lower() in ("win", "true")} for t in game.get("teams", [])]
    match = {
        "metadata": {"matchId": f"LCU_{game['gameId']}", "source": "lcu"},
        "info": {"gameId": game["gameId"], "queueId": game.get("queueId", 0), "gameVersion": game.get("gameVersion", "0.0"),
                 "gameDuration": game.get("gameDuration", 0), "gameCreation": game.get("gameCreation", 0),
                 "platformId": game.get("platformId", ""), "participants": parts, "teams": teams},
    }
    team_of = {p["participantId"]: p["teamId"] for p in parts}
    frames = []
    for f in (timeline or {}).get("frames", []):
        events = []
        for e in f.get("events", []):
            e = dict(e)
            if e.get("type") == "ELITE_MONSTER_KILL" and "killerTeamId" not in e:
                e["killerTeamId"] = team_of.get(e.get("killerId"), 300)
            if "assistingParticipantIds" not in e:
                e["assistingParticipantIds"] = []
            events.append(e)
        pfs = {}
        for k, pf in (f.get("participantFrames") or {}).items():
            pf = dict(pf)
            for key in ("totalGold", "xp", "minionsKilled", "jungleMinionsKilled", "level"):
                pf.setdefault(key, 0)
            pfs[str(pf.get("participantId", k))] = pf
        frames.append({"timestamp": f.get("timestamp", 0), "participantFrames": pfs, "events": events})
    return match, {"info": {"frames": frames}}


def history_row(g: dict, lcu: LCU) -> dict:
    """One entry of the client's match list (only the queried player's stats are included)."""
    p = (g.get("participants") or [{}])[0]
    s = p.get("stats") or {}
    mins = max(g.get("gameDuration", 0) / 60, 1)
    cs = s.get("totalMinionsKilled", 0) + s.get("neutralMinionsKilled", 0)
    return {
        "match_id": f"LCU_{g['gameId']}", "game_id": g["gameId"], "queue": QUEUES.get(g.get("queueId"), g.get("queueId")),
        "queue_id": g.get("queueId"), "champion": lcu.champ_name(p.get("championId", 0)), "champion_id": p.get("championId", 0),
        "role": _position(p) or "?", "win": bool(s.get("win")), "kda": f"{s.get('kills', 0)}/{s.get('deaths', 0)}/{s.get('assists', 0)}",
        "kills": s.get("kills", 0), "deaths": s.get("deaths", 0), "assists": s.get("assists", 0),
        "cs_per_min": round(cs / mins, 1), "minutes": round(mins, 1), "created": g.get("gameCreation", 0),
        "remake": g.get("gameDuration", 0) < 300,
    }


def live_game() -> dict | None:
    """The game client's own live API (port 2999, no auth): every player's KDA, CS, level, items. None outside a game."""
    try:
        r = requests.get("https://127.0.0.1:2999/liveclientdata/allgamedata", verify=False, timeout=2)
        return r.json() if r.status_code == 200 else None
    except (requests.RequestException, ValueError):
        return None


def scoreboard(game: dict, lcu: LCU) -> dict:
    """Both teams of one finished game: KDA, CS, gold, damage, vision, items, spells, keystone and a 0-10 rating."""
    from lol_review import opgg, playstyle

    st = opgg.static()
    ids = {pi["participantId"]: pi.get("player") or {} for pi in game.get("participantIdentities", [])}
    mins = max(game.get("gameDuration", 0) / 60, 1)
    rows = []
    for p in game.get("participants", []):
        s = p.get("stats") or {}
        who = ids.get(p["participantId"], {})
        icon = lambda table, i: (st[table].get(i) or {}).get("icon", "")  # noqa: E731
        rows.append({
            "participantId": p["participantId"], "teamId": p["teamId"], "puuid": who.get("puuid", ""),
            "name": who.get("gameName") or who.get("summonerName") or "", "tag": who.get("tagLine", ""),
            "champion_id": p["championId"], "champion": lcu.champ_name(p["championId"]), "position": _position(p),
            "level": s.get("champLevel", 0), "win": bool(s.get("win")),
            "kills": s.get("kills", 0), "deaths": s.get("deaths", 0), "assists": s.get("assists", 0),
            "cs": s.get("totalMinionsKilled", 0) + s.get("neutralMinionsKilled", 0), "gold": s.get("goldEarned", 0),
            "damage": s.get("totalDamageDealtToChampions", 0), "taken": s.get("totalDamageTaken", 0),
            "vision": s.get("visionScore", 0), "wards": s.get("wardsPlaced", 0), "pinks": s.get("visionWardsBoughtInGame", 0),
            "items": [{"id": i, "name": (st["items"].get(i) or {}).get("name", ""), "icon": icon("items", i)}
                      for i in (s.get(f"item{k}", 0) for k in range(7)) if i],
            "spells": [icon("spells", p.get("spell1Id")), icon("spells", p.get("spell2Id"))],
            "keystone": icon("runes", s.get("perk0")), "sub_style": icon("runes", s.get("perkSubStyle")),
        })
    scores = playstyle.performance_scores(rows, mins)
    for r in rows:
        r["score"] = scores.get(r["participantId"], 0)
    for team_id in {r["teamId"] for r in rows}:
        team = sorted((r for r in rows if r["teamId"] == team_id), key=lambda r: -r["score"])
        if team:
            team[0]["badge"] = "MVP" if team[0]["win"] else "ACE"
    teams = []
    for t in game.get("teams", []):
        teams.append({"teamId": t["teamId"], "win": str(t.get("win", "")).lower() in ("win", "true"),
                      "kills": sum(r["kills"] for r in rows if r["teamId"] == t["teamId"]),
                      "gold": sum(r["gold"] for r in rows if r["teamId"] == t["teamId"]),
                      "towers": t.get("towerKills", 0), "dragons": t.get("dragonKills", 0), "barons": t.get("baronKills", 0),
                      "heralds": t.get("riftHeraldKills", 0), "inhibitors": t.get("inhibitorKills", 0),
                      "players": [r for r in rows if r["teamId"] == t["teamId"]]})
    return {"game_id": game.get("gameId"), "match_id": f"LCU_{game.get('gameId')}", "queue": QUEUES.get(game.get("queueId"), game.get("queueId")),
            "minutes": round(mins, 1), "created": game.get("gameCreation", 0), "teams": teams,
            "max_damage": max((r["damage"] for r in rows), default=1) or 1}


_default: LCU | None = None


def default() -> LCU:
    global _default
    if _default is None:
        _default = LCU(os.environ.get("LOL_INSTALL_DIR") or None)
    return _default

