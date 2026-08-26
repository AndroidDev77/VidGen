"""Deterministic, zero-cost provider used by tests and local verification."""

from __future__ import annotations

from uuid import UUID, uuid4, uuid5

from services.script.compressor import compress_plot
from services.script.diff import build_script_diff
from services.script.editor import propose_revision
from services.script.provider import GenerationContext
from services.script.rubric import approval_recommendation, score_script
from services.script.validator import build_beat_coverage
from services.script.writer import write_script
from vidgen.contracts.script import (
    ComedyEditRequest,
    ComedyEditResult,
    ComedyWritingRequest,
    PlotCompressionRequest,
    ProviderComedyEditResult,
    ProviderCompressedPlotResult,
    ProviderRecapScriptResult,
    ScriptProviderMetadata,
)

FAKE_NAMESPACE = UUID("6f0f6a3a-4a3b-4a49-9f1e-8b2c0b7a2b7a")


class FakeScriptGenerationProvider:
    """Runs the real deterministic algorithms in compressor/writer/editor/rubric."""

    provider = "fake"
    model = "deterministic-script-v1"
    configuration_version = "fake-script-v1"

    def __init__(self) -> None:
        self.submissions: list[str] = []

    def _metadata(
        self,
        request: PlotCompressionRequest | ComedyWritingRequest | ComedyEditRequest,
        context: GenerationContext,
        operation: str,
        rubric_version: str | None = None,
    ) -> ScriptProviderMetadata:
        self.submissions.append(request.idempotency_key)
        return ScriptProviderMetadata(
            provider=self.provider,
            model=self.model,
            provider_request_id=str(uuid5(FAKE_NAMESPACE, request.idempotency_key)),
            operation=operation,  # type: ignore[arg-type]
            attempt_number=context.attempt_number,
            input_hash=request.input_hash,
            prompt_version=request.prompt_version,
            contract_version=request.contract_version,
            rubric_version=rubric_version,
        )

    async def compress_plot(
        self, request: PlotCompressionRequest, context: GenerationContext
    ) -> ProviderCompressedPlotResult:
        plan_id = uuid5(FAKE_NAMESPACE, f"plan:{request.input_hash}:{request.idempotency_key}")
        plan = compress_plot(analysis=request.episode_analysis, request=request, plan_id=plan_id)
        return ProviderCompressedPlotResult(
            output=plan, metadata=self._metadata(request, context, "compress_plot")
        )

    async def write_script(
        self, request: ComedyWritingRequest, context: GenerationContext
    ) -> ProviderRecapScriptResult:
        script_id = uuid5(FAKE_NAMESPACE, f"script:{request.input_hash}:{request.idempotency_key}")
        script = write_script(plan=request.compressed_plot, request=request, script_id=script_id, version=1)
        return ProviderRecapScriptResult(
            output=script, metadata=self._metadata(request, context, "write_script")
        )

    async def edit_script(
        self, request: ComedyEditRequest, context: GenerationContext
    ) -> ProviderComedyEditResult:
        edits, revised = propose_revision(request.recap_script)
        coverage = build_beat_coverage(revised, request.compressed_plot)
        revised = revised.model_copy(update={"beat_coverage": coverage})
        mandatory_total = sum(1 for item in coverage if item.mandatory)
        mandatory_covered = sum(1 for item in coverage if item.mandatory and item.coverage == "covered")
        mandatory_ratio = 1.0 if mandatory_total == 0 else mandatory_covered / mandatory_total
        within_target = (
            revised.target_word_count == 0
            or abs(revised.actual_word_count - revised.target_word_count) / revised.target_word_count
            <= 0.05
        )
        # The fake editor only trims filler text, so it cannot itself break a
        # cross-reference; the pipeline re-runs the authoritative deterministic
        # validator against the real EpisodeAnalysis once this result comes back.
        error_count = 0 if mandatory_ratio >= 1.0 else mandatory_total - mandatory_covered
        scores = score_script(revised, validation_error_count=error_count)
        recommendation = approval_recommendation(
            scores,
            request.rubric,
            mandatory_coverage_ratio=mandatory_ratio,
            word_count_within_target=within_target,
            validation_valid=error_count == 0,
        )
        result = ComedyEditResult(
            scores=scores,
            issues=[],
            edits=edits,
            revised_script=revised,
            approval_recommendation=recommendation,
        )
        return ProviderComedyEditResult(
            output=result,
            metadata=self._metadata(request, context, "edit_script", request.rubric_version),
        )
