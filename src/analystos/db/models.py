"""Control-plane schema (FND-005/FND-009). Lives in the `analystos` database only.

The analytics plane (staged source data) is a different database reached through different
credentials; nothing in here is visible to user or model SQL.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Computed,
    DateTime,
    FetchedValue,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column

from analystos.db.base import Base
from analystos.db.vectors import Embedding, uses_pgvector

EMBEDDING_DIM = 256
# Knowledge-index vectors (P4-K10): the column is created at this dimension; `analystos knowledge
# reembed` retypes it when the configured provider needs another (knowledge/embeddings.py).
KNOWLEDGE_EMBEDDING_DIM = 256
JSON = JSONB


def _ts() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


def by_code(column) -> tuple:
    """ORDER BY a run-scoped sequential code (`H-2` before `H-10`). `created_at` is the transaction
    time, so rows written together tie on it and would come back in whatever order the plan reads them."""
    return func.length(column), column


class Tenant(Base):
    """The isolation boundary above workspaces (ADR-009). Row-level security binds every tenant-owned
    row to one tenant; see analystos.tenancy."""

    __tablename__ = "tenant"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    slug: Mapped[str] = mapped_column(String(80), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(20), default="active", server_default="active")
    data_residency: Mapped[str | None] = mapped_column(String(20), nullable=True)
    settings: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, server_default="{}")
    created_at: Mapped[datetime] = _ts()


class TenantMember(Base):
    """A user's membership of a tenant other than (or in addition to) their home tenant."""

    __tablename__ = "tenant_member"
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenant.id", ondelete="CASCADE"), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("app_user.id", ondelete="CASCADE"), primary_key=True, index=True)
    role: Mapped[str] = mapped_column(String(30), default="member")  # tenant_admin | tenant_auditor | member
    created_at: Mapped[datetime] = _ts()


def _tenant_column() -> Mapped[str]:
    """Filled by the tenancy trigger from the bound tenant (default tenant in system scope). NOT NULL is
    enforced in Postgres by ``tenancy.ddl.install``; the metadata stays nullable for SQLite unit fixtures."""
    return mapped_column(String(40), ForeignKey("tenant.id"), index=True, nullable=True, server_default=FetchedValue())


class User(Base):
    __tablename__ = "app_user"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200))
    password_hash: Mapped[str] = mapped_column(String(200))
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    attributes: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # ABAC inputs (department, pii_clearance)
    created_at: Mapped[datetime] = _ts()
    tenant_id: Mapped[str | None] = _tenant_column()  # home tenant


class UserIdentity(Base):
    """An external identity (OIDC issuer + subject) linked to a local user (SEC-001). Memberships the
    IdP's groups granted are remembered so a later login can revoke exactly those, never a manual grant."""

    __tablename__ = "user_identity"
    __table_args__ = (UniqueConstraint("issuer", "subject"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("app_user.id", ondelete="CASCADE"), index=True)
    issuer: Mapped[str] = mapped_column(String(500))
    subject: Mapped[str] = mapped_column(String(255))
    email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    groups: Mapped[list[str]] = mapped_column(JSON, default=list)
    managed_memberships: Mapped[dict[str, str]] = mapped_column(JSON, default=dict)  # workspace id -> role granted by SSO
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = _ts()


class Workspace(Base):
    __tablename__ = "workspace"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    tenant_id: Mapped[str | None] = _tenant_column()
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    objective: Mapped[str] = mapped_column(Text, default="")
    autonomy_level: Mapped[int] = mapped_column(Integer, default=3)
    status: Mapped[str] = mapped_column(String(20), default="active")
    settings: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    policy_version: Mapped[int] = mapped_column(Integer, default=1)
    created_by: Mapped[str] = mapped_column(ForeignKey("app_user.id"))
    created_at: Mapped[datetime] = _ts()
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Named business and technical owner (P4-09, contracts/pilot.py): {"business": {name, email, user_id?}, ...}
    owners: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, server_default="{}")


class WorkspaceMember(Base):
    __tablename__ = "workspace_member"
    __table_args__ = (UniqueConstraint("workspace_id", "user_id"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspace.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("app_user.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(20))  # owner | editor | analyst | approver | viewer
    created_at: Mapped[datetime] = _ts()


class WorkspacePolicy(Base):
    """Immutable, versioned policy documents. workspace.policy_version points at the effective one."""

    __tablename__ = "workspace_policy"
    __table_args__ = (UniqueConstraint("workspace_id", "version"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspace.id", ondelete="CASCADE"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    document: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_by: Mapped[str] = mapped_column(String(40))
    created_at: Mapped[datetime] = _ts()


class Source(Base):
    __tablename__ = "source"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspace.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(30))  # postgres | sqlserver | csv | servicenow
    name: Mapped[str] = mapped_column(String(200))
    config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # never secret values
    secret_ref: Mapped[str | None] = mapped_column(String(200), nullable=True)  # env:NAME | file:/path
    status: Mapped[str] = mapped_column(String(20), default="registered")
    # Where the gateway queries this source: "pushdown" (in place) or "staged" (analytics DB schema)
    execution_mode: Mapped[str] = mapped_column(String(20), default="staged")
    staging_schema: Mapped[str | None] = mapped_column(String(80), nullable=True)
    last_discovered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = _ts()
    owners: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, server_default="{}")  # as Workspace.owners (P4-09)


class SourceAsset(Base):
    __tablename__ = "source_asset"
    __table_args__ = (UniqueConstraint("source_id", "schema_name", "name"),)
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("source.id", ondelete="CASCADE"), index=True)
    workspace_id: Mapped[str] = mapped_column(String(40), index=True)
    schema_name: Mapped[str] = mapped_column(String(120))  # schema where the gateway sees it
    name: Mapped[str] = mapped_column(String(200))
    source_name: Mapped[str] = mapped_column(String(200))  # name in the origin system (e.g. servicenow table)
    kind: Mapped[str] = mapped_column(String(20), default="table")
    selected: Mapped[bool] = mapped_column(Boolean, default=False)
    row_count: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    freshness_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    business_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    stats: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # Crawler state: structural fingerprint, lifecycle and deterministic semantics (role/domain/grain).
    fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lifecycle: Mapped[str] = mapped_column(String(20), default="active", server_default="active")  # active | deprecated
    semantics: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, server_default="{}")
    description_origin: Mapped[str | None] = mapped_column(String(20), nullable=True)  # source | rule | model | user
    business_name_origin: Mapped[str | None] = mapped_column(String(20), nullable=True)  # source | rule | model | user
    reviewed: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    last_crawled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # What the staged snapshot is relative to its origin: rows staged, origin total, truncated, sampling (P4-C12)
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, server_default="{}")
    created_at: Mapped[datetime] = _ts()


class SourceColumn(Base):
    __tablename__ = "source_column"
    __table_args__ = (UniqueConstraint("asset_id", "name"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    asset_id: Mapped[str] = mapped_column(ForeignKey("source_asset.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    ordinal: Mapped[int] = mapped_column(Integer, default=0)
    data_type: Mapped[str] = mapped_column(String(80))
    semantic_type: Mapped[str | None] = mapped_column(String(40), nullable=True)  # numeric|categorical|datetime|boolean|text|id
    nullable: Mapped[bool] = mapped_column(Boolean, default=True)
    is_key: Mapped[bool] = mapped_column(Boolean, default=False)
    business_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # A person's business name / description is `user`: no crawl or knowledge ingest overwrites it.
    business_name_origin: Mapped[str | None] = mapped_column(String(20), nullable=True)  # source | rule | user
    description_origin: Mapped[str | None] = mapped_column(String(20), nullable=True)  # source | rule | user
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)  # pii, sensitive, restricted
    profile: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    semantics: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, server_default="{}")  # role, unit, pii, glossary
    # Tags set by a person are never removed by a crawl; crawler tags can only be added (tighten, never loosen).
    tags_origin: Mapped[str] = mapped_column(String(20), default="crawler", server_default="crawler")  # crawler | user
    # P7-20: a user-curated column missing from one crawl is kept (curation intact) and marked absent; it is
    # hidden from every ORM query (`db/column_presence.py`) until a crawl sees it again and clears this.
    absent_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Relationship(Base):
    __tablename__ = "relationship"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(40), index=True)
    from_asset_id: Mapped[str] = mapped_column(String(40))
    from_column: Mapped[str] = mapped_column(String(200))
    to_asset_id: Mapped[str] = mapped_column(String(40))
    to_column: Mapped[str] = mapped_column(String(200))
    cardinality: Mapped[str] = mapped_column(String(20), default="many_to_one")
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    validated: Mapped[bool] = mapped_column(Boolean, default=False)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    origin: Mapped[str] = mapped_column(String(20), default="discovered")  # discovered | context | user
    created_at: Mapped[datetime] = _ts()


class ContextEntry(Base):
    """Semantic + episodic memory: glossary terms, definitions, metrics, rules, notes, prior-run episodes."""

    __tablename__ = "context_entry"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    # Platform-wide knowledge lives in the read-only `platform` knowledge pack (P4-K01), never here.
    workspace_id: Mapped[str] = mapped_column(String(40), index=True, nullable=False)
    kind: Mapped[str] = mapped_column(String(30))  # term | definition | metric | rule | note | episode | dashboard
    name: Mapped[str] = mapped_column(String(300))
    body: Mapped[str] = mapped_column(Text)
    synonyms: Mapped[list[str]] = mapped_column(JSON, default=list)
    mapped_columns: Mapped[list[str]] = mapped_column(JSON, default=list)  # "schema.table.column"
    origin: Mapped[str] = mapped_column(String(30), default="user")  # user | context2ai | agent
    trusted: Mapped[bool] = mapped_column(Boolean, default=True)
    embedding: Mapped[list[float] | None] = mapped_column(Embedding(EMBEDDING_DIM), nullable=True)
    created_at: Mapped[datetime] = _ts()


class KnowledgePack(Base):
    """An OKF v0.2 knowledge pack: the system of record for knowledge (P4-K01, ADR-0013). `platform`
    (exactly one, no workspace, read-only), `workspace` (a workspace's own) or `imported` (an Atlas
    or other OKF bundle, read-only, replaced only by re-import). Content is its revisions."""

    __tablename__ = "knowledge_pack"
    __table_args__ = (
        UniqueConstraint("workspace_id", "slug"),
        CheckConstraint("(kind = 'platform') = (workspace_id IS NULL)", name="ck_knowledge_pack_platform_scope"),
        Index("uq_knowledge_pack_platform_slug", "slug", unique=True, postgresql_where="workspace_id IS NULL"),
    )
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    workspace_id: Mapped[str | None] = mapped_column(ForeignKey("workspace.id", ondelete="CASCADE"), index=True, nullable=True)
    kind: Mapped[str] = mapped_column(String(20))  # platform | workspace | imported
    slug: Mapped[str] = mapped_column(String(120))
    title: Mapped[str] = mapped_column(String(300), default="")
    read_only: Mapped[bool] = mapped_column(Boolean, default=False)
    okf_version: Mapped[str] = mapped_column(String(10), default="0.2")
    okf_spec_revision: Mapped[str] = mapped_column(String(64))
    okf_spec_sha256: Mapped[str] = mapped_column(String(64))
    okf_root: Mapped[str] = mapped_column(String(200), default="")  # bundle root inside the pack ("bundle" for Atlas)
    head_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    git_remote: Mapped[str | None] = mapped_column(String(500), nullable=True)
    git_branch: Mapped[str] = mapped_column(String(120), default="main")
    origin: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # importer / provider configuration
    created_at: Mapped[datetime] = _ts()
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class KnowledgeObject(Base):
    """Content-addressed file bytes of a pack (sha256 of the bytes). Immutable; shared by revisions."""

    __tablename__ = "knowledge_object"
    pack_id: Mapped[str] = mapped_column(ForeignKey("knowledge_pack.id", ondelete="CASCADE"), primary_key=True)
    sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    size: Mapped[int] = mapped_column(Integer)
    content: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = _ts()


class KnowledgeRevision(Base):
    """One immutable revision of a pack: the full {path: sha256} file map, who and why."""

    __tablename__ = "knowledge_revision"
    __table_args__ = (UniqueConstraint("pack_id", "number"),)
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    pack_id: Mapped[str] = mapped_column(ForeignKey("knowledge_pack.id", ondelete="CASCADE"), index=True)
    number: Mapped[int] = mapped_column(Integer)
    parent_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    author: Mapped[str] = mapped_column(String(200))
    reason: Mapped[str] = mapped_column(Text, default="")
    origin: Mapped[str] = mapped_column(String(30))  # seed | migration | user | okf_import | crawler | learning
    files: Mapped[dict[str, str]] = mapped_column(JSON, default=dict)
    content_digest: Mapped[str] = mapped_column(String(64))
    meta: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # conformance, import report, remote commit
    created_at: Mapped[datetime] = _ts()


class KnowledgeDocument(Base):
    """Index (rebuildable from the pack): one OKF concept document of a pack's head revision."""

    __tablename__ = "knowledge_document"
    __table_args__ = (UniqueConstraint("pack_id", "path"),)
    id: Mapped[str] = mapped_column(String(40), primary_key=True)  # deterministic: pack + path
    pack_id: Mapped[str] = mapped_column(ForeignKey("knowledge_pack.id", ondelete="CASCADE"), index=True)
    workspace_id: Mapped[str | None] = mapped_column(String(40), index=True, nullable=True)  # NULL = platform pack
    revision: Mapped[int] = mapped_column(Integer)
    path: Mapped[str] = mapped_column(String(500))
    concept_id: Mapped[str] = mapped_column(String(500))
    type: Mapped[str] = mapped_column(String(120), index=True)
    kind: Mapped[str] = mapped_column(String(40), index=True)  # term | metric | rule | ... | document (entries.doc_kind)
    title: Mapped[str] = mapped_column(String(500))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="stable")
    trust_tier: Mapped[str] = mapped_column(String(30), default="unverified")
    stale_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    frontmatter: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    body: Mapped[str] = mapped_column(Text, default="")
    sha256: Mapped[str] = mapped_column(String(64))
    size: Mapped[int] = mapped_column(Integer)


