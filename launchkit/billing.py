"""Postgres state: processed webhook events and who has paid. user_ref is whatever the app passed as ref to checkout()
(a Supabase user id for account-based apps, an order token for one-off purchases). Safe to add to an existing DB."""
import datetime

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from . import config

SCHEMA = """
create table if not exists stripe_events (
    id text primary key,
    type text not null,
    received_at timestamptz not null default now()
);
create table if not exists billing_customers (
    user_ref text primary key,
    stripe_customer_id text,
    stripe_subscription_id text,
    stripe_payment_intent text,
    lookup_key text,
    status text not null,
    current_period_end timestamptz,
    updated_at timestamptz not null default now()
);
create index if not exists billing_customers_customer_idx on billing_customers(stripe_customer_id);
alter table billing_customers add column if not exists stripe_payment_intent text;
"""

ACTIVE = ("active", "trialing", "paid")


def connect():
    """DB_SCHEMA (optional) puts this app's tables in their own Postgres schema, so many apps can share one database,
    e.g. one Supabase project. Set it once per app; it is applied to every connection the kit opens."""
    conn = psycopg.connect(config.get("DATABASE_URL"), row_factory=dict_row)
    schema = config.get("DB_SCHEMA", required=False)
    if schema:
        conn.execute(sql.SQL("set search_path to {}, public").format(sql.Identifier(schema)))
    return conn


def init_schema():
    schema = config.get("DB_SCHEMA", required=False)
    with connect() as conn:
        if schema:
            conn.execute(sql.SQL("create schema if not exists {}").format(sql.Identifier(schema)))
        conn.execute(SCHEMA)


def get(user_ref):
    with connect() as conn:
        return conn.execute("select * from billing_customers where user_ref = %s", (str(user_ref),)).fetchone()


def has_access(user_ref):
    row = get(user_ref)
    if not row or row["status"] not in ACTIVE:
        return False
    return row["current_period_end"] is None or row["current_period_end"] > datetime.datetime.now(datetime.timezone.utc)


def _ts(value):
    return datetime.datetime.fromtimestamp(value, datetime.timezone.utc) if value else None


def _period_end(sub):
    # Newer API versions moved current_period_end from the subscription onto its items.
    if sub.get("current_period_end"):
        return _ts(sub["current_period_end"])
    items = (sub.get("items") or {}).get("data") or []
    return _ts(max((i.get("current_period_end") or 0) for i in items)) if items else None


def _on_checkout_completed(s, conn):
    ref = s.get("client_reference_id")
    if not ref:
        return
    if s.get("mode") == "subscription":
        status = "active" if s.get("status") == "complete" else "incomplete"
    elif s.get("payment_status") == "paid":
        status = "paid"
    else:
        return
    conn.execute(
        """insert into billing_customers (user_ref, stripe_customer_id, stripe_subscription_id, stripe_payment_intent, lookup_key, status)
           values (%s, %s, %s, %s, %s, %s)
           on conflict (user_ref) do update set stripe_customer_id = coalesce(excluded.stripe_customer_id, billing_customers.stripe_customer_id),
             stripe_subscription_id = coalesce(excluded.stripe_subscription_id, billing_customers.stripe_subscription_id),
             stripe_payment_intent = coalesce(excluded.stripe_payment_intent, billing_customers.stripe_payment_intent),
             lookup_key = excluded.lookup_key, status = excluded.status, updated_at = now()""",
        (ref, s.get("customer"), s.get("subscription"), s.get("payment_intent"), (s.get("metadata") or {}).get("lookup_key"), status))


def _on_subscription_changed(sub, conn):
    ref = (sub.get("metadata") or {}).get("ref")
    status = sub.get("status")
    if sub.get("pause_collection"):
        status = "paused"
    params = (sub.get("customer"), sub.get("id"), status, _period_end(sub))
    if ref:
        conn.execute(
            """insert into billing_customers (user_ref, stripe_customer_id, stripe_subscription_id, status, current_period_end)
               values (%s, %s, %s, %s, %s)
               on conflict (user_ref) do update set stripe_customer_id = excluded.stripe_customer_id,
                 stripe_subscription_id = excluded.stripe_subscription_id, status = excluded.status,
                 current_period_end = excluded.current_period_end, updated_at = now()""",
            (ref, *params))
    else:
        conn.execute(
            """update billing_customers set stripe_customer_id = %s, stripe_subscription_id = %s, status = %s,
               current_period_end = %s, updated_at = now() where stripe_subscription_id = %s""",
            (*params, sub.get("id")))


def _on_charge_refunded(charge, conn):
    if charge.get("refunded") and charge.get("payment_intent"):  # full refunds only
        conn.execute("update billing_customers set status = 'refunded', updated_at = now() where stripe_payment_intent = %s",
                     (charge["payment_intent"],))


builtin_handlers = {
    "charge.refunded": [_on_charge_refunded],
    "checkout.session.completed": [_on_checkout_completed],
    "customer.subscription.created": [_on_subscription_changed],
    "customer.subscription.updated": [_on_subscription_changed],
    "customer.subscription.deleted": [_on_subscription_changed],
}
