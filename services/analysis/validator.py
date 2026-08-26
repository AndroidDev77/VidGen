"""Deterministic validation; providers never validate their own output."""

from __future__ import annotations

from collections.abc import Iterable
from uuid import UUID

from vidgen.contracts.episode_analysis import (
    AnalysisValidationError,
    AnalysisValidationReport,
    EpisodeAnalysis,
    SourceReference,
)


def validate_episode_analysis(
    analysis: EpisodeAnalysis,
    *,
    valid_scene_ids: set[UUID],
    valid_reference_ids: set[UUID],
) -> AnalysisValidationReport:
    errors: list[AnalysisValidationError] = []

    def error(code: str, path: str, value: object, explanation: str) -> None:
        errors.append(
            AnalysisValidationError(
                code=code, entity_path=path, invalid_value=str(value), explanation=explanation
            )
        )

    def refs(path: str, values: Iterable[SourceReference]) -> None:
        for index, item in enumerate(values):
            reference = item.reference_id
            if reference not in valid_reference_ids:
                error(
                    "UNKNOWN_SOURCE_REFERENCE",
                    f"{path}.{index}.reference_id",
                    reference,
                    "Reference must belong to the selected evidence package",
                )

    scene_ids = [scene.scene_id for scene in analysis.scenes]
    sequences = [scene.sequence for scene in analysis.scenes]
    if len(scene_ids) != len(set(scene_ids)):
        error("DUPLICATE_ID", "scenes", scene_ids, "Canonical scene IDs must be unique")
    if set(scene_ids) != valid_scene_ids:
        error(
            "SCENE_SET_MISMATCH",
            "scenes",
            scene_ids,
            "Analysis must contain exactly the selected evidence scenes",
        )
    if sequences != sorted(sequences) or len(sequences) != len(set(sequences)):
        error(
            "INVALID_CHRONOLOGY",
            "scenes.sequence",
            sequences,
            "Scene sequences must be unique and monotonic",
        )
    previous_end = 0
    for index, scene in enumerate(analysis.scenes):
        if scene.source_end_ms > analysis.duration_ms or scene.source_start_ms < previous_end:
            error(
                "INVALID_SCENE_RANGE",
                f"scenes.{index}",
                scene.source_start_ms,
                "Scene must be in duration and may not overlap a preceding scene",
            )
        previous_end = scene.source_end_ms
        refs(f"scenes.{index}.source_references", scene.source_references)

    characters = {item.character_id for item in analysis.characters}
    locations = {item.location_id for item in analysis.locations}
    stable_ids = [*characters, *locations, *scene_ids]
    if len(stable_ids) != len(set(stable_ids)):
        error(
            "DUPLICATE_ID", "canonical_entities", stable_ids, "Stable IDs must be globally unique"
        )
    character_keys = [
        (c.canonical_name.casefold(), tuple(sorted(a.casefold() for a in c.aliases)))
        for c in analysis.characters
    ]
    if len(character_keys) != len(set(character_keys)):
        error(
            "DUPLICATE_CHARACTER",
            "characters",
            character_keys,
            "Duplicate canonical characters are not allowed",
        )
    location_keys = [location.canonical_name.casefold() for location in analysis.locations]
    if len(location_keys) != len(set(location_keys)):
        error(
            "DUPLICATE_LOCATION",
            "locations",
            location_keys,
            "Duplicate canonical locations are not allowed",
        )
    for index, scene in enumerate(analysis.scenes):
        for character_id in scene.character_ids:
            if character_id not in characters:
                error(
                    "UNKNOWN_CHARACTER",
                    f"scenes.{index}.character_ids",
                    character_id,
                    "Character must resolve",
                )
        if scene.location_id is not None and scene.location_id not in locations:
            error(
                "UNKNOWN_LOCATION",
                f"scenes.{index}.location_id",
                scene.location_id,
                "Location must resolve",
            )
    for index, event in enumerate(analysis.state_events):
        if event.entity_id not in characters | locations:
            error(
                "UNKNOWN_STATE_ENTITY",
                f"state_events.{index}.entity_id",
                event.entity_id,
                "State entity must resolve",
            )
        if event.scene_id not in valid_scene_ids:
            error(
                "UNKNOWN_SCENE",
                f"state_events.{index}.scene_id",
                event.scene_id,
                "State scene must resolve",
            )
        refs(f"state_events.{index}.source_references", event.source_references)
    for index, relationship in enumerate(analysis.relationships):
        if (
            relationship.source_character_id not in characters
            or relationship.target_character_id not in characters
        ):
            error(
                "UNKNOWN_RELATIONSHIP_ENDPOINT",
                f"relationships.{index}",
                relationship.relationship_id,
                "Both relationship endpoints must resolve",
            )
        refs(f"relationships.{index}.source_references", relationship.source_references)
    beat_ids = {beat.plot_beat_id for beat in analysis.plot_beats}
    beat_sequence = {beat.plot_beat_id: beat.sequence for beat in analysis.plot_beats}
    for index, beat in enumerate(analysis.plot_beats):
        if not set(beat.scene_ids) <= valid_scene_ids:
            error(
                "UNKNOWN_SCENE",
                f"plot_beats.{index}.scene_ids",
                beat.scene_ids,
                "Beat scenes must resolve",
            )
        if not set(beat.character_ids) <= characters:
            error(
                "UNKNOWN_CHARACTER",
                f"plot_beats.{index}.character_ids",
                beat.character_ids,
                "Beat characters must resolve",
            )
        if beat.mandatory and not beat.source_references:
            error(
                "MANDATORY_BEAT_WITHOUT_EVIDENCE",
                f"plot_beats.{index}",
                beat.plot_beat_id,
                "Mandatory beats require evidence",
            )
        refs(f"plot_beats.{index}.source_references", beat.source_references)
    graph: dict[UUID, list[UUID]] = {beat_id: [] for beat_id in beat_ids}
    for index, dependency in enumerate(analysis.beat_dependencies):
        refs(f"beat_dependencies.{index}.source_references", dependency.source_references)
        if dependency.cause_beat_id not in beat_ids or dependency.effect_beat_id not in beat_ids:
            error(
                "UNKNOWN_BEAT_DEPENDENCY",
                f"beat_dependencies.{index}",
                dependency.effect_beat_id,
                "Dependency beats must resolve",
            )
            continue
        graph[dependency.cause_beat_id].append(dependency.effect_beat_id)
        if beat_sequence[dependency.cause_beat_id] >= beat_sequence[dependency.effect_beat_id]:
            error(
                "CAUSE_AFTER_EFFECT",
                f"beat_dependencies.{index}",
                dependency.cause_beat_id,
                "Cause must precede effect",
            )
    visiting: set[UUID] = set()
    visited: set[UUID] = set()

    def visit(node: UUID) -> bool:
        if node in visiting:
            return True
        if node in visited:
            return False
        visiting.add(node)
        cycle = any(visit(child) for child in graph[node])
        visiting.remove(node)
        visited.add(node)
        return cycle

    if any(visit(node) for node in graph if node not in visited):
        error(
            "CYCLIC_BEAT_DEPENDENCY",
            "beat_dependencies",
            "cycle",
            "Beat dependency graph must be acyclic",
        )
    refs("source_references", analysis.source_references)
    for collection_name in ("characters", "locations", "unresolved_ambiguities"):
        for index, entity in enumerate(getattr(analysis, collection_name)):
            refs(f"{collection_name}.{index}.source_references", entity.source_references)
    return AnalysisValidationReport(valid=not errors, errors=errors)
