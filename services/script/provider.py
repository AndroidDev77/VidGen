"""Provider-neutral T11 compression/writing/editing port."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from vidgen.contracts.script import (
    ComedyEditRequest,
    ComedyWritingRequest,
    PlotCompressionRequest,
    ProviderComedyEditResult,
    ProviderCompressedPlotResult,
    ProviderRecapScriptResult,
)


@dataclass(frozen=True, slots=True)
class GenerationContext:
    attempt_number: int = 1
    validation_errors_json: str | None = None


class ScriptGenerationProvider(Protocol):
    async def compress_plot(
        self, request: PlotCompressionRequest, context: GenerationContext
    ) -> ProviderCompressedPlotResult: ...

    async def write_script(
        self, request: ComedyWritingRequest, context: GenerationContext
    ) -> ProviderRecapScriptResult: ...

    async def edit_script(
        self, request: ComedyEditRequest, context: GenerationContext
    ) -> ProviderComedyEditResult: ...
