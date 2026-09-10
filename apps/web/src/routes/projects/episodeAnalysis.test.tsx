import type { QueryClient } from "@tanstack/react-query";
import { screen, waitFor } from "@testing-library/react";
import { HttpResponse, http } from "msw";
import { Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { EpisodeAnalysisProgress } from "../../api/projects";
import { EPISODE_ANALYSIS_POLL_INTERVAL_MS } from "../../state/episodeAnalysis";
import * as fixtures from "../../test/fixtures";
import { renderWithProviders } from "../../test/render";
import { server } from "../../test/server";
import { ProjectDashboardPage } from "./ProjectDashboardPage";

const BASE = "http://localhost";
const { PROJECT_ID } = fixtures;
const STATUS_URL = `${BASE}/api/v1/projects/${PROJECT_ID}/status`;

/**
 * Serve one status response per poll, repeating the last one, and count
 * how many times the dashboard asked.
 */
function serveProgress(sequence: readonly (EpisodeAnalysisProgress | null)[]): {
  readonly requests: () => number;
} {
  let served = 0;
  server.use(
    http.get(STATUS_URL, () => {
      const progress = sequence[Math.min(served, sequence.length - 1)] ?? null;
      served += 1;
      return HttpResponse.json({ ...fixtures.projectStatus, episode_analysis: progress });
    }),
  );
  return { requests: () => served };
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

describe("episode-analysis progress on the dashboard", () => {
  afterEach(() => {
    vi.useRealTimers();
    setVisibility("visible");
  });

  it("shows the scene count, message, bar and last update from the status response", async () => {
    serveProgress([fixtures.episodeAnalysisProgress()]);
    renderDashboard();

    expect(await screen.findByRole("heading", { name: "Episode analysis" })).toBeVisible();
    expect(screen.getByTestId("episode-analysis-message")).toHaveTextContent(
      "Analyzing scene 18 of 32",
    );
    const bar = screen.getByRole("progressbar", { name: "Episode analysis progress" });
    expect(bar).toHaveAttribute("aria-valuenow", "42.5");
    expect(bar).toHaveAttribute("aria-valuemax", "100");
    expect(screen.getByText("43%")).toBeVisible();
    expect(screen.getByText("17 of 32 scenes analyzed")).toBeVisible();
    expect(screen.getByText(/^Updated 1 Aug 2026, 09:05 UTC$/)).toBeVisible();
  });

  it("stays hidden until the workflow has opened an analysis run", async () => {
    serveProgress([null]);
    renderDashboard();
    expect(await screen.findByRole("heading", { name: "Dashboard" })).toBeVisible();
    await waitFor(() => expect(screen.getByRole("heading", { name: "Cost" })).toBeVisible());
    expect(screen.queryByRole("heading", { name: "Episode analysis" })).not.toBeInTheDocument();
  });

  it("polls every five seconds, updates the bar in place and stops when the run finishes", async () => {
    const served = serveProgress([
      fixtures.episodeAnalysisProgress(),
      fixtures.episodeAnalysisProgress({
        phase: "building_model",
        completed_scene_count: 32,
        percentage: 85,
        message: "Building episode model",
      }),
      fixtures.episodeAnalysisProgress({
        phase: "validating",
        completed_scene_count: 32,
        percentage: 95,
        message: "Validating episode analysis",
      }),
      fixtures.episodeAnalysisProgress({
        phase: "completed",
        completed_scene_count: 32,
        percentage: 100,
        message: "Episode analysis complete",
      }),
    ]);
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const { queryClient } = renderDashboard();
    expect(await screen.findByText("Analyzing scene 18 of 32")).toBeVisible();
    const panel = screen.getByTestId("episode-analysis-progress");
    const bar = screen.getByRole("progressbar", { name: "Episode analysis progress" });

    vi.advanceTimersByTime(EPISODE_ANALYSIS_POLL_INTERVAL_MS);
    expect(await screen.findByText("Building episode model")).toBeVisible();
    expect(served.requests()).toBe(2);
    // The same nodes were updated: nothing was unmounted for a loading state.
    expect(screen.getByTestId("episode-analysis-progress")).toBe(panel);
    expect(screen.getByRole("progressbar", { name: "Episode analysis progress" })).toBe(bar);
    expect(bar).toHaveAttribute("aria-valuenow", "85");
    expect(screen.queryByText(/Loading/)).not.toBeInTheDocument();

    vi.advanceTimersByTime(EPISODE_ANALYSIS_POLL_INTERVAL_MS);
    expect(await screen.findByText("Validating episode analysis")).toBeVisible();
    expect(served.requests()).toBe(3);

    vi.advanceTimersByTime(EPISODE_ANALYSIS_POLL_INTERVAL_MS);
    expect(await screen.findByText("Episode analysis complete")).toBeVisible();
    expect(served.requests()).toBe(4);
    expect(bar).toHaveAttribute("aria-valuenow", "100");

    // The run is over and the workflow fixture has moved past the stage, so
    // no further poll is scheduled.
    vi.advanceTimersByTime(EPISODE_ANALYSIS_POLL_INTERVAL_MS * 3);
    expect(statusFetching(queryClient)).toBe(0);
    expect(served.requests()).toBe(4);
  });

  it("pauses polling while the page is hidden and resumes when it is shown", async () => {
    const served = serveProgress([fixtures.episodeAnalysisProgress()]);
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const { queryClient } = renderDashboard();
    expect(await screen.findByText("Analyzing scene 18 of 32")).toBeVisible();
    expect(served.requests()).toBe(1);

    setVisibility("hidden");
    vi.advanceTimersByTime(EPISODE_ANALYSIS_POLL_INTERVAL_MS * 3);
    expect(statusFetching(queryClient)).toBe(0);
    expect(served.requests()).toBe(1);

    setVisibility("visible");
    vi.advanceTimersByTime(EPISODE_ANALYSIS_POLL_INTERVAL_MS);
    expect(statusFetching(queryClient)).toBe(1);
    await waitFor(() => expect(served.requests()).toBe(2));
  });
});
