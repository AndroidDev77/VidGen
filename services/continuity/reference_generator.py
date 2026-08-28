"""Reference generation through the existing provider-neutral T14 request boundary."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID, uuid5

from opentelemetry import trace
from sqlalchemy import select
from sqlalchemy.orm import Session

from services.continuity.identity import canonical_hash
from services.image_generation.providers import ImageGenerationProvider
from services.image_generation.validation import validate_base64_image
from vidgen.contracts.continuity import (
    ReferenceGenerationRequest,
    ReferenceGenerationResult,
    ReferenceValidationReport,
)
from vidgen.contracts.costs import BudgetDecision, CostReservationRequest
from vidgen.contracts.image_generation import (
    ImageFormat,
    ImageProviderRequest,
    ImageQuality,
    KeyframeRole,
)
from vidgen.db.cost_models import ProjectBudget
from vidgen.db.cost_repository import BudgetExceededError, CostRepository
from vidgen.storage.asset_service import AssetService
from vidgen.storage.blob import BlobStore
from vidgen.telemetry.metrics import Metrics
from vidgen.telemetry.provider import instrument_provider_attempt

FAKE_NAMESPACE = UUID("b9e6406a-28a4-4995-a1e9-588126d36fb8")


def reference_identity(request: ReferenceGenerationRequest, source_hashes: list[str]) -> str:
    return canonical_hash(
        {
            "request": request.model_dump(mode="json", exclude={"idempotency_key"}),
            "ordered_source_hashes": source_hashes,
            "prompt_compiler_version": "image-prompt/1.0",
            "contract_version": "1.0",
            "pipeline_version": "continuity-reference/1.0",
        }
    )


@dataclass
class DeterministicFakeReferenceGenerator:
    """Free fake used by tests/CI; a repeated identity returns the same result."""

    completed: dict[str, ReferenceGenerationResult]

    def generate(
        self, request: ReferenceGenerationRequest, source_hashes: list[str]
    ) -> ReferenceGenerationResult:
        identity = reference_identity(request, source_hashes)
        if identity in self.completed:
            return self.completed[identity].model_copy(update={"reused": True})
        asset_id = uuid5(FAKE_NAMESPACE, f"asset:{identity}")
        payload_hash = hashlib.sha256(f"fake-reference:{identity}".encode()).hexdigest()
        result = ReferenceGenerationResult(
            reference_identity=identity,
            asset_id=asset_id,
            provider_attempt_id=uuid5(FAKE_NAMESPACE, f"attempt:{identity}"),
            provider_request_id=f"fake-{identity[:16]}",
            reused=False,
            validation=ReferenceValidationReport(
                valid=True, sha256=payload_hash, width=1024, height=1024, media_type="image/png"
            ),
        )
        self.completed[identity] = result
        return result


class ProviderReferenceGenerator:
    """T19 adapter over T14's provider, validation, AssetService, and T23 accounting."""

    def __init__(self, session: Session, blob_store: BlobStore, provider: ImageGenerationProvider):
        self.session = session
        self.provider = provider
        self.assets = AssetService(session, blob_store)
        self.costs = CostRepository(session)
        self.metrics = Metrics()
        self.tracer = trace.NoOpTracerProvider().get_tracer("vidgen.continuity.references")

    async def generate(
        self,
        request: ReferenceGenerationRequest,
        *,
        source_hashes: list[str],
        source_bytes: tuple[bytes, ...],
        prompt: str,
        width: int = 1024,
        height: int = 1024,
        estimated_cost: Decimal = Decimal("0.040000"),
    ) -> ReferenceGenerationResult:
        if request.provider != self.provider.name:
            raise ValueError("configured provider does not match reference request identity")
        if not (len(request.ordered_source_asset_ids) == len(source_hashes) == len(source_bytes)):
            raise ValueError("ordered source IDs, hashes, and bytes must have identical lengths")
        identity = reference_identity(request, source_hashes)
        asset_key = f"continuity-reference:{identity}"
        existing = self.assets.assets.get_by_idempotency(request.project_id, asset_key)
        if existing is not None:
            return ReferenceGenerationResult(
                reference_identity=identity,
                asset_id=existing.id,
                provider_request_id=existing.provider_request_id,
                reused=True,
                validation=ReferenceValidationReport(
                    valid=True,
                    sha256=existing.sha256,
                    width=width,
                    height=height,
                    media_type=existing.media_type,
                ),
            )
        pseudo_run = uuid5(FAKE_NAMESPACE, f"run:{identity}")
        provider_request = ImageProviderRequest(
            application_idempotency_key=identity,
            project_id=request.project_id,
            image_generation_run_id=pseudo_run,
            storyboard_id=request.identity_version_id,
            storyboard_version=1,
            shot_id=request.identity_version_id,
            shot_sequence=0,
            keyframe_role=KeyframeRole.FIRST_FRAME,
            compiled_prompt=prompt,
            model=request.model,
            width=width,
            height=height,
            quality=ImageQuality.MEDIUM,
            output_format=ImageFormat.PNG,
            attempt_number=1,
            provider_configuration_version="continuity-reference/1",
        )
        async with instrument_provider_attempt(
            session=self.session,
            tracer=self.tracer,
            metrics=self.metrics,
            project_id=request.project_id,
            provider=self.provider.name,
            model=request.model,
            operation="continuity_reference_generation",
            input_hash=identity,
            idempotency_key=identity,
            related_entity_id=request.identity_version_id,
            estimated_cost=estimated_cost if self.provider.name != "fake" else Decimal("0"),
        ) as attempt:
            reservation_id = None
            budget = self.session.scalar(
                select(ProjectBudget.id).where(ProjectBudget.project_id == request.project_id)
            )
            if self.provider.name != "fake" and budget is not None:
                reservation = self.costs.reserve(
                    CostReservationRequest(
                        project_id=request.project_id,
                        provider_attempt_id=attempt.row.id,
                        idempotency_key=f"{identity}:reservation",
                        estimated_amount=estimated_cost,
                        currency="USD",
                    )
                )
                if reservation.decision not in {
                    BudgetDecision.ALLOW,
                    BudgetDecision.ALLOW_WITH_WARNING,
                }:
                    raise BudgetExceededError(
                        f"reference generation denied: {reservation.decision}"
                    )
                reservation_id = reservation.reservation_id
                self.session.commit()
            try:
                result = await self.provider.generate(provider_request, source_bytes)
            except BaseException:
                if reservation_id is not None:
                    self.costs.reconcile(
                        reservation_id,
                        f"{identity}:reconciliation",
                        Decimal("0"),
                        billable=False,
                    )
                    self.session.commit()
                raise
            attempt.set_result(
                provider_request_id=result.provider_request_id,
                usage=[dict(result.usage)] if result.usage else [],
                metadata=dict(result.response_metadata),
                actual_cost=estimated_cost if self.provider.name != "fake" else Decimal("0"),
            )
            if reservation_id is not None:
                self.costs.reconcile(reservation_id, f"{identity}:reconciliation", estimated_cost)
        validated = validate_base64_image(
            result.image_base64, expected_format=result.output_format, width=width, height=height
        )
        if not validated.report.valid:
            raise ValueError("generated reference failed deterministic validation")
        stored = self.assets.store(
            content=validated.content,
            kind=f"{request.entity_kind}_reference_sheet",
            media_type=validated.report.mime_type or "image/png",
            project_id=request.project_id,
            parent_asset_ids=tuple(request.ordered_source_asset_ids),
            provider=result.provider,
            provider_request_id=result.provider_request_id,
            idempotency_key=asset_key,
            generation_parameters={
                "model": request.model,
                "reference_identity": identity,
                "source_hashes": source_hashes,
                **request.generation_parameters,
            },
            metadata={
                "identity_version_id": str(request.identity_version_id),
                "provider_attempt_id": str(attempt.row.id),
                "approval_status": "draft",
                "pipeline_version": "continuity-reference/1.0",
            },
        )
        self.session.commit()
        return ReferenceGenerationResult(
            reference_identity=identity,
            asset_id=stored.id,
            provider_attempt_id=attempt.row.id,
            provider_request_id=result.provider_request_id,
            validation=ReferenceValidationReport(
                valid=True,
                sha256=stored.sha256,
                width=validated.report.width,
                height=validated.report.height,
                media_type=stored.media_type,
            ),
        )
