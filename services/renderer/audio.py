"""Structured loudness measurements and deterministic audio policy."""

from __future__ import annotations

import json
import math
import re
from typing import Any


def parse_loudnorm_json(output: str) -> dict[str, float]:
    blocks = re.findall(r"\{[^{}]+\}", output, flags=re.DOTALL)
    if not blocks:
        raise ValueError("missing loudness measurement JSON")
    raw: dict[str, Any] = json.loads(blocks[-1])
    result = {}
    for source, target in (
        ("input_i", "integrated_lufs"),
        ("input_tp", "true_peak_dbtp"),
        ("input_lra", "loudness_range"),
        ("input_thresh", "threshold"),
        ("target_offset", "offset"),
    ):
        value = float(raw[source])
        if not math.isfinite(value):
            raise ValueError("non-finite loudness measurement")
        result[target] = value
    return result


def music_duck_filter() -> str:
    return "sidechaincompress=threshold=0.0316228:ratio=6:attack=20:release=400"
