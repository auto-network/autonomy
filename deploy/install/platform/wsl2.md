# Platform notes: WSL2

- Run everything inside the WSL2 distro (Docker Desktop with WSL
  integration, or Docker Engine installed in-distro). Don't mix Windows
  paths into the compose file; keep the checkout on the Linux filesystem
  (`~/autonomy`, not `/mnt/c/...`) — bind performance across the boundary
  is poor and file-mode bits misbehave.
- `https://localhost:8080` is reachable from the Windows browser directly
  (WSL2 forwards localhost). The identity step happens in the WINDOWS
  browser — passkeys enroll against Windows Hello.
- If localhost forwarding is off (rare, configurable), use the distro's IP
  from `ip addr` and expect the self-signed-cert prompt.
