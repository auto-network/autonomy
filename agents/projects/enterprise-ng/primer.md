## Container vs Host Differences

This container is NOT the same as working on the host. Key differences:

- **License**: already at `/license.yaml`. Do NOT run `ln -s ~/license.yaml` — it's pre-installed.
- **Env vars**: all `ANCHORE_*` vars are pre-configured. Do NOT copy `.env.example` or create `.env` files.
- **`task build`**: will FAIL (no SSH keys for private repo clones in the Dockerfile). Use `task test-deps-up` to start test infrastructure instead.
- **`task test-deps-up`**: works. Postgres has pg_cron pre-installed (required by job_framework).
- **`ANCHORE_EXTERNAL_TLS`**: pre-unset by startup. Do NOT set it — the config field was removed and Pydantic rejects it.
- **Auth**: use basic auth `-u admin:foobar` against `localhost:8228`. JWT token generation is not needed.
- **Legacy tables**: standalone mode (without v5 Enterprise) requires seeding legacy `anchore`, `accounts`, `services` tables before component_catalog fully bootstraps. The service will start and respond to health checks without them, but will log "Legacy schema not ready" warnings.
- **Enterprise repo**: mounted read-only at `/workspace/enterprise`. You cannot edit v5 code — dispatch a bead for that.
