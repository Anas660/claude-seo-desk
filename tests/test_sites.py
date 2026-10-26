"""Several sites in one database, the site files and the `init` templates, sitemap indexes, the SEO / AEO / GEO scores
and their week and month comparisons, and DataForSEO, all with fakes."""
import asyncio
import json
import threading
from http.server import ThreadingHTTPServer

import httpx
import pytest

from seodesk import crawl, dataforseo, pipeline, progress
from seodesk.__main__ import main
from seodesk.dashboard.auth import Auth, set_password
from seodesk.dashboard.server import Dashboard, make_handler
from seodesk.db import DB
from seodesk.settings import Keyword, load, load_all, site_slugs

from helpers import BRIEF_JSON, GOOD_PAGE, BAD_PAGE, SITE, FakeLLM, fake_site, make_settings
from test_dashboard import call, login

# ── site files ───────────────────────────────────────────────────────────


def write_config(tmp_path, sites=("one", "two")):
    (tmp_path / "sites").mkdir()
    (tmp_path / "config.yaml").write_text(
        f"sites: [{', '.join(sites)}]\nllm: {{model: deepseek/deepseek-v4.1-flash}}\n"
        "limits: {max_run_usd: 1, max_monthly_usd: 30, site_monthly_usd: 4, max_serper_per_run: 50}\n"
        "crawl: {max_pages: 100, timeout_s: 9}\nai_crawlers: [GPTBot]\ndataforseo: {ranked_every_days: 7}\n")
    (tmp_path / "sites" / "one.yaml").write_text(
        "name: One\nsite: https://www.one.test/\ncountry: GB\nlanguage: en\nabout: >-\n  One sells\n  things.\n"
        "brand_terms: [One Co]\nlimits: {max_monthly_usd: 15}\ncrawl: {max_pages: 20, thin_ok: [/privacy/]}\n"
        "keywords:\n  - {keyword: one thing, tier: easy, target: /x}\n  - {keyword: other, country: us, tier: head}\n"
        "claims:\n  - {id: soc2, pattern: 'SOC 2', about_us: true, why: none}\n"
        "ai_visibility:\n  models: [perplexity/sonar]\n  prompts: [best thing, {prompt: \"What is One?\", branded: true}]\n")
    (tmp_path / "sites" / "two.yaml").write_text("site: https://two.test\nkeywords: []\n")
    return tmp_path / "config.yaml", tmp_path / "sites"


def test_site_files_merge_with_the_shared_config(tmp_path):
    cfg, sites = write_config(tmp_path)
    assert site_slugs(cfg) == ["one", "two"]
    one = load("one", cfg, sites)
    assert one.site == "https://www.one.test" and one.country == "gb" and one.about == "One sells things."
    assert [k.country for k in one.keywords] == ["gb", "us"]  # a keyword's country defaults to the site's
    assert one.max_monthly_usd == 15 and one.max_total_monthly_usd == 30 and one.max_serper_per_run == 50
    assert one.crawl["max_pages"] == 20 and one.crawl["timeout_s"] == 9 and one.crawl["concurrency"] == 4
    assert one.thin_ok == ["/privacy"] and one.ai_crawlers == ["GPTBot"] and one.dataforseo == {"ranked_every_days": 7}
    assert one.about_us.search("One Co is SOC 2 certified") and not one.about_us.search("Insist on SOC 2")
    assert one.visibility.models == ["perplexity/sonar"] and [p.branded for p in one.visibility.prompts] == [False, True]
    assert one.db_path == tmp_path / "data" / "seo.db"
    two = load("two", cfg, sites)
    assert two.label == "two.test" and two.max_monthly_usd == 4 and two.keywords == [] and two.visibility is None
    assert len(two.ai_crawlers) == 1  # config.yaml's list
    assert [s.slug for s in load_all(cfg, sites)] == ["one", "two"]
    assert load(None, cfg, sites).slug == "one"
    with pytest.raises(SystemExit):
        load("three", cfg, sites)


