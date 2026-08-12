"""Release 2+3 tests — discovery, ROI, quotation versions, closed-won
workflow, clients, global search, read-only gate."""
import os, sys

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


def make_opp(client, admin, name):
    co = data(client.post("/api/companies", headers=admin, json={"name": name}))
    d = data(client.post(f"/api/leads/{co['id']}/convert", headers=admin,
                         json={"force": True, "opportunity": {"title": name + " Opp", "quoted_value": 10000,
                                                              "monthly_recurring": 500}}))
    return co["id"], d["opportunity"]["id"]


# ── Discovery ────────────────────────────────────────────────────────────────
def test_discovery_draft_then_complete(client, admin):
    _, oid = make_opp(client, admin, "Disco Pty")
    d = data(client.put(f"/api/opportunities/{oid}/discovery", headers=admin,
                        json={"process_name": "Quote follow-up", "manual_hours_week": "12"}))
    assert d["status"] == "draft" and d["process_name"] == "Quote follow-up"
    d2 = data(client.put(f"/api/opportunities/{oid}/discovery", headers=admin,
                         json={"budget": "$10k", "complete": True}))
    assert d2["status"] == "completed" and d2["completed_at"]
    assert d2["process_name"] == "Quote follow-up"      # draft fields kept


# ── ROI: formulas per spec, snapshots immutable ──────────────────────────────
def test_roi_formulas_exact():
    c = sales_app.compute_roi({"employees": 3, "hours_week": 10, "hourly_cost": 50,
                               "automation_pct": 40, "error_cost": 5000, "error_reduction_pct": 50,
                               "software_cost": 4000, "software_reduction_pct": 25,
                               "implementation_cost": 10000, "support_cost_annual": 2000})
    assert c["labour_cost_annual"] == 78000.0            # 3×10×50×52
    assert c["labour_saving"] == 31200.0                 # ×40%
    assert c["error_saving"] == 2500.0
    assert c["software_saving"] == 1000.0
    assert c["total_annual_saving"] == 34700.0
    assert c["first_year_net"] == 22700.0                # − impl − support
    assert c["first_year_roi_pct"] == 227.0
    assert round(c["payback_months"], 2) == 3.46         # 10000 / (34700/12)
    assert c["three_year_net"] == 88100.0                # 3×34700 − 10000 − 3×2000


def test_roi_snapshot_create_and_immutability(client, admin):
    _, oid = make_opp(client, admin, "ROI Pty")
    s = data(client.post(f"/api/opportunities/{oid}/roi", headers=admin,
                         json={"name": "Base case", "employees": 2, "hours_week": 5, "hourly_cost": 60,
                               "automation_pct": 50, "implementation_cost": 8000}))
    assert s["name"] == "Base case" and s["labour_cost_annual"] == 31200.0
    assert client.put(f"/api/roi/{s['id']}", headers=admin, json={"employees": 99}).status_code == 405
    # referenced by a quotation -> delete blocked
    data(client.post("/api/quotations", headers=admin,
                     json={"opportunity_id": oid, "amount": 8000, "roi_snapshot_id": s["id"]}))
    assert client.delete(f"/api/roi/{s['id']}", headers=admin).status_code == 409


# ── Quotations: versioning + statuses ────────────────────────────────────────
def test_quotation_versioning_and_status(client, admin):
    _, oid = make_opp(client, admin, "QuoteVer Pty")
    qt = data(client.post("/api/quotations", headers=admin,
                          json={"opportunity_id": oid, "amount": 9000}))["quotation"]
    assert qt["version"] == 1
    v2 = data(client.post(f"/api/quotations/{qt['id']}/new-version", headers=admin,
                          json={"amount": 8500}))
    assert v2["version"] == 2 and v2["status"] == "draft" and v2["amount"] == 8500
    old = sales_app.q1("SELECT * FROM quotations WHERE id=?", (qt["id"],))
    assert old["status"] == "superseded" and old["superseded_by"] == v2["id"]
    assert client.post(f"/api/quotations/{v2['id']}/status", headers=admin,
                       json={"status": "flying"}).status_code == 400
    data(client.post(f"/api/quotations/{v2['id']}/status", headers=admin, json={"status": "sent"}))
    acc = data(client.post(f"/api/quotations/{v2['id']}/status", headers=admin, json={"status": "accepted"}))
    assert acc["accepted_at"]
    op = sales_app.q1("SELECT accepted_value FROM opportunities WHERE id=?", (oid,))
    assert op["accepted_value"] == 8500


