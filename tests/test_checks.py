"""Crawl, GEO and rank-check logic on fixed inputs."""
import asyncio
import re

import pytest

from seodesk import crawl, geo, serp
from seodesk.errors import FatalError, LimitReached
from seodesk.settings import Claim, Keyword

from helpers import BAD_PAGE, GOOD_PAGE, SITE, fake_site, make_settings


def codes(page):
    return {i["code"] for i in page["issues"]}


def test_a_good_page_has_no_errors_and_reads_its_aeo_signals():
    p = crawl.analyse(f"{SITE}/", 200, GOOD_PAGE, {}, 300)
    assert not [i for i in p["issues"] if i["level"] == "error"]
    assert "title-long" not in codes(p) and "desc-length" not in codes(p)
    assert p["aeo"]["question_headings"] == 1 and p["aeo"]["answer_first"] == 1
    assert p["aeo"]["faq_schema"] and p["aeo"]["tables"] == 1
    assert p["schema_types"] == ["FAQPage", "Organization"]


def test_a_bad_page_lists_every_problem():
    p = crawl.analyse(f"{SITE}/bad", 200, BAD_PAGE, {}, 3000)
    assert {"title-long", "desc-length", "canonical-other", "noindex", "h1-many", "thin", "slow",
            "search-action", "product-no-image"} <= codes(p)
    assert any(i["level"] == "error" and i["code"] == "noindex" for i in p["issues"])


def test_a_non_200_page_is_an_error_and_thin_ok_pages_skip_the_word_count():
    p = crawl.analyse(f"{SITE}/gone", 404, "", {}, 50)
    assert codes(p) == {"status"}
    legal = crawl.analyse(f"{SITE}/privacy", 200, BAD_PAGE.replace('content="noindex"', 'content="index"'), {}, 50, thin_ok=True)
    assert "thin" not in codes(legal)


def test_x_robots_tag_header_counts_as_noindex():
    p = crawl.analyse(f"{SITE}/", 200, GOOD_PAGE, {"x-robots-tag": "noindex, nofollow"}, 100)
    assert "noindex" in codes(p)


def test_sitemap_keeps_only_our_pages_once():
    xml = f"<loc>{SITE}/a</loc><loc>{SITE}/a</loc><loc>https://other.test/b</loc><loc>{SITE}/s.xml</loc><loc>{SITE}/c</loc>"
    assert crawl.sitemap_urls(xml, "www.example-site.test", 10) == [f"{SITE}/a", f"{SITE}/c"]


def test_crawl_end_to_end_with_duplicates_and_summary():
    client = fake_site({"/": GOOD_PAGE, "/bad": BAD_PAGE, "/bad2": BAD_PAGE, "/missing-page": None})
    out = asyncio.run(crawl.crawl(SITE, client=client))
    asyncio.run(client.aclose())
    by = {p["url"]: p for p in out["pages"]}
    assert out["sitemap_count"] == 4
    assert "title-duplicate" in codes(by[f"{SITE}/bad"])
    assert by[f"{SITE}/missing-page"]["status"] == 404
    s = out["summary"]
    assert s["pages"] == 4 and s["ok"] == 3 and s["health"] == 25  # only / has no error
    assert s["aeo"]["faq_schema"] == 33


def test_robots_reads_ai_crawler_access():
    robots = "User-agent: *\nAllow: /\n\nUser-agent: CCBot\nDisallow: /\n"
    got = {c["crawler"]: c["home"] for c in geo.crawler_access(robots, SITE, ["GPTBot", "CCBot"])}
    assert got == {"GPTBot": True, "CCBot": False}


def test_claims_scan_quotes_the_context_and_checks_extra_files():
    claim = Claim("soc2", re.compile(r"\bSOC[\s-]?2\b", re.I), "no report")
    hits = geo.scan_claims([{"url": f"{SITE}/x", "text": "Security: we are SOC 2 Type II certified since 2025."}],
                           [claim], {f"{SITE}/llms.txt": "nothing here"})
    assert len(hits) == 1 and hits[0]["url"] == f"{SITE}/x" and "SOC 2 Type II" in hits[0]["context"]


def test_geo_check_end_to_end():
    client = fake_site({})
    claim = Claim("setup-time", re.compile(r"3m 18s"), "unmeasured")
    out = asyncio.run(geo.check(client, SITE, ["GPTBot", "CCBot"], [], [claim]))
    asyncio.run(client.aclose())
    assert out["summary"]["crawlers_blocked"] == ["CCBot"]
    assert out["summary"]["llms_missing"] == []
    assert out["summary"]["claim_hits"] == 2  # both llms files carry it
    assert [f["status"] for f in out["llms"]] == [200, 200]


def test_serp_result_finds_our_position_and_questions():
    data = {"organic": [{"link": "https://www.rival.test/a", "position": 1},
                        {"link": "https://www.example-site.test/compare/x", "position": 2}],
            "peopleAlsoAsk": [{"question": "Is there a free invoice template?"}, {"question": ""}]}
    got = serp.read_result(data, "www.example-site.test")
    assert got["position"] == 2 and got["url"].endswith("/compare/x")
    assert got["top"] == ["rival.test", "example-site.test"] and got["questions"] == ["Is there a free invoice template?"]
    assert serp.read_result({"organic": []}, "www.example-site.test")["position"] is None


def test_serp_stops_on_no_credits_and_on_the_search_cap():
    client = fake_site({}, serper_status=400)
    s = serp.Serper("k", client, max_calls=5)
    with pytest.raises(FatalError):
        asyncio.run(s.search("x"))
    capped = serp.Serper("k", fake_site({}), max_calls=1)
    out = asyncio.run(serp.check_all(capped, [Keyword("a", "us", "easy"), Keyword("b", "us", "easy")], "www.example-site.test"))
    assert len(out["rows"]) == 1 and "search limit" in out["errors"][0]
    with pytest.raises(LimitReached):
        asyncio.run(capped.search("c"))


