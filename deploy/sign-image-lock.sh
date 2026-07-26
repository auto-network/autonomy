#!/usr/bin/env bash
# Human-gated signing of an immutable image lock.
#
# This command is intentionally absent from CI. It accepts only a local,
# password-armored key file and leaves cosign to prompt the operator for its
# passphrase. The project public key remains the offline verification root.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

LOCK_FILE="${1:-${AUTONOMY_IMAGE_LOCK:-$REPO_ROOT/image-lock.env}}"
PRIVATE_KEY="${AUTONOMY_COSIGN_PRIVATE_KEY:-$SCRIPT_DIR/cosign.key}"
PUBLIC_KEY="${AUTONOMY_COSIGN_PUBLIC_KEY:-$SCRIPT_DIR/cosign.pub}"

[[ -f "$LOCK_FILE" ]] || { echo "image lock not found: $LOCK_FILE" >&2; exit 2; }
case "$PRIVATE_KEY" in
    env://*) echo "refusing environment-backed signing key" >&2; exit 2 ;;
esac
[[ -s "$PRIVATE_KEY" ]] || {
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
printf 'Type SIGN %s to unlock the project key and publish signatures: ' "$release_tag"
IFS= read -r confirmation
if [[ "$confirmation" != "SIGN $release_tag" ]]; then
    echo "signing cancelled" >&2
    exit 2
fi

for digest_ref in "${refs[@]}"; do
    echo "==> Signing $digest_ref"
    # The tracked project key is the complete trust root. Do not contact
    # Rekor/Fulcio; verification must also work fully offline.
    cosign sign --yes --tlog-upload=false --key "$PRIVATE_KEY" "$digest_ref"
    cosign verify --insecure-ignore-tlog --key "$PUBLIC_KEY" \
        "$digest_ref" >/dev/null
done

echo "==> Operator signatures published and verified"
