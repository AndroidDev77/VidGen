"""Deterministic narration quality gates."""

from __future__ import annotations

import math
import wave
from collections.abc import Iterable
from pathlib import Path

from vidgen.contracts.narration import (
    NarrationAlignment,
    NarrationQualityDiagnostic,
    NarrationQualityReport,
    NarrationQualityThresholds,
)

#: The pipeline's gate configuration is the resolved contract itself: the API,
#: the worker and the identity material all speak the same strict values.
QualityThresholds = NarrationQualityThresholds

DEFAULT_THRESHOLDS = QualityThresholds()


def validate_quality(
    path: Path,
    text: str,
    duration: float,
    alignment: NarrationAlignment,
    t: QualityThresholds = DEFAULT_THRESHOLDS,
    *,
    warn_only_codes: Iterable[str] | None = None,
) -> NarrationQualityReport:
    """Measure one normalized take against the gate.

    ``warn_only_codes`` names the codes this deployment or project tolerates:
    they are still measured and recorded, at ``warning`` severity, but do not
    make the report invalid. Unset, the thresholds' own set applies.
    """
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
    warn_only = frozenset(t.warn_only_codes if warn_only_codes is None else warn_only_codes)

    def check(code: str, bad: bool, value: float, limit: float) -> None:
        if bad:
            # A warn-only code is still measured and recorded on the report so
            # the retry guidance and the operator can see it; it just does not
            # fail the attempt and buy another provider call.
            diagnostics.append(
                NarrationQualityDiagnostic(
                    code=code,
                    severity="warning" if code in warn_only else "error",
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
        valid=not any(item.severity == "error" for item in diagnostics),
        diagnostics=diagnostics,
        clipping_ratio=clipping,
        leading_silence_seconds=leading,
        trailing_silence_seconds=trailing,
        speaking_rate_wpm=wpm,
        alignment_coverage=alignment.coverage,
    )
