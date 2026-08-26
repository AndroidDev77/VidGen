"""Deterministic narration quality gates."""

from __future__ import annotations

import math
import wave
from dataclasses import dataclass
from pathlib import Path

from vidgen.contracts.narration import (
    NarrationAlignment,
    NarrationQualityDiagnostic,
    NarrationQualityReport,
)


@dataclass(frozen=True)
class QualityThresholds:
    min_wpm: float = 80
    max_wpm: float = 220
    min_alignment_coverage: float = 0.95
    max_clipping_ratio: float = 0.001
    max_leading_silence: float = 0.5
    max_trailing_silence: float = 0.7
    max_internal_silence: float = 1.5


DEFAULT_THRESHOLDS = QualityThresholds()


def validate_quality(
    path: Path,
    text: str,
    duration: float,
    alignment: NarrationAlignment,
    t: QualityThresholds = DEFAULT_THRESHOLDS,
) -> NarrationQualityReport:
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("duration must be finite and positive")
    with wave.open(str(path), "rb") as wav:
        if wav.getsampwidth() != 2:
            raise ValueError("canonical audio must be 16-bit PCM")
        rate = wav.getframerate()
        total = clipped = 0
        first_active: int | None = None
        last_active: int | None = None
        silence_run = longest_silence = 0
        while frames := wav.readframes(48_000):
            for offset in range(0, len(frames), 2):
                sample = int.from_bytes(frames[offset : offset + 2], "little", signed=True)
                clipped += abs(sample) >= 32760
                if abs(sample) > 128:
                    first_active = total if first_active is None else first_active
                    last_active = total
                    longest_silence = max(longest_silence, silence_run)
                    silence_run = 0
                else:
                    silence_run += 1
                total += 1
    clipping = clipped / max(1, total)
    leading = first_active / rate if first_active is not None else duration
    trailing = (total - 1 - last_active) / rate if last_active is not None else duration
    internal_silence = longest_silence / rate
    wpm = len(text.split()) / duration * 60
    diagnostics = []

    def check(code: str, bad: bool, value: float, limit: float) -> None:
        if bad:
            diagnostics.append(
                NarrationQualityDiagnostic(
                    code=code,
                    severity="error",
                    message=code.replace("_", " "),
                    measured_value=value,
                    threshold=limit,
                )
            )

    check("clipping", clipping > t.max_clipping_ratio, clipping, t.max_clipping_ratio)
    check("leading_silence", leading > t.max_leading_silence, leading, t.max_leading_silence)
    check("trailing_silence", trailing > t.max_trailing_silence, trailing, t.max_trailing_silence)
    check(
        "internal_silence",
        internal_silence > t.max_internal_silence,
        internal_silence,
        t.max_internal_silence,
    )
    check("speaking_rate", wpm < t.min_wpm or wpm > t.max_wpm, wpm, t.max_wpm)
    check(
        "alignment_coverage",
        alignment.coverage < t.min_alignment_coverage,
        alignment.coverage,
        t.min_alignment_coverage,
    )
    return NarrationQualityReport(
        valid=not diagnostics,
        diagnostics=diagnostics,
        clipping_ratio=clipping,
        leading_silence_seconds=leading,
        trailing_silence_seconds=trailing,
        speaking_rate_wpm=wpm,
        alignment_coverage=alignment.coverage,
    )
