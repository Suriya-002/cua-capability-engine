# One container serves: FastAPI (mock bank + engine API + operator console), a headed Chromium on
# Xvfb that the engine drives, and noVNC so a human can take over that same browser from the
# operator page. Everything is behind nginx on a single port (7860) for HF Spaces / Container Apps.
FROM mcr.microsoft.com/playwright/python:v1.63.0-noble

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 DISPLAY=:99 \
    CUA_HEADLESS=false PORT=7860 NOVNC_PATH=/session/vnc.html?autoconnect=1&path=session/websockify

RUN apt-get update && apt-get install -y --no-install-recommends \
      xvfb x11vnc novnc websockify nginx supervisor fluxbox python3-venv curl \
    && rm -rf /var/lib/apt/lists/*

# Install into a venv: the base image's system Python has Debian-managed packages pip cannot replace.
# The venv's Playwright is pinned to the same version as the base image, whose browsers live in /ms-playwright.
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright PATH="/opt/venv/bin:$PATH"
RUN python -m venv /opt/venv && /opt/venv/bin/pip install --upgrade pip

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
COPY mockbank ./mockbank
COPY policies ./policies
RUN pip install -e . && playwright install chromium

COPY deploy/supervisord.conf /etc/supervisor/conf.d/cua.conf
COPY deploy/nginx.conf /etc/nginx/sites-available/default

# Evidence is ephemeral in the container; the real record is committed to git by CI.
RUN mkdir -p /app/evidence && useradd -m -u 1000 cua && chown -R cua:cua /app /var/lib/nginx /var/log/nginx /run 2>/dev/null || true
EXPOSE 7860
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s CMD curl -fs http://localhost:7860/health || exit 1
CMD ["/usr/bin/supervisord", "-n", "-c", "/etc/supervisor/supervisord.conf"]
