"""Compatibility exports for the canonical T19 workflow contracts."""

from vidgen.contracts.continuity_workflow import (
    ApplyReferencesCommand,
    BuildReferencesCommand,
    ReferenceApprovalSignal,
    ReferenceDraftResult,
    ReferenceWorkflowInput,
    ReferenceWorkflowResult,
    ReferenceWorkflowStatus,
)

__all__ = [
    "ApplyReferencesCommand",
    "BuildReferencesCommand",
    "ReferenceApprovalSignal",
    "ReferenceDraftResult",
    "ReferenceWorkflowInput",
    "ReferenceWorkflowResult",
    "ReferenceWorkflowStatus",
]