class KnowledgeSection(Base):
    """Index: one top-level section of a document, with its full-text vector and embedding."""

    __tablename__ = "knowledge_section"
    __table_args__ = (
        UniqueConstraint("document_id", "anchor"),
        Index("ix_knowledge_section_tsv", "tsv", postgresql_using="gin"),
        # The HNSW index exists only with pgvector; the `array` backend ranks in-process (db/vectors.py).
        Index("ix_knowledge_section_embedding_hnsw", "embedding", postgresql_using="hnsw",
              postgresql_ops={"embedding": "vector_cosine_ops"}).ddl_if(callable_=lambda *_a, **_k: uses_pgvector()),
    )
    id: Mapped[str] = mapped_column(String(40), primary_key=True)  # deterministic: document + anchor
    document_id: Mapped[str] = mapped_column(ForeignKey("knowledge_document.id", ondelete="CASCADE"), index=True)
    pack_id: Mapped[str] = mapped_column(String(40), index=True)
    workspace_id: Mapped[str | None] = mapped_column(String(40), index=True, nullable=True)
    ordinal: Mapped[int] = mapped_column(Integer)
    anchor: Mapped[str] = mapped_column(String(200))
    heading: Mapped[str] = mapped_column(String(500), default="")
    text: Mapped[str] = mapped_column(Text, default="")
    sha256: Mapped[str] = mapped_column(String(64))
    search_text: Mapped[str] = mapped_column(Text, default="")
    tsv = mapped_column(TSVECTOR, Computed("to_tsvector('english', coalesce(search_text, ''))", persisted=True))
    embedding: Mapped[list[float] | None] = mapped_column(Embedding(KNOWLEDGE_EMBEDDING_DIM), nullable=True)
    embedding_model: Mapped[str | None] = mapped_column(String(200), nullable=True)


class KnowledgeLink(Base):
    """Index: the link graph (§6.1 links are untyped directed edges)."""

    __tablename__ = "knowledge_link"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    pack_id: Mapped[str] = mapped_column(String(40), index=True)
    workspace_id: Mapped[str | None] = mapped_column(String(40), index=True, nullable=True)
    source_path: Mapped[str] = mapped_column(String(500))
    raw: Mapped[str] = mapped_column(String(2000))
    kind: Mapped[str] = mapped_column(String(20))  # internal | external | fragment | invalid
    target_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    resolved: Mapped[bool] = mapped_column(Boolean, default=False)


class KnowledgeIndexState(Base):
    """What the index was built from: per pack the indexed revision (`pack:<id>`), plus the embedding
    provider (`embedding`), so `refresh` finds stale packs and queries embed with the index's provider."""

    __tablename__ = "knowledge_index_state"
    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class KnowledgeSuggestion(Base):
    """A knowledge draft waiting for review (P4-K07/K08): proposed by a model (crawler enrichment) or
    by the learning loop (an accepted finding, an approved KPI, a user correction). Every field
    carries its own value, confidence and provenance. Nothing here reaches a prompt: an approval
    writes the document into the workspace pack (a revision), a rejection writes negative
    knowledge there instead. `subject` names what the draft is about (`asset:<id>`, `insight:<id>`,
    `metric:<name>@v<n>`, `feedback:<id>`, ...)."""

    __tablename__ = "knowledge_suggestion"
    __table_args__ = (UniqueConstraint("workspace_id", "subject", "content_hash", name="uq_knowledge_suggestion_content"),
                      Index("ix_knowledge_suggestion_queue", "workspace_id", "status", "created_at"))
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspace.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(40))  # term|definition|metric|rule|note|attested_computation|table_description|negative
    subject: Mapped[str] = mapped_column(String(200))
    title: Mapped[str] = mapped_column(String(300))
    path: Mapped[str] = mapped_column(String(500))  # the workspace-pack path an approval writes
    fields: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # {name: {value, confidence, provenance}}
    confidence: Mapped[float] = mapped_column(Float, default=0.0)  # the least confident field
    origin: Mapped[str] = mapped_column(String(60))  # crawler.enrichment | learning.finding | learning.metric | learning.feedback
    proposed_by: Mapped[str] = mapped_column(String(80))  # model:<id> | process:<name> | user:<id>
    batch: Mapped[str | None] = mapped_column(String(80), nullable=True)  # crawl run, run, request that produced it
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending|approved|rejected|superseded
    content_hash: Mapped[str] = mapped_column(String(64))
    decided_by: Mapped[str | None] = mapped_column(String(40), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    revision: Mapped[int | None] = mapped_column(Integer, nullable=True)  # workspace-pack revision the decision wrote
    created_at: Mapped[datetime] = _ts()
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class AgentDefinition(Base):
    __tablename__ = "agent_definition"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    version: Mapped[str] = mapped_column(String(20))
    spec: Mapped[dict[str, Any]] = mapped_column(JSON)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class WorkspaceCapability(Base):
    """Per-workspace enablement of a capability (spec v3 §3.1). No row = the default: on for the
    built-in set `playbook.investigate` needs (and built-in tools), off for everything else."""

    __tablename__ = "workspace_capability"
    __table_args__ = (UniqueConstraint("workspace_id", "capability_id"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspace.id", ondelete="CASCADE"), index=True)
    capability_id: Mapped[str] = mapped_column(String(160))
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_by: Mapped[str] = mapped_column(String(40))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class ToolDefinition(Base):
    __tablename__ = "tool_definition"
    id: Mapped[str] = mapped_column(String(120), primary_key=True)
    spec: Mapped[dict[str, Any]] = mapped_column(JSON)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class SkillDefinition(Base):
    __tablename__ = "skill_definition"
    id: Mapped[str] = mapped_column(String(120), primary_key=True)
    spec: Mapped[dict[str, Any]] = mapped_column(JSON)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class AnalysisRun(Base):
    __tablename__ = "analysis_run"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspace.id", ondelete="CASCADE"), index=True)
    objective: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(30), default="NEW")
    autonomy_level: Mapped[int] = mapped_column(Integer, default=3)
    plan: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    plan_version: Mapped[int] = mapped_column(Integer, default=0)
    plan_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    policy_version: Mapped[int] = mapped_column(Integer, default=1)
    scope: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # authorized data scope snapshot
    instructions: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)  # user redirects, in order
    constraints: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # compiled filters from redirects
    control: Mapped[str] = mapped_column(String(20), default="run")  # run | pause | cancel
    requested_by: Mapped[str] = mapped_column(String(40))
    workflow_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    iteration: Mapped[int] = mapped_column(Integer, default=0)
    tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # How the run was started: {"type": "user"} or {"type": "schedule", "schedule_id", "schedule_run_id",
    # "previous_run_id", "publish": "skip"|"propose", "report": {...}} or {"type": "alert", "alert_id"}
    origin: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # Capability bindings (spec v3 §3.1, P4-X01): {"playbook": id, "refs": ["id@version", ...],
    # "manifests": {id: manifest}, "skipped": {step: reason}}. Bound when the plan materializes; the run
    # keeps these versions after a registry reload, and the refs are part of the plan hash.
    capabilities: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, server_default="{}")
    # Data-version manifest (P4-03): the version of every analysed asset the run read (evidence.manifest).
    data_manifest: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, server_default="{}")
    created_at: Mapped[datetime] = _ts()
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class RunTask(Base):
    __tablename__ = "run_task"
    __table_args__ = (UniqueConstraint("run_id", "key"),)
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("analysis_run.id", ondelete="CASCADE"), index=True)
    key: Mapped[str] = mapped_column(String(120))  # stable idempotency key within the run
    agent_id: Mapped[str] = mapped_column(String(80))
    title: Mapped[str] = mapped_column(String(300))
    status: Mapped[str] = mapped_column(String(30), default="NEW")
    depends_on: Mapped[list[str]] = mapped_column(JSON, default=list)
    input: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    output: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    plan_version: Mapped[int] = mapped_column(Integer, default=1)
    seq: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)  # claim time
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Bumped by every claim: a claim is a compare-and-set on it (P4-S02), and an attempt whose claim was
    # retaken (timed out, released) cannot write its result over the new attempt's.
    claim_version: Mapped[int] = mapped_column(Integer, default=0, server_default="0")


class RunEvent(Base):
    """Persisted event stream (§42). The SSE endpoint and activity feed read from here."""

    __tablename__ = "run_event"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    workspace_id: Mapped[str] = mapped_column(String(40), index=True)
    run_id: Mapped[str | None] = mapped_column(String(40), index=True, nullable=True)
    type: Mapped[str] = mapped_column(String(60))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    actor: Mapped[str | None] = mapped_column(String(80), nullable=True)
    created_at: Mapped[datetime] = _ts()


class AgentMessage(Base):
    __tablename__ = "agent_message"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(40), index=True)
    task_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    agent_id: Mapped[str] = mapped_column(String(80))
    kind: Mapped[str] = mapped_column(String(30))  # thought | decision | observation | user
    content: Mapped[str] = mapped_column(Text)
    data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = _ts()


