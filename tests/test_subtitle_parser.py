from __future__ import annotations

import struct
from pathlib import Path

from services.subtitles.movie_hash import BLOCK_SIZE, opensubtitles_movie_hash
from services.subtitles.parser import parse_subtitles


def test_srt_parser_cleans_formatting_and_preserves_speaker_hint() -> None:
    cues = parse_subtitles(
        b"1\n00:00:00,100 --> 00:00:01,250\n<b>ALICE: Hello</b>\n\n"
        b"2\n00:00:01,300 --> 00:00:02,000\n[MUSIC]\n\n"
        b"3\n00:00:02,100 --> 00:00:03,000\nWorld &amp; friends\n",
        "srt",
    )
    assert [cue.text for cue in cues] == ["Hello", "World & friends"]
    assert cues[0].speaker_hint == "ALICE"
    assert [cue.sequence for cue in cues] == [0, 1]


def test_webvtt_and_ass_are_normalized() -> None:
    vtt = parse_subtitles(b"WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nHello\n", "webvtt")
    ass = parse_subtitles(
        b"[Events]\nDialogue: 0,0:00:00.00,0:00:01.20,Default,,0,0,0,,Hello\\Nthere\n",
        "ass",
    )
    assert vtt[0].text == "Hello"
    assert ass[0].text == "Hello there"


def test_opensubtitles_hash_matches_specification(tmp_path: Path) -> None:
    path = tmp_path / "video.bin"
    words = list(range((BLOCK_SIZE * 2) // 8))
    path.write_bytes(b"".join(struct.pack("<Q", word) for word in words))
    expected = (path.stat().st_size + sum(words)) & ((1 << 64) - 1)
    assert opensubtitles_movie_hash(path) == f"{expected:016x}"
