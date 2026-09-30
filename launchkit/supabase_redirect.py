"""python -m launchkit supabase-redirect --url https://host [--dry-run]
Adds <url>/auth/callback (the template's magic-link landing page) to the shared Supabase project's Auth redirect
allow-list via the Management API: GET, then PATCH /v1/projects/{ref}/config/auth with uri_allow_list (comma-separated).
Keeps existing entries; does nothing if the URL is already there."""
import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .check import fetch

REF = "wurikcpajrkvmnohungl"
API = "https://api.supabase.com/v1/projects/{ref}/config/auth"
TOKEN_FILE = Path("~/.secrets/supabase/access-token.txt").expanduser()
KIT = Path(__file__).resolve().parent.parent


def merge(allow_list, url):
    """(new comma-separated list, changed?)"""
    entries = [e.strip() for e in (allow_list or "").split(",") if e.strip()]
    if url in entries:
        return ",".join(entries), False
    return ",".join(entries + [url]), True


def _env_value(name, *files):
    if os.environ.get(name):
        return os.environ[name]
    for f in files:
        if f.is_file():
            for line in f.read_text().splitlines():
                if line.startswith(name + "="):
                    return line.split("=", 1)[1].split("#")[0].strip().strip("\"'")
    return ""


def project_ref():
    url = _env_value("SUPABASE_URL", Path.cwd() / ".env", KIT / "template" / ".env")
    ref = (urllib.parse.urlparse(url).hostname or "").split(".")[0]
    if ref and ref != REF:
        raise SystemExit(f"SUPABASE_URL points at project {ref}, expected the shared project {REF}")
    return REF


def token():
    t = os.environ.get("SUPABASE_ACCESS_TOKEN", "")
    return t or (TOKEN_FILE.read_text().strip() if TOKEN_FILE.is_file() else "")


def main(argv):
    ap = argparse.ArgumentParser(prog="python -m launchkit supabase-redirect")
    ap.add_argument("--url", required=True)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    callback = a.url.rstrip("/") + "/auth/callback"
    ref = project_ref()
    tok = token()
    if not tok:
        print('No Supabase token: run `ask-secret ~/.secrets/supabase/access-token.txt '
              '"Supabase personal access token (supabase.com/dashboard/account/tokens)"`', file=sys.stderr)
        return 2
    endpoint, h = API.format(ref=ref), {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}
    status, body = fetch(endpoint, headers=h)
    if status != 200:
        print(f"GET auth config failed: HTTP {status}", file=sys.stderr)
        return 1
    merged, changed = merge(json.loads(body).get("uri_allow_list"), callback)
    if not changed:
        print(f"{callback} already in the redirect allow-list of {ref}")
        return 0
    if a.dry_run:
        print(f"[dry-run] would add {callback} to the redirect allow-list of {ref}")
        return 0
    req = urllib.request.Request(endpoint, json.dumps({"uri_allow_list": merged}).encode(), h, method="PATCH")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            ok = callback in [e.strip() for e in (json.loads(r.read()).get("uri_allow_list") or "").split(",")]
    except urllib.error.HTTPError as e:
        print(f"PATCH auth config failed: HTTP {e.code}", file=sys.stderr)
        return 1
    print(f"{'added' if ok else 'PATCH returned but the list does not contain'} {callback} ({ref})")
    return 0 if ok else 1
