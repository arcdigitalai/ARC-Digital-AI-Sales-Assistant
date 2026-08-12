"""Release 1 CRM tests — run with:  .venv\\Scripts\\python.exe -m pytest tests -q

Uses a throwaway SQLite DB per session; never touches data/sales.db.
Groq/Twilio/Bland are not called: no keys are set in the test environment
(the code paths fall back deterministically).
"""
import os, sys, json

import pytest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

# DB_PATH + credential stripping happen in conftest.py, which pytest loads
# before ANY test module — import order between test files no longer matters.
import sales_app  # noqa: E402  (import runs init_db + init_crm_db on the temp DB)


@pytest.fixture(scope="session")
def client():
    return sales_app.app.test_client()


@pytest.fixture(scope="session")
def admin(client):
    r = client.post("/api/auth/login", json={"email": "kevin@salesai.com", "password": "adminkevin@123456"})
    tok = r.get_json()["data"]["token"]
    return {"Authorization": "Bearer " + tok}


@pytest.fixture(scope="session")
def rep(client):
    client.post("/api/auth/register", json={"email": "rep@salesai.com", "full_name": "Test Rep",
                                            "password": "Rep@123456", "role": "sales_rep"})
    r = client.post("/api/auth/login", json={"email": "rep@salesai.com", "password": "Rep@123456"})
    tok = r.get_json()["data"]["token"]
    return {"Authorization": "Bearer " + tok}


def data(r):
    j = r.get_json()
    assert j and j.get("ok"), f"API error: {j}"
    return j.get("data")


def make_lead(client, admin, name, **kw):
    co = data(client.post("/api/companies", headers=admin, json={"name": name, **kw}))
    return co["id"]


# ── Existing scoring engine ──────────────────────────────────────────────────
def test_score_company_thresholds():
    hot = sales_app.score_company({"annual_revenue": 500_000_000, "employee_count": 2000, "industry": "SaaS"},
                                  [{"seniority_level": "c_suite", "is_decision_maker": 1}],
                                  [{"strength": 9}, {"strength": 8}])
    assert hot["total_score"] >= 70 and hot["tier"] == "hot"
    cold = sales_app.score_company({"annual_revenue": 0, "employee_count": 1, "industry": ""}, [], [])
    assert cold["tier"] in ("cold", "warm")
    assert 0 <= cold["total_score"] <= 100


def test_crm_tier_uses_settings():
    assert sales_app.crm_tier(0) == "unscored"
    assert sales_app.crm_tier(70) == "hot"
    assert sales_app.crm_tier(69) == "warm"
    assert sales_app.crm_tier(39) == "cold"


# ── Leads workspace API ──────────────────────────────────────────────────────
def test_leads_list_counts_and_pagination(client, admin):
    d = data(client.get("/api/leads?per_page=5", headers=admin))
    assert d["total"] >= 10          # seed data
    assert len(d["items"]) == 5
    assert d["pages"] >= 2
    for key in ("hot", "warm", "cold", "unscored", "qualified", "decision_makers"):
        assert key in d["counts"]
    d2 = data(client.get("/api/leads?per_page=5&page=2", headers=admin))
    assert d2["items"][0]["id"] != d["items"][0]["id"]


def test_leads_search_and_tier_filter(client, admin):
    d = data(client.get("/api/leads?search=Stripe", headers=admin))
    assert any(r["name"] == "Stripe" for r in d["items"])
    hot = data(client.get("/api/leads?tier=hot", headers=admin))
    for r in hot["items"]:
        assert (r["eff_score"] or 0) >= 70


def test_leads_export_csv(client, admin):
    r = client.get("/api/leads/export", headers=admin)
    assert r.status_code == 200
    assert r.headers["Content-Type"].startswith("text/csv")
    assert b"Company" in r.data and b"Stripe" in r.data


def test_lead_update_and_audit(client, admin):
    cid = make_lead(client, admin, "Audit Lead Pty Ltd")
    data(client.put(f"/api/leads/{cid}", headers=admin,
                    json={"crm_status": "contacted", "lead_source": "referral"}))
    d = data(client.get(f"/api/leads/{cid}", headers=admin))
    assert d["company"]["crm_status"] == "contacted"
    log = data(client.get(f"/api/crm/audit-log?record_type=lead&record_id={cid}", headers=admin))
    assert any(a["field"] == "crm_status" and a["new_value"] == "contacted" for a in log)


def test_lead_update_rejects_bad_status(client, admin):
    cid = make_lead(client, admin, "Bad Status Lead")
    r = client.put(f"/api/leads/{cid}", headers=admin, json={"crm_status": "nonsense"})
    assert r.status_code == 400


