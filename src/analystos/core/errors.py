"""Error taxonomy (FND-014). Retryability is a property of the error class, not of call sites."""
from __future__ import annotations


class AnalystOSError(Exception):
    code = "internal_error"
    retryable = False
    http_status = 500

    def __init__(self, message: str, *, details: dict | None = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "retryable": self.retryable, "details": self.details}


class NotFound(AnalystOSError):
    code, http_status = "not_found", 404


class Conflict(AnalystOSError):
    code, http_status = "conflict", 409


class InvalidInput(AnalystOSError):
    code, http_status = "invalid_input", 422


class IdempotencyConflict(Conflict):
    """The `Idempotency-Key` was already used for a different request body (workbench API §1)."""

    code = "idempotency_conflict"


class IdempotencyInProgress(Conflict):
    """The first request with this key is still executing; retry after it finishes."""

    code, retryable = "idempotency_in_progress", True


class PreconditionFailed(AnalystOSError):
    """`If-Match` names a revision that is no longer current (optimistic concurrency)."""

    code, http_status = "precondition_failed", 412


class PreconditionRequired(AnalystOSError):
    """An edit of a revisioned resource without `If-Match`."""

    code, http_status = "precondition_required", 428


class UnsupportedCapability(InvalidInput):
    """A typed request whose payload exists as a contract but has no executor yet (e.g. MLSpec before P5)."""

    code = "unsupported_capability"


class ReadinessBlocked(Conflict):
    """A readiness check the job requires failed (P4-04): correct the input, never retry as is."""

    code = "readiness_blocked"


class MeaningRequired(ReadinessBlocked):
    """Reviewed context and model assistance could not resolve material business meaning."""
    code = "meaning_required"


class Unauthenticated(AnalystOSError):
    code, http_status = "unauthenticated", 401


class Forbidden(AnalystOSError):
    """Authorization or policy denial. Never retryable."""

    code, http_status = "forbidden", 403


class PolicyDenied(Forbidden):
    code = "policy_denied"


class OutputContractViolation(PolicyDenied):
    """An agent tried to persist an output its manifest's `output_contract` does not declare, or whose
    content fails the declared schema (FND-006). Nothing is written; the step's error names the output."""

    code = "output_contract_violation"


class ApprovalRequired(AnalystOSError):
    code, http_status = "approval_required", 409


class SQLRejected(AnalystOSError):
    """The gateway refused a statement (not read-only, out of scope, restricted column...)."""

    code, http_status = "sql_rejected", 422


class NotAnswerable(AnalystOSError):
    """The data in scope cannot answer the question (or it asks Ask to change data): a refusal, never a
    placeholder answer. Raised by the Ask shape check or when the model itself says it cannot answer."""

    code, http_status = "not_answerable", 422


class QueryTimeout(AnalystOSError):
    code, http_status, retryable = "query_timeout", 504, False


class UpstreamUnavailable(AnalystOSError):
    """A dependency (source, model provider, BI tool) failed transiently."""

    code, http_status, retryable = "upstream_unavailable", 503, True


class BudgetExceeded(AnalystOSError):
    code, http_status = "budget_exceeded", 429


class SpendCapReached(BudgetExceeded):
    """A hard spend cap (platform daily or workspace monthly) would be exceeded by this call's
    reserved estimate. The call is not sent; the caller takes its deterministic path. `details`
    carries cap, spent, limit, estimate and the remedy the UI shows."""

    code = "spend_cap_reached"


class SpendCountersUnavailable(BudgetExceeded):
    """The atomic spend counters (Redis) are unreachable, so a reservation cannot be proven within
    the caps: billable model calls fail closed until they return."""

    code, http_status, retryable = "spend_counters_unavailable", 503, True


class ModelRouteUnavailable(AnalystOSError):
    """No allowed provider could serve a model profile. Fail closed: never route elsewhere silently."""

    code, http_status, retryable = "model_route_unavailable", 503, True


class RunCancelled(AnalystOSError):
    code, http_status = "run_cancelled", 409


