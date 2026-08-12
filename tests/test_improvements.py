"""Improvement-pass tests — cadence engine, reply-check guards, quotation
email send, buying roles."""
import os, sys
from datetime import datetime, timedelta

import pytest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import sales_app  # noqa: E402


@pytest.fixture(scope="session")
def client():
    return sales_app.app.test_client()


@pytest.fixture(scope="session")
def admin(client):
    r = client.post("/api/auth/login", json={"email": "kevin@salesai.com", "password": "adminkevin@123456"})
    return {"Authorization": "Bearer " + r.get_json()["data"]["token"]}


def data(r):
    j = r.get_json()
    assert j and j.get("ok"), f"API error: {j}"
    return j.get("data")


def _mk_autopilot_lead(client, admin, name, email):
    co = data(client.post("/api/companies", headers=admin, json={"name": name}))
    ct = data(client.post("/api/contacts", headers=admin,
                          json={"company_id": co["id"], "first_name": "Pat", "email": email,
                                "is_decision_maker": True}))
    sales_app.run("UPDATE companies SET autopilot=1 WHERE id=?", (co["id"],))
    return co["id"], ct["id"]


def test_reply_check_skips_without_google(client, admin):
    r = sales_app.check_email_replies()      # test env has no Google token
    assert r.get("skipped") == "google_not_connected"


def test_cadence_drafts_day3_followup_once(client, admin):
    cid, ctid = _mk_autopilot_lead(client, admin, "Cadence Pty", "pat@cadencepty.com")
    old = (datetime.utcnow() - timedelta(days=4)).strftime("%Y-%m-%d %H:%M:%S")
    sales_app.run("""INSERT INTO emails(company_id,contact_id,email_type,subject,body,
                     recipient_email,status,sent_at) VALUES(?,?,?,?,?,?,?,?)""",
                  (cid, ctid, "cold", "Intro", "hi", "pat@cadencepty.com", "sent", old))
    with sales_app.app.test_request_context():
        r1 = sales_app.cadence_tick()
    assert r1["drafted"] >= 1
    drafts = sales_app.q("SELECT * FROM email_drafts WHERE company_id=? AND draft_type='cadence_d3'", (cid,))
    assert len(drafts) == 1 and drafts[0]["status"] == "pending_approval"
    with sales_app.app.test_request_context():
        r2 = sales_app.cadence_tick()        # idempotent — no duplicate draft
    drafts2 = sales_app.q("SELECT * FROM email_drafts WHERE company_id=? AND draft_type='cadence_d3'", (cid,))
    assert len(drafts2) == 1


def test_cancel_cadence_stops_pending_drafts(client, admin):
    cid, ctid = _mk_autopilot_lead(client, admin, "CancelCad Pty", "pat@cancelcad.com")
    sales_app.run("""INSERT INTO email_drafts(company_id,contact_id,draft_type,subject,body,
                     recipient_email,status) VALUES(?,?,?,?,?,?,?)""",
                  (cid, ctid, "cadence_d3", "s", "b", "pat@cancelcad.com", "pending_approval"))
    sales_app._cancel_cadence(cid, "test")
    d = sales_app.q1("SELECT status FROM email_drafts WHERE company_id=? AND draft_type='cadence_d3'", (cid,))
    assert d["status"] == "cancelled"


def test_cadence_ignores_replied_companies(client, admin):
    cid, ctid = _mk_autopilot_lead(client, admin, "Replied Pty", "pat@repliedpty.com")
    old = (datetime.utcnow() - timedelta(days=5)).strftime("%Y-%m-%d %H:%M:%S")
    sales_app.run("""INSERT INTO emails(company_id,contact_id,email_type,subject,body,
                     recipient_email,status,sent_at) VALUES(?,?,?,?,?,?,?,?)""",
                  (cid, ctid, "cold", "Intro", "hi", "pat@repliedpty.com", "replied", old))
    with sales_app.app.test_request_context():
        sales_app.cadence_tick()
    assert not sales_app.q("SELECT id FROM email_drafts WHERE company_id=?", (cid,))


def test_quotation_send_validation(client, admin):
    co = data(client.post("/api/companies", headers=admin, json={"name": "QuoteSend Pty"}))
    d = data(client.post(f"/api/leads/{co['id']}/convert", headers=admin,
                         json={"force": True, "opportunity": {"title": "QS Opp", "quoted_value": 4000}}))
    qt = data(client.post("/api/quotations", headers=admin,
                          json={"opportunity_id": d["opportunity"]["id"], "amount": 4000}))["quotation"]
    # no contact email and no 'to' -> 400
    assert client.post(f"/api/quotations/{qt['id']}/send", headers=admin, json={}).status_code == 400
    # with a recipient but no email backends configured in tests -> clean error, status unchanged
    r = client.post(f"/api/quotations/{qt['id']}/send", headers=admin, json={"to": "someone@example.com"})
    assert r.status_code == 400 and "Send failed" in r.get_json()["error"]
    assert sales_app.q1("SELECT status FROM quotations WHERE id=?", (qt["id"],))["status"] == "draft"


def test_buying_roles_endpoint(client, admin):
    co = data(client.post("/api/companies", headers=admin, json={"name": "Roles Pty"}))
    ct = data(client.post("/api/contacts", headers=admin,
                          json={"company_id": co["id"], "first_name": "Ro"}))
    r = data(client.post(f"/api/contacts/{ct['id']}/buying-roles", headers=admin,
                         json={"roles": ["decision_maker", "champion", "bogus_role"]}))
    assert set(r["roles"]) == {"decision_maker", "champion"}      # bogus filtered
    assert sales_app.q1("SELECT is_decision_maker FROM contacts WHERE id=?", (ct["id"],))["is_decision_maker"] == 1
    lead = data(client.get(f"/api/leads/{co['id']}", headers=admin))
    assert set(lead["contacts"][0]["buying_roles"]) == {"decision_maker", "champion"}
