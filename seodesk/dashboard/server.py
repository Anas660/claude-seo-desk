"""The SEO Desk dashboard: every site's scores side by side, and for each site its progress week to week and month to
month, today's brief and actions, keyword ranks, AI visibility, site health, app-store listings, runs and spend, and
"Run now", behind one password. Every API call takes ?site=<slug> (the first site in config.yaml when it's missing).
It listens on 127.0.0.1; to reach it from elsewhere, put an HTTPS reverse proxy (Caddy, nginx) in front of it."""
from __future__ import annotations

import json
import mimetypes
import subprocess
import sys
import threading
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Union
from urllib.parse import parse_qs, urlparse

from .. import dataforseo, progress, stores as store_checks
from ..db import DB
from ..runlock import is_running, run_log
from ..settings import Settings, home
from .auth import COOKIE, SESSION_DAYS, Auth

STATIC = Path(__file__).parent / "static"
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": ("default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
                                "img-src 'self' data:; font-src 'self'; connect-src 'self'; frame-ancestors 'none'"),
}
LOOPBACK = ("127.0.0.1", "::1", "localhost")
PROXY_HEADERS = ("X-Forwarded-For", "X-Real-IP", "Forwarded")


def spawn(*args: str) -> subprocess.Popen:
    """Starts `seodesk <args>` in its own process, in the same home folder (it takes the run lock and writes the
    run log)."""
    return subprocess.Popen([sys.executable, "-m", "seodesk", "--home", str(home()), *args], cwd=str(home()),
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            start_new_session=True)


def tail(path: Path, lines: int = 80) -> str:
    try:
        return "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])
    except OSError:
        return ""


class Dashboard:
    def __init__(self, settings: Union[Settings, List[Settings]], db: Optional[DB] = None, auth: Optional[Auth] = None,
                 start=spawn, open_local: bool = False):
        sites = settings if isinstance(settings, list) else [settings]
        # --no-password: requests from this machine need no sign-in (never ones through a reverse proxy)
        self.open_local = open_local
        self.sites: Dict[str, Settings] = {s.slug: s for s in sites}
        self.default = sites[0].slug
        self.db = db or DB(sites[0].db_path)
        self.lock = threading.Lock()  # one SQLite connection shared by the request threads
        self.auth = auth or Auth()
        self.start = start
        self.children: List[Any] = []
        for s in sites:  # runs from before scoring existed: score them now, so Progress has history at once
            try:
                progress.backfill(self.db.scoped(s.slug), s)
            except Exception:  # an odd old run must not keep the dashboard down
                pass

    def site(self, slug: Optional[str]) -> Settings:
        return self.sites.get(slug or "", self.sites[self.default])

    def busy(self) -> bool:
        self.children = [p for p in self.children if hasattr(p, "poll") and p.poll() is None]
        return bool(self.children) or is_running()

    def site_list(self) -> List[Dict[str, Any]]:
        return [{"slug": s.slug, "name": s.label, "site": s.site} for s in self.sites.values()]

    def summary(self, slug: Optional[str] = None) -> Dict[str, Any]:
        s = self.site(slug)
        with self.lock:
            db = self.db.scoped(s.slug)
            crawl = db.latest_snapshot("crawl") or {}
            geo = db.latest_snapshot("geo") or {}
            stores = db.latest_snapshot("stores") or {}
            volumes = db.latest_snapshot("volumes")
            ranked = db.latest_snapshot("ranked")
            probes = db.latest_snapshot("probes") if s.visibility else None
            brief = db.latest_brief()
            ranks = db.serp_latest()
            for r in ranks:
                r["search_volume"], r["volume_trend"] = dataforseo.volume_for(volumes, r["keyword"], r["country"])
            out = {
                "slug": s.slug,
                "name": s.label,
                "site": s.site,
                "sites": self.site_list(),
                "has_stores": bool(s.stores),
                "has_claims": bool(s.claims),
                "has_visibility": s.visibility is not None,
                "visibility": probes,
                "crawl": crawl.get("summary") or {},
                "crawl_at": crawl.get("_at"),
                "sitemap": crawl.get("sitemap", ""),
                "geo": {k: geo.get(k) for k in ("summary", "crawlers", "llms", "claims")} if geo else {},
                "stores": stores,
                "stores_trend": store_checks.trend(db.snapshot_history("stores", 30)),
                "brief": brief,
                "briefs": db.briefs(14),
                "ranks": ranks,
                "ranked": ranked,
                "volumes_at": (volumes or {}).get("_at"),
                "dataforseo": bool(s.dataforseo_login and s.dataforseo_password),
                "progress": progress.progress(db, days=70),
                "action_counts": db.action_counts(),
                "runs": db.runs(15),
                "month_spend": round(db.month_spend(), 2),
                "monthly_cap": s.max_monthly_usd,
                "total_spend": round(db.month_spend(all_sites=True), 2),
                "total_cap": s.max_total_monthly_usd,
                "model": s.model,
            }
        out["running"] = self.busy()
        return out

    def overview(self) -> Dict[str, Any]:
        with self.lock:
            rows = [progress.overview_row(self.db.scoped(s.slug), s) for s in self.sites.values()]
            total = round(self.db.month_spend(all_sites=True), 2)
        first = self.sites[self.default]
        return {"sites": rows, "total_spend": total, "total_cap": first.max_total_monthly_usd, "running": self.busy()}

    def progress(self, slug: Optional[str]) -> Dict[str, Any]:
        s = self.site(slug)
        with self.lock:
            return progress.progress(self.db.scoped(s.slug))

    def search(self, slug: Optional[str] = None) -> Dict[str, Any]:
        with self.lock:
            return {"serp_history": self.db.scoped(self.site(slug).slug).serp_history(30)}

    def pages(self, slug: Optional[str] = None) -> List[Dict[str, Any]]:
        with self.lock:
            return self.db.scoped(self.site(slug).slug).latest_pages()

    def actions(self, status: Optional[str], slug: Optional[str] = None) -> List[Dict[str, Any]]:
        with self.lock:
            return self.db.scoped(self.site(slug).slug).actions(status or None)

    def set_action(self, action_id: int, status: str, slug: Optional[str] = None) -> bool:
        with self.lock:
            return self.db.scoped(self.site(slug).slug).set_action(action_id, status)

    def brief(self, brief_id: int, slug: Optional[str] = None) -> Optional[Dict[str, Any]]:
        with self.lock:
            return self.db.scoped(self.site(slug).slug).brief(brief_id)

    def run(self, slug: Optional[str] = None) -> Dict[str, Any]:
        if self.busy():
            return {"ok": False, "error": "A run is already going."}
        args = ["run", "--kind", "manual"] + ([] if slug == "*" else ["--site", self.site(slug).slug])
        child = self.start(*args)
        if hasattr(child, "poll"):
            self.children.append(child)
        return {"ok": True}