class ModelCall(Base):
    __tablename__ = "model_call"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    workspace_id: Mapped[str | None] = mapped_column(String(40), index=True, nullable=True)
    run_id: Mapped[str | None] = mapped_column(String(40), index=True, nullable=True)
    task_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    agent_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    purpose: Mapped[str] = mapped_column(String(60))
    profile: Mapped[str] = mapped_column(String(60))
    provider: Mapped[str] = mapped_column(String(40))
    model: Mapped[str] = mapped_column(String(120))
    prompt_version: Mapped[str | None] = mapped_column(String(60), nullable=True)
    status: Mapped[str] = mapped_column(String(20))
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    tokens_saved: Mapped[int] = mapped_column(Integer, default=0, server_default="0")  # cache hits and deterministic skips
    request_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Replay (P4-C09): content addresses of the redacted request and response in model_payload.
    request_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    response_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Execution ladder (P4-T01): the rung that answered — cache | registry | rules | decision | llm_small | llm_large.
    answered_by: Mapped[str] = mapped_column(String(20), default="llm_large", server_default="llm_large")
    # P4-T07: provider | price_table@<prices_version> | missing_price | none (no billable call).
    cost_source: Mapped[str | None] = mapped_column(String(60), nullable=True)
    # Prompt caching (P4-T04): prompt tokens the provider served from its cache (share = / input_tokens).
    cached_input_tokens: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # Context compiler receipts (P4-T03): the context items the prompt carried ("context used").
    context_receipts: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, nullable=True)
    # Cheap first, escalate: set on a call of the large tier made because a small-tier answer failed
    # deterministic validation (the small model, and why: invalid_json, schema, sql_rejected, ...).
    escalated_from: Mapped[str | None] = mapped_column(String(120), nullable=True)
    escalation_reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = _ts()

    __table_args__ = (Index("ix_model_call_created_purpose_rung", "created_at", "purpose", "answered_by"),)


class ToolExecution(Base):
    __tablename__ = "tool_execution"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    workspace_id: Mapped[str] = mapped_column(String(40), index=True)
    run_id: Mapped[str | None] = mapped_column(String(40), index=True, nullable=True)
    task_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    agent_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    tool_id: Mapped[str] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(20))  # ok | error | denied
    input: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    output: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    decision: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = _ts()


class QueryExecution(Base):
    __tablename__ = "query_execution"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(40), index=True)
    source_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    run_id: Mapped[str | None] = mapped_column(String(40), index=True, nullable=True)
    task_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    actor: Mapped[str] = mapped_column(String(80))  # user:<id> | agent:<id>
    purpose: Mapped[str] = mapped_column(String(120), default="analysis")
    sql: Mapped[str] = mapped_column(Text)
    executed_sql: Mapped[str | None] = mapped_column(Text, nullable=True)
    fingerprint: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    status: Mapped[str] = mapped_column(String(20))  # ok | rejected | error | timeout
    rejected_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    referenced_assets: Mapped[list[str]] = mapped_column(JSON, default=list)
    row_count: Mapped[int] = mapped_column(Integer, default=0)
    truncated: Mapped[bool] = mapped_column(Boolean, default=False)
    columns: Mapped[list[str]] = mapped_column(JSON, default=list)
    result_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    result_preview: Mapped[list[Any]] = mapped_column(JSON, default=list)  # first rows (masked), for evidence UI
    cache_hit: Mapped[bool] = mapped_column(Boolean, default=False)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = _ts()


class Hypothesis(Base):
    __tablename__ = "hypothesis"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(40), index=True)
    run_id: Mapped[str] = mapped_column(String(40), index=True)
    code: Mapped[str] = mapped_column(String(20))
    question: Mapped[str] = mapped_column(Text, default="")
    statement: Mapped[str] = mapped_column(Text)
    spec: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # executable analysis spec
    provenance: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, server_default="{}")
    priority: Mapped[str] = mapped_column(String(10), default="medium")
    priority_score: Mapped[float] = mapped_column(Float, default=0.5)
    status: Mapped[str] = mapped_column(String(20), default="proposed")
    methods: Mapped[list[str]] = mapped_column(JSON, default=list)
    evidence: Mapped[list[str]] = mapped_column(JSON, default=list)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    conclusion: Mapped[str | None] = mapped_column(Text, nullable=True)
    parent_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    iteration: Mapped[int] = mapped_column(Integer, default=1)
    origin: Mapped[str] = mapped_column(String(20), default="agent")  # agent | user | heuristic
    created_at: Mapped[datetime] = _ts()
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class Experiment(Base):
    """One analysis method applied to one hypothesis (§44 experiment/analysis_result)."""

    __tablename__ = "experiment"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(40), index=True)
    run_id: Mapped[str] = mapped_column(String(40), index=True)
    hypothesis_id: Mapped[str | None] = mapped_column(String(40), index=True, nullable=True)
    method: Mapped[str] = mapped_column(String(60))
    params: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    result: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    query_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    role: Mapped[str] = mapped_column(String(20), default="primary")  # primary | verification
    created_at: Mapped[datetime] = _ts()


class Insight(Base):
    __tablename__ = "insight"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(40), index=True)
    run_id: Mapped[str] = mapped_column(String(40), index=True)
    hypothesis_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    code: Mapped[str] = mapped_column(String(20))
    title: Mapped[str] = mapped_column(String(300))
    finding: Mapped[str] = mapped_column(Text)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    population_size: Mapped[int] = mapped_column(BigInteger, default=0)
    business_impact: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    caveats: Mapped[list[str]] = mapped_column(JSON, default=list)
    evidence: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)  # [{type, id, label}]
    verified: Mapped[bool] = mapped_column(Boolean, default=False)
    verification: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # REV record
    status: Mapped[str] = mapped_column(String(20), default="draft")  # draft|verified|failed_verification|rejected
    narrative_source: Mapped[str] = mapped_column(String(160), default="template")  # llm | template
    # Typed evidence (P4-03): the versioned EvidenceBundle (facts, data, method, validation, limits), its
    # validation state (exploratory | replicated | confirmed | inconclusive | invalid | insufficient_evidence
    # | legacy), the data-manifest version it was computed on, and when a newer snapshot made it stale.
    evidence_bundle: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, server_default="{}")
    validation: Mapped[str] = mapped_column(String(30), default="exploratory", server_default="legacy")
    data_version: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    stale_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # ADR-0019 (P7-02): `governed` only when computed from a compiled SemanticQuery (then `semantic` holds
    # the model and compiler versions); run findings are `ad_hoc`.
    governance: Mapped[str] = mapped_column(String(10), default="ad_hoc", server_default="ad_hoc")
    semantic: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = _ts()


class VerificationRecord(Base):
    """One verdict bound to its dependency fingerprint (P7-01, ADR-0020). The state is the only thing that
    changes after creation: PENDING -> ACTIVE -> VOID (a dependency changed) | SUPERSEDED (a newer verdict
    on the same subject). LEGACY marks findings verified before fingerprints whose dependencies could not be
    rebuilt. Re-verification writes a new record; a voided one stays readable."""

    __tablename__ = "verification_record"
    __table_args__ = (Index("ix_verification_record_subject", "subject_type", "subject_id"),)
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(40), index=True)
    run_id: Mapped[str | None] = mapped_column(String(40), index=True, nullable=True)
    subject_type: Mapped[str] = mapped_column(String(30))  # insight | chart | kpi_tile | report_section
    subject_id: Mapped[str] = mapped_column(String(80))
    question_hash: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)  # the AnalysisSpec identity
    verdict: Mapped[str] = mapped_column(String(30))  # verified | failed_verification
    checks: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    verifier: Mapped[str] = mapped_column(String(80))  # rev.v<version> | user:<id>
    evidence_bundle_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    fingerprint: Mapped[str] = mapped_column(String(64))
    dependencies: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    state: Mapped[str] = mapped_column(String(20), index=True)  # PENDING | ACTIVE | VOID | SUPERSEDED | LEGACY
    void_kind: Mapped[str | None] = mapped_column(String(20), nullable=True)  # the dependency kind that changed
    void_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    void_detail: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    voided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    superseded_by: Mapped[str | None] = mapped_column(String(40), nullable=True)
    flags: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)  # "wrong" flags, each with its reason
    created_at: Mapped[datetime] = _ts()


class EvidenceCitationSet(Base):
    """The citations of one finding or Ask answer (N-8, `contracts/citations.py`): measured results and
    knowledge documents as separate kinds, where each narrative number comes from, and document claims
    that disagree with measured data. Rewritten when the subject is re-verified or re-answered."""

    __tablename__ = "evidence_citation_set"
    __table_args__ = (UniqueConstraint("subject_type", "subject_id"),)
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(40), index=True)
    run_id: Mapped[str | None] = mapped_column(String(40), index=True, nullable=True)
    subject_type: Mapped[str] = mapped_column(String(30))  # insight | ask_turn
    subject_id: Mapped[str] = mapped_column(String(80))
    version: Mapped[str] = mapped_column(String(30))
    quantitative_count: Mapped[int] = mapped_column(Integer, default=0)
    document_count: Mapped[int] = mapped_column(Integer, default=0)
    conflict_count: Mapped[int] = mapped_column(Integer, default=0, index=True)
    document_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    citations: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = _ts()
    updated_at: Mapped[datetime] = _ts()


