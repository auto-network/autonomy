# Dashboard agent image — extends base with starlette/uvicorn
# Agent can spin up the dashboard server, curl endpoints, validate work.
#
# Build:  docker build -t autonomy-session-platform -f agents/images/dashboard/Dockerfile .
# Depends: autonomy-session (base) must be built first

FROM autonomy-session

USER root

# Dashboard dependencies (python3-pip already in base image)
RUN pip install --break-system-packages --no-cache-dir \
       "starlette>=1.0,<2" "fastapi==0.139.2" "uvicorn==0.44.0" \
       "httpx==0.28.1" "websockets>=13,<17" httptools \
       inotify_simple "pytest-asyncio==1.3.0" \
       "webauthn==3.0.0" "cbor2==6.1.3"

# Copy dashboard source into image
COPY tools/dashboard/ /opt/autonomy/tools/dashboard/

USER agent
