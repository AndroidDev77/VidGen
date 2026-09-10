import type { PipelineStage, WorkflowStatusProjection } from "@vidgen/contracts";
import { describe, expect, it } from "vitest";

import type { ProjectStatus } from "../api/projects";
import * as fixtures from "../test/fixtures";
import {
  EPISODE_ANALYSIS_POLL_INTERVAL_MS,
  episodeAnalysisPollInterval,
  isEpisodeAnalysisRunning,
} from "./episodeAnalysis";

function status(overrides: Partial<ProjectStatus> = {}): ProjectStatus {
  return { ...fixtures.projectStatus, ...overrides };
}

function workflow(
  overrides: Partial<WorkflowStatusProjection> & { current_stage: PipelineStage | null },
): WorkflowStatusProjection {
  return { ...fixtures.workflowStatus, status: "running", ...overrides };
}

describe("isEpisodeAnalysisRunning", () => {
  it("is true for every phase before the run finishes or fails", () => {
    for (const phase of ["queued", "scene_analysis", "building_model", "validating"] as const) {
      expect(isEpisodeAnalysisRunning(fixtures.episodeAnalysisProgress({ phase }))).toBe(true);
    }
    expect(isEpisodeAnalysisRunning(fixtures.episodeAnalysisProgress({ phase: "completed" }))).toBe(
      false,
    );
    expect(isEpisodeAnalysisRunning(fixtures.episodeAnalysisProgress({ phase: "failed" }))).toBe(
      false,
    );
    expect(isEpisodeAnalysisRunning(null)).toBe(false);
  });
});

describe("episodeAnalysisPollInterval", () => {
  it("waits for the first status response before deciding", () => {
    expect(episodeAnalysisPollInterval(undefined, fixtures.workflowStatus)).toBe(false);
  });

  it("polls every five seconds while a run is in flight, whatever the workflow says", () => {
    const running = status({ episode_analysis: fixtures.episodeAnalysisProgress() });
    expect(episodeAnalysisPollInterval(running, undefined)).toBe(
      EPISODE_ANALYSIS_POLL_INTERVAL_MS,
    );
    expect(episodeAnalysisPollInterval(running, fixtures.workflowStatus)).toBe(
      EPISODE_ANALYSIS_POLL_INTERVAL_MS,
    );
  });

  it("stops once the analysis has finished and the workflow has moved on", () => {
    const finished = status({
      episode_analysis: fixtures.episodeAnalysisProgress({ phase: "completed", percentage: 100 }),
    });
    expect(episodeAnalysisPollInterval(finished, fixtures.workflowStatus)).toBe(false);
    expect(
      episodeAnalysisPollInterval(finished, workflow({ current_stage: "script_generation" })),
    ).toBe(false);
  });

  it("keeps polling a finished run while the workflow is still on the analysis stage", () => {
    // A retry after a failure reuses the stage; the next response shows it running again.
    const failed = status({
      episode_analysis: fixtures.episodeAnalysisProgress({
        phase: "failed",
        error_code: "SCENE_ANALYSIS_FAILED",
      }),
    });
    expect(episodeAnalysisPollInterval(failed, workflow({ current_stage: "episode_analysis" }))).toBe(
      EPISODE_ANALYSIS_POLL_INTERVAL_MS,
    );
    expect(
      episodeAnalysisPollInterval(
        failed,
        workflow({ current_stage: "episode_analysis", status: "failed" }),
      ),
    ).toBe(false);
  });

  it("watches for the first run only while a live workflow has not passed the stage", () => {
    const none = status({ episode_analysis: null });
    expect(episodeAnalysisPollInterval(none, undefined)).toBe(false);
    expect(
      episodeAnalysisPollInterval(none, workflow({ current_stage: null, workflow_id: null })),
    ).toBe(false);
    expect(episodeAnalysisPollInterval(none, workflow({ current_stage: null }))).toBe(
      EPISODE_ANALYSIS_POLL_INTERVAL_MS,
    );
    expect(episodeAnalysisPollInterval(none, workflow({ current_stage: "evidence" }))).toBe(
      EPISODE_ANALYSIS_POLL_INTERVAL_MS,
    );
    expect(
      episodeAnalysisPollInterval(none, workflow({ current_stage: "episode_analysis" })),
    ).toBe(EPISODE_ANALYSIS_POLL_INTERVAL_MS);
    expect(
      episodeAnalysisPollInterval(none, workflow({ current_stage: "script_generation" })),
    ).toBe(false);
    expect(
      episodeAnalysisPollInterval(none, workflow({ current_stage: "evidence", cancelled: true })),
    ).toBe(false);
    expect(
      episodeAnalysisPollInterval(none, workflow({ current_stage: "evidence", status: "failed" })),
    ).toBe(false);
  });
});
