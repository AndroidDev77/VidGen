"""Finding recomputation, remediation routing and the final completion gate.

The gate is the only place a T22 decision is made, and it is made from validated
structure, never from a provider's opinion of itself:

* Every failed deterministic, audio or caption check becomes a blocking finding
  with the measurement that produced it as evidence. No provider score, averaged
  dimension or human decision can remove one.
* A provider proposal becomes a canonical finding only after the categories and
  evidence it cites are checked. A proposal in a structurally blocking category
  blocks; anything the first pass was not confident about becomes
  ``review_required`` and is escalated to bounded adjudication.
* ``PASS`` is computed, not asserted: it requires zero blocking findings, zero
  failed deterministic checks and zero unresolved reviews.

Routing is a classification, not an action. T22 emits a
:class:`FinalRemediationRoute`; the parent workflow or an authorized user action
executes it against the stage that owns the defect.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import UUID

from services.qa.final_evidence import (
    SampledFrame,
    check_evidence,
    deterministic_id,
    frame_evidence,
    nearest_frame,
)
from services.qa.final_rubric import (
    BLOCKING_CATEGORIES,
    GATE_VERSION,
    REMEDIATION_ROUTING,
)
from vidgen.contracts.final_editorial import (
    ADJUDICATION_CONFIDENCE_FLOOR,
    SEVERITY_ORDER,
    FinalAudioCheck,
    FinalCaptionCheck,
    FinalCheckType,
    FinalDeterministicCheck,
    FinalEditorialAdjudication,
    FinalEditorialCategory,
    FinalEditorialEvidence,
    FinalEditorialFinding,
    FinalEditorialProviderFinding,
    FinalEditorialProviderResult,
    FinalFindingSeverity,
    FinalGateDecision,
    FinalIssueCode,
    FinalQAConfiguration,
    FinalQADecision,
    FinalRemediationRoute,
    FinalRemediationTarget,
)

#: Which editorial category a deterministic check family answers to, so a media
#: failure is reported under a dimension a reviewer recognizes.
_CHECK_CATEGORY: dict[FinalCheckType, FinalEditorialCategory] = {
    FinalCheckType.LINEAGE: FinalEditorialCategory.SCRIPT_CONTRADICTION,
    FinalCheckType.MEDIA: FinalEditorialCategory.SCENE_COMPLETENESS,
    FinalCheckType.TIMELINE: FinalEditorialCategory.PACING,
    FinalCheckType.AUDIO: FinalEditorialCategory.NARRATION_VISUAL_AGREEMENT,
    FinalCheckType.CAPTION: FinalEditorialCategory.CAPTION_NARRATION_AGREEMENT,
    FinalCheckType.MANIFEST: FinalEditorialCategory.SCENE_COMPLETENESS,
}

#: Deterministic issue codes whose remediation is not the family default.
_CODE_ROUTING: dict[FinalIssueCode, FinalRemediationTarget] = {
    FinalIssueCode.SHOT_COVERAGE_GAP: FinalRemediationTarget.RERENDER_T17,
    FinalIssueCode.SHOT_COVERAGE_OVERLAP: FinalRemediationTarget.RERENDER_T17,
    FinalIssueCode.CORRUPT_RENDER_SECTION: FinalRemediationTarget.RERENDER_T17,
    FinalIssueCode.EXCESSIVE_FREEZE_INTERVAL: FinalRemediationTarget.REPAIR_SHOT_T21,
    FinalIssueCode.UNEXPECTED_BLACK_INTERVAL: FinalRemediationTarget.REPAIR_SHOT_T21,
    FinalIssueCode.UNAPPROVED_PROVIDER_AUDIO: FinalRemediationTarget.REMIX_AUDIO_T17,
}


def _default_route(check: FinalDeterministicCheck) -> FinalRemediationTarget:
    if isinstance(check, FinalCaptionCheck):
        return check.remediation_target
    if check.code in _CODE_ROUTING:
        return _CODE_ROUTING[check.code]
    if isinstance(check, FinalAudioCheck):
        return FinalRemediationTarget.REMIX_AUDIO_T17
    return FinalRemediationTarget.RERENDER_T17


def findings_from_checks(
    checks: Sequence[FinalDeterministicCheck],
    *,
    timeline_duration_us: int,
) -> list[FinalEditorialFinding]:
    """Turn every failed deterministic check into an evidenced blocking finding."""
    findings: list[FinalEditorialFinding] = []
    for check in checks:
        if check.status != "fail":
            continue
        evidence = check_evidence(check)
        start = min(check.start_us or 0, timeline_duration_us)
        end = min(check.end_us if check.end_us is not None else start, timeline_duration_us)
        findings.append(
            FinalEditorialFinding(
                finding_id=check.check_id,
                category=_CHECK_CATEGORY[check.check_type],
                severity=FinalFindingSeverity.BLOCKING,
                blocking=True,
                confidence=1.0,
                issue_code=check.code,
                summary=check.message or f"{check.code.value} failed",
                start_us=start,
                end_us=max(end, start),
                caption_cue_sequences=(
                    [check.cue_sequence]
                    if isinstance(check, FinalCaptionCheck) and check.cue_sequence is not None
                    else []
                ),
                narration_segment_ids=(
                    [check.narration_segment_id]
                    if isinstance(check, FinalAudioCheck | FinalCaptionCheck)
                    and check.narration_segment_id is not None
                    else []
                ),
                evidence=[evidence],
                expected_behavior=(
                    f"{check.code.value} within {check.threshold}{check.unit}"
                    if check.threshold is not None
                    else f"{check.code.value} satisfied"
                ),
                observed_behavior=(
                    f"measured {check.measurement}{check.unit}"
                    if check.measurement is not None
                    else "check failed"
                ),
                remediation_target=_default_route(check),
                source_check=check.check_version,
                provenance="deterministic",
            )
        )
    return findings


def findings_from_provider(
    result: FinalEditorialProviderResult,
    *,
    timeline_duration_us: int,
    frames: Sequence[SampledFrame] = (),
    attempt_number: int = 1,
    provenance: str = "provider",
) -> list[FinalEditorialFinding]:
    """Validate and recompute provider proposals into canonical findings.

    A proposal never sets its own blocking flag. A structurally blocking category
    with a confident, evidenced proposal blocks; anything less confident becomes
    ``review_required`` so a human or the adjudicator settles it.
    """
    findings: list[FinalEditorialFinding] = []
    for index, proposal in enumerate(result.findings):
        blocking = (
            proposal.proposed_severity is FinalFindingSeverity.BLOCKING
            and proposal.category in BLOCKING_CATEGORIES
            and proposal.confidence >= ADJUDICATION_CONFIDENCE_FLOOR
        )
        if blocking:
            severity = FinalFindingSeverity.BLOCKING
        elif proposal.proposed_severity in {
            FinalFindingSeverity.BLOCKING,
            FinalFindingSeverity.REVIEW_REQUIRED,
        }:
            severity = FinalFindingSeverity.REVIEW_REQUIRED
        else:
            severity = proposal.proposed_severity
        start = min(proposal.start_us, timeline_duration_us)
        end = min(max(proposal.end_us, start), timeline_duration_us)
        evidence = _provider_evidence(proposal, frames, start, end)
        if blocking and not evidence:
            # A blocking claim without resolvable evidence is a review question,
            # never a silent failure of the render.
            severity, blocking = FinalFindingSeverity.REVIEW_REQUIRED, False
        findings.append(
            FinalEditorialFinding(
                finding_id=_finding_id(result.attempt_identity, index),
                category=proposal.category,
                severity=severity,
                blocking=blocking,
                confidence=proposal.confidence,
                issue_code=proposal.issue_code,
                summary=proposal.summary,
                start_us=start,
                end_us=end,
                shot_ids=list(proposal.shot_ids),
                caption_cue_sequences=list(proposal.caption_cue_sequences),
                sample_ids=list(proposal.sample_ids),
                evidence=evidence,
                expected_behavior=proposal.expected_behavior,
                observed_behavior=proposal.observed_behavior,
                remediation_target=(
                    proposal.proposed_remediation
                    if proposal.proposed_remediation is not FinalRemediationTarget.NONE
                    else REMEDIATION_ROUTING.get(
                        proposal.category, FinalRemediationTarget.HUMAN_EDITORIAL_REVIEW
                    )
                ),
                source_check=result.model[:64],
                provider_attempt_number=attempt_number,
                provenance=provenance,  # type: ignore[arg-type]
            )
        )
    return findings


def _finding_id(attempt_identity: str, index: int) -> UUID:
    return deterministic_id("provider-finding", attempt_identity, index)


def _provider_evidence(
    proposal: FinalEditorialProviderFinding,
    frames: Sequence[SampledFrame],
    start: int,
    end: int,
) -> list[FinalEditorialEvidence]:
    """Resolve a proposal's citations to frames that were really sampled."""
    code = proposal.issue_code.value
    by_id = {frame.sample_id: frame for frame in frames}
    evidence = [
        frame_evidence(by_id[sample_id], code=code)
        for sample_id in proposal.sample_ids
        if sample_id in by_id
    ]
    if evidence:
        return evidence[:16]
    anchor = nearest_frame(frames, start)
    if anchor is not None and proposal.shot_ids:
        return [frame_evidence(anchor, code=code)]
    if proposal.caption_cue_sequences:
        return [
            FinalEditorialEvidence(
                evidence_id=deterministic_id("cue-evidence", code, start, end),
                evidence_type="caption_cue",
                start_us=start,
                end_us=end,
                caption_cue_sequence=proposal.caption_cue_sequences[0],
                explanation=proposal.summary[:500],
            )
        ]
    return []


