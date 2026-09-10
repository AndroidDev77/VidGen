import type { WorkflowStatusProjection } from "@vidgen/contracts";
import { describe, expect, it } from "vitest";

import type { ProjectStatus, StageProgressState } from "../api/projects";
import * as fixtures from "../test/fixtures";
import {
  STAGE_PROGRESS_POLL_INTERVAL_MS,
  isStageProgressActive,
  stageProgressPollInterval,
} from "./stageProgress";

function status(overrides: Partial<ProjectStatus> = {}): ProjectStatus {
  return { ...fixtures.projectStatus, ...overrides };
}

function withState(state: StageProgressState): ProjectStatus {
  return status({ stage_progress: fixtures.stageProgress({ state }) });
}

function workflow(overrides: Partial<WorkflowStatusProjection> = {}): WorkflowStatusProjection {
  return { ...fixtures.workflowStatus, status: "running", ...overrides };
}

const finished = workflow({ status: "completed" });
const cancelled = workflow({ cancelled: true });
const unstarted = workflow({ workflow_id: null });

describe("isStageProgressActive", () => {
  it("is true only while a stage is queued or running", () => {
    expect(isStageProgressActive(fixtures.stageProgress({ state: "queued" }))).toBe(true);
    expect(isStageProgressActive(fixtures.stageProgress({ state: "running" }))).toBe(true);
    for (const state of ["waiting", "completed", "failed"] as const) {
      expect(isStageProgressActive(fixtures.stageProgress({ state }))).toBe(false);
    }
    expect(isStageProgressActive(null)).toBe(false);
  });
});

describe("stageProgressPollInterval", () => {
  it("waits for the first status response before deciding", () => {
    expect(stageProgressPollInterval(undefined, workflow())).toBe(false);
  });

  it("polls every five seconds while a stage is in flight, whatever the workflow says", () => {
    for (const state of ["queued", "running"] as const) {
      expect(stageProgressPollInterval(withState(state), undefined)).toBe(
        STAGE_PROGRESS_POLL_INTERVAL_MS,
      );
      expect(stageProgressPollInterval(withState(state), finished)).toBe(
        STAGE_PROGRESS_POLL_INTERVAL_MS,
      );
    }
  });

  it("keeps watching a settled stage only while the workflow can open the next one", () => {
    // Narration finished; the workflow is still running, so the storyboard
    // is about to appear here without a reload.
    expect(stageProgressPollInterval(withState("completed"), workflow())).toBe(
      STAGE_PROGRESS_POLL_INTERVAL_MS,
    );
    // A failed stage is retried by a live workflow, so the retry shows too.
    expect(stageProgressPollInterval(withState("failed"), workflow())).toBe(
      STAGE_PROGRESS_POLL_INTERVAL_MS,
    );
    for (const settled of [finished, cancelled, unstarted, undefined]) {
      expect(stageProgressPollInterval(withState("completed"), settled)).toBe(false);
      expect(stageProgressPollInterval(withState("failed"), settled)).toBe(false);
    }
  });

  it("stops at a human gate even while the workflow is alive", () => {
    expect(stageProgressPollInterval(withState("waiting"), workflow())).toBe(false);
  });

  it("watches for the first stage only while a workflow is live", () => {
    const none = status({ stage_progress: null });
    expect(stageProgressPollInterval(none, workflow())).toBe(STAGE_PROGRESS_POLL_INTERVAL_MS);
    expect(stageProgressPollInterval(none, undefined)).toBe(false);
    expect(stageProgressPollInterval(none, unstarted)).toBe(false);
    expect(stageProgressPollInterval(none, cancelled)).toBe(false);
    expect(stageProgressPollInterval(none, finished)).toBe(false);
  });
});
