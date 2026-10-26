"""One daily run of one site: crawl the site, run the GEO checks, check the app-store listings and the keyword ranks,
refresh the DataForSEO numbers and the AI-visibility probes when they're due, score the day, then let the agent write
the brief and the actions. Every step stands alone: one that fails is noted on the run and the rest still go, so a
dead Serper key doesn't cost you the crawl."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import httpx

from . import agent, crawl as crawler, dataforseo, geo, llm, probes, progress, serp, stores as store_checks
from .db import DB
from .errors import FatalError, LimitReached, SearchError
from .settings import SERPER_USD, Settings

log = logging.getLogger(__name__)


@dataclass
class Result:
    run_id: int = 0
    status: str = "ok"
    notes: List[str] = field(default_factory=list)
    llm_usd: float = 0.0
    serper_calls: int = 0
    pages: int = 0
    new_actions: int = 0
    scores: Optional[Dict[str, Any]] = None


def budget_left(settings: Settings, db: DB, spent_this_run: float = 0.0) -> float:
    """What this run may still spend: the run cap, this site's monthly cap and every site's together."""
    return max(0.0, min(settings.max_run_usd - spent_this_run, settings.max_monthly_usd - db.month_spend(),
                        settings.max_total_monthly_usd - db.month_spend(all_sites=True)))


def scores_line(scores: Optional[Dict[str, Any]]) -> str:
    if not scores:
        return ""
    return " · ".join(f"{k.upper()} {scores[k]}" for k in ("seo", "aeo", "geo") if scores.get(k) is not None)


def evidence(settings: Settings, crawl: Dict[str, Any], geo_out: Dict[str, Any], ranks: List[Dict[str, Any]],
             open_actions: List[Dict[str, Any]], new_pages: Optional[Dict[str, str]] = None,
             stores: Optional[Dict[str, Any]] = None, volumes: Optional[Dict[str, Any]] = None,
             ranked: Optional[Dict[str, Any]] = None, trend: Optional[Dict[str, Any]] = None,
             visibility: Optional[Dict[str, Any]] = None) -> str:
    """The day's facts for the model, compact: numbers, not whole tables."""
    parts: List[str] = [f"Site: {settings.site}"]
    if trend and trend.get("latest"):
        t, w, m = trend["latest"], trend["week"], trend["month"]
        parts.append("SEO Desk scores (0-100, same formula every day: SEO = keyword visibility, errors, on-page; AEO = "
                     "question headings, answer-first paragraphs, FAQ schema, tables; GEO = AI crawler access, llms "
                     f"files, structured data, false claims, AI mentions). Today: SEO {t['seo']}, AEO {t['aeo']}, "
                     f"GEO {t['geo']}, parts {json.dumps(t['parts'], ensure_ascii=False)}. This week vs last: "
                     f"{w['verdict']} This month vs last: {m['verdict']}")
    parts.append("Search Console isn't connected: there's no click or impression data. Rely on the crawl, the rank "
                 "checks, the AI answers and the DataForSEO numbers, and don't invent traffic figures.")
    if ranked:
        parts.append("Keywords Google ranks the site for (DataForSEO Labs, "
                     f"{ranked.get('country', '').upper()}, refreshed weekly; etv = estimated monthly clicks): " + json.dumps(
                         {k: ranked.get(k) for k in ("total", "top3", "top10", "top20", "etv", "new", "up", "down", "lost")})
                     + "; top keywords by volume: " + json.dumps(
                         [{k: i.get(k) for k in ("keyword", "volume", "position", "url")} for i in (ranked.get("items") or [])[:30]],
                         ensure_ascii=False))
    s = crawl.get("summary") or {}
    if crawl.get("sitemap"):
        parts.append(f"Today's live sitemap lists {crawl.get('sitemap_count', 0)} URLs, every one crawled below; "
                     "a page in that list IS in the sitemap.")
    else:
        parts.append("The site has no sitemap that could be read: the crawl below is the homepage and the pages it "
                     "links to. " + "; ".join(crawl.get("errors") or []))
    parts.append("Crawl summary: " + json.dumps(s))
    if new_pages:
        parts.append("Pages first crawled in the last 14 days (path: first seen). Google needs days to rank a new page, "
                     "so no rank yet is expected, not a problem: " + json.dumps(new_pages))
    parts.append("Every page on the site (path: title). Check here before saying a page doesn't exist: " + json.dumps(
        {urlparse(p["url"]).path or "/": p.get("title", "") for p in crawl.get("pages") or []}, ensure_ascii=False))
    worst = sorted(crawl.get("pages") or [], key=lambda p: -sum(2 if i["level"] == "error" else 1 for i in p["issues"]))[:15]
    parts.append("Pages with the most issues: " + json.dumps(
        [{"url": p["url"], "issues": [i["text"] for i in p["issues"]][:5]} for p in worst if p["issues"]], ensure_ascii=False))
    parts.append("GEO checks: " + json.dumps(geo_out.get("summary") or {}) + "; llms files: " + json.dumps(geo_out.get("llms") or []))
    if geo_out.get("claims"):
        parts.append("False claims found on live pages: " + json.dumps(geo_out["claims"][:20], ensure_ascii=False))
    if visibility and visibility.get("results"):
        vs = visibility.get("summary") or {}
        parts.append(f"AI visibility ({(visibility.get('_at') or 'today')[:10]}): {vs.get('mentions')} of "
                     f"{vs.get('prompts')} unbranded AI answers name the site, {vs.get('cited')} link to it. By model: "
                     + json.dumps(vs.get("by_model") or {}) + ". Sites the answers cite instead: "
                     + json.dumps(vs.get("top_cited") or [], ensure_ascii=False) + ". Answers: " + json.dumps(
                         [{k: r[k] for k in ("model", "prompt", "branded", "mentioned", "cited", "flags", "excerpt")}
                          for r in visibility["results"][:20]], ensure_ascii=False))
    rows = []
    for r in ranks:
        row = {k: r.get(k) for k in ("keyword", "country", "tier", "volume", "target", "position", "url", "top", "questions")}
        measured, _ = dataforseo.volume_for(volumes, r["keyword"], r["country"])
        if measured is not None:
            row["volume"] = f"{measured}/month (Google Ads, DataForSEO)"
        rows.append(row)
    if rows:
        parts.append("Keyword ranks today (Google via Serper, top 20; position null = not in the top 20): " + json.dumps(
            rows, ensure_ascii=False))
    if stores and (stores.get("wordpress") or stores.get("shopify")):
        wp, sh = stores.get("wordpress") or {}, stores.get("shopify") or {}
        parts.append("App-store listings today. Each store ranks its own search mostly on installs, reviews and the "
                     "listing name, so the levers are reviews, installs and listing copy, not the website. "
                     "WordPress.org plugin (active_installs 0 = fewer than 10; position null = below the first "
                     "200 results): " + json.dumps({k: wp.get(k) for k in ("name", "version", "tested", "wp_latest",
                     "majors_behind", "active_installs", "downloads_7d", "num_ratings", "rating", "support_threads",
                     "support_resolved", "ranks")}, ensure_ascii=False) + ". Shopify App Store app (its search "
                     "positions aren't checked: Shopify's robots.txt disallows it; drift = text that must not be on "
                     "the listing): " + json.dumps({k: sh.get(k) for k in ("title", "reviews", "rating", "drift")},
                                                   ensure_ascii=False))
    parts.append("Open actions from earlier days: " + json.dumps(
        [{"title": a["title"], "url": a["url"], "first_seen": a["first_seen"]} for a in open_actions[:30]], ensure_ascii=False))
    return "\n\n".join(parts)


