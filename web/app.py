"""Chat web app for the LoL review agent.

    .venv/bin/python -m uvicorn web.app:app --port 8765

Two modes:
- LLM mode (LLM_BASE_URL / LLM_API_KEY / LLM_MODEL set in .env): an
  OpenAI-compatible model runs a tool-calling loop over the MCP tools.
- No-LLM mode: the fixed review skeleton runs and a template turns the tool
  outputs into a review (baseline B0).

Each turn streams server-sent events: tool calls, tool results, the answer,
and the trace id, so the page can show what the agent did.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

import server as S
from lol_review import analysis as A
from lol_review.riot import RiotError, load_env

load_env()
ROOT = Path(__file__).resolve().parent
MAX_STEPS = 14

SYSTEM_PROMPT = """你是英雄联盟对局复盘教练，只能根据工具返回的数据说话。

工作方式：
1. 先找到对局，再调用 get_match_summary 和 turning_points。
   - 用户说"我""刚打完的这局"，或者给出 LCU_ 开头的对局 id：用 client_recent_matches 从本机客户端读取（任何服务器都能用，包括国服，不需要 Riot key）。
   - 给出 Riot ID 查别人：用 find_player 和 list_recent_matches（需要 Riot key，不支持国服）。
2. 按需深挖：死亡多 → analyze_deaths；打野 → jungle_stats；团战 → segment_fights；对线 → lane_phase_stats。
3. 说到"单杀""某人参与"之类，先用 kill_log 核对助攻名单。
4. 动手前用 update_plan 列出计划，做完一步更新一步。
5. 出装、符文、对位胜率、英雄强度类问题，先用 opgg_champion 拿统计数据，并注明来源 OP.GG 和版本。
6. "这把怎么打 / 我该出什么 / 对面谁最弱"这类正在进行的对局问题：先用 client_game_plan。
7. "分析我最近的对局 / 我的强项弱项"这类多局问题：用 client_coach_data（可按位置筛选，最多 100 局排位），
   输出三段：强项、弱项、一个最该改进的重点，然后再写详细分析。

