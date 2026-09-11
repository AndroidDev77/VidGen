import type {
  PipelineFailureListItem,
  PipelineStage,
  WorkflowStatusProjection,
} from "@vidgen/contracts";

/**
 * The failure that explains why a project is sitting still.
 *
 * A project whose workflow died leaves no live execution to query, so the
 * dashboard cannot learn the failure from the workflow projection: the stage
 * timeline goes blank and the page reports a run that is still going. The
 * `pipeline_failure_events` the stage wrote on its way out are the record that
 * survives, and this module turns the latest unresolved one into the two things
 * the owner actually needs - what broke, and which stage to re-enter.
 */

/**
 * Failure `stage` values mapped onto the timeline stage a retry re-enters.
 *
 * The recorded stage is the *workflow's* name for it, the same vocabulary as
 * the backend's `WORKFLOW_STAGE_ALIASES`. Retrying needs the timeline stage,
 * because that is what `entryStageFor` translates back into a workflow entry
 * stage. A stage missing from this table is shown but not offered for retry:
 * guessing would spend provider budget on the wrong work.
 */
const TIMELINE_STAGE_BY_FAILURE_STAGE: Readonly<Record<string, PipelineStage>> = {
  upload: "upload",
  media_processing: "media_processing",
  transcript_acquisition: "transcript_acquisition",
  evidence: "evidence",
  episode_analysis: "episode_analysis",
  script_generation: "script_generation",
  narration: "narration",
  storyboard: "storyboard",
  keyframes: "keyframes",
  animation: "animation",
  shot_generation: "shot_orchestration",
  shot_orchestration: "shot_orchestration",
  captions: "captions",
  rendering: "rendering",
  render: "rendering",
  render_failed: "rendering",
  review: "review",
  final_editorial_qa: "review",
};

/** The timeline stage a retry of this failure should re-enter, if any. */
export function timelineStageForFailure(stage: string): PipelineStage | undefined {
  return TIMELINE_STAGE_BY_FAILURE_STAGE[stage];
}

/**
 * Workflow statuses that mean the run stopped on a failure.
 *
 * `shot_generation_failed` is deliberately absent: it is a *review* pause, not
 * a dead workflow, and the header already offers the owner the storyboard. A
 * cancelled run is absent for the same reason - somebody chose that.
 */
const STOPPED_ON_FAILURE: ReadonlySet<string> = new Set([
  "failed",
  "script_generation_failed",
  "render_failed",
  "FINAL_QA_FAILED",
  "references_failed",
]);

/** Whether the project stopped on a failure and nothing is running for it. */
export function stoppedOnFailure(
  workflow: WorkflowStatusProjection | undefined,
): boolean {
  if (workflow === undefined || workflow.cancelled) {
    return false;
  }
  return STOPPED_ON_FAILURE.has(workflow.status);
}

/**
 * Failure statuses that describe something the pipeline got past on its own.
 *
 * A retried provider call leaves an unresolved row behind, and treating that as
 * the reason the project is stopped would put a retry button on a project that
 * is working perfectly well.
 */
const RECOVERED_FAILURE_STATUSES: ReadonlySet<string> = new Set(["recovered", "resolved"]);

/**
 * The most recent failure nobody has resolved.
 *
 * The API orders failures newest first, but the dashboard must not depend on
 * that: a stale banner pointing at an older failure would send the owner to
 * retry the wrong stage. Ordering here is by recorded time, with the response
 * order as the tie-break for rows written in the same instant.
 */
export function unresolvedFailure(
  failures: readonly PipelineFailureListItem[] | undefined,
): PipelineFailureListItem | undefined {
  const open = (failures ?? []).filter(
    (failure) =>
      failure.resolvedAt === null && !RECOVERED_FAILURE_STATUSES.has(failure.status),
  );
  if (open.length === 0) {
    return undefined;
  }
  return open.reduce((latest, failure) =>
    timeOf(failure) > timeOf(latest) ? failure : latest,
  );
}

function timeOf(failure: PipelineFailureListItem): number {
  if (failure.createdAt === null) {
    return Number.NEGATIVE_INFINITY;
  }
  const parsed = Date.parse(failure.createdAt);
  return Number.isNaN(parsed) ? Number.NEGATIVE_INFINITY : parsed;
}
