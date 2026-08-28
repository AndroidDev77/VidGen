"""Deterministic T21 unit tests: classification, policy, planning and providers.

Nothing here makes a paid provider call, and nothing here needs a Google
credential. The Veo adapter is exercised through its pure serialization
functions and a mocked transport; the deterministic fake covers the rest.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from PIL import Image, ImageDraw

from services.animation.veo import (
    DEFAULT_VEO_MODEL,
    VEO_30_FAST,
    VEO_31_FAST,
    VEO_CAPABILITIES,
    UnsupportedVeoCapability,
    VeoOperationTimeout,
    VeoRateLimited,
    VeoSubmissionAmbiguous,
    capability_profile,
    estimate_veo_cost,
    veo_pricing_catalog,
)
from services.animation.veo_adapter import (
    GoogleVeoProvider,
    VeoInputImage,
    VeoInputImages,
    validate_veo_request,
    veo_request_payload,
)
from services.animation.veo_fake import FakeVeoProvider
from services.qa.repair_classifier import (
    CLASSIFIER_VERSION,
    REPAIR_CODE_DIAGNOSTICS,
    ClassificationContext,
    ReferenceIntegrity,
    RepairClassificationError,
    TechnicalSignal,
    classify,
)
from services.qa.repair_planner import (
    DeterministicRepairPlanner,
    LanguageModelRepairPlanner,
    PromptRepairRequest,
    RepairPlanningError,
    apply_edits,
    derive_seed,
    extract_constraints,
    prompt_hash,
    render_prompt,
    validate_delta,
)
from services.qa.repair_policy import POLICY_VERSION, RouteContext, default_policy, next_route
from services.renderer.parallax import ParallaxInputs, render_parallax
from services.renderer.parallax_manifest import (
    ParallaxRequest,
    ParallaxSource,
    build_plan,
    decide_eligibility,
    frame_count,
    render_identity,
)
from tests.repair_fixtures import qa_result
from tests.visual_qa_fixtures import shot_contract
from vidgen.contracts.repair import (
    HumanReviewReason,
    RepairAttemptKind,
    RepairFailureCategory,
    RepairRoute,
    RepairSeverity,
    VeoGenerationRequest,
    VeoOperationState,
)
from vidgen.contracts.storyboard import StoryboardShot
from vidgen.contracts.visual_qa import (
    VisualQADimension,
    VisualQAOutcome,
    VisualQARepairCode,
    VisualQAShotImportance,
)

CAPABILITY = "runway/2024-11-06"


def _shot(**overrides: Any) -> StoryboardShot:
    contract = shot_contract(
        shot_id=uuid4(),
        storyboard_run_id=uuid4(),
        segment_id=uuid4(),
        script_segment_id=uuid4(),
        narration_segment_id=uuid4(),
        sequence=0,
        character_id=uuid4(),
        location_id=uuid4(),
        importance=0.5,
    )
    contract.update(overrides)
    return StoryboardShot.model_validate(contract)


# --- classification ----------------------------------------------------------
def test_every_t20_repair_code_maps_to_a_t21_diagnostic() -> None:
    """A drift between the two taxonomies must fail loudly, never silently."""
    assert set(REPAIR_CODE_DIAGNOSTICS) == set(VisualQARepairCode)


def test_classifies_a_prompt_issue() -> None:
    classification = classify(qa_result(repair_codes=(VisualQARepairCode.WRONG_ACTION,)))
    assert classification.category is RepairFailureCategory.PROMPT_ISSUE
    assert classification.classifier_version == CLASSIFIER_VERSION
    assert classification.primary_code.value == "missing_or_incorrect_action"


def test_classifies_a_seed_issue() -> None:
    classification = classify(
        qa_result(
            repair_codes=(VisualQARepairCode.INSUFFICIENT_MOTION,),
            finding_repair_codes=(VisualQARepairCode.INSUFFICIENT_MOTION,),
        )
    )
    assert classification.category is RepairFailureCategory.SEED_ISSUE


def test_classifies_a_provider_issue_from_a_technical_signal() -> None:
    classification = classify(
        qa_result(),
        context=ClassificationContext(
            technical_signals=(TechnicalSignal.PROVIDER_TIMEOUT,),
            technical_messages={TechnicalSignal.PROVIDER_TIMEOUT: "provider timed out"},
        ),
    )
    assert classification.category is RepairFailureCategory.PROVIDER_ISSUE
    assert classification.primary_code.value == "provider_timeout_or_service_failure"


def test_classifies_an_impossible_shot() -> None:
    classification = classify(
        qa_result(
            repair_codes=(VisualQARepairCode.DURATION_MISMATCH,),
            finding_repair_codes=(VisualQARepairCode.DURATION_MISMATCH,),
        )
    )
    assert classification.category is RepairFailureCategory.IMPOSSIBLE_SHOT
    assert classification.severity is RepairSeverity.UNRECOVERABLE


def test_classifies_a_reference_issue_and_demands_upstream_correction() -> None:
    """An approved reference that is itself wrong is never repaired by spending."""
    reference = uuid4()
    classification = classify(
        qa_result(
            repair_codes=(VisualQARepairCode.WRONG_CHARACTER_IDENTITY,),
            finding_code="reference_conflict",
            finding_repair_codes=(VisualQARepairCode.WRONG_CHARACTER_IDENTITY,),
            compared_reference_asset_id=reference,
        ),
        context=ClassificationContext(reference_integrity=ReferenceIntegrity.STALE),
    )
    assert classification.category is RepairFailureCategory.REFERENCE_ISSUE
    assert classification.requires_upstream_reference_correction
    # The evidence explaining the conflict is preserved, not discarded.
    conflict = next(
        item for item in classification.diagnostics if item.code.value == "reference_conflict"
    )
    assert conflict.severity == "hard_failure"
    assert conflict.summary


def test_a_passing_result_has_nothing_to_repair() -> None:
    passing = qa_result(score=96.0, outcome=VisualQAOutcome.PASS, repair_codes=())
    with pytest.raises(RepairClassificationError):
        classify(passing)


def test_scores_below_seventy_five_earn_a_structural_repair() -> None:
    assert classify(qa_result(score=60.0)).severity is RepairSeverity.STRUCTURAL


def test_scores_between_seventy_five_and_the_threshold_earn_a_targeted_repair() -> None:
    assert classify(qa_result(score=80.0)).severity is RepairSeverity.TARGETED


def test_a_hero_shot_uses_the_ninety_point_threshold() -> None:
    """87 passes a normal shot and fails a hero one; T21 copies T20's decision."""
    hero = classify(qa_result(score=87.0, importance=VisualQAShotImportance.HERO))
    assert hero.pass_threshold == 90
    assert hero.severity is RepairSeverity.TARGETED


