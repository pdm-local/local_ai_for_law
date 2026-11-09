# Lean DEV image: the app ONLY. It talks to an EXTERNAL Ollama (see docker-compose.yml),
# so no model weights or Ollama server are baked in. For a single self-contained,
# ship-to-a-client image (app + Ollama + baked models) use Dockerfile.appliance instead.
FROM python:3.13-slim

# app.py imports the package `local_ai_law.*` after putting its parent dir on
# sys.path, so the code MUST live at <root>/local_ai_law or nothing imports. Mirror
# that layout; app.py then adds /app to the path and the imports resolve. Mutable state
# lives on the /app/data volume — never in the app dir — so paths point there.
ENV APP_HOME=/app/local_ai_law \
    RAG_DATA_DIR=/app/data \
    RAG_USERS_FILE=/app/data/users.json \
    RAG_AUDIT_LOG=/app/data/audit.log \
    RAG_SETTINGS_FILE=/app/data/settings.json \
    RAG_JWT_SECRET_FILE=/app/data/.jwt_secret

WORKDIR /app/local_ai_law

# System deps for document processing. No Pango/cairo: WeasyPrint is an optional extra
# (requirements-pdf.txt) the default build leaves out, so reports are served as HTML.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libxml2 libxslt1.1 curl \
    libffi-dev && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Non-root app user, created before the COPYs so `COPY --chown` sets ownership inline.
RUN useradd --create-home --home-dir /home/appuser --shell /usr/sbin/nologin appuser \
 && mkdir -p /app/data \
 && chown -R appuser:appuser /app /home/appuser

# App RUNTIME code only — explicit list (not `COPY . .`) so secrets, tenant data,
# the frontend/ Vite source (the built UI ships in static/), tests, and the internal
# bench/eval scripts/ are never baked into the image. The .dockerignore is a second
# line of defence; listing runtime paths is the real guarantee.
COPY --chown=appuser:appuser app.py config.py models.py gunicorn.conf.py ./
COPY --chown=appuser:appuser routes/   ./routes/
COPY --chown=appuser:appuser services/ ./services/
COPY --chown=appuser:appuser static/   ./static/

USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8000/api/health || exit 1

CMD ["gunicorn", "-c", "gunicorn.conf.py", "app:app"]
