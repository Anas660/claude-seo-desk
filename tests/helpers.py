"""Shared fixtures: settings that point at a fake site, a fake site served through httpx's MockTransport, and a fake
chat-model client."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

from seodesk.settings import Claim, Keyword, Settings

SITE = "https://www.example-site.test"


def make_settings(tmp_path: Path, **over: Any) -> Settings:
    base = dict(
        site=SITE, slug="acme", name="Acme", brand_terms=["acme"], model="deepseek/deepseek-v4.1-flash", effort="low",
        max_tool_calls=3, max_run_usd=1.0, max_monthly_usd=15.0, max_serper_per_run=10,
        crawl={"max_pages": 50, "concurrency": 2, "timeout_s": 5},
        keywords=[Keyword("invoice template", "us", "easy", "100-1K", "/templates/invoice"),
                  Keyword("invoicing software", "us", "head", "10K-100K", "/")],
        claims=[Claim("soc2", re.compile(r"\bSOC[\s-]?2\b", re.I), "no SOC 2 report exists")],
        ai_crawlers=["GPTBot", "ClaudeBot"], serper_key="test-serper", db_path=tmp_path / "seo.db", thin_ok=["/privacy"],
    )
    base.update(over)
    return Settings(**base)


def page_html(title: str, desc: str, canonical: str, body: str, jsonld: Any = None, robots: str = "") -> str:
    ld = f'<script type="application/ld+json">{json.dumps(jsonld)}</script>' if jsonld is not None else ""
    rb = f'<meta name="robots" content="{robots}">' if robots else ""
    return (f"<html><head><title>{title}</title><meta name=\"description\" content=\"{desc}\">"
            f"<link rel=\"canonical\" href=\"{canonical}\">{rb}{ld}</head><body><main>{body}</main></body></html>")


WORDS = " ".join(["word"] * 400)
ANSWER = " ".join(["Acme sends invoices, chases late payments and keeps the books for freelancers."] * 3)

GOOD_PAGE = page_html(
    "Invoicing Software for Freelancers | Acme",
    "Invoicing software for freelancers: send invoices, get paid online and chase late payments without the awkward emails.",
    f"{SITE}/", f"<h1>Invoicing</h1><h2>What is invoicing software?</h2><p>{ANSWER}</p><table><tr><td>x</td></tr></table><p>{WORDS}</p>",
    {"@context": "https://schema.org", "@graph": [{"@type": "Organization"}, {"@type": "FAQPage"}]})

BAD_PAGE = page_html(
    "A very long title that keeps going well past the sixty characters Google shows",
    "short", f"{SITE}/other",
    "<h1>One</h1><h1>Two</h1><p>We are SOC 2 Type II certified.</p>",
    {"@type": "WebSite", "potentialAction": {"@type": "SearchAction"}, "hasPart": {"@type": "Product", "name": "Plan"}},
    robots="noindex")

ROBOTS = "User-agent: *\nAllow: /\nDisallow: /dashboard\n\nUser-agent: CCBot\nDisallow: /\n"


def fake_site(pages: Dict[str, Optional[str]], serper: Optional[Dict[str, Any]] = None, serper_status: int = 200,
              sitemap: bool = True, llms: Optional[str] = "# Acme\nInvoices for freelancers. Setup in 3m 18s.") -> httpx.AsyncClient:
    calls: List[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if request.url.host == "google.serper.dev":
            if serper_status != 200:
                return httpx.Response(serper_status, text="Not enough credits")
            q = json.loads(request.content)["q"]
            return httpx.Response(200, json=(serper or {}).get(q, {"organic": []}))
        p = request.url.path
        if p == "/sitemap.xml" and sitemap:
            locs = "".join(f"<url><loc>{SITE}{k}</loc></url>" for k in pages)
            return httpx.Response(200, text=f"<urlset>{locs}<url><loc>https://elsewhere.test/x</loc></url></urlset>")
        if p == "/robots.txt":
            return httpx.Response(200, text=ROBOTS)
        if p in ("/llms.txt", "/llms-full.txt") and llms is not None:
            return httpx.Response(200, text=llms)
        if pages.get(p) is not None:  # a None page is in the sitemap but gone (404)
            return httpx.Response(200, text=pages[p], headers={"content-type": "text/html"})
        return httpx.Response(404, text="not found")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client.calls = calls  # type: ignore[attr-defined]
    return client


class FakeLLM:
    """llm.Client stand-in: plays back a script of chat responses and records what it was sent. A script entry is a
    message dict, or (message, extra top-level fields) for things like Perplexity's `citations`."""

    def __init__(self, script: List[Any]):
        self.script, self.sent = list(script), []

    async def chat(self, body: Dict[str, Any]) -> Dict[str, Any]:
        self.sent.append(body)
        entry = self.script.pop(0) if self.script else {"content": "{}"}
        msg, extra = entry if isinstance(entry, tuple) else (entry, {})
        return {"choices": [{"message": msg, "finish_reason": "stop"}], "usage": {"cost": 0.002}, **extra}


BRIEF_JSON = {
    "headline": "Two pages lost their canonical; one false claim live",
    "summary": "SEO 57, AEO 98, GEO 39. One page still claims SOC 2.",
    "wins": ["AEO steady at 98"],
    "risks": ["SOC 2 claim on /other"],
    "actions": [{"area": "GEO", "priority": 1, "title": "Remove the SOC 2 claim from /other", "why": "False claim AI models repeat.",
                 "how": "Delete the sentence.", "url": "/other", "keyword": "", "effort": "S", "evidence": "crawl: soc2 hit"}],
    "content_ideas": [{"keyword": "invoice template", "page_type": "guide", "angle": "Free and editable", "questions": ["Is there a free invoice template?"]}],
}
