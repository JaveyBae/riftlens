"""Riot API client with on-disk caching and rate-limit handling.

Match data never changes once a game ends, so match and timeline responses are
cached forever. Account lookups and match lists are cached briefly.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from urllib.parse import quote

import requests

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "cache"  # public match data, safe to share between state dirs
# Per-user state (database, traces, mailbox, saved key). Override to run an isolated demo.
STATE = Path(os.environ.get("RIFTLENS_STATE_DIR", ROOT)).resolve()
KEY_FILE = STATE / "data" / "riot_api_key"


def saved_key() -> str:
    try:
        return KEY_FILE.read_text().strip()
    except OSError:
        return ""


def save_key(key: str) -> None:
    KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
    KEY_FILE.write_text(key.strip())
    KEY_FILE.chmod(0o600)


def key_status() -> dict:
    load_env()
    env_key = os.environ.get("RIOT_API_KEY", "").strip()
    key = env_key or saved_key()
    return {"configured": bool(key), "source": "env" if env_key else ("settings" if key else None),
            "masked": (key[:6] + "…" + key[-4:]) if len(key) > 12 else ""}
DDRAGON = "https://ddragon.leagueoflegends.com"


def load_env() -> None:
    env = ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


class RiotError(RuntimeError):
    pass


class Riot:
    def __init__(self) -> None:
        load_env()
        self.key = os.environ.get("RIOT_API_KEY", "").strip() or saved_key()  # optional
        self.platform = os.environ.get("RIOT_PLATFORM", "euw1")
        self.region = os.environ.get("RIOT_REGION", "europe")
        self.session = requests.Session()
        CACHE.mkdir(exist_ok=True)

    # ---- low level -------------------------------------------------------
    tracer = None  # set to a Tracer to record every request
    # Development-key limits are 20 requests / 1 s and 100 requests / 120 s; stay just under.
    LIMITS = ((18, 1.0), (95, 120.0))
    _sent: list[float] = []

    def _throttle(self) -> None:
        while True:
            now = time.time()
            self._sent[:] = [t for t in self._sent if now - t < 120.0]
            wait = 0.0
            for n, window in self.LIMITS:
                recent = [t for t in self._sent if now - t < window]
                if len(recent) >= n:
                    wait = max(wait, window - (now - recent[0]) + 0.05)
            if wait <= 0:
                self._sent.append(now)
                return
            time.sleep(wait)

    def _get(self, url: str, cache_key: str | None = None, ttl: float | None = None, auth: bool = True):
        path = CACHE / f"{cache_key}.json" if cache_key else None
        if path and path.exists() and (ttl is None or time.time() - path.stat().st_mtime < ttl):
            if self.tracer:
                self.tracer.api(url, cache_hit=True, status=None, latency_ms=0)
            return json.loads(path.read_text())
        if auth:
            self._throttle()
        started = time.perf_counter()
        if auth and not self.key:
            raise RiotError("未配置 Riot API key：查询对局需要它。请在网页左下角的设置里填写，或写入 .env 的 RIOT_API_KEY。")
        headers = {"X-Riot-Token": self.key} if auth else {}
        for attempt in range(4):
            r = self.session.get(url, headers=headers, timeout=15)
            if r.status_code == 429:
                time.sleep(float(r.headers.get("Retry-After", 2)) + 0.5)
                continue
            if r.status_code in (500, 502, 503, 504) and attempt < 3:
                time.sleep(1 + attempt)
                continue
            break
        if self.tracer:
            self.tracer.api(url, cache_hit=False, status=r.status_code, latency_ms=(time.perf_counter() - started) * 1000)
        if r.status_code == 401:
            raise RiotError("Riot API returned 401: the API key is invalid or expired (development keys expire every 24 hours).")
        if r.status_code == 403:
            raise RiotError("Riot API returned 403: the key is not allowed to call this endpoint.")
        if r.status_code == 404:
            raise RiotError(f"Riot API returned 404 for {url.split('?')[0]}")
        r.raise_for_status()
        data = r.json()
        if path:
            path.write_text(json.dumps(data))
        return data

    # ---- endpoints -------------------------------------------------------
    def account_by_riot_id(self, game_name: str, tag_line: str) -> dict:
        url = f"https://{self.region}.api.riotgames.com/riot/account/v1/accounts/by-riot-id/{quote(game_name)}/{quote(tag_line)}"
        return self._get(url, f"account_{game_name.lower()}_{tag_line.lower()}", ttl=86400)

    def match_ids(self, puuid: str, count: int = 10, queue: int | None = None) -> list[str]:
        q = f"&queue={queue}" if queue else ""
        url = f"https://{self.region}.api.riotgames.com/lol/match/v5/matches/by-puuid/{puuid}/ids?start=0&count={count}{q}"
        return self._get(url, f"ids_{puuid[:16]}_{count}_{queue}", ttl=120)

    def match(self, match_id: str) -> dict:
        url = f"https://{self.region}.api.riotgames.com/lol/match/v5/matches/{match_id}"
        return self._get(url, f"match_{match_id}")

    def timeline(self, match_id: str) -> dict:
        url = f"https://{self.region}.api.riotgames.com/lol/match/v5/matches/{match_id}/timeline"
        return self._get(url, f"timeline_{match_id}")

    def league_entries(self, puuid: str) -> list[dict]:
        url = f"https://{self.platform}.api.riotgames.com/lol/league/v4/entries/by-puuid/{puuid}"
        return self._get(url, f"league_{puuid[:16]}", ttl=600)

    # ---- static data (no key needed) --------------------------------------
    def ddragon_version(self, game_version: str) -> str:
        versions = self._get(f"{DDRAGON}/api/versions.json", "ddragon_versions", ttl=86400, auth=False)
        major_minor = ".".join(game_version.split(".")[:2])
        for v in versions:
            if v.startswith(major_minor + "."):
                return v
        return versions[0]

    def champions(self, version: str, lang: str = "zh_CN") -> dict:
        return self._get(f"{DDRAGON}/cdn/{version}/data/{lang}/champion.json", f"champs_{lang}_{version}", auth=False)["data"]

    def champion(self, champ_id: str, version: str, lang: str = "zh_CN") -> dict:
        url = f"{DDRAGON}/cdn/{version}/data/{lang}/champion/{champ_id}.json"
        return self._get(url, f"champ_{lang}_{champ_id}_{version}", auth=False)["data"][champ_id]

    def items(self, game_version: str) -> dict:
        v = self.ddragon_version(game_version)
        return self._get(f"{DDRAGON}/cdn/{v}/data/en_US/item.json", f"items_{v}", auth=False)["data"]
