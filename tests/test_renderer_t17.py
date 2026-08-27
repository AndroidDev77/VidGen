from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from services.renderer.audio import parse_loudnorm_json
from services.renderer.captions import (
    CaptionConfig,
    build_caption_track,
    serialize_ass,
    serialize_srt,
    serialize_webvtt,
)
from services.renderer.manifest import render_identity
from services.renderer.normalize import stage_chunks
from services.renderer.selection import animation_run_for_video
from vidgen.contracts.render import CaptionTrack, CaptionWord


def words() -> list[CaptionWord]:
    values = ["Repeat", "repeat,", "then", "finish!"]
    return [
        CaptionWord(sequence=i, text=text, start_us=i * 500_000, end_us=(i + 1) * 500_000)
        for i, text in enumerate(values)
    ]


def test_caption_golden_preserves_approved_punctuation() -> None:
    track, report = build_caption_track(
        track_id=uuid4(),
        words=words(),
        duration_us=2_000_000,
        config=CaptionConfig(max_words_per_cue=3),
    )
    assert report.valid
    assert "Repeat repeat," in serialize_srt(track)
    assert serialize_webvtt(track).startswith("WEBVTT\n\n")
    assert "finish!" in serialize_ass(track)
    assert all(cue.end_us <= track.duration_us for cue in track.cues)


def test_caption_overlap_and_dense_cues_rejected() -> None:
    broken = words()
    broken[1] = broken[1].model_copy(update={"start_us": 100_000})
    with pytest.raises(ValueError, match="overlap"):
        build_caption_track(track_id=uuid4(), words=broken, duration_us=2_000_000)
    track, _ = build_caption_track(track_id=uuid4(), words=words(), duration_us=2_000_000)
    payload = track.model_dump()
    payload["cues"][0]["sequence"] = 2
    with pytest.raises(ValidationError, match="dense"):
        CaptionTrack.model_validate(payload)


def test_identity_is_canonical_and_material_changes_bind() -> None:
    assert render_identity({"b": 2, "a": 1}) == render_identity({"a": 1, "b": 2})
    assert render_identity({"a": 1}) != render_identity({"a": 2})


def test_streaming_staging_hash_and_containment(tmp_path: Path) -> None:
    import hashlib

    content = b"bounded" * 100
    destination = tmp_path / "input"
    assert stage_chunks(
        [content[:10], content[10:]],
        destination,
        root=tmp_path,
        expected_sha256=hashlib.sha256(content).hexdigest(),
    ) == len(content)
    with pytest.raises(ValueError, match="escapes"):
        stage_chunks([b"x"], tmp_path / ".." / "escape", root=tmp_path, expected_sha256="0" * 64)


def test_loudness_structured_and_nonfinite_rejected() -> None:
    output = (
        '{"input_i":"-14.1","input_tp":"-1.6","input_lra":"3.2",'
        '"input_thresh":"-24.0","target_offset":"0.1"}'
    )
    assert parse_loudnorm_json(output)["integrated_lufs"] == -14.1
    with pytest.raises(ValueError, match="non-finite"):
        parse_loudnorm_json(output.replace('"-14.1"', '"NaN"'))


def test_animation_run_selection_follows_animation_item() -> None:
    class CapturingSession:
        statement: object | None = None

        def scalar(self, statement: object) -> None:
            self.statement = statement

    class Video:
        animation_item_id = uuid4()

    session = CapturingSession()
    assert animation_run_for_video(session, Video(), uuid4()) is None  # type: ignore[arg-type]
    sql = str(session.statement)
    assert "animation_items.run_id = animation_runs.id" in sql
    assert "animation_items.id" in sql
    assert "animation_generated_videos.project_id" not in sql
