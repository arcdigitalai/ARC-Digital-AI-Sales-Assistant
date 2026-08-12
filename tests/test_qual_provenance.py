"""AI-recommendation vs human-decision provenance.

Kevin asked why the AI recommendation didn't change when he set the
qualification. It MUST NOT: the spec requires storing the AI recommendation
and the human decision separately so the override is auditable. These tests
pin that contract, plus the ability to re-run the AI on demand.
"""
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


def test_human_decision_never_overwrites_ai_recommendation(client, admin):
    co = data(client.post("/api/companies", headers=admin,
                          json={"name": "Provenance Pty", "industry": "Plumbing"}))
    cid = co["id"]
    data(client.post(f"/api/leads/{cid}/qualification/ai-recommend", headers=admin))
    ai_before = data(client.get(f"/api/leads/{cid}/qualification", headers=admin))
    assert ai_before["ai_recommendation"] in ("qualified", "unqualified", "more_info")
    assert ai_before["ai_recommended_at"], "AI recommendation must be timestamped"

    # Human decides the OPPOSITE of whatever the AI said.
    opposite = "unqualified" if ai_before["ai_recommendation"] == "qualified" else "qualified"
    payload = {"result": opposite, "human_decision": "Spoke to the owner directly"}
    if opposite == "unqualified":
        payload["unqualified_reason"] = "Out of service area"
    data(client.post(f"/api/leads/{cid}/qualification", headers=admin, json=payload))

    after = data(client.get(f"/api/leads/{cid}/qualification", headers=admin))
    assert after["result"] == opposite                                   # human decision stored
    assert after["ai_recommendation"] == ai_before["ai_recommendation"]   # AI value untouched
    assert after["ai_reasoning"] == ai_before["ai_reasoning"]
    assert after["qualified_by"] and after["qualified_at"]


def test_ai_recommendation_can_be_rerun_and_restamped(client, admin):
    co = data(client.post("/api/companies", headers=admin, json={"name": "Rerun Pty"}))
    cid = co["id"]
    data(client.post(f"/api/leads/{cid}/qualification/ai-recommend", headers=admin))
    first = data(client.get(f"/api/leads/{cid}/qualification", headers=admin))
    # Enrich the lead, then re-run: the endpoint must accept a second call
    # (the UI previously hid the button after the first run).
    data(client.put(f"/api/companies/{cid}", headers=admin,
                    json={"employee_count": 14, "annual_revenue": 1500000, "industry": "Plumbing"}))
    sales_app.run("UPDATE lead_qualifications SET ai_recommended_at='2020-01-01 00:00:00' WHERE company_id=?", (cid,))
    data(client.post(f"/api/leads/{cid}/qualification/ai-recommend", headers=admin))
    second = data(client.get(f"/api/leads/{cid}/qualification", headers=admin))
    assert second["ai_recommended_at"] > "2020-01-02", "re-run must refresh the timestamp"
    assert second["ai_recommendation"] in ("qualified", "unqualified", "more_info")
    assert first["id"] == second["id"], "re-run updates in place, never duplicates the row"


def test_rerun_preserves_existing_human_decision(client, admin):
    co = data(client.post("/api/companies", headers=admin, json={"name": "Preserve Pty"}))
    cid = co["id"]
    data(client.post(f"/api/leads/{cid}/qualification", headers=admin,
                     json={"result": "qualified", "human_decision": "Confirmed on call",
                           "human_notes": "Budget approved"}))
    data(client.post(f"/api/leads/{cid}/qualification/ai-recommend", headers=admin))
    after = data(client.get(f"/api/leads/{cid}/qualification", headers=admin))
    assert after["result"] == "qualified"                 # re-running AI must not wipe the decision
    assert after["human_decision"] == "Confirmed on call"
    assert after["human_notes"] == "Budget approved"


def test_consumer_enquiry_gets_not_applicable_not_unqualified(client, admin):
    """Jake's console leads are homeowners, not B2B prospects — the business
    qualifier must say so instead of scoring them 'unqualified' forever."""
    co = data(client.post("/api/companies", headers=admin,
                          json={"name": "Homeowner Lead", "industry": "Plumbing Lead",
                                "description": "[Jakes JP-999999] | issue: Burst pipe | suburb: Subiaco"}))
    d = data(client.post(f"/api/leads/{co['id']}/qualification/ai-recommend", headers=admin))
    q = d["qualification"]
    assert q["ai_recommendation"] == "not_applicable"
    assert "not a B2B prospect" in q["ai_reasoning"]
    assert q["ai_confidence"] == "n/a"
    # A real business must still be scored normally.
    biz = data(client.post("/api/companies", headers=admin,
                           json={"name": "Real Plumbing Biz", "industry": "Plumbing",
                                 "employee_count": 14, "annual_revenue": 1600000}))
    d2 = data(client.post(f"/api/leads/{biz['id']}/qualification/ai-recommend", headers=admin))
    assert d2["qualification"]["ai_recommendation"] in ("qualified", "more_info", "unqualified")
    assert d2["qualification"]["ai_recommendation"] != "not_applicable"


def test_consumer_lead_can_still_be_human_qualified(client, admin):
    co = data(client.post("/api/companies", headers=admin,
                          json={"name": "Homeowner Qualifiable", "industry": "Plumbing Lead",
                                "description": "[Jakes JP-888888] | issue: Blocked drain"}))
    data(client.post(f"/api/leads/{co['id']}/qualification/ai-recommend", headers=admin))
    d = data(client.post(f"/api/leads/{co['id']}/qualification", headers=admin,
                         json={"result": "qualified", "human_decision": "Caller also runs a trade business"}))
    assert d["result"] == "qualified"
    assert d["ai_recommendation"] == "not_applicable"   # AI opinion preserved, not overwritten
