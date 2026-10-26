"""`seodesk audit`: the one-off audit against a fake site, its three report formats, the CI threshold, sites with no
sitemap and sites that redirect."""
import asyncio
import json

import httpx

from seodesk import audit, crawl
from seodesk.settings import adhoc, origin

from helpers import BAD_PAGE, BRIEF_JSON, GOOD_PAGE, SITE, FakeLLM, fake_site


def run_audit(pages, **kw):
    async def go():
        async with fake_site(pages, **{k: kw.pop(k) for k in ("sitemap", "llms", "serper") if k in kw}) as http:
            return await audit.audit(SITE, http=http, **kw)
    return asyncio.run(go())


def test_an_audit_scores_the_site_and_finds_the_quick_wins():
    r = run_audit({"/": GOOD_PAGE, "/other": BAD_PAGE, "/gone": None}, llms=None)
    assert r["site"] == SITE and r["crawl"]["pages"] == 3 and r["crawl"]["sitemap"].endswith("/sitemap.xml")
    assert all(r["scores"][k] is not None for k in ("seo", "aeo", "geo", "overall")) and not r["ranks_checked"]
    wins = " ".join(r["quick_wins"])
    assert "CCBot" in wins                                   # robots.txt blocks it
    assert "/llms.txt" in wins and "llmstxt.org" in wins     # missing
    assert "don't return 200" in wins and "marked noindex" in wins
    assert "text" not in r["pages"][0]                       # the page text stays out of the report
    assert {i["code"] for p in r["pages"] for i in p["issues"]} >= {"status", "noindex", "title-long"}


def test_the_three_formats_render():
    r = run_audit({"/": GOOD_PAGE, "/other": BAD_PAGE})
    text, md, js = audit.to_text(r), audit.to_markdown(r), audit.to_json(r)
    assert f"SEO Desk audit: {SITE}" in text and "BLOCKED: CCBot" in text and "Quick wins" in text
    assert "| Overall | SEO | AEO | GEO |" in md and "❌ CCBot" in md and "### Issues" in md
    assert json.loads(js)["scores"] == r["scores"]


def test_fail_under_is_for_ci():
    r = run_audit({"/": GOOD_PAGE})
    overall = r["scores"]["overall"]
    assert audit.failed(r, 0) is None and audit.failed(r, overall) is None
    assert "under" in audit.failed(r, overall + 1)
    empty = {"scores": {"overall": None}}
    assert "no pages" in audit.failed(empty, 50)


def test_a_site_without_a_sitemap_is_crawled_from_its_homepage():
    home = GOOD_PAGE.replace("<h1>Invoicing</h1>", '<h1>Invoicing</h1><a href="/pricing">Pricing</a><a href="/guide.pdf">PDF</a>'
                                                   '<a href="https://elsewhere.test/x">out</a><a href="#top">top</a>')
    r = run_audit({"/": home, "/pricing": BAD_PAGE}, sitemap=False)
    assert [p["url"] for p in r["pages"]] == [f"{SITE}/", f"{SITE}/pricing"]
    assert r["crawl"]["sitemap"] == "" and any("no sitemap found" in n for n in r["notes"])
    noindex = [i for p in r["pages"] for i in p["issues"] if i["code"] == "noindex"]
    assert noindex and noindex[0]["level"] == "warn"  # found by a link, not listed in a sitemap
    assert any("Publish a sitemap" in w for w in r["quick_wins"])


def test_an_audit_follows_the_homepage_to_the_real_origin():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "example-site.test":
            return httpx.Response(301, headers={"location": f"{SITE}/"})
        return httpx.Response(200, text="<html><title>x</title></html>")

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            return await crawl.resolve_origin(http, "http://example-site.test")
    assert asyncio.run(go()) == SITE


def test_rank_checks_and_the_brief_join_in_when_keys_are_there(monkeypatch):
    monkeypatch.setenv("SERPER_API_KEY", "k")
    monkeypatch.setenv("LLM_API_KEY", "k")
    llm = FakeLLM([{"content": "Ready."}, {"content": json.dumps(BRIEF_JSON)}])
    serper = {"invoice template": {"organic": [{"link": f"{SITE}/", "position": 3}]}}
    r = run_audit({"/": GOOD_PAGE}, serper=serper, keywords=["invoice template"], brief=True, client=llm)
    assert r["ranks_checked"] and r["ranks"][0]["position"] == 3 and r["parts"]["seo"]["visibility"] == 80
    assert r["brief"]["headline"] == BRIEF_JSON["headline"] and r["brief_cost_usd"] > 0
    assert "One-off audit" in llm.sent[0]["messages"][1]["content"]
    assert "Brief:" in audit.to_text(r) and "### Brief:" in audit.to_markdown(r)


def test_keywords_without_a_key_are_a_note_not_a_failure(monkeypatch):
    monkeypatch.delenv("SERPER_API_KEY", raising=False)
    r = run_audit({"/": GOOD_PAGE}, keywords=["x"])
    assert not r["ranks_checked"] and any("SERPER_API_KEY" in n for n in r["notes"])


def test_urls_are_read_the_way_people_type_them():
    assert origin("example.com/pricing") == "https://example.com"
    assert origin("HTTP://Example.com") == "http://example.com"
    s = adhoc("www.example.co.uk")
    assert s.site == "https://www.example.co.uk" and s.slug == "example-co-uk" and len(s.ai_crawlers) >= 5


def test_quick_wins_say_what_to_do_in_plain_words():
    from helpers import page_html
    bare = page_html("Home", "", "", "<p>Hello</p>").replace('<meta name="description" content="">', "").replace('<link rel="canonical" href="">', "")
    wins = run_audit({"/": bare})["quick_wins"]
    text = " ".join(wins)
    assert "No pages carry structured data" in text and "Only 0%" not in text
    assert "Give each page one <h1> that says what it's about (1 page, e.g. /)." in wins
    assert any(w.startswith("Write a meta description") and "(1 page, e.g. /)" in w for w in wins)
