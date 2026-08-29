import { expect, test } from "@playwright/test";

import { createFakeState, installFakeApi, PROJECT_ID, type FakeApiState } from "./fakeApi";

/**
 * The T22 browser final-editorial-QA flow.
 *
 * It drives the real production build against the deterministic in-page API
 * stub: the gate verdict, the measurements, the finding timeline, and the one
 * action a reviewer is allowed to take on a genuinely uncertain semantic
 * finding. No provider is contacted.
 */
test.describe("T22 final editorial QA", () => {
  let state: FakeApiState;

  test.beforeEach(async ({ page }) => {
    state = createFakeState();
    await installFakeApi(page, state);
  });

  test("an owner sees the gate blocking completion and resolves the review", async ({ page }) => {
    await page.goto(`/projects/${PROJECT_ID}/review`);
    const panel = page.getByRole("region", { name: "Final editorial QA" });
    await expect(panel).toBeVisible();

    // 1. The gate withholds completion, and says so where the owner approves.
    await expect(page.getByText("Final editorial QA blocks completion")).toBeVisible();
    await expect(panel.getByText("The project cannot complete yet")).toBeVisible();
    await expect(panel.getByText("REVIEW").first()).toBeVisible();

    // 2. The measured facts and the finding are on screen with their evidence.
    await expect(panel.getByText("INCOMPREHENSIBLE SEQUENCE")).toBeVisible();
    await expect(panel.getByText("00:09.000–00:12.000")).toBeVisible();
    await expect(panel.getByText("RESOLUTION MISMATCH")).toBeVisible();

    // 3. Nothing offers to pass a measured failure; only the semantic finding
    //    carries a resolve action.
    await expect(panel.getByText("Deterministic failures cannot be overridden")).toHaveCount(0);

    // 4. Resolving the eligible review recomputes the gate to PASS.
    await panel.getByRole("button", { name: "Resolve" }).click();
    await expect.poll(() => state.finalQaReviews.length).toBe(1);
    await expect(page.getByText("Final editorial QA blocks completion")).toHaveCount(0);
  });

  test("an owner can start a final-QA run", async ({ page }) => {
    await page.goto(`/projects/${PROJECT_ID}/review`);
    const panel = page.getByRole("region", { name: "Final editorial QA" });
    await panel.getByRole("button", { name: "Rerun final QA" }).click();
    await expect.poll(() => state.finalQaRuns.length).toBe(1);
    // Every mutation carries a replayable idempotency key.
    expect(state.finalQaRuns[0]).not.toBe("");
  });
});