def test_a_hard_failure_never_passes_on_score_alone() -> None:
    """A near-perfect score with a categorical hard failure is still structural."""
    classification = classify(
        qa_result(
            score=99.0,
            hard_failure_dimension=VisualQADimension.CHARACTER_IDENTITY,
            hard_failure_code=VisualQARepairCode.WRONG_CHARACTER_IDENTITY,
            repair_codes=(VisualQARepairCode.WRONG_CHARACTER_IDENTITY,),
        )
    )
    assert classification.hard_failure
    assert classification.severity is RepairSeverity.STRUCTURAL
    assert classification.primary_code.value == "wrong_character_identity"


def test_corrupt_media_never_triggers_a_paid_generation() -> None:
    classification = classify(
        qa_result(
            repair_codes=(VisualQARepairCode.DECODE_FAILURE,),
            finding_repair_codes=(VisualQARepairCode.DECODE_FAILURE,),
        )
    )
    assert classification.deterministic_only


# --- policy ------------------------------------------------------------------
def _context(**overrides: Any) -> RouteContext:
    values: dict[str, Any] = {
        "classification": classify(qa_result()),
        "policy": default_policy(),
        "fallback_eligible": True,
    }
    values.update(overrides)
    return RouteContext(**values)


def test_the_route_order_is_exactly_the_bounded_policy() -> None:
    routes = []
    for same, alternate, fallback in ((0, 0, 0), (1, 0, 0), (2, 0, 0), (2, 1, 0), (2, 1, 1)):
        routes.append(
            next_route(
                _context(
                    same_provider_repairs_used=same,
                    alternate_provider_attempts_used=alternate,
                    fallback_renders_used=fallback,
                )
            ).route
        )
    assert routes == [
        RepairRoute.SAME_PROVIDER_REPAIR,
        RepairRoute.SAME_PROVIDER_REPAIR,
        RepairRoute.ALTERNATE_PROVIDER,
        RepairRoute.DETERMINISTIC_FALLBACK,
        RepairRoute.HUMAN_REVIEW_REQUIRED,
    ]
    assert default_policy().policy_version == POLICY_VERSION


