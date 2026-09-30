import json
import sys

import pytest
import stripe

from launchkit import check, golive, new, supabase_redirect


def test_supabase_merge_keeps_entries_and_is_idempotent():
    assert supabase_redirect.merge(None, "https://a.dev/auth/callback") == ("https://a.dev/auth/callback", True)
    merged, changed = supabase_redirect.merge("http://localhost:8000/**, https://b.dev/auth/callback", "https://a.dev/auth/callback")
    assert changed and merged == "http://localhost:8000/**,https://b.dev/auth/callback,https://a.dev/auth/callback"
    assert supabase_redirect.merge(merged, "https://b.dev/auth/callback") == (merged, False)


def test_sign_matches_stripe_verification():
    body = json.dumps({"id": "evt_1", "object": "event", "type": "x", "data": {"object": {}}}).encode()
    assert stripe.Webhook.construct_event(body, check.sign(body, "whsec_abc"), "whsec_abc").id == "evt_1"
    with pytest.raises(stripe.error.SignatureVerificationError):
        stripe.Webhook.construct_event(body, check.sign(body, "whsec_forged"), "whsec_abc")


def test_check_robots():
    good = "\n".join(f"User-agent: {b}\nAllow: /" for b in check.BOTS) + "\nSitemap: https://x.dev/sitemap.xml\n"
    assert all(ok for _, ok, _ in check.check_robots(good))
    rows = check.check_robots("User-agent: *\nAllow: /\n")
    assert [ok for _, ok, _ in rows] == [False, False] and "Claude-SearchBot" in rows[0][2]


def test_check_sitemap():
    xml = ('<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>https://x.dev/</loc>'
           '<lastmod>{}</lastmod></url><url><loc>https://x.dev/a</loc><lastmod>2026-09-01</lastmod></url></urlset>')
    assert all(ok for _, ok, _ in check.check_sitemap(xml.format("2026-09-29"), "2026-09-29"))
    assert not check.check_sitemap(xml.replace("2026-09-01", "2026-09-29T10:00:00Z").format("2026-09-29"), "2026-09-29")[1][1]
    assert not check.check_sitemap("<urlset>", "2026-09-29")[0][1]


def test_check_sitemap_today_in_any_timezone():
    def xml(*mods):
        return ('<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">' + "".join(
            f"<url><loc>https://x.dev/{i}</loc><lastmod>{m}</lastmod></url>" for i, m in enumerate(mods)) + "</urlset>")
    today = "2026-09-30"  # UTC
    stale = [
        ("2026-09-30", "2026-09-30"),                                 # date-only, UTC today
        ("2026-09-29", "2026-09-29"),                                 # date-only, US local today (UTC already next day)
        ("2026-10-01", "2026-10-01"),                                 # date-only, Asia local today
        ("2026-09-30T07:44:00Z", "2026-09-30T07:44:00+00:00"),        # full timestamp, UTC
        ("2026-09-29T23:30:00-08:00", "2026-09-29T21:00:00-07:00"),   # full timestamp, US local
        ("2026-09-30T00:10:00+09:00", "2026-09-30T08:00:00+09:00"),   # full timestamp, Asia local
    ]
    for mods in stale:
        assert not check.check_sitemap(xml(*mods), today)[1][1], mods
    assert not check.check_sitemap(xml("2026-09-29", "2026-09-29"), check.datetime.date(2026, 9, 30))[1][1]
    assert check.check_sitemap(xml("2026-09-29", "2026-08-15T10:00:00-07:00"), today)[1][1]
    assert check.check_sitemap(xml("2026-09-30", "2026-09-01"), today)[1][1]


def test_check_home():
    page = ('<html><head><title>{}</title><meta name="description" content="Find permits">'
            '<link rel="canonical" href="https://x.dev/"></head><body><h1>Hi</h1>{}</body></html>')
    assert all(ok for _, ok, _ in check.check_home(page.format("PermitGap: permit gaps for LA listings", ""), "permitgap.fly.dev"))
    assert not check.check_home(page.format("PermitGap", ""), "permitgap.fly.dev")[0][1]
    assert not check.check_home(page.format("Lowball", "pk_test_123"), "lowball.rent", expect_live=True)[-1][1]
    assert [ok for _, ok, _ in check.check_home("<title>Other page</title>", "x.dev")] == [True, False, False, False]


def test_golive_discovers_events_and_prices(tmp_path, monkeypatch):
    (tmp_path / "fakeapp.py").write_text(
        "from launchkit import payments, webhooks\n"
        "KEY = 'fake_pro'\nPRODUCT = {'cents': 1500}\n"
        "def boot():\n    payments.ensure_price(KEY, 'Fake', PRODUCT['cents'], interval='year')\n"
        "@webhooks.on('invoice.paid')\ndef paid(obj, event): pass\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    events = golive.app_events("fakeapp")
    assert "invoice.paid" in events and "checkout.session.completed" in events and events == sorted(events)
    assert golive.app_prices("fakeapp") == [("fake_pro", 1500, "year")]
    sys.modules.pop("fakeapp")


def test_golive_refuses_test_key(tmp_path, monkeypatch, capsys):
    (tmp_path / "fakeapp2.py").write_text("x = 1\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    key = tmp_path / "key.txt"
    key.write_text("sk_test_123")
    args = ["--app-module", "fakeapp2", "--fly-app", "f", "--url", "https://f.dev", "--key-file", str(key)]
    assert golive.main(args) == 1
    assert golive.main(args + ["--dry-run"]) == 0
    assert "not a live key" in capsys.readouterr().out
    sys.modules.pop("fakeapp2")


def test_new_scaffolds_project(tmp_path):
    src = tmp_path / "src.env"
    src.write_text("STRIPE_SECRET_KEY=sk_test_shared\nDATABASE_URL=postgresql:///local\n"
                   "SUPABASE_URL=https://wurikcpajrkvmnohungl.supabase.co\nSUPABASE_ANON_KEY=sb_publishable_x\n")
    slug, fly_app, notes = new.scaffold("Rent Radar", tmp_path / "RentRadar", 2900, "once", 'Say "hi"', sources=[src])
    assert (slug, fly_app) == ("rent_radar", "rent-radar")
    main = (tmp_path / "RentRadar/app/main.py").read_text()
    assert 'APP_NAME = "Rent Radar"' in main and 'TAGLINE = "Say \\"hi\\""' in main
    assert ('PRODUCT = {"lookup_key": "rent_radar_pro", "name": "Rent Radar Pro", "unit_amount": 2900, '
            '"interval": None}') in main
    env = new.read_env(tmp_path / "RentRadar/.env")
    assert env["DB_SCHEMA"] == env["APP_ID"] == "rent_radar" and env["STRIPE_SECRET_KEY"] == "sk_test_shared"
    assert env["DATABASE_URL"] == "postgresql:///rent_radar" and env["SUPABASE_ANON_KEY"] == "sb_publishable_x"
    assert any("localhost placeholder" in n for n in notes)
    assert 'app = "rent-radar"' in (tmp_path / "RentRadar/fly.toml").read_text()
    assert not (tmp_path / "RentRadar/.venv").exists()
    with pytest.raises(SystemExit):
        new.scaffold("Rent Radar", tmp_path / "RentRadar", 2900, "once", "x", sources=[src])


def test_new_never_copies_a_live_key(tmp_path):
    src = tmp_path / "src.env"
    src.write_text("STRIPE_SECRET_KEY=sk_live_nope\n")
    assert "STRIPE_SECRET_KEY" not in new.shared_values([src])
