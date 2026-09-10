import type {
  PipelineStage,
  StageTimelineEntry,
  WorkflowStatusProjection,
} from "@vidgen/contracts";

import { newIdempotencyKey, type VidGenClient } from "../api/client";
import { continueWorkflow as continueWorkflowRequest } from "../api/commands";
import {
  cancelWorkflow as cancelWorkflowRequest,
  getWorkflow as getWorkflowRequest,
} from "../api/workflows";

/**
 * Manually re-entering the pipeline at a stage that ran out of retries.
 *
 * The browser sees the *timeline* stages (`keyframes`, `animation`, …) but the
 * project workflow only accepts its own coarser entry stages, so the mapping
 * below is the inverse of the backend's `WORKFLOW_STAGE_ALIASES`. It is a table
 * rather than a guess: sending a stage the workflow does not know is rejected
 * with a validation error, and silently retrying the wrong stage would spend
 * provider budget on work that was already complete.
 */
const ENTRY_STAGE_BY_TIMELINE_STAGE: Readonly<Record<PipelineStage, string>> = {
  upload: "upload",
  media_processing: "media_processing",
  transcript_acquisition: "transcript_acquisition",
  evidence: "evidence",
  episode_analysis: "episode_analysis",
  script_generation: "script_generation",
  narration: "narration",
  storyboard: "storyboard",
  keyframes: "shot_generation",
  animation: "shot_generation",
  shot_orchestration: "shot_generation",
  // Captions are produced inside the render stage, which is therefore the
  // earliest entry point that rebuilds them.
  captions: "render",
  rendering: "render",
  review: "final_editorial_qa",
};

/** The workflow entry stage that re-runs a timeline stage. */
export function entryStageFor(stage: PipelineStage): string | undefined {
  return ENTRY_STAGE_BY_TIMELINE_STAGE[stage];
}

/** Stage states that mean the stage stopped without producing its output. */
const RETRYABLE_STAGE_STATES: ReadonlySet<string> = new Set(["failed", "cancelled"]);

/** Workflow statuses after which no execution is still running. */
const FINISHED_WORKFLOW_STATUSES: ReadonlySet<string> = new Set([
  "completed",
  "failed",
  "cancelled",
]);

/**
 * What the owner may do to the workflow right now.
 *
 * The workflow projection carries no `permitted_actions` of its own — only a
 * control command does — so it is derived here, once, from the same facts the
 * cancel button already reads. `cancel` present means an execution is still
 * live, which is exactly the case where a retry has to stop it first.
 */
export function workflowPermittedActions(
  workflow: WorkflowStatusProjection | undefined,
): Array<"cancel"> {
  if (workflow === undefined || workflow.workflow_id === null) {
    return [];
  }
  if (workflow.cancelled || FINISHED_WORKFLOW_STATUSES.has(workflow.status)) {
    return [];
  }
  return ["cancel"];
}

/** Whether a stage stopped in a state a manual retry can re-enter. */
export function isRetryableStage(stage: StageTimelineEntry): boolean {
  return RETRYABLE_STAGE_STATES.has(stage.state);
}

/**
 * The stages the dashboard should offer a Retry button for.
 *
 * A stage that is still pending or running is never offered: its own retry
 * budget may not be spent yet, and re-entering underneath a live execution
 * would race it. Once the workflow is cancelled every stalled stage is
 * eligible, which is what the cancel-then-continue flow leaves behind.
 */
export function retryableStages(
  workflow: WorkflowStatusProjection | undefined,
): StageTimelineEntry[] {
  if (workflow === undefined) {
    return [];
  }
  return workflow.stages.filter(isRetryableStage);
}

export class RetryStageError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "RetryStageError";
  }
}

/** How often the retry flow re-reads the workflow while waiting for a cancel. */
export const CANCEL_POLL_INTERVAL_MS = 1_000;

/** How many polls to allow before giving up on a cancellation. */
export const CANCEL_POLL_ATTEMPTS = 30;

export interface RetryStageDependencies {
  readonly getWorkflow: typeof getWorkflowRequest;
  readonly cancelWorkflow: typeof cancelWorkflowRequest;
  readonly continueWorkflow: typeof continueWorkflowRequest;
  readonly newKey: (prefix: string) => string;
  readonly wait: (ms: number) => Promise<void>;
}

const defaultDependencies: RetryStageDependencies = {
  getWorkflow: getWorkflowRequest,
  cancelWorkflow: cancelWorkflowRequest,
  continueWorkflow: continueWorkflowRequest,
  newKey: newIdempotencyKey,
  wait: (ms) =>
    new Promise((resolve) => {
      setTimeout(resolve, ms);
    }),
};

export interface RetryStageOptions {
  readonly projectId: string;
  readonly stage: PipelineStage;
  /** The workflow as the page last saw it. Re-read when omitted. */
  readonly workflow?: WorkflowStatusProjection | undefined;
  readonly client?: VidGenClient;
  readonly pollIntervalMs?: number;
  readonly pollAttempts?: number;
  readonly dependencies?: Partial<RetryStageDependencies>;
}

export interface RetryStageResult {
  /** The workflow entry stage the continuation was submitted for. */
  readonly entryStage: string;
  /** Whether a live execution had to be cancelled first. */
  readonly cancelled: boolean;
  /** The control command id the backend created for the continuation. */
  readonly commandId: string;
}

/**
 * Cancel the live execution, if there is one, then continue from `stage`.
 *
 * `workflow:continue` starts a new generation run, and the backend refuses to
 * have two of them in flight, so a workflow that still permits `cancel` is
 * stopped first. The cancel is only observed as done when the workflow itself
 * reports `cancelled: true`: the endpoint returns as soon as the signal is
 * sent, and continuing on that alone would race the dispatcher.
 */
export async function retryStage(options: RetryStageOptions): Promise<RetryStageResult> {
  const deps: RetryStageDependencies = { ...defaultDependencies, ...options.dependencies };
  const { projectId, stage, client } = options;
  const interval = options.pollIntervalMs ?? CANCEL_POLL_INTERVAL_MS;
  const attempts = options.pollAttempts ?? CANCEL_POLL_ATTEMPTS;
  const entryStage = entryStageFor(stage);
  if (entryStage === undefined) {
    throw new RetryStageError(`There is no workflow entry stage for ${stage}.`);
  }

  let workflow = options.workflow;
  if (workflow === undefined) {
    workflow = (await deps.getWorkflow(projectId, client)).data;
  }
  if (workflow.workflow_id === null) {
    throw new RetryStageError("This project has no workflow run to retry.");
  }

  let cancelled = false;
  if (workflowPermittedActions(workflow).includes("cancel")) {
    const response = await deps.cancelWorkflow(
      projectId,
      deps.newKey("workflow-cancel"),
      client,
    );
    cancelled = true;
    let current = response.data;
    let remaining = attempts;
    while (!current.cancelled) {
      if (remaining <= 0) {
        throw new RetryStageError(
          "The workflow did not stop in time, so it was not continued. Try again in a moment.",
        );
      }
      remaining -= 1;
      await deps.wait(interval);
      current = (await deps.getWorkflow(projectId, client)).data;
    }
  }

  const continued = await deps.continueWorkflow(
    projectId,
    { entry_stage: entryStage, reason: "remediation" },
    // A fresh key per click: replaying the previous one would return the old
    // command instead of starting the retry the owner just asked for.
    deps.newKey("workflow-continue"),
    client,
  );
  return { entryStage, cancelled, commandId: continued.data.command.command_id };
}
