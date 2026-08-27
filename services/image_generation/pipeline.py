"""Restartable T14 orchestration over authoritative selected T13 shots."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from opentelemetry import trace
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from services.image_generation.artifact_writer import store_keyframe
from services.image_generation.openai_image import UnknownProviderOutcome
from services.image_generation.prompt_compiler import COMPILER_VERSION, compile_prompt
from services.image_generation.providers import (
    GPT_IMAGE_SNAPSHOT,
    ImageGenerationProvider,
    validate_dimensions,
)
from services.image_generation.references import resolve_references
from services.image_generation.validation import validate_base64_image
from vidgen.contracts.costs import BudgetDecision, CostReservationRequest
from vidgen.contracts.image_generation import (
    GeneratedImageCandidate,
    ImageGenerationResult,
    ImagePromptPackage,
    ImageProviderRequest,
    ImageQuality,
    ImageReferenceBinding,
    ImageValidationReport,
    KeyframeRole,
    ShotKeyframeResult,
    VisualIntent,
)
from vidgen.contracts.storyboard import StoryboardShot
from vidgen.db.cost_models import ProjectBudget
from vidgen.db.cost_repository import BudgetExceededError, CostRepository
from vidgen.db.episode_analysis_models import EpisodeAnalysisRecord
from vidgen.db.image_generation_models import (
    GeneratedKeyframeImage,
    ImageGenerationItem,
    ImageGenerationRun,
)
from vidgen.db.image_generation_repository import ImageGenerationRepository, SelectedStoryboard
from vidgen.db.models import Asset, Character, Location, Project
from vidgen.storage.asset_service import AssetService
from vidgen.storage.blob import BlobStore
from vidgen.telemetry.failures import classify_failure
from vidgen.telemetry.metrics import Metrics
from vidgen.telemetry.provider import instrument_provider_attempt

PIPELINE_VERSION = "image-generation/1.0.0"
VALIDATION_VERSION = "technical-image/1.0"


class ImageGenerationCancelled(RuntimeError):
    pass


class ProviderResponseRequiresReview(RuntimeError):
    """A paid request completed but local processing did not finish safely."""


def _hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


class ImageGenerationPipeline:
    def __init__(
        self,
        session: Session,
        blob_store: BlobStore,
        provider: ImageGenerationProvider,
        *,
        model: str = GPT_IMAGE_SNAPSHOT,
        width: int = 1536,
        height: int = 864,
        quality: str = "medium",
        provider_configuration_version: str = "openai-image/1",
        cancellation_check: Callable[[], bool] | None = None,
        metrics: Metrics | None = None,
    ) -> None:
        validate_dimensions(width, height)
        self.session = session
        self.blob_store = blob_store
        self.provider = provider
        self.model = model
        self.width = width
        self.height = height
        self.quality = quality
        self.provider_configuration_version = provider_configuration_version
        self.cancelled = cancellation_check or (lambda: False)
        self.metrics = metrics or Metrics()
        self.tracer = trace.NoOpTracerProvider().get_tracer("vidgen.image_generation")
        self.repo = ImageGenerationRepository(session)
        self.assets = AssetService(session, blob_store)
        self.costs = CostRepository(session)

    async def process(
        self,
        *,
        project_id: UUID,
        idempotency_key: str,
        storyboard_id: UUID | None = None,
        shot_id: UUID | None = None,
        role: KeyframeRole | None = None,
    ) -> ImageGenerationResult:
        selected = self.repo.selected_storyboard(project_id, storyboard_id)
        material = {
            "project_id": str(project_id),
            "storyboard_id": str(selected.storyboard.id),
            "storyboard_version": selected.storyboard.version,
            "storyboard_hash": selected.storyboard_asset.sha256,
            "timing_hash": selected.timing_asset.sha256,
            "provider": self.provider.name,
            "model": self.model,
            "width": self.width,
            "height": self.height,
            "quality": self.quality,
            "provider_configuration_version": self.provider_configuration_version,
            "pipeline_version": PIPELINE_VERSION,
            "shot_id": str(shot_id) if shot_id is not None else None,
            "role": role.value if role is not None else None,
        }
        input_hash = _hash(material)
        run = self.repo.run_by_key(project_id, idempotency_key)
        if run is not None and run.input_hash != input_hash:
            raise ValueError("idempotency key already binds different material inputs")
        if run is None:
            run = ImageGenerationRun(
                project_id=project_id,
                storyboard_id=selected.storyboard.id,
                storyboard_version=selected.storyboard.version,
                idempotency_key=idempotency_key,
                input_hash=input_hash,
                status="keyframes_queued",
                provider_configuration_version=self.provider_configuration_version,
                prompt_compiler_version=COMPILER_VERSION,
                pipeline_version=PIPELINE_VERSION,
                parameters=material,
            )
            self.session.add(run)
            self.session.flush()
            self.session.commit()
        targets = self._targets(selected, shot_id, role)
        run.requested_item_count = len(targets)
        selected.project.status = run.status = "keyframes_compiling"
        self.session.commit()
        results: list[ShotKeyframeResult] = []
        try:
            for shot, keyframe_role in targets:
                if self.cancelled():
                    raise ImageGenerationCancelled("T14 cancellation requested at item checkpoint")
                results.append(await self._process_item(selected, run, shot, keyframe_role))
            run.completed_item_count = sum(
                item.status in {"completed", "reused"} for item in results
            )
            run.failed_item_count = sum(item.status == "failed" for item in results)
            complete = (
                run.completed_item_count == run.requested_item_count and not run.failed_item_count
            )
            run.status = "keyframes_complete" if complete else "keyframes_failed"
            selected.project.status = run.status
            self.session.commit()
        except BaseException as exc:
            self.session.rollback()
            durable = self.session.get(ImageGenerationRun, run.id)
            project = self.session.get(Project, project_id)
            if durable is not None:
                durable.status = "keyframes_failed"
                durable.error_code = type(exc).__name__[:128]
            if project is not None:
                project.status = "keyframes_failed"
            self.session.commit()
            raise
        return ImageGenerationResult(
            run_id=run.id,
            storyboard_id=selected.storyboard.id,
            storyboard_version=selected.storyboard.version,
            requested_count=run.requested_item_count,
            completed_count=sum(item.status == "completed" for item in results),
            reused_count=sum(item.status == "reused" for item in results),
            failed_count=run.failed_item_count,
            status=run.status,
            items=results,
        )

    def _targets(
        self, selected: SelectedStoryboard, shot_id: UUID | None, role: KeyframeRole | None
    ) -> list[tuple[Any, KeyframeRole]]:
        result: list[tuple[Any, KeyframeRole]] = []
        for shot in selected.shots:
            if shot_id is not None and shot.id != shot_id and shot.stable_shot_id != shot_id:
                continue
            roles = [KeyframeRole.FIRST_FRAME]
            contract = StoryboardShot.model_validate(shot.contract)
            if contract.requires_last_frame:
                roles.append(KeyframeRole.LAST_FRAME)
            for item_role in roles:
                if role is None or role == item_role:
                    result.append((shot, item_role))
        if shot_id is not None and not result:
            raise ValueError("requested shot or explicitly required keyframe role is not eligible")
        return result

    async def _process_item(
        self, selected: SelectedStoryboard, run: ImageGenerationRun, row: Any, role: KeyframeRole
    ) -> ShotKeyframeResult:
        shot = StoryboardShot.model_validate(row.contract)
        package = self._package(selected, shot, role)
        identity = _hash(
            {
                "project": selected.project.id,
                "storyboard": run.storyboard_id,
                "storyboard_version": run.storyboard_version,
                "storyboard_hash": selected.storyboard_asset.sha256,
                "timing_hash": selected.timing_asset.sha256,
                "shot": row.stable_shot_id,
                "sequence": row.global_sequence,
                "role": role.value,
                "shot_hash": _hash(row.contract),
                "visual_style": _hash(selected.project.visual_style),
                "prompt_hash": package.prompt_hash,
                "references": [(str(ref.asset_id), ref.sha256) for ref in package.references],
                "model": self.model,
                "quality": self.quality,
                "width": self.width,
                "height": self.height,
                "format": "png",
                "background": "opaque",
                "provider_config": self.provider_configuration_version,
                "compiler": COMPILER_VERSION,
                "pipeline": PIPELINE_VERSION,
                "validation": VALIDATION_VERSION,
            }
        )
        item = self.repo.item_by_identity(identity)
        if item is not None:
            generated = self.repo.generated(item.id)
            if item.status == "completed" and generated is not None:
                return self._item_result(item, generated, "reused")
            if item.status == "provider_outcome_unknown":
                raise UnknownProviderOutcome(
                    "provider outcome is unknown; manual reconciliation is required before retry"
                )
            if item.status == "provider_response_received":
                raise ProviderResponseRequiresReview(
                    "provider response was received; manual recovery is required before retry"
                )
            if item.run_id != run.id:
                raise ValueError("generation identity belongs to an incompatible run")
        else:
            item = ImageGenerationItem(
                run_id=run.id,
                shot_id=row.id,
                shot_sequence=row.global_sequence,
                keyframe_role=role.value,
                generation_identity=identity,
                input_hash=package.input_hash,
                prompt_package=package.model_dump(mode="json"),
                status="compiled",
            )
            self.session.add(item)
            self.session.flush()
            self.session.commit()
        resolved = resolve_references(
            self.session,
            self.blob_store,
            project_id=selected.project.id,
            bindings=package.references,
        )
        request = ImageProviderRequest(
            application_idempotency_key=identity,
            project_id=selected.project.id,
            image_generation_run_id=run.id,
            storyboard_id=run.storyboard_id,
            storyboard_version=run.storyboard_version,
            shot_id=package.visual_intent.shot_id,
            shot_sequence=row.global_sequence,
            keyframe_role=role,
            compiled_prompt=package.prompt,
            references=list(resolved.bindings),
            model=self.model,
            width=self.width,
            height=self.height,
            quality=ImageQuality(self.quality),
            attempt_number=item.attempt_count + 1,
            provider_configuration_version=self.provider_configuration_version,
        )
        run.status = selected.project.status = "keyframes_generating"
        item.status = "provider_checkpointed"
        item.attempt_count += 1
        self.session.commit()
        estimated_cost = (
            {
                "low": Decimal("0.020000"),
                "medium": Decimal("0.040000"),
                "high": Decimal("0.080000"),
            }[self.quality]
            if self.provider.name != "fake"
            else Decimal("0")
        )
        async with instrument_provider_attempt(
            session=self.session,
            tracer=self.tracer,
            metrics=self.metrics,
            project_id=selected.project.id,
            provider=self.provider.name,
            model=self.model,
            operation="image_generation",
            input_hash=package.input_hash,
            idempotency_key=identity,
            related_entity_id=item.id,
            attempt_number=item.attempt_count,
            estimated_cost=estimated_cost,
        ) as attempt:
            reservation_id = None
            has_budget = self.session.scalar(
                select(ProjectBudget.id).where(ProjectBudget.project_id == selected.project.id)
            )
            if self.provider.name != "fake" and has_budget is not None:
                reservation = self.costs.reserve(
                    CostReservationRequest(
                        project_id=selected.project.id,
                        provider_attempt_id=attempt.row.id,
                        idempotency_key=f"{identity}:reservation",
                        estimated_amount=estimated_cost,
                        currency="USD",
                    )
                )
                if reservation.decision in {
                    BudgetDecision.DENY_ENTITY_CAP,
                    BudgetDecision.DENY_HARD_CAP,
                    BudgetDecision.UNKNOWN_PRICE_REVIEW,
                }:
                    raise BudgetExceededError(f"image generation denied: {reservation.decision}")
                reservation_id = reservation.reservation_id
                self.session.commit()
            try:
                response = await self.provider.generate(request, resolved.contents)
            except BaseException as exc:
                if isinstance(exc, UnknownProviderOutcome):
                    # This terminal checkpoint is committed before propagating the
                    # non-retryable error. A workflow replay or manual resume can
                    # therefore never submit the same paid request again.
                    item.status = "provider_outcome_unknown"
                    item.error_code = "PROVIDER_OUTCOME_UNKNOWN"
                    self.session.commit()
                if reservation_id is not None and not isinstance(exc, UnknownProviderOutcome):
                    self.costs.reconcile(
                        reservation_id,
                        f"{identity}:reconciliation",
                        Decimal("0"),
                        billable=False,
                    )
                # The context manager will apply the same classification while
                # unwinding, but persist it here with reconciliation before the
                # outer process rollback can discard the failed attempt.
                failure = classify_failure(exc)
                attempt.row.status = "FAILED"
                attempt.row.failure_class = failure.failure_class
                attempt.row.error_code = failure.error_code
                attempt.row.retryable = failure.retryable
                self.session.commit()
                raise
            # Persist the known provider outcome before validation, decoding,
            # blob writes, or relational projection can fail. Since image bytes
            # are intentionally forbidden in database JSON, an interrupted item
            # is terminal/manual-review rather than eligible for another paid call.
            item.status = "provider_response_received"
            item.provider_result = response.model_dump(mode="json")
            self.session.commit()
            attempt.set_result(
                provider_request_id=response.provider_request_id,
                usage=[dict(response.usage)] if response.usage else [],
                metadata=dict(response.response_metadata),
            )
            self.session.flush()
            attempt_id = attempt.row.id
            if reservation_id is not None:
                # Until usage-specific image pricing is available, T23's configured
                # estimate is the reconciliation amount. The ledger remains exact
                # and idempotent rather than inventing token quantities.
                self.costs.reconcile(
                    reservation_id,
                    f"{identity}:reconciliation",
                    estimated_cost,
                )
        # Make the known provider attempt and any reconciliation durable before
        # entering fallible local image processing.
        self.session.commit()
        run.status = selected.project.status = "keyframes_persisting"
        validated = validate_base64_image(
            response.image_base64,
            expected_format=response.output_format,
            width=self.width,
            height=self.height,
        )
        if not validated.report.valid:
            raise ValueError("generated image failed deterministic integrity validation")
        parent_ids = (
            selected.storyboard_asset.id,
            selected.timing_asset.id,
            *(ref.asset_id for ref in resolved.bindings),
        )
        stored = store_keyframe(
            self.assets,
            project_id=selected.project.id,
            validated=validated,
            package=package,
            request=request,
            result=response,
            parent_asset_ids=parent_ids,
            lineage={
                "episode_model_id": str(selected.storyboard.episode_model_id),
                "script_id": str(selected.storyboard.script_id),
                "narration_run_id": str(selected.storyboard.narration_run_id),
                "storyboard_id": str(selected.storyboard.id),
                "timing_manifest_asset_id": str(selected.timing_asset.id),
                "pipeline_version": PIPELINE_VERSION,
                "validation_version": VALIDATION_VERSION,
            },
        )
        generated = GeneratedKeyframeImage(
            project_id=selected.project.id,
            shot_id=row.id,
            keyframe_role=role.value,
            item_id=item.id,
            provider_attempt_id=attempt_id,
            asset_id=stored.id,
            provider=response.provider,
            model=response.model_snapshot or response.model,
            prompt_hash=package.prompt_hash,
            reference_hash=_hash([(str(ref.asset_id), ref.sha256) for ref in resolved.bindings]),
            width=validated.report.width,
            height=validated.report.height,
            mime_type=validated.report.mime_type,
            byte_size=validated.report.byte_size,
            sha256=validated.report.sha256,
            validation_report=validated.report.model_dump(mode="json"),
            selected=True,
        )
        # A material configuration change creates a new immutable candidate. It
        # replaces the selection atomically without overwriting the old asset.
        self.session.execute(
            update(GeneratedKeyframeImage)
            .where(
                GeneratedKeyframeImage.shot_id == row.id,
                GeneratedKeyframeImage.keyframe_role == role.value,
                GeneratedKeyframeImage.selected,
            )
            .values(selected=False)
        )
        self.session.add(generated)
        self.session.flush()
        item.selected_generated_image_id = generated.id
        item.status = "completed"
        self.session.commit()
        return self._item_result(item, generated, "completed")

    def _package(
        self, selected: SelectedStoryboard, shot: StoryboardShot, role: KeyframeRole
    ) -> ImagePromptPackage:
        continuity = (
            shot.incoming_continuity
            if role == KeyframeRole.FIRST_FRAME
            else shot.expected_outgoing_continuity
        )
        pose_key = "start_pose" if role == KeyframeRole.FIRST_FRAME else "expected_end_pose"
        pose = str(
            shot.provenance.get(pose_key) or shot.action.staging_note or shot.action.subject_action
        )
        identity_ids = list(continuity.present_character_ids)
        for prop in continuity.props:
            if prop.owner_character_id is not None and prop.owner_character_id not in identity_ids:
                identity_ids.append(prop.owner_character_id)
        identity_descriptions = self._character_descriptions(selected, identity_ids)
        character_labels = dict(zip(identity_ids, identity_descriptions, strict=True))
        descriptions = [character_labels[value] for value in continuity.present_character_ids]
        states = [
            f"{character_labels[state.character_id]}: wardrobe {state.wardrobe_state}, "
            f"injury {state.injury_state}, emotion {state.emotional_state}"
            for state in continuity.character_appearance_states
        ]
        props = []
        for prop in continuity.props:
            owner = (
                character_labels[prop.owner_character_id]
                if prop.owner_character_id is not None
                else "unassigned"
            )
            props.append(f"{prop.prop_id} owned by {owner}: {prop.note}")
        intent = VisualIntent(
            shot_id=shot.shot_id,
            shot_sequence=shot.global_sequence,
            keyframe_role=role,
            visual_purpose=shot.visual_objective,
            style_lock=selected.project.visual_style,
            visible_character_count=len(continuity.present_character_ids),
            character_descriptions=descriptions,
            character_states=states,
            location_description=self._location_description(selected, continuity.location_id),
            location_invariants=[continuity.sub_location, *continuity.environment_conditions]
            if continuity.sub_location
            else list(continuity.environment_conditions),
            props_and_ownership=props,
            composition=shot.action.staging_note or shot.visual_objective,
            shot_size=shot.camera.framing,
            camera_angle=shot.camera.angle,
            subject_priority=descriptions,
            pose=pose,
            primary_action=shot.action.subject_action,
            emotional_state=continuity.emotional_state,
            continuity_assumptions=[
                f"time of day {continuity.time_of_day}",
                f"screen direction {continuity.screen_direction}",
            ],
            required_source_evidence=[ref.reference_id for ref in shot.evidence_references],
            positive_constraints=[shot.camera.lens_note] if shot.camera.lens_note else [],
            negative_constraints=["no unnamed characters", "no contradictory camera framing"],
        )
        refs = [
            ImageReferenceBinding.model_validate(value)
            for value in shot.provenance.get("image_reference_bindings", [])
        ]
        return compile_prompt(intent, refs)

    @staticmethod
    def _item_result(
        item: ImageGenerationItem,
        generated: GeneratedKeyframeImage,
        status: Literal["completed", "reused", "failed"],
    ) -> ShotKeyframeResult:
        report = ImageValidationReport.model_validate(generated.validation_report)
        package = ImagePromptPackage.model_validate(item.prompt_package)
        canonical_shot_id = package.visual_intent.shot_id
        return ShotKeyframeResult(
            shot_id=canonical_shot_id,
            keyframe_role=KeyframeRole(item.keyframe_role),
            status=status,
            prompt_hash=generated.prompt_hash,
            candidate=GeneratedImageCandidate(
                generated_image_id=generated.id,
                asset_id=generated.asset_id,
                shot_id=canonical_shot_id,
                keyframe_role=KeyframeRole(item.keyframe_role),
                selected=generated.selected,
                validation=report,
            ),
        )

    def _character_descriptions(
        self, selected: SelectedStoryboard, character_ids: list[UUID]
    ) -> list[str]:
        """Resolve project-owned T10 identities into deterministic prompt text."""
        rows = {
            row.id: row
            for row in self.session.scalars(
                select(Character).where(
                    Character.project_id == selected.project.id,
                    Character.id.in_(character_ids),
                )
            )
        }
        fallback: dict[UUID, dict[str, object]] = {}
        if len(rows) != len(character_ids):
            episode = self.session.get(EpisodeAnalysisRecord, selected.storyboard.episode_model_id)
            asset = (
                self.session.get(Asset, episode.canonical_analysis_asset_id) if episode else None
            )
            if asset is not None and asset.project_id == selected.project.id:
                payload = json.loads(self.blob_store.read(asset.storage_key))
                fallback = {
                    UUID(value["character_id"]): value for value in payload.get("characters", [])
                }
        descriptions: list[str] = []
        for character_id in character_ids:
            row = rows.get(character_id)
            definition = row.definition if row is not None else fallback.get(character_id)
            if definition is None:
                raise ValueError(f"project character {character_id} has no selected T10 definition")
            name = row.canonical_name if row is not None else str(definition["canonical_name"])
            aliases_value = definition.get("aliases")
            aliases = (
                [str(value) for value in aliases_value] if isinstance(aliases_value, list) else []
            )
            description = f"{name}"
            if aliases:
                description += f" (also known as {', '.join(aliases)})"
            if bool(definition.get("anonymous")):
                description += ", anonymous character"
            descriptions.append(description)
        return descriptions

    def _location_description(self, selected: SelectedStoryboard, location_id: UUID | None) -> str:
        if location_id is None:
            return "unspecified project location"
        row = self.session.get(Location, location_id)
        definition: dict[str, object] | None = None
        name: str | None = None
        if row is not None:
            if row.project_id != selected.project.id:
                raise ValueError(f"location {location_id} belongs to another project")
            name = row.canonical_name
            definition = row.definition
        else:
            episode = self.session.get(EpisodeAnalysisRecord, selected.storyboard.episode_model_id)
            asset = (
                self.session.get(Asset, episode.canonical_analysis_asset_id) if episode else None
            )
            if asset is not None and asset.project_id == selected.project.id:
                payload = json.loads(self.blob_store.read(asset.storage_key))
                for value in payload.get("locations", []):
                    if UUID(value["location_id"]) == location_id:
                        definition = value
                        name = str(value["canonical_name"])
                        break
        if definition is None or name is None:
            raise ValueError(f"project location {location_id} has no selected T10 definition")
        aliases_value = definition.get("aliases")
        aliases = [str(value) for value in aliases_value] if isinstance(aliases_value, list) else []
        return f"{name} (also known as {', '.join(aliases)})" if aliases else name
