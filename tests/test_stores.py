"""The app-store listing checks (seodesk/stores.py): the parsers, and a full check against fake store APIs."""
from __future__ import annotations

import asyncio
import json
from urllib.parse import parse_qs, urlparse

import httpx

from seodesk import stores


def test_majors_behind_counts_wordpress_releases_across_a_major_bump():
    assert stores.majors_behind("6.8", "7.1.3") == 3  # 6.9, 7.0, 7.1: the untested warning
    assert stores.majors_behind("7.1.3", "7.1.3") == 0
    assert stores.majors_behind("7.2", "7.1.3") == 0  # never negative
    assert stores.majors_behind("", "7.1") is None


def test_shopify_listing_reads_reviews_and_flags_text_that_must_not_be_there():
    html = ('<html><head><title>Acme &#8209; Ai Support Agent | Shopify App Store</title></head><body>'
            '<p>Solo Plan $12 / month or $9/year and save 94%</p><p>No reviews</p></body></html>')
    out = stores.shopify_listing(html, ["/year and save 94%", "over fifty languages"])
    assert out["title"].startswith("Acme")
    assert out["reviews"] == 0
    assert out["drift"] == ["/year and save 94%"]

    rated = ('<title>App</title><script type="application/ld+json">'
             + json.dumps({"@type": "SoftwareApplication", "aggregateRating": {"ratingValue": "4.8", "ratingCount": "12"}})
             + "</script>")
    assert stores.shopify_listing(rated, [])["reviews"] == 12
    assert stores.shopify_listing(rated, [])["rating"] == 4.8


def fake_stores() -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        url = urlparse(str(request.url))
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        if url.netloc == "api.wordpress.org" and url.path.startswith("/plugins/info"):
            if q.get("action") == "plugin_information":
                return httpx.Response(200, json={"name": "Acme &#8211; AI Chatbot &amp; Live Chat", "version": "2.5.1",
                                                 "tested": "6.8", "active_installs": 0, "downloaded": 450,
                                                 "num_ratings": 0, "rating": 0, "support_threads": 2,
                                                 "support_threads_resolved": 1})
            plugins = [{"slug": f"other-{i}"} for i in range(4)] + [{"slug": "acme-widget"}]
            if q.get("request[search]") == "ai chatbot":
                plugins = [{"slug": f"other-{i}"} for i in range(3)]
            return httpx.Response(200, json={"info": {"results": len(plugins)}, "plugins": plugins})
        if url.path.startswith("/stats/plugin"):
            return httpx.Response(200, json={"2026-10-22": "100", "2026-10-23": "20", "2026-10-24": "3"})
        if url.path.startswith("/core/version-check"):
            return httpx.Response(200, json={"offers": [{"current": "7.1.3"}]})
        if url.netloc == "apps.shopify.com" and url.path == "/acme-widget":
            return httpx.Response(200, text="<title>Acme ‑ Ai Support Agent</title><p>No reviews</p><p>over fifty languages</p>")
        if url.netloc == "apps.shopify.com" and "q" in q:
            raise AssertionError("Shopify's robots.txt disallows search; the check must not query it")
        return httpx.Response(404)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_check_reports_both_stores_and_what_needs_attention():
    cfg = {"wordpress": {"slug": "acme-widget", "keywords": ["whatsapp chatbot", "ai chatbot"]},
           "shopify": {"handle": "acme-widget", "must_not_contain": ["over fifty languages", "save 94%"]}}

    async def go():
        async with fake_stores() as client:
            return await stores.check(client, cfg)

    out = asyncio.run(go())
    assert out["errors"] == []
    wp, sh = out["wordpress"], out["shopify"]
    assert wp["name"] == "Acme – AI Chatbot & Live Chat"
    assert wp["downloads_7d"] == 123
    assert wp["majors_behind"] == 3
    assert [(r["keyword"], r["position"]) for r in wp["ranks"]] == [("whatsapp chatbot", 5), ("ai chatbot", None)]
    assert sh["reviews"] == 0 and sh["drift"] == ["over fifty languages"]
    assert "ranks" not in sh
    att = " ".join(out["summary"]["attention"])
    assert "3 majors behind" in att and "1 support thread(s) unresolved" in att and "over fifty languages" in att
    assert out["summary"]["wp_top20"] == 1


def test_a_store_that_fails_is_an_error_line_not_a_failed_check():
    def down(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(down)) as client:
            return await stores.check(client, {"wordpress": {"slug": "x", "keywords": []},
                                               "shopify": {"handle": "y"}})

    out = asyncio.run(go())
    assert out["wordpress"] is None and out["shopify"] is None
    assert len(out["errors"]) == 2


def test_trend_is_oldest_first():
    hist = [{"_at": "2026-10-24T07:05:00Z", "summary": {"wp_installs": 10}},
            {"_at": "2026-10-23T07:05:00Z", "summary": {"wp_installs": 0}}]
    assert stores.trend(hist) == [("2026-10-23", {"wp_installs": 0}), ("2026-10-24", {"wp_installs": 10})]
