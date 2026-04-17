# Anchore Enterprise project agent image.
# Extends autonomy-agent:dind with Go, make, and the Postgres client so an
# agent can build the Enterprise server, run test compose stacks inside the
# nested docker daemon, and shell into the service DB.
#
# Build:  docker build -f agents/projects/enterprise/Dockerfile -t autonomy-agent:enterprise agents/
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
