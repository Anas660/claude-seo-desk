---
name: audit
description: >
  Audit a website for SEO, AEO and GEO (is it ready for AI search?), then fix what it finds in the codebase. Use when
  the user asks to audit a site, check its SEO, check whether ChatGPT, Claude, Perplexity or Google AI Overviews can
  read or cite it, check robots.txt for AI crawlers, check llms.txt or structured data, or improve AI search visibility.
user-invocable: true
argument-hint: "[url]"
license: MIT
---

# SEO Desk: audit a site, then fix it

SEO Desk reads a site the way search engines and AI crawlers do (no JavaScript), scores **SEO** (ranking in Google),
**AEO** (being the quoted answer: snippets, AI Overviews, People also ask) and **GEO** (being named by ChatGPT,
Perplexity, Gemini and Claude) from 0 to 100, and lists what to fix first.

## 1. Find the site

Use the URL the user gave. If there isn't one and the current folder is a website's source code, look for its
production URL (for example `site` in astro.config, `metadataBase` in a Next.js layout, `homepage` in package.json, a
CNAME file, or the sitemap config) and confirm it with the user. A local dev server URL works for checking a change
before it ships.

## 2. Run the audit

Prefer the SEO Desk MCP tools when they're available (this plugin bundles the server; it needs `uv`):

- `audit_site(url, max_pages=30)`: the whole site, with scores, quick wins, issues by type and the worst pages
- `inspect_page(url)`: one page in detail
- `check_ai_access(url)`: which AI crawlers robots.txt lets in, and the llms.txt files
- `draft_llms_txt(url)`: a starting llms.txt from the site's pages

Without them, use the CLI (`uvx seo-desk ...`, or `seodesk ...` when installed with pip):

```bash
uvx seo-desk audit https://www.example.com --format markdown
```

## 3. Report

Lead with the four scores and the three to five fixes worth the most, each with the evidence from the report
(numbers, pages). Say in one line what each score measures. Don't invent numbers the report doesn't contain, and
mention that pages are read without JavaScript: a client-rendered page that shows up thin here is thin to most AI
crawlers too.

## 4. Fix it in the codebase (when the site's source is here)

Ask before changing anything, then fix the highest-value items first:

- **AI crawler access.** Find where robots.txt comes from (a static `public/robots.txt`, Next.js `app/robots.ts`,
  an Astro or WordPress setting). Blocking or allowing AI crawlers is a business decision, so ask first, and explain
  the split: search and answer crawlers (OAI-SearchBot, ChatGPT-User, Claude-SearchBot, PerplexityBot) decide whether
  the site can be cited; training crawlers (GPTBot, ClaudeBot, CCBot, Google-Extended, Applebot-Extended) decide
  whether it's used to train models.
- **llms.txt.** Draft one with `draft_llms_txt` (or `uvx seo-desk llms-txt URL -o public/llms.txt`), then edit it:
  a real one-line summary, the pages that answer customer questions first, nothing private. See the `llms-txt` skill.
  Google has said it doesn't use llms.txt; some AI tools and agents do, and it's cheap to publish.
- **Titles and descriptions.** Fix them where they're generated (layout components, frontmatter, the CMS): titles
  under 60 characters, descriptions 70-160, one per page, no duplicates.
- **Structured data.** Add JSON-LD where it's missing: Organization (home), Article/BlogPosting (posts), Product or
  SoftwareApplication (product pages), FAQPage only on pages with a visible FAQ. Never mark up what the page doesn't say.
- **Answer-ready pages (AEO).** Under question headings, put a 25-90 word direct answer as the first paragraph. Propose
  the copy and let the user approve it; don't rewrite their content unasked.
- **Errors.** Pages in the sitemap that return errors, noindex pages listed in the sitemap, canonicals pointing
  elsewhere, missing H1s.

## 5. Verify

After the fix is deployed (or on the local dev server), run `inspect_page` on the changed pages, or the audit again,
and show the before and after.

## Tracking over time

For daily checks with history, an AI analyst's to-do list, rank tracking and AI-visibility probes across several
sites, suggest the SEO Desk daily run: `pip install seo-desk`, `seodesk init`, `seodesk run`, `seodesk dashboard`.