def test_at_most_two_same_provider_repairs() -> None:
    decision = next_route(_context(same_provider_repairs_used=2))
    assert decision.attempt_kind is RepairAttemptKind.ALTERNATE_PROVIDER


def test_at_most_one_alternate_provider_attempt() -> None:
    decision = next_route(
        _context(same_provider_repairs_used=2, alternate_provider_attempts_used=1)
    )
    assert decision.attempt_kind is RepairAttemptKind.DETERMINISTIC_FALLBACK


def test_the_policy_is_bounded_and_never_loops() -> None:
    decision = next_route(
        _context(
            same_provider_repairs_used=2,
            alternate_provider_attempts_used=1,
            fallback_renders_used=1,
        )
    )
    assert decision.route is RepairRoute.HUMAN_REVIEW_REQUIRED
    assert decision.human_review_reason is HumanReviewReason.ATTEMPT_LIMIT_REACHED
    assert not decision.consumes_attempt
    # One original, two same-provider repairs, one alternate, one fallback.
    assert default_policy().max_total_attempts == 5


def test_a_resumable_operation_consumes_no_attempt() -> None:
    decision = next_route(_context(resumable_operation=True))
    assert decision.route is RepairRoute.RESUME_PROVIDER_OPERATION
    assert not decision.consumes_attempt


def test_unpersisted_provider_output_is_resumed_not_regenerated() -> None:
    decision = next_route(_context(unpersisted_provider_output=True))
    assert decision.route is RepairRoute.RESUME_PROVIDER_OPERATION
    assert not decision.consumes_attempt


def test_an_ambiguous_submission_is_never_resubmitted() -> None:
    decision = next_route(_context(ambiguous_submission=True))
    assert decision.route is RepairRoute.HUMAN_REVIEW_REQUIRED
    assert decision.human_review_reason is HumanReviewReason.DETERMINISTIC_FAILURE


def test_budget_denial_stops_before_the_provider_call() -> None:
    decision = next_route(
        _context(
            budget_allows_next_attempt=False,
            budget_denial_reason=HumanReviewReason.PROJECT_BUDGET_DENIED,
        )
    )
    assert decision.route is RepairRoute.HUMAN_REVIEW_REQUIRED
    assert decision.human_review_reason is HumanReviewReason.PROJECT_BUDGET_DENIED
    assert decision.attempt_kind is None


def test_an_ineligible_fallback_routes_to_human_review() -> None:
    decision = next_route(
        _context(
            same_provider_repairs_used=2,
            alternate_provider_attempts_used=1,
            fallback_eligible=False,
            fallback_ineligibility_reasons=("mandatory physical action",),
        )
    )
    assert decision.human_review_reason is HumanReviewReason.FALLBACK_INELIGIBLE


def test_cancellation_is_honoured_between_paid_attempts() -> None:
    decision = next_route(_context(cancellation_requested=True))
    assert decision.human_review_reason is HumanReviewReason.CANCELLED_BEFORE_PAID_ATTEMPT


