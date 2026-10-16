"""Build per-minute benchmarks for one champion + role + tier from sampled ranked games.

Sampling: list players of the tier (all four divisions), shuffle with a fixed
seed, read each player's recent Ranked Solo/Duo matches, keep matches on the
same patch where the champion was played in the role, and extract that
player's per-minute gold, XP and CS from the timeline. Each match is used once.

    .venv/bin/python -m lol_review.benchmark LeeSin JUNGLE DIAMOND 16.20 --n 40
"""

from __future__ import annotations

import argparse
import json
import random
import statistics
from pathlib import Path

from .riot import ROOT, Riot, RiotError

OUT = ROOT / "benchmarks"


def _pct(values: list[float], q: float) -> float:
    s = sorted(values)
    if not s:
        return float("nan")
    k = (len(s) - 1) * q
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def curve(timeline: dict, pid: int) -> list[dict]:
    rows = []
    for m, f in enumerate(timeline["info"]["frames"]):
        pf = f["participantFrames"][str(pid)]
        rows.append({"minute": m, "gold": pf["totalGold"], "xp": pf["xp"], "cs": pf["minionsKilled"] + pf["jungleMinionsKilled"]})
    return rows


def build(champion: str, role: str, tier: str, patch: str, n: int, max_players: int = 400, seed: int = 20261016,
          exclude: set[str] | None = None, log=print) -> dict:
    riot = Riot()
    exclude = exclude or set()
    players: list[str] = []
    for division in ("I", "II", "III", "IV"):
        url = f"https://{riot.platform}.api.riotgames.com/lol/league/v4/entries/RANKED_SOLO_5x5/{tier}/{division}?page=1"
        players += [e["puuid"] for e in riot._get(url, f"league_{tier}_{division}_p1", ttl=6 * 3600)]
    random.Random(seed).shuffle(players)

    samples, seen, scanned = [], set(), 0
    for puuid in players[:max_players]:
        if len(samples) >= n:
            break
        try:
            ids = riot.match_ids(puuid, count=10, queue=420)
        except RiotError as e:
            log(f"skip player: {e}")
            continue
        for mid in ids:
            if mid in seen or mid in exclude:
                continue
            seen.add(mid)
            try:
                m = riot.match(mid)
            except RiotError:
                continue
            scanned += 1
            if not m["info"]["gameVersion"].startswith(patch + "."):
                continue
            if m["info"]["gameDuration"] < 15 * 60:  # remakes and early surrenders distort curves
                continue
            hit = next((p for p in m["info"]["participants"]
                        if p["championName"] == champion and p.get("teamPosition") == role), None)
            if not hit:
                continue
            tl = riot.timeline(mid)
            samples.append({"match_id": mid, "win": hit["win"], "duration_s": m["info"]["gameDuration"],
                            "curve": curve(tl, hit["participantId"])})
            log(f"[{len(samples)}/{n}] {mid} win={hit['win']} (scanned {scanned} matches)")
            if len(samples) >= n:
                break

    minutes = max((len(s["curve"]) for s in samples), default=0)
    per_minute = []
    for mnt in range(minutes):
        row = {"minute": mnt}
        for key in ("gold", "xp", "cs"):
            vals = [s["curve"][mnt][key] for s in samples if len(s["curve"]) > mnt]
            row[key] = {"n": len(vals), "p25": _pct(vals, .25), "p50": _pct(vals, .5), "p75": _pct(vals, .75),
                        "mean": statistics.fmean(vals) if vals else None}
        per_minute.append(row)

    bench = {
        "champion": champion, "role": role, "tier": tier, "patch": patch, "queue": "RANKED_SOLO_5x5",
        "n_games": len(samples), "wins": sum(s["win"] for s in samples), "matches_scanned": scanned,
        "sampling": f"{tier} I–IV 每档第 1 页玩家，seed={seed} 打乱，每人最近 10 场单双排；同版本、时长 ≥ 15 分钟、每场只取一次",
        "per_minute": per_minute, "samples": samples,
    }
    OUT.mkdir(exist_ok=True)
    path = OUT / f"{champion}_{role}_{tier}_{patch}.json"
    path.write_text(json.dumps(bench))
    log(f"saved {path} with {len(samples)} games")
    return bench


def compare(bench: dict, target_curve: list[dict], key: str = "gold") -> list[dict]:
    out = []
    for row, t in zip(bench["per_minute"], target_curve):
        b = row[key]
        if b["n"] < 5:
            break
        v = t[key]
        band = "高于 P75" if v > b["p75"] else ("低于 P25" if v < b["p25"] else "P25–P75 之间")
        out.append({"minute": row["minute"], "you": v, "p25": round(b["p25"]), "p50": round(b["p50"]),
                    "p75": round(b["p75"]), "vs_median": round(v - b["p50"]), "band": band, "n": b["n"]})
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("champion")
    ap.add_argument("role")
    ap.add_argument("tier")
    ap.add_argument("patch")
    ap.add_argument("--n", type=int, default=40)
    ap.add_argument("--exclude", nargs="*", default=[])
    a = ap.parse_args()
    build(a.champion, a.role, a.tier, a.patch, a.n, exclude=set(a.exclude))