# ── AI scoring + human override ──────────────────────────────────────────────
def test_ai_score_breakdown(client, admin):
    cid = make_lead(client, admin, "Score Me Pty", annual_revenue=5_000_000, employee_count=50, industry="SaaS")
    d = data(client.post(f"/api/leads/{cid}/ai-score", headers=admin))
    assert 0 <= d["total_score"] <= 100
    assert isinstance(d["positive_indicators"], list)
    assert isinstance(d["missing_info"], list)
    assert d["recommended_action"]


def test_score_override_and_clear(client, admin):
    cid = make_lead(client, admin, "Override Pty", annual_revenue=100_000, employee_count=3)
    data(client.post(f"/api/leads/{cid}/ai-score", headers=admin))
    r = client.post(f"/api/leads/{cid}/score-override", headers=admin, json={"score": 95})
    assert r.status_code == 400          # reason required
    d = data(client.post(f"/api/leads/{cid}/score-override", headers=admin,
                         json={"score": 95, "reason": "Owner confirmed budget"}))
    assert d["override_score"] == 95 and d["tier"] == "hot"
    det = data(client.get(f"/api/leads/{cid}", headers=admin))
    assert det["score"]["override_score"] == 95
    assert det["company"]["lead_score"] == 95
    log = data(client.get(f"/api/crm/audit-log?record_type=lead&record_id={cid}", headers=admin))
    assert any(a["action"] == "score_override" for a in log)
    data(client.post(f"/api/leads/{cid}/score-override", headers=admin, json={"clear": True}))
    det2 = data(client.get(f"/api/leads/{cid}", headers=admin))
    assert det2["score"]["override_score"] is None


# ── Qualification: AI recommends, human decides ──────────────────────────────
def test_qualification_requires_human_decision(client, admin):
    cid = make_lead(client, admin, "Qualify Guard Pty")
    r = client.post(f"/api/leads/{cid}/qualification", headers=admin, json={"result": "qualified"})
    assert r.status_code == 400          # human_decision missing
    r = client.post(f"/api/leads/{cid}/qualification", headers=admin, json={"result": "unqualified"})
    assert r.status_code == 400          # unqualified_reason missing


def test_qualification_full_flow(client, admin):
    cid = make_lead(client, admin, "Qualify Flow Pty", industry="Plumbing", employee_count=10)
    data(client.post(f"/api/leads/{cid}/qualification/ai-recommend", headers=admin))
    q = data(client.get(f"/api/leads/{cid}/qualification", headers=admin))
    assert q["ai_recommendation"] in ("qualified", "unqualified", "more_info")
    d = data(client.post(f"/api/leads/{cid}/qualification", headers=admin, json={
        "result": "qualified", "human_decision": "Confirmed on call",
        "need_problem": "Missed calls", "comm_budget_known": 1,
        "authority_flags": {"decision_maker": True}}))
    assert d["result"] == "qualified" and d["qualified_by"]
    det = data(client.get(f"/api/leads/{cid}", headers=admin))
    assert det["company"]["status"] == "qualified"
    assert det["company"]["crm_status"] == "qualified"


# ── Conversion + duplicate detection ─────────────────────────────────────────
def test_convert_requires_qualification_unless_forced(client, admin):
    cid = make_lead(client, admin, "Unqualified Convert Pty")
    r = client.post(f"/api/leads/{cid}/convert", headers=admin, json={})
    assert r.status_code == 409
    d = data(client.post(f"/api/leads/{cid}/convert", headers=admin,
                         json={"force": True, "opportunity": {"title": "Forced Opp", "quoted_value": 1000}}))
    assert d["opportunity"]["stage"] == "new"
    assert d["company"]["crm_status"] == "converted"


def test_convert_creates_opp_history_and_task(client, admin):
    cid = make_lead(client, admin, "Convert Full Pty")
    client.post("/api/contacts", headers=admin,
                json={"company_id": cid, "first_name": "Dana", "email": "dana@convertfull.com",
                      "phone": "+61411000222", "is_decision_maker": True})
    data(client.post(f"/api/leads/{cid}/qualification", headers=admin,
                     json={"result": "qualified", "human_decision": "yes"}))
    d = data(client.post(f"/api/leads/{cid}/convert", headers=admin, json={
        "opportunity": {"title": "Full Opp", "quoted_value": 8000, "service_category": "ai_assistant"}}))
    oid = d["opportunity"]["id"]
    det = data(client.get(f"/api/opportunities/{oid}/detail", headers=admin))
    assert det["stage_history"][-1]["to_stage"] == "new"
    assert det["contact"]["email"] == "dana@convertfull.com"
    acts = data(client.get(f"/api/activities?opportunity_id={oid}", headers=admin))
    assert len(acts) == 1 and acts[0]["type"] == "task"
    # original lead retained
    lead = data(client.get(f"/api/leads/{cid}", headers=admin))
    assert lead["company"]["crm_status"] == "converted"


