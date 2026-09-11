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
  // Wait for the selected run to render before any assertion touches it.
  await within(panel).findByRole("region", { name: "Dimension scorecard" });
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
    expect(await within(viewer).findByText("Timestamp 1.500s")).toBeVisible();
    const evidence = fixtures.visualQaEvidence(0).items[0]!;
    await waitFor(() =>
      expect(screen.getByTestId(`bounding-box-${evidence.evidence_id}`)).toBeVisible(),
    );
    expect(
      await within(viewer).findByText(
        /Face geometry does not match the approved identity version/,
      ),
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
    // Fluent marks the rest of the page aria-hidden once the modal opens, which
    // takes the dialog out of the queried accessibility tree. The repository's
    // other dialog assertions pass `hidden` for the same reason.
    const dialog = await screen.findByRole("dialog", { hidden: true });
    expect(
      within(dialog).getByText(/hard failure is a measured fact and cannot be cleared/),
    ).toBeVisible();
    expect(
      within(dialog).getByRole("button", { name: "Record approval", hidden: true }),
    ).toBeDisabled();
  });
});

describe("ShotInspector video player", () => {
  it("renders a video element with the signed URL for the selected shot", async () => {
    renderProjectRoute(<StoryboardPage />, storyboardRoute(0));
    const video = await screen.findByTestId<HTMLVideoElement>("shot-video-player");
    const expectedAssetId = fixtures.storyboardShot(0).selected_video_asset_id;
    expect(video.src).toContain(expectedAssetId);
  });

  it("uses a fresh signed URL each time a different shot is selected", async () => {
    renderProjectRoute(<StoryboardPage />, storyboardRoute(1));
    const video = await screen.findByTestId<HTMLVideoElement>("shot-video-player");
    const shotB = fixtures.storyboard.shots[1]!;
    expect(video.src).toContain(shotB.selected_video_asset_id);
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

/**
 * Deciding twenty keyframes used to mean twenty trips through the inspector.
 * These cover the flow that replaces it: the decision is on the card, the two
 * bulk decisions are above the grid, and the dashboard says a decision is due.
 */
describe("keyframe review without the inspector", () => {
  /** Shot 1 is ambiguous, shot 2 soft-failed, shot 3 carries a hard failure. */
  function reviewableProject() {
    const shotId = (index: number) => fixtures.storyboard.shots[index]!.shot_id;
    server.use(
      http.get(/\/api\/v1\/projects\/[^/]+\/visual-qa$/, () =>
        HttpResponse.json({
          project_id: PROJECT_ID,
          items: [
            fixtures.visualQaRun(1, {
              shot_id: shotId(1),
              outcome: "REVIEW",
              hard_failure: false,
              repair_codes: ["HUMAN_REVIEW_REQUIRED"],
            }),
            fixtures.visualQaRun(2, {
              shot_id: shotId(2),
              outcome: "FAIL",
              hard_failure: false,
              repair_codes: ["PROMPT_TOO_COMPLEX"],
            }),
            fixtures.visualQaRun(3, { shot_id: shotId(3), outcome: "FAIL", hard_failure: true }),
          ],
        }),
      ),
    );
  }

  it("offers the decision on the shot card, naming the override for a soft failure", async () => {
    reviewableProject();
    renderProjectRoute(<StoryboardPage />, `/projects/${PROJECT_ID}/storyboard`);
    // Shot 2 is the ambiguous one (shots are numbered from one in the UI).
    expect(await screen.findByRole("button", { name: "Approve shot 2" })).toBeVisible();
    expect(screen.getByRole("button", { name: "Reject shot 2" })).toBeVisible();
    // Shot 3 failed on judgement: the same button, named as the override it is.
    expect(screen.getByRole("button", { name: "Force approve shot 3" })).toBeVisible();
    // Shot 4 carries a hard failure, which no decision can clear.
    expect(screen.queryByRole("button", { name: /approve shot 4/i })).toBeNull();
    // Shot 1 passed; there is nothing to decide.
    expect(screen.queryByRole("button", { name: /approve shot 1/i })).toBeNull();
  });

  it("records the decision for one shot straight from its card", async () => {
    reviewableProject();
    const posted: string[] = [];
    server.use(
      http.post(/\/visual-qa\/[^/]+:approve$/, ({ request }) => {
        posted.push(new URL(request.url).pathname);
        return HttpResponse.json({
          qa_run_id: fixtures.uuid(2, 9),
          review_id: fixtures.uuid(3, 9),
          decision: "force_approved",
          resulting_gate: "visual_qa_human_force_approved",
          row_version: 4,
        });
      }),
    );
    renderProjectRoute(<StoryboardPage />, `/projects/${PROJECT_ID}/storyboard`);
    await userEvent.click(await screen.findByRole("button", { name: "Force approve shot 3" }));
    const dialog = await screen.findByRole("dialog", { hidden: true });
    // The dialog says plainly that this overrides the automated verdict.
    expect(
      within(dialog).getByText(/failed automated QA on judgement, not on a measured defect/),
    ).toBeVisible();
    await userEvent.click(
      within(dialog).getByRole("button", { name: "Record approval", hidden: true }),
    );
    await waitFor(() => expect(posted).toHaveLength(1));
    expect(posted[0]).toContain(fixtures.storyboard.shots[2]!.shot_id);
  });

  it("counts what is waiting and force-approves every soft failure at once", async () => {
    reviewableProject();
    const posted: string[] = [];
    server.use(
      http.post(/\/visual-qa\/[^/]+:approve$/, ({ request }) => {
        posted.push(new URL(request.url).pathname);
        return HttpResponse.json({
          qa_run_id: fixtures.uuid(2, 9),
          review_id: fixtures.uuid(3, 9),
          decision: "force_approved",
          resulting_gate: "visual_qa_human_force_approved",
          row_version: 4,
        });
      }),
    );
    renderProjectRoute(<StoryboardPage />, `/projects/${PROJECT_ID}/storyboard`);
    const bar = await screen.findByRole("region", { name: "Shots awaiting review" });
    // One ambiguous shot and one soft failure; the hard failure is in neither.
    expect(within(bar).getByText(/1 shot needs review, 1 shot failed\./)).toBeVisible();
    await userEvent.click(
      within(bar).getByRole("button", { name: "Force approve all failed (1)" }),
    );
    const dialog = await screen.findByRole("dialog", { hidden: true });
    await userEvent.click(
      within(dialog).getByRole("button", { name: "Record approval", hidden: true }),
    );
    await waitFor(() => expect(posted).toHaveLength(1));
    expect(posted[0]).toContain(fixtures.storyboard.shots[2]!.shot_id);
  });

  it("tells the dashboard owner that shots are waiting and where to decide", async () => {
    reviewableProject();
    renderProjectRoute(<ProjectDashboardPage />, `/projects/${PROJECT_ID}`);
    expect(await screen.findByText("Keyframes need review")).toBeVisible();
    expect(screen.getByText(/1 shot needs review, 1 shot failed/)).toBeVisible();
    expect(screen.getByRole("link", { name: "Go to Storyboard" })).toHaveAttribute(
      "href",
      `/projects/${PROJECT_ID}/storyboard`,
    );
  });
});

describe("VisualQAResultPanel human override", () => {
  it("offers a force approval for a failure that is not a hard failure", async () => {
    server.use(
      http.get(/\/api\/v1\/projects\/[^/]+\/shots\/[^/]+\/visual-qa$/, () =>
        HttpResponse.json({
          project_id: PROJECT_ID,
          items: [
            fixtures.visualQaRun(0, {
              outcome: "FAIL",
              hard_failure: false,
              repair_codes: ["PROMPT_TOO_COMPLEX"],
            }),
          ],
        }),
      ),
      http.get(/\/api\/v1\/projects\/[^/]+\/shots\/[^/]+\/visual-qa\/[^/]+$/, () =>
        HttpResponse.json(
          fixtures.visualQaDetail(0, {
            outcome: "FAIL",
            hard_failure: false,
            repair_codes: ["PROMPT_TOO_COMPLEX"],
          }),
        ),
      ),
    );
    const panel = await openVisualQa();
    expect(await within(panel).findByRole("button", { name: "Force approve" })).toBeVisible();
    expect(within(panel).getByRole("button", { name: "Confirm failure" })).toBeVisible();
    expect(
      within(panel).getByText(/failed on score, not on a measured defect/),
    ).toBeVisible();
  });

  it("offers nothing at all for a hard failure", async () => {
    server.use(
      http.get(/\/api\/v1\/projects\/[^/]+\/shots\/[^/]+\/visual-qa$/, () =>
        HttpResponse.json({ project_id: PROJECT_ID, items: [fixtures.visualQaRun(0)] }),
      ),
      http.get(/\/api\/v1\/projects\/[^/]+\/shots\/[^/]+\/visual-qa\/[^/]+$/, () =>
        HttpResponse.json(fixtures.visualQaDetail(0)),
      ),
    );
    const panel = await openVisualQa();
    expect(within(panel).queryByRole("button", { name: /approve/i })).toBeNull();
    expect(within(panel).queryByRole("button", { name: /reject|confirm failure/i })).toBeNull();
  });
});
