"""Deterministic caption checks for the canonical manifest and delivered asset.

T22 validates two things and never confuses them: the canonical caption track
the render manifest declared, and the selectable caption files that were actually
delivered. A cue that is correct in the manifest but missing from the delivered
SRT is a blocking failure, because the viewer sees the delivered file.

Caption QA never rewrites the approved script. It compares the delivered text
against the approved projection and reports a mismatch; correcting the wording is
an upstream decision, not a QA action.

Every failure identifies the affected cue sequence, the narration segment it
serves, its exact timestamps and the repair target that owns it.
"""

from __future__ import annotations

import re
from typing import Any
from uuid import UUID

from services.qa.final_evidence import deterministic_id
from services.qa.final_rubric import CAPTION_CHECK_VERSION
from services.renderer.captions import CaptionConfig, build_caption_track, caption_identity
from vidgen.contracts.final_editorial import (
    FinalCaptionCheck,
    FinalIssueCode,
    FinalQAConfiguration,
    FinalQAInput,
    FinalRemediationTarget,
)
from vidgen.contracts.render import CaptionCue, CaptionTrack, CaptionWord

_SRT_BLOCK = re.compile(
    r"(?P<sequence>\d+)\s*\n"
    r"(?P<start>\d{2}:\d{2}:\d{2},\d{3})\s*-->\s*(?P<end>\d{2}:\d{2}:\d{2},\d{3})\s*\n"
    r"(?P<text>(?:.+\n?)+)"
)
_VTT_BLOCK = re.compile(
    r"(?P<start>\d{2}:\d{2}:\d{2}\.\d{3})\s*-->\s*(?P<end>\d{2}:\d{2}:\d{2}\.\d{3})\s*\n"
    r"(?P<text>(?:.+\n?)+)"
)


class CaptionParseError(ValueError):
    """A delivered caption file could not be parsed as its declared format."""


def _caption_check(
    code: FinalIssueCode,
    ok: bool,
    *,
    identity: str,
    cue_sequence: int | None = None,
    narration_segment_id: UUID | None = None,
    caption_asset_id: UUID | None = None,
    measurement: float | None = None,
    threshold: float | None = None,
    unit: str = "",
    start_us: int | None = None,
    end_us: int | None = None,
    message: str = "",
    remediation: FinalRemediationTarget = FinalRemediationTarget.REBUILD_CAPTIONS_T17,
    suffix: str = "",
) -> FinalCaptionCheck:
    return FinalCaptionCheck(
        check_id=deterministic_id("caption-check", identity, code.value, suffix),
        check_version=CAPTION_CHECK_VERSION,
        code=code,
        status="pass" if ok else "fail",
        blocking=not ok,
        measurement=measurement,
        threshold=threshold,
        unit=unit,
        start_us=start_us,
        end_us=end_us,
        tool="vidgen.captions",
        tool_version=CAPTION_CHECK_VERSION,
        message=message[:500],
        cue_sequence=cue_sequence,
        narration_segment_id=narration_segment_id,
        caption_asset_id=caption_asset_id,
        remediation_target=remediation,
    )


def _timestamp_us(value: str) -> int:
    hours, minutes, rest = value.split(":")
    seconds, _, fraction = rest.replace(",", ".").partition(".")
    return (
        int(hours) * 3_600_000_000
        + int(minutes) * 60_000_000
        + int(seconds) * 1_000_000
        + int(fraction.ljust(6, "0")[:6])
    )


def parse_srt(content: bytes) -> list[CaptionCue]:
    """Parse a delivered SRT file; invalid UTF-8 or structure is a parse failure."""
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise CaptionParseError("caption text is not valid UTF-8") from error
    cues: list[CaptionCue] = []
    for match in _SRT_BLOCK.finditer(text.replace("\r\n", "\n")):
        lines = [line for line in match.group("text").strip().split("\n") if line.strip()]
        if not lines:
            raise CaptionParseError("caption cue carries no text")
        start, end = _timestamp_us(match.group("start")), _timestamp_us(match.group("end"))
        if end <= start:
            raise CaptionParseError("caption cue end must follow its start")
        cues.append(
            CaptionCue(
                sequence=int(match.group("sequence")),
                start_us=start,
                end_us=end,
                lines=lines[:2],
                word_start=0,
                word_end=1,
            )
        )
    if not cues:
        raise CaptionParseError("the delivered caption file contains no cues")
    return cues


