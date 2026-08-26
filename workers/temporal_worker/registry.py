from packages.workflows.activities import (
    run_episode_analysis_activity,
    run_evidence_activity,
    run_media_processing_activity,
    run_narration_activity,
    run_script_generation_activity,
    run_transcript_acquisition_activity,
    run_upload_activity,
)
from packages.workflows.project import ProjectWorkflow

WORKFLOWS = [ProjectWorkflow]
ACTIVITIES = [
    run_upload_activity,
    run_media_processing_activity,
    run_transcript_acquisition_activity,
    run_evidence_activity,
    run_episode_analysis_activity,
    run_script_generation_activity,
    run_narration_activity,
]
