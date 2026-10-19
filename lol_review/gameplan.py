"""Pre-game plan: "how should I play this game, and what should I build".

Built in two steps so it is fast enough for champ select and the loading screen:
1. Deterministic (≈1-2 s): team compositions (AD/AP mix, healing, hard CC,
   tanks), enemy roles, my lane opponent and the OP.GG matchup win rate, the
   weakest / strongest players from their recent stats, premades, and an item
   build: OP.GG's best build adjusted for the enemy composition. The build can
   be written into the client as an item set, so it shows in the in-game shop.
2. Optional, one model call (≈10 s): turns the facts above into a short plan.

Plans are produced before the game starts only (champ select / loading screen);
in game they are displayed, never updated with live decisions, which keeps the
feature within Riot's third-party rules on real-time in-game advice.
"""

from __future__ import annotations

import json
import os
import subprocess
from itertools import permutations

from lol_review import opgg

POS_ZH = {"TOP": "上单", "JUNGLE": "打野", "MIDDLE": "中单", "BOTTOM": "下路", "UTILITY": "辅助"}
OPGG_POS = {"TOP": "TOP", "JUNGLE": "JUNGLE", "MID": "MIDDLE", "ADC": "BOTTOM", "SUPPORT": "UTILITY"}
HEALERS = {"Aatrox", "Soraka", "Warwick", "Vladimir", "DrMundo", "Sylas", "Yuumi", "Fiddlesticks", "Swain", "Nidalee", "Sona",
           "Seraphine", "Olaf", "Briar", "Trundle", "Illaoi", "Volibear", "Gwen", "Senna", "Milio", "Nami", "Kayn", "Maokai", "Sett"}
HARD_CC = {"Leona", "Nautilus", "Amumu", "Sejuani", "Lissandra", "Morgana", "Thresh", "Rell", "Maokai", "Ashe", "Zyra", "Rakan",
           "Alistar", "Malphite", "Skarner", "Vi", "Warwick", "Galio", "Annie", "Neeko", "Sett", "Zac", "Ornn", "JarvanIV", "Braum",
           "Poppy", "Blitzcrank", "Pyke", "Veigar", "Cassiopeia", "Lux", "Nami", "Taric", "Gragas", "Rammus", "Nunu", "Kennen"}
# item ids (Data Dragon, current patch); missing ids are skipped at runtime
ANTI_HEAL = {"ad_carry": 3033, "ad_bruiser": 6609, "ap": 3165, "tank": 3075, "enchanter": 3916}
PEN = {"ad_carry": 3036, "ad_bruiser": 3071, "ap": 3135}
VS_AP = {"ad_carry": 3156, "ad_bruiser": 3156, "ap": 3102, "tank": 4401}
VS_AD = {"ad_carry": 3026, "ad_bruiser": 3026, "ap": 3157, "tank": 3110}
MERCURY, PLATED = 3111, 3047


# ---------------------------------------------------------------- champion traits
def _ddragon_champs() -> dict[int, dict]:
    v = opgg.version()
    data = opgg._cached(f"{opgg.DDRAGON}/cdn/{v}/data/zh_CN/champion.json", f"champs_zh_CN_{v}", 7 * 86400)["data"]
    return {int(c["key"]): c for c in data.values()}


def traits(cid: int) -> dict:
    c = _ddragon_champs().get(cid) or {}
    tags, info, alias = c.get("tags") or [], c.get("info") or {}, c.get("id", "")
    atk, mag = info.get("attack", 5), info.get("magic", 5)
    dmg = "AP" if mag > atk + 1 else "AD" if atk > mag + 1 else "混合"
    return {"id": cid, "name": c.get("name", str(cid)), "alias": alias, "tags": tags, "damage": dmg,
            "tank": "Tank" in tags[:1] or info.get("defense", 0) >= 8, "healer": alias in HEALERS, "hard_cc": alias in HARD_CC,
            "assassin": "Assassin" in tags}


