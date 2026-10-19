"""OP.GG champion statistics: tier lists and per-champion builds.

Uses OP.GG's public JSON API (the one its website and League Akari read). It
is unofficial and undocumented, so it can change without notice; every call
fails soft with an `error` field. Responses are cached for an hour.

Names and icons come from Data Dragon (zh_CN), so this works without the
League client running.
"""

from __future__ import annotations

import json
import time

import requests

from lol_review.riot import CACHE, DDRAGON

API = "https://lol-api-champion.op.gg/api"
HEADERS = {"User-Agent": "Mozilla/5.0 RiftLens"}
REGIONS = ["global", "kr", "na", "euw", "eune", "jp", "br", "lan", "las", "oce", "tr", "ru", "sg", "tw", "vn", "th", "ph", "me"]
TIERS = ["all", "iron_plus", "bronze_plus", "silver_plus", "gold_plus", "platinum_plus", "emerald_plus", "diamond_plus", "master_plus", "grandmaster", "challenger"]
MODES = ["ranked", "aram"]
POSITIONS = ["TOP", "JUNGLE", "MID", "ADC", "SUPPORT"]
# OP.GG position names <-> the client's / match-v5 names
FROM_CLIENT = {"TOP": "TOP", "JUNGLE": "JUNGLE", "MIDDLE": "MID", "MID": "MID", "BOTTOM": "ADC", "ADC": "ADC", "UTILITY": "SUPPORT", "SUPPORT": "SUPPORT"}


class OPGGError(RuntimeError):
    pass


_session = requests.Session()


def _cached(url: str, key: str, ttl: float) -> dict | list:
    path = CACHE / f"{key}.json"
    if path.exists() and time.time() - path.stat().st_mtime < ttl:
        return json.loads(path.read_text(encoding="utf-8"))
    try:
        r = _session.get(url, headers=HEADERS, timeout=15)
    except requests.RequestException as e:
        if path.exists():  # stale beats nothing when offline
            return json.loads(path.read_text(encoding="utf-8"))
        raise OPGGError(f"连接 OP.GG 失败：{e}") from e
    if r.status_code != 200:
        raise OPGGError(f"OP.GG 返回 {r.status_code}")
    data = r.json()
    CACHE.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return data


def _first(*vals):
    """First value that is not None (OP.GG tier 0 means the OP tier, so `or` would drop it)."""
    return next((v for v in vals if v is not None), None)


def _check(region: str, tier: str, mode: str) -> None:
    if region not in REGIONS or tier not in TIERS or mode not in MODES:
        raise OPGGError("参数不合法")


# ---------------------------------------------------------------- static data (Data Dragon, zh_CN)
def version() -> str:
    return _cached(f"{DDRAGON}/api/versions.json", "ddragon_versions", 86400)[0]


def static() -> dict:
    """Champions, items, runes and summoner spells keyed by numeric id, with icon URLs."""
    v = version()
    base = f"{DDRAGON}/cdn/{v}"
    champs = _cached(f"{base}/data/zh_CN/champion.json", f"champs_zh_CN_{v}", 7 * 86400)["data"]
    items = _cached(f"{base}/data/zh_CN/item.json", f"items_zh_CN_{v}", 7 * 86400)["data"]
    spells = _cached(f"{base}/data/zh_CN/summoner.json", f"spells_zh_CN_{v}", 7 * 86400)["data"]
    styles = _cached(f"{base}/data/zh_CN/runesReforged.json", f"runes_zh_CN_{v}", 7 * 86400)
    runes = {}
    for st in styles:
        runes[st["id"]] = {"name": st["name"], "icon": f"{DDRAGON}/cdn/img/{st['icon']}"}
        for slot in st["slots"]:
            for r in slot["runes"]:
                runes[r["id"]] = {"name": r["name"], "icon": f"{DDRAGON}/cdn/img/{r['icon']}", "style": st["id"]}
    for sid, name in {5001: "生命值成长", 5002: "护甲", 5003: "魔抗", 5005: "攻击速度", 5007: "技能急速", 5008: "适应之力",
                      5010: "移动速度", 5011: "生命值", 5013: "韧性与减速抗性"}.items():
        runes.setdefault(sid, {"name": name, "icon": ""})
    return {
        "version": v,
        "champions": {int(c["key"]): {"name": c["name"], "title": c["title"], "alias": c["id"], "icon": f"{base}/img/champion/{c['image']['full']}"} for c in champs.values()},
        "items": {int(k): {"name": it["name"], "icon": f"{base}/img/item/{k}.png", "gold": it.get("gold", {}).get("total")} for k, it in items.items()},
        "spells": {int(s["key"]): {"name": s["name"], "icon": f"{base}/img/spell/{s['image']['full']}"} for s in spells.values()},
        "runes": runes,
    }