def adjudication_triggers(
    findings: Sequence[FinalEditorialFinding], configuration: FinalQAConfiguration
) -> list[FinalEditorialFinding]:
    """The bounded set of findings a second opinion is worth buying for."""
    disputed = [
        finding
        for finding in findings
        if finding.provenance == "provider"
        and finding.severity is FinalFindingSeverity.REVIEW_REQUIRED
    ]
    disputed.sort(key=lambda finding: (finding.confidence, str(finding.finding_id)))
    return disputed[: configuration.max_adjudications]


def apply_adjudication(
    findings: Sequence[FinalEditorialFinding], adjudication: FinalEditorialAdjudication
) -> list[FinalEditorialFinding]:
    """Fold a decided adjudication back into the canonical findings.

    An undecided adjudication changes nothing: the disputed findings stay
    ``review_required`` and the gate returns ``REVIEW``.
    """
    if not adjudication.decided:
        return list(findings)
    confirmed = set(adjudication.confirmed_finding_ids)
    dismissed = set(adjudication.dismissed_finding_ids)
    resolved: list[FinalEditorialFinding] = []
    for finding in findings:
        blocking_category = finding.category in BLOCKING_CATEGORIES
        if finding.finding_id in confirmed and blocking_category and not finding.evidence:
            # A blocking finding must carry evidence, by contract. Confirming
            # one that cites nothing resolvable would write a report that fails
            # its own validation the next time it is read. Downgrading it to a
            # warning would be worse still: that silently clears the gate on a
            # finding the adjudicator just confirmed. It stays a review
            # question, which is the honest outcome for an unevidenced claim.
            resolved.append(
                finding.model_copy(update={"confidence": adjudication.confidence})
            )
        elif finding.finding_id in confirmed and blocking_category:
            resolved.append(
                finding.model_copy(
                    update={
                        "severity": FinalFindingSeverity.BLOCKING,
                        "blocking": True,
                        "confidence": adjudication.confidence,
                        "provenance": "adjudication",
                    }
                )
            )
        elif finding.finding_id in confirmed:
            resolved.append(
                finding.model_copy(
                    update={
                        "severity": FinalFindingSeverity.WARNING,
                        "blocking": False,
                        "confidence": adjudication.confidence,
                        "provenance": "adjudication",
                    }
                )
            )
        elif finding.finding_id in dismissed:
            resolved.append(
                finding.model_copy(
                    update={
                        "severity": FinalFindingSeverity.INFORMATIONAL,
                        "blocking": False,
                        "confidence": adjudication.confidence,
                        "provenance": "adjudication",
                    }
                )
            )
        else:
            resolved.append(finding)
    return resolved