def test_an_upstream_reference_correction_never_spends() -> None:
    classification = classify(
        qa_result(finding_code="reference_conflict"),
        context=ClassificationContext(reference_integrity=ReferenceIntegrity.MISSING),
    )
    decision = next_route(_context(classification=classification))
    assert decision.route is RepairRoute.UPSTREAM_REFERENCE_CORRECTION
    assert not decision.consumes_attempt


def test_a_corrected_reference_is_not_a_permanent_dead_end() -> None:
    """Reference integrity comes from T20's findings, not a hash comparison.

    An upstream correction necessarily changes the approved bundle, so judging
    integrity by comparing the current bundle hash against the one the QA result
    recorded would make every corrected reference look permanently stale - and
    a restarted repair would route straight back to human review forever.
    """
    corrected = classify(
        qa_result(
            repair_codes=(VisualQARepairCode.WRONG_CHARACTER_IDENTITY,),
            finding_repair_codes=(VisualQARepairCode.WRONG_CHARACTER_IDENTITY,),
        ),
        context=ClassificationContext(reference_integrity=ReferenceIntegrity.VALID),
    )
    assert not corrected.requires_upstream_reference_correction
    decision = next_route(_context(classification=corrected))
    assert decision.attempt_kind is RepairAttemptKind.SAME_PROVIDER_REPAIR


def test_a_diagnostic_with_no_editable_clause_is_repaired_by_a_new_seed() -> None:
    """A provider timeout implicates no clause, so the retry redraws the sample."""
    request = _request(
        classification_score=80.0,
    )
    timed_out = classify(
        qa_result(score=80.0),
        context=ClassificationContext(technical_signals=(TechnicalSignal.PROVIDER_TIMEOUT,)),
    )
    request = PromptRepairRequest(
        classification=timed_out,
        constraints=request.constraints,
        base_prompt=request.base_prompt,
        base_prompt_hash=request.base_prompt_hash,
        previous_seed=None,
        attempt_ordinal=1,
        attempt_identity=request.attempt_identity,
    )
    delta = DeterministicRepairPlanner().plan(request)
    validate_delta(delta, request)
    assert delta.seed_changed and delta.new_seed is not None
    assert delta.touched_constraint_ids == []


def test_a_cloud_storage_output_location_is_refused_rather_than_mis_fetched() -> None:
    """T21 never sets ``storageUri``, so a gs:// handle means a foreign request."""
    import asyncio

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "name": "operations/op-1",
                "done": True,
                "response": {"videos": [{"gcsUri": "gs://bucket/clip.mp4"}]},
            },
        )

    provider = GoogleVeoProvider(
        project="p", access_token=lambda: "token", transport=httpx.MockTransport(handler)
    )

    async def drive() -> None:
        try:
            await provider.download("operations/op-1", Path("/tmp/unused.mp4"))
        finally:
            await provider.aclose()

    with pytest.raises(ValueError, match="veo_unsupported_output_location"):
        asyncio.run(drive())


def test_a_generation_that_ignored_a_valid_reference_still_earns_a_repair() -> None:
    """Only an invalid reference is escalated; a missed one is repairable."""
    classification = classify(
        qa_result(
            repair_codes=(VisualQARepairCode.WRONG_CHARACTER_IDENTITY,),
            finding_repair_codes=(VisualQARepairCode.WRONG_CHARACTER_IDENTITY,),
        ),
        context=ClassificationContext(reference_integrity=ReferenceIntegrity.VALID),
    )
    assert classification.category is RepairFailureCategory.PROMPT_ISSUE
    assert next_route(_context(classification=classification)).attempt_kind is (
        RepairAttemptKind.SAME_PROVIDER_REPAIR
    )


# --- prompt repair -----------------------------------------------------------
def _request(classification_score: float = 60.0, **kwargs: Any) -> PromptRepairRequest:
    shot = _shot()
    constraints = extract_constraints(shot, capability_profile=CAPABILITY)
    base = render_prompt(constraints)
    return PromptRepairRequest(
        classification=classify(qa_result(score=classification_score, **kwargs)),
        constraints=tuple(constraints),
        base_prompt=base,
        base_prompt_hash=prompt_hash(base),
        previous_seed=None,
        attempt_ordinal=1,
        attempt_identity="a1b2c3d4" + "0" * 56,
    )


