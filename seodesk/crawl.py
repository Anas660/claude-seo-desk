"""Crawl of every URL in the sitemap: the technical SEO basics, the AEO signals answer engines lift, and the page
text for the claims check. Polite: a few pages at a time, one identifiable User-Agent, no JavaScript: what a crawler
gets without running scripts is what most AI crawlers see, so a client-rendered page shows up thin here.

The sitemap is the one robots.txt names, else /sitemap.xml, /sitemap-index.xml or /sitemap_index.xml; a sitemap
index (Astro, WordPress, Yoast) is followed one level down. A site with no sitemap gets its homepage and the pages
the homepage links to, and a note saying so.

Pages robots.txt disallows for SEODesk (its own group, else *) are skipped, with a note; a site owner auditing their
own site can turn that off (crawl.respect_robots: false, or `seodesk audit --ignore-robots`)."""
from __future__ import annotations

import asyncio
import json
import re
import time
from html import unescape
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from . import __version__, geo

ROBOTS_NAME = "SEODesk"
UA = f"{ROBOTS_NAME}/{__version__} (+https://github.com/Anas660/claude-seo-desk)"
TITLE_MAX, DESC_MIN, DESC_MAX = 60, 70, 160  # what Google shows before cutting a title or description
THIN_WORDS = 300
SLOW_MS = 2500
QUESTION = re.compile(r"\?\s*$|^(what|how|why|when|which|who|can|does|do|is|are|should|will)\b", re.I)
LOC = re.compile(r"<loc>\s*(?:<!\[CDATA\[)?\s*([^<\s\]]+)\s*(?:\]\]>)?\s*</loc>", re.I)
SITEMAP_LINE = re.compile(r"^\s*sitemap\s*:\s*(\S+)", re.I | re.M)
SITEMAP_PATHS = ("/sitemap.xml", "/sitemap-index.xml", "/sitemap_index.xml")
MAX_CHILD_SITEMAPS = 25


def make_client(timeout: float = 20.0, user_agent: str = "") -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=httpx.Timeout(timeout), follow_redirects=False,
                             headers={"User-Agent": user_agent or UA, "Accept": "text/html,application/xhtml+xml,*/*;q=0.8"})


async def resolve_origin(client: httpx.AsyncClient, site: str) -> str:
    """The origin the homepage ends up on after redirects (example.com -> https://www.example.com), so an audit
    typed without www or https checks the real site. The site itself when it can't be reached."""
    url = f"{site}/"
    for _ in range(5):
        try:
            r = await client.get(url)
        except httpx.HTTPError:
            return site
        if r.status_code not in (301, 302, 303, 307, 308) or not r.headers.get("location"):
            break
        url = urljoin(url, r.headers["location"])
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme in ("http", "https") and parsed.netloc else site


def homepage_links(html: str, site: str, limit: int) -> List[str]:
    """The homepage and the pages on the same host it links to, for a site without a sitemap."""
    host = urlparse(site).netloc
    out = [f"{site}/"]
    for a in BeautifulSoup(html, "html.parser").find_all("a", href=True):
        url = urljoin(f"{site}/", a["href"]).split("#", 1)[0]
        parsed = urlparse(url)
        if parsed.netloc == host and parsed.scheme in ("http", "https") and url not in out \
                and not re.search(r"\.(?:pdf|jpe?g|png|gif|webp|svg|zip|xml|json|txt)$", parsed.path, re.I):
            out.append(url)
    return out[:limit]


def sitemap_urls(xml: str, host: str, limit: int) -> List[str]:
    """Page URLs on our host from one sitemap file (child sitemaps in an index are left out)."""
    seen, out = set(), []
    for loc in LOC.findall(xml):
        loc = unescape(loc)
        if urlparse(loc).netloc == host and loc not in seen and not loc.endswith(".xml"):
            seen.add(loc)
            out.append(loc)
    return out[:limit]


def child_sitemaps(xml: str, host: str) -> List[str]:
    """The sitemaps a sitemap index lists, on our host."""
    if "<sitemapindex" not in xml.lower():
        return []
    return [unescape(loc) for loc in LOC.findall(xml) if urlparse(unescape(loc)).netloc == host][:MAX_CHILD_SITEMAPS]


async def find_sitemap(client: httpx.AsyncClient, site: str) -> Tuple[str, str]:
    """(url, xml) of the site's sitemap, or ("", "") when there isn't one."""
    host = urlparse(site).netloc
    named: List[str] = []
    status, robots, _, _ = await fetch(client, f"{site}/robots.txt")
    if status == 200:
        named = [u for u in SITEMAP_LINE.findall(robots) if urlparse(u).netloc == host]
    for url in named + [f"{site}{p}" for p in SITEMAP_PATHS if f"{site}{p}" not in named]:
        status, xml, _, _ = await fetch(client, url)
        if status == 200 and "<loc" in xml.lower():
            return url, xml
    return "", ""


