import os
import sys

# SECRET_KEY signs login sessions (a known key forges an admin session) and encrypts stored connection passwords.
# No default: bootstrap.sh generates one on first boot and keeps it in the superset-home volume. A short or
# published value refuses to start. To rotate, set the new key plus SUPERSET_PREVIOUS_SECRET_KEY for one boot.
_PUBLISHED_SECRET_KEYS = {"analystos-dev-superset-secret-change-me", "change_me_to_a_complex_random_secret",
                          "thisisasecret_1234", "change-me", "changeme"}
SECRET_KEY = os.environ.get("SUPERSET_SECRET_KEY", "")
if len(SECRET_KEY) < 32 or SECRET_KEY.lower() in _PUBLISHED_SECRET_KEYS:
    raise RuntimeError("SUPERSET_SECRET_KEY must be a random value of at least 32 characters (unset: "
                       "deploy/superset/bootstrap.sh generates and keeps one); refusing to start with a known or short key")
PREVIOUS_SECRET_KEY = os.environ.get("SUPERSET_PREVIOUS_SECRET_KEY") or None
SQLALCHEMY_DATABASE_URI = os.environ.get("SUPERSET_DATABASE_URI", "postgresql+psycopg2://superset:superset@postgres:5432/superset")
WTF_CSRF_ENABLED = True
WTF_CSRF_EXEMPT_LIST = ["superset.views.core.log", "superset.charts.data.api.data"]
FEATURE_FLAGS = {"DASHBOARD_NATIVE_FILTERS": True, "ENABLE_TEMPLATE_PROCESSING": False,
                 # S08-T5: dashboards are shown inside the AnalystOS UI with guest tokens the platform mints
                 # after rechecking authorization (dashboards/access.py::guest_token).
                 "EMBEDDED_SUPERSET": True}
TALISMAN_ENABLED = False
# P4-02: the publisher provisions one Superset role per workspace (database_access on that workspace's
# database only) and assigns workspace users to it through the FAB security REST API.
FAB_ADD_SECURITY_API = True
ENABLE_PROXY_FIX = True

# Guest tokens. A guest holds only this role (read-only, no data access of its own, created by guest_role.py);
# it reads a dashboard's data because the token names that dashboard, filtered by the token's RLS clauses.
GUEST_ROLE_NAME = "aos_embed_guest"
# A guest's chart-data request must be the saved chart (aos_security.py: Superset 4.1.1 skips its own check
# for guests because the chart lookup is permission-filtered).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from aos_security import AosSecurityManager  # noqa: E402

CUSTOM_SECURITY_MANAGER = AosSecurityManager
# Whoever knows this secret can mint a guest token for any embedded dashboard with no row filters, skipping
# every platform check. Superset signs and verifies these tokens itself (the platform asks its API for them),
# so no default exists: bootstrap.sh generates a random one per container when none is given, and a short or
# formerly published value refuses to start. Several Superset replicas must share one explicit value.
_PUBLISHED_GUEST_SECRETS = {"analystos-dev-guest-token-secret-change-me", "test_secret", "change-me", "changeme"}
GUEST_TOKEN_JWT_SECRET = os.environ.get("SUPERSET_GUEST_TOKEN_SECRET", "")
if len(GUEST_TOKEN_JWT_SECRET) < 32 or GUEST_TOKEN_JWT_SECRET.lower() in _PUBLISHED_GUEST_SECRETS:
    raise RuntimeError("SUPERSET_GUEST_TOKEN_SECRET must be a random value of at least 32 characters (unset: "
                       "deploy/superset/bootstrap.sh generates one); refusing to start with a known or short secret")
GUEST_TOKEN_JWT_EXP_SECONDS = int(os.environ.get("SUPERSET_GUEST_TOKEN_TTL", "300"))

# Who may frame Superset: the AnalystOS UI origins (comma or space separated). Only http(s)://host[:port]
# entries are kept, so a stray "*" or "https:" cannot open framing to every site. Each embedded dashboard also
# carries its own allowed-domains list, checked against the referrer.
EMBED_ORIGINS = " ".join(
    o for o in os.environ.get("SUPERSET_EMBED_ORIGINS", "http://localhost:5173 http://127.0.0.1:5173").replace(",", " ").split()
    if o.split("://", 1)[0] in ("http", "https") and "://" in o and o.split("://", 1)[1].strip("/") and "*" not in o)
HTTP_HEADERS = {"Content-Security-Policy": f"frame-ancestors 'self' {EMBED_ORIGINS}"}
