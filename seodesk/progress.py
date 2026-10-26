"""SEO / AEO / GEO scores for every run, and how they move week to week and month to month.

The scores are worked out here from what every site has (the crawl, the GEO checks, the rank checks), so they mean
the same on every site and can be recomputed for old runs (`seodesk rescore`). Each is 0-100, a weighted
mean of its parts; a part with no data that run is left out and the other weights scale up.

SEO  = keyword visibility 50% (points per keyword by Google position: #1 100, #2 90, #3 80, #4-5 65, #6-10 50,
       #11-15 25, #16-20 15, lower 0) + technical 30% (pages without errors) + on-page 20% (pages without title,
       description, canonical, H1, thin-text or alt-text warnings).
AEO  = share of content pages with question headings 30% + an answer-first paragraph under one 30% + FAQPage schema
       20% + tables or lists 20% (pages the site file marks thin on purpose are left out).
GEO  = AI crawlers allowed in robots.txt 25% + llms.txt (60) and llms-full.txt (40) 15% + pages with structured
       data 20% + no false claims on the site 15% (sites with a claims list) + AI models naming the site in
       unbranded answers 25% (sites with ai_visibility, the latest probes; a 25% mention rate or better scores 100).
Overall = the mean of the three.

A day without rank checks (Serper down or out of credits) keeps the previous day's keyword visibility, so one missing
source doesn't swing the score; a site with a keyword plan and a Serper key but no rank check yet has no SEO score
rather than one made of the technical parts alone. Without a Serper key, SEO is the technical and on-page parts."""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlparse

from .db import DB
from .settings import Settings

ON_PAGE = {"title-missing", "title-long", "desc-missing", "desc-length", "canonical-missing", "canonical-other",
           "h1-missing", "h1-many", "title-duplicate", "desc-duplicate", "thin", "img-alt"}
WEIGHTS = {
    "seo": {"visibility": 0.5, "technical": 0.3, "on_page": 0.2},
    "aeo": {"questions": 0.3, "answers": 0.3, "faq": 0.2, "structure": 0.2},
    "geo": {"crawlers": 0.25, "llms": 0.15, "schema": 0.2, "claims": 0.15, "mentions": 0.25},
}
DIMENSIONS = ("seo", "aeo", "geo", "overall")
# Numbers shown next to the scores. "lower" ones are better when they go down; "last" ones take a period's last value
# (rolling or weekly figures) instead of its average.
METRICS = {
    "top10": {"label": "Keywords in Google's top 10"},
    "keywords": {"label": "Keywords checked"},
    "avg_position": {"label": "Average position (ranked keywords)", "lower": True},
    "errors": {"label": "Crawl errors", "lower": True},
    "pages": {"label": "Pages in the sitemap"},
    "ai_mentions": {"label": "Unbranded AI answers naming the site (latest probes)", "last": True},
    "ranked_total": {"label": "Keywords Google ranks the site for (DataForSEO)", "last": True},
    "ranked_top10": {"label": "…of which in the top 10 (DataForSEO)", "last": True},
    "etv": {"label": "Estimated monthly clicks (DataForSEO)", "last": True},
}


def position_points(p: Optional[int]) -> int:
    if p is None:
        return 0
    for limit, pts in ((1, 100), (2, 90), (3, 80), (5, 65), (10, 50), (15, 25), (20, 15)):
        if p <= limit:
            return pts
    return 0


def pct(n: int, d: int) -> Optional[float]:
    return round(100 * n / d, 1) if d else None


def blend(parts: Dict[str, Optional[float]], weights: Dict[str, float]) -> Optional[int]:
    have = {k: v for k, v in parts.items() if v is not None and k in weights}
    total = sum(weights[k] for k in have)
    return round(sum(v * weights[k] for k, v in have.items()) / total) if total else None


