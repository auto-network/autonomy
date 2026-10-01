"""The node's GitHub SSH key: only from the vault (auto-zhbje).

The dashboard clones and fetches workspace repositories over git+SSH as the
autonomy user. That key used to be copied at container start from a plaintext
disk artifact (data/artifacts/*/*/id_ed25519) into ~/.ssh of every container
sharing the entrypoint. It is now the operator's audited vault entry
``node.github-ssh-key`` (personal store), released by the dashboard into its
own ramfs key cache -- the carrier backup and the MCP relay use
(tools.dashboard.host_release) -- where the entrypoint's ssh config points
(``IdentityFile``). A reboot empties the ramfs: until the vault is unlocked
there is no key, and a private clone says so (workspace_manager).
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

#: The audited personal vault entry (bare key: the operator's own store).
VAULT_KEY = "node.github-ssh-key"
RELEASE_SUBDIR = "node-ssh"
KEY_FILE = "id_ed25519"


def release_node_ssh_key(*, directory=None, memory_check=None) -> str:
    """Release the node's GitHub key from the audited vault; the status:
    ``ok``, ``vault-cold`` (released key kept), ``unsealed`` (cleared) or
    ``release-failed``. Never raises; never logs the key. Decrypts: call off
    the event loop."""
    from tools.dashboard import host_release
    from tools.graph import settings_ops
    from tools.graph.schemas.vault_credential import VAULT_AUDITED_SET_ID

    directory = host_release.release_dir(RELEASE_SUBDIR) if directory is None else directory
    with host_release.RELEASE_LOCK:
        try:
            if not settings_ops.personal_delegate_audited_is_warm():
                return "vault-cold"
            row = settings_ops.read_set_key(VAULT_AUDITED_SET_ID, VAULT_KEY,
                                            org=None, peers=[])
            if row is not None and row.get("vault_error") is not None:
                return "vault-cold"
            value = ((row or {}).get("payload") or {}).get("value") or ""
            if not value.strip():
                host_release.clear_files(directory, (KEY_FILE,))
                return "unsealed"
            # OpenSSH refuses a private key file that does not end in a
            # newline ("invalid format"); the sealed bytes may not.
            key = value.strip().replace("\r\n", "\n") + "\n"
            host_release.write_files(directory, {KEY_FILE: key.encode("utf-8")},
                                     memory_check=memory_check)
            logger.info("node ssh: GitHub key released for workspace clones")
            return "ok"
        except Exception:
            logger.exception("node ssh: key release failed")
            return "release-failed"
