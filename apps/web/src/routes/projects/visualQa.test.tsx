import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpResponse, http } from "msw";
import type { JSX } from "react";
import { Route, Routes } from "react-router-dom";
import { describe, expect, it } from "vitest";

import * as fixtures from "../../test/fixtures";
import { renderWithProviders } from "../../test/render";
import { server } from "../../test/server";
import { FinalReviewPage } from "./FinalReviewPage";
import { ProjectDashboardPage } from "./ProjectDashboardPage";
import { StoryboardPage } from "./StoryboardPage";

function renderProjectRoute(element: JSX.Element, route: string) {
  return renderWithProviders(
    <Routes>
      <Route path="/projects/:projectId/*" element={element} />
    </Routes>,
    { route },
  );
}

const { PROJECT_ID } = fixtures;

function storyboardRoute(index: number): string {
  const shotId = fixtures.storyboard.shots[index]!.shot_id;
  return `/projects/${PROJECT_ID}/storyboard?shot=${shotId}`;
}

async function openVisualQa(index = 5): Promise<HTMLElement> {
  renderProjectRoute(<StoryboardPage />, storyboardRoute(index));
  const panel = await screen.findByRole("region", { name: "Visual QA" });
  await userEvent.click(within(panel).getAllByRole("button", { name: "Inspect" })[0]!);
  return panel;
}

describe("VisualQAResultPanel", () => {
  it("shows the keyframe and video outcomes with the recomputed score", async () => {
    renderProjectRoute(<StoryboardPage />, storyboardRoute(5));
    const panel = await screen.findByRole("region", { name: "Visual QA" });
    expect(within(panel).getByText(/Keyframe QA:/)).toBeVisible();
    expect(within(panel).getByText(/Video QA:/)).toBeVisible();
    // The compact table shows each run's recomputed score against its threshold.
    expect(within(panel).getByText("96.00 / 85")).toBeVisible();
    expect(within(panel).getByText("82.50 / 85")).toBeVisible();
  });

  it("renders the dimension scorecard with the exact rubric weights", async () => {
    const panel = await openVisualQa();
    const scorecard = await within(panel).findByRole("region", {
      name: "Dimension scorecard",
    });
    const table = within(scorecard).getByRole("table");
    const rows = within(table).getAllByRole("row");
    // Eight rubric dimensions plus the header row.
    expect(rows).toHaveLength(9);
    expect(within(table).getByText("Character identity")).toBeVisible();
    expect(within(table).getAllByText("25.00").length).toBeGreaterThan(0);
    expect(within(table).getAllByText("10.00").length).toBeGreaterThan(0);
  });

  it("marks a hard failure and shows its repair code", async () => {
    const panel = await openVisualQa();
    const scorecard = await within(panel).findByRole("region", { name: "Dimension scorecard" });
    expect(
      within(scorecard).getByText("Hard failure blocks this shot"),
    ).toBeVisible();
    expect(within(scorecard).getAllByText("WRONG_CHARACTER_IDENTITY").length).toBeGreaterThan(0);
  });

  it("displays the recommended repair family as informational only", async () => {
    const panel = await openVisualQa();
    expect(
      await within(panel).findByText(/Recommended repair family: NEW SEED/),
    ).toBeVisible();
    expect(
      within(panel).getByText(/T20 identifies repairs and never performs one/),
    ).toBeVisible();
    // No repair or regeneration action is offered by the QA panel itself.
    expect(within(panel).queryByRole("button", { name: /repair/i })).toBeNull();
  });

  it("shows deterministic diagnostics with their measured values", async () => {
    const panel = await openVisualQa();
    const table = await within(panel).findByRole("table", {
      name: "Deterministic diagnostics",
    });
    expect(within(table).getByText("Freeze ratio")).toBeVisible();
    expect(within(table).getByText("0.510")).toBeVisible();
    expect(within(table).getByText("0.350")).toBeVisible();
  });
});