def test_a_minimal_repair_changes_only_implicated_clauses() -> None:
    request = _request()
    delta = DeterministicRepairPlanner().plan(request)
    validate_delta(delta, request)
    assert delta.touched_constraint_ids == ["action"]
    assert "action" not in delta.preserved_constraint_ids


def test_a_repair_preserves_every_unaffected_constraint() -> None:
    request = _request()
    delta = DeterministicRepairPlanner().plan(request)
    required = {
        "character-identity-0",
        "character-count",
        "character-state-0",
        "location",
        "timing",
        "continuity",
        "reference-binding",
        "safety",
        "provider-capability",
    }
    assert required <= set(delta.preserved_constraint_ids)
    repaired = render_prompt(request.constraints, delta)
    # Every rendered immutable clause survives verbatim in the repaired prompt.
    for constraint in request.constraints:
        if constraint.mutable or not constraint.rendered:
            continue
        assert constraint.clause in repaired


def test_the_prompt_delta_hashes_the_exact_repaired_prompt() -> None:
    request = _request()
    delta = DeterministicRepairPlanner().plan(request)
    assert delta.before_prompt_hash == request.base_prompt_hash
    assert delta.after_prompt_hash == prompt_hash(render_prompt(request.constraints, delta))
    assert delta.after_prompt_hash != delta.before_prompt_hash


def test_the_deterministic_planner_is_deterministic() -> None:
    request = _request()
    planner = DeterministicRepairPlanner()
    assert planner.plan(request).model_dump() == planner.plan(request).model_dump()
    assert derive_seed(request.attempt_identity) == derive_seed(request.attempt_identity)


def test_a_structural_repair_also_changes_the_seed() -> None:
    delta = DeterministicRepairPlanner().plan(_request(classification_score=60.0))
    assert delta.seed_changed and delta.new_seed is not None


def test_validation_rejects_a_delta_that_drops_a_required_constraint() -> None:
    request = _request()
    delta = DeterministicRepairPlanner().plan(request)
    tampered = delta.model_copy(
        update={
            "preserved_constraint_ids": [
                item for item in delta.preserved_constraint_ids if item != "location"
            ]
        }
    )
    with pytest.raises(RepairPlanningError, match="does not preserve required constraints"):
        validate_delta(tampered, request)


def test_validation_rejects_a_delta_that_touches_an_immutable_constraint() -> None:
    request = _request()
    delta = DeterministicRepairPlanner().plan(request)
    tampered = delta.model_copy(
        update={
            "touched_constraint_ids": [*delta.touched_constraint_ids, "location"],
            "preserved_constraint_ids": [
                item for item in delta.preserved_constraint_ids if item != "location"
            ],
        }
    )
    with pytest.raises(RepairPlanningError, match="may not touch immutable constraints"):
        validate_delta(tampered, request)


def test_a_configured_model_planner_must_pass_the_same_validation() -> None:
    """A model proposes; deterministic validation still decides."""
    request = _request()
    proposal = json.dumps(
        {
            "repair_reason": "make the beat explicit",
            "added_clauses": ["Keep the beat unmistakably on screen."],
            "removed_clause_ids": [],
            "rewritten_clauses": [],
            "change_seed": True,
        }
    )
    planner = LanguageModelRepairPlanner(lambda _prompt: proposal, model="test-model")
    delta = planner.plan(request)
    assert delta.added_clauses == ["Keep the beat unmistakably on screen."]
    assert "test-model" in planner.version
    # The bounded brief never leaks an immutable clause the model could edit.
    brief = json.loads(LanguageModelRepairPlanner.render_instruction(request))
    editable = {item["constraint_id"] for item in brief["editable_clauses"]}
    assert editable <= {"action"}
    # No immutable clause is even shown to the model, so it cannot edit one.
    rendered = json.dumps(brief["editable_clauses"])
    for constraint in request.constraints:
        if not constraint.mutable:
            assert constraint.clause not in rendered


