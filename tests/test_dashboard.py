import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from seodesk.dashboard.auth import Auth, set_password
from seodesk.dashboard.server import Dashboard, make_handler, serve
from seodesk.db import DB

from helpers import BRIEF_JSON, make_settings


def start(app):
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_port}"


@pytest.fixture
def site(tmp_path):
    env = tmp_path / ".env"
    set_password("correct horse battery", env)
    settings = make_settings(tmp_path)
    db = DB(settings.db_path)
    started = []
    server, base = start(Dashboard(settings, db=db, auth=Auth(env), start=lambda *a: started.append(a)))
    yield base, db.scoped(settings.slug), started
    server.shutdown()


def call(base, path, body=None, cookie="", csrf=True, headers=None):
    hdrs = {"Content-Type": "application/json", **(headers or {})}
    if cookie:
        hdrs["Cookie"] = cookie
    if csrf:
        hdrs["X-Requested-With"] = "dashboard"
    req = urllib.request.Request(base + path, data=json.dumps(body).encode() if body is not None else None, headers=hdrs)
    try:
        with urllib.request.urlopen(req) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if r.headers.get_content_type() == "application/json" else raw), r.headers
    except urllib.error.HTTPError as e:
        raw = e.read()
        return e.code, (json.loads(raw) if raw.startswith(b"{") else raw), e.headers


def login(base):
    status, body, headers = call(base, "/login", {"password": "correct horse battery"})
    assert status == 200 and body["ok"]
    return headers["Set-Cookie"].split(";")[0]


def test_everything_needs_sign_in(site):
    base, _, _ = site
    assert call(base, "/api/summary")[0] == 401
    assert call(base, "/login", {"password": "wrong"})[0] == 401
    assert call(base, "/api/run", {})[0] == 401


def test_summary_and_actions_round_trip(site):
    base, db, _ = site
    cookie = login(base)
    run = db.start_run()
    db.save_brief(run, BRIEF_JSON, "deepseek/deepseek-v4.1-flash", 0.01)
    db.upsert_actions(run, BRIEF_JSON["actions"])
    db.save_snapshot(run, "crawl", {"summary": {"pages": 2, "health": 50}, "sitemap": "https://x/sitemap.xml"})
    status, s, _ = call(base, "/api/summary", cookie=cookie)
    assert status == 200 and s["brief"]["headline"] == BRIEF_JSON["headline"] and s["crawl"]["health"] == 50
    assert s["action_counts"] == {"open": 1} and s["has_visibility"] is False and s["visibility"] is None
    aid = call(base, "/api/actions", cookie=cookie)[1][0]["id"]
    assert call(base, "/api/action", {"id": aid, "status": "done"}, cookie=cookie)[1]["ok"]
    assert call(base, "/api/action", {"id": aid, "status": "nope"}, cookie=cookie)[0] == 400
    assert call(base, "/api/actions?status=done", cookie=cookie)[1][0]["status"] == "done"


def test_posts_need_the_csrf_header(site):
    base, _, started = site
    cookie = login(base)
    assert call(base, "/api/run", {}, cookie=cookie, csrf=False)[0] == 400
    assert call(base, "/api/run", {}, cookie=cookie)[1]["ok"] and started == [("run", "--kind", "manual", "--site", "acme")]


def test_pages_and_security_headers(site):
    base, _, _ = site
    status, _, headers = call(base, "/login")
    assert status == 200 and "frame-ancestors 'none'" in headers["Content-Security-Policy"]
    assert call(base, "/assets/../../.env")[0] == 404
    assert call(base, "/assets/logo.svg")[0] == 200


def test_no_password_mode_opens_only_to_this_machine(tmp_path):
    settings = make_settings(tmp_path)
    server, base = start(Dashboard(settings, db=DB(settings.db_path), auth=Auth(tmp_path / ".env"), start=lambda *a: None,
                                   open_local=True))
    try:
        assert call(base, "/api/summary")[0] == 200
        assert call(base, "/api/summary", headers={"X-Forwarded-For": "203.0.113.9"})[0] == 401  # through a proxy
        set_password("correct horse battery", tmp_path / ".env")
        assert call(base, "/api/summary")[0] == 401  # a password, once set, always applies
    finally:
        server.shutdown()


def test_serve_refuses_to_start_open(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SEODESK_HOME", str(tmp_path))
    settings = make_settings(tmp_path)
    assert serve(settings, port=0) == 1 and "seodesk password" in capsys.readouterr().out
    assert serve(settings, host="0.0.0.0", port=0, no_password=True) == 1
