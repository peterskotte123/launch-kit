"""Several apps on one Stripe account: each tags its checkouts and ignores the others' events."""
import pytest
import stripe

from launchkit import billing, payments, webhooks
from test_webhooks import event, signed


@pytest.fixture
def app_a(monkeypatch):
    monkeypatch.setenv("APP_ID", "app_a")
    monkeypatch.setattr(payments, "_price_cache", {})


def paid(meta, id_="cs_1", ref="user-1"):
    return {"id": id_, "mode": "payment", "payment_status": "paid", "client_reference_id": ref, "metadata": meta}


def test_other_apps_checkout_is_ignored(app_a):
    calls = []
    webhooks.on("checkout.session.completed")(lambda obj, ev: calls.append(obj["id"]))
    result = webhooks.handle(*signed(event("checkout.session.completed", paid({"app": "app_b"}))))
    assert result == {"received": True, "ignored": "another app"}
    assert billing.get("user-1") is None and calls == []


def test_own_checkout_grants_access(app_a):
    webhooks.handle(*signed(event("checkout.session.completed", paid({"app": "app_a", "lookup_key": "a_report"}))))
    assert billing.has_access("user-1")


def test_untagged_checkout_only_accepted_for_own_price(app_a):
    payments._price_cache["a_report"] = object()
    webhooks.handle(*signed(event("checkout.session.completed", paid({"lookup_key": "a_report"}, "cs_1", "user-1"), "evt_1")))
    webhooks.handle(*signed(event("checkout.session.completed", paid({"user_id": "u", "tier": "pro"}, "cs_2", "user-2"), "evt_2")))
    assert billing.has_access("user-1")
    assert billing.get("user-2") is None


def test_other_apps_subscription_is_ignored(app_a):
    sub = {"id": "sub_9", "customer": "cus_9", "status": "active", "metadata": {"app": "app_b", "ref": "user-1"}}
    webhooks.handle(*signed(event("customer.subscription.created", sub)))
    assert billing.get("user-1") is None


def test_other_apps_invoice_is_ignored_for_custom_handlers(app_a):
    calls = []
    webhooks.on("invoice.paid")(lambda obj, ev: calls.append(obj["id"]))
    inv = {"id": "in_9", "subscription_details": {"metadata": {"app": "app_b"}}}
    webhooks.handle(*signed(event("invoice.paid", inv)))
    assert calls == []


def test_checkout_tags_session_and_payment(app_a, monkeypatch):
    sent = {}
    monkeypatch.setattr(stripe.checkout.Session, "create", lambda **p: sent.update(p) or p)
    payments._price_cache["a_report"] = stripe.Price.construct_from({"id": "price_1", "recurring": None}, "sk_test")
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_x")
    payments.checkout("a_report", "https://a/ok", "https://a/no", ref="user-1")
    assert sent["metadata"]["app"] == "app_a"
    assert sent["payment_intent_data"]["metadata"]["app"] == "app_a"


def test_ensure_price_refuses_another_apps_lookup_key(app_a, monkeypatch):
    other = stripe.Price.construct_from({"id": "price_b", "metadata": {"app": "app_b"}}, "sk_test")
    monkeypatch.setattr(stripe.Price, "list", lambda **k: type("L", (), {"data": [other]})())
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_x")
    with pytest.raises(ValueError, match="belongs to app 'app_b'"):
        payments.ensure_price("pro", "Pro", 900, interval="month")
