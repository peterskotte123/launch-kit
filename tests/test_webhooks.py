import hashlib
import hmac
import json
import time

import pytest

from launchkit import billing, webhooks


def signed(event, secret="whsec_test_secret", ts=None):
    payload = json.dumps(event).encode()
    ts = ts or int(time.time())
    sig = hmac.new(secret.encode(), f"{ts}.".encode() + payload, hashlib.sha256).hexdigest()
    return payload, f"t={ts},v1={sig}"


def event(type_, obj, id_="evt_1"):
    return {"id": id_, "object": "event", "type": type_, "data": {"object": obj}}


def test_rejects_bad_signature():
    payload, sig = signed(event("x", {}), secret="whsec_wrong")
    with pytest.raises(webhooks.InvalidSignature):
        webhooks.handle(payload, sig)


def test_rejects_stale_timestamp():
    payload, sig = signed(event("x", {}), ts=int(time.time()) - 3600)
    with pytest.raises(webhooks.InvalidSignature):
        webhooks.handle(payload, sig)


def test_dispatch_and_dedupe():
    calls = []
    webhooks.on("invoice.paid")(lambda obj, ev: calls.append(obj["id"]))
    payload, sig = signed(event("invoice.paid", {"id": "in_1"}))
    assert webhooks.handle(payload, sig) == {"received": True}
    assert webhooks.handle(payload, sig) == {"received": True, "duplicate": True}
    assert calls == ["in_1"]


def test_failed_handler_is_retried():
    calls = []

    def flaky(obj, ev):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")

    webhooks.on("invoice.paid")(flaky)
    payload, sig = signed(event("invoice.paid", {"id": "in_1"}))
    with pytest.raises(RuntimeError):
        webhooks.handle(payload, sig)
    assert webhooks.handle(payload, sig) == {"received": True}
    assert len(calls) == 2


def test_one_time_payment_grants_access():
    s = {"id": "cs_1", "mode": "payment", "payment_status": "paid", "client_reference_id": "user-1",
         "customer": "cus_1", "metadata": {"lookup_key": "report"}}
    webhooks.handle(*signed(event("checkout.session.completed", s)))
    assert billing.has_access("user-1")
    assert billing.get("user-1")["lookup_key"] == "report"
    assert not billing.has_access("user-2")


def test_unpaid_checkout_ignored():
    s = {"id": "cs_1", "mode": "payment", "payment_status": "unpaid", "client_reference_id": "user-1"}
    webhooks.handle(*signed(event("checkout.session.completed", s)))
    assert billing.get("user-1") is None


def test_subscription_lifecycle():
    future = int(time.time()) + 86400 * 30
    s = {"id": "cs_1", "mode": "subscription", "status": "complete", "client_reference_id": "user-1",
         "customer": "cus_1", "subscription": "sub_1", "metadata": {"lookup_key": "pro"}}
    webhooks.handle(*signed(event("checkout.session.completed", s, "evt_1")))
    sub = {"id": "sub_1", "customer": "cus_1", "status": "active", "metadata": {"ref": "user-1"},
           "items": {"data": [{"current_period_end": future}]}}
    webhooks.handle(*signed(event("customer.subscription.updated", sub, "evt_2")))
    assert billing.has_access("user-1")
    assert billing.get("user-1")["current_period_end"] is not None

    webhooks.handle(*signed(event("customer.subscription.updated", {**sub, "pause_collection": {"behavior": "void"}}, "evt_3")))
    assert not billing.has_access("user-1")

    webhooks.handle(*signed(event("customer.subscription.deleted", {**sub, "status": "canceled"}, "evt_4")))
    assert billing.get("user-1")["status"] == "canceled"
    assert not billing.has_access("user-1")


def test_subscription_without_ref_updates_by_subscription_id():
    s = {"id": "cs_1", "mode": "subscription", "status": "complete", "client_reference_id": "user-1",
         "customer": "cus_1", "subscription": "sub_1", "metadata": {}}
    webhooks.handle(*signed(event("checkout.session.completed", s, "evt_1")))
    webhooks.handle(*signed(event("customer.subscription.updated",
                                  {"id": "sub_1", "customer": "cus_1", "status": "past_due", "metadata": {}}, "evt_2")))
    assert billing.get("user-1")["status"] == "past_due"


def test_schemas_isolate_apps(monkeypatch):
    s = {"id": "cs_1", "mode": "payment", "payment_status": "paid", "client_reference_id": "user-1", "metadata": {}}
    monkeypatch.setenv("DB_SCHEMA", "app_a")
    billing.init_schema()
    webhooks.handle(*signed(event("checkout.session.completed", s, "evt_a")))
    assert billing.has_access("user-1")
    monkeypatch.setenv("DB_SCHEMA", "app_b")
    billing.init_schema()
    assert not billing.has_access("user-1")
    webhooks.handle(*signed(event("checkout.session.completed", s, "evt_a")))  # same event id, separate app: not a duplicate
    assert billing.has_access("user-1")


def test_full_refund_revokes_one_time_access():
    s = {"id": "cs_1", "mode": "payment", "payment_status": "paid", "client_reference_id": "user-1",
         "payment_intent": "pi_1", "metadata": {}}
    webhooks.handle(*signed(event("checkout.session.completed", s, "evt_1")))
    webhooks.handle(*signed(event("charge.refunded", {"id": "ch_1", "payment_intent": "pi_1", "refunded": False}, "evt_2")))
    assert billing.has_access("user-1")  # partial refund keeps access
    webhooks.handle(*signed(event("charge.refunded", {"id": "ch_1", "payment_intent": "pi_1", "refunded": True}, "evt_3")))
    assert billing.get("user-1")["status"] == "refunded"
    assert not billing.has_access("user-1")
