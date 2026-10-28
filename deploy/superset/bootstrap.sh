#!/bin/bash
set -e
# The placeholder SECRET_KEY this config used to ship with: secrets stored under it are re-encrypted once below.
PUBLISHED_SECRET_KEY="analystos-dev-superset-secret-change-me"

# SECRET_KEY signs login sessions and encrypts the stored database-connection passwords, so it must stay the same
# across restarts. When none is configured, one is generated on first boot and kept in the superset-home volume.
# superset_config.py refuses a short or published value.
KEY_FILE="${SUPERSET_HOME:-/app/superset_home}/aos_secret_key"
NEW_KEY=""
if [ -z "${SUPERSET_SECRET_KEY:-}" ]; then
  if [ -s "$KEY_FILE" ]; then
    SUPERSET_SECRET_KEY="$(cat "$KEY_FILE")"
  else
    SUPERSET_SECRET_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(48))')"
    NEW_KEY=1
    echo "SUPERSET_SECRET_KEY not set: generated one; it is kept in $KEY_FILE"
  fi
fi
export SUPERSET_SECRET_KEY

# Guest-token secret (S08-T5): one random value for this container's lifetime when none is configured, shared by
# every command below and the gunicorn workers. superset_config.py refuses a short or published value.
if [ -z "${SUPERSET_GUEST_TOKEN_SECRET:-}" ]; then
  SUPERSET_GUEST_TOKEN_SECRET="$(python -c 'import secrets; print(secrets.token_urlsafe(48))')"
  echo "SUPERSET_GUEST_TOKEN_SECRET not set: generated a random one for this container"
fi
export SUPERSET_GUEST_TOKEN_SECRET

superset db upgrade

# Move stored secrets to the current key: from the published placeholder when the key was just generated, or from
# SUPERSET_PREVIOUS_SECRET_KEY on a deliberate rotation. Values the current key already reads are skipped; a value
# neither key reads stops the boot (set -e) before a generated key is saved, so the next boot tries again.
PREVIOUS="${SUPERSET_PREVIOUS_SECRET_KEY:-}"
if [ -z "$PREVIOUS" ] && [ -n "$NEW_KEY" ]; then
  PREVIOUS="$PUBLISHED_SECRET_KEY"
fi
if [ -n "$PREVIOUS" ]; then
  if ! superset re-encrypt-secrets --previous_secret_key "$PREVIOUS"; then
    echo "Stored connection secrets are encrypted with a key that is neither the current nor the previous one." >&2
    echo "Set SUPERSET_SECRET_KEY to that key (or keep it and set SUPERSET_PREVIOUS_SECRET_KEY to rotate)." >&2
    exit 1
  fi
  echo "stored secrets re-encrypted with the current SECRET_KEY"
fi
if [ -n "$NEW_KEY" ]; then
  (umask 077 && printf '%s' "$SUPERSET_SECRET_KEY" > "$KEY_FILE")
fi

superset fab create-admin --username "${SUPERSET_ADMIN_USER:-admin}" --firstname Admin --lastname User \
  --email admin@superset.local --password "${SUPERSET_ADMIN_PASSWORD:-admin}" || true
superset init
python /app/analystos/guest_role.py  # S08-T5: the read-only role embedded guests hold
exec gunicorn --bind 0.0.0.0:8088 --workers 2 --timeout 120 "superset.app:create_app()"
