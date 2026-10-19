"""Client assistant: League Akari-style helpers on top of the local client API.

A background thread polls the client once a second and:
- accepts the ready check (auto-accept);
- hovers/locks picks and bans from the user's priority lists (auto pick/ban);
- looks up every visible player in champ select and in game: rank, recent
  record, streak, main champions, and simple tags;
- notices when a game ends so the page can start a RiftLens review of it,
  and optionally honors a teammate (auto-honor);
- in game, reads the game's own live API for KDA / CS / item gold, marks
  premade groups, and computes a scout score for the overlay.

Everything here is deterministic. Settings live in data/client_settings.json.
"""

from __future__ import annotations

import json
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

from lol_review import notes, playstyle
from lol_review.lcu import LCU, LCUError, QUEUES, history_row, live_game
from lol_review.riot import STATE

SETTINGS_FILE = STATE / "data" / "client_settings.json"
RUNE_CONFIGS = STATE / "data" / "rune_configs.json"
POSITIONS = ("TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY")
TONES = ("objective", "roast", "praise")
DEFAULTS = {
    "auto_accept": True, "accept_delay": 1.0,
    "auto_pick": False, "pick": [], "pick_by_pos": {}, "pick_delay": 0.0,
    "auto_ban": False, "ban": [], "ban_by_pos": {}, "ban_delay": 0.0, "lock_in": True,
    "query_players": True, "auto_review": True, "history_count": 20, "review_tone": "objective",
    "auto_runes": False, "auto_spells": False, "flash_on_f": False, "rune_source": "opgg",
    "opgg_region": "global", "opgg_tier": "emerald_plus",
    "auto_honor": False, "overlay_hotkey": True, "overlay_auto": False,
    "game_plan": True, "plan_ai": True, "auto_items": True,
}
TIERS = {"IRON": "黑铁", "BRONZE": "青铜", "SILVER": "白银", "GOLD": "黄金", "PLATINUM": "铂金", "EMERALD": "翡翠",
         "DIAMOND": "钻石", "MASTER": "大师", "GRANDMASTER": "宗师", "CHALLENGER": "王者"}
PHASES = {"None": "空闲", "Lobby": "房间中", "Matchmaking": "匹配中", "ReadyCheck": "找到对局", "ChampSelect": "英雄选择",
          "GameStart": "游戏加载", "InProgress": "游戏中", "Reconnect": "等待重连", "WaitingForStats": "等待结算",
          "PreEndOfGame": "结算中", "EndOfGame": "对局结束"}


def load_settings() -> dict:
    try:
        return {**DEFAULTS, **json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))}
    except (OSError, ValueError):
        return dict(DEFAULTS)


def save_settings(s: dict) -> dict:
    merged = {**load_settings(), **{k: v for k, v in s.items() if k in DEFAULTS}}
    merged["pick"] = [int(x) for x in merged["pick"]][:10]
    merged["ban"] = [int(x) for x in merged["ban"]][:10]
    for key in ("pick_by_pos", "ban_by_pos"):
        merged[key] = {k: [int(x) for x in v][:10] for k, v in (merged[key] or {}).items() if k in POSITIONS and v}
    merged["accept_delay"] = max(0.0, min(float(merged["accept_delay"]), 10.0))
    merged["pick_delay"] = round(max(0.0, min(float(merged["pick_delay"]), 5.0)) * 2) / 2
    merged["ban_delay"] = round(max(0.0, min(float(merged["ban_delay"]), 5.0)) * 2) / 2
    if merged["review_tone"] not in TONES:
        merged["review_tone"] = "objective"
    if merged["rune_source"] not in ("opgg", "tencent"):
        merged["rune_source"] = "opgg"
    merged["history_count"] = max(5, min(int(merged["history_count"]), 50))
    from lol_review import opgg

    if merged["opgg_region"] not in opgg.REGIONS:
        merged["opgg_region"] = DEFAULTS["opgg_region"]
    if merged["opgg_tier"] not in opgg.TIERS:
        merged["opgg_tier"] = DEFAULTS["opgg_tier"]
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(json.dumps(merged, ensure_ascii=False, indent=1), encoding="utf-8")
    return merged


