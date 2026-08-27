"""Validated immutable image persistence through AssetService."""

from __future__ import annotations

from uuid import UUID

from services.image_generation.validation import ValidatedImage
from vidgen.contracts.image_generation import (
    ImagePromptPackage,
    ImageProviderRequest,
    ImageProviderResult,
)
from vidgen.storage.asset_service import AssetService, StoredAsset


def store_keyframe(
    assets: AssetService,
    *,
    project_id: UUID,
    validated: ValidatedImage,
    package: ImagePromptPackage,
    request: ImageProviderRequest,
    result: ImageProviderResult,
    parent_asset_ids: tuple[UUID, ...],
    lineage: dict[str, object],
) -> StoredAsset:
    report = validated.report
    return assets.store(
        content=validated.content,
        kind="generated_keyframe",
        media_type=report.mime_type or "image/png",
        project_id=project_id,
        parent_asset_ids=parent_asset_ids,
        provider=result.provider,
        provider_request_id=result.provider_request_id,
        idempotency_key=f"keyframe:{request.application_idempotency_key}",
        generation_parameters={
            "model": result.model,
            "model_snapshot": result.model_snapshot,
            "quality": request.quality.value,
            "requested_width": request.width,
            "requested_height": request.height,
            "output_format": request.output_format.value,
            "background": request.background,
            "provider_configuration_version": request.provider_configuration_version,
            "prompt_compiler_version": package.prompt_compiler_version,
            "prompt_template_version": package.template_version,
            "prompt_hash": package.prompt_hash,
            "input_hash": package.input_hash,
        },
        metadata={
            **lineage,
            "shot_id": str(request.shot_id),
            "shot_sequence": request.shot_sequence,
            "keyframe_role": request.keyframe_role.value,
            "reference_asset_ids": [str(item.asset_id) for item in package.references],
            "reference_hashes": [item.sha256 for item in package.references],
            "actual_width": report.width,
            "actual_height": report.height,
            "byte_size": report.byte_size,
            "sha256": report.sha256,
            "validation": report.model_dump(mode="json"),
        },
    )