def bound_findings(
    findings: Sequence[FinalEditorialFinding], configuration: FinalQAConfiguration
) -> list[FinalEditorialFinding]:
    """Truncate to the contract bound, most severe first, deterministically."""
    ordered = sorted(
        findings,
        key=lambda finding: (
            SEVERITY_ORDER[finding.severity],
            -finding.confidence,
            str(finding.finding_id),
        ),
    )
    return ordered[: configuration.max_findings]


def remediation_routes(
    findings: Sequence[FinalEditorialFinding],
) -> list[FinalRemediationRoute]:
    """Group actionable findings by the stage that owns their repair."""
    grouped: dict[FinalRemediationTarget, list[FinalEditorialFinding]] = {}
    for finding in findings:
        if finding.severity not in {
            FinalFindingSeverity.BLOCKING,
            FinalFindingSeverity.REVIEW_REQUIRED,
        }:
            continue
        target = finding.remediation_target
        if target is FinalRemediationTarget.NONE:
            target = FinalRemediationTarget.HUMAN_EDITORIAL_REVIEW
        grouped.setdefault(target, []).append(finding)
    routes: list[FinalRemediationRoute] = []
    for target, items in sorted(grouped.items(), key=lambda entry: entry[0].value):
        shots = sorted({shot for item in items for shot in item.shot_ids}, key=str)
        cues = sorted({cue for item in items for cue in item.caption_cue_sequences})
        routes.append(
            FinalRemediationRoute(
                target=target,
                finding_ids=[item.finding_id for item in items][:64],
                shot_ids=shots[:64],
                caption_cue_sequences=cues[:64],
                reason=f"{len(items)} finding(s) routed to {target.value}",
                # Any change to a selected input invalidates the render, so
                # every route except a pure review requires a new T17 render and
                # a new T22 run against the new render identity.
                requires_new_render=target is not FinalRemediationTarget.HUMAN_EDITORIAL_REVIEW,
            )
        )
    return routes