class VerificationDependency(Base):
    """The lookup side of a record's fingerprint: events find dependents by (kind, ref)."""

    __tablename__ = "verification_dependency"
    __table_args__ = (Index("ix_verification_dependency_kind_ref", "kind", "ref"),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    record_id: Mapped[str] = mapped_column(ForeignKey("verification_record.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(20))  # query | data | semantic | method | context | model_call | policy
    ref: Mapped[str] = mapped_column(String(300))
    version_hash: Mapped[str] = mapped_column(String(128))


class VerificationSweep(Base):
    """One nightly sweep: how many ACTIVE records it re-checked and how many voids the events had missed
    (`late_voids` is a defect metric: each is a change path without its event hook)."""

    __tablename__ = "verification_sweep"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    actor: Mapped[str] = mapped_column(String(80))
    checked: Mapped[int] = mapped_column(Integer, default=0)
    late_voids: Mapped[int] = mapped_column(Integer, default=0)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime] = _ts()


class Artifact(Base):
    __tablename__ = "artifact"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(40), index=True)
    run_id: Mapped[str | None] = mapped_column(String(40), index=True, nullable=True)
    type: Mapped[str] = mapped_column(String(40), index=True)
    name: Mapped[str] = mapped_column(String(300))
    version: Mapped[int] = mapped_column(Integer, default=1)
    # Plan version of the run that last wrote this artifact; a replan makes lower versions stale.
    plan_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="draft")
    platform: Mapped[str | None] = mapped_column(String(40), nullable=True)
    external_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    external_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    creator_agent: Mapped[str | None] = mapped_column(String(80), nullable=True)
    creator_user: Mapped[str | None] = mapped_column(String(40), nullable=True)
    content: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    content_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = _ts()
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class ArtifactVersion(Base):
    __tablename__ = "artifact_version"
    __table_args__ = (UniqueConstraint("artifact_id", "version"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    artifact_id: Mapped[str] = mapped_column(ForeignKey("artifact.id", ondelete="CASCADE"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    content: Mapped[dict[str, Any]] = mapped_column(JSON)
    content_hash: Mapped[str] = mapped_column(String(64))
    created_by: Mapped[str | None] = mapped_column(String(80), nullable=True)
    created_at: Mapped[datetime] = _ts()


class LineageEdge(Base):
    """Generic provenance edge between any two persisted objects (artifact, insight, query, table...).
    Unique per workspace: name-identified nodes (``table``, ``src_x.orders``) recur across workspaces."""

    __tablename__ = "lineage_edge"
    # The unique key serves lookups by the `from` end; the index serves the `to` end (per-artifact lineage CTE).
    __table_args__ = (UniqueConstraint("workspace_id", "from_type", "from_id", "relation", "to_type", "to_id",
                                       name="uq_lineage_edge_workspace_edge"),
                      Index("ix_lineage_edge_workspace_to", "workspace_id", "to_type", "to_id"))
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    workspace_id: Mapped[str] = mapped_column(String(40), index=True)
    run_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    from_type: Mapped[str] = mapped_column(String(40))
    from_id: Mapped[str] = mapped_column(String(200))
    relation: Mapped[str] = mapped_column(String(40))
    to_type: Mapped[str] = mapped_column(String(40))
    to_id: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = _ts()


class Approval(Base):
    """An approval binds to an immutable action proposal (§39): payload hash + plan hash + policy version."""

    __tablename__ = "approval"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(40), index=True)
    run_id: Mapped[str | None] = mapped_column(String(40), index=True, nullable=True)
    action: Mapped[str] = mapped_column(String(60))  # publish_dashboard | ...
    risk_tier: Mapped[str] = mapped_column(String(10))  # low | medium | high
    destination: Mapped[str | None] = mapped_column(String(120), nullable=True)
    affected_assets: Mapped[list[str]] = mapped_column(JSON, default=list)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    payload_hash: Mapped[str] = mapped_column(String(64))
    plan_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    policy_version: Mapped[int] = mapped_column(Integer)
    requested_by: Mapped[str] = mapped_column(String(80))  # the human who owns the run
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending|approved|rejected|expired|invalidated|executed
    decided_by: Mapped[str | None] = mapped_column(String(40), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = _ts()


class SemanticModel(Base):
    """One version of a workspace's semantic model structure (P4-K03, SEM-001/004): Ossie datasets
    (with fields and dimensions), relationships and `ai_context`. Every change is a new version row;
    the newest non-deprecated version is current. Metrics are versioned separately (`semantic_metric`)
    because each one has its own approval."""

    __tablename__ = "semantic_model"
    __table_args__ = (UniqueConstraint("workspace_id", "version", name="uq_semantic_model_version"),)
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspace.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), default="draft")  # draft | proposed | approved | deprecated
    owner_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    ai_context: Mapped[Any] = mapped_column(JSON, nullable=True)
    datasets: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    relationships: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    custom_extensions: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    origin: Mapped[str] = mapped_column(String(80))  # user | agent:<id> | dbt | ossie
    run_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    content_hash: Mapped[str] = mapped_column(String(64))
    created_by: Mapped[str] = mapped_column(String(80))
    created_at: Mapped[datetime] = _ts()
    # P4-05: an agent-proposed structure version waits for a hash-bound approval (separation of duties).
    approval_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    decided_by: Mapped[str | None] = mapped_column(String(40), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SemanticRelationshipCandidate(Base):
    """P7-09 review queue: a join between two assets, measured through the gateway (containment, each
    side's uniqueness, the cardinality they imply) with Atlas's assessment, waiting for a person.
    `cardinality` is only ever the measured one. Accepting it (a hash-bound approval by someone other
    than the proposer) writes the relationship into the semantic model with validated_at/validated_by."""

    __tablename__ = "semantic_relationship_candidate"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspace.id", ondelete="CASCADE"), index=True)
    source_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    from_asset: Mapped[str] = mapped_column(String(400))
    from_columns: Mapped[list[str]] = mapped_column(JSON, default=list)
    to_asset: Mapped[str] = mapped_column(String(400))
    to_columns: Mapped[list[str]] = mapped_column(JSON, default=list)
    cardinality: Mapped[str] = mapped_column(String(20))  # measured: one_to_one|many_to_one|one_to_many|many_to_many
    containment: Mapped[float] = mapped_column(Float, default=0.0)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # counts and the measuring SQL
    assessment: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # skills.relationships.assess_relationship
    origin: Mapped[str] = mapped_column(String(80))  # discovered | user | agent:<id>
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending | accepted | rejected | superseded
    proposed_by: Mapped[str] = mapped_column(String(40))
    approval_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    decided_by: Mapped[str | None] = mapped_column(String(40), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    relationship_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    measured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    content_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = _ts()


class SemanticMetric(Base):
    """One version of a KPI definition with its own approval (SEM-002/003/005). `proposed` versions
    wait for an approver who is not the proposer; approving one deprecates the name's previous
    approved version. `approval_id` binds the decision to the definition's hash (governance/approvals)."""

    __tablename__ = "semantic_metric"
    __table_args__ = (UniqueConstraint("workspace_id", "name", "version", name="uq_semantic_metric_version"),)
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspace.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(120), index=True)
    version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), default="proposed")  # draft|proposed|approved|deprecated|rejected
    definition: Mapped[dict[str, Any]] = mapped_column(JSON)  # contracts.semantic.SemanticMetricDef
    expression: Mapped[str] = mapped_column(Text)
    normalized_expression: Mapped[str] = mapped_column(Text)
    display_name: Mapped[str | None] = mapped_column(String(300), nullable=True)
    owner_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    proposed_by: Mapped[str] = mapped_column(String(40))  # the human accountable for the proposal
    proposed_via: Mapped[str] = mapped_column(String(80))  # user | agent:<id> | dbt | ossie
    run_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    approval_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    decided_by: Mapped[str | None] = mapped_column(String(40), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = _ts()
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class Publication(Base):
    """Each attempt to write to an external BI destination; enables reconcile-before-retry."""

    __tablename__ = "publication"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(40), index=True)
    run_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    approval_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    destination: Mapped[str] = mapped_column(String(40))
    idempotency_key: Mapped[str] = mapped_column(String(120), unique=True)
    status: Mapped[str] = mapped_column(String(20))  # started | succeeded | partial | failed | rolled_back
    external_ids: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = _ts()
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class BiDashboardImport(Base):
    """One import of an existing BI dashboard (v1 §35, BI-011/012): what the dashboard is (inspection and its
    fingerprint), what it maps to (sources, semantic metrics), whether its numbers re-execute to the same values
    through the gateway (verification), what is wrong (findings) and what the platform proposes. Immutable once
    written except `applied`; a re-import is a new version, so drift since the last import is a fingerprint diff."""

    __tablename__ = "bi_dashboard_import"
    __table_args__ = (UniqueConstraint("workspace_id", "destination", "external_id", "version", name="uq_bi_dashboard_import_version"),)
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspace.id", ondelete="CASCADE"), index=True)
    destination: Mapped[str] = mapped_column(String(40))
    external_id: Mapped[str] = mapped_column(String(200))
    version: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(300))
    status: Mapped[str] = mapped_column(String(20))  # verified | attention | unverified
    fingerprint: Mapped[str] = mapped_column(String(64))
    previous_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    inspection: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    mapping: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    verification: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    findings: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    proposals: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    applied: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    imported_by: Mapped[str] = mapped_column(String(40))
    created_at: Mapped[datetime] = _ts()
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class Feedback(Base):
    __tablename__ = "feedback"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(40), index=True)
    run_id: Mapped[str | None] = mapped_column(String(40), index=True, nullable=True)
    user_id: Mapped[str] = mapped_column(String(40))
    kind: Mapped[str] = mapped_column(String(30))  # redirect | add_context | reject_finding | deeper | edit_metric | edit_hypothesis
    text: Mapped[str] = mapped_column(Text, default="")
    target_type: Mapped[str | None] = mapped_column(String(40), nullable=True)
    target_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    applied: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = _ts()


class AuditEvent(Base):
    __tablename__ = "audit_event"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    workspace_id: Mapped[str | None] = mapped_column(String(40), index=True, nullable=True)
    run_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    actor: Mapped[str] = mapped_column(String(80))
    action: Mapped[str] = mapped_column(String(80))
    target: Mapped[str | None] = mapped_column(String(200), nullable=True)
    decision: Mapped[str | None] = mapped_column(String(30), nullable=True)  # allow | deny | approval_required
    reasons: Mapped[list[str]] = mapped_column(JSON, default=list)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = _ts()


# ------------------------------------------------------------------------------------------ Phase 3
class Schedule(Base):
    """Recurring work (§37). Executes with the owner's CURRENT permissions, re-checked at fire time."""

    __tablename__ = "schedule"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspace.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(30))  # reanalysis | dataset_refresh | report | monitor
    cron: Mapped[str] = mapped_column(String(120))
    timezone: Mapped[str] = mapped_column(String(60), default="UTC")
    config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("app_user.id"))
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True, nullable=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Optimistic concurrency (P4-06): every edit and every accepted upgrade bumps it; PATCH takes If-Match.
    revision: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    # The frozen set a fire replays (ADR-0021, P7-03): definition, capability manifests, semantic model,
    # metric and method versions and the baseline's AnalysisSpecs. Empty until a baseline exists.
    pins: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, server_default="{}")
    # Last computed comparison of the pins with what is current (contracts.definition.PinStatus).
    pin_status: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, server_default="{}")
    created_at: Mapped[datetime] = _ts()


class ScheduleRun(Base):
    """One firing (SCH-005). `fire_key` makes concurrent schedulers and retries idempotent."""

    __tablename__ = "schedule_run"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    schedule_id: Mapped[str] = mapped_column(ForeignKey("schedule.id", ondelete="CASCADE"), index=True)
    workspace_id: Mapped[str] = mapped_column(String(40), index=True)
    fire_key: Mapped[str] = mapped_column(String(120), unique=True)
    scheduled_for: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    trigger: Mapped[str] = mapped_column(String(20), default="cron")  # cron | manual
    status: Mapped[str] = mapped_column(String(20), default="started")  # started | running | succeeded | failed | skipped
    result: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = _ts()
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Monitor(Base):
    """Continuous analytics (§38): a metric or data-quality signal evaluated on a schedule."""

    __tablename__ = "monitor"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspace.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(30))  # metric_threshold | metric_drift | change_point | data_quality
    config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    auto_investigate: Mapped[bool] = mapped_column(Boolean, default=False)
    state: Mapped[str] = mapped_column(String(20), default="unknown")  # unknown | ok | alerting | error
    last_evaluated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_result: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_by: Mapped[str] = mapped_column(String(40))
    created_at: Mapped[datetime] = _ts()
    revision: Mapped[int] = mapped_column(Integer, default=1, server_default="1")  # P4-06: optional If-Match on PATCH


class Alert(Base):
    __tablename__ = "alert"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(40), index=True)
    monitor_id: Mapped[str | None] = mapped_column(String(40), index=True, nullable=True)
    severity: Mapped[str] = mapped_column(String(10))  # info | warning | critical
    title: Mapped[str] = mapped_column(String(300))
    message: Mapped[str] = mapped_column(Text)
    data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    dedupe_key: Mapped[str] = mapped_column(String(200), index=True)
    status: Mapped[str] = mapped_column(String(20), default="open")  # open | acknowledged | resolved
    investigation_run_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    acknowledged_by: Mapped[str | None] = mapped_column(String(40), nullable=True)
    created_at: Mapped[datetime] = _ts()
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Notification(Base):
    """In-app notifications. External delivery (email/webhook) is approval-gated (§39): see `Delivery` (N-3)."""

    __tablename__ = "notification"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    workspace_id: Mapped[str] = mapped_column(String(40), index=True)
    user_id: Mapped[str | None] = mapped_column(String(40), index=True, nullable=True)  # null = all members
    kind: Mapped[str] = mapped_column(String(40))  # alert | report | approval | run
    title: Mapped[str] = mapped_column(String(300))
    body: Mapped[str] = mapped_column(Text, default="")
    link: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # {type, id}
    read_by: Mapped[list[str]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = _ts()


class DeliveryDestination(Base):
    """An external delivery target (N-3): email recipients or a webhook URL. Sending needs an approved
    authorization bound to `destination_hash`; any change to the target clears it (re-approval)."""

    __tablename__ = "delivery_destination"
    __table_args__ = (UniqueConstraint("workspace_id", "name"),)
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspace.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(20))  # email | webhook
    config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # email {recipients}; webhook {url, secret_ref}
    content_kinds: Mapped[list[str]] = mapped_column(JSON, default=list)  # report | alert
    destination_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending | authorized | rejected | revoked | lapsed
    approval_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    authorized_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    authorized_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[str] = mapped_column(String(40))
    revision: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    created_at: Mapped[datetime] = _ts()
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class Delivery(Base):
    """One send of a verified report snapshot or an alert to one destination (N-3). `idempotency_key` makes
    enqueueing idempotent and travels with the send so a receiver can drop a retried duplicate."""

    __tablename__ = "delivery"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(40), index=True)
    destination_id: Mapped[str] = mapped_column(String(40), index=True)
    destination_hash: Mapped[str] = mapped_column(String(64))
    approval_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    subject_type: Mapped[str] = mapped_column(String(20))  # report | alert
    subject_id: Mapped[str] = mapped_column(String(40))
    content_hash: Mapped[str] = mapped_column(String(64))
    idempotency_key: Mapped[str] = mapped_column(String(64), unique=True)
    # queued | sending | retrying | delivered | dead_letter | refused
    status: Mapped[str] = mapped_column(String(20), default="queued", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=5)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    response: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    origin: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # {schedule_run_id} | {monitor_id} | {user}
    created_at: Mapped[datetime] = _ts()
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class PlatformSetting(Base):
    """Append-only versions of the platform settings document (admin control plane)."""

    __tablename__ = "platform_setting"
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    document: Mapped[dict[str, Any]] = mapped_column(JSON)
    note: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str] = mapped_column(String(40))
    created_at: Mapped[datetime] = _ts()