describe("VisualQAEvidenceViewer", () => {
  it("shows each evidence frame at its exact timestamp with its bounding box", async () => {
    const panel = await openVisualQa();
    const viewer = await within(panel).findByRole("region", { name: "Evidence frames" });
    expect(within(viewer).getByText("Timestamp 1.500s")).toBeVisible();
    const evidence = fixtures.visualQaEvidence(0).items[0]!;
    await waitFor(() =>
      expect(screen.getByTestId(`bounding-box-${evidence.evidence_id}`)).toBeVisible(),
    );
    expect(
      within(viewer).getByText(/Face geometry does not match the approved identity version/),
    ).toBeVisible();
  });

  it("refreshes signed asset URLs on request and stays keyboard accessible", async () => {
    let requests = 0;
    server.use(
      http.get(/\/api\/v1\/assets\/[^/]+\/download-url$/, ({ request }) => {
        requests += 1;
        const assetId = new URL(request.url).pathname.split("/").at(-2)!;
        return HttpResponse.json({
          asset_id: assetId,
          url: `http://localhost/blobs/${assetId}?sig=fresh-${requests}`,
          expires_in_seconds: 900,
        });
      }),
    );
    const panel = await openVisualQa();
    const viewer = await within(panel).findByRole("region", { name: "Evidence frames" });
    const refresh = within(viewer).getByRole("button", { name: "Refresh frames" });
    const before = requests;
    refresh.focus();
    expect(refresh).toHaveFocus();
    await userEvent.keyboard("{Enter}");
    await waitFor(() => expect(requests).toBeGreaterThan(before));
  });
});

describe("VisualQAComparisonPanel", () => {
  it("shows the sampled frame beside the approved reference", async () => {
    const panel = await openVisualQa();
    const comparison = await within(panel).findByRole("region", {
      name: "Compared references",
    });
    expect(within(comparison).getByText("Generated at 0.000s")).toBeVisible();
    expect(within(comparison).getByText("Approved T19 reference")).toBeVisible();
  });
});

describe("VisualQAReviewDialog", () => {
  it("refuses to let a human clear a hard failure", async () => {
    server.use(
      http.get(/\/api\/v1\/projects\/[^/]+\/shots\/[^/]+\/visual-qa$/, () =>
        HttpResponse.json({
          project_id: PROJECT_ID,
          items: [
            fixtures.visualQaRun(0, {
              outcome: "REVIEW",
              hard_failure: true,
              repair_codes: ["HUMAN_REVIEW_REQUIRED"],
            }),
          ],
        }),
      ),
      http.get(/\/api\/v1\/projects\/[^/]+\/shots\/[^/]+\/visual-qa\/[^/]+$/, () =>
        HttpResponse.json(
          fixtures.visualQaDetail(0, {
            outcome: "REVIEW",
            hard_failure: true,
            repair_codes: ["HUMAN_REVIEW_REQUIRED"],
          }),
        ),
      ),
    );
    const panel = await openVisualQa();
    await userEvent.click(
      await within(panel).findByRole("button", { name: "Approve after review" }),
    );
    const dialog = await screen.findByRole("dialog");
    expect(
      within(dialog).getByText(/hard failure is a measured fact and cannot be cleared/),
    ).toBeVisible();
    expect(within(dialog).getByRole("button", { name: "Record approval" })).toBeDisabled();
  });
});

describe("visual-QA status across the review UI", () => {
  it("badges every shot in the storyboard grid with its QA outcome", async () => {
    renderProjectRoute(<StoryboardPage />, storyboardRoute(0));
    const grid = await screen.findAllByText(/QA PASS|QA FAIL|QA REVIEW/);
    expect(grid.length).toBeGreaterThan(0);
  });

  it("summarises visual QA on the project dashboard", async () => {
    renderProjectRoute(<ProjectDashboardPage />, `/projects/${PROJECT_ID}`);
    expect(await screen.findByRole("heading", { name: "Visual QA" })).toBeVisible();
    expect(await screen.findByText(/blocked · .* awaiting review/)).toBeVisible();
  });

  it("explains that visual QA blocks a new render on the final review page", async () => {
    renderProjectRoute(<FinalReviewPage />, `/projects/${PROJECT_ID}/review`);
    expect(
      await screen.findByText("Visual QA blocks a new render"),
    ).toBeVisible();
  });
});
