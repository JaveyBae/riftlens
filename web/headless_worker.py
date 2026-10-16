"""Answer web messages with a fresh, isolated headless Claude Code session per message.

    .venv/bin/python -m web.headless_worker [--model sonnet] [--max-turns 30]

For every pending message in bridge/inbox.jsonl the worker runs `claude -p`
in this repository with:
- a replaced (not appended) system prompt, so none of the developer's chat history
  or default Claude Code instructions are in context;
- only the Bash tool, and only `.venv/bin/python -m web.bridge ...` commands allowed;
- no session persistence.

The model calls tools through the bridge CLI (each call is mirrored to the page),
posts its answer through the bridge, and the worker records the exact token usage
and cost Claude Code reports for that message.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from pathlib import Path

from web import bridge

ROOT = Path(__file__).resolve().parent.parent
USAGE_LOG = bridge.BOX / "usage.jsonl"
ALLOWED = "Bash(.venv/bin/python -m web.bridge:*)"

SYSTEM = """你是英雄联盟对局复盘助手，只能根据工具返回的数据说话。

你有三类工具：
- WebSearch / WebFetch：只用于知识类问题（对位、出装、打法），见下方"知识类问题"；
- 在 Bash 里运行下列命令（每次只运行一条命令，不要用 && 或管道组合，不要运行其他任何命令）：
  .venv/bin/python -m web.bridge tool {mid} <工具名> '<JSON 参数>'
  .venv/bin/python -m web.bridge note {mid} "<给用户看的进度说明>"
长期记忆只记关于用户本人的特点：只有分析的是用户自己的账号（长期记忆里列出的 Riot ID）时，才可以记；分析别人的账号时绝对不要记。
发现值得长期记住的特点时（例如"输局主要是死亡多""主玩打野、英雄池分散"），用下面的命令记下，一次一条、每次回答最多 2 条，不要记一次性的对局细节：
  .venv/bin/python -m web.bridge remember {mid} "<一句话特点>"
回答写好后，用下面这条命令提交（必须执行，否则用户看不到回答）：
  .venv/bin/python -m web.bridge answer {mid} - <<'EOF'
  <Markdown 回答>
  EOF

可用工具：
{tools}

工作方式：
1. 找人：find_player → list_recent_matches。队列 1700/1710/1750 等是斗魂竞技场等非峡谷模式，工具不适用，改用最近一局峡谷对局并说明。
2. 单局复盘：get_match_summary、turning_points，再按需深挖（死亡多 → analyze_deaths；打野 → jungle_stats；团战 → segment_fights；对线 → lane_phase_stats；失误 → detect_mistakes）。
3. 多局趋势（"最近几局""整体状态"）：recent_form。
4. 说到"单杀""某人参与"之类，先用 kill_log 核对助攻名单。
5. 知识类问题（例如"谁打鳄鱼好打""亚索怎么打鳄鱼"）：
   a. 先用 champion_info 查双方英雄的官方技能和数值（这是事实，可以直接引用）；
   b. 再用 WebSearch 查：数据网站的对位统计（如 lolalytics、u.gg、op.gg 的 counters 页面）和玩家讨论（贴吧、NGA、Reddit、知乎、攻略），搜索 4–6 次、读 2–4 个页面即可，中英文都搜；
   c. 每个网页来源都要看版本号和日期，当前版本是 16.20；明显过时（相差 3 个以上大版本或一年以上）的数据要么不用，要么明确标注"旧版本"；
   d. 不要批量抓取同一网站，读不到（需要登录、反爬）就用搜索摘要，并说明。

