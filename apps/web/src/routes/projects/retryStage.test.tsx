import { type QueryClient } from "@tanstack/react-query";
import { fireEvent, screen, waitFor } from "@testing-library/react";
import { HttpResponse, http } from "msw";
import { Route, Routes } from "react-router-dom";
import { describe, expect, it } from "vitest";

import type { WorkflowStatusProjection } from "@vidgen/contracts";

import { apiError } from "../../test/handlers";
import * as fixtures from "../../test/fixtures";
import { renderWithProviders } from "../../test/render";
import { server } from "../../test/server";
import { ProjectDashboardPage } from "./ProjectDashboardPage";

const BASE = "http://localhost";
const { PROJECT_ID } = fixtures;

async function settle(queryClient: QueryClient): Promise<void> {
  await waitFor(() => expect(queryClient.isFetching()).toBe(0));
}

function renderDashboard() {
  return renderWithProviders(
    <Routes>
      <Route path="/projects/:projectId/*" element={<ProjectDashboardPage />} />
    </Routes>,
    { route: `/projects/${PROJECT_ID}` },
  );
}

/** A workflow whose keyframes stage exhausted its retries. */
function stalledWorkflow(
  overrides: Partial<WorkflowStatusProjection> = {},
): WorkflowStatusProjection {
  return {
    ...fixtures.workflowStatus,
    status: "shot_generation",
    current_stage: "shot_orchestration",
    cancelled: false,
    stages: fixtures.workflowStatus.stages.map((stage) =>
      stage.stage === "keyframes"
        ? { ...stage, state: "failed" as const, detail_code: "provider_exhausted" }
        : stage.stage === "captions" || stage.stage === "rendering" || stage.stage === "review"
          ? { ...stage, state: "pending" as const }
          : stage,
    ),
    ...overrides,
  };
}

/**
 * Serve `/workflow` from a mutable projection, and record the retry requests.
 *
 * The cancel endpoint returns before the run has actually stopped, exactly as
 * the real one does, so the page has to poll `/workflow` to see `cancelled`
 * turn true before it may continue.
 */
function installRetryApi(
  options: { readonly cancelTakesPolls?: number; readonly continueFails?: boolean } = {},
) {
  const calls: string[] = [];
  const continueBodies: Array<Record<string, unknown>> = [];
  const idempotencyKeys: string[] = [];
  let pollsLeft = options.cancelTakesPolls ?? 0;
  let cancelled = false;

  server.use(
    http.get(`${BASE}/api/v1/projects/:projectId/workflow`, () => {
      calls.push("get");
      if (!cancelled) {
        return HttpResponse.json(stalledWorkflow());
      }
      if (pollsLeft > 0) {
        pollsLeft -= 1;
        return HttpResponse.json(stalledWorkflow({ cancelled: false }));
      }
      return HttpResponse.json(stalledWorkflow({ cancelled: true, status: "cancelled" }));
    }),
    http.post(/\/workflow:cancel$/, ({ request }) => {
      calls.push("cancel");
      idempotencyKeys.push(request.headers.get("Idempotency-Key") ?? "");
      cancelled = true;
      // Still `false` here: the signal was sent, the run has not stopped yet.
      return HttpResponse.json(stalledWorkflow({ cancelled: false }));
    }),
    http.post(/\/workflow:continue$/, async ({ request }) => {
      calls.push("continue");
      idempotencyKeys.push(request.headers.get("Idempotency-Key") ?? "");
      continueBodies.push((await request.json()) as Record<string, unknown>);
      if (options.continueFails) {
        return HttpResponse.json(
          apiError("validation_failed", "Select a narration voice profile first."),
          { status: 409 },
        );
      }
      return HttpResponse.json({ command: fixtures.commands.items[0] }, { status: 202 });
    }),
  );

  return { calls, continueBodies, idempotencyKeys };
}

describe("ProjectDashboardPage stage retry", () => {
  it("offers a Retry button only for the stage that failed", async () => {
    installRetryApi();
    const { queryClient } = renderDashboard();
    await settle(queryClient);

    expect(await screen.findByTestId("retry-stage-keyframes")).toBeVisible();
    expect(screen.queryByTestId("retry-stage-animation")).toBeNull();
    expect(screen.queryByTestId("retry-stage-narration")).toBeNull();
  });

  it("shows no Retry button while every stage is running or complete", async () => {
    const { queryClient } = renderDashboard();
    await settle(queryClient);

    expect(screen.queryByTestId("failure-panel-retry")).toBeNull();
    for (const stage of fixtures.workflowStatus.stages) {
      expect(screen.queryByTestId(`retry-stage-${stage.stage}`)).toBeNull();
    }
  });

  it("cancels the live run, waits for it to stop, then continues from the stage", async () => {
    const api = installRetryApi({ cancelTakesPolls: 1 });
    const { queryClient } = renderDashboard();
    await settle(queryClient);

    fireEvent.click(await screen.findByTestId("retry-stage-keyframes"));

    // The flow waits a poll interval for the cancel to take effect.
    await waitFor(() => expect(api.calls).toContain("continue"), { timeout: 5_000 });
    // A continuation must never be sent before the cancel it depends on.
    expect(api.calls.indexOf("cancel")).toBeLessThan(api.calls.indexOf("continue"));
    expect(api.continueBodies).toEqual([
      { entry_stage: "shot_generation", reason: "remediation" },
    ]);
    // Both requests carried their own key, so neither replays the other.
    expect(api.idempotencyKeys).toHaveLength(2);
    expect(new Set(api.idempotencyKeys).size).toBe(2);
    expect(api.idempotencyKeys.every((key) => key !== "")).toBe(true);
  });

  it("disables the button and reports success once the retry is queued", async () => {
    installRetryApi();
    const { queryClient } = renderDashboard();
    await settle(queryClient);

    const button = await screen.findByTestId("retry-stage-keyframes");
    fireEvent.click(button);
    await waitFor(() => expect(button).toBeDisabled());

    expect(await screen.findByText("Retrying Shot generation", {}, { timeout: 5_000 })).toBeVisible();
    expect(
      await screen.findByText("The running workflow was cancelled and a new run was queued."),
    ).toBeVisible();
  });

  it("reports a rejected continuation instead of claiming the retry started", async () => {
    installRetryApi({ continueFails: true });
    const { queryClient } = renderDashboard();
    await settle(queryClient);

    fireEvent.click(await screen.findByTestId("retry-stage-keyframes"));

    expect(
      await screen.findByText("The retry could not be started", {}, { timeout: 5_000 }),
    ).toBeVisible();
    expect(screen.queryByText(/A new run was queued/)).toBeNull();
  });

  it("offers the same retry from the failures panel, naming the stalled stage", async () => {
    const api = installRetryApi();
    const { queryClient } = renderDashboard();
    await settle(queryClient);

    const button = await screen.findByTestId("failure-panel-retry");
    expect(button).toHaveTextContent("Retry Keyframes");
    fireEvent.click(button);

    await waitFor(() => expect(api.continueBodies).toHaveLength(1), { timeout: 5_000 });
    expect(api.continueBodies[0]).toEqual({
      entry_stage: "shot_generation",
      reason: "remediation",
    });
  });
});
