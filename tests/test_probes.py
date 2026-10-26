"""AI visibility (probes.py): what counts as naming or citing the site, the caps, and the probes inside a daily run."""
import asyncio
import json
import re

from seodesk import pipeline, probes
from seodesk.db import DB, now
from seodesk.settings import Claim, Prompt, Visibility

from helpers import BRIEF_JSON, GOOD_PAGE, FakeLLM, fake_site, make_settings


def vis(**over):
    base = dict(models=["perplexity/sonar", "openai/gpt-5-mini:online"],
                prompts=[Prompt("best invoicing software"), Prompt("What is Acme?", branded=True)])
    base.update(over)
    return Visibility(**base)


def test_citations_come_from_annotations_perplexity_and_the_text():
    message = {"content": "Try Acme (https://www.example-site.test/pricing). Or https://rival.test.",
               "annotations": [{"type": "url_citation", "url_citation": {"url": "https://docs.rival.test/a"}}]}
    got = probes.citations({"citations": ["https://news.test/x", 3]}, message)
    assert got == ["https://docs.rival.test/a", "https://news.test/x", "https://www.example-site.test/pricing", "https://rival.test"]


def test_the_brand_pattern_names_the_site_and_not_lookalikes(tmp_path):
    brand = make_settings(tmp_path).brand
    assert brand.search("I'd pick Acme for this") and brand.search("see example-site.test for details")
    assert not brand.search("Acmeware is different") and not brand.search("an acme-inc product")


def test_every_prompt_goes_to_every_model_and_is_read(tmp_path):
    settings = make_settings(tmp_path, visibility=vis(),
                             claims=[Claim("soc2", re.compile(r"SOC 2"), "none", about_us=True)])
    llm = FakeLLM([
        ({"content": "Top picks: Acme, Rival."}, {"citations": ["https://www.example-site.test/", "https://rival.test/x"]}),
        {"content": "Rival and Other are popular.", "annotations": [{"type": "url_citation", "url_citation": {"url": "https://rival.test/y"}}]},
        {"content": "Acme is an invoicing tool. Acme is SOC 2 certified."},
        {"content": "I'm not familiar with Acme."},
    ])
    out = asyncio.run(probes.run(llm, settings, budget_usd=1.0))
    assert out["calls"] == 4 and not out["errors"] and round(out["cost"], 3) == 0.008
    assert [b["messages"] for b in llm.sent[:1]] == [[{"role": "user", "content": "best invoicing software"}]]
    s = out["summary"]
    assert s["prompts"] == 2 and s["mentions"] == 1 and s["cited"] == 1 and s["flagged"] == 1
    assert s["top_cited"][0] == {"domain": "rival.test", "answers": 2}
    assert s["by_model"]["perplexity/sonar"] == {"answers": 2, "unbranded": 1, "mentions": 1, "cited": 1, "flagged": 1}
    assert out["results"][2]["flags"] == ["soc2"] and out["results"][2]["branded"]


def test_the_call_cap_and_the_budget_stop_the_probes(tmp_path):
    capped = asyncio.run(probes.run(FakeLLM([]), make_settings(tmp_path, visibility=vis(max_calls=3)), 1.0))
    assert capped["calls"] == 3 and "call limit" in capped["errors"][0]
    broke = asyncio.run(probes.run(FakeLLM([]), make_settings(tmp_path, visibility=vis()), budget_usd=0.003))
    assert broke["calls"] == 2 and broke["errors"] == ["budget used up"]


def test_probes_run_weekly_or_when_the_plan_changes(tmp_path):
    settings = make_settings(tmp_path, visibility=vis())
    assert probes.probes_due(None, settings)
    fresh = {"_at": now(), "plan": probes.plan(settings)}
    assert not probes.probes_due(fresh, settings)
    settings.visibility.prompts.append(Prompt("a new question"))
    assert probes.probes_due(fresh, settings)
    assert not probes.probes_due(None, make_settings(tmp_path))  # no ai_visibility: never


def test_a_run_with_ai_visibility_scores_mentions_and_tells_the_brief(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "test")
    settings = make_settings(tmp_path, visibility=vis(models=["perplexity/sonar"]), serper_key="")
    db = DB(settings.db_path)
    llm = FakeLLM([{"content": "Acme is the one to try."}, {"content": "Acme makes invoices."},
                   {"content": "Ready."}, {"content": json.dumps(BRIEF_JSON)}])
    http = fake_site({"/": GOOD_PAGE})
    res = asyncio.run(pipeline.run(settings, db, client=llm, http=http))
    asyncio.run(http.aclose())
    site = db.scoped("acme")
    snap = site.latest_snapshot("probes")
    assert snap["summary"]["mentions"] == 1 and snap["summary"]["prompts"] == 1
    assert site.scores()[0]["parts"]["geo"]["mentions"] == 100.0  # 1 of 1 unbranded answers
    assert "AI visibility" in llm.sent[2]["messages"][1]["content"]
    assert site.month_spend() > 0 and res.llm_usd > 0
    again = asyncio.run(pipeline.refresh_visibility(settings, site, 99, llm, []))
    assert again == 0.0  # asked today already
