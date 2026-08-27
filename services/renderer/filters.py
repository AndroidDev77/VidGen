"""Pure FFmpeg filter graph construction."""

from vidgen.contracts.render import RenderManifest, TransitionKind


def normalization_filter(manifest: RenderManifest, sequence: int) -> str:
    shot = manifest.shots[sequence]
    profile = manifest.video_profile
    scale = (
        f"scale={profile.width}:{profile.height}:force_original_aspect_ratio=decrease,pad={profile.width}:{profile.height}:(ow-iw)/2:(oh-ih)/2"
        if shot.normalization_policy == "scale_pad"
        else (
            f"scale={profile.width}:{profile.height}:"
            f"force_original_aspect_ratio=increase,crop={profile.width}:{profile.height}"
        )
    )
    return (
        f"trim=start={shot.trim_start_us // 1_000_000}.{shot.trim_start_us % 1_000_000:06d}:"
        f"end={shot.trim_end_us // 1_000_000}.{shot.trim_end_us % 1_000_000:06d},"
        f"setpts=PTS-STARTPTS,{scale},setsar=1,fps={profile.frame_rate},"
        f"format={profile.pixel_format}"
    )


def validate_transitions(manifest: RenderManifest) -> None:
    for shot in manifest.shots:
        for transition in (shot.transition_in, shot.transition_out):
            if transition.kind not in (TransitionKind.CUT, TransitionKind.CROSSFADE):
                raise ValueError("unsupported transition")
            if transition.kind == TransitionKind.CROSSFADE:
                raise ValueError("crossfade assembly requires the versioned xfade profile")