def score(pages: List[Dict[str, Any]], geo: Optional[Dict[str, Any]], serp: List[Dict[str, Any]],
          probes: Optional[Dict[str, Any]] = None, ranked: Optional[Dict[str, Any]] = None,
          thin_ok: Iterable[str] = (), has_claims: bool = False, expect_ranks: bool = False,
          carry: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """{seo, aeo, geo, overall, parts: {seo: {...}, aeo: {...}, geo: {...}, metrics: {...}}} for one run.
    `carry` is the previous run's parts, for a source missing today; `expect_ranks` (the site has a keyword plan)
    leaves SEO empty rather than scoring it without visibility."""
    prev_seo = (carry or {}).get("seo") or {}
    carried: List[str] = []
    ok = [p for p in pages if p.get("status") == 200]
    errors = sum(1 for p in pages for i in p.get("issues") or [] if i.get("level") == "error")
    clean = sum(1 for p in pages if not any(i.get("level") == "error" for i in p.get("issues") or []))
    tidy = sum(1 for p in ok if not {i.get("code") for i in p.get("issues") or []} & ON_PAGE)
    ranked_rows = [r for r in serp if r.get("position") is not None]
    seo = {
        "visibility": round(sum(position_points(r.get("position")) for r in serp) / len(serp), 1) if serp else None,
        "technical": pct(clean, len(pages)),
        "on_page": pct(tidy, len(ok)),
    }
    if seo["visibility"] is None and expect_ranks and prev_seo.get("visibility") is not None:
        seo["visibility"] = prev_seo["visibility"]
        carried.append("visibility")

    thin = {t.rstrip("/") or "/" for t in thin_ok}
    content = [p for p in ok if (urlparse(p["url"]).path.rstrip("/") or "/") not in thin]
    aeo_of = lambda p: p.get("aeo") or {}  # noqa: E731
    aeo = {
        "questions": pct(sum(1 for p in content if aeo_of(p).get("question_headings")), len(content)),
        "answers": pct(sum(1 for p in content if aeo_of(p).get("answer_first")), len(content)),
        "faq": pct(sum(1 for p in content if aeo_of(p).get("faq_schema")), len(content)),
        "structure": pct(sum(1 for p in content if aeo_of(p).get("tables") or (aeo_of(p).get("lists") or 0) >= 2), len(content)),
    }

    g = geo or {}
    crawlers = g.get("crawlers") or []
    llms = {f.get("path"): f.get("status") for f in g.get("llms") or []}
    ps = (probes or {}).get("summary") or {}
    prompts, mentions = int(ps.get("prompts") or 0), int(ps.get("mentions") or 0)
    claim_pages = len({h.get("url") for h in g.get("claims") or []})
    geo_parts = {
        "crawlers": pct(sum(1 for c in crawlers if c.get("home")), len(crawlers)),
        "llms": (60 if llms.get("/llms.txt") == 200 else 0) + (40 if llms.get("/llms-full.txt") == 200 else 0) if llms else None,
        "schema": pct(sum(1 for p in ok if p.get("schema_types")), len(ok)),
        "claims": max(0, 100 - 25 * claim_pages) if has_claims and geo is not None else None,
        "mentions": min(100.0, round(400 * mentions / prompts, 1)) if prompts else None,
    }

    out: Dict[str, Any] = {"seo": blend(seo, WEIGHTS["seo"]) if seo["visibility"] is not None or not expect_ranks else None,
                           "aeo": blend(aeo, WEIGHTS["aeo"]), "geo": blend(geo_parts, WEIGHTS["geo"])}
    dims = [out[k] for k in ("seo", "aeo", "geo")]
    out["overall"] = round(sum(dims) / 3) if None not in dims else None  # a partial mean wouldn't compare day to day
    rk = ranked or {}
    out["parts"] = {
        "seo": seo, "aeo": aeo, "geo": geo_parts, "carried": carried,
        "metrics": {
            "keywords": len(serp) or None,
            "top3": sum(1 for r in ranked_rows if r["position"] <= 3) if serp else None,
            "top10": sum(1 for r in ranked_rows if r["position"] <= 10) if serp else None,
            "top20": len(ranked_rows) if serp else None,
            "avg_position": round(sum(r["position"] for r in ranked_rows) / len(ranked_rows), 1) if ranked_rows else None,
            "pages": len(pages) or None,
            "errors": errors if pages else None,
            "ai_mentions": mentions if prompts else None,
            "ai_prompts": prompts or None,
            "ranked_total": rk.get("total"),
            "ranked_top10": rk.get("top10"),
            "etv": rk.get("etv"),
        },
    }
    return out


def score_run(db: DB, settings: Settings, run: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Scores one stored run from what it saved, and stores them. None when the run has nothing to score."""
    rid = run["id"]
    pages, serp = db.run_pages(rid), db.run_serp(rid)
    if not pages and not serp:
        return None
    result = score(pages, db.run_snapshot(rid, "geo"), serp, probes=db.latest_snapshot("probes", run_id=rid) if settings.visibility else None,
                   ranked=db.latest_snapshot("ranked", run_id=rid), thin_ok=settings.thin_ok,
                   has_claims=bool(settings.claims), expect_ranks=bool(settings.keywords and settings.serper_key),
                   carry=db.previous_parts(rid))
    db.save_scores(rid, run["started_at"][:10], result)
    return result


def backfill(db: DB, settings: Settings) -> int:
    """Scores the runs from before scoring existed. Returns how many were scored."""
    return sum(1 for run in db.unscored_runs() if score_run(db, settings, run) is not None)


def rescore(db: DB, settings: Settings) -> int:
    """Recomputes every run's scores (after the formula changes)."""
    runs = {r["id"]: r for r in db.scored_runs() + db.unscored_runs()}
    return sum(1 for rid in sorted(runs) if score_run(db, settings, runs[rid]) is not None)


# ── progress ────────────────────────────────────────────────────────────
def _avg(values: List[Optional[float]]) -> Optional[float]:
    v = [x for x in values if x is not None]
    return round(sum(v) / len(v), 1) if v else None


def _last(values: List[Optional[float]]) -> Optional[float]:
    v = [x for x in values if x is not None]
    return v[-1] if v else None


def week_of(day: str) -> str:
    d = date.fromisoformat(day)
    return (d - timedelta(days=d.weekday())).isoformat()


def periods(rows: List[Dict[str, Any]], key) -> List[Dict[str, Any]]:
    """Daily rows grouped into periods (oldest first): average scores, metrics, and the change on the period before."""
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows:
        groups.setdefault(key(r["date"]), []).append(r)
    out: List[Dict[str, Any]] = []
    for k in sorted(groups):
        days = groups[k]
        p: Dict[str, Any] = {"period": k, "days": len(days), "first": days[0]["date"], "last": days[-1]["date"]}
        for d in DIMENSIONS:
            p[d] = _avg([x[d] for x in days])
        p["metrics"] = {}
        for m, spec in METRICS.items():
            vals = [(x["parts"].get("metrics") or {}).get(m) for x in days]
            p["metrics"][m] = _last(vals) if spec.get("last") else _avg(vals)
        out.append(p)
    for prev, cur in zip(out, out[1:]):
        cur["change"] = {d: round(cur[d] - prev[d], 1) if cur[d] is not None and prev[d] is not None else None
                         for d in DIMENSIONS}
        cur["metric_change"] = {m: round(cur["metrics"][m] - prev["metrics"][m], 1)
                                if cur["metrics"][m] is not None and prev["metrics"][m] is not None else None
                                for m in METRICS}
    return out


def trend(change: Optional[float], lower_better: bool = False, threshold: float = 1.0) -> str:
    """'up' (better), 'down' (worse) or 'flat'."""
    if change is None:
        return "none"
    if abs(change) < threshold:
        return "flat"
    return "up" if (change < 0 if lower_better else change > 0) else "down"


def compare(cur: Optional[Dict[str, Any]], prev: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """This period against the one before, per score: now, before, change and whether that's better."""
    out: Dict[str, Any] = {"period": (cur or {}).get("period"), "previous": (prev or {}).get("period")}
    for d in DIMENSIONS:
        now, before = (cur or {}).get(d), (prev or {}).get(d)
        change = round(now - before, 1) if now is not None and before is not None else None
        out[d] = {"now": now, "before": before, "change": change, "trend": trend(change)}
    return out


def verdict(cmp: Dict[str, Any]) -> str:
    """One line: 'Improving: SEO +4.2, GEO +1.5; AEO flat.'"""
    ups = [f"{d.upper()} {cmp[d]['change']:+g}" for d in ("seo", "aeo", "geo") if cmp[d]["trend"] == "up"]
    downs = [f"{d.upper()} {cmp[d]['change']:+g}" for d in ("seo", "aeo", "geo") if cmp[d]["trend"] == "down"]
    flats = [d.upper() for d in ("seo", "aeo", "geo") if cmp[d]["trend"] == "flat"]
    if not ups and not downs and not flats:
        return "Not enough history yet."
    head = "Improving" if ups and not downs else "Declining" if downs and not ups else "Mixed" if ups else "Steady"
    flat = flats[0] if len(flats) == 1 else ", ".join(flats[:-1]) + " and " + flats[-1] if flats else ""
    bits = ([", ".join(ups)] if ups else []) + ([", ".join(downs)] if downs else []) + ([flat + " flat"] if flats else [])
    return f"{head}: " + "; ".join(bits) + "."


def progress(db: DB, days: int = 400) -> Dict[str, Any]:
    since = (date.today() - timedelta(days=days)).isoformat()
    rows = db.scores(since)
    weeks = periods(rows, week_of)
    months = periods(rows, lambda d: d[:7])
    week_cmp = compare(weeks[-1] if weeks else None, weeks[-2] if len(weeks) > 1 else None)
    month_cmp = compare(months[-1] if months else None, months[-2] if len(months) > 1 else None)
    latest = rows[-1] if rows else None
    return {
        "latest": latest,
        "week": {**week_cmp, "verdict": verdict(week_cmp)},
        "month": {**month_cmp, "verdict": verdict(month_cmp)},
        "weeks": weeks[-12:],
        "months": months[-12:],
        "series": [{"date": r["date"], **{d: r[d] for d in DIMENSIONS}} for r in rows[-120:]],
        "metrics": {m: {"label": s["label"], "lower": bool(s.get("lower"))} for m, s in METRICS.items()},
        "days": len(rows),
    }


def overview_row(db: DB, settings: Settings) -> Dict[str, Any]:
    """One site's line on the All sites tab."""
    p = progress(db, days=120)
    runs = db.runs(1)
    latest = p["latest"] or {}
    return {
        "slug": settings.slug, "name": settings.label, "site": settings.site,
        "scores": {d: latest.get(d) for d in DIMENSIONS}, "date": latest.get("date"),
        "metrics": (latest.get("parts") or {}).get("metrics") or {},
        "week": p["week"], "month": p["month"],
        "series": p["series"][-30:],
        "run": runs[0] if runs else None,
        "month_spend": round(db.month_spend(), 2),
    }


def ensure_scored(db: DB, settings: Settings) -> Tuple[int, int]:
    """(scored now, total days with scores): fills in runs that were never scored."""
    n = backfill(db, settings)
    return n, len(db.scores())
