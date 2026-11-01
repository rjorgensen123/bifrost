# Bifrost. © Roger Jorgensen (rogerj@gmail.com). Last modified: v2.4.2
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Integration test: every page keeps to its Content Security Policy (needs the database).

Every HTML page the app serves on GET — as admin, and the login pages without a session — is fetched,
and each must:
  - send a CSP whose style-src and script-src hold the request's nonce and no 'unsafe-inline',
  - carry that nonce on every <style> block and every inline <script>,
  - have no element with a style="" attribute (the CSP blocks them),
  - have JSON data blocks (<script type="application/json">) that parse.

  # inside the webapp container (WORKDIR /app), once the database is up:
  python tests/citest_csp_pages.py
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import db             # noqa: E402
import app as appmod  # noqa: E402

RESULTS = []
SKIP = re.compile(r"logout|static|healthz|\.png|\.svg|download|export|backup")


def check(name, passed, detail=""):
    RESULTS.append((name, bool(passed), detail))


def admin_client():
    import sessions as srv
    cl = appmod.app.test_client()
    sid = srv.create("admin", None, 3600, ua="citest", ip="127.0.0.1")
    with cl.session_transaction() as s:
        s["admin"] = True
        s["csrf"] = "t"
        s["sid"] = sid
    return cl


def page_urls(args):
    out = set()
    for rule in appmod.app.url_map.iter_rules():
        if "GET" not in rule.methods or SKIP.search(rule.rule) or SKIP.search(rule.endpoint):
            continue
        if any(a not in args for a in rule.arguments):
            continue
        url = rule.rule
        for a in rule.arguments:
            url = re.sub(r"<(?:[a-z]+:)?%s>" % a, str(args[a]), url)
        out.add(url)
    return sorted(out)


def problems(resp):
    """What is wrong with one HTML response, as a list of short texts."""
    out = []
    csp = resp.headers.get("Content-Security-Policy", "")
    parts = {p.split()[0]: p.split()[1:] for p in csp.split(";") if p.strip()}
    nonces = {t[len("'nonce-"):-1] for t in parts.get("style-src", []) if t.startswith("'nonce-")}
    for directive in ("style-src", "script-src"):
        if "'unsafe-inline'" in parts.get(directive, []):
            out.append(f"{directive} allows 'unsafe-inline'")
    if len(nonces) != 1 or nonces != {t[len("'nonce-"):-1] for t in parts.get("script-src", [])
                                     if t.startswith("'nonce-")}:
        return out + [f"no single nonce in style-src and script-src: {csp!r}"]
    nonce = nonces.pop()
    html = resp.get_data(as_text=True)
    for m in re.finditer(r"<style\b([^>]*)>", html, re.I):
        if f'nonce="{nonce}"' not in m.group(1):
            out.append(f"<style> without the nonce: {m.group(0)[:60]}")
    for m in re.finditer(r"<script\b([^>]*)>(.*?)</script>", html, re.I | re.S):
        attrs, body = m.group(1), m.group(2)
        if "type=\"application/json\"" in attrs:
            try:
                json.loads(body)
            except ValueError as e:
                out.append(f"JSON data block does not parse: {e}")
        elif "src=" not in attrs and body.strip() and f'nonce="{nonce}"' not in attrs:
            out.append(f"inline <script> without the nonce: {body.strip()[:60]}")
    markup = re.sub(r"<(script|style)\b.*?</\1>", "", html, flags=re.I | re.S)
    for m in re.finditer(r"<[a-zA-Z][^>]*?\sstyle\s*=", markup):
        out.append(f"style attribute: {m.group(0)[:70]}")
    return out


def run():
    db.await_boot_gate()
    import maps
    with db.config() as cx:
        hid = cx.execute("INSERT INTO host (name, address, snmp_community) VALUES ('CITEST-csp','192.0.2.97','public') "
                         "RETURNING id").fetchone()["id"]
        cx.commit()
    mid = maps._make_map("CITEST-csp", [hid])
    with db.config() as cx:
        cx.execute("UPDATE composer.map SET status='published' WHERE id=%s", (mid,))
        cx.commit()
    try:
        ids = dict.fromkeys(("map_id", "host_id", "hid", "id"), mid)
        ids.update(host_id=hid, hid=hid)
        cl = admin_client()
        pages = 0
        for url in page_urls(ids):
            r = cl.get(url, follow_redirects=True)
            if "html" not in r.headers.get("Content-Type", ""):
                continue
            pages += 1
            check(f"admin GET {url}: status", r.status_code < 500, r.status_code)
            p = problems(r)
            check(f"admin GET {url}: CSP kept", not p, "; ".join(p[:3]))
        check("admin: enough pages were checked", pages >= 30, pages)
        anon = appmod.app.test_client()
        for url in ("/login", "/admin/login", "/this-page-does-not-exist"):
            r = anon.get(url)
            if "html" in r.headers.get("Content-Type", ""):
                p = problems(r)
                check(f"anonymous GET {url}: CSP kept", not p, "; ".join(p[:3]))
    finally:
        with db.config() as cx:
            cx.execute("DELETE FROM composer.map WHERE id=%s OR draft_of=%s", (mid, mid))
            cx.execute("DELETE FROM host WHERE id=%s", (hid,))
            cx.commit()

    failed = [(n, d) for n, ok, d in RESULTS if not ok]
    for n, ok, d in RESULTS:
        if not ok:
            print(f"  [FAIL] {n}" + (f" ({d})" if d else ""))
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} PASS")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    run()