def test_a_model_planner_answering_prose_is_rejected_before_any_spend() -> None:
    planner = LanguageModelRepairPlanner(lambda _prompt: "sure, here you go!", model="m")
    with pytest.raises(RepairPlanningError, match="structured contract"):
        planner.plan(_request())


def test_a_model_planner_may_not_edit_an_immutable_clause() -> None:
    request = _request()
    proposal = json.dumps(
        {
            "repair_reason": "swap the location",
            "added_clauses": [],
            "removed_clause_ids": ["location"],
            "rewritten_clauses": [],
            "change_seed": False,
        }
    )
    planner = LanguageModelRepairPlanner(lambda _prompt: proposal, model="m")
    with pytest.raises(RepairPlanningError):
        planner.plan(request)


def test_prompt_rendering_is_stable_and_order_preserving() -> None:
    shot = _shot()
    constraints = extract_constraints(shot, capability_profile=CAPABILITY)
    assert render_prompt(constraints) == apply_edits(constraints)
    assert prompt_hash(render_prompt(constraints)) == prompt_hash(apply_edits(constraints))


# --- Veo capability profile and adapter --------------------------------------
def test_the_default_veo_model_is_the_cost_controlled_fast_variant() -> None:
    assert DEFAULT_VEO_MODEL == VEO_31_FAST.model
    assert VEO_31_FAST.variant == "fast"
    assert estimate_veo_cost(VEO_31_FAST.model, 8) < estimate_veo_cost("veo-3.1-generate-001", 8)


def test_veo_model_names_live_in_exactly_one_place() -> None:
    catalog = veo_pricing_catalog()
    assert {rate.model for rate in catalog.rates} == set(VEO_CAPABILITIES)


def test_the_capability_profile_hash_is_stable_and_version_bound() -> None:
    assert capability_profile().profile_hash == VEO_31_FAST.profile_hash
    assert VEO_31_FAST.profile_hash != VEO_30_FAST.profile_hash


def test_an_unknown_veo_model_is_a_configuration_error() -> None:
    with pytest.raises(UnsupportedVeoCapability):
        capability_profile("veo-9.9-imaginary")


def test_capability_enforcement_rejects_an_undeclared_feature() -> None:
    """A model with no last-frame control never silently drops one."""
    request = _veo_request(model=VEO_30_FAST.model, profile=VEO_30_FAST, last_frame=True)
    with pytest.raises(UnsupportedVeoCapability, match="unsupported_strict_last_frame"):
        validate_veo_request(request, VEO_30_FAST)


def test_capability_enforcement_rejects_an_unsupported_duration() -> None:
    request = _veo_request(duration=7)
    with pytest.raises(UnsupportedVeoCapability, match="unsupported_duration"):
        validate_veo_request(request, VEO_31_FAST)


def test_a_shorter_shot_is_generated_at_the_smallest_supported_duration() -> None:
    assert VEO_31_FAST.smallest_supported_duration(3.0) == 4
    assert VEO_31_FAST.smallest_supported_duration(4.0) == 4
    assert VEO_31_FAST.smallest_supported_duration(5.5) == 6
    with pytest.raises(UnsupportedVeoCapability):
        VEO_31_FAST.smallest_supported_duration(30.0)


def _veo_request(
    *,
    model: str = VEO_31_FAST.model,
    profile: Any = VEO_31_FAST,
    duration: int = 4,
    last_frame: bool = False,
) -> VeoGenerationRequest:
    return VeoGenerationRequest(
        application_idempotency_key="a" * 64,
        project_id=uuid4(),
        repair_run_id=uuid4(),
        repair_attempt_id=uuid4(),
        shot_id=uuid4(),
        attempt_ordinal=3,
        model=model,
        capability_profile_version=profile.capability_version,
        capability_profile_hash=profile.profile_hash,
        prompt="A wide comic frame of the lead reacting.",
        prompt_hash="b" * 64,
        first_frame_asset_id=uuid4(),
        first_frame_sha256="c" * 64,
        last_frame_asset_id=uuid4() if last_frame else None,
        last_frame_sha256="d" * 64 if last_frame else None,
        duration_seconds=duration,
        aspect_ratio="16:9",
        resolution="720p",
        generate_audio=False,
    )


