import type { WorkflowStatusProjection } from "@vidgen/contracts";

import type { ProjectStatus, StageProgress, StageProgressState } from "../api/projects";

/** How often the dashboard re-reads the project status while a stage runs. */
export const STAGE_PROGRESS_POLL_INTERVAL_MS = 5_000;

const ACTIVE_STATES: ReadonlySet<StageProgressState> = new Set(["queued", "running"]);

/** Workflow statuses after which no further stage can start. */
const FINISHED_WORKFLOW_STATUSES: ReadonlySet<string> = new Set([
  "completed",
  "failed",
  "cancelled",
]);

/** Whether the reported stage still has work in flight. */
export function isStageProgressActive(progress: StageProgress | null | undefined): boolean {
  return progress !== null && progress !== undefined && ACTIVE_STATES.has(progress.state);
}

/**
 * The polling interval for the status query, or `false` to stop.
 *
 * Polling runs while the reported stage is queued or running, whatever the
 * workflow says. Once that stage has settled it keeps going only while a live
 * workflow can still open the next stage (or retry a failed one), so the panel
 * moves on without a reload. A stage waiting on the owner stops the timer:
 * nothing changes until they act, and the page they act on refreshes itself.
 */
export function stageProgressPollInterval(
  status: ProjectStatus | undefined,
  workflow: WorkflowStatusProjection | undefined,
): number | false {
  if (status === undefined) {
    return false;
  }
  const progress = status.stage_progress;
  if (isStageProgressActive(progress)) {
    return STAGE_PROGRESS_POLL_INTERVAL_MS;
  }
  if (progress?.state === "waiting") {
    return false;
  }
  const live =
    workflow !== undefined &&
    workflow.workflow_id !== null &&
    !workflow.cancelled &&
    !FINISHED_WORKFLOW_STATUSES.has(workflow.status);
  return live ? STAGE_PROGRESS_POLL_INTERVAL_MS : false;
}