输出要求（中文）：
- 每条结论后用方括号标注依据的时间点或工具名，例如 [11:22 jungle_stats]。
- 区分"数据直接显示"和"推断"，推断要写明。
- 数据里没有技能释放、精确走位和插眼坐标，涉及这些就说无法判断，不要编。
- 结构：概况 → 关键转折 → 做得好的 → 需要改进的 → 1–3 条可执行建议。"""


TONES = {
    "objective": "",
    "roast": ("【回答风格：毒舌喷子】用尖刻、夸张、好笑的语气吐槽这位玩家的游戏表现，像损友一样毫不留情。"
              "每一句吐槽都必须有数据依据并标注出处；只针对游戏表现，不涉及现实身份、外貌、家人，不用脏话和歧视性词语。"
              "最后仍然给出 1–3 条真正有用的建议。"),
    "praise": ("【回答风格：专业夸夸】用专业教练的口吻，几乎全部是夸奖：把亮点放大，把问题说成成长空间或独特风格。"
               "引用的数字必须真实，不能编造数据。"),
}


def tone_note(tone: str | None) -> str:
    return TONES.get(tone or "", "")


def agent_mode() -> str:
    """AGENT_MODE=llm | bridge | b0. Default: llm if configured, otherwise bridge."""
    mode = os.environ.get("AGENT_MODE", "").strip().lower()
    if mode in ("llm", "bridge", "b0"):
        return mode
    return "llm" if llm_config() else os.environ.get("AGENT_MODE_FALLBACK", "bridge")  # the desktop exe falls back to b0


def llm_config() -> dict | None:
    base, key, model = (os.environ.get(k, "").strip() for k in ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL"))
    return {"base_url": base, "api_key": key, "model": model} if base and key and model else None


TOOLS: list[dict] = []  # filled from the MCP server at startup


async def _load_tools() -> None:
    global TOOLS
    listed = await S.mcp.list_tools()
    TOOLS = [{"type": "function", "function": {"name": t.name, "description": t.description or "",
                                                "parameters": t.input_schema}} for t in listed]


def call_tool(name: str, args: dict):
    fn = getattr(S, name, None)
    if fn is None or name not in {t["function"]["name"] for t in TOOLS}:
        return {"error": f"unknown tool {name}"}
    try:
        return fn(**args)
    except TypeError as e:
        return {"error": f"bad arguments: {e}"}


def sse(kind: str, data) -> str:
    return f"event: {kind}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def short(obj, limit: int = 160) -> str:
    text = json.dumps(obj, ensure_ascii=False)
    return text if len(text) <= limit else text[: limit - 1] + "…"


# ------------------------------------------------------------------ LLM mode
async def run_llm(history: list[dict], cfg: dict):
    from openai import AsyncOpenAI

    client = AsyncOpenAI(base_url=cfg["base_url"], api_key=cfg["api_key"])
    msgs = [{"role": "system", "content": SYSTEM_PROMPT}, *history]
    S.TRACE.note(f"模式：模型 {cfg['model']}", kind="request")
    for step in range(MAX_STEPS):
        resp = await client.chat.completions.create(model=cfg["model"], messages=msgs, tools=TOOLS, temperature=0.2)
        choice = resp.choices[0].message
        usage = resp.usage.model_dump() if resp.usage else {}
        S.TRACE.note(f"模型第 {step + 1} 轮：{len(choice.tool_calls or [])} 个工具调用，token {usage}", kind="model")
        yield sse("usage", usage)
        if not choice.tool_calls:
            yield sse("answer", {"text": choice.content or ""})
            return
        msgs.append({"role": "assistant", "content": choice.content or "",
                     "tool_calls": [tc.model_dump() for tc in choice.tool_calls]})
        for tc in choice.tool_calls:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            yield sse("tool_call", {"name": tc.function.name, "args": args})
            out = await asyncio.to_thread(call_tool, tc.function.name, args)
            yield sse("tool_result", {"name": tc.function.name, "preview": short(out), "error": out.get("error") if isinstance(out, dict) else None})
            msgs.append({"role": "tool", "tool_call_id": tc.id, "content": json.dumps(out, ensure_ascii=False)})
    yield sse("answer", {"text": f"已达到 {MAX_STEPS} 轮工具调用上限，停止。"})


# ------------------------------------------------------------------ no-LLM mode (B0)
RIOT_ID = re.compile(r"([^\s#]{2,16}#[A-Za-z0-9]{2,5})")


LCU_ID = re.compile(r"LCU_\d+", re.I)


def b0_review(riot_id: str | None, match_id: str | None = None):
    """Fixed skeleton + template. Returns (steps, answer). match_id LCU_... reviews a game from the local client."""
    steps = []

    def run(name, **kw):
        steps.append(("tool_call", {"name": name, "args": kw}))
        out = call_tool(name, kw)
        steps.append(("tool_result", {"name": name, "preview": short(out), "error": out.get("error") if isinstance(out, dict) else None}))
        return out

    if match_id or not riot_id:  # the local client: the logged-in account's game
        ms = run("client_recent_matches", count=20 if match_id else 1)
        if "error" in ms or not ms.get("matches"):
            return steps, f"读取客户端对局失败：{ms.get('error', '没有对局')}"
        m = next((x for x in ms["matches"] if x["match_id"].upper() == (match_id or "").upper()), ms["matches"][0])
        riot_id = ms["player"]
    else:
        p = run("find_player", riot_id=riot_id)
        if "error" in p:
            return steps, f"找不到玩家 {riot_id}：{p['error']}"
        ms = run("list_recent_matches", puuid=p["puuid"], count=1)
        if "error" in ms or not ms.get("matches"):
            return steps, "没有找到最近的对局。"
        m = ms["matches"][0]
    mid = match_id or m["match_id"]
    summ = run("get_match_summary", match_id=mid)
    if "error" in summ:
        return steps, f"读取对局失败：{summ['error']}"
    if match_id:  # take champion/role/result from the game itself, not the (possibly shorter) list
        me = next((x for x in summ["players"] if x["player"].lower() == riot_id.lower()), None)
        if me is None:
            return steps, f"对局 {mid} 里没有找到当前登录的账号 {riot_id}。"
        m = {"champion": me["champion"], "role": me["role"] or "?", "win": summ["winner"] == me["team"], "kda": me["kda"]}
    tps = run("turning_points", match_id=mid)
    mist = run("detect_mistakes", match_id=mid, player=riot_id)
    extra = run("jungle_stats", match_id=mid, player=riot_id) if m["role"] == "JUNGLE" else run("analyze_deaths", match_id=mid, player=riot_id)
    lane = run("lane_phase_stats", match_id=mid, player=riot_id)

    me = next((x for x in summ["players"] if x["player"].lower() == riot_id.lower()), None)
    lines = [f"**无模型模式（B0：固定骨架 + 模板）**，未配置 LLM 时使用。", "",
             f"### 概况",
             f"{mid}，队列 {summ['queue']}，{summ['patch']} 版本，{summ['duration']}，{'胜利' if m['win'] else '失败'}。",
             f"你：{m['champion']}（{m['role']}），KDA {m['kda']}" + (f"，补刀 {me['cs']}（{me['cs_per_min']}/分），伤害 {me['damage']:,}，视野 {me['vision_score']}" if me else "") + "。",
             "", "### 经济转折（经济差变化最大的时段）"]
    for t in tps["turning_points"]:
        objs = "；".join(t["objectives"]) or "无资源事件"
        lines.append(f"- {t['window']} 偏向{t['gold_swing_to']}方 {t['gold_swing']:,} 金币，击杀 蓝{t['kills']['蓝']}:红{t['kills']['红']}；{objs}")
    for cp in lane.get("checkpoints", []):
        if "diff" in cp:
            d = cp["diff"]
            lines.append(f"- {cp['minute']} 分钟对位 {lane['opponent']}：补刀 {d['cs']:+}，金币 {d['gold']:+}，经验 {d['xp']:+}")
    lines += ["", "### 规则检出的问题"]
    lines += [f"- {f['rule']}（{f['time'] or '全局'}）：{f['evidence'] if isinstance(f['evidence'], str) else '；'.join(f['evidence'])}" for f in mist["flags"]] or ["- 没有触发任何规则。规则主要覆盖死亡和对线，不代表没有问题。"]
    if m["role"] == "JUNGLE":
        lines += ["", "### 打野数据",
                  f"- 参团率 {extra['kill_participation']:.0%}，15 分钟前参与 {extra['takedowns_before_15']} 次击杀，未参与 {extra['team_kills_before_15_without_you']} 次",
                  (f"- 反野：对方野区野怪 {extra['invade']['enemy_jungle_monsters']}，偷 buff {extra['invade']['buffs_stolen']}"
                   if extra["invade"]["enemy_jungle_monsters"] is not None else "- 反野：客户端数据不含这项统计")]
        lines += [f"- {e['time']} {e['objective']}：{e['outcome']}" for e in extra["epic_monsters"]]
    lines += ["", "_模板只罗列数据，不做解读和建议。配置 LLM 后可获得完整复盘。_"]
    return steps, "\n".join(lines)


# ------------------------------------------------------------------ API
app = FastAPI(title="RiftLens")


@app.on_event("startup")
async def _startup() -> None:
    await _load_tools()
    from web import store

    store.import_bridge_history()
    if os.environ.get("RIFTLENS_CLIENT_ASSISTANT", "1") != "0":
        from lol_review import assistant

        assistant.get().start()


def _client_connected() -> bool:
    from lol_review import assistant

    return bool(assistant._assistant and assistant._assistant.state["connected"])


class ChatIn(BaseModel):
    text: str
    conversation_id: str | None = None
    tone: str | None = None  # objective | roast | praise


@app.get("/")
def index():
    return FileResponse(ROOT / "static" / "index.html")


@app.get("/api/status")
def status():
    from lol_review.riot import key_status

    mode, cfg = agent_mode(), llm_config()
    return {"mode": mode, "model": cfg["model"] if (cfg and mode == "llm") else None,
            "tools": [t["function"]["name"] for t in TOOLS], "riot_key": key_status()}


class KeyIn(BaseModel):
    key: str


@app.put("/api/settings/riot-key")
def set_riot_key(body: KeyIn):
    """Save an optional Riot API key locally (data/riot_api_key, git-ignored) and check it against Riot."""
    import requests
    from fastapi import HTTPException
    from lol_review.riot import key_status, save_key

    key = body.key.strip()
    if not re.fullmatch(r"RGAPI-[0-9a-fA-F-]{36}", key):
        raise HTTPException(status_code=400, detail="格式不对：Riot key 形如 RGAPI-xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx")
    try:
        r = requests.get(f"https://{S.riot.platform}.api.riotgames.com/lol/status/v4/platform-data",
                         headers={"X-Riot-Token": key}, timeout=10)
        valid = r.status_code == 200
    except requests.RequestException:
        valid = None  # could not reach Riot; save anyway
    if valid is False:
        raise HTTPException(status_code=400, detail=f"Riot 拒绝了这个 key（HTTP {r.status_code}）。开发者 key 每 24 小时过期，请重新生成。")
    save_key(key)
    S.riot.key = key  # running server picks it up immediately; worker tool calls read it per process
    return {"ok": True, "verified": valid, "riot_key": key_status()}


async def run_bridge(text: str, history: list[dict], cid: str, memory: str):
    """Hand the message to the headless worker through the file mailbox and relay its events."""
    from web import bridge

    mid = bridge.post_user(text, history, cid, memory)
    yield sse("note", {"text": f"已排队（消息 {mid}）"})
    offset, waited = 0, 0.0
    while waited < 1800:
        rows, offset = bridge.events_after(mid, offset)
        for r in rows:
            yield sse(r["event"], r["data"])
            if r["event"] == "done":
                return
        await asyncio.sleep(0.5)
        waited += 0.5
    yield sse("answer", {"text": "等待超时（30 分钟），没有收到回复。"})


@app.post("/api/chat")
async def chat(body: ChatIn):
    from web import store

    cid = body.conversation_id or store.create_conversation()
    store.add_message(cid, "user", body.text)
    history = store.history_for_model(cid)
    memory = store.memory_context()

    async def stream():
        title = next((c["title"] for c in store.list_conversations() if c["id"] == cid), "新对话")
        yield sse("conversation", {"id": cid, "title": title})
        S.TRACE.rotate("web")
        S.TRACE.note(f"用户：{body.text}", kind="request")
        yield sse("trace", {"run_id": S.TRACE.run_id})
        mode, cfg = agent_mode(), llm_config()
        answer, tools = None, []
        try:
            note = tone_note(body.tone)
            if note:
                yield sse("note", {"text": "回答风格：" + ("毒舌喷子" if body.tone == "roast" else "专业夸夸")})
            if mode == "bridge":  # the worker persists the answer itself
                async for chunk in run_bridge(body.text + ("\n\n" + note if note else ""), history, cid, memory):
                    yield chunk
                return
            if mode == "llm" and cfg:
                msgs = list(history)
                if memory:
                    msgs.insert(0, {"role": "system", "content": "关于这位用户的长期记忆：\n" + memory})
                if note:
                    msgs.insert(0, {"role": "system", "content": note})
                async for chunk in run_llm(msgs, cfg):
                    if chunk.startswith("event: answer"):
                        answer = json.loads(chunk.split("data: ", 1)[1])["text"]
                    elif chunk.startswith("event: tool_call"):
                        tools.append(json.loads(chunk.split("data: ", 1)[1])["name"])
                    yield chunk
            else:
                hit, lcu_hit = RIOT_ID.search(body.text), LCU_ID.search(body.text)
                rid = hit.group(1) if hit else (store.get_profile()["riot_ids"] or [None])[0]
                if lcu_hit or (not hit and _client_connected()):
                    steps, answer = await asyncio.to_thread(b0_review, None, lcu_hit.group(0).upper() if lcu_hit else None)
                    for kind, data in steps:
                        tools += [data["name"]] if kind == "tool_call" else []
                        yield sse(kind, data)
                elif not rid:
                    answer = "请输入 Riot ID（例如 `Player#TAG`），或者在左下角的个人资料里填写你的账号。"
                else:
                    steps, answer = await asyncio.to_thread(b0_review, rid)
                    for kind, data in steps:
                        tools += [data["name"]] if kind == "tool_call" else []
                        yield sse(kind, data)
                yield sse("answer", {"text": answer})
        except RiotError as e:
            answer = f"Riot API 出错：{e}"
            yield sse("answer", {"text": answer})
        except Exception as e:  # surface model/gateway errors to the page
            answer = f"出错了：{type(e).__name__}: {e}"
            yield sse("answer", {"text": answer})
        if answer is not None:
            store.add_message(cid, "assistant", answer, None, tools)
        S.TRACE.note("本轮结束", kind="handoff")
        yield sse("done", {"steps": S.TRACE.n})

    return StreamingResponse(stream(), media_type="text/event-stream")


# ---------------------------------------------------------------- conversations
class TitleIn(BaseModel):
    title: str


@app.get("/api/conversations")
def conversations():
    from web import store

    return store.list_conversations()


@app.post("/api/conversations")
def new_conversation():
    from web import store

    return {"id": store.create_conversation()}


@app.get("/api/conversations/{cid}")
def conversation(cid: str):
    from web import store

    return {"id": cid, "messages": store.get_messages(cid)}


@app.patch("/api/conversations/{cid}")
def rename(cid: str, body: TitleIn):
    from web import store

    store.rename_conversation(cid, body.title)
    return {"ok": True}


@app.delete("/api/conversations/{cid}")
def remove(cid: str):
    from web import store

    store.delete_conversation(cid)
    return {"ok": True}


# ---------------------------------------------------------------- profile & memory
@app.get("/api/profile")
def profile():
    from web import store

    return {"profile": store.get_profile(), "memories": store.list_memories(), "context": store.memory_context()}


@app.put("/api/profile")
def save_profile(body: dict):
    from fastapi import HTTPException
    from web import store

    try:
        return {"profile": store.save_profile(body)}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.delete("/api/memories/{mem_id}")
def forget(mem_id: int):
    from web import store

    store.delete_memory(mem_id)
    return {"ok": True}


@app.get("/api/trace/{run_id}")
def trace(run_id: str):
    path = (Path(S.TRACE.path).parent / f"{run_id}.jsonl").resolve()
    if path.parent != Path(S.TRACE.path).parent.resolve() or not path.exists():
        return {"error": "not found"}
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


# ---------------------------------------------------------------- client assistant (local League client)
@app.get("/api/client/state")
async def client_state(since: int = -1):
    """Assistant state. Pass the last seen `version` to long-poll until something changes (≤ 25 s)."""
    from lol_review import assistant

    a = assistant.get()
    if since == a.version:
        await asyncio.to_thread(a.wait, since)
    return a.snapshot()


@app.put("/api/client/settings")
def client_settings(body: dict):
    from lol_review import assistant

    return assistant.get().update_settings(body)


@app.get("/api/client/champions")
def client_champions():
    from fastapi import HTTPException
    from lol_review import lcu

    try:
        champs = lcu.default().champions()
    except lcu.LCUError as e:
        raise HTTPException(status_code=503, detail=str(e))
    return sorted(({"id": i, **c} for i, c in champs.items()), key=lambda c: c["name"])


@app.get("/api/client/player/{puuid}")
def client_player(puuid: str):
    return S.client_player(puuid)


@app.get("/api/client/matches")
def client_matches(count: int = 20):
    return S.client_recent_matches(count=min(count, 50))


@app.get("/api/client/asset")
def client_asset(path: str):
    """Proxy champion/profile icons from the client (it needs auth and uses a self-signed cert)."""
    from fastapi import HTTPException, Response
    from lol_review import lcu

    if not path.startswith("/lol-game-data/assets/") or ".." in path:
        raise HTTPException(status_code=400, detail="only game assets")
    try:
        data = lcu.default().get(path)
    except lcu.LCUError as e:
        raise HTTPException(status_code=503, detail=str(e))
    if not isinstance(data, (bytes, bytearray)):
        raise HTTPException(status_code=404)
    return Response(content=bytes(data), media_type="image/png", headers={"Cache-Control": "max-age=86400"})


# ---------------------------------------------------------------- OP.GG
@app.get("/api/opgg/tiers")
def opgg_tiers(region: str = "global", tier: str = "emerald_plus", mode: str = "ranked"):
    from fastapi import HTTPException
    from lol_review import opgg

    try:
        return opgg.tier_list(region, tier, mode)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.get("/api/opgg/champion/{cid}")
def opgg_champion(cid: int, position: str = "", region: str = "global", tier: str = "emerald_plus", mode: str = "ranked"):
    from fastapi import HTTPException
    from lol_review import opgg

    try:
        return opgg.champion(cid, position, region, tier, mode)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


class RunesIn(BaseModel):
    name: str
    primary_style: int
    sub_style: int
    perks: list[int]


@app.post("/api/client/apply-runes")
def apply_runes(body: RunesIn):
    from fastapi import HTTPException
    from lol_review import lcu

    if len(body.perks) != 9:
        raise HTTPException(status_code=400, detail="符文页需要 9 个符文")
    try:
        lcu.default().apply_runes(f"RiftLens {body.name}", body.primary_style, body.sub_style, body.perks)
    except lcu.LCUError as e:
        raise HTTPException(status_code=503, detail=str(e))
    return {"ok": True}


class SpellsIn(BaseModel):
    spells: list[int]


@app.post("/api/client/apply-spells")
def apply_spells(body: SpellsIn):
    from fastapi import HTTPException
    from lol_review import lcu

    if len(body.spells) != 2:
        raise HTTPException(status_code=400, detail="需要 2 个召唤师技能")
    try:
        if lcu.default().phase() != "ChampSelect":
            raise HTTPException(status_code=409, detail="只能在英雄选择阶段设置召唤师技能")
        lcu.default().apply_spells(*body.spells)
    except lcu.LCUError as e:
        raise HTTPException(status_code=503, detail=str(e))
    return {"ok": True}


# ---------------------------------------------------------------- profile, scoreboard, search, notes
def _lcu_call(fn, *args, **kw):
    from fastapi import HTTPException
    from lol_review import lcu

    try:
        return fn(*args, **kw)
    except lcu.LCUError as e:
        raise HTTPException(status_code=503, detail=str(e))


def _with_opgg(me: dict, hist: dict) -> dict:
    """Past seasons and OP.GG's stored LP points for overseas accounts, merged into the locally recorded curve."""
    from lol_review import assistant, lp, opgg

    region = (assistant.get().state.get("me") or {}).get("region") or assistant.get()._region()
    out = {"lp_history": hist, "opgg": None}
    if not region:
        return out
    try:
        h = opgg.summoner_history(region, me.get("gameName", ""), me.get("tagLine", ""))
    except Exception as e:
        out["opgg"] = {"error": str(e), "region": region}
        return out
    solo = hist.get("RANKED_SOLO_5x5", []) + [{**p, "score": lp.score(p["tier"], p["division"], p["lp"])} for p in h["lp_points"]]
    solo.sort(key=lambda r: r["t"])
    merged = []
    for r in solo:  # OP.GG and RiftLens often record the same rank; keep one point per change
        if merged and all(merged[-1].get(k) == r.get(k) for k in ("tier", "division", "lp")) and "opgg" in (merged[-1].get("source"), r.get("source")):
            continue
        merged.append(r)
    out["lp_history"] = {**hist, "RANKED_SOLO_5x5": merged}
    out["opgg"] = {k: h[k] for k in ("previous_seasons", "url", "region", "updated_at")} | {"points": len(h["lp_points"])}
    return out