class CrawlRun(Base):
    """One metadata crawl of a source (META-005/006): what was seen, what changed, what it cost."""

    __tablename__ = "crawl_run"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(40), index=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("source.id", ondelete="CASCADE"), index=True)
    mode: Mapped[str] = mapped_column(String(20))  # full | incremental
    trigger: Mapped[str] = mapped_column(String(20), default="manual")  # manual | schedule
    status: Mapped[str] = mapped_column(String(20), default="running")  # running | succeeded | failed
    stage: Mapped[str] = mapped_column(String(40), default="discover")
    options: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    stats: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    changes: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # CrawlDiff summary
    log: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_by: Mapped[str] = mapped_column(String(80))
    started_at: Mapped[datetime] = _ts()
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SpendCounter(Base):
    """Hard spend cap counters without Redis (ADR-0025, lite): the same keys as the Redis counters
    (platform day, workspace month, alert marks). A reservation locks every cap's row, checks every cap,
    then adds to every row in one transaction: atomic, and a database error refuses the call (fail
    closed). A row past `expires_at` is dead and is re-seeded from model_call like a missing Redis key."""

    __tablename__ = "spend_counter"
    key: Mapped[str] = mapped_column(String(200), primary_key=True)
    value: Mapped[float] = mapped_column(Float, default=0.0)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class ModelPayload(Base):
    """Redacted model request/response bodies, content-addressed (P4-C09).

    A separate table rather than two JSON columns on model_call because the same body recurs:
    every retry and fallback attempt re-sends the identical request, cache hits repeat a stored
    response, and scheduled re-runs resend the same catalog. Addressing by sha256 of the canonical
    JSON stores each body once. Bodies are zlib-compressed and capped (llm/replay.py MAX_PAYLOAD_BYTES);
    an over-cap body is replaced by a stub and marked truncated, never cut mid-JSON."""

    __tablename__ = "model_payload"
    hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    kind: Mapped[str] = mapped_column(String(20))  # request | response
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)  # uncompressed canonical JSON size
    truncated: Mapped[bool] = mapped_column(Boolean, default=False)
    body: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = _ts()


class DecisionRecord(Base):
    """One DecisionService decision (ADR-0015, P4-T08): who decided, what every backend said, what the
    authority class enforced. `answer` is the enforced value; `proposal` the deciding backend's raw one."""

    __tablename__ = "decision"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    workspace_id: Mapped[str | None] = mapped_column(String(40), index=True, nullable=True)
    run_id: Mapped[str | None] = mapped_column(String(40), index=True, nullable=True)
    task_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    agent_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    purpose: Mapped[str] = mapped_column(String(60), index=True)
    authority: Mapped[str] = mapped_column(String(30))
    backend: Mapped[str] = mapped_column(String(30))  # jev | rules | local_classifier | llm_structured | default
    model: Mapped[str | None] = mapped_column(String(120), nullable=True)
    inputs_hash: Mapped[str] = mapped_column(String(64))
    subject: Mapped[str | None] = mapped_column(String(240), index=True, nullable=True)  # insight:<id> | alert:<key> | feedback:<id>
    options: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    answer: Mapped[Any] = mapped_column(JSON, nullable=True)
    proposal: Mapped[Any] = mapped_column(JSON, nullable=True)
    probabilities: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    fallback_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempts: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    enforced: Mapped[list[str]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = _ts()


class DecisionOutcome(Base):
    """A labelled outcome for a decision from a user signal (P4-T09): finding accepted/rejected/dismissed,
    alert acknowledged/dismissed, feedback class corrected. `label` is the true answer on the decision's
    own scale (an option key, or yes/no for probability purposes)."""

    __tablename__ = "decision_outcome"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    decision_id: Mapped[str] = mapped_column(ForeignKey("decision.id", ondelete="CASCADE"), index=True)
    purpose: Mapped[str] = mapped_column(String(60), index=True)
    backend: Mapped[str] = mapped_column(String(30))
    workspace_id: Mapped[str | None] = mapped_column(String(40), index=True, nullable=True)
    label: Mapped[str] = mapped_column(String(80))
    source: Mapped[str] = mapped_column(String(40))  # finding.accept | finding.reject | alert.dismiss | feedback.correct ...
    user_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    created_at: Mapped[datetime] = _ts()


class DecisionCalibration(Base):
    """Append-only calibration evaluations and backend downgrades per purpose x backend (P4-T09).
    The latest row per (purpose, backend) is the effective state: `downgraded` rows remove that backend
    from the purpose's chain until a later evaluation or an administrator restores it."""

    __tablename__ = "decision_calibration"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    purpose: Mapped[str] = mapped_column(String(60), index=True)
    backend: Mapped[str] = mapped_column(String(30))
    window_days: Mapped[int] = mapped_column(Integer, default=30)
    n: Mapped[int] = mapped_column(Integer, default=0)
    brier: Mapped[float | None] = mapped_column(Float, nullable=True)
    ece: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_brier: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_ece: Mapped[float | None] = mapped_column(Float, nullable=True)
    downgraded: Mapped[bool] = mapped_column(Boolean, default=False)
    action: Mapped[str] = mapped_column(String(30))  # evaluated | downgraded | restored | admin_restored | admin_downgraded
    note: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str] = mapped_column(String(80))
    created_at: Mapped[datetime] = _ts()


class McpServer(Base):
    """An external MCP server registered in one workspace (spec v3 §3.7, P4-X05).

    Nothing is called until an owner/admin sets `allowed`. `tools` is the last screened
    `tools/list` snapshot; `classifications` holds the owner's side-effect verdict per tool, bound to
    the tool's definition hash so a changed tool falls back to `write_external`.
    """

    __tablename__ = "mcp_server"
    __table_args__ = (UniqueConstraint("workspace_id", "name"),)
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspace.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(60))
    url: Mapped[str] = mapped_column(String(500))
    transport: Mapped[str] = mapped_column(String(30), default="streamable_http")
    secret_ref: Mapped[str | None] = mapped_column(String(200), nullable=True)  # env:NAME | file:/path, never a value
    status: Mapped[str] = mapped_column(String(20), default="registered")  # registered | ready | error
    allowed: Mapped[bool] = mapped_column(Boolean, default=False)
    allowed_by: Mapped[str | None] = mapped_column(String(40), nullable=True)
    config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # max_calls_per_run, timeout_seconds
    tools: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    classifications: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    last_refreshed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str] = mapped_column(String(40))
    created_at: Mapped[datetime] = _ts()


class McpClient(Base):
    """An external MCP client of AnalystOS (P4-X06). Only a SHA-256 of the secret is stored; the client
    acts as its own service user, so scope, roles and the gateway apply unchanged."""

    __tablename__ = "mcp_client"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)  # the public client_id
    name: Mapped[str] = mapped_column(String(200))
    secret_hash: Mapped[str] = mapped_column(String(64))
    service_user_id: Mapped[str] = mapped_column(ForeignKey("app_user.id", ondelete="CASCADE"))
    status: Mapped[str] = mapped_column(String(20), default="active")  # active | revoked
    created_by: Mapped[str] = mapped_column(String(40))
    created_at: Mapped[datetime] = _ts()
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class McpGrant(Base):
    """What one MCP client may do in one workspace: role, tools and per-tool daily quotas."""

    __tablename__ = "mcp_grant"
    __table_args__ = (UniqueConstraint("client_id", "workspace_id"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    client_id: Mapped[str] = mapped_column(ForeignKey("mcp_client.id", ondelete="CASCADE"), index=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspace.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(20), default="viewer")  # viewer | analyst
    tools: Mapped[list[str]] = mapped_column(JSON, default=list)
    quotas: Mapped[dict[str, int]] = mapped_column(JSON, default=dict)  # tool -> calls per UTC day
    created_by: Mapped[str] = mapped_column(String(40))
    created_at: Mapped[datetime] = _ts()


class McpUsage(Base):
    """Per-day call counters for quota enforcement (incremented atomically before a call runs)."""

    __tablename__ = "mcp_usage"
    __table_args__ = (UniqueConstraint("client_id", "workspace_id", "tool", "day"),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    client_id: Mapped[str] = mapped_column(ForeignKey("mcp_client.id", ondelete="CASCADE"), index=True)
    workspace_id: Mapped[str] = mapped_column(String(40))
    tool: Mapped[str] = mapped_column(String(80))
    day: Mapped[str] = mapped_column(String(10))  # YYYY-MM-DD (UTC)
    count: Mapped[int] = mapped_column(Integer, default=0)


# ------------------------------------------------------------------------------ registries (P4-T05)
class VerifiedQuery(Base):
    """A parameterised, verified query for Ask (ladder rung L1, ADR-0012). Promoted from a successful
    Ask answer or a verified finding; `sql_template` holds `{{name}}` placeholders for `parameters`
    (typed: enum over a profiled column's vocabulary, date, number, string). Executed through the
    gateway under the asking user's scope, so a registry hit never widens what anyone can read."""

    __tablename__ = "verified_query"
    __table_args__ = (UniqueConstraint("workspace_id", "name"),)
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspace.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    patterns: Mapped[list[str]] = mapped_column(JSON, default=list)  # question phrasings (parameter values removed)
    sql_template: Mapped[str] = mapped_column(Text)
    parameters: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    source_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    dialect: Mapped[str] = mapped_column(String(40), default="postgres")
    origin: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # {type: ask|finding, query_id, insight_id, question}
    spec: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)  # the AnalysisSpec of a promoted finding
    status: Mapped[str] = mapped_column(String(20), default="active")  # active | retired
    hits: Mapped[int] = mapped_column(Integer, default=0)
    last_hit_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[str] = mapped_column(String(40))
    created_at: Mapped[datetime] = _ts()
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class RegisteredHypothesis(Base):
    """The hypothesis registry (review C11): every tested hypothesis spec of a workspace, keyed by its
    spec hash, with the claim identity of the method registry. Scheduled re-analysis replays it with
    no model call, so "new" and "resolved" mean the evidence changed, not that the questions did."""

    __tablename__ = "registered_hypothesis"
    __table_args__ = (UniqueConstraint("workspace_id", "spec_hash"),)
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspace.id", ondelete="CASCADE"), index=True)
    spec_hash: Mapped[str] = mapped_column(String(64))
    spec: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    question: Mapped[str] = mapped_column(Text, default="")
    statement: Mapped[str] = mapped_column(Text, default="")
    method: Mapped[str] = mapped_column(String(60))
    asset: Mapped[str] = mapped_column(String(300))
    question_key: Mapped[str] = mapped_column(String(64), index=True)  # hash of the claim key without its top group
    claim: Mapped[list[Any]] = mapped_column(JSON, default=list)  # last claim key (method registry), top group last
    status: Mapped[str] = mapped_column(String(20), default="active")  # active | retired
    last_outcome: Mapped[str] = mapped_column(String(30), default="")  # verified | supported | rejected | inconclusive | ...
    last_result: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    origin: Mapped[str] = mapped_column(String(20), default="agent")  # origin of the first registration
    first_run_id: Mapped[str] = mapped_column(String(40))
    last_run_id: Mapped[str] = mapped_column(String(40))
    times_tested: Mapped[int] = mapped_column(Integer, default=0)
    times_verified: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = _ts()
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class BuildTarget(Base):
    """A schema the workspace owner designated for builds (P4-E06). The BuildGateway writes nowhere
    else: its per-workspace build role holds CREATE on these schemas only, and sources stay read-only."""

    __tablename__ = "build_target"
    __table_args__ = (UniqueConstraint("workspace_id", "engine", "schema_name"),)
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspace.id", ondelete="CASCADE"), index=True)
    engine: Mapped[str] = mapped_column(String(80))  # postgres:analytics
    schema_name: Mapped[str] = mapped_column(String(63))
    build_role: Mapped[str] = mapped_column(String(63))  # NOLOGIN role the builder login SETs for this workspace
    status: Mapped[str] = mapped_column(String(20), default="active")  # active | retired
    provisioning: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_by: Mapped[str] = mapped_column(String(40))
    created_at: Mapped[datetime] = _ts()


class BuildJob(Base):
    """One generated dbt project and its execution (P4-E04): the project files and their hash, the dry
    run and cost estimate, the approval that covers it, the rollback plan, the harvested manifest,
    run results and OpenLineage events. Provenance: dataset/metrics -> transformation -> job -> tables."""

    __tablename__ = "build_job"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspace.id", ondelete="CASCADE"), index=True)
    run_id: Mapped[str] = mapped_column(String(40), index=True)  # the elt_build run
    source_run_id: Mapped[str] = mapped_column(String(40), index=True)  # the run whose dataset and KPIs are built
    artifact_id: Mapped[str | None] = mapped_column(String(40), nullable=True)  # the `transformation` artifact
    approval_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    engine: Mapped[str] = mapped_column(String(80))
    runner: Mapped[str] = mapped_column(String(40), default="dbt-core")
    target_schema: Mapped[str] = mapped_column(String(63))
    project_name: Mapped[str] = mapped_column(String(120))
    project_files: Mapped[dict[str, str]] = mapped_column(JSON, default=dict)
    project_hash: Mapped[str] = mapped_column(String(64))
    plan_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    relations: Mapped[list[str]] = mapped_column(JSON, default=list)  # schema.relation the job creates or replaces
    dry_run: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    estimate: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    rollback: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(20), default="planned")  # planned|awaiting_approval|running|succeeded|failed|refused
    manifest: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # harvested summary of target/manifest.json
    run_results: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    openlineage: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    log_tail: Mapped[str] = mapped_column(Text, default="")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str] = mapped_column(String(80))
    created_at: Mapped[datetime] = _ts()
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# ------------------------------------------------------------------------------ Ask threads (P4-U02)
class AskThread(Base):
    """A persisted Ask conversation. Private to its creator; the workspace scopes what it can read."""

    __tablename__ = "ask_thread"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspace.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[str] = mapped_column(String(40), index=True)
    title: Mapped[str] = mapped_column(String(300))
    archived: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = _ts()
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    revision: Mapped[int] = mapped_column(Integer, default=1, server_default="1")  # P4-06: optional If-Match on PATCH


