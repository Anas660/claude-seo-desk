"""`seodesk mcp`: SEO Desk as an MCP server (stdio), so Claude Code, Claude Desktop, Cursor or any MCP client can audit
a site, check one page, see which AI crawlers a site lets in, and draft its llms.txt.

    pip install "seo-desk[mcp]"            # the MCP SDK needs Python 3.10+
    claude mcp add seo-desk -- seodesk mcp

Every tool only reads public pages (no keys needed); rank checks join in when SERPER_API_KEY is set."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import httpx

from . import __version__, audit, crawl as crawler, geo, llmstxt
from .settings import AI_CRAWLERS, origin

INSTRUCTIONS = """SEO Desk checks websites for SEO (Google ranking), AEO (being the quoted answer) and GEO (being named by
AI assistants). Start with audit_site for a whole site; use inspect_page to check one page (for example after editing
it), check_ai_access for robots.txt and llms.txt alone, and draft_llms_txt to write a starting llms.txt. Pages are read
without JavaScript, the way most AI crawlers read them. Quote the scores and issues you rely on."""


async def check_ai_access(url: str, http: Optional[httpx.AsyncClient] = None) -> Dict[str, Any]:
    own = http is None
    http = http or crawler.make_client(20.0)
    try:
        site = await crawler.resolve_origin(http, origin(url))
        out = await geo.check(http, site, AI_CRAWLERS, [], [])
    finally:
        if own:
            await http.aclose()
    return {"site": site,
            "ai_crawlers": {c["crawler"]: "allowed" if c["home"] else "blocked from the homepage" for c in out["crawlers"]},
            "llms_files": {f["path"]: f["status"] for f in out["llms"]},
            "robots_txt_found": out.get("robots_ok", False), "errors": out["errors"]}


async def inspect_page(url: str, http: Optional[httpx.AsyncClient] = None) -> Dict[str, Any]:
    own = http is None
    http = http or crawler.make_client(20.0)
    try:
        status, html, headers, ms = await crawler.fetch(http, url)
    finally:
        if own:
            await http.aclose()
    page = crawler.analyse(url, status, html, headers, ms, in_sitemap=False)
    if status in (301, 302, 303, 307, 308):
        page["redirects_to"] = headers.get("location", "")
    text = page.pop("text", "")
    return {**page, "text_start": text[:1200]}


async def draft_llms_txt(url: str, max_pages: int = 60, name: str = "",
                         http: Optional[httpx.AsyncClient] = None) -> str:
    own = http is None
    http = http or crawler.make_client(20.0)
    try:
        site = await crawler.resolve_origin(http, origin(url))
        result = await crawler.crawl(site, max_pages, client=http)
    finally:
        if own:
            await http.aclose()
    return llmstxt.draft(site, result["pages"], name)


def build():
    try:
        from mcp.server.mcpserver import MCPServer
        from mcp.types import ToolAnnotations
    except ImportError as e:
        raise SystemExit('The MCP server needs the MCP SDK (Python 3.10+): pip install "seo-desk[mcp]"') from e

    server = MCPServer("seo-desk", title="SEO Desk", version=__version__, instructions=INSTRUCTIONS,
                       website_url="https://github.com/Anas660/claude-seo-desk")
    reads_the_web = ToolAnnotations(readOnlyHint=True, openWorldHint=True)

    @server.tool(annotations=reads_the_web)
    async def audit_site(url: str, max_pages: int = 30, keywords: Optional[List[str]] = None) -> Dict[str, Any]:
        """Audit a website for SEO, AEO and GEO: crawls its sitemap (or homepage links), scores each 0-100, and returns
        the quick wins, issues by type with example pages, the pages with the most issues, and which AI crawlers
        robots.txt lets in. `keywords` adds Google rank checks when SERPER_API_KEY is set."""
        report = await audit.audit(url, max_pages=max(1, min(max_pages, 200)), keywords=keywords or [])
        return audit.for_agents(report)

    @server.tool(name="inspect_page", annotations=reads_the_web)
    async def inspect_page_tool(url: str) -> Dict[str, Any]:
        """Check one page: status, title, meta description, canonical, H1, structured data types, AEO signals
        (question headings, answer-first paragraphs, FAQ schema) and its issues, plus the start of its text."""
        return await inspect_page(url)

    @server.tool(name="check_ai_access", annotations=reads_the_web)
    async def check_ai_access_tool(url: str) -> Dict[str, Any]:
        """Which AI crawlers (GPTBot, ClaudeBot, PerplexityBot, Google-Extended...) the site's robots.txt lets read the
        homepage, read the way the crawlers read it (RFC 9309), and whether /llms.txt and /llms-full.txt exist."""
        return await check_ai_access(url)

    @server.tool(name="draft_llms_txt", annotations=reads_the_web)
    async def draft_llms_txt_tool(url: str, max_pages: int = 60, name: str = "") -> str:
        """Draft an llms.txt (llmstxt.org) for the site from its pages' titles and descriptions, grouped by section.
        A starting point to edit before publishing at /llms.txt."""
        return await draft_llms_txt(url, max(1, min(max_pages, 300)), name)

    return server


def serve() -> int:  # pragma: no cover - runs until the client disconnects
    build().run("stdio")
    return 0