async def sitemap_pages(client: httpx.AsyncClient, site: str, limit: int,
                        only: Optional[List[str]] = None) -> Tuple[str, List[str], List[str]]:
    """(sitemap url, page urls, errors), following a sitemap index one level down. `only` (the site file's
    crawl.sitemaps) names the sitemaps to read instead, for a site whose index is mostly pages not worth a daily
    crawl (thousands of product or video pages)."""
    host = urlparse(site).netloc
    errors: List[str] = []
    if only:
        sources = []
        for path in only:
            url = path if "://" in path else f"{site}{path if path.startswith('/') else '/' + path}"
            status, xml, _, _ = await fetch(client, url)
            if status == 200 and "<loc" in xml.lower():
                sources.append((url, xml))
            else:
                errors.append(f"{urlparse(url).path} returned {status}")
        if not sources:
            return "", [], errors or ["none of crawl.sitemaps could be read"]
        url = ", ".join(u for u, _ in sources)
    else:
        url, xml = await find_sitemap(client, site)
        if not url:
            missing = "no sitemap found (robots.txt, " + ", ".join(SITEMAP_PATHS) + ")"
            status, html, _, _ = await fetch(client, f"{site}/")
            if status != 200:
                return "", [], [missing, f"the homepage returned {status or 'no answer'}"]
            return "", homepage_links(html, site, limit), [missing + "; crawled the homepage and the pages it links to"]
        sources = [(url, xml)]
    pages: List[str] = []
    for _, xml in sources:
        pages += [u for u in sitemap_urls(xml, host, limit) if u not in pages]
        for child in child_sitemaps(xml, host):
            if len(pages) >= limit:
                break
            status, sub, _, _ = await fetch(client, child)
            if status != 200:
                errors.append(f"{urlparse(child).path} returned {status}")
                continue
            pages += [u for u in sitemap_urls(sub, host, limit) if u not in pages]
    return url, pages[:limit], errors


def _schema_types(node: Any, out: List[str]) -> None:
    if isinstance(node, dict):
        t = node.get("@type")
        if isinstance(t, str):
            out.append(t)
        elif isinstance(t, list):
            out.extend(str(x) for x in t)
        for v in node.values():
            _schema_types(v, out)
    elif isinstance(node, list):
        for v in node:
            _schema_types(v, out)


def _has_search_action(node: Any) -> bool:
    if isinstance(node, dict):
        if node.get("@type") == "SearchAction":
            return True
        return any(_has_search_action(v) for v in node.values())
    if isinstance(node, list):
        return any(_has_search_action(v) for v in node)
    return False


def _product_without_image(node: Any) -> bool:
    if isinstance(node, dict):
        if node.get("@type") == "Product" and not node.get("image"):
            return True
        return any(_product_without_image(v) for v in node.values())
    if isinstance(node, list):
        return any(_product_without_image(v) for v in node)
    return False


