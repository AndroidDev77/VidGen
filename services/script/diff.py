"""Structured segment-level diffing between two RecapScript versions."""

from __future__ import annotations

from vidgen.contracts.script import RecapScript, ScriptDiff, ScriptEdit
from services.script.validator import canonical_word_count


def build_script_diff(
    previous: RecapScript | None, current: RecapScript, edits: list[ScriptEdit] | None = None
) -> ScriptDiff:
    if previous is None:
        return ScriptDiff(
            from_version=None,
            to_version=current.version,
            added_segment_ids=[segment.segment_id for segment in current.segments],
        )
    previous_by_id = {segment.segment_id: segment for segment in previous.segments}
    current_by_id = {segment.segment_id: segment for segment in current.segments}
    added = [sid for sid in current_by_id if sid not in previous_by_id]
    removed = [sid for sid in previous_by_id if sid not in current_by_id]
    unchanged = [
        sid
        for sid in current_by_id
        if sid in previous_by_id and current_by_id[sid].content_hash == previous_by_id[sid].content_hash
    ]
    changed_ids = [
        sid
        for sid in current_by_id
        if sid in previous_by_id and current_by_id[sid].content_hash != previous_by_id[sid].content_hash
    ]
    edits_by_segment = {edit.segment_id: edit for edit in edits or []}
    changed_segments: list[ScriptEdit] = []
    for segment_id in changed_ids:
        existing = edits_by_segment.get(segment_id)
        if existing is not None:
            changed_segments.append(existing)
            continue
        old, new = previous_by_id[segment_id], current_by_id[segment_id]
        changed_segments.append(
            ScriptEdit(
                segment_id=segment_id,
                old_text=old.text,
                new_text=new.text,
                reason="Content changed between versions.",
                plot_beat_ids=list(new.plot_beat_ids),
                changes_word_count=canonical_word_count(old.text) != canonical_word_count(new.text),
                was_locked=old.locked,
            )
        )
    return ScriptDiff(
        from_version=previous.version,
        to_version=current.version,
        added_segment_ids=added,
        removed_segment_ids=removed,
        changed_segments=changed_segments,
        unchanged_segment_ids=unchanged,
    )