@app.post("/api/client/plan/ai")
def plan_ai():
    from fastapi import HTTPException
    from lol_review import assistant

    if not assistant.get().plan_ai():
        raise HTTPException(status_code=409, detail="现在没有对局计划，或者 AI 正在写")
    return {"ok": True}


@app.post("/api/opgg/renew")
def opgg_renew():
    from fastapi import HTTPException
    from lol_review import assistant

    r = assistant.get().renew_opgg()
    if r is None:
        raise HTTPException(status_code=409, detail="只有外服账号能在 OP.GG 刷新")
    return r


@app.get("/api/client/profile")
def client_profile(count: int = 30):
    """The logged-in account: ranks, playstyle tags (with kill participation), mastery, per-champion records."""
    from lol_review import assistant, lcu, playstyle

    def build():
        c = lcu.default()
        me = c.me() or {}
        if not me.get("puuid"):
            raise lcu.LCUError("客户端还没有登录")
        raw = c.history_pages(me["puuid"], max(10, min(count, 50)))
        rows = []
        for g in raw:
            r = playstyle.stat_row(g)
            if r:
                try:
                    r["team_kills"] = S._team_kills(c, g)
                except lcu.LCUError:
                    pass
                rows.append(r)
        prof = playstyle.profile(rows)
        ranked = c.ranked(me["puuid"])
        from lol_review import lp

        lp.record(me["puuid"], ranked)
        mastery = c.get("/lol-champion-mastery/v1/local-player/champion-mastery") or c.mastery(me["puuid"]) or []
        champs: dict[int, list] = {}
        for r in rows:
            champs.setdefault(r["champion_id"], []).append(r)
        top = sorted(champs.items(), key=lambda kv: -len(kv[1]))[:8]
        return {
            "name": me.get("gameName") or me.get("displayName"), "tag": me.get("tagLine", ""), "level": me.get("summonerLevel"),
            "icon": me.get("profileIconId"), "puuid": me["puuid"],
            "solo": assistant.rank_text(ranked), "flex": assistant.rank_text(ranked, "RANKED_FLEX_SR"),
            "playstyle": prof, **_with_opgg(me, lp.history(me["puuid"])),
            "champions": [{"champion_id": cid, "champion": c.champ_name(cid), "games": len(rs), "wins": sum(r["win"] for r in rs),
                           "kda": round(sum(r["kills"] + r["assists"] for r in rs) / max(sum(r["deaths"] for r in rs), 1), 2)} for cid, rs in top],
            "mastery": [{"champion_id": m.get("championId"), "champion": c.champ_name(m.get("championId", 0)), "level": m.get("championLevel"),
                         "points": m.get("championPoints")} for m in sorted(mastery if isinstance(mastery, list) else [], key=lambda m: -(m.get("championPoints") or 0))[:10]],
        }

    return _lcu_call(build)


