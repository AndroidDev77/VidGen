"""Deterministic approved-token reconciliation; no LLM is involved."""

from __future__ import annotations

import re
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path
from typing import Protocol

import httpx

from vidgen.contracts.narration import NarrationAlignment, NarrationWordTiming


@dataclass(frozen=True)
class RecognizedWord:
    word: str
    start_seconds: float
    end_seconds: float
    confidence: float = 1


def _token(word: str) -> str:
    return "".join(c for c in word.casefold() if c.isalnum() or c == "'")


def reconcile_alignment(
    approved_text: str, recognized: list[RecognizedWord], duration: float
) -> NarrationAlignment:
    if any(
        w.start_seconds < 0 or w.end_seconds <= w.start_seconds or w.end_seconds > duration
        for w in recognized
    ):
        raise ValueError("recognized timestamps are reversed or outside measured duration")
    if any(b.start_seconds < a.end_seconds for a, b in pairwise(recognized)):
        raise ValueError("recognized timestamp reversal")
    approved = re.findall(
        r"(\w+(?:['\N{RIGHT SINGLE QUOTATION MARK}]\w+)*)([^\w\s]*)",
        approved_text,
        re.UNICODE,
    )
    words = [word for word, _punctuation in approved]
    punctuation = [suffix for _word, suffix in approved]
    rec = [_token(x.word) for x in recognized]
    # Bounded Wagner-Fischer alignment with deterministic diagonal/delete/insert tie order.
    if len(words) * max(1, len(rec)) > 100_000:
        raise ValueError("alignment input exceeds bounded matrix")
    a = [_token(x) for x in words]
    dp = [[0] * (len(rec) + 1) for _ in range(len(a) + 1)]
    for i in range(len(a) + 1):
        dp[i][0] = i
    for j in range(len(rec) + 1):
        dp[0][j] = j
    for i in range(1, len(a) + 1):
        for j in range(1, len(rec) + 1):
            dp[i][j] = min(
                dp[i - 1][j - 1] + (a[i - 1] != rec[j - 1]), dp[i - 1][j] + 1, dp[i][j - 1] + 1
            )
    i, j = len(a), len(rec)
    pairs = []
    omissions = []
    insertions = []
    substitutions = []
    while i or j:
        if i and j and dp[i][j] == dp[i - 1][j - 1] + (a[i - 1] != rec[j - 1]):
            pairs.append((i - 1, j - 1))
            if a[i - 1] != rec[j - 1]:
                substitutions.append(f"{words[i - 1]}->{recognized[j - 1].word}")
            i -= 1
            j -= 1
        elif i and dp[i][j] == dp[i - 1][j] + 1:
            omissions.append(words[i - 1])
            i -= 1
        else:
            insertions.append(recognized[j - 1].word)
            j -= 1
    timings = [
        NarrationWordTiming(
            word_index=ai,
            word=words[ai],
            comparison_token=a[ai],
            punctuation=punctuation[ai],
            start_seconds=recognized[rj].start_seconds,
            end_seconds=recognized[rj].end_seconds,
            confidence=recognized[rj].confidence,
        )
        for ai, rj in reversed(pairs)
    ]
    exact = sum(a[ai] == rec[rj] for ai, rj in pairs)
    return NarrationAlignment(
        timings=timings,
        coverage=exact / max(1, len(a)),
        insertions=list(reversed(insertions)),
        omissions=list(reversed(omissions)),
        substitutions=list(reversed(substitutions)),
    )


class FakeAligner:
    def align(
        self,
        text: str,
        duration: float,
        audio_path: Path | None = None,
        *,
        idempotency_key: str | None = None,
    ) -> NarrationAlignment:
        words = re.findall(r"\w+(?:['\N{RIGHT SINGLE QUOTATION MARK}]\w+)*", text)
        step = duration / max(1, len(words))
        return reconcile_alignment(
            text,
            [
                RecognizedWord(w, i * step, min(duration, (i + 1) * step), 1)
                for i, w in enumerate(words)
            ],
            duration,
        )


class NarrationAligner(Protocol):
    def align(
        self,
        text: str,
        duration: float,
        audio_path: Path | None = None,
        *,
        idempotency_key: str | None = None,
    ) -> NarrationAlignment: ...


class OpenAIWhisperAligner:
    """Whisper timestamp adapter; deterministic reconciliation remains local."""

    def __init__(self, api_key: str, model: str = "whisper-1") -> None:
        if not api_key:
            raise ValueError("OpenAI API key is required for alignment")
        self.api_key, self.model = api_key, model

    def align(
        self,
        text: str,
        duration: float,
        audio_path: Path | None = None,
        *,
        idempotency_key: str | None = None,
    ) -> NarrationAlignment:
        if audio_path is None:
            raise ValueError("production alignment requires an audio path")
        with audio_path.open("rb") as audio, httpx.Client(timeout=120) as client:
            response = client.post(
                "https://api.openai.com/v1/audio/transcriptions",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    **({"Idempotency-Key": idempotency_key} if idempotency_key else {}),
                },
                data={
                    "model": self.model,
                    "response_format": "verbose_json",
                    "prompt": text,
                    "timestamp_granularities[]": "word",
                },
                files={"file": (audio_path.name, audio, "audio/wav")},
            )
            response.raise_for_status()
        payload = response.json()
        words = [
            RecognizedWord(
                word=str(item["word"]),
                start_seconds=float(item["start"]),
                end_seconds=float(item["end"]),
                confidence=float(item.get("confidence", 1)),
            )
            for item in payload.get("words", [])
        ]
        if not words:
            raise ValueError("alignment provider returned no timestamped words")
        return reconcile_alignment(text, words, duration)
