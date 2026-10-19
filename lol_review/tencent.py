"""国服 (Tencent) champion data from lol.qq.com, the source the Chinese client's own guides use.

- `recommend(cid)`: per-lane rune pages and summoner spells sorted by how often
  国服 players pick them, with win rates. Used for auto-runes when the rune
  source is 国服.
- `abilities(cid)`: passive and Q/W/E/R with per-rank cooldown, cost and range
  (e.g. "10/9/8/7/6"), for the in-game overlay.

Both endpoints are public but undocumented; every call fails soft. Responses
are cached (recommendations 6 h, abilities 1 day).
"""

from __future__ import annotations

import json
import re
import time

import requests

from lol_review.riot import CACHE

RUNES_URL = "https://lol.qq.com/act/lbp/common/guides/champDetail/champDetail_{cid}.js"
HERO_URL = "https://game.gtimg.cn/images/lol/act/img/js/hero/{cid}.js"
LANES = {"top": "TOP", "jungle": "JUNGLE", "mid": "MIDDLE", "bottom": "BOTTOM", "support": "UTILITY"}
SLOTS = {"passive": "P", "q": "Q", "w": "W", "e": "E", "r": "R"}
_session = requests.Session()


class TencentError(RuntimeError):
    pass


def _get(url: str, key: str, ttl: float) -> dict:
    path = CACHE / f"{key}.json"
    if path.exists() and time.time() - path.stat().st_mtime < ttl:
        return json.loads(path.read_text(encoding="utf-8"))
    try:
        r = _session.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0 RiftLens", "Referer": "https://lol.qq.com/"})
        r.raise_for_status()
        text = r.content.decode("utf-8", errors="replace")
        data = json.loads(text[text.find("{"): text.rfind("}") + 1])  # the rune file is `var X = {...};`
    except (requests.RequestException, ValueError) as e:
        if path.exists():  # stale beats nothing when offline
            return json.loads(path.read_text(encoding="utf-8"))
        raise TencentError(f"读取国服数据失败：{e}") from e
    CACHE.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return data


def _entries(raw: str) -> list[dict]:
    """The lane fields are JSON strings holding {"1": {...}, "2": {...}} or one more level of nesting."""
    try:
        obj = json.loads(raw or "{}")
    except ValueError:
        return []
    out = []
    for v in (obj.values() if isinstance(obj, dict) else obj):
        if isinstance(v, dict) and ("perk" in v or "spellid" in v or "itemid" in v):
            out.append(v)
        elif isinstance(v, dict):
            out.extend(x for x in v.values() if isinstance(x, dict))
    return out


def recommend(cid: int) -> dict:
    """{"version", "lanes": {POSITION: {"games", "win_rate", "runes": [...], "spells": [...]}}} for one champion."""
    from lol_review import opgg

    data = _get(RUNES_URL.format(cid=cid), f"tencent_champ_{cid}", 6 * 3600)
    lanes_raw = ((data.get("list") or {}).get("championLane")) or {}
    st = opgg.static()
    runes_meta = st["runes"]
    lanes, version = {}, ""
    for lane, v in lanes_raw.items():
        pos = LANES.get(lane)
        if not pos or not isinstance(v, dict):
            continue
        version = v.get("gameversion") or version
        pages = []
        for e in _entries(v.get("perkdetail", "")):
            perks = [5001 if p == 0 else p for p in (int(x) for x in str(e.get("perk", "")).split("&") if x.strip().isdigit())][:9]
            if len(perks) != 9:
                continue
            primary = (runes_meta.get(perks[0]) or {}).get("style")
            sub = (runes_meta.get(perks[4]) or {}).get("style")
            if not primary or not sub:
                continue
            pages.append({"primary_style": primary, "sub_style": sub, "perks": perks, "games": int(e.get("igamecnt") or 0),
                          "win_rate": round(int(e.get("winrate") or 0) / 10000, 4),
                          "names": [(runes_meta.get(p) or {}).get("name", str(p)) for p in perks],
                          "icons": [(runes_meta.get(p) or {}).get("icon", "") for p in perks]})
        pages.sort(key=lambda p: -p["games"])
        spells = []
        for e in _entries(v.get("spellidjson", "")):
            ids = [int(x) for x in str(e.get("spellid", "")).split("&") if x.strip().isdigit()]
            if len(ids) == 2:
                spells.append({"ids": ids, "names": [(st["spells"].get(i) or {}).get("name", str(i)) for i in ids],
                               "games": int(e.get("igamecnt") or 0), "win_rate": round(int(e.get("winrate") or 0) / 10000, 4)})
        spells.sort(key=lambda s: -s["games"])
        games = int(v.get("igamecnt") or 0)
        lanes[pos] = {"games": games, "win_rate": round(int(v.get("wincnt") or 0) / games, 4) if games else None,
                      "runes": pages[:3], "spells": spells[:3]}
    return {"version": version, "lanes": lanes, "source": "国服（lol.qq.com）"}


def best(cid: int, position: str = "") -> dict | None:
    """The lane matching `position` (client name, e.g. MIDDLE), else the most played one."""
    lanes = recommend(cid)["lanes"]
    if not lanes:
        return None
    pos = (position or "").upper()
    if pos not in lanes:
        pos = max(lanes, key=lambda k: lanes[k]["games"])
    return {"position": pos, **lanes[pos]}


def _clean(text: str) -> str:
    text = re.sub(r"<br\s*/?>", "\n", text or "")
    return re.sub(r"<[^>]+>", "", text).replace("【", "").replace("】", "").strip()


def abilities(cid: int) -> dict:
    """Champion name and passive/Q/W/E/R with per-rank cooldown, cost and range."""
    data = _get(HERO_URL.format(cid=cid), f"tencent_hero_{cid}", 86400)
    hero = data.get("hero") or {}
    out = []
    for s in data.get("spells") or []:
        slot = SLOTS.get(str(s.get("spellKey", "")).lower())
        if not slot:
            continue
        out.append({"slot": slot, "name": s.get("name", ""), "icon": s.get("abilityIconPath", ""),
                    "cooldown": s.get("cooldownburn") or s.get("cooldown") or "", "cost": s.get("costburn") or s.get("cost") or "",
                    "range": s.get("range") or "", "description": _clean(s.get("description", ""))[:600]})
    out.sort(key=lambda a: "PQWER".index(a["slot"]))
    return {"id": cid, "name": hero.get("name", ""), "title": hero.get("title", ""), "abilities": out}
