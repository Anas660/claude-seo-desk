"""`seodesk demo`: eight weeks of made-up history for two fictional sites, so you can try the dashboard without
setting anything up (it's also where the README's screenshots come from).

    seodesk demo                 # writes ./seodesk-demo and opens the dashboard on http://127.0.0.1:5380

Everything here is sample data: the sites use the reserved .example domain and nothing is fetched."""
from __future__ import annotations

import random
from datetime import date, timedelta
from pathlib import Path

from . import probes
from .db import DB

DAYS = 56
CONFIG = """sites: [acme, northwind]
llm: {model: deepseek/deepseek-v4.1-flash}
limits: {max_run_usd: 1, max_monthly_usd: 30, site_monthly_usd: 4, max_serper_per_run: 60}
"""
ACME = """name: Acme Invoicing
site: https://www.acme-invoicing.example
brand_terms: [acme, acme invoicing]
claims:
  - {id: soc2, pattern: 'SOC 2', about_us: true, unless: '\\bno\\b|\\bnot\\b', why: there is no SOC 2 report}
keywords:
  - {keyword: free invoice template, tier: easy, target: /templates/invoice}
  - {keyword: late payment email template, tier: easy, target: /blog/late-payment-email}
  - {keyword: how to invoice as a freelancer, tier: easy, target: /blog/how-to-invoice-as-a-freelancer}
  - {keyword: wave alternative, tier: easy, target: /compare/acme-vs-wave}
  - {keyword: freelance invoicing software, tier: medium, target: /}
  - {keyword: invoice app for freelancers, tier: medium, target: /}
  - {keyword: invoicing software, tier: head, target: /}
ai_visibility:
  models: [perplexity/sonar, openai/gpt-5-mini:online]
  prompts:
    - best invoicing software for freelancers
    - how do I chase a late invoice politely
    - {prompt: "What is Acme Invoicing?", branded: true}
"""
NORTHWIND = """name: Northwind Docs
site: https://docs.northwind.example
keywords:
  - {keyword: northwind api rate limits, tier: easy, target: /api/limits}
"""
PAGES = [
    ("/", "Invoicing Software for Freelancers | Acme", []),
    ("/pricing", "Pricing | Acme", []),
    ("/features", "Features: invoices, reminders, payments and reports for freelancers | Acme", [("warn", "title-long", "title is 74 characters (Google shows about 60)")]),
    ("/templates/invoice", "Free Invoice Template (Word, PDF, Google Docs) | Acme", []),
    ("/templates/receipt", "Free Receipt Template | Acme", [("warn", "thin", "only 212 words of text")]),
    ("/blog/how-to-invoice-as-a-freelancer", "How to Invoice as a Freelancer | Acme", []),
    ("/blog/late-payment-email", "Late Payment Email Templates That Get You Paid | Acme", []),
    ("/blog/net-30-explained", "Net 30 Explained | Acme", [("warn", "desc-missing", "no meta description")]),
    ("/compare/acme-vs-wave", "Acme vs Wave | Acme", []),
    ("/docs/getting-started", "Getting Started | Acme", [("warn", "img-alt", "3 image(s) without an alt attribute")]),
    ("/about", "About | Acme", []),
    ("/old-pricing", "", [("error", "status", "returns HTTP 404")]),
]


def curve(start: float, end: float, i: int, noise: float = 1.5) -> int:
    return round(start + (end - start) * i / (DAYS - 1) + random.uniform(-noise, noise))


