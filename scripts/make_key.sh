#!/usr/bin/env bash
# Materialise the keypair from an env var, never from the repository.
# Exports the base58 secret to $1 with mode 600.
set -euo pipefail
out="${1:-/run/secrets/payer.json}"
if [ -z "${SOLANA_SECRET_KEY:-}" ]; then
  echo "SOLANA_SECRET_KEY is not set" >&2
  exit 1
fi
umask 077
printf '%s' "$SOLANA_SECRET_KEY" > "$out"
echo "wrote $out"
