#!/bin/bash
set -e
# Guest-token secret (S08-T5): one random value for this container's lifetime when none is configured, shared by
# every command below and the gunicorn workers. superset_config.py refuses a short or published value.
if [ -z "${SUPERSET_GUEST_TOKEN_SECRET:-}" ]; then
  SUPERSET_GUEST_TOKEN_SECRET="$(python -c 'import secrets; print(secrets.token_urlsafe(48))')"
  echo "SUPERSET_GUEST_TOKEN_SECRET not set: generated a random one for this container"
fi
export SUPERSET_GUEST_TOKEN_SECRET
superset db upgrade
superset fab create-admin --username "${SUPERSET_ADMIN_USER:-admin}" --firstname Admin --lastname User \
  --email admin@superset.local --password "${SUPERSET_ADMIN_PASSWORD:-admin}" || true
superset init
python /app/analystos/guest_role.py  # S08-T5: the read-only role embedded guests hold
exec gunicorn --bind 0.0.0.0:8088 --workers 2 --timeout 120 "superset.app:create_app()"
