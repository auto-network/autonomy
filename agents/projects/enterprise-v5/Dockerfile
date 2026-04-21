# Anchore Enterprise v5 project agent image.
# Extends autonomy-agent:dind with Go, make, and the Postgres client so an
# agent can build the v5 Enterprise server, run test compose stacks inside the
# nested docker daemon, and shell into the service DB.
#
# v5 is a subset of the NG stack — for full-stack NG work use the
# enterprise-ng workspace, which mounts both the enterprise and enterprise_ng
# repos. This image only mounts the enterprise repo.
#
# Build:  docker build -f agents/projects/enterprise-v5/Dockerfile -t autonomy-agent:enterprise-v5 agents/
#         (build.sh auto-discovers this file.)

FROM autonomy-agent:dind

USER root

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
        golang-go \
        make \
        postgresql-client \
    && rm -rf /var/lib/apt/lists/*

USER agent
