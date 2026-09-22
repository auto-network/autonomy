#!/bin/sh
# autonomy-grok-auth — Grok Build external auth provider for gateway sessions.
#
# Grok Build refuses to start without a stored sign-in or a first-party xAI key
# that passes its api.x.ai probe. A session whose models live behind an
# OpenAI-compatible gateway (OpenRouter, a corporate proxy) has neither, so the
# launcher configures this script as ``[auth] auth_provider_command`` and runs
# ``grok login`` once before the TUI: Grok stores whatever this prints as its
# sign-in and the gate is satisfied. Requests to the gateway models carry the
# same key through their ``env_key``; the stored token is never sent to xAI
# (Grok only uses it for its own catalog/settings fetches, which 401 harmlessly).
#
# Contract (Grok Build user guide, "External Auth Provider"): stdout is the
# token — nothing else — and stderr is for humans. Exit non-zero and Grok
# falls back to interactive sign-in, so a missing key fails loudly here.
if [ -z "${GROK_GATEWAY_API_KEY:-}" ]; then
    echo "autonomy-grok-auth: GROK_GATEWAY_API_KEY is not set" >&2
    exit 1
fi
printf '{"access_token":"%s","expires_in":2592000}' "$GROK_GATEWAY_API_KEY"