def test_the_veo_request_serializes_exactly_as_vertex_expects() -> None:
    request = _veo_request()
    payload = veo_request_payload(
        request,
        VeoInputImages(
            first_frame=VeoInputImage(asset_id_hex="x", content=b"\x89PNG", media_type="image/png")
        ),
    )
    instance = payload["instances"][0]
    parameters = payload["parameters"]
    assert instance["prompt"] == request.prompt
    assert instance["image"]["mimeType"] == "image/png"
    assert parameters["durationSeconds"] == 4
    assert parameters["aspectRatio"] == "16:9"
    assert parameters["resolution"] == "720p"
    # T17 owns the final mix, so no provider audio is requested.
    assert parameters["generateAudio"] is False


def test_the_veo_adapter_polls_a_long_running_operation(tmp_path: Path) -> None:
    """The adapter submits once, then polls the operation it was given."""
    del tmp_path
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if request.url.path.endswith(":predictLongRunning"):
            calls.append("submit")
            return httpx.Response(200, json={"name": "operations/op-1"})
        calls.append(f"poll:{body['operationName']}")
        if len(calls) < 3:
            return httpx.Response(200, json={"name": "operations/op-1", "done": False})
        return httpx.Response(
            200,
            json={
                "name": "operations/op-1",
                "done": True,
                "response": {"videos": [{"bytesBase64Encoded": "AAAA"}]},
            },
        )

    provider = GoogleVeoProvider(
        project="p",
        access_token=lambda: "token",
        transport=httpx.MockTransport(handler),
    )
    import asyncio

    async def drive() -> Any:
        name = await provider.submit(_veo_request(), VeoInputImages())
        first = await provider.poll(name)
        second = await provider.poll(name)
        await provider.aclose()
        return name, first, second

    name, first, second = asyncio.run(drive())
    assert name == "operations/op-1"
    assert first.state is VeoOperationState.RUNNING
    assert second.state is VeoOperationState.SUCCEEDED
    assert calls == ["submit", "poll:operations/op-1", "poll:operations/op-1"]


def test_the_veo_adapter_never_resubmits_after_an_ambiguous_failure() -> None:
    import asyncio

    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection reset")

    provider = GoogleVeoProvider(
        project="p", access_token=lambda: "token", transport=httpx.MockTransport(handler)
    )

    async def drive() -> None:
        try:
            await provider.submit(_veo_request(), VeoInputImages())
        finally:
            await provider.aclose()

    with pytest.raises(VeoSubmissionAmbiguous):
        asyncio.run(drive())


def test_the_veo_adapter_classifies_rate_limits_and_timeouts() -> None:
    import asyncio

    for status, error in ((429, VeoRateLimited), (504, VeoOperationTimeout)):
        provider = GoogleVeoProvider(
            project="p",
            access_token=lambda: "token",
            transport=httpx.MockTransport(lambda _r, s=status: httpx.Response(s, json={})),
        )

        async def drive(current: GoogleVeoProvider = provider) -> None:
            try:
                await current.poll("operations/op-1")
            finally:
                await current.aclose()

        with pytest.raises(error):
            asyncio.run(drive())


def test_the_fake_veo_provider_needs_no_credentials_and_makes_no_call() -> None:
    import asyncio

    provider = FakeVeoProvider(output_width=320, output_height=180)

    async def drive() -> tuple[str, Any]:
        name = await provider.submit(_veo_request(), VeoInputImages())
        # A repeated submission of the same identity resumes rather than paying.
        again = await provider.submit(_veo_request(), VeoInputImages())
        assert name == again
        return name, await provider.poll(name)

    name, result = asyncio.run(drive())
    assert provider.submissions == 1
    assert result.state is VeoOperationState.SUCCEEDED
    assert name.endswith(result.operation_name.rsplit("/", 1)[-1])
    asyncio.run(provider.aclose())


