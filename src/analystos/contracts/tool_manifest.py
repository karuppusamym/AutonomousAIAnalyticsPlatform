"""Tool manifest v2 (spec S10 §3, ADR-007): one contract for everything a caller can invoke by id.

Built-in tools, agents, playbooks, admitted external MCP tools and data products (certified metrics,
verified queries, datasets) published as typed tools. Governance reads the manifest: `side_effect`
decides whether a hash-bound approval is needed, `execution.via` names the one deterministic path
that runs it (never caller SQL), `limits` cap rows, time and rate, `exposure` says where it is
offered and `signature` (ed25519, tenant key) proves the registry wrote it. `description` is data
shown to people and models, never instructions (CLAUDE.md rule 7). A data product may carry a `contract`
(freshness SLA, quality-rule refs; review item P2-9) that a `data_contract` monitor checks; it is signed with the
rest, and a manifest without one hashes exactly as before the field existed.
"""
from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from analystos.contracts.quality import DataContract
from analystos.core.ids import stable_hash

API_VERSION = "analystos/tool.v2"
ToolKind = Literal["builtin", "agent", "playbook", "external_mcp", "data_product"]
ToolSideEffect = Literal["none", "write_internal", "write_external"]
DataClassification = Literal["public", "internal", "internal_aggregate", "confidential", "restricted"]
ExecutionVia = Literal["semantic_compiler", "verified_query", "dataset", "playbook", "mcp", "builtin"]
TOOL_STATUSES = ("certified", "quarantined", "refused", "deprecated", "retired")
INVOCABLE_STATUSES = ("certified", "deprecated")  # deprecated only when pinned explicitly
TOOL_ID = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+)+$")
SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
PIN = re.compile(r"^\d+(\.\d+){0,2}$")
# Which execution path a kind may use: a data product can never run a playbook or a remote call.
KIND_PATHS: dict[str, tuple[str, ...]] = {
    "data_product": ("semantic_compiler", "verified_query", "dataset"),
    "playbook": ("playbook",),
    "external_mcp": ("mcp",),
    "builtin": ("builtin",),
    "agent": ("builtin",),
}


class ToolExecutionSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    via: ExecutionVia
    source: str | None = None  # source id the statement runs against (None: the workspace's only source)
    ref: dict[str, Any] = Field(default_factory=dict)  # what is executed: {metric, metric_version} | {verified_query_id} ...


class ToolLimits(BaseModel):
    model_config = ConfigDict(extra="forbid")
    timeout_s: int = Field(default=30, ge=1, le=600)
    max_rows: int = Field(default=5000, ge=1, le=50_000)
    rate_per_min: int = Field(default=60, ge=1, le=10_000)


class ToolExposure(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rest: bool = True
    mcp: bool = True
    grants_required: list[str] = Field(default_factory=list)  # grant entries that cover it (id or family pattern)


class ToolSignature(BaseModel):
    model_config = ConfigDict(extra="forbid")
    alg: Literal["ed25519"] = "ed25519"
    key_id: str = Field(min_length=1, max_length=80)
    value: str = Field(min_length=16, max_length=200)  # base64url of the 64-byte signature


class ToolManifestV2(BaseModel):
    model_config = ConfigDict(extra="forbid")
    apiVersion: Literal["analystos/tool.v2"] = API_VERSION
    tool_id: str = Field(max_length=200)
    version: str
    kind: ToolKind
    owner: str = Field(min_length=1, max_length=120)
    title: str = Field(default="", max_length=300)
    description: str = Field(default="", max_length=2000)
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] = Field(default_factory=dict)
    side_effect: ToolSideEffect = "write_external"  # unknown is the most dangerous class
    permissions: list[str] = Field(default_factory=list)
    data_classification_max: DataClassification = "internal"
    execution: ToolExecutionSpec
    limits: ToolLimits = Field(default_factory=ToolLimits)
    exposure: ToolExposure = Field(default_factory=ToolExposure)
    contract: DataContract | None = None  # data products only (P2-9)
    signature: ToolSignature | None = None

    @field_validator("tool_id")
    @classmethod
    def _tool_id(cls, v: str) -> str:
        if not TOOL_ID.match(v):
            raise ValueError(f"tool_id {v!r} must look like '<family>.<name>' in lower snake case")
        return v

    @field_validator("version")
    @classmethod
    def _version(cls, v: str) -> str:
        if not SEMVER.match(v):
            raise ValueError(f"version {v!r} must be MAJOR.MINOR.PATCH")
        return v

    @field_validator("input_schema", "output_schema")
    @classmethod
    def _schema(cls, v: dict[str, Any]) -> dict[str, Any]:
        if not v:
            return v
        from jsonschema import Draft202012Validator
        from jsonschema.exceptions import SchemaError

        try:
            Draft202012Validator.check_schema(v)
        except SchemaError as exc:
            raise ValueError(f"not a valid JSON Schema: {exc.message}") from None
        return v

    @model_validator(mode="after")
    def _consistent(self) -> ToolManifestV2:
        if self.input_schema.get("type") != "object":
            raise ValueError("input_schema must be a JSON-Schema object")
        if self.execution.via not in KIND_PATHS[self.kind]:
            raise ValueError(f"a {self.kind} tool cannot execute via {self.execution.via}")
        if self.kind == "data_product" and self.side_effect != "none":
            raise ValueError("a data product is read-only (side_effect: none)")
        if self.contract is not None and self.kind != "data_product":
            raise ValueError("only a data product carries a contract")
        return self

    @property
    def major(self) -> int:
        return int(self.version.split(".", 1)[0])

    @property
    def ref(self) -> str:
        return f"{self.tool_id}@{self.version}"

    def signing_payload(self) -> dict[str, Any]:
        """What the signature covers: every field but the signature itself (no `contract` key when there is
        none, so manifests signed before P2-9 still verify)."""
        return self.model_dump(mode="json", exclude={"signature"} | ({"contract"} if self.contract is None else set()))

    def content_hash(self) -> str:
        return stable_hash(self.signing_payload())


def semver_key(version: str) -> tuple[int, int, int]:
    m = SEMVER.match(version)
    if not m:
        return (-1, -1, -1)
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def matches_pin(version: str, pin: str) -> bool:
    """`4` matches 4.x.y, `4.1` matches 4.1.y, `4.1.0` matches exactly."""
    return semver_key(version)[:len(pin.split("."))] == tuple(int(p) for p in pin.split("."))


def split_ref(ref: str) -> tuple[str, str | None]:
    """`metric.x@4` -> ("metric.x", "4"); a pin that is not 1-3 dot-separated integers is refused."""
    tool_id, sep, pin = (ref or "").partition("@")
    if sep and not PIN.match(pin):
        raise ValueError(f"version pin {pin!r} must look like 4, 4.1 or 4.1.0")
    return tool_id, (pin or None)
