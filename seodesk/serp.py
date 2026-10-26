"""Google rank checks through Serper (https://serper.dev): where the site sits for each keyword in its site file,
which domains hold the first page, and Google's "People also ask" questions (the AEO raw material). Serper's terms
allow this; scraping Google directly doesn't."""
from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import httpx

from .errors import FatalError, LimitReached, SearchError
from .settings import Keyword

# Google stopped honouring num=100 in Sept 2025: every search returns one page of 10, whatever `num` says.
# So "top 20" takes a second search (page 2) for a keyword that isn't on page 1.
PAGES = 2


class Serper:
    def __init__(self, api_key: str, http: httpx.AsyncClient, max_calls: int):
        self.api_key, self.http, self.max_calls, self.calls = api_key, http, max_calls, 0

    async def search(self, q: str, country: str = "us", page: int = 1, language: str = "en") -> Dict[str, Any]:
        if not self.api_key:
            raise FatalError("SERPER_API_KEY isn't set in .env")
        if self.calls >= self.max_calls:
            raise LimitReached(f"search limit reached ({self.max_calls} searches)")
        self.calls += 1
        payload: Dict[str, Any] = {"q": q, "gl": country, "hl": language}
        if page > 1:
            payload["page"] = page
        for attempt in range(3):
            if attempt:
                await asyncio.sleep(3 * attempt)
            try:
                r = await self.http.post("https://google.serper.dev/search", json=payload,
                                         headers={"X-API-KEY": self.api_key}, timeout=30.0)
            except httpx.HTTPError:
                continue
            if r.status_code in (401, 403):
                raise FatalError("Serper rejected the API key - check SERPER_API_KEY in .env")
            if r.status_code == 429 or r.status_code >= 500:
                continue
            if r.status_code >= 400:
                if "credit" in r.text.lower():
                    raise FatalError("Serper account is out of credits")
                raise SearchError(f"Serper returned {r.status_code}: {r.text[:200]}")
            return r.json()
        raise SearchError(f"Serper didn't answer after 3 tries for: {q}")


def domain(url: str) -> str:
    return urlparse(url).netloc.lower().removeprefix("www.")


def read_result(data: Dict[str, Any], host: str, offset: int = 0) -> Dict[str, Any]:
    """Our position (1-based, None if absent), the URL that ranks, the first domains and the PAA questions.
    `offset` is 10 for page 2, whose results Serper numbers either 1-10 or 11-20."""
    organic = data.get("organic") or []
    ours = host.lower().removeprefix("www.")
    position: Optional[int] = None
    url = ""
    for i, r in enumerate(organic, 1):
        if domain(r.get("link", "")) == ours:
            pos = int(r.get("position") or i)
            position = pos + offset if offset and pos <= offset else pos
            url = r.get("link", "")
            break
    return {
        "position": position,
        "url": url,
        "top": [domain(r.get("link", "")) for r in organic[:10]],
        "questions": [q.get("question", "") for q in (data.get("peopleAlsoAsk") or []) if q.get("question")][:6],
    }


async def check_all(serper: Serper, keywords: List[Keyword], host: str, language: str = "en") -> Dict[str, Any]:
    rows: List[Dict[str, Any]] = []
    errors: List[str] = []
    for k in keywords:
        try:
            data = await serper.search(k.keyword, k.country, language=language)
        except LimitReached as e:
            errors.append(str(e))
            break
        except SearchError as e:
            errors.append(f"{k.keyword}: {e}")
            continue
        found = read_result(data, host)
        if found["position"] is None and PAGES > 1:
            try:
                second = read_result(await serper.search(k.keyword, k.country, page=2, language=language), host, offset=10)
                if second["position"] is not None:
                    found.update(position=second["position"], url=second["url"])
            except (LimitReached, SearchError) as e:
                errors.append(f"{k.keyword} page 2: {e}")
        rows.append({"keyword": k.keyword, "country": k.country, "tier": k.tier, "volume": k.volume,
                     "target": k.target, **found})
    return {"rows": rows, "errors": errors, "calls": serper.calls}
