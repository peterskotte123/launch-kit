"""python -m launchkit new <Name> [--dir DIR] --price-cents N --interval month|year|once --tagline "..." [--fly-app X] [--venv]
Copies template/ to ~/Projects/<Name>, fills the config block, writes .env (shared test Stripe key and Supabase values
copied from an existing kit app) and sets the Fly app name."""
import argparse
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

KIT = Path(__file__).resolve().parent.parent
TEMPLATE = KIT / "template"
SOURCES = [Path("~/Projects/PermitGap/.env").expanduser(), TEMPLATE / ".env"]
SHARED = ("STRIPE_SECRET_KEY", "DATABASE_URL", "SUPABASE_URL", "SUPABASE_ANON_KEY")
PLAN = {"month": "pro_monthly", "year": "pro_yearly", "once": "pro"}


def slugify(name):
    slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    if not re.match(r"[a-z]", slug):
        raise SystemExit(f"name {name!r} must start with a letter")
    return slug


def read_env(path):
    values = {}
    for line in path.read_text().splitlines() if path.is_file() else []:
        m = re.match(r"([A-Z_][A-Z0-9_]*)=(.*)", line.strip())
        if m:
            v = m.group(2).strip()
            v = v[1:-1] if v[:1] in "\"'" and v[-1:] == v[:1] and len(v) > 1 else re.sub(r"\s+#.*", "", v)
            values[m.group(1)] = v
    return values


def placeholder(name, value):
    """Empty, an example value, or (for DATABASE_URL) a local database rather than the shared Supabase one."""
    if not value or "..." in value or "<" in value:
        return True
    if name == "STRIPE_SECRET_KEY":
        return not value.startswith(("sk_test_", "rk_test_"))  # new apps start on the TEST key
    return bool(re.search(r"(localhost|127\.0\.0\.1|://[^@]*/|:///)", value)) if name == "DATABASE_URL" else False


def shared_values(sources=SOURCES):
    """{name: (value, source)} for each shared setting found in a source; placeholders are skipped."""
    found = {}
    for src in sources:
        for k, v in read_env(src).items():
            if k in SHARED and k not in found and not placeholder(k, v):
                found[k] = (v, src)
    return found


def set_line(text, name, value):
    """Replace `NAME=...` in an env file (dropping any trailing comment), or append it."""
    line = f"{name}={value}"
    new, n = re.subn(rf"^{name}=.*$", lambda _: line, text, flags=re.M)
    return new if n else new.rstrip("\n") + "\n" + line + "\n"


def fill_config(main_py, name, tagline, slug, cents, interval):
    lookup = f"{slug}_{PLAN[interval]}"
    product = (f'{{"lookup_key": {json.dumps(lookup)}, "name": {json.dumps(name + " Pro")}, "unit_amount": {cents}, '
               f'"interval": {json.dumps(interval) if interval != "once" else "None"}}}')
    for var, value in (("APP_NAME", json.dumps(name)), ("TAGLINE", json.dumps(tagline)), ("PRODUCT", product)):
        main_py, n = re.subn(rf"^{var} = .*$", lambda _: f"{var} = {value}", main_py, count=1, flags=re.M)
        if not n:
            raise SystemExit(f"template app/main.py has no {var} line")
    return main_py


def scaffold(name, target, cents, interval, tagline, fly_app=None, sources=SOURCES):
    """Create the project and return a list of notes for the user."""
    slug, target = slugify(name), Path(target).expanduser().resolve()
    fly_app = fly_app or slug.replace("_", "-")
    if not TEMPLATE.is_dir():
        raise SystemExit(f"{TEMPLATE} not found; run new from the launch-kit checkout (pip install -e)")
    if target.exists():
        raise SystemExit(f"{target} already exists; refusing to overwrite")
    shutil.copytree(TEMPLATE, target, ignore=shutil.ignore_patterns(".env", ".venv", "__pycache__", ".pytest_cache"))

    main_py = target / "app" / "main.py"
    main_py.write_text(fill_config(main_py.read_text(), name, tagline, slug, cents, interval))

    fly = target / "fly.toml"
    text = re.sub(r'^app = .*$', f'app = "{fly_app}"', fly.read_text(), count=1, flags=re.M)
    fly.write_text(text.replace("[http_service]", f'[env]\n  BASE_URL = "https://{fly_app}.fly.dev"\n\n[http_service]', 1))

    req = target / "requirements.txt"  # keep the editable kit path valid wherever the project lives
    req.write_text(req.read_text().replace("-e ../launch-kit", f"-e {os.path.relpath(KIT, target) if target.parent == KIT.parent else KIT}"))

    env = (target / ".env.example").read_text()
    env = set_line(env, "BASE_URL", "http://localhost:8000")
    env = set_line(env, "DATABASE_URL", f"postgresql:///{slug}")
    env = set_line(env, "DB_SCHEMA", slug)
    env = env.replace(f"DB_SCHEMA={slug}\n", f"DB_SCHEMA={slug}\nAPP_ID={slug}\n", 1)
    notes = []
    shared = shared_values(sources)
    for k in SHARED:
        if k in shared:
            env = set_line(env, k, shared[k][0])
            notes.append(f"{k}: copied from {shared[k][1]}")
        elif k == "DATABASE_URL":
            notes.append(f"DATABASE_URL: sources only have a localhost placeholder; left as postgresql:///{slug}. "
                         "Paste the shared Supabase session-pooler URL before deploying.")
        else:
            notes.append(f"{k}: not found in {', '.join(map(str, sources))}; fill it in .env")
    envfile = target / ".env"
    envfile.write_text(env)
    envfile.chmod(0o600)
    return slug, fly_app, notes


def main(argv):
    ap = argparse.ArgumentParser(prog="python -m launchkit new")
    ap.add_argument("name")
    ap.add_argument("--dir")
    ap.add_argument("--price-cents", type=int, required=True)
    ap.add_argument("--interval", choices=("month", "year", "once"), required=True)
    ap.add_argument("--tagline", required=True)
    ap.add_argument("--fly-app")
    ap.add_argument("--venv", action="store_true", help="create .venv and install requirements")
    a = ap.parse_args(argv)
    target = Path(a.dir or f"~/Projects/{a.name}").expanduser().resolve()
    slug, fly_app, notes = scaffold(a.name, target, a.price_cents, a.interval, a.tagline, a.fly_app)
    print(f"Created {target} (DB_SCHEMA/APP_ID={slug}, lookup_key={slug}_{PLAN[a.interval]}, Fly app {fly_app})")
    for n in notes:
        print("  " + n)
    if a.venv:
        subprocess.run(["python3", "-m", "venv", ".venv"], cwd=target, check=True)
        subprocess.run([".venv/bin/pip", "install", "-q", "-r", "requirements.txt"], cwd=target, check=True)
    print(f"""
Next steps:
  cd {target}{'' if a.venv else '''
  python3 -m venv .venv && .venv/bin/pip install -r requirements.txt'''}
  PYTHONPATH=. .venv/bin/pytest -q                       # smoke test (local Postgres)
  set -a; . ./.env; set +a; .venv/bin/uvicorn app.main:app --port 8000
  stripe listen --api-key "$STRIPE_SECRET_KEY" --forward-to localhost:8000/stripe/webhook   # whsec_ -> .env
  ~/.fly/bin/flyctl apps create {fly_app}                 # then set secrets and deploy when ready
  python -m launchkit supabase-redirect --url https://{fly_app}.fly.dev""")
    return 0