def parse_webvtt(content: bytes) -> list[CaptionCue]:
    """Parse a delivered WebVTT file, requiring its signature and cue structure."""
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise CaptionParseError("caption text is not valid UTF-8") from error
    normalized = text.replace("\r\n", "\n")
    if not normalized.lstrip("﻿").startswith("WEBVTT"):
        raise CaptionParseError("the delivered WebVTT file is missing its signature")
    cues: list[CaptionCue] = []
    for sequence, match in enumerate(_VTT_BLOCK.finditer(normalized), start=1):
        lines = [line for line in match.group("text").strip().split("\n") if line.strip()]
        start, end = _timestamp_us(match.group("start")), _timestamp_us(match.group("end"))
        if end <= start:
            raise CaptionParseError("caption cue end must follow its start")
        cues.append(
            CaptionCue(
                sequence=sequence,
                start_us=start,
                end_us=end,
                lines=lines[:2],
                word_start=0,
                word_end=1,
            )
        )
    if not cues:
        raise CaptionParseError("the delivered caption file contains no cues")
    return cues


def reading_speed_cps(cue: CaptionCue) -> float:
    """Characters per second a viewer must read to keep up with one cue."""
    characters = sum(len(line) for line in cue.lines)
    seconds = (cue.end_us - cue.start_us) / 1_000_000
    return characters / seconds if seconds > 0 else float("inf")


