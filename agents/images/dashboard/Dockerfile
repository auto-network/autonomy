# Dashboard agent image — extends base with starlette/uvicorn
# Agent can spin up the dashboard server, curl endpoints, validate work.
#
# Build:  docker build -t autonomy-agent:dashboard -f agents/images/dashboard/Dockerfile .
# Depends: autonomy-agent (base) must be built first

FROM autonomy-agent

USER root

# Dashboard dependencies
RUN apt-get update && apt-get install -y --no-install-recommends python3-pip \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --break-system-packages --no-cache-dir \
       starlette uvicorn websockets httptools

# Copy dashboard source into image
COPY tools/dashboard/ /opt/autonomy/tools/dashboard/

USER agent
