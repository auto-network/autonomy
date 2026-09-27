#!/usr/bin/env bash
# Human-gated signing of an immutable image lock.
#
# This command is intentionally absent from CI. The project public key remains
# the offline verification root. The private key comes from one of two places:
#
#   * AUTONOMY_COSIGN_VAULT_KEY=<name>: the key is sealed in the secured tier of
#     the vault (organization AUTONOMY_COSIGN_VAULT_ORG, default autonomy). The
#     operator's approval of the release is the human act that authorizes this
#     signing; the key is delivered to this session's private ramfs, used, and
#     removed. Such a key carries no passphrase of its own.
#   * otherwise a local, password-armored key file, and cosign prompts the
#     operator for its passphrase after a typed release confirmation.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

LOCK_FILE="${1:-${AUTONOMY_IMAGE_LOCK:-$REPO_ROOT/image-lock.env}}"
PRIVATE_KEY="${AUTONOMY_COSIGN_PRIVATE_KEY:-$SCRIPT_DIR/cosign.key}"
PUBLIC_KEY="${AUTONOMY_COSIGN_PUBLIC_KEY:-$SCRIPT_DIR/cosign.pub}"
VAULT_KEY="${AUTONOMY_COSIGN_VAULT_KEY:-}"
VAULT_ORG="${AUTONOMY_COSIGN_VAULT_ORG:-autonomy}"
VAULT_WAIT="${AUTONOMY_COSIGN_VAULT_WAIT:-900}"

[[ -f "$LOCK_FILE" ]] || { echo "image lock not found: $LOCK_FILE" >&2; exit 2; }
if [[ -n "$VAULT_KEY" ]]; then
    PRIVATE_KEY=""
fi
case "$PRIVATE_KEY" in
    env://*) echo "refusing environment-backed signing key" >&2; exit 2 ;;
esac
[[ -n "$VAULT_KEY" || -s "$PRIVATE_KEY" ]] || {
    echo "password-armored cosign private key not found: $PRIVATE_KEY" >&2
    exit 2
}
[[ -s "$PUBLIC_KEY" ]] || {
    echo "tracked cosign public key not found: $PUBLIC_KEY" >&2
    exit 2
}
if [[ -n "${COSIGN_PASSWORD:-}" ]]; then
    echo "refusing COSIGN_PASSWORD: unlock the armored key interactively" >&2
    exit 2
fi
command -v cosign >/dev/null || { echo "cosign is required" >&2; exit 2; }

release_tag=""
refs=()
while IFS='=' read -r name value; do
    case "$name" in
        AUTONOMY_RELEASE_TAG)
            release_tag="$value"
            ;;
        AUTONOMY_*_IMAGE)
            if [[ ! "$value" =~ ^[A-Za-z0-9._:/-]+@sha256:[0-9a-f]{64}$ ]]; then
                echo "refusing non-digest image lock entry: $name=$value" >&2
                exit 2
            fi
            refs+=("$value")
            ;;
    esac
done <"$LOCK_FILE"

[[ -n "$release_tag" ]] || { echo "image lock has no release tag" >&2; exit 2; }
[[ "${#refs[@]}" -gt 0 ]] || { echo "image lock has no image digests" >&2; exit 2; }

echo "Images to sign for release $release_tag:"
printf '  %s\n' "${refs[@]}"
if [[ -n "$VAULT_KEY" ]]; then
    command -v graph >/dev/null || { echo "graph is required for a vault key" >&2; exit 2; }
    echo "==> Requesting release of secured vault key $VAULT_ORG:$VAULT_KEY (approve it to sign)"
    read_out="$(graph vault read "$VAULT_KEY" --org "$VAULT_ORG" --wait "$VAULT_WAIT")"
    PRIVATE_KEY="$(printf '%s\n' "$read_out" | tail -n 1)"
    if [[ "$PRIVATE_KEY" != /* ]]; then
        # A secured release is asynchronous: the read returns "pending" at
        # once and the vault places the key at /run/secrets/<name> when the
        # operator approves. Wait for the file, bounded by VAULT_WAIT.
        pending="/run/secrets/$VAULT_KEY"
        if printf '%s' "$read_out" | grep -q "release pending"; then
            echo "==> Waiting up to ${VAULT_WAIT}s for the approval ($pending)"
            for _ in $(seq 1 "$VAULT_WAIT"); do [[ -s "$pending" ]] && break; sleep 1; done
            [[ -s "$pending" ]] && PRIVATE_KEY="$pending"
        fi
    fi
    if [[ "$PRIVATE_KEY" != /* || ! -s "$PRIVATE_KEY" ]]; then
        echo "vault key was not released: $(printf '%s' "$read_out" | head -n 1)" >&2
        exit 2
    fi
    trap 'rm -f "$PRIVATE_KEY" "${SIGNING_CONFIG:-}"' EXIT
    export COSIGN_PASSWORD=""
else
    printf 'Type SIGN %s to unlock the project key and publish signatures: ' "$release_tag"
    IFS= read -r confirmation
    if [[ "$confirmation" != "SIGN $release_tag" ]]; then
        echo "signing cancelled" >&2
        exit 2
    fi
fi

# The tracked project key is the complete trust root. A signing config that
# names no services keeps cosign (v3+) away from Rekor and Fulcio, and
# verification must also work fully offline.
SIGNING_CONFIG="$(mktemp)"
printf '{"mediaType":"application/vnd.dev.sigstore.signingconfig.v0.2+json"}\n' >"$SIGNING_CONFIG"
for digest_ref in "${refs[@]}"; do
    echo "==> Signing $digest_ref"
    cosign sign --yes --signing-config "$SIGNING_CONFIG" --key "$PRIVATE_KEY" "$digest_ref"
    cosign verify --insecure-ignore-tlog --key "$PUBLIC_KEY" \
        "$digest_ref" >/dev/null
done

echo "==> Operator signatures published and verified"
