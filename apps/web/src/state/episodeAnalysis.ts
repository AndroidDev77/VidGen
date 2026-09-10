import { PIPELINE_STAGE_ORDER, type WorkflowStatusProjection } from "@vidgen/contracts";

import type { EpisodeAnalysisPhase, EpisodeAnalysisProgress, ProjectStatus } from "../api/projects";

/** How often the dashboard re-reads the project status while analysis runs. */
export const EPISODE_ANALYSIS_POLL_INTERVAL_MS = 5_000;

const TERMINAL_PHASES: ReadonlySet<EpisodeAnalysisPhase> = new Set(["completed", "failed"]);

/** Workflow statuses after which no further analysis run can start. */
const FINISHED_WORKFLOW_STATUSES: ReadonlySet<string> = new Set([
  "completed",
  "failed",
  "cancelled",
]);

const ANALYSIS_STAGE_INDEX = PIPELINE_STAGE_ORDER.indexOf("episode_analysis");

/** Whether the run still has work ahead of it. */
export function isEpisodeAnalysisRunning(
  progress: EpisodeAnalysisProgress | null | undefined,
): boolean {
  return progress !== null && progress !== undefined && !TERMINAL_PHASES.has(progress.phase);
}

/**
 * The polling interval for the status query, or `false` to stop.
 *
 * Polling runs while an analysis is in flight. Before the first run exists it
 * also runs while a live workflow has not yet passed the analysis stage, so the
 * panel appears without a reload; after a run finishes it keeps going only
 * while the workflow is still on that stage, which is what a retry looks like.
 * Everything else, a project that never started, a finished analysis, a
 * cancelled or failed workflow, stops the timer.
 */
export function episodeAnalysisPollInterval(
  status: ProjectStatus | undefined,
  workflow: WorkflowStatusProjection | undefined,
): number | false {
  if (status === undefined) {
    return false;
  }
  if (isEpisodeAnalysisRunning(status.episode_analysis)) {
    return EPISODE_ANALYSIS_POLL_INTERVAL_MS;
  }
  const live =
    workflow !== undefined &&
    workflow.workflow_id !== null &&
    !workflow.cancelled &&
    !FINISHED_WORKFLOW_STATUSES.has(workflow.status);
  if (!live) {
    return false;
  }
  const stageIndex =
    workflow.current_stage === null ? 0 : PIPELINE_STAGE_ORDER.indexOf(workflow.current_stage);
  if (status.episode_analysis === null) {
    return stageIndex <= ANALYSIS_STAGE_INDEX ? EPISODE_ANALYSIS_POLL_INTERVAL_MS : false;
  }
  return stageIndex === ANALYSIS_STAGE_INDEX ? EPISODE_ANALYSIS_POLL_INTERVAL_MS : false;
}