def rune_configs() -> dict[str, dict]:
    """Rune pages the user saved per champion; they win over OP.GG / 国服 recommendations."""
    try:
        return json.loads(RUNE_CONFIGS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_rune_config(cid: int, page: dict | None) -> dict:
    d = rune_configs()
    if page:
        d[str(cid)] = {k: page[k] for k in ("primary_style", "sub_style", "perks")} | {"name": page.get("name", ""), "saved": time.time()}
    else:
        d.pop(str(cid), None)
    RUNE_CONFIGS.parent.mkdir(parents=True, exist_ok=True)
    RUNE_CONFIGS.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    return d


def rank_text(ranked: dict | None, queue: str = "RANKED_SOLO_5x5") -> dict | None:
    q = ((ranked or {}).get("queueMap") or {}).get(queue) or {}
    tier = q.get("tier") or ""
    if not tier or tier in ("NONE", "UNRANKED"):
        return None
    div = q.get("division") if q.get("division") not in (None, "NA", "") else ""
    def label(t, d):
        if not t or t in ("NONE", "UNRANKED"):
            return None
        return f"{TIERS.get(t, t)} {d if d not in (None, 'NA', '') else ''}".strip()

    return {"tier": tier, "text": f"{TIERS.get(tier, tier)} {div}".strip(), "lp": q.get("leaguePoints", 0),
            "wins": q.get("wins", 0), "losses": q.get("losses", 0),
            "highest": label(q.get("highestTier"), q.get("highestDivision")),
            "prev_highest": label(q.get("previousSeasonHighestTier"), q.get("previousSeasonHighestDivision")),
            "prev_end": label(q.get("previousSeasonEndTier"), q.get("previousSeasonEndDivision"))}


class Assistant:
    def __init__(self, lcu: LCU) -> None:
        self.lcu = lcu
        self.settings = load_settings()
        self.state: dict = {"connected": False, "phase": "None", "phase_text": PHASES["None"], "me": None,
                            "champ_select": None, "in_game": None, "last_game": None, "plan": None}
        self.events: list[dict] = []  # recent log entries, newest last
        self.version = 0  # bumps on every state change; the page long-polls it
        self._cond = threading.Condition()
        self._cards: dict[str, tuple[float, dict]] = {}
        self._pending: set[str] = set()
        self._pool = ThreadPoolExecutor(max_workers=6)
        self._acted: set[int] = set()
        self._action_seen: dict[int, float] = {}  # action id -> when it became ours (pick/ban delay)
        self._honored: int | None = None
        self._live_at = 0.0
        self._mastery: dict[tuple, dict | None] = {}
        self._lp_at = 0.0
        self._plan_key = None  # (champion, position, enemy champions) the champ-select plan was made for
        self._plan_game = None  # game id the loading-screen plan was made for
        self._ingame_since = 0.0
        self._items_written = None
        self._applied_for: tuple | None = None  # (champion, position) runes/spells were applied for
        self._accepted_at: float | None = None
        self._ingame_key = None
        self._thread: threading.Thread | None = None

    # ---- lifecycle -------------------------------------------------------
    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="client-assistant", daemon=True)
            self._thread.start()

    def _changed(self) -> None:
        with self._cond:
            self.version += 1
            self._cond.notify_all()

    def wait(self, since: int, timeout: float = 25.0) -> int:
        with self._cond:
            self._cond.wait_for(lambda: self.version != since, timeout=timeout)
            return self.version

    def log(self, text: str, kind: str = "info") -> None:
        self.events.append({"t": time.time(), "kind": kind, "text": text})
        del self.events[:-60]
        self._changed()

    def snapshot(self) -> dict:
        return {**self.state, "settings": self.settings, "events": self.events[-30:], "version": self.version}

    def update_settings(self, s: dict) -> dict:
        self.settings = save_settings(s)
        self._changed()
        return self.settings

    # ---- main loop -------------------------------------------------------
    def _loop(self) -> None:
        while True:
            try:
                self._tick()
            except LCUError:
                if self.state["connected"]:
                    self.state.update(connected=False, me=None, champ_select=None, in_game=None)
                    self.log("与客户端断开连接", "warn")
            except Exception as e:  # never let the loop die
                self.log(f"助手出错：{type(e).__name__}: {e}", "error")
            time.sleep(1.0 if self.state["connected"] else 3.0)

    def _tick(self) -> None:
        if not self.lcu.connected and not self.lcu.connect():
            if self.state["connected"]:
                self.state["connected"] = False
                self._changed()
            return
        if not self.state["connected"] or not self.state["me"]:
            me = self.lcu.me()
            if not me:
                return  # client up but not logged in yet
            self.state.update(connected=True, me={
                "name": me.get("gameName") or me.get("displayName") or me.get("internalName"), "tag": me.get("tagLine", ""),
                "puuid": me.get("puuid"), "level": me.get("summonerLevel"), "icon": me.get("profileIconId"), "region": self._region()})
            self._pool.submit(self._record_lp)
            self.log(f"已连接客户端：{self.state['me']['name']}" + (f"#{self.state['me']['tag']}" if self.state["me"]["tag"] else ""), "ok")
        phase = self.lcu.phase()
        if time.time() - self._lp_at > 900 and phase not in ("ChampSelect", "GameStart", "InProgress"):
            self._pool.submit(self._record_lp)
        if phase != self.state["phase"]:
            prev = self.state["phase"]
            self.state.update(phase=phase, phase_text=PHASES.get(phase, phase))
            self._on_phase(prev, phase)
            self._changed()
        if phase == "ReadyCheck":
            self._ready_check()
        elif phase == "ChampSelect":
            self._champ_select()
        elif phase in ("GameStart", "InProgress", "Reconnect"):
            self._in_game()

    def _on_phase(self, prev: str, phase: str) -> None:
        if phase != "ReadyCheck":
            self._accepted_at = None
        if phase != "ChampSelect":
            self._acted.clear()
            self._action_seen.clear()
            self._applied_for = None
            if prev == "ChampSelect" and phase not in ("GameStart", "InProgress"):
                self.state["champ_select"] = None
        if phase in ("Lobby", "None", "Matchmaking"):
            self.state["in_game"] = None
            self._ingame_key = None
            self.state["plan"] = None
            self._plan_key = self._plan_game = self._items_written = None
        if phase in ("PreEndOfGame", "EndOfGame") and prev not in ("PreEndOfGame", "EndOfGame"):
            self._game_ended()
        if phase in ("PreEndOfGame", "EndOfGame") and prev not in ("PreEndOfGame", "EndOfGame"):
            self._pool.submit(self._record_lp, (5, 15, 45))  # the client updates LP a few seconds after the game
            self._pool.submit(lambda: (time.sleep(90), self.renew_opgg()))  # OP.GG sees the game about a minute later
        if phase in ("PreEndOfGame", "EndOfGame") and self.settings["auto_honor"]:
            self._pool.submit(self._auto_honor)

    # ---- auto accept -----------------------------------------------------
    def _ready_check(self) -> None:
        if not self.settings["auto_accept"]:
            return
        rc = self.lcu.ready_check() or {}
        if rc.get("playerResponse") not in (None, "None"):
            return
        now = time.time()
        if self._accepted_at is None:
            self._accepted_at = now
        if now - self._accepted_at >= self.settings["accept_delay"]:
            self.lcu.accept_ready_check()
            self.log("已自动接受对局", "ok")
            self._accepted_at = now + 3600  # once per ready check

    # ---- champ select ----------------------------------------------------
    def _champ_select(self) -> None:
        sess = self.lcu.champ_select()
        if not sess:
            return
        me_cell = sess.get("localPlayerCellId")
        my_team = sess.get("myTeam") or []
        their_team = sess.get("theirTeam") or []
        actions = [a for group in sess.get("actions") or [] for a in group]
        taken = {a["championId"] for a in actions if a.get("completed") and a.get("championId")}
        taken |= set((sess.get("bans") or {}).get("myTeamBans") or []) | set((sess.get("bans") or {}).get("theirTeamBans") or [])
        intents = {m.get("championPickIntent") for m in my_team if m.get("cellId") != me_cell} - {0, None}

        me = next((m for m in my_team if m.get("cellId") == me_cell), {})
        my_pos = (me.get("assignedPosition") or "").upper()
        my_pos = {"MID": "MIDDLE", "BOT": "BOTTOM", "ADC": "BOTTOM", "SUPPORT": "UTILITY"}.get(my_pos, my_pos)
        now = time.time()
        for a in actions:
            if a.get("actorCellId") != me_cell or not a.get("isInProgress") or a.get("completed") or a["id"] in self._acted:
                continue
            kind = a.get("type")
            if kind not in ("pick", "ban") or not self.settings[f"auto_{kind}"]:
                continue
            prefs = (self.settings[f"{kind}_by_pos"] or {}).get(my_pos) or self.settings[kind]
            if not prefs:
                continue
            seen = self._action_seen.setdefault(a["id"], now)
            if now - seen < self.settings[f"{kind}_delay"]:
                continue
            ok = self.lcu.pickable() if kind == "pick" else self.lcu.bannable()
            cid = next((c for c in prefs if c not in taken and c not in intents and (not ok or c in ok)), None)
            if not cid:
                continue
            lock = self.settings["lock_in"] if kind == "pick" else True
            self.lcu.do_action(a["id"], cid, lock)
            self._acted.add(a["id"])
            where = f"（{my_pos} 列表）" if (self.settings[f"{kind}_by_pos"] or {}).get(my_pos) else ""
            self.log(f"已自动{'禁用' if kind == 'ban' else '锁定' if lock else '预选'} {self.lcu.champ_name(cid)}{where}", "ok")

        my_champ = me.get("championId") or 0
        locked = any(a.get("actorCellId") == me_cell and a.get("type") == "pick" and a.get("completed") for a in actions)
        if my_champ and locked and (self.settings["auto_runes"] or self.settings["auto_spells"]):
            key = (my_champ, (me.get("assignedPosition") or "").upper())
            if key != self._applied_for:
                self._applied_for = key
                self._pool.submit(self._apply_build, my_champ, key[1])

        def member(m: dict, mine: bool) -> dict:
            cid = m.get("championId") or m.get("championPickIntent") or 0
            return {"cell": m.get("cellId"), "puuid": m.get("puuid") or "", "champion_id": cid, "champion": self.lcu.champ_name(cid),
                    "position": (m.get("assignedPosition") or "").upper(), "me": mine and m.get("cellId") == me_cell,
                    "card": self.card(m.get("puuid")) if mine else None}

        cs = {"my_team": [member(m, True) for m in my_team], "their_team": [member(m, False) for m in their_team],
              "bans": sorted(taken - {0}), "timer": (sess.get("timer") or {}).get("phase"),
              "my_champion": my_champ, "my_position": my_pos, "locked": locked}
        for m in cs["my_team"]:
            m["note"] = notes.get(m["puuid"]) if m["puuid"] and not m["me"] else None
        self._mark_premades([cs["my_team"]])
        if self.settings["game_plan"] and locked and my_champ:
            key = (my_champ, my_pos, tuple(sorted(m["champion_id"] for m in cs["their_team"] if m["champion_id"])))
            if key != self._plan_key:
                self._plan_key = key
                self._pool.submit(self._make_plan, "champ_select", key)
        if cs != self.state["champ_select"]:
            self.state["champ_select"] = cs
            self._changed()

    def _apply_build(self, cid: int, position: str) -> None:
        """Runes / summoner spells for the champion just locked: the user's saved page first, then 国服 or OP.GG data."""
        name = self.lcu.champ_name(cid)
        source = "国服" if self.settings["rune_source"] == "tencent" else "OP.GG"
        try:
            page, spells = None, None
            if self.settings["rune_source"] == "tencent":
                from lol_review import tencent

                b = tencent.best(cid, position)
                if b and b["runes"]:
                    r = b["runes"][0]
                    page = (r["primary_style"], r["sub_style"], r["perks"], f"{r['names'][0]} · 胜率 {r['win_rate']:.1%}")
                if b and b["spells"]:
                    spells = b["spells"][0]["ids"]
            else:
                from lol_review import opgg

                d = opgg.champion(cid, position, self.settings["opgg_region"], self.settings["opgg_tier"])
                if d["runes"]:
                    r = d["runes"][0]
                    page = (r["primary_style"], r["sub_style"], r["perks"], f"{r['primary'][0]['name']} · 胜率 {r['win_rate']:.1%}")
                if d["spells"]:
                    spells = [x["id"] for x in d["spells"][0]["spells"]]
            saved = rune_configs().get(str(cid))
            if saved:
                page, source = (saved["primary_style"], saved["sub_style"], saved["perks"], "你保存的符文页"), "自定义"
            if self.settings["auto_runes"] and page:
                self.lcu.apply_runes(f"RiftLens {name}", *page[:3])
                self.log(f"已为 {name} 应用{source}符文（{page[3]}）", "ok")
            if self.settings["auto_spells"] and spells and len(spells) == 2:
                a, b = spells
                if (self.settings["flash_on_f"] and a == 4) or (not self.settings["flash_on_f"] and b == 4):
                    a, b = b, a
                self.lcu.apply_spells(a, b)
                self.log(f"已为 {name} 设置召唤师技能", "ok")
        except Exception as e:
            self.log(f"自动应用 {name} 的{source}配置失败：{e}", "warn")

    # ---- in game ---------------------------------------------------------
    def _in_game(self) -> None:
        gs = self.lcu.gameflow_session() or {}
        gd = gs.get("gameData") or {}
        teams = [gd.get("teamOne") or [], gd.get("teamTwo") or []]
        key = (gd.get("gameId"), tuple(p.get("puuid") for t in teams for p in t))
        my = (self.state["me"] or {}).get("puuid")
        rows = [[{"puuid": p.get("puuid") or "", "name": p.get("gameName") or p.get("summonerName") or "",
                  "tag": p.get("tagLine", ""), "champion_id": p.get("championId", 0), "champion": self.lcu.champ_name(p.get("championId", 0)),
                  "position": (p.get("selectedPosition") or "").upper(), "me": p.get("puuid") == my, "card": self.card(p.get("puuid"))}
                 for p in t] for t in teams]
        for p in (p for t in rows for p in t):
            p["mastery"] = self._champ_mastery(p["puuid"], p["champion_id"])
            p["note"] = notes.get(p["puuid"]) if p["puuid"] and not p["me"] else None
        game = {"game_id": gd.get("gameId"), "queue": QUEUES.get((gd.get("queue") or {}).get("id"), (gd.get("queue") or {}).get("description", "")),
                "teams": rows, "my_team": next((i for i, t in enumerate(rows) if any(p["me"] for p in t)), 0),
                "live": (self.state.get("in_game") or {}).get("live") if key == self._ingame_key else None}
        self._mark_premades(rows, game)
        gid = gd.get("gameId")
        if key != self._ingame_key:
            self._ingame_since = time.time()
        plan_due = False
        if self.settings["game_plan"] and gid and gid != self._plan_game:
            cards = [p.get("card") for t in rows for p in t if p.get("puuid")]
            ready = cards and all(c and not c.get("loading") for c in cards)
            plan_due = bool(ready or time.time() - self._ingame_since > 20)
        if time.time() - self._live_at >= 3:
            self._live_at = time.time()
            game["live"] = self._live(rows)
        if key != self._ingame_key or game != self.state["in_game"]:
            if key != self._ingame_key and gd.get("gameId"):
                self.log(f"进入游戏 {gd.get('gameId')}，已查询 {sum(len(t) for t in teams)} 名玩家", "info")
            self._ingame_key = key
            self.state["in_game"] = game
            self._changed()
        if plan_due:  # after the state holds this tick's cards, so the plan reads them
            self._plan_game = gid
            self._pool.submit(self._make_plan, "game", gid)

    def _region(self) -> str:
        """OP.GG region code of the logged-in server (euw, na, kr, ...), or "" for servers OP.GG does not cover (国服)."""
        from lol_review import opgg

        try:
            r = (self.lcu.get("/riotclient/region-locale") or {}).get("webRegion") or ""
        except LCUError:
            return ""
        r = {"oc1": "oce", "eun": "eune"}.get(r.lower(), r.lower())
        return r if r in opgg.REGIONS and r != "global" else ""

    def renew_opgg(self) -> dict | None:
        """Ask OP.GG to refresh this account so it records an LP point (overseas accounts only)."""
        from lol_review import opgg

        me = self.state.get("me") or {}
        if not me.get("region") or not me.get("tag"):
            return None
        try:
            return opgg.renew(me["region"], me["name"], me["tag"])
        except Exception:
            return None

    def _record_lp(self, waits: tuple = (0,)) -> None:
        """Save the logged-in account's rank whenever it changes; this builds the LP curve over time."""
        from lol_review import lp

        self._lp_at = time.time()
        for w in waits:
            time.sleep(w)
            try:
                me = (self.state.get("me") or {}).get("puuid")
                if not me:
                    return
                changed = lp.record(me, self.lcu.ranked(me))
            except Exception:
                return
            if changed:
                self._changed()
                return

    # ---- pre-game plan and build ------------------------------------------
    def _make_plan(self, stage: str, key) -> None:
        """Facts + rule tips + build right away; the model's written plan follows on the loading screen."""
        from lol_review import gameplan

        time.sleep(1.0)  # champ select: several picks often land together; game: let this tick store the cards first
        if stage == "champ_select" and key != self._plan_key:
            return
        try:
            ctx = gameplan.context(self.state)
            if not ctx:
                return
            b = gameplan.build(ctx["me"]["champion_id"], ctx["me"]["position"], ctx["enemy_cids"])
            tips = gameplan.rules(ctx, b)
        except Exception as e:
            self.log(f"生成对局计划失败：{type(e).__name__}: {e}", "warn")
            return
        old = self.state.get("plan") or {}
        plan = {"stage": stage, "key": str(key), "made_at": time.time(), "context": ctx, "build": b, "tips": tips,
                "ai": old.get("ai") if old.get("key") == str(key) else None, "ai_status": None, "items_status": old.get("items_status")}
        self.state["plan"] = plan
        self._changed()
        if stage == "champ_select" and self.settings["auto_items"]:
            sig = (ctx["me"]["champion_id"], tuple(i["id"] for i in b["core"] + b["boots"] + [a["item"] for a in b["adjust"]]))
            if sig != self._items_written:
                try:
                    self.lcu.apply_item_set(f"RiftLens {b['champion']}（按这把阵容）", ctx["me"]["champion_id"], gameplan.item_set_blocks(b))
                    self._items_written = sig
                    plan["items_status"] = f"已写入游戏商店的推荐装备页「RiftLens {b['champion']}」"
                    self.log(f"已按对面阵容为 {b['champion']} 生成出装，进游戏后在商店推荐页里", "ok")
                except Exception as e:
                    plan["items_status"] = f"写入游戏商店失败：{e}"
                self._changed()
        if stage == "game" and self.settings["plan_ai"]:
            self.plan_ai()

    def plan_ai(self) -> bool:
        """Ask the model for a written plan from the current facts (one call, ~10-30 s). Runs in the background."""
        plan = self.state.get("plan")
        if not plan or plan.get("ai_status") == "running":
            return False
        plan["ai_status"] = "running"
        self._changed()

        def run():
            from lol_review import gameplan

            try:
                text = gameplan.write_with_model(plan["context"], plan["tips"], plan["build"])
                plan.update(ai=text.strip(), ai_status="done")
            except Exception as e:
                plan.update(ai_status=f"AI 计划生成失败：{str(e)[:200]}")
            self._changed()

        self._pool.submit(run)
        return True

    def _champ_mastery(self, puuid: str, cid: int) -> dict | None:
        if not puuid or not cid:
            return None
        k = (puuid, cid)
        if k not in self._mastery:
            self._mastery[k] = None
            self._pool.submit(self._load_mastery, k)
        return self._mastery[k]

    def _load_mastery(self, k: tuple) -> None:
        try:
            m = self.lcu.mastery(*k)
        except Exception:
            m = None
        if isinstance(m, dict):
            self._mastery[k] = {"level": m.get("championLevel"), "points": m.get("championPoints")}
            self._changed()

    def _mark_premades(self, teams: list[list[dict]], game: dict | None = None) -> None:
        """Group label (1, 2, ...) on players who share >= 2 recent games with a teammate."""
        hist = {p["puuid"]: set(p["card"].get("game_ids") or []) for t in teams for p in t
                if p.get("puuid") and isinstance(p.get("card"), dict) and p["card"].get("game_ids")}
        if game and game.get("game_id"):
            hist = {k: v - {game["game_id"]} for k, v in hist.items()}
        groups = playstyle.premade_groups(hist, [[p["puuid"] for p in t] for t in teams])
        label = {puuid: i + 1 for i, g in enumerate(groups) for puuid in g}
        for t in teams:
            for p in t:
                p["premade"] = label.get(p.get("puuid"))

    def _live(self, rows: list[list[dict]]) -> dict | None:
        """KDA / CS / level / item gold per player from the game's live API, keyed by puuid."""
        data = live_game()
        if not data:
            return None
        from lol_review import opgg

        try:
            items = opgg.static()["items"]
        except Exception:
            items = {}
        by_name = {}
        for t in rows:
            for p in t:
                for n in (f"{p['name']}#{p['tag']}" if p["tag"] else None, p["name"]):
                    if n:
                        by_name[n.lower()] = p["puuid"]
        out = {}
        for lp in data.get("allPlayers") or []:
            puuid = by_name.get(str(lp.get("riotId") or "").lower()) or by_name.get(str(lp.get("riotIdGameName") or lp.get("summonerName") or "").lower())
            if not puuid:
                continue
            sc = lp.get("scores") or {}
            gold = sum(((items.get(it.get("itemID")) or {}).get("gold") or it.get("price") or 0) * max(it.get("count", 1), 1) for it in lp.get("items") or [])
            out[puuid] = {"k": sc.get("kills", 0), "d": sc.get("deaths", 0), "a": sc.get("assists", 0), "cs": sc.get("creepScore", 0),
                          "level": lp.get("level"), "dead": lp.get("isDead"), "respawn": round(lp.get("respawnTimer") or 0),
                          "gold": gold, "items": [it.get("itemID") for it in lp.get("items") or []]}
        gt = (data.get("gameData") or {}).get("gameTime")
        return {"time": round(gt or 0), "players": out}

    def _auto_honor(self) -> None:
        """Honor teammates (category 友善 HEART), rotating who goes first by game id. Once per game."""
        for wait in (1.0, 1.5, 2.0):
            time.sleep(wait)
            ballot = self.lcu.honor_ballot()
            if not ballot or not ballot.get("gameId"):
                continue
            gid = ballot["gameId"]
            if gid == self._honored:
                return
            allies = [p for p in ballot.get("eligibleAllies") or [] if not p.get("botPlayer") and p.get("puuid")]
            votes = int(((ballot.get("votePool") or {}).get("votes")) or (1 if allies else 0))
            if not allies or votes <= 0:
                return
            start = gid % len(allies)
            picks = (allies[start:] + allies[:start])[:votes]
            self._honored = gid
            try:
                for p in picks:
                    self.lcu.honor(p["puuid"], gid, p.get("summonerId"))
                self.lcu.submit_honor()
                names = "、".join(p.get("gameName") or p.get("summonerName") or "队友" for p in picks)
                self.log(f"已自动点赞：{names}", "ok")
            except LCUError as e:
                self.log(f"自动点赞失败：{e}", "warn")
            return

    # ---- end of game -----------------------------------------------------
    def _game_ended(self) -> None:
        gid = None
        try:
            gid = (self.lcu.end_of_game() or {}).get("gameId")
        except LCUError:
            pass
        if not gid and (self.state["in_game"] or {}).get("game_id"):
            gid = self.state["in_game"]["game_id"]
        if not gid:
            return
        ig = self.state.get("in_game") or {}
        players = []
        if ig.get("game_id") == gid:  # who you just played with, for post-game notes
            for i, t in enumerate(ig.get("teams") or []):
                for p in t:
                    if p.get("puuid") and not p.get("me"):
                        players.append({"puuid": p["puuid"], "name": p.get("name") or (p.get("card") or {}).get("name", ""),
                                        "champion": p.get("champion"), "champion_id": p.get("champion_id"),
                                        "with": "ally" if i == ig.get("my_team") else "enemy"})
        self.state["last_game"] = {"game_id": gid, "match_id": f"LCU_{gid}", "ended_at": time.time(),
                                   "auto_review": self.settings["auto_review"], "tone": self.settings["review_tone"], "players": players}
        self.log(f"对局 {gid} 已结束" + ("，开始自动复盘" if self.settings["auto_review"] else "，可以点击复盘"), "ok")

    # ---- player cards ----------------------------------------------------
    def card(self, puuid: str | None) -> dict | None:
        """Cached player card; computed in the background the first time it is asked for."""
        if not puuid or not self.settings["query_players"]:
            return None
        hit = self._cards.get(puuid)
        if (hit and time.time() - hit[0] < 600) or puuid in self._pending:
            return hit[1] if hit else {"loading": True}
        self._pending.add(puuid)
        self._pool.submit(self._build_card, puuid)
        return hit[1] if hit else {"loading": True}

    def _build_card(self, puuid: str) -> None:
        try:
            data = self.player(puuid, self.settings["history_count"])
        except Exception as e:
            data = {"error": str(e)}
        self._cards[puuid] = (time.time(), data)
        self._pending.discard(puuid)
        self._patch_cards()

    def _patch_cards(self) -> None:
        for block in ("champ_select", "in_game"):
            st = self.state.get(block)
            if not st:
                continue
            groups = [st["my_team"]] if block == "champ_select" else st["teams"]
            for g in groups:
                for p in g:
                    if p.get("puuid") in self._cards:
                        p["card"] = self._cards[p["puuid"]][1]
        self._changed()

    def player(self, puuid: str, count: int = 20) -> dict:
        """Rank + recent record for one player, with Akari-style tags."""
        s = self.lcu.summoner(puuid) or {}
        ranked = self.lcu.ranked(puuid)
        raw = self.lcu.history_pages(puuid, count)
        style = playstyle.profile([playstyle.stat_row(g) for g in raw])
        games = [history_row(g, self.lcu) for g in raw]
        games = [g for g in games if not g["remake"]]
        games.sort(key=lambda g: -g["created"])
        wins = sum(g["win"] for g in games)
        k, d, a = (sum(g[x] for g in games) for x in ("kills", "deaths", "assists"))
        streak, last = 0, games[0]["win"] if games else None
        for g in games:
            if g["win"] != last:
                break
            streak += 1
        champs = Counter(g["champion"] for g in games)
        top = [{"champion": c, "games": n, "wins": sum(g["win"] for g in games if g["champion"] == c)} for c, n in champs.most_common(3)]
        tags = []
        if games and streak >= 3:
            tags.append({"text": f"{streak} 连{'胜' if last else '败'}", "tone": "good" if last else "bad"})
        if len(games) >= 8:
            wr = wins / len(games)
            if wr >= 0.65:
                tags.append({"text": f"近期胜率 {wr:.0%}", "tone": "good"})
            elif wr <= 0.35:
                tags.append({"text": f"近期胜率 {wr:.0%}", "tone": "bad"})
        if top and len(games) >= 8 and top[0]["games"] / len(games) >= 0.5:
            tags.append({"text": f"绝活 {top[0]['champion']}", "tone": "info"})
        if games and d and (k + a) / d >= 4.5:
            tags.append({"text": "高 KDA", "tone": "good"})
        level = s.get("summonerLevel")
        solo = rank_text(ranked)
        if level and level < 50 and len(games) >= 8 and wins / len(games) >= 0.7:
            tags.append({"text": "疑似小号", "tone": "warn"})
        if s.get("privacy") == "PRIVATE":
            tags.append({"text": "战绩隐藏", "tone": "info"})
        have = {t["text"] for t in tags}
        for t in style["tags"]:
            if t["text"] not in have and not t["text"].endswith(("连胜", "连败")) and t["text"] != "单英雄":
                tags.append(t)
        kda = round((k + a) / max(d, 1), 2) if games else None
        return {
            "name": s.get("gameName") or s.get("displayName") or "", "tag": s.get("tagLine", ""), "level": level,
            "solo": solo, "flex": rank_text(ranked, "RANKED_FLEX_SR"),
            "games": len(games), "wins": wins, "win_rate": round(wins / len(games), 2) if games else None,
            "kda": kda, "score": playstyle.scout_score(len(games), wins, kda), "game_ids": [g["game_id"] for g in games],
            "note": notes.get(puuid), "averages": style["averages"], "avg": f"{k / max(len(games), 1):.1f}/{d / max(len(games), 1):.1f}/{a / max(len(games), 1):.1f}" if games else "",
            "recent": [{"win": g["win"], "champion": g["champion"], "champion_id": g["champion_id"], "kda": g["kda"], "queue": g["queue"]} for g in games[:10]],
            "top_champions": top, "tags": tags,
        }


_assistant: Assistant | None = None


def get() -> Assistant:
    global _assistant
    if _assistant is None:
        from lol_review import lcu

        _assistant = Assistant(lcu.default())
    return _assistant