class AskTurn(Base):
    """One question and its governed answer or refusal. `stages` are the plain-language steps streamed
    while it ran; `decision` and `model_call` rows of the turn carry `task_id = id` (the inspector's
    Decision tab); `promotions` records what the answer became (verified query, metric, monitor, run)."""

    __tablename__ = "ask_turn"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    thread_id: Mapped[str] = mapped_column(ForeignKey("ask_thread.id", ondelete="CASCADE"), index=True)
    workspace_id: Mapped[str] = mapped_column(String(40), index=True)
    user_id: Mapped[str] = mapped_column(String(40))
    seq: Mapped[int] = mapped_column(Integer, default=1)
    question: Mapped[str] = mapped_column(Text)
    parameters: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(20), default="running")  # running | answered | needs_input | clarify | refused
    route: Mapped[str | None] = mapped_column(String(30), nullable=True)  # ask_route decision: verified_query | tool | generate | decline
    answered_by: Mapped[str | None] = mapped_column(String(20), nullable=True)  # registry | model
    refusal: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)  # {kind, message, remedy, details}
    sql: Mapped[str | None] = mapped_column(Text, nullable=True)
    explanation: Mapped[str | None] = mapped_column(Text, nullable=True)
    chart: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    verified_query: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    model: Mapped[str | None] = mapped_column(String(120), nullable=True)
    attempts: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    stages: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    decisions: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)  # summaries; full rows in `decision`
    provenance: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    promotions: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    # Analyst mode: plan, per-step results/facts/checks, cited synthesis, follow-ups; NULL for a quick turn.
    analysis: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = _ts()


# ------------------------------------------------------------ definitions, work orders, outbox (P7-03, P4-06)
class Definition(Base):
    """One version of an executable definition (ADR-0021): `kind` + JSON `spec`. A draft is editable
    (its `revision` guards concurrent edits); publishing freezes it; retiring stops it running. One
    draft per (workspace, kind, key) at a time; published versions are immutable."""

    __tablename__ = "definition"
    __table_args__ = (UniqueConstraint("workspace_id", "kind", "key", "version", name="uq_definition_version"),
                      Index("uq_definition_one_draft", "workspace_id", "kind", "key", unique=True,
                            postgresql_where=text("status IN ('draft', 'tested')"),
                            sqlite_where=text("status IN ('draft', 'tested')")))
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspace.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(40))
    key: Mapped[str] = mapped_column(String(120))
    version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), default="draft")  # draft | tested | published | deprecated | retired
    title: Mapped[str | None] = mapped_column(String(300), nullable=True)
    spec: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    content_hash: Mapped[str] = mapped_column(String(64))
    revision: Mapped[int] = mapped_column(Integer, default=1)
    created_by: Mapped[str] = mapped_column(String(80))
    published_by: Mapped[str | None] = mapped_column(String(80), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    retired_by: Mapped[str | None] = mapped_column(String(80), nullable=True)
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # P7-11: the test run that made a draft `tested`, bound to the content hash it tested
    test_evidence: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    # ADR-0021 §5: per-environment connection bindings (not content) and the version a promotion copied
    bindings: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    promoted_from: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = _ts()
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class IdempotencyRecord(Base):
    """`Idempotency-Key` of a mutating request (workbench API §1): scoped by principal, workspace and
    operation; the canonical request hash decides replay (same) or 409 (different)."""

    __tablename__ = "idempotency_record"
    __table_args__ = (UniqueConstraint("principal", "workspace_id", "operation", "key", name="uq_idempotency_scope"),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    principal: Mapped[str] = mapped_column(String(80))
    workspace_id: Mapped[str] = mapped_column(String(40))
    operation: Mapped[str] = mapped_column(String(60))
    key: Mapped[str] = mapped_column(String(200))
    request_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(20), default="in_progress")  # in_progress | completed
    status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    response: Mapped[Any] = mapped_column(JSON, nullable=True)
    resource_type: Mapped[str | None] = mapped_column(String(40), nullable=True)
    resource_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = _ts()
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class DispatchOutbox(Base):
    """Run creation and its dispatch in one transaction (workbench API §4): the row commits with the
    run; a relay starts the workflow (stable id, so a duplicate delivery converges) and retries."""

    __tablename__ = "dispatch_outbox"
    __table_args__ = (UniqueConstraint("kind", "run_id", name="uq_dispatch_outbox_run"),
                      Index("ix_dispatch_outbox_pending", "status", "available_at"))
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(40), index=True)
    kind: Mapped[str] = mapped_column(String(30), default="run.start")
    run_id: Mapped[str] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending | dispatched | cancelled
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    workflow_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    created_at: Mapped[datetime] = _ts()
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class WorkOrder(Base):
    """A typed work order (`contracts.work.WorkOrderSpec`), editable under revision checks."""

    __tablename__ = "work_order"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspace.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(30))
    spec_type: Mapped[str] = mapped_column(String(30))
    objective: Mapped[str] = mapped_column(Text)
    spec: Mapped[dict[str, Any]] = mapped_column(JSON)
    spec_hash: Mapped[str] = mapped_column(String(64))
    revision: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(20), default="draft")  # draft | started
    run_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    created_by: Mapped[str] = mapped_column(String(40))
    created_at: Mapped[datetime] = _ts()
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


# ------------------------------------------------------------------------------ transformation recipes (P6-04..07)
class RecipeVersion(Base):
    """One version of a transformation recipe (ADR-0023): the validated IR with every node's schema
    stamped, and its hash. Drafts until published (ADR-0021); a new save of changed content is a new version."""

    __tablename__ = "recipe"
    __table_args__ = (UniqueConstraint("workspace_id", "name", "version"),)
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspace.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(60))
    version: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(20), default="draft")  # draft | published | superseded
    spec: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    spec_hash: Mapped[str] = mapped_column(String(64))
    created_by: Mapped[str] = mapped_column(String(80))
    created_at: Mapped[datetime] = _ts()
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    published_by: Mapped[str | None] = mapped_column(String(80), nullable=True)


class RecipeRun(Base):
    """One execution of a recipe version: where it ran and why (pushdown or snapshot fallback), the join
    pre-flight, the input snapshots, gate results, schema-policy changes, what was materialized or
    quarantined, the column lineage from the IR and its OpenLineage events. Provenance: recipe ->
    recipe_run -> table (output, quarantine); source table -> transformed_into -> output table."""

    __tablename__ = "recipe_run"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspace.id", ondelete="CASCADE"), index=True)
    recipe_id: Mapped[str] = mapped_column(ForeignKey("recipe.id", ondelete="CASCADE"), index=True)
    recipe_name: Mapped[str] = mapped_column(String(60))
    recipe_version: Mapped[int] = mapped_column(Integer)
    spec_hash: Mapped[str] = mapped_column(String(64))
    mode: Mapped[str] = mapped_column(String(20), default="materialize")  # preview | materialize
    engine: Mapped[str | None] = mapped_column(String(20), nullable=True)  # sql | duckdb
    status: Mapped[str] = mapped_column(String(20), default="running")  # running|succeeded|blocked|refused|failed
    plan: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    preflight: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    snapshots: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    gates: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # output name -> gate summary
    schema_changes: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # output name -> policy verdict
    outputs: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # output name -> table, rows, fingerprint...
    lineage: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    openlineage: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    query_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str] = mapped_column(String(80))
    created_at: Mapped[datetime] = _ts()
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# ------------------------------------------------------------------------------ workspace brief and readiness (P4-04)
class WorkspaceBrief(Base):
    """One version of the workspace brief (workspace spec §2): every assertion with its origin, evidence,
    review state and own version. A new version is written on every change; older ones stay readable."""

    __tablename__ = "workspace_brief"
    __table_args__ = (UniqueConstraint("workspace_id", "version", name="uq_workspace_brief_version"),)
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspace.id", ondelete="CASCADE"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    assertions: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    content_hash: Mapped[str] = mapped_column(String(64))
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str] = mapped_column(String(80))
    created_at: Mapped[datetime] = _ts()


