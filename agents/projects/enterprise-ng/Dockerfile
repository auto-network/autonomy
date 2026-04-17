# Anchore Enterprise NG project agent image.
# Extends autonomy-agent:dind. Enterprise NG uses Python + Poetry for
# component_catalog; Poetry is already present in the base Autonomy image,
# so nothing extra is installed here. The file exists so build.sh
# auto-discovers the enterprise-ng image.
#
# Build:  docker build -f agents/projects/enterprise-ng/Dockerfile -t autonomy-agent:enterprise-ng agents/
#         (build.sh auto-discovers this file.)

FROM autonomy-agent:dind
