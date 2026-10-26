"""App-store listings, for a site that also sells a WordPress.org plugin or a Shopify App Store app. Each store is a
search engine of its own, ranked mostly on installs, reviews and the listing's name, so the daily run records the
numbers each store ranks on, where the plugin stands in WordPress.org search (through WordPress.org's public API), and
any text that must not be on the Shopify listing (old copy waiting to be replaced, for example).

Shopify App Store search positions are NOT checked: apps.shopify.com/robots.txt disallows `*q=*` for crawlers, so a
scheduled bot doesn't run searches there. The listing page itself is allowed. Check Shopify positions by hand."""
from __future__ import annotations

import json
import re
from html import unescape
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode

import httpx

WP_API = "https://api.wordpress.org/plugins/info/1.2/"
WP_STATS = "https://api.wordpress.org/stats/plugin/1.0/downloads.php"
WP_CORE = "https://api.wordpress.org/core/version-check/1.7/"
SHOPIFY = "https://apps.shopify.com"


def wp_major(version: str) -> Optional[int]:
    """'7.1.3' -> 71. WordPress majors run x.0 to x.9, so x*10 + y counts them in order (6.9 -> 7.0 is one step)."""
    m = re.match(r"(\d+)\.(\d+)", version or "")
    return int(m.group(1)) * 10 + int(m.group(2)) if m else None


def majors_behind(tested: str, latest: str) -> Optional[int]:
    """How many major releases the readme's 'Tested up to' trails the newest WordPress. WordPress.org shows a
    'hasn't been tested with the latest 3 major releases' warning at 3, which also costs search placement."""
    t, l = wp_major(tested), wp_major(latest)
    return None if t is None or l is None else max(0, l - t)


def shopify_listing(html: str, must_not: List[str]) -> Dict[str, Any]:
    """What the listing page shows: its title, reviews and rating, and any text that must not be there."""
    title = re.search(r"<title>(.*?)</title>", html, re.S)
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", re.sub(r"<script.*?</script>|<style.*?</style>", " ", html, flags=re.S)))
    reviews: Optional[int] = None
    rating: Optional[float] = None
    for block in re.findall(r'<script type="application/ld\+json">(.*?)</script>', html, re.S):
        try:
            data = json.loads(block)
        except ValueError:
            continue
        for node in data if isinstance(data, list) else [data]:
            agg = node.get("aggregateRating") if isinstance(node, dict) else None
            if agg:
                reviews = int(str(agg.get("ratingCount") or agg.get("reviewCount") or 0).replace(",", ""))
                rating = float(agg.get("ratingValue") or 0) or None
    if reviews is None:
        if re.search(r"\bNo reviews\b", text):
            reviews = 0
        else:
            m = re.search(r"\(([\d,]+)\)\s*(?:reviews|Reviews)|([\d,]+) reviews?\b", text)
            reviews = int((m.group(1) or m.group(2)).replace(",", "")) if m else None
    return {
        "title": unescape(title.group(1)).strip() if title else "",
        "reviews": reviews,
        "rating": rating,
        "drift": [s for s in must_not if s.lower() in text.lower()],
    }


async def _json(client: httpx.AsyncClient, url: str) -> Any:
    r = await client.get(url, timeout=30)
    r.raise_for_status()
    return r.json()


