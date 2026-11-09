"""Gunicorn production config for RAG Studio."""

import os

bind = f"0.0.0.0:{os.getenv('RAG_PORT', '8000')}"

# Default 1 worker  -  WebSocket chat state is per-connection.
# Increase only behind a sticky-session load balancer.
workers = int(os.getenv("RAG_WORKERS", "1"))
worker_class = "uvicorn.workers.UvicornWorker"

timeout = 300
graceful_timeout = 30
keepalive = 5

accesslog = "-"
errorlog = "-"
loglevel = os.getenv("RAG_LOG_LEVEL", "info").lower()

# Only trust proxy headers from loopback: this appliance runs with no reverse
# proxy, and "*" would let any network peer spoof request.client via
# X-Forwarded-For (e.g. to pass the machine_id loopback gate).
forwarded_allow_ips = os.getenv("RAG_FORWARDED_ALLOW_IPS", "127.0.0.1")
proxy_protocol = False
