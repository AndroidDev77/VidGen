"""Stable IDs, ordering, and content hashing for T11 canonical artifacts."""

from __future__ import annotations

import hashlib
import json
from typing import Any
from uuid import UUID, uuid5

from vidgen.contracts.episode_analysis import StructuredNote
from vidgen.contracts.script import CompressedPlotPlan, RecapScript, ScriptSegment

SCRIPT_NAMESPACE = UUID("2f6f9e5f-8a2b-4a34-9a2a-3d6a2f0a9d41")
#: Warning code recorded on a script whose provider output carried a segment
#: with no text; the segment is removed rather than passed downstream.
EMPTY_SEGMENT_DROPPED = "EMPTY_SEGMENT_DROPPED"
#: Warning code recorded on a script whose provider output carried a
#: ``callback_id`` on a joke not typed ``callback``; the annotation is either
#: retyped or unlinked before the contract sees it.
JOKE_CALLBACK_NORMALIZED = "JOKE_CALLBACK_NORMALIZED"


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


def _id_key(value: Any) -> str:
    return str(value).strip().lower()


def normalize_raw_joke_callbacks(raw: Any) -> None:
    """Reconcile ``callback_id`` with ``joke_type`` on a raw script payload, in place.

    ``JokeAnnotation`` refuses a ``callback_id`` on a joke whose type is not
    ``callback``. That is a cross-field rule a structured-output schema cannot
    express, so a model can satisfy every field and still emit it - and the
    whole payload would then fail validation. The script's ``callbacks`` list
    decides which reading is right:

    * the id resolves to a callback whose payoff is this annotation's segment,
      and no other annotation there already claims it - the annotation *is*
      the callback payoff with the wrong type, so it is retyped ``callback``;
    * otherwise (no such callback, the annotation sits in the setup or another
      segment, or the payoff already has its callback joke) the id is spurious
      and is dropped; the joke keeps the mechanism the model gave it.

    Each change is recorded as a ``JOKE_CALLBACK_NORMALIZED`` warning. Anything
    that is not shaped like a script is left for the contract to refuse.
    """
    if not isinstance(raw, dict):
        return
    segments = raw.get("segments")
    if not isinstance(segments, list):
        return
    payoff_by_callback = {
        _id_key(item["callback_id"]): _id_key(item.get("payoff_segment_id"))
        for item in raw.get("callbacks") or []
        if isinstance(item, dict) and item.get("callback_id") is not None
    }
    notes: list[dict[str, str]] = []
    for segment in segments:
        if not isinstance(segment, dict):
            continue
        annotations = segment.get("joke_annotations")
        if not isinstance(annotations, list):
            continue
        segment_key = _id_key(segment.get("segment_id"))
        claimed = {
            _id_key(item["callback_id"])
            for item in annotations
            if isinstance(item, dict)
            and item.get("joke_type") == "callback"
            and item.get("callback_id") is not None
        }
        for annotation in annotations:
            if not isinstance(annotation, dict):
                continue
            callback_id = annotation.get("callback_id")
            if callback_id is None or annotation.get("joke_type") == "callback":
                continue
            key = _id_key(callback_id)
            where = f"joke {annotation.get('joke_id')} in segment {segment.get('segment_id')}"
            if payoff_by_callback.get(key) == segment_key and key not in claimed:
                claimed.add(key)
                notes.append(
                    {
                        "code": JOKE_CALLBACK_NORMALIZED,
                        "message": (
                            f"{where} paid off callback {callback_id} but was typed "
                            f"{annotation.get('joke_type')}; retyped as callback"
                        ),
                    }
                )
                annotation["joke_type"] = "callback"
            else:
                notes.append(
                    {
                        "code": JOKE_CALLBACK_NORMALIZED,
                        "message": (
                            f"{where} is typed {annotation.get('joke_type')} but carried "
                            f"callback_id {callback_id}, which it does not pay off; "
                            "callback_id removed"
                        ),
                    }
                )
                annotation["callback_id"] = None
    if notes:
        warnings = raw.setdefault("warnings", [])
        if isinstance(warnings, list):
            warnings.extend(notes)


def _is_empty_beat(segment: ScriptSegment) -> bool:
    return segment.type == "PAUSE" or not segment.text.strip()


def drop_empty_segments(script: RecapScript) -> RecapScript:
    """Remove every empty beat and keep the script consistent without it.

    A writer or editor occasionally emits an empty beat: a segment with blank
    text, or one typed ``PAUSE`` - the only type the contract lets be empty and
    a type nothing downstream handles. Narration has no words to voice and the
    storyboard has no words to time, so both are cleared here, at the stage
    that produced them. Surviving segments are re-sequenced
    contiguously, a callback whose setup or payoff was removed goes with it
    (and the joke annotation that pointed at it is unlinked), beat coverage and
    the word count are recomputed, and one ``EMPTY_SEGMENT_DROPPED`` warning is
    recorded per removed segment. A script with no text at all is refused.
    """
    dropped = [segment for segment in script.segments if _is_empty_beat(segment)]
    if not dropped:
        return script
    survivors = [segment for segment in script.segments if not _is_empty_beat(segment)]
    if not survivors:
        raise ValueError("script has no segment with text")
    surviving_ids = {segment.segment_id for segment in survivors}
    callbacks = [
        callback
        for callback in script.callbacks
        if callback.setup_segment_id in surviving_ids
        and callback.payoff_segment_id in surviving_ids
    ]
    removed_callbacks = {callback.callback_id for callback in script.callbacks} - {
        callback.callback_id for callback in callbacks
    }
    segments = [
        segment.model_copy(
            update={
                "sequence": index,
                "joke_annotations": [
                    annotation.model_copy(update={"callback_id": None})
                    if annotation.callback_id in removed_callbacks
                    else annotation
                    for annotation in segment.joke_annotations
                ],
            }
        )
        for index, segment in enumerate(survivors)
    ]
    coverage = [
        item.model_copy(
            update={
                "segment_ids": kept_ids,
                "coverage": "covered" if kept_ids else "missing",
            }
        )
        for item in script.beat_coverage
        for kept_ids in [[sid for sid in item.segment_ids if sid in surviving_ids]]
    ]
    warnings = [
        *script.warnings,
        *(
            StructuredNote(
                code=EMPTY_SEGMENT_DROPPED,
                message=(
                    f"{segment.type} segment {segment.segment_id} at sequence "
                    f"{segment.sequence} "
                    + ("is a pause" if segment.type == "PAUSE" else "had no text")
                    + " and was removed"
                ),
            )
            for segment in dropped
        ),
    ]
    updated = script.model_copy(
        update={
            "segments": segments,
            "callbacks": callbacks,
            "beat_coverage": coverage,
            "actual_word_count": sum(len(segment.text.split()) for segment in segments),
            "warnings": warnings,
        }
    )
    # ``model_copy`` does not validate; the result crosses a pipeline boundary.
    return RecapScript.model_validate(updated.model_dump())