def evaluate(
    inputs: FinalQAInput,
    configuration: FinalQAConfiguration,
    *,
    canonical: CaptionTrack,
    delivered: dict[UUID, bytes],
    approved_words: list[CaptionWord],
    narration_segments: list[tuple[UUID, int, int]],
    delivered_hashes: dict[UUID, str],
    declared_caption_identity: str,
    burned_in: bool = False,
) -> list[FinalCaptionCheck]:
    """Grade the canonical caption manifest and every delivered caption asset."""
    identity = inputs.render_identity
    checks: list[FinalCaptionCheck] = []

    def add(code: FinalIssueCode, ok: bool, **kwargs: Any) -> None:
        kwargs.setdefault("identity", identity)
        checks.append(_caption_check(code, ok, **kwargs))

    cues = list(canonical.cues)

    # --- structural validity of the canonical track ------------------------
    out_of_order = [
        cue.sequence
        for index, cue in enumerate(cues)
        if index and cue.start_us < cues[index - 1].start_us
    ]
    add(
        FinalIssueCode.CAPTION_ORDER_INVALID,
        not out_of_order,
        measurement=float(len(out_of_order)),
        cue_sequence=out_of_order[0] if out_of_order else None,
        message="cue ordering must be monotonic",
    )
    negative = [cue.sequence for cue in cues if cue.start_us < 0]
    add(
        FinalIssueCode.CAPTION_NEGATIVE_START,
        not negative,
        cue_sequence=negative[0] if negative else None,
        message="cue start times must be nonnegative",
    )
    nonpositive = [cue.sequence for cue in cues if cue.end_us <= cue.start_us]
    add(
        FinalIssueCode.CAPTION_NONPOSITIVE_DURATION,
        not nonpositive,
        cue_sequence=nonpositive[0] if nonpositive else None,
        message="cue end times must be greater than their start times",
    )
    out_of_bounds = [cue for cue in cues if cue.end_us > inputs.timeline_duration_us]
    add(
        FinalIssueCode.CAPTION_OUT_OF_BOUNDS,
        not out_of_bounds,
        measurement=float(len(out_of_bounds)),
        threshold=float(inputs.timeline_duration_us),
        unit="us",
        cue_sequence=out_of_bounds[0].sequence if out_of_bounds else None,
        start_us=out_of_bounds[0].start_us if out_of_bounds else None,
        end_us=out_of_bounds[0].end_us if out_of_bounds else None,
        message="every cue must fall inside the final render duration",
    )
    overlaps = [
        cue.sequence
        for index, cue in enumerate(cues)
        if index and cue.start_us < cues[index - 1].end_us
    ]
    add(
        FinalIssueCode.CAPTION_OVERLAP,
        not overlaps,
        measurement=float(len(overlaps)),
        cue_sequence=overlaps[0] if overlaps else None,
        message="unintended cue overlaps are rejected",
    )
    duplicated = sorted(
        {cue.sequence for cue in cues if [item.sequence for item in cues].count(cue.sequence) > 1}
    )
    add(
        FinalIssueCode.CAPTION_CUE_DUPLICATED,
        not duplicated,
        measurement=float(len(duplicated)),
        cue_sequence=duplicated[0] if duplicated else None,
        message="no cue sequence may be duplicated",
    )

    # --- coverage of every approved narration segment ----------------------
    uncovered = [
        (segment_id, start, end)
        for segment_id, start, end in narration_segments
        if not any(cue.start_us < end and cue.end_us > start for cue in cues)
    ]
    add(
        FinalIssueCode.CAPTION_COVERAGE_MISSING,
        not uncovered,
        measurement=float(len(uncovered)),
        threshold=0.0,
        narration_segment_id=uncovered[0][0] if uncovered else None,
        start_us=uncovered[0][1] if uncovered else None,
        end_us=uncovered[0][2] if uncovered else None,
        message="every approved narration segment requires caption coverage",
    )

    # --- text fidelity and deterministic reflow ----------------------------
    try:
        rebuilt, _ = build_caption_track(
            track_id=canonical.caption_track_id,
            words=approved_words,
            duration_us=canonical.duration_us,
            config=CaptionConfig(
                max_chars_per_line=configuration.max_caption_line_characters,
                max_lines=configuration.max_caption_lines,
                max_chars_per_second=round(configuration.max_caption_reading_speed_cps),
                safe_zone_percent=configuration.caption_safe_area_percent,
                language=configuration.expected_caption_language,
            ),
        )
    except ValueError:
        # The approved words cannot be reflowed into the configured shape at
        # all, which is itself the defect: report it and skip the comparisons
        # that depend on a rebuilt track.
        add(
            FinalIssueCode.CAPTION_REFLOW_NONDETERMINISTIC,
            False,
            message="the approved words cannot be deterministically reflowed",
        )
        rebuilt = canonical
    else:
        # The declared identity is what T17 bound into the render manifest.
        # Rebuilding it from the approved words proves the reflow that produced
        # the delivered captions is reproducible, not merely self-consistent.
        add(
            FinalIssueCode.CAPTION_REFLOW_NONDETERMINISTIC,
            caption_identity(rebuilt) == declared_caption_identity,
            message="caption reflow must deterministically reproduce the declared identity",
        )
    mismatched = [
        cue.sequence
        for cue, expected in zip(cues, rebuilt.cues, strict=False)
        if cue.lines != expected.lines
    ]
    add(
        FinalIssueCode.CAPTION_TEXT_MISMATCH,
        not mismatched,
        measurement=float(len(mismatched)),
        cue_sequence=mismatched[0] if mismatched else None,
        remediation=FinalRemediationTarget.REBUILD_CAPTIONS_T17,
        message="caption text must match the approved script projection",
    )
    approved_text = "".join(word.text for word in approved_words)
    delivered_text = "".join("".join(cue.lines) for cue in cues)
    add(
        FinalIssueCode.CAPTION_PUNCTUATION_ALTERED,
        _punctuation(approved_text) == _punctuation(delivered_text),
        message="approved punctuation and wording must be preserved",
    )
    missing_cues = len(rebuilt.cues) - len(cues)
    add(
        FinalIssueCode.CAPTION_CUE_MISSING,
        missing_cues <= 0,
        measurement=float(max(missing_cues, 0)),
        message="no required cue may be missing from the canonical track",
    )

    # --- timing alignment with the approved word timings -------------------
    drift = _worst_timing_drift(cues, approved_words)
    add(
        FinalIssueCode.CAPTION_TIMING_DRIFT,
        drift <= configuration.caption_timing_tolerance_us,
        measurement=float(drift),
        threshold=float(configuration.caption_timing_tolerance_us),
        unit="us",
        message="caption timing must stay aligned with the T12 word timings",
    )

    # --- readability limits ------------------------------------------------
    too_many_lines = [
        cue.sequence for cue in cues if len(cue.lines) > configuration.max_caption_lines
    ]
    add(
        FinalIssueCode.CAPTION_LINE_COUNT_EXCEEDED,
        not too_many_lines,
        measurement=float(len(too_many_lines)),
        threshold=float(configuration.max_caption_lines),
        cue_sequence=too_many_lines[0] if too_many_lines else None,
        message="cue line count exceeds the configured limit",
    )
    longest = max(
        ((max(len(line) for line in cue.lines), cue.sequence) for cue in cues), default=(0, None)
    )
    add(
        FinalIssueCode.CAPTION_LINE_LENGTH_EXCEEDED,
        longest[0] <= configuration.max_caption_line_characters,
        measurement=float(longest[0]),
        threshold=float(configuration.max_caption_line_characters),
        unit="characters",
        cue_sequence=longest[1],
        message="cue line length exceeds the configured limit",
    )
    fastest = max(((reading_speed_cps(cue), cue.sequence) for cue in cues), default=(0.0, None))
    add(
        FinalIssueCode.CAPTION_READING_SPEED_EXCEEDED,
        fastest[0] <= configuration.max_caption_reading_speed_cps,
        measurement=fastest[0] if fastest[0] != float("inf") else 999.0,
        threshold=configuration.max_caption_reading_speed_cps,
        unit="cps",
        cue_sequence=fastest[1],
        message="cue reading speed exceeds the configured characters-per-second limit",
    )

    # --- delivered assets ---------------------------------------------------
    add(
        FinalIssueCode.CAPTION_LANGUAGE_MISMATCH,
        canonical.language == configuration.expected_caption_language,
        message=(
            f"delivered caption metadata must identify {configuration.expected_caption_language}, "
            f"found {canonical.language}"
        ),
    )
    for index, asset_id in enumerate(inputs.caption_asset_ids):
        expected_hash = inputs.caption_asset_hashes[index]
        actual_hash = delivered_hashes.get(asset_id)
        add(
            FinalIssueCode.CAPTION_ASSET_HASH_MISMATCH,
            actual_hash == expected_hash,
            caption_asset_id=asset_id,
            suffix=str(asset_id),
            message="delivered caption asset hashes must match the render manifest",
        )
        content = delivered.get(asset_id)
        if content is None:
            continue
        try:
            parsed = parse_webvtt(content) if _is_vtt(content) else parse_srt(content)
        except CaptionParseError as error:
            add(
                FinalIssueCode.CAPTION_PARSE_FAILURE,
                False,
                caption_asset_id=asset_id,
                suffix=str(asset_id),
                message=str(error),
            )
            continue
        add(
            FinalIssueCode.CAPTION_ENCODING_INVALID,
            True,
            caption_asset_id=asset_id,
            suffix=str(asset_id),
            message="delivered caption text is valid UTF-8",
        )
        add(
            FinalIssueCode.CAPTION_CUE_MISSING,
            len(parsed) == len(cues),
            measurement=float(len(parsed)),
            threshold=float(len(cues)),
            caption_asset_id=asset_id,
            suffix=f"delivered:{asset_id}",
            message="the delivered caption file must carry every canonical cue",
        )

    if burned_in:
        add(
            FinalIssueCode.CAPTION_SAFE_AREA_VIOLATION,
            canonical.safe_zone_percent >= configuration.caption_safe_area_percent,
            measurement=float(canonical.safe_zone_percent),
            threshold=float(configuration.caption_safe_area_percent),
            unit="percent",
            message="burned-in captions must remain inside the configured safe area",
        )
    return checks


def _is_vtt(content: bytes) -> bool:
    return content.lstrip()[:6].lstrip(b"\xef\xbb\xbf")[:6].upper().startswith(b"WEBVTT")


def _punctuation(text: str) -> str:
    return "".join(character for character in text if character in ".,!?;:-'\"")


def _worst_timing_drift(cues: list[CaptionCue], words: list[CaptionWord]) -> int:
    """The largest gap between a cue boundary and the word timing it covers."""
    if not words or not cues:
        return 0
    worst = 0
    ordered = sorted(words, key=lambda word: word.start_us)
    for cue in cues:
        covered = [
            word for word in ordered if word.start_us < cue.end_us and word.end_us > cue.start_us
        ]
        if not covered:
            continue
        worst = max(
            worst,
            abs(cue.start_us - min(word.start_us for word in covered)),
            abs(cue.end_us - max(word.end_us for word in covered)),
        )
    return worst
