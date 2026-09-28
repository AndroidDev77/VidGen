"""Deterministic motion-only prompt compiler.

Runway's image-to-video guidance shapes this template:

* The keyframe already establishes subject, composition, colour, lighting and
  style, so the text is almost entirely motion. Style is a short tag, never the
  project's full ``visual_style`` paragraph (see :func:`motion_style_tag`).
* Negative prompting is not supported; negative phrasing "may produce
  unpredictable or even opposite results". Continuity is therefore stated
  positively as what holds true for the whole shot. Shot-authored
  ``negative_motion_constraints`` are kept on the intent (and so in the
  persisted, hashed package) but are never rendered into the prompt text: free
  text such as "no camera shake" cannot be rewritten positively without guessing
  its meaning, and sending it verbatim is exactly what Runway warns against.
  T20 QA and T21 repair still read them from the storyboard shot.
* ``promptText`` is bounded per model. The budget comes from the selected
  model's capability profile and is adaptive: optional sections are trimmed,
  least important first, so a long shot loses a stylistic hint rather than
  failing before it reaches the provider.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable

from services.animation.providers import prompt_length
from vidgen.contracts.animation import MotionIntent, MotionPromptPackage

COMPILER_VERSION = "2.0.0"
TEMPLATE_VERSION = "runway-motion-v3"

#: Always rendered, always required: the positive form of the continuity intent
#: the previous template expressed as a list of things the model must never do.
CONTINUITY_STATEMENT = (
    "Continuity: one continuous shot; the same subjects throughout; "
    "wardrobe, colours and art style as shown."
)

#: A style tag is "a handful of words", bounded in both words and characters.
STYLE_TAG_MAX_WORDS = 6
STYLE_TAG_MAX_CHARACTERS = 60
_CLAUSE_BREAK = re.compile(r"[.;:,!?\n()\[\]]|\s[-\u2013\u2014]\s|[\u2013\u2014]")
_TRAILING_CONNECTORS = frozenset(
    {"a", "an", "and", "as", "at", "by", "for", "from", "in", "of", "on", "or", "the", "to"}
    | {"with", "where", "which", "that", "while"}
)


def motion_style_tag(visual_style: str) -> str:
    """A short, deterministic style tag derived from a project's ``visual_style``.

    The tag is the first clause of the style text, capped at
    ``STYLE_TAG_MAX_WORDS`` words and ``STYLE_TAG_MAX_CHARACTERS`` characters,
    with dangling connector words removed. The keyframe carries the rest of the
    style; this is only a brief reinforcing hint.

    This is the single place the motion-prompt style text is chosen. A future
    per-project motion style override resolves ahead of this derivation and
    passes through the compiler unchanged as ``MotionIntent.style_lock``.
    """
    normalized = " ".join(visual_style.split())
    clause = _CLAUSE_BREAK.split(normalized, maxsplit=1)[0].strip()
    words = clause.split()[:STYLE_TAG_MAX_WORDS]
    while words and len(" ".join(words)) > STYLE_TAG_MAX_CHARACTERS:
        words.pop()
    while words and words[-1].lower() in _TRAILING_CONNECTORS:
        words.pop()
    return " ".join(words)


def _list_section(label: str, items: list[str]) -> str:
    return f"{label}: " + "; ".join(items) + "." if items else ""


def compile_motion_prompt(intent: MotionIntent, *, limit: int) -> MotionPromptPackage:
    """Compile ``intent`` into a prompt of at most ``limit`` UTF-16 code units.

    ``limit`` is the selected model's ``VideoCapability.prompt_characters``.
    Sections are trimmed in a fixed order until the prompt fits: the style tag,
    then environment motion items (last first), then timing beats (last first),
    then the camera line. The action, the movement, the preserved invariants
    and the continuity statement are never trimmed; only when they alone
    exceed ``limit`` does compilation fail.
    """
    style = intent.style_lock.strip()
    environment = list(intent.environment_motion)
    timing = list(intent.timing_beats)
    camera = intent.camera_movement.strip()
    diagnostics: list[str] = []

    def render() -> str:
        sections = [
            f"Action: {intent.primary_action}.",
            f"Movement: {intent.start_pose} to {intent.expected_end_pose}.",
            f"Camera: {camera}." if camera else "",
            _list_section("Timing", timing),
            _list_section("Environment", environment),
            f"Style: {style}." if style else "",
            _list_section("Preserve", intent.continuity_invariants),
            CONTINUITY_STATEMENT,
        ]
        return " ".join(x for x in sections if x)

    def drop_style() -> None:
        nonlocal style
        style = ""

    def drop_camera() -> None:
        nonlocal camera
        camera = ""

    trims: list[tuple[str, Callable[[], bool], Callable[[], object]]] = [
        ("style", lambda: bool(style), drop_style),
        ("environment", lambda: bool(environment), environment.pop),
        ("timing", lambda: bool(timing), timing.pop),
        ("camera", lambda: bool(camera), drop_camera),
    ]
    prompt = render()
    for name, present, trim in trims:
        removed = 0
        while prompt_length(prompt) > limit and present():
            trim()
            removed += 1
            prompt = render()
        if removed:
            diagnostics.append(f"trimmed_{name}:{removed}")
    if prompt_length(prompt) > limit:
        raise ValueError(
            "invalid_motion_prompt: required motion and continuity "
            "constraints exceed provider limit"
        )
    if intent.negative_motion_constraints:
        diagnostics.append(
            f"negative_motion_constraints_not_rendered:{len(intent.negative_motion_constraints)}"
        )
    digest = hashlib.sha256(prompt.encode()).hexdigest()
    return MotionPromptPackage(
        intent=intent,
        compiler_version=COMPILER_VERSION,
        template_version=TEMPLATE_VERSION,
        prompt=prompt,
        prompt_hash=digest,
        diagnostics=diagnostics,
    )
