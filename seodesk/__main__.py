"""SEO Desk: SEO, AEO and GEO checks for your sites, an AI analyst that writes the to-do list, and a dashboard.

    seodesk audit URL [--format text|markdown|json]   one site, once, no config or keys needed
    seodesk llms-txt URL [-o llms.txt]               draft an llms.txt from the site's pages
    seodesk mcp                                       run as an MCP server (stdio) for Claude, Cursor and others
    seodesk demo [DIR]                                try the dashboard on sample data (no setup)
    seodesk init                                      write config.yaml, sites/example.yaml and .env.example here
    seodesk run [--site SLUG ...] [--no-brief]        the daily run: every site in config.yaml, one after another
    seodesk check [--site SLUG]                       keys, DataForSEO balance, each site's sitemap
    seodesk stats [--site SLUG]                       what's in the database
    seodesk rescore [--site SLUG]                     recompute every run's SEO / AEO / GEO scores
    seodesk dashboard [--port 5380] [--no-password]   the dashboard, on http://127.0.0.1:5380
    seodesk password [--stdin]                        set the dashboard password

Everything but `audit` works in the SEO Desk home folder: --home DIR, else $SEODESK_HOME, else the current folder.
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import logging
import os
import sys
from contextlib import contextmanager
from importlib import resources
from pathlib import Path
from typing import Iterator, List, Optional

from . import __version__, audit as auditor, crawl as crawler, dataforseo, llm, llmstxt, pipeline, progress
from .db import DB
from .errors import FatalError, SearchError
from .runlock import AlreadyRunning, run_lock, run_log
from .settings import Settings, home, load, load_all, origin

log = logging.getLogger("seodesk")
TEMPLATES = {"config.yaml": "config.yaml", "sites/example.yaml": "example-site.yaml", ".env.example": "env.example"}


@contextmanager
def log_to_file() -> Iterator[None]:
    """The dashboard shows the latest run's progress from this file."""
    path = run_log()
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(path, mode="w", encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S"))
    logging.getLogger().addHandler(handler)
    try:
        yield
    finally:
        logging.getLogger().removeHandler(handler)
        handler.close()


def chosen(args: argparse.Namespace) -> List[Settings]:
    """The sites named with --site, or every site."""
    return [load(slug) for slug in args.site] if getattr(args, "site", None) else load_all()


def cmd_init(args: argparse.Namespace) -> int:
    root = home()
    for target, name in TEMPLATES.items():
        path = root / target
        if path.exists() and not args.force:
            print(f"kept      {path} (already there; --force overwrites)")
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text((resources.files("seodesk") / "templates" / name).read_text(encoding="utf-8"), encoding="utf-8")
        print(f"wrote     {path}")
    print("\nNext: put your site in sites/example.yaml (or copy it to sites/<name>.yaml and list it in config.yaml),\n"
          "copy .env.example to .env and add the keys you have, then `seodesk check` and `seodesk run`.")
    return 0


def cmd_audit(args: argparse.Namespace) -> int:
    report = asyncio.run(auditor.audit(args.url, max_pages=args.max_pages, concurrency=args.concurrency,
                                       keywords=args.keyword, country=args.country.lower(), language=args.language.lower(),
                                       brief=args.brief, model=args.model, base_url=args.base_url, max_usd=args.max_usd,
                                       respect_robots=not args.ignore_robots))
    text = auditor.FORMATS[args.format](report)
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
    else:
        try:  # the Markdown report has emoji; a Windows console's default encoding can't print them
            sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass
        sys.stdout.write(text)
    if args.json:
        Path(args.json).write_text(auditor.to_json(report), encoding="utf-8")
    why = auditor.failed(report, args.fail_under)
    if why:
        print(f"FAILED: {why}", file=sys.stderr)
        return 1
    return 0


def cmd_llms_txt(args: argparse.Namespace) -> int:
    from .mcp_server import draft_llms_txt

    async def go() -> str:
        async with crawler.make_client(20.0) as http:
            if args.ignore_robots:
                site = await crawler.resolve_origin(http, origin(args.url))
                result = await crawler.crawl(site, args.max_pages, client=http, respect_robots=False)
                return llmstxt.draft(site, result["pages"], args.name)
            return await draft_llms_txt(args.url, args.max_pages, args.name, http=http)
    text = asyncio.run(go())
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
        print(f"wrote {args.output}: edit it, then publish it at /llms.txt", file=sys.stderr)
    else:
        sys.stdout.write(text)
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    from .dashboard.server import serve
    from .demo import seed

    root = Path(args.dir).expanduser().resolve()
    if not (root / "data" / "seo.db").exists():
        seed(root)
        print(f"Wrote eight weeks of sample data for two fictional sites to {root}")
    os.environ["SEODESK_HOME"] = str(root)
    print("Sample data only: press Ctrl+C to stop. For your own sites: seodesk init")
    return serve(load_all(root / "config.yaml"), host="127.0.0.1", port=args.port, no_password=True)


def cmd_mcp(args: argparse.Namespace) -> int:
    from .mcp_server import serve as serve_mcp

    return serve_mcp()


def cmd_run(args: argparse.Namespace) -> int:
    sites = chosen(args)
    try:
        with run_lock(wait_seconds=args.wait), log_to_file():
            failed = 0
            for settings in sites:
                db = DB(settings.db_path, settings.slug)
                log.info("── %s (%s) ──", settings.label, settings.site)
                res = asyncio.run(pipeline.run(settings, db, kind=args.kind, with_brief=not args.no_brief))
                print(f"{settings.label} · run {res.run_id}: {res.status}. {res.pages} pages, {res.serper_calls} searches, "
                      f"{res.new_actions} new actions, ${res.llm_usd:.3f} on the model.")
                for n in res.notes:
                    print(f"  ! {n}")
                failed += res.status == "failed"
                db.close()
            db = DB(sites[0].db_path)
            print(f"Spent this month, every site: ${db.month_spend(all_sites=True):.2f} of ${sites[0].max_total_monthly_usd:.2f}")
            return 1 if failed else 0
    except AlreadyRunning:
        print("A run is already going.")
        return 1


async def _site_check(settings: Settings) -> str:
    async with crawler.make_client(10.0, str(settings.crawl.get("user_agent") or "")) as http:
        moved = await crawler.resolve_origin(http, settings.site)
        sitemap, urls, errors = await crawler.sitemap_pages(http, settings.site, int(settings.crawl.get("max_pages", 150)),
                                                            [str(x) for x in settings.crawl.get("sitemaps") or []] or None)
    warn = f" WARNING: the homepage redirects to {moved}; use that as `site`." if moved != settings.site else ""
    if not urls:
        return f"NO PAGES ({'; '.join(errors)}){warn}"
    return f"{len(urls)} pages from {sitemap or 'the homepage links'}" + (f" ({'; '.join(errors)})" if errors else "") + warn


async def _dataforseo_balance(settings: Settings) -> float:
    async with crawler.make_client(30.0) as http:
        return await dataforseo.DataForSEO(settings.dataforseo_login, settings.dataforseo_password, http).balance()


def cmd_check(args: argparse.Namespace) -> int:
    sites = chosen(args)
    first = sites[0]
    ok = True
    print(f"Model API key: {'set' if llm.available() else 'not set (optional: the brief and AI visibility need it)'} "
          f"({first.model} at {first.llm_base_url})")
    print(f"Serper key: {'set' if first.serper_key else 'not set (optional: Google rank checks need it)'}")
    if first.dataforseo_login and first.dataforseo_password:
        try:
            print(f"DataForSEO: logged in, balance ${asyncio.run(_dataforseo_balance(first)):.2f}")
        except (FatalError, SearchError) as e:
            print(f"DataForSEO: {e}")
            ok = False
    else:
        print("DataForSEO: not set up (optional: DATAFORSEO_LOGIN / DATAFORSEO_PASSWORD in .env)")
    for settings in sites:
        line = asyncio.run(_site_check(settings))
        ok &= not line.startswith("NO PAGES")
        extra = f", {len(settings.visibility.prompts)} AI prompts" if settings.visibility else ""
        print(f"{settings.label} ({settings.site}): {line}; {len(settings.keywords)} keywords{extra}")
    return 0 if ok else 1


def cmd_stats(args: argparse.Namespace) -> int:
    sites = chosen(args)
    for settings in sites:
        db = DB(settings.db_path, settings.slug)
        runs = db.runs(3)
        print(f"{settings.label}: {len(db.runs(10000))} runs, actions {db.action_counts()}, "
              f"${db.month_spend():.2f} of ${settings.max_monthly_usd:.2f} this month")
        for r in runs:
            print(f"  #{r['id']} {r['started_at']} {r['status']}: {r['note'][:140]}")
        p = progress.progress(db)
        if p["latest"]:
            t = p["latest"]
            print(f"  Scores {t['date']}: SEO {t['seo']} · AEO {t['aeo']} · GEO {t['geo']}. Week: {p['week']['verdict']} "
                  f"Month: {p['month']['verdict']}")
    db = DB(sites[0].db_path)
    print(f"Every site this month: ${db.month_spend(all_sites=True):.2f} of ${sites[0].max_total_monthly_usd:.2f}")
    return 0


def cmd_rescore(args: argparse.Namespace) -> int:
    for settings in chosen(args):
        db = DB(settings.db_path, settings.slug)
        print(f"{settings.label}: {progress.rescore(db, settings)} runs scored")
    return 0


def cmd_dashboard(args: argparse.Namespace) -> int:
    from .dashboard.server import serve

    return serve(load_all(), host=args.host, port=args.port, no_password=args.no_password)


def cmd_password(args: argparse.Namespace) -> int:
    from .dashboard.auth import set_password

    if args.stdin:
        password = sys.stdin.readline().rstrip("\n")
    else:
        password = getpass.getpass("New dashboard password (10+ characters): ")
        if getpass.getpass("Again: ") != password:
            print("The two didn't match.")
            return 1
    try:
        set_password(password)
    except ValueError as e:
        print(f"Not set: {e}")
        return 1
    print("Password set. Everyone signed in before is signed out.")
    return 0


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="seodesk", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version=f"seodesk {__version__}")
    p.add_argument("--home", help="the SEO Desk folder (config.yaml, sites/, .env, data/); default $SEODESK_HOME or here")
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("audit", help="audit one site once (no config or keys needed)")
    a.add_argument("url", help="the site, e.g. https://www.example.com or example.com")
    a.add_argument("--format", choices=sorted(auditor.FORMATS), default="text")
    a.add_argument("--output", "-o", help="write the report here instead of printing it")
    a.add_argument("--json", help="also write the full report as JSON to this file")
    a.add_argument("--max-pages", type=int, default=50, help="sitemap pages to crawl (default 50)")
    a.add_argument("--concurrency", type=int, default=4, help="pages fetched at once (default 4)")
    a.add_argument("--fail-under", type=float, default=0, help="exit with 1 when the overall score is lower (for CI)")
    a.add_argument("--ignore-robots", action="store_true", help="crawl pages robots.txt disallows (only on your own site)")
    a.add_argument("--keyword", action="append", default=[], help="check this keyword's Google rank (needs SERPER_API_KEY); repeat")
    a.add_argument("--country", default="us", help="2-letter country for rank checks (default us)")
    a.add_argument("--language", default="en", help="language for rank checks (default en)")
    a.add_argument("--brief", action="store_true", help="have the AI analyst write actions (needs LLM_API_KEY)")
    a.add_argument("--model", default="", help="model for --brief (default deepseek/deepseek-v4.1-flash on OpenRouter)")
    a.add_argument("--base-url", default=os.environ.get("LLM_BASE_URL", ""), help="OpenAI-compatible API for --brief")
    a.add_argument("--max-usd", type=float, default=0.25, help="most --brief may spend (default $0.25)")
    a.set_defaults(fn=cmd_audit)

    t = sub.add_parser("llms-txt", help="draft an llms.txt from the site's pages")
    t.add_argument("url")
    t.add_argument("--output", "-o", help="write it here instead of printing it")
    t.add_argument("--max-pages", type=int, default=100)
    t.add_argument("--name", default="", help="the site's name for the title (default: from the homepage)")
    t.add_argument("--ignore-robots", action="store_true", help="read pages robots.txt disallows (only on your own site)")
    t.set_defaults(fn=cmd_llms_txt)

    dm = sub.add_parser("demo", help="try the dashboard on sample data, no setup")
    dm.add_argument("dir", nargs="?", default="seodesk-demo", help="where to put the sample data (default ./seodesk-demo)")
    dm.add_argument("--port", type=int, default=5380)
    dm.set_defaults(fn=cmd_demo)

    m = sub.add_parser("mcp", help="run as an MCP server over stdio (needs: pip install 'seo-desk[mcp]')")
    m.set_defaults(fn=cmd_mcp)

    i = sub.add_parser("init", help="write a starting config.yaml, sites/example.yaml and .env.example")
    i.add_argument("--force", action="store_true", help="overwrite files that are already there")
    i.set_defaults(fn=cmd_init)

    r = sub.add_parser("run", help="the daily check of every site in config.yaml")
    r.add_argument("--site", action="append", help="only this site (config.yaml name); repeat for more")
    r.add_argument("--no-brief", action="store_true", help="skip the AI brief (no model cost)")
    r.add_argument("--kind", default="daily", help="label on the run: daily | manual")
    r.add_argument("--wait", type=float, default=0, help="seconds to wait if another run holds the lock")
    r.set_defaults(fn=cmd_run)
    for name, fn, text in (("check", cmd_check, "keys, DataForSEO balance and each site's sitemap"),
                           ("stats", cmd_stats, "what's in the database"),
                           ("rescore", cmd_rescore, "recompute every run's scores")):
        c = sub.add_parser(name, help=text)
        c.add_argument("--site", action="append", help="only this site; repeat for more")
        c.set_defaults(fn=fn)
    d = sub.add_parser("dashboard", help="serve the dashboard")
    d.add_argument("--host", default="127.0.0.1")
    d.add_argument("--port", type=int, default=5380)
    d.add_argument("--no-password", action="store_true", help="no sign-in, for this machine only (127.0.0.1)")
    d.set_defaults(fn=cmd_dashboard)
    pw = sub.add_parser("password", help="set the dashboard password")
    pw.add_argument("--stdin", action="store_true")
    pw.set_defaults(fn=cmd_password)
    return p


def main(argv: Optional[List[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        datefmt="%H:%M:%S", stream=sys.stderr)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    args = parser().parse_args(argv)
    if args.home:
        os.environ["SEODESK_HOME"] = str(Path(args.home).expanduser().resolve())
    if args.cmd in ("audit", "llms-txt", "mcp"):  # their output is the point; progress lines would only get in its way
        logging.getLogger().setLevel(logging.WARNING)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
