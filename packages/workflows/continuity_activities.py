"""The T19 activity boundary: ID-only messages, injected production handlers.

``ContinuityReferenceWorkflow`` has always named these two activities. Until
T18b nothing defined or registered them, so the workflow could not run in
production at all. They follow the same shape as the T16 shot activities: the
activity itself is a thin ``@activity.defn`` that heartbeats and delegates to a
handler the worker installs, and the handler owns the database session, the
provider client and the blob store.
"""

from __future__ import annotations

from collections.abc import Callable

from temporalio import activity

from packages.workflows.activities import activity_heartbeats
from vidgen.contracts.continuity_workflow import (
    ReferenceApprovalSignal,
    ReferenceDraftResult,
    ReferenceWorkflowInput,
    ReferenceWorkflowResult,
)
from vidgen.contracts.workflow import StageActivityInput

BuildReferencesHandler = Callable[[ReferenceWorkflowInput], ReferenceDraftResult]
ApplyReferencesHandler = Callable[[ReferenceApprovalSignal], ReferenceWorkflowResult]
ResolveInputsHandler = Callable[[StageActivityInput], ReferenceWorkflowInput]

_build: dict[str, BuildReferencesHandler] = {}
_apply: dict[str, ApplyReferencesHandler] = {}
_resolve: dict[str, ResolveInputsHandler] = {}


def configure_continuity_activity_handlers(
    *,
    build: BuildReferencesHandler | None,
    apply: ApplyReferencesHandler | None,
    resolve: ResolveInputsHandler | None = None,
) -> None:
    """Install the production T19 adapters. Passing ``None`` clears them."""
    _build.clear()
    _apply.clear()
    _resolve.clear()
    if build is not None:
        _build["build"] = build
    if apply is not None:
        _apply["apply"] = apply
    if resolve is not None:
        _resolve["resolve"] = resolve


@activity.defn(name="resolve_continuity_inputs")
def resolve_continuity_inputs(request: StageActivityInput) -> ReferenceWorkflowInput:
    """Resolve the project's authoritative T10 and T13 inputs for T19.

    Returns the compact reference-run message the T19 child is started with,
    including the deterministic reference-run ID derived from those inputs. A
    restarted parent therefore adopts the same child instead of drafting a
    second set of references.
    """
    handler = _resolve.get("resolve")
    if handler is None:
        raise RuntimeError("no activity handler configured for resolve_continuity_inputs")
    with activity_heartbeats("resolve_continuity_inputs"):
        return handler(request)


@activity.defn(name="build_continuity_references")
def build_continuity_references(request: ReferenceWorkflowInput) -> ReferenceDraftResult:
    """Draft or reuse every reference sheet the project requires.

    The message carries the project, the authoritative T10 analysis, the
    authoritative T13 storyboard and a reference run ID. Candidate frames,
    prompts, generated images and validation payloads stay in durable storage;
    the result is IDs, counts and whether a human decision is owed.
    """
    handler = _build.get("build")
    if handler is None:
        raise RuntimeError("no activity handler configured for build_continuity_references")
    with activity_heartbeats("build_continuity_references"):
        return handler(request)


@activity.defn(name="apply_continuity_references")
def apply_continuity_references(signal: ReferenceApprovalSignal) -> ReferenceWorkflowResult:
    """Bind the approved references and stale only the shots they changed.

    Refuses a stale approval: the activity re-reads the approved rows and their
    lineage, so a decision made against a superseded reference set can never
    bind a shot.
    """
    handler = _apply.get("apply")
    if handler is None:
        raise RuntimeError("no activity handler configured for apply_continuity_references")
    with activity_heartbeats("apply_continuity_references"):
        return handler(signal)


CONTINUITY_ACTIVITIES = [
    resolve_continuity_inputs,
    build_continuity_references,
    apply_continuity_references,
]
