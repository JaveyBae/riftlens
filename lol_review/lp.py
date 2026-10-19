"""LP history recorded by RiftLens itself.

The client only reports the current rank (plus this and last season's highest),
not past LP, and OP.GG does not cover the Tencent servers. So RiftLens saves a
snapshot whenever the logged-in account's rank changes, and the LP curve grows
from the day it starts recording.

data/lp_history.json: {puuid: {"RANKED_SOLO_5x5": [{"t", "tier", "division", "lp", "wins", "losses"}, ...], ...}}
"""

from __future__ import annotations

import json
import threading
import time

from lol_review.riot import STATE

FILE = STATE / "data" / "lp_history.json"
QUEUES = ("RANKED_SOLO_5x5", "RANKED_FLEX_SR")
ORDER = ["IRON", "BRONZE", "SILVER", "GOLD", "PLATINUM", "EMERALD", "DIAMOND", "MASTER", "GRANDMASTER", "CHALLENGER"]
DIVS = {"IV": 0, "III": 1, "II": 2, "I": 3}
_lock = threading.Lock()


def score(tier: str, division: str, lp: int) -> int | None:
    """One number for the chart: 400 per tier below Master, 100 per division, plus LP; Master and above share one scale."""
    if tier not in ORDER:
        return None
    i = ORDER.index(tier)
    if i >= ORDER.index("MASTER"):
        return ORDER.index("MASTER") * 400 + lp
    return i * 400 + DIVS.get(division, 0) * 100 + lp


def _load() -> dict:
    try:
        return json.loads(FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def record(puuid: str, ranked: dict | None) -> list[str]:
    """Append a snapshot for each queue whose LP / tier / record changed. Returns the queues that changed."""
    qm = (ranked or {}).get("queueMap") or {}
    changed = []
    with _lock:
        d = _load()
        mine = d.setdefault(puuid, {})
        for q in QUEUES:
            e = qm.get(q) or {}
            tier = e.get("tier") or ""
            if tier in ("", "NONE", "UNRANKED") or e.get("isProvisional"):
                continue
            snap = {"tier": tier, "division": e.get("division") or "", "lp": e.get("leaguePoints", 0),
                    "wins": e.get("wins", 0), "losses": e.get("losses", 0)}
            rows = mine.setdefault(q, [])
            if rows and all(rows[-1].get(k) == v for k, v in snap.items()):
                continue
            rows.append({"t": time.time(), **snap})
            del rows[:-500]
            changed.append(q)
        if changed:
            FILE.parent.mkdir(parents=True, exist_ok=True)
            FILE.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    return changed


def history(puuid: str) -> dict:
    out = {}
    for q, rows in (_load().get(puuid) or {}).items():
        out[q] = [{**r, "score": score(r["tier"], r["division"], r["lp"])} for r in rows]
    return out
