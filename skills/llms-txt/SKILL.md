---
name: llms-txt
description: >
  Create or improve a website's llms.txt (and llms-full.txt) from its real pages, and add it to the codebase. Use when
  the user asks for an llms.txt, wants AI assistants to understand their site, or an SEO Desk audit flagged llms.txt
  as missing.
user-invocable: true
argument-hint: "[url]"
license: MIT
---

# SEO Desk: write the site's llms.txt

`llms.txt` (https://llmstxt.org) is a Markdown file at the site's root that tells AI assistants what the site is and
which pages matter: an H1 with the name, a one-line summary in a blockquote, then sections of links with a short note
each. Google has said it doesn't use it; some AI tools and agents read it. It's cheap to publish and keep current.

## Steps

1. **Draft from the live site.** Use the `draft_llms_txt(url)` MCP tool, or
   `uvx seo-desk llms-txt https://www.example.com -o llms.txt`. It lists every sitemap page (noindex pages left out)
   with its title and meta description, grouped by section.
2. **Edit it with the user.** Write the summary in plain words (what the site is, who it's for). Put the pages that
   answer real customer questions first: pricing, docs, how it works, comparisons. Cut thin pages, legal pages, tag
   archives and anything private. Keep notes short and factual; no marketing claims the pages don't back up.
3. **Put it where the site serves static files** (for example `public/llms.txt` in Next.js, Astro or Vite), or
   generate it at build time if the site already generates its sitemap, so it stays current.
4. **Optionally add `llms-full.txt`**: the full text of the key pages in one Markdown file, for assistants that read
   everything at once.
5. **Check it once it's live** with `check_ai_access(url)`: both files should answer 200.
