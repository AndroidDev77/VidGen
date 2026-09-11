"""How each stage's durable statuses read as progress.

One ``StageSpec`` per stage, in pipeline order. The status literals are the
ones the pipelines write to their run rows or to ``project.status``; the
loaders in ``services.progress.loaders`` decide which of the two to feed in
and gather the counts. Synthetic phase keys (``"generating"``, ``"reviewing"``,
``"complete"``) are used where a stage records no status of its own and the
loader has to derive one from its rows.
"""

from __future__ import annotations

from services.progress.engine import PhaseSpec, ProgressState, StageSpec

_Q = ProgressState.QUEUED
_R = ProgressState.RUNNING
_W = ProgressState.WAITING
_C = ProgressState.COMPLETED
_F = ProgressState.FAILED


MEDIA_PROCESSING = StageSpec(
    stage="media_processing",
    label="Media processing",
    unit="scene",
    count_label="scenes with frames",
    status_phases={
        "probing": "probing",
        "extracting_audio": "extracting_audio",
        "detecting_scenes": "detecting_scenes",
        "extracting_frames": "extracting_frames",
        "media_ready": "complete",
        "media_failed": "failed",
    },
    phases={
        "queued": PhaseSpec(_Q, 0, "Preparing media processing"),
        "probing": PhaseSpec(_R, 5, "Probing the source video"),
        "extracting_audio": PhaseSpec(_R, 20, "Extracting the audio track"),
        "detecting_scenes": PhaseSpec(_R, 40, "Detecting scene cuts"),
        # Frames are committed together once every scene has one, so this
        # phase reads 0 of N until it reads N of N; the bar still moves on.
        "extracting_frames": PhaseSpec(
            _R, 60, "Extracting frames for scene {next} of {total}", end=95, counted=True
        ),
        "complete": PhaseSpec(_C, 100, "Media ready"),
        "failed": PhaseSpec(_F, 0, "Media processing failed"),
    },
)

TRANSCRIPTION = StageSpec(
    stage="transcript_acquisition",
    label="Transcription",
    unit="chunk",
    count_label="chunks transcribed",
    status_phases={
        "subtitle_discovery": "subtitle_discovery",
        "subtitle_validating": "subtitle_validating",
        "subtitle_downloading": "subtitle_downloading",
        "subtitle_searching": "subtitle_searching",
        "subtitle_importing": "subtitle_importing",
        "subtitle_imported": "complete",
        # The subtitle pipeline hands over to audio transcription, whose own
        # run row takes over the moment it exists.
        "subtitle_unavailable": "subtitle_unavailable",
        "subtitle_failed": "failed",
        "transcription_chunking": "chunking",
        "transcribing": "transcribing",
        "diarizing": "diarizing",
        "transcript_merging": "merging",
        "transcribed": "complete",
        "transcription_failed": "failed",
    },
    phases={
        "queued": PhaseSpec(_Q, 0, "Preparing transcription"),
        "subtitle_discovery": PhaseSpec(_R, 5, "Looking for subtitles"),
        "subtitle_validating": PhaseSpec(_R, 15, "Checking subtitle candidates"),
        "subtitle_searching": PhaseSpec(_R, 20, "Searching for subtitles"),
        "subtitle_downloading": PhaseSpec(_R, 25, "Downloading subtitles"),
        "subtitle_importing": PhaseSpec(_R, 85, "Importing subtitles"),
        "subtitle_unavailable": PhaseSpec(_R, 10, "No subtitles found; transcribing the audio"),
        "chunking": PhaseSpec(_R, 10, "Splitting the audio into chunks"),
        "transcribing": PhaseSpec(
            _R, 10, "Transcribing chunk {next} of {total}", end=80, counted=True
        ),
        "diarizing": PhaseSpec(
            _R, 10, "Identifying speakers in chunk {next} of {total}", end=80, counted=True
        ),
        "merging": PhaseSpec(_R, 90, "Merging the transcript"),
        "complete": PhaseSpec(_C, 100, "Transcript ready"),
        "failed": PhaseSpec(_F, 0, "Transcription failed"),
    },
)