def recent_pages(db: DB, days: int = 14) -> Dict[str, str]:
    """Paths first crawled in the last `days` days. The first run ever has no history, so it reports none."""
    seen = db.first_seen()
    if not seen:
        return {}
    earliest = min(seen.values())
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {urlparse(u).path or "/": t[:10] for u, t in seen.items() if t > earliest and t >= cutoff}


async def refresh_dataforseo(settings: Settings, db: DB, run_id: int, http: httpx.AsyncClient,
                             notes: List[str]) -> float:
    """Search volumes (every ~4 weeks, or when the keyword plan changes) and the keywords Google ranks the site for
    (weekly), when they're due and the money allows. Returns what it cost."""
    cfg = settings.dataforseo
    if not (settings.dataforseo_login and settings.dataforseo_password) or cfg.get("enabled") is False:
        return 0.0
    vol_due = dataforseo.volumes_due(db.latest_snapshot("volumes"), settings.keywords, float(cfg.get("volume_every_days", 28)))
    rank_due = dataforseo.due(db.latest_snapshot("ranked"), float(cfg.get("ranked_every_days", 7)))
    if not (vol_due or rank_due):
        return 0.0
    if budget_left(settings, db) < 0.2:
        notes.append("DataForSEO: skipped, the monthly budget is nearly used up")
        return 0.0
    client = dataforseo.DataForSEO(settings.dataforseo_login, settings.dataforseo_password, http,
                                   max_calls=int(cfg.get("max_calls_per_run", 6)))
    try:
        if vol_due:
            out = await dataforseo.volumes(client, settings.keywords, settings.language)
            if out["by_keyword"]:
                db.save_snapshot(run_id, "volumes", out)
            notes += [f"DataForSEO: {e}" for e in out["errors"]]
            log.info("DataForSEO: %d search volumes", len(out["by_keyword"]))
        if rank_due:
            try:
                out = await dataforseo.ranked(client, settings.host, settings.country, int(cfg.get("ranked_limit", 100)),
                                              settings.language)
                db.save_snapshot(run_id, "ranked", out)
                log.info("DataForSEO: ranks for %d keywords (%d in the top 10)", out["total"], out["top10"])
            except (SearchError, LimitReached) as e:
                notes.append(f"DataForSEO: ranked keywords: {e}")
    except FatalError as e:
        notes.append(f"DataForSEO: {e}")
    db.add_spend("dataforseo", client.cost)
    return client.cost


