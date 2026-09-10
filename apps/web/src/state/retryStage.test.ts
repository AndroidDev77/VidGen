import type { WorkflowStatusProjection } from "@vidgen/contracts";
import { describe, expect, it, vi } from "vitest";

import * as fixtures from "../test/fixtures";
import {
  RetryStageError,
  entryStageFor,
  isRetryableStage,
  retryStage,
  retryableStages,
  workflowPermittedActions,
  type RetryStageDependencies,
} from "./retryStage";

const { PROJECT_ID } = fixtures;

function workflowWith(overrides: Partial<WorkflowStatusProjection>): WorkflowStatusProjection {
  return { ...fixtures.workflowStatus, ...overrides };
}

function stagesWith(
  states: Partial<Record<string, "pending" | "running" | "complete" | "failed" | "cancelled">>,
): WorkflowStatusProjection["stages"] {
  return fixtures.workflowStatus.stages.map((stage) => ({
    ...stage,
    state: states[stage.stage] ?? stage.state,
  }));
}

/** A recording double for the three endpoints the retry flow touches. */
function fakeDependencies(
  script: {
    readonly workflows?: WorkflowStatusProjection[];
    readonly cancelResponse?: WorkflowStatusProjection;
    readonly continueFails?: Error;
  } = {},
): {
  readonly deps: RetryStageDependencies;
  readonly calls: string[];
  readonly continueBodies: Array<{ entry_stage: string; reason: string }>;
  readonly keys: string[];
} {
  const queue = [...(script.workflows ?? [])];
  const calls: string[] = [];
  const continueBodies: Array<{ entry_stage: string; reason: string }> = [];
  const keys: string[] = [];
  let key = 0;
  const deps: RetryStageDependencies = {
    getWorkflow: vi.fn(() => {
      calls.push("get");
      const next = queue.shift() ?? fixtures.workflowStatus;
      return Promise.resolve({ data: next, response: new Response() } as never);
    }),
    cancelWorkflow: vi.fn((_projectId: string, idempotencyKey: string) => {
      calls.push("cancel");
      keys.push(idempotencyKey);
      return Promise.resolve({
        data: script.cancelResponse ?? workflowWith({ cancelled: false }),
        response: new Response(),
      } as never);
    }),
    continueWorkflow: vi.fn(
      (
        _projectId: string,
        body: { entry_stage: string; reason: string },
        idempotencyKey?: string,
      ) => {
        calls.push("continue");
        continueBodies.push(body);
        keys.push(idempotencyKey ?? "");
        if (script.continueFails) {
          return Promise.reject(script.continueFails);
        }
        return Promise.resolve({
          data: { command: { ...fixtures.commands.items[0]! } },
          response: new Response(),
        } as never);
      },
    ),
    newKey: (prefix: string) => {
      key += 1;
      return `${prefix}-${key}`;
    },
    wait: () => Promise.resolve(),
  };
  return { deps, calls, continueBodies, keys };
}

describe("entryStageFor", () => {
  it("maps every timeline stage onto a stage the workflow accepts", () => {
    // The workflow's own entry stages, from `PROJECT_STAGE_ORDER`.
    const accepted = new Set([
      "upload",
      "media_processing",
      "transcript_acquisition",
      "evidence",
      "episode_analysis",
      "script_generation",
      "narration",
      "storyboard",
      "continuity_references",
      "shot_generation",
      "render",
      "final_editorial_qa",
    ]);
    for (const stage of fixtures.workflowStatus.stages) {
      expect(accepted).toContain(entryStageFor(stage.stage));
    }
  });

  it("folds the per-shot timeline stages back onto shot generation", () => {
    expect(entryStageFor("keyframes")).toBe("shot_generation");
    expect(entryStageFor("animation")).toBe("shot_generation");
    expect(entryStageFor("shot_orchestration")).toBe("shot_generation");
    expect(entryStageFor("captions")).toBe("render");
    expect(entryStageFor("review")).toBe("final_editorial_qa");
  });
});

describe("workflowPermittedActions", () => {
  it("permits cancel only while an execution is live", () => {
    expect(workflowPermittedActions(workflowWith({ status: "shot_generation" }))).toEqual([
      "cancel",
    ]);
    expect(workflowPermittedActions(workflowWith({ cancelled: true }))).toEqual([]);
    expect(workflowPermittedActions(workflowWith({ status: "completed" }))).toEqual([]);
    expect(workflowPermittedActions(workflowWith({ workflow_id: null }))).toEqual([]);
    expect(workflowPermittedActions(undefined)).toEqual([]);
  });
});