def champion_id(name: str) -> int | None:
    """Resolve a Chinese name, title, English id or numeric id."""
    name = name.strip()
    if name.isdigit():
        return int(name)
    for cid, c in static()["champions"].items():
        if name.lower() in (c["name"].lower(), c["alias"].lower(), c["title"].lower()):
            return cid
    return None


# ---------------------------------------------------------------- API
def tier_list(region: str = "global", tier: str = "emerald_plus", mode: str = "ranked") -> dict:
    """One row per champion and position, sorted by OP.GG's rank within the position."""
    _check(region, tier, mode)
    q = f"?tier={tier}" if mode == "ranked" else ""
    data = _cached(f"{API}/{region}/champions/{mode}{q}", f"opgg_tiers_{region}_{mode}_{tier}", 3600)
    st = static()
    rows = []
    for c in data.get("data", []):
        info = st["champions"].get(c["id"], {"name": f"#{c['id']}", "alias": "", "icon": ""})
        groups = [(p["name"], p["stats"]) for p in c.get("positions") or []] or [("NONE", c.get("average_stats") or {})]
        for pos, s in groups:
            td = s.get("tier_data") or {}
            if not s.get("play"):
                continue
            rows.append({"id": c["id"], "name": info["name"], "alias": info["alias"], "icon": info["icon"], "position": pos,
                         "tier": _first(td.get("tier"), s.get("tier")), "rank": _first(td.get("rank"), s.get("rank")),
                         "rank_prev": td.get("rank_prev"), "win_rate": s.get("win_rate"), "pick_rate": s.get("pick_rate"),
                         "ban_rate": s.get("ban_rate"), "role_rate": s.get("role_rate"), "kda": s.get("kda"), "play": s.get("play")})
    rows.sort(key=lambda r: (r["position"], r["rank"] or 999))
    return {"version": (data.get("meta") or {}).get("version"), "analyzed_at": (data.get("meta") or {}).get("analyzed_at"),
            "region": region, "tier": tier, "mode": mode, "rows": rows}