@app.get("/api/client/game/{gid}")
def client_game(gid: int):
    """Full scoreboard of one finished game, both teams."""
    from lol_review import lcu

    return _lcu_call(lambda: lcu.scoreboard(lcu.default().game(gid), lcu.default()))


@app.get("/api/client/search")
def client_search(riot_id: str):
    """Find a player on your own server by name#TAG and return their card."""
    from fastapi import HTTPException
    from lol_review import assistant, lcu

    s = _lcu_call(lcu.default().lookup, riot_id)
    if not s or not s.get("puuid"):
        raise HTTPException(status_code=404, detail=f"在你所在的大区找不到 {riot_id}")
    card = _lcu_call(assistant.get().player, s["puuid"], assistant.get().settings["history_count"])
    return {"puuid": s["puuid"], **card}


class NoteIn(BaseModel):
    name: str = ""
    note: str | None = None
    tags: list[str] | None = None
    game: dict | None = None


@app.get("/api/notes")
def list_notes():
    from lol_review import notes

    return {"notes": notes.all_notes(), "quick_tags": notes.QUICK_TAGS}


@app.put("/api/notes/{puuid}")
def put_note(puuid: str, body: NoteIn):
    from lol_review import notes

    return notes.save(puuid, body.name, body.note, body.tags, body.game)


