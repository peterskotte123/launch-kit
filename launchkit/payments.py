import stripe

from . import config

_price_cache = {}


def _client():
    stripe.api_key = config.get("STRIPE_SECRET_KEY")
    return stripe


def ensure_price(lookup_key, name, unit_amount, interval=None, currency="usd", description=None):
    """Find the active price by lookup_key, creating product + price on first use. Prices live in code, not the dashboard.
    interval: None for one-time, or 'month'/'year' for a subscription."""
    if lookup_key in _price_cache:
        return _price_cache[lookup_key]
    s = _client()
    found = s.Price.list(lookup_keys=[lookup_key], active=True, limit=1).data
    if found:
        price = found[0]
    else:
        product = s.Product.create(name=name, **({"description": description} if description else {}))
        price = s.Price.create(product=product.id, unit_amount=unit_amount, currency=currency, lookup_key=lookup_key,
                               **({"recurring": {"interval": interval}} if interval else {}))
    _price_cache[lookup_key] = price
    return price


def get_price(lookup_key):
    if lookup_key in _price_cache:
        return _price_cache[lookup_key]
    found = _client().Price.list(lookup_keys=[lookup_key], active=True, limit=1).data
    if not found:
        raise LookupError(f"no active price with lookup_key {lookup_key!r}; call ensure_price first")
    _price_cache[lookup_key] = found[0]
    return found[0]


def checkout(lookup_key, success_url, cancel_url, ref=None, email=None, customer=None, metadata=None, quantity=1, **extra):
    """Create a hosted Checkout session and return it (redirect to session.url).
    ref -> client_reference_id (an order token or a Supabase user id). Mode follows the price: recurring -> subscription.
    extra passes straight through to Stripe (e.g. shipping_address_collection, allow_promotion_codes)."""
    price = get_price(lookup_key)
    mode = "subscription" if price.recurring else "payment"
    meta = {"lookup_key": lookup_key, **(metadata or {})}
    params = dict(mode=mode, line_items=[{"price": price.id, "quantity": quantity}], success_url=success_url,
                  cancel_url=cancel_url, metadata=meta, **extra)
    if ref:
        params["client_reference_id"] = str(ref)
    if customer:
        params["customer"] = customer
    elif email:
        params["customer_email"] = email
    if mode == "subscription":
        params.setdefault("subscription_data", {}).setdefault("metadata", {}).update(meta, **({"ref": str(ref)} if ref else {}))
    return _client().checkout.Session.create(**params)


def portal(customer_id, return_url):
    """Stripe-hosted page where the customer cancels, updates their card and sees invoices. Returns the URL."""
    return _client().billing_portal.Session.create(customer=customer_id, return_url=return_url).url


def refund(payment_intent, amount=None):
    return _client().Refund.create(payment_intent=payment_intent, **({"amount": amount} if amount else {}))