def analyse(url: str, status: int, html: str, headers: Dict[str, str], ms: int, thin_ok: bool = False,
            in_sitemap: bool = True) -> Dict[str, Any]:
    """One page's facts and issues. Each issue: {level: error|warn, code, text}. A noindex page is an error when the
    sitemap lists it (the site asks Google to index it and not to, at once), a warning when it was found by a link."""
    issues: List[Dict[str, str]] = []

    def issue(level: str, code: str, text: str) -> None:
        issues.append({"level": level, "code": code, "text": text})

    page: Dict[str, Any] = {"url": url, "status": status, "ms": ms, "title": "", "description": "", "canonical": "",
                            "h1": "", "site_name": "", "words": 0, "schema_types": [], "aeo": {}, "issues": issues, "text": ""}
    if status != 200:
        issue("error", "status", f"returns HTTP {status}" + (f" (to {headers.get('location')})" if headers.get("location") else ""))
        return page
    soup = BeautifulSoup(html, "html.parser")
    title = (soup.title.string or "").strip() if soup.title and soup.title.string else ""
    desc_tag = soup.find("meta", attrs={"name": "description"})
    desc = (desc_tag.get("content") or "").strip() if desc_tag else ""
    canon_tag = soup.find("link", attrs={"rel": "canonical"})
    canonical = (canon_tag.get("href") or "").strip() if canon_tag else ""
    robots_tag = soup.find("meta", attrs={"name": "robots"})
    robots = ((robots_tag.get("content") or "") if robots_tag else "") + " " + headers.get("x-robots-tag", "")
    h1s = [h.get_text(" ", strip=True) for h in soup.find_all("h1")]
    og_name = soup.find("meta", attrs={"property": "og:site_name"})
    page.update(title=title, description=desc, canonical=canonical, h1=h1s[0] if h1s else "",
                site_name=(og_name.get("content") or "").strip() if og_name else "")

    schema_types: List[str] = []
    for block in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = block.string or block.get_text() or ""
        try:
            data = json.loads(raw)
        except ValueError:
            issue("error", "jsonld-invalid", "a JSON-LD block doesn't parse")
            continue
        _schema_types(data, schema_types)
        if _has_search_action(data):
            issue("warn", "search-action", "WebSite SearchAction markup: Google retired the sitelinks search box, and the "
                                           "{search_term_string} template gets crawled as a URL")
        if _product_without_image(data):
            issue("warn", "product-no-image", "Product markup without an image (Search Console marks it an invalid "
                                              "merchant listing); a SaaS plan fits SoftwareApplication alone")
    page["schema_types"] = sorted(set(schema_types))

    for tag in soup(["script", "style", "noscript", "svg", "template"]):
        tag.decompose()
    main = soup.find("main") or soup.body or soup
    text = main.get_text(" ", strip=True)
    page["text"] = text
    page["words"] = len(text.split())

    # ── technical SEO ──
    if not title:
        issue("error", "title-missing", "no <title>")
    elif len(title) > TITLE_MAX:
        issue("warn", "title-long", f"title is {len(title)} characters (Google shows about {TITLE_MAX})")
    if not desc:
        issue("warn", "desc-missing", "no meta description")
    elif not DESC_MIN <= len(desc) <= DESC_MAX:
        issue("warn", "desc-length", f"meta description is {len(desc)} characters (aim for {DESC_MIN}-{DESC_MAX})")
    if not canonical:
        issue("warn", "canonical-missing", "no canonical link")
    elif canonical.rstrip("/") != url.rstrip("/"):
        issue("warn", "canonical-other", f"canonical points elsewhere: {canonical}")
    if "noindex" in robots.lower():
        if in_sitemap:
            issue("error", "noindex", "marked noindex but listed in the sitemap")
        else:
            issue("warn", "noindex", "marked noindex: search engines won't index it")
    if len(h1s) == 0:
        issue("warn", "h1-missing", "no <h1>")
    elif len(h1s) > 1:
        issue("warn", "h1-many", f"{len(h1s)} <h1> elements")
    if page["words"] < THIN_WORDS and not thin_ok:
        issue("warn", "thin", f"only {page['words']} words of text")
    if ms > SLOW_MS:
        issue("warn", "slow", f"took {ms} ms to download")
    imgs = main.find_all("img") if hasattr(main, "find_all") else []
    no_alt = [i for i in imgs if i.get("alt") is None]
    if no_alt:
        issue("warn", "img-alt", f"{len(no_alt)} image(s) without an alt attribute")

    # ── AEO: what answer engines and AI Overviews lift ──
    headings = main.find_all(["h2", "h3"]) if hasattr(main, "find_all") else []
    questions = [h for h in headings if QUESTION.search(h.get_text(" ", strip=True))]
    answered = 0
    for h in questions:
        nxt = h.find_next(["p", "ul", "ol", "table"])
        if nxt is not None and nxt.name == "p" and 25 <= len(nxt.get_text(" ", strip=True).split()) <= 90:
            answered += 1
    page["aeo"] = {
        "question_headings": len(questions),
        "answer_first": answered,
        "faq_schema": "FAQPage" in schema_types,
        "speakable": "SpeakableSpecification" in schema_types or bool(soup.find(attrs={"class": re.compile("speakable|cmp-answer")})),
        "tables": len(main.find_all("table")) if hasattr(main, "find_all") else 0,
        "lists": len(main.find_all(["ul", "ol"])) if hasattr(main, "find_all") else 0,
        "internal_links": len({urljoin(url, a.get("href", "")) for a in main.find_all("a", href=True)
                               if urlparse(urljoin(url, a["href"])).netloc == urlparse(url).netloc}) if hasattr(main, "find_all") else 0,
    }
    return page


async def fetch(client: httpx.AsyncClient, url: str) -> Tuple[int, str, Dict[str, str], int]:
    started = time.monotonic()
    try:
        r = await client.get(url)
    except httpx.HTTPError as e:
        return 0, str(e), {}, int((time.monotonic() - started) * 1000)
    ms = int((time.monotonic() - started) * 1000)
    return r.status_code, r.text if r.status_code == 200 else "", {k.lower(): v for k, v in r.headers.items()}, ms


