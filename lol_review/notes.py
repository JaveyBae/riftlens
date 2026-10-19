"""Player notes: what the user wrote about someone they played with, shown again next time they meet.

Stored per puuid in data/player_notes.json:
{puuid: {"name", "note", "tags": [...], "updated", "games": [{"game_id", "champion", "with": "ally"|"enemy", "t"}]}}
"""

from __future__ import annotations

import json
import threading
import time

from lol_review.riot import STATE

FILE = STATE / "data" / "player_notes.json"
QUICK_TAGS = ["好队友", "大腿", "会指挥", "心态好", "演员", "挂机", "喷子", "送人头", "抢资源", "开黑车队"]
_lock = threading.Lock()


def _load() -> dict:
    try:
        return json.loads(FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save(d: dict) -> None:
    FILE.parent.mkdir(parents=True, exist_ok=True)
    FILE.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")


def all_notes() -> list[dict]:
    return sorted(({"puuid": k, **v} for k, v in _load().items()), key=lambda n: -n.get("updated", 0))


def get(puuid: str) -> dict | None:
    n = _load().get(puuid)
    return {"puuid": puuid, **n} if n else None


def save(puuid: str, name: str = "", note: str | None = None, tags: list[str] | None = None, game: dict | None = None) -> dict:
    with _lock:
        d = _load()
        n = d.setdefault(puuid, {"name": name, "note": "", "tags": [], "games": [], "updated": 0})
        if name:
            n["name"] = name
        if note is not None:
            n["note"] = note.strip()[:500]
        if tags is not None:
            n["tags"] = [str(t)[:12] for t in tags][:8]
        if game and not any(g.get("game_id") == game.get("game_id") for g in n["games"]):
            n["games"] = ([{**game, "t": time.time()}] + n["games"])[:20]
        n["updated"] = time.time()
        if not n["note"] and not n["tags"]:
            d.pop(puuid)
            _save(d)
            return {"puuid": puuid, "deleted": True}
        _save(d)
        return {"puuid": puuid, **n}


def delete(puuid: str) -> None:
    with _lock:
        d = _load()
        if d.pop(puuid, None) is not None:
            _save(d)