def test_init_writes_a_config_that_loads(tmp_path, capsys):
    assert main(["--home", str(tmp_path), "init"]) == 0
    assert (tmp_path / "config.yaml").exists() and (tmp_path / ".env.example").exists()
    s = load(config=tmp_path / "config.yaml")
    assert s.slug == "example" and s.site == "https://www.example.com" and s.keywords and s.claims
    assert all(k.country in dataforseo.LOCATIONS for k in s.keywords)
    assert s.claims[0].unless.search("we have no SOC 2 report")
    (tmp_path / "config.yaml").write_text("sites: [mine]\n")
    assert main(["--home", str(tmp_path), "init"]) == 0  # never overwrites without --force
    assert (tmp_path / "config.yaml").read_text() == "sites: [mine]\n"
    assert "kept" in capsys.readouterr().out


def test_a_missing_config_says_how_to_start(tmp_path):
    with pytest.raises(SystemExit, match="seodesk init"):
        load(config=tmp_path / "config.yaml")


# ── one database, many sites ─────────────────────────────────────────────

def test_sites_share_one_database_without_seeing_each_other(tmp_path):
    db = DB(tmp_path / "seo.db", "one")
    run = db.start_run()
    db.save_pages(run, [{"url": "https://one.test/", "status": 200}])
    db.upsert_actions(run, [{"title": "Fix it", "url": "/x"}])
    other = db.scoped("two")
    assert other.runs() == [] and other.action_counts() == {} and other.latest_pages() == []
    other.upsert_actions(other.start_run(), [{"title": "Fix it", "url": "/x"}])  # the same action, its own row
    assert other.action_counts() == {"open": 1} and db.action_counts() == {"open": 1}
    assert other.set_action(db.actions()[0]["id"], "done") is False  # can't touch another site's row
    other.add_spend("brief", 0.5)
    assert db.month_spend(all_sites=True) >= 0.5 and other.month_spend() == 0.5 and db.month_spend() == 0


# ── sitemaps ─────────────────────────────────────────────────────────────

def index_site(robots_line: bool = True) -> httpx.AsyncClient:
    pages = {"/": GOOD_PAGE, "/a": GOOD_PAGE, "/b": GOOD_PAGE}

    def handler(request: httpx.Request) -> httpx.Response:
        p = request.url.path
        if p == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n" + (f"Sitemap: {SITE}/sitemap-index.xml\n" if robots_line else ""))
        if p == "/sitemap-index.xml":
            return httpx.Response(200, text=f"<sitemapindex><sitemap><loc>{SITE}/sitemap-0.xml</loc></sitemap>"
                                            f"<sitemap><loc>{SITE}/sitemap-1.xml</loc></sitemap></sitemapindex>")
        if p == "/sitemap-0.xml":
            return httpx.Response(200, text=f"<urlset><url><loc>{SITE}/</loc></url><url><loc>{SITE}/a</loc></url></urlset>")
        if p == "/sitemap-1.xml":
            return httpx.Response(200, text=f"<urlset><url><loc><![CDATA[{SITE}/b]]></loc></url></urlset>")
        if p in pages:
            return httpx.Response(200, text=pages[p])
        return httpx.Response(404, text="nope")

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_a_sitemap_index_named_in_robots_is_followed():
    async def go():
        async with index_site() as http:
            return await crawl.sitemap_pages(http, SITE, 50)
    url, pages, errors = asyncio.run(go())
    assert url.endswith("/sitemap-index.xml") and pages == [f"{SITE}/", f"{SITE}/a", f"{SITE}/b"] and not errors


