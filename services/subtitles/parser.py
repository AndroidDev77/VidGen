from __future__ import annotations

import html
import re
from collections.abc import Iterable

from vidgen.contracts.subtitles import SubtitleCue

TIMING = re.compile(
    r"(?P<start>(?:\d{1,2}:)?\d{2}:\d{2}[,.]\d{3})\s*-->\s*"
    r"(?P<end>(?:\d{1,2}:)?\d{2}:\d{2}[,.]\d{3})"
)
ASS_DIALOGUE = re.compile(
    r"^Dialogue:\s*[^,]*,(?P<start>[^,]+),(?P<end>[^,]+),(?:[^,]*,){6}(?P<text>.*)$"
)
TAG = re.compile(r"<[^>]+>|\{\\[^}]+\}")
HI_LINE = re.compile(r"^\s*(?:\[[^]]+]|\([^)]{1,80}\))\s*$")
SPEAKER = re.compile(r"^([A-Z][A-Z0-9 _.'-]{1,40}):\s+(.+)$")


def parse_subtitles(content: bytes, subtitle_format: str) -> list[SubtitleCue]:
    text = _decode(content).replace("\r\n", "\n").replace("\r", "\n")
    normalized = subtitle_format.lower().lstrip(".")
    if normalized in {"srt", "subrip", "vtt", "webvtt"}:
        return _parse_timed_blocks(text)
    if normalized in {"ass", "ssa"}:
        return _parse_ass(text)
    raise ValueError(f"unsupported text subtitle format: {subtitle_format}")


def _parse_timed_blocks(text: str) -> list[SubtitleCue]:
    lines = text.split("\n")
    cues: list[SubtitleCue] = []
    index = 0
    while index < len(lines):
        match = TIMING.search(lines[index])
        if match is None:
            index += 1
            continue
        body: list[str] = []
        index += 1
        while index < len(lines) and lines[index].strip():
            body.append(lines[index])
            index += 1
        cleaned, speaker = _clean_lines(body)
        if cleaned:
            cues.append(
                SubtitleCue(
                    sequence=len(cues),
                    start_seconds=_timestamp(match.group("start")),
                    end_seconds=_timestamp(match.group("end")),
                    text=cleaned,
                    speaker_hint=speaker,
                )
            )
    return _ordered_nonempty(cues)


def _parse_ass(text: str) -> list[SubtitleCue]:
    cues: list[SubtitleCue] = []
    for line in text.split("\n"):
        match = ASS_DIALOGUE.match(line)
        if match is None:
            continue
        cleaned, speaker = _clean_lines([match.group("text").replace(r"\N", " ")])
        if cleaned:
            cues.append(
                SubtitleCue(
                    sequence=len(cues),
                    start_seconds=_ass_timestamp(match.group("start")),
                    end_seconds=_ass_timestamp(match.group("end")),
                    text=cleaned,
                    speaker_hint=speaker,
                )
            )
    return _ordered_nonempty(cues)


def _clean_lines(lines: Iterable[str]) -> tuple[str, str | None]:
    values: list[str] = []
    for line in lines:
        cleaned = html.unescape(TAG.sub("", line)).strip()
        if cleaned and not HI_LINE.fullmatch(cleaned):
            values.append(cleaned)
    text = " ".join(values)
    text = re.sub(r"\s+", " ", text).strip()
    match = SPEAKER.match(text)
    if match:
        return match.group(2).strip(), match.group(1).strip()
    return text, None


def _ordered_nonempty(cues: list[SubtitleCue]) -> list[SubtitleCue]:
    ordered = sorted(cues, key=lambda cue: (cue.start_seconds, cue.end_seconds, cue.sequence))
    result: list[SubtitleCue] = []
    for cue in ordered:
        if cue.end_seconds <= cue.start_seconds:
            continue
        result.append(cue.model_copy(update={"sequence": len(result)}))
    if not result:
        raise ValueError("subtitle contains no valid timed cues")
    return result


def _decode(content: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-16", "cp1252"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ValueError("subtitle text encoding is unsupported")


def _timestamp(value: str) -> float:
    parts = value.replace(",", ".").split(":")
    hours, minutes, seconds = parts if len(parts) == 3 else ("0", parts[0], parts[1])
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def _ass_timestamp(value: str) -> float:
    hours, minutes, seconds = value.split(":")
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
