#!/usr/bin/env bash
# Verify one immutable Autonomy image reference against the project key.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PUBLIC_KEY="${AUTONOMY_COSIGN_PUBLIC_KEY:-$SCRIPT_DIR/cosign.pub}"
IMAGE_REF="${1:-}"

if [[ ! "$IMAGE_REF" =~ ^[^[:space:]@]+@sha256:[0-9a-f]{64}$ ]]; then
    echo "refusing mutable image reference; use image@sha256:<64 lowercase hex>" >&2
    exit 2
fi
if [[ ! -f "$PUBLIC_KEY" ]]; then
    echo "project cosign public key not found: $PUBLIC_KEY" >&2
    exit 2
fi
command -v cosign >/dev/null || { echo "cosign is required" >&2; exit 2; }

cosign verify --insecure-ignore-tlog --key "$PUBLIC_KEY" \
    "$IMAGE_REF" >/dev/null
printf 'verified %s with %s\n' "$IMAGE_REF" "$PUBLIC_KEY"
