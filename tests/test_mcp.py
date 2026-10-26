"""The MCP server's tools, the agent-sized report, and the llms.txt draft."""
import asyncio
import json

import pytest

from seodesk import audit, llmstxt, mcp_server

from helpers import BAD_PAGE, GOOD_PAGE, SITE, fake_site


def run(coro_fn, pages, **kw):
    async def go():
        async with fake_site(pages, **kw) as http:
            return await coro_fn(http)
    return asyncio.run(go())


def test_check_ai_access_reads_robots_and_llms_files():
    out = run(lambda http: mcp_server.check_ai_access(SITE, http=http), {"/": GOOD_PAGE}, llms=None)
    assert out["site"] == SITE and out["ai_crawlers"]["GPTBot"] == "allowed"
    assert out["ai_crawlers"]["CCBot"].startswith("blocked") and out["llms_files"] == {"/llms.txt": 404, "/llms-full.txt": 404}


def test_inspect_page_checks_one_page():
    out = run(lambda http: mcp_server.inspect_page(f"{SITE}/other", http=http), {"/other": BAD_PAGE})
    codes = {i["code"] for i in out["issues"]}
    assert out["status"] == 200 and {"title-long", "h1-many", "noindex"} <= codes and "text" not in out
    assert "SOC 2" in out["text_start"]


def test_the_agent_report_is_small_and_keeps_what_matters():
    async def go(http):
        return await audit.audit(SITE, http=http)
    report = run(go, {"/": GOOD_PAGE, "/other": BAD_PAGE})
    small = audit.for_agents(report)
    assert small["scores"] == report["scores"] and small["ai_crawlers"]["blocked"] == ["CCBot"]
    assert small["pages_with_issues"][0]["url"] == f"{SITE}/other" and small["quick_wins"]
    assert all("text" not in p for p in small["pages_with_issues"]) and len(json.dumps(small)) < len(json.dumps(report))


def test_an_llms_txt_draft_groups_pages_and_drops_noindex():
    home = GOOD_PAGE.replace("<title>", '<meta property="og:site_name" content="Acme Invoicing"><title>')
    pages = {"/": home, "/pricing": GOOD_PAGE.replace("Invoicing Software for Freelancers | Acme", "Pricing | Acme"),
             "/blog/first-invoice": GOOD_PAGE, "/other": BAD_PAGE}
    text = run(lambda http: mcp_server.draft_llms_txt(SITE, http=http), pages)
    lines = text.splitlines()
    assert lines[0] == "# Acme Invoicing" and lines[2].startswith("> Invoicing software for freelancers")
    assert "## Pages" in text and "## Blog" in text and text.index("## Pages") < text.index("## Blog")
    assert f"- [Pricing]({SITE}/pricing): Invoicing software" in text  # "| Acme" dropped though og:site_name differs
    assert "[Invoicing Software for Freelancers]" in text
    assert "/other" not in text  # noindex pages stay out


def test_the_site_name_comes_from_the_homepage_title_when_there_is_no_og_name():
    assert llmstxt.site_name({"title": "Invoicing for freelancers | Acme"}, "www.acme.test") == "Acme"
    assert llmstxt.site_name({"title": "Acme - invoicing for freelancers"}, "www.acme.test") == "Acme"
    assert llmstxt.site_name({}, "www.acme.test") == "acme.test"
    assert llmstxt.section("https://x.test/docs/getting-started") == "Docs" and llmstxt.section("https://x.test/about") == "Pages"


def test_the_server_offers_four_read_only_tools():
    pytest.importorskip("mcp.server.mcpserver")
    tools = {t.name: t for t in asyncio.run(mcp_server.build().list_tools())}
    assert set(tools) == {"audit_site", "inspect_page", "check_ai_access", "draft_llms_txt"}
    assert all(t.annotations.read_only_hint for t in tools.values())
    assert set(tools["audit_site"].input_schema["properties"]) == {"url", "max_pages", "keywords"}