def test_duplicate_detection(client, admin):
    make_lead(client, admin, "Dupe Detect Pty", website="dupedetect.com.au")
    cid2 = make_lead(client, admin, "Dupe Detect Company")
    client.post("/api/contacts", headers=admin,
                json={"company_id": cid2, "first_name": "Sam", "email": "sam@dupedetect.com.au"})
    p = data(client.get(f"/api/leads/{cid2}/convert-preview", headers=admin))
    reasons = [c["match_reason"] for c in p["duplicates"]["companies"]]
    assert any("name" in r.lower() or "domain" in r.lower() for r in reasons)


def test_import_promote_dedupes_on_email(client, admin):
    # imported lead whose email matches an existing contact must reuse it
    cid = make_lead(client, admin, "Promote Target Pty", website="promotetarget.com")
    ct = data(client.post("/api/contacts", headers=admin,
              json={"company_id": cid, "first_name": "Existing", "email": "boss@promotetarget.com"}))
    csv_body = ("First Name,Last Name,Email,Company,Website\n"
                "Existing,Boss,boss@promotetarget.com,Promote Target Pty,promotetarget.com\n")
    import io as _io
    r = client.post("/api/leads-import/upload-csv", headers=admin,
                    data={"file": (_io.BytesIO(csv_body.encode()), "t.csv")},
                    content_type="multipart/form-data")
    assert r.status_code == 201
    imp = data(client.get("/api/leads-import", headers=admin))
    row = [x for x in imp if x["email"] == "boss@promotetarget.com"][0]
    d = data(client.post(f"/api/leads-import/{row['id']}/promote", headers=admin, json={}))
    assert d["company_id"] == cid
    assert d["contact_id"] == ct["id"]      # reused, not duplicated


# ── Opportunities: weighted value, stage machine, validation ─────────────────
def test_weighted_value_formula(client, admin):
    cid = make_lead(client, admin, "Weighted Pty")
    d = data(client.post(f"/api/leads/{cid}/convert", headers=admin,
                         json={"force": True, "opportunity": {"title": "W", "quoted_value": 10000}}))
    oid = d["opportunity"]["id"]
    upd = data(client.put(f"/api/opportunities/{oid}/crm", headers=admin, json={"probability": 60}))
    assert upd["weighted_value"] == 6000.0


def test_stage_validation(client, admin):
    cid = make_lead(client, admin, "Stage Pty")
    d = data(client.post(f"/api/leads/{cid}/convert", headers=admin,
                         json={"force": True, "opportunity": {"title": "S", "quoted_value": 5000}}))
    oid = d["opportunity"]["id"]
    assert client.post(f"/api/opportunities/{oid}/stage", headers=admin,
                       json={"stage": "flying"}).status_code == 400
    assert client.post(f"/api/opportunities/{oid}/stage", headers=admin,
                       json={"stage": "closed_lost"}).status_code == 422    # no lost_reason
    st = data(client.post(f"/api/opportunities/{oid}/stage", headers=admin,
                          json={"stage": "negotiation"}))
    assert st["stage"] == "negotiation" and st["probability"] == 80
    st2 = data(client.post(f"/api/opportunities/{oid}/stage", headers=admin,
                           json={"stage": "closed_won", "final_value": 5000, "won_reason": "verbal ok"}))
    assert st2["stage"] == "closed_won" and st2["status"] == "won" and st2["closed_at"]
    det = data(client.get(f"/api/opportunities/{oid}/detail", headers=admin))
    stages = [h["to_stage"] for h in det["stage_history"]]
    assert stages[0] == "closed_won" and "negotiation" in stages
    # closed-won marks the company a client
    lead = data(client.get(f"/api/leads/{cid}", headers=admin))
    assert lead["company"]["relationship_status"] == "client"
    assert lead["company"]["client_since"]


def test_closed_won_requires_value_or_quote(client, admin):
    cid = make_lead(client, admin, "Won Guard Pty")
    d = data(client.post(f"/api/leads/{cid}/convert", headers=admin,
                         json={"force": True, "opportunity": {"title": "WG", "quoted_value": 0}}))
    oid = d["opportunity"]["id"]
    assert client.post(f"/api/opportunities/{oid}/stage", headers=admin,
                       json={"stage": "closed_won"}).status_code == 422


# ── Activities ───────────────────────────────────────────────────────────────
def test_activity_create_complete_followup(client, admin):
    cid = make_lead(client, admin, "Activity Pty")
    a = data(client.post("/api/activities", headers=admin,
                         json={"type": "task", "subject": "Call back", "company_id": cid,
                               "due_date": "2026-08-05"}))
    assert client.post(f"/api/activities/{a['id']}/complete", headers=admin,
                       json={}).status_code == 400     # outcome required
    d = data(client.post(f"/api/activities/{a['id']}/complete", headers=admin,
                         json={"outcome": "Spoke to owner", "follow_up_required": True,
                               "follow_up_date": "2026-08-08", "next_action": "Send quote"}))
    assert d["activity"]["status"] == "completed"
    assert d["follow_up"]["type"] == "follow_up" and d["follow_up"]["due_date"] == "2026-08-08"
    lead = data(client.get(f"/api/leads/{cid}", headers=admin))
    assert lead["company"]["next_action"] == "Send quote"
    assert lead["company"]["last_activity_at"]


