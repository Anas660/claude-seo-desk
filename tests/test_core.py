"""The SQLite store, the agent loop and one whole run with everything faked."""
import asyncio
import json

from seodesk import agent, pipeline
from seodesk.db import DB

from helpers import BAD_PAGE, BRIEF_JSON, GOOD_PAGE, SITE, FakeLLM, fake_site, make_settings


def test_actions_dedupe_across_days_and_keep_your_status(tmp_path):
    db = DB(tmp_path / "x.db")
    run = db.start_run()
    a = {"area": "SEO", "priority": 1, "title": "Fix the title", "url": "/x", "why": "w"}
    assert db.upsert_actions(run, [a]) == 1
    aid = db.actions()[0]["id"]
    db.set_action(aid, "dismissed")
    assert db.upsert_actions(run, [{**a, "why": "new evidence"}]) == 0
    row = db.actions()[0]
    assert row["status"] == "dismissed" and row["why"] == "new evidence" and row["seen_count"] == 2


def test_spend_and_serp_trend(tmp_path):
    db = DB(tmp_path / "x.db")
    db.add_spend("brief", 0.02)
    db.add_spend("brief", 0)  # nothing recorded for free calls
    assert round(db.month_spend(), 3) == 0.02
    run = db.start_run()
    db.save_serp(run, [{"keyword": "k", "country": "us", "tier": "easy", "position": 9}])
    latest = db.serp_latest()
    assert latest[0]["position"] == 9 and latest[0]["position_7d"] is None


def test_tools_only_read_our_own_pages():
    tools = agent.Tools(SITE, [{"url": f"{SITE}/pricing", "title": "Pricing", "text": "Plans", "issues": []}], None)
    assert "Pricing" in asyncio.run(tools.run("read_page", {"url": "/pricing"}))
    assert "Only pages on" in asyncio.run(tools.run("read_page", {"url": "https://evil.test/pricing"}))
    assert "aren't available" in asyncio.run(tools.run("google", {"query": "x"}))
    assert "no tool called" in asyncio.run(tools.run("search_console", {"contains": "x"}))


def test_brief_loop_runs_a_tool_then_writes_strict_json():
    llm = FakeLLM([
        {"content": "", "tool_calls": [{"id": "1", "function": {"name": "read_page", "arguments": json.dumps({"url": "/other"})}}]},
        {"content": "Ready."},
        {"content": json.dumps(BRIEF_JSON)},
    ])
    tools = agent.Tools(SITE, [{"url": f"{SITE}/other", "title": "Other", "text": "SOC 2", "issues": []}], None)
    brief, cost = asyncio.run(agent.write_brief(llm, "m/x", "low", "evidence", tools, 3, 1.0))
    assert brief is not None and brief.actions[0].area == "GEO" and round(cost, 3) == 0.006
    tool_msg = [m for m in llm.sent[1]["messages"] if m["role"] == "tool"][0]
    assert "SOC 2" in tool_msg["content"]
    assert llm.sent[-1]["response_format"]["json_schema"]["strict"] is True
    assert llm.sent[0]["effort"] == "low"  # the client turns it into each API's own setting


def test_the_system_prompt_says_what_the_site_is_and_what_it_must_not_claim(tmp_path):
    s = agent.system_prompt(make_settings(tmp_path, about="Acme sends invoices.", rules="SOC 2"))
    assert "analyst for www.example-site.test. Acme sends invoices." in s and "they aren't true or aren't allowed: SOC 2" in s
    bare = agent.system_prompt(make_settings(tmp_path))
    assert "analyst for www.example-site.test.\n" in bare and "Never suggest" not in bare


def test_a_whole_run_saves_every_step(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "test")
    settings = make_settings(tmp_path)
    db = DB(settings.db_path)
    serper = {"invoice template": {"organic": [{"link": f"{SITE}/templates/invoice", "position": 4}]}}
    http = fake_site({"/": GOOD_PAGE, "/other": BAD_PAGE}, serper=serper)
    llm = FakeLLM([{"content": "Ready."}, {"content": json.dumps(BRIEF_JSON)}])
    res = asyncio.run(pipeline.run(settings, db, client=llm, http=http))
    asyncio.run(http.aclose())
    assert res.status == "ok", res.notes
    assert res.pages == 2 and res.serper_calls == 3 and res.new_actions == 1  # "invoicing software" missed page 1, so page 2 too
    run = db.scoped("acme").runs(1)[0]
    assert run["status"] == "ok" and run["note"].startswith("SEO ") and "2 pages" in run["note"]
    site_db = db.scoped("acme")
    assert site_db.latest_brief()["headline"] == BRIEF_JSON["headline"]
    assert {r["keyword"]: r["position"] for r in site_db.serp_latest()} == {"invoice template": 4, "invoicing software": None}
    assert site_db.latest_snapshot("geo")["summary"]["claim_hits"] >= 1  # the SOC 2 sentence on /other
    evidence = llm.sent[0]["messages"][1]["content"]
    assert "invoice template" in evidence and "Today's live sitemap lists 2 URLs" in evidence
    assert site_db.month_spend() > 0 and site_db.scores()[0]["seo"] is not None