describe("retryableStages", () => {
  it("offers only stages that stopped without producing their output", () => {
    const workflow = workflowWith({
      stages: stagesWith({ keyframes: "failed", animation: "running", captions: "pending" }),
    });
    expect(retryableStages(workflow).map((stage) => stage.stage)).toEqual(["keyframes"]);
  });

  it("offers a cancelled stage too, which is what a cancelled run leaves behind", () => {
    const workflow = workflowWith({ stages: stagesWith({ rendering: "cancelled" }) });
    expect(retryableStages(workflow).map((stage) => stage.stage)).toEqual(["rendering"]);
  });

  it("never offers a running or complete stage", () => {
    const running = fixtures.workflowStatus.stages.map((stage) => ({
      ...stage,
      state: "running" as const,
    }));
    expect(retryableStages(workflowWith({ stages: running }))).toEqual([]);
    expect(retryableStages(fixtures.workflowStatus)).toEqual([]);
    expect(isRetryableStage({ ...running[0]!, state: "failed" })).toBe(true);
  });

  it("returns nothing while the workflow has not loaded", () => {
    expect(retryableStages(undefined)).toEqual([]);
  });
});

describe("retryStage", () => {
  it("cancels the live run, waits for it to stop, then continues", async () => {
    const { deps, calls, continueBodies } = fakeDependencies({
      cancelResponse: workflowWith({ cancelled: false }),
      workflows: [workflowWith({ cancelled: false }), workflowWith({ cancelled: true })],
    });
    const result = await retryStage({
      projectId: PROJECT_ID,
      stage: "keyframes",
      workflow: workflowWith({ status: "shot_generation" }),
      pollIntervalMs: 0,
      dependencies: deps,
    });

    // The continuation is only sent after the workflow reported it had stopped.
    expect(calls).toEqual(["cancel", "get", "get", "continue"]);
    expect(continueBodies).toEqual([{ entry_stage: "shot_generation", reason: "remediation" }]);
    expect(result.cancelled).toBe(true);
    expect(result.entryStage).toBe("shot_generation");
  });

  it("skips the cancel when the workflow is no longer running", async () => {
    const { deps, calls, continueBodies } = fakeDependencies();
    const result = await retryStage({
      projectId: PROJECT_ID,
      stage: "rendering",
      workflow: workflowWith({ cancelled: true }),
      dependencies: deps,
    });

    expect(calls).toEqual(["continue"]);
    expect(continueBodies).toEqual([{ entry_stage: "render", reason: "remediation" }]);
    expect(result.cancelled).toBe(false);
  });

  it("sends a fresh idempotency key for every request", async () => {
    const { deps, keys } = fakeDependencies({
      cancelResponse: workflowWith({ cancelled: true }),
    });
    await retryStage({
      projectId: PROJECT_ID,
      stage: "narration",
      workflow: workflowWith({ status: "narration" }),
      dependencies: deps,
    });
    await retryStage({
      projectId: PROJECT_ID,
      stage: "narration",
      workflow: workflowWith({ cancelled: true }),
      dependencies: deps,
    });

    expect(new Set(keys).size).toBe(keys.length);
    expect(keys.filter((key) => key.startsWith("workflow-continue"))).toHaveLength(2);
  });

  it("reads the workflow itself when the caller has none", async () => {
    const { deps, calls } = fakeDependencies({
      workflows: [workflowWith({ cancelled: true })],
    });
    await retryStage({ projectId: PROJECT_ID, stage: "storyboard", dependencies: deps });
    expect(calls).toEqual(["get", "continue"]);
  });

  it("gives up rather than continuing under a run that never stopped", async () => {
    const { deps, calls } = fakeDependencies({
      cancelResponse: workflowWith({ cancelled: false }),
      workflows: [workflowWith({ cancelled: false }), workflowWith({ cancelled: false })],
    });
    await expect(
      retryStage({
        projectId: PROJECT_ID,
        stage: "keyframes",
        workflow: workflowWith({ status: "shot_generation" }),
        pollIntervalMs: 0,
        pollAttempts: 2,
        dependencies: deps,
      }),
    ).rejects.toBeInstanceOf(RetryStageError);
    // Crucially, no continuation was sent.
    expect(calls).not.toContain("continue");
  });

  it("refuses a project that has never started a workflow", async () => {
    const { deps, calls } = fakeDependencies();
    await expect(
      retryStage({
        projectId: PROJECT_ID,
        stage: "keyframes",
        workflow: workflowWith({ workflow_id: null }),
        dependencies: deps,
      }),
    ).rejects.toBeInstanceOf(RetryStageError);
    expect(calls).toEqual([]);
  });

  it("surfaces a rejected continuation to the caller", async () => {
    const { deps } = fakeDependencies({ continueFails: new Error("voice profile required") });
    await expect(
      retryStage({
        projectId: PROJECT_ID,
        stage: "keyframes",
        workflow: workflowWith({ cancelled: true }),
        dependencies: deps,
      }),
    ).rejects.toThrow("voice profile required");
  });
});