@app.delete("/api/notes/{puuid}")
def delete_note(puuid: str):
    from lol_review import notes

    notes.delete(puuid)
    return {"ok": True}


# ---------------------------------------------------------------- 国服 data, saved rune pages, overlay
@app.get("/api/tencent/champion/{cid}")
def tencent_champion(cid: int):
    from fastapi import HTTPException
    from lol_review import tencent

    try:
        return tencent.recommend(cid)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.get("/api/tencent/abilities/{cid}")
def tencent_abilities(cid: int):
    from fastapi import HTTPException
    from lol_review import tencent

    try:
        return tencent.abilities(cid)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.get("/api/client/rune-configs")
def get_rune_configs():
    from lol_review import assistant

    return assistant.rune_configs()


class RuneConfigIn(BaseModel):
    name: str = ""
    primary_style: int | None = None
    sub_style: int | None = None
    perks: list[int] | None = None
    from_client: bool = False  # save the client's current rune page


@app.put("/api/client/rune-configs/{cid}")
def put_rune_config(cid: int, body: RuneConfigIn):
    from fastapi import HTTPException
    from lol_review import assistant, lcu

    page = body.model_dump()
    if body.from_client:
        cur = _lcu_call(lcu.default().get, "/lol-perks/v1/currentpage") or {}
        if len(cur.get("selectedPerkIds") or []) != 9:
            raise HTTPException(status_code=409, detail="客户端当前没有完整的符文页")
        page = {"name": cur.get("name", ""), "primary_style": cur["primaryStyleId"], "sub_style": cur["subStyleId"], "perks": cur["selectedPerkIds"]}
    if not page.get("perks") or len(page["perks"]) != 9:
        raise HTTPException(status_code=400, detail="符文页需要 9 个符文")
    return assistant.save_rune_config(cid, page)


@app.delete("/api/client/rune-configs/{cid}")
def delete_rune_config(cid: int):
    from lol_review import assistant

    return assistant.save_rune_config(cid, None)


OVERLAY = {"toggle": None}  # the desktop app installs a callback that shows/hides the overlay window


@app.post("/api/overlay/toggle")
def overlay_toggle():
    from fastapi import HTTPException

    if not OVERLAY["toggle"]:
        raise HTTPException(status_code=409, detail="悬浮窗只在桌面版里可用")
    return {"visible": OVERLAY["toggle"]()}


@app.get("/overlay")
def overlay_page():
    return FileResponse(ROOT / "static" / "overlay.html")