def seed(home: Path) -> None:
    random.seed(7)
    (home / "sites").mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(CONFIG)
    (home / "sites" / "acme.yaml").write_text(ACME)
    (home / "sites" / "northwind.yaml").write_text(NORTHWIND)
    db_path = home / "data" / "seo.db"
    if db_path.exists():
        raise SystemExit(f"{db_path} already exists; pick an empty folder")
    db = DB(db_path)
    first = date.today() - timedelta(days=DAYS - 1)
    for slug, site, scale in (("acme", "https://www.acme-invoicing.example", 1.0), ("northwind", "https://docs.northwind.example", 0.9)):
        s = db.scoped(slug)
        for i in range(DAYS):
            day = (first + timedelta(days=i)).isoformat()
            run = s.start_run()
            pages = [{"url": site + path, "status": 404 if issues and issues[0][1] == "status" else 200, "title": title,
                      "words": 900, "schema_types": ["Organization"] if path == "/" else ["Article"],
                      "aeo": {"question_headings": 2, "answer_first": int(i > 20), "faq_schema": path == "/pricing"},
                      "issues": [{"level": lv, "code": c, "text": t} for lv, c, t in issues]}
                     for path, title, issues in PAGES]
            s.save_pages(run, pages)
            s.save_snapshot(run, "crawl", {"summary": {"pages": len(pages), "ok": len(pages) - 1, "errors": 1, "warnings": 5,
                                                       "health": 92, "median_ms": 240,
                                                       "issues_by_code": {"title-long": 1, "thin": 1, "desc-missing": 1, "img-alt": 1, "status": 1},
                                                       "aeo": {"with_questions": 58, "answer_first": 33 if i > 20 else 8, "faq_schema": 8, "with_tables": 25}},
                                           "sitemap": f"{site}/sitemap.xml", "sitemap_count": len(pages), "errors": []})
            s.save_snapshot(run, "geo", {"summary": {"crawlers_blocked": ["CCBot"] if i < 30 else [], "llms_missing": [] if i > 14 else ["/llms.txt"],
                                                     "claim_hits": 0, "claim_pages": 0},
                                         "crawlers": [{"crawler": c, "home": not (c == "CCBot" and i < 30), "blog": True}
                                                      for c in ("GPTBot", "OAI-SearchBot", "ChatGPT-User", "ClaudeBot", "Claude-SearchBot",
                                                                "PerplexityBot", "Google-Extended", "Applebot-Extended", "CCBot")],
                                         "llms": [{"path": "/llms.txt", "status": 200 if i > 14 else 404, "bytes": 4200},
                                                  {"path": "/llms-full.txt", "status": 200 if i > 35 else 404, "bytes": 51000}],
                                         "claims": [], "errors": []})
            positions = [curve(14, 3, i, 1), curve(19, 6, i, 1), curve(25, 8, i, 2), curve(30, 11, i, 2),
                         curve(28, 15, i, 2), curve(40, 18, i, 2), None]
            rows = [{"keyword": k, "country": "us", "tier": t, "position": (p if p and p <= 20 else None), "url": f"{site}{target}" if p and p <= 20 else "",
                     "top": ["wave.example", "freshbooks.example", "reddit.com", "zoho.example"], "questions": ["Is there a free invoice template?", "How do I invoice as a freelancer?"]}
                    for (k, t, target), p in zip([("free invoice template", "easy", "/templates/invoice"), ("late payment email template", "easy", "/blog/late-payment-email"),
                                                  ("how to invoice as a freelancer", "easy", "/blog/how-to-invoice-as-a-freelancer"), ("wave alternative", "easy", "/compare/acme-vs-wave"),
                                                  ("freelance invoicing software", "medium", "/"), ("invoice app for freelancers", "medium", "/"),
                                                  ("invoicing software", "head", "/")], positions)]
            if slug == "acme":
                s.save_serp(run, rows)
            seo, aeo, geo = curve(52 * scale, 71 * scale, i), curve(30 * scale, 54 * scale, i), curve(58 * scale, 86 * scale, i)
            top10 = sum(1 for r in rows if r["position"] and r["position"] <= 10)
            s.save_scores(run, day, {"seo": seo, "aeo": aeo, "geo": geo, "overall": round((seo + aeo + geo) / 3), "parts": {
                "seo": {"visibility": curve(20, 48, i), "technical": 92, "on_page": 67}, "aeo": {"questions": 58, "answers": 33, "faq": 8, "structure": 50},
                "geo": {"crawlers": 100 if i >= 30 else 89, "llms": (60 if i > 14 else 0) + (40 if i > 35 else 0), "schema": 92, "claims": 100, "mentions": curve(25, 62, i, 4)},
                "metrics": {"top10": top10 if slug == "acme" else None, "keywords": 7, "avg_position": round(sum(r["position"] for r in rows if r["position"]) / max(1, sum(1 for r in rows if r["position"])), 1),
                            "errors": 1, "pages": len(pages), "ai_mentions": round(curve(1, 3, i, 0.4))}}})
            s.finish_run(run, "ok" if i % 9 else "partial", f"SEO {seo} · AEO {aeo} · GEO {geo}; {len(pages)} pages", pages=len(pages),
                         serper_calls=11, llm_usd=0.012)
            stamp = f"{day}T07:0{i % 6}:00Z"
            s.conn.execute("UPDATE runs SET started_at = ?, finished_at = ? WHERE id = ?", (stamp, stamp.replace(":0", ":1", 1), run))
            s.conn.execute("UPDATE snapshots SET created_at = ? WHERE run_id = ?", (stamp, run))
            s.conn.execute("UPDATE serp SET date = ? WHERE run_id = ?", (day, run))
            s.add_spend("brief", 0.011)
            s.add_spend("serper", 0.011)
        s.conn.commit()
    acme = db.scoped("acme")
    last = acme.runs(1)[0]["id"]
    results = [
        {"model": "perplexity/sonar", "prompt": "best invoicing software for freelancers", "branded": False, "mentioned": True, "cited": True,
         "citations": ["acme-invoicing.example", "wave.example", "reddit.com"], "flags": [],
         "excerpt": "For freelancers, popular picks are Wave (free), Acme Invoicing (simple invoices with automatic late-payment reminders) and FreshBooks for time tracking..."},
        {"model": "openai/gpt-5-mini:online", "prompt": "best invoicing software for freelancers", "branded": False, "mentioned": False, "cited": False,
         "citations": ["freshbooks.example", "wave.example", "nerdwallet.example"], "flags": [],
         "excerpt": "The most recommended options are FreshBooks, Wave and Zoho Invoice. FreshBooks suits service businesses that bill by the hour..."},
        {"model": "perplexity/sonar", "prompt": "how do I chase a late invoice politely", "branded": False, "mentioned": True, "cited": True,
         "citations": ["acme-invoicing.example", "indeed.example"], "flags": [],
         "excerpt": "Send a short, friendly reminder a few days after the due date. Acme Invoicing's late payment templates suggest three steps: a nudge, a firm follow-up, then a final notice..."},
        {"model": "openai/gpt-5-mini:online", "prompt": "how do I chase a late invoice politely", "branded": False, "mentioned": False, "cited": False,
         "citations": ["indeed.example", "reddit.com"], "flags": [], "excerpt": "Start with a polite email that restates the invoice number, amount and due date..."},
        {"model": "perplexity/sonar", "prompt": "What is Acme Invoicing?", "branded": True, "mentioned": True, "cited": True,
         "citations": ["acme-invoicing.example"], "flags": [], "excerpt": "Acme Invoicing is invoicing software for freelancers: invoices, reminders and online payments..."},
        {"model": "openai/gpt-5-mini:online", "prompt": "What is Acme Invoicing?", "branded": True, "mentioned": True, "cited": False,
         "citations": ["g2.example"], "flags": ["soc2"], "excerpt": "Acme Invoicing is a cloud invoicing tool for small businesses. It is SOC 2 certified and integrates with..."},
    ]
    acme.save_snapshot(last, "probes", {"results": results, "summary": probes.summarise(results, "acme-invoicing.example"), "plan": [], "errors": []})
    brief = {
        "headline": "Free invoice template reaches #3; ChatGPT still doesn't name Acme",
        "summary": "SEO is up 10 points this month, to 71, as the template and late-payment pages climbed into the top 10. GEO is at 85 "
                   "now that llms-full.txt is live and CCBot is allowed. AEO is the weakest score at 53: only a third of content pages "
                   "answer a question in their first paragraph. Perplexity names Acme in 2 of 2 unbranded answers; ChatGPT in none.",
        "wins": ["'free invoice template' up from #6 to #3", "llms-full.txt now live (51 KB)", "CCBot no longer blocked in robots.txt"],
        "risks": ["/old-pricing in the sitemap returns 404", "ChatGPT's answer about Acme says 'SOC 2 certified', which isn't true"],
        "actions": [
            {"area": "GEO", "priority": 1, "title": "Publish a comparison page ChatGPT can cite for 'best invoicing software for freelancers'",
             "why": "ChatGPT cites freshbooks.example and nerdwallet.example for this prompt and doesn't name Acme; Perplexity cites /compare/acme-vs-wave.",
             "how": "Add /compare/best-invoicing-software-for-freelancers with a 40-word answer first, a comparison table and FAQPage markup.",
             "url": "/compare/best-invoicing-software-for-freelancers", "keyword": "best invoicing software for freelancers", "effort": "M",
             "evidence": "AI visibility: 0/2 ChatGPT answers name Acme"},
            {"area": "Technical", "priority": 1, "title": "Remove /old-pricing from the sitemap", "why": "It returns 404 and is listed in sitemap.xml.",
             "how": "Redirect /old-pricing to /pricing with a 301 and drop it from the sitemap.", "url": "/old-pricing", "keyword": "", "effort": "S",
             "evidence": "crawl: HTTP 404"},
            {"area": "GEO", "priority": 1, "title": "Correct the SOC 2 claim AI answers repeat about Acme",
             "why": "ChatGPT says Acme is SOC 2 certified; there is no SOC 2 report.",
             "how": "State security practices plainly on /security and in llms.txt: 'Acme has no SOC 2 report yet.'", "url": "/security", "keyword": "", "effort": "S",
             "evidence": "branded probe flagged soc2"},
            {"area": "AEO", "priority": 2, "title": "Answer 'What is net 30?' in the first paragraph of /blog/net-30-explained",
             "why": "The question heading has no direct answer under it, and the page has no meta description.",
             "how": "Open with: 'Net 30 means the client pays within 30 days of the invoice date...' (35 words), and add a 150-character description.",
             "url": "/blog/net-30-explained", "keyword": "net 30", "effort": "S", "evidence": "crawl: desc-missing; answer_first 0"},
            {"area": "SEO", "priority": 2, "title": "Shorten the /features title to under 60 characters", "why": "Google cuts the 74-character title.",
             "how": "Features: Invoices, Reminders and Payments | Acme", "url": "/features", "keyword": "", "effort": "S", "evidence": "crawl: title-long"},
        ],
        "content_ideas": [{"keyword": "invoice payment terms", "page_type": "guide", "angle": "Net 15, 30, 60 with sample wording for each",
                           "questions": ["What are standard invoice payment terms?", "Is net 30 normal for freelancers?"]}],
    }
    acme.save_brief(last, brief, "deepseek/deepseek-v4.1-flash", 0.011)
    acme.upsert_actions(last, brief["actions"])
    first_action = acme.actions("open")[-1]["id"]
    acme.set_action(first_action, "done")
    db.close()
