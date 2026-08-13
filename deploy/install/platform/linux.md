# Platform notes: Linux

The reference platform; the least to say.

- Docker Engine + the Compose plugin (`docker compose version`). Rootless
  Docker works; the node needs no privileged flags.
- The dashboard binds `${DASHBOARD_PORT:-8080}`; self-signed TLS by
  default. Behind your own reverse proxy set `DASHBOARD_TLS=off` and
  terminate TLS yourself.
- Session containers are spawned by the node and need nested-container
  support only for workloads that themselves run Docker.
