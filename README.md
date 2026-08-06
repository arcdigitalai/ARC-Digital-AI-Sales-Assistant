# ARC Digital AI Sales Assistant

Full-featured AI-powered sales CRM for ARC Digital. Runs locally on `http://127.0.0.1:9000` behind the ARC reverse proxy (`/sales` route).

## Features

- **CRM** — leads & qualification, companies, contacts, 13-stage opportunity pipeline (Kanban drag & drop), activities & timeline, quotations with versioning, invoices, closed-won handover with onboarding tasks, clients page, global search, audit log
- **AI & automation** — Groq-powered email generation, lead scoring, Bland AI voice calls, Twilio SMS qualification ("Sarah" agent via n8n), Autopilot with human approve-and-send gate
- **Integrations** — Groq, Twilio SMS, Bland AI, Gmail, Google Calendar, n8n webhooks (HMAC-signed)
- **Admin** — role-based access (admin / manager / sales_rep, readonly gate), CRM settings & thresholds, system health

## Architecture

Deliberately single-file for rapid iteration:

| Path | Purpose |
|---|---|
| `sales_app.py` | Entire Flask backend — config, SQLite schema, all 154 routes, integrations, background scheduler |
| `static/index.html` | Entire frontend — served by the backend, re-read per request |
| `app/` | Thin entry wrapper used by the Windows watchdog scheduled task |
| `tests/` | Pytest suite (55 tests): CRM, autopilot, reminders, R2/R3 features, frontend integrity |

## Run locally

```
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python sales_app.py
```

Open http://127.0.0.1:9000 — default login `admin@salesai.com` / `Admin@123456`.

Configuration comes from a `.env` file (not committed — see `.gitignore`). Without it the app runs with external integrations inactive. Live data lives in `data/sales.db` (also not committed).

## Tests

```
pytest tests/ -q
```
