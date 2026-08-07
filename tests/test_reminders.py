"""Chat-created reminders + due-reminder pop-up API tests."""
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
    r = client.post("/api/auth/login", json={"email": "kevin@salesai.com", "password": "Adminkevin@123456"})
    return {"Authorization": "Bearer " + r.get_json()["data"]["token"]}


def data(r):
    j = r.get_json()
    assert j and j.get("ok"), f"API error: {j}"
    return j.get("data")


def test_fallback_parser_extracts_subject_date_time():
    from datetime import datetime
    base = datetime(2026, 8, 5, 10, 0)
    p = sales_app._parse_reminder_fallback("remind me to call Jake about the quote tomorrow at 2pm", base)
    assert p["due_date"] == "2026-08-06"
    assert p["due_time"] == "14:00"
    assert "call jake about the quote" in p["subject"].lower()
    p2 = sales_app._parse_reminder_fallback("set a reminder to send the invoice in 3 days", base)
    assert p2["due_date"] == "2026-08-08"
    p3 = sales_app._parse_reminder_fallback("remind me to check the pipeline in 2 hours", base)
    assert p3["due_date"] == "2026-08-05" and p3["due_time"] == "12:00"
    # no date given -> defaults to tomorrow, never crashes
    p4 = sales_app._parse_reminder_fallback("remind me to breathe", base)
    assert p4["due_date"] == "2026-08-06" and p4["subject"]


def test_chat_creates_reminder_activity(client, admin):
    before = sales_app.q1("SELECT COUNT(*) AS n FROM activities WHERE description='Reminder set from AI Chat'")["n"]
    d = data(client.post("/api/chat", headers=admin,
                         json={"message": "remind me to call the test plumber tomorrow at 9am"}))
    assert "Reminder set" in d["reply"]
    after = sales_app.q1("SELECT COUNT(*) AS n FROM activities WHERE description='Reminder set from AI Chat'")["n"]
    assert after == before + 1
    act = sales_app.q1("""SELECT * FROM activities WHERE description='Reminder set from AI Chat'
                          ORDER BY id DESC LIMIT 1""")
    assert act["type"] == "follow_up" and act["due_date"]


def test_chat_lists_reminders(client, admin):
    d = data(client.post("/api/chat", headers=admin, json={"message": "show my reminders"}))
    assert "reminders" in d["reply"].lower()


def test_due_endpoint_and_ack_flow(client, admin):
    # overdue activity -> appears in /reminders/due exactly once, then acks
    aid = sales_app.run("""INSERT INTO activities(type,subject,assigned_to,created_by,due_date,due_time,status)
                           VALUES('follow_up','Overdue pop test',1,1,'2026-01-01','09:00','not_started')""")
    due = data(client.get("/api/reminders/due", headers=admin))
    assert any(a["id"] == aid for a in due)
    data(client.post(f"/api/reminders/{aid}/ack", headers=admin, json={"action": "dismiss"}))
    due2 = data(client.get("/api/reminders/due", headers=admin))
    assert not any(a["id"] == aid for a in due2)          # dismissed = no re-pop
    # snooze pushes due into the future and re-arms notification
    aid2 = sales_app.run("""INSERT INTO activities(type,subject,assigned_to,created_by,due_date,status)
                            VALUES('task','Snooze pop test',1,1,'2026-01-01','not_started')""")
    s = data(client.post(f"/api/reminders/{aid2}/ack", headers=admin,
                         json={"action": "snooze", "minutes": 60}))
    assert s["snoozed_until"]
    act = sales_app.q1("SELECT * FROM activities WHERE id=?", (aid2,))
    assert act["reminder_notified_at"] is None and act["due_date"] >= "2026-08-01"


def test_future_reminder_not_due_yet(client, admin):
    aid = sales_app.run("""INSERT INTO activities(type,subject,assigned_to,created_by,due_date,status)
                           VALUES('task','Far future test',1,1,'2030-01-01','not_started')""")
    due = data(client.get("/api/reminders/due", headers=admin))
    assert not any(a["id"] == aid for a in due)