def test_without_robots_the_usual_paths_are_tried_and_crawl_sitemaps_limits_it():
    async def go(only=None):
        async with index_site(robots_line=False) as http:
            return await crawl.sitemap_pages(http, SITE, 50, only)
    assert asyncio.run(go())[1] == [f"{SITE}/", f"{SITE}/a", f"{SITE}/b"]  # /sitemap.xml 404s, -index answers
    url, pages, errors = asyncio.run(go(["/sitemap-1.xml", "/missing.xml"]))
    assert pages == [f"{SITE}/b"] and errors == ["/missing.xml returned 404"]


# ── scores and progress ──────────────────────────────────────────────────

PAGE_OK = {"url": f"{SITE}/", "status": 200, "issues": [], "schema_types": ["Organization"],
           "aeo": {"question_headings": 2, "answer_first": 1, "faq_schema": True, "tables": 1, "lists": 0}}
PAGE_BAD = {"url": f"{SITE}/x", "status": 200, "issues": [{"level": "error", "code": "noindex"}, {"level": "warn", "code": "title-long"}],
            "schema_types": [], "aeo": {}}
GEO = {"crawlers": [{"crawler": "GPTBot", "home": True}, {"crawler": "CCBot", "home": False}],
       "llms": [{"path": "/llms.txt", "status": 200}, {"path": "/llms-full.txt", "status": 404}], "claims": []}


def test_scores_weigh_their_parts_and_skip_what_is_missing():
    serp = [{"keyword": "a", "position": 1}, {"keyword": "b", "position": 8}, {"keyword": "c", "position": None}]
    s = progress.score([PAGE_OK, PAGE_BAD], GEO, serp, has_claims=True)
    p = s["parts"]
    assert p["seo"] == {"visibility": 50.0, "technical": 50.0, "on_page": 50.0} and s["seo"] == 50
    assert p["aeo"]["questions"] == 50.0 and p["aeo"]["faq"] == 50.0 and s["aeo"] == 50
    assert p["geo"]["crawlers"] == 50.0 and p["geo"]["llms"] == 60 and p["geo"]["claims"] == 100 and p["geo"]["mentions"] is None
    assert s["geo"] == round((50 * .25 + 60 * .15 + 50 * .2 + 100 * .15) / .75)
    assert p["metrics"]["top10"] == 2 and p["metrics"]["avg_position"] == 4.5 and p["metrics"]["errors"] == 1
    assert progress.score([PAGE_OK], None, [])["parts"]["seo"]["visibility"] is None  # no rank checks: left out
    with_probes = progress.score([PAGE_OK], GEO, [], probes={"summary": {"prompts": 100, "mentions": 5}})
    assert with_probes["parts"]["geo"]["mentions"] == 20.0 and with_probes["parts"]["metrics"]["ai_mentions"] == 5