def kind(t: dict) -> str:
    tags = t["tags"]
    if t["tank"] and "Marksman" not in tags:
        return "tank"
    if "Support" in tags[:1] and t["damage"] != "AD":
        return "enchanter"
    if "Marksman" in tags[:1]:
        return "ad_carry"
    return "ap" if t["damage"] == "AP" else "ad_bruiser"


def comp(cids: list[int]) -> dict:
    ts = [traits(c) for c in cids if c]
    return {"champions": [t["name"] for t in ts], "ad": sum(t["damage"] == "AD" for t in ts), "ap": sum(t["damage"] == "AP" for t in ts),
            "mixed": sum(t["damage"] == "混合" for t in ts), "tanks": [t["name"] for t in ts if t["tank"]],
            "healers": [t["name"] for t in ts if t["healer"]], "hard_cc": [t["name"] for t in ts if t["hard_cc"]],
            "assassins": [t["name"] for t in ts if t["assassin"]]}


def guess_roles(cids: list[int], known: dict[int, str] | None = None) -> dict[int, str]:
    """Most likely position for each enemy champion: the assignment with the highest total OP.GG role share."""
    known = {c: p for c, p in (known or {}).items() if p in POS_ZH}
    cids = [c for c in cids if c]
    try:
        rows = opgg.tier_list()["rows"]
    except Exception:
        rows = []
    share: dict[tuple[int, str], float] = {}
    for r in rows:
        pos = OPGG_POS.get(r.get("position"))
        if pos:
            share[(r["id"], pos)] = max(share.get((r["id"], pos), 0), r.get("role_rate") or 0)
    free = [p for p in POS_ZH if p not in known.values()]
    todo = [c for c in cids if c not in known]
    best, best_score = {}, -1.0
    for perm in permutations(free, len(todo)) if len(todo) <= len(free) else []:
        s = sum(share.get((c, p), 0) for c, p in zip(todo, perm))
        if s > best_score:
            best, best_score = dict(zip(todo, perm)), s
    return {**known, **best}


# ---------------------------------------------------------------- items
def _item(i: int) -> dict | None:
    it = opgg.static()["items"].get(i)
    return {"id": i, "name": it["name"], "icon": it["icon"]} if it else None


def build(cid: int, position: str, enemy_cids: list[int]) -> dict:
    """OP.GG's build for my champion, adjusted for the enemy composition. Every adjustment says why."""
    me = traits(cid)
    k = kind(me)
    en = comp(enemy_cids)
    try:
        d = opgg.champion(cid, position)
    except Exception as e:
        d = {"starter_items": [], "core_items": [], "boots": [], "last_items": [], "error": str(e)}
    first = lambda rows: rows[0]["items"] if rows else []  # noqa: E731
    starter, core = first(d.get("starter_items") or []), first(d.get("core_items") or [])
    boots = [b["items"][0] for b in (d.get("boots") or [])[:2] if b.get("items")]
    adjust = []

    def add(item_id, why):
        it = _item(item_id) if item_id else None
        if it and all(a["item"]["id"] != item_id for a in adjust) and all(x["id"] != item_id for x in core):
            adjust.append({"item": it, "why": why})

    n = len([c for c in enemy_cids if c])
    if en["healers"] and k in ANTI_HEAL:
        add(ANTI_HEAL[k], f"对面有回复：{'、'.join(en['healers'])}，第二、三件出重伤")
    if len(en["tanks"]) >= 2 and k in PEN:
        add(PEN[k], f"对面坦克多：{'、'.join(en['tanks'])}，需要穿透")
    if en["ap"] >= 3 and k in VS_AP:
        add(VS_AP[k], f"对面 {en['ap']} 个魔法伤害，魔抗收益高")
    if (en["ad"] >= 4 or len(en["assassins"]) >= 2) and k in VS_AD:
        add(VS_AD[k], f"对面 {en['ad']} 个物理伤害" + (f"、刺客 {'、'.join(en['assassins'])}" if en["assassins"] else ""))
    boot_note = None
    if k != "enchanter" or n:
        if (en["ap"] >= 3 and en["ad"] <= 2) or len(en["hard_cc"]) >= 3:
            boot_note = (_item(MERCURY), f"对面魔法伤害 {en['ap']} 个、硬控 {len(en['hard_cc'])} 个（{'、'.join(en['hard_cc'][:4])}）")
        elif en["ad"] >= 4 and "Marksman" not in me["tags"][:1]:
            boot_note = (_item(PLATED), f"对面 {en['ad']} 个物理伤害")
    if boot_note and boot_note[0]:
        boots = [boot_note[0]] + [b for b in boots if b["id"] != boot_note[0]["id"]]
    late = [x for b in (d.get("last_items") or [])[:4] for x in b.get("items", [])][:6]
    return {"champion": me["name"], "position": d.get("position") or position, "kind": k, "starter": starter, "core": core,
            "boots": boots, "boots_why": boot_note[1] if boot_note and boot_note[0] else None, "adjust": adjust, "late": late,
            "enemy": en, "source": "OP.GG（全球 翡翠+）" + ("" if not d.get("error") else f"，读取失败：{d['error']}")}


