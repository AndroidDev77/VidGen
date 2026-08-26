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
        frames = wav.readframes(wav.getnframes())
    samples = [
        int.from_bytes(frames[i : i + 2], "little", signed=True) for i in range(0, len(frames), 2)
    ]
    clipping = sum(abs(x) >= 32760 for x in samples) / max(1, len(samples))
    rate = 48000
    active = [i for i, x in enumerate(samples) if abs(x) > 128]
    leading = active[0] / rate if active else duration
    trailing = (len(samples) - 1 - active[-1]) / rate if active else duration
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
