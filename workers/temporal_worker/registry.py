from packages.workflows.activities import (
    run_episode_analysis_activity,
    run_evidence_activity,
    run_final_editorial_qa_activity,
    run_image_generation_activity,
    run_media_processing_activity,
    run_narration_activity,
    run_render_activity,
    run_script_generation_activity,
    run_storyboard_activity,
    run_transcript_acquisition_activity,
    run_upload_activity,
)
from packages.workflows.continuity import ContinuityReferenceWorkflow
from packages.workflows.continuity_activities import CONTINUITY_ACTIVITIES
from packages.workflows.control import FinalEditorialQAWorkflow, RenderWorkflow
from packages.workflows.project import ProjectWorkflow
from packages.workflows.shot import ProjectShotFanoutWorkflow, ShotWorkflow
from packages.workflows.shot_activities import SHOT_ACTIVITIES

WORKFLOWS = [
    ProjectWorkflow,
    ProjectShotFanoutWorkflow,
    ShotWorkflow,
    # T19 runs inside the normal project lifecycle and is also started directly
    # by a control command, so the worker must host it either way.
    ContinuityReferenceWorkflow,
    # The owners a manual T22 run and a requested rerender need in order to
    # outlive the HTTP request that asked for them.
    FinalEditorialQAWorkflow,
    RenderWorkflow,
]
#: The T17b render activity runs on its own task queue so a CPU-bound encode
#: never competes with provider-bound activities for the project worker's
#: bounded concurrency.
RENDER_ACTIVITIES = [run_render_activity]
ACTIVITIES = [
    run_upload_activity,
    run_media_processing_activity,
    run_transcript_acquisition_activity,
    run_evidence_activity,
    run_image_generation_activity,
    run_episode_analysis_activity,
    run_script_generation_activity,
    run_narration_activity,
    run_storyboard_activity,
    run_final_editorial_qa_activity,
    *SHOT_ACTIVITIES,
    *CONTINUITY_ACTIVITIES,
]
