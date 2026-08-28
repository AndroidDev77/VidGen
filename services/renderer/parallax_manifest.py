"""Deterministic planning for the T21 2.5D parallax fallback.

Everything in this module is a pure function of stable inputs: the shot's
identity, the approved still it animates, the canonical duration and the render
geometry. The same shot always yields the same render identity, the same layer
transforms and byte-for-byte the same FFmpeg filter graph, so a fallback render
is reproducible and a repeat request is free.

Eligibility is decided here too, and it is deliberately conservative. A 2.5D
render moves a still image; it cannot invent motion. A shot that requires
essential character interaction or a mandatory physical action is rejected, and
so is a shot whose only available still already carries a T20 hard identity or
location failure - using parallax there would hide an invalid source image
behind a camera move rather than fix the shot.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID, uuid5

from vidgen.contracts.repair import (
    ParallaxEasing,
    ParallaxEligibility,
    ParallaxLayer,
    ParallaxLayerRole,
    ParallaxRenderPlan,
)
from vidgen.contracts.visual_qa import VisualQARepairCode

RENDERER_VERSION = "parallax-renderer/1.0"
ELIGIBILITY_POLICY_VERSION = "parallax-eligibility/1.0"
PLAN_NAMESPACE = UUID("6b2a1f9c-6d3f-5f2a-9a71-0f4c8b2d7e10")

#: Motion a controlled still-image move cannot truthfully represent. A shot
#: whose storyboard action needs one of these is never faked with a camera move.
MANDATORY_MOTION_TERMS: tuple[str, ...] = (
    "punch",
    "throw",
    "throws",
    "catch",
    "catches",
    "hand over",
    "hands over",
    "handshake",
    "shakes hands",
    "hug",
    "hugs",
    "kiss",
    "kisses",
    "fight",
    "fights",
    "chase",
    "chases",
    "run",
    "runs",
    "jump",
    "jumps",
    "fall",
    "falls",
    "dance",
    "dances",
    "pour",
    "pours",
    "open the door",
    "opens the door",
    "walks in",
    "walks out",
    "collide",
    "collides",
)

#: T20 codes that condemn the source still itself. Animating an image with one
#: of these would conceal a wrong character, a wrong place, or a broken face.
DISQUALIFYING_KEYFRAME_CODES: frozenset[VisualQARepairCode] = frozenset(
    {
        VisualQARepairCode.WRONG_CHARACTER_IDENTITY,
        VisualQARepairCode.MISSING_PRIMARY_CHARACTER,
        VisualQARepairCode.WRONG_CHARACTER_COUNT,
        VisualQARepairCode.EXTRA_CHARACTER,
        VisualQARepairCode.WRONG_LOCATION,
        VisualQARepairCode.WRONG_LOCATION_STATE,
        VisualQARepairCode.FACE_BREAKAGE,
        VisualQARepairCode.ANATOMY_BREAKAGE,
        VisualQARepairCode.UNINTENDED_TEXT,
        VisualQARepairCode.DECODE_FAILURE,
    }
)


@dataclass(frozen=True, slots=True)
class ParallaxSource:
    """One approved still, with the optional depth information we may have."""

    asset_id: UUID
    sha256: str
    mask_asset_id: UUID | None = None
    depth_map_asset_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class ParallaxRequest:
    """Everything the deterministic planner is allowed to look at."""

    repair_attempt_id: UUID
    shot_id: UUID
    canonical_shot_hash: str
    source: ParallaxSource
    width: int
    height: int
    frame_rate: int
    exact_duration_us: int
    #: The storyboard action clause, used only to decide eligibility.
    required_action: str = ""
    secondary_action: str = ""
    camera_movement: str = ""
    present_character_count: int = 1
    #: Repair codes carried by the *keyframe* QA result for this shot.
    keyframe_repair_codes: tuple[VisualQARepairCode, ...] = ()
    keyframe_hard_failure: bool = False


def decide_eligibility(request: ParallaxRequest) -> ParallaxEligibility:
    """Decide deterministically whether a shot may fall back to 2.5D parallax."""
    reasons: list[str] = []
    blocked = False
    if request.keyframe_hard_failure:
        blocked = True
        reasons.append(
            "the approved still already carries a T20 hard failure; a camera move would "
            "conceal an invalid source image"
        )
    disqualifying = sorted(
        code.value for code in request.keyframe_repair_codes if code in DISQUALIFYING_KEYFRAME_CODES
    )
    if disqualifying:
        blocked = True
        reasons.append(
            "the source keyframe is condemned by T20 repair codes: " + ", ".join(disqualifying)
        )
    mandatory = _mandatory_motion(request)
    if mandatory:
        blocked = True
        reasons.append(
            "the shot requires physical action a still-image move cannot represent: "
            + ", ".join(mandatory)
        )
    if request.present_character_count >= 2 and mandatory:
        reasons.append("multi-character interaction cannot be staged from one still")
    if request.exact_duration_us <= 0:
        blocked = True
        reasons.append("the shot has no canonical duration to render")
    if not blocked:
        reasons.append(
            "the shot's purpose is conveyed by framing and emphasis, which controlled "
            "still-image motion represents truthfully"
        )
    return ParallaxEligibility(
        eligible=not blocked,
        reasons=reasons[:16],
        source_keyframe_asset_id=request.source.asset_id if not blocked else None,
        policy_version=ELIGIBILITY_POLICY_VERSION,
    )


def _mandatory_motion(request: ParallaxRequest) -> list[str]:
    haystack = f"{request.required_action} {request.secondary_action}".lower()
    return sorted({term for term in MANDATORY_MOTION_TERMS if term in haystack})


def render_identity(request: ParallaxRequest) -> str:
    """A stable identity over exactly the material inputs of one render."""
    material = {
        "renderer_version": RENDERER_VERSION,
        "shot_id": str(request.shot_id),
        "canonical_shot_hash": request.canonical_shot_hash,
        "source_asset_id": str(request.source.asset_id),
        "source_sha256": request.source.sha256,
        "mask_asset_id": _optional(request.source.mask_asset_id),
        "depth_map_asset_id": _optional(request.source.depth_map_asset_id),
        "width": request.width,
        "height": request.height,
        "frame_rate": request.frame_rate,
        "exact_duration_us": request.exact_duration_us,
        "camera_movement": request.camera_movement,
    }
    payload = json.dumps(material, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def _optional(value: UUID | None) -> str | None:
    return str(value) if value is not None else None


def build_plan(request: ParallaxRequest) -> ParallaxRenderPlan:
    """Derive the complete, deterministic render plan for one shot.

    Layer transforms come from the render identity rather than a random source,
    so the same shot always produces the same camera move, and two different
    shots do not accidentally produce the same one.
    """
    identity = render_identity(request)
    layers = [_background(request, identity)]
    if request.source.mask_asset_id is not None or request.source.depth_map_asset_id is not None:
        # A real depth separation is the only honest reason to move a second
        # plane; without one the render stays a single-plane camera move.
        layers.append(_foreground(request, identity))
    return ParallaxRenderPlan(
        plan_id=uuid5(PLAN_NAMESPACE, identity),
        renderer_version=RENDERER_VERSION,
        repair_attempt_id=request.repair_attempt_id,
        shot_id=request.shot_id,
        render_identity=identity,
        layers=layers,
        width=request.width,
        height=request.height,
        frame_rate=request.frame_rate,
        exact_duration_us=request.exact_duration_us,
    )


def _background(request: ParallaxRequest, identity: str) -> ParallaxLayer:
    """The background camera move.

    Two choices here are deliberate rather than aesthetic. The move is large -
    roughly a 40% push-in with a third-of-a-frame pan - and its easing is
    linear. A gentle, eased Ken Burns move looks nicer but is nearly static at
    both ends, and T20's deterministic freeze check measures exactly that: it
    counts frames whose luma barely changes. A fallback that has to pass T20
    must actually move, at a constant rate, for the whole shot.
    """
    direction = _signed(identity, 0)
    depth = _unit(identity, 2)
    start_scale = 1.04 + 0.04 * depth
    end_scale = start_scale + 0.40 + 0.10 * depth
    pan = 0.28 + 0.10 * _unit(identity, 4)
    drift = 0.04 * _unit(identity, 6) * _signed(identity, 8)
    return ParallaxLayer(
        role=ParallaxLayerRole.BACKGROUND,
        source_asset_id=request.source.asset_id,
        source_sha256=request.source.sha256,
        start_scale=round(start_scale, 6),
        end_scale=round(end_scale, 6),
        start_offset_x=round(-pan * direction, 6),
        start_offset_y=round(-drift, 6),
        end_offset_x=round(pan * direction, 6),
        end_offset_y=round(drift, 6),
        easing=ParallaxEasing.LINEAR,
        mask_asset_id=None,
        depth_map_asset_id=request.source.depth_map_asset_id,
    )


def _foreground(request: ParallaxRequest, identity: str) -> ParallaxLayer:
    background = _background(request, identity)
    # The foreground plane always moves further than the background; that
    # differential is the entire perceptual content of a 2.5D render.
    factor = 1.25
    return ParallaxLayer(
        role=ParallaxLayerRole.FOREGROUND,
        source_asset_id=request.source.asset_id,
        source_sha256=request.source.sha256,
        start_scale=round(background.start_scale * 1.08, 6),
        end_scale=round(background.end_scale * 1.08, 6),
        start_offset_x=round(background.start_offset_x * factor, 6),
        start_offset_y=round(background.start_offset_y * factor, 6),
        end_offset_x=round(background.end_offset_x * factor, 6),
        end_offset_y=round(background.end_offset_y * factor, 6),
        easing=ParallaxEasing.LINEAR,
        mask_asset_id=request.source.mask_asset_id,
        depth_map_asset_id=request.source.depth_map_asset_id,
    )


def _unit(identity: str, offset: int) -> float:
    """A stable value in [0, 1) derived from two hex digits of the identity."""
    return int(identity[offset : offset + 2], 16) / 256.0


def _signed(identity: str, offset: int) -> float:
    """A stable value of -1 or 1 derived from the identity."""
    return 1.0 if int(identity[offset : offset + 2], 16) % 2 == 0 else -1.0


def frame_count(exact_duration_us: int, frame_rate: int) -> int:
    """Frames rendered before the deterministic trim pins the exact duration."""
    if exact_duration_us <= 0 or frame_rate <= 0:
        raise ValueError("a parallax render needs a positive duration and frame rate")
    exact = exact_duration_us * frame_rate / 1_000_000
    return max(1, int(exact) + (1 if exact % 1 else 0))


def easing_expression(easing: ParallaxEasing, frames: int) -> str:
    """The FFmpeg expression for normalized, eased progress across the shot.

    Progress is a function of the output frame index rather than presentation
    time, so a render is identical frame by frame on any machine.
    """
    span = max(1, frames - 1)
    progress = f"min(1,on/{span})"
    if easing is ParallaxEasing.LINEAR:
        return progress
    # Smoothstep: continuous, symmetric and cheap to evaluate.
    return f"({progress})*({progress})*(3-2*({progress}))"


def layer_filter(layer: ParallaxLayer, plan: ParallaxRenderPlan, *, index: int, frames: int) -> str:
    """The deterministic ``zoompan`` chain for exactly one layer."""
    eased = easing_expression(layer.easing, frames)
    zoom = f"{layer.start_scale:.6f}+({layer.end_scale:.6f}-{layer.start_scale:.6f})*({eased})"
    # zoompan pans in supersampled input coordinates; normalizing the offsets to
    # the pannable range keeps a move inside the frame at every zoom level.
    x_position = (
        f"(iw-iw/zoom)*(0.5+({layer.start_offset_x:.6f}"
        f"+({layer.end_offset_x:.6f}-{layer.start_offset_x:.6f})*({eased})))"
    )
    y_position = (
        f"(ih-ih/zoom)*(0.5+({layer.start_offset_y:.6f}"
        f"+({layer.end_offset_y:.6f}-{layer.start_offset_y:.6f})*({eased})))"
    )
    # Supersampling before zoompan removes its well-known integer-step jitter.
    return (
        f"[{index}:v]scale={plan.width * 4}:{plan.height * 4}:flags=bicubic,"
        f"setsar=1,zoompan=z='{zoom}':x='{x_position}':y='{y_position}':"
        f"d=1:s={plan.width}x{plan.height}:fps={plan.frame_rate}[layer{index}]"
    )


def filter_graph(plan: ParallaxRenderPlan, *, frames: int) -> str:
    """The complete deterministic filter graph for a one- or two-plane render."""
    chains = [
        layer_filter(layer, plan, index=index, frames=frames)
        for index, layer in enumerate(plan.layers)
    ]
    if len(plan.layers) == 1:
        chains.append(f"[layer0]format={plan.pixel_format}[out]")
        return ";".join(chains)
    foreground = plan.layers[1]
    mask_index = len(plan.layers)
    if foreground.mask_asset_id is not None:
        chains.append(
            f"[{mask_index}:v]scale={plan.width}:{plan.height}:flags=bicubic,format=gray[mask]"
        )
        chains.append("[layer1][mask]alphamerge[fg]")
    else:
        chains.append(f"[layer1]format=yuva420p,colorchannelmixer=aa={foreground.opacity:.6f}[fg]")
    chains.append(f"[layer0][fg]overlay=0:0:format=auto,format={plan.pixel_format}[out]")
    return ";".join(chains)


def input_arguments(plan: ParallaxRenderPlan, sources: Sequence[str], *, frames: int) -> list[str]:
    """The looped still-image inputs, one per layer plus an optional mask."""
    seconds = frames / plan.frame_rate
    arguments: list[str] = []
    for source in sources:
        arguments.extend(
            [
                "-loop",
                "1",
                "-framerate",
                str(plan.frame_rate),
                "-t",
                f"{seconds:.6f}",
                "-i",
                source,
            ]
        )
    return arguments


__all__ = [
    "DISQUALIFYING_KEYFRAME_CODES",
    "ELIGIBILITY_POLICY_VERSION",
    "MANDATORY_MOTION_TERMS",
    "RENDERER_VERSION",
    "ParallaxRequest",
    "ParallaxSource",
    "build_plan",
    "decide_eligibility",
    "easing_expression",
    "filter_graph",
    "frame_count",
    "input_arguments",
    "layer_filter",
    "render_identity",
]
