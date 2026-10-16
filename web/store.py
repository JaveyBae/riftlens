"""SQLite storage for the web app: conversations, messages, the user profile and long-term memories.

Long-term memory has two sources:
- profile:  what the user fills in (Riot IDs, region, roles, rank, goals, preferences);
- memories: short facts the assistant decides to keep (via `web.bridge remember`),
            each with the message it came from, so the user can review and delete them.
Both are rendered by `memory_context()` and injected into every model prompt.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
from lol_review.riot import STATE

DB_PATH = STATE / "data" / "app.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
  id TEXT PRIMARY KEY, title TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL);
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  role TEXT NOT NULL, text TEXT NOT NULL, msg_id TEXT, tools TEXT, usage TEXT, created REAL NOT NULL);
CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conversation_id, id);
CREATE TABLE IF NOT EXISTS profile (id INTEGER PRIMARY KEY CHECK (id = 1), data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS memories (
  id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT NOT NULL, source_msg TEXT, created REAL NOT NULL);
"""

DEFAULT_PROFILE = {
    "display_name": "", "avatar": "", "avatar_color": "#b98a2c",
    "riot_ids": [], "region": "EUW", "roles": [], "rank": "", "goals": "", "detail": "标准",
}
MAX_AVATAR_CHARS = 200_000  # data URL of a ~128px image


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn


# ---------------------------------------------------------------- conversations
def list_conversations() -> list[dict]:
    with closing(connect()) as c:
        rows = c.execute("""SELECT cv.id, cv.title, cv.updated,
                                   (SELECT COUNT(*) FROM messages m WHERE m.conversation_id = cv.id) AS n
                            FROM conversations cv ORDER BY cv.updated DESC""").fetchall()
    return [dict(r) for r in rows]


def create_conversation(title: str = "新对话") -> str:
    cid, now = uuid.uuid4().hex[:10], time.time()
    with closing(connect()) as c, c:
        c.execute("INSERT INTO conversations VALUES (?, ?, ?, ?)", (cid, title, now, now))
    return cid


def rename_conversation(cid: str, title: str) -> None:
    with closing(connect()) as c, c:
        c.execute("UPDATE conversations SET title = ? WHERE id = ?", (title[:60], cid))


def delete_conversation(cid: str) -> None:
    with closing(connect()) as c, c:
        c.execute("DELETE FROM conversations WHERE id = ?", (cid,))