class ProviderQuotaExhausted(ModelRouteUnavailable):
    """The provider refused for payment/credit reasons (HTTP 402). Every model behind that provider
    will fail the same way, so the router stops trying it for a cooldown instead of burning calls."""

    code, retryable = "provider_quota_exhausted", False


class LLMDisabled(ModelRouteUnavailable):
    """An administrator set this purpose to `off` (or the prompt was refused as oversize).
    Callers take their deterministic path; this is a decision, not an outage."""

    code, retryable = "llm_disabled", False


class EscalationUnavailable(LLMDisabled):
    """A caller asked for the large tier after a validation failure, but the purpose's escalation
    policy is `never`/`always_large`, the profile has no large tier, or the run was downgraded."""

    code = "escalation_unavailable"


class EgressBlocked(ModelRouteUnavailable):
    """The model transport refused a host that is not a configured provider endpoint (or, on an
    air-gapped install, not an internal one). A configuration error, never retried."""

    code, retryable = "egress_blocked", False


class ModelKeyMissing(ModelRouteUnavailable):
    """The provider needs an API key and the process environment has none (`details.env`). The key is
    read from this process's environment only, so the API and worker processes each need it."""

    code, retryable = "no_api_key", False


class ModelPolicyBlocked(ModelRouteUnavailable):
    """The workspace provider list or the air-gapped install excludes every model of the profile."""

    code, retryable = "policy_blocked", False


class ModelResidencyBlocked(ModelRouteUnavailable):
    """No model of the profile has a known region matching the workspace data residency."""

    code, retryable = "residency_blocked", False


class ModelOutputInvalid(ModelRouteUnavailable):
    """Every attempt answered, but not with the JSON the purpose needs."""

    code, retryable = "invalid_output", True


class ModelUnavailable(AnalystOSError):
    """A step that needs a model got no usable answer. `details.reason` names the one cause (mode off,
    no API key, provider cooldown, policy, residency, approval, budget or cap, context size, invalid
    output), so the caller can say exactly what to fix instead of "no model route"."""

    code, http_status = "model_unavailable", 503


class ContextOverBudget(AnalystOSError):
    """The mandatory part of a prompt's context alone exceeds the purpose's budget (context
    compiler, P4-T03). Callers take the deterministic path and record the refusal; the context is
    never cut to make it fit."""

    code, http_status = "context_over_budget", 422


class FeatureUnavailable(AnalystOSError):
    """The installation lacks what a feature needs: an optional extra (`pip install analystos[ml]`) or a
    deployment profile (`bi`, `standard`, ...; ADR-0025). `details` names what is missing and the remedy;
    the UI shows it as unavailable with that reason, never as a broken button."""

    code, http_status = "feature_unavailable", 501


class WorkerUnavailable(UpstreamUnavailable):
    """An isolated compute worker was lost mid-task or could not be reached (P7-06). Retried under the
    task's idempotency key; outputs are content-addressed, so a retry that also finishes writes nothing new."""

    code = "worker_unavailable"


class WorkerTaskFailed(AnalystOSError):
    """An isolated job ended without a structured result (killed by a signal, a crash in native code)."""

    code = "worker_task_failed"


def _codes(cls: type[AnalystOSError]) -> dict[str, type[AnalystOSError]]:
    out = {cls.code: cls}
    for sub in cls.__subclasses__():
        for code, found in _codes(sub).items():
            out.setdefault(code, found)
    return out


def error_from_dict(data: dict) -> AnalystOSError:
    """The inverse of `AnalystOSError.to_dict` (worker results, P7-06): the class registered for `code`,
    or a plain AnalystOSError carrying the unknown code. Retryability stays a property of the class, except
    for an unknown code, which keeps what the worker said."""
    code = str(data.get("code") or "internal_error")
    cls = _codes(AnalystOSError).get(code)
    err = (cls or AnalystOSError)(str(data.get("message") or code), details=dict(data.get("details") or {}))
    if cls is None:
        err.code = code
        err.retryable = bool(data.get("retryable"))
    return err