EVIDENCE = StageSpec(
    stage="evidence",
    label="Evidence package",
    unit="scene",
    count_label="scenes with evidence",
    status_phases={"pending": "pending", "complete": "complete"},
    phases={
        "queued": PhaseSpec(_Q, 0, "Preparing the evidence package"),
        "pending": PhaseSpec(_Q, 0, "Waiting to assemble evidence for {total} {units}"),
        "complete": PhaseSpec(_C, 100, "Evidence package ready for {total} {units}"),
    },
)

EPISODE_ANALYSIS = StageSpec(
    stage="episode_analysis",
    label="Episode analysis",
    unit="scene",
    count_label="scenes analyzed",
    status_phases={
        "episode_analysis_pending": "queued",
        "episode_scene_mapping": "scene_analysis",
        "episode_global_reduction": "building_model",
        "episode_analysis_validating": "validating",
        "episode_analyzed": "completed",
        "episode_analysis_failed": "failed",
    },
    phases={
        "queued": PhaseSpec(_Q, 0, "Preparing episode analysis"),
        # Scene analysis is the long, per-scene part of the run and owns most
        # of the bar; the single reduce and validate calls take the rest.
        "scene_analysis": PhaseSpec(
            _R, 0, "Analyzing scene {next} of {total}", end=80, counted=True
        ),
        "building_model": PhaseSpec(_R, 85, "Building episode model"),
        "validating": PhaseSpec(_R, 95, "Validating episode analysis"),
        "completed": PhaseSpec(_C, 100, "Episode analysis complete"),
        "failed": PhaseSpec(_F, 0, "Episode analysis failed"),
    },
)

#: One draft plus the bounded editorial revisions the pipeline allows.
#: The historical default; a run reports its own ``max_editing_passes``.
SCRIPT_EDITORIAL_PASSES = 3

SCRIPT_GENERATION = StageSpec(
    stage="script_generation",
    label="Script generation",
    unit="pass",
    count_label="editorial passes",
    status_phases={
        "plot_compression_pending": "queued",
        "compressing_plot": "compressing_plot",
        "plot_compressed": "plot_compressed",
        "comedy_writing": "writing",
        "script_validating": "validating",
        "comedy_editing": "editing",
        "script_approved": "approved",
        "completed": "approved",
        "script_review_required": "review_required",
        "script_generation_failed": "failed",
    },
    phases={
        "queued": PhaseSpec(_Q, 0, "Preparing script generation"),
        "compressing_plot": PhaseSpec(_R, 15, "Compressing the plot"),
        "plot_compressed": PhaseSpec(_R, 30, "Plot compressed"),
        "writing": PhaseSpec(_R, 45, "Writing the recap script"),
        "validating": PhaseSpec(_R, 65, "Validating the script"),
        "editing": PhaseSpec(_R, 70, "Editing pass {next} of {total}", end=95, counted=True),
        "approved": PhaseSpec(_C, 100, "Script approved"),
        "review_required": PhaseSpec(_W, 100, "Script needs your review"),
        "failed": PhaseSpec(_F, 0, "Script generation failed"),
    },
)

NARRATION = StageSpec(
    stage="narration",
    label="Narration",
    unit="segment",
    count_label="segments narrated",
    status_phases={
        "narration_queued": "queued",
        "narration_generating": "generating",
        "narration_aligning": "aligning",
        "narration_validating": "validating",
        "narration_previewing": "previewing",
        "narration_complete": "complete",
        "narration_failed": "failed",
    },
    phases={
        "queued": PhaseSpec(_Q, 0, "Preparing narration"),
        "generating": PhaseSpec(
            _R, 5, "Generating narration {next} of {total}", end=85, counted=True
        ),
        "aligning": PhaseSpec(_R, 5, "Aligning narration {next} of {total}", end=85, counted=True),
        "validating": PhaseSpec(
            _R, 5, "Checking narration {next} of {total}", end=85, counted=True
        ),
        "previewing": PhaseSpec(_R, 90, "Building the narration preview"),
        "complete": PhaseSpec(_C, 100, "Narration complete"),
        "failed": PhaseSpec(_F, 0, "Narration failed"),
    },
)

