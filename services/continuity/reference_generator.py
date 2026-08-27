"""Reference generation through the existing provider-neutral T14 request boundary."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from uuid import UUID, uuid5

from services.continuity.identity import canonical_hash
from vidgen.contracts.continuity import (
    ReferenceGenerationRequest,
    ReferenceGenerationResult,
    ReferenceValidationReport,
)

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