def champion(cid: int, position: str = "", region: str = "global", tier: str = "emerald_plus", mode: str = "ranked") -> dict:
    """Builds for one champion: summary, spells, runes, items, skill order, matchups."""
    _check(region, tier, mode)
    st = static()
    position = FROM_CLIENT.get(position.upper(), position.upper())
    if mode == "aram":
        position = "NONE"
    elif position not in POSITIONS:  # pick the champion's main position
        summary = _cached(f"{API}/{region}/champions/{mode}?tier={tier}", f"opgg_tiers_{region}_{mode}_{tier}", 3600)
        c = next((x for x in summary.get("data", []) if x["id"] == cid), None)
        if not c or not c.get("positions"):
            raise OPGGError("OP.GG 没有这个英雄的数据")
        position = max(c["positions"], key=lambda p: p["stats"].get("play", 0))["name"]
    q = f"?tier={tier}" if mode == "ranked" else ""
    d = _cached(f"{API}/{region}/champions/{mode}/{cid}/{position}{q}", f"opgg_champ_{region}_{mode}_{tier}_{cid}_{position}", 3600).get("data") or {}
    if not d:
        raise OPGGError("OP.GG 没有这个英雄在该位置的数据")

    def rate(x: dict) -> dict:
        return {"play": x.get("play"), "win_rate": round(x["win"] / x["play"], 4) if x.get("play") else None, "pick_rate": x.get("pick_rate")}

    def items(rows: list, n: int) -> list:
        return [{"items": [{"id": i, **st["items"].get(i, {"name": f"#{i}", "icon": ""})} for i in r["ids"]], **rate(r)} for r in rows[:n]]

    summary = d.get("summary") or {}
    pos_stats = next((p["stats"] for p in summary.get("positions") or [] if p["name"] == position), summary.get("average_stats") or {})
    rune_pages = []
    for r in (d.get("runes") or [])[:3]:
        rune_pages.append({
            "primary_style": r["primary_page_id"], "sub_style": r["secondary_page_id"],
            "perks": r["primary_rune_ids"] + r["secondary_rune_ids"] + r["stat_mod_ids"],
            "primary": [{"id": i, **st["runes"].get(i, {"name": f"#{i}", "icon": ""})} for i in r["primary_rune_ids"]],
            "secondary": [{"id": i, **st["runes"].get(i, {"name": f"#{i}", "icon": ""})} for i in r["secondary_rune_ids"]],
            "shards": [{"id": i, **st["runes"].get(i, {"name": f"#{i}", "icon": ""})} for i in r["stat_mod_ids"]],
            "styles": [st["runes"].get(r["primary_page_id"], {}), st["runes"].get(r["secondary_page_id"], {})],
            **rate(r)})
    counters = []
    for c in d.get("counters") or []:
        if c.get("play", 0) < 100:
            continue
        info = st["champions"].get(c["champion_id"], {"name": f"#{c['champion_id']}", "icon": ""})
        counters.append({"id": c["champion_id"], "name": info["name"], "icon": info["icon"], "play": c["play"], "win_rate": round(c["win"] / c["play"], 4)})
    counters.sort(key=lambda c: c["win_rate"])
    info = st["champions"].get(cid, {"name": f"#{cid}", "title": "", "alias": "", "icon": ""})
    td = pos_stats.get("tier_data") or {}
    return {
        "id": cid, **info, "position": position, "region": region, "tier_filter": tier, "mode": mode,
        "positions": [p["name"] for p in summary.get("positions") or []],
        "stats": {"tier": _first(td.get("tier"), pos_stats.get("tier")), "rank": _first(td.get("rank"), pos_stats.get("rank")),
                  "win_rate": pos_stats.get("win_rate"), "pick_rate": pos_stats.get("pick_rate"), "ban_rate": pos_stats.get("ban_rate"),
                  "role_rate": pos_stats.get("role_rate"), "kda": pos_stats.get("kda"), "play": pos_stats.get("play")},
        "spells": [{"spells": [{"id": i, **st["spells"].get(i, {"name": f"#{i}", "icon": ""})} for i in s["ids"]], **rate(s)} for s in (d.get("summoner_spells") or [])[:2]],
        "runes": rune_pages,
        "starter_items": items(d.get("starter_items") or [], 2),
        "boots": items(d.get("boots") or [], 2),
        "core_items": items(d.get("core_items") or [], 4),
        "last_items": items(d.get("last_items") or [], 8),
        "skills": [{"order": s["order"], **rate(s)} for s in (d.get("skills") or [])[:2]],
        "skill_priority": [{"order": s["ids"], **rate(s)} for s in (d.get("skill_masteries") or [])[:1]],
        "counters": {"hard": counters[:6], "easy": counters[::-1][:6]}, "counters_all": counters,
        "source": "OP.GG", "note": "对位胜率是该英雄对阵对方时的胜率；样本少于 100 局的对位已省略。",
    }


# ---------------------------------------------------------------- summoner profiles (overseas servers only)
SUMMONER_API = "https://lol-api-summoner.op.gg/api"
WEB_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) RiftLens", "Accept": "application/json",
               "Origin": "https://op.gg", "Referer": "https://op.gg/"}
ROMAN = {1: "I", 2: "II", 3: "III", 4: "IV"}