# ── Closed-won workflow: handover + profile + onboarding + review ────────────
def test_closed_won_creates_handover_profile_and_tasks(client, admin):
    cid, oid = make_opp(client, admin, "WonFlow Pty")
    data(client.post(f"/api/opportunities/{oid}/stage", headers=admin,
                     json={"stage": "closed_won", "final_value": 10000, "won_reason": "test"}))
    ho = data(client.get(f"/api/opportunities/{oid}/handover", headers=admin))
    assert ho and ho["final_value"] == 10000
    clients_ = data(client.get("/api/clients", headers=admin))
    me = [c for c in clients_ if c["id"] == cid]
    assert me and me[0]["client_status"] == "active" and me[0]["mrr"] == 500
    tasks = data(client.get(f"/api/activities?opportunity_id={oid}", headers=admin))
    onboarding = [t for t in tasks if t["subject"].startswith("Onboarding:")]
    assert len(onboarding) == 11
    assert any(t["type"] == "client_review" for t in tasks)          # 30-day review
    # editable handover
    upd = data(client.put(f"/api/opportunities/{oid}/handover", headers=admin,
                          json={"hosting_model": "self-hosted", "deposit_required": 1}))
    assert upd["hosting_model"] == "self-hosted"


def test_client_profile_update_and_validation(client, admin):
    cid, oid = make_opp(client, admin, "ClientProf Pty")
    data(client.post(f"/api/opportunities/{oid}/stage", headers=admin,
                     json={"stage": "closed_won", "final_value": 5000}))
    assert client.put(f"/api/clients/{cid}/profile", headers=admin,
                      json={"client_health": "great"}).status_code == 400
    p = data(client.put(f"/api/clients/{cid}/profile", headers=admin,
                        json={"client_health": "at_risk", "satisfaction_score": 6}))
    assert p["client_health"] == "at_risk"
    log = data(client.get(f"/api/crm/audit-log?record_type=company&record_id={cid}", headers=admin))
    assert any(a["action"] == "client_health" for a in log)


# ── Global search ────────────────────────────────────────────────────────────
def test_global_search_groups(client, admin):
    data(client.post("/api/companies", headers=admin, json={"name": "Searchable Unicorn Pty"}))
    r = data(client.get("/api/search?q=Unicorn", headers=admin))
    names = {gp["name"] for gp in r["groups"]}
    assert "Leads / Companies" in names
    assert any(it["title"] == "Searchable Unicorn Pty" for gp in r["groups"] for it in gp["items"])
    assert data(client.get("/api/search?q=a", headers=admin))["groups"] == []   # too short


# ── Read-only role gate ──────────────────────────────────────────────────────
def test_readonly_role_blocked_from_writes(client, admin):
    client.post("/api/auth/register", json={"email": "ro@salesai.com", "full_name": "Read Only",
                                            "password": "Ro@123456", "role": "readonly"})
    r = client.post("/api/auth/login", json={"email": "ro@salesai.com", "password": "Ro@123456"})
    ro = {"Authorization": "Bearer " + r.get_json()["data"]["token"]}
    assert client.get("/api/leads", headers=ro).status_code == 200               # can view
    assert client.post("/api/companies", headers=ro, json={"name": "Nope"}).status_code == 403
    assert client.put("/api/leads/1", headers=ro, json={"crm_status": "contacted"}).status_code == 403
    assert client.delete("/api/webhooks/999", headers=ro).status_code == 403
