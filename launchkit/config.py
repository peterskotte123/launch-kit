"""Settings come from the environment at call time, so apps can load .env after import.
An app with its own config module can call configure(...) instead."""
import os

_overrides = {}

NAMES = ("STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET", "DATABASE_URL", "SUPABASE_URL", "SUPABASE_JWT_SECRET", "DB_SCHEMA")


def configure(**values):
    for k, v in values.items():
        if k.upper() not in NAMES:
            raise ValueError(f"unknown setting {k}")
        _overrides[k.upper()] = v


def get(name, required=True):
    value = _overrides.get(name) or os.environ.get(name, "")
    if required and not value:
        raise RuntimeError(f"{name} is not set")
    return value
