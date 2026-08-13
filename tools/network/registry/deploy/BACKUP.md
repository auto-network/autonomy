# Registry state backup boundary

Copying the live `registry.db` is not a backup: production's 4 KiB main file
contained zero tables while its WAL held all organizations and links. The
snapshot tool instead uses SQLite's online backup API and verifies the one
standalone result before restic sees it.

No real object-store round trip has run. The timer stays uninstalled until
`auto-clune.6` proves a real backup and scratch restore against the approved
auto.network-owned repository. Activation belongs in the existing registry
deploy path, not a second backup-specific deploy script.

That deployment consumes four root-owned files in
`/etc/autonomy-registry-backup/`:

- `repository`: the dedicated restic S3 repository URL
- `restic-password`: an independent random repository password
- `access-key-id`: a standard B2 application key id
- `secret-access-key`: that key's secret

The B2 bucket and key require explicit approval after the pre-check records the
exact ownership boundary, endpoint, capabilities, and cost. The serving host
runs no `forget` or `prune`; retention is off-host. No witness key or other
service secret belongs in this backup.
