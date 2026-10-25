"""Event model (§42, FND-010)."""
from __future__ import annotations

EVENT_TYPES = {
    "investigation.action_created", "investigation.action_updated", "pilot.review_saved",
    "workspace.created", "workspace.updated", "source.connected", "metadata.collected", "context.loaded",
    "profile.completed", "quality.issue.detected", "relationship.discovered", "hypothesis.created",
    "hypothesis.updated", "query.executed", "query.rejected", "analysis.completed", "insight.created",
    "insight.verified", "dataset.created", "metric.created", "chart.created", "dashboard.created",
    "dashboard.published", "report.generated", "schedule.executed", "agent.started", "agent.completed",
    "agent.failed", "agent.message", "task.updated", "run.created", "run.status", "run.replanned",
    "approval.requested", "approval.completed", "approval.invalidated", "policy.denied", "feedback.received",
    "model.called", "budget.warning", "budget.cap_reached", "monitor.evaluated", "alert.raised", "notification.created",
    "crawl.started", "crawl.completed", "crawl.failed", "schema.changed", "settings.updated",
    "mcp.server.refreshed", "mcp.tool_called", "agent.action", "verified_query.promoted", "verified_query.updated",
    "hypothesis_registry.updated",
    # Write path (P4-E04/E06): build targets and dbt build jobs
    "build_target.provisioned", "build.planned", "build.started", "build.completed", "build.failed", "build.refused",
    "semantic.model.updated", "semantic.metric.proposed", "semantic.metric.approved", "semantic.metric.rejected",
    "semantic.metric.deprecated", "semantic.conflict.detected", "semantic.imported",
    # Semantic compilation and review (P7-02, P7-09, P4-05)
    "semantic.metric.stale", "semantic.model.approved", "semantic.relationship.candidate", "semantic.relationship.validated",
    "semantic.ownership_offered", "semantic.ownership_transferred", "semantic.ownership_declined",  # P4-05
    "ask.answered", "ask.promoted", "ask.step_rerun", "capability.invoked",
    "knowledge.revision_committed", "knowledge.imported", "knowledge.pushed",
    # Review queue (P4-K07/K08): drafts proposed by a model or the learning loop, decided in batches
    "knowledge.suggestion_proposed", "knowledge.suggestions_reviewed",
    # Crawler sources and facet-level failure (P4-K06)
    "crawl.facet_failed", "knowledge.ingested",
    # Typed evidence (P4-03): a newer snapshot of an analysed asset made a finding stale
    "insight.stale",
    # Verification records (P7-01, ADR-0020): a verdict bound to its dependency fingerprint; voided when a
    # dependency changes (with its cause), swept nightly; a finding flagged wrong carries its reason
    "verification.recorded", "verification.voided", "verification.sweep_completed", "insight.flagged_wrong",
    "verification.reverify_requested",
    # Definition lifecycle and pinned schedules (P7-03, ADR-0021)
    "definition.draft_saved", "definition.published", "definition.deprecated", "definition.retired",
    "definition.tested", "definition.promoted",
    "schedule.upgrade_available", "schedule.upgraded", "schedule.pin_warning", "schedule.blocked", "narrative.stale",
    # Typed work orders and the dispatch outbox (P4-06)
    "work_order.created", "work_order.updated", "run.dispatched", "run.dispatch_failed",
    # Transformation recipes (P6-04..07) and file ingestion (P6-06)
    "recipe.saved", "recipe.published", "recipe.run.started", "recipe.run.completed", "recipe.run.blocked",
    "recipe.run.refused", "recipe.run.failed", "file.ingested",
    # Workspace brief and readiness (P4-04)
    "brief.updated", "readiness.assessed",
    # Steps, branches and notebooks: the Data Thread (P7-04, P7-05, P7-12)
    "step.created", "step.edited", "step.executed", "step.flagged", "step.pinned", "step.pin_refreshed",
    "branch.forked", "branch.merged", "notebook.created", "notebook.updated",
    # Pipelines, incremental runs and the managed writer (P6-01..P6-03): operational alerts included
    "pipeline.saved", "pipeline.published", "pipeline.dry_run.completed", "pipeline.dry_run.blocked",
    "pipeline.dry_run.failed", "pipeline.run.completed", "pipeline.run.blocked", "pipeline.run.failed",
    "writer_destination.provisioned", "pipeline.materialized", "pipeline.materialization.failed",
    "pipeline.rolled_back", "pipeline.freshness.breached",
    # Isolated compute pools (P7-06, ADR-0022): a task dispatched to compute-py / compute-ml, as its worker reports it
    "task.started", "task.progress", "task.completed", "task.failed",
    # Governed classical ML (P5-01..P5-06, ADR-0024): experiments, the model registry, approved batch scoring
    "ml.experiment.started", "ml.experiment.completed", "ml.experiment.refused", "ml.experiment.failed",
    "ml.model.registered", "ml.model.promoted", "ml.model.rolled_back", "ml.scoring.planned", "ml.scoring.completed",
    "ml.scoring.refused", "ml.scoring.duplicate",
    # General entity matching (INT-004, N-7): proposals, review, the approved crosswalk
    "entity_match.started", "entity_match.proposed", "entity_match.failed", "entity_match.reviewed",
    "entity_match.promotion_requested", "entity_match.promoted",
    # Controlled pilot (P4-09): named business and technical owners of a workspace and of a source
    "workspace.owners_changed", "source.owners_changed",
    # What-if scenarios (N-9): a governed query with declared changes; every number labelled observed or simulated
    "scenario.computed",
    # Approved external delivery (N-3): destinations authorized by hash-bound approvals, then bounded-retry sends
    "delivery.destination_requested", "delivery.destination_authorized", "delivery.destination_rejected",
    "delivery.destination_revoked", "delivery.queued", "delivery.sent", "delivery.retrying", "delivery.dead_lettered",
    "delivery.refused",
    # Physical-design advice (N-11): index / partitioning / clustering recommendations, never applied
    "index_advice.generated", "index_advice.reviewed",
    # Evidence fusion (N-8): a finding's or answer's measured and document citations recorded; a document
    # claim that disagrees with measured data flagged (measured data wins)
    "evidence.citations_recorded", "evidence.conflict_flagged",
    # Existing-dashboard mode (N-2, BI-011/012): import, drift, proposed changes and their approved write-back
    "bi_dashboard.imported", "bi_dashboard.drift_detected", "bi_dashboard.update_requested", "bi_dashboard.updated",
}

RUN_TERMINAL = {"COMPLETED", "FAILED", "REJECTED", "CANCELLED"}
RUN_STATUSES = ["NEW", "PLANNING", "READY", "RUNNING", "WAITING_TOOL", "WAITING_USER", "PAUSED",
                "EVALUATING", "VERIFYING", *sorted(RUN_TERMINAL)]
TASK_STATUSES = ["NEW", "READY", "RUNNING", "WAITING_USER", "COMPLETED", "FAILED", "SKIPPED", "CANCELLED", "INVALIDATED"]
