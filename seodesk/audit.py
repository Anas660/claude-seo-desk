"""`seodesk audit URL`: one site, once, with no config files and no keys. It crawls the sitemap, runs the GEO checks,
scores SEO / AEO / GEO and prints a report as text, Markdown or JSON, with the quick wins it found. With
SERPER_API_KEY and --keyword it also checks Google ranks; with LLM_API_KEY and --brief the agent reads the evidence
and writes prioritised actions. Nothing is saved: for history, a dashboard and a daily brief, use `seodesk run`."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import httpx

from . import __version__, agent, crawl as crawler, geo, llm, pipeline, progress, serp
from .errors import FatalError
from .settings import Keyword, adhoc

# What each crawl issue code means, for the report's issue table.
ISSUES = {
    "status": "pages that don't return 200",
    "noindex": "pages marked noindex",
    "jsonld-invalid": "JSON-LD blocks that don't parse",
    "title-missing": "pages without a <title>",
    "title-long": "titles longer than Google shows (60 characters)",
    "title-duplicate": "titles shared with another page",
    "desc-missing": "pages without a meta description",
    "desc-length": "meta descriptions outside 70-160 characters",
    "desc-duplicate": "meta descriptions shared with another page",
    "canonical-missing": "pages without a canonical link",
    "canonical-other": "canonicals pointing to another URL",
    "h1-missing": "pages without an <h1>",
    "h1-many": "pages with more than one <h1>",
    "thin": "pages under 300 words",
    "slow": "pages slower than 2.5 s to download",
    "img-alt": "pages with images missing alt text",
    "search-action": "pages with retired SearchAction markup",
    "product-no-image": "Product markup without an image",
}


# Quick wins for page-level issues: what to do, then how many pages and one example.
FIXES = {
    "title-missing": "Give every page a <title>",
    "desc-missing": "Write a meta description (70-160 characters) for each page without one",
    "h1-missing": "Give each page one <h1> that says what it's about",
    "title-duplicate": "Make every page's title unique",
    "desc-duplicate": "Make every page's meta description unique",
    "canonical-missing": "Add a canonical link to each page",
    "slow": "Speed up the slowest pages (caching, server response time)",
}


def _pct(part: Optional[float]) -> str:
    return "-" if part is None else str(round(part))


def _share(part: float, of: str) -> str:
    """'No pages carry...' or 'Only 12% of pages carry...'."""
    return f"No {of}" if round(part) == 0 else f"Only {_pct(part)}% of {of}"


def _example(report: Dict[str, Any], code: str) -> str:
    for p in report["pages"]:
        if any(i["code"] == code for i in p["issues"]):
            return urlparse(p["url"]).path or "/"
    return ""


def quick_wins(report: Dict[str, Any]) -> List[str]:
    """The fixes worth doing first, most valuable first, worked out from the evidence alone (no model)."""
    wins: List[str] = []
    crawl, g = report["crawl"], report["geo"]
    by_code = (crawl.get("summary") or {}).get("issues_by_code") or {}
    blocked = [c["crawler"] for c in g.get("crawlers") or [] if not c["home"]]
    if blocked:
        wins.append(f"Let AI crawlers in: robots.txt blocks {', '.join(blocked)} from the homepage. Add a group per "
                    "crawler, e.g. `User-agent: GPTBot` then `Allow: /`, unless blocking them is deliberate.")
    if not crawl.get("sitemap"):
        wins.append("Publish a sitemap (/sitemap.xml) and name it in robots.txt (`Sitemap: https://.../sitemap.xml`): "
                    "search engines and this audit found pages only through links.")
    if by_code.get("status"):
        wins.append(f"Fix or drop the {by_code['status']} page(s) that don't return 200: a sitemap should list only "
                    "live pages.")
    if by_code.get("noindex") and crawl.get("sitemap"):
        wins.append(f"{by_code['noindex']} page(s) are in the sitemap but marked noindex: take them out of the sitemap "
                    "or remove the noindex.")
    llms = {f["path"]: f["status"] for f in g.get("llms") or []}
    if llms.get("/llms.txt") != 200:
        wins.append("Add /llms.txt: a short Markdown map of the site for AI assistants (see https://llmstxt.org).")
    elif llms.get("/llms-full.txt") != 200:
        wins.append("Add /llms-full.txt next to /llms.txt: the full text of the key pages in one file.")
    parts = report.get("parts") or {}
    schema = (parts.get("geo") or {}).get("schema")
    if schema is not None and schema < 50:
        wins.append(f"{_share(schema, 'pages')} carry structured data: add JSON-LD (Organization on the home page, "
                    "Article on posts, Product or SoftwareApplication on product pages).")
    aeo = parts.get("aeo") or {}
    if aeo.get("questions") is not None and aeo["questions"] < 30:
        wins.append(f"{_share(aeo['questions'], 'content pages')} have question headings: phrase some H2s as the "
                    "questions people ask (What is..., How do I...).")
    if aeo.get("answers") is not None and aeo["answers"] < 30:
        wins.append(f"{_share(aeo['answers'], 'content pages')} open a question heading with a direct answer: put a "
                    "25-90 word answer straight under each question heading, the passage answer engines quote.")
    for code, fix in FIXES.items():
        if by_code.get(code):
            n = by_code[code]
            wins.append(f"{fix} ({n} page{'' if n == 1 else 's'}, e.g. {_example(report, code)}).")
    return wins[:10]


async def audit(url: str, max_pages: int = 50, concurrency: int = 4, keywords: Optional[List[str]] = None,
                country: str = "us", language: str = "en", brief: bool = False, model: str = "", base_url: str = "",
                max_usd: float = 0.25, respect_robots: bool = True, http: Optional[httpx.AsyncClient] = None,
                client: Optional[llm.Client] = None) -> Dict[str, Any]:
    """The whole audit as one JSON-ready dict."""
    settings = adhoc(url, country=country, language=language)
    if model:
        settings.model = model
    if base_url:
        settings.llm_base_url = base_url.rstrip("/")
    own_http = http is None
    http = http or crawler.make_client(20.0)
    notes: List[str] = []
    try:
        site = await crawler.resolve_origin(http, settings.site)
        if site != settings.site:
            settings.site = site
        crawl = await crawler.crawl(site, max_pages, concurrency, client=http, respect_robots=respect_robots)
        notes += crawl["errors"]
        geo_out = await geo.check(http, site, settings.ai_crawlers, crawl["pages"], [], settings.about_us)
        notes += geo_out["errors"]
        ranks: List[Dict[str, Any]] = []
        searcher: Optional[serp.Serper] = None
        if keywords:
            if not settings.serper_key:
                notes.append("Rank checks skipped: SERPER_API_KEY isn't set")
            else:
                searcher = serp.Serper(settings.serper_key, http, max_calls=2 * len(keywords) + 5)
                try:
                    out = await serp.check_all(searcher, [Keyword(k, country, "medium") for k in keywords],
                                               settings.host, language)
                    ranks = out["rows"]
                    notes += out["errors"]
                except FatalError as e:
                    notes.append(f"Ranks: {e}")
                    searcher = None
        scores = progress.score(crawl["pages"], geo_out, ranks, expect_ranks=bool(ranks))
        report: Dict[str, Any] = {
            "site": site,
            "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "version": __version__,
            "scores": {k: scores.get(k) for k in ("seo", "aeo", "geo", "overall")},
            "parts": {k: scores["parts"][k] for k in ("seo", "aeo", "geo")},
            "metrics": scores["parts"]["metrics"],
            "ranks_checked": bool(ranks),
            "crawl": {"sitemap": crawl.get("sitemap", ""), "pages": len(crawl["pages"]), "summary": crawl["summary"]},
            "pages": [{k: p.get(k) for k in ("url", "status", "ms", "title", "words", "schema_types", "aeo", "issues")}
                      for p in crawl["pages"]],
            "geo": {k: geo_out.get(k) for k in ("crawlers", "llms", "summary")},
            "ranks": ranks,
            "brief": None,
            "notes": notes,
        }
        report["quick_wins"] = quick_wins(report)
        if brief:
            if not llm.available():
                notes.append("Brief skipped: LLM_API_KEY (or OPENROUTER_API_KEY) isn't set")
            else:
                evidence = (f"One-off audit, {report['date']}. Today's scores (0-100): {json.dumps(report['scores'])}, "
                            f"parts {json.dumps(report['parts'])}. There's no history and no earlier actions.\n\n"
                            + pipeline.evidence(settings, crawl, geo_out, ranks, []))
                tools = agent.Tools(site, crawl["pages"], searcher, language)
                try:
                    b, cost = await agent.write_brief(client or llm.Client(settings.llm_base_url), settings.model,
                                                      settings.effort, evidence, tools, settings.max_tool_calls, max_usd,
                                                      system=agent.system_prompt(settings))
                    report["brief"] = b.model_dump() if b else None
                    report["brief_cost_usd"] = round(cost, 4)
                    if b is None:
                        notes.append("Brief: the model's answer didn't fit the schema twice")
                except llm.LLMError as e:
                    notes.append(f"Brief: {e}")
        return report
    finally:
        if own_http:
            await http.aclose()


# ── reports ──────────────────────────────────────────────────────────────

def _score(v: Optional[int]) -> str:
    return "-" if v is None else str(v)


def _pages(n: int) -> str:
    return f"{n} page" + ("" if n == 1 else "s")


def _issue_rows(report: Dict[str, Any]) -> List[Dict[str, Any]]:
    by_code = (report["crawl"]["summary"] or {}).get("issues_by_code") or {}
    rows = []
    for code, n in by_code.items():
        examples = [urlparse(p["url"]).path or "/" for p in report["pages"] if any(i["code"] == code for i in p["issues"])]
        level = "error" if any(i["code"] == code and i["level"] == "error" for p in report["pages"] for i in p["issues"]) else "warn"
        rows.append({"code": code, "count": n, "level": level, "what": ISSUES.get(code, code), "examples": examples[:3]})
    return sorted(rows, key=lambda r: (r["level"] != "error", -r["count"]))


def _parts_lines(report: Dict[str, Any]) -> List[str]:
    p = report["parts"]
    seo = ([f"keyword visibility {_pct(p['seo'].get('visibility'))}"] if report["ranks_checked"] else []) + [
        f"pages without errors {_pct(p['seo'].get('technical'))}", f"clean on-page tags {_pct(p['seo'].get('on_page'))}"]
    aeo = [f"question headings {_pct(p['aeo'].get('questions'))}", f"answer-first {_pct(p['aeo'].get('answers'))}",
           f"FAQ schema {_pct(p['aeo'].get('faq'))}", f"tables or lists {_pct(p['aeo'].get('structure'))}"]
    geo_ = [f"AI crawlers allowed {_pct(p['geo'].get('crawlers'))}", f"llms files {_pct(p['geo'].get('llms'))}",
            f"structured data {_pct(p['geo'].get('schema'))}"]
    return [" · ".join(seo), " · ".join(aeo), " · ".join(geo_)]


def to_text(report: Dict[str, Any]) -> str:
    s, c, g = report["scores"], report["crawl"], report["geo"]
    seo_p, aeo_p, geo_p = _parts_lines(report)
    out = [f"SEO Desk audit: {report['site']} ({report['date']})",
           f"Crawled {_pages(c['pages'])} from " + (c["sitemap"] or "the homepage and its links (no sitemap found)"), "",
           f"  Overall {_score(s['overall'])}    SEO {_score(s['seo'])}    AEO {_score(s['aeo'])}    GEO {_score(s['geo'])}", "",
           f"  SEO {_score(s['seo']):>3}  {seo_p}",
           f"  AEO {_score(s['aeo']):>3}  {aeo_p}",
           f"  GEO {_score(s['geo']):>3}  {geo_p}"]
    if not report["ranks_checked"]:
        out.append("  (SEO is technical and on-page only. Add --keyword with SERPER_API_KEY set for Google rank checks.)")
    allowed = [x["crawler"] for x in g.get("crawlers") or [] if x["home"]]
    blocked = [x["crawler"] for x in g.get("crawlers") or [] if not x["home"]]
    out += ["", "AI crawlers"]
    out.append(f"  allowed: {', '.join(allowed) or 'none'}")
    if blocked:
        out.append(f"  BLOCKED: {', '.join(blocked)}")
    out.append("  " + "    ".join(f"{f['path']} {'ok' if f['status'] == 200 else 'MISSING (' + str(f['status']) + ')'}"
                                  for f in g.get("llms") or []))
    if report["quick_wins"]:
        out += ["", "Quick wins"] + [f"  {i}. {w}" for i, w in enumerate(report["quick_wins"], 1)]
    rows = _issue_rows(report)
    if rows:
        out += ["", "Issues"]
        out += [f"  {r['count']:>4}  {r['level']:<5}  {r['what']}  e.g. {', '.join(r['examples'])}" for r in rows]
    if report["ranks"]:
        out += ["", "Google ranks"] + [f"  {('#' + str(r['position'])) if r['position'] else 'not in top 20':>14}  {r['keyword']}"
                                       for r in report["ranks"]]
    b = report.get("brief")
    if b:
        out += ["", f"Brief: {b['headline']}", f"  {b['summary']}", ""]
        for a in b["actions"]:
            out.append(f"  P{a['priority']} [{a['area']}] {a['title']}" + (f"  ({a['url']})" if a["url"] else ""))
            out.append(f"     {a['how']}")
    if report["notes"]:
        out += ["", "Notes"] + [f"  - {n}" for n in report["notes"]]
    return "\n".join(out) + "\n"


def to_markdown(report: Dict[str, Any]) -> str:
    s, c, g = report["scores"], report["crawl"], report["geo"]
    seo_p, aeo_p, geo_p = _parts_lines(report)
    md = [f"## SEO Desk audit: {report['site']}", "",
          f"{report['date']} · {_pages(c['pages'])} from " + (f"`{c['sitemap']}`" if c["sitemap"] else "the homepage and its links (no sitemap found)"),
          "", "| Overall | SEO | AEO | GEO |", "|:-:|:-:|:-:|:-:|",
          f"| **{_score(s['overall'])}** | {_score(s['seo'])} | {_score(s['aeo'])} | {_score(s['geo'])} |", "",
          f"- **SEO**: {seo_p}" + ("" if report["ranks_checked"] else " (no rank checks)"),
          f"- **AEO**: {aeo_p}", f"- **GEO**: {geo_p}", "", "### AI crawlers and llms.txt", "",
          " ".join(f"{'✅' if x['home'] else '❌'} {x['crawler']}" for x in g.get("crawlers") or []), "",
          " ".join(f"{'✅' if f['status'] == 200 else '❌'} `{f['path']}`" for f in g.get("llms") or [])]
    if report["quick_wins"]:
        md += ["", "### Quick wins", ""] + [f"{i}. {w}" for i, w in enumerate(report["quick_wins"], 1)]
    rows = _issue_rows(report)
    if rows:
        md += ["", "### Issues", "", "| Pages | Level | Issue | Examples |", "|--:|:--|:--|:--|"]
        md += [f"| {r['count']} | {r['level']} | {r['what']} | {', '.join(f'`{e}`' for e in r['examples'])} |" for r in rows]
    if report["ranks"]:
        md += ["", "### Google ranks", "", "| Keyword | Position | Page |", "|:--|--:|:--|"]
        md += [f"| {r['keyword']} | {('#' + str(r['position'])) if r['position'] else 'not in top 20'} | {r['url'] or ''} |"
               for r in report["ranks"]]
    b = report.get("brief")
    if b:
        md += ["", f"### Brief: {b['headline']}", "", b["summary"], ""]
        for a in b["actions"]:
            md.append(f"- **P{a['priority']} · {a['area']}**: {a['title']}" + (f" (`{a['url']}`)" if a["url"] else ""))
            md.append(f"  {a['how']}")
    if report["notes"]:
        md += ["", "<details><summary>Notes</summary>", ""] + [f"- {n}" for n in report["notes"]] + ["", "</details>"]
    md += ["", f"<sub>SEO Desk {report['version']} · https://github.com/Anas660/claude-seo-desk</sub>"]
    return "\n".join(md) + "\n"


def to_json(report: Dict[str, Any]) -> str:
    return json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n"


def for_agents(report: Dict[str, Any], pages: int = 20) -> Dict[str, Any]:
    """The report cut down for an AI agent's context (the MCP server, the Claude Code skill): scores and their parts,
    quick wins, issues by type with example pages, the pages with the most issues, and AI crawler access."""
    worst = sorted(report["pages"], key=lambda p: -sum(2 if i["level"] == "error" else 1 for i in p["issues"]))
    g = report["geo"]
    return {
        "site": report["site"],
        "date": report["date"],
        "sitemap": report["crawl"]["sitemap"] or None,
        "pages_crawled": report["crawl"]["pages"],
        "scores": report["scores"],
        "score_parts": report["parts"],
        "rank_checks": report["ranks"] if report["ranks_checked"] else "not run (needs SERPER_API_KEY and keywords)",
        "quick_wins": report["quick_wins"],
        "issues": _issue_rows(report),
        "pages_with_issues": [{"url": p["url"], "status": p["status"], "title": p["title"],
                               "issues": [f"{i['level']}: {i['text']}" for i in p["issues"]]}
                              for p in worst[:pages] if p["issues"]],
        "ai_crawlers": {"allowed": [c["crawler"] for c in g.get("crawlers") or [] if c["home"]],
                        "blocked": [c["crawler"] for c in g.get("crawlers") or [] if not c["home"]]},
        "llms_files": {f["path"]: f["status"] for f in g.get("llms") or []},
        "brief": report.get("brief"),
        "notes": report["notes"],
    }


FORMATS = {"text": to_text, "markdown": to_markdown, "json": to_json}


def failed(report: Dict[str, Any], fail_under: float) -> Optional[str]:
    """Why the audit fails `--fail-under`, or None when it passes."""
    if fail_under <= 0:
        return None
    overall = report["scores"]["overall"]
    if overall is None:
        return "no overall score (no pages could be crawled)"
    if overall < fail_under:
        return f"overall score {overall} is under {fail_under:g}"
    return None