def make_handler(app: Dashboard) -> type:
    class Handler(BaseHTTPRequestHandler):
        server_version = "seodesk"
        sys_version = ""

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - quiet access log
            pass

        def _send(self, status: int, body: bytes, content_type: str, extra: Optional[Dict[str, str]] = None) -> None:
            self.send_response(status)
            for key, value in {**SECURITY_HEADERS, "Content-Type": content_type,
                               "Content-Length": str(len(body)), **(extra or {})}.items():
                self.send_header(key, value)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, payload: Any, status: int = 200, extra: Optional[Dict[str, str]] = None) -> None:
            self._send(status, json.dumps(payload, ensure_ascii=False, default=str).encode(),
                       "application/json; charset=utf-8", {"Cache-Control": "no-store", **(extra or {})})

        def _file(self, path: Path, cache: bool = False) -> None:
            content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            if path.suffix == ".woff2":
                content_type = "font/woff2"
            self._send(200, path.read_bytes(), content_type,
                       {"Cache-Control": "public, max-age=604800" if cache else "no-cache"})

        def _redirect(self, location: str, extra: Optional[Dict[str, str]] = None) -> None:
            self._send(302, b"", "text/plain", {"Location": location, **(extra or {})})

        def _client(self) -> str:
            if self.client_address[0] in ("127.0.0.1", "::1"):  # behind the reverse proxy
                return self.headers.get("X-Real-IP") or self.client_address[0]
            return self.client_address[0]

        def _signed_in(self) -> bool:
            if app.open_local and self.client_address[0] in LOOPBACK and not any(self.headers.get(h) for h in PROXY_HEADERS) \
                    and not app.auth.has_password():
                return True
            jar = cookies.SimpleCookie(self.headers.get("Cookie", ""))
            return app.auth.session_valid(jar[COOKIE].value if COOKIE in jar else None)

        def _cookie(self, value: str, max_age: int) -> str:
            secure = "; Secure" if self.headers.get("X-Forwarded-Proto") == "https" else ""
            return f"{COOKIE}={value}; Path=/; HttpOnly; SameSite=Lax; Max-Age={max_age}{secure}"

        def _body(self, limit: int = 100_000) -> Dict[str, Any]:
            length = min(int(self.headers.get("Content-Length") or 0), limit)
            raw = self.rfile.read(length) if length else b""
            try:
                body = json.loads(raw or b"{}")
                return body if isinstance(body, dict) else {}
            except ValueError:
                return {k: v[0] for k, v in parse_qs(raw.decode(errors="ignore")).items()}

        def do_HEAD(self) -> None:
            self.do_GET()

        def do_GET(self) -> None:
            url = urlparse(self.path)
            path = url.path
            if path == "/login":
                return self._file(STATIC / "login.html")
            if path.startswith("/assets/"):
                asset = (STATIC / "assets" / path[len("/assets/"):]).resolve()
                if asset.is_file() and (STATIC / "assets").resolve() in asset.parents:
                    return self._file(asset, cache=True)
                return self._send(404, b"not found", "text/plain")
            if path == "/logout":
                return self._redirect("/login", {"Set-Cookie": self._cookie("", 0)})
            if not self._signed_in():
                if path.startswith("/api/"):
                    return self._json({"ok": False, "error": "signed out"}, 401)
                return self._redirect("/login")

            query = {k: v[0] for k, v in parse_qs(url.query).items()}
            site = query.get("site")
            if path == "/":
                return self._file(STATIC / "index.html")
            if path == "/api/summary":
                return self._json(app.summary(site))
            if path == "/api/overview":
                return self._json(app.overview())
            if path == "/api/progress":
                return self._json(app.progress(site))
            if path == "/api/search":
                return self._json(app.search(site))
            if path == "/api/pages":
                return self._json(app.pages(site))
            if path == "/api/actions":
                return self._json(app.actions(query.get("status"), site))
            if path == "/api/brief":
                bid = query.get("id", "")
                brief = app.brief(int(bid), site) if bid.isdigit() else None
                return self._json(brief) if brief else self._json({"ok": False, "error": "not found"}, 404)
            if path == "/api/log":
                return self._json({"log": tail(run_log()), "running": app.busy()})
            return self._send(404, b"not found", "text/plain")

        def do_POST(self) -> None:
            path = urlparse(self.path).path
            if path == "/login":
                ok, value, status = app.auth.login(self._client(), str(self._body().get("password", "")))
                if not ok:
                    return self._json({"ok": False, "error": value}, status)
                return self._json({"ok": True}, extra={"Set-Cookie": self._cookie(value, SESSION_DAYS * 86400)})
            if not self._signed_in():
                return self._json({"ok": False, "error": "signed out"}, 401)
            # Pages from other sites can't add this header to a cross-site request (CSRF guard).
            if self.headers.get("X-Requested-With") != "dashboard":
                return self._json({"ok": False, "error": "bad request"}, 400)
            body = self._body()
            if path == "/api/action":
                aid = body.get("id")
                if not isinstance(aid, int):
                    return self._json({"ok": False, "error": "id must be a number"}, 400)
                try:
                    ok = app.set_action(aid, str(body.get("status", "")), str(body.get("site") or "") or None)
                except ValueError as e:
                    return self._json({"ok": False, "error": str(e)}, 400)
                return self._json({"ok": ok}, 200 if ok else 404)
            if path == "/api/run":
                result = app.run(str(body.get("site") or "") or None)
                return self._json(result, 200 if result["ok"] else 409)
            return self._send(404, b"not found", "text/plain")

    return Handler


def serve(settings: Union[Settings, List[Settings]], host: str = "127.0.0.1", port: int = 5380,
          no_password: bool = False) -> int:
    auth = Auth()
    if no_password and host not in LOOPBACK:
        print("--no-password only works on 127.0.0.1 (this machine). Set a password with `seodesk password`.")
        return 1
    if not no_password and not auth.has_password():
        print("No dashboard password yet. Set one with `seodesk password`, or run `seodesk dashboard --no-password`\n"
              "to open it without one on this machine only.")
        return 1
    server = ThreadingHTTPServer((host, port), make_handler(Dashboard(settings, auth=auth, open_local=no_password)))
    server.daemon_threads = True
    print(f"Dashboard on http://{host}:{port}" + (" (no password: this machine only)" if no_password else ""), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