def test_activity_type_validation(client, admin):
    assert client.post("/api/activities", headers=admin,
                       json={"type": "party", "subject": "x"}).status_code == 400


def test_timeline_merges_sources(client, admin):
    cid = make_lead(client, admin, "Timeline Pty")
    data(client.post("/api/activities", headers=admin,
                     json={"type": "note", "subject": "First note", "company_id": cid, "status": "completed"}))
    data(client.put(f"/api/leads/{cid}", headers=admin, json={"crm_status": "contacted"}))
    tl = data(client.get(f"/api/timeline?company_id={cid}", headers=admin))
    assert any(t["type"] == "activity" for t in tl)
    assert any(t["type"] == "audit" for t in tl)


# ── Dashboard calculations ───────────────────────────────────────────────────
def test_dashboard_crm_calculations(client, admin):
    d = data(client.get("/api/dashboard/crm", headers=admin))
    k = d["kpis"]
    assert k["active_pipeline"] >= 0 and k["weighted_pipeline"] <= k["active_pipeline"]
    assert isinstance(d["pipeline_by_stage"], list)
    won = [s for s in d["pipeline_by_stage"] if s["stage"] == "closed_won"]
    assert won and won[0]["count"] >= 1          # from stage test above
    assert "leads_not_contacted" in d["attention"]


# ── Permissions ──────────────────────────────────────────────────────────────
def test_settings_admin_only(client, admin, rep):
    r = client.put("/api/crm/settings", headers=rep, json={"score_hot_threshold": 80})
    assert r.status_code == 403
    d = data(client.put("/api/crm/settings", headers=admin,
                        json={"score_hot_threshold": 75, "score_warm_threshold": 45}))
    assert d["score_hot_threshold"] == 75
    # thresholds change tiering globally
    assert sales_app.crm_tier(74) == "warm"
    # restore defaults for other tests / real DB parity
    data(client.put("/api/crm/settings", headers=admin,
                    json={"score_hot_threshold": 70, "score_warm_threshold": 40}))


def test_settings_validation(client, admin):
    assert client.put("/api/crm/settings", headers=admin,
                      json={"score_hot_threshold": 30, "score_warm_threshold": 40}).status_code == 400
    assert client.put("/api/crm/settings", headers=admin,
                      json={"score_hot_threshold": 200}).status_code == 400


def test_auth_required_everywhere(client):
    for path in ("/api/leads", "/api/activities", "/api/dashboard/crm", "/api/crm/audit-log", "/api/users"):
        assert client.get(path).status_code == 401


# ── Bulk actions ─────────────────────────────────────────────────────────────
def test_bulk_status_and_owner(client, admin):
    ids = [make_lead(client, admin, f"Bulk Pty {i}") for i in range(3)]
    d = data(client.post("/api/leads/bulk", headers=admin,
                         json={"ids": ids, "action": "set_status", "crm_status": "contact_attempted"}))
    assert d["processed"] == 3
    users = data(client.get("/api/users", headers=admin))
    d2 = data(client.post("/api/leads/bulk", headers=admin,
                          json={"ids": ids, "action": "assign_owner", "owner_id": users[0]["id"]}))
    assert d2["processed"] == 3
    lead = data(client.get(f"/api/leads/{ids[0]}", headers=admin))
    assert lead["company"]["owner_id"] == users[0]["id"]
    assert lead["company"]["crm_status"] == "contact_attempted"


def test_bulk_rejects_bad_action(client, admin):
    assert client.post("/api/leads/bulk", headers=admin,
                       json={"ids": [1], "action": "explode"}).status_code == 400


# ── SMS consent gate (no Twilio keys in test env → send path not reached
#    for allowed contacts; the do-not-contact gate must still 403) ───────────
def test_sms_respects_do_not_contact(client, admin):
    cid = make_lead(client, admin, "DNC Pty")
    ct = data(client.post("/api/contacts", headers=admin,
              json={"company_id": cid, "first_name": "No", "phone": "+61400111222"}))
    data(client.put(f"/api/contacts/{ct['id']}", headers=admin, json={"contact_status": "dont_disturb"}))
    sales_app.run("UPDATE contacts SET do_not_contact=1 WHERE id=?", (ct["id"],))
    r = client.post("/api/sms/send", headers=admin,
                    json={"to": "+61400111222", "body": "hi", "company_id": cid})
    assert r.status_code == 403
