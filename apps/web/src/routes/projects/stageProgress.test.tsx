import type { QueryClient } from "@tanstack/react-query";
import { screen, waitFor } from "@testing-library/react";
import { HttpResponse, http } from "msw";
import { Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { StageProgress } from "../../api/projects";
import { STAGE_PROGRESS_POLL_INTERVAL_MS } from "../../state/stageProgress";
import * as fixtures from "../../test/fixtures";
import { renderWithProviders } from "../../test/render";
import { server } from "../../test/server";
import { ProjectDashboardPage } from "./ProjectDashboardPage";

const BASE = "http://localhost";
const { PROJECT_ID } = fixtures;
const STATUS_URL = `${BASE}/api/v1/projects/${PROJECT_ID}/status`;
const WORKFLOW_URL = `${BASE}/api/v1/projects/${PROJECT_ID}/workflow`;

/**
 * Serve one status response per poll, repeating the last one, and count
 * how many times the dashboard asked.
 */
function serveProgress(sequence: readonly (StageProgress | null)[]): {
  readonly requests: () => number;
} {
  let served = 0;
  server.use(
    http.get(STATUS_URL, () => {
      const progress = sequence[Math.min(served, sequence.length - 1)] ?? null;
      served += 1;
      return HttpResponse.json({ ...fixtures.projectStatus, stage_progress: progress });
    }),
  );
  return { requests: () => served };
}

/** A workflow that has finished, so a settled stage ends the polling. */
function serveFinishedWorkflow(): void {
  server.use(
    http.get(WORKFLOW_URL, () =>
      HttpResponse.json({ ...fixtures.workflowStatus, status: "completed" }),
    ),
  );
}

function renderDashboard() {
  return renderWithProviders(
    <Routes>
      <Route path="/projects/:projectId/*" element={<ProjectDashboardPage />} />
    </Routes>,
    { route: `/projects/${PROJECT_ID}` },
  );
}

/**
 * Whether the status query started a fetch. A query marks itself as fetching
 * synchronously when its interval fires, so this can be read straight after
 * advancing the clock without waiting for the mocked server to answer.
 */
function statusFetching(queryClient: QueryClient): number {
  return queryClient.isFetching({ queryKey: ["projects", PROJECT_ID, "status"] });
}

function setVisibility(state: DocumentVisibilityState): void {
  Object.defineProperty(document, "visibilityState", { configurable: true, value: state });
  window.dispatchEvent(new Event("visibilitychange"));
}

const narration = fixtures.stageProgress({
  stage: "narration",
  label: "Narration",
  phase: "generating",
  completed_count: 5,
  total_count: 12,
  unit: "segment",
  count_label: "segments narrated",
  percentage: 38.3,
  message: "Generating narration 6 of 12",
  updated_at: "2026-08-01T09:20:00Z",
});

const animating = fixtures.stageProgress({
  stage: "shot_generation",
  label: "Shot generation",
  phase: "animating",
  completed_count: 8,
  total_count: 20,
  unit: "shot",
  count_label: "shots animated",
  percentage: 46.7,
  message: "Animating shot 9 of 20",
});

const reviewing = fixtures.stageProgress({
  ...animating,
  label: "Quality review",
  phase: "reviewing",
  completed_count: 5,
  count_label: "shots reviewed",
  percentage: 75,
  message: "Reviewing shot 6 of 20",
});

const rendering = fixtures.stageProgress({
  stage: "rendering",
  label: "Final render",
  phase: "rendering",
  completed_count: 0,
  total_count: 0,
  unit: "step",
  count_label: "steps completed",
  percentage: 40,
  message: "Rendering the final cut",
});

describe("stage progress on the dashboard", () => {
  afterEach(() => {
    vi.useRealTimers();
    setVisibility("visible");
  });

  it("shows the stage heading, message, bar, counts and last update", async () => {
    serveProgress([narration]);
    renderDashboard();

    expect(await screen.findByRole("heading", { name: "Narration" })).toBeVisible();
    expect(screen.getByTestId("stage-progress-message")).toHaveTextContent(
      "Generating narration 6 of 12",
    );
    const bar = screen.getByRole("progressbar", { name: "Narration progress" });
    expect(bar).toHaveAttribute("aria-valuenow", "38.3");
    expect(bar).toHaveAttribute("aria-valuemax", "100");
    expect(screen.getByText("38%")).toBeVisible();
    expect(screen.getByText("5 of 12 segments narrated")).toBeVisible();
    expect(screen.getByText(/^Updated 1 Aug 2026, 09:20 UTC$/)).toBeVisible();
  });

  it("omits the count line for a stage that reports no counted work", async () => {
    serveProgress([rendering]);
    renderDashboard();
    expect(await screen.findByRole("heading", { name: "Final render" })).toBeVisible();
    expect(screen.getByText("Rendering the final cut")).toBeVisible();
    expect(screen.queryByText(/steps completed/)).not.toBeInTheDocument();
  });

  it("paints a human gate as waiting rather than as a failure", async () => {
    serveProgress([
      fixtures.stageProgress({
        stage: "script_generation",
        label: "Script generation",
        state: "waiting",
        phase: "review_required",
        percentage: 100,
        message: "Script needs your review",
      }),
    ]);
    renderDashboard();
    expect(await screen.findByText("Script needs your review")).toBeVisible();
    expect(screen.getByTestId("stage-progress-message")).toHaveAttribute("role", "status");
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("stays hidden until a stage has started", async () => {
    serveProgress([null]);
    renderDashboard();
    expect(await screen.findByRole("heading", { name: "Dashboard" })).toBeVisible();
    await waitFor(() => expect(screen.getByRole("heading", { name: "Cost" })).toBeVisible());
    expect(screen.queryByTestId("stage-progress")).not.toBeInTheDocument();
  });

  it("polls every five seconds, follows the pipeline from stage to stage in place, and stops when it settles", async () => {
    serveFinishedWorkflow();
    const served = serveProgress([
      narration,
      animating,
      reviewing,
      fixtures.stageProgress({
        ...rendering,
        state: "completed",
        phase: "complete",
        percentage: 100,
        message: "Render complete",
      }),
    ]);
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const { queryClient } = renderDashboard();
    expect(await screen.findByText("Generating narration 6 of 12")).toBeVisible();
    const panel = screen.getByTestId("stage-progress");

    vi.advanceTimersByTime(STAGE_PROGRESS_POLL_INTERVAL_MS);
    expect(await screen.findByText("Animating shot 9 of 20")).toBeVisible();
    expect(served.requests()).toBe(2);
    // The same panel carried over to the next stage: nothing was unmounted
    // for a loading state, and the heading simply changed.
    expect(screen.getByTestId("stage-progress")).toBe(panel);
    expect(screen.getByRole("heading", { name: "Shot generation" })).toBeVisible();
    expect(screen.getByText("8 of 20 shots animated")).toBeVisible();
    expect(screen.queryByText(/Loading/)).not.toBeInTheDocument();

    vi.advanceTimersByTime(STAGE_PROGRESS_POLL_INTERVAL_MS);
    expect(await screen.findByText("Reviewing shot 6 of 20")).toBeVisible();
    expect(screen.getByRole("heading", { name: "Quality review" })).toBeVisible();
    expect(screen.getByText("5 of 20 shots reviewed")).toBeVisible();
    expect(served.requests()).toBe(3);

    vi.advanceTimersByTime(STAGE_PROGRESS_POLL_INTERVAL_MS);
    expect(await screen.findByText("Render complete")).toBeVisible();
    expect(served.requests()).toBe(4);
    expect(screen.getByRole("progressbar", { name: "Final render progress" })).toHaveAttribute(
      "aria-valuenow",
      "100",
    );

    // The last stage settled and the workflow has finished: no further poll.
    vi.advanceTimersByTime(STAGE_PROGRESS_POLL_INTERVAL_MS * 3);
    expect(statusFetching(queryClient)).toBe(0);
    expect(served.requests()).toBe(4);
  });

  it("pauses polling while the page is hidden and resumes when it is shown", async () => {
    const served = serveProgress([narration]);
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const { queryClient } = renderDashboard();
    expect(await screen.findByText("Generating narration 6 of 12")).toBeVisible();
    expect(served.requests()).toBe(1);

    setVisibility("hidden");
    vi.advanceTimersByTime(STAGE_PROGRESS_POLL_INTERVAL_MS * 3);
    expect(statusFetching(queryClient)).toBe(0);
    expect(served.requests()).toBe(1);

    setVisibility("visible");
    vi.advanceTimersByTime(STAGE_PROGRESS_POLL_INTERVAL_MS);
    expect(statusFetching(queryClient)).toBe(1);
    await waitFor(() => expect(served.requests()).toBe(2));
  });
});
