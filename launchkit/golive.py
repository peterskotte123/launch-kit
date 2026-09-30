"""python -m launchkit golive --app-module app.main --fly-app X --url https://host --key-file PATH [--dry-run]
Switches a kit app on Fly to live Stripe. Safe to re-run. Run it from the app's folder with the app's venv.
1. live webhook endpoint at <url>/stripe/webhook for the events the app handles (created, or enabled/updated); its
   signing secret is kept next to the key file (<fly-app>-live-whsec.txt, 600) so re-runs reuse it
2. STRIPE_SECRET_KEY, STRIPE_WEBHOOK_SECRET, APP_ID -> Fly secrets over stdin (restarts the app; ensure_price creates
   the live prices on boot)
3. verify: /health, live prices tagged metadata.app, endpoint enabled in live mode with exact events, a signed event
   is accepted and a forged one rejected
--dry-run makes no Stripe writes and no Fly changes (read-only Stripe lookups only) and prints the plan."""
import argparse
import ast
import importlib
import inspect
import os
import subprocess
import sys
import time
from pathlib import Path

import stripe

from . import billing, payments, webhooks
from .check import fetch, webhook_checks

FLY = os.path.expanduser("~/.fly/bin/flyctl")


def app_events(module):
    importlib.import_module(module)  # registers the app's @webhooks.on handlers
    return sorted(set(webhooks._handlers) | set(billing.builtin_handlers))


def app_prices(module):
    """[(lookup_key, unit_amount, interval)] for each ensure_price(...) call in the module's source, with the arguments
    evaluated against the module's globals."""
    mod = importlib.import_module(module)
    sig = inspect.signature(payments.ensure_price)
    ns = vars(mod)
    ev = lambda node: eval(compile(ast.Expression(node), mod.__file__, "eval"), ns)  # noqa: E731
    found = []
    for node in ast.walk(ast.parse(Path(mod.__file__).read_text())):
        f = getattr(node, "func", None)
        if isinstance(node, ast.Call) and (getattr(f, "attr", None) or getattr(f, "id", None)) == "ensure_price":
            b = sig.bind(*[ev(a) for a in node.args], **{k.arg: ev(k.value) for k in node.keywords})
            found.append((b.arguments["lookup_key"], b.arguments["unit_amount"], b.arguments.get("interval")))
    return found


def main(argv):
    ap = argparse.ArgumentParser(prog="python -m launchkit golive")
    ap.add_argument("--app-module", required=True)
    ap.add_argument("--fly-app", required=True)
    ap.add_argument("--url", required=True)
    ap.add_argument("--key-file", required=True)
    ap.add_argument("--app-id", help="metadata.app tag and APP_ID secret (default: --fly-app)")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    sys.path.insert(0, os.getcwd())
    url, app_id, dry = a.url.rstrip("/"), a.app_id or a.fly_app, a.dry_run
    hook = f"{url}/stripe/webhook"
    keyfile = Path(a.key_file).expanduser()
    whfile = keyfile.with_name(f"{a.fly_app}-live-whsec.txt")
    say = lambda s: print(("[dry-run] " if dry else "") + s, flush=True)  # noqa: E731

    events, prices = app_events(a.app_module), app_prices(a.app_module)
    key = keyfile.read_text().strip() if keyfile.is_file() else ""
    if not key.startswith(("sk_live_", "rk_live_")):
        say(f"{keyfile}: {'missing' if not key else 'not a live key (needs sk_live_ or rk_live_)'}")
        say(f"plan: webhook endpoint {hook} for {', '.join(events)}; set STRIPE_SECRET_KEY, STRIPE_WEBHOOK_SECRET, "
            f"APP_ID={app_id} on Fly app {a.fly_app}; boot creates live prices {[p[0] for p in prices]}; verify.")
        return 0 if dry else 1
    stripe.api_key = key

    ep = next((e for e in stripe.WebhookEndpoint.list(limit=100).auto_paging_iter() if e.url == hook), None)
    whsec = whfile.read_text().strip() if whfile.is_file() else ""
    if ep and not whsec:
        say(f"endpoint {ep.id} exists but its signing secret is not saved in {whfile}: delete and recreate it")
        if not dry:
            stripe.WebhookEndpoint.delete(ep.id)
        ep = None
    if ep:
        if ep.status != "enabled" or set(ep.enabled_events) != set(events):
            say(f"endpoint {ep.id}: enable and set events {events}")
            if not dry:
                ep = stripe.WebhookEndpoint.modify(ep.id, enabled_events=events, disabled=False)
        else:
            say(f"endpoint {ep.id} already enabled with {events}; secret in {whfile}")
    else:
        say(f"create live webhook endpoint {hook} for {events}; save its secret to {whfile}")
        if not dry:
            ep = stripe.WebhookEndpoint.create(url=hook, enabled_events=events, description=app_id)
            whsec = ep.secret
            whfile.write_text(whsec)
            whfile.chmod(0o600)

    def live_price(lookup_key):
        return next(iter(stripe.Price.list(lookup_keys=[lookup_key], active=True, limit=1).data), None)

    for lookup_key, amount, interval in prices:
        p = live_price(lookup_key)
        tag = p and (p.to_dict().get("metadata") or {}).get("app")
        say(f"live price {lookup_key}: " + (f"{p.id} ({p.unit_amount} {p.currency}, metadata.app={tag}) already in place"
                                            if p else f"missing; ensure_price creates it ({amount}, {interval or 'once'}) on boot"))
    say(f"set STRIPE_SECRET_KEY, STRIPE_WEBHOOK_SECRET, APP_ID={app_id} on Fly app {a.fly_app} (machines restart)")
    if dry:
        return 0

    subprocess.run([FLY, "secrets", "import", "-a", a.fly_app], check=True, text=True,
                   input=f"STRIPE_SECRET_KEY={key}\nSTRIPE_WEBHOOK_SECRET={whsec}\nAPP_ID={app_id}\n")

    ok = True

    def check(label, cond):
        nonlocal ok
        ok &= bool(cond)
        print(f"{'PASS' if cond else 'FAIL'}  {label}", flush=True)

    for _ in range(40):
        if fetch(f"{url}/health", timeout=10)[0] == 200:
            break
        time.sleep(3)
    check("/health 200 after restart", fetch(f"{url}/health", timeout=10)[0] == 200)
    for lookup_key, amount, interval in prices:
        p = live_price(lookup_key)
        meta = (p.to_dict().get("metadata") or {}) if p else {}
        check(f"live price {lookup_key} exists with metadata.app={app_id}" + (f" ({p.id})" if p else ""),
              p and p.livemode and p.unit_amount == amount and meta.get("app") == app_id)
    ep = stripe.WebhookEndpoint.retrieve(ep.id)
    check(f"webhook endpoint {ep.id} enabled in live mode with exact events",
          ep.status == "enabled" and ep.livemode and set(ep.enabled_events) == set(events))
    for label, passed, detail in webhook_checks(url, whsec):
        check(f"{label} ({detail})", passed)
    return 0 if ok else 1