class ReadinessAssessment(Base):
    """A readiness verdict for one job kind over given inputs: `ready | needs_input | blocked | unsupported`
    with every check's own status, reason and remediation (no averaged score)."""

    __tablename__ = "readiness_assessment"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspace.id", ondelete="CASCADE"), index=True)
    job_kind: Mapped[str] = mapped_column(String(30))
    work_order_id: Mapped[str | None] = mapped_column(String(40), index=True, nullable=True)
    work_order_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    brief_version: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(20))  # ready | needs_input | blocked | unsupported
    checks: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    inputs: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    inputs_hash: Mapped[str] = mapped_column(String(64))
    alternatives: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    created_by: Mapped[str] = mapped_column(String(80))
    created_at: Mapped[datetime] = _ts()


# ------------------------------------------------------------------------------ steps, branches, notebooks (P7-04/05/12)
class StepBranch(Base):
    """A line of steps inside one container (a run, an Ask thread or a notebook). The main branch has no
    parent; a fork keeps its parent pointer, the step it forked from and the versions it inherited."""

    __tablename__ = "step_branch"
    __table_args__ = (Index("ix_step_branch_container", "container_type", "container_id"),)
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspace.id", ondelete="CASCADE"), index=True)
    container_type: Mapped[str] = mapped_column(String(20))  # run | ask_thread | notebook
    container_id: Mapped[str] = mapped_column(String(40))
    name: Mapped[str] = mapped_column(String(200))
    parent_branch_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    forked_from_step_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    forked_from_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    base: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)  # inherited [{step_id, version}]
    status: Mapped[str] = mapped_column(String(20), default="open")  # open | merged
    merged_into: Mapped[list[str]] = mapped_column(JSON, default=list)  # report artifact ids
    created_by: Mapped[str] = mapped_column(String(80))
    created_at: Mapped[datetime] = _ts()


class AnalysisStep(Base):
    """A step's identity (spec v4 §7): its kind, place in a branch and what it depends on. Its content
    lives in versions; `current_version` is the one shown and the one dependents were computed from."""

    __tablename__ = "analysis_step"
    __table_args__ = (Index("ix_analysis_step_container", "container_type", "container_id"),)
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspace.id", ondelete="CASCADE"), index=True)
    branch_id: Mapped[str] = mapped_column(ForeignKey("step_branch.id", ondelete="CASCADE"), index=True)
    container_type: Mapped[str] = mapped_column(String(20))
    container_id: Mapped[str] = mapped_column(String(40))
    seq: Mapped[int] = mapped_column(Integer, default=1)
    kind: Mapped[str] = mapped_column(String(20))  # plan | query | method | recipe | train | chart | claim
    title: Mapped[str] = mapped_column(String(300))
    depends_on: Mapped[list[str]] = mapped_column(JSON, default=list)
    current_version: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    origin: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # {type: ask_turn|hypothesis|insight|cell|user, id}
    forked_from: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)  # {step_id, version}
    created_by: Mapped[str] = mapped_column(String(80))
    created_at: Mapped[datetime] = _ts()
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class AnalysisStepVersion(Base):
    """One execution of a step: the spec it ran, the inputs (upstream version ids), receipts, a result
    snapshot (artifact ref), checks, corrections and its verification record. Immutable once finished."""

    __tablename__ = "analysis_step_version"
    __table_args__ = (UniqueConstraint("step_id", "version", name="uq_analysis_step_version"),)
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    step_id: Mapped[str] = mapped_column(ForeignKey("analysis_step.id", ondelete="CASCADE"), index=True)
    workspace_id: Mapped[str] = mapped_column(String(40), index=True)
    version: Mapped[int] = mapped_column(Integer)
    spec: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    spec_hash: Mapped[str] = mapped_column(String(64))
    inputs: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # upstream step id -> its version row id
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending|ok|flagged|failed|unsupported|recorded
    reason: Mapped[str] = mapped_column(String(40), default="created")  # created|edited|rerun|upstream_changed|forked|ingested
    receipts: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    result_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    chart_spec: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    checks: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    corrections: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    verification_record_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str] = mapped_column(String(80))
    created_at: Mapped[datetime] = _ts()
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class StepPin(Base):
    """A step pinned to a dashboard tile or a schedule: the frozen query/spec of one version (ADR-0021),
    replayed as frozen until the owner re-pins a newer version."""

    __tablename__ = "step_pin"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspace.id", ondelete="CASCADE"), index=True)
    step_id: Mapped[str] = mapped_column(String(40), index=True)
    step_version: Mapped[int] = mapped_column(Integer)
    target: Mapped[str] = mapped_column(String(20))  # tile | schedule
    target_id: Mapped[str | None] = mapped_column(String(40), nullable=True)  # chart artifact | schedule id
    frozen: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    frozen_hash: Mapped[str] = mapped_column(String(64))
    definition: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)  # the published DefinitionRef
    approval_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    last_result: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_by: Mapped[str] = mapped_column(String(80))
    created_at: Mapped[datetime] = _ts()


class Notebook(Base):
    """A workspace notebook (P7-12): markdown, SQL and restricted-Python cells, each cell a step."""

    __tablename__ = "notebook"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspace.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(300))
    revision: Mapped[int] = mapped_column(Integer, default=1)
    archived: Mapped[bool] = mapped_column(Boolean, default=False)
    created_by: Mapped[str] = mapped_column(String(80))
    created_at: Mapped[datetime] = _ts()
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class Pipeline(Base):
    """One version of a PipelineSpec (P6-01): the envelope around published recipes (input versions, output
    contract, join expectations, reconciliation checks, freshness, budgets, incremental block and managed
    destination). Drafts until published (ADR-0021); a changed save is a new version."""

    __tablename__ = "pipeline"
    __table_args__ = (UniqueConstraint("workspace_id", "name", "version"),)
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspace.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(60))
    version: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(20), default="draft")  # draft | published | superseded
    spec: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    spec_hash: Mapped[str] = mapped_column(String(64))
    created_by: Mapped[str] = mapped_column(String(80))
    created_at: Mapped[datetime] = _ts()
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    published_by: Mapped[str | None] = mapped_column(String(80), nullable=True)


class PipelineRun(Base):
    """One pipeline execution: a dry run (compiled SQL, per-source scope and snapshot manifest, key/fan-out/
    unmatched checks, the reconciled virtual output and its candidate snapshot) or a run into the managed
    recipe output (incremental window, full, reconcile, backfill). A dry run of a pipeline with a destination
    asks for the hash-bound approval its materialization needs. Provenance: pipeline -> run -> materialization."""

    __tablename__ = "pipeline_run"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspace.id", ondelete="CASCADE"), index=True)
    pipeline_id: Mapped[str] = mapped_column(ForeignKey("pipeline.id", ondelete="CASCADE"), index=True)
    pipeline_name: Mapped[str] = mapped_column(String(60))
    pipeline_version: Mapped[int] = mapped_column(Integer)
    spec_hash: Mapped[str] = mapped_column(String(64))
    mode: Mapped[str] = mapped_column(String(20))  # dry_run | auto | full | reconcile | backfill
    status: Mapped[str] = mapped_column(String(20), default="running")  # running|succeeded|blocked|awaiting_approval|failed|refused
    recipes: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)  # [{name, version, id, spec_hash}]
    plan: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    manifest: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    sql: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    checks: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    reconciliation: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    candidate: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    recipe_run_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    query_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    plan_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    approval_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str] = mapped_column(String(80))
    created_at: Mapped[datetime] = _ts()
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class WriterDestination(Base):
    """An allowlisted destination of the managed output writer (P6-03, ADR-0011): a schema the workspace
    owner designated, and optionally the tables allowed in it. The writer's per-workspace role holds CREATE
    on these schemas only; sources, build targets and system schemas are never destinations."""

    __tablename__ = "writer_destination"
    __table_args__ = (UniqueConstraint("engine", "schema_name"),)
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspace.id", ondelete="CASCADE"), index=True)
    engine: Mapped[str] = mapped_column(String(80))  # postgres:analytics
    schema_name: Mapped[str] = mapped_column(String(63))
    tables: Mapped[list[str]] = mapped_column(JSON, default=list)  # empty = any table name in the schema
    writer_role: Mapped[str] = mapped_column(String(63))
    status: Mapped[str] = mapped_column(String(20), default="active")  # active | retired
    provisioning: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_by: Mapped[str] = mapped_column(String(40))
    created_at: Mapped[datetime] = _ts()


class Materialization(Base):
    """One version of a destination table written by the managed writer (P6-03): staged as its own
    version table, validated, then promoted atomically (the destination view is re-pointed in one
    transaction). `previous_id` is the rollback pointer to the good version it replaced; `idempotency_key`
    makes a retry of the same approved candidate resume from its `checkpoint` instead of writing twice."""

    __tablename__ = "materialization"
    __table_args__ = (UniqueConstraint("idempotency_key"), UniqueConstraint("destination_id", "table_name", "version"))
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspace.id", ondelete="CASCADE"), index=True)
    destination_id: Mapped[str] = mapped_column(ForeignKey("writer_destination.id", ondelete="CASCADE"), index=True)
    schema_name: Mapped[str] = mapped_column(String(63))
    table_name: Mapped[str] = mapped_column(String(63))
    version: Mapped[int] = mapped_column(Integer)
    version_table: Mapped[str] = mapped_column(String(63))
    pipeline_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    pipeline_run_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    approval_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(64))
    candidate: Mapped[str] = mapped_column(String(64))  # the approved candidate snapshot digest
    # approved | staged | promoted | superseded | rolled_back | failed
    status: Mapped[str] = mapped_column(String(20), default="approved")
    row_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    content_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    previous_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    checkpoint: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str] = mapped_column(String(80))
    created_at: Mapped[datetime] = _ts()
    promoted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class WorkerTask(Base):
    """One task dispatched to an isolated compute pool (ADR-0022, P7-06): the envelope's identity and hash,
    where it ran, its status and its result (output artifact refs, a small result, a structured error,
    resource usage). Provenance: run -> dispatched -> worker_task; input artifact -> input_to -> worker_task
    -> produced -> output artifact."""

    __tablename__ = "worker_task"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspace.id", ondelete="CASCADE"), index=True)
    run_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    work_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    pool: Mapped[str] = mapped_column(String(40))
    capability_id: Mapped[str] = mapped_column(String(120))
    capability_version: Mapped[str] = mapped_column(String(40))
    capability_hash: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(60))
    idempotency_key: Mapped[str] = mapped_column(String(128), index=True)
    envelope_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(20), default="queued")  # queued|running|completed|failed
    inputs: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    outputs: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    result: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    error: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    usage: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = _ts()
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class WorkerTaskEvent(Base):
    """task.started | task.progress | task.completed | task.failed as the worker reported them (Temporal
    signals or the local pool's event lines), persisted by the control plane. Retries append, never rewrite."""

    __tablename__ = "worker_task_event"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("worker_task.id", ondelete="CASCADE"), index=True)
    seq: Mapped[int] = mapped_column(Integer)
    type: Mapped[str] = mapped_column(String(40))
    data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    worker_at: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = _ts()


# ------------------------------------------------------------------------------ governed classical ML (P5-01..P5-06)
class MLSplit(Base):
    """An immutable split manifest (ADR-0024): partition sizes and membership hashes of one dataset version
    under one strategy and seed. Its holdout is consumed by the first experiment that reads it; tuning again
    after that needs a new partition (another seed or holdout fraction) and so a new manifest."""

    __tablename__ = "ml_split"
    __table_args__ = (UniqueConstraint("workspace_id", "manifest_hash", name="uq_ml_split_manifest"),)
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspace.id", ondelete="CASCADE"), index=True)
    manifest_hash: Mapped[str] = mapped_column(String(64))
    dataset_version: Mapped[str] = mapped_column(String(64))
    strategy: Mapped[str] = mapped_column(String(30))
    manifest: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    membership_hash: Mapped[str] = mapped_column(String(64))
    holdout_consumed_by: Mapped[str | None] = mapped_column(String(40), nullable=True)
    holdout_spec_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    holdout_consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = _ts()


