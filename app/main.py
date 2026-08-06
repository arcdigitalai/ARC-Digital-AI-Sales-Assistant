"""ASGI entry point for the shared ARC watchdog (uvicorn app.main:app).

The real application is the single-file Flask app in ..\sales_app.py (WSGI).
This shim wraps it in an ASGI adapter so serve-arc-app.ps1 can run it the
same way as every other ARC app, and pins the SQLite DB inside the app
folder — the upstream default is %TEMP%\sales.db, which Windows temp
cleanup would silently delete.
"""
import os
import sys
from pathlib import Path

_BASE = Path(__file__).resolve().parent.parent
os.environ.setdefault("DB_PATH", str(_BASE / "data" / "sales.db"))
sys.path.insert(0, str(_BASE))

# Load integration credentials from <appdir>\.env (KEY=VALUE lines).
# Real environment variables win over .env entries.
_ENV_FILE = _BASE / ".env"
if _ENV_FILE.exists():
    for _line in _ENV_FILE.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

from a2wsgi import WSGIMiddleware

from sales_app import app as _flask_app

app = WSGIMiddleware(_flask_app)
