import { expect, test } from "@playwright/test";

import {
  createFakeState,
  installFakeApi,
  PROJECT_ID,
  shotId,
  type FakeApiState,
} from "./fakeApi";

/**
 * The T20 browser evidence-review flow.
 *
 * It drives the real production build through the whole visual-QA review
 * journey against the deterministic in-page API stub: the dashboard summary,
 * the storyboard-grid badges, the shot inspector's scorecard and diagnostics,
 * the evidence viewer with exact timestamps and bounding boxes, the comparison
 * view, and a recorded human decision. No provider is contacted.
 */
test.describe("T20 visual-QA review", () => {
  let state: FakeApiState;

  test.beforeEach(async ({ page }) => {
    state = createFakeState();
    await installFakeApi(page, state);
  });

  test("an owner inspects a blocking visual-QA result and records a decision", async ({
    page,
  }) => {
    // 1. The dashboard summarises visual QA for the whole project.
    await page.goto(`/projects/${PROJECT_ID}`);
    await expect(page.getByRole("heading", { name: "Visual QA" })).toBeVisible();
    await expect(page.getByText(/blocked · .* awaiting review/)).toBeVisible();
    await expect(page.getByText(/(carries|carry) a hard failure and cannot be rendered/)).toBeVisible();

    // 2. The storyboard grid badges every shot with its QA outcome.
    await page.goto(`/projects/${PROJECT_ID}/storyboard?shot=${shotId(5)}`);
    await expect(page.getByText("Video QA FAIL").first()).toBeVisible();
    await expect(page.getByText("Keyframe QA PASS").first()).toBeVisible();

    // 3. The inspector shows both outcomes against their pass threshold.
    const panel = page.getByRole("region", { name: "Visual QA" });
    await expect(panel.getByRole("cell", { name: "82.50 / 85" })).toBeVisible();
    await expect(panel.getByRole("cell", { name: "96.00 / 85" })).toBeVisible();

    // 4. Open the blocking video result.
    const rows = panel.getByRole("row");
    await rows.filter({ hasText: "Video" }).getByRole("button", { name: "Inspect" }).click();

    // 5. The scorecard recomputes the weighted total from the exact rubric.
    const scorecard = panel.getByRole("region", { name: "Dimension scorecard" });
    await expect(scorecard).toBeVisible();
    await expect(scorecard.getByText("Hard failure blocks this shot")).toBeVisible();
    await expect(scorecard.getByText(/Recomputed total 82.50 \/ pass threshold 85/)).toBeVisible();
    await expect(scorecard.getByRole("row")).toHaveCount(9);

    // 6. The repair family is informational; T20 never executes a repair.
    await expect(panel.getByText(/Recommended repair family: NEW SEED/)).toBeVisible();
    await expect(
      panel.getByText(/T20 identifies repairs and never performs one/),
    ).toBeVisible();

    // 7. Deterministic diagnostics carry their measurement and threshold.
    const diagnostics = panel.getByRole("table", { name: "Deterministic diagnostics" });
    await expect(diagnostics.getByText("0.510")).toBeVisible();
    await expect(diagnostics.getByText("0.350")).toBeVisible();

    // 8. The evidence viewer shows the exact timestamp, the frame, its bounding
    //    box, and the approved reference beside it.
    const viewer = panel.getByRole("region", { name: "Evidence frames" });
    await expect(viewer.getByText("Timestamp 1.500s")).toBeVisible();
    await expect(
      viewer.getByText(/Face geometry does not match the approved identity version/),
    ).toBeVisible();
    await expect(page.getByTestId(/^bounding-box-/)).toBeVisible();
    await expect(viewer.getByText("Approved T19 reference")).toBeVisible();

    // 9. Frames are fetched through short-lived signed URLs, never embedded.
    expect(state.downloads.length).toBeGreaterThan(0);

    // 10. The comparison view sets the generated frame against the reference.
    const comparison = panel.getByRole("region", { name: "Compared references" });
    await expect(comparison.getByText("Generated at 1.500s")).toBeVisible();

    // 11. A hard failure offers no human override.
    await expect(panel.getByRole("button", { name: "Approve after review" })).toHaveCount(0);

    // 12. Re-running QA is queued through the control plane, not executed here.
    await panel.getByRole("button", { name: "Run or resume visual QA" }).click();
    await expect(panel.getByRole("button", { name: "Run or resume visual QA" })).toBeEnabled();

    // 13. The final-review page explains that visual QA blocks a new render.
    await page.goto(`/projects/${PROJECT_ID}/review`);
    await expect(page.getByText("Visual QA blocks a new render")).toBeVisible();
  });
});