def test_a_run_without_keys_still_crawls_and_scores(tmp_path, monkeypatch):
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    settings = make_settings(tmp_path, serper_key="")
    db = DB(settings.db_path)
    http = fake_site({"/": GOOD_PAGE})
    res = asyncio.run(pipeline.run(settings, db, http=http))
    asyncio.run(http.aclose())
    assert res.status == "partial" and res.pages == 1
    assert any("SERPER_API_KEY" in n for n in res.notes) and any("LLM_API_KEY" in n for n in res.notes)
    assert res.scores and res.scores["aeo"] is not None and res.scores["geo"] is not None


def test_a_spent_budget_skips_the_paid_steps(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "test")
    settings = make_settings(tmp_path, max_monthly_usd=0.01)
    db = DB(settings.db_path)
    db.scoped("acme").add_spend("brief", 0.02)
    http = fake_site({"/": GOOD_PAGE})
    res = asyncio.run(pipeline.run(settings, db, http=http, client=FakeLLM([])))
    asyncio.run(http.aclose())
    assert res.serper_calls == 0 and not db.scoped("acme").latest_brief()
    assert any("budget" in n for n in res.notes)


def test_an_empty_summary_is_sent_back_once():
    empty = {**BRIEF_JSON, "summary": " "}
    llm = FakeLLM([{"content": "Ready."}, {"content": json.dumps(empty)}, {"content": json.dumps(BRIEF_JSON)}])
    brief, _ = asyncio.run(agent.write_brief(llm, "m/x", "low", "evidence", agent.Tools(SITE, [], None), 2, 1.0))
    assert brief is not None and brief.summary == BRIEF_JSON["summary"]
    assert "summary is empty" in llm.sent[-1]["messages"][-1]["content"]


def test_priorities_numbered_one_to_n_become_three_bands():
    acts = [{"title": f"a{i}", "priority": i} for i in range(1, 10)]
    got = [a["priority"] for a in agent.normalise_priorities(acts)]
    assert got == [1, 1, 1, 2, 2, 2, 2, 3, 3]
    ok = [{"title": "x", "priority": 2}]
    assert agent.normalise_priorities(ok) == ok


def test_new_pages_are_named_so_no_data_isnt_read_as_a_problem(tmp_path):
    db = DB(tmp_path / "x.db")
    r1 = db.start_run()
    db.save_pages(r1, [{"url": f"{SITE}/", "status": 200}])
    db.conn.execute("UPDATE runs SET started_at = '2026-10-01T07:00:00Z' WHERE id = ?", (r1,))
    r2 = db.start_run()
    db.save_pages(r2, [{"url": f"{SITE}/", "status": 200}, {"url": f"{SITE}/templates/invoice", "status": 200}])
    db.conn.commit()
    new = pipeline.recent_pages(db)
    assert list(new) == ["/templates/invoice"]
    ev = pipeline.evidence(make_settings(tmp_path), {"summary": {}, "pages": []}, {}, [], [], new)
    assert "first crawled in the last 14 days" in ev and "/templates/invoice" in ev
    assert "no sitemap that could be read" in ev  # that crawl had none


def test_the_same_task_reworded_on_the_same_page_is_one_action(tmp_path):
    from seodesk.db import norm_url, same_task
    assert norm_url("https://www.example.com/about/") == norm_url("/about") == "/about"
    assert same_task("Restore AI Overview tracking (the engine has never succeeded) before writing more AEO content",
                     "Top up DataForSEO credit to restore AI Overview tracking")
    assert same_task("Remove three false claims from /industries in one edit", "Remove the integration claim from /industries")
    assert not same_task("Fix wrong pricing + remove unsupported white-label claims on /pricing",
                         "Write one canonical 25-40 word answer to \"What is Acme?\" on /pricing")
    db = DB(tmp_path / "x.db")
    run = db.start_run()
    db.upsert_actions(run, [{"title": "Answer the two checkout queries verbatim on the post", "url": "https://www.example.com/blog/x"}])
    assert db.upsert_actions(run, [{"title": "Re-answer the two checkout queries on the post", "url": "/blog/x"}]) == 0
    assert db.upsert_actions(run, [{"title": "Answer the two checkout queries verbatim on the post", "url": "/blog/other"}]) == 1
    rows = db.actions("open")
    assert len(rows) == 2 and max(r["seen_count"] for r in rows) == 2


def test_existing_duplicates_merge_into_the_oldest(tmp_path):
    db = DB(tmp_path / "x.db")
    run = db.start_run()
    for t, u in [("Close the 'unknown' gap with third-party descriptions", "/about"),
                 ("Close the 'unknown' gap with third-party descriptions", "https://www.example.com/about"),
                 ("Swap /pricing Product markup for SoftwareApplication", "/pricing")]:
        db.conn.execute("INSERT INTO actions(key, area, priority, title, url, first_seen, last_seen, run_id) VALUES (?, 'GEO', 2, ?, ?, ?, ?, ?)",
                        (t + u, t, u, "2026-10-17T00:00:00Z", "2026-10-18T00:00:00Z", run))
    db.conn.commit()
    assert db.merge_duplicates() == 1
    rows = db.actions("open")
    assert len(rows) == 2 and {r["url"] for r in rows} == {"/about", "/pricing"}