def get_messages(cid: str) -> list[dict]:
    with closing(connect()) as c:
        rows = c.execute("SELECT role, text, msg_id, tools, usage, created FROM messages WHERE conversation_id = ? ORDER BY id",
                         (cid,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["tools"] = json.loads(d["tools"]) if d["tools"] else []
        d["usage"] = json.loads(d["usage"]) if d["usage"] else None
        out.append(d)
    return out


def add_message(cid: str, role: str, text: str, msg_id: str | None = None,
                tools: list[str] | None = None, usage: dict | None = None) -> None:
    now = time.time()
    with closing(connect()) as c, c:
        exists = c.execute("SELECT title FROM conversations WHERE id = ?", (cid,)).fetchone()
        if not exists:
            c.execute("INSERT INTO conversations VALUES (?, ?, ?, ?)", (cid, "新对话", now, now))
        c.execute("INSERT INTO messages (conversation_id, role, text, msg_id, tools, usage, created) VALUES (?, ?, ?, ?, ?, ?, ?)",
                  (cid, role, text, msg_id, json.dumps(tools or [], ensure_ascii=False),
                   json.dumps(usage, ensure_ascii=False) if usage else None, now))
        c.execute("UPDATE conversations SET updated = ? WHERE id = ?", (now, cid))
        if role == "user" and (not exists or exists["title"] == "新对话"):
            c.execute("UPDATE conversations SET title = ? WHERE id = ?", (text.strip().replace("\n", " ")[:28] or "新对话", cid))


def history_for_model(cid: str, limit: int = 12) -> list[dict]:
    return [{"role": m["role"], "content": m["text"]} for m in get_messages(cid)[-limit:]]


# ---------------------------------------------------------------- profile & memories
def get_profile() -> dict:
    with closing(connect()) as c:
        row = c.execute("SELECT data FROM profile WHERE id = 1").fetchone()
    return {**DEFAULT_PROFILE, **(json.loads(row["data"]) if row else {})}


def save_profile(data: dict) -> dict:
    prof = {**get_profile(), **{k: v for k, v in data.items() if k in DEFAULT_PROFILE}}
    if isinstance(prof["riot_ids"], str):
        prof["riot_ids"] = [x.strip() for x in prof["riot_ids"].splitlines() if x.strip()]
    prof["riot_ids"] = [x for x in prof["riot_ids"] if "#" in x][:5]
    if prof["avatar"] and (len(prof["avatar"]) > MAX_AVATAR_CHARS or not prof["avatar"].startswith("data:image/")):
        raise ValueError("头像必须是不超过约 150KB 的图片")
    with closing(connect()) as c, c:
        c.execute("INSERT INTO profile (id, data) VALUES (1, ?) ON CONFLICT(id) DO UPDATE SET data = excluded.data",
                  (json.dumps(prof, ensure_ascii=False),))
    return prof


def list_memories() -> list[dict]:
    with closing(connect()) as c:
        return [dict(r) for r in c.execute("SELECT id, text, source_msg, created FROM memories ORDER BY id DESC").fetchall()]


def add_memory(text: str, source_msg: str | None = None) -> int:
    text = text.strip()[:200]
    with closing(connect()) as c, c:
        dup = c.execute("SELECT id FROM memories WHERE text = ?", (text,)).fetchone()
        if dup:
            return dup["id"]
        if c.execute("SELECT COUNT(*) FROM memories").fetchone()[0] >= 50:
            c.execute("DELETE FROM memories WHERE id = (SELECT MIN(id) FROM memories)")
        return c.execute("INSERT INTO memories (text, source_msg, created) VALUES (?, ?, ?)",
                         (text, source_msg, time.time())).lastrowid


def delete_memory(mid: int) -> None:
    with closing(connect()) as c, c:
        c.execute("DELETE FROM memories WHERE id = ?", (mid,))


def memory_context() -> str:
    """Profile + memories as a short block for the model prompt ('' if nothing is set)."""
    p, mems = get_profile(), list_memories()
    lines = []
    if p["display_name"]:
        lines.append(f"称呼：{p['display_name']}")
    if p["riot_ids"]:
        lines.append(f"用户自己的 Riot ID：{'、'.join(p['riot_ids'])}（用户说'我''我的账号'时指这些，默认第一个）")
    if p["region"]:
        lines.append(f"常玩大区：{p['region']}")
    if p["roles"]:
        lines.append(f"主玩位置：{'、'.join(p['roles'])}")
    if p["rank"]:
        lines.append(f"段位：{p['rank']}")
    if p["goals"]:
        lines.append(f"提升目标：{p['goals']}")
    if p["detail"]:
        lines.append(f"复盘详细程度偏好：{p['detail']}")
    if mems:
        lines.append("助手之前记下的长期特点（可能过时，与新数据冲突时以新数据为准）：")
        lines += [f"- {m['text']}" for m in mems[:15]]
    return "\n".join(lines)


def import_bridge_history() -> None:
    """One-time import of Q&A that predates the database (bridge inbox/outbox)."""
    from web import bridge

    with closing(connect()) as c:
        if c.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]:
            return
    inbox, outbox = bridge._read(bridge.INBOX), bridge._read(bridge.OUTBOX)
    if not inbox:
        return
    cid = create_conversation("早期对话（导入）")
    for m in inbox:
        ev = [e for e in outbox if e["msg_id"] == m["id"]]
        answer = next((e["data"]["text"] for e in ev if e["event"] == "answer"), None)
        add_message(cid, "user", m["text"], m["id"])
        if answer:
            add_message(cid, "assistant", answer, m["id"], [e["data"]["name"] for e in ev if e["event"] == "tool_call"])
    rename_conversation(cid, "早期对话（导入）")