def test_page_two_counts_from_eleven_whichever_way_serper_numbers_it():
    page2_local = {"organic": [{"link": "https://www.rival.test/x", "position": 1},
                               {"link": "https://www.example-site.test/p", "position": 3}]}
    page2_global = {"organic": [{"link": "https://www.example-site.test/p", "position": 14}]}
    assert serp.read_result(page2_local, "www.example-site.test", offset=10)["position"] == 13
    assert serp.read_result(page2_global, "www.example-site.test", offset=10)["position"] == 14


def test_a_keyword_missing_from_page_one_is_looked_up_on_page_two():
    import json as _json
    import httpx

    calls = []

    def handler(request):
        body = _json.loads(request.content)
        calls.append(body.get("page", 1))
        if body.get("page") == 2:
            return httpx.Response(200, json={"organic": [{"link": "https://www.example-site.test/compare", "position": 2}]})
        return httpx.Response(200, json={"organic": [{"link": "https://www.rival.test/", "position": 1}]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    out = asyncio.run(serp.check_all(serp.Serper("k", client, 10), [Keyword("invoice template", "us", "easy")], "www.example-site.test"))
    asyncio.run(client.aclose())
    assert calls == [1, 2]
    assert out["rows"][0]["position"] == 12 and out["rows"][0]["url"].endswith("/compare")
    assert out["rows"][0]["top"] == ["rival.test"]  # page 1's first page, not page 2's


def test_a_claim_counts_only_when_it_is_said_about_us_and_not_denied(tmp_path):
    rule = Claim("soc2", re.compile(r"\bSOC[\s-]?2\b|ISO 27001", re.I), "no report", about_us=True,
                 unless=re.compile(r"\bno\b|\bnot\b|without|doesn.t|does not|holds no|isn.t", re.I))
    about = make_settings(tmp_path).about_us  # "acme", or we / our / us
    texts = {
        "We are SOC 2 Type II certified.": 1,
        "Acme holds no compliance certification today (no SOC 2 report or ISO 27001).": 0,
        "Insist on encryption and compliance with frameworks like GDPR and SOC 2.": 0,
        "Rival logs every decision, useful for SOC 2 and regulated workflows.": 0,
        "Our platform is ISO 27001 aligned.": 1,
        "Acme is SOC 2 compliant.": 1,
    }
    for text, want in texts.items():
        got = len(geo.scan_claims([{"url": "u", "text": text}], [rule], {}, about))
        assert got == want, text


def test_a_rule_about_our_own_trial_skips_a_rivals():
    rule = Claim("trial-terms", re.compile(r"\b(?:Acme|our|we)\b[^.]{0,60}\b14-day", re.I), "the trial is 7 days")
    texts = {
        "Rival's homepage pitches an invoicing tool with a 14-day free trial.": 0,
        "Our plans come with a 14-day trial.": 1,
    }
    for text, want in texts.items():
        assert len(geo.scan_claims([{"url": "u", "text": text}], [rule], {})) == want, text


def test_robots_wildcards_and_longest_match_like_real_crawlers():
    txt = ("User-agent: *\nAllow: /*wp-admin/admin-ajax.php\nDisallow: /*wp-admin\nDisallow: /*page\n\n"
           "User-agent: GPTBot \nDisallow: /*\n\nUser-agent: Googlebot\nUser-agent: ClaudeBot\nAllow: /\nDisallow: /x$\n\n"
           "User-agent: GPTBot\nAllow: /public\n")
    groups = geo.robots_groups(txt)
    site = "https://example.test"
    assert not geo.can_fetch(groups, "GPTBot", f"{site}/")              # Disallow: /* (urllib.robotparser says yes)
    assert geo.can_fetch(groups, "GPTBot", f"{site}/public/a")          # a second GPTBot group merges in; longer rule wins
    assert geo.can_fetch(groups, "PerplexityBot", f"{site}/blog/x")    # falls back to *
    assert not geo.can_fetch(groups, "PerplexityBot", f"{site}/page-sitemap.xml")
    assert geo.can_fetch(groups, "PerplexityBot", f"{site}/wp-admin/admin-ajax.php")  # longer Allow beats Disallow
    assert geo.can_fetch(groups, "claudebot", f"{site}/x/y") and not geo.can_fetch(groups, "ClaudeBot", f"{site}/x")
    access = {a["crawler"]: a["home"] for a in geo.crawler_access(txt, site, ["GPTBot", "ClaudeBot", "CCBot"])}
    assert access == {"GPTBot": False, "ClaudeBot": True, "CCBot": True}


def test_the_crawl_skips_what_robots_txt_disallows_unless_told_not_to():
    import httpx

    def handler(request):
        p = request.url.path
        if p == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /private\n\nUser-agent: SEODesk\nDisallow: /drafts\n")
        if p == "/sitemap.xml":
            return httpx.Response(200, text="".join(f"<loc>{SITE}{x}</loc>" for x in ("/", "/drafts/a", "/private")))
        return httpx.Response(200, text=GOOD_PAGE)

    def go(respect):
        async def run():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
                return await crawl.crawl(SITE, client=http, respect_robots=respect)
        return asyncio.run(run())
    polite = go(True)
    # SEODesk's own group replaces *, so /private is allowed and /drafts isn't (RFC 9309)
    assert [p["url"] for p in polite["pages"]] == [f"{SITE}/", f"{SITE}/private"]
    assert polite["sitemap_count"] == 3 and "skipped 1 page(s)" in polite["errors"][0]
    assert len(go(False)["pages"]) == 3