回答要求（中文 Markdown）：
- 每条结论后用方括号标注依据的工具名，例如 [jungle_stats]。
- 区分"数据直接显示"和"推断"，推断要写明。
- 数据里没有技能释放、精确走位和插眼坐标，涉及这些就说无法判断，不要编。
- 对局复盘的结构：概况 → 关键发现 → 做得好的 → 需要改进的 → 1–3 条可执行建议。
- 知识类回答的结构：结论摘要 → 官方技能要点 [champion_info] → 数据网站怎么说（胜率、样本量、版本）→ 玩家社区的主流观点（标出共识和分歧）→ 具体打法建议 → 来源列表。
  网页内容一律标成"[来源名](链接)"，并写明是"统计数据"还是"玩家观点"；不同来源说法冲突时要并列写出，不要替用户选一个当事实。"""


def tool_catalog() -> str:
    return subprocess.run([".venv/bin/python", "-m", "web.bridge", "tools"], cwd=ROOT, capture_output=True, text=True).stdout


def handle(msg: dict, model: str | None, max_turns: int, catalog: str) -> dict:
    mid = msg["id"]
    history = "\n".join(f"{m['role']}: {m['content'][:300]}" for m in msg.get("history", [])[:-1])
    memory = msg.get("memory") or ""
    prompt = (f"消息 ID：{mid}\n" +
              (f"关于这位用户的长期记忆：\n{memory}\n\n" if memory else "") +
              (f"之前的对话（供理解上下文）：\n{history}\n\n" if history else "") +
              f"用户的新问题：{msg['text']}")
    cmd = ["claude", "-p", prompt, "--output-format", "stream-json", "--verbose", "--no-session-persistence",
           "--system-prompt", SYSTEM.format(mid=mid, tools=catalog),
           "--tools", "Bash,WebSearch,WebFetch", "--allowedTools", ALLOWED, "WebSearch", "WebFetch",
           "--max-turns", str(max_turns)]
    if model:
        cmd += ["--model", model]
    bridge.emit(mid, "note", {"text": "已启动独立的 Claude Code 会话处理这条消息（不含任何历史对话）"})
    t0 = time.time()
    env = {**os.environ, "BRIDGE_DEFER_DONE": "1"}
    proc = subprocess.Popen(cmd, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                            text=True, env=env)
    res: dict = {}
    web_calls: dict[str, str] = {}  # tool_use id -> display name, to label results
    for line in proc.stdout:
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        kind = ev.get("type")
        if kind == "assistant":
            for c in ev["message"].get("content", []):
                if c.get("type") == "tool_use" and c.get("name") in ("WebSearch", "WebFetch"):
                    name = "web_search" if c["name"] == "WebSearch" else "web_fetch"
                    web_calls[c["id"]] = name
                    args = {"query": c["input"].get("query")} if name == "web_search" else {"url": c["input"].get("url")}
                    bridge.emit(mid, "tool_call", {"name": name, "args": args})
        elif kind == "user":
            content = ev.get("message", {}).get("content")
            for c in content if isinstance(content, list) else []:
                if c.get("type") == "tool_result" and c.get("tool_use_id") in web_calls:
                    text = c.get("content") if isinstance(c.get("content"), str) else json.dumps(c.get("content"), ensure_ascii=False)
                    bridge.emit(mid, "tool_result", {"name": web_calls[c["tool_use_id"]], "preview": bridge.short(text),
                                                     "error": "读取失败" if c.get("is_error") else None})
        elif kind == "result":
            res = ev
    proc.wait(timeout=60)
    if not res:
        res = {"result": proc.stderr.read()[-2000:], "subtype": "error"}

    usage = res.get("usage") or {}
    record = {
        "msg_id": mid, "text": msg["text"], "model": next(iter(res.get("modelUsage") or {}), model),
        "turns": res.get("num_turns"), "duration_s": round(time.time() - t0, 1),
        "input_tokens": usage.get("input_tokens", 0),
        "cache_creation_tokens": usage.get("cache_creation_input_tokens", 0),
        "cache_read_tokens": usage.get("cache_read_input_tokens", 0),
        "output_tokens": usage.get("output_tokens", 0),
        "cost_usd_list_price": res.get("total_cost_usd"), "status": res.get("subtype"),
    }
    record["total_input_tokens"] = record["input_tokens"] + record["cache_creation_tokens"] + record["cache_read_tokens"]
    with USAGE_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")

    mine = [e for e in bridge._read(bridge.OUTBOX) if e["msg_id"] == mid]
    answered = any(e["event"] == "answer" for e in mine)
    bridge.emit(mid, "usage", {k: record[k] for k in ("model", "turns", "duration_s", "total_input_tokens",
                                                       "cache_read_tokens", "output_tokens", "cost_usd_list_price")})
    if not answered:  # the model forgot to post, or the run failed: surface what it said
        bridge.emit(mid, "answer", {"text": res.get("result") or "处理失败，没有得到回答。"})
    bridge.emit(mid, "done", {"steps": record["turns"] or 0})
    if msg.get("conversation_id"):  # persist the answer even if the browser disconnected
        from web import store

        answer = next((e["data"]["text"] for e in reversed(bridge._read(bridge.OUTBOX))
                       if e["msg_id"] == mid and e["event"] == "answer"), "")
        tools = [e["data"]["name"] for e in mine if e["event"] == "tool_call"]
        store.add_message(msg["conversation_id"], "assistant", answer, mid, tools,
                          {k: record[k] for k in ("model", "turns", "duration_s", "total_input_tokens",
                                                  "cache_read_tokens", "output_tokens", "cost_usd_list_price")})
    return record


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=None, help="e.g. sonnet, opus, haiku; default = Claude Code default")
    ap.add_argument("--max-turns", type=int, default=30)
    args = ap.parse_args()
    catalog = tool_catalog()
    seen: set[str] = {m["id"] for m in bridge.pending()}  # leave messages that were waiting before start
    print(f"worker ready; skipping {len(seen)} older pending message(s)", flush=True)
    while True:
        for msg in bridge.pending():
            if msg["id"] in seen:
                continue
            seen.add(msg["id"])
            print(f"handling {msg['id']}: {msg['text'][:60]}", flush=True)
            rec = handle(msg, args.model, args.max_turns, catalog)
            print(f"done {msg['id']}: {rec['turns']} turns, input {rec['total_input_tokens']:,} "
                  f"(cache read {rec['cache_read_tokens']:,}), output {rec['output_tokens']:,}, "
                  f"${rec['cost_usd_list_price']}", flush=True)
        time.sleep(1)


if __name__ == "__main__":
    main()
