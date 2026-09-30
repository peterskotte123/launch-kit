import os
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from launchkit import auth, billing, payments, webhooks

# ---- the only block to edit for a new project ------------------------------------------------------------
APP_NAME = "NewThing"
TAGLINE = "One sentence on the problem this solves."
PRODUCT = {"lookup_key": "newthing_pro_monthly", "name": "NewThing Pro", "unit_amount": 900, "interval": "month"}
FREE_FEATURES = ["The free thing"]
PAID_FEATURES = ["Everything in Free", "The thing people pay for"]
# -----------------------------------------------------------------------------------------------------------

BASE_URL = os.environ.get("BASE_URL", "http://localhost:8000").rstrip("/")
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


@asynccontextmanager
async def lifespan(app):
    billing.init_schema()
    payments.ensure_price(PRODUCT["lookup_key"], PRODUCT["name"], PRODUCT["unit_amount"], interval=PRODUCT["interval"])
    yield


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None)


def page(request, name, **ctx):
    return templates.TemplateResponse(request, name, {
        "app_name": APP_NAME, "tagline": TAGLINE, "product": PRODUCT, "free": FREE_FEATURES, "paid": PAID_FEATURES,
        "supabase_url": os.environ.get("SUPABASE_URL", ""), "supabase_anon_key": os.environ.get("SUPABASE_ANON_KEY", ""),
        "user": auth.optional_user(request), **ctx})


def signed_in(request: Request):
    user = auth.optional_user(request)
    if not user:
        # The cookie lapses after an hour; /login renews it from the Supabase session and comes straight back.
        raise HTTPException(303, headers={"Location": "/login?next=" + quote(request.url.path)})
    return user


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/", response_class=HTMLResponse)
def landing(request: Request):
    return page(request, "landing.html")


@app.get("/login", response_class=HTMLResponse)
def login(request: Request):
    return page(request, "login.html")


@app.get("/auth/callback", response_class=HTMLResponse)
def auth_callback(request: Request):
    return page(request, "callback.html")


@app.get("/checkout")
def checkout(user=Depends(signed_in)):
    existing = billing.get(user["sub"])
    session = payments.checkout(PRODUCT["lookup_key"], success_url=f"{BASE_URL}/app?welcome=1", cancel_url=f"{BASE_URL}/#pricing",
                                ref=user["sub"], email=None if existing and existing["stripe_customer_id"] else user.get("email"),
                                customer=existing["stripe_customer_id"] if existing else None, allow_promotion_codes=True)
    return RedirectResponse(session.url, status_code=303)


@app.get("/portal")
def portal(user=Depends(signed_in)):
    row = billing.get(user["sub"])
    if not row or not row["stripe_customer_id"]:
        return RedirectResponse("/#pricing", status_code=303)
    return RedirectResponse(payments.portal(row["stripe_customer_id"], f"{BASE_URL}/app"), status_code=303)


@app.post("/stripe/webhook")
async def stripe_webhook(request: Request):
    try:
        return webhooks.handle(await request.body(), request.headers.get("stripe-signature", ""))
    except webhooks.InvalidSignature as e:
        raise HTTPException(400, f"bad webhook: {e}")


@app.get("/app", response_class=HTMLResponse)
def product_page(request: Request, user=Depends(signed_in)):
    return page(request, "app.html", paid=billing.has_access(user["sub"]), welcome=request.query_params.get("welcome"))


# App-specific reactions to payments go here, e.g. send a welcome email:
# @webhooks.on("checkout.session.completed")
# def welcome(session, event): ...