async def wordpress(client: httpx.AsyncClient, slug: str, keywords: List[str], depth: int = 2) -> Dict[str, Any]:
    """The plugin's numbers, its daily downloads, how far 'Tested up to' trails WordPress, and its position in
    WordPress.org search for each keyword (first `depth` pages of 100; null = further down)."""
    fields = {f"request[fields][{f}]": 1 for f in ("active_installs", "downloaded", "rating", "num_ratings",
                                                    "support_threads", "support_threads_resolved")}
    fields.update({f"request[fields][{f}]": 0 for f in ("sections", "description", "versions", "screenshots")})
    info = await _json(client, WP_API + "?" + urlencode({"action": "plugin_information", "request[slug]": slug, **fields}))
    out: Dict[str, Any] = {
        "slug": slug, "name": unescape(info.get("name", "")), "version": info.get("version"),
        "tested": info.get("tested"), "active_installs": info.get("active_installs"),
        "downloaded": info.get("downloaded"), "rating": info.get("rating"), "num_ratings": info.get("num_ratings"),
        "support_threads": info.get("support_threads"), "support_resolved": info.get("support_threads_resolved"),
        "last_updated": info.get("last_updated"),
    }
    try:
        daily = await _json(client, WP_STATS + "?" + urlencode({"slug": slug, "limit": 30}))
        days = sorted((d, int(n)) for d, n in (daily or {}).items())
        out["downloads_daily"] = days
        out["downloads_7d"] = sum(n for _, n in days[-7:])
    except (httpx.HTTPError, ValueError):
        out["downloads_daily"], out["downloads_7d"] = [], None
    try:
        core = await _json(client, WP_CORE)
        out["wp_latest"] = (core.get("offers") or [{}])[0].get("current")
        out["majors_behind"] = majors_behind(out["tested"] or "", out["wp_latest"] or "")
    except (httpx.HTTPError, ValueError):
        out["wp_latest"], out["majors_behind"] = None, None

    ranks = []
    for kw in keywords:
        pos: Optional[int] = None
        total = 0
        for page in range(1, depth + 1):
            q = urlencode({"action": "query_plugins", "request[search]": kw, "request[per_page]": 100,
                           "request[page]": page, "request[fields][description]": 0,
                           "request[fields][sections]": 0, "request[fields][short_description]": 0})
            data = await _json(client, WP_API + "?" + q)
            total = (data.get("info") or {}).get("results", 0)
            slugs = [p.get("slug") for p in data.get("plugins") or []]
            if slug in slugs:
                pos = (page - 1) * 100 + slugs.index(slug) + 1
                break
            if len(slugs) < 100:
                break
        ranks.append({"keyword": kw, "position": pos, "results": total})
    out["ranks"] = ranks
    return out


async def shopify(client: httpx.AsyncClient, handle: str, must_not: List[str]) -> Dict[str, Any]:
    """The listing page: reviews, rating and any text that must not be there. No search (see the module doc)."""
    r = await client.get(f"{SHOPIFY}/{handle}", timeout=30)
    r.raise_for_status()
    return {"handle": handle, **shopify_listing(r.text, must_not)}


def summary(wp: Optional[Dict[str, Any]], sh: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """The few numbers worth a line on the Today view, and what needs attention."""
    attention: List[str] = []
    if wp:
        if (wp.get("majors_behind") or 0) >= 3:
            attention.append(f"WordPress.org: 'Tested up to' {wp.get('tested')} is {wp['majors_behind']} majors behind "
                             f"{wp.get('wp_latest')}; the listing shows the untested warning")
        if wp.get("support_threads") and wp.get("support_resolved") is not None and wp["support_resolved"] < wp["support_threads"]:
            attention.append(f"WordPress.org: {wp['support_threads'] - wp['support_resolved']} support thread(s) unresolved")
    if sh and sh.get("drift"):
        attention.append("Shopify listing still shows: " + "; ".join(f"“{d}”" for d in sh["drift"]))

    def top(ranks: List[Dict[str, Any]], n: int) -> int:
        return sum(1 for r in ranks or [] if r.get("position") and r["position"] <= n)

    return {
        "wp_installs": wp.get("active_installs") if wp else None,
        "wp_downloads_7d": wp.get("downloads_7d") if wp else None,
        "wp_ratings": wp.get("num_ratings") if wp else None,
        "wp_top20": top(wp.get("ranks"), 20) if wp else None,
        "shopify_reviews": sh.get("reviews") if sh else None,
        "attention": attention,
    }


async def check(client: httpx.AsyncClient, cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Both stores; one that fails is an error line, not a failed run."""
    out: Dict[str, Any] = {"errors": [], "wordpress": None, "shopify": None}
    wp_cfg, sh_cfg = cfg.get("wordpress") or {}, cfg.get("shopify") or {}
    if wp_cfg.get("slug"):
        try:
            out["wordpress"] = await wordpress(client, str(wp_cfg["slug"]), [str(k) for k in wp_cfg.get("keywords") or []])
        except (httpx.HTTPError, ValueError) as e:
            out["errors"].append(f"WordPress.org: {type(e).__name__}: {str(e)[:120]}")
    if sh_cfg.get("handle"):
        try:
            out["shopify"] = await shopify(client, str(sh_cfg["handle"]), [str(s) for s in sh_cfg.get("must_not_contain") or []])
        except (httpx.HTTPError, ValueError) as e:
            out["errors"].append(f"Shopify App Store: {type(e).__name__}: {str(e)[:120]}")
    out["summary"] = summary(out["wordpress"], out["shopify"])
    return out


def trend(history: List[Dict[str, Any]]) -> List[Tuple[str, Dict[str, Any]]]:
    """(date, summary) per past run, oldest first, for the dashboard's trend line."""
    rows = [((h.get("_at") or "")[:10], h.get("summary") or {}) for h in history if h.get("summary")]
    return list(reversed(rows))