async def allowed(client: httpx.AsyncClient, site: str, urls: List[str]) -> Tuple[List[str], int]:
    """(the URLs robots.txt lets SEODesk fetch, how many it doesn't). No robots.txt allows everything."""
    status, robots, _, _ = await fetch(client, f"{site}/robots.txt")
    if status != 200:
        return urls, 0
    groups = geo.robots_groups(robots)
    ok = [u for u in urls if geo.can_fetch(groups, ROBOTS_NAME, u)]
    return ok, len(urls) - len(ok)


async def crawl(site: str, max_pages: int = 150, concurrency: int = 4, timeout: float = 20.0,
                thin_ok: Optional[List[str]] = None, client: Optional[httpx.AsyncClient] = None,
                sitemaps: Optional[List[str]] = None, respect_robots: bool = True) -> Dict[str, Any]:
    """{pages: [...], summary: {...}, sitemap_count, sitemap, errors: [...]}. sitemap is "" when there was none."""
    own = client is None
    client = client or make_client(timeout)
    host = urlparse(site).netloc
    thin_ok = thin_ok or []
    try:
        sitemap, urls, errors = await sitemap_pages(client, site, max_pages, sitemaps)
        listed = len(urls)
        if respect_robots and urls:
            urls, skipped = await allowed(client, site, urls)
            if skipped:
                errors.append(f"skipped {skipped} page(s) robots.txt disallows for {ROBOTS_NAME} "
                              "(on your own site: crawl.respect_robots: false, or audit --ignore-robots)")
        if not urls:
            return {"pages": [], "summary": summarise([]), "sitemap_count": 0, "sitemap": sitemap,
                    "errors": errors or [f"{sitemap} lists no pages on {host}"]}
        sem = asyncio.Semaphore(max(1, concurrency))

        async def one(u: str) -> Dict[str, Any]:
            async with sem:
                st, html, hdrs, ms = await fetch(client, u)
                return analyse(u, st, html, hdrs, ms, thin_ok=urlparse(u).path.rstrip("/") in thin_ok,
                               in_sitemap=bool(sitemap))

        pages = await asyncio.gather(*(one(u) for u in urls))
    finally:
        if own:
            await client.aclose()
    flag_duplicates(pages)
    return {"pages": pages, "summary": summarise(pages), "sitemap_count": listed if sitemap else 0, "sitemap": sitemap,
            "errors": errors}


def flag_duplicates(pages: List[Dict[str, Any]]) -> None:
    for field, code in (("title", "title-duplicate"), ("description", "desc-duplicate")):
        seen: Dict[str, List[Dict[str, Any]]] = {}
        for p in pages:
            if p.get(field):
                seen.setdefault(p[field], []).append(p)
        for same in seen.values():
            if len(same) > 1:
                for p in same:
                    others = ", ".join(urlparse(o["url"]).path or "/" for o in same if o is not p)[:200]
                    p["issues"].append({"level": "warn", "code": code, "text": f"same {field} as {others}"})


def summarise(pages: List[Dict[str, Any]]) -> Dict[str, Any]:
    ok = [p for p in pages if p["status"] == 200]
    errors = sum(1 for p in pages for i in p["issues"] if i["level"] == "error")
    warns = sum(1 for p in pages for i in p["issues"] if i["level"] == "warn")
    clean = sum(1 for p in pages if not any(i["level"] == "error" for i in p["issues"]))
    by_code: Dict[str, int] = {}
    for p in pages:
        for i in p["issues"]:
            by_code[i["code"]] = by_code.get(i["code"], 0) + 1
    n = len(ok) or 1
    return {
        "pages": len(pages),
        "ok": len(ok),
        "errors": errors,
        "warnings": warns,
        "health": round(100 * clean / (len(pages) or 1)),       # pages with no error, as a percentage
        "issues_by_code": dict(sorted(by_code.items(), key=lambda kv: -kv[1])),
        "aeo": {
            "with_questions": round(100 * sum(1 for p in ok if p["aeo"].get("question_headings")) / n),
            "answer_first": round(100 * sum(1 for p in ok if p["aeo"].get("answer_first")) / n),
            "faq_schema": round(100 * sum(1 for p in ok if p["aeo"].get("faq_schema")) / n),
            "with_tables": round(100 * sum(1 for p in ok if p["aeo"].get("tables")) / n),
        },
        "median_ms": sorted(p["ms"] for p in ok)[len(ok) // 2] if ok else 0,
    }
