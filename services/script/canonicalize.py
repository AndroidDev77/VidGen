"""Stable IDs, ordering, and content hashing for T11 canonical artifacts."""

from __future__ import annotations

import hashlib
import json
from typing import Any
from uuid import UUID, uuid5

from vidgen.contracts.script import CompressedPlotPlan, RecapScript

SCRIPT_NAMESPACE = UUID("2f6f9e5f-8a2b-4a34-9a2a-3d6a2f0a9d41")


def stable_id(
    *, input_hash: str, kind: str, key: str, contract_version: str, prompt_version: str
) -> UUID:
    canonical_key = ":".join(
        (input_hash, contract_version, prompt_version, kind, key.strip().casefold())
    )
    return uuid5(SCRIPT_NAMESPACE, canonical_key)


def compute_segment_content_hash(
    *,
    text: str,
    segment_type: str,
    speaker_kind: str,
    speaker_character_id: UUID | None,
    anonymous_speaker_label: str | None,
    joke_annotations: list[dict[str, Any]],
    visual_gag: str | None,
    voice_direction: str,
) -> str:
    payload = json.dumps(
        {
            "text": text,
            "segment_type": segment_type,
            "speaker_kind": speaker_kind,
            "speaker_character_id": str(speaker_character_id) if speaker_character_id else None,
            "anonymous_speaker_label": anonymous_speaker_label,
            "joke_annotations": joke_annotations,
            "visual_gag": visual_gag,
            "voice_direction": voice_direction,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def canonicalize_plan(plan: CompressedPlotPlan) -> CompressedPlotPlan:
    data = plan.model_copy(deep=True)
    data.selected_beats.sort(key=lambda beat: (beat.sequence, str(beat.plot_beat_id)))
    data.omitted_beats.sort(key=lambda beat: str(beat.plot_beat_id))
    data.connective_explanations.sort(
        key=lambda item: (str(item.cause_beat_id), str(item.effect_beat_id))
    )
    data.pacing_plan.sort(key=lambda item: str(item.plot_beat_id))
    data.word_budget.allocations.sort(key=lambda item: str(item.plot_beat_id))
    return data


def canonical_plan_hash(plan: CompressedPlotPlan) -> str:
    payload = json.dumps(
        canonicalize_plan(plan).model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def canonicalize_script(script: RecapScript) -> RecapScript:
    data = script.model_copy(deep=True)
    data.segments.sort(key=lambda segment: segment.sequence)
    data.callbacks.sort(key=lambda item: str(item.callback_id))
    data.beat_coverage.sort(key=lambda item: str(item.plot_beat_id))
    return data


def canonical_script_hash(script: RecapScript) -> str:
    payload = json.dumps(
        canonicalize_script(script).model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode()).hexdigest()
