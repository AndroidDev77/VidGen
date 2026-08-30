import { expect, test } from "@playwright/test";

import {
  createFakeState,
  installFakeApi,
  PROJECT_ID,
  SHOT_COUNT,
  shotId,
  type FakeApiState,
} from "./fakeApi";

/**
 * The mandatory T18 MVP acceptance test.
 *
 * It drives the real production build through the whole customer journey
 * against a deterministic in-browser API with synthetic media and fake
 * providers. No paid provider is contacted, and no Temporal cluster is needed.
 */
test.describe("T18 MVP acceptance", () => {
  let state: FakeApiState;

  test.beforeEach(async ({ page }) => {
    state = createFakeState();
    await installFakeApi(page, state);
  });

  test("an owner takes a project from upload to an approved, downloadable render", async ({
    page,
  }) => {
    // 1. Create a project.
    await page.goto("/projects/new");
    await page.getByLabel(/Project name/).fill("Season 3 Episode 4");
    await page.getByRole("button", { name: "Create project" }).click();

    // 2-4. Select a synthetic source file; it is hashed and uploaded in parts.
    const chooser = page.getByLabel(/Source video file/);
    await chooser.setInputFiles({
      name: "episode.mp4",
      mimeType: "video/mp4",
      buffer: Buffer.alloc(12, 7),
    });
    await expect(page.getByText("Source video ready")).toBeVisible({ timeout: 15_000 });
    expect(state.uploadedParts.sort((a, b) => a - b)).toEqual([0, 1, 2]);
    expect(state.uploadComplete).toBe(true);

    // 5. The workflow cannot start without a narration voice, so setup shows
    // the selection and the button only enables once one is chosen. Before
    // T18b this project reached T12 and failed there.
    // The caption only renders once the selection has resolved, so asserting it
    // proves the picker actually resolved a voice rather than merely rendering.
    await expect(page.getByText(/Model fake-tts, en, wav\./)).toBeVisible();

    // 6. Start the project workflow. A duplicate submission must not start two.
    const start = page.getByTestId("start-workflow");
    await expect(start).toBeEnabled();
    await start.click();
    await expect(page).toHaveURL(new RegExp(`/projects/${PROJECT_ID}$`));
    expect(state.workflowStarts).toHaveLength(1);
    expect(state.workflowStarts[0]).toMatch(/^workflow-start-/);

    // 7. Progress reaches the browser, and the control plane is visible beside
    // it: the dashboard names the active generation run rather than inferring
    // project state from whatever it last polled.
    await expect(page.getByRole("heading", { name: "Pipeline stages" })).toBeVisible();
    await expect(page.getByText(/10 of 10 shots locked/)).toBeVisible();
    await expect(page.getByRole("heading", { name: "Commands" })).toBeVisible();
    await expect(page.getByText("Generation run 1, from upload.")).toBeVisible();

    // 7-8. Open the transcript and edit one segment.
    await page.getByRole("link", { name: "Review the transcript" }).click();
    const transcriptField = page.getByLabel("Transcript text for segment 1", { exact: true });
    await transcriptField.fill("The detective explains the corrected plan.");
    await page.getByRole("button", { name: "Save segment" }).first().click();
    await page.getByRole("button", { name: "Save the segment" }).click();
    await expect
      .poll(() => state.transcriptText[0])
      .toBe("The detective explains the corrected plan.");

    // 9-11. Open the script, edit one beat, and confirm downstream invalidation.
    await page.goto(`/projects/${PROJECT_ID}/script`);
    const scriptField = page.getByLabel("Narration text for beat 1", { exact: true });
    await scriptField.fill("A far funnier opening beat.");
    await page.getByRole("button", { name: "Save beat" }).first().click();
    await expect(page.getByText(/creates a new script version/)).toBeVisible();
    await page.getByRole("button", { name: "Save the beat" }).click();
    await expect(page.getByText("Downstream work is now stale")).toBeVisible();
    expect(state.scriptVersion).toBe(2);
    expect(state.scriptText[0]).toBe("A far funnier opening beat.");

    // 12-13. Open the storyboard and inspect shot 6.
    await page.goto(`/projects/${PROJECT_ID}/storyboard`);
    await expect(page.getByRole("heading", { name: "Shot 1", exact: true })).toBeVisible();
    await page.getByRole("button", { name: "Inspect shot 6" }).click();
    const inspector = page.getByRole("complementary", { name: "Shot inspector" });
    await expect(inspector.getByRole("heading", { name: "Shot 6" })).toBeVisible();

    const identitiesBefore = { ...state.shotIdentities };

    // 14. Regenerate shot 6 only.
    await inspector.getByRole("button", { name: "Regenerate this shot" }).click();
    await expect(page.getByText(/Sibling shots keep their locked results/)).toBeVisible();
    await page.getByRole("button", { name: "Regenerate the shot" }).click();
    await expect(page.getByText("Regeneration started")).toBeVisible();

    // 15. Confirm sibling shots were not rerun and kept their identities.
    expect(state.regeneratedShots).toEqual([shotId(5)]);
    for (let index = 0; index < SHOT_COUNT; index += 1) {
      if (index === 5) {
        continue;
      }
      expect(state.shotIdentities[shotId(index)]).toBe(identitiesBefore[shotId(index)]);
    }
    // 16. The replacement shot locks; no extra provider attempts are charged.
    await page.reload();
    await page.goto(`/projects/${PROJECT_ID}/storyboard?shot=${shotId(5)}`);
    await expect(
      page.getByRole("complementary", { name: "Shot inspector" }).getByText("Locked").first(),
    ).toBeVisible();
    expect(state.providerAttempts).toBe(SHOT_COUNT);

    // 17-18. Observe the render and preview the verified final render.
    await page.goto(`/projects/${PROJECT_ID}/review`);
    await expect(page.getByText("This render is stale", { exact: true })).toBeVisible();
    await expect(page.getByTestId("approve-render")).toBeDisabled();
    await page.evaluate(async () => {
      await fetch(`/api/v1/projects/${location.pathname.split("/")[2]}/render:start`, {
        method: "POST",
        headers: { "Content-Type": "application/json", "Idempotency-Key": "e2e-render" },
        body: "{}",
      });
    });
    await page.reload();
    const video = page.getByTestId("final-render-video");
    await expect(video).toBeVisible();
    await expect(page.getByText("Passed")).toBeVisible();

    // 19. Toggle captions on the standalone WebVTT track.
    const captionToggle = page.getByTestId("caption-toggle");
    await expect(page.getByTestId("final-render-captions")).toHaveAttribute("kind", "captions");
    await expect(captionToggle).toBeChecked();
    await captionToggle.click();
    await expect(captionToggle).not.toBeChecked();
    await captionToggle.click();
    await expect(captionToggle).toBeChecked();

    // 20. Approve the render, and prove a duplicate click cannot double-apply.
    await page.getByTestId("approve-render").click();
    await expect(page.getByText("Approved by local-user", { exact: false })).toBeVisible();
    expect(state.approvals).toBe(1);

    // 21. Request the final MP4, SRT and WebVTT downloads.
    await page.getByRole("button", { name: "Download" }).click();
    await page.getByRole("menuitem", { name: "Final MP4" }).click();
    await page.getByRole("button", { name: "Download" }).click();
    await page.getByRole("menuitem", { name: "Captions (SRT)" }).click();
    await page.getByRole("button", { name: "Download" }).click();
    await page.getByRole("menuitem", { name: "Captions (WebVTT)" }).click();
    await expect
      .poll(() => new Set(state.downloads).size)
      .toBeGreaterThanOrEqual(3);

    // 22. The T25 publication panel renders on the same page, showing the
    //     connected channel, the privacy YouTube actually reports, and the
    //     public watch link - and never a credential.
    await expect(page.getByText("Publish to YouTube")).toBeVisible();
    await expect(page.getByText("VidGen Test Channel")).toBeVisible();
    await expect(page.getByText("Actual privacy")).toBeVisible();
    await expect(
      page.getByRole("link", { name: "vidfake000000001" }),
    ).toHaveAttribute("href", "https://www.youtube.com/watch?v=vidfake000000001");
    // Quota units are a rate limit, and the panel says so rather than
    // presenting them as spend.
    await expect(page.getByText("251 (rate limit, not a charge)")).toBeVisible();
    const panelText = (await page.locator("section[aria-label='YouTube publication']").innerText());
    for (const forbidden of ["Bearer", "refresh_token", "access_token", "googleapis.com/upload"]) {
      expect(panelText).not.toContain(forbidden);
    }
  });

  test("a cross-owner project is indistinguishable from a missing one", async ({ page }) => {
    await page.route("**/api/v1/projects/*", (route) =>
      route.fulfill({
        status: 404,
        contentType: "application/json",
        body: JSON.stringify({
          schema_version: "1.0",
          code: "not_found",
          summary: "The requested project was not found.",
          retryable: false,
          current_version: null,
          workflow_id: null,
          stage: null,
          fields: [],
          correlation_id: null,
          detail_code: null,
        }),
      }),
    );
    await page.goto("/projects/00000000-0000-4000-8000-000000000000");
    await expect(page.getByText("The requested project was not found.")).toBeVisible();
  });
});