async def refresh_visibility(settings: Settings, db: DB, run_id: int, client: llm.Client, notes: List[str]) -> float:
    """The AI-visibility probes, when the site has them, they're due, a model key is set and the money allows.
    Returns what they cost."""
    if settings.visibility is None or not probes.probes_due(db.latest_snapshot("probes"), settings):
        return 0.0
    if not llm.available():
        notes.append("AI visibility: LLM_API_KEY not set")
        return 0.0
    left = budget_left(settings, db)
    if left < 0.05:
        notes.append("AI visibility: skipped, the monthly budget is nearly used up")
        return 0.0
    out = await probes.run(client, settings, budget_usd=left * 0.5)  # leave the rest for the brief
    if out["results"]:
        db.save_snapshot(run_id, "probes", {k: out[k] for k in ("results", "summary", "plan", "errors")})
    notes += [f"AI visibility: {e}" for e in out["errors"][:3]]
    db.add_spend("probes", out["cost"])
    return out["cost"]


async def run(settings: Settings, db: DB, kind: str = "daily", client: Optional[llm.Client] = None,
              http: Optional[httpx.AsyncClient] = None, with_brief: bool = True) -> Result:
    db = db.scoped(settings.slug)
    try:  # runs from before scoring existed get their scores, so progress has history from day one
        progress.backfill(db, settings)
    except Exception:  # never let old data stop today's run
        log.exception("Scoring earlier runs failed")
    res = Result(run_id=db.start_run(kind))
    own_http = http is None
    http = http or crawler.make_client(float(settings.crawl.get("timeout_s", 20)), str(settings.crawl.get("user_agent") or ""))
    client = client or llm.Client(settings.llm_base_url)
    try:
        # 1. crawl the sitemap
        crawl = await crawler.crawl(settings.site, int(settings.crawl.get("max_pages", 150)),
                                    int(settings.crawl.get("concurrency", 4)), thin_ok=settings.thin_ok, client=http,
                                    sitemaps=[str(x) for x in settings.crawl.get("sitemaps") or []] or None,
                                    respect_robots=settings.crawl.get("respect_robots", True) is not False)
        res.pages = db.save_pages(res.run_id, crawl["pages"])
        db.save_snapshot(res.run_id, "crawl", {"summary": crawl["summary"], "sitemap_count": crawl["sitemap_count"],
                                               "sitemap": crawl.get("sitemap", ""), "errors": crawl["errors"]})
        res.notes += [f"Crawl: {e}" for e in crawl["errors"]]
        log.info("Crawl: %d pages, health %s%%", res.pages, crawl["summary"].get("health"))

        # 2. GEO checks
        geo_out = await geo.check(http, settings.site, settings.ai_crawlers, crawl["pages"], settings.claims, settings.about_us)
        db.save_snapshot(res.run_id, "geo", geo_out)
        res.notes += [f"GEO: {e}" for e in geo_out["errors"]]

        # 2b. the app-store listings
        stores_out: Dict[str, Any] = {}
        if settings.stores:
            stores_out = await store_checks.check(http, settings.stores)
            db.save_snapshot(res.run_id, "stores", stores_out)
            res.notes += [f"Stores: {e}" for e in stores_out["errors"]]

        # 3. keyword ranks
        searcher = serp.Serper(settings.serper_key, http, settings.max_serper_per_run) if settings.serper_key else None
        ranks: List[Dict[str, Any]] = []
        if not settings.keywords:
            pass
        elif searcher is None:
            res.notes.append("Ranks: SERPER_API_KEY not set")
        elif budget_left(settings, db) <= 0:
            res.notes.append("Ranks: monthly budget used up")
        else:
            try:
                out = await serp.check_all(searcher, settings.keywords, settings.host, settings.language)
                ranks = out["rows"]
                db.save_serp(res.run_id, ranks)
                res.notes += [f"Ranks: {e}" for e in out["errors"][:3]]
            except FatalError as e:
                res.notes.append(f"Ranks: {e}")
                searcher = None

        # 3b. DataForSEO: search volumes and the keywords Google ranks the site for, when due
        await refresh_dataforseo(settings, db, res.run_id, http, res.notes)

        # 4. AI visibility: what AI models answer to the site's prompts, when due
        try:
            res.llm_usd += await refresh_visibility(settings, db, res.run_id, client, res.notes)
        except Exception as e:  # a model that answers oddly must not cost the brief
            log.exception("AI visibility failed")
            res.notes.append(f"AI visibility: {type(e).__name__}: {str(e)[:160]}")

        # 5. today's scores, and how they compare with last week and last month
        trend: Optional[Dict[str, Any]] = None
        try:
            res.scores = progress.score_run(db, settings, db.run(res.run_id))
            trend = progress.progress(db, days=70)
        except Exception as e:
            log.exception("Scoring failed")
            res.notes.append(f"Scores: {type(e).__name__}: {str(e)[:160]}")

        # 6. the brief
        if with_brief:
            left = budget_left(settings, db)
            if not llm.available():
                res.notes.append("Brief: LLM_API_KEY not set")
            elif left <= 0.01:
                res.notes.append("Brief: budget used up for this month")
            else:
                tools = agent.Tools(settings.site, crawl["pages"], searcher, settings.language)
                visibility = db.latest_snapshot("probes") if settings.visibility else None
                try:
                    brief, cost = await agent.write_brief(client, settings.model, settings.effort,
                                                          evidence(settings, crawl, geo_out, ranks, db.actions("open"),
                                                                   recent_pages(db), stores_out,
                                                                   db.latest_snapshot("volumes"), db.latest_snapshot("ranked"),
                                                                   trend, visibility),
                                                          tools, settings.max_tool_calls, left, system=agent.system_prompt(settings))
                    res.llm_usd += cost
                    db.add_spend("brief", cost)
                    if brief is None:
                        res.notes.append("Brief: the model's answer didn't fit the schema twice")
                    else:
                        db.save_brief(res.run_id, brief.model_dump(), settings.model, cost)
                        res.new_actions = db.upsert_actions(
                            res.run_id, agent.normalise_priorities([a.model_dump() for a in brief.actions]))
                        db.merge_duplicates()
                except llm.LLMError as e:
                    res.notes.append(f"Brief: {e}")
        if searcher is not None:
            res.serper_calls = searcher.calls
            db.add_spend("serper", searcher.calls * SERPER_USD)
    except Exception as e:
        log.exception("Run failed")
        res.status = "failed"
        res.notes.append(f"{type(e).__name__}: {str(e)[:200]}")
    finally:
        if own_http:
            await http.aclose()
    if res.status != "failed" and res.notes:
        res.status = "partial"
    head = scores_line(res.scores)
    note = "; ".join(([head] if head else []) + [f"{res.pages} pages", f"{res.new_actions} new actions"] + res.notes)
    db.finish_run(res.run_id, res.status, note, llm_usd=round(res.llm_usd, 4), serper_calls=res.serper_calls,
                  pages=res.pages, new_actions=res.new_actions)
    return res
