import os

import psycopg
import pytest

TEST_DB = os.environ.get("LAUNCHKIT_TEST_DB", "launchkit_test")


@pytest.fixture(scope="session", autouse=True)
def database():
    with psycopg.connect("postgresql:///postgres", autocommit=True) as conn:
        conn.execute(f"drop database if exists {TEST_DB}")
        conn.execute(f"create database {TEST_DB}")
    os.environ["DATABASE_URL"] = f"postgresql:///{TEST_DB}"
    os.environ["STRIPE_WEBHOOK_SECRET"] = "whsec_test_secret"
    from launchkit import billing
    billing.init_schema()
    yield
    with psycopg.connect("postgresql:///postgres", autocommit=True) as conn:
        conn.execute(f"drop database if exists {TEST_DB} with (force)")


@pytest.fixture(autouse=True)
def clean(database):
    from launchkit import billing, webhooks
    with billing.connect() as conn:
        conn.execute("truncate stripe_events, billing_customers")
    webhooks._handlers.clear()
