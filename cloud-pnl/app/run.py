"""
Entry point for the Cloud P&L backend.

Uses waitress (production WSGI server) with a request-logging middleware.
Logs every API call with method, path, status code and duration — same as uvicorn.

Run:
    python run.py
"""

import time
import logging
import sys
from waitress import serve
from app import create_app

# ── Logging setup ─────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("api")

# Silence noisy waitress internal logs, keep only warnings+
logging.getLogger("waitress").setLevel(logging.WARNING)


# ── Request logging middleware ────────────────────────────────────────────────
class LoggingMiddleware:
    """Wraps the Flask WSGI app and logs every request with status + duration."""

    def __init__(self, wsgi_app):
        self.app = wsgi_app

    def __call__(self, environ, start_response):
        method  = environ.get("REQUEST_METHOD", "?")
        path    = environ.get("PATH_INFO", "?")
        qs      = environ.get("QUERY_STRING", "")
        full    = f"{path}?{qs}" if qs else path

        status_holder = []
        t0 = time.perf_counter()

        def capturing_start_response(status, headers, exc_info=None):
            status_holder.append(status)
            return start_response(status, headers, exc_info)

        result = self.app(environ, capturing_start_response)
        ms = (time.perf_counter() - t0) * 1000
        status = status_holder[0] if status_holder else "???"
        log.info("%-7s %-45s  %s  (%.1f ms)", method, full, status, ms)
        return result


# ── Build and run ─────────────────────────────────────────────────────────────
flask_app = create_app()
app = LoggingMiddleware(flask_app)

if __name__ == "__main__":
    HOST = "0.0.0.0"
    PORT = 6000
    log.info("Starting Cloud P&L backend on http://%s:%d", HOST, PORT)
    log.info("Press Ctrl+C to stop.\n")
    serve(app, host=HOST, port=PORT, threads=4)
