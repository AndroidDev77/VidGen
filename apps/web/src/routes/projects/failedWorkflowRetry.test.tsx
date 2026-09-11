import { type QueryClient } from "@tanstack/react-query";
import { fireEvent, screen, waitFor } from "@testing-library/react";
import { HttpResponse, http } from "msw";
import { Route, Routes } from "react-router-dom";
import { describe, expect, it } from "vitest";

import type { PipelineFailureListItem, WorkflowStatusProjection } from "@vidgen/contracts";

import * as fixtures from "../../test/fixtures";
import { renderWithProviders } from "../../test/render";
import { server } from "../../test/server";
import { ProjectDashboardPage } from "./ProjectDashboardPage";

/**
 * The dashboard for a project whose workflow died.
 *
 * A failed Temporal execution answers no query, so the workflow projection
 * arrives with nothing running and nothing failed - every stage pending. The
 * only witness left is the failure the stage recorded on its way out, and these
 * tests pin that the dashboard reads it and offers the way back.
 */

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

/** What `GET /workflow` returns once the API has reconciled a dead execution. */
const failedWorkflow: WorkflowStatusProjection = {
  ...fixtures.workflowStatus,
  status: "failed",
  current_stage: null,
  cancelled: false,
  stages: fixtures.workflowStatus.stages.map((stage) => ({
    ...stage,
    state: "pending" as const,
  })),
};

function failure(overrides: Partial<PipelineFailureListItem> = {}): PipelineFailureListItem {
  return {
    id: fixtures.failures.items[0]!.id,
    workflowId: `vidgen-project-${PROJECT_ID}`,
    stage: "script_generation",
    failureClass: "contract_validation",
    errorCode: "COMPRESSION_VALIDATION_FAILED",
    retryable: false,
    status: "script_generation_failed",
    createdAt: "2026-08-01T12:00:00Z",
    resolvedAt: null,
    ...overrides,
  };
}

function installFailedProject(
  options: { readonly failures?: readonly PipelineFailureListItem[] } = {},
) {
  const continueBodies: Array<Record<string, unknown>> = [];
  const cancels: string[] = [];
  server.use(
    http.get(`${BASE}/api/v1/projects/:projectId/workflow`, () =>
      HttpResponse.json(failedWorkflow),
    ),
    http.get(`${BASE}/api/v1/projects/:projectId/failures`, () =>
      HttpResponse.json({ items: options.failures ?? [failure()] }),
    ),
    http.post(/\/workflow:cancel$/, () => {
      cancels.push("cancel");
      return HttpResponse.json(failedWorkflow);
    }),
    http.post(/\/workflow:continue$/, async ({ request }) => {
      continueBodies.push((await request.json()) as Record<string, unknown>);
      return HttpResponse.json({ command: fixtures.commands.items[0] }, { status: 202 });
    }),
  );
  return { continueBodies, cancels };
}

describe("ProjectDashboardPage failed-workflow recovery", () => {
  it("shows the failure that stopped the project", async () => {
    installFailedProject();
    const { queryClient } = renderDashboard();
    await settle(queryClient);

    const banner = await screen.findByTestId("project-failure-banner");
    expect(banner).toHaveTextContent("Script failed");
    expect(banner).toHaveTextContent("COMPRESSION VALIDATION FAILED");
    expect(banner).toHaveTextContent("Contract validation");
  });

  it("offers a retry from the stage the failure names", async () => {
    const api = installFailedProject();
    const { queryClient } = renderDashboard();
    await settle(queryClient);

    const button = await screen.findByTestId("project-failure-retry");
    expect(button).toHaveTextContent("Retry from Script");
    fireEvent.click(button);

    await waitFor(() => expect(api.continueBodies).toHaveLength(1), { timeout: 5_000 });
    expect(api.continueBodies[0]).toEqual({
      entry_stage: "script_generation",
      reason: "remediation",
    });
    // Nothing is running, so there is nothing to cancel first.
    expect(api.cancels).toEqual([]);
  });

  it("says nothing when every recorded failure was resolved", async () => {
    installFailedProject({
      failures: [failure({ resolvedAt: "2026-08-01T12:05:00Z", status: "recovered" })],
    });
    const { queryClient } = renderDashboard();
    await settle(queryClient);

    expect(screen.queryByTestId("project-failure-banner")).toBeNull();
  });

  it("reports the newest unresolved failure, not whichever arrived first", async () => {
    installFailedProject({
      failures: [
        failure({ stage: "narration", createdAt: "2026-08-01T09:00:00Z" }),
        failure({
          id: fixtures.failures.items[0]!.id,
          stage: "script_generation",
          createdAt: "2026-08-01T12:00:00Z",
        }),
      ],
    });
    const { queryClient } = renderDashboard();
    await settle(queryClient);

    expect(await screen.findByTestId("project-failure-banner")).toHaveTextContent("Script failed");
  });

  it("says nothing about a failure the pipeline retried past", async () => {
    // A running project with an unresolved-but-recovered provider timeout: the
    // dashboard must not tell the owner to retry a project that is working.
    server.use(
      http.get(`${BASE}/api/v1/projects/:projectId/workflow`, () =>
        HttpResponse.json(fixtures.workflowStatus),
      ),
      http.get(`${BASE}/api/v1/projects/:projectId/failures`, () =>
        HttpResponse.json({
          items: [failure({ stage: "animation", status: "recovered", retryable: true })],
        }),
      ),
    );
    const { queryClient } = renderDashboard();
    await settle(queryClient);

    expect(screen.queryByTestId("project-failure-banner")).toBeNull();
  });

  it("lists the failure history with when each one was recorded", async () => {
    installFailedProject();
    const { queryClient } = renderDashboard();
    await settle(queryClient);

    const table = await screen.findByRole("table", { name: "Recent pipeline failures" });
    expect(table).toHaveTextContent("Unresolved");
    expect(table).toHaveTextContent("COMPRESSION VALIDATION FAILED");
  });
});
