# launch-kit

Shared payments + login for small apps, so each new project gets Stripe and Supabase in about an hour.

- `launchkit/`: the package. Install with `pip install -e ../launch-kit` locally.
- `template/`: FastAPI starter. Copy it for a new project.

## New project
```bash
cp -r ~/Projects/launch-kit/template ~/Projects/NewThing && cd ~/Projects/NewThing
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env         # fill in keys; pick a DB_SCHEMA name
# edit the config block at the top of app/main.py (name, tagline, price)
set -a; . ./.env; set +a; .venv/bin/uvicorn app.main:app --port 8000
stripe listen --api-key "$STRIPE_SECRET_KEY" --forward-to localhost:8000/stripe/webhook   # copy whsec_ into .env
```

## CLI
```bash
python -m launchkit new NewThing --price-cents 900 --interval month --tagline "..." [--dir DIR] [--fly-app X] [--venv]
python -m launchkit golive --app-module app.main --fly-app X --url https://x.fly.dev --key-file ~/.secrets/stripe/live-key.txt [--dry-run]
python -m launchkit supabase-redirect --url https://x.fly.dev [--dry-run]    # token: ~/.secrets/supabase/access-token.txt
python -m launchkit check --url https://x.fly.dev [--webhook-secret-file F] [--expect-live]
```
- `new`: copies `template/`, fills the config block, `.env` (shared test key + Supabase values) and `fly.toml`.
- `golive`: live webhook endpoint for the app's handled events, Fly secrets via stdin, then verifies. Run it from the
  app's folder with its venv.
- `supabase-redirect`: adds `<url>/auth/callback` to the shared project's redirect allow-list (idempotent).
- `check`: pass/fail table for health, robots.txt, sitemap, home-page SEO basics and webhook signatures.

## One Stripe login, one Supabase project
- **Stripe:** one account for every app. All apps share `STRIPE_SECRET_KEY`. Each app has its own webhook endpoint
  and its own `STRIPE_WEBHOOK_SECRET`.
  - Every endpoint receives every app's events. `checkout()` and `ensure_price()` tag their Stripe objects with
    `metadata.app`, and webhooks ignore events tagged for another app.
  - The app tag is `APP_ID`, which defaults to `DB_SCHEMA`.
  - Prefix lookup keys with the app name (`permitgap_report`). `ensure_price()` refuses a lookup key that another
    app owns.
- **Supabase:** one project for every app. All apps share `DATABASE_URL`; each app sets its own `DB_SCHEMA`, so its tables (`stripe_events`, `billing_customers`, and the app's own) live in a separate Postgres schema.
- **Shared sign-ins:** Supabase Auth has one user pool per project, so someone who signs up for app A can sign in to app B with the same email. Access is still per app, because `has_access()` reads that app's schema.
- **Redirect URLs:** `python -m launchkit supabase-redirect --url https://<app>` adds the app's `/auth/callback`.

## API
```python
from launchkit import auth, billing, payments, webhooks

payments.ensure_price("x_monthly", "X Pro", 900, interval="month")   # idempotent; run at startup
payments.checkout("x_monthly", success_url, cancel_url, ref=user_id)  # ref -> client_reference_id
payments.portal(customer_id, return_url); payments.refund(payment_intent)

webhooks.handle(body, sig)            # verify + dedupe + dispatch; raises webhooks.InvalidSignature
@webhooks.on("checkout.session.completed")
def fulfil(obj, event): ...           # raise to make Stripe retry

billing.init_schema(); billing.has_access(user_id); billing.get(user_id)
auth.verify(token); Depends(auth.current_user); auth.optional_user(request)
```
`billing` keeps access in sync automatically:
- A paid checkout grants access.
- Subscription updates, pauses and cancellations update it.
- A full refund revokes it.

## Tests
```bash
.venv/bin/pytest -q                                   # kit: needs local Postgres (peer auth)
cd template && PYTHONPATH=. ../.venv/bin/pytest -q    # template smoke test
```