def decide(
    *,
    findings: Sequence[FinalEditorialFinding],
    checks: Sequence[FinalDeterministicCheck],
    final_video_asset_id: UUID,
    render_identity: str,
    resolved_review_ids: frozenset[UUID] = frozenset(),
) -> FinalGateDecision:
    """Recompute the completion gate. ``PASS`` is earned, never asserted."""
    blocking = [finding for finding in findings if finding.blocking]
    unresolved = [
        finding
        for finding in findings
        if finding.severity is FinalFindingSeverity.REVIEW_REQUIRED
        and finding.finding_id not in resolved_review_ids
    ]
    warnings = [finding for finding in findings if finding.severity is FinalFindingSeverity.WARNING]
    failures = [check for check in checks if check.status == "fail"]
    reasons: list[str] = []
    if failures:
        reasons.append(f"{len(failures)} deterministic check(s) failed")
    if blocking:
        reasons.append(f"{len(blocking)} blocking editorial finding(s)")
    if unresolved:
        reasons.append(f"{len(unresolved)} unresolved review finding(s)")
    if blocking or failures:
        decision = FinalQADecision.FAIL
    elif unresolved:
        decision = FinalQADecision.REVIEW
    else:
        decision = FinalQADecision.PASS
        reasons.append("all deterministic, audio, caption and editorial checks passed")
    return FinalGateDecision(
        gate_version=GATE_VERSION,
        decision=decision,
        final_video_asset_id=final_video_asset_id,
        render_identity=render_identity,
        blocking_finding_count=len(blocking),
        review_finding_count=len(unresolved),
        warning_finding_count=len(warnings),
        deterministic_failure_count=len(failures),
        unresolved_review_count=len(unresolved),
        reasons=reasons[:32],
        decided_at=datetime.now(UTC),
    )
