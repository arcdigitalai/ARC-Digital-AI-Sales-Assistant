"""Test bootstrap — runs before ANY test module is imported.

Pins DB_PATH to a fresh throwaway directory and strips live integration
credentials. This must live here (not in a test module): pytest imports test
files alphabetically, so a test module that imports sales_app before the one
that sets DB_PATH would silently run the whole suite against the stale
%TEMP%\\sales.db from a previous run.
"""
import os, tempfile

_tmp = tempfile.mkdtemp(prefix="arc-crm-test-")
os.environ["DB_PATH"] = os.path.join(_tmp, "test.db")

for _k in ("GROQ_API_KEY", "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER",
           "TWILIO_ADMIN_NUMBER", "BLAND_API_KEY", "SENDGRID_API_KEY",
           "GMAIL_SENDER_EMAIL", "GMAIL_APP_PASSWORD",
           "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET"):
    os.environ.pop(_k, None)
