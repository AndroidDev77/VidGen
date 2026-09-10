"""Clearing empty beats out of a provider's script before anything downstream sees them."""

from __future__ import annotations

from uuid import uuid4

import pytest

from services.script.canonicalize import EMPTY_SEGMENT_DROPPED, drop_empty_segments
from services.script.compressor import compress_plot
from services.script.validator import (
    build_beat_coverage,
    canonical_word_count,
    spoken_word_count,
    validate_recap_script,
)
from services.script.writer import write_script
from tests.test_openai_script_adapter import _compression_request, _writing_request
from tests.test_script_pipeline import _make_analysis
from vidgen.contracts.script import RecapScript


def _script() -> RecapScript:
    analysis = _make_analysis(uuid4())
    plan = compress_plot(analysis=analysis, request=_compression_request(analysis), plan_id=uuid4())
    script = write_script(plan=plan, request=_writing_request(analysis, plan), script_id=uuid4())
    assert script.callbacks, "the writer's callback is what the pruning case exercises"
    return script.model_copy(update={"beat_coverage": build_beat_coverage(script, plan)})


def test_a_script_without_empty_beats_is_returned_unchanged() -> None:
    script = _script()
    assert drop_empty_segments(script) is script


def test_empty_beats_are_removed_and_the_rest_re_sequenced() -> None:
    script = _script()
    middle = script.segments[1]
    pause = middle.model_copy(
        update={"segment_id": uuid4(), "type": "PAUSE", "text": "", "joke_annotations": []}
    )
    blank = middle.model_copy(update={"segment_id": uuid4(), "text": " \n "})
    padded = script.model_copy(
        update={
            "segments": [
                script.segments[0],
                pause.model_copy(update={"sequence": 1}),
                blank.model_copy(update={"sequence": 2}),
                *(
                    segment.model_copy(update={"sequence": segment.sequence + 2})
                    for segment in script.segments[1:]
                ),
            ]
        }
    )
    cleared = drop_empty_segments(padded)
    assert [s.segment_id for s in cleared.segments] == [s.segment_id for s in script.segments]
    assert [s.sequence for s in cleared.segments] == list(range(len(script.segments)))
    assert cleared.actual_word_count == sum(len(s.text.split()) for s in script.segments)
    assert cleared.callbacks == script.callbacks
    assert [note.code for note in cleared.warnings] == [
        EMPTY_SEGMENT_DROPPED,
        EMPTY_SEGMENT_DROPPED,
    ]
    assert f"PAUSE segment {pause.segment_id} at sequence 1" in cleared.warnings[0].message
    assert f"NARRATION segment {blank.segment_id} at sequence 2" in cleared.warnings[1].message


def test_dropping_a_callback_payoff_removes_the_callback_and_unlinks_its_joke() -> None:
    script = _script()
    callback = script.callbacks[0]
    payoff_index = next(
        i for i, s in enumerate(script.segments) if s.segment_id == callback.payoff_segment_id
    )
    segments = list(script.segments)
    segments[payoff_index] = segments[payoff_index].model_copy(update={"text": ""})
    cleared = drop_empty_segments(script.model_copy(update={"segments": segments}))
    assert cleared.callbacks == []
    assert callback.payoff_segment_id not in {s.segment_id for s in cleared.segments}
    assert all(
        annotation.callback_id is None
        for segment in cleared.segments
        for annotation in segment.joke_annotations
    )
    # Coverage no longer credits the removed segment.
    for item in cleared.beat_coverage:
        assert callback.payoff_segment_id not in item.segment_ids
        assert item.coverage == ("covered" if item.segment_ids else "missing")
    assert any(item.coverage == "missing" for item in cleared.beat_coverage)


def test_a_script_with_no_text_at_all_is_refused() -> None:
    script = _script()
    blank = [s.model_copy(update={"text": ""}) for s in script.segments]
    with pytest.raises(ValueError, match="no segment with text"):
        drop_empty_segments(script.model_copy(update={"segments": blank}))


def test_a_pause_with_text_is_still_an_empty_beat() -> None:
    """Nothing downstream handles a PAUSE, whatever it says; it is cleared too."""
    script = _script()
    last = script.segments[-1]
    trailing = last.model_copy(
        update={
            "segment_id": uuid4(),
            "sequence": last.sequence + 1,
            "type": "PAUSE",
            "text": "(beat)",
            "joke_annotations": [],
        }
    )
    cleared = drop_empty_segments(
        script.model_copy(update={"segments": [*script.segments, trailing]})
    )
    assert [s.segment_id for s in cleared.segments] == [s.segment_id for s in script.segments]
    assert cleared.actual_word_count == script.actual_word_count
    assert [note.code for note in cleared.warnings] == [EMPTY_SEGMENT_DROPPED]
    assert "is a pause" in cleared.warnings[0].message


def test_word_counts_leave_pause_segments_out() -> None:
    script = _script()
    pause = script.segments[0].model_copy(
        update={
            "segment_id": uuid4(),
            "type": "PAUSE",
            "text": "one two three",
            "joke_annotations": [],
        }
    )
    spoken = sum(len(s.text.split()) for s in script.segments)
    assert spoken_word_count(script.segments) == spoken
    assert spoken_word_count([*script.segments, pause]) == spoken
    assert canonical_word_count(pause.text) == 3


def test_the_validator_counts_only_spoken_words() -> None:
    analysis = _make_analysis(uuid4())
    plan = compress_plot(analysis=analysis, request=_compression_request(analysis), plan_id=uuid4())
    script = write_script(plan=plan, request=_writing_request(analysis, plan), script_id=uuid4())
    pause = script.segments[-1].model_copy(
        update={
            "segment_id": uuid4(),
            "sequence": script.segments[-1].sequence + 1,
            "type": "PAUSE",
            "text": "one two three",
            "joke_annotations": [],
        }
    )
    with_pause = script.model_copy(update={"segments": [*script.segments, pause]})
    honest = validate_recap_script(with_pause, analysis=analysis, plan=plan)
    assert "WORD_COUNT_MISMATCH" not in {error.code for error in honest.errors}
    inflated = validate_recap_script(
        with_pause.model_copy(update={"actual_word_count": script.actual_word_count + 3}),
        analysis=analysis,
        plan=plan,
    )
    assert "WORD_COUNT_MISMATCH" in {error.code for error in inflated.errors}
