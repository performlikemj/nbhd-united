#!/bin/sh
set -eu

ENV_FILE="${HOME}/.config/steward/env"
if [ ! -f "$ENV_FILE" ]; then
    echo "Steward env file is missing: $ENV_FILE" >&2
    exit 1
fi
if [ "$(stat -c '%a' "$ENV_FILE")" != "600" ]; then
    echo "Steward env file must have mode 600: $ENV_FILE" >&2
    exit 1
fi

# shellcheck disable=SC1090
. "$ENV_FILE"
: "${STEWARD_INGEST_SECRET:?STEWARD_INGEST_SECRET is required}"
: "${STEWARD_URL:?STEWARD_URL is required}"

TIMESTAMP=$(date +%s)
BODY='{"subject":"personal-openclaw-gateway"}'

# Never pass the shared secret in argv. Python's standard-library HMAC reads it
# from a mode-600 temporary key file; the file is removed on every exit path.
# OpenSSL 3's `mac` CLI accepts HMAC keys only as `key:`/`hexkey:` option values,
# which would put the secret in the process argument list.
umask 077
KEY_FILE=$(mktemp "${TMPDIR:-/tmp}/steward-heartbeat-key.XXXXXX")
trap 'rm -f "$KEY_FILE"' 0 HUP INT TERM
printf '%s' "$STEWARD_INGEST_SECRET" >"$KEY_FILE"
unset STEWARD_INGEST_SECRET

SIGNATURE=$(
    printf '%s.%s' "$TIMESTAMP" "$BODY" |
        python3 -c 'import hashlib, hmac, pathlib, sys; print(hmac.new(pathlib.Path(sys.argv[1]).read_bytes(), sys.stdin.buffer.read(), hashlib.sha256).hexdigest())' "$KEY_FILE"
)

printf '%s' "$BODY" |
    curl --fail --silent --show-error --max-time 10 \
        -X POST \
        -H "Content-Type: application/json" \
        -H "X-Steward-Timestamp: $TIMESTAMP" \
        -H "X-Steward-Signature: $SIGNATURE" \
        --data-binary @- \
        "${STEWARD_URL%/}/api/steward/heartbeat/"