def _summoner_get(path: str, key: str | None, ttl: float):
    url = SUMMONER_API + path
    if key:
        p = CACHE / f"{key}.json"
        if p.exists() and time.time() - p.stat().st_mtime < ttl:
            return json.loads(p.read_text(encoding="utf-8"))
    try:
        r = _session.get(url, headers=WEB_HEADERS, timeout=15)
    except requests.RequestException as e:
        raise OPGGError(f"连接 OP.GG 失败：{e}") from e
    if r.status_code == 404:
        return None
    if r.status_code != 200:
        raise OPGGError(f"OP.GG 返回 {r.status_code}")
    data = r.json().get("data")
    if key:
        CACHE.mkdir(parents=True, exist_ok=True)
        (CACHE / f"{key}.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return data


def summoner_id(region: str, game_name: str, tag: str) -> str | None:
    if region not in REGIONS or region == "global":
        raise OPGGError(f"OP.GG 不支持这个服务器（{region}），国服账号在 OP.GG 上查不到")
    from urllib.parse import quote

    key = "opgg_sid_" + "".join(c if c.isalnum() else "_" for c in f"{region}_{game_name}_{tag}".lower())
    rows = _summoner_get(f"/v3/{region}/summoners?riot_id={quote(game_name + '#' + tag)}&hl=en_US", key, 7 * 86400) or []
    return rows[0]["summoner_id"] if rows else None


def _tier(t: dict | None) -> dict | None:
    if not t or not t.get("tier"):
        return None
    div = ROMAN.get(t.get("division"), "") if t["tier"] not in ("MASTER", "GRANDMASTER", "CHALLENGER") else ""
    return {"tier": t["tier"], "division": div, "lp": t.get("lp") or 0}


def _season_names() -> dict[int, str]:
    rows = _summoner_get("/meta/seasons?hl=en_US", "opgg_seasons", 7 * 86400) or []
    names = {}
    for s in rows:
        label = str(s.get("display_value"))
        if s.get("split"):
            label += f" 第{s['split']}赛段"
        names[s["id"]] = label
    return names


def summoner_history(region: str, game_name: str, tag: str) -> dict:
    """Past-season ranks and the LP points OP.GG has stored for one overseas account (10-minute cache)."""
    sid = summoner_id(region, game_name, tag)
    if not sid:
        raise OPGGError("OP.GG 上还没有这个账号，在 OP.GG 网站搜索一次后就会收录")
    safe = "".join(c if c.isalnum() else "_" for c in sid)[:60]
    summary = (_summoner_get(f"/{region}/summoners/{sid}/summary", f"opgg_summary_{safe}", 600) or {}).get("summoner") or {}
    points = _summoner_get(f"/{region}/summoners/{sid}/lp-histories", f"opgg_lp_{safe}", 600) or []
    names = _season_names()
    seasons = []
    for s in summary.get("previous_seasons") or []:
        t = _tier(s.get("tier_info"))
        if t:
            seasons.append({"season": names.get(s.get("season_id"), f"赛季 {s.get('season_id')}"), "season_id": s.get("season_id"), **t})
    seasons.sort(key=lambda s: -(s["season_id"] or 0))
    from datetime import datetime

    lp_points = []
    for p in points:
        t = _tier(p.get("tier_info"))
        if t and p.get("created_at"):
            lp_points.append({"t": datetime.fromisoformat(p["created_at"]).timestamp(), **t, "source": "opgg"})
    return {"summoner_id": sid, "region": region, "url": f"https://op.gg/lol/summoners/{region}/{quote_riot(game_name, tag)}",
            "previous_seasons": seasons, "lp_points": lp_points, "updated_at": summary.get("updated_at")}


def quote_riot(game_name: str, tag: str) -> str:
    from urllib.parse import quote

    return quote(f"{game_name}-{tag}")


def renew(region: str, game_name: str, tag: str) -> dict:
    """Ask OP.GG to refresh the profile (same as its "Update" button); OP.GG stores an LP point each time."""
    sid = summoner_id(region, game_name, tag)
    if not sid:
        raise OPGGError("OP.GG 上还没有这个账号")
    try:
        r = _session.post(f"{SUMMONER_API}/{region}/summoners/{sid}/renewal", headers=WEB_HEADERS, timeout=20)
    except requests.RequestException as e:
        raise OPGGError(f"连接 OP.GG 失败：{e}") from e
    safe = "".join(c if c.isalnum() else "_" for c in sid)[:60]
    for k in (f"opgg_summary_{safe}", f"opgg_lp_{safe}"):  # next read fetches fresh data
        (CACHE / f"{k}.json").unlink(missing_ok=True)
    return {"status": r.status_code, **(r.json() if "json" in r.headers.get("Content-Type", "") else {})}
