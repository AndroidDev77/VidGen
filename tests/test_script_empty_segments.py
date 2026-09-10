"""Clearing empty beats out of a provider's script before anything downstream sees them."""

from __future__ import annotations

from uuid import uuid4

import pytest

from services.script.canonicalize import EMPTY_SEGMENT_DROPPED, drop_empty_segments
from services.script.compressor import compress_plot
from services.script.validator import build_beat_coverage
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
