"""Integration tests act for the default tenant unless they bind another one (S01-T4).

The binding covers this package's fixtures and tests in the main thread. Code that starts its own
threads (the local orchestrator, the in-process scheduler) and API requests (bound from the token by
TenantMiddleware) start without it, so strict mode exercises their own tenant binding."""
from __future__ import annotations

import pytest


@pytest.fixture(scope="package", autouse=True)
def default_tenant():
    from analystos.tenancy import DEFAULT_TENANT_ID, tenant_scope

    with tenant_scope(DEFAULT_TENANT_ID):
        yield DEFAULT_TENANT_ID