def item_set_blocks(b: dict) -> list[dict]:
    blk = lambda title, items: {"type": title[:60], "items": [{"id": str(i["id"]), "count": 1} for i in items if i],  # noqa: E731
                                "showIfSummonerSpell": "", "hideIfSummonerSpell": ""}
    out = []
    if b["starter"]:
        out.append(blk("出门装（OP.GG）", b["starter"]))
    if b["core"]:
        out.append(blk("核心装（OP.GG 胜率最高）", b["core"]))
    if b["boots"]:
        out.append(blk("鞋子" + ("（按对面阵容）" if b["boots_why"] else ""), b["boots"]))
    if b["adjust"]:
        out.append(blk("针对这把阵容：" + " / ".join(a["item"]["name"] for a in b["adjust"]), [a["item"] for a in b["adjust"]]))
    if b["late"]:
        out.append(blk("后期可选（OP.GG）", b["late"]))
    return out


# ---------------------------------------------------------------- the plan
def _player_line(p: dict) -> dict:
    c = p.get("card") or {}
    solo = c.get("solo") or {}
    return {"champion": p.get("champion"), "position": POS_ZH.get(p.get("position", ""), ""), "name": c.get("name") or p.get("name") or "",
            "rank": solo.get("text"), "recent": f"{c.get('wins')}/{c.get('games')} 胜" if c.get("games") else None, "kda": c.get("kda"),
            "score": c.get("score"), "tags": [t["text"] for t in (c.get("tags") or [])][:6],
            "mastery": (p.get("mastery") or {}).get("level"), "premade": p.get("premade"),
            "note": " · ".join(filter(None, [*((p.get("note") or {}).get("tags") or []), (p.get("note") or {}).get("note")])) or None}


def context(state: dict) -> dict | None:
    """Everything the plan is based on, from the assistant's champ-select or in-game state."""
    ig, cs = state.get("in_game"), state.get("champ_select")
    me_puuid = (state.get("me") or {}).get("puuid")
    if ig and ig.get("teams"):
        mine = ig.get("my_team", 0)
        allies, enemies = ig["teams"][mine], ig["teams"][1 - mine]
        stage = "game"
    elif cs:
        allies, enemies = cs["my_team"], cs["their_team"]
        stage = "champ_select"
    else:
        return None
    me = next((p for p in allies if p.get("me") or p.get("puuid") == me_puuid), None)
    if not me or not me.get("champion_id"):
        return None
    pos = me.get("position") or (cs or {}).get("my_position") or ""
    pos = {"MID": "MIDDLE", "BOT": "BOTTOM", "ADC": "BOTTOM", "SUPPORT": "UTILITY"}.get(pos, pos)
    enemy_cids = [p.get("champion_id") for p in enemies if p.get("champion_id")]
    roles = guess_roles(enemy_cids, {p["champion_id"]: p.get("position") for p in enemies if p.get("champion_id") and p.get("position")})
    opp = next((c for c, r in roles.items() if r == pos), None)
    matchup = None
    if opp:
        try:
            d = opgg.champion(me["champion_id"], pos)
            hit = next((x for x in d.get("counters_all") or d["counters"]["hard"] + d["counters"]["easy"] if x["id"] == opp), None)
            matchup = {"vs": traits(opp)["name"], "win_rate": hit["win_rate"] if hit else None, "games": hit["play"] if hit else None}
        except Exception:
            matchup = {"vs": traits(opp)["name"], "win_rate": None}
    elines = []
    for p in enemies:
        line = _player_line({**p, "position": p.get("position") or roles.get(p.get("champion_id"), "")})
        elines.append(line)
    alines = [_player_line(p) | {"me": bool(p.get("me"))} for p in allies]
    scored = [x for x in elines if x["score"] is not None]
    return {
        "stage": stage, "me": {"champion": me.get("champion"), "champion_id": me["champion_id"], "position": pos, "position_zh": POS_ZH.get(pos, "")},
        "matchup": matchup, "allies": alines, "enemies": elines,
        "ally_comp": comp([p.get("champion_id") for p in allies]), "enemy_comp": comp(enemy_cids),
        "weakest_enemy": min(scored, key=lambda x: x["score"]) if scored else None,
        "strongest_enemy": max(scored, key=lambda x: x["score"]) if scored else None,
        "enemy_cids": enemy_cids, "enemies_known": len(enemy_cids),
    }


