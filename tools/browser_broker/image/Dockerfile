# autonomy-browser:local — one leased, visibly operated browser per container
# (auto-8q7oe.5; design graph://c330323d-986). Build with build.sh.
#
# Branded Google Chrome (auto-8q7oe.3: it reports the Google Chrome brand like
# a normal install, for +9 MB over patchright's Chromium). Software WebGL via
# SwiftShader; no GPU passthrough.
FROM python:3.12-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright \
    BROWSER_SCREEN=1920x1080x24

RUN apt-get update && apt-get install -y --no-install-recommends \
        xvfb x11vnc tini procps ca-certificates \
        fonts-liberation fonts-dejavu-core fonts-noto-core fonts-noto-color-emoji \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir patchright==1.63.0 websockets==17.1 \
    && python -m patchright install --with-deps chrome \
    && rm -rf /var/lib/apt/lists/* /tmp/*

RUN useradd -m -u 1000 browser \
    && mkdir -p /profile /tmp/downloads /opt/lease \
    && chown browser:browser /profile /tmp/downloads
WORKDIR /opt/lease
COPY tools/connectors/__init__.py tools/connectors/stealth_repl.py tools/connectors/repl_auth.py tools/connectors/
COPY tools/browser_broker/__init__.py tools/browser_broker/lease_agent.py tools/browser_broker/lease_watchdog.py tools/browser_broker/
COPY tools/browser_broker/image/entrypoint.sh /opt/lease/entrypoint.sh

USER browser
EXPOSE 7300 5900
# -g: signals reach the whole process group, so docker stop ends Chrome too.
ENTRYPOINT ["tini", "-g", "--", "/opt/lease/entrypoint.sh"]
