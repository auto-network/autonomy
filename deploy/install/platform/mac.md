# Platform notes: macOS

- Docker Desktop (or Colima/OrbStack) provides the daemon; `docker compose`
  ships with all of them. Allocate ≥4 GB to the VM for comfortable builds.
- File-watching and bind mounts are slower than Linux; Autonomy keeps its
  state in a named volume, which is the fast path — don't move the data
  volume to a bind mount for convenience.
- Safari counts as a WebAuthn platform authenticator: the identity step's
  passkey enrolls in iCloud Keychain natively.
