"""Verified, deduplicated Stripe webhooks with handler registration. Framework-agnostic:

    @webhooks.on("checkout.session.completed")
    def paid(obj, event): ...

    # FastAPI:  return webhooks.handle(await request.body(), request.headers.get("stripe-signature", ""))
    # Flask:    return webhooks.handle(request.data, request.headers.get("Stripe-Signature", ""))

handle() raises InvalidSignature (return 400). If a handler raises, the event is not marked processed, the exception
propagates (return 500) and Stripe retries it.
"""
import collections

import stripe

from . import billing, config, payments

_handlers = collections.defaultdict(list)


class InvalidSignature(Exception):
    pass


def on(event_type):
    def register(fn):
        _handlers[event_type].append(fn)
        return fn
    return register


def verify(payload, sig):
    try:
        return stripe.Webhook.construct_event(payload, sig, config.get("STRIPE_WEBHOOK_SECRET"))
    except (ValueError, stripe.error.SignatureVerificationError) as e:
        raise InvalidSignature(str(e)) from e


def _is_ours(obj, event_type):
    """With one Stripe account, every app's webhook receives every app's events. Checkouts and subscriptions made by
    checkout() carry metadata.app; anything else untagged (charges, invoices) passes through, and the built-in handlers
    only touch rows this app created."""
    app = config.app_id()
    if not app:
        return True
    meta = obj.get("metadata") or {}
    owner = meta.get("app") or ((obj.get("subscription_details") or {}).get("metadata") or {}).get("app")
    if owner:
        return owner == app
    if event_type.startswith(("checkout.session.", "customer.subscription.")):
        # Sessions from launch-kit 0.1 have no app tag, only a lookup_key: accept this app's own prices.
        return meta.get("lookup_key") in payments._price_cache
    return True


def handle(payload, sig):
    event = verify(payload, sig)
    return dispatch(event)


def dispatch(event):
    event_id, event_type = event["id"], event["type"]
    with billing.connect() as conn:
        with conn.transaction():
            claimed = conn.execute(
                "insert into stripe_events (id, type) values (%s, %s) on conflict (id) do nothing returning id",
                (event_id, event_type)).fetchone()
            if not claimed:
                return {"received": True, "duplicate": True}
            obj = event["data"]["object"]
            obj = obj.to_dict() if hasattr(obj, "to_dict") else dict(obj)
            if not _is_ours(obj, event_type):
                return {"received": True, "ignored": "another app"}
            for fn in billing.builtin_handlers.get(event_type, []):
                fn(obj, conn)
            for fn in _handlers.get(event_type, []):
                fn(obj, event)
    return {"received": True}