def scored_db(tmp_path, days):
    """A database with one scored run a day: (date, seo, aeo, geo)."""
    db = DB(tmp_path / "p.db")
    for d, seo, aeo, geo in days:
        run = db.start_run()
        db.save_scores(run, d, {"seo": seo, "aeo": aeo, "geo": geo, "overall": round((seo + aeo + geo) / 3),
                                "parts": {"metrics": {"top10": seo // 10, "avg_position": 20 - seo / 10}}})
    return db


def test_weeks_and_months_compare_with_the_one_before(tmp_path):
    db = scored_db(tmp_path, [("2026-09-28", 30, 50, 40), ("2026-09-30", 32, 50, 40),        # week of 28 Sep
                              ("2026-10-06", 40, 50, 36), ("2026-10-07", 42, 50, 36),        # week of 5 Oct
                              ("2026-10-13", 50, 50, 30)])                                   # week of 12 Oct
    p = progress.progress(db, days=10000)
    assert [w["period"] for w in p["weeks"]] == ["2026-09-28", "2026-10-05", "2026-10-12"]
    assert p["weeks"][1]["seo"] == 41.0 and p["weeks"][1]["change"]["seo"] == 10.0
    assert p["week"]["seo"] == {"now": 50.0, "before": 41.0, "change": 9.0, "trend": "up"}
    assert p["week"]["geo"]["trend"] == "down" and p["week"]["aeo"]["trend"] == "flat"
    assert p["week"]["verdict"] == "Mixed: SEO +9; GEO -6; AEO flat."
    assert [m["period"] for m in p["months"]] == ["2026-09", "2026-10"]
    assert p["month"]["seo"]["before"] == 31.0 and p["month"]["seo"]["change"] == round(44 - 31, 1)
    assert p["weeks"][-1]["metric_change"]["avg_position"] == -0.9  # lower position = better; the UI flips the colour
    assert progress.trend(-0.9, lower_better=True, threshold=0.5) == "up"
    assert len(p["series"]) == 5 and p["latest"]["date"] == "2026-10-13"


def test_one_day_of_history_says_so(tmp_path):
    p = progress.progress(scored_db(tmp_path, [("2026-10-13", 50, 51, 30)]), days=10000)
    assert p["week"]["verdict"] == "Not enough history yet." and p["week"]["seo"]["now"] == 50.0


def test_old_runs_are_scored_from_what_they_saved(tmp_path):
    settings = make_settings(tmp_path, slug="")
    db = DB(settings.db_path)
    run = db.start_run()
    db.save_pages(run, [PAGE_OK, PAGE_BAD])
    db.save_snapshot(run, "geo", GEO)
    db.save_serp(run, [{"keyword": "a", "country": "us", "position": 3}])
    db.finish_run(run, "ok", pages=2)
    assert progress.backfill(db, settings) == 1 and progress.backfill(db, settings) == 0
    row = db.scores()[0]
    assert row["run_id"] == run and row["seo"] is not None and row["parts"]["metrics"]["top10"] == 1
    assert progress.rescore(db, settings) == 1


# ── DataForSEO ───────────────────────────────────────────────────────────

def dfs_api(calls, balance_status=20000):
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.url.path, json.loads(request.content) if request.content else None))
        if request.headers.get("authorization", "") == "":
            return httpx.Response(401)
        if request.url.path.endswith("/search_volume/live"):
            words = json.loads(request.content)[0]["keywords"]
            return httpx.Response(200, json={"status_code": 20000, "cost": 0.05, "tasks": [{"status_code": 20000, "result": [
                {"keyword": w, "search_volume": 1000 + i, "cpc": 1.2, "competition": "LOW",
                 "monthly_searches": [{"year": 2026, "month": 9, "search_volume": 900}, {"year": 2026, "month": 8, "search_volume": 800}]}
                for i, w in enumerate(words)]}]})
        if request.url.path.endswith("/ranked_keywords/live"):
            return httpx.Response(200, json={"status_code": 20000, "cost": 0.0201, "tasks": [{"status_code": 20000, "result": [{
                "total_count": 57, "metrics": {"organic": {"pos_1": 2, "pos_2_3": 3, "pos_4_10": 10, "pos_11_20": 12, "etv": 340.5,
                                                           "is_new": 4, "is_up": 6, "is_down": 2, "is_lost": 1}},
                "items": [{"keyword_data": {"keyword": "invoicing software", "keyword_info": {"search_volume": 90000}},
                           "ranked_serp_element": {"serp_item": {"rank_group": 14, "url": f"{SITE}/", "etv": 12.04}}}]}]}]})
        if request.url.path.endswith("/user_data"):
            return httpx.Response(200, json={"status_code": balance_status, "status_message": "Payment Required." if balance_status != 20000 else "Ok.",
                                             "cost": 0, "tasks": [{"status_code": 20000, "result": [{"money": {"balance": 49.5}}]}]})
        return httpx.Response(404)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_dataforseo_volumes_and_ranked_keywords_are_read_and_costed():
    calls = []

    async def go():
        async with dfs_api(calls) as http:
            c = dataforseo.DataForSEO("login", "pw", http)
            v = await dataforseo.volumes(c, [Keyword("Invoice Template", "us", "easy"), Keyword("invoice maker", "de", "easy")], "de")
            r = await dataforseo.ranked(c, "www.example-site.test", "us")
            return c, v, r, await c.balance()
    c, v, r, balance = asyncio.run(go())
    assert v["by_keyword"]["invoice template|us"]["volume"] == 1000 and v["by_keyword"]["invoice maker|de"]["trend"] == [800, 900]
    assert calls[1][1][0]["location_code"] == 2276 and calls[1][1][0]["language_code"] == "de"
    assert calls[2][1][0]["target"] == "example-site.test"
    assert r["total"] == 57 and r["top3"] == 5 and r["top10"] == 15 and r["top20"] == 27 and r["etv"] == 340.5
    assert r["items"][0] == {"keyword": "invoicing software", "volume": 90000, "position": 14, "url": f"{SITE}/", "etv": 12.0}
    assert round(c.cost, 4) == 0.1201 and balance == 49.5
    assert dataforseo.volume_for(v, "Invoice Template", "us") == (1000, [800, 900])


def test_dataforseo_refuses_cleanly_without_money_or_login():
    async def go(status, login):
        async with dfs_api([], balance_status=status) as http:
            return await dataforseo.DataForSEO(login, "pw", http).balance()
    from seodesk.errors import FatalError
    with pytest.raises(FatalError, match="out of money"):
        asyncio.run(go(40200, "login"))
    with pytest.raises(FatalError, match="aren't set"):
        asyncio.run(go(20000, ""))


def test_dataforseo_runs_only_when_due(tmp_path):
    settings = make_settings(tmp_path, dataforseo_login="l", dataforseo_password="p",
                             dataforseo={"volume_every_days": 28, "ranked_every_days": 7})
    db = DB(settings.db_path, settings.slug)
    calls, notes = [], []

    async def go():
        async with dfs_api(calls) as http:
            run = db.start_run()
            first = await pipeline.refresh_dataforseo(settings, db, run, http, notes)
            second = await pipeline.refresh_dataforseo(settings, db, run, http, notes)
            return first, second
    first, second = asyncio.run(go())
    assert first > 0 and second == 0 and not notes
    assert len(calls) == 2 and db.latest_snapshot("ranked")["total"] == 57 and db.month_spend() > 0
    settings.keywords.append(Keyword("new keyword", "us", "easy"))  # a changed plan refreshes the volumes early
    assert dataforseo.volumes_due(db.latest_snapshot("volumes"), settings.keywords, 28)


# ── a whole run, and the dashboard ───────────────────────────────────────

def test_a_run_scores_the_day_and_tells_the_brief(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "test")
    settings = make_settings(tmp_path, slug="docs", name="Docs", about="Docs makes posts.", rules="claiming SOC 2")
    db = DB(settings.db_path)
    http = fake_site({"/": GOOD_PAGE, "/other": BAD_PAGE})
    llm = FakeLLM([{"content": "Ready."}, {"content": json.dumps(BRIEF_JSON)}])
    res = asyncio.run(pipeline.run(settings, db, client=llm, http=http))
    asyncio.run(http.aclose())
    assert res.status == "ok", res.notes
    site_db = db.scoped("docs")
    assert site_db.scores()[0]["seo"] is not None and db.scores() == []  # stored under its own site
    assert site_db.latest_brief() and not db.latest_brief()
    system, evidence = llm.sent[0]["messages"][0]["content"], llm.sent[0]["messages"][1]["content"]
    assert "analyst for www.example-site.test. Docs makes posts." in system and "claiming SOC 2" in system
    assert "SEO Desk scores" in evidence and "Search Console isn't connected" in evidence


@pytest.fixture
def two_sites(tmp_path):
    env = tmp_path / ".env"
    set_password("correct horse battery", env)
    a = make_settings(tmp_path, slug="acme", name="Acme")
    b = make_settings(tmp_path, slug="beta", name="Beta")
    db = DB(a.db_path)
    started = []
    app = Dashboard([a, b], db=db, auth=Auth(env), start=lambda *x: started.append(x))
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}", db, started
    server.shutdown()


def test_the_dashboard_keeps_sites_apart_and_shows_progress(two_sites):
    base, db, started = two_sites
    cookie = login(base)
    beta = db.scoped("beta")
    run = beta.start_run()
    beta.save_brief(run, BRIEF_JSON, "m/x", 0.01)
    beta.save_scores(run, "2026-10-20", {"seo": 40, "aeo": 50, "geo": 60, "overall": 50, "parts": {"metrics": {}}})
    beta.finish_run(run, "ok")
    s_beta = call(base, "/api/summary?site=beta", cookie=cookie)[1]
    s_default = call(base, "/api/summary", cookie=cookie)[1]
    assert s_beta["name"] == "Beta" and s_beta["brief"]["headline"] == BRIEF_JSON["headline"]
    assert s_default["slug"] == "acme" and s_default["brief"] is None
    assert [x["slug"] for x in s_default["sites"]] == ["acme", "beta"]
    prog = call(base, "/api/progress?site=beta", cookie=cookie)[1]
    assert prog["latest"]["seo"] == 40 and prog["weeks"][0]["period"] == "2026-10-19"  # weeks start on Monday
    over = call(base, "/api/overview", cookie=cookie)[1]
    assert [r["slug"] for r in over["sites"]] == ["acme", "beta"] and over["sites"][1]["scores"]["geo"] == 60
    assert call(base, "/api/run", {"site": "beta"}, cookie=cookie)[1]["ok"]
    assert started == [("run", "--kind", "manual", "--site", "beta")]


def test_a_day_without_rank_checks_keeps_yesterdays_visibility(tmp_path):
    settings = make_settings(tmp_path, slug="")  # has a keyword plan
    db = DB(settings.db_path)
    first = db.start_run()
    db.save_pages(first, [PAGE_OK])
    db.finish_run(first, "ok", pages=1)
    second = db.start_run()
    db.save_pages(second, [PAGE_OK])
    db.save_serp(second, [{"keyword": "a", "country": "us", "position": 2}])
    db.finish_run(second, "ok", pages=1)
    third = db.start_run()
    db.save_pages(third, [PAGE_OK])
    db.finish_run(third, "ok", pages=1)
    progress.backfill(db, settings)
    rows = {r["run_id"]: r for r in db.conn.execute("SELECT run_id, seo, parts FROM scores").fetchall()}
    assert rows[first]["seo"] is None                      # no rank check yet: no SEO score, not a technical-only 100
    assert rows[second]["seo"] == rows[third]["seo"] == round(90 * .5 + 100 * .3 + 100 * .2)
    assert json.loads(rows[third]["parts"])["carried"] == ["visibility"]


def test_without_a_serper_key_seo_is_the_technical_parts(tmp_path):
    settings = make_settings(tmp_path, slug="", serper_key="")  # a keyword plan, but no way to check it
    db = DB(settings.db_path)
    run = db.start_run()
    db.save_pages(run, [PAGE_OK])
    db.finish_run(run, "ok", pages=1)
    progress.backfill(db, settings)
    assert db.scores()[0]["seo"] == 100


def test_the_demo_seeds_two_sites_with_history(tmp_path):
    from seodesk.demo import seed
    seed(tmp_path)
    sites = load_all(tmp_path / "config.yaml")
    assert [s.slug for s in sites] == ["acme", "northwind"] and sites[0].visibility is not None
    db = DB(sites[0].db_path, "acme")
    p = progress.progress(db, days=100)
    assert p["days"] == 56 and p["month"]["seo"]["trend"] == "up"
    assert db.latest_brief()["actions"] and db.latest_snapshot("probes")["summary"]["prompts"] == 4
    with pytest.raises(SystemExit):
        seed(tmp_path)  # never over an existing database
