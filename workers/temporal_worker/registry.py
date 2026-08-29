from packages.workflows.activities import (
    run_episode_analysis_activity,
    run_evidence_activity,
    run_final_editorial_qa_activity,
    run_image_generation_activity,
    run_media_processing_activity,
    run_narration_activity,
    run_script_generation_activity,
    run_storyboard_activity,
    run_transcript_acquisition_activity,
    run_upload_activity,
)
from packages.workflows.project import ProjectWorkflow
from packages.workflows.shot import ProjectShotFanoutWorkflow, ShotWorkflow
from packages.workflows.shot_activities import SHOT_ACTIVITIES

WORKFLOWS = [ProjectWorkflow, ProjectShotFanoutWorkflow, ShotWorkflow]
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
]
