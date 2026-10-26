# Changelog

## 0.1.1 (2026-10-26)

- The GitHub Action's description fits the Marketplace's 125-character limit, so the Action can be listed there.
- Packaging: the license is declared as an SPDX expression, and each supported Python version is listed.
- Dependabot, a release workflow that publishes to PyPI through trusted publishing, and a code of conduct.

## 0.1.0 (2026-10-26)

First public release.

- `seodesk audit URL`: a one-off SEO / AEO / GEO audit with no config or keys, as text, Markdown or JSON, with quick
  wins and a `--fail-under` threshold for CI.
- A GitHub Action that runs the audit and writes the report to the job summary.
- The daily run for any number of sites: sitemap crawl, AI crawler access, llms.txt, claims scan, Google ranks
  (Serper), search volumes and ranked keywords (DataForSEO), WordPress.org and Shopify listings, scores with week and
  month comparisons, and an AI analyst's brief with a deduplicated action queue.
- AI visibility: asks AI models your customers' questions and records mentions, links, cited sites and false claims.
- The dashboard, with a password or a local-only `--no-password` mode.
- Any OpenAI-compatible chat API for the analyst (OpenRouter by default).
- The crawler respects robots.txt for its own user agent.
- `seodesk llms-txt URL`: drafts an llms.txt from the site's pages.
- `seodesk mcp`: an MCP server with four read-only tools (audit_site, inspect_page, check_ai_access,
  draft_llms_txt) for Claude Code, Claude Desktop, Cursor and other MCP clients.
- A Claude Code plugin with two skills: audit a site and fix what it finds in the codebase, and write its llms.txt.
- `seodesk demo`: the dashboard on eight weeks of sample data, no setup.
- `uvx seo-desk ...` works without installing anything.
