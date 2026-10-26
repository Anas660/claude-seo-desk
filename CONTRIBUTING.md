# Contributing

Thanks for helping. Bug reports, new checks, better scoring and docs fixes are all welcome.

## Set up

```console
git clone https://github.com/Anas660/claude-seo-desk && cd claude-seo-desk
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/pytest -q
```

Python 3.9 or newer. The tests fake every network call (httpx's MockTransport), so they need no keys and no internet.

## Making a change

1. Open an issue first for anything bigger than a small fix, so we can agree on the shape before you write it.
2. Add or change a test with every behaviour change. Tests read like sentences
   (`test_a_site_without_a_sitemap_is_crawled_from_its_homepage`); follow that.
3. Keep each step of a run independent: a step that fails adds a note to the run and the others still go.
4. Anything that costs money (a model call, a search) goes through the spending caps and into the `spend` table.
5. A new check that changes a score: explain the reasoning in `progress.py`'s docstring and in the README, and
   remember `seodesk rescore` recomputes old runs with it.
6. Run `pytest -q` before you open the pull request.

## Adding a crawl check

Page checks live in `crawl.analyse()`. Each issue has a level (`error` for something broken, `warn` for something to
improve), a stable `code`, and a short `text` that says what's wrong with numbers. Add the code to `audit.ISSUES`
so the report can describe it, and to `progress.ON_PAGE` if it should count against the on-page score.

## The Claude Code plugin and the MCP server

`.claude-plugin/` and `skills/` make this repository a Claude Code plugin, and `.mcp.json` starts its MCP server
(`seodesk/mcp_server.py`) through `uvx`. If you open this repository in Claude Code, it offers that MCP server as a
project server; you can decline it, since it's meant for people who install the plugin. Test the server with
`seodesk mcp` and any MCP client, or `pytest tests/test_mcp.py`.

## The README's pictures

- The dashboard screenshots come from `seodesk demo` (sample data), taken at 1280 px wide.
- The terminal picture is a real run: `seodesk audit example.com > audit.txt`, then
  `python scripts/terminal_svg.py audit.txt "uvx seo-desk audit example.com" > docs/assets/audit.svg`.

## Reporting a bug

Include the command you ran, what you expected, what happened, and `seodesk --version`. If a specific site behaves
oddly, the JSON report (`seodesk audit URL --format json`) usually shows why.