STORYBOARD = StageSpec(
    stage="storyboard",
    label="Storyboard",
    unit="segment",
    count_label="segments directed",
    status_phases={
        "storyboard_queued": "queued",
        "storyboard_directing": "directing",
        "storyboard_repairing": "repairing",
        "storyboard_retiming": "retiming",
        "storyboard_validating": "validating",
        "storyboard_complete": "complete",
        "storyboard_failed": "failed",
    },
    phases={
        "queued": PhaseSpec(_Q, 0, "Preparing the storyboard"),
        "directing": PhaseSpec(_R, 0, "Directing segment {next} of {total}", end=85, counted=True),
        "repairing": PhaseSpec(_R, 0, "Repairing segment {next} of {total}", end=85, counted=True),
        "retiming": PhaseSpec(_R, 0, "Retiming segment {next} of {total}", end=85, counted=True),
        "validating": PhaseSpec(_R, 90, "Validating the storyboard"),
        "complete": PhaseSpec(_C, 100, "Storyboard complete"),
        "failed": PhaseSpec(_F, 0, "Storyboard failed"),
    },
)

REFERENCES = StageSpec(
    stage="references",
    label="Reference sheets",
    unit="reference sheet",
    count_label="reference sheets generated",
    status_phases={
        "generating": "generating",
        "awaiting_approval": "awaiting_approval",
        "binding": "binding",
        "complete": "complete",
    },
    phases={
        "queued": PhaseSpec(_Q, 0, "Preparing reference sheets"),
        "generating": PhaseSpec(
            _R, 0, "Generating reference sheet {next} of {total}", end=70, counted=True
        ),
        "awaiting_approval": PhaseSpec(
            _W,
            80,
            "{done} of {total} reference sheets approved; the rest need your approval",
            count_label="reference sheets approved",
        ),
        "binding": PhaseSpec(
            _R, 90, "Binding references to shots", count_label="reference sheets approved"
        ),
        "complete": PhaseSpec(
            _C,
            100,
            "References approved and bound to every shot",
            count_label="reference sheets approved",
        ),
    },
)

SHOT_GENERATION = StageSpec(
    stage="shot_generation",
    label="Shot generation",
    unit="shot",
    count_label="shots animated",
    status_phases={
        "shot_generation_queued": "queued",
        "keyframes": "keyframes",
        "animating": "animating",
        "reviewing": "reviewing",
        "shot_generation_complete": "complete",
        "shot_generation_partial": "partial",
        "shot_generation_failed": "failed",
        "shot_generation_cancelled": "cancelled",
    },
    # Every counted phase spans the whole bar: the loader feeds a share that
    # blends keyframes, animation and review so the bar never jumps back when
    # one shot's animation starts while another's keyframes are still coming.
    phases={
        "queued": PhaseSpec(_Q, 0, "Preparing shot generation"),
        "keyframes": PhaseSpec(
            _R,
            0,
            "Generating keyframes for shot {next} of {total}",
            end=100,
            counted=True,
            count_label="shots with keyframes",
        ),
        "animating": PhaseSpec(_R, 0, "Animating shot {next} of {total}", end=100, counted=True),
        "reviewing": PhaseSpec(
            _R,
            0,
            "Reviewing shot {next} of {total}",
            end=100,
            counted=True,
            count_label="shots reviewed",
        ),
        "complete": PhaseSpec(_C, 100, "Every shot is animated and reviewed"),
        "partial": PhaseSpec(
            _W,
            0,
            "{done} of {total} shots are ready; the rest need attention",
            end=100,
            counted=True,
        ),
        "failed": PhaseSpec(_F, 0, "Shot generation failed"),
        "cancelled": PhaseSpec(_F, 0, "Shot generation cancelled"),
    },
)