class MLExperiment(Base):
    """One experiment over a published `ml_spec` definition: readiness, split, bounded search, sealed holdout
    evaluation, package and verdict. Its records (spec, manifest, trials, package, evaluation, model card)
    are artifacts; this row indexes them and is the platform record a package must match to load."""

    __tablename__ = "ml_experiment"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspace.id", ondelete="CASCADE"), index=True)
    run_id: Mapped[str | None] = mapped_column(String(40), index=True, nullable=True)
    definition_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    definition_key: Mapped[str] = mapped_column(String(120))
    definition_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    task: Mapped[str] = mapped_column(String(20))
    spec: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    spec_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(20), default="running")  # running | succeeded | refused | failed
    verdict: Mapped[str | None] = mapped_column(String(30), nullable=True)  # improved | no_improvement | guardrail_failed | invalid
    dataset_asset: Mapped[str] = mapped_column(String(300))
    dataset_source_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    dataset_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    split_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    manifest_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    selection_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    evaluation_seal: Mapped[str | None] = mapped_column(String(64), nullable=True)
    package_hash: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    code_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    environment_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    readiness: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    summary: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    artifacts: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # record type -> artifact id
    verification_record_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    query_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    reproduction_of: Mapped[str | None] = mapped_column(String(40), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str] = mapped_column(String(80))
    created_at: Mapped[datetime] = _ts()
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class MLModelVersion(Base):
    """A registered model version (the registry is the artifact store, ADR-0024). Retraining registers a
    challenger; only a hash-bound approval makes a version the champion. One champion per model name; the
    champion it replaced is kept as the rollback version."""

    __tablename__ = "ml_model_version"
    __table_args__ = (UniqueConstraint("workspace_id", "name", "version", name="uq_ml_model_version"),
                      Index("uq_ml_model_one_champion", "workspace_id", "name", unique=True,
                            postgresql_where=text("status = 'champion'"), sqlite_where=text("status = 'champion'")))
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspace.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    version: Mapped[int] = mapped_column(Integer)
    experiment_id: Mapped[str] = mapped_column(ForeignKey("ml_experiment.id", ondelete="CASCADE"), index=True)
    task: Mapped[str] = mapped_column(String(20))
    package_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(20), default="candidate")  # candidate | challenger | champion | retired
    feature_schema: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    approval_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    previous_champion_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    promoted_by: Mapped[str | None] = mapped_column(String(80), nullable=True)
    promoted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str] = mapped_column(String(80))
    created_at: Mapped[datetime] = _ts()


class MLScoringRun(Base):
    """One approved batch scoring of a published `ml_scoring` definition: the pinned model version and
    package, the input snapshot, rows scored and rejected, and where they were written (the managed output
    source). `dedupe_key` (definition hash + package + input version) makes a repeat a no-op."""

    __tablename__ = "ml_scoring_run"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspace.id", ondelete="CASCADE"), index=True)
    run_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    definition_id: Mapped[str] = mapped_column(String(40))
    definition_key: Mapped[str] = mapped_column(String(120))
    definition_version: Mapped[int] = mapped_column(Integer)
    definition_hash: Mapped[str] = mapped_column(String(64))
    model_version_id: Mapped[str] = mapped_column(String(40), index=True)
    package_hash: Mapped[str] = mapped_column(String(64))
    input_asset: Mapped[str | None] = mapped_column(String(300), nullable=True)
    input_source_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    input_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    dedupe_key: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(20), default="awaiting_approval")  # awaiting_approval|succeeded|refused|failed
    approval_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    rows_input: Mapped[int] = mapped_column(Integer, default=0)
    rows_scored: Mapped[int] = mapped_column(Integer, default=0)
    rows_rejected: Mapped[int] = mapped_column(Integer, default=0)
    output_source_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    output_table: Mapped[str | None] = mapped_column(String(200), nullable=True)
    rejected_table: Mapped[str | None] = mapped_column(String(200), nullable=True)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    query_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str] = mapped_column(String(80))
    created_at: Mapped[datetime] = _ts()
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)



class WhatIfScenario(Base):
    """A what-if scenario (N-9): a governed `SemanticQuery`, the declared changes and assumptions (hashed),
    the observed baseline's receipt and the labelled result. Private to its author, like an Ask thread;
    never a publishable artifact (every scenario number is labelled simulated)."""

    __tablename__ = "what_if_scenario"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspace.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    ask_turn_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    spec: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    spec_hash: Mapped[str] = mapped_column(String(64))
    assumptions: Mapped[list[str]] = mapped_column(JSON, default=list)
    assumptions_hash: Mapped[str] = mapped_column(String(64))
    baseline: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    result: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    result_hash: Mapped[str] = mapped_column(String(64))
    semantic_model_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    compiler_version: Mapped[str | None] = mapped_column(String(40), nullable=True)
    scenario_version: Mapped[str] = mapped_column(String(20))
    created_by: Mapped[str] = mapped_column(String(80), index=True)
    created_at: Mapped[datetime] = _ts()


# ------------------------------------------------------------------------------ physical-design advice (N-11)
class IndexAdvice(Base):
    """An index / partitioning / clustering recommendation derived from the gateway's query history and dry
    plans (skills/index_advice.py). Advisory only: `ddl` is text for a person; no status means "applied"
    because the platform never applies it. Re-analysis updates the row with the same `advice_key`."""

    __tablename__ = "index_advice"
    __table_args__ = (UniqueConstraint("workspace_id", "advice_key", name="uq_index_advice_key"),
                      CheckConstraint("status IN ('open', 'acknowledged', 'dismissed', 'superseded')",
                                      name="ck_index_advice_status"))
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspace.id", ondelete="CASCADE"), index=True)
    source_id: Mapped[str] = mapped_column(String(40))
    asset: Mapped[str] = mapped_column(String(330))  # "schema.table" as the gateway sees it
    kind: Mapped[str] = mapped_column(String(20))  # index | partition | clustering
    columns: Mapped[list[str]] = mapped_column(JSON, default=list)
    dialect: Mapped[str] = mapped_column(String(30))
    ddl: Mapped[str] = mapped_column(Text)
    confidence: Mapped[str] = mapped_column(String(10))  # high | medium | low
    estimated_benefit: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # query fingerprints/ids, plan nodes, stats
    notes: Mapped[list[str]] = mapped_column(JSON, default=list)
    advice_key: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(20), default="open")
    status_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    status_by: Mapped[str | None] = mapped_column(String(80), nullable=True)
    created_by: Mapped[str] = mapped_column(String(80))
    created_at: Mapped[datetime] = _ts()
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class EntityMatchRun(Base):
    """One deterministic record-linkage run between two tables (INT-004, N-7). Its pairs are proposals;
    only the accepted ones, promoted under a hash-bound approval, become the reviewed crosswalk table
    (`crosswalk_table` in the managed output source) and the join keys analysis may use (`join_keys`).
    No PII value is stored here: `left_version`/`right_version` are digests of the compared features."""

    __tablename__ = "entity_match_run"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspace.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(60))
    spec: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    spec_hash: Mapped[str] = mapped_column(String(64))
    left_asset: Mapped[str] = mapped_column(String(300))
    right_asset: Mapped[str] = mapped_column(String(300))
    left_source_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    right_source_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    left_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    right_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    pii_fields: Mapped[list[str]] = mapped_column(JSON, default=list)  # field labels compared as keyed digests
    status: Mapped[str] = mapped_column(String(20), default="running")  # running|proposed|awaiting_approval|promoted|failed
    stats: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    query_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    approval_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    crosswalk_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    crosswalk_source_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    crosswalk_table: Mapped[str | None] = mapped_column(String(200), nullable=True)
    join_keys: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str] = mapped_column(String(80))
    created_at: Mapped[datetime] = _ts()
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    promoted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class EntityMatchPair(Base):
    """One proposed link: the two record keys, the score, its band (`match` or `review`) and the per-field
    similarities (numbers only). `decision` is a person's: proposed until accepted or rejected."""

    __tablename__ = "entity_match_pair"
    __table_args__ = (UniqueConstraint("run_id", "left_key", "right_key", name="uq_entity_match_pair"),)
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("entity_match_run.id", ondelete="CASCADE"), index=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspace.id", ondelete="CASCADE"), index=True)
    left_key: Mapped[str] = mapped_column(String(300))
    right_key: Mapped[str] = mapped_column(String(300))
    score: Mapped[float] = mapped_column(Float)
    band: Mapped[str] = mapped_column(String(10))  # match | review
    fields: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    decision: Mapped[str] = mapped_column(String(10), default="proposed")  # proposed | accepted | rejected
    decided_by: Mapped[str | None] = mapped_column(String(80), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)


class InvestigationAction(Base):
    __tablename__ = "investigation_action"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspace.id", ondelete="CASCADE"), index=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("analysis_run.id", ondelete="CASCADE"), index=True)
    insight_id: Mapped[str] = mapped_column(ForeignKey("insight.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(300))
    next_step: Mapped[str] = mapped_column(Text)
    owner_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    due_date: Mapped[str | None] = mapped_column(String(10), nullable=True)
    impact_assessment: Mapped[str] = mapped_column(Text, default="")
    success_measure: Mapped[str] = mapped_column(Text)
    open_questions: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), default="proposed")
    resolution_note: Mapped[str] = mapped_column(Text, default="")
    revision: Mapped[int] = mapped_column(Integer, default=1)
    created_by: Mapped[str] = mapped_column(String(40))
    created_at: Mapped[datetime] = _ts()
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class PilotReview(Base):
    __tablename__ = "pilot_review"
    __table_args__ = (UniqueConstraint("run_id", "reviewer_id", "evidence_kind", name="uq_pilot_review_run_user_kind"),)
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspace.id", ondelete="CASCADE"), index=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("analysis_run.id", ondelete="CASCADE"), index=True)
    reviewer_id: Mapped[str] = mapped_column(String(40))
    evidence_kind: Mapped[str] = mapped_column(String(20))
    task: Mapped[str] = mapped_column(String(500))
    baseline_minutes: Mapped[float] = mapped_column(Float)
    assisted_minutes: Mapped[float] = mapped_column(Float)
    accuracy: Mapped[str] = mapped_column(String(20))
    usefulness: Mapped[int] = mapped_column(Integer)
    would_use_again: Mapped[bool] = mapped_column(Boolean)
    notes: Mapped[str] = mapped_column(Text, default="")
    revision: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = _ts()
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class ContextBinding(Base):
    __tablename__ = "context_binding"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(40), index=True)
    entry_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    term: Mapped[str] = mapped_column(String(200))
    state: Mapped[str] = mapped_column(String(20), default="draft")
    definition: Mapped[dict[str, Any]] = mapped_column(JSON)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    created_by: Mapped[str] = mapped_column(String(40))
    created_at: Mapped[datetime] = _ts()
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class GoldenQuestion(Base):
    __tablename__ = "golden_question"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(40), index=True)
    definition: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_by: Mapped[str] = mapped_column(String(40))
    created_at: Mapped[datetime] = _ts()


class GroundingEvaluation(Base):
    __tablename__ = "grounding_evaluation"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(40), index=True)
    reviewer_id: Mapped[str] = mapped_column(String(40))
    result: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = _ts()


from analystos.db import column_presence  # noqa: E402,F401  (registers the absent-column filter, P7-20)
from analystos.tenancy.schema import attach_tenant_columns  # noqa: E402

attach_tenant_columns(Base.metadata)  # tenant_id on every tenant-owned table (ADR-009)