def rules(ctx: dict, b: dict | None) -> list[dict]:
    """Plan without a model: short tips, each with the data it comes from."""
    out = []
    m, me = ctx.get("matchup"), ctx["me"]
    if m and m.get("win_rate") is not None:
        wr = m["win_rate"]
        feel = "优势对线，可以主动换血压线" if wr >= 0.52 else "劣势对线，稳住补刀、等打野" if wr <= 0.48 else "五五开，看操作和节奏"
        out.append({"k": "对线", "t": f"{me['champion']} 对 {m['vs']}：OP.GG 对位胜率 {wr:.1%}（{m['games']} 局），{feel}。"})
    elif m:
        out.append({"k": "对线", "t": f"对位大概率是 {m['vs']}，OP.GG 样本不足，没有对位胜率。"})
    opp = next((e for e in ctx["enemies"] if m and e["champion"] == m["vs"]), None)
    if opp and (opp["tags"] or opp["mastery"]):
        bits = []
        if opp["mastery"] and opp["mastery"] >= 7:
            bits.append(f"他这个英雄熟练度 M{opp['mastery']}，是熟手")
        bad = [t for t in opp["tags"] if t in ("莽夫", "决斗弱势", "恐补症", "对线乏力", "早已阵亡") or "连败" in t]
        if bad:
            bits.append(f"近期标签「{'、'.join(bad)}」，可以针对")
        if bits:
            out.append({"k": "对线", "t": f"对线的 {opp['name'] or opp['champion']}：" + "；".join(bits) + "。"})
    w = ctx.get("weakest_enemy")
    if w and ctx["stage"] == "game":
        out.append({"k": "节奏", "t": f"对面最弱的一路：{w['position']} {w['champion']}（{w['name']}，侦察分 {w['score']:,}"
                    + (f"，{'、'.join(w['tags'][:3])}" if w["tags"] else "") + "），可以多去这边、让打野帮忙。"})
    s = ctx.get("strongest_enemy")
    if s and ctx["stage"] == "game" and s is not w:
        out.append({"k": "注意", "t": f"对面最强的是 {s['position']} {s['champion']}（{s['name']}，侦察分 {s['score']:,}"
                    + (f"，{'、'.join(s['tags'][:3])}" if s["tags"] else "") + "），别让他拿到早期人头。"})
    groups: dict[int, list[str]] = {}
    for e in ctx["enemies"]:
        if e.get("premade"):
            groups.setdefault(e["premade"], []).append(f"{e['position']}{e['champion']}")
    for g in groups.values():
        out.append({"k": "注意", "t": f"对面开黑：{'、'.join(g)}，他们常一起行动，单走时小心包夹。"})
    for e in ctx["enemies"]:
        if e.get("note"):
            out.append({"k": "备注", "t": f"你之前给 {e['name']}（{e['champion']}）写过：{e['note']}"})
    ec, ac = ctx["enemy_comp"], ctx["ally_comp"]
    if ctx["enemies_known"]:
        dmg = f"对面 {ec['ad']} 物理 / {ec['ap']} 魔法" + (f" / {ec['mixed']} 混合" if ec["mixed"] else "")
        extra = []
        if ec["hard_cc"]:
            extra.append(f"硬控：{'、'.join(ec['hard_cc'][:4])}")
        if ec["tanks"]:
            extra.append(f"坦克：{'、'.join(ec['tanks'])}")
        out.append({"k": "阵容", "t": dmg + ("；" + "；".join(extra) if extra else "") + "。"})
        if len(ac["tanks"]) == 0 and len(ec["tanks"]) >= 2:
            out.append({"k": "团战", "t": "我方没有坦克、对面有前排：别正面硬冲，找侧翼和拉扯的机会。"})
        if len(ec["hard_cc"]) >= 3:
            out.append({"k": "团战", "t": "对面控制多，站位靠后、留好位移和闪现，别先手进场。"})
    if b:
        tips = [f"{a['item']['name']}（{a['why']}）" for a in b["adjust"]]
        if b.get("boots_why") and b["boots"]:
            tips.insert(0, f"鞋子换成{b['boots'][0]['name']}（{b['boots_why']}）")
        core = " → ".join(i["name"] for i in b["core"])
        out.append({"k": "出装", "t": (f"核心：{core}。" if core else "") + ("针对阵容：" + "；".join(tips) + "。" if tips else "对面阵容没有特别需要针对的，按核心出装即可。")})
    return out