#: The heading shown while the shot-generation stage is reviewing shots.
QUALITY_REVIEW_LABEL = "Quality review"

RENDERING = StageSpec(
    stage="rendering",
    label="Final render",
    unit="step",
    count_label="steps completed",
    status_phases={
        "pending": "queued",
        "render_queued": "queued",
        "render_claiming": "claiming",
        "render_preparing": "preparing",
        "render_manifest_ready": "manifest_ready",
        "render_rendering": "rendering",
        "render_verifying": "verifying",
        "render_persisting": "persisting",
        "render_complete": "complete",
        "render_failed": "failed",
        "render_cancelled": "cancelled",
    },
    # The render job keeps its own durable percentage, which the loader feeds
    # in as the share, so every in-flight phase spans the whole bar.
    phases={
        "queued": PhaseSpec(_Q, 0, "Render queued"),
        "claiming": PhaseSpec(_R, 0, "Starting the render", end=100, counted=True),
        "preparing": PhaseSpec(_R, 0, "Preparing render inputs", end=100, counted=True),
        "manifest_ready": PhaseSpec(_R, 0, "Render manifest ready", end=100, counted=True),
        "rendering": PhaseSpec(_R, 0, "Rendering the final cut", end=100, counted=True),
        "verifying": PhaseSpec(_R, 0, "Verifying the render", end=100, counted=True),
        "persisting": PhaseSpec(_R, 0, "Storing the render", end=100, counted=True),
        "complete": PhaseSpec(_C, 100, "Render complete"),
        "failed": PhaseSpec(_F, 0, "Render failed"),
        "cancelled": PhaseSpec(_F, 0, "Render cancelled"),
    },
)

FINAL_QA = StageSpec(
    stage="final_qa",
    label="Final quality check",
    unit="check",
    count_label="checks completed",
    status_phases={
        "FINAL_QA_QUEUED": "queued",
        "FINAL_QA_VALIDATING_INPUTS": "validating_inputs",
        "FINAL_QA_CHECKING_MEDIA": "checking_media",
        "FINAL_QA_CHECKING_CAPTIONS": "checking_captions",
        "FINAL_QA_ANALYZING": "analyzing",
        "FINAL_QA_ADJUDICATING": "adjudicating",
        "FINAL_QA_PASSED": "passed",
        "FINAL_QA_REVIEW_REQUIRED": "review_required",
        "FINAL_QA_FAILED": "failed",
    },
    phases={
        "queued": PhaseSpec(_Q, 0, "Final quality check queued"),
        "validating_inputs": PhaseSpec(
            _R, 0, "Validating render inputs ({done} of {total} checks done)", end=95, counted=True
        ),
        "checking_media": PhaseSpec(
            _R, 0, "Checking the media ({done} of {total} checks done)", end=95, counted=True
        ),
        "checking_captions": PhaseSpec(
            _R, 0, "Checking captions ({done} of {total} checks done)", end=95, counted=True
        ),
        "analyzing": PhaseSpec(
            _R, 0, "Analyzing the cut ({done} of {total} checks done)", end=95, counted=True
        ),
        "adjudicating": PhaseSpec(
            _R, 0, "Adjudicating findings ({done} of {total} checks done)", end=95, counted=True
        ),
        "passed": PhaseSpec(_C, 100, "Final quality check passed"),
        "review_required": PhaseSpec(_W, 100, "Final quality check needs your review"),
        "failed": PhaseSpec(_F, 0, "Final quality check failed"),
    },
)

#: Every stage that reports progress, in pipeline order.
STAGE_SPECS: tuple[StageSpec, ...] = (
    MEDIA_PROCESSING,
    TRANSCRIPTION,
    EVIDENCE,
    EPISODE_ANALYSIS,
    SCRIPT_GENERATION,
    NARRATION,
    STORYBOARD,
    REFERENCES,
    SHOT_GENERATION,
    RENDERING,
    FINAL_QA,
)

STAGE_ORDER: tuple[str, ...] = tuple(spec.stage for spec in STAGE_SPECS)
