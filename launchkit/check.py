"""python -m launchkit check --url https://host [--webhook-secret-file F] [--expect-live]
Pass/fail table for a deployed app: health, robots.txt, sitemap.xml, home page SEO basics, webhook signature checks."""
import argparse
import datetime
import hashlib
import hmac
import html.parser
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0 Safari/537.36"
BOTS = ("Googlebot", "Bingbot", "OAI-SearchBot", "PerplexityBot", "Claude-SearchBot")


def fetch(url, data=None, headers=None, timeout=20):
    """(status, body text); never raises on HTTP errors. status 0 means no response."""
    req = urllib.request.Request(url, data=data, headers={"User-Agent": UA, **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except (urllib.error.URLError, OSError) as e:
        return 0, str(e)


def sign(payload, secret, ts=None):
    """Stripe-Signature header value for payload (bytes) signed with a whsec_ secret."""
    ts = ts or int(time.time())
    sig = hmac.new(secret.encode(), f"{ts}.".encode() + payload, hashlib.sha256).hexdigest()
    return f"t={ts},v1={sig}"


def webhook_checks(url, secret):
    """Post a signed synthetic event (expect 200) and a forged one (expect 4xx)."""
    hook = url.rstrip("/") + "/stripe/webhook"
    body = json.dumps({"id": f"evt_check_{int(time.time() * 1000)}", "object": "event", "type": "launchkit.check",
                       "data": {"object": {}}}).encode()
    h = {"content-type": "application/json"}
    good, _ = fetch(hook, body, {**h, "stripe-signature": sign(body, secret)})
    bad, _ = fetch(hook, body, {**h, "stripe-signature": sign(body, "whsec_forged")})
    return [("webhook accepts a signed event", good == 200, f"HTTP {good}"),
            ("webhook rejects a forged event", 400 <= bad < 500, f"HTTP {bad}")]


def check_robots(text):
    names = {n.lower() for n in re.findall(r"^\s*user-agent:\s*(\S+)", text, re.I | re.M)}
    missing = [b for b in BOTS if b.lower() not in names]
    return [("robots.txt names " + ", ".join(BOTS), not missing, "missing " + ", ".join(missing) if missing else ""),
            ("robots.txt has a Sitemap line", bool(re.search(r"^\s*sitemap:\s*\S", text, re.I | re.M)), "")]


def check_sitemap(text, today):
    try:
        root = ET.fromstring(text.encode())
    except ET.ParseError as e:
        return [("sitemap.xml is valid XML", False, str(e))]
    lastmods = [e.text.strip()[:10] for e in root.iter() if e.tag.endswith("lastmod") and e.text]
    urls = [e for e in root.iter() if e.tag.endswith("}loc") or e.tag == "loc"]
    all_today = bool(lastmods) and all(d == today for d in lastmods)
    return [("sitemap.xml is valid XML", bool(urls), f"{len(urls)} URLs"),
            ("sitemap lastmod values not all today", bool(lastmods) and not all_today,
             "no lastmod" if not lastmods else f"{len(lastmods)} lastmod, {len(set(lastmods))} distinct")]


class _Head(html.parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self.title, self.in_title, self.description, self.canonical, self.h1, self.site_name, self.text = \
            "", False, "", "", False, "", ""

    def handle_starttag(self, tag, attrs):
        a = {k: v or "" for k, v in attrs}
        if tag == "title":
            self.in_title = True
        elif tag == "meta" and a.get("name", "").lower() == "description":
            self.description = a.get("content", "").strip()
        elif tag == "meta" and a.get("property", "").lower() == "og:site_name":
            self.site_name = a.get("content", "")
        elif tag == "link" and "canonical" in a.get("rel", "").lower().split():
            self.canonical = a.get("href", "")
        elif tag == "h1":
            self.h1 = True

    def handle_endtag(self, tag):
        if tag == "title":
            self.in_title = False

    def handle_data(self, data):
        if self.in_title:
            self.title += data


def _norm(s):
    return re.sub(r"[^a-z0-9]", "", s.lower())


def check_home(page, host, expect_live=False):
    p = _Head()
    p.feed(page)
    title = p.title.strip()
    names = {_norm(host.split(".")[0])} | ({_norm(p.site_name)} if p.site_name else set())
    rows = [("home <title> is more than the app name", bool(title) and _norm(title) not in names, repr(title[:60])),
            ("home has a meta description", bool(p.description), f"{len(p.description)} chars"),
            ("home has a canonical link", bool(p.canonical), p.canonical),
            ("home has an <h1>", p.h1, "")]
    if expect_live:
        test = re.findall(r"\b(pk_test_|cs_test_)", page)
        rows.append(("home shows no test-mode Stripe keys/links", not test, ", ".join(sorted(set(test)))))
    return rows


def run(url, secret=None, expect_live=False):
    url = url.rstrip("/")
    host = urllib.parse.urlparse(url).hostname or ""
    status, _ = fetch(url + "/health")
    rows = [("/health returns 200", status == 200, f"HTTP {status}")]
    status, text = fetch(url + "/robots.txt")
    rows += check_robots(text) if status == 200 else [("robots.txt returns 200", False, f"HTTP {status}")]
    status, text = fetch(url + "/sitemap.xml")
    today = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
    rows += check_sitemap(text, today) if status == 200 else [("sitemap.xml returns 200", False, f"HTTP {status}")]
    status, text = fetch(url + "/")
    rows += check_home(text, host, expect_live) if status == 200 else [("home returns 200", False, f"HTTP {status}")]
    if secret:
        rows += webhook_checks(url, secret)
    elif expect_live:
        rows.append(("--expect-live needs --webhook-secret-file (the live whsec)", False, ""))
    return rows


def main(argv):
    ap = argparse.ArgumentParser(prog="python -m launchkit check")
    ap.add_argument("--url", required=True)
    ap.add_argument("--webhook-secret-file")
    ap.add_argument("--expect-live", action="store_true",
                    help="require the live webhook secret file and no pk_test_/cs_test_ on the home page")
    a = ap.parse_args(argv)
    secret = Path(a.webhook_secret_file).expanduser().read_text().strip() if a.webhook_secret_file else None
    rows = run(a.url, secret, a.expect_live)
    width = max(len(r[0]) for r in rows)
    for label, ok, detail in rows:
        print(f"{'PASS' if ok else 'FAIL'}  {label.ljust(width)}  {detail}")
    failed = sum(not r[1] for r in rows)
    print(f"{len(rows) - failed}/{len(rows)} passed")
    return 1 if failed else 0
