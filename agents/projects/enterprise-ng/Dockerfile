# Anchore Enterprise NG project agent image.
# Extends autonomy-agent:dind with:
#   - openssh-client (needed by tasks that shell out to git over SSH)
#   - go-task, for the Taskfile-based dev workflow in the enterprise_ng repo
# Poetry is inherited from autonomy-agent:dind.
#
# Build:  docker build -f agents/projects/enterprise-ng/Dockerfile -t autonomy-agent:enterprise-ng agents/
#         (build.sh auto-discovers this file.)

FROM autonomy-agent:dind

USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends openssh-client jq make \
    && rm -rf /var/lib/apt/lists/*
RUN curl -fsSL https://taskfile.dev/install.sh | sh -s -- -d -b /usr/local/bin \
    && task --version
USER agent
