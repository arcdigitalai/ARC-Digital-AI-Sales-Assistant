"""Autopilot Phase A tests — webhooks, signing, events, safety fence."""
import json, os, sys

import pytest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import sales_app  # noqa: E402  (test DB configured by test_crm import order or env)


@pytest.fixture(scope="session")
def client():
    return sales_app.app.test_client()


@pytest.fixture(scope="session")
def admin(client):
    r = client.post("/api/auth/login", json={"email": "admin@salesai.com", "password": "Admin@123456"})
    return {"Authorization": "Bearer " + r.get_json()["data"]["token"]}


@pytest.fixture(scope="session")
def rep(client):
    client.post("/api/auth/register", json={"email": "rep2@salesai.com", "full_name": "Rep Two",
                                            "password": "Rep@123456", "role": "sales_rep"})
    r = client.post("/api/auth/login", json={"email": "rep2@salesai.com", "password": "Rep@123456"})
    return {"Authorization": "Bearer " + r.get_json()["data"]["token"]}


def data(r):
    j = r.get_json()
    assert j and j.get("ok"), f"API error: {j}"
    return j.get("data")


def test_webhook_signature_is_hmac_sha256():
    sig = sales_app.sign_webhook("topsecret", b'{"a":1}')
    assert sig.startswith("sha256=") and len(sig) == 71
    assert sig == sales_app.sign_webhook("topsecret", b'{"a":1}')          # deterministic
    assert sig != sales_app.sign_webhook("othersecret", b'{"a":1}')        # keyed


def test_webhook_crud_and_secret_shown_once(client, admin):
    d = data(client.post("/api/webhooks", headers=admin,
                         json={"url": "http://127.0.0.1:59999/hook", "events": "autopilot.enabled,draft.sent"}))
    assert len(d["secret"]) == 48
    wid = d["id"]
    lst = data(client.get("/api/webhooks", headers=admin))
    sub = [s for s in lst["subscriptions"] if s["id"] == wid][0]
    assert sub["secret"].endswith("…") and len(sub["secret"]) < 12   # masked on read
    assert client.post("/api/webhooks", headers=admin,
                       json={"url": "http://x/", "events": "nope.event"}).status_code == 400
    data(client.delete(f"/api/webhooks/{wid}", headers=admin))


def test_webhook_admin_only(client, rep):
    assert client.post("/api/webhooks", headers=rep,
                       json={"url": "http://127.0.0.1:1/x"}).status_code == 403
    assert client.post("/api/autopilot/pause", headers=rep, json={"paused": True}).status_code == 403


def test_autopilot_fence_blocks_demo_seed(client, admin):
    stripe = sales_app.q1("SELECT id FROM companies WHERE name='Stripe'")
    if not stripe:
        pytest.skip("seed company not present")
    r = client.post(f"/api/leads/{stripe['id']}/autopilot", headers=admin, json={"on": True})
    assert r.status_code == 403
    assert "demo seed" in r.get_json()["error"]


def test_autopilot_toggle_emits_event_and_delivery_row(client, admin):
    co = data(client.post("/api/companies", headers=admin, json={"name": "Autopilot Unit Pty"}))
    sub = data(client.post("/api/webhooks", headers=admin,
                           json={"url": "http://127.0.0.1:59998/nowhere", "events": "autopilot.enabled"}))
    d = data(client.post(f"/api/leads/{co['id']}/autopilot", headers=admin, json={"on": True}))
    assert d["autopilot"] == 1 and d["webhooks_notified"] >= 1
    row = sales_app.q1("""SELECT d.* FROM webhook_deliveries d
                          WHERE d.subscription_id=? ORDER BY d.id DESC LIMIT 1""", (sub["id"],))
    assert row and row["event"] == "autopilot.enabled"
    payload = json.loads(row["payload"])
    assert payload["data"]["company"] == "Autopilot Unit Pty"
    lead = data(client.get(f"/api/leads/{co['id']}", headers=admin))
    assert lead["company"]["autopilot"] == 1
    data(client.delete(f"/api/webhooks/{sub['id']}", headers=admin))


def test_pause_blocks_event_emission(client, admin):
    co = data(client.post("/api/companies", headers=admin, json={"name": "Paused Pty"}))
    sub = data(client.post("/api/webhooks", headers=admin,
                           json={"url": "http://127.0.0.1:59997/nowhere", "events": "*"}))
    data(client.post("/api/autopilot/pause", headers=admin, json={"paused": True}))
    n = sales_app.emit_crm_event("webhook.test", {"x": 1})
    assert n == 0
    data(client.post("/api/autopilot/pause", headers=admin, json={"paused": False}))
    n2 = sales_app.emit_crm_event("webhook.test", {"x": 1})
    assert n2 >= 1
    data(client.delete(f"/api/webhooks/{sub['id']}", headers=admin))


def test_delivery_retries_and_records_failure(client, admin):
    sub = data(client.post("/api/webhooks", headers=admin,
                           json={"url": "http://127.0.0.1:1/dead", "events": "webhook.test"}))
    r = data(client.post(f"/api/webhooks/{sub['id']}/test", headers=admin))
    assert r["status"] == "failed" and r["attempts"] == 3 and r["last_error"]
    data(client.delete(f"/api/webhooks/{sub['id']}", headers=admin))


def test_bulk_autopilot_respects_fence(client, admin):
    safe = data(client.post("/api/companies", headers=admin, json={"name": "Bulk AP Pty"}))
    stripe = sales_app.q1("SELECT id FROM companies WHERE name='Stripe'")
    ids = [safe["id"]] + ([stripe["id"]] if stripe else [])
    d = data(client.post("/api/leads/bulk", headers=admin, json={"ids": ids, "action": "autopilot_on"}))
    assert d["processed"] == 1
    if stripe:
        assert "Stripe" in d.get("skipped_demo_fence", [])
