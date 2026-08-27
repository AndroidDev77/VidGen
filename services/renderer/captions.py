"""Deterministic caption grouping using approved words and T12 timing authority."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from uuid import UUID

from vidgen.contracts.render import (
    CaptionCue,
    CaptionTrack,
    CaptionValidationDiagnostic,
    CaptionValidationReport,
    CaptionWord,
)


@dataclass(frozen=True, slots=True)
class CaptionConfig:
    max_chars_per_line: int = 42
    max_lines: int = 2
    max_words_per_cue: int = 12
    min_duration_us: int = 500_000
    max_duration_us: int = 7_000_000
    max_chars_per_second: int = 25
    safe_zone_percent: int = 10
    language: str = "en"


def _join(words: list[str]) -> str:
    text = " ".join(words)
    return re.sub(r"\s+([,.;:!?])", r"\1", text).strip()


def _wrap(text: str, width: int, max_lines: int) -> list[str]:
    tokens = text.split()
    lines: list[str] = []
    while tokens:
        line = tokens.pop(0)
        while tokens and len(line) + 1 + len(tokens[0]) <= width:
            line += " " + tokens.pop(0)
        lines.append(line)
    if len(lines) > max_lines:
        raise ValueError("caption cannot be reflowed within configured line limit")
    return lines


def build_caption_track(
    *,
    track_id: UUID,
    words: list[CaptionWord],
    duration_us: int,
    config: CaptionConfig | None = None,
) -> tuple[CaptionTrack, CaptionValidationReport]:
    config = config or CaptionConfig()
    if not words or any(word.sequence != i for i, word in enumerate(words)):
        raise ValueError("caption words must be non-empty and densely ordered")
    if any(words[i].start_us < words[i - 1].end_us for i in range(1, len(words))):
        raise ValueError("caption word timings overlap or reverse")
    if words[-1].end_us > duration_us:
        raise ValueError("caption words exceed narration duration")
    groups: list[list[CaptionWord]] = []
    current: list[CaptionWord] = []
    for word in words:
        candidate = [*current, word]
        text = _join([item.text for item in candidate])
        elapsed = word.end_us - candidate[0].start_us
        boundary = bool(re.search(r"[.!?;:]$", word.text))
        overflow = (
            len(text) > config.max_chars_per_line * config.max_lines
            or len(candidate) > config.max_words_per_cue
        )
        too_long = elapsed > config.max_duration_us
        if current and (overflow or too_long):
            groups.append(current)
            current = [word]
        else:
            current = candidate
        if boundary and current:
            groups.append(current)
            current = []
    if current:
        groups.append(current)

    cues: list[CaptionCue] = []
    adjustments: list[str] = []
    diagnostics: list[CaptionValidationDiagnostic] = []
    for index, group in enumerate(groups, 1):
        start, end = group[0].start_us, group[-1].end_us
        minimum_end = min(duration_us, start + config.min_duration_us)
        if end < minimum_end:
            next_start = groups[index][0].start_us if index < len(groups) else duration_us
            end = min(minimum_end, next_start)
            adjustments.append(f"cue:{index}:minimum_duration")
        text = _join([word.text for word in group])
        lines = _wrap(text, config.max_chars_per_line, config.max_lines)
        elapsed_seconds = (end - start) / 1_000_000
        cps = len(text) / elapsed_seconds if elapsed_seconds else float("inf")
        if cps > config.max_chars_per_second:
            diagnostics.append(
                CaptionValidationDiagnostic(
                    code="reading_speed",
                    severity="warning",
                    message="cue exceeds configured reading speed",
                    cue_sequence=index,
                )
            )
        cues.append(
            CaptionCue(
                sequence=index,
                start_us=start,
                end_us=end,
                lines=lines,
                word_start=group[0].sequence,
                word_end=group[-1].sequence + 1,
            )
        )
    track = CaptionTrack(
        caption_track_id=track_id,
        language=config.language,
        cues=cues,
        duration_us=duration_us,
        safe_zone_percent=config.safe_zone_percent,
    )
    identity = caption_identity(track)
    return track, CaptionValidationReport(
        valid=not any(d.severity == "error" for d in diagnostics),
        caption_identity=identity,
        diagnostics=diagnostics,
        adjustment_codes=adjustments,
    )


def caption_identity(track: CaptionTrack) -> str:
    """Hash the complete canonical caption contract, including every cue."""
    return hashlib.sha256(
        json.dumps(track.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _timestamp(us: int, separator: str) -> str:
    milliseconds = us // 1000
    hours, milliseconds = divmod(milliseconds, 3_600_000)
    minutes, milliseconds = divmod(milliseconds, 60_000)
    seconds, milliseconds = divmod(milliseconds, 1000)
    return f"{hours:02}:{minutes:02}:{seconds:02}{separator}{milliseconds:03}"


def serialize_srt(track: CaptionTrack) -> str:
    return (
        "\n".join(
            f"{cue.sequence}\n{_timestamp(cue.start_us, ',')} --> {_timestamp(cue.end_us, ',')}\n"
            + "\n".join(cue.lines)
            + "\n"
            for cue in track.cues
        )
        + "\n"
    )


def serialize_webvtt(track: CaptionTrack) -> str:
    return (
        "WEBVTT\n\n"
        + "\n".join(
            f"{cue.sequence}\n{_timestamp(cue.start_us, '.')} --> {_timestamp(cue.end_us, '.')}\n"
            + "\n".join(cue.lines)
            + "\n"
            for cue in track.cues
        )
        + "\n"
    )


def serialize_ass(track: CaptionTrack) -> str:
    header = (
        "[Script Info]\nScriptType: v4.00+\n[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, Alignment, "
        "MarginL, MarginR, MarginV\n"
        "Style: Default,Arial,48,&H00FFFFFF,2,80,80,60\n[Events]\n"
        "Format: Layer, Start, End, Style, Text\n"
    )

    def ts(us: int) -> str:
        cs = us // 10_000
        h, cs = divmod(cs, 360_000)
        m, cs = divmod(cs, 6_000)
        s, cs = divmod(cs, 100)
        return f"{h}:{m:02}:{s:02}.{cs:02}"

    rows = []
    for cue in track.cues:
        text = r"\N".join(cue.lines).replace("\\", r"\\").replace("{", r"\{").replace("}", r"\}")
        rows.append(f"Dialogue: 0,{ts(cue.start_us)},{ts(cue.end_us)},Default,{text}")
    return header + "\n".join(rows) + "\n"
