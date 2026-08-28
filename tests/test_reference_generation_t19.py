from __future__ import annotations

import asyncio
from pathlib import Path

from sqlalchemy import func, select

from packages.providers.image_generation import DeterministicFakeImageProvider
from services.continuity.reference_generator import ProviderReferenceGenerator
from tests.storyboard_fixtures import build_fixture
from vidgen.contracts.continuity import ReferenceGenerationRequest
from vidgen.db.cost_models import ProviderAttempt
from vidgen.db.models import Asset


def test_reference_provider_reuses_t14_validation_assets_and_t23_attempt(tmp_path: Path) -> None:
    fixture = build_fixture(tmp_path)
    source = fixture.session.scalar(
        select(Asset).where(Asset.project_id == fixture.project.id).limit(1)
    )
    assert source is not None
    provider = DeterministicFakeImageProvider()
    pipeline = ProviderReferenceGenerator(fixture.session, fixture.blobs, provider)
    request = ReferenceGenerationRequest(
        project_id=fixture.project.id,
        identity_version_id=fixture.character_ids[0],
        entity_kind="character",
        ordered_source_asset_ids=[source.id],
        provider="fake",
        model="fake-v1",
        idempotency_key="reference-sheet-1",
    )
    first = asyncio.run(
        pipeline.generate(
            request,
            source_hashes=[source.sha256],
            source_bytes=(b"bounded-source",),
            prompt="Evidence-bounded neutral character reference sheet; do not invent details.",
        )
    )
    second = asyncio.run(
        pipeline.generate(
            request,
            source_hashes=[source.sha256],
            source_bytes=(b"bounded-source",),
            prompt="Evidence-bounded neutral character reference sheet; do not invent details.",
        )
    )
    assert first.asset_id == second.asset_id and second.reused
    assert provider.call_count == 1
    assert (
        fixture.session.scalar(
            select(func.count())
            .select_from(ProviderAttempt)
            .where(ProviderAttempt.operation == "continuity_reference_generation")
        )
        == 1
    )
    stored = fixture.session.get(Asset, first.asset_id)
    assert stored is not None
    assert stored.kind == "character_reference_sheet"
    assert stored.extra_metadata["approval_status"] == "draft"
    assert stored.parents[0].id == source.id
