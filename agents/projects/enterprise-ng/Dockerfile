# Anchore Enterprise NG project agent image.
# Extends autonomy-agent:dind with the go-task runner, which the
# enterprise_ng repo uses to orchestrate test deps, migrations, and
# services (Taskfile.yaml in the repo root: `task up`, `task test-deps-up`,
# etc.). Poetry is inherited from autonomy-agent:dind.
#
# Build:  docker build -f agents/projects/enterprise-ng/Dockerfile -t autonomy-agent:enterprise-ng agents/
#         (build.sh auto-discovers this file.)

FROM autonomy-agent:dind

USER root
RUN curl -fsSL https://taskfile.dev/install.sh | sh -s -- -d -b /usr/local/bin \
    && task --version
USER agent