# --- deterministic parallax fallback -----------------------------------------
def _still(path: Path) -> Path:
    image = Image.new("RGB", (320, 180), (40, 120, 150))
    draw = ImageDraw.Draw(image)
    draw.ellipse([90, 40, 230, 150], fill=(220, 180, 150))
    draw.rectangle([0, 156, 320, 180], fill=(30, 90, 120))
    image.save(path, format="PNG")
    return path


def _parallax(**overrides: Any) -> ParallaxRequest:
    values: dict[str, Any] = {
        "repair_attempt_id": UUID(int=1),
        "shot_id": UUID(int=2),
        "canonical_shot_hash": "a" * 64,
        "source": ParallaxSource(asset_id=UUID(int=3), sha256="b" * 64),
        "width": 320,
        "height": 180,
        "frame_rate": 24,
        "exact_duration_us": 3_000_000,
        "required_action": "The lead reacts to the news",
    }
    values.update(overrides)
    return ParallaxRequest(**values)


def test_parallax_parameters_are_deterministic_and_identity_derived() -> None:
    first, second = build_plan(_parallax()), build_plan(_parallax())
    assert first.model_dump() == second.model_dump()
    assert first.render_identity == render_identity(_parallax())
    # A different shot yields a different, still deterministic, camera move.
    other = build_plan(_parallax(canonical_shot_hash="c" * 64))
    assert other.render_identity != first.render_identity
    assert other.layers[0].end_scale != first.layers[0].end_scale


def test_a_fallback_render_produces_the_exact_canonical_duration(tmp_path: Path) -> None:
    plan = build_plan(_parallax())
    inputs = ParallaxInputs(
        layer_paths=(_still(tmp_path / "still.png"),),
        asset_ids=(UUID(int=3),),
        asset_hashes=("b" * 64,),
    )
    rendered = render_parallax(plan, inputs, workspace=tmp_path / "ws")
    assert rendered.measured_duration_us == 3_000_000
    assert rendered.pixel_format == "yuv420p"
    assert rendered.video_codec == "h264"
    # The same plan renders byte-for-byte the same clip.
    again = render_parallax(plan, inputs, workspace=tmp_path / "ws2")
    assert again.output_sha256 == rendered.output_sha256
    # FFmpeg is invoked through argument arrays, never a shell command string.
    assert rendered.manifest.ffmpeg_arguments[0] == "ffmpeg"
    assert "<output>" in rendered.manifest.ffmpeg_arguments
    assert frame_count(3_000_000, 24) == 72


def test_a_fallback_is_ineligible_for_mandatory_complex_action() -> None:
    eligibility = decide_eligibility(
        _parallax(required_action="The lead throws a mug and runs out of frame")
    )
    assert not eligibility.eligible
    assert any("physical action" in reason for reason in eligibility.reasons)


def test_a_fallback_is_rejected_when_the_source_keyframe_hard_failed() -> None:
    eligibility = decide_eligibility(_parallax(keyframe_hard_failure=True))
    assert not eligibility.eligible
    assert any("conceal an invalid source image" in reason for reason in eligibility.reasons)


def test_a_fallback_is_rejected_when_t20_condemned_the_keyframe() -> None:
    eligibility = decide_eligibility(
        _parallax(keyframe_repair_codes=(VisualQARepairCode.WRONG_CHARACTER_IDENTITY,))
    )
    assert not eligibility.eligible


def test_a_conveyable_shot_is_eligible() -> None:
    assert decide_eligibility(_parallax()).eligible


def test_the_veo_pricing_catalog_is_a_frozen_t23_projection() -> None:
    catalog = veo_pricing_catalog()
    rate = next(item for item in catalog.rates if item.model == VEO_31_FAST.model)
    assert rate.currency == "USD"
    assert rate.unit_price > Decimal("0")
    assert rate.source_reference.startswith("https://")
