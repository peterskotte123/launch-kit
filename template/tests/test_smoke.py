"""Runs against a throwaway local Postgres DB; Stripe calls are stubbed, the webhook path is real (signed locally)."""
import hashlib, hmac, json, os, time, types

import psycopg
import pytest
from fastapi.testclient import TestClient

DB = "launchkit_template_test"
USER = {"sub": "user-1", "email": "u@example.com", "aud": "authenticated"}


@pytest.fixture()
def client(monkeypatch):
    with psycopg.connect("postgresql:///postgres", autocommit=True) as conn:
        conn.execute(f"drop database if exists {DB} with (force)")
        conn.execute(f"create database {DB}")
    monkeypatch.setenv("DATABASE_URL", f"postgresql:///{DB}")
    monkeypatch.setenv("DB_SCHEMA", "newthing")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_test")
    from launchkit import auth, payments
    monkeypatch.setattr(payments, "ensure_price", lambda *a, **k: None)
    monkeypatch.setattr(payments, "checkout", lambda *a, **k: types.SimpleNamespace(url="https://checkout.stripe.test/s", kwargs=k))
    monkeypatch.setattr(payments, "portal", lambda cid, ret: f"https://billing.stripe.test/{cid}")
    monkeypatch.setattr(auth, "optional_user", lambda request: USER if request.cookies.get("sb-access-token") else None)
    from app.main import app
    with TestClient(app) as c:
        yield c


def signed(event):
    payload = json.dumps(event).encode()
    ts = int(time.time())
    sig = hmac.new(b"whsec_test", f"{ts}.".encode() + payload, hashlib.sha256).hexdigest()
    return payload, {"stripe-signature": f"t={ts},v1={sig}"}


def test_landing_shows_pricing(client):
    r = client.get("/")
    assert r.status_code == 200 and "$9" in r.text and "/month" in r.text


def test_signed_out_redirects_to_login(client):
    for path in ("/app", "/checkout", "/portal"):
        r = client.get(path, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/login?next=" + path, path


def test_pay_then_access(client):
    client.cookies.set("sb-access-token", "stub")
    r = client.get("/app")
    assert "Upgrade to" in r.text and "Manage subscription" not in r.text
    r = client.get("/checkout", follow_redirects=False)
    assert r.headers["location"] == "https://checkout.stripe.test/s"

    session = {"id": "cs_1", "mode": "subscription", "status": "complete", "client_reference_id": "user-1",
               "customer": "cus_1", "subscription": "sub_1", "metadata": {"lookup_key": "newthing_pro_monthly", "app": "newthing"}}
    body, headers = signed({"id": "evt_1", "type": "checkout.session.completed", "data": {"object": session}})
    assert client.post("/stripe/webhook", content=body, headers=headers).json() == {"received": True}
    assert client.post("/stripe/webhook", content=body, headers=headers).json()["duplicate"] is True

    r = client.get("/app")
    assert "Manage subscription" in r.text
    assert client.get("/portal", follow_redirects=False).headers["location"] == "https://billing.stripe.test/cus_1"


def test_bad_webhook_signature(client):
    r = client.post("/stripe/webhook", content=b"{}", headers={"stripe-signature": "t=1,v1=bad"})
    assert r.status_code == 400