PROMPT = """你是英雄联盟教练。下面是这把对局开始前能拿到的全部数据（JSON）和按规则得出的要点。
请用中文写一份赛前计划，不超过 10 行，分成：对线、前 15 分钟节奏、团战、出装 四部分，每部分 1–3 条可以直接照做的建议。
要求：只根据数据说话，不要编造对手的具体操作习惯；对位胜率、侦察分等数字可以引用；
不要写游戏进行中的实时指令（例如“现在去打龙”），只写开局前的计划。

数据：
{ctx}

规则要点：
{rules}"""


def write_with_model(ctx: dict, tips: list[dict], b: dict | None) -> str:
    """One model call: the configured OpenAI-compatible LLM, else the user's Claude Code (claude -p, no tools)."""
    slim = {k: v for k, v in ctx.items() if k not in ("enemy_cids",)}
    if b:
        slim["build"] = {"core": [i["name"] for i in b["core"]], "boots": [i["name"] for i in b["boots"]],
                         "adjust": [{"item": a["item"]["name"], "why": a["why"]} for a in b["adjust"]]}
    prompt = PROMPT.format(ctx=json.dumps(slim, ensure_ascii=False), rules="\n".join(f"- [{t['k']}] {t['t']}" for t in tips))
    base, key, model = (os.environ.get(k, "").strip() for k in ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL"))
    if base and key and model:
        from openai import OpenAI

        r = OpenAI(base_url=base, api_key=key).chat.completions.create(
            model=model, messages=[{"role": "user", "content": prompt}], temperature=0.3, timeout=60)
        return r.choices[0].message.content or ""
    from web.headless_worker import NO_WINDOW, claude_path

    exe = claude_path()
    if not exe:
        raise RuntimeError("没有配置模型，也没有找到 Claude Code")
    from lol_review.riot import STATE

    STATE.mkdir(parents=True, exist_ok=True)
    out = subprocess.run([exe, "-p", prompt, "--model", os.environ.get("PLAN_MODEL", "sonnet"), "--max-turns", "1", "--output-format", "json"],
                         capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120, cwd=str(STATE),
                         stdin=subprocess.DEVNULL, creationflags=NO_WINDOW)
    try:
        return json.loads(out.stdout).get("result") or ""
    except ValueError:
        raise RuntimeError((out.stderr or out.stdout or "Claude Code 没有返回内容")[:300])
