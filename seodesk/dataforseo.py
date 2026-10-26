"""DataForSEO (https://dataforseo.com, pay as you go): two things Serper can't give.

- Search volumes for the keyword plan (Keywords Data, Google Ads search volume): the real monthly searches, instead
  of the ranges typed into the site files. Refreshed every `volume_every_days` (default 28) or when the plan changes.
- The keywords Google ranks the site for (DataForSEO Labs, ranked keywords): how many keywords, in which positions,
  and the estimated monthly clicks, without needing the site's Search Console. Refreshed every `ranked_every_days`
  (default 7).

Logins go in .env as DATAFORSEO_LOGIN and DATAFORSEO_PASSWORD (the API login and API password from the DataForSEO
dashboard, not the account password). Every response says what it cost; that goes into the spend table."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import httpx

from .errors import FatalError, LimitReached, SearchError
from .settings import Keyword

BASE = "https://api.dataforseo.com/v3"
# Google Ads location codes for the 2-letter countries the site files use (a country's code is 2000 + its ISO 3166
# numeric code). A country not listed here falls back to the US.
LOCATIONS = {"us": 2840, "gb": 2826, "uk": 2826, "ca": 2124, "au": 2036, "nz": 2554, "ie": 2372, "in": 2356,
             "pk": 2586, "ae": 2784, "sa": 2682, "qa": 2634, "my": 2458, "id": 2360, "sg": 2702, "ph": 2608,
             "de": 2276, "fr": 2250, "es": 2724, "it": 2380, "nl": 2528, "se": 2752, "pl": 2616, "tr": 2792,
             "br": 2076, "mx": 2484, "jp": 2392, "za": 2710, "ng": 2566}
OK = 20000


class DataForSEO:
    def __init__(self, login: str, password: str, http: httpx.AsyncClient, max_calls: int = 10):
        self.auth = (login, password)
        self.http, self.max_calls, self.calls, self.cost = http, max_calls, 0, 0.0

    async def call(self, path: str, tasks: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        """The first task of a POST (or a GET when `tasks` is None). Raises FatalError for a bad login or an empty
        balance (no point trying more calls), SearchError for anything else."""
        if not all(self.auth):
            raise FatalError("DATAFORSEO_LOGIN / DATAFORSEO_PASSWORD aren't set in .env")
        if self.calls >= self.max_calls:
            raise LimitReached(f"DataForSEO call limit reached ({self.max_calls})")
        self.calls += 1
        for attempt in range(3):
            if attempt:
                await asyncio.sleep(3 * attempt)
            try:
                if tasks is None:
                    r = await self.http.get(f"{BASE}{path}", auth=self.auth, timeout=60.0)
                else:
                    r = await self.http.post(f"{BASE}{path}", json=tasks, auth=self.auth, timeout=60.0)
            except httpx.HTTPError:
                continue
            if r.status_code == 401:
                raise FatalError("DataForSEO rejected the login - check DATAFORSEO_LOGIN and DATAFORSEO_PASSWORD in .env")
            if r.status_code == 429 or r.status_code >= 500:
                continue
            try:
                data = r.json()
            except ValueError:
                raise SearchError(f"DataForSEO returned {r.status_code}: {r.text[:200]}") from None
            self.cost += float(data.get("cost") or 0)
            message = str(data.get("status_message") or "")
            if data.get("status_code") != OK:
                if data.get("status_code") in (40100, 40101) or "auth" in message.lower():
                    raise FatalError(f"DataForSEO login refused: {message}")
                if data.get("status_code") == 40200 or "balance" in message.lower() or "payment" in message.lower():
                    raise FatalError(f"DataForSEO account is out of money: {message}")
                raise SearchError(f"DataForSEO: {message or r.status_code}")
            task = (data.get("tasks") or [{}])[0]
            if task.get("status_code") != OK:
                raise SearchError(f"DataForSEO {path}: {task.get('status_message') or 'task failed'}")
            return task
        raise SearchError(f"DataForSEO didn't answer after 3 tries ({path})")

    async def balance(self) -> float:
        task = await self.call("/appendix/user_data")
        return float((((task.get("result") or [{}])[0].get("money") or {}).get("balance")) or 0)


def location(country: str) -> int:
    return LOCATIONS.get(country.lower(), LOCATIONS["us"])


def target(host: str) -> str:
    """DataForSEO Labs wants the bare domain: no scheme, no www."""
    return host.lower().removeprefix("www.")


def plan_key(keywords: List[Keyword]) -> List[str]:
    return sorted(f"{k.keyword.lower()}|{k.country}" for k in keywords)


async def volumes(client: DataForSEO, keywords: List[Keyword], language: str = "en") -> Dict[str, Any]:
    """{by_keyword: {"keyword|country": {volume, cpc, competition, trend: [12 monthly volumes, oldest first]}},
    plan: [...], errors: [...]}. One task per country."""
    out: Dict[str, Any] = {"by_keyword": {}, "plan": plan_key(keywords), "errors": []}
    by_country: Dict[str, List[str]] = {}
    for k in keywords:
        by_country.setdefault(k.country, []).append(k.keyword.lower())
    for country, words in by_country.items():
        try:
            task = await client.call("/keywords_data/google_ads/search_volume/live", [
                {"keywords": sorted(set(words)), "location_code": location(country), "language_code": language}])
        except (SearchError, LimitReached) as e:
            out["errors"].append(f"volumes ({country}): {e}")
            continue
        for r in task.get("result") or []:
            months = sorted(r.get("monthly_searches") or [], key=lambda m: (m.get("year", 0), m.get("month", 0)))
            out["by_keyword"][f"{str(r.get('keyword', '')).lower()}|{country}"] = {
                "volume": r.get("search_volume"), "cpc": r.get("cpc"), "competition": r.get("competition"),
                "trend": [m.get("search_volume") for m in months][-12:]}
    return out


async def ranked(client: DataForSEO, host: str, country: str, limit: int = 100, language: str = "en") -> Dict[str, Any]:
    """What Google ranks the domain for in one country: totals by position band, estimated monthly clicks (etv),
    and the top keywords by search volume."""
    task = await client.call("/dataforseo_labs/google/ranked_keywords/live", [
        {"target": target(host), "location_code": location(country), "language_code": language, "limit": limit,
         "order_by": ["keyword_data.keyword_info.search_volume,desc"]}])
    res = (task.get("result") or [{}])[0] or {}
    organic = ((res.get("metrics") or {}).get("organic")) or {}
    items = []
    for it in res.get("items") or []:
        kd = it.get("keyword_data") or {}
        serp = ((it.get("ranked_serp_element") or {}).get("serp_item")) or {}
        items.append({"keyword": kd.get("keyword", ""), "volume": (kd.get("keyword_info") or {}).get("search_volume"),
                      "position": serp.get("rank_group"), "url": serp.get("url", ""),
                      "etv": round(float(serp.get("etv") or 0), 1)})
    return {
        "country": country,
        "total": int(res.get("total_count") or organic.get("count") or 0),
        "top3": int(organic.get("pos_1") or 0) + int(organic.get("pos_2_3") or 0),
        "top10": sum(int(organic.get(k) or 0) for k in ("pos_1", "pos_2_3", "pos_4_10")),
        "top20": sum(int(organic.get(k) or 0) for k in ("pos_1", "pos_2_3", "pos_4_10", "pos_11_20")),
        "etv": round(float(organic.get("etv") or 0), 1),
        "new": int(organic.get("is_new") or 0), "up": int(organic.get("is_up") or 0),
        "down": int(organic.get("is_down") or 0), "lost": int(organic.get("is_lost") or 0),
        "items": items,
    }


def _age_days(snap: Optional[Dict[str, Any]]) -> float:
    if not snap or not snap.get("_at"):
        return 1e9
    taken = datetime.strptime(snap["_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - taken) / timedelta(days=1)


def due(snap: Optional[Dict[str, Any]], every_days: float) -> bool:
    """Daily runs drift by a few minutes, so "every 7 days" allows a quarter of a day early."""
    return _age_days(snap) >= every_days - 0.25


def volumes_due(snap: Optional[Dict[str, Any]], keywords: List[Keyword], every_days: float) -> bool:
    if not keywords:
        return False
    return due(snap, every_days) or (snap or {}).get("plan") != plan_key(keywords)


def volume_for(snap: Optional[Dict[str, Any]], keyword: str, country: str) -> Tuple[Optional[int], List[Any]]:
    row = ((snap or {}).get("by_keyword") or {}).get(f"{keyword.lower()}|{country}") or {}
    return row.get("volume"), row.get("trend") or []
